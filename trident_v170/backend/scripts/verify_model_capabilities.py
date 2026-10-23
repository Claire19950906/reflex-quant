#!/usr/bin/env python3
"""验证 AI 模型 3 项核心能力 (量化、可重复):

1. 筛选有效交易信号 — news → ai_decision → quick_sim_trade 三级漏斗转化率
2. 判断涨跌的正确性 — 整体胜率 / BUY vs SELL 胜率 / 平均 forward_pnl / MFE-MAE
3. 判断多空趋势的能力 — 事件方向 vs AI 决策方向一致性 / 跨资产胜率矩阵

用法:
    python3 scripts/verify_model_capabilities.py
"""
import sys
sys.path.insert(0, '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/src_python')
import sqlite3
import db

c = db.get_connection()
c.row_factory = sqlite3.Row


def banner(s):
    print()
    print('=' * 72)
    print(s)
    print('=' * 72)


banner('能力 1: 筛选有效交易信号 (漏斗转化率)')

n_news = c.execute('SELECT COUNT(*) AS n FROM news_events').fetchone()['n']
n_dec = c.execute('SELECT COUNT(*) AS n FROM ai_decisions').fetchone()['n']
n_qs = c.execute('SELECT COUNT(*) AS n FROM quick_sim_trades').fetchone()['n']
print(f'  news_events          : {n_news:>6}')
print(f'  ai_decisions         : {n_dec:>6}  ({n_dec/max(n_news,1)*100:.2f}% of news)')
print(f'  quick_sim_trades     : {n_qs:>6}  ({n_qs/max(n_dec,1)*100:.2f}% of decisions)')

print()
print('  ai_decisions status x suggested_action:')
rows = c.execute("""
    SELECT status, suggested_action, COUNT(*) AS n
      FROM ai_decisions
     GROUP BY status, suggested_action
     ORDER BY status, n DESC
""").fetchall()
for r in rows:
    print(f'    status={str(r["status"])[:18]:<20} '
          f'action={str(r["suggested_action"])[:14]:<16} {r["n"]:>5}')

print()
gate_stats = c.execute("""
    SELECT
      SUM(CASE WHEN gate_passed=1 THEN 1 ELSE 0 END) AS passed,
      SUM(CASE WHEN gate_passed=0 THEN 1 ELSE 0 END) AS blocked,
      SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
      COUNT(*) AS total
    FROM quick_sim_trades
""").fetchone()
print(f'  quick_sim gate_passed : {gate_stats["passed"] or 0}/{gate_stats["total"]}')
print(f'  quick_sim gate_blocked: {gate_stats["blocked"] or 0}/{gate_stats["total"]}')
print(f'  quick_sim settled    : {gate_stats["settled"] or 0}/{gate_stats["total"]}')

conv_news_dec = n_dec / max(n_news, 1)
conv_dec_qs = n_qs / max(n_dec, 1)
print()
print('  验收:')
print(f'    news->dec 转化率 = {conv_news_dec*100:.2f}% '
      f'(期望 1-10%; {"OK" if 0.01 <= conv_news_dec <= 0.10 else "FAIL"})')
print(f'    dec->qs 落地率  = {conv_dec_qs*100:.2f}% '
      f'(期望 1-30%; {"OK" if conv_dec_qs <= 0.30 else "FAIL"})')


banner('能力 2: 判断涨跌的正确性 (胜率分析)')

overall = c.execute("""
    SELECT is_correct, COUNT(*) AS n,
           AVG(forward_pnl) AS avg_pnl,
           AVG(mfe_pct) AS avg_mfe,
           AVG(mae_pct) AS avg_mae,
           AVG(holding_horizon_minutes) AS avg_horizon
    FROM ai_decisions
    WHERE is_correct IS NOT NULL AND is_correct != ''
    GROUP BY is_correct
""").fetchall()
print('  总体 (is_correct x 统计):')
for r in overall:
    print(f'    is_correct={str(r["is_correct"]):<12} n={r["n"]:>4}  '
          f'avg_pnl={r["avg_pnl"] or 0:+.3f}%  '
          f'mfe={r["avg_mfe"] or 0:.2f}%  '
          f'mae={r["avg_mae"] or 0:.2f}%  '
          f'horizon={r["avg_horizon"] or 0:.0f}min')

print()
print('  多空胜率 (BUY vs SELL, is_correct=WIN 视为胜):')
dir_stats = c.execute("""
    SELECT suggested_action,
            COUNT(*) AS total,
            SUM(CASE WHEN is_correct='WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN is_correct='LOSS' THEN 1 ELSE 0 END) AS losses,
            CAST(SUM(CASE WHEN is_correct='WIN' THEN 1 ELSE 0 END) AS REAL)
              / NULLIF(COUNT(*), 0) AS win_rate,
            AVG(forward_pnl) AS avg_pnl
    FROM ai_decisions
    WHERE suggested_action IN ('BUY','SELL')
      AND is_correct IS NOT NULL AND is_correct != ''
    GROUP BY suggested_action
""").fetchall()
for r in dir_stats:
    print(f'    {r["suggested_action"]:<8} n={r["total"]:>3}  '
          f'wins={r["wins"]:>3} losses={r["losses"]:>3}  '
          f'win_rate={r["win_rate"] or 0:.1%}  '
          f'avg_pnl={r["avg_pnl"] or 0:+.3f}%')

print()
print('  按 sentiment_score 置信度分桶:')
bucket_stats = c.execute("""
    SELECT
      CASE
        WHEN sentiment_score >= 0.7 THEN 'high (>=0.7)'
        WHEN sentiment_score >= 0.4 THEN 'mid  (0.4-0.7)'
        WHEN sentiment_score >= 0   THEN 'low  (0-0.4)'
        ELSE 'neg (<0)'
      END AS bucket,
      COUNT(*) AS n,
      CAST(SUM(CASE WHEN is_correct='WIN' THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0) AS win_rate,
      AVG(forward_pnl) AS avg_pnl
    FROM ai_decisions
    WHERE sentiment_score IS NOT NULL
      AND is_correct IS NOT NULL AND is_correct != ''
    GROUP BY bucket
    ORDER BY bucket
""").fetchall()
for r in bucket_stats:
    print(f'    {r["bucket"]:<22} n={r["n"]:>3}  '
          f'win_rate={r["win_rate"] or 0:.1%}  '
          f'avg_pnl={r["avg_pnl"] or 0:+.3f}%')

buy_row = next((r for r in dir_stats if r["suggested_action"] == "BUY"), None)
sell_row = next((r for r in dir_stats if r["suggested_action"] == "SELL"), None)
print()
print('  验收:')
if buy_row and sell_row:
    overall_wr = ((buy_row["wins"] + sell_row["wins"])
                  / max(buy_row["total"] + sell_row["total"], 1))
    print(f'    总胜率 = {overall_wr:.1%} (期望 45%-70%)')

print()
print('  quick_sim_trades 实战胜率 (verdict):')
qs_verdicts = c.execute("""
    SELECT verdict, COUNT(*) AS n,
           AVG(pnl_pct) AS avg_pnl,
           AVG(mae_pct) AS avg_mae,
           AVG(mfe_pct) AS avg_mfe
    FROM quick_sim_trades
    WHERE verdict IS NOT NULL
    GROUP BY verdict
""").fetchall()
for r in qs_verdicts:
    print(f'    verdict={str(r["verdict"]):<14} n={r["n"]:>3}  '
          f'avg_pnl={r["avg_pnl"] or 0:+.3f}%  '
          f'mfe={r["avg_mfe"] or 0:.2f}%  mae={r["avg_mae"] or 0:.2f}%')


banner('能力 3: 判断多空趋势 (事件方向 vs AI 决策一致性)')

# 3.0 event_atom 方向覆盖率 (期望 bullish/bearish 多于 unknown)
dir_break = c.execute("""
    SELECT direction, COUNT(*) AS n
    FROM event_atom
    GROUP BY direction
""").fetchall()
print('  event_atom.direction 分布 (期望 mostly unknown 是 LLM 没填):')
for r in dir_break:
    print(f'    {str(r["direction"]):<14} {r["n"]:>4}')
total_atom = sum(r["n"] for r in dir_break)
known_atom = sum(r["n"] for r in dir_break
                 if r["direction"] in ("bullish", "bearish"))
known_rate = known_atom / max(total_atom, 1)
print(f'    已识别方向比例: {known_rate:.1%} '
      f'({"OK" if known_rate >= 0.5 else "FAIL"} 期望 >= 50%)')

print()
print('  事件方向 vs AI 决策方向 (LIKE 多种匹配模式):')
trend_stats = c.execute("""
    SELECT e.direction AS event_dir,
           d.suggested_action AS ai_action,
           COUNT(*) AS n,
           SUM(CASE WHEN d.is_correct='WIN' THEN 1 ELSE 0 END) AS wins,
           CAST(SUM(CASE WHEN d.is_correct='WIN' THEN 1 ELSE 0 END) AS REAL)
             / NULLIF(COUNT(*), 0) AS win_rate
    FROM ai_decisions d, event_atom e
    WHERE (
            (d.event_atom_ids LIKE '[' || e.id || ']')
         OR (d.event_atom_ids LIKE '[' || e.id || ',%')
         OR (d.event_atom_ids LIKE '%,' || e.id || ']')
         OR (d.event_atom_ids LIKE '%,' || e.id || ',%')
        )
      AND d.event_atom_ids LIKE '[%' AND d.event_atom_ids LIKE '%]'
      AND d.suggested_action IN ('BUY','SELL')
      AND e.direction IS NOT NULL AND e.direction != ''
      AND e.direction != 'unknown'
      AND d.is_correct IS NOT NULL AND d.is_correct != ''
    GROUP BY e.direction, d.suggested_action
    ORDER BY n DESC
""").fetchall()
for r in trend_stats:
    print(f'    event={str(r["event_dir"]):<10} ai={r["ai_action"]:<8} '
          f'n={r["n"]:>3}  win_rate={r["win_rate"] or 0:.1%}')

print()
print('  资产 x 方向 胜率矩阵 (n>=3):')
asset_stats = c.execute("""
    SELECT target_asset, suggested_action,
           COUNT(*) AS n,
           CAST(SUM(CASE WHEN is_correct='WIN' THEN 1 ELSE 0 END) AS REAL)
             / NULLIF(COUNT(*), 0) AS win_rate,
           AVG(forward_pnl) AS avg_pnl
    FROM ai_decisions
    WHERE suggested_action IN ('BUY','SELL')
      AND is_correct IS NOT NULL AND is_correct != ''
    GROUP BY target_asset, suggested_action
    HAVING n >= 3
    ORDER BY n DESC
    LIMIT 15
""").fetchall()
for r in asset_stats:
    print(f'    {str(r["target_asset"]):<8} {r["suggested_action"]:<8}  '
          f'n={r["n"]:>3}  win_rate={r["win_rate"] or 0:.1%}  '
          f'avg_pnl={r["avg_pnl"] or 0:+.3f}%')

total_with_atom = c.execute("""
    SELECT COUNT(*) AS n
    FROM ai_decisions
    WHERE event_atom_ids LIKE '[%' AND event_atom_ids LIKE '%]'
      AND event_atom_ids IS NOT NULL AND event_atom_ids != ''
""").fetchone()['n']
print()
print(f'  关联 event_atom_ids 的 ai_decisions: {total_with_atom}')
print()
print('  验收 (event_dir->ai_action 一致率):')
if trend_stats:
    consistent = sum(
        r["n"] for r in trend_stats
        if (r["event_dir"] == "bullish" and r["ai_action"] == "BUY")
        or (r["event_dir"] == "bearish" and r["ai_action"] == "SELL")
    )
    all_n = sum(r["n"] for r in trend_stats)
    rate = consistent / max(all_n, 1)
    print(f'    实测: {consistent}/{all_n} = {rate:.1%} '
          f'{"OK" if rate >= 0.60 else "FAIL"}')


banner('总结')
print('  能力 1: 漏斗转化率 -- news->dec->qs')
print('  能力 2: 胜率分布 -- 总体 / 多空 / 置信度 / 实战')
print('  能力 3: 趋势一致性 -- event_dir vs ai_action 跨资产')

c.close()