"""policy_engine + trade_decisions regression test suite (P2 #13)。

覆盖范围：
  P0: sentiment_score 三档兼容（P2 #10）
  P1: policy_engine.evaluate()（P1 #5 ADJUST_SIZE_PCT + P2 #9 锚定）
  P2: 闸门拒绝聚合（P2 #9）
  P3: shadow 投影（P2 #12）

设计目标：每次 policy_engine / trade_decisions 改动后能秒级验证关键路径。
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3

import pytest

# 路径设置（与 tests/conftest.py 一致）
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# =============================================================================
# P0: sentiment_score 三档兼容
# =============================================================================

class TestSentimentNormalize:
    """P2 #10: LLM 可能给 0-100 / 0-1 / -1~1，统一归到 -1~1 存 DB。"""

    def test_three_tier_negative_is_c_tier(self):
        from engine.ai_worker import _normalize_sentiment_score
        assert _normalize_sentiment_score(-0.5) == -0.5
        assert _normalize_sentiment_score(-0.05) == -0.05
        assert _normalize_sentiment_score(-1.0) == -1.0
        # 超出 -1 也 clamp
        assert _normalize_sentiment_score(-1.5) == -1.0

    def test_three_tier_large_positive_is_a_tier(self):
        from engine.ai_worker import _normalize_sentiment_score
        assert _normalize_sentiment_score(75) == 0.75
        assert _normalize_sentiment_score(100) == 1.0
        assert _normalize_sentiment_score(0) == 0.0  # 边界 0 不进 a
        assert _normalize_sentiment_score(50) == 0.5
        assert _normalize_sentiment_score(150) == 1.0  # 超出 100 clamp

    def test_three_tier_zero_to_one_is_b_tier(self):
        from engine.ai_worker import _normalize_sentiment_score
        assert _normalize_sentiment_score(0.0) == 0.0
        assert _normalize_sentiment_score(0.5) == 0.5
        assert _normalize_sentiment_score(1.0) == 1.0
        assert _normalize_sentiment_score(0.42) == 0.42

    def test_invalid_inputs_default_to_zero(self):
        from engine.ai_worker import _normalize_sentiment_score
        assert _normalize_sentiment_score(None) == 0.0
        assert _normalize_sentiment_score("garbage") == 0.0
        assert _normalize_sentiment_score({}) == 0.0

    def test_norm_score_action_three_tier_preserved(self):
        from engine.ai_worker import _norm_score_action
        s, a = _norm_score_action(80, "BUY")
        assert s == 0.8 and a == "BUY"
        s, a = _norm_score_action(0.6, "SELL")
        assert s == 0.6 and a == "SELL"
        s, a = _norm_score_action(-0.4, "SELL")
        assert s == -0.4 and a == "SELL"

    def test_norm_score_action_hold_below_threshold_zeroed(self):
        from engine.ai_worker import _norm_score_action
        # 极小值归 0
        s, _ = _norm_score_action(0.02, "HOLD")
        assert s == 0.0
        s, _ = _norm_score_action(-0.01, "HOLD")
        assert s == 0.0
        # 超过阈值保留
        s, _ = _norm_score_action(0.10, "HOLD")
        assert s == 0.10
        s, _ = _norm_score_action(-0.10, "HOLD")
        assert s == -0.10

    def test_norm_score_action_direction_alignment(self):
        from engine.ai_worker import _norm_score_action
        # 0.0 + BUY → +0.05（避免空方向）
        s, _ = _norm_score_action(0.0, "BUY")
        assert s == 0.05
        # 0.0 + SELL → -0.05
        s, _ = _norm_score_action(0.0, "SELL")
        assert s == -0.05
        # HOLD 不受此影响
        s, _ = _norm_score_action(0.0, "HOLD")
        assert s == 0.0

    def test_norm_score_action_clamp_to_unit(self):
        from engine.ai_worker import _norm_score_action
        s, _ = _norm_score_action(150, "BUY")
        assert s == 1.0
        s, _ = _norm_score_action(-150, "SELL")
        assert s == -1.0


# =============================================================================
# P1: policy_engine.evaluate()
# =============================================================================

@pytest.fixture
def policy_db():
    """:memory: DB with policy_engine schema"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db
    db.migrate(conn)
    import policy_engine
    policy_engine.init_schema(conn)
    yield conn
    conn.close()


class TestPolicyEngineEvaluate:
    """P1 #5 ADJUST_SIZE_PCT 实战 + P2 #9 锚定闸门值"""

    def test_no_rules_returns_allow(self, policy_db):
        import policy_engine
        d = policy_engine.evaluate(policy_db, "XAU", "BUY")
        assert d.allow is True
        assert d.rule_type is None
        # policy_engine 默认 adjusted_notional_pct=1.0（无规则时满仓）
        assert d.adjusted_notional_pct == 1.0

    def test_forbid_action_blocks(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="FORBID_ACTION", params={"reason": "hedge"},
        )
        d = policy_engine.evaluate(policy_db, "XAU", "BUY")
        assert d.allow is False
        assert d.rule_type == "FORBID_ACTION"
        assert "hedge" in d.reason

    def test_forbid_action_does_not_block_other_asset(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="FORBID_ACTION", params={"reason": "hedge"},
        )
        d = policy_engine.evaluate(policy_db, "WTI", "BUY")
        assert d.allow is True

    def test_require_rr_blocks_below_min(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="ALL", action="ALL",
            rule_type="REQUIRE_RR", params={"min": 2.0},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={}).allow is False
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"rr_ratio": 1.5}).allow is False
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"rr_ratio": 2.5}).allow is True

    def test_max_size_usdt_caps_notional(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="ALL", action="ALL",
            rule_type="MAX_SIZE_USDT", params={"cap": 50.0},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"notional_usdt": 30.0}).allow is True
        d = policy_engine.evaluate(policy_db, "XAU", "BUY", context={"notional_usdt": 100.0})
        assert d.allow is False
        assert "100" in d.reason and "50" in d.reason

    def test_adjust_size_pct_passes_with_pct(self, policy_db):
        """P1 #5: XAU BUY 减仓 50% 用的就是这个 rule_type"""
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="ADJUST_SIZE_PCT", params={"pct": 0.5},
        )
        d = policy_engine.evaluate(policy_db, "XAU", "BUY")
        assert d.allow is True
        assert d.rule_type == "ADJUST_SIZE_PCT"
        assert d.adjusted_notional_pct == 0.5

    def test_require_confidence_blocks_low(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="ALL", action="ALL",
            rule_type="REQUIRE_CONFIDENCE", params={"min": 0.6},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={}).allow is False
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"evidence_confidence": 0.3}).allow is False
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"evidence_confidence": 0.8}).allow is True

    def test_asset_ALL_matches_specific(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="ALL", action="BUY",
            rule_type="MAX_SIZE_USDT", params={"cap": 50.0},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY", context={"notional_usdt": 100.0}).allow is False
        assert policy_engine.evaluate(policy_db, "BTC", "BUY", context={"notional_usdt": 100.0}).allow is False
        # SELL 不触发
        assert policy_engine.evaluate(policy_db, "XAU", "SELL", context={"notional_usdt": 100.0}).allow is True

    def test_action_ALL_matches_specific(self, policy_db):
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="XAU", action="ALL",
            rule_type="FORBID_ACTION", params={"reason": "blocked"},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY").allow is False
        assert policy_engine.evaluate(policy_db, "XAU", "SELL").allow is False
        assert policy_engine.evaluate(policy_db, "BTC", "BUY").allow is True

    def test_disabled_rule_does_not_trigger(self, policy_db):
        import policy_engine
        rule_id = policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="FORBID_ACTION", params={"reason": "x"},
        )
        assert policy_engine.evaluate(policy_db, "XAU", "BUY").allow is False
        policy_engine.disable_rule(policy_db, rule_id)
        assert policy_engine.evaluate(policy_db, "XAU", "BUY").allow is True

    def test_multiple_rules_return_first_match(self, policy_db):
        """多条规则同时满足时按 id 顺序返回第一个"""
        import policy_engine
        policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="FORBID_ACTION", params={"reason": "first"},
        )
        policy_engine.add_rule(
            policy_db, asset="XAU", action="BUY",
            rule_type="MAX_SIZE_USDT", params={"cap": 50.0},
        )
        d = policy_engine.evaluate(policy_db, "XAU", "BUY", context={"notional_usdt": 100.0})
        assert d.allow is False
        assert d.rule_type == "FORBID_ACTION"


# =============================================================================
# P2: 闸门拒绝聚合
# =============================================================================

@pytest.fixture
def decisions_db():
    """:memory: DB 预填 BUY/SELL 决策（被闸门拒 / 通过各几条）

    注：get_ai_decision_gate_breakdown 过滤用 ts 字段（生产代码未迁移到 created_at），
    所以这里要同时填 ts 和 created_at 字段。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db
    db.migrate(conn)
    now = int(time.time())
    # 字段顺序：(id, news_id, suggested_action, target_asset, sentiment_score,
    #           evidence_confidence, evidence_action, trade_gate_reason, ts)
    rows = [
        (1, 1, "BUY", "XAU", 50, 5.0, "HOLD", "统一置信度 5.0 低于闸门 40.0", now - 100),
        (2, 1, "BUY", "WTI", 30, 8.0, "HOLD", "统一置信度 8.0 低于闸门 40.0", now - 200),
        (3, 1, "SELL", "BTC", 60, 50.0, "SELL", "", now - 300),  # 通过
        (4, 1, "SELL", "ETH", 20, 15.0, "HOLD", "统一置信度 15.0 低于闸门 40.0", now - 400),
    ]
    conn.executemany(
        """INSERT INTO ai_decisions
           (id, news_id, suggested_action, target_asset, sentiment_score,
            evidence_confidence, evidence_action, trade_gate_reason, reasoning, ts, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', ?, datetime(?, 'unixepoch'))""",
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[8]) for r in rows],
    )
    conn.commit()
    yield conn
    conn.close()


class TestGateBreakdown:
    """P2 #9: 闸门拒绝聚合 + 数字模板归并"""

    def test_basic_counts(self, decisions_db):
        from trade_decisions import get_ai_decision_gate_breakdown
        r = get_ai_decision_gate_breakdown(decisions_db, lookback_days=7)
        assert r["buy_or_sell"] == 4
        assert r["gate_rejected"] == 3
        assert r["gate_passed"] == 1
        assert r["rejection_rate_pct"] == 75.0

    def test_by_asset_distribution(self, decisions_db):
        from trade_decisions import get_ai_decision_gate_breakdown
        r = get_ai_decision_gate_breakdown(decisions_db, lookback_days=7)
        by_asset = {x["asset"]: x for x in r["by_asset"]}
        assert by_asset["XAU"]["n"] == 1
        assert by_asset["XAU"]["rejected"] == 1
        assert by_asset["BTC"]["n"] == 1
        assert by_asset["BTC"]["rejected"] == 0
        assert by_asset["BTC"]["passed"] == 1

    def test_reason_template_merges_numbers(self, decisions_db):
        """数字部分应被归并成 N"""
        from trade_decisions import get_ai_decision_gate_breakdown
        r = get_ai_decision_gate_breakdown(decisions_db, lookback_days=7)
        templates = [x["template"] for x in r["by_reason_template"]]
        assert any("N" in t and "低于闸门" in t for t in templates)
        joined = " | ".join(templates)
        assert "5.0" not in joined
        assert "8.0" not in joined

    def test_confidence_buckets(self, decisions_db):
        from trade_decisions import get_ai_decision_gate_breakdown
        r = get_ai_decision_gate_breakdown(decisions_db, lookback_days=7)
        buckets = {x["range"]: x["n"] for x in r["confidence_buckets"]}
        # 5.0 → 0-10; 8.0 → 0-10; 15.0 → 10-20; 50.0 → 40-60
        assert buckets.get("0-10", 0) == 2
        assert buckets.get("10-20", 0) == 1
        assert buckets.get("40-60", 0) == 1


# =============================================================================
# P3: shadow 投影
# =============================================================================

@pytest.fixture
def shadow_db():
    """:memory: DB + 决策 + quick_sim_trades（混合胜负）"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db
    db.migrate(conn)
    now = int(time.time())
    # 字段顺序：(id, news_id, suggested_action, target_asset, sentiment_score,
    #           evidence_confidence, evidence_action, trade_gate_reason, ts)
    decisions = [
        (1, 1, "BUY", "XAU", 50, 50.0, "SELL", "", now - 100),  # conf=50 → PnL +3%
        (2, 1, "BUY", "WTI", 50, 50.0, "SELL", "", now - 200),  # conf=50 → PnL -2%
        (3, 1, "SELL", "BTC", 5, 5.0, "SELL", "", now - 300),   # conf=5  → PnL +1%
        (4, 1, "SELL", "ETH", 5, 5.0, "SELL", "", now - 400),   # conf=5  → PnL -0.5%
        (5, 1, "BUY", "SOL", 5, 5.0, "SELL", "", now - 500),    # conf=5  → PnL +0.5%
    ]
    pnl = {1: 3.0, 2: -2.0, 3: 1.0, 4: -0.5, 5: 0.5}
    for d in decisions:
        # d = (id, news_id, suggested_action, target_asset, sentiment_score,
        #      evidence_confidence, evidence_action, trade_gate_reason, ts)
        conn.execute(
            """INSERT INTO ai_decisions
               (id, news_id, suggested_action, target_asset, sentiment_score,
                evidence_confidence, evidence_action, trade_gate_reason, reasoning, ts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', ?, datetime(?, 'unixepoch'))""",
            (d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[8]),
        )
        conn.execute(
            """INSERT INTO quick_sim_trades
               (decision_id, asset, action, pnl_pct, settled, gate_passed,
                entry_price, entry_ts, horizon_minutes)
               VALUES (?, ?, ?, ?, 1, 1, 100.0, ?, 60)""",
            (d[0], d[3], d[2], pnl[d[0]], d[8]),
        )
    conn.commit()
    yield conn
    conn.close()


class TestShadowProjection:
    """P2 #12: 闸门 / 模型校准 dry-run 投影"""

    def test_returns_4_scenarios(self, shadow_db):
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(shadow_db, lookback_days=7)
        assert r["total_buy_sell"] == 5
        assert len(r["scenarios"]) == 4
        labels = [s["label"] for s in r["scenarios"]]
        assert any("现状" in l for l in labels)
        assert any("闸门 → 20" in l for l in labels)
        assert any("P2 #10" in l for l in labels)

    def test_current_gate_filters(self, shadow_db):
        """闸门 20 时 conf=5 不通过 (5<20)；conf=50 通过 2 条 (50>=20)"""
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(shadow_db, lookback_days=7)
        cur = r["scenarios"][0]
        # 2026-10-08 闸门 40→20，current_gate 默认从 config.EVIDENCE_CONFIDENCE_GATE 读
        assert cur["gate"] == 20.0
        assert cur["would_pass"] == 2  # 决策 1+2 (conf=50)
        assert cur["projected_pnl_pct"] == 1.0  # +3 + (-2) = +1
        assert cur["win_rate_pct"] == 50.0

    def test_boost_multiplies_low_conf(self, shadow_db):
        """boost 路径：conf=5 × 5 = 25，能过 20 闸门"""
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(shadow_db, lookback_days=7)
        combo = r["scenarios"][3]
        # conf=5 × 5 = 25 ≥ 20 ✓
        # conf=50 × 5 = 250 → clamp 100 ≥ 20 ✓
        # 全部 5 条都过
        assert combo["would_pass"] == 5
        # PnL: 3 + (-2) + 1 + (-0.5) + 0.5 = 2.0
        assert combo["projected_pnl_pct"] == 2.0

    def test_boost_saturates_at_100(self, shadow_db):
        """boost 后 conf × 5：conf=50 → 250 clamp 100 ≥ 20 ✓；conf=5 × 5 = 25 ≥ 20 ✓"""
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(shadow_db, lookback_days=7)
        boosted = r["scenarios"][2]
        # 闸门 20 时 conf=5 × 5 = 25 ≥ 20 → 5 条全过（不是 2 条）
        assert boosted["would_pass"] == 5
        # PnL: 3 + (-2) + 1 + (-0.5) + 0.5 = 2.0
        assert boosted["projected_pnl_pct"] == 2.0

    def test_empty_data_returns_empty_scenarios(self):
        """空 DB 不崩"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        import db
        db.migrate(conn)
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(conn, lookback_days=7)
        assert r["total_buy_sell"] == 0
        assert r["scenarios"] == []
        conn.close()

    def test_holds_excluded(self):
        """HOLD 决策不应计入 BUY/SELL 基线"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        import db
        db.migrate(conn)
        now = int(time.time())
        for i, action in enumerate(["BUY", "HOLD", "HOLD"]):
            conn.execute(
                """INSERT INTO ai_decisions
                   (id, news_id, suggested_action, target_asset, sentiment_score,
                    evidence_confidence, evidence_action, trade_gate_reason, reasoning, ts, created_at)
                   VALUES (?, 1, ?, 'XAU', 50, 50, 'SELL', '', 'test', ?, datetime(?, 'unixepoch'))""",
                (i + 1, action, now - 100, now - 100),
            )
        conn.commit()
        from trade_decisions import get_gate_shadow_projection
        r = get_gate_shadow_projection(conn, lookback_days=7)
        assert r["total_buy_sell"] == 1  # 只算 BUY
        conn.close()


# =============================================================================
# 套件自检
# =============================================================================

def test_suite_summary():
    """冒烟测试：本文件包含所有 P0-P3 测试类"""
    test_classes = [
        TestSentimentNormalize,
        TestPolicyEngineEvaluate,
        TestGateBreakdown,
        TestShadowProjection,
    ]
    for cls in test_classes:
        methods = [m for m in dir(cls) if m.startswith("test_")]
        assert len(methods) >= 4, f"{cls.__name__} 测试 case 太少了：{methods}"
    total = sum(len([m for m in dir(c) if m.startswith("test_")]) for c in test_classes)
    print(f"\n✓ policy_engine regression suite: {len(test_classes)} 类 / {total} 测试")
