#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1-B 迁移脚本：重处理历史 SK Hynix 脏数据。

干的事情：
  1. 找出 ai_decisions 中 target_asset=NONE 且对应 raw_news 内容含 SKHYNIX 关键字的行
  2. 调 _rescue_target_asset_from_content 重新识别 target_asset
  3. 把 ai_decisions.target_asset 写回正确 ticker + market_category 用 market_classifier.map_target 重算
  4. 清掉 unclassified_events 对应 news_id 的脏行（如有）
  5. 打日志 + dry-run 选项（默认 dry-run，先观察再 commit）

运行：
  python3 migrate_hynix_v211.py --dry-run    # 只打印，不改库
  python3 migrate_hynix_v211.py              # 实际改库
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "engine")

import db
import market_classifier
from engine.ai_worker import _rescue_target_asset_from_content


def find_hynix_like_news(conn) -> list:
    """找出 ai_decisions 中 target_asset=NONE 但 raw_news 内容含 SKHYNIX/MU/SAMSUNG/TSMC/INTC 关键字的脏数据。"""
    rows = []
    sql = (
        "SELECT ad.id, ad.news_id, ad.target_asset, ad.market_category, ad.status, ad.reasoning, rn.content "
        "FROM ai_decisions ad LEFT JOIN raw_news rn ON ad.news_id = rn.id "
        "WHERE ad.target_asset IN ('NONE', 'OTHER') "
        "  AND (rn.content LIKE '%海力士%' OR rn.content LIKE '%SK Hynix%' "
        "       OR rn.content LIKE '%SKHynix%' OR rn.content LIKE '%hynix%' "
        "       OR rn.content LIKE '%000660%' "
        "       OR rn.content LIKE '%美光%' OR rn.content LIKE '%Micron%' "
        "       OR rn.content LIKE '%三星电子%' OR rn.content LIKE '%Samsung%' "
        "       OR rn.content LIKE '%台积电%' OR rn.content LIKE '%TSMC%' "
        "       OR rn.content LIKE '%英特尔%' OR rn.content LIKE '%Intel%' "
        "       OR rn.content LIKE '%Solidigm%')"
    )
    for row in conn.execute(sql):
        rows.append({
            "ai_id": row[0],
            "news_id": row[1],
            "current_target": row[2] or "NONE",
            "current_cat": row[3] or "",
            "status": row[4] or "",
            "reasoning": row[5] or "",
            "content": row[6] or "",
        })
    return rows


def find_unclassified_for_news(conn, news_ids: list) -> list:
    """找 unclassified_events 表中关联到这些 news_id 的脏行。"""
    if not news_ids:
        return []
    rows = []
    placeholders = ",".join("?" * len(news_ids))
    for row in conn.execute(
        f"SELECT id, news_id, reason FROM unclassified_events WHERE news_id IN ({placeholders})",
        news_ids,
    ):
        rows.append({"ue_id": row[0], "news_id": row[1], "reason": row[2] or ""})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True, help="(默认) 只打印不写库")
    parser.add_argument("--commit", action="store_true", help="实际写库")
    parser.add_argument("--limit", type=int, default=200, help="最多处理条数（防脚本失控）")
    args = parser.parse_args()

    commit = args.commit and not args.dry_run
    if args.commit:
        print("=== WARNING: 实际写库模式 — 所有改动会 commit ===")
    else:
        print("=== DRY-RUN 模式 — 不写库，仅打印修复建议 ===")

    conn = db.get_connection()
    dirty_rows = find_hynix_like_news(conn)
    print(f"找到 {len(dirty_rows)} 条脏数据（ai_decisions.target_asset=NONE/OTHER + 内容含相关 ticker）")

    if not dirty_rows:
        print("无脏数据，退出")
        return

    fixed_count = 0
    no_match_count = 0
    news_ids_to_clean = []
    for row in dirty_rows[: args.limit]:
        content = row["content"]
        cur_target = row["current_target"]
        rescued = _rescue_target_asset_from_content(content, cur_target)
        if rescued and rescued not in ("NONE", "OTHER", cur_target):
            new_cat = market_classifier.map_target(rescued) or ""
            print(
                f"\n  [FIX] ai_id={row['ai_id']:6d} news_id={row['news_id']:6d}"
                f"\n        current target={cur_target}, cat={row['current_cat']!r}"
                f"\n        → rescued: target={rescued}, cat={new_cat!r}"
                f"\n        content: {content[:80]!r}"
            )
            if commit:
                # 1. 更新 ai_decisions
                conn.execute(
                    "UPDATE ai_decisions SET target_asset=?, market_category=?, "
                    "       reasoning='[v211 P1-B 服务端兜底] 修正主品种: ' || ? "
                    "WHERE id=?",
                    (rescued, new_cat, row["reasoning"][:60], row["ai_id"]),
                )
                # 2. 标记 unclassified_events（不删，留痕）
                news_ids_to_clean.append(row["news_id"])
            fixed_count += 1
        else:
            no_match_count += 1
            print(
                f"\n  [SKIP] ai_id={row['ai_id']:6d} news_id={row['news_id']:6d}"
                f" — rescue 返回 {rescued!r}（无法纠错）"
                f"\n        content: {content[:80]!r}"
            )

    # 找 unclassified_events 中匹配的脏行
    ue_rows = find_unclassified_for_news(conn, news_ids_to_clean)
    if ue_rows:
        print(f"\n  unclassified_events 中将标记 resolved: {len(ue_rows)} 条")
        for ue in ue_rows:
            print(f"    ue_id={ue['ue_id']} news_id={ue['news_id']} reason={ue['reason']}")
            if commit:
                conn.execute(
                    "UPDATE unclassified_events SET status='resolved', "
                    "       resolved_at=datetime('now'), "
                    "       resolved_target_asset='SKHYNIX_OR_OTHER', "
                    "       resolved_category='STOCK', "
                    "       resolver='migrate_hynix_v211', "
                    "       note='v211 P1-B 服务端兜底已修正 ai_decisions.target_asset' "
                    "WHERE id=?",
                    (ue["ue_id"],),
                )

    if commit:
        conn.commit()
        print(f"\n=== COMMIT 完成：{fixed_count} 条 ai_decisions 已修正，{len(ue_rows)} 条 unclassified_events 已标记 resolved ===")
    else:
        print(f"\n=== DRY-RUN 完成：{fixed_count} 条可修正，{no_match_count} 条无法纠错（run with --commit 实际写库）===")


if __name__ == "__main__":
    main()
