# -*- coding: utf-8 -*-
"""v342 — 门卫假冗余自检（gate_pool_report）

现场事实：MINIMAX_GATE_KEY / MINIMAX_MAIN_KEY / MINIMAX_API_KEY 是同一个 key，
所以 config 里的"gate 专用 + gate 备用"只建成 1 条通道，与主分析共用配额。
这个自检必须在启动时把这件事喊出来，而不是等人从 429 日志里猜。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from review.gate_pool_health import gate_pool_report


def _ch(alias, key):
    return {"id": alias, "key_alias": alias, "api_key": key, "purpose": ["gate"]}


def test_single_channel_is_flagged():
    rep = gate_pool_report([_ch("minimax-gate", "sk-aaa")])
    assert rep["channels"] == 1
    assert rep["distinct_keys"] == 1
    assert rep["fake_redundancy"] is True
    assert "共用配额" in rep["warning"]


def test_two_channels_same_key_is_fake_redundancy():
    rep = gate_pool_report([_ch("gate", "sk-aaa"), _ch("gate2", "sk-aaa")])
    assert rep["channels"] == 2
    assert rep["distinct_keys"] == 1
    assert rep["fake_redundancy"] is True


def test_two_channels_two_keys_is_healthy():
    rep = gate_pool_report([_ch("gate", "sk-aaa"), _ch("gate-alt", "sk-bbb")])
    assert rep["channels"] == 2
    assert rep["distinct_keys"] == 2
    assert rep["fake_redundancy"] is False
    assert rep["warning"] is None


def test_non_gate_channels_ignored():
    rep = gate_pool_report([
        {"id": "main", "api_key": "sk-aaa", "purpose": ["main"]},
        _ch("gate", "sk-aaa"),
    ])
    assert rep["channels"] == 1
