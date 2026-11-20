# -*- coding: utf-8 -*-
"""v319 回归: **双端口** /api/v201/event/opportunities 的契约一致性。

为什么必须单独测 —— 前端的 `fetchWithFallback` 是**先打 8000**, 8000 只要
不是 422/5xx 就永远不会降级到 8100。所以两个端口必须:

  · 同参数 → **同结果** (同源同口径, 字节级一致);
  · 同参数校验 → 同样返回 422 (一个 clamp 一个报错 = 前端行为不可预测);
  · 都带 edge_measure 口径声明 (别把"冲击量级"误读成"预期到手收益")。

纯离线: 临时 sqlite + TestClient, 不碰生产库、不联网。
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_SCHEMA = [
    """
    CREATE TABLE event_kline_impact (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_source TEXT DEFAULT '',
        event_id TEXT DEFAULT '',
        event_title TEXT DEFAULT '',
        event_time INTEGER,
        importance TEXT DEFAULT '',
        asset TEXT DEFAULT '',
        pre_event_close REAL,
        max_high REAL,
        min_low REAL,
        impact_pct REAL,
        impact_window_start INTEGER,
        impact_window_end INTEGER,
        impact_severity TEXT DEFAULT 'low',
        direction TEXT DEFAULT 'neutral'
    )
    """,
    """
    CREATE TABLE ohlcv_daily (
        asset TEXT, ts INTEGER, open REAL, high REAL,
        low REAL, close REAL, volume INTEGER, source TEXT
    )
    """,
]


def _seed(path: str, *, now: int) -> None:
    conn = sqlite3.connect(path)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    # 事件前的日线 (带波动, 让 σ > 0)
    for i in range(30):
        ts = now - (30 - i) * 86400
        close = 100.0 * (1 + 0.002 * i + 0.0012 * math.sin(i * 1.7))
        conn.execute(
            "INSERT INTO ohlcv_daily (asset, ts, open, high, low, close, volume, source)"
            " VALUES ('XAU',?,?,?,?,?,0,'test')",
            (ts, close, close, close, close),
        )
    # 历史同类事件 (给当前事件提供条件统计)
    for i in range(8):
        ts = now - (i + 1) * 86400
        conn.execute(
            "INSERT INTO event_kline_impact (event_source, event_id, event_title,"
            " event_time, importance, asset, pre_event_close, max_high, min_low,"
            " impact_pct, impact_window_start, impact_window_end, impact_severity, direction)"
            " VALUES ('s',?,?,?,'high','XAU',100,102,99,0.02,?,?,'medium','up')",
            (f"h{i}", "历史事件", ts, ts - 60, ts + 60),
        )
    # 当前事件
    conn.execute(
        "INSERT INTO event_kline_impact (event_source, event_id, event_title,"
        " event_time, importance, asset, pre_event_close, max_high, min_low,"
        " impact_pct, impact_window_start, impact_window_end, impact_severity, direction)"
        " VALUES ('s','cur','央行购金',?,'high','XAU',100,102,99,0.02,?,?,'high','up')",
        (now, now - 60, now + 60),
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def clients():
    """两个端口的 TestClient (各 import 一次)。"""
    from fastapi.testclient import TestClient
    import api_server as A
    import bridge_server as B
    return {"8000": (A, TestClient(A.app)), "8100": (B, TestClient(B.app))}


@pytest.fixture()
def seeded(clients, tmp_path, monkeypatch):
    """两个端口都指向同一份临时库 (同一批事件 → 必须算出同一张卡)。"""
    path = str(tmp_path / "opp.db")
    now = int(time.time())
    _seed(path, now=now)
    for mod, _client in clients.values():
        monkeypatch.setattr(mod, "DB_PATH", path)
        # 2026-11-18: bridge 的 db_cursor 已搬到 bridge_builders，单独再指一次
        if getattr(mod, "__name__", "") == "bridge_server":
            import bridge_builders
            monkeypatch.setattr(bridge_builders, "DB_PATH", path)
    return {"now": now, "path": path}


def _get(clients, port, qs):
    _mod, client = clients[port]
    return client.get(f"/api/v201/event/opportunities?{qs}", headers={"User-Agent": _UA})


# ─────────────────────────── 双端口一致性 ───────────────────────────

def test_both_ports_return_identical_payload(clients, seeded):
    """同一参数 → 8000 与 8100 必须**字节级一致** (同源同口径)。"""
    qs = f"limit=5&to_ts={seeded['now']}"
    r8 = _get(clients, "8000", qs)
    r10 = _get(clients, "8100", qs)
    assert r8.status_code == 200, r8.text
    assert r10.status_code == 200, r10.text
    assert r8.content == r10.content, "两个端口返回不一致 —— 前端只会先打 8000"


def test_both_ports_honest_empty_when_no_rows(clients, tmp_path, monkeypatch):
    """空库 → 两个端口都诚实报空 (ok=True, n=0), 不能一个报 200 一个报错。"""
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    for mod, _client in clients.values():
        monkeypatch.setattr(mod, "DB_PATH", path)
        # 2026-11-18: bridge 的 db_cursor 已搬到 bridge_builders，单独再指一次
        if getattr(mod, "__name__", "") == "bridge_server":
            import bridge_builders
            monkeypatch.setattr(bridge_builders, "DB_PATH", path)

    for port in ("8000", "8100"):
        r = _get(clients, port, "limit=5")
        assert r.status_code == 200, (port, r.text)
        body = r.json()
        assert body["ok"] is True
        assert body["n"] == 0
        assert body["items"] == []
        assert body["ready"] == 0


def test_parameter_validation_identical_on_both_ports(clients, seeded):
    """越界参数 → 两个端口都 422 (不能一个 clamp 一个报错)。"""
    now = seeded["now"]
    for qs in ("limit=999", "limit=0", "horizon_minutes=1", "horizon_minutes=99999",
               "lookback_days=0", "lookback_days=99999"):
        codes = {port: _get(clients, port, f"{qs}&to_ts={now}").status_code
                 for port in ("8000", "8100")}
        assert codes["8000"] == codes["8100"] == 422, (qs, codes)


def test_valid_params_accepted_on_both_ports(clients, seeded):
    """合法参数 → 两个端口都 200。"""
    now = seeded["now"]
    for qs in ("limit=1", "asset=XAU&limit=3", "entry_mode=confirm&limit=3",
               "entry_mode=immediate&limit=3", "min_abs_edge_pct=5&limit=3",
               f"from_ts={now - 86400}&to_ts={now}"):
        codes = {port: _get(clients, port, qs).status_code for port in ("8000", "8100")}
        assert codes["8000"] == codes["8100"] == 200, (qs, codes)


# ─────────────────────────── 载荷契约 ───────────────────────────

def test_payload_declares_point_in_time_and_edge_measure(clients, seeded):
    """载荷必须自证: 时点口径 + edge 的度量口径 (上界)。"""
    body = _get(clients, "8000", f"limit=5&to_ts={seeded['now']}").json()
    assert body["ok"] is True
    assert body["point_in_time"] is True
    assert body["source"] == "event_kline_impact"
    assert body["n"] == len(body["items"])
    assert body["honest_note"]
    for it in body["items"]:
        assert it["status"] in ("READY", "NO_TRADE")
        assert it["direction"] in ("LONG", "SHORT", "NO_TRADE")
        assert it["edge_measure"] == "max_excursion_4h"
        assert "上界" in it["edge_measure_note"]
        assert it["rationale"]
        assert it["cost"]["round_trip_pct"] >= 0


def test_edge_measure_note_present_on_both_ports(clients, seeded):
    """两个端口都要带口径说明 —— 前端只认先打的 8000。"""
    for port in ("8000", "8100"):
        body = _get(clients, port, f"limit=5&to_ts={seeded['now']}").json()
        assert body["items"], (port, body)
        for it in body["items"]:
            assert it["edge_measure_note"], port
