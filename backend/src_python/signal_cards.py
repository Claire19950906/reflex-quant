# -*- coding: utf-8 -*-
"""信号可执行卡片（v422，纯计算模块，不连库、不写库）。

问题：系统里每天都在写"证据充分，允许输出方向性结论"的决策，但用户看到的只是
一行结论（BUY/SELL + 一段理由）。落到能不能做、做多少、错了在哪停，全靠人脑补。

本模块给每条方向性决策补三样东西：
  1) 止损位 —— 用 ATR 定，不是拍脑袋的百分比；
  2) 仓位 —— 按"单笔风险占总权益 X%"反推，止损越远仓位越小；
  3) 失效条件 —— 什么情况下这条结论作废。

输入是**已取好的 K 线**（调用方负责从 kline_bars 读），所以可以离线单测。
K 线不足时明确标 `estimated=True`（用 2% 兜底），绝不假装算过。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# 单笔风险默认 1%（可配）；ATR 倍数默认 1.5（日内常被扫，2 太宽、1 太紧）
DEFAULT_RISK_PCT = 1.0
DEFAULT_ATR_MULT = 1.5
FALLBACK_STOP_PCT = 2.0


def _norm_ts(ts: Any) -> Optional[int]:
    """kline_bars.ts 混着秒和毫秒（实测 BTC 1m 是 1794931620000），统一成秒。"""
    try:
        v = int(float(ts))
    except Exception:
        return None
    if v <= 0:
        return None
    return v // 1000 if v > 10_000_000_000 else v


def atr(bars: Sequence[Tuple[float, float, float, float]], period: int = 14) -> Optional[float]:
    """真实波幅均值（TR = max(H-L, |H-PC|, |L-PC|)）。

    bars: [(high, low, close), ...] 升序；不够 period+1 根返回 None，
    **不拿一两根硬算** —— 那是"看起来算了其实没算"。
    """
    if not bars or len(bars) < period + 1:
        return None
    trs: List[float] = []
    prev_close = bars[0][2]
    for high, low, close in bars[1:]:
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    tail = trs[-period:]
    if not tail:
        return None
    return sum(tail) / len(tail)


def build_plan(entry: Optional[float],
               direction: str,
               atr_value: Optional[float],
               risk_pct: float = DEFAULT_RISK_PCT,
               atr_mult: float = DEFAULT_ATR_MULT) -> Dict[str, Any]:
    """把「方向 + 价格 + 波动」变成「止损 / 仓位 / 目标」。

    返回字段：
      ok            有没有算成（价格或方向缺失时 False，调用方要照实显示"算不出"）
      stop          止损价
      stop_pct      止损距离（占入场价的百分比）
      targets       [1R, 2R, 3R] 三个价位
      size_pct      仓位（占总权益 %）= risk_pct / stop_pct（超过 100% 截到 100）
      estimated     是否用了 2% 兜底（ATR 不足时）
    """
    d = (direction or "").upper()
    if d in ("LONG", "BUY"):
        d = "BUY"
    elif d in ("SHORT", "SELL"):
        d = "SELL"
    if d not in ("BUY", "SELL") or not entry or entry <= 0:
        return {"ok": False, "why": "没有方向或没有价格，算不出止损/仓位"}

    est = False
    if atr_value and atr_value > 0:
        stop_dist = atr_value * atr_mult
    else:
        stop_dist = entry * FALLBACK_STOP_PCT / 100.0
        est = True

    stop_pct = stop_dist / entry * 100.0
    if stop_pct <= 0:
        return {"ok": False, "why": "止损距离为 0（价格或波动异常）"}

    if d == "BUY":
        stop = entry - stop_dist
        targets = [entry + stop_dist * k for k in (1, 2, 3)]
    else:
        stop = entry + stop_dist
        targets = [entry - stop_dist * k for k in (1, 2, 3)]

    size_pct = min(100.0, risk_pct / stop_pct * 100.0)
    return {
        "ok": True,
        "direction": d,
        "stop": round(stop, 4),
        "stop_pct": round(stop_pct, 2),
        "targets": [round(t, 4) for t in targets],
        "size_pct": round(size_pct, 2),
        "risk_pct": risk_pct,
        "atr_mult": atr_mult,
        "estimated": est,
        "why": ("K 线不足 %d 根，止损按 %.0f%% 兜底（不是 ATR 算的）"
                % (15, FALLBACK_STOP_PCT)) if est else "",
    }


def summarize(bars: Sequence[Tuple[int, float, float, float, float]],
              tf: str = "1d") -> Dict[str, Any]:
    """从 K 线算出入场价 / ATR / 数据新鲜度。

    bars: [(ts, high, low, close), ...]（ts 原始单位，秒或毫秒都行）
    """
    clean: List[Tuple[int, float, float, float]] = []
    for ts, h, l, c in bars:
        t = _norm_ts(ts)
        if t is None:
            continue
        try:
            clean.append((t, float(h), float(l), float(c)))
        except Exception:
            continue
    clean.sort(key=lambda x: x[0])
    if not clean:
        return {"bars": 0, "price": None, "atr": None, "atr_pct": None,
                "last_ts": None, "data_age_h": None, "tf": tf}

    price = clean[-1][3]
    atr_v = atr([(h, l, c) for _, h, l, c in clean])
    last_ts = clean[-1][0]
    return {
        "bars": len(clean),
        "price": round(price, 6),
        "atr": round(atr_v, 6) if atr_v else None,
        "atr_pct": round(atr_v / price * 100.0, 2) if (atr_v and price) else None,
        "last_ts": last_ts,
        "tf": tf,
        "first_ts": clean[0][0],
    }


def build_card(decision: Dict[str, Any],
               bars: Sequence[Tuple[int, float, float, float, float]],
               tf: str = "1d",
               risk_pct: float = DEFAULT_RISK_PCT,
               atr_mult: float = DEFAULT_ATR_MULT,
               now_ts: Optional[int] = None) -> Dict[str, Any]:
    """一条决策 → 一张卡片（决策字段 + 市场结构 + 计划）。"""
    m = summarize(bars, tf=tf)
    direction = (decision.get("action") or "").upper()
    plan = build_plan(m.get("price"), direction, m.get("atr"), risk_pct, atr_mult)
    age_h = None
    if m.get("last_ts") and now_ts:
        age_h = round((now_ts - int(m["last_ts"])) / 3600.0, 1)
    stale = bool(age_h is not None and age_h > {"1d": 48, "4h": 12, "1h": 4}.get(tf, 48))
    return {
        **decision,
        "market": {**m, "data_age_h": age_h, "stale": stale,
                   "source": "kline_bars:%s" % tf if m.get("bars") else "无 K 线"},
        "plan": plan,
        "execute_ready": bool(plan.get("ok") and not stale),
        "blocking": ([] if plan.get("ok") else [plan.get("why") or "算不出计划"])
                    + (["K 线数据 %.1f 小时未更新" % age_h] if stale and age_h else []),
    }
