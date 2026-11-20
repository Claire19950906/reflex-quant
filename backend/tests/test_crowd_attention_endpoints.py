# -*- coding: utf-8 -*-
"""v317 端点级回归测试：Crowd Attention 的 HTTP 接口。

前面的 test_crowd_attention_lead.py 测的是**算法层**；
这里测的是**接口层** —— 路由是否真的能打到、字段是否真的带上、
以及最关键的一条：路由必须注册在 catch-all 兜底**之前**。

背景（真实踩过的坑）：
  bridge_server 尾部有 `@app.get("/api/{full_path:path}")` 兜底，
  凡是没显式注册的路径都会被它吃掉并返回 {ok:true,_bridge_fallback:true}；
  前端把这种壳当成"后端没实现"，于是**真实存在的数据被显示成"信号不存在"**。
  所以下面每个端点都要断言 _bridge_fallback 不为真。

全部离线：临时 sqlite 文件库（不碰生产库）、无网络、无 LLM。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db


# ─────────────────────────── 临时库 ───────────────────────────

_SCHEMA = [
    """
    CREATE TABLE crowd_source_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        item_kind TEXT NOT NULL DEFAULT 'theme',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        media_channel TEXT NOT NULL DEFAULT '',
        heat REAL NOT NULL DEFAULT 0,
        rank_no INTEGER NOT NULL DEFAULT 0,
        published_at TEXT NOT NULL DEFAULT '',
        collected_at TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL DEFAULT '',
        month TEXT NOT NULL DEFAULT '',
        topic_key TEXT NOT NULL DEFAULT '',
        meta_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE kline_bars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        ts INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        UNIQUE(symbol, timeframe, ts)
    )
    """,
    """
    CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_asset TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        ts INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE critical_price_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        ts INTEGER NOT NULL,
        start_price REAL, peak_price REAL, end_price REAL,
        move_pct REAL, move_kind TEXT, algo_score REAL,
        duration_min INTEGER, context_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE abnormal_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        asset TEXT NOT NULL DEFAULT '',
        signal_kind TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        anchor_ts INTEGER,
        detail_json TEXT NOT NULL DEFAULT '{}',
        hint TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE abnormal_attributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        asset TEXT NOT NULL DEFAULT '',
        signal_kind TEXT NOT NULL DEFAULT '',
        anchor_ts INTEGER,
        summary TEXT NOT NULL DEFAULT '',
        drivers_json TEXT NOT NULL DEFAULT '[]',
        cross_json TEXT NOT NULL DEFAULT '[]',
        actionable TEXT NOT NULL DEFAULT '观望',
        severity_hint INTEGER NOT NULL DEFAULT 1,
        source_model TEXT NOT NULL DEFAULT '',
        used_news_n INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
]


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """建一个临时 sqlite 库，并把 db.get_connection 指向它。"""
    path = str(tmp_path / "t.db")

    def _connect():
        conn = sqlite3.connect(path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    conn = _connect()
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    conn.close()

    monkeypatch.setattr(_db, "get_connection", _connect)
    return path


def _utc_str(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def scenario(tmp_db):
    """造一个真实结构的时间线（相对**当前时间**，避免测试随日期推移而失效）：

      T   = now-2h   跨平台注意力出现（2 个平台提到 Bitcoin）
      T+30m         行情异动（move_ts），duration_min=10 → anchor = T+20m
                    → 注意力领先异动锚点 20 分钟
    """
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    t_attn = now - _dt.timedelta(hours=2)
    t_move = t_attn + _dt.timedelta(minutes=30)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    # 两个平台的真实条目（带真实发布时间戳）
    for src, title in (
        ("gnews_crypto", "Bitcoin price surges past $120K"),
        ("gnews", "Bitcoin rally draws retail buyers"),
    ):
        conn.execute(
            """INSERT INTO crowd_source_items
               (source, external_id, title, url, media_channel, published_at,
                collected_at, day, month, topic_key)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (src, "%s:%s" % (src, title), title, "https://news.test/%s" % src,
             "media", _utc_str(t_attn), _utc_str(t_attn),
             t_attn.strftime("%Y-%m-%d"), t_attn.strftime("%Y-%m"),
             "text:%s" % title[:20]),
        )

    # 分钟级行情：注意力之前价格已动（媒体追价格的典型形态），
    # 之后又动一次 —— 这样前向命中率与反向对照都非零，判定才有意义。
    base_ms = int(t_attn.timestamp() * 1000)
    closes = [100.0] * 20 + [101.0] * 20 + [100.0] * 20 + [101.5] * 20
    for i, c in enumerate(closes):
        conn.execute(
            "INSERT OR REPLACE INTO kline_bars (symbol,timeframe,ts,close) "
            "VALUES (?,'1m',?,?)",
            ("BTC", base_ms - 20 * 60_000 + i * 60_000, c),
        )

    # 行情异动（bridge 端点的 id 空间 = critical_price_moves.id）
    conn.execute(
        """INSERT INTO critical_price_moves
           (id, asset, ts, start_price, peak_price, end_price, move_pct,
            move_kind, algo_score, duration_min, context_json, created_at)
           VALUES (7,'BTC',?,100.0,101.5,101.5,1.5,'fast_rise',0.9,10,'{}','')""",
        (int(t_move.timestamp()),),
    )

    # AI 归因（signal_id 指向 abnormal_signals.id，与 critical_price_moves.id 不同空间）
    conn.execute(
        """INSERT INTO abnormal_signals
           (id, ts, asset, signal_kind, direction, anchor_ts, detail_json,
            hint, status, created_at)
           VALUES (100,?,'BTC','multi_factor','bullish',?,'{}','','open','')""",
        (int(t_move.timestamp()), int(t_move.timestamp() * 1000)),
    )
    conn.execute(
        """INSERT INTO abnormal_attributions
           (signal_id, asset, signal_kind, anchor_ts, summary, drivers_json,
            cross_json, actionable, severity_hint, source_model, used_news_n,
            created_at)
           VALUES (100,'BTC','multi_factor',?,'测试归因摘要',
                   ?,'[]','yes',2,'test-model',3,'')""",
        (int(t_move.timestamp()),
         json.dumps([{"cause": "驱动因子", "confidence": "high"}],
                    ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return {"attn_ts": t_attn, "move_ts": t_move, "db": tmp_db}


# ─────────────────────────── bridge 归因端点 ───────────────────────────

@pytest.fixture(scope="module")
def bridge_client():
    """bridge_server 的 TestClient。

    注意：import bridge_server 会启动一个后台"持仓快照刷新"线程（既有行为），
    它只读生产库，不会影响本文件的断言（断言全部走临时库）。
    """
    from fastapi.testclient import TestClient
    import bridge_server as bs
    return TestClient(bs.app)


def test_attribution_endpoint_not_swallowed_by_catch_all(bridge_client, scenario):
    """端点必须真的命中，不能被 catch-all 兜底吃掉。"""
    r = bridge_client.get("/api/abnormal-signals/7/attribution")
    assert r.status_code == 200
    body = r.json()
    # 兜底壳的标志位绝不能出现
    assert body.get("_bridge_fallback") is not True
    assert body.get("error") is None
    assert body["signal_id"] == 7
    assert body["asset"] == "BTC"


def test_attribution_returns_real_crowd_attention(bridge_client, scenario):
    """有真实注意力条目 → crowd_attention 必须带真实字段，不得为 null。"""
    body = bridge_client.get("/api/abnormal-signals/7/attribution").json()
    ev = body.get("crowd_attention")
    assert ev is not None, "有真实注意力条目时 crowd_attention 不得为 null"
    assert ev["symbol"] == "BTC"
    assert ev["lead_minutes"] == 20, "注意力应领先异动锚点 20 分钟"
    assert ev["platform_count"] == 2
    assert set(ev["platform_list"]) == {"gnews_crypto", "gnews"}
    assert ev["item_count"] == 2
    assert ev["kind"] == "crowd_attention"
    assert ev["explain"]
    # items 带真实标题/来源/链接，前端要原样展示（不能是空壳）
    assert ev["items"] and ev["items"][0]["title"]
    assert ev["items"][0]["source"] in ("gnews_crypto", "gnews")
    assert ev["items"][0]["url"].startswith("https://")


def test_attribution_crowd_is_null_when_no_attention(bridge_client, tmp_db):
    """没有注意力条目 → crowd_attention 为 null（不编造证据）。"""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        """INSERT INTO critical_price_moves
           (id, asset, ts, move_pct, move_kind, algo_score, duration_min)
           VALUES (9,'SKHYNIX',?,1.2,'slow_fall',0.8,60)""",
        (int(_dt.datetime.now(_dt.timezone.utc).timestamp()),),
    )
    conn.commit()
    conn.close()
    body = bridge_client.get("/api/abnormal-signals/9/attribution").json()
    assert body.get("crowd_attention") is None


def test_attribution_matches_ai_attribution_across_id_spaces(bridge_client, scenario):
    """id 空间不同（critical_price_moves.id vs abnormal_signals.id）也要匹配到 AI 归因，
    并如实标注匹配方式，不假装是 signal_id 直查。"""
    body = bridge_client.get("/api/abnormal-signals/7/attribution").json()
    attr = body.get("attribution")
    assert attr is not None
    assert attr["summary"] == "测试归因摘要"
    assert attr["drivers"][0]["cause"] == "驱动因子"
    assert body["attribution_match"] == "abnormal_signals_id"


def test_attribution_unknown_signal_returns_not_found(bridge_client, scenario):
    body = bridge_client.get("/api/abnormal-signals/999999/attribution").json()
    assert body.get("error") == "not found"


# ─────────────────────────── bridge crowd 端点 ───────────────────────────

def test_attention_lead_endpoint_returns_verdict(bridge_client, scenario):
    """注意力领先性端点：必须带 summary + 判定字段，且判定要能区分
    「注意力领先」与「价格先动」。"""
    r = bridge_client.get("/api/crowd/attention-lead?lookback_hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_bridge_fallback") is not True
    s = body["summary"]
    assert s["events_total"] >= 1
    assert s["events_with_price"] >= 1
    # 判定必须是显式枚举，不能是空/None
    assert s["verdict"] in ("attention_leads_price", "attention_lags_price",
                             "no_signal", "insufficient_data")
    # 反向对照（价格先动的比例）必须有值 —— 没有它就分不清谁领先
    assert s["prior_move_ratio"] is not None
    # 说明文字必须带样本量，不能只给结论
    assert body.get("note")


def test_pending_attention_endpoint(bridge_client, scenario):
    r = bridge_client.get("/api/crowd/pending-attention?lookback_hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_bridge_fallback") is not True
    assert "summary" in body
    assert isinstance(body.get("pending"), list)


# ─────────────────────────── 路由顺序铁律 ───────────────────────────

def test_catch_all_is_registered_after_our_endpoints():
    """catch-all 必须在业务端点**之后**注册 —— 否则一切新端点都会被吃掉。

    这条断言是防止将来有人把端点加到文件尾部时静默失效。
    """
    import bridge_server as bs
    paths = [getattr(r, "path", "") for r in bs.app.routes]
    catch_all = paths.index("/api/{full_path:path}")
    for must_after in ("/api/crowd/attention-lead",
                       "/api/crowd/pending-attention",
                       "/api/abnormal-signals/{signal_id}/attribution"):
        assert paths.index(must_after) < catch_all, (
            "%s 必须注册在 catch-all 之前" % must_after
        )
