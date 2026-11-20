# -*- coding: utf-8 -*-
"""纪元闸门（单一实现）。

为什么要有这个模块
------------------
v340 数据纪元（data_epoch）是"清空旧判断、只认新数据"的分界线。各作业都要
按它拦掉 epoch 之前的历史行。这套逻辑一开始是**每个文件各写一份**，于是踩了
一个很典型的坑（2026-11-15 实测）：

    abnormal_detector.py 顶部是 ``import datetime``（模块），闸门里写
    ``datetime.strptime(...)`` —— 模块没有 strptime → AttributeError →
    被 ``except Exception: return 0`` 吞掉 → 纪元永远读到 0 →
    ``if _ep and ...`` 短路 → **闸门等于没开**，清了三次旧数据还在长回来。

结论：这类"读不到就返回默认值"的写法必须集中一处、并且要**能被测出来**。
所以：

* 只有本模块负责解析 data_epoch，其它模块一律调它；
* 解析失败返回 None（而不是 0），调用方必须显式处理 None，
  不允许再把"读不到"当成"没有纪元"；
* ``is_pre_epoch`` / ``clamp_since`` 把"留一天余量"的口径也统一掉
  （日频信号锚在当天 00:00，比纪元早几小时，属正常，不能当旧数据）。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import time
from typing import Optional

#: 日频信号的时间锚是当天 00:00，会比纪元早几个小时；留一天余量再判"旧数据"。
EPOCH_GRACE_SEC = 86400

_EPOCH_CACHE: dict[int, int] = {}


def parse_epoch(value: object) -> Optional[int]:
    """把 data_epoch.started_at（'YYYY-MM-DD HH:MM:SS'，本地墙钟）转成 unix 秒。

    故意不用 ``datetime.strptime``：那个写法在不同导入风格下会解析到模块上而
    静默失败（见模块 docstring）。这里用 ``time.strptime`` + ``time.mktime``，
    语义与其它模块原来的 ``datetime.strptime(...).timestamp()`` 完全一致
    （都是"本地墙钟→epoch"），但不依赖 datetime 的导入方式。
    """
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    if len(text) < 10:
        return None
    fmt = "%Y-%m-%d %H:%M:%S" if len(text) >= 19 else "%Y-%m-%d"
    head = text[:19] if fmt.endswith("%S") else text[:10]
    try:
        # tm_isdst=-1（strptime 默认）→ mktime 按本地时区规则自动判定夏令时
        return int(time.mktime(time.strptime(head, fmt)))
    except (ValueError, OverflowError):
        return None


def epoch_ts(conn: sqlite3.Connection, *, use_cache: bool = True) -> Optional[int]:
    """读 data_epoch 起点（unix 秒）。表不存在或没有行 → None。"""
    key = id(conn)
    if use_cache and key in _EPOCH_CACHE:
        return _EPOCH_CACHE[key]
    val: Optional[int] = None
    try:
        row = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
        if row and row[0]:
            val = parse_epoch(row[0])
    except sqlite3.OperationalError:
        val = None
    if use_cache and val is not None:
        _EPOCH_CACHE[key] = val
    return val


def clear_cache() -> None:
    _EPOCH_CACHE.clear()


def is_pre_epoch(ts: Optional[int], epoch: Optional[int],
                 grace: int = EPOCH_GRACE_SEC) -> bool:
    """该 ts 是否属于"纪元之前的旧数据"（留 grace 秒余量）。

    epoch 为 None（读不到纪元）时一律返回 False —— 即"不拦"，
    与历史行为一致；但调用方应当把 None 当成"闸门未就绪"记日志，
    而不是当成功。
    """
    if ts is None or epoch is None:
        return False
    return int(ts) < int(epoch) - int(grace)


def clamp_since(since: Optional[int], epoch: Optional[int],
                grace: int = 0) -> Optional[int]:
    """把作业的 since 抬到纪元（since 为空则直接返回纪元）。

    grace=0：起点就是纪元本身（用于"重扫窗口"类作业，不额外留余量）；
    """
    if epoch is None:
        return since
    floor = int(epoch) - int(grace)
    if since is None:
        return floor
    return max(int(since), floor)
