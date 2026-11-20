#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v322: 把线上 raw_news.status 的 CHECK 放宽到允许 'FILTERED'（重建表）。

为什么需要（2026-11-07 生产实测）
-------------------------------
「待分析」4850 条永不下降。根因是**数据库约束**，不是逻辑：

  raw_news.status 的 CHECK 只允许 ('PENDING','PROCESSING','DONE','FAILED')
  ai_worker._expire_stale_news / scripts/cleanup_noise_pending.py 把超龄
  僵尸行标成 'FILTERED' → 每条 UPDATE 都被数据库拒绝
  → 异常被 `except Exception: return 0` 吞掉 → 库里 0 条 FILTERED
  → 前端「待分析」只涨不降

用法（会自动备份 + 前后校验）：
    python3 scripts/migrate_raw_news_filtered.py --dry-run   # 只看，不改
    python3 scripts/migrate_raw_news_filtered.py             # 执行
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE_DIR, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _counts(conn: sqlite3.Connection) -> dict:
    out = {}
    out["raw_news"] = conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    out["ai_decisions"] = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    out["pending"] = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE status='PENDING'").fetchone()[0]
    out["statuses"] = conn.execute(
        "SELECT status, COUNT(*) FROM raw_news GROUP BY status").fetchall()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不执行")
    args = ap.parse_args()

    import db

    db_path = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
        BASE_DIR, "trident_event_bus.db")
    if not os.path.exists(db_path):
        cand = os.path.join(BASE_DIR, "backend", "trident_event_bus.db")
        if os.path.exists(cand):
            db_path = cand
    print(f"DB: {db_path}")

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        before = _counts(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_news'"
        ).fetchone()
        live_sql = str(sql[0] or "") if sql else ""
        print("迁移前:", {k: v for k, v in before.items() if k != "statuses"})
        print("  状态分布:", before["statuses"])
        if "'FILTERED'" in live_sql:
            print("✅ 已经是新约束（允许 FILTERED），无需迁移")
            return 0
        print("当前 CHECK:", live_sql[live_sql.find("CHECK"):][:120])
        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0

        # ── 备份 raw_news 表结构+数据（可单独回滚这张表）──
        # ⚠️ 不要用 conn.iterdump()：它会遍历**整个 1.1GB 库**的所有表，
        #    只按表过滤写入文件也得先把全库 dump 出来 → 几分钟起步。
        #    用 sqlite3 CLI 的 `.dump raw_news`（只导这一张表）。
        bak = os.path.join(BASE_DIR, "runtime",
                            f"raw_news_dump_{int(time.time())}.sql")
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        import subprocess
        with open(bak, "w", encoding="utf-8") as fh:
            subprocess.run(
                ["sqlite3", db_path, ".dump raw_news"],
                stdout=fh, stderr=subprocess.PIPE, check=False, timeout=600)
        print(f"备份已写入: {bak} ({os.path.getsize(bak)} 字节)")

        # ── 执行重建 ──
        db._upgrade_raw_news_status_check(conn)

        after = _counts(conn)
        print("迁移后:", {k: v for k, v in after.items() if k != "statuses"})
        print("  状态分布:", after["statuses"])

        # ── 校验 ──
        ok = True
        if before["raw_news"] != after["raw_news"]:
            print(f"❌ 行数变了: {before['raw_news']} → {after['raw_news']}")
            ok = False
        if before["ai_decisions"] != after["ai_decisions"]:
            print(f"❌ 决策记录被删了: {before['ai_decisions']} → "
                  f"{after['ai_decisions']}（外键级联删！）")
            ok = False
        new_sql = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_news'"
        ).fetchone()[0] or "")
        if "'FILTERED'" not in new_sql:
            print("❌ CHECK 仍然不允许 FILTERED")
            ok = False
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='raw_news'").fetchall()
        if not idx:
            print("❌ 索引丢了")
            ok = False
        child = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='ai_decisions'").fetchone()[0] or "")
        if "REFERENCES raw_news(id)" not in child:
            print("❌ 外键引用被改写了")
            ok = False
        # 实际写一条 FILTERED 试试
        conn.execute(
            "INSERT INTO raw_news (source, content, timestamp, ts, status)"
            " VALUES ('__probe__','__probe__',datetime('now'),?, 'FILTERED')",
            (int(time.time()),))
        conn.execute("DELETE FROM raw_news WHERE source='__probe__'")
        conn.commit()
        print("✅ FILTERED 写入探针通过")

        print("\n" + ("✅ 迁移完成且校验通过" if ok else "❌ 迁移有问题，见上"))
        return 0 if ok else 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
