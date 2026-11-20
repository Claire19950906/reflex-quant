# -*- coding: utf-8 -*-
"""安全时钟 — 把"系统挂钟跳变 / 时间漂移"对 signal_state 衰减的影响降到最小。

设计目的：
- SIGINT / 离线复盘 / 跨夜运行 → time.time() 可能出现跳变 / 回退
- signal_state.compute_decay 用 age_seconds = now - created_at；
  age_seconds 为负 → 衰减系数 > 1 → 残留 > initial_impact（设计上限），
  age_seconds 突然变大 → carry-over 信号看起来"瞬间消失"。
- 本模块在调用方透明插入：
  1. monotonic + wall clock 双源校验，检测跳变
  2. clamp age_seconds 在 [0, MAX_AGE_SECONDS] 之间，避免极端值
  3. epoch 异常回退 → 返回 0 + warning（不抛异常，下游走退化分支）

约束：
- 仅 stdlib，无 pytz / ntplib 依赖
- 不调用网络，engine 离线可 import
"""

from __future__ import annotations

import logging
import time as _time
from typing import Optional, Tuple

# ── 配置常量 ─────────────────────────────────────────────────
# 单次跳跃容忍阈值：相邻两次 wall-clock 差 > 此值 → 视为跳变（首次启动除外）
JUMP_DETECTION_THRESHOLD_SECONDS = 30.0

# age_seconds 的合理上限：超过 30 天认为 carry-over 完全衰减（基本无意义）
MAX_AGE_SECONDS = 30 * 86400

# 历史回放允许最大单次跨度（避免 OS 升级 / DST 切换造成的时间断点）
OFFLINE_REPLAY_MAX_AGE_SECONDS = 365 * 86400


_module_logger = logging.getLogger("trident.clock")
# 防止上层没配置 handler 时出现 "No handlers could be found" 警告
if not _module_logger.handlers:
    _module_logger.addHandler(logging.NullHandler())
_module_logger.propagate = False  # 不向上冒泡到 root

_last_wall: float = 0.0
_last_mono: float = 0.0
_jump_warned: bool = False


def _reset_state_for_tests() -> None:
    """测试可调用：把模块级状态清零。"""
    global _last_wall, _last_mono, _jump_warned
    _last_wall = 0.0
    _last_mono = 0.0
    _jump_warned = False


def safe_now() -> int:
    """安全的"现在"——epoch 秒，检测 wall-clock 跳变。

    Returns:
        int epoch 秒

    Notes:
        - 正常情况 → 直接返回 int(time.time())
        - 跳变情况 → 仍返回当前 wall-clock 值（不抛异常），并 warn 一次
        - 不试图"修正"时间 → 这不是 NTP daemon 的职责；只做"标记异常 + 下游可观测"
    """
    global _last_wall, _last_mono, _jump_warned

    wall = _time.time()
    mono = _time.monotonic()

    if _last_wall == 0.0:
        # 首次调用
        _last_wall = wall
        _last_mono = mono
        return int(wall)

    delta_wall = wall - _last_wall
    delta_mono = mono - _last_mono

    # monotonic 永远递增 → 与 wall-clock 差 = 跳变量
    jump = delta_wall - delta_mono

    if abs(jump) > JUMP_DETECTION_THRESHOLD_SECONDS:
        if not _jump_warned:
            _module_logger.warning(
                "Wall clock jump detected: delta_wall=%.1fs delta_mono=%.1fs jump=%.1fs",
                delta_wall, delta_mono, jump,
            )
            _jump_warned = True
        # 重置基准，下一次重新对齐
        _last_wall = wall
        _last_mono = mono
        return int(wall)

    _last_wall = wall
    _last_mono = mono
    return int(wall)


def safe_age(
    created_at: Optional[int],
    *,
    now: Optional[int] = None,
    max_age: int = MAX_AGE_SECONDS,
) -> int:
    """安全 age — 给 carry-over / event_atom / signal_state 等用。

    Args:
        created_at: 创建时刻 epoch 秒（None / 0 视为"无可比基准"）
        now: 当前时刻（None=自动 safe_now()）
        max_age: 上限 clamp（默认 30 天；离线回放可放宽）

    Returns:
        int 已 clamp 到 [0, max_age] 的秒数
        created_at 异常 → 返回 max_age（视为"已过很久，无 carry-over 残留"）
    """
    if created_at is None:
        return max_age
    try:
        ts = int(created_at)
    except (TypeError, ValueError):
        return max_age
    if ts <= 0:
        return max_age

    current = int(now) if now is not None else safe_now()
    try:
        age_raw = current - ts
    except Exception:
        return max_age

    # 钳位到 [0, max_age]
    if age_raw < 0:
        return 0  # 时间回退防御
    if age_raw > max_age:
        return max_age
    return int(age_raw)


def safe_age_pair(
    created_at: Optional[int],
    last_seen_at: Optional[int],
    *,
    now: Optional[int] = None,
    max_age: int = MAX_AGE_SECONDS,
) -> Tuple[int, int]:
    """信号残留专用：返回 (age_since_last_seen, total_age_since_created)。

    Args:
        created_at: 信号创建时刻
        last_seen_at: 最近一次观察到同指纹的时刻
        now: 当前时刻
        max_age: clamp 上限

    Returns:
        (age_since_last_seen, total_age_since_created)
        二者已分别 clamp
    """
    if last_seen_at is None or int(last_seen_at or 0) <= 0:
        last_seen_at = created_at  # 退化：用 created_at 当 last_seen

    age_last = safe_age(last_seen_at, now=now, max_age=max_age)
    age_total = safe_age(created_at, now=now, max_age=max_age)
    return age_last, age_total


def clock_diagnostics() -> dict:
    """调试 / 监控：返回当前时钟内部状态。"""
    global _last_wall, _last_mono, _jump_warned
    return {
        "last_wall": _last_wall,
        "last_mono": _last_mono,
        "jump_warned": _jump_warned,
        "wall_now": _time.time(),
        "mono_now": _time.monotonic(),
        "max_age": MAX_AGE_SECONDS,
        "jump_threshold": JUMP_DETECTION_THRESHOLD_SECONDS,
    }


# ────────────────────────────────────────────────────────────────────────────
# Self-test
# ────────────────────────────────────────────────────────────────────────────
# v171+ P5：CLI 入口（必须先于 if __name__ 定义，否则 sys.exit(_cli()) 找不到）
# ────────────────────────────────────────────────────────────────────────────

def _cli() -> int:
    """v171+ P5：clock CLI（sub-commands: now / age / diagnostics / simulate-jump）。"""
    import argparse
    parser = argparse.ArgumentParser(
        prog="clock",
        description="Trident 安全时钟 CLI (v171+)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_n = sub.add_parser("now", help="输出 safe_now() epoch")
    p_a = sub.add_parser("age", help="计算 age_seconds (clamp 到 [0, max_age])")
    p_a.add_argument("epoch", type=int, help="事件创建 epoch")
    p_a.add_argument("--max-age", type=int, default=MAX_AGE_SECONDS,
                     help="age 上限（秒）")
    p_d = sub.add_parser("diagnostics", help="输出 diagnostics()")
    p_j = sub.add_parser("simulate-jump", help="模拟时钟跳变（测试用）")
    p_j.add_argument("delta", type=float, help="跳变量（秒）")

    args = parser.parse_args()

    if args.cmd == "now":
        print(safe_now())
        return 0
    elif args.cmd == "age":
        print(safe_age(args.epoch, max_age=args.max_age))
        return 0
    elif args.cmd == "diagnostics":
        import json as _json
        print(_json.dumps(clock_diagnostics(), ensure_ascii=False, indent=2, default=str))
        return 0
    elif args.cmd == "simulate-jump":
        # 测试用：注入跳变 → 触发 safe_now() 检测到 → diagnostics 报告
        global _last_wall, _last_mono, _jump_warned
        import time as _t
        _last_wall = _t.time() - args.delta
        _last_mono = _t.monotonic() - 0.001
        _jump_warned = True  # 强制 warn
        # 立即调用 safe_now() 触发 detection 并 print diagnostics
        safe_now()
        import json as _json
        print(_json.dumps(clock_diagnostics(), ensure_ascii=False, indent=2, default=str))
        return 0
    return 1


# ────────────────────────────────────────────────────────────────────────────
# Self-test
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("now", "age", "diagnostics", "simulate-jump"):
        sys.exit(_cli())
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    print("=== safe_now ===")
    print(f"  now = {safe_now()}")
    print(f"  now (再次) = {safe_now()}")

    print("\n=== safe_age (正常) ===")
    now = safe_now()
    print(f"  age(now-100) = {safe_age(now - 100, now=now)}")
    print(f"  age(now-86400) = {safe_age(now - 86400, now=now)}")

    print("\n=== safe_age (异常) ===")
    print(f"  age(None) = {safe_age(None)}")
    print(f"  age(0) = {safe_age(0)}")
    print(f"  age('-x') = {safe_age('-x')}")

    print("\n=== safe_age (跳变) ===")
    print(f"  age(future) = {safe_age(now + 999, now=now)}")
    print(f"  age(epoch=0) = {safe_age(0, now=now)}")
    print(f"  age(epoch=1) = {safe_age(1, now=now, max_age=7200)}  # max=2h")

    print("\n=== safe_age_pair ===")
    print(f"  pair(now-100, now-50) = {safe_age_pair(now - 100, now - 50, now=now)}")

    print("\n=== clock_diagnostics ===")
    for k, v in clock_diagnostics().items():
        print(f"  {k:18s} = {v!r}")
