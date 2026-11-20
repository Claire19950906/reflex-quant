#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_reflection_a_subclass.py — v326: 把 A 类"方向或时机错误"拆成可定位的子类
===============================================================================
问题 (2026-11-13 只读实测):
  trade_reflections_v2 的 A 类 2626 条 **100%** 落在 market_regime_misread 一个
  小类里, S10「反思维度细分集中度」53.0%, 门槛 40%。细分不是没定义 ——
  reflection_dimension_map 早就给了 A 类 4 个小类, 但**没有任何一条路径用得上**:
  旧分类器只有一句 "该资产有因果链, 但信号没命中触发条件" 就判成"市场状态误读"。

真实成因 (join trade_decisions.deny_reason 才看得见):
    asset_BTC / asset_WTI       1481 条  ← 资产额度/白名单把单子拒了 (其中 1433 条事后是赚钱的)
    R8:同批次兄弟决策=N>3        613 条  ← 同批次连坐拒单 (其中 585 条事后是亏的)
    high_strength                283 条  ← 强度闸门拒单 (278 条事后赚钱)
    无拒单原因                       7 条
  也就是说: 这些"漏检"绝大多数**不是认知错**, 是执行规则把该做的单拦掉了。
  把它们和"看错市场"混在一起, 既解释不了问题, 也让三维度卡片失去诊断价值。

本脚本做三件事 (都基于机器事实, 不做猜测):
  1. 有 deny_reason → 按 reflection_dimension_map.classify_deny_reason 落到
     risk_limit_blocked / batch_conflict_blocked / strength_gate_blocked
     (或 B/C 里对应的小类, 如"置信度低于闸门"→ calibration_drift);
  2. 没有 deny_reason 但有因果链 → 用同组联动区分:
     同组 ≥2 个资产 ±2h 同向异动 → market_regime_misread (板块级状态没读到)
     只有它自己动 → pattern_recognition (单资产形态/强度没识别)
  3. 证据写进 subcategory_evidence 列, 便于事后复核每一条判定。

用法:
    python3 backfill_reflection_a_subclass.py --dry-run     # 只统计不写 (默认)
    python3 backfill_reflection_a_subclass.py --apply       # 真写
    python3 backfill_reflection_a_subclass.py --apply --limit 500

安全性:
  · 只改 dimension / subcategory / subcategory_evidence 三列, 不删行不动别的列。
  · 幂等: 重跑结果一致 (分类是纯函数, 输入不变则输出不变)。
  · 只处理 dimension='A' 的行 —— B/C 细分不在本条 (用户第 3 项只要求拆 A)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review import reflection_dimension_map as rdm

# v486：统一走 db_location（支持 TRIDENT_DB_PATH 覆盖）
from db_location import DB_PATH

# 同组联动阈值 —— 与 missed_signal_reflection / missed_signal_taxonomy 同口径
PEER_CO_MOVE_MIN = 2
PEER_WINDOW_SEC = 7200
PEER_MIN_MOVE_PCT = 1.5

try:
    from cem_constants import CROSS_ASSET_GROUPS
except ImportError:  # pragma: no cover
    CROSS_ASSET_GROUPS = {
        "crypto": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
        "metal": ["XAU"], "energy": ["WTI"],
        "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "memory": ["MU", "SKHYNIX"],
    }


def _peer_co_move_count(conn: sqlite3.Connection, asset: str, ts: int,
                        move_sign: int) -> int:
    if not ts or not asset or move_sign == 0:
        return 0
    peers: List[str] = []
    for members in CROSS_ASSET_GROUPS.values():
        if asset in members:
            peers = [a for a in members if a != asset]
            break
    if not peers:
        return 0
    ph = ",".join("?" for _ in peers)
    try:
        rows = conn.execute(
            "SELECT asset FROM critical_price_moves WHERE asset IN (%s) "
            "AND ts BETWEEN ? AND ? AND ABS(COALESCE(move_pct,0)) >= ? "
            "AND ((move_pct > 0 AND ? > 0) OR (move_pct < 0 AND ? < 0)) "
            "GROUP BY asset" % ph,
            (*peers, ts - PEER_WINDOW_SEC, ts + PEER_WINDOW_SEC,
             PEER_MIN_MOVE_PCT, move_sign, move_sign),
        ).fetchall()
    except sqlite3.Error:
        return 0
    return len(rows)


def classify_one(conn: sqlite3.Connection, row: Dict) -> Tuple[str, str, str]:
    """返回 (dimension, subcategory, evidence)。纯函数式: 同样输入必得同样输出。"""
    deny = str(row.get("deny_reason") or "").strip()
    if deny:
        mapped = rdm.classify_deny_reason(deny)
        if mapped:
            return mapped[0], mapped[1], "%s · deny_reason=%s" % (mapped[2], deny[:80])

    asset = str(row.get("asset") or "").upper()
    ts = int(row.get("decision_ts") or 0)
    pnl = row.get("pnl_pct")
    try:
        move_sign = 1 if float(pnl) > 0 else (-1 if float(pnl) < 0 else 0)
    except (TypeError, ValueError):
        move_sign = 0

    has_chain = False
    if ts and asset:
        try:
            has_chain = bool(conn.execute(
                "SELECT 1 FROM causal_chains WHERE asset=? AND enabled=1 LIMIT 1",
                (asset,),
            ).fetchone())
        except sqlite3.Error:
            has_chain = False

    if has_chain:
        peers = _peer_co_move_count(conn, asset, ts, move_sign)
        if peers >= PEER_CO_MOVE_MIN:
            return ("A", "market_regime_misread",
                    "链未触发 + 同组 %d 个资产同窗口同向异动 (板块级状态没读到)" % peers)
        return ("A", "pattern_recognition",
                "链未触发 + 同组无联动 (单资产形态/强度信号没识别)")

    # 既无拒单原因也无链: 保留原判定, 不改 (不为了凑分布去瞎改)
    return (str(row.get("dimension") or "A"), str(row.get("subcategory") or ""),
            "无 deny_reason 且无因果链 → 维持原判定")


def _load_rows(conn: sqlite3.Connection, limit: Optional[int]) -> List[Dict]:
    sql = """
        SELECT r.id, r.asset, r.subcategory, r.dimension, r.pnl_pct,
               t.deny_reason, t.decision_ts
          FROM trade_reflections_v2 r
          LEFT JOIN trade_decisions t ON t.id = r.decision_id
         WHERE r.dimension = 'A'
         ORDER BY r.id
    """
    if limit:
        sql += " LIMIT %d" % int(limit)
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    except sqlite3.Error as exc:
        raise SystemExit("查询失败: %s" % exc)


def run(apply: bool, limit: Optional[int]) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)")}
        if "subcategory_evidence" not in cols:
            conn.execute("ALTER TABLE trade_reflections_v2 "
                         "ADD COLUMN subcategory_evidence TEXT NOT NULL DEFAULT ''")
            print("[MIGRATE] 补列 subcategory_evidence")

        rows = _load_rows(conn, limit)
        before = Counter(r["subcategory"] or "(空)" for r in rows)
        after = Counter()
        changed = 0
        updates = []
        for r in rows:
            dim, sub, ev = classify_one(conn, r)
            after[sub or "(空)"] += 1
            if (dim, sub) != (r["dimension"], r["subcategory"]):
                changed += 1
            updates.append((dim, sub, ev, r["id"]))

        total = len(rows) or 1
        print("A 类待处理: %d 条" % len(rows))
        print("【前】" + " · ".join("%s %d (%.1f%%)" % (k, v, 100 * v / total)
                                   for k, v in before.most_common(6)))
        print("【后】" + " · ".join("%s %d (%.1f%%)" % (k, v, 100 * v / total)
                                   for k, v in after.most_common(8)))
        top = after.most_common(1)[0] if after else ("", 0)
        print("最大小类占比 %.1f%% (门槛 40%%)" % (100 * top[1] / total))
        print("判定变化: %d 条" % changed)

        if not apply:
            print("\n[DRY-RUN] 未写库。加 --apply 生效。")
            # 抽样打印, 方便人工核对判定是否合理
            for (dim, sub, ev, rid) in updates[:6]:
                src = next(x for x in rows if x["id"] == rid)
                print("  #%s %s pnl=%s → %s/%s | %s"
                      % (rid, src["asset"], src["pnl_pct"], dim, sub, ev))
            return 0

        conn.executemany(
            "UPDATE trade_reflections_v2 SET dimension=?, subcategory=?, "
            "subcategory_evidence=? WHERE id=?",
            updates,
        )
        conn.commit()
        print("\n[APPLY] 已写入 %d 行" % len(updates))
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="A 类反思维度细分回填 (v326)")
    ap.add_argument("--apply", action="store_true", help="真写库 (默认只 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写 (默认行为)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条 (排查用)")
    args = ap.parse_args()
    return run(apply=bool(args.apply) and not bool(args.dry_run),
               limit=args.limit or None)


if __name__ == "__main__":
    raise SystemExit(main())
