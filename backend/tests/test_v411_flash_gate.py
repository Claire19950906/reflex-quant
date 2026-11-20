"""v411：快讯采集器过闸（flash_gate）单元测试。

背景：`sina_flash.py` / `wscn_flash.py` 原来是裸 INSERT，绕过整条过滤器 ——
实测近 3 小时非噪音新闻 134 条里 76 条（57%）没有门卫判定就进了主分析队列。
这里守住三件事：判噪音不入库、正常入库带门卫列、过滤器挂了不丢新闻。
"""
import os

import pytest

import flash_gate


def test_noise_is_skipped():
    """门卫/过滤器判噪音 → 返回 None，调用方不应入库。"""
    def fake_eval(title, content, published_at="", ingested_ts=None):
        return {"is_noise": 1, "relevance_score": 0.0, "status": "FILTERED",
                "verdict": "L1", "gate": None}

    assert flash_gate.gate_columns("某明星演唱会门票售罄", _evaluate=fake_eval) is None


def test_pass_carries_gate_columns():
    def fake_eval(title, content, published_at="", ingested_ts=None):
        return {"is_noise": 0, "relevance_score": 0.9, "status": "PENDING",
                "gate": {"verdict": "可交易", "strength": 3, "main_asset": "原油"},
                "gate_verdict": "可交易", "gate_unavailable": 0}

    cols = flash_gate.gate_columns("OPEC 宣布减产", _evaluate=fake_eval)
    assert cols["is_noise"] == 0
    assert cols["relevance_score"] == 0.9
    assert cols["gate_verdict"] == "可交易"
    assert cols["gate_unavailable"] == 0
    assert cols["gate_strength"] == 3
    assert cols["gate_main_asset"] == "原油"
    assert cols["filter_reason"] == ""  # v453 P1-4：进了队列就不写过滤原因


def test_gate_unavailable_is_preserved_not_hidden():
    """门卫不可用时，标记必须落到库里（1=冻结待回放 / 2=降级放行），不能伪装成"没判过"。"""
    def fake_eval(title, content, published_at="", ingested_ts=None):
        return {"is_noise": 0, "relevance_score": 0.5, "status": "PENDING",
                "gate": {"verdict": "未判定"}, "gate_verdict": "未判定",
                "gate_unavailable": 1}

    cols = flash_gate.gate_columns("美国公布 AI 出口计划", _evaluate=fake_eval)
    assert cols["gate_unavailable"] == 1
    assert cols["gate_verdict"] == "未判定"
    assert cols["filter_reason"] == ""  # v453 P1-4：门卫不可用≠被过滤


def test_evaluate_exception_falls_back_to_insert():
    """过滤器抛异常 → 退回旧行为（照常入库），绝不因为过滤挂了丢新闻。"""
    def boom(*a, **kw):
        raise RuntimeError("LLM 全池不可用")

    cols = flash_gate.gate_columns("某条快讯", _evaluate=boom)
    assert cols is not None
    assert cols["gate_verdict"] == ""
    assert cols["filter_reason"] == ""  # v453 P1-4：退化路径同样照常入库


def test_empty_body_skipped():
    assert flash_gate.gate_columns("   ", _evaluate=lambda *a, **k: {}) is None


def test_enabled_switch(monkeypatch):
    monkeypatch.delenv("FLASH_FEEDS_GATE", raising=False)
    assert flash_gate.enabled() is True
    monkeypatch.setenv("FLASH_FEEDS_GATE", "0")
    assert flash_gate.enabled() is False
    monkeypatch.setenv("FLASH_FEEDS_GATE", " OFF ")
    assert flash_gate.enabled() is False
