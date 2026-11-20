#!/usr/bin/env python3
"""AI 通道体检 —— 一条命令看清"决策链还能不能跑"。

背景：ai_decisions 长期为 0，根因不是代码，而是 gate/advisor 两条通道都接不上。
过去每轮都要手工 curl 各个 provider，容易漏、也不可复现。本脚本把探测固化：
  · 对每个已配置的 LLM provider 各发一次极小的 chat 请求（max_tokens=4）
  · 顺带探测本地 Ollama 端口是否在听
  · 输出"可用 / 不可用 + 原因"，退出码：0=至少一条通道可用，2=全断

用法：
  python3 scripts/audit_ai_channels.py            # 正常探测（约 10-40s）
  python3 scripts/audit_ai_channels.py --json     # 机器可读
  python3 scripts/audit_ai_channels.py --skip-local   # 不探本地

判据（与运行时一致）：能拿到非空 message.content 才算可用；
HTTP 429 = 额度/限流，401/403 = key 无效，000/URLError = 网络不通，
503 + model_not_found = 网关活着但该模型无可用通道。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

BACKEND = "/Users/claire/Desktop/金融1.0/backend"
ENV_FILE = os.path.join(BACKEND, ".env")

# 每个 provider: (env 前缀, 试投的模型候选列表, base 缺省值)
# 注意：有些 provider 在 .env 里只配了 key、没配 base（如 OPENROUTER），
# 此处补默认 base，否则会误报"未配置"。
PROVIDERS = [
    ("MINIMAX", ["MiniMax-M2.7-highspeed", "MiniMax-M3.1-Flash"], ""),
    ("AIPING", ["gpt-5.4-mini", "claude-opus-4-8"], ""),
    ("OPENROUTER", ["meta-llama/llama-3.1-8b-instruct"], "https://openrouter.ai/api/v1"),
    ("DEEPSEEK", ["deepseek-chat"], ""),
    ("XINSHU", ["gpt-4o-mini"], ""),
    ("ARK", ["doubao-pro-32k"], ""),
]


def read_env(path: str) -> dict:
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = re.match(r"(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def probe_chat(base: str, key: str, model: str, timeout: float = 20.0) -> tuple[str, str, float]:
    """返回 (状态, 说明, 耗时秒)。状态 ∈ ok / quota / auth / net / model / http"""
    url = base.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "reply OK"}],
        "max_tokens": 4,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = json.loads(resp.read())
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if content.strip():
            return "ok", content.strip()[:24], time.time() - t0
        return "http", "返回空内容", time.time() - t0
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode()[:160]
        except Exception:
            pass
        low = snippet.lower()
        if exc.code == 429 or "用量上限" in snippet or "rate_limit" in low:
            kind = "quota"
        elif exc.code in (401, 403):
            kind = "auth"
        elif "model_not_found" in low or "no available channel" in low:
            kind = "model"
        else:
            kind = "http"
        return kind, f"HTTP {exc.code} {snippet[:110]}", time.time() - t0
    except Exception as exc:  # URLError / 超时 / TLS
        return "net", f"{type(exc).__name__} {str(exc)[:70]}", time.time() - t0


def probe_local(base: str) -> tuple[str, str]:
    """本地 Ollama：先看端口是否在听，再看 /api/tags。"""
    try:
        host_port = base.split("//")[-1].split("/")[0]
        host, _, port = host_port.partition(":")
        sock = socket.create_connection((host or "127.0.0.1", int(port or 11434)), timeout=3)
        sock.close()
    except Exception:
        return "down", "端口未监听（服务没起）"
    try:
        resp = urllib.request.urlopen(base.rstrip("/") + "/api/tags", timeout=5)
        models = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        return ("up", f"服务在，模型 {len(models)} 个") if models else ("up", "服务在，但无模型")
    except Exception as exc:
        return "down", f"{type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-local", action="store_true")
    args = ap.parse_args()

    env = read_env(ENV_FILE)
    rows: list[dict] = []
    usable = 0

    for prefix, models, default_base in PROVIDERS:
        base = env.get(f"{prefix}_BASE_URL", "") or default_base
        key = env.get(f"{prefix}_API_KEY", "")
        if not base or not key:
            rows.append({"channel": prefix, "state": "unconfigured",
                         "detail": "base 或 key 为空", "secs": 0.0})
            continue
        best = None
        for model in models:
            state, detail, secs = probe_chat(base, key, model)
            best = best or {"channel": f"{prefix}/{model}", "state": state,
                            "detail": detail, "secs": round(secs, 1)}
            if state == "ok":
                best = {"channel": f"{prefix}/{model}", "state": state,
                        "detail": detail, "secs": round(secs, 1)}
                break
        rows.append(best)
        if best["state"] == "ok":
            usable += 1

    if not args.skip_local:
        local_base = env.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        state, detail = probe_local(local_base)
        rows.append({"channel": "local/ollama", "state": "ok" if state == "up" else "down",
                     "detail": detail, "secs": 0.0})
        if state == "up":
            usable += 1

    if args.json:
        print(json.dumps({"usable": usable, "channels": rows}, ensure_ascii=False, indent=2))
    else:
        mark = {"ok": "可用", "quota": "额度满", "auth": "key无效",
                "model": "无可用通道", "net": "网络不通", "down": "未启动",
                "unconfigured": "未配置", "http": "异常"}
        print("AI 通道体检 —— 能拿到非空回答才算\"可用\"\n")
        for row in rows:
            tag = mark.get(row["state"], row["state"])
            print(f"  {row['channel']:<34}{tag:<8}{row['secs']:>5.1f}s  {row['detail'][:88]}")
        print()
        if usable:
            print(f"结论：{usable} 条通道可用 —— 决策链可以跑。")
        else:
            print("结论：全部不可用 —— 此时 ai_decisions 为 0 是正确行为，"
                  "需要给一把可用 key 或加载本地 ollama。")

    return 0 if usable else 2


if __name__ == "__main__":
    sys.exit(main())
