"""v339: 参数调整积压 → 过期切分 → 冲突裁决 → 批量应用（「反思一键生效」执行层）。

为什么需要这个模块
------------------
1. `strategy_param_adjustments` 现有 98 条 pending，100% 的 param_key 带命名空间
   前缀（`score.buy_min_score` 等），审批端白名单不认 → 点「批准」恒 400。
2. `strategy_store.add_version(source='reflection')` 有硬限制：**每天最多 1 个版本**，
   逐条应用要 98 天。
3. 反思侧按 (asset, action) 维度给建议，但生效点（paper_trading_settings /
   strategy_versions.params）是**全局参数**。同一时刻 XAU/BUY 要求提高
   `buy_min_score`、BTC/BUY 要求降低 → 必然冲突。实测 2026-11-14 08:58:03
   同一秒就同时写出 sell_min_score 的 up 与 down。

处理规则
--------
· 新鲜度：`last_suggested_at` 超过 `max_age_days`（默认 7 天）的 pending
  → 标 `expired`（证据已过期，不再自动应用）。实测 98 条里 86 条超 7 天。
· 冲突裁决：同 canonical key 的 up/down（或 on/off）按证据权重比较
  `Σ(sample_size × avg_confidence)`，胜方方向生效，magnitude 取胜方组内最大值。
  这是把 per-asset 建议折算成全局参数时唯一站得住的算法。
· 不可映射（sizing./filter. 等语义不等价）一律不动，只报告原因。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from param_key_aliases import check_direction, resolve_key

DEFAULT_MAX_AGE_DAYS = 7

# 与 paper_trading.set_settings 内部的 min/max 钳制保持一致（2026-11-14 核对：
# buy/sell_min_score → [0.1, 0.9]；tp*_mult → [0.1, 3.0]；tp*_ratio → [0.0, 0.9]）。
# 不同步这套边界，「预览」和「实际写入」会给出两个不同答案。
SETTINGS_CLAMP = {
    "buy_min_score": (0.1, 0.9),
    "sell_min_score": (0.1, 0.9),
    "tp1_mult": (0.1, 3.0),
    "tp2_mult": (0.1, 3.0),
    "tp1_ratio": (0.0, 0.9),
    "tp2_ratio": (0.0, 0.9),
}


def _row_dict(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _evidence_weight(adj: Dict[str, Any]) -> float:
    try:
        sample = float(adj.get("sample_size") or 1)
    except (TypeError, ValueError):
        sample = 1.0
    try:
        conf = float(adj.get("avg_confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    return max(sample, 1.0) * max(conf, 0.01)


def _group_direction(dirs: Dict[str, float]) -> str:
    """按证据权重选方向；on/off 与 up/down 同时出现时以权重高者为准。"""
    if not dirs:
        return ""
    return max(dirs.items(), key=lambda kv: kv[1])[0]


def build_plan(conn, *, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> Dict[str, Any]:
    """读取 pending，切分过期 / 裁决冲突 / 产出净变更方案（不写库）。"""
    conn.row_factory = sqlite3.Row
    rows = [_row_dict(r) for r in conn.execute(
        """SELECT * FROM strategy_param_adjustments
            WHERE status='pending'
              AND last_suggested_at >= datetime('now', ?)
            ORDER BY id""",
        (f"-{int(max_age_days)} day",),
    ).fetchall()]
    stale_rows = [_row_dict(r) for r in conn.execute(
        """SELECT id, param_key, last_suggested_at, sample_size FROM strategy_param_adjustments
            WHERE status='pending'
              AND (last_suggested_at < datetime('now', ?) OR last_suggested_at IS NULL)
            ORDER BY id""",
        (f"-{int(max_age_days)} day",),
    ).fetchall()]

    groups: Dict[str, Dict[str, Any]] = {}
    unmappable: List[Dict[str, Any]] = []

    for adj in rows:
        raw = str(adj.get("param_key") or "")
        canon, bucket, why = resolve_key(raw)
        if canon is None:
            unmappable.append({
                "id": adj.get("id"), "param_key": raw,
                "asset": adj.get("asset"), "action": adj.get("action"),
                "reason": why,
            })
            continue
        g = groups.setdefault(canon, {
            "canonical_key": canon, "bucket": bucket,
            "raw_keys": set(), "ids": [], "dir_weight": {},
            "max_mag_by_dir": {}, "evidence": [],
        })
        d = str(adj.get("direction") or "")
        w = _evidence_weight(adj)
        g["raw_keys"].add(raw)
        g["ids"].append(int(adj.get("id") or 0))
        g["dir_weight"][d] = g["dir_weight"].get(d, 0.0) + w
        g["max_mag_by_dir"][d] = max(
            g["max_mag_by_dir"].get(d, 0.0), float(adj.get("magnitude") or 0.05))
        g["evidence"].append({
            "id": adj.get("id"), "asset": adj.get("asset"), "action": adj.get("action"),
            "direction": d, "magnitude": adj.get("magnitude"),
            "sample_size": adj.get("sample_size"), "confidence": adj.get("avg_confidence"),
            "weight": round(w, 2), "last_suggested_at": adj.get("last_suggested_at"),
        })

    plan_groups: List[Dict[str, Any]] = []
    for canon, g in sorted(groups.items(), key=lambda kv: (kv[1]["bucket"], kv[0])):
        direction = _group_direction(g["dir_weight"])
        dirs = sorted(g["dir_weight"].keys())
        conflict = len(dirs) > 1
        magnitude = round(g["max_mag_by_dir"].get(direction, 0.05), 4)
        loser_weight = round(sum(v for k, v in g["dir_weight"].items() if k != direction), 2)
        ok_dir, why_dir = check_direction(canon, direction, magnitude)
        plan_groups.append({
            "canonical_key": canon,
            "bucket": g["bucket"],
            "raw_keys": sorted(g["raw_keys"]),
            "direction": direction,
            "magnitude": magnitude,
            "conflict": conflict,
            "conflict_directions": dirs if conflict else [],
            "direction_weights": {k: round(v, 2) for k, v in g["dir_weight"].items()},
            "loser_weight": loser_weight,
            "adjustment_ids": g["ids"],
            "apply": ok_dir,
            "direction_error": "" if ok_dir else why_dir,
            "evidence": g["evidence"],
        })

    return {
        "max_age_days": max_age_days,
        "fresh_pending": len(rows),
        "stale_pending": len(stale_rows),
        "stale_ids": [int(r["id"]) for r in stale_rows],
        "groups": plan_groups,
        "unmappable": unmappable,
        "summary": {
            "fresh_rows": len(rows),
            "stale_rows": len(stale_rows),
            "mappable_groups": len(plan_groups),
            "conflict_groups": sum(1 for g in plan_groups if g["conflict"]),
            "settings_groups": sum(1 for g in plan_groups if g["bucket"] == "settings"),
            "strategy_groups": sum(1 for g in plan_groups if g["bucket"] == "strategy"),
            "unmappable_rows": len(unmappable),
        },
    }


# v339: `paper_trading._apply_param_change` 里**不同参数用的是不同算法**：
#   buy_min_score / sell_min_score / tp1_ratio / tp2_ratio → 绝对加法（cur ± magnitude）
#   tp1_mult / tp2_mult                                   → 乘法（cur × (1 ± magnitude)）
#   news_trade_max_age_seconds                            → 乘法 + 取整
# 之前这里一律按乘法折算（注释还写着"与 paper_trading 同口径"，其实是错的），
# 于是「预览 0.15 → 0.1725」而真实落地是「0.15 → 0.30」，两者差 74% ——
# 界面上看到的预览数字是假的，审批的人按它做判断必然错。
# v339 后续：上面那 4 个加法型参数已统一改成乘法（magnitude 全系统按百分比理解），
# 现在两边都是乘法，_ADDITIVE_PARAMS 留作空占位、不再有特殊分支。
_ADDITIVE_PARAMS = frozenset()


def describe_change(old_value: Any, direction: str, magnitude: float,
                    param_key: str = "") -> Any:
    """把 up/down/on/off 折算成预览值（与 paper_trading._apply_param_change 同口径）。

    v339: 旧值缺失时返回 None，不编造 0 或 0.5 —— 把 `None` 当 0 折算出来的
    "预览值" 会让人以为参数真能从 0 涨上去（`sl_mult` 当前就是 None）。
    """
    if direction in ("on", "off"):
        return 1 if direction == "on" else 0
    if old_value is None:
        return None
    try:
        base = float(old_value)
    except (TypeError, ValueError):
        return None                      # 枚举/空串/非数值 —— 没有可折算的基线
    if param_key in _ADDITIVE_PARAMS:
        delta = magnitude if direction == "up" else -magnitude
        return round(base + delta, 4)
    factor = 1.0 + (magnitude if direction == "up" else -magnitude)
    return round(base * factor, 4)


def is_effective(old_value: Any, direction: str) -> bool:
    """比例型调参在基数为 0 时是空操作（0 × 0.93 = 0）。

    实测：策略参数 `signal_threshold` 当前就是 0.0，任何 up/down 都改不动它，
    这类变更必须显式跳过并报告，而不是写一个"看起来应用了"的版本。
    旧值为 None（未设置）同样不可比 —— 但原因是"没有基线"，不是"基线为 0"。
    """
    if direction in ("on", "off"):
        return True
    if old_value is None:
        return False
    try:
        return abs(float(old_value)) > 1e-9
    except (TypeError, ValueError):
        return False


def apply_plan(conn, plan: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    """执行归并方案。dry_run=True 只算不写；写库时所有 Result 落在一个 strategy 版本。"""
    import paper_trading as _pt
    import strategy_store as _ss

    results: List[Dict[str, Any]] = []
    cur = _ss.get_current_strategy(conn)
    sid = int(cur["id"])
    params = dict(cur.get("active_version", {}).get("params") or {})
    cur_settings = _pt.get_settings(conn)
    strategy_touched = 0
    version_id: Optional[int] = None

    for g in plan["groups"]:
        canon = g["canonical_key"]
        if not g.get("apply", True):
            results.append({**g, "status": "skipped_invalid_direction",
                            "detail": g.get("direction_error", "")})
            continue
        before = cur_settings.get(canon) if g["bucket"] == "settings" else params.get(canon)
        if not is_effective(before, g["direction"]):
            results.append({**g, "status": "skipped_ineffective", "old_value": before,
                            "new_value": before,
                            "detail": f"基线为 0，{g['direction']} {g['magnitude']} 是空操作"})
            continue
        synth = {
            "id": 0, "asset": (g["evidence"][0].get("asset") if g["evidence"] else None),
            "action": (g["evidence"][0].get("action") if g["evidence"] else None),
            "param_key": canon, "direction": g["direction"],
            "magnitude": g["magnitude"],
        }
        after = describe_change(before, g["direction"], g["magnitude"],
                                canon)
        if g["bucket"] == "settings" and g["direction"] not in ("on", "off"):
            # 与 paper_trading.set_settings 完全同口径的钳制。不同步这一层，
            # 预览会显示 0.1→0.09（看起来有变化），实际写库被 max(0.1) 钳回 0.1，
            # 状态标 applied 而数值不动 —— 假通过。
            lo, hi = SETTINGS_CLAMP.get(canon, (None, None))
            if lo is not None:
                after = round(min(hi, max(lo, float(after))), 4)
        if g["direction"] not in ("on", "off"):
            try:
                if abs(float(after) - float(before)) < 1e-9:
                    results.append({**g, "status": "skipped_noop", "old_value": before,
                                    "new_value": after,
                                    "detail": "折算后与原值相同（受钳制区间限制），应用无意义"})
                    continue
            except (TypeError, ValueError):
                pass
        if g["bucket"] == "settings":
            if not dry_run:
                _pt.apply_adjustment_to_settings(synth, conn)
            results.append({**g, "status": "would_apply" if dry_run else "applied",
                            "old_value": before, "new_value": after})
        else:
            params[canon] = after
            strategy_touched += 1
            results.append({**g, "status": "would_apply" if dry_run else "applied",
                            "old_value": before, "new_value": after})

    if strategy_touched and not dry_run:
        ver = _ss.add_version(
            sid, params, source="reflection",
            note=f"v339 backlog apply: {strategy_touched} key(s) merged",
            connection=conn,
        )
        version_id = int(ver["id"])

    if not dry_run:
        applied_keys = {r["canonical_key"] for r in results
                        if r["status"] in ("applied", "would_apply")}
        for g in plan["groups"]:
            if g["canonical_key"] not in applied_keys:
                continue
            ids = g["adjustment_ids"]
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET status='applied', applied_strategy_version_id=?,
                       applied_setting_change=?, note=?, updated_at=datetime('now')
                   WHERE id IN (%s)""" % ",".join("?" * len(ids)),
                (version_id, f"{g['canonical_key']}: {g['direction']} {g['magnitude']}",
                 f"v339 merged apply ids={','.join(map(str, ids))}", *ids),
            )
        if plan.get("stale_ids"):
            conn.execute(
                """UPDATE strategy_param_adjustments
                   SET status='expired', note=?, updated_at=datetime('now')
                   WHERE id IN (%s)""" % ",".join("?" * len(plan["stale_ids"])),
                (f"v339 expired: last_suggested_at 超过 {plan['max_age_days']} 天，证据已过期",
                 *plan["stale_ids"]),
            )
        conn.commit()

    return {
        "dry_run": dry_run,
        "settings_changes": sum(1 for g in plan["groups"] if g["bucket"] == "settings"),
        "strategy_keys": strategy_touched,
        "strategy_version_id": version_id,
        "results": results,
        "unmappable": plan["unmappable"],
        "stale_expired": len(plan.get("stale_ids") or []),
    }
