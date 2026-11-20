# -*- coding: utf-8 -*-
"""cron_tech_signals.py — 定时刷新 tech_signals (K线形态 / 技术异动信号)。

为什么需要这个模块
------------------
``tech_signals`` 的**唯一写入方**是 ``backfill_tech_signals.py``, 而那是一次性
回填脚本 —— 全仓 grep 没有任何 cron / launchd 在周期性调它。结果:

    tech_signals  6459 行, 最后一条 ts = 2026-10-21 08:00

即技术信号(K线形态: ma_support / rsi / macd / 背离 / 支撑测试)从 10-21 起
**再没有新增**。而下游 (归因页的"识别更有价值的信号"、missed_signal 的
pattern_recognition 维度、abnormal_signals 的形态证据)都依赖这张表 ——
它一停, 这些能力就在拿 11 天前的旧形态当"当前形态"。

本模块把一次性回填变成**周期性刷新**, 直接复用 backfill_tech_signals 的
同一套判定逻辑 (不复制、不改口径, 避免两处逻辑漂移):

    · 判定逻辑 —— 复用 backfill_tech_signals.backfill()
    · 幂等     —— UNIQUE(asset,ts) + upsert, 重复跑安全
    · 诚实     —— 数据不足的资产直接跳过并报告, 不写占位行

用法:
    python3 cron_tech_signals.py --dry-run   # 只报告不写库
    python3 cron_tech_signals.py             # 刷新
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contextlib import contextmanager
import sqlite3

DB_PATH = BACKEND / "trident_event_bus.db"


@contextmanager
def write_cursor():
    """可写连接 (与 bridge_server.write_db_cursor 同口径, 不从 bridge 导入)。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


#: SQLite 写锁重试：全量回填会长时间持有写事务，定时任务撞上就报
#: "database is locked" 并整轮作废。busy_timeout 只覆盖等锁，不覆盖"等不到就放弃"。
LOCK_RETRIES = 4
LOCK_BACKOFF_S = (2, 8, 20, 45)


def _backfill_with_retry(conn: sqlite3.Connection, fn, **kwargs):
    """执行回填；遇到 database is locked 按退避重试，其它异常原样抛。"""
    last: Exception | None = None
    for attempt in range(LOCK_RETRIES + 1):
        wait = 0 if attempt == 0 else LOCK_BACKOFF_S[min(attempt - 1, len(LOCK_BACKOFF_S) - 1)]
        if wait:
            time.sleep(wait)
        try:
            return fn(conn, **kwargs)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            if "locked" not in str(exc).lower():
                raise
            last = exc
            if attempt >= LOCK_RETRIES:
                break                # 最后一次也锁着：不再回滚、不再记一条假的重试日志
            try:
                conn.rollback()      # 上一笔事务的锁没释放，先回滚再重试
            except sqlite3.Error:
                pass
            print(f"[tech_signals] 写锁冲突，第 {attempt + 1}/{LOCK_RETRIES} 次重试")
    raise last if last else RuntimeError("backfill failed without exception")


def _has_new_data(conn: sqlite3.Connection) -> bool:
    """tech_signals 的最新 ts 是否落后于 ohlcv_daily 的最新 ts。

    没有新行情数据时整轮可以跳过 —— 避免每天重复算一遍全量资产,
    也避免在没有新数据时把旧 bars 重新写一遍(结果一样, 纯浪费)。
    """
    try:
        last_sig = conn.execute("SELECT MAX(ts) FROM tech_signals").fetchone()[0]
        last_bar = conn.execute("SELECT MAX(ts) FROM ohlcv_daily").fetchone()[0]
    except sqlite3.Error:
        return True          # 表不存在 → 交给 backfill 去建
    if not last_bar:
        return False
    if not last_sig:
        return True
    return int(last_sig) < int(last_bar)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="定时刷新 tech_signals (K线形态信号)")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--force", action="store_true",
                    help="即使没有新行情数据也强制重算")
    args = ap.parse_args()

    from backfill_tech_signals import backfill

    with write_cursor() as conn:
        if not args.force and not args.dry_run and not _has_new_data(conn):
            # 逐小时被 launchd 拉起来、每小时都往日志里写一行"跳过"，日志就废了：
            # 真出问题时翻不到有效信息。只在人手动跑（TTY）时打印，日志里保持安静。
            if sys.stdout.isatty():
                print("[tech_signals] 无新行情数据, 跳过本轮")
            else:
                print("[tech_signals] skip: no new bars")
            return 0
            return 0
        res = _backfill_with_retry(conn, backfill, dry_run=args.dry_run,
                                   ignore_epoch=True)

    print("[tech_signals] assets=%d written=%d table_total=%s" % (
        len(res.get("assets") or []), res.get("written", 0),
        res.get("table_total")))
    for a in (res.get("assets") or []):
        print("  %-10s %-26s bars=%s signals=%s" % (
            a.get("asset"), a.get("status"), a.get("bars"),
            a.get("signals", "-")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
