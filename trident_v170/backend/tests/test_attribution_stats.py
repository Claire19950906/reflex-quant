"""归因统计与持久化的纯函数 / 隔离 DB 测试。

覆盖：
  · Bug #1 修复 — _primary_code / attribution_stats 单选主因：pct 加和 ≤ 100%
  · Bug #3 修复 — _validate_reason_code 拒绝越界 code（>23 / <1 / 非整数）
  · Bug #5 修复 — 主因提取失败时归入 unattributed（code=0）
  · Bug #6 修复 — unreset_attributions：reset → pending 反向可恢复
  · Bug #7 修复 — attribution_stats 服务端 30s 缓存（命中不重算）
  · _attribution_codes 范围校验（保留向后兼容语义：只过滤无效 code）

不依赖外部服务，单测用 :memory: SQLite 隔离。
"""

import os
import sys
import sqlite3

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ──────────────────────────────────────────────────────────────────
# Fixtures：用 :memory: 隔离 DB，覆盖 db.get_connection()
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def in_memory_db(monkeypatch, tmp_path):
    """覆盖 db.get_connection 返回临时文件 SQLite 连接，建好 attribution_reviews 表。

    用临时文件而不是 :memory: 的原因：review_agent 的 attribution_stats() 在 finally
    里调 conn.close()，对 :memory: 库这会立刻销毁数据；临时文件关闭后数据依然保留。
    """
    import db  # noqa: F401  (use the real module under monkeypatch)

    db_path = tmp_path / "test_attribution.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE attribution_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'news',
            ref_id INTEGER NOT NULL DEFAULT 0,
            trade_id INTEGER,
            asset TEXT DEFAULT '',
            action TEXT DEFAULT '',
            verdict TEXT DEFAULT '',
            pnl_pct REAL,
            reason_code INTEGER,
            reason_text TEXT DEFAULT '',
            confidence REAL,
            human_status TEXT NOT NULL DEFAULT 'pending',
            corrected_code INTEGER,
            corrected_text TEXT DEFAULT '',
            core_impact TEXT DEFAULT '',
            reasons_json TEXT DEFAULT '',
            note TEXT DEFAULT '',
            rule_pushed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            reviewed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE review_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT NOT NULL,
            asset TEXT DEFAULT '',
            scope TEXT DEFAULT 'all',
            active INTEGER NOT NULL DEFAULT 1,
            source_samples INTEGER DEFAULT 0,
            origin TEXT DEFAULT 'real',
            kind INTEGER NOT NULL DEFAULT 1,
            counter_factual TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    def _get_conn():
        new = sqlite3.connect(str(db_path))
        new.row_factory = sqlite3.Row
        return new

    monkeypatch.setattr(db, "get_connection", _get_conn)

    # 重置缓存，避免上次测试的缓存污染
    from review_agent import _STATS_CACHE  # noqa: F401
    _STATS_CACHE.clear()

    yield conn
    _STATS_CACHE.clear()
    conn.close()


def _insert_review(conn, **kwargs):
    defaults = {
        "source": "news",
        "ref_id": 0,
        "asset": "BTC",
        "action": "BUY",
        "verdict": "LOSS",
        "pnl_pct": -0.05,
        "reason_code": None,
        "reason_text": "",
        "human_status": "pending",
        "corrected_code": None,
        "corrected_text": "",
        "core_impact": "",
        "reasons_json": "",
        "rule_pushed": 0,
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    cur = conn.execute(
        f"INSERT INTO attribution_reviews ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    conn.commit()
    return cur.lastrowid


# ──────────────────────────────────────────────────────────────────
# Bug #1 修复 — _primary_code / attribution_stats 单选主因
# ──────────────────────────────────────────────────────────────────


def test_primary_code_prefers_corrected_over_reasons():
    """人工纠错 corrected_code 优先于 reasons_json 主因。"""
    from review_agent import _primary_code

    assert _primary_code(
        corrected_code=10,
        reason_code=6,
        reasons_json='[{"code":7,"is_primary":true},{"code":6,"is_primary":false}]',
    ) == 10


def test_primary_code_uses_is_primary_marker():
    """reasons_json 里 is_primary=true 优先于 reason_code。"""
    from review_agent import _primary_code

    assert _primary_code(
        corrected_code=None,
        reason_code=6,
        reasons_json='[{"code":7,"is_primary":false},{"code":11,"is_primary":true}]',
    ) == 11


def test_primary_code_falls_back_to_first_when_no_primary_marker():
    """没有 is_primary 标记时取第一项。"""
    from review_agent import _primary_code

    assert _primary_code(
        corrected_code=None,
        reason_code=6,
        reasons_json='[{"code":11},{"code":7}]',
    ) == 11


def test_primary_code_falls_back_to_reason_code():
    """reasons_json 不可解析时取 reason_code。"""
    from review_agent import _primary_code

    assert _primary_code(
        corrected_code=None,
        reason_code=6,
        reasons_json="not a json",
    ) == 6


def test_primary_code_returns_none_when_all_missing():
    """全部缺失返回 None（被 stats 计入 unattributed）。"""
    from review_agent import _primary_code

    assert (
        _primary_code(
            corrected_code=None,
            reason_code=None,
            reasons_json="",
        )
        is None
    )


def test_attribution_stats_pct_sum_at_most_100(in_memory_db):
    """Bug #1: 多标签记录存在时 pct 加和应 ≤ 100%（修复前 100%+100% = 200%）。"""
    from review_agent import attribution_stats

    _insert_review(
        in_memory_db,
        reason_code=24,
        reasons_json='[{"code":24,"is_primary":true},{"code":7,"is_primary":false}]',
    )
    stats = attribution_stats()
    pct_sum = sum(r["pct"] for r in stats["reasons"])
    assert pct_sum <= 100.0 + 0.1, f"pct 加和 {pct_sum}% 超 100%"
    assert any(r["code"] == 7 for r in stats["reasons"])


# ──────────────────────────────────────────────────────────────────
# Bug #3 修复 — 范围校验：拒绝越界 code
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, None),
        (-1, None),
        (24, None),
        (100, None),
        ("abc", None),
        (None, None),
        (1, 1),
        (23, 23),
        ("7", 7),
    ],
)
def test_validate_reason_code_range(raw, expected):
    from review_agent import _validate_reason_code

    assert _validate_reason_code(raw) == expected


def test_attribution_codes_drops_out_of_range():
    from review_agent import _attribution_codes

    codes = _attribution_codes(
        corrected_code=None,
        reason_code=None,
        reasons_json='[{"code":24},{"code":7},{"code":-1},{"code":6}]',
    )
    assert codes == [7, 6]
# ──────────────────────────────────────────────────────────────────
# Bug #5 修复 — 未归因明细
# ──────────────────────────────────────────────────────────────────


def test_attribution_stats_lists_unattributed_when_codes_missing(in_memory_db):
    """主因提取失败的记录归入 unattributed = code=0 单列。"""
    from review_agent import attribution_stats

    _insert_review(in_memory_db, reason_code=24, reasons_json="garbage")
    _insert_review(in_memory_db, reason_code=6)

    stats = attribution_stats()
    codes = sorted(r["code"] for r in stats["reasons"])
    assert 0 in codes, "unattributed (code=0) 缺失"
    assert stats["unattributed"] == 1
    assert stats["total"] == 2
    assert sum(r["count"] for r in stats["reasons"]) == stats["total"]


def test_attribution_stats_no_unattr_when_all_have_codes(in_memory_db):
    """全部记录都有有效 code 时 unattr=0 且无 code=0 行。"""
    from review_agent import attribution_stats

    _insert_review(in_memory_db, reason_code=6)
    _insert_review(in_memory_db, reason_code=7)

    stats = attribution_stats()
    assert stats["unattributed"] == 0
    assert not any(r["code"] == 0 for r in stats["reasons"])


# ──────────────────────────────────────────────────────────────────
# Bug #6 修复 — unreset_attributions
# ──────────────────────────────────────────────────────────────────


def test_unreset_restores_pending_from_reset(in_memory_db):
    """Bug #6: 重置后 unreset 能恢复为 pending。"""
    from review_agent import reset_attributions, unreset_attributions

    _insert_review(in_memory_db, human_status="pending", reason_code=6)
    _insert_review(in_memory_db, human_status="reset", reason_code=7)

    reset_attributions()
    rows = in_memory_db.execute(
        "SELECT human_status, COUNT(*) AS n FROM attribution_reviews GROUP BY human_status"
    ).fetchall()
    statuses = {r["human_status"]: r["n"] for r in rows}
    assert statuses.get("reset", 0) == 2

    ur = unreset_attributions()
    assert ur["ok"] is True
    assert ur["unreset"] == 2
    rows = in_memory_db.execute(
        "SELECT human_status, COUNT(*) AS n FROM attribution_reviews GROUP BY human_status"
    ).fetchall()
    statuses = {r["human_status"]: r["n"] for r in rows}
    assert statuses.get("pending", 0) == 2
    assert statuses.get("reset", 0) == 0


def test_unreset_validates_scope(in_memory_db):
    """scope 非法时应返回 ok=False（不抛异常）。"""
    from review_agent import unreset_attributions

    res = unreset_attributions(scope="bad_scope")
    assert res["ok"] is False
    assert "scope" in res["error"]


# ──────────────────────────────────────────────────────────────────
# Bug #7 修复 — 服务端 30s 缓存
# ──────────────────────────────────────────────────────────────────


def test_attribution_stats_caches_within_ttl(in_memory_db):
    """30s 内再次调用 attribution_stats 应复用缓存（不重新查 DB）。"""
    from review_agent import attribution_stats

    _insert_review(in_memory_db, reason_code=6)
    first = attribution_stats()

    in_memory_db.execute(
        "INSERT INTO attribution_reviews (source, ref_id, reason_code, human_status) "
        "VALUES ('news', 0, 7, 'pending')"
    )
    in_memory_db.commit()
    second = attribution_stats()
    assert second == first
    assert second["total"] == 1


def test_attribution_stats_cache_invalidated_by_reset(in_memory_db):
    """reset 写路径必须失效缓存。"""
    from review_agent import attribution_stats, reset_attributions

    _insert_review(in_memory_db, reason_code=6)
    a = attribution_stats()
    assert a["total"] == 1

    reset_attributions()
    b = attribution_stats()
    assert b["total"] == 0


def test_attribution_stats_cache_invalidated_by_unreset(in_memory_db):
    """unreset 也必须失效缓存。"""
    from review_agent import attribution_stats, unreset_attributions

    _insert_review(in_memory_db, human_status="reset", reason_code=6)
    a = attribution_stats()
    assert a["total"] == 0

    unreset_attributions()
    b = attribution_stats()
    assert b["total"] == 1


def test_attribution_stats_cache_invalidated_by_feedback(in_memory_db):
    """apply_attribution_feedback 也必须失效缓存。"""
    from review_agent import attribution_stats, apply_attribution_feedback

    _insert_review(in_memory_db, reason_code=6, corrected_code=None)
    attribution_stats()  # warm cache

    rid = in_memory_db.execute(
        "SELECT id FROM attribution_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    res = apply_attribution_feedback(in_memory_db, rid, "agree", None, "", "")
    assert res["ok"] is True
    b = attribution_stats()
    assert b["total"] == 1


# ──────────────────────────────────────────────────────────────────
# 端到端：apply_attribution_feedback 也应走范围校验
# ──────────────────────────────────────────────────────────────────


def test_apply_feedback_rejects_out_of_range_corrected_code(in_memory_db):
    """人工纠错时 corrected_code 越界也应被 stats 主因过滤。"""
    from review_agent import apply_attribution_feedback, attribution_stats

    _insert_review(in_memory_db, reason_code=6, corrected_code=None)
    rid = in_memory_db.execute(
        "SELECT id FROM attribution_reviews ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    res = apply_attribution_feedback(in_memory_db, rid, "corrected", 24, "", "")
    assert res["ok"] is True
    s = attribution_stats()
    assert any(r["code"] == 6 for r in s["reasons"])
    assert not any(r["code"] == 24 for r in s["reasons"])