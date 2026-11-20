# -*- coding: utf-8 -*-
"""证据就绪检查：调用模型之前，先确认「有东西可看」。

背景（2026-11-16 实测）
-----------------------
482 条决策里 `hard_data_status=ok` 只有 104 条（21.6%），`missing:feed_stale`
185 条；`market_confirmation=unknown` 478 条（99.2%）。也就是说，绝大多数
「七段推演」是在没有盘面结构证据的情况下生成的 —— 模型只能回 unknown，
结构因子（权重 0.15）恒零，方向分自然回落到 0。

本模块把「有没有证据」变成显式前置检查，返回三件事：
  1. ready —— 能不能调用模型；
  2. code  —— 不能调用的原因（与 decision_provenance.hard_status 同一套词）；
  3. detail —— 人读的缺口说明，写进 hard_data_evidence / 日志。

注意：不抓数据、不补造。缺就是缺，宁可少分析一条，也不要生成一条
「看起来完整但没有证据」的决策。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import CORE_ASSETS, _norm, scope_of

# 快照最大允许年龄（秒）。加密 7×24 用短窗口，商品/黄金有交易时段用长窗口。
MAX_AGE_S = 600
MAX_AGE_BY_SCOPE = {
    "core": 600,
    "adjacent": 900,
    "unknown": 600,
}

# 这些品种必须带本地源；个股/桶标签没有本地源，属于不分析的对象
_LOCAL_SYMBOL_ASSETS = CORE_ASSETS | {"BRENT", "DXY", "US10Y", "VIX", "SPX", "NDX", "CN_INDEX"}


@dataclass(frozen=True)
class Readiness:
    ready: bool
    code: str      # ok / missing:none_target / missing:no_local_feed / missing:feed_stale / missing:no_kline
    detail: str

    @property
    def hard_status(self) -> str:
        """可直接写入 ai_decisions.hard_data_status 的标签。"""
        return self.code


def max_age_for(asset) -> int:
    return MAX_AGE_BY_SCOPE.get(scope_of(asset), MAX_AGE_S)


def check(*, asset, snapshot_age_s=None, has_local_symbol=None, kline_ok=True,
          max_age_s=None) -> Readiness:
    """判断证据是否就绪（纯函数，参数由调用方从 DB/快照里查好）。

    Args:
        asset:             目标品种
        snapshot_age_s:    最新快照距今多少秒（None = 没有快照）
        has_local_symbol:  本地是否有该品种的行情源（None = 按品种清单推断）
        kline_ok:          K 线是否覆盖了决策所需的主导周期
        max_age_s:         覆盖默认年龄上限
    """
    a = _norm(asset)
    scope = scope_of(a)

    if scope == "none":
        return Readiness(False, "missing:none_target", "没有可分析标的")

    if has_local_symbol is None:
        has_local_symbol = a in _LOCAL_SYMBOL_ASSETS
    if not has_local_symbol:
        return Readiness(False, "missing:no_local_feed",
                         f"{a or '该品种'} 无本地行情源（个股/桶标签不分析）")

    limit = int(max_age_s if max_age_s is not None else max_age_for(a))
    if snapshot_age_s is None:
        return Readiness(False, "missing:feed_stale", f"{a} 无可用快照")
    try:
        age = float(snapshot_age_s)
    except (TypeError, ValueError):
        return Readiness(False, "missing:feed_stale", f"{a} 快照年龄不可解析")

    if age < 0:
        return Readiness(False, "missing:feed_stale", f"{a} 快照时间在未来（时钟/时区异常）")
    if age > limit:
        return Readiness(False, "missing:feed_stale",
                         f"{a} 快照已过期 {int(age)}s > {limit}s")

    if not kline_ok:
        return Readiness(False, "missing:no_kline", f"{a} 缺决策周期 K 线")

    return Readiness(True, "ok", f"{a} 快照 {int(age)}s（≤{limit}s）")
