#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_node_cleanup.py — v407: 因果图谱节点清洗（可逆，默认 dry-run）

实测问题（2026-11-17 只读审计 asset_driver_nodes 1156 节点）
------------------------------------------------------------
1. **字段名当驱动**：10 个活跃节点的 label 是列名 —— BTC 的唯一 core 驱动
   叫 `historical_confidence`，ETH 的 core 叫 `cluster_heat`。LLM 拿到这种
   "驱动"只会把它当成因果因子。
2. **新闻标题当驱动**：WTI 的 core 是"美sec批准多资产3倍杠杆etp(含原油)" ——
   一条事件标题被当成底层驱动，事件是"果的前一步"，不是"因"。
3. 无证据节点置信度 0.5 铺满（456 条 evidence 节点统一 0.5），排序无意义。

处置（不做删除，只改状态，一条 UPDATE 可恢复）
------------------------------------------------
· 字段名样式 → active=0（失效），evidence 追加 v407 审计理由；
· 疑似事件标题且 is_core=1 → is_core=0 + kind='condition'（降级为条件因素，
  仍然可被注入，但不再冒充"核心驱动"）；
· 置信度 0.5 且 evidence 为空/全是审计日志的 core → 压到 0.35（低置信标记）。

用法
----
    python3 scripts/kg_node_cleanup.py            # dry-run
    python3 scripts/kg_node_cleanup.py --apply
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

FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,}$")
# 事件标题特征：动词 + 长句。命中就说明这是"某次事件"，不是"某个驱动"。
EVENT_VERBS = ("批准", "发布", "宣布", "下调", "上调", "谈判", "袭击", "会谈",
               "决议", "讲话", "报告", "数据", "签署", "制裁", "停火", "投票")
MIN_HEADLINE_LEN = 16


def _is_field(name: str) -> bool:
    return bool(FIELD_RE.match(name or ""))


def _is_event_headline(name: str) -> bool:
    s = str(name or "")
    if len(s) < MIN_HEADLINE_LEN:
        return False
    return any(v in s for v in EVENT_VERBS)


# v410: 自指/执行层当成"市场驱动"。实测 COIN 的 core 是「信号生成与交易执行机制缺陷」——
# 那是系统自身的毛病，不是加密资产的涨跌原因。
META_WORDS = ("信号生成", "交易执行", "手续费", "滑点", "仓位管理", "止损", "复盘", "结算失败")


def _is_meta_label(name: str) -> bool:
    return any(w in str(name or "") for w in META_WORDS)


def main() -> int:
    ap = argparse.ArgumentParser(description="因果图谱节点清洗 (v407)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row

    rows = [dict(r) for r in conn.execute(
        "SELECT id, asset, label, kind, is_core, confidence, active, evidence "
        "FROM asset_driver_nodes WHERE COALESCE(active,1)=1"
    ).fetchall()]

    fields = [r for r in rows if r["kind"] != "asset" and _is_field(r["label"])]
    headlines = [r for r in rows if int(r["is_core"] or 0) == 1 and _is_event_headline(r["label"])]
    low_conf = [r for r in rows if int(r["is_core"] or 0) == 1
                and float(r["confidence"] or 0) == 0.5
                and "audit" not in (r["evidence"] or "")]
    meta = [r for r in rows if int(r["is_core"] or 0) == 1 and _is_meta_label(r["label"])]
    try:
        from causal_graph import _foreign_asset_label as _foreign
        foreign = [r for r in rows if int(r["is_core"] or 0) == 1 and _foreign(r["asset"], r["label"])]
    except Exception:
        foreign = []

    print("库: %s" % DB_PATH)
    print("活跃节点 %d · 字段名样式 %d · 事件标题冒充核因 %d · 0.5 无证据核因 %d · 跨品种污染 %d · 自指/执行层 %d"
          % (len(rows), len(fields), len(headlines), len(low_conf), len(foreign), len(meta)))
    for r in fields[:8]:
        print("  [字段名] %s/%s" % (r["asset"], r["label"]))
    for r in headlines[:6]:
        print("  [事件标题] %s/%s" % (r["asset"], str(r["label"])[:44]))

    if not args.apply:
        print("（dry-run：未写库。加 --apply 落库）")
        return 0

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    touched = 0
    for r in fields:
        conn.execute(
            "UPDATE asset_driver_nodes SET active=0, updated_at=? WHERE id=?",
            (now, int(r["id"])),
        )
        touched += 1
    for r in headlines:
        conn.execute(
            "UPDATE asset_driver_nodes SET is_core=0, kind='condition', updated_at=? WHERE id=?",
            (now, int(r["id"])),
        )
        touched += 1
    for r in low_conf:
        # 0.5 的"无证据核因"统一标到 0.35，让 prompt 里的低置信提示生效
        conn.execute(
            "UPDATE asset_driver_nodes SET confidence=0.35, updated_at=? WHERE id=?",
            (now, int(r["id"])),
        )
        touched += 1
    for r in meta:
        # 系统自身的"信号生成/交易执行"问题被当成市场驱动 → 停用（这些词描述的不是涨跌原因）
        conn.execute(
            "UPDATE asset_driver_nodes SET active=0, updated_at=? WHERE id=?",
            (now, int(r["id"])),
        )
        touched += 1
    for r in foreign:
        # 别的品种的专有别名当"核心驱动" → 降级为条件因素（不删：它可能是某次触发链的
        # 真实条件，只是不配当这个品种的底层原因）
        conn.execute(
            "UPDATE asset_driver_nodes SET is_core=0, kind='condition', updated_at=? WHERE id=?",
            (now, int(r["id"])),
        )
        touched += 1
    conn.commit()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit = AUDIT_DIR / ("nodes-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with audit.open("w", encoding="utf-8") as fh:
        for r in fields + headlines + low_conf:
            fh.write(json.dumps({"id": r["id"], "asset": r["asset"], "label": r["label"]},
                                ensure_ascii=False) + "\n")
    print("已落库: 触碰 %d 条节点" % touched)
    print("审计: %s" % audit)

    left = conn.execute(
        "SELECT COUNT(*) FROM asset_driver_nodes WHERE COALESCE(active,1)=1 AND is_core=1"
    ).fetchone()[0]
    print("剩余活跃核心驱动: %d 条" % left)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
