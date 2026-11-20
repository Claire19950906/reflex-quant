# -*- coding: utf-8 -*-
"""v406：降级保留原始方向 / 范围前置过滤的保守边界。

两条都不碰交易判定，但都直接影响「账本上还能不能看到模型原本说了什么」。
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import pytest


# ─────────────────────────────────────────── ① 降级不删除原始方向
def _focus_downgrade():
    from engine.ai_worker import _focus_downgrade as fn
    return fn


def test_downgrade_keeps_raw_action_and_score():
    """主品种被降级成「分析失败」时，模型原本的 BUY/score 不能消失。"""
    fn = _focus_downgrade()
    res = {
        "target_asset": "BTC",
        "suggested_action": "BUY",
        "sentiment_score": 0.62,
        "reasoning_path": "[总框架] 无\n[总结] 只谈宏观",
    }
    downgraded, hits = fn(res, "BTC", res["reasoning_path"])
    assert downgraded and hits == 0
    assert res["suggested_action"] == "FAILED"       # 仍然不可下单
    assert res["sentiment_score"] == 0.0             # 仍然抹零
    assert res["_raw_action"] == "BUY"               # 但原始方向留痕
    assert res["_raw_score"] == 0.62                 # 原始分数留痕


def test_downgrade_does_not_touch_hold_rows():
    """本来就是 HOLD 的行不该被「降级」改写，也不该凭空多出 raw 字段。"""
    fn = _focus_downgrade()
    res = {"target_asset": "BTC", "suggested_action": "HOLD", "sentiment_score": 0.05,
           "reasoning_path": "[总框架] 无"}
    downgraded, hits = fn(res, "BTC", res["reasoning_path"])
    assert downgraded is False
    assert res["suggested_action"] == "HOLD"
    assert "_raw_action" not in res


# ─────────────────────────────────────────── ② 前置过滤：默认关，判不准就留
def test_prefilter_default_on_and_can_be_disabled(monkeypatch):
    """v412-G 起默认开启（两个判据 bug 修完、14 天全量复抽只剩 0.01% 误杀）。

    旧断言是"默认关"，那是收紧判据之前的口径；开关仍在：ANALYSIS_PREFILTER=0 可关。
    """
    from analysis import prefilter as pf
    monkeypatch.delenv(pf.PREFILTER_ENV, raising=False)
    assert pf.enabled() is True, "v412-G 起默认开启"
    monkeypatch.setenv(pf.PREFILTER_ENV, "0")
    assert pf.enabled() is False, "显式 0 必须能关掉"
    monkeypatch.setenv(pf.PREFILTER_ENV, "1")
    assert pf.enabled() is True


def test_prefilter_keeps_no_evidence():
    """门卫什么都没给（no_evidence）是最容易误杀的桶，必须保留。"""
    from analysis.prefilter import judge
    row = {"id": 1, "content": "某公司发布公告，具体影响待观察。",
           "gate_main_asset": "", "gate_assets": ""}
    drop, why = judge(row)
    assert drop is False


def test_prefilter_drops_plain_stock_bucket():
    from analysis.prefilter import judge
    row = {"id": 2, "content": "英特尔公司股价在盘后下跌1%。",
           "gate_main_asset": "个股", "gate_assets": '["个股"]'}
    drop, why = judge(row)
    assert drop is True and why == "bucket"


def test_prefilter_apply_is_a_noop_when_disabled(monkeypatch):
    from analysis import prefilter as pf
    monkeypatch.delenv(pf.PREFILTER_ENV, raising=False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, content TEXT,"
                 " gate_main_asset TEXT, gate_assets TEXT, status TEXT, filter_reason TEXT)")
    conn.execute("INSERT INTO raw_news VALUES (1, '英特尔股价下跌', '个股', '[\"个股\"]', 'PROCESSING', '')")
    rows = conn.execute("SELECT * FROM raw_news").fetchall()
    kept = pf.apply(conn, rows)
    assert len(kept) == 1
    assert conn.execute("SELECT status FROM raw_news WHERE id=1").fetchone()[0] == "PROCESSING"
