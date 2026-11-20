# -*- coding: utf-8 -*-
"""v322: 「全通道不可用」主动告警的节流不变量。

背景（2026-11-07）
-----------------
临时 key 额度用尽 → 主循环进入 pool-blocked → **不认领、不消耗额度、
也完全不告警** → 用户只看到「待分析不涨」，以为系统抽风。

加了告警之后，最怕的是**反过来刷屏**：Server酱免费版每天只有 5 次发送额度
（日志实测撞过 "[AUTH] 超过当天的发送次数限制[5]"）。

本文件锁死的不变量
------------------
A. 首次阻塞立即告警
B. 同一原因、6 小时内 → 不重复告警
C. 同一原因、超过 6 小时 → 再告警一次（≤4 次/天）
D. 原因变了 → 立即告警（不能等 6 小时）
E. 从没告警过阻塞时，"恢复"不告警（不浪费额度）
F. 告警过阻塞后，"恢复"要告警一次（告诉用户可以换回 key 了）
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine import ai_worker


@pytest.fixture()
def alert_env(tmp_path, monkeypatch):
    """把告警状态文件指到临时目录，并记录每次派发的标题。"""
    state_file = str(tmp_path / "pool_blocked_alert.json")
    monkeypatch.setattr(ai_worker, "_POOL_ALERT_FILE", state_file)
    sent = []

    def _fake_dispatch(title, body):
        sent.append((title, body))

    monkeypatch.setattr(ai_worker, "_dispatch_pool_alert", _fake_dispatch)
    return sent


def test_first_block_notifies(alert_env):
    """A. 首次阻塞立即告警。"""
    assert ai_worker.notify_pool_blocked("全部通道熔断: minimax1") is True
    assert len(alert_env) == 1
    assert "全挂" in alert_env[0][0]


def test_same_reason_within_interval_does_not_notify(alert_env, monkeypatch):
    """B. 同一原因、6 小时内不重复告警。"""
    assert ai_worker.notify_pool_blocked("原因A") is True
    monkeypatch.setattr(ai_worker, "_POOL_ALERT_INTERVAL_S", 21600.0)
    assert ai_worker.notify_pool_blocked("原因A") is False
    assert len(alert_env) == 1


def test_same_reason_after_interval_notifies_again(alert_env, monkeypatch):
    """C. 超过节流窗口 → 再告警一次（保证不会永久静默）。"""
    assert ai_worker.notify_pool_blocked("原因A") is True
    monkeypatch.setattr(ai_worker, "_POOL_ALERT_INTERVAL_S", 0.0)
    assert ai_worker.notify_pool_blocked("原因A") is True
    assert len(alert_env) == 2


def test_changed_reason_notifies_immediately(alert_env, monkeypatch):
    """D. 原因变了 → 立即告警（不能等 6 小时才告诉用户）。"""
    assert ai_worker.notify_pool_blocked("原因A") is True
    monkeypatch.setattr(ai_worker, "_POOL_ALERT_INTERVAL_S", 21600.0)
    assert ai_worker.notify_pool_blocked("原因B") is True
    assert len(alert_env) == 2


def test_recovery_without_prior_alert_does_not_notify(alert_env):
    """E. 从没告警过阻塞 → "恢复"不告警（不浪费 Server酱额度）。"""
    assert ai_worker.notify_pool_blocked("", recovered=True) is False
    assert len(alert_env) == 0


def test_recovery_after_block_notifies(alert_env):
    """F. 告警过阻塞 → "恢复"要告警一次。"""
    assert ai_worker.notify_pool_blocked("原因A") is True
    assert ai_worker.notify_pool_blocked("", recovered=True) is True
    assert len(alert_env) == 2
    assert "恢复" in alert_env[1][0]


def test_alert_state_persists_across_calls(alert_env, tmp_path):
    """状态必须落盘 —— 引擎重启后不能重复轰炸。"""
    assert ai_worker.notify_pool_blocked("原因A") is True
    assert os.path.exists(ai_worker._POOL_ALERT_FILE)
    # 新进程视角（重新读文件）
    st = ai_worker._pool_alert_state()
    assert st.get("notified") is True
    assert st.get("reason") == "原因A"


def test_alert_body_contains_recovery_steps(alert_env):
    """告警正文必须写清楚「怎么办」——只说坏了不给动作等于没说。"""
    ai_worker.notify_pool_blocked("全部通道熔断: minimax1")
    body = alert_env[0][1]
    assert "rotate_minimax_keys.py" in body
    assert "kickstart" in body
    assert "全部通道熔断" in body
