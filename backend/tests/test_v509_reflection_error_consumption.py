#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v509：错误码的消费端行为测试（routers/reflection 的错误码聚合）。

构建端（注册表 43 个码）已经测过，这轮测的是"码被打上之后怎么用"：
  ① 各码出现次数、来源（checker/llm）、严重度必须分别聚合，不能混一起；
  ② 子原因（E001/a-j）要按 evidence_refs.sub_kind 展开，JSON 字符串也要能解；
  ③ 「这些错误码让我亏了多少」必须只用已结算且有 pnl 的决策算，
     未结算 / pnl 为空的决策不许稀释均值。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import db
import routers.reflection as rf
from review import reflection_checkers as rc

R = rf._reflection_recent_sync


@pytest.fixture()
def fake(monkeypatch):
    """用临时库替换生产库，并接管错误列表与跨资产快照。"""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, settled INTEGER, forward_pnl REAL);
        CREATE TABLE decision_errors(decision_id INTEGER, error_code TEXT, origin TEXT,
                                     severity INTEGER, evidence_refs TEXT);
    """)
    monkeypatch.setattr(db, "get_connection", lambda: c, raising=False)
    monkeypatch.setattr(rc, "format_errors_for_prompt", lambda errs: f"ERR:{len(errs)}")
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [], raising=False)
    import cross_asset_context as cac
    monkeypatch.setattr(cac, "cross_asset_snapshot",
                        lambda asset, ts_iso=None: {"anchor_ts": ts_iso, "asset": asset,
                                                    "macro_regime": "neutral",
                                                    "risk_off_count": 2, "risk_on_count": 3,
                                                    "total_symbols": 6, "warning": "",
                                                    "pct_only": False}, raising=False)
    return c


def _err(code="E003", origin="checker", severity=2, refs=None, created="2026-11-20 10:00:00"):
    return {"error_code": code, "origin": origin, "severity": severity,
            "evidence_refs": refs, "created_at": created, "detail": "d"}


# ── 聚合：码 / 来源 / 严重度 ───────────────────────────────
def test_code_origin_severity_stats(fake, monkeypatch):
    errs = [_err("E003", "checker", 2), _err("E003", "llm", 3),
            _err("E011", "checker", 1)]
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: errs, raising=False)
    out = R(limit=50, code=None, origin=None)
    assert out["total"] == 3
    assert out["code_stats"] == {"E003": 2, "E011": 1}
    assert out["origin_stats"]["checker"] == 2 and out["origin_stats"]["llm"] == 1
    assert out["severity_stats"]["1"] == 1 and out["severity_stats"]["2"] == 1
    assert out["severity_stats"]["3"] == 1
    assert out["prompt_preview"] == "ERR:3"


def test_origin_filter_applied_locally(fake, monkeypatch):
    errs = [_err("E003", "checker"), _err("E009", "llm")]
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: errs, raising=False)
    out = R(limit=50, code=None, origin="llm")
    assert out["total"] == 1 and out["code_stats"] == {"E009": 1}
    assert out["origin_filter"] == "llm"


def test_code_filter_is_forwarded(monkeypatch, fake):
    seen = {}
    def _get(error_codes=None, limit=50):
        seen["codes"] = error_codes
        return []
    monkeypatch.setattr(rc, "get_recent_errors", _get, raising=False)
    R(limit=5, code="E011", origin=None)
    assert seen["codes"] == ["E011"], "按码过滤要下推给查询层，不能全量拉回再筛"


# ── 子原因展开：dict 与 JSON 字符串都要能吃 ─────────────────
def test_subkind_counts_dict_and_json_string(fake, monkeypatch):
    errs = [
        _err("E001", refs={"sub_kind": "a"}),
        _err("E001", refs=json.dumps({"sub_kind": "a"})),
        _err("E001", refs={"sub_kind": "c"}),
        _err("E001", refs=None),
        _err("E001", refs="not-json"),
    ]
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: errs, raising=False)
    out = R(limit=50, code=None, origin=None)
    assert out["code_subkinds"] == {"E001/a": 2, "E001/c": 1}, "没子原因的不计数，也不许崩"


# ── 错误码 → 亏损影响 ─────────────────────────────────────
def test_pnl_impact_uses_only_settled_with_pnl(fake, monkeypatch):
    c = fake
    c.execute("INSERT INTO ai_decisions VALUES(1,1,-4.0),(2,1,-6.0),(3,0,NULL),(4,1,NULL)")
    for did, code in ((1, "E003"), (2, "E003"), (3, "E003"), (4, "E003")):
        c.execute("INSERT INTO decision_errors VALUES(?,?,?,?,?)", (did, code, "checker", 2, None))
    c.commit()
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [], raising=False)

    out = R(limit=50, code=None, origin=None)
    imp = {r["code"]: r for r in out["code_pnl_impact"]}["E003"]
    assert imp["n_errors"] == 4
    # 已结算但 pnl 缺失的那条仍算「已结算」（状态是真的），只是不进均值
    assert imp["n_settled"] == 3, "未结算的不计入；已结算但缺 pnl 的仍计入"
    assert imp["avg_pnl"] == pytest.approx(-5.0), "-4 与 -6 的均值，空 pnl 不参与"
    assert imp["total_pnl"] == pytest.approx(-10.0)
    assert imp["win_rate_when_coded"] == pytest.approx(0.0)
    assert imp["worst_pnl"] == pytest.approx(-6.0) and imp["best_pnl"] == pytest.approx(-4.0)


def test_decision_tagged_by_two_codes_counts_in_both(fake, monkeypatch):
    """一条决策可能既"追高"又"证据不足"：两个码都要认账，不能只算一个。"""
    c = fake
    c.execute("INSERT INTO ai_decisions VALUES(1,1,-3.0),(2,1,2.0)")
    c.execute("INSERT INTO decision_errors VALUES(1,'E003','checker',2,NULL)")
    c.execute("INSERT INTO decision_errors VALUES(1,'E011','llm',3,NULL)")
    c.execute("INSERT INTO decision_errors VALUES(2,'E011','llm',1,NULL)")
    c.commit()
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [], raising=False)
    out = R(limit=50, code=None, origin=None)
    imp = {r["code"]: r for r in out["code_pnl_impact"]}
    assert imp["E003"]["n_errors"] == 1 and imp["E003"]["total_pnl"] == pytest.approx(-3.0)
    assert imp["E011"]["n_errors"] == 2
    assert imp["E011"]["total_pnl"] == pytest.approx(-1.0), "-3 + 2"
    assert imp["E011"]["win_rate_when_coded"] == pytest.approx(0.5)


def test_pnl_impact_sorted_worst_first(fake, monkeypatch):
    c = fake
    c.execute("INSERT INTO ai_decisions VALUES(1,1,-8.0),(2,1,1.0)")
    c.execute("INSERT INTO decision_errors VALUES(1,'E020','checker',2,NULL)")
    c.execute("INSERT INTO decision_errors VALUES(2,'E005','checker',2,NULL)")
    c.commit()
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [], raising=False)
    out = R(limit=50, code=None, origin=None)
    assert [r["code"] for r in out["code_pnl_impact"]] == ["E020", "E005"], "亏得最多的排前面"


def test_pnl_impact_failure_does_not_break_reflection(monkeypatch, fake):
    """统计 SQL 挂了，反思本身必须照常返回 —— 不能因为报表坏了就看不到错误。"""
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [_err()], raising=False)

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_connection", _boom, raising=False)
    out = R(limit=50, code=None, origin=None)
    assert out["total"] == 1
    assert out["code_pnl_impact"] == []


def test_cross_asset_snapshot_is_trimmed(fake, monkeypatch):
    """跨资产快照要塞进 prompt，必须裁到几个字段，不能整包灌进去。"""
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [_err()], raising=False)
    out = R(limit=50, code=None, origin=None)
    snap = out["cross_asset_snapshot"]
    assert set(snap) == {"anchor_ts", "asset", "macro_regime", "risk_off_count",
                         "risk_on_count", "total_symbols", "warning", "pct_only"}


def test_cross_asset_failure_is_reported_not_swallowed(monkeypatch, fake):
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [_err()], raising=False)
    import cross_asset_context as cac
    def _boom(*a, **k):
        raise RuntimeError("no bars")
    monkeypatch.setattr(cac, "cross_asset_snapshot", _boom, raising=False)
    out = R(limit=50, code=None, origin=None)
    assert "error" in out["cross_asset_snapshot"]


# ── 聚类委派 ──────────────────────────────────────────────
def test_clustering_delegates_and_closes_conn(monkeypatch, fake):
    calls = {}
    def _cluster(days, min_count, limit_per_group, conn):
        calls.update(days=days, min_count=min_count, limit=limit_per_group)
        return {"groups": []}
    monkeypatch.setattr(rc, "cluster_failures", _cluster, raising=False)
    monkeypatch.setattr(rc, "_connect", lambda: fake, raising=False)
    out = rf._reflection_clustering_sync(days=7, min_count=3, limit_per_group=5)
    assert out == {"groups": []}
    assert calls == {"days": 7, "min_count": 3, "limit": 5}


def test_unparseable_created_at_falls_back_to_now(fake, monkeypatch):
    """时间戳格式不对（NULL / 带 T / 空串）不能让反思接口 500。"""
    captured = {}

    def _snap(asset, ts_iso=None):
        captured["ts"] = ts_iso
        return {"anchor_ts": ts_iso, "asset": asset, "macro_regime": "neutral",
                "risk_off_count": 0, "risk_on_count": 0, "total_symbols": 0,
                "warning": "", "pct_only": False}

    import cross_asset_context as cac
    monkeypatch.setattr(cac, "cross_asset_snapshot", _snap, raising=False)
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [_err(created=None)], raising=False)
    out = R(limit=50, code=None, origin=None)
    assert out["total"] == 1
    assert captured["ts"], "解析失败要退到当前时间，而不是空值"
    assert out["cross_asset_snapshot"]["asset"] == "BTC"


def test_pnl_table_missing_degrades_to_empty_list(fake, monkeypatch):
    """报表表缺失时只降级这一段，反思结果照常返回。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(db, "get_connection", lambda: c, raising=False)
    monkeypatch.setattr(rc, "get_recent_errors",
                        lambda error_codes=None, limit=50: [_err()], raising=False)
    out = R(limit=50, code=None, origin=None)
    assert out["total"] == 1
    assert out["code_pnl_impact"] == []
