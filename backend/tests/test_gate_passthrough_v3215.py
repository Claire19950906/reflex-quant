"""FIX-8b: 门卫鉴权失效 → 降级放行必须真的放行（不是被主分析打死）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from review import gate_drop_policy as gdp
import realtime_filter as rt


def test_auth_failed_marks_passthrough_not_zero(monkeypatch):
    """鉴权失效/额度耗尽 → gate_unavailable=2（放行），不能是 0（会被判成'没信号'）。"""
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": None)
    monkeypatch.setattr(rt, "_LAST_POOL_FAIL_CODES", {402})

    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.9, "status": "PENDING"},
                          "美联储暗示降息", "body")

    assert out["gate_unavailable"] == gdp.GATE_UNAVAILABLE_PASSTHROUGH
    assert out["gate_miss"] is True
    assert out["is_noise"] == 0


def test_passthrough_row_is_analyzed_not_dropped():
    """核心回归：gu=2 时 resolve 必须返回 None（= 不丢弃，照常写 ai_decisions）。"""
    st, rc, why = gdp.resolve_drop_status(
        gate_unavailable=2, gate_strength=0, gate_verdict="无信号", retry_count=0
    )
    assert st is None, "降级放行的行不能被丢弃"
    assert why == gdp.REASON_AUTH_PASSTHROUGH
    assert rc == 0


def test_passthrough_wins_over_retry_exhaustion():
    """即使 retry_count 已满，放行标记仍然优先 —— 否则又变成'埋掉'。"""
    st, _, _ = gdp.resolve_drop_status(gate_unavailable=2, retry_count=99, max_retries=3)
    assert st is None


def test_frozen_still_retries_after_passthrough_change():
    """gu=1（冻结待回放）语义不变：重试，不是放行、也不是判死。"""
    st, rc, _ = gdp.resolve_drop_status(gate_unavailable=1, retry_count=0)
    assert st == "PENDING"
    assert rc == 1


def test_claim_sql_allows_passthrough_rows():
    """认领门槛必须放行 gu=2，否则标记了也进不了分析。"""
    from engine import ai_worker
    for sql in (ai_worker._BATCH_CLAIM_SQL, ai_worker._BATCH_CLAIM_FRESH_SQL):
        assert "COALESCE(gate_unavailable, 0) IN (0, 2)" in sql
