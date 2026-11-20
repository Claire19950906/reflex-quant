# -*- coding: utf-8 -*-
"""v409 — 新闻 → 真实代码（tableView 级单测）。

四个必须守住的边界：
1. 显式写法能解析：$NVDA、NASDAQ: NVDA、600519.SH、（0700.HK）；
2. 免费语里的裸六位数**不认**（"营收 600519 万元"不能被当成 A 股代码）；
3. 桶标签在新闻无代码时保持原样，并给出可读原因；
4. 已经是真实代码时不改写（幂等、不引入抖动）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import ticker_resolve as tr


def test_explicit_forms_resolve():
    assert tr.resolve_from_text("英伟达 $NVDA 盘后大涨")[0] == "NVDA"
    assert tr.resolve_from_text("(NASDAQ: NVDA) 财报超预期")[0] == "NVDA"
    assert tr.resolve_from_text("贵州茅台 600519.SH 公告")[0] == "600519.SH"
    assert tr.resolve_from_text("腾讯（0700.HK）回购")[0] == "00700.HK"


def test_bare_six_digits_not_resolved():
    assert tr.resolve_from_text("公司营收 600519 万元，同比增长")[0] is None
    assert tr.resolve_from_text("2026 年 11 月 17 日 披露")[0] is None


def test_placeholder_without_code_keeps_asset():
    asset, how, why = tr.maybe_resolve("CN_STOCK", "某A股上市公司发布业绩预告")
    assert asset == "CN_STOCK"
    assert how == ""
    assert "没有显式代码" in why


def test_placeholder_with_code_rewrites():
    asset, how, why = tr.maybe_resolve("CN_STOCK", "贵州茅台（600519.SH）提价")
    assert asset == "600519.SH"
    assert how == "suffix"


def test_real_ticker_untouched():
    assert tr.maybe_resolve("NVDA", "英伟达 $NVDA")[0] == "NVDA"
    assert tr.maybe_resolve("XAU", "黄金 $GLD")[0] == "XAU"
