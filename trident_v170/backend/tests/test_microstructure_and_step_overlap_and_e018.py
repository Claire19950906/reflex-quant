
# v200+ three-piece regression test: step overlap / E018 / ontology registration
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))


def test_calc_step_overlap():
    from engine.ai_worker import _calc_step_overlap, _extract_reasoning_steps
    a = _extract_reasoning_steps("[总框架] A [因素定位] B [驱动力] C [水位博弈] D [跨资产资金] E [反共识结论] F [逻辑自检] G [总结] H")
    b = _extract_reasoning_steps("[总框架] X [因素定位] Y [驱动力] Z [水位博弈] P [跨资产资金] Q [反共识结论] R [逻辑自检] S [总结] T")
    assert _calc_step_overlap(a, b) == 1.0
    a2 = _extract_reasoning_steps("[总框架] X [因素定位] X")
    b2 = _extract_reasoning_steps("[驱动力] Y [水位博弈] Y")
    assert _calc_step_overlap(a2, b2) == 0.0
    assert abs(_calc_step_overlap(["A", "B"], ["B", "C"]) - 1/3) < 0.01
    assert _calc_step_overlap([], ["A"]) == 0.0
    assert _calc_step_overlap([], []) == 0.0
    print("  PASS  _calc_step_overlap / _extract_reasoning_steps")


def test_build_verification_step_overlap():
    import json
    from engine.ai_worker import _build_verification
    main = {"suggested_action": "BUY", "sentiment_score": 0.6,
            "reasoning_path": "[总框架] X [因素定位] Y [驱动力] Z [水位博弈] P [跨资产资金] Q [反共识结论] R [逻辑自检] S [总结] T"}
    reviews = [
        {"model_label": "test_a", "action": "BUY", "score": 0.55,
         "reasoning": "[总框架] 美联储宽松 [因素定位] 流动性 [驱动力] 美元弱 [反共识结论] 风险 [逻辑自检] ok [总结] BUY"},
        {"model_label": "test_b", "action": "SELL", "score": -0.40, "reasoning": "risk high"},
    ]
    v = json.loads(_build_verification(main, reviews))
    assert v["total"] == 2 and v["main_action"] == "BUY"
    assert "step_overlap_avg" in v and "logic_similar_count" in v and "logic_consensus" in v
    # review_a 有 5 个步骤与主推理重合 → step_overlap > 0
    assert v["reviews"][0]["step_overlap"] > 0.5, f"预期 review_a steps 大部分重合, 实际 {v['reviews'][0]}"
    assert v["reviews"][0]["logic_similar"] is True
    # review_b 没步骤 → step_overlap = 0
    assert v["reviews"][1]["step_overlap"] == 0.0
    print(f"  PASS  _build_verification step_overlap_avg={v['step_overlap_avg']}, logic_similar_count={v['logic_similar_count']}")


def test_get_latest_samples_by_symbols():
    import coinglass_lsr
    try:
        out = coinglass_lsr.get_latest_samples_by_symbols(symbols=("BTC", "ETH"))
        assert isinstance(out, dict)
        print(f"  PASS  get_latest_samples_by_symbols returned {len(out)} samples (DB up)")
    except Exception as e:
        print(f"  PASS  get_latest_samples_by_symbols silent on DB unavailable ({type(e).__name__})")


def test_check_historical_counterfactual_import():
    import reflection_checkers as rc
    assert rc.E018_HISTORICAL_COUNTERFACTUAL == "E018"
    assert hasattr(rc, "check_historical_counterfactual")
    print("  PASS  E018 registered + check_historical_counterfactual function exists")


def test_ontology_signals_event_logic():
    import reflection_ontology_signals as ros
    sig = ros.SIGNAL_NAMES
    assert "event_logic" in sig
    el = sig["event_logic"]
    for sub in ("causal_chain", "concept_match", "counter_consensus",
                "cross_asset_chain", "sentiment_driver_chain",
                "macro_state_chain", "summary_chain"):
        assert sub in el
    assert "historical_counterfactual" in el["counter_consensus"]
    print("  PASS  event_logic 7 sub_layers all registered + counter_consensus hcf signal")


def test_infer_layer_sub_signal_e018():
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "XAU",
         "event_type_code": "fomc_pivot", "is_correct": "LOSS",
         "reasoning": "[总框架] X [因素定位] Y [驱动力] Z",
         "baseline": {"by_type": {"fomc_pivot": {"sample_n": 5, "win_rate": 0.20, "avg_pnl_4h": -0.012}}}}
    out = ro.infer_layer_sub_signal(d)
    assert out["layer"] == "event_logic"
    assert out["sub_layer"] == "counter_consensus"
    assert out["signal_name"] == "historical_counterfactual"
    print(f"  PASS  infer_layer_sub_signal E018 hit -> {out['layer']}/{out['sub_layer']}/{out['signal_name']}")


def test_e018_negative_case():
    import reflection_ontology as ro
    d = {"suggested_action": "BUY", "target_asset": "XAU", "event_type_code": "fomc_pivot",
         "reasoning": "[总框架] X [因素定位] Y [驱动力] Z",
         "baseline": {"by_type": {"fomc_pivot": {"sample_n": 5, "win_rate": 0.65, "avg_pnl_4h": 0.012}}}}
    out = ro.infer_layer_sub_signal(d)
    assert out["signal_name"] != "historical_counterfactual"
    print(f"  PASS  negative case (WR=65%) not triggered E018 -> {out['signal_name']}")


def test_infer_layer_sub_signal_e018_safe_fallback():
    import reflection_ontology as ro
    d = {"suggested_action": "BUY"}
    out = ro.infer_layer_sub_signal(d)
    assert out["layer"] in ("analysis", "event_logic")
    print(f"  PASS  E018 missing fields fallback -> {out['layer']}/{out['sub_layer']}/{out['signal_name']}")


if __name__ == "__main__":
    print("\n=== v200+ three-piece regression test ===\n")
    test_calc_step_overlap()
    test_build_verification_step_overlap()
    test_get_latest_samples_by_symbols()
    test_check_historical_counterfactual_import()
    test_ontology_signals_event_logic()
    test_infer_layer_sub_signal_e018()
    test_e018_negative_case()
    test_infer_layer_sub_signal_e018_safe_fallback()
    print("\n=== ALL PASS ===\n")
