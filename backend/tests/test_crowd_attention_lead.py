# -*- coding: utf-8 -*-
"""Crowd Attention Lead 回归测试（v317）。

检验命题：**跨平台注意力是否先于有意义的行情移动**。

覆盖：
  P0  标的识别：标题正则 + topic_key 结构化键 → 正确映射到 kline symbol
  P1  时间对齐：published_at(UTC) ↔ kline ts(epoch ms) 同一基准
  P2  价格移动检测：前向（注意力之后）与反向（注意力之前）两个方向
  P3  verdict 判定 —— 本模块核心：表面命中率高但反向对照也高时，
        必须判 attention_lags_price，绝不能因"命中率 90%"就报成"注意力领先"
  P4  干净子集：剔除「价格已先动」的样本后重算命中率
  P5  先行漏检候选：已兑现的不算候选；系统已 BUY/SELL 的标 already_acted
  P6  归因证据：找离异动最近的那条注意力事件
  P7  采集层：parse_gnews 的 source 参数 + SourceError 不被吞掉

全部离线：无网络、无 LLM、不碰生产库（用 :memory:）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import crowd_attention_lead as cal
import crowd_sources as cs


# ─────────────────────── 时间锚点（v321 时间炸弹修复） ───────────────────────
# 命题不变：用例造"注意力事件 + 对应行情"，再验证 verdict。
# 但 collect_attention_events / evaluate_lead / pending_attention 都按
# `since_ms = now - lookback_hours*3600*1000` 过滤 —— 原用例把事件时间**硬编码**
# 成 "2026-10-30"，随着时间推移（今天已到 2026-11-04）事件整体滑出 72h 窗口，
# 用例集体变成"假失败"（events_total 从 1 变 0），与被测逻辑无关。
# 改为以**运行时刻**为锚点生成相对时间：语义完全不变，且永不过期。
_NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _ago(hours: float) -> str:
    """返回 (now - hours) 的 "YYYY-MM-DD HH:MM:SS"（UTC 字符串）。"""
    return (_NOW - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────── 内存库 ───────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
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
            day TEXT NOT NULL,
            month TEXT NOT NULL,
            topic_key TEXT NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE kline_bars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(symbol, timeframe, ts)
        )
    """)
    conn.execute("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_asset TEXT NOT NULL,
            suggested_action TEXT NOT NULL,
            ts INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT ''
        )
    """)
    return conn


def _add_item(conn, source, title, published_at, url="https://x.test/a",
              topic_key="", channel="media"):
    # day/month 从 published_at 派生（原来硬编码 "2026-10-30"/"2026-10"，
    # 换成相对时间后必须同步派生，否则展示列与事件时间对不上）
    conn.execute(
        """INSERT INTO crowd_source_items
           (source, external_id, title, url, media_channel, published_at,
            collected_at, day, month, topic_key)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (source, "%s:%s" % (source, title), title, url, channel,
         published_at, published_at, published_at[:10], published_at[:7],
         topic_key or "text:%s" % title[:20]),
    )
    conn.commit()


def _add_bars(conn, symbol, base_ts, closes, step_ms=60_000, timeframe="1m"):
    for i, close in enumerate(closes):
        conn.execute(
            "INSERT OR REPLACE INTO kline_bars (symbol,timeframe,ts,close) VALUES (?,?,?,?)",
            (symbol, timeframe, base_ts + i * step_ms, close),
        )
    conn.commit()


# ─────────────────────────── P0 标的识别 ───────────────────────────

def test_detect_symbols_from_title():
    assert cal.detect_symbols("Bitcoin price surges past $120K") == ["BTC"]
    assert cal.detect_symbols("Ethereum ETF inflows hit record") == ["ETH"]
    assert cal.detect_symbols("Solana network outage resolved") == ["SOL"]
    assert cal.detect_symbols("Gold price climbs to new high") == ["XAU"]
    assert cal.detect_symbols("Silver price slips") == ["XAG"]
    assert cal.detect_symbols("Crude oil prices fall sharply") == ["WTI"]
    # 不认识的文本不猜
    assert cal.detect_symbols("A股三大指数集体收跌") == []
    assert cal.detect_symbols("") == []


def test_symbol_of_item_prefers_topic_key():
    assert cal.symbol_of_item("asset:CRYPTO:BTC", "some random title") == "BTC"
    # topic_key 不认识时退化到标题
    assert cal.symbol_of_item("asset:沪A:600519", "Bitcoin ETF news") == "BTC"


# ─────────────────────────── P1 时间对齐 ───────────────────────────

def test_parse_utc_ms():
    assert cal.parse_utc_ms("2026-10-30 10:21:32") == 1793355692000
    assert cal.parse_utc_ms("") == 0
    assert cal.parse_utc_ms("not a time") == 0


def test_fmt_utc_roundtrip():
    ms = cal.parse_utc_ms("2026-10-30 10:21:32")
    assert cal.fmt_utc(ms) == "2026-10-30 10:21:32"
    assert cal.fmt_utc(0) == ""


# ─────────────────────────── P2 价格移动检测 ───────────────────────────

def test_price_move_after_detects_forward_move():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    closes = [100.0] * 5 + [101.0] + [101.0] * 5
    _add_bars(conn, "BTC", base, closes)
    res = cal.price_move_after(conn, "BTC", base, horizon_minutes=60,
                               move_pct=0.5, timeframe="1m")
    assert res["has_move"] is True
    assert res["move_ts"] == base + 5 * 60_000
    assert res["lead_minutes"] == 5
    assert res["move_pct"] == pytest.approx(1.0, rel=1e-6)
    conn.close()


def test_price_move_after_no_move():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    _add_bars(conn, "BTC", base, [100.0] * 20)
    res = cal.price_move_after(conn, "BTC", base, horizon_minutes=60,
                               move_pct=0.5, timeframe="1m")
    assert res["has_move"] is False
    assert res["max_abs_move_pct"] == 0.0
    assert res["bars"] == 20
    conn.close()


def test_price_move_before_detects_prior_move():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    closes = [100.0] + [101.0] * 15
    _add_bars(conn, "BTC", base, closes)
    res = cal.price_move_before(conn, "BTC", base + 15 * 60_000,
                                 horizon_minutes=60, move_pct=0.5,
                                 timeframe="1m")
    assert res["prior_move"] is True
    assert res["reverse_lead_minutes"] == 14
    conn.close()


def test_price_move_before_no_prior_move():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    _add_bars(conn, "BTC", base, [100.0] * 15)
    res = cal.price_move_before(conn, "BTC", base + 14 * 60_000,
                                 horizon_minutes=60, move_pct=0.5,
                                 timeframe="1m")
    assert res["prior_move"] is False
    conn.close()




# ─────────────────────────── P3/P4 verdict 判定 ───────────────────────────

def _scenario(conn, symbol, attention_at, prior_move, forward_move):
    """造一个注意力事件 + 对应行情。

    prior_move=True  → 注意力之前价格已动过（媒体追价格的典型形态）
    forward_move=True → 注意力之后价格才动（注意力真有领先性的形态）
    """
    base = cal.parse_utc_ms(attention_at)
    _add_item(conn, "gnews_crypto", "%s price story" % symbol, attention_at)
    # 行情：注意力时刻往前 60 分钟，往后 60 分钟，每分钟一根
    closes = []
    for i in range(121):
        if i < 60:
            closes.append(100.0 + (1.0 if (prior_move and i >= 30) else 0.0))
        else:
            closes.append(100.0 + (1.0 if prior_move else 0.0)
                          + (1.0 if (forward_move and i >= 70) else 0.0))
    _add_bars(conn, symbol, base - 60 * 60_000, closes)


def test_verdict_attention_lags_price_not_mistaken_for_lead():
    """反向对照必须推翻"表面命中率很高"的错觉。

    场景：注意力之后价格确实动了（表面命中率高），
    但注意力**之前**价格就已经动过 → 判定必须是 attention_lags_price。
    """
    conn = _make_conn()
    _scenario(conn, "BTC", _ago(2), prior_move=True, forward_move=True)
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=1,
                            move_pct=0.5, horizon_minutes=60, timeframe="1m")
    s = res["summary"]
    assert s["events_total"] == 1
    assert s["events_with_move"] == 1
    assert s["hit_rate"] == 1.0            # 表面命中率 100%
    assert s["events_with_prior_move"] == 1  # 但价格先动了
    assert s["prior_move_ratio"] == 1.0
    # 关键断言：不能因为 hit_rate=1.0 就说"注意力领先"
    assert s["verdict"] == "attention_lags_price"
    assert s["clean_events"] == 0
    assert s["clean_hit_rate"] is None
    assert "注意力领先价格移动" not in res["note"]
    conn.close()


def test_verdict_attention_leads_price_when_no_prior_move():
    """注意力之前价格没动、之后动了 → 判定为注意力领先。"""
    conn = _make_conn()
    _scenario(conn, "BTC", _ago(2), prior_move=False, forward_move=True)
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=1,
                            move_pct=0.5, horizon_minutes=60, timeframe="1m")
    s = res["summary"]
    assert s["hit_rate"] == 1.0
    assert s["events_with_prior_move"] == 0
    assert s["clean_events"] == 1
    assert s["clean_hit_rate"] == 1.0
    assert s["verdict"] == "attention_leads_price"
    conn.close()


def test_verdict_no_data_when_no_price():
    conn = _make_conn()
    _add_item(conn, "gnews_crypto", "Bitcoin price story", _ago(2))
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=1,
                            move_pct=0.5, horizon_minutes=60, timeframe="1m")
    assert res["summary"]["events_total"] == 1
    assert res["summary"]["events_with_price"] == 0
    assert res["summary"]["verdict"] == "no_data"
    assert res["summary"]["hit_rate"] is None
    conn.close()


def test_evaluate_lead_empty_returns_none_not_placeholder():
    """样本不足时不得用 0 / 占位数字冒充结论。"""
    conn = _make_conn()
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=1)
    assert res["summary"]["events_total"] == 0
    assert res["summary"]["hit_rate"] is None
    assert res["summary"]["clean_hit_rate"] is None
    assert res["events"] == []
    assert "不做任何推断" in res["note"]
    conn.close()


def test_clean_subset_excludes_prior_move_samples():
    """干净子集只统计"价格没有先动"的事件。"""
    conn = _make_conn()
    _scenario(conn, "BTC", _ago(3), prior_move=True, forward_move=True)
    _scenario(conn, "ETH", _ago(2), prior_move=False, forward_move=True)
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=1,
                            move_pct=0.5, horizon_minutes=60, timeframe="1m")
    s = res["summary"]
    assert s["events_total"] == 2
    assert s["events_with_prior_move"] == 1
    assert s["clean_events"] == 1
    assert s["clean_hit_rate"] == 1.0
    conn.close()


def test_min_platforms_filters_single_platform():
    conn = _make_conn()
    _scenario(conn, "BTC", _ago(2), prior_move=False, forward_move=True)
    res = cal.evaluate_lead(conn, lookback_hours=72, min_platforms=2,
                            move_pct=0.5, horizon_minutes=60, timeframe="1m")
    assert res["summary"]["events_total"] == 0
    conn.close()


def test_collect_events_requires_real_timestamp():
    """没有真实发布时间戳的条目不产生注意力事件（采集时刻≠出现时刻）。"""
    conn = _make_conn()
    conn.execute(
        """INSERT INTO crowd_source_items
           (source, external_id, title, url, media_channel, published_at,
            collected_at, day, month, topic_key)
           VALUES ('coingecko','x','Bitcoin coin','u','cg','',
                   '2026-10-30T19:10:19','2026-10-30','2026-10','asset:CRYPTO:BTC')""")
    conn.commit()
    assert cal.collect_attention_events(conn, lookback_hours=72) == []
    conn.close()


def test_collect_events_counts_platforms():
    # v321.5: 修真实病因 —— 聚类按**整点小时**归桶（ts_ms // 3600_000）。
    # 旧写法用 _ago(2) 与 _ago(1.9)，两条目相差 6 分钟，是否跨整点取决于
    # 运行时刻：约 90% 概率同桶(通过)，约 10% 跨整点 → 拆成 2 个事件(失败)。
    # 全量跑偶发失败、单独跑必过，就是这个原因，与窗口 lookback 无关。
    # 现在把两条目**钉在同一个整点小时内**，与运行时刻彻底解耦。
    base = int(_NOW.replace(tzinfo=timezone.utc).timestamp()) // 3600 * 3600
    base -= 2 * 3600                      # 往前 2 小时, 稳在 72h 窗口内
    ts_a = datetime.fromtimestamp(base + 60, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ts_b = datetime.fromtimestamp(base + 6 * 60, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    anchor = (base + 3600) * 1000         # 窗口锚在该整点小时末
    conn = _make_conn()
    _add_item(conn, "gnews_crypto", "Bitcoin rises", ts_a)
    _add_item(conn, "coingecko", "Bitcoin coin", ts_b,
              topic_key="asset:CRYPTO:BTC")
    evs = cal.collect_attention_events(
        conn, lookback_hours=72, min_platforms=1, anchor_ms=anchor)
    assert len(evs) == 1
    assert evs[0]["platform_count"] == 2
    assert evs[0]["item_count"] == 2
    assert sorted(evs[0]["platform_list"]) == ["coingecko", "gnews_crypto"]
    # 两个平台构成跨平台共识 → min_platforms=2 应保留
    assert len(cal.collect_attention_events(
        conn, lookback_hours=72, min_platforms=2, anchor_ms=anchor)) == 1
    conn.close()


def test_collect_events_single_platform_filtered_by_min_platforms():
    """只有一个平台时，min_platforms=2 必须把它过滤掉（不构成跨平台共识）。"""
    conn = _make_conn()
    _add_item(conn, "gnews_crypto", "Bitcoin rises", _ago(2))
    assert len(cal.collect_attention_events(
        conn, lookback_hours=72, min_platforms=1)) == 1
    assert cal.collect_attention_events(conn, lookback_hours=72, min_platforms=2) == []
    conn.close()


# ─────────────────────────── P5 先行漏检候选 ───────────────────────────

def test_pending_attention_excludes_already_realized():
    """价格已经兑现的事件不算"待兑现"候选。"""
    conn = _make_conn()
    _scenario(conn, "BTC", _ago(2), prior_move=False, forward_move=True)
    res = cal.pending_attention(conn, lookback_hours=72, min_platforms=1,
                                move_pct=0.5, horizon_minutes=60, timeframe="1m")
    assert res["summary"]["events_with_move"] == 1
    assert res["pending"] == []
    conn.close()


def test_pending_attention_keeps_unrealized_event():
    conn = _make_conn()
    # 价格一直平 → 没有兑现 → 是候选
    base = cal.parse_utc_ms(_ago(2))
    _add_item(conn, "gnews_crypto", "BTC price story", _ago(2))
    _add_bars(conn, "BTC", base - 30 * 60_000, [100.0] * 90)
    res = cal.pending_attention(conn, lookback_hours=72, min_platforms=1,
                                move_pct=0.5, horizon_minutes=60, timeframe="1m")
    assert len(res["pending"]) == 1
    p = res["pending"][0]
    assert p["symbol"] == "BTC"
    assert p["already_acted"] is False
    assert p["system_signal"] is None
    assert "先行漏检候选" in p["explain"]
    conn.close()


def test_pending_attention_marks_already_acted():
    """系统已经 BUY/SELL 的，标 already_acted（不算真漏检）。"""
    conn = _make_conn()
    base = cal.parse_utc_ms(_ago(2))
    _add_item(conn, "gnews_crypto", "BTC price story", _ago(2))
    _add_bars(conn, "BTC", base - 30 * 60_000, [100.0] * 90)
    conn.execute(
        """INSERT INTO ai_decisions (target_asset, suggested_action, ts, created_at)
           VALUES ('BTC','BUY',?,?)""",
        (base // 1000 + 60, _ago(1.9)))
    conn.commit()
    res = cal.pending_attention(conn, lookback_hours=72, min_platforms=1,
                                move_pct=0.5, horizon_minutes=60, timeframe="1m")
    assert len(res["pending"]) == 1
    assert res["pending"][0]["already_acted"] is True
    assert res["pending"][0]["system_signal"]["suggested_action"] == "BUY"
    conn.close()


# ─────────────────────────── P6 归因证据 ───────────────────────────

def test_attribution_evidence_finds_nearest_event():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC old story", "2026-10-30 09:00:00")
    _add_item(conn, "gnews_crypto", "BTC near story", "2026-10-30 10:30:00")
    ev = cal.attribution_evidence(conn, "BTC", base + 60 * 60_000,
                                  window_minutes=120, min_platforms=1)
    assert ev is not None
    assert ev["kind"] == "crowd_attention"
    assert ev["lead_minutes"] == 30
    assert "已被" in ev["explain"]
    conn.close()


def test_attribution_evidence_none_when_outside_window():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC story", "2026-10-30 08:00:00")
    assert cal.attribution_evidence(conn, "BTC", base, window_minutes=30,
                                    min_platforms=1) is None
    conn.close()


def test_attribution_evidence_none_for_other_asset():
    conn = _make_conn()
    base = cal.parse_utc_ms("2026-10-30 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC story", "2026-10-30 09:30:00")
    assert cal.attribution_evidence(conn, "ETH", base, window_minutes=120,
                                    min_platforms=1) is None
    conn.close()


# ─────────────────── P6b 回看窗口锚点（v318 回归） ───────────────────
# 命题：归因证据的回看窗口必须锚在**被归因的异动时刻**，而不是 wall-clock now。
# 否则给历史异动做归因时窗口落在「今天往前 N 小时」，异动当时的注意力条目
# 全部落在窗口外 → 历史异动永远没有证据（归因恒空）。

def test_attribution_evidence_works_for_historical_move():
    """历史异动（几个月前）依然能找到当时的注意力证据。"""
    conn = _make_conn()
    move_ts = cal.parse_utc_ms("2026-03-05 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC old story", "2026-03-05 09:00:00")
    ev = cal.attribution_evidence(conn, "BTC", move_ts,
                                  window_minutes=120, min_platforms=1)
    assert ev is not None, "历史异动的归因证据不应因 wall-clock 漂移而消失"
    assert ev["lead_minutes"] == 60
    conn.close()


def test_attribution_evidence_ignores_events_after_the_move():
    """锚定后只取异动**之前**的注意力（no-lookahead）。"""
    conn = _make_conn()
    move_ts = cal.parse_utc_ms("2026-03-05 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC later story", "2026-03-05 10:30:00")
    assert cal.attribution_evidence(conn, "BTC", move_ts, window_minutes=120,
                                    min_platforms=1) is None
    conn.close()


def test_collect_events_anchor_ms_bounds_the_window():
    """anchor_ms 显式指定时，窗口 = [anchor - lookback, anchor]。"""
    conn = _make_conn()
    anchor = cal.parse_utc_ms("2026-03-05 10:00:00")
    _add_item(conn, "gnews_crypto", "BTC inside", "2026-03-05 09:30:00")
    _add_item(conn, "gnews_crypto", "BTC outside", "2026-03-04 09:30:00")
    evs = cal.collect_attention_events(conn, lookback_hours=2, min_platforms=1,
                                       anchor_ms=anchor)
    titles = {it["title"] for e in evs for it in e["items"]}
    assert "BTC inside" in titles
    assert "BTC outside" not in titles
    conn.close()


def test_collect_events_anchor_ms_none_uses_now():
    """anchor_ms=None 保持「最近 N 小时」的实时语义（不回归）。"""
    conn = _make_conn()
    now = cal._dt.datetime.now(cal._dt.timezone.utc)
    fresh = now.strftime("%Y-%m-%d %H:%M:%S")
    _add_item(conn, "gnews_crypto", "BTC fresh", fresh)
    evs = cal.collect_attention_events(conn, lookback_hours=2, min_platforms=1)
    assert any("BTC fresh" == it["title"] for e in evs for it in e["items"])
    conn.close()


# ─────────────────────────── P7 采集层 ───────────────────────────

GNEWS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Bitcoin tumbles as exchange reports hack - CNBC</title>
  <link>https://news.google.com/rss/abc</link>
  <pubDate>Fri, 30 Oct 2026 08:34:06 GMT</pubDate>
  <source url="https://www.cnbc.com">CNBC</source>
</item>
</channel></rss>
"""


def test_parse_gnews_source_param():
    """同一服务不同语料池 → 不同 source（避免虚假跨平台共识）。"""
    a = cs.parse_gnews(GNEWS_XML)
    b = cs.parse_gnews(GNEWS_XML, source="gnews_crypto")
    assert len(a) == 1 and len(b) == 1
    assert a[0].source == "gnews"
    assert b[0].source == "gnews_crypto"
    assert a[0].external_id != b[0].external_id
    # 真实 pubDate 必须解析出来（UTC）
    assert a[0].published_at == "2026-10-30 08:34:06"
    assert a[0].author == "CNBC"


def test_source_error_is_not_swallowed():
    """采集失败必须抛出 SourceError（带真实原因），不能静默变成空列表。

    v317 修的真实 bug：原实现 `except Exception: return []` 把 CoinGecko 的
    429 限流吞成了空列表，上层只看到"empty result"，看不到真正的原因。
    """
    def boom(url, timeout=None, headers=None, attempts=3):
        raise cs.SourceError("coingecko → HTTP 429 (body 0 bytes)")
    orig = cs._http_get
    cs._http_get = boom
    try:
        with pytest.raises(cs.SourceError) as ei:
            cs.fetch_coingecko_trending()
    finally:
        cs._http_get = orig
    assert "429" in str(ei.value)


def test_collect_records_source_error():
    """collect() 必须把失败源记进 errors，而不是假装成功。"""
    def boom():
        raise cs.SourceError("coingecko → HTTP 429 (body 0 bytes)")
    orig = cs.SOURCE_FETCHERS["coingecko"]
    cs.SOURCE_FETCHERS["coingecko"] = boom
    try:
        res = cs.collect(["coingecko"])
    finally:
        cs.SOURCE_FETCHERS["coingecko"] = orig
    assert res["per_source"]["coingecko"] == 0
    assert "coingecko" in res["errors"]
    assert "429" in res["errors"]["coingecko"]
