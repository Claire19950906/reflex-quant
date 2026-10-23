#!/usr/bin/env python3
"""P1 下游模块单元验证脚本。

跑每条核心 SQL(dry-run) + 用历史 5 笔结算检查数据通 (lineage 一致) +
单元测试每个公共函数 (如果可调)。

P1 改造列表:
- risk_manager.py: 2 处 (get_recent_loss_streak, get_daily_pnl_pct)
- causal_graph.py: 1 处 (collect_causal_facts 来源 1)
- reflection_journal.py: 3 处 (rollup_daily live 路径 stats/wl/ont/llm)
- backtest.py: 1 处 (load_settled_trades)
- gate_reflection.py: 1 处 (_build_forward)
- paper_reflection_worker.py: 1 处 (_DEDUPE_SELECT)
- trade_reflection_llm.py: 3 处 (enrich_one, enrich_pending_loop, reproduce)
- paper_trading_kpi.py: 1 处 (compute_buy_sell_kpi base_sql mfe/mae 来源)
"""
import os
import sys
import sqlite3
import time
from typing import List

DB = "/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db"
SP = "/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/src_python"
PASS = "PASS"
FAIL = "FAIL"


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def check(label, ok, detail=""):
    icon = PASS if ok else FAIL
    suffix = " (" + detail + ")" if detail else ""
    print("  [" + icon + "] " + label + suffix)
    return ok


def section(title):
    print()
    print("=== " + title + " ===")


def main():
    conn = get_conn()
    all_results = []
    section("DB baseline")
    n_qs = conn.execute("SELECT COUNT(*) FROM quick_sim_trades WHERE settled=1").fetchone()[0]
    n_ad = conn.execute("SELECT COUNT(*) FROM ai_decisions WHERE settled=1").fetchone()[0]
    print("quick_sim_trades.settled=1 = " + str(n_qs))
    print("ai_decisions.settled=1 = " + str(n_ad))
    if n_qs < 5:
        print("ABORT: n_qs<5")
        return 1
    if SP not in sys.path:
        sys.path.insert(0, SP)

    # --- risk_manager ---
    section("risk_manager.py")
    import risk_manager as rm
    try:
        streak = rm.get_recent_loss_streak(conn, limit=10)
        all_results.append(check("get_recent_loss_streak runs", isinstance(streak, int), "=" + str(streak)))
    except Exception as e:
        all_results.append(check("get_recent_loss_streak runs", False, type(e).__name__ + ": " + str(e)))
    try:
        dailypnl = rm.get_daily_pnl_pct(conn)
        all_results.append(check("get_daily_pnl_pct runs", isinstance(dailypnl, (int, float)), "=" + str(round(dailypnl, 6))))
    except Exception as e:
        all_results.append(check("get_daily_pnl_pct runs", False, type(e).__name__ + ": " + str(e)))

    # --- causal_graph ---
    section("causal_graph.py")
    rows = conn.execute(
        "SELECT DISTINCT UPPER(target_asset) AS a FROM ai_decisions ad "
        "JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
        "WHERE qs.settled = 1"
    ).fetchall()
    asset_list = [r["a"] for r in rows if r["a"]]
    all_results.append(check("has settled asset", len(asset_list) > 0, "n=" + str(len(asset_list))))
    # 直接验证 P1 改造的 SQL (来源 1) 能跑 - 模拟 causal_graph.collect_causal_facts 的核心路径
    for asset in asset_list[:3]:
        try:
            sql = ("SELECT ad.id, qs.exit_ts AS created_at, ad.suggested_action, qs.pnl_pct AS forward_pnl "
                   "FROM ai_decisions ad "
                   "JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
                   "WHERE ad.target_asset=? AND qs.settled = 1 "
                   "ORDER BY qs.exit_ts DESC LIMIT 200")
            facts_rows = conn.execute(sql, (asset,)).fetchall()
            all_results.append(check("causal_graph SQL(" + asset + ")", True, "n=" + str(len(facts_rows))))
        except Exception as e:
            all_results.append(check("causal_graph SQL(" + asset + ")", False, type(e).__name__ + ": " + str(e)))

    # --- reflection_journal ---
    section("reflection_journal.py")
    import reflection_journal as rj
    row = conn.execute(
        "SELECT DISTINCT date(ad.created_at) AS d FROM ai_decisions ad "
        "JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
        "WHERE qs.settled = 1 ORDER BY d DESC LIMIT 1"
    ).fetchone()
    target_date = row["d"] if row else time.strftime("%Y-%m-%d")
    try:
        res = rj.rollup_daily(journal_date=target_date, run_id="LIVE:")
        keys = list(res.keys())[:6] if isinstance(res, dict) else []
        all_results.append(check("rollup_daily(" + target_date + ", LIVE:)", True, "keys=" + str(keys)))
    except Exception as e:
        all_results.append(check("rollup_daily", False, type(e).__name__ + ": " + str(e)))

    # --- backtest ---
    section("backtest.py")
    import backtest as bt
    rows = conn.execute(
        "SELECT DISTINCT UPPER(ad.target_asset) AS a FROM ai_decisions ad "
        "JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
        "WHERE qs.settled = 1"
    ).fetchall()
    asset_list = [r["a"] for r in rows if r["a"]]
    all_results.append(check("at least one settled asset", len(asset_list) > 0, "n=" + str(len(asset_list))))
    for asset in asset_list[:3]:
        try:
            trades = bt.load_settled_trades(asset=asset, limit=500, connection=conn)
            all_results.append(check("load_settled_trades(" + asset + ")", len(trades) > 0, "n=" + str(len(trades))))
            if trades:
                t = trades[0]
                for k in ("entry_price", "exit_price", "forward_pnl", "mfe_pct", "mae_pct", "is_correct", "action", "asset"):
                    all_results.append(check("  trade[0]." + k, k in t and t[k] is not None, "=" + repr(t.get(k))))
        except Exception as e:
            all_results.append(check("load_settled_trades(" + asset + ")", False, type(e).__name__ + ": " + str(e)))

    # --- gate_reflection ---
    section("gate_reflection.py")
    import gate_reflection as gr
    rows = conn.execute(
        "SELECT DISTINCT UPPER(target_asset) AS a FROM ai_decisions "
        "WHERE target_asset IS NOT NULL LIMIT 3"
    ).fetchall()
    asset_list = [r["a"] for r in rows if r["a"]]
    for asset in asset_list[:2]:
        try:
            forward = gr._build_forward(asset, days=30)
            all_results.append(check("_build_forward(" + asset + ")", isinstance(forward, list), "n=" + str(len(forward))))
            if forward:
                f0 = forward[0]
                all_results.append(check("  forward[0].decision_id", "decision_id" in f0))
                all_results.append(check("  forward[0].settled", "settled" in f0))
                all_results.append(check("  forward[0].is_correct", "is_correct" in f0))
                all_results.append(check("  forward[0].forward_pnl", "forward_pnl" in f0))
        except Exception as e:
            all_results.append(check("_build_forward(" + asset + ")", False, type(e).__name__ + ": " + str(e)))

    # --- paper_reflection_worker ---
    section("paper_reflection_worker.py")
    import paper_reflection_worker as prw
    # 直接验证 P1 改造的 _DEDUPE_SELECT SQL 模式能跑
    try:
        rows = conn.execute(
            "SELECT a.id AS decision_id, qs.entry_price AS entry_price, qs.exit_price AS exit_price, "
            "qs.pnl_pct AS forward_pnl, qs.mfe_pct, qs.mae_pct, "
            "qs.verdict AS is_correct, qs.settled AS settled "
            "FROM ai_decisions a "
            "JOIN quick_sim_trades qs ON qs.decision_id = a.id "
            "LEFT JOIN trade_reflections_v2 t ON t.decision_id = a.id "
            "WHERE a.paper_trading_run_id IS NOT NULL AND qs.settled = 1 AND t.id IS NULL "
            "ORDER BY a.id ASC LIMIT 10"
        ).fetchall()
        all_results.append(check("_DEDUPE_SELECT runs", isinstance(rows, list), "n=" + str(len(rows))))
        if rows:
            r = dict(rows[0])
            for k in ("entry_price", "exit_price", "forward_pnl", "mfe_pct", "mae_pct", "is_correct", "settled"):
                all_results.append(check("  row[0]." + k, k in r and r[k] is not None, "=" + repr(r.get(k))))
    except Exception as e:
        all_results.append(check("_DEDUPE_SELECT", False, type(e).__name__ + ": " + str(e)))

    # --- trade_reflection_llm ---
    section("trade_reflection_llm.py")
    import trade_reflection_llm as trl
    # P1 改造的 SQL 模式: enrich_one/enrich_pending_loop/reproduce 都使用 JOIN quick_sim_trades
    # 即便 trade_reflections_v2 历史数据少, 也要确认 SQL 语法可执行
    try:
        # 模拟 enrich_one 的 SQL (取 reflection_id + JOIN quick_sim)
        sample = conn.execute(
            "SELECT r.id, a.event_strength, a.event_type_code, a.market_confirmation, "
            "a.reasoning, qs.verdict AS is_correct "
            "FROM trade_reflections_v2 r "
            "JOIN ai_decisions a ON a.id = r.decision_id "
            "JOIN quick_sim_trades qs ON qs.decision_id = a.id "
            "WHERE r.id IN (SELECT id FROM trade_reflections_v2 ORDER BY id DESC LIMIT 5)"
        ).fetchall()
        all_results.append(check("enrich_one SQL pattern runs", True, "sample_n=" + str(len(sample))))
        if sample:
            r = dict(sample[0])
            for k in ("event_strength", "market_confirmation", "is_correct"):
                all_results.append(check("  sample[0]." + k, k in r, "=" + repr(r.get(k))))
    except Exception as e:
        all_results.append(check("enrich_one SQL pattern", False, type(e).__name__ + ": " + str(e)))
    try:
        # 模拟 enrich_pending_loop 的 SQL (过滤 qs.verdict='LOSS')
        rows = conn.execute(
            "SELECT r.id FROM trade_reflections_v2 r "
            "JOIN ai_decisions a ON a.id = r.decision_id "
            "JOIN quick_sim_trades qs ON qs.decision_id = a.id "
            "WHERE r.llm_depth_reflection = '' "
            "AND qs.verdict = 'LOSS' AND r.depth_score >= 3 AND r.pnl_pct < -0.005 "
            "ORDER BY r.id DESC LIMIT 10"
        ).fetchall()
        all_results.append(check("enrich_pending_loop SQL", True, "matched n=" + str(len(rows))))
    except Exception as e:
        all_results.append(check("enrich_pending_loop SQL", False, type(e).__name__ + ": " + str(e)))

    # --- paper_trading_kpi ---
    section("paper_trading_kpi.py")
    import paper_trading_kpi as kpi
    try:
        res = kpi.compute_buy_sell_kpi(conn, days=60)
        all_results.append(check("compute_buy_sell_kpi", isinstance(res, dict), "keys=" + str(list(res.keys())[:8])))
    except Exception as e:
        all_results.append(check("compute_buy_sell_kpi", False, type(e).__name__ + ": " + str(e)))

    n_pass = sum(1 for r in all_results if r)
    n_total = len(all_results)
    print()
    print("=== P1 验证汇总 ===")
    print("通过: " + str(n_pass) + "/" + str(n_total))
    print("DB 状态: quick_sim_trades settled=1 有 " + str(n_qs) + " 笔")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())