# -*- coding: utf-8 -*-
"""realtime_filter._check_news_age + evaluate_news 的时效闸门单测。

v181 L0 时效检查：避免 12h 之前的老新闻被当信号输出。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import realtime_filter as rt


def _iso(dt: datetime) -> str:
    """返回 SQLite 默认 timestamp 格式 'YYYY-MM-DD HH:MM:SS'。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_published_at_fresh_passes():
    # 1 小时前 → 应放行
    pub = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    r = rt._check_news_age(pub)
    assert r["stale"] is False
    assert r["source"] == "published_at"
    assert r["threshold_h"] == 12  # 默认 NEWS_MAX_AGE_HOURS


def test_published_at_just_inside_threshold_passes():
    # 边界附近：11.99h → 应放行（stale = age > threshold）
    pub = _iso(datetime.now(timezone.utc) - timedelta(hours=11, minutes=59))
    r = rt._check_news_age(pub)
    assert r["stale"] is False
    # 阈值刚好 12.0h：超过 → 排除
    pub_over = _iso(datetime.now(timezone.utc) - timedelta(hours=12, minutes=1))
    r_over = rt._check_news_age(pub_over)
    assert r_over["stale"] is True


def test_published_at_over_threshold_excluded():
    # 13h 前 → 应排除
    pub = _iso(datetime.now(timezone.utc) - timedelta(hours=13))
    r = rt._check_news_age(pub)
    assert r["stale"] is True
    assert r["threshold_h"] == 12


def test_missing_published_at_uses_ingested_ts_fallback():
    # 没有 published_at → 用 ingested_ts，2h 阈值
    ing_ts = int(time.time()) - 3600  # 1h 前入库
    r = rt._check_news_age("", ingested_ts=ing_ts)
    assert r["stale"] is False
    assert r["source"] == "ingested_ts"
    assert r["threshold_h"] == 2


def test_missing_published_at_old_ingest_excluded():
    # ingested_ts 5h 前 → 应被排除（fallback 阈值 2h）
    ing_ts = int(time.time()) - 5 * 3600
    r = rt._check_news_age("", ingested_ts=ing_ts)
    assert r["stale"] is True
    assert r["source"] == "ingested_ts"
    assert r["threshold_h"] == 2


def test_both_missing_passes():
    # 两路都没拿到 → 放行（避免误杀合法新闻）
    r = rt._check_news_age("", ingested_ts=None)
    assert r["stale"] is False
    assert r["source"] == "none"


def test_garbage_published_at_falls_back():
    # published_at 解析失败 → 用 ingested_ts fallback
    r = rt._check_news_age("not a date", ingested_ts=int(time.time()) - 3600)
    assert r["source"] == "ingested_ts"
    assert r["stale"] is False


def test_iso8601_with_t_separator():
    # ISO-8601 with T separator should also work
    pub = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    r = rt._check_news_age(pub)
    assert r["source"] == "published_at"
    assert r["stale"] is False


def test_iso8601_with_z_suffix():
    # ISO-8601 with Z suffix should also work
    pub = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = rt._check_news_age(pub)
    assert r["source"] == "published_at"
    assert r["stale"] is False