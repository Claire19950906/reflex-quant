"""Tests for perf_benchmarks.py (v231+).

Runs each registered benchmark and asserts it's within the documented
threshold.  Thresholds are intentionally generous (~3-5x current value)
so the tests pass today but fail on real regression.

Run manually: `python src_python/perf_benchmarks.py`
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import perf_benchmarks as pb  # noqa: E402


@pytest.mark.parametrize("name,bench,thresh", [
    (n, fn, th) for n, (fn, th, _note) in pb.BENCHMARKS.items()
])
def test_benchmark_within_threshold(name, bench, thresh):
    """Each benchmark must finish within its CI threshold."""
    t = bench()
    assert t <= thresh, (
        f"perf regression: {name} took {t:.3f}s > {thresh:.3f}s threshold"
    )


def test_benchmarks_have_thresholds():
    """Sanity: every registered benchmark has a positive threshold."""
    for name, (_fn, thresh, _note) in pb.BENCHMARKS.items():
        assert thresh > 0, f"{name} has non-positive threshold: {thresh}"


def test_run_all_runs_every_benchmark():
    """run_all should not raise and should return a value for each."""
    results = pb.run_all()
    assert set(results.keys()) == set(pb.BENCHMARKS.keys())
    for v in results.values():
        assert v >= 0.0


def test_format_report_has_all_lines():
    """format_report should produce a row per benchmark."""
    results = {name: 0.001 for name in pb.BENCHMARKS}
    text = pb.format_report(results)
    for name in pb.BENCHMARKS:
        assert name in text
