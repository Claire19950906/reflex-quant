"""v314 P0 回归测试：结算必须用「到期时刻价」，过期仓不再被现价结算。

事故背景 (2026-10-29 实测复盘):
    · 结算价取「扫描当刻的现价」，exit_ts 也写成扫描时刻；
    · 结算链被 cut_bleeding_trades 的 TypeError 掐断，静默停摆 8.85 天；
    · 一笔 horizon=20 分钟的仓位被拖到 12742.9 分钟 (8.85 天) 才结算，
      收益被放大到 ±50% 量级，回填 forward_pnl 后界面均值拉到 +117.71%。

修复口径:
    1. settle_trades: exit_price = db.price_at(due_ts)，exit_ts = due_ts；
    2. 宽限期内到期价取不到 → 允许用现价兜底 (now≈due_ts，误差可接受)；
    3. 超宽限期且无任何价 → 熔断废弃 (ABANDONED)，不回填 ai_decisions；
    4. check_tp_sl / cut_bleeding_trades / update_open_extremes: 过期仓一律跳过。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import config
import db
import quick_sim


def _setup_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    c.execute(
        """CREATE TABLE IF NOT EXISTS ohlcv_intraday (
               asset TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
               open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
               close REAL NOT NULL, volume REAL NOT NULL,
               trades INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 1,
               source TEXT, PRIMARY KEY (asset, interval, ts))"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS ohlcv_daily (
               asset TEXT NOT NULL, ts INTEGER NOT NULL,
               open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
               close REAL NOT NULL, volume INTEGER NOT NULL,
               source TEXT NOT NULL DEFAULT 'test', UNIQUE(asset, ts))"""
    )
    return c


def _bar(c, asset, ts, close, interval="1m"):
    c.execute(
        """INSERT OR REPLACE INTO ohlcv_intraday
               (asset, interval, ts, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?)""",
        (asset, interval, ts, close, close, close, close, 1.0),
    )


def _open_trade(c, *, did, asset="BTC", action="BUY", entry_price=100.0,
                entry_ts=None, horizon_minutes=20, notional=100.0,
                max_price=None, min_price=None):
    """插一条 ai_decisions + 一条 quick_sim_trades（entry_ts 用 epoch 秒）。"""
    now = int(time.time())
    entry_ts = now if entry_ts is None else entry_ts
    cur = c.execute(
        """INSERT INTO raw_news (source, content, ts, first_seen_ts)
           VALUES ('test', 'v314 settle due price', ?, ?)""",
        (str(entry_ts - 60), str(entry_ts - 60)),
    )
    news_id = cur.lastrowid
    c.execute(
        """INSERT INTO ai_decisions
               (id, news_id, sentiment_score, suggested_action, reasoning,
                target_asset, evidence_confidence, settled)
           VALUES (?,?,0.5,?,'',?,0.9,0)""",
        (did, news_id, action, asset),
    )
    c.execute(
        """INSERT INTO quick_sim_trades
               (decision_id, asset, action, entry_price, notional_usdt,
                entry_ts, horizon_minutes, settled, max_price, min_price)
           VALUES (?,?,?,?,?,?,?,0,?,?)""",
        (did, asset, action, entry_price, notional, entry_ts,
         horizon_minutes, max_price, min_price),
    )
    c.commit()


def _trade(c, did):
    return dict(c.execute(
        "SELECT * FROM quick_sim_trades WHERE decision_id=?", (did,)
    ).fetchone())


def _decision(c, did):
    return dict(c.execute(
        "SELECT * FROM ai_decisions WHERE id=?", (did,)
    ).fetchone())


# ───────────────────────── db.price_at 取价口径 ─────────────────────────


def test_price_at_prefers_asof_1m_bar():
    """as-of 优先：取到期时刻**之前**最近一根 1m 线（严格无未来函数）。"""
    c = _setup_db()
    t = 1_700_000_000
    _bar(c, "BTC", t - 60, 99.0)
    _bar(c, "BTC", t + 60, 101.0)
    assert db.price_at(c, "BTC", t) == pytest.approx(99.0)


def test_price_at_falls_back_to_forward_bar():
    """到期前完全没有数据时，才用到期之后第一根（结算发生在到期之后，不算未来函数）。"""
    c = _setup_db()
    t = 1_700_000_000
    _bar(c, "BTC", t + 60, 101.0)
    assert db.price_at(c, "BTC", t) == pytest.approx(101.0)


def test_price_at_ignores_non_1m_intervals():
    """ohlcv_intraday 混存 1m/3m/5m/10m/15m/1h/4h，不过滤会取到 4h 的收盘价。"""
    c = _setup_db()
    t = 1_700_000_000
    _bar(c, "BTC", t + 60, 101.0, interval="4h")
    _bar(c, "BTC", t + 60, 102.0, interval="1h")
    assert db.price_at(c, "BTC", t) is None


def test_price_at_returns_none_without_data():
    c = _setup_db()
    assert db.price_at(c, "BTC", 1_700_000_000) is None
    assert db.price_at(c, "", 1_700_000_000) is None
    assert db.price_at(c, "BTC", None) is None


# ───────────────────────── settle_trades 结算口径 ─────────────────────────


def test_settle_uses_due_price_not_live_price():
    """核心：结算价取到期时刻的价，不是扫描当刻的现价。"""
    c = _setup_db()
    now = int(time.time())
    entry_ts = now - 1200
    due_ts = entry_ts + 1200
    _open_trade(c, did=9001, entry_ts=entry_ts, horizon_minutes=20, entry_price=100.0)
    _bar(c, "BTC", due_ts, 101.0)
    # 现价故意给 150（旧实现会用它结算）
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 150.0)
    assert len(settled) == 1
    assert settled[0]["exit_price"] == pytest.approx(101.0)
    assert settled[0]["price_source"] == "due_price"
    assert settled[0]["pnl_pct"] > 0


def test_settle_exit_ts_equals_due_ts():
    """exit_ts 记到期时刻，持有时长 = horizon（不是扫描时刻）。"""
    c = _setup_db()
    now = int(time.time())
    entry_ts = now - 1200
    due_ts = entry_ts + 1200
    _open_trade(c, did=9002, entry_ts=entry_ts, horizon_minutes=20, entry_price=100.0)
    _bar(c, "BTC", due_ts, 101.0)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 150.0)
    assert settled[0]["exit_ts"] == due_ts
    assert settled[0]["hold_minutes"] == pytest.approx(20.0)
    assert _trade(c, 9002)["exit_ts"] == due_ts


def test_settle_overdue_uses_due_price_not_stale_live_price():
    """★ 8.85 天停摆回归：过期仓仍按到期价结算，不会用几天后的现价。"""
    c = _setup_db()
    now = int(time.time())
    entry_ts = now - int(8.85 * 86400)      # 停摆 8.85 天
    due_ts = entry_ts + 1200
    _open_trade(c, did=9003, entry_ts=entry_ts, horizon_minutes=20, entry_price=100.0)
    _bar(c, "BTC", due_ts, 100.5)            # 到期时刻几乎持平
    # 8.85 天后的现价腰斩 —— 旧实现用它结算 → -50%
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 50.0)
    assert len(settled) == 1
    assert settled[0]["exit_price"] == pytest.approx(100.5)
    assert settled[0]["pnl_pct"] > 0
    assert settled[0]["price_source"] == "due_price"
    assert settled.settle_stats["overdue"] == 1


def test_settle_overdue_without_any_price_is_abandoned():
    """★ 熔断：超宽限期且无到期价/无现价 → 废弃，不进统计，不回填 ai_decisions。"""
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9004, entry_ts=now - 5 * 86400,
                horizon_minutes=20, entry_price=100.0)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: None)
    assert len(settled) == 0
    assert settled.settle_stats["abandoned"] == 1
    row = _trade(c, 9004)
    assert row["settled"] == 1
    assert row["verdict"] == "ABANDONED"
    assert row["pnl_pct"] == 0
    assert row["exit_price"] is None
    d = _decision(c, 9004)
    assert d["forward_pnl"] is None      # 不伪造收益
    assert d["settled"] == 0


def test_settle_within_grace_falls_back_to_live_price():
    """宽限期内到期价取不到 → 允许现价兜底（now≈due_ts，误差可接受）。"""
    c = _setup_db()
    now = int(time.time())
    entry_ts = now - 1200
    _open_trade(c, did=9005, entry_ts=entry_ts, horizon_minutes=20, entry_price=100.0)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 110.0)
    assert len(settled) == 1
    assert settled[0]["exit_price"] == pytest.approx(110.0)
    assert settled[0]["price_source"] == "live_price"
    assert settled[0]["pnl_pct"] > 0


def test_settle_overdue_with_live_price_is_abandoned_not_live_priced():
    """★ v386：超宽限期即使拿得到"现价"也不能拿来结算。

    一笔 horizon=20 分钟的仓位若在 12 小时后才结算，用的"现价"已经是另一个
    市场环境下的价，得到的收益（如 -1.21%）不是这笔交易的真实结果。
    """
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9007, entry_ts=now - 12 * 3600,
                horizon_minutes=20, entry_price=100.0)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 88.17)
    assert len(settled) == 0
    assert settled.settle_stats["abandoned"] == 1
    row = _trade(c, 9007)
    assert row["verdict"] == "ABANDONED"
    assert row["exit_price"] is None


def test_settle_deferred_when_no_price_within_grace():
    """宽限期内连现价都没有 → 留到下一轮（不猜价、不废弃）。"""
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9006, entry_ts=now - 1200,
                horizon_minutes=20, entry_price=100.0)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: None)
    assert len(settled) == 0
    assert settled.settle_stats["deferred"] == 1
    assert _trade(c, 9006)["settled"] == 0


# ───────────────────────── 过期仓不再被现价路径结算 ─────────────────────────


def _enable_tp_sl(c):
    c.execute(
        """UPDATE paper_trading_settings
           SET tp_enabled=1, tp1_mult=0.6, tp2_mult=1.0, tp1_ratio=0.5,
               tp2_ratio=0.3, sl_mult=0.5
           WHERE id=1"""
    )
    c.commit()


def test_expired_trade_is_not_hit_by_tp_sl():
    """★ 过期仓的 SL 不该被触发（旧实现会用几天后的价结算）。"""
    c = _setup_db()
    _enable_tp_sl(c)
    now = int(time.time())
    _open_trade(c, did=9007, entry_ts=now - 5 * 86400, horizon_minutes=20,
                entry_price=100.0, min_price=90.0)
    hits = quick_sim.check_tp_sl(c, price_fn=lambda a: 80.0)
    assert hits == []
    assert _trade(c, 9007)["settled"] == 0


def test_expired_trade_is_not_cut_by_bleeding_cut():
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9008, entry_ts=now - 5 * 86400, horizon_minutes=20,
                entry_price=100.0, min_price=90.0)
    cut = quick_sim.cut_bleeding_trades(c, price_fn=lambda a: 80.0, max_loss_pct=0.3)
    assert cut == []
    assert _trade(c, 9008)["settled"] == 0


def test_update_open_extremes_skips_expired_trade():
    """过期仓的 max/min 不再用现价刷新（否则是几天的极值，污染 MFE/MAE）。"""
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9009, entry_ts=now - 5 * 86400, horizon_minutes=20,
                entry_price=100.0, max_price=100.0, min_price=100.0)
    quick_sim.update_open_extremes(c, price_fn=lambda a: 80.0)
    row = _trade(c, 9009)
    assert row["max_price"] == pytest.approx(100.0)
    assert row["min_price"] == pytest.approx(100.0)


def test_non_expired_trade_still_tracked_normally():
    """未过期的仓仍按原逻辑刷新极值（防止修复过头）。"""
    c = _setup_db()
    now = int(time.time())
    _open_trade(c, did=9010, entry_ts=now - 300, horizon_minutes=20,
                entry_price=100.0, max_price=100.0, min_price=100.0)
    quick_sim.update_open_extremes(c, price_fn=lambda a: 80.0)
    row = _trade(c, 9010)
    assert row["max_price"] == pytest.approx(100.0)
    assert row["min_price"] == pytest.approx(80.0)


def test_expired_trade_is_expired_helper():
    now = int(time.time())
    assert quick_sim._is_expired({"entry_ts": now - 5 * 86400,
                                   "horizon_minutes": 20}, now) is True
    assert quick_sim._is_expired({"entry_ts": now - 300,
                                   "horizon_minutes": 20}, now) is False


def test_to_epoch_accepts_datetime_string():
    """entry_ts 是 datetime 字符串时不能抛异常（v313 同款陷阱）。"""
    assert quick_sim._to_epoch("2026-10-29 12:00:00") > 0
    assert quick_sim._to_epoch(1_700_000_000) == 1_700_000_000
    assert quick_sim._to_epoch(None) == 0
    assert quick_sim._to_epoch("not-a-time") == 0


# ───────────────── v386 P0：日线兜底不能取"最老"的一根 ─────────────────
#
# 事故 (2026-11-16)：WTI 1m 线在 11-15 00:00 ~ 11-16 07:00 整段断档，
# 两笔 horizon=20 分钟的仓位到期时 intraday 取不到价，日线兜底分支
# 用 `ORDER BY ts ASC` 取到了 6 天前那根最早的日线（86.43 / 86.42），
# 于是凭空结算出 -3.17%/笔，胜率、权益、反思样本全被污染。


def _daily_bar(c, asset, ts, close):
    c.execute(
        """INSERT OR REPLACE INTO ohlcv_daily
               (asset, ts, open, high, low, close, volume, source)
           VALUES (?,?,?,?,?,?,?, 'test')""",
        (asset, ts, close, close, close, close, 1),
    )


def test_price_at_daily_fallback_takes_nearest_bar_not_oldest():
    """日线兜底必须取**到期前最近一根**，不是窗口里最老的一根。"""
    c = _setup_db()
    t = 1_700_000_000
    _daily_bar(c, "BTC", t - 6 * 86400, 50.0)   # 窗口下沿：6 天前的老价
    _daily_bar(c, "BTC", t - 2 * 86400, 80.0)
    _daily_bar(c, "BTC", t - 86400, 99.0)       # 最近一根
    assert db.price_at(c, "BTC", t) == pytest.approx(99.0)


def test_price_at_rejects_daily_bar_older_than_lag_limit():
    """唯一的日线比到期时刻早 6 天 → 视为"无价"，不许拿来结算。"""
    c = _setup_db()
    t = 1_700_000_000
    _daily_bar(c, "WTI", t - 6 * 86400, 86.43)
    assert db.price_at(c, "WTI", t) is None


def test_settle_never_uses_stale_daily_close():
    """端到端：1m 断档 + 只有 6 天前日线 → 不用老价结算（不同源的价格不许伪造盈亏）。"""
    c = _setup_db()
    now = int(time.time())
    entry_ts = now - 1200
    _open_trade(c, did=9021, asset="WTI", entry_ts=entry_ts,
                horizon_minutes=20, entry_price=88.985)
    # 到期价取不到；日线只有 6 天前的一根（旧实现会用它结算成 -2.8%）
    _daily_bar(c, "WTI", entry_ts - 6 * 86400, 86.43)
    settled = quick_sim.settle_trades(c, price_fn=lambda a: 88.99)
    assert len(settled) == 1
    # 只能走"现价兜底"路径，绝不能是那根 6 天前的日线
    assert settled[0]["price_source"] == "live_price"
    assert settled[0]["exit_price"] == pytest.approx(88.99)
    assert abs(settled[0]["pnl_pct"]) < 1.0   # 只扣成本，不是 -3.17% 的"假亏损"
