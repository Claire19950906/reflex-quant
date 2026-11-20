#!/usr/bin/env python3
"""股票市值序列 —— PB 分子的**可推导**通道（v454）。

为什么需要它：v448 实测 `eq.pb` 分位 0/16 可用，归因是"分子缺一半、分母缺一半"。
分母（净资产）由 `sec_fundamentals` 补；分子（市值）原来的唯一来源是
`macro_snapshots.eq.mktcap` 快照，MU/CLSK/HUT/SMCI 全 NULL（FMP 对这些票不返回
marketCap），新加的 12 只票也只积累了 2 天。

推导口径（**只做已公布数据的乘法，不造任何数**）：
    市值(日) = 该日「已公布的股本」 × 该日收盘价
  · 股本：yfinance `get_shares_full` 的**真实历史股本序列**（实测 AAPL 27 点 /
    MU 51 点 / MARA 23 点，MARA 一年内从 3.70 亿增发到 3.87 亿——这正是不该
    用"当前股本"回填历史的理由）；
  · 收盘：优先 `ohlcv_daily`（本系统自己的日线），缺则 yfinance 历史（落盘缓存）；
  · 取"该日或之前最近一次公布"的股本 → 不会用到未来才披露的股本。

为什么这不违反「恢复时刻为新起点、不补历史」：
  那条纪律针对的是**采集中断后不重放积压的采集任务**（不把停机期的快照补成
  "看起来连续"的时间序列）。这里做的是**用已公布的两个真实数相乘**，和
  `pe_history`（收盘 ÷ 已公布 EPS TTM 倒推 PE）是同一类推导，不是伪造采集。
  口径在返回值里带 `basis` 标签，复盘能看见"这是推导的、股本用的是哪一期"。

不做什么：
  · 不用当前股本回填一年（增发票会算出假 PB）；
  · 不在缺股本/缺收盘时插值凑点（缺就是缺，分位样本宁少不多）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

_SHARES_TTL = 7 * 86400
_HISTORY_TTL = 12 * 3600
_MIN_POINTS = 3


def _cache_dir() -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "runtime", "mktcap_cache")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "/tmp"
    return base


def _disk_json(name: str, ttl: float) -> Optional[Any]:
    path = os.path.join(_cache_dir(), name)
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        return None
    return None


def _write_json(name: str, data: Any) -> None:
    try:
        with open(os.path.join(_cache_dir(), name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass


# ── 股本序列 ────────────────────────────────────────────────────────────────
def shares_series(ticker: str, days: int = 420) -> List[Tuple[int, float]]:
    """[(ts, shares)] 升序。落盘缓存 7 天；抓不到返回 []（不猜）。

    Yahoo 的股本接口间歇性返回 "web request for share count failed"（实测同一分钟
    重试就成功），所以带 3 次退避重试。失败不写缓存，下一次调用会重新尝试。
    """
    ticker = str(ticker).upper()
    cached = _disk_json(f"shares_{ticker}.json", _SHARES_TTL)
    if isinstance(cached, list) and cached:
        return [(int(ts), float(v)) for ts, v in cached]
    for attempt in range(3):
        out = _shares_once(ticker, days)
        if out:
            _write_json(f"shares_{ticker}.json", [[ts, v] for ts, v in out])
            return out
        time.sleep(0.8 * (attempt + 1))
    return []


def _shares_once(ticker: str, days: int) -> List[Tuple[int, float]]:
    try:
        import datetime as _dt
        import yfinance as yf
        start = (_dt.date.today() - _dt.timedelta(days=int(days))).isoformat()
        raw = yf.Ticker(ticker).get_shares_full(start=start)
        if raw is None or len(raw) == 0:
            return []
        out: List[Tuple[int, float]] = []
        for idx, val in raw.items():
            try:
                shares = float(val)
            except (TypeError, ValueError):
                continue
            if shares <= 0:
                continue
            try:
                ts = int(idx.timestamp())
            except Exception:
                continue
            out.append((ts, shares))
        out.sort()
        return out if len(out) >= _MIN_POINTS else []
    except Exception:
        return []


def current_shares(ticker: str) -> Optional[float]:
    """当日股本兜底（只用于"今天这一个点"，**不**回填历史）。

    历史序列抓不到时，至少让今天的市值有据可依：今天用今天的股本是对的，
    拿今天的股本去解释一年前的价格才是错的（增发票会被算成"一路变便宜"）。
    """
    ticker = str(ticker).upper()
    cached = _disk_json(f"current_shares_{ticker}.json", 3600)
    if cached:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    for attempt in range(2):
        try:
            import yfinance as yf
            val = yf.Ticker(ticker).fast_info.get("shares")
            if val and float(val) > 0:
                _write_json(f"current_shares_{ticker}.json", float(val))
                return float(val)
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None


def shares_asof(series: List[Tuple[int, float]], ts: int) -> Optional[float]:
    """该时刻「已知」的最近一次股本（取 <= ts 的最后一个点）。"""
    known = None
    for when, val in series:
        if when <= ts:
            known = val
        else:
            break
    return known


# ── 收盘价 ──────────────────────────────────────────────────────────────────
def _db_closes(conn: Any, ticker: str, days: int) -> List[Tuple[int, float]]:
    try:
        start = int(time.time()) - (int(days) + 10) * 86400
        rows = conn.execute(
            """SELECT ts, close FROM ohlcv_daily
               WHERE asset=? AND ts >= ? AND close > 0 ORDER BY ts""",
            (str(ticker).upper(), start),
        ).fetchall()
        # 同一天可能多根（小时级）→ 取当日最后一根当收盘
        daily: Dict[int, float] = {}
        for ts, close in rows:
            daily[int(ts) // 86400] = float(close)
        return [(d * 86400, v) for d, v in sorted(daily.items())]
    except Exception:
        return []


def _yf_closes(ticker: str, days: int) -> List[Tuple[int, float]]:
    ticker = str(ticker).upper()
    cached = _disk_json(f"closes_{ticker}_{int(days)}.json", _HISTORY_TTL)
    if isinstance(cached, list) and cached:
        return [(int(ts), float(v)) for ts, v in cached]
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{max(1, int(days) // 250 + 1)}y",
                                         interval="1d", auto_adjust=False)
        if hist is None or len(hist) == 0:
            return []
        out: List[Tuple[int, float]] = []
        for idx, row in hist.iterrows():
            try:
                close = float(row["Close"])
            except Exception:
                continue
            if close <= 0:
                continue
            ts = int(idx.timestamp())
            out.append((int(ts) // 86400 * 86400, close))
        out.sort()
        _write_json(f"closes_{ticker}_{int(days)}.json", [[ts, v] for ts, v in out])
        return out
    except Exception:
        return []


def closes(conn: Any, ticker: str, days: int = 365) -> List[Tuple[int, float]]:
    rows = _db_closes(conn, ticker, days)
    if len(rows) >= 60:
        return rows
    net = _yf_closes(ticker, days)          # 库内不够 60 天就用 yfinance 历史（免费）
    if len(net) > len(rows):
        return net
    return rows


# ── 推导市值 ────────────────────────────────────────────────────────────────
def derived_series(conn: Any, ticker: str, days: int = 365) -> List[Tuple[int, float, str]]:
    """[(ts, mktcap_usd, basis)] 升序。

    basis: `shares×db_close` / `shares×yf_close` / 空表示不可用。
    """
    series = shares_series(ticker)
    if not series:
        # 历史股本不可得 → 只给"今天"一个点（今天用今天的股本是对的），
        # 不给历史分位（拿当前股本解释一年前价格会算出假 PB）。
        cur = current_shares(ticker)
        px_now = closes(conn, ticker, days=max(10, int(days)))
        if cur and px_now:
            last = px_now[-1]
            return [(int(last[0]), float(cur) * float(last[1]), "shares_current×close")]
        return []
    px = closes(conn, ticker, days)
    if len(px) < _MIN_POINTS:
        return []
    basis_src = "db" if len(_db_closes(conn, ticker, days)) >= 60 else "yf"
    out: List[Tuple[int, float, str]] = []
    for ts, close in px:
        shares = shares_asof(series, ts)
        if not shares:
            continue
        out.append((ts, float(shares) * float(close), f"shares×{basis_src}_close"))
    return out


# ── 每日落库（让"按天累积"这条路也真的在长）────────────────────────────────
def ensure_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS equity_mktcap_daily (
            ticker TEXT NOT NULL,
            day TEXT NOT NULL,
            ts INTEGER NOT NULL,
            shares REAL,
            close REAL,
            mktcap_usd REAL,
            source TEXT,
            PRIMARY KEY (ticker, day)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mktcap_day ON equity_mktcap_daily(day)")


def fill_today(conn: Any, tickers: Optional[List[str]] = None,
               days_back: int = 3, dry_run: bool = False) -> Dict[str, int]:
    """把最近几天的推导市值写进 `equity_mktcap_daily`（幂等 upsert）。

    写锁纪律：只写最近 3 天，短事务，失败重试 —— 和 SEC 通道同一套纪律。
    """
    import config
    tickers = tickers or list(config.EQUITY_TICKERS)
    stats = {"tickers": 0, "rows": 0, "no_shares": 0, "no_close": 0}
    payload: List[Tuple[str, str, int, float, float, float, str]] = []
    for t in tickers:
        t = str(t).upper()
        series = shares_series(t)
        if not series:
            stats["no_shares"] += 1
            continue
        px = closes(conn, t, days=max(10, int(days_back) + 5))
        if not px:
            stats["no_close"] += 1
            continue
        keep = px[-max(1, int(days_back)):]
        for ts, close in keep:
            shares = shares_asof(series, ts)
            if not shares:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(int(ts) + 43200))
            payload.append((t, day, int(ts) // 86400 * 86400, float(shares),
                            float(close), float(shares) * float(close), "derived"))
        stats["tickers"] += 1
    if dry_run or not payload:
        stats["rows"] = len(payload)
        return stats
    for attempt in range(6):
        try:
            ensure_table(conn)
            conn.executemany(
                """INSERT INTO equity_mktcap_daily
                   (ticker, day, ts, shares, close, mktcap_usd, source)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(ticker, day) DO UPDATE SET
                     ts=excluded.ts, shares=excluded.shares, close=excluded.close,
                     mktcap_usd=excluded.mktcap_usd, source=excluded.source""",
                payload,
            )
            conn.commit()
            stats["rows"] = len(payload)
            return stats
        except Exception as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            time.sleep(min(8.0, 0.6 * (2 ** attempt)))
    stats["rows"] = 0
    return stats


def db_series(conn: Any, ticker: str, days: int = 365) -> List[Tuple[int, float]]:
    """已落库的市值序列（美元）。"""
    try:
        ensure_table(conn)
        start = int(time.time()) - int(days) * 86400
        rows = conn.execute(
            """SELECT ts, mktcap_usd FROM equity_mktcap_daily
               WHERE ticker=? AND ts >= ? AND mktcap_usd > 0 ORDER BY ts""",
            (str(ticker).upper(), start),
        ).fetchall()
        return [(int(ts), float(v)) for ts, v in rows]
    except Exception:
        return []


__all__ = ["shares_series", "current_shares", "shares_asof", "closes",
           "derived_series", "ensure_table", "fill_today", "db_series"]
