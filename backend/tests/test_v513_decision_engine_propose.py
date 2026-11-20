#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v513：决策生产端（propose_one）的行为测试。

这一层回答"要不要做这一单"。它出过一个致命 bug：调用方传的是 sqlite3.Row，
而代码里用 `move.get(...)` —— Row 没有 .get()，每一条异动都在写决策之前抛
AttributeError，整批回填直接中断（产线零产出，日志看起来像"没有异动"）。
所以第一条用例就是"用真实 Row 跑通"。

其余钉住：
  ① 方向映射与决策类型（BUY/SELL=ENTRY，HOLD=REJECT 且必须写拒绝理由）；
  ② 因果链否决会改动作并留证；链不足只注释不改动作；链模块崩了不许阻断下单；
  ③ 重复信号不重复下单；新闻回链失败不影响决策落库。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import decision_engine as de

MOVES_DDL = """
CREATE TABLE critical_price_moves(
    id INTEGER, asset TEXT, ts INTEGER, start_price REAL, peak_price REAL,
    end_price REAL, move_pct REAL, move_kind TEXT, duration_min INTEGER,
    algo_score REAL, context_json TEXT
);
CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, context_json TEXT);
CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, close REAL);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(MOVES_DDL)
    return c


@pytest.fixture()
def writer(monkeypatch):
    """接管写库：只记录参数并返回 id，便于逐字段断言。"""
    calls = []

    def _write(conn, **kw):
        calls.append(kw)
        return 101

    import signals.decision_writer as dw
    monkeypatch.setattr(dw, "write_decision", _write, raising=False)
    return calls


@pytest.fixture()
def chain(monkeypatch):
    """默认：链模块可用但没有任何链命中。"""
    import chain_prior as cp
    monkeypatch.setattr(cp, "match_by_time", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(cp, "aggregate_direction", lambda p: "UNKNOWN", raising=False)
    monkeypatch.setattr(cp, "conflict_veto", lambda p, a: None, raising=False)
    return cp


def _row(conn, sid=1, asset="BTC", move_pct=5.0, end=100.0, peak=99.0,
         algo=0.9, ts=1_600_000_000):
    conn.execute("INSERT INTO critical_price_moves VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, asset, ts, 100.0, peak, end, move_pct, "spike", 30, algo, "{}"))
    conn.commit()
    return conn.execute("SELECT id, asset, ts, start_price, peak_price, end_price, "
                        "move_pct, move_kind, duration_min, algo_score, context_json "
                        "FROM critical_price_moves WHERE id=?", (sid,)).fetchone()


# ── 回归：真实 sqlite3.Row 不许崩 ───────────────────────────
def test_real_row_inserts_without_get_attribute(conn, writer, chain):
    """Row 没有 .get()：这条一旦挂，决策产线就是零产出。"""
    mv = _row(conn)
    assert type(mv).__name__ == "Row"
    res = de.propose_one(conn, mv)
    assert res["action"] == "inserted" and res["decision_id"] == 101
    assert len(writer) == 1


def test_optional_columns_are_none_not_crash(conn, writer, chain):
    mv = _row(conn)          # 表里没有 news_id/planned_stop 这些列
    de.propose_one(conn, mv)
    kw = writer[0]
    assert kw["news_id"] is None
    assert kw["planned_stop"] is None and kw["planned_target"] is None
    assert kw["size_usdt"] is None


# ── 方向与决策类型 ───────────────────────────────────────
def test_buy_is_entry_with_full_evidence(conn, writer, chain):
    mv = _row(conn, move_pct=5.0, end=100.0)
    de.propose_one(conn, mv)
    kw = writer[0]
    assert kw["final_action"] == "BUY" and kw["decision_type"] == "ENTRY"
    assert kw["entry_price"] == pytest.approx(100.0)
    assert kw["score"] == pytest.approx(0.9), "algo_score 作为方向分写进 score"
    assert kw["deny_reason"] is None
    ctx = kw["context"]
    assert ctx["signal_id"] == 1 and isinstance(ctx["signal_id"], int)
    assert ctx["algo_score"] == pytest.approx(0.9)
    assert ctx["move_pct"] == pytest.approx(5.0)
    assert ctx["source"] == "decision_engine"


def test_sell_for_down_move(conn, writer, chain):
    mv = _row(conn, move_pct=-6.0)
    de.propose_one(conn, mv)
    assert writer[0]["final_action"] == "SELL"
    assert writer[0]["decision_type"] == "ENTRY"


def test_hold_is_reject_with_reason(conn, writer, chain):
    mv = _row(conn, move_pct=0.05)
    res = de.propose_one(conn, mv)
    assert res["reason"] == "HOLD"
    kw = writer[0]
    assert kw["final_action"] == "HOLD" and kw["decision_type"] == "REJECT"
    assert kw["deny_reason"], "HOLD 必须写拒绝理由，否则事后无法解释为什么不做"
    assert "signal_id=1" in kw["deny_reason"]


def test_entry_price_falls_back_to_peak(conn, writer, chain):
    mv = _row(conn, end=None, peak=99.5)
    de.propose_one(conn, mv)
    assert writer[0]["entry_price"] == pytest.approx(99.5)


def test_entry_price_falls_back_to_daily_close(conn, writer, chain):
    conn.execute("INSERT INTO ohlcv_daily VALUES('BTC', 1_600_000_000, 98.0)")
    conn.commit()
    mv = _row(conn, end=None, peak=None)
    de.propose_one(conn, mv)
    assert writer[0]["entry_price"] == pytest.approx(98.0)


# ── 因果链：否决 / 只注释 / 崩了也不阻断 ────────────────────
def test_chain_veto_turns_action_into_hold(conn, writer, chain, monkeypatch):
    monkeypatch.setattr(chain, "match_by_time", lambda *a, **k: [{"chain_id": "C7"}], raising=False)
    monkeypatch.setattr(chain, "aggregate_direction", lambda p: "down", raising=False)
    monkeypatch.setattr(chain, "conflict_veto", lambda p, a: {
        "chain_id": "C7", "name": "加息→风险资产", "direction": "down",
        "hit_rate": 0.72, "n_outcomes": 9, "posterior": 0.88}, raising=False)
    mv = _row(conn, move_pct=5.0)
    res = de.propose_one(conn, mv)
    assert res["reason"] == "HOLD", "强链与异动方向相反时必须否决"
    kw = writer[0]
    assert kw["final_action"] == "HOLD" and kw["decision_type"] == "REJECT"
    assert kw["context"]["chain_veto"]["chain_id"] == "C7"
    assert "C7" in kw["deny_reason"]


def test_weak_chain_only_annotates(conn, writer, chain, monkeypatch):
    monkeypatch.setattr(chain, "match_by_time", lambda *a, **k: [{"chain_id": "C3"}], raising=False)
    monkeypatch.setattr(chain, "aggregate_direction", lambda p: "up", raising=False)
    mv = _row(conn, move_pct=5.0)
    de.propose_one(conn, mv)
    kw = writer[0]
    assert kw["final_action"] == "BUY", "链不足时只注释，不改动作"
    assert kw["context"]["chain_prior"] == [{"chain_id": "C3"}]
    assert kw["context"]["chain_direction"] == "up"
    assert "chain_veto" not in kw["context"]


def test_chain_module_failure_does_not_block_order(conn, writer, chain, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("chain db locked")
    monkeypatch.setattr(chain, "match_by_time", _boom, raising=False)
    res = de.propose_one(conn, _row(conn))
    assert res["action"] == "inserted", "链模块挂了也必须能下单（失败开放，但要留日志）"
    assert writer[0]["final_action"] == "BUY"


# ── 去重与回链 ───────────────────────────────────────────
def test_duplicate_signal_is_skipped(conn, writer, chain):
    conn.execute("INSERT INTO trade_decisions(id, context_json) VALUES(9, ?)",
                 (json.dumps({"signal_id": 1}),))
    conn.commit()
    res = de.propose_one(conn, _row(conn, sid=1))
    assert res["action"] == "skipped" and writer == []


def test_news_link_failure_does_not_lose_decision(conn, writer, chain, monkeypatch):
    import feeds.news_decision_link as nl
    monkeypatch.setattr(nl, "link_news_to_decision",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no link")),
                        raising=False)
    res = de.propose_one(conn, _row(conn))
    assert res["action"] == "inserted" and res["decision_id"] == 101
