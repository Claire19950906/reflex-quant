"""backfill_weight_replay.py —— 用修正后的输入重算历史权重（可回滚）

背景（2026-11-14）：
  · impact_score 94.5% 是缺省 5.0，已用门卫分回填 7263 条
  · 情绪分 (a) 档中心映射错误，已修代码 + 定向回填 90 条
  但 `signal_weight_history` 里存的是**写入当时**用旧输入算出的权重，
  所以 S14（权重分辨率）仍停在 14.6% —— 它量到的是一份过期快照。

本脚本做的事（只读 → 双表备份 → 才写）：
  1. 用 ai_decisions **现在**的 sentiment/heat/impact 重算 base_mult
  2. 其余五个乘子（impact/consensus/source/reaction/regime/time）保持库中原值
  3. 用生产公式 compose_weight 合成新权重
  4. 更新 signal_weight_history.signal_weight 与 base_mult

不改：suggested_action、decision 结论、reaction/regime 等其它乘子。
回滚：--rollback 从 signal_weight_replay_backfill 还原。

用法：
  python3 backfill_weight_replay.py           # dry-run：看分辨率与 ρ 变化
  python3 backfill_weight_replay.py --commit
  python3 backfill_weight_replay.py --rollback
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
RB_TABLE = "signal_weight_replay_backfill"

from signals.signal_weight_evaluator import _base_mult, compose_weight  # noqa: E402


def load(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT h.id hid, h.signal_weight w, h.base_mult b, h.impact_mult im, "
        "       h.consensus_mult c, h.source_mult s, h.reaction_mult rt, "
        "       h.regime_mult g, h.time_mult t, "
        "       a.sentiment_score sc, a.heat_score hs, a.impact_magnitude mg, "
        "       a.impact_score isc, a.ts, a.forward_move_pct fm "
        "FROM signal_weight_history h JOIN ai_decisions a ON a.id = h.ai_decision_id "
        "WHERE h.trigger_kind='insert' AND h.decision_space='ai'").fetchall()
    out = []
    for r in rows:
        new_base = _base_mult(r["sc"], r["mg"], r["hs"], impact_score=r["isc"])
        new_w = compose_weight(new_base, r["im"], r["c"], r["s"], r["rt"], r["g"], r["t"])["weight"]
        out.append({
            "hid": int(r["hid"]), "old_w": float(r["w"] or 0.0),
            "old_b": float(r["b"] or 1.0), "new_w": new_w, "new_b": new_base,
            "ts": int(r["ts"] or 0),
            "mv": abs(float(r["fm"])) if r["fm"] is not None else None,
        })
    return out


def _rho(rows: List[Dict[str, Any]], key: str) -> Any:
    from opt_weight_shadow import bucket_rho
    pairs = [{"ts": r["ts"], "abs_move": r["mv"], key: r[key]} for r in rows if r["mv"] is not None]
    if not pairs:
        return None
    return bucket_rho(pairs, key)[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    if args.rollback:
        if RB_TABLE not in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            print(f"没有 {RB_TABLE}，无法回滚")
            return 1
        n = conn.execute(f"SELECT COUNT(*) FROM {RB_TABLE}").fetchone()[0]
        conn.execute(
            "UPDATE signal_weight_history SET "
            f" signal_weight = (SELECT b.old_w FROM {RB_TABLE} b WHERE b.hid = signal_weight_history.id),"
            f" base_mult    = (SELECT b.old_b FROM {RB_TABLE} b WHERE b.hid = signal_weight_history.id)"
            f" WHERE id IN (SELECT hid FROM {RB_TABLE})")
        conn.commit()
        print(f"已回滚 {n} 条历史权重")
        return 0

    rows = load(conn)
    changed = [r for r in rows if abs(r["new_w"] - r["old_w"]) > 1e-9]
    n = len(rows)
    d_old = len({round(r["old_w"], 6) for r in rows})
    d_new = len({round(r["new_w"], 9) for r in rows})
    print(f"insert 权重行 {n} 条 · 数值会变 {len(changed)} 条 ({len(changed)/max(1,n):.1%})")
    print(f"  分辨率 distinct: {d_old} ({d_old/max(1,n):.1%}) → {d_new} ({d_new/max(1,n):.1%})")
    r_old, r_new = _rho(rows, "old_w"), _rho(rows, "new_w")
    print(f"  S4 口径桶内 ρ  : {r_old} → {r_new}")

    if not args.commit:
        print("\n[dry-run] 未写入。样例:")
        for r in changed[:4]:
            print(f"  hid={r['hid']} {r['old_w']:.4f} → {r['new_w']:.4f} "
                  f"(base {r['old_b']:.3f} → {r['new_b']:.3f})")
        print("加 --commit 执行（会先建回滚表）")
        return 0

    import time as _t
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RB_TABLE} ("
        f" hid INTEGER PRIMARY KEY, old_w REAL NOT NULL, old_b REAL NOT NULL,"
        f" backed_up_at INTEGER NOT NULL)")
    now = int(_t.time())
    conn.executemany(f"INSERT OR REPLACE INTO {RB_TABLE} VALUES (?,?,?,?)",
                     [(r["hid"], r["old_w"], r["old_b"], now) for r in rows])
    conn.executemany(
        "UPDATE signal_weight_history SET signal_weight = ?, base_mult = ? WHERE id = ?",
        [(r["new_w"], r["new_b"], r["hid"]) for r in rows])
    conn.commit()
    print(f"\n[commit] 已重算 {n} 条 · 回滚表 {RB_TABLE}")
    print(f"撤销: python3 backfill_weight_replay.py --rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
