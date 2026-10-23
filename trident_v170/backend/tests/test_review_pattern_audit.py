"""v233: 「规律发现」深入自审 — _audit_pattern_deep 系列函数测试。

覆盖：
- 单步终止（next_question=null）
- 触达 max_depth 上限
- batch pattern 数上限
- LLM 不可用（_llm 返回 None）→ stop_reason='llm_failed'
- evidence 引用了不存在的 #id → fabricated_evidence + confidence 被压低
- fallback skeleton（pattern_audits 与 patterns 数量一致，confidence=None）
- contradicted_by_evidence 路径
- _fallback_summary 自动挂载 pattern_audits
"""
from __future__ import annotations

import json
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import review_agent as ra


SAMPLE_TRADES = [
    {"id": 10, "asset": "BTC", "action": "LONG", "is_correct": "WIN",
     "forward_pnl": 1.5, "decision_context": '{"status":"up"}',
     "news_text": "BTC [hash:abcd] up"},
    {"id": 11, "asset": "BTC", "action": "LONG", "is_correct": "WIN",
     "forward_pnl": 2.0, "decision_context": '{"status":"up"}',
     "news_text": "BTC [hash:efgh] strong"},
    {"id": 12, "asset": "ETH", "action": "SHORT", "is_correct": "LOSS",
     "forward_pnl": -1.0, "decision_context": '{"status":"up"}',
     "news_text": "ETH surprise"},
]
SAMPLE_AGG = {
    "total": 3, "decided": 3, "wins": 2, "losses": 1, "holds": 0,
    "winrate": 2 / 3, "avg_pnl": 0.83, "total_pnl": 2.5,
    "by_asset": {
        "BTC": {"total": 2, "wins": 2, "losses": 0, "pnl": 3.5},
        "ETH": {"total": 1, "wins": 0, "losses": 1, "pnl": -1.0},
    },
}


def _stub_single_step(*a, **k):
    """单步回答 + next_question=null：测试 LLM 主动停下的路径。"""
    return json.dumps({
        "answer": "因为 BTC 十月一直涨",
        "next_question": None,
        "factual": True,
        "evidence": ["#10 BTC 看多 WIN", "#11 BTC 看多 WIN"],
        "conditions_apply": ["BTC 趋势向上"],
        "conditions_fail": ["BTC 急跌时"],
        "overfit_risks": ["只跑了 2 周"],
        "confidence": 0.7,
        "caution": "样本量小",
        "stop_reason": "no_further_why",
    })


def _stub_always_more(*a, **k):
    """每次都给出 next_question：测试 max_depth 上限。"""
    return json.dumps({
        "answer": "因为 X",
        "next_question": "为什么 X 会发生？",
        "factual": True,
        "evidence": ["#10 BTC"],
        "conditions_apply": [], "conditions_fail": [], "overfit_risks": [],
        "confidence": 0.5, "caution": "", "stop_reason": "",
    })


def _stub_fabricated(*a, **k):
    """evidence 引用了 #9999（不在样本里）：测试 fabricated 标记 + 置信度压低。"""
    return json.dumps({
        "answer": "看 #9999",
        "next_question": None,
        "factual": True,
        "evidence": ["#9999 一笔 WIN"],
        "conditions_apply": [], "conditions_fail": [], "overfit_risks": [],
        "confidence": 0.9, "caution": "", "stop_reason": "no_further_why",
    })


def _stub_contradict(*a, **k):
    return json.dumps({
        "answer": "反向证据显示规律不成立",
        "next_question": None,
        "factual": False,
        "evidence": ["#10 BTC"],
        "conditions_apply": [], "conditions_fail": [], "overfit_risks": [],
        "confidence": 0.2, "caution": "被本期证据反驳",
        "stop_reason": "contradicted_by_evidence",
    })


# ──────────────────── _validate_audit_evidence ────────────────────

def test_validate_audit_evidence_flags_fabricated_ids():
    fab = ra._validate_audit_evidence(
        {"evidence": ["#10 WIN", "#9999 LOSS"]},
        [{"id": 10}, {"id": 11}],
    )
    assert fab == ["#9999 LOSS"], fab
    assert ra._validate_audit_evidence(
        {"evidence": ["#10", "#11"]}, [{"id": 10}, {"id": 11}]
    ) == []
    assert ra._validate_audit_evidence(
        {"evidence": ["宏观情绪修复"]}, [{"id": 10}]
    ) == []
    assert ra._validate_audit_evidence(
        {"evidence": [None, 42, "#10 WIN"]}, [{"id": 10}]
    ) == []


# ──────────────────── _audit_pattern_deep: 终止路径 ────────────────────

def test_audit_stops_when_next_question_null():
    a = ra._audit_pattern_deep(
        "BTC 牛市 LONG 必赚", SAMPLE_TRADES, SAMPLE_AGG,
        llm_call=_stub_single_step, max_depth=3,
    )
    assert a["stop_reason"] == "no_further_why"
    assert a["depth_reached"] == 1
    assert len(a["why_chain"]) == 1
    assert a["confidence"] == 0.7
    assert a["factual"] is True
    assert a["fabricated_evidence"] == []


def test_audit_respects_max_depth_cap():
    a = ra._audit_pattern_deep(
        "ETH 跟随 BTC", SAMPLE_TRADES, SAMPLE_AGG,
        llm_call=_stub_always_more, max_depth=3,
    )
    assert a["depth_reached"] == 3
    assert len(a["why_chain"]) == 3
    assert a["stop_reason"] == "max_depth"


def test_audit_stops_on_contradicted_by_evidence():
    a = ra._audit_pattern_deep(
        "BTC 永远 LONG 必赚", SAMPLE_TRADES, SAMPLE_AGG,
        llm_call=_stub_contradict, max_depth=5,
    )
    assert a["stop_reason"] == "contradicted_by_evidence"
    assert a["factual"] is False
    assert a["confidence"] == 0.2


def test_audit_empty_input_returns_skeleton():
    a = ra._audit_pattern_deep("", SAMPLE_TRADES, SAMPLE_AGG, llm_call=_stub_single_step)
    assert a["stop_reason"] == "empty_input"
    assert a["depth_reached"] == 0
    assert a["why_chain"] == []
    a = ra._audit_pattern_deep("anything", [], SAMPLE_AGG, llm_call=_stub_single_step)
    assert a["stop_reason"] == "empty_input"


# ──────────────────── LLM 不可用路径 ────────────────────

def test_audit_when_llm_returns_none(monkeypatch):
    monkeypatch.setattr(ra, "_llm", lambda *a, **k: None)
    a = ra._audit_pattern_deep("P", SAMPLE_TRADES, SAMPLE_AGG)
    assert a["stop_reason"] == "llm_failed"
    assert a["depth_reached"] == 1
    assert a["confidence"] is None
    assert a["why_chain"][-1]["answer"] == ""


def test_audit_when_llm_returns_garbage(monkeypatch):
    monkeypatch.setattr(ra, "_llm", lambda *a, **k: "not a json at all, just text")
    a = ra._audit_pattern_deep("P", SAMPLE_TRADES, SAMPLE_AGG)
    assert a["stop_reason"] == "llm_failed"


# ──────────────────── fabricated evidence 处理 ────────────────────

def test_fabricated_evidence_drops_confidence(monkeypatch):
    monkeypatch.setattr(ra, "_llm", _stub_fabricated)
    a = ra._audit_pattern_deep("P", SAMPLE_TRADES, SAMPLE_AGG)
    assert a["fabricated_evidence"]
    assert a["confidence"] <= 0.4
    assert "编造" in a["caution"]


def test_fabricated_evidence_keeps_low_confidence(monkeypatch):
    """当 LLM 已给出 confidence<0.4 时（已经认怂），不再二次压低、不再追加警告。"""
    def stub(*a, **k):
        return json.dumps({
            "answer": "ok",
            "next_question": None,
            "factual": False,
            "evidence": ["#9999 LOSS"],
            "conditions_apply": [], "conditions_fail": [], "overfit_risks": [],
            "confidence": 0.2, "caution": "evidence 弱",
            "stop_reason": "no_further_why",
        })
    monkeypatch.setattr(ra, "_llm", stub)
    a = ra._audit_pattern_deep("P", SAMPLE_TRADES, SAMPLE_AGG)
    assert a["confidence"] == 0.2, "已低的 confidence 不应被二次压低"
    assert a["fabricated_evidence"]
    # 既然 LLM 已给出低 confidence，原 caution 保留（不再追加"编造"）
    assert a["caution"] == "evidence 弱"


# ──────────────────── batch 上限 ────────────────────

def test_audit_patterns_batch_caps_pattern_count():
    batch = ra._audit_patterns_batch(
        ["A", "B", "C", "D", "E"], SAMPLE_TRADES, SAMPLE_AGG,
        llm_call=_stub_single_step, max_patterns=3,
    )
    assert len(batch) == 3


def test_audit_patterns_batch_empty_returns_empty():
    assert ra._audit_patterns_batch([], SAMPLE_TRADES, SAMPLE_AGG) == []


# ──────────────────── _fallback_summary 自动挂载 pattern_audits ────────────────────

def test_fallback_summary_attaches_pattern_audits_skeleton():
    fb = ra._fallback_summary(SAMPLE_TRADES, SAMPLE_AGG)
    assert "pattern_audits" in fb
    assert len(fb["pattern_audits"]) == len(fb["patterns"])
    for a in fb["pattern_audits"]:
        assert a["stop_reason"] == "no_llm"
        assert a["confidence"] is None
        assert a["why_chain"] == []
        assert a["fabricated_evidence"] == []
        assert a["claim"] in fb["patterns"]
        assert a["caution"]


def test_fallback_pattern_audits_empty_patterns_returns_empty():
    assert ra._fallback_pattern_audits([]) == []


# ──────────────────── _normalize_audit_payload 兜底 ────────────────────

def test_normalize_payload_handles_non_dict():
    n = ra._normalize_audit_payload(None, "x")
    assert n["stop_reason"] == "parse_failed"
    assert n["evidence"] == []
    assert n["confidence"] is None


def test_normalize_payload_clamps_confidence():
    n = ra._normalize_audit_payload({"confidence": 1.7}, "x")
    assert n["confidence"] == 1.0
    n = ra._normalize_audit_payload({"confidence": -0.3}, "x")
    assert n["confidence"] == 0.0


def test_normalize_payload_handles_string_evidence():
    n = ra._normalize_audit_payload({"evidence": "#10 WIN 单条字符串"}, "x")
    assert n["evidence"] == ["#10 WIN 单条字符串"]


# ──────────────────── 集成：_review_block 挂 pattern_audits ────────────────────

def test_review_block_attaches_pattern_audits(monkeypatch):
    """_review_block 在 summary 出来后会挂上 pattern_audits（LLM 路径）。"""
    monkeypatch.setattr(ra, "_review_summary_llm", lambda *a, **k: {
        "overview": "test",
        "patterns": ["P1", "P2"],
    })
    monkeypatch.setattr(ra, "_llm", _stub_single_step)
    out = ra._review_block(None, SAMPLE_TRADES)  # type: ignore[arg-type]
    assert "pattern_audits" in out["summary"]
    assert len(out["summary"]["pattern_audits"]) == 2
    assert all(a["claim"] in ("P1", "P2") for a in out["summary"]["pattern_audits"])


def test_review_block_fallback_path_keeps_pattern_audits(monkeypatch):
    """LLM 路径返回 None 时 fallback 仍带 pattern_audits 骨架。"""
    monkeypatch.setattr(ra, "_review_summary_llm", lambda *a, **k: None)
    out = ra._review_block(None, SAMPLE_TRADES)  # type: ignore[arg-type]
    assert "pattern_audits" in out["summary"]
    for a in out["summary"]["pattern_audits"]:
        assert a["stop_reason"] == "no_llm"


