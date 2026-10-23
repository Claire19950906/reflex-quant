#!/usr/bin/env python3
"""回测显著性分析 (Backtest Significance Statistics)。

读取已 settle 的 ai_decisions + signal_performance,计算胜率 / Bootstrap CI /
因子 IC / MFE-MAE 分布 / 最大回撤 / 夏普比率,输出 JSON 或 Markdown 报告。

设计原则:
- 只读,绝不修改源表(避免污染决策流)。
- 离线跑(CLI),不挂入 engine,跑完即退出。
- Bootstrap 用 numpy 加速,无 numpy 时退化纯 Python 抽样。
- 输出报告可写文件,也可 stdout 直接看。

CLI 用法:
    python3 -m backtest_stats --days 30 --format md --out report.md
    python3 -m backtest_stats --days 7 --format json
    python3 -m backtest_stats --asset BTCUSDT --days 30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# 容错:numpy 可选(加速 Bootstrap)
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None
    _HAS_NUMPY = False


# ── 数据拉取 ─────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_decisions(conn: sqlite3.Connection, *,
                    lookback_days: int = 30,
                    asset: Optional[str] = None) -> List[sqlite3.Row]:
    """拉取最近 N 天已 settle 的 ai_decisions。可按 asset 过滤。"""
    cutoff_ts = time.time() - lookback_days * 86400
    sql = """
        SELECT id, target_asset, market_category, suggested_action,
               sentiment_score, entry_price, exit_price,
               forward_pnl, mfe_pct, mae_pct,
               is_correct, settled, created_at,
               prediction_type, event_strength, reasoning
        FROM ai_decisions
        WHERE settled = 1
          AND created_at >= ?
    """
    params: List[Any] = [datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")]
    if asset:
        sql += " AND target_asset = ?"
        params.append(asset)
    sql += " ORDER BY created_at ASC"
    return list(conn.execute(sql, params))


def fetch_signal_performance(conn: sqlite3.Connection, *,
                              lookback_days: int = 30) -> List[sqlite3.Row]:
    """拉取最近 N 天的 signal_performance(逐 K 线回放记录)。"""
    cutoff_ts = time.time() - lookback_days * 86400
    sql = """
        SELECT decision_id, asset, ts, action, is_correct,
               forward_pnl, mfe_pct, mae_pct
        FROM signal_performance
        WHERE ts >= ?
        ORDER BY ts ASC
    """
    return list(conn.execute(sql, (cutoff_ts,)))


# ── 显著性计算 ───────────────────────────────────────────────────────────

def _is_win(is_correct: str) -> Optional[bool]:
    """把 is_correct 字段映射到 True/False/None。空字符串/未知视为 None(中性,不计入胜率)。"""
    s = (is_correct or "").strip().upper()
    if s in ("TRUE", "YES", "1", "WIN", "T", "CORRECT"):
        return True
    if s in ("FALSE", "NO", "0", "LOSS", "F", "WRONG"):
        return False
    return None


def _bootstrap_ci(samples: List[bool], *, n_bootstrap: int = 10000,
                  seed: int = 42) -> Tuple[float, float, float]:
    """对二元胜率做 bootstrap 95% 置信区间。

    返回: (点估计胜率, 下界 CI, 上界 CI)。
    """
    if not samples:
        return 0.0, 0.0, 0.0
    p_hat = sum(samples) / len(samples)
    if _HAS_NUMPY:
        rng = np.random.default_rng(seed)
        arr = np.asarray(samples, dtype=np.float64)
        n = len(arr)
        idx = rng.integers(0, n, size=(n_bootstrap, n))
        means = arr[idx].mean(axis=1)
        lo = float(np.quantile(means, 0.025))
        hi = float(np.quantile(means, 0.975))
        return p_hat, lo, hi
    import random as _random
    _random.seed(seed)
    n = len(samples)
    means = []
    for _ in range(n_bootstrap):
        boot = [_random.choice(samples) for _ in range(n)]
        means.append(sum(boot) / n)
    means.sort()
    lo = means[int(0.025 * n_bootstrap)]
    hi = means[int(0.975 * n_bootstrap)]
    return p_hat, lo, hi


def _rank_average_np(arr: "np.ndarray") -> "np.ndarray":
    """numpy 版平均秩(处理 ties,与纯 Python _rank 等价)。

    返回 1-based 平均秩,与纯 Python 实现对 ties 的处理完全一致:
    - 稳定排序(mergesort)保留相等元素的相对顺序
    - 每个 tie group 的秩 = (起始位置 + 结束位置) / 2 + 1(1-based)
    """
    n = arr.size
    if n == 0:
        return arr.copy()
    order = np.argsort(arr, kind="mergesort")
    sorted_arr = arr[order]
    # 找相等元素的边界
    change = np.ones(n, dtype=bool)
    change[1:] = sorted_arr[1:] != sorted_arr[:-1]
    group_id = np.cumsum(change)  # 1-based tie group id
    counts = np.bincount(group_id)[1:]  # 跳过 bincount 在 index 0 的 dummy
    # 每个 tie group 在 sorted 数组中的 0-based 起始位置
    ends_1based = np.cumsum(counts)
    starts_0 = ends_1based - counts
    # avg rank = start + (count+1)/2,与纯 Python 公式 (i+j)/2 + 1 等价
    avg_ranks_per_group = starts_0 + (counts + 1) / 2.0
    # 反向:rank 在原数组位置上
    ranks_sorted = avg_ranks_per_group[group_id - 1]
    result = np.empty(n, dtype=np.float64)
    result[order] = ranks_sorted
    return result


def _spearman(x: List[float], y: List[float]) -> Optional[float]:
    """Spearman 等级相关系数。失败/样本不足返回 None。

    numpy 可用时用 np.argsort + tie-group 向量化(快 50-100x);
    否则回落到纯 Python(向后兼容,无依赖)。
    """
    n = len(x)
    if n < 3 or n != len(y):
        return None
    if _HAS_NUMPY:
        xa = np.asarray(x, dtype=np.float64)
        ya = np.asarray(y, dtype=np.float64)
        rx = _rank_average_np(xa)
        ry = _rank_average_np(ya)
        # 中心化后的 Pearson = (rx-mx) · (ry-my) / (|rx-mx| |ry-my|)
        rx_c = rx - rx.mean()
        ry_c = ry - ry.mean()
        dx = np.sqrt((rx_c * rx_c).sum())
        dy = np.sqrt((ry_c * ry_c).sum())
        if dx == 0 or dy == 0:
            return None
        return float((rx_c * ry_c).sum() / (dx * dy))
    # Fallback: 纯 Python
    def rank(vals: List[float]) -> List[float]:
        sorted_idx = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[sorted_idx[j + 1]] == vals[sorted_idx[i]]:
                j += 1
            rank_avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[sorted_idx[k]] = rank_avg
            i = j + 1
        return r

    rx = rank(x)
    ry = rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _percentile(values: List[float], p: float) -> float:
    """分位数(线性插值)。纯 Python Timsort + 3 lookup,实测比 np.percentile 快。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _equity_curve(pnls: List[float]) -> Dict[str, float]:
    """给定 per-trade pnl 序列,算 max drawdown / sharpe / 总收益。

    numpy 可用时:
    - equity: cumsum 向量化
    - peak: maximum.accumulate
    - max_dd: max(peak - equity)
    - sharpe: mean/std sqrt(n)
    """
    if not pnls:
        return {"total_pnl": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "n": 0}
    if _HAS_NUMPY:
        a = np.asarray(pnls, dtype=np.float64)
        equity = np.cumsum(a)
        peak = np.maximum.accumulate(equity)
        max_dd = float(np.max(peak - equity))
        n = a.size
        mean_p = float(a.mean())
        std_p = float(a.std(ddof=1)) if n > 1 else 0.0
        # 浮点 epsilon:避免 std_p ≈ 1e-18 时 mean/std 爆炸
        sharpe = (mean_p / std_p) * math.sqrt(n) if std_p > 1e-12 else 0.0
        return {
            "total_pnl": round(float(equity[-1]), 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 4),
            "n": n,
        }
    equity: List[float] = []
    cum = 0.0
    for p in pnls:
        cum += p
        equity.append(cum)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
    n = len(pnls)
    mean_p = sum(pnls) / n
    var = sum((p - mean_p) ** 2 for p in pnls) / max(1, n - 1)
    std_p = math.sqrt(var)
    sharpe = (mean_p / std_p) * math.sqrt(n) if std_p > 0 else 0.0
    return {
        "total_pnl": round(equity[-1], 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "n": n,
    }


# ── 主分析入口 ───────────────────────────────────────────────────────────

def _extract_row_features(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """把 sqlite Row 拉平成标准 dict 列表(便于后续计算)。"""
    out: List[Dict[str, Any]] = []
    for r in rows:
        win = _is_win(r["is_correct"] if "is_correct" in r.keys() else "")
        out.append({
            "id": r["id"],
            "asset": r["target_asset"] if "target_asset" in r.keys() else r["asset"],
            "category": r["market_category"] if "market_category" in r.keys() else "n/a",
            "action": r["suggested_action"] if "suggested_action" in r.keys() else r["action"],
            "sentiment": r["sentiment_score"] if r["sentiment_score"] is not None else None,
            "forward_pnl": r["forward_pnl"] if r["forward_pnl"] is not None else None,
            "mfe_pct": r["mfe_pct"] if r["mfe_pct"] is not None else None,
            "mae_pct": r["mae_pct"] if r["mae_pct"] is not None else None,
            "win": win,
            "is_correct": (r["is_correct"] if "is_correct" in r.keys() else "") or "",
            "prediction_type": r["prediction_type"] if "prediction_type" in r.keys() else "",
            "event_strength": r["event_strength"] if "event_strength" in r.keys() else "",
            "created_at": r["created_at"] if "created_at" in r.keys() else "",
        })
    return out


def _bucket_hour(created_at: str) -> Optional[int]:
    """从 created_at 提取小时(0-23)。解析失败返回 None。"""
    if not created_at:
        return None
    try:
        s = created_at.replace("T", " ")[:19]
        return int(s.split(" ")[1].split(":")[0])
    except Exception:
        return None


def _group_by(items: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        k = it.get(key)
        out[k or "unknown"].append(it)
    return dict(out)


def analyze(rows: List[Dict[str, Any]], *, n_bootstrap: int = 5000) -> Dict[str, Any]:
    """主分析入口:输入拉平的 dict 列表,输出 dict 结构化报告。"""
    total = len(rows)
    wins = [r["win"] for r in rows if r["win"] is not None]
    win_count = sum(1 for w in wins if w)
    loss_count = sum(1 for w in wins if not w)
    unknown_count = total - len(wins)

    overall_p, overall_lo, overall_hi = _bootstrap_ci(wins, n_bootstrap=n_bootstrap)

    by_action: Dict[str, Any] = {}
    for action, items in _group_by(rows, "action").items():
        sub_wins = [r["win"] for r in items if r["win"] is not None]
        if not sub_wins:
            by_action[action] = {"n": len(items), "win_rate": None, "ci_low": 0, "ci_high": 0}
            continue
        p, lo, hi = _bootstrap_ci(sub_wins, n_bootstrap=n_bootstrap)
        by_action[action] = {
            "n": len(items),
            "win_rate": round(p, 4),
            "ci_low": round(lo, 4),
            "ci_high": round(hi, 4),
        }

    by_asset: Dict[str, Any] = {}
    for asset, items in _group_by(rows, "asset").items():
        sub_wins = [r["win"] for r in items if r["win"] is not None]
        pnls = [r["forward_pnl"] for r in items if r["forward_pnl"] is not None]
        sentiments = [(r["sentiment"], r["forward_pnl"]) for r in items
                      if r["sentiment"] is not None and r["forward_pnl"] is not None]
        if sub_wins:
            p, lo, hi = _bootstrap_ci(sub_wins, n_bootstrap=n_bootstrap)
        else:
            p, lo, hi = 0, 0, 0
        ic = _spearman([s for s, _ in sentiments], [p for _, p in sentiments]) if len(sentiments) >= 3 else None
        by_asset[asset] = {
            "n": len(items),
            "win_rate": round(p, 4) if sub_wins else None,
            "ci_low": round(lo, 4),
            "ci_high": round(hi, 4),
            "ic_sentiment_to_pnl": round(ic, 4) if ic is not None else None,
            "mean_pnl": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "median_pnl": round(_percentile(pnls, 0.5), 4) if pnls else None,
        }

    by_category: Dict[str, Any] = {}
    for cat, items in _group_by(rows, "category").items():
        sub_wins = [r["win"] for r in items if r["win"] is not None]
        if not sub_wins:
            by_category[cat] = {"n": len(items), "win_rate": None}
            continue
        p, lo, hi = _bootstrap_ci(sub_wins, n_bootstrap=n_bootstrap)
        by_category[cat] = {
            "n": len(items),
            "win_rate": round(p, 4),
            "ci_low": round(lo, 4),
            "ci_high": round(hi, 4),
        }

    by_hour: Dict[str, Any] = {}
    hourly: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        h = _bucket_hour(r["created_at"])
        if h is not None:
            hourly[h].append(r)
    for h, items in sorted(hourly.items()):
        sub_wins = [r["win"] for r in items if r["win"] is not None]
        if not sub_wins:
            continue
        p, _, _ = _bootstrap_ci(sub_wins, n_bootstrap=n_bootstrap)
        by_hour[str(h)] = {"n": len(items), "win_rate": round(p, 4)}

    mfes = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    maes = [r["mae_pct"] for r in rows if r["mae_pct"] is not None]
    mfe_dist = {
        "n": len(mfes),
        "p5": round(_percentile(mfes, 0.05), 4),
        "p25": round(_percentile(mfes, 0.25), 4),
        "median": round(_percentile(mfes, 0.5), 4),
        "p75": round(_percentile(mfes, 0.75), 4),
        "p95": round(_percentile(mfes, 0.95), 4),
    } if mfes else None
    mae_dist = {
        "n": len(maes),
        "p5": round(_percentile(maes, 0.05), 4),
        "p25": round(_percentile(maes, 0.25), 4),
        "median": round(_percentile(maes, 0.5), 4),
        "p75": round(_percentile(maes, 0.75), 4),
        "p95": round(_percentile(maes, 0.95), 4),
    } if maes else None

    pnls_all = [r["forward_pnl"] for r in rows if r["forward_pnl"] is not None]
    equity_stats = _equity_curve(pnls_all)

    significant_positive = overall_hi > 0.5 and overall_lo > 0.5
    significant_negative = overall_lo < 0.5 and overall_hi < 0.5
    significance_note = (
        "显著正期望" if significant_positive
        else "显著负期望" if significant_negative
        else "置信区间包含 0.5,无法区分随机"
    )

    return {
        "total_decisions": total,
        "win": win_count,
        "loss": loss_count,
        "unknown": unknown_count,
        "overall_win_rate": round(overall_p, 4),
        "overall_ci": [round(overall_lo, 4), round(overall_hi, 4)],
        "significance": significance_note,
        "by_action": by_action,
        "by_asset": by_asset,
        "by_category": by_category,
        "by_hour": by_hour,
        "mfe_distribution": mfe_dist,
        "mae_distribution": mae_dist,
        "equity_curve": equity_stats,
    }


# ── 报告格式化 ───────────────────────────────────────────────────────────

def format_markdown(stats: Dict[str, Any], *, lookback_days: int,
                    asset_filter: Optional[str] = None) -> str:
    """把分析结果渲染成可读 Markdown。"""
    lines: List[str] = []
    title = f"回测显著性报告 ({lookback_days}d)"
    if asset_filter:
        title += f" — asset={asset_filter}"
    lines.append(f"# {title}\n")
    lines.append(f"_生成时间: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_\n")

    lines.append("## 1. 样本概览\n")
    lines.append(f"- 决策总数: **{stats['total_decisions']}**")
    lines.append(f"- 已结算 WIN: **{stats['win']}**")
    lines.append(f"- 已结算 LOSS: **{stats['loss']}**")
    lines.append(f"- 未结算 / 未知: {stats['unknown']}")
    lines.append(f"- 总胜率: **{stats['overall_win_rate']:.2%}**")
    lines.append(f"- 95% 置信区间: [{stats['overall_ci'][0]:.2%}, {stats['overall_ci'][1]:.2%}]")
    lines.append(f"- **显著性判定: {stats['significance']}**")
    lines.append("")

    lines.append("## 2. 按 Action 分组\n")
    if stats["by_action"]:
        lines.append("| Action | n | Win Rate | 95% CI |")
        lines.append("|--------|---|----------|--------|")
        for act, v in sorted(stats["by_action"].items(), key=lambda x: -x[1]["n"]):
            if v["win_rate"] is None:
                lines.append(f"| {act} | {v['n']} | n/a | n/a |")
            else:
                lines.append(f"| {act} | {v['n']} | {v['win_rate']:.2%} | [{v['ci_low']:.2%}, {v['ci_high']:.2%}] |")
    else:
        lines.append("_无数据_")
    lines.append("")

    lines.append("## 3. 按 Asset 分组\n")
    if stats["by_asset"]:
        lines.append("| Asset | n | Win Rate | 95% CI | IC(sentiment→pnl) | Mean PnL |")
        lines.append("|-------|---|----------|--------|-------------------|----------|")
        for a, v in sorted(stats["by_asset"].items(), key=lambda x: -x[1]["n"]):
            wr = f"{v['win_rate']:.2%}" if v["win_rate"] is not None else "n/a"
            ci = f"[{v['ci_low']:.2%}, {v['ci_high']:.2%}]" if v["win_rate"] is not None else "n/a"
            ic = f"{v['ic_sentiment_to_pnl']:.3f}" if v["ic_sentiment_to_pnl"] is not None else "n/a"
            mp = f"{v['mean_pnl']:.4f}" if v["mean_pnl"] is not None else "n/a"
            lines.append(f"| {a} | {v['n']} | {wr} | {ci} | {ic} | {mp} |")
    else:
        lines.append("_无数据_")
    lines.append("")

    lines.append("## 4. 按市场分类分组\n")
    if stats["by_category"]:
        lines.append("| Category | n | Win Rate | 95% CI |")
        lines.append("|----------|---|----------|--------|")
        for c, v in sorted(stats["by_category"].items(), key=lambda x: -x[1]["n"]):
            if v.get("win_rate") is None:
                lines.append(f"| {c} | {v['n']} | n/a | n/a |")
            else:
                lines.append(f"| {c} | {v['n']} | {v['win_rate']:.2%} | [{v['ci_low']:.2%}, {v['ci_high']:.2%}] |")
    else:
        lines.append("_无数据_")
    lines.append("")

    if stats["by_hour"]:
        lines.append("## 5. 按小时段分组\n")
        lines.append("| Hour | n | Win Rate |")
        lines.append("|------|---|----------|")
        for h, v in sorted(stats["by_hour"].items(), key=lambda x: int(x[0])):
            lines.append(f"| {h}:00 | {v['n']} | {v['win_rate']:.2%} |")
        lines.append("")

    lines.append("## 6. MFE / MAE 分布\n")
    mfe = stats["mfe_distribution"]
    mae = stats["mae_distribution"]
    lines.append("| 指标 | n | p5 | p25 | median | p75 | p95 |")
    lines.append("|------|---|----|-----|--------|-----|-----|")
    if mfe:
        lines.append(f"| MFE% | {mfe['n']} | {mfe['p5']:.2f}% | {mfe['p25']:.2f}% | {mfe['median']:.2f}% | {mfe['p75']:.2f}% | {mfe['p95']:.2f}% |")
    else:
        lines.append("| MFE% | 0 | n/a | n/a | n/a | n/a | n/a |")
    if mae:
        lines.append(f"| MAE% | {mae['n']} | {mae['p5']:.2f}% | {mae['p25']:.2f}% | {mae['median']:.2f}% | {mae['p75']:.2f}% | {mae['p95']:.2f}% |")
    else:
        lines.append("| MAE% | 0 | n/a | n/a | n/a | n/a | n/a |")
    lines.append("")

    eq = stats["equity_curve"]
    lines.append("## 7. 模拟权益曲线(按 settle 顺序累加 pnl%)\n")
    lines.append(f"- 累计 PnL: **{eq['total_pnl']:.2f}%**")
    lines.append(f"- 最大回撤: **{eq['max_drawdown']:.2f}%**")
    lines.append(f"- Sharpe(per-trade, 简化): **{eq['sharpe']:.3f}**")
    lines.append(f"- 样本数: {eq['n']}")
    lines.append("")

    lines.append("---")
    lines.append("_本报告为离线统计,所有计算均基于已 settle 的 ai_decisions / signal_performance,"
                 "仅用于系统自评,不构成投资建议。_")
    return "\n".join(lines)


def format_json(stats: Dict[str, Any]) -> str:
    return json.dumps(stats, ensure_ascii=False, indent=2)


# ── CLI 入口 ─────────────────────────────────────────────────────────────

def _resolve_db_path(arg_path: Optional[str]) -> str:
    """优先用 CLI 传入,否则从 config 取 DB_PATH,再否则默认 backend 目录。"""
    if arg_path:
        return arg_path
    try:
        from config import DB_PATH  # type: ignore
        return DB_PATH
    except Exception:
        # 回退:假设 script 从 src_python/ 运行
        return os.path.join(os.path.dirname(__file__), "..", "data", "trident.db")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_stats",
        description="回测显著性分析 — 计算胜率 / Bootstrap CI / 因子 IC / MFE-MAE 分布",
    )
    p.add_argument("--days", type=int, default=30, help="回看天数(默认 30)")
    p.add_argument("--asset", type=str, default=None, help="过滤单一资产,例如 BTCUSDT")
    p.add_argument("--n-bootstrap", type=int, default=5000, help="bootstrap 抽样次数")
    p.add_argument("--format", choices=["md", "json"], default="md", help="输出格式")
    p.add_argument("--out", type=str, default=None, help="输出文件路径(默认 stdout)")
    p.add_argument("--db", type=str, default=None, help="SQLite 路径(默认读 config.DB_PATH)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    db_path = _resolve_db_path(args.db)
    if not os.path.exists(db_path):
        print(f"[backtest_stats] ERROR: db not found: {db_path}", file=sys.stderr)
        return 1
    conn = _connect(db_path)
    try:
        rows = fetch_decisions(conn, lookback_days=args.days, asset=args.asset)
        feats = _extract_row_features(rows)
        stats = analyze(feats, n_bootstrap=args.n_bootstrap)
        if args.format == "json":
            out_text = format_json(stats)
        else:
            out_text = format_markdown(stats, lookback_days=args.days, asset_filter=args.asset)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out_text)
                if not out_text.endswith("\n"):
                    f.write("\n")
            print(f"[backtest_stats] wrote {args.out} ({len(out_text)} bytes, n={stats['total_decisions']})")
        else:
            sys.stdout.write(out_text)
            if not out_text.endswith("\n"):
                sys.stdout.write("\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())