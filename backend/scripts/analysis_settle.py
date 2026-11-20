#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HOLD 事后结算器：把「观望之后到底有没有行情」回写进账本。

为什么需要它
------------
`forward_pnl` 只有 3 条（都是真下单的），479 条 HOLD 从不结算 →
「准确率」的分母永远是 3，任何改动都无法验证，只能过拟合。
`forward_move_pct` 这一列本来就存在（516 条里只有 43 条有值），
说明管线有这个位置，只是没人在跑。

本脚本只做一件事：给 HOLD 行补 60 分钟事后位移。
**只写 `forward_move_pct`，不碰 `missed_signal`** —— 后者归
`missed_signal_detector` 所有，两个写者同时改同一列，口径必然打架。

口径
----
* 价位：`kline_bars` 的 1 分钟收盘价（只有 BTC/ETH/SOL/XAU 有 1m 线；WTI 只有日线，跳过）
* t0：`ai_decisions.created_at`
* t1：t0 + horizon（默认 60 分钟）
* 缺口容忍：t0/t1 附近 10 分钟内取不到 bar → 该行不结算（宁可空着，不要编数）

安全
----
默认 **dry-run**：只统计「将写入多少条」并打印样本，不改库。
加 `--apply` 才真的 UPDATE，且只更新 `forward_move_pct IS NULL` 的行 —— 不覆盖已有值。

用法
----
    python3 backend/scripts/analysis_settle.py                     # 干跑，看能结算多少
    python3 backend/scripts/analysis_settle.py --days 3 --horizon 60
    python3 backend/scripts/analysis_settle.py --apply              # 真正写库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "backend", "trident_event_bus.db")

SETTLE_ASSETS = ("BTC", "ETH", "SOL", "XAU")
GAP_TOL_MIN = 10


def connect(readonly: bool) -> sqlite3.Connection:
    uri = f"file:{DB_PATH}?mode=ro" if readonly else f"file:{DB_PATH}?mode=rw"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_ts_ms(text: str) -> Optional[int]:
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def close_at(conn: sqlite3.Connection, symbol: str, ts_ms: int) -> Optional[float]:
    """取 ts 之后最近一根 1m 线收盘价；距 ts 超过 GAP_TOL_MIN 分钟视为缺口。"""
    row = conn.execute(
        """SELECT ts, close FROM kline_bars
           WHERE symbol=? AND timeframe='1m' AND ts>=? ORDER BY ts LIMIT 1""",
        (symbol, ts_ms),
    ).fetchone()
    if not row:
        return None
    if row["ts"] - ts_ms > GAP_TOL_MIN * 60_000:
        return None
    return float(row["close"])


def settle_rows(conn: sqlite3.Connection, days: int, horizon: int) -> Tuple[List[Dict[str, Any]], int]:
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    lo_ms = now_ms - days * 86_400_000
    lo_iso = datetime.fromtimestamp(lo_ms / 1000).astimezone().isoformat()

    rows = conn.execute(
        """SELECT id, news_id, target_asset, created_at, suggested_action,
                  original_action, sentiment_score, forward_move_pct
           FROM ai_decisions
           WHERE forward_move_pct IS NULL
             AND target_asset IN ('BTC','ETH','SOL','XAU')
             AND created_at >= ?
           ORDER BY id DESC""",
        (lo_iso,),
    ).fetchall()

    todo: List[Dict[str, Any]] = []
    skipped = 0
    for r in rows:
        t0 = parse_ts_ms(r["created_at"])
        if t0 is None:
            skipped += 1
            continue
        p0 = close_at(conn, r["target_asset"], t0)
        p1 = close_at(conn, r["target_asset"], t0 + horizon * 60_000)
        if not p0 or not p1:
            skipped += 1
            continue
        mv = (p1 - p0) / p0 * 100.0
        todo.append({
            "id": r["id"],
            "asset": r["target_asset"],
            "action": r["suggested_action"],
            "orig": r["original_action"] or "",
            "score": r["sentiment_score"],
            "move_pct": round(mv, 4),
        })
    return todo, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="HOLD 事后结算（回写 forward_move_pct）")
    ap.add_argument("--days", type=int, default=2, help="结算最近 N 天（默认 2）")
    ap.add_argument("--horizon", type=int, default=60, help="事后窗口，分钟（默认 60）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认干跑）")
    a = ap.parse_args()

    conn = connect(readonly=not a.apply)
    todo, skipped = settle_rows(conn, a.days, a.horizon)

    print(f"== HOLD 事后结算 == 窗口={a.horizon} 分钟  最近 {a.days} 天  "
          f"{'写入模式' if a.apply else '干跑模式（不写库）'}")
    print(f"可结算 {len(todo)} 条；缺口/无价跳过 {skipped} 条")

    if todo:
        mv = [t["move_pct"] for t in todo]
        big = [t for t in todo if abs(t["move_pct"]) >= 0.30]
        with_dir = [t for t in todo]
        print(f"位移中位数 {sorted(mv)[len(mv) // 2]:+.3f}%   最大 {max(mv):+.3f}% / {min(mv):+.3f}%")
        print(f"|位移| ≥0.30% 的 {len(big)} 条（{len(big) / len(todo) * 100:.1f}%）"
              f" —— 这些是「观望但事后真有行情」的候选漏单")
        # 方向分非零的行，看事后是否同向（这是唯一能验证 score 有没有用的口径）
        nz = [t for t in todo if t["score"] and abs(t["score"]) > 1e-9]
        if nz:
            hit = [t for t in nz if (t["score"] > 0) == (t["move_pct"] > 0)]
            print(f"方向分非零 {len(nz)} 条，事后同向 {len(hit)} 条（{len(hit) / len(nz) * 100:.1f}%）")
        print("\n前 10 条：")
        print(f"{'id':>6}{'asset':>7}{'act':>6}{'orig':>6}{'score':>8}{'move%':>9}")
        for t in todo[:10]:
            print(f"{t['id']:>6}{t['asset']:>7}{t['action']:>6}{t['orig']:>6}"
                  f"{(t['score'] or 0):>8.3f}{t['move_pct']:>9.3f}")
        _ = with_dir

    if a.apply:
        n = 0
        for t in todo:
            conn.execute(
                "UPDATE ai_decisions SET forward_move_pct=? WHERE id=? AND forward_move_pct IS NULL",
                (t["move_pct"], t["id"]),
            )
            n += 1
        conn.commit()
        print(f"\n已写入 {n} 条 forward_move_pct（未触碰 missed_signal）")
    else:
        print("\n干跑结束。确认口径无误后加 --apply 写入。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
