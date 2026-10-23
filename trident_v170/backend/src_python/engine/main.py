"""Bootstrap — DB migration + task orchestration (entry: main())."""

from __future__ import annotations

import asyncio
import os
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
    """
    need_init = not os.path.exists(DB_PATH)

    conn = db.get_connection()
    try:
        db.migrate(conn)
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


# -- main -----------------------------------------------------------------

async def main():
    print("="*50)
    print("[MAIN] Trident Agent MVP - engine starting")
    print("="*50)

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
        asyncio.create_task(ai_worker(loop), name="ai_worker"),
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
        from .ai_worker import _snapshot_market_style, _reflect_recent_decisions
    except Exception as _e:
        print(f"[v176.2-loop] import failed: {_e}", flush=True)
        return

    ASSETS = ("XAU", "WTI", "BTC", "ETH", "SOL", "US_STOCK")
    interval_sec = 300  # 5 分钟
    next_run = time.time()
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
            for a in ASSETS:
                try:
                    snap_n += _snapshot_market_style(a)
                except Exception as _e:
                    print(f"[v176.2-loop] snapshot {a} failed: {_e}", flush=True)
                try:
                    refl_n += _reflect_recent_decisions(a, "1h", 8)
                except Exception as _e:
                    print(f"[v176.2-loop] reflect {a} failed: {_e}", flush=True)
            print(f"[v176.2-loop] tick ok: snap={snap_n} refl={refl_n} cost={time.time()-t0:.1f}s", flush=True)
        except asyncio.CancelledError:
            print("[v176.2-loop] cancelled", flush=True)
            return
        except Exception as _e:
            print(f"[v176.2-loop] tick err: {_e}", flush=True)
            await asyncio.sleep(60)


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

