# -*- coding: utf-8 -*-
"""schema_migrations 注册表与幂等执行测试。"""
from __future__ import annotations

import sqlite3

import schema_migrations as sm


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    return conn


def test_baseline_version_respected():
    conn = _mem()
    assert sm.get_user_version(conn) == 0
    # 空注册表 → 无待执行迁移
    assert sm.pending_migrations(conn) == []


def test_dry_run_does_not_raise_version():
    conn = _mem()
    before = sm.get_user_version(conn)
    res = sm.run_pending(conn, real_run=False)
    assert res["dry_run"] is True
    assert sm.get_user_version(conn) == before


def test_migration_applied_and_idempotent():
    conn = _mem()
    # 临时登记一条补列迁移
    m = sm.Migration(
        version=24,
        description="测试补列",
        up=lambda c: sm._ensure_column(c, "t", "tag", "TEXT DEFAULT ''"),
    )
    orig = sm.MIGRATIONS[:]
    sm.MIGRATIONS.append(m)
    try:
        res = sm.run_pending(conn)
        assert res["applied"] == [24]
        assert sm.get_user_version(conn) == 24
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
        assert "tag" in cols
        # 再跑一次：无待执行
        again = sm.run_pending(conn)
        assert again["applied"] == []
        assert sm.get_user_version(conn) == 24
    finally:
        sm.MIGRATIONS[:] = orig


def test_failed_migration_does_not_raise_version():
    conn = _mem()

    def bad(_c):
        raise sqlite3.OperationalError("boom")

    m = sm.Migration(version=25, description="坏迁移", up=bad)
    orig = sm.MIGRATIONS[:]
    sm.MIGRATIONS.append(m)
    try:
        res = sm.run_pending(conn)
        assert res["error"] and "boom" in res["error"]
        assert sm.get_user_version(conn) == 0  # 失败不抬版本
    finally:
        sm.MIGRATIONS[:] = orig
