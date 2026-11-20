# -*- coding: utf-8 -*-
"""v341: 品种命名归一 —— 同一标的不许在库里出现两个名字。

实测：ADA 2480 行 / ADA-USD 9 行，同一 ts 两边都有，只是量源不同
（kline_bars vs yfinance），差 0.5%~2%。权重/命中率/归因按名字分组，
等于同一枚币被拆成两份样本。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import asset_alias as aa


@pytest.mark.parametrize("name,expect", [
    ("ADA-USD", "ADA"),
    ("ada-usd", "ADA"),      # 大小写混写
    ("  BNB-USD ", "BNB"),   # 带空格
    ("GC=F", "XAU"),
    ("CL=F", "WTI"),
    ("NVIDIA", "NVDA"),
    ("BTC", "BTC"),          # 规范名幂等
])
def test_canonical_maps_alias(name, expect):
    assert aa.canonical(name) == expect


def test_canonical_is_idempotent():
    for n in ("ADA-USD", "GC=F", "NVIDIA", "TSLA"):
        c = aa.canonical(n)
        assert aa.canonical(c) == c


@pytest.mark.parametrize("name", ["AMZN", "META", "NG", "IXIC", "SPX", None, ""])
def test_unknown_names_are_left_alone(name):
    """不认识的名字**原样返回**，绝不猜 —— 猜错就是伪造数据。"""
    if name is None or name == "":
        assert aa.canonical(name) == ""
    else:
        assert aa.canonical(name) == name


def test_aliases_of_roundtrip():
    for a in aa.aliases_of("ADA"):
        assert aa.canonical(a) == "ADA"
    assert "ADA-USD" in aa.aliases_of("ADA")


def test_is_alias_detects_only_real_aliases():
    assert aa.is_alias("ADA-USD") is True
    assert aa.is_alias("ADA") is False
    assert aa.is_alias("AMZN") is False


def test_all_canonical_dedupes():
    got = aa.all_canonical(["ADA", "ADA-USD", "ada", "BTC", "BTC-USD"])
    assert got == {"ADA", "BTC"}


def test_unknown_lists_only_unmapped():
    assert "AMZN" in aa.unknown(["ADA", "ADA-USD", "AMZN"])
    assert "ADA" not in aa.unknown(["ADA", "ADA-USD"])
