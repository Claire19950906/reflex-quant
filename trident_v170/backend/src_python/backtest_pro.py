"""v230p1 — Walk-Forward 回测 + Regime 条件分析 + Bootstrap。

承接 risk_metrics_pro + market_regime_hmm,提供:
  1. BacktestResult    完整回测结果(in-sample / out-of-sample / 显著性)
  2. walk_forward_backtest()    滚动/锚定 walk-forward
  3. regime_conditional_metrics()   按 HMM regime 拆分绩效
  4. bootstrap_sharpe_ci()    Sharpe 区间估计
  5. compare_strategies()    多策略并排比较

设计原则:
  - 纯 numpy + risk_metrics_pro + market_regime_hmm,不引入新依赖
  - 序列是 daily returns(小数),signal 是同长度 +1/-1/0 序列
  - 容错:序列太短 / 含 nan / signal 全 0 都安全退化

CLI:
  python3 -m backtest_pro --synthetic bull_bear --signal momentum --walk-forward
  python3 -m backtest_pro --data returns.json --signal mean_revert --compare buy_hold
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

import risk_metrics_pro as rmp


# ─────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────


def _to_arr(x):
    return np.asarray(list(x), dtype=np.float64) if not isinstance(x, np.ndarray) else x.astype(np.float64)


def _drop_nan(a):
    return a[np.isfinite(a)]


def _flatten_metrics(raw):
    """把 compute_portfolio_metrics_pro 的嵌套 dict 展平成 {key: float}。"""
    flat = {}
    for k, v in raw.items():
        if k == "_meta":
            continue
        if isinstance(v, dict):
            # pro 指标 value/ci_95
            if "value" in v and v["value"] is not None:
                flat[k] = float(v["value"])
            if "ci_95" in v and v["ci_95"] is not None:
                flat[f"{k}_ci_low"] = float(v["ci_95"][0])
                flat[f"{k}_ci_high"] = float(v["ci_95"][1])
            # 兼容 drawdown / winrate 等无 value 字段
            for sk, sv in v.items():
                if sk in ("value", "ci_95", "interpretation"):
                    continue
                if isinstance(sv, (int, float)) and math.isfinite(float(sv)):
                    flat[f"{k}_{sk}"] = float(sv)
        elif isinstance(v, (int, float)) and math.isfinite(float(v)):
            flat[k] = float(v)
    return flat


def _safe_sharpe(returns, rf=0.0, periods=252):
    r = _drop_nan(_to_arr(returns))
    if r.size < 5:
        return 0.0
    mu = float(np.mean(r)) - rf / periods
    sd = float(np.std(r, ddof=1))
    if sd < 1e-12:
        return 0.0
    return mu / sd * math.sqrt(periods)


# ─────────────────────────────────────────────────────────────────────
# 1. BacktestResult — 单次回测结果
# ─────────────────────────────────────────────────────────────────────


@dataclass
class BacktestResult:
    """单次回测(in-sample + out-of-sample)结果。"""
    name: str
    n_obs: int
    metrics: Dict[str, float]                    # 全部 SOTA 指标
    regime_breakdown: Dict[str, Dict[str, float]]  # per-regime metrics
    sharpe_ci: Tuple[float, float, float]         # (low, mid, high) bootstrap 95% CI
    p_value_vs_zero: float                        # Sharpe > 0 的置换检验 p-value
    cumulative_return: float
    annualized_return: float
    annualized_vol: float
    max_drawdown: float
    win_rate: float
    n_trades_per_year: float                      # 估算
    turnover: float                               # 换手率(信号变化率)
    in_sample_metrics: Optional[Dict[str, float]] = None
    out_of_sample_metrics: Optional[Dict[str, float]] = None
    regime_state: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["sharpe_ci"] = list(self.sharpe_ci)
        return d


# ─────────────────────────────────────────────────────────────────────
# 2. 核心:compute_backtest
# ─────────────────────────────────────────────────────────────────────


def _turnover(signal):
    """换手率 = |Δ signal| 的均值。signal ∈ {-1, 0, 1}。"""
    s = _to_arr(signal)
    if s.size < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(s))))


def _apply_signal(returns, signal, cost_bps=0.0):
    """把 signal 应用到 returns,返回 strategy_returns。cost_bps 单边手续费。"""
    r = _to_arr(returns)
    s = _to_arr(signal)
    n = min(r.size, s.size)
    if n == 0:
        return np.zeros(0)
    r = r[:n]
    s = s[:n]
    # strategy return = s * r - cost * |Δs|
    sr = s * r
    if cost_bps > 0:
        turnover_arr = np.abs(np.diff(s, prepend=0.0))
        sr = sr - turnover_arr * (cost_bps / 1e4)
    return sr


def compute_backtest(
    returns,
    signal,
    *,
    name: str = "strategy",
    periods: int = 252,
    cost_bps: float = 0.0,
    risk_free: float = 0.0,
    regimes: Optional[Sequence[str]] = None,
    use_regime_hmm: bool = False,
    bootstrap_n: int = 1000,
    seed: int = 0,
) -> BacktestResult:
    """单次回测主入口。

    参数:
      returns       日收益序列
      signal        同长度 +1/-1/0 序列
      periods       年化周期(默认 252)
      cost_bps      单边手续费(bps)
      risk_free     年化无风险利率
      regimes       已知 regime 标签(可选,与 use_regime_hmm 二选一)
      use_regime_hmm  用 market_regime_hmm 自动识别
      bootstrap_n   Bootstrap 抽样次数
      seed          随机种子
    """
    r = _drop_nan(_to_arr(returns))
    s = _to_arr(signal)[: r.size]
    if r.size < 10:
        return BacktestResult(
            name=name, n_obs=int(r.size), metrics={},
            regime_breakdown={}, sharpe_ci=(0.0, 0.0, 0.0),
            p_value_vs_zero=1.0, cumulative_return=0.0,
            annualized_return=0.0, annualized_vol=0.0,
            max_drawdown=0.0, win_rate=0.0, n_trades_per_year=0.0,
            turnover=0.0, notes=["样本不足(>10)"],
        )
    sr = _apply_signal(r, s, cost_bps=cost_bps)
    # 全部指标(转 list,risk_metrics.compute_drawdown 不接受 numpy)
    raw_metrics = rmp.compute_portfolio_metrics_pro(
        sr.tolist(), periods_per_year=periods, n_trials=1,
    )
    metrics = _flatten_metrics(raw_metrics)
    # 累计收益
    cum = float(np.prod(1 + sr) - 1)
    ann_ret = float((1 + cum) ** (periods / max(sr.size, 1)) - 1) if sr.size > 0 else 0.0
    ann_vol = float(np.std(sr, ddof=1) * math.sqrt(periods)) if sr.size > 1 else 0.0
    max_dd = float(metrics.get("max_drawdown", 0.0))
    # 胜率:正收益占比
    pos = sr[sr > 0]
    neg = sr[sr < 0]
    win_rate = float(len(pos) / max(len(pos) + len(neg), 1))
    # 估算年化交易数
    turn = _turnover(s)
    n_trades_per_year = float(turn * periods)
    # 换手率
    # Regime breakdown
    regime_breakdown = {}
    regime_state_dict = None
    if regimes is None and use_regime_hmm:
        try:
            import market_regime_hmm as mrh
            rs = mrh.classify_regime_hmm(r, n_iter=10)
            regimes = list(rs.viterbi_path)[-sr.size:]
            # 若 viterbi_path 长度不够,扩展
            if len(regimes) < sr.size:
                regimes = (["range"] * (sr.size - len(regimes))) + list(regimes)
            regime_state_dict = mrh.regime_state_to_dict(rs)
        except Exception as e:
            regimes = None
            regime_state_dict = None
    if regimes is not None:
        reg_arr = list(regimes)[: sr.size]
        unique = set(reg_arr)
        for reg in unique:
            mask = np.array([1 if r == reg else 0 for r in reg_arr], dtype=bool)
            sub = sr[mask]
            if sub.size >= 3:
                sub_raw = rmp.compute_portfolio_metrics_pro(
                    sub.tolist(), periods_per_year=periods, n_trials=1,
                )
                sub_metrics = _flatten_metrics(sub_raw)
                regime_breakdown[reg] = {k: float(v) for k, v in sub_metrics.items()}
            else:
                regime_breakdown[reg] = {"n_obs": int(sub.size), "note": "样本不足"}
    # Bootstrap CI
    ci = bootstrap_sharpe_ci(sr, n=bootstrap_n, seed=seed, periods=periods)
    # 置换检验:信号是否非零
    p_value = permutation_test_sharpe(sr, n=min(bootstrap_n, 500), seed=seed)
    return BacktestResult(
        name=name, n_obs=int(sr.size),
        metrics={k: float(v) for k, v in metrics.items()},
        regime_breakdown=regime_breakdown,
        sharpe_ci=ci,
        p_value_vs_zero=float(p_value),
        cumulative_return=cum,
        annualized_return=ann_ret,
        annualized_vol=ann_vol,
        max_drawdown=max_dd,
        win_rate=win_rate,
        n_trades_per_year=n_trades_per_year,
        turnover=turn,
        regime_state=regime_state_dict,
    )


# ─────────────────────────────────────────────────────────────────────
# 3. Walk-Forward
# ─────────────────────────────────────────────────────────────────────


@dataclass
class WalkForwardResult:
    """Walk-forward 多折结果。"""
    n_folds: int
    in_sample_sharpe_mean: float
    in_sample_sharpe_std: float
    out_of_sample_sharpe_mean: float
    out_of_sample_sharpe_std: float
    oos_degradation: float       # OOS mean / IS mean - 1, 负值表示过拟合
    fold_results: List[Dict[str, Any]]
    aggregate_metrics: Dict[str, float]
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def walk_forward_backtest(
    returns,
    signal_func: Callable[[np.ndarray, np.ndarray], np.ndarray],
    *,
    n_folds: int = 5,
    train_frac: float = 0.7,
    anchored: bool = False,
    periods: int = 252,
    cost_bps: float = 0.0,
    risk_free: float = 0.0,
    min_train_size: int = 30,
    min_test_size: int = 10,
    seed: int = 0,
) -> WalkForwardResult:
    """Walk-forward 验证。详见模块 docstring。"""
    r = _drop_nan(_to_arr(returns))
    T = int(r.size)
    if T < min_train_size + min_test_size:
        return WalkForwardResult(
            n_folds=0, in_sample_sharpe_mean=0.0, in_sample_sharpe_std=0.0,
            out_of_sample_sharpe_mean=0.0, out_of_sample_sharpe_std=0.0,
            oos_degradation=0.0, fold_results=[], aggregate_metrics={},
            notes=["样本不足"],
        )
    fold_results = []
    is_sharpes = []
    oos_sharpes = []
    for fold_i in range(n_folds):
        test_size = (T - min_train_size) // n_folds
        if test_size < min_test_size:
            break
        if anchored:
            train_end = min_train_size + fold_i * test_size
            test_end = train_end + test_size
        else:
            train_start = fold_i * test_size
            train_end = train_start + int((T * train_frac) / n_folds)
            test_end = train_end + test_size
        if test_end > T:
            break
        if anchored:
            train_r = r[:train_end]
        else:
            train_r = r[train_start:train_end]
        test_r = r[train_end:test_end]
        try:
            sig_full = np.asarray(signal_func(r, train_r), dtype=np.float64)
        except Exception as e:
            fold_results.append({"fold": fold_i, "error": str(e)})
            continue
        if sig_full.size != T:
            sig_full = np.zeros(T)
        if anchored:
            is_sig = sig_full[:train_end]
        else:
            is_sig = sig_full[train_start:train_end]
        is_sr = _apply_signal(train_r, is_sig, cost_bps=cost_bps)
        is_raw = rmp.compute_portfolio_metrics_pro(
            is_sr.tolist(), periods_per_year=periods, n_trials=1,
        )
        is_metrics = _flatten_metrics(is_raw)
        is_sharpe = float(is_metrics.get("sharpe", 0.0))
        oos_sig = sig_full[train_end:test_end]
        oos_sr = _apply_signal(test_r, oos_sig, cost_bps=cost_bps)
        oos_raw = rmp.compute_portfolio_metrics_pro(
            oos_sr.tolist(), periods_per_year=periods, n_trials=1,
        )
        oos_metrics = _flatten_metrics(oos_raw)
        oos_sharpe = float(oos_metrics.get("sharpe", 0.0))
        is_sharpes.append(is_sharpe)
        oos_sharpes.append(oos_sharpe)
        fold_results.append({
            "fold": fold_i, "train_size": int(train_r.size), "test_size": int(test_r.size),
            "is_sharpe": is_sharpe, "oos_sharpe": oos_sharpe,
            "is_metrics": {k: float(v) for k, v in is_metrics.items()},
            "oos_metrics": {k: float(v) for k, v in oos_metrics.items()},
        })
    if not is_sharpes:
        return WalkForwardResult(
            n_folds=0, in_sample_sharpe_mean=0.0, in_sample_sharpe_std=0.0,
            out_of_sample_sharpe_mean=0.0, out_of_sample_sharpe_std=0.0,
            oos_degradation=0.0, fold_results=fold_results, aggregate_metrics={},
            notes=["无有效折"],
        )
    is_arr = np.array(is_sharpes)
    oos_arr = np.array(oos_sharpes)
    is_mean = float(np.mean(is_arr))
    oos_mean = float(np.mean(oos_arr))
    degradation = (oos_mean / is_mean - 1.0) if abs(is_mean) > 1e-6 else 0.0
    aggregate = {
        "is_sharpe_mean": is_mean,
        "is_sharpe_std": float(np.std(is_arr, ddof=1)) if is_arr.size > 1 else 0.0,
        "oos_sharpe_mean": oos_mean,
        "oos_sharpe_std": float(np.std(oos_arr, ddof=1)) if oos_arr.size > 1 else 0.0,
        "oos_degradation": degradation,
        "n_positive_oos": int((oos_arr > 0).sum()),
    }
    return WalkForwardResult(
        n_folds=len(is_sharpes),
        in_sample_sharpe_mean=is_mean,
        in_sample_sharpe_std=float(np.std(is_arr, ddof=1)) if is_arr.size > 1 else 0.0,
        out_of_sample_sharpe_mean=oos_mean,
        out_of_sample_sharpe_std=float(np.std(oos_arr, ddof=1)) if oos_arr.size > 1 else 0.0,
        oos_degradation=degradation,
        fold_results=fold_results,
        aggregate_metrics=aggregate,
    )


# ─────────────────────────────────────────────────────────────────────
# 4. Bootstrap CI + 置换检验
# ─────────────────────────────────────────────────────────────────────


def bootstrap_sharpe_ci(returns, *, n=1000, seed=0, periods=252, alpha=0.05):
    """Block bootstrap 计算 Sharpe 95% 置信区间。"""
    r = _drop_nan(_to_arr(returns))
    if r.size < 5:
        return (0.0, 0.0, 0.0)
    rng = np.random.default_rng(seed)
    block_size = max(5, int(math.sqrt(r.size)))
    n_blocks = max(1, r.size // block_size)
    sharpes = np.empty(n)
    for i in range(n):
        start_idx = rng.integers(0, max(r.size - block_size + 1, 1), size=n_blocks)
        sample = np.concatenate([r[s: s + block_size] for s in start_idx])[: r.size]
        sharpes[i] = _safe_sharpe(sample, periods=periods)
    lo = float(np.quantile(sharpes, alpha / 2))
    mid = float(np.quantile(sharpes, 0.5))
    hi = float(np.quantile(sharpes, 1 - alpha / 2))
    return (lo, mid, hi)


def permutation_test_sharpe(returns, *, n=500, seed=0, periods=252):
    """置换检验:H0 = Sharpe = 0,返回单侧 p-value(> 0)。"""
    r = _drop_nan(_to_arr(returns))
    if r.size < 5:
        return 1.0
    rng = np.random.default_rng(seed)
    actual = _safe_sharpe(r, periods=periods)
    perm_sharpes = np.empty(n)
    for i in range(n):
        perm = r[rng.permutation(r.size)]
        perm_sharpes[i] = _safe_sharpe(perm, periods=periods)
    p = float((perm_sharpes >= actual).sum() / n)
    return p


# ─────────────────────────────────────────────────────────────────────
# 5. compare_strategies
# ─────────────────────────────────────────────────────────────────────


def compare_strategies(
    returns,
    signal_a,
    signal_b,
    *,
    name_a: str = "A",
    name_b: str = "B",
    periods: int = 252,
    cost_bps: float = 0.0,
    risk_free: float = 0.0,
    bootstrap_n: int = 1000,
    seed: int = 0,
) -> Dict[str, Any]:
    """A/B 对比:分别 compute_backtest + Sharpe 差 bootstrap。

    返回:
      {
        "a": BacktestResult.to_dict(),
        "b": BacktestResult.to_dict(),
        "sharpe_diff": float,         # b - a
        "diff_ci": (lo, mid, hi),     # 差值的 bootstrap 95% CI
        "p_value_diff": float,        # 差 > 0 的置换检验 p
      }
    """
    res_a = compute_backtest(
        returns, signal_a, name=name_a, periods=periods,
        cost_bps=cost_bps, risk_free=risk_free,
        bootstrap_n=bootstrap_n, seed=seed,
    )
    res_b = compute_backtest(
        returns, signal_b, name=name_b, periods=periods,
        cost_bps=cost_bps, risk_free=risk_free,
        bootstrap_n=bootstrap_n, seed=seed + 1,
    )
    sr_a = res_a.metrics.get("sharpe", 0.0)
    sr_b = res_b.metrics.get("sharpe", 0.0)
    # Diff CI
    rng = np.random.default_rng(seed + 2)
    a_sr = _apply_signal(returns, signal_a, cost_bps=cost_bps)
    b_sr = _apply_signal(returns, signal_b, cost_bps=cost_bps)
    r = _drop_nan(_to_arr(returns))
    n = min(a_sr.size, b_sr.size, r.size)
    a_sr = a_sr[:n]
    b_sr = b_sr[:n]
    block = max(5, int(math.sqrt(n)))
    n_blocks = max(1, n // block)
    diffs = np.empty(bootstrap_n)
    for i in range(bootstrap_n):
        starts = rng.integers(0, max(n - block + 1, 1), size=n_blocks)
        sa = np.concatenate([a_sr[s: s + block] for s in starts])[:n]
        sb = np.concatenate([b_sr[s: s + block] for s in starts])[:n]
        diffs[i] = _safe_sharpe(sb, periods=periods) - _safe_sharpe(sa, periods=periods)
    diff_ci = (
        float(np.quantile(diffs, 0.025)),
        float(np.quantile(diffs, 0.5)),
        float(np.quantile(diffs, 0.975)),
    )
    # 置换检验:差 > 0?
    actual_diff = sr_b - sr_a
    n_perm = min(bootstrap_n, 500)
    perm_diffs = np.empty(n_perm)
    for i in range(n_perm):
        # 翻转两个序列的顺序(保持绝对值分布不变)
        order = rng.permutation(n)
        pa = a_sr[order]
        pb = b_sr[order]
        perm_diffs[i] = _safe_sharpe(pb, periods=periods) - _safe_sharpe(pa, periods=periods)
    p_value = float((perm_diffs >= actual_diff).sum() / n_perm)
    return {
        "a": res_a.to_dict(),
        "b": res_b.to_dict(),
        "sharpe_diff": float(actual_diff),
        "diff_ci": diff_ci,
        "p_value_diff": p_value,
    }


# ─────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────

def _action_to_signal(action: str) -> float:
    """把 suggested_action 字符串映射到 -1/0/+1。

    BUY/BULL/LONG → +1, SELL/BEAR/SHORT → -1, HOLD/NEUTRAL/其他 → 0
    """
    a = (action or "").strip().upper()
    if a in ("BUY", "BULL", "LONG", "GO_LONG", "OPEN_LONG"):
        return 1.0
    if a in ("SELL", "BEAR", "SHORT", "GO_SHORT", "OPEN_SHORT"):
        return -1.0
    return 0.0


def _load_returns_json(path: str) -> np.ndarray:
    """从 JSON 文件加载 returns 数组。

    支持格式:
      1. 顶层 list,每个元素是数字
      2. 顶层 dict,{"returns": [...], "name": "..."}
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return np.asarray(data, dtype=np.float64)
    if isinstance(data, dict) and "returns" in data:
        return np.asarray(data["returns"], dtype=np.float64)
    raise ValueError(f"unsupported JSON format in {path}: need list or dict with 'returns'")


def _resolve_db_path(arg_path: Optional[str]) -> str:
    """优先 CLI 传入,否则 config.DB_PATH,再否则 backend/trident.db。"""
    if arg_path:
        return arg_path
    try:
        from config import DB_PATH  # type: ignore
        return DB_PATH
    except Exception:
        return os.path.join(os.path.dirname(__file__), "..", "trident.db")


def fetch_decisions_as_arrays(
    conn: sqlite3.Connection,
    *,
    lookback_days: int = 30,
    asset: Optional[str] = None,
    require_settled: bool = True,
) -> Tuple[np.ndarray, np.ndarray, List[Dict[str, Any]]]:
    """从 ai_decisions 拉取 (returns, signal, meta) 三元组。

    returns: forward_pnl/100 数组(因为表中存百分数)
    signal:  +1/-1/0 数组(suggested_action 映射)
    meta:    原始行 dict 列表(审计)
    """
    import time as _time
    from datetime import datetime, timezone
    cutoff_ts = _time.time() - lookback_days * 86400
    cutoff_str = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = """
        SELECT id, target_asset, market_category, suggested_action,
               forward_pnl, mfe_pct, mae_pct, is_correct, settled, created_at
        FROM ai_decisions
        WHERE created_at >= ?
    """
    params: List[Any] = [cutoff_str]
    if require_settled:
        sql += " AND settled = 1"
    if asset:
        sql += " AND target_asset = ?"
        params.append(asset)
    sql += " ORDER BY created_at ASC"
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(sql, params))

    returns: List[float] = []
    signals: List[float] = []
    meta: List[Dict[str, Any]] = []
    for r in rows:
        pnl = r["forward_pnl"]
        if pnl is None:
            returns.append(float("nan"))
        else:
            returns.append(float(pnl) / 100.0)
        signals.append(_action_to_signal(r["suggested_action"]))
        meta.append({
            "id": r["id"],
            "asset": r["target_asset"],
            "action": r["suggested_action"],
            "pnl_pct": pnl,
            "settled": bool(r["settled"]),
            "created_at": r["created_at"],
        })
    return (
        np.asarray(returns, dtype=np.float64),
        np.asarray(signals, dtype=np.float64),
        meta,
    )


def backtest_from_db(
    db_path: str,
    *,
    lookback_days: int = 30,
    asset: Optional[str] = None,
    cost_bps: float = 5.0,
    bootstrap_n: int = 1000,
    seed: int = 42,
    n_folds: int = 5,
    walk_forward: bool = True,
    compare_to_buy_hold: bool = True,
) -> Dict[str, Any]:
    """从 trident DB 拉已 settle 的 ai_decisions,跑完整 backtest_pro 流水线。

    返回 dict:
      - source / lookback_days / asset_filter
      - n_decisions / n_with_pnl
      - backtest: BacktestResult.to_dict()(ai 信号)
      - walk_forward: WF.to_dict()(可选)
      - comparison: vs buy_hold(可选)
      - meta: 原始决策列表
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        returns, signal, meta = fetch_decisions_as_arrays(
            conn, lookback_days=lookback_days, asset=asset, require_settled=True,
        )
        n_decisions = len(returns)
        n_with_pnl = int(np.isfinite(returns).sum())

        report: Dict[str, Any] = {
            "source": db_path,
            "lookback_days": lookback_days,
            "asset_filter": asset,
            "n_decisions": n_decisions,
            "n_with_pnl": n_with_pnl,
            "meta": meta,
        }

        if n_with_pnl < 20:
            report["warning"] = (
                f"n_with_pnl={n_with_pnl} < 20,跳过回测计算。"
                f"等系统多跑几天积攒 settle 数据再试。"
            )
            report["backtest"] = None
            report["walk_forward"] = None
            report["comparison"] = None
            return report

        # 1. 主回测
        res = compute_backtest(
            returns, signal, name="ai_signal",
            cost_bps=cost_bps,
            bootstrap_n=bootstrap_n, seed=seed,
        )
        report["backtest"] = res.to_dict()

        # 2. walk-forward
        if walk_forward and n_with_pnl >= 60:
            def sig_func(fr: np.ndarray, tr: np.ndarray) -> np.ndarray:
                return signal[:fr.size] if fr.size <= signal.size else np.resize(signal, fr.size)
            wf = walk_forward_backtest(
                returns, sig_func, n_folds=n_folds, cost_bps=cost_bps,
            )
            report["walk_forward"] = wf.to_dict()
        else:
            report["walk_forward"] = None

        # 3. vs buy_hold
        if compare_to_buy_hold:
            bh = np.ones_like(signal)
            cmp_result = compare_strategies(
                returns, signal, bh,
                name_a="ai_signal", name_b="buy_hold",
                cost_bps=cost_bps, bootstrap_n=bootstrap_n, seed=seed,
            )
            report["comparison"] = {
                "sharpe_diff": cmp_result["sharpe_diff"],
                "diff_ci": list(cmp_result["diff_ci"]),
                "p_value_diff": cmp_result["p_value_diff"],
            }
        else:
            report["comparison"] = None

        return report
    finally:
        conn.close()
    """从 JSON 文件加载 returns 数组。

    支持格式:
      1. 顶层 list,每个元素是数字
      2. 顶层 dict,{"returns": [...], "name": "..."}
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return np.asarray(data, dtype=np.float64)
    if isinstance(data, dict) and "returns" in data:
        return np.asarray(data["returns"], dtype=np.float64)
    raise ValueError(f"unsupported JSON format in {path}: need list or dict with 'returns'")


def _make_signal(returns: np.ndarray, method: str, *, lookback: int = 20) -> np.ndarray:
    """内置 signal 生成器。

    支持:
      - buy_hold / bh:全 1
      - momentum / mom:lookback 天均值 > 0 → 1,否则 0
      - mean_revert / rev:lookback 天均值 > 0 → -1,否则 1
      - zero / flat:全 0(空仓基准)
    """
    n = len(returns)
    if method in ("buy_hold", "bh"):
        return np.ones(n)
    if method in ("zero", "flat"):
        return np.zeros(n)
    sig = np.zeros(n)
    if method in ("momentum", "mom"):
        for i in range(lookback, n):
            sig[i] = 1.0 if returns[i - lookback: i].mean() > 0 else 0.0
        return sig
    if method in ("mean_revert", "rev"):
        for i in range(lookback, n):
            sig[i] = -1.0 if returns[i - lookback: i].mean() > 0 else 1.0
        return sig
    raise ValueError(f"unknown signal method: {method!r}")


def _make_synthetic(kind: str, n: int, seed: int) -> np.ndarray:
    """合成 returns 数据(无 --data 时使用)。"""
    rng = np.random.default_rng(seed)
    if kind == "bull_bear":
        a, b = n // 2, n - n // 2
        return np.concatenate([
            rng.normal(0.002, 0.008, a),
            rng.normal(-0.001, 0.015, b),
        ])
    if kind == "iid":
        return rng.normal(0.0005, 0.01, n)
    if kind == "trending":
        t = np.arange(n)
        return 0.0003 * t + rng.normal(0, 0.01, n)
    raise ValueError(f"unknown synthetic kind: {kind}")


def _build_wf_signal(returns: np.ndarray, method: str, lookback: int) -> Callable:
    """把内置 signal 包成 walk_forward 需要的 (full, train) -> signal 形式。"""
    def sig_func(full_r: np.ndarray, train_r: np.ndarray) -> np.ndarray:
        return _make_signal(full_r, method, lookback=lookback)
    return sig_func


def _format_md(report: Dict[str, Any]) -> str:
    """生成可读 Markdown 报告。"""
    lines: List[str] = []
    a = report["a"]
    cmp_ = report.get("comparison")
    wf = report.get("walk_forward")

    lines.append("# backtest_pro 报告\n")
    lines.append(f"- 样本数: **{a['n_obs']}**")
    lines.append(f"- 信号方法: **{report['signal_method']}**")
    if report.get("compare_to"):
        lines.append(f"- 比较对象: **{report['compare_to']}**")
    lines.append("")

    lines.append("## 1. 主回测\n")
    lines.append(f"- 名称: **{a['name']}**")
    lines.append(f"- Sharpe: **{a['metrics']['sharpe']:.3f}** "
                 f"(95%CI [{a['sharpe_ci'][0]:.2f}, {a['sharpe_ci'][2]:.2f}])")
    lines.append(f"- Sortino: {a['metrics'].get('sortino', 0):.3f}")
    lines.append(f"- Calmar: {a['metrics'].get('calmar', 0):.3f}")
    lines.append(f"- MaxDD: {a['max_drawdown']:.4f}")
    lines.append(f"- 累计: {a['cumulative_return'] * 100:.2f}%")
    lines.append(f"- 年化: {a['annualized_return'] * 100:.2f}%")
    lines.append(f"- 换手: {a['turnover'] * 100:.1f}%")
    lines.append(f"- p(vs 0): {a['p_value_vs_zero']:.4f}")
    if a.get("regime_breakdown"):
        rb = a["regime_breakdown"]
        lines.append("- Regime 拆分:")
        for r, v in rb.items():
            lines.append(f"  - {r}: Sharpe={v.get('sharpe', 0):.2f}, n={v.get('n', 0)}")
    if a.get("notes"):
        lines.append(f"- 备注: {', '.join(a['notes'])}")
    lines.append("")

    if wf is not None:
        lines.append("## 2. Walk-Forward\n")
        lines.append(f"- 折数: **{wf['n_folds']}**")
        lines.append(f"- 锚定模式: {wf.get('anchored', False)}")
        lines.append(f"- IS Sharpe mean: {wf['in_sample_sharpe_mean']:.3f} "
                     f"(std {wf['in_sample_sharpe_std']:.3f})")
        lines.append(f"- OOS Sharpe mean: **{wf['out_of_sample_sharpe_mean']:.3f}** "
                     f"(std {wf['out_of_sample_sharpe_std']:.3f})")
        lines.append(f"- OOS degradation: **{wf['oos_degradation'] * 100:+.1f}%** "
                     "(负值=过拟合,正值=稳定)")
        agg = wf.get("aggregate_metrics", {})
        lines.append(f"- 正 OOS 折: {agg.get('n_positive_oos', 0)}/{wf['n_folds']}")
        lines.append("")

    if cmp_ is not None:
        lines.append("## 3. 策略比较\n")
        lines.append(f"- Sharpe 差: **{cmp_['sharpe_diff']:.3f}** "
                     f"(95%CI [{cmp_['diff_ci'][0]:.2f}, {cmp_['diff_ci'][2]:.2f}])")
        lines.append(f"- p(差 > 0): {cmp_['p_value_diff']:.4f}")
        lines.append("")
    return "\n".join(lines)


def _build_argparser():
    import argparse
    p = argparse.ArgumentParser(
        prog="backtest_pro",
        description="回测显著性 + Walk-Forward + 策略比较 (v230p1)",
    )
    p.add_argument("--data", type=str, default=None,
                   help="JSON 文件路径,内含 returns 数组(list 或 {'returns': [...]})")
    p.add_argument("--db", type=str, default=None,
                   help="SQLite 路径(从 ai_decisions 拉取已 settle 的 forward_pnl + suggested_action)")
    p.add_argument("--db-days", type=int, default=30, help="DB 回看天数(默认 30)")
    p.add_argument("--db-asset", type=str, default=None, help="DB 单资产过滤,例如 BTCUSDT")
    p.add_argument("--synthetic", type=str, default=None,
                   choices=["bull_bear", "iid", "trending"],
                   help="无 --data 时使用合成数据")
    p.add_argument("--synthetic-n", type=int, default=500, help="合成样本数(默认 500)")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument("--signal", type=str, default="momentum",
                   choices=["momentum", "mom", "mean_revert", "rev",
                            "buy_hold", "bh", "zero", "flat"],
                   help="内置 signal 方法(默认 momentum)")
    p.add_argument("--lookback", type=int, default=20, help="momentum/mean_revert 回看期")
    p.add_argument("--compare", type=str, default="buy_hold",
                   choices=["buy_hold", "bh", "zero", "none"],
                   help="比较对象(none 表示不做比较)")
    p.add_argument("--walk-forward", dest="walk_forward", action="store_true",
                   help="启用 walk-forward")
    p.add_argument("--n-folds", type=int, default=5, help="walk-forward 折数")
    p.add_argument("--cost-bps", type=float, default=5.0, help="单边手续费 (bps)")
    p.add_argument("--bootstrap-n", type=int, default=1000, help="bootstrap 次数")
    p.add_argument("--format", choices=["json", "md"], default="md", help="输出格式")
    p.add_argument("--out", type=str, default=None, help="输出文件路径(默认 stdout)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    args = _build_argparser().parse_args(argv)

    # 1. 加载/合成数据
    if args.db:
        # DB 模式:直接走 backtest_from_db,生成 JSON 报告并返回
        db_path = _resolve_db_path(args.db)
        print(f"[backtest_pro] DB source: {db_path}, lookback={args.db_days}d",
              file=sys.stderr)
        try:
            db_report = backtest_from_db(
                db_path,
                lookback_days=args.db_days,
                asset=args.db_asset,
                cost_bps=args.cost_bps,
                bootstrap_n=args.bootstrap_n,
                seed=args.seed,
                n_folds=args.n_folds,
                walk_forward=args.walk_forward,
                compare_to_buy_hold=(args.compare != "none"),
            )
        except FileNotFoundError as e:
            print(f"[backtest_pro] ERROR: {e}", file=sys.stderr)
            return 1
        # 直接输出 DB 报告
        out_text = json.dumps(db_report, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out_text)
                if not out_text.endswith("\n"):
                    f.write("\n")
            print(f"[backtest_pro] wrote {args.out} ({len(out_text)} bytes)",
                  file=sys.stderr)
        else:
            sys.stdout.write(out_text)
            if not out_text.endswith("\n"):
                sys.stdout.write("\n")
        return 0

    if args.data:
        if not os.path.exists(args.data):
            print(f"[backtest_pro] ERROR: data file not found: {args.data}",
                  file=sys.stderr)
            return 1
        returns = _load_returns_json(args.data)
        source = args.data
    elif args.synthetic:
        returns = _make_synthetic(args.synthetic, args.synthetic_n, args.seed)
        source = f"synthetic:{args.synthetic} n={args.synthetic_n} seed={args.seed}"
    else:
        # 默认:bull_bear 合成
        returns = _make_synthetic("bull_bear", args.synthetic_n, args.seed)
        source = f"synthetic:bull_bear n={args.synthetic_n} seed={args.seed}"

    print(f"[backtest_pro] data source: {source}, n={len(returns)}", file=sys.stderr)

    # 2. 构造 signal + 主回测
    signal = _make_signal(returns, args.signal, lookback=args.lookback)
    res = compute_backtest(
        returns, signal, name=f"signal:{args.signal}",
        cost_bps=args.cost_bps,
        bootstrap_n=args.bootstrap_n,
        seed=args.seed,
    )

    # 3. 比较
    cmp_report: Optional[Dict[str, Any]] = None
    b_label: Optional[str] = None
    if args.compare != "none":
        b_method = args.compare
        b_signal = _make_signal(returns, b_method, lookback=args.lookback)
        cmp_result = compare_strategies(
            returns, signal, b_signal,
            name_a=f"signal:{args.signal}", name_b=f"signal:{b_method}",
            cost_bps=args.cost_bps,
            bootstrap_n=args.bootstrap_n, seed=args.seed,
        )
        cmp_report = {
            "sharpe_diff": cmp_result["sharpe_diff"],
            "diff_ci": list(cmp_result["diff_ci"]),
            "p_value_diff": cmp_result["p_value_diff"],
        }
        b_label = f"signal:{b_method}"

    # 4. walk-forward
    wf_report: Optional[Dict[str, Any]] = None
    if args.walk_forward:
        wf = walk_forward_backtest(
            returns, _build_wf_signal(returns, args.signal, args.lookback),
            n_folds=args.n_folds, cost_bps=args.cost_bps,
        )
        wf_report = wf.to_dict()

    # 5. 组装报告
    report = {
        "data_source": source,
        "n_obs": int(len(returns)),
        "signal_method": args.signal,
        "compare_to": b_label,
        "a": res.to_dict(),
        "comparison": cmp_report,
        "walk_forward": wf_report,
    }

    if args.format == "json":
        out_text = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        out_text = _format_md(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
            if not out_text.endswith("\n"):
                f.write("\n")
        print(f"[backtest_pro] wrote {args.out} ({len(out_text)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(out_text)
        if not out_text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
