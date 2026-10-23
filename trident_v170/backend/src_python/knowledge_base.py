#!/usr/bin/env python3
"""历史 K 线知识库（阶段1）—— 反共识分析的"历史位置/趋势"参考资料。

把关键品种历史 K 线持久化，供 AI 分析时注入"只描述位置、不直接给方向"
的反共识对照。区别于宏观层(FRED/BLS)与实时快照(market_snapshot)：这里回答
"价格现在站在 7 日区间什么位置、趋势是什么"这类历史事实。

数据源（复用 market_snapshot/quote_sources，零重写）：
  - XAU/GOLD/XAG → OKX 永续 (quote_sources.fetch_okx_candles)
  - BTC/ETH/SOL   → 币安永续 (market_snapshot._fetch_fapi_klines_sync)
  - WTI/OIL       → 新增 Yahoo CL=F 完整 OHLCV；失败回退 FRED DCOILWTICO 日收盘序列

特征复用 market_snapshot._compute_period_stats（ATR/趋势/区间高低/RSI/ADX）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

import config
import db
import source_status

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _HAS_REQUESTS = False


KLINE_SYMBOLS: tuple = ("XAU", "BTC", "ETH", "SOL", "WTI")
KLINE_TIMEFRAMES: tuple = ("1m", "15m", "1h", "4h", "1d")
# 每周期最近根数（分钟线覆盖短窗、大周期覆盖长得多的历史区间）
_LIMIT_FOR_TF = {"1m": 240, "15m": 192, "1h": 168, "4h": 96, "1d": 96}
_KLINE_LIMIT = 96              # 兼容旧默认值（实际按 _LIMIT_FOR_TF 取数）
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F"
_YAHOO_INTERVAL = {"1d": "1d", "4h": "60m"}   # Yahoo 无 4h，用 60m 近似
_CONSISTENCY = {"1m": 30, "15m": 32, "1h": 48, "4h": 6, "1d": 5}   # _compute_period_stats 趋势连贯性


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fetch_yahoo_wti_klines(interval: str, limit: int) -> Optional[List[List[float]]]:
    """Yahoo CL=F 完整 OHLCV（升序 [[ts_ms,o,h,l,c,vol],...]）。失败 None。

    v2026.10.05：复用 quote_sources.fetch_yahoo_candles（统一 Yahoo fetcher，含限速+熔断+重试），
    保留旧函数名供 _fetch_klines 调用方使用。Yahoo 对非常规 UA 直接 TLS RST（SSLEOFError），
    quote_sources.fetch_yahoo_candles 已内置 Mozilla UA + 限速/熔断/重试。
    """
    try:
        import quote_sources
        # 1d → interval=1d, 4h → 60m（Yahoo 无 4h）, 其它 → 60m
        yahoo_iv = _YAHOO_INTERVAL.get(interval, "60m")
        # 大周期需要更宽 range 以拿到足够历史
        if yahoo_iv == "1d":
            rng = "6mo"
        else:
            rng = "1mo"
        rows = quote_sources.fetch_yahoo_candles("CL=F", interval=yahoo_iv, range_or_period=rng, limit=limit)
        if not rows:
            return None
        return rows
    except Exception as exc:
        try:
            source_status.report("kline:CL=F", False, error=f"{type(exc).__name__}")
        except Exception:
            pass
        return None


def _fetch_fred_wti_daily(limit: int) -> Optional[List[List[float]]]:
    """FRED DCOILWTICO 日收盘 → 伪 K 线（o=h=l=c=收盘，vol=NaN）。仅 1d 兜底。"""
    try:
        import datetime

        import fred_feed
        if not config.FRED_API_KEY:
            return None
        body = fred_feed._default_get(
            fred_feed._observations_url("DCOILWTICO", config.FRED_API_KEY), 12
        )
        obs = fred_feed._clean_observations(body)
        rows: List[List[float]] = []
        for item in obs:
            try:
                d = datetime.datetime.strptime(str(item.get("date") or ""), "%Y-%m-%d")
                ts_ms = int(d.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                c = float(str(item.get("value") or ""))
            except (ValueError, TypeError):
                continue
            if c <= 0:
                continue
            rows.append([float(ts_ms), c, c, c, c, float("nan")])
        rows.reverse()  # FRED desc → 升序
        if len(rows) < 2:
            return None
        source_status.report("kline:DCOILWTICO", True, detail=f"rows={len(rows)}")
        return rows[-limit:]
    except Exception as exc:
        source_status.report("kline:DCOILWTICO", False, error=f"{type(exc).__name__}")
        return None


def fetch_klines(asset_id: str, interval: str, limit: int = 96) -> Optional[List[List[float]]]:
    """按资产拉历史 K 线（升序 [[ts_ms,o,h,l,c,vol],...]）。

    XAU/BTC/ETH 复用 market_snapshot._fetch_asset_klines；WTI 走 Yahoo→FRED 回退。
    """
    a = (asset_id or "").upper()
    if a in ("WTI", "OIL"):
        rows = _fetch_yahoo_wti_klines(interval, limit)
        if not rows and interval == "1d":
            rows = _fetch_fred_wti_daily(limit)
        return rows
    try:
        from market_snapshot import _fetch_asset_klines
        return _fetch_asset_klines(a, interval, limit)
    except Exception:
        return None


def _compute_stats(candles: List[List[float]], timeframe: str) -> Dict[str, Any]:
    """复用 market_snapshot._compute_period_stats 计算 ATR/趋势/区间高低。失败返回空。"""
    try:
        from market_snapshot import _compute_period_stats
        return _compute_period_stats(candles, _CONSISTENCY.get(timeframe, 5)) or {}
    except Exception:
        return {}


def _pos_pct_7d(close: Optional[float], highest: Optional[float],
                lowest: Optional[float]) -> Optional[float]:
    """现价在 [lowest, highest] 区间的位置 0~1。区间退化时返回 0.5（中性）。"""
    if close is None or highest is None or lowest is None:
        return None
    span = highest - lowest
    if span <= 0:
        return 0.5
    return round((close - lowest) / span, 4)


def persist(connection: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    """拉取全部知识库品种×周期的 K 线，写 kline_bars + kline_snapshots（幂等）。

    v174 修复: 每个 (sym, tf) 单独 commit,不要把所有 INSERT 堆积在单个长事务里——
    25 次 INSERT 累计持锁 8s+,与 ai_worker batch_claim / ingest 互锁. 每完成一对就
    commit + 短暂释放,让其它 writer 能立刻拿到写锁.
    """
    own = connection is None
    conn = connection or db.get_connection()
    now_ms = _now_ms()
    written_bars = 0
    written_snaps = 0
    try:
        for sym in KLINE_SYMBOLS:
            for tf in KLINE_TIMEFRAMES:
                candles = fetch_klines(sym, tf, _LIMIT_FOR_TF.get(tf, 96))
                if not candles:
                    conn.execute(
                        """INSERT OR REPLACE INTO kline_snapshots
                           (symbol, timeframe, status, payload, ts)
                           VALUES (?,?, 'unavailable', '{}', ?)""",
                        (sym, tf, now_ms),
                    )
                    conn.commit()  # v174: per-iteration commit
                    continue
                for c in candles:
                    try:
                        conn.execute(
                            """INSERT INTO kline_bars
                               (symbol, timeframe, ts, open, high, low, close, volume)
                               VALUES (?,?,?,?,?,?,?,?)
                               ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                                 open=excluded.open, high=excluded.high,
                                 low=excluded.low, close=excluded.close,
                                 volume=excluded.volume""",
                            (sym, tf, int(c[0]), c[1], c[2], c[3], c[4], c[5]),
                        )
                        written_bars += 1
                    except (TypeError, ValueError, IndexError):
                        continue
                stats = _compute_stats(candles, tf)
                close = candles[-1][4]
                highest = stats.get("highest")
                lowest = stats.get("lowest")
                pos = _pos_pct_7d(close, highest, lowest)
                payload = json.dumps(
                    {
                        "range_pct": stats.get("range_pct"),
                        "return_pct": stats.get("return_pct"),
                        "trend_strength": stats.get("trend_strength"),
                        "indicators": stats.get("indicators") or {},
                    },
                    ensure_ascii=False,
                    default=str,
                )
                conn.execute(
                    """INSERT OR REPLACE INTO kline_snapshots
                       (symbol, timeframe, close, pos_pct_7d, range_high_7d, range_low_7d,
                        atr_pct, trend, status, payload, ts)
                       VALUES (?,?,?,?,?,?,?,?, 'ok', ?, ?)""",
                    (sym, tf, close, pos, highest, lowest,
                     stats.get("atr_pct"), stats.get("trend"), payload, now_ms),
                )
                written_snaps += 1
                conn.commit()  # v174: per-iteration commit — 避免长事务持锁 8s+
    finally:
        if own:
            conn.close()
    return {"written_bars": written_bars, "written_snaps": written_snaps}


def latest_snapshots(connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """读最新 kline_snapshots（status='ok'），payload 已反序列化。"""
    own = connection is None
    conn = connection or db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kline_snapshots'"
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            "SELECT * FROM kline_snapshots WHERE status='ok' ORDER BY symbol, timeframe"
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
            out.append(d)
        return out
    finally:
        if own:
            conn.close()


def render_kline_block(snapshots: Optional[List[Dict[str, Any]]] = None) -> str:
    """渲染 prompt 段：只描述位置/趋势，不直接给方向。空 → 空串（不注入）。"""
    snaps = snapshots if snapshots is not None else latest_snapshots()
    if not snaps:
        return ""
    lines = ["── 历史K线参考（反共识对照：只描述位置/趋势，不得据此直接出方向）──"]
    for s in snaps:
        sym = s.get("symbol")
        tf = s.get("timeframe")
        close = s.get("close")
        pos = s.get("pos_pct_7d")
        hi = s.get("range_high_7d")
        lo = s.get("range_low_7d")
        atr = s.get("atr_pct")
        trend = s.get("trend") or "N/A"
        if close is None:
            lines.append(f"{sym}·{tf}: 数据不足(N/A)，不得臆造")
        elif hi is not None and lo is not None and hi > lo and pos is not None:
            tag = ">80%高位" if pos >= 0.8 else ("<20%低位" if pos <= 0.2 else "中性")
            atr_s = f"ATR {atr:.2f}%" if atr is not None else "ATR N/A"
            lines.append(
                f"{sym}·{tf}: 现价 {close:g} 处近7日区间 {pos*100:.0f}%({tag}, 高 {hi:g}/低 {lo:g})，{atr_s}，趋势 {trend}"
            )
        else:
            lines.append(f"{sym}·{tf}: 现价 {close:g}，趋势 {trend}（仅收盘序列，无区间信息）")
    lines.append("以上仅作位置对照；是否过度计价/追多赔率差由 [水位博弈][反共识结论] 自行判断，缺失标 N/A 不补造。")
    return "\n".join(lines)