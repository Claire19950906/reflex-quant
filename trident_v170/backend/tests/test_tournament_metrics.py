# -*- coding: utf-8 -*-
"""v240+ Tournament circuit breaker + metrics tests."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import pytest
import reflection_tournament as rt
import tournament_metrics as tm
from reflection_tournament import Vote, Finding, TournamentResult


@pytest.fixture(autouse=True)
def _reset_state():
    tm.reset_for_test()
    yield
    tm.reset_for_test()


def _err_vote(vid: str) -> Vote:
    return Vote(verifier_id=vid, findings=[], raw_output="", error="boom")


def _ok_vote(vid: str) -> Vote:
    return Vote(verifier_id=vid, findings=[], raw_output="ok", error=None)


# ═══════════════════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════

def test_circuit_breaker_closed_initially():
    assert tm._should_short_circuit() is False


def test_circuit_breaker_opens_when_window_all_failed():
    """window=10, threshold=0.7 → 7/10 全失败即 open."""
    for _ in range(7):
        tm._record_tournament_outcome(
            [_err_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    state = tm.get_circuit_breaker_state()
    assert state["is_open"] is True
    assert tm._should_short_circuit() is True


def test_circuit_breaker_does_not_open_when_mixed_outcomes():
    """混合场景: 5 成功 + 5 全失败 (50% < 70%) → 不 open."""
    for _ in range(5):
        tm._record_tournament_outcome(
            [_ok_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    for _ in range(5):
        tm._record_tournament_outcome(
            [_err_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    state = tm.get_circuit_breaker_state()
    assert state["is_open"] is False
    assert tm._should_short_circuit() is False


def test_circuit_breaker_cooldown_half_open():
    """open 后冷却 N 次 → 自动 half-open → 下次按真实结果决定."""
    for _ in range(7):
        tm._record_tournament_outcome(
            [_err_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    assert tm._should_short_circuit() is True
    # 模拟冷却 20 次 (cooldown=20)
    last = False
    for _ in range(20):
        last = tm._should_short_circuit()
    assert last is False  # 冷却结束 → half-open


def test_circuit_breaker_partial_failure_does_not_count():
    """只要有任一 verifier 成功, 不计 all_failed."""
    for _ in range(10):
        tm._record_tournament_outcome(
            [_ok_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    state = tm.get_circuit_breaker_state()
    assert state["is_open"] is False
    assert state["recent_all_failed"] == 0


def test_circuit_breaker_empty_votes_does_not_count_as_failed():
    """votes=[] (极端情况) 不算 all_failed."""
    for _ in range(10):
        tm._record_tournament_outcome([])
    state = tm.get_circuit_breaker_state()
    assert state["recent_all_failed"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════════════

def _tr(consensus=True, findings=None, raw=None, elapsed_ms=10.0):
    return TournamentResult(
        votes=[_ok_vote("v1")],
        confirmed_findings=findings or [],
        raw_findings=raw or [],
        consensus_reached=consensus,
        quorum_required=2,
        elapsed_ms=elapsed_ms,
    )


def test_metrics_total_runs_and_confirmed():
    tm._record_metrics(_tr(consensus=True, elapsed_ms=100))
    tm._record_metrics(_tr(consensus=True, elapsed_ms=200))
    m = tm.get_metrics()
    assert m["total_runs"] == 2
    assert m["confirmed_count"] == 2
    assert m["avg_elapsed_ms"] == 150.0
    assert m["max_elapsed_ms"] == 200.0


def test_metrics_no_consensus_branch():
    tm._record_metrics(_tr(consensus=False, raw=[{"finding": "x"}]))
    m = tm.get_metrics()
    assert m["no_consensus_count"] == 1
    assert m["confirmed_count"] == 0


def test_metrics_all_failed_branch():
    tm._record_metrics(_tr(consensus=False, raw=[]))
    m = tm.get_metrics()
    assert m["all_failed_count"] == 1


def test_metrics_quorum_distribution():
    from reflection_tournament import ConfirmedFinding
    f2 = [ConfirmedFinding(error_code="E001", category="x", severity=2,
                           description="m", evidence={},
                           quorum=2, verifier_ids=["v1", "v2"], avg_confidence=0.8)]
    f3 = [ConfirmedFinding(error_code="E002", category="y", severity=2,
                           description="m", evidence={},
                           quorum=3, verifier_ids=["v1", "v2", "v3"], avg_confidence=0.9)]
    tm._record_metrics(_tr(consensus=True, findings=f2))
    tm._record_metrics(_tr(consensus=True, findings=f2))
    tm._record_metrics(_tr(consensus=True, findings=f3))
    m = tm.get_metrics()
    assert m["quorum_distribution"] == {2: 2, 3: 1}


def test_metrics_empty_returns_zero_rates():
    m = tm.get_metrics()
    assert m["total_runs"] == 0
    assert m["avg_elapsed_ms"] == 0.0
    assert m["confirmed_rate"] == 0.0


def test_metrics_confirmed_rate():
    tm._record_metrics(_tr(consensus=True))
    tm._record_metrics(_tr(consensus=False, raw=[{"x": 1}]))
    tm._record_metrics(_tr(consensus=False, raw=[{"x": 1}]))
    m = tm.get_metrics()
    assert m["confirmed_rate"] == pytest.approx(1 / 3, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════
# Integration: _llm_call_tournament respects circuit breaker + records metrics
# ═══════════════════════════════════════════════════════════════════════════

def test_llm_call_tournament_short_circuits_when_circuit_open(monkeypatch):
    """circuit open 时, _llm_call_tournament 应直接回 _llm_call (不开 tournament)."""
    import blindspot_reflector as br

    # 强制 open circuit
    for _ in range(7):
        tm._record_tournament_outcome(
            [_err_vote("v1"), _err_vote("v2"), _err_vote("v3")]
        )
    assert tm.get_circuit_breaker_state()["is_open"] is True

    monkeypatch.setattr(br, "_llm_call", lambda p: '{"blindspots": []}')
    monkeypatch.setattr(rt, "set_llm_backend", lambda f: None)

    import config as _cfg
    monkeypatch.setattr(_cfg, "TOURNAMENT_ENABLED", True)

    out = br._llm_call_tournament("test")
    assert "_tournament_quorum" not in out  # 短路 → 走单次路径


def test_llm_call_tournament_records_metrics_on_success(monkeypatch):
    """正常走完 tournament 后, metrics 应 +1."""
    import blindspot_reflector as br
    import config as _cfg

    monkeypatch.setattr(_cfg, "TOURNAMENT_ENABLED", True)
    monkeypatch.setattr(br, "_llm_call", lambda p: '{"blindspots": []}')
    monkeypatch.setattr(rt, "set_llm_backend", lambda f: None)

    before = tm.get_metrics()["total_runs"]
    br._llm_call_tournament("p")
    after = tm.get_metrics()["total_runs"]
    assert after == before + 1