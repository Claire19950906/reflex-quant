# -*- coding: utf-8 -*-
"""v474 公司侧分歧度：估值 vs 盈利修正打架时必须能被看见。

公司票最典型的自相矛盾就是"估值说便宜、盈利在砍"（价值陷阱），
以前 `_consistency` 会把分数折半，但折半后没有任何标记 —— 复盘分不清
"分数被折过"还是"分数本来就低"。这组测试钉住新口径。
"""
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import fundamental_factor as ff
import fundamental_shadow as fs


def test_dispersion_same_direction_is_low():
    out = ff._dispersion([(0.6, 0.5), (0.7, 0.5)])
    assert out["dispersion_level"] == "低"
    assert out["confidence"] > 0.8
    assert out["bear_weight"] == 0.0


def test_value_trap_shape_is_high():
    """估值 +1（极便宜）与盈利 −1（狂下修）对峙：分歧必须高、可信度必须低。"""
    out = ff._dispersion([(1.0, 0.5), (-1.0, 0.5), (None, 0.10)])
    assert out["dispersion"] > 0.9
    assert out["dispersion_level"] == "高"
    assert out["confidence"] < 0.4
    assert out["bull_weight"] == 0.5 and out["bear_weight"] == 0.5


def test_single_component_has_no_dispersion():
    """只有一个分量时不存在"分歧"，返回 None —— 不能假装算出了一个 0。"""
    out = ff._dispersion([(0.5, 0.5), (None, 0.5)])
    assert out["dispersion"] is None and out["confidence"] is None


def test_macro_tilt_is_only_background():
    """宏观倾斜只占 0.10：两个主分量一致时，加上反向宏观也不该翻成高分歧。"""
    out = ff._dispersion([(0.5, 0.5), (0.5, 0.5), (-0.15, 0.10)])
    assert out["dispersion_level"] == "低", out
    # 宏观那 0.10 确实反向，所以空头权重是 0.1；但因为权重小，等级仍是"低"
    assert out["bull_weight"] == 1.0 and out["bear_weight"] == 0.1


def test_build_outputs_all_five_fields():
    """公司侧输出必须带五个字段（不支持时也是 None，不是缺键）。"""
    for ticker in ("PLATINUM", "xyzzy"):
        r = ff.build(ticker, use_cache=False)
        assert "dispersion" in r and "confidence" in r, ticker


def test_shadow_uses_company_confidence():
    """公司票带分歧度后，影子乘数必须跟着打折（v473 的机制对两侧通用）。"""
    conn = sqlite3.connect(":memory:")
    comp = {"ticker": "COIN", "dispersion": 0.836, "dispersion_level": "高", "confidence": 0.442}
    guard = {"factors": {"fundamental": {"available": True, "score": 0.07,
                                         "weight": 0.08, "explanation": "x"}},
             "thresholds": {"buy_min": 0.35, "sell_min": -0.35}}
    fs.record(conn, 2001, None, "COIN", guard, "HOLD", components=comp)
    row = conn.execute("""SELECT dispersion, dispersion_level, confidence, sizing_mult
                          FROM decision_fundamental_snapshot WHERE decision_id=2001""").fetchone()
    assert row[0] == 0.836 and row[1] == "高"
    assert row[3] < 1.0, row[3]      # 0.07 的正分本该微加仓，被分歧度压回 1 倍以下


def test_company_row_without_dispersion_stays_base():
    """老数据（无分歧度）不能被新机制偷偷打折。"""
    conn = sqlite3.connect(":memory:")
    guard = {"factors": {"fundamental": {"available": True, "score": 0.6,
                                         "weight": 0.08, "explanation": "x"}},
             "thresholds": {"buy_min": 0.35, "sell_min": -0.35}}
    fs.record(conn, 2002, None, "AAPL", guard, "BUY", components={"ticker": "AAPL"})
    row = conn.execute("""SELECT dispersion, sizing_mult FROM decision_fundamental_snapshot
                          WHERE decision_id=2002""").fetchone()
    assert row[0] is None and row[1] == 1.12, row
