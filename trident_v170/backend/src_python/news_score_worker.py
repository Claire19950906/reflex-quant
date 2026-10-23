#!/usr/bin/env python3
"""news_score_worker.py — 后台 AI 评分 worker

扫描 news_events 中 score=0 或 action 被污染(媒体名/分类名)的条目,
用关键词打分写回 score + action(HOLD/BUY/SELL/IGNORE). 幂等.

用法:
  python3 news_score_worker.py --once            # 一次性清场 (默认 200 条)
  python3 news_score_worker.py --once --limit 500
  python3 news_score_worker.py --loop            # 持续守护 (默认 5 分钟)
  python3 news_score_worker.py --loop --period 180 --limit 50
"""
import argparse
import json
import sqlite3
import sys
import os
import time
from pathlib import Path

DB_PATH = Path(os.getenv("TRIDENT_DB_PATH") or (
    Path(__file__).resolve().parent.parent / "trident_event_bus.db"
))

BULLISH_KEYWORDS = (
    "突破", "创新高", "新高", "上涨", "飙升", "暴涨", "大涨", "利好", "看多",
    "增长", "加仓", "买入", "增持", "底部", "筑底", "反弹", "回暖", "复苏",
    "rally", "surge", "breakout", "bullish", "long",
)
BEARISH_KEYWORDS = (
    "暴跌", "腰斩", "崩盘", "跌停", "创新低", "新低", "下跌", "大跌", "利空", "看空",
    "撤资", "出逃", "清仓", "减持", "减仓", "闪崩", "恐慌", "衰退",
    "crash", "plunge", "drop", "fall", "bearish", "short", "liquidation",
)
DECISION_VALUES = ("HOLD", "BUY", "SELL", "IGNORE")


def _score_text(text):
    """返回 (text_score [-1,1], bull_hits, bear_hits)"""
    bull = sum(1 for k in BULLISH_KEYWORDS if k in text)
    bear = sum(1 for k in BEARISH_KEYWORDS if k in text)
    if bull + bear == 0:
        return 0.0, bull, bear
    return (bull - bear) / (bull + bear), bull, bear


def _decide_action(s):
    """v303 Q3: 三态显式映射 — |score|<0.3 显式归 HOLD, 杜绝空字符串污染."""
    if s >= 0.3:
        return "BUY"
    if s <= -0.3:
        return "SELL"
    return "HOLD"  # 包含 score=0 (无关键词命中) 和 |score|<0.3 (弱信号) 两种情况


def run_once(limit=200):
    if not DB_PATH.exists():
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # v302+ P2: 加 action IS NULL 分支 — 旧版只看 score=0, 但 score 有值但 action=NULL 的
    # 新闻 (例如外部 API 只写 score 不写 action) 会被漏掉,30 天内积压 56 条.
    # v303 Q3 P0: 加 action='' (空字符串) 分支 — SQLite 里 '' 不等于 NULL, 旧 query 漏抓 58 条
    # techflow/eastmoney 源 (无 bull/bear 关键词命中 → score=0 → 派生 action 也 = '').
    q = (
        "SELECT id, headline, body, action, score, ts, source FROM news_events "
        "WHERE score=0 OR score IS NULL "
        "   OR action IS NULL "
        "   OR action = '' "
        "   OR (action IS NOT NULL AND action NOT IN ('HOLD','BUY','SELL','IGNORE')) "
        "ORDER BY ts DESC LIMIT ?"
    )
    pending = list(conn.execute(q, (limit,)))
    if not pending:
        conn.close()
        return {"ok": True, "scanned": 0, "updated": 0, "action_dist": {}}
    results = []
    for r in pending:
        text = (r["headline"] or "") + " " + (r["body"] or "")
        ts, bull, bear = _score_text(text)
        action = _decide_action(ts)
        conn.execute(
            "UPDATE news_events SET score=?, action=? WHERE id=?",
            (round(ts, 3), action, r["id"]),
        )
        results.append({
            "id": r["id"], "score": round(ts, 3), "action": action,
            "bull": bull, "bear": bear, "old_action": r["action"],
            "ts_iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"])),
        })
    conn.commit()
    dist = {}
    for r in results:
        dist[r["action"]] = dist.get(r["action"], 0) + 1
    conn.close()
    return {
        "ok": True, "scanned": len(pending), "updated": len(results),
        "action_dist": dist, "sample": results[:3],
    }


def run_loop(period_s=300, limit=100):
    print(f"[score-worker] started, period={period_s}s limit={limit}", file=sys.stderr)
    while True:
        try:
            r = run_once(limit=limit)
            ts = time.strftime("%H:%M:%S")
            if r["updated"] > 0:
                print(
                    f"[{ts}] scanned={r['scanned']} updated={r['updated']} "
                    f"dist={r['action_dist']}",
                    file=sys.stderr,
                )
            else:
                print(f"[{ts}] no pending (scanned={r['scanned']})", file=sys.stderr)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] error: {e}", file=sys.stderr)
        time.sleep(period_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--period", type=int, default=300)
    args = ap.parse_args()
    if args.loop:
        run_loop(period_s=args.period, limit=args.limit)
    else:
        r = run_once(limit=args.limit)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
