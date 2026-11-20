# -*- coding: utf-8 -*-
"""通知去重器兜底语义回归。

背景：fed_rate_notifier / cross_asset_anomaly_notifier 的 `_record_and_dedup`
原先把 `return True` 写在 `finally` 块里（close 失败才触发），既吞掉 try 异常，
又达不到"表失败/库锁 → 兜底当成该推"的意图。重构为：DB 操作异常在 `except` 里
返回 True，`finally` 只负责关连接、不再 return。

这两个用例锁住新语义：去重表抛异常时通知不静默丢失，兜底返回"应推送"。
"""
from __future__ import annotations

import sqlite3

from engine import fed_rate_notifier as fed_notif
from engine import cross_asset_anomaly_notifier as cross_notif


class _BoomConnection:
    """模拟已连接成功、但任意 SQL 操作抛库锁异常的假连接。"""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        pass

    def close(self):
        pass


def test_fed_dedup_db_error_falls_back_to_push(monkeypatch):
    monkeypatch.setattr(fed_notif.sqlite3, "connect", lambda *a, **k: _BoomConnection())
    # 库锁/表失败 → 兜底当成"该推"，不抛异常、不静默丢通知。
    assert fed_notif._record_and_dedup({"severity": "high"}, "high") is True


def test_cross_dedup_db_error_falls_back_to_push(monkeypatch):
    monkeypatch.setattr(cross_notif.sqlite3, "connect", lambda *a, **k: _BoomConnection())
    assert cross_notif._record_and_dedup("high", "up") is True
