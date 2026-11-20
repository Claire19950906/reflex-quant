#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_reverse_lookup.py — 「时刻 → 事件」反向检索（v490 P1-2 · 需求 #6）

为什么需要
----------
现有归因是「事件 → 异动」：给定一条新闻，在时间窗内找它解释了哪波行情。
需求 #6 反过来：**给定某个时刻 + 某个金融产品，是什么事件影响了它的涨跌？**
此前 reverse_attributions 存的是异动点的技术因子（背离/KDJ/十字星），
不是事件；也没有「按时刻反查事件」的入口。

本模块做纯只读检索，口径与归因器一致：
  · 事件取 news_events（标题/方向/标的/时间/来源/分数）；
  · 行情取 critical_price_moves（涨跌幅度/快慢形态/持续时长）；
  · 时间窗默认 [-3h, +30min]：事件应先于或紧邻异动，晚于异动超过 30 分钟
    不视作起因（事后拟合）；
  · 已有的 event_move_attribution 归因结果作为「已配对事件」优先标注；
  · 资金面快照（OI/多空比/费率/量能）作为同期佐证一并返回。

不调 LLM、不写库；纯函数 + 只读连接，便于被 HTTP 端点和命令行共用。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]

_HASH_PREFIX_RE = re.compile(r"^\s*\[hash:[0-9a-fA-F]+\]\s*")

# 事件相对异动的允许窗口（秒）：前 3h ~ 后 30min
LOOKBACK_SEC = 3 * 3600
LOOKAHEAD_SEC = 30 * 60
# 标题方向词（与归因器口径保持一致）
BULL_WORDS = ("上涨", "大涨", "飙升", "走高", "反弹", "利好", "超预期", "增持",
              "买入", "突破", "新高", "上调", "看多", "获批", "rally", "surge",
              "gain", "soar", "beat")
BEAR_WORDS = ("下跌", "大跌", "暴跌", "走低", "回落", "利空", "不及预期", "减持",
              "卖出", "跌破", "新低", "下调", "看空", "获批" and "禁令",
              "drop", "fall", "plunge", "bear", "sell")


def _headline_direction(headline: str) -> int:
    h = (headline or "").lower()
    bull = any(w.lower() in h for w in BULL_WORDS)
    bear = any(w.lower() in h for w in BEAR_WORDS)
    if bull and not bear:
        return 1
    if bear and not bull:
        return -1
    return 0


@dataclass
class ReverseHit:
    event_id: Optional[int]
    headline: str
    source: str
    event_ts: int
    lag_sec: int                      # 事件相对目标时刻的滞后（负=事件更早）
    direction: int                     # 标题方向 1/-1/0
    score: float
    linked: bool = False               # 是否已在归因表里配对
    match_type: str = ""
    flow_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "headline": self.headline,
            "source": self.source,
            "event_ts": self.event_ts,
            "event_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self.event_ts)),
            "lag_sec": self.lag_sec,
            "lag_min": round(self.lag_sec / 60, 1),
            "direction": self.direction,
            "score": round(self.score, 3),
            "linked": self.linked,
            "match_type": self.match_type,
            "flow_notes": self.flow_notes,
        }


def _flow_notes(conn: sqlite3.Connection, asset: str, move_ts: int) -> List[str]:
    """同期资金面佐证（异动前最近一条 OI/多空比/费率/量能，各取最新）。"""
    notes: List[str] = []
    if not asset:
        return notes
    label = {"oi": "持仓", "lsratio": "多空比", "funding": "费率", "volume": "量能"}
    seen_kind: Dict[str, float] = {}
    try:
        rows = conn.execute(
            "SELECT kind, value FROM flow_snapshots "
            "WHERE asset=? AND ts <= ? ORDER BY ts DESC LIMIT 12",
            ((asset or "").upper(), int(move_ts)),
        ).fetchall()
    except sqlite3.OperationalError:
        return notes
    for kind, val in rows:
        if kind in label and kind not in seen_kind:
            seen_kind[kind] = float(val)
    for kind, name in label.items():
        if kind in seen_kind:
            notes.append(f"{name}={seen_kind[kind]:.4g}")
    return notes


def lookup(conn: sqlite3.Connection, asset: str, target_ts: int,
           lookback_sec: int = LOOKBACK_SEC,
           lookahead_sec: int = LOOKAHEAD_SEC,
           max_events: int = 20) -> Dict[str, Any]:
    """反向检索：指定品种 + 时刻，返回同期行情与候选事件。

    Args:
        conn: 只读/读写连接均可；本函数不写库。
        asset: 品种代码（BTC/ETH/XAU/WTI…），空串表示不限定品种。
        target_ts: 目标时刻（unix 秒）。
        lookback_sec / lookahead_sec: 事件相对目标时刻的窗口。
    """
    asset_u = (asset or "").upper()
    lo = int(target_ts) - lookback_sec
    hi = int(target_ts) + lookahead_sec

    # 1) 同期行情：目标时刻前后 ±90min 内的临界点/快慢形态
    moves: List[Dict[str, Any]] = []
    try:
        m_rows = conn.execute(
            """SELECT id, asset, ts, move_pct, move_kind, duration_min, algo_score
                 FROM critical_price_moves
                WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 10""",
            (int(target_ts) - 5400, int(target_ts) + 5400, int(target_ts)),
        ).fetchall()
        for r in m_rows:
            if asset_u and (r[1] or "").upper() != asset_u:
                continue
            moves.append({
                "move_id": r[0], "asset": r[1], "ts": r[2],
                "move_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[2])),
                "move_pct": round(float(r[3] or 0), 3),
                "move_kind": r[4], "duration_min": r[5], "algo_score": r[6],
            })
    except sqlite3.OperationalError:
        pass

    # 2) 已配对归因（事件 ↔ 本品种本时刻附近异动），用于标注 linked
    linked_keys: set[tuple] = set()
    try:
        for r in conn.execute(
            """SELECT DISTINCT ev_news_id, asset FROM event_move_attribution
                WHERE move_ts BETWEEN ? AND ?""",
            (int(target_ts) - 5400, int(target_ts) + 5400),
        ).fetchall():
            linked_keys.add((r[0], (r[1] or "").upper()))
    except sqlite3.OperationalError:
        pass

    # 3) 候选事件：时间窗内、标的相关（或宏观无标的），排除噪声/已删
    ev_rows: List[Any] = []
    try:
        sql = """SELECT id, source, headline, assets, asset, score, ts, action, status
                   FROM news_events
                  WHERE ts BETWEEN ? AND ?
                    AND COALESCE(is_noise, 0) = 0
                    AND COALESCE(status, 'ok') != 'removed'
                  ORDER BY ts DESC LIMIT 200"""
        ev_rows = conn.execute(sql, (lo, hi)).fetchall()
    except sqlite3.OperationalError:
        ev_rows = []

    hits: List[ReverseHit] = []
    for r in ev_rows:
        eid, src, headline, ev_assets, ev_asset, score, ts, action, status = r
        headline = _HASH_PREFIX_RE.sub("", headline or "")
        if not headline.strip():
            continue
        assets_set = {(x or "").upper() for x in
                      str(ev_assets or "").replace("，", ",").split(",") if x.strip()}
        assets_set.add((ev_asset or "").upper())
        asset_match = (not asset_u) or asset_u in assets_set or asset_u in headline.upper()
        if not asset_match:
            continue
        lag = int(ts) - int(target_ts)
        direction = _headline_direction(headline)
        # 方向与同期行情一致性加分
        dir_align = 0
        for mv in moves:
            mv_dir = 1 if (mv["move_pct"] or 0) > 0 else -1
            if direction != 0 and direction == mv_dir:
                dir_align = 1
                break
        base = float(score or 0.5)
        recency = max(0.5, 1.0 - abs(lag) / lookback_sec)
        hit_score = base * recency + 0.1 * dir_align
        linked = (eid, asset_u) in linked_keys
        flow = _flow_notes(conn, asset_u, int(target_ts))
        hits.append(ReverseHit(
            event_id=eid, headline=headline[:200], source=src or "",
            event_ts=int(ts), lag_sec=lag, direction=direction,
            score=hit_score, linked=linked,
            match_type="asset" if asset_u in assets_set else "headline",
            flow_notes=flow,
        ))

    hits.sort(key=lambda h: (not h.linked, -h.score))
    return {
        "asset": asset_u or "ALL",
        "target_ts": int(target_ts),
        "target_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(int(target_ts))),
        "window": {"lookback_sec": lookback_sec, "lookahead_sec": lookahead_sec},
        "moves": moves,
        "events": [h.to_dict() for h in hits[:max_events]],
        "event_count": len(hits),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="时刻→事件反向检索")
    ap.add_argument("--asset", default="", help="品种，如 BTC/ETH/XAU/WTI")
    ap.add_argument("--ts", type=int, default=None, help="目标时刻 unix 秒（默认 now）")
    ap.add_argument("--time", default=None, help="目标时刻 'YYYY-MM-DD HH:MM:SS'（北京时间）")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_SEC)
    ap.add_argument("--lookahead", type=int, default=LOOKAHEAD_SEC)
    args = ap.parse_args()

    target_ts = args.ts or int(time.time())
    if args.time:
        import datetime as _dt
        try:
            dt = _dt.datetime.strptime(args.time, "%Y-%m-%d %H:%M:%S")
            target_ts = int(dt.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp())
        except ValueError:
            print("时间格式应为 YYYY-MM-DD HH:MM:SS")
            return 2

    import db
    conn = sqlite3.connect(str(db.get_db_path()))
    conn.row_factory = sqlite3.Row
    res = lookup(conn, args.asset, target_ts, args.lookback, args.lookahead)
    conn.close()

    print(f"=== {res['target_time']}  {res['asset']} ===")
    for mv in res["moves"]:
        print(f"  行情 {mv['move_kind']} {mv['move_pct']:+.2f}% "
              f"({mv['duration_min']}min) @ {mv['move_time']}")
    print(f"  候选事件 {res['event_count']} 条：")
    for ev in res["events"]:
        flag = "已归因" if ev["linked"] else "未配对"
        print(f"    [{flag}] {ev['event_time']} lag={ev['lag_min']}min "
              f"dir={ev['direction']} score={ev['score']} {ev['headline'][:50]}")
        if ev["flow_notes"]:
            print(f"        资金: {', '.join(ev['flow_notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
