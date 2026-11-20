"""v479：波动档调仓位的**收益侧结论**必须随读数一起走，且不允许悄悄改口径。

背景：v414 只用「5 根内 |MAE|」验过风险侧，收益侧一直是欠账。v479 补了回测
（`scripts/risk_sizing_replay.py`，16 标的、因果、含随机对照 + 符号翻转 + 时间外），结论：
  - 降回撤 16/16 成立；
  - Calmar 仅 4/16 优于满仓，符号翻转后 0/16 → 等价于降杠杆；
  - 连续等风险样本内 9/16，但**时间外滚动窗口胜率 30.8%（20/65，剔除基准过弱窗）** → 样本内拟合。
所以本版**不改** size_factor 的计算，只把证据挂进读数。这个测试锁两件事：
  1. 证据字段存在且内容不能被删（删了就等于把"没验证过"重新藏起来）；
  2. size_factor 的计算口径不许在本轮被顺手改掉（回归护栏）。
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import technical_state as ts


def _state(**blocks):
    base = {"trend": {}, "volatility": {}, "value_area": {}, "leverage": {},
            "flow": {}, "positioning": {}, "_meta": {"timeframe": "1d"}}
    base.update(blocks)
    return base


# ---------------------------------------------------------------------------
# 一、证据字段
# ---------------------------------------------------------------------------

def test_daily_risk_carries_sizing_evidence():
    st = _state(volatility={"atr_pct": 2.2}, trend={"adx": 25.0},
                value_area={"position": "in_value"}, leverage={})
    r = ts.risk_profile(st, "1d")
    ev = r.get("sizing_evidence")
    assert isinstance(ev, dict), "收益侧证据必须随读数返回"
    assert ev["verdict"] == "not_supported"
    assert "时间外" in ev["rule"], "规则必须点明改口径要先过时间外检验"


def test_evidence_keeps_all_four_numbers():
    """四个关键数字缺一个，结论就会被读歪。"""
    ev = ts._RISK_SIZING_EVIDENCE
    assert "16/16" in ev["risk_side"]
    assert "4/16" in ev["return_side_discrete"]
    assert "0/16" in ev["return_side_discrete"]
    assert "30.8%" in ev["return_side_continuous"]
    assert "65" in ev["return_side_continuous"]
    assert ev["report"].endswith("risk_sizing_replay.md")


def test_evidence_available_on_intraday_too():
    """4h 不缩仓位，但同样要带证据 —— 否则前端切周期就看不到这句话。"""
    st = _state(volatility={"atr_pct": 2.4}, trend={"adx": 40.0},
                value_area={"position": "below_value"}, leverage={})
    r = ts.risk_profile(st, "4h")
    assert r["sizing_evidence"]["verdict"] == "not_supported"


def test_validation_note_mentions_return_side():
    st = _state(volatility={"atr_pct": 2.2}, trend={"adx": 25.0},
                value_area={"position": "in_value"}, leverage={})
    r = ts.risk_profile(st, "1d")
    assert "收益侧" in r["validation_note"]
    assert "30.8%" in r["validation_note"]


def test_error_state_has_no_evidence():
    """读不出来的状态不该伪造证据 —— 保持 available=False。"""
    r = ts.risk_profile({"error": "只有 10 根 K 线"}, "1d")
    assert r["available"] is False
    assert "sizing_evidence" not in r


# ---------------------------------------------------------------------------
# 二、回归护栏：size_factor 口径本轮不许变
# ---------------------------------------------------------------------------

def test_daily_bands_unchanged_low_mid_high():
    """日线三档仍是 1.0 / 0.8 / 0.5 —— 证据不支持改，就不许改。"""
    for atr, level, size in ((0.9, "低", 1.0), (2.2, "中", 0.8), (4.0, "高", 0.5)):
        st = _state(volatility={"atr_pct": atr}, trend={"adx": 25.0},
                    value_area={"position": "in_value"}, leverage={})
        r = ts.risk_profile(st, "1d")
        assert r["level"] == level, f"ATR {atr}% 应判 {level}"
        assert r["size_factor"] == size, f"{level} 档应为 ×{size}"
        assert r["size_basis"] == "atr_band_daily"


def test_intraday_never_cuts_size():
    """日内仍然不缩仓（只调止损），不能被本轮的日线结论带到 4h 上。"""
    st = _state(volatility={"atr_pct": 3.1}, trend={"adx": 50.0},
                value_area={"position": "above_value"}, leverage={})
    r = ts.risk_profile(st, "4h")
    assert r["size_factor"] == 1.0
    assert r["size_basis"] == "uniform_intraday"


def test_stop_multiple_still_driven_by_band():
    """止损倍数继续按档位走（1.5 / 2 / 3）—— 这是本轮明确保留的部分。"""
    st = _state(volatility={"atr_pct": 4.0}, trend={}, value_area={}, leverage={})
    r = ts.risk_profile(st, "1d")
    assert r["stop_atr_multiple"] == 3.0
    st2 = _state(volatility={"atr_pct": 0.9}, trend={}, value_area={}, leverage={})
    assert ts.risk_profile(st2, "1d")["stop_atr_multiple"] == 1.5


def test_position_pct_capped_and_floored():
    """止损距离换算敞口的上下限仍在（0% ~ 100%）。"""
    st = _state(volatility={"atr_pct": 0.2}, trend={}, value_area={}, leverage={})
    r = ts.risk_profile(st, "1d")
    assert 0 < r["position_pct"] <= 100.0


def test_sizing_evidence_is_deep_copied():
    """v486：返回的是深拷贝。调用方改返回值不得污染后续读数。"""
    import technical_state as ts
    r1 = ts.risk_profile({"price": 100.0, "timeframe": "1d"}, timeframe="1d")
    ev1 = r1["sizing_evidence"]
    ev1["verdict"] = "hacked"
    ev1["rule"] = "hacked"
    r2 = ts.risk_profile({"price": 100.0, "timeframe": "1d"}, timeframe="1d")
    assert r2["sizing_evidence"]["verdict"] == "not_supported"
    assert "时间外" in r2["sizing_evidence"]["rule"]
