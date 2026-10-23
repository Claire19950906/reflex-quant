# -*- coding: utf-8 -*-
"""yfinance 行情接入 — 韩股 + 山寨币专用层（v226+ Q4 闭环）。

背景：
  - coingecko_feed.py 拉头部 250 币市场宽度（涨跌家数），不带具体价格序列。
  - asia_prices.py 只覆盖 5 个亚洲标的的实时报价，无历史 K 线 → 无 7d trend / RSI / ADX.
  - stock_prices.py 美股专用，雅虎 JSON 协议被 Yahoo 官方 deprecate.

本模块职责：
  1) 拉任意 yfinance symbol 的 1d K 线（默认 60 天，覆盖 7d + 30d 趋势窗口）
  2) 写入 external_indicators 表，供 decision_guard 趋势因子 + abnormal_detector 读取
  3) 提供韩股（000660.KS 海力士）+ 山寨币（SOL-USD/DOGE-USD/XRP-USD）的批量接入

数据源：yfinance（与 stock_prices.py 同源，国内可直连；失败兜底 Yahoo Finance JSON API）。
免费限额：~30-60 req/min，配合 6h TTL 缓存完全足够。

注意：本模块只补**价格上下文**层，不写 funding_rate/basis/OI（这些只在加密永续合约有），
     韩股的"资金背离"判断用 coingecko 的 SOL/DOGE/XRP 资金面作 proxy（已在 evidence.py 实现）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── 默认覆盖：海力士 / 美光 / 主流山寨币 ─────────────────────────────────
# v227 P1 演进：增加 MU（Micron，"美国海力士"）作为 000660.KS 的镜像对照，
#             用于 discovery.undervalued_detector 做 SK/MU 价差套利检测。
DEFAULT_KR_SYMBOLS: Tuple[str, ...] = (
    "000660.KS",   # SK 海力士 (KOSPI)
    # 韩国综合指数可后续扩展：KOSPI = ^KS11
)
DEFAULT_US_PEERS: Tuple[str, ...] = (
    "MU",          # Micron Technology — 美国存储芯片同行,中文圈称"美国海力士"
)
DEFAULT_ALTCOIN_SYMBOLS: Tuple[str, ...] = (
    "SOL-USD",
    "DOGE-USD",
    "XRP-USD",
    "ADA-USD",
    "BNB-USD",
)

DEFAULT_ALL_SYMBOLS: Tuple[str, ...] = DEFAULT_KR_SYMBOLS + DEFAULT_US_PEERS + DEFAULT_ALTCOIN_SYMBOLS

# yfinance Ticker() → asset code（LLM target_asset 字符串）
_YF_TO_ASSET: Dict[str, str] = {
    "000660.KS": "SKHYNIX",
    "MU": "MU",
    "SOL-USD": "SOL",
    "DOGE-USD": "DOGE",
    "XRP-USD": "XRP",
    "ADA-USD": "ADA",
    "BNB-USD": "BNB",
}

# ── 进程内缓存：symbol -> (expire_ts, rows) ──
_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
_TTL_S = 6 * 3600.0  # 6 小时，与 equity_feed 对齐


def _db_path():
    import db as _db
    return _db.get_db_path()


def _ensure_external_schema(conn: sqlite3.Connection) -> None:
    """建 external_indicators 表（若不存在），与 external_data.py 共用表结构。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            indicator TEXT NOT NULL,
            value REAL NOT NULL,
            ts INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            regime_tags TEXT NOT NULL DEFAULT '[]',
            meta TEXT NOT NULL DEFAULT '{}',
            ingested_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_external_indicator_ts
        ON external_indicators(indicator, ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_external_asset_indicator
        ON external_indicators(asset, indicator)
    """)
    conn.commit()


def _cache_get(symbol: str) -> Optional[List[Dict[str, Any]]]:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(symbol)
        if hit and hit[0] > now:
            return list(hit[1])
    return None


def _cache_put(symbol: str, rows: List[Dict[str, Any]]) -> None:
    with _cache_lock:
        _cache[symbol] = (time.time() + _TTL_S, list(rows))


def _fetch_yfinance(symbol: str, lookback_days: int) -> List[Dict[str, Any]]:
    """拉单一 symbol 的 1d K 线（默认 60 天）。失败抛 RuntimeError。"""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(f"yfinance not installed: {exc}") from exc
    df = yf.Ticker(symbol).history(
        period=f"{max(lookback_days, 5)}d", interval="1d", auto_adjust=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned empty for {symbol}")
    rows: List[Dict[str, Any]] = []
    for ts_or_idx, row in df.iterrows():
        try:
            close = float(row.get("Close") or row.get("Adj Close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        try:
            epoch = int(ts_or_idx.timestamp()) if hasattr(ts_or_idx, "timestamp") else int(ts_or_idx)
        except (AttributeError, ValueError):
            epoch = int(time.time()) - lookback_days * 86400
        rows.append({"ts": epoch, "value": close})
    if not rows:
        raise RuntimeError(f"yfinance no usable close for {symbol}")
    return rows


def fetch_history(
    symbol: str,
    lookback_days: int = 60,
    *,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """拉 symbol 的 1d 收盘价序列（带 6h 缓存）。

    Returns
    -------
    List[dict]: [{"ts": int_epoch, "value": float_close}, ...]  升序
    """
    if use_cache:
        hit = _cache_get(symbol)
        if hit is not None:
            return hit
    rows = _fetch_yfinance(symbol, lookback_days)
    _cache_put(symbol, rows)
    return rows


def _ingest_one(symbol: str, lookback_days: int = 60) -> Dict[str, Any]:
    """拉 + 写 external_indicators。失败返回 {'ok': False, 'error': ...}。"""
    asset = _YF_TO_ASSET.get(symbol, symbol.split(".")[0])
    try:
        rows = fetch_history(symbol, lookback_days)
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "asset": asset,
                "error": f"{type(exc).__name__}: {exc}"}
    conn = sqlite3.connect(_db_path())
    try:
        _ensure_external_schema(conn)
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM external_indicators WHERE asset=? AND indicator=?",
            (asset, symbol),
        )
        meta = json.dumps({"lookback_days": lookback_days, "source_symbol": symbol})
        regime_tags = json.dumps(["asset_context", "yfinance"])
        for rec in rows:
            cur.execute(
                """INSERT INTO external_indicators
                   (asset, indicator, value, ts, source, regime_tags, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (asset, symbol, float(rec["value"]), int(rec["ts"]),
                 "yfinance", regime_tags, meta),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "symbol": symbol, "asset": asset, "written": len(rows)}


def ingest_kr_equity(
    symbols: Iterable[str] = DEFAULT_KR_SYMBOLS,
    lookback_days: int = 60,
) -> List[Dict[str, Any]]:
    """批量 ingest 韩国股票（默认只 SK 海力士）。"""
    out: List[Dict[str, Any]] = []
    for s in symbols:
        out.append(_ingest_one(s, lookback_days))
    return out


def ingest_altcoin(
    symbols: Iterable[str] = DEFAULT_ALTCOIN_SYMBOLS,
    lookback_days: int = 60,
) -> List[Dict[str, Any]]:
    """批量 ingest 主流山寨币（SOL/DOGE/XRP/ADA/BNB）。"""
    out: List[Dict[str, Any]] = []
    for s in symbols:
        out.append(_ingest_one(s, lookback_days))
    return out


def ingest_all(
    kr_symbols: Iterable[str] = DEFAULT_KR_SYMBOLS,
    us_symbols: Iterable[str] = DEFAULT_US_PEERS,
    altcoin_symbols: Iterable[str] = DEFAULT_ALTCOIN_SYMBOLS,
    lookback_days: int = 60,
) -> Dict[str, Any]:
    """一键：韩股 + 美股 peer + 山寨币全量写入。

    Returns
    -------
    dict: {
      "kr": List[result_per_symbol],
      "us": List[result_per_symbol],
      "altcoin": List[result_per_symbol],
      "kr_ok": int, "kr_fail": int,
      "us_ok": int, "us_fail": int,
      "alt_ok": int, "alt_fail": int,
      "ts": int,
    }
    """
    kr = ingest_kr_equity(kr_symbols, lookback_days)
    us = ingest_us_peer(us_symbols, lookback_days)
    alt = ingest_altcoin(altcoin_symbols, lookback_days)
    return {
        "kr": kr,
        "us": us,
        "altcoin": alt,
        "kr_ok": sum(1 for r in kr if r.get("ok")),
        "kr_fail": sum(1 for r in kr if not r.get("ok")),
        "us_ok": sum(1 for r in us if r.get("ok")),
        "us_fail": sum(1 for r in us if not r.get("ok")),
        "alt_ok": sum(1 for r in alt if r.get("ok")),
        "alt_fail": sum(1 for r in alt if not r.get("ok")),
        "ts": int(time.time()),
    }


def ingest_us_peer(
    symbols: Iterable[str] = DEFAULT_US_PEERS,
    lookback_days: int = 60,
) -> List[Dict[str, Any]]:
    """v227 P1：批量 ingest 美股 peer（默认 MU）。"""
    out: List[Dict[str, Any]] = []
    for s in symbols:
        out.append(_ingest_one(s, lookback_days))
    return out


def reset_cache() -> None:
    """测试 / 调试：清缓存。"""
    with _cache_lock:
        _cache.clear()


# ─── Daemon ───────────────────────────────────────────────────────────────

def daemon_loop(interval_s: int = 300, lookback_days: int = 60) -> None:
    """周期拉取守护（默认 5 分钟）。

    每次调 ingest_all() 把韩股 + 山寨币 K 线写入 external_indicators。
    拉取失败不抛（内部 try/except 落到 ok=False），但若全盘失败会打 warn。
    间隔可由环境变量 YFINANCE_INTERVAL_S 覆盖（最少 60s）。

    守护为阻塞循环；daemon=True 时由 lifespan 启动线程。
    """
    import os
    interval_s = max(60, int(os.environ.get("YFINANCE_INTERVAL_S", interval_s)))
    print(f"[YFINANCE] daemon started | interval={interval_s}s | "
          f"lookback_days={lookback_days} | "
          f"kr={DEFAULT_KR_SYMBOLS} us={DEFAULT_US_PEERS} altcoin={DEFAULT_ALTCOIN_SYMBOLS}",
          flush=True)
    while True:
        try:
            r = ingest_all(lookback_days=lookback_days)
            kr_ok = r.get("kr_ok", 0); kr_n = len(r.get("kr", []))
            us_ok = r.get("us_ok", 0); us_n = len(r.get("us", []))
            alt_ok = r.get("alt_ok", 0); alt_n = len(r.get("altcoin", []))
            print(f"[YFINANCE] @ {r['ts']:.0f} kr={kr_ok}/{kr_n} "
                  f"us={us_ok}/{us_n} alt={alt_ok}/{alt_n}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[YFINANCE] loop err: {type(exc).__name__}: {exc}",
                  flush=True)
        time.sleep(interval_s)


# ─── CLI 调试 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="单次拉取")
    p.add_argument("--daemon", action="store_true", help="周期 daemon")
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--lookback", type=int, default=60)
    args = p.parse_args()
    if args.daemon:
        daemon_loop(interval_s=args.interval, lookback_days=args.lookback)
    else:
        r = ingest_all(lookback_days=args.lookback)
        import json as _json
        print(_json.dumps({k: v for k, v in r.items() if k not in ("kr", "altcoin")},
                          ensure_ascii=False, indent=2))


__all__ = [
    "DEFAULT_KR_SYMBOLS",
    "DEFAULT_US_PEERS",
    "DEFAULT_ALTCOIN_SYMBOLS",
    "DEFAULT_ALL_SYMBOLS",
    "fetch_history",
    "ingest_kr_equity",
    "ingest_us_peer",
    "ingest_altcoin",
    "ingest_all",
    "daemon_loop",
    "reset_cache",
]
