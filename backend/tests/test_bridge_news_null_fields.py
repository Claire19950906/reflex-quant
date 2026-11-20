#!/usr/bin/env python3
"""新闻影响评估对"列不齐"的新闻行要能兜住（2026-11-18）。

背景（线上实测）：`GET /api/news/impact-evaluate?news_id=1` 一直 500，
根因是 `_news_impact_evaluate` 里 `f"[{row['source']}] {row['headline'][:80]}"`——
`headline` 为 NULL 时对 None 取切片直接 TypeError。

外部写入的新闻（飞书/telegram 转发、第三方接口）本来就可能只带标题或只带正文，
所以这里锁两件事：
1. headline / source / ts 为 NULL 时不抛异常，返回 ok=True；
2. 评估结果里 headline 是 None 而不是崩掉。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

bridge_builders = pytest.importorskip("bridge_builders")
bridge_news = pytest.importorskip("bridge_news")


@pytest.fixture()
def null_headline_db(tmp_path, monkeypatch):
    """临时库：一行 headline/source/ts 全 NULL 的新闻。"""
    db = tmp_path / "news_null.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, news_id TEXT, source TEXT, "
        "headline TEXT, body TEXT, score REAL, assets TEXT, asset TEXT, action TEXT, ts INTEGER)"
    )
    conn.execute(
        "INSERT INTO news_events (id, news_id, source, headline, body, score, assets, asset, action, ts) "
        "VALUES (1, 'n1', NULL, NULL, '只有正文没有标题', NULL, NULL, NULL, NULL, NULL)"
    )
    conn.execute(
        "CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER, "
        "move_pct REAL, move_kind TEXT, algo_score REAL, duration_min INTEGER)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(bridge_builders, "DB_PATH", db)
    return db


def test_null_headline_does_not_crash(null_headline_db):
    result = bridge_news._news_impact_evaluate(news_id=1)
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result.get("headline") is None


def test_missing_news_id_returns_error_not_exception(null_headline_db):
    result = bridge_news._news_impact_evaluate(news_id=999)
    assert result.get("ok") is False
    assert "不存在" in result.get("error", "")
