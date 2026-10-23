"""Performance benchmarks for reflection-system hot paths (v231+).

Pure-stdlib: timeit + synthetic data.  No pytest-benchmark dep.

Each benchmark function returns wall-clock seconds for a representative
workload. The `tests/test_perf_benchmarks.py` then asserts a CI threshold
(generous 3-5x of current value) so regressions are caught.

Why not pytest-benchmark? The project has a strict "0 new dependencies"
convention. timeit + custom asserts give the same signal at zero cost.
"""
from __future__ import annotations

import random
import timeit
from typing import Any, Dict, List, Tuple

# Re-export from the two modules we want to bench
import reflection_calibration as rc  # noqa: E402
import meta_loop_stopping as ms       # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Synthetic data generators
# ─────────────────────────────────────────────────────────────────────

def synth_aggs(n_groups: int = 200) -> Dict[Tuple[str, str, str], Dict[str, float]]:
    """Build n_groups random aggregates with realistic n/win_rate/luck."""
    rng = random.Random(42)
    out: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    for i in range(n_groups):
        key = (f"layer_{i % 5}", f"sub_{i % 10}", f"sig_{i}")
        n = rng.randint(5, 200)
        wr = rng.uniform(0.2, 0.85)
        luck = rng.uniform(-0.4, 0.5)
        out[key] = {"n": n, "win_rate": wr, "avg_luck": luck}
    return out


def synth_signals(n: int = 200) -> List[Tuple[str, str]]:
    """Build n (signal_key, kind) pairs for the loop planner."""
    rng = random.Random(42)
    kinds = ["invalidated", "confirmed", "refined", "stable_skill", "unstable_luck"]
    return [(f"sig_{i}", rng.choice(kinds)) for i in range(n)]

def bench_overall_f1(n_groups: int = 200, n_iter: int = 100) -> float:
    """Time _overall_f1 over n_groups × n_iter calls (synthetic)."""
    aggs = synth_aggs(n_groups)
    params = {"wr_delta_refine": 0.15, "confirm_band": 0.05,
              "min_samples": 5, "luck_threshold": 0.30}
    t = timeit.timeit(
        lambda: rc._overall_f1(aggs, **params),
        number=n_iter,
    )
    return t


def bench_classify_one(n_iter: int = 10000) -> float:
    """Time _classify_one on a typical agg dict, n_iter times."""
    agg = {"n": 50, "win_rate": 0.6, "avg_luck": 0.1}
    t = timeit.timeit(
        lambda: rc._classify_one(agg, 0.15, 0.05, 5, 0.3),
        number=n_iter,
    )
    return t


def bench_bootstrap_score(n_groups: int = 200, n_iter: int = 30) -> float:
    """Time _bootstrap_score (the inner calibration loop)."""
    aggs = synth_aggs(n_groups)
    params = {"wr_delta_refine": 0.15, "confirm_band": 0.05,
              "min_samples": 5, "luck_threshold": 0.30}
    t = timeit.timeit(
        lambda: rc._bootstrap_score(aggs, params, n_iter=20),
        number=n_iter,
    )
    return t


def bench_should_continue(n_iter: int = 10000) -> float:
    """Time should_continue on a typical prior / current pair."""
    prior = ["confirmed"] * 100
    curr = ["invalidated"] * 100
    t = timeit.timeit(
        lambda: ms.should_continue(prior, curr),
        number=n_iter,
    )
    return t


def bench_plan_levels(n_iter: int = 100) -> float:
    """Time plan_levels with 3 levels, ~50 signals each."""
    def runner(L, prev):
        kinds = ["invalidated", "confirmed", "refined"]
        return [(f"sig_{i}", kinds[(i + L) % 3]) for i in range(50)]
    t = timeit.timeit(
        lambda: ms.plan_levels([], runner, max_level=3),
        number=n_iter,
    )
    return t


def bench_level_entropy(n_iter: int = 10000) -> float:
    """Time level_entropy (Shannon entropy)."""
    kinds = ["a", "b", "c", "a", "b", "a", "d", "a", "a", "c"] * 10
    t = timeit.timeit(
        lambda: ms.level_entropy(kinds),
        number=n_iter,
    )
    return t


def bench_e2e_calibration(n_groups: int = 50, n_per: int = 30,
                          grid_size: int = 5) -> float:
    """Time calibrate_param on a small grid with bootstrap, 1 param."""
    aggs = synth_aggs(n_groups)
    grid = [0.10, 0.125, 0.15, 0.175, 0.20][:grid_size]
    other = {"confirm_band": 0.05, "min_samples": 5, "luck_threshold": 0.30}
    t = timeit.timeit(
        lambda: rc.calibrate_param(
            "wr_delta", grid, aggs, other, n_bootstrap=n_per),
        number=1,
    )
    return t
BENCHMARKS: Dict[str, Tuple[Any, float, str]] = {
    "classify_one":      (bench_classify_one,      0.50,
                          "10k calls of _classify_one on a single agg"),
    "overall_f1":        (bench_overall_f1,        0.50,
                          "100 calls of _overall_f1 over 200 aggs"),
    "bootstrap_score":   (bench_bootstrap_score,   1.50,
                          "30 calls of _bootstrap_score (n_iter=20 each)"),
    "should_continue":   (bench_should_continue,   2.50,
                          "10k calls of should_continue"),
    "level_entropy":     (bench_level_entropy,     1.00,
                          "10k calls of level_entropy"),
    "plan_levels":       (bench_plan_levels,       0.50,
                          "100 calls of plan_levels, 3 levels x 50 sigs"),
    "e2e_calibration":   (bench_e2e_calibration,   1.00,
                          "1 calibrate_param (5-point grid x 30 bootstraps)"),
}


def run_all() -> Dict[str, float]:
    """Run all benchmarks; return name -> seconds."""
    out: Dict[str, float] = {}
    for name, (fn, _thresh, _note) in BENCHMARKS.items():
        t = fn()
        out[name] = t
    return out


def format_report(results: Dict[str, float]) -> str:
    """Pretty-print benchmark results with thresholds."""
    lines = ["perf_benchmarks (v231+)", "=" * 60]
    for name, t in results.items():
        fn, thresh, note = BENCHMARKS[name]
        ok = "PASS" if t <= thresh else "FAIL"
        lines.append("  [{:4s}]  {:8.4f}s  <= {:.2f}s   {}".format(
            ok, t, thresh, name))
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    results = run_all()
    print(format_report(results))

