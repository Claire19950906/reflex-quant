#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务实例守卫 —— 治「端口被非 launchd 的孤儿进程占着」。

事故形态（已发生两次，2026-11-16）：
    frontend: 一个脱离 launchd 的 `next start` 占着 3030 → launchd 新实例
              反复 EADDRINUSE → 前端"反复挂"。
    bridge:   一个 PPID=1 的 bridge_server.py 占着 8100、RSS 涨到 606MB →
              launchd 实例退出码 3（端口占用）→ 8100 由"孤儿"服务，
              launchd 里那行 PID 永远对不上，日志看起来像"服务正常"。

为什么危险：孤儿不归 launchd 管，bootout 带不走它，日志也看不到它的退出；
它还因为长期不重启而内存膨胀，最终把 api 挤到 OOM。

判定逻辑（都很保守）：
    1. 找出端口当前的 LISTEN 进程 pid；
    2. 和 `launchctl list` 里该 job 的 pid 比较；
    3. 两者不一致、且端口进程启动超过 GRACE_S(默认120s) → 判为孤儿，
       先记录，再按 --apply 决定是否 kill -9 + kickstart。
    4. 端口根本没有进程、且 launchd pid 在 → 说明正在启动，跳过。

用法：
    python3 backend/scripts/service_guard.py            # dry-run，只打印
    python3 backend/scripts/service_guard.py --apply    # 真正处置
    python3 backend/scripts/service_guard.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
from pathlib import Path
import sys
import time

# v454: api(8000) 已停用 —— 全套 lifespan/路由由 bridge(8100) 托管，不再巡检/自愈 8000。
SERVICES = [
    # {"name": "api", "port": 8000, "job": "com.trident.api", "proc": "api_server.py"},
    {"name": "bridge", "port": 8100, "job": "com.trident.bridge", "proc": "bridge_server.py"},
    {"name": "frontend", "port": 3030, "job": "com.trident.frontend", "proc": "next-server"},
]

GRACE_S = 120


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def port_owner_pid(port: int) -> int | None:
    out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return pids[0] if pids else None


def wait_port_free(port: int, timeout_s: int = 12) -> int | None:
    """杀掉占用者后轮询端口真正释放；超时仍被占则 SIGKILL 兜底。

    v435：旧逻辑 bootout 后只 sleep 2 就 bootstrap —— 旧进程卡在阻塞 IO/事件
    循环里不退净，端口没释放，新实例 EADDRINUSE 崩溃循环、旧进程脱离 launchd
    一直占端口（与 watchdog.sh 同一类事故）。这里确认端口空了再挂新实例。
    返回最终仍占用端口的 pid（已释放返回 None）。
    """
    for _ in range(timeout_s):
        if port_owner_pid(port) is None:
            return None
        time.sleep(1)
    holder = port_owner_pid(port)
    if holder is not None:
        try:
            os.kill(holder, signal.SIGKILL)
            time.sleep(2)
        except Exception:
            pass
        return port_owner_pid(port)
    return None


def job_pid(job: str) -> int | None:
    uid = os.getuid()
    out = _run(["launchctl", "print", f"gui/{uid}/{job}"])
    m = re.search(r"\bpid = (\d+)", out)
    return int(m.group(1)) if m else None


def proc_age_s(pid: int) -> int:
    out = _run(["ps", "-o", "etime=", "-p", str(pid)]).strip()
    if not out:
        return 0
    d = h = m = s = 0
    day = re.match(r"(\d+)-(\d+):(\d+):(\d+)", out)
    if day:
        d, h, m, s = map(int, day.groups())
    else:
        parts = out.split(":")
        if len(parts) == 3:
            h, m, s = map(int, parts)
        elif len(parts) == 2:
            m, s = map(int, parts)
    return d * 86400 + h * 3600 + m * 60 + s


def proc_rss_mb(pid: int) -> int:
    out = _run(["ps", "-o", "rss=", "-p", str(pid)]).strip()
    try:
        return int(int(out) / 1024)
    except Exception:
        return 0


def ancestors(pid: int) -> set[int]:
    """沿 ppid 向上走到 1，用于判断某进程是否属于 launchd 那个 job 的进程树。

    教训：wrapper 脚本常常是 `bash -c '... node ...'`（fork 而非 exec），
    于是 launchd 记的是 bash 的 pid，真正 LISTEN 的是它的子进程。
    只比较 pid 相等会把这种正常情况误判成孤儿 —— 必须看进程树。
    """
    chain, cur, guard = set(), pid, 0
    while cur and cur > 1 and guard < 20:
        chain.add(cur)
        out = _run(["ps", "-o", "ppid=", "-p", str(cur)]).strip()
        try:
            cur = int(out)
        except Exception:
            break
        guard += 1
    return chain


def _chain_root_ppid(pid: int) -> tuple[list[int], int]:
    """返回 (祖链 pid 列表, 最顶层进程的 ppid)。"""
    chain, cur, guard = [pid], pid, 0
    while cur and cur > 1 and guard < 25:
        out = _run(["ps", "-o", "ppid=", "-p", str(cur)]).strip()
        try:
            ppid = int(out)
        except Exception:
            return chain, 0
        if ppid <= 1:
            return chain, ppid
        chain.append(ppid)
        cur = ppid
        guard += 1
    return chain, 1


def check(svc: dict) -> dict:
    """判定一个端口的归属状态。

    关键区分（两次踩坑换来的）：
      - wrapper(exports + exec node) 下，launchd 记的 pid 就是 node；
        next-server 是它的子进程 → 端口 owner 在祖链里 → 正常。
      - 主进程被 launchd 杀掉、子进程活下来 → 祖链顶端挂在 1，且 launchd 侧无 pid
        → 这才是孤儿。仅为"pid 不相等"就判孤儿会把正常的分叉进程当成病。
    """
    owner = port_owner_pid(svc["port"])
    lp = job_pid(svc["job"])
    chain: list[int] = []
    root_ppid = 0
    if owner:
        chain, root_ppid = _chain_root_ppid(owner)

    state, note = "ok", ""
    if owner is None:
        state = "starting" if lp else "down"
    elif lp is not None and lp in chain:
        state = "ok"                       # 端口进程在 launchd 的祖链上
    elif lp is None and root_ppid == 1:
        age = proc_age_s(owner)
        if age < GRACE_S:
            state, note = "starting", f"孤儿但龄 {age}s < {GRACE_S}s，先放过"
        else:
            state, note = "orphan_conflict", "祖链挂在 1 且 job 无 pid"
    elif lp is not None and lp not in chain:
        age = proc_age_s(owner)
        if age >= GRACE_S:
            state, note = "orphan_conflict", f"端口进程 {owner} 不在 job {lp} 的祖链上"

    return {
        "service": svc["name"], "port": svc["port"], "job": svc["job"],
        "port_owner": owner, "job_pid": lp,
        "chain": chain[:4],
        "chain_root_ppid": root_ppid,
        "owner_age_s": proc_age_s(owner) if owner else None,
        "owner_rss_mb": proc_rss_mb(owner) if owner else None,
        "state": state, "note": note,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正 kill 孤儿并 kickstart")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = [check(s) for s in SERVICES]
    bad = [r for r in rows if r["state"] in ("orphan_conflict", "orphan_unjobbed")]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            print(
                f"[{ts}] {r['service']:9s} port={r['port']} state={r['state']:16s} "
                f"owner={r['port_owner']} job_pid={r['job_pid']} "
                f"rss={r['owner_rss_mb']}MB age={r['owner_age_s']}s {r['note']}"
            )

    # ── 自愈：服务被 launchd 卸载（不是崩溃，是"消失"）────────────────────
    # 2026-11-16 事故：一次 next build 把 load 推到 300+，api/bridge/frontend
    # 三个 job 直接从 launchd 列表里消失，端口全灭，而孤儿检测逻辑对此**无感**
    # （没有端口 owner 可杀，也没有孤儿可报）。这里补上"重新挂载"。
    unregistered = []
    for r in rows:
        if r["state"] == "down" and not r.get("job_pid"):
            unregistered.append(r)
    if unregistered:
        for r in unregistered:
            job = r["job"]
            if not args.apply:
                print(f"DRY-RUN: {r['service']} 端口 {r['port']} 无响应且 job 不在 launchd "
                      f"→ 将 enable + bootstrap {job}")
                continue
            uid = os.getuid()
            subprocess.run(["launchctl", "enable", f"gui/{uid}/{job}"],
                           capture_output=True, timeout=15)
            plist = Path.home() / "Library" / "LaunchAgents" / f"{job}.plist"
            if plist.exists():
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                               capture_output=True, timeout=20)
                print(f"已自愈: {r['service']} → 重新挂载 {job}")

    if not bad:
        if not args.json:
            print("结果: 无孤儿实例")
        return 0

    for r in bad:
        owner = r["port_owner"]
        if not args.apply:
            print(f"DRY-RUN: 将 kill -9 {owner}（{r['service']}:{r['port']} 孤儿）并 kickstart {r['job']}")
            continue
        # 2026-11-16：这里原来是 kill -9 + kickstart -k —— 这两招正是今天
        # 三次"锁僵死"的成因（强杀写事务中的进程 / 打断正在写库的服务）。
        # 现在统一交给 safe_kill 的判断，并把重启换成 bootout → bootstrap 的有序版本。
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import safe_kill as _sk
        except Exception:
            _sk = None

        if owner and _sk is not None:
            _sk.kill_pid(int(owner), wait_s=8.0, apply=True)   # 先 TERM，等 8s 才 -9，杀完复测写锁
        elif owner:
            subprocess.run(["kill", "-TERM", str(owner)], capture_output=True, timeout=10)

        _job = r["job"]
        uid = os.getuid()
        # 有序重启：bootout（发 TERM、带走整个进程树）→ 确认端口释放 → bootstrap
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_job}"],
                       capture_output=True, timeout=20)
        stuck = wait_port_free(r["port"])
        if stuck is not None:
            print(f"  ⚠️ 端口 {r['port']} 仍被 pid={stuck} 占用，bootstrap 后可能 EADDRINUSE")
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_job}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                           capture_output=True, timeout=20)
        print(f"已处置: {r['service']} owner={owner} → bootout/bootstrap {_job}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
