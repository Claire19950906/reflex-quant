#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_ingest_cleanup.py — 因果链存量回填：触发词净化 + 品种重绑（v400 P0-1/P0-2）

为什么要有这个脚本
------------------
改了入库口径（`chain_keywords` + `causal_chains.add_chain/update_chain`）之后，
**存量 752 条链不会自动变干净** —— 而决策侧（`chain_prior` / bridge）读的是库存值。
不跑这一步，等于只修了"以后新加的链"。

做什么
------
1. `trigger_words` → `chain_keywords.clean_keywords()`（删系统 token / 品种代码 /
   超长短语 / 重复，上限 12 个）；
2. `asset` / `trigger_keywords` → `causal_chain_evaluator.derive_strategy_columns()`
   （语义打分：effect+3 / name+2 / cause+1 / trigger+1，判不准留 UNKNOWN）；
3. 不删链、不改 weight、不改 enabled —— 净化后没有关键词的链会**自然失活**
   （匹配要求 ≥1 关键词），不需要也不应该由脚本去停用它。

安全
----
· **默认 dry-run**，只打表不写库；`--apply` 才落库。
· 每条改动都写 `runtime/chain_cleanup/<ts>.jsonl` 审计（原值 → 新值）。
· 支持 `--db` 指向副本，先在副本上验证再动生产。

用法
----
    python3 scripts/chain_ingest_cleanup.py                       # 预览
    python3 scripts/chain_ingest_cleanup.py --db /tmp/copy.db --apply   # 副本上落库
    python3 scripts/chain_ingest_cleanup.py --apply --json        # 生产落库（慎用）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC_PY = BACKEND / "src_python"
if str(SRC_PY) not in sys.path:
    sys.path.insert(0, str(SRC_PY))

import chain_keywords as ck

AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"


def _connect(db_path: str, write: bool) -> sqlite3.Connection:
    if write:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def plan(db_path: str, limit: int | None = None) -> dict:
    """算出每条链的新词表/新品种（不写库）。"""
    conn = _connect(db_path, write=False)
    try:
        chains = [dict(r) for r in conn.execute(
            "SELECT id, name, cause, effect, trigger_words, asset, trigger_keywords"
            " FROM causal_chains ORDER BY id"
        ).fetchall()]
        priced = {r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily").fetchall() if r[0]}
    finally:
        conn.close()

    if limit:
        chains = chains[:limit]

    items, changes = [], Counter()
    for ch in chains:
        raw = ch["trigger_words"] or ""
        # trigger_words 保留 ticker（品种证据），匹配列 trigger_keywords 会剔除
        res = ck.clean_keywords(raw, drop_tickers=False)
        matchable = ck.clean_keywords(raw)   # 真正能用于匹配的词（ticker 已剔除）
        decision = ck.decide_asset(
            ch["name"] or "", ch["cause"] or "", ch["effect"] or "", raw,
            priced_assets=priced or None,
        )
        old_asset = (ch["asset"] or "UNKNOWN").upper()
        new_asset = decision.asset
        items.append({
            "id": ch["id"],
            "trigger_words_old": raw,
            "trigger_words_new": res.joined,
            "asset_old": old_asset,
            "asset_new": new_asset,
            "asset_strength": decision.strength,
            "kw_removed": len(res.removed) + (len(res.kept) - len(matchable.kept)),
            "kw_kept": len(matchable.kept),
            "kw_suspect": res.suspect,
            "changed": (res.joined != raw) or (new_asset != old_asset),
        })
        if new_asset != old_asset:
            changes[(old_asset, new_asset)] += 1

    return {
        "db": db_path,
        "chains": len(items),
        "changed": sum(1 for i in items if i["changed"]),
        "kw_changed": sum(1 for i in items if i["trigger_words_new"] != i["trigger_words_old"]),
        "asset_changed": sum(1 for i in items if i["asset_new"] != i["asset_old"]),
        "kw_total_removed": sum(i["kw_removed"] for i in items),
        "kw_zero_after": sum(1 for i in items if i["kw_kept"] == 0),
        "suspect": sum(1 for i in items if i["kw_suspect"]),
        "asset_transitions": {f"{a}->{b}": n for (a, b), n in changes.most_common(15)},
        "items": items,
    }


def apply(db_path: str, plan_result: dict, *, write_audit: bool = True) -> dict:
    """落库：先写 trigger_words，再统一刷新派生列。"""
    conn = _connect(db_path, write=True)
    audit_path = None
    try:
        # 延迟导入：派生列与纯函数同源
        from causal_chain_evaluator import derive_strategy_columns

        touched = []
        for item in plan_result["items"]:
            if item["trigger_words_new"] == item["trigger_words_old"]:
                continue
            conn.execute(
                "UPDATE causal_chains SET trigger_words=?,"
                " updated_at=datetime('now','localtime') WHERE id=?",
                (item["trigger_words_new"][:300], item["id"]),
            )
            touched.append(int(item["id"]))
        conn.commit()

        # 派生列必须**全表重算**：品种绑定规则变了，词表没变的链也要重绑，
        # 否则"半张表新规则、半张表旧规则"，比不改更难排查。
        derived = derive_strategy_columns(conn, chain_ids=None)
        conn.commit()

        if write_audit:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            audit_path = AUDIT_DIR / f"cleanup-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            with audit_path.open("w", encoding="utf-8") as f:
                for item in plan_result["items"]:
                    if not item["changed"]:
                        continue
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

        return {
            "trigger_words_updated": len(touched),
            "derived": derived,
            "audit": str(audit_path) if audit_path else None,
        }
    finally:
        conn.close()


def _print_report(res: dict, applied: dict | None = None) -> None:
    print(f"库: {res['db']}")
    print(f"链: {res['chains']}  需要改动: {res['changed']}  "
          f"（词表 {res['kw_changed']} / 品种 {res['asset_changed']}）")
    print(f"删词总数: {res['kw_total_removed']}  净化后零关键词: {res['kw_zero_after']}  "
          f"疑似污染(suspect): {res['suspect']}")
    print("品种迁移 TOP:")
    for k, v in res["asset_transitions"].items():
        print(f"  {k:<24}{v:>6}")
    if applied:
        print(f"已落库: trigger_words {applied['trigger_words_updated']} 条 · "
              f"派生列 {applied['derived'].get('updated', 0)} 条 · 审计 {applied['audit']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="默认 config.DB_PATH")
    ap.add_argument("--apply", action="store_true", help="真正落库（默认 dry-run）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-audit", action="store_true", help="不写审计 jsonl")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        import config
        db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print(f"[ERR] 库不存在: {db_path}", file=sys.stderr)
        return 2

    res = plan(db_path, limit=args.limit)
    applied = None
    if args.apply:
        applied = apply(db_path, res, write_audit=not args.no_audit)

    if args.json:
        out = {k: v for k, v in res.items() if k != "items"}
        out["applied"] = applied
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(res, applied)
        if not args.apply:
            print("（dry-run：未写库。加 --apply 落库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
