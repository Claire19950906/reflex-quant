"""api_format 纯函数单元测试。

2026-11-20: 这组函数刚从 api_server.py 抽出（只移不改）。抽出的意义就是能这样直接测：
不用起 HTTP 服务、不用连数据库，几毫秒跑完，覆盖"两个端口同一行数据状态不一致"
这类历史上真出过事故的分支。
"""

import json
import sqlite3

import pytest

from api_format import (
    _normalize_analysis_status,
    _parse_peer_reviews,
    _row_to_event,
    _safe,
    _strip_hold_override,
    gate_reason_fallback,
)


def test_safe_dict_missing_key_returns_default():
    assert _safe({"a": 1}, "a") == 1
    assert _safe({"a": 1}, "b") is None
    assert _safe({"a": 1}, "b", 0) == 0


def test_safe_sqlite_row_missing_column_returns_default():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 AS news_id").fetchone()
    assert _safe(row, "news_id") == 1
    assert _safe(row, "decision_id") is None
    assert _safe(row, "gate_unavailable", 0) == 0
    conn.close()


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ([], []),
        ([{"model_label": "kimi", "action": "HOLD"}], [{"model_label": "kimi", "action": "HOLD"}]),
        ('[{"model_label": "grok"}]', [{"model_label": "grok"}]),
        ('{"model_label": "grok"}', []),   # 对象不是数组 → 空
        ("", []),
        ("not json", []),
    ],
)
def test_parse_peer_reviews(raw, expected):
    assert _parse_peer_reviews(raw) == expected


def test_strip_hold_override_removes_legacy_prefix():
    assert _strip_hold_override("由观望转看多；锂矿减产落地") == "锂矿减产落地"
    assert _strip_hold_override("由观望转看空; 库存回升") == "库存回升"
    assert _strip_hold_override("评分较强(8.5)，由观望转看多；涨价") == "涨价"
    assert _strip_hold_override("本就是看多，无前缀") == "本就是看多，无前缀"
    assert _strip_hold_override(None) == ""


def test_gate_reason_fallback():
    assert gate_reason_fallback("证据充分，允许输出方向性结论") == ""
    assert gate_reason_fallback("") == ""
    assert gate_reason_fallback("   ") == ""
    assert gate_reason_fallback("置信度不足") == "置信度不足"


def test_row_to_event_no_decision_is_pending():
    ev = _row_to_event({"news_id": 7, "market_category": "CRYPTO", "timestamp": "2026-11-20 10:00:00"})
    assert ev["id"] == 7 and ev["news_id"] == 7
    assert ev["decision_id"] is None
    assert ev["reason"] == "待 AI 分析"
    assert ev["market_category"] == "CRYPTO"
    assert ev["unclassified"] is False
    assert ev["action"] == "HOLD"
    assert ev["score"] == 0.0
    assert ev["ai_time"] == "——"


def test_row_to_event_unknown_market_is_unclassified():
    ev = _row_to_event({"news_id": 8, "market_category": "OTHER"})
    assert ev["market_category"] == ""
    assert ev["unclassified"] is True

    ev2 = _row_to_event({"news_id": 9, "market_category": ""})
    assert ev2["unclassified"] is True


def test_row_to_event_hash_stripped_and_truncated():
    ev = _row_to_event({"news_id": 10, "market_category": "CRYPTO",
                        "news_text": "[hash:DEADBEEF] 美联储官员讲话 " + "x" * 300})
    assert "[hash:" not in ev["news_text"]
    assert ev["news_text"].startswith("美联储官员讲话")
    assert len(ev["news_text"]) <= 200


def test_row_to_event_consensus_fields():
    consensus = json.dumps({
        "DeepSeek": {"action": "BUY", "reasoning": "库存低位"},
        "Gemini": {"action": "HOLD"},
        "Grok": {"action": "SELL", "reasoning": "需求走弱"},
        "ChatGPT": {"action": "BUY"},
    })
    ev = _row_to_event({"news_id": 11, "market_category": "CRYPTO",
                        "extra_models_consensus": consensus})
    assert ev["deepseek_action"] == "BUY"
    assert ev["deepseek_reasoning"] == "库存低位"
    assert ev["gemini_action"] == "HOLD"
    assert ev["grok_action"] == "SELL"
    assert ev["chatgpt_action"] == "BUY"


def test_normalize_status_noise_and_gate_pending():
    assert _normalize_analysis_status("PENDING", has_decision=False, is_noise=False) == "PENDING"
    noise = _normalize_analysis_status("PENDING", has_decision=False, is_noise=True)
    assert noise != "PENDING"
    pending_gate = _normalize_analysis_status("PENDING", has_decision=False, gate_pending=True)
    assert pending_gate != "PENDING"
    assert pending_gate != noise


def test_normalize_status_with_decision_is_done():
    assert _normalize_analysis_status("FAILED", has_decision=True) == "DONE"
