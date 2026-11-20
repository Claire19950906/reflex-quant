# -*- coding: utf-8 -*-
"""signal_weight_diagnostics.py — 权重判别力多层诊断 (v324)

背景
----
验收 #2「准确评估信号权重」用的是**单一全样本 Spearman**:
    ρ(权重, |超额涨跌|) ≥ 0.20
实测 2026-11-10: 全样本 ρ=+0.199, 恰好卡在阈值下方 → 判 FAIL。

但单一全样本 ρ 把三件不同的事混在一起:
    1. **tier 之间**的排序   —— strong 是否真的比 medium 强
    2. **tier 内部**的连续值 —— 权重微调是否真携带信息
    3. **市场状态**的差异   —— 平静期准 / 剧烈期失效

v324 实测拆开看 (n=4355 事前权重配对):
    全样本           ρ=+0.1974
    低波时段         ρ=+0.3920   ← 权重在平静市场很好用
    高波时段         ρ=+0.1715
    中波时段         ρ=+0.1596
    medium tier 内   ρ=-0.0518   ← tier 内的连续值基本是噪声
    strong tier 内   ρ=+0.1666
    按月             10月 +0.3410 → 11月 -0.1383 (反向)

即: 「差一点到阈值」是**口径造成的**, 不是权重忽然变差。
本模块把这些口径固化成可复跑的诊断, 并加一项**乘子坍缩自检**:

v324 实测发现 base 乘子 **88.6% 恒等于 0.55** —— 因为 sentiment_score 95.9% 是 0、
impact_score 94.7% 是 5、heat_score 94.7% 是 5, 代入 _base_mult 的凸组合恒得
strength=0.25 → base=0.55。base 维一旦坍缩, 权重排序就只剩 impact 维说话,
判别力上限被锁死在 impact 维上 (实测 impact 是唯一有区分度的乘子: 168 个不同取值,
其余 base 139 / consensus 4 / regime 2 / source 25 / reaction 1 / time 1)。
坍缩检测能让这类问题**在权重看起来"正常"时就被发现**, 而不是等验收变红。

铁律
----
· **只读** —— SQLite 一律 `file:...?mode=ro` 打开, 不写任何表。
· **纯函数优先** —— 统计与判定逻辑不依赖 DB, 可离线单测。
· 不改阈值 —— 本模块只报告, 判据阈值属于产品决策。

用法
----
    cd backend/src_python
    python3 signal_weight_diagnostics.py            # 人读报告
    python3 signal_weight_diagnostics.py --json     # 机读
    python3 signal_weight_diagnostics.py --min-n 200
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BACKEND_DIR = BACKEND_DIR
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 判据阈值 —— 与 capability_acceptance.THRESHOLDS["weight_impact_spearman_min"] 对齐
DEFAULT_TARGET_RHO = 0.20
DEFAULT_MIN_N = 100

# 乘子列 → 中文标签 (signal_weight_history 的列名)
MULTIPLIER_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("base_mult", "base(信号强度)"),
    ("consensus_mult", "consensus(多模型共识)"),
    ("source_mult", "source(信源历史)"),
    ("reaction_mult", "reaction(市场反应)"),
    ("regime_mult", "regime(市场状态)"),
    ("time_mult", "time(时效衰减)"),
    ("impact_mult", "impact(预期影响力)"),
)

# 乘子坍缩判定: 众数占比超过此值 → 该维度已退化成常数, 不再携带信息
COLLAPSE_RATIO = 0.80
# 众数占比再高到这个程度 → 彻底没有区分度
DEAD_RATIO = 0.95



# ══════════════════════════════════════════════════════════════════════════
# 纯函数区 (不依赖 DB, 可离线单测)
# ══════════════════════════════════════════════════════════════════════════
def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Spearman 秩相关 (并列取平均秩)。样本 < 3 或任一侧为常数 → None。

    与 capability_acceptance._spearman 口径一致 (并列值取平均秩)。
    """
    n = len(xs)
    if n != len(ys) or n < 3:
        return None

    def _rank(values: Sequence[float]) -> List[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not dx or not dy:
        return None
    return num / (dx * dy)


def tier_label(rho: Optional[float], target: float = DEFAULT_TARGET_RHO) -> str:
    """把 ρ 映射成四档状态 (与记分卡口径一致: 永远不判 PASS/FAIL)。"""
    if rho is None:
        return "不可判"
    if rho >= target:
        return "达标"
    # 1e-9 容差: target*0.75 在浮点下可能比 0.15 略大 (0.2*0.75=0.15000000000000002),
    # 会把"恰好 75%" 的样本错判成"未达线"。
    if rho >= target * 0.75 - 1e-9:
        return "接近达标"
    if rho > 0:
        return "未达线"
    return "反向"


def detect_multiplier_collapse(
    values: Sequence[Optional[float]],
    *,
    collapse_ratio: float = COLLAPSE_RATIO,
    dead_ratio: float = DEAD_RATIO,
) -> Dict[str, Any]:
    """检测一个乘子维度是否已坍缩成常数 (不再携带信息)。"""
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "distinct": 0, "mode": None, "mode_ratio": 0.0,
                "ratio": 0.0, "status": "无数据", "detail": "该维度没有样本"}
    cnt = Counter(round(v, 6) for v in vals)
    mode, mode_n = cnt.most_common(1)[0]
    ratio = mode_n / n
    distinct = len(cnt)
    if ratio >= dead_ratio:
        status = "完全退化"
        detail = "%.1f%% 的样本取同一个值 (%.4g) —— 该维度已完全不携带信息" % (ratio * 100, mode)
    elif ratio >= collapse_ratio:
        status = "已坍缩"
        detail = "%.1f%% 的样本取同一个值 (%.4g), 只有 %d 个不同取值 —— 区分度已被吃掉" % (
            ratio * 100, mode, distinct)
    else:
        status = "有区分度"
        detail = "%d 个不同取值, 众数占比 %.1f%%" % (distinct, ratio * 100)
    return {"n": n, "distinct": distinct, "mode": mode, "mode_ratio": round(ratio, 4),
            "ratio": round(ratio, 4), "status": status, "detail": detail}


def slice_rho(rows: Sequence[Tuple[float, float]], target: float = DEFAULT_TARGET_RHO) -> Dict[str, Any]:
    """对一组 (权重, |超额涨跌|) 算 ρ + 状态 + 样本量。"""
    n = len(rows)
    if n == 0:
        return {"n": 0, "rho": None, "status": "不可判"}
    rho = spearman([w for w, _ in rows], [a for _, a in rows])
    return {"n": n, "rho": round(rho, 4) if rho is not None else None,
            "status": tier_label(rho, target), "target": target}


def by_month(rows: Sequence[Tuple[float, float, int]]) -> Dict[str, Dict[str, Any]]:
    """按月切分 (rows = (权重, |超额|, ts)) —— 看判别力是在增强还是在衰减。"""
    buckets: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for w, a, ts in rows:
        buckets[time.strftime("%Y-%m", time.localtime(ts))].append((w, a))
    return {m: slice_rho(buckets[m]) for m in sorted(buckets)}


def by_regime(rows: Sequence[Tuple[float, float, str]]) -> Dict[str, Dict[str, Any]]:
    """按市场状态切分 (rows = (权重, |超额|, regime))。"""
    buckets: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for w, a, reg in rows:
        buckets[reg].append((w, a))
    return {reg: slice_rho(buckets[reg]) for reg in sorted(buckets)}


def by_tier(rows: Sequence[Tuple[str, float, float]]) -> Dict[str, Dict[str, Any]]:
    """按 tier 切分 (rows = (tier, 权重, |超额|))。

    tier 内部 ρ 是**最严格**的检验: 同一 tier 内权重只差一点点, 如果连这里
    都没有正相关, 说明"连续权重"本身没携带信息, 判别力全部来自 tier 之间的排序。
    """
    buckets: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for t, w, a in rows:
        buckets[t].append((w, a))
    return {t: slice_rho(buckets[t]) for t in sorted(buckets)}

# 乘子坍缩判定: 众数占比超过此值 → 该维度已退化成常数, 不再携带信息
COLLAPSE_RATIO = 0.80
# 众数占比再高到这个程度 → 彻底没有区分度
DEAD_RATIO = 0.95



# ══════════════════════════════════════════════════════════════════════════
# DB 读取层 (只读)
# ══════════════════════════════════════════════════════════════════════════
def _ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """只读连接 —— 铁律: 不动生产 DB。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def market_beta_benchmark(conn: sqlite3.Connection) -> Dict[int, float]:
    """同小时横截面的 forward_move_pct 中位数 = 市场基准 (剥离 beta)。

    与 capability_acceptance._market_beta_benchmark 同口径:
    中位数而非均值 —— 抗极值 (一个暴涨币种不该拉动整个基准)。
    """
    buckets: Dict[int, List[float]] = defaultdict(list)
    try:
        rows = conn.execute(
            "SELECT (ts / 3600) AS ts_bucket, forward_move_pct AS fmv FROM ai_decisions "
            "WHERE forward_move_pct IS NOT NULL AND ts IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    for r in rows:
        try:
            buckets[int(r[0])].append(float(r[1]))
        except (TypeError, ValueError):
            continue
    out: Dict[int, float] = {}
    for k, vals in buckets.items():
        vals.sort()
        mid = len(vals) // 2
        out[k] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return out


def _vol_regime(value: float, lo_thr: float, hi_thr: float) -> str:
    """按市场波动分档 (阈值由调用方给, 纯函数不碰 I/O)。"""
    if value <= lo_thr:
        return "低波"
    if value >= hi_thr:
        return "高波"
    return "中波"


def collect_ex_ante_samples(conn: sqlite3.Connection) -> Dict[str, Any]:
    """取「决策时点权重 × 可判定真值」的配对样本 (验收 #2 的口径)。

    口径与 acceptance.check_02 一致:
        signal_weight_history.trigger_kind='insert'   (决策时点, 非事后回填)
        AND decision_space='ai'                        (join 口径)
        AND ai_decisions.forward_move_pct IS NOT NULL  (可判定真值)

    返回:
        {"samples": [ {tier, weight, abs_excess, excess, ts, regime, ...}, ... ],
         "multipliers": {列名: [取值...]}}
    """
    bench = market_beta_benchmark(conn)
    samples: List[Dict[str, Any]] = []
    mult: Dict[str, List[Optional[float]]] = {c: [] for c, _ in MULTIPLIER_COLUMNS}
    try:
        rows = conn.execute(
            "SELECT h.weight_tier tier, h.signal_weight w, a.forward_move_pct f, a.ts ts, "
            "       h.base_mult, h.consensus_mult, h.source_mult, h.reaction_mult, "
            "       h.regime_mult, h.time_mult, h.impact_mult "
            "FROM signal_weight_history h "
            "JOIN ai_decisions a ON a.id = h.ai_decision_id "
            "WHERE h.trigger_kind='insert' AND h.decision_space='ai' "
            "AND a.forward_move_pct IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"samples": [], "multipliers": mult, "error": str(exc)[:200]}

    # 每个小时桶的绝对波动中位数 → 市场状态分档 (三等分)
    abs_by_bucket: Dict[int, List[float]] = defaultdict(list)
    for r in rows:
        try:
            abs_by_bucket[int(r["ts"]) // 3600].append(abs(float(r["f"])))
        except (TypeError, ValueError):
            continue
    abs_med: Dict[int, float] = {}
    for b, vals in abs_by_bucket.items():
        vals.sort()
        abs_med[b] = vals[len(vals) // 2]
    meds = sorted(abs_med.values())
    lo_thr = meds[len(meds) // 3] if meds else 0.0
    hi_thr = meds[2 * len(meds) // 3] if meds else 0.0

    for r in rows:
        try:
            w = float(r["w"])
            f = float(r["f"])
            ts = int(r["ts"])
        except (TypeError, ValueError):
            continue
        excess = f - bench.get(ts // 3600, 0.0)
        reg = _vol_regime(abs_med.get(ts // 3600, 0.0), lo_thr, hi_thr)
        sample = {
            "tier": str(r["tier"] or ""),
            "weight": w,
            "abs_excess": abs(excess),
            "excess": excess,
            "ts": ts,
            "regime": reg,
        }
        samples.append(sample)
        for col, _label in MULTIPLIER_COLUMNS:
            try:
                mult[col].append(float(r[col]) if r[col] is not None else None)
            except (TypeError, ValueError):
                mult[col].append(None)
    return {"samples": samples, "multipliers": mult}


# ══════════════════════════════════════════════════════════════════════════
# 主计算
# ══════════════════════════════════════════════════════════════════════════
def compute_diagnostics(conn: sqlite3.Connection, *,
                         target: float = DEFAULT_TARGET_RHO,
                         min_n: int = DEFAULT_MIN_N) -> Dict[str, Any]:
    """跑完整诊断, 返回可 JSON 序列化的 dict。"""
    collected = collect_ex_ante_samples(conn)
    samples = collected.get("samples") or []
    multipliers = collected.get("multipliers") or {}

    overall = slice_rho([(s["weight"], s["abs_excess"]) for s in samples], target)
    tiers = by_tier([(s["tier"], s["weight"], s["abs_excess"]) for s in samples])
    regimes = by_regime([(s["weight"], s["abs_excess"], s["regime"]) for s in samples])
    months = by_month([(s["weight"], s["abs_excess"], s["ts"]) for s in samples])

    collapse: Dict[str, Any] = {}
    for col, label in MULTIPLIER_COLUMNS:
        info = detect_multiplier_collapse(multipliers.get(col) or [])
        info["label"] = label
        collapse[col] = info

    tier_names = sorted(tiers.keys())
    inside = [tiers[t].get("rho") for t in tier_names if tiers[t].get("rho") is not None]
    best_inside = max(inside) if inside else None
    tier_spread = round(max(inside) - min(inside), 4) if len(inside) >= 2 else None
    dead = [c for c, v in collapse.items() if v.get("status") in ("已坍缩", "完全退化")]

    findings: List[str] = []
    if overall.get("rho") is not None and overall.get("rho", 0) < target:
        findings.append(
            "全样本 ρ=%s 未达线 %s —— 但分层后并非整体失效: %s" % (
                overall.get("rho"), target,
                ", ".join("%s ρ=%s" % (k, v.get("rho")) for k, v in regimes.items() if v.get("rho") is not None)))
    if best_inside is not None and best_inside <= 0:
        findings.append(
            "所有 tier **内部**的 ρ 都 ≤ 0 —— 连续权重本身不携带信息, "
            "判别力全部来自 tier 之间的排序 (tier 内 ρ 最高的只有 %s)" % best_inside)
    elif best_inside is not None and best_inside < target:
        findings.append(
            "tier 内部 ρ 最高仅 %s (< %s) —— 连续权重的增量信息很弱, "
            "tier 排序才是主要判别力来源" % (best_inside, target))
    if dead:
        findings.append(
            "乘子坍缩: %s 已退化成常数 —— 这些维度对权重排序没有任何贡献" % (
                ", ".join("%s(%s)" % (collapse[c].get("label"), collapse[c].get("status")) for c in dead)))
    months_sorted = [m for m in sorted(months) if months[m].get("rho") is not None]
    if len(months_sorted) >= 2:
        first, last = months_sorted[0], months_sorted[-1]
        if (months[first].get("rho") or 0) > 0 and (months[last].get("rho") or 0) < 0:
            findings.append(
                "判别力在**衰减并已反向**: %s ρ=%s → %s ρ=%s" % (
                    first, months[first].get("rho"), last, months[last].get("rho")))
        elif (months[last].get("rho") or 0) < (months[first].get("rho") or 0):
            findings.append(
                "判别力在衰减: %s ρ=%s → %s ρ=%s" % (
                    first, months[first].get("rho"), last, months[last].get("rho")))

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": str(DB_PATH),
        "target_rho": target,
        "min_n": min_n,
        "overall": overall,
        "by_tier": tiers,
        "by_regime": regimes,
        "by_month": months,
        "multiplier_collapse": collapse,
        "tier_rho_spread": tier_spread,
        "best_tier_internal_rho": best_inside,
        "dead_multipliers": dead,
        "findings": findings,
        "sample_count": len(samples),
    }


def render_report(diag: Dict[str, Any]) -> str:
    """人读报告。"""
    L: List[str] = []
    target = diag.get("target_rho", DEFAULT_TARGET_RHO)
    ov = diag.get("overall") or {}
    L.append("=" * 78)
    L.append("权重判别力诊断 (v324)  生成于 %s" % diag.get("generated_at"))
    L.append("=" * 78)
    L.append("目标 ρ ≥ %s   样本 n=%s   口径: 决策时点权重(trigger_kind=insert) × |超额涨跌|"
             % (target, diag.get("sample_count")))
    L.append("")
    L.append("【1】全样本 (验收 #2 的口径)")
    L.append("    ρ = %s   状态 = %s   n = %s" % (ov.get("rho"), ov.get("status"), ov.get("n")))
    L.append("")
    L.append("【2】按 tier 切分 (tier 内 ρ = 连续权重是否携带信息)")
    for t, v in (diag.get("by_tier") or {}).items():
        L.append("    %-8s ρ = %-8s 状态 = %-10s n = %s" % (t, v.get("rho"), v.get("status"), v.get("n")))
    L.append("")
    L.append("【3】按市场状态切分 (平静期 vs 剧烈期)")
    for r, v in (diag.get("by_regime") or {}).items():
        L.append("    %-8s ρ = %-8s 状态 = %-10s n = %s" % (r, v.get("rho"), v.get("status"), v.get("n")))
    L.append("")
    L.append("【4】按月切分 (判别力在增强还是在衰减)")
    for m, v in (diag.get("by_month") or {}).items():
        L.append("    %-8s ρ = %-8s 状态 = %-10s n = %s" % (m, v.get("rho"), v.get("status"), v.get("n")))
    L.append("")
    L.append("【5】乘子坍缩自检 (退化 = 该维度不再影响权重排序)")
    for col, v in (diag.get("multiplier_collapse") or {}).items():
        flag = "  <<<" if v.get("status") in ("已坍缩", "完全退化") else ""
        L.append("    %-22s %-8s %s%s" % (v.get("label"), v.get("status"), v.get("detail"), flag))
    L.append("")
    L.append("【6】结论")
    findings = diag.get("findings") or []
    if not findings:
        L.append("    (无异常)")
    for i, f in enumerate(findings, 1):
        L.append("    %d) %s" % (i, f))
    L.append("")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="权重判别力多层诊断 (只读)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET_RHO, help="目标 ρ")
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N, help="最小样本量")
    ap.add_argument("--db", default=str(DB_PATH), help="数据库路径 (只读打开)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    conn = _ro(Path(args.db))
    try:
        diag = compute_diagnostics(conn, target=args.target, min_n=args.min_n)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(diag, ensure_ascii=False, indent=1))
    else:
        print(render_report(diag))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
