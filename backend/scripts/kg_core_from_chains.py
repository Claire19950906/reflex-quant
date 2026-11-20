#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_core_from_chains.py — v410: 用**已有证据**补核心驱动（不靠 LLM 编）

为什么要这个
------------
`kg_coverage_fill.py` 走 LLM 5-why，对库里没有真实新闻的品种（COIN/COPPER/EUR…）
只会吐出"原油/黄金/加密"通用宏观框架，全被入库校验拦掉。实测 ETH 连补 7 条、7 条全拦。

真正的证据在链库里：一条链 `name` 形如「地缘冲突→黄金避险传导」，前半段就是**机制**，
后半段是结果。链上还带 `hit_count/miss_count`（真实结算过的样本）。
所以对"有链、但有链没核因"的品种，直接从命中链的机制名聚类，比 LLM 猜可靠。

规则
----
· 只处理 `enabled=1 且 hit_count>0` 的链（要有真实结算样本，没命中的不采纳）；
· 机制 = `name` 里第一个「→」之前的部分；按规范化后前 8 字聚类，取组内 hit 最多的那条；
· 每品种最多 3 条 core，`confidence = min(0.5, 0.25 + hit*0.05)`（有样本才配谈置信度）；
· `evidence` 写链 id + 命中样本数，`origin` 无列，用 evidence 前缀标 `chain:` 便于追溯；
· 幂等：同 asset + 同 label 已存在就不重复插。

用法
    python3 scripts/kg_core_from_chains.py            # dry-run
    python3 scripts/kg_core_from_chains.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
sys.path.insert(0, str(BACKEND / "src_python"))

MAX_PER_ASSET = 3


def _mechanism(name: str) -> str:
    s = str(name or "").strip()
    for sep in ("→", "->", "=>"):
        if sep in s:
            s = s.split(sep)[0]
    s = re.sub(r"^[【\[]?(?:数据|复盘|总结)[】\]]?", "", s).strip()
    return s[:40]


def _key(label: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", label)[:8]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row

    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM asset_driver_nodes WHERE is_core=1 AND COALESCE(active,1)=1")}
    rows = conn.execute(
        "SELECT id, asset, name, hit_count, miss_count, confidence FROM causal_chains "
        "WHERE enabled=1 AND hit_count>0 AND asset != 'UNKNOWN' "
        "ORDER BY hit_count DESC").fetchall()

    by_asset: dict = {}
    for r in rows:
        mech = _mechanism(r["name"])
        if not mech or len(mech) < 4:
            continue
        by_asset.setdefault(r["asset"], {}).setdefault(_key(mech), []).append((mech, r))

    print("库: %s" % DB_PATH)
    print("有命中样本的品种 %d 个: %s" % (len(by_asset), ", ".join(sorted(by_asset))))
    targets = [a for a in sorted(by_asset) if a not in have]
    print("其中缺核心驱动的: %s" % (", ".join(targets) or "无"))

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for asset in targets:
        picks = []
        for _k, group in by_asset[asset].items():
            group.sort(key=lambda x: -int(x[1]["hit_count"] or 0))
            picks.append(group[0])
        picks.sort(key=lambda x: -int(x[1]["hit_count"] or 0))
        picks = picks[:MAX_PER_ASSET]
        for mech, r in picks:
            hit = int(r["hit_count"] or 0)
            conf = round(min(0.5, 0.25 + hit * 0.05), 3)
            ev = json.dumps({
                # v424: 旧版 evidence 只写 "chain:69(hit=9/miss=2)"（主键+计数），
                # 模型从中什么也学不到，还会被当成核心驱动的"证据"喂进 prompt。
                # 现在写人话：链名 + 样本数，主键只留在审计文件里。
                "chain": [(r["name"] or mech).strip()],
                "_status": "UNCONFIRMED",
                "_source": "chain_lib:%d(hit=%d/miss=%d)"
                           % (r["id"], hit, int(r["miss_count"] or 0)),
            }, ensure_ascii=False)
            exists = conn.execute(
                "SELECT 1 FROM asset_driver_nodes WHERE asset=? AND label=? AND COALESCE(active,1)=1",
                (asset, mech)).fetchone()
            if exists:
                print("  跳过（已存在）%s / %s" % (asset, mech[:30]))
                continue
            print("  + %s / %s  conf=%.2f  %s" % (asset, mech[:34], conf, ev))
            if args.apply:
                conn.execute(
                    "INSERT INTO asset_driver_nodes "
                    "(asset,label,kind,is_core,confidence,evidence,version,active,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (asset, mech, "core", 1, conf, ev, 1, 1, now))
            inserted += 1

    if args.apply and inserted:
        conn.commit()
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIT_DIR / ("core_from_chains-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
        path.write_text('{"inserted": %d, "assets": %s}\n'
                        % (inserted, sorted(targets)), "utf-8")
        print("\n已写入 %d 条。审计: %s" % (inserted, path))
    elif not args.apply:
        print("\n（dry-run：将写入 %d 条，加 --apply 落库）" % inserted)
    else:
        print("\n无需写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
