# -*- coding: utf-8 -*-
"""v321.1: 主分析停摆三条不变量的回归单测。

事故背景（2026-11-04 生产实测）
------------------------------
1) 引擎崩溃重启循环 —— 主分析整体停摆
   engine/main.py::_ensure_db_exists() 启动时**无条件**跑 db.migrate()。
   migrate 在 1GB 库上 CREATE INDEX / ALTER TABLE，**全程持 SQLite 写锁**；
   引擎与 api_server、各 cron 同时启动时互相撞 SQLITE_BUSY →
   引擎在 main() 里直接抛 OperationalError: database is locked 崩掉 →
   launchd 立刻重启 → 再次撞锁 → 崩溃重启循环。
   实测：单份 engine.out.log 里 75 次 "engine starting"；期间主分析
   DONE 停在 06:45（最新一条 DONE 的时间戳），队列 22 行可认领却不被认领。
   api_server 侧早有 schema_ready() 只读快路径（实测 4ms，不申请写锁），
   所以只有引擎在崩 —— 这就是差异点。

2) 认领时重置 retry_count → 失败行无限重试
   v321 把 retry_count=0 写在 _batch_claim 的 UPDATE 里 → 每次认领都把已消耗
   的重试次数清零 → retry_count 永远到不了阈值(3) → 失败行永远退回 PENDING。
   实测：8 行卡在 rc=1 反复 120s 超时，既不 DONE 也不 FAILED。
   08:27 认领的那批 12 行，10 行全部 "LLM 超时(120s) 退回 PENDING 重试"。

3) 「给满 3 次机会」的语义放错了位置
   正确位置是**重新入队**（门卫回放解冻），不是**认领**。

本文件锁死的三条不变量：
  A. 认领**不**重置 retry_count
  B. 门卫回放解冻时把 retry_count 归零（给满 3 次机会）
  C. 引擎启动：schema 已就绪 → 跳过 migrate；migrate 撞写锁 → 不崩溃
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import realtime_filter as rt
from engine import ai_worker
from engine import gate_replay_guard
from engine import main as engine_main


RAW_NEWS_DDL = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT DEFAULT 'PENDING',
    is_noise INTEGER DEFAULT 0,
    gate_unavailable INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    processing_at TEXT,
    relevance_score REAL DEFAULT 0.0,
    content TEXT DEFAULT '',
    gate_verdict TEXT,
    gate_core_impact TEXT,
    gate_is_material INTEGER DEFAULT 0,
    gate_assets TEXT,
    gate_main_asset TEXT,
    gate_kind TEXT,
    gate_timing TEXT,
    gate_strength INTEGER DEFAULT 0,
    gate_impact_1to100 INTEGER DEFAULT 0
);
"""


_MIN_AI_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL
);
"""


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    c.executescript(RAW_NEWS_DDL)
    # v339: 认领 SQL 的去重子句会 SELECT ai_decisions，夹具只建 raw_news 会
    # "no such table: ai_decisions"。本文件不读这张表，建最小空表即可。
    c.executescript(_MIN_AI_DECISIONS_DDL)
    yield c
    c.close()


def _insert(conn, **kw):
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    conn.execute(
        "INSERT INTO raw_news (%s) VALUES (%s)" % (cols, marks),
        tuple(kw.values()),
    )
    conn.commit()
# ───────────────── 不变量 A：认领不重置 retry_count ─────────────────

def test_claim_does_not_reset_retry_count(conn):
    """已失败 2 次的行被认领时，retry_count 必须保持 2，不能被清零。"""
    _insert(conn, id=1, status="PENDING", is_noise=0, gate_unavailable=0, retry_count=2)

    rows = conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()

    assert len(rows) == 1, "应当认领这一行"
    got = conn.execute(
        "SELECT status, retry_count FROM raw_news WHERE id=1").fetchone()
    assert got["status"] == "PROCESSING"
    assert got["retry_count"] == 2, (
        "认领不得重置 retry_count —— 否则失败行永远回 PENDING 无限重试")


def test_claim_preserves_retry_count_across_claims(conn):
    """连续认领两次，retry_count 仍是原值（v321 会在这里把它清零）。"""
    _insert(conn, id=2, status="PENDING", is_noise=0, gate_unavailable=0, retry_count=1)

    conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()
    # 模拟失败回写：retry_count + 1，状态回 PENDING（阈值未到）
    conn.execute(
        "UPDATE raw_news SET status='PENDING', retry_count=retry_count+1 WHERE id=2")
    conn.commit()
    conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()

    got = conn.execute("SELECT retry_count FROM raw_news WHERE id=2").fetchone()
    assert got["retry_count"] == 2, "第二次认领后 retry_count 必须累加到 2"


def test_claim_skips_noise_and_gate_unavailable(conn):
    """噪音行与门卫未判定行都不认领（v320 语义不能被回归掉）。"""
    _insert(conn, id=3, status="PENDING", is_noise=1, gate_unavailable=0, retry_count=0)
    _insert(conn, id=4, status="PENDING", is_noise=0, gate_unavailable=1, retry_count=0)
    _insert(conn, id=5, status="PENDING", is_noise=0, gate_unavailable=0, retry_count=0)

    rows = conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()

    assert [r["id"] for r in rows] == [5]


# ───────────────── 不变量 B：回放解冻重置 retry_count ─────────────────

def test_replay_unfreeze_resets_retry_count(conn, monkeypatch):
    """门卫回放解冻 = 重新入队 → retry_count 归零，给满 3 次机会。"""
    _insert(conn, id=7, status="PENDING", is_noise=0, gate_unavailable=1,
            retry_count=2, content="Fed 意外加息 50bp")

    monkeypatch.setattr(
        rt, "_l3_semantic_gate",
        lambda title, content: {
            "verdict": "可交易", "strength": 8, "core_impact": "利率",
            "is_material": True, "assets": ["BTC"], "main_asset": "BTC",
            "kind": "宏观", "timing": "即时", "impact_1to100": 70,
        },
    )

    row = conn.execute("SELECT * FROM raw_news WHERE id=7").fetchone()
    res, detail = gate_replay_guard._replay_one(conn.cursor(), conn, row)
    assert res == "ok", detail

    got = conn.execute(
        "SELECT gate_unavailable, retry_count, is_noise FROM raw_news WHERE id=7"
    ).fetchone()
    assert got["gate_unavailable"] == 0
    assert got["retry_count"] == 0, "解冻必须重置 retry_count（重新入队给满 3 次机会）"
    assert got["is_noise"] == 0


# ───────────────── 不变量 C：启动不崩 ─────────────────

def test_engine_startup_skips_migrate_when_schema_ready(monkeypatch):
    """schema 已就绪 → 不得跑重 migrate（启动期不再抢写锁）。"""
    calls = []
    monkeypatch.setattr(engine_main.db, "get_connection",
                        lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(engine_main.db, "schema_ready", lambda c: True)
    monkeypatch.setattr(engine_main.db, "migrate",
                        lambda c: calls.append(1))
    monkeypatch.setattr("causal_chains.seed_defaults",
                        lambda conn: {"added": 0, "total": 0})

    engine_main._ensure_db_exists()

    assert calls == [], "schema 已就绪时不得执行 db.migrate（会持写锁 → 崩溃重启循环）"


def test_engine_startup_survives_locked_migrate(monkeypatch):
    """migrate 撞写锁 → 退避重试 → 仍抢不到锁但 schema 完整 → 降级继续，不崩。"""
    attempts = []

    def _migrate(_conn):
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    # 第一次 schema_ready=False（需要 migrate），之后一律 True
    seq = iter([False, True])

    monkeypatch.setattr(engine_main.db, "get_connection",
                        lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(engine_main.db, "migrate", _migrate)
    monkeypatch.setattr(engine_main.db, "schema_ready", lambda c: next(seq, True))
    monkeypatch.setattr("causal_chains.seed_defaults",
                        lambda conn: {"added": 0, "total": 0})
    monkeypatch.setattr(engine_main.time, "sleep", lambda s: None)

    engine_main._ensure_db_exists()   # 不抛异常 = 通过

    assert len(attempts) >= 1, "应当真的尝试过 migrate"


