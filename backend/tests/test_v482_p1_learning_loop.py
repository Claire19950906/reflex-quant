# -*- coding: utf-8 -*-
"""v482 第二批：反事实净口径 / 赢单归因 / 教训绑代码代次。

锁住四件"以前是假的"事：
1. 反事实不能把扣费净值取负 —— 反向机械反演要再扣一次往返成本；
2. 反事实要落结构化字段(毛/净/成本), 不能只活在 lesson 文案里；
3. 赢单也要进 LLM 深度归因(否则经验池只有"别做什么")；
4. 经验来源带代次戳, 换代后旧经验在注入侧失效。
"""
from __future__ import annotations

import json
import sqlite3

import db
import paper_reflection_worker as pw
import trade_reflection_llm as trl
import experience_distiller as ed
import experience_reflector as er
import meta_reflection_loop as mr


def _mem_with_hitrate() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE decision_layer_signals("
        "layer, sub_layer, signal_name, asset, n_total, n_win, n_loss)"
    )
    return c


def test_counterfactual_loss_deducts_cost_twice(monkeypatch):
    """ETH 30bp：净 -0.4041 → 反向机械反演净值 -0.1959，不是 +0.4041。"""
    conn = _mem_with_hitrate()
    monkeypatch.setattr(pw.db, "get_connection", lambda: conn)
    rc = pw._classify_root_cause({
        "decision_id": 13093, "target_asset": "ETH", "suggested_action": "BUY",
        "forward_pnl": -0.4041, "is_correct": "LOSS",
        "event_type_code": "etf_flow", "market_confirmation": "unknown",
        "mfe_pct": 0.02, "mae_pct": -0.45,
    })
    assert "机械反演净值≈-0.1959" in rc["lesson"]
    assert "+0.4041" not in rc["lesson"]
    cf = json.loads(rc["counterfactual"])
    assert cf["basis"] == "mechanical"
    assert cf["cost_bps"] == 30
    assert abs(cf["net_rev_pct"] - (-0.1959)) < 1e-6
    assert abs(cf["gross_rev_pct"] - 0.1041) < 1e-6


def test_counterfactual_win_is_negative_after_costs(monkeypatch):
    """赢单 1.5%、ETH 30bp → 反向净值 = -(1.5 + 0.6) = -2.1(仍亏)。"""
    conn = _mem_with_hitrate()
    monkeypatch.setattr(pw.db, "get_connection", lambda: conn)
    rc = pw._classify_root_cause({
        "decision_id": 2, "target_asset": "ETH", "suggested_action": "BUY",
        "forward_pnl": 1.5, "is_correct": "WIN",
        "event_type_code": "etf_flow", "market_confirmation": "bull",
        "mfe_pct": 1.8, "mae_pct": -0.2,
    })
    cf = json.loads(rc["counterfactual"])
    assert abs(cf["net_rev_pct"] - (-2.1)) < 1e-6


def _llm_db(tmp_path) -> str:
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY, decision_id INTEGER, asset TEXT, action TEXT,
            error_layer TEXT, error_sub_layer TEXT, error_signal TEXT,
            root_cause TEXT, lesson TEXT, pnl_pct REAL, mfe_pct REAL, mae_pct REAL,
            depth_score INTEGER, llm_depth_reflection TEXT DEFAULT '');
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY, event_strength REAL, event_type_code TEXT,
            market_confirmation TEXT, reasoning TEXT, created_at TEXT);
        """
    )
    c.execute("INSERT INTO ai_decisions VALUES (1, 0.8, 'etf', 'bull', 'r', "
              "'2026-11-19 10:00:00')")
    c.execute(
        "INSERT INTO trade_reflections_v2 (id, decision_id, asset, action, "
        "error_layer, error_sub_layer, error_signal, root_cause, lesson, "
        "pnl_pct, mfe_pct, mae_pct, depth_score, llm_depth_reflection) "
        "VALUES (1, 1, 'ETH', 'BUY', 'filter', 'direction', 'trend_buy', "
        "'rc', 'ls', 3.2100, 0.05, -0.01, 6, '')"
    )
    c.commit()
    c.close()
    return p


def test_win_gets_llm_reflection(monkeypatch, tmp_path):
    """赢单(+3.21%)必须进 LLM 归因，prompt 用 WIN 模板且带成本/反事实净值。"""
    path = _llm_db(tmp_path)

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "get_connection", _conn)
    seen = {}

    def fake_call(prompt):
        seen["p"] = prompt
        return {"key_success_chain": "ok", "lesson_candidate": "保留 ETH BUY 路径",
                "keep_rule": "event_strength>=0.7", "mode": "WIN"}

    monkeypatch.setattr(trl, "_call_llm_depth", fake_call)
    assert trl.enrich_one(1) is True
    p = seen["p"]
    assert "赢单" in p
    assert "往返成本: 30bp" in p
    # 反向净值 = -(3.21 + 0.6) = -3.81
    assert "机械反演净值: -3.8100" in p
    c = _conn()
    row = c.execute("SELECT lesson, llm_depth_reflection FROM trade_reflections_v2 "
                    "WHERE id=1").fetchone()
    assert row[0] == "保留 ETH BUY 路径"
    assert json.loads(row[1])["mode"] == "WIN"
    c.close()


def test_distill_from_wins_builds_principle():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY, signal_name TEXT, asset TEXT, lesson TEXT,
            llm_depth_reflection TEXT DEFAULT '', pnl_pct REAL, settled INTEGER,
            origin TEXT, created_at TEXT);
        """
    )
    llm = json.dumps({"lesson_candidate": "ETH 放量突破后追多有效",
                      "keep_rule": "event_strength>=0.7 且 vol_z>2",
                      "invalidation_condition": "连续 5 次高开低走"}, ensure_ascii=False)
    for i in range(3):
        c.execute(
            "INSERT INTO trade_reflections_v2 (signal_name, asset, lesson, "
            "llm_depth_reflection, pnl_pct, settled, origin, created_at) "
            "VALUES ('trend_buy', 'ETH', 'ls', ?, ?, 1, 'paper', datetime('now'))",
            (llm if i == 0 else "", 1.2))
    c.execute(
        "INSERT INTO trade_reflections_v2 (signal_name, asset, lesson, "
        "llm_depth_reflection, pnl_pct, settled, origin, created_at) "
        "VALUES ('trend_buy', 'ETH', 'ls', '', -1.0, 1, 'paper', datetime('now'))")
    items = ed.distill_from_wins(c, 30, min_count=3)
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "principle"
    assert it["evidence_n"] == 3           # 亏损行不算进赢单样本
    assert "KEEP_RULE: event_strength>=0.7 且 vol_z>2" in it["content"]
    assert "INVALIDATION: 连续 5 次高开低走" in it["content"]


def test_stale_generation_source_is_filtered(monkeypatch):
    assert er._is_stale_source("reflection_distill_v337:abc/p1", "abc/p1") is False
    assert er._is_stale_source("reflection_distill_v337:abc/p1", "def/p2") is True
    assert er._is_stale_source("reflection_distill_v337", "def/p2") is False  # 老格式不误杀
    assert er._is_stale_source("manual_v188", "def/p2") is False
    # source_tag 一定带当前代次
    monkeypatch.setattr(ed, "current_fingerprint", lambda: "abc/p1")
    assert ed.source_tag() == "reflection_distill_v337:abc/p1"
    monkeypatch.setattr(ed, "current_fingerprint", lambda: "")
    assert ed.source_tag() == ed.SOURCE


def test_meta_loop_ignores_other_generation(monkeypatch):
    """同 5 条本代 + 5 条旧代：只算本代那 5 条(n=5), 旧代不参与学习判定。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY, decision_id INTEGER, layer TEXT, sub_layer TEXT,
            signal_name TEXT, pnl_pct REAL, settled INTEGER, origin TEXT,
            enrich_status TEXT, created_at TEXT, code_fingerprint TEXT DEFAULT '',
            luck_vs_signal_score REAL);
        """
    )
    for i in range(5):
        c.execute(
            "INSERT INTO trade_reflections_v2 (decision_id, layer, sub_layer, "
            "signal_name, pnl_pct, settled, origin, enrich_status, created_at, "
            "code_fingerprint) VALUES (?, 'filter','direction','trend_buy', 0.8, 1, "
            "'paper','ok', datetime('now'), 'newgen/p1')", (100 + i,))
        c.execute(
            "INSERT INTO trade_reflections_v2 (decision_id, layer, sub_layer, "
            "signal_name, pnl_pct, settled, origin, enrich_status, created_at, "
            "code_fingerprint) VALUES (?, 'filter','direction','trend_buy', -0.8, 1, "
            "'paper','ok', datetime('now'), 'oldgen/p0')", (200 + i,))
    monkeypatch.setattr(mr, "_current_fp", lambda: "newgen/p1")
    agg = mr._aggregate_signal_window(c, 7)
    assert len(agg) == 1
    assert agg[0]["n"] == 5 and agg[0]["win_n"] == 5


def test_record_injected_experiences_writes_and_clears(monkeypatch):
    """v482: 记账紧跟落库; 用完后标记必须清空, 否则会串到下一条决策。"""
    import engine.ai_worker as aw
    calls = []

    def fake_apply(eid, did, applied_action="referenced", llm_invoked=0):
        calls.append((eid, did, applied_action, llm_invoked))
        return True

    monkeypatch.setattr(aw, "_LAST_INJECTED_EXP_IDS", [21, 24])
    monkeypatch.setattr(er, "apply_experience", fake_apply)
    n = aw._record_injected_experiences(90210)
    assert n == 2
    assert calls == [(21, 90210, "prompt_inject", 1),
                     (24, 90210, "prompt_inject", 1)]
    assert aw._LAST_INJECTED_EXP_IDS == []
    # 没有 id 时不应误记账
    assert aw._record_injected_experiences(0) == 0


def test_experience_block_cache_is_per_key(monkeypatch):
    """v490: 经验块缓存按 (asset,gate,actor) 分键, 不再是一锅烩的单条。

    旧实现只有一条全局缓存, BTC 取完紧接着 WTI 取, BTC 的块就被踢掉;
    更糟的是并发下"命中哪条"取决于最后写入者。
    """
    import engine.ai_worker as aw
    aw._EXPERIENCE_BLOCK_CACHE.clear()
    calls = []

    def fake_build(asset="", gate_kind="", actor="", max_chars=1800, decision_id=0):
        calls.append(asset)
        return (f"BLOCK-{asset}", [1])

    monkeypatch.setattr(er, "build_prompt_block", fake_build)
    b1 = aw._load_experience_block_sync(asset="BTC")
    w1 = aw._load_experience_block_sync(asset="WTI")
    b2 = aw._load_experience_block_sync(asset="BTC")
    assert b1 == "BLOCK-BTC" and w1 == "BLOCK-WTI"
    assert b2 == "BLOCK-BTC"
    # BTC 第二次必须命中缓存(不再调 build), 说明两键并存
    assert calls == ["BTC", "WTI"]


def test_injected_ids_are_thread_local(monkeypatch):
    """v490: 注入 id 标记改成线程本地, 避免 A 决策的经验记到 B 决策头上。"""
    import threading
    import engine.ai_worker as aw

    barrier = threading.Barrier(2)
    seen = {}

    def worker(tag, ids):
        aw._set_injected_exp_ids(ids)
        barrier.wait(timeout=5)
        seen[tag] = aw._get_injected_exp_ids()
        barrier.wait(timeout=5)
        aw._set_injected_exp_ids([])

    t1 = threading.Thread(target=worker, args=("a", [11]))
    t2 = threading.Thread(target=worker, args=("b", [22]))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert seen == {"a": [11], "b": [22]}


def test_distiller_includes_win_source():
    """v482: 蒸馏要同时包含赢单来源, 否则经验池还是单边。

    调度侧不在 Python 里 —— 由 launchd `com.trident.experience-distill-cron`
    调 `~/.trident/wrappers/run_experience_distill.sh` 每 6 小时跑一次,
    所以这里只锁"来源是否齐全", 不再假设进程内 daemon。
    """
    src = open(ed.__file__, encoding="utf-8").read()
    assert "distill_from_wins(conn, days, 3)" in src
