"""v397 回归：硬数据前置注入 + impact_score 阶梯。

两条都是"细节洞察"的原料问题：
  1) hard_data_evidence 只在 LLM 漏填后回填 → 模型看不到数 → 只能写 unknown；
  2) impact_score 停在 5.0（84.9%）→ 影响力维名存实亡（S15 14.2%）。

这里钉住：
  · 证据行/冻结/过期文案逐字稳定（措辞一错，0.00% 就会被读成真盘面）
  · 缓存只省重复读库，不改变内容
  · 阶梯优先级：model > gate(1-100) > gate_strength > default
  · 回填脚本的 plan() 只碰"有外部证据"的行，剩下的必须留 5.0 并计入原因
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import hard_data_block as hdb
import impact_score_ladder as ladder
import backfill_impact_score as bk


# ── 硬数据文案 ────────────────────────────────────────────────────────────

def _bar(ts, o, h, l, c, v=0.0):
    return (ts, o, h, l, c, v)


def test_fresh_bar_has_no_quality_warning():
    now = 1_700_000_000
    block = hdb.build_block("BTC", _bar(now - 60, 65000.0, 65100.0, 64900.0, 65050.0),
                            [], now_ts=now)
    assert block.startswith("BTC ")
    assert "close=65050.0000" in block
    assert "数据质量提示" not in block, "新鲜且有振幅的 K 线不该被标'不作方向依据'"
    assert hdb.is_usable(block)


def test_frozen_bar_is_flagged():
    now = 1_700_000_000
    block = hdb.build_block("NVDA", _bar(now - 60, 10.0, 10.0, 10.0, 10.0),
                            [], now_ts=now)
    assert "该K线无振幅（冻结报价）" in block
    assert "仅作记录，不作方向依据" in block
    assert not hdb.is_usable(block), "冻结报价不能算可用盘面"


def test_stale_bar_is_flagged_with_minutes():
    now = 1_700_000_000
    block = hdb.build_block("XAU", _bar(now - 67 * 60, 3800.0, 3810.0, 3790.0, 3805.0),
                            [], now_ts=now)
    assert "最后更新距今 67 分钟" in block


def test_hour_line_aggregates_from_1m():
    now = 1_700_000_040
    rows = [_bar(now - i * 60, 100.0 + i, 101.0, 99.0, 100.5 + i) for i in range(5)]
    block = hdb.build_block("ETH", rows[0], rows, now_ts=now)
    assert "1H close=" in block
    assert "range=" in block


def test_empty_when_no_bar():
    assert hdb.build_block("AMZN", None, [], now_ts=1_700_000_000) == ""


# ── 缓存 ──────────────────────────────────────────────────────────────────

def test_hard_data_block_uses_cache(monkeypatch):
    from engine import ai_worker as aw
    calls = {"n": 0}

    def _fake_local(asset):
        calls["n"] += 1
        return f"{asset} 15:53 close=1.0000 pct=+0.00% vol=0"

    monkeypatch.setattr(aw, "_build_evidence_from_local", _fake_local)
    monkeypatch.setattr(aw, "_build_evidence_for_asset", lambda *a, **k: "")
    aw._HARD_DATA_BLOCK_CACHE.clear()

    t = 1_700_000_000.0
    a = aw._hard_data_block("BTC", now_ts=t)
    b = aw._hard_data_block("BTC", now_ts=t + 1)
    assert a == b and a
    assert calls["n"] == 1, "同一时间片应命中缓存，不该重复读库"

    c = aw._hard_data_block("BTC", now_ts=t + aw._HARD_DATA_BLOCK_TTL_SEC + 1)
    assert calls["n"] == 2, "跨时间片必须重新取（数据可能已更新）"


def test_hard_data_block_skips_pseudo_assets():
    from engine import ai_worker as aw
    assert aw._hard_data_block("NONE") == ""
    assert aw._hard_data_block("") == ""


# ── impact 阶梯 ───────────────────────────────────────────────────────────

def test_model_value_wins():
    assert ladder.resolve_impact_score(7.5, 90) == (7.5, "model")


def test_gate_score_beats_default():
    assert ladder.resolve_impact_score(5.0, 63) == (6.3, "gate")


def test_gate_strength_used_when_no_1to100():
    assert ladder.resolve_impact_score(5.0, 0, gate_strength=8) == (8.0, "gate_strength")


def test_nothing_available_marks_default():
    assert ladder.resolve_impact_score(None, 0, gate_strength=0) == (None, "unavailable")


def test_clamps_to_0_10():
    assert ladder.resolve_impact_score(99, 0) == (10.0, "model")
    assert ladder.resolve_impact_score(5.0, 100) == (10.0, "gate")


def test_source_labels():
    assert ladder.source_label("gate") == "gate_impact_1to100"
    assert ladder.source_label("default") == "default_unknown"


# ── 回填 plan ─────────────────────────────────────────────────────────────

def test_backfill_plan_only_touches_rows_with_evidence():
    rows = [
        {"id": 1, "gi": 63, "gs": 0, "field_sources": "{}", "target_asset": "XAU"},
        {"id": 2, "gi": 0, "gs": 8, "field_sources": "{}", "target_asset": "WTI"},
        {"id": 3, "gi": 0, "gs": 0, "field_sources": "{}", "target_asset": "AMZN"},
    ]
    todo, why = bk.plan(rows)
    assert [r["id"] for r in todo] == [1, 2]
    assert todo[0]["new_score"] == 6.3 and todo[1]["new_score"] == 8.0
    assert sum(why.values()) == 1, "无证据的行必须留在 5.0，并计入'无法回填'"


def test_merge_field_sources_keeps_existing_keys():
    raw = '{"reasoning": "llm", "impact_score": "rule_default"}'
    out = bk.merge_field_sources(raw, "gate_strength")
    assert '"reasoning": "llm"' in out.replace(",", ", ") or '"reasoning":"llm"' in out
    assert "gate_strength" in out
