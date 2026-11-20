# -*- coding: utf-8 -*-
"""
causal_weight_feedback.py — 因果图谱权重闭环回写 (2026-10-22 新增 / v310 P1 重做)
=================================================================

问题
----
causal_chain_outcomes 持续结算 (已 269 条), 但 causal_chains.weight
永远停在 1.0 —— outcome 算了从不回写, 图谱"只读不写"。

v310 P1 之前为什么必然空转
-------------------------
旧版有两处硬伤, 让这个模块**永远不可能生效**:

1. **样本阈值不可达**: MIN_SAMPLES=8, 而实测每条链最多只有 2 个已结算样本
   (121 条链 n=1, 68 条 n=2, n>=3 为 0)。189 条链**全部**被判 skipped_sample,
   模块一次都没写成功过 —— 是个纯 no-op。
2. **权重语义与列的语义冲突**: causal_chains.weight 在下游被当作
   **stated confidence** 使用 (load_active_chains → confidence=weight;
   derive_strategy_columns 显式夹到 [0,1]; api_server 手工改也锁 ≤1.0),
   而旧版按"以 1.0 为中心的乘数"算 (WEIGHT_HI=2.0), 写进去的值会被下游
   clamp 回 1.0, 而且"好的链"永远无法被奖励。

现在的算法
----------
**分层贝叶斯收缩**, 目标是把 weight 修正成"这条链实际有多准":

  posterior = (credit + τ · prior) / (n + τ)      τ = PRIOR_STRENGTH 虚拟样本
  weight    = clip(posterior, 0.10, 1.00)          stated confidence 域

  · credit: HIT 计 1, HIT_WEAK 计 **0.5** (旧版算 1.5, 与文档"折半"矛盾)
  · prior 分三级回退: **(asset, expected_direction) 族** → asset 级 → 全局
    族里样本越多族先验越准; 族样本不足就往上退, 不会拿 8 个样本的均值
    当成 54 个样本的真相
  · 族命中率落在 [5%, 95%] 之外 = **退化率**, 判为方向标注/评估的系统性问题
    (不是链质量), 该族**不做族先验**并单独上报, 避免把 bug 放大进权重
  · MIN_CHAIN_SAMPLES: 链自身至少 2 个已结算样本才改它的 confidence
  · MIN_DELTA: 与当前 weight 差 < 此值不写 (防抖动, 也让第二轮自动收敛)

为什么 n=2 也敢写
----------------
旧版怕 n=1 噪声 (单条 HIT 算出 1.31 倍权重), 那是因为**没有收缩**。
有了收缩, n=1 时链自身证据最多把 confidence 拉动 |credit-prior|/(1+τ) ≈ 6~12%,
而且被 [0.1, 1.0] 夹住; n=2 要两次结果同向才够 MIN_DELTA 门槛。
证据不足 → 权重不动 (skipped_sample); 证据够了 → 按比例温和调整。

用法
----
    python3 causal_weight_feedback.py --dry-run     # 只看会改什么
    python3 causal_weight_feedback.py               # 实际回写
    python3 causal_weight_feedback.py --stats-only   # 只输出统计
"""

from __future__ import annotations
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = BACKEND_DIR
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# v406 P4: 原值 2 与链侧方向闸门（chain_prior.MIN_CHAIN_SAMPLES_FOR_DIRECTION = 12）
# 不一致 —— 2 个样本就敢改"这条链有多准"，而 12 才敢让它指挥方向。
# 同一个库里两套置信门槛，等于用 A 的把握程度去支撑 B 的结论。
MIN_CHAIN_SAMPLES = 12   # 与 chain_prior 方向闸门对齐：链自身 ≥12 个已结算样本
WILSON_Z = 1.96          # 与 chain_prior._wilson_lower 同口径
MIN_WILSON_OVER_PRIOR = 0.0   # 证据下限必须高于先验（>0 才算"真的比平均准"）
MIN_FAMILY_SAMPLES = 20  # 族 (asset+direction) 至少 20 个样本才用族先验
FAMILY_RATE_MIN = 0.05   # 族命中率低于此值 = 退化率 (系统性问题), 不做族先验
FAMILY_RATE_MAX = 0.95   # 族命中率高于此值 = 退化率, 不做族先验
PRIOR_STRENGTH = 8.0     # 收缩强度(虚拟样本数) —— n=1 时最多拉动 6~12%
MIN_DELTA = 0.02         # 与当前 weight 差 < 此值不写 (防抖动 + 第二轮自动收敛)
WEIGHT_LO, WEIGHT_HI = 0.10, 1.00   # weight = stated confidence, 下游夹到 [0,1]


def _wilson_lower(hits: float, n: int, z: float = WILSON_Z) -> float:
    """Wilson 置信下界（与 chain_prior._wilson_lower 同一公式，接受浮点 hits）。

    credit 是 0.5 起步的加权量（HIT 1.0 / HIT_WEAK 0.5），不是整数命中数，
    所以这里不能直接复用那边的 int 版本，但公式必须一致 —— 口径不一致就是新的静默漂移。
    """
    if not n:
        return 0.0
    p = max(0.0, min(1.0, float(hits) / float(n)))
    d = 1.0 + (z * z) / n
    centre = p + (z * z) / (2.0 * n)
    margin = z * ((p * (1.0 - p) / n + (z * z) / (4.0 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / d)


def _db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _chain_stats(conn: sqlite3.Connection) -> Dict[int, Dict[str, float]]:
    """按 chain_id 聚合已结算 outcome。**weak 命中折半计分** (v310 P1 修正)。

    v310 P1 修正: 旧版 credit = strong + weak + 0.5*weak = strong + **1.5***weak,
    与文档写的"weak 命中折半"矛盾, 把全局先验从 0.576 抬到 0.770,
    等于把所有弱命中当强命中记账。
    """
    # v322: 方向列可能不存在（老库 / 简化 schema / 内存测试库）→ 缺列就退回旧口径，
    # 守卫自动不生效（而不是让整个统计 SQL 报 "no such column"）。
    try:
        _cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(causal_chain_outcomes)")}
    except sqlite3.Error:
        _cols = set()
    has_dir = {"predicted_direction", "direction_hit",
               "magnitude_hit"}.issubset(_cols)

    dir_sql = ""
    if has_dir:
        dir_sql = (
            # dir_n        该链的"有方向主张"的 outcome 数
            # dir_hit      其中方向判对的数（超额收益口径）
            # dir_mag_hit  其中幅度也合适的数
            # credit 被拆成 方向部分 + 幅度部分:
            #   HIT     = dir_hit*0.5 + mag_hit*0.5
            #   HIT_WEAK= dir_hit*0.5 + mag_hit*0
            # 当族的方向判别力不如随机时, 方向部分不给分（否则拿噪声改权重）。
            "       , SUM(CASE WHEN predicted_direction IN ('UP','DOWN') "
            "                       THEN 1 ELSE 0 END) AS dir_n "
            "       , SUM(CASE WHEN predicted_direction IN ('UP','DOWN') "
            "                       AND direction_hit=1 THEN 1 ELSE 0 END) AS dir_hit "
            "       , SUM(CASE WHEN predicted_direction IN ('UP','DOWN') "
            "                       AND magnitude_hit=1 THEN 1 ELSE 0 END) AS dir_mag_hit "
        )

    rows = conn.execute(
        "SELECT CAST(chain_id AS INTEGER) AS cid, "
        "       COUNT(*) AS n, "
        "       SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) AS hit, "
        "       SUM(CASE WHEN status='HIT' THEN 1 ELSE 0 END) AS strong_hit, "
        "       SUM(CASE WHEN status='HIT_WEAK' THEN 1 ELSE 0 END) AS weak_hit, "
        "       SUM(CASE WHEN status='MISS' THEN 1 ELSE 0 END) AS miss, "
        "       SUM(CASE WHEN status='HIT' THEN 1.0 "
        "                WHEN status='HIT_WEAK' THEN 0.5 ELSE 0 END) AS credit "
        + dir_sql +
        "FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS') "
        "GROUP BY CAST(chain_id AS INTEGER)"
    ).fetchall()

    out: Dict[int, Dict[str, float]] = {}
    for r in rows:
        if r["cid"] is None:
            continue
        out[int(r["cid"])] = {
            "n": int(r["n"] or 0),
            "hit": int(r["hit"] or 0),
            "strong_hit": int(r["strong_hit"] or 0),
            "weak_hit": int(r["weak_hit"] or 0),
            "miss": int(r["miss"] or 0),
            "credit": float(r["credit"] or 0.0),
            "dir_n": int(r["dir_n"] or 0) if has_dir else 0,
            "dir_hit": int(r["dir_hit"] or 0) if has_dir else 0,
            "dir_mag_hit": int(r["dir_mag_hit"] or 0) if has_dir else 0,
        }
    return out


def _chain_meta(conn: sqlite3.Connection) -> Dict[int, Dict[str, str]]:
    """链的族属性: asset + expected_direction (决定它属于哪个族)。"""
    out: Dict[int, Dict[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(asset,'UNKNOWN') AS asset, "
            "       COALESCE(expected_direction,'BOTH') AS direction "
            "FROM causal_chains"
        ).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out[int(r["id"])] = {"asset": str(r["asset"]), "direction": str(r["direction"])}
    return out


def _group_priors(stats: Dict[int, Dict[str, float]],
                  meta: Dict[int, Dict[str, str]],
                  group_key) -> Dict[str, Dict[str, float]]:
    """按 group_key 聚合出每个分组的 (n, credit, rate)。"""
    n_by: Dict[str, float] = {}
    credit_by: Dict[str, float] = {}
    for cid, v in stats.items():
        m = meta.get(cid, {})
        key = group_key(m)
        n_by[key] = n_by.get(key, 0.0) + v["n"]
        credit_by[key] = credit_by.get(key, 0.0) + v["credit"]
    return {
        k: {"n": n_by[k], "credit": credit_by[k],
            "rate": (credit_by[k] / n_by[k]) if n_by[k] else 0.0}
        for k in n_by
    }


def compute_global_prior(stats: Dict[int, Dict[str, float]]) -> float:
    """全局命中率作为先验 (带弱命中折算), 防除零。"""
    gn = sum(v["n"] for v in stats.values())
    gh = sum(v["credit"] for v in stats.values())
    if gn <= 0:
        return 0.5
    return max(gh / gn, 0.05)


#: v322: 方向判别力下限（超额收益口径）。低于它 → 该族的方向命中不给 credit。
#: 0.5 = 随机水平。实测当前有方向链 48.3% → 触发守卫（这正是应有的诚实）。
DIRECTION_MIN_DISCRIMINATION = 0.50
#: v322: **滞回** —— 解除守卫需要的判别力（比触发门槛高 5 个点）。
#: 为什么需要：实测全局判别力 48.3%，离 50% 只有 **1.66 个百分点** ——
#: 样本多几十条就可能翻过门槛 → 守卫反复开关 → 权重来回跳（"越跑越歪"的另一种形式）。
#: 滞回区间 [0.45, 0.55] 内**保持上次状态**，不因噪声翻转。
DIRECTION_GUARD_RELEASE = 0.55
#: 滞回区间内保持"上次状态"的状态文件（跨进程/跨轮次）
_GUARD_STATE_FILE = (BACKEND_DIR
                     / "runtime" / "direction_guard_state.json")


def _load_guard_state() -> Dict[str, bool]:
    """读上次的守卫状态（{族: 是否被守卫}）。读不到就当空。"""
    try:
        with open(str(_GUARD_STATE_FILE), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read() or "{}")
        return {str(k): bool(v) for k, v in (data or {}).items()}
    except Exception:
        return {}


def _save_guard_state(state: Dict[str, bool]) -> None:
    try:
        d = _GUARD_STATE_FILE.parent
        if d and not d.exists():
            d.mkdir(parents=True, exist_ok=True)
        tmp = "%s.tmp.%d" % (_GUARD_STATE_FILE, os.getpid())
        with open(str(tmp), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(str(tmp), str(_GUARD_STATE_FILE))
    except Exception:
        pass


def _apply_direction_guard(stats: Dict[int, Dict[str, float]],
                           meta: Dict[int, Dict[str, str]]):
    """方向判别力守卫：把"方向猜对"从 credit 里剥离（当它不如随机时）。

    实测（2026-11-07，超额收益口径）：有方向链的方向命中率 **43.9%** ——
    **低于随机 50%**，即这些链的方向主张基本是噪声。
    照旧把"方向猜对"记进 credit = **拿噪声改权重** → 越跑越歪。

    credit 拆解（精确等价，不改原语义）：
        HIT     = dir_hit*0.5 + mag_hit*0.5
        HIT_WEAK= dir_hit*0.5 + mag_hit*0
    守卫触发时（族或全局判别力 ≤ DIRECTION_MIN_DISCRIMINATION）：
        credit = mag_hit*0.5          ← 只保留幅度部分（"这个事件引起了多大波动"）

    就地改写 stats[cid]["credit"]，返回 (stats, fam_rate, global_rate, guarded)。
    """
    fam_num: Dict[str, List[int]] = {}
    for cid, v in stats.items():
        if not v.get("dir_n"):
            continue
        m = meta.get(cid, {})
        fk = "%s|%s" % (m.get("asset", "UNKNOWN"), m.get("direction", "BOTH"))
        agg = fam_num.setdefault(fk, [0, 0])
        agg[0] += int(v.get("dir_hit") or 0)
        agg[1] += int(v.get("dir_n") or 0)
    fam_rate = {k: (v[0] / v[1]) for k, v in fam_num.items()
                if v[1] >= MIN_FAMILY_SAMPLES}
    tot_dh = sum(int(v.get("dir_hit") or 0) for v in stats.values())
    tot_dn = sum(int(v.get("dir_n") or 0) for v in stats.values())
    global_rate = (tot_dh / tot_dn) if tot_dn else 0.0

    guarded: List[str] = []
    prev = _load_guard_state()
    next_state: Dict[str, bool] = {}
    for cid, v in stats.items():
        m = meta.get(cid, {})
        direction = m.get("direction", "BOTH")
        if direction not in ("UP", "DOWN") or not v.get("dir_n"):
            continue
        fkey = "%s|%s" % (m.get("asset", "UNKNOWN"), direction)
        rate = fam_rate.get(fkey, global_rate)
        # ── 滞回判定 ────────────────────────────────────────────────
        #   rate < 触发门槛        → 守卫
        #   rate > 解除门槛        → 解除守卫
        #   中间地带              → **保持上次状态**（不因噪声翻转）
        if rate < DIRECTION_MIN_DISCRIMINATION:
            is_guarded = True
        elif rate > DIRECTION_GUARD_RELEASE:
            is_guarded = False
        else:
            is_guarded = bool(prev.get(fkey, True))
        next_state[fkey] = is_guarded
        if not is_guarded:
            continue
        # 守卫触发：方向部分不给分，只留幅度
        v["credit"] = int(v.get("dir_mag_hit") or 0) * 0.5
        if fkey not in guarded:
            guarded.append(fkey)
    if next_state:
        _save_guard_state(next_state)
    return stats, fam_rate, global_rate, guarded


def plan_updates(conn: sqlite3.Connection) -> Dict[str, Any]:
    """算出每条链应写入的 confidence, 但不写库。"""
    stats = _chain_stats(conn)
    meta = _chain_meta(conn)
    # v322: 方向判别力守卫（必须在算先验**之前** ——
    # 否则被污染的 credit 会把全局/族先验一起带偏）
    stats, fam_dir_rate, global_dir_rate, dir_guarded = \
        _apply_direction_guard(stats, meta)
    prior = compute_global_prior(stats)

    fam_priors = _group_priors(stats, meta, lambda m: f"{m.get('asset','UNKNOWN')}|{m.get('direction','BOTH')}")
    asset_priors = _group_priors(stats, meta, lambda m: m.get("asset", "UNKNOWN"))

    degenerate: Dict[str, Dict[str, Any]] = {}
    plans: List[Dict[str, Any]] = []

    for cid, v in sorted(stats.items()):
        n = v["n"]
        m = meta.get(cid, {})
        asset = m.get("asset", "UNKNOWN")
        direction = m.get("direction", "BOTH")
        fkey = f"{asset}|{direction}"

        # ── 先验三级回退: 族 → asset → 全局 ────────────────────────────────
        prior_rate, prior_kind = prior, "global"
        fp = fam_priors.get(fkey)
        if fp and fp["n"] >= MIN_FAMILY_SAMPLES:
            if FAMILY_RATE_MIN <= fp["rate"] <= FAMILY_RATE_MAX:
                prior_rate, prior_kind = fp["rate"], "family"
            else:
                # 退化率 (0% / 100%): 方向标注或评估的系统性问题, 不是链质量
                degenerate[fkey] = {
                    "family": fkey, "n": fp["n"], "rate": round(fp["rate"], 4),
                    "note": "族命中率退化, 不做族先验 (疑方向标注/评估问题)",
                }
        if prior_kind == "global":
            ap = asset_priors.get(asset)
            if ap and ap["n"] >= MIN_FAMILY_SAMPLES:
                if FAMILY_RATE_MIN <= ap["rate"] <= FAMILY_RATE_MAX:
                    prior_rate, prior_kind = ap["rate"], "asset"
                else:
                    # asset 级也退化 → 整条资产线的评估口径有问题, 回退全局
                    akey = f"asset:{asset}"
                    degenerate[akey] = {
                        "family": akey, "n": ap["n"], "rate": round(ap["rate"], 4),
                        "note": "asset 级命中率退化, 回退全局先验 (疑该资产评估口径问题)",
                    }

        prior_rate = max(0.05, min(1.0, prior_rate))
        raw = v["credit"] / n if n else 0.0
        shrunk = (v["credit"] + PRIOR_STRENGTH * prior_rate) / (n + PRIOR_STRENGTH)
        w = max(WEIGHT_LO, min(WEIGHT_HI, shrunk))

        cur = conn.execute(
            "SELECT weight FROM causal_chains WHERE id=?", (cid,)
        ).fetchone()
        cur_w = float(cur["weight"]) if cur and cur["weight"] is not None else 1.0

        wl = _wilson_lower(v["credit"], n)
        if n < MIN_CHAIN_SAMPLES:
            reason = "skipped_sample(n=%d<%d)" % (n, MIN_CHAIN_SAMPLES)
        elif w > cur_w and wl <= prior_rate + MIN_WILSON_OVER_PRIOR:
            # **只对"往上调"设证**：把置信度抬高必须有 95% 下界站得住，
            # 否则就是拿 12 条样本里的一次侥幸给链镀金。
            # 往下调不设这道闸 —— 证据显示它比自称的差，就该降，这是安全方向。
            reason = "skipped_unproven_upgrade(wilson_lower=%.3f<=prior=%.3f)" % (wl, prior_rate)
        elif abs(w - cur_w) < MIN_DELTA:
            # 已经站在证据给出的 confidence 上 (含首轮无变化) —— 不写, 防抖动
            reason = "skipped_noise(Δ=%.3f<%.2f)" % (abs(w - cur_w), MIN_DELTA)
        else:
            reason = "update"

        plans.append({
            "chain_id": cid, "reason": reason, "n": n,
            "hit": v["hit"], "credit": round(v["credit"], 3),
            "raw_rate": round(raw, 4), "shrunk_rate": round(shrunk, 4),
            "prior_kind": prior_kind, "prior_rate": round(prior_rate, 4),
            "family": fkey,
            "proposed_weight": round(w, 4), "current_weight": round(cur_w, 4),
            "wilson_lower": round(wl, 4),
        })

    # 门槛敏感性：让人能看到"12 会不会太严" —— 而不是只给一个 0 改动的结论
    sens = {}
    for th in (2, 5, 12, 20):
        sel = [v for v in stats.values() if v["n"] >= th]
        beat = [v for v in sel if _wilson_lower(v["credit"], v["n"]) > prior]
        sens["n>=%d" % th] = {"chains": len(sel), "wilson_beats_prior": len(beat)}

    return {
        "prior": round(prior, 4),
        "min_samples": MIN_CHAIN_SAMPLES,
        "wilson_z": WILSON_Z,
        "threshold_sensitivity": sens,
        "min_family_samples": MIN_FAMILY_SAMPLES,
        "prior_strength": PRIOR_STRENGTH,
        "chains_with_outcomes": len(stats),
        "family_priors": {k: {"n": v["n"], "rate": round(v["rate"], 4)}
                          for k, v in sorted(fam_priors.items())},
        "degenerate_families": list(degenerate.values()),
        # v322: 方向判别力（超额收益口径）—— 如实暴露，避免"命中率好看=有判别力"的错觉
        "direction_discrimination": {
            "global_rate": round(global_dir_rate, 4),
            "family_rates": {k: round(v, 4)
                             for k, v in sorted(fam_dir_rate.items())},
            "guarded_families": list(dir_guarded),
            "min_required": DIRECTION_MIN_DISCRIMINATION,
            "note": ("≤ min_required 的族，其『方向猜对』不计入 credit"
                     "（否则拿噪声改权重）"),
        },
        "plans": plans,
    }


def apply_updates(conn: sqlite3.Connection, plan: Dict[str, Any],
                  dry_run: bool = False) -> Dict[str, Any]:
    """执行回写。dry_run=True 时只报告不落库。"""
    # updated_at 是 TEXT 列 (默认 datetime('now','localtime')), 必须同格式写,
    # 否则写进去的是 epoch 字符串, 任何日期比较都会失效。
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    applied: List[Dict[str, Any]] = []
    skipped_sample = 0
    skipped_noise = 0

    for p in plan["plans"]:
        r = p["reason"]
        if r.startswith("skipped_sample"):
            skipped_sample += 1
            continue
        if r.startswith("skipped_noise"):
            skipped_noise += 1
            continue

        row = conn.execute(
            "SELECT name FROM causal_chains WHERE id=?", (p["chain_id"],)
        ).fetchone()
        applied.append({
            **p,
            "name": str(row["name"])[:70] if row else "",
        })
        if not dry_run:
            conn.execute(
                "UPDATE causal_chains SET weight=?, updated_at=? WHERE id=?",
                (p["proposed_weight"], now, p["chain_id"]),
            )

    # 派生列 (confidence 等) 必须跟着 weight 一起更新, 否则 API 读到的
    # confidence 永远是旧的 1.0 —— 派生只在建链/扩链时跑, 没有周期调度。
    # 放在 commit 之前, 保证权重与派生列同事务落库。
    derived = {"updated": 0}
    if applied and not dry_run:
        try:
            from causal_chain_evaluator import derive_strategy_columns
            derived = derive_strategy_columns(conn, chain_ids=[p["chain_id"] for p in applied])
        except Exception as e:      # 派生失败不该回滚已写入的权重
            derived = {"error": f"{type(e).__name__}: {e}"}

    if not dry_run:
        conn.commit()

    return {
        "applied": len(applied),
        "skipped_sample": skipped_sample,
        "skipped_noise": skipped_noise,
        "dry_run": dry_run,
        "derived_columns": derived,
        "details": applied,
    }


def report(conn: sqlite3.Connection, plan: Dict[str, Any],
           res: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """生成人可读摘要 (JSON)。"""
    dist = dict(conn.execute(
        "SELECT weight, COUNT(*) FROM causal_chains GROUP BY weight ORDER BY 2 DESC"
    ).fetchall())

    all_n = [p["n"] for p in plan["plans"]]
    by_prior = {}
    for p in plan["plans"]:
        if p["reason"] == "update":
            by_prior[p["prior_kind"]] = by_prior.get(p["prior_kind"], 0) + 1
    return {
        "ts": int(time.time()),
        "dry_run": dry_run,
        "global_prior_hit_rate": plan["prior"],
        "min_chain_samples_required": plan["min_samples"],
        "min_family_samples_required": plan["min_family_samples"],
        "prior_strength": plan["prior_strength"],
        "chains_with_outcomes": plan["chains_with_outcomes"],
        "sample_per_chain": {
            "max": max(all_n) if all_n else 0,
            "avg": round(sum(all_n) / len(all_n), 2) if all_n else 0,
        },
        "applied": res["applied"],
        "skipped_insufficient_sample": res["skipped_sample"],
        "skipped_noise": res["skipped_noise"],
        "updates_by_prior_kind": by_prior,
        "family_priors": plan["family_priors"],
        "degenerate_families": plan["degenerate_families"],
        "weight_distribution_after": dist,
        "updates": res["details"][:40],
    }


def run(db_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    conn = _db(db_path)
    try:
        plan = plan_updates(conn)
        res = apply_updates(conn, plan, dry_run=dry_run)
        return report(conn, plan, res, dry_run=dry_run)
    finally:
        conn.close()


def _append_audit(path: str, payload: Dict[str, Any]) -> None:
    """把本次回写追加一行 JSON 到审计文件 (自动改生产权重, 必须留痕)。"""
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass   # 审计写不了不能影响主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="因果图谱权重闭环回写")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--stats-only", action="store_true", help="只输出统计")
    ap.add_argument("--db", default=None)
    ap.add_argument("--audit-log", default="", help="追加一行 JSON 审计到该文件")
    args = ap.parse_args()

    if args.stats_only:
        conn = _db(Path(args.db) if args.db else None)
        try:
            plan = plan_updates(conn)
            print(json.dumps({
                "global_prior_hit_rate": plan["prior"],
                "chains_with_outcomes": plan["chains_with_outcomes"],
                "would_update": sum(1 for p in plan["plans"]
                                    if p["reason"] == "update"),
                "skipped_sample": sum(1 for p in plan["plans"]
                                      if p["reason"].startswith("skipped_sample")),
                "family_priors": plan["family_priors"],
                "degenerate_families": plan["degenerate_families"],
            }, ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    out = run(Path(args.db) if args.db else None, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.audit_log and not args.dry_run:
        _append_audit(args.audit_log, {
            "ts": out["ts"],
            "applied": out["applied"],
            "global_prior_hit_rate": out["global_prior_hit_rate"],
            "updates_by_prior_kind": out["updates_by_prior_kind"],
            "degenerate_families": out["degenerate_families"],
            "updates": out["updates"],
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())