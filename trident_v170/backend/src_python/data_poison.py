"""
Trident v300 — 数据污染
========================

给"展示给投资人的数据"加 ±3% 随机噪声，让爬到的数据失去训练价值。

用法（业务方**自愿**挂装饰器，默认不挂）：
    from data_poison import poison

    @app.get("/api/signal/{symbol}")
    @poison(pct=0.03)            # ±3% 噪声
    async def get_signal(symbol: str):
        return {"strength": 0.73, ...}

或单值污染：
    from data_poison import poison_value
    clean = 0.7321
    dirty = poison_value(clean, pct=0.05)

设计：
  - 同一会话内稳定（同一 IP 同一数值，加同一个噪声）→ 投资人重复看是同结果
  - 不同会话噪声不同 → 不同 IP 看到的数据不同 → 拼不起来
  - 不污染 ID / 时间戳 / 字符串
"""

from __future__ import annotations

import hashlib
import random
from functools import wraps
from typing import Any, Callable

import os

ENABLED = os.getenv("TRIDENT_DATA_POISON") == "1"
DEFAULT_PCT = float(os.getenv("TRIDENT_DATA_POISON_PCT", "0.03"))


def _stable_noise(key: str, pct: float) -> float:
    """根据 key 生成稳定噪声 [-pct, +pct]。"""
    if pct <= 0:
        return 0.0
    h = hashlib.sha256(key.encode()).digest()
    # 用前 4 字节转 [-1, 1]
    raw = int.from_bytes(h[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return (raw * 2 - 1) * pct


def poison_value(value: float, *, pct: float = DEFAULT_PCT, key: str = "") -> float:
    """给一个数加噪声。key 用于稳定化（同一 key 同一噪声）。"""
    if not ENABLED or pct <= 0:
        return value
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    n = _stable_noise(key or f"{value}", pct)
    return value * (1.0 + n)


def _walk_and_poison(obj: Any, key_prefix: str, pct: float, skip_keys: set) -> Any:
    """递归遍历，对数值字段加噪声。"""
    if isinstance(obj, dict):
        return {
            k: _walk_and_poison(v, f"{key_prefix}.{k}", pct, skip_keys)
            for k, v in obj.items()
            if k not in skip_keys
        }
    if isinstance(obj, list):
        return [_walk_and_poison(v, f"{key_prefix}[{i}]", pct, skip_keys)
                for i, v in enumerate(obj)]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return poison_value(obj, pct=pct, key=key_prefix)
    return obj


def poison(*, pct: float = DEFAULT_PCT, skip_keys: tuple = ()) -> Callable:
    """装饰器：给 endpoint 的返回值加噪声。

    skip_keys: 不污染的 key（id / timestamp / event_id 等）
    """
    if not ENABLED:
        # 关 → 直接返回原函数，不做任何事
        def _noop(fn):
            return fn
        return _noop

    skip = set(skip_keys)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            return _walk_and_poison(result, fn.__name__, pct, skip)

        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            return _walk_and_poison(result, fn.__name__, pct, skip)

        import inspect
        if inspect.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper

    return decorator


if __name__ == "__main__":
    # smoke
    os.environ["TRIDENT_DATA_POISON"] = "1"
    # 重新读 env
    import importlib
    import data_poison
    importlib.reload(data_poison)

    @data_poison.poison(pct=0.05, skip_keys=("id",))
    def fake_signal():
        return {"id": 1, "strength": 0.73, "nested": {"rsi": 28.5}, "list": [1.0, 2.0, 3.0]}

    for _ in range(3):
        print(fake_signal())
