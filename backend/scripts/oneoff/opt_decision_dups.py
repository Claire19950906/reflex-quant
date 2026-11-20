"""opt_decision_dups.py —— 决策重复分析审计（只读）

背景：认领 SQL 原来只看 raw_news.status='PENDING'，不看"这条新闻是不是已经
产出过决策"。落库成功但状态没置 DONE（只有 done_news_ids 里的才置）时，
看门狗 15 分钟回收 PROCESSING → 回 PENDING → 同一条新闻被重新分析一遍、
决策再写一遍。同一份 LLM 输出被反复计分。

本脚本量化三件事：
  1. (news_id, target_asset) 级别的重复组 / 行数 / 占比
  2. 完全复制品（weight+action+score 逐位相同）的规模
  3. 去重后 S12（LLM 兜底占比）会变成多少 —— 现有分母被复制品虚高

用法：python3 opt_decision_dups.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "trident_event_bus.db"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    total = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]

    groups = conn.execute(
        "SELECT COUNT(*), SUM(c) FROM ("
        "  SELECT COUNT(*) c FROM ai_decisions WHERE news_id IS NOT NULL"
        "  GROUP BY news_id, target_asset HAVING c>1)").fetchone()
    g, rows = groups[0], int(groups[1] or 0)
    print(f"① 重复组 (news_id, target_asset): {g} 组 · {rows} 行"
          f" ({rows/max(1,total):.1%} of {total})")

    exact = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT COUNT(*) c FROM ai_decisions WHERE news_id IS NOT NULL"
        "  GROUP BY news_id, target_asset, suggested_action,"
        "           ROUND(sentiment_score,3), ROUND(signal_weight,4)"
        "  HAVING c>1)").fetchone()[0]
    exact_rows = conn.execute(
        "SELECT SUM(c) FROM ("
        "  SELECT COUNT(*) c FROM ai_decisions WHERE news_id IS NOT NULL"
        "  GROUP BY news_id, target_asset, suggested_action,"
        "           ROUND(sentiment_score,3), ROUND(signal_weight,4)"
        "  HAVING c>1)").fetchone()[0] or 0
    print(f"② 完全复制品: {exact} 组 · {int(exact_rows)} 行"
          f"（多余 {int(exact_rows)-exact} 行是可去掉的）")

    # 去重（每个 news+asset 只留最早一条）后的 S12
    uniq = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions d WHERE d.news_id IS NOT NULL"
        " AND d.id IN (SELECT MIN(id) FROM ai_decisions"
        "              GROUP BY news_id, target_asset)").fetchone()[0]
    # S12 同口径（与 capability_scorecard 的兜底判定一致）：看 reasoning_path
    fb = ("COALESCE(d.reasoning_path,'') LIKE '%核心监控品种%'"
          " OR COALESCE(d.reasoning_path,'') LIKE '%LLM 未返回可用分析%'"
          " OR COALESCE(d.reasoning_path,'') LIKE '%服务端兜底%'")
    deg = conn.execute(
        f"SELECT COUNT(*) FROM ai_decisions d WHERE d.news_id IS NOT NULL"
        f" AND d.id IN (SELECT MIN(id) FROM ai_decisions"
        f"              GROUP BY news_id, target_asset)"
        f" AND ({fb})").fetchone()[0]
    deg_now = conn.execute(
        f"SELECT COUNT(*) FROM ai_decisions d WHERE d.news_id IS NOT NULL AND ({fb})"
    ).fetchone()[0]
    n_now = conn.execute("SELECT COUNT(*) FROM ai_decisions WHERE news_id IS NOT NULL").fetchone()[0]
    print(f"③ S12 去重前 {deg_now}/{n_now} = {deg_now/max(1,n_now):.1%}"
          f" → 去重后 {deg}/{uniq} = {deg/max(1,uniq):.1%}")

    top = conn.execute(
        "SELECT news_id, target_asset, COUNT(*) c, MIN(target_asset) "
        "FROM ai_decisions WHERE news_id IS NOT NULL"
        " GROUP BY news_id, target_asset ORDER BY c DESC LIMIT 5").fetchall()
    print("\n重复最多的 5 组:")
    for r in top:
        print(f"  news {r[0]} · {r[1]} · {r[2]} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
