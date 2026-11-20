#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_coverage_fill.py — v407: 因果图谱覆盖度补齐（每次最多 N 个品种）

实测问题
--------
36 个品种里只有 10 个有 core 节点（其中 `all` 这个跨品种桶占了 160 条），
BTC/ETH/NDX/XAU/WTI/XAG 各 1 条。没有 core 的品种会被 ai_worker 静默退回
"通用硬编码兜底"（美元反向/实际利率反向）——那套框架对该品种没有任何样本证据。

做法
----
· 只挑 `enabled=1 且 active=1 的 core 节点数 = 0` 的品种；
· 调 causal_graph.run_reasoning（LLM 5-why），入库走 v407 的节点校验
  （列名 / 事件标题会被拒或降级）；
· 每次最多 --max 个，避免一次打满配额；失败品种记 error 不重试。

用法
    python3 kg_coverage_fill.py --max 3 --dry-run
    python3 kg_coverage_fill.py --max 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
GIVEUP_PATH = BACKEND / "runtime" / "kg_coverage_giveup.json"
GIVEUP_DAYS = 14


def _recent_giveup() -> set:
    """14 天内放弃过的品种：LLM 没有这个品种的知识，补出来的东西全被校验拦掉。"""
    try:
        state = json.loads(GIVEUP_PATH.read_text("utf-8")) if GIVEUP_PATH.exists() else {}
    except Exception:
        return set()
    cutoff = time.time() - GIVEUP_DAYS * 86400
    out = set()
    for a, v in state.items():
        try:
            ts = time.mktime(time.strptime(str(v.get("date")), "%Y-%m-%d"))
        except Exception:
            continue
        if ts >= cutoff:
            out.add(str(a).upper())
    return out


def _missing_assets(conn) -> list:
    try:
        from market.market_classifier import TARGET_TO_MARKET
        universe = [a for a in TARGET_TO_MARKET if a and a.upper() != "ALL"]
    except Exception:
        universe = []
    have = {str(r[0]) for r in conn.execute(
        "SELECT DISTINCT asset FROM asset_driver_nodes "
        "WHERE is_core=1 AND COALESCE(active,1)=1")}
    # 优先补有行情、且系统真的会分析的品种；按字母序稳定输出
    skip = _recent_giveup()
    return sorted(a for a in universe
                  if str(a).upper() not in {h.upper() for h in have}
                  and str(a).upper() not in skip)


def main() -> int:
    ap = argparse.ArgumentParser(description="因果图谱覆盖度补齐 (v407)")
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    missing = _missing_assets(conn)
    print("无核心驱动的品种 %d 个: %s" % (len(missing), ", ".join(missing[:12])))
    if not missing:
        print("覆盖度已满，无需补齐")
        return 0
    targets = missing[: max(1, args.max)]
    print("本次处理: %s" % ", ".join(targets))
    if args.dry_run:
        print("（dry-run：不调 LLM、不写库）")
        return 0

    import causal_graph
    t0 = time.time()
    res = causal_graph.run_reasoning(assets=targets, max_workers=2)
    detail = res.get("detail") or {}
    ok = [a for a in (res.get("reasoned") or []) if (detail.get(a) or {}).get("ok")]
    bad = {a: (v or {}).get("error") for a, v in detail.items()
           if not (v or {}).get("ok")}
    n_nodes = conn.execute(
        "SELECT COUNT(*) FROM asset_driver_nodes WHERE is_core=1 AND COALESCE(active,1)=1"
    ).fetchone()[0]
    print("完成 %d/%d · 核心驱动现存 %d 条 · 耗时 %.1fs"
          % (len(ok), len(targets), n_nodes, time.time() - t0))
    for a, e in bad.items():
        print("  %s 失败: %s" % (a, e))

    # v407: 有的品种 LLM 根本没有知识，补出来的全是"原油/黄金/加密"通用框架，
    # 全被 causal_graph 的新校验拦掉 —— 记下来 14 天内不再重试，别每天烧一次配额。
    try:
        state = json.loads(GIVEUP_PATH.read_text("utf-8")) if GIVEUP_PATH.exists() else {}
    except Exception:
        state = {}
    today = time.strftime("%Y-%m-%d")
    for a in ok:
        gained = conn.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes WHERE asset=? AND is_core=1 "
            "AND COALESCE(active,1)=1", (a,)).fetchone()[0]
        if gained == 0:
            state[str(a)] = {"date": today, "reason": "0 core accepted (all rejected by guard)"}
            print("  %s 补出的驱动全被校验拦掉 → 14 天内不再重试" % a)
    if state:
        GIVEUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        GIVEUP_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
