#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v519：能力记分卡（capability_scorecard）统计层与报告层的行为测试。

记分卡最容易出的错不是算错数，而是"把没证据的当结论"：
  ① 四个状态没有 FAIL：n < 门槛一律"样本不足"，不许下结论；
  ② 达标要求"点估计过线 且 区间下界高于基准"，方向对了但证据不够只能"接近达标"；
  ③ spearman / 净期望是点估计量，区间不参与 PASS；
  ④ share 类（越低越好）不看区间，只看有没有越过线；
  ⑤ 口径变了就不许跟上一次比（delta 直接不给，标"口径变更"）；
  ⑥ 快照写失败只告警、不拖垮整张表。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import capability_scorecard as cs  # noqa: E402


def _metric(**kw):
    base = dict(id="M1", name="测试指标", kind="ratio", target=55.0, gate=10,
                baseline=50.0, higher_better=True, unit="%",
                query=lambda conn: (0.0, 0), definition="定义")
    base.update(kw)
    return cs.Metric(**base)


# ── ① 区间 ───────────────────────────────────────────────────
def test_wilson_interval_is_conservative_at_the_extremes():
    assert cs.wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = cs.wilson_interval(0, 20)
    assert lo == 0.0 and hi < 0.2
    lo, hi = cs.wilson_interval(20, 20)
    assert hi == 1.0 and lo > 0.8
    lo, hi = cs.wilson_interval(11, 19)
    assert 0.33 < lo < 0.579 < hi < 0.8
    # k 超过 n 时按 100% 处理，不许算出 >1
    lo, hi = cs.wilson_interval(25, 20)
    assert hi <= 1.0


def test_definition_hash_tracks_definition_not_data():
    h1 = cs.definition_hash("M1", "ratio", 55, 10, 50, "分母=池内样本")
    assert len(h1) == 12 and h1 == cs.definition_hash(
        "M1", "ratio", 55, 10, 50, "分母=池内样本")
    assert h1 != cs.definition_hash("M1", "ratio", 55, 10, 50, "分母=全库样本")


# ── ② 判定 ───────────────────────────────────────────────────
def test_thin_wins_over_everything():
    assert cs.judge_status("ratio", 90.0, 5, 0.8, 55.0, 10, 50.0, True) == cs.ST_THIN
    assert cs.judge_status("ratio", None, 100, None, 55.0, 10, 50.0, True) == cs.ST_THIN


def test_ratio_pass_needs_both_point_and_lower_bound():
    assert cs.judge_status("ratio", 70.0, 40, 60.0, 55.0, 10, 50.0, True) == cs.ST_GRAD
    assert cs.judge_status("ratio", 70.0, 40, 48.0, 55.0, 10, 50.0, True) == cs.ST_NEAR
    assert cs.judge_status("ratio", 52.0, 40, 45.0, 55.0, 10, 50.0, True) == cs.ST_BELOW
    # 没有区间时只认点估计（老调用方）
    assert cs.judge_status("ratio", 70.0, 40, None, 55.0, 10, 50.0, True) == cs.ST_GRAD


def test_point_estimate_metrics_ignore_interval():
    assert cs.judge_status("spearman", 0.35, 30, 0.1, 0.3, 12, 0.0, True) == cs.ST_GRAD
    assert cs.judge_status("spearman", 0.25, 30, 0.24, 0.3, 12, 0.0, True) == cs.ST_BELOW
    assert cs.judge_status("expectancy", 12.0, 30, None, 5.0, 12, 0.0, True) == cs.ST_GRAD
    assert cs.judge_status("expectancy", -3.0, 30, None, 5.0, 12, 0.0, True) == cs.ST_BELOW


def test_lower_is_better_share_only_checks_the_line():
    assert cs.judge_status("share", 10.0, 40, None, 15.0, 10, 0.0, False) == cs.ST_GRAD
    assert cs.judge_status("share", 20.0, 40, None, 15.0, 10, 0.0, False) == cs.ST_BELOW
    assert cs.judge_status("share", 10.0, 3, None, 15.0, 10, 0.0, False) == cs.ST_THIN


# ── ③ 指标计算 ───────────────────────────────────────────────
def test_ratio_metric_reports_percent_and_wilson():
    conn = sqlite3.connect(":memory:")
    r = cs.evaluate_metric(conn, _metric(query=lambda c: (14, 20)))
    assert r.value == pytest.approx(70.0)
    assert r.n == 20 and r.status in (cs.ST_GRAD, cs.ST_NEAR)
    assert r.ci_low is not None and r.ci_high > r.ci_low
    assert "n=20" in r.detail and r.definition == cs.definition_hash(
        "M1", "ratio", 55.0, 10, 50.0, "定义")


def test_share_and_expectancy_have_no_wilson_band():
    conn = sqlite3.connect(":memory:")
    share = cs.evaluate_metric(conn, _metric(kind="share", query=lambda c: (4, 40),
                                             higher_better=False, target=15.0))
    assert share.ci_low is None and share.ci_high is None
    assert share.value == pytest.approx(10.0)
    exp = cs.evaluate_metric(conn, _metric(kind="expectancy", unit="bps",
                                          query=lambda c: (-7.5, 30), target=5.0))
    assert exp.value == pytest.approx(-7.5)   # 可为负，不能当比例渲染
    assert exp.status == cs.ST_BELOW


def test_metric_query_failure_degrades_to_thin_instead_of_crashing():
    def boom(conn):
        raise sqlite3.OperationalError("no such table")

    r = cs.evaluate_metric(sqlite3.connect(":memory:"), _metric(query=boom))
    assert r.n == 0 and r.value is None and r.status == cs.ST_THIN


def test_ci_fn_overrides_the_wilson_guess():
    conn = sqlite3.connect(":memory:")
    r = cs.evaluate_metric(conn, _metric(query=lambda c: (14, 20),
                                         ci_fn=lambda c: (30.0, 80.0)))
    assert (r.ci_low, r.ci_high) == (30.0, 80.0)
    # ci_fn 自己崩了也不许拖垮指标
    r2 = cs.evaluate_metric(conn, _metric(query=lambda c: (14, 20),
                                         ci_fn=lambda c: (_ for _ in ()).throw(ValueError())))
    assert r2.ci_low is None and r2.status in (cs.ST_GRAD, cs.ST_NEAR)


def test_detail_fn_appends_live_facts_but_survives_failure():
    conn = sqlite3.connect(":memory:")
    r = cs.evaluate_metric(conn, _metric(query=lambda c: (3, 10),
                                         detail_fn=lambda c: "排除 12 条无价样本"))
    assert "排除 12 条无价样本" in r.detail
    r2 = cs.evaluate_metric(conn, _metric(query=lambda c: (3, 10),
                                          detail_fn=lambda c: 1 / 0))
    assert "n=10" in r2.detail


# ── ④ 历史与增量 ─────────────────────────────────────────────
def test_load_history_skips_broken_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    assert cs.load_history(path) == []
    path.write_text('{"metric":"M1","value":10}\n\nnot-json\n{"metric":"M2","value":20}\n',
                    encoding="utf-8")
    recs = cs.load_history(path)
    assert [r["metric"] for r in recs] == ["M1", "M2"]


def test_last_snapshot_takes_the_latest_not_the_first():
    hist = [{"metric": "M1", "value": 1}, {"metric": "M2", "value": 2},
            {"metric": "M1", "value": 3}]
    assert cs.last_snapshot(hist, "M1")["value"] == 3
    assert cs.last_snapshot(hist, "M3") is None


def test_attach_deltas_refuses_to_compare_across_definition_change():
    r = cs.MetricResult(id="M1", name="x", kind="ratio", value=60.0, n=20,
                        ci_low=50.0, ci_high=70.0, target=55.0, gate=10,
                        baseline=50.0, status=cs.ST_GRAD, unit="%",
                        definition="new-hash", detail="")
    hist = [{"metric": "M1", "value": 55.0, "n": 15, "definition": "old-hash"},
            {"metric": "M1", "value": 58.0, "n": 18, "definition": "old-hash"}]
    cs.attach_deltas([r], hist)
    assert r.definition_changed is True
    assert r.delta is None
    assert r.history_n == 2


def test_attach_deltas_computes_value_and_sample_change():
    r = cs.MetricResult(id="M1", name="x", kind="ratio", value=60.0, n=20,
                        ci_low=50.0, ci_high=70.0, target=55.0, gate=10,
                        baseline=50.0, status=cs.ST_GRAD, unit="%",
                        definition="h", detail="")
    cs.attach_deltas([r], [{"metric": "M1", "value": 55.0, "n": 15, "definition": "h"}])
    assert r.delta == pytest.approx(5.0) and r.delta_n == 5
    assert r.definition_changed is False
    # 首次没有可比的记录
    r2 = cs.MetricResult(id="M2", name="y", kind="ratio", value=60.0, n=20,
                         ci_low=None, ci_high=None, target=55.0, gate=10,
                         baseline=50.0, status=cs.ST_GRAD, unit="%",
                         definition="h", detail="")
    cs.attach_deltas([r2], [])
    assert r2.delta is None and r2.history_n == 0


def test_append_snapshot_writes_one_line_per_metric(tmp_path):
    path = tmp_path / "sub" / "h.jsonl"
    rs = [cs.MetricResult(id="M1", name="a", kind="ratio", value=60.0, n=20,
                          ci_low=50.0, ci_high=70.0, target=55.0, gate=10,
                          baseline=50.0, status=cs.ST_GRAD, unit="%",
                          definition="h", detail="")]
    cs.append_snapshot(rs, path)
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["metric"] == "M1" and rec["value"] == 60.0 and rec["n"] == 20
    assert "ts" in rec and rec["definition"] == "h"


def test_append_snapshot_warns_instead_of_crashing(tmp_path, capsys):
    target = tmp_path / "not-a-file"
    target.mkdir()
    cs.append_snapshot([], target)
    assert "快照写入失败" in capsys.readouterr().err


# ── ⑤ 结构层与报告 ───────────────────────────────────────────
def test_structure_verdict_without_payload_is_explicit():
    msg, info = cs.structure_verdict(None)
    assert "无结果" in msg and info == {}


def test_structure_verdict_lists_only_real_failures():
    payload = {"generated_at": "2026-11-21 10:00:00",
               "summary": {"PASS": 18, "FAIL": 1, "WARN": 2, "ERROR": 0, "total": 20},
               "results": [{"id": 14, "name": "端点", "status": "FAIL"},
                           {"id": 15, "name": "实时", "status": "WARN"},
                           {"id": 16, "name": "表", "status": "ERROR"}]}
    msg, info = cs.structure_verdict(payload)
    assert msg == ""
    assert {f["status"] for f in info["fails"]} == {"FAIL", "ERROR"}
    assert info["summary"]["PASS"] == 18
    assert info["generated_at"].startswith("2026-11-21")


def test_print_report_signals_structure_problem_in_exit_code(capsys):
    r = cs.MetricResult(id="M1", name="a", kind="ratio", value=60.0, n=20,
                        ci_low=50.0, ci_high=70.0, target=55.0, gate=10,
                        baseline=50.0, status=cs.ST_GRAD, unit="%",
                        definition="h", detail="d")
    assert cs.print_report([r], ("结构层没有结果", {}), []) == 1
    ok = {"summary": {"PASS": 20}, "fails": [], "generated_at": "now"}
    assert cs.print_report([r], ("", ok), []) == 0
    bad = {"summary": {"PASS": 19}, "fails": [{"id": 7, "name": "n", "status": "FAIL"}],
           "generated_at": "now"}
    assert cs.print_report([r], ("", bad), []) == 1
    out = capsys.readouterr().out
    assert "样本不足" in out and "不判 FAIL" in out


# ── ⑥ 渲染 ───────────────────────────────────────────────────
def test_formatters_never_show_a_fake_zero_for_missing_values():
    r = cs.MetricResult(id="M1", name="a", kind="spearman", value=None, n=0,
                        ci_low=None, ci_high=None, target=0.3, gate=12,
                        baseline=0.0, status=cs.ST_THIN, unit="", definition="h",
                        detail="")
    assert cs._fmt_val(r) == "—"
    assert cs._fmt_ci(r) == "—"
    assert cs._fmt_delta(r) == "首次"
    assert cs._pct(None) == "—" and cs._pt(None) == "—" and cs._bps(None) == "—"
    assert cs._ci((None, 5)) == "—" and cs._ci((1.0, 2.0)) == "[1.0, 2.0]"
    r.value = 0.42
    assert cs._fmt_val(r) == "+0.420"
    r.kind, r.value, r.unit = "expectancy", -7.5, "bps"
    assert cs._fmt_val(r) == "-7.5bps"
    r.kind, r.value, r.unit = "ratio", 62.34, "%"
    assert cs._fmt_val(r) == "62.3%"


def test_fmt_delta_marks_definition_changes_and_signs():
    r = cs.MetricResult(id="M1", name="a", kind="ratio", value=60.0, n=20,
                        ci_low=None, ci_high=None, target=55.0, gate=10,
                        baseline=50.0, status=cs.ST_GRAD, unit="%", definition="h",
                        detail="", delta=3.2, delta_n=4)
    assert cs._fmt_delta(r) == "+3.2% (n+4)"
    r.delta, r.delta_n = -3.2, -4
    assert cs._fmt_delta(r) == "-3.2% (n-4)"
    r.definition_changed = True
    assert cs._fmt_delta(r) == "口径变更"


# ── ⑦ 参考口径 ───────────────────────────────────────────────
def test_reference_metrics_degrade_to_none_on_an_empty_db():
    refs = cs.compute_reference(sqlite3.connect(":memory:"))
    assert refs and all(r["pct"] is None for r in refs)
    assert all({"id", "name", "pct", "num", "den"} <= set(r) for r in refs)
