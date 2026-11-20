#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重启审计：抓「谁在杀引擎」。

背景（2026-11-17）
-----------------
引擎近 1 小时被 SIGTERM 9 次（17:38 / 17:42 / 17:44 / 17:45 / 17:51 / 18:00 /
18:08 / 18:17 / 18:26 / 18:52 / 19:14 / 19:21），`ops-audit` 已报警
「先停掉外部 kickstart」，但仓库里没有任何 `kickstart` / `bootout` 调用点，
`~/.trident/log/restart_calls.log` 也没有记录 —— 说明调用方在进程之外
（另一个会话 / 人工 / 外部脚本），我们只有被杀的视角。

这个脚本补上那个视角：高频采样进程表，一旦出现 `launchctl kickstart|bootout`、
`restart_backend.sh`、`kill ... engine.py` 这类命令行，就把**完整父子链**写下来。
进程本身只活几十毫秒，所以采样间隔默认 0.1s（单次 `ps` 约 20–40ms 开销）。

用法
----
    python3 backend/scripts/restart_audit.py                # 一直跑（默认）
    python3 backend/scripts/restart_audit.py --duration 3600  # 只跑 1 小时
    python3 backend/scripts/restart_audit.py --once          # 采一次就退（自检用）

输出
----
    backend/runtime/restart_audit.jsonl   每命中一条写一行 JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

# 命中即记录：这些命令行都可能导致引擎被杀
_PATTERNS = [
    re.compile(r"launchctl\s+(?:-\S+\s+)*(?:kickstart|bootout|stop|unload)\b"),
    re.compile(r"restart_backend\.sh|recover_stack\.sh"),
    re.compile(r"\bkill(?:\s+-(?:TERM|KILL|9|15|HUP|INT)\b)?\s+\d+"),
    re.compile(r"\b(?:pkill|killall)\b"),
    re.compile(r"\bkill\b.*engine\.py"),
]
# AgentInfra 后代短命 shell：命令走 fd9 通道、argv 看不到 kill，只能靠父链 + lr token 定位会话
_AGENT_CHAIN = re.compile(r"AgentInfraService")
_AGENT_SHELL = re.compile(r"^\s*(/bin/)?(ba|z|tc)?sh\b|exec\s+\d+>")

_OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)),
                    ),
    "runtime", "restart_audit.jsonl",
)


# 自家进程族：出现在父链里就不记（引擎包装脚本内部自带的 grep 排查子进程会误命中）
_SELF_FAMILY = re.compile(
    r"restart_audit|run_engine\.sh|run_api\.sh|run_bridge\.sh|"
    r"engine\.py|api_server\.py|bridge_server\.py|master_api\.py"
)


def _in_self_family(chain: List[str]) -> bool:
    return any(_SELF_FAMILY.search(c) for c in chain)


def match_line(line: str) -> bool:
    """该进程命令行是否属于「杀引擎」类操作。

    注意：不能只看「文本里出现过 kickstart」——`bash -c "...launchctl kickstart..."`
    这种包装命令行本身也含这段字面量，而长驻 shell / 采集脚本的正文同样会命中，
    会把审计日志灌成噪声。真正的杀手掌是**短命进程**，由 `confirm_short_lived`
    在下一个采样周期确认后落盘。

    还多一道长度判据：真实的调用命令很短（`launchctl kickstart -k gui/501/…` 约 50 字符，
    套一层 `bash -c "cd … && … && launchctl …"` 也就一百出头），而「在 heredoc 里写文档／
    写 CHANGELOG」这类命令动辄几百上千字符，正文里提到 kickstart 纯属巧合。
    实测（20:11:25）审计就把这类写文档的 bash 包装层记成了调用方，故加此门。
    """
    if "restart_audit" in line:
        return False          # 别把自己（以及自己所在的长命令行）记进去
    stripped = line.strip()
    if re.match(r"^(?:/bin/)?(grep|ps|awk|sed|egrep|fgrep)\b", stripped):
        return False          # 排查命令 argv 里带 kickstart/pkill 字样，不是真杀手
    if len(line) > 220:
        return False          # 长命令行多半是大段脚本/heredoc，不是一次真实调用
    return any(p.search(line) for p in _PATTERNS)


def confirm_short_lived(candidates: Dict[str, Dict[str, object]],
                        alive_pids: set) -> List[Dict[str, object]]:
    """在下一轮采样里确认：pid 已消失 ⇒ 短命进程 ⇒ 才是真正的调用方。

    长驻的 shell / 编辑器 / 采集脚本即使命令行里带 kickstart 字样，也会一直活着，
    在这里被剔除。
    """
    confirmed: List[Dict[str, object]] = []
    for pid in list(candidates):
        if pid not in alive_pids:
            confirmed.append(candidates.pop(pid))
    return confirmed


def parse_ps(text: str) -> List[Dict[str, str]]:
    """把 `ps -axo pid=,ppid=,uid=,tty=,command=` 的输出解析成行字典。"""
    rows: List[Dict[str, str]] = []
    for raw in text.splitlines():
        parts = raw.strip().split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, uid, tty, cmd = parts
        rows.append({"pid": pid, "ppid": ppid, "uid": uid, "tty": tty, "cmd": cmd})
    return rows


def _ps_rows() -> List[Dict[str, str]]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,uid=,tty=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    return parse_ps(out)


def ancestry(rows: List[Dict[str, str]], pid: str, depth: int = 5) -> List[str]:
    """沿 ppid 往上走，返回「谁调了它」的完整命令行链（最多 depth 层）。"""
    by_pid = {r["pid"]: r for r in rows}
    chain: List[str] = []
    cur = pid
    seen = set()
    for _ in range(depth):
        row = by_pid.get(cur)
        if not row or cur in seen:
            break
        seen.add(cur)
        chain.append(f"{row['pid']} {row['cmd'][:160]}")
        cur = row["ppid"]
        if cur in ("0", "1"):
            chain.append(f"<launchd pid={cur}>")
            break
    return chain


def _agent_shell(r: Dict[str, str], rows: List[Dict[str, str]]) -> Optional[str]:
    """若是 AgentInfraService 拉起的短命 shell，提取其 lr 会话 token；否则 None。"""
    chain = ancestry(rows, r["pid"])
    if not any(_AGENT_CHAIN.search(c) for c in chain):
        return None
    if not _AGENT_SHELL.search(r["cmd"]):
        return None
    m = re.search(r"/to/(lr_[0-9a-f]+)/", r["cmd"])
    return m.group(1) if m else "agent"


def scan_once(rows: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, object]]:
    rows = rows if rows is not None else _ps_rows()
    hits: List[Dict[str, object]] = []
    for r in rows:
        chain = ancestry(rows, r["pid"])
        role: Optional[str] = None
        token: Optional[str] = None
        if _in_self_family(chain):
            continue
        if match_line(r["cmd"]):
            role = "killer"
        else:
            token = _agent_shell(r, rows)
            if token:
                role = "agent_shell"
        if not role:
            continue
        hits.append({
            "role": role,
            "ts": round(time.time(), 3),
            "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": r["pid"],
            "uid": r["uid"],
            "tty": r["tty"],
            "lr": token,
            "cmd": r["cmd"][:400],
            "ancestry": chain,
        })
    return hits


def _append(records: List[Dict[str, object]]) -> None:
    if not records:
        return
    os.makedirs(os.path.dirname(_OUT_PATH), exist_ok=True)
    with open(_OUT_PATH, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="重启审计：抓谁在杀引擎")
    ap.add_argument("--duration", type=float, default=0,
                    help="运行秒数，0 = 一直跑（默认）")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="采样间隔秒（默认 0.1；进程只活几十毫秒，别调大）")
    ap.add_argument("--once", action="store_true", help="只采一次并打印，用于自检")
    args = ap.parse_args(argv)

    if args.once:
        hits = scan_once()
        print(json.dumps(hits, ensure_ascii=False, indent=2) if hits else "无命中")
        return 0

    print(f"[RESTART-AUDIT] 开始采样 interval={args.interval}s → {_OUT_PATH}", flush=True)
    started = time.time()
    candidates: Dict[str, Dict[str, object]] = {}
    logged: Dict[str, int] = {}
    while True:
        if args.duration and (time.time() - started) >= args.duration:
            break
        rows = _ps_rows()
        alive = {r["pid"] for r in rows}
        # 1) 上一轮「命中但还活着」的，这轮死了 ⇒ 确认是短命调用方
        for rec in confirm_short_lived(candidates, alive):
            _append([rec])
            print(f"[RESTART-AUDIT] {rec['ts_str']} 确认调用方 pid={rec['pid']} "
                  f"cmd={str(rec['cmd'])[:120]}", flush=True)
        # 2) 本轮新命中的，先进候选（下一轮再确认它是否短命）
        for rec in scan_once(rows):
            key = f"{rec['pid']}:{str(rec['cmd'])[:80]}"
            if key in logged or rec["pid"] in candidates:
                continue
            logged[key] = 1
            candidates[rec["pid"]] = rec
        if len(logged) > 5000:
            logged.clear()
        time.sleep(max(0.02, args.interval))
    # 收尾：退出时仍挂着的候选不写（它们可能是长驻进程）
    print("[RESTART-AUDIT] 结束", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
