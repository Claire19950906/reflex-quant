#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用熔断器（Circuit Breaker）—— 连续失败自动熔断，避免反复打必失败请求。

背景（金十 token 被烧的教训）：原 5s x 2 页轮询对同一必失败请求反复打，
免费配额快速耗尽。根因不是轮询快，而是「没有失败计数 → 没有熔断 → 没有冷却」。

三态流转：
    closed ──连续失败 N 次──> open（拒绝请求，冷却 open_seconds 秒）
    open   ──冷却结束──────> half_open（放行一次探测）
    half_open ──探测成功──> closed（清零）
    half_open ──探测失败──> open（立即重新熔断，重新计时）

进程内内存态（单进程 watcher 足够）；跨进程持久化暂不引入，避免复杂度。
纯 stdlib，无副作用，任何数据源 fetch 前调用 ``allow``，成功/失败回报即可。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional


class CircuitBreaker:
    """按 key（数据源/资产）独立计数的熔断器。"""

    def __init__(self, name: str, fail_threshold: int = 3,
                 open_seconds: float = 60.0) -> None:
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold))
        self.open_seconds = max(0.001, float(open_seconds))
        self._fail = 0
        self._opened_at = 0.0
        self._state = "closed"
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """请求前调用：True 可发起，False 已熔断（应跳过本轮并降级）。"""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.open_seconds:
                    self._state = "half_open"
                    return True
                return False
            # half_open：只放行一次探测请求
            return True

    def success(self) -> None:
        """请求成功回执：清零并闭合。"""
        with self._lock:
            self._fail = 0
            self._state = "closed"
            self._opened_at = 0.0

    def failure(self) -> None:
        """请求失败回执：累计；达阈值熔断 / half-open 探测失败立即重新熔断。"""
        with self._lock:
            self._fail += 1
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                return
            if self._fail >= self.fail_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "fail_count": self._fail,
                "fail_threshold": self.fail_threshold,
                "open_seconds": self.open_seconds,
                "opened_at": round(self._opened_at, 3),
            }
# ---------------------------------------------------------------------------
# 全局按 key 注册表（进程内共享，避免每个 fetch 各自维护一个 breaker）
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get(name: str, fail_threshold: int = 3,
        open_seconds: float = 60.0) -> CircuitBreaker:
    """按 key 取（不存在则创建）熔断器。"""
    key = str(name or "").strip() or "_default"
    with _REGISTRY_LOCK:
        brk = _REGISTRY.get(key)
        if brk is None:
            brk = CircuitBreaker(key, fail_threshold=fail_threshold,
                                 open_seconds=open_seconds)
            _REGISTRY[key] = brk
        return brk


def report_all() -> Dict[str, Dict[str, object]]:
    """导出全部熔断器状态（供 /api/sources/status 之类面板展示）。"""
    with _REGISTRY_LOCK:
        return {k: b.status() for k, b in _REGISTRY.items()}


def reset(name: Optional[str] = None) -> None:
    """手动复位：name=None 复位全部。"""
    with _REGISTRY_LOCK:
        if name is None:
            _REGISTRY.clear()
        else:
            _REGISTRY.pop(str(name), None)


if __name__ == "__main__":
    b = CircuitBreaker("jin10", fail_threshold=2, open_seconds=0.2)
    print("初始:", b.status())
    assert b.allow() is True
    b.failure()
    b.failure()  # 达到阈值 → open
    print("连续失败后:", b.status())
    assert b.state == "open"
    assert b.allow() is False  # 熔断中拒绝
    time.sleep(0.25)  # 冷却结束
    assert b.allow() is True  # half-open 放行一次探测
    b.success()
    print("探测成功复位:", b.status())
    assert b.state == "closed"

    # half-open 探测失败 → 立即重新熔断
    b.failure(); b.failure()
    time.sleep(0.25)
    assert b.allow() is True
    b.failure()
    print("探测失败重新熔断:", b.status())
    assert b.state == "open"

    # 全局注册表
    get("yahoo_finance").failure()
    print("注册表:", report_all())
    print("熔断器自测通过 ✔")