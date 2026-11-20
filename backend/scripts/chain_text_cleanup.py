#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_text_cleanup.py — v413: 清掉链"文本字段"里的 JSON 字段名

背景（2026-11-17 实测）
----------------------
第一轮只洗了 `trigger_words/trigger_keywords`，但**描述文本**没洗：
161 条链的 `mechanism` 里还混着 `Asset, verdict, market_event, prompt,
geopolitical_conflict, trigger_ref` 这些**我们自己的字段名**，而且会原样进到
决策 prompt（`chain_prior.build_prompt_block` 打印"机理"那一段），
等于让模型读一堆 schema 噪声，还稀释了真实关键词的权重。

做法
----
只删**明确的 schema token**（白名单），不动 BUY/SELL/WIN 这类真实语义词；
删除范围：`mechanism / precondition / invalidation / note` 四个文本字段。
默认 dry-run，`--apply` 才写库，每条改动进审计 jsonl。

用法
    python3 scripts/chain_text_cleanup.py
    python3 scripts/chain_text_cleanup.py --apply
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

TEXT_FIELDS = ("mechanism", "precondition", "invalidation", "note")

# 只删这些（都是本系统自己的列名/枚举名，绝不可能是市场语义）
SCHEMA_TOKENS = (
    "Asset", "verdict", "partial", "market_event", "prompt", "geopolitical_conflict",
    "trigger_ref", "trigger_source", "expected_direction", "magnitude_lo", "magnitude_hi",
    "confidence", "evidence", "hit_count", "miss_count", "decision_space", "predicted_mag_lo",
)


def _clean(text: str) -> str:
    s = str(text or "")
    if not s:
        return s
    for tok in SCHEMA_TOKENS:
        # 逗号列表里的 token：", Asset" / "Asset, " / 单独成项
        s = re.sub(r"[，,]\s*%s(?=[，,\s」]|$)" % re.escape(tok), "", s)
        s = re.sub(r"(?<=[「，,\s])%s\s*[，,]" % re.escape(tok), "", s)
    # 兜底：剩下的裸 token（前后是空白或引号）
    for tok in SCHEMA_TOKENS:
        s = re.sub(r"(?<![A-Za-z_])%s(?![A-Za-z_])" % re.escape(tok), "", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"「\s*[，,]", "「", s)
    s = re.sub(r"[，,]\s*」", "」", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row

    hits = []
    for r in conn.execute("SELECT * FROM causal_chains"):
        for f in TEXT_FIELDS:
            old = r[f]
            if not old:
                continue
            new = _clean(old)
            if new != old:
                hits.append((int(r["id"]), f, old, new))

    print("库: %s" % DB_PATH)
    print("待清理: %d 处（涉及 %d 条链）"
          % (len(hits), len({h[0] for h in hits})))
    for cid, f, old, new in hits[:4]:
        print("  #%s %s\n    旧: %s\n    新: %s"
              % (cid, f, str(old)[:70].replace("\n", " "), str(new)[:70].replace("\n", " ")))
    if not args.apply:
        print("\n（dry-run：未写库。加 --apply 落库）")
        return 0

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for cid, f, old, new in hits:
        conn.execute("UPDATE causal_chains SET %s=?, updated_at=? WHERE id=?" % f,
                     (new, now, cid))
    conn.commit()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / ("text-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with path.open("w", encoding="utf-8") as fh:
        for cid, f, old, new in hits:
            fh.write(json.dumps({"id": cid, "field": f, "old": old, "new": new},
                                ensure_ascii=False) + "\n")
    print("\n已落库 %d 处。审计: %s" % (len(hits), path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
