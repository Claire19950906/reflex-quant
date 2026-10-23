"""Tests for reflection_tournament.py (v240+) — k=3 verifier tournament.

覆盖:
  - Finding / Vote / ConfirmedFinding 数据结构
  - _parse_findings JSON 容错
  - Tournament 聚合逻辑 (quorum 投票)
  - 单个 verifier 失败容错
  - 并发执行
  - performance
  - 默认 verifier
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import reflection_tournament as rt  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════════════════

def test_finding_key_groups_by_code_and_category():
    a = rt.Finding("E021", "reasoning_logic", 2, "x", "y", confidence=0.5)
    b = rt.Finding("E021", "reasoning_logic", 3, "z", "w", confidence=0.7)
    c = rt.Finding("E021", "causal", 2, "x", "y", confidence=0.5)
    d = rt.Finding("E022", "reasoning_logic", 2, "x", "y", confidence=0.5)
    assert a.key() == b.key()
    assert a.key() != c.key()
    assert a.key() != d.key()


def test_vote_with_error():
    v = rt.Vote(verifier_id="v1", error="some failure")
    assert v.error == "some failure"
    assert v.findings == []


def test_confirmed_finding_fields():
    cf = rt.ConfirmedFinding(
        error_code="E021", category="reasoning_logic", severity=2,
        description="x", evidence="e", quorum=2,
        verifier_ids=["v1", "v2"], avg_confidence=0.85,
    )
    for k in ["error_code", "category", "severity", "description",
              "evidence", "quorum", "verifier_ids", "avg_confidence"]:
        assert hasattr(cf, k)


# ═══════════════════════════════════════════════════════════════════════════
# 2. _parse_findings JSON 容错
# ═══════════════════════════════════════════════════════════════════════════

def test_parse_empty():
    assert rt._parse_findings("", "v1") == []


def test_parse_garbage():
    assert rt._parse_findings("not json at all", "v1") == []


def test_parse_partial_json():
    raw = '{"blindspots": [{"error_code": "E001"}'
    assert rt._parse_findings(raw, "v1") == []


def test_parse_markdown_fenced():
    raw = (
        '```json\n{"blindspots": [{"error_code": "E021", '
        '"category": "x", "severity": 2, "description": "d", '
        '"evidence": "e", "confidence": 0.9}]}\n```'
    )
    parsed = rt._parse_findings(raw, "v1")
    assert len(parsed) == 1
    assert parsed[0].error_code == "E021"


def test_parse_confidence_clamped():
    raw = '{"blindspots": [{"error_code": "E021", "category": "x", "severity": 2, "description": "d", "evidence": "e", "confidence": 5.0}]}'
    parsed = rt._parse_findings(raw, "v1")
    assert parsed[0].confidence == 1.0


def test_parse_skips_non_dict_items():
    raw = '{"blindspots": [{"error_code": "E021", "category": "x", "severity": 2, "description": "d", "evidence": "e", "confidence": 0.5}, "not a dict", null]}'
    parsed = rt._parse_findings(raw, "v1")
    assert len(parsed) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. Tournament 聚合逻辑
# ═══════════════════════════════════════════════════════════════════════════

class _MockVerifier:
    def __init__(self, verifier_id: str, findings):
        self.verifier_id = verifier_id
        self.fixed = findings
        self.call_count = 0

    def vote(self, prompt: str) -> rt.Vote:
        self.call_count += 1
        return rt.Vote(
            verifier_id=self.verifier_id,
            findings=[rt.Finding(
                error_code=f.error_code, category=f.category,
                severity=f.severity, description=f.description,
                evidence=f.evidence, verifier_id=self.verifier_id,
                confidence=f.confidence,
            ) for f in self.fixed],
            raw_output="<mock>",
        )


def _make_f(code="E021", cat="reasoning_logic", sev=2, conf=0.7):
    return rt.Finding(code, cat, sev, f"desc-{code}", f"ev-{code}", confidence=conf)


def test_quorum_2_all_agree():
    verifiers = [
        _MockVerifier("v1", [_make_f()]),
        _MockVerifier("v2", [_make_f()]),
        _MockVerifier("v3", [_make_f()]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert res.consensus_reached
    assert res.confirmed_findings[0].quorum == 3


def test_quorum_2_partial_agreement():
    verifiers = [
        _MockVerifier("v1", [_make_f("E021")]),
        _MockVerifier("v2", [_make_f("E021")]),
        _MockVerifier("v3", [_make_f("E022")]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert res.consensus_reached
    assert res.confirmed_findings[0].error_code == "E021"


def test_quorum_2_no_agreement():
    verifiers = [
        _MockVerifier("v1", [_make_f("E021")]),
        _MockVerifier("v2", [_make_f("E022")]),
        _MockVerifier("v3", [_make_f("E023")]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert not res.consensus_reached
    assert len(res.confirmed_findings) == 0
    assert len(res.raw_findings) == 3


def test_quorum_3_requires_unanimous():
    verifiers = [
        _MockVerifier("v1", [_make_f("E021")]),
        _MockVerifier("v2", [_make_f("E021")]),
        _MockVerifier("v3", [_make_f("E022")]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=3)
    assert not res.consensus_reached


def test_avg_confidence():
    verifiers = [
        _MockVerifier("v1", [_make_f(conf=0.6)]),
        _MockVerifier("v2", [_make_f(conf=0.8)]),
        _MockVerifier("v3", [_make_f(conf=1.0)]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert abs(res.confirmed_findings[0].avg_confidence - 0.8) < 0.001


def test_best_severity_wins():
    verifiers = [
        _MockVerifier("v1", [_make_f(sev=1)]),
        _MockVerifier("v2", [_make_f(sev=3)]),
        _MockVerifier("v3", [_make_f(sev=2)]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert res.confirmed_findings[0].severity == 3


# ═══════════════════════════════════════════════════════════════════════════
# 4. 容错
# ═══════════════════════════════════════════════════════════════════════════

def test_one_verifier_error_does_not_block():
    class _ErrorVerifier:
        verifier_id = "v_err"
        def vote(self, prompt):
            return rt.Vote(verifier_id="v_err", error="LLM timeout")

    verifiers = [
        _MockVerifier("v1", [_make_f()]),
        _ErrorVerifier(),
        _MockVerifier("v3", [_make_f()]),
    ]
    res = rt.run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert res.consensus_reached
    err_votes = [v for v in res.votes if v.error]
    assert len(err_votes) == 1


def test_prompt_verifier_error_capture():
    def bad_llm(prompt):
        raise RuntimeError("simulated LLM API error")

    v = rt.PromptVerifier("v_bad", "dummy template", llm_backend=bad_llm)
    vote = v.vote("any prompt")
    assert vote.error is not None
    assert "simulated" in vote.error
    assert vote.findings == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. 并发执行
# ═══════════════════════════════════════════════════════════════════════════

def test_all_verifiers_invoked_in_parallel():
    """并行模式下所有 verifier 都应被调用."""
    verifiers = [
        _MockVerifier(f"v{i}", [_make_f("E021")]) for i in range(3)
    ]
    t = rt.Tournament(rt.TournamentConfig(verifiers=verifiers, parallel=True))
    t.run("dummy")
    assert all(v.call_count == 1 for v in verifiers)


def test_serial_mode_works():
    """parallel=False 也应正确执行."""
    verifiers = [
        _MockVerifier(f"v{i}", [_make_f("E021")]) for i in range(3)
    ]
    t = rt.Tournament(rt.TournamentConfig(verifiers=verifiers, parallel=False))
    res = t.run("dummy")
    assert res.consensus_reached
    assert all(v.call_count == 1 for v in verifiers)


# ═══════════════════════════════════════════════════════════════════════════
# 6. 性能
# ═══════════════════════════════════════════════════════════════════════════

def test_performance_3_verifiers():
    """3 个 mock verifier tournament 应 < 50ms (k=3 并发 wall-clock)."""
    verifiers = [
        _MockVerifier(f"v{i}", [_make_f("E021")]) for i in range(3)
    ]
    t0 = time.time()
    for _ in range(20):
        rt.run_tournament("dummy", verifiers=verifiers)
    elapsed_ms = (time.time() - t0) * 1000 / 20
    assert elapsed_ms < 50, f"too slow: {elapsed_ms:.1f}ms per run"


# ═══════════════════════════════════════════════════════════════════════════
# 7. 默认 verifier + PromptVerifier 集成
# ═══════════════════════════════════════════════════════════════════════════

def test_default_verifiers_returns_3_unique():
    vs = rt.default_verifiers()
    assert len(vs) == 3
    ids = [v.verifier_id for v in vs]
    assert len(set(ids)) == 3


def test_default_verifier_prompts_differ():
    """3 个 verifier 的 prompt 模板应不同（覆盖不同角度）."""
    vs = rt.default_verifiers()
    templates = [v.template for v in vs]
    assert len(set(templates)) == 3


def test_prompt_verifier_with_mock_llm():
    """PromptVerifier 接受 llm_backend 参数 (注入式 LLM)."""
    def mock_llm(prompt):
        return '{"blindspots": [{"error_code": "E021", "category": "x", "severity": 2, "description": "d", "evidence": "e", "confidence": 0.9}]}'

    v = rt.PromptVerifier("v_test", "dummy", llm_backend=mock_llm)
    vote = v.vote("dummy prompt")
    assert len(vote.findings) == 1
    assert vote.findings[0].error_code == "E021"
    assert vote.findings[0].confidence == 0.9


def test_system_prompt_actually_prepended():
    """v240+ fix: PromptVerifier 必须把 system_prompt 拼到 LLM 调用里."""
    captured = []

    def spy_llm(prompt):
        captured.append(prompt)
        return '{"blindspots": []}'

    v = rt.PromptVerifier(
        "v_angled", "template",
        system_prompt="ALWAYS FLAG MISSED_REGIME",
        llm_backend=spy_llm,
    )
    v.vote("raw task prompt")
    assert len(captured) == 1
    sent = captured[0]
    assert "ALWAYS FLAG MISSED_REGIME" in sent
    assert "raw task prompt" in sent
    # 顺序: system angle 在前, task 在后
    assert sent.index("ALWAYS FLAG MISSED_REGIME") < sent.index("raw task prompt")


def test_no_system_prompt_passes_through():
    """没设 system_prompt 时, prompt 应原样发送 (向后兼容)."""
    captured = []
    def spy_llm(prompt):
        captured.append(prompt)
        return "{}"
    v = rt.PromptVerifier("v_plain", "dummy", llm_backend=spy_llm)
    v.vote("verbatim prompt")
    assert captured[0] == "verbatim prompt"


def test_default_verifier_perspectives_differ():
    """3 个默认 verifier 的 system_prompt 必须不同 (角度多样性核心)."""
    vs = rt.default_verifiers()
    perspectives = [v.system_prompt for v in vs]
    assert len(set(perspectives)) == 3
    for p in perspectives:
        assert len(p) > 30  # 不是空


def test_default_verifier_llm_calls_diverge():
    """同一 prompt, 3 verifier 发出去的 full_prompt 必须不同."""
    captured = []

    def spy_llm(prompt):
        captured.append(prompt)
        return '{"blindspots": []}'

    verifiers = [
        rt.PromptVerifier(
            f"v{i}", "tpl",
            system_prompt=f"ANGLE_{i}_UNIQUE",
            llm_backend=spy_llm,
        ) for i in range(3)
    ]
    for v in verifiers:
        v.vote("SAME INPUT PROMPT")
    assert len(captured) == 3
    assert len(set(captured)) == 3  # 3 个不同 prompt 发出


# ═══════════════════════════════════════════════════════════════════════════
# 8. blindspot_reflector 集成 (_llm_call_tournament)
# ═══════════════════════════════════════════════════════════════════════════

def test_synthesize_confirmed_findings_to_json():
    """_synthesize_tournament_json: ConfirmedFinding → 单次 LLM 同形态 JSON."""
    from blindspot_reflector import _synthesize_tournament_json
    import json

    tr = rt.TournamentResult(
        votes=[],
        confirmed_findings=[rt.ConfirmedFinding(
            error_code="E021", category="reasoning_logic",
            severity=3, description="test blindspot",
            evidence="quoted evidence", quorum=2,
            verifier_ids=["v1", "v2"], avg_confidence=0.85,
        )],
        raw_findings=[],
        consensus_reached=True,
        quorum_required=2,
        elapsed_ms=12.3,
    )
    out = _synthesize_tournament_json(tr, "fake prompt")
    parsed = json.loads(out)
    assert parsed["_source"] == "tournament"
    assert len(parsed["blindspots"]) == 1
    bs = parsed["blindspots"][0]
    assert bs["error_code"] == "E021"
    assert bs["_tournament_quorum"] == 2
    assert bs["_tournament_verifier_ids"] == ["v1", "v2"]


def test_synthesize_no_consensus_marks_raw():
    """未达 quorum: raw_findings 仍输出, 标 _tournament_no_consensus."""
    from blindspot_reflector import _synthesize_tournament_json
    import json

    tr = rt.TournamentResult(
        votes=[],
        confirmed_findings=[],
        raw_findings=[rt.Finding("E022", "x", 1, "d", "e", verifier_id="v1", confidence=0.5)],
        consensus_reached=False,
        quorum_required=3,
        elapsed_ms=5.0,
    )
    out = _synthesize_tournament_json(tr, "prompt")
    parsed = json.loads(out)
    assert len(parsed["blindspots"]) == 1
    assert parsed["blindspots"][0].get("_tournament_no_consensus") is True


def test_llm_call_tournament_off_falls_back():
    """TOURNAMENT_ENABLED=0 (默认) → 走单次 _llm_call, 不进 tournament."""
    import blindspot_reflector as br
    rt.set_llm_backend(None)
    # 强制关闭 (默认就是关, 但显式 set 以防环境变量污染)
    import config as _cfg
    if not hasattr(_cfg, "TOURNAMENT_ENABLED"):
        _cfg.TOURNAMENT_ENABLED = False
    else:
        _cfg.TOURNAMENT_ENABLED = False

    captured = []
    original_llm = br._llm_call
    def spy(prompt):
        captured.append(prompt)
        return '{"blindspots": [{"error_code": "E001", "category": "x", "severity": 1, "description": "d", "evidence": "e", "confidence": 0.5}]}'
    br._llm_call = spy
    try:
        result = br._llm_call_tournament("dummy prompt")
    finally:
        br._llm_call = original_llm

    assert len(captured) == 1
    assert captured[0] == "dummy prompt"  # 原样透传, 没拼 system angle
    parsed = json.loads(result)
    assert parsed["blindspots"][0]["error_code"] == "E001"


def test_llm_call_tournament_on_runs_tournament():
    """TOURNAMENT_ENABLED=1 → 走 tournament, 3 verifier 都被调到."""
    import blindspot_reflector as br
    rt.set_llm_backend(None)
    import config as _cfg
    _cfg.TOURNAMENT_ENABLED = True
    _cfg.TOURNAMENT_QUORUM = 2
    _cfg.TOURNAMENT_TIMEOUT = 10
    _cfg.TOURNAMENT_PARALLEL = True

    captured = []
    def mock_llm(prompt):
        captured.append(prompt)
        return '{"blindspots": [{"error_code": "E021", "category": "x", "severity": 2, "description": "d", "evidence": "e", "confidence": 0.7}]}'
    rt.set_llm_backend(mock_llm)

    try:
        result = br._llm_call_tournament("INPUT")
    finally:
        _cfg.TOURNAMENT_ENABLED = False
        rt.set_llm_backend(None)

    assert len(captured) == 3  # 3 个 verifier 都调了
    assert len(set(captured)) >= 2  # 不同角度 → prompt 不同
    parsed = json.loads(result)
    assert parsed["_source"] == "tournament"
    assert parsed["blindspots"][0]["_tournament_quorum"] == 3


def test_llm_call_tournament_failure_falls_back():
    """Tournament 本身抛异常 → 回落单次 _llm_call (verifier 失败不触发)."""
    import blindspot_reflector as br
    import config as _cfg
    _cfg.TOURNAMENT_ENABLED = True
    rt.set_llm_backend(None)

    captured = []
    original_llm = br._llm_call

    # 强制 Tournament.run() 抛错 (e.g. mock 一个总是抛的 Tournament)
    import reflection_tournament as rt_mod
    original_tournament = rt_mod.Tournament
    class _BoomTournament:
        def __init__(self, cfg):
            pass
        def run(self, prompt):
            raise RuntimeError("simulated Tournament failure")

    def spy(prompt):
        captured.append(prompt)
        return '{"blindspots": [{"error_code": "FALLBACK", "category": "x", "severity": 1, "description": "d", "evidence": "e", "confidence": 0.5}]}'
    br._llm_call = spy
    rt_mod.Tournament = _BoomTournament

    try:
        result = br._llm_call_tournament("dummy")
    finally:
        br._llm_call = original_llm
        _cfg.TOURNAMENT_ENABLED = False
        rt.set_llm_backend(None)
        rt_mod.Tournament = original_tournament

    assert len(captured) == 1  # 回落了单次 LLM
    parsed = json.loads(result)
    assert parsed["blindspots"][0]["error_code"] == "FALLBACK"


def test_llm_call_tournament_verifier_errors_handled():
    """3 verifier 全失败 → synthesis 返回空 blindspots, 不抛."""
    import blindspot_reflector as br
    import config as _cfg
    _cfg.TOURNAMENT_ENABLED = True
    _cfg.TOURNAMENT_QUORUM = 2

    def broken_llm(prompt):
        raise RuntimeError("LLM API down")

    rt.set_llm_backend(broken_llm)
    try:
        result = br._llm_call_tournament("dummy")
    finally:
        _cfg.TOURNAMENT_ENABLED = False
        rt.set_llm_backend(None)

    # 不抛 → 返回空 blindspots JSON
    parsed = json.loads(result)
    assert parsed["_source"] == "tournament"
    assert parsed["blindspots"] == []


def test_set_llm_backend_round_trip():
    """set_llm_backend 可注入, None 恢复默认."""
    rt.set_llm_backend(lambda p: "mock")
    rt.set_llm_backend(None)  # reset, 不抛错


def test_default_tournament_factory():
    """default_tournament() 返回 Tournament, 默认 3 verifier."""
    t = rt.default_tournament()
    assert len(t.config.verifiers) == 3