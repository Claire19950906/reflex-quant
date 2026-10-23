"""Tests for meta_loop_stopping.py (v231+)."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import meta_loop_stopping as ms  # noqa: E402


def test_should_continue_stable():
    """If current level has same kinds as prior, stop."""
    prior = ["invalidated"] * 10
    curr  = ["invalidated"] * 10
    assert ms.should_continue(prior, curr) is False


def test_should_continue_new_info():
    """If current level changes its mind on every signal, continue."""
    prior = ["confirmed"] * 10
    curr  = ["invalidated"] * 10
    assert ms.should_continue(prior, curr) is True


def test_should_continue_chaotic():
    """If current level's distribution is high-entropy, stop (noise)."""
    prior = ["a"] * 10
    curr  = ["a", "b", "c", "a", "b", "c", "a", "b", "c", "d"]
    assert ms.should_continue(prior, curr) is False


def test_should_continue_empty():
    assert ms.should_continue([], []) is False
    assert ms.should_continue(["a"], []) is False
    assert ms.should_continue([], ["a"]) is False


def test_new_info_rate_basic():
    assert ms.new_info_rate(["a", "a"], ["a", "b"]) == 0.5
    assert ms.new_info_rate(["a"], ["a"]) == 0.0
    assert ms.new_info_rate(["a"], ["b"]) == 1.0


def test_new_info_rate_length_mismatch():
    """Should zip up to min length."""
    assert ms.new_info_rate(["a", "b", "c"], ["a", "b"]) == 0.0
    assert ms.new_info_rate(["a", "b", "c"], ["a", "b", "c"]) == 0.0


def test_level_entropy_uniform():
    """Uniform distribution → entropy 1.0 (max)."""
    assert ms.level_entropy(["a", "b", "c", "d"]) == pytest.approx(1.0)


def test_level_entropy_singleton():
    """Single unique kind → entropy 0.0."""
    assert ms.level_entropy(["a"] * 10) == pytest.approx(0.0)


def test_plan_levels_stops_on_stability():
    """If level 2 repeats level 1 kinds, stop after L=2."""
    def runner(L, prev):
        if L == 1:
            return [("s1", "invalidated"), ("s2", "confirmed"), ("s3", "confirmed")]
        if L == 2:
            return [("s1", "invalidated"), ("s2", "confirmed"), ("s3", "confirmed")]
        return []
    levels = ms.plan_levels([], runner, max_level=4)
    assert len(levels) == 2   # ran L=1 (always), then L=2 (because prior empty), then stopped


def test_plan_levels_keeps_going_when_novel():
    """If level 2 has new info, continue to L=3."""
    def runner(L, prev):
        if L == 1:
            return [("s1", "confirmed"), ("s2", "confirmed")]
        if L == 2:
            return [("s1", "invalidated"), ("s2", "invalidated")]  # all different
        if L == 3:
            return [("s1", "invalidated"), ("s2", "invalidated")]  # stable
        return []
    levels = ms.plan_levels([], runner, max_level=4)
    assert len(levels) == 3


def test_plan_levels_respects_max_level():
    """Hard upper bound: never exceed max_level."""
    def runner(L, prev):
        # Always return new info, never converge
        return [(f"s{i}", f"kind_{L}_{i}") for i in range(3)]
    levels = ms.plan_levels([], runner, max_level=2)
    assert len(levels) == 2


def test_plan_levels_empty_signal_stops():
    """If runner returns no signals, stop immediately."""
    def runner(L, prev):
        return []
    levels = ms.plan_levels([], runner, max_level=4)
    assert levels == []
