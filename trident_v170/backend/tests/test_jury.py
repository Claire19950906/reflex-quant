"""Tests for jury.py (v231+) - three-track heterogeneous review."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import jury  # noqa: E402


# ── TrackVerdict / JuryResult basics ────────────────────────────────

def test_track_verdict_to_dict():
    v = jury.TrackVerdict("t1", "confirmed", 0.5, {"k": 1})
    d = v.to_dict()
    assert d["track_name"] == "t1"
    assert d["kind"] == "confirmed"
    assert d["confidence"] == 0.5
    assert d["evidence"] == {"k": 1}



# ── track_stat_heuristic ───────────────────────────────────────────

def test_stat_track_lucky():
    v = jury.track_stat_heuristic(
        {"n": 30, "win_rate": 0.7, "avg_luck": 0.5})
    assert v.kind == "unstable_luck"
    assert v.confidence == 0.7


def test_stat_track_skill():
    v = jury.track_stat_heuristic(
        {"n": 30, "win_rate": 0.7, "avg_luck": 0.1})
    assert v.kind == "stable_skill"
    assert v.confidence == 0.7


def test_stat_track_invalidated():
    v = jury.track_stat_heuristic(
        {"n": 30, "win_rate": 0.3, "baseline_wr": 0.5})
    assert v.kind == "invalidated"
    assert v.confidence > 0.5


def test_stat_track_refined():
    """WR +0.20 over baseline (no luck) -> refined verdict via delta branch."""
    v = jury.track_stat_heuristic(
        {"n": 30, "win_rate": 0.5, "avg_luck": 0.0, "baseline_wr": 0.3})
    assert v.kind == "refined"
    assert v.confidence > 0.5


def test_stat_track_n_too_small():
    v = jury.track_stat_heuristic({"n": 3, "win_rate": 0.7})
    assert v.kind == "confirmed"
    assert v.confidence == 0.0


# ── track_bayes ─────────────────────────────────────────────────────

def test_bayes_track_refined():
    v = jury.track_bayes({"n": 50, "win_rate": 0.8})
    assert v.kind == "refined"
    assert v.confidence > 0.5


def test_bayes_track_invalidated():
    v = jury.track_bayes({"n": 50, "win_rate": 0.2})

# ── track_meta_loop ─────────────────────────────────────────────────

def test_meta_loop_track_no_cur():
    v = jury.track_meta_loop({})
    assert v.kind == "confirmed"
    assert v.confidence == 0.0
    assert "no cur" in v.evidence.get("reason", "")


# ── Jury class basics ───────────────────────────────────────────────

def test_jury_register_and_list():
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "x", 0.5))
    j.register("b", lambda e: jury.TrackVerdict("b", "x", 0.5))
    assert j.tracks() == ["a", "b"]


def test_jury_duplicate_raises():
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "x", 0.5))
    with pytest.raises(ValueError):
        j.register("a", lambda e: jury.TrackVerdict("a", "x", 0.5))


def test_jury_no_tracks():
    j = jury.Jury()
    r = j.deliberate({})
    assert r.kind == "confirmed"
    assert r.confidence == 0.0
    assert "no tracks" in r.reason


def test_jury_quorum_fail():
    """1 confident track out of 3 -> quorum fails."""
    j = jury.Jury(min_quorum=2, min_confidence=0.5)
    j.register("confident", lambda e: jury.TrackVerdict("confident", "refined", 0.9))
    j.register("shy1", lambda e: jury.TrackVerdict("shy1", "refined", 0.1))
    j.register("shy2", lambda e: jury.TrackVerdict("shy2", "refined", 0.0))
    r = j.deliberate({})
    assert r.kind == "confirmed"
    assert r.confidence == 0.0
    assert "quorum failed" in r.reason
    # But raw votes preserved
    assert len(r.votes) == 3


def test_jury_strong_consensus():
    """3 tracks all agree -> strong consensus."""
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "refined", 0.7))
    j.register("b", lambda e: jury.TrackVerdict("b", "refined", 0.8))
    j.register("c", lambda e: jury.TrackVerdict("c", "refined", 0.6))
    r = j.deliberate({})
    assert r.kind == "refined"
    assert r.confidence > 0.5
    assert "strong consensus" in r.reason


def test_jury_plurality():
    """2 of 4 agree (50%) -> plurality wins (with weak dissent)."""
    # Use 4 tracks so 2/4 = 50%, just at plurality threshold.
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "refined", 0.7))
    j.register("b", lambda e: jury.TrackVerdict("b", "refined", 0.7))
    j.register("c", lambda e: jury.TrackVerdict("c", "invalidated", 0.7))
    j.register("d", lambda e: jury.TrackVerdict("d", "confirmed", 0.7))
    r = j.deliberate({})
    assert r.kind == "refined"
    assert "plurality" in r.reason


def test_jury_track_exception_caught():
    """A track that throws should still be recorded (as a 0-conf vote)."""
    j = jury.Jury(min_quorum=2)
    def bad(_e):
        raise RuntimeError("boom")
    j.register("bad", bad)
    j.register("good1", lambda e: jury.TrackVerdict("good1", "refined", 0.7))
    j.register("good2", lambda e: jury.TrackVerdict("good2", "refined", 0.7))
    r = j.deliberate({})
    # bad is filtered out (0 conf), 2 good tracks both say refined
    assert r.kind == "refined"
    # When quorum is met, only valid votes are kept in the result
    assert len(r.votes) == 2
    # The bad track is recoverable via a separate test of the catch
    # behavior; here we just assert the surviving verdict is correct.
    assert all(v.track_name != "bad" for v in r.votes)


def test_jury_track_exception_surfaces_on_quorum_fail():
    """When a track throws and quorum fails, the raw vote (with error) is kept."""
    j = jury.Jury(min_quorum=2)
    def bad(_e):
        raise RuntimeError("kaboom")
    j.register("bad", bad)
    j.register("only_one_good",
               lambda e: jury.TrackVerdict("g", "refined", 0.7))
    r = j.deliberate({})
    # quorum fails: only 1 confident track, need 2
    assert r.kind == "confirmed"
    assert "quorum failed" in r.reason
    # All raw votes are preserved on quorum failure
    assert len(r.votes) == 2
    err_vote = next(v for v in r.votes if v.track_name == "bad")
    assert "kaboom" in err_vote.evidence.get("error", "")


def test_jury_contested():
    """All 3 tracks disagree -> contested."""
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "refined", 0.7))
    j.register("b", lambda e: jury.TrackVerdict("b", "invalidated", 0.7))
    j.register("c", lambda e: jury.TrackVerdict("c", "confirmed", 0.7))
    r = j.deliberate({})
    assert r.kind == "contested"
    assert "contested" in r.reason


# ── default_jury integration ────────────────────────────────────────

def test_default_jury_three_tracks():
    j = jury.default_jury()
    assert j.tracks() == ["meta_loop", "stat_heur", "bayes_post"]


def test_default_jury_stable_skill_case():
    """High win_rate, low luck, no base -> stat+meta agree, skill verdict."""
    j = jury.default_jury()
    r = j.deliberate({"n": 50, "win_rate": 0.7, "avg_luck": 0.1})
    # At least one of the result's votes should be stable_skill
    kinds = [v.kind for v in r.votes]
    assert "stable_skill" in kinds or "refined" in kinds


def test_default_jury_luck_case():
    """High win_rate, high luck -> unstable_luck verdict."""
    j = jury.default_jury()
    r = j.deliberate({"n": 50, "win_rate": 0.7, "avg_luck": 0.5})
    kinds = [v.kind for v in r.votes]
    assert "unstable_luck" in kinds


def test_default_jury_deteriorating_case():
    """Current WR << baseline -> invalidated verdict."""
    j = jury.default_jury()
    r = j.deliberate({
        "n": 50, "win_rate": 0.3, "avg_luck": 0.0, "baseline_wr": 0.5,
    })
    kinds = [v.kind for v in r.votes]
    assert "invalidated" in kinds

    """All 3 tracks disagree -> contested."""
    j = jury.Jury()
    j.register("a", lambda e: jury.TrackVerdict("a", "refined", 0.7))
    j.register("b", lambda e: jury.TrackVerdict("b", "invalidated", 0.7))
    j.register("c", lambda e: jury.TrackVerdict("c", "confirmed", 0.7))
    r = j.deliberate({})
    assert r.kind == "contested"
    assert "contested" in r.reason



def test_bayes_track_neutral():
    v = jury.track_bayes({"n": 50, "win_rate": 0.5})
    assert v.kind == "confirmed"
    assert v.confidence < 0.05   # ~0 (no info)


def test_bayes_track_n_too_small():
    v = jury.track_bayes({"n": 2, "win_rate": 0.7})
    assert v.kind == "confirmed"
    assert v.confidence == 0.0

def test_jury_result_to_dict():
    v = jury.TrackVerdict("t1", "confirmed", 0.5)
    r = jury.JuryResult(kind="confirmed", confidence=0.6, votes=[v],
                        reason="x")
    d = r.to_dict()
    assert d["kind"] == "confirmed"
    assert d["votes"] == [v.to_dict()]
    assert d["reason"] == "x"


def test_valid_kinds_is_frozen():
    assert "confirmed" in jury.VALID_KINDS
    assert "contested" in jury.VALID_KINDS
    # frozenset is immutable
    with pytest.raises(AttributeError):
        jury.VALID_KINDS.add("x")
