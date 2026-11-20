#!/usr/bin/env python3
"""宏观 → 个股板块的传导（v454）。

问题：33 组 FRED 指标在系统里只被拼成一段 prompt 文本（`macro_context.render_prompt_block`），
`decision_guard` 里**没有任何宏观权重**。也就是说"利率上行了 30bp"这件事能不能影响一只
成长股的方向分，完全取决于 LLM 那天有没有注意到 —— 不可复现、不可审计、不可回测。

做法：把宏观读数压成 4 个无量纲驱动，再按人工维护的板块敏感度映射成 [-1, 1] 的倾斜分：
  R = 长端利率压力（10Y 20 日变动，+1 = 快速上行） → 利空长久期资产
  K = 风险偏好（VIX 水平 + 高收益债利差变动）          → 利好高 beta
  D = 美元压力（DXY 20 日变动）                       → 利空海外收入占比高的票
  C = 曲线压力（10Y-2Y 倒挂深度）                      → 利空周期股

  tilt(板块) = -wR·R + wK·K - wD·D - wC·C ，截到 ±1

这份倾斜**不是新因子**：它以 ±0.15 的幅度并入既有的 `fundamental`（权重 0.08）里，
所以对最终方向分的影响上限是 0.08×0.15 ≈ 1.2%，远小于一条新闻。作用是把"宏观背景"
变成一条可复现的证据，而不是让它当主角。

不做：不做宏观择时、不预测利率、不用单一指标（VIX 高不等于跌，倒挂不等于衰退）。
每个驱动都要求 **至少 10 个交易日**的序列，否则整块判不可用。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_MIN_DAYS = 10
_WINDOW = 20

# 板块敏感度（人工维护）：权重和为 1，方向见上表说明。
SECTOR_SENSITIVITY: Dict[str, Tuple[float, float, float, float]] = {
    #              wR(利率)  wK(风险)  wD(美元)  wC(曲线)
    "semis":        (0.45,     0.30,     0.10,     0.15),
    "ai_growth":    (0.60,     0.25,     0.05,     0.10),
    "mega_tech":    (0.45,     0.20,     0.25,     0.10),
    "crypto_miner": (0.15,     0.75,     0.05,     0.05),
    "crypto_asset": (0.20,     0.70,     0.05,     0.05),
}

_KEYS = ("dgs10", "dgs2", "t10y2y", "vix", "hy_oas", "dxy")


def _daily(conn: sqlite3.Connection, key: str, days: int = 90) -> List[Tuple[int, float]]:
    cutoff = int(time.time()) - int(days) * 86400
    try:
        rows = conn.execute(
            """SELECT ts, value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL AND ts >= ?
               ORDER BY ts""",
            (key, cutoff),
        ).fetchall()
    except Exception:
        return []
    daily: Dict[int, float] = {}
    for ts, value in rows:
        daily[int(ts) // 86400] = float(value)     # 同一天只留最后一条
    return [(d * 86400, v) for d, v in sorted(daily.items())]


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _change(series: List[Tuple[int, float]], window: int = _WINDOW) -> Optional[float]:
    if len(series) < max(_MIN_DAYS, window + 1):
        return None
    return series[-1][1] - series[-window - 1][1]


def build(conn: Optional[sqlite3.Connection] = None, days: int = 90) -> Dict[str, Any]:
    """当前宏观状态 + 各板块倾斜分。数据不足时 available=False。"""
    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        series = {k: _daily(conn, k, days) for k in _KEYS}
    except Exception:
        return {"available": False, "tilt": {}, "drivers": {}, "evidence": [],
                "detail": "宏观序列不可读"}
    finally:
        if own and conn is not None:
            conn.close()

    d10 = series["dgs10"]
    if len(d10) < _MIN_DAYS:
        return {"available": False, "tilt": {}, "drivers": {}, "evidence": [],
                "detail": f"10Y 序列仅 {len(d10)} 天 < {_MIN_DAYS}"}

    evidence: List[str] = []
    drivers: Dict[str, Optional[float]] = {}

    chg10 = _change(d10)
    drivers["rate"] = None if chg10 is None else round(_clamp(chg10 / 0.30), 3)
    if chg10 is not None:
        evidence.append(f"10Y {d10[-1][1]:.2f}%（20日 {chg10:+.2f}pp）")

    vix = series["vix"]
    hy = series["hy_oas"]
    k_parts: List[float] = []
    if len(vix) >= _MIN_DAYS:
        k_parts.append(_clamp((18.0 - vix[-1][1]) / 8.0))
        evidence.append(f"VIX {vix[-1][1]:.1f}")
    hy_chg = _change(hy)
    if hy_chg is not None:
        k_parts.append(_clamp(-hy_chg / 0.40))     # 利差走阔 = 风险偏好下降
        evidence.append(f"高收益利差 {hy[-1][1]:.2f}%（20日 {hy_chg:+.3f}pp）")
    drivers["risk"] = round(sum(k_parts) / len(k_parts), 3) if k_parts else None

    dxy = series["dxy"]
    chg_dxy = _change(dxy)
    if chg_dxy is not None and dxy and dxy[-1][1]:
        pct = chg_dxy / dxy[-1][1] * 100.0
        drivers["usd"] = round(_clamp(pct / 3.0), 3)
        evidence.append(f"DXY {dxy[-1][1]:.1f}（20日 {pct:+.1f}%）")
    else:
        drivers["usd"] = None

    curve = series["t10y2y"]
    if curve:
        drivers["curve"] = round(_clamp(-curve[-1][1] / 0.50), 3)   # 倒挂越深 → 越大
        evidence.append(f"10Y-2Y {curve[-1][1]:+.2f}pp")
    else:
        drivers["curve"] = None

    tilt: Dict[str, float] = {}
    for sector, (wr, wk, wd, wc) in SECTOR_SENSITIVITY.items():
        r = drivers.get("rate") or 0.0
        k = drivers.get("risk") or 0.0
        d = drivers.get("usd") or 0.0
        c = drivers.get("curve") or 0.0
        tilt[sector] = round(_clamp(-wr * r + wk * k - wd * d - wc * c), 4)

    usable = [v for v in drivers.values() if v is not None]
    if not usable:
        return {"available": False, "tilt": {}, "drivers": drivers,
                "evidence": evidence, "detail": "四个驱动全缺"}
    return {
        "available": True,
        "drivers": drivers,
        "tilt": tilt,
        "evidence": evidence[:4],
        "detail": f"驱动可得 {len(usable)}/4",
        "asof": int(d10[-1][0]),
    }


_REGIME_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_REGIME_TTL = 1800      # 30 分钟：宏观是慢变量，逐笔决策不需要重算


def cached_build(conn: Optional[sqlite3.Connection] = None, ttl: float = _REGIME_TTL) -> Dict[str, Any]:
    """带进程内缓存的 build()。ai_worker 每个批次会问多次，不能每次扫 6 条序列。"""
    now = time.time()
    hit = _REGIME_CACHE.get("data")
    if hit and now - float(_REGIME_CACHE.get("ts") or 0) < ttl:
        return hit
    data = build(conn)
    _REGIME_CACHE["ts"] = now
    _REGIME_CACHE["data"] = data
    return data


def tilt_for(ticker: str, conn: Optional[sqlite3.Connection] = None,
             regime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """单票所在板块的宏观倾斜分（板块未知/驱动缺失时 available=False）。"""
    import peer_valuation as _pv
    sector = _pv.sector_of(ticker)
    if not sector or sector not in SECTOR_SENSITIVITY:
        return {"available": False, "tilt": None, "sector": sector, "detail": "板块未映射"}
    reg = regime if regime is not None else build(conn)
    if not reg.get("available"):
        return {"available": False, "tilt": None, "sector": sector,
                "detail": str(reg.get("detail") or "")}
    return {"available": True, "tilt": reg["tilt"].get(sector), "sector": sector,
            "drivers": reg.get("drivers"), "evidence": reg.get("evidence") or [],
            "detail": f"{sector} 宏观倾斜 {reg['tilt'].get(sector):+.2f}"}


__all__ = ["SECTOR_SENSITIVITY", "build", "cached_build", "tilt_for"]
