# -*- coding: utf-8 -*-
"""v423 规范因子本体 + 输出刻度的契约测试。

覆盖：
  1. 别名映射：机制词归到规范因子，列名碎片/纯品种词不归；
  2. `_upsert_node` 落 `factor_key`；
  3. `_reconcile` 慢变量分层衰减：同版本下慢因子不被杀、快因子照常衰减；
  4. `_node_stats` 的 n/命中率/Wilson 下界；
  5. `get_knowledge` 的跨品种同源；
  6. `inject_block_json` 字段与文本版同口径。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import causal_graph as cg
import factor_ontology as fo

SCHEMA = """
CREATE TABLE asset_driver_nodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, label TEXT, kind TEXT,
    is_core INTEGER, confidence REAL, evidence TEXT, version INTEGER,
    active INTEGER, updated_at TEXT, factor_key TEXT);
CREATE UNIQUE INDEX ux_nodes ON asset_driver_nodes(asset, label);
CREATE TABLE asset_driver_edges(
    id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER,
    kind TEXT, weight REAL, evidence TEXT, updated_at TEXT);
CREATE UNIQUE INDEX ux_edges ON asset_driver_edges(from_id, to_id, kind);
CREATE TABLE causal_chains(
    id INTEGER PRIMARY KEY, name TEXT, cause TEXT, effect TEXT,
    weight REAL, enabled INTEGER, asset TEXT, trigger_words TEXT,
    hit_count INTEGER, miss_count INTEGER, active INTEGER);
"""


@pytest.fixture
def gdb(tmp_path, monkeypatch):
    path = tmp_path / "ont.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    def _factory():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(cg.db, "get_connection", _factory)
    monkeypatch.setattr(cg, "_SCHEMA_TOKENS_CACHE", None)
    fo.ensure_tables(sqlite3.connect(str(path)))
    return path


def _q(path, sql, params=()):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec(path, sql, params=()):
    conn = sqlite3.connect(str(path))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


# ── 1. 映射 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,key", [
    ("美国实际利率与美元信用预期", "real_rate"),
    ("全球地缘风险溢价与避险需求", "risk_premium"),
    ("通胀预期与实际购买力对冲需求", "inflation"),
    ("资金费率与杠杆清算", "crypto_structure"),
    ("funding/cluster_heat 指标同步异动", None),
    ("news_sentiment", None),
    ("btc趋势,市场确认与结构状态", "tech_structure"),
])
def test_map_label(label, key):
    assert fo.map_label(label) == key


def test_canonical_names_are_short_and_chinese():
    for f in fo.all_factors():
        assert fo.canonical_label_safe(f["key"]), f["key"]
        assert len(f["canonical"]) <= 14


def test_seed_ontology_is_idempotent(gdb):
    conn = sqlite3.connect(str(gdb))
    n1 = fo.seed_ontology(conn)
    n2 = fo.seed_ontology(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM factor_ontology").fetchone()[0]
    conn.close()
    assert n1 == n2 == len(fo.all_factors())
    assert count == len(fo.all_factors())


# ── 2. 入库带 factor_key ─────────────────────────────────────────────────────

def test_upsert_node_writes_factor_key(gdb):
    conn = sqlite3.connect(str(gdb))
    conn.row_factory = sqlite3.Row
    nid = cg._upsert_node(conn, "XAU", "美国实际利率与美元信用预期", "core", True, 0.5, "[]")
    conn.commit()
    row = conn.execute("SELECT factor_key FROM asset_driver_nodes WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row["factor_key"] == "real_rate"


# ── 3. 慢变量衰减分层 ────────────────────────────────────────────────────────

def test_reconcile_protects_slow_factors(gdb, monkeypatch):
    import config
    monkeypatch.setattr(config, "adaptive_protect_version", lambda n: 3)
    monkeypatch.setattr(config, "adaptive_decay_factor", lambda n: (0.7, 0.85))

    conn = sqlite3.connect(str(gdb))
    conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at) "
        "VALUES ('XAU','美国实际利率与美元信用预期','core',1,0.2,'[]',5,1,'t')")
    conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at) "
        "VALUES ('XAU','短期事件驱动噪声','core',1,0.2,'[]',5,1,'t')")
    conn.commit()

    cg._reconcile(conn, "XAU", core_labels=[], cond_labels=[])
    conn.commit()
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT label,confidence,active FROM asset_driver_nodes").fetchall()}
    conn.close()
    # 慢因子：保护期 3*2+1=7 > version 5 → 原样保留
    assert rows["美国实际利率与美元信用预期"][1] == 1
    assert rows["美国实际利率与美元信用预期"][0] == pytest.approx(0.2)
    # 快因子（归不到因子）：照常衰减 0.2*0.7=0.14 < 0.2 → 失效
    assert rows["短期事件驱动噪声"][1] == 0


# ── 4/5/6. 输出刻度、跨品种、JSON ────────────────────────────────────────────

def _mk_factor_node(gdb, asset, label, key, conf=0.5):
    conn = sqlite3.connect(str(gdb))
    conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at,factor_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (asset, label, "core", 1, conf, json.dumps({"chain": ["机制成立"], "_status": "UNCONFIRMED"}, ensure_ascii=False),
         1, 1, "t", key))
    nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    eid = conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (asset, "[证据] %s" % asset.lower(), "evidence", 0, 0.5, "", 1, 1, "t")).lastrowid
    conn.commit()
    conn.close()
    return nid, eid


def test_node_stats_and_wilson(gdb):
    nid, eid = _mk_factor_node(gdb, "XAU", "美国实际利率", "real_rate")
    _exec(gdb, "INSERT INTO asset_driver_edges(from_id,to_id,kind,weight,evidence,updated_at) VALUES (?,?,?,?,?,?)",
          (eid, nid, "supports", 5, "", "t"))
    _exec(gdb, "INSERT INTO asset_driver_edges(from_id,to_id,kind,weight,evidence,updated_at) VALUES (?,?,?,?,?,?)",
          (eid, nid, "contradicts", 2, "", "t"))
    conn = sqlite3.connect(str(gdb))
    st = cg._node_stats(conn, [nid])[nid]
    conn.close()
    assert st["n"] == 7 and st["hits"] == 5
    assert 0.29 < st["wilson_low"] < 0.72
    assert st["status"] == "未定"


def test_get_knowledge_cross_asset_and_json(gdb):
    xau_id, _ = _mk_factor_node(gdb, "XAU", "美国实际利率与美元信用预期", "real_rate")
    _mk_factor_node(gdb, "BTC", "美元实际利率与全球流动性", "real_rate")
    conn = sqlite3.connect(str(gdb))
    conn.close()

    k = cg.get_knowledge("XAU")
    assert k["core_factors"][0]["factor_key"] == "real_rate"
    assert k["core_factors"][0]["n"] == 0
    cross = {c["key"]: c for c in k["cross_asset"]}
    assert "real_rate" in cross and "BTC" in cross["real_rate"]["assets"]

    js = cg.inject_block_json("XAU")
    assert js["asset"] == "XAU"
    assert js["core_factors"][0]["factor_key"] == "real_rate"
    assert "cross_asset" in js and "total_samples" in js


def test_get_graph_includes_macro_neighbour(gdb):
    xau_id, _ = _mk_factor_node(gdb, "XAU", "美国实际利率与美元信用预期", "real_rate")
    conn = sqlite3.connect(str(gdb))
    mid = conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at,factor_key) "
        "VALUES ('MACRO','实际利率','macro',0,0.5,'[]',1,1,'t','real_rate')").lastrowid
    conn.execute(
        "INSERT INTO asset_driver_edges(from_id,to_id,kind,weight,evidence,updated_at) "
        "VALUES (?,?,'instance_of',1,'','t')", (xau_id, mid))
    conn.commit()
    conn.close()

    g = cg.get_graph("XAU")
    ids = {n["id"] for n in g["nodes"]}
    assert mid in ids
    assert any(e["kind"] == "instance_of" for e in g["edges"])
    assert any(n["kind"] == "macro" for n in g["nodes"])


def test_contradicted_slow_core_is_structural_only(gdb):
    """慢核被旧核验否证过：输出要如实降级为"结构位"，不能既叫核心又当方向依据。"""
    conn = sqlite3.connect(str(gdb))
    conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at,factor_key) "
        "VALUES ('XAU','美国实际利率','core',1,0.2,?,1,1,'t','real_rate')",
        (json.dumps({"chain": ["实际利率下行利多黄金"], "_status": "CONTRADICTED",
                     "_fail": "WRONG_INFERENCE"}, ensure_ascii=False),))
    conn.commit()
    conn.close()

    k = cg.get_knowledge("XAU")
    assert k["core_factors"][0]["structural_only"] is True
    block = cg.inject_block("XAU")
    assert "旧核验否证" in block and "仅结构位" in block


def test_log_injection_never_raises(gdb, monkeypatch):
    import db as _db
    monkeypatch.setattr(_db, "get_db_path", lambda: str(gdb))
    fo.log_injection("XAU", "美国实际利率（归因:实际利率）/ 地缘（归因:地缘政治风险）", mode="batch")
    row = _q(gdb, "SELECT asset,mode,factors FROM causal_injection_log")[-1]
    assert row["asset"] == "XAU" and row["mode"] == "batch"
    assert "real_rate" in row["factors"] and "geopolitics" in row["factors"]
    # 空 block / 非法 asset 也不能抛
    fo.log_injection("", "", mode="batch")
