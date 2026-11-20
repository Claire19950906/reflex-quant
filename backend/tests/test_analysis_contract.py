# -*- coding: utf-8 -*-
"""analysis/ 包回归：五终态派生 / HOLD 枚举 / 范围路由 / 证据就绪。

这些用例全部离线、纯函数，不碰数据库、不发请求。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import pytest

from analysis import contract as ac
from analysis import readiness as ar
from analysis import route as art
from analysis.confidence import coverage_from_hard_status as ac_cov


# ── contract.scope_of ────────────────────────────────────────────────────

@pytest.mark.parametrize("asset,expected", [
    ("BTC", "core"),
    ("btc", "core"),
    ("XAU", "core"),
    ("BRENT", "adjacent"),
    ("DXY", "adjacent"),
    ("CN_STOCK", "stock"),
    ("个股", "stock"),
    ("NONE", "none"),
    ("", "none"),
    ("SKHYNIX", "unknown"),
])
def test_scope_of(asset, expected):
    assert ac.scope_of(asset) == expected


# ── contract.derive_state ────────────────────────────────────────────────

def test_state_complete_when_not_degraded():
    assert ac.derive_state(degraded=0, reason="", asset="XAU",
                           hard_status="ok") == "complete"


@pytest.mark.parametrize("asset,reason", [
    ("NONE", "target_none"),
    ("CN_STOCK", "target_none"),
    ("NVDA", "target_none"),
    ("US_STOCK", "llm_empty"),      # 越界优先于「模型失败」
])
def test_state_out_of_scope_beats_model_failure(asset, reason):
    assert ac.derive_state(degraded=1, reason=reason, asset=asset,
                           hard_status="missing:no_local_feed") == "out_of_scope"


@pytest.mark.parametrize("reason,expected", [
    ("llm_empty", "llm_failed"),
    ("server_fallback", "llm_failed"),
    ("summary_only", "compressed"),   # 有摘要=有方向，属可用低质量，不算失败
    ("target_none", "parse_failed"),   # 核心品种拿不到目标 = 输出契约失败
])
def test_state_model_side_failures_on_core_asset(reason, expected):
    assert ac.derive_state(degraded=1, reason=reason, asset="XAU",
                           hard_status="missing:feed_stale") == expected


def test_state_data_unavailable_when_feed_missing():
    assert ac.derive_state(degraded=1, reason="", asset="XAU",
                           hard_status="missing:feed_stale") == "data_unavailable"
    assert ac.derive_state(degraded=1, reason="", asset="WTI",
                           hard_status="missing:no_local_feed") == "data_unavailable"


def test_state_out_of_scope_covers_no_target_and_feedless_symbols():
    # 越界不等于「模型失败」：没标的、个股代码、桶标签都归这一档
    assert ac.derive_state(degraded=1, reason="target_none", asset="NONE",
                           hard_status="missing:none_target") == "out_of_scope"
    assert ac.derive_state(degraded=1, reason="target_none", asset="NVDA",
                           hard_status="missing:unknown_symbol") == "out_of_scope"
    assert ac.derive_state(degraded=1, reason="llm_empty", asset="CN_STOCK",
                           hard_status="missing:no_local_feed") == "out_of_scope"


def test_state_unknown_reason_falls_back_to_parse_failed():
    assert ac.derive_state(degraded=1, reason="something_new", asset="BTC",
                           hard_status="ok") == "parse_failed"


def test_state_is_always_in_enum():
    cases = [
        dict(degraded=0, reason="", asset="BTC", hard_status="ok"),
        dict(degraded=1, reason="target_none", asset="NONE", hard_status="missing:none_target"),
        dict(degraded=1, reason="llm_empty", asset="SOL", hard_status="missing:feed_stale"),
        dict(degraded=1, reason="summary_only", asset="WTI", hard_status="ok"),
        dict(degraded=1, reason="", asset="UNKNOWNX", hard_status="missing:unknown_symbol"),
    ]
    for kw in cases:
        assert ac.derive_state(**kw) in ac.ANALYSIS_STATES


# ── contract.derive_no_trade_reason ──────────────────────────────────────

def test_no_reason_for_directional_action():
    assert ac.derive_no_trade_reason(action="BUY", state="complete") == ""
    assert ac.derive_no_trade_reason(action="sell", state="complete") == ""


@pytest.mark.parametrize("state", ["out_of_scope", "data_unavailable",
                                   "llm_failed", "parse_failed"])
def test_reason_mirrors_state(state):
    assert ac.derive_no_trade_reason(action="HOLD", state=state) == state


def test_reason_gate_blocked():
    assert ac.derive_no_trade_reason(
        action="HOLD", state="complete",
        gate_reason="统一置信度 0.0 低于闸门 15.0，仅输出观望结论") == "gate_blocked"
    assert ac.derive_no_trade_reason(
        action="HOLD", state="complete",
        gate_reason="无可证据（coverage=0，置信度不适用）：target_none") == "gate_blocked"


def test_reason_ai_neutral_is_not_gate_blocked():
    assert ac.derive_no_trade_reason(
        action="HOLD", state="complete",
        gate_reason="AI 主动观望 (evidence=42.0，方向=HOLD)") == "direction_neutral"


def test_reason_stale_news_wins_over_gate():
    assert ac.derive_no_trade_reason(
        action="HOLD", state="complete", gate_reason="低于闸门，未建仓",
        news_age_min=90) == "stale_news"


def test_reason_default_neutral():
    assert ac.derive_no_trade_reason(action="HOLD", state="complete") == "direction_neutral"


def test_enum_values_are_closed():
    assert set(ac.NO_TRADE_REASONS) == {
        "out_of_scope", "data_unavailable", "llm_failed", "parse_failed",
        "compressed", "stale_news", "gate_blocked", "direction_neutral",
    }
    assert set(ac.ANALYSIS_STATES) == {
        "complete", "compressed", "out_of_scope", "data_unavailable",
        "llm_failed", "parse_failed",
    }


def test_validators_reject_unknown():
    with pytest.raises(ValueError):
        ac.validate_state("degraded")
    with pytest.raises(ValueError):
        ac.validate_no_trade_reason("no_idea")
    assert ac.validate_no_trade_reason("") == ""


def test_length_budget_boundary():
    assert not ac.is_over_budget("x" * ac.REASONING_CHAR_BUDGET)
    assert ac.is_over_budget("x" * (ac.REASONING_CHAR_BUDGET + 1))


def test_ai_worker_writes_no_trade_reason():
    # 落库契约：闸门 UPDATE 必须带 no_trade_reason，否则 reflection_ontology 的死分支
    # 会一直是死的（v400 接入点，见 engine/ai_worker.py 闸门 UPDATE）。
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src_python" / "engine" / "ai_worker.py").read_text(encoding="utf-8")
    assert "no_trade_reason=?" in src
    assert "_ntr_reason" in src
    assert "derive_no_trade_reason" in src


# ── route.route_news ─────────────────────────────────────────────────────

def test_route_from_gate_core():
    r = art.route_news(gate_main_asset="XAU")
    assert (r.scope, r.target, r.reason) == ("core", "XAU", "gate_core")
    assert r.should_analyze


def test_route_from_gate_adjacent():
    r = art.route_news(gate_main_asset="DXY")
    assert r.scope == "adjacent"
    assert r.should_analyze


@pytest.mark.parametrize("bucket", ["CN_STOCK", "US_STOCK", "ETF", "个股"])
def test_route_bucket_is_out_of_scope(bucket):
    r = art.route_news(gate_main_asset=bucket)
    assert r.scope == "out_of_scope"
    assert not r.should_analyze


def test_route_gate_assets_list():
    r = art.route_news(gate_main_asset="", gate_assets="NVDA,XAU")
    assert r.scope == "core" and r.target == "XAU"


def test_route_gate_assets_pipe_separator():
    r = art.route_news(gate_main_asset="", gate_assets="US10Y|WTI")
    assert r.scope == "core" and r.target == "WTI"


def test_route_text_alias():
    r = art.route_news(gate_main_asset="", news_text="美国非农数据超预期，黄金短线拉升 1.2%")
    assert r.scope == "core" and r.target == "XAU"


def test_route_btc_etf_not_misread_as_stock():
    # 「ETF」是股票类特征词，但正文里有明确品种别名 → 仍按核心处理
    r = art.route_news(gate_main_asset="", news_text="比特币现货 ETF 获批，BTC 突破前高")
    assert r.scope == "core" and r.target == "BTC"


def test_route_stock_hints_without_alias():
    r = art.route_news(gate_main_asset="",
                       news_text="某芯片公司财报净利润超预期，股价盘后涨 12%")
    assert r.scope == "out_of_scope"


def test_route_category_stock():
    r = art.route_news(gate_main_asset="", news_text="某行业新闻", category="STOCK")
    assert r.scope == "out_of_scope"


def test_route_gate_alias_gold_is_core():
    # 门卫常写 GOLD / OIL 这类叫法 —— 必须归一到 XAU / WTI，否则核心新闻掉成 unknown
    assert art.route_news(gate_main_asset="GOLD").target == "XAU"
    assert art.route_news(gate_main_asset="OIL").target == "WTI"
    assert art.route_news(gate_main_asset="GOLD").scope == "core"


def test_route_gate_assets_json_string():
    # 门卫把 gate_assets 写成 JSON 串（实测 50 条），不解析就会整条误判成桶
    r = art.route_news(gate_main_asset="个股",
                       gate_assets='["个股", "原油", "黄金"]')
    assert r.scope == "core" and r.target in ("WTI", "XAU")


def test_route_gate_class_word_is_core():
    r = art.route_news(gate_main_asset="", gate_assets='["加密主流"]')
    assert r.scope == "core" and r.reason == "gate_class_word"


def test_route_bucket_without_core_assets_still_out():
    r = art.route_news(gate_main_asset="个股", gate_assets='["个股", "板块"]')
    assert r.scope == "out_of_scope"


def test_route_core_beats_adjacent_in_gate_assets():
    # 两遍扫描：先扫核心再扫邻近，不能因为先遇到邻近就把核心新闻降级
    r = art.route_news(gate_main_asset="", gate_assets="DXY,XAU")
    assert (r.scope, r.target) == ("core", "XAU")


def test_route_nothing_known_is_conservative():
    r = art.route_news(gate_main_asset="", news_text="央行召开会议，讨论明年工作安排", category="")
    # v453：补了宏观/地缘别名（央行/美联储/战争/制裁…），这类传导源归 adjacent 而非 unknown；
    # 不变量只剩「判不准也要进分析，不许误判成 out_of_scope」。
    assert r.scope in ("unknown", "adjacent")
    assert r.should_analyze          # 判不准就进分析，不误杀


# ── readiness.check ──────────────────────────────────────────────────────

def test_ready_core_fresh():
    rd = ar.check(asset="BTC", snapshot_age_s=120, has_local_symbol=True)
    assert rd.ready and rd.code == "ok" and rd.hard_status == "ok"


def test_stale_core():
    rd = ar.check(asset="BTC", snapshot_age_s=700, has_local_symbol=True)
    assert not rd.ready
    assert rd.code == "missing:feed_stale"


def test_adjacent_has_longer_window():
    assert ar.check(asset="DXY", snapshot_age_s=800, has_local_symbol=True).ready
    assert not ar.check(asset="DXY", snapshot_age_s=1000, has_local_symbol=True).ready


def test_no_target():
    rd = ar.check(asset="NONE", snapshot_age_s=10)
    assert rd.code == "missing:none_target"


def test_stock_bucket_has_no_feed():
    rd = ar.check(asset="CN_STOCK", snapshot_age_s=10)
    assert rd.code == "missing:no_local_feed"


def test_missing_snapshot_and_negative_age():
    assert ar.check(asset="XAU", snapshot_age_s=None).code == "missing:feed_stale"
    assert ar.check(asset="XAU", snapshot_age_s=-5).code == "missing:feed_stale"


def test_kline_gap():
    rd = ar.check(asset="WTI", snapshot_age_s=60, kline_ok=False)
    assert rd.code == "missing:no_kline"


def test_explicit_max_age_override():
    assert not ar.check(asset="XAU", snapshot_age_s=200, max_age_s=180).ready


# ── confidence.rank（分档选择器）──────────────────────────────────────────

from analysis import confidence as cf


def test_rank_zero_score_is_hold():
    v = cf.rank(score=0.0, coverage=1.0, structure=1.0)
    assert v.tier == cf.TIER_D and not v.has_direction


def test_rank_degraded_is_hold_even_with_score():
    v = cf.rank(score=0.5, coverage=1.0, structure=0.3, degraded=True)
    assert v.tier == cf.TIER_D


def test_rank_compressed_keeps_direction():
    # 关键行为：压缩输出（只有摘要/骨架）不再一律 HOLD，降档但不丢方向
    v = cf.rank(score=0.16, coverage=0.6, structure=0.3, compressed=True)
    assert v.tier == cf.TIER_C and v.has_direction


def test_rank_strong_signal_tradable():
    v = cf.rank(score=0.45, coverage=0.9, structure=0.9, consistency=1.0)
    assert v.tier == cf.TIER_A and v.tradable


def test_rank_unknown_market_confirmation_caps_to_shadow():
    # 99.2% 的 market_confirmation=unknown → coverage 0.6 → 按默认阈值进不了 A
    v = cf.rank(score=0.45, coverage=cf.coverage_from_hard_status("ok", "unknown"),
                structure=0.9, consistency=1.0)
    assert v.tier == cf.TIER_B
    assert not v.tradable


def test_rank_confidence_stays_ordered():
    weak = cf.rank(score=0.1, coverage=0.5, structure=0.5).confidence
    strong = cf.rank(score=0.6, coverage=0.9, structure=0.9).confidence
    assert weak < strong <= 100


def test_coverage_mapping():
    assert cf.coverage_from_hard_status("ok", "confirmed") == 1.0
    assert cf.coverage_from_hard_status("ok", "unknown") == 0.6
    assert cf.coverage_from_hard_status("missing:none_target") == 0.0


def test_humanize_and_classify_roundtrip():
    """枚举 ↔ 中文：写库用枚举，读侧翻中文，不能出现第三种口径。"""
    assert ac.humanize_no_trade_reason("gate_blocked") == "闸门拦截"
    assert ac.humanize_no_trade_reason("新闻已发布 87 分钟") == "新闻已发布 87 分钟"
    assert ac.humanize_no_trade_reason("") == ""
    # 历史自由文本要能归位，否则新旧数据没法合并统计
    assert ac.classify_no_trade_reason("新闻已发布 87 分钟，超过时效门槛 45 分钟") == "stale_news"
    assert ac.classify_no_trade_reason("资产 WTI 不在模拟盘允许交易列表") == "gate_blocked"
    assert ac.classify_no_trade_reason("gate_blocked") == "gate_blocked"
    assert ac.classify_no_trade_reason("完全看不懂的一句话") == ""   # 认不出就说认不出


def test_ontology_understands_enum_reasons():
    """复盘维度必须认得运行时写的枚举，否则 430 条降级行的归因永远空转。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))
    from review import reflection_ontology as ro  # noqa: WPS433

    base = {"suggested_action": "HOLD", "target_asset": "BTC",
            "reasoning": "模型判中性，等待更好点位", "sentiment_score": 0.0}
    cases = {
        "gate_blocked": "below_min_score",
        "stale_news": "event_too_old",
        "out_of_scope": "no_match_should_exclude",
        "compressed": "summary_too_short",
        "data_unavailable": "source_degraded",
    }
    for reason, expected in cases.items():
        got = ro.infer_layer_sub_signal(dict(base, no_trade_reason=reason))
        assert got["signal_name"] == expected, f"{reason} → {got}"


def test_coverage_proxy_is_opt_in():
    """没有代理值时必须和旧行为一致；代理足够强才抬到 0.75（且不到 1.0）。"""
    assert ac_cov("ok", "unknown") == 0.6
    assert ac_cov("ok", "unknown", None) == 0.6
    assert ac_cov("ok", "unknown", 0.05) == 0.6      # 动量不足，不算确认
    assert ac_cov("ok", "unknown", 0.30) == 0.75     # 本地可算的强确认
    assert ac_cov("ok", "positive") == 1.0           # 模型真给了确认就是满分
    assert ac_cov("missing:none_target", "unknown", 0.30) == 0.0
