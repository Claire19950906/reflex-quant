#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reset_derived_epoch.py — 真正的"干净起点"（可回滚）

为什么之前"清不下去"
--------------------
1. clean_epoch.py 只做三件事：review_insights 里 seeded/quick_sim 置 active=0、
   auto_quick_sim 链置 active=0、重算 magnitude_hit。它**一条决策、一条权重、
   一条结算都没删**，所以清完仍是 8,865 条 ai_decisions / 49,606 条权重。
2. 它写入的 data_epoch 表，全项目只有它自己读写（`grep -rn data_epoch` 无第二个
   调用方）→ "纪元"是个装饰品，所有统计照旧读全表。
3. 有 4 个常驻进程 + 多个 5/15/60 分钟 cron 持续写入；其中
   calc_event_move_attribution 是 DELETE+INSERT 整表重建，删完 5 分钟内自己长回来。

本脚本做什么
------------
A. 只删"系统自己的判断与账本"，不碰任何原始事实（行情 / 新闻原文 / 资金流 / COT /
   经济日历 / 因果链定义）。
B. 因果链保留定义与三问，只把 weight 重置为 1.0、hit/miss 清零。
C. 写入新的 data_epoch（带 backup_file 与清理清单），供后续统计按时间切片。
D. 全库先做一致性备份（SQLite backup API，含 WAL），失败即中止。

用法
----
    python3 reset_derived_epoch.py --dry-run     # 只打印将要删除的行数
    python3 reset_derived_epoch.py --apply       # 备份 → 删 → 重置 → 写纪元
回滚
----
    backend/runtime/backup/reset_YYYYMMDD_HHMM.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
BACKUP_DIR = BACKEND / "runtime" / "backup"

# ── 要清空的：系统自己的判断 / 评分 / 账本 / 反思 ──────────────────────────
# 原则：删掉的是"结论"，留下的是"事实"。
CLEAR_TABLES = [
    # 决策与成交
    "ai_decisions", "trade_decisions", "decision_evidence", "decision_errors",
    "decision_ref_archive", "decision_layer_signals", "decision_reflection",
    "decision_narrative", "kline_at_decision_cache",
    # 权重与校准
    "signal_weight_history", "signal_weight_replay_backfill", "signal_weight_cron_log",
    "ai_decisions_impact_backfill", "ai_decisions_sentiment_backfill",
    "judgment_calibration", "gate_evaluations",
    # 因果结算
    "causal_chain_outcomes", "causal_chain_outcome_decisions",
    # 事件 / 归因
    "event_kline_impact", "event_impact_curve", "event_impact_stat",
    "event_atom", "event_atom_decisions",
    "event_move_attribution", "event_move_attribution_candidates",
    "event_move_attribution_pending", "event_move_attribution_rejected",
    "attribution_reviews", "event_attribution_t1", "critical_price_moves",
    # 漏检
    "missed_signals", "missed_signal_scan_state",
    # 反思
    "trade_reflections_v2", "trade_reflections", "reflection_3layer",
    "structured_reflections", "reflection_cycles", "reflection_cycle_log",
    "gate_reflections_cycle", "gate_reflections", "review_insights",
    "review_insights_backup", "review_reports",
    "reflection_daily_journal", "reflection_alert_log", "daily_reflection_summary",
    # 信念 / 画像 / 异动标注
    "beliefs", "belief_history", "belief_edges",
    "abnormal_signals", "abnormal_attributions", "reverse_attributions",
    "anomaly_history", "market_style_snapshot", "factor_snapshots",
    # 信号检测派生
    "tech_signals", "cycle_reversal_signals", "signal_chain_archive",
    "signal_state", "signal_performance",
    # 账本 / 模拟 / 经验
    "shadow_ledger", "event_swing_ledger", "quick_sim_trades", "news_trade_reviews",
    "experience_entries", "experience_applications", "experience_application_daily",
    # 其他派生
    "unclassified_events", "monitor_llm_value", "kg_improvements",
    "strategy_param_adjustments", "strategy_reflections", "policy_patch_queue",
    # ── v460 追加：模拟盘账户 / 影子账本 / 基本面影子台账 ───────────────────
    # 为什么补这一批：v340 那版写于 2026-11-15，之后的 t2 模拟盘、影子账本、
    # 基本面 0.08 的影子埋点都是新表，不在清单里 → 清了判断，账户和账本还留着，
    # 净值曲线跟新口径混在一起，正是"污染"的来源。
    "t2_positions", "t2_orders", "t2_sim_trades", "t2_alerts",
    "t2_anomaly_snapshots", "t2_cost_ledger", "t2_interventions", "t2_param_changes",
    "t2_results", "t2_decision_snapshots", "t2_event_backfill", "t2_source_scores",
    "sim_equity_snapshots", "paper_trading_runs",
    "shadow_ledger", "shadow_reflections", "shadow_degraded", "shadow_reflection_state",
    "fundamental_shadow", "funding_shadow", "funding_shadow_meta",
    "decision_fundamental_snapshot", "decision_outcomes_horizon",
    "decision_horizon_reflection", "decision_actions",
    "causal_veto_shadow",
    "tech_shadow_eval", "tech_shadow_signals", "tech_shadow_signals_v452",
    "tech_shadow_signals_v455", "tech_signals_future_archive",
    "trade_audit_log", "trade_reflections_v2_bak_20261118_092454",
    "decision_errors_backup_v417",
]

# ── 保留：原始事实 / 配置 / 知识结构 ─────────────────────────────────────
KEEP_NOTE = [
    ("raw_news", "新闻原文（接口只能翻最近 1~3 天，删了补不回）"),
    ("news_events", "事件条目与标的映射（LLM 当前不可用，删了会盲）"),
    ("market_ticks", "秒级真实报价"),
    ("kline_bars / ohlcv_intraday / ohlcv_daily", "K 线事实"),
    ("flow_snapshots", "资金费率 / OI / 多空比"),
    ("external_indicators / macro_snapshots", "外部指标与宏观快照"),
    ("cftc_cot_data", "COT 持仓报告"),
    ("econ_events / macro_calendar", "官方财经日历"),
    ("causal_chains", "因果链定义 + 机制/前提/证伪（只重置权重）"),
    ("asset_driver_nodes / asset_driver_edges", "资产驱动知识图谱"),
    ("filter_keywords / ai_prompts / ai_asset_rules / strategies", "配置"),
]


def _counts(db_path: Path, tables: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = {}
    for t in tables:
        if t not in existing:
            out[t] = -1
            continue
        out[t] = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    conn.close()
    return out


def _backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = BACKUP_DIR / f"reset_{stamp}.db"
    src = sqlite3.connect(str(DB), timeout=60)
    try:
        src.execute("PRAGMA wal_checkpoint(PASSIVE)")
        dst = sqlite3.connect(str(dest))
        src.backup(dst)
        dst.close()
    finally:
        src.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.print_help()
        return 2
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2

    before = _counts(DB, CLEAR_TABLES)
    existing = {t: n for t, n in before.items() if n >= 0}
    total = sum(existing.values())
    print(f"库: {DB}")
    print(f"待清空表 {len(existing)} 张，合计 {total:,} 行\n")
    for t, n in sorted(existing.items(), key=lambda kv: -kv[1]):
        print(f"  {n:9,d}  {t}")
    missing = [t for t, n in before.items() if n < 0]
    if missing:
        print(f"\n[跳过] 库中不存在: {', '.join(missing)}")

    chain = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    n_chain = chain.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
    wdist = chain.execute("SELECT COUNT(DISTINCT weight) FROM causal_chains").fetchone()[0]
    chain.close()
    print(f"\n保留因果链定义 {n_chain} 条（当前 {wdist} 个不同权重 → 全部重置为 1.0）")
    print("\n保留事实表：")
    for t, why in KEEP_NOTE:
        print(f"  · {t:<40s} {why}")

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        return 0

    # ── 备份 ────────────────────────────────────────────────────────────
    bak = _backup()
    print(f"\n[1/4] 备份完成 {bak}  ({bak.stat().st_size/1024/1024:.0f} MB)")

    # ── 删除 ────────────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    deleted = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for t in existing:
            try:
                deleted[t] = conn.execute(f"DELETE FROM [{t}]").rowcount
            except sqlite3.OperationalError as exc:
                deleted[t] = f"ERR {exc}"
        ch = conn.execute(
            "UPDATE causal_chains SET weight=1.0, hit_count=0, miss_count=0, "
            "last_verified_ts=NULL").rowcount
        # v460：模拟盘账户必须回到初始本金 —— 只删持仓/成交而不改 cash/equity，
        # 会出现"没有持仓但净值仍是 9991"的鬼状态，之后的收益曲线全错。
        acct = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='t2_account'").fetchone():
            init = 10000.0
            acct = conn.execute(
                "UPDATE t2_account SET cash=?, equity=?, realized_pnl=0, updated_at=?",
                (init, init, int(time.time())),
            ).rowcount
            print(f"      模拟盘账户重置为初始本金 {init:,.0f}（{acct} 行）")
        conn.execute("""CREATE TABLE IF NOT EXISTS data_epoch(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, note TEXT,
            backup_file TEXT)""")
        conn.execute("DELETE FROM data_epoch")
        conn.execute(
            "INSERT INTO data_epoch(started_at, note, backup_file) VALUES(?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "v460 干净起点（非公司基本面接通后）：清空全部交易/判断/评分/账本并重置模拟盘本金，"
             "保留原始事实与因果链定义",
             bak.name))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"[2/4] 已清空 {len(deleted)} 张表；因果链权重重置 {ch} 条")

    # ── 校验 ────────────────────────────────────────────────────────────
    after = _counts(DB, list(existing))
    leftover = {t: n for t, n in after.items() if n > 0}
    print(f"[3/4] 校验：残留非空表 {len(leftover)} 张"
          + (f" → {leftover}" if leftover else "（全部为 0）"))

    # ── 清单 ────────────────────────────────────────────────────────────
    manifest = BACKUP_DIR / f"reset_manifest_{datetime.now():%Y%m%d_%H%M}.json"
    manifest.write_text(json.dumps({
        "at": datetime.now().isoformat(),
        "backup": str(bak),
        "before": existing,
        "deleted": deleted,
        "after": after,
        "chains_weight_reset": ch,
        "account_reset_rows": acct,
        "kept_note": KEEP_NOTE,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[4/4] 清单 {manifest}")

    print("\n注意：文件体积不会自动缩小（未做 VACUUM，避免停机）。"
          "\n后续新数据即为新纪元；三把尺应只统计 data_epoch.started_at 之后的行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
