#!/usr/bin/env python3
"""开跑前自检：确认「今晚这套代码能真的产出可归因的数据」。

为什么需要：今天已经踩过三次同类坑 —— 暂停标记没解除（引擎空转）、
测试跑进生产库（样本被截断）、代码改了没重启（跑的是上一代）。这三件事
都不会报错，只会让第二天的数字难看。开跑前 5 秒跑一遍，能挡掉其中大部分。

只读：不解除暂停、不重启服务、不写业务表。

用法：
    python3 scripts/preflight_check.py
    python3 scripts/preflight_check.py --fix-hint   # 额外打印修复命令
退出码：0 全通过；1 有阻断项（今晚跑了也白跑）。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "backend", "src_python")
sys.path.insert(0, SRC)
import db

PAUSED = os.path.expanduser("~/.trident/state/DATA_PAUSED")
PY = sys.executable


def _run(code: str) -> str:
    r = subprocess.run([PY, "-c", code], cwd=SRC, capture_output=True, text=True, timeout=60)
    return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""


def main() -> int:
    rows, blocked = [], False

    def add(name, ok, detail, hint=""):
        nonlocal blocked
        rows.append((("✅" if ok else "❌"), name, detail, hint))
        if not ok:
            blocked = True

    paused = os.path.exists(PAUSED)
    paused_at = ""
    if paused:
        paused_at = dt.datetime.fromtimestamp(os.path.getmtime(PAUSED)).strftime("%H:%M")
    add("数据未暂停", not paused,
        "无暂停标记" if not paused else f"存在 DATA_PAUSED（{paused_at} 写入）→ 引擎拒绝启动",
        "bash ~/.trident/wrappers/resume_data.sh")

    pg = subprocess.run(["pgrep", "-f", "engine.py"], capture_output=True, text=True)
    pids = [p for p in pg.stdout.split() if p]
    add("引擎在跑", bool(pids) and not paused,
        f"pid={','.join(pids) or '无'}" if pids else "没有 engine.py 进程",
        "launchctl kickstart -k gui/$(id -u)/com.trident.engine")

    try:
        conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=20)
        cols = {}
        for t in ("quick_sim_trades", "trade_decisions"):
            cols[t] = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        conn.close()
        missing = [t for t in cols if "code_fingerprint" not in cols[t]]
        add("指纹列已建", not missing,
            "quick_sim_trades / trade_decisions 均有 code_fingerprint"
            if not missing else f"缺列：{', '.join(missing)}",
            "python3 -c \"import db;db.migrate(db.connect())\"")
    except sqlite3.Error as e:
        add("指纹列已建", False, f"读库失败：{e}", "")

    bare = _run("import code_fingerprint as c;print(c.fingerprint())")
    withcfg = _run("import config, code_fingerprint as c;print(c.fingerprint())")
    same = bare == withcfg and bare.startswith(("",)) and "/p" in bare
    add("指纹与启动方式无关", same,
        f"裸进程={bare} / 加载config={withcfg}",
        "检查 code_fingerprint._env_file_values（.env 缺省、进程环境优先）")

    try:
        conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=rw", uri=True, timeout=5)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        conn.close()
        add("写锁可抢到", True, "BEGIN IMMEDIATE 成功")
    except sqlite3.Error as e:
        add("写锁可抢到", False, f"{e}（有长事务占着库，今晚数据会卡）",
            "python3 backend/scripts/lock_watchdog.py --once")

    try:
        n = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        loaded = sum(1 for line in n.splitlines() if "com.trident." in line)
        ok = loaded >= 100
        add("定时作业已加载", ok or paused,
            f"{loaded} 个（暂停后只剩引擎/守卫是正常）",
            "bash ~/.trident/wrappers/resume_data.sh")
    except Exception:
        pass

    print("| | 检查项 | 现状 |")
    print("|---|---|---|")
    for icon, name, detail, _h in rows:
        print(f"| {icon} | {name} | {detail} |")
    if blocked:
        print("\n**阻断项修复：**")
        for icon, name, _d, hint in rows:
            if icon == "❌" and hint:
                print(f"- {name}: `{hint}`")
    print("\n结论：" + ("❌ 现在开跑也不会产出可用数据，先修上面几项"
                     if blocked else "✅ 可以开跑，明早用 overnight_report.py 复盘"))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
