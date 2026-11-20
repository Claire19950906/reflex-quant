# -*- coding: utf-8 -*-
"""纸面回放器：把**真实历史信号**按**真实历史行情**结算成纸面样本。

为什么需要它：
    这套库的方向性信号很稀疏（ai_decisions 504 条里只有 3 条 BUY/SELL，
    quick_sim_trades 只有 3 笔），于是策略健康度（F45）、PnL 归因（F12）、
    置信度校准（F59）全都卡在「样本不足」。等实盘慢慢攒太慢，而**回放历史**是
    唯一不编造数据的补法。

口径（每一笔都能追回去）：
    - 信号来源：`decision_replay` = ai_decisions 的 BUY/SELL（系统自己的决策）；
                `news_replay`     = news_events 的 BUY/SELL（新闻层方向标签）。
    - 入场价：事件 ts 之后第一根 1m K 线收盘价（真实成交过的价格带）。
    - 出场价：入场后 horizon_min（默认 20 分钟）的 1m 收盘。
    - 成本：cost_bps=30（与 quick_sim_trades 的 cost_bps 口径一致）。
    - 缺 K 线就不回放，绝不补价、不插值。

两种来源的权重不同，所以结果里必须分开报，不能混成一个「胜率」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import store

DEFAULT_HORIZON_MIN = 20
DEFAULT_COST_BPS = 30.0


def _bars(conn, asset: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe='1m' "
        "AND ts>=? AND ts<=? ORDER BY ts",
        (asset, start_ts * 1000, end_ts * 1000),
    ).fetchall()
    out = []
    for r in rows:
        ts = int(r[0])
        if ts > 10_000_000_000:
            ts //= 1000
        out.append({"ts": ts, "close": r[1]})
    return out


def _settle(conn, asset: str, ts: int, horizon_min: int = DEFAULT_HORIZON_MIN,
            cost_bps: float = DEFAULT_COST_BPS) -> Optional[Dict[str, Any]]:
    """用 1m K 线结算一笔：入场=事件后首根收盘，出场=+horizon 分钟收盘。"""
    bars = _bars(conn, asset, ts, ts + (horizon_min + 5) * 60)
    if not bars:
        return None
    entry_bar = bars[0]
    target = entry_bar["ts"] + horizon_min * 60
    exit_bar = next((b for b in bars if b["ts"] >= target), None)
    if exit_bar is None:
        return None
    return {"entry_ts": entry_bar["ts"], "entry_price": float(entry_bar["close"]),
            "exit_ts": exit_bar["ts"], "exit_price": float(exit_bar["close"])}


def _pnl(action: str, entry: float, exit_: float, cost_bps: float) -> float:
    direction = 1.0 if action.upper() == "BUY" else -1.0
    gross = direction * (exit_ - entry) / entry * 100.0
    return gross - cost_bps / 100.0


def replay_news(conn, limit: int = 300, horizon_min: int = DEFAULT_HORIZON_MIN,
                cost_bps: float = DEFAULT_COST_BPS, now: Optional[int] = None,
                include_noise: bool = False) -> Dict[str, Any]:
    """回放新闻层方向标签（真实新闻 ID + 真实 1m 行情）。

    默认剔除 `is_noise=1`：噪声标签本身不该算成策略信号，算进去会把胜率评低。
    """
    rows = conn.execute(
        "SELECT id, news_id, ts, asset, action, score FROM news_events "
        "WHERE action IN ('BUY','SELL') AND asset IS NOT NULL AND asset NOT IN ('NONE','') "
        "AND ts IS NOT NULL" + ("" if include_noise else " AND COALESCE(is_noise,0)=0") +
        " ORDER BY ts DESC LIMIT ?", (int(limit),),
    ).fetchall()
    inserted = skipped = 0
    for r in rows:
        asset = str(r["asset"]).upper() if hasattr(r, "keys") else str(r[3]).upper()
        news_id = r["news_id"] if hasattr(r, "keys") else r[1]
        ts = int(r["ts"] if hasattr(r, "keys") else r[2])
        if ts > 10_000_000_000:
            ts //= 1000
        action = (r["action"] if hasattr(r, "keys") else r[4]) or ""
        score = r["score"] if hasattr(r, "keys") else r[5]
        s = _settle(conn, asset, ts, horizon_min, cost_bps)
        if not s:
            skipped += 1
            continue
        pnl = _pnl(action, s["entry_price"], s["exit_price"], cost_bps)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO t2_sim_trades "
                "(source, news_id, asset, action, confidence, entry_ts, entry_price, exit_ts, "
                " exit_price, horizon_min, pnl_pct, cost_bps, settled, created_at) "
                "VALUES ('news_replay',?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (str(news_id), asset, action.upper(), _conf(score), s["entry_ts"],
                 s["entry_price"], s["exit_ts"], s["exit_price"], horizon_min, pnl,
                 cost_bps, now or s["exit_ts"]),
            )
            inserted += 1
        except Exception:
            skipped += 1
    conn.commit()
    return {"source": "news_replay", "candidates": len(rows), "inserted": inserted, "skipped": skipped}


def replay_decisions(conn, limit: int = 100, horizon_min: int = DEFAULT_HORIZON_MIN,
                     cost_bps: float = DEFAULT_COST_BPS, now: Optional[int] = None) -> Dict[str, Any]:
    """回放系统自己的方向性决策（ai_decisions BUY/SELL）。"""
    rows = conn.execute(
        "SELECT id, created_at, target_asset, suggested_action, evidence_confidence, strategy_id "
        "FROM ai_decisions WHERE suggested_action IN ('BUY','SELL') "
        "AND target_asset IS NOT NULL ORDER BY created_at DESC LIMIT ?", (int(limit),),
    ).fetchall()
    inserted = skipped = 0
    for r in rows:
        asset = str(r["target_asset"]).upper()
        ts = _parse_ts(r["created_at"])
        if not ts:
            skipped += 1
            continue
        s = _settle(conn, asset, ts, horizon_min, cost_bps)
        if not s:
            skipped += 1
            continue
        action = r["suggested_action"].upper()
        pnl = _pnl(action, s["entry_price"], s["exit_price"], cost_bps)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO t2_sim_trades "
                "(source, decision_id, asset, action, confidence, entry_ts, entry_price, exit_ts, "
                " exit_price, horizon_min, pnl_pct, cost_bps, strategy_id, settled, created_at) "
                "VALUES ('decision_replay',?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (int(r["id"]), asset, action, _conf(r["evidence_confidence"]), s["entry_ts"],
                 s["entry_price"], s["exit_ts"], s["exit_price"], horizon_min, pnl,
                 cost_bps, str(r["strategy_id"]) if r["strategy_id"] is not None else None,
                 now or s["exit_ts"]),
            )
            inserted += 1
        except Exception:
            skipped += 1
    conn.commit()
    return {"source": "decision_replay", "candidates": len(rows), "inserted": inserted, "skipped": skipped}


def _conf(v: Any) -> Optional[float]:
    """置信度统一压到 0–1；新闻 score 可能带符号，取绝对值。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(min(abs(f), 1.0), 4)


def _parse_ts(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n / 1000) if n > 10_000_000_000 else int(n)
    s = str(v).strip()
    if s.isdigit():
        n = float(s)
        return int(n / 1000) if n > 10_000_000_000 else int(n)
    import datetime as _dt
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(_dt.datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def replay_all(conn, news_limit: int = 300, decision_limit: int = 100,
               horizon_min: int = DEFAULT_HORIZON_MIN,
               cost_bps: float = DEFAULT_COST_BPS, now: Optional[int] = None,
               include_noise: bool = False) -> Dict[str, Any]:
    a = replay_decisions(conn, decision_limit, horizon_min, cost_bps, now)
    b = replay_news(conn, news_limit, horizon_min, cost_bps, now, include_noise)
    total = conn.execute("SELECT COUNT(*) FROM t2_sim_trades").fetchone()[0]
    settled = conn.execute("SELECT COUNT(*) FROM t2_sim_trades WHERE settled=1").fetchone()[0]
    return {"decision_replay": a, "news_replay": b, "total": total, "settled": settled}
