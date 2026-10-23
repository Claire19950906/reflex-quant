"""R5 路径 A 接入测试：insight_verifier / apply_verification / reflection_scheduler。

覆盖：
  - review_insights 的 verification_* 列在 db.py 定义并可幂等补齐（铁律1）
  - verify_all_insights + write_verification_results 单连接往返
  - reflection_scheduler._verification_due 的 6 小时持久节流
"""

import os
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db  # noqa: E402
import insight_verifier as ivr  # noqa: E402
import apply_verification as apv  # noqa: E402
import reflection_scheduler as rs  # noqa: E402

_VERIF_COLS = (
    "verification_score", "verification_verdict", "verification_method",
    "verification_sample_n", "verification_kline_n", "verified_at",
)


def _mk_ts(dt: str) -> int:
    return int(time.mktime(time.strptime(dt, "%Y-%m-%d %H:%M:%S")) * 1000)


@pytest.fixture
def conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_db._CREATE_REVIEW_INSIGHTS)
    c.execute(_db._CREATE_KLINE_BARS)
    c.execute(_db._CREATE_QUICK_SIM_TRADES)
    _db._ensure_columns(c, "review_insights", _db._REVIEW_INSIGHTS_COLUMNS)
    c.commit()
    yield c
    c.close()


def test_verification_columns_defined_and_applied(conn):
    # 铁律1：verification_* 列必须在 db 中定义，且 _ensure_columns（migrate 同路径）能补齐
    defined = [name for name, _ in _db._REVIEW_INSIGHTS_COLUMNS]
    for c in _VERIF_COLS:
        assert c in defined, f"{c} 未在 db._REVIEW_INSIGHTS_COLUMNS 定义"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(review_insights)").fetchall()}
    for c in _VERIF_COLS:
        assert c in cols, f"缺少列 {c}"


def test_verify_and_write_roundtrip(conn):
    """event 型 insight + 事后上行 K 线 → verify_all_insights 产出 verdict，
    write_verification_results 落库 verification_* 客观记录（不停用）。"""
    ts0 = _mk_ts("2020-01-01 00:00:00")
    conn.execute(
        "INSERT INTO review_insights (rule, asset, scope, active, origin, kind, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("XAU BUY 中东冲突 做多黄金", "XAU", "all", 1, "real", 1, "2020-01-01 00:00:00"),
    )
    for i in range(1, 21):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("XAU", "1h", ts0 + i * 3600 * 1000, 100.0, 101.0, 99.5, 100.8, 10.0),
        )
    conn.commit()

    results = ivr.verify_all_insights(active_only=True, conn=conn)
    assert len(results) == 1
    assert results[0]["asset"] == "XAU"
    assert results[0]["verdict"] != ""

    stats = apv.write_verification_results(results, dry_run=False, deactivate_failed=False, conn=conn)
    assert stats["n_written"] == 1

    row = conn.execute(
        "SELECT verification_verdict, verification_score, verified_at "
        "FROM review_insights WHERE id=?",
        (results[0]["id"],),
    ).fetchone()
    assert row["verification_verdict"] != ""
    assert row["verified_at"] != ""
    # 未指定 deactivate_failed → 不停用
    assert conn.execute(
        "SELECT active FROM review_insights WHERE id=?", (results[0]["id"],)
    ).fetchone()["active"] == 1


def test_verification_due_no_verified_is_due(conn):
    assert rs._verification_due(conn) is True


def test_verification_due_recent_is_cooled_down(conn):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO review_insights (rule, created_at, verified_at) VALUES ('x','2020-01-01 00:00:00',?)",
        (now,),
    )
    conn.commit()
    assert rs._verification_due(conn) is False


def test_verification_due_old_is_due(conn):
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7 * 3600))
    conn.execute(
        "INSERT INTO review_insights (rule, created_at, verified_at) VALUES ('x','2020-01-01 00:00:00',?)",
        (old,),
    )
    conn.commit()
    assert rs._verification_due(conn) is True