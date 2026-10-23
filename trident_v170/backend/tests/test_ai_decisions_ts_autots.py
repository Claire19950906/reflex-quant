"""v303+ P0+【回归测试】ai_decisions.ts 自动回填 + INSERT 触发器兜底.

历史 BUG：v302+ 加了 ai_decisions.ts 列 (无 default), 但 ai_worker.py 的
INSERT 漏写 → 全 NULL → /api/events/today live_signal_count 永远 0.
本测试覆盖三层防护:
  1) 历史数据回填: _backfill_ai_decisions_ts() 把 ts=NULL 的存量行从
     created_at 解析成 UTC 秒.
  2) AFTER INSERT 触发器: 即使 INSERT 漏写 ts, 数据库层也会兜底.
  3) 已显式写入的 INSERT 不被触发器覆盖: WHEN NEW.ts IS NULL 保护.
"""
import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db  # noqa: E402


def _make_minimal_ai_decisions(conn: sqlite3.Connection) -> None:
    """建一张最小 ai_decisions 表, 足够 INSERT 跑通 + 触发器工作."""
    conn.execute(
        """
        CREATE TABLE ai_decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id         INTEGER NOT NULL,
            sentiment_score REAL    NOT NULL,
            suggested_action TEXT   NOT NULL,
            reasoning       TEXT    NOT NULL,
            created_at      TEXT    NOT NULL,
            target_asset    TEXT    NOT NULL DEFAULT 'NONE',
            ts              INTEGER
        )
        """
    )
    conn.execute(_db._AI_DECISIONS_AUTOTS_TRIGGER_DDL)


def test_backfill_parses_19char_beijing_created_at_to_utc_seconds():
    """19 字符 ISO 串 (含 '+08:00') → strftime 认偏移, 给出正确 UTC 秒."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _make_minimal_ai_decisions(conn)
        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, "
            "reasoning, created_at, target_asset) VALUES "
            "(1, 0.7, 'BUY', 'r', '2026-10-13T07:30:00+08:00', 'XAU')"
        )
        conn.execute("UPDATE ai_decisions SET ts=NULL WHERE id=1")

        n = _db._backfill_ai_decisions_ts(conn)
        assert n == 1, f"应回填 1 行, 实际 {n}"

        ts = conn.execute("SELECT ts FROM ai_decisions WHERE id=1").fetchone()[0]
        # 07:30:00 Beijing = 23:30:00 UTC 前一天 → 1791847800
        assert ts == 1791847800, f"回填 UTC 秒错误: {ts}"

        n2 = _db._backfill_ai_decisions_ts(conn)
        assert n2 == 0, f"二次 backfill 应返回 0, 实际 {n2}"
    finally:
        conn.close()


def test_backfill_handles_short_created_at_with_offset_correction():
    """< 19 字符的早期短串 → Python datetime 解析 (按 Beijing +08:00 解读)."""
    conn = sqlite3.connect(":memory:")
    try:
        _make_minimal_ai_decisions(conn)
        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, "
            "reasoning, created_at, target_asset) VALUES "
            "(1, 0.5, 'HOLD', 'r', '2026-10-13 07:30:00', 'XAU')"
        )
        conn.execute("UPDATE ai_decisions SET ts=NULL WHERE id=1")

        n = _db._backfill_ai_decisions_ts(conn)
        assert n == 1

        ts = conn.execute("SELECT ts FROM ai_decisions WHERE id=1").fetchone()[0]
        # Beijing 2026-10-13 07:30:00 = UTC 2026-10-12 23:30:00 = 1791819000
        # 路径 2 走 Python datetime.astimezone(UTC), 不依赖 SQLite strftime tz 行为.
        from datetime import datetime, timezone, timedelta
        expected = int(datetime(2026, 10, 13, 7, 30, 0,
                                tzinfo=timezone(timedelta(hours=8))
                                ).astimezone(timezone.utc).timestamp())
        assert ts == expected, f"短串回填 UTC 秒错误: got {ts} expected {expected}"
    finally:
        conn.close()


def test_trigger_auto_fills_ts_when_insert_omits_it():
    """INSERT 漏写 ts → AFTER INSERT 触发器兜底从 created_at 解析."""
    conn = sqlite3.connect(":memory:")
    try:
        _make_minimal_ai_decisions(conn)

        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, "
            "reasoning, created_at, target_asset) VALUES "
            "(1, 0.7, 'BUY', 'r', '2026-10-13T07:30:00+08:00', 'XAU')"
        )

        ts = conn.execute("SELECT ts FROM ai_decisions WHERE id=1").fetchone()[0]
        assert ts == 1791847800, f"触发器应填 1791847800, 实际 {ts}"
    finally:
        conn.close()


def test_trigger_does_not_overwrite_explicit_ts():
    """INSERT 显式带 ts → WHEN NEW.ts IS NULL 不触发, 保留应用代码值."""
    conn = sqlite3.connect(":memory:")
    try:
        _make_minimal_ai_decisions(conn)

        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, "
            "reasoning, created_at, target_asset, ts) VALUES "
            "(1, 0.7, 'BUY', 'r', '2026-10-13T07:30:00+08:00', 'XAU', 12345)"
        )

        ts = conn.execute("SELECT ts FROM ai_decisions WHERE id=1").fetchone()[0]
        assert ts == 12345, f"显式 ts 应被保留为 12345, 实际 {ts}"
    finally:
        conn.close()


def test_migrate_creates_trigger_idempotently():
    """_ensure_ai_decisions_autots_trigger() 二次调用不报错, 仍只 1 个 trigger."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _make_minimal_ai_decisions(conn)
        _db._ensure_ai_decisions_autots_trigger(conn)
        _db._ensure_ai_decisions_autots_trigger(conn)
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name='trg_ai_decisions_autots'"
        ).fetchone()[0]
        assert n == 1, f"trigger 应只 1 个, 实际 {n}"
    finally:
        conn.close()
