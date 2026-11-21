# -*- coding: utf-8 -*-
"""v418（录入侧 P0）· 手填链也不许造死链

同日实测：库里 237 条零触发词链 + 305 条无品种链，全部"启用即死"（
`load_active_chains` 直接把 UNKNOWN 跳过），却在 UI 分母里占位。
自动建链的根因已经在 expander 里挡掉，这条用例守的是**手填路径**：
  1) 没有触发词 → 400（ValueError），不落库；
  2) 落库后判不出品种 → 自动 enabled=0 + note 标记 + 返回 warning；
  3) 正常链（因/果里写了品种、有触发词）→ enabled=1，asset 推导正确。
"""
import sqlite3
import sys

import pytest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import causal_chains as cc

LIVE_DB = BACKEND / "trident_event_bus.db"

# 本文件从真库只读拷 schema；公开版仓库不带真库，此时整模块 skip，
# 而不是在 import 期直接 OperationalError（那会让 CI 红而不是跳过）。
# 库文件在、表却被清空（数据纪元重置 / 换机）同样按"没有真库"处理。
_REQUIRED_TABLES = ("causal_chains", "ohlcv_daily", "causal_chain_outcomes")


def _live_schema_ready() -> bool:
    if not LIVE_DB.exists():
        return False
    try:
        with sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True) as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False
    return all(t in names for t in _REQUIRED_TABLES)


if not _live_schema_ready():
    pytest.skip("真库不存在或缺表（公开版 / 纪元重置后），跳过 schema 依赖用例",
                allow_module_level=True)


def _temp_db() -> sqlite3.Connection:
    """从真库只读拷 schema（不拷数据），造一个可写的临时库。"""
    live = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    ddls = {}
    for name in ("causal_chains", "ohlcv_daily", "causal_chain_outcomes"):
        row = live.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        assert row and row[0], f"真库缺少表 {name}"
        ddls[name] = row[0]
    live.close()
    conn = sqlite3.connect(":memory:")
    for ddl in ddls.values():
        conn.execute(ddl)
    # 一份最小行情：让 WTI 成为"有价资产"，asset 推导才认它
    conn.executemany(
        "INSERT INTO ohlcv_daily (asset, ts, open, high, low, close, volume)"
        " VALUES (?,?,?,?,?,?,?)",
        [("WTI", 1_700_000_000 + i * 86400, 80 + i, 81 + i, 79 + i, 80 + i, 1000)
         for i in range(10)],
    )
    conn.commit()
    return conn


def test_empty_trigger_words_rejected():
    conn = _temp_db()
    try:
        cc.add_chain("OPEC 减产", "WTI 原油价格上涨", trigger_words="", conn=conn)
    except ValueError as exc:
        assert "触发词" in str(exc)
    else:
        raise AssertionError("没有触发词的链居然落库了")
    assert conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0] == 0


def test_unknown_asset_chain_auto_disabled():
    conn = _temp_db()
    cc.add_chain("寒潮来袭", "居民用电需求上升", trigger_words="寒潮", conn=conn)
    row = conn.execute(
        "SELECT asset, enabled, note FROM causal_chains"
    ).fetchone()
    assert str(row[0] or "").upper() in ("UNKNOWN", "NONE", "", "NULL")
    assert int(row[1]) == 0, "判不出品种的链必须是停用状态"
    assert "待补品种" in (row[2] or "")


def test_update_recomputes_asset():
    """编辑后必须重算策略列 —— 否则"把链改对"也救不回来（老代码不调 derive）。"""
    conn = _temp_db()
    out = cc.add_chain("寒潮来袭", "居民用电需求上升", trigger_words="寒潮", conn=conn)
    cid = out["id"]
    assert out["enabled"] is False
    cc.update_chain(cid, "OPEC 减产", "WTI 原油价格上涨", trigger_words="减产,OPEC", conn=conn)
    row = conn.execute("SELECT asset, trigger_keywords FROM causal_chains WHERE id=?", (cid,)).fetchone()
    assert str(row[0]).upper() == "WTI", "编辑后 asset 没重算，链还是死的"
    assert "减产" in (row[1] or "")



def test_normal_chain_enabled_with_asset():
    conn = _temp_db()
    out = cc.add_chain("OPEC 减产", "WTI 原油价格上涨", trigger_words="OPEC,减产", conn=conn)
    assert out["ok"] is True and out["enabled"] is True and out["warning"] == ""
    row = conn.execute("SELECT asset, enabled FROM causal_chains").fetchone()
    assert str(row[0]).upper() == "WTI"
    assert int(row[1]) == 1
