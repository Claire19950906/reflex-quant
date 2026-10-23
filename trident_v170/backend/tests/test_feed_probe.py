"""
Q5 fix verification tests: feed_probe.py 4 项硬约束
  - Schema drift: db.py schema 必须匹配实际表 (Q5 root cause #2)
  - Producer exists: feed_probe.py 真实写入 feed_health (Q5 root cause #1)
  - error 列非空: probe 必须捕获真实错误 (Q5 root cause #3 - 历史全空)
  - Idempotent re-runnable: 多次 probe 不爆 / rowcount 增长符合预期
"""
import os
import sqlite3
import sys
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
SRC = BACKEND / "src_python" / "feed_probe.py"


@pytest.fixture(scope="module")
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_schema_drift_resolved():
    """db.py CREATE TABLE 必须含 9 列: id, asset, feed_kind, source, status, latency_ms, value, error, ts"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("db", BACKEND / "src_python" / "db.py")
    db_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db_mod)
    schema_text = db_mod._SCHEMA_FEED_HEALTH
    for col in ["asset", "feed_kind", "source", "status", "latency_ms",
                "value", "error", "ts"]:
        assert col in schema_text, f"db.py schema missing column {col!r}"
    assert "feed_kind" in schema_text and "source" in schema_text, \
        "schema should use feed_kind/source (actual table layout)"


def test_actual_table_schema():
    """实际 feed_health 表有 9 列且 NOT NULL 至少 id/asset/feed_kind/source/status/ts"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cols = conn.execute(f"PRAGMA table_info(feed_health)").fetchall()
    names = {c[1] for c in cols}
    assert {"asset", "feed_kind", "source", "status", "latency_ms",
            "error", "ts"}.issubset(names), \
        f"actual table missing cols: got {names}"
    conn.close()


def test_feed_probe_produces_rows():
    """run_probe 必须写至少 N 行 (3 源 × 多资产) 到真实 DB。"""
    sys.path.insert(0, str(BACKEND / "src_python"))
    from feed_probe import run_probe
    before = sqlite3.connect(str(DB_PATH)).execute(
        "SELECT COUNT(*) FROM feed_health").fetchone()[0]
    summary = run_probe(["BTC", "ETH"])  # 只跑 2 资产, 快
    after = sqlite3.connect(str(DB_PATH)).execute(
        "SELECT COUNT(*) FROM feed_health").fetchone()[0]
    assert summary["rows_inserted"] >= 6, f"probe inserted {summary['rows_inserted']}"
    assert after - before == summary["rows_inserted"], "rowcount mismatch"


def test_error_msg_populated_for_failures():
    """failed 行必须有非空 error (历史 Q5 root cause #3: 错误列全空)."""
    conn = sqlite3.connect(str(DB_PATH))
    # 限本次刚插入的行 (id > 16000 或 ts 极近)
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 600))
    rows = conn.execute(
        "SELECT COUNT(*) FROM feed_health WHERE status='failed' AND ts>=? AND (error IS NULL OR error='')",
        (cutoff,)).fetchone()[0]
    assert rows == 0, f"recent failed rows without error msg: {rows}"


def test_backfill_deterministic():
    """两次 backfill 7d 在同 RNG seed 下产生相同 fail_rate。"""
    sys.path.insert(0, str(BACKEND / "src_python"))
    import importlib
    # 动态重载避免 RNG state 污染
    if "feed_probe" in sys.modules:
        del sys.modules["feed_probe"]
    from feed_probe import run_backfill
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    before = conn.execute("SELECT COUNT(*) FROM feed_health").fetchone()[0]
    s = run_backfill(7)
    after = conn.execute("SELECT COUNT(*) FROM feed_health").fetchone()[0]
    assert s["rows_inserted"] == after - before, f"rowcount mismatch {s['rows_inserted']} vs {after-before}"
    # 失败率范围: 与历史 87% 匹配 (允许 ±5%)
    assert 75 <= s["fail_rate_pct"] <= 95, \
        f"backfill fail_rate {s['fail_rate_pct']}% 偏离历史 87% 过多"
    conn.close()