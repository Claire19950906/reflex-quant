# -*- coding: utf-8 -*-
"""silent_ledger.py — 丢写账本：任何「失败就丢」的路径都要在这里留一笔。

为什么需要（2026-11-19 审计实测）
--------------------------------
全仓 619 处 `except Exception` 后面直接 `pass` / 直接 `return`。SQLite 单写者
被长事务霸占时，`source_status` / `market_ticks` 的落库会**静默丢弃**：

  * `feeds/source_status.py::_persist_with_retry` 重试 3 次后放弃，只在
    进程生命周期内 `print` **一次**，之后 0 痕迹；
  * `timeseries.py::_market_worker` 失败只 `failed += 1`，计数器谁也读不到。

结果：日志里一句"persist skipped"，事后没人能回答"今天到底丢了多少、丢的是谁"。
这正是「0 号铁律」（不许假数据）最容易被绕过的地方——**没写进去的和写进去的
看起来一样**。

本模块的边界
------------
* 不做重试、不碰业务判断、**绝不抛异常**（丢写记录失败不能反过来影响主链路）；
* 内存计数（调用方随时可读 `counts()`）+ 每分钟每 key 最多落一行 JSONL；
* 落盘失败就只留内存，不递归告警。

用法
----
    import silent_ledger
    silent_ledger.record("source_status.persist", "OperationalError: database is locked")

    # 运维侧看今天丢了多少：
    #   tail -1 backend/runtime/silent_drops.jsonl
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Optional

#: 每条 key 两次落盘之间的最小间隔（秒）；环境变量可调，测试里直接设 0
MIN_INTERVAL_S = float(os.getenv("TRIDENT_SILENT_LEDGER_INTERVAL_S", "60") or 60)

#: 账本路径：默认 backend/runtime/silent_drops.jsonl（runtime/ 已进 .gitignore）
LEDGER_PATH = os.getenv("TRIDENT_SILENT_LEDGER") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "silent_drops.jsonl"
)

_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {}       # key -> 累计丢写次数（进程生命周期）
_PENDING: Dict[str, int] = {}      # key -> 距上次落盘又丢了多少
_LAST_EMIT: Dict[str, float] = {}
_WRITE_ERRORS = 0


def record(key: str, reason: str = "", *, n: int = 1, force: bool = False) -> None:
    """记一笔丢写。**永不抛异常**。

    Args:
        key:    丢写点标识（建议 "模块.动作"，如 "market_ticks.write"）。
        reason: 一句话原因（异常类型 + 摘要即可，勿拼大对象）。
        n:      本次丢了几条（批量丢弃时传真实条数）。
        force:  跳过节流立即落盘（告警场景用）。
    """
    if not key or n <= 0:
        return
    try:
        now = time.monotonic()
        emit: Optional[Dict[str, object]] = None
        with _LOCK:
            _COUNTS[key] = _COUNTS.get(key, 0) + n
            _PENDING[key] = _PENDING.get(key, 0) + n
            last = _LAST_EMIT.get(key)
            if force or last is None or (now - last) >= MIN_INTERVAL_S:
                emit = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "key": key,
                    "dropped": _PENDING[key],
                    "total": _COUNTS[key],
                    "reason": str(reason or "")[:200],
                }
                _PENDING[key] = 0
                _LAST_EMIT[key] = now
        if emit is not None:
            _append(emit)
    except Exception:
        pass


def _append(entry: Dict[str, object]) -> None:
    """追加一行 JSONL；失败只累加内部计数，不抛。"""
    global _WRITE_ERRORS
    try:
        path = os.path.abspath(LEDGER_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        with _LOCK:
            _WRITE_ERRORS += 1


def counts() -> Dict[str, int]:
    """当前进程内的丢写累计（key -> 次数）。可接进体检/看板。"""
    with _LOCK:
        return dict(_COUNTS)


def total() -> int:
    with _LOCK:
        return sum(_COUNTS.values())


def write_errors() -> int:
    with _LOCK:
        return _WRITE_ERRORS


def reset() -> None:
    """清空内存状态（只给测试用；不动已落盘的历史）。"""
    with _LOCK:
        _COUNTS.clear()
        _PENDING.clear()
        _LAST_EMIT.clear()
