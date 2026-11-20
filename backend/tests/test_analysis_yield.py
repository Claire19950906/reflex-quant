# -*- coding: utf-8 -*-
"""analysis_yield 回归：终态聚合、分母、重试清单。

全离线：只碰内存库，不读生产库、不发请求。
重点验四件事（都是真出过问题的口径）：
  1. news_events 一对多不能把决策行放大；
  2. 越界必须从分母剔除，不能被算成「分析失败」；
  3. 分母为 0 时比例是 None，不是 0.0；
  4. 重试清单要按 news_id 去重，且已经分析成功的新闻不再出现。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import pytest

import analysis_yield as ay
from analysis_yield import gate_funnel, queue_health


_DDL_DECISIONS = """
CREATE TABLE ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    created_at TEXT,
    news_id INTEGER,
    target_asset TEXT,
    suggested_action TEXT,
    analysis_degraded INTEGER DEFAULT 0,
    analysis_degraded_reason TEXT DEFAULT '',
    hard_data_status TEXT DEFAULT '',
    trade_gate_reason TEXT DEFAULT '',
    evidence_confidence REAL,
    sentiment_score REAL,
    analysis_state TEXT DEFAULT ''
);
"""

_DDL_NEWS = """
CREATE TABLE news_events (news_id INTEGER, headline TEXT, body TEXT);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_DDL_DECISIONS)
    c.execute(_DDL_NEWS)
    return c


def _add(conn, *, news_id=None, asset="BTC", action="HOLD", degraded=0, reason="",
         hard="ok", gate="", ts=None, sentiment=0.0, headline=""):
    conn.execute(
        "INSERT INTO ai_decisions (ts, created_at, news_id, target_asset, suggested_action,"
        " analysis_degraded, analysis_degraded_reason, hard_data_status, trade_gate_reason,"
        " evidence_confidence, sentiment_score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts if ts is not None else time.time()),
         time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time())),
         news_id, asset, action, degraded, reason, hard, gate, None, sentiment),
    )
    if headline:
        conn.execute("INSERT INTO news_events (news_id, headline, body) VALUES (?,?,?)",
                     (news_id, headline, ""))


def test_news_events_one_to_many_does_not_inflate_rows():
    """一条新闻在 news_events 里有 4 行时，决策只能算 1 条（实测放大过 4 倍）。"""
    conn = _conn()
    _add(conn, news_id=1, degraded=1, reason="llm_empty",
         headline="[hash:x] 标题原文 快讯 | 原文: https://a.b")
    for _ in range(4):
        conn.execute("INSERT INTO news_events (news_id, headline, body) VALUES (1,'x','')")
    rows = ay.load_rows(conn, days=7)
    assert len(rows) == 1, f"应只有 1 行，实际 {len(rows)} 行"
    assert rows[0]["headline"] == "[hash:x] 标题原文 快讯 | 原文: https://a.b"


@pytest.mark.parametrize("degraded,reason,asset,hard,expect", [
    (0, "", "BTC", "ok", "complete"),
    (1, "llm_empty", "BTC", "ok", "llm_failed"),
    (1, "server_fallback", "BTC", "ok", "llm_failed"),
    (1, "summary_only", "BTC", "ok", "compressed"),
    (1, "target_none", "CN_STOCK", "missing:none_target", "out_of_scope"),
    (1, "target_none", "XAU", "ok", "parse_failed"),
    (1, "asset_late_bound", "BTC", "ok", "parse_failed"),
])
def test_classify_matches_contract(degraded, reason, asset, hard, expect):
    row = {"id": 1, "ts": 0, "created_at": "", "news_id": 1, "target_asset": asset,
           "suggested_action": "HOLD", "analysis_degraded": degraded,
           "analysis_degraded_reason": reason, "hard_data_status": hard,
           "trade_gate_reason": "", "evidence_confidence": None, "sentiment_score": None}
    assert ay.classify(row)["state"] == expect


def test_out_of_scope_is_excluded_from_denominator():
    conn = _conn()
    _add(conn, asset="BTC", degraded=0)                       # complete
    _add(conn, asset="BTC", degraded=1, reason="llm_empty")   # llm_failed
    _add(conn, asset="CN_STOCK", degraded=1, reason="target_none", hard="missing:none_target")
    rep = ay.report(ay.load_rows(conn, 7), days=7)
    t, r = rep["totals"], rep["rates"]
    assert t["all"] == 3
    assert t["out_of_scope"] == 1
    assert t["in_scope"] == 2
    assert r["complete_over_in_scope"] == pytest.approx(0.5)
    assert r["real_failure_over_in_scope"] == pytest.approx(0.5)
    # 越界占比只在「全部」里有意义，在范围内必须是 None —— 不能算出 0%
    bucket = {b["state"]: b for b in rep["buckets"]}["out_of_scope"]
    assert bucket["share_of_in_scope"] is None


def test_zero_denominator_returns_none_not_zero():
    conn = _conn()
    _add(conn, asset="CN_STOCK", degraded=1, reason="target_none", hard="missing:none_target")
    rep = ay.report(ay.load_rows(conn, 7), days=1)
    assert rep["totals"]["in_scope"] == 0
    assert rep["rates"]["complete_over_in_scope"] is None
    assert rep["rates"]["real_failure_over_in_scope"] is None


def test_evidence_unknown_is_reported_separately():
    """hard_data_status 为空的行不能冒充「证据不 ok」，要单列 unknown。"""
    conn = _conn()
    _add(conn, hard="ok", degraded=0)
    _add(conn, hard="", degraded=0)
    rep = ay.report(ay.load_rows(conn, 7), days=7)
    assert rep["totals"]["hard_status_known"] == 1
    assert rep["totals"]["hard_status_unknown"] == 1
    assert rep["rates"]["evidence_ready_over_known"] == pytest.approx(1.0)


def test_retry_dedupes_and_skips_recovered_news():
    conn = _conn()
    now = int(time.time())
    _add(conn, news_id=10, degraded=1, reason="llm_empty", ts=now - 300, headline="A")
    _add(conn, news_id=10, degraded=1, reason="llm_empty", ts=now - 200)
    _add(conn, news_id=11, degraded=1, reason="llm_empty", ts=now - 100, headline="B")
    _add(conn, news_id=11, degraded=0, ts=now - 50)                    # 之后分析成功
    _add(conn, news_id=12, degraded=1, reason="summary_only", ts=now - 10)  # 契约问题，不重试
    items = ay.retry_candidates(conn, days=7)
    ids = [x["news_id"] for x in items]
    assert ids == [10], f"应只剩 news 10，实际 {ids}"
    assert items[0]["attempts"] == 2


def test_report_has_disposition_for_every_state():
    """每个终态都必须有处置口径，否则前端会出现"无事可做"的空桶。"""
    rep = ay.report([], days=7)
    states = {b["state"] for b in rep["buckets"]}
    assert states == set(ay.ac.ANALYSIS_STATES)
    for b in rep["buckets"]:
        assert b["todo"], f"{b['state']} 缺处置口径"


# ── 通道质量 ────────────────────────────────────────────────────────────
def _call(conn, model="M3", out=900, status="ok", ts=None):
    conn.execute(
        "INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id, tokens_in,"
        " tokens_out, latency_ms, status, error_code, call_site, cached_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts if ts is not None else time.time()), "main", "k", model,
         3000, out, 19000, status, "", "main", 0),
    )


def _call_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE ai_call_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, purpose TEXT, key_alias TEXT,
        model_id TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER,
        status TEXT, error_code TEXT, call_site TEXT, cached_tokens INTEGER)""")
    return c


def test_channel_quality_flags_short_output_channel():
    conn = _call_conn()
    for _ in range(30):
        _call(conn, model="good/M3", out=900)
    for _ in range(20):
        _call(conn, model="bad/M2.7", out=300)      # 100% 短输出
    rows = {c["model_id"]: c for c in ay.channel_quality(conn, days=7)}
    assert rows["good/M3"]["short_rate"] == 0.0
    assert rows["good/M3"]["verdict"] == "正常"
    assert rows["bad/M2.7"]["short_rate"] == pytest.approx(1.0)
    assert rows["bad/M2.7"]["verdict"] == "不建议用于主分析"


def test_channel_quality_small_sample_does_not_conclude():
    """调用数 < 20 时不许下「不行」的结论，哪怕错得离谱。"""
    conn = _call_conn()
    for _ in range(5):
        _call(conn, model="tiny/M2.7", out=100)
    rows = ay.channel_quality(conn, days=7)
    assert rows[0]["verdict"] == "样本不足"


def test_channel_quality_failures_do_not_enter_quality_denominator():
    """失败调用不能进短输出率分母（否则失败被当成「输出短」）。"""
    conn = _call_conn()
    for _ in range(20):
        _call(conn, model="m/M3", out=900)
    for _ in range(10):
        _call(conn, model="m/M3", out=0, status="http_error")
    rows = ay.channel_quality(conn, days=7)
    row = rows[0]
    assert row["short_rate"] == 0.0
    assert row["fail_rate"] == pytest.approx(10 / 30)
    assert row["ok_calls"] == 20


def test_channel_quality_without_table_returns_empty():
    conn = _conn()          # 只有 ai_decisions / news_events
    assert ay.channel_quality(conn, days=7) == []


# ── v414 补：队列水位 / 入口漏斗 ────────────────────────────────────────
def _news_db(rows):
    """最小 raw_news 表：rows = [(status, ts, gate_unavailable, gate_verdict, gate_main_asset)]"""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE raw_news(
        id INTEGER PRIMARY KEY, ts INTEGER, status TEXT, is_noise INTEGER DEFAULT 0,
        gate_unavailable INTEGER DEFAULT 0, gate_verdict TEXT, gate_main_asset TEXT,
        first_seen_ts INTEGER, filter_reason TEXT DEFAULT '')""")
    # queue_health / gate_funnel 会数 ai_decisions 的 news_id（消费口径），离线库也要有
    conn.execute("CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, news_id INTEGER, ts INTEGER)")
    for i, r in enumerate(rows):
        status, ts = r[0], r[1]
        gu = r[2] if len(r) > 2 else 0
        verdict = r[3] if len(r) > 3 else ""
        asset = r[4] if len(r) > 4 else ""
        conn.execute(
            "INSERT INTO raw_news(id,ts,status,is_noise,gate_unavailable,gate_verdict,gate_main_asset)"
            " VALUES(?,?,?,0,?,?,?)", (i + 1, ts, status, gu, verdict, asset))
    return conn


def test_queue_health_counts_exit_by_status_not_only_decisions():
    """出队要含 FILTERED —— 门卫否掉的同样算处理完，否则队列在掉却报积压在涨。"""
    now = int(time.time())
    conn = _news_db([
        ("PENDING", now - 60), ("DONE", now - 3600), ("FILTERED", now - 3600),
        ("FILTERED", now - 3600), ("FILTERED", now - 3600),
    ])
    q = queue_health(conn, hours=24)
    assert q["available"] is True
    assert q["pending"] == 1
    assert q["pending_actionable"] == 1
    assert q["out_per_h"] > 0
    assert q["verdict"].startswith("正常消化")


def test_queue_health_no_growth_claim_when_nothing_left():
    now = int(time.time())
    conn = _news_db([("DONE", now - 3600), ("FILTERED", now - 3600)])
    q = queue_health(conn, hours=24)
    assert q["pending"] == 0
    assert q["pending_actionable"] == 0
    assert q["verdict"].startswith("队列已清空")


def test_queue_health_missing_table_returns_unavailable():
    conn = sqlite3.connect(":memory:")
    q = queue_health(conn, hours=24)
    assert q == {"available": False, "reason": "raw_news 表不存在"}


def test_gate_funnel_splits_gate_down_from_real_filter():
    """门卫不可用(2) 与 门卫有判定(0) 必须分列，不能混成一个「误杀」。"""
    now = int(time.time())
    conn = _news_db([
        ("FILTERED", now - 3600, 0, "排除", "BTC"),      # 真过滤
        ("FILTERED", now - 3600, 0, "", "BTC"),          # 有判定但空 verdict
        ("FILTERED", now - 3600, 2, "", "ETH"),          # 降级放行后仍被否
        ("DONE", now - 3600, 2, "", "SOL"),              # 门卫不可用仍出决策
        ("DONE", now - 3600, 0, "可交易", "BTC"),        # 正常进分析
        ("PENDING", now - 300, 1, "", ""),               # 冻结待回放
    ])
    f = gate_funnel(conn, days=7)
    assert f["available"] is True
    assert f["inbound"] == 6
    assert f["filtered"] == 3
    assert f["analyzed"] == 2
    assert f["filtered_by_gate"] == 2
    assert f["filtered_gate_down"] == 1
    assert f["analyzed_gate_down"] == 1
    assert f["frozen"] == 1
    assert f["verdict_tradeable_but_filtered"] == 0   # 可交易那条是 DONE，不是被否
    assert f["filtered_with_asset"] == 3
    assert f["expired_gate_down"] == 0   # 夹具里没有 expired_gate_down 行


def test_gate_funnel_no_inbound_no_division_by_zero():
    conn = _news_db([])
    f = gate_funnel(conn, days=7)
    assert f["available"] is False
    assert f["filter_rate"] is None
    assert f["analyze_rate"] is None


def test_queue_health_separates_noise_residue_from_actionable():
    """PENDING 合计里混着 is_noise=1 的行，认领 SQL 不收 —— 不能当「快排空」。"""
    now = int(time.time())
    conn = _news_db([
        ("PENDING", now - 600), ("PENDING", now - 600), ("PENDING", now - 600),
        ("DONE", now - 3600), ("FILTERED", now - 3600),
    ])
    conn.execute("UPDATE raw_news SET is_noise=1 WHERE id IN (2,3)")
    q = queue_health(conn, hours=24)
    assert q["pending"] == 3
    assert q["noise_pending"] == 2
    assert q["pending_actionable"] == 1


def test_gate_funnel_counts_grace_archive_separately():
    """v419：过宽限才归档的行要单独计数，不能混进「门卫不可用被否」。"""
    now = int(time.time())
    conn = _news_db([("FILTERED", now - 3600, 2, "", "BTC")])
    conn.execute("UPDATE raw_news SET filter_reason='expired_gate_down' WHERE id=1")
    f = gate_funnel(conn, days=7)
    assert f["filtered"] == 1
    assert f["expired_gate_down"] == 1
