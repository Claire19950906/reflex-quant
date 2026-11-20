"""shadow_degraded — 记录"分析降级但模型仍给了方向"的决策，做旁路结算（v412-G）。

## 它和 shadow_ledger 的区别

| | shadow_ledger | shadow_degraded（本表） |
|---|---|---|
| 收哪些决策 | `analysis_degraded=0`，真正跑完分析的 | `analysis_degraded=1` 且 reason ∈ summary_only / llm_empty / server_fallback |
| 用途 | 给"判断能力"打分，是**主口径** | 只回答一个问题：**被降级路径丢掉的那些方向，值不值钱** |
| 是否进真实交易 | 否（只是记账） | 否（永远不碰交易，只观测） |
| 是否参与反思统计 | 否 | 否 |

`analysis/contract.py` 记着：实测 summary_only 62 条里 22 条模型给了 BUY/SELL，
其中 21 条被降级路径强制改成 HOLD 后**直接丢弃**——既没执行，也没记账。
于是"丢掉它们是对的吗"这个问题，从来没人算过。本表就是用来算这个的。

放在独立表而不是塞进 shadow_ledger，是因为：
  · shadow_ledger 的主口径必须干净（v400 已明确剔除降级），不能污染；
  · shadow_ledger.prune_invalid() 会主动删除降级行，塞进去会被清掉。

## 取价铁律（上一版踩过的坑）

出场价必须**真的在目标时间附近**：tick 时间与 `entry+horizon` 相差超过
`EXIT_TOL_MIN` 分钟，就判该样本 `no_exit_price` 不记账 —— 不能用"ts<=目标时间的
最近一根"冒充出场价，那会取到陈旧价，让 move 恒为 0，把命中率系统性算错。

用法：
    python3 shadow_degraded_collect.py --days 30 --dry-run
    python3 shadow_degraded_collect.py --days 30 --commit
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "trident_event_bus.db"

TRADABLE = ("BTC", "XAU", "WTI", "ETH", "SOL")
HORIZON_MIN = 240
EXIT_TOL_MIN = 30          # 出场价距目标时间超过 30 分钟 → 陈旧，本样本作废
ENTRY_TOL_MIN = 15         # 入场价同理
# 4 小时内价格变化恰好 < 0.01% 的概率极低；实测这类样本的 tick 间隔都正常（235-248 分钟），
# 说明不是"市场没动"，而是数据源把同一个价反复写进不同 ts（假 tick）。这类不能当样本。
FLAT_PCT = 0.01
DEGRADED_REASONS = ("summary_only", "llm_empty", "server_fallback")

DDL = """
CREATE TABLE IF NOT EXISTS shadow_degraded(
    decision_id INTEGER PRIMARY KEY,
    decided_ts INTEGER NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    degrade_reason TEXT,
    confidence REAL,
    entry_ts INTEGER, entry_price REAL,
    exit_ts INTEGER, exit_price REAL,
    wait_min REAL,          -- 实际等待分钟数（应接近 horizon）
    move_pct REAL,
    hit INTEGER,
    note TEXT,              -- ok / no_entry_price / stale_entry / no_exit_price / stale_exit
    settled_at INTEGER
)
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    conn.commit()


def price_at(conn, asset: str, target_ts: int, tol_min: int):
    """取 target_ts 附近的真实 tick（双向窗口取最接近的），取不到返回 None。

    与 shadow_ledger.nearest_price 的区别：本函数**不允许多新也不允许太旧**，
    避免把 6 小时前的价当成现在的价。
    """
    tol = tol_min * 60
    row = conn.execute(
        "SELECT ts, price FROM market_ticks "
        "WHERE symbol=? AND ts BETWEEN ? AND ? AND price>0 "
        "ORDER BY abs(ts-?) LIMIT 1",
        (asset, target_ts - tol, target_ts + tol, target_ts)).fetchone()
    return row


def collect(conn: sqlite3.Connection, days: int) -> list[dict]:
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    rows = conn.execute(
        "SELECT id, ts, target_asset, ungated_action, evidence_confidence, "
        "       COALESCE(analysis_degraded_reason,'') "
        "  FROM ai_decisions "
        " WHERE COALESCE(analysis_degraded,0)=1 "
        "   AND ungated_action IN ('BUY','SELL') "
        "   AND target_asset IN ({}) "
        "   AND ts >= ? "
        " ORDER BY ts".format(",".join("?" * len(TRADABLE))),
        (*TRADABLE, since)).fetchall()

    out = []
    for did, ts, asset, direction, conf, why in rows:
        if why not in DEGRADED_REASONS:
            continue
        ts = int(ts)
        item = {"decision_id": did, "decided_ts": ts, "asset": asset,
                "direction": direction, "degrade_reason": why,
                "confidence": float(conf or 0.0), "note": "ok"}
        e = price_at(conn, asset, ts, ENTRY_TOL_MIN)
        if not e:
            item["note"] = "no_entry_price"
            out.append(item)
            continue
        item["entry_ts"], item["entry_price"] = int(e[0]), float(e[1])

        target_exit = ts + HORIZON_MIN * 60
        x = price_at(conn, asset, target_exit, EXIT_TOL_MIN)
        if not x:
            item["note"] = "no_exit_price"
            out.append(item)
            continue
        item["exit_ts"], item["exit_price"] = int(x[0]), float(x[1])
        item["wait_min"] = round((x[0] - e[0]) / 60.0, 1)
        move = (float(x[1]) - float(e[1])) / float(e[1]) * 100.0
        if abs(move) < FLAT_PCT:
            item["note"] = "flat_price_suspect"
            item["move_pct"] = round(move, 4)
            out.append(item)
            continue
        item["move_pct"] = round(move, 4)
        item["hit"] = 1 if ((direction == "BUY" and move > 0)
                            or (direction == "SELL" and move < 0)) else 0
        out.append(item)
    return out


def write(conn: sqlite3.Connection, items: list[dict]) -> int:
    ensure(conn)
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for it in items:
        conn.execute(
            """INSERT INTO shadow_degraded
               (decision_id, decided_ts, asset, direction, degrade_reason, confidence,
                entry_ts, entry_price, exit_ts, exit_price, wait_min, move_pct, hit,
                note, settled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(decision_id) DO UPDATE SET
                 exit_ts=excluded.exit_ts, exit_price=excluded.exit_price,
                 wait_min=excluded.wait_min, move_pct=excluded.move_pct,
                 hit=excluded.hit, note=excluded.note, settled_at=excluded.settled_at""",
            (it["decision_id"], it["decided_ts"], it["asset"], it["direction"],
             it["degrade_reason"], it["confidence"],
             it.get("entry_ts"), it.get("entry_price"),
             it.get("exit_ts"), it.get("exit_price"), it.get("wait_min"),
             it.get("move_pct"), it.get("hit"), it["note"],
             now if it.get("hit") is not None else None))
        n += 1
    conn.commit()
    return n


def report(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT degrade_reason, direction, hit FROM shadow_degraded "
        "WHERE hit IS NOT NULL").fetchall()
    if not rows:
        print("  暂无可结算样本")
        return
    n = len(rows)
    wins = sum(r[2] for r in rows)
    print(f"  可结算 {n} 条 · 命中率 {wins / n * 100:.1f}%")
    agg: dict = {}
    for why, d, h in rows:
        a = agg.setdefault(why, [0, 0])
        a[0] += 1
        a[1] += h
    for why, (nn, w) in sorted(agg.items(), key=lambda x: -x[1][0]):
        print(f"    {why}: n={nn} 命中 {w / nn * 100:.1f}%")
    for d in ("BUY", "SELL"):
        sub = [r for r in rows if r[1] == d]
        if sub:
            print(f"    {d}: n={len(sub)} 命中 {sum(x[2] for x in sub) / len(sub) * 100:.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=90)
    try:
        conn.execute("PRAGMA busy_timeout=90000")
        items = collect(conn, args.days)
        ok = [i for i in items if i["note"] == "ok"]
        print(f"降级方向样本 {len(items)} 条 → 可结算 {len(ok)} 条；"
              f"缺价丢弃 {len(items) - len(ok)} 条")
        for i in items:
            if i["note"] != "ok":
                print(f"  #{i['decision_id']} {i['asset']} {i['direction']} "
                      f"[{i['degrade_reason']}] → {i['note']}")
        if ok:
            n = len(ok)
            print(f"命中率 {sum(i['hit'] for i in ok) / n * 100:.1f}% · "
                  f"平均 move {sum(i['move_pct'] for i in ok) / n:+.3f}%")

        if args.commit:
            written = write(conn, items)
            print(f"已写入 {written} 条 → shadow_degraded（不入真实交易、不入反思统计）")
            print("全表统计：")
            report(conn)
        else:
            print("[dry-run] 未写库。加 --commit 生效。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
