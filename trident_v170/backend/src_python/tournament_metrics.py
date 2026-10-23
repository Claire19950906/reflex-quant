# -*- coding: utf-8 -*-
"""v240+ Tournament circuit breaker + metrics (lightweight in-process).

Design:
  - Circuit breaker: sliding window tracks "all-verifier-failed" rate.
    Open when rate >= threshold; cooldown N calls then half-open retry.
  - Metrics: counters for total / confirmed / no_consensus / all_failed
    + elapsed_ms avg/max + quorum distribution (2 vs 3).
  - Thread-safe via Lock; zero external deps.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from reflection_tournament import TournamentResult, Vote


# ═══════════════════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CircuitBreakerState:
    recent_calls: int = 0
    recent_all_failed: int = 0
    is_open: bool = False
    opened_at: Optional[float] = None
    cooldown_calls: int = 0


_CB_STATE = CircuitBreakerState()
_CB_LOCK = threading.Lock()


def _should_short_circuit() -> bool:
    """Return True if circuit breaker is currently OPEN (should bypass tournament)."""
    try:
        import config as _cfg
        cooldown = int(getattr(_cfg, "TOURNAMENT_CB_COOLDOWN", 20))
    except Exception:
        cooldown = 20

    with _CB_LOCK:
        if _CB_STATE.is_open:
            _CB_STATE.cooldown_calls += 1
            if _CB_STATE.cooldown_calls >= cooldown:
                _CB_STATE.is_open = False
                _CB_STATE.recent_calls = 0
                _CB_STATE.recent_all_failed = 0
                _CB_STATE.cooldown_calls = 0
                logging.info("tournament circuit breaker HALF-OPEN: retry")
            return _CB_STATE.is_open
        return False


def _record_tournament_outcome(votes: List["Vote"]) -> None:
    """Update circuit breaker window from a tournament's votes."""
    try:
        import config as _cfg
        threshold = float(getattr(_cfg, "TOURNAMENT_CB_THRESHOLD", 0.7))
        window = int(getattr(_cfg, "TOURNAMENT_CB_WINDOW", 10))
    except Exception:
        threshold, window = 0.7, 10

    with _CB_LOCK:
        _CB_STATE.recent_calls += 1
        all_failed = bool(votes) and all(v.error for v in votes)
        if all_failed:
            _CB_STATE.recent_all_failed += 1

        # 每次都判定 (不需要等窗口满), 仅需 ≥2 个样本避免单次噪声误判
        if _CB_STATE.recent_calls >= 2:
            fail_rate = _CB_STATE.recent_all_failed / _CB_STATE.recent_calls
            if fail_rate >= threshold:
                _CB_STATE.is_open = True
                _CB_STATE.opened_at = time.time()
                _CB_STATE.cooldown_calls = 0
                logging.warning(
                    "tournament circuit breaker OPEN: "
                    "fail_rate=%.2f (%d/%d) >= threshold=%.2f",
                    fail_rate, _CB_STATE.recent_all_failed,
                    _CB_STATE.recent_calls, threshold,
                )
        # 窗口滑动: 超 window 后重置计数
        if _CB_STATE.recent_calls >= window:
            _CB_STATE.recent_calls = 0
            _CB_STATE.recent_all_failed = 0


def get_circuit_breaker_state() -> Dict[str, Any]:
    with _CB_LOCK:
        return {
            "is_open": _CB_STATE.is_open,
            "recent_calls": _CB_STATE.recent_calls,
            "recent_all_failed": _CB_STATE.recent_all_failed,
            "cooldown_calls": _CB_STATE.cooldown_calls,
            "opened_at": _CB_STATE.opened_at,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TournamentMetrics:
    total_runs: int = 0
    confirmed_count: int = 0
    no_consensus_count: int = 0
    all_failed_count: int = 0
    elapsed_ms_total: float = 0.0
    elapsed_ms_max: float = 0.0
    quorum_distribution: Dict[int, int] = field(default_factory=dict)

    def avg_elapsed_ms(self) -> float:
        return (self.elapsed_ms_total / self.total_runs) if self.total_runs else 0.0


_METRICS = TournamentMetrics()
_METRICS_LOCK = threading.Lock()


def _record_metrics(result: "TournamentResult") -> None:
    """Aggregate one tournament result into global metrics."""
    with _METRICS_LOCK:
        _METRICS.total_runs += 1
        _METRICS.elapsed_ms_total += result.elapsed_ms
        if result.elapsed_ms > _METRICS.elapsed_ms_max:
            _METRICS.elapsed_ms_max = result.elapsed_ms
        if result.consensus_reached:
            _METRICS.confirmed_count += 1
        elif result.raw_findings:
            _METRICS.no_consensus_count += 1
        else:
            _METRICS.all_failed_count += 1
        for cf in result.confirmed_findings:
            _METRICS.quorum_distribution[cf.quorum] = (
                _METRICS.quorum_distribution.get(cf.quorum, 0) + 1
            )


def get_metrics() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return {
            "total_runs": _METRICS.total_runs,
            "confirmed_count": _METRICS.confirmed_count,
            "no_consensus_count": _METRICS.no_consensus_count,
            "all_failed_count": _METRICS.all_failed_count,
            "avg_elapsed_ms": round(_METRICS.avg_elapsed_ms(), 3),
            "max_elapsed_ms": round(_METRICS.elapsed_ms_max, 3),
            "quorum_distribution": dict(_METRICS.quorum_distribution),
            "confirmed_rate": (
                round(_METRICS.confirmed_count / _METRICS.total_runs, 4)
                if _METRICS.total_runs else 0.0
            ),
        }


def reset_for_test() -> None:
    """Test helper: zero out metrics + circuit breaker."""
    global _METRICS, _CB_STATE
    with _METRICS_LOCK:
        _METRICS = TournamentMetrics()
    with _CB_LOCK:
        _CB_STATE = CircuitBreakerState()