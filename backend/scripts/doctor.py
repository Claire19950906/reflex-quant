#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键体检 —— 回答一个问题：现在这套系统还能不能正常用？

用法:
    python3 scripts/doctor.py            # 完整体检（含真实探活，每个通道烧 1 次极小请求）
    python3 scripts/doctor.py --fast     # 跳过探活，只看本地状态（零消耗）

体检项:
    1. 机器资源（内存 / swap / load）—— 决定本地模型能不能跑
    2. 服务状态（引擎 / api / ollama）
    3. 额度账本（今天烧了多少、闸门拦没拦）
    4. LLM 通道探活（每个通道真发 1 个 8-token 请求，看是 429 还是 401）
    5. 闸门审计（有没有裸调模型的漏网点）

退出码: 0 = 一切正常; 1 = 无任何可用 LLM 通道; 2 = 部分降级
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

OK, WARN, BAD = "[OK]", "[!!]", "[XX]"
_state = {"exit": 0}


def section(title: str) -> None:
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def load_env() -> dict:
    env: dict = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as exc:
        print(BAD + " 读 .env 失败: " + str(exc))
    return env


def sh(cmd, timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


# -- 1. 机器资源 ------------------------------------------------
def check_machine() -> None:
    section("1. 机器资源（本地模型能不能跑就看这里）")
    mem = sh(["sysctl", "-n", "hw.memsize"])
    total_gb = int(mem) / 1073741824 if mem.isdigit() else 0
    vm = sh(["vm_stat"])
    free_mb = 0.0
    for line in vm.splitlines():
        m = re.match(r"Pages (free|speculative):\s+([0-9.]+)", line)
        if m:
            try:
                free_mb += float(m.group(2)) * 4096 / 1048576
            except Exception:
                pass
    swap = sh(["sysctl", "vm.swapusage"])
    la = sh(["sysctl", "-n", "vm.loadavg"])
    # macOS 返回 "{ 2.34 2.45 2.46 }"，取第一个数字
    lm = re.search(r"[0-9]+\.[0-9]+", la)
    load = lm.group(0) if lm else "?"
    print("  物理内存      : %.0f GB" % total_gb)
    print("  空闲内存      : %.0f MB" % free_mb)
    print("  load average  : %s" % load)
    print("  swap          : %s" % (swap or "n/a"))
    if free_mb < 500:
        print("  " + BAD + " 空闲内存 %.0fMB —— 本地 3B 模型跑不动（需要 ~2GB）" % free_mb)
        print("     -> 关掉豆包/Chrome/多余 VS Code 窗口，或把内存扩到 16GB")
        _state["exit"] = max(_state["exit"], 2)
    elif free_mb < 2000:
        print("  " + WARN + " 空闲内存 %.0fMB —— 本地模型勉强" % free_mb)
        _state["exit"] = max(_state["exit"], 2)
    else:
        print("  " + OK + " 内存充足")
    if load not in ("?", "") and float(load) > 50:
        print("  " + WARN + " load %s 偏高 —— 系统在换页" % load)


# -- 2. 服务状态 ------------------------------------------------
def check_services() -> None:
    section("2. 服务状态")
    out = sh(["launchctl", "list"])
    for label, name in [("引擎", "com.trident.engine"), ("Bridge", "com.trident.bridge"),
                        ("Ollama", "com.trident.ollama")]:
        hit = [ln for ln in out.splitlines() if ln.split()[-1:] == [name]]
        if not hit:
            print("  " + WARN + " %-7s 未注册（%s）" % (label, name))
            continue
        parts = hit[0].split()
        pid = parts[0].strip()
        status = parts[1].strip() if len(parts) > 1 else "?"
        if pid == "-":
            print("  " + BAD + " %-7s 已退出（last status %s）" % (label, status))
            if name == "com.trident.ollama":
                print("     -> 正常：Ollama.app 自己占着 11434 端口时就是这样（wrapper 在等）")
        else:
            print("  " + OK + " %-7s 运行中 (PID %s)" % (label, pid))
    pf = ROOT / "runtime" / "ai_worker_progress.json"
    if pf.exists():
        try:
            age = time.time() - pf.stat().st_mtime
            data = json.loads(pf.read_text(encoding="utf-8"))
            print("  " + OK + " 引擎主循环 step=%s (%.0fs 前)" % (data.get("step"), age))
            if age > 600:
                print("  " + WARN + " progress 已 %.0fs 没更新 —— 主循环可能卡住" % age)
        except Exception:
            pass
    else:
        print("  " + WARN + " progress 文件不存在（引擎可能刚启动）")



# -- 3. 额度账本 ------------------------------------------------
def check_budget() -> None:
    section("3. 额度账本")
    try:
        sys.path.insert(0, str(ROOT / "src_python"))
        import llm_budget # type: ignore
        u = llm_budget.usage(refresh=True)
        st = llm_budget.state()
        print("  今日请求数    : {:,} / {:,}   token: {:,} / {:,}".format(
            u.get("requests", 0), int(st.get("request_budget") or 0),
            u.get("tokens", 0), int(st.get("token_budget") or 0)))
        print("  账本可读      : " + ("是" if u.get("db_ok") else "否 [!!]"))
        print("  闸门状态      : " + ("放行" if st.get("ok") else "拦截") +
              "  (" + str(st.get("reason") or "-") + ")")
        if not st.get("ok"):
            print("  " + WARN + " 闸门正在拦截请求 —— 引擎会停在分析环节")
            _state["exit"] = max(_state["exit"], 2)
        # ── v322: 速率闸门 + 熔断共享状态 ────────────────────────────────
        rate = st.get("rate") or {}
        rpm_cap = int(rate.get("max_rpm") or 0)
        if rpm_cap > 0:
            used = int(rate.get("used_in_window") or 0)
            print("  速率闸门      : {:,} / {:,} 次/60s  {}".format(
                used, rpm_cap, "放行" if rate.get("ok") else "拦截"))
            if not rate.get("ok"):
                print("  " + WARN + " 速率已达上限 —— 请求被挡下（防瞬时打满）")
                _state["exit"] = max(_state["exit"], 2)
        else:
            print("  速率闸门      : 未启用 (LLM_MAX_RPM=0)")
        try:
            import circuit_breaker # type: ignore
            brks = circuit_breaker.report_all()
            open_ones = {k: v for k, v in brks.items()
                         if v.get("state") in ("open", "half_open")}
            shared = any(v.get("shared") for v in brks.values())
            print("  熔断器        : {} 个通道{}".format(
                len(brks), "（跨进程共享）" if shared else ""))
            for k, v in list(open_ones.items())[:5]:
                print("      {} → {}（冷却中，已跳过请求）".format(k, v.get("state")))
        except Exception:
            pass
        # ── v322: 单点故障检测（多个通道共用同一个 key）──────────────────
        # MiniMax 的 Token Plan 是**账户级**配额：多个通道共用一个 key 时，
        # 它们抢的是同一份额度 —— 看似冗余，实则是单点故障。
        try:
            sys.path.insert(0, str(ROOT / "src_python"))
            import config as _cfgd
            seen = {}
            for m in getattr(_cfgd, "AI_MODEL_ROSTER", []) or []:
                k = (m.get("api_key") or "").strip()
                if not k:
                    continue
                # 本地模型不共享额度池（没有 key、没有计费），不算单点故障
                if m.get("is_local") or "localhost" in str(m.get("api_base") or "") \
                        or "127.0.0.1" in str(m.get("api_base") or ""):
                    continue
                seen.setdefault(k, []).append(m.get("id") or m.get("label") or "?")
            dup = {k: v for k, v in seen.items() if len(v) > 1}
            if dup:
                for k, ids in list(dup.items())[:3]:
                    print("  " + WARN + " 单点故障：{} 个通道共用同一个 key".format(len(ids)))
                    print("      {} … {}  →  {}".format(
                        k[:14], k[-4:], ", ".join(str(i) for i in ids[:6])))
                print("      这些通道抢的是同一份额度 —— 换掉一个 key 等于全挂")
                _state["exit"] = max(_state["exit"], 2)
        except Exception:
            pass
    except Exception as exc:
        print("  " + WARN + " 读账本失败: %s: %s" % (type(exc).__name__, exc))


# -- 4. 通道探活 ------------------------------------------------
def probe(name: str, url: str, key: str, model: str) -> bool:
    if not key:
        print("  " + WARN + " %-14s 没有 key" % name)
        return False
    try:
        import requests
        t = time.time()
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "ok"}],
                  "max_tokens": 8},
            timeout=45,
        )
        dt = time.time() - t
        if r.status_code == 200:
            print("  " + OK + " %-14s 可用 (%.1fs)" % (name, dt))
            return True
        body = r.text[:110].replace("\n", " ")
        if r.status_code == 429:
            print("  " + BAD + " %-14s 额度用尽 (429) -> 需要充值/换套餐" % name)
        elif r.status_code in (401, 403):
            print("  " + BAD + " %-14s key 无效 (%d) -> 需要换 key" % (name, r.status_code))
        else:
            print("  " + BAD + " %-14s HTTP %d -> %s" % (name, r.status_code, body))
    except Exception as exc:
        print("  " + BAD + " %-14s 连不上 (%s)" % (name, type(exc).__name__))
    return False


def check_channels(fast: bool) -> None:
    section("4. LLM 通道探活" + ("（已跳过 --fast）" if fast else ""))
    env = load_env()
    if fast:
        for k in ("MINIMAX_API_KEY", "MINIMAX2_API_KEY", "OPENROUTER_API_KEY", "AIPING_API_KEY"):
            print("  .  %-24s %s" % (k, "已配置" if env.get(k) else "未配置"))
        return
    mm = env.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1").rstrip("/")
    res = []
    res.append(probe("MiniMax 账户1", mm + "/chat/completions",
                      env.get("MINIMAX_API_KEY", ""), env.get("MINIMAX_MODEL", "MiniMax-M3")))
    res.append(probe("MiniMax 账户2", mm + "/chat/completions",
                      env.get("MINIMAX2_API_KEY", ""), env.get("MINIMAX2_MODEL", "MiniMax-M3")))
    res.append(probe("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
                      env.get("OPENROUTER_API_KEY", ""), "openai/gpt-4o-mini"))
    res.append(probe("AIPING/Xinshu",
                      env.get("AIPING_BASE_URL", "").rstrip("/") + "/chat/completions",
                      env.get("AIPING_API_KEY", ""), env.get("AIPING_MODEL", "gpt-5.4-mini")))
    try:
        import requests
        t = time.time()
        requests.post("http://127.0.0.1:11434/api/generate",
                      json={"model": env.get("LOCAL_LLM_MODEL", "qwen2.5:3b"),
                            "prompt": "ok", "stream": False, "options": {"num_predict": 4}},
                      timeout=20)
        print("  " + OK + " %-14s 可用 (%.1fs)" % ("本地 Ollama", time.time() - t))
        res.append(True)
    except Exception:
        print("  " + WARN + " %-14s 不可用/太慢（8GB 机器正常现象）" % "本地 Ollama")
        res.append(False)
    if not any(res):
        print("\n  " + BAD + " 没有任何可用 LLM 通道 —— AI 分析功能无法工作。")
        print("     必须充值 MiniMax 或换 key；代码层面已无路可走。")
        _state["exit"] = 1
    elif not all(res):
        print("\n  " + WARN + " 部分通道可用 —— 系统能跑，但会有降级。")
        _state["exit"] = max(_state["exit"], 2)


# -- 5. 闸门审计 ------------------------------------------------
def check_audit() -> None:
    section("5. 闸门审计（有没有裸调模型的漏网点）")
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_llm_gates.py")],
                           capture_output=True, text=True, timeout=240)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            if "调用点总数" in line or "全部 LLM" in line:
                print("  " + line.strip())
            if "未保护" in line and "未保护 0" not in line:
                _state["exit"] = max(_state["exit"], 2)
    except Exception as exc:
        print("  " + WARN + " 审计失败: %s" % exc)


# -- 6. 静默失效审计 ------------------------------------------------
def check_silent_failures() -> None:
    """两类「静默失效」审计。

    2026-11-07 事故的教训：「待分析」4851 条两天不降，根因是
    raw_news.status 的 CHECK 不允许 'FILTERED' → 每条归档 UPDATE 被数据库拒绝
    → `except Exception: return 0` 把异常吞掉 → **日志 0 行，功能静默死亡**。

    这两个审计把同类问题一次性挡住：
      · audit_status_constraints.py —— 代码写的值 vs 数据库 CHECK 约束
      · audit_silent_except.py      —— 写库/调用路径上的静默吞异常
    """
    section("6. 静默失效审计（静默吞异常 / 写非法状态值）")
    # v483: audit_status_constraints.py 已挪到 scripts/oneoff/，doctor 一直按老路径找
    # 不到 → 每次都打 [XX]。改成按候选路径依次解析，脚本搬家不再让体检结果失真。
    for script, kw in (("audit_status_constraints.py", "检查了"),
                        ("audit_silent_except.py", "扫描完成")):
        _cand = [ROOT / "scripts" / script, ROOT / "scripts" / "oneoff" / script]
        _path = next((p for p in _cand if p.exists()), _cand[0])
        try:
            r = subprocess.run([sys.executable, str(_path)],
                               capture_output=True, text=True, timeout=600)
            out = (r.stdout or "") + (r.stderr or "")
            for line in out.splitlines():
                if kw in line or line.strip().startswith("❌") \
                        or line.strip().startswith("✅"):
                    print("  " + line.strip())
            if r.returncode != 0:
                _state["exit"] = max(_state["exit"], 2)
                print("  " + BAD + " %s 有问题 —— 详见 scripts/%s" % (script, script))
        except Exception as exc:
            print("  " + WARN + " %s 审计失败: %s" % (script, exc))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过通道探活（零消耗）")
    args = ap.parse_args()
    print("\n  系统体检  ·  Trident 金融后端")
    # 每一段独立兜底：某一段炸了不能带倒后面的检查
    for name, fn in (("机器资源", lambda: check_machine()),
                     ("服务状态", lambda: check_services()),
                     ("额度账本", lambda: check_budget()),
                     ("通道探活", lambda: check_channels(args.fast)),
                     ("闸门审计", lambda: check_audit()),
                     ("静默失效审计", lambda: check_silent_failures())):
        try:
            fn()
        except Exception as exc:
            print("  " + BAD + " %s 检查异常: %s: %s" % (name, type(exc).__name__, exc))
            _state["exit"] = max(_state["exit"], 2)
    print("=" * 62)
    verdict = {0: "一切正常", 2: "部分降级，功能受限", 1: "无可用 LLM 通道，功能不可用"}
    print("  结论: " + verdict.get(_state["exit"], "?"))
    print("=" * 62 + "\n")
    return _state["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
