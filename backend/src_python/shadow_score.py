# -*- coding: utf-8 -*-
"""shadow_score.py — 决策级"影子打分"（不依赖开仓，1h/4h/24h 出结果）

为什么需要（v488）
----------------
现有真值链路 `signals/decision_truth.py` 用 `expected_horizon` 定持有期，
而当前决策几乎全是 `1w+` → 一条决策要等 **7 天** 才判得出胜负。
后果很具体：`game` 的 `strategy_reflections` / `beliefs` / `gate_calibration`
全是 0 行或 `applied=0` —— 不是代码坏了，是**根本还没有结果可学**。

影子打分的口径与真值链路一致（起点=决策时刻之前最近 K 线收盘，
终点=决策时刻+持有期之后最近 K 线收盘），只把持有期换成短窗口，
并且**不写 ai_decisions**（不动 settled / is_correct / forward_pnl 那些
"模拟盘口径"字段，避免把"没开仓"和"方向错了"混成一件事）。

口径声明
--------
· 只打 BUY/SELL：HOLD 没有方向，判不出胜负（HOLD 的市场走势另算）
· 标的为 NONE / 空 的决策不参与（本来就没有可交易标的）
· 未到期（t0 + 窗口 > now）返回 None，**绝不拿未来数据凑数**
· 成本：默认扣 8bps 往返（`SHADOW_COST_BPS` 可调），报 net 而不是纯毛收益
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from signals.decision_truth import KlineIndex, breakeven_pct, derive_verdict, signed_move

# (窗口秒数, 标签)
HORIZONS: Tuple[Tuple[int, str], ...] = (
    (3600, "1h"),
    (4 * 3600, "4h"),
    (24 * 3600, "24h"),
)

SHADOW_COST_BPS = 8.0   # 往返成本假设（与 quick_sim 默认同量级）

DDL = """
CREATE TABLE IF NOT EXISTS shadow_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER NOT NULL,
    asset         TEXT,
    action        TEXT,
    horizon_sec   INTEGER NOT NULL,
    entry_ts      INTEGER,
    entry_price   REAL,
    exit_price    REAL,
    move_pct      REAL,
    signed_move   REAL,
    net_move      REAL,
    verdict       TEXT,
    code_fingerprint TEXT DEFAULT '',
    created_at    INTEGER,
    UNIQUE(decision_id, horizon_sec)
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    conn.commit()


def _due_rows(conn: sqlite3.Connection, days_back: int, limit: int) -> List[Tuple]:
    since = int(time.time()) - days_back * 86400
    try:
        return conn.execute(
            "SELECT id, target_asset, suggested_action, ts, code_fingerprint "
            "FROM ai_decisions "
            "WHERE suggested_action IN ('BUY','SELL') AND ts >= ? "
            "AND UPPER(COALESCE(target_asset,'')) NOT IN ('', 'NONE') "
            "ORDER BY id DESC LIMIT ?",
            (since, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []


def _existing(conn: sqlite3.Connection) -> set:
    try:
        return {(int(r[0]), int(r[1])) for r in conn.execute(
            "SELECT decision_id, horizon_sec FROM shadow_scores")}
    except sqlite3.Error:
        return set()


def compute_scores(
    conn: sqlite3.Connection,
    now_ts: Optional[int] = None,
    days_back: int = 10,
    limit: int = 5000,
    index: Optional[KlineIndex] = None,
) -> List[Dict[str, Any]]:
    """算出所有已到期、且还没打过分的结果（幂等，不写库）。"""
    now = int(now_ts or time.time())
    idx = index if index is not None else KlineIndex(conn)
    done = _existing(conn)
    thr = breakeven_pct()
    out: List[Dict[str, Any]] = []
    for did, asset, action, ts, fp in _due_rows(conn, days_back, limit):
        if not ts:
            continue
        for hsec, _label in HORIZONS:
            if (int(did), int(hsec)) in done:
                continue
            t0 = int(ts) * 1000
            t1 = t0 + hsec * 1000
            if t1 > now * 1000:
                continue                      # 未到期，不能用未来数据
            pe = idx.price_at(asset, t0)
            px = idx.price_at(asset, t1)
            if not pe or not px:
                continue                      # K 线不够 → 跳过，不猜
            p0, p1 = float(pe[0]), float(px[0])
            if p0 <= 0:
                continue
            mv = (p1 - p0) / p0 * 100.0
            sm = signed_move(action, mv)
            if sm is None:
                continue
            net = sm - SHADOW_COST_BPS / 100.0
            out.append({
                "decision_id": int(did),
                "asset": str(asset).upper(),
                "action": str(action).upper(),
                "horizon_sec": int(hsec),
                "entry_ts": int(ts),
                "entry_price": round(p0, 6),
                "exit_price": round(p1, 6),
                "move_pct": round(mv, 4),
                "signed_move": round(sm, 4),
                "net_move": round(net, 4),
                "verdict": derive_verdict(action, mv, thr),
                "code_fingerprint": str(fp or ""),
            })
    return out


def persist(conn: sqlite3.Connection, rows: Sequence[Dict[str, Any]]) -> int:
    n = 0
    now = int(time.time())
    for r in rows:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO shadow_scores "
                "(decision_id, asset, action, horizon_sec, entry_ts, entry_price, "
                " exit_price, move_pct, signed_move, net_move, verdict, "
                " code_fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["decision_id"], r["asset"], r["action"], r["horizon_sec"],
                 r["entry_ts"], r["entry_price"], r["exit_price"], r["move_pct"],
                 r["signed_move"], r["net_move"], r["verdict"],
                 r["code_fingerprint"], now),
            )
            n += 1
        except sqlite3.Error:
            continue
    if n:
        conn.commit()
    return n


def summarize(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (指纹, 窗口) 汇总：样本、胜、均值净收益。"""
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r.get("code_fingerprint") or "(未标注)", int(r["horizon_sec"]))
        groups.setdefault(key, []).append(r)
    out = []
    for (fp, hsec), rs in sorted(groups.items(), key=lambda kv: (-kv[0][1], kv[0][0])):
        n = len(rs)
        wins = sum(1 for r in rs if r.get("verdict") == "WIN")
        losses = sum(1 for r in rs if r.get("verdict") == "LOSS")
        avg_net = sum(float(r.get("net_move") or 0) for r in rs) / n if n else 0.0
        out.append({
            "fingerprint": fp, "horizon_sec": hsec, "n": n,
            "win": wins, "loss": losses,
            "win_rate": (wins / (wins + losses)) if (wins + losses) else None,
            "avg_net_pct": round(avg_net, 4),
        })
    return out


__all__ = ["HORIZONS", "SHADOW_COST_BPS", "ensure_schema", "compute_scores",
           "persist", "summarize"]
