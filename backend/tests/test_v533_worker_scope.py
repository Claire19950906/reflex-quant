# -*- coding: utf-8 -*-
"""worker 判定护栏第二批（标的范围 / 占位噪声 / 重试门槛），全离线。

这批函数守着两件很贵的事：
  · 一条"NONE"到底是模型偷懒还是新闻天生没标的 —— 判错就白烧 3-5 次模型调用；
  · 写进库的字段必须能绑进 sqlite —— 判错就是整批 8 条回滚、队列反复打转。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from engine import worker_helpers as w  # noqa: E402


# ── 占位/模板噪声 ─────────────────────────────────────────────────────────

def test_looks_placeholder_noise_words():
    for s in ("", "  ", "N/A", "none", "TBD", "TODO", "other", "HOLD", "BUY"):
        assert w._looks_placeholder(s) is True, s
    assert w._looks_placeholder("因素名") is True          # LLM 没替换的模板词
    assert w._looks_placeholder("服务端兜底文案") is True
    assert w._looks_placeholder("美联储加息预期升温") is False


# ── 落库前的标量归一 ─────────────────────────────────────────────────────

def test_db_safe_normalizes_containers():
    out = w._db_safe(["降息", "风险偏好"])
    assert isinstance(out, str) and json.loads(out) == ["降息", "风险偏好"]
    assert "降息" in out                                   # 中文不能被转义成 \u
    assert json.loads(w._db_safe({"a": 1})) == {"a": 1}
    assert json.loads(w._db_safe(("x", 1))) == ["x", 1]
    assert w._db_safe(True) == 1 and isinstance(w._db_safe(True), int)
    for v in (None, 0, 1.5, "text", b"raw"):
        assert w._db_safe(v) == v


def test_db_safe_unknown_object_falls_back_to_str():
    class X:
        def __str__(self):
            return "obj"
    assert w._db_safe(X()) == "obj"


# ── 标的是否没锁住 ───────────────────────────────────────────────────────

def test_is_none_target_only_exact_markers():
    for v in (None, "", " none ", "OTHER", "other"):
        assert w.is_none_target(v) is True, v
    assert w.is_none_target("BTC") is False
    assert w.is_none_target("NONE2") is False              # 不许前缀松判


# ── 六段严格格式 ─────────────────────────────────────────────────────────

def test_six_stage_strict_requires_order():
    path = "".join(f"[{s}] …内容… " for s in w._SIX_STAGES)
    assert w._six_stage_strict(path) is True
    assert w._six_stage_strict("") is False
    assert w._six_stage_strict(path.replace(f"[{w._SIX_STAGES[1]}]", "", 1)) is False
    rev = "".join(f"[{s}] " for s in reversed(w._SIX_STAGES))
    assert w._six_stage_strict(rev) is False                # 顺序反了不算严格格式


# ── 别名独立词匹配 ───────────────────────────────────────────────────────

def test_alias_word_match_ascii_needs_boundary():
    assert w._alias_word_match("coinbase", "COINBASE 财报", "COINBASE 财报") is True
    assert w._alias_word_match("coin", "COINBASE 财报", "COINBASE 财报") is False
    assert w._alias_word_match("xau", "XAUUSD 走强", "XAUUSD 走强") is False
    assert w._alias_word_match("xau", "XAU 走强", "XAU 走强") is True
    assert w._alias_word_match("", "XAU", "XAU") is False


def test_alias_word_match_cjk_substring():
    assert w._alias_word_match("黄金", "黄金价格新高", "黄金价格新高") is True
    assert w._alias_word_match("黄金", "白银上涨", "白银上涨") is False


def test_segment_mentions_asset_via_alias_and_code():
    assert w._segment_mentions_asset("金价再创新高", "XAU") is True
    assert w._segment_mentions_asset("XAUD 走强", "XAU") is False      # 代码要全词（别名字段里没有别名）
    assert w._segment_mentions_asset("NVDA 财报", "NVDA") is True
    assert w._segment_mentions_asset("", "XAU") is False
    assert w._segment_mentions_asset("金价", "") is False


def test_asset_in_core_scope():
    assert w.asset_in_core_scope("btc") is True
    assert w.asset_in_core_scope(" XAU ") is True
    assert w.asset_in_core_scope("AAPL") is False
    assert w.asset_in_core_scope("") is False


# ── 新闻里有没有池内标的 ─────────────────────────────────────────────────

def test_news_mentions_pool_asset_empty_is_conservative():
    assert w.news_mentions_pool_asset("") is True          # 空文本不敢断言"没有"
    assert w.news_mentions_pool_asset("   ") is True
    assert w.news_mentions_pool_asset("BTC 突破前高") is True
    assert w.news_mentions_pool_asset("美联储宣布加息 25bp") is False


# ── "天生非核心"快路径 ───────────────────────────────────────────────────

def _non_core_result(**kw):
    base = {"target_asset": "NONE", "market_category": "MACRO",
            "reasoning_path": "宏观层面的推演：" + "该新闻只谈整体利率路径，不指向任何单一品种。" * 6}
    base.update(kw)
    return base


def test_declares_non_core_happy_path():
    assert w.declares_non_core_news(_non_core_result(), "美联储官员讲话，未提及具体资产") is True


def test_declares_non_core_rejects_weak_evidence():
    assert w.declares_non_core_news(_non_core_result(target_asset="BTC"), "") is False
    assert w.declares_non_core_news(_non_core_result(market_category="黄金"), "") is False
    assert w.declares_non_core_news(_non_core_result(reasoning_path="没推演"), "美联储") is False
    assert w.declares_non_core_news(_non_core_result(), "BTC 大涨") is False


def test_specific_asset_category():
    assert w._is_specific_asset_category("MACRO") is False
    assert w._is_specific_asset_category("") is False
    assert w._is_specific_asset_category("黄金") is True
    assert w._is_specific_asset_category("美股科技") is True


# ── schema 重试门槛 ─────────────────────────────────────────────────────

def test_should_schema_retry_fast_channel_always():
    assert w.should_schema_retry(False, []) is False
    assert w.should_schema_retry(False, ["summary"]) is True
    assert w.should_schema_retry(False, None) is False


def test_should_schema_retry_slow_channel_only_when_fatal():
    assert w.should_schema_retry(True, ["summary"]) is False          # 慢通道不为小缺口多烧一次
    assert w.should_schema_retry(True, ["target_asset"]) is True      # 没标的 = 废数据
    assert w.should_schema_retry(True, ["a", "b", "c"]) is True       # 缺口 >= 3
    assert w.should_schema_retry(True, ["a", "b"]) is False
    assert w.should_schema_retry(True, ["target_asset"], allow_slow_retry=False) is False
