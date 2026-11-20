#!/usr/bin/env python3
"""Trident 业务产出体检（ops_audit）

目的：进程活着 ≠ 系统正常。本脚本不看进程存活，只看**业务产出是否达标**，
专门捕捉"程序自检全绿、实际已经不出活"的静默失效：

  · 入库量塌方（采集线程僵死 / 源断流）
  · 可交易率异常（门卫规则误杀）
  · 信号产出不足（HOLD 泛滥 / NONE 泛滥）
  · 写库锁冲突（判完丢库）
  · 引擎被反复重启（批次落库作废）

用法：
    python3 scripts/ops_audit.py            # 人类可读报表
    python3 scripts/ops_audit.py --json     # 机器可读
    python3 scripts/ops_audit.py --quiet    # 只在有告警时输出

退出码：0 = 全部正常；1 = 存在告警。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "trident_event_bus.db"
LOG_DIR = Path.home() / ".trident" / "log"

# ── 告警阈值（可用环境变量覆盖）────────────────────────────────────────
TH = {
    "min_intake_ratio": float(os.getenv("AUDIT_MIN_INTAKE_RATIO", "0.6")),  # 今日入库 / 昨日同期
    "min_intake_today": int(os.getenv("AUDIT_MIN_INTAKE_TODAY", "400")),
    "min_tradable_rate": float(os.getenv("AUDIT_MIN_TRADABLE_RATE", "0.05")),
    "min_signals_today": int(os.getenv("AUDIT_MIN_SIGNALS_TODAY", "20")),
    "max_hold_rate": float(os.getenv("AUDIT_MAX_HOLD_RATE", "0.90")),
    "max_none_rate": float(os.getenv("AUDIT_MAX_NONE_RATE", "0.30")),
    "stale_source_hours": float(os.getenv("AUDIT_STALE_SOURCE_HOURS", "2")),
    "max_lock_fails": int(os.getenv("AUDIT_MAX_LOCK_FAILS", "100")),
    "max_engine_exits_1h": int(os.getenv("AUDIT_MAX_ENGINE_EXITS_1H", "2")),
    "max_pending": int(os.getenv("AUDIT_MAX_PENDING", "200")),
}


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)


def scalar(cur, sql: str, args: tuple = ()) -> float:
    row = cur.execute(sql, args).fetchone()
    return (row[0] if row and row[0] is not None else 0)


def collect() -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    conn = connect()
    cur = conn.cursor()
    d = {"ts": now.strftime("%Y-%m-%d %H:%M:%S")}

    # ── 1. 入库量 ────────────────────────────────────────────────
    d["intake_today"] = int(scalar(cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=?", (today,)))
    d["intake_yday_same"] = int(scalar(
        cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=? AND substr(timestamp,12,5)<=?", (yday, hhmm)))
    d["intake_1h"] = int(scalar(
        cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=? AND substr(timestamp,12,2)=?",
        (today, now.strftime("%H"))))

    # ── 2. 门卫/可交易 ───────────────────────────────────────────
    d["tradable_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=? AND gate_verdict='可交易'", (today,)))
    d["excluded_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=? AND gate_verdict='排除'", (today,)))
    d["gate_miss_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM raw_news WHERE substr(timestamp,1,10)=? AND gate_unavailable>0", (today,)))
    d["tradable_rate"] = round(d["tradable_today"] / d["intake_today"], 4) if d["intake_today"] else 0.0

    # ── 3. 信号产出 ──────────────────────────────────────────────
    d["decisions_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM ai_decisions WHERE substr(created_at,1,10)=?", (today,)))
    d["buy_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM ai_decisions WHERE substr(created_at,1,10)=? AND suggested_action='BUY'", (today,)))
    d["sell_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM ai_decisions WHERE substr(created_at,1,10)=? AND suggested_action='SELL'", (today,)))
    d["hold_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM ai_decisions WHERE substr(created_at,1,10)=? AND suggested_action='HOLD'", (today,)))
    d["none_today"] = int(scalar(
        cur, "SELECT COUNT(*) FROM ai_decisions WHERE substr(created_at,1,10)=? AND target_asset='NONE'", (today,)))
    d["signals_today"] = d["buy_today"] + d["sell_today"]
    d["hold_rate"] = round(d["hold_today"] / d["decisions_today"], 4) if d["decisions_today"] else 0.0
    d["none_rate"] = round(d["none_today"] / d["decisions_today"], 4) if d["decisions_today"] else 0.0
    d["last_decision_at"] = cur.execute("SELECT MAX(created_at) FROM ai_decisions").fetchone()[0]

    # ── 4. 队列 ──────────────────────────────────────────────────
    d["queue"] = dict(cur.execute(
        "SELECT status, COUNT(*) FROM raw_news WHERE is_noise=0 GROUP BY status").fetchall())
    d["pending"] = int(d["queue"].get("PENDING", 0))
    d["processing"] = int(d["queue"].get("PROCESSING", 0))

    # ── 5. 源存活（按最后入库时间，不看 source_status 自报）─────────
    cutoff_hhmm = (now - timedelta(hours=TH["stale_source_hours"])).strftime("%H:%M")
    rows = cur.execute(
        "SELECT source, COUNT(*) n, MAX(substr(timestamp,12,5)) last "
        "FROM raw_news WHERE substr(timestamp,1,10)=? GROUP BY source", (today,)).fetchall()
    seen_alive = [r for r in rows if r[2] and r[2] >= cutoff_hhmm]
    stale = [r for r in rows if not r[2] or r[2] < cutoff_hhmm]
    d["sources_active"] = len(seen_alive)
    d["sources_total_today"] = len(rows)
    d["sources_stale"] = [{"source": r[0], "last": r[2]} for r in sorted(stale, key=lambda x: x[2] or "")][:15]

    conn.close()

    # ── 6. 日志侧：写锁失败 / 引擎重启 ────────────────────────────
    d["lock_fails"] = 0
    bridge_log = LOG_DIR / "bridge.out.log"
    if bridge_log.exists():
        try:
            with open(bridge_log, "rb") as f:
                f.seek(max(0, bridge_log.stat().st_size - 8 * 1024 * 1024))
                tail = f.read().decode("utf-8", "ignore")
            d["lock_fails"] = tail.count("database is locked")
        except Exception:
            pass

    d["engine_exits_1h"] = 0
    eng = LOG_DIR / "engine.out.log"
    if eng.exists():
        try:
            with open(eng, "rb") as f:
                f.seek(max(0, eng.stat().st_size - 4 * 1024 * 1024))
                tail = f.read().decode("utf-8", "ignore")
            stamps = re.findall(r"\[ENGINE-EXIT\].*?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", tail)
            for s in stamps:
                try:
                    if now - datetime.strptime(s, "%Y-%m-%d %H:%M:%S") <= timedelta(hours=1):
                        d["engine_exits_1h"] += 1
                except ValueError:
                    pass
        except Exception:
            pass

    return d


def audit(d: dict) -> list:
    """返回告警列表 [(级别, 项目, 说明, 建议)]。"""
    out = []
    ratio = (d["intake_today"] / d["intake_yday_same"]) if d["intake_yday_same"] else 1.0
    if d["intake_yday_same"] and ratio < TH["min_intake_ratio"]:
        out.append(("严重", "入库量塌方",
                    f"今日 {d['intake_today']} 条，昨日同期 {d['intake_yday_same']} 条（{ratio:.0%}）",
                    "查采集线程是否僵死：先看各源最后入库时间，必要时重启 bridge"))
    if d["intake_today"] < TH["min_intake_today"]:
        out.append(("警告", "入库量偏低",
                    f"今日 {d['intake_today']} 条 < 阈值 {TH['min_intake_today']}",
                    "核对源清单与网络（DNS/SNI 阻断），确认是否有源被静默停用"))
    if d["intake_today"] and d["tradable_rate"] < TH["min_tradable_rate"]:
        out.append(("严重", "可交易率异常",
                    f"可交易 {d['tradable_today']}/{d['intake_today']}（{d['tradable_rate']:.1%}），排除 {d['excluded_today']}",
                    "检查门卫硬裁决规则（_harden_verdict）与 kind/timing 标签分布"))
    if d["signals_today"] < TH["min_signals_today"] and datetime.now().hour >= 10:
        out.append(("严重", "信号产出不足",
                    f"今日 BUY+SELL 仅 {d['signals_today']} 条（目标 ≥{TH['min_signals_today']}），HOLD {d['hold_rate']:.1%}、NONE {d['none_rate']:.1%}",
                    "查决策 prompt 的 HOLD 倾向与门卫放行量是否同步塌方"))
    if d["decisions_today"] and d["hold_rate"] > TH["max_hold_rate"]:
        out.append(("警告", "HOLD 泛滥", f"HOLD 占 {d['hold_rate']:.1%}", "检查决策 prompt 是否过度保守（'拿不准就 HOLD'）"))
    if d["decisions_today"] and d["none_rate"] > TH["max_none_rate"]:
        out.append(("警告", "无标的决策过多", f"NONE 占 {d['none_rate']:.1%}", "池路由（news_router）未拦住的池外新闻在空跑"))
    if d["sources_stale"]:
        names = "、".join(f"{s['source']}({s['last']})" for s in d["sources_stale"][:5])
        out.append(("警告", "源断流", f"{len(d['sources_stale'])} 个源超 {TH['stale_source_hours']}h 无入库：{names}",
                    "确认是源本身故障还是采集线程卡死"))
    if d["lock_fails"] > TH["max_lock_fails"]:
        out.append(("严重", "写库锁冲突", f"日志尾部 database is locked × {d['lock_fails']}",
                    "判完的新闻落库失败即丢弃，需加退避重试或合并写入"))
    if d["engine_exits_1h"] > TH["max_engine_exits_1h"]:
        out.append(("严重", "引擎被反复重启", f"近 1 小时 SIGTERM × {d['engine_exits_1h']}",
                    "批次落库在批末一次性提交，重启会整批作废；先停掉外部 kickstart"))
    if d["pending"] > TH["max_pending"]:
        out.append(("警告", "队列积压", f"PENDING {d['pending']} 条 > {TH['max_pending']}",
                    "检查通道熔断/认领是否停滞，消化速度是否跟得上"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    d = collect()
    warns = audit(d)
    d["alerts"] = [{"level": l, "item": i, "detail": s, "hint": h} for l, i, s, h in warns]

    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    elif not a.quiet or warns:
        print(f"══ Trident 业务产出体检  {d['ts']} ══")
        print(f"  入库   今日 {d['intake_today']:>5} 条 | 昨日同期 {d['intake_yday_same']:>5} 条 | 本小时 {d['intake_1h']}")
        print(f"  门卫   可交易 {d['tradable_today']:>5}（{d['tradable_rate']:.1%}）| 排除 {d['excluded_today']:>4} | 待回放 {d['gate_miss_today']}")
        print(f"  信号   决策 {d['decisions_today']:>5} | BUY {d['buy_today']} SELL {d['sell_today']} HOLD {d['hold_today']} | NONE {d['none_today']}")
        print(f"  队列   PENDING {d['pending']} | PROCESSING {d['processing']} | 最后决策 {d['last_decision_at']}")
        print(f"  源     {d['sources_active']}/{d['sources_total_today']} 活跃 | 写锁失败 {d['lock_fails']} | 引擎重启(1h) {d['engine_exits_1h']}")
        if warns:
            print("  ── 告警 " + "─" * 46)
            for lv, item, det, hint in warns:
                print(f"  [{lv}] {item}：{det}")
                print(f"        → {hint}")
        else:
            print("  ✓ 所有产出指标正常")
    # v485：报告类作业不该把"发现了告警"当成"自身运行失败"。
    # 以前 exit 1 → launchd 记 last exit code=1 → audit_jobs 报"退出码 1"，
    # 真正的问题被淹没在"退出码"里。告警已经打印到 stdout，退出码只表示"我跑完了"。
    if warns:
        print(f"[ops-audit] 共 {len(warns)} 条告警（详见上方），本次以 0 退出；"
              f"需要硬失败时请人工看输出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
