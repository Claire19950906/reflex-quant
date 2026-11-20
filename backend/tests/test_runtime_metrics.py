# -*- coding: utf-8 -*-
"""runtime_metrics 聚合测试：隔离临时库，验证空库降级与计数口径。"""
from __future__ import annotations

import sqlite3
import time


def _connect_memory():
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def test_snapshot_empty_db_degrades_gracefully(monkeypatch):
    """空库不应抛错，各分组返回空/0，错误率为 None。"""
    import runtime_metrics as rm
    conn = _connect_memory()
    monkeypatch.setattr(rm, "_connect", lambda: conn)
    snap = rm.snapshot(24)
    assert snap["decisions"]["total"] == 0
    assert snap["decisions"]["by_action"] == {}
    assert snap["llm"]["total"] == 0
    assert snap["llm"]["error_rate"] is None
    # 空库无 t2_positions / 反思表时降级为 None，不抛错
    assert snap["trades"]["open"] is None
    # 闸门表在基线内：空库计数为 0；隔离表缺表时降级为 None
    assert snap["gate"]["total"] == 0
    assert snap["quarantine"]["total"] is None
    conn.close()


def test_snapshot_counts_decisions_and_llm(monkeypatch):
    """插入方向单与 LLM 调用后，聚合口径正确。"""
    import runtime_metrics as rm
    conn = _connect_memory()
    now = int(time.time())
    base = dict(news_id=1, sentiment_score=0.0, reasoning="t", market_category="",
                target_asset="BTC", is_sibling=0, source_stats_counted_ts=0,
                insight_hit_source="", low_impact=0, cold_signal=0)
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, evidence_action, "
        "reasoning, market_category, target_asset, is_sibling, source_stats_counted_ts, "
        "insight_hit_source, low_impact, cold_signal, ts, entry_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (base["news_id"], base["sentiment_score"], "BUY", "BUY", base["reasoning"],
         base["market_category"], base["target_asset"], base["is_sibling"],
         base["source_stats_counted_ts"], base["insight_hit_source"], base["low_impact"],
         base["cold_signal"], now, 100.0))
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, evidence_action, "
        "reasoning, market_category, target_asset, is_sibling, source_stats_counted_ts, "
        "insight_hit_source, low_impact, cold_signal, ts, entry_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, 0.0, "SELL", "HOLD", "t", "", "BTC", 0, 0, "", 0, 0, now, None))
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, evidence_action, "
        "reasoning, market_category, target_asset, is_sibling, source_stats_counted_ts, "
        "insight_hit_source, low_impact, cold_signal, ts, entry_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (3, 0.0, "HOLD", "HOLD", "t", "", "BTC", 0, 0, "", 0, 0, now, None))
    conn.execute(
        "INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id, status, error_code) "
        "VALUES (?, 'test','k','m','ok',''), (?, 'test','k','m','network_error','network'), "
        "(?, 'test','k','m','http_error','429')",
        (now, now, now))
    conn.commit()
    monkeypatch.setattr(rm, "_connect", lambda: conn)
    snap = rm.snapshot(24)
    d = snap["decisions"]
    assert d["total"] == 3
    assert d["directional"] == 2
    assert d["filled"] == 1
    assert d["missing_price"] == 1
    llm = snap["llm"]
    assert llm["total"] == 3
    assert llm["error_rate"] == round(2 / 3, 4)
    conn.close()


def test_snapshot_db_unavailable(monkeypatch):
    """连不上库时返回 error 标记，而不是抛异常。"""
    import runtime_metrics as rm
    monkeypatch.setattr(rm, "_connect", lambda: None)
    snap = rm.snapshot(24)
    assert snap.get("error") == "db_unavailable"


def test_classify_exit_codes():
    """退出状态归类：正常退出 / 信号终止 / 非零退出码。"""
    import runtime_metrics as rm
    assert rm._classify_exit(0)["abnormal"] is False
    sig = rm._classify_exit(-15)
    assert sig["state"] == "signal" and sig["signal_name"] == "SIGTERM" and sig["abnormal"]
    kill = rm._classify_exit(-9)
    assert kill["signal_name"] == "SIGKILL"
    ex = rm._classify_exit(3)
    assert ex["state"] == "exit" and ex["exit_code"] == 3 and ex["abnormal"]


def test_launchd_jobs_parse(monkeypatch):
    """解析 launchctl list：运行中 / 正常退出 / 信号终止 / 非零退出码。"""
    import runtime_metrics as rm

    class _R:
        returncode = 0
        stdout = ("PID\tStatus\tLabel\n"
                  "100\t0\tcom.trident.running\n"
                  "-\t0\tcom.trident.exited_ok\n"
                  "-\t-15\tcom.trident.bridge\n"
                  "-\t3\tcom.trident.master\n"
                  "-\t0\tcom.other.system\n")

    monkeypatch.setattr(rm.subprocess, "run", lambda *a, **k: _R())
    jobs = rm._launchd_jobs()
    assert jobs["available"] is True
    assert jobs["total"] == 4
    labels = {j["label"]: j for j in jobs["jobs"]}
    assert labels["com.trident.running"]["pid"] == 100
    assert labels["com.trident.bridge"]["signal_name"] == "SIGTERM"
    assert labels["com.trident.master"]["exit_code"] == 3
    abnormal = {a["label"] for a in jobs["abnormal"]}
    assert abnormal == {"com.trident.bridge", "com.trident.master"}


def test_launchd_jobs_unavailable(monkeypatch):
    """无 launchctl / 非 macOS 时降级 available=False，不抛错。"""
    import runtime_metrics as rm

    def _raise(*a, **k):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(rm.subprocess, "run", _raise)
    jobs = rm._launchd_jobs()
    assert jobs["available"] is False
    assert jobs["abnormal"] == []
