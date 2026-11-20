"""repository/news.py 的契约测试。

为什么这类测试重要：repository 的目标是「SQL 只存在一处、行为与原 handler 逐字一致」。
所以断言的不是"能查出来"，而是**与原始 SQL 的结果完全相等**——
键集合、空值处理都要一致，否则搬到一半就会悄悄改行为。

只读：所有连接都是 mode=ro，可在生产库上安全运行。

注意：tests/conftest.py 会把 TRIDENT_DB_PATH 指向隔离测试库（用于常规单测）。
本文件是"对真实库的契约校验"，因此显式指向仓库内的真实库，并在用例内临时
覆盖环境变量；真实库不存在时整体 skip，不让 CI 在无库环境里红。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REAL_DB = BACKEND / "trident_event_bus.db"
sys.path.insert(0, str(BACKEND / "src_python"))

# 模块级 skip：本文件在 import 期就会读真实库构造参数化用例，
# 所以 skipif 标记来不及生效（CI 无库环境会直接 ERROR 而不是 skip）。
if not REAL_DB.exists():
    pytest.skip("真实库不存在（CI 无库环境），跳过契约校验", allow_module_level=True)

from repository import news as repo_news  # noqa: E402

pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason="真实库不存在（CI 无库环境），跳过契约校验",
)

_ORIGINAL_SQL = """
SELECT ad.structured_input, ad.structured_check,
       ad.event_type_code, ad.snapshot_proximity,
       ad.reasoning, ad.id AS decision_id, ad.created_at
FROM raw_news rn
LEFT JOIN ai_decisions ad ON ad.id = (
    SELECT MAX(x.id) FROM ai_decisions x
    WHERE x.news_id = rn.id AND (x.is_sibling = 0 OR x.is_sibling IS NULL)
)
WHERE rn.id = ?
"""


def _real_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(autouse=True)
def _point_repo_at_real_db(monkeypatch):
    """repository 每次调用都会重读路径，这里把环境变量临时指回真实库。"""
    monkeypatch.setenv("TRIDENT_DB_PATH", str(REAL_DB))


def _news_ids(limit: int = 3) -> list[int]:
    conn = _real_ro()
    try:
        rows = conn.execute(
            "SELECT id FROM raw_news WHERE status='DONE' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [int(r["id"]) for r in rows]
    finally:
        conn.close()


@pytest.mark.parametrize("news_id", _news_ids())
def test_same_result_as_original_sql(news_id: int):
    expected = None
    conn = _real_ro()
    try:
        row = conn.execute(_ORIGINAL_SQL, (int(news_id),)).fetchone()
        expected = dict(row) if row is not None else None
    finally:
        conn.close()

    got = repo_news.get_news_with_latest_decision(int(news_id))
    assert (got is None) == (expected is None)
    if expected is not None:
        assert set(got.keys()) == set(expected.keys())
        for k in expected:
            assert got[k] == expected[k], f"字段 {k} 不一致"


def test_missing_news_returns_none():
    assert repo_news.get_news_with_latest_decision(2_000_000_000) is None


def test_count_by_status_matches_raw_sql():
    expected = {}
    conn = _real_ro()
    try:
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM raw_news GROUP BY status"):
            expected[str(r["status"])] = int(r["n"])
    finally:
        conn.close()
    assert repo_news.count_by_status() == expected
