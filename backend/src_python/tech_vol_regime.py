#!/usr/bin/env python3
"""tech_vol_regime.py —— 技术面的"品种类别 + 波动状态"口径（v456）。

为什么单独一个模块
------------------
基本面那套 `asset_kinds.classify()` 回答的是"**这是不是一家公司、代码解析出来没有**"
（non_equity / placeholder / us / a_share / hk）。技术面要的是另一件事：
"这类标的的波动结构和漂移是否相似"。BTC 和 AAPL 归成同一类做技术面统计，
等于把两种完全不同的波动混在一起算基线——结论会系统性偏掉。

所以这里单独维护：
  · 品种类别  crypto / metal / energy / index / equity / other
  · 波动档位  low / mid / high（ATR14 占收盘价的百分比，在**自身**近 200 根里的分位）

为什么用"自身历史分位"而不是绝对阈值：
XAU 的 ATR 3% 已经算剧烈，AAPL 的 3% 只是平常。同一把绝对尺子量不出
"相对自己是否极端"，也就没法比较不同品种的"高波动"。

因果约束：档位只用 ts 及之前的数据；历史不足 120 根一律返回 None，
宁可"不知道"，也不默认塞进中档——那等于凭空造出一个状态。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ── 品种类别 ──────────────────────────────────────────────────────────────
ASSET_CLASS: Dict[str, str] = {}
_CLASS_MEMBERS = {
    "crypto": ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LTC", "BCH"),
    "metal": ("XAU", "XAG", "HG", "COPPER", "GOLD", "SILVER"),
    "energy": ("WTI", "BRENT", "OIL", "NG"),
    "index": ("SPX", "NDX", "DJI", "IXIC", "SHCOMP", "HSI", "CSI300"),
    "equity": ("AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "MU", "NVDA", "TSLA",
               "TENCENT", "SKHYNIX", "000660.KS", "NVDA.US"),
}
for _cls, _members in _CLASS_MEMBERS.items():
    for _a in _members:
        ASSET_CLASS[_a] = _cls

VOL_WINDOW = 200      # 分位窗口（根）
VOL_MIN_HISTORY = 120 # 少于这个根数不算档位


def asset_class(asset: object) -> str:
    return ASSET_CLASS.get(str(asset).upper(), "other")


def is_known_class(asset: object) -> bool:
    return str(asset).upper() in ASSET_CLASS


# ── 波动档位 ──────────────────────────────────────────────────────────────
Bar = Tuple[int, float, float, float]   # (ts, high, low, close)


def vol_bucket_series(bars: List[Bar]) -> Dict[int, str]:
    """{ts: low|mid|high}。ATR14% 在近 VOL_WINDOW 根里的分位三分。"""
    n = len(bars)
    if n < 15:
        return {}
    tr: List[Optional[float]] = [None] * n
    for i in range(n):
        _, high, low, close = bars[i]
        if i == 0:
            tr[i] = high - low
        else:
            pc = bars[i - 1][3]
            tr[i] = max(high - low, abs(high - pc), abs(low - pc))
    atr: List[Optional[float]] = [None] * n
    prev: Optional[float] = None
    for i in range(14, n):
        prev = sum(tr[i - 13:i + 1]) / 14.0 if prev is None else (prev * 13 + tr[i]) / 14.0
        atr[i] = prev
    atrp: List[Optional[float]] = [None] * n
    for i in range(n):
        if atr[i] is not None and bars[i][3] > 0:
            atrp[i] = atr[i] / bars[i][3] * 100.0
    out: Dict[int, str] = {}
    window: List[float] = []
    for i in range(n):
        v = atrp[i]
        if v is not None:
            window.append(v)
        if len(window) > VOL_WINDOW:
            window.pop(0)
        if v is None or len(window) < VOL_MIN_HISTORY:
            continue
        rank = sum(1 for x in window if x < v) / len(window)
        out[bars[i][0]] = "low" if rank < 0.33 else ("high" if rank > 0.67 else "mid")
    return out


def load_bars(conn: sqlite3.Connection,
              upto_ts: Optional[int] = None) -> Dict[str, List[Bar]]:
    sql = ("SELECT asset, ts, high, low, close FROM ohlcv_daily"
           " WHERE close>0 AND high>0 AND low>0")
    args: tuple = ()
    if upto_ts is not None:
        sql += " AND ts<=?"
        args = (int(upto_ts),)
    sql += " ORDER BY asset, ts"
    out: Dict[str, List[Bar]] = defaultdict(list)
    for asset, ts, high, low, close in conn.execute(sql, args).fetchall():
        out[asset].append((int(ts), float(high), float(low), float(close)))
    return out


def vol_bucket(conn: sqlite3.Connection, asset: str,
               ts: int) -> Optional[str]:
    """信号当根所处的波动档位。只读 ts 及之前的 bar（因果）。"""
    rows = conn.execute(
        "SELECT ts, high, low, close FROM ohlcv_daily"
        " WHERE asset=? AND ts<=? AND close>0 AND high>0 AND low>0"
        " ORDER BY ts DESC LIMIT ?",
        (asset, int(ts), VOL_WINDOW),
    ).fetchall()
    if len(rows) < VOL_MIN_HISTORY:
        return None
    bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in reversed(rows)]
    return vol_bucket_series(bars).get(int(rows[0][0]))
