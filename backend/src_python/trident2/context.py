# -*- coding: utf-8 -*-
"""功能层上下文 —— 所有读数集中在这里，方便审计「这个数字从哪来」。

约定：
- 入参 ``conn`` 为 sqlite3 连接（生产走 db.get_connection()，测试走 :memory:）；
- 时间一律 UTC 秒（``int(time.time())``），K 线原始 ts 是毫秒，读入即换算；
- 查不到就返回 None / []，**不补 0、不插值、不拿旧值顶替**；
- 每个查询都能在 ``evidence`` 里复原：表名 + 过滤条件 + 时间窗。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")


class Context:
    def __init__(self, conn: sqlite3.Connection, now: Optional[int] = None,
                 assets: Optional[Sequence[str]] = None) -> None:
        self.conn = conn
        self.now = int(now if now is not None else time.time())
        self._assets = list(assets) if assets else None
        self._cols_cache: Dict[str, set] = {}
        self.trace: List[Dict[str, Any]] = []

    # ── 基础 ────────────────────────────────────────────────────────────
    def columns(self, table: str) -> set:
        if table not in self._cols_cache:
            try:
                self._cols_cache[table] = {
                    r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')
                }
            except sqlite3.Error:
                self._cols_cache[table] = set()
        return self._cols_cache[table]

    def has_table(self, table: str) -> bool:
        try:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def q(self, sql: str, params: Iterable[Any] = (), note: str = "") -> List[sqlite3.Row]:
        try:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error:
            return []
        if note:
            self.trace.append({"note": note, "sql": sql, "params": list(params)})
        return rows

    def assets(self) -> List[str]:
        if self._assets:
            return list(self._assets)
        rows = self.q("SELECT DISTINCT symbol FROM market_ticks", note="assets")
        if not rows:
            rows = self.q("SELECT DISTINCT symbol FROM kline_bars", note="assets")
        return sorted({r[0] for r in rows if r[0]})

    def kline_symbols(self) -> set:
        """有 K 线的标的集合 —— 新闻里的 NONE / MACRO / 个股 这类标签不可回放，要能筛掉。"""
        if not hasattr(self, "_ksyms") or not self._ksyms:
            rows = self.q("SELECT DISTINCT symbol FROM kline_bars", note="kline_symbols")
            self._ksyms = {str(r[0]).upper() for r in rows if r[0]}
        return self._ksyms

    # ── 行情 ────────────────────────────────────────────────────────────
    def latest_tick(self, asset: str) -> Optional[Dict[str, Any]]:
        rows = self.q(
            "SELECT symbol, ts, price, change24h, source, source_count "
            "FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (asset,), note=f"tick:{asset}",
        )
        if not rows:
            return None
        r = rows[0]
        if r["price"] is None or r["price"] <= 0:
            return None
        return {
            "asset": r["symbol"], "ts": int(r["ts"]), "price": float(r["price"]),
            "change24h": r["change24h"], "source": r["source"],
            "source_count": r["source_count"],
        }

    def price(self, asset: str) -> Optional[float]:
        t = self.latest_tick(asset)
        return None if t is None else t["price"]

    def price_age_sec(self, asset: str) -> Optional[int]:
        t = self.latest_tick(asset)
        return None if t is None else self.now - t["ts"]

    def tick_sources(self, asset: str, lookback_sec: int = 600) -> List[Dict[str, Any]]:
        """同一时间窗内各源的最新价（F47 多源互校 / F40 跨所价差用）。"""
        since = self.now - lookback_sec
        rows = self.q(
            "SELECT source, ts, price FROM market_ticks "
            "WHERE symbol=? AND ts>=? ORDER BY ts DESC",
            (asset, since), note=f"tick_sources:{asset}",
        )
        latest: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            src = r["source"] or "unknown"
            if src not in latest and r["price"] and r["price"] > 0:
                latest[src] = {"source": src, "ts": int(r["ts"]), "price": float(r["price"])}
        return list(latest.values())

    def klines(self, asset: str, timeframe: str = "1d", limit: int = 60) -> List[Dict[str, Any]]:
        """返回按时间正序的 K 线（旧→新）。ts 统一为秒。"""
        rows = self.q(
            "SELECT symbol, ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
            (asset, timeframe, int(limit)), note=f"kline:{asset}:{timeframe}",
        )
        out = []
        for r in reversed(rows):
            ts = int(r["ts"])
            if ts > 10_000_000_000:  # 毫秒 → 秒
                ts //= 1000
            if r["close"] is None:
                continue
            out.append({
                "asset": r["symbol"], "ts": ts, "open": r["open"], "high": r["high"],
                "low": r["low"], "close": float(r["close"]),
                "volume": float(r["volume"]) if r["volume"] is not None else None,
            })
        return out

    def closes(self, asset: str, timeframe: str = "1d", limit: int = 60) -> List[float]:
        return [b["close"] for b in self.klines(asset, timeframe, limit)]

    def daily_closes(self, asset: str, limit: int = 90) -> List[float]:
        """日线优先；日线不足时用 ohlcv_daily 兜底（同一资产不跨源拼接，按表整体选源）。"""
        bars = self.closes(asset, "1d", limit)
        if len(bars) >= 20:
            return bars
        rows = self.q(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?", (asset, int(limit)), note=f"ohlcv_daily:{asset}",
        )
        vals = []
        for r in reversed(rows):
            if r["close"] and r["close"] > 0:
                vals.append(float(r["close"]))
        return vals if len(vals) > len(bars) else bars

    def returns_pct(self, closes: List[float]) -> List[float]:
        out = []
        for i in range(1, len(closes)):
            prev, cur = closes[i - 1], closes[i]
            if prev and prev > 0:
                out.append((cur - prev) / prev * 100.0)
        return out

    def median_volume(self, asset: str, timeframe: str = "1d", lookback: int = 20) -> Optional[float]:
        bars = self.klines(asset, timeframe, lookback + 1)
        vols = [b["volume"] for b in bars[:-1] if b["volume"]]
        if len(vols) < 5:
            return None
        vols.sort()
        n = len(vols)
        return vols[n // 2] if n % 2 else (vols[n // 2 - 1] + vols[n // 2]) / 2

    # ── 数据源健康 ──────────────────────────────────────────────────────
    def source_rows(self) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT source_id, category, label, status, last_ok_ts, last_fail_ts, "
            "last_error, consecutive_failures, last_latency_ms, last_tick_at "
            "FROM source_status", note="source_status",
        )
        return [dict(r) for r in rows]

    def feed_rows(self, lookback_sec: int = 900) -> List[Dict[str, Any]]:
        since = self.now - lookback_sec
        rows = self.q(
            "SELECT asset, feed_kind, source, status, latency_ms, error, ts FROM feed_health "
            "WHERE ts>=? ORDER BY ts DESC", (since,), note="feed_health",
        )
        return [dict(r) for r in rows]

    def indicator_rows(self, lookback_hours: int = 48) -> List[Dict[str, Any]]:
        since_ts = self.now - lookback_hours * 3600
        rows = self.q(
            "SELECT computed_at, source, asset, indicator, last_ts, age_seconds, "
            "median_interval_seconds, status, note FROM indicator_freshness "
            "WHERE computed_at>=? ORDER BY computed_at DESC",
            (since_ts,), note="indicator_freshness",
        )
        # computed_at 可能是毫秒
        out = []
        for r in rows:
            d = dict(r)
            if d.get("computed_at") and d["computed_at"] > 10_000_000_000:
                d["computed_at"] //= 1000
            out.append(d)
        return out

    def quality_flags(self, lookback_hours: int = 24) -> List[Dict[str, Any]]:
        since = self.now - lookback_hours * 3600
        rows = self.q(
            "SELECT ts, source, asset, indicator, value, rule, note FROM data_quality_flags "
            "WHERE ts>=? ORDER BY ts DESC", (since,), note="data_quality_flags",
        )
        return [dict(r) for r in rows]

    def heartbeats(self) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT worker_name, last_tick_at, last_status, last_error, processed FROM worker_heartbeat",
            note="worker_heartbeat",
        )
        return [dict(r) for r in rows]

    # ── 决策 / 新闻 / 异动 ──────────────────────────────────────────────
    def decisions(self, limit: int = 100, settled_only: bool = False) -> List[Dict[str, Any]]:
        sql = ("SELECT id, created_at, target_asset, suggested_action, status, settled, "
               "entry_price, exit_price, forward_pnl, mfe_pct, mae_pct, evidence_confidence, "
               "impact_score, is_correct, strategy_id, invalidation_condition "
               "FROM ai_decisions {} ORDER BY id DESC LIMIT ?")
        where = "WHERE settled=1" if settled_only else ""
        rows = self.q(sql.format(where), (int(limit),), note="ai_decisions")
        return [dict(r) for r in rows]

    def decision_by_id(self, decision_id: int) -> Optional[Dict[str, Any]]:
        rows = self.q("SELECT * FROM ai_decisions WHERE id=?", (int(decision_id),), note="decision_by_id")
        return dict(rows[0]) if rows else None

    def decisions_since(self, since_ts: int) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT id, created_at, target_asset, suggested_action, status, settled, "
            "forward_pnl, evidence_confidence, strategy_id FROM ai_decisions "
            "WHERE created_at>=? ORDER BY id DESC", (int(since_ts),), note="decisions_since",
        )
        return [dict(r) for r in rows]

    def news(self, hours: int = 24) -> List[Dict[str, Any]]:
        since = self.now - hours * 3600
        rows = self.q(
            "SELECT id, news_id, ts, source, asset, action, score, is_noise, headline, assets, status "
            "FROM news_events WHERE ts>=? ORDER BY ts DESC", (since,), note="news_events",
        )
        out = []
        for r in rows:
            d = dict(r)
            if d.get("ts") and d["ts"] > 10_000_000_000:
                d["ts"] //= 1000
            out.append(d)
        return out

    def raw_news(self, hours: int = 24) -> List[Dict[str, Any]]:
        since = self.now - hours * 3600
        rows = self.q(
            "SELECT id, source, content, timestamp, ts, status, is_noise, gate_assets, gate_main_asset "
            "FROM raw_news WHERE ts>=? ORDER BY ts DESC", (since,), note="raw_news",
        )
        return [dict(r) for r in rows]

    def abnormal(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT id, ts, asset, signal_kind, direction, anchor_ts, detail_json, hint, status "
            "FROM abnormal_signals ORDER BY id DESC LIMIT ?", (int(limit),), note="abnormal_signals",
        )
        return [dict(r) for r in rows]

    def tech(self, asset: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT id, asset, ts, close, trigger_type, score, vol_ratio, support_level, "
            "merged_types, bars_available FROM tech_signals WHERE asset=? ORDER BY id DESC LIMIT ?",
            (asset, int(limit)), note=f"tech_signals:{asset}",
        )
        return [dict(r) for r in rows]

    def event_impact_curve(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT event_atom_id, asset_id, window_label, sample_ts, price, delta_pct, volume_ratio "
            "FROM event_impact_curve ORDER BY id DESC LIMIT ?", (int(limit),), note="event_impact_curve",
        )
        return [dict(r) for r in rows]

    # ── 盘口 / 资金流 / 宏观 ────────────────────────────────────────────
    def depth(self, asset: str) -> Optional[Dict[str, Any]]:
        rows = self.q(
            "SELECT asset, ts, best_bid, best_ask, spread_bp, bid_vol, ask_vol, imbalance, "
            "near_bid, near_ask, wall_bid, wall_ask, wall_bid_px, wall_ask_px "
            "FROM depth_snapshots WHERE asset=? ORDER BY ts DESC LIMIT 1",
            (asset,), note=f"depth:{asset}",
        )
        return dict(rows[0]) if rows else None

    def depth_history(self, asset: str, limit: int = 24) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT asset, ts, best_bid, best_ask, spread_bp, bid_vol, ask_vol, imbalance, "
            "near_bid, near_ask FROM depth_snapshots WHERE asset=? ORDER BY ts DESC LIMIT ?",
            (asset, int(limit)), note=f"depth_history:{asset}",
        )
        return [dict(r) for r in rows]

    def flows(self, kind: str, asset: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if asset:
            rows = self.q(
                "SELECT asset, kind, ts, value, source FROM flow_snapshots "
                "WHERE kind=? AND asset=? ORDER BY ts DESC LIMIT ?",
                (kind, asset, int(limit)), note=f"flow:{kind}:{asset}",
            )
        else:
            rows = self.q(
                "SELECT asset, kind, ts, value, source FROM flow_snapshots "
                "WHERE kind=? ORDER BY ts DESC LIMIT ?", (kind, int(limit)), note=f"flow:{kind}",
            )
        return [dict(r) for r in rows]

    def flow_kinds(self) -> List[str]:
        rows = self.q("SELECT DISTINCT kind FROM flow_snapshots", note="flow_kinds")
        return [r[0] for r in rows if r[0]]

    def macro_latest(self, category: str, metric_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if metric_key:
            rows = self.q(
                "SELECT ts, category, source, metric_key, value, unit, status FROM macro_snapshots "
                "WHERE category=? AND metric_key=? ORDER BY ts DESC LIMIT 1",
                (category, metric_key), note=f"macro:{category}:{metric_key}",
            )
        else:
            rows = self.q(
                "SELECT ts, category, source, metric_key, value, unit, status FROM macro_snapshots "
                "WHERE category=? ORDER BY ts DESC LIMIT 1", (category,), note=f"macro:{category}",
            )
        return dict(rows[0]) if rows else None

    def macro_history(self, category: str, metric_key: str, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT ts, value FROM macro_snapshots WHERE category=? AND metric_key=? "
            "ORDER BY ts DESC LIMIT ?", (category, metric_key, int(limit)), note="macro_history",
        )
        return [dict(r) for r in reversed(rows)]

    def external(self, indicator: str, asset: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """external_indicators 的最新一条（指标名精确匹配）。"""
        if asset:
            rows = self.q(
                "SELECT asset, indicator, value, ts, source FROM external_indicators "
                "WHERE indicator=? AND asset=? ORDER BY ts DESC LIMIT 1",
                (indicator, asset), note=f"external:{indicator}:{asset}",
            )
        else:
            rows = self.q(
                "SELECT asset, indicator, value, ts, source FROM external_indicators "
                "WHERE indicator=? ORDER BY ts DESC LIMIT 1",
                (indicator,), note=f"external:{indicator}",
            )
        return dict(rows[0]) if rows else None

    def external_history(self, indicator: str, asset: Optional[str] = None,
                         limit: int = 30) -> List[Dict[str, Any]]:
        if asset:
            rows = self.q(
                "SELECT asset, value, ts FROM external_indicators WHERE indicator=? AND asset=? "
                "ORDER BY ts DESC LIMIT ?", (indicator, asset, int(limit)),
                note=f"external_history:{indicator}:{asset}",
            )
        else:
            rows = self.q(
                "SELECT asset, value, ts FROM external_indicators WHERE indicator=? "
                "ORDER BY ts DESC LIMIT ?", (indicator, int(limit)),
                note=f"external_history:{indicator}",
            )
        return [dict(r) for r in reversed(rows)]

    def macro_series(self, metric_key: str, limit: int = 60) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT ts, value, status FROM macro_snapshots WHERE metric_key=? "
            "ORDER BY ts DESC LIMIT ?", (metric_key, int(limit)), note=f"macro_series:{metric_key}",
        )
        return [dict(r) for r in reversed(rows)]

    def calendar_events(self, days: int = 7) -> List[Dict[str, Any]]:
        """宏观日历：event_time 可能是 UTC 秒或毫秒，统一换算后按未来窗口过滤。"""
        rows = self.q(
            "SELECT source, event_id, country, title, impact, event_time, forecast, previous, actual, star "
            "FROM macro_calendar ORDER BY event_time ASC", note="macro_calendar",
        )
        out = []
        lo, hi = self.now - 3600, self.now + days * 86400
        for r in rows:
            d = dict(r)
            t = d.get("event_time")
            if t is None:
                continue
            t = int(t)
            if t > 10_000_000_000:
                t //= 1000
            if lo <= t <= hi:
                d["event_time"] = t
                out.append(d)
        return out

    def econ_events(self, days: int = 7) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT country, title, ts, impact, actual, forecast, previous FROM econ_events",
            note="econ_events",
        )
        out = []
        lo, hi = self.now - 3600, self.now + days * 86400
        for r in rows:
            d = dict(r)
            t = d.get("ts")
            if t is None:
                continue
            t = int(t)
            if t > 10_000_000_000:
                t //= 1000
            if lo <= t <= hi:
                d["ts"] = t
                out.append(d)
        return sorted(out, key=lambda x: x["ts"])

    # ── 纸面账本 / 功能层自有表 ─────────────────────────────────────────
    def positions(self, status: str = "open") -> List[Dict[str, Any]]:
        if not self.has_table("t2_positions"):
            return []
        rows = self.q(
            "SELECT * FROM t2_positions WHERE status=? ORDER BY id DESC", (status,), note="t2_positions",
        )
        return [self._json_cols(dict(r), ("thesis_json",)) for r in rows]

    def account(self) -> Dict[str, Any]:
        if not self.has_table("t2_account"):
            return {}
        rows = self.q("SELECT * FROM t2_account WHERE id=1", note="t2_account")
        return dict(rows[0]) if rows else {}

    def orders(self, open_only: bool = False) -> List[Dict[str, Any]]:
        if not self.has_table("t2_orders"):
            return []
        sql = "SELECT * FROM t2_orders {} ORDER BY id DESC LIMIT 500".format(
            "WHERE status IN ('pending','working')" if open_only else ""
        )
        return [dict(r) for r in self.q(sql, note="t2_orders")]

    def param_changes(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.has_table("t2_param_changes"):
            return []
        rows = self.q("SELECT * FROM t2_param_changes ORDER BY id DESC LIMIT ?", (int(limit),),
                      note="t2_param_changes")
        return [self._json_cols(dict(r), ("old_json", "new_json", "perf_24h_json")) for r in rows]

    def interventions(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.has_table("t2_interventions"):
            return []
        rows = self.q("SELECT * FROM t2_interventions ORDER BY id DESC LIMIT ?", (int(limit),),
                      note="t2_interventions")
        return [self._json_cols(dict(r), ("before_json", "after_json")) for r in rows]

    def cost_entries(self, since_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.has_table("t2_cost_ledger"):
            return []
        since = since_ts if since_ts is not None else self.now - 30 * 86400
        rows = self.q(
            "SELECT * FROM t2_cost_ledger WHERE ts>=? ORDER BY ts DESC", (int(since),),
            note="t2_cost_ledger",
        )
        return [dict(r) for r in rows]

    def subaccounts(self) -> List[Dict[str, Any]]:
        if not self.has_table("t2_subaccounts"):
            return []
        return [dict(r) for r in self.q("SELECT * FROM t2_subaccounts ORDER BY id", note="t2_subaccounts")]

    def snapshots(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.has_table("t2_decision_snapshots"):
            return []
        rows = self.q("SELECT * FROM t2_decision_snapshots ORDER BY id DESC LIMIT ?", (int(limit),),
                      note="t2_decision_snapshots")
        return [self._json_cols(dict(r), ("market_json", "news_json")) for r in rows]

    def eval_set(self) -> List[Dict[str, Any]]:
        if not self.has_table("t2_eval_set"):
            return []
        rows = self.q("SELECT * FROM t2_eval_set ORDER BY id", note="t2_eval_set")
        return [self._json_cols(dict(r), ("expected_json", "output_json")) for r in rows]

    def equity_snapshots(self, limit: int = 60) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT ts, equity, realized_pnl, n_settled, n_open, peak_equity, drawdown_pct "
            "FROM sim_equity_snapshots ORDER BY ts DESC LIMIT ?", (int(limit),), note="sim_equity_snapshots",
        )
        return [dict(r) for r in reversed(rows)]

    def replay_trades(self, limit: int = 500, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """历史回放样本（t2_sim_trades）：真实历史信号 + 真实 1m 行情结算。

        **隔离口径（v404）**：回放是"研究样本"，不是"绩效"。它绝不能进入胜率/Sharpe/
        累计盈亏这类对外口径——回放可以反复重跑、可以调参重选样本，与真实成交不可比。
        source 可选 decision_replay / news_replay；不传则两类都读。
        绩效口径请用 `live_trades()`。
        """
        if not self.has_table("t2_sim_trades"):
            return []
        if source:
            rows = self.q(
                "SELECT * FROM t2_sim_trades WHERE settled=1 AND source=? "
                "ORDER BY entry_ts DESC LIMIT ?", (source, int(limit)), note="t2_sim_trades",
            )
        else:
            rows = self.q(
                "SELECT * FROM t2_sim_trades WHERE settled=1 ORDER BY entry_ts DESC LIMIT ?",
                (int(limit),), note="t2_sim_trades",
            )
        return [dict(r) for r in rows]

    def replay_count(self) -> int:
        """已结算回放样本条数（仅用于在"样本不足"的说明里报出被隔离的规模）。"""
        if not self.has_table("t2_sim_trades"):
            return 0
        rows = self.q("SELECT COUNT(*) AS n FROM t2_sim_trades WHERE settled=1",
                      note="t2_sim_trades(count)")
        return int(rows[0]["n"]) if rows else 0

    def live_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        """真实纸面成交（quick_sim_trades，已结算且有 pnl）—— **绩效口径唯一来源**。

        回放（`t2_sim_trades`）不在此列：它样本大、可重复生成，混进绩效就是假数据。
        """
        if not self.has_table("quick_sim_trades"):
            return []
        rows = self.q(
            "SELECT id, decision_id, asset, action, entry_price, entry_ts, exit_price, exit_ts, "
            "pnl_pct, pnl_usdt, gross_pnl_pct, verdict, settled, horizon_minutes, cost_bps, "
            "strategy_id FROM quick_sim_trades "
            "WHERE settled=1 AND pnl_pct IS NOT NULL ORDER BY entry_ts DESC LIMIT ?",
            (int(limit),), note="quick_sim_trades",
        )
        return [dict(r) for r in rows]

    def sim_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT id, decision_id, asset, action, entry_price, entry_ts, exit_price, exit_ts, "
            "pnl_pct, pnl_usdt, gross_pnl_pct, verdict, settled, horizon_minutes, sl_price, "
            "cost_bps, strategy_id FROM quick_sim_trades ORDER BY id DESC LIMIT ?",
            (int(limit),), note="quick_sim_trades",
        )
        return [dict(r) for r in rows]

    def shadow_rows(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT decision_id, decided_ts, asset, direction, action_after, confidence, gate, "
            "entry_ts, entry_price, horizon_min, exit_ts, exit_price, move_pct, hit, gate_score, gate_basis "
            "FROM shadow_ledger ORDER BY decided_ts DESC LIMIT ?", (int(limit),), note="shadow_ledger",
        )
        return [dict(r) for r in rows]

    def source_stats(self) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT news_source, asset, sample_n, win_n, loss_n, avg_forward_pnl, hit_rate, "
            "source_mult, last_updated FROM source_stats", note="source_stats",
        )
        return [dict(r) for r in rows]

    def trade_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT id, decision_id, asset, symbol, side, status, notional_usdt, quantity, "
            "filled_price, order_id, adapter, mode, reason, created_at FROM trade_audit_log "
            "ORDER BY id DESC LIMIT ?", (int(limit),), note="trade_audit_log",
        )
        return [dict(r) for r in rows]

    def strategy_versions(self) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT * FROM strategy_versions ORDER BY id DESC LIMIT 50", note="strategy_versions",
        )
        return [dict(r) for r in rows]

    # ── 工具 ────────────────────────────────────────────────────────────
    @staticmethod
    def _json_cols(row: Dict[str, Any], cols: Iterable[str]) -> Dict[str, Any]:
        for c in cols:
            v = row.get(c)
            if isinstance(v, str) and v.strip():
                try:
                    row[c] = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    pass
        return row

    def evidence(self, **kw: Any) -> Dict[str, Any]:
        base = {"as_of": self.now, "db": getattr(self.conn, "database", None) or "memory"}
        base.update(kw)
        return base
