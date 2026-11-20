"""v332: 两张表用不同代码表示同一标的 → 归因直配永远打不中。

实测: 异动侧资产 `SKHYNIX`（82 条异动，窗口内 45 条有真新闻），
新闻侧标签是韩国交易所原始代码 `000660` / `000660.KS`（74 条已打标新闻）。
两边字符串永不相等 → 只能靠标题弱匹配 (semantic)，而 v321.5 又默认把弱匹配
挡在主表之外 —— 结果 SKHYNIX 归因 **0 条**，直接拖垮 #11「按资产覆盖 ≥10」。

修法只做代码归一（不做新启发式），并补上中文标题缺的方向词。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import calc_event_move_attribution as cem
from cem_constants import normalize_asset_tag


# ── 代码归一 ────────────────────────────────────────────────────────────────
def test_normalize_korean_ticker():
    assert normalize_asset_tag("000660") == "SKHYNIX"
    assert normalize_asset_tag("000660.KS") == "SKHYNIX"
    assert normalize_asset_tag("000660.ks") == "SKHYNIX"


def test_normalize_leaves_platform_codes_alone():
    assert normalize_asset_tag("BTC") == "BTC"
    assert normalize_asset_tag("skhynix") == "SKHYNIX"   # 大小写归一


def test_normalize_drops_bucket_tags():
    """INDIVIDUAL_STOCK / US_STOCK / 个股 是"资产桶"不是标的, 归一为空。"""
    for tag in ("INDIVIDUAL_STOCK", "US_STOCK", "个股"):
        assert normalize_asset_tag(tag) == ""


# ── 直配判定 ────────────────────────────────────────────────────────────────
def test_direct_match_via_ticker_alias():
    s = cem.build_news_asset_set("000660,US_STOCK", "000660.KS")
    assert cem.asset_direct_match("SKHYNIX", s) is True


def test_direct_match_does_not_steal_other_asset():
    s = cem.build_news_asset_set("000660", "")
    assert cem.asset_direct_match("MU", s) is False


def test_bucket_tag_is_not_a_direct_match():
    """桶标签不能替任何具体标的"刷"一个直配。"""
    s = cem.build_news_asset_set("INDIVIDUAL_STOCK", "")
    assert cem.asset_direct_match("MU", s) is False
    assert cem.asset_direct_match("NVDA", s) is False


def test_bucket_stays_in_set_for_cross_macro_gating():
    """桶标签必须留在集合里 —— 否则"有标签的新闻"会被误当成"无标签宏观新闻"。"""
    s = cem.build_news_asset_set("US_STOCK", "")
    assert "US_STOCK" in s


def test_main_asset_column_is_authoritative():
    s = cem.build_news_asset_set("", "BTC")
    assert cem.asset_direct_match("BTC", s) is True
    s2 = cem.build_news_asset_set("", "NONE")
    assert s2 == set()


# ── 标题方向词 (方向矛盾闸) ──────────────────────────────────────────────────
def test_chinese_momentum_words_are_recognised():
    assert cem._headline_direction("部分存储概念股走强") == 1
    assert cem._headline_direction("SK海力士涨8.6%") == 1
    assert cem._headline_direction("美股集体高开") == 1
    assert cem._headline_direction("芯片股走弱") == -1
    assert cem._headline_direction("盘前下挫") == -1


def test_direction_contradiction_is_blocked():
    """标题说涨、异动是跌 → 这不是原因, 必须拦掉。"""
    assert cem._direction_blocked("SK海力士涨8.6%", -7.08) is True
    assert cem._direction_blocked("芯片股走弱", +5.0) is True


def test_mixed_direction_still_passes():
    """涨跌同现的标题读不出方向 → 不判, 避免误杀真实跨资产事件。"""
    assert cem._headline_direction("涨跌互现") == 0
    assert cem._headline_direction("纳指跌、芯片涨") == 0
    assert cem._direction_blocked("纳指跌、芯片涨", -2.0) is False


def test_same_direction_passes():
    assert cem._direction_blocked("比特币大涨", +3.0) is False
