#!/usr/bin/env python3
"""
干净起点清洗（可回滚）

背景：反复修改留下的历史残留混在同一条链路里：
  · review_insights 里有 162 条来自 seeded / quick_sim（模拟或预置）
  · causal_chains 里有 203 条来自 auto_quick_sim，另有 252 条"覆盖度告警"模板
  · causal_chain_outcomes.magnitude_hit 自报 85.7%，按区间真算只有 28.4%
  · news_events 的 ai_decision_id 只有迁移函数会写，实时链路不写 → 10-27 后断供

做法（只隔离不物理删除，随时可回滚）：
  1 备份三张表成 INSERT 脚本 → runtime/backup/clean_YYYYMMDD.sql
  2 模拟来源的洞察 active=0；auto_quick_sim 链 active=0
  3 magnitude_hit 按 actual_pct_5d 是否落在 [lo,hi] 重算
  4 打印每步影响行数

回滚：把备份里的 INSERT 重新执行即可（或把 active 改回 1）。

用法：python3 clean_epoch.py [--dry-run]

⚠ 已废弃（v340, 2026-11-15）
============================
本脚本**不是**"清空历史数据"的正确工具，历史上有多次"以为清过了、其实没清"
都源于它：它只碰 3 张表（review_insights / auto_quick_sim 链 / magnitude_hit），
一条决策、一条权重、一条结算都不删；它写的 data_epoch 表当年没有任何读取方，
所以统计口径照旧读全表；而回填 cron 又会把旧账重新长回来。

真正可用的工具是 reset_derived_epoch.py：
    python3 reset_derived_epoch.py --dry-run   # 先看会清掉多少
    python3 reset_derived_epoch.py --apply     # 备份 → 单事务清 73 表 → 写纪元
它清空 judgment/ledger 类数据、保留原始事实与因果链定义，并被
backfill_critical_moves / fast_move_detector / frozen_ruler / 验收脚本读取。

保留本文件只为回看历史口径；默认拒绝执行，需要时显式加 --i-know-this-is-legacy。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
BACKUP_DIR = BACKEND / "runtime" / "backup"
TABLES = ["review_insights", "causal_chains", "causal_chain_outcomes"]


def dump(conn, out: Path) -> int:
    lines = [f"-- clean backup {datetime.now():%Y-%m-%d %H:%M:%S}\n"]
    total = 0
    for t in TABLES:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not schema:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        lines.append(f"\n-- table {t}\n{schema[0]};\nBEGIN;\n")
        for row in conn.execute(f"SELECT * FROM {t}"):
            vals = []
            for v in row:
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    vals.append("'" + str(v).replace("'", "''") + "'")
            lines.append(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join(vals)});\n")
            total += 1
        lines.append("COMMIT;\n")
    out.write_text("".join(lines), encoding="utf-8")
    return total


LEGACY_BANNER = """\
[REFUSED] clean_epoch.py 已废弃（v340）。
  它只清 3 张表、且写出的 data_epoch 当年无人读取 —— 这正是"清了好几次清不掉"的来源。
  正确工具: python3 reset_derived_epoch.py --dry-run / --apply
  确需回看历史口径: python3 clean_epoch.py --i-know-this-is-legacy [--dry-run]
"""


def main() -> int:
    if "--i-know-this-is-legacy" not in sys.argv:
        print(LEGACY_BANNER, file=sys.stderr)
        return 64
    dry = "--dry-run" in sys.argv
    apply_only = "--apply-only" in sys.argv
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = BACKUP_DIR / f"clean_{stamp}.sql"
    if apply_only:
        prev = sorted(BACKUP_DIR.glob("clean_*.sql"))
        out = prev[-1] if prev else out
        print(f"跳过备份，沿用 {out.name}")
    else:
        ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        n = dump(ro, out)
        ro.close()
        print(f"备份 {n} 行 → {out}  ({out.stat().st_size/1024:.0f} KB)")
    if dry:
        print("dry-run：不写库")
        return 0

    conn = sqlite3.connect(str(DB), timeout=90)
    conn.execute("PRAGMA busy_timeout=90000")
    try:
        c1 = conn.execute(
            "UPDATE review_insights SET active=0 "
            "WHERE active=1 AND origin IN ('seeded','quick_sim')").rowcount
        c2 = conn.execute(
            "UPDATE causal_chains SET active=0 "
            "WHERE active=1 AND origin='auto_quick_sim'").rowcount
        c3 = conn.execute(
            "UPDATE causal_chain_outcomes SET magnitude_hit = "
            "CASE WHEN actual_pct_5d IS NOT NULL "
            "     AND predicted_mag_lo IS NOT NULL AND predicted_mag_hi IS NOT NULL "
            "     AND actual_pct_5d BETWEEN predicted_mag_lo AND predicted_mag_hi "
            "THEN 1 ELSE 0 END").rowcount
        conn.execute("""CREATE TABLE IF NOT EXISTS data_epoch(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, note TEXT,
            backup_file TEXT)""")
        conn.execute("DELETE FROM data_epoch")
        conn.execute("INSERT INTO data_epoch(started_at, note, backup_file) VALUES(?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "清洗反复修改留下的模拟/模板残留，只隔离未删除", out.name))
        conn.commit()
        print(f"隔离模拟洞察 {c1} 条；隔离 quick_sim 因果链 {c2} 条")
        print(f"重算 magnitude_hit：{c3} 行（真算口径 = 实际 5 日收益落在预测区间）")
        r = conn.execute(
            "SELECT sum(magnitude_hit), count(*) FROM causal_chain_outcomes "
            "WHERE magnitude_hit IS NOT NULL").fetchone()
        print(f"重算后幅度命中 = {r[0]}/{r[1]} = {(r[0]/r[1]*100 if r[1] else 0):.1f}%")
        for label, sql in (
                ("洞察来源(active)", "SELECT origin, count(*) FROM review_insights WHERE active=1 GROUP BY 1 ORDER BY 2 DESC"),
                ("因果链来源(active)", "SELECT origin, count(*) FROM causal_chains WHERE active=1 GROUP BY 1 ORDER BY 2 DESC")):
            print(f"\n{label}")
            for k, v in conn.execute(sql):
                print(f"  {k}: {v}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
