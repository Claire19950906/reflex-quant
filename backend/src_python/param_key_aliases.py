"""v339: strategy_param_adjustments 的 param_key 命名空间别名层。

背景（实测 2026-11-14）
----------------------
反思侧（`engine/reflection.py` 的候选生成、v182 LLM 建议）写库时用的是
**带命名空间前缀** 的 key：`score.buy_min_score`、`sizing.position_weight`、
`filter.asset_match.main_asset_missed`……

而审批侧用的是 **裸 key**：
  · `paper_trading._V182_SETTINGS_KEYS` = {"buy_min_score", "sell_min_score", ...}
  · `review_agent._V182_PARAM_KEYS`     = {"signal_threshold", "min_event_strength", ...}

两边从不相等 → `POST /api/paper/adjustments/{id}/approve` 必然抛
`HTTPException(400, "unknown param_key")`。

实测：`strategy_param_adjustments` 里 98 条 pending，`param_key like '%.%'`
命中 98/98（100%）。也就是说「反思 → 参数落地」这一环
**不是没人点，是点了必失败**，停摆 45 天。

本模块只做一件事：把带前缀的 key 解析到审批侧承认的 canonical key，
并明确标出「语义不等价、不能自动应用」的 key（宁可不做，也不能乱改交易参数）。

分桶
----
settings   → 写 paper_trading_settings（用户运行态参数，不受版本频率限制）
strategy   → 写 strategy_versions.params（受「每日 1 个 reflection 版本」限制）
unmappable → 无等价参数，必须人工处理（返回原因）
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ── settings 桶：paper_trading_settings 有对应列，语义完全等价 ──────────────
SETTINGS_ALIASES: Dict[str, str] = {
    "score.buy_min_score": "buy_min_score",
    "score.sell_min_score": "sell_min_score",
    "trading.buy_min_score": "buy_min_score",
    "trading.sell_min_score": "sell_min_score",
    # v409.5: settings 已补 max_position_pct 列，且 quick_sim 已把它当**真实上限**用
    # （equity × pct 封顶单笔名义），所以这个键从 unmappable 转正。
    "sizing.max_position_pct": "max_position_pct",
}

# ── strategy 桶：写 strategy_versions.params，语义近似但可接受 ────────────────
# confidence_threshold 与 signal_threshold 都是「评分门槛」，同一决策位；
# event_strength_threshold 与 min_event_strength 同理。
STRATEGY_ALIASES: Dict[str, str] = {
    "score.signal_threshold": "signal_threshold",
    "score.confidence_threshold": "signal_threshold",
    "score.event_strength_threshold": "min_event_strength",
    "score.min_event_strength": "min_event_strength",
    "sizing.holding_horizon_minutes": "holding_horizon_minutes",
    "sizing.notional_usdt": "notional_usdt",
    "sizing.leverage": "leverage",
}

# ── 不能自动应用：语义不等价，改了会直接污染交易参数 ────────────────────────
# v409: 下面四个 sizing 键**不是绝对不能落地**，而是"缺上下文就不能落地"——
# 有了账户权益和当时的 ATR% 就能换算，没有就只能标 unmappable 并写清缺什么。
# 换算实现见 SIZING_CONVERTERS / convert_value()；resolve_key() 保持旧行为（不给上下文 = unmappable），
# 免得任何老调用方在没上下文的情况下悄悄算出错误数值。
UNMAPPABLE: Dict[str, str] = {
    "sizing.position_weight": "仓位权重(比例) 需要账户权益才能换成 notional_usdt",
    "sizing.stop_loss_pct": "止损百分比需要当时 ATR%（才能换成 sl_mult 倍数）",
    "sizing.take_profit_pct": "止盈百分比需要当时 ATR%（才能换成 tp1_mult 倍数）",
    "filter.asset_match.main_asset_missed": "这是过滤器命中统计，不是可写参数",
    "filter.contradiction_check.sentiment_vs_market": "这是过滤器命中统计，不是可写参数",
}


def resolve_key(raw_key: str) -> Tuple[Optional[str], str, str]:
    """把任意写法的 param_key 解析成 (canonical_key, bucket, reason)。

    bucket ∈ {"settings", "strategy", "unmappable", "literal"}

    · literal   —— 本来就是裸 key 且在白名单里，原样返回（兼容历史合法数据）
    · unmappable —— canonical 为 None，reason 说明为什么不自动应用
    """
    raw = (raw_key or "").strip()
    if not raw:
        return None, "unmappable", "param_key 为空"

    if raw in SETTINGS_ALIASES:
        return SETTINGS_ALIASES[raw], "settings", f"{raw} → {SETTINGS_ALIASES[raw]}（settings 桶）"

    if raw in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[raw], "strategy", f"{raw} → {STRATEGY_ALIASES[raw]}（策略参数桶）"

    if raw in UNMAPPABLE:
        return None, "unmappable", UNMAPPABLE[raw]

    # 裸 key：交给调用方自己的白名单再校验一次，这里只判「有没有命名空间前缀」
    if "." in raw:
        return None, "unmappable", f"{raw} 带命名空间前缀但无登记别名，拒绝自动应用"

    return raw, "literal", f"{raw} 原样使用"


def is_mappable(raw_key: str) -> bool:
    """能否自动落到某个具体参数上（unmappable 一律 False）。"""
    canon, bucket, _ = resolve_key(raw_key)
    return bool(canon) and bucket in ("settings", "strategy", "literal")


# ── v409: 带上下文的单位换算 ──────────────────────────────────────────────────
# 设计原则：**没有上下文就拒绝**，绝不假设一个权益或 ATR 去凑数。
# 每个转换器声明自己需要哪些字段，缺一个就返回 None + 缺什么。
SIZING_REQUIRED_CONTEXT = {
    "sizing.position_weight": ("equity_usdt",),
    "sizing.stop_loss_pct": ("atr_pct",),
    "sizing.take_profit_pct": ("atr_pct",),
}


def convert_value(raw_key: str, value, context: Optional[Dict[str, float]] = None
                  ) -> Tuple[Optional[str], Optional[float], str, str]:
    """把 sizing 类的百分比/比例换成系统真正的参数单位。

    → (canonical_key, 换算后的值, bucket, 说明)。换算不了时为 (None, None, 'unmappable', 原因)。

    单位关系（写明在 docstring 里，避免下一个人再猜）：
      · 止损百分比 → sl_mult：sl_mult = stop_loss_pct / atr_pct
        （ATR% 是"1 倍 ATR 占价格的百分比"，所以百分比除以 ATR% 得到倍数）
      · 止盈百分比 → tp1_mult：同上
      · 仓位比例 → notional_usdt：notional = equity_usdt × weight
    """
    raw = (raw_key or "").strip()
    ctx = dict(context or {})
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, None, "unmappable", f"{raw} 的值不是数字（{value!r}），拒绝换算"

    if raw in ("sizing.stop_loss_pct", "sizing.take_profit_pct"):
        atr = ctx.get("atr_pct")
        if atr is None or float(atr) <= 0:
            return (None, None, "unmappable",
                    f"{raw} 需要当时 ATR%（context.atr_pct），缺失或为 0，拒绝换算")
        target = "sl_mult" if raw == "sizing.stop_loss_pct" else "tp1_mult"
        mult = round(abs(v) / abs(float(atr)), 4)
        return target, mult, "strategy", (
            f"{raw} {v}% ÷ ATR% {float(atr):.3f} = {mult}× → {target}")

    if raw == "sizing.position_weight":
        eq = ctx.get("equity_usdt")
        if eq is None or float(eq) <= 0:
            return (None, None, "unmappable",
                    f"{raw} 需要账户权益（context.equity_usdt），缺失或为 0，拒绝换算")
        notional = round(abs(float(eq)) * abs(v), 2)
        return "notional_usdt", notional, "strategy", (
            f"{raw} {v:.4f} × 权益 {float(eq):.2f} = {notional} USDT → notional_usdt")

    # 其余键交给 resolve_key 的静态别名
    canon, bucket, why = resolve_key(raw)
    if not canon:
        return None, None, bucket, why
    return canon, v, bucket, why


def missing_context(raw_key: str, context: Optional[Dict[str, float]] = None) -> Tuple[str, ...]:
    """这个 key 换算还缺哪些上下文字段（空元组 = 不缺 / 不需要换算）。"""
    need = SIZING_REQUIRED_CONTEXT.get((raw_key or "").strip(), ())
    ctx = context or {}
    return tuple(k for k in need if ctx.get(k) in (None, ""))


# ── 参数类型层：direction 的语义必须与参数类型匹配 ───────────────────────────
# 真实踩过的坑（2026-11-14）：策略反思建议「提高 min_event_strength」，
# 以 direction='up' magnitude=0.1 落地时被算成浮点 0.73 写进 params，
# strategy_store.clamp_params 发现 0.73 不在 {low,medium,high} → 回退成空串 ''，
# 等于**把强度门槛彻底关掉**。这比不改更坏，且状态照样记 applied。
BOOL_PARAMS = {
    "tp_enabled", "gate_enabled", "market_structure_enabled", "require_direct_catalyst",
}
ENUM_PARAMS = {
    "min_event_strength": ("low", "medium", "high"),
    "asset_filter": None,  # 资产白名单，值域由 ALLOWED_ASSETS 决定，仍只接受 set
}


def check_direction(param_key: str, direction: str, magnitude: float,
                    target_value: Any = None) -> Tuple[bool, str]:
    """校验 direction 是否适用于该参数类型。

    返回 (ok, reason)。ok=False 时调用方必须跳过并把这句 reason 展示出来，
    不能静默写一个"看起来生效"的版本。
    """
    d = (direction or "").strip().lower()
    if param_key in BOOL_PARAMS:
        if d in ("on", "off"):
            return True, ""
        return False, f"{param_key} 是布尔开关，只接受 on/off，收到 '{d}'"
    if param_key in ENUM_PARAMS:
        if d != "set":
            return False, (f"{param_key} 是枚举参数，只接受 set + 具体取值"
                           f"（如 {'/'.join(ENUM_PARAMS[param_key] or ['XAU'])}），"
                           f"收到的 '{d} {magnitude}' 会把它写成非法值并被清空")
        # v339: set 本身必须带合法取值。原实现对 set 直接返回 True，
        # 于是 target 是任意串也能"通过校验"，再被 clamp_params 清空 —— 换成
        # 另一种形态的假通过。这里把值域真的校验一遍。
        allowed: Tuple[str, ...] = ENUM_PARAMS[param_key] or ()
        if not allowed:
            try:
                from strategy_store import ALLOWED_ASSETS as _ASSETS
                allowed = tuple(sorted(a for a in _ASSETS if a))
            except Exception:
                allowed = ()
        tv = "" if target_value is None else str(target_value).strip()
        if not tv:
            return False, (f"{param_key} 用 set 必须给出 target_value"
                           f"（可选: {'/'.join(allowed) or '见配置'}）")
        if allowed and tv not in allowed:
            return False, (f"{param_key} 不接受目标值 '{tv}'，"
                           f"可选: {'/'.join(allowed)}")
        return True, ""
    if d in ("on", "off"):
        return False, f"{param_key} 是数值参数，on/off 会被写成 1/0，语义错误"
    if d == "set":
        # v339: 数值参数也允许 set。`signal_threshold` 基线是 0.0，
        # `0 × (1±m)` 永远是 0，任何 up/down 都注定是空操作 —— 唯一能动的
        # 办法就是直接给绝对值。set 只读 target_value、不读 magnitude，
        # 所以不会与 up/down 的比例语义打架。
        try:
            num = float(target_value)
        except (TypeError, ValueError):
            return False, (f"{param_key} 是数值参数，用 set 必须给数值型 target_value"
                           f"（收到 {target_value!r}）")
        if num != num or num in (float("inf"), float("-inf")):
            return False, f"{param_key} 的 target_value 不是有限数"
        return True, ""
    if d not in ("up", "down"):
        return False, f"未知 direction '{d}'"
    return True, ""


def alias_table() -> Dict[str, Dict[str, str]]:
    """给前端/诊断页展示的完整映射表。"""
    return {
        "settings": dict(SETTINGS_ALIASES),
        "strategy": dict(STRATEGY_ALIASES),
        "unmappable": dict(UNMAPPABLE),
    }
