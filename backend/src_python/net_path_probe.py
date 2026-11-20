# -*- coding: utf-8 -*-
"""net_path_probe.py — 出网通路体检 (v324)

为什么需要它
------------
v324 实测 (2026-11-10) 的三起"看起来不相干"的故障, 根因都是**出网不通**:

  1. `ollama pull qwen2.5-coder:7b` → `connection reset by peer` (registry.ollama.ai)
  2. 远程 LLM 通道 → `quota_exhausted` (key 已废, 但代理缺失让探测也失败)
  3. Yahoo / Polygon / FRED / 翻译 API 在常驻进程里全是 SSL EOF
     —— launchd 拉起的进程**不继承 shell 环境变量**, 所以
     `~/.trident/env` 里的 HTTP_PROXY/HTTPS_PROXY 为空 = 裸奔出网

前两件是"代理缺失"的直接后果, 第三件是"代理配置了但没生效"。
本模块把"到底哪个源通 / 哪条路被堵"一次性测清楚, 并给出可直接粘贴的修复行。

用法
----
    cd backend/src_python
    python3 net_path_probe.py                 # 只测, 打印报告
    python3 net_path_probe.py --json          # 机读
    python3 net_path_probe.py --write-env     # 探测到代理则写入 ~/.trident/env

铁律
----
· 默认**只读**: 不改任何系统配置, 只发 GET。
· `--write-env` 才写 ~/.trident/env, 且只写代理三行, 写前自动备份。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 被测目标 (显示名, URL, 是否需要海外直连) ─────────────────────────────
TARGETS: Tuple[Tuple[str, str, bool], ...] = (
    ("Binance 现货", "https://api.binance.com/api/v3/ping", False),
    ("Yahoo Finance",
     "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1d&interval=1d", True),
    ("FRED 宏观", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", True),
    ("Google News RSS", "https://news.google.com/rss/search?q=bitcoin", True),
    ("百度热搜", "https://top.baidu.com/api/board?tab=realtime", False),
    ("Ollama Registry",
     "https://registry.ollama.ai/v2/library/qwen2.5-coder/manifests/7b", True),
    ("OpenRouter", "https://openrouter.ai/api/v1/models", True),
)

# ── 国内直连源 (v324) ─────────────────────────────────────────────────────
# 没有代理时的可用路径: 实测 2026-11-10 腾讯/新浪/同花顺/财联社/Coingecko 全部 200。
# 这组是"没有代理也能用"的清单 —— 体检报告要能把它们单独标出来,
# 否则用户看到一串 FAIL 会误以为整个系统都不能用了。
CN_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("腾讯行情", "https://qt.gtimg.cn/q=sh000001"),
    ("新浪行情", "https://hq.sinajs.cn/list=sh000001"),
    ("同花顺", "https://q.10jqka.com.cn/"),
    ("财联社", "https://www.cls.cn/"),
    ("Coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"),
)

# 常见本地代理端口 ( Clash / v2ray / Surge / Stash / sing-box ... )
COMMON_PROXY_PORTS: Tuple[int, ...] = (
    7890, 7891, 7892, 7897, 1080, 1081, 1087, 10808, 10809,
    8888, 6152, 6153, 20171, 33210, 4780, 10810,
)

# 代理客户端进程名特征
PROXY_PROCESS_HINTS: Tuple[str, ...] = (
    "clash", "mihomo", "v2ray", "xray", "sing-box", "singbox",
    "surge", "shadowsocks", "verge", "quantumult", "stash",
)

ENV_PATH = Path(os.path.expanduser("~/.trident/env"))
TIMEOUT = 12.0


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def detect_local_proxy() -> Dict[str, Any]:
    """探测本机是否有代理端口在监听。"""
    hits = [p for p in COMMON_PROXY_PORTS if _port_open(p)]
    procs: List[str] = []
    try:
        out = subprocess.run(["ps", "-Ao", "comm="], capture_output=True,
                             text=True, timeout=6).stdout
        for line in out.splitlines():
            low = line.strip().lower()
            if low and any(h in low for h in PROXY_PROCESS_HINTS):
                procs.append(line.strip())
    except Exception:
        pass
    return {"ports": hits, "processes": sorted(set(procs))}


def _probe_once(url: str, timeout: float) -> Dict[str, Any]:
    """真正发一次 GET。异常在这里被吞成 {ok: False, ...}。"""
    import urllib.request
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    }
    # 新浪行情必须带 Referer, 否则 403 (实测 2026-11-10)
    if "sinajs.cn" in url:
        headers["Referer"] = "https://finance.sina.com.cn"
    req = urllib.request.Request(url, method="GET", headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(512)
            code = getattr(r, "status", 200)
        return {"ok": True, "code": code,
                "elapsed_ms": int((time.time() - t0) * 1000), "error": ""}
    except Exception as exc:
        return {"ok": False, "code": getattr(exc, "code", None),
                "elapsed_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:90])}


def probe_url(url: str, timeout: float = TIMEOUT,
              hard_cap: Optional[float] = None) -> Dict[str, Any]:
    """GET 一次, 返回 {ok, code, elapsed_ms, error}。

    v324: 加**硬时限**。urllib 的 timeout 只管 socket 读写, 不管 DNS 解析 ——
    实测被墙的 Google News 单项能拖 72s (12s timeout × 多次解析/重定向),
    串行体检要跑两分钟。这里把探测放进 daemon 线程并 join(硬时限),
    超时直接判 FAIL, 线程随进程退出回收, 不拖住体检。
    """
    cap = hard_cap if hard_cap is not None else max(timeout * 2.0, 8.0)
    box: Dict[str, Dict[str, Any]] = {}

    def _work() -> None:
        try:
            box["r"] = _probe_once(url, timeout)
        except Exception as exc:                     # 兜底, 永不外泄
            box["r"] = {"ok": False, "code": None, "elapsed_ms": int(cap * 1000),
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:90])}

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(cap)
    if "r" in box:
        return box["r"]
    return {"ok": False, "code": None, "elapsed_ms": int(cap * 1000),
            "error": "硬超时 (%.0fs 内无响应)" % cap}



def run_probe(proxy: Optional[str] = None, timeout: float = TIMEOUT) -> Dict[str, Any]:
    """逐个探测目标源。proxy 非空则走该代理。

    v324: **并发**探测 —— 串行时一个被墙源能拖 72s (Google News 实测),
    整个体检要跑两分钟, 没人愿意等。并发后总耗时 = 最慢那一个 (≈timeout)。
    """
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy

    def _probe_overseas(item: Tuple[str, str, bool]) -> Dict[str, Any]:
        name, url, overseas = item
        r = probe_url(url, timeout)
        r["name"] = name
        r["url"] = url
        r["overseas"] = overseas
        return r

    def _probe_cn(item: Tuple[str, str]) -> Dict[str, Any]:
        name, url = item
        r = probe_url(url, timeout)
        r["name"] = name
        r["url"] = url
        return r

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as pool:
        fut_ov = [pool.submit(_probe_overseas, t) for t in TARGETS]
        fut_cn = [pool.submit(_probe_cn, t) for t in CN_TARGETS]
        results = [f.result() for f in fut_ov]
        cn_results = [f.result() for f in fut_cn]

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probe_seconds": round(time.time() - t0, 1),
        "proxy_used": proxy or "",
        "env_proxy": {
            "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
            "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
        },
        "local_proxy": detect_local_proxy(),
        "results": results,
        "cn_results": cn_results,
        "cn_ok": sum(1 for r in cn_results if r.get("ok")),
        "cn_total": len(cn_results),
    }


def suggest_proxy_line(local: Dict[str, Any]) -> str:
    """根据探测结果给出建议的代理配置。"""
    ports = local.get("ports") or []
    if ports:
        p = ports[0]
        return ("检测到本机代理端口 %d —— 建议在 %s 里填:\n"
                "    HTTP_PROXY=http://127.0.0.1:%d\n"
                "    HTTPS_PROXY=http://127.0.0.1:%d\n"
                "  改完重启: launchctl kickstart -k gui/$(id -u)/com.trident.{api,bridge,engine}"
                % (p, ENV_PATH, p, p))
    return ("未检测到本地代理端口 —— 请先开启你的代理客户端 (Clash/Surge/v2ray 等),\n"
            "  然后在 %s 里填上它的**本地 HTTP 端口** (不是系统 TUN 模式),\n"
            "  再执行: launchctl kickstart -k gui/$(id -u)/com.trident.{api,bridge,engine}"
            % ENV_PATH)


def write_env_proxy(proxy_url: str) -> Dict[str, Any]:
    """把代理写进 ~/.trident/env (写前备份)。只动代理三行。"""
    if not proxy_url:
        return {"ok": False, "error": "没有可用代理地址"}
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if ENV_PATH.exists():
        backup = ENV_PATH.with_suffix(".env.bak")
        shutil.copy2(str(ENV_PATH), str(backup))
    lines: List[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    new_lines: List[str] = []
    seen = set()
    for ln in lines:
        stripped = ln.strip()
        matched = None
        for k in keys:
            if stripped.startswith(k + "="):
                matched = k
                break
        if matched:
            if matched in seen:
                continue
            seen.add(matched)
            if matched == "ALL_PROXY":
                new_lines.append("%s=" % matched)
            else:
                new_lines.append("%s=%s" % (matched, proxy_url))
        else:
            new_lines.append(ln)
    for k in keys:
        if k not in seen:
            new_lines.append("%s=%s" % (k, proxy_url if k != "ALL_PROXY" else ""))
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(ENV_PATH),
            "backup": str(backup) if backup else "", "proxy": proxy_url}


def render_report(diag: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("=" * 74)
    L.append("出网通路体检 (v324)  %s" % diag.get("generated_at"))
    L.append("=" * 74)
    L.append("当前进程 HTTP_PROXY = %r" % (diag.get("env_proxy", {}).get("HTTP_PROXY") or ""))
    lp = diag.get("local_proxy") or {}
    L.append("本机代理端口 = %s   代理进程 = %s"
             % (lp.get("ports") or "无", lp.get("processes") or "无"))
    if diag.get("probe_seconds"):
        L.append("探测总耗时 = %.1fs (并发)" % diag["probe_seconds"])
    L.append("")
    L.append("【海外/通用源】")
    L.append("%-18s %-10s %-9s %s" % ("源", "结果", "耗时", "备注"))
    L.append("-" * 74)
    for r in diag.get("results") or []:
        status = "OK %s" % r.get("code") if r.get("ok") else "FAIL"
        note = r.get("error") or ("需海外直连" if r.get("overseas") else "")
        L.append("%-18s %-10s %-9s %s" % (r.get("name"), status,
                                          "%dms" % r.get("elapsed_ms", 0), note[:34]))
    cn = diag.get("cn_results") or []
    if cn:
        L.append("")
        L.append("【国内直连源】(没有代理也能用)")
        L.append("%-18s %-10s %-9s %s" % ("源", "结果", "耗时", "备注"))
        L.append("-" * 74)
        for r in cn:
            status = "OK %s" % r.get("code") if r.get("ok") else "FAIL"
            L.append("%-18s %-10s %-9s %s" % (r.get("name"), status,
                                              "%dms" % r.get("elapsed_ms", 0),
                                              (r.get("error") or "")[:34]))
        L.append("  → 国内源可用 %d/%d (行情/新闻/加密价格不依赖代理)" % (
            diag.get("cn_ok", 0), diag.get("cn_total", 0)))
    L.append("")
    L.append("【修复建议】")
    L.append(suggest_proxy_line(lp))
    L.append("")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="出网通路体检 (只读)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--proxy", default="", help="用指定代理测 (如 http://127.0.0.1:7890)")
    ap.add_argument("--write-env", action="store_true",
                    help="把探测到的本地代理写入 ~/.trident/env")
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    args = ap.parse_args(list(argv) if argv is not None else None)

    diag = run_probe(proxy=args.proxy or None, timeout=args.timeout)
    if args.write_env:
        ports = diag.get("local_proxy", {}).get("ports") or []
        url = args.proxy or ("http://127.0.0.1:%d" % ports[0] if ports else "")
        diag["write_env"] = write_env_proxy(url)
    if args.json:
        print(json.dumps(diag, ensure_ascii=False, indent=1))
    else:
        print(render_report(diag))
        if args.write_env:
            print("写入结果:", diag.get("write_env"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
