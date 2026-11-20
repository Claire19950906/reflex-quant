#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""frozen_ruler_r6.py — 尺子 R6：修取数 + 补覆盖度，不改任何门槛

设计纪律（见 runtime/frozen_ruler/SPEC_R6.md）：
  · LABEL / GATES / 判定词 直接从 R5 导入，本文件不重新定义任何阈值
  · 唯一改动 1：价格底座改为「每资产每日一根真日线」（剔占位行、同日取最后一根）
  · 唯一改动 2：补上 11 条要求里原本没有测量项的部分（M3b / M4 / M5 / M6b / M12）
  · 没有数据源的项一律标「不可测」，不拿别的变量顶替

跑法：
  python3 frozen_ruler_r6.py --spec       # 打印锁定的定义（跑数前看这个）
  python3 frozen_ruler_r6.py --selfcheck  # 连跑两次逐字段一致（含区间）
  python3 frozen_ruler_r6.py              # 出表 + 写快照/history_r6.jsonl
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import tempfile
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

import frozen_ruler_full as R5
from frozen_ruler_full import (GATES, LABEL, SIGNALS, _norm_asset, base_rate,
                               judge, spearman, wilson, sig_e1, sig_e2, sig_e4,
                               sig_e6, sig_e2l, sig_e2k)

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
EVAL_DB = BACKEND / "runtime" / "eval" / "eval.db"
ROLE_DB = BACKEND / "runtime" / "role_lab" / "role_lab.db"
OUT_DIR = BACKEND / "runtime" / "frozen_ruler"

RULER_VERSION = "R6.1"

MEASURES: List[Dict[str, Any]] = [
    {"id": "M1",  "req": 1,  "name": "事件方向", "kind": "ratio",
     "src": "event_kline_impact.direction", "gate": "n≥200 & 55%"},
    {"id": "M2",  "req": 2,  "name": "权重判别力", "kind": "rho",
     "src": "ai_decisions.signal_weight", "gate": "ρ≥0.20"},
    {"id": "M3a", "req": 3,  "name": "模型互评一致性", "kind": "ratio",
     "src": "ai_decisions.verification", "gate": "n≥200 & 55%"},
    {"id": "M3b", "req": 3,  "name": "市场共识(新闻净方向)", "kind": "ratio",
     "src": "news_events 按资产×日聚合", "gate": "n≥200 & 55%"},
    {"id": "M3c", "req": 3,  "name": "心理预期", "kind": "na",
     "src": "无（资金费率仅29天/无持仓结构/无期权偏斜）", "gate": "不可测"},
    {"id": "M4",  "req": 4,  "name": "K线A/B增量", "kind": "ab",
     "src": "event_kline_impact × critical_price_moves", "gate": "两组各n≥100 & 差值≥5pp"},
    {"id": "M5",  "req": 5,  "name": "字段被真正使用", "kind": "share",
     "src": "ai_decisions / news_events / trade_reflections_v2", "gate": "五项各≥90%=通·70-90=半·<70=断"},
    {"id": "M6a", "req": 6,  "name": "异动方向延续", "kind": "ratio",
     "src": "critical_price_moves.move_kind", "gate": "n≥200 & 55%"},
    {"id": "M6b", "req": 6,  "name": "双重验证", "kind": "ab",
     "src": "ai_decisions.verification × peer_reviews", "gate": "两组各n≥100 才比命中率"},
    {"id": "M6c", "req": 6,  "name": "盘口/资金流背景", "kind": "na",
     "src": "无订单簿；资金流107资产-日停在10-29", "gate": "不可测"},
    {"id": "M7",  "req": 7,  "name": "三维错因判准", "kind": "ratio",
     "src": "reflection_3layer", "gate": "判断类n≥200 & 55%"},
    {"id": "M8",  "req": 8,  "name": "因果闭环", "kind": "chain",
     "src": "chain_steps_eval + causal_chains", "gate": "步骤3 n≥200 & 可判率≥50%"},
    {"id": "M12", "req": 12, "name": "影响力区间覆盖", "kind": "cover",
     "src": "news_events + 日线（超额口径重算）", "gate": "45–55% 合理"},
    {"id": "M13", "req": 13, "name": "新闻方向+映射", "kind": "ratio",
     "src": "news_events.action", "gate": "n≥200 & 55%"},
    {"id": "M14", "req": 14, "name": "市场角色", "kind": "ratio",
     "src": "role_labels", "gate": "n≥200 & 55%"},
    # ── R6.1 补充项：跑数后发现的尺子缺口，定义见 SPEC_R6_1.md ──
    {"id": "M2b", "req": 2, "name": "权重分层(补充)", "kind": "ab", "supp": True,
     "src": "ai_decisions.signal_weight × 方向", "gate": "两组各n≥100 & 差≥5pp"},
    {"id": "M5b", "req": 5, "name": "引用完整性(补充)", "kind": "share", "supp": True,
     "src": "event_atom_ids→event_atoms / structured_input.snapshot", "gate": "三项各≥90%=通·70-90=半·<70=断"},
    {"id": "M6b2", "req": 6, "name": "双重验证覆盖率(补充)", "kind": "share", "supp": True,
     "src": "ai_decisions.peer_reviews / verification", "gate": "≥50%=在跑·20-50=半·<20=虚设"},
    {"id": "M7b", "req": 7, "name": "错过信号页(补充)", "kind": "code", "supp": True,
     "src": "frontend/app/missed-signals + 后端路由 + missed_signal", "gate": "三项代码检查全在=存在"},
    {"id": "M8b", "req": 8, "name": "纠正回写(补充)", "kind": "share", "supp": True,
     "src": "causal_chains.miss_count/updated_at", "gate": "≥50%=有回写·20-50=弱·<20=没有"},
]


def fingerprint() -> str:
    cfg = {"version": RULER_VERSION, "label": LABEL, "gates": GATES,
           "measures": [{k: m[k] for k in ("id", "req", "kind", "src", "gate")} for m in MEASURES],
           "price_base": "one_true_daily_bar_per_asset_day"}
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def fp_measure(mid: str) -> str:
    m = next(x for x in MEASURES if x["id"] == mid)
    cfg = {"label": LABEL, "gates": GATES, "measure": m}
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════
# 价格底座：每资产每日一根真日线
# ══════════════════════════════════════════════════════════════════
class DailyBook(R5.PriceBook):
    """与 R5 唯一不同的地方：取数。

    占位行 = open==high==low==close 且 volume<=0（实测 WTI 105.25、XAU 4063.39 属此类）。
    同一资产同一天：剔掉占位行后取 ts 最大的一根；若当天全是占位行，取 ts 最大的并计入 placeholder_days。
    """

    def __init__(self, conn: sqlite3.Connection):
        raw: Dict[str, Dict[str, List[Tuple[int, float, bool]]]] = {}
        rows = conn.execute(
            "SELECT asset, ts, date(ts,'unixepoch') d, open, high, low, close, volume "
            "FROM ohlcv_daily WHERE close IS NOT NULL AND close > 0 ORDER BY asset, ts"
        ).fetchall()
        for asset, ts, d, o, h, l, c, v in rows:
            flat = (o == h == l == c) and (v is None or float(v) <= 0)
            raw.setdefault(_norm_asset(asset), {}).setdefault(d, []).append((int(ts), float(c), flat))

        self.series: Dict[str, List[Tuple[str, float]]] = {}
        self.placeholder_days = 0
        chosen_rows = 0
        for a, days in raw.items():
            arr = []
            for d in sorted(days):
                cand = [x for x in days[d] if not x[2]]
                if not cand:
                    cand = days[d]
                    self.placeholder_days += 1
                best = max(cand, key=lambda x: x[0])
                arr.append((d, best[1]))
            chosen_rows += len(arr)
            self.series[a] = arr
        self.chosen_rows = chosen_rows

        self.index = {a: {d: i for i, (d, _) in enumerate(v)} for a, v in self.series.items()}
        daily_ret: Dict[str, List[float]] = {}
        for a, arr in self.series.items():
            for i in range(1, len(arr)):
                c0, c1 = arr[i - 1][1], arr[i][1]
                if c0 > 0:
                    daily_ret.setdefault(arr[i][0], []).append((c1 - c0) / c0)
        self.cs_median = {d: median(v) for d, v in daily_ret.items() if len(v) >= 3}


# ══════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════
def _day(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _ratio_row(mid: str, name: str, rows, book: DailyBook) -> Dict[str, Any]:
    hits = n = 0
    for asset, ts, direction, wend in rows:
        lab = book.label(asset, ts, wend)
        if lab is None:
            continue
        n += 1
        hits += 1 if lab == direction else 0
    return _pack(mid, name, "ratio", hits, n)


def _pack(mid: str, name: str, kind: str, hits: int, n: int,
          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    br = 50.0
    val = (hits / n * 100) if n else 0.0
    lo, hi = wilson(hits, n, GATES["z_ci"])
    row = {"id": mid, "name": name, "kind": kind, "value": round(val, 2), "n": n,
           "hits": hits, "ci_low": round(lo, 1), "ci_high": round(hi, 1),
           "status": "样本不足" if n < GATES["min_n"] else "", "fp": fp_measure(mid)}
    if extra:
        row.update(extra)
    return row


# ══════════════════════════════════════════════════════════════════
# 各测量项
# ══════════════════════════════════════════════════════════════════
def m1(conn, book):
    rows, _ = sig_e1(conn, book)
    return _ratio_row("M1", "事件方向", rows, book)


def m2(conn, book):
    xs, ys, ex = [], [], {}
    for asset, ts, w in conn.execute(
            "SELECT target_asset, ts, signal_weight FROM ai_decisions "
            "WHERE signal_weight IS NOT NULL AND signal_weight > 0"):
        if not w:
            continue
        a = _norm_asset(asset)
        idx = book.index.get(a)
        if idx is None:
            ex["资产不可映射"] = ex.get("资产不可映射", 0) + 1
            continue
        day = _day(ts)
        if not day:
            continue
        arr = book.series[a]
        entry = next((i for i in range(len(arr)) if arr[i][0] > day), None)
        if entry is None or entry + 1 >= len(arr):
            ex["无后续行情"] = ex.get("无后续行情", 0) + 1
            continue
        r = (arr[entry + 1][1] - arr[entry][1]) / arr[entry][1]
        b = book.cs_median.get(arr[entry + 1][0])
        if b is None:
            ex["无横截面基准"] = ex.get("无横截面基准", 0) + 1
            continue
        xs.append(float(w))
        ys.append(abs(r - b))
    rho = spearman(xs, ys)
    n = len(xs)
    status = "样本不足" if n < GATES["min_n"] else ("达标" if (rho or 0) >= 0.20 else "未达线")
    return {"id": "M2", "name": "权重判别力", "kind": "rho",
            "value": round(rho, 3) if rho is not None else None, "n": n,
            "target": 0.20, "status": status, "excluded": ex, "fp": fp_measure("M2")}


def m3a(conn, book):
    rows, ex = sig_e6(conn, book)
    return _ratio_row("M3a", "模型互评一致性", rows, book)


def m3b(conn, book):
    """市场共识：某资产某日新闻一边倒（|净多空比|≥0.6 且 ≥3 条），次日超额是否顺向。"""
    by_day: Dict[Tuple[str, str], List] = {}
    for asset, ts, action in conn.execute("SELECT asset, ts, action FROM news_events"):
        a = _norm_asset(asset)
        d = _day(ts)
        if not d:
            continue
        by_day.setdefault((a, d), []).append((int(ts), str(action or "").upper()))
    hits = n = 0
    ex = {"样本不足(条数<3)": 0, "净方向不够一边倒": 0, "取不到行情/无超额": 0}
    for (a, d), items in by_day.items():
        b = sum(1 for _, x in items if x == "BUY")
        s = sum(1 for _, x in items if x == "SELL")
        if b + s < 3:
            ex["样本不足(条数<3)"] += 1
            continue
        net = (b - s) / (b + s)
        if abs(net) < 0.6:
            ex["净方向不够一边倒"] += 1
            continue
        want = 1 if net > 0 else -1
        cutoff = max(t for t, _ in items)
        lab = book.label(a, cutoff, None)
        if lab is None:
            ex["取不到行情/无超额"] += 1
            continue
        n += 1
        hits += 1 if lab == want else 0
    return _pack("M3b", "市场共识(新闻净方向)", "ratio", hits, n, {"excluded": ex})


def m4(conn, book):
    """K 线 A/B：事件前 3 日内有异动 vs 前 7 日内无任何异动。"""
    moves: Dict[str, List[str]] = {}
    for asset, ts in conn.execute("SELECT asset, ts FROM critical_price_moves"):
        d = _day(ts)
        if d:
            moves.setdefault(_norm_asset(asset), []).append(d)
    md = {a: sorted(set(v)) for a, v in moves.items()}

    rows, _ = sig_e1(conn, book)
    g1 = [0, 0]   # 有异动
    g0 = [0, 0]   # 无异动
    for asset, ts, direction, wend in rows:
        cutoff = max(int(ts), int(wend)) if wend else int(ts)
        day = _day(cutoff)
        if not day:
            continue
        lab = book.label(asset, ts, wend)
        if lab is None:
            continue
        a = _norm_asset(asset)
        ds = md.get(a, [])
        base = datetime.strptime(day, "%Y-%m-%d")
        d3 = (base - timedelta(days=3)).strftime("%Y-%m-%d")
        d7 = (base - timedelta(days=7)).strftime("%Y-%m-%d")
        has3 = any(d3 <= x <= day for x in ds)
        has7 = any(d7 <= x <= day for x in ds)
        if has3:
            g1[0] += 1
            g1[1] += 1 if lab == direction else 0
        elif not has7:
            g0[0] += 1
            g0[1] += 1 if lab == direction else 0
    p1 = (g1[1] / g1[0] * 100) if g1[0] else 0.0
    p0 = (g0[1] / g0[0] * 100) if g0[0] else 0.0
    diff = p1 - p0
    ok = g1[0] >= 100 and g0[0] >= 100
    if not ok:
        status = "样本不足"
    elif diff >= 5:
        status = "K线有增量"
    else:
        status = "K线无增量"
    return {"id": "M4", "name": "K线A/B增量", "kind": "ab",
            "with_move": {"value": round(p1, 2), "n": g1[0], "hits": g1[1],
                          "ci": [round(x, 1) for x in wilson(g1[1], g1[0])]},
            "without_move": {"value": round(p0, 2), "n": g0[0], "hits": g0[1],
                             "ci": [round(x, 1) for x in wilson(g0[1], g0[0])]},
            "diff_pp": round(diff, 2), "value": round(diff, 2),
            "n": g1[0] + g0[0], "status": status, "fp": fp_measure("M4")}


def m5(conn, book):
    def share(sql: str) -> Tuple[int, int]:
        t, x = conn.execute(sql).fetchone()
        return int(x or 0), int(t or 0)

    items = {}
    items["决策含 structured_input"] = share(
        "SELECT count(*), sum(CASE WHEN structured_input IS NOT NULL AND structured_input!='' "
        "AND structured_input NOT IN ('{}','[]','null') THEN 1 ELSE 0 END) FROM ai_decisions")
    items["决策含 hard_data_evidence"] = share(
        "SELECT count(*), sum(CASE WHEN hard_data_evidence IS NOT NULL AND hard_data_evidence!='' "
        "AND hard_data_evidence NOT IN ('{}','[]','null') THEN 1 ELSE 0 END) FROM ai_decisions")
    items["决策含 event_atom_ids"] = share(
        "SELECT count(*), sum(CASE WHEN event_atom_ids IS NOT NULL AND event_atom_ids!='' "
        "AND event_atom_ids NOT IN ('{}','[]','null') THEN 1 ELSE 0 END) FROM ai_decisions")
    items["新闻回写决策"] = share(
        "SELECT count(*), sum(CASE WHEN ai_decision_id IS NOT NULL THEN 1 ELSE 0 END) FROM news_events")
    items["反思回写决策"] = share(
        "SELECT count(*), sum(CASE WHEN ai_decision_id IS NOT NULL THEN 1 ELSE 0 END) FROM trade_reflections_v2")

    detail = {}
    worst = 100.0
    for k, (x, t) in items.items():
        pct = (x / t * 100) if t else 0.0
        detail[k] = {"value": round(pct, 1), "hits": x, "n": t}
        worst = min(worst, pct)
    if worst >= 90:
        status = "通"
    elif worst >= 70:
        status = "半"
    else:
        status = "断"
    return {"id": "M5", "name": "字段被真正使用", "kind": "share",
            "value": round(worst, 1), "n": 5, "detail": detail,
            "status": status, "fp": fp_measure("M5")}


def m6a(conn, book):
    rows, _ = sig_e4(conn, book)
    return _ratio_row("M6a", "异动方向延续", rows, book)


def m6b(conn, book):
    agree = [0, 0]
    disagree = [0, 0]
    total = parsed = 0
    for asset, ts, action, verification in conn.execute(
            "SELECT target_asset, ts, suggested_action, verification FROM ai_decisions "
            "WHERE verification LIKE '%reviews%'"):
        total += 1
        try:
            v = json.loads(verification or "{}")
        except (TypeError, ValueError):
            continue
        parsed += 1
        dirs = []
        for r in (v.get("reviews") or []):
            a = str((r or {}).get("action") or "").upper()
            if a == "BUY":
                dirs.append(1)
            elif a == "SELL":
                dirs.append(-1)
        if not dirs:
            continue
        score = sum(dirs)
        if score == 0:
            continue
        peer = 1 if score > 0 else -1
        selfd = 1 if str(action or "").upper() == "BUY" else (-1 if str(action or "").upper() == "SELL" else 0)
        if selfd == 0:
            continue
        lab = book.label(asset, ts, None)
        if lab is None:
            continue
        g = agree if selfd == peer else disagree
        g[0] += 1
        g[1] += 1 if lab == selfd else 0
    pa = (agree[1] / agree[0] * 100) if agree[0] else 0.0
    pd = (disagree[1] / disagree[0] * 100) if disagree[0] else 0.0
    rate = (agree[0] / (agree[0] + disagree[0]) * 100) if (agree[0] + disagree[0]) else 0.0
    if agree[0] < 100 or disagree[0] < 100:
        status = "样本不足"
    else:
        status = "一致组更高" if pa - pd >= 5 else ("一致组无增量" if pa - pd > -5 else "不一致组更高")
    return {"id": "M6b", "name": "双重验证", "kind": "ab",
            "agreement_rate": round(rate, 1), "parsed": parsed, "total": total,
            "agree": {"value": round(pa, 2), "n": agree[0], "hits": agree[1]},
            "disagree": {"value": round(pd, 2), "n": disagree[0], "hits": disagree[1]},
            "diff_pp": round(pa - pd, 2), "value": round(pa - pd, 2),
            "n": agree[0] + disagree[0], "status": status, "fp": fp_measure("M6b")}


def m7(conn, book, br=50.0):
    tot = conn.execute("SELECT count(*) FROM reflection_3layer").fetchone()[0]
    n, v = conn.execute(
        "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) FROM reflection_3layer "
        "WHERE layer IN ('买卖判断失误','信息方向错误') AND verdict IN ('验证','否证')").fetchone()
    n, v = int(n or 0), int(v or 0)
    layers = conn.execute(
        "SELECT layer, count(*) FROM reflection_3layer GROUP BY 1 ORDER BY 2 DESC").fetchall()
    subs = conn.execute(
        "SELECT sub_layer, count(*) FROM reflection_3layer GROUP BY 1 ORDER BY 2 DESC LIMIT 12").fetchall()
    row = _pack("M7", "三维错因判准", "ratio", v, n,
                {"total": int(tot), "layers": dict(layers), "sub_layers": dict(subs)})
    if n < 200:
        row["status"] = "样本不足"
    else:
        row["status"] = judge(row["value"], n, row["ci_low"], br, row["ci_high"])
    return row


def m8(conn, book, br=50.0):
    out: Dict[str, Any] = {"id": "M8", "name": "因果闭环", "kind": "chain",
                           "fp": fp_measure("M8")}
    try:
        ec = sqlite3.connect(f"file:{EVAL_DB}?mode=ro", uri=True)
        total, v, f, u = ec.execute(
            "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN verdict='否证' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN verdict='不可分离' THEN 1 ELSE 0 END) "
            "FROM chain_steps_eval WHERE step_idx=3").fetchone()
        v, f, u = int(v or 0), int(f or 0), int(u or 0)
        steps = ec.execute("SELECT count(*), count(DISTINCT chain_id) FROM chain_steps_eval").fetchone()
        ec.close()
        out.update({"step3": {"verify": v, "falsify": f, "unseparable": u,
                              "n": v + f, "value": round((v / (v + f) * 100) if v + f else 0, 2),
                              "ci": [round(x, 1) for x in wilson(v, v + f)]},
                    "steps": int(steps[0]), "chains_with_steps": int(steps[1])})
    except sqlite3.Error as e:
        out["error"] = str(e)
        out["status"] = "不可测"
        return out

    tot, tmpl, mapped = conn.execute(
        "SELECT count(*), sum(CASE WHEN cause LIKE '[覆盖度告警]%' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN asset IS NOT NULL AND asset!='' THEN 1 ELSE 0 END) FROM causal_chains").fetchone()
    nonempty = conn.execute(
        "SELECT count(*) FROM causal_chains WHERE chain_steps IS NOT NULL AND chain_steps!='' "
        "AND chain_steps!='[]'").fetchone()[0]
    out.update({"chains_total": int(tot or 0), "template": int(tmpl or 0),
                "asset_mapped": int(mapped or 0), "prod_steps_nonempty": int(nonempty or 0)})

    n3 = out["step3"]["n"]
    cov = (n3 / int(tot)) * 100 if tot else 0
    out["coverage_pct"] = round(cov, 1)
    out["n"] = n3
    out["value"] = out["step3"]["value"]
    if n3 < 200 or cov < 50:
        out["status"] = "闭环不成立"
    else:
        out["status"] = judge(out["value"], n3, out["step3"]["ci"][0], br, out["step3"]["ci"][1])
    if out["prod_steps_nonempty"] == 0:
        out["status"] = "闭环不成立"
    return out


def m12(conn, book):
    """超额口径重算：未来3日绝对超额移动的 P25/P50/P75，只用事件之前的样本。"""
    # 每资产 3 日前向收益 → 每日期横截面中位数
    fwd: Dict[str, List[float]] = {}
    fwd_of: Dict[Tuple[str, str], float] = {}
    for a, arr in book.series.items():
        for i in range(len(arr) - 3):
            c0, c3 = arr[i][1], arr[i + 3][1]
            if c0 > 0:
                r = c3 / c0 - 1
                fwd_of[(a, arr[i][0])] = r
                fwd.setdefault(arr[i + 3][0], []).append(r)
    med3 = {d: median(v) for d, v in fwd.items() if len(v) >= 3}

    events = []
    for nid, ts, asset, action in conn.execute(
            "SELECT id, ts, asset, action FROM news_events WHERE action IN ('BUY','SELL') "
            "AND ts IS NOT NULL ORDER BY ts"):
        a = _norm_asset(asset)
        if a not in book.index:
            continue
        day = _day(ts)
        if not day:
            continue
        arr = book.series[a]
        entry = next((i for i in range(len(arr)) if arr[i][0] > day), None)
        if entry is None or entry + 3 >= len(arr):
            continue
        base = med3.get(arr[entry + 3][0])
        if base is None:
            continue
        ex = abs(((arr[entry + 3][1] / arr[entry][1]) - 1) - base) * 100
        events.append((int(ts), a, str(action).upper(), ex))

    hist: Dict[Tuple[str, str], List[float]] = {}
    covered = pred = 0
    p50s = []
    for ts, a, act, ex in events:
        prior = hist.get((a, act), [])
        if len(prior) >= 20:
            pred += 1
            lo, p50, hi = _pct(prior, 0.25), _pct(prior, 0.5), _pct(prior, 0.75)
            covered += 1 if lo <= ex <= hi else 0
            p50s.append(p50)
        hist.setdefault((a, act), []).append(ex)
    cov = (covered / pred * 100) if pred else 0.0
    if pred < GATES["min_n"]:
        status = "样本不足"
    elif 45 <= cov <= 55:
        status = "区间合理"
    elif cov > 65:
        status = "区间太宽"
    elif cov < 35:
        status = "区间太窄"
    else:
        status = "接近合理"
    return {"id": "M12", "name": "影响力区间覆盖", "kind": "cover",
            "value": round(cov, 2), "n": pred, "events": len(events),
            "median_p50": round(_pct(p50s, 0.5), 2) if p50s else None,
            "status": status, "fp": fp_measure("M12")}


def _pct(values: List[float], q: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def m13(conn, book):
    rows, ex = sig_e2(conn, book)
    row = _ratio_row("M13", "新闻方向+映射", rows, book)
    tot_dir = len(rows)
    mapped = sum(1 for a, *_ in rows if _norm_asset(a) in book.index)
    row["mapping"] = {"direction_rows": tot_dir, "mapped": mapped,
                      "mapped_pct": round(mapped / tot_dir * 100, 1) if tot_dir else 0.0}
    row["excluded"] = ex
    return row


def m14(conn, book, br=50.0):
    if not ROLE_DB.exists():
        return {"id": "M14", "name": "市场角色", "kind": "ratio", "value": None,
                "n": 0, "status": "不可测", "fp": fp_measure("M14")}
    lc = sqlite3.connect(f"file:{ROLE_DB}?mode=ro", uri=True)
    n, v = lc.execute(
        "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
        "FROM role_labels WHERE verdict IN ('验证','否证')").fetchone()
    roles = lc.execute("SELECT role, count(*) FROM role_labels GROUP BY 1 ORDER BY 2 DESC").fetchall()
    lc.close()
    n, v = int(n or 0), int(v or 0)
    row = _pack("M14", "市场角色", "ratio", v, n,
                {"roles": dict(roles), "missing_roles": ["套利离场"]})
    if n >= 200:
        row["status"] = judge(row["value"], n, row["ci_low"], br, row["ci_high"])
    return row


# ══════════════════════════════════════════════════════════════════
# R6.1 补充项（定义见 SPEC_R6_1.md，跑数后补，不改已判结果）
# ══════════════════════════════════════════════════════════════════
def m2b(conn, book):
    pts = []
    for asset, ts, action, w in conn.execute(
            "SELECT target_asset, ts, suggested_action, signal_weight FROM ai_decisions "
            "WHERE signal_weight IS NOT NULL AND signal_weight > 0 "
            "AND suggested_action IN ('BUY','SELL')"):
        lab = book.label(asset, ts, None)
        if lab is None:
            continue
        hit = 1 if (lab == 1 and action == "BUY") or (lab == -1 and action == "SELL") else 0
        pts.append((float(w), hit))
    pts.sort()
    n = len(pts)
    q = n // 4
    top = pts[-q:] if q else []
    bot = pts[:q]
    pt = (sum(h for _, h in top) / len(top) * 100) if top else 0.0
    pb = (sum(h for _, h in bot) / len(bot) * 100) if bot else 0.0
    if len(top) < 100 or len(bot) < 100:
        status = "样本不足"
    elif pt - pb >= 5:
        status = "权重有分层力"
    elif pt - pb <= -5:
        status = "权重反向"
    else:
        status = "权重无分层力"
    return {"id": "M2b", "name": "权重分层(补充)", "kind": "ab", "supp": True,
            "top": {"value": round(pt, 2), "n": len(top), "hits": sum(h for _, h in top)},
            "bottom": {"value": round(pb, 2), "n": len(bot), "hits": sum(h for _, h in bot)},
            "diff_pp": round(pt - pb, 2), "value": round(pt - pb, 2), "n": n,
            "status": status, "fp": fp_measure("M2b")}


def m5b(conn, book):
    atom_ids = {int(r[0]) for r in conn.execute("SELECT id FROM event_atom")}
    for name in ("event_atoms",):
        try:
            atom_ids |= {int(r[0]) for r in conn.execute(f"SELECT id FROM {name}")}
        except sqlite3.Error:
            pass
    total_ids = found_ids = 0
    snap_total = snap_unavail = px_total = px_ok = 0
    for ids, sin in conn.execute(
            "SELECT event_atom_ids, structured_input FROM ai_decisions"):
        if ids and ids not in ("", "[]", "{}", "null"):
            try:
                lst = json.loads(ids)
                if isinstance(lst, list):
                    for x in lst:
                        total_ids += 1
                        if isinstance(x, int) and x in atom_ids:
                            found_ids += 1
            except (TypeError, ValueError):
                pass
        if not sin or sin in ("", "{}", "[]"):
            continue
        try:
            s = json.loads(sin)
        except (TypeError, ValueError):
            continue
        snaps = s.get("snapshot") or []
        pub = ((s.get("meta") or {}).get("published_at"))
        day = _day(pub) if pub else None
        for it in snaps:
            if not isinstance(it, dict):
                continue
            snap_total += 1
            if str(it.get("status") or "") == "unavailable":
                snap_unavail += 1
                continue
            px = it.get("price")
            a = _norm_asset(it.get("asset"))
            if px is None or day is None or a not in book.index:
                continue
            pos = book.index[a].get(day)
            if pos is None:
                continue
            ref = book.series[a][pos][1]
            px_total += 1
            px_ok += 1 if abs(float(px) / ref - 1) <= 0.01 else 0

    r_atom = (found_ids / total_ids * 100) if total_ids else 0.0
    r_avail = ((snap_total - snap_unavail) / snap_total * 100) if snap_total else 0.0
    r_px = (px_ok / px_total * 100) if px_total else 0.0
    worst = min(r_atom, r_avail, r_px)
    status = "通" if worst >= 90 else ("半" if worst >= 70 else "断")
    return {"id": "M5b", "name": "引用完整性(补充)", "kind": "share", "supp": True,
            "value": round(worst, 1), "n": 3, "status": status,
            "detail": {
                "原子可join": {"value": round(r_atom, 1), "hits": found_ids, "n": total_ids},
                "快照资产可用": {"value": round(r_avail, 1), "hits": snap_total - snap_unavail, "n": snap_total},
                "快照价与日线一致(1%)": {"value": round(r_px, 1), "hits": px_ok, "n": px_total},
            }, "fp": fp_measure("M5b")}


def m6b2(conn):
    total = conn.execute("SELECT count(*) FROM ai_decisions").fetchone()[0]
    total = int(total or 0)
    with_peers = geo2 = directional = dir_with2 = skip = 0
    for action, peers, ver in conn.execute(
            "SELECT suggested_action, peer_reviews, verification FROM ai_decisions"):
        if action in ("BUY", "SELL"):
            directional += 1
        cnt = 0
        if peers and peers not in ("", "[]", "null"):
            try:
                v = json.loads(peers)
                cnt = len(v) if isinstance(v, list) else 1
            except (TypeError, ValueError):
                cnt = 1
        if cnt > 0:
            with_peers += 1
            if cnt >= 2:
                geo2 += 1
                if action in ("BUY", "SELL"):
                    dir_with2 += 1
        if str(ver or "").strip().lower().startswith("skip"):
            skip += 1
    r_cover_all = (geo2 / total * 100) if total else 0.0
    r_cover_dir = (dir_with2 / directional * 100) if directional else 0.0
    status = "在跑" if r_cover_dir >= 50 else ("半" if r_cover_dir >= 20 else "形同虚设")
    return {"id": "M6b2", "name": "双重验证覆盖率(补充)", "kind": "share", "supp": True,
            "value": round(r_cover_dir, 1), "n": directional, "status": status,
            "detail": {"全库覆盖率": round(r_cover_all, 1), "有评审": with_peers, "评审≥2": geo2, "方向决策": directional,
                       "方向且评审≥2": dir_with2, "verification=skip": skip},
            "fp": fp_measure("M6b2")}


def m7b(conn):
    root = BACKEND.parent
    page = (root / "frontend" / "app" / "missed-signals" / "page.tsx").exists()
    nav = (root / "frontend" / "components" / "app-nav.tsx")
    nav_ok = nav.exists() and "missed" in nav.read_text(encoding="utf-8", errors="ignore").lower()
    api = (BACKEND / "src_python" / "api_server.py")
    api_ok = api.exists() and "missed" in api.read_text(encoding="utf-8", errors="ignore").lower()
    rows = conn.execute(
        "SELECT count(*), sum(CASE WHEN missed_signal='1' THEN 1 ELSE 0 END) FROM ai_decisions").fetchone()
    checks = [page, nav_ok, api_ok]
    npass = sum(1 for x in checks if x)
    status = "页面存在" if npass == 3 else ("半" if npass >= 1 else "不存在")
    return {"id": "M7b", "name": "错过信号页(补充)", "kind": "code", "supp": True,
            "value": npass, "n": 3, "status": status,
            "detail": {"页面文件": page, "导航注册": nav_ok, "后端路由": api_ok,
                       "missed_signal=1(被标错过)": {"hits": int(rows[1] or 0), "n": int(rows[0] or 0)}},
            "fp": fp_measure("M7b")}


def m8b(conn):
    tot_miss, upd = conn.execute(
        "SELECT count(*), sum(CASE WHEN updated_at > created_at THEN 1 ELSE 0 END) "
        "FROM causal_chains WHERE miss_count > 0").fetchone()
    tot_miss, upd = int(tot_miss or 0), int(upd or 0)
    tot = conn.execute("SELECT count(*) FROM causal_chains").fetchone()[0]
    rate = (upd / tot_miss * 100) if tot_miss else 0.0
    outcomes = {}
    try:
        for k, v in conn.execute("SELECT verdict, count(*) FROM causal_chain_outcomes GROUP BY 1"):
            outcomes[str(k)] = int(v)
    except sqlite3.Error:
        outcomes = {}
    if tot_miss == 0:
        status = "不可测"
    else:
        status = "有回写" if rate >= 50 else ("弱" if rate >= 20 else "没有回写")
    return {"id": "M8b", "name": "纠正回写(补充)", "kind": "share", "supp": True,
            "value": round(rate, 1), "n": tot_miss, "status": status,
            "detail": {"链总数": int(tot or 0), "有失误的链": tot_miss, "更新过的链": upd,
                       "结局分布": outcomes},
            "fp": fp_measure("M8b")}


# ══════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════
def compute(conn: sqlite3.Connection) -> Dict[str, Any]:
    book = DailyBook(conn)
    br, br_n = base_rate(book)
    out: List[Dict[str, Any]] = [
        m1(conn, book), m2(conn, book), m3a(conn, book), m3b(conn, book),
        {"id": "M3c", "name": "心理预期", "kind": "na", "value": None, "n": 0,
         "status": "不可测", "reason": "无资金费率长序列/持仓结构/期权偏斜", "fp": fp_measure("M3c")},
        m4(conn, book), m5(conn, book), m6a(conn, book), m6b(conn, book),
        {"id": "M6c", "name": "盘口/资金流背景", "kind": "na", "value": None, "n": 0,
         "status": "不可测", "reason": "无订单簿/深度；资金流仅107资产-日且停在10-29", "fp": fp_measure("M6c")},
        m7(conn, book, br), m8(conn, book, br), m12(conn, book), m13(conn, book), m14(conn, book, br),
        m2b(conn, book), m5b(conn, book), m6b2(conn), m7b(conn), m8b(conn),
    ]
    for r in out:
        if r.get("status") == "" and r.get("kind") == "ratio":
            r["status"] = judge(r["value"], r["n"], r["ci_low"], br, r["ci_high"])
        r["baseline"] = round(br, 2)
    return {"version": RULER_VERSION, "fingerprint": fingerprint(),
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "label": LABEL, "gates": GATES,
            "base_rate": {"value": round(br, 2), "n": br_n},
            "price_base": {"assets": len(book.series), "rows_kept": book.chosen_rows,
                           "placeholder_days": book.placeholder_days,
                           "span": [min(v[0][0] for v in book.series.values() if v),
                                    max(v[-1][0] for v in book.series.values() if v)]},
            "results": out}


def print_report(rep: Dict[str, Any]) -> None:
    print("=" * 108)
    print(f"尺子 {rep['version']} · 指纹 {rep['fingerprint']} · {rep['ts']}")
    print(f"实测基准 {rep['base_rate']['value']:.2f}% (n={rep['base_rate']['n']})　"
          f"价格底座 {rep['price_base']['assets']} 品种 / {rep['price_base']['rows_kept']} 根 / "
          f"占位日 {rep['price_base']['placeholder_days']}")
    print("=" * 108)
    print(f"{'ID':<4}{'要求':<5}{'指标':<22}{'值':>9}{'n':>7}  {'判定':<12}指纹")
    for r in rep["results"]:
        v = r.get("value")
        if r["kind"] == "rho":
            shown = f"{v:+.3f}" if v is not None else "—"
        elif r["kind"] in ("ab",):
            shown = f"{v:+.1f}pp" if v is not None else "—"
        elif v is None:
            shown = "—"
        else:
            shown = f"{v:.2f}%"
        req = next(m["req"] for m in MEASURES if m["id"] == r["id"])
        mark = "*" if r.get("supp") else " "
        print(f"{mark}{r['id']:<4}{req:<5}{r['name']:<24}{shown:>9}{r['n']:>7}  {r['status']:<12}{r.get('fp','')}")
    print("-" * 108)
    ok = [r["id"] for r in rep["results"] if r["status"] in ("达标", "通", "区间合理", "K线有增量", "一致组更高")]
    na = [r["id"] for r in rep["results"] if r["status"] == "不可测"]
    print(f"达标/通过：{ok or '无'}　　不可测：{na}")
    print(f"快照：{OUT_DIR / ('r6_' + rep['fingerprint'] + '.json')}")


def run_once() -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return compute(conn)
    finally:
        conn.close()


def selfcheck() -> int:
    global OUT_DIR
    fails = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    print("=" * 84)
    print(f"R6 自检 · 指纹 {fingerprint()}")
    print("=" * 84)
    check("门槛与 R5 完全一致（LABEL 未改）",
          LABEL["entry"] == "first_close_after_cutoff_date"
          and abs(LABEL["exclude_neutral_move"] - 0.0005) < 1e-9)
    check("门槛与 R5 完全一致（GATES 未改）",
          GATES["min_n"] == 200 and GATES["target"] == 55.0
          and abs(GATES["z_ci"] - 1.96) < 1e-9)
    check("指标清单 20 项（15 主项 + 5 补充，含 2 项不可测）", len(MEASURES) == 20)

    origin = OUT_DIR
    OUT_DIR = Path(tempfile.mkdtemp(prefix="r6_selfcheck_"))
    try:
        a = run_once()
        b = run_once()
    finally:
        OUT_DIR = origin

    def strip(rep):
        rows = [{k: r.get(k) for k in ("id", "value", "n", "ci_low", "ci_high", "status", "fp")}
                for r in rep["results"]]
        return json.dumps({"base": rep["base_rate"], "price": rep["price_base"],
                           "rows": rows}, sort_keys=True, ensure_ascii=False)

    check("可复现：连跑两次逐字段一致（含区间）", strip(a) == strip(b))
    print("-" * 84)
    return 0 if not fails else 1


def main() -> int:
    if "--spec" in sys.argv:
        print(json.dumps(MEASURES, ensure_ascii=False, indent=2))
        return 0
    rep = run_once()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snap = OUT_DIR / f"r6_{rep['fingerprint']}.json"
    snap.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT_DIR / "history_r6.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": rep["ts"], "version": rep["version"],
                             "fingerprint": rep["fingerprint"],
                             "results": [{k: r.get(k) for k in ("id", "value", "n", "status", "fp")}
                                         for r in rep["results"]]}, ensure_ascii=False) + "\n")
    print_report(rep)
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    sys.exit(main())
