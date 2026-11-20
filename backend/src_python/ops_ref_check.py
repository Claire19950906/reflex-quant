#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维引用体检（v450）：把"定时任务调用的脚本已经不存在"这类静默故障揪出来。

为什么要有这个
--------------
今天查到的最大问题不是某个指标算错，而是：
`~/.trident/log/missed_signals.err.log` 每分钟一行
`can't open file .../market_flow.py: [Errno 2] No such file or directory`，
连续跑了几周没人发现 —— 因为**失败只写日志，不进任何页面**。
于是 flow_snapshots 整表停更，而 /edge 还在拿它算分位、下结论。

这一个脚本干三件小事：
  1. 扫 `~/Library/LaunchAgents/com.trident.*.plist` 和 `~/.trident/wrappers/*.sh`
     里出现的 `.py` 绝对路径，检查文件是否存在；
  2. 扫 plist 的 StandardOutPath / StandardErrorPath，检查目录是否存在；
  3. 把缺的列出来，退出码 1 表示"有引用断了"。

用法：
  python3 ops_ref_check.py           # 人看
  python3 ops_ref_check.py --json    # 机器看（health_check / cron 用）
"""
from __future__ import annotations

import glob
import json
import os
import plistlib
import re
import sys
from typing import Any, Dict, List

HOME = os.path.expanduser("~")
LAUNCH_AGENTS = os.path.join(HOME, "Library", "LaunchAgents")
WRAPPERS = os.path.join(HOME, ".trident", "wrappers")
PY_RE = re.compile(r"(/Users/[^\s\"'<>]+\.py)")


def _plist_refs(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as f:
            d = plistlib.load(f)
    except Exception as e:
        return {"label": os.path.basename(path), "error": str(e)}
    args = d.get("ProgramArguments") or ([d["Program"]] if d.get("Program") else [])
    return {
        "label": d.get("Label") or os.path.basename(path),
        "args": [a for a in args if isinstance(a, str)],
        "logs": [p for k, p in (("stdout", d.get("StandardOutPath")),
                                ("stderr", d.get("StandardErrorPath"))) if p],
    }


def scan() -> Dict[str, Any]:
    missing: List[Dict[str, str]] = []
    checked_scripts = 0
    jobs = []

    for path in sorted(glob.glob(os.path.join(LAUNCH_AGENTS, "com.trident.*.plist"))):
        info = _plist_refs(path)
        jobs.append(info["label"])
        for a in info.get("args", []):
            if a.endswith(".py") and a.startswith("/"):
                checked_scripts += 1
                if not os.path.exists(a):
                    missing.append({"job": info["label"], "script": a})
        for lg in info.get("logs", []):
            d = os.path.dirname(lg)
            if not os.path.isdir(d):
                missing.append({"job": info["label"], "script": f"日志目录 {d}"})

    # wrapper 里以 `$PY x.py` / `python3 x.py` 形式出现的同目录脚本
    for path in sorted(glob.glob(os.path.join(WRAPPERS, "*.sh"))):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        wname = os.path.basename(path)
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        for m in PY_RE.findall(code):
            checked_scripts += 1
            if not os.path.exists(m):
                missing.append({"job": wname, "script": m})
        # 裸脚本名：`"${PY}" foo.py` / `python3 foo.py`，按工程 src_python 解析
        for m in re.findall(r'(?:\\$\\{PY\\}|python3?)\s+([A-Za-z0-9_./-]+\.py)', text):
            target = os.path.join("/Users/claire/Desktop/金融1.0/backend/src_python", m)
            checked_scripts += 1
            if not os.path.exists(target):
                missing.append({"job": wname, "script": m})

    return {
        "jobs": len(jobs),
        "checked_scripts": checked_scripts,
        "missing": missing,
        "ok": not missing,
    }


def main(argv: List[str]) -> int:
    rep = scan()
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"任务 {rep['jobs']} 个，检查引用 {rep['checked_scripts']} 处")
        if not rep["missing"]:
            print("全部引用存在")
        else:
            print(f"断掉的引用 {len(rep['missing'])} 处：")
            for m in rep["missing"]:
                print(f"  - {m['job']}: {m['script']}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
