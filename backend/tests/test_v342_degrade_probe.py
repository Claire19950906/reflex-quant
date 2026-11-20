# -*- coding: utf-8 -*-
"""v342 — 降级探针（llm_degrade_probe 回归测试）

钉死三件事：
  1. 五类兜底按 reasoning_path 特征串正确归类（含 markdown `**` 的坑）
  2. 真实八段不被误判成降级
  3. record 写库失败不抛（探针不许影响主流程）；stats 能按类型聚合
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import llm_degrade_probe as ldp


def test_classify_in_scope_full_degrade():
    path = "[总框架] 目标资产 XAU；本次 **LLM 未返回可用分析**（空输出或解析前被截断）"
    assert ldp.classify_path(path) == ldp.ARCH_FULL_IN


def test_classify_out_of_scope_full_degrade():
    path = "[总框架] 目标资产 NONE；该标的**不在**核心监控清单（BTC/XAU/WTI/ETH/SOL），按非交易信号处理"
    assert ldp.classify_path(path) == ldp.ARCH_FULL_OUT


def test_classify_summary_only_and_short():
    assert ldp.classify_path("[驱动力] LLM 真东西摘要：CPI 回落利好风险资产") == ldp.ARCH_RS_SUMMARY
    assert ldp.classify_path("[LLM 摘要] 偏空\n[兜底补全] 未展开八段") == ldp.ARCH_SHORT


def test_classify_real_eight_section_is_ok():
    path = ("[总框架] 目标资产 XAU；[因素定位] CPI 超预期；[驱动力] 实际利率上行；"
            "[水位博弈] 多头拥挤；[跨资产资金] 美元走强；[反共识结论] BUY；"
            "[逻辑自检] 自洽；[总结] 结论")
    assert ldp.classify_path(path) == ldp.ARCH_OK
    assert ldp.classify_path("") == ldp.ARCH_OK
    assert ldp.classify_path(None) == ldp.ARCH_OK


def test_record_and_stats(tmp_path):
    path = str(tmp_path / "p.db")
    conn = sqlite3.connect(path)
    ldp.init_schema(conn)
    ldp.record(conn, news_id=1, asset="xau",
               result={"reasoning_path": "[总框架] 目标资产 XAU；本次 **LLM 未返回可用分析**",
                       "reasoning": "空", "suggested_action": "hold",
                       "sentiment_score": 0.0})
    ldp.record(conn, news_id=2, asset="btc",
               result={"reasoning_path": "[总框架] 目标资产 BTC；[因素定位] 真实分析",
                       "reasoning": "看多", "suggested_action": "BUY",
                       "sentiment_score": 0.4})
    conn.commit()
    s = ldp.stats(conn, days=3650)
    assert s["total"] == 2
    assert s["by_archetype"][ldp.ARCH_FULL_IN]["n"] == 1
    assert s["by_archetype"][ldp.ARCH_OK]["n"] == 1
    assert s["by_archetype"][ldp.ARCH_OK]["holds"] == 0
    conn.close()


def test_record_never_raises_without_table():
    conn = sqlite3.connect(":memory:")      # 没有建表
    assert ldp.record(conn, news_id=3, asset="XAU",
                      result={"reasoning_path": "x"}) == ldp.ARCH_OK
    assert ldp.stats(conn, days=30)["ok"] is True
