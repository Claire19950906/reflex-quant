# -*- coding: utf-8 -*-
"""功能注册表 —— 「一张表管 64 项」的落点。

每个功能函数签名：``fn(ctx, **kwargs) -> FeatureResult``。
kwargs 只给交互类功能用（如 F05 的剧本选择、F17 的改单指令），批量巡检时为空。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .catalog import BY_ID, FeatureSpec, FEATURES

FeatureFn = Callable[..., "object"]

_REGISTRY: Dict[str, FeatureFn] = {}
_LOADED = False

_MODULES = (
    "trident2.features.health",
    "trident2.features.risk",
    "trident2.features.monitor",
    "trident2.features.monitor2",
    "trident2.features.research",
    "trident2.features.behavior",
    "trident2.features.money",
    "trident2.features.trust",
)


def feature(feature_id: str) -> Callable[[FeatureFn], FeatureFn]:
    if feature_id not in BY_ID:
        raise ValueError(f"unknown feature id: {feature_id}")

    def deco(fn: FeatureFn) -> FeatureFn:
        if feature_id in _REGISTRY:
            raise ValueError(f"duplicate feature implementation: {feature_id}")
        _REGISTRY[feature_id] = fn
        return fn

    return deco


def load_all() -> None:
    """导入所有功能模块，触发装饰器注册（幂等）。

    单个模块导入失败不影响其它模块：失败的功能会以「未实现」出现在目录里，
    而不是让整个 API 500。
    """
    global _LOADED
    if _LOADED:
        return
    import importlib
    import logging

    log = logging.getLogger("trident2.registry")
    for m in _MODULES:
        try:
            importlib.import_module(m)
        except Exception as exc:
            log.warning("feature module %s failed to load: %s", m, exc)
    _LOADED = True


def get(feature_id: str) -> Optional[FeatureFn]:
    load_all()
    return _REGISTRY.get(feature_id)


def implemented() -> Dict[str, FeatureFn]:
    load_all()
    return dict(_REGISTRY)


def missing() -> List[str]:
    load_all()
    return [f.id for f in FEATURES if f.id not in _REGISTRY]


def spec(feature_id: str) -> Optional[FeatureSpec]:
    return BY_ID.get(feature_id)
