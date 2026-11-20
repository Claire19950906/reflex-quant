"""v171 safe clock — 单调时钟 + wall-clock 跳变守护 单元测试。

覆盖：
- safe_now 返回合理 epoch / 多次调用递增
- safe_age 正常 / 异常 / future 跳变 / clamp 上限
- safe_age_pair（carry-over 双时间）
- clock_diagnostics 返回结构化状态
"""
from __future__ import annotations

import os
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import clock


def setup_function(_):
    """每个 test 前清零 clock 模块状态。"""
    clock._reset_state_for_tests()


# ────────────────────────────────────────────────────────────────────────────
# safe_now 基础
# ────────────────────────────────────────────────────────────────────────────

def test_safe_now_returns_int_epoch():
    n = clock.safe_now()
    assert isinstance(n, int)
    # epoch 秒应大于 2020 年初
    assert n > 1577836800


def test_safe_now_monotonic_in_short_window():
    a = clock.safe_now()
    time.sleep(0.01)
    b = clock.safe_now()
    assert b >= a


def test_safe_now_after_reset():
    """reset 之后第一次调用仍返回合理值。"""
    n = clock.safe_now()
    assert isinstance(n, int)
    assert n > 1577836800


# ────────────────────────────────────────────────────────────────────────────
# safe_age 正常 + 异常
# ────────────────────────────────────────────────────────────────────────────

def test_safe_age_normal():
    now = clock.safe_now()
    assert clock.safe_age(now - 100, now=now) == 100
    assert clock.safe_age(now - 86400, now=now) == 86400


def test_safe_age_returns_max_for_invalid():
    """异常 / None / 0 → 返回 max_age（视为"已过很久"）。"""
    assert clock.safe_age(None) == clock.MAX_AGE_SECONDS
    assert clock.safe_age(0) == clock.MAX_AGE_SECONDS
    assert clock.safe_age(-1) == clock.MAX_AGE_SECONDS
    assert clock.safe_age("not-a-number") == clock.MAX_AGE_SECONDS


def test_safe_age_clamps_future_to_zero():
    """未来时间（时钟回退 / 错误时间戳）→ clamp 到 0，不出现负数。"""
    now = clock.safe_now()
    future = now + 9999
    assert clock.safe_age(future, now=now) == 0


def test_safe_age_clamps_to_max_age():
    """极古老时间 → clamp 到 max_age。"""
    now = clock.safe_now()
    assert clock.safe_age(0, now=now) == clock.MAX_AGE_SECONDS
    assert clock.safe_age(now - 1000000000, now=now) == clock.MAX_AGE_SECONDS


def test_safe_age_custom_max():
    now = clock.safe_now()
    assert clock.safe_age(now - 99999, now=now, max_age=3600) == 3600


# ────────────────────────────────────────────────────────────────────────────
# safe_age_pair（carry-over 专用）
# ────────────────────────────────────────────────────────────────────────────

def test_safe_age_pair_normal():
    now = clock.safe_now()
    last, total = clock.safe_age_pair(now - 100, now - 50, now=now)
    assert last == 50
    assert total == 100


def test_safe_age_pair_missing_last_seen_falls_back_to_created():
    """last_seen 缺失 → 退化用 created_at 当 last_seen。"""
    now = clock.safe_now()
    last, total = clock.safe_age_pair(now - 100, None, now=now)
    # last 退化 = 100, total = 100
    assert last == 100
    assert total == 100


def test_safe_age_pair_zero_last_seen_falls_back():
    """last_seen=0 → 退化。"""
    now = clock.safe_now()
    last, total = clock.safe_age_pair(now - 100, 0, now=now)
    assert last == 100
    assert total == 100


def test_safe_age_pair_both_future_clamps_to_zero():
    now = clock.safe_now()
    future = now + 9999
    last, total = clock.safe_age_pair(future, future, now=now)
    assert last == 0
    assert total == 0


# ────────────────────────────────────────────────────────────────────────────
# clock_diagnostics
# ────────────────────────────────────────────────────────────────────────────

def test_clock_diagnostics_returns_keys():
    diag = clock.clock_diagnostics()
    for k in ("last_wall", "last_mono", "jump_warned",
              "wall_now", "mono_now", "max_age", "jump_threshold"):
        assert k in diag, f"缺键: {k}"


def test_clock_diagnostics_consistent_after_call():
    """调一次 safe_now 后诊断应该有 last_wall / last_mono。"""
    clock.safe_now()
    diag = clock.clock_diagnostics()
    assert diag["last_wall"] > 0
    assert diag["last_mono"] > 0
    assert diag["jump_warned"] is False


# ────────────────────────────────────────────────────────────────────────────
# 跳变检测（不依赖真实系统时间，手动改 _last_wall / _last_mono）
# ────────────────────────────────────────────────────────────────────────────

def test_jump_detection_logs_warning(caplog):
    """人为构造 wall-clock 跳变 → jump_warned 翻 True。"""
    import logging
    caplog.set_level(logging.WARNING, logger="trident.clock")

    # 第一次调用：初始化 _last_wall / _last_mono
    clock.safe_now()
    # 手动将 _last_wall 设到 1000 秒前（模拟跳变）
    clock._last_wall -= 10000
    clock._last_mono -= 5  # mono 只走了 5 秒
    clock.safe_now()
    diag = clock.clock_diagnostics()
    assert diag["jump_warned"] is True


# ────────────────────────────────────────────────────────────────────────────
# 与 db.compute_decay 集成（兜底）
# ────────────────────────────────────────────────────────────────────────────

def test_compute_decay_uses_safe_now_path():
    """db.compute_decay 在正常 age 输入下保持单调衰减。"""
    import db
    impact = 1.0
    age = 100
    initial = db.compute_decay(impact, 0.01, age)
    after_longer = db.compute_decay(impact, 0.01, age * 2)
    assert after_longer < initial  # 衰减
    assert 0 < after_longer < initial