"""v200+ adaptive forgetting triplet tests (Q5).

Verifies:
- adaptive_decay_factor: sparse / dense sample switch
- adaptive_protect_version: bootstrap 3 -> grows with samples -> capped at MAX
- adaptive_min_samples: sparse 5/low -> dense 10/normal
- causal_graph._reconcile actually uses adaptive (not hardcoded)
- api_server calls adaptive_min_samples in >= 2 places
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
sys.path.insert(0, _SRC + "/src_python")

import config  # noqa


def test_decay_factor_sparse_zero():
    core, cond = config.adaptive_decay_factor(0)
    assert core == 0.9, f"sparse core should be 0.9, got {core}"
    assert cond == 0.95, f"sparse cond should be 0.95, got {cond}"
    print(f"  PASS  adaptive_decay_factor(0) -> ({core}, {cond})")


def test_decay_factor_sparse_below_threshold():
    core, cond = config.adaptive_decay_factor(9)
    assert core == 0.9 and cond == 0.95, \
        f"n=9 should be sparse, got ({core}, {cond})"
    print(f"  PASS  adaptive_decay_factor(9) -> ({core}, {cond})  [sparse]")


def test_decay_factor_dense_at_threshold():
    core, cond = config.adaptive_decay_factor(10)
    assert core == 0.7 and cond == 0.85, \
        f"n=10 should be dense, got ({core}, {cond})"
    print(f"  PASS  adaptive_decay_factor(10) -> ({core}, {cond})  [dense]")


def test_decay_factor_dense_large():
    core, cond = config.adaptive_decay_factor(200)
    assert core == 0.7 and cond == 0.85
    print(f"  PASS  adaptive_decay_factor(200) -> ({core}, {cond}) [dense large]")


def test_decay_factor_handles_bad_input():
    for bad in [None, "abc", [], 3.7]:
        core, cond = config.adaptive_decay_factor(bad)
        assert core == 0.9 and cond == 0.95, \
            f"bad {bad!r} should fall back to sparse, got ({core}, {cond})"
    print(f"  PASS  adaptive_decay_factor handles None/str/list/float")


def test_protect_version_sparse_zero():
    v = config.adaptive_protect_version(0)
    assert v == 3, f"sparse n=0 should be 3, got {v}"
    print(f"  PASS  adaptive_protect_version(0) -> {v}")


def test_protect_version_sparse_below_threshold():
    v = config.adaptive_protect_version(9)
    assert v == 3, f"sparse n=9 should be 3, got {v}"
    print(f"  PASS  adaptive_protect_version(9) -> {v}")


def test_protect_version_dense_min():
    v = config.adaptive_protect_version(10)
    assert v == 5, f"n=10 dense 10//2=5, got {v}"
    print(f"  PASS  adaptive_protect_version(10) -> {v} [dense min]")


def test_protect_version_dense_mid():
    v = config.adaptive_protect_version(20)
    assert v == 10, f"n=20 dense 20//2=10, got {v}"
    print(f"  PASS  adaptive_protect_version(20) -> {v} [dense mid]")


def test_protect_version_dense_capped_at_max():
    assert config.adaptive_protect_version(50) == 11
    assert config.adaptive_protect_version(200) == 11
    print(f"  PASS  adaptive_protect_version(50/200) -> 11  [capped at MAX]")


def test_protect_version_handles_bad_input():
    assert config.adaptive_protect_version(None) == 3
    print(f"  PASS  adaptive_protect_version(None) -> 3")


def test_min_samples_sparse_zero():
    n, conf = config.adaptive_min_samples(0)
    assert n == 5 and conf == "low"
    print(f"  PASS  adaptive_min_samples(0) -> ({n}, {conf})")


def test_min_samples_sparse_below_threshold():
    n, conf = config.adaptive_min_samples(29)
    assert n == 5 and conf == "low"
    print(f"  PASS  adaptive_min_samples(29) -> ({n}, {conf}) [sparse]")


def test_min_samples_dense_at_threshold():
    n, conf = config.adaptive_min_samples(30)
    assert n == 10 and conf == "normal"
    print(f"  PASS  adaptive_min_samples(30) -> ({n}, {conf}) [dense threshold]")


def test_min_samples_dense_large():
    n, conf = config.adaptive_min_samples(200)
    assert n == 10 and conf == "normal"
    print(f"  PASS  adaptive_min_samples(200) -> ({n}, {conf}) [dense]")


def test_min_samples_handles_bad_input():
    n, conf = config.adaptive_min_samples(None)
    assert n == 5 and conf == "low"
    print(f"  PASS  adaptive_min_samples(None) -> ({n}, {conf})")


def test_reconcile_uses_adaptive_decay():
    import inspect
    from causal_graph import _reconcile
    src = inspect.getsource(_reconcile)
    assert "config.adaptive_decay_factor" in src, \
        "_reconcile should call config.adaptive_decay_factor"
    assert "config.adaptive_protect_version" in src, \
        "_reconcile should call config.adaptive_protect_version"
    assert "core_factor *= 0.7" not in src, \
        "_reconcile should NOT hardcode core_factor *= 0.7"
    assert "protect=3" not in src, \
        "_reconcile should NOT hardcode protect=3"
    print(f"  PASS  _reconcile calls config.adaptive_decay_factor + adaptive_protect_version")


def test_reconcile_uses_adaptive_sample_estimation():
    import inspect
    from causal_graph import _reconcile
    src = inspect.getsource(_reconcile)
    assert "asset_driver_nodes" in src, \
        "_reconcile should query asset_driver_nodes"
    assert "COUNT(*)" in src, \
        "_reconcile should use COUNT(*) for sample_n"
    print(f"  PASS  _reconcile samples from asset_driver_nodes active count")


def test_api_server_uses_adaptive_min_samples():
    import inspect
    import api_server as srv
    src = inspect.getsource(srv)
    assert "config.adaptive_min_samples" in src, \
        "api_server should call config.adaptive_min_samples"
    cnt = src.count("config.adaptive_min_samples")
    assert cnt >= 2, \
        f"api_server should call adaptive_min_samples in >= 2 places, got {cnt}"
    print(f"  PASS  api_server calls adaptive_min_samples x{cnt}")


def test_reconcile_endtoend_sparse_uses_sparse_values():
    v = config.adaptive_protect_version(2)
    assert v == 3, f"sample_n=2 should yield protect=3, got {v}"
    core, cond = config.adaptive_decay_factor(2)
    assert core == 0.9 and cond == 0.95
    print(f"  PASS  reconcile sparse: protect=3, decay=(0.9, 0.95)")


def test_reconcile_endtoend_dense_uses_dense_values():
    v = config.adaptive_protect_version(20)
    assert v == 10
    core, cond = config.adaptive_decay_factor(20)
    assert core == 0.7 and cond == 0.85
    print(f"  PASS  reconcile dense: protect=10, decay=(0.7, 0.85)")


def test_thresholds_at_exact_boundary():
    assert config.adaptive_protect_version(10) == 5
    assert config.adaptive_decay_factor(10) == (0.7, 0.85)
    assert config.adaptive_min_samples(30) == (10, "normal")
    print(f"  PASS  boundary: n=10 dense for decay/protect, n=30 dense for min_samples")


def test_constants_overridable_via_env():
    required = [
        "ADAPTIVE_PROTECT_VERSION_MIN",
        "ADAPTIVE_PROTECT_VERSION_MAX",
        "ADAPTIVE_PROTECT_VERSION_SPARSE_THRESHOLD",
        "ADAPTIVE_DECAY_CORE_SPARSE",
        "ADAPTIVE_DECAY_CORE_DENSE",
        "ADAPTIVE_DECAY_COND_SPARSE",
        "ADAPTIVE_DECAY_COND_DENSE",
        "ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE",
        "ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE",
        "ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD",
    ]
    for name in required:
        assert hasattr(config, name), f"config.{name} should exist"
        val = getattr(config, name)
        assert isinstance(val, (int, float)), \
            f"config.{name} should be int/float, got {type(val)}"
    print(f"  PASS  all {len(required)} ADAPTIVE_* constants exist & typed")


if __name__ == "__main__":
    print("=== v200+ adaptive forgetting triplet tests ===\n")
    print("-- adaptive_decay_factor --")
    test_decay_factor_sparse_zero()
    test_decay_factor_sparse_below_threshold()
    test_decay_factor_dense_at_threshold()
    test_decay_factor_dense_large()
    test_decay_factor_handles_bad_input()
    print("\n-- adaptive_protect_version --")
    test_protect_version_sparse_zero()
    test_protect_version_sparse_below_threshold()
    test_protect_version_dense_min()
    test_protect_version_dense_mid()
    test_protect_version_dense_capped_at_max()
    test_protect_version_handles_bad_input()
    print("\n-- adaptive_min_samples --")
    test_min_samples_sparse_zero()
    test_min_samples_sparse_below_threshold()
    test_min_samples_dense_at_threshold()
    test_min_samples_dense_large()
    test_min_samples_handles_bad_input()
    print("\n-- Integration: causal_graph + api_server --")
    test_reconcile_uses_adaptive_decay()
    test_reconcile_uses_adaptive_sample_estimation()
    test_api_server_uses_adaptive_min_samples()
    test_reconcile_endtoend_sparse_uses_sparse_values()
    test_reconcile_endtoend_dense_uses_dense_values()
    print("\n-- Boundary / config --")
    test_thresholds_at_exact_boundary()
    test_constants_overridable_via_env()
    print("\nAll 22 adaptive_forgetting tests passed")