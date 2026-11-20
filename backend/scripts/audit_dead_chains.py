#!/usr/bin/env python3
"""v419 · 存量死链可挽回性诊断（只读，不改库）

背景：v418 已堵住新建死链的三个入口，但库里还躺着约 308 条"启用即死"的历史链：
  `asset` 判不出（UNKNOWN/NONE/空/MACRO）或 `trigger_words` 为空，
  `load_active_chains` 直接跳过 —— 永不评估、永不注入，只占 UI 分母。

这一步不删不写，只回答一个问题：**这 308 条里，多少是真能救回来的？**

分类口径（按"该怎么处置"分，不是按字段分）：

  A 非市场机制   note 已被 v424 判为"系统体检日志/非市场机制" —— 本就不该进链库
  B 可补品种     源 review_insight 有明确 asset（WTI/XAU/BTC/ETH/SOL），只是建链时丢了
                 → 补 asset + 补触发词即可救活（需人工过一眼）
  C 缺触发词     有品种、只是触发词空 → 补词即可救活
  D 规则/教训     无品种、源洞察也无品种，但语义是"执行规则/复盘教训"（非资产因果链）
                 → 建议转入 review_insights / 规则库，不留在链库
  E 待人工         其余（信息不全，需逐条看）

用法：
  python3 scripts/audit_dead_chains.py [--out runtime/dead_chains_audit.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BACKEND, "trident_event_bus.db")

DEAD_ASSETS = {"", "UNKNOWN", "NONE", "NULL", "MACRO"}
PRICED = {"WTI", "XAU", "BTC", "ETH", "SOL"}

_NON_MARKET = ("系统体检日志", "非市场机制")
# 复盘教训 / 执行规则的语言特征（这类东西不是"因→品种价格"的传导）
_RULE_HINTS = ("过度交易", "重复入场", "重复信号", "仓位", "止损", "执行", "风控",
               "分散", "集中押注", "胜率", "盈亏", "摩擦成本", "策略")
_INSIGHT_REF = re.compile(r"review_insight\s*#(\d+)")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _insight(conn, note: str):
    m = _INSIGHT_REF.search(note or "")
    if not m:
        return None
    row = conn.execute(
        "SELECT id, asset, rule, scope, kind FROM review_insights WHERE id=?", (int(m.group(1)),)
    ).fetchone()
    return dict(row) if row else None


def classify(row: dict, insight: dict | None) -> tuple[str, str]:
    """返回 (类别, 一句话理由)。"""
    note = row["note"] or ""
    asset = str(row["asset"] or "").strip().upper()
    kw = (row["trigger_words"] or "").strip()
    dead_asset = asset in DEAD_ASSETS
    no_kw = not kw

    if any(h in note for h in _NON_MARKET):
        return "A", "note 已判为非市场机制（系统体检日志），不进链库"

    src_asset = str((insight or {}).get("asset") or "").strip().upper()
    if dead_asset and src_asset in PRICED:
        if no_kw:
            return "B", f"源洞察有品种 {src_asset}，但触发词也空 —— 补 asset+触发词可救"
        return "B", f"源洞察有品种 {src_asset}，仅建链时丢了 asset —— 补一行 asset 可救"

    if not dead_asset and no_kw:
        return "C", f"品种= {asset} 已明确，只缺触发词"

    text = " ".join([row["name"] or "", row["cause"] or "", row["effect"] or "", note])
    if any(h in text for h in _RULE_HINTS) and dead_asset:
        return "D", "无品种且语义是执行/风控规则或复盘教训，不是资产因果链"

    return "E", "信息不全，需逐条看"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runtime/dead_chains_audit.csv")
    args = ap.parse_args()
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BACKEND, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = _connect()
    rows = [
        dict(r) for r in conn.execute(
            """SELECT id, name, cause, effect, trigger_words, note, asset, origin, enabled, active
               FROM causal_chains
               WHERE asset IS NULL OR asset = '' OR UPPER(asset) IN ('UNKNOWN','NONE','MACRO')
                  OR COALESCE(trigger_words,'') = ''
               ORDER BY id"""
        ).fetchall()
    ]
    seen: dict[int, dict] = {}
    result = []
    for r in rows:
        ins = _insight(conn, r["note"] or "")
        cls, why = classify(r, ins)
        r["class"], r["why"] = cls, why
        r["src_insight_id"] = (ins or {}).get("id", "")
        r["src_asset"] = (ins or {}).get("asset", "")
        r["src_rule"] = ((ins or {}).get("rule") or "")[:120]
        # 同一机制被建两条（v418 根因）—— 按 (cause, effect) 记重
        key = (r["cause"] or "").strip()[:80], (r["effect"] or "").strip()[:80]
        if key in seen:
            r["dup_of"] = seen[key]
        else:
            seen[key] = r["id"]
            r["dup_of"] = ""
        result.append(r)

    fields = ["class", "id", "asset", "trigger_words", "name", "cause", "effect", "note",
              "origin", "enabled", "active", "src_insight_id", "src_asset", "src_rule", "dup_of", "why"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(result)

    counts: dict[str, int] = {}
    for r in result:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    dups = sum(1 for r in result if r["dup_of"])
    print(f"死链合计 {len(result)} 条 → {out_path}")
    for cls in ("A", "B", "C", "D", "E"):
        print(f"  {cls}: {counts.get(cls, 0)}")
    print(f"  其中同一机制重复建链(可安全合并): {dups}")
    for cls in ("A", "B", "C", "D", "E"):
        sample = next((r for r in result if r["class"] == cls), None)
        if sample:
            print(f"  {cls} 示例 #{sample['id']} {sample['name'][:40]} | {sample['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
