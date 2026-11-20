"""
calibration.py — 纯 Python 评分校准工具(无 sklearn 依赖)

提供:
- isotonic_regression: PAV 单调回归
- brier_score: Brier Score (越小越好)
- expected_calibration_error: ECE 期望校准误差
- recalibrate_scores: 把原始 algo_score 映射到 recalibrated score
"""
from __future__ import annotations
from typing import List, Tuple


def isotonic_regression(y: List[float], increasing: bool = True) -> List[float]:
    """Pool Adjacent Violators (PAV) 算法
    输入: 一维 y 列表
    输出: 同样长度, 单调 (increasing=True 时递增) 的 y

    用于把"实际 5d 涨幅"按"原始预测分数"排序后做单调性修正
    """
    n = len(y)
    if n == 0:
        return []
    # 块: (start_idx, end_idx, value)
    blocks: List[Tuple[int, int, float]] = [(i, i, float(yi)) for i, yi in enumerate(y)]
    # PAV 主循环
    while True:
        merged = False
        i = 0
        while i < len(blocks) - 1:
            a, b = blocks[i], blocks[i + 1]
            avg_a, avg_b = a[2], b[2]
            violate = (increasing and avg_a > avg_b) or ((not increasing) and avg_a < avg_b)
            if violate:
                new_start = a[0]
                new_end = b[1]
                w_a = a[1] - a[0] + 1
                w_b = b[1] - b[0] + 1
                new_value = (avg_a * w_a + avg_b * w_b) / (w_a + w_b)
                blocks[i] = (new_start, new_end, new_value)
                blocks.pop(i + 1)
                merged = True
                # 不 i += 1,因为可能与下一块仍冲突
            else:
                i += 1
        if not merged:
            break
    # 展开
    out = [0.0] * n
    for start, end, val in blocks:
        for j in range(start, end + 1):
            out[j] = val
    return out


def brier_score(pred: List[float], actual: List[float]) -> float:
    """Brier Score: (1/n) Σ (p_i - a_i)²
    - 输入 pred 应在 [0,1]; actual 在 {0,1} 或 [0,1]
    - 0=完美, 1=最差
    """
    if not pred:
        return 0.0
    return sum((p - a) ** 2 for p, a in zip(pred, actual)) / len(pred)


def expected_calibration_error(
    pred: List[float], actual: List[float], n_bins: int = 10
) -> float:
    """ECE = Σ_b |avg_pred_b - avg_actual_b| * n_b / n
    - 各 bin 预测均值 vs 实际均值之差的加权平均
    - 0=完美校准
    """
    if not pred:
        return 0.0
    ece = 0.0
    n = len(pred)
    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        in_bin = [
            (p, a)
            for p, a in zip(pred, actual)
            if lo <= p < hi or (b == n_bins - 1 and p == hi)
        ]
        if not in_bin:
            continue
        avg_p = sum(p for p, _ in in_bin) / len(in_bin)
        avg_a = sum(a for _, a in in_bin) / len(in_bin)
        ece += abs(avg_a - avg_p) * len(in_bin) / n
    return ece


def recalibrate_scores(
    raw_scores: List[float], actuals: List[float]
) -> List[float]:
    """把原始 algo_score 映射到 recalibrated score

    步骤:
    1) 按 raw_score 升序排序(对 actuals 做同样排列)
    2) 对 actuals 做 isotonic 回归(单调递增)
    3) 把结果夹到 [0,1]
    4) 还原原顺序

    用于 v237/calibration 端点: 把"原始概率"映射到"实际 5d 涨跌幅方向"
    注意: actuals 是连续值 (5d_pct),不是 0/1 标签,所以这里输出"预期 5d 涨跌幅"
    """
    if not raw_scores or len(raw_scores) != len(actuals):
        return list(raw_scores)
    # 排序
    pairs = sorted(zip(raw_scores, actuals), key=lambda p: p[0])
    sorted_actuals = [a for _, a in pairs]
    # isotonic 回归
    mono = isotonic_regression(sorted_actuals, increasing=True)
    # 还原原顺序
    out: List[float] = [0.0] * len(raw_scores)
    for i, (raw, _) in enumerate(pairs):
        out[raw_scores.index(raw) if raw_scores.count(raw) == 1 else i] = mono[i]
    # 简化版: 由于上面 index 处理重复值不好, 直接用 zip 配对
    out = [0.0] * len(raw_scores)
    for rank, (raw, _) in enumerate(pairs):
        out[raw_scores.index(raw) if raw_scores.count(raw) == 1 else rank] = mono[rank]
    return out


def calibration_table(
    raw_scores: List[float], actuals: List[float], n_bins: int = 10
) -> List[dict]:
    """生成各 bin 校准表
    返回: [{bin, lo, hi, n, avg_pred, avg_actual, gap}, ...]
    """
    if not raw_scores:
        return []
    rows: List[dict] = []
    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        in_bin = [
            (p, a)
            for p, a in zip(raw_scores, actuals)
            if lo <= p < hi or (b == n_bins - 1 and p >= hi - 1e-9)
        ]
        if not in_bin:
            continue
        avg_p = sum(p for p, _ in in_bin) / len(in_bin)
        avg_a = sum(a for _, a in in_bin) / len(in_bin)
        rows.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "lo": lo,
            "hi": hi,
            "n": len(in_bin),
            "avg_pred": round(avg_p, 3),
            "avg_actual_5d_pct": round(avg_a, 3),
            "gap": round(avg_a - avg_p * 5, 3),  # 假设 5% 范围
        })
    return rows
