"""Tests for demo_policy.py (v231+).

Covers the pure class-based DemoPolicy API.  Does NOT depend on env vars
or the global demo_mode module — tests build a DemoPolicy directly.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import demo_policy as dp  # noqa: E402


# ────────── sanitize ──────────

def test_disabled_policy_is_noop():
    p = dp.DemoPolicy.disabled()
    assert p.sanitize(1.23456789) == 1.23456789
    assert p.sanitize({"a": 0.1, "b": [1.1, 2.2]}) == {"a": 0.1, "b": [1.1, 2.2]}


def test_enabled_policy_rounds_floats():
    p = dp.DemoPolicy(enabled=True, decimals=2)
    assert p.sanitize(1.234567) == 1.23
    assert p.sanitize(-0.0001) == 0.0
    assert p.sanitize(0.005) == 0.0 or p.sanitize(0.005) == 0.01  # banker rounding ok


def test_sanitize_preserves_int_bool_str():
    p = dp.DemoPolicy(enabled=True, decimals=2)
    assert p.sanitize(42) == 42
    assert p.sanitize(True) is True
    assert p.sanitize("hello") == "hello"
    assert p.sanitize(None) is None


def test_sanitize_preserves_nan_inf():
    import math
    p = dp.DemoPolicy(enabled=True, decimals=2)
    assert math.isnan(p.sanitize(float("nan")))
    assert math.isinf(p.sanitize(float("inf")))


def test_sanitize_recurses_into_dict():
    p = dp.DemoPolicy(enabled=True, decimals=1)
    out = p.sanitize({"a": 1.55, "nested": {"b": 2.45}})
    assert out == {"a": 1.6 if round(1.55, 1) == 1.6 else 1.5, "nested": {"b": 2.4 if round(2.45, 1) == 2.4 else 2.5}}


def test_sanitize_recurses_into_list_and_tuple():
    p = dp.DemoPolicy(enabled=True, decimals=0)
    out_list = p.sanitize([1.4, 2.6, "str"])
    assert out_list == [1.0, 3.0, "str"]
    out_tup = p.sanitize((1.4, 2.6))
    assert isinstance(out_tup, tuple)
    assert out_tup == (1.0, 3.0)


def test_sanitize_skip_keys_preserved():
    p = dp.DemoPolicy(enabled=True, decimals=2, skip_keys=frozenset({"id"}))
    out = p.sanitize({"id": "abc-123", "value": 0.123456})
    assert out["id"] == "abc-123"
    assert out["value"] != 0.123456   # rounded


# ────────── trim_history ──────────

def test_trim_history_disabled_is_noop():
    p = dp.DemoPolicy.disabled()
    rows = [{"ts": 100, "v": 1}, {"ts": 200, "v": 2}]
    assert p.trim_history(rows) == rows


def test_trim_history_zero_days_is_noop():
    p = dp.DemoPolicy(enabled=True, history_days=0)
    rows = [{"ts": 100, "v": 1}]
    assert p.trim_history(rows) == rows


def test_trim_history_cuts_old_rows():
    p = dp.DemoPolicy(enabled=True, history_days=7)
    now = datetime.now(timezone.utc)
    new_ts = (now - timedelta(days=2)).isoformat()
    old_ts = (now - timedelta(days=30)).isoformat()
    rows = [
        {"ts": new_ts, "v": 1},
        {"ts": old_ts, "v": 2},
        {"ts": now.isoformat(), "v": 3},
    ]
    kept = p.trim_history(rows)
    kept_vs = [r["v"] for r in kept]
    assert 1 in kept_vs
    assert 3 in kept_vs
    assert 2 not in kept_vs   # the 30-day-old row is gone


def test_trim_history_keeps_rows_with_no_ts():
    p = dp.DemoPolicy(enabled=True, history_days=7)
    rows = [{"v": 1}, {"ts": "garbage"}, {"ts": None}]
    kept = p.trim_history(rows)
    assert len(kept) == 3   # all kept (no usable ts)


def test_trim_history_epoch_ms():
    p = dp.DemoPolicy(enabled=True, history_days=7)
    now = datetime.now(timezone.utc)
    new_ms = int((now - timedelta(days=1)).timestamp() * 1000)
    old_ms = int((now - timedelta(days=30)).timestamp() * 1000)
    rows = [{"ts": new_ms, "v": 1}, {"ts": old_ms, "v": 2}]
    kept = p.trim_history(rows)
    assert [r["v"] for r in kept] == [1]


# ────────── factories & describe ──────────

def test_disabled_factory():
    p = dp.DemoPolicy.disabled()
    assert p.enabled is False


def test_from_env_does_not_raise():
    """Should always return a valid policy (even if env is empty)."""
    p = dp.DemoPolicy.from_env()
    assert isinstance(p, dp.DemoPolicy)
    assert isinstance(p.enabled, bool)


def test_describe_returns_dict():
    p = dp.DemoPolicy(enabled=True, decimals=3, history_days=7)
    d = p.describe()
    assert d["enabled"] is True
    assert d["decimals"] == 3
    assert d["history_days"] == 7
    assert "skip_keys_n" in d


# ────────── module-level shortcuts ──────────

def test_module_sanitize_uses_env_policy():
    """sanitize() at module level should follow env (full mode by default)."""
    out = dp.sanitize(1.234567)
    # Default IS_FULL=True so this should be a no-op
    assert out == 1.234567
