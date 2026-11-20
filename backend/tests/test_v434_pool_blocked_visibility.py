#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v434（v413 P0-A1）：熔断干等期间必须可见 —— 轮次 / 已等待 / 最近可恢复倒计时。

背景（2026-11-17 实测）
----------------------
主循环停在 pool-blocked 11,400s 时，`runtime/ai_worker_progress.json` 里
`step` 恒为 "pool-blocked"，日志只有一句「跳过认领：全部通道熔断」——
运维分不清是 quota（冷却 30 分钟会自愈）还是 auth（24 小时，等也没用），
也看不出已经等了几轮。这一轮只加**只读**观测，不改任何决策行为：

  · `_log_progress(step, **extra)` 支持附加字段；step 名保持 "pool-blocked"
    不变（看门狗 `POOL_BLOCKED_STEP` 是精确匹配，改名会让 6 小时宽线失效）；
  · 新增 `pool_recovery_eta()`：按熔断器放行时刻算出「最近可恢复」剩余秒数。

红线
----
`pool_recovery_eta()` **只能读** `state` / `_wait_until` / `probe_timeout`，
不许调用 `allow()` —— 那会占掉 half_open 唯一的探测名额，把一条额度正常的
通道卡死 30 分钟（v409.2 事故，见 tests/test_v409_readiness_no_probe_consume.py）。
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import engine.ai_worker as aw
import engine_watchdog as _wd


class _FakeBreaker:
    """只提供 pool_recovery_eta 会读的属性；allow() 被调用即计数。"""

    def __init__(self, state="closed", wait_until=0.0,
                 probe_in_flight=False, probe_at=0.0, probe_timeout=1800.0):
        self.state = state
        self._wait_until = wait_until
        self._probe_in_flight = probe_in_flight
        self._probe_at = probe_at
        self.probe_timeout = probe_timeout
        self.allow_calls = 0

    def allow(self):  # 被调用就是回归 —— 见模块 docstring 的红线
        self.allow_calls += 1
        return True


def _pool_with(*aliases):
    return [{"key_alias": a, "id": a, "purpose": ["main"]} for a in aliases]


@pytest.fixture
def fake_breakers(monkeypatch):
    """按 (alias, kind) 注入熔断器；未登记的 kind 返回 None（视为不可用）。"""
    table = {}

    def _getter(alias, kind):
        return table.get((alias, kind))

    monkeypatch.setattr(aw, "_analysis_breaker", _getter)
    return table


def test_open_breaker_reports_remaining_seconds(fake_breakers):
    """一条通道 quota 熔断、其余类别正常 → 返回该放行时刻的剩余秒数。"""
    now = time.time()
    fake_breakers[("mm1", "quota")] = _FakeBreaker(
        state="open", wait_until=now + 120)
    fake_breakers[("mm1", "net")] = _FakeBreaker(state="closed")
    fake_breakers[("mm1", "auth")] = _FakeBreaker(state="closed")

    eta = aw.pool_recovery_eta(_pool_with("mm1"))
    assert eta is not None
    assert 110 <= eta <= 125


def test_healthy_channel_wins_over_blocked_one(fake_breakers):
    """池内只要有一条通道健康，最近可恢复就是 0（应该立刻认领）。"""
    now = time.time()
    fake_breakers[("mm1", "quota")] = _FakeBreaker(
        state="open", wait_until=now + 1800)
    fake_breakers[("mm1", "net")] = _FakeBreaker(state="closed")
    fake_breakers[("mm2", "quota")] = _FakeBreaker(state="closed")
    fake_breakers[("mm2", "net")] = _FakeBreaker(state="closed")

    assert aw.pool_recovery_eta(_pool_with("mm1", "mm2")) == 0.0


def test_earliest_across_channels(fake_breakers):
    """两条通道都熔断 → 取**最快**恢复的那条，而不是最慢的。"""
    now = time.time()
    fake_breakers[("mm1", "quota")] = _FakeBreaker(
        state="open", wait_until=now + 1800)
    fake_breakers[("mm1", "net")] = _FakeBreaker(state="closed")
    fake_breakers[("mm2", "quota")] = _FakeBreaker(
        state="open", wait_until=now + 90)
    fake_breakers[("mm2", "net")] = _FakeBreaker(state="closed")

    eta = aw.pool_recovery_eta(_pool_with("mm1", "mm2"))
    assert 80 <= eta <= 95


def test_half_open_probe_in_flight_uses_probe_timeout(fake_breakers):
    """探测名额被占死（v409.2 形态）→ 按 probe_timeout 估算最坏等待。"""
    now = time.time()
    fake_breakers[("mm1", "quota")] = _FakeBreaker(
        state="half_open", probe_in_flight=True,
        probe_at=now - 60, probe_timeout=1800)
    fake_breakers[("mm1", "net")] = _FakeBreaker(state="closed")

    eta = aw.pool_recovery_eta(_pool_with("mm1"))
    assert eta is not None
    assert 1700 <= eta <= 1800


def test_no_breakers_returns_none(fake_breakers):
    """熔断器全取不到 → 不编一个数字出来，返回 None。"""
    assert aw.pool_recovery_eta(_pool_with("mm1")) is None


def test_empty_pool_returns_none(fake_breakers):
    assert aw.pool_recovery_eta([]) is None


def test_never_calls_allow(fake_breakers):
    """回归 v409.2：算倒计时不许调用 allow()（会占掉探测名额）。"""
    now = time.time()
    quota = _FakeBreaker(state="open", wait_until=now + 300)
    net = _FakeBreaker(state="closed")
    fake_breakers[("mm1", "quota")] = quota
    fake_breakers[("mm1", "net")] = net

    aw.pool_recovery_eta(_pool_with("mm1"))
    assert quota.allow_calls == 0
    assert net.allow_calls == 0


def test_step_name_unchanged_for_watchdog():
    """step 名必须还是看门狗精确匹配的那个字面量，否则 6 小时宽线失效。"""
    assert _wd.POOL_BLOCKED_STEP == "pool-blocked"
    with open(aw.__file__, encoding="utf-8") as f:
        src = f.read()
    assert '_log_progress(\n                "pool-blocked",' in src
    for field in ("pool_blocked_round", "pool_blocked_elapsed_s",
                  "pool_next_recovery_s", "pool_blocked_reason"):
        assert field in src
