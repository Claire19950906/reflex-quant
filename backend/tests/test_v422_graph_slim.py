# -*- coding: utf-8 -*-
"""v422 P0：因果图谱瘦身的三处契约测试。

覆盖：
  1. seed_from_causal_chains 默认不再往 asset='all' 写 macro 节点（atomic 模式才写，且过校验）；
  2. persist_evidence_edges 每资产只留一个稳定证据锚点，边权=本次票数（覆盖而非累加），票为 0 时删边；
  3. 注入前的证据过滤：跨品种 / 执行层 / 列名碎片不进 prompt。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import causal_graph as cg

SCHEMA = """
CREATE TABLE asset_driver_nodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, label TEXT, kind TEXT,
    is_core INTEGER, confidence REAL, evidence TEXT, version INTEGER,
    active INTEGER, updated_at TEXT);
CREATE UNIQUE INDEX ux_nodes ON asset_driver_nodes(asset, label);
CREATE TABLE asset_driver_edges(
    id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER,
    kind TEXT, weight REAL, evidence TEXT, updated_at TEXT);
CREATE UNIQUE INDEX ux_edges ON asset_driver_edges(from_id, to_id, kind);
CREATE TABLE causal_chains(
    id INTEGER PRIMARY KEY, name TEXT, cause TEXT, effect TEXT,
    weight REAL, enabled INTEGER);
"""


@pytest.fixture
def gdb(tmp_path, monkeypatch):
    """临时 sqlite 文件 + 把 causal_graph.db.get_connection 指过去（测试绝不碰生产库）。"""
    path = tmp_path / "graph_test.db"
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
    return path


def _query(path, sql):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# ── 1. seed 入口 ─────────────────────────────────────────────────────────────

def test_seed_default_does_not_write_all_bucket(gdb):
    conn = sqlite3.connect(str(gdb))
    conn.execute(
        "INSERT INTO causal_chains(name,cause,effect,weight,enabled) VALUES (?,?,?,?,?)",
        ("地缘冲突→黄金避险", "2026-10-03也门胡塞武装袭击沙特阿美石油设施并推高避险买盘", "XAU 上行", 0.6, 1),
    )
    conn.commit()
    conn.close()

    r = cg.seed_from_causal_chains()
    assert r["macro_mode"] == "off"
    assert r["assets_ensured"] >= 1
    assert _query(gdb, "SELECT COUNT(*) n FROM asset_driver_nodes WHERE asset='all'")[0]["n"] == 0
    assets = {x["asset"] for x in _query(gdb, "SELECT asset,kind FROM asset_driver_nodes WHERE kind='asset'")}
    assert "XAU" in assets


def test_seed_atomic_only_accepts_short_non_event_cause(gdb):
    conn = sqlite3.connect(str(gdb))
    conn.executemany(
        "INSERT INTO causal_chains(name,cause,effect,weight,enabled) VALUES (?,?,?,?,?)",
        [
            ("实际利率→黄金", "实际利率下行", "XAU 上行", 0.7, 1),
            ("事件→黄金", "2026-10-07美联储官员讲话后美元冲高回落", "XAU 上行", 0.6, 1),
        ],
    )
    conn.commit()
    conn.close()

    r = cg.seed_from_causal_chains(macro_mode="atomic")
    assert r["nodes_added"] == 1
    assert r["skipped_macro"] == 1
    labels = [x["label"] for x in _query(gdb, "SELECT label FROM asset_driver_nodes WHERE asset='all'")]
    assert labels == ["实际利率下行"]


# ── 2. 证据边 ────────────────────────────────────────────────────────────────

def _add_hypothesis(gdb, label="全球实际利率与流动性定价", asset="BTC"):
    conn = sqlite3.connect(str(gdb))
    conn.execute(
        "INSERT INTO asset_driver_nodes(asset,label,kind,is_core,confidence,evidence,version,active,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (asset, label, "core", 1, 0.5, "", 1, 1, "2026-11-17 00:00:00"),
    )
    nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return nid


def test_persist_uses_single_stable_anchor_and_absolute_weight(gdb):
    nid = _add_hypothesis(gdb)

    cg.persist_evidence_edges("BTC", results=[{"node_id": nid, "support_count": 5, "contradict_count": 2}])
    assert _query(gdb, "SELECT COUNT(*) n FROM asset_driver_nodes WHERE kind='evidence'")[0]["n"] == 1
    edges = _query(gdb, "SELECT kind,weight FROM asset_driver_edges")
    assert {e["kind"]: e["weight"] for e in edges} == {"supports": 5.0, "contradicts": 2.0}

    # 第二次票数变了：权重要覆盖，不是累加（否则同一批事实被重复计票）
    cg.persist_evidence_edges("BTC", results=[{"node_id": nid, "support_count": 9, "contradict_count": 0}])
    edges = _query(gdb, "SELECT kind,weight FROM asset_driver_edges")
    assert {e["kind"]: e["weight"] for e in edges} == {"supports": 9.0}
    assert _query(gdb, "SELECT COUNT(*) n FROM asset_driver_nodes WHERE kind='evidence'")[0]["n"] == 1


def test_persist_zero_votes_removes_stale_edge(gdb):
    nid = _add_hypothesis(gdb)
    cg.persist_evidence_edges("BTC", results=[{"node_id": nid, "support_count": 3, "contradict_count": 1}])
    cg.persist_evidence_edges("BTC", results=[{"node_id": nid, "support_count": 0, "contradict_count": 1}])
    edges = _query(gdb, "SELECT kind FROM asset_driver_edges")
    assert [e["kind"] for e in edges] == ["contradicts"]


# ── 3. 注入前过滤 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("资金/费率结构与波动率聚类(funding/cluster_heat/volatility)", True),
    ("market_event 与 prompt 混入", True),
    ("BTC 现货 ETF 资金流", False),
    ("US10Y 实际利率", False),
])
def test_has_schema_fragment(text, expected):
    assert cg._has_schema_fragment(text) is expected


def test_clean_why_drops_foreign_meta_and_schema():
    items = {"chain": [
        "实际利率下行压低美元并支撑风险资产",
        "XAU多头反应更快",
        "同一事件重复入场",
        "funding/cluster_heat 指标同步异动",
        "因为",
    ]}
    out = cg._clean_why(items, limit=5, asset="BTC")
    assert out == ["实际利率下行压低美元并支撑风险资产"]


def test_clean_why_keeps_phrase_that_names_own_asset():
    items = ["BTC 与 XAU 的避险属性共振", "实际利率下行"]
    out = cg._clean_why(items, limit=5, asset="BTC")
    assert "BTC 与 XAU 的避险属性共振" in out


def test_commodity_family_not_treated_as_foreign():
    # 油冲击传导至金价是真结构，不能当跨品种噪声清掉
    assert cg._mentions_foreign_asset("XAU", "WTI 供给冲击推升通胀预期") is False


@pytest.mark.parametrize("name,expected", [
    ("同一事件重复入场", True),
    ("资金/费率结构与波动率聚类(funding/cluster_heat/volatility)", True),
    ("市场技术结构与未平仓止盈纪律(趋势兑现条件)", True),
    ("全球实际利率与流动性定价", False),
    ("eth等加密主流币不涨反跌", False),
])
def test_junk_factor_name(name, expected):
    assert cg._is_junk_factor_name(name) is expected


def test_keep_for_prompt_drops_factor_whose_evidence_is_all_junk():
    f = {"factor": "事件驱动交易的微观结构与规则",
         "why_chain": ["首笔盈利被后续冗余入场对冲、品种错配放大收益不兑现风险"]}
    assert cg._keep_for_prompt("SPX", f) is False


def test_keep_for_prompt_keeps_factor_with_usable_evidence():
    f = {"factor": "全球实际利率与流动性定价",
         "why_chain": ["无风险利率和美元流动性影响加密资产估值"]}
    assert cg._keep_for_prompt("BTC", f) is True
