#!/usr/bin/env python3
"""把 risk_manager 真正接进下单链路（之前它从未被调用过）。

背景（v343 实测）：
    risk_manager.evaluate() 有完整实现（白名单 / 单笔上限 / 最大持仓 / 同标的次数 /
    kill switch），但全库只有 reflection_policy_updater 提到它 ——
    **下单链路 quick_sim 一次都没调用**。等于"写了风控、上线裸奔"。

这个适配层解决三件事：
  1. RiskManager 是**内存态**（计数在进程里），quick_sim 是批处理，
     每次都要从库里把"当前持仓/同标的笔数"重新种进去，否则计数永远 0 → 不设防。
  2. 被拒的下单要**留痕**，否则"为什么没成交"又变成黑箱。
  3. kill switch 要能从进程外关掉（文件开关），不用重启。

用法：
    import risk_guard
    allow, size, reason = risk_guard.check(conn, asset, action, notional)
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

import risk_manager as _rm

KILL_FILE = os.path.expanduser("~/.trident/state/risk_kill_switch")


def _seed(conn: sqlite3.Connection) -> _rm.RiskManager:
    """从库里重建风控计数（内存态不能跨进程继承）。"""
    mgr = _rm.RiskManager(
        whitelist=tuple((os.getenv("RISK_WHITELIST", "*") or "*").split(",")),
        max_position_usdt=float(os.getenv("RISK_MAX_POSITION_USDT", "5000")),
        max_open_positions=int(os.getenv("RISK_MAX_OPEN_POSITIONS", "5")),
        max_actions_per_asset=int(os.getenv("RISK_MAX_ACTIONS_PER_ASSET", "3")),
    )
    # 未平仓的模拟单 = 当前持仓
    try:
        rows = conn.execute(
            "SELECT UPPER(asset), COUNT(*) FROM quick_sim_trades "
            "WHERE COALESCE(settled,0)=0 GROUP BY UPPER(asset)"
        ).fetchall()
        for asset, n in rows:
            for _ in range(int(n)):
                mgr.on_position_opened(asset)
    except sqlite3.OperationalError:
        pass

    # 文件开关优先（可在不重启引擎的情况下熔断）
    try:
        if os.path.exists(KILL_FILE):
            mgr.set_kill_switch(True)
    except OSError:
        pass
    if os.getenv("RISK_KILL_SWITCH", "0") in ("1", "true", "True"):
        mgr.set_kill_switch(True)
    return mgr


def check(conn: sqlite3.Connection, asset: str, action: str,
          notional: float, ctx: Optional[dict] = None) -> tuple[bool, float, str]:
    """返回 (是否放行, 允许的名义金额, 原因)。"""
    if os.getenv("RISK_GUARD_ENABLED", "1").strip() == "0":
        return True, float(notional), "risk_guard_disabled"
    mgr = _seed(conn)
    d = mgr.evaluate(asset, action, float(notional), ctx or {})
    return bool(d.allow), float(d.max_allowed_size), str(d.reason)
