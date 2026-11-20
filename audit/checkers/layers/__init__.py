"""7 层审计边界（物理拆分后的注册表）。

层的名字与顺序在这里集中定义；每层实现在同目录下一个独立模块里
（data / logic / rules / causal / market / event / reasoning），
层内实现仍委托内核，避免"拆文件"变成"改逻辑"。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...core.context import CheckContext
from ...core.report import LayerResult
from . import causal, data, event, logic, market, reasoning, rules
from ._base import LayerSpec

MODULES = (data, logic, rules, causal, market, event, reasoning)

LAYER_SPECS = tuple(m.SPEC for m in MODULES)
LAYER_LABEL = {m.SPEC.name: m.SPEC.label for m in MODULES}


def run_layer(name: str, decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    for m in MODULES:
        if m.SPEC.name == name:
            return m.run(decision, ctx)
    raise KeyError(f"未知层 {name}，可选：{[s.name for s in LAYER_SPECS]}")


def run_all(decision: Dict[str, Any], ctx: CheckContext) -> List[LayerResult]:
    return [m.run(decision, ctx) for m in MODULES]


__all__ = [
    "MODULES",
    "LAYER_SPECS",
    "LAYER_LABEL",
    "LayerSpec",
    "run_layer",
    "run_all",
]
