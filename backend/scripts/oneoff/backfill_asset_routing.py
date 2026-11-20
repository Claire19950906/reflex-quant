"""门卫补分房 —— 把 target_asset=NONE 的决策用规则路由器重新分房。

背景 (v335):
  线上近 7 天 940 条决策里 306 条(33%) target_asset 是 NONE。抽查发现其中约 38%
  其实有明确传导路径(制裁→黄金 / 霍尔木兹油轮→原油 / 亚马逊 AWS→AMZN), 属于
  "新闻没点名、但确实影响某个品种" —— 这批现在被判 NONE, 于是既不进任何房间,
  也进不了样本池(影子账本需要 target_asset 才能取价结算)。

  一条重要边界(用户原话): "不能把这个产品错误地推向一个对它影响不大的标题上"。
  所以只接受 direct(点名) / transmit(强传导) 两档, weak(弱联想)一律不动,
  宁可不分房, 也不乱分。

用法:
  python3 backfill_asset_routing.py --days 14            # 只报
  python3 backfill_asset_routing.py --days 14 --commit   # 落库
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from feeds import news_asset_router as router

DB_PATH = Path(__file__).resolve().parents[2] / "trident_event_bus.db"


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")]
    if "asset_route" not in cols:
        conn.execute("ALTER TABLE ai_decisions ADD COLUMN asset_route TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_asset_route "
                     "ON ai_decisions(asset_route)")


def run(days: int, commit: bool) -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) if not commit \
        else sqlite3.connect(DB_PATH)
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    has_route = "asset_route" in [r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")]
    route_filter = "AND COALESCE(d.asset_route, '') = ''" if has_route else ""
    rows = conn.execute(
        f"""SELECT d.id, n.headline, n.body
             FROM ai_decisions d JOIN news_events n ON n.news_id = d.news_id
            WHERE d.ts >= ?
              AND (d.target_asset IS NULL OR d.target_asset IN ('', 'NONE'))
              {route_filter}
            GROUP BY d.id""", (since,)).fetchall()

    planned = []
    stats = {"direct": 0, "transmit": 0, "weak": 0, "none": 0, "noise": 0}
    for did, headline, body in rows:
        res = router.route(headline or "", body or "")
        stats[res["verdict"]] = stats.get(res["verdict"], 0) + 1
        if res["verdict"] in ("direct", "transmit"):
            top = res["primary"][0]
            planned.append((top["asset"], f"rule:{res['verdict']}:{top['weight']}", did))

    total = len(rows)
    print(f"近 {days} 天待分房 {total} 条 → 可分 {len(planned)} 条 "
          f"({100*len(planned)/total:.1f}%)" if total else "没有待分房记录")
    print(f"  档位: {stats}")
    if planned[:5]:
        print("  抽样:")
        for asset, route, did in planned[:5]:
            print(f"    #{did} → {asset} ({route})")
    if commit and planned:
        ensure_columns(conn)
        conn.executemany(
            "UPDATE ai_decisions SET target_asset=?, asset_route=? "
            "WHERE id=? AND (target_asset IS NULL OR target_asset IN ('', 'NONE'))",
            planned)
        conn.commit()
        print(f"  已写库 {len(planned)} 条 (weak 档保持 NONE, 不打主品种)")
    conn.close()
    return len(planned)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    run(a.days, a.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
