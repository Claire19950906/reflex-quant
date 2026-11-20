# -*- coding: utf-8 -*-
"""Trident 2.0 功能层（64 项功能候选的落地骨架）。

设计约束（与 AGENTS.md「数据真实性铁律」一致）：
    1. 不猜价 / 不补价：取不到就 ``status="no_data"``，绝不返回占位 0；
    2. 单一真值源：所有读数走 ``db.get_connection()`` 指向的 event-bus 库；
    3. 口径可追溯：每个结果都带 ``evidence``（表名 + 查询条件 + 时间锚点）；
    4. 执行类功能一律「纸面 / dry-run」：真实下单需要显式开关，默认关闭。
"""
from __future__ import annotations

__version__ = "2.0.0"
