#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v277: FOMC 高等级事件强制升 5 星(impact=high, star=5)。

背景:
  · FF 源给 FOMC Meeting Minutes / Press Conference / Statement 都是 high,
    但 star 只有 4(FF 三档 high/med/low → 4/3/2)。
  · jin10 源给 5 星,但不一定包含会议纪要发布日(纪要日 jin10 不标红)。
  · 用户视角:FOMC Meeting Minutes 与非农/CPI 同级 → 必须 5 星 + 红框。

v283 重要变更:
  · race condition 已修复 —— macro_calendar_ff.py 现在在 INSERT 源头就
    把命中关键词的事件直接写 star=5,不再依赖本 daemon 的二次升级。
  · 本 daemon 现在是 *备份保险*:任何不在 macro_calendar_ff 同步路径的
    来源(如未来 jin10 落库、运维手动 insert)若 star 不足 → 本进程兜底。
  · 仍每小时跑一次,作为 belt-and-suspenders。

策略:
  · 启动时 + 每小时扫一次 macro_calendar 表
  · 凡是 title 命中关键词的 row → 强制 star=5, impact='high'
    (避免影响原源统计)
  · 只动"未来 30 天内"的事件(过期的不动)

调用:
  python3 fomc_minutes_upgrade.py --once
  python3 fomc_minutes_upgrade.py --interval 3600
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db  # noqa: E402

logger = logging.getLogger("fomc_minutes_upgrade")


# 命中即升 5 星的事件关键词(标题模糊匹配)
_FOMC_HIGH_RANK_KEYWORDS = (
    "FOMC Meeting Minutes",         # 会议纪要(纪要日)
    "FOMC Press Conference",       # 发布会(会议后 30min)
    "FOMC Statement",               # 利率决议声明(会议后 30min)
    "FOMC Meeting",                 # 利率决议会议
    "FOMC Member",                  # FOMC 委员讲话(任何委员)
    "FOMC Chair",                   # FOMC 主席讲话
    "Fed Chair Speaks",             # Fed 主席讲话
    "Fed Chair Powell",             # 鲍威尔讲话
    "Fed Funds Rate",               # 联邦基金利率决议
    "Federal Reserve Rate",         # 联邦储备利率
    "Interest Rate Decision",       # 利率决议
    "Rate Decision",                # 利率决定
    "美联储",                       # 中文
    "议息会议",                     # 中文
)


def _should_upgrade(title: str) -> bool:
    if not title:
        return False
    title_l = title.lower()
    return any(k.lower() in title_l for k in _FOMC_HIGH_RANK_KEYWORDS)


def _fetch_candidates(conn: sqlite3.Connection) -> List[Tuple[Any, ...]]:
    """未来 30 天内的 FOMC 高等级事件(star<5 或 impact!='high')。"""
    now = int(time.time())
    hi = now + 30 * 86400
    rows = conn.execute(
        """
        SELECT source, event_id, country, title, event_time, impact, star
          FROM macro_calendar
         WHERE country = 'USD'
           AND event_time BETWEEN ? AND ?
           AND (star < 5 OR impact != 'high')
        """,
        (now, hi),
    ).fetchall()
    out: List[Tuple[Any, ...]] = []
    for r in rows:
        # r[3] = title(位置访问,避免依赖 sqlite3.Row 工厂)
        title = (r[3] or "").strip() if r[3] else ""
        if _should_upgrade(title):
            out.append(r)
    return out


def sync_to_db(verbose: bool = False) -> Dict[str, int]:
    """一次性升级;返回 {"scanned", "upgraded"}。"""
    conn = db.get_connection()
    try:
        candidates = _fetch_candidates(conn)
        upgraded = 0
        for r in candidates:
            # r 可能是 sqlite3.Row 或普通 tuple — 都用位置 3=title 0=source 1=event_id 5=star 4=impact
            src = r[0]
            eid = r[1]
            title = (r[3] or "").strip() if r[3] else ""
            old_impact = r[5]
            old_star = r[6]
            try:
                conn.execute(
                    """
                    UPDATE macro_calendar
                       SET star = 5,
                           impact = 'high'
                     WHERE source = ? AND event_id = ?
                    """,
                    (src, eid),
                )
                upgraded += 1
                if verbose:
                    print(f"  [UPGRADE] {title} (was star={old_star} impact={old_impact})")
            except Exception as exc:
                logger.warning("upgrade failed for %s/%s: %s: %s",
                               src, eid, type(exc).__name__, exc)
        conn.commit()
        if verbose or upgraded:
            print(f"[fomc-minutes-upgrade] scanned={len(candidates)}  upgraded={upgraded}")
        return {"scanned": len(candidates), "upgraded": upgraded}
    except Exception as exc:
        logger.warning("sync_to_db failed: %s: %s", type(exc).__name__, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"scanned": 0, "upgraded": 0}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def daemon_loop(interval_s: int = 3600) -> None:
    """每小时跑一次 upgrade(可被 kill -SIGTERM 优雅退出)。"""
    print(f"[fomc-minutes-upgrade] daemon 启动 (interval={interval_s}s)", flush=True)
    while True:
        try:
            sync_to_db(verbose=False)
        except Exception as exc:
            print(f"[fomc-minutes-upgrade] loop error: {type(exc).__name__}: {exc}", flush=True)
        try:
            time.sleep(interval_s)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FOMC 高等级事件 → 5 星升级器")
    ap.add_argument("--once", action="store_true", help="跑一次就退出")
    ap.add_argument("--interval", type=int, default=3600, help="守护循环间隔(秒)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.once:
        sync_to_db(verbose=args.verbose)
    else:
        daemon_loop(interval_s=args.interval)