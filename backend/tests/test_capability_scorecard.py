# -*- coding: utf-8 -*-
"""capability_scorecard 的纯函数测试 (不碰生产库)。

跑法: pytest backend/tests/test_capability_scorecard.py -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_scorecard as cs


def _metric(**kw):
    base = dict(id="X", name="x", kind="ratio", target=55.0, gate=100,
                baseline=50.0, higher_better=True, unit="%",
                query=lambda conn: (0, 0), definition="d")
    base.update(kw)
    return cs.Metric(**base)


def _result(value, n, ci_low, target=55.0, gate=100, baseline=50.0,
            higher_better=True, kind="ratio"):
    return cs.MetricResult(
        id="X", name="x", kind=kind, value=value, n=n,
        ci_low=ci_low, ci_high=None, target=target, gate=gate,
        baseline=baseline, status="", unit="%" if kind != "spearman" else "ρ",
        definition="d", detail="")


# ── Wilson ────────────────────────────────────────────────────────────────
def test_wilson_zero_n():
    assert cs.wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_bounds_and_width():
    lo, hi = cs.wilson_interval(41, 75)
    assert 0.43 < lo < 0.45        # 54.7% 的 95% 下界 ≈ 43.x%
    assert 0.65 < hi < 0.67
    assert cs.wilson_interval(0, 10)[0] == 0.0
    assert cs.wilson_interval(10, 10)[1] == 1.0


def test_wilson_narrows_with_n():
    w_small = cs.wilson_interval(55, 100)
    w_large = cs.wilson_interval(550, 1000)
    assert (w_large[1] - w_large[0]) < (w_small[1] - w_small[0])


# ── 口径指纹 ──────────────────────────────────────────────────────────────
def test_definition_hash_stable_and_sensitive():
    a = cs.definition_hash("S1", "ratio", 55, 200, "sql")
    assert a == cs.definition_hash("S1", "ratio", 55, 200, "sql")
    assert a != cs.definition_hash("S1", "ratio", 60, 200, "sql")
    assert len(a) == 12


# ── 状态判定: 统计层永不 FAIL ─────────────────────────────────────────────
def test_status_thin_when_below_gate():
    assert cs.judge_status("ratio", 54.7, 75, 43.4, 55, 100, 50, True) == cs.ST_THIN


def test_status_grade_needs_ci_lower_above_baseline():
    assert cs.judge_status("ratio", 58.0, 300, 52.4, 55, 100, 50, True) == cs.ST_GRAD
    # 点过线但区间下界没过基准 → 接近达标, 不是 FAIL
    assert cs.judge_status("ratio", 58.0, 300, 49.9, 55, 100, 50, True) == cs.ST_NEAR


def test_status_below():
    assert cs.judge_status("ratio", 48.0, 300, 45.0, 55, 100, 50, True) == cs.ST_BELOW


def test_status_share_lower_better():
    assert cs.judge_status("share", 35.0, 1000, None, 40, 500, 25, False) == cs.ST_GRAD
    assert cs.judge_status("share", 51.4, 1000, None, 40, 500, 25, False) == cs.ST_BELOW


def test_status_spearman():
    assert cs.judge_status("spearman", 0.205, 4345, None, 0.2, 200, 0.0, True) == cs.ST_GRAD
    assert cs.judge_status("spearman", 0.012, 4345, None, 0.2, 200, 0.0, True) == cs.ST_BELOW


def test_status_never_fail():
    for v, n, lo in [(0, 0, None), (10, 500, 8), (99, 500, 98)]:
        st = cs.judge_status("ratio", v, n, lo, 55, 100, 50, True)
        assert st in (cs.ST_GRAD, cs.ST_NEAR, cs.ST_BELOW, cs.ST_THIN)


# ── 快照 / 增量 / 口径变更 ────────────────────────────────────────────────
def test_snapshot_delta_and_definition_change(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    r1 = _result(48.0, 200, 44.0)
    r1.definition = "hashA"
    r1.status = cs.ST_BELOW
    cs.append_snapshot([r1], path)

    hist = cs.load_history(path)
    assert len(hist) == 1 and hist[0]["value"] == 48.0

    r2 = _result(52.0, 260, 47.0)
    r2.definition = "hashA"
    cs.attach_deltas([r2], hist)
    assert r2.delta == 4.0
    assert r2.delta_n == 60

    r3 = _result(52.0, 260, 47.0)
    r3.definition = "hashB"
    cs.attach_deltas([r3], hist)
    assert r3.definition_changed is True
    assert r3.delta is None          # 口径变了不许比


def test_load_history_ignores_broken_lines(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"metric":"S1","value":1}\nnot-json\n\n', encoding="utf-8")
    assert len(cs.load_history(p)) == 1


# ── 指标注册表 ────────────────────────────────────────────────────────────
def test_metrics_registry_shape():
    ids = [m.id for m in cs.METRICS]
    assert len(ids) == len(set(ids))
    for m in cs.METRICS:
        # v490: 新增 expectancy（净期望 bps/笔）—— 这是连续量、可为负，
        # 门槛天然是"＞0"而不是"≥某个正数"，所以 target 允许等于 0。
        if m.kind == "expectancy":
            assert m.target >= 0
        else:
            assert m.gate > 0 and m.target > 0
        assert m.kind in ("ratio", "spearman", "share", "expectancy")
        assert m.definition
