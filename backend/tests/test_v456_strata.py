# -*- coding: utf-8 -*-
"""v456：品种类别 + 波动状态的分层放行。

分层最容易出的错不是算错，而是"挑出来最好看的那个格子就当规律"。所以这里的
测试一半在测口径（档位算得对不对、因果不因果），一半在测闸门
（样本不够 / 多重比较没过 / 时间外翻车的格子**不能**接管线上周期）。
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
sys.path.insert(0, str(BACKEND / "scripts"))

import refresh_tech_actionable as rta
import tech_signal_bridge as tsb
import tech_vol_regime as tvr


# ── 口径 ─────────────────────────────────────────────────────────────────
def test_asset_class_mapping():
    assert tvr.asset_class("BTC") == "crypto"
    assert tvr.asset_class("XAU") == "metal"
    assert tvr.asset_class("WTI") == "energy"
    assert tvr.asset_class("SPX") == "index"
    assert tvr.asset_class("000660.KS") == "equity"
    assert tvr.asset_class("SOMETHING_NEW") == "other"


def test_vol_bucket_high_after_range_expansion():
    """先 200 根窄幅、再 20 根宽幅：最后几根必须落在 high 档。"""
    bars = []
    ts = 1_700_000_000
    for i in range(210):
        close = 100.0
        rng = 0.3 if i < 190 else 6.0          # 后 20 根振幅突然放大
        bars.append((ts + i * 86400, close + rng, close - rng, close))
    series = tvr.vol_bucket_series(bars)
    assert series[bars[-1][0]] == "high"
    # 历史不足 120 根之前不给档位（不能拿没算出来的分位充当中档）
    assert bars[100][0] not in series


def test_vol_bucket_is_causal():
    """把未来的 bar 删掉，同一根 ts 的档位不能变。"""
    bars = []
    ts = 1_700_000_000
    for i in range(230):
        rng = 0.3 if i < 200 else 4.0
        bars.append((ts + i * 86400, 100 + rng, 100 - rng, 100.0))
    full = tvr.vol_bucket_series(bars)
    cut = tvr.vol_bucket_series(bars[:205])
    for t in (bars[150][0], bars[204][0]):
        assert full[t] == cut[t]


# ── 闸门 ─────────────────────────────────────────────────────────────────
ALLOW_ENABLED = {
    "by_horizon": {"5": {"bull": ["rsi_oversold"], "bear": ["kdj_overbought"]}},
    "strata": {"enabled": True, "cells": [
        {"dim": "vol", "key": "high", "factor": "kdj_overbought",
         "direction": "bear", "horizon": 10}]},
}


def test_strata_overrides_global_horizon():
    """全局 5 日放行 kdj_overbought，但高波动状态下应按 10 日结算。"""
    items = [{"k": "kdj_overbought", "w": 1.0}]
    assert tsb.resolve_horizon(items, ALLOW_ENABLED, cls="crypto", vol="high") == 10
    assert tsb.resolve_horizon(items, ALLOW_ENABLED, cls="crypto", vol="mid") == 5


def test_strata_off_when_disabled_or_unknown():
    items = [{"k": "kdj_overbought", "w": 1.0}]
    disabled = json.loads(json.dumps(ALLOW_ENABLED))
    disabled["strata"]["enabled"] = False
    assert tsb.strata_horizon(items, disabled, "crypto", "high") is None
    # 档位算不出来（历史不足）不猜，退回全局口径
    assert tsb.strata_horizon(items, ALLOW_ENABLED, "crypto", None) is None


def test_strata_class_and_both_dims():
    items = [{"k": "kdj_overbought", "w": 1.0}]
    allow = {"strata": {"enabled": True, "cells": [
        {"dim": "class", "key": "equity", "factor": "kdj_overbought", "horizon": 3},
        {"dim": "both", "key": "crypto/high", "factor": "kdj_overbought", "horizon": 10}]}}
    assert tsb.strata_horizon(items, allow, "equity", "low") == 3
    assert tsb.strata_horizon(items, allow, "crypto", "high") == 10
    assert tsb.strata_horizon(items, allow, "crypto", "low") is None


# ── 生成端的三道闸 ────────────────────────────────────────────────────────
def _row(**kw):
    base = {"group": "stratum", "dim": "vol", "key": "high", "name": "kdj_overbought",
            "direction": rta.BEAR_WORD, "horizon": 10, "n": 329, "hit_rate": 58.97,
            "base_hit": 45.72, "edge": 2.625, "holdout_n": 100, "holdout_hit": 55.0,
            "holdout_base_hit": 40.0, "holdout_edge": 1.74}
    base.update(kw)
    return base


def _ledger(*rows):
    return {"generated_at": "2026-11-18", "signals_used": 7253,
            "strata": {"cells_with_sample": 10, "cells_passing_5pct": 3,
                       "expected_by_chance_5pct": 2.0, "max_z": 4.82,
                       "bonferroni_z": 2.0},
            "rows": list(rows)}


def test_strata_rejects_thin_sample():
    st = rta._build_strata(_ledger(_row(n=150)))
    assert st["candidates"] == [] and st["enabled"] is False


def test_strata_rejects_holdout_fade():
    """全样本漂亮、时间外均值超额塌了的格子不能放行。"""
    st = rta._build_strata(_ledger(_row(holdout_edge=0.2)))
    assert len(st["candidates"]) == 1 and st["enabled"] is False


def test_strata_accepts_all_gates():
    st = rta._build_strata(_ledger(_row()))
    assert st["enabled"] is True
    assert st["cells"][0]["factor"] == "kdj_overbought"
    assert st["cells"][0]["horizon"] == 10


def test_strata_rejects_weak_hit_edge_outside_sample():
    """时间外命中率超额不足 5pt：方向都不稳，均值过线也不能放行。"""
    st = rta._build_strata(_ledger(_row(holdout_hit=42.0, holdout_base_hit=40.0)))
    assert st["enabled"] is False
