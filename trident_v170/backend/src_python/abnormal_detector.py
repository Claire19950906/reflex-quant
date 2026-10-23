#!/usr/bin/env python3
"""需求 4 —— 3 分钟异动检测 + 时间锚回溯（纯函数，无 I/O，便于单测）。

触发口径（用户定义，任一条满足即触发，不设双重条件）：
  1. 单根 3 分钟 K 线涨跌幅绝对值 ≥ 2.5%
  2. 成交量 > 近 20 根 3 分钟 K 线成交量均值 × 1.5

触发后：以该 3 分钟 K 线为时间锚（anchor_ts）往回溯源，交由 AI 归因；
AI 只引用公开免费数据源内容，不确定就列候选原因并标注"推测"，严禁无依据幻觉。

蜡烛数据形态与 ``quote_sources.fetch_*_candles`` 保持一致（升序）：
  [ts_ms, open, high, low, close, volume]
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import technical_indicators

# ── 触发阈值（需求 4 口径）─────────────────────────────────────────────
#   按 interval 自适应（10m 振幅 > 3m，1h 振幅 > 10m）：
#     3m:  1.5% / 1.4×
#     5m:  1.2% / 1.3×
#     10m: 1.1% / 2.0×（v277：10m 视图下"中等抬高 +90%"——pct 0.6→1.1 / vol 1.5→2.0；
#                      用户反馈 24h 内 8 异动过密，把噪声砍一半，仅保留显著大阴/大阳/量激增）
#     1h:  2.0% / 1.5×
#   旧版写死 2.5%/1.5× 导致 10m/1h 视图下基本不触发，标不到大阴/大阳。
PCT_CHANGE_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 1.5,    # 3m（v221.3: 2.5→1.5；与 L2 联动过滤，趋势中段微动靠 L2 去掉）
    300: 1.2,    # 5m（v221.3: 2.0→1.2）
    600: 1.1,    # 10m（v277: 0.6→1.1 中等抬高；BTC 10m 单根 -0.65% 仍可走 big_candle 路线捕捉）
    3600: 2.0,   # 1h（v221.3: 3.0→2.0）
    14400: 4.0,  # 4h（v221.3: 5.0→4.0）
}
VOL_RATIO_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 1.4,    # 3m（v221.3: 1.5→1.4）
    300: 1.3,    # 5m（保持）
    600: 2.0,    # 10m（v277: 1.5→2.0 中等抬高；10m 单根 vol_ratio 1.5-2 区间多为趋势放大区段噪声）
    3600: 1.5,   # 1h（保持）
    14400: 1.8,  # 4h（保持）
}
# 兼容旧接口（默认 3m 阈值）
PCT_CHANGE_THRESHOLD = PCT_CHANGE_THRESHOLD_BY_INTERVAL[180]
VOL_LOOKBACK = 20               # 成交量对比窗口（近 20 根）
VOL_RATIO_THRESHOLD = VOL_RATIO_THRESHOLD_BY_INTERVAL[180]

# v221 — 滚动摆动检测阈值（捕捉"连跌/连涨形成的大阴/大阳 + 转折点"）
#   实测 BTC 10m 单根 max=0.65%（永远到不了 2.5%），但 5 根累计能到 -1.27%
#   触发：lookback 根内最高-最低 / 最低价 >= X%（区间振幅），或累计 open-to-close swing
SWING_LOOKBACK = 5              # 滚动窗口 5 根（v221.3: 保持）
SWING_RANGE_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.6,    # 3m: 5 根区间振幅 0.6%（v221.3: 0.8→0.6；3m 颗粒小，需更敏感）
    300: 0.7,    # 5m: 0.7%（v221.3: 1.0→0.7）
    600: 1.1,    # 10m: 1.1%（v277: 0.6→1.1 中等抬高；BTC 10m 5 根区间 1.27% 仍可触发）
    3600: 2.0,   # 1h: 2%（v221.3: 3.0→2.0）
    14400: 4.0,  # 4h: 4%（v221.3: 6.0→4.0）
}
SWING_PCT_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.7,    # 3m: 5 根累计 swing 0.7%（v221.3: 1.0→0.7）
    300: 0.8,    # 5m: 0.8%（v221.3: 1.2→0.8）
    600: 1.3,    # 10m: 1.3%（v277: 0.7→1.3 中等抬高；BTC 10m 5 根最大 -1.27% 仍可触发）
    3600: 2.5,   # 1h: 2.5%（v221.3: 4.0→2.5）
    14400: 5.5,  # 4h: 5.5%（v221.3: 8.0→5.5）
}
SWING_PCT_THRESHOLD = SWING_PCT_THRESHOLD_BY_INTERVAL[180]
SWING_RANGE_THRESHOLD = SWING_RANGE_THRESHOLD_BY_INTERVAL[180]

# v221.3 — 4 根累计涨跌幅阈值（短周期转折点专用，介于单根 pct 与 5 根 swing 之间）
#   用户："必备出转折点" → 单根阈值常漏"3-4 根连动后突然反方向"，5 根 swing 又太迟
#   4 根累计捕捉"短促的连跌/连涨后反转"，是 v221 swing 检测的快速版
CUMULATIVE4_LOOKBACK = 4
CUMULATIVE4_PCT_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.6,    # 3m: 4 根累计 0.6%
    300: 0.6,    # 5m: 4 根累计 0.6%
    600: 0.95,   # 10m: 4 根累计 0.95%（v277: 0.5→0.95 中等抬高；BTC 10m 4 根最大 -1.05% 仍可触发）
    3600: 1.5,   # 1h: 4 根累计 1.5%
    14400: 3.0,  # 4h: 4 根累计 3.0%
}

# v221 — L2 趋势同向过滤弱化：只过滤小波动（< 1.5%），大波动（>= 1.5%）一律保留
#   用户："必备出涨跌幅度大的转折点" → 大阴/大阳不能被"动量延续"吃掉
L2_FILTER_PCT_CEILING = 1.5     # 单根 pct < 1.5% 仍走 L2；>= 1.5% 一律保留
# v221.3 — L1 聚簇窗口收紧：30min → 20min（避免相邻 swing 与 pct 标记堆叠成片）
L1_CLUSTER_WINDOW_MS_DEFAULT = 20 * 60 * 1000  # 20 min

# v221.4 — 单根大 K 线阈值（high-low 区间振幅 / |body| 任一超即触发）。
#   旧 0.6% pct 阈值是「单根 body 涨跌幅」，对 10m BTC 太严（实测 max=-0.65%）。
#   用户的「必备出大阴/大阳」实际想要的是「这根 K 线本身振幅很大」——影线长也算。
#   阈值按 interval 自适应（10m 1.2% 区间 / 0.8% body，覆盖 BTC 24h 10m 99% 分位）。
BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.5,    # 3m: 0.5% 区间（与 SWING_RANGE 3m=0.6% 略低——单根比窗口更敏感）
    300: 0.6,    # 5m: 0.6%
    600: 0.7,    # 10m: 0.7% 区间（与 SWING_RANGE 10m=0.6% 同口径，覆盖 BTC 24h 单根 ≥95 分位）
    3600: 1.8,   # 1h: 1.8% / 1.2% body
    14400: 4.0,  # 4h: 4% / 2.5% body
}
BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.35,
    300: 0.45,
    600: 0.5,    # 10m: |body| ≥ 0.5%（BTC 10m 单根 body max 实测 -0.65%，覆盖 ≥75 分位）
    3600: 1.2,
    14400: 2.5,
}

# v221.6 — 事件箭头（event_arrow）专用阈值：单根 |body| + 量比同根双触发。
#   用户 2026-10-05 反馈：截图里 4 个红箭头（02:30 NFP / 21:00 / 22:00 / 21:30 大阴+巨量）
#   全部是「单根 body 显著 + 当根量比 ≥ 2×」的复合信号，单走 big_candle 或 vol_break/vol_z
#   都会漏一种。event_arrow 把这两种维度合并成一个独立箭头，importance=90 最高，
#   确保 NFP/CPI/ETF 这类事件下的「巨量大阴/大阳」必标。
EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.8,    # 3m: |body| ≥ 0.8% + 量比 ≥ 2.0×（3m 颗粒小，body 阈值稍高防噪声）
    300: 0.7,    # 5m
    600: 0.5,    # 10m: |body| ≥ 0.5% + 量比 ≥ 2.0×
    3600: 1.5,   # 1h
    14400: 3.0,  # 4h
}
EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 2.0,    # 3m
    300: 2.0,
    600: 2.0,    # 10m: BTC 10m 量比 ≥ 2× 才算「巨量」（普通量比 1.5× 太多）
    3600: 1.8,
    14400: 1.5,
}

# v221.4 — 局部转折点阈值。
#   「涨跌幅度大的转折点」= 局部极值（6 根窗口最高/最低）+ 短促反向（3 根内反向 ≥ 阈值%）。
#   区别于 swing（多根累计同向）：reversal 必须是「方向反过来了」。
REVERSAL_LOOKBACK = 6           # 找极值用的窗口
REVERSAL_DECISION_WINDOW = 3    # 极值之后 N 根内出现反向
REVERSAL_PCT_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 0.4,    # 3m: 4 根内反向 ≥ 0.4%
    300: 0.5,    # 5m
    600: 0.6,    # 10m: 3 根内反向 ≥ 0.6%
    3600: 1.5,   # 1h
    14400: 3.0,  # 4h
}

# v221.4 — 量能 Z-score 阈值（独立信号：纯量能激增，不要求价格突破）。
#   旧 vol_break 要求 z ≥ 2 且价格突破 → 单纯量能尖峰被漏。
#   解耦后输出为 vol_z 标记，z ≥ 阈值即触发。
VOL_Z_THRESHOLD_BY_INTERVAL: Dict[int, float] = {
    180: 2.0,
    300: 2.0,
    600: 2.0,    # 10m: BTC 10m 量 z 通常 < 2，≥ 2 即异常
    3600: 1.8,
    14400: 1.5,
}

# v221.4 — swing 方向门槛改为自适应：不超过 swing_pct_th 的 0.8 倍，避免「中等 swing 被自相矛盾的方向门槛挡住」。
#   旧硬编码 0.8% 在 10m (swing_pct_th=0.7%) 下 > 阈值，导致 0.7~0.8% 的中等 swing 被方向门槛拒掉。
DIRECTION_FLOOR_RATIO = 0.8     # 方向门槛 = swing_pct_th × 0.8（10m = 0.56%，3m = 0.56%，1h = 2.0%）


# ════════════════════════════════════════════════════════════════════════════
# v221.5 — 噪声治理三件套：importance 评分 + 聚簇 + 同 ts 去重
# ════════════════════════════════════════════════════════════════════════════
#
# 用户 2026-10-05 反馈：「噪声太过了」+ 附图2（理想效果）。
# 图2 BTC 24h 截图约 8 个有意义事件标注（CPI/FOMC/ETF/川普/价格急涨急跌），
# 密度 ~3%。当前 v221.4 模拟 10m BTC 24h 实测 70 markers / 106 bars = 66% 密度。
#
# v221.5 设计原则：
#   A. 保留每路信号维度（不删检测器），但合并重复 + 限频。
#   B. 重要性评分（0-100）：高 = 必标，低 = 静默（仍落库供回溯，但不在图表渲染）。
#   C. 聚簇：同方向同类型在窗口内 → 1 个 cluster marker（带 count + 区间总幅度）。
#   D. 同 ts 去重：按信号语义优先级去重（vol_break ⊃ vol_z；swing ⊃ cum4）。
#   E. 反馈指标：4 个统计量（正向准确率/反向召回/异动漏检数/放行拦截率）。

_ABNORMAL_IMPORTANCE: Dict[str, int] = {
    "both": 90, "volume": 70, "swing": 70, "reversal": 75,
    "big_candle": 65, "pct": 55, "cum4": 50,  # v221.5: 40→50，让 cum4 通过门槛
    "event_arrow": 90,  # v221.6: 事件箭头（巨量大阴/大阳），与 both 同档最高分
}
_ANOMALY_IMPORTANCE: Dict[str, int] = {
    "vol_break": 80,
    # v221.6: 35→55 —— 纯量能 z≥2 是肉眼能看出的箭头（旧版被门滤掉，用户反馈漏报）
    "vol_z": 55,
    "big_candle": 65, "reversal": 75,
    "rsi_blunting": 50, "macd_cross": 50, "macd_zero_cross": 45,
    # v221.6: 35→50 —— 2 根包覆形态是经典箭头（旧版被门滤掉）
    "engulfing": 50,
    "event_arrow": 90,  # v221.6: 事件箭头（巨量大阴/大阳），最高分
    # v221.10 — 4 类行情独立 marker（多因子加权得分触发，importance 最高 95）
    #   用户 2026-10-05 截图 4 个红箭头对应：交易量激增/大阳线/大阴线/转折点
    #   与单 indicator 是不同语义维度，不参与 cofire_dedup
    "volume_spike": 95,
    "big_bullish": 95,
    "big_bearish": 95,
    "reversal_p": 95,  # pattern 版（与上面 reversal=75 区分）
}

# v221.5 — importance gate（重要性门槛）。低于门槛的 marker 不渲染但仍保留在落库中。
# v221.6: 45→40 —— 两边门槛对齐，确保任何一路 indicator 达到 40 分即渲染
_ABNORMAL_IMPORTANCE_MIN = 40   # v221.6: 45→40，与 anomaly 侧对齐
_ANOMALY_IMPORTANCE_MIN = 40    # 保留 vol_break/macd_cross/rsi_blunting/big_candle/reversal/vol_z/engulfing
# v221.10: 4 类行情 marker 不参与重要性过滤（自带 95 分）
_PATTERN_INDICATORS = frozenset({"volume_spike", "big_bullish", "big_bearish", "reversal_p"})

# v221.5 — 同 ts 去重规则（语义优先级）
# v221.6：event_arrow 是「巨量大阴/大阳」复合信号，重要性最高
#   同 ts 内 event_arrow + vol_break/vol_z/big_candle 任意 → 只保 event_arrow
#   event_arrow ⊃ vol_break + vol_z + big_candle（同时满足多维）
# v221.10: 4 类行情 marker 与父级 indicator 共存（不互吃）—— 是独立维度
_COFIRE_PARENT_PRECEDENCE: List[tuple] = [
    ("event_arrow", "vol_break"),
    ("event_arrow", "vol_z"),
    ("event_arrow", "big_candle"),
    ("vol_break", "vol_z"),
    ("reversal", "engulfing"),
]


# ════════════════════════════════════════════════════════════════════════════
# v221.10 — 4 类行情多因子加权识别（用户 2026-10-05 截图反馈）
# ════════════════════════════════════════════════════════════════════════════
# 用户截图 4 个红箭头分别对应：交易量激增 / 大阳线 / 大阴线 / 大幅涨跌转折点
# 实测 10m BTC 100 bar：最大 body=0.45%（远低于 big_candle 阈值 0.5%）
#   → 旧单指标全部漏掉。解法：多因子加权（量能可补偿价格幅度）
#
# 关键设计：
#   A. 多因子加权得分 —— body_pct × 100 + range_pct × 50
#      量能 ≥ 1.5× 加成 ×1.5、≥ 2.0× 额外 ×1.3（封顶 ×3.0）
#      score ≥ 50 触发对应 marker（独立于单指标 indicator）
#   B. reversal 实时化 —— 不再等 3 bar 决策窗口，当前 bar 突破反向即可
#      前 6 根极值 + 当前 close 突破反向 + 振幅 ≥ 0.4% → 即时标
#   C. 4 类 marker 独立输出 —— 不进 cofire_dedup（与单 indicator 不同维度）
#      importance = 95（最高，必渲染）
#   D. 量能激增解耦 —— vol_z 不被 vol_break/event_arrow 覆盖
#      vol_z ≥ 2.0 + vol_ratio ≥ 1.5× 任一即可触发 volume_spike

# v221.10 — 多因子得分阈值（每类行情 score ≥ 该值即触发）
#   设计依据：实测 BTC 10m max body=0.45% → score=45（无 vol 加成）刚好触发
#            max range=0.62% → score=31 需配合 body 或 vol
#   v277：volume_spike 阈值 50→35——用户反馈「交易量大的怎么没辨别出来」，
#         旧阈值要求 z=2.0 单独触发，z=1.4~1.9 区间的"明显放量但价格不动"被漏。
#         新阈值下 z=1.4 或 vol_ratio≥1.3× 即可触发。
_PATTERN_SCORE_THRESHOLD: Dict[str, int] = {
    "volume_spike": 35,   # v277: 50→35；让"高量未突破极值"的纯量能尖峰也标
    "big_bullish": 50,    # |body| × 100 + range × 50（量能加成）
    "big_bearish": 50,
    "reversal_p": 30,     # 极值反向幅度 × 30
}

# v277 — volume_spike 量比加成梯度（量比 ×1.3 就开始计分，覆盖"价微动 + 量激增"场景）
#   旧 1.5×/3.0× 两档：1.5× 之前的量比被忽略，z=1.5/vol_ratio=1.2 这种"明显放量"漏报
#   新 1.3×/2.0×/3.5× 三档：1.3× +25 / 2.0× +30 / 3.5× +35
_PATTERN_VOL_RATIO_GRADIENT: List[Tuple[float, int]] = [
    (1.3, 25),
    (2.0, 30),
    (3.5, 35),
]

# v221.10 — reversal 实时化窗口
#   旧 REVERSAL_LOOKBACK=6 + REVERSAL_DECISION_WINDOW=3 → 等 3 根才确认
#   v221.10: 6 根极值窗口 + 当前 bar 立即确认（0 bar 延迟）
_PATTERN_REVERSAL_LOOKBACK = 6

# v221.5 — 聚簇窗口（30min：10m 周期 3 根 / 3m 周期 10 根）
CLUSTER_WINDOW_MS_V221_5 = 30 * 60 * 1000


def _marker_importance_abnormal(trigger_type: str) -> int:
    return _ABNORMAL_IMPORTANCE.get(trigger_type, 0)


def _marker_importance_anomaly(indicator: str) -> int:
    return _ANOMALY_IMPORTANCE.get(indicator, 0)


def _cofire_dedup_anomaly(
    markers: List[Dict[str, Any]],
    *,
    precedence: Optional[List[tuple]] = None,
) -> List[Dict[str, Any]]:
    """同 ts 多 indicator 触发 → 按 precedence 保留优先级最高的，其余丢。

    Args:
      markers: scan_anomalies 输出（按任意顺序）
      precedence: [(parent, child), ...] 优先级对
    """
    if not markers:
        return markers
    prec = precedence or _COFIRE_PARENT_PRECEDENCE
    by_ts: Dict[int, List[Dict[str, Any]]] = {}
    order: List[int] = []  # preserve ts order
    rest: List[Dict[str, Any]] = []  # ts=None 透传保留
    for m in markers:
        ts = m.get("ts")
        if ts is None:
            rest.append(m)
            continue
        if ts not in by_ts:
            by_ts[ts] = []
            order.append(ts)
        by_ts[ts].append(m)
    out: List[Dict[str, Any]] = []
    for ts in order:
        group = by_ts[ts]
        if len(group) == 1:
            out.append(group[0])
            continue
        indicators = {m.get("indicator") for m in group}
        drop = set()
        for parent, child in prec:
            if parent in indicators and child in indicators:
                drop.add(child)
        for m in group:
            if m.get("indicator") not in drop:
                out.append(m)
    return out + rest


def _flush_cluster(
    buf: List[Dict[str, Any]], ts_key: str, type_key: str, pct_key: str,
) -> List[Dict[str, Any]]:
    """缓冲区 → 1 个 cluster marker（count=1 时原样返回）。"""
    if len(buf) == 1:
        return list(buf)
    start = min(int(m[ts_key]) for m in buf if m.get(ts_key) is not None)
    end = max(int(m[ts_key]) for m in buf if m.get(ts_key) is not None)
    total_pct = sum(abs(m.get(pct_key) or 0) for m in buf)
    types = sorted({m.get(type_key) for m in buf if m.get(type_key)})
    head = dict(buf[0])
    head[ts_key] = start
    head["cluster_end_ts"] = end
    head["cluster_count"] = len(buf)
    head["total_pct"] = round(total_pct, 4)
    head["clustered"] = True
    head["cluster_types"] = types
    head["cluster_window_ms"] = end - start
    return [head]


def _cluster_turn_markers(
    markers: List[Dict[str, Any]],
    *,
    window_ms: int = CLUSTER_WINDOW_MS_V221_5,
    ts_key: str = "ts",
    type_key: str = "trigger_type",
    pct_key: str = "pct_change",
) -> List[Dict[str, Any]]:
    """同方向同类型在窗口内 → 1 个 cluster marker（带 count + 区间总幅度）。"""
    if not markers:
        return markers
    # 先按 ts 排序
    sorted_m = sorted(
        [m for m in markers if m.get(ts_key) is not None],
        key=lambda x: x[ts_key],
    )
    rest = [m for m in markers if m.get(ts_key) is None]
    out: List[Dict[str, Any]] = []
    buf: List[Dict[str, Any]] = []
    for m in sorted_m:
        ttype = m.get(type_key)
        if not buf:
            buf.append(m)
            continue
        head = buf[0]
        same_type = head.get(type_key) == ttype
        same_dir = (head.get(pct_key) or 0) * (m.get(pct_key) or 0) > 0
        within_window = abs(int(m[ts_key]) - int(head[ts_key])) <= window_ms
        if same_type and same_dir and within_window:
            buf.append(m)
        else:
            out.extend(_flush_cluster(buf, ts_key, type_key, pct_key))
            buf = [m]
    if buf:
        out.extend(_flush_cluster(buf, ts_key, type_key, pct_key))
    return out + rest


def _importance_filter_abnormal(
    markers: List[Dict[str, Any]], *, min_score: int = _ABNORMAL_IMPORTANCE_MIN,
) -> List[Dict[str, Any]]:
    return [m for m in markers if _marker_importance_abnormal(m.get("trigger_type", "")) >= min_score]


def _importance_filter_anomaly(
    markers: List[Dict[str, Any]], *, min_score: int = _ANOMALY_IMPORTANCE_MIN,
) -> List[Dict[str, Any]]:
    return [m for m in markers if _marker_importance_anomaly(m.get("indicator", "")) >= min_score]


# 蜡烛列下标
TS, OPEN, HIGH, LOW, CLOSE, VOL = range(6)


def _interval_thresholds(interval_sec: int) -> tuple:
    """按 interval_sec 取 (pct_threshold, vol_ratio_threshold, swing_pct, swing_range,
    cumulative4_pct)。

    未匹配 interval 时回退到 3m 阈值；用作 scan_abnormals 的默认参数。
    v221.3：返回 5 元组，新增 cum4 阈值供短周期转折点检测。
    """
    pct = PCT_CHANGE_THRESHOLD_BY_INTERVAL.get(int(interval_sec), PCT_CHANGE_THRESHOLD)
    vr = VOL_RATIO_THRESHOLD_BY_INTERVAL.get(int(interval_sec), VOL_RATIO_THRESHOLD)
    sp = SWING_PCT_THRESHOLD_BY_INTERVAL.get(int(interval_sec), SWING_PCT_THRESHOLD)
    sr = SWING_RANGE_THRESHOLD_BY_INTERVAL.get(int(interval_sec), SWING_RANGE_THRESHOLD)
    c4 = CUMULATIVE4_PCT_THRESHOLD_BY_INTERVAL.get(int(interval_sec), 0.5)
    return pct, vr, sp, sr, c4


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _candle_parts(c: Any) -> Optional[List[Optional[float]]]:
    """解析单根蜡烛为 [ts, open, high, low, close, volume]，缺列补 None。"""
    if not isinstance(c, (list, tuple)):
        return None
    return [_f(c[i]) if i < len(c) else None for i in range(6)]


def _pct_change(open_: Optional[float], close: Optional[float]) -> Optional[float]:
    """单根涨跌幅 %：以 open 为基准。"""
    if open_ is None or close is None or open_ == 0:
        return None
    return (close - open_) / open_ * 100.0


def _vol_ratio(candles: List[List[Optional[float]]], idx: int) -> Optional[float]:
    """第 idx 根成交量 / 其前 VOL_LOOKBACK 根（不足则用全部前序）均值。"""
    if idx < 2:
        return None
    start = max(0, idx - VOL_LOOKBACK)
    prev = candles[start:idx]
    vols = [c[VOL] for c in prev if c[VOL] is not None and c[VOL] > 0]
    if not vols:
        return None
    mean = sum(vols) / len(vols)
    cur = candles[idx][VOL]
    if mean <= 0 or cur is None:
        return None
    return cur / mean


def _classify(pct: Optional[float], ratio: Optional[float],
              interval_sec: int = 180) -> Dict[str, Any]:
    """任一条满足即触发（不双重）。返回命中详情。

    v221：阈值按 ``interval_sec`` 自适应（10m 1.5%/1.2×，1h 3%/1.5×，3m 2.5%/1.5×）。
    """
    pct_th, vol_th, _, _, _ = _interval_thresholds(interval_sec)
    pct_hit = pct is not None and abs(pct) >= pct_th
    vol_hit = ratio is not None and ratio > vol_th
    if pct_hit and vol_hit:
        ttype = "both"
    elif pct_hit:
        ttype = "pct"
    elif vol_hit:
        ttype = "volume"
    else:
        ttype = ""
    return {"triggered": bool(ttype), "pct_hit": pct_hit, "vol_hit": vol_hit, "trigger_type": ttype}


def _describe(ts: int, pct: Optional[float], ratio: Optional[float], ttype: str) -> str:
    parts: List[str] = []
    if pct is not None and ttype in ("pct", "both", "swing", "cum4"):
        parts.append(f"涨跌 {pct:+.2f}%")
    if ratio is not None and ttype in ("volume", "both"):
        parts.append(f"量比 {ratio:.2f}×")
    if ttype == "swing":
        parts.append("连摆")
    if ttype == "cum4":
        parts.append("4根连动")
    if not ttype:
        return "未触发异动"
    return " · ".join(parts)


def detect_abnormal(candles: List[Any], interval_sec: int = 180) -> Dict[str, Any]:
    """检测最新一根 K 是否异动。

    candles 升序、至少 2 根。返回触发详情（triggered / anchor_ts / pct_change /
    vol_ratio / trigger_type / detail）。

    v221：``interval_sec`` 自适应阈值（默认 3m，向后兼容）。
    """
    parsed = [p for p in (_candle_parts(c) for c in (candles or [])) if p is not None]
    if len(parsed) < 2:
        return {
            "triggered": False, "anchor_ts": None, "pct_change": None,
            "vol_ratio": None, "trigger_type": "", "detail": "K 线样本不足",
        }
    last = parsed[-1]
    idx = len(parsed) - 1
    pct = _pct_change(last[OPEN], last[CLOSE])
    ratio = _vol_ratio(parsed, idx)
    cls = _classify(pct, ratio, interval_sec)
    anchor_ts = int(last[TS]) if last[TS] is not None else 0
    return {
        "triggered": cls["triggered"],
        "anchor_ts": anchor_ts if cls["triggered"] else None,
        "pct_change": round(pct, 4) if pct is not None else None,
        "vol_ratio": round(ratio, 4) if ratio is not None else None,
        "trigger_type": cls["trigger_type"],
        "detail": _describe(anchor_ts, pct, ratio, cls["trigger_type"]),
    }


def _swing_score(candles: List[List[Optional[float]]], idx: int, lookback: int,
                  swing_pct_th: float, swing_range_th: float) -> Optional[Dict[str, Any]]:
    """v221：滚动摆动检测 —— 以 ``idx`` 收尾向前 lookback 根。

    返回 dict 或 None：
      pct:   子窗口 open[0] → close[idx] 累计涨跌幅 %
      range_pct: 子窗口最高-最低 / 子窗口最低价（区间振幅 %）
      direction: 'bullish' / 'bearish'
    用于捕捉"单根不到阈值、但 N 根累计成大阴/大阳"的转折点。

    v221.1 收紧：必须含「真实方向」(|pct|>=0.3%)，否则仅靠振幅不触发。
      防止「无方向但有振幅」的平淡段被误判成摆动（噪声）。
    """
    start = max(0, idx - lookback)
    sub = candles[start:idx + 1]
    if len(sub) < 2:
        return None
    o0 = sub[0][OPEN]
    cl = sub[-1][CLOSE]
    hi = max((b[HIGH] for b in sub if b[HIGH] is not None), default=None)
    lo = min((b[LOW] for b in sub if b[LOW] is not None), default=None)
    if o0 is None or cl is None or o0 <= 0:
        return None
    pct = (cl - o0) / o0 * 100.0
    rng_pct = None
    if hi is not None and lo is not None and lo > 0:
        rng_pct = (hi - lo) / lo * 100.0
    # v221.4 — 真实方向门槛：swing_pct_th × DIRECTION_FLOOR_RATIO
    #   防止「无方向但有振幅」的噪声段误触发；10m=0.7%×0.8=0.56%，1h=2.5%×0.8=2.0%
    #   旧硬编码 0.8% 在 10m swing_pct_th=0.7% 下 > 阈值 → 0.7~0.8% swing 被自相矛盾挡掉。
    if abs(pct) < swing_pct_th * DIRECTION_FLOOR_RATIO:
        return None
    triggered = (abs(pct) >= swing_pct_th) or (rng_pct is not None and rng_pct >= swing_range_th)
    if not triggered:
        return None
    direction = "bullish" if pct > 0 else ("bearish" if pct < 0 else "neutral")
    return {
        "pct": round(pct, 4),
        "range_pct": round(rng_pct, 4) if rng_pct is not None else None,
        "direction": direction,
    }


def _big_candle_score(candle: List[Optional[float]], range_th: float,
                      body_th: float) -> Optional[Dict[str, Any]]:
    """v221.4 — 单根大 K 线检测（用户：「必备出大阴线/大阳线」）。

    设计要点：「大阴/大阳」必须 |body| 显著（实体涨跌），range 仅作支撑信号。
    这样能避免「长影线十字星 / 长影线 doji」（高 range + 低 body）的噪声干扰——
    那只是高波动率，不代表价格发生显著变动。

    触发条件（任一）：
      - "body_strong": |body| ≥ body_th（实体涨跌幅够大，最常见）
      - "wick_strong": range ≥ range_th **且** |body| ≥ body_th × 0.6
        （长影线 + 中等实体，捕捉"暴跌/暴涨后反弹"的剧烈震荡 K 线）

    返回 {pct, range_pct, body_pct, direction, kind} 或 None。
    kind: "body_strong" / "wick_strong"。
    """
    o, h, lo, c = candle[OPEN], candle[HIGH], candle[LOW], candle[CLOSE]
    if None in (o, h, lo, c) or o is None or o <= 0 or h is None or lo is None or c is None:
        return None
    rng_pct = (h - lo) / o * 100.0
    body_pct = abs(c - o) / o * 100.0
    body_hit = body_pct >= body_th
    # 长影线条件：range 触阈 + body 至少 60% 阈值（不能是纯 doji）
    wick_hit = rng_pct >= range_th and body_pct >= body_th * 0.6
    if not (body_hit or wick_hit):
        return None
    # body 强 → "body_strong"（主信号）；只有 wick 强 → "wick_strong"
    kind = "body_strong" if body_hit else "wick_strong"
    direction = "bullish" if c > o else ("bearish" if c < o else "neutral")
    return {
        "pct": round((c - o) / o * 100.0, 4),
        "range_pct": round(rng_pct, 4),
        "body_pct": round(body_pct, 4),
        "direction": direction,
        "kind": kind,
    }


def _event_arrow_score(candles: List[List[Optional[float]]], idx: int,
                        body_th: float, vol_ratio_th: float) -> Optional[Dict[str, Any]]:
    """v221.6 — 事件箭头（用户 2026-10-05：截图里 4 个红箭头都是「巨量大阴/大阳」）。

    设计要点：「事件」= 单根 |body| 显著 **且** 当根量比 ≥ 阈值（同时满足）。
    与 big_candle（仅 body 触发）、vol_break（body + 价格突破 + 量 z）形成互补：
      - big_candle：单纯 body 大（无巨量）→ 噪声少但容易漏事件
      - vol_break：需要量 z≥2 且价格突破近 20 根极值 → 在下跌中段无新极值突破会漏
      - event_arrow：只要求 body + 量比（不要求极值突破），确保「事件当下」必标

    触发条件（同时满足）：
      1. 当前 bar |body| ≥ body_th%
      2. 当前 bar vol_ratio（前 VOL_LOOKBACK 根均值 × 阈值） ≥ vol_ratio_th

    返回 {pct, body_pct, vol_ratio, direction} 或 None。
    """
    if idx < 2:
        return None
    cur = candles[idx]
    o, cl = cur[OPEN], cur[CLOSE]
    if o is None or cl is None or o <= 0:
        return None
    body_pct = abs(cl - o) / o * 100.0
    if body_pct < body_th:
        return None
    # 计算 vol_ratio（复用同文件 _vol_ratio 的口径：近 VOL_LOOKBACK 根均值）
    start = max(0, idx - VOL_LOOKBACK)
    prev = candles[start:idx]
    vols = [c[VOL] for c in prev if c[VOL] is not None and c[VOL] > 0]
    if not vols:
        return None
    mean_vol = sum(vols) / len(vols)
    cur_vol = cur[VOL]
    if mean_vol <= 0 or cur_vol is None or cur_vol <= 0:
        return None
    vol_ratio = cur_vol / mean_vol
    if vol_ratio < vol_ratio_th:
        return None
    direction = "bullish" if cl > o else ("bearish" if cl < o else "neutral")
    return {
        "pct": round((cl - o) / o * 100.0, 4),
        "body_pct": round(body_pct, 4),
        "vol_ratio": round(vol_ratio, 4),
        "direction": direction,
    }


def _reversal_score(candles: List[List[Optional[float]]], idx: int,
                    lookback: int, decision_window: int,
                    pct_th: float) -> Optional[Dict[str, Any]]:
    """v221.4 — 单根反向大 K 线（用户：「必备出涨跌幅度大的转折点」）。

    设计要点：「反转」= 单根 |body| 显著 **且** 方向与近 lookback 根趋势相反。
    避免在单边趋势中每个 bar 都误判为「反转」（旧逻辑用 pivot_high/low 极值对比
    在慢跌/慢涨中每一根都满足条件 → 噪声爆炸）。

    触发条件（同时满足）：
      1. 当前 bar |body| ≥ pct_th%（单根实体够大）
      2. 近 lookback 根整体方向与当前 bar 方向相反：
         - 当前 bar 阳（close>open）→ 近 lookback 根 close[0] > close[-lookback]（下跌趋势）
         - 当前 bar 阴（close<open）→ 近 lookback 根 close[0] < close[-lookback]（上涨趋势）
      3. 「反向幅度」≥ pct_th（近 lookback 根极值 vs 当前 close 差值）

    返回 {pct, pivot} 或 None。
    """
    if idx < lookback + 1:
        return None
    cur = candles[idx]
    o, cl = cur[OPEN], cur[CLOSE]
    if o is None or cl is None or o <= 0:
        return None
    body_pct = abs(cl - o) / o * 100.0
    cur_is_bull = cl > o
    # 条件 1：单根 |body| 显著
    if body_pct < pct_th:
        return None
    # 条件 2：近 lookback 根 trend 与当前 bar 方向相反
    prev_start = idx - lookback
    prev_close = candles[prev_start][CLOSE]
    prev_before_close = candles[idx - 1][CLOSE] if idx >= 1 else None
    if prev_close is None or prev_close <= 0 or prev_before_close is None or prev_before_close <= 0:
        return None
    # prior_trend = 区间内（不含 idx）的累计涨跌幅
    prior_trend = (prev_before_close - prev_close) / prev_close * 100.0
    # 当前 bar 阳但 prior_trend < 0（前面在跌）→ 反向 bullish ✓
    # 当前 bar 阴但 prior_trend > 0（前面在涨）→ 反向 bearish ✓
    is_reversal = (cur_is_bull and prior_trend < -pct_th) or (not cur_is_bull and prior_trend > pct_th)
    if not is_reversal:
        return None
    direction = "bullish" if cur_is_bull else "bearish"
    pivot = "low" if cur_is_bull else "high"
    # 条件 3：反向幅度（用 body_pct 已 ≥ pct_th 自动满足）
    return {
        "pct": round(body_pct, 4),
        "pivot": pivot,
        "direction": direction,
        "prior_trend": round(prior_trend, 4),
    }


def _pattern_score(
    candles: List[List[Optional[float]]],
    idx: int,
    interval_sec: int,
) -> Dict[str, Any]:
    """v221.10 — 单 bar 4 类行情多因子加权得分。

    4 类得分公式：
      - volume_spike: vol_z × 25 + 量比梯度（_PATTERN_VOL_RATIO_GRADIENT：1.3×/2.0×/3.5× 三档）
        v277 把量比加成起点从 1.5× 降到 1.3×，单独 vol_ratio=1.3× 也可加 25 分
      - big_bullish/bearish: |body| × 100 + range × 50
                            × (vol_ratio ≥ 1.5)×1.5 / × (vol_ratio ≥ 2.0)×2.0
      - reversal_p: 前 6 根极值 → 当前 bar 反向幅度% × 30（实时化，0 bar 延迟）
    """
    out = {
        "volume_spike": 0, "big_bullish": 0,
        "big_bearish": 0, "reversal_p": 0,
        "extras": {},
    }
    if idx < 2 or idx >= len(candles):
        return out
    cur = candles[idx]
    o, h, lo, c, v = cur[OPEN], cur[HIGH], cur[LOW], cur[CLOSE], cur[VOL]
    if None in (o, h, lo, c, v) or o is None or o <= 0:
        return out
    body_pct = (c - o) / o * 100.0
    range_pct = (h - lo) / o * 100.0
    direction = "bullish" if c > o else ("bearish" if c < o else "neutral")

    vol_z = technical_indicators.vol_zscore(candles[: idx + 1])
    vol_ratio = _vol_ratio(candles, idx)

    # ── 1. volume_spike ──
    #   v277：改用梯度表（_PATTERN_VOL_RATIO_GRADIENT），1.3× 就开始计分
    s = 0
    if vol_z is not None and vol_z > 0:
        s += int(vol_z * 25)
    if vol_ratio is not None:
        for ratio_th, pts in _PATTERN_VOL_RATIO_GRADIENT:
            if vol_ratio >= ratio_th:
                s += pts
    out["volume_spike"] = min(s, 100)

    # ── 2. big_bullish / big_bearish ──
    s0 = abs(body_pct) * 100 + range_pct * 50
    s = s0
    if vol_ratio is not None and vol_ratio >= 2.0:
        s = s0 * 2.0
    elif vol_ratio is not None and vol_ratio >= 1.5:
        s = s0 * 1.5
    if direction == "bullish":
        out["big_bullish"] = int(min(s, 100))
    elif direction == "bearish":
        out["big_bearish"] = int(min(s, 100))

    # ── 3. reversal_p（实时化：当前 bar 突破反向即可）──
    #   v221.10 修正：旧版用「突破 prev_H/L 极值」误判趋势延续为反转
    #   正确语义 = 前 N 根趋势方向 + 当前 bar 反向
    #     · 前 N 根累计下跌 + 当前 bar 阳 → 底部反转
    #     · 前 N 根累计上涨 + 当前 bar 阴 → 顶部反转
    #   实时性：当前 bar close 时即可判定（0 bar 延迟，无 3 bar 决策窗口）
    if idx >= _PATTERN_REVERSAL_LOOKBACK:
        window = candles[idx - _PATTERN_REVERSAL_LOOKBACK: idx]
        first_close = next((b[CLOSE] for b in window if b[CLOSE] is not None), None)
        last_close = next((b[CLOSE] for b in reversed(window) if b[CLOSE] is not None), None)
        if first_close is not None and last_close is not None and first_close > 0:
            prev_trend_pct = (last_close - first_close) / first_close * 100.0
            cur_body_pct = (c - o) / o * 100.0
            s = 0
            # 底部反转：前跌 + 当前涨
            if prev_trend_pct < -0.3 and cur_body_pct > 0:
                s = int(abs(prev_trend_pct) * 30 + cur_body_pct * 30)
                out["extras"]["reversal_kind"] = "bottom_up"
                out["extras"]["prev_trend_pct"] = round(prev_trend_pct, 3)
            # 顶部反转：前涨 + 当前跌
            elif prev_trend_pct > 0.3 and cur_body_pct < 0:
                s = int(prev_trend_pct * 30 + abs(cur_body_pct) * 30)
                out["extras"]["reversal_kind"] = "top_down"
                out["extras"]["prev_trend_pct"] = round(prev_trend_pct, 3)
            out["reversal_p"] = min(s, 100)

    out["extras"]["body_pct"] = round(body_pct, 3)
    out["extras"]["range_pct"] = round(range_pct, 3)
    out["extras"]["vol_ratio"] = round(vol_ratio, 2) if vol_ratio else None
    out["extras"]["vol_z"] = round(vol_z, 2) if vol_z is not None else None
    out["extras"]["direction"] = direction
    return out


def detect_4patterns(
    candles: List[Any],
    *,
    interval_sec: int = 180,
    min_bar_idx: int = 30,
) -> List[Dict[str, Any]]:
    """v221.10 — 4 类行情独立识别（schema 与 scan_anomalies 兼容）。

    4 类触发（任一 score ≥ 阈值即输出独立 marker）:
      - 交易量激增 (volume_spike):  score ≥ 50
      - 大阳线 (big_bullish):       score ≥ 50（量能加成）
      - 大阴线 (big_bearish):       score ≥ 50
      - 转折点 (reversal_p):        score ≥ 30（实时化）

    与 scan_anomalies 单 indicator 关系:
      - 4 类 marker 是「多因子加权」输出，独立维度
      - 不参与 cofire_dedup（不同语义）
      - importance = 95（最高，必渲染）
    """
    parsed = [p for p in (_candle_parts(c) for c in (candles or [])) if p is not None]
    if len(parsed) < min_bar_idx:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(min_bar_idx, len(parsed)):
        ps = _pattern_score(parsed, i, interval_sec)
        ts_sec = int(parsed[i][TS] // 1000) if parsed[i][TS] is not None else None
        if ts_sec is None:
            continue
        extras = ps["extras"]

        # ── 交易量激增 ──
        if ps["volume_spike"] >= _PATTERN_SCORE_THRESHOLD["volume_spike"]:
            vr_txt = f"{extras.get('vol_ratio')}x" if extras.get("vol_ratio") else ""
            vz_txt = f"Z={extras['vol_z']}" if extras.get("vol_z") is not None else ""
            if vr_txt and vz_txt:
                label = f"量激增·{vr_txt} {vz_txt}"
            elif vr_txt:
                label = f"量激增·{vr_txt}"
            elif vz_txt:
                label = f"量激增·{vz_txt}"
            else:
                label = "交易量激增"
            out.append({
                "ts": ts_sec,
                "type": label,
                "direction": extras.get("direction", "neutral"),
                "indicator": "volume_spike",
                "score": ps["volume_spike"],
                "details": extras,
            })

        # ── 大阳线 ──
        if ps["big_bullish"] >= _PATTERN_SCORE_THRESHOLD["big_bullish"]:
            body_txt = f"{extras['body_pct']:+.2f}%"
            vr_txt = f"量{extras['vol_ratio']}x" if extras.get("vol_ratio") else ""
            label = f"大阳线·{body_txt}"
            if vr_txt:
                label = f"{label} {vr_txt}"
            out.append({
                "ts": ts_sec,
                "type": label,
                "direction": "bullish",
                "indicator": "big_bullish",
                "score": ps["big_bullish"],
                "details": extras,
            })

        # ── 大阴线 ──
        if ps["big_bearish"] >= _PATTERN_SCORE_THRESHOLD["big_bearish"]:
            body_txt = f"{extras['body_pct']:+.2f}%"
            vr_txt = f"量{extras['vol_ratio']}x" if extras.get("vol_ratio") else ""
            label = f"大阴线·{body_txt}"
            if vr_txt:
                label = f"{label} {vr_txt}"
            out.append({
                "ts": ts_sec,
                "type": label,
                "direction": "bearish",
                "indicator": "big_bearish",
                "score": ps["big_bearish"],
                "details": extras,
            })

        # ── 转折点（实时化：v221.10 修正版 — 前 N 根趋势 + 当前 bar 反向）──
        if ps["reversal_p"] >= _PATTERN_SCORE_THRESHOLD["reversal_p"]:
            kind = extras.get("reversal_kind", "bottom_up")
            label = "底部反转" if kind == "bottom_up" else "顶部反转"
            vr_txt = f"量{extras['vol_ratio']}x" if extras.get("vol_ratio") else ""
            if vr_txt:
                label = f"{label} {vr_txt}"
            out.append({
                "ts": ts_sec,
                "type": label,
                "direction": "bullish" if kind == "bottom_up" else "bearish",
                "indicator": "reversal_p",
                "score": ps["reversal_p"],
                "details": extras,
            })
    return out


def scan_abnormals(
    candles: List[Any],
    limit: int = 8,
    *,
    cluster_window_ms: int = L1_CLUSTER_WINDOW_MS_DEFAULT,
    cluster_min_size: int = 3,
    trend_filter: bool = True,
    ma_period: int = 5,
    ma_slope_lookback: int = 10,
    interval_sec: int = 180,
) -> List[Dict[str, Any]]:
    """扫描整段 K，返回最近 ``limit`` 个异动根（升序后截尾）。

    用于图表一次性标注多个异动点（供事件归因页 ⭐ 标注）。

    v170+ 增强（噪声治理）:
      L1 聚簇去重 — ``cluster_window_ms``（v221.3 默认 20min）跨度内
          ≥ ``cluster_min_size`` 个触发，则仅保留首根，避免堆叠（如 5 根挤在一起只标 1 个）。
          swing / cum4（转折点）类型跳过聚簇去重。
      L2 趋势同向过滤 — bar 方向 vs MA(``ma_period``) 在 ``ma_slope_lookback`` 窗口
          斜率同向 → 视为「动量延续」噪声，滤掉；反向或样本不足时保留（潜在转折点）。

    v221 增强（用户 2026-10-03："必备出大阴/大阳 + 量能激增 + 转折点"）:
      ① 自适应阈值 — ``interval_sec`` → 10m=0.6%/1.5×，1h=2.0%/1.5×（3m=1.5%/1.4×）。
         旧版 10m 阈值 1.5%/1.2× 在 BTC 10m 上几乎不触发，截图大阴/大阳全漏。
      ② 滚动摆动检测（``SWING_LOOKBACK``=5）— N 根累计 swing 或区间振幅超阈值
         → 触发 'swing' 类型，覆盖"连跌/连涨形成的大阴/大阳"。
      ③ L2 弱化 — 仅过滤 pct < ``L2_FILTER_PCT_CEILING``（1.5%）的小波动；
         ≥ 1.5% 的一律保留（用户原话："必备出涨跌幅度大的转折点"）。

    v221.3 增强（用户 2026-10-05 截图反馈："大阴/大阳 + 量激增 + 转折点"全漏）:
      ① 阈值全面下调（10m pct 1.5→0.6, vol 1.2→1.5, swing_pct 1.2→0.7, swing_range 1.0→0.6）。
      ② 新增 4 根累计涨跌幅检测（``CUMULATIVE4_LOOKBACK``=4）— 介于单根 pct
         与 5 根 swing 之间，专测短周期转折点（"3-4 根连动后突然反方向"）。
      ③ L1 聚簇窗口收紧 30min→20min，减少堆叠成片。

    v221.4 增强（用户 2026-10-05 第二轮截图反馈：「异常识别还是没识别出大阴线和大阳线，
      也没有是必备出交易量激增；也没有是必备出涨跌幅度大的转折点；摒弃大量没用的噪音」）:
      ① **新增 big_candle 检测器** — 单根 high-low 区间振幅或 |body| 任一超阈值（10m 1.2% / 0.8%），
         覆盖"单根 body 不到 0.6% 但振幅很大"的真实大阴/大阳（旧 pct 阈值过严漏检）。
      ② **新增 reversal 检测器** — 局部 6 根极值 + 当前 close 突破（反向决策），捕捉
         "涨跌幅度大的转折点"（与 swing 多根累计同向互补）。
      ③ **swing 方向门槛改为自适应** —— ``DIRECTION_FLOOR_RATIO=0.8`` × swing_pct_th，
         修复旧硬编码 0.8% 在 10m swing_pct_th=0.7% 下 > 阈值、导致中等 swing 被自相矛盾挡掉的 bug。
      ④ **big_candle / reversal 加入 L2 趋势过滤 bypass + L1 turn_out set**——专属信号
         （大 K 线、转折点）不被"动量延续"滤掉，也不参与聚簇堆叠压缩。

    v221.5 增强（用户 2026-10-05 第三轮截图反馈：「噪声太过了」+ 图2 理想效果）:
      ① **重要性评分 + 门槛过滤** — 每个 trigger_type 有 0-100 importance score，
         低于 ``_ABNORMAL_IMPORTANCE_MIN=45``（cum4=40）的 marker 在 L1/L2 之后被过滤掉。
         cum4 不再在 swing 同 ts 时单独输出（swing=70 已代表同义信号）。
      ② **同方向聚簇** — 同 trigger_type 同方向在 ``CLUSTER_WINDOW_MS_V221_5=30min`` 内
         连续触发 → 合并为 1 个 cluster marker（带 cluster_count + total_pct + cluster_end_ts）。
         8 根连跌 swing 序列 → 1 个「8 根下跌累计 -X%」cluster。
      ③ **回溯落库辅助** — 标记 ``clustered=True`` 后端可识别并写入 abnormal_signals 表。
    """
    pct_th, vol_th, swing_pct_th, swing_range_th, cum4_th = _interval_thresholds(interval_sec)
    parsed = [p for p in (_candle_parts(c) for c in (candles or [])) if p is not None]
    raw: List[Dict[str, Any]] = []
    ts_to_idx: Dict[int, int] = {}
    for idx in range(1, len(parsed)):
        c = parsed[idx]
        pct = _pct_change(c[OPEN], c[CLOSE])
        ratio = _vol_ratio(parsed, idx)
        # v221：使用自适应阈值（不再走 _classify 默认 2.5%/1.5×）
        pct_hit = pct is not None and abs(pct) >= pct_th
        vol_hit = ratio is not None and ratio > vol_th
        if pct_hit and vol_hit:
            ttype = "both"
        elif pct_hit:
            ttype = "pct"
        elif vol_hit:
            ttype = "volume"
        else:
            ttype = ""
        if ttype:
            ts = int(c[TS])
            ts_to_idx[ts] = idx
            raw.append({
                "ts": ts,
                "pct_change": round(pct, 4) if pct is not None else None,
                "vol_ratio": round(ratio, 4) if ratio is not None else None,
                "trigger_type": ttype,
            })

    # ── v221.3：4 根累计涨跌幅检测（短周期转折点专用，见下方 swing 之后）──

    # ── v221：滚动摆动检测（捕捉连跌/连涨形成的大阴/大阳）──
    #   对每根 K（除前 lookback 根外）算子窗口累计 swing + 区间振幅，
    #   任一超阈值即触发 trigger_type='swing'。
    #   v221.2：swing 与 volume/both 可共存于同一 ts（用户原话"必备出"）
    #     —— swing 表示价格变动，volume 表示量能激增，是两个独立信号。
    #     但 swing 与 pct/both 重复时仍只保留第一个（避免同一 ts 多 marker）。
    if len(parsed) > SWING_LOOKBACK:
        seen_swing_ts: set = set()
        for idx in range(SWING_LOOKBACK, len(parsed)):
            sw = _swing_score(parsed, idx, SWING_LOOKBACK, swing_pct_th, swing_range_th)
            if not sw:
                continue
            ts = int(parsed[idx][TS])
            # 同一 ts 已有 pct/both/cum4 时不重复；volume 可共存
            ts_owner = ts_to_idx.get(ts)
            if ts_owner is not None:
                # 检查该 ts 是否已有 pct/both/cum4；如果是，跳过 swing
                # 已有 raw[ts_owner].trigger_type
                existing_type = next((r["trigger_type"] for r in raw if r.get("ts") == ts), None)
                if existing_type in ("pct", "both", "cum4"):
                    continue
            if ts in seen_swing_ts:
                continue
            seen_swing_ts.add(ts)
            ts_to_idx[ts] = idx
            raw.append({
                "ts": ts,
                "pct_change": sw["pct"],
                "vol_ratio": None,
                "trigger_type": "swing",
            })

    # ── v221.3：4 根累计涨跌幅检测（短周期转折点专用，在 swing 之后跑）──
    #   介于「单根 pct」（实时单根）和「5 根 swing」（窗口较宽）之间
    #   捕捉"3-4 根连动后突然反方向"的转折场景——单根阈值滤掉的小段同向 swing
    #   用此检测器补回来；标记 trigger_type='cum4' 与 pct/swing/both/volume 区分。
    #   顺序：swing 先跑（5 根窗口更宽、置信度更高），cum4 只补 swing 未覆盖的 ts。
    if len(parsed) > CUMULATIVE4_LOOKBACK:
        seen_cum4_ts: set = set()
        for idx in range(CUMULATIVE4_LOOKBACK, len(parsed)):
            start = idx - CUMULATIVE4_LOOKBACK + 1
            sub = parsed[start:idx + 1]
            if len(sub) < 2:
                continue
            o0 = sub[0][OPEN]
            cl = sub[-1][CLOSE]
            if o0 is None or cl is None or o0 <= 0:
                continue
            cum_pct = (cl - o0) / o0 * 100.0
            if abs(cum_pct) < cum4_th:
                continue
            ts = int(parsed[idx][TS])
            if ts in seen_cum4_ts:
                continue
            # v221.4.1 — 同一 ts 已有 pct/both 时不重复（这两个语义最具体）；
            # v221.5 — 同 ts 已有 swing 时也跳过：swing=70 ⊃ cum4=50，避免重复标注。
            #   单独 cum4（无 swing）仍保留（4-bar 窗口比 swing 的 5-bar 早触发 1 bar）。
            if ts in ts_to_idx:
                existing_types = {r.get("trigger_type") for r in raw if r.get("ts") == ts}
                if existing_types & {"pct", "both", "swing"}:
                    continue
            seen_cum4_ts.add(ts)
            seen_cum4_ts.add(ts)
            ts_to_idx[ts] = idx
            raw.append({
                "ts": ts,
                "pct_change": round(cum_pct, 4),
                "vol_ratio": None,
                "trigger_type": "cum4",
            })
            # v221.4.1 — 注释：cum4 与 swing 都是多根累计信号，可共存。
            # swing 5 根 / cum4 4 根，窗口略有不同；用户原意保留两路信号便于复盘。
            # 仅 pct/both/big_candle 抢占 ts 时才 skip cum4。

    # ── v221.4：单根大 K 线检测（捕捉"大阴线/大阳线"）──
    #   阈值按 interval 自适应：10m range 1.2% / body 0.8%；3m 0.6% / 0.45%。
    #   旧规则只比 |body|（≤ 1.5% / 0.6%）—— BTC 10m 单根 body 实测 max=-0.65%，几乎
    #   全漏。big_candle 同时看 high-low 区间振幅，覆盖"影线长 + body 小"的真实大阴/大阳。
    #   与 pct/both/swing 不重复（同一 ts 只保一个最强信号），volume 可共存。
    big_range_th = BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL[180])
    big_body_th = BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL[180])
    # v221.6 — event_arrow 阈值（与 scan_anomalies 侧一致）
    ea_body_th = EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL[180])
    ea_vol_th = EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL[180])
    for idx in range(1, len(parsed)):
        bc = _big_candle_score(parsed[idx], big_range_th, big_body_th)
        if not bc:
            continue
        ts = int(parsed[idx][TS])
        # 同一 ts 已有 pct/both/swing/cum4 时不重复（pct 优先）
        existing_type = next((r["trigger_type"] for r in raw if r.get("ts") == ts), None)
        if existing_type in ("pct", "both", "swing", "cum4"):
            continue
        ts_to_idx[ts] = idx
        raw.append({
            "ts": ts,
            "pct_change": bc["pct"],
            "vol_ratio": None,
            "trigger_type": "big_candle",
        })
        # v221.6 — event_arrow（巨量大阴/大阳，body 显著 + 量比 ≥ 阈值）
        #   若当前 bar 已是 big_candle 且同时满足 event_arrow 条件，替换为 event_arrow
        #   （importance=90 更高，且语义更准确：事件 = 量价齐动）。
        ea = _event_arrow_score(parsed, idx, ea_body_th, ea_vol_th)
        if ea:
            # 替换刚 append 的 big_candle 为 event_arrow（更具体的语义）
            raw[-1] = {
                "ts": ts,
                "pct_change": ea["pct"],
                "vol_ratio": ea["vol_ratio"],
                "trigger_type": "event_arrow",
            }

    # ── v221.4：局部转折点检测（捕捉"涨跌幅度大的转折点"）──
    #   与 swing 互补：swing 是 N 根累计同向，reversal 是 6 根窗口内出现极值 + 当前反向突破。
    #   例：22:00 大阴线后 30 分钟内 price 自低点反弹 ≥ 0.6% → reversal bullish。
    #   30 分钟 = 3 根 10m bar，确保决策窗口够短、能赶上快速转折。
    rev_pct_th = REVERSAL_PCT_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), REVERSAL_PCT_THRESHOLD_BY_INTERVAL[180])
    if len(parsed) > REVERSAL_LOOKBACK + 1:
        seen_rev_ts: set = set()
        for idx in range(REVERSAL_LOOKBACK + 1, len(parsed)):
            rv = _reversal_score(parsed, idx, REVERSAL_LOOKBACK,
                                 REVERSAL_DECISION_WINDOW, rev_pct_th)
            if not rv:
                continue
            ts = int(parsed[idx][TS])
            if ts in seen_rev_ts:
                continue
            # v221.4.1 — reversal 与 big_candle 是独立信号（大 K 线 + 转折可同时存在）；
            # 仅当 ts 已有 pct/both/swing/cum4/volume 时才跳过（避免 marker 堆叠）
            existing_type = next((r["trigger_type"] for r in raw if r.get("ts") == ts), None)
            if existing_type in ("pct", "both", "swing", "cum4", "volume"):
                continue
            seen_rev_ts.add(ts)
            ts_to_idx[ts] = idx
            raw.append({
                "ts": ts,
                "pct_change": rv["pct"],
                "vol_ratio": None,
                "trigger_type": "reversal",
            })

    # ── L2: 趋势同向过滤（小波动才走 L2；>= 1.5% 一律保留）──
    # v221 弱化（用户："必备出涨跌幅度大的转折点"）：
    #   'both'      → 量能信号，直接保留
    #   'volume'    → 量能信号，直接保留
    #   'swing'     → 摆动信号（v221 新增），直接保留
    #   'cum4'      → 短周期转折点（v221.3 新增），直接保留（专测转折，不走 L2 滤）
    #   'big_candle'→ 大阴/大阳线（v221.4 新增），直接保留（用户原话"必备出大阴/大阳"）
    #   'reversal'  → 局部转折点（v221.4 新增），直接保留（用户原话"必备出涨跌幅度大的转折点"）
    #   'pct' 且 |pct| >= L2_FILTER_PCT_CEILING → 直接保留（大阴/大阳）
    #   'pct' 且 |pct| <  L2_FILTER_PCT_CEILING → 走 L2 趋势过滤（小波动同向 = 噪声）
    _L2_BYPASS_TYPES = ("both", "volume", "swing", "cum4", "big_candle", "reversal", "event_arrow")  # v221.6: event_arrow 不被 L2 趋势过滤
    if trend_filter and raw:
        closes = [p[CLOSE] for p in parsed]
        ma_arr = technical_indicators.ma(closes, ma_period)
        filtered: List[Dict[str, Any]] = []
        for item in raw:
            t = item.get("trigger_type")
            if t in _L2_BYPASS_TYPES:
                filtered.append(item)
                continue
            bar_pct = item.get("pct_change") or 0.0
            if abs(bar_pct) >= L2_FILTER_PCT_CEILING:
                filtered.append(item)
                continue
            idx = ts_to_idx.get(item["ts"])
            if idx is None or idx < ma_slope_lookback:
                filtered.append(item)
                continue
            ma_cur = ma_arr[idx]
            ma_prev = ma_arr[idx - ma_slope_lookback]
            if ma_cur is None or ma_prev is None:
                filtered.append(item)
                continue
            slope = ma_cur - ma_prev
            if abs(bar_pct) < 1e-9:
                filtered.append(item)
                continue
            slope_dir = 1 if slope > 0 else (-1 if slope < 0 else 0)
            bar_dir = 1 if bar_pct > 0 else -1
            if slope_dir != 0 and slope_dir == bar_dir:
                # 趋势同向小波动 → 视为噪声
                continue
            # 反向 / 斜率近零 → 潜在转折点 → 保留
            filtered.append(item)
        raw = filtered

    # ── L1: 聚簇去重（window_ms 内 ≥min_size 个 → 仅保首）──
    # v221：swing 信号跳过聚簇去重（用户原话"必备出"，且 swing 已代表独立事件）
    # v221.3：cum4 也跳过聚簇去重（4 根累计与 swing 同义，专测转折不参与堆叠压缩）
    # v221.4：big_candle / reversal 同样跳过聚簇去重（用户原话"必备出大阴/大阳"+"必备出转折点"）
    # v221.3：默认窗口收紧到 20min（cluster_window_ms 调用方透传），减少堆叠成片
    _TURN_TYPES = ("swing", "cum4", "big_candle", "reversal")
    if raw:
        raw.sort(key=lambda x: x["ts"])
        turn_out: List[Dict[str, Any]] = [x for x in raw if x.get("trigger_type") in _TURN_TYPES]
        non_turn: List[Dict[str, Any]] = [x for x in raw if x.get("trigger_type") not in _TURN_TYPES]
        deduped: List[Dict[str, Any]] = []
        i = 0
        n = len(non_turn)
        while i < n:
            j = i
            while j + 1 < n and non_turn[j + 1]["ts"] - non_turn[i]["ts"] <= cluster_window_ms:
                j += 1
            cluster_size = j - i + 1
            if cluster_size >= cluster_min_size:
                deduped.append(non_turn[i])
            else:
                deduped.extend(non_turn[i:j + 1])
            i = j + 1
        raw = deduped + turn_out

    # ── v221.5 L3: 聚簇 turn 类型（同方向同类型在 30min 窗口内合并）──
    #   把连续的 swing 序列、big_candle 序列、reversal 序列合并为 1 个 cluster marker。
    #   例：8 根连续下跌 swing → 1 个「cluster_count=8 total_pct=-X%」cluster。
    #   关键：swing/cum4 同 bar 同时触发（cum4 importance=40 不够，会被 L4 滤掉）。
    #   在 cluster 前剔除 cum4 避免同 ts 不同 type 反复 flush → 全部 cluster_count=1。
    _V221_5_CLUSTERABLE_TYPES = {"swing", "big_candle", "reversal"}
    if raw:
        clusterable = [m for m in raw if m.get("trigger_type") in _V221_5_CLUSTERABLE_TYPES]
        non_clusterable = [m for m in raw if m.get("trigger_type") not in _V221_5_CLUSTERABLE_TYPES]
        clusterable = _cluster_turn_markers(
            clusterable,
            window_ms=CLUSTER_WINDOW_MS_V221_5,
            ts_key="ts",
            type_key="trigger_type",
            pct_key="pct_change",
        )
        raw = clusterable + non_clusterable

    # ── v221.5 L4: importance 门槛过滤（cum4 静默但保留 cluster 形式）──
    #   score < _ABNORMAL_IMPORTANCE_MIN 的 marker 不在图表渲染。
    #   cluster marker 的 importance 取决于其 trigger_type（cum4 cluster = 40，滤掉）。
    raw = _importance_filter_abnormal(raw, min_score=_ABNORMAL_IMPORTANCE_MIN)

    return raw[-max(0, min(int(limit), 200)):]


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 —— 多因子触发 + 全板块异动（异常签名 → 重要新闻判定）
# ═══════════════════════════════════════════════════════════════════════════
#
# 决策口径（2026-09-21 拍板）：
#   1. RSI 阈值 70/30、背离窗口 14/30（行业惯例）；
#      全板块「大面积异动」= 已触发异动的资产中，同向占比 ≥60% 且至少 3 个资产。
#   2. 指标落库（abnormal_signals，见 db._CREATE_ABNORMAL_SIGNALS 走 migrate）。
#   3. 清算/链上：先只走公开接口（链上已由 blockchain_feed 覆盖）；清算与链上 key
#      预留 env（有 key 才启用、无 key 静默降级）——本纯函数层不发任何网络请求。
#   4. prompt 注入：只在触发时生成 hint（省 token），由异常触发链路注入归因 prompt。
#   5. 接入范围：BTC / ETH / XAU 全上。

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_PERIOD = 14
SECTOR_SAME_DIR_PCT = 0.6   # 全板块同向占比阈值
SECTOR_MIN_ASSETS = 3        # 至少 N 个资产同向才判「大面积异动」

# v182.3 新增阈值
VOL_ZSCORE_THRESHOLD = 2.0   # 量能 Z-score 绝对值 ≥ 2 视为异常（替代固定 1.5×）

_DIR_LABEL = {"bullish": "偏多", "bearish": "偏空"}


def rsi_trip(closes: List[Any], *,
             overbought: float = RSI_OVERBOUGHT,
             oversold: float = RSI_OVERSOLD,
             period: int = RSI_PERIOD) -> Dict[str, Any]:
    """RSI 超买/超卖触发。返回 {triggered, rsi, direction}。

    超买（≥overbought）= 上涨动能衰竭 → bearish；超卖（≤oversold）→ bullish。
    样本不足返回 triggered=False，绝不抛异常。
    """
    r = technical_indicators.rsi(closes, period)
    val = r[-1] if r else None
    if val is None:
        return {"triggered": False, "rsi": None, "direction": None}
    val = round(val, 2)
    if val >= overbought:
        return {"triggered": True, "rsi": val, "direction": "bearish"}
    if val <= oversold:
        return {"triggered": True, "rsi": val, "direction": "bullish"}
    return {"triggered": False, "rsi": val, "direction": None}


def _attribution_hint(factors: List[str], direction: Optional[str]) -> str:
    """组装归因注入提示语。只在异常触发时调用，避免常态化白烧 token。"""
    d = _DIR_LABEL.get(direction or "", "方向未明")
    return (
        f"⚠️ 技术面异常签名已触发：{'、'.join(factors)}（净信号：{d}）。"
        f"请优先核对是否有对应的重要新闻/资金异动作为归因，而非沿用旧趋势惯性。"
    )


def multi_factor(candles: List[Any], interval_sec: int = 180) -> Dict[str, Any]:
    """多因子触发汇总（任一击中即触发）。

    因子清单（v182.3 + Q5 扩展）：
      1. 单根异动（涨跌幅≥2.5% 或 量>1.5×）
      2. RSI 超买超卖（70/30）
      3. RSI 顶/底背离
      4. 量价顶/底背离（volume_regime）
      5. KDJ 超买超卖（80/20）
      6. MFI 超买超卖（80/20，量价加权）
      7. 布林带突破（外轨 2σ）
      8. 量能 Z-score（替代 1.5× 固定阈值）—— Q6 修复：按方向投票
      9. MA5/MA20 金叉死叉
     10. 单根 K 线形态（hammer/shooting_star/doji）
     11. ATR 突破（波动率扩张）
     12. MACD 金叉死叉（macd_cross）—— Q5 新增
     13. MACD 零轴穿越（macd_zero_cross）—— Q5 新增
     14. RSI 钝化（连续超买/超卖 rsi_blunting）—— Q5 新增
     15. 量能突破（量能显著放大 + 价格突破近 20 根极值 vol_break）—— Q5 新增
     16. 吞没形态（两 K 线实体包覆 engulfing）—— Q5 新增

    方向用多因子投票：bearish/bullish 得票多者胜，平票 → None。
    hint 只在 triggered 时非空（异常触发时才注入归因，省 token）。

    返回字段新增（v182.3 + Q5）：kdj / mfi / bollinger_break / vol_z / ma_cross /
    candle_pattern / atr_break / macd_cross / macd_zero_cross / rsi_blunting /
    vol_break / engulfing 供落库回查。
    """
    parsed = [p for p in (_candle_parts(c) for c in (candles or [])) if p is not None]
    closes = [p[CLOSE] for p in parsed]
    highs_arr = [p[HIGH] for p in parsed]
    lows_arr = [p[LOW] for p in parsed]

    base = detect_abnormal(candles, interval_sec=interval_sec)
    rsi_hit = rsi_trip(closes)
    rsi_div = technical_indicators.rsi_divergence(closes)
    vol_regime = technical_indicators.volume_regime(parsed)
    vol_div = vol_regime.get("divergence")

    # ── v182.3 因子 ──
    kdj_vals = technical_indicators.kdj(closes, highs_arr, lows_arr)
    kdj_hit = technical_indicators.kdj_trip(kdj_vals)
    mfi_hit = technical_indicators.mfi_trip(parsed)
    bb_break = technical_indicators.bollinger_break(closes)
    vol_z = technical_indicators.vol_zscore(parsed)
    ma_x = technical_indicators.ma_cross(closes)
    candle_pat = technical_indicators.candle_pattern(parsed[-1]) if parsed else None
    atr_brk = technical_indicators.atr_break(parsed)

    # ── Q5 因子（macd_cross + 4 个新指标） ──
    macd_x = technical_indicators.macd_cross(closes)
    macd_zero_x = technical_indicators.macd_zero_cross(closes)
    rsi_blunt = technical_indicators.rsi_blunting(closes)
    vol_brk = technical_indicators.vol_break(parsed)
    eng = technical_indicators.engulfing(parsed)

    bull = 0
    bear = 0
    factors: List[str] = []

    if base["triggered"]:
        pct = base.get("pct_change")
        if pct is not None:
            if pct < 0:
                bear += 1
            elif pct > 0:
                bull += 1
        factors.append(f"单根异动/{base['trigger_type']}")

    if rsi_hit["triggered"]:
        if rsi_hit["direction"] == "bearish":
            bear += 1
            factors.append("RSI超买")
        else:
            bull += 1
            factors.append("RSI超卖")

    if rsi_div == "bearish":
        bear += 1
        factors.append("RSI顶背离")
    elif rsi_div == "bullish":
        bull += 1
        factors.append("RSI底背离")

    if vol_div == "sell":
        bear += 1
        factors.append("量价顶背离")
    elif vol_div == "buy":
        bull += 1
        factors.append("量价底背离")

    # ── 新因子投票 ──
    if kdj_hit["triggered"]:
        if kdj_hit["direction"] == "bearish":
            bear += 1
            factors.append(f"KDJ超买(J={kdj_hit['j']})")
        else:
            bull += 1
            factors.append(f"KDJ超卖(J={kdj_hit['j']})")

    if mfi_hit["triggered"]:
        if mfi_hit["direction"] == "bearish":
            bear += 1
            factors.append(f"MFI超买({mfi_hit['mfi']})")
        else:
            bull += 1
            factors.append(f"MFI超卖({mfi_hit['mfi']})")

    if bb_break == "upper_break":
        bear += 1
        factors.append("布林上轨突破")
    elif bb_break == "lower_break":
        bull += 1
        factors.append("布林下轨突破")

    if vol_z is not None and abs(vol_z) >= VOL_ZSCORE_THRESHOLD:
        factors.append(f"量能Z异常(z={vol_z})")
        # Q6 修复：按价格方向投票（z>0 放量 + 价格同向）
        if vol_z > 0 and len(closes) >= 2 and closes[-1] > closes[-2]:
            bull += 1   # 放量上涨 → 顺势多头
        elif vol_z > 0 and len(closes) >= 2 and closes[-1] < closes[-2]:
            bear += 1   # 放量下跌 → 顺势空头
        # z<0（缩量）暂不投票，等其他因子判方向

    if ma_x == "golden":
        bull += 1
        factors.append("MA5/MA20金叉")
    elif ma_x == "death":
        bear += 1
        factors.append("MA5/MA20死叉")

    # K 线形态：方向依位置；这里只标翻，不强加方向（避免误判）
    if candle_pat == "hammer":
        bull += 1
        factors.append("锤子线")
    elif candle_pat == "shooting_star":
        bear += 1
        factors.append("射击之星")
    elif candle_pat == "doji":
        factors.append("十字星")  # 中性信号，不投票

    if atr_brk:
        factors.append("ATR波动率扩张")  # 中性预警

    # ── Q5 因子投票（macd_cross + 4 个新指标） ──
    if macd_x == "golden":
        bull += 1
        factors.append("MACD金叉")
    elif macd_x == "death":
        bear += 1
        factors.append("MACD死叉")

    if macd_zero_x == "above_zero":
        bull += 1
        factors.append("MACD上穿零轴")
    elif macd_zero_x == "below_zero":
        bear += 1
        factors.append("MACD下穿零轴")

    if rsi_blunt == "bearish":
        bear += 1
        factors.append("RSI超买钝化")
    elif rsi_blunt == "bullish":
        bull += 1
        factors.append("RSI超卖钝化")

    if vol_brk == "bullish_break":
        bull += 1
        factors.append("放量向上突破")
    elif vol_brk == "bearish_break":
        bear += 1
        factors.append("放量向下突破")

    if eng == "bullish":
        bull += 1
        factors.append("看涨吞没")
    elif eng == "bearish":
        bear += 1
        factors.append("看跌吞没")

    triggered = bool(factors)
    direction = "bearish" if bear > bull else ("bullish" if bull > bear else None)
    hint = _attribution_hint(factors, direction) if triggered else ""
    return {
        "triggered": triggered,
        "factors": factors,
        "direction": direction,
        "rsi": rsi_hit["rsi"],
        "rsi_divergence": rsi_div,
        "volume_divergence": vol_div,
        # v182.3 新增回查字段
        "kdj": kdj_vals.get("j", [-1])[-1] if kdj_vals.get("j") else None,
        "mfi": mfi_hit["mfi"],
        "bollinger_break": bb_break,
        "vol_z": vol_z,
        "ma_cross": ma_x,
        "candle_pattern": candle_pat,
        "atr_break": atr_brk,
        # Q5 新增回查字段（5 个）
        "macd_cross": macd_x,
        "macd_zero_cross": macd_zero_x,
        "rsi_blunting": rsi_blunt,
        "vol_break": vol_brk,
        "engulfing": eng,
        "hint": hint,
    }


def scan_anomalies(candles: List[Any], *, limit: int = 12,
                   scan_window: int = 60, interval_sec: int = 180) -> List[Dict[str, Any]]:
    """逐根扫异常 → 给前端 chart markers 用。

    对每根 K 线（仅最近 scan_window 根）以该 K 线为止切 sub-history，跑：
      - macd_cross / macd_zero_cross（横切信号：最新一根 cross 即可）
      - rsi_blunting（连续超买/超卖，**v221.4 仅 transition 时输出**——降噪）
      - vol_break（量能显著放大 + 价格突破近 20 根极值）
      - engulfing（两 K 线实体包覆）
      - **big_candle**（v221.4 新增——单根 high-low range / |body| 超阈值）
      - **vol_z**（v221.4 新增——纯量能 Z-score ≥ 阈值，不要求价格突破）
      - **reversal**（v221.4 新增——局部极值 + 短促反向突破）

    返回：按时间倒序（最新在前）、按 (ts, indicator) 去重、最多 limit 条。
    每条 schema：{ts, type, direction, indicator} —— 前端 markers 直接用。

    v221.5 增强（用户 2026-10-05 反馈：「噪声太过了」+ 图2 理想效果）:
      ① **同 ts 多 indicator 去重** — 同一根 bar 同时触发 vol_break + vol_z → 仅保 vol_break
         （vol_break 含价格突破语义）；reversal + engulfing → 仅保 reversal（更具体的反转）。
         去重规则见 ``_COFIRE_PARENT_PRECEDENCE``。
      ② **importance 门槛过滤** — 每个 indicator 有 0-100 importance score；
         低于 ``_ANOMALY_IMPORTANCE_MIN=40`` 的 marker 被滤掉（engulfing=35 / vol_z=35 静默）。
    """
    parsed = [p for p in (_candle_parts(c) for c in (candles or [])) if p is not None]
    if len(parsed) < 30:
        return []
    closes_full = [p[CLOSE] for p in parsed]
    scan_start = max(0, len(parsed) - int(scan_window))
    out: List[Dict[str, Any]] = []
    prev_rsi_blunt = None  # v221.4 — 仅 transition 时输出 rsi_blunting
    big_range_th = BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL[180])
    big_body_th = BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL[180])
    vol_z_th = VOL_Z_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), VOL_Z_THRESHOLD_BY_INTERVAL[180])
    rev_pct_th = REVERSAL_PCT_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), REVERSAL_PCT_THRESHOLD_BY_INTERVAL[180])
    # v221.6 — event_arrow 阈值
    ea_body_th = EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL[180])
    ea_vol_th = EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL.get(
        int(interval_sec), EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL[180])
    for i in range(scan_start, len(parsed)):
        sub_parsed = parsed[:i + 1]
        sub_closes = closes_full[:i + 1]
        try:
            macd_x = technical_indicators.macd_cross(sub_closes)
            macd_zero_x = technical_indicators.macd_zero_cross(sub_closes)
            rsi_blunt = technical_indicators.rsi_blunting(sub_closes)
            vol_brk = technical_indicators.vol_break(sub_parsed)
            eng = technical_indicators.engulfing(sub_parsed)
            bc = _big_candle_score(parsed[i], big_range_th, big_body_th)
            vol_z_val = technical_indicators.vol_zscore(sub_parsed)
            # v221.6 — 事件箭头（巨量大阴/大阳）
            ea = _event_arrow_score(parsed, i, ea_body_th, ea_vol_th)
            rv = None
            if i >= REVERSAL_LOOKBACK + 1:
                rv = _reversal_score(parsed, i, REVERSAL_LOOKBACK,
                                     REVERSAL_DECISION_WINDOW, rev_pct_th)
        except Exception:
            continue
        bar_ts_ms = parsed[i][TS]
        # v221.4 — 修复：bar_ts_ms=0 时旧 `if bar_ts_ms` 判 falsy → ts_sec=None。
        # 改用 `is not None` 保留 ts=0 的合法 bar（之前测试恰好漏过此 case）。
        ts_sec = int(bar_ts_ms // 1000) if bar_ts_ms is not None else None
        if macd_x:
            out.append({
                "ts": ts_sec,
                "type": "MACD金叉" if macd_x == "golden" else "MACD死叉",
                "direction": "bullish" if macd_x == "golden" else "bearish",
                "indicator": "macd_cross",
            })
        if macd_zero_x:
            out.append({
                "ts": ts_sec,
                "type": "MACD上穿零轴" if macd_zero_x == "above_zero" else "MACD下穿零轴",
                "direction": "bullish" if macd_zero_x == "above_zero" else "bearish",
                "indicator": "macd_zero_cross",
            })
        # v221.4 — rsi_blunting transition-only（避免 RSI 高位连续 10 根钝化时刷 10 marker）
        if rsi_blunt and rsi_blunt != prev_rsi_blunt:
            out.append({
                "ts": ts_sec,
                "type": "RSI超买钝化" if rsi_blunt == "bearish" else "RSI超卖钝化",
                "direction": rsi_blunt,
                "indicator": "rsi_blunting",
            })
        prev_rsi_blunt = rsi_blunt
        if vol_brk:
            out.append({
                "ts": ts_sec,
                "type": "放量向上突破" if vol_brk == "bullish_break" else "放量向下突破",
                "direction": "bullish" if vol_brk == "bullish_break" else "bearish",
                "indicator": "vol_break",
            })
        if eng:
            out.append({
                "ts": ts_sec,
                "type": "看涨吞没" if eng == "bullish" else "看跌吞没",
                "direction": eng,
                "indicator": "engulfing",
            })
        # v221.4 — big_candle（v221.4.1 区分 body_strong vs wick_strong）
        if bc:
            if bc["kind"] == "body_strong":
                t_label = "大阳线" if bc["direction"] == "bullish" else "大阴线"
            else:
                t_label = "长影线阳" if bc["direction"] == "bullish" else "长影线阴"
            out.append({
                "ts": ts_sec,
                "type": t_label,
                "direction": bc["direction"],
                "indicator": "big_candle",
            })
        # v221.6 — event_arrow（巨量大阴/大阳，单根 body 显著 + 量比 ≥ 阈值）
        #   与 big_candle / vol_break / vol_z 同根同时触发时，按 _COFIRE_PARENT_PRECEDENCE
        #   优先级（event_arrow ⊃ 三个）去重，仅保 event_arrow。
        if ea:
            t_label = "放量异动·上行" if ea["direction"] == "bullish" else "放量异动·下行"
            out.append({
                "ts": ts_sec,
                "type": t_label,
                "direction": ea["direction"],
                "indicator": "event_arrow",
            })
        # v221.4 — vol_z（纯量能 z，解耦 vol_break）
        if vol_z_val is not None and abs(vol_z_val) >= vol_z_th:
            if i >= 1 and parsed[i][CLOSE] is not None and parsed[i - 1][CLOSE] is not None:
                if parsed[i][CLOSE] > parsed[i - 1][CLOSE]:
                    direction = "bullish"
                elif parsed[i][CLOSE] < parsed[i - 1][CLOSE]:
                    direction = "bearish"
                else:
                    direction = "neutral"
            else:
                direction = "neutral"
            out.append({
                "ts": ts_sec,
                "type": "放量激增" if vol_z_val > 0 else "缩量异动",
                "direction": direction,
                "indicator": "vol_z",
            })
        # v221.4 — reversal
        if rv:
            out.append({
                "ts": ts_sec,
                "type": "底部反转" if rv["direction"] == "bullish" else "顶部回落",
                "direction": rv["direction"],
                "indicator": "reversal",
            })
    # 按 ts 倒序、按 (ts, indicator) 去重（同根同指标只保留一次）
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for a in sorted(out, key=lambda x: (x["ts"] or 0), reverse=True):
        k = (a["ts"], a["indicator"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(a)
    # ── v221.5 L2: 同 ts 多 indicator 去重（语义优先级）──
    #   vol_break ⊃ vol_z（vol_break 含价格突破）
    #   reversal ⊃ engulfing（reversal 是更具体的反转信号）
    #   同 ts 内 vol_break + vol_z → 只保 vol_break
    #   同 ts 内 reversal + engulfing → 只保 reversal
    if deduped:
        deduped = _cofire_dedup_anomaly(deduped, precedence=_COFIRE_PARENT_PRECEDENCE)
    # ── v221.5 L3: importance 门槛过滤（engulfing=35 / vol_z=35 静默）──
    if deduped:
        deduped = _importance_filter_anomaly(deduped, min_score=_ANOMALY_IMPORTANCE_MIN)
    # ── v221.10: 4 类行情独立 marker 追加（多因子加权，已自带 95 分，不参与 dedup/过滤）──
    #   用户 2026-10-05 截图 4 个红箭头对应：交易量激增/大阳线/大阴线/转折点
    #   这些是「多因子加权得分」输出，与单 indicator 是不同维度，必出
    #   关键：4 类 marker 不被 limit 截断 —— 用户要求「全部都要识别到」
    try:
        pattern_markers = detect_4patterns(candles, interval_sec=interval_sec)
    except Exception:
        pattern_markers = []
    # v221.10: 拆 limit —— 单 marker 限 limit 条，4 类 marker 独立（最多 limit 条）
    single_markers = deduped  # 已去重 + 过滤（scan_anomalies 内部已按 ts 倒序）
    single_markers = single_markers[:max(0, min(int(limit), 200))]
    # v221.10 修正：pattern_markers 按 ts 倒序后再截尾（detect_4patterns 是正序输出）
    pattern_markers = sorted(
        pattern_markers, key=lambda x: (x["ts"] or 0), reverse=True
    )
    pattern_markers = pattern_markers[:max(0, min(int(limit), 200))]
    merged = list(single_markers) + list(pattern_markers)
    merged = sorted(merged, key=lambda x: (x["ts"] or 0), reverse=True)
    return merged


def sector_anomaly(snapshots: Dict[str, Dict[str, Any]], *,
                   same_dir_pct: float = SECTOR_SAME_DIR_PCT,
                   min_assets: int = SECTOR_MIN_ASSETS) -> Dict[str, Any]:
    """全板块异动：多个资产在同一时间窗同向异动 → 疑似重大宏观/资金驱动。

    snapshots: {asset: {"triggered": bool, "direction": "bullish"|"bearish"|None, ...}}
    同向占比 = 众数方向触发资产数 / 已触发资产总数；< min_assets 或 < same_dir_pct 不触发。
    """
    triggered = {
        a: s for a, s in (snapshots or {}).items()
        if isinstance(s, dict) and s.get("triggered")
    }
    bulls = [a for a, s in triggered.items() if s.get("direction") == "bullish"]
    bears = [a for a, s in triggered.items() if s.get("direction") == "bearish"]
    total = len(triggered)
    n = max(len(bulls), len(bears))
    ratio = round(n / total, 4) if total else 0.0
    if total < min_assets or n < min_assets or ratio < same_dir_pct:
        return {
            "triggered": False, "direction": None,
            "total_triggered": total, "same_dir_count": n,
            "same_dir_ratio": ratio, "same_dir_assets": [],
            "hint": "",
        }
    direction = "bullish" if len(bulls) >= len(bears) else "bearish"
    same_dir_assets = bulls if direction == "bullish" else bears
    label = _DIR_LABEL[direction]
    hint = (
        f"⚠️ 全板块大面积异动：{total} 个资产异动中 {n} 个同向{label}"
        f"（{', '.join(same_dir_assets)}），疑似有重大宏观/资金事件，请优先做事件归因。"
    )
    return {
        "triggered": True, "direction": direction,
        "total_triggered": total, "same_dir_count": n,
        "same_dir_ratio": ratio, "same_dir_assets": same_dir_assets,
        "hint": hint,
    }


def record_signal(conn: Any, *, ts: Optional[int], asset: str, signal_kind: str,
                  direction: Optional[str] = None, anchor_ts: Optional[int] = None,
                  detail: Optional[Dict[str, Any]] = None, hint: str = "",
                  status: str = "open") -> int:
    """把一条异常签名写入 abnormal_signals（Phase 2 落库），返回新行 id。

    abnormal_signals 表：ts(触发 unix 秒) / asset / signal_kind / direction /
    anchor_ts(触发 K 线 ts_ms，sector 为 NULL) / detail_json / hint / status。
    传入毫秒会自动归一为秒；幂等/去重交给调用方，本函数只插入不查重。
    """
    if ts is not None and ts > 10_000_000_000:  # 毫秒 → 秒
        ts = int(ts) // 1000
    cur = conn.execute(
        """INSERT INTO abnormal_signals
           (ts, asset, signal_kind, direction, anchor_ts, detail_json, hint, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(ts) if ts is not None else None, str(asset or ""), str(signal_kind or ""),
         str(direction or ""), int(anchor_ts) if anchor_ts is not None else None,
         json.dumps(detail or {}, ensure_ascii=False),
         str(hint or ""), str(status or "open")),
    )
    return int(cur.lastrowid or 0)


def record_attribution(conn: Any, *, signal_id: int, asset: str,
                       signal_kind: str, anchor_ts: Optional[int],
                       summary: str = "", drivers: Optional[List[Dict[str, Any]]] = None,
                       cross_asset_likely: Optional[List[str]] = None,
                       actionable: str = "观望", severity_hint: int = 1,
                       source_model: str = "", used_news_n: int = 0) -> int:
    """把 AI 归因结果写入 abnormal_attributions，返回新行 id。

    同一 signal_id 可多次写（保留多次归因历史）；前端默认取最新一条。
    任意字段缺失也允许落库（summary=''/severity=1 兜底）。
    """
    cur = conn.execute(
        """INSERT INTO abnormal_attributions
           (signal_id, asset, signal_kind, anchor_ts,
            summary, drivers_json, cross_json, actionable,
            severity_hint, source_model, used_news_n)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(signal_id), str(asset or ""), str(signal_kind or ""),
         int(anchor_ts) if anchor_ts is not None else None,
         str(summary or "")[:300],
         json.dumps(drivers or [], ensure_ascii=False),
         json.dumps(cross_asset_likely or [], ensure_ascii=False),
         str(actionable or "观望")[:40],
         int(severity_hint) if severity_hint in (1, 2, 3) else 1,
         str(source_model or "")[:60],
         int(used_news_n or 0)),
    )
    return int(cur.lastrowid or 0)


def fetch_latest_attribution(conn: Any, signal_id: int) -> Optional[Dict[str, Any]]:
    """取 signal_id 的最新一条归因；无返回 None。"""
    row = conn.execute(
        """SELECT id, signal_id, asset, signal_kind, anchor_ts, summary,
                  drivers_json, cross_json, actionable, severity_hint,
                  source_model, used_news_n, created_at
           FROM abnormal_attributions
           WHERE signal_id = ?
           ORDER BY id DESC LIMIT 1""",
        (int(signal_id),),
    ).fetchone()
    if not row:
        return None
    cols = ("id", "signal_id", "asset", "signal_kind", "anchor_ts", "summary",
            "drivers_json", "cross_json", "actionable", "severity_hint",
            "source_model", "used_news_n", "created_at")
    out = {cols[i]: row[i] for i in range(len(cols))}
    try:
        out["drivers"] = json.loads(out.pop("drivers_json") or "[]")
    except Exception:
        out["drivers"] = []
    try:
        out["cross_asset_likely"] = json.loads(out.pop("cross_json") or "[]")
    except Exception:
        out["cross_asset_likely"] = []
    return out


def _last_ts_ms(candles: Any) -> Optional[int]:
    """倒序找最后一根有效 K 线的 ts_ms（升序蜡烛序列的最后有效锚）。"""
    for c in reversed(candles or []):
        p = _candle_parts(c)
        if p and p[TS] is not None:
            return int(p[TS])
    return None


def eval_assets(candles_by_asset: Dict[str, List[Any]], *,
                same_dir_pct: float = SECTOR_SAME_DIR_PCT,
                min_assets: int = SECTOR_MIN_ASSETS,
                interval_sec: int = 180) -> Dict[str, Any]:
    """多资产异常签名评估（Phase 2 编排）：每资产 multi_factor + 跨资产 sector_anomaly。

    candles_by_asset: {asset: candles 升序}。返回：
      per_asset: {asset: multi_factor 结果}
      sector:    sector_anomaly 结果
      signals:   [可落库的签名记录 …]（signal_kind ∈ {multi_factor, sector}），
                 每条含 ts(秒)/asset/signal_kind/direction/anchor_ts/detail/hint。
    v221：``interval_sec`` 透传给 multi_factor，单根 pct/vol 阈值自适应。
    纯函数、不发网络请求；实际落库由调用方逐条 record_signal。
    """
    per_asset: Dict[str, Any] = {}
    snapshots: Dict[str, Dict[str, Any]] = {}
    signals: List[Dict[str, Any]] = []
    latest_ts_sec: Optional[int] = None
    for asset, candles in (candles_by_asset or {}).items():
        mf = multi_factor(candles, interval_sec=interval_sec)
        per_asset[asset] = mf
        snapshots[asset] = {"triggered": mf["triggered"], "direction": mf["direction"]}
        last_ms = _last_ts_ms(candles)
        ts_sec = (last_ms // 1000) if last_ms else None
        if ts_sec is not None:
            latest_ts_sec = ts_sec if latest_ts_sec is None else max(latest_ts_sec, ts_sec)
        if not mf["triggered"]:
            continue
        anchor = detect_abnormal(candles).get("anchor_ts")
        signals.append({
            "ts": ts_sec, "asset": str(asset or ""), "signal_kind": "multi_factor",
            "direction": mf["direction"] or "", "anchor_ts": anchor,
            "detail": {
                "factors": mf["factors"], "rsi": mf["rsi"],
                "rsi_divergence": mf["rsi_divergence"],
                "volume_divergence": mf["volume_divergence"],
                # v182.3 新增字段（落库回查）
                "kdj": mf.get("kdj"),
                "mfi": mf.get("mfi"),
                "bollinger_break": mf.get("bollinger_break"),
                "vol_z": mf.get("vol_z"),
                "ma_cross": mf.get("ma_cross"),
                "candle_pattern": mf.get("candle_pattern"),
                "atr_break": mf.get("atr_break"),
            },
            "hint": mf["hint"],
        })
    sector = sector_anomaly(snapshots, same_dir_pct=same_dir_pct, min_assets=min_assets)
    if sector["triggered"]:
        signals.append({
            "ts": latest_ts_sec, "asset": "SECTOR", "signal_kind": "sector",
            "direction": sector["direction"] or "", "anchor_ts": None,
            "detail": {
                "total_triggered": sector["total_triggered"],
                "same_dir_count": sector["same_dir_count"],
                "same_dir_ratio": sector["same_dir_ratio"],
                "same_dir_assets": sector["same_dir_assets"],
            },
            "hint": sector["hint"],
        })
    return {"per_asset": per_asset, "sector": sector, "signals": signals}


_SIGNAL_COLS = ["id", "ts", "asset", "signal_kind", "direction", "anchor_ts",
                "detail_json", "hint", "status", "created_at"]


def query_recent_signals(conn: Any, *, asset: Optional[str] = None,
                         signal_kind: Optional[str] = None,
                         since_ts: Optional[int] = None,
                         status: str = "open",
                         limit: int = 50) -> List[Dict[str, Any]]:
    """反查 abnormal_signals（「重要新闻判定」/ 复盘页追溯入口）。

    asset/signal_kind: 过滤条件（可选）；since_ts: unix 秒下限；
    status 默认 'open'（未归因/未否定的活跃签名）。返回按 ts 倒序，
    detail_json 反序列化为 detail 字段。conn 无需依赖 row_factory。
    """
    sql = "SELECT id, ts, asset, signal_kind, direction, anchor_ts, detail_json, hint, status, created_at" \
          " FROM abnormal_signals WHERE 1=1"
    params: List[Any] = []
    if asset:
        sql += " AND asset = ?"
        params.append(str(asset).upper())
    if signal_kind:
        sql += " AND signal_kind = ?"
        params.append(signal_kind)
    if since_ts is not None:
        sql += " AND ts >= ?"
        params.append(int(since_ts))
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows: List[Dict[str, Any]] = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(zip(_SIGNAL_COLS, r))
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except (TypeError, ValueError):
            d["detail"] = {}
        rows.append(d)
    return rows