"""
price_structure.py — v303 R14: 价格结构形态识别
=================================================
替代 trend_pivot 的"指标组合判拐点", 后者在日线上被验证为不可预测。

设计原则
--------
1. **纯因果**: i 时刻的标记只用 [max(0, i-lookback)..i] 的数据
2. **不预报方向**: 标记即描述, 后续走势由用户自行判断
3. **冷却期**: 同形态间隔至少 N 根
4. **可读**: 返回形态名称、关键价位、置信度
"""
from __future__ import annotations

CFG = {
    "lookback_double": 20,
    "tolerance_pct": 0.03,
    "min_height_pct": 0.04,
    "body_min_pct": 0.005,
    "upper_shadow_min": 0.02,
    "lower_shadow_min": 0.02,
    "cooldown": 5,
    "min_atr_ratio": 0.5,
}


def _range(o, h, l, c):
    return max(h - l, 1e-9)


def _shadows(o, h, l, c):
    rng = _range(o, h, l, c)
    return (h - max(o, c)) / rng, (min(o, c) - l) / rng


def _atr(rows, i, n=14):
    if i < n:
        return None
    s = 0.0
    for k in range(i - n + 1, i + 1):
        h, l = rows[k]["high"], rows[k]["low"]
        prev = rows[k - 1]["close"] if k > 0 else rows[k]["open"]
        s += max(h - l, abs(h - prev), abs(l - prev))
    return s / n


def _within_atr(rows, i):
    a = _atr(rows, i)
    if a is None or a == 0:
        return False
    rng = rows[i]["high"] - rows[i]["low"]
    return rng / a >= CFG["min_atr_ratio"]


# ──────────────────── 单 K 形态 ────────────────────
def _hammer(rows, i):
    """锤子线(底部): 实体小, 下影线长, 上影线短"""
    o, h, l, c = (rows[i][k] for k in ("open", "high", "low", "close"))
    rng = _range(o, h, l, c)
    body = abs(c - o) / rng
    up, dn = _shadows(o, h, l, c)
    if body > 0.35:
        return None
    if dn < CFG["lower_shadow_min"] * 3 or up > CFG["lower_shadow_min"] * 1.2:
        return None
    return _mk(rows, i, "hammer", {
        "lower_shadow": round(dn * 100, 1),
        "upper_shadow": round(up * 100, 1),
        "body_pct": round(body * 100, 1),
    })


def _shooting_star(rows, i):
    """射击之星(顶部): 实体小, 上影线长, 下影线短"""
    o, h, l, c = (rows[i][k] for k in ("open", "high", "low", "close"))
    rng = _range(o, h, l, c)
    body = abs(c - o) / rng
    up, dn = _shadows(o, h, l, c)
    if body > 0.35:
        return None
    if up < CFG["upper_shadow_min"] * 3 or dn > CFG["upper_shadow_min"] * 1.2:
        return None
    return _mk(rows, i, "shooting_star", {
        "upper_shadow": round(up * 100, 1),
        "lower_shadow": round(dn * 100, 1),
        "body_pct": round(body * 100, 1),
    })


def _engulfing(rows, i):
    """吞没: 当前 K 线实体包住前一根, 方向相反"""
    if i < 1:
        return None
    po, pc = rows[i - 1]["open"], rows[i - 1]["close"]
    o, c = rows[i]["open"], rows[i]["close"]
    if pc < po and c > o and o <= pc and c >= po:
        return _mk(rows, i, "bullish_engulfing", {
            "prev_body_pct": round(abs(pc - po) / rows[i - 1]["close"] * 100, 2),
            "curr_body_pct": round((c - o) / rows[i]["close"] * 100, 2),
        })
    if pc > po and c < o and o >= pc and c <= po:
        return _mk(rows, i, "bearish_engulfing", {
            "prev_body_pct": round(abs(pc - po) / rows[i - 1]["close"] * 100, 2),
            "curr_body_pct": round((c - o) / rows[i]["close"] * 100, 2),
        })
    return None


def _doji(rows, i):
    """十字星: 实体 < 振幅 10%"""
    o, c = rows[i]["open"], rows[i]["close"]
    h, l = rows[i]["high"], rows[i]["low"]
    rng = h - l
    if rng == 0:
        return None
    body = abs(c - o) / rng
    if body > 0.1:
        return None
    return _mk(rows, i, "doji", {
        "body_pct": round(body * 100, 2),
        "range_pct": round(rng / c * 100, 2) if c else 0,
    })


def _double_top_bottom(rows, i):
    """双顶/双底: 当前 K 线重测前高低点而未突破, 中间有回撤"""
    if i < CFG["lookback_double"]:
        return None
    lo = i - CFG["lookback_double"]
    hi, lo_v = rows[i]["high"], rows[i]["low"]
    tops = [rows[k]["high"] for k in range(lo, i)]
    if not tops:
        return None
    peak = max(tops)
    if hi > peak * (1 + 0.005):
        return None
    if abs(hi - peak) / peak > CFG["tolerance_pct"] * 0.5:
        return None
    mid_lows = [rows[k]["low"] for k in range(lo, i)
                if rows[k]["low"] < peak * (1 - CFG["min_height_pct"])]
    if not mid_lows:
        return None
    neckline = max(mid_lows)
    if peak - neckline < peak * CFG["min_height_pct"]:
        return None
    return _mk(rows, i, "double_top", {
        "level": round(peak, 4),
        "test_high": round(hi, 4),
        "neckline": round(neckline, 4),
        "tolerance": round((hi - peak) / peak * 100, 2),
    })


def _mk(rows, i, name: str, info: dict) -> dict:
    o, h, l, c = (rows[i][k] for k in ("open", "high", "low", "close"))
    rng = h - l
    height = round(rng / c * 100, 2) if c else 0
    return {
        "ts": rows[i]["ts"],
        "name": name,
        "open": o, "high": h, "low": l, "close": c,
        "height_pct": height,
        "info": info,
        "uses_future": False,
    }


def detect(rows: list[dict]) -> list[dict]:
    """扫一遍所有形态, 返回去重+冷却后的形态序列。纯因果。"""
    out: list[dict] = []
    last_emit: dict[str, int] = {}
    for i in range(20, len(rows)):
        if not _within_atr(rows, i):
            continue
        cands = []
        for fn in (_hammer, _shooting_star, _engulfing, _doji, _double_top_bottom):
            try:
                r = fn(rows, i)
                if r:
                    cands.append(r)
            except Exception:
                pass
        for c in cands:
            name = c["name"]
            if i - last_emit.get(name, -10**9) < CFG["cooldown"]:
                continue
            out.append(c)
            last_emit[name] = i
    out.sort(key=lambda x: x["ts"])
    return out
