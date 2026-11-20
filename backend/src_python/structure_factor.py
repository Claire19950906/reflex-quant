"""structure_factor — 把 prompt 里的「盘面结构分析 v2」变成可计算的数值因子（v342）

背景（v342 判断链颗粒度）：
    分析 prompt 要求 LLM 输出 [盘面结构分析]（价位位置 / 波动分档 / 周期匹配 /
    周期共振 / 结构评分 -2~+2），但落库只有一个三值枚举
    market_confirmation = positive|negative|unknown，实测 98.6% 是 unknown。
    → 推理里算过的结构，到决策闸门时已经丢了。

本模块不依赖 LLM、不依赖第三方库，只用快照里已有的确定字段重算一遍，
输出 [-2, +2] 的结构评分（正=盘面偏多，负=盘面偏空），供 decision_guard 使用。

输入约定（market_snapshot._assemble_asset 的单个 asset 条目）：
    {"price": float, "stats_7d": {"highest","lowest","trend","atr_pct"}, "mtf": {...}}

设计原则：
    1) 取不到 ≠ 中性：缺 price / highest / lowest / atr / trend 任一项 → available=False，
       绝不拿 0 占位当「结构中性」。
    2) 只做增量：本模块是纯函数，不改任何既有字段；调用方自行决定是否采用。
    3) 可解释：每个分项都写进 detail，禁止黑箱。
"""
from typing import Any, Dict, Optional

# 位置阈值（对齐 prompt「盘面结构分析 v2 a.」）
_HIGH_POS = 0.80
_LOW_POS = 0.20
# 波动分档（对齐 b.）
_ATR_LOW = 0.5
_ATR_HIGH = 1.5

_BULL_WORDS = ("bull",)
_BEAR_WORDS = ("bear",)


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _trend_dir(trend: Any) -> int:
    """趋势标签 → 方向（+1 多 / -1 空 / 0 无方向）。"""
    text = str(trend or "").strip().lower()
    if not text:
        return 0
    if any(w in text for w in _BULL_WORDS):
        return 1
    if any(w in text for w in _BEAR_WORDS):
        return -1
    return 0


def _range_position(price: float, highest: float, lowest: float) -> Optional[float]:
    """当前价在区间内的相对位置 0..1；区间退化（振幅为 0）返回 None。"""
    span = highest - lowest
    if span <= 0 or price <= 0:
        return None
    pos = (price - lowest) / span
    return max(0.0, min(1.0, pos))


def _atr_band(atr_pct: float) -> str:
    if atr_pct < _ATR_LOW:
        return "low"      # 低波动：新闻冲击容易被放大（变盘前夜）
    if atr_pct > _ATR_HIGH:
        return "high"     # 高波动：已定价，冲击钝化
    return "mid"


def _tf_resonance(mtf: Dict[str, Any], base_dir: int) -> str:
    """1d 趋势与大周期（4h/1h）是否共振。无数据 → unknown。"""
    if not isinstance(mtf, dict) or base_dir == 0:
        return "unknown"
    dirs = []
    for tf in ("4h", "1h"):
        stats = mtf.get(tf)
        if isinstance(stats, dict):
            d = _trend_dir(stats.get("trend"))
            if d != 0:
                dirs.append(d)
    if not dirs:
        return "unknown"
    agree = sum(1 for d in dirs if d == base_dir)
    if agree == len(dirs):
        return "aligned"
    if agree == 0:
        return "conflict"
    return "mixed"


def compute(asset_context: Dict[str, Any]) -> Dict[str, Any]:
    """把单个资产的快照条目折算成结构评分。

    Returns:
        {
          "available": bool,
          "bias": float,          # -2..+2，正=偏多；不可得时 0.0
          "range_pos": float|None,
          "position_bucket": "high"|"mid"|"low"|None,
          "atr_band": "low"|"mid"|"high"|None,
          "tf_resonance": "aligned"|"conflict"|"mixed"|"unknown",
          "detail": str,
        }
    """
    if not isinstance(asset_context, dict):
        return _unavailable("快照条目缺失", reason="no_asset_context")

    # 行情是否真的取到 —— status != ok 时所有数值都是 0 占位，不能用。
    # v342-fix: partial（本地 ticks/ohlcv 兜底价）也算可用，与闸门 market_ok 口径一致。
    status = str(asset_context.get("status") or "").strip().lower()
    if status and status not in ("ok", "partial"):
        return _unavailable(f"行情状态={status}，数值为占位", reason="market_unavailable")

    price = _f(asset_context.get("price"))
    stats = asset_context.get("stats_7d")
    if not isinstance(stats, dict):
        return _unavailable("无 stats_7d", reason="no_stats")
    # v342-fix: 兼容两种区间键名。_compute_period_stats 通用键是 highest/lowest；
    # _compute_7d_stats 对外键是 highest_7d/lowest_7d。此前只读 highest/lowest，
    # 而 stats_7d 实际写的是 *_7d → 区间恒 0，structure 全部 no_range 不可得。
    highest = _f(stats.get("highest"))
    if highest is None:
        highest = _f(stats.get("highest_7d"))
    lowest = _f(stats.get("lowest"))
    if lowest is None:
        lowest = _f(stats.get("lowest_7d"))
    atr_pct = _f(stats.get("atr_pct"))
    trend = stats.get("trend")

    if price is None or price <= 0:
        return _unavailable("无现价", reason="no_price")
    if highest is None or lowest is None or highest <= lowest:
        return _unavailable("无有效 7d 区间", reason="no_range")
    base_dir = _trend_dir(trend)
    if base_dir == 0:
        return _unavailable(f"7d 趋势无方向（{trend or 'N/A'}）", reason="no_trend")
    if atr_pct is None or atr_pct <= 0:
        return _unavailable("无 ATR", reason="no_atr")

    pos = _range_position(price, highest, lowest)
    if pos is None:
        return _unavailable("区间位置不可计算", reason="no_position")

    if pos > _HIGH_POS:
        bucket = "high"
    elif pos < _LOW_POS:
        bucket = "low"
    else:
        bucket = "mid"
    band = _atr_band(atr_pct)
    resonance = _tf_resonance(asset_context.get("mtf") or {}, base_dir)

    parts = []
    # ① 趋势基准：7d 结构方向 ±1
    bias = float(base_dir)
    parts.append(f"7d趋势={trend}({base_dir:+d})")

    # ② 周期共振：同向 +0.5 / 冲突 -0.5（不改方向，只调幅度）
    if resonance == "aligned":
        bias += 0.5 if base_dir > 0 else -0.5
        parts.append(f"4h/1h 共振({0.5 if base_dir > 0 else -0.5:+.1f})")
    elif resonance == "conflict":
        bias -= 0.5 if base_dir > 0 else -0.5
        parts.append(f"4h/1h 冲突({-0.5 if base_dir > 0 else 0.5:+.1f})")
    else:
        parts.append("大周期无读数(±0)")

    # ③ 位置阻尼：越贴区间边缘越不敢追（高位追多 / 低位追空同罪）
    extreme = 2.0 * abs(pos - 0.5)          # 0=中位, 1=边缘
    pos_damp = 1.0 - 0.5 * extreme
    bias *= pos_damp
    parts.append(f"7d位置={pos:.0%}({bucket},阻尼×{pos_damp:.2f})")

    # ④ 波动分档：低波动冲击放大 ×1.2 / 高波动已定价 ×0.8
    if band == "low":
        vol_mult = 1.2
    elif band == "high":
        vol_mult = 0.8
    else:
        vol_mult = 1.0
    bias *= vol_mult
    parts.append(f"ATR={atr_pct:.2f}%({band},×{vol_mult:.1f})")

    bias = max(-2.0, min(2.0, bias))
    return {
        "available": True,
        "bias": round(bias, 4),
        "range_pos": round(pos, 4),
        "position_bucket": bucket,
        "atr_band": band,
        "tf_resonance": resonance,
        "detail": " | ".join(parts),
    }


def _unavailable(note: str, *, reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "bias": 0.0,
        "range_pos": None,
        "position_bucket": None,
        "atr_band": None,
        "tf_resonance": "unknown",
        "detail": note,
        "unavailable_reason": reason,
    }
