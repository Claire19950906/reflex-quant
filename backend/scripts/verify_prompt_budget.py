#!/usr/bin/env python3
"""v442 验收：主分析单次输入 token 是否降下来 + 门卫成本 + 队列。

用法：
    python3 backend/scripts/verify_prompt_budget.py            # 近 1 小时
    python3 backend/scripts/verify_prompt_budget.py --hours 6
"""
import argparse
import os
import sqlite3
import time

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("TRIDENT_DB", os.path.join(_BACKEND, "trident_event_bus.db"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1)
    a = ap.parse_args()
    since = int(time.time()) - int(a.hours * 3600)
    conn = sqlite3.connect(DB, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")

    def q(sql):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error as e:
            return [("ERR", str(e))]

    print(f"窗口：近 {a.hours:g} 小时（库={DB}）")
    print("\n[主分析单次成本] 期望：tokens_in ≈ 10,800，cached 越高越好")
    for row in q(f"""SELECT call_site, COUNT(*), ROUND(AVG(tokens_in)),
                            ROUND(AVG(cached_tokens)), ROUND(AVG(tokens_out))
                       FROM ai_call_stats
                      WHERE purpose='main' AND ts >= {since}
                      GROUP BY 1 ORDER BY 2 DESC"""):
        print("   ", row)

    print("\n[门卫成本] 期望：每条约 0.8~1.0 次")
    news = q(f"SELECT COUNT(*) FROM raw_news WHERE ts >= {since}")[0][0]
    gate = q(f"SELECT COUNT(*) FROM ai_call_stats WHERE purpose='gate' AND ts >= {since}")[0][0]
    ratio = f"{gate / news:.2f}" if isinstance(news, int) and news else "n/a"
    print(f"    入库新闻 {news} · 门卫调用 {gate} · 次数/条 {ratio}")

    print("\n[归档理由] 期望：无空理由")
    for row in q(f"""SELECT CASE WHEN filter_reason IS NULL OR filter_reason=''
                                THEN '(空)' ELSE filter_reason END r, COUNT(*)
                       FROM raw_news WHERE status='FILTERED' AND ts >= {since}
                      GROUP BY 1 ORDER BY 2 DESC"""):
        print("   ", row)

    print("\n[队列]")
    for row in q("SELECT status, COUNT(*) FROM raw_news GROUP BY 1"):
        print("   ", row)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
