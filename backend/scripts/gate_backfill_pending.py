"""把积压的"未判定"新闻重新过一遍门卫，补上真实判定。

背景（2026-11-17 事故）：门卫日预算 REALTIME_GATE_DAILY_BUDGET=900 在下午打满，
之后 _gate_budget_allow() 恒 False → gate 恒 None → 所有新闻落 gate_unavailable=2
"未判定"。预算抬到 6000 后门卫恢复，但已被标"未判定"的存量不会自动重判
（仓库里没有 gate 回放调度），所以用本脚本一次性补判。

用法：python3 gate_backfill_pending.py [--limit N] [--dry-run]
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import realtime_filter as rf

    db = os.path.join(ROOT, "trident_event_bus.db")
    conn = sqlite3.connect(db, timeout=30)
    rows = conn.execute(
        "select rowid, content from raw_news "
        "where status='PENDING' and is_noise=0 "
        "and (gate_verdict is null or gate_verdict='' or gate_verdict='未判定') "
        "order by ts desc limit ?", (args.limit,)
    ).fetchall()
    print(f"待补判 {len(rows)} 条", flush=True)

    stat = {"可交易": 0, "排除": 0, "未判定": 0}
    for rid, content in rows:
        title = (content or "")[:80]
        gate = rf._l3_semantic_gate(title, content)
        verdict = (gate or {}).get("verdict") or "未判定"
        stat[verdict] = stat.get(verdict, 0) + 1
        print(f"#{rid} {verdict}", flush=True)
        if args.dry_run or not gate:
            continue
        if verdict == "排除":
            conn.execute(
                "update raw_news set gate_verdict=?, gate_unavailable=0, "
                "is_noise=1, relevance_score=0.0, status='FILTERED' where rowid=?",
                (verdict, rid))
        else:
            conn.execute(
                "update raw_news set gate_verdict=?, gate_unavailable=0 "
                "where rowid=?", (verdict, rid))
        conn.commit()

    print(f"完成：{stat}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
