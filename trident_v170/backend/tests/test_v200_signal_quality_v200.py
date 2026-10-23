#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v200+ Q6: signal_quality_v200 8 类信号质量细分规则测试。

每条正例 + 反例都覆盖,确保:
  1) 触发条件命中时正确分类到 (analysis, signal_quality_v200, <signal_name>)
  2) 触发条件不全时回退到兜底 (unknown 或其它规则)
  3) 新规则在 SUB_LAYERS / SIGNAL_NAMES 注册一致(is_valid 通过)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))


def _assert_quality(out, signal_name, hint_substr=None):
    assert out["layer"] == "analysis", f"layer={out['layer']}"
    assert out["sub_layer"] == "signal_quality_v200", f"sub_layer={out['sub_layer']}"
    assert out["signal_name"] == signal_name, f"signal_name={out['signal_name']}"
    if hint_substr:
        assert hint_substr in out["root_cause_hint"], \
            f"hint={out['root_cause_hint']!r} 缺 {hint_substr!r}"


def test_signal_quality_v200_sub_layer_registered():
    """sub_layer 注册检查"""
    import reflection_ontology as ro
    assert "signal_quality_v200" in ro.SUB_LAYERS["analysis"]
    print("  PASS  signal_quality_v200 registered in SUB_LAYERS")


def test_signal_quality_v200_signal_names_registered():
    """8 个 signal_name 注册 + 模板完整性"""
    import reflection_ontology_signals as ros
    sig = ros.SIGNAL_NAMES["analysis"]["signal_quality_v200"]
    expected = {
        "magnitude_overestimated", "magnitude_underestimated",
        "asset_category_misclassified", "asset_class_misclassified",
        "dirty_news_replay", "dirty_news_correction",
        "dirty_news_merged", "dirty_news_partial",
    }
    for s in expected:
        assert s in sig, f"{s} missing"
        assert "root_cause" in sig[s]
        assert "lesson" in sig[s]
        assert "verification" in sig[s]
    print(f"  PASS  8 signal_names registered with templates")


def test_is_valid_signal_quality_v200():
    """is_valid 三元组校验"""
    import reflection_ontology as ro
    sigs = ["magnitude_overestimated", "magnitude_underestimated",
            "asset_category_misclassified", "asset_class_misclassified",
            "dirty_news_replay", "dirty_news_correction",
            "dirty_news_merged", "dirty_news_partial"]
    for s in sigs:
        assert ro.is_valid("analysis", "signal_quality_v200", s), \
            f"is_valid failed for {s}"
    print(f"  PASS  is_valid 8 signals all true")


def test_rule_23_magnitude_overestimated():
    """23: impact_score >= 70 + BUY/SELL + LOSS → magnitude_overestimated"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
         "impact_score": 85, "target_asset": "BTC",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X [因素定位] Y [驱动力] Z"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "magnitude_overestimated", "高估")
    print(f"  PASS  rule 23 magnitude_overestimated")


def test_rule_23_negative_high_impact_win():
    """23 反例: impact_score >= 70 + WIN → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "is_correct": "WIN", "forward_pnl": 0.020,
         "impact_score": 85, "target_asset": "BTC",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "magnitude_overestimated"
    print(f"  PASS  rule 23 negative: high-impact WIN not triggered -> {out['signal_name']}")

def test_rule_24_magnitude_underestimated():
    """24: impact_score <= 30 + HOLD + WIN → magnitude_underestimated"""
    import reflection_ontology as ro
    d = {"suggested_action": "HOLD", "is_correct": "WIN", "forward_pnl": 0.012,
         "impact_score": 20, "target_asset": "ETH",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "magnitude_underestimated", "低估")
    print(f"  PASS  rule 24 magnitude_underestimated")


def test_rule_24_negative_low_impact_loss():
    """24 反例: impact_score <= 30 + LOSS → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "HOLD", "is_correct": "LOSS", "forward_pnl": -0.005,
         "impact_score": 20, "target_asset": "ETH",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "magnitude_underestimated"
    print(f"  PASS  rule 24 negative: low-impact LOSS not triggered -> {out['signal_name']}")


def test_rule_25_asset_category_misclassified_macro_stock():
    """25a: target_asset 像是股票 + market_category=MACRO → 错配"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "AAPL", "market_category": "MACRO",
         "is_correct": "LOSS", "forward_pnl": -0.008,
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "asset_category_misclassified", "AAPL")
    print(f"  PASS  rule 25a asset_category_misclassified (AAPL/MACRO)")


def test_rule_25_asset_category_misclassified_stock_macro():
    """25b: market_category=STOCK + target_asset 是外汇 → 错配"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "USD", "market_category": "STOCK",
         "is_correct": "LOSS", "forward_pnl": -0.008,
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "asset_category_misclassified", "USD")
    print(f"  PASS  rule 25b asset_category_misclassified (STOCK/USD)")


def test_rule_25_negative_consistent():
    """25 反例: BTC/CRYPTO 一致 → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "BTC", "market_category": "CRYPTO",
         "is_correct": "LOSS", "forward_pnl": -0.008,
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "asset_category_misclassified"
    print(f"  PASS  rule 25 negative: BTC/CRYPTO consistent -> {out['signal_name']}")


def test_rule_26_asset_class_misclassified():
    """26: gate_kind=energy + asset_nature=policy → 子类错配"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "OIL", "gate_kind": "energy",
         "asset_nature": "policy", "is_correct": "LOSS", "forward_pnl": -0.008,
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "asset_class_misclassified", "energy")
    print(f"  PASS  rule 26 asset_class_misclassified (energy vs policy)")


def test_rule_26_negative_consistent():
    """26 反例: energy/commodity 一致 → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "OIL", "gate_kind": "energy",
         "asset_nature": "commodity", "is_correct": "LOSS", "forward_pnl": -0.008,
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
def test_rule_27_dirty_news_replay():
    """27: content_fingerprint 重复 + first_seen_ts > 1h 前 → dirty_news_replay"""
    import reflection_ontology as ro
    from datetime import datetime, timezone, timedelta
    _fst = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008, "content_fingerprint": "abc123def4567890",
         "first_seen_ts": _fst, "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "dirty_news_replay", "abc123def4567890")
    print(f"  PASS  rule 27 dirty_news_replay (24h old fingerprint)")


def test_rule_27_negative_fresh_news():
    """27 反例: first_seen_ts 10min 内 → 不命中"""
    import reflection_ontology as ro
    from datetime import datetime, timezone, timedelta
    _fst = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008, "content_fingerprint": "abc123def4567890",
         "first_seen_ts": _fst, "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "dirty_news_replay"
    print(f"  PASS  rule 27 negative: fresh news not triggered -> {out['signal_name']}")


def test_rule_28_dirty_news_correction():
    """28: 标题含"辟谣" → dirty_news_correction"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008, "title": "官方辟谣: BTC ETF 消息不实",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "dirty_news_correction", "澄清")
    print(f"  PASS  rule 28 dirty_news_correction (辟谣 in title)")


def test_rule_28_negative_no_correction_keyword():
    """28 反例: 普通新闻 → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008, "title": "BTC ETF 获 SEC 批准",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "dirty_news_correction"
    print(f"  PASS  rule 28 negative: regular news not triggered -> {out['signal_name']}")


def test_rule_29_dirty_news_merged():
    """29: is_sibling=1 + parent_event_type 不同 → dirty_news_merged"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "ETH", "is_correct": "LOSS",
         "forward_pnl": -0.008, "is_sibling": 1,
         "event_type_code": "rate_hike", "parent_event_type": "etf_approval",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "dirty_news_merged", "rate_hike")
    print(f"  PASS  rule 29 dirty_news_merged (sibling but diff event_type)")


def test_rule_29_negative_same_event_type():
    """29 反例: sibling 同 event_type → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "ETH", "is_correct": "LOSS",
         "forward_pnl": -0.008, "is_sibling": 1,
         "event_type_code": "etf_approval", "parent_event_type": "etf_approval",
         "paper_trading_run_id": 1, "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "dirty_news_merged"
    print(f"  PASS  rule 29 negative: same event_type not triggered -> {out['signal_name']}")


def test_rule_30_dirty_news_partial():
    """30: news_content < 50 字 + hard_data_evidence 空 → dirty_news_partial"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008, "news_content": "BTC 跌",
         "hard_data_evidence": "", "paper_trading_run_id": 1,
         "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    _assert_quality(out, "dirty_news_partial", "5")
    print(f"  PASS  rule 30 dirty_news_partial (5-char news no hard_data)")


def test_rule_30_negative_full_news():
    """30 反例: 完整新闻 + hard_data 有内容 → 不命中"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "BTC", "is_correct": "LOSS",
         "forward_pnl": -0.008,
         "news_content": "BTC ETF 获 SEC 批准, 交易量激增, 涨幅 5%",
         "hard_data_evidence": "2025-01-15 BTC close=42k vol=10B",
         "paper_trading_run_id": 1,
         "reasoning": "[总框架] X"}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "dirty_news_partial"
    print(f"  PASS  rule 30 negative: full news not triggered -> {out['signal_name']}")


def test_priority_other_rules_still_work():
    """新规则不应破坏已有规则的命中优先级(E018 仍然先命中)"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "XAU", "is_correct": "LOSS",
         "event_type_code": "fomc_pivot", "paper_trading_run_id": 1, "reasoning": "[总框架] X",
         "baseline": {"by_type": {"fomc_pivot": {"sample_n": 5,
                                                 "win_rate": 0.20,
                                                 "avg_pnl_4h": -0.012}}},
         "impact_score": 50, "market_category": "MACRO",
         "content_fingerprint": "old123", "first_seen_ts": "2025-01-01 00:00:00"}
    out = ro.infer_layer_sub_signal(d)
    assert out["sub_layer"] == "counter_consensus", \
        f"expected counter_consensus got {out['sub_layer']}/{out['signal_name']}"
    print(f"  PASS  priority: E018 still beats new rules")


def test_safe_fallback_no_new_fields():
    """旧 d 字段缺新字段时(没 JOIN raw_news),安全回退"""
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012}
    out = ro.infer_layer_sub_signal(d)
    assert out["layer"] == "analysis"
    print(f"  PASS  safe fallback no new fields -> {out['sub_layer']}/{out['signal_name']}")


if __name__ == "__main__":
    print("\n=== v200+ Q6 signal_quality_v200 8 类信号质量细分测试 ===\n")
    test_signal_quality_v200_sub_layer_registered()
    test_signal_quality_v200_signal_names_registered()
    test_is_valid_signal_quality_v200()
    test_rule_23_magnitude_overestimated()
    test_rule_23_negative_high_impact_win()
    test_rule_24_magnitude_underestimated()
    test_rule_24_negative_low_impact_loss()
    test_rule_25_asset_category_misclassified_macro_stock()
    test_rule_25_asset_category_misclassified_stock_macro()
    test_rule_25_negative_consistent()
    test_rule_26_asset_class_misclassified()
    test_rule_26_negative_consistent()
    test_rule_27_dirty_news_replay()
    test_rule_27_negative_fresh_news()
    test_rule_28_dirty_news_correction()
    test_rule_28_negative_no_correction_keyword()
    test_rule_29_dirty_news_merged()
    test_rule_29_negative_same_event_type()
    test_rule_30_dirty_news_partial()
    test_rule_30_negative_full_news()
    test_priority_other_rules_still_work()
    test_safe_fallback_no_new_fields()
    print("\n  ✅ All 22 tests passed\n")

