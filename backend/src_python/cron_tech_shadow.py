# -*- coding: utf-8 -*-
"""cron_tech_shadow.py — 技术信号「影子账本」（只记账，不下单）。

为什么需要它
------------
`tech_signal_bridge.py` 早就写好了，但从未进调度，`ai_decisions` 里 0 条技术来源。
在把技术面真正接进决策之前，先跑一段 7~30 天的影子账本：
  · 每个技术信号按桥接规则算出 BUY/SELL/HOLD 与强度；
  · 到期（5 个交易日）后用 ohlcv_daily 独立结算方向命中；
  · 只写自身表，不碰 raw_news / ai_decisions / signal_performance。

表结构 tech_shadow_signals（幂等，UNIQUE(asset, ts)）
----------------------------------------------------
id, asset, ts, trigger_type, action, strength, score, grade, reason,
close, created_at, fwd_ret_5d, hit, settled

表结构 tech_shadow_eval（v452，评估台账，UNIQUE(asset, ts)）
----------------------------------------------------------
asset, ts, verdict, created_at
每个候选只评估一次并记下结论；被拒的也记，避免每小时重算同一批。
verdict ∈ added / hold / not_actionable / regime / no_items。

用法
----
    python3 cron_tech_shadow.py --days 90       # 同步 + 结算 + 出报告
    python3 cron_tech_shadow.py --settle-only   # 只结算已到期
    python3 cron_tech_shadow.py --report-only   # 只重绘报告
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import tech_detector as td
import tech_signal_bridge as tsb

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports" / "tech_shadow"
SAMPLE_TARGET = 30        # 读结论的最小样本；低于这个数只能说"还看不出来"
HORIZON = 5               # 默认结算周期；v453 起按白名单命中的周期记账
MIN_SCORE = 0.58          # B 级起（新口径满值约 0.99）

SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_shadow_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    trigger_type TEXT,
    action      TEXT,
    horizon     INTEGER NOT NULL DEFAULT 5,
    asset_class TEXT,
    vol_bucket  TEXT,
    strength    REAL,
    score       REAL,
    grade       TEXT,
    reason      TEXT,
    close       REAL,
    created_at  TEXT,
    fwd_ret_5d  REAL,
    hit         INTEGER,
    settled     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(asset, ts)
)
"""
INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_tech_shadow_asset_ts "
             "ON tech_shadow_signals(asset, ts)")


#: 评估台账：每个候选只评估一次，**包括被拒的**。
#: 表里没有它时，sync 只能靠 created_at 时间窗反复扫同一批候选：
#: created_at 是整点批量写入的（全库只有几个值），窗口一错开就永远抓不到
#: —— v451 里 tech_shadow_signals 长期 0 行就是这个原因。
EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_shadow_eval (
    asset      TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(asset, ts)
)
"""

#: 单轮评估上限：积压时分多轮吃完，不让一次调度跑成小时级。
MAX_PER_RUN = 800
#: 线上每轮回看的天数。再配合评估台账去重，既不漏也不重复扫。
ONLINE_DAYS = 30


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute(INDEX_SQL)
    conn.execute(EVAL_SCHEMA)
    # v453：老库的 tech_shadow_signals 没有 horizon 列，CREATE IF NOT EXISTS 补不上。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tech_shadow_signals)")}
    for col, ddl in (("asset_class", "TEXT"), ("vol_bucket", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE tech_shadow_signals ADD COLUMN {col} {ddl}")
    if "horizon" not in cols:
        conn.execute("ALTER TABLE tech_shadow_signals "
                     "ADD COLUMN horizon INTEGER NOT NULL DEFAULT 5")
    conn.commit()


def _load_series(conn: sqlite3.Connection) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for asset, ts, close in conn.execute(
            "SELECT asset, ts, close FROM ohlcv_daily "
            "WHERE close IS NOT NULL ORDER BY asset, ts"):
        out[str(asset)].append((int(ts), float(close)))
    return out


def _max_shadow_ts(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(ts) FROM tech_shadow_signals").fetchone()
    return int(row[0]) if row and row[0] else 0


def _asset_class(asset: str) -> str:
    try:
        return tsb.asset_class(asset)
    except Exception:
        return "other"


def _vol_bucket(conn: sqlite3.Connection, asset: str, ts: int):
    try:
        return tsb._vol_cached(conn, asset, ts)
    except Exception:
        return None


def sync(conn: sqlite3.Connection, days: int, min_score: float,
         max_per_run: int = MAX_PER_RUN,
         forced_horizon: int | None = None) -> dict[str, int]:
    """把**尚未评估**的技术信号按桥接规则入账，返回各档计数。

    v452 修法：窗口从 `created_at >= 近1小时` 改成「评估台账 + 时间回溯」。
    旧窗口有三个硬伤：created_at 是整点批量写入的（全库只有 4 个值）；
    信号落库与调度时刻一错开就整轮空转（日志里连续十几轮 skipped=2/added=0）；
    被拒的候选下一轮还要再算一遍，纯烧 CPU。

    现在：每个候选只评估一次，结论（added / hold / not_actionable / regime /
    no_items）写进 `tech_shadow_eval`，既不漏也不重复。
    """
    now = dt.datetime.now()
    if days > 0:
        # 手工回填：按 K 线时间回溯。
        since: int | None = int((now - dt.timedelta(days=days)).timestamp())
    else:
        # 线上：固定回看 ONLINE_DAYS，再靠评估台账去重。
        # 不回看全史 —— 2021 年的老信号对当下决策没有证据价值，扫它纯浪费。
        since = int((now - dt.timedelta(days=ONLINE_DAYS)).timestamp())

    sql = (
        "SELECT t.asset, t.ts, t.close, t.trigger_type, t.score, t.pct_1d, "
        "       t.signal_json "
        "FROM tech_signals t "
        "WHERE t.uses_future = 0 AND t.score >= ? "
        "  AND NOT EXISTS (SELECT 1 FROM tech_shadow_eval e "
        "                  WHERE e.asset = t.asset AND e.ts = t.ts) "
    )
    params: list[Any] = [float(min_score)]
    if since is not None:
        sql += "AND t.ts >= ? "
        params.append(int(since))
    sql += "ORDER BY t.ts ASC LIMIT ?"
    params.append(int(max_per_run))

    rows = conn.execute(sql, tuple(params)).fetchall()
    allow = tsb.load_actionable()
    counts = {"added": 0, "hold": 0, "not_actionable": 0, "regime": 0,
              "no_items": 0}
    now_txt = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for asset, ts, close, ttype, score, pct_1d, sj in rows:
        items = tsb._parse_items(sj)
        verdict = "no_items"
        horizon: int | None = None
        if items:
            # v453：白名单按周期分档，先定「这条信号按几日口径兑现」。
            # 台账里 bb_upper_break 3 日有效、5 日噪声；一套 5 日通吃会把它误杀。
            # v456：分层口径要在这里就用上，否则记账周期和线上决策口径对不上。
            cls = _asset_class(asset)
            vb = _vol_bucket(conn, asset, int(ts))
            horizon = forced_horizon or tsb.resolve_horizon(
                items, allow, cls=cls, vol=vb)
            if horizon is None:
                verdict = "not_actionable"
            else:
                try:
                    resonance = tsb.multi_tf_resonance(conn, asset)
                except Exception:
                    resonance = None
                action, reason, strength = tsb.decide_direction(
                    items, pct_1d, resonance, allow)
                if action == "HOLD":
                    verdict = "hold"
                elif not tsb.regime_ok(conn, asset, int(ts), action, items, allow):
                    verdict = "regime"
                else:
                    verdict = "added"
                    conn.execute(
                        "INSERT OR IGNORE INTO tech_shadow_signals"
                        " (asset, ts, trigger_type, action, horizon, strength,"
                        "  score, grade, reason, close, created_at, asset_class, vol_bucket)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (asset, int(ts), ttype, action, int(horizon),
                         float(strength), float(score), td.grade_of(float(score)),
                         reason, float(close or 0.0), now_txt, cls, vb),
                    )
        counts[verdict] += 1
        conn.execute(
            "INSERT OR IGNORE INTO tech_shadow_eval (asset, ts, verdict, created_at)"
            " VALUES (?,?,?,?)",
            (asset, int(ts), verdict, now_txt),
        )
    conn.commit()
    return counts


def settle(conn: sqlite3.Connection, horizon: int = HORIZON) -> int:
    """结算到期影子信号：方向收益 = (close[t+h]-close[t])/close[t] * 方向。

    v453：每条按自己记账时定的 horizon 结算（`--horizon` 显式指定时才统一覆盖），
    否则 3 日口径的信号会被拖到 5 日，结论跟着漂。
    """
    series = _load_series(conn)
    idx = {a: {t: i for i, (t, _) in enumerate(v)} for a, v in series.items()}
    pending = conn.execute(
        "SELECT id, asset, ts, action, close, horizon "
        "FROM tech_shadow_signals WHERE settled = 0"
    ).fetchall()
    done = 0
    for rid, asset, ts, action, base_close, row_h in pending:
        h = int(row_h or horizon)
        v = series.get(asset)
        m = idx.get(asset, {}).get(int(ts))
        if not v or m is None or m + h >= len(v):
            continue
        exit_close = v[m + h][1]
        base = float(base_close or v[m][1])
        if base <= 0:
            continue
        raw = (exit_close - base) / base * 100.0
        signed = raw if action == "BUY" else -raw
        hit = 1 if signed > 0 else 0
        conn.execute(
            "UPDATE tech_shadow_signals SET fwd_ret_5d = ?, hit = ?, settled = 1 "
            "WHERE id = ?",
            (round(raw, 4), hit, rid),
        )
        done += 1
    conn.commit()
    return done


def _stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0, "hit_rate": None, "avg_ret": None, "avg_signed": None}
    hits = sum(1 for r in rows if r["hit"])
    signed = [(-r["fwd_ret_5d"] if r["action"] == "SELL" else r["fwd_ret_5d"])
              for r in rows if r["fwd_ret_5d"] is not None]
    avg_raw = sum(r["fwd_ret_5d"] for r in rows if r["fwd_ret_5d"] is not None) / n
    return {
        "n": n,
        "hit_rate": round(hits / n * 100, 1),
        "avg_ret": round(avg_raw, 3),
        "avg_signed": round(sum(signed) / len(signed), 3) if signed else None,
    }


def render_report(conn: sqlite3.Connection) -> str:
    settled = conn.execute(
        "SELECT * FROM tech_shadow_signals WHERE settled = 1 ORDER BY ts DESC"
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) FROM tech_shadow_signals WHERE settled = 0"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM tech_shadow_signals").fetchone()[0]
    lines = [
        "# 技术信号影子账本",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
        f"总候选 {total}，已结算 {len(settled)}，待到期 {pending}",
        "",
        f"> 口径：信号当日收盘为基准，按各自记账的周期（1/3/5/10 日）结算；"
        "方向收益 = BUY 取涨幅、SELL 取跌幅；不含成本、不含仓位。",
        "",
    ]

    # 入账漏斗：候选 → 各档结论。转换率低的地方就是链路真正的瓶颈，
    # 只报"总候选 0"看不出是没信号、还是被规则挡掉。
    try:
        funnel = conn.execute(
            "SELECT verdict, COUNT(*) FROM tech_shadow_eval "
            "GROUP BY verdict ORDER BY 2 DESC").fetchall()
    except sqlite3.Error:
        funnel = []
    if funnel:
        evaluated = sum(int(v) for _k, v in funnel)
        lines += ["## 入账漏斗（已评估 %d 条候选）" % evaluated, "",
                  "| 结论 | 条数 | 占比 |", "|---|---|---|"]
        for verdict, cnt in funnel:
            lines.append(f"| {verdict} | {cnt} | {cnt / evaluated * 100:.1f}% |")
        lines.append("")
    # 样本进度：只有 22 条样本时，"命中率 50%"读不出任何东西。与其反复解释
    # "样本不足"，不如把"还差多少、按当前速率多久能到"直接算给用户看。
    if settled or total:
        # 用**信号自身的 ts 跨度**（市场时间）估算速率，而不是 created_at：
        # 回填是同一秒写进来的，created_at 跨度永远是 0 天，估出来的速率会无穷大。
        span_days = 0
        try:
            lo, hi = conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM tech_shadow_signals").fetchone()
            if lo and hi and hi > lo:
                span_days = max(1, (int(hi) - int(lo)) // 86400)
        except Exception:
            span_days = 0
        rate = (len(settled) / span_days) if (span_days and settled) else 0.0
        # 注：这里算的是"每个交易日大约入账几条"，用来给样本积累一个量级感，
        # 不是精确预测——口径一变（放行集调整）速率就会变。
        if len(settled) < SAMPLE_TARGET and rate > 0:
            eta_days = int((SAMPLE_TARGET - len(settled)) / rate)
            eta = (dt.date.today() + dt.timedelta(days=eta_days)).isoformat()
            progress = (f"已结算 {len(settled)} / 目标 {SAMPLE_TARGET} 条，"
                        f"在 {span_days} 个交易日的信号跨度里入账速率约 {rate:.2f} 条/天，"
                        f"按此节奏约 {eta_days} 天后（{eta}）够样本")
        elif len(settled) < SAMPLE_TARGET:
            progress = (f"已结算 {len(settled)} / 目标 {SAMPLE_TARGET} 条；"
                        "账本里只有同一天的信号，暂无法估算积累速率")
        else:
            progress = (f"已结算 {len(settled)} 条，已过 {SAMPLE_TARGET} 条目标，"
                        "可以按形态/周期分组读结论了")
        lines += ["## 样本进度", "", progress, ""]

    if settled:
        overall = _stats(settled)
        lines += ["## 总览", "",
                  "| 样本 | 方向命中率 | 平均原始涨幅 | 平均方向收益 |",
                  "|---|---|---|---|",
                  f"| {overall['n']} | {overall['hit_rate']}% | "
                  f"{overall['avg_ret']}% | {overall['avg_signed']}% |", ""]
        by_action: dict[str, list[sqlite3.Row]] = defaultdict(list)
        by_type: dict[str, list[sqlite3.Row]] = defaultdict(list)
        by_asset: dict[str, list[sqlite3.Row]] = defaultdict(list)
        by_grade: dict[str, list[sqlite3.Row]] = defaultdict(list)
        by_horizon: dict[str, list[sqlite3.Row]] = defaultdict(list)
        # v456 分层计数：样本够了才谈得上"某类资产/某个波动档下这个形态灵不灵"。
        by_class: dict[str, list[sqlite3.Row]] = defaultdict(list)
        by_vol: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in settled:
            by_action[r["action"]].append(r)
            by_type[str(r["trigger_type"])].append(r)
            by_asset[r["asset"]].append(r)
            by_grade[str(r["grade"])].append(r)
            by_horizon[f"{r['horizon']}日"].append(r)
            by_class[str(r["asset_class"] or "unknown")].append(r)
            by_vol[str(r["vol_bucket"] or "unknown")].append(r)
        for title, group in (("按方向", by_action), ("按周期", by_horizon),
                             ("按等级", by_grade), ("按触发类型", by_type),
                             ("按品种类别", by_class), ("按波动状态", by_vol),
                             ("按品种", by_asset)):
            lines += [f"## {title}", "",
                      "| 分组 | 样本 | 命中率 | 平均原始涨幅 | 平均方向收益 |",
                      "|---|---|---|---|---|"]
            for key in sorted(group):
                s = _stats(group[key])
                lines.append(f"| {key} | {s['n']} | {s['hit_rate']}% | "
                             f"{s['avg_ret']}% | {s['avg_signed']}% |")
            lines.append("")
    else:
        lines += ["尚无到期样本 —— 影子账本需要至少 5 个交易日累积。", ""]
    lines += ["## 说明", "",
              "- 本账本只记录技术信号按桥接规则会怎么下单，不写 raw_news / ai_decisions。",
              "- 命中率显著低于 50% 的形态应退回观察池；高于 55% 且样本 ≥30 才考虑放量。",
              ""]
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md = render_report(conn)
    (REPORT_DIR / "latest.md").write_text(md, encoding="utf-8")
    return REPORT_DIR / "latest.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0,
                    help="0=线上模式（回看 ONLINE_DAYS 且只评估未评估过的）；"
                         ">0 用于手工回填指定天数")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--horizon", type=int, default=0,
                    help="强制统一结算周期；0=按白名单命中的周期自动定（1/3/5/10）")
    ap.add_argument("--settle-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    summary: dict[str, Any] = {}
    try:
        _ensure_schema(conn)
        if not args.report_only:
            if not args.settle_only:
                summary.update(sync(conn, args.days, args.min_score,
                                    forced_horizon=args.horizon or None))
            summary["settled"] = settle(conn, horizon=args.horizon or HORIZON)
        if not args.no_write:
            path = write_report(conn)
            summary["report"] = str(path)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
