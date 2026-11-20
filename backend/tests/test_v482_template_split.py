"""v482 反思三修：HOLD 漏检记账从反思表分出去。

背景：
    trade_reflections_v2 里 93% 是 missed_signal 模板行（2026-11-19 实测 52/56），
    它们没有成交、没有 pnl、永远判不出对错，却把"反思条数/深度分/一致率"全部拉平。
    且模板文案无条件写"市场出现本应触发交易的方向"—— 实际 |move| 多数 < 0.3%，
    连 1% 的 hit 阈值都没到，等于在编造漏检。

验收：
  1) 漏检行写入 missed_signal_reflections，trade_reflections_v2 不再新增；
  2) hit=0 时文案是"未达阈值，仅记账"，不再声称漏检；
  3) 幂等：同一 decision 反复扫描只有一行。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import config
import db
import missed_signal_reflection as msr


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    path = str(tmp_path / "v482_template_split.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    yield conn
    conn.close()


def _decision(conn, did, *, asset="ETH", action="HOLD", move=None):
    ts = int(time.time()) - 3600
    news_id = conn.execute(
        """INSERT INTO raw_news (source, content, timestamp, published_at)
           VALUES ('test', 'v482 missed', datetime('now'), '')"""
    ).lastrowid
    conn.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, event_strength, event_phase,
            evidence_action, missed_signal, ts, created_at)
           VALUES (?, ?, 0.3, ?, '', ?, 0.4, 'medium', 'mid', 'HOLD', 1, ?,
                   datetime('now','localtime'))""",
        (did, news_id, action, asset, ts),
    )
    conn.commit()
    return {
        "id": did,
        "target_asset": asset,
        "suggested_action": action,
        "missed_signal": 1,
        "ts": ts,
        "created_at": db._time_str() if hasattr(db, "_time_str") else "",
        "confidence_at_time": 0.4,
        "sentiment_score": 0.3,
        "forward_move_pct": move,
    }


def test_missed_signal_writes_to_own_table(file_db):
    conn = file_db
    dec = _decision(conn, 2001, move=0.1)   # 未达 1% 阈值
    rid = msr._upsert_one(conn, dec, 0, int(time.time()))
    assert rid is not None

    msr_rows = conn.execute(
        "SELECT * FROM missed_signal_reflections WHERE decision_id=2001").fetchall()
    assert len(msr_rows) == 1
    row = dict(msr_rows[0])
    assert row["hit"] == 0
    assert row["dimension"] in ("A", "B", "C")
    assert "未达" in row["lesson"], "未达阈值时不能声称'本应触发交易'"
    assert "本应触发交易" not in row["lesson"]
    assert not row["lesson"].startswith("漏检信号")

    # 反思表不能再被漏检行污染
    n = conn.execute(
        "SELECT COUNT(*) FROM trade_reflections_v2 WHERE decision_id=2001").fetchone()[0]
    assert n == 0


def test_hit_branch_still_says_missed(file_db):
    conn = file_db
    dec = _decision(conn, 2002, move=2.0)   # 超过 1% 阈值，真漏检
    assert msr._upsert_one(conn, dec, 0, int(time.time())) is not None
    row = dict(conn.execute(
        "SELECT * FROM missed_signal_reflections WHERE decision_id=2002").fetchone())
    assert row["hit"] == 1
    assert "本应触发交易" in row["lesson"]


def test_upsert_is_idempotent(file_db):
    conn = file_db
    dec = _decision(conn, 2003, move=0.1)
    msr._upsert_one(conn, dec, 0, int(time.time()))
    msr._upsert_one(conn, dec, 0, int(time.time()))
    n = conn.execute(
        "SELECT COUNT(*) FROM missed_signal_reflections WHERE decision_id=2003"
    ).fetchone()[0]
    assert n == 1
