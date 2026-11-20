# -*- coding: utf-8 -*-
"""v318 端点级回归测试: /api/trading/signals + /api/v201/impact/by-asset。

背景 —— 两个真实 bug:
  1) **交易信号全是 HOLD**: 旧实现读 `trade_reflections_v2.dimension` 当方向,
     但 dimension 装的是归因分类 (A/B/C...), 不是 BUY/SELL → 端点永远只返回 HOLD,
     前端"交易信号"区一片灰。重写后主源 = `ai_decisions.suggested_action`
     (真实买卖决策), 兜底 = `trade_reflections_v2.action` (BUY/SELL/REJECTED)。
  2) **impact marker 满屏红**: 后端没给方向, 前端硬编码红色 → 涨的跌的都红;
     而且没有最小幅度门槛, 0.1% 的波动也画一个点。

本文件全部离线: 临时 sqlite 库 (不碰生产库)、无网络、无 LLM。
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import sys
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ─────────────────────────── 临时库 schema ───────────────────────────
# 只建端点 SQL 真正用到的列 —— 少了任何一列, 端点会在 except 里静默返回空,
# 那正是这个测试要抓的"静默失败"。
_SCHEMA = [
    """
    CREATE TABLE raw_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT DEFAULT '',
        content TEXT DEFAULT '',
        timestamp TEXT DEFAULT '',
        ts INTEGER
    )
    """,
    """
    CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id INTEGER,
        sentiment_score REAL,
        suggested_action TEXT DEFAULT 'HOLD',
        reasoning TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        market_category TEXT DEFAULT '',
        target_asset TEXT DEFAULT '',
        event_strength TEXT DEFAULT '',
        direct_catalyst INTEGER DEFAULT 0,
        ts INTEGER
    )
    """,
    """
    CREATE TABLE trade_reflections_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER,
        ai_decision_id INTEGER,
        asset TEXT DEFAULT '',
        action TEXT DEFAULT '',
        pnl_pct REAL,
        confidence_at_time REAL,
        hit INTEGER,
        dimension TEXT DEFAULT '',
        lesson TEXT DEFAULT '',
        root_cause TEXT DEFAULT '',
        news_id INTEGER,
        created_at TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE event_move_attribution (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT DEFAULT '',
        move_ts INTEGER,
        move_pct REAL,
        move_kind TEXT DEFAULT '',
        algo_score REAL,
        duration_min INTEGER,
        ev_news_id INTEGER,
        ev_source TEXT DEFAULT '',
        ev_action TEXT DEFAULT '',
        ev_score REAL
    )
    """,
    """
    CREATE TABLE critical_price_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT DEFAULT '',
        ts INTEGER,
        move_pct REAL,
        move_kind TEXT DEFAULT '',
        algo_score REAL,
        duration_min INTEGER
    )
    """,
    # v319: event_kline_impact —— 主资产 (XAU/BTC/WTI) 真实事件影响的唯一来源。
    #   ⚠️ impact_pct 是**小数** (0.0033 = 0.33%) 且恒 >= 0, 方向另存 direction 列 ——
    #   端点必须转成有符号百分数, 否则前端幅度门槛会把所有 marker 过滤掉。
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
]


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """建临时库, 并把 bridge_server.DB_PATH 指过去。"""
    path = str(tmp_path / "signals.db")
    conn = sqlite3.connect(path)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    conn.close()

    import bridge_server as bs
    # 2026-11-18: db_cursor 已搬到 bridge_builders（bridge 的 DB_PATH 收口到 db_location），
    # 只 patch bridge_server.DB_PATH 已经管不到新的取连接口 —— 两处都要指过去。
    monkeypatch.setattr(bs, "DB_PATH", path)
    import bridge_builders
    monkeypatch.setattr(bridge_builders, "DB_PATH", path)
    return path


@pytest.fixture(scope="module")
def bridge_client():
    from fastapi.testclient import TestClient
    import bridge_server as bs
    return TestClient(bs.app)


@pytest.fixture()
def seeded(tmp_db):
    """造一份有时间线的数据 (相对当前时间, 不会随日期推移失效)。

      T-3h : news#1 → decision#1 BUY  XAU   (主源)
      T-2h : news#2 → decision#2 HOLD GOLD  (主源, 无方向)
      T-1h : news#3 → decision#3 SELL XAU   (主源)
             news#4 → decision#4 BUY  BTC sentiment_score=0 (主源, 中性分数)
      T-30m: reflection#1 BUY XAU pnl=+2.5 (已结算, 挂在 decision#1 上)
      T-45m: reflection#2 REJECTED XAU     (只有 REJECTED, 不该出现在兜底结果里)
      impact: XAU 涨 +5.0 (有事件链) / XAU 跌 -3.2 (无事件链) / 量纲污染行 (+9867)
    """
    now = int(time.time())

    def t(h):
        return now - int(h * 3600)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    for nid, ts, content in (
        (1, t(3), "黄金突破关键位"),
        (2, t(2), "黄金震荡整理"),
        (3, t(1), "XAU 短线承压"),
        (4, t(1), "BTC 无明确方向"),
    ):
        conn.execute(
            "INSERT INTO raw_news (id, source, content, timestamp, ts) VALUES (?,?,?,?,?)",
            (nid, "test", content, _bj_str(ts), ts),
        )

    for did, nid, action, asset, score, ts in (
        (1, 1, "BUY", "XAU", 0.82, t(3)),
        (2, 2, "HOLD", "GOLD", 0.10, t(2)),
        (3, 3, "SELL", "XAU", -0.71, t(1)),
        (4, 4, "BUY", "BTC", 0.0, t(1)),
    ):
        conn.execute(
            "INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action,"
            " reasoning, created_at, market_category, target_asset, event_strength,"
            " direct_catalyst, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, nid, score, action, f"{action} 推理链 #{did}",
             _bj_str(ts), "macro", asset, "strong" if action != "HOLD" else "weak",
             1 if action in ("BUY", "SELL") else 0, ts),
        )

    # 结算复盘: 挂在 decision#1 上 (主源 LEFT JOIN 补盈亏)
    conn.execute(
        "INSERT INTO trade_reflections_v2 (id, decision_id, ai_decision_id, asset, action,"
        " pnl_pct, confidence_at_time, hit, dimension, lesson, root_cause, news_id,"
        " created_at) VALUES (1,1,1,'XAU','BUY',2.5,0.66,1,'A','做对了','方向对',1,?)",
        (_bj_str(t(0.5)),),
    )
    # 只有 REJECTED —— 兜底查询只取 BUY/SELL, 这条不该出现
    conn.execute(
        "INSERT INTO trade_reflections_v2 (id, decision_id, ai_decision_id, asset, action,"
        " pnl_pct, confidence_at_time, hit, dimension, lesson, root_cause, news_id,"
        " created_at) VALUES (2,NULL,NULL,'XAU','REJECTED',NULL,0.1,0,'B','不该出现','',2,?)",
        (_bj_str(t(0.75)),),
    )

    # impact: 有事件链的涨 (+5.0) + 无事件链的跌 (-3.2) + 量纲污染行 (+9867)
    conn.execute(
        "INSERT INTO event_move_attribution (id, asset, move_ts, move_pct, move_kind,"
        " algo_score, duration_min, ev_news_id, ev_source, ev_action, ev_score)"
        " VALUES (1,'XAU',?,5.0,'fast_rise',0.9,10,1,'testsrc','BUY',0.8)",
        (t(3),),
    )
    conn.execute(
        "INSERT INTO event_move_attribution (id, asset, move_ts, move_pct, move_kind,"
        " algo_score, duration_min, ev_news_id, ev_source, ev_action, ev_score)"
        " VALUES (2,'XAU',?,-3.2,'fast_drop',0.6,5,NULL,'','',NULL)",
        (t(2),),
    )
    conn.execute(
        "INSERT INTO event_move_attribution (id, asset, move_ts, move_pct, move_kind,"
        " algo_score, duration_min, ev_news_id, ev_source, ev_action, ev_score)"
        " VALUES (3,'XAU',?,9867.0,'fast_rise',0.2,1,1,'testsrc','BUY',0.1)",
        (t(1),),
    )
    conn.commit()
    conn.close()
    return {"now": now}


@pytest.fixture()
def seeded_eki(seeded, tmp_db):
    """在 seeded 之上补 event_kline_impact 行 (主资产真实来源)。

    event_kline_impact 是**第一优先**数据源 —— 主资产 (XAU/BTC/WTI) 的真实事件影响
    只存在这张表里 (event_move_attribution 对这些品种是空的) → 端点必须优先用它。

    ⚠️ impact_pct 按 worker 的口径是**小数**: 0.0123 = 1.23%, 且恒 >= 0,
    方向另存在 direction 列。
    """
    now = int(time.time())

    def t(h):
        return now - int(h * 3600)

    conn = sqlite3.connect(tmp_db)
    # id, asset, event_time, impact_pct(小数), severity, direction, title
    rows = (
        (1, "XAU", t(4), 0.0123, "high", "up", "美联储官员鹰派讲话"),
        (2, "XAU", t(3), 0.0040, "medium", "down", "通胀数据高于预期"),
        (3, "XAU", t(2), 0.0060, "medium", "neutral", "官员讲话模棱两可"),
        (4, "XAU", t(1), 0.0010, "low", "up", "无关紧要的消息"),
        (5, "BTC", t(1), 0.0500, "high", "up", "BTC 相关消息"),
    )
    for rid, asset, ts, pct, sev, direction, title in rows:
        conn.execute(
            "INSERT INTO event_kline_impact (id, event_source, event_id, event_title,"
            " event_time, importance, asset, pre_event_close, max_high, min_low,"
            " impact_pct, impact_window_start, impact_window_end, impact_severity, direction)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "testsrc", str(rid), title, ts, "medium", asset, 100.0, 101.0, 99.0,
             pct, ts - 60, ts + 60, sev, direction),
        )
    conn.commit()
    conn.close()
    return seeded


def _bj_str(epoch: int) -> str:
    """epoch 秒 → 北京墙钟 'YYYY-MM-DDTHH:MM:SS' (T 分隔 = 北京, 免 +8h 平移)。"""
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone(_dt.timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )



# ─────────────────────────── /api/trading/signals ───────────────────────────

def test_signals_endpoint_not_swallowed_by_catch_all(bridge_client, seeded):
    """端点必须真的命中, 不能被 catch-all 兜底吃掉 (v317 踩过的坑)。"""
    r = bridge_client.get("/api/trading/signals")
    assert r.status_code == 200
    body = r.json()
    assert body.get("_bridge_fallback") is not True
    assert body.get("error") is None


def test_signals_primary_source_returns_buy_and_sell(bridge_client, seeded):
    """主源 = ai_decisions.suggested_action → 必须真的返回 BUY / SELL。

    这是本次修复的核心断言: 旧实现读 dimension (归因分类 A/B/C) 当方向,
    结果永远是 HOLD。
    """
    body = bridge_client.get("/api/trading/signals").json()
    assert body["ok"] is True
    assert body["source"] == "ai_decisions"
    actions = {s["action"] for s in body["signals"]}
    assert "BUY" in actions, f"主源应返回 BUY, 实际: {actions}"
    assert "SELL" in actions, f"主源应返回 SELL, 实际: {actions}"


def test_signals_enrich_with_reflection_pnl(bridge_client, seeded):
    """LEFT JOIN trade_reflections_v2 → 已结算信号要带上盈亏/置信度/命中。"""
    body = bridge_client.get("/api/trading/signals").json()
    buy = next(s for s in body["signals"] if s["id"] == 1)
    assert buy["action"] == "BUY"
    assert buy["pnl_pct"] == pytest.approx(2.5)
    assert buy["confidence"] == pytest.approx(0.66)
    assert buy["hit"] == 1


def test_signals_carry_news_title_and_reasoning(bridge_client, seeded):
    """信号要带真实新闻原文 (前端 tooltip 要用)。"""
    body = bridge_client.get("/api/trading/signals").json()
    buy = next(s for s in body["signals"] if s["id"] == 1)
    assert buy["news_title"], "必须带真实新闻标题"
    assert "黄金" in buy["news_title"]
    assert buy["reasoning"], "必须带推理链"
    assert buy["news_time"] > 0, "news_time 必须是 epoch 秒 (前端要落 K 线桶)"


def test_signals_hide_neutral_filters_hold_and_zero_score(bridge_client, seeded):
    """hide_neutral=1 → 只返回有方向的信号 (BUY/SELL 且 sentiment_score != 0)。

    场景里的 4 条: BUY(0.82) / HOLD(0.10) / SELL(-0.71) / BUY(0.0)
    → 只剩 2 条 (decision#1 与 decision#3)。
    """
    body = bridge_client.get("/api/trading/signals?hide_neutral=1").json()
    ids = {s["id"] for s in body["signals"]}
    assert ids == {1, 3}, f"hide_neutral 应只剩 #1/#3, 实际: {ids}"
    assert all(s["action"] in ("BUY", "SELL") for s in body["signals"])


def test_signals_include_neutral_by_default(bridge_client, seeded):
    """默认 (hide_neutral=0) → HOLD 也要返回, 前端按 action 上色, HOLD 不上 K 线。"""
    body = bridge_client.get("/api/trading/signals").json()
    ids = {s["id"] for s in body["signals"]}
    assert {1, 2, 3, 4} <= ids, f"默认应返回全部 4 条, 实际: {ids}"


def test_signals_asset_filter(bridge_client, seeded):
    """asset 参数只返回该标的 (UPPER 比对)。"""
    body = bridge_client.get("/api/trading/signals?asset=XAU").json()
    assert {s["asset"] for s in body["signals"]} == {"XAU"}
    body2 = bridge_client.get("/api/trading/signals?asset=xau").json()
    assert {s["asset"] for s in body2["signals"]} == {"XAU"}, "小写 asset 也应命中"


def test_signals_time_window_filters_outside(bridge_client, seeded):
    """from_ts/to_ts 窗口外的老信号必须被排除。"""
    now = seeded["now"]
    body = bridge_client.get(
        f"/api/trading/signals?from_ts={now - 3600}&to_ts={now}"
    ).json()
    ids = {s["id"] for s in body["signals"]}
    assert 1 not in ids, "3 小时前的信号不该出现在 1 小时窗口里"
    assert 3 in ids, "1 小时前的信号应该命中"


def test_signals_limit_caps_rows(bridge_client, seeded):
    """limit 必须真的截断。"""
    body = bridge_client.get("/api/trading/signals?limit=2").json()
    assert len(body["signals"]) == 2
    assert body["n"] == 2


def test_signals_fallback_to_reflections_when_no_buy_sell(bridge_client, tmp_db):
    """主源窗口内没有 BUY/SELL → 兜底 trade_reflections_v2.action (只取 BUY/SELL)。

    兜底必须:
      · 只出 BUY/SELL —— REJECTED 不能出现
      · news_time 用 created_at 换算成 epoch 秒 (前端要落 K 线桶)
    """
    now = int(time.time())
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO raw_news (id, source, content, timestamp, ts) VALUES (1,'t','旧消息',?,?)",
        (_bj_str(now - 7200), now - 7200),
    )
    conn.execute(
        "INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action,"
        " reasoning, created_at, market_category, target_asset, event_strength,"
        " direct_catalyst, ts) VALUES (1,1,0.0,'HOLD','hold',?,'macro','XAU','weak',0,?)",
        (_bj_str(now - 7200), now - 7200),
    )
    conn.execute(
        "INSERT INTO trade_reflections_v2 (id, decision_id, ai_decision_id, asset, action,"
        " pnl_pct, confidence_at_time, hit, dimension, lesson, root_cause, news_id,"
        " created_at) VALUES (1,NULL,NULL,'XAU','BUY',1.5,0.5,1,'A','ok','',NULL,?)",
        (_bj_str(now - 3600),),
    )
    conn.execute(
        "INSERT INTO trade_reflections_v2 (id, decision_id, ai_decision_id, asset, action,"
        " pnl_pct, confidence_at_time, hit, dimension, lesson, root_cause, news_id,"
        " created_at) VALUES (2,NULL,NULL,'XAU','REJECTED',NULL,0.1,0,'B','x','',NULL,?)",
        (_bj_str(now - 3600),),
    )
    conn.commit()
    conn.close()

    body = bridge_client.get("/api/trading/signals").json()
    assert body["source"].startswith("trade_reflections_v2"), body["source"]
    actions = [s["action"] for s in body["signals"]]
    assert actions == ["BUY"], f"兜底只应返回 BUY, 实际: {actions}"
    assert body["signals"][0]["news_time"] > 0, "兜底时 news_time 必须换算成 epoch 秒"
    assert body["signals"][0]["pnl_pct"] == pytest.approx(1.5)


# ─────────────────────── /api/v201/impact/by-asset ───────────────────────

def test_impact_direction_reflects_price_move_not_event_action(bridge_client, seeded):
    """方向必须来自**涨跌幅符号**, 不是 ev_action。

    场景: +5.0 且 ev_action=BUY (方向一致), -3.2 且 ev_action 空。
    前端要靠 move_direction 上色 (涨绿跌红), 不再一律红。
    """
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    assert body.get("_bridge_fallback") is not True
    items = {it["event_id"]: it for it in body["items"]}

    up = items["ema-1"]
    assert up["move_direction"] == "up"
    assert up["impact_pct"] == pytest.approx(5.0)
    assert up["event_backed"] is True, "有 ev_news_id → event_backed=True"

    down = items["ema-2"]
    assert down["move_direction"] == "down"
    assert down["event_backed"] is False, "无 ev_news_id → event_backed=False"


def test_impact_drops_implausible_move_pct(bridge_client, seeded):
    """量纲污染行 (9867%) 必须被剔除, 不能以 critical severity 混进影响面板。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    ids = {it["event_id"] for it in body["items"]}
    assert "ema-3" not in ids, "9867% 的假异动必须被剔除"
    assert all(abs(it["impact_pct"]) <= 90 for it in body["items"])


def test_impact_min_abs_pct_filter(bridge_client, seeded):
    """min_abs_pct 参数: 幅度门槛在服务端生效。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU&min_abs_pct=4").json()
    ids = {it["event_id"] for it in body["items"]}
    assert ids == {"ema-1"}, f"min_abs_pct=4 应只剩 +5.0 那条, 实际: {ids}"

    body2 = bridge_client.get("/api/v201/impact/by-asset?asset=XAU&min_abs_pct=0").json()
    assert len(body2["items"]) == 2, "默认不过滤时两条都在"


def test_impact_severity_min_filter(bridge_client, seeded):
    """severity_min=critical → 只留 |pct|>=7 的 (本场景两条都不满足 → 空)。"""
    body = bridge_client.get(
        "/api/v201/impact/by-asset?asset=XAU&severity_min=critical"
    ).json()
    assert body["items"] == []


def test_impact_has_event_title_and_severity(bridge_client, seeded):
    """前端 ImpactRow 契约: event_title / impact_severity / impact_pct 必须存在。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    for it in body["items"]:
        assert it["event_title"], "event_title 不能为空 (前端直接 continue 掉空标题)"
        assert it["impact_severity"] in ("low", "medium", "high", "critical")
        assert it["impact_pct"] is not None


# ─────────────────── v319: event_kline_impact 成为第一优先数据源 ───────────────────

def test_impact_prefers_event_kline_impact_for_main_assets(bridge_client, seeded_eki):
    """主资产 (XAU) 的真实事件影响只在 event_kline_impact 里 → 端点必须优先用它。

    背景: 之前端点只读 event_move_attribution (主资产为空) + critical_price_moves,
    对 XAU 恒返回空 → 归因页 K 线上一个 impact marker 都没有。
    """
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    assert body["source"] == "event_kline_impact", body["source"]
    ids = {it["event_id"] for it in body["items"]}
    assert "eki-1" in ids, ids
    assert not any(i.startswith("ema-") for i in ids), "有 eki 数据时不该回退到 event_move_attribution"
    assert "eki-5" not in ids, "别的品种不该混进 XAU 的结果"


def test_impact_normalizes_eki_units_and_direction(bridge_client, seeded_eki):
    """impact_pct (小数) → 有符号百分数; 方向来自 direction 列, 不是符号。

    · 0.0123 (小数) = 1.23% → impact_pct=+1.23 (direction=up)
    · direction=down → impact_pct 为负 (前端要靠它涨绿跌红)
    · direction=neutral → impact_pct=0, 但幅度仍在 impact_abs_pct (前端门槛按幅度判)
    """
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    items = {it["event_id"]: it for it in body["items"]}

    up = items["eki-1"]
    assert up["impact_pct"] == pytest.approx(1.23)
    assert up["impact_abs_pct"] == pytest.approx(1.23)
    assert up["move_direction"] == "up"
    assert up["event_backed"] is True, "eki 来自 news_events → event_backed=True"
    assert up["impact_severity"] == "high"
    assert up["event_title"] == "美联储官员鹰派讲话"

    down = items["eki-2"]
    assert down["impact_pct"] == pytest.approx(-0.4)
    assert down["impact_abs_pct"] == pytest.approx(0.4)
    assert down["move_direction"] == "down"

    neutral = items["eki-3"]
    assert neutral["impact_pct"] == pytest.approx(0.0)
    assert neutral["impact_abs_pct"] == pytest.approx(0.6)
    assert neutral["move_direction"] == "flat"


def test_impact_eki_min_abs_pct_and_severity_filters(bridge_client, seeded_eki):
    """幅度门槛按**幅度**算 (0.6% 的 neutral 事件幅度就是 0.6)。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU&min_abs_pct=0.5").json()
    ids = {it["event_id"] for it in body["items"]}
    assert ids == {"eki-1", "eki-3"}, ids

    body2 = bridge_client.get("/api/v201/impact/by-asset?asset=XAU&severity_min=high").json()
    ids2 = {it["event_id"] for it in body2["items"]}
    assert ids2 == {"eki-1"}, ids2


def test_impact_severity_filter_not_truncated_by_limit(bridge_client, seeded_eki):
    """severity 门槛必须在 LIMIT **之前**生效。

    eki-4 是最新的一行但 severity=low; eki-1 更早但 severity=high。
    limit=1 + severity_min=high → 必须还能拿到 eki-1 (若先 LIMIT 再过滤, 这里会是空)。
    """
    body = bridge_client.get(
        "/api/v201/impact/by-asset?asset=XAU&severity_min=high&limit=1"
    ).json()
    ids = {it["event_id"] for it in body["items"]}
    assert ids == {"eki-1"}, ids


def test_impact_order_magnitude_returns_biggest_first(bridge_client, seeded_eki):
    """order=magnitude → 幅度最大的排前面。

    归因页要的是"这段窗口里最大的几次冲击"; 旧口径只按时间排序 + LIMIT,
    XAU 每天 130+ 条事件 → 200 行只覆盖 1.5 天, 30d 图左侧全空。
    """
    body = bridge_client.get(
        "/api/v201/impact/by-asset?asset=XAU&order=magnitude"
    ).json()
    # ⚠️ 按**幅度**排 (impact_abs_pct), 不能按有符号 impact_pct:
    #    neutral 事件的 impact_pct=0 (方向未知, 幅度仍在 impact_abs_pct 里)。
    mags = [it["impact_abs_pct"] for it in body["items"]]
    assert mags == sorted(mags, reverse=True), mags
    assert body["items"][0]["event_id"] == "eki-1"  # 1.23% 最大


def test_impact_order_magnitude_survives_limit(bridge_client, seeded_eki):
    """order=magnitude + limit=1 → 留最大的那条 (eki-1), 而不是最新的 (eki-4)。"""
    body = bridge_client.get(
        "/api/v201/impact/by-asset?asset=XAU&order=magnitude&limit=1"
    ).json()
    assert {it["event_id"] for it in body["items"]} == {"eki-1"}


def test_impact_order_time_returns_newest_first(bridge_client, seeded_eki):
    """默认 order=time → 最新优先。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    ts = [it["event_time"] for it in body["items"]]
    assert ts == sorted(ts, reverse=True), ts
    assert body["items"][0]["event_id"] == "eki-4"


def test_impact_falls_back_to_event_move_attribution_when_no_eki(bridge_client, seeded):
    """eki 没数据 → 降级到 event_move_attribution (老行为保持不变)。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    assert body["source"] == "event_move_attribution", body["source"]
    assert {it["event_id"] for it in body["items"]} == {"ema-1", "ema-2"}


def test_impact_honest_empty_when_no_rows_anywhere(bridge_client, tmp_db):
    """三张表都没数据 → 诚实报空, 不伪造 marker。"""
    body = bridge_client.get("/api/v201/impact/by-asset?asset=XAU").json()
    assert body["items"] == []
    assert body["n"] == 0
    assert body["source"] == "none", body["source"]



def test_signals_honest_empty_when_nothing(bridge_client, tmp_db):
    """空库 → 必须诚实报空 (honest_empty=True), 不能伪造信号。"""
    body = bridge_client.get("/api/trading/signals").json()
    assert body["signals"] == []
    assert body["n"] == 0
    assert body["honest_empty"] is True
