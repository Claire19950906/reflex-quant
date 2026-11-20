# -*- coding: utf-8 -*-
"""v337 修复项回归测试 (全离线, 不碰生产库)。

覆盖 4 个修复:
  1. chain_prior —— 因果链接进决策 (asset 过滤 / 证据加权 / 否决门槛)
  2. calc_event_move_attribution._is_price_echo —— 行情播报类标题识别
  3. calc_event_move_attribution._relocate_to_rejected —— 存量治理 (非破坏迁移)
  4. capability_acceptance._llm_outage —— LLM 通道故障与真积压的分离
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import chain_prior


# ── 夹具 ──────────────────────────────────────────────────────────────────
def _mem_conn(chain_rows, news_rows=(), outcome_rows=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE causal_chains (
            id INTEGER PRIMARY KEY, chain_id TEXT, name TEXT, asset TEXT,
            trigger_words TEXT, trigger_keywords TEXT, expected_direction TEXT,
            weight REAL, hit_count INTEGER, miss_count INTEGER,
            magnitude_lo REAL, magnitude_hi REAL, mechanism TEXT, enabled INTEGER,
            confidence REAL DEFAULT 1.0, active INTEGER DEFAULT 1
        );
        CREATE TABLE causal_chain_outcomes (
            id INTEGER PRIMARY KEY, chain_id TEXT, status TEXT,
            predicted_direction TEXT, direction_hit REAL, trigger_ts INTEGER
        );
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER, headline TEXT
        );
        """
    )
    for r in chain_rows:
        conn.execute(
            "INSERT INTO causal_chains (id, chain_id, name, asset, trigger_words,"
            " trigger_keywords, expected_direction, weight, hit_count, miss_count,"
            " magnitude_lo, magnitude_hi, mechanism, enabled, confidence, active)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r,
        )
    for o in outcome_rows:
        conn.execute(
            "INSERT INTO causal_chain_outcomes (chain_id, status) VALUES (?,?)", o,
        )
    for r in news_rows:
        conn.execute("INSERT INTO news_events (id, asset, ts, headline) VALUES (?,?,?,?)", r)
    return conn


def _chain(asset="XAU", words="降息,美联储", direction="UP", weight=0.9,
           hit=8, miss=2, enabled=1, cid="c1"):
    # v414: posterior 改成取 confidence（原来取恒 1.0 的 weight），夹具必须带上这一列；
    #       id 取 cid 的数字形式，避免 cid="1" 时仍被算成 id=2 撞主键。
    rid = int(cid) if str(cid).isdigit() else (1 if cid == "c1" else 2)
    return (rid, cid, f"{asset}链", asset, words, "",
            direction, weight, hit, miss, 1.0, 3.0, "降息压实际利率推金价", enabled,
            0.6, 1)


# ── 1. chain_prior ────────────────────────────────────────────────────────
def test_match_requires_same_asset():
    conn = _mem_conn([_chain(asset="XAU"), _chain(asset="WTI", cid="c2")])
    priors = chain_prior.match_by_headline(conn, "XAU", "美联储降息预期升温")
    assert len(priors) == 1
    assert priors[0]["asset"] == "XAU"
    # 品种不符 (WTI) 不得蹭 XAU 的新闻
    assert chain_prior.match_by_headline(conn, "WTI", "美联储降息预期升温")[0]["chain_id"] == "c2"


def test_match_requires_keyword_hit():
    conn = _mem_conn([_chain()])
    assert chain_prior.match_by_headline(conn, "XAU", "今日天气晴朗") == []


def test_unenabled_chain_never_matches():
    conn = _mem_conn([_chain(enabled=0)])
    assert chain_prior.match_by_headline(conn, "XAU", "美联储降息") == []


def test_evidence_beats_raw_posterior():
    """命中率高的老链应排在只靠权重新链前面。"""
    conn = _mem_conn([
        _chain(cid="1", weight=1.0, hit=0, miss=0),          # 新链, 零证据
        (2, "2", "验证链", "XAU", "降息", "", "UP", 0.7, 7, 3, 1.0, 3.0, "m", 1, 0.6, 1),
    ], outcome_rows=[("2", "HIT")] * 7 + [("2", "MISS")] * 3)
    priors = chain_prior.match_by_headline(conn, "XAU", "降息交易")
    # v414: 证据表用 c.id=o.chain_id 连接（生产里 chain_id 就是 id 的文本形式），
    #        夹具用数字型 chain_id 才能复现"证据覆盖先验"。
    assert [p["chain_id"] for p in priors] == ["2", "1"]


def test_aggregate_direction_conflict():
    # v414 口径：正式方向(UP/DOWN)要 n>=12 且 Wilson 下界>0.5；薄样本只能出 *_WEAK
    up = {"direction": "UP", "n_outcomes": 12, "hit_rate": 0.8, "wilson_lower": 0.6}
    dn = {"direction": "DOWN", "n_outcomes": 12, "hit_rate": 0.8, "wilson_lower": 0.6}
    thin_up = {"direction": "UP", "n_outcomes": 5, "hit_rate": 0.8, "wilson_lower": 0.45}
    assert chain_prior.aggregate_direction([up]) == "UP"
    assert chain_prior.aggregate_direction([up, dn]) == "CONFLICT"
    assert chain_prior.aggregate_direction([thin_up]) == "UP_WEAK"
    assert chain_prior.aggregate_direction([]) == "UNKNOWN"


def test_conflict_veto_requires_evidence():
    strong = {"chain_id": "c1", "direction": "DOWN", "n_outcomes": 12,
              "hit_rate": 0.75, "wilson_lower": 0.55, "posterior": 0.9}
    weak = dict(strong, n_outcomes=1, wilson_lower=0.2)
    low_posterior = dict(strong, posterior=0.5)
    # BUY 撞上"方向 DOWN + 证据充分"的链 → 否决
    assert chain_prior.conflict_veto([strong], "BUY")["chain_id"] == "c1"
    # 样本不足 / 后验不够 → 不否决
    assert chain_prior.conflict_veto([weak], "BUY") is None
    assert chain_prior.conflict_veto([low_posterior], "BUY") is None
    # 同向 → 不否决
    assert chain_prior.conflict_veto([strong], "SELL") is None


def test_match_by_time_uses_nearest_news():
    conn = _mem_conn(
        [_chain(asset="BTC", words="ETF,净流入", cid="c1")],
        news_rows=[(1, "BTC", 1000, "比特币 ETF 单日净流入 3 亿美元")],
    )
    priors = chain_prior.match_by_time(conn, "BTC", 1100, window_sec=600)
    assert priors and priors[0]["match_type"] == "time_window"
    # 窗口外 → 不猜
    assert chain_prior.match_by_time(conn, "BTC", 5000, window_sec=600) == []
    # v337: 默认不吃"异动之后"的新闻 (前视偏差), 只有 allow_future 才看未来
    assert chain_prior.match_by_time(conn, "BTC", 900, window_sec=600) == []
    assert chain_prior.match_by_time(conn, "BTC", 900, window_sec=600,
                                     allow_future=True)


def test_prompt_block_empty_when_no_prior():
    assert chain_prior.build_prompt_block([]) == ""
    block = chain_prior.build_prompt_block([
        {"asset": "XAU", "direction": "UP", "name": "降息推金", "posterior": 0.9,
         "hit_rate": 0.8, "n_outcomes": 10, "wilson_lower": 0.55,
         "matched_keywords": ["降息"], "mechanism": "实际利率下行"}
    ])
    # v414: prompt 已按档位拆成"已验证档/候选档"，不再有"反例依据"字样；
    #        无证据(n=0)的链不进已验证档，只能出现在候选档。
    assert "命中因果链" in block and "降息推金" in block


# ── 2/3. 归因治理 ─────────────────────────────────────────────────────────
def test_generic_tokens_do_not_match():
    """触发词里的 BUY/SELL/ETH/verdict 等通用词不得造成命中。"""
    conn = _mem_conn([_chain(words="BUY,SELL,ETH,verdict,market_event,降息")])
    assert chain_prior.match_by_headline(conn, "XAU", "ETH 现货 ETF 净流入, 市场情绪转暖") == []
    assert chain_prior.match_by_headline(conn, "XAU", "美联储降息落地")  # 中文词仍可命中


def test_ascii_keyword_requires_word_boundary():
    """英文关键词必须词边界匹配, 否则 oil 会命中 boils。"""
    conn = _mem_conn([_chain(words="oil")])
    assert chain_prior.match_by_headline(conn, "XAU", "crude boils over") == []
    assert chain_prior.match_by_headline(conn, "XAU", "oil prices surge")


def test_depth_watch_commit_actually_writes(tmp_path, monkeypatch):
    """v337 回归: --commit 以前只打印"已存盘口", 从不调用 store(), 库内 0 行。"""
    import depth_watch as dw
    db = tmp_path / "depth.db"
    fake = dict(best_bid=1.0, best_ask=1.01, spread_bp=1.0, bid_vol=10.0,
                ask_vol=12.0, imbalance=0.45, near_bid=5.0, near_ask=6.0,
                wall_bid=2.0, wall_ask=3.0, wall_bid_px=0.99, wall_ask_px=1.02)
    monkeypatch.setattr(dw, "DB_PATH", db)
    monkeypatch.setattr(dw, "SYMBOLS", {"BTC": "BTCUSDT"})
    monkeypatch.setattr(dw, "snapshot", lambda sym: dict(fake))
    monkeypatch.setattr(sys, "argv", ["depth_watch.py", "--fetch", "--commit"])
    dw.main()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM depth_snapshots").fetchone()[0] == 1


def test_is_price_echo_positive_and_negative():
    from calc_event_move_attribution import _is_price_echo
    assert _is_price_echo("BTC 跌破 75,000 美元，24 小时跌幅 1.08%")
    assert _is_price_echo("ETH 涨破 2,300 美元关口")
    assert not _is_price_echo("美联储宣布降息 25 个基点")
    assert not _is_price_echo("BTC 跌破 75,000")  # 缺"美元/关口/24小时"标记 → 不算纯播报


def _attr_conn():
    conn = sqlite3.connect(":memory:")
    cols = ("id INTEGER PRIMARY KEY, move_id INTEGER, news_event_id INTEGER, asset TEXT,"
            " time_lag_sec INTEGER, algo_score REAL, algo_reasons TEXT, ai_called INTEGER,"
            " ai_explanation TEXT, ai_confidence REAL, ai_verdict TEXT, move_ts INTEGER,"
            " move_pct REAL, move_kind TEXT, duration_min INTEGER, ev_news_id INTEGER,"
            " ev_source TEXT, ev_action TEXT, ev_ts INTEGER")
    conn.executescript(
        f"CREATE TABLE event_move_attribution ({cols});"
        "CREATE TABLE event_move_attribution_rejected (id INTEGER PRIMARY KEY,"
        " orig_row_id INTEGER, move_id INTEGER, news_event_id INTEGER, asset TEXT,"
        " time_lag_sec INTEGER, algo_score REAL, algo_reasons TEXT, ai_called INTEGER,"
        " ai_explanation TEXT, ai_confidence REAL, ai_verdict TEXT, move_ts INTEGER,"
        " move_pct REAL, move_kind TEXT, duration_min INTEGER, ev_news_id INTEGER,"
        " ev_source TEXT, ev_action TEXT, ev_ts INTEGER, reject_reason TEXT, created_at TEXT);"
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, headline TEXT);"
        "CREATE TABLE critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT);"
    )
    return conn


def _insert_attr(conn, rid, move_id, news_id, asset="BTC"):
    conn.execute(
        "INSERT INTO event_move_attribution (id, move_id, news_event_id, asset, time_lag_sec,"
        " algo_score, algo_reasons, ai_called, move_ts, move_pct, move_kind, duration_min,"
        " ev_news_id, ev_source, ev_action, ev_ts)"
        " VALUES (?,?,?,?,60,0.9,'match=asset; lag=60s; predictive',0,1000,-2.5,'fast_fall',60,?,"
        "'src','SELL',900)", (rid, move_id, news_id, asset, news_id),
    )


def test_purge_price_echo_moves_rows_to_rejected():
    from calc_event_move_attribution import purge_price_echo
    conn = _attr_conn()
    conn.execute("INSERT INTO news_events VALUES (1,'BTC 跌破 75,000 美元，24 小时跌幅 1.08%')")
    conn.execute("INSERT INTO news_events VALUES (2,'美联储降息 25 个基点')")
    conn.execute("INSERT INTO critical_price_moves VALUES (10,'BTC')")
    conn.execute("INSERT INTO critical_price_moves VALUES (11,'BTC')")
    _insert_attr(conn, 1, 10, 1)   # 行情播报类
    _insert_attr(conn, 2, 11, 2)   # 真事件

    res = purge_price_echo(conn)
    assert res == {"matched": 1, "moved": 1, "dry_run": False}
    left = conn.execute("SELECT id FROM event_move_attribution").fetchall()
    assert [r[0] for r in left] == [2]
    rej = conn.execute(
        "SELECT orig_row_id, reject_reason FROM event_move_attribution_rejected").fetchall()
    assert rej == [(1, "price_echo")]


def test_repair_orphans_moves_missing_move_rows():
    from calc_event_move_attribution import repair_orphans
    conn = _attr_conn()
    conn.execute("INSERT INTO news_events VALUES (1,'美联储降息')")
    conn.execute("INSERT INTO critical_price_moves VALUES (10,'BTC')")
    _insert_attr(conn, 1, 10, 1)   # 正常
    _insert_attr(conn, 2, 99, 1)   # move_id 已不存在
    res = repair_orphans(conn)
    assert res == {"matched": 1, "moved": 1, "dry_run": False}
    assert conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0] == 1
    assert conn.execute(
        "SELECT reject_reason FROM event_move_attribution_rejected").fetchone()[0] == "orphan_move"


def test_dry_run_does_not_write():
    from calc_event_move_attribution import purge_price_echo
    conn = _attr_conn()
    conn.execute("INSERT INTO news_events VALUES (1,'BTC 跌破 75,000 美元')")
    conn.execute("INSERT INTO critical_price_moves VALUES (10,'BTC')")
    _insert_attr(conn, 1, 10, 1)
    res = purge_price_echo(conn, dry_run=True)
    assert res["moved"] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0] == 1


# ── 4. LLM 故障与真积压分离 ───────────────────────────────────────────────
def test_llm_outage_detects_recent_alert(tmp_path, monkeypatch):
    import capability_acceptance as ca
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(ca, "BACKEND_DIR", tmp_path)
    (runtime / "pool_blocked_alert.json").write_text(
        json.dumps({"last_ts": time.time(), "reason": "全部通道熔断: minimax1[熔断中]"}),
        encoding="utf-8",
    )
    down, reason = ca._llm_outage()
    assert down is True and "全部通道熔断" in reason


def test_llm_outage_ignores_stale_alert(tmp_path, monkeypatch):
    import capability_acceptance as ca
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(ca, "BACKEND_DIR", tmp_path)
    (runtime / "pool_blocked_alert.json").write_text(
        json.dumps({"last_ts": time.time() - 48 * 3600, "reason": "全部通道熔断"}),
        encoding="utf-8",
    )
    assert ca._llm_outage()[0] is False


def test_llm_outage_without_file(tmp_path, monkeypatch):
    import capability_acceptance as ca
    monkeypatch.setattr(ca, "BACKEND_DIR", tmp_path)
    assert ca._llm_outage() == (False, "")
