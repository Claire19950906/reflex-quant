# -*- coding: utf-8 -*-
"""v319 回归: **8000 端口** (api_server.py) 的 /api/v201/impact/by-asset 契约。

为什么单独一个文件 —— 之前只测了 8100 (bridge_server), 但前端
`fetchWithFallback` 是**先打 8000**, 8000 只要返回 200 就永远不会降级到 8100。
所以 8000 这条路径的契约必须和 8100 **完全一致**:

  · impact_pct → 有符号百分数 (worker 存的是小数: 0.0033 = 0.33%)
  · impact_abs_pct → 幅度 (恒 >= 0), 前端 0.3% 门槛按它判
  · move_direction → up/down/flat (来自 direction 列, 不是符号)
  · severity 门槛下推到 SQL (先 LIMIT 再过滤会把更早的高严重度行吃掉)
  · order=magnitude → 幅度最大的优先 (归因页要"窗口里最大的几次冲击";
    只按时间排序 + LIMIT 会把窗口另一端挤掉 → 30d 图右侧挤一片点、左侧全空)
  · source / n 字段与 bridge 同名 (前端只认一套 JSON)

纯离线: 临时 sqlite + TestClient, 不碰生产库、不需要网络。
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

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_SCHEMA = """
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
"""


@pytest.fixture(scope="module")
def api_client():
    """api_server.app 的 TestClient (只导入一次, 避免重复初始化开销)。"""
    from fastapi.testclient import TestClient
    import api_server as A
    return A, TestClient(A.app)


@pytest.fixture()
def seeded(tmp_path, monkeypatch, api_client):
    """临时库 + 指向它。

    行 (event_id, asset, 相对现在的秒数, impact_pct 小数, severity, direction):
      e1  XAU  -4h  0.0123 (1.23%)  high     up
      e2  XAU  -3h  0.0040 (0.40%)  medium   down
      e3  XAU  -2h  0.0060 (0.60%)  medium   neutral
      e4  XAU  -1h  0.0010 (0.10%)  low      up      ← 最新但幅度最小
      e5  BTC  -1h  0.0500 (5.00%)  high     up      ← 别的品种, 不该混进来
    """
    A, _ = api_client
    path = str(tmp_path / "impact_api.db")
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    now = int(time.time())
    rows = (
        ("e1", "XAU", now - 4 * 3600, 0.0123, "high", "up", "鹰派讲话"),
        ("e2", "XAU", now - 3 * 3600, 0.0040, "medium", "down", "通胀超预期"),
        ("e3", "XAU", now - 2 * 3600, 0.0060, "medium", "neutral", "模棱两可"),
        ("e4", "XAU", now - 1 * 3600, 0.0010, "low", "up", "无关紧要消息"),
        ("e5", "BTC", now - 1 * 3600, 0.0500, "high", "up", "BTC 消息"),
    )
    for eid, asset, ts, pct, sev, direction, title in rows:
        conn.execute(
            "INSERT INTO event_kline_impact (event_source, event_id, event_title,"
            " event_time, importance, asset, pre_event_close, max_high, min_low,"
            " impact_pct, impact_window_start, impact_window_end, impact_severity, direction)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("testsrc", eid, title, ts, "medium", asset, 100.0, 101.0, 99.0,
             pct, ts - 60, ts + 60, sev, direction),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(A, "DB_PATH", path)
    return {"now": now}


def _get(api_client, seeded, qs):
    A, client = api_client
    r = client.get("/api/v201/impact/by-asset?" + qs, headers={"User-Agent": _UA})
    assert r.status_code == 200, r.text[:300]
    return r.json()


# ─────────────────────────────── 单位 / 方向 ───────────────────────────────

def test_impact_pct_is_signed_percent_not_fraction(api_client, seeded):
    """0.0123 (小数) 必须输出成 1.23 (有符号百分数), 原始小数留在 impact_pct_raw。"""
    body = _get(api_client, seeded, "asset=XAU")
    items = {it["event_id"]: it for it in body["items"]}
    up = items["e1"]
    assert up["impact_pct"] == pytest.approx(1.23)
    assert up["impact_abs_pct"] == pytest.approx(1.23)
    assert up["impact_pct_raw"] == pytest.approx(0.0123)
    assert up["move_direction"] == "up"

    down = items["e2"]
    assert down["impact_pct"] == pytest.approx(-0.4), "direction=down → 负号"
    assert down["impact_abs_pct"] == pytest.approx(0.4)
    assert down["move_direction"] == "down"

    neutral = items["e3"]
    assert neutral["impact_pct"] == pytest.approx(0.0), "neutral → 幅度不背锅给方向"
    assert neutral["impact_abs_pct"] == pytest.approx(0.6), "幅度仍在 impact_abs_pct"
    assert neutral["move_direction"] == "flat"


def test_impact_response_shape_matches_bridge_contract(api_client, seeded):
    """前端只认一套 JSON: source / n / items / 每行的 severity。"""
    body = _get(api_client, seeded, "asset=XAU")
    assert body["ok"] is True
    assert body["source"] == "event_kline_impact"
    assert body["n"] == body["count"] == len(body["items"])
    for it in body["items"]:
        assert it["event_title"], "空标题的前端会 continue 掉"
        assert it["impact_severity"] in ("low", "medium", "high", "critical")
        assert it["severity"] == it["impact_severity"]
        assert it["event_backed"] is True


def test_impact_asset_isolation(api_client, seeded):
    """XAU 的结果里不能混进 BTC。"""
    ids = {it["event_id"] for it in _get(api_client, seeded, "asset=XAU")["items"]}
    assert "e5" not in ids
    assert "e1" in ids

# ─────────────────────────────── 排序 / 过滤 ───────────────────────────────

def test_impact_order_magnitude_returns_biggest_first(api_client, seeded):
    """order=magnitude → 幅度最大的排前面 (归因页要"窗口里最大的几次冲击")。"""
    body = _get(api_client, seeded, "asset=XAU&order=magnitude")
    mags = [it["impact_abs_pct"] for it in body["items"]]
    assert mags == sorted(mags, reverse=True), mags
    assert body["items"][0]["event_id"] == "e1"


def test_impact_order_time_returns_newest_first(api_client, seeded):
    """默认 order=time → 最新优先 (旧实现是 ASC, 会把窗口另一端挤掉)。"""
    body = _get(api_client, seeded, "asset=XAU")
    ts = [it["event_time"] for it in body["items"]]
    assert ts == sorted(ts, reverse=True), ts
    assert body["items"][0]["event_id"] == "e4"


def test_impact_severity_filter_applied_before_limit(api_client, seeded):
    """severity_min=high + limit=1 → 必须拿到**更早**但 severity=high 的 e1。

    若先 LIMIT 再过滤 (旧口径), e4 (最新, low) 会占掉名额 → 结果为空。
    """
    body = _get(api_client, seeded, "asset=XAU&severity_min=high&limit=1")
    ids = {it["event_id"] for it in body["items"]}
    assert ids == {"e1"}, ids


def test_impact_min_abs_pct_filter(api_client, seeded):
    """min_abs_pct 按**百分数**比 (0.6 = 0.6%), 且 neutral 事件按幅度算。"""
    body = _get(api_client, seeded, "asset=XAU&min_abs_pct=0.5")
    ids = {it["event_id"] for it in body["items"]}
    assert ids == {"e1", "e3"}, ids

    body2 = _get(api_client, seeded, "asset=XAU&min_abs_pct=1.0")
    ids2 = {it["event_id"] for it in body2["items"]}
    assert ids2 == {"e1"}, ids2


def test_impact_order_magnitude_survives_limit(api_client, seeded):
    """order=magnitude + limit=2 → 留最大的两条, 而不是最新的两条。"""
    body = _get(api_client, seeded, "asset=XAU&order=magnitude&limit=2")
    ids = [it["event_id"] for it in body["items"]]
    assert set(ids) == {"e1", "e3"}, ids


def test_impact_missing_asset_is_400(api_client, seeded):
    """没有 asset → 400 (不能默默返回全库)。"""
    A, client = api_client
    r = client.get("/api/v201/impact/by-asset", headers={"User-Agent": _UA})
    assert r.status_code == 400
