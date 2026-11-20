# -*- coding: utf-8 -*-
"""v406 P5：方向覆盖层的 DB 镜像（chain_direction_overlay）。

背景：369 条 LLM 方向标注原来只存在 `runtime/chain_direction/llm_labels.json`。
两个隐患：换机/清 runtime 目录标注凭空消失；想按"方向 = DOWN 且启用"查链
只能把 JSON 读进内存再过滤，SQL 侧无法审计。

这里锁四件事：
1. 建表幂等、写入是 upsert（重复同步不产生重复行）。
2. 非法方向 / 非法 id 不写（宁可不写，也不要塞垃圾进覆盖层）。
3. 读路径 DB 优先，缓存会在写之后失效。
4. 空写返回 0，不报错（调用方不需要 try）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import causal_chain_evaluator as ev


def _fresh_cache():
    ev._OVERLAY_DB_CACHE.update({"ts": 0.0, "map": {}})


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_table_is_idempotent():
    conn = _mem_conn()
    assert ev.ensure_overlay_table(conn) is True
    assert ev.ensure_overlay_table(conn) is True
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % ev.DIRECTION_OVERLAY_TABLE)]
    assert cols == ["chain_id", "direction", "source", "confidence", "updated_at", "note"]


def test_sync_upserts_not_duplicates():
    conn = _mem_conn()
    assert ev.sync_overlay_to_db(conn, {"1": "UP", "2": "DOWN"}) == 2
    assert ev.sync_overlay_to_db(conn, {"2": "BOTH", "3": "UP"}) == 2
    rows = conn.execute(
        "SELECT chain_id, direction FROM %s ORDER BY chain_id" % ev.DIRECTION_OVERLAY_TABLE
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "UP"), (2, "BOTH"), (3, "UP")]


def test_sync_rejects_junk():
    conn = _mem_conn()
    ev.ensure_overlay_table(conn)
    assert ev.sync_overlay_to_db(conn, {"abc": "UP", "4": "SIDEWAYS", "5": ""}) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM %s" % ev.DIRECTION_OVERLAY_TABLE).fetchone()[0] == 0


def test_sync_empty_is_noop():
    assert ev.sync_overlay_to_db(_mem_conn(), {}) == 0
    assert ev.sync_overlay_to_db(_mem_conn(), None) == 0


def test_overlay_from_db_reads_and_caches(monkeypatch, tmp_path):
    db_file = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    ev.ensure_overlay_table(conn)
    ev.sync_overlay_to_db(conn, {"11": "DOWN", "12": "UP"})
    conn.close()

    import db as dbmod
    monkeypatch.setattr(dbmod, "get_db_path", lambda: str(db_file))
    _fresh_cache()
    assert ev._overlay_from_db() == {"11": "DOWN", "12": "UP"}

    # 缓存生效：直接改库但不动缓存，读到的仍是旧值
    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE %s SET direction='BOTH'" % ev.DIRECTION_OVERLAY_TABLE)
    conn.commit()
    assert ev._overlay_from_db() == {"11": "DOWN", "12": "UP"}
    # 显式失效后才拿到新值
    _fresh_cache()
    assert ev._overlay_from_db() == {"11": "BOTH", "12": "BOTH"}
    conn.close()


def test_overlay_from_db_missing_table_is_empty(monkeypatch, tmp_path):
    db_file = tmp_path / "empty.db"
    sqlite3.connect(str(db_file)).close()
    import db as dbmod
    monkeypatch.setattr(dbmod, "get_db_path", lambda: str(db_file))
    _fresh_cache()
    assert ev._overlay_from_db() == {}


def test_direction_for_write_prefers_overlay(monkeypatch):
    # 覆盖层有标注时，规则法不许赢（v322 实测规则法 41.9%，低于随机）
    monkeypatch.setattr(ev, "llm_direction_overlay", lambda: {"99": "DOWN"})
    out = ev._direction_for_write("金价大涨带动避险", "利多", "", "", "", chain_id=99)
    assert out == "DOWN"


def test_direction_source_honoured(monkeypatch):
    monkeypatch.setattr(ev, "llm_direction_overlay", lambda: {})
    out = ev._direction_for_write("whatever", "", "", "UP", "llm", chain_id=None)
    assert out == "UP"
