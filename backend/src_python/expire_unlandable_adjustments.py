# -*- coding: utf-8 -*-
"""v339: 把「新鲜但永远落不了地」的参数建议标成 expired，别让界面一直挂着"待审"。

背景
----
`strategy_param_adjustments` 里 12 条 pending 建议（外加 8 条 sizing.* 不可映射）在
当前参数下**无论怎么批都不会生效**：

  · `sell_min_score` 已在钳制下限 0.1，再"降 10%"被 `set_settings` 钳回 0.1 → noop
  · `signal_threshold` 基线是 0.0，0 × 任何倍数 = 0 → 空操作
  · `sizing.*` 与 settings/策略参数的**计量单位不等价**（比例 vs 名义金额 vs ATR 倍数），
    自动换算会误伤真实仓位/止损，只能人工处理

留着它们 pending 的后果：界面上永远显示「12 条待审」，点批准必失败或必空转 ——
和 P0 那批「98 条 45 天点不动」是同一种假状态。

做法
----
逐条按与审批链路完全一致的口径判定（resolve_key → is_effective → 钳制后比较），
判为不可落地的就置 `status='expired'`，把原因写进 `note`，并保留原始方向/幅度。
**默认 dry-run**，`--apply` 才写库；幂等（重复跑不会重复写）。

用法
----
    python3 expire_unlandable_adjustments.py            # 只看会动哪些
    python3 expire_unlandable_adjustments.py --apply    # 真写
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import db
import param_adjust_apply as paa
import param_key_aliases as pka


_SETTINGS_KEYS = ("buy_min_score", "sell_min_score", "tp1_mult", "tp2_mult",
                  "tp1_ratio", "tp2_ratio")


def _settings_snapshot(conn) -> dict:
    # v339: 必须走 paper_trading.get_settings —— 它固定读 `WHERE id=1`，
    # 而这里原来用 `ORDER BY id DESC LIMIT 1`。表当前只有 1 行所以等价，
    # 一旦变成多行，判定的"当前值"和引擎实际用的就不是同一行。
    try:
        import paper_trading as _pt
        return dict(_pt.get_settings(conn))
    except Exception:
        return {}


def classify(conn, row) -> tuple[str, str] | None:
    """返回 (kind, reason)；能落地的返回 None。"""
    raw_key = str(row["param_key"] or "")
    direction = str(row["direction"] or "")
    try:
        magnitude = float(row["magnitude"] or 0.05)
    except (TypeError, ValueError):
        magnitude = 0.05

    key, bucket, why = pka.resolve_key(raw_key)
    if key is None:
        return ("unmappable", why or "该参数与任何可调项都不等价")

    cur_settings = _settings_snapshot(conn)
    if key in _SETTINGS_KEYS:
        old = cur_settings.get(key)
        if not paa.is_effective(old, direction):
            return ("noop", f"{key} 当前为 {old!r}，比例调整无法生效")
        new = paa.describe_change(old, direction, magnitude, key)
        lo_hi = paa.SETTINGS_CLAMP.get(key)
        if lo_hi and new is not None:
            lo, hi = lo_hi
            new = max(lo, min(hi, new))
        try:
            if new is not None and old is not None and abs(float(new) - float(old)) < 1e-9:
                return ("noop", f"{key} 被钳制回原位（{old} → {new}），等于没改")
        except (TypeError, ValueError):
            pass
        return None

    # strategy 桶：当前版本 params
    try:
        import strategy_store as ss
        cur = ss.get_current_strategy(conn)
        params = dict((cur or {}).get("active_version", {}).get("params") or {})
    except Exception as exc:
        return ("unknown", f"读当前策略失败: {type(exc).__name__}: {exc}")
    old = params.get(key)
    if not paa.is_effective(old, direction):
        return ("noop", f"{key} 基线为 {old!r}，比例调整是空操作（0 × 任何倍数 = 0）")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只预览）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    rows = conn.execute(
        "SELECT id, asset, action, param_key, direction, magnitude, sample_size, status, note "
        "FROM strategy_param_adjustments WHERE status='pending' ORDER BY id"
    ).fetchall()

    expirable = []
    for r in rows:
        verdict = classify(conn, r)
        if verdict:
            kind, reason = verdict
            expirable.append((r, kind, reason))

    if args.json:
        import json
        print(json.dumps({
            "pending_before": len(rows),
            "expirable": len(expirable),
            "detail": [{"id": r["id"], "param_key": r["param_key"],
                        "direction": r["direction"], "kind": k, "reason": why}
                       for r, k, why in expirable],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"pending {len(rows)} 条 → 其中 {len(expirable)} 条在当前参数下不可落地")
    for r, kind, why in expirable:
        print(f"  id={r['id']:<4} {r['param_key']:24} {r['direction']:6} "
              f"{r['magnitude']:<5} [{kind}] {why}")

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 才真的标 expired。")
        return 0

    deleted = 0
    updated = 0
    for attempt in range(5):
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted = updated = 0
            for r, kind, why in expirable:
                note = f"v339 清退：{why}" + (f" | 原: {r['note']}" if r["note"] else "")
                # v339: 表上有唯一索引 (asset, action, param_key, direction, status)，
                # 于是 pending → expired 在「同键 expired 已存在」时必然 UNIQUE 冲突、
                # 整批回滚 —— 这就是第一次清退被回滚、pending 11 条原地不动的真因。
                # 处理：把样本数并入已有的 expired 行，再删掉这条重复的 pending。
                dup = conn.execute(
                    "SELECT id, sample_size, note FROM strategy_param_adjustments "
                    "WHERE status='expired' AND asset=? AND action=? AND param_key=? "
                    "  AND direction=? ORDER BY id DESC LIMIT 1",
                    (r["asset"], r["action"], r["param_key"], r["direction"]),
                ).fetchone()
                if dup:
                    merged_n = int(dup["sample_size"] or 0) + int(r["sample_size"] or 0)
                    merged_note = ((str(dup["note"] or "") +
                                    f" | 合并重复 pending id={r['id']}")[:800])
                    conn.execute(
                        "UPDATE strategy_param_adjustments "
                        "SET sample_size=?, note=?, updated_at=datetime('now') WHERE id=?",
                        (merged_n, merged_note, int(dup["id"]),
                    ))
                    conn.execute(
                        "DELETE FROM strategy_param_adjustments WHERE id=? AND status='pending'",
                        (r["id"],),
                    )
                    deleted += 1
                    continue
                conn.execute(
                    "UPDATE strategy_param_adjustments "
                    "SET status='expired', note=?, updated_at=datetime('now') "
                    "WHERE id=? AND status='pending'",
                    (note[:800], r["id"]),
                )
                updated += 1
            conn.commit()
            print(f"\n✅ 清退完成：改状态 {updated} 条，合并并删除重复 {deleted} 条")
            return 0
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc).lower():
                raise
            time.sleep(2 + attempt)
    print("❌ 多次重试仍 database is locked，未写库", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
