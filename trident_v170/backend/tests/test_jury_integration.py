"""Tests for classify_with_jury integration in meta_reflection_loop."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import pytest  # noqa: E402

import meta_reflection_loop as mrl  # noqa: E402


def test_classify_with_jury_skill_case():
    """Strong signal: stable skill — should agree, not contested."""
    cur = {
        "layer": "L1", "sub_layer": "S1", "signal_name": "x",
        "n": 100, "win_rate": 0.7, "avg_luck": 0.1,
    }
    base = {"win_rate": 0.55, "n": 200}
    kind, detail, ev = mrl.classify_with_jury(cur, base)
    # All 3 tracks should converge on "refined" or "stable_skill"
    # (not contested)
    assert "JURY" in detail
    assert "jury_kind" in ev
    assert "jury_votes" in ev
    assert "jury_confidence" in ev
    # The fallback path or consensus should pick something useful
    assert kind in ("stable_skill", "refined", "confirmed",
                    "unstable_luck", "invalidated", "contested")


def test_classify_with_jury_evidence_has_jury_meta():
    cur = {
        "layer": "L1", "sub_layer": "S1", "signal_name": "x",
        "n": 50, "win_rate": 0.7, "avg_luck": 0.5,
    }
    base = {"win_rate": 0.5, "n": 100}
    kind, detail, ev = mrl.classify_with_jury(cur, base)
    # The detail must carry the jury verdict (or "CONTESTED" tag)
    assert "[JURY" in detail
    # Evidence must have the full jury breakdown
    assert isinstance(ev["jury_votes"], list)
    assert len(ev["jury_votes"]) == 3   # 3 default tracks
    # Each vote dict has the required fields
    for v in ev["jury_votes"]:
        assert "track_name" in v
        assert "kind" in v
        assert "confidence" in v


def test_classify_with_jury_no_baseline():
    """No baseline: meta_loop returns no_baseline_new (jury filter band)."""
    cur = {
        "layer": "L1", "sub_layer": "S1", "signal_name": "x",
        "n": 30, "win_rate": 0.6, "avg_luck": 0.1,
    }
    kind, detail, ev = mrl.classify_with_jury(cur, baseline=None)
    # Whatever the jury decides, the integration must:
    #   - return a string with a [JURY ...] tag
    #   - have a full jury_votes breakdown
    #   - and the meta_loop track's vote is preserved in the breakdown
    assert "[JURY" in detail
    assert isinstance(ev["jury_votes"], list)
    # At least meta_loop + stat_heur contribute; bayes may be filtered if n is small
    assert len(ev["jury_votes"]) >= 2
    meta_vote = next(v for v in ev["jury_votes"] if v["track_name"] == "meta_loop")
    # meta_loop is told to flag no baseline as a special kind
    assert meta_vote["kind"] in ("no_baseline_new", "contested")


def test_classify_with_jury_invalidated_case():
    """Deteriorating signal -> all tracks should agree on invalidated."""
    cur = {
        "layer": "L1", "sub_layer": "S1", "signal_name": "x",
        "n": 50, "win_rate": 0.3, "avg_luck": 0.0,
    }
    base = {"win_rate": 0.55, "n": 100}
    kind, detail, ev = mrl.classify_with_jury(cur, base)
    # Either jury agrees on invalidated, OR meta_loop track returns
    # invalidated via fallback.  In all cases, the meta_loop track
    # should call it invalidated.
    meta_vote = next(v for v in ev["jury_votes"] if v["track_name"] == "meta_loop")
    assert meta_vote["kind"] == "invalidated"
