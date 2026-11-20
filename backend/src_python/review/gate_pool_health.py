# -*- coding: utf-8 -*-
"""v342: 门卫通道"假冗余"自检。

事故背景（2026-11-16）：config 里写了
    minimax-gate  (主) → MINIMAX_GATE_KEY  = 账户2
    minimax-gate2 (备) → MINIMAX_MAIN_KEY  = 账户1
但 .env 里三个 key 其实是同一个 → 备用通道的守卫 `key != GATE_KEY` 不成立 →
**备用通道根本没被创建**，purposes='gate' 池里只有 1 条通道，和主分析抢同一份配额。
配额一满就是全池 429，新闻冻结等回放（实测断流数小时）。

这个模块只做一件事：启动时把"看起来有多条、实际共用一份配额"这件事喊出来。
纯函数，不碰网络、不读数据库。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List


def _fp(key: str) -> str:
    """key 指纹（只用于判重，不泄露原文）。"""
    return hashlib.sha256(str(key or "").encode()).hexdigest()[:12]


def gate_pool_report(roster: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计 purpose='gate' 的通道数与不同 key 数。

    fake_redundancy=True 表示"通道 >= 2 但 key 只有 1 个"（或只有 1 条通道）。
    """
    chans = [m for m in (roster or []) if "gate" in (m.get("purpose") or [])]
    keys = {_fp(m.get("api_key")) for m in chans}
    aliases = [str(m.get("key_alias") or m.get("id") or "?") for m in chans]
    fake = len(chans) < 2 or len(keys) < 2
    warn = None
    if fake:
        warn = (
            f"门卫通道只有 {len(chans)} 条、独立 key {len(keys)} 个 "
            f"(aliases={aliases}) → 与主分析共用配额；配额打满时门卫会整体 429。"
            " 可在 .env 配 GATE_ALT_{BASE_URL,API_KEY,MODEL} 接一个独立端点。"
        )
    return {
        "channels": len(chans),
        "distinct_keys": len(keys),
        "aliases": aliases,
        "fake_redundancy": fake,
        "warning": warn,
    }
