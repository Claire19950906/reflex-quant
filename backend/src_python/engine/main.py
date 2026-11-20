"""Bootstrap — DB migration + task orchestration (entry: main())."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from typing import Optional

import db
from config import DB_PATH

from .ai_worker import ai_worker, get_current_model_config
from .reflection import reflection_worker
from .structured_reflection import structured_reflection_worker
from .ingest import websocket_ingest
from .webhook import _TREE_NEWS_PORT, _tree_news_handler
from .data_source_alert import data_source_watcher
from .macro_calendar_notifier import macro_calendar_notifier_loop
from .fed_rate_notifier import fed_rate_notifier_loop
from .cross_asset_anomaly_notifier import cross_asset_anomaly_loop

# ── 代理注入 — 完全由 .env / 环境变量控制 ──
# 本地 Windows: .env 设置 HTTP_PROXY=http://127.0.0.1:10808
# 服务器首尔:  不设代理, 直连即可
_proxy_set = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
if _proxy_set:
    print(f"[MAIN] 代理已配置: {_proxy_set}")
else:
    print("[MAIN] 无代理, 直连模式")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _ensure_db_exists() -> None:
    """Create the SQLite event bus if it doesn't exist, or repair a missing-table DB.

    Schema 统一定义在 db.py — 幂等，可安全地在每次启动时调用。
    每次启动顺带预置因果链种子（幂等）——不依赖手工跑 init_db/seed 脚本，
    部署后直接起服务因果链库就有内容。
    v321.1: 启动期不再无条件跑重 migrate。
    背景：migrate 会在 1GB 库上 CREATE INDEX / ALTER TABLE，**全程持有 SQLite
    写锁**；引擎与 api_server、各 cron 同时启动时互相撞 SQLITE_BUSY →
    引擎在 main() 里直接抛 sqlite3.OperationalError: database is locked 崩掉 →
    launchd 立刻重启 → 再次撞锁 → **崩溃重启循环**（实测单份日志 75 次启动，
    主分析整段时间完全停摆：DONE 停在 06:45，认领到的 12 行全部 120s 超时）。
    修法（与 api_server._migrate_schema 同一策略）：
      1) schema_ready() 只读检查（实测 4ms，不申请写锁），schema 完整就跳过
         migrate —— 启动期不再抢写锁；
      2) 确需 migrate 时撞写锁 → 退避重试，而不是让整个引擎陪葬；
      3) 抢不到锁但 schema 完整 → 降级继续跑（绝不自杀）。
    """
    need_init = not os.path.exists(DB_PATH)

    conn = db.get_connection()
    try:
        if db.schema_ready(conn):
            print("[ENGINE] schema 已就绪 → 跳过 migrate（免启动期写锁竞争）",
                  flush=True)
        else:
            migrated = False
            for attempt in range(5):
                try:
                    db.migrate(conn)
                    migrated = True
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    wait = 3 * (attempt + 1)
                    print("[ENGINE] migrate 撞写锁（第 %d/5 次），%ds 后重试: %s"
                          % (attempt + 1, wait, exc), flush=True)
                    time.sleep(wait)
            if not migrated:
                # 抢不到写锁但 schema 已完整 → 不致命，继续启动（绝不自杀重启循环）
                if db.schema_ready(conn):
                    print("[ENGINE] migrate 放弃（仅写锁冲突，schema 已完整）→ 继续启动",
                          flush=True)
                else:
                    print("[ENGINE] migrate 失败且 schema 不完整 → 报错退出", flush=True)
                    raise sqlite3.OperationalError(
                        "database is locked and schema incomplete")
        try:
            import causal_chains
            seeded = causal_chains.seed_defaults(conn)
            if seeded.get("added"):
                print(f"[ENGINE] 因果链种子新增 {seeded['added']} 条，库内共 {seeded.get('total', 0)} 条")
        except Exception as exc:
            print(f"[ENGINE] 因果链种子预置跳过: {type(exc).__name__}: {str(exc)[:80]}")
    finally:
        conn.close()

    tag = "CREATED" if need_init else "VERIFIED"
    print(f"[ENGINE] DB {tag}: {DB_PATH}")


# ---------------------------------------------------------------------------
# v323 P0-1: 引擎单实例 + 端口占用预检
# ---------------------------------------------------------------------------
# 事故背景（2026-11-09 实测，backend/logs/engine_20261109.log 尾部）：
#   两个 engine 实例同时被拉起 → 第二个在 asyncio.start_server 处抛
#   OSError [Errno 48] address already in use → launchd KeepAlive 看到
#   **非 0 退出码** → 判定为崩溃 → 按 ThrottleInterval 反复拉起 → crash-loop。
#   现象是「引擎时有时无 / AI 分析停产」，而日志里只有一行 Errno 48，没人告警。
#
# 现在两道闸：
#   ① fcntl.flock 独占锁（/tmp/trident_engine.lock）：同机只允许一个引擎进程。
#      锁的 fd 存模块级变量 → 进程存活期间锁不释放；进程死了锁由内核释放。
#   ② bind 预检：端口已被占用 → 打印 [FATAL] 后退出。
#      **退出码必须是 0**：非 0 会让 launchd KeepAlive 当成崩溃而 crash-loop。
#
# 锁文件放 /tmp 而不是 backend/runtime —— runtime 在 ~/Desktop 下，
# launchd 进程受 macOS TCC 限制读不了（见 wrappers/run_bridge.sh 的注释）。

_ENGINE_LOCK_FH = None


def _acquire_single_instance_lock() -> bool:
    """拿单实例独占锁。返回 False = 已有实例在跑，调用方应退出。

    拿锁本身出错（异常）时**不阻断启动** —— 单实例保护是加固项，
    不能因为保护机制自己出问题把引擎搞挂。
    """
    global _ENGINE_LOCK_FH
    try:
        import fcntl
    except Exception:
        return True
    try:
        fh = open("/tmp/trident_engine.lock", "w")
    except Exception as exc:
        print(f"[ENGINE-LOCK] 无法打开锁文件（不阻断启动）: {exc}", flush=True)
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            fh.close()
        except Exception:
            pass
        return False
    try:
        fh.write("%d\n" % os.getpid())
        fh.flush()
    except Exception:
        pass
    _ENGINE_LOCK_FH = fh  # 持有引用 → 进程存活期间锁不释放
    print(f"[ENGINE-LOCK] 单实例锁已获取 (pid={os.getpid()})", flush=True)
    return True


def _port_in_use(host: str, port: int) -> bool:
    """bind 预检：端口是否已被别的进程监听。

    故意**不设** SO_REUSEADDR：macOS 下它只放行 TIME_WAIT 残留连接，
    对「活跃监听」仍然返回 EADDRINUSE —— 正是我们要的语义。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError as exc:
        # 只有 EADDRINUSE(48) 算「被占用」；权限等其它错误不当占用处理
        return getattr(exc, "errno", None) == 48
    finally:
        try:
            s.close()
        except Exception:
            pass


# -- main -----------------------------------------------------------------

async def _ai_worker_supervisor(loop) -> None:
    """v398c：ai_worker 的守护壳 —— 任务异常不再让引擎"活着但不干活"。

    事故（2026-11-16 19:08 实测）：ai_worker 用裸 asyncio.create_task 起，
    循环里任何一次未捕获异常（DB 锁超时、解析 ValueError、单批 schema 失败抛出的
    RuntimeError…）都会让**整个任务静默退出**。进程还活着、事件循环还在 kevent
    上空转，progress 停在 step=loop，看门狗看"文件新鲜"判健康 ——
    结果就是"引擎健康、待分析 120 条一动不动、决策一条不出"。
    这里改成：异常 → 打完整堆栈 → 退避 5s → 重新拉起 ai_worker，
    只要进程活着，队列就一定有人在认领。
    """
    import traceback as _tb
    _restarts = 0
    while True:
        try:
            await ai_worker(loop)
            print("[AI-WORKER] ai_worker 正常返回（不该发生）→ 5s 后重新拉起", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _restarts += 1
            print(f"[AI-WORKER] ⚠️ ai_worker 异常退出（第 {_restarts} 次）: "
                  f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)
            _tb.print_exc()
        await asyncio.sleep(5.0)


async def main():
    print("="*50)
    print("[MAIN] Trident Agent MVP - engine starting")
    print("="*50)

    # -- v323 P0-1: 单实例闸门（必须在任何任务启动 / 端口绑定之前）----
    # 已有实例在跑 -> 直接退出。**退出码必须 0**：非 0 会让 launchd
    # KeepAlive 当成崩溃而反复拉起（crash-loop）。
    if not _acquire_single_instance_lock():
        print("[FATAL] another engine instance is already running "
              "(single-instance lock held), exit", flush=True)
        sys.exit(0)
    if _port_in_use("127.0.0.1", _TREE_NEWS_PORT):
        print(f"[FATAL] port {_TREE_NEWS_PORT} already in use, exit", flush=True)
        sys.exit(0)

    # ── v322: DNS 超时护栏（必须在任何协程/网络调用之前安装）────────────
    # 事故背景（2026-11-04 实测）：引擎主线程卡死在 macOS mDNS 的
    # socket_getaddrinfo 里 44 分钟，progress 停在 macro、日志停刷、
    # 进程却"活着"（launchd KeepAlive 只看进程退出 → 完全无感 → 永久停摆）。
    # 根因：asyncio.wait_for 的超时依赖事件循环能跑，而同步 DNS 解析会
    # 直接把事件循环线程焊死，timeout 形同虚设。
    # 护栏把每次解析限制在 5s 内，超时即抛 gaierror 走既有错误分支。
    try:
        from .net_guard import install_dns_timeout_guard
        install_dns_timeout_guard()
    except Exception as _ng_exc:  # pragma: no cover
        print(f"[net-guard] 跳过（不阻断启动）: {type(_ng_exc).__name__}: {str(_ng_exc)[:120]}",
              flush=True)

    # 打印模型配置
    print(f"[MAIN] ══════════════════════════════════════════════")
    print(f"[MAIN] 默认主模型（后端配置，非分析通道）：")
    m = get_current_model_config()
    key_status = "✅" if m.get("api_key") else "❌"
    json_mode = "Strict" if m.get("json_mode") else "Prompt"
    print(f"[MAIN]   {key_status} {m['label']:12s} | {m['id']:35s} | {json_mode} | Timeout: 45s")
    print(f"[MAIN] 分析模型池（实际分片通道）：见启动后的 [AI] 启动模型池: ...")
    print(f"[MAIN] ══════════════════════════════════════════════")

    # Auto-migrate DB schema
    _ensure_db_exists()
    loop = asyncio.get_running_loop()

    # Start TCP server for local webhook ingestion (tree_news, telegram, etc.)
    webhook_server = await asyncio.start_server(
        _tree_news_handler, "127.0.0.1", _TREE_NEWS_PORT
    )
    print(f"[MAIN] Webhook server listening on 127.0.0.1:{_TREE_NEWS_PORT}")

    # Create background workers
    tasks = [
        asyncio.create_task(websocket_ingest(loop), name="ws_ingest"),
        asyncio.create_task(_ai_worker_supervisor(loop), name="ai_worker"),
        # P0: forward_tracker 已废弃（dead code：SELECT 永远 0 行，因 ai_decisions.entry_price 从未填入）。
        # 结算唯一权威已切到 quick_sim.py：open_trades 同步 ai_decisions.entry_*
        # + settle_trades 同步 ai_decisions.{is_correct,settled,forward_pnl,exit_price,mfe_pct,mae_pct}。
        asyncio.create_task(reflection_worker(), name="reflection_worker"),
        # v173: 真实性飞轮 — 监听 ai_decisions.structured_check verdict= fabricated/partial
        asyncio.create_task(structured_reflection_worker(), name="structured_reflection_worker"),
        # v176.2: 市场风格快照 + 决策反思（每 5 分钟跑一次）
        asyncio.create_task(market_style_reflection_loop(), name="market_style_reflection_loop"),
        # v187: 数据源健康告警 — 周期性扫描 source_status,状态翻转/阈值触发推送飞书
        asyncio.create_task(data_source_watcher(loop), name="data_source_watcher"),
        # v190: 实盘交易通道 — 扫描 ai_decisions → 风控 → adapter → 回写 entry_price
        # mode 由 TRADE_EXECUTOR_MODE 控制（off/paper/live_dry/live_real）。off 时直接 return。
        asyncio.create_task(_trade_executor_worker(), name="trade_executor"),
        # v191: 每日归档 — 启动时写 reset_log + 每日 23:59 写 auto_daily reset + 跑 build_daily_summary
        asyncio.create_task(daily_archive_loop(), name="daily_archive"),
        # v202: 宏观日历提前 2h + 即时通知 — 每 5min 扫金十日历，复用 SMTP/Server酱/PushPlus 通道
        asyncio.create_task(macro_calendar_notifier_loop(), name="macro_calendar_notifier"),
        # v226: Fed 利率预期异动推送 — 用 2Y UST + EFFR 第一性原理推导未来 24 月 Fed 路径
        asyncio.create_task(fed_rate_notifier_loop(), name="fed_rate_notifier"),
        # v226: 跨资产异常同步推送 — ≥ 4 个不同行业同向同幅度 + 形态相似
        asyncio.create_task(cross_asset_anomaly_loop(), name="cross_asset_anomaly"),
        # v321.2: 策略级反思周期任务 (需求 #3) — 以前**完全没有调度注册**,
        # strategy_reflections 停在 2026-09-30 (停摆 5 周+), 进化链缺一环。
        asyncio.create_task(strategy_reflection_loop(), name="strategy_reflection_loop"),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("[MAIN] Shutting down...")
    finally:
        webhook_server.close()
        await webhook_server.wait_closed()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def market_style_reflection_loop() -> None:
    """v176.2：每 5 分钟一次，对 6 个主品种轮询 (1) 落 market_style_snapshot
    (2) 跑 _reflect_recent_decisions 写 decision_reflection。

    设计：后台轮询不抢主路径 CPU；任一品种任意一次失败不阻断下一品种。
    """
    import asyncio
    import time
    try:
        from .ai_worker import (_snapshot_market_style, _reflect_recent_decisions,
                                reap_stale_processing)
    except Exception as _e:
        print(f"[v176.2-loop] import failed: {_e}", flush=True)
        return

    ASSETS = ("XAU", "WTI", "BTC", "ETH", "SOL", "US_STOCK")
    interval_sec = 300  # 5 分钟
    next_run = time.time()
    next_cost_sync = 0.0  # v453 P2-E16：token 成本账每小时同步一次
    print(f"[v176.2-loop] started, interval={interval_sec}s, assets={ASSETS}", flush=True)
    while True:
        try:
            now = time.time()
            if now < next_run:
                await asyncio.sleep(min(60, next_run - now))
                continue
            next_run = now + interval_sec
            t0 = time.time()
            snap_n = refl_n = 0
            # v453 P1-3：僵尸回收（PROCESSING 超 15 分钟）搭这趟 5 分钟循环跑
            reap_n = 0
            try:
                reap_n = reap_stale_processing()
            except Exception as _e:
                print(f"[v176.2-loop] reap stale failed: {_e}", flush=True)
            # v453 P2-E16：LLM token 用量 → t2_cost_ledger（整点级，幂等重写当天）
            if now >= next_cost_sync:
                next_cost_sync = now + 3600
                try:
                    import cost_ledger
                    _cost_n = cost_ledger.sync_day()
                    print(f"[v176.2-loop] cost ledger rows={_cost_n}", flush=True)
                except Exception as _e:
                    print(f"[v176.2-loop] cost ledger failed: {_e}", flush=True)
            for a in ASSETS:
                # v489 P0（2026-11-20 实栈定位）：这两个函数是**同步**的，内部直接
                # requests 打 OKX（_okx_get → fetch_okx_candles）。在协程里裸调用 =
                # 把整个事件循环焊死：AI 批次的 await/超时全部无法触发，分析完了
                # 却落不了库，批次卡到 watchdog 12 分钟才强杀 —— 这就是「token 花了
                # 但零产出」的真凶（SIGUSR1 取栈实测命中的正是这条链路）。
                # 改法：丢进线程，并给每次 45s 的等待上限；超时就跳过这个品种，
                # 绝不阻塞主循环。
                try:
                    snap_n += await asyncio.wait_for(
                        asyncio.to_thread(_snapshot_market_style, a), timeout=45)
                except (asyncio.TimeoutError, Exception) as _e:
                    print(f"[v176.2-loop] snapshot {a} skipped: {type(_e).__name__}", flush=True)
                try:
                    refl_n += await asyncio.wait_for(
                        asyncio.to_thread(_reflect_recent_decisions, a, "1h", 8), timeout=45)
                except (asyncio.TimeoutError, Exception) as _e:
                    print(f"[v176.2-loop] reflect {a} skipped: {type(_e).__name__}", flush=True)
            print(f"[v176.2-loop] tick ok: snap={snap_n} refl={refl_n} reap={reap_n} cost={time.time()-t0:.1f}s", flush=True)
        except asyncio.CancelledError:
            print("[v176.2-loop] cancelled", flush=True)
            return
        except Exception as _e:
            print(f"[v176.2-loop] tick err: {_e}", flush=True)
            await asyncio.sleep(60)


async def strategy_reflection_loop() -> None:
    """v321.2: 策略级反思周期任务 (需求 #3 "策略买卖的反思进化")。

    为什么以前不跑
    --------------
    `engine/strategy_reflection.py` 里 `run_strategy_reflection_daemon()` 写好了,
    但**从来没有被任何调度注册** —— engine/main.py 的 tasks 列表里没有它,
    ~/Library/LaunchAgents 下也没有对应的 plist。结果:
        strategy_reflections 最新一条 2026-09-30 (停摆 5 周+)
        strategy_versions       最新一条 2026-09-23
    而 strategy_param_adjustments 一直更新到 2026-11-04 —— 参数在调,
    但"为什么调"的策略级反思断了, 进化链缺一环。

    为什么不用 run_strategy_reflection_daemon
    ---------------------------------------
    它是**阻塞式同步函数** (time.sleep), 直接丢进 asyncio 会卡死整个事件循环
    (engine 里 12 个 worker 共用一个 loop)。这里用 asyncio.sleep 复刻同样的
    周期语义, 但不阻塞 loop。

    行为
    ----
    · 启动后**先跑一次** (不等 7 天) —— 否则新部署仍要等一周才看到第一条;
    · 之后每 `interval_days` 天跑一次;
    · 任一轮失败只记录, 绝不终止 loop;
    · 样本不足 (_MIN_SAMPLE_SIZE=30) 时 generate_strategy_reflection 自身会
      返回 None, 不产生脏数据 —— 无需在这里额外判断。
    """
    import asyncio
    import time as _time
    try:
        from .strategy_reflection import (
            _DAEMON_INTERVAL_DAYS,
            _DEFAULT_PERIOD_DAYS,
            generate_strategy_reflection,
        )
    except Exception as _e:
        print(f"[STRAT-REFLECT] import failed: {_e}", flush=True)
        return

    interval_sec = int(_DAEMON_INTERVAL_DAYS) * 86400
    print(
        f"[STRAT-REFLECT] loop started, interval={_DAEMON_INTERVAL_DAYS}d, "
        f"period={_DEFAULT_PERIOD_DAYS}d",
        flush=True,
    )
    while True:
        try:
            t0 = _time.time()
            rid = generate_strategy_reflection(period_days=_DEFAULT_PERIOD_DAYS)
            print(
                f"[STRAT-REFLECT] tick rid={rid} cost={_time.time()-t0:.1f}s",
                flush=True,
            )
        except asyncio.CancelledError:
            print("[STRAT-REFLECT] cancelled", flush=True)
            return
        except Exception as _e:
            print(f"[STRAT-REFLECT] tick err: {type(_e).__name__}: {_e}", flush=True)
        # 用 sleep 分段, 让取消信号能及时打断 (直接 sleep(7天) 取消不了)
        deadline = _time.time() + interval_sec
        while True:
            remaining = deadline - _time.time()
            if remaining <= 0:
                break
            try:
                await asyncio.sleep(min(60.0, remaining))
            except asyncio.CancelledError:
                print("[STRAT-REFLECT] cancelled", flush=True)
                return


async def _trade_executor_worker() -> None:
    """v190: Binance Futures 实盘通道 worker。

    模式由环境变量 `TRADE_EXECUTOR_MODE` 控制：
      - off        : 不启动（默认）
      - paper      : MockAdapter（不真下单，但写 audit_log + entry_price，模拟路径）
      - live_dry   : BinanceAdapter dry-run（连真 Binance API 取价，但不下单）
      - live_real  : BinanceAdapter 真下单（需要 BINANCE_API_KEY/SECRET）

    设计要点：
      - 与 quick_sim 完全排他：trade_executor 写 `entry_price` 时使用
        `WHERE id=? AND entry_price IS NULL` 原子保护，谁先写谁是建仓者。
      - 拒绝/跳过也写 `trade_audit_log`，fetch_pending 用 NOT IN 子查询排重。
    """
    try:
        from trade_executor import build_default_executor
    except Exception as exc:
        print(f"[TRADE] import failed: {exc}", flush=True)
        return

    mode = (os.getenv("TRADE_EXECUTOR_MODE", "off") or "off").strip().lower()
    if mode == "off":
        print("[TRADE] mode=off, worker skipped", flush=True)
        return

    try:
        executor = build_default_executor(DB_PATH, env=os.environ)
    except Exception as exc:
        print(f"[TRADE] build_default_executor failed: {exc}", flush=True)
        return

    print(
        f"[TRADE] started mode={mode} adapter={executor.adapter.name} "
        f"poll={executor.cfg.poll_interval}s notional={executor.cfg.notional_usdt}usdt",
        flush=True,
    )
    try:
        await executor.run()
    except asyncio.CancelledError:
        print("[TRADE] cancelled", flush=True)
        raise
    except Exception as exc:
        print(f"[TRADE] worker crashed: {exc}", flush=True)


async def daily_archive_loop() -> None:
    """v191: 每日归档 worker — 写 daily_reset_log + 跑 daily_reflection_summary。

    行为:
      - 启动时立即写一条 trigger='startup' 的 daily_reset_log
      - 每天本地 23:59(预留 1 分钟缓冲,实际触发 23:59:30)写
        trigger='auto_daily' 的 reset_log + 跑前一日的 build_daily_summary
      - 每 60s 检查一次是否到了归档时点(轻量轮询,不会抢 CPU)
      - 任何异常吞掉,仅打印,不能影响主引擎

    设计:
      - run_id = 当前 UTC 日期(YYYY-MM-DD),与 paper_trading 的 run_id 概念独立
      - 写 reset_log 时同步调用 build_daily_summary(date, run_id)刷新当日累计统计
      - 注意:不依赖 OS 时区,datetime.now() 返回本地时区
    """
    from datetime import datetime as _dt, timedelta as _td

    try:
        from daily_archive import archive_daily_reset, build_daily_summary
    except Exception as exc:
        print(f"[ARCHIVE] import failed: {exc}", flush=True)
        return

    print("[ARCHIVE] daily_archive_loop started", flush=True)

    # 启动时记录一条 startup 重置(供 ops 追溯"系统何时启动")
    try:
        from db import get_connection
        conn0 = get_connection()
        try:
            archive_daily_reset(
                conn0,
                run_id=_dt.now().strftime("%Y-%m-%d"),
                trade_mode=(os.getenv("TRADE_EXECUTOR_MODE", "off") or "off"),
                trigger="startup",
                notes="engine.main startup",
            )
        finally:
            conn0.close()
    except Exception as exc:
        print(f"[ARCHIVE] startup reset_log failed: {exc}", flush=True)

    last_run_date: Optional[str] = None
    while True:
        try:
            now = _dt.now()
            # 触发条件:现在是 23:59:30~23:59:59 之间, 且 last_run_date != 今天
            if (now.hour == 23 and now.minute == 59 and 30 <= now.second <= 59
                    and last_run_date != now.strftime("%Y-%m-%d")):
                today = now.strftime("%Y-%m-%d")
                from db import get_connection
                conn = get_connection()
                try:
                    reset_id = archive_daily_reset(
                        conn,
                        run_id=today,
                        trade_mode=(os.getenv("TRADE_EXECUTOR_MODE", "off") or "off"),
                        trigger="auto_daily",
                        notes=f"end-of-day reset for {today}",
                        reset_date=today,
                    )
                    # 同步聚合当日(可能还有未结算的,build_daily_summary 仍然聚合 PENDING 状态)
                    summary_id = build_daily_summary(
                        conn, today,
                        run_id=today,
                        reset_log_id=reset_id,
                        trade_mode=(os.getenv("TRADE_EXECUTOR_MODE", "off") or "off"),
                        lessons_learned="自动汇总,人工可编辑",
                        next_day_focus="",
                    )
                    print(f"[ARCHIVE] end-of-day {today}: reset_id={reset_id} "
                          f"summary_id={summary_id}", flush=True)
                finally:
                    conn.close()
                last_run_date = today
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            print("[ARCHIVE] cancelled", flush=True)
            return
        except Exception as exc:
            print(f"[ARCHIVE] tick error: {exc}", flush=True)
            await asyncio.sleep(60)

