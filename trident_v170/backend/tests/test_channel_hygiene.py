"""通道层脏数据修复回归测试（P1/P2/history_review 时区口径）。

覆盖本轮落地：
  · P1 stock_news 来源标签按 provider 区分（Finnhub/Yahoo），不再硬编码 Yahoo Finance
  · P2 stock_news / reuters 的 id 用稳定 sha256 内容哈希（内置 hash() 受 PYTHONHASHSEED
    影响跨进程变化，会让去重 marker 失效导致重复入库）
  · history_review 回看窗口不再用 strftime('%s','now','localtime')（实测 +8h 偏移，
    导致回看窗口错位 / 小窗口空结果）
"""

import os
import sys
import hashlib

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import stock_news
import reuters_news


@pytest.fixture(autouse=True)
def _no_translate(monkeypatch):
    # to_ingest_items 内部会调 translate_title_sync（网络翻译）；隔离成原文透传，
    # 让来源标签 / id 生成的纯逻辑可离线断言。
    monkeypatch.setattr(stock_news, "translate_title_sync", lambda title: title)
    monkeypatch.setattr(reuters_news, "translate_title_sync", lambda title: title)


def _sha12(title: str) -> str:
    return hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]


def test_stock_news_summary_source_by_provider():
    # Finnhub 数据不再被标成 Yahoo Finance
    finn = stock_news.to_ingest_items([{
        "title": "Nvidia beats earnings estimates",
        "ticker": "NVDA",
        "publish_ts": 1737000000,
        "publisher": "Reuters",
        "provider": "finnhub",
    }])[0]
    assert "来源:Finnhub" in finn["summary"]
    assert "Reuters" in finn["summary"]
    assert "Yahoo Finance" not in finn["summary"]

    # 无 provider（Yahoo 兜底）→ Yahoo Finance
    yah = stock_news.to_ingest_items([{
        "title": "Apple unveils new chip",
        "ticker": "AAPL",
        "publish_ts": 1737000000,
        "publisher": "Yahoo Finance",
    }])[0]
    assert "来源:Yahoo Finance" in yah["summary"]
    # publisher 与来源名一致时不重复拼接出 "来源:Yahoo Finance (Yahoo Finance)"
    assert "来源:Yahoo Finance (Yahoo Finance)" not in yah["summary"]


def test_stock_news_id_is_stable_hash():
    title = "Nvidia beats earnings estimates"
    items = [
        stock_news.to_ingest_items([{
            "title": title, "ticker": "NVDA", "publish_ts": 1737000000,
            "provider": "finnhub",
        }])[0]
        for _ in range(3)
    ]
    ids = {it["id"] for it in items}
    assert len(ids) == 1  # 同一标题多次组装 id 一致（跨进程稳定，不依赖内置 hash()）
    assert _sha12(title) in items[0]["id"]


def test_reuters_id_is_stable_hash():
    title = "Fed holds rates steady"
    items = [
        reuters_news.to_ingest_items([{
            "title": title, "news_ts": 1737000000,
            "published_at": "2025-01-16 10:00:00",
        }])[0]
        for _ in range(3)
    ]
    ids = {it["id"] for it in items}
    assert len(ids) == 1
    assert _sha12(title) in items[0]["id"]


def test_history_review_window_sql_no_localtime_epoch():
    # 回看窗口用 strftime('%s','now')（正确 UTC epoch），不得再用 'localtime'
    # （服务器 +0800 时实测会 +28800 秒，导致窗口错位）。
    path = os.path.join(SRC, "history_review.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "strftime('%s','now','localtime')" not in src
    assert "strftime('%s','now') - ?*3600" in src