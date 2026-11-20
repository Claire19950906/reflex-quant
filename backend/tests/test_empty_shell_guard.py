# -*- coding: utf-8 -*-
"""空壳库守卫测试：连到 0 表空壳要告警并落账本；有表库不误报；缓存去重。"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest


@pytest.fixture
def _isolated_ledger(tmp_path, monkeypatch):
    import silent_ledger
    import db
    ledger = tmp_path / "silent_drops.jsonl"
    monkeypatch.setenv("TRIDENT_SILENT_LEDGER", str(ledger))
    # 显式开启空壳守卫（绕过 pytest 进程默认豁免）：测试要验证守卫本身必须打开。
    monkeypatch.setenv("TRIDENT_SHELL_GUARD", "on")
    monkeypatch.setattr(silent_ledger, "LEDGER_PATH", str(ledger))
    monkeypatch.setattr(db, "silent_ledger", silent_ledger, raising=False)
    # 重置账本内存计数，避免跨用例污染
    silent_ledger._COUNTS.clear()
    silent_ledger._PENDING.clear()
    silent_ledger._LAST_EMIT.clear()
    yield ledger


def test_empty_shell_records_to_ledger(tmp_path, _isolated_ledger):
    import db
    empty = tmp_path / "shell.db"
    conn = sqlite3.connect(str(empty))
    db._EMPTY_SHELL_CACHE.clear()
    db._warn_if_empty_shell(conn, str(empty))
    conn.close()
    assert _isolated_ledger.exists(), "空壳库必须落账本留痕"
    txt = _isolated_ledger.read_text(encoding="utf-8")
    assert "db.empty_shell" in txt


def test_real_db_not_flagged(tmp_path, _isolated_ledger):
    import db
    real = tmp_path / "real.db"
    conn = sqlite3.connect(str(real))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    db._EMPTY_SHELL_CACHE.clear()
    db._warn_if_empty_shell(conn, str(real))
    conn.close()
    # 有表库不应记空壳告警
    assert not _isolated_ledger.exists()


def test_empty_shell_cache_dedups(tmp_path, _isolated_ledger):
    import db
    empty = tmp_path / "shell.db"
    c1 = sqlite3.connect(str(empty))
    db._EMPTY_SHELL_CACHE.clear()
    db._warn_if_empty_shell(c1, str(empty))
    c1.close()
    c2 = sqlite3.connect(str(empty))
    db._warn_if_empty_shell(c2, str(empty))
    c2.close()
    lines = [ln for ln in _isolated_ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, "同一空壳库二次连接走缓存，不重复记账"
