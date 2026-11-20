#!/usr/bin/env python3
"""
Trident Agent MVP -- Risk Manager (MVP)

最小风控：
  - 白名单品种过滤（WHITELIST_ASSETS='*' 表示全部）
  - 单笔名义 USDT 上限（MAX_POSITION_USDT，默认 5000）
  - 黑名单 action（永远禁止）
  - 账户级别 kill switch（一键熔断，全部拒绝）

输入：asset / action / size_usdt / ctx
输出：RiskDecision(allow: bool, max_allowed_size: float, reason: str)

不依赖 exchange 实时账户（账户检查由 adapter 在下单前完成）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence

_ALWAYS_REJECT = ("HOLD", "")


@dataclass(frozen=True)
class RiskDecision:
    allow: bool
    max_allowed_size: float
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "max_allowed_size": self.max_allowed_size,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class RiskManager:
    def __init__(self, *,
                 whitelist: Sequence[str] = ("*",),
                 max_position_usdt: float = 5000.0,
                 max_open_positions: int = 5,
                 max_actions_per_asset: int = 3) -> None:
        self.whitelist = tuple(a.upper() for a in whitelist)
        self.max_position_usdt = float(max_position_usdt)
        self.max_open_positions = int(max_open_positions)
        self.max_actions_per_asset = int(max_actions_per_asset)
        self._kill_switch = False
        self._open_position_count = 0
        self._per_asset_count: Dict[str, int] = {}
        self._log = logging.getLogger("risk_manager")

    def set_kill_switch(self, on: bool) -> None:
        self._kill_switch = bool(on)
        self._log.warning("kill_switch -> %s", on)

    def kill_switch_active(self) -> bool:
        return self._kill_switch

    def on_position_opened(self, asset: str) -> None:
        self._open_position_count += 1
        self._per_asset_count[asset.upper()] = self._per_asset_count.get(asset.upper(), 0) + 1

    def on_position_closed(self, asset: str) -> None:
        self._open_position_count = max(0, self._open_position_count - 1)
        cur = self._per_asset_count.get(asset.upper(), 0)
        if cur <= 1:
            self._per_asset_count.pop(asset.upper(), None)
        else:
            self._per_asset_count[asset.upper()] = cur - 1

    def reset_counters(self) -> None:
        self._open_position_count = 0
        self._per_asset_count.clear()

    def evaluate(self, asset: str, action: str,
                 size_usdt: float, ctx: Optional[Dict[str, Any]] = None) -> RiskDecision:
        ctx = ctx or {}
        asset_u = (asset or "").upper()
        action_u = (action or "").upper()

        if self._kill_switch:
            return RiskDecision(False, 0.0, reason="kill_switch_active")

        if action_u in _ALWAYS_REJECT:
            return RiskDecision(False, 0.0, reason=f"action={action_u}_not_executable")

        if action_u not in ("BUY", "SELL"):
            return RiskDecision(False, 0.0, reason=f"unknown_action={action_u}")

        if not asset_u or asset_u in ("N/A", "NONE", "NULL"):
            return RiskDecision(False, 0.0, reason="empty_asset")

        if not self._asset_allowed(asset_u):
            return RiskDecision(False, 0.0, reason=f"asset_not_whitelisted:{asset_u}")

        if size_usdt <= 0:
            return RiskDecision(False, 0.0, reason="non_positive_size")

        max_allowed = float(self.max_position_usdt)
        if size_usdt > max_allowed:
            return RiskDecision(
                True, max_allowed,
                reason=f"size_capped_to_max={max_allowed}",
                metadata={"requested": size_usdt},
            )

        if self._open_position_count >= self.max_open_positions:
            return RiskDecision(False, 0.0,
                                reason=f"max_open_positions={self.max_open_positions}")

        if self._per_asset_count.get(asset_u, 0) >= self.max_actions_per_asset:
            return RiskDecision(False, 0.0,
                                reason=f"max_actions_per_asset={self.max_actions_per_asset}")

        return RiskDecision(True, float(size_usdt), reason="ok",
                            metadata={"asset": asset_u, "action": action_u})

    def _asset_allowed(self, asset_u: str) -> bool:
        if not self.whitelist:
            return False
        if "*" in self.whitelist:
            return True
        return asset_u in self.whitelist

    def snapshot(self) -> Dict[str, Any]:
        return {
            "whitelist": list(self.whitelist),
            "max_position_usdt": self.max_position_usdt,
            "max_open_positions": self.max_open_positions,
            "max_actions_per_asset": self.max_actions_per_asset,
            "kill_switch": self._kill_switch,
            "open_position_count": self._open_position_count,
            "per_asset_count": dict(self._per_asset_count),
        }


def parse_whitelist(raw: str) -> Iterable[str]:
    if not raw:
        return ("*",)
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else ("*",)


__all__ = ["RiskManager", "RiskDecision", "parse_whitelist"]