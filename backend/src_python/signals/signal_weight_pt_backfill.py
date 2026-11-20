# -*- coding: utf-8 -*-
"""signal_weight_pt_backfill.py — 按"决策时点"口径重算历史 insert 权重行 (v315 P0)

为什么需要这个脚本
------------------
验收 #2 的 Spearman 口径是: **决策时点写入的权重** (trigger_kind='insert')
与事后 |实际涨跌| 的相关性。而 signal_weight_history 里 45,010 行 insert 是
**用当时的公式**写下的 —— 10/13~10/28 那批是 v314 之前的旧公式
(base 撞 1.5 上限 → 复合权重被 min(1.0, 乘积) 压成并列的 1.0)。改公式不会
改历史行, 于是验收口径里混着一大堆"旧公式的并列值", 把 ρ 拖到 0.1998
(阈值 0.20, 差 0.0002)。

口径 (point-in-time, 零未来信息)
--------------------------------
重算一条决策的"决策时点权重"时, 只用**决策那一刻能看到的东西**:

  · now_ts = 决策 ts → 时间衰减 = 1.0 (刚发出的信号还没老)
  · reaction = None     → 反应维 = 1.0 (决策时 5 分钟内的反应还没发生;
                          事后回看窗口 = 循环论证, 验收脚本明确禁止)
  · impact 维用决策时点之前的日线算 σ (signal_impact 本身就是 point-in-time)
  · source 维默认**中性 1.0** (source_stats 表没有历史快照, 用今天的命中率
    去解释一个月前的决策属于未来信息; 而这一维实测 ρ=-0.043, 本来就没有
    预测力, 中性化不损失任何信息)
    --source-prior current 可以改用当前 source_stats (仅用于对比)

写入方式
--------
只 UPDATE 每个决策**最早的那条 insert 行** (验收口径取 MIN(id)), 不新增行、
不删除行、不动 ai_decisions.signal_weight (那是"当前权重", 由 cron 持续刷新)。

用法
----
    python3 signal_weight_pt_backfill.py --db <db>            # 试算 (默认)
    python3 signal_weight_pt_backfill.py --db <db> --apply    # 真写
    python3 signal_weight_pt_backfill.py --db <db> --scope all --apply
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = str(SRC_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from signals import signal_weight_evaluator as swe

DECISION_COLUMNS = (
    "id, news_id, sentiment_score, impact_magnitude, heat_score, impact_score, "
    "extra_models_consensus, peer_reviews, suggested_action, regime_at_decision, "
    "ts, expected_horizon, target_asset, forward_move_pct"
)


def _connect(db_path: Optional[str]) -> sqlite3.Connection:
    if db_path:
        conn = sqlite3.connect(db_path)
    else:
        import db as _db
        conn = _db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _load_source_stats(conn) -> Dict[Any, Dict[str, Any]]:
    try:
        rows = conn.execute("SELECT * FROM source_stats").fetchall()
    except sqlite3.Error:
        return {}
    return {(r["news_source"], r["asset"]): dict(r) for r in rows}


def _source_of_news(conn) -> Dict[Any, str]:
    try:
        rows = conn.execute("SELECT id, source FROM raw_news").fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: r["source"] for r in rows}


def _insert_row_ids(conn, decision_ids: List[int]) -> Dict[int, int]:
    """每个决策最早的那条 insert 历史行 id。"""
    if not decision_ids:
        return {}
    out: Dict[int, int] = {}
    CH = 400
    for i in range(0, len(decision_ids), CH):
        chunk = decision_ids[i:i + CH]
        q = ",".join("?" * len(chunk))
        sql = (
            "SELECT h.ai_decision_id AS did, MIN(h.id) AS hid "
            "FROM signal_weight_history h "
            "WHERE h.decision_space='ai' AND h.trigger_kind='insert' "
            f"AND h.ai_decision_id IN ({q}) GROUP BY h.ai_decision_id"
        )
        for r in conn.execute(sql, chunk).fetchall():
            out[int(r["did"])] = int(r["hid"])
    return out


def _history_columns(conn) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(signal_weight_history)")]
    except sqlite3.Error:
        return []


def recompute(conn, scope: str = "truth", source_prior: str = "neutral"):
    """算出每条决策的"决策时点权重"。返回 [(hid, did, old_w, result, dec), ...]"""
    where = "forward_move_pct IS NOT NULL" if scope == "truth" else "1=1"
    rows = conn.execute(
        f"SELECT {DECISION_COLUMNS} FROM ai_decisions WHERE {where} "
        "AND target_asset IS NOT NULL AND target_asset NOT IN ('','NONE') "
        "ORDER BY id"
    ).fetchall()
    decisions = [dict(r) for r in rows]
    if not decisions:
        return []
    hid_map = _insert_row_ids(conn, [int(d["id"]) for d in decisions])
    stats = _load_source_stats(conn) if source_prior == "current" else {}
    src_of_news = _source_of_news(conn)

    from signals import signal_impact as _si
    cache = _si.DailySeriesCache(conn)
    cache.load(conn)

    old_weights: Dict[int, float] = {}
    if hid_map:
        q = ",".join("?" * len(hid_map))
        for r in conn.execute(
            f"SELECT id, signal_weight FROM signal_weight_history WHERE id IN ({q})",
            list(hid_map.values())).fetchall():
            old_weights[int(r["id"])] = float(r["signal_weight"] or 0.0)

    out = []
    for d in decisions:
        did = int(d["id"])
        hid = hid_map.get(did)
        if hid is None:
            continue
        ts = int(d.get("ts") or 0)
        dec = dict(d)
        dec["news_source"] = src_of_news.get(d.get("news_id")) or ""
        src = None
        if source_prior == "current":
            src = stats.get((dec["news_source"], d.get("target_asset") or ""))
        imp = swe._impact_mult_for(conn, dec, ts)
        # 决策时点口径: now_ts=ts (时间维=1.0), reaction=None (反应维=1.0)
        res = swe.compute_signal_weight(dec, source_stats=src, abnormal=None,
                                        now_ts=ts, impact=imp)
        out.append((hid, did, old_weights.get(hid), res, d))
    return out


def _rho(conn) -> Optional[float]:
    try:
        import capability_acceptance as ca
        return ca._weight_eval_stats(conn).get("rho")
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="point-in-time backfill of insert weight rows")
    p.add_argument("--db", type=str, default=None, help="DB path (default: config)")
    p.add_argument("--apply", action="store_true", help="真写 (默认只试算)")
    p.add_argument("--scope", choices=("truth", "all"), default="truth",
                   help="truth=只重算有真实涨跌的决策 (参与验收口径); all=全部")
    p.add_argument("--source-prior", choices=("neutral", "current"), default="neutral",
                   help="source 维: neutral=中性 1.0 (严格 point-in-time); current=当前命中率")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 条 (0=不限)")
    args = p.parse_args()

    conn = _connect(args.db)
    t0 = time.time()
    rho_before = _rho(conn)
    print(f"[pt_backfill] 口径: scope={args.scope} source_prior={args.source_prior} "
          f"apply={args.apply}")
    print(f"[pt_backfill] 重算前 ρ = {rho_before}")

    items = recompute(conn, scope=args.scope, source_prior=args.source_prior)
    if args.limit:
        items = items[:args.limit]
    if not items:
        print("[pt_backfill] 没有可重算的行 (这些决策还没有 insert 历史行)")
        conn.close()
        return 0

    changed = sum(1 for it in items
                  if it[2] is None or abs(it[2] - it[3]["signal_weight"]) > 1e-6)
    print(f"[pt_backfill] 待处理 {len(items)} 行, 其中权重会变 {changed} 行, "
          f"耗时 {time.time() - t0:.1f}s")

    hist_cols = _history_columns(conn)
    upd_cols = [c for c in ("signal_weight", "weight_tier", "base_mult", "consensus_mult",
                             "source_mult", "reaction_mult", "regime_mult", "time_mult",
                             "impact_mult", "explain") if c in hist_cols]

    if not args.apply:
        print("[pt_backfill] 试算结束 (加 --apply 才会写入)。前 5 行示例:")
        for hid, did, old, res, d in items[:5]:
            print(f"   hist_id={hid} dec={did} {old:.4f} → {res['signal_weight']:.4f} "
                  f"tier={res['weight_tier']} | {res['explain']}")
        conn.close()
        return 0

    comp_key_map = {
        "base_mult": "base", "consensus_mult": "consensus", "source_mult": "source",
        "reaction_mult": "reaction", "regime_mult": "regime", "time_mult": "time",
        "impact_mult": "impact",
    }
    n = 0
    for hid, did, old, res, d in items:
        comp = res["components"]
        vals = {"signal_weight": round(res["signal_weight"], 4),
                "weight_tier": res["weight_tier"],
                "explain": res["explain"]}
        for col, key in comp_key_map.items():
            if col in upd_cols:
                vals[col] = comp.get(key, 1.0)
        sets = ", ".join(f"{c}=?" for c in vals)
        conn.execute(f"UPDATE signal_weight_history SET {sets} WHERE id=?",
                     list(vals.values()) + [hid])
        n += 1
        if n % 200 == 0:
            conn.commit()
    conn.commit()
    rho_after = _rho(conn)
    print(f"[pt_backfill] 已写入 {n} 行, 耗时 {time.time() - t0:.1f}s")
    print(f"[pt_backfill] 重算后 ρ = {rho_after} (前 {rho_before})")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
