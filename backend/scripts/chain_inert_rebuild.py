#!/usr/bin/env python3
"""v401 P0 · 无效链收尾：没有可用关键词的链，统一停用（可逆）。

背景（回填后实测，752 条链）：
    236 条链 `trigger_words` 里全是系统 token/ticker，`clean_keywords()` 之后
    一个可用词都不剩。逐条看过它们的正文：**235 条是 `[auto_from_insight]
    ...auto_truth#NNN` 这类自动生成的模板链**（正文就是"经常漏报事件类型""资产
    价格、信号受影响"的填空句），1 条是流程教训（"原油门卫主线信号冗余→过度交易"）。
    它们不是"事件→价格"的因果链，是流程/覆盖度告警，塞在市场因果表里只能制造噪声。

**不重挖**：试过用 `name+cause+effect` 重抽关键词，抽出来的全是
`auto_from_insight / Asset=NONE / 覆盖度告警 / NONE-auto_truth#774` ——
比空词表更糟（会给它们一个"能匹配"的假象）。所以处置只有一条：**停用**。

停用是 `enabled=0 / active=0`，行不删、原值写审计，恢复一条 UPDATE 即可。

用法：
    python3 scripts/chain_inert_rebuild.py            # 默认 dry-run
    python3 scripts/chain_inert_rebuild.py --apply
    python3 scripts/chain_inert_rebuild.py --db /tmp/fin10_chain_cleanup.db --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import chain_keywords as ck

AUDIT_DIR = os.path.join(os.path.dirname(_HERE), "runtime", "chain_cleanup")


def _db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    import db
    return db.get_db_path()


def plan(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id, name, trigger_words, asset, enabled, active FROM causal_chains"
    ).fetchall()
    items = []
    for r in rows:
        if ck.clean_keywords(r["trigger_words"] or "").kept:
            continue                      # 还有可用词，不归本脚本管
        items.append({
            "id": r["id"],
            "name": (r["name"] or "")[:60],
            "asset": r["asset"],
            "trigger_words_old": r["trigger_words"],
            "enabled_old": r["enabled"],
            "active_old": r["active"],
        })
    return {"total": len(rows), "items": items}


def apply(conn: sqlite3.Connection, p: dict) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f"inert-{stamp}.jsonl")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    changed = 0
    with open(audit_path, "w", encoding="utf-8") as fh:
        for it in p["items"]:
            if not it["enabled_old"] and not it["active_old"]:
                continue                  # 本来就停用，不重复写
            conn.execute(
                "UPDATE causal_chains SET enabled=0, active=0, updated_at=? WHERE id=?",
                (now, it["id"]),
            )
            changed += 1
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    conn.commit()
    return {"deactivated": changed, "audit": audit_path}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = _db_path(args.db)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    p = plan(conn)
    items = p["items"]
    need = [i for i in items if i["enabled_old"] or i["active_old"]]

    print(f"库: {path}")
    print(f"总链 {p['total']} · 无可用关键词 {len(items)}"
          f" · 其中仍处于启用/活跃态 {len(need)}")
    if args.json:
        print(json.dumps({"items": items}, ensure_ascii=False))

    if not args.apply:
        print("dry-run：未写库（加 --apply 落库）")
        conn.close()
        return 0

    res = apply(conn, p)
    left = conn.execute(
        "SELECT COUNT(*) FROM causal_chains WHERE active=1"
    ).fetchone()[0]
    conn.close()
    print(f"已落库: 停用 {res['deactivated']} 条 · 剩余 active=1 {left} 条")
    print(f"审计: {res['audit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
