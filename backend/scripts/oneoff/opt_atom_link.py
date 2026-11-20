# -*- coding: utf-8 -*-
"""opt_atom_link.py — v336 证据链补齐（ai_decisions.event_atom_ids）

问题（实测）:
    ai_decisions 8865 条, 带 event_atom_ids 的只有 5357 条（60.4%）。
    剩下 3508 条决策**没有可追溯的事件原子** —— 于是"双重验证""因果链回溯"
    在最底层就是空的：你说不出这条决策引用了哪条原子事实。

可补的只有一部分:
    事件原子表 event_atom 只有 3804 条, 覆盖的新闻有限;
    实测 3508 条缺链决策里,**只有 307 条（8.7%）**能靠 news_id 找回原子。
    剩下 3201 条不是"关联写漏了", 是**上游根本没做原子化** ——
    补链只能补这 307 条, 其余的要把 event_struct 的原子化覆盖率提上去。

用法:
    python3 opt_atom_link.py            # 只报告（默认）
    python3 opt_atom_link.py --commit   # 写回 event_atom_ids（只补空值）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _empty(v: Any) -> bool:
    s = str(v or "").strip()
    return s in ("", "[]", "null", "None")


def plan(conn: sqlite3.Connection) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
    """返回 ([(decision_id, atom_ids_json)], 统计)。"""
    missing = conn.execute(
        "SELECT count(*) n FROM ai_decisions WHERE event_atom_ids IS NULL "
        "OR event_atom_ids='' OR event_atom_ids='[]'"
    ).fetchone()["n"]

    pairs: List[Tuple[int, str]] = []
    no_atom_news = 0
    rows = conn.execute(
        "SELECT d.id did, ne.news_id nid FROM ai_decisions d "
        "JOIN news_events ne ON ne.id = d.news_id "
        "WHERE d.event_atom_ids IS NULL OR d.event_atom_ids='' OR d.event_atom_ids='[]'"
    ).fetchall()
    for r in rows:
        atoms = conn.execute(
            "SELECT id FROM event_atom WHERE raw_news_id=? ORDER BY id", (r["nid"],)).fetchall()
        if not atoms:
            no_atom_news += 1
            continue
        atom_ids = [a["id"] for a in atoms]
        pairs.append((r["did"], json.dumps(atom_ids, separators=(",", ":"))))

    total = conn.execute("SELECT count(*) n FROM ai_decisions").fetchone()["n"]
    covered = total - missing
    return pairs, {
        "decisions_total": total,
        "missing": missing,
        "covered_pct": round(100.0 * covered / total, 1) if total else None,
        "recoverable": len(pairs),
        "recoverable_pct_of_missing": round(100.0 * len(pairs) / missing, 1) if missing else None,
        "news_without_atom": no_atom_news,
        "upstream_gap": missing - len(pairs),
    }


def commit(pairs: List[Tuple[int, str]], limit: int = 0) -> int:
    rw = sqlite3.connect(DB, timeout=60)
    n = 0
    try:
        with rw:
            for did, blob in (pairs[:limit] if limit else pairs):
                n += rw.execute(
                    "UPDATE ai_decisions SET event_atom_ids=? WHERE id=? "
                    "AND (event_atom_ids IS NULL OR event_atom_ids='' OR event_atom_ids='[]')",
                    (blob, did)).rowcount
    finally:
        rw.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        pairs, rep = plan(conn)
    finally:
        conn.close()
    if args.commit:
        rep["committed"] = commit(pairs, args.limit)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"决策 {rep['decisions_total']} 条 · 已带证据链 {rep['covered_pct']}% · 缺 {rep['missing']} 条")
    print(f"可补回 {rep['recoverable']} 条（占缺口 {rep['recoverable_pct_of_missing']}%）")
    print(f"上游未原子化 {rep['upstream_gap']} 条（这些补不了，要提 event_struct 覆盖率）")
    if args.commit:
        print(f"已写入 {rep.get('committed', 0)} 条")
    else:
        print("（dry-run：没有写库。确认后加 --commit）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
