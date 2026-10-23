# -*- coding: utf-8 -*-
"""跨资产 K 线形态上下文 — v225 Stage 5.5 反思系统扩展。

设计动机：
  用户指出反思系统"没有看跨资产的 K 线形态，所有金融产品都跌了" — 即反思应能看到
  同时间 BTC/SPX/DXY/VIX/UST/TLT/GOLD 的走势，理解核心涨跌的传导逻辑，而不是只盯
  当前品种。

输入：时间戳 + 当前品种
输出：{symbol: {pct_1h, pct_24h, pct_72h, dir, regime}}  + 整体 market_regime 标签

数据源：
  1. external_indicators 表（v225 Stage 5 外部 ingest）
  2. factor_snapshots 表（v170 内部因子快照，含 BTC/USD/SPX/VIX）
  3. 兜底：空 dict（无数据时）

调用方：
  - /api/reflection/recent 端点输出 cross_asset_snapshot
  - reflection_run 时给每个 decision 注入 background 字段
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import db


# 核心跨资产 symbol 列表 — 这些覆盖"全市场都在跌/都在涨"的判断维度
CROSS_ASSET_SYMBOLS = [
    "BTC-USD",   # 加密大盘
    "^GSPC",    # S&P 500
    "^VIX",     # 恐慌指数（>25 风险偏好收缩）
    "DX-Y.NYB", # 美元指数
    "TLT",      # 20Y+ 国债 ETF（避险）
    "GLD",      # 黄金 ETF
    "^TNX",     # 10Y 美债收益率
]

# v226: 跨资产行业分类 — 用于异常同步检测（"4 个不同行业同步"判断）
# 把每个 symbol 映射到 1 个行业 class；用户问"4 个不同行业"就是这个集合的 size。
ASSET_CLASS_MAP = {
    "BTC-USD":   "crypto",        # 加密
    "BTC":       "crypto",
    "BTC_score": "crypto",
    "ETH-USD":   "crypto",
    "ETH":       "crypto",
    "SOL-USD":   "crypto",
    "SOL":       "crypto",
    "^GSPC":     "us_equity",     # 美股
    "NDX":       "us_equity_tech", # 美股科技
    "DJI":       "us_equity",     # 美股
    "US_STOCK":  "us_equity",     # 自定义聚合
    "CN_STOCK":  "em_equity",     # EM 股票
    "^VIX":      "volatility",    # 波动率（VIX）
    "DX-Y.NYB":  "dollar",        # 美元
    "USD":       "dollar",
    "TLT":       "safe_bond",     # 避险债
    "IEF":       "safe_bond",
    "GLD":       "safe_metal",    # 避险黄金
    "XAU":       "safe_metal",    # 黄金
    "^TNX":      "yield",         # 美债收益率
    "^TYX":      "yield",
    "WTI":       "energy",        # 能源
    "CL=F":      "energy",
    "XAG":       "safe_metal",    # 白银
    "SILVER":    "safe_metal",
}

# macro regime 启发式（基于跨资产 24h 方向）
def _classify_macro_regime(symbols_pct: Dict[str, float]) -> str:
    if not symbols_pct:
        return "UNKNOWN"
    spx = symbols_pct.get("^GSPC", 0.0)
    vix = symbols_pct.get("^VIX", 0.0)
    dxy = symbols_pct.get("DX-Y.NYB", 0.0)
    tlt = symbols_pct.get("TLT", 0.0)
    btc = symbols_pct.get("BTC-USD", 0.0)
    if spx < -1.5 and btc < -3 and dxy > 0.5:
        return "RISK_OFF_GLOBAL"
    if vix > 15:
        return "RISK_OFF_GLOBAL"
    if spx > 1.5 and btc > 3 and dxy < -0.3:
        return "RISK_ON_GLOBAL"
    if tlt > 1.5 and spx < 0 and btc < 0:
        return "FLIGHT_TO_SAFETY"
    return "MIXED"


def _fetch_external_pct(asset_filter: str, ts_iso: str,
                        lookback_hours: int = 24) -> Dict[str, Dict[str, float]]:
    """从 external_indicators 拉过去 N 小时每个 symbol 的 PctChange。

    v225 修正：ts 是 Unix 秒 (integer)，不是 ISO 字符串。
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        # 兼容 ISO 字符串与 unix 秒
        if isinstance(ts_iso, (int, float)):
            dt = datetime.fromtimestamp(float(ts_iso), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except Exception:
        return out
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    window_start_unix = int((dt - timedelta(hours=lookback_hours)).timestamp())
    anchor_unix = int(dt.timestamp())
    conn = sqlite3.connect(db.get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT indicator, value, ts, regime_tags, source
            FROM external_indicators
            WHERE ts BETWEEN ? AND ?
              AND asset IN (?, '', '*')
              AND indicator IN ({})
            ORDER BY indicator, ts ASC
            """.format(",".join(["?"] * len(CROSS_ASSET_SYMBOLS))),
            (window_start_unix, anchor_unix, asset_filter, *CROSS_ASSET_SYMBOLS),
        ).fetchall()
    finally:
        conn.close()

    grouped: Dict[str, List[Any]] = {}
    for r in rows:
        grouped.setdefault(r["indicator"], []).append(r)

    for sym, items in grouped.items():
        if not items:
            continue
        first = items[0]
        last = items[-1]
        v0 = float(first["value"] or 0)
        v1 = float(last["value"] or 0)
        pct = ((v1 - v0) / v0 * 100) if v0 else 0.0
        out[sym] = {
            "pct_24h": round(pct, 3),
            "latest_value": v1,
            "earliest_value": v0,
            "ts": last["ts"],
            "regime_tags": (last["regime_tags"] or "").split(",") if last["regime_tags"] else [],
            "source": last["source"],
        }
    return out


def _fetch_factor_pct(asset: str, ts_iso: str,
                      lookback_hours: int = 24) -> Dict[str, Dict[str, float]]:
    """从 factor_snapshots 拉过去 N 小时内同一资产 + 跨资产快照。

    v225 修正：ts 是 unix 秒（integer）；用 final_score 作 proxy value，
    factors_snapshots 不直接给价格，而是 final_score 与 raw_action。
    这里我们用 raw_action 推断方向，final_score 取首次末次差值作 pct_24h 代理。
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        if isinstance(ts_iso, (int, float)):
            dt = datetime.fromtimestamp(float(ts_iso), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except Exception:
        return out
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    window_start_unix = int((dt - timedelta(hours=lookback_hours)).timestamp())
    anchor_unix = int(dt.timestamp())

    conn = sqlite3.connect(db.get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT asset, ts, final_score, confidence, raw_action, gated_action
               FROM factor_snapshots
               WHERE asset = ? AND ts BETWEEN ? AND ?
               ORDER BY ts ASC""",
            (asset, window_start_unix, anchor_unix),
        ).fetchall()
        if rows:
            first = rows[0]
            last = rows[-1]
            v0 = float(first["final_score"] or 0)
            v1 = float(last["final_score"] or 0)
            out[f"{asset}_score"] = {
                "pct_24h": round((v1 - v0), 3),   # final_score 已经是 -1~1 区间，直接 delta
                "latest_value": v1,
                "earliest_value": v0,
                "ts": last["ts"],
                "raw_action": last["raw_action"],
                "gated_action": last["gated_action"],
                "source": "factor_snapshots",
            }
        rows = conn.execute(
            """SELECT asset, ts, final_score, raw_action
               FROM factor_snapshots
               WHERE ts BETWEEN ? AND ?
                 AND asset != ?
                 AND asset != 'NONE'
               ORDER BY asset, ts ASC""",
            (window_start_unix, anchor_unix, asset),
        ).fetchall()
        grouped: Dict[str, List[Any]] = {}
        for r in rows:
            grouped.setdefault(r["asset"], []).append(r)
        for sym, items in grouped.items():
            if not items:
                continue
            first = items[0]
            last = items[-1]
            v0 = float(first["final_score"] or 0)
            v1 = float(last["final_score"] or 0)
            out[sym] = {
                "pct_24h": round((v1 - v0), 3),
                "latest_value": v1,
                "earliest_value": v0,
                "ts": last["ts"],
                "raw_action": last["raw_action"],
                "source": "factor_snapshots",
            }
    finally:
        conn.close()
    return out


def cross_asset_snapshot(asset: str, ts_iso: Optional[str] = None) -> Dict[str, Any]:
    """主入口：返回某品种 + 某时间的跨资产上下文。

    Returns:
        {
            "anchor_ts": "...",
            "asset": "BTC",
            "symbols": {symbol: {pct_24h, latest_value, source, ...}},
            "pct_only": {symbol: pct_24h},  # 简化版给 prompt 注入
            "macro_regime": "RISK_OFF_GLOBAL" | ...,
            "risk_off_count": N,
            "risk_on_count": N,
            "warning": "全市场下跌"  # 当 N/M >= 70% 时提示
        }
    """
    if ts_iso is None:
        ts_iso = datetime.now(timezone.utc).isoformat()
    ext = _fetch_external_pct(asset, ts_iso)
    fac = _fetch_factor_pct(asset, ts_iso)
    merged: Dict[str, Dict[str, float]] = {}
    for sym, info in fac.items():
        merged[sym] = info
    for sym, info in ext.items():
        merged[sym] = info

    pct_only = {sym: info.get("pct_24h", 0.0) for sym, info in merged.items()}
    macro_regime = _classify_macro_regime(pct_only)
    risk_off_count = sum(1 for v in pct_only.values() if v < -1.0)
    risk_on_count = sum(1 for v in pct_only.values() if v > 1.0)
    total = len(pct_only)
    warning = ""
    if total >= 3 and risk_off_count / total >= 0.7:
        warning = f"⚠️ {risk_off_count}/{total} 跨资产同步下跌（系统性风险），单笔决策亏损可能是宏观传导"
    elif total >= 3 and risk_on_count / total >= 0.7:
        warning = f"✅ {risk_on_count}/{total} 跨资产同步上涨（系统性风险偏好回升）"

    return {
        "anchor_ts": ts_iso,
        "asset": asset,
        "symbols": merged,
        "pct_only": pct_only,
        "macro_regime": macro_regime,
        "risk_off_count": risk_off_count,
        "risk_on_count": risk_on_count,
        "total_symbols": total,
        "warning": warning,
    }


def format_for_prompt(asset: str, ts_iso: Optional[str] = None,
                      max_chars: int = 800) -> str:
    """压缩成 prompt 可注入的简短字符串。"""
    snap = cross_asset_snapshot(asset, ts_iso)
    lines = [f"[跨资产快照 · {asset} @ {snap['anchor_ts'][:16]} · {snap['macro_regime']}]"]
    if snap["warning"]:
        lines.append(snap["warning"])
    items = sorted(snap["symbols"].items(), key=lambda kv: -(kv[1].get("pct_24h", 0)))
    for sym, info in items[:8]:
        pct = info.get("pct_24h", 0.0)
        arrow = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
        lines.append(f"  {arrow} {sym:<10} 24h {pct:+.2f}%")
    txt = "\n".join(lines)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "…"
    return txt


# ─── 跨资产同步异常检测（v226） ────────────────────────────────────────

def _classify_dir(pct: float, threshold: float) -> int:
    """+1 (up), -1 (down), 0 (flat)."""
    if pct > threshold:
        return 1
    if pct < -threshold:
        return -1
    return 0


def _shape_signature(pct: float, regime: str) -> str:
    """用 pct_24h + macro_regime 估算 K 线形态分类。

    Returns
    -------
    str: 'gap_then_fade' | 'rally_then_drop' | 'steady_up' | 'steady_down' | 'mixed'
    """
    if regime == "RISK_OFF_GLOBAL" and pct < -1.0:
        return "steady_down"
    if regime == "RISK_ON_GLOBAL" and pct > 1.0:
        return "steady_up"
    if pct > 2.0:
        return "steady_up"
    if pct < -2.0:
        return "steady_down"
    return "mixed"


def detect_anomaly(snapshot: Optional[Dict[str, Any]] = None,
                   min_classes: int = 4,
                   min_pct: float = 0.5) -> Dict[str, Any]:
    """v226: 检测"≥ N 个不同行业同步同向"异动。

    Parameters
    ----------
    snapshot : dict, optional
        若 None → 自动取 cross_asset_snapshot() 当前快照。
    min_classes : int
        触发异动的最少不同行业数（默认 4，对应用户原话"超过 4 类不同行业"）。
    min_pct : float
        同向最小幅度（绝对值），低于此视为"基本不动"。

    Returns
    -------
    dict {
      "is_anomaly": bool,
      "direction": "up" | "down" | "none",
      "classes_aligned": int,         # 同向行业数
      "classes_total": int,
      "symbols_aligned": List[str],   # 同向的具体 symbol
      "classes_aligned_names": List[str],  # 同向的行业名
      "shape_consistent": bool,       # 形态是否相似
      "severity": "high" | "med" | "low",
      "message": str,
      "snapshot_ts": str
    }
    """
    if snapshot is None:
        snapshot = cross_asset_snapshot(asset="*", ts_iso=None)

    pct = snapshot.get("pct_only") or {}
    if not pct:
        return {
            "is_anomaly": False,
            "direction": "none",
            "classes_aligned": 0,
            "classes_total": 0,
            "symbols_aligned": [],
            "classes_aligned_names": [],
            "shape_consistent": False,
            "severity": "low",
            "message": "无跨资产数据",
            "snapshot_ts": snapshot.get("anchor_ts", ""),
        }

    # 1) 每个 symbol 投票方向
    up_syms, down_syms, flat_syms = [], [], []
    up_classes, down_classes = set(), set()
    for sym, p in pct.items():
        d = _classify_dir(float(p), min_pct)
        if d > 0:
            up_syms.append(sym)
            cls = ASSET_CLASS_MAP.get(sym, "unknown")
            up_classes.add(cls)
        elif d < 0:
            down_syms.append(sym)
            cls = ASSET_CLASS_MAP.get(sym, "unknown")
            down_classes.add(cls)
        else:
            flat_syms.append(sym)

    up_n = len(up_classes)
    down_n = len(down_classes)
    if up_n >= down_n and up_n >= min_classes:
        direction = "up"
        classes_n = up_n
        symbols = up_syms
        classes = up_classes
    elif down_n > up_n and down_n >= min_classes:
        direction = "down"
        classes_n = down_n
        symbols = down_syms
        classes = down_classes
    else:
        direction = "none"
        classes_n = max(up_n, down_n)
        symbols = up_syms if up_n >= down_n else down_syms
        classes = up_classes if up_n >= down_n else down_classes

    is_anomaly = direction != "none" and classes_n >= min_classes

    # 2) 形态相似度（同向 symbols 的 pct 标准差）
    pcts = [float(pct.get(s, 0)) for s in symbols if abs(float(pct.get(s, 0))) > min_pct]
    shape_consistent = False
    if len(pcts) >= 3:
        mean_p = sum(pcts) / len(pcts)
        var = sum((p - mean_p) ** 2 for p in pcts) / len(pcts)
        std = var ** 0.5
        # 标准差 < mean 绝对值 × 0.5 → 形态相对一致
        shape_consistent = std < (abs(mean_p) * 0.5 + 0.3)

    # 3) severity
    if classes_n >= min_classes + 2:
        severity = "high"
    elif classes_n >= min_classes + 1:
        severity = "med"
    else:
        severity = "low"

    arrow = "🟢" if direction == "up" else ("🔴" if direction == "down" else "⚪")
    verb = "上涨" if direction == "up" else ("下跌" if direction == "down" else "未达阈值")
    if is_anomaly:
        message = (
            f"{arrow} {classes_n} 个不同行业 24h 同步{verb} "
            f"（{len(symbols)} 个品种：{', '.join(sorted(symbols)[:6])}"
            f"{'...' if len(symbols) > 6 else ''}）"
        )
        if shape_consistent:
            message += "｜形态高度一致 → 系统性传导信号"
    else:
        message = f"跨资产同步度低（仅 {classes_n}/{min_classes} 行业同向）"

    return {
        "is_anomaly": is_anomaly,
        "direction": direction,
        "classes_aligned": classes_n,
        "classes_total": len({ASSET_CLASS_MAP.get(s, "unknown") for s in pct.keys()}),
        "symbols_aligned": sorted(symbols),
        "classes_aligned_names": sorted(classes),
        "shape_consistent": shape_consistent,
        "severity": severity,
        "message": message,
        "snapshot_ts": snapshot.get("anchor_ts", ""),
        "macro_regime": snapshot.get("macro_regime", "UNKNOWN"),
    }


__all__ = [
    "cross_asset_snapshot",
    "format_for_prompt",
    "detect_anomaly",
    "CROSS_ASSET_SYMBOLS",
    "ASSET_CLASS_MAP",
]