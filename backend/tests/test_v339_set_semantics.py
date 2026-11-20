# -*- coding: utf-8 -*-
"""v339: 枚举参数的 set 语义。

背景：`min_event_strength` 是 {low, medium, high}、`asset_filter` 是资产白名单，
两者都无法用"up 10%"这种比例缩放表达。原实现里 `check_direction` 对 set 直接返回
True（不校验取值），于是任意串都能"通过校验"再被 `clamp_params` 清空成空串 ——
等于把门槛整个关掉，比不改更坏。这里锁死三件事：

1. set 必须带 target_value，且必须在值域内；
2. 数值参数不接受 set（避免 magnitude 的"比例"和"绝对值"两种语义混用）；
3. `_validate_recommendations` 对非法 set 直接丢弃，不让它进 recommendations。
"""
import pytest

from param_key_aliases import check_direction


class TestCheckDirectionSet:
    def test_enum_set_accepts_legal_value(self):
        ok, why = check_direction("min_event_strength", "set", 0.1, "medium")
        assert ok is True, why

    def test_enum_set_rejects_illegal_value(self):
        ok, why = check_direction("min_event_strength", "set", 0.1, "bogus")
        assert ok is False
        assert "bogus" in why and "low/medium/high" in why

    def test_enum_set_requires_target_value(self):
        ok, why = check_direction("min_event_strength", "set", 0.1, None)
        assert ok is False
        assert "target_value" in why

    def test_asset_filter_set_validates_against_allowlist(self):
        ok, why = check_direction("asset_filter", "set", 0.05, "XAU")
        assert ok is True, why
        ok, why = check_direction("asset_filter", "set", 0.05, "NOTAREAL")
        assert ok is False
        assert "NOTAREAL" in why and "XAU" in why

    def test_numeric_rejects_set(self):
        """数值参数用 set 会让 magnitude 同时表示比例和绝对值，必须拒绝。"""
        ok, why = check_direction("signal_threshold", "set", 0.3)
        assert ok is False
        assert "set" in why

    def test_enum_still_rejects_up_down(self):
        ok, why = check_direction("min_event_strength", "up", 0.2)
        assert ok is False
        assert "只接受 set" in why


class TestValidateRecommendationsSet:
    def test_legal_set_survives_and_carries_target(self):
        from engine.strategy_reflection import _validate_recommendations
        recs = _validate_recommendations([
            {"key": "min_event_strength", "direction": "set",
             "target_value": "medium", "magnitude": 0.1},
        ])
        assert len(recs) == 1
        assert recs[0]["target_value"] == "medium"
        assert recs[0]["magnitude"] == 0.1

    def test_illegal_set_is_dropped(self):
        from engine.strategy_reflection import _validate_recommendations
        assert _validate_recommendations([
            {"key": "min_event_strength", "direction": "set",
             "target_value": "bogus", "magnitude": 0.1},
        ]) == []
        assert _validate_recommendations([
            {"key": "min_event_strength", "direction": "set", "magnitude": 0.1},
        ]) == []

    def test_numeric_set_kept_with_value_dropped_without(self):
        """v339: 数值参数的 set 是合法的（signal_threshold 基线 0，比例调整恒为空操作）。
        但必须带数值 target_value，缺了或给非数值就丢弃 —— 不能靠 magnitude 蒙混。"""
        from engine.strategy_reflection import _validate_recommendations
        kept = _validate_recommendations([
            {"key": "signal_threshold", "direction": "set",
             "target_value": 0.3, "magnitude": 0.1},
        ])
        assert len(kept) == 1
        assert kept[0]["direction"] == "set"
        assert kept[0]["target_value"] == 0.3
        # 缺值 / 非数值 → 丢弃
        assert _validate_recommendations([
            {"key": "signal_threshold", "direction": "set", "magnitude": 0.1},
        ]) == []
        assert _validate_recommendations([
            {"key": "signal_threshold", "direction": "set",
             "target_value": "high", "magnitude": 0.1},
        ]) == []

    def test_plain_up_still_works(self):
        from engine.strategy_reflection import _validate_recommendations
        recs = _validate_recommendations([
            {"key": "buy_min_score", "direction": "up", "magnitude": 0.15},
        ])
        assert len(recs) == 1
        assert recs[0]["target_value"] is None
        assert recs[0]["direction"] == "up"

    def test_namespaced_key_is_normalized(self):
        """v339: LLM 写 `score.signal_threshold` 时应归一化成 `signal_threshold` 再进白名单，
        而不是被当成自创 key 静默丢掉；没有登记前缀的别名仍必须拒。"""
        from engine.strategy_reflection import _validate_recommendations
        recs = _validate_recommendations([
            {"key": "score.signal_threshold", "direction": "set", "target_value": 0.2},
        ])
        assert len(recs) == 1
        assert recs[0]["key"] == "signal_threshold"
        assert _validate_recommendations([
            {"key": "strategy.signal_threshold", "direction": "set", "target_value": 0.3},
        ]) == []
        assert _validate_recommendations([
            {"key": "totally_bogus", "direction": "up", "magnitude": 0.1},
        ]) == []
