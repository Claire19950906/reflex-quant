# -*- coding: utf-8 -*-
"""v342 端点回归: /api/decision/{id}/horizons 与 /api/horizons/stats

钉死的四件事：
  1. 单条决策能读回 5 个周期（缺周期的如实不返回，不补 0）
  2. 胜率分子分母只算 status='settled' —— no_price 不能被当成"判断错"
  3. 不同周期分别给：15m 胜率高、5d 胜率低这种情况必须能看出来
  4. 决策不存在 → ok=False，不 500

全部离线：临时 sqlite，不碰生产库、无网络、无 LLM。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """建临时库（真实建表函数），并把 api_server 用到的 db.get_connection 指过去。"""
    import db
    import multi_horizon_settle as mhs
    import trade_decisions as td

    path = str(tmp_path / "horizons.db")
    conn = sqlite3.connect(path)
    td.init_schema(conn)
    # v342 给 trade_decisions 补的 6 列（端点会读 decision_source/score/confidence/status）
    for col, decl in (("status", "TEXT"), ("decision_source", "TEXT"),
                      ("score", "REAL"), ("confidence", "REAL"),
                      ("structure_score", "REAL"), ("confidence_detail", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE trade_decisions ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    mhs.init_schema(conn)
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "get_connection", _conn)
    return path


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


def _ins_hz(conn, did, asset, horizon, sec, status, outcome, pnl, hit, now):
    """一行一个 (decision, horizon) —— PK 是这两列的组合，不能重复。"""
    conn.execute(
        "INSERT INTO decision_outcomes_horizon (decision_id, horizon, horizon_sec, "
        " asset, action, entry_price, decision_ts, target_ts, fwd_price, fwd_ts, "
        " outcome_pct, pnl_pct, hit, status, note, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, horizon, sec, asset, "BUY", 68000.0, now - 86400,
         now - 86400 + sec, None, None, outcome, pnl, hit, status, "test", int(now)))


def _decision(conn, did, asset, now):
    conn.execute(
        "INSERT INTO trade_decisions (id, asset, intended_action, final_action, "
        " decision_type, entry_price, decision_ts, decision_source, score, "
        " confidence, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (did, asset, "BUY", "BUY", "trade", 68000.0, now - 86400,
         "algo_move", 0.61, None, "pending"))


def _seed_one(path, decision_id=101, asset="BTC"):
    """单条决策：四个周期各一行（含取不到价 / 不可执行），给单条读取端点用。"""
    now = int(time.time())
    conn = sqlite3.connect(path)
    _decision(conn, decision_id, asset, now)
    _ins_hz(conn, decision_id, asset, "15m", 900, "settled", 0.8, 0.62, 1, now)
    _ins_hz(conn, decision_id, asset, "1h", 3600, "settled", 1.5, 1.22, 1, now)
    _ins_hz(conn, decision_id, asset, "4h", 14400, "no_price", None, None, None, now)
    _ins_hz(conn, decision_id, asset, "1d", 86400, "not_executable", None, None, None, now)
    conn.commit()
    conn.close()
    return decision_id


def _seed_many(path, asset="ETH", base=301, n=3):
    """三条决策：15m 两胜一负；1h 一胜；4h 一条 no_price；1d 一条 not_executable。"""
    now = int(time.time())
    conn = sqlite3.connect(path)
    for i in range(n):
        did = base + i
        _decision(conn, did, asset, now)
    _ins_hz(conn, base + 0, asset, "15m", 900, "settled", 0.8, 0.62, 1, now)
    _ins_hz(conn, base + 1, asset, "15m", 900, "settled", -0.4, -0.51, 0, now)
    _ins_hz(conn, base + 2, asset, "15m", 900, "settled", 0.3, 0.21, 1, now)
    _ins_hz(conn, base + 0, asset, "1h", 3600, "settled", 1.5, 1.22, 1, now)
    _ins_hz(conn, base + 1, asset, "4h", 14400, "no_price", None, None, None, now)
    _ins_hz(conn, base + 2, asset, "1d", 86400, "not_executable", None, None, None, now)
    conn.commit()
    conn.close()
    return base


def test_decision_horizons_endpoint(client, tmp_db):
    did = _seed_one(tmp_db)
    r = client.get(f"/api/decision/{did}/horizons")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["decision"]["asset"] == "BTC"
    horizons = {h["horizon"] for h in body["horizons"]}
    assert {"15m", "1h", "4h", "1d"} == horizons   # 没算的周期不补 0


def test_decision_horizons_missing_id(client, tmp_db):
    r = client.get("/api/decision/999999/horizons")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_horizon_stats_excludes_untradeable_from_rate(client, tmp_db):
    _seed_many(tmp_db, asset="ETH", base=301, n=3)
    r = client.get("/api/horizons/stats?days=7&asset=ETH")
    assert r.status_code == 200
    body = r.json()
    by = {h["horizon"]: h for h in body["horizons"]}

    h15 = by["15m"]
    assert h15["settled"] == 3 and h15["wins"] == 2
    assert h15["hit_rate"] == 0.6667
    assert h15["avg_pnl_pct"] == round((0.62 - 0.51 + 0.21) / 3, 3)

    assert by["4h"]["settled"] == 0
    assert by["4h"]["no_price"] == 1
    assert by["4h"]["hit_rate"] is None          # 没有可结算样本 → 不给胜率

    assert by["1d"]["not_executable"] == 1
    assert body["status_totals"]["no_price"] == 1


def test_horizon_stats_empty_is_explicit(client, tmp_db):
    r = client.get("/api/horizons/stats?days=1&asset=NOPE")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["horizons"] == []
    assert "note" in body
