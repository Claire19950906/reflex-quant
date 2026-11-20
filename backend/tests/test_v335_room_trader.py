"""房间交易员打分器的单元测试 (v335)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

from room_trader import decide, SCORE_BUY, SCORE_WATCH


def _ctx(**kw):
    base = {"asset": "BTC", "price": 100.0, "ret_30m": 0.0, "ret_1h": 0.0,
            "ret_4h": 0.0, "atr_pct": 0.10, "vol_ratio": 1.0, "funding_pct": None}
    base.update(kw)
    return base


def test_no_direction_holds():
    assert decide(_ctx(), "HOLD")["action"] == "HOLD"


def test_strong_setup_buys():
    """市场确认 + 双趋势同向 + 放量 → 应该下手。"""
    d = decide(_ctx(ret_30m=0.08, ret_1h=0.15, ret_4h=0.30, vol_ratio=1.8), "BUY")
    assert d["action"] == "BUY"
    assert d["score"] >= SCORE_BUY
    assert 1.0 <= d["size_pct"] <= 5.0


def test_no_market_confirmation_never_buys():
    """新闻说买但市场没动 → 不给分, 不下手 (这是 40% 命中率的主因)。"""
    d = decide(_ctx(ret_30m=-0.02, ret_1h=-0.05, ret_4h=-0.10, vol_ratio=0.6), "BUY")
    assert d["action"] == "HOLD"
    assert d["score"] < SCORE_WATCH


def test_priced_in_penalised():
    """已经跑完的行情扣分 → 不追。"""
    fresh = decide(_ctx(ret_30m=0.05, ret_1h=0.10, ret_4h=0.20, vol_ratio=1.6), "BUY")
    late = decide(_ctx(ret_30m=0.25, ret_1h=0.30, ret_4h=0.45, vol_ratio=1.6), "BUY")
    assert late["score"] < fresh["score"]


def test_crowded_funding_penalised():
    """资金费率极端且同向 = 拥挤, 扣分。"""
    normal = decide(_ctx(ret_30m=0.05, ret_1h=0.10, vol_ratio=1.6, funding_pct=0.01), "BUY")
    crowded = decide(_ctx(ret_30m=0.05, ret_1h=0.10, vol_ratio=1.6, funding_pct=0.09), "BUY")
    assert crowded["score"] < normal["score"]


def test_sell_mirrors_buy():
    """SELL 方向逻辑对称。"""
    up = decide(_ctx(ret_30m=0.08, ret_1h=0.15, vol_ratio=1.8), "BUY")
    down = decide(_ctx(ret_30m=-0.08, ret_1h=-0.15, vol_ratio=1.8), "SELL")
    assert up["score"] == down["score"]
