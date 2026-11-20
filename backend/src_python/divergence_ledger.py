# -*- coding: utf-8 -*-
"""背离信号：样本外验证 + 本地信号台账（2026-11-16）

两件事：
1. validate()  时间外样本检验。前 train_ratio 段只用于观察规则，后段单独结算，
   扣往返成本；按趋势上下文 / 等级 / 品种给 verdict。
2. ledger      把每天新确认的背离信号写进 divergence_ledger，到期自动结算，
   形成可持续观测的本地账本（不是一次性回测）。

成本口径：cost_bps 为往返总成本（默认 8bp = 手续费+滑点），直接从方向收益里扣。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import divergence_engine as de

# v480：统一走 db_location（支持 TRIDENT_DB_PATH 覆盖）
from db_location import DB_PATH
DEFAULT_COST_BPS = 8.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS divergence_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key    TEXT NOT NULL UNIQUE,
    asset         TEXT NOT NULL,
    interval      TEXT NOT NULL,
    type          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    grade         TEXT NOT NULL,
    score         REAL NOT NULL,
    agree_legs    INTEGER NOT NULL DEFAULT 0,
    trend         TEXT NOT NULL DEFAULT '',
    context       TEXT NOT NULL DEFAULT '',
    pivot_a_ts    INTEGER,
    pivot_b_ts    INTEGER,
    confirm_ts    INTEGER NOT NULL,
    entry         REAL NOT NULL,
    trigger       REAL,
    invalidation  REAL,
    target        REAL,
    atr           REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    horizon       INTEGER NOT NULL DEFAULT 10,
    exit_ts       INTEGER,
    exit_price    REAL,
    ret_pct       REAL,
    pnl_pct       REAL,
    hit           INTEGER,
    mfe_pct       REAL,
    mae_pct       REAL,
    stop_hit      INTEGER,
    target_hit    INTEGER,
    executable    INTEGER NOT NULL DEFAULT 0,
    rule_id       TEXT,
    settled_at    INTEGER,
    created_at    INTEGER NOT NULL,
    meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_div_ledger_status ON divergence_ledger(status);
CREATE INDEX IF NOT EXISTS idx_div_ledger_asset ON divergence_ledger(asset, interval);
CREATE INDEX IF NOT EXISTS idx_div_ledger_context ON divergence_ledger(context);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(divergence_ledger)")}
    if "executable" not in cols:
        conn.execute("ALTER TABLE divergence_ledger ADD COLUMN executable INTEGER NOT NULL DEFAULT 0")
    if "rule_id" not in cols:
        conn.execute("ALTER TABLE divergence_ledger ADD COLUMN rule_id TEXT")
    conn.commit()


# ───────────────────────── 样本外验证 ─────────────────────────
def _net_pnl(sig: Dict[str, Any], ret_pct: float, cost_bps: float) -> float:
    gross = -ret_pct if sig["type"] == "bearish" else ret_pct
    return gross - cost_bps / 100.0


def _bucket(items: List[Tuple[Dict[str, Any], float]], cost_bps: float,
            baseline: float) -> Dict[str, Any]:
    if not items:
        return {"n": 0, "hit_rate": None, "avg_pnl": None, "profit_factor": None, "verdict": "样本不足"}
    pnls = [_net_pnl(s, r, cost_bps) for s, r in items]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    hit = len(wins) / len(pnls) * 100.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    n = len(pnls)
    avg = sum(pnls) / n
    if n < 15:
        verdict = "样本不足"
    elif avg > 0 and hit >= baseline + 3:
        verdict = "有效"
    elif avg > 0:
        verdict = "观察"
    else:
        verdict = "未通过"
    return {"n": n, "hit_rate": round(hit, 1), "avg_pnl": round(avg, 2),
            "profit_factor": round(pf, 2) if pf else None,
            "verdict": verdict,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None}


def validate(horizon: int = 10, train_ratio: float = 0.7,
             cost_bps: float = DEFAULT_COST_BPS,
             assets: Optional[Sequence[str]] = None,
             intervals: Sequence[str] = ("1d",)) -> Dict[str, Any]:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if assets:
            todo = [(a.upper(), iv) for a in assets for iv in intervals]
        else:
            todo = []
            for iv in intervals:
                tbl = "ohlcv_daily" if iv == "1d" else "ohlcv_intraday"
                if iv == "1d":
                    rows = conn.execute(
                        "SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset HAVING n>=800").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT asset, COUNT(*) n FROM ohlcv_intraday WHERE interval=? "
                        "GROUP BY asset HAVING n>=600", (iv,)).fetchall()
                todo += [(r["asset"], iv) for r in rows]

        train: Dict[str, List[Tuple[Dict[str, Any], float]]] = defaultdict(list)
        test: Dict[str, List[Tuple[Dict[str, Any], float]]] = defaultdict(list)
        all_test: List[Tuple[Dict[str, Any], float]] = []
        by_asset: Dict[str, List[Tuple[Dict[str, Any], float]]] = defaultdict(list)
        rule_train: Dict[str, List[Tuple[Dict[str, Any], float]]] = defaultdict(list)
        rule_test: Dict[str, List[Tuple[Dict[str, Any], float]]] = defaultdict(list)
        skipped = 0

        for asset, iv in todo:
            rows = de.load_bars(conn, asset, iv, 3000)
            if len(rows) < 400:
                skipped += 1
                continue
            sigs = de.scan_signals(rows, asset=asset, interval=iv)
            closes = [float(r["close"]) for r in rows]
            cut = int(len(rows) * train_ratio)
            for s in sigs:
                c = s["confirm_idx"]
                if c is None or c + horizon >= len(closes):
                    continue
                ret = (closes[c + horizon] - closes[c]) / closes[c] * 100.0
                (train if c < cut else test)[s["context"]].append((s, ret))
                rule = de.rule_of(s)
                if rule:
                    (rule_train if c < cut else rule_test)[rule["id"]].append((s, ret))
                if c >= cut:
                    all_test.append((s, ret))
                    by_asset[asset].append((s, ret))

        base_up = 0
        base_n = 0
        for asset, iv in todo:
            rows = de.load_bars(conn, asset, iv, 3000)
            closes = [float(r["close"]) for r in rows]
            for i in range(len(closes) - horizon):
                if closes[i] > 0:
                    base_n += 1
                    base_up += 1 if closes[i + horizon] > closes[i] else 0
        base_rate = base_up / max(1, base_n) * 100

        ctx_rows = []
        for ctx in sorted(set(train) | set(test)):
            ctx_rows.append({
                "context": ctx,
                "train": _bucket(train.get(ctx, []), cost_bps, base_rate),
                "test": _bucket(test.get(ctx, []), cost_bps, base_rate),
            })

        grade_train = defaultdict(list)
        grade_test = defaultdict(list)
        for ctx, items in train.items():
            for s, r in items:
                grade_train[s["grade"]].append((s, r))
        for ctx, items in test.items():
            for s, r in items:
                grade_test[s["grade"]].append((s, r))
        grades = []
        for g in ("A", "B", "C"):
            grades.append({"grade": g, "train": _bucket(grade_train.get(g, []), cost_bps, base_rate),
                           "test": _bucket(grade_test.get(g, []), cost_bps, base_rate)})

        rules_rows = []
        for rule in de.EFFECTIVE_RULES:
            rules_rows.append({
                "id": rule["id"], "label": rule["label"], "horizon": rule["horizon"],
                "note": rule.get("note", ""),
                "train": _bucket(rule_train.get(rule["id"], []), cost_bps, base_rate),
                "test": _bucket(rule_test.get(rule["id"], []), cost_bps, base_rate),
            })

        assets_rows = []
        for a, items in sorted(by_asset.items(), key=lambda x: -len(x[1])):
            assets_rows.append({"asset": a, **_bucket(items, cost_bps, base_rate)})

        # 样本外资金曲线（按确认时间排序，单笔等权）
        curve = []
        eq = 0.0
        for i, (s, r) in enumerate(sorted(all_test, key=lambda x: x[0]["confirm_ts"]), 1):
            eq += _net_pnl(s, r, cost_bps)
            curve.append({"i": i, "eq": round(eq, 2)})

        return {
            "ok": True,
            "horizon": horizon,
            "train_ratio": train_ratio,
            "cost_bps": cost_bps,
            "baseline_rate": round(base_rate, 1),
            "signals_total": len(all_test) + sum(len(v) for v in train.values()),
            "tests": len(all_test),
            "assets": len(todo) - skipped,
            "skipped": skipped,
            "overall": _bucket(all_test, cost_bps, base_rate),
            "by_context": ctx_rows,
            "by_grade": grades,
            "by_asset": assets_rows,
            "by_rule": rules_rows,
            "equity": curve[-1]["eq"] if curve else 0.0,
            "equity_curve": curve,
        }
    finally:
        conn.close()


# ───────────────────────── 本地台账 ─────────────────────────
def _signal_key(asset: str, iv: str, s: Dict[str, Any]) -> str:
    return f"{asset}|{iv}|{s['type']}|{s['kind']}|{s['pivot_b']['ts']}"


def _norm_interval(iv: str) -> str:
    v = (iv or "1d").lower()
    return "1d" if v in ("1d", "d", "day", "daily") else v


def sync_ledger(horizon: int = 10, within_bars: int = 90,
                assets: Optional[Sequence[str]] = None,
                intervals: Sequence[str] = ("1d",)) -> Dict[str, Any]:
    """把最近确认的信号写入台账；重复信号按 signal_key 去重。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    inserted = 0
    scanned = 0
    try:
        ensure_schema(conn)
        if assets:
            todo = [(a.upper(), _norm_interval(iv)) for a in assets for iv in intervals]
        else:
            todo = []
            for iv in intervals:
                iv = _norm_interval(iv)
                if iv == "1d":
                    rows = conn.execute(
                        "SELECT asset FROM ohlcv_daily GROUP BY asset HAVING COUNT(*)>=400").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT asset FROM ohlcv_intraday WHERE interval=? GROUP BY asset HAVING COUNT(*)>=400",
                        (iv,)).fetchall()
                todo += [(r["asset"], iv) for r in rows]

        for asset, iv in todo:
            rows = de.load_bars(conn, asset, iv, 1200)
            if len(rows) < 150:
                continue
            scanned += 1
            sigs = de.scan_signals(rows, asset=asset, interval=iv)
            last = len(rows) - 1
            for s in sigs:
                c = s["confirm_idx"]
                if c is None or last - c > within_bars:
                    continue
                entry = float(rows[c]["close"])
                now = int(time.time())
                rule = de.rule_of(s)
                eff = 1 if rule else 0
                hz = int(rule["horizon"]) if rule else int(horizon)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO divergence_ledger "
                    "(signal_key,asset,interval,type,kind,grade,score,agree_legs,trend,context,"
                    "pivot_a_ts,pivot_b_ts,confirm_ts,entry,trigger,invalidation,target,atr,"
                    "executable,rule_id,status,horizon,created_at,meta_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?)",
                    (_signal_key(asset, iv, s), asset, iv, s["type"], s["kind"], s["grade"],
                     s["score"], s["agree_legs"], s.get("trend", ""), s.get("context", ""),
                     s["pivot_a"]["ts"], s["pivot_b"]["ts"], s["confirm_ts"], entry,
                     s["trigger"], s["invalidation"], s["target"], s["atr"], eff,
                     rule["id"] if rule else None, hz, now,
                     json.dumps({"bars_gap": s["bars_gap"], "adx": s["adx"],
                                 "ema200": s["ema200"]}, ensure_ascii=False)))
                inserted += cur.rowcount
        conn.commit()
        settled = settle_ledger(conn, cost_bps=DEFAULT_COST_BPS)
        return {"ok": True, "scanned": scanned, "inserted": inserted, "settled": settled}
    finally:
        conn.close()


def settle_ledger(conn: sqlite3.Connection, cost_bps: float = DEFAULT_COST_BPS,
                  max_batch: int = 500) -> int:
    """到期结算：从确认根往后取 horizon+1 根，按收盘结算，记录 MFE/MAE/止损止盈。"""
    open_rows = conn.execute(
        "SELECT * FROM divergence_ledger WHERE status='open' ORDER BY confirm_ts LIMIT ?",
        (max_batch,)).fetchall()
    settled = 0
    for r in open_rows:
        h = int(r["horizon"] or 10)
        table = "ohlcv_daily" if r["interval"] == "1d" else "ohlcv_intraday"
        sql = (f"SELECT ts,high,low,close FROM {table} WHERE asset=? AND ts>=? "
               + ("" if r["interval"] == "1d" else "AND interval=? ")
               + "ORDER BY ts LIMIT ?")
        args: List[Any] = [r["asset"], r["confirm_ts"]]
        if r["interval"] != "1d":
            args.append(r["interval"])
        args.append(h + 1)
        bars = conn.execute(sql, args).fetchall()
        if len(bars) < h + 1:
            continue
        entry = float(r["entry"])
        exit_price = float(bars[h]["close"])
        bear = r["type"] == "bearish"
        ret = (exit_price - entry) / entry * 100.0
        pnl = (-ret if bear else ret) - cost_bps / 100.0
        if bear:
            mfe = max((entry - float(b["low"])) / entry * 100.0 for b in bars[1:])
            mae = max((float(b["high"]) - entry) / entry * 100.0 for b in bars[1:])
            stop_hit = any(float(b["high"]) >= r["invalidation"] for b in bars[1:])
            target_hit = any(float(b["low"]) <= r["target"] for b in bars[1:])
        else:
            mfe = max((float(b["high"]) - entry) / entry * 100.0 for b in bars[1:])
            mae = max((entry - float(b["low"])) / entry * 100.0 for b in bars[1:])
            stop_hit = any(float(b["low"]) <= r["invalidation"] for b in bars[1:])
            target_hit = any(float(b["high"]) >= r["target"] for b in bars[1:])
        conn.execute(
            "UPDATE divergence_ledger SET status='settled', settled_at=?, exit_ts=?, exit_price=?, "
            "ret_pct=?, pnl_pct=?, hit=?, mfe_pct=?, mae_pct=?, stop_hit=?, target_hit=? WHERE id=?",
            (int(time.time()), int(bars[h]["ts"]), exit_price, round(ret, 3), round(pnl, 3),
             1 if pnl > 0 else 0, round(mfe, 3), round(mae, 3),
             1 if stop_hit else 0, 1 if target_hit else 0, r["id"]))
        settled += 1
    conn.commit()
    return settled


def ledger_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT context, grade, COUNT(*) n, AVG(hit) hit_rate, AVG(pnl_pct) avg_pnl, "
        "SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) wins FROM divergence_ledger "
        "WHERE status='settled' GROUP BY context, grade").fetchall()
    total = conn.execute(
        "SELECT COUNT(*) n, AVG(hit) hit_rate, AVG(pnl_pct) avg_pnl, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n FROM divergence_ledger").fetchone()
    by_ctx = [{"context": r["context"], "grade": r["grade"], "n": r["n"],
               "hit_rate": round((r["hit_rate"] or 0) * 100, 1),
               "avg_pnl": round(r["avg_pnl"] or 0, 2)} for r in rows]
    by_ctx.sort(key=lambda x: -x["n"])
    return {
        "total": total["n"] if total else 0,
        "open": total["open_n"] if total else 0,
        "hit_rate": round((total["hit_rate"] or 0) * 100, 1) if total and total["n"] else None,
        "avg_pnl": round(total["avg_pnl"] or 0, 2) if total and total["n"] else None,
        "by_context": by_ctx,
    }


def ledger_list(status: str = "all", limit: int = 200) -> Dict[str, Any]:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        sql = "SELECT * FROM divergence_ledger "
        if status in ("open", "settled"):
            sql += "WHERE status=? "
        sql += "ORDER BY confirm_ts DESC LIMIT ?"
        args = ([status] if status in ("open", "settled") else []) + [min(max(limit, 1), 500)]
        rows = conn.execute(sql, args).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item.pop("meta_json", None)
            item["confirm_at"] = de._fmt_ts(r["confirm_ts"])
            items.append(item)
        return {"ok": True, "items": items, "stats": ledger_stats(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 指标记分牌
# 为什么单独做：`ledger_stats()` 按 context+grade 分桶，回答的是"什么环境下
# 背离好用"；但用户真正要问的是"这个指标到底该不该留"。后者必须按**方向**
# 拆开 —— 实测看跌背离净 +1.9%、看涨背离净 −1.06%，混在一起看是 +0.3%，
# 结论会变成"勉强能用"，实际是一半赚钱一半送钱。
_MIN_SAMPLE_WATCH = 10   # 少于 10 笔只能算轶事
_MIN_SAMPLE_FULL = 30    # 30 笔以上才谈得上统计意义


def _verdict(n: int, hit_rate: Optional[float], avg_pnl: Optional[float]) -> str:
    if n < _MIN_SAMPLE_WATCH:
        return "样本不足"
    # 亏钱这件事**不需要 30 笔才能判**：10 笔里只赢 1 笔、平均亏 8.8%，
    # 已经足够说明"别照这个信号做"（对称地，赚小钱要 30 笔才算数）。
    # 这是刻意的不对称阈值 —— 宁可晚一点承认有效，也不要晚一点承认有害。
    if (avg_pnl or 0) <= -3 and (hit_rate or 0) <= 25:
        return "避开"
    if n < _MIN_SAMPLE_FULL:
        return "观察中"
    if (avg_pnl or 0) > 0 and (hit_rate or 0) >= 50:
        return "可用"
    return "降权/停用"


def scoreboard(conn: Optional[sqlite3.Connection] = None,
               horizon: int = 10) -> Dict[str, Any]:
    """按 方向 / 形态 / 等级 拆开的命中率记分牌 + 一句话结论。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) settled, "
            "SUM(CASE WHEN status<>'settled' THEN 1 ELSE 0 END) open_n "
            "FROM divergence_ledger").fetchone()
        by_dir_rows = conn.execute(
            "SELECT type, COUNT(*) n, SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) wins, "
            "AVG(hit) hit_rate, AVG(pnl_pct) avg_pnl, AVG(mfe_pct) avg_mfe, "
            "AVG(mae_pct) avg_mae, AVG(ret_pct) avg_ret "
            "FROM divergence_ledger WHERE status='settled' GROUP BY type").fetchall()
        detail_rows = conn.execute(
            "SELECT type, kind, grade, COUNT(*) n, "
            "SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) wins, AVG(hit) hit_rate, "
            "AVG(pnl_pct) avg_pnl FROM divergence_ledger WHERE status='settled' "
            "GROUP BY type, kind, grade ORDER BY n DESC").fetchall()
        # 按「方向/形态/趋势」拆 —— 这是**结论真正落地**的那一层：
        # 笼统说"看涨背离不行"是错的（隐藏底背离+上升趋势 15 笔赢 9 笔 +3.18%）；
        # 亏钱的是"常规底背离 + 下跌趋势"这一格（10 笔只赢 1 笔、−8.81%）。
        ctx_rows = conn.execute(
            "SELECT context, COUNT(*) n, SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) wins, "
            "AVG(hit) hit_rate, AVG(pnl_pct) avg_pnl FROM divergence_ledger "
            "WHERE status='settled' GROUP BY context ORDER BY n DESC").fetchall()
    finally:
        if own:
            conn.close()

    by_dir = []
    for r in by_dir_rows:
        n = int(r["n"] or 0)
        hr = round((r["hit_rate"] or 0) * 100, 1) if n else None
        pnl = round(r["avg_pnl"], 2) if r["avg_pnl"] is not None else None
        by_dir.append({
            "direction": r["type"],
            "n": n,
            "wins": int(r["wins"] or 0),
            "hit_rate": hr,
            "avg_pnl": pnl,
            "avg_mfe": round(r["avg_mfe"], 2) if r["avg_mfe"] is not None else None,
            "avg_mae": round(r["avg_mae"], 2) if r["avg_mae"] is not None else None,
            "avg_ret": round(r["avg_ret"], 2) if r["avg_ret"] is not None else None,
            "verdict": _verdict(n, hr, pnl),
        })
    by_dir.sort(key=lambda x: -x["n"])

    detail = []
    for r in detail_rows:
        n = int(r["n"] or 0)
        hr = round((r["hit_rate"] or 0) * 100, 1) if n else None
        pnl = round(r["avg_pnl"], 2) if r["avg_pnl"] is not None else None
        detail.append({
            "direction": r["type"], "kind": r["kind"], "grade": r["grade"],
            "n": n, "wins": int(r["wins"] or 0), "hit_rate": hr,
            "avg_pnl": pnl, "verdict": _verdict(n, hr, pnl),
        })

    by_ctx = []
    for r in ctx_rows:
        n = int(r["n"] or 0)
        hr = round((r["hit_rate"] or 0) * 100, 1) if n else None
        pnl = round(r["avg_pnl"], 2) if r["avg_pnl"] is not None else None
        ctx_raw = str(r["context"] or "")
        parts = ctx_raw.split("/")
        by_ctx.append({
            "context": ctx_raw,
            "label": (("看涨" if (parts[1] == "bullish" if len(parts) > 1 else False) else "看跌")
                      + ("·隐藏" if (parts[0] == "hidden") else "·常规")
                      + ("·上升趋势" if (parts[2] == "up" if len(parts) > 2 else False) else "·下跌趋势")),
            "n": n, "wins": int(r["wins"] or 0), "hit_rate": hr,
            "avg_pnl": pnl, "verdict": _verdict(n, hr, pnl),
        })

    lines = []
    for d in by_dir:
        label = "看涨" if d["direction"] == "bullish" else "看跌"
        if d["verdict"] in ("样本不足", "观察中"):
            lines.append(f"{label}背离 {d['n']} 笔命中 {d['hit_rate']}%、净 {d['avg_pnl']}%：{d['verdict']}")
            continue
        if d["verdict"] == "可用":
            lines.append(f"{label}背离 {d['n']} 笔命中 {d['hit_rate']}%、净 +{d['avg_pnl']}%：目前唯一站得住的用法")
        else:
            lines.append(f"{label}背离 {d['n']} 笔命中 {d['hit_rate']}%、净 {d['avg_pnl']}%：建议降权或加确认条件")

    for c in by_ctx:
        if c["verdict"] == "避开":
            lines.append(f"⚠ {c['label']} {c['n']} 笔只赢 {c['wins']} 笔、"
                         f"平均 {c['avg_pnl']}%：这一格别照着做")

    settled = int(total["settled"] or 0) if total else 0
    return {
        "ok": True,
        "horizon": horizon,
        "total": int(total["n"] or 0) if total else 0,
        "settled": settled,
        "open": int(total["open_n"] or 0) if total else 0,
        "by_direction": by_dir,
        "by_context": by_ctx,
        "detail": detail,
        "conclusion": "；".join(lines) if lines else "台账还没有结算样本",
        "disclaimer": ("每档样本 %d 笔以上才算「可用」，%d 笔以下只能当轶事；"
                       "净收益已扣 8bp 往返成本。" % (_MIN_SAMPLE_FULL, _MIN_SAMPLE_WATCH)),
    }
