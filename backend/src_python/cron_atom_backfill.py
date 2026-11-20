#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron_atom_backfill.py — 事件原子抽取的独立调度入口 (v337)

为什么需要它
------------
`extract_and_persist()` 原来只有两个调用点：
  ① `engine/ingest.py` 的 WebSocket 推送线程（只覆盖实时推送源）
  ② `engine/ai_worker.py` 决策落库时（**必须 LLM 先跑完才有内容可抽**）

于是 LLM 一熔断，原子就彻底停产：24 小时 2,434 条 raw_news，只有 227 条有原子，
其中 220 条还是我手工回填的，AI 通路只贡献 7 条。事件影响力曲线、事件→异动
归因的输入全部断在这里。

本脚本只做**纯规则抽取**（不调 LLM），对象是「已通过过滤器（DONE/PENDING）但
还没原子」的新闻，不限时间窗——既补历史，又持续接新。
过滤器丢掉的 FILTERED 行不处理：那是系统主动判定"不必分析"的终态。

用法:
    python3 cron_atom_backfill.py                 # 补 5,000 条
    python3 cron_atom_backfill.py --limit 500 --json
    python3 cron_atom_backfill.py --status DONE   # 只补已分析的
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db
import event_atom


# 提交节奏（v489）：条数先到 or 时间先到，都是为了让写锁别长期独占。
_COMMIT_EVERY = int(os.getenv("TRIDENT_ATOM_COMMIT_EVERY", "50"))
_COMMIT_EVERY_S = float(os.getenv("TRIDENT_ATOM_COMMIT_SECONDS", "2.0"))
_YIELD_S = float(os.getenv("TRIDENT_ATOM_YIELD_S", "0.02"))


def run(limit: int = 5000, statuses=("DONE", "PENDING"), json_out: bool = False) -> dict:
    conn = db.get_connection()
    conn.row_factory = __import__("sqlite3").Row
    # 回填是后台作业：撞锁时宁可 2 秒就退让重排队，也不要跟着默认 20s 一起排长队
    # （排长队会把 bridge 的交互写请求也拖成"慢事务"）。
    try:
        conn.execute("PRAGMA busy_timeout=2000")
    except Exception:
        pass
    stats = {"processed": 0, "atoms_inserted": 0, "failed": 0, "atoms_before": 0, "atoms_after": 0}
    try:
        t_last_commit = time.time()
        stats["atoms_before"] = conn.execute("SELECT COUNT(*) FROM event_atom").fetchone()[0]
        ph = ",".join("?" * len(statuses))
        # 用 id 游标分页：抽不出原子的行不会进 event_atom，若每次按 "ORDER BY id ASC"
        # 重查，会永远卡在同一批「规则抽不出」的行上（实测连跑 3 次 processed 都是
        # 1491、atoms +0，活锁）。游标保证每轮只往前推进。
        last_id = 0
        t0 = time.time()
        while stats["processed"] < int(limit):
            rows = conn.execute(
                f"""SELECT id, content, ts FROM raw_news
                     WHERE status IN ({ph})
                       AND length(COALESCE(content,'')) > 10
                       AND id > ?
                       AND id NOT IN (SELECT raw_news_id FROM event_atom WHERE raw_news_id IS NOT NULL)
                     ORDER BY id ASC LIMIT 500""",
                (*statuses, last_id),
            ).fetchall()
            if not rows:
                break
            # v489：原来每 200 条才 commit 一次，实测单事务能顶到 3,313 秒 —— 这段时间
            # 整库被这一个回填任务写锁住，其他 writer 只能排队（busy_timeout 20s 一到就丢写，
            # 今日 source_status 丢写的直接来源之一）。改成：
            #   ① 每 N 条（默认 50）或每 T 秒（默认 2s）冲洗一次，先到先算；
            #   ② 每次 commit 后 sleep 一小下把锁让出去，别把交互请求饿死；
            #   ③ 单条耗时 > 5s 记进丢写账本，便于回头查是哪条新闻拖慢了。
            for row in rows:
                last_id = int(row["id"])
                t_item = time.time()
                try:
                    out = event_atom.extract_and_persist(
                        row["content"] or "",
                        raw_news_id=int(row["id"]),
                        default_event_time=int(row["ts"]) if row["ts"] else int(time.time()),
                        event_time_source="rule_backfill_v337",
                        event_time_confidence=0.6,
                        connection=conn,
                    )
                    stats["processed"] += 1
                    stats["atoms_inserted"] += len(out or [])
                except Exception as exc:
                    stats["failed"] += 1
                    if stats["failed"] <= 3:
                        print(f"  [atom-backfill] news={row['id']} FAIL: "
                              f"{type(exc).__name__}: {exc}", flush=True)
                    continue
                dur = time.time() - t_item
                if dur > 5:
                    try:
                        import silent_ledger
                        silent_ledger.record("atom_backfill.slow_item",
                                             f"news={row['id']} 单条 {dur:.1f}s", force=True)
                    except Exception:
                        pass
                if (stats["processed"] % _COMMIT_EVERY == 0
                        or time.time() - t_last_commit >= _COMMIT_EVERY_S):
                    try:
                        db.retry_on_busy(conn.commit)
                    except Exception as exc:
                        stats["commit_failed"] = stats.get("commit_failed", 0) + 1
                        try:
                            import silent_ledger
                            silent_ledger.record("atom_backfill.commit",
                                                 f"{type(exc).__name__}: {str(exc)[:100]}")
                        except Exception:
                            pass
                    t_last_commit = time.time()
                    time.sleep(_YIELD_S)
            try:
                db.retry_on_busy(conn.commit)
            except Exception:
                pass
            t_last_commit = time.time()
        stats["atoms_after"] = conn.execute("SELECT COUNT(*) FROM event_atom").fetchone()[0]
        stats["elapsed_sec"] = round(time.time() - t0, 1)
        stats["pending_left"] = conn.execute(
            f"""SELECT COUNT(*) FROM raw_news
                 WHERE status IN ({ph})
                   AND length(COALESCE(content,'')) > 10
                   AND id > ?
                   AND id NOT IN (SELECT raw_news_id FROM event_atom WHERE raw_news_id IS NOT NULL)""",
            (*statuses, last_id),
        ).fetchone()[0]
    finally:
        conn.close()
    if json_out:
        print(json.dumps(stats, ensure_ascii=False))
    else:
        print(f"[atom-backfill] processed={stats['processed']} "
              f"+{stats['atoms_inserted']} atoms (total {stats['atoms_before']}→{stats['atoms_after']}) "
              f"failed={stats['failed']} left={stats['pending_left']} · {stats['elapsed_sec']}s")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--status", default="DONE,PENDING")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    run(limit=a.limit, statuses=tuple(s.strip() for s in a.status.split(",") if s.strip()),
        json_out=a.json)
