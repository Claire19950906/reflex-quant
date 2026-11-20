"""校准门卫 —— 用**事后真实结果**给每条决策打分, 不再信模型自评。

为什么现在的门卫是瞎的:
  统一置信度 = |模型方向分| × 各种乘子。而模型方向分在 94% 的情况下是 0
  (它自己说"观望"), 六个乘子又全部坍缩 —— 结果这个"分数"对所有单都是 0.0,
  没有任何排序能力。所谓"门槛 20"实际是在拦一堆 0 分, 跟质量无关。

正确的门卫该看什么(全部可复算, 不靠模型自夸):
  1. 这个品种 × 这个方向, 历史上到底准不准   ← shadow_ledger 结算结果
  2. 新闻出来前, 市场是否已经朝这个方向动了   ← market_ticks 前 30 分钟
  3. 历史样本有多少(少就收缩回 50%, 不让小样本吹牛)

做法: 按 (资产, 方向, 是否已确认) 分格, 用贝叶斯收缩算每格的历史命中率,
作为"预测准确概率"。**按时间切分**验证: 前 70% 决策当训练, 后 30% 当考试 ——
考试那部分的数据在训练时完全没见过, 才算真本事。

用法:
  python3 gate_calibrator.py --days 40            # 训练+考试, 出报告
  python3 gate_calibrator.py --days 40 --commit   # 把分数写回 shadow_ledger
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import datetime
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 收缩强度: 一个格子要攒到这么多条, 历史命中率才被"当真"。
# 20 = 少于 20 条时, 结论往 50% 拉, 防止 3 条全中就当 100% 用。
SHRINK_N = 20.0

CONFIRM_WINDOW_SEC = 1800      # 看决策前 30 分钟
CONFIRM_MOVE_PCT = 0.0         # 只要同向就算"已确认"(方向对, 幅度另说)


def _connect(readonly: bool) -> sqlite3.Connection:
    # v337: 生产库 1.4G + engine/ai_worker 持续写入, 默认 5s 锁等待太短 ——
    # wrapper 里出现过 "database is locked" 把整个校准作业打断。给到 30s。
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


def pre_move_align(conn: sqlite3.Connection, asset: str, ts: int,
                   direction: str) -> int | None:
    """决策前 30 分钟, 价格是否已朝 direction 走。1=已同向 / 0=未同向 / None=没数据。"""
    now_p = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    old_p = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, ts - CONFIRM_WINDOW_SEC)).fetchone()
    if not now_p or not old_p or not old_p[0]:
        return None
    move = (now_p[0] - old_p[0]) / old_p[0]
    if move == 0:
        return 0
    return 1 if ((direction == "BUY") == (move > 0)) else 0


def load_samples(conn: sqlite3.Connection, days: int) -> list[dict]:
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    rows = conn.execute(
        "SELECT decision_id, decided_ts, asset, direction, confidence, hit "
        "  FROM shadow_ledger WHERE decided_ts >= ? AND hit IS NOT NULL",
        (since,)).fetchall()
    out = []
    for did, ts, asset, direction, conf, hit in rows:
        conf_flag = pre_move_align(conn, asset, int(ts), direction)
        out.append({
            "decision_id": did, "ts": int(ts), "asset": asset,
            "direction": direction, "confidence": float(conf or 0),
            "hit": int(hit), "confirm": conf_flag,
        })
    return out


def _prob(hits: int, n: int) -> float:
    """贝叶斯收缩后的命中概率: 样本少就往 0.5 拉, 不让小样本吹牛。"""
    return (hits + SHRINK_N * 0.5) / (n + SHRINK_N)


def train(rows: list[dict]) -> dict:
    """按格子统计历史命中率。格子 = (资产, 方向, 是否已确认)。"""
    cells: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        key = (r["asset"], r["direction"], r["confirm"])
        cells[key][0] += r["hit"]
        cells[key][1] += 1
    # 粗一点的兜底格子: (资产, 方向) 和全局 —— 细格子样本不够时降级
    asset_dir: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    asset_only: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    glob: list[int] = [0, 0]
    for r in rows:
        for d, k in ((asset_dir, (r["asset"], r["direction"]),),
                     (asset_only, r["asset"],)):
            d[k][0] += r["hit"]
            d[k][1] += 1
        glob[0] += r["hit"]
        glob[1] += 1
    return {"cells": dict(cells), "asset_dir": dict(asset_dir),
            "asset_only": dict(asset_only), "glob": glob}


def score(row: dict, model: dict) -> dict:
    """给一条决策算"预测命中概率", 并把每一部分的来源写清楚(不黑箱)。"""
    key = (row["asset"], row["direction"], row["confirm"])
    if key in model["cells"] and model["cells"][key][1] >= 8:
        h, n = model["cells"][key]
        return {"p": round(_prob(h, n), 4), "basis": f"格 {key} n={n}", "n": n}
    key2 = (row["asset"], row["direction"])
    if key2 in model["asset_dir"] and model["asset_dir"][key2][1] >= 8:
        h, n = model["asset_dir"][key2]
        return {"p": round(_prob(h, n), 4), "basis": f"品种方向 {key2} n={n}", "n": n}
    h, n = model["glob"]
    return {"p": round(_prob(h, n), 4), "basis": f"全局 n={n}", "n": n}


def evaluate(rows: list[dict], model: dict, thresholds=(0.50, 0.52, 0.55, 0.58)) -> None:
    scored = [(r, score(r, model)) for r in rows]
    n = len(scored)
    if n == 0:
        print("没有可评估样本")
        return
    base = sum(r["hit"] for r, _ in scored) / n
    print(f"\n【考试集】样本 {n} 条 · 不设门卫时命中率 {100*base:.1f}%")
    print(f"{'门卫门槛':>8} {'放行条数':>8} {'覆盖率':>7} {'命中率':>8} {'结论':>6}")
    for th in thresholds:
        sub = [(r, s) for r, s in scored if s["p"] >= th]
        if not sub:
            print(f"{th:>8.2f} {0:>8} {'0.0%':>7} {'—':>8} {'空':>6}")
            continue
        hit = sum(r["hit"] for r, _ in sub)
        lift = 100 * hit / len(sub) - 100 * base
        verdict = "↑提升" if lift >= 3 else ("≈持平" if lift >= -3 else "↓变差")
        print(f"{th:>8.2f} {len(sub):>8} {100*len(sub)/n:>6.1f}% "
              f"{100*hit/len(sub):>7.1f}% {verdict:>6}")


def write_scores(conn: sqlite3.Connection, rows: list[dict], model: dict) -> int:
    """把校准分数写回 shadow_ledger.gate_score (只加列, 不改历史语义)。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shadow_ledger)")]
    if "gate_score" not in cols:
        conn.execute("ALTER TABLE shadow_ledger ADD COLUMN gate_score REAL")
    if "gate_basis" not in cols:
        conn.execute("ALTER TABLE shadow_ledger ADD COLUMN gate_basis TEXT")
    if "pre_confirm" not in cols:
        conn.execute("ALTER TABLE shadow_ledger ADD COLUMN pre_confirm INTEGER")
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for r in rows:
        s = score(r, model)
        conn.execute(
            "UPDATE shadow_ledger SET gate_score=?, gate_basis=?, pre_confirm=? "
            "WHERE decision_id=?", (s["p"], s["basis"], r["confirm"], r["decision_id"]))
        n += 1
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_gate ON shadow_ledger(gate_score)")
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    conn = _connect(not a.commit)
    rows = load_samples(conn, a.days)
    if not rows:
        print("shadow_ledger 没有结算样本, 先跑 shadow_ledger.py --commit")
        return 1
    rows.sort(key=lambda r: r["ts"])
    cut = int(len(rows) * a.train_ratio)
    train_rows, test_rows = rows[:cut], rows[cut:]
    model = train(train_rows)
    print(f"训练 {len(train_rows)} 条 (前 {int(a.train_ratio*100)}% 时间) · "
          f"考试 {len(test_rows)} 条 (后段, 训练时没见过)")

    # 对照组: 用现在线上那个"模型自评置信度"当门卫, 看它在考试集上表现如何
    print("\n=== 对照组: 现在线上的置信度门卫 ===")
    for th in (0.0, 5.0, 10.0, 20.0):
        sub = [r for r in test_rows if r["confidence"] >= th]
        if sub:
            print(f"  门槛 {th:>4.0f} → 放行 {len(sub):>3} 条, 命中 "
                  f"{100*sum(r['hit'] for r in sub)/len(sub):.1f}%")

    evaluate(test_rows, model)
    if a.commit:
        n = write_scores(conn, rows, model)
        conn.close()
        print(f"\n已把校准分数写回 {n} 条 shadow_ledger 记录")
    else:
        conn.close()
        print("\n[dry-run] 未写库。加 --commit 落库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
