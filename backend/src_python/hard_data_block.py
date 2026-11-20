"""hard_data_block.py —— 硬数据证据的纯格式化 / 质量判定（零依赖，可单测）。

为什么单列一个模块（v397）：
  `ai_worker._build_evidence_from_local` 里"拼一条证据行 + 判冻结/过期"是纯函数逻辑，
  但和数据访问耦合在一起，无法离线断言。而这段文案会直接进 prompt 和
  `hard_data_evidence` 落库，措辞一错模型就会把 0.00% 读成"真的没波动"，
  所以必须能被单测钉住。

  实测背景（2026-11-16）：470 条决策里 hard_data_evidence 只有 21%、
  market_confirmation 99.1% 是 unknown —— 模型看不到可引用的数，
  只能写 unknown。本模块只做"把已有 K 线说清楚"，不做任何推断。

约定：
  · 拿不到 → 返回 ""，绝不拿 0 占位或编价。
  · 冻结（high==low 且 open==close）或过期（>30 分钟）→ 必须带
    "数据质量提示 … 仅作记录，不作方向依据"，语义是"别拿它当盘面"。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable, Sequence, Tuple

# 过期阈值：本地 1m 线超过这个分钟数就视为"只能记录、不能作方向依据"
STALE_MINUTES = 30


Bar = Sequence[Any]  # (ts_epoch, open, high, low, close, volume)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def quality_notes(last_bar: Bar, now_ts: float) -> list[str]:
    """返回该根 K 线的质量提示（空列表 = 数据可用）。

    · 冻结：high == low 且 open == close（真实盘面不可能四价全等）
    · 过期：最后更新时间距今 > STALE_MINUTES
    """
    if not last_bar:
        return []
    _key = _f(last_bar[0])
    o, h, l, c = (_f(last_bar[1]), _f(last_bar[2]), _f(last_bar[3]), _f(last_bar[4]))
    notes: list[str] = []
    if h == l and o == c:
        notes.append("该K线无振幅（冻结报价）")
    age_min = int((float(now_ts) - _key) / 60) if _key else 0
    if age_min > STALE_MINUTES:
        notes.append(f"最后更新距今 {age_min} 分钟")
    return notes


def format_bar(asset: str, bar: Bar) -> str:
    """1m 行：`XAU 15:53 close=3853.1200 pct=+0.00% vol=0`"""
    ts, o, _h, _l, c, v = bar[0], _f(bar[1]), bar[2], bar[3], _f(bar[4]), bar[5]
    t_str = _dt.datetime.fromtimestamp(_f(ts)).strftime("%H:%M")
    chg = ((c - o) / o * 100.0) if o else 0.0
    return f"{asset} {t_str} close={c:.4f} pct={chg:+.2f}% vol={_f(v):.0f}"


def format_hour(asset: str, hour_rows: Iterable[Bar]) -> str:
    """用最近一小时的 1m 行聚出 1H 行（hour_rows 为 DESC，最新在前）。

    少于 2 根 → 返回 ""（不足一小时就没法说"这一小时"）。
    """
    rows = list(hour_rows or [])
    if not rows:
        return ""
    bucket = int(_f(rows[0][0])) // 3600
    grp = [r for r in rows if int(_f(r[0])) // 3600 == bucket]
    if len(grp) < 2:
        return ""
    o1 = _f(grp[-1][1])
    c1 = _f(grp[0][4])
    hi1 = max(_f(r[2]) for r in grp)
    lo1 = min(_f(r[3]) for r in grp)
    chg1 = ((c1 - o1) / o1 * 100.0) if o1 else 0.0
    t1 = _dt.datetime.fromtimestamp(int(_f(grp[0][0]))).strftime("%m-%d %H:%M")
    return (f"{asset} {t1} 1H close={c1:.4f} pct={chg1:+.2f}% "
            f"range={lo1:.4f}~{hi1:.4f}")


def build_block(
    asset: str,
    last_bar: Bar | None,
    hour_rows: Iterable[Bar] = (),
    *,
    now_ts: float | None = None,
) -> str:
    """拼一条可直接进 prompt 的硬数据证据（拿不到则 ""）。"""
    if not last_bar:
        return ""
    now = float(now_ts) if now_ts is not None else _dt.datetime.now().timestamp()
    parts: list[str] = [format_bar(asset, last_bar)]
    notes = quality_notes(last_bar, now)
    if notes:
        parts.append(
            f"{asset} 数据质量提示：{'；'.join(notes)} —— 仅作记录，不作方向依据")
    hour = format_hour(asset, hour_rows)
    if hour:
        parts.append(hour)
    return " | ".join(parts)


def is_usable(block: str) -> bool:
    """证据里带"不作方向依据" → 不可用于方向判断（客观性护栏）。"""
    return bool(block) and "不作方向依据" not in block


def _is_flat(bar: Bar) -> bool:
    o, h, l, c = (_f(bar[1]), _f(bar[2]), _f(bar[3]), _f(bar[4]))
    return h == l and o == c


def select_bar(rows: Sequence[Bar], now_ts: float | None = None) -> Bar | None:
    """从 DESC 的 1m 行里挑"能代表盘面"的那一根（v409）。

    起因（2026-11-17 实测）：XAU/WTI 有 184 条决策被判 `missing:feed_stale`，
    但本地 K 线其实是新鲜的 —— 每次整点刚过，最新一根 1m 线只包含一笔成交，
    open=high=low=close，被 `quality_notes` 当成"冻结报价" → `is_usable` False
    → 转去网络拿（HKT 盘前拿不到）→ 证据为空。

    判据只区分"这小时刚开局"和"真的冻住"：
      · 最新一根无振幅，且它落在当前小时、该小时内的 1m 行 < 3 根
        → 视为未走完的进行中 K 线，回退到最近一根有振幅的；
      · 其余情况（整小时都无振幅 / 不在当前小时 / 已经是老数据）→ 保持原样，
        该带的"冻结/过期"提示照常带，绝不假装可用。
    """
    rows = list(rows or [])
    if not rows:
        return None
    top = rows[0]
    if not _is_flat(top):
        return top
    now = float(now_ts) if now_ts is not None else _dt.datetime.now().timestamp()
    bucket_top = int(_f(top[0])) // 3600
    bucket_now = int(now) // 3600
    if bucket_top != bucket_now:
        return top
    in_bucket = [r for r in rows if int(_f(r[0])) // 3600 == bucket_top]
    if len(in_bucket) >= 3:
        return top                      # 整小时都没振幅 = 真冻结
    for r in rows[1:]:
        if not _is_flat(r):
            return r                    # 回退到最近一根走完的 K 线
    return top


def cache_key(asset: str, now_ts: float, ttl_sec: float) -> Tuple[str, int]:
    """TTL 缓存键：品种小写 + 时间片。同片内复用，避免每条新闻都读一次库。"""
    asset_key = str(asset or "").strip().upper()
    ttl = max(1.0, float(ttl_sec or 1.0))
    bucket = int(float(now_ts) // ttl)
    return asset_key, bucket
