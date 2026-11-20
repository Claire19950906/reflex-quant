# -*- coding: utf-8 -*-
"""v417 P0-3 · 同一事件不许扇出成链雨

实测（近 30 天、只读库）：246 个 (事件, 品种) 组共 1222 条候选，
单个事件最多同时命中 39 条链；16 个事件的扇出 ≥20。
后果：一次事件灌进几十行同源样本，统计被稀释；分析时 LLM 收到一堆同义链当独立证据。

收敛口径（两份，必须同时成立）：
  1) 同一事件同一品种最多 MAX_CHAINS_PER_EVENT_ASSET 条；
  2) 同一事件里同一"机制族"只留一条 —— 名字重合率低但讲同一件事的链
     （实测 XAU 那 4 条地缘链字面重合只有 0.2）靠字符串去重是挡不住的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import causal_chain_evaluator as ev
import mechanism_family

GEOPOL_A = "中东冲突升级推升避险与供应风险溢价"
GEOPOL_B = "地缘冲突→黄金避险传导"
GEOPOL_C = "中东地缘冲突升级→黄金避险溢价"
GEOPOL_D = "地缘升级与航运受阻推升黄金风险溢价"
RATES = "美联储加息压制风险偏好"
REG = "监管收紧压制加密资产估值"


def _chain(i, name, asset="XAU"):
    return ev.ChainRow(chain_id=str(i), asset=asset, trigger_keywords=["袭击"],
                       expected_direction="UP", magnitude_lo=0.01, magnitude_hi=0.2,
                       confidence=0.5, name=name)


def _run(monkeypatch, chains):
    calls = []
    monkeypatch.setattr(ev, "load_active_chains", lambda conn, **kw: chains)
    monkeypatch.setattr(
        ev, "find_news_triggers",
        lambda conn, ch, days: [{
            "id": 1, "news_id": 1, "headline": "中东突发袭击", "asset": ch.asset,
            "ts": 1_700_000_000, "action": "", "matched_kws": ["袭击"],
        }],
    )
    monkeypatch.setattr(ev, "chain_horizon_bars", lambda conn, cid: 4)
    monkeypatch.setattr(ev, "insert_pending_outcome",
                        lambda conn, ch, e, d: calls.append(ch.chain_id) or True)
    out = ev.backfill(None, days=30)
    return calls, out


def test_families_collapse_synonyms_that_strings_miss():
    """4 条地缘链字面完全不同，但必须归到同一族（否则入库会各算一遍）。"""
    fams = {mechanism_family.family_of(n) for n in (GEOPOL_A, GEOPOL_B, GEOPOL_C, GEOPOL_D)}
    assert fams == {"geopolitics"}
    assert mechanism_family.family_of(RATES) == "rates"
    assert mechanism_family.family_of(REG) == "regulation"


def test_event_fanout_capped_and_same_family_deduped(monkeypatch):
    """6 条候选（4 条地缘 + 利率 + 监管）→ 只留 3 条，且地缘只留 1 条。"""
    chains = [_chain(i, nm) for i, nm in enumerate(
        [GEOPOL_A, GEOPOL_B, GEOPOL_C, GEOPOL_D, RATES, REG])]
    calls, out = _run(monkeypatch, chains)
    assert len(calls) == ev.MAX_CHAINS_PER_EVENT_ASSET == 3
    assert out["suppressed_same_event"] == 3
    assert out["outcomes_inserted"] == 3
    kept_fams = [mechanism_family.family_of(_chain(0, n).name)
                 for n in [GEOPOL_A, GEOPOL_B, GEOPOL_C, GEOPOL_D, RATES, REG]]
    assert len(set(kept_fams)) == 3        # 恰好三族，可一一保留


def test_different_assets_are_not_capped_together(monkeypatch):
    """XAU 与 WTI 各自留满 3 条，互不挤占（3 族：地缘/利率/监管）。"""
    names = [GEOPOL_A, RATES, REG]
    chains = [_chain(i, nm, "XAU") for i, nm in enumerate(names)] + \
             [_chain(10 + i, nm, "WTI") for i, nm in enumerate(names)]
    calls, out = _run(monkeypatch, chains)
    assert out["outcomes_inserted"] == 6
    assert out["suppressed_same_event"] == 0
