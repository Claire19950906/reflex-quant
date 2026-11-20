#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填因果链的 expected_direction（用机制语义补方向）+ **回测防泄漏**。

背景（2026-11-07 实测）
----------------------
628 条链里 **476 条(76%) 的 expected_direction = BOTH**，关联链更是 86% BOTH。

根因：`_infer_direction` 只看链的 **name**，而链名是"事件→传导"的标题，
通常不含方向词 → 落 BOTH。

而链的 **mechanism**（第一性原理三问的产物）里其实有明确的方向推理：
    "避险资金流入黄金…推升XAU价格"      → UP
    "产油国设施受威胁…推动WTI价格上涨"  → UP

而且 BOTH 链的命中标准是**幅度**（abs_pct >= mag_lo），UP/DOWN 链才是**方向**
→ 大量 BOTH 会让整体 hit rate 虚高（"波动够大就算中"），掩盖真实判别力。

⚠️ **绝不读 effect** —— 那是复盘结论（"方向正确"/"LOSS"/"兑现"），
用它推导"预测"就是数据泄漏。

用法：
    python3 scripts/backfill_chain_direction.py --dry-run
    python3 scripts/backfill_chain_direction.py
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE_DIR, "src_python")
DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    BASE_DIR, "trident_event_bus.db")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _hit(pred_dir: str, pct_5d: float, mag_lo: float) -> int:
    """与 causal_chain_evaluator.evaluate_one 完全同源的方向判定。"""
    if pred_dir == "UP":
        return 1 if pct_5d > 0 else 0
    if pred_dir == "DOWN":
        return 1 if pct_5d < 0 else 0
    return 1 if abs(pct_5d) >= mag_lo else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-direction-hit", type=float, default=0.0,
                    help="回填所需的最低方向命中率（低于此值判定为泄漏，拒绝回填）")
    args = ap.parse_args()

    import causal_chain_evaluator as cce

    if not os.path.exists(args.db):
        print("DB 不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")

        chains = conn.execute(
            "SELECT id, name, mechanism, precondition, expected_direction, "
            "       asset, magnitude_lo, magnitude_hi FROM causal_chains"
        ).fetchall()
        new_dir = {}
        changed = 0
        for c in chains:
            d = cce._infer_direction(c["name"] or "",
                                     c["mechanism"] or "",
                                     c["precondition"] or "")
            new_dir[str(c["id"])] = d
            if d != (c["expected_direction"] or ""):
                changed += 1

        dist_new, dist_old = {}, {}
        for c in chains:
            d = new_dir[str(c["id"])]
            dist_new[d] = dist_new.get(d, 0) + 1
            o = (c["expected_direction"] or "BOTH")
            dist_old[o] = dist_old.get(o, 0) + 1
        print("[INFO] 链总数 %d | 方向会变的 %d" % (len(chains), changed))
        print("[INFO] 旧方向分布: %s" % dist_old)
        print("[INFO] 新方向分布: %s" % dist_new)
        # ── 回测：只对已对账、且能算出实际涨跌幅的 outcome ──
        mag_of = {}
        for c in chains:
            mag_of[str(c["id"])] = float(c["magnitude_lo"] or 1.0)
        outs = conn.execute(
            """SELECT chain_id, predicted_direction, direction_hit,
                      actual_pct_5d
               FROM causal_chain_outcomes
               WHERE actual_pct_5d IS NOT NULL
                 AND status IN ('HIT','HIT_WEAK','MISS','NEUTRAL')"""
        ).fetchall()
        old_n = old_hit = new_n = new_hit = 0
        old_dn = old_dhit = new_dn = new_dhit = 0
        grp_a_n = grp_a_hit = grp_b_n = grp_b_hit = grp_c_n = grp_c_hit = 0
        for o in outs:
            cid = str(o["chain_id"])
            d_new = new_dir.get(cid)
            if d_new is None:
                continue
            pct = float(o["actual_pct_5d"])
            h_new = _hit(d_new, pct, mag_of.get(cid, 1.0))
            h_old = int(o["direction_hit"] or 0)
            old_n += 1; old_hit += h_old
            new_n += 1; new_hit += h_new
            if d_new in ("UP", "DOWN"):
                new_dn += 1; new_dhit += h_new
            old_d = (o["predicted_direction"] or "BOTH")
            if old_d in ("UP", "DOWN"):
                old_dn += 1; old_dhit += h_old
            # 分组
            if old_d in ("UP", "DOWN") and d_new in ("UP", "DOWN"):
                grp_a_n += 1; grp_a_hit += h_new
            elif old_d not in ("UP", "DOWN") and d_new in ("UP", "DOWN"):
                grp_b_n += 1; grp_b_hit += h_new
            elif old_d in ("UP", "DOWN") and d_new not in ("UP", "DOWN"):
                grp_c_n += 1; grp_c_hit += h_new

        print("\n[回测] 已对账 outcome %d 条" % old_n)
        if old_n:
            print("  全部(含 BOTH 的幅度命中): 旧 %.1f%% → 新 %.1f%%"
                  % (old_hit / old_n * 100, new_hit / new_n * 100))
        # ★关键分组：旧 BOTH → 新有方向 的那批链。
        #   这是唯一能看出"新方向有没有判别力"的组：
        #     · 命中率 ≈ 50% → 无判别力（也没泄漏）
        #     · 命中率 > 70% → 泄漏（mechanism 是看着答案编的）
        #     · 命中率 < 35% → 方向推反了
        if grp_b_n:
            print("  ★旧BOTH→新有方向: %d 条, 方向命中率 %.1f%%  "
                  "(≈50%%=无判别力, >70%%=泄漏)"
                  % (grp_b_n, grp_b_hit / grp_b_n * 100))
        if grp_a_n:
            print("  · 旧有方向→新有方向(未变): %d 条, 命中率 %.1f%%"
                  % (grp_a_n, grp_a_hit / grp_a_n * 100))
        if grp_c_n:
            print("  · 旧有方向→新BOTH(退化): %d 条, 命中率 %.1f%%"
                  % (grp_c_n, grp_c_hit / grp_c_n * 100))
        if not old_dn:
            print("  ★有方向的链样本为 0，无法回测")

        d_rate = (new_dhit / max(1, new_dn)) if new_dn else 0.0
        if new_dn and d_rate < args.min_direction_hit:
            print("\n❌ 新方向命中率 %.1f%% < 阈值 %.1f%% —— 判定为泄漏/不可靠，**拒绝回填**"
                  % (d_rate * 100, args.min_direction_hit * 100))
            return 2

        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0

        t0 = time.time()
        upd = 0
        failed = 0
        for c in chains:
            d = new_dir[str(c["id"])]
            if d == (c["expected_direction"] or ""):
                continue
            # ⚠️ 引擎正在高频写库 → 直接 UPDATE 会 "database is locked"。
            # 逐条提交 + 长退避重试（db.retry_on_busy 只重试 3 次/0.35s，扛不住）。
            delay = 0.4
            ok = False
            for attempt in range(10):
                try:
                    conn.execute(
                        "UPDATE causal_chains SET expected_direction=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (d, c["id"]))
                    conn.commit()
                    ok = True
                    break
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if ("locked" in msg or "busy" in msg) and attempt < 9:
                        time.sleep(delay)
                        delay = min(delay * 1.6, 3.0)
                        continue
                    print("[WARN] 更新 id=%s 失败: %s" % (c["id"], e))
                    failed += 1
                    break
            if ok:
                upd += 1
        print("\n[OK] 已更新 %d 条链的方向 (失败 %d, 耗时 %.1fs)"
              % (upd, failed, time.time() - t0))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

