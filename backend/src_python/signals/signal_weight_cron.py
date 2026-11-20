# -*- coding: utf-8 -*-
"""
v20251014 signal_weight_cron

Background task that re-evaluates signal_weight for all recent decisions every
5 minutes. Two main effects:

  1. reaction_mult update — picks up any new abnormal_signals triggered in
     the 5min window since the last cron tick. Down-weights "cold signals"
     (signal sent but market didn't move).

  2. time_mult decay — naturally decreases weight for older signals (handled
     inside compute_signal_weight, no special logic needed here).

  3. cold_signal flag — sets ai_decisions.cold_signal=1 for stale signals
     so frontend can gray them out (or paper trading can skip them).

Usage:
    # One-shot (manual / cron entry-point)
    python3 signal_weight_cron.py --once

    # Daemon mode (run every 5min, never exit)
    python3 signal_weight_cron.py --loop --interval 300

Cron entry (recommended):
    */5 * * * * cd /path/to/backend/src_python && python3 signal_weight_cron.py --once >> logs/signal_weight_cron.log 2>&1

Performance:
    ~10ms per decision on a 1k decision batch. Default 24h window = ~500-2000
    decisions = 5-20 seconds total. Safe to run every 5min.
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import sqlite3
import sys
import time
import traceback
from typing import Any, Dict, Optional


# Add backend/src_python to sys.path so we can import db / signal_weight_evaluator
import os
_HERE = str(SRC_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _connect(db_path=None):
    """Connect to DB. db_path is used for tests; default reads from config.

    v376: 连接设为 **autocommit**（isolation_level=None）。
    为什么: 本任务每轮要跑 truth 回填 / verdict 判定 / source_stats 回写 / 权重重算
    四段，每段内部是几百行 × 多次查询的循环。默认隔离级别下整个循环处在一个写事务里，
    实测外部 `BEGIN IMMEDIATE` 连续 6s+ 取不到锁 —— 这就是 api 启动卡死、全站 5xx 的
    最后一环。改成逐语句提交后，单次持锁降到毫秒级；本任务所有写入都是幂等重算，
    不需要跨语句原子性。
    """
    _conn = None
    if db_path:
        _conn = sqlite3.connect(db_path)
    else:
        try:
            import db as _db
        # db.py exposes get_connection() (with timeout, row_factory, isolation)
        # but historically also a connect() alias — try both for forward-compat.
            if hasattr(_db, "get_connection"):
                _conn = _db.get_connection()
            elif hasattr(_db, "connect"):
                _conn = _db.connect()
        except Exception:
            pass
    if _conn is None:
        # Last-resort fallback (only used if db module fails to import).
        _conn = sqlite3.connect("ai.db")
    try:
        _conn.isolation_level = None   # autocommit: 逐语句释放写锁
    except Exception:
        pass
    return _conn


def run_once(hours=24, db_path=None, trigger_kind="cron_react",
             backfill_source=True, backfill_limit=500,
             backfill_truth=True):
    """Single pass: recompute all recent non-HOLD decisions.

    v20261023: 新增 source_stats 回写步骤。每轮结算后把已结算但尚未计入的
    决策增量写入命中率统计 —— 此前该闭环只有 HTTP 手动端点驱动，定时任务
    从不回写，命中率会随时间冻结。幂等性见 backfill_source_stats()。

    v313 P1: 新增**真实市场涨跌**回填 (decision_truth)。验收 #2 的
    "决策时点权重 × 可判定真值"需要每条决策的 forward_move_pct —— 用真实
    K 线算"信号发出后市场实际走了多少"。以前只有 forward_pnl (模拟盘盈亏),
    HOLD 决策永远为 NULL, 配对数只有 5 条, 相关检验无从做起。
    这一步幂等: 已算过的 (forward_move_pct 非 NULL) 不会被重算。

    Returns:
        {"processed": N, "updated": M, "cold": K, "errors": E, "log_id": L,
         "source_stats": {...}}
    """
    started = int(time.time())
    conn = _connect(db_path)
    log_id = 0
    try:
        # Ensure schema
        try:
            import db as _db
            _db.migrate(conn)
        except Exception:
            pass
        # v310 P0: **收尾上一条未完成的运行**。
        # 历史根因: launchd 用 StartInterval=300 + --once 启动本任务, 进程在
        # 每轮开头写一行 started_at 后, 会被外部信号杀死 (Mac 睡眠/唤醒、
        # launchd 重载、手工 restart), 于是留下 finished_at IS NULL 的僵尸行。
        # 实测 471 行里有 42 行是这种僵尸 (processed_n=0, error_msg=''),
        # 拉低"正常完成率"到 91%。
        # 现在: 启动时把上一条僵尸行补上 finished_at + error_msg, 如实记录
        #       它是"被中断"而不是"正常完成" —— 既是崩溃恢复, 也不掩盖事实。
        try:
            cur_fix = conn.execute(
                """UPDATE signal_weight_cron_log
                      SET finished_at = ?, error_msg = 'interrupted_by_restart'
                    WHERE finished_at IS NULL""",
                (started,),
            )
            n_fixed = cur_fix.rowcount or 0
            conn.commit()
            if n_fixed:
                print(f"[signal_weight_cron] 收尾 {n_fixed} 条中断运行", flush=True)
        except sqlite3.OperationalError as _silent_exc_115:
            print("[signal_weight_cron] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                      % (type(_silent_exc_115).__name__, str(_silent_exc_115)[:120]), flush=True)
        # Log start
        try:
            cur = conn.execute(
                """INSERT INTO signal_weight_cron_log
                    (started_at, processed_n, updated_n, cold_n, error_msg)
                   VALUES (?, 0, 0, 0, '')""",
                (started,),
            )
            log_id = cur.lastrowid or 0
            conn.commit()
        except sqlite3.OperationalError as _silent_exc_127:
            print("[signal_weight_cron] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                      % (type(_silent_exc_127).__name__, str(_silent_exc_127)[:120]), flush=True)
        from signals import signal_weight_evaluator as _swe
        # v313 P1: 回填真实市场涨跌 (forward_move_pct) —— 放在权重重算**之前**,
        # 这样本轮就能把新决策的真值补齐, 验收 #2 下一轮就有配对样本。
        # 失败不得阻断权重重算 (权重正确性优先), 降级记日志。
        truth_stats: Dict[str, Any] = {"skipped": "disabled"}
        if backfill_truth:
            try:
                from signals import decision_truth as _dt
                _t0 = time.time()
                _index = _dt.KlineIndex(conn)
                _truths = _dt.collect_decision_truth(conn, index=_index, now_ts=started)
                _n = _dt.persist_truth(conn, _truths)
                truth_stats = {"written": _n, "elapsed_s": round(time.time() - _t0, 1)}
                # v323 P0-1: 分段提交 —— truth 回填 / verdict 判定 / source_stats
                # 原本挤在**同一个事务**里，直到 source_stats 之后才 commit。
                # 这段期间写锁被独占（实测：外部 BEGIN IMMEDIATE 5s/15s 全失败，
                # 只读 0.007s；停掉本 cron 写锁立刻可得）→ 引擎的批次持久化
                # 抢不到锁 → AI 决策卡死、批次被反复重认领（队头永久霸占）。
                # 现在每阶段结束即提交，给关键路径留出锁窗口。
                conn.commit()
            except Exception as e:
                conn.rollback()
                truth_stats = {"error": f"{type(e).__name__}: {e}"}
                print(f"[signal_weight_cron] truth backfill failed: {truth_stats['error']}",
                      file=sys.stderr, flush=True)
        # v315 P0: 把"真实涨跌"变成**方向判定** (WIN/LOSS/BREAKEVEN)。
        # 放在 truth 回填之后、source_stats 回写之前 —— 否则本轮算出来的判定
        # 要等下一轮才进命中率闭环。判定只写 truth_verdict, 不碰
        # is_correct/settled (那是"模拟盘这笔单结算了吗"的口径)。
        verdict_stats: Dict[str, Any] = {"skipped": "disabled"}
        if backfill_truth:
            try:
                from signals import decision_truth as _dt
                _v0 = time.time()
                _verdicts = _dt.collect_truth_verdicts(conn, now_ts=started)
                _nv = _dt.persist_truth_verdicts(conn, _verdicts, now_ts=started)
                verdict_stats = {"judged": _nv,
                                 "elapsed_s": round(time.time() - _v0, 1)}
                # v323 P0-1: 同上 —— verdict 判定也单独提交，别把写锁
                # 一直攥到 source_stats 之后。
                conn.commit()
            except Exception as e:
                conn.rollback()
                verdict_stats = {"error": f"{type(e).__name__}: {e}"}
                print(f"[signal_weight_cron] truth verdict failed: {verdict_stats['error']}",
                      file=sys.stderr, flush=True)
        # v20261023: 先回写命中率闭环，再重算权重 —— 保证本轮重算能读到
        # 本轮刚更新的 source_mult。顺序反过来会慢一轮才生效。
        src_stats = {"skipped": "disabled"}
        if backfill_source:
            try:
                src_stats = _swe.backfill_source_stats(
                    conn, limit=backfill_limit, now_ts=started,
                )
                conn.commit()
            except Exception as e:
                # 回写失败不得阻断权重重算 —— 权重正确性优先于统计新鲜度。
                conn.rollback()
                src_stats = {"error": f"{type(e).__name__}: {e}"}
                print(f"[signal_weight_cron] source_stats backfill failed: "
                      f"{src_stats['error']}", file=sys.stderr, flush=True)
        stats = _swe.batch_recompute(
            conn, hours=hours, trigger_kind=trigger_kind, now_ts=started,
        )
        conn.commit()
        if log_id:
            try:
                conn.execute(
                    """UPDATE signal_weight_cron_log SET
                        finished_at = ?, processed_n = ?, updated_n = ?,
                        cold_n = ?, error_msg = ''
                       WHERE id = ?""",
                    (int(time.time()), stats["processed"], stats["updated"],
                     stats["cold"], log_id),
                )
                conn.commit()
            except sqlite3.OperationalError as _silent_exc_195:
                print("[signal_weight_cron] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                          % (type(_silent_exc_195).__name__, str(_silent_exc_195)[:120]), flush=True)
        stats["log_id"] = log_id
        stats["source_stats"] = src_stats
        stats["truth"] = truth_stats
        stats["verdicts"] = verdict_stats
        return stats
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[:500]}"
        if log_id:
            try:
                conn.execute(
                    """UPDATE signal_weight_cron_log SET
                        finished_at = ?, error_msg = ? WHERE id = ?""",
                    (int(time.time()), err[:1000], log_id),
                )
                conn.commit()
            except sqlite3.OperationalError as _silent_exc_212:
                print("[signal_weight_cron] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                          % (type(_silent_exc_212).__name__, str(_silent_exc_212)[:120]), flush=True)
        print(f"[signal_weight_cron] ERROR: {err}", file=sys.stderr, flush=True)
        return {"processed": 0, "updated": 0, "cold": 0, "errors": 1, "log_id": log_id}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================================
# 代码漂移检测 (v315 P0) —— 守护进程必须自己发现"代码被改了"
# ============================================================================
#
# 根因: --loop 是**常驻进程**, Python 在启动时把模块 import 进来就再也不看磁盘。
# 于是"改了代码 → 重启守护进程"这一步全靠人记 —— 忘了就等于没改。
# 实测: signal_weight_evaluator.py 的 mtime 是 Nov 2 22:52, 而常驻进程
# (pid 798) 是 Nov 3 15:53 才启动的; 也就是说 Nov 2 那次改动在进程存活期间
# **一次都没生效**, 权重逻辑跑的还是旧版 —— 这就是验收里"公式改了但 ρ 不动"
# 的一半原因 (另一半是历史行本身被旧公式写死)。
#
# 做法: 每轮 tick 之前比对被监听模块的 mtime, 变了就**主动退出**。
# plist 是 KeepAlive=true, 进程一退出 launchd 会立刻拉起新进程 → 新代码生效。
# 不需要人工记得重启, 也不会出现"改了代码半小时后才生效"的空窗。
#
# 为什么不改成 StartInterval + --once: 那样同样能每次拿新代码, 但进程会被
# 睡眠/唤醒杀掉 (v310 P0 那批僵尸行就是这么来的), 而当前 launchd
# (KeepAlive + --loop) 在睡眠时进程只是被冻结, 醒来继续跑, 不会丢一轮。

WATCHED_MODULES = (
    "signal_weight_evaluator.py",
    "decision_truth.py",
    "signal_impact.py",
    "db.py",
    "signal_weight_cron.py",
)


def _watched_mtimes():
    """被监听模块的 mtime 快照 (文件不存在 → None)。"""
    out = {}
    for name in WATCHED_MODULES:
        try:
            out[name] = os.path.getmtime(os.path.join(_HERE, name))
        except OSError:
            out[name] = None
    return out


def _code_drift(snapshot):
    """返回发生变化的模块 {name: (旧 mtime, 新 mtime)}; 没变 → {}。"""
    if not snapshot:
        return {}
    now = _watched_mtimes()
    changed = {}
    for name, old in snapshot.items():
        new = now.get(name)
        if old != new:
            changed[name] = (old, new)
    return changed


def run_loop(interval_sec=300, hours=24, drift_watch=True):
    """Daemon mode: run every interval_sec until killed.

    drift_watch=True 时每轮 tick 前检查被监听模块的 mtime; 发现代码被改
    就打印并**退出进程** —— launchd (KeepAlive=true) 会立刻用新代码重启。
    """
    snapshot = _watched_mtimes() if drift_watch else None
    print(f"[signal_weight_cron] starting loop mode (interval={interval_sec}s, "
          f"hours={hours}, drift_watch={'on' if drift_watch else 'off'})", flush=True)
    while True:
        if drift_watch:
            changed = _code_drift(snapshot)
            if changed:
                for name, (old, new) in changed.items():
                    print(f"[signal_weight_cron] CODE DRIFT detected: {name} "
                          f"(mtime {old} → {new}) — 退出进程让 launchd 用新代码重启",
                          flush=True)
                print("[signal_weight_cron] exiting for code reload", flush=True)
                return 0
        try:
            stats = run_once(hours=hours)
            print(f"[signal_weight_cron] tick: {stats}", flush=True)
        except KeyboardInterrupt:
            print("[signal_weight_cron] interrupted, exiting", flush=True)
            return 0
        except Exception as e:
            print(f"[signal_weight_cron] tick error: {e}", file=sys.stderr, flush=True)
        # v342: 每轮收尾强制提交全局缓存的连接。_connect() 有模块级连接缓存，
        # 某条分支漏了 commit 时，这把写事务会跨过 300s 的 sleep 一直开着，
        # 全库其它任务就全卡在 "database is locked"（实测锁死 13 分钟）。
        try:
            _cached = globals().get("_conn")
            if _cached is not None:
                _cached.commit()
        except Exception:
            try:
                globals().get("_conn").rollback()
            except Exception:
                pass
        time.sleep(interval_sec)


def main():
    p = argparse.ArgumentParser(description="signal_weight_cron - re-evaluate signal weights")
    p.add_argument("--once", action="store_true",
                   help="Single pass (default for cron entry-point)")
    p.add_argument("--loop", action="store_true",
                   help="Daemon mode: run forever every --interval seconds")
    p.add_argument("--interval", type=int, default=300,
                   help="Loop interval in seconds (default 300 = 5min)")
    p.add_argument("--hours", type=int, default=24,
                   help="Re-evaluate decisions in the last N hours (default 24)")
    p.add_argument("--db", type=str, default=None,
                   help="DB path override (default: use db.get_connection())")
    p.add_argument("--no-backfill", action="store_true",
                   help="跳过 source_stats 命中率回写（只重算权重）")
    p.add_argument("--backfill-limit", type=int, default=500,
                   help="单次回写最多处理多少条结算决策 (default 500)")
    p.add_argument("--no-truth", action="store_true",
                   help="跳过真实市场涨跌回填 (forward_move_pct)")
    p.add_argument("--no-drift-watch", action="store_true",
                   help="守护进程不检测代码改动 (改完代码不重启进程)")
    args = p.parse_args()
    if args.loop:
        run_loop(interval_sec=args.interval, hours=args.hours,
                 drift_watch=not args.no_drift_watch)
        return 0
    stats = run_once(hours=args.hours, db_path=args.db,
                     backfill_source=not args.no_backfill,
                     backfill_limit=args.backfill_limit,
                     backfill_truth=not args.no_truth)
    print(f"[signal_weight_cron] {stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

