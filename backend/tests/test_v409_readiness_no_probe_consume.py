# -*- coding: utf-8 -*-
"""v409.2: 通道体检不许消耗 half_open 的探测名额 —— 回归单测。

事故背景（2026-11-17 10:24–10:57 生产实测）
------------------------------------------
`main_pool_ready()` 用 `brk.allow()` 做通道体检。但 `allow()` 有副作用：
quota 熔断器 open 冷却到期时，它会顺手把状态推到 `half_open`，并把
`_probe_in_flight = True`（占用唯一一次探测名额）。体检本身**不发请求**，
真正要发请求的调用点再 `allow()` 就拿到 False —— 而且这个占用要等
`probe_timeout`（quota = max(60, 1800) = **30 分钟**）才释放。

现场表现：
  · 日志反复 `[AI] 跳过认领：全部通道熔断: minimax1[熔断中],minimax2[熔断中]`
    （注意没有 "123s" 倒计时 —— 说明卡的是 half_open 而不是 open）；
  · 熔断状态文件停在 `{"state": "half_open", "fail": 1}`，30 分钟不动；
  · 决策数 4 小时纹丝不动，`raw_news` PENDING 一路涨到 259；
  · 而 `llm_probe.json` 显示 9/9 通道 available=true —— 额度其实是好的。

修法：新增只读的 `CircuitBreaker.available()`，体检只读不占名额。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import circuit_breaker as cb


def _fresh(tmp_path, name="llm_main_quota:testch", **kw):
    b = cb.CircuitBreaker(
        name,
        fail_threshold=kw.get("fail_threshold", 1),
        open_seconds=kw.get("open_seconds", 0.05),
        probe_timeout=kw.get("probe_timeout"),
    )
    b._shared_path = None        # 单测不落盘，避免读到生产状态文件
    return b


def test_available_does_not_consume_probe(tmp_path):
    """open 冷却到期后：available() 连调多次都应为 True，且状态不被推进。"""
    b = _fresh(tmp_path)
    b.failure()
    assert b.state == "open"
    time.sleep(0.08)                      # 冷却到期
    for _ in range(5):
        assert b.available() is True
    # 状态仍是 open —— 体检没有替调用点把探测名额用掉
    assert b.state == "open"
    # 真正发请求的那一次才拿得到名额
    assert b.allow() is True
    assert b.state == "half_open"


def test_allow_twice_second_blocked_but_available_still_true(tmp_path):
    """half_open 探测在飞时：allow() 应拒绝（single-flight），available() 也判不可用。"""
    b = _fresh(tmp_path, open_seconds=0.01)
    b.failure()
    time.sleep(0.03)
    assert b.allow() is True              # 第一个探测放行
    assert b.allow() is False             # 第二个被 single-flight 挡住
    assert b.available() is False         # 探测在飞 → 体检不可用


def test_probe_timeout_reopens_slot(tmp_path):
    """探测超时未回报 → 名额自动释放，不会永久卡死。"""
    b = _fresh(tmp_path, open_seconds=0.01)
    b.failure()
    time.sleep(0.03)
    assert b.allow() is True
    b._probe_at = time.time() - (b.probe_timeout + 1.0)   # 模拟探测丢了
    assert b.available() is True
    assert b.allow() is True


def test_closed_available_true_open_false(tmp_path):
    """closed 恒可用；open 冷却未到期不可用。"""
    b = _fresh(tmp_path, open_seconds=600)
    assert b.available() is True
    b.failure()
    assert b.state == "open"
    assert b.available() is False
    assert b.state == "open"              # 只读，不改状态
