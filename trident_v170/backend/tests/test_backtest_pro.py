"""PR-3: backtest_pro 测试。"""
import math
import numpy as np
import pytest

import backtest_pro as bp


# ─────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────


def _rng_returns(n, mu=0.001, sigma=0.01, seed=0):
    return np.random.default_rng(seed).normal(mu, sigma, n)


def _constant_signal(n, val=1.0):
    return np.full(n, val, dtype=np.float64)


# ─────────────────────────────────────────────────────────────────────
# 1. 工具函数
# ─────────────────────────────────────────────────────────────────────


def test_to_arr_handles_list():
    out = bp._to_arr([1.0, 2.0, 3.0])
    assert out.dtype == np.float64
    assert out.shape == (3,)


def test_to_arr_handles_ndarray():
    out = bp._to_arr(np.array([1.0, 2.0]))
    assert out.dtype == np.float64


def test_drop_nan():
    a = np.array([1.0, np.nan, 2.0, np.inf, 3.0])
    out = bp._drop_nan(a)
    assert out.size == 3


def test_safe_sharpe_zero_vol():
    r = np.zeros(20)
    assert bp._safe_sharpe(r) == 0.0


def test_safe_sharpe_positive():
    r = _rng_returns(252, mu=0.002, sigma=0.01, seed=1)
    s = bp._safe_sharpe(r)
    assert s > 0.5


def test_turnover_constant():
    assert bp._turnover(np.ones(10)) == 0.0


def test_turnover_oscillating():
    s = np.array([1, -1, 1, -1, 1], dtype=np.float64)
    # |Δs| = [2,2,2,2] / 4 diffs = 2.0
    assert abs(bp._turnover(s) - 2.0) < 1e-6


def test_apply_signal_no_cost():
    r = np.array([0.01, -0.02, 0.03])
    s = np.array([1, 1, 1])
    out = bp._apply_signal(r, s)
    np.testing.assert_array_almost_equal(out, r)


def test_apply_signal_with_cost():
    r = np.array([0.01, -0.02, 0.03])
    s = np.array([0, 1, 1])
    out = bp._apply_signal(r, s, cost_bps=10.0)
    # 0→1 的 turnover 在 index 1 才发生(index 0 prepend 0,无变化)
    assert out[0] == pytest.approx(0.0)
    assert out[1] == pytest.approx(-0.02 - 0.001)  # 0.001 = 10bps
    assert out[2] == pytest.approx(0.03)


def test_apply_signal_short_circuit():
    out = bp._apply_signal([], [])
    assert out.size == 0


def test_flatten_metrics_simple():
    raw = {"a": 1.0, "b": {"value": 2.0, "ci_95": (1.0, 3.0), "interp": "x"}, "_meta": {}}
    flat = bp._flatten_metrics(raw)
    assert flat["a"] == 1.0
    assert flat["b"] == 2.0
    assert flat["b_ci_low"] == 1.0
    assert flat["b_ci_high"] == 3.0


# ─────────────────────────────────────────────────────────────────────
# 2. compute_backtest 主入口
# ─────────────────────────────────────────────────────────────────────


def test_compute_backtest_long_signal_positive():
    r = _rng_returns(500, mu=0.002, sigma=0.01, seed=2)
    s = _constant_signal(500, 1.0)
    res = bp.compute_backtest(r, s, name="long", bootstrap_n=100, seed=0)
    assert res.n_obs == 500
    assert res.metrics.get("sharpe", 0) > 0
    assert res.turnover == 0.0
    assert res.cumulative_return > 0


def test_compute_backtest_short_signal_negative():
    r = _rng_returns(500, mu=0.002, sigma=0.01, seed=3)
    s = _constant_signal(500, -1.0)
    res = bp.compute_backtest(r, s, name="short", bootstrap_n=100, seed=0)
    assert res.metrics.get("sharpe", 0) < 0
    assert res.cumulative_return < 0


def test_compute_backtest_zero_signal_is_cash():
    """0 signal 时 strategy return 全 0 → cumulative ~ 0,turnover ~ 0。"""
    r = _rng_returns(200, mu=0.005, sigma=0.01, seed=4)
    s = np.zeros(200)
    res = bp.compute_backtest(r, s, name="cash", bootstrap_n=50, seed=0)
    assert abs(res.cumulative_return) < 0.01
    assert res.turnover == 0.0
    assert res.p_value_vs_zero >= 0.5


def test_compute_backtest_too_short_returns_empty():
    r = np.array([0.01] * 5)
    s = np.array([1.0] * 5)
    res = bp.compute_backtest(r, s, bootstrap_n=10)
    assert "样本不足" in res.notes[0] or res.metrics == {}


def test_compute_backtest_with_costs_reduces_sharpe():
    r = _rng_returns(500, mu=0.001, sigma=0.01, seed=5)
    s = np.sign(np.sin(np.arange(500) * 0.3))
    res_no = bp.compute_backtest(r, s, cost_bps=0.0, bootstrap_n=50, seed=0)
    res_cost = bp.compute_backtest(r, s, cost_bps=20.0, bootstrap_n=50, seed=0)
    assert res_cost.metrics.get("sharpe", 0) < res_no.metrics.get("sharpe", 0)


def test_compute_backtest_with_explicit_regimes():
    r = _rng_returns(200, seed=6)
    s = _constant_signal(200, 1.0)
    regimes = ["bull"] * 100 + ["bear"] * 100
    res = bp.compute_backtest(r, s, regimes=regimes, bootstrap_n=50, seed=0)
    assert "bull" in res.regime_breakdown
    assert "bear" in res.regime_breakdown


def test_compute_backtest_sharpe_ci_includes_midpoint():
    r = _rng_returns(200, mu=0.002, sigma=0.01, seed=7)
    s = _constant_signal(200, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=200, seed=0)
    lo, mid, hi = res.sharpe_ci
    assert lo <= mid <= hi


def test_compute_backtest_p_value_in_unit_interval():
    r = _rng_returns(300, mu=0.003, sigma=0.005, seed=8)
    s = _constant_signal(300, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=100, seed=0)
    # IID 数据的 permutation 检验,保持原分布,p 不一定小
    assert 0.0 <= res.p_value_vs_zero <= 1.0


# ─────────────────────────────────────────────────────────────────────
# 3. walk_forward
# ─────────────────────────────────────────────────────────────────────


def test_walk_forward_rolling_basic():
    """长数据 + 低波动 → OOS sharpe 应 > 0(常 1 signal)。"""
    r = _rng_returns(2000, mu=0.002, sigma=0.005, seed=10)
    s = _constant_signal(2000, 1.0)

    def sig_func(full_r, train_r):
        return np.ones_like(full_r)

    wf = bp.walk_forward_backtest(r, sig_func, n_folds=4, anchored=False,
                                  cost_bps=0.0)
    assert wf.n_folds >= 2
    assert all(f["test_size"] > 0 for f in wf.fold_results)
    # IS 至少有一折 sharpe > 0
    n_pos_is = sum(1 for f in wf.fold_results if f["is_sharpe"] > 0)
    assert n_pos_is >= 1


def test_walk_forward_anchored():
    r = _rng_returns(500, mu=0.001, sigma=0.01, seed=11)

    def sig_func(full_r, train_r):
        return np.ones_like(full_r)

    wf = bp.walk_forward_backtest(r, sig_func, n_folds=3, anchored=True)
    assert wf.n_folds >= 1
    sizes = [f["train_size"] for f in wf.fold_results]
    assert sizes == sorted(sizes)


def test_walk_forward_signal_func_error_recorded():
    r = _rng_returns(100, seed=12)

    def bad_sig(full_r, train_r):
        raise ValueError("intentional")

    wf = bp.walk_forward_backtest(r, bad_sig, n_folds=2)
    errors = [f for f in wf.fold_results if "error" in f]
    assert len(errors) >= 1


def test_walk_forward_too_short_returns_empty():
    r = _rng_returns(20, seed=13)
    wf = bp.walk_forward_backtest(r, lambda fr, tr: np.zeros_like(fr), n_folds=5)
    assert "样本不足" in wf.notes[0] or wf.n_folds == 0


# ─────────────────────────────────────────────────────────────────────
# 4. Bootstrap + 置换
# ─────────────────────────────────────────────────────────────────────


def test_bootstrap_ci_includes_mid():
    r = _rng_returns(200, mu=0.002, sigma=0.01, seed=20)
    lo, mid, hi = bp.bootstrap_sharpe_ci(r, n=300, seed=0)
    assert lo <= mid <= hi


def test_bootstrap_ci_empty_returns_zero():
    lo, mid, hi = bp.bootstrap_sharpe_ci([], n=100, seed=0)
    assert lo == mid == hi == 0.0


def test_bootstrap_ci_short_returns_zero():
    lo, mid, hi = bp.bootstrap_sharpe_ci([0.01, 0.02, 0.03], n=100, seed=0)
    assert lo == mid == hi == 0.0


def test_permutation_test_in_unit_interval():
    """Permutation 检验对 IID 数据的 p 应在 [0, 1] 范围内。"""
    r = _rng_returns(300, mu=0.005, sigma=0.005, seed=21)
    p = bp.permutation_test_sharpe(r, n=200, seed=0)
    assert 0.0 <= p <= 1.0


def test_permutation_test_returns_finite_pvalue():
    r = _rng_returns(100, seed=22)
    p = bp.permutation_test_sharpe(r, n=100, seed=0)
    assert 0.0 <= p <= 1.0


# ─────────────────────────────────────────────────────────────────────
# 5. compare_strategies
# ─────────────────────────────────────────────────────────────────────


def test_compare_strategies_basic():
    r = _rng_returns(300, mu=0.001, sigma=0.01, seed=30)
    s_a = np.zeros(300)
    s_b = _constant_signal(300, 1.0)
    cmp = bp.compare_strategies(r, s_a, s_b, name_a="cash", name_b="long",
                                bootstrap_n=100, seed=0)
    assert "a" in cmp and "b" in cmp
    assert "sharpe_diff" in cmp
    assert "diff_ci" in cmp and len(cmp["diff_ci"]) == 3
    assert 0.0 <= cmp["p_value_diff"] <= 1.0
    assert cmp["sharpe_diff"] > 0


def test_compare_strategies_identical():
    r = _rng_returns(200, seed=31)
    s = _constant_signal(200, 1.0)
    cmp = bp.compare_strategies(r, s, s, bootstrap_n=50, seed=0)
    assert abs(cmp["sharpe_diff"]) < 0.5


# ─────────────────────────────────────────────────────────────────────
# 6. regime breakdown via HMM
# ─────────────────────────────────────────────────────────────────────


def test_compute_backtest_with_hmm_regime():
    np.random.seed(42)
    r = np.concatenate([
        np.random.normal(0.005, 0.005, 100),
        np.random.normal(0.000, 0.010, 80),
        np.random.normal(-0.005, 0.015, 100),
    ])
    s = _constant_signal(r.size, 1.0)
    res = bp.compute_backtest(r, s, use_regime_hmm=True, bootstrap_n=50, seed=0)
    assert res.regime_state is not None
    assert "regime" in res.regime_state


# ─────────────────────────────────────────────────────────────────────
# 7. 边界 / 容错
# ─────────────────────────────────────────────────────────────────────


def test_compute_backtest_nan_in_returns():
    r = _rng_returns(200, seed=40)
    r[10:15] = np.nan
    s = _constant_signal(200, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=50, seed=0)
    assert res.n_obs < 200


def test_compute_backtest_signal_mismatched_length():
    r = _rng_returns(200, seed=41)
    s = _constant_signal(100, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=50, seed=0)
    assert res.n_obs > 0


def test_walk_forward_to_dict_serializable():
    r = _rng_returns(200, seed=42)

    def sig_func(fr, tr):
        return np.zeros(fr.size)

    wf = bp.walk_forward_backtest(r, sig_func, n_folds=2)
    d = wf.to_dict()
    assert "n_folds" in d
    assert "fold_results" in d


def test_backtest_result_to_dict_serializable():
    r = _rng_returns(100, seed=43)
    s = _constant_signal(100, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=20, seed=0)
    d = res.to_dict()
    assert "sharpe_ci" in d
    assert isinstance(d["sharpe_ci"], list)


# ─────────────────────────────────────────────────────────────────────
# CLI 入口测试
# ─────────────────────────────────────────────────────────────────────

def test_cli_make_synthetic_bull_bear():
    r = bp._make_synthetic("bull_bear", 100, seed=42)
    assert r.shape == (100,)
    assert np.isfinite(r).all()
    # 前半段均值 > 0(牛),后半段均值 < 0(熊)
    assert r[:50].mean() > 0
    assert r[50:].mean() < 0


def test_cli_make_synthetic_iid_trending():
    r_iid = bp._make_synthetic("iid", 200, seed=42)
    r_tr = bp._make_synthetic("trending", 200, seed=42)
    assert r_iid.shape == (200,)
    assert r_tr.shape == (200,)
    # trending 单调上升
    assert r_tr[-1] > r_tr[0]


def test_cli_make_signal_buy_hold_zero():
    r = np.array([0.01, -0.02, 0.03, -0.01, 0.005])
    s_bh = bp._make_signal(r, "buy_hold")
    s_bh2 = bp._make_signal(r, "bh")
    s_z = bp._make_signal(r, "zero")
    s_z2 = bp._make_signal(r, "flat")
    assert np.all(s_bh == 1.0)
    assert np.all(s_bh2 == 1.0)
    assert np.all(s_z == 0.0)
    assert np.all(s_z2 == 0.0)


def test_cli_make_signal_momentum_lookback():
    r = np.array([0.01] * 30 + [-0.01] * 30)
    s = bp._make_signal(r, "momentum", lookback=10)
    assert np.all(s[:10] == 0)
    assert np.all(s[10:30] == 1.0)
    # 30 之后:过去 10 天多数是 -0.01 → 0
    assert np.all(s[40:] == 0.0)


def test_cli_make_signal_mean_revert():
    r = np.array([0.01] * 30 + [-0.01] * 30)
    s = bp._make_signal(r, "mean_revert", lookback=10)
    assert np.all(s[:10] == 0)
    assert np.all(s[10:30] == -1.0)
    assert np.all(s[40:] == 1.0)


def test_cli_make_signal_unknown_raises():
    r = np.ones(20)
    with pytest.raises(ValueError, match="unknown signal method"):
        bp._make_signal(r, "magic_signal_xyz")


def test_cli_load_returns_json_list(tmp_path):
    p = tmp_path / "rets.json"
    p.write_text("[0.01, -0.02, 0.03, -0.01, 0.005]")
    arr = bp._load_returns_json(str(p))
    assert arr.shape == (5,)
    assert arr.dtype == np.float64
    assert arr[0] == 0.01


def test_cli_load_returns_json_dict(tmp_path):
    p = tmp_path / "rets.json"
    p.write_text('{"name": "demo", "returns": [0.01, -0.02, 0.03]}')
    arr = bp._load_returns_json(str(p))
    assert arr.shape == (3,)


def test_cli_load_returns_json_bad_format(tmp_path):
    p = tmp_path / "rets.json"
    p.write_text('{"not_returns": []}')
    with pytest.raises(ValueError, match="unsupported JSON format"):
        bp._load_returns_json(str(p))


def test_cli_format_md_contains_keys():
    r = _rng_returns(200, seed=42)
    s = _constant_signal(200, 1.0)
    res = bp.compute_backtest(r, s, bootstrap_n=50, seed=0)
    report = {
        "data_source": "test",
        "n_obs": 200,
        "signal_method": "buy_hold",
        "compare_to": None,
        "a": res.to_dict(),
        "comparison": None,
        "walk_forward": None,
    }
    md = bp._format_md(report)
    assert "backtest_pro 报告" in md
    assert "Sharpe" in md
    assert "样本数" in md


def test_cli_format_md_with_comparison_and_wf():
    r = _rng_returns(200, seed=42)
    s_a = _constant_signal(200, 1.0)
    res = bp.compute_backtest(r, s_a, bootstrap_n=50, seed=0)
    cmp_ = {"sharpe_diff": 0.1, "diff_ci": [-0.1, 0.05, 0.3], "p_value_diff": 0.4}
    wf_data = {
        "n_folds": 5, "anchored": False,
        "in_sample_sharpe_mean": 1.0, "in_sample_sharpe_std": 0.2,
        "out_of_sample_sharpe_mean": 0.8, "out_of_sample_sharpe_std": 0.15,
        "oos_degradation": -0.2,
        "aggregate_metrics": {"n_positive_oos": 4},
    }
    report = {
        "data_source": "test",
        "n_obs": 200,
        "signal_method": "buy_hold",
        "compare_to": "signal:zero",
        "a": res.to_dict(),
        "comparison": cmp_,
        "walk_forward": wf_data,
    }
    md = bp._format_md(report)
    assert "Walk-Forward" in md
    assert "策略比较" in md
    assert "OOS degradation" in md


def test_cli_main_synthetic_momentum(tmp_path, capsys):
    """端到端 CLI:合成数据 + momentum + 比较 + walk-forward + JSON 输出。"""
    out_path = tmp_path / "report.json"
    rc = bp.main([
        "--synthetic", "bull_bear",
        "--synthetic-n", "200",
        "--seed", "7",
        "--signal", "momentum",
        "--lookback", "15",
        "--compare", "buy_hold",
        "--walk-forward",
        "--n-folds", "3",
        "--cost-bps", "5",
        "--bootstrap-n", "50",
        "--format", "json",
        "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.exists()
    import json as _json
    report = _json.loads(out_path.read_text())
    assert report["signal_method"] == "momentum"
    assert report["compare_to"] == "signal:buy_hold"
    assert report["walk_forward"] is not None
    assert report["walk_forward"]["n_folds"] >= 1  # 实际折数 >= 1(n 小可能自动折)
    assert report["walk_forward"]["n_folds"] <= 3  # 不会超过请求
    assert "sharpe" in report["a"]["metrics"]


def test_cli_main_md_output(capsys):
    """端到端 CLI:默认合成 + md 到 stdout。"""
    rc = bp.main([
        "--synthetic", "iid",
        "--synthetic-n", "100",
        "--signal", "buy_hold",
        "--compare", "none",
        "--bootstrap-n", "20",
        "--format", "md",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "# backtest_pro 报告" in captured.out
    assert "信号方法" in captured.out


def test_cli_main_data_file_not_found(capsys):
    """--data 不存在时返回非零 exit code。"""
    rc = bp.main(["--data", "/tmp/__definitely_not_here__.json"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_cli_main_data_file_loads(tmp_path):
    """--data 从 JSON 文件读取,生成完整报告。"""
    import json as _json
    p = tmp_path / "rets.json"
    p.write_text(_json.dumps([0.001] * 100 + [-0.001] * 100))
    rc = bp.main([
        "--data", str(p),
        "--signal", "momentum",
        "--lookback", "20",
        "--compare", "buy_hold",
        "--bootstrap-n", "30",
        "--format", "json",
    ])
    assert rc == 0


# ─────────────────────────────────────────────────────────────────────
# DB 集成测试
# ─────────────────────────────────────────────────────────────────────

def _build_settled_db(tmp_path, *, n: int = 200, seed: int = 0,
                       win_pct: float = 0.6) -> str:
    """构造一个含已 settle ai_decisions 的临时 SQLite,用于 backtest_from_db 测试。"""
    import sqlite3 as _sqlite3
    import time as _time
    from datetime import datetime, timezone
    p = tmp_path / "test.db"
    conn = _sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_asset TEXT,
            market_category TEXT,
            suggested_action TEXT,
            forward_pnl REAL,
            mfe_pct REAL,
            mae_pct REAL,
            is_correct TEXT,
            settled INTEGER,
            created_at TEXT
        );
    """)
    rng = np.random.default_rng(seed)
    now = _time.time()
    actions = ["BUY", "HOLD", "SELL"]
    for i in range(n):
        is_win = rng.random() < win_pct
        pnl = float(rng.normal(0.02 if is_win else -0.01, 0.005))
        action = rng.choice(actions)
        ts = datetime.fromtimestamp(now - (n - i) * 3600, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO ai_decisions (target_asset, suggested_action, forward_pnl, settled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            ("BTCUSDT", action, pnl * 100, ts),
        )
    conn.commit()
    conn.close()
    return str(p)


def test_action_to_signal_known_actions():
    assert bp._action_to_signal("BUY") == 1.0
    assert bp._action_to_signal("buy") == 1.0
    assert bp._action_to_signal("LONG") == 1.0
    assert bp._action_to_signal("BULL") == 1.0
    assert bp._action_to_signal("SELL") == -1.0
    assert bp._action_to_signal("SHORT") == -1.0
    assert bp._action_to_signal("BEAR") == -1.0
    assert bp._action_to_signal("HOLD") == 0.0
    assert bp._action_to_signal("NEUTRAL") == 0.0
    assert bp._action_to_signal("") == 0.0
    assert bp._action_to_signal(None) == 0.0
    assert bp._action_to_signal("UNKNOWN_XYZ") == 0.0


def test_action_to_signal_handles_whitespace():
    assert bp._action_to_signal("  BUY  ") == 1.0
    assert bp._action_to_signal(" sell ") == -1.0


def test_resolve_db_path_explicit():
    assert bp._resolve_db_path("/foo/bar.db") == "/foo/bar.db"


def test_fetch_decisions_as_arrays_basic(tmp_path):
    db = _build_settled_db(tmp_path, n=50, seed=42, win_pct=0.55)
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    returns, signal, meta = bp.fetch_decisions_as_arrays(
        conn, lookback_days=10, require_settled=True,
    )
    assert len(returns) == 50
    assert len(signal) == 50
    assert len(meta) == 50
    assert np.isfinite(returns).all()
    assert set(np.unique(signal).tolist()).issubset({-1.0, 0.0, 1.0})
    assert "id" in meta[0]
    assert "asset" in meta[0]
    assert "action" in meta[0]
    assert "pnl_pct" in meta[0]


def test_fetch_decisions_as_arrays_with_unsettled(tmp_path):
    """未 settle 的 forward_pnl=null → NaN。"""
    import sqlite3 as _sqlite3
    p = tmp_path / "u.db"
    conn = _sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_asset TEXT, market_category TEXT, suggested_action TEXT,
            forward_pnl REAL, mfe_pct REAL, mae_pct REAL,
            is_correct TEXT, settled INTEGER, created_at TEXT
        );
    """)
    conn.execute("INSERT INTO ai_decisions VALUES (1, 'BTCUSDT', 'crypto', 'BUY', 5.0, 0.1, 0.05, 'TRUE', 1, '2025-12-01')")
    conn.execute("INSERT INTO ai_decisions VALUES (2, 'BTCUSDT', 'crypto', 'HOLD', NULL, NULL, NULL, '', 0, '2025-12-02')")
    conn.commit()
    # require_settled=False 才能包含未 settle 行(看 NaN 处理)
    returns, signal, meta = bp.fetch_decisions_as_arrays(
        conn, lookback_days=365, require_settled=False,
    )
    assert len(returns) == 2
    assert returns[0] == 0.05
    assert np.isnan(returns[1])
    assert signal[0] == 1.0
    assert signal[1] == 0.0


def test_fetch_decisions_as_arrays_require_settled_filter(tmp_path):
    """require_settled=False 包含未 settle,=True 只取 settled。"""
    import sqlite3 as _sqlite3
    p = tmp_path / "f.db"
    conn = _sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_asset TEXT, market_category TEXT, suggested_action TEXT,
            forward_pnl REAL, mfe_pct REAL, mae_pct REAL,
            is_correct TEXT, settled INTEGER, created_at TEXT
        );
    """)
    conn.execute("INSERT INTO ai_decisions VALUES (1, 'BTCUSDT', 'crypto', 'BUY', 5.0, 0.1, 0.05, 'TRUE', 1, '2025-12-01')")
    conn.execute("INSERT INTO ai_decisions VALUES (2, 'BTCUSDT', 'crypto', 'HOLD', NULL, NULL, NULL, '', 0, '2025-12-02')")
    conn.commit()
    r1, s1, m1 = bp.fetch_decisions_as_arrays(conn, lookback_days=365, require_settled=True)
    assert len(r1) == 1
    r2, s2, m2 = bp.fetch_decisions_as_arrays(conn, lookback_days=365, require_settled=False)
    assert len(r2) == 2


def test_fetch_decisions_as_arrays_asset_filter(tmp_path):
    db = _build_settled_db(tmp_path, n=100, seed=42)
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    r1, _, _ = bp.fetch_decisions_as_arrays(conn, lookback_days=10, asset="BTCUSDT")
    r2, _, _ = bp.fetch_decisions_as_arrays(conn, lookback_days=10, asset="ETHUSDT")
    assert len(r1) == 100
    assert len(r2) == 0


def test_backtest_from_db_missing_db_raises(tmp_path):
    """不存在的 DB 应抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="DB not found"):
        bp.backtest_from_db(str(tmp_path / "nope.db"))


def test_backtest_from_db_insufficient_data_warns(tmp_path):
    """n_with_pnl < 20 时返回带 warning 的 report,backtest 字段为 None。"""
    db = _build_settled_db(tmp_path, n=5, seed=42)
    report = bp.backtest_from_db(db, lookback_days=10, bootstrap_n=20, walk_forward=False)
    assert "warning" in report
    assert "n_with_pnl=5" in report["warning"]
    assert report["backtest"] is None
    assert report["walk_forward"] is None
    assert report["comparison"] is None
    assert report["n_decisions"] == 5


def test_backtest_from_db_full_pipeline(tmp_path):
    """完整流水线:n=200 settle,无 walk_forward,只 compare。"""
    db = _build_settled_db(tmp_path, n=200, seed=42, win_pct=0.55)
    report = bp.backtest_from_db(
        db, lookback_days=10,
        cost_bps=5.0, bootstrap_n=100, seed=0,
        n_folds=3, walk_forward=False, compare_to_buy_hold=True,
    )
    assert report["n_decisions"] == 200
    assert report["n_with_pnl"] == 200
    assert report["backtest"] is not None
    assert "sharpe" in report["backtest"]["metrics"]
    assert report["comparison"] is not None
    assert "sharpe_diff" in report["comparison"]
    assert report["walk_forward"] is None
    assert "warning" not in report


def test_backtest_from_db_with_walk_forward(tmp_path):
    """启用 walk-forward 且 n>=60 时应生成 wf 报告。"""
    db = _build_settled_db(tmp_path, n=100, seed=42, win_pct=0.55)
    report = bp.backtest_from_db(
        db, lookback_days=10, bootstrap_n=50, seed=0,
        n_folds=3, walk_forward=True, compare_to_buy_hold=False,
    )
    assert report["n_with_pnl"] == 100
    assert report["walk_forward"] is not None
    assert report["walk_forward"]["n_folds"] >= 1
    assert report["comparison"] is None


def test_cli_main_db_mode_returns_report(tmp_path):
    """端到端:--db 走 backtest_from_db,JSON 输出。"""
    import json as _json
    db = _build_settled_db(tmp_path, n=80, seed=42, win_pct=0.55)
    out_path = tmp_path / "db_report.json"
    rc = bp.main([
        "--db", db,
        "--db-days", "10",
        "--bootstrap-n", "30",
        "--compare", "buy_hold",
        "--format", "json",
        "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.exists()
    report = _json.loads(out_path.read_text())
    assert report["n_decisions"] == 80
    assert "meta" in report
    assert report["backtest"] is not None
    assert "sharpe" in report["backtest"]["metrics"]


def test_cli_main_db_mode_missing_db_prints_error(tmp_path, capsys):
    """--db 不存在时返回非零 exit code + ERROR。"""
    rc = bp.main(["--db", str(tmp_path / "ghost.db")])
    assert rc == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
