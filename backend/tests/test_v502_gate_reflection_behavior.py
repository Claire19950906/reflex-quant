#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v502：门卫反思的行为测试（review/gate_reflection 从 16% 起步）。

这块是"我为什么不下这一单"的自证材料：正向看放行后赚没赚，反向看
异动发生时门卫有没有放行相关新闻（漏检率）。两个口径最容易出错的地方
是"无结果"被当成"判错"，以及 recall 把"窗口里根本没新闻"算成漏检。
"""
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

from review import gate_reflection as gr


# ── 判对/判错/无结果 ─────────────────────────────────────────
def test_unsettled_is_none_not_false():
    assert gr._judge_correct({"settled": 0, "is_correct": "LOSS"}) is None
    assert gr._judge_correct({"settled": "0"}) is None


def test_explicit_win_loss_wins_over_pnl():
    # 显式 WIN/LOSS 优先：即使 pnl 符号相反也听标签的
    assert gr._judge_correct({"settled": 1, "is_correct": "WIN", "forward_pnl": -3.0}) is True
    assert gr._judge_correct({"settled": 1, "is_correct": "loss", "forward_pnl": 3.0}) is False


def test_pnl_sign_is_fallback():
    assert gr._judge_correct({"settled": 1, "forward_pnl": 1.2}) is True
    assert gr._judge_correct({"settled": 1, "forward_pnl": -0.8}) is False


def test_zero_pnl_is_no_result():
    assert gr._judge_correct({"settled": 1, "forward_pnl": 0.0}) is None


def test_garbage_settled_does_not_crash():
    assert gr._judge_correct({"settled": "abc", "forward_pnl": 1.0}) is None


# ── 窗口参数 ─────────────────────────────────────────────────
def test_cutoff_is_clamped():
    now = int(time.time())
    # 口径：传 0/None 视为"没填"，默认 3 天（不是 1 天）
    assert now - gr._cutoff_ts(0) == pytest.approx(3 * 86400, abs=5)
    assert now - gr._cutoff_ts(500) == pytest.approx(90 * 86400, abs=5)  # 500 → 90 天


def test_reverse_window_is_30_minutes():
    assert gr.REVERSE_WINDOW_S == 1800


def test_pass_verdicts_include_tradable_and_look_only():
    assert "可交易" in gr._PASS_VERDICTS and "只能看" in gr._PASS_VERDICTS
    assert "排除" not in gr._PASS_VERDICTS


# ── 反向推：漏检与覆盖 ───────────────────────────────────────
def test_reverse_marks_covered_when_passed_news_exists(monkeypatch):
    monkeypatch.setattr(gr, "_news_in_window", lambda asset, lo, hi: [
        {"news_id": 1, "verdict": "可交易", "content": "x", "strength": 5, "core_impact": ""},
        {"news_id": 2, "verdict": "排除", "content": "y", "strength": 1, "core_impact": ""},
    ])
    out = gr._build_reverse("BTC", [{"ts": 1000, "pct_change": 2.1, "trigger_type": "涨幅"}])
    assert len(out) == 1
    row = out[0]
    assert row["covered"] is True and row["miss"] is False
    assert row["passed_count"] == 1 and row["excluded_count"] == 1
    assert row["pct_change"] == 2.1


def test_reverse_marks_miss_when_only_excluded(monkeypatch):
    monkeypatch.setattr(gr, "_news_in_window", lambda asset, lo, hi: [
        {"news_id": 2, "verdict": "排除", "content": "y", "strength": 1, "core_impact": ""},
    ])
    out = gr._build_reverse("BTC", [{"ts": 1000}])
    assert out[0]["covered"] is False and out[0]["miss"] is True


def test_reverse_skips_abnormal_without_ts(monkeypatch):
    called = []
    monkeypatch.setattr(gr, "_news_in_window", lambda *a: called.append(a) or [])
    out = gr._build_reverse("BTC", [{"ts": 0}, {"pct_change": 1.0}])
    assert out == [] and called == []


def test_reverse_with_no_abnormals_is_empty():
    assert gr._build_reverse("BTC", None) == []


# ── 汇总口径 ─────────────────────────────────────────────────
def _fwd(correct, did=1, action="BUY", verdict="可交易", pnl=1.0):
    return {"decision_id": did, "news_id": did, "verdict": verdict, "strength": 3,
            "core_impact": "", "main_asset": "BTC", "kind": "", "action": action,
            "settled": 1 if correct is not None else 0, "correct": correct,
            "is_correct": "", "forward_pnl": pnl, "mfe_pct": 1.0, "mae_pct": -1.0,
            "content": "x"}


def test_summary_precision_counts_only_decided(monkeypatch):
    monkeypatch.setattr(gr, "_build_forward", lambda asset, days: [
        _fwd(True, 1), _fwd(True, 2), _fwd(False, 3), _fwd(None, 4)])
    monkeypatch.setattr(gr, "_build_reverse", lambda asset, abnormals: [])
    r = gr.gate_reflection("btc", days=3)
    s = r["summary"]
    assert s["n_judged"] == 4 and s["n_decided"] == 3
    assert s["n_win"] == 2 and s["n_loss"] == 1
    assert s["precision"] == pytest.approx(66.7)
    assert s["pass_winrate"] == s["precision"]
    assert r["asset"] == "BTC"


def test_summary_recall_none_when_no_abnormals(monkeypatch):
    monkeypatch.setattr(gr, "_build_forward", lambda asset, days: [])
    monkeypatch.setattr(gr, "_build_reverse", lambda asset, abnormals: [])
    r = gr.gate_reflection("BTC", days=3)
    assert r["summary"]["precision"] is None
    assert r["summary"]["recall"] is None
    assert r["summary"]["n_abnormal"] == 0


def test_summary_recall_math(monkeypatch):
    rev = [{"ts": 1, "covered": True, "miss": False}, {"ts": 2, "covered": False, "miss": True},
           {"ts": 3, "covered": True, "miss": False}, {"ts": 4, "covered": False, "miss": True}]
    monkeypatch.setattr(gr, "_build_forward", lambda asset, days: [_fwd(True)])
    monkeypatch.setattr(gr, "_build_reverse", lambda asset, abnormals: rev)
    r = gr.gate_reflection("BTC", days=7)
    assert r["summary"]["n_abnormal"] == 4
    assert r["summary"]["n_covered"] == 2 and r["summary"]["n_miss"] == 2
    assert r["summary"]["recall"] == pytest.approx(50.0)
    assert r["days"] == 7


def test_days_is_clamped_in_output(monkeypatch):
    monkeypatch.setattr(gr, "_build_forward", lambda asset, days: [])
    monkeypatch.setattr(gr, "_build_reverse", lambda asset, abnormals: [])
    assert gr.gate_reflection("BTC", days=0)["days"] == 3      # 0 视为未填 → 默认 3
    assert gr.gate_reflection("BTC", days=999)["days"] == 90


def test_asset_is_uppercased(monkeypatch):
    seen = {}
    monkeypatch.setattr(gr, "_build_forward",
                        lambda asset, days: seen.setdefault("a", asset) and [])
    monkeypatch.setattr(gr, "_build_reverse", lambda asset, abnormals: [])
    gr.gate_reflection("eth", days=3)
    assert seen["a"] == "ETH"
