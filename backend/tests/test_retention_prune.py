# -*- coding: utf-8 -*-
"""retention_prune 保留策略回归测试（P1-5）。

覆盖：
  * 各时间类型 cutoff 格式（int / int_ms / text / text_iso）；
  * 追加型表清单只认显式登记的表，时间列配置齐全；
  * text_iso 的 cutoff 必须与 ISO 8601 文本（带 T）同格式，保证字符串比较不错位。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE_DIR, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import retention_prune as rp  # noqa: E402


def test_cutoff_int_is_epoch_seconds():
    c = rp._cutoff("int", 30)
    assert isinstance(c, int)
    now = int(datetime.now().timestamp())
    assert now - 30 * 86400 - 5 <= c <= now


def test_cutoff_int_ms_is_epoch_millis():
    c = rp._cutoff("int_ms", 180)
    assert isinstance(c, int)
    assert c > 1_000_000_000_000


def test_cutoff_text_uses_space_separator():
    c = rp._cutoff("text", 14)
    assert " " in c and "T" not in c
    datetime.strptime(c, "%Y-%m-%d %H:%M:%S")


def test_cutoff_text_iso_uses_t_separator():
    c = rp._cutoff("text_iso", 30)
    assert "T" in c
    datetime.strptime(c, "%Y-%m-%dT%H:%M:%S")


def test_text_iso_cutoff_matches_iso_string_order():
    """同口径下 ISO 文本与 cutoff 字符串比较方向必须正确。"""
    days = 30
    c = rp._cutoff("text_iso", days)
    old = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert old < c < fresh


def test_registered_tables_have_required_fields():
    for table, cfg in rp.TABLES.items():
        assert cfg["ts_kind"] in ("int", "int_ms", "text", "text_iso"), table
        assert cfg["days"] > 0, table
        assert cfg.get("why"), table


def test_crowd_source_items_registered_with_iso_column():
    cfg = rp.TABLES["crowd_source_items"]
    assert cfg["ts_kind"] == "text_iso"
    assert cfg["ts_col"] == "collected_at"
    assert cfg["days"] == 30
