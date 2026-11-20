"""v303+ P0+ 一次性脚本：回填 ai_decisions.ts + 创建 autots trigger.

不需要重启 engine —— 直接在 live DB 上跑:
  1) 把 ts=NULL 的存量行从 created_at (Beijing 字面串) 解析成 UTC 秒
  2) 创建 trg_ai_decisions_autots trigger, 任何忘写 ts 的 INSERT 都会自动兜底

引擎下次启动时 migrate() 也会做同样的事 (幂等), 但跑这个能立即见效,
不需等待 engine 重启.

运行: python3 _apply_ts_fix.py
"""
import sqlite3
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))
import db # noqa: E402

DB_PATH = str(Path(__file__).resolve().parents[2] / "trident_event_bus.db")


def main():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        n = db._backfill_ai_decisions_ts(conn)
        print(f"[backfill] ai_decisions.ts 回填 {n} 条")
        db._ensure_ai_decisions_autots_trigger(conn)
        conn.commit()

        trig = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE name='trg_ai_decisions_autots'"
        ).fetchone()
        print(f"[trigger] {'OK' if trig else 'MISSING'} -> {dict(trig) if trig else None}")

        null_cnt = conn.execute(
            "SELECT count(*) FROM ai_decisions WHERE ts IS NULL"
        ).fetchone()[0]
        print(f"[verify] 剩余 ts=NULL: {null_cnt}")

        n_live = conn.execute(
            "SELECT count(*) FROM ai_decisions "
            "WHERE suggested_action IN ('BUY','SELL') "
            "AND ts >= strftime('%s','now')-4*3600"
        ).fetchone()[0]
        n_today = conn.execute(
            "SELECT count(*) FROM ai_decisions "
            "WHERE suggested_action IN ('BUY','SELL') "
            "AND substr(created_at,1,10)='2026-10-13'"
        ).fetchone()[0]
        print(f"[verify] live_signal_count (4h) = {n_live}  |  "
              f"today_local (Beijing date) = {n_today}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
