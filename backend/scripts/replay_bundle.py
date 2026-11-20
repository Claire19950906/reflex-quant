#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据回放口 —— 把真实数据倒进临时库，跑同一套语义检查。

为什么要有它：
    离线自测（offline_selftest.py）用的是合成数据，只能证明"代码路径通"，
    证明不了"这套判断在真实分布上合不合理"（样本量、胜率、方向一致性、
    反思覆盖率都得真数据才能看）。但真数据要读生产库 —— 一边迭代一边跑
    程序既慢又容易互相干扰。

    所以这里留一个口：**你把要验的数据导出成一个 JSON，倒进临时库**，
    库结构和生产一致，检查口径也和线上一致，跑完只出一份报告，不碰生产。

用法：
    cd backend
    python3 scripts/replay_bundle.py --bundle /path/to/bundle.json
    python3 scripts/replay_bundle.py --bundle ... --json /tmp/r.json --md /tmp/r.md
    python3 scripts/replay_bundle.py --bundle ... --keep-db     # 保留临时库方便手查

导出 SQL（在只读连接上跑，按需要裁时间窗）：
    SELECT * FROM ai_decisions WHERE created_at >= datetime('now','-7 day');
    SELECT * FROM quick_sim_trades WHERE created_at >= datetime('now','-7 day');
    SELECT * FROM trade_reflections_v2 WHERE created_at >= datetime('now','-7 day');
    SELECT * FROM signal_performance;
    SELECT * FROM news_events WHERE ts >= strftime('%s','now') - 7*86400;
    SELECT * FROM critical_price_moves WHERE ts >= strftime('%s','now') - 7*86400;
    SELECT * FROM ohlcv_intraday WHERE ts >= strftime('%s','now') - 7*86400;
    SELECT * FROM event_move_attribution WHERE move_ts >= strftime('%s','now') - 7*86400;

bundle.json 结构：
    {
      "meta": {"source": "生产库导出 2026-11-20", "note": "7 天窗口"},
      "tables": {
        "ai_decisions": [ {...一行一对象，列名对得上就行... } ],
        "quick_sim_trades": [ ... ],
        ...
      }
    }

设计原则：
  · 只插 bundle 里出现的列；缺列走库默认值，缺 NOT NULL 列就跳过该表并明说。
  · 不写生产库：临时目录建库，跑完删（--keep-db 可留）。
  · 检查只读，不改数据；结论按样本量分档（n<30 一律标"只能当轶事"）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
for p in (SRC, BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

# migrate 里没有、但线上真实存在的表（采集侧单独建）。bundle 里有就按这个建。
_EXTRA_TABLES = {
    "ohlcv_intraday": (
        "CREATE TABLE IF NOT EXISTS ohlcv_intraday (asset TEXT, interval TEXT, ts INTEGER, "
        "open REAL, high REAL, low REAL, close REAL, volume REAL, trades INTEGER, "
        "closed INTEGER, source TEXT)"
    ),
    "critical_price_moves": (
        "CREATE TABLE IF NOT EXISTS critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, "
        "ts INTEGER, start_price REAL, peak_price REAL, end_price REAL, move_pct REAL, "
        "move_kind TEXT, duration_min INTEGER, algo_score REAL, context_json TEXT, created_at TEXT)"
    ),
}


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.1f}%)" if d else "0/0"


def _verdict_by_n(n: int) -> str:
    if n == 0:
        return "无样本"
    if n < 30:
        return f"{n} 笔，低于 30，只能当轶事"
    return f"{n} 笔，可作参考"


def load_bundle(path: str, db_path: str) -> dict:
    """把 bundle 灌进临时库。返回每张表的装载结果。"""
    import db as _db

    bundle = json.load(open(path, encoding="utf-8"))
    tables = bundle.get("tables") or {}
    if not isinstance(tables, dict) or not tables:
        raise SystemExit("bundle 里没有 tables 段，或 tables 为空")

    conn = sqlite3.connect(db_path)
    _db.migrate(conn)
    for name, ddl in _EXTRA_TABLES.items():
        conn.execute(ddl)
    # t2_* 不在 db.migrate 里（由 trident2.store 单独建）
    try:
        from trident2 import store as _t2store
        _t2store.ensure_schema(conn)
    except Exception as exc:   # 建表失败不该阻断回放，但要在报告里说清楚
        print(f"[replay] t2 schema skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    report = {}
    for name, rows in tables.items():
        if not rows:
            report[name] = {"loaded": 0, "note": "空数组，跳过"}
            continue
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        if not exists:
            report[name] = {"loaded": 0, "note": f"库里没有这张表（{name}），跳过"}
            continue
        cols = {r[1]: r for r in conn.execute(f"PRAGMA table_info({name})")}
        # 只用表里存在的列，其余忽略
        common = [c for c in rows[0].keys() if c in cols]
        if not common:
            report[name] = {"loaded": 0, "note": "bundle 列名与库表列名对不上"}
            continue
        notnull = {c for c, info in cols.items()
                   if info[3] and info[4] is None and c not in ("id",)}
        missing = sorted(notnull - set(common))
        if missing:
            report[name] = {"loaded": 0,
                            "note": f"缺 NOT NULL 列 {missing}，整表跳过（别硬塞假值）"}
            continue
        ph = ",".join("?" for _ in common)
        sql = f"INSERT OR REPLACE INTO {name} ({','.join(common)}) VALUES ({ph})"
        done = skipped = 0
        first_error = ""
        for r in rows:
            try:
                conn.execute(sql, [r.get(c) for c in common])
                done += 1
            except sqlite3.Error as exc:
                skipped += 1
                if not first_error:
                    first_error = f"{type(exc).__name__}: {exc}"
        conn.commit()
        report[name] = {"loaded": done, "skipped": skipped,
                        "first_error": first_error,
                        "columns_ignored": sorted(set(rows[0].keys()) - set(common))}
    conn.close()
    return {"meta": bundle.get("meta", {}), "load": report}


def _table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0])


def _count(conn, sql: str) -> int:
    try:
        return int(conn.execute(sql).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def analyse(db_path: str) -> dict:
    """跑语义检查。全部只读，任何一项缺表都跳过而不是崩。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: dict = {"sections": []}

    def add(name: str, verdict: str, facts: list):
        out["sections"].append({"name": name, "verdict": verdict, "facts": facts})

    # 1) 决策侧
    if _table(conn, "ai_decisions"):
        n = _count(conn, "SELECT COUNT(*) FROM ai_decisions")
        hold = _count(conn, "SELECT COUNT(*) FROM ai_decisions "
                            "WHERE UPPER(COALESCE(suggested_action,''))='HOLD'")
        deg = _count(conn, "SELECT COUNT(*) FROM ai_decisions "
                           "WHERE COALESCE(analysis_degraded,0)=1")
        no_asset = _count(conn, "SELECT COUNT(*) FROM ai_decisions "
                                "WHERE UPPER(COALESCE(target_asset,'')) IN ('','NONE')")
        facts = [f"决策 {n} 条；HOLD {_pct(hold, n)}；降级 {_pct(deg, n)}；"
                 f"无品种 {_pct(no_asset, n)}"]
        verdict = ("HOLD 占绝大多数 → 瓶颈在决策层，先别看收益"
                   if n and hold / n > 0.8 else "决策方向分布正常")
        add("决策分布", verdict, facts)
    else:
        add("决策分布", "缺 ai_decisions，跳过", [])

    # 2) 结算覆盖与胜率
    perf = 0
    if _table(conn, "signal_performance"):
        perf = _count(conn, "SELECT COUNT(*) FROM signal_performance")
        win = _count(conn, "SELECT COUNT(*) FROM signal_performance WHERE is_correct='WIN'")
        loss = _count(conn, "SELECT COUNT(*) FROM signal_performance WHERE is_correct='LOSS'")
        avg = conn.execute("SELECT ROUND(AVG(forward_pnl),4), ROUND(AVG(mfe_pct),4), "
                           "ROUND(AVG(mae_pct),4) FROM signal_performance "
                           "WHERE is_correct IN ('WIN','LOSS')").fetchone()
        add("成绩单结算",
            f"{_verdict_by_n(win + loss)}；胜率 {_pct(win, win + loss)}",
            [f"signal_performance 行 {perf}；WIN {win} / LOSS {loss}",
             f"平均 forward_pnl={avg[0]}% mfe={avg[1]}% mae={avg[2]}%"
             if avg and avg[0] is not None else "文件内无已结算的盈亏"])
    else:
        add("成绩单结算", "缺 signal_performance，跳过", [])

    # 3) 反思覆盖（能不能形成"看反思下次更稳"的闭环）
    if _table(conn, "trade_reflections_v2"):
        rf = _count(conn, "SELECT COUNT(*) FROM trade_reflections_v2")
        dim_null = _count(conn, "SELECT COUNT(*) FROM trade_reflections_v2 "
                                "WHERE COALESCE(dimension,'')=''")
        decided = _count(conn, "SELECT COUNT(DISTINCT decision_id) FROM quick_sim_trades "
                               "WHERE COALESCE(settled,0)=1") if _table(conn, "quick_sim_trades") else 0
        add("反思闭环",
            f"覆盖 {_pct(rf, decided)}；三维度归因空值 {_pct(dim_null, rf)}",
            [f"反思 {rf} 条 / 已结算成交 {decided} 笔",
             "三维度空值率高 → 错误码写入没接上，统计会退化成单一桶"
             if rf and dim_null / rf > 0.5 else "三维度已落库"])
    else:
        add("反思闭环", "缺 trade_reflections_v2，跳过", [])

    # 4) 教训是否带本笔数字（v497 去假阳性的验收点）
    if _table(conn, "decision_reflection"):
        dr = _count(conn, "SELECT COUNT(*) FROM decision_reflection")
        with_num = _count(conn, "SELECT COUNT(*) FROM decision_reflection "
                                "WHERE bias_text LIKE '%本笔%'")
        sug = _count(conn, "SELECT COUNT(*) FROM decision_reflection "
                           "WHERE COALESCE(actionable_suggestion,'')<>''")
        add("教训可执行性",
            "模板化教训占比偏高，进 prompt 的信息量不足"
            if dr and with_num / dr < 0.5 else "教训已带本笔数字",
            [f"decision_reflection {dr} 条；带「本笔」{_pct(with_num, dr)}；"
             f"有可执行建议 {_pct(sug, dr)}"])
    else:
        add("教训可执行性", "缺 decision_reflection，跳过", [])

    # 5) 两套账本
    if _table(conn, "quick_sim_trades") and _table(conn, "t2_positions"):
        qs_n = _count(conn, "SELECT COUNT(*) FROM quick_sim_trades")
        qs_net = conn.execute("SELECT ROUND(COALESCE(SUM(pnl_usdt),0),2) FROM quick_sim_trades").fetchone()[0]
        t2_n = _count(conn, "SELECT COUNT(*) FROM t2_positions WHERE status='closed'")
        t2_net = conn.execute("SELECT ROUND(COALESCE(SUM(realized_pnl),0),2) FROM t2_positions "
                              "WHERE status='closed'").fetchone()[0]
        add("双账本对账",
            "两套口径不能相加，对外只报官方那套（quick_sim）",
            [f"quick_sim {qs_n} 笔 / 净 {qs_net} USDT",
             f"t2 已平 {t2_n} 笔 / 净 {t2_net} USDT"])

    # 6) 事件 ↔ 异动配对
    if _table(conn, "event_move_attribution"):
        em = _count(conn, "SELECT COUNT(*) FROM event_move_attribution")
        linked = _count(conn, "SELECT COUNT(DISTINCT ev_news_id) FROM event_move_attribution")
        moves = _count(conn, "SELECT COUNT(*) FROM critical_price_moves") \
            if _table(conn, "critical_price_moves") else 0
        # rule_verdict 的实际取值是 confirm / partial / reject（不是 consistent），
        # 之前按 'consistent' 统计会恒为 0，看着像"方向全不一致"，实际是口径写错。
        confirm = _count(conn, "SELECT COUNT(*) FROM event_move_attribution "
                               "WHERE rule_verdict='confirm'")
        partial = _count(conn, "SELECT COUNT(*) FROM event_move_attribution "
                               "WHERE rule_verdict='partial'")
        reject = _count(conn, "SELECT COUNT(*) FROM event_move_attribution "
                              "WHERE rule_verdict='reject'")
        if not em:
            verdict = "无归因行：先确认 event_move_attribution 有写入，再谈方向一致性"
        elif (confirm + partial) / em < 0.5:
            verdict = "confirm+partial 合计过低 → 事件与异动的时间/方向对不上，归因不能用于判断"
        else:
            verdict = "方向一致性在可接受区间"
        add("事件归因",
            f"配对覆盖 {_pct(em, moves)}；方向确认 {_pct(confirm, em) if em else '无'}，"
            f"部分 {_pct(partial, em) if em else '无'}，反驳 {_pct(reject, em) if em else '无'}",
            [f"归因行 {em}；涉及事件 {linked}；异动点 {moves}", verdict])
    else:
        add("事件归因", "缺 event_move_attribution，跳过", [])

    conn.close()
    return out


def render_md(report: dict, bundle_path: str) -> str:
    lines = ["# 数据回放报告", ""]
    meta = report.get("meta") or {}
    lines.append(f"- 数据来源：{meta.get('source') or '未标注'}")
    if meta.get("note"):
        lines.append(f"- 说明：{meta['note']}")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- bundle：`{bundle_path}`")
    lines += ["", "## 装载", "", "| 表 | 写入 | 跳过 | 备注 |", "|---|---|---|---|"]
    for name, r in (report.get("load") or {}).items():
        lines.append(f"| {name} | {r.get('loaded', 0)} | {r.get('skipped', 0)} | "
                     f"{r.get('note') or r.get('first_error') or ''} |")
    lines += ["", "## 结论", ""]
    for s in report.get("sections", []):
        lines.append(f"### {s['name']}")
        lines.append("")
        lines.append(f"**{s['verdict']}**")
        lines.append("")
        for f in s["facts"]:
            lines.append(f"- {f}")
        lines.append("")
    lines.append("> 样本量不足时，任何胜率/收益结论都只能当轶事；"
                 "这份报告只说明「口径通不通」，不构成策略判断。")
    return "\n".join(lines)


def run(bundle_path: str, keep_db: bool = False) -> dict:
    d = tempfile.mkdtemp(prefix="trident_replay_")
    db_path = os.path.join(d, "replay.db")
    try:
        report = load_bundle(bundle_path, db_path)
        report["analysis"] = analyse(db_path)
        return report
    finally:
        if keep_db:
            print(f"[replay] 临时库保留在 {db_path}", file=sys.stderr)
        else:
            shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="真实数据回放（只读，不碰生产库）")
    ap.add_argument("--bundle", required=True, help="bundle.json 路径")
    ap.add_argument("--json", default=None, help="把报告写成 JSON")
    ap.add_argument("--md", default=None, help="把报告写成 Markdown")
    ap.add_argument("--keep-db", action="store_true", help="保留临时库")
    args = ap.parse_args()

    report = run(args.bundle, keep_db=args.keep_db)

    print("\n装载：")
    for name, r in report["load"].items():
        print(f"  {name}: 写入 {r.get('loaded', 0)} 跳过 {r.get('skipped', 0)} "
              f"{r.get('note') or ''}")
    print("\n结论：")
    for s in report["analysis"]["sections"]:
        print(f"  [{s['name']}] {s['verdict']}")
        for f in s["facts"]:
            print(f"    - {f}")

    if args.json:
        json.dump({"meta": report["meta"], "load": report["load"],
                   "analysis": report["analysis"]},
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\nJSON → {args.json}")
    if args.md:
        open(args.md, "w", encoding="utf-8").write(render_md(report, args.bundle))
        print(f"Markdown → {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
