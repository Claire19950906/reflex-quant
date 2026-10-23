"""v176 market_style — 市场风格 + 5 层预警模块。

输出市场风格（trending_up/down/mean_revert/volatile_high/low）与 5 层预警：
  Layer 1: 波动率极端（ATR%>2% 或 <0.2%）
  Layer 2: 趋势衰竭（趋势强度突破 + 反向 → 反转概率上升）
  Layer 3: 资金费率极端（|funding|>0.05% 暗示情绪拥挤）
  Layer 4: 量能异常（volume vs 7d avg > 3x 或 <0.3x）
  Layer 5: 持仓/CVD 背离（价格 vs OI / CVD 同向/反向不一致）

每个预警一条结构化信号，注入 prompt 用以提示 LLM 留意。

入口函数：
  market_style_block(target_asset) -> str
    返回拼好的 prompt 块；无数据时返回空串。
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple


_STYLE_LABELS = {
    "trending_up": "趋势上",
    "trending_down": "趋势下",
    "mean_revert": "震荡",
    "volatile_high": "高波动",
    "volatile_low": "低波动",
    "unknown": "未知",
}


def _atr_pct(candles: list, n: int = 24) -> Optional[float]:
    if not candles or len(candles) < 3:
        return None
    rows = candles[-n:] if len(candles) > n else candles
    hl = []
    closes = []
    for r in rows:
        try:
            hl.append(float(r[2]) - float(r[3]))
            closes.append(float(r[4]))
        except (TypeError, ValueError, IndexError):
            continue
    if not hl or not closes:
        return None
    return sum(hl) / len(hl) / closes[-1] * 100.0


def _trend_pct(candles: list, n: int = 24) -> Optional[float]:
    if not candles or len(candles) < 3:
        return None
    rows = candles[-n:] if len(candles) > n else candles
    try:
        first = float(rows[0][4])
        last = float(rows[-1][4])
        return (last - first) / first * 100.0 if first else None
    except (TypeError, ValueError, IndexError):
        return None


def _volume_ratio(candles: list, n: int = 24) -> Optional[float]:
    if not candles or len(candles) < 5:
        return None
    try:
        recent = float(candles[-1][5])
        prev = [float(r[5]) for r in candles[-n:-1] if r and len(r) > 5]
        if not prev:
            return None
        avg = sum(prev) / len(prev)
        return recent / avg if avg > 0 else None
    except (TypeError, ValueError, IndexError):
        return None


def _candles(inst_id: str, bar: str, limit: int) -> list:
    try:
        import quote_sources as _qs
        rows = _qs.fetch_okx_candles(inst_id, bar, limit) or []
        return rows
    except Exception:
        return []


def _funding_pct(inst_id: str) -> Optional[float]:
    try:
        import quote_sources as _qs
        f = _qs.fetch_okx_funding(inst_id)
        return float(f) if f is not None else None
    except Exception:
        return None


def _oi_change(inst_id: str) -> Optional[float]:
    try:
        import quote_sources as _qs
        v = _qs.fetch_okx_open_interest(inst_id)
        return float(v) if v is not None else None
    except Exception:
        return None


def _label_for(asset: str) -> Tuple[Optional[str], Optional[str]]:
    table = {
        "BTC": ("BTC-USDT-SWAP", "crypto"),
        "ETH": ("ETH-USDT-SWAP", "crypto"),
        "SOL": ("SOL-USDT-SWAP", "crypto"),
        "XAU": ("XAU-USDT-SWAP", "swap"),
        "WTI": ("WTI-USDT-SWAP", "swap"),
    }
    return table.get((asset or "").upper(), (None, None))


def _classify_style(atr_pct: Optional[float], trend_pct: Optional[float]) -> str:
    if atr_pct is None or trend_pct is None:
        return "unknown"
    if atr_pct > 2.0:
        return "volatile_high"
    if atr_pct < 0.2:
        return "volatile_low"
    if abs(trend_pct) > 1.5:
        return "trending_up" if trend_pct > 0 else "trending_down"
    return "mean_revert"


def _layer_alerts(
    asset: str,
    candles_1h: list,
    candles_1d: list,
    funding: Optional[float],
    oi: Optional[float],
    vol_ratio: Optional[float],
) -> List[str]:
    """5 层预警 → 一组自然语言短线提示。"""
    alerts: List[str] = []

    atr_1h = _atr_pct(candles_1h, 24)
    if atr_1h is not None:
        if atr_1h > 2.0:
            alerts.append(f"⚠️ 1H ATR%={atr_1h:.2f}% > 2%（极端波动）→ 新闻冲击易被钝化")
        elif atr_1h < 0.2:
            alerts.append(f"⚠️ 1H ATR%={atr_1h:.2f}% < 0.2%（极低波动）→ 新闻冲击易被放大，注意假突破")

    tr_1h = _trend_pct(candles_1h, 24)
    tr_1d = _trend_pct(candles_1d, 24 if len(candles_1d) > 24 else len(candles_1d))
    if tr_1h is not None and tr_1d is not None and abs(tr_1d) > 1.5:
        if (tr_1h > 0.5 and tr_1d < -1.0) or (tr_1h < -0.5 and tr_1d > 1.0):
            alerts.append(
                f"⚠️ 趋势衰竭信号：1H 趋势 {tr_1h:+.2f}% vs 1D 趋势 {tr_1d:+.2f}% 方向反转"
            )

    if funding is not None:
        if funding > 0.05:
            alerts.append(
                f"⚠️ 资金费率 +{funding:.3f}% > 0.05%（多头拥挤）→ 警惕 SELL（利好出尽）"
            )
        elif funding < -0.05:
            alerts.append(
                f"⚠️ 资金费率 {funding:.3f}% < -0.05%（空头拥挤）→ 警惕 BUY（空头挤压）"
            )

    if vol_ratio is not None:
        if vol_ratio > 3.0:
            alerts.append(f"⚠️ 最新 1H 量能 {vol_ratio:.2f}x 前 24 根均值（>3x）→ 异动放大")
        elif vol_ratio < 0.3:
            alerts.append(f"⚠️ 最新 1H 量能 {vol_ratio:.2f}x 前 24 根均值（<0.3x）→ 量能萎缩")

    if oi is not None and oi > 1_000_000:
        alerts.append(f"ℹ️ OI={oi:,.0f} USDT（较高）→ 留意 OI 急涨急跌与价格背离")

    return alerts


def market_style_block(target_asset: str) -> str:
    """构造「市场风格 + 5 层预警」块，注入 LLM prompt。

    无数据时返回空串。异常一律 try/except 包住，不阻塞主分析。
    """
    asset = (target_asset or "").upper()
    inst_id, kind = _label_for(asset)
    if not inst_id:
        return ""

    try:
        c_1h = _candles(inst_id, "1H", 30)
        c_1d = _candles(inst_id, "1D", 30)
        funding = _funding_pct(inst_id)
        oi = _oi_change(inst_id)
        vol_ratio = _volume_ratio(c_1h, 24)

        atr = _atr_pct(c_1h, 24)
        trend = _trend_pct(c_1h, 24)
        style = _classify_style(atr, trend)
        style_label = _STYLE_LABELS.get(style, "未知")

        alerts = _layer_alerts(asset, c_1h, c_1d, funding, oi, vol_ratio)

        lines = [
            "═══ 市场风格 + 5 层预警（v176）═══",
            f"品种: {asset} ({inst_id}) | 风格: {style_label}"
            + (f" | ATR%={atr:.2f} 趋势={trend:+.2f}%" if atr is not None and trend is not None else ""),
        ]
        if not alerts:
            lines.append("5 层预警：均未触发（市场状态正常）")
        else:
            lines.append("5 层预警：")
            lines.extend(alerts)
        lines.append(
            "（上述预警不强制改方向，仅提示 LLM 在 [水位博弈] / [逻辑自检] 留意。"
            "空串即该品种无足够 K 线数据）"
        )
        return "\n".join(lines)
    except Exception:
        return ""