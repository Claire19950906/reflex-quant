#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v506：锚点回放的行为测试（anchor_replay 从 0% 起步）。

这个模块的全部价值在于"不偷看未来"：把决策放回新闻发布那一刻，
只用锚点及之前的 K 线重跑，再按锚点价结算。所以测试的重点不是覆盖率，
而是死钉四条纪律：
  ① 快照里绝不出现锚点之后的 bar（这是它跟 backdated_entry_replay 的唯一区别）；
  ② 细档行情过期就退粗档，全档都不新鲜时返回空价——宁缺勿假；
  ③ 微观结构（资金费率/OI）历史不可还原，必须写 N/A，不能编数；
  ④ 只跑"真做过分析、有标的、非降级"的决策，降级和别的品种一律不进样本。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import anchor_replay as ar

ANCHOR = 1_700_000_000.0     # 固定锚点，避免依赖今天


# ── 库 ───────────────────────────────────────────────────────
@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE kline_bars(symbol TEXT, timeframe TEXT, ts INTEGER,
                                open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, news_id INTEGER, target_asset TEXT,
                                  suggested_action TEXT, sentiment_score REAL, forward_pnl REAL,
                                  analysis_degraded INTEGER, created_at TEXT);
        CREATE TABLE raw_news(id INTEGER PRIMARY KEY, ts INTEGER, content TEXT);
    """)
    c.commit()
    return c


def _bars(c, symbol, tf, anchor, offsets_sec, step, base=100.0):
    """在锚点前后插 bar：offsets_sec 为负=锚点之前。"""
    for i, off in enumerate(offsets_sec):
        ts = int((anchor + off) * 1000)
        p = base + i * 1.0
        c.execute("INSERT INTO kline_bars VALUES(?,?,?,?,?,?,?,?)",
                  (symbol, tf, ts, p, p + 1, p - 1, p, 10.0))
    c.commit()


# ── 时间归一 ────────────────────────────────────────────────
def test_epoch_handles_ms_and_sec_and_garbage():
    assert ar._epoch(1_700_000_000_123) == pytest.approx(1_700_000_000.123)
    assert ar._epoch("1700000000") == pytest.approx(1_700_000_000.0)
    assert ar._epoch(None) is None
    assert ar._epoch("not-a-number") is None


# ── 纪律①：绝不取到锚点之后的 bar ────────────────────────────
def test_bars_excludes_future(db):
    _bars(db, "BTC", "1h", ANCHOR, [-7200, -3600, 0, 3600, 7200], 3600)
    px = ar.AnchorPrices(db)
    got = px.bars("BTC", ANCHOR, 10, "1h")
    assert len(got) == 3, "锚点之后的两根 bar 不许出现"
    assert all(b["t"] <= ANCHOR for b in got)
    assert [b["t"] for b in got] == sorted(b["t"] for b in got), "必须升序"


def test_bars_respects_n_and_order(db):
    _bars(db, "BTC", "1h", ANCHOR, [-4 * 3600, -3 * 3600, -2 * 3600, -3600], 3600)
    px = ar.AnchorPrices(db)
    got = px.bars("BTC", ANCHOR, 2, "1h")
    assert len(got) == 2
    assert got[-1]["t"] == pytest.approx(ANCHOR - 3600), "最近的一根必须是锚点前一根"


def test_bars_missing_symbol_returns_empty(db):
    px = ar.AnchorPrices(db)
    assert px.bars("ETH", ANCHOR, 10, "1h") == []


def test_bars_unknown_timeframe_returns_empty(db):
    px = ar.AnchorPrices(db)
    assert px.bars("BTC", ANCHOR, 10, "2h") == []


# ── 纪律②：过期就退档，全过期宁缺勿假 ────────────────────────
def test_close_at_prefers_finest_fresh(db):
    # 1m 序列跨过锚点（含一根锚点之后的 bar），最新锚点前一根在 60 秒前
    _bars(db, "BTC", "1m", ANCHOR, [-60, 60], 60, base=100.0)
    _bars(db, "BTC", "1h", ANCHOR, [-3600, 3600], 3600, base=50.0)
    px = ar.AnchorPrices(db)
    price, tf, stale = px.close_at("BTC", ANCHOR)
    assert tf == "1m" and price == pytest.approx(100.0)
    assert stale == pytest.approx(60.0)


def test_close_at_falls_back_when_1m_is_stale(db):
    # 1m 序列跨过锚点，但锚点附近断档：最近一根在 1 小时前，超过 900 秒上限
    _bars(db, "BTC", "1m", ANCHOR, [-3600, 1800], 60, base=100.0)
    _bars(db, "BTC", "1h", ANCHOR, [-3600, 3600], 3600, base=50.0)
    px = ar.AnchorPrices(db)
    price, tf, stale = px.close_at("BTC", ANCHOR)
    assert tf == "1h", "细档过期必须退粗档"
    assert price == pytest.approx(50.0)


def test_close_at_never_fabricates_when_all_stale(db):
    # 只有一根很久以前的 1d，远超 1d 的 90000 秒上限
    _bars(db, "BTC", "1d", ANCHOR, [-10 * 86400, 86400], 86400, base=100.0)
    px = ar.AnchorPrices(db)
    price, tf, stale = px.close_at("BTC", ANCHOR)
    assert price is None and tf is None, "全档都不新鲜时必须返回空价，不许拿旧价凑"
    assert stale is not None and stale > 86400


def test_close_at_without_any_bars_returns_none(db):
    px = ar.AnchorPrices(db)
    assert px.close_at("BTC", ANCHOR) == (None, None, None)


# ── 技术指标 ────────────────────────────────────────────────
def test_rsi_needs_enough_bars():
    assert ar.rsi([100.0] * 5) is None
    assert ar.rsi([100.0 + i for i in range(16)]) == 100.0, "只涨不跌的 RSI 是 100"


def test_rsi_flat_is_100_by_division_guard():
    # 不涨不跌：al=0 走保护分支，返回 100.0 而不是 NaN/崩
    assert ar.rsi([100.0] * 16) == 100.0


def test_atr_pct_insufficient_or_zero_close():
    assert ar.atr_pct([]) is None
    assert ar.atr_pct([{"h": 1, "l": 1, "c": 0}] * 20) is None
    bars = [{"h": 102, "l": 98, "c": 100} for _ in range(20)]
    v = ar.atr_pct(bars)
    assert v is not None and v > 0


def test_trend_thresholds():
    assert ar._trend([]) == "N/A"
    assert ar._trend([100, 103]) == "Bull"
    assert ar._trend([100, 102]) == "Mild Bull"        # 恰好 +2% 不算 Bull
    assert ar._trend([100, 101]) == "Mild Bull"
    assert ar._trend([100, 100.2]) == "Range"
    assert ar._trend([100, 99]) == "Mild Bear"
    assert ar._trend([100, 98]) == "Mild Bear"         # 恰好 -2%
    assert ar._trend([100, 97]) == "Bear"


# ── 纪律③：不可还原的写 N/A ─────────────────────────────────
def test_snapshot_says_na_when_no_bars(db):
    px = ar.AnchorPrices(db)
    out = ar.snapshot_summary(px, ANCHOR)
    assert out.count("N/A（无锚点前行情）") == 4


def test_snapshot_never_invents_microstructure(db):
    _bars(db, "BTC", "1h", ANCHOR, [-24 * 3600 + i * 3600 for i in range(24)], 3600)
    _bars(db, "BTC", "4h", ANCHOR, [-40 * 3600 + i * 14400 for i in range(10)], 14400)
    _bars(db, "BTC", "1d", ANCHOR, [-29 * 86400 + i * 86400 for i in range(29)], 86400)
    px = ar.AnchorPrices(db)
    out = ar.snapshot_summary(px, ANCHOR, assets=("BTC",))
    assert "资金费率 N/A" in out
    assert "历史不可还原" in out
    assert "BTC" in out


# ── 纪律④：只挑真做过分析的决策 ──────────────────────────────
def _news(c, nid, ts):
    c.execute("INSERT INTO raw_news VALUES(?,?,?)", (nid, ts, f"news {nid}"))
    c.commit()


def _dec(c, did, nid, asset="BTC", action="BUY", degraded=0, created="2023-11-14 22:00:00"):
    c.execute("INSERT INTO ai_decisions VALUES(?,?,?,?,?,?,?,?)",
              (did, nid, asset, action, 0.5, 1.2, degraded, created))
    c.commit()


def test_pick_news_excludes_degraded_and_unknown_assets(db):
    for nid in (1, 2, 3, 4):
        _news(db, nid, int(ANCHOR))
    _dec(db, 1, 1, "BTC")
    _dec(db, 2, 2, "BTC", degraded=1)          # 降级，必须排除
    _dec(db, 3, 3, "DOGE")                      # 白名单外，必须排除
    _dec(db, 4, 4, "ETH", action="HOLD")

    rows = ar.pick_news(db, int(ANCHOR) - 86400, int(ANCHOR) + 86400, 10)
    ids = sorted(r["decision_id"] for r in rows)
    assert ids == [1, 4]


def test_pick_news_only_directional(db):
    for nid in (1, 2):
        _news(db, nid, int(ANCHOR))
    _dec(db, 1, 1, "BTC", action="BUY")
    _dec(db, 2, 2, "ETH", action="HOLD")
    rows = ar.pick_news(db, int(ANCHOR) - 86400, int(ANCHOR) + 86400, 10, only_directional=True)
    assert [r["decision_id"] for r in rows] == [1]


def test_pick_news_window_and_order_and_limit(db):
    _news(db, 1, int(ANCHOR))
    _dec(db, 1, 1, "BTC", created="2023-11-14 20:00:00")
    _dec(db, 2, 1, "BTC", created="2023-11-14 22:30:00")
    _dec(db, 3, 1, "BTC", created="2023-11-16 22:30:00")   # 窗口外

    rows = ar.pick_news(db, int(ANCHOR) - 86400, int(ANCHOR) + 86400, 10)
    assert [r["decision_id"] for r in rows] == [1, 2], "按时间升序"
    rows = ar.pick_news(db, int(ANCHOR) - 86400, int(ANCHOR) + 86400, 1)
    assert [r["decision_id"] for r in rows] == [1], "limit 生效"


# ── 端到端：--no-llm 只验管道，不花钱 ────────────────────────
import argparse


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    prod_path = tmp_path / "prod.db"
    rep_path = tmp_path / "replay.db"
    p = sqlite3.connect(str(prod_path))
    p.executescript("""
        CREATE TABLE kline_bars(symbol TEXT, timeframe TEXT, ts INTEGER,
                                open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, news_id INTEGER, target_asset TEXT,
                                  suggested_action TEXT, sentiment_score REAL, forward_pnl REAL,
                                  analysis_degraded INTEGER, created_at TEXT);
        CREATE TABLE raw_news(id INTEGER PRIMARY KEY, ts INTEGER, content TEXT);
    """)
    p.commit()

    def add(asset, entry, exit_, action="BUY", ts_off=300, degraded=0):
        p.execute("INSERT INTO raw_news VALUES(?,?,?)", (1, int(ANCHOR), "some news"))
        p.execute("INSERT INTO ai_decisions VALUES(?,?,?,?,?,?,?,?)",
                  (1, 1, asset, action, 0.4, 0.9, degraded,
                   ar.datetime.datetime.fromtimestamp(ANCHOR + ts_off).strftime("%Y-%m-%d %H:%M:%S")))
        # 入场：锚点前一根 1h（新鲜）；出场：恰好锚点后 240 分钟（horizon=240）
        for off, px_ in ((-3600, entry), (3600, entry), (4 * 3600, exit_)):
            p.execute("INSERT INTO kline_bars VALUES(?,?,?,?,?,?,?,?)",
                      (asset, "1h", int((ANCHOR + off) * 1000), px_, px_, px_, px_, 1.0))
        p.commit()

    r = sqlite3.connect(str(rep_path))
    r.execute(ar.DDL)
    r.commit()
    r.close()
    monkeypatch.setattr(ar, "PROD_DB", prod_path, raising=False)
    monkeypatch.setattr(ar, "REPLAY_DB", rep_path, raising=False)
    return p, rep_path, add


def _args(**kw):
    base = dict(since=int(ANCHOR) - 86400, until=int(ANCHOR) + 86400, limit=10,
                only_directional=False, live=False, strict=False, dump_prompt=None,
                no_llm=True, horizon=240, cost_bps=18.0, retries=1, retry_wait=0)
    base.update(kw)
    return argparse.Namespace(**base)


def _rows(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT asset, action, status, entry_price, exit_price, gross_pct, "
                         "net_pct, hit, snapshot_hash FROM replay_anchor_trades").fetchall()
    finally:
        c.close()


def test_e2e_buy_pays_cost_and_counts_hit(e2e):
    _p, rep, add = e2e
    add("BTC", 100.0, 110.0, "BUY")
    assert ar.run(_args()) == 0
    (row,) = _rows(rep)
    _asset, action, status, entry, exit_, gross, net, hit, snap = row
    assert action == "BUY" and status == "no_llm"
    assert entry == pytest.approx(100.0) and exit_ == pytest.approx(110.0)
    assert gross == pytest.approx(10.0)
    assert net == pytest.approx(9.82), "净利必须扣掉 18bps"
    assert hit == 1
    assert snap and len(snap) == 12


def test_e2e_sell_flips_sign(e2e):
    _p, rep, add = e2e
    add("BTC", 100.0, 90.0, "SELL")
    ar.run(_args())
    (_a, action, _s, _e, _x, gross, net, hit, _h) = _rows(rep)[0]
    assert action == "SELL"
    assert gross == pytest.approx(10.0), "跌 10% 对 SELL 是 +10% 毛利"
    assert net == pytest.approx(9.82) and hit == 1


def test_e2e_cost_can_turn_small_gain_into_loss(e2e):
    _p, rep, add = e2e
    add("BTC", 100.0, 100.1, "BUY")     # 毛利 +0.1%，扣 0.18% 成本后为负
    ar.run(_args())
    (_a, _act, _s, _e, _x, gross, net, hit, _h) = _rows(rep)[0]
    assert gross == pytest.approx(0.1)
    assert net == pytest.approx(-0.08)
    assert hit == 0, "毛利为正但扣成本后亏钱，不能算命中"


def test_e2e_flat_price_is_flagged_not_scored(e2e):
    _p, rep, add = e2e
    add("BTC", 100.0, 100.0, "BUY")
    ar.run(_args())
    (_a, _act, status, _e, _x, gross, net, _hit, _h) = _rows(rep)[0]
    assert status == "flat_price", "入场=出场必须单独标记，不能混进统计"


def test_e2e_skip_when_anchor_has_no_price(e2e):
    _p, rep, add = e2e
    # 只有 1h 但锚点前断档 6 小时（超过 1h 的 4500 秒上限），也没有更粗的档
    _p.execute("INSERT INTO raw_news VALUES(?,?,?)", (1, int(ANCHOR), "news"))
    _p.execute("INSERT INTO ai_decisions VALUES(?,?,?,?,?,?,?,?)",
               (1, 1, "BTC", "BUY", 0.4, 0.9, 0,
                ar.datetime.datetime.fromtimestamp(ANCHOR).strftime("%Y-%m-%d %H:%M:%S")))
    _p.execute("INSERT INTO kline_bars VALUES(?,?,?,?,?,?,?,?)",
               ("BTC", "1h", int((ANCHOR - 6 * 3600) * 1000), 100, 100, 100, 100, 1.0))
    _p.commit()
    ar.run(_args())
    (_a, _act, status, entry, _x, _g, _n, _hit, _h) = _rows(rep)[0]
    assert status == "skipped" and entry is None


def test_report_empty_db_says_no_samples(e2e, capsys):
    _p, rep, _add = e2e
    c = sqlite3.connect(str(rep))
    try:
        ar.report(c)
    finally:
        c.close()
    assert "暂无结算样本" in capsys.readouterr().out


def test_snapshot_survives_symbol_with_only_intraday_bars(db):
    """只有 1H 没有日线的品种（新上市/新入库）不能让整轮回放崩掉。"""
    _bars(db, "BTC", "1h", ANCHOR, [-3 * 3600, -2 * 3600, -3600], 3600)
    px = ar.AnchorPrices(db)
    out = ar.snapshot_summary(px, ANCHOR, assets=("BTC",))
    assert "区间: N/A" in out
    assert "24h/7d N/A" in out
