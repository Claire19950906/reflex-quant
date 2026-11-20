"""事件效力统计（v344）：把「哪些事件真的推动了价格」变成可复用的加权依据。

背景：`event_move_attribution` 里每条记录都是「一条新闻 ↔ 一次行情异动」的配对，带
`algo_score`（匹配强度 0~1）与 `move_pct`（异动幅度）。用户 2026-11-14 的诉求是
"把真正有效的事件喂给因果链做加权"，否则链条只能凭 hit_count 自我循环。

口径：
  effective  = algo_score >= 0.8 且 |move_pct| >= 1.0   （高确信 + 真动了）
  hit_rate   = effective / n_total                       （该资产上"事件真能推动价格"的比例）
  avg_move   = 有效事件的平均 |move_pct|

产物写入 `event_effectiveness` 表，按 (asset, source) 与 (asset, '*') 两档存储：
  · source='*'  资产级总览（供 prompt 用一句话概括）
  · source=xxx  具体新闻源在該資產上的效力（供后续按源加权）

用法：
    python3 event_effectiveness.py --recompute      # 重算并入库
    python3 event_effectiveness.py --show           # 只打印现状
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "trident_event_bus.db"

SCORE_MIN = 0.8
MOVE_MIN = 1.0


def _connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15.0)
        conn.execute("PRAGMA busy_timeout=15000")
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_effectiveness (
            asset        TEXT NOT NULL,
            source       TEXT NOT NULL,
            n_total      INTEGER NOT NULL,
            n_effective  INTEGER NOT NULL,
            hit_rate     REAL NOT NULL,
            avg_move     REAL NOT NULL,
            updated_at   INTEGER NOT NULL,
            PRIMARY KEY (asset, source)
        )
        """
    )


def compute(conn: sqlite3.Connection) -> List[Tuple[str, str, int, int, float, float, int]]:
    """按 (asset, source) 与 (asset,'*') 两档统计。返回可直接入库的行。"""
    rows = conn.execute(
        """
        SELECT asset, COALESCE(ev_source,''), COALESCE(algo_score,0),
               COALESCE(move_pct,0), COALESCE(move_ts,0)
          FROM event_move_attribution
        """
    ).fetchall()

    now = int(time.time())
    per_src: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "eff": 0, "sum_abs": 0.0, "n_eff": 0}
    )
    per_asset: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "eff": 0, "sum_abs": 0.0, "n_eff": 0}
    )

    for asset, src, score, mv, _ts in rows:
        a = (asset or "").upper()
        if not a:
            continue
        is_eff = float(score) >= SCORE_MIN and abs(float(mv)) >= MOVE_MIN
        for key in ((a, src or "unknown"), (a, "*")):
            bucket = per_asset[a] if key[1] == "*" else per_src[key]
            bucket["n"] += 1
            if is_eff:
                bucket["eff"] += 1
                bucket["n_eff"] += 1
                bucket["sum_abs"] += abs(float(mv))

    out: List[Tuple[str, str, int, int, float, float, int]] = []
    for (a, src), b in per_src.items():
        if b["n"] == 0:
            continue
        out.append((a, src, b["n"], b["eff"], b["eff"] / b["n"],
                    (b["sum_abs"] / b["n_eff"]) if b["n_eff"] else 0.0, now))
    for a, b in per_asset.items():
        if b["n"] == 0:
            continue
        out.append((a, "*", b["n"], b["eff"], b["eff"] / b["n"],
                    (b["sum_abs"] / b["n_eff"]) if b["n_eff"] else 0.0, now))
    return out


def recompute() -> int:
    conn = _connect()
    try:
        ensure_table(conn)
        rows = compute(conn)
        conn.executemany(
            "INSERT INTO event_effectiveness "
            "(asset, source, n_total, n_effective, hit_rate, avg_move, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(asset, source) DO UPDATE SET "
            "  n_total=excluded.n_total, n_effective=excluded.n_effective, "
            "  hit_rate=excluded.hit_rate, avg_move=excluded.avg_move, "
            "  updated_at=excluded.updated_at",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_asset_stats(asset: str, conn: sqlite3.Connection | None = None) -> Dict[str, Any] | None:
    """读某一资产的总览行（source='*'）。没有表/没有行都返回 None，绝不抛异常。"""
    own = conn is None
    c = conn or _connect(readonly=True)
    try:
        row = c.execute(
            "SELECT n_total, n_effective, hit_rate, avg_move FROM event_effectiveness "
            "WHERE asset=? AND source='*'",
            ((asset or "").upper(),),
        ).fetchone()
        if not row:
            return None
        return {"n_total": row[0], "n_effective": row[1],
                "hit_rate": row[2], "avg_move": row[3]}
    except Exception:
        return None
    finally:
        if own:
            c.close()


def describe(asset: str) -> str:
    """一句话描述某资产上"事件到底有多能推动价格"，供 prompt 注入。"""
    st = get_asset_stats(asset)
    if not st or not st["n_total"]:
        return ""
    pct = st["hit_rate"] * 100
    if pct >= 30:
        grade = "事件驱动型（新闻对价格有明显推动力，链方向可高权重）"
    elif pct >= 10:
        grade = "半事件驱动（多数新闻只是背景，只有强事件才有反应）"
    else:
        grade = "噪音为主（绝大多数新闻不推动价格，链方向需谨慎，别把巧合当因果）"
    return (f"{asset} 事件效力：近 {st['n_total']} 条配对中 {st['n_effective']} 条真正推动价格"
            f"（{pct:.0f}%，有效时平均幅度 {st['avg_move']:.2f}%）→ {grade}")


def _show() -> None:
    conn = _connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT asset, n_total, n_effective, hit_rate, avg_move FROM event_effectiveness "
            "WHERE source='*' ORDER BY hit_rate DESC"
        ).fetchall()
        print(f"{'资产':<10}{'配对':>6}{'有效':>6}{'有效率':>9}{'均幅%':>9}")
        for a, n, e, r, m in rows:
            print(f"{a:<10}{n:>6}{e:>6}{r*100:>8.1f}%{m:>9.2f}")
        if not rows:
            print("(空表 —— 先跑 --recompute)")
    except Exception as exc:  # pragma: no cover
        print(f"读取失败（表可能还没建）：{exc}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="事件效力统计")
    ap.add_argument("--recompute", action="store_true", help="重算并写入 event_effectiveness")
    ap.add_argument("--show", action="store_true", help="打印当前统计")
    args = ap.parse_args()
    if args.recompute:
        n = recompute()
        print(f"[event_effectiveness] 写入 {n} 行")
    if args.show or not args.recompute:
        _show()


if __name__ == "__main__":
    main()
