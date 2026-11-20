#!/usr/bin/env python3
"""
跨页一致性检查

同一件事在不同页面必须给出同一个数。本脚本不去猜页面代码，
而是直接比对「接口返回的数」与「库里真实的数」，把不一致点名。

检查项（每项都有明确的真值来源）：
  1 因果链页      /api/causal/chains            vs  causal_chains 行数
  2 归因页        /api/review/attributions/stats vs  event_move_attribution 行数
  3 错过信号页    missed_signals 表              vs  ai_decisions.missed_signal=1
  4 归因覆盖率    event_move_attribution         vs  critical_price_moves 异动数

用法：python3 page_consistency_check.py
退出码：0 全部一致 / 1 存在不一致
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"
import sys as _sys
_sys.path.insert(0, str(BACKEND / "src_python"))
import config as _config
API = _config.BRIDGE_BASE_URL


def api_json(path: str, timeout: int = 12):
    p = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-H", "User-Agent: Mozilla/5.0", API + path],
        capture_output=True, text=True)
    txt = (p.stdout or "").strip()
    if not txt:
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return None


def main() -> int:
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = lambda sql: conn.execute(sql).fetchone()[0]

    rows = []

    # 1 因果链
    d = api_json("/api/causal/chains?limit=1000")
    api_n = len(d) if isinstance(d, list) else None
    db_n = q("SELECT count(*) FROM causal_chains")
    rows.append(("因果链页", api_n, db_n, "页面条数 vs causal_chains"))

    # 2 归因页
    s = api_json("/api/review/attributions/stats")
    api_a = s.get("total") if isinstance(s, dict) else None
    db_a = q("SELECT count(*) FROM event_move_attribution")
    rows.append(("归因页", api_a, db_a, "页面总数 vs 归因表行数"))

    # 3 错过信号
    db_m1 = q("SELECT count(*) FROM missed_signals")
    db_m2 = q("SELECT count(*) FROM ai_decisions WHERE missed_signal=1")
    rows.append(("错过信号页", db_m1, db_m2, "漏检表 vs 决策表 missed_signal=1"))

    # 4 归因覆盖率
    db_c = q("SELECT count(*) FROM critical_price_moves")
    rows.append(("归因覆盖率", db_a, db_c, "被归因异动 vs 全部异动"))

    print("=" * 96)
    print("跨页一致性检查（左边=页面展示，右边=库内真值）")
    print("=" * 96)
    print(f"{'检查项':<12}{'页面/口径A':>12}{'库内/口径B':>12}  判定    说明")
    bad = 0
    for name, a, b, note in rows:
        same = (a == b)
        if not same:
            bad += 1
        print(f"{name:<12}{str(a):>12}{str(b):>12}  {'一致' if same else '不一致':<6}  {note}")
    print("-" * 96)
    if bad:
        print(f"[WARN] {bad} 处口径不一致。页面与库对不上时，用户看到的和实际算的不是一回事。")
        print("       典型原因：页面统计的是「已复查条数」，库统计的是「全部条数」，两者分母不同。")
    else:
        print("[OK] 四处全部一致。")
    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
