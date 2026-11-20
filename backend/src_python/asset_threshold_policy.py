#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""品种级阈值政策（v341）。

来源：`run_backtest.py --sweep` 产出的 `runtime/backtest/threshold_map_*.json`。
那里每个品种都有实测判定：

  use        净超额 ≥ 0.2%（扣 12bps 往返成本后）且样本 ≥ 30 → 给出适用阈值
  weak       净超额在 0~0.2% 之间 → 能做但太薄，阈值按最保守处理
  blocked    最优阈值扣成本仍为负 → 该品种追涨无价值，阈值取更严的一档
  insufficient 样本不够 → 不给政策，回落原有自适应阈值

这批结论是"日线 / 5 日前瞻 / 已扣成本"的口径，所以：
  · 只用来收紧**检测**阈值（少产生没价值的噪声信号），
  · 绝不放松：最终阈值 = max(政策阈值, 3% × 波动率自适应 scale)。

开关：环境变量 `TRIDENT_THRESH_POLICY=off` 时本模块返回 None，
系统行为完全回到 v341 之前（逐点波动率自适应）。
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any

MAP_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runtime", "backtest", "threshold_map_*.json")
_cache: dict[str, Any] = {"loaded": False, "map": None, "path": None}


def enabled() -> bool:
    return os.environ.get("TRIDENT_THRESH_POLICY", "on").strip().lower() not in ("off", "0", "false")


def load_map(path: str | None = None) -> dict[str, Any] | None:
    """读最近一次的阈值矩阵；读不到返回 None（调用方回落到旧口径）。"""
    if _cache["loaded"] and not path:
        return _cache["map"]
    target = path
    if not target:
        files = sorted(glob.glob(MAP_GLOB))
        if not files:
            _cache.update({"loaded": True, "map": None, "path": None})
            return None
        target = files[-1]
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        _cache.update({"loaded": True, "map": None, "path": None})
        return None
    _cache.update({"loaded": True, "map": data, "path": target})
    return data


def _canon(asset: str) -> str:
    try:
        import asset_alias
        return asset_alias.canonical(asset)
    except Exception:
        return (asset or "").strip().upper()


def policy(asset: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """返回该品种的阈值政策；None 表示不干预（走自适应）。"""
    if not enabled():
        return None
    if data is None:
        data = load_map()
    if not data:
        return None
    a = _canon(asset)
    entry = (data.get("assets") or {}).get(a)
    if not entry:
        return None
    pick = entry.get("pick") or {}
    verdict = pick.get("verdict")
    if verdict == "insufficient" or "thresh" not in pick:
        return None
    return {
        "asset": a,
        "verdict": verdict,
        "thresh": float(pick["thresh"]),
        "net_edge_pct": pick.get("net_edge_pct"),
        "winrate": pick.get("winrate"),
        "n": pick.get("n"),
        "source": _cache.get("path"),
    }


def describe(asset: str) -> str:
    p = policy(asset)
    if not p:
        return "无政策（走波动率自适应）"
    return (f"{p['verdict']} · 阈值 {p['thresh']:g}% · "
            f"净超额 {p['net_edge_pct']:+.2f}% · n={p['n']}")


def clear_cache() -> None:
    _cache.update({"loaded": False, "map": None, "path": None})
