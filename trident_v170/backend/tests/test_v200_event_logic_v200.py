#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v200+ Q6: event_logic 6 类推理链完整性规则测试 (rules 31-36).

每条规则覆盖正例 + 反例 + 字段缺失 fallback + is_valid + 模板完整性.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))


def _assert_event_logic(out, sub_layer, signal_name, hint_substr=None):
    assert out["layer"] == "event_logic", f"layer={out['layer']}, expected event_logic"
    assert out["sub_layer"] == sub_layer, f"sub_layer={out['sub_layer']}, expected {sub_layer}"
    assert out["signal_name"] == signal_name, f"signal_name={out['signal_name']}, expected {signal_name}"
    if hint_substr:
        assert hint_substr in out["root_cause_hint"], \
            f"hint={out['root_cause_hint']!r} 缺 {hint_substr!r}"


_FULL_8 = ("[总框架] X [因素定位] Y [驱动力] Z [水位博弈] W [跨资产] V "
           "[反共识] U [逻辑自检] T [总结] S")


def test_event_logic_sub_layers_registered():
    import reflection_ontology as ro
    expected = ["causal_chain", "concept_match", "counter_consensus",
                "cross_asset_chain", "sentiment_driver_chain",
                "macro_state_chain", "summary_chain"]
    for sub in expected:
        assert sub in ro.SUB_LAYERS["event_logic"], f"{sub} missing"
    print(f"  PASS  7 event_logic sub_layers registered")


def test_event_logic_signal_names_registered():
    import reflection_ontology_signals as ros
    sigs = ros.SIGNAL_NAMES["event_logic"]
    expected = {
        "causal_chain": ["reasoning_chain_broken"],
        "concept_match": ["concept_misaligned"],
        "cross_asset_chain": ["cross_asset_unverified"],
        "sentiment_driver_chain": ["sentiment_driver_misaligned"],
        "macro_state_chain": ["macro_state_misread"],
        "summary_chain": ["summary_chain_broken"],
    }
    for sub, names in expected.items():
        assert sub in sigs, f"{sub} missing"
        for n in names:
            assert n in sigs[sub], f"{sub}/{n} missing"
            assert "root_cause" in sigs[sub][n]
            assert "lesson" in sigs[sub][n]
            assert "verification" in sigs[sub][n]
    print(f"  PASS  6 new signals registered with full templates")


def test_is_valid_event_logic_v200():
    import reflection_ontology as ro
    pairs = [
        ("event_logic", "causal_chain", "reasoning_chain_broken"),
        ("event_logic", "concept_match", "concept_misaligned"),
        ("event_logic", "cross_asset_chain", "cross_asset_unverified"),
        ("event_logic", "sentiment_driver_chain", "sentiment_driver_misaligned"),
        ("event_logic", "macro_state_chain", "macro_state_misread"),
        ("event_logic", "summary_chain", "summary_chain_broken"),
    ]
    for layer, sub, sig in pairs:
        assert ro.is_valid(layer, sub, sig), f"is_valid failed: {layer}/{sub}/{sig}"
    print(f"  PASS  is_valid 6 new event_logic signals all true")


def test_rule_31_reasoning_chain_broken():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] 涨 [驱动力] 牛市 [总结] 好",
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "causal_chain", "reasoning_chain_broken", "八段链不完整")
    print(f"  PASS  rule 31 reasoning_chain_broken")


def test_rule_31_negative_full_chain():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "reasoning_chain_broken", \
        f"complete 8-chain should not trigger rule 31, got {out['signal_name']}"
    print(f"  PASS  rule 31 negative: 8-marker chain -> {out['signal_name']}")


def test_rule_31_negative_win():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "WIN", "forward_pnl": 0.020,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] X [驱动力] Y [总结] Z",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "reasoning_chain_broken"
    print(f"  PASS  rule 31 negative: WIN -> {out['signal_name']}")


def test_rule_32_concept_misaligned():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "factor_ranking": '["random_idea_a", "random_idea_b"]',
        "asset_knowledge": {"core_factors": [{"factor": "liquidity"}],
                            "condition_factors": [{"factor": "regulation"}]},
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "concept_match", "concept_misaligned", "全无重叠")
    print(f"  PASS  rule 32 concept_misaligned")


def test_rule_32_negative_overlap():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "factor_ranking": '["liquidity factor", "another"]',
        "asset_knowledge": {"core_factors": [{"factor": "liquidity"}],
                            "condition_factors": []},
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "concept_misaligned", \
        f"overlap should not trigger rule 32, got {out['signal_name']}"
    print(f"  PASS  rule 32 negative: overlap -> {out['signal_name']}")


def test_rule_32_fallback_empty_kg():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "factor_ranking": '["random_idea_a"]',
        "asset_knowledge": {"core_factors": [], "condition_factors": []},
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "concept_misaligned"
    print(f"  PASS  rule 32 fallback: empty KG -> {out['signal_name']}")


def test_rule_33_cross_asset_unverified():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] X [因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "cross_asset_chain", "cross_asset_unverified", "跨资产传导")
    print(f"  PASS  rule 33 cross_asset_unverified")


def test_rule_33_negative_has_keywords():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] X [因素定位] Y [驱动力] Z [跨资产] UST 跌 BTC 跌 反向共识 [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "cross_asset_unverified"
    print(f"  PASS  rule 33 negative: has cross-asset keyword -> {out['signal_name']}")


def test_rule_33_negative_stock_target():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "AAPL", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] X [因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "cross_asset_unverified"
    print(f"  PASS  rule 33 negative: AAPL (stock) -> {out['signal_name']}")


def test_rule_34_sentiment_driver_misaligned():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "sentiment_score": 0.7,
        "factor_ranking": '["liquidity squeeze", "recession risk"]',
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "sentiment_driver_chain", "sentiment_driver_misaligned",
                        "情绪与驱动因子")
    print(f"  PASS  rule 34 sentiment_driver_misaligned")


def test_rule_34_negative_no_negative_keys():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "sentiment_score": 0.7,
        "factor_ranking": '["growth", "expansion"]',
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "sentiment_driver_misaligned"
    print(f"  PASS  rule 34 negative: positive factor -> {out['signal_name']}")


def test_rule_34_negative_low_sentiment():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "sentiment_score": 0.2,
        "factor_ranking": '["liquidity squeeze"]',
        "reasoning_path": _FULL_8,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "sentiment_driver_misaligned"
    print(f"  PASS  rule 34 negative: low sentiment -> {out['signal_name']}")


def test_rule_35_macro_state_misread():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "regime_at_decision": "risk_off",
        "reasoning_path": "[因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "macro_state_chain", "macro_state_misread", "宏观状态未触达")
    print(f"  PASS  rule 35 macro_state_misread")


def test_rule_35_negative_mentions_regime():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "AAPL", "paper_trading_run_id": 1,
        "regime_at_decision": "risk_off",
        "reasoning_path": "[总框架] 逆势 BUY [因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "macro_state_misread"
    print(f"  PASS  rule 35 negative: mentions 逆势 -> {out['signal_name']}")


def test_rule_35_negative_bullish_regime():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "AAPL", "paper_trading_run_id": 1,
        "regime_at_decision": "risk_on",
        "reasoning_path": "[因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "macro_state_misread"
    print(f"  PASS  rule 35 negative: bullish regime -> {out['signal_name']}")


def test_rule_36_summary_chain_broken():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] 框架 [因素定位] 流动性紧缩 节点 BTC [驱动力] 牛市 [跨资产] W "
                          "[反共识] U [逻辑自检] T [总结] ABCDEF",
    }
    out = ro.infer_layer_sub_signal(d)
    _assert_event_logic(out, "summary_chain", "summary_chain_broken", "总结与因果链脱节")
    print(f"  PASS  rule 36 summary_chain_broken")


def test_rule_36_negative_summary_cites_anchor():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "reasoning_path": "[总框架] 框架 [因素定位] 流动性紧缩 BTC 驱动力 [驱动力] 牛市 "
                          "[跨资产] W [反共识] U [逻辑自检] T [总结] 流动性紧缩 牛市 ABC",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "summary_chain_broken"
    print(f"  PASS  rule 36 negative: summary cites anchor -> {out['signal_name']}")


def test_priority_macro_state_beats_cross_asset():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
        "regime_at_decision": "risk_off",
        "reasoning_path": "[因素定位] Y [驱动力] Z [水位博弈] W [反共识] U [逻辑自检] T [总结] S",
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] == "macro_state_misread", \
        f"macro_state should fire first, got {out['signal_name']}"
    print(f"  PASS  priority: macro_state beats cross_asset")


def test_priority_e018_still_wins():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "event_type_code": "etf",
        "paper_trading_run_id": 1,
        # summary must cite [因素定位] anchor so rule 36 doesn't fire
        "reasoning_path": "[总框架] X [因素定位] 紧缩 预期 [驱动力] 牛市 传导 [跨资产] W "
                          "[反共识] U [逻辑自检] T [总结] 紧缩 预期 牛市 传导 OK",
        "baseline": {"by_type": {"etf": {"sample_n": 5, "win_rate": 0.2, "avg_pnl_4h": -0.01}}},
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] == "historical_counterfactual", \
        f"E018 should beat rules 31-36, got {out['signal_name']}"
    print(f"  PASS  priority: E018 still beats new event_logic rules")


def test_safe_fallback_all_fields_missing():
    import reflection_ontology as ro
    d = {
        "suggested_action": "BUY", "is_correct": "LOSS", "forward_pnl": -0.012,
        "target_asset": "BTC", "paper_trading_run_id": 1,
    }
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "reasoning_chain_broken"
    assert out["signal_name"] != "concept_misaligned"
    assert out["signal_name"] != "cross_asset_unverified"
    assert out["signal_name"] != "sentiment_driver_misaligned"
    assert out["signal_name"] != "macro_state_misread"
    assert out["signal_name"] != "summary_chain_broken"
    print(f"  PASS  safe fallback: all event_logic fields missing -> {out['signal_name']}")


if __name__ == "__main__":
    print("=== v200+ event_logic 6 规则测试 ===\n")
    test_event_logic_sub_layers_registered()
    test_event_logic_signal_names_registered()
    test_is_valid_event_logic_v200()
    test_rule_31_reasoning_chain_broken()
    test_rule_31_negative_full_chain()
    test_rule_31_negative_win()
    test_rule_32_concept_misaligned()
    test_rule_32_negative_overlap()
    test_rule_32_fallback_empty_kg()
    test_rule_33_cross_asset_unverified()
    test_rule_33_negative_has_keywords()
    test_rule_33_negative_stock_target()
    test_rule_34_sentiment_driver_misaligned()
    test_rule_34_negative_no_negative_keys()
    test_rule_34_negative_low_sentiment()
    test_rule_35_macro_state_misread()
    test_rule_35_negative_mentions_regime()
    test_rule_35_negative_bullish_regime()
    test_rule_36_summary_chain_broken()
    test_rule_36_negative_summary_cites_anchor()
    test_priority_macro_state_beats_cross_asset()
    test_priority_e018_still_wins()
    test_safe_fallback_all_fields_missing()
    print("\n✅ All 22 tests passed")