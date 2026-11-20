"""v325 — 慢涨/慢跌归因口径修复（全离线）。

背景（2026-11-13 生产库只读实测）：
  · critical_price_moves 里 slow_rise 111 条 → event_move_attribution **0 条**
  · slow_fall 141 条 → 5 条
  · 根因：#1 窗口按形态给了 ±3d（`_window_for_kind`），但 #2 时间闸门
    `_apply_lag_gates` 对所有形态硬切 lag>6h → 慢形态候选在闸门里被全部丢弃。
  · 附带发现：只放开窗口不加约束，慢形态会被 `cross`（同组同向）弱匹配灌满，
    实测 BNB −5.07% 被挂到「tBTC 跨链桥恢复」、XRP −4.44% 被挂到
    「Machi 卖出无聊猿」—— 假因果比缺归因更伤。

本文件锁死三件事：
  1. 闸二上限按形态（快 6h / 慢 72h），且与窗口同口径（不可能"窗大闸小"）；
  2. 慢形态的远距离候选保留但**分档降权**，负延迟仍然绝对禁止；
  3. 慢形态的 `cross` 弱匹配一律不进主表（`asset`/`semantic` 才放行）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import calc_event_move_attribution as attr


# ── 1. 上限按形态，且与窗口同口径 ──────────────────────────────────────────

def test_lag_max_for_slow_is_three_days():
    assert attr._lag_max_for_kind("slow_rise", 1440) == attr.LAG_MAX_SLOW_SEC == 72 * 3600
    assert attr._lag_max_for_kind("slow_fall", 4320) == 72 * 3600


def test_lag_max_for_fast_stays_six_hours():
    assert attr._lag_max_for_kind("fast_rise", 60) == 6 * 3600
    assert attr._lag_max_for_kind("fast_fall", 30) == 6 * 3600


def test_lag_max_default_follows_duration():
    assert attr._lag_max_for_kind("", 120) == 6 * 3600      # 短形态 → 快口径
    assert attr._lag_max_for_kind("unknown", 900) == attr.LAG_MAX_DEFAULT_SEC == 86400


def test_window_and_lag_limit_never_contradict():
    """闸门不得比候选窗口更紧 —— 否则该形态必然 0 覆盖（旧 bug 的判据）。"""
    for kind, dur in [("fast_rise", 60), ("fast_fall", 30), ("slow_rise", 1440),
                      ("slow_fall", 4320), ("", 900)]:
        win = attr._window_for_kind(kind, dur)
        lag_max = attr._lag_max_for_kind(kind, dur)
        assert lag_max >= win, f"{kind} 闸门不应比窗口更紧 (win={win}, gate={lag_max})"


# ── 2. 分档降权 + 负延迟硬禁 ────────────────────────────────────────────────

def _item(score, lag, match_type="asset", retro=False):
    return (score, 1, "src", lag, match_type, retro, "headline")


def test_slow_far_lag_is_kept_but_discounted():
    kept, st = attr._apply_lag_gates([_item(0.8, 48 * 3600)], attr.LAG_MAX_SLOW_SEC)
    assert len(kept) == 1, "48h 在 72h 内, 应保留"
    assert kept[0][0] == round(0.8 * attr.LAG_PENALTY_FAR_FACTOR, 4)
    assert st["dropped_too_far"] == 0
    assert st["lag_penalized"] == 1


def test_slow_mid_lag_discount_is_gentler_than_near():
    near, _ = attr._apply_lag_gates([_item(0.8, 2 * 3600)], 72 * 3600)
    mid, _ = attr._apply_lag_gates([_item(0.8, 12 * 3600)], 72 * 3600)
    far, _ = attr._apply_lag_gates([_item(0.8, 48 * 3600)], 72 * 3600)
    assert near[0][0] > mid[0][0] > far[0][0], "距离越远分数越低, 且单调"


def test_fast_move_still_hard_capped_at_six_hours():
    """快形态必须保持原纪律 —— 8 小时前的新闻解释不了 60 分钟的异动。"""
    kept, st = attr._apply_lag_gates([_item(0.9, 8 * 3600)], attr.LAG_MAX_FAST_SEC)
    assert kept == []
    assert st["dropped_too_far"] == 1


def test_negative_lag_rejected_for_slow_too():
    """事后拟合永远不算因果 —— 慢形态也不例外。"""
    kept, st = attr._apply_lag_gates([_item(0.95, -3600)], attr.LAG_MAX_SLOW_SEC)
    assert kept == []
    assert st["dropped_negative_lag"] == 1


def test_slow_beyond_three_days_dropped():
    kept, st = attr._apply_lag_gates([_item(0.9, 80 * 3600)], attr.LAG_MAX_SLOW_SEC)
    assert kept == []
    assert st["dropped_too_far"] == 1


def test_default_gate_matches_legacy_behaviour():
    """不传上限时保持 v323 的 6h 纪律（向后兼容, 老调用点不受影响）。"""
    kept, st = attr._apply_lag_gates([_item(0.7, 8 * 3600)])
    assert kept == [] and st["dropped_too_far"] == 1


# ── 3. 慢形态拒收 cross 弱匹配 ──────────────────────────────────────────────

def test_slow_cross_is_blocked():
    assert attr._slow_weak_blocked("cross", True) is True
    assert attr._slow_weak_blocked("CROSS", True) is True


def test_slow_direct_hits_are_allowed():
    assert attr._slow_weak_blocked("asset", True) is False
    assert attr._slow_weak_blocked("semantic", True) is False


def test_fast_cross_is_untouched():
    """快形态的 cross 是既有行为, 本次不动（避免夹带口径变更）。"""
    assert attr._slow_weak_blocked("cross", False) is False
    assert attr._slow_weak_blocked("", False) is False


# ── 4. 慢形态方向一致性 ────────────────────────────────────────────────────

def test_headline_direction():
    assert attr._headline_direction("BNB 跌破 700 美元") == -1
    assert attr._headline_direction("比特币涨超 5%") == 1
    assert attr._headline_direction("英伟达跌逾 8%") == -1
    assert attr._headline_direction("纳指跌、芯片涨") == 0   # 方向混杂 → 不下判断
    assert attr._headline_direction("美联储宣布维持利率不变") == 0
    assert attr._headline_direction("") == 0


def test_slow_direction_contradiction_is_blocked():
    """「跌破」不能拿来解释上涨。"""
    assert attr._direction_blocked("BNB 跌破 700 美元", 3.01) is True
    assert attr._direction_blocked("比特币涨超 5%", -2.0) is True


def test_slow_direction_consistency_passes():
    assert attr._direction_blocked("比特币涨超 5%", 3.0) is False
    assert attr._direction_blocked("BNB 跌破 700 美元", -3.0) is False
    assert attr._direction_blocked("美联储维持利率不变", 3.0) is False


# ── 5. 行情播报（价格回声）不分快慢形态 ─────────────────────────────────────

def test_price_echo_detection():
    assert attr._is_price_echo("SOL 跌破 95 美元，24 小时跌幅 0.84%") is True
    assert attr._is_price_echo("BNB 跌破 700 美元") is True
    assert attr._is_price_echo("SOL 跌破 95 美元") is True
    # 事件型标题不是行情播报
    assert attr._is_price_echo("Elliptic：Binance 黑客将约 5000 万美元 XRP 兑换为其他资产") is False
    assert attr._is_price_echo("美联储宣布维持利率不变") is False
    assert attr._is_price_echo("") is False


def test_direction_blocked_applies_to_fast_too():
    """实测反例: SOL fast_rise +1.57% 被「跌破 95 美元」解释 (score 0.821);
    MU fast_rise +1.62% 被「AI 硬件大幅下挫」解释。"""
    assert attr._direction_blocked("SOL 跌破 95 美元，24 小时跌幅 0.84%", 1.5662) is True
    assert attr._direction_blocked("美股 AI 硬件与半导体板块大幅下挫，Lumentum 跌超 11%", 1.6186) is True
    # 同向不拦（跌破解释下跌：可视为支撑失守）
    assert attr._direction_blocked("SOL 跌破 95 美元，24 小时跌幅 0.84%", -3.0) is False
    # 非播报/无方向标题不受影响
    assert attr._direction_blocked("Binance 黑客抛售 XRP", 1.5) is False
