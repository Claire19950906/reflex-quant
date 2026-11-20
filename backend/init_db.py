#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Trident Agent MVP — SQLite Event Bus Initialization (fresh deployments only)
# ---------------------------------------------------------------------------
# 安全说明：本脚本【不会删除已有数据库】。schema 的单一权威定义在
# backend/src_python/db.py — migrate() 幂等，重复执行只会补齐缺失的列。
# 日常启动无需运行本脚本：engine.py / api_server.py 启动时会自动 migrate。

import os
import sqlite3
import sys

# 将 src_python 加入 sys.path，以便 import config / db
_SRC_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

import db
import causal_chains
import seed_restore


def init_db() -> None:
    conn = db.get_connection()
    try:
        db.migrate(conn)
        # v200+: 模拟盘默认开启 — fresh deployment 后立即建立默认 RUNNING run,
        # 避免下游 reflection / experience / journal worker 第一次扫描就 0 行.
        try:
            import paper_trading as _pt
            _pt.ensure_default_running(connection=conn)
        except Exception as _exc:
            print(f"[init_db] ensure_default_running 跳过: {type(_exc).__name__}: {str(_exc)[:80]}")
        # 预置因果链库种子（幂等：fresh deployment 后因果链库不再为空；
        # 已有同名链则跳过，重复执行安全）
        seeded = causal_chains.seed_defaults(conn)
        # 配置种子库自动入库（幂等：只在"配置表为空"时导入 —— 即首次部署；
        # 已有数据的表跳过，绝不用种子覆盖本机改动）。这样部署时不必记得
        # 手动跑 deploy/restore_seed.py（实测极易遗漏，漏了提示库就是空的）。
        imported = seed_restore.auto_import_from_seed(conn)
    finally:
        conn.close()

    print(f"[init_db] Database ready at {db.get_db_path()}")
    print(f"[init_db]   raw_news     — ready for ingestion")
    print(f"[init_db]   ai_decisions — ready for agent output")
    print(f"[init_db]   causal_chains — 预置种子新增 {seeded.get('added', 0)} 条，"
          f"库内共 {seeded.get('total', 0)} 条")
    if imported:
        print(f"[init_db]   seed_config  — 配置表为空，已自动入库 "
              f"{sum(imported.values())} 条（{len(imported)} 张表）")
    else:
        print(f"[init_db]   seed_config  — 配置表已有数据，未覆盖（强制导入：RESTORE_SEED=1）")
    print(f"[init_db]   WAL mode     — enabled (concurrent reads + writes)")
    print(f"[init_db]   (existing data preserved — migrate is idempotent)")


if __name__ == "__main__":
    init_db()
