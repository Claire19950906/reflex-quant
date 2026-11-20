#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine_watchdog 挂死判定逻辑的单元测试。

覆盖 2026-11-04 生产事故的四个真实场景：
  1. 引擎启动中 progress 还没生成 → 不能判死（否则无限重启，实测 101 次）
  2. progress 长时间不更新 + 刚重启过 → 冷却期内跳过（防死循环）
  3. progress 长时间不更新 + 冷却期已过 → 该重启
  4. 进程直接没了 → 硬故障，立刻拉起，不受冷却期限制
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import engine_watchdog as wd


def test_healthy_no_restart():
    hung, hard, reason, restart = wd.decide(True, age=5.0, uptime=3600.0, threshold=600.0)
    assert not hung
    assert not hard
    assert not restart
    assert "正常" in reason


def test_progress_missing_during_startup_is_not_dead():
    """引擎刚起来、progress 还没写出来 —— 绝不能判死（无限重启的根因）。"""
    hung, hard, reason, restart = wd.decide(True, age=None, uptime=60.0, threshold=600.0)
    assert not hung
    assert not restart
    assert "宽限期" in reason or "暂不判定" in reason


def test_progress_missing_after_grace_is_dead():
    """跑了很久还没 progress → 判死。"""
    hung, hard, reason, restart = wd.decide(
        True, age=None, uptime=wd.GRACE_AFTER_BOOT_S + 10.0, threshold=600.0)
    assert hung
    assert not hard
    assert restart


def test_stale_progress_restarts_after_cooldown():
    """卡死很久且冷却期已过 → 立刻重启。"""
    hung, hard, reason, restart = wd.decide(
        True, age=3600.0, uptime=7200.0, threshold=600.0,
        since_last_restart=wd.MIN_RESTART_INTERVAL_S + 1.0)
    assert hung
    assert restart
    assert not hard


def test_stale_progress_respects_cooldown():
    """卡死但刚重启过（冷却期内）→ 跳过重启，防无限重启循环。"""
    hung, hard, reason, restart = wd.decide(
        True, age=3600.0, uptime=7200.0, threshold=600.0, since_last_restart=10.0)
    assert hung          # 仍然判定为挂死
    assert not restart   # 但不重启
    assert "冷却期" in reason


def test_dead_process_is_hard_failure_ignores_cooldown():
    """进程被 OOM kill 了 —— 硬故障，必须立刻拉起，不能被冷却期挡住。"""
    hung, hard, reason, restart = wd.decide(
        False, age=3600.0, uptime=None, threshold=600.0, since_last_restart=1.0)
    assert hung
    assert hard
    assert restart, "进程死亡必须立刻重启（launchd 也可能拉不起来，看门狗是唯一兜底）"


def test_dead_process_with_no_progress_file():
    hung, hard, reason, restart = wd.decide(False, age=None, uptime=None, threshold=600.0)
    assert hung and hard and restart


def test_pid_miss_but_fresh_heartbeat_not_restarted():
    """v434：PID 探测瞬时漏判，但心跳刚更新（21s）→ 视为探测误判，不重启。"""
    hung, hard, reason, restart = wd.decide(
        False, age=21.0, uptime=3600.0, threshold=1800.0)
    assert not hung
    assert not hard
    assert not restart, "心跳新鲜说明进程活着，不能误杀在办批次"


def test_pid_miss_and_stale_heartbeat_still_hard_failure():
    """心跳也陈旧（>120s）仍未见进程 → 按硬故障拉起。"""
    hung, hard, reason, restart = wd.decide(
        False, age=3600.0, uptime=None, threshold=1800.0, since_last_restart=1.0)
    assert hung and hard and restart


def test_cannot_decide_when_uptime_unknown():
    """拿不到运行时长 → 不判死（宁可漏判也不误杀）。"""
    hung, hard, reason, restart = wd.decide(True, age=None, uptime=None, threshold=600.0)
    assert not hung
    assert not restart


def test_never_restart_when_alive_and_fresh():
    """边界：age 正好等于阈值 → 不该判死。"""
    hung, _, _, restart = wd.decide(True, age=600.0, uptime=3600.0, threshold=600.0)
    assert not hung
    assert not restart


def test_step_stuck_over_hang_after_restarts():
    """v435：心跳新鲜但 analyze 步长期不换（>STEP_HANG_AFTER_S）→ 判挂死并重启。"""
    hung, hard, reason, restart = wd.decide(
        True, age=5.0, uptime=7200.0, threshold=1800.0,
        step_age=wd.STEP_HANG_AFTER_S + 60.0, step="analyze",
        since_last_restart=wd.MIN_RESTART_INTERVAL_S + 1.0)
    assert hung
    assert not hard
    assert restart
    assert "未换步" in reason


def test_step_within_hang_after_is_healthy():
    """分析步停留时间在挂死线内（长批次）→ 不判死。"""
    hung, _, _, restart = wd.decide(
        True, age=5.0, uptime=7200.0, threshold=1800.0,
        step_age=600.0, step="analyze")
    assert not hung
    assert not restart


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
