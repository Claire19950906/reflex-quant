#!/usr/bin/env python3
"""v439 回归：写锁看门狗（lock_watchdog.py）。

背景（2026-11-17 21:42 事故）：写库停摆 8 分钟，当时靠人工逐个 SIGTERM
几十个进程才弄清"谁占着库"。看门狗把这个流程固化，这里只测它的两件事：
  1) 锁可用时必须立刻返回 True（不能给正常写入添堵）；
  2) 锁被占时能判定失败，且诊断函数在日志缺失时也不能崩。
"""
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
for p in (SRC, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import lock_watchdog as lw


def test_probe_free_db(tmp_path, monkeypatch):
    db = tmp_path / "free.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(lw, "DB", str(db))
    ok, cost = lw.probe_wait(2.0)
    assert ok is True
    assert cost < 2.0


def test_probe_deadlocked_db(tmp_path, monkeypatch):
    db = tmp_path / "locked.db"
    holder = sqlite3.connect(db, timeout=1)
    holder.execute("CREATE TABLE t(x INTEGER)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")   # 故意占住写锁
    monkeypatch.setattr(lw, "DB", str(db))
    ok, _ = lw.probe_wait(1.0)
    holder.execute("ROLLBACK")
    holder.close()
    assert ok is False


def test_diagnose_survives_missing_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(lw, "ALERT_LOG", str(tmp_path / "alert.log"))
    monkeypatch.setattr(lw, "TXN_LOG", str(tmp_path / "no_txn.log"))
    monkeypatch.setattr(lw, "LOG_DIR", str(tmp_path))
    lw.diagnose(3, [1.0, 2.0])
    text = (tmp_path / "alert.log").read_text(encoding="utf-8")
    assert "写锁停摆" in text
    assert "处置顺序" in text
