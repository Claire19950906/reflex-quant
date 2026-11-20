#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_direction_prune.py — v410: 方向口径校准（数据驱动剪枝，不调用 LLM）

背景
----
`/api/causal/quality` 实测：UP 链方向命中 33.4%、DOWN 链 67.9%。
这个命中率是**超额收益**口径（`causal_chain_evaluator` 用 `pct_5d - 市场等权基准`），
所以不是"大盘跌了所以 UP 全错"。

原因在录入侧：链几乎都是"新闻高峰那一刻自动生成"的，而新闻高峰后的标的常**跑输市场**
（利好出尽 / 注意力退潮）。于是预测 UP 的链系统性反向，预测 DOWN 的系统性"对"。

为什么不做方向翻转
------------------
链名本身写着「利好→上涨」，把 expected_direction 改成 DOWN 会让名称与方向自相矛盾，
而且 `chain_direction_reconcile.py` 的两遍 LLM 交叉复核（不一致即 BOTH）才是项目既定的改法。

本脚本只做**按证据剪枝**：样本够（n≥8，与 `chain_prior.MIN_EVAL_FOR_OFF` 同口径）
且超额命中率 ≤30% 的链，其因果假设已被数据否证 → 停用。
同时把方向基率写入 `runtime/chain_calibration.json`，供 prompt 标注"UP 类链当前不可信"。

用法
    python3 scripts/chain_direction_prune.py            # dry-run，只报告
    python3 scripts/chain_direction_prune.py --apply    # 停用被否证的链
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
CALIB_PATH = BACKEND / "runtime" / "chain_calibration.json"
sys.path.insert(0, str(BACKEND / "src_python"))

MIN_EVAL = 8
FALSIFY_RATE = 0.30


def _wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT c.id, c.asset, c.name, c.expected_direction, "
        "       SUM(CASE WHEN o.direction_hit=1 THEN 1 ELSE 0 END) hit, "
        "       COUNT(o.id) n "
        "FROM causal_chains c JOIN causal_chain_outcomes o ON o.chain_id=c.id "
        "WHERE o.direction_hit IS NOT NULL AND c.enabled=1 "
        "GROUP BY c.id HAVING n >= ?", (MIN_EVAL,)).fetchall()

    by_dir: dict = {}
    for r in rows:
        d = str(r["expected_direction"] or "BOTH").upper()
        agg = by_dir.setdefault(d, {"n": 0, "hit": 0})
        agg["n"] += int(r["n"])
        agg["hit"] += int(r["hit"] or 0)

    print("库: %s" % DB_PATH)
    print("样本 n≥%d 的启用链 %d 条\n" % (MIN_EVAL, len(rows)))
    print("%-6s %6s %6s %9s %10s" % ("方向", "样本", "命中", "命中率", "Wilson下界"))
    for d in sorted(by_dir):
        a = by_dir[d]
        lo, _ = _wilson(a["hit"], a["n"])
        print("%-6s %6d %6d %8.1f%% %9.1f%%"
              % (d, a["n"], a["hit"], 100.0 * a["hit"] / a["n"], 100 * lo))

    cands = [r for r in rows
             if str(r["expected_direction"] or "").upper() in ("UP", "DOWN")
             and (r["hit"] or 0) / r["n"] <= FALSIFY_RATE]
    print("\n超额命中率 ≤%.0f%%（因果假设被否证）: %d 条" % (100 * FALSIFY_RATE, len(cands)))
    for r in sorted(cands, key=lambda x: (x["hit"] or 0) / x["n"])[:10]:
        print("  [%s] %s %s  %.0f%% (%d/%d)"
              % (r["expected_direction"], r["asset"], str(r["name"])[:26],
                 100 * (r["hit"] or 0) / r["n"], r["hit"] or 0, r["n"]))

    calib = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "min_samples": MIN_EVAL,
        "by_direction": {
            d: {"n": a["n"], "hit": a["hit"],
                "hit_rate": round(a["hit"] / a["n"], 4),
                "wilson_lower": round(_wilson(a["hit"], a["n"])[0], 4)}
            for d, a in by_dir.items()},
        "falsified_chains": len(cands),
        "note": "命中率为超额收益口径(已剥离市场等权基准)。UP 基率低源于'新闻高峰生成链、之后"
                "跑输市场'的录入偏差；不翻转方向，只按证据剪枝。",
    }
    print("\n方向基率:")
    print(json.dumps(calib["by_direction"], ensure_ascii=False, indent=1))

    if not args.apply:
        print("\n（dry-run：未写库、未覆盖 %s）" % CALIB_PATH.name)
        return 0

    CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIB_PATH.write_text(json.dumps(calib, ensure_ascii=False, indent=1), "utf-8")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in cands:
        conn.execute("UPDATE causal_chains SET enabled=0, active=0, updated_at=? WHERE id=?",
                     (now, int(r["id"])))
    conn.commit()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / ("prune-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with path.open("w", encoding="utf-8") as fh:
        for r in cands:
            fh.write(json.dumps({"id": int(r["id"]), "asset": r["asset"],
                                 "direction": r["expected_direction"],
                                 "hit": int(r["hit"] or 0), "n": int(r["n"])},
                                ensure_ascii=False) + "\n")
    print("\n已停用 %d 条（审计里有 id，可逐条回滚）· 标定已写入 %s" % (len(cands), CALIB_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
