# -*- coding: utf-8 -*-
"""Trident 2.0 功能层回归测试（64 项）。

覆盖四层：
1. 目录层：64 项 id 唯一、优先级合法、保护名单一致；
2. 注册层：64 项都有实现，缺模块也不抛异常；
3. 类型层：FeatureResult 的「ok 必须有值 / 非 ok 不能带值」硬约束；
4. 运行层：空库（刚 migrate）上跑全部功能不崩、状态合法、结果可落库。

不依赖真实库：conftest 已把 TRIDENT_DB_PATH 指向临时库。
"""
from __future__ import annotations

import sqlite3

import pytest

import trident2
from trident2 import catalog, pipeline, registry, store
from trident2.context import Context
from trident2.features import monitor, research
from trident2.types import STATUS_ERROR, STATUS_NO_DATA, FeatureResult, SEVERITY_TO_LEVEL


@pytest.fixture
def t2db(fresh_db):
    store.ensure_schema(fresh_db)
    return fresh_db


# ── 1. 目录 ─────────────────────────────────────────────────────────────
def test_catalog_has_64_unique_features():
    assert len(catalog.FEATURES) == 64
    ids = [f.id for f in catalog.FEATURES]
    assert len(set(ids)) == 64
    assert ids[0] == "F01" and ids[-1] == "F64"


def test_catalog_priority_and_batches():
    valid = {"P0", "P1", "P2"}
    for f in catalog.FEATURES:
        assert f.priority in valid, f"{f.id} 优先级非法: {f.priority}"
        assert f.name and f.what, f"{f.id} 缺名称或说明"
    assert len([f for f in catalog.FEATURES if f.priority == "P0"]) >= 8


def test_catalog_summary_counts():
    s = catalog.summary()
    assert s["total"] == 64
    assert s["P0"] + s["P1"] + s["P2"] == 64
    assert s["P0"] >= 8


def test_protected_features_are_known():
    for fid in ("F05", "F08", "F09", "F17"):
        assert catalog.is_protected(fid), f"{fid} 应在保护名单中"


# ── 2. 注册 ─────────────────────────────────────────────────────────────
def test_registry_implements_all_64():
    registry.load_all()
    assert len(registry.implemented()) == 64
    assert registry.missing() == []


def test_unknown_feature_returns_not_configured(t2db):
    r = pipeline.run_one(t2db, "F99")
    assert r.status == "not_configured"
    assert any("F99" in n for n in r.notes), r.notes


# ── 3. 结果类型契约 ─────────────────────────────────────────────────────
def test_feature_result_ok_requires_value():
    with pytest.raises(ValueError):
        FeatureResult(feature_id="F01", status="ok", headline="x", value={})


def test_feature_result_non_ok_forbids_value():
    with pytest.raises(ValueError):
        FeatureResult(feature_id="F01", status="no_data", headline="x", value={"a": 1})


def test_feature_result_rejects_bad_status():
    with pytest.raises(ValueError):
        FeatureResult(feature_id="F01", status="done", headline="x", value={"a": 1})


def test_severity_mapping_is_declared():
    assert SEVERITY_TO_LEVEL["critical"] == "P1"
    assert SEVERITY_TO_LEVEL["warn"] == "P2"
    assert SEVERITY_TO_LEVEL["info"] == "P3"


# ── 4. 建表 / 落库 ──────────────────────────────────────────────────────
def test_ensure_schema_is_idempotent(t2db):
    store.ensure_schema(t2db)
    store.ensure_schema(t2db)
    tables = {r[0] for r in t2db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't2_%'")}
    expected = {"t2_results", "t2_alerts", "t2_state", "t2_param_changes", "t2_account",
                "t2_positions", "t2_orders", "t2_interventions", "t2_cost_ledger",
                "t2_decision_snapshots", "t2_eval_set", "t2_source_scores",
                "t2_subaccounts", "t2_anomaly_snapshots", "t2_event_backfill"}
    assert expected <= tables
    assert store.account(t2db)["mode"] == "paper"


def test_record_result_and_latest(t2db):
    r = FeatureResult.ok("F03", "巡检", {"ready": True}, sample_size=6)
    store.record_result(t2db, r)
    latest = store.latest_by_feature(t2db)
    assert "F03" in latest
    assert latest["F03"]["status"] == "ok"


def test_alert_dedupe_by_feature_and_key(t2db):
    r1 = FeatureResult.ok("F25", "流动性恶化", {"x": 1}, severity="warn")
    a1 = store.emit_alert(t2db, r1, dedupe_key="BTC")
    a2 = store.emit_alert(t2db, r1, dedupe_key="BTC")
    assert a1 and a1["level"] == "P2"
    assert a2 is None, "同一 feature+dedupe_key 在冷却窗口内不应重复发告警"
    assert len(store.alerts(t2db)) == 1


# ── 5. 全量跑通（空库） ─────────────────────────────────────────────────
def test_all_features_run_without_crash(t2db):
    results = pipeline.run_all(t2db)
    assert len(results) == 64
    errors = [r for r in results if r.status == STATUS_ERROR]
    assert not errors, [(r.feature_id, r.evidence.get("error")) for r in errors]
    for r in results:
        assert r.status in ("ok", "no_data", "not_configured")
        if r.status == "ok":
            assert r.value, f"{r.feature_id} status=ok 却没有值"


def test_run_persists_results(t2db):
    pipeline.run_one(t2db, "F03")
    rows = store.recent_results(t2db, "F03", limit=1)
    assert rows and rows[0]["feature_id"] == "F03"


# ── 6. 关键功能的语义约定 ───────────────────────────────────────────────
def test_f17_parses_amend_command_without_executing(t2db):
    r = pipeline.run_one(t2db, "F17", text="把 BTC 止损提到 62000")
    assert r.status == "ok"
    draft = r.value
    assert draft["asset"] == "BTC"
    assert draft["action"] == "stop_loss"
    assert draft["price"] == 62000
    assert draft["dry_run"] is True and draft["requires_second_confirm"] is True


def test_f17_without_text_is_not_configured(t2db):
    r = pipeline.run_one(t2db, "F17")
    assert r.status == "not_configured"
    assert r.value == {}


def test_f01_window_without_news_is_no_data(t2db):
    r = pipeline.run_one(t2db, "F01", window_hours=1)
    assert r.status == "no_data"


def test_f05_stop_the_bleeding_defaults_to_dry_run(t2db):
    r = pipeline.run_one(t2db, "F05")
    assert r.status in ("ok", "no_data")
    if r.status == "ok":
        assert r.value.get("mode") in ("dry_run", "paper")
        assert "plan" in r.value


def test_f48_hypothetical_order_bounds(t2db):
    r = pipeline.run_one(t2db, "F48")
    assert r.status in ("ok", "no_data")
    if r.status == "ok":
        for row in r.value["assets"]:
            assert row["hypo_price"] < row["spot"], "限价买单价必须低于现价"


def test_pipeline_records_errors_instead_of_raising(t2db, monkeypatch):
    def _boom(ctx, **kw):
        raise RuntimeError("故意炸")

    monkeypatch.setitem(registry._REGISTRY, "F03", _boom)
    r = pipeline.run_one(t2db, "F03")
    assert r.status == STATUS_ERROR
    assert "故意炸" in r.evidence.get("error", "")


# ── 7. 路由 ─────────────────────────────────────────────────────────────
def test_router_exposes_expected_paths():
    from routers import trident2 as t2_router
    paths = {r.path for r in t2_router.router.routes}
    for p in ("/api/trident2/features", "/api/trident2/status", "/api/trident2/run/{feature_id}",
              "/api/trident2/run", "/api/trident2/alerts", "/api/trident2/nl-amend"):
        assert p in paths, f"缺少路由 {p}"


# ── 8. 工具函数 ─────────────────────────────────────────────────────────
def test_research_at_helper_tolerates_gap():
    bars = [{"ts": 100, "close": 1.0}, {"ts": 4000, "close": 2.0}]
    assert research._at(bars, 200) is None or research._at(bars, 200) == 2.0
    assert research._at(bars, 20000) is None


def test_safe_pct_change_never_fabricates_zero():
    from trident2.types import safe_pct_change
    assert safe_pct_change(None, 10) is None
    assert safe_pct_change(11, 0) is None
    assert safe_pct_change(11, 10) == pytest.approx(10.0)


# ── 9. 补数据链路（回放器 / 纸面样本取数 / F02 观察窗） ────────────────
def _seed_1m_bars(conn, symbol="BTC", start=1_790_000_000, n=40, base=100.0, drift=0.5):
    for i in range(n):
        conn.execute(
            "INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, "1m", (start + i * 60) * 1000, base + i * drift, base + i * drift,
             base + i * drift, base + i * drift, 1.0),
        )
    conn.commit()


def test_replay_settles_with_real_bars_and_charges_cost(t2db):
    from trident2 import replay
    _seed_1m_bars(t2db, "BTC", start=1_790_000_000, n=40, drift=0.5)
    t2db.execute(
        "INSERT INTO news_events (news_id, ts, asset, action, score, is_noise, headline) "
        "VALUES (1, 1790000000, 'BTC', 'BUY', 1.0, 0, 'test')"
    )
    t2db.commit()
    out = replay.replay_news(t2db, limit=1)
    assert out["inserted"] == 1
    row = t2db.execute("SELECT * FROM t2_sim_trades").fetchone()
    assert row["settled"] == 1 and row["cost_bps"] == 30.0
    # 入场=事件后首根收盘 100，出场=+20min 收盘 110；毛收益 10%，扣 0.3% 成本
    assert row["pnl_pct"] == pytest.approx(9.7, abs=0.01)


def test_replay_skips_when_no_bars(t2db):
    from trident2 import replay
    t2db.execute(
        "INSERT INTO news_events (news_id, ts, asset, action, score, is_noise, headline) "
        "VALUES (2, 1790000000, 'NOPE', 'SELL', 1.0, 0, 'test')"
    )
    t2db.commit()
    out = replay.replay_news(t2db, limit=1)
    assert out["inserted"] == 0 and out["skipped"] == 1


def test_replay_never_enters_performance(t2db):
    """v404 隔离：回放样本（t2_sim_trades）不许进入绩效口径，哪怕凑够 25 笔。"""
    for i in range(25):
        t2db.execute(
            "INSERT INTO t2_sim_trades (source, asset, action, confidence, entry_ts, entry_price, "
            "exit_ts, exit_price, pnl_pct, cost_bps, settled) "
            "VALUES ('news_replay','BTC','BUY',1.0,?,?,?,?,?,30.0,1)",
            (1_000_000 + i, 100.0, 1_000_100 + i, 101.0, 0.7),
        )
    t2db.commit()
    ctx = Context(t2db, now=1_000_000 + 100)
    assert len(ctx.replay_trades()) == 25        # 回放仍然可读（供研究）
    assert ctx.replay_count() == 25
    assert ctx.live_trades() == []               # 但不进绩效
    res = pipeline.run_one(t2db, "F45")
    assert res.status == STATUS_NO_DATA
    assert "隔离" in " ".join(res.notes)


def test_f02_reports_observation_window_instead_of_no_data(t2db):
    store.insert_param_change(t2db, scope="account", key="cash", old_json={"cash": 0},
                              new_json={"cash": 10000}, actor="test", note="t")
    res = pipeline.run_one(t2db, "F02")
    assert res.status == "ok"
    assert res.value["window_open"] is True
