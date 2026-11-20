"""v477 — 每笔成交/打点都带代码指纹（git 短哈希 + 参数哈希）。

钉死三件事：
  1. 成交台账 quick_sim_trades 有 code_fingerprint 列（新库建表 + 老库迁移）；
  2. trade_decisions 同样有该列，且 decision_writer 写入时自动填充；
  3. 指纹在"裸进程"和"已加载 config"两种启动方式下一致
     （否则同一代代码会被劈成两个指纹，样本无法合并）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import code_fingerprint
import db
from signals import decision_writer
import trade_decisions


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trade_decisions.init_schema(conn)
    db.migrate(conn)
    return conn


def test_tables_carry_fingerprint_column():
    conn = _fresh_db()
    for table in ("quick_sim_trades", "trade_decisions"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "code_fingerprint" in cols, (table, sorted(cols))


def test_decision_writer_fills_fingerprint():
    conn = _fresh_db()
    did = decision_writer.write_decision(
        conn, asset="BTC", intended_action="BUY", decision_type="ENTRY",
        source="test_fp", persist_signal_weight=False,
    )
    row = conn.execute(
        "SELECT code_fingerprint FROM trade_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0], "trade_decisions.code_fingerprint 为空"
    assert row[0] == code_fingerprint.fingerprint()
    # 形如 <git>/p<hash>，取不到 git 时退化为 nogit/p<hash>
    assert "/p" in row[0]


def test_fingerprint_stable_regardless_of_config_import():
    # 同一进程内先取一次（此时可能还没加载 .env），再导入 config 后重算
    first = code_fingerprint.fingerprint()
    import config
    second = code_fingerprint.fingerprint()
    assert first == second, (first, second)
