#!/usr/bin/env python3
"""signal_performance 结算器：把每条决策的真实前向盈亏 / MFE / MAE 落库。

为什么要有它：
    signal_performance 这张表 **建好了但从来没有写入者**（实测 0 行）：
    字段齐全（is_correct / forward_pnl / mfe_pct / mae_pct），
    上游 db.py 的 baseline 聚合（_agg(1)/_agg(4)）一直在读它，读到的是空集。
    结果是：
      · 只有"命中率"这种非黑即白的指标，
      · 完全没有 MFE/MAE —— 而这两个数是**唯一能回答"止盈止损该设几个点"**的数据。

关键设计（为什么不只结算 BUY/SELL）：
    实测 327 条决策里 **325 条是 HOLD**。只结算那 2 笔 BUY 等于没做。
    所以 HOLD 也落行：is_correct 写 'HOLD'（占位，下游 baseline 显式排除它，
    不会污染胜率），但 **forward_pnl / MFE / MAE 照算** ——
    这样才能回答"HOLD 到底是稳健，还是错过了一波行情"。

口径：
    窗口 W = 4 小时（240 分钟，与下游 _agg(4) 对齐）
    入场价 = 决策 ts 之后第一根（容忍 15 分钟内）
    出场价 = ts+W 附近那根（容忍 ±10 分钟，**取不到就不写，绝不拿相邻 K 线冒充**）
    pnl%   = (exit/entry - 1) * 100 * dir     dir: BUY=+1 / SELL=-1 / HOLD=0
    MFE/MAE= 窗口内按 dir 归一的最大有利 / 最大不利偏移%
             （HOLD 用 dir=0，即原始价格位移，用来衡量"错过了多少"）

用法：
    python3 signal_performance_settler.py            # 试运行，只报覆盖率
    python3 signal_performance_settler.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# 脚本位于 signals/ 子目录；wrapper 从 src_python 根目录调用 signals/xxx.py，
# 需把父目录加进 sys.path 才能 import db / db_retry（否则 ModuleNotFoundError）。
_SRC_PY = Path(__file__).resolve().parent.parent
if str(_SRC_PY) not in sys.path:
    sys.path.insert(0, str(_SRC_PY))

import db
import db_retry

W_SEC = 240 * 60          # 4 小时
ENTRY_TOL = 30 * 60       # 入场容忍 30 分钟
EXIT_TOL = 30 * 60        # 出场容忍 30 分钟（取不到就跳过，绝不拿相邻 K 线冒充）
MIN_SPAN_RATIO = 0.85     # 真实跨度必须 >= 85% 窗口，否则视作"测不到"


def _series(conn, asset: str, t0: int, t1: int):
    """取窗口内 (ts, high, low, close)，按 ts 升序。优先 ohlcv_intraday。"""
    rows = conn.execute(
        "SELECT ts, high, low, close FROM ohlcv_intraday "
        "WHERE asset=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
        (asset, t0, t1),
    ).fetchall()
    if rows:
        return rows
    # 兜底 kline_bars（ts 是毫秒）
    rows = conn.execute(
        "SELECT ts/1000 AS ts, high, low, close FROM kline_bars "
        "WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
        (asset, t0 * 1000, t1 * 1000),
    ).fetchall()
    return rows


def _settle_one(conn, row):
    did, asset, dts, action = row
    dirn = {"BUY": 1.0, "SELL": -1.0}.get(action, 0.0)

    series = _series(conn, asset, dts, dts + W_SEC + EXIT_TOL)
    if len(series) < 3:
        return None, "no_data"

    # 入场：决策 ts 之后第一根
    entry = None
    for ts, _h, _l, close in series:
        if ts >= dts:
            if ts - dts > ENTRY_TOL:
                return None, "entry_too_late"
            entry = (ts, float(close))
            break
    if not entry:
        return None, "no_entry"

    target = dts + W_SEC
    # 出场：锁到 target 附近那根（不许拿相邻 K 线冒充整段）
    exit_px = None
    for ts, _h, _l, close in reversed(series):
        if ts <= target + EXIT_TOL:
            if abs(ts - target) <= EXIT_TOL:
                exit_px = (ts, float(close))
            break
    if not exit_px:
        return None, "exit_too_far"
    # 跨度闸门：出场那根离决策太近（比如只有 1 小时）→ 测的是别的时段，不算
    if (exit_px[0] - entry[0]) < W_SEC * MIN_SPAN_RATIO:
        return None, "span_too_short"

    e_px = entry[1]
    if e_px <= 0:
        return None, "bad_entry"

    win = [r for r in series if entry[0] <= r[0] <= exit_px[0]]
    if not win:
        return None, "empty_window"

    # ── 冻结报价闸门（v343）──────────────────────────────────────────
    # 实测 1 分钟线里有大量"与前一根同价"：XAU 24.1%、NVDA 19.0%
    # （最长连续 959 根 ≈ 16 小时）。窗口被冻住时 MFE=MAE=0，
    # 会被误读成"这段完全没波动"，进而把止盈止损标定得过窄。
    # 这里只要窗口内同价占比 >50% 就判"测不到"，不写库。
    frozen = sum(1 for i in range(1, len(win)) if win[i][3] == win[i - 1][3])
    frozen_ratio = frozen / max(1, len(win) - 1)
    if frozen_ratio > 0.5:
        return None, "frozen_window"

    hi = max(float(r[1]) for r in win)
    lo = min(float(r[2]) for r in win)
    # 整窗零振幅（high==low）→ 这条 K 线序列是冻结的，MFE/MAE 写出来就是 0.0，
    # 会被误读成"这段时间完全没波动"（实测 NVDA 7280 根里有 1415 根 high==low）。
    if hi <= lo:
        return None, "no_range"
    if dirn >= 0:
        mfe = (hi / e_px - 1) * 100
        mae = (lo / e_px - 1) * 100
    else:
        mfe = (e_px / lo - 1) * 100
        mae = (e_px / hi - 1) * 100
    if action not in ("BUY", "SELL"):
        mfe = abs((hi / e_px - 1) * 100)
        mae = -abs((lo / e_px - 1) * 100)

    # HOLD 的方向乘子是 0：若直接乘，位移会被乘成 0.0（实测 58 行全 0，
    # 把"错过了多少"这个核心指标算没了）。HOLD 保留**原始位移**，
    # 用 |forward_pnl| 衡量错过幅度。
    raw = (exit_px[1] / e_px - 1) * 100
    pnl = raw * dirn if action in ("BUY", "SELL") else raw
    if action in ("BUY", "SELL"):
        verdict = "WIN" if pnl > 0 else "LOSS"
    else:
        verdict = "HOLD"   # 占位：下游 baseline 显式排除，不影响胜率

    return {
        "decision_id": did, "asset": asset, "ts": exit_px[0], "action": action,
        "frozen_ratio": round(frozen_ratio, 3),
        "is_correct": verdict,
        "forward_pnl": round(pnl, 4),
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
    }, "ok"


def settle(conn: sqlite3.Connection, *, apply: bool = False, limit: int = 0,
           prune: bool = False, now: int | None = None) -> dict:
    """可复用的结算主逻辑（不打印、不自己建连接）。

    抽出来的原因：原来所有逻辑都塞在 main() 里，只能靠命令行跑生产库验证；
    离线自测 / 数据回放脚本没法复用同一段计算，只能各写一套 —— 那等于测的不是
    线上那条路径。现在 main() 只是这层的壳。
    """
    # ai_decisions.ts 是整型 unix 秒（实测 typeof=integer），不要对它套 strftime：
    # 之前用 strftime('%s', substr(ts,...)) 把整型当字符串切，234 条全部 bad_ts。
    sql = ("SELECT id, UPPER(COALESCE(target_asset,'')), "
           "CAST(COALESCE(ts, 0) AS INTEGER), "
           "UPPER(COALESCE(suggested_action,'HOLD')) FROM ai_decisions "
           "WHERE UPPER(COALESCE(target_asset,'')) NOT IN ('', 'NONE') "
           "ORDER BY id")
    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]

    now = int(time.time()) if now is None else int(now)
    stats: dict[str, int] = {}
    payload = []
    for r in rows:
        if not r[2]:
            stats["bad_ts"] = stats.get("bad_ts", 0) + 1
            continue
        if r[2] + W_SEC + EXIT_TOL > now:
            stats["window_not_reached"] = stats.get("window_not_reached", 0) + 1
            continue
        rec, why = _settle_one(conn, r)
        stats[why] = stats.get(why, 0) + 1
        if rec:
            payload.append(rec)

    pruned = 0
    if prune:
        def _p(cur):
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "DELETE FROM signal_performance WHERE COALESCE(mfe_pct,0)=0 "
                "AND COALESCE(mae_pct,0)=0 AND COALESCE(forward_pnl,0)=0")
            n = cur.total_changes
            cur.execute("COMMIT")
            return n
        pruned = db_retry.run_write(conn, _p)

    written = 0
    if apply and payload:
        def _w(cur):
            # isolation_level=None = autocommit：不显式 BEGIN，最后那句 COMMIT 会报
            # "cannot commit - no transaction is active"。这里显式开事务。
            cur.execute("BEGIN IMMEDIATE")
            cur.executemany(
                "INSERT INTO signal_performance "
                "(decision_id, asset, ts, action, is_correct, forward_pnl, mfe_pct, mae_pct) "
                "VALUES (:decision_id,:asset,:ts,:action,:is_correct,:forward_pnl,:mfe_pct,:mae_pct) "
                "ON CONFLICT(decision_id, ts) DO UPDATE SET "
                "is_correct=excluded.is_correct, forward_pnl=excluded.forward_pnl, "
                "mfe_pct=excluded.mfe_pct, mae_pct=excluded.mae_pct",
                payload,
            )
            cur.execute("COMMIT")
        db_retry.run_write(conn, _w)
        written = len(payload)

    return {"candidates": len(rows), "stats": stats, "payload": payload,
            "written": written, "pruned": pruned}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--prune", action="store_true",
                    help="删除历史中 mfe=mae=pnl 全 0 的伪测量行")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=20000")

    out = settle(conn, apply=args.apply, limit=args.limit, prune=args.prune)
    payload = out["payload"]

    print(f"候选决策 {out['candidates']} 条 → 可结算 {len(payload)} 条")
    for k, v in sorted(out["stats"].items()):
        print(f"  {k}: {v}")

    if payload:
        from collections import Counter
        print("  is_correct 分布:", dict(Counter(p["is_correct"] for p in payload)))
        pf = [p["forward_pnl"] for p in payload if p["action"] in ("BUY", "SELL")]
        if pf:
            print(f"  BUY/SELL 前向 pnl: min {min(pf):.2f}% / max {max(pf):.2f}%")

    if args.prune:
        print(f"已清理零振幅伪测量 {out['pruned']} 行")

    if out["written"]:
        print(f"已写入 {out['written']} 行")
    elif payload:
        print("\n(试运行，未写库 — 加 --apply 生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
