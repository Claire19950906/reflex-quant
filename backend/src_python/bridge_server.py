"""
bridge_server.py — 桥接服务 (FastAPI, port 8100)
================================================

为什么需要:
  - 8000 端口的 api_server.py 磁盘源文件已删,内存里 200+ 模块仍在跑
  - /api/v237/missed-signals/by-dimension 需要 Python 模块 missed_signal_dimensions
    磁盘上不存在,无法注入,8000 永远返 500 "No module named 'missed_signal_dimensions'"
  - 本服务在 8100 端口上提供:
      ① v237/missed-signals/by-dimension (基于 critical_price_moves 真实数据 + feed_health 真实失败数据 推导 A/B/C 三维)
      ② v237/calibration 校准补充 (周维度)
      ③ v291/attribution-stats 资金流交叉验证 (从 external_indicators 真实价格推导)
      ④ v291/causal-graph 因果图谱 builder (从 critical_price_moves + feed_health 真实事件构建)
      ⑤ 实时 critical-move 增量检测
      ⑥ health

数据来源 (100% 真实,绝不伪造):
  - critical_price_moves: external_indicators 真实 yfinance 价格扫描结果
  - feed_health: BRENT/SKHYNIX/US_HYNIX 真实 feed 探测 4450 行
  - external_indicators: yfinance 真实日线 420 行
  - audit_log: 真实 API 调用 8440 行

路由:挂在 /api 下,与 8000 一致;前端 lib/api.ts 加 fallback 链
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import threading
import hashlib
from datetime import datetime, timezone, time as datetime_time, date, timedelta
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

import intraday_store

# v291+ P1 校准工具 (纯 Python, 无 sklearn 依赖)
sys.path.insert(0, str(Path(__file__).resolve().parent))

# v309 P1: 统一时间口径 (唯一入口, 详见 time_utils.py 文件头)
# 必须在 sys.path.insert 之后 import —— 否则从别的目录启动时会 ModuleNotFoundError。
from common import time_utils
import shadow_ledger # v400: 影子账本闸门结论（sweep_verdict 与 CLI 同一份口径）
import event_status # v317: 事件分析状态协议 (8000/8100 唯一真相源)

# v310 P2: /api/review/snapshot 的**唯一实现** —— 8000 与 8100 共用, 同源同口径。
from review import review_snapshot as review_snapshot_mod

# ── v321.3 P0: fd 泄漏防护 (根治「AI分析挂了」) ────────────────────────────────
# 同一个根因也打在 bridge 上 (launchd fd 上限 256 + yfinance peewee 每线程连接)。
# 详见 yf_fd_guard.py 文件头。
try:
    import yf_fd_guard
    yf_fd_guard.install()
except Exception as _fdg_err:
    print(f"[FD-GUARD] yf_fd_guard 安装失败 (忽略): {type(_fdg_err).__name__}: {_fdg_err}",
          flush=True)
try:
    import fd_guard
    fd_guard.start_fd_monitor()
except Exception as _fdg_err2:
    print(f"[FD-GUARD] fd_guard 启动失败 (忽略): {type(_fdg_err2).__name__}: {_fdg_err2}",
          flush=True)
# v321.3 P1: 出网代理 (需求 #6 挂单量/资金流 / #14 外部源)。
try:
    import config as _config_mod
    _config_mod.apply_proxy_env()
except Exception as _proxy_err:
    print(f"[PROXY] 应用失败 (忽略): {type(_proxy_err).__name__}: {_proxy_err}",
          flush=True)

# v314: news_events.action 的枚举归一 (写入边界唯一入口, 见 db.normalize_news_action)
# v311 fix: 结算/复盘链路用的是 db.forward_outcome_pct / db.is_implausible_outcome /
#   db.resolve_entry_price 这类**模块级**函数, 之前这里只写了
#   `from db import normalize_news_action` → 模块名 db 从未绑定 →
#   decision_settle_loop 启动即抛 NameError("name 'db' is not defined"),
#   结算线程从未真正跑起来(决策的出场价/盈亏/结算结论一直是空的)。
import db
from db import normalize_news_action

try:
    from calibration import (
        isotonic_regression,
        brier_score,
        expected_calibration_error,
        calibration_table,
    )
    _HAS_CALIBRATION = True
except ImportError as _e:
    print(f"[bridge] calibration module not available: {_e}", file=sys.stderr)
    _HAS_CALIBRATION = False

# v310 P2: 与 api_server 读同一个库 —— 路径解析收在 db_location（唯一来源），
# 两个服务必须读**同一个** DB, 否则"真实持仓战绩"两边对不上。
from db_location import BACKEND_DIR, DB_PATH
# 2026-11-18: v237 分维度构建 + 时间窗助手 + db_cursor 搬到 bridge_builders
from bridge_builders import *
# 2026-11-18: 新闻解析/影响评估这一簇（含词典常量）搬到 bridge_news
from bridge_news import *
# 2026-11-18: 行情/异动排行簇（含 120s 缓存 + 预热线程）搬到 bridge_market
from bridge_market import *
from bridge_analytics import *
from bridge_panels import *
from bridge_panels import _forward_to_api
movers_prewarm_thread = start_movers_prewarm()

# v308 P1-fix3: 量纲污染阈值 (全局唯一来源)。
# critical_price_moves 里有价格序列混档产生的行 (BTC 曾出现 77.48 → 7722.72,
# move_pct=+9867%)。这不是市场异动, 是数据污染。
# |move_pct| > 90% 对任何资产的一日/多日涨跌都不成立 → 全部剔除。
# 所有读 critical_price_moves 的端点都必须套这个过滤, 否则污染会漏到 UI 上
# (实测 attribution-stats 的 avg_move_pct 曾显示 9187%)。

# ── v383: 桥"假死"诊断 ─────────────────────────────────────────────
# 症状：进程还在、8100 还在 LISTEN、所有请求却返回 000（超时），
# 要人工重启才能恢复，且当时 py-spy 抓不到栈（需要 root）。
# 这里自带两件工具：
#   ① faulthandler + SIGUSR1：任何时候 `kill -USR1 <pid>` 立刻把**全部线程栈**
#      写进日志，不用等它自己出事；
#   ② 心跳监视线程：事件循环每 2 秒要打一次点，超过 25 秒没打点就判定"卡住"，
#      自动 dump 全部线程栈（同一分钟只 dump 一次，避免刷爆日志）。
_BOOT_T0 = time.time()
_BRIDGE_TICK = [0.0]
_BRIDGE_LAST_DUMP = [0.0]


async def _bridge_loop_ticker() -> None:
    while True:
        _BRIDGE_TICK[0] = time.time()
        await asyncio.sleep(2)


def _start_hang_detector() -> None:
    try:
        import faulthandler
        import signal as _sig
        faulthandler.enable(file=sys.stderr, all_threads=True)
        if hasattr(_sig, "SIGUSR1"):
            faulthandler.register(_sig.SIGUSR1, file=sys.stderr,
                                  all_threads=True, chain=False)
    except Exception:
        pass

    def _monitor() -> None:
        import traceback
        while True:
            time.sleep(5)
            lag = time.time() - _BRIDGE_TICK[0]
            if lag <= 25:
                continue
            if time.time() - _BRIDGE_LAST_DUMP[0] < 120:
                continue
            _BRIDGE_LAST_DUMP[0] = time.time()
            print(f"[BRIDGE-HANG] 事件循环 {lag:.0f}s 未响应，抓全部线程栈",
                  file=sys.stderr, flush=True)
            try:
                for tid, frame in sys._current_frames().items():
                    print(f"--- thread {tid} ---", file=sys.stderr, flush=True)
                    traceback.print_stack(frame, file=sys.stderr)
            except Exception:
                pass
            try:
                sys.stderr.flush()
            except Exception:
                pass

    threading.Thread(target=_monitor, name="bridge-hang-monitor", daemon=True).start()


# ─── v367 单后端合并：接管 api_server 的 lifespan ──────────────────────────
# 背景：8100 在文件末尾把 api_server 的 329 条**路由**挂进了本进程，但只挂了路由、
# 没跑它的 lifespan —— 于是预热线程(market/all、movers、发现页)、coinglass watcher、
# 反思 worker、审计守护全都不在 8100 起。结果是同一套代码在 8000 和 8100 各跑一份：
# 内存翻倍、两个进程同时写 source_status → 日志里 8000+ 次 "database is locked"。
# 这里用 asynccontextmanager 把 api_server.lifespan 包进 bridge 的生命周期，
# 让 8100 成为**完整**后端，8000 可以停掉（BRIDGE_HOST_API_LIFESPAN=0 可回退）。
# 已确认 api_server.lifespan 内部不使用传入的 app 对象，只操作模块级全局与线程。
try:
    from contextlib import asynccontextmanager as _asynccontextmanager
except Exception:  # pragma: no cover
    _asynccontextmanager = None


if _asynccontextmanager is not None:
    @_asynccontextmanager
    async def _bridge_lifespan(_app):
        _ctx = None
        print(f"[BOOT] t={time.time()-_BOOT_T0:.1f}s  lifespan 开始（此前为模块导入耗时）",
              flush=True)
        # v383: 挂死检测（信号抓栈 + 事件循环心跳），先于一切初始化
        try:
            _start_hang_detector()
            asyncio.create_task(_bridge_loop_ticker())
            print("[bridge] 挂死检测已启用（kill -USR1 可随时抓栈）", flush=True)
        except Exception as _he:
            print(f"[bridge] 挂死检测启用失败(忽略): {_he}", flush=True)
        if os.getenv("BRIDGE_HOST_API_LIFESPAN", "1") == "1":
            try:
                # v377: 告诉 api_server「你正跑在 8100 里」，让它跳过
                # "探测 8100 是否在线" 的自等待（那会白等 96s）。
                os.environ["TRIDENT_HOSTED_IN_BRIDGE"] = "1"
                import api_server as _as_life
                _ctx = _as_life.lifespan(_app)
                _t_enter = time.time()
                await _ctx.__aenter__()
                print(f"[BOOT] t={time.time()-_BOOT_T0:.1f}s  api_server lifespan 进入完成"
                      f"（本段 +{time.time()-_t_enter:.1f}s）", flush=True)
            except Exception as _exc:
                print(f"[bridge] ⚠️ api_server lifespan 接管失败(忽略): "
                      f"{type(_exc).__name__}: {str(_exc)[:160]}", flush=True)
                _ctx = None
        try:
            yield
        finally:
            if _ctx is not None:
                try:
                    await _ctx.__aexit__(None, None, None)
                except Exception:
                    pass
    _APP_LIFESPAN = _bridge_lifespan
else:  # pragma: no cover
    _APP_LIFESPAN = None

app = FastAPI(title="trident_v170 bridge", version="1.0", lifespan=_APP_LIFESPAN)

# v345: 本服务几乎所有端点是 `def`（同步）→ Starlette 丢进 anyio 线程池执行，
#   默认只有 40 个令牌。Next 页面一次并发 10~20 个请求，其中几个走外网/全表扫描的
#   慢端点一占就是几十秒，把 40 个槽吃光后连 /api/health 都排队 → 观测上"整机接口全挂"。
#   抬到 160：慢端点再多也不至于把健康检查/关键读接口饿死。
@app.on_event("startup")
def _raise_thread_pool() -> None:
    """v345: 把 anyio 默认线程池 40 → 160（必须放在 startup 里，模块级拿不到事件循环）。"""
    try:
        import anyio.to_thread as _anyio_to_thread
        _anyio_to_thread.current_default_thread_limiter().total_tokens = 160
        print("[boot] anyio 线程池上限 → 160", flush=True)
    except Exception as _exc:  # pragma: no cover
        print(f"[boot] 线程池上限设置失败: {_exc}", flush=True)
    # v345: 出网超时兜底 —— 整机"所有接口一起卡死"反复出现的真凶是
    #   某个数据源(Yahoo 的 ^IRX/^FVX、东财)SSL 挂住不回，urllib/requests
    #   又没传 timeout，线程就永远停在 recv 上；anyio 线程池(160)被这些
    #   僵尸线程占满后，连 /api/health 都要排队 → 页面看上去"整机挂了"。
    #   这里给所有**出网**调用一个默认超时。SSE 是服务端写不受影响(不改 socket 默认)。
    try:
        import urllib.request as _ur
        _orig_urlopen = _ur.urlopen
        def _urlopen_timeout(url, data=None, timeout=12, *a, **kw):
            if timeout is None:
                timeout = 12
            return _orig_urlopen(url, data=data, timeout=timeout, *a, **kw)
        _ur.urlopen = _urlopen_timeout
        print("[boot] urllib 默认超时 → 12s", flush=True)
    except Exception as _exc:  # pragma: no cover
        print(f"[boot] urllib 超时补丁失败: {_exc}", flush=True)
    try:
        import requests as _rq
        _orig_send = _rq.sessions.Session.request
        def _request_timeout(self, method, url, **kw):
            if kw.get("timeout") is None:
                kw["timeout"] = 12
            return _orig_send(self, method, url, **kw)
        _rq.sessions.Session.request = _request_timeout
        print("[boot] requests 默认超时 → 12s", flush=True)
    except Exception as _exc:  # pragma: no cover
        print(f"[boot] requests 超时补丁失败: {_exc}", flush=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── v374: 聚合 GET 的服务端短 TTL 缓存 ────────────────────────────────
# 前端多个页面各自 setInterval 轮询同一批"看板聚合"接口，且这些接口每次都要
# 扫大表（实测 /api/dashboard/overview 3.5s、/api/market/movers 1.9s）。
# 在网关层直接按 URL 缓存几秒，重复请求不再进业务逻辑，也不需要重新构建前端。
# 只对白名单内的 GET 生效；SSE / 写操作 / 流式响应一律绕过。
_CACHE_TTL_BY_PATH = {
    "/api/dashboard/overview": 3.0,
    "/api/market/movers": 3.0,
    "/api/events/today": 3.0,
    "/api/sources/alerts": 5.0,
    "/api/v237/missed-signals/by-dimension": 10.0,
    "/api/discovery/summary": 10.0,
    "/api/crowd/topics": 30.0,        # v385: 单次 ~6s（15k 话题聚合），缓存 30s
    "/api/news/recent": 15.0,         # v468: 三表 JOIN + 两个相关子查询，首波被写锁排队 ~15s；热点列表短缓存 15s
}
_CACHE_STORE: dict = {}
_CACHE_MAX = 128


@app.middleware("http")
async def _ttl_cache_middleware(request: Request, call_next):
    path = request.url.path
    ttl = _CACHE_TTL_BY_PATH.get(path)
    if ttl is None or request.method != "GET":
        return await call_next(request)
    key = path + "?" + (request.url.query or "")
    hit = _CACHE_STORE.get(key)
    if hit is not None and (time.time() - hit[0]) < ttl:
        return Response(
            content=hit[1],
            media_type="application/json",
            headers={"x-cache": "hit", "x-cache-age": f"{time.time() - hit[0]:.1f}"},
        )
    response = await call_next(request)
    if response.status_code != 200:
        return response
    # BaseHTTPMiddleware 拿到的永远是 _StreamingResponse（没有 .body），
    # 必须把 body_iterator 读空再重新组装，否则缓存永远存不进东西（首版就是这么失效的）。
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    body = b"".join(chunks)
    if len(_CACHE_STORE) > _CACHE_MAX:
        _CACHE_STORE.clear()
    _CACHE_STORE[key] = (time.time(), body)
    keep = {
        k: v for k, v in response.headers.items()
        if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")
    }
    keep["x-cache"] = "miss"
    return Response(content=body, status_code=200, headers=keep, media_type=response.media_type)


# ─── 工具 ───────────────────────────────────────────


@contextmanager
def write_db_cursor():
    """可写连接 (P1: trade_decisions / ohlcv_daily 写入)

    v311 (静默丢数据修复): 原来 finally 里 `except Exception: pass` 把
    **commit 失败**整个吞掉了 —— 撞写锁导致 commit 失败时, 上层照样拿到
    "已插入 N 行"的计数, 但数据一行没落库 (静默丢数据, 且日志里什么都看不到)。
    现在: 体内抛异常 → rollback 后原样抛出; commit 失败 → rollback + 抛
    OperationalError, 由调用方决定重试/降级, **不再假装成功**。
    """
    # v345: engine 常驻高频写库，bridge 的批量写（news/cot/ohlcv）撞锁后 10s 就抛
    #   "database is locked" → 日志里成片失败、前端偶发 500。把等待窗口抬到 30s，
    #   并保持 WAL（读不阻塞写），让写请求排队而不是直接失败。
    # v345 关键修复：不要在每个写连接上再跑 `PRAGMA journal_mode=WAL` ——
    #   这条 PRAGMA 需要拿库级独占锁，engine 高频写时它自己就被卡住，
    #   实测表现为「整机接口全挂、连 /api/health 都不回」。
    #   WAL 是库级持久设置，建库时设一次就够，连接上只需 busy_timeout。
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    try:
        conn.commit()
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    conn.close()








# ─── health ─────────────────────────────────────────
try:
    with db_cursor() as _hc:
        _HEALTH_TABLES = [r[0] for r in _hc.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
except Exception:  # pragma: no cover
    _HEALTH_TABLES: list[str] = []
@app.get("/api/health")
async def health() -> dict:
    # v345: 原来是同步 def + 每次查 sqlite_master —— 引擎写锁一紧、线程池一满，
    #   连健康检查都排队（实测 15~25s 不回，看上去像"整机挂了"）。
    #   改成 async 且不打 DB：表数取进程启动时的快照，探针永远秒回。
    tables = _HEALTH_TABLES
    # v452：版本号改读 config 的单一真值源（原来写死 v308-p1，比实际代码落后 140 多版）
    try:
        import config as _cfg_v452
        _cv452 = str(_cfg_v452.CODE_VERSION)
    except Exception:
        _cv452 = "unknown"
    out = {
        "ok": True,
        "service": "bridge",
        "version": "1.0",
        "port": 8100,
        # v308 P1: 部署探针 —— 这个字段是 v308 阶段才加的。
        # 用来确认 8100 进程跑的是不是磁盘上的最新代码 (改完文件看它变没变)。
        "code_version": _cv452,
        # v398: db（数据库绝对路径）已删除 —— 与 8000 侧 v321.3 的安全口径统一，
        # 健康检查不该往外报本机路径。探针需要的话看 /api/health/internal。
        "tables": tables,
        "status": "ok",
        "ts": int(time.time()),
    }
    # v321.3 P0: fd 水位 —— 过去「AI分析挂了」是静默故障, 现在把它摆出来。
    try:
        import fd_guard as _fdg
        snap = _fdg.fd_snapshot()
        out["fd"] = {
            "level": snap.get("level"),
            "pct": snap.get("pct"),
            "used": snap.get("used"),
            "soft_limit": snap.get("soft_limit"),
        }
        if snap.get("level") in ("warn", "critical"):
            out["ok"] = False
            out["status"] = "degraded"
    except Exception:
        pass
    return out


@app.get("/api/status")
async def api_status() -> dict:
    """v498: 补上 /api/status —— 之前落进兜底返回 503 `_implemented:false`，
    前端或外部探针一旦请求就会白等。这里给一个轻量真实状态：进程、DB 计数、数据新鲜度。
    放线程池里读库，避免同步查询再堵事件循环（v498 修的同类问题）。"""
    def _collect() -> dict:
        import sqlite3
        out = {"ok": True, "service": "bridge", "ts": int(time.time())}
        try:
            with db_cursor() as conn:
                q = lambda s: conn.execute(s).fetchone()[0]
                out["counts"] = {
                    "raw_news_24h": int(q(
                        "SELECT COUNT(*) FROM raw_news WHERE ts >= strftime('%s','now','-24 hours')")),
                    "ai_decisions_24h": int(q(
                        "SELECT COUNT(*) FROM ai_decisions WHERE created_at >= datetime('now','-1 day')")),
                    "lsr_samples_24h": int(q(
                        "SELECT COUNT(*) FROM lsr_samples WHERE ts >= strftime('%s','now','-24 hours')")),
                }
            out["freshness"] = {
                "reasonable": True,
                "note": "counts 为 24h 窗口；健康详情见 /api/health、缺口见 /api/health/data-gaps",
            }
            # v499: 把「暂停采集」状态暴露给前端 —— 之前 DATA_PAUSED 只影响写库，
            #   页面拿不到任何标记，用户看到的是"暂无数据/异常"，分不清"停了"还是"坏了"。
            try:
                import os
                flag = os.path.expanduser("~/.trident/state/DATA_PAUSED")
                out["paused"] = os.path.exists(flag)
                if out["paused"]:
                    out["paused_since"] = int(os.stat(flag).st_mtime)
                    out["paused_note"] = (
                        "数据采集/分析已暂停（engine 未启动）：新闻仍在入库但不会分析，"
                        "决策、信号、复盘样本因此为空。恢复：bash ~/.trident/wrappers/resume_data.sh"
                    )
            except Exception:
                out["paused"] = False
        except Exception as exc:
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    return await asyncio.to_thread(_collect)






# ─── v237/missed-signals/by-dimension ───────────────
# v345.7: by-dimension 实测 4.7s（汇总 critical_price_moves + feed_health +
#   反思表），而 /missed-signals 与 /discover 首屏都会拉它 → 页面被拖到 20-30s。
#   同参数 60s 内直接回缓存；统计窗口是 90 天，60 秒陈旧完全可接受。
_V237_CACHE: dict[int, tuple[float, dict]] = {}
_V237_TTL_S = 60.0


@app.get("/api/v237/missed-signals/by-dimension")
def v237_by_dimension(days: Annotated[int, Query(ge=1, le=3650)] = 90) -> dict:
    _key = int(days)
    _hit = _V237_CACHE.get(_key)
    if _hit and (time.time() - _hit[0]) < _V237_TTL_S:
        return _hit[1]
    payload = _v237_by_dimension_build(_key)
    if isinstance(payload, dict):
        _V237_CACHE[_key] = (time.time(), payload)
    return payload




# ─── v237/calibration 评分校准 (P1) ─────────────────
@app.get("/api/v237/calibration")
def v237_calibration(days: Annotated[int, Query(ge=1, le=3650)] = 90) -> dict:
    """评分校准:algo_score 直方图 + 实际涨跌反向回算
    真实数据: critical_price_moves.algo_score (0-1) vs ohlcv_daily 之后 5 日实际涨跌
    """
    cutoff = int(time.time()) - days * 86400
    with db_cursor() as conn:
        moves = conn.execute(
            "SELECT asset, ts, move_pct, algo_score FROM critical_price_moves "
            "WHERE ts >= ? AND algo_score > 0 AND ABS(COALESCE(move_pct,0)) <= ? "
            "ORDER BY ts DESC LIMIT 500",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchall()
    # 直方图 (10 个 bin)
    bins = [0] * 10
    for m in moves:
        idx = min(int(m["algo_score"] * 10), 9)
        bins[idx] += 1
    # 反向回算: 用 ohlcv_daily 取后 5 天 close, 算实际涨跌
    cal_data: list[dict] = []
    with db_cursor() as conn:
        for m in moves:
            target = m["ts"] + 5 * 86400
            fwd = conn.execute(
                "SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? ORDER BY ts ASC LIMIT 1",
                (m["asset"], target),
            ).fetchone()
            back = conn.execute(
                "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                (m["asset"], m["ts"]),
            ).fetchone()
            if fwd and back and back[0] > 0:
                actual = round((fwd[0] - back[0]) / back[0] * 100, 3)
                cal_data.append({
                    "asset": m["asset"],
                    "ts": m["ts"],
                    "algo_score": m["algo_score"],
                    "actual_5d_pct": actual,
                })
    # 分箱平均
    bucket_stats: list[dict] = []
    for b in range(10):
        in_bucket = [c for c in cal_data if b / 10 <= c["algo_score"] < (b + 1) / 10]
        if in_bucket:
            avg = sum(c["actual_5d_pct"] for c in in_bucket) / len(in_bucket)
            bucket_stats.append({
                "bin": f"{b/10:.1f}-{(b+1)/10:.1f}",
                "n": len(in_bucket),
                "avg_actual_5d_pct": round(avg, 3),
                "expected": (b + 0.5) / 10 * 5,  # 假设 5% 范围线性映射
                "calibration_gap": round(avg - (b + 0.5) / 10 * 5, 3),
            })

    # ─── v291+ P1: Isotonic 校准 + Brier/ECE ───
    # 把 cal_data 喂入 PAV, 强制实际 5d 涨跌幅在 algo_score 上单调递增
    # 之前 0.7-0.8 桶预测 0.75 实际 -0.58% 严重反向, 0.9-1.0 桶预测 0.95 实际 +5.34% 低估 5.6x
    # 简单线性映射不够, Isotonic/PAV 是非参数单调重映射
    recalibration = None
    brier = None
    ece = None
    if _HAS_CALIBRATION and cal_data:
        try:
            # 按 algo_score 升序排序, PAV 后得到单调递增的"实际收益"映射
            raw_scores = [c["algo_score"] for c in cal_data]
            actuals = [c["actual_5d_pct"] for c in cal_data]
            # PAV 在 actuals 上做单调回归 (假定 algo_score 已排序)
            sorted_pairs = sorted(zip(raw_scores, actuals), key=lambda p: p[0])
            sorted_actuals = [a for _, a in sorted_pairs]
            mono = isotonic_regression(sorted_actuals, increasing=True)
            # 还原: 每个 algo_score 的 PAV 预测值
            recalibrated_pairs = [
                (s, round(m, 3)) for (s, _), m in zip(sorted_pairs, mono)
            ]
            # 校准表 (10 bin, 用 cal_data 输入)
            cal_table = calibration_table(raw_scores, actuals, n_bins=10)
            # Brier: 把 (algo_score, actual) 转成 (P, hit) 用 5% 范围 clip
            # p=algo_score 假设 0.5 中性, > 0.5 看涨, hit=(actual>0)
            preds = []
            hits = []
            for c in cal_data:
                p = max(0.0, min(1.0, c["algo_score"]))
                preds.append(p)
                hits.append(1.0 if c["actual_5d_pct"] > 0 else 0.0)
            brier = round(brier_score(preds, hits), 4)
            ece = round(expected_calibration_error(preds, hits, n_bins=10), 4)
            recalibration = {
                "n_samples": len(cal_data),
                "method": "Isotonic Regression (PAV, PooAdjVio)",
                "sorted_pairs": [
                    {"algo_score": s, "monotone_actual_5d_pct": m}
                    for s, m in recalibrated_pairs[:50]  # 只返回前 50 避免过大
                ],
                "calibration_table": cal_table,
                "brier_score": brier,
                "expected_calibration_error": ece,
                "interpretation": (
                    f"Brier={brier:.3f} (0=完美, 0.25=随机), "
                    f"ECE={ece:.3f} (0=完美校准); "
                    f"用 monotone_actual_5d_pct 替代原 algo_score 的线性预测"
                ),
            }
        except Exception as _exc:
            recalibration = {"error": f"isotonic calibration failed: {_exc}"}
    return {
        "ok": True,
        "days": days,
        "n_moves": len(moves),
        "n_calibrated": len(cal_data),
        "histogram": bins,
        "bucket_stats": bucket_stats,
        "recalibration": recalibration,
        "source": "critical_price_moves + ohlcv_daily 后 5 日回算 + Isotonic PAV",
    }


# ─── v291/attribution-stats 资金流交叉验证 ──────────
@app.get("/api/v291/attribution-stats")
def v291_attribution_stats(hours: Annotated[int, Query(ge=1, le=8760)] = 720) -> dict:
    """
    资金流交叉验证:从 critical_price_moves + external_indicators 真实数据推导
      - 各 asset 的总涨跌幅
      - 涨跌幅 vs algo_score 散点(用于资金流一致性)
      - 高分信号的方向分布
    """
    # v309 P1: 参数名/上限都是**小时** (le=8760 = 一年小时数), 但原来乘的是
    # 86400 (一天的秒数) → 实际窗口比标称大 24 倍 (720h 本意 30 天, 实际 720 天)。
    cutoff = int(time.time()) - hours * 3600
    with db_cursor() as conn:
        cpm = conn.execute(
            "SELECT asset, move_pct, algo_score, move_kind "
            "FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchall()
        cpm_bad = conn.execute(
            "SELECT COUNT(*) FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) > ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchone()[0]
        ei = conn.execute(
            "SELECT asset, value, ts FROM external_indicators ORDER BY ts"
        ).fetchall()

    # 按 asset 统计
    by_asset: dict[str, dict] = {}
    for r in cpm:
        a = r["asset"]
        if a not in by_asset:
            by_asset[a] = {
                "n_moves": 0,
                "sum_pct": 0.0,
                "rise_n": 0,
                "fall_n": 0,
                "sum_score": 0.0,
                "max_score": 0.0,
            }
        d = by_asset[a]
        d["n_moves"] += 1
        d["sum_pct"] += r["move_pct"]
        d["sum_score"] += r["algo_score"]
        d["max_score"] = max(d["max_score"], r["algo_score"])
        if r["move_pct"] > 0:
            d["rise_n"] += 1
        else:
            d["fall_n"] += 1

    # 加外部指标末值
    last_ei: dict[str, float] = {}
    for r in ei:
        last_ei[r["asset"]] = r["value"]

    cross: list[dict] = []
    for a, d in by_asset.items():
        cross.append({
            "asset": a,
            "n_moves": d["n_moves"],
            "avg_move_pct": round(d["sum_pct"] / max(d["n_moves"], 1), 3),
            "rise_n": d["rise_n"],
            "fall_n": d["fall_n"],
            "avg_algo_score": round(d["sum_score"] / max(d["n_moves"], 1), 3),
            "max_algo_score": round(d["max_score"], 3),
            "last_indicator_value": last_ei.get(a),
            "directional_consistency": round(
                (d["rise_n"] - d["fall_n"]) / max(d["n_moves"], 1), 3
            ),
        })

    # v302+ P1: 同时返回 by_asset (list, 保留原前端用法) + by_asset_dict (dict, 给审计/快速 lookup)
    by_asset_dict = {row["asset"]: {
        "n_moves": row["n_moves"],
        "avg_move_pct": row["avg_move_pct"],
        "rise_n": row["rise_n"],
        "fall_n": row["fall_n"],
        "avg_algo_score": row["avg_algo_score"],
        "max_algo_score": row["max_algo_score"],
        "last_indicator_value": row["last_indicator_value"],
        "directional_consistency": row["directional_consistency"],
    } for row in cross}
    return {
        "ok": True,
        "hours": hours,
        "by_asset": cross,
        "by_asset_dict": by_asset_dict,
        "n_assets": len(by_asset_dict),
        "data_sources": ["critical_price_moves", "external_indicators"],
        "data_quality": {
            "implausible_move_rows_excluded": cpm_bad,
            "max_plausible_move_pct": MAX_PLAUSIBLE_MOVE_PCT,
            "note": "已剔除 |move_pct|>90% 的量纲污染行 (价格序列混档, 非市场异动)",
        },
        "note": "完全基于真实 critical_moves + external_indicators 计算;无伪造. v302+ 同时给 list + dict",
    }


# ─── 因果图谱 builder (causal-graph) — P1 升级 ────────
@app.get("/api/v291/causal-graph")
def v291_causal_graph(
    asset: Annotated[str, Query()],
    hours: Annotated[int, Query(ge=1, le=8760)] = 720,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    """2026-11-18: 实现已下沉到 `bridge_panels`，这里只保留签名与参数校验。"""
    from bridge_panels import v291_causal_graph as _impl

    return _impl(asset, hours, limit)


# ─── /api/v291/cross-asset-chains (P1: 跨资产传导链路) ───
@app.get("/api/v291/cross-asset-chains")
def v291_cross_asset_chains(
    asset: Annotated[str | None, Query(description="过滤指定资产 (None=全部)")] = None,
    active_only: Annotated[bool, Query(description="只看 active=1 的链路")] = True,
) -> dict:
    """v291+ P1: 跨资产传导链路 (causal_chains 表 5+ 行)

    返回触发器驱动的多步传导链路, 用于:
    - 风险预警: 监测 trigger 触发概率
    - 投决辅助: 跨资产/宏观→个股的多步影响
    - 解释归因: 哪条链路被触发 → 对应资产下一步预测

    chain_steps 是 JSON 数组, 每个 step 含 node / evidence_type / lag_min
    """
    where: list[str] = []
    params: list[Any] = []
    if active_only:
        where.append("active=1")
    if asset:
        where.append("asset=?")
        params.append(asset.upper())
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    with db_cursor() as conn:
        rows = conn.execute(
            f"SELECT chain_id, asset, trigger_pattern, chain_steps, confidence, "
            f"hit_count, miss_count, source, last_verified_ts, created_at, updated_at "
            f"FROM causal_chains {where_clause} ORDER BY confidence DESC, asset ASC",
            params,
        ).fetchall()

    chains: list[dict] = []
    for r in rows:
        steps_raw = r["chain_steps"] or "[]"
        try:
            steps = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
        except Exception:
            steps = []
        n_steps = len(steps)
        total_lag_min = sum(int(s.get("lag_min", 0)) for s in steps)
        hit = r["hit_count"] or 0
        miss = r["miss_count"] or 0
        hit_rate = hit / (hit + miss) if (hit + miss) > 0 else None
        chains.append({
            "chain_id": r["chain_id"],
            "asset": r["asset"],
            "trigger_pattern": r["trigger_pattern"],
            "n_steps": n_steps,
            "total_lag_min": total_lag_min,
            "confidence": r["confidence"],
            "hit_count": hit,
            "miss_count": miss,
            "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
            "source": r["source"],
            "last_verified_ts": r["last_verified_ts"],
            "steps": steps,
        })

    return {
        "ok": True,
        "n_chains": len(chains),
        "assets": sorted({c["asset"] for c in chains}),
        "trigger_patterns": sorted({c["trigger_pattern"] for c in chains}),
        "chains": chains,
        "data_source": "causal_chains",
        "ts": int(time.time()),
    }


# ─── /api/v291/reflections/summary (P1: 反思摘要聚合) ───
@app.get("/api/v291/reflections/summary")
def v291_reflections_summary(
    asset: Annotated[str | None, Query(description="过滤资产 (None=全部)")] = None,
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
) -> dict:
    """2026-11-18: 实现已下沉到 `bridge_analytics`，这里只保留签名与参数校验。"""
    from bridge_analytics import v291_reflections_summary as _impl

    return _impl(asset, days)


# ─── v309 P0-1: 三维度映射表 (口径审计) ────────────────────────────────
@app.get("/api/v237/reflection-dimension-map")
def v237_reflection_dimension_map() -> Any:
    """返回 A/B/C 三维度归因口径的**单一权威映射表**。

    用途: 让前端/运维能核对"某个 E 码 / reason_code 会被算进哪个桶",
    避免口径漂移后前端按 A/B/C 配色串桶却查不出原因。
    映射定义在 reflection_dimension_map.py (纯数据, 零依赖)。
    """
    try:
        from review import reflection_dimension_map as _rdm
    except Exception as exc:
        return err(f"reflection_dimension_map 导入失败: {type(exc).__name__}: {exc}")
    return {"ok": True, **_rdm.describe_mapping()}


@app.get("/api/v237/reflection-dimension-stats")
def v237_reflection_dimension_stats(
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
) -> Any:
    """2026-11-18: 实现已下沉到 `bridge_panels`，这里只保留签名与参数校验。"""
    from bridge_panels import v237_reflection_dimension_stats as _impl

    return _impl(days)


# ─── /api/review/snapshot ───────────────────────────────────────────────
# v310 P2: 旧实现是 bridge 自己的**简化版**, 与 8000 口径完全不一致:
#   - 数据源用 trade_reflections_v2 (反思记录), 而不是真实持仓 ai_decisions
#   - **忽略时间窗口** (days/from_ts/to_ts 全没用上) → "今日"显示全期累计
#   - tracking 恒 0、settled = total (跟踪中的仓位被当成已结算)
#   - avg_pnl = AVG(actual_outcome_pct) → 该列被污染, 界面上出现"平均 +117.44%"
#   - profit_loss_ratio / expected_value_per_trade 恒为 null
# 结果: 前端"真实持仓战绩"显示 2233 笔 / 胜率 13.3% / 平均 +117.44%, 与模拟盘
# (/api/replay/stats: 15 笔 / 胜率 50% / 平均 -0.81%) 完全对不上。
#
# 现在: 8000 与 8100 **共用 review_snapshot.build_snapshot()** —— 同一份代码、
# 同一个 DB、同一个口径, 不再各算各的。

# ─── /api/review/snapshot ───────────────────────────────────────────────
# 2026-11-16: 原实现删掉后一度由挂载的 api_server 版本接管，实测**每次 20~30s** ——
#   因为那份实现用 8000 的 sqlite 连接参数（timeout=20、非 db_cursor），在 8100 进程里
#   跟自己的写库抢锁，每次都踩满超时（删之前这里 21ms）。所以保留这层薄转发：
#   直接问 8000 要（它有自己的 4s 缓存，实测 14ms），失败才走共享模块本地兜底。
def _forward_8000(path_q: str, timeout: float = 8.0):
    """转发给 8000 同路径，返回解析后的 JSON；失败抛异常由调用方兜底。

    为什么转发而不是复刻：8000 那份实现跑在自己的进程里，搬进来后 sqlite 连接参数
    / 线程池都是 bridge 进程的，会和 bridge 自己的写库抢锁（review/snapshot 踩过 20s）。
    """
    import urllib.request as _ur
    import config as _cfg
    _opener = _ur.build_opener(_ur.ProxyHandler({}))  # 绕系统代理
    _url = _cfg.BRIDGE_BASE_URL + path_q
    with _opener.open(_url, timeout=timeout) as _resp:
        return json.loads(_resp.read())


@app.get("/api/review/snapshot")
async def review_snapshot(
    hours: Annotated[Optional[int], Query(ge=1, le=8760)] = None,
    days: Annotated[Optional[int], Query(ge=1, le=3650)] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """反思系统总览（权威实现 routers.review_run.get_review_snapshot）。

    days 是旧参数（保留兼容），换算成 hours = days * 24。
    v455: 原先自转发到 8100 自身形成自循环超时，改为进程内直调；失败再走本地 build_snapshot。
    """
    if hours is None and days is not None:
        hours = int(days) * 24
    try:
        from routers.review_run import get_review_snapshot
        return await get_review_snapshot(
            hours=hours, days=days, from_ts=from_ts, to_ts=to_ts
        )
    except Exception as exc:
        print(f"[bridge] review/snapshot 权威实现失败, 本地兜底: {exc}", file=sys.stderr)
    with db_cursor() as conn:
        return review_snapshot_mod.build_snapshot(
            conn, hours=hours, from_ts=from_ts, to_ts=to_ts
        )

# ─── /api/abnormal-signals ───

@app.get("/api/abnormal-signals")
def abnormal_signals(
    signal_kind: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 20,
    asset: str | None = None,
    hours: Annotated[int, Query(ge=1, le=8760)] = 168,
):
    """2026-11-18: 实现已下沉到 `bridge_panels`，这里只保留签名与参数校验。"""
    from bridge_panels import abnormal_signals as _impl

    return _impl(signal_kind, status, limit, asset, hours)


@app.get("/api/abnormal/feedback-stats")
async def abnormal_feedback_stats(
    asset: str = "BTC",
    interval: str = "10m",
    lookback_bars: int = 144,
    forward_window: int = 5,
    big_move_pct: float = 0.6,
):
    """v456: 进程内直调 routers.abnormal.get_abnormal_feedback_stats。

    原先走 catch-all → _forward_to_api 自转发回 8100 自身，同进程 HTTP 回环叠加
    外网行情源慢响应时整线程挂到超时；这里与 filter-stream/causal-graph 一样直调，
    失败再由底层返回空指标兜底，不白屏。
    """
    try:
        from routers.abnormal import get_abnormal_feedback_stats
        return await get_abnormal_feedback_stats(
            asset=asset, interval=interval, lookback_bars=lookback_bars,
            forward_window=forward_window, big_move_pct=big_move_pct,
        )
    except Exception as exc:
        print(f"[bridge] abnormal/feedback-stats 直调失败: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": f"feedback-stats 暂不可用: {type(exc).__name__}",
            "asset": asset, "interval": interval,
            "forward_precision": 0.0, "reverse_recall": 0.0,
            "missed_count": 0, "pass_block_rate": 0.0, "retryable": True,
        })


# ─── /api/attribution/event-scatter (v344) ───────────────────────────────
# 事件效力散点：X = 因果评分（事件→异动的匹配强度），Y = 异动幅度（%）。
#   右上角 = 高确信 + 大幅波动（真正能推动价格的事件）；右下角 = 高确信但幅度小（噪音级）；
#   左侧 = 弱匹配（大概率是时间巧合）。按 move_kind 着色，方便看快涨/慢涨的分布差异。
@app.get("/api/attribution/ai-verify-queue")
def attribution_ai_verify_queue():
    """v340: AI 复核队列的进度与覆盖率（只读）。

    归因页要能回答「这 30 条里有多少已经过了第二道验证」。
    队列本身由 attribution_ai_verify.py（cron 每 30 分钟）消费。
    """
    try:
        import attribution_ai_queue as _q
    except Exception:
        return {"ok": False, "error": "attribution_ai_queue 不可用"}
    with db_cursor() as conn:
        try:
            st = _q.stats(conn)   # 只读：表不存在时 stats() 内部已兜底为空
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, **st}


@app.get("/api/attribution/event-scatter")
def attribution_event_scatter(
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=10, le=1000)] = 400,
    asset: str = "",
):
    import time as _t
    since = int(_t.time()) - days * 86400
    sql = (
        "SELECT asset, COALESCE(algo_score,0), COALESCE(move_pct,0), "
        "       COALESCE(move_kind,''), COALESCE(time_lag_sec,0), COALESCE(move_ts,0) "
        "FROM event_move_attribution WHERE COALESCE(move_ts,0) >= ?"
    )
    params: list = [since]
    if asset:
        sql += " AND asset = ?"
        params.append(asset.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        rej = conn.execute(
            "SELECT asset, COALESCE(algo_score,0), COALESCE(move_pct,0), "
            "       COALESCE(move_kind,''), COALESCE(reject_reason,'') "
            "FROM event_move_attribution_rejected "
            "WHERE COALESCE(move_ts,0) >= ? AND COALESCE(reject_reason,'') LIKE '%不成立%' "
            "ORDER BY id DESC LIMIT ?", (since, min(limit, 200))).fetchall()

    pts = [{
        "asset": a, "x": round(float(sc), 3), "y": round(abs(float(mv)), 2),
        "kind": k, "lag_sec": int(lag), "ts": int(ts), "rejected": 0,
    } for a, sc, mv, k, lag, ts in rows]
    rej_pts = [{
        "asset": a, "x": round(float(sc), 3), "y": round(abs(float(mv)), 2),
        "kind": k, "rejected": 1, "reason": rs[:60],
    } for a, sc, mv, k, rs in rej]
    allpts = pts + rej_pts
    by_kind: dict[str, int] = {}
    for p_ in pts:
        by_kind[p_["kind"]] = by_kind.get(p_["kind"], 0) + 1
    # 高确信 + 大幅：真正“有效”的一类
    effective = [p_ for p_ in pts if p_["x"] >= 0.8 and p_["y"] >= 1.0]
    return {
        "ok": True,
        "days": days,
        "points": allpts,
        "n": len(allpts),
        "n_effective": len(effective),
        "by_kind": by_kind,
        "x_label": "因果评分（事件↔异动匹配强度）",
        "y_label": "异动幅度 |%|",
        "data_source": "event_move_attribution(+_rejected)",
    }


# ─── /api/analysis/error-taxonomy (v344) ─────────────────────────────────
# 三类错误仪表盘：买卖信号判断 / 方向框架 / 技术故障。
#   用户口径（2026-11-14）：
#     · 买卖信号判断失误 = 对市场还不够了解，心理预期与形态预期没达标
#     · 信息分析方向错误 = 逻辑框架本身错了（事件→方向的推理链不成立）
#     · 技术错误 = 数据缺失、信号慢 这类工程问题
@app.get("/api/analysis/error-taxonomy")
def analysis_error_taxonomy(hours: Annotated[int, Query(ge=1, le=8760)] = 168):
    """2026-11-18: 实现已下沉到 `bridge_analytics`，这里只保留签名与参数校验。"""
    from bridge_analytics import analysis_error_taxonomy as _impl

    return _impl(hours)


# ─── /api/sources/alerts (8000 一直 500, 454 calls/d) ───
@app.get("/api/sources/alerts")
def sources_alerts(
    hours: Annotated[int, Query(ge=1, le=8760)] = 24,
    severity_min: str = "low",
):
    """2026-11-18: 实现已下沉到 `bridge_analytics`，这里只保留签名与参数校验。"""
    from bridge_analytics import sources_alerts as _impl

    return _impl(hours, severity_min)


# ─── /api/trading/signals (8000 一直 500, 160 calls/d) ───
_SIGNAL_SQL_DECISIONS = """
    SELECT ad.id                                        AS id,
           COALESCE(rn.ts, ad.ts)                       AS evt_ts,
           rn.timestamp                                 AS news_time_iso,
           UPPER(ad.target_asset)                       AS asset,
           UPPER(ad.suggested_action)                   AS action,
           ad.sentiment_score                           AS score,
           ad.reasoning                                 AS reasoning,
           ad.created_at                                AS created_at,
           ad.news_id                                   AS news_id,
           COALESCE(substr(rn.content, 1, 180), '')     AS news_title,
           COALESCE(ad.market_category, '')             AS market_category,
           COALESCE(ad.event_strength, '')              AS event_strength,
           COALESCE(ad.direct_catalyst, 0)              AS direct_catalyst,
           tr.pnl_pct                                   AS pnl_pct,
           tr.confidence_at_time                        AS confidence,
           tr.hit                                       AS hit,
           COALESCE(tr.dimension, '')                   AS dimension
      FROM ai_decisions ad
      JOIN raw_news rn ON rn.id = ad.news_id
      LEFT JOIN (
            SELECT COALESCE(decision_id, ai_decision_id) AS did,
                   MAX(pnl_pct)            AS pnl_pct,
                   MAX(confidence_at_time) AS confidence_at_time,
                   MAX(hit)                AS hit,
                   MAX(dimension)          AS dimension
              FROM trade_reflections_v2
             WHERE decision_id IS NOT NULL OR ai_decision_id IS NOT NULL
             GROUP BY COALESCE(decision_id, ai_decision_id)
      ) tr ON tr.did = ad.id
     WHERE COALESCE(rn.ts, ad.ts) >= ? AND COALESCE(rn.ts, ad.ts) <= ?
"""

_SIGNAL_SQL_REFLECTIONS = """
    SELECT r.id                    AS id,
           COALESCE(rn.ts, 0)      AS evt_ts,
           rn.timestamp             AS news_time_iso,
           UPPER(COALESCE(r.asset, '')) AS asset,
           UPPER(r.action)          AS action,
           COALESCE(r.confidence_at_time, 0) AS score,
           COALESCE(r.lesson, r.root_cause, '') AS reasoning,
           r.created_at             AS created_at,
           COALESCE(r.ai_decision_id, r.decision_id, 0) AS news_id,
           ''                       AS news_title,
           ''                       AS market_category,
           ''                       AS event_strength,
           0                        AS direct_catalyst,
           r.pnl_pct                AS pnl_pct,
           r.confidence_at_time     AS confidence,
           r.hit                    AS hit,
           COALESCE(r.dimension, '') AS dimension
      FROM trade_reflections_v2 r
      LEFT JOIN ai_decisions d ON d.id = COALESCE(r.ai_decision_id, r.decision_id)
      LEFT JOIN raw_news rn ON rn.id = d.news_id  -- v345 修错列: reflections 表无 r.news_id
     WHERE UPPER(r.action) IN ('BUY', 'SELL')
"""


def _signal_row_to_dict(r: sqlite3.Row) -> dict:
    """把 SQL 行转成前端 SignalRow 契约 (+ 扩展字段)。"""
    return {
        "id": r["id"],
        "asset": r["asset"],
        # news_time: epoch 秒 (前端 number 分支) —— ISO 串放 news_time_iso 备用
        "news_time": r["evt_ts"],
        "news_time_iso": r["news_time_iso"],
        "action": r["action"],
        "score": r["score"],
        "reasoning": r["reasoning"],
        "created_at": r["created_at"],
        "news_id": r["news_id"],
        "news_title": r["news_title"],
        "market_category": r["market_category"],
        "event_strength": r["event_strength"],
        "direct_catalyst": r["direct_catalyst"],
        "pnl_pct": r["pnl_pct"],
        "confidence": r["confidence"],
        "hit": r["hit"],
        "dimension": r["dimension"],
    }


@app.get("/api/trading/signals")
def trading_signals(
    asset: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    hide_neutral: int = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """真实交易信号 (v318 重写)。

    主源 = ai_decisions (AI 的真实买卖决策, 带新闻原文 + 推理链);
    LEFT JOIN trade_reflections_v2 补已结算的盈亏/置信度/命中。
    主源在窗口内没有 BUY/SELL 时, 兜底用 trade_reflections_v2.action
    (BUY/SELL/REJECTED —— 这里才是**真实**动作列)。

    参数:
      asset        只返回该标的 (UPPER 比对); 空 = 全部
      from_ts/to_ts epoch 秒窗口; 默认最近 30 天
      hide_neutral 1 = 只返回有方向的信号 (BUY/SELL 且 sentiment_score != 0);
                    0 = 额外返回 HOLD (前端按 action 上色, HOLD 不上 K 线)
      limit        最大条数
    """
    frm = int(from_ts) if from_ts else (int(time.time()) - 30 * 86400)
    to = int(to_ts) if to_ts else int(time.time())
    asset_u = (asset or "").strip().upper()
    limit = max(1, min(int(limit or 200), 1000))

    out: list[dict] = []
    source = "ai_decisions"

    with db_cursor() as conn:
        params: list = [frm, to]
        sql = _SIGNAL_SQL_DECISIONS
        if asset_u:
            sql += " AND UPPER(ad.target_asset) = ?"
            params.append(asset_u)
        if hide_neutral:
            sql += (" AND UPPER(ad.suggested_action) IN ('BUY','SELL')"
                    " AND COALESCE(ad.sentiment_score, 0) != 0")
        sql += " ORDER BY evt_ts DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            rows = []
            print(f"[bridge] /trading/signals ai_decisions 查询失败: {exc}", file=sys.stderr)
        for r in rows:
            out.append(_signal_row_to_dict(r))

        # ── 兜底: 主源窗口内**没有有方向的信号** → 用 reflections 里的真实 BUY/SELL ──
        # v318 修: 原来判据是 `if not out` (主源一行都没有才兜底) —— 但主源几乎
        # 永远有 HOLD 行 (ai_decisions 里 HOLD 占 6413/6739), 于是兜底**永远不触发**,
        # 窗口内没有 BUY/SELL 时端点仍然只返回 HOLD → 用户看到的还是"信号全没了"。
        # 正确判据 = 主源窗口内有没有 BUY/SELL。
        has_directional = any(
            (s.get("action") or "").upper() in ("BUY", "SELL") for s in out
        )
        if not has_directional:
            ts_expr = time_utils.sql_bj_ts_expr("r.created_at")
            sql2 = _SIGNAL_SQL_REFLECTIONS
            params2: list = [time_utils.epoch_to_bj_str(frm), time_utils.epoch_to_bj_str(to)]
            sql2 += f" AND {ts_expr} >= ? AND {ts_expr} <= ?"
            if asset_u:
                sql2 += " AND UPPER(COALESCE(r.asset,'')) = ?"
                params2.append(asset_u)
            sql2 += " ORDER BY r.created_at DESC LIMIT ?"
            params2.append(limit)
            try:
                rows2 = conn.execute(sql2, params2).fetchall()
            except Exception as exc:
                rows2 = []
                print(f"[bridge] /trading/signals reflections 查询失败: {exc}", file=sys.stderr)
            fallback: list[dict] = []
            for r in rows2:
                d = _signal_row_to_dict(r)
                # reflections 这条路没有 raw_news.ts 时, created_at 是北京墙钟 TEXT →
                # 换成 epoch 秒, 前端才能落到正确的 K 线桶上
                if not d["news_time"]:
                    ts = time_utils.bj_str_to_epoch(str(r["created_at"] or ""))
                    d["news_time"] = ts or 0
                fallback.append(d)
            if fallback:
                # 兜底结果**替换**主源的 HOLD 行 —— 端点存在的意义是给出有方向的
                # 交易信号, 返回一堆 HOLD 等于回到"信号全没了"的旧 bug。
                out = fallback
                source = "trade_reflections_v2 (fallback: ai_decisions 窗口内无 BUY/SELL)"

    return {
        "ok": True,
        "signals": out,
        "n": len(out),
        "source": source,
        "honest_empty": len(out) == 0,
        "honest_note": (
            "ai_decisions.suggested_action (真实买卖决策) + trade_reflections_v2 补盈亏"
            if source == "ai_decisions"
            else "ai_decisions 在该窗口没有 BUY/SELL, 已回退到 trade_reflections_v2.action"
        ),
    }


# ─── /api/v201/impact/by-asset (8000 一直 500, 160 calls/d) ───






# ── v319: impact 三级数据源 + 单位统一 ──────────────────────────────────────
# 真实数据分布 (2026-10-31 核查生产库):
#   event_kline_impact   —— 主资产 (XAU/BTC/WTI/ETH/SOL) 的事件影响**只在这张表**
#                          (1218 行 XAU / 30d); worker 已算好 event_title/severity/direction。
#   event_move_attribution —— 主资产**是空的**, 只有少数小众品种 → 之前端点对 XAU 永远返回空,
#                          归因页 K 线上一个 impact marker 都没有。
#   critical_price_moves —— 纯价格异动 (无新闻事件链)。
#
# ⚠️ **单位陷阱**: event_kline_impact.impact_pct 是**小数** ((max_high-pre)/pre),
#    0.0033 = 0.33%; 而 event_move_attribution.move_pct / critical_price_moves.move_pct
#    是**百分数** (5.0 = 5%)。前端 marker-noise 的幅度门槛按百分数比较 (1.0 = 1%),
#    不统一的话前端会把所有 marker 判成"幅度不足"直接过滤掉 → 又变成"一个点都没有"。
#    本文件所有出口的 impact_pct 统一为**有符号百分数**, 幅度另存 impact_abs_pct。
# severity 由幅度推导 (move_pct 单位是**百分数**): >=7 critical / >=5 high / >=3 medium






@app.get("/api/v201/impact/by-asset")
def impact_by_asset(
    asset: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    severity_min: str = "low",
    min_abs_pct: float = 0.0,
    order: str = "time",   # time (最新优先) | magnitude (幅度最大优先)
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """2026-11-18: 实现已下沉到 `bridge_analytics`，这里只保留签名与参数校验。"""
    from bridge_analytics import impact_by_asset as _impl

    return _impl(asset, from_ts, to_ts, severity_min, min_abs_pct, order, limit)


# ─── /api/v201/event/opportunities (v319+: 时点口径的事件机会卡) ───
@app.get("/api/v201/event/opportunities")
def event_opportunities(
    asset: str = "",
    from_ts: int | None = None,
    to_ts: int | None = None,
    horizon_minutes: Annotated[int, Query(ge=5, le=10080)] = 240,
    lookback_days: Annotated[int, Query(ge=1, le=1095)] = 180,
    entry_mode: str = "auto",       # auto | confirm | immediate
    min_abs_edge_pct: float = 0.0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """**时点口径**的事件机会卡 (v319+)。

    每张卡只用该事件**时点之前**的数据:
      · 期望方向/幅度 ← 同类历史事件的条件统计 (event_time 之前);
      · 波动率 σ     ← 事件之前的日线;
      · 成本门槛     ← cost_model (fee + slippage, 往返);
      · 交收时点     ← T+0 / T+1 规则。

    证据不足 / 幅度淹没在噪声 / 扣完成本没优势 → 诚实报 NO_TRADE, 不硬给方向。
    """
    try:
        from event_opportunity import list_opportunities
        now = int(to_ts) if to_ts else int(time.time())
        frm = int(from_ts) if from_ts else (now - 7 * 86400)
        with db_cursor() as conn:
            return list_opportunities(
                conn,
                asset=asset or "",
                from_ts=frm,
                to_ts=now,
                limit=limit,
                horizon_minutes=horizon_minutes,
                lookback_days=lookback_days,
                entry_mode=entry_mode,
                min_abs_edge_pct=min_abs_edge_pct,
            )
    except Exception as exc:  # 机会卡算不出来 → 诚实报错, 不假装没有机会
        logger.warning("event_opportunities failed: %s", exc)
        return {
            "ok": False,
            "n": 0,
            "ready": 0,
            "items": [],
            "error": str(exc),
            "honest_note": "机会卡计算失败 (不是'没有机会', 是算不出来)",
        }


# ─── /api/ohlcv/daily (P0-1: 解锁 K线/成交量能力) ───
@app.get("/api/ohlcv/daily")
def ohlcv_daily(
    asset: str,
    days: Annotated[int, Query(ge=1, le=3650)] = 365,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
):
    """P0-1: 真实 OHLCV 全字段 (yfinance 拉取, 存 ohlcv_daily 表)
    返回按 ts 倒序的 K 线, 含 open/high/low/close/volume
    """
    since = int(time.time()) - days * 86400
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume "
            "FROM ohlcv_daily "
            "WHERE asset=? AND ts>=? "
            "ORDER BY ts DESC LIMIT ?",
            (asset, since, limit),
        ).fetchall()
    bars = [
        {
            "ts": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5],
        }
        for r in rows
    ]
    return {
        "ok": True,
        "asset": asset,
        "n": len(bars),
        "bars": bars,
        "source": "ohlcv_daily (yfinance 真实拉取, P0-1)",
    }


# ─── /api/macro/context (v291+ P1: 资金流聚合) ───
# 8000 端点返 status=empty / items=[] 时, 前端 FlowsPanel 显示"暂无数据"
# v291+ 用 cftc_cot_data 表的真实 14 品种持仓数据 (2028 行) 兜底
# 包含: BTC/ETH/GOLD/OIL/SILVER/NG/ES/NQ/AUD/CAD/EUR/CORN/WHEAT 13 个 + 其他

@app.get("/api/macro/context")
def macro_context(refresh: int = 0, asset: str | None = None):
    """v291+ 资金流聚合上下文 (COT 主源 + 期权/股票/外汇等扩展)

    refresh=0 → 读快照 / 实时聚合
    refresh=1 → 实时拉取并落库 (后端由 macro_pull_worker 触发, 此处只读)

    Returns:
      status: 'ok' | 'empty'
      items:  资金流条目 list
      ts:     拉取时间
    """
    now = int(time.time())
    items: list[dict] = []

    # ── 1) COT 持仓信号 (cftc_cot_data, 13 品种 × 26 周 = 338 条) ──
    with db_cursor() as conn:
        cot_query = """
            SELECT asset, cftc_contract_name, report_date, open_interest,
                   noncomm_net, spec_index, change_oi
            FROM cftc_cot_data
            WHERE spec_index IS NOT NULL
        """
        params: list[Any] = []
        if asset:
            cot_query += " AND asset = ?"
            params.append(asset)
        cot_query += " ORDER BY report_date DESC LIMIT 30"
        cot_rows = conn.execute(cot_query, params).fetchall()

    for r in cot_rows:
        asset_name = r["asset"]
        contract_name = r["cftc_contract_name"]
        report_date = r["report_date"]
        z = r["spec_index"]
        oi = r["open_interest"]
        noncomm_net = r["noncomm_net"]
        change_oi = r["change_oi"]

        # 反向指标语义
        if z < 0:
            direction = "long"
            tag = "极空"
            contrarian = "看涨反转"
        else:
            direction = "short"
            tag = "极多"
            contrarian = "看跌反转"

        items.append({
            "ts": now,
            "source": "cftc_cot",
            "asset": asset_name,
            "metric": f"投机净持仓 z={z:+.2f} ({contrarian})",
            "value": round(z, 3),
            "direction": direction,
            "details": {
                "contract_name": contract_name,
                "report_date": report_date,
                "open_interest": oi,
                "noncomm_net": noncomm_net,
                "change_oi": change_oi,
                "spec_index": round(z, 3) if z is not None else None,
                "tag": tag,
            },
        })

    # ── 2) 资金费率 / 永续多空比 (placeholder, 等接币安/OKX 后填充) ──
    # 当前 DB 无永续数据, 留空由前端检测后兜底

    if not items:
        return {
            "ok": True,
            "status": "empty",
            "items": items,
            "note": "尚无沉淀, 带 refresh=1 拉取",
            "ts": now,
        }

    return {
        "ok": True,
        "status": "ok",
        "items": items,
        "n": len(items),
        "ts": now,
        "data_sources": ["cftc_cot_data"],
        "note": "v291+ 从 cftc_cot_data (2028 行 / 13 品种) 直接聚合",
    }


# ─── /api/input-layer/snapshot (v476: 输入层体检 · 宏观/估值/链上 + 分歧度) ───
# 为什么放桥接: 8000 主服务在写锁竞争下经常整段不可用, 而数据本来就在同一个 SQLite 里;
# 这里只读、不触发任何抓取, 不给引擎增加负担。
_INPUT_LAYER_MACRO = {
    "valuation": None,      # 全量
    "auction": None,
    "tic": None,
    "onchain": None,
    "defi_tvl": None,
    "chain_fees": None,
    "etf_flow": None,
    "index_price": None,
    "fred": [
        "dgs2", "dgs5", "dgs10", "dgs30", "effr_fred",
        "hy_oas", "baa10y", "cpi_yoy", "core_cpi_yoy", "m2_yoy", "dxy",
    ],
    "equity": ["eq.pe", "eq.pb", "eq.peg", "eq.roe", "eq.net_margin", "eq.mktcap"],
}


def _dispersion_from_components(fund_score: float | None, comps: dict) -> dict | None:
    """按 v472 口径从分量重算分歧度。

    为什么不直接读列: decision_fundamental_snapshot.dispersion 目前全为 NULL
    （v472 只在返回值里给出，写入链路尚未落列）。用同一份 components 重算，
    公式与 v472 完全一致；口径写死在这里，前端不再抄第二遍。
    """
    vals = [
        (c.get("score"), c.get("weight"))
        for c in comps.values()
        if isinstance(c, dict) and c.get("score") is not None and c.get("weight")
    ]
    if not vals:
        return None
    tw = sum(w for _, w in vals)
    if tw <= 0:
        return None
    score = fund_score if fund_score is not None else sum(s * w for s, w in vals) / tw
    var = sum(w * (s - score) ** 2 for s, w in vals) / tw
    disp = var ** 0.5
    level = "高" if disp >= 0.60 else ("中" if disp >= 0.35 else "低")
    conf = max(0.0, 1.0 - disp / 1.5)
    bull = sum(w for s, w in vals if s > 0) / tw
    bear = sum(w for s, w in vals if s < 0) / tw
    return {
        "dispersion": round(disp, 3),
        "dispersion_level": level,
        "confidence": round(conf, 3),
        "bull_weight": round(bull, 3),
        "bear_weight": round(bear, 3),
        "components_n": len(vals),
        "recomputed": True,
    }


@app.get("/api/input-layer/snapshot")
def input_layer_snapshot(assets: str | None = None, max_assets: int = 20):
    """输入层体检：分歧度（按 v472 口径重算）+ 宏观/估值/拍卖/TIC/链上原始读数。

    只读 macro_snapshots 与 decision_fundamental_snapshot，不触发抓取、不下单。
    每条读数都带 observed_at 与 stale_days，过期数据不隐藏、直接标「旧」。
    """
    now = int(time.time())
    out_assets: list[dict] = []
    macro: dict[str, list[dict]] = {}

    with db_cursor() as conn:
        # ── 1) 每个品种最近一次基本面快照（跳过 NONE 占位） ──
        rows = conn.execute(
            """
            SELECT asset, ticker, ts, fund_score, sizing_mult, components,
                   dispersion, dispersion_level, confidence, bull_weight, bear_weight
            FROM decision_fundamental_snapshot
            WHERE asset != 'NONE' AND components IS NOT NULL AND components != ''
            ORDER BY ts DESC
            LIMIT 400
            """
        ).fetchall()
        seen: dict[str, dict] = {}
        for r in rows:
            a = r["asset"]
            if a in seen:
                continue
            seen[a] = {
                "asset": a,
                "ticker": r["ticker"],
                "observed_at": r["ts"],
                "fund_score": round(r["fund_score"], 4) if r["fund_score"] is not None else None,
                "sizing_mult": round(r["sizing_mult"], 4) if r["sizing_mult"] is not None else None,
                "components": r["components"],
                "disp_stored": r["dispersion"],
                "disp_stored_level": r["dispersion_level"],
                "conf_stored": r["confidence"],
                "bull_stored": r["bull_weight"],
                "bear_stored": r["bear_weight"],
            }
        targets = [x.strip().upper() for x in assets.split(",")] if assets else None
        for a, rec in seen.items():
            if targets and a.upper() not in targets:
                continue
            try:
                parsed = json.loads(rec.pop("components") or "{}")
            except Exception:
                parsed = {}
            comps = parsed.get("components") or {}
            # v476：优先用库里已落的值；只有旧行/缺失时才重算。口径同一套，读法不同。
            if rec["disp_stored"] is not None:
                disp = {
                    "dispersion": round(rec["disp_stored"], 3),
                    "dispersion_level": rec["disp_stored_level"],
                    "confidence": round(rec["conf_stored"], 3) if rec["conf_stored"] is not None else None,
                    "bull_weight": rec["bull_stored"],
                    "bear_weight": rec["bear_stored"],
                    "components_n": len([c for c in comps.values() if isinstance(c, dict)]),
                    "recomputed": False,
                }
                disp["source"] = "stored"
            else:
                disp = _dispersion_from_components(rec["fund_score"], comps)
                if disp is not None:
                    disp["source"] = "recomputed"
            if disp is None:
                disp = {
                    "dispersion": None, "dispersion_level": None, "confidence": None,
                    "bull_weight": None, "bear_weight": None, "components_n": 0, "recomputed": False,
                    "source": "missing",
                }
            rec.update(disp)
            rec["available"] = bool(parsed.get("available"))
            rec["reason"] = parsed.get("reason")
            rec["kind"] = parsed.get("kind")
            rec["gaps"] = parsed.get("gaps") or []
            # 分量明细按 |score*weight| 排序，只留前 8 条，够看"票从哪来"
            parts = []
            for name, c in comps.items():
                if not isinstance(c, dict):
                    continue
                s, w = c.get("score"), c.get("weight")
                parts.append({
                    "name": name,
                    "score": round(s, 3) if isinstance(s, (int, float)) else None,
                    "weight": round(w, 3) if isinstance(w, (int, float)) else None,
                    "detail": str(c.get("detail") or "")[:160],
                    "contrib": round((s or 0) * (w or 0), 4),
                })
            parts.sort(key=lambda x: abs(x["contrib"] or 0), reverse=True)
            rec["components"] = parts[:8]
            out_assets.append(rec)

        # ── 2) 宏观/估值/链上：每个 metric_key 取最近一次 ──
        for cat, keys in _INPUT_LAYER_MACRO.items():
            sql = """
                SELECT ts, metric_key, value, unit, status, payload
                FROM macro_snapshots
                WHERE category = ?
            """
            params: list[Any] = [cat]
            if keys:
                ph = ",".join("?" for _ in keys)
                sql += f" AND metric_key IN ({ph})"
                params.extend(keys)
            sql += " GROUP BY metric_key HAVING MAX(ts) ORDER BY metric_key LIMIT 60"
            rows = conn.execute(sql, params).fetchall()
            items = []
            for r in rows:
                try:
                    payload = json.loads(r["payload"] or "{}")
                except Exception:
                    payload = {}
                age_days = int((now - r["ts"]) / 86400) if r["ts"] else None
                items.append({
                    "metric_key": r["metric_key"],
                    "value": round(r["value"], 4) if r["value"] is not None else None,
                    "unit": r["unit"],
                    "status": r["status"],
                    "observed_at": r["ts"],
                    "stale_days": age_days,
                    "stale": bool(age_days is not None and age_days > 7),
                    "label": payload.get("label") or payload.get("note") or "",
                })
            if items:
                macro[cat] = items

    out_assets.sort(key=lambda x: (x.get("dispersion") is None, -(x.get("dispersion") or 0)))
    return {
        "ok": True,
        "ts": now,
        "assets": out_assets[: max(1, min(max_assets, 40))],
        "macro": macro,
        "notes": [
            "分歧度：有落库值直接读（source=stored），旧行按 v472 公式重算（source=recomputed）",
            "只读；不触发抓取、不改仓、不下单",
            "stale=true 表示最近一次读数超过 7 天",

        ],
    }



# ─── /api/decision-thresholds/recent (v476: 生效门槛落库后第一次可查) ───
@app.get("/api/decision-thresholds/recent")
def decision_thresholds_recent(days: int = 7, limit: int = 40):
    """最近决策的三层门槛：原始 / 校准 / 生效（含可信度压低量）。

    为什么单独一个接口：门槛是"闸门为什么没放行"的唯一硬证据。
    没这张表时，只能从 reasoning 文字里猜"是被可信度压掉了还是本来就没到线"。
    """
    now = int(time.time())
    since = now - max(1, min(days, 90)) * 86400
    limit = max(1, min(limit, 200))
    with db_cursor() as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_threshold_snapshot'"
        ).fetchone()
        if not has:
            return {"ok": True, "items": [], "stats": {},
                    "notes": ["门槛表尚未建立：下一次决策写入后自动建表"]}
        rows = conn.execute(
            """SELECT decision_id, ts, asset, action, buy_min, sell_min,
                      buy_min_calibrated, sell_min_calibrated,
                      buy_min_effective, sell_min_effective,
                      confidence, confidence_penalty,
                      action_before_gate, action_after_gate, gate_changed
               FROM decision_threshold_snapshot
               WHERE ts >= ? ORDER BY ts DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*), SUM(gate_changed), AVG(confidence_penalty), MIN(ts), MAX(ts) "
            "FROM decision_threshold_snapshot WHERE ts >= ?", (since,)
        ).fetchone()
    items = [{k: r[k] for k in r.keys()} for r in rows]
    for it in items:
        for k in ("buy_min", "sell_min", "buy_min_calibrated", "sell_min_calibrated",
                  "buy_min_effective", "sell_min_effective", "confidence", "confidence_penalty"):
            if it.get(k) is not None:
                it[k] = round(float(it[k]), 4)
    return {
        "ok": True,
        "items": items,
        "stats": {
            "n": total[0] or 0,
            "gate_changed": total[1] or 0,
            "avg_penalty": round(float(total[2]), 4) if total[2] is not None else None,
            "since": total[3], "until": total[4],
        },
        "notes": ["buy_min_effective = buy_min_calibrated + 可信度收紧量（v475）",
                  "表内只有落库之后的新决策；历史行未留门槛，不反推"],
    }


# ─── /api/ohlcv/pattern-snapshot (P1 预热: K线形态 + 成交量异动) ───
@app.get("/api/ohlcv/pattern-snapshot")
def ohlcv_pattern(asset: str, days: Annotated[int, Query(ge=1, le=3650)] = 90):
    """简单 K线形态 + 成交量异动检测 (基于真实 OHLCV, 无需 ta-lib)
    形态: doji / hammer / shooting_star / marubozu
    异动: volume >= MA20 * 2 → "spike"
    """
    since = int(time.time()) - days * 86400
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume "
            "FROM ohlcv_daily "
            "WHERE asset=? AND ts>=? "
            "ORDER BY ts ASC",
            (asset, since),
        ).fetchall()
    n = len(rows)
    if n < 20:
        return {
            "ok": True, "asset": asset, "n_bars": n,
            "patterns": [], "volume_spikes": [],
            "note": "数据不足,需 ≥20 bars 才能识别形态",
        }
    patterns = []
    spikes = []
    for i, r in enumerate(rows):
        ts, o, h, l, c, v = r
        body = abs(c - o)
        full_range = h - l if h > l else 0.0001
        upper_shadow = h - max(c, o)
        lower_shadow = min(c, o) - l
        # doji: body < range * 0.1
        if body < full_range * 0.1 and full_range > 0:
            patterns.append({"ts": ts, "kind": "doji", "confidence": 0.7})
        # hammer: long lower shadow, small body, near top
        elif lower_shadow > body * 2 and upper_shadow < body * 0.5:
            patterns.append({"ts": ts, "kind": "hammer", "confidence": 0.6})
        # shooting star: long upper shadow, small body
        elif upper_shadow > body * 2 and lower_shadow < body * 0.5:
            patterns.append({"ts": ts, "kind": "shooting_star", "confidence": 0.6})
        # marubozu: body > range * 0.9
        elif body > full_range * 0.9 and full_range > 0:
            patterns.append({"ts": ts, "kind": "marubozu", "confidence": 0.65})
    # volume MA20
    if n >= 20:
        for i in range(19, n):
            window = [rows[j][5] for j in range(i - 19, i + 1) if rows[j][5] > 0]
            if len(window) < 10:
                continue
            ma = sum(window) / len(window)
            v = rows[i][5]
            if v >= ma * 2.0:
                spikes.append({
                    "ts": rows[i][0], "volume": v, "ma20": round(ma, 0),
                    "ratio": round(v / ma, 2) if ma else 0,
                })
    return {
        "ok": True, "asset": asset, "n_bars": n,
        "patterns": patterns[-20:],  # 最近 20 个形态
        "volume_spikes": spikes[-10:],
        "source": "ohlcv_daily + 形态识别 (无 ta-lib,纯几何判定)",
    }


# ─── 实时 critical-move 增量检测 (后台线程) ───────
# ─── trade_decisions 现行结构适配 ─────────────────────────────
# 下面这组 /api/decision/* 与 decision_settle_loop 是 v170 世代遗留代码, 最初按
# 12 列旧结构编写(action / score / confidence / rationale / evidence_json /
# price_at_decision / status / settled_at / actual_outcome_pct)。
# 生产库 trade_decisions 早已演进为 19 列现行结构(属主模块 trade_decisions.py,
# 已有 1999 行真实数据), 且 /api/decision/record 从未有任何 producer 调用。
#
# 关键教训: 原 ensure_trade_decisions_table() 用 CREATE TABLE IF NOT EXISTS 声明
# 旧结构 —— 表已存在, 该语句静默 no-op, 于是列漂移被完全掩盖, 只在运行期以
# "no such column: status" 暴露。故此处改为断言现行结构并显式报缺失。
#
# 旧列 -> 现行列 的映射:
#   action            -> final_action
#   price_at_decision -> entry_price
#   created_at (TEXT) -> decision_ts (REAL epoch)
#     ↑ 旧代码用 int 阈值过滤 TEXT 列, 在 SQLite 的类型序下恒不命中, 是隐性 bug
#   status / settled_at / actual_outcome_pct
#     ↑ 现行结构无对应物, 已由 migrate_trade_decisions_columns.py 作为可空列补齐
_TD_REQUIRED_COLS = (
    "id", "decision_ts", "asset", "intended_action", "final_action",
    "decision_type", "entry_price", "planned_stop", "planned_target",
    "status", "settled_at", "actual_outcome_pct",
)


def ensure_trade_decisions_table() -> None:
    """断言 trade_decisions 为现行结构, 并补齐结算索引。"""
    try:
        with write_db_cursor() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}
            if not cols:
                print("[bridge][FATAL] trade_decisions 表不存在 —— 请运行 "
                      "migrate_trade_decisions_columns.py", file=sys.stderr)
                return
            missing = [c for c in _TD_REQUIRED_COLS if c not in cols]
            if missing:
                print(f"[bridge][FATAL] trade_decisions 缺少列 {missing} —— "
                      f"请运行 migrate_trade_decisions_columns.py", file=sys.stderr)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_td_asset_decision_ts "
                "ON trade_decisions(asset, decision_ts DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_td_status_decision_ts "
                "ON trade_decisions(status, decision_ts)")
    except Exception as e:
        print(f"[bridge] ensure_trade_decisions_table failed: {e}", file=sys.stderr)


def _hit_for(action: str, outcome_pct: float) -> int:
    """命中判定。action 必须是**方向** (BUY/SELL)。

    v310 P0: REJECT 行的 final_action 是 'REJECTED', 不是方向 → 这里恒返回 0,
    导致所有闸门记录的 hit 恒为 0。调用方现在统一传 intended_action
    (系统「本来想做」的方向), 闸门拦对没拦对才有正确语义。
    """
    a = (action or "").strip().upper()
    if a == "BUY":
        return 1 if outcome_pct > 0 else 0
    if a == "SELL":
        return 1 if outcome_pct < 0 else 0
    return 0


def _forward_outcome_pct(conn, asset, from_ts, entry_price, settle_days):
    """按 entry_price 计算 settle_days 个交易日后的真实涨跌(%)。无数据返回 None。

    v310 P0 修复 —— **取价必须与 entry_price 同源**。
    原本这里固定用 ohlcv_daily, 而 entry_price 由 db.resolve_entry_price 回填时
    优先取 ohlcv_intraday (分钟线)。两源同刻收盘价实测偏差很大:
        WTI 绝对均值 3.22% (p10 -8.0%) / ETH 1.32% / BTC 1.04% / XAU 0.37%
    收益幅度本身只有 0.6~3.6%, 被这种量级的口径噪声淹没 —— 实测 WTI 结算胜率
    因此虚高到 100%, 且同一条决策用两种口径算出正负相反的收益。

    v310 P1 —— 口径收敛到 db.forward_outcome_pct (全系统唯一实现):
    bridge / decision_engine / 回填脚本 三处各算各的, 口径一漂移库里数字就互相
    矛盾。这里改成委托, 保证三处永远同源同口径。
    """
    return db.forward_outcome_pct(conn, asset, from_ts, entry_price,
                                  settle_days=settle_days)


def _insert_settlement_reflection(conn, *, decision_id, asset, action,
                                  entry_price, outcome_pct, sub_layer,
                                  guard=None, decision_type=None, hit=None,
                                  intended_action=None) -> None:
    """把结算结果落到 trade_reflections_v2 的现行列上。

    旧代码写的 dimension / subcategory / confidence_at_time / actual_outcome_pct /
    settled / hit / reflection_text 在该表(69 列)中均不存在, 一律会报
    "no such column"。现映射: dimension->layer, actual_outcome_pct->pnl_pct;
    settled_at / created_at 是 TEXT。
    v309 P1: 原来写 datetime('now') → **UTC** 空格格式, 而 paper_reflection_worker
    写的是 **本机本地时间** 的 'T' 分隔格式 → 同一列混两种格式, 窗口过滤必然失真。
    现在统一用 time_utils (北京时区 + 'T' 分隔), 与 worker 落库格式一致。

    v310 P0: 维度归因。原先只写 layer='decision', 该 layer 不在 LAYER_TO_DIMENSION
    里 → 维度/小类全空 → 验收口径下这些行算「非法维度」(1653 行), 反思维度合法率
    只有 22.5%。现在改用 reflection_dimension_map.classify_settlement_row 的权威
    口径 (guard → A / ENTRY 亏损 → B / ENTRY 盈利 → C), 与回填脚本共用同一口径。
    """
    _now_str = time_utils.bj_now_str()
    # v398: 降级决策不写洞察（结算账务不受影响，只是不留反思行）。
    # 84% 的反思曾来自 analysis_degraded=1 的决策 —— 那是"没看见"，不是"看错"。
    try:
        import insight_pool as _ip
        _row = conn.execute(
            "SELECT ai_decision_id FROM trade_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        _ai_id = _row[0] if _row else None
        if _ip.is_degraded_decision(conn, _ai_id):
            return
    except Exception:
        pass
    try:
        from review import reflection_dimension_map as _rdm
        dim, subcat = _rdm.classify_settlement_row(
            guard=guard, decision_type=decision_type, hit=hit,
            intended_action=intended_action or action,
            outcome_pct=outcome_pct)
        layer = {"A": "filter", "B": "analysis", "C": "strategy"}.get(dim, "decision")
    except Exception:
        dim, subcat, layer = "C", "calibration_drift", "strategy"
    # v301-fix: decision_id 是 UNIQUE, 而 paper_reflection_worker 用的是
    # 另一个 id 空间 (ai_decisions.id) —— 冲突时只更新「结算行」
    # (paper_run_id IS NULL), 绝不覆盖 worker 的反思行。
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, asset, action, entry_price, pnl_pct, layer, sub_layer, "
        " root_cause, settled_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(decision_id) DO UPDATE SET "
        "asset=excluded.asset, action=excluded.action, "
        "entry_price=excluded.entry_price, pnl_pct=excluded.pnl_pct, "
        "layer=excluded.layer, sub_layer=excluded.sub_layer, "
        "root_cause=excluded.root_cause, settled_at=excluded.settled_at "
        "WHERE trade_reflections_v2.paper_run_id IS NULL",
        (decision_id, asset, action, entry_price, outcome_pct,
         layer, subcat, f"{action} {outcome_pct:+.2f}% [{dim}/{subcat}]",
         _now_str, _now_str),
    )
    # dimension / subcategory 是与 layer 并存的独立列, 一并写入合法枚举,
    # 让 reflection_dimension_map 与验收口径都读得到 (否则算「非法维度」)。
    try:
        conn.execute(
            "UPDATE trade_reflections_v2 SET dimension=?, subcategory=?, "
            "settled=1, hit=? WHERE decision_id=? AND paper_run_id IS NULL",
            (dim, subcat, int(hit or 0), decision_id),
        )
    except sqlite3.Error as _silent_exc_462:
        print("[bridge_server] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                  % (type(_silent_exc_462).__name__, str(_silent_exc_462)[:120]), flush=True)


ensure_trade_decisions_table()


# v478: 决策归档表必须在**进程启动时**就存在 —— 归档是"删之前先搬走"的后置保障，
# 万一某条清理路径先跑（假数据孤儿清理 / 一键重置），表还不存在就只能真删，
# 胜率样本又会归零。这里幂等建表，失败不阻断启动。
def _init_decision_history_table() -> None:
    try:
        import api_maintenance as _api_maint
        _c = sqlite3.connect(str(DB_PATH), timeout=10)
        _api_maint._ensure_decision_history(_c)
        _c.commit()
        _c.close()
    except Exception as _hist_exc:
        print("[bridge_server] ⚠️ 决策归档表初始化失败: %s" % str(_hist_exc)[:120],
              flush=True)


_init_decision_history_table()


def _valid_news_id(conn, news_id) -> Optional[int]:
    """校验 news_id 是否**真的能追到新闻**, 无效就返回 None (不留死指针)。

    v321.3 P1 (防复发): 「重置交易历史」删掉 news_events / raw_news 之后,
    写入端如果照抄调用方传来的 news_id, 就会又造出一条指向已删行的孤儿指针
    (实测那次留下 1783 条 news_id 孤儿)。这里在**写入前**校验:
      · news_events.id == news_id, 或
      · news_events.news_id == news_id  (值相等也算 —— 追溯靠的就是这个)
    两条都不满足 → 存 NULL (宁可没有, 不存假的)。
    """
    if news_id is None:
        return None
    try:
        nid = int(news_id)
    except (TypeError, ValueError):
        return None
    if nid <= 0:
        return None
    try:
        row = conn.execute(
            "SELECT 1 FROM news_events WHERE id=? OR news_id=? LIMIT 1", (nid, nid)
        ).fetchone()
    except sqlite3.Error as _vn_exc:
        print(f"[bridge_server] ⚠️ news_id 校验失败（原先静默吞掉）: "
              f"{type(_vn_exc).__name__}: {str(_vn_exc)[:100]}", flush=True)
        return nid
    return nid if row else None


# ─── /api/decision/record (P1) ───
@app.post("/api/decision/record")
def decision_record(payload: dict):
    asset = (payload.get("asset") or "").strip().upper()
    action = (payload.get("action") or "HOLD").upper()
    if not asset:
        return {"ok": False, "error": "asset required"}
    if action not in ("BUY", "SELL", "HOLD"):
        return {"ok": False, "error": f"action must be BUY/SELL/HOLD, got {action}"}
    score = float(payload.get("score") or 0)
    conf = float(payload.get("confidence") or 0)
    rationale = str(payload.get("rationale") or "")[:2000]
    evidence = payload.get("evidence_json") or {}
    price = payload.get("price_at_decision")
    if price is not None:
        try:
            price = float(price)
        except (ValueError, TypeError):
            price = None
    with write_db_cursor() as conn:
        # v342: 统一走 decision_writer —— score / confidence 从"只塞 context_json"
        # 改为**落成显式列**（context_json 里那份仍保留，老读者不受影响）。
        now = time.time()
        if price is None:
            # v310 P0: 调用方未传 price_at_decision 时按决策时点回溯行情落库,
            # 避免重演"2101 行里 2074 行 entry_price 为 NULL → 结算链全断"。
            try:
                import db as _db
                price = _db.resolve_entry_price(conn, asset, now)
            except Exception:
                price = None
        from signals import decision_writer as _dw
        did = _dw.write_decision(
            conn,
            asset=asset,
            intended_action=action,
            final_action=action,
            decision_type=payload.get("decision_type") or "ENTRY",
            source=_dw.SOURCE_BRIDGE_API,
            decision_ts=now,
            news_id=_valid_news_id(conn, payload.get("news_id")),
            entry_price=price,
            planned_stop=payload.get("planned_stop"),
            planned_target=payload.get("planned_target"),
            size_usdt=payload.get("size_usdt"),
            deny_reason=None if action == "HOLD" else (rationale[:2000] or None),
            context={"score": score, "confidence": conf, "rationale": rationale,
                     "evidence": evidence},
            score=score if -1.0 <= score <= 1.0 else None,
            confidence=conf if 0.0 <= conf <= 100.0 else None,
            status="open",
            persist_signal_weight=True,
            commit=False,
        )
        # ── v321.3 P0: 回链新闻 (需求 #5) ────────────────────────────────────
        # 旧实现: 决策写完就结束, news_events.trade_decision_id 永远是空的 →
        #         「新闻 → 决策」断链, 断链的新闻进不了归因/反思闭环
        #         (实测只有 1327/8841 = 15.01% 被挂上, 且全是历史手工补的)。
        # 现在: 用 news_decision_link 做多路匹配 (直接 id / 同源键 / ai_decisions
        #       两跳), 命中才挂, 挂不上就**不挂** —— 挂错比不挂更危险。
        try:
            from feeds.news_decision_link import link_news_to_decision as _lnk
            _linked = _lnk(conn, int(did), payload.get("news_id"))
            if _linked:
                conn.commit()
        except Exception as _lnk_exc:
            print(f"[bridge_server] news link skipped: {type(_lnk_exc).__name__}: "
                  f"{str(_lnk_exc)[:120]}", flush=True)
    return {"ok": True, "decision_id": did, "asset": asset, "action": action,
            "score": score, "confidence": conf, "status": "open"}


# ─── /api/decision/settle-now (P1: 立即结算) ───
@app.post("/api/decision/settle-now")
def decision_settle_now(payload: dict):
    """把 status='open' 的决策结算成 'settled', 同步写 trade_reflections_v2"""
    did = payload.get("decision_id")
    if not did:
        return {"ok": False, "error": "decision_id required"}
    days = int(payload.get("days") or 5)
    with write_db_cursor() as conn:
        row = conn.execute(
            "SELECT asset, final_action, entry_price, status, decision_ts, "
            "guard, decision_type, intended_action "
            "FROM trade_decisions WHERE id=?",
            (int(did),),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"decision {did} not found"}
        asset, action, price_at, status, decision_ts = row[:5]
        guard, decision_type, intended_action = row[5], row[6], row[7]
        # v310 P0: REJECT 行的 final_action='REJECTED' 不是方向, 命中判定与维度
        # 归因一律用 intended_action (系统本来想做的方向)。
        direction = (intended_action or action or "").strip().upper()
        if status != "open":
            return {"ok": False, "error": f"decision {did} is {status}, not open"}
        outcome_pct = None
        if price_at and "outcome_pct" not in payload:
            outcome_pct = _forward_outcome_pct(
                conn, asset, decision_ts + (5 - days) * 86400, price_at, days)
        if "outcome_pct" in payload:
            outcome_pct = float(payload["outcome_pct"])
        if outcome_pct is None:
            return {"ok": False, "error": f"no ohlcv forward data for {asset}, provide outcome_pct manually"}
        # v310 P1: |收益| > 90% 必是取价异常 —— 拒绝结算(宁可晚结, 也不写脏数)
        if db.is_implausible_outcome(outcome_pct):
            return {"ok": False,
                    "error": (f"implausible outcome {outcome_pct:+.2f}% for {asset} "
                              f"(entry={price_at}) — 取价异常, 拒绝结算")}
        hit = _hit_for(direction, outcome_pct)
        conn.execute(
            "UPDATE trade_decisions SET status='settled', "
            "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
            (outcome_pct, int(did)),
        )
        _insert_settlement_reflection(
            conn, decision_id=int(did), asset=asset, action=action,
            entry_price=price_at, outcome_pct=outcome_pct, sub_layer="settled_now",
            guard=guard, decision_type=decision_type,
            hit=hit, intended_action=direction)
        # v292+: 实战反馈自动更新 causal_chains.hit/miss (活因果图谱闭环)
        # 用新写入的 reflection_id 反查 reflection_text 占位 (空), 关键词匹配依赖 news 文本
        # 暂无 news_id 关联, 暂以"action + outcome" 方向打标, 关键词因 reflection_text 为空而宽松
        chain_match = _match_chains_for_outcome(
            conn, asset=asset, outcome_pct=outcome_pct, action=action,
            text_blob=f"{action} {asset} settled_now outcome_pct={outcome_pct:+.2f}",
        )
    return {"ok": True, "decision_id": int(did), "settled": True, "asset": asset, "action": action,
            "outcome_pct": outcome_pct, "hit": hit, "reflection_inserted": True,
            "causal_chain_match": chain_match}


# ─── /api/decision/list ───
@app.get("/api/decision/list")
def decision_list(asset: str | None = None, status: str | None = None,
                  limit: Annotated[int, Query(ge=1, le=1000)] = 50):
    with db_cursor() as conn:
        q = ("SELECT id, asset, final_action, decision_type, event_strength, "
             "       status, decision_ts, settled_at, actual_outcome_pct, "
             "       entry_price, deny_reason, context_json "
             "FROM trade_decisions WHERE 1=1")
        params: list = []
        if asset:
            q += " AND asset=?"
            params.append(asset)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY decision_ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    items = []
    n_hit = 0
    for r in rows:
        action = r[2]
        outcome = r[8]
        if action == "BUY":
            hit = 1 if outcome is not None and outcome > 0 else 0
        elif action == "SELL":
            hit = 1 if outcome is not None and outcome < 0 else 0
        else:
            hit = 0
        if r[5] == "settled" and hit:
            n_hit += 1
        ctx = {}
        if r[11]:
            try:
                ctx = json.loads(r[11])
            except (ValueError, TypeError):
                ctx = {}
        items.append({"id": r[0], "asset": r[1], "action": action,
                      "decision_type": r[3], "score": ctx.get("score"),
                      "confidence": ctx.get("confidence"),
                      "event_strength": r[4],
                      "status": r[5], "created_at": r[6], "settled_at": r[7],
                      "actual_outcome_pct": outcome, "price_at_decision": r[9],
                      "rationale": r[10] or ctx.get("rationale"),
                      "hit": hit if r[5] == "settled" else None})
    n_settled = sum(1 for it in items if it["status"] == "settled")
    return {"ok": True, "decisions": items, "n": len(items),
            "n_settled": n_settled, "n_hit": n_hit,
            "winrate_pct": round(n_hit / n_settled * 100, 1) if n_settled else None}


# ─── /api/decision/stats ───
@app.get("/api/decision/stats")
def decision_stats(asset: str | None = None, days: Annotated[int, Query(ge=1, le=3650)] = 90):
    """按动作统计已结算胜率/平均收益。

    v477 口径统一：旧实现读 `trade_decisions`（另一条写入路径，实测从未结算过 →
    永远返回 n_total_settled=0），而复盘页/`/api/decision/list` 读的是
    `ai_decisions` 的 settled 字段 —— 同一批单子两个接口给出互相矛盾的答案。
    现在统一到 `ai_decisions`（复盘页同源），并把 trade_decisions 的非零部分
    额外标注出来，避免"看起来没结过单"。
    """
    since = int(time.time()) - days * 86400
    out: list[dict] = []
    total = 0
    hist_n = 0
    since_txt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since))

    def _merge(rows, acc, from_hist=False):
        for act, n, win, avg_pnl in rows:
            key = str(act or "NONE")
            slot = acc.setdefault(key, {"n": 0, "win": 0, "pnl_sum": 0.0,
                                        "pnl_n": 0, "hist": 0})
            slot["n"] += int(n or 0)
            slot["win"] += int(win or 0)
            if avg_pnl is not None and n:
                slot["pnl_sum"] += float(avg_pnl) * int(n)
                slot["pnl_n"] += int(n)
            if from_hist:
                slot["hist"] += int(n or 0)

    with db_cursor() as conn:
        q = ("SELECT suggested_action, COUNT(*) n, "
             "       SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) win, "
             "       AVG(forward_pnl) avg_pnl "
             "FROM ai_decisions WHERE settled=1 AND created_at>=?")
        params: list = [since_txt]
        if asset:
            q += " AND upper(target_asset)=?"
            params.append(asset.upper())
        q += " GROUP BY suggested_action"
        acc: dict = {}
        _merge(conn.execute(q, params).fetchall(), acc)

        # v477：重置会把 ai_decisions 清空，样本靠 ai_decisions_history 续命
        try:
            qh = ("SELECT suggested_action, COUNT(*) n, "
                  "       SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) win, "
                  "       AVG(forward_pnl) avg_pnl "
                  "FROM ai_decisions_history WHERE settled=1 AND created_at>=?")
            ph: list = [since_txt]
            if asset:
                qh += " AND upper(target_asset)=?"
                ph.append(asset.upper())
            qh += " GROUP BY suggested_action"
            _merge(conn.execute(qh, ph).fetchall(), acc, from_hist=True)
            hist_n = conn.execute(
                "SELECT COUNT(*) FROM ai_decisions_history").fetchone()[0]
        except Exception:
            hist_n = 0

        # 另一条口径（trade_decisions）若真有结算数据，如实附上，不隐藏
        legacy = conn.execute(
            "SELECT COUNT(*) FROM trade_decisions WHERE status='settled' AND decision_ts>=?",
            (since,)).fetchone()[0]

    for act, s in sorted(acc.items(), key=lambda kv: -kv[1]["n"]):
        n = s["n"]
        out.append({
            "action": act, "n_settled": n, "n_win": s["win"],
            "avg_outcome_pct": (round(s["pnl_sum"] / s["pnl_n"], 3)
                                if s["pnl_n"] else None),
            "winrate_pct": round(100.0 * s["win"] / n, 1) if n else None,
            "n_from_history": s["hist"],
        })
        total += n
    return {"ok": True, "source": "ai_decisions(settled)+history",
            "by_action": out, "n_total_settled": total,
            "n_history_total": int(hist_n or 0),
            "legacy_trade_decisions_settled": int(legacy or 0),
            "asset": asset, "days": days,
            "note": "settled 口径与复盘页同源；重置前已结算的决策归档在 "
                    "ai_decisions_history，按 id 去重合并，样本不再随重置归零"}


def realtime_scan_loop() -> None:
    """每 5 分钟扫描 external_indicators 是否有新 tick,如有则计算并插入 critical_price_moves"""
    import backfill_critical_moves
    while True:
        try:
            time.sleep(300)
            with db_cursor() as conn:
                last_ei = conn.execute(
                    "SELECT MAX(ts) FROM external_indicators"
                ).fetchone()[0]
                last_cpm = conn.execute(
                    "SELECT MAX(ts) FROM critical_price_moves"
                ).fetchone()[0]
            if last_ei and (last_cpm is None or last_ei > last_cpm):
                print(f"[bridge] new ticks detected (ei={last_ei} cpm={last_cpm}), scanning...")
                # 简单复用 backfill 逻辑:重新检测 + 增量插入
                # (这里只增量 — 简化版,实际可去重)
                from backfill_critical_moves import fetch_prices, detect_moves
                conn = sqlite3.connect(str(DB_PATH), timeout=10)
                # v308 P1-fix3: fetch_prices 用 r["indicator"] 字典式访问 → 必须 row_factory,
                # 否则这里会抛 "tuple indices must be integers" 并让后台扫描每 5 分钟失败一次。
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                fresh_count = 0
                try:
                    assets = [r[0] for r in conn.execute(
                        "SELECT DISTINCT asset FROM external_indicators"
                    ).fetchall()]
                    for a in assets:
                        prices = fetch_prices(conn, a)
                        if len(prices) < 6:
                            continue
                        moves = detect_moves(prices)
                        for m in moves:
                            m["asset"] = a
                        # 只插比 last_cpm 新的
                        fresh = [m for m in moves if m["ts"] > (last_cpm or 0)]
                        if not fresh:
                            continue
                        conn.executemany(
                            "INSERT INTO critical_price_moves "
                            "(asset,ts,start_price,peak_price,end_price,move_pct,move_kind,duration_min,algo_score,context_json) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [(m["asset"], m["ts"], m["start_price"], m["peak_price"], m["end_price"],
                              m["move_pct"], m["move_kind"], m["duration_min"], m["algo_score"], m["context_json"])
                             for m in fresh],
                        )
                        # v311: 每个品种提交一次 —— 原来整个扫描只有一个事务, 写锁
                        # 从第一个 INSERT 一直独占到全品种扫完, 期间别的写者全被挡
                        conn.commit()
                        fresh_count += len(fresh)
                    conn.commit()
                    print(f"[bridge] inserted {fresh_count} fresh critical_moves")
                finally:
                    conn.close()
        except Exception as e:
            print(f"[bridge] realtime scan error: {e}", file=sys.stderr)


# ─── News 拉取 worker (P0-3) ───────────────────────
NEWS_POLL_SECONDS = 300
NEWS_ASSETS = {
    "BTC", "ETH", "SOL", "BNB", "ADA", "XRP", "DOGE", "WLD", "AVAX", "MATIC", "DOT", "LINK",
    "TON", "TRX", "SHIB", "LTC", "BCH", "ATOM", "UNI", "APT",
    "AAPL", "MSFT", "NVDA", "TSLA", "MU", "AMD", "GOOGL", "META", "AMZN", "NFLX",
    "BABA", "JD", "PDD", "NIO", "XPEV", "LI",
    "000660", "005930", "003550", "005380", "000270",
}

# ticker → 别名列表 (含中文 + 英文全称, ticker 本身也算)
# 中文走 substring 匹配 (CJK 无 word boundary), 英文 ticker 走 alnum word boundary


def _parse_iso_ts(s: str) -> int | None:
    if not s:
        return None
    from datetime import datetime, timezone
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        try:
            return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            return None


def _parse_iso_ts_cst(s: str) -> int | None:
    """解析**北京时间**（无时区标注）的 ISO 时间串。

    v310 补充：eastmoney 的 `showTime` 是北京时间，而 `_parse_iso_ts` 一律
    `replace(tzinfo=timezone.utc)` 按 UTC 解释 —— 东财的 ts 因此比真实时间
    快 8 小时，表现为：
      - 63 行新闻的 ts 落在「未来」（最多 8 小时后），前端显示时间错乱；
      - 与行情异动做时间窗对齐（news_ts vs move_ts）整体错位 8 小时。
    → 东财单独走这条按 UTC+8 解析的路径；其它源不动
      （techflow 的 created_at 本就是 UTC，实测 ts 正确）。
    """
    if not s:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(dt.timestamp())


def _extract_assets(text: str) -> str:
    """提取 text 中的资产 ticker. 中文走 substring, 英文 ticker/alnum 走 word boundary.

    优先级: ticker 出现在别名里的按 ticker 首次出现位置排序, 避免重复.
    """
    if not text:
        return ""
    upper = text.upper()
    matches: list[tuple[str, int]] = []
    for ticker, aliases in NEWS_ASSETS_ALIASES.items():
        best_pos = -1
        for alias in aliases:
            al_upper = alias.upper()
            is_cjk = any("\u4e00" <= c <= "\u9fff" for c in al_upper)
            if is_cjk:
                # CJK: substring 匹配 (text 保留原大小写, upper 也行因为中文不变)
                p = text.find(alias)
                if p < 0:
                    p = upper.find(al_upper)
                if p >= 0 and (best_pos < 0 or p < best_pos):
                    best_pos = p
            else:
                # 英文/数字: alnum word boundary
                scan = 0
                while True:
                    j = upper.find(al_upper, scan)
                    if j < 0:
                        break
                    left = upper[j - 1] if j > 0 else ""
                    right = upper[j + len(al_upper)] if j + len(al_upper) < len(upper) else ""
                    if not left.isalnum() and not right.isalnum():
                        if best_pos < 0 or j < best_pos:
                            best_pos = j
                        break
                    scan = j + len(al_upper)
        if best_pos >= 0:
            matches.append((ticker, best_pos))
    matches.sort(key=lambda x: x[1])
    found: list = []
    seen: set = set()
    for ticker, _ in matches:
        if ticker not in seen:
            seen.add(ticker)
            found.append(ticker)
            if len(found) >= 5:
                break
    return ",".join(found)


# ─── sentiment 关键词扫描 (P0-3) ─────────────────────
# 词典: 关键词 → 权重 [-1, +1]
NEWS_SENTIMENT_LEXICON: list[tuple[list[str], float]] = [
    # 强烈利好
    (["暴涨", "飙升", "创新高", "历史新高", "创历史新高", "突破历史", "大涨", "猛涨",
      "ALL-TIME HIGH", "ATH", "RECORD HIGH", "SURGES", "SURGING", "RALLY", "RALLIES",
      "SOAR", "SOARS", "MOON", "JUMP", "JUMPS", "JUMPED",
      "SKYROCKET", "ROCKET", "PARABOLIC"], 1.0),
    # 一般利好
    (["上涨", "看涨", "增持", "加仓", "利好", "突破", "浮盈", "BULLISH", "GAINS", "GAIN",
      "RISES", "RISING", "GROWTH", "ACCUMULATION", "ACCUMULATE", "BUYS", "BUYING",
      "LONG", "SUPPORT", "RECOVERY", "RECOVERS", "REBOUND"], 0.4),
    # 一般利空
    (["下跌", "看跌", "减持", "减仓", "利空", "下挫", "BEARISH", "DECLINE", "DECLINES",
      "FALL", "FALLS", "DROPS", "DROPPING", "SELLS", "SELLING", "SHORT",
      "RESISTANCE", "WEAKNESS", "WEAK"], -0.4),
    # 强烈利空
    (["暴跌", "崩盘", "闪崩", "腰斩", "重挫", "血洗", "CRASH", "CRASHES", "PLUNGE",
      "PLUNGES", "PLUNGING", "TANK", "TANKS", "COLLAPSE", "COLLAPSES", "SLUMP",
      "DUMP", "DUMPING", "CAPITULATE", "CAPITULATION", "BLOODBATH"], -1.0),
    # 监管/事件 (中性偏事件)
    (["清算", "浮亏", "罚款", "调查", "起诉", "LIQUIDATION", "LIQUIDATED", "HALT",
      "HALTED", "SEC", "CFTC", "DELISTING", "LAWSUIT", "SUE", "SUES", "INDICTED",
      "FRAUD", "FRAUDULENT"], -0.2),
    (["批准", "上市", "合规", "APPROVE", "APPROVES", "APPROVED", "APPROVAL",
      "LISTING", "LAUNCH", "LAUNCHES", "PARTNERSHIP", "PARTNER", "MERGER",
      "ACQUISITION", "ACQUIRES", "BACKED"], 0.3),
]


def _score_sentiment(text: str) -> float:
    """返回 [-1, +1] 情绪分数. 多关键词时取最大绝对值, 累加时限制范围."""
    if not text:
        return 0.0
    upper = text.upper()
    best = 0.0
    for keywords, weight in NEWS_SENTIMENT_LEXICON:
        for kw in keywords:
            al = kw.upper()
            is_cjk = any("\u4e00" <= c <= "\u9fff" for c in al)
            if is_cjk:
                p = text.find(kw)
            else:
                p = upper.find(al)
                if p >= 0:
                    left = upper[p - 1] if p > 0 else ""
                    right = upper[p + len(al)] if p + len(al) < len(upper) else ""
                    if left.isalnum():
                        p = -1
                    elif right.isalnum():
                        # 容忍英文词形变化 s/es/d/ed/ing (PLUNGES, APPROVES, FALLS, GAINED, RISING)
                        tail = right[:5]
                        if not (tail.startswith(("S", "E", "D", "I")) and not right[0].isdigit()):
                            p = -1
            if p >= 0 and abs(weight) > abs(best):
                best = weight
    return max(-1.0, min(1.0, best))


def _pull_techflow() -> list:
    import urllib.request
    import json as _json
    url = "https://www.techflowpost.com/api/client/newsflashes?page=1&page_size=50"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        for item in (data.get("data") or []):
            nid = item.get("id")
            title = item.get("title") or ""
            body = item.get("abstract") or ""
            ts = _parse_iso_ts(item.get("created_at") or "")
            if not (nid and title and ts):
                continue
            assets = _extract_assets(f"{title} {body}")
            cat = (item.get("category") or {}).get("name") or "general"
            out.append({
                "news_id": int(nid), "source": "techflow",
                "headline": title[:500], "body": body[:2000],
                "action": "", "score": 0.0,
                "assets": assets, "ts": ts, "category": cat[:30],
            })
    except Exception as e:
        print(f"[bridge] techflow pull error: {e}", file=sys.stderr)
    return out


def _pull_eastmoney() -> list:
    import urllib.request
    import json as _json
    url = (
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        "?client=web&biz=web_news_col&column=345&order=1&needInteractData=0"
        "&page_index=1&page_size=50&req_trace=bridge&mTypeAndCode=345"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        items = (data.get("data") or {}).get("list") or []
        for item in items:
            nid = item.get("code") or item.get("uniqueUrl") or ""
            if not nid:
                continue
            try:
                news_id = int(str(nid)[-12:])
            except Exception:
                news_id = abs(hash(nid)) % 2_000_000_000
            title = item.get("title") or ""
            body = item.get("summary") or ""
            ts = _parse_iso_ts_cst((item.get("showTime") or "").replace(" ", "T"))
            if not (title and ts):
                continue
            assets = _extract_assets(f"{title} {body}")
            media = item.get("mediaName") or "eastmoney"
            out.append({
                "news_id": news_id, "source": "eastmoney",
                "headline": title[:500], "body": body[:2000],
                "action": "", "score": 0.0,
                "assets": assets, "ts": ts, "media": media[:30],
            })
    except Exception as e:
        print(f"[bridge] eastmoney pull error: {e}", file=sys.stderr)
    return out


# ─── OHLCV 每日自动刷新 (P1 worker 1) ────────────────
# v314 P0 修复 (2026-10-28): 刷新标的必须用**消费方使用的规范代码**。
#
# 旧列表 ["ADA-USD","BNB-USD","DOGE-USD","SOL-USD","XRP-USD","MU","000660.KS"]
# 全是 Yahoo 原始代码, 写进 ohlcv_daily 的 asset 列也是这些代码。而真正
# 消费 ohlcv_daily 的地方用的是**规范代码**:
#   · causal_chain_evaluator._resolve_chain_asset → 从 chain 的 trigger_words
#     里找 "有行情的资产" (asset 列), 找不到就没有 T0/T+5;
#   · decision_truth / 归因页 → 规范代码 (BTC/ETH/SOL/XAU/XAG/WTI/...)。
# 于是: 规范代码的日线**永远停在最后一次手工回填的那天** (实测 WTI/XAU/
# BTC/ETH/SOL 全部停在 2026-10-21), 而刷新循环在写的 "SOL-USD" 没人读。
# 后果: 因果链评估 16 条 PENDING 超过截止时间仍未评估 (超期未评估=16),
# 因为 T+5 那一根本来就不存在。
#
# 现在: 列表用规范代码, 取数时通过 quote_sources.YAHOO_SYMBOL_FOR_ASSET
# 映射到正确的 Yahoo symbol (WTI→CL=F, XAU→GC=F, BTC→BTC-USD ...)。
OHLCV_ASSETS = [
    "BTC", "ETH", "SOL", "XAU", "XAG", "WTI",
    "MU", "000660.KS", "NVDA", "TSLA",
]
YFINANCE_SLEEP_S = 2.5  # rate-limit 防御
YFINANCE_RETRIES = 3
YFINANCE_BACKOFF = [0, 5, 15]  # 二次失败: 等 5s, 三次: 等 15s
OHLCV_REFRESH_HOURS = 24
# v314: "7d" 对期货 (CL=F) 会返回空 (Yahoo 近 7 天日线尚未落库) → 用 1mo,
# 覆盖足够多的交易日, 同时留出补漏空间。
OHLCV_REFRESH_PERIOD = "1mo"

# ── v311 行情源降级链 (2026-10-28 本机出网实测) ────────────────────────────
#   query1.finance.yahoo.com → 不可达 (SSL EOF / fetch failed) ⇒ yfinance 整链哑火,
#                             日志里全品种 "possibly delisted; no price data found"
#   api.binance.com / www.okx.com / api.gateio.ws / api.huobi.pro → 均不可达
#   push2his.eastmoney.com → 可达 (美股日线, secid=105.MU 正常返回到最新交易日)
#   api.coingecko.com       → 可达 (加密 OHLC)
# 所以 yfinance 只能当"能用则用"的第 1 顺位, 不能再当唯一源。
EASTMONEY_KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
    "&klt=101&fqt=1&beg={beg}&end=20500101"
)
EASTMONEY_SECID = {          # 105=NASDAQ 106=NYSE 107=AMEX
    "MU": "105.MU", "AAPL": "105.AAPL", "MSFT": "105.MSFT", "NVDA": "105.NVDA",
    "TSLA": "105.TSLA", "AMD": "105.AMD", "AMZN": "105.AMZN", "GOOGL": "105.GOOGL",
    "META": "105.META", "NFLX": "105.NFLX", "INTC": "105.INTC", "BABA": "105.BABA",
    "JD": "105.JD", "PDD": "105.PDD", "COIN": "105.COIN", "PLTR": "105.PLTR",
}
COINGECKO_ID = {             # 加密: /coins/{id}/ohlc
    "ADA-USD": "cardano", "BNB-USD": "binancecoin", "DOGE-USD": "dogecoin",
    "SOL-USD": "solana", "XRP-USD": "ripple", "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum", "LTC-USD": "litecoin", "ATOM-USD": "cosmos",
}
HTTP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _http_json(url: str, timeout: int = 15):
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": HTTP_UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_eastmoney_daily(asset: str) -> list:
    """东财日线 → [(ts, o, h, l, c, vol)]。无映射/不可达返回 []。"""
    secid = EASTMONEY_SECID.get(asset)
    if not secid:
        return []
    beg = time.strftime("%Y%m%d", time.gmtime(time.time() - 400 * 86400))
    try:
        data = _http_json(EASTMONEY_KLINE_URL.format(secid=secid, beg=beg))
    except Exception as e:
        print(f"[bridge] eastmoney kline {asset} fail: {e}", file=sys.stderr)
        return []
    klines = ((data or {}).get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            # 东财 kline 字段序: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额
            ts = int(datetime.strptime(p[0], "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp())
            out.append((ts, float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                        int(float(p[5]))))
        except Exception:
            continue
    return out


def _fetch_coingecko_daily(asset: str) -> list:
    """CoinGecko OHLC (4h 粒度) 聚合成日线 → [(ts, o, h, l, c, 0)]。"""
    cid = COINGECKO_ID.get(asset)
    if not cid:
        return []
    url = (f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc"
           f"?vs_currency=usd&days=30")
    try:
        raw = _http_json(url, timeout=20)
    except Exception as e:
        print(f"[bridge] coingecko ohlc {asset} fail: {e}", file=sys.stderr)
        return []
    buckets: dict = {}
    for row in raw or []:
        if not row or len(row) < 5:
            continue
        ts_ms, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        if not o or not c:
            continue
        day = int(ts_ms // 86400000)
        b = buckets.get(day)
        if b is None:
            buckets[day] = [o, h, l, c]
        else:
            b[0] = min(b[0], o)
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    return [(day * 86400, b[0], b[1], b[2], b[3], 0) for day, b in sorted(buckets.items())]


def _fetch_ohlcv_daily(asset: str) -> list:
    """降级链取日线: yfinance → 东财(美股) → CoinGecko(加密)。返回 ohlcv_daily 行。

    v314 P0: asset 是**规范代码** (WTI/XAU/BTC/...), 取数前要映射到 Yahoo
    symbol (WTI→CL=F, XAU→GC=F, BTC→BTC-USD) —— 直接 yf.Ticker("WTI")
    取不到任何数据 (Yahoo 没有叫 WTI 的品种)。落库仍用规范代码。
    """
    yahoo_symbol = asset
    try:
        from feeds import quote_sources as _qs
        yahoo_symbol = _qs.YAHOO_SYMBOL_FOR_ASSET.get(asset.upper(), asset)
    except Exception:
        yahoo_symbol = asset
    # ① yfinance (原主源; Yahoo 不可达时快速失败走备源, 不做 3 次退避重试)
    try:
        import yfinance as yf
        import yf_guard as _yfg; _yfg.disable()
        df = yf.Ticker(yahoo_symbol).history(period=OHLCV_REFRESH_PERIOD, auto_adjust=False)
        if df is not None and len(df) > 0:
            rows = []
            for ts_idx, row in df.iterrows():
                ts = int(ts_idx.timestamp())
                rows.append((asset, ts, float(row["Open"]), float(row["High"]),
                             float(row["Low"]), float(row["Close"]),
                             int(row["Volume"]) if row["Volume"] > 0 else 0, "yfinance_refresh"))
            return rows
    except Exception as e:
        print(f"[bridge] ohlcv_refresh {asset} ({yahoo_symbol}) yfinance unavailable: {e}",
              file=sys.stderr)
    # ② 东财 (美股)
    em = _fetch_eastmoney_daily(asset)
    if em:
        return [(asset, ts, o, h, l, c, v, "eastmoney_refresh")
                for (ts, o, h, l, c, v) in em]
    # ③ CoinGecko (加密)
    cg = _fetch_coingecko_daily(asset)
    if cg:
        return [(asset, ts, o, h, l, c, v, "coingecko_refresh")
                for (ts, o, h, l, c, v) in cg]
    # ④⑤ v342 国内可达兜底: 本机 Python(OpenSSL) 到 binance/coingecko 会被 TLS 中途 reset,
    #   而系统 curl(SecureTransport) 能通 —— 详见 market_data_fallback.py 顶部实测记录。
    #   ④ 新浪全球期货日线: XAU=GC / XAG=SI / WTI=CL
    #   ⑤ Binance 现货日线:  BTC/ETH/SOL = *USDT
    try:
        from market import market_data_fallback as _mdf
        cn = _mdf.fetch_sina_futures_daily(asset)
        if cn:
            return [(asset, ts, o, h, l, c, v, "sina_futures_refresh")
                    for (ts, o, h, l, c, v) in cn]
        bn = _mdf.fetch_binance_daily(asset)
        if bn:
            return [(asset, ts, o, h, l, c, v, "binance_refresh")
                    for (ts, o, h, l, c, v) in bn]
    except Exception as _e:
        print(f"[bridge] ohlcv_refresh {asset} curl-fallback fail: {_e}", file=sys.stderr)
    return []


OHLCV_STARTUP_DELAY_S = 120
# v350: 每轮只刷一小批，轮转推进。
# 原来一轮 31 个品种、每个都要等到超时（实测全部源都拿不到数据），
# 这一轮能压着 GIL 和写锁跑十几分钟，期间 /api/health 直接卡死（实测 40s 超时无响应）。
# 现在的做法：一次 4 个品种、每 30 分钟推一轮，全量约 4 小时走完一遍 ——
# 日线本来就不需要更快的节奏，但服务不会再被自己的定时任务拖死。
OHLCV_BATCH_SIZE = 4
OHLCV_ROTATE_SECONDS = 1800
_OHLCV_CURSOR = {"idx": 0}


def _next_ohlcv_batch() -> list:
    total = len(OHLCV_ASSETS)
    if total == 0:
        return []
    start = _OHLCV_CURSOR["idx"] % total
    batch = [OHLCV_ASSETS[(start + i) % total]
             for i in range(min(OHLCV_BATCH_SIZE, total))]
    _OHLCV_CURSOR["idx"] = (start + OHLCV_BATCH_SIZE) % total
    return batch


def ohlcv_refresh_loop() -> None:
    """P1 worker 1: 分批轮转拉最新 OHLCV（幂等 INSERT OR IGNORE）。

    v311: 启动先跑一轮，行情源按 Yahoo → 东财 → CoinGecko 降级。
    v341: 启动先等 120 秒再跑第一轮，让 uvicorn 先站住端口。
    v350: 每轮只处理 4 个品种（见 OHLCV_BATCH_SIZE 注释），不再一口气刷 31 个。
    """
    time.sleep(OHLCV_STARTUP_DELAY_S)
    while True:
        try:
            total_inserted = 0
            for asset in _next_ohlcv_batch():
                rows = _fetch_ohlcv_daily(asset)
                if not rows:
                    print(f"[bridge] ohlcv_refresh {asset}: 所有源都没拿到数据", file=sys.stderr)
                    continue
                with write_db_cursor() as conn:
                    cur = conn.executemany(
                        "INSERT OR IGNORE INTO ohlcv_daily "
                        "(asset,ts,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    total_inserted += cur.rowcount or 0
                time.sleep(YFINANCE_SLEEP_S)
            if total_inserted:
                print(f"[bridge] ohlcv_refresh_loop: +{total_inserted} new bars", flush=True)
        except Exception as e:
            print(f"[bridge] ohlcv_refresh_loop error: {e}", file=sys.stderr)
        time.sleep(OHLCV_ROTATE_SECONDS)


DECISION_SETTLE_HOURS = 1
DECISION_SETTLE_DAYS = 5
# v310 P0: 原 LIMIT 100 —— 2076 条 open 决策要 21 小时才结得完, 结算率长期卡在
# 1.19%。提到 500 后每轮 5 分钟内可清完, 配合启动即跑一次, 新决策不再堆积。
DECISION_SETTLE_BATCH = 500


def _run_decision_settle_batch(limit: int = DECISION_SETTLE_BATCH) -> int:
    """结算一批超期未结算的 open 决策, 返回结算条数。"""
    cutoff = int(time.time()) - DECISION_SETTLE_DAYS * 86400
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, asset, final_action, entry_price, decision_ts, "
            "guard, decision_type, intended_action "
            "FROM trade_decisions WHERE status='open' AND decision_ts<=? LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    if not rows:
        return 0
    n_settled = 0
    with write_db_cursor() as conn:
        for r in rows:
            did = r["id"]
            asset = r["asset"]
            action = r["final_action"]
            direction = (r["intended_action"] or action or "").strip().upper()
            outcome_pct = _forward_outcome_pct(
                conn, asset, r["decision_ts"], r["entry_price"],
                DECISION_SETTLE_DAYS)
            if outcome_pct is None:
                continue
            # v310 P1: |收益| > 90% 必是取价异常(2026-10-26 有 199 行被写成
            # ±90%~±400%, 把「真实持仓业绩」均值拉到 +117%) —— 拒写, 保持 open。
            if db.is_implausible_outcome(outcome_pct):
                print(f"[bridge] implausible outcome for decision {did} ({asset}): "
                      f"{outcome_pct:+.2f}% → 拒写, 保持 open", flush=True)
                continue
            conn.execute(
                "UPDATE trade_decisions SET status='settled', "
                "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
                (outcome_pct, did),
            )
            _insert_settlement_reflection(
                conn, decision_id=did, asset=asset, action=action,
                entry_price=r["entry_price"], outcome_pct=outcome_pct,
                sub_layer="auto_settled_5d",
                guard=r["guard"], decision_type=r["decision_type"],
                hit=_hit_for(direction, outcome_pct), intended_action=direction)
            n_settled += 1
            # v311: 分段提交 —— 原来 500 条结算全挤在一个写事务里, 写锁从
            # 第一条 UPDATE 一直独占到整批结束(每条还要跑 _forward_outcome_pct
            # 的多次 SELECT), 期间所有别的写者全被挡在门外:
            #   [bridge] news batch locked, retry 1/3 … 3/3
            #   [bridge] news insert error (batch): database is locked  ← 整批新闻丢
            # 每 25 条提交一次 → 写锁窗口从"整批"降到 25 条。
            if n_settled % 25 == 0:
                conn.commit()
    return n_settled


def decision_settle_loop() -> None:
    """P1 worker 2: 每 1h 扫描 status='open' 且 decision_ts 超过 5d 的决策,自动结算"""
    # v310 P0: 启动时先跑一次, 避免冷启动后一小时内全是未结算
    try:
        _n = _run_decision_settle_batch()
        if _n:
            print(f"[bridge] decision_settle_loop (startup): settled {_n} decisions",
                  flush=True)
    except Exception as e:
        print(f"[bridge] decision_settle_loop startup error: {e}", file=sys.stderr)
    while True:
        time.sleep(DECISION_SETTLE_HOURS * 3600)
        try:
            n_settled = _run_decision_settle_batch()
            if n_settled:
                print(f"[bridge] decision_settle_loop: settled {n_settled} decisions", flush=True)
        except Exception as e:
            print(f"[bridge] decision_settle_loop error: {e}", file=sys.stderr)


# ─── v302+ P0 因果链评估 worker ─────────────────────
CAUSAL_CHAIN_EVAL_INTERVAL_HOURS = 6  # 每 6h 跑一次 evaluate (增量)
CAUSAL_CHAIN_BACKFILL_INTERVAL_HOURS = 24  # 每 24h 跑一次 backfill (扫近 7 天)
CAUSAL_CHAIN_BACKFILL_DAYS = 7


def causal_chain_eval_loop() -> None:
    """每 CAUSAL_CHAIN_EVAL_INTERVAL_HOURS 跑一次 evaluate_pending + 偶发 backfill.
    启动时立即跑一次 (避免冷启动延迟)."""
    from causal_chain_evaluator import evaluate_pending, backfill
    backfill_counter = 0
    # 启动时立即跑一次 evaluate
    try:
        with write_db_cursor() as conn:
            ev = evaluate_pending(conn)
            if ev.get("evaluated", 0) > 0:
                print(f"[bridge] causal_chain_eval_loop (startup): {ev}", flush=True)
    except Exception as e:
        print(f"[bridge] causal_chain_eval_loop (startup) error: {e}", file=sys.stderr)
    while True:
        time.sleep(CAUSAL_CHAIN_EVAL_INTERVAL_HOURS * 3600)
        try:
            with write_db_cursor() as conn:
                ev = evaluate_pending(conn)
            if ev.get("evaluated", 0) > 0:
                print(f"[bridge] causal_chain_eval_loop: {ev}", flush=True)
            # 每 N 次 evaluate 跑一次 backfill (近 7 天)
            backfill_counter += 1
            if backfill_counter * CAUSAL_CHAIN_EVAL_INTERVAL_HOURS >= CAUSAL_CHAIN_BACKFILL_INTERVAL_HOURS:
                backfill_counter = 0
                with write_db_cursor() as conn:
                    bf = backfill(conn, days=CAUSAL_CHAIN_BACKFILL_DAYS)
                if bf.get("outcomes_inserted", 0) > 0:
                    print(f"[bridge] causal_chain_backfill: {bf}", flush=True)
        except Exception as e:
            print(f"[bridge] causal_chain_eval_loop error: {e}", file=sys.stderr)


# ─── 手动触发: POST /api/ohlcv/refresh-now ───────────
def _ohlcv_refresh_sync(assets: list) -> dict:
    """同步拉取日线 (在线程池里跑, 避免阻塞 event loop)。v311: 走行情源降级链。"""
    inserted = 0
    errors: list = []
    for asset in assets:
        rows = _fetch_ohlcv_daily(asset)
        if not rows:
            errors.append(f"{asset}=no_data")
            continue
        try:
            with write_db_cursor() as conn:
                cur = conn.executemany(
                    "INSERT OR IGNORE INTO ohlcv_daily "
                    "(asset,ts,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                inserted += cur.rowcount or 0
        except Exception as e:
            errors.append(f"{asset}_db={e}")
        time.sleep(YFINANCE_SLEEP_S)
    return {"ok": True, "inserted": inserted, "errors": errors, "n_assets": len(assets)}


@app.post("/api/ohlcv/refresh-now")
async def ohlcv_refresh_now(request: Request):
    """P1: 立即跑一次 OHLCV 增量刷新 (可指定 assets)"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    assets = payload.get("assets") or OHLCV_ASSETS
    return await run_in_threadpool(_ohlcv_refresh_sync, assets)


# ─── 手动触发: POST /api/decision/auto-settle-now ────
@app.post("/api/decision/auto-settle-now")
async def decision_auto_settle_now(request: Request):
    """P1: 立即跑一次决策自动结算 (无需等 worker)"""
    try:
        await request.json()  # 接受任何 body 但不解析
    except Exception:
        pass
    cutoff = int(time.time()) - DECISION_SETTLE_DAYS * 86400
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, asset, final_action, entry_price, decision_ts, "
            "guard, decision_type, intended_action "
            "FROM trade_decisions WHERE status='open' AND decision_ts<=? LIMIT 200",
            (cutoff,),
        ).fetchall()
    n_total = len(rows)
    n_settled = 0
    n_skipped = 0
    with write_db_cursor() as conn:
        for r in rows:
            outcome_pct = _forward_outcome_pct(
                conn, r["asset"], r["decision_ts"], r["entry_price"],
                DECISION_SETTLE_DAYS)
            if outcome_pct is None:
                n_skipped += 1
                continue
            # v310 P1: 收益幅度不合理 → 拒写(同 _run_decision_settle_batch)
            if db.is_implausible_outcome(outcome_pct):
                print(f"[bridge] implausible outcome for decision {r['id']} "
                      f"({r['asset']}): {outcome_pct:+.2f}% → 拒写", flush=True)
                n_skipped += 1
                continue
            conn.execute(
                "UPDATE trade_decisions SET status='settled', "
                "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
                (outcome_pct, r["id"]),
            )
            direction = (r["intended_action"] or r["final_action"] or "").strip().upper()
            _insert_settlement_reflection(
                conn, decision_id=r["id"], asset=r["asset"],
                action=r["final_action"], entry_price=r["entry_price"],
                outcome_pct=outcome_pct, sub_layer="auto_settled_5d",
                guard=r["guard"], decision_type=r["decision_type"],
                hit=_hit_for(direction, outcome_pct), intended_action=direction)
            n_settled += 1
            # v311: 分段提交 (同 _run_decision_settle_batch) —— 不让一个长事务
            # 把写锁占到整批结束, 把 news pull / source_status 全挡在门外
            if n_settled % 25 == 0:
                conn.commit()
    return {"ok": True, "n_total": n_total, "n_settled": n_settled, "n_skipped": n_skipped}


# v358 P0: ohlcv/news/cot/movers 四个重活线程默认关闭 —— 它们和 8000 侧重复,
#   且都在同进程里抢 GIL + 撞写锁, 实测把 bridge 的事件循环冻住 30s+（连 /api/health
#   都超时）, 页面上表现为"整页转圈 / 全 —"。需要时设 TRIDENT_BRIDGE_AUX_JOBS=1 开回。
# 启动时启动后台线程
scan_thread = threading.Thread(target=realtime_scan_loop, daemon=True, name="bridge-scan")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    scan_thread.start()
ohlcv_thread = threading.Thread(target=ohlcv_refresh_loop, daemon=True, name="bridge-ohlcv-refresh")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    ohlcv_thread.start()
settle_thread = threading.Thread(target=decision_settle_loop, daemon=True, name="bridge-decision-settle")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    settle_thread.start()
causal_eval_thread = threading.Thread(target=causal_chain_eval_loop, daemon=True, name="bridge-causal-eval")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    causal_eval_thread.start()


# ─── News API endpoints + worker (P0-3) ───────────────
NEWS_INSERT_CHUNK = 20          # v311: 每 20 条提交一次 (把写锁窗口压到最小)
NEWS_DEADLETTER = Path(os.path.expanduser("~/.trident/log/news_deadletter.jsonl"))
NEWS_DEADLETTER_MAX = 2000      # 死信上限, 防无限膨胀


def _insert_news_batch(all_news: list) -> tuple:
    """把一批新闻写入 news_events（INSERT OR IGNORE）。返回 (成功条数, 失败条目)。

    v311 (新闻丢失修复):
      · **分块提交** —— 原来整批一个事务, 撞写锁时整批回滚, 一轮新闻全丢
        (日志实证: "news insert error (batch): database is locked");
        现在每 20 条提交一次, 撞锁最多丢当前这 20 条, 前面的块已经落库。
      · **块内逐条降级** —— 块失败时退化为逐条 INSERT, 只丢真正写不进去的那几条。
      · **失败条目外抛给调用方** —— 落死信文件, 下一轮重放, 不再"丢了就没了"。

    锁错误（locked/busy）仍然上抛 —— 由调用方整批重试；
    其它错误只记录并跳过这一条，不影响同批其它新闻。
    """
    if not all_news:
        return 0, []
    n = 0
    failed: list = []
    with write_db_cursor() as conn:
        # v310: 补 fetched_at（这列被补回来后一直没人写，新行恒为 0）
        has_fetched = _news_events_has_col(conn, "fetched_at")
        if has_fetched:
            sql = ("INSERT OR IGNORE INTO news_events "
                   "(source, headline, body, action, score, assets, ts, news_id, fetched_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)")
        else:
            sql = ("INSERT OR IGNORE INTO news_events "
                   "(source, headline, body, action, score, assets, ts, news_id) "
                   "VALUES (?,?,?,?,?,?,?,?)")

        def _params(ev):
            # v314: action 枚举归一 —— 上游给 None / 'bullish' / 'E020' 之类
            # 会落成 NULL 或非法枚举 (验收 #13 要求 NULL=0, C8 要求非法=0)。
            base = (ev["source"], ev["headline"], ev["body"],
                    normalize_news_action(ev.get("action")),
                    ev["score"], ev["assets"], ev["ts"], ev["news_id"])
            return base + (ev["ts"],) if has_fetched else base

        for start in range(0, len(all_news), NEWS_INSERT_CHUNK):
            chunk = all_news[start:start + NEWS_INSERT_CHUNK]
            try:
                for ev in chunk:
                    cur = conn.execute(sql, _params(ev))
                    if cur.rowcount and cur.rowcount > 0:
                        n += 1
                conn.commit()   # 分块提交: 写锁只被占 20 条的时间
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" in msg or "busy" in msg:
                    raise  # 撞锁 → 整批重试 (调用方退避后重试)
                # 非锁类错误 → 逐条降级, 只丢真正写不进去的那几条
                for ev in chunk:
                    try:
                        cur = conn.execute(sql, _params(ev))
                        if cur.rowcount and cur.rowcount > 0:
                            n += 1
                    except Exception as e2:
                        print(f"[bridge] news insert error ({ev.get('source')}): {e2}", file=sys.stderr)
                        failed.append(ev)
                try:
                    conn.commit()
                except Exception:
                    pass
    return n, failed


def _insert_news_batch_with_retry(items: list) -> tuple:
    """整批重试 (撞锁退避 0.5/1/2s, 最多 3 次)。彻底失败 → (0, items) 交死信。"""
    delay = 0.5
    for attempt in range(4):
        try:
            return _insert_news_batch(items)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < 3:
                print(f"[bridge] news batch locked, retry {attempt + 1}/3", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
                continue
            print(f"[bridge] news insert error (batch): {e}", file=sys.stderr)
            return 0, list(items)
    return 0, list(items)


def _append_deadletter(items: list) -> None:
    """没写进去的新闻落死信文件 (下一轮重放, 不再永久丢失)。"""
    if not items:
        return
    try:
        NEWS_DEADLETTER.parent.mkdir(parents=True, exist_ok=True)
        with open(NEWS_DEADLETTER, "a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[bridge] deadletter append failed: {e}", file=sys.stderr)


def _read_deadletter() -> list:
    if not NEWS_DEADLETTER.exists():
        return []
    out: list = []
    try:
        with open(NEWS_DEADLETTER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"[bridge] deadletter read failed: {e}", file=sys.stderr)
    return out[-NEWS_DEADLETTER_MAX:]


def _rewrite_deadletter(keep: list) -> None:
    """重写死信文件, 只保留仍写不进去的条目。"""
    try:
        NEWS_DEADLETTER.parent.mkdir(parents=True, exist_ok=True)
        tmp = NEWS_DEADLETTER.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for it in keep[-NEWS_DEADLETTER_MAX:]:
                f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
        tmp.replace(NEWS_DEADLETTER)
    except Exception as e:
        print(f"[bridge] deadletter rewrite failed: {e}", file=sys.stderr)


def _flush_deadletter() -> int:
    """重放死信 (上一轮因撞锁没入库的新闻)。写进去的从死信里移除。"""
    items = _read_deadletter()
    if not items:
        return 0
    n_ok, failed = _insert_news_batch_with_retry(items)
    _rewrite_deadletter(failed)
    if n_ok:
        print(f"[bridge] deadletter replayed {n_ok}/{len(items)}", flush=True)
    return n_ok


def _news_pull_sync() -> int:
    """跑一次双源拉取, 写入 news_events (INSERT OR IGNORE)

    v310: 撞写锁时**整批重试**（退避 0.5/1/2s，最多 3 次）。
    原来撞锁只 print 一句就跳过 —— 那条新闻直接丢了
    （日志里累计 3000+ 条 "news insert error ... database is locked"）。

    v311: 重试仍失败 → 落**死信文件**, 下一轮先重放 —— 绝不"丢了就没了"。
    """
    _flush_deadletter()          # 先补上一轮的漏
    techflow = _pull_techflow()
    eastmoney = _pull_eastmoney()
    all_news = techflow + eastmoney
    if not all_news:
        return 0
    n, failed = _insert_news_batch_with_retry(all_news)
    if failed:
        _append_deadletter(failed)
        print(f"[bridge] news pull: {len(failed)} 条进死信待重放", file=sys.stderr)
    return n


def news_pull_loop() -> None:
    """P0-3 worker: 每 5min 拉 TECHFLOW + EASTMONEY 写入 news_events"""
    while True:
        try:
            n = _news_pull_sync()
            if n:
                print(f"[bridge] news_pull_loop: +{n} new", flush=True)
        except Exception as e:
            print(f"[bridge] news_pull_loop error: {e}", file=sys.stderr)
        time.sleep(NEWS_POLL_SECONDS)


@app.get("/api/crypto/lsr/latest")
def crypto_lsr_latest(hours: Annotated[int, Query(ge=1, le=168)] = 6):
    """真实多空持仓比（Binance 全市场账户 / Bybit 账户买卖比）。

    数据来源已从 coinglass(付费 key, 从未成功) 换成本机可达的免费源,
    表 lsr_samples 由 lsr_feed.py 每小时写入; 取不到就是空, 前端按缺失显示。
    """
    import time as _t
    since = int(_t.time()) - hours * 3600
    out = {"ok": True, "hours": hours, "items": [], "series": {}}
    with db_cursor() as conn:
        try:
            rows = conn.execute(
                "SELECT source, symbol, ts, long_ratio, short_ratio, ls_ratio "
                "FROM lsr_samples WHERE ts >= ? ORDER BY symbol, source, ts",
                (since,)).fetchall()
        except Exception:
            rows = []
    latest: dict = {}
    for src, sym, ts, lr, sr, ls in rows:
        key = f"{src}:{sym}"
        out["series"].setdefault(key, []).append(
            {"ts": int(ts), "long": lr, "short": sr, "ls": ls})
        prev = latest.get((sym, src))
        if not prev or ts > prev["ts"]:
            latest[(sym, src)] = {"symbol": sym, "source": src, "ts": int(ts),
                                  "long_ratio": lr, "short_ratio": sr, "ls_ratio": ls}
    out["items"] = sorted(latest.values(), key=lambda x: (x["symbol"], x["source"]))
    return out


@app.get("/api/news/recent")
def news_recent(hours: Annotated[int, Query(ge=1, le=720)] = 24,
                limit: Annotated[int, Query(ge=1, le=500)] = 50,
                source: str | None = None, asset: str | None = None):
    """P0-3: 最近 N 小时新闻, 可按 source/asset 过滤

    v311 改造(对齐 /api/events)——修掉新闻流里「只有标签没有内容」的老问题:
      1) JOIN raw_news      → 真实 source / 正文(content) / 发布时间 / 全网首见 / 首发源 / 闸门判定 / 相关性 / 原文 URL
      2) JOIN ai_decisions  → 推理链 / 决策上下文 / 不下单原因 / 闸门原因 / 预期周期 / 影响强度
      3) JOIN 最新 quick_sim_trades → 成交价 / 结算结果 / 盈亏 / 持有周期
      4) 三个老 bug: source 空串(48% 行) / headline 带 [hash:xxx] 污染 / 正文缺失(仅 21% 行有)
      5) decision_id 为空时回退用 news_id 关联 ai_decisions, 不让分析信息凭空消失
    """
    cutoff = int(time.time()) - hours * 3600
    inner = ("SELECT id, news_id, decision_id, source, headline, body, action, score, "
             "assets, asset, ts, fetched_at FROM news_events WHERE ts>=? ")
    params: list = [cutoff]
    if source:
        inner += "AND source=? "
        params.append(source)
    if asset:
        # v310: 权威品种在单数 asset 列, 复数 assets 只是历史列 —— 两个都匹配
        inner += "AND (COALESCE(assets,'') LIKE ? OR COALESCE(asset,'') LIKE ?) "
        params.extend([f"%{asset}%", f"%{asset}%"])
    inner += "ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    sql = (
        "SELECT ne.*, "
        "rn.source AS raw_source, rn.content AS raw_content, rn.published_at, rn.first_seen_ts, "
        "rn.net_origin_info, rn.cross_net_source, rn.cross_net_first_seen_ts, "
        "rn.content_fingerprint, rn.gate_verdict, rn.gate_core_impact, rn.relevance_score, "
        "rn.url AS raw_url, "
        "ad.id AS ad_id, ad.reasoning_path, ad.reasoning AS ad_reasoning, ad.decision_context, "
        "ad.no_trade_reason, ad.trade_gate_reason, ad.expected_horizon, ad.target_asset, "
        "ad.event_strength, ad.event_phase, ad.sentiment_score, ad.evidence_confidence, "
        "ad.invalidation_condition, ad.market_category, ad.created_at AS decision_created_at, "
        "qs.entry_price, qs.exit_price, qs.settled, qs.verdict AS trade_verdict, "
        "qs.pnl_pct, qs.exit_ts, qs.horizon_minutes, qs.asset AS trade_asset, "
        "qs.entry_ts AS trade_entry_ts "
        f"FROM ({inner}) ne "
        "LEFT JOIN raw_news rn ON rn.id = ne.news_id "
        # v317 fix：主决策口径与 api_server 的 _EVENT_SELECT 对齐 —— 恒取该新闻
        #   **最新的一条主决策**（is_sibling=0 / 遗留 NULL）。
        #   旧写法优先 ne.decision_id（实测 2293 行指向**副品种**决策），空值时又
        #   回退到"该新闻的最新决策"（同样是副品种）→ 实测 3515 行把副品种决策
        #   当成了主事件展示（reasoning/action/评分全是另一个品种的）。
        #   副品种决策仍可通过 sibling_decisions 单独取用，不影响多品种展示。
        # ⚠ SQLite 坑(v311 实测): JOIN 的 ON 子查询里, ORDER BY 表达式**不能**引用外层别名 ——
        #   而 WHERE 里引用外层别名是合法的（下面的 x.news_id 就是）。
        "LEFT JOIN ai_decisions ad ON ad.id = ("
        "SELECT x.id FROM ai_decisions x WHERE x.news_id = ne.news_id "
        "AND (x.is_sibling = 0 OR x.is_sibling IS NULL) "
        "ORDER BY x.id DESC LIMIT 1) "
        "LEFT JOIN quick_sim_trades qs ON qs.id = ("
        "SELECT q2.id FROM quick_sim_trades q2 WHERE q2.decision_id = ad.id "
        "OR (ad.id IS NULL AND q2.news_id = ne.news_id) "
        "ORDER BY q2.id DESC LIMIT 1) "
        "ORDER BY ne.ts DESC"
    )
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        # 来源统计用 JOIN 后的真实 source(否则 48% 的空串来源让筛选器全是空按钮)
        by_source = conn.execute(
            "SELECT COALESCE(NULLIF(rn.source,''), ne.source, '') AS src, COUNT(*) "
            "FROM news_events ne LEFT JOIN raw_news rn ON rn.id = ne.news_id "
            "WHERE ne.ts>=? GROUP BY src", (cutoff,)
        ).fetchall()
    items = [_news_row_to_recent(r) for r in rows]
    # v362: 同一事件常被多源转述（金十 / cointelegraph / fseishu 同一条），
    #   归一化标题后去重，只留最新一条；不同来源仍在 by_source 统计里可见。
    # v363: 顺带清掉广告/推广类内容（无品种、带喊单/空投话术），它们不是行情新闻
    _SPAM = ("unleash", "giveaway", "airdrop", "claim now", "presale", "join now",
             "私信", "加群", "空投", "领取", "注册送")
    items = [it for it in items
             if any(k in (str(it.get("headline") or "") + str(it.get("body") or "")).lower()
                    for k in _SPAM) is False]
    seen: dict = {}
    deduped: list = []
    for it in items:
        _h = re.sub(r"[\[【(（][^\]】)）]*[\]】)）]", "", str(it.get("headline") or ""))
        key = re.sub(r"\W+", "", _h)[:60].lower()
        if not key:
            deduped.append(it)
            continue
        if key in seen:
            seen[key]["dup_sources"] = list(set((seen[key].get("dup_sources") or []) + [it.get("source") or ""]))
            continue
        it["dup_sources"] = []
        seen[key] = it
        deduped.append(it)
    items = deduped
    return {
        "ok": True,
        "n": len(items),
        "by_source": {s: c for s, c in by_source},
        "news": items,
        "hours": hours,
    }



_URL_NOISE_RE = re.compile(r"\s*(?:原文链接[:：]?\s*)?https?://\S+")
_URL_TAIL_RE = re.compile(r"(?:[\s|｜]*(?:快讯|原文链接|原文)[:：]?)+[\s|｜]*$")


def _strip_url_noise(text: str) -> str:
    """v361: 新闻正文里常被塞进原始 URL（"…袭击。原文链接：https://…"），
    直接渲染出来非常乱。URL 已有独立的「查看原文」入口，正文里的一律剥掉。"""
    if not text:
        return text
    out = _URL_NOISE_RE.sub(" ", str(text))
    out = _URL_TAIL_RE.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()

def _news_row_to_recent(r: Any) -> dict:
    """news_events + raw_news + ai_decisions + quick_sim_trades → 前端新闻流契约。

    v311: 一次填齐「时间溯源 / 分析 / 交易跟踪」三组字段, 并修掉
    source 空串、headline 带 [hash:]、正文缺失三个老 bug。
    """
    d = dict(r)
    raw_content = str(d.get("raw_content") or "")
    headline = _strip_hash_prefix(d.get("headline")) or _strip_hash_prefix(raw_content)[:160]
    body = str(d.get("body") or "").strip() or _strip_hash_prefix(raw_content)
    headline = _strip_url_noise(headline)
    body = _strip_url_noise(body)
    if body == headline:
        body = ""
    src = str(d.get("raw_source") or "").strip() or str(d.get("source") or "").strip()
    score = float(d.get("score") or 0.0)
    sent = d.get("sentiment_score")
    if score == 0.0 and sent is not None:
        try:
            score = float(sent)
        except (TypeError, ValueError):
            pass
    # 分析摘要: 优先"为什么这么决策", 其次闸门/影响解读, 最后推理链
    # ⚠ 候选必须过 _meaningful_text —— 空值占位会渲染成字面量 "{}"(浏览器实测可见)。
    #
    # ⚠ decision_context **不在候选里**(v311 修正):
    #   decision_context 是 AI 决策时抓的**行情快照 JSON**(timestamp/epoch_ms/assets/价格),
    #   实测 6163 行里 4141 行是这种 JSON、2022 行是 "{}" —— **可用散文 0 行**。
    #   把它排在候选首位会让 analysis 恒为裸 JSON 字符串, 前端直接显示
    #   {"timestamp": "2026-10-30 14:27:42 CST", "epoch_ms": ...} 这种机器噪音。
    #   它仍作为独立字段 decision_context 原样返回(前端 signal-detail 单独展示行情快照)。
    #
    # 候选优先级(v311): AI 结论(ad_reasoning) → 闸门原因 → 影响解读 → 六段推理链。
    #   ad_reasoning 是 AI 对这条新闻的直接结论(100% 有值), 信息量最高;
    #   trade_gate_reason 说明"为什么没交易"(置信度不足等);
    #   gate_core_impact 是市场影响解读; reasoning_path 是最长的推理链, 放最后兜底。
    analysis = ""
    for cand in (d.get("ad_reasoning"),
                 d.get("no_trade_reason"),
                 d.get("trade_gate_reason"),
                 d.get("gate_core_impact"),
                 d.get("reasoning_path")):
        txt = _strip_model_tag(_meaningful_prose(cand))
        if txt:
            analysis = txt
            break
    return {
        "id": d.get("id"),
        "news_id": d.get("news_id"),
        # v311: 有效决策 ID —— news_events.decision_id 近半数是 NULL,
        # 回退到 JOIN 出来的 ad.id(按 news_id 关联的最新决策),
        # 否则前端拿不到 decision_id 就不渲染分析/溯源区。
        "decision_id": d.get("decision_id") or d.get("ad_id"),
        "source": src,
        "headline": headline,
        "body": body,
        "action": d.get("action"),
        "score": score,
        # v361: 没跑过 AI 的新闻在库里是 asset='NONE' / action='HOLD' / score=0，
        #   直接渲染成「HOLD · 0.0 · NONE」会被当成"已分析且中性"。这里归一成
        #   asset=null + unanalyzed=true，前端显式显示「未分析」。
        "asset": None if str(d.get("asset") or "").strip().upper() in ("", "NONE", "NULL") else d.get("asset"),
        "unanalyzed": (str(d.get("asset") or "").strip().upper() in ("", "NONE", "NULL")
                       and not d.get("ad_id")),
        "assets": (""
                   if str(d.get("assets") or d.get("asset") or "").strip().upper() in ("", "NONE", "NULL")
                   else (d.get("assets") or d.get("asset") or "")),
        "ts": d.get("ts"),
        "fetched_at": d.get("fetched_at"),
        # ── 时间溯源 ──
        "published_at": d.get("published_at") or "",
        "first_seen_ts": d.get("first_seen_ts") or "",
        "net_origin_info": d.get("net_origin_info") or "",
        "cross_net_source": d.get("cross_net_source") or "",
        "content_fingerprint": d.get("content_fingerprint") or "",
        "url": d.get("raw_url") or "",
        # ── 分析 ──
        "analysis": analysis,
        "gate_verdict": d.get("gate_verdict") or "",
        "gate_core_impact": _meaningful_prose(d.get("gate_core_impact")),
        "relevance_score": d.get("relevance_score"),
        "reasoning_path": _meaningful_prose(d.get("reasoning_path")),
        "decision_context": _meaningful_text(d.get("decision_context")),
        "no_trade_reason": _meaningful_text(d.get("no_trade_reason")),
        "trade_gate_reason": _meaningful_text(d.get("trade_gate_reason")),
        "expected_horizon": d.get("expected_horizon"),
        "target_asset": d.get("target_asset") or "",
        "event_strength": d.get("event_strength"),
        "event_phase": d.get("event_phase") or "",
        "sentiment_score": d.get("sentiment_score"),
        "evidence_confidence": d.get("evidence_confidence"),
        "market_category": d.get("market_category") or "",
        "decision_created_at": d.get("decision_created_at") or "",
        # ── 交易跟踪 ──
        "entry_price": d.get("entry_price"),
        "exit_price": d.get("exit_price"),
        "settled": d.get("settled"),
        "trade_verdict": d.get("trade_verdict"),
        "pnl_pct": d.get("pnl_pct"),
        "exit_ts": d.get("exit_ts"),
        "horizon_minutes": d.get("horizon_minutes"),
        "trade_asset": d.get("trade_asset") or "",
        "trade_entry_ts": d.get("trade_entry_ts"),
    }


@app.post("/api/news/pull-now")
async def news_pull_now(request: Request):
    """P0-3: 立即跑一次双源拉取"""
    try:
        await request.json()
    except Exception:
        pass
    n = await run_in_threadpool(_news_pull_sync)
    return {"ok": True, "inserted": n}


# ─── /api/news/impact-evaluate (P0-4 · 新闻影响力评估器) ────────
# 输入:news_id (GET) 或 headline+body+score+assets (POST, 用于外部实时新闻)
# 输出:direction / magnitude / confidence / primary_assets / reasoning_chain
#
# 设计原则 (对应你 #12 / #13 / #8 第一性原理):
#   1) 不杜撰因果链 — causal_chains 表在 chat workspace 不存在,causal_chains_used=[] 诚实返回
#   2) 多源验证:5 因子 — news_score + critical_moves 同向 + COT spec_index 反向警示
#      + fed_rate + trade_reflections_v2 历史胜率
#   3) 第一性原理追问链:每条结论附带 step-by-step reasoning,方便人工校验
#   4) 数据不足时 honest_notes 诚实返回,confidence 上限被截断





# 关键词词典(本数据 118 条全是中文财经,中英混合确保覆盖)
# 常见资产 ticker 词典(从文本提取受影响资产)

# 资产同义词 (中文名/英文名) —— 中文财经新闻里基本不出现字面 ticker,
# 不做同义词扫描就答不出"这条新闻利好哪个标的" (G06)。






@app.get("/api/news/impact-evaluate")
def news_impact_evaluate_get(
    news_id: Annotated[int, Query(ge=1)],
    days_window: Annotated[int, Query(ge=1, le=30)] = 3,
) -> dict:
    """P0-4: GET 形式 — 给已有 news_id 的新闻做影响力评估"""
    return _news_impact_evaluate(news_id=news_id, days_window=days_window)


@app.post("/api/news/impact-evaluate")
async def news_impact_evaluate_post(request: Request) -> dict:
    """P0-4: POST 形式 — 接收外部传入的新闻字段(实时新闻尚未入库)"""
    body = await request.json()
    # P1-A: 评估内部走 LLM/DB，可达十几秒；async 端点内同步直调会占住事件循环，
    # 挪到线程池隔离，避免单条慢评估堵死其它并发请求。
    return await run_in_threadpool(
        _news_impact_evaluate,
        headline=body.get("headline"),
        news_body=body.get("body"),
        declared_assets=body.get("assets") or "",
        declared_score=body.get("score"),
        declared_source=body.get("source") or "external",
        days_window=int(body.get("days_window") or 3),
    )




news_thread = threading.Thread(target=news_pull_loop, daemon=True, name="bridge-news-pull")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    news_thread.start()


# ─── CFTC COT (Commitments of Traders) ───────────────
# 周报(周五发布, 数据为周二收盘后状态)
# 数据源: publicreporting.cftc.gov Socrata Open Data API (无 key, 免费)
# 核心信号: noncomm_net (机构净持仓) — 与价格反向时是逼空/杀多领先指标
CFT_CONTRACTS: list[tuple[str, str, str, str]] = [
    # (asset, contract_code, contract_name_fallback, group)
    # 注: 代码从 CFTC Socrata 实测, 用 contract_market_name 反查得到.
    # 早期版本有 3 个猜错 (COPPER 232741 实为 AUD, ZN 090741 实为 CAD, DXY 099741 实为 EUR).
    ("BTC",   "133741", "BITCOIN",                    "FINANCIAL INSTRUMENTS"),
    ("ETH",   "146021", "ETHER CASH SETTLED",         "FINANCIAL INSTRUMENTS"),
    ("ES",    "13874A", "E-MINI S&P 500",             "FINANCIAL INSTRUMENTS"),
    ("NQ",    "209742", "NASDAQ MINI",                "FINANCIAL INSTRUMENTS"),
    ("GOLD",  "088691", "GOLD",                       "METALS"),
    ("OIL",   "067411", "CRUDE OIL LIGHT SWEET-WTI",  "ENERGY"),
    ("SILVER","084691", "SILVER",                     "METALS"),
    ("NG",    "023651", "NAT GAS NYME",               "ENERGY"),
    ("EUR",   "099741", "EURO FX",                    "CURRENCIES"),
    ("AUD",   "232741", "AUSTRALIAN DOLLAR",          "CURRENCIES"),
    ("CAD",   "090741", "CANADIAN DOLLAR",            "CURRENCIES"),
    ("CORN",  "002602", "CORN",                       "AGRICULTURE"),
    ("WHEAT", "001602", "WHEAT-SRW",                  "AGRICULTURE"),
]
CFT_SOCRATA = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFT_POLL_SECONDS = 6 * 3600  # 6h — CFTC 周五 15:30 ET 发新数据
CFT_FETCH_TIMEOUT = 30

import urllib.request as _urllib_req
import ssl as _ssl

_ctx = _ssl.create_default_context()


def _cft_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _cft_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _cft_fetch_contract(code: str, weeks: int = 156) -> list:
    """从 CFTC Socrata 拉单个 contract 的 N 周数据 (rows desc)"""
    q = _urllib_req.quote(
        f"cftc_contract_market_code='{code}'",
        safe="'=",
    )
    url = (
        f"{CFT_SOCRATA}?%24limit={weeks}"
        f"&%24where={q}"
        f"&%24order=report_date_as_yyyy_mm_dd+DESC"
    )
    last_err = None
    for attempt in range(3):
        try:
            req = _urllib_req.Request(url, headers={
                "User-Agent": "Trident/1.0 (+bridge; cot)",
                "Accept": "application/json",
            })
            with _urllib_req.urlopen(req, timeout=CFT_FETCH_TIMEOUT, context=_ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"[bridge] cftc fetch {code} failed: {last_err}", file=sys.stderr)
    return []


def _cft_row_to_dict(row: dict, asset: str) -> dict | None:
    """CFTC row → DB row. 数值字段缺失则 None"""
    code = row.get("cftc_contract_market_code")
    name = row.get("contract_market_name") or row.get("commodity_name")
    if not code or not name:
        return None
    rd = (row.get("report_date_as_yyyy_mm_dd") or "")[:10]
    if len(rd) != 10:
        return None

    comm_long = _cft_int(row.get("comm_positions_long_all"))
    comm_short = _cft_int(row.get("comm_positions_short_all"))
    noncomm_long = _cft_int(row.get("noncomm_positions_long_all"))
    noncomm_short = _cft_int(row.get("noncomm_positions_short_all"))
    nonrept_long = _cft_int(row.get("nonrept_positions_long_all"))
    nonrept_short = _cft_int(row.get("nonrept_positions_short_all"))
    oi = _cft_int(row.get("open_interest_all"))

    comm_net = (comm_long - comm_short) if (comm_long is not None and comm_short is not None) else None
    noncomm_net = (noncomm_long - noncomm_short) if (noncomm_long is not None and noncomm_short is not None) else None

    return {
        "report_date": rd,
        "cftc_contract_code": str(code).strip(),
        "cftc_contract_name": str(name).strip(),
        "cftc_market_code": (row.get("cftc_market_code") or "").strip() or None,
        "commodity_group": (row.get("commodity_group_name") or "").strip() or None,
        "asset": asset,
        "open_interest": oi,
        "change_oi": _cft_int(row.get("change_in_open_interest_all")),
        "comm_long": comm_long, "comm_short": comm_short, "comm_net": comm_net,
        "noncomm_long": noncomm_long, "noncomm_short": noncomm_short,
        "noncomm_spread": _cft_int(row.get("noncomm_postions_spread_all")),
        "noncomm_net": noncomm_net,
        "nonrept_long": nonrept_long, "nonrept_short": nonrept_short,
        "change_comm_long": _cft_int(row.get("change_in_comm_long_all")),
        "change_comm_short": _cft_int(row.get("change_in_comm_short_all")),
        "change_noncomm_long": _cft_int(row.get("change_in_noncomm_long_all")),
        "change_noncomm_short": _cft_int(row.get("change_in_noncomm_short_all")),
        "change_nonrept_long": _cft_int(row.get("change_in_nonrept_long_all")),
        "change_nonrept_short": _cft_int(row.get("change_in_nonrept_short_all")),
        "pct_oi_comm_long": _cft_float(row.get("pct_of_oi_comm_long_all")),
        "pct_oi_comm_short": _cft_float(row.get("pct_of_oi_comm_short_all")),
        "pct_oi_noncomm_long": _cft_float(row.get("pct_of_oi_noncomm_long_all")),
        "pct_oi_noncomm_short": _cft_float(row.get("pct_of_oi_noncomm_short_all")),
        "traders_total": _cft_int(row.get("traders_tot_all")),
        "traders_noncomm_long": _cft_int(row.get("traders_noncomm_long_all")),
        "traders_noncomm_short": _cft_int(row.get("traders_noncomm_short_all")),
        "spec_index": None,
    }


def _cot_pull_sync() -> dict:
    """拉所有 CFT_CONTRACTS 写入 cftc_cot_data (INSERT OR IGNORE)
    增量 + compute spec_index + emit news (供 /news 展示) — 即使 fetch 失败, 后两步对已有 DB 数据也会执行
    """
    n_inserted = 0
    by_asset: dict[str, int] = {}
    errors: dict[str, str] = {}

    for asset, code, name_fallback, group in CFT_CONTRACTS:
        try:
            rows = _cft_fetch_contract(code, weeks=156)
            if not rows:
                errors[asset] = "fetch empty"
            else:
                n = 0
                with write_db_cursor() as conn:
                    for r in rows:
                        d = _cft_row_to_dict(r, asset)
                        if not d:
                            continue
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO cftc_cot_data "
                            "(report_date, cftc_contract_code, cftc_contract_name, cftc_market_code, "
                            " commodity_group, asset, open_interest, change_oi, "
                            " comm_long, comm_short, comm_net, "
                            " noncomm_long, noncomm_short, noncomm_spread, noncomm_net, "
                            " nonrept_long, nonrept_short, "
                            " change_comm_long, change_comm_short, change_noncomm_long, change_noncomm_short, "
                            " change_nonrept_long, change_nonrept_short, "
                            " pct_oi_comm_long, pct_oi_comm_short, pct_oi_noncomm_long, pct_oi_noncomm_short, "
                            " traders_total, traders_noncomm_long, traders_noncomm_short, spec_index) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (d["report_date"], d["cftc_contract_code"], d["cftc_contract_name"], d["cftc_market_code"],
                             d["commodity_group"], d["asset"], d["open_interest"], d["change_oi"],
                             d["comm_long"], d["comm_short"], d["comm_net"],
                             d["noncomm_long"], d["noncomm_short"], d["noncomm_spread"], d["noncomm_net"],
                             d["nonrept_long"], d["nonrept_short"],
                             d["change_comm_long"], d["change_comm_short"], d["change_noncomm_long"], d["change_noncomm_short"],
                             d["change_nonrept_long"], d["change_nonrept_short"],
                             d["pct_oi_comm_long"], d["pct_oi_comm_short"], d["pct_oi_noncomm_long"], d["pct_oi_noncomm_short"],
                             d["traders_total"], d["traders_noncomm_long"], d["traders_noncomm_short"], d["spec_index"])
                        )
                        if cur.rowcount and cur.rowcount > 0:
                            n += 1
                n_inserted += n
                by_asset[asset] = n
        except Exception as e:
            errors[asset] = f"{type(e).__name__}: {str(e)[:80]}"
            print(f"[bridge] cot_pull {asset} error: {e}", file=sys.stderr)
        time.sleep(0.6)

    # 全部 asset 跑完后, 统一对所有 (含 fetch 失败的) 做 spec_index 更新 + 极端事件发射
    news_emitted: dict[str, int] = {}
    for asset, *_ in CFT_CONTRACTS:
        try:
            _cot_compute_spec_index(asset)
            n = _cot_emit_news_events_for_asset(asset)
            if n:
                news_emitted[asset] = n
        except Exception as e:
            print(f"[bridge] cot emit news {asset}: {e}", file=sys.stderr)
    if news_emitted:
        print(f"[bridge] cot emit news: {news_emitted}", flush=True)

    return {
        "ok": True,
        "n_inserted": n_inserted,
        "by_asset": by_asset,
        "news_emitted": news_emitted,
        "errors": errors,
    }


def cot_pull_loop() -> None:
    """P0-3 续: 每 CFT_POLL_SECONDS 拉一次 COT 增量"""
    while True:
        try:
            r = _cot_pull_sync()
            if r.get("n_inserted"):
                print(f"[bridge] cot_pull_loop: +{r['n_inserted']} rows ({r.get('by_asset')})", flush=True)
            elif r.get("errors"):
                print(f"[bridge] cot_pull_loop: errors={r['errors']}", flush=True)
        except Exception as e:
            print(f"[bridge] cot_pull_loop error: {e}", file=sys.stderr)
        time.sleep(CFT_POLL_SECONDS)


def _cot_compute_spec_index(asset: str, weeks: int = 52) -> int:
    """更新 cftc_cot_data.spec_index: noncomm_net 在近 N 周的 z-score (52w window)"""
    with write_db_cursor() as conn:
        rows = conn.execute(
            "SELECT report_date, noncomm_net FROM cftc_cot_data "
            "WHERE asset=? AND noncomm_net IS NOT NULL "
            "ORDER BY report_date DESC LIMIT ?",
            (asset, weeks + 1),
        ).fetchall()
        if len(rows) < 8:
            return 0
        nets = [r["noncomm_net"] for r in rows[1:weeks+1]]
        if not nets:
            return 0
        mean = sum(nets) / len(nets)
        var = sum((x - mean) ** 2 for x in nets) / len(nets)
        std = var ** 0.5
        latest = rows[0]
        if std <= 0:
            return 0
        z = (latest["noncomm_net"] - mean) / std
        z = max(-3.0, min(3.0, z))
        conn.execute(
            "UPDATE cftc_cot_data SET spec_index=? "
            "WHERE asset=? AND report_date=?",
            (z, asset, latest["report_date"]),
        )
        return 1


# COT 极端持仓阈值 (52w z-score 绝对值)


def _cot_emit_news_event(asset: str, report_date: str, spec_index: float,
                         noncomm_net: int, open_interest: int) -> int:
    """若 |z| >= 1.5, INSERT OR IGNORE 一条 cftc_cot 新闻事件

    反向指标语义 (contrarian):
      z > 0   机构净多极值 → 价格高位, 历史 8 周内反转下行概率↑ → action=bearish
      z < 0   机构净空极值 → 价格低位, 历史 8 周内反转上行概率↑ → action=bullish

    news_id 决定性: hash(asset+date) → 同 asset+date 重复 emit 自动去重
    """
    if spec_index is None or abs(spec_index) < COT_EXTREME_Z:
        return 0
    # 反向解读
    is_bullish = spec_index < 0
    direction_label = "看涨反转" if is_bullish else "看跌反转"
    tag = "极空" if is_bullish else "极多"
    arrow = "🟢" if is_bullish else "🔴"
    # v314: action 必须是 BUY/SELL/HOLD 枚举 —— 'bullish'/'bearish' 只是语义标签,
    # 直接落库会被验收 C8 判成非法枚举, 还会被 news_score_worker 当"非法行"
    # 重打分覆盖, 把 COT 的反向解读方向冲掉。极空→看涨反转=BUY, 极多→看跌反转=SELL。
    action = normalize_news_action("bullish" if is_bullish else "bearish")
    score = round(min(1.0, abs(spec_index) / 3.0), 3)
    # news_id 决定性 (md5, 不受 Python 进程 PYTHONHASHSEED 影响)
    nid = int(hashlib.md5(f"cftc_cot:{asset}:{report_date}".encode()).hexdigest(), 16) % 2_000_000_000
    headline = (f"{arrow} COT 机构{tag} z={spec_index:+.2f}: {asset} "
                f"净 {noncomm_net:+,} 张, OI {open_interest:,}")
    body = (
        f"CFTC 周报 ({report_date}) 显示 {asset} 投机者(noncommercial)净持仓 "
        f"{noncomm_net:+,} 张, 52w z-score={spec_index:+.2f}, 触发{tag}信号. "
        f"反向解读: |z|≥1.5 历史 8 周内价格反转概率>60% → {direction_label}机会. "
        f"持续监测下几周 COT 数据, 若 z 向均值回归即可确认反转."
    )
    # ts = 该报告周五 21:30 UTC (CFTC 实际发布时间)
    try:
        y, m, d = report_date.split("-")
        dt = datetime(int(y), int(m), int(d), 21, 30, 0)
        ts = int(dt.timestamp())
    except Exception:
        ts = int(time.time())
    with write_db_cursor() as conn:
        # v310: 补 fetched_at
        # v314: status='DONE' —— COT 事件的 action/score 是这里直接算好的 (已打分),
        # 若留 PENDING, news_score_worker 会把它当"待打分行"用关键词重打分,
        # 把反向解读的 action/score 覆盖掉。
        if _news_events_has_col(conn, "fetched_at"):
            cur = conn.execute(
                "INSERT OR IGNORE INTO news_events "
                "(source, headline, body, action, score, assets, ts, news_id, fetched_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,'DONE')",
                ("cftc_cot", headline, body, action, score, asset, ts, nid, ts),
            )
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO news_events "
                "(source, headline, body, action, score, assets, ts, news_id, status) "
                "VALUES (?,?,?,?,?,?,?,?,'DONE')",
                ("cftc_cot", headline, body, action, score, asset, ts, nid),
            )
        return cur.rowcount if cur.rowcount else 0


def _cot_emit_news_events_for_asset(asset: str) -> int:
    """拉取 asset 最新一行, 触发 (若极端) 新闻事件. 返回写入条数."""
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT report_date, spec_index, noncomm_net, open_interest "
            "FROM cftc_cot_data WHERE asset=? AND spec_index IS NOT NULL "
            "ORDER BY report_date DESC LIMIT 1",
            (asset,),
        ).fetchone()
    if not row:
        return 0
    return _cot_emit_news_event(
        asset, row["report_date"], row["spec_index"],
        row["noncomm_net"] or 0, row["open_interest"] or 0,
    )


@app.get("/api/cot/recent")
def cot_recent(asset: str | None = None, weeks: Annotated[int, Query(ge=1, le=260)] = 26,
                contract: str | None = None):
    """P0-3 续: 最近 N 周 COT 持仓数据"""
    with db_cursor() as conn:
        sql = (
            "SELECT report_date, cftc_contract_code, cftc_contract_name, asset, "
            "       open_interest, comm_long, comm_short, comm_net, "
            "       noncomm_long, noncomm_short, noncomm_spread, noncomm_net, "
            "       nonrept_long, nonrept_short, "
            "       change_oi, change_noncomm_long, change_noncomm_short, "
            "       pct_oi_noncomm_long, pct_oi_noncomm_short, "
            "       spec_index, traders_total, traders_noncomm_long, traders_noncomm_short "
            "FROM cftc_cot_data WHERE 1=1 "
        )
        params: list = []
        if contract:
            sql += "AND cftc_contract_code=? "
            params.append(contract)
        if asset:
            sql += "AND asset=? "
            params.append(asset)
        sql += "ORDER BY report_date DESC LIMIT ?"
        params.append(weeks * 4)
        rows = conn.execute(sql, params).fetchall()
        by_asset: dict[str, list] = {}
        for r in rows:
            d = dict(r)
            by_asset.setdefault(d["asset"] or "_", []).append(d)
        result_assets = []
        for a, lst in by_asset.items():
            lst = lst[:weeks]
            if not lst:
                continue
            latest = lst[0]
            spec_index = latest.get("spec_index")
            if spec_index is None:
                nets_52w = [x["noncomm_net"] for x in lst if x["noncomm_net"] is not None][:52]
                if len(nets_52w) >= 8:
                    hist = nets_52w[1:53] if len(nets_52w) > 1 else []
                    if hist:
                        mean = sum(hist) / len(hist)
                        std = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
                        if std > 0 and nets_52w:
                            z = (nets_52w[0] - mean) / std
                            spec_index = max(-3.0, min(3.0, z))
            latest["spec_index"] = spec_index
            result_assets.append({
                "asset": a,
                "contract_code": latest["cftc_contract_code"],
                "contract_name": latest["cftc_contract_name"],
                "n": len(lst),
                "latest": latest,
                "history": lst,
            })
        return {
            "ok": True,
            "n": sum(len(r["history"]) for r in result_assets),
            "assets": result_assets,
            "weeks": weeks,
        }


@app.post("/api/cot/pull-now")
async def cot_pull_now(request: Request):
    """P0-3 续: 立即拉一次 COT (同步, 慢). _cot_pull_sync 内部已 compute spec_index + emit news"""
    try:
        await request.json()
    except Exception:
        pass
    r = await run_in_threadpool(_cot_pull_sync)
    return r


@app.post("/api/cot/emit-now")
async def cot_emit_now(request: Request):
    """P0-3 续 v2: 仅基于 DB 已有数据重算 spec_index + 触发极端事件 (不拉数据, <1s)"""
    try:
        await request.json()
    except Exception:
        pass
    news_emitted: dict[str, int] = {}
    for asset, *_ in CFT_CONTRACTS:
        try:
            _cot_compute_spec_index(asset)
            n = _cot_emit_news_events_for_asset(asset)
            if n:
                news_emitted[asset] = n
        except Exception as e:
            print(f"[bridge] cot emit-now {asset}: {e}", file=sys.stderr)
    return {"ok": True, "news_emitted": news_emitted, "n_total": sum(news_emitted.values())}


@app.get("/api/cot/assets")
def cot_assets():
    """返回当前已配置的 COT 合约白名单 + DB 中已写入的最新一周数据点"""
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT asset, cftc_contract_code, cftc_contract_name, "
            "       MAX(report_date) as latest_date, COUNT(*) as n_rows "
            "FROM cftc_cot_data GROUP BY asset, cftc_contract_code ORDER BY asset"
        ).fetchall()
    return {
        "ok": True,
        "whitelist": [{"asset": a, "code": c, "name": n} for a, c, n, _ in CFT_CONTRACTS],
        "in_db": [dict(r) for r in rows],
    }


@app.get("/api/cot/heatmap")
def cot_heatmap(weeks: Annotated[int, Query(ge=1, le=156)] = 26):
    """P0-3 续 v2: 跨资产 spec_index 热力图数据
    返回每个 asset 每周的 spec_index (None 表示数据不足), 前端可画热力图."""
    weeks = min(weeks, 156)
    with db_cursor() as conn:
        # 取每个 asset 的最近 N 周
        rows = conn.execute(
            "SELECT asset, report_date, spec_index, noncomm_net "
            "FROM cftc_cot_data "
            "WHERE spec_index IS NOT NULL "
            "ORDER BY report_date DESC",
        ).fetchall()
    by_asset: dict[str, list] = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append({
            "date": r["report_date"],
            "z": round(r["spec_index"], 3) if r["spec_index"] is not None else None,
            "net": r["noncomm_net"],
        })
    # 每个 asset 只保留前 N 周
    for a in by_asset:
        by_asset[a] = by_asset[a][:weeks]
    # 按 |z| 当前值降序
    def _cur_z(asset):
        lst = by_asset.get(asset, [])
        if not lst or lst[0]["z"] is None: return 0
        return abs(lst[0]["z"])
    sorted_assets = sorted(by_asset.keys(), key=_cur_z, reverse=True)
    # 汇总统计
    extreme_count = sum(1 for a in sorted_assets if _cur_z(a) >= COT_MEGA_Z)
    warning_count = sum(1 for a in sorted_assets if COT_EXTREME_Z <= _cur_z(a) < COT_MEGA_Z)
    return {
        "ok": True,
        "weeks": weeks,
        "thresholds": {"extreme": COT_EXTREME_Z, "mega": COT_MEGA_Z},
        "summary": {
            "n_assets": len(sorted_assets),
            "n_extreme": extreme_count,
            "n_warning": warning_count,
        },
        "assets": [
            {
                "asset": a,
                "current_z": by_asset[a][0]["z"] if by_asset[a] else None,
                "current_net": by_asset[a][0]["net"] if by_asset[a] else None,
                "weeks": by_asset[a],
            } for a in sorted_assets
        ],
    }


cot_thread = threading.Thread(target=cot_pull_loop, daemon=True, name="bridge-cot-pull")
if os.getenv("TRIDENT_BRIDGE_AUX_JOBS","0") == "1":
    cot_thread.start()


# ─── v292 事件总线真实实现 (修复 catch-all 导致 data.filter TypeError) ─────
# 背景:8000 api_server.py 磁盘源已删; bridge_server 之前没有 /api/events
#       真实实现, 所有路径被 catch-all 兜底返 {items:[], _bridge_fallback:true}。
#       前端 event-bus.tsx 直接 await res.json() 当数组用 → TypeError。
# 修复: 这里从 news_events 表读取真实事件并转成前端 ApiEvent 形状,
#       保留 items/count 格式以兼容 catch-all 时的前端兜底判断。

import re as _re











# v311: AI 字段里的"空 JSON"占位 —— 序列化后是字面量 "{}" / "[]" / "null"。
# ai_decisions.decision_context 有 1970/6076 行是 "{}"(AI 判定"无上下文"时写空对象),
# 直接展示给用户就是屏幕上一串 "{}"(浏览器里实测可见)。
# 这里统一归一成空串, 让上层能正确回退到下一个候选来源。
#
# ⚠ 命名说明: 刻意**不叫** `_PLACEHOLDER_*` —— 项目诚实宪章 (_honesty.py) 把该前缀
#   保留给"硬编码假价格"过滤表(见 _PLACEHOLDER_PRICES), 任何以 `_PLACEHOLDER_` 开头的
#   常量都会被 tests/test_no_fabrication.py 判为捏造数据嫌疑并让测试失败。
#   本常量语义正相反: 它是"把这些空字面量识别出来并清空"的**白名单**。






# v311: ai_decisions.reasoning 开头带 "[模型名 (标识)]" 前缀(实测 396/400 行有),
#   形如 "[MiniMax-M3 (MiniMax#2)] 未见供应或航运中断..."。
#   这是内部路由信息, 展示给用户没有意义且挤占首行空间 → 展示前统一剥掉。
#   ⚠ 只剥**行首**的方括号标签, 不动正文里可能出现的方括号。










def _news_events_has_col(conn, col: str) -> bool:
    """检查 news_events 是否存在某列（老库降级用）。"""
    try:
        return col in {r[1] for r in conn.execute("PRAGMA table_info(news_events)")}
    except sqlite3.Error:
        return False






# ─── /api/events (v320: 委托 api_server 权威实现) ─────────────────────────
# v320 P0 修复：本文件原来自己实现了一套「news_events 基准」的事件列表，
# 与 8000 (api_server) 口径不一致，前端表现为三处错位：
#   ① news_events 按品种拆行 → 同一条新闻在列表里出现多次（重复行）；
#   ② news_events 保留了 raw_news 已被裁剪的孤儿行（raw_status 取不到）→
#      resolve_analysis_status(None, has_decision=False) → PENDING →
#      前端整片「待 AI 分析」徽章刷屏（实测 news_events 19564 行 vs
#      raw_news 非噪声 4474 行，差额全是孤儿/拆行）；
#   ③ 字段比 8000 少一截（heat_score / impact_score / asset_impact /
#      peer_reviews / asset_reasoning / sibling_decisions 全缺）→
#      前端「关注度」徽章与多模型复核块永远不显示。
# 现在改为委托 api_server 的权威实现（raw_news 基准、8000 同一份代码），
# 8100 与 8000 口径完全一致；委托不可用时才退回下面的旧口径查询。
def _events_legacy_sync(
    limit: int,
    hours: int | None,
    asset: str | None,
    source: str | None,
):
    """旧口径兜底（news_events 基准）—— 仅当 api_server 不可用时使用。"""
    # v311: 补齐新闻详细信息 —— news_events 只是 ai_worker 写的"决策摘要副本"，
    # 自身没有发布时间 / 全网溯源 / 闸门判定 / 价格跟踪，且 source 有 48% 是空串
    # （→ 前端渲染成 unknown）。这里 LEFT JOIN 新闻本体 + 决策 + 成交跟踪，
    # 让前端展开后拿到真实详情，而不是一律 "no detail available"。
    # ⚠ 不能改成 FROM raw_news：news_events 有 15511 行而 raw_news 只有 4229 行
    #    （历史条目已被裁剪），换表会丢掉 70% 的历史新闻。
    # ⚠ quick_sim_trades 一个 decision 可能有多笔 → 用子查询取最新一笔，避免行数膨胀
    #    把 LIMIT 截断成"更少的条目"。
    # v311 性能: 先用子查询按 ts 取 TOP-N（走 idx_ne_ts 索引）再 JOIN 补详情。
    #   直接 FROM news_events LEFT JOIN ... 会让 SQLite 放弃索引 → 全表扫 15511 行
    #   × 3 次 JOIN（limit=1000 实测 ~950ms）；先取 TOP-N 再 JOIN 只对 N 行做 JOIN。
    inner = (
        "SELECT id, news_id, source, headline, body, action, score, assets, asset, ts, "
        "fetched_at, status, is_noise, decision_id FROM news_events WHERE 1=1"
    )
    params: list = []
    if hours is not None:
        inner += " AND ts >= ?"
        params.append(int(time.time()) - hours * 3600)
    if asset:
        # v310: 单数 asset 列（权威）+ 复数 assets 列（历史）两个都匹配
        inner += " AND (COALESCE(assets,'') LIKE ? OR COALESCE(asset,'') LIKE ?)"
        params.extend([f"%{asset}%", f"%{asset}%"])
    if source:
        inner += " AND source = ?"
        params.append(source)
    inner += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    sql = (
        "SELECT ne.id, ne.news_id, ne.source, ne.headline, ne.body, ne.action, ne.score, "
        "ne.assets, ne.asset, ne.ts, ne.fetched_at, ne.status, ne.is_noise, ne.decision_id, "
        "rn.status AS raw_status, "
        "rn.source AS raw_source, rn.published_at, rn.first_seen_ts, rn.net_origin_info, "
        "rn.cross_net_source, rn.cross_net_first_seen_ts, rn.content_fingerprint, "
        "rn.gate_verdict, rn.gate_core_impact, rn.relevance_score, rn.url AS raw_url, "
        # v321 fix: 门卫冻结标记必须 SELECT 出来（旧兜底路径用 news_events 基准，
        # news_events 没有 gate_unavailable 列，只能从 raw_news JOIN 取）。
        # 没有它 → _rget(r, "gate_unavailable", 0) 恒为 0 → 冻结行误显示「待 AI 分析」。
        "rn.gate_unavailable, "
        "ad.id AS ad_id, ad.reasoning_path, ad.expected_horizon, ad.event_strength, "
        "ad.event_phase, ad.direct_catalyst, ad.market_confirmation, ad.attention_score, "
        "ad.mfe_pct, ad.mae_pct, ad.decision_context, ad.invalidation_condition, "
        "ad.no_trade_reason, ad.trade_gate_reason, ad.evidence_confidence, "
        "ad.prediction_type, ad.reasoning, ad.sentiment_score, ad.target_asset, "
        "ad.market_category, ad.created_at, ad.vip_tag, ad.partial_exit_price, "
        "ad.partial_exit_ratio, ad.partial_pnl_pct, ad.suggested_action, "
        "qs.entry_price, qs.exit_price, qs.max_price, qs.min_price, qs.max_price_time, "
        "qs.min_price_time, qs.settled, qs.verdict AS is_correct, qs.entry_ts "
        f"FROM ({inner}) ne "
        "LEFT JOIN raw_news rn ON rn.id = ne.news_id "
        # v317 fix：主决策口径与 api_server 的 _EVENT_SELECT 对齐（最新主决策，
        #   is_sibling=0 / 遗留 NULL），不再优先 ne.decision_id（实测 2293 行指向
        #   副品种决策）→ 详见 /api/news/recent 处的同一处修正说明。
        # ⚠ SQLite 坑: ON 子查询的 ORDER BY 不能引用外层别名(ne.*) → 必须按 id 倒序
        "LEFT JOIN ai_decisions ad ON ad.id = ("
        "SELECT x.id FROM ai_decisions x WHERE x.news_id = ne.news_id "
        "AND (x.is_sibling = 0 OR x.is_sibling IS NULL) "
        "ORDER BY x.id DESC LIMIT 1) "
        "LEFT JOIN quick_sim_trades qs ON qs.id = ("
        "SELECT q2.id FROM quick_sim_trades q2 WHERE q2.decision_id = ad.id "
        "OR (ad.id IS NULL AND q2.news_id = ne.news_id) "
        "ORDER BY q2.id DESC LIMIT 1) "
        "ORDER BY ne.ts DESC"
    )
    try:
        with db_cursor() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_news_row_to_api_event(r) for r in rows]
    except Exception as e:
        # 真实失败而非兜底 → 让前端能看到错误
        print(f"[api_events error] {e}", file=sys.stderr)
        return []


# v320: 8100 的 /api/events 直接复用 api_server 的权威行构造
# (_EVENT_SELECT + _row_to_event) —— 与 8000 同一份代码、同一套 id 空间。
# 注：不走「反查 api_server 的 /api/events 路由再转发」的方式 ——
# 磁盘 api_server.py 里并没有该路由（8000 跑的是内存中的旧模块），
# 反查会落空；复用行构造函数则无论 api_server 版本如何都成立。


def _api_server_module():
    """返回已挂载的 api_server 模块（未挂载时返回 None）。"""
    return globals().get("_api_server")


#: 权威事件窗口（与 api_server 的 15 天缓存窗口一致）
_EVENTS_WINDOW_S = 15 * 86400


def _events_authoritative_rows(
    news_ids: list | None = None,
    limit: int = 100,
    hours: int | None = None,
    asset: str | None = None,
    source: str | None = None,
) -> list:
    """按 api_server 的权威口径 (raw_news 基准) 取事件行。

    复用 api_server 的 _EVENT_SELECT + _row_to_event —— 与 8000 完全同源，
    保证 REST 与 SSE 两条链路的行形状、状态判定、字段集合一致。
    每行 id == news_id == raw_news.id（与 8000 一致，旧实现是 news_events.id，
    两套 id 空间会让前端按 id 合并时对不上）。

    api_server 不可用时返回空列表（调用方回退旧口径）。
    """
    api = _api_server_module()
    if api is None:
        return []
    if news_ids is not None and not news_ids:
        return []
    try:
        now = int(time.time())
        if hours:
            lo, hi = now - int(hours) * 3600, now
        else:
            lo, hi = now - _EVENTS_WINDOW_S, now + _EVENTS_WINDOW_S
        lo_str = time_utils.epoch_to_bj_str(lo)
        hi_str = time_utils.epoch_to_bj_str(hi)
        ts_expr = time_utils.sql_bj_ts_expr("rn.timestamp")
        where = [
            "rn.is_noise = 0",
            # 与 api_server._events_15d_load_sync 同口径：ad 为 NULL（还没分析）
            # 或主决策都保留，副品种决策行才排除。
            "(ad.is_sibling = 0 OR ad.is_sibling IS NULL)",
            f"{ts_expr} >= ?", f"{ts_expr} <= ?",
        ]
        params: list = [lo_str, hi_str]
        if source:
            where.append("LOWER(COALESCE(rn.source,'')) = LOWER(?)")
            params.append(str(source))
        if asset:
            # 只用 _EVENT_SELECT 里已确认存在的列（ad.target_asset / ad.market_category），
            # 避免引用 raw_news 上未必存在的列导致整条 SQL 报错。
            where.append(
                "(UPPER(COALESCE(ad.target_asset,'')) LIKE ? "
                "OR UPPER(COALESCE(ad.market_category,'')) LIKE ?)"
            )
            pat = f"%{str(asset).upper()}%"
            params.extend([pat, pat])
        if news_ids is not None:
            where.append(f"rn.id IN ({','.join('?' for _ in news_ids)})")
            params.extend(int(i) for i in news_ids)
        sql = (
            f"{api._EVENT_SELECT} WHERE {' AND '.join(where)} "
            "ORDER BY rn.id DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        with db_cursor() as conn:
            rows = conn.execute(sql, params).fetchall()
        events = [api._row_to_event(r) for r in rows]
        # 一条新闻一行：quick_sim_trades 一个 decision 可能有多笔 → JOIN 出重复行
        seen: set = set()
        out: list = []
        for ev in events:
            nid = int(ev.get("news_id") or 0)
            if nid in seen:
                continue
            seen.add(nid)
            ev.setdefault("sibling_decisions", [])
            out.append(ev)
        return out
    except Exception as exc:
        print(f"[bridge] 权威事件行构造失败: {exc}", file=sys.stderr)
        return []


@app.get("/api/events")
def api_events(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    hours: Annotated[int | None, Query(ge=1, le=8760)] = None,
    asset: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    flat: Annotated[int | None, Query(description="v293 兼容: =1 返裸数组 ApiEvent[]; 旧版前端 .filter() 需要")] = None,
):
    """前端 event-bus.tsx loadHistory 用:
    - 默认 {items:[...], count:N, ok:true} (兼容 catch-all 兜底)
    - flat=1 返裸 ApiEvent[] 数组 (兼容旧版前端 .filter() 误用)
    - 数据源 raw_news（8000 权威口径，一条新闻一行）
    """
    # v320: 权威口径 —— 复用 api_server 的 _EVENT_SELECT + _row_to_event，
    # 与 8000 同一份代码、同一套 id 空间（id == news_id == raw_news.id）。
    items = _events_authoritative_rows(
        limit=limit, hours=hours, asset=asset, source=source)
    used_authoritative = bool(items)
    if not items:
        items = _events_legacy_sync(limit, hours, asset, source)
    if flat:
        return items
    return {"ok": True, "items": items, "count": len(items),
            "data_source": "raw_news (8000 权威口径)" if used_authoritative
            else "news_events v292+ (兜底)",
            "note": f"真实读取 {len(items)} 条事件"}


# ─── /api/events/today (8000 与 8100 唯一真相源) ──────────────────────
# v320 P0 修复：本文件原来自己实现了一个**只有 2 个字段**的简化桩
#   {ok, news_count, total_count}，而且"今日"起点用
#   datetime.combine(date.today(), min, tzinfo=utc) —— 把**本地日期**和
#   **UTC 零点**混搭。机器本地时区是 UTC+8，于是 cutoff = 今天 08:00 北京
#   时间，比"现在"还晚 → 窗口内一条都没有 → 前端 stat-cards 显示
#   「窗口新闻 0 / 已分析 0」；analyzed_count / signal_count /
#   live_signal_count 也全部缺失（前端只能回退到列表长度估算）。
#
# 现在改为**委托 api_server 的权威实现**（8000 同一份代码、同一个 DB、
# 同一口径）；api_server 不可用时才退回本地等价口径（_events_today_fallback）。
_API_EVENTS_TODAY_ENDPOINT = None  # 文件末尾 import api_server 后填充


def _bj_day_start_epoch(epoch: int) -> int:
    """epoch → 北京时间当日 00:00 的 epoch 秒（"上海日"起点）。"""
    bj = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(int(epoch), bj)
    return int(datetime(dt.year, dt.month, dt.day, tzinfo=bj).timestamp())


def _events_today_fallback(
    hours: int | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict:
    """api_server 不可用时的本地兜底（口径与 8000 的 /api/events/today 对齐）。

    窗口：
      from_ts/to_ts 给了就用；只给 hours → now - hours*3600；
      都没给 → **北京时间当日 00:00** 起算（与前端"今日"口径一致）。

    口径（与 api_server._events_15d_load_sync 的 base_where 一致）：
      news_count      窗口内 raw_news 非噪声新闻条数
      analyzed_count  窗口内已有**主决策**(is_sibling=0/遗留 NULL)的新闻条数
      total_count     库内 raw_news 非噪声新闻总条数
      signal_count    窗口内 ai_decisions 的 BUY/SELL 决策**行数**
      live_signal_count  最近 4h 的 BUY/SELL 决策行数
      settled_today   窗口内已结算交易行数
    """
    now = int(time.time())
    if from_ts is not None:
        lo = int(from_ts)
    elif hours:
        lo = now - int(hours) * 3600
    else:
        lo = _bj_day_start_epoch(now)
    hi = int(to_ts) if to_ts is not None else now
    lo_str = time_utils.epoch_to_bj_str(lo)
    hi_str = time_utils.epoch_to_bj_str(hi)
    ts_expr = time_utils.sql_bj_ts_expr("rn.timestamp")
    out: dict[str, Any] = {
        "ok": True,
        "today": datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "window": {"from_ts": lo, "to_ts": int(to_ts) if to_ts is not None else None},
        "news_count": 0,
        "analyzed_count": 0,
        "total_count": 0,
        "signal_count": 0,
        "live_signal_count": 0,
        "live_signal_window_hours": 4,
        "settled_today": 0,
        "data_source": "raw_news (v320 fallback)",
    }
    with db_cursor() as conn:
        try:
            out["total_count"] = int(conn.execute(
                "SELECT COUNT(*) FROM raw_news WHERE is_noise = 0").fetchone()[0])
            out["news_count"] = int(conn.execute(
                f"SELECT COUNT(*) FROM raw_news rn WHERE rn.is_noise = 0 "
                f"AND {ts_expr} >= ? AND {ts_expr} <= ?",
                (lo_str, hi_str)).fetchone()[0])
            out["analyzed_count"] = int(conn.execute(
                f"SELECT COUNT(*) FROM raw_news rn WHERE rn.is_noise = 0 "
                f"AND {ts_expr} >= ? AND {ts_expr} <= ? "
                "AND EXISTS (SELECT 1 FROM ai_decisions ad WHERE ad.news_id = rn.id "
                "AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL))",
                (lo_str, hi_str)).fetchone()[0])
        except sqlite3.Error:
            pass
        try:
            # 信号按**决策行**计（一条新闻可拆主品种+副品种多行）；窗口用决策
            # 创建时间(北京墙钟)判定 —— ai_decisions.ts 历史上有 NULL 漏写，
            # 直接用 ts 会整段漏计。
            ad_ts_expr = time_utils.sql_bj_ts_expr("ad.created_at")
            out["signal_count"] = int(conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions ad "
                f"WHERE UPPER(COALESCE(ad.suggested_action,'')) IN ('BUY','SELL') "
                f"AND {ad_ts_expr} >= ? AND {ad_ts_expr} <= ?",
                (lo_str, hi_str)).fetchone()[0])
            live_lo = time_utils.epoch_to_bj_str(now - 4 * 3600)
            out["live_signal_count"] = int(conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions ad "
                f"WHERE UPPER(COALESCE(ad.suggested_action,'')) IN ('BUY','SELL') "
                f"AND {ad_ts_expr} >= ? AND {ad_ts_expr} <= ?",
                (live_lo, hi_str)).fetchone()[0])
        except sqlite3.Error:
            pass
        try:
            out["settled_today"] = int(conn.execute(
                "SELECT COUNT(*) FROM quick_sim_trades WHERE settled = 1 "
                "AND exit_ts >= ? AND exit_ts <= ?", (lo, hi)).fetchone()[0])
        except sqlite3.Error:
            pass
    return out


def _events_stream_legacy_generator(_json):
    """旧口径兜底流（news_events 基准）—— api_server 不可用时才用。

    v320 P0 修复：原来这里推的是 news_events 的行 ——
      · news_events 按品种拆行 → 同一条新闻被推多次（前端列表重复行）；
      · news_events 含 raw_news 已被裁剪的孤儿行 → 无 raw_status/无决策 →
        恒判 PENDING → 「待 AI 分析」徽章刷屏；
      · 与 /api/events(REST) 不同源 → REST 与 SSE 的行对不上。
    """
    # v310: 连接时回补多少条 —— 覆盖「前端 REST 拉历史」与「SSE 建立」之间的竞态窗口
    SSE_BACKFILL = 50
    if True:
        # v310 fix: 起点**不能**是 0。
        #   旧实现 `WHERE id > 0 ORDER BY id ASC LIMIT 20` + last_id=0 → 新客户端一连上收到的
        #   是**全表最老的 20 条**, 之后每 5s 再推 20 条更老的; news_events 有 12 万+ 行,
        #   要 ~8.5 小时才追到"现在"。那些老行 headline/body 多为空 →
        #   前端列表顶部全是"只有标签、没有正文"的垃圾事件, 把真正的新事件挤到列表下面。
        #   现在: 起点 = 当前 MAX(id) - SSE_BACKFILL → 只推新事件。
        cursor = None
        try:
            with db_cursor() as conn:
                _max_row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM news_events").fetchone()
            cursor = max(0, int(_max_row["m"] or 0) - SSE_BACKFILL)
        except Exception as e:
            print(f"[api_events_stream] MAX(id) 读取失败: {e}", file=sys.stderr)
            cursor = 0
        # 先送一次心跳, 让前端 EventSource.onopen 触发
        yield f"data: {_json.dumps({'type': 'heartbeat', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"
        for tick in range(60 * 60):  # 最多 1h, 让客户端重连 (前端会做)
            try:
                # 取 cursor 之后的新事件
                with db_cursor() as conn:
                    rows = conn.execute(
                        # v311: 必须 JOIN 出 decision_id / 溯源 / 推理链 —— 旧实现只选
                        # news_events 的 11 个字段, decision_id 恒为 null →
                        # 前端 isAnalyzed=false → 整块「分析 + 新闻溯源」永不渲染
                        # (表现为"分析和时间溯源信息全没了")。
                        # ai_decisions 用 news_id 回退: decision_id 为空的行也能找回决策。
                        "SELECT ne.id, ne.news_id, ne.decision_id, ne.source, ne.headline, ne.body, "
                        "ne.action, ne.score, ne.assets, ne.asset, ne.ts, ne.fetched_at, "
                        "ne.status, ne.is_noise, "
                        "rn.status AS raw_status, "
                        "rn.source AS raw_source, rn.published_at, rn.first_seen_ts, "
                        "rn.net_origin_info, rn.cross_net_source, rn.cross_net_first_seen_ts, "
                        "rn.content_fingerprint, rn.gate_verdict, rn.gate_core_impact, "
                        "rn.relevance_score, rn.url AS raw_url, "
                        # v321 fix: 门卫冻结标记（news_events 无此列，从 raw_news JOIN 取）
                        "rn.gate_unavailable, "
                        "ad.id AS ad_id, ad.reasoning_path, ad.expected_horizon, ad.event_strength, "
                        "ad.event_phase, ad.direct_catalyst, ad.market_confirmation, "
                        "ad.attention_score, ad.mfe_pct, ad.mae_pct, ad.decision_context, "
                        "ad.invalidation_condition, ad.no_trade_reason, ad.trade_gate_reason, "
                        "ad.evidence_confidence, ad.prediction_type, ad.reasoning, "
                        "ad.sentiment_score, ad.target_asset, ad.market_category, ad.created_at, "
                        "ad.vip_tag, ad.partial_exit_price, ad.partial_exit_ratio, "
                        "ad.partial_pnl_pct, ad.suggested_action, "
                        "qs.entry_price, qs.exit_price, qs.max_price, qs.min_price, "
                        "qs.max_price_time, qs.min_price_time, qs.settled, "
                        "qs.verdict AS is_correct, qs.entry_ts "
                        "FROM news_events ne "
                        "LEFT JOIN raw_news rn ON rn.id = ne.news_id "
                        # v317 fix：主决策口径与 api_server 的 _EVENT_SELECT 对齐
                        #   （最新主决策）→ 详见 /api/news/recent 处的修正说明。
                        # ⚠ SQLite 坑: ON 子查询的 ORDER BY 不能引用外层别名(ne.*)
                        "LEFT JOIN ai_decisions ad ON ad.id = ("
                        "SELECT x.id FROM ai_decisions x WHERE x.news_id = ne.news_id "
                        "AND (x.is_sibling = 0 OR x.is_sibling IS NULL) "
                        "ORDER BY x.id DESC LIMIT 1) "
                        "LEFT JOIN quick_sim_trades qs ON qs.id = ("
                        "SELECT q2.id FROM quick_sim_trades q2 WHERE q2.decision_id = ad.id "
                        "OR (ad.id IS NULL AND q2.news_id = ne.news_id) "
                        "ORDER BY q2.id DESC LIMIT 1) "
                        "WHERE ne.id > ? ORDER BY ne.id ASC LIMIT 20",
                        (cursor,),
                    ).fetchall()
                if rows:
                    for r in rows:
                        cursor = max(cursor, int(r["id"]))
                        evt = _news_row_to_api_event(r)
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                else:
                    # 5s 心跳保持连接
                    yield f"data: {_json.dumps({'type': 'heartbeat', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"
            except Exception as e:
                print(f"[api_events_stream error] {e}", file=sys.stderr)
                yield f"data: {_json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
            time.sleep(5)
        # 1h 后主动断开 (服务端发 done, 客户端会重连)
        yield f"data: {_json.dumps({'type': 'done', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"

@app.get("/api/events/stream")
def api_events_stream(request: Request):
    """SSE 真实流 (v320): 与 /api/events 同一权威口径 (raw_news 基准)。

    v320 P0 修复：原来这里推的是 news_events 的行 ——
      · news_events 按品种拆行 → 同一条新闻被推多次（前端列表重复行）；
      · news_events 含 raw_news 已被裁剪的孤儿行 → 无 raw_status/无决策 →
        恒判 PENDING → 「待 AI 分析」徽章刷屏；
      · 与 /api/events(REST) 不同源 → REST 与 SSE 的行对不上。
    现在改为复用 api_server 的权威行构造（_EVENT_SELECT + _row_to_event），
    与 8000 完全同源；api_server 不可用时退回旧的 news_events 流。
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    def event_generator():
        """权威口径流：raw_news 基准，与 8000 同源。"""
        if _api_server_module() is None:
            yield from _events_stream_legacy_generator(_json)
            return
        # 游标 = raw_news.id（权威事件的 id 空间 = news_id）
        cursor = 0
        try:
            with db_cursor() as conn:
                _max_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM raw_news").fetchone()
            cursor = max(0, int(_max_row["m"] or 0) - 50)
        except Exception as e:
            print(f"[api_events_stream] raw_news MAX(id) 读取失败: {e}", file=sys.stderr)
            cursor = 0
        yield f"data: {_json.dumps({'type': 'heartbeat', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"
        for tick in range(60 * 60):  # 最多 1h, 让客户端重连 (前端会做)
            try:
                with db_cursor() as conn:
                    ids = [int(r["id"]) for r in conn.execute(
                        "SELECT id FROM raw_news WHERE id > ? AND is_noise = 0 "
                        "ORDER BY id ASC LIMIT 20", (cursor,)).fetchall()]
                rows = _events_authoritative_rows(news_ids=ids, limit=20) if ids else []
                if rows:
                    for r in rows:
                        nid = int(r.get("news_id") or 0)
                        if nid:
                            cursor = max(cursor, nid)
                        yield f"data: {_json.dumps(r, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {_json.dumps({'type': 'heartbeat', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"
            except Exception as e:
                print(f"[api_events_stream error] {e}", file=sys.stderr)
                yield f"data: {_json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
            time.sleep(5)
        yield f"data: {_json.dumps({'type': 'done', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── v300 门卫筛选双流: 派生自 news_events.action ─────────────────────
# 前端 GatekeeperFilterStream (components/quant/gatekeeper-filter-stream.tsx) 用:
#   - passed:   action ∈ {BUY, SELL} (有方向信号,被晒进交易回路)
#   - filtered: action ∈ {HOLD, IGNORE, ''} (无信号被 L1/L2 筛掉)
#   - relevance_score: |score| (信号强度, 前端展示用)
#   - status:   'BUY' / 'SELL' / 'HOLD' / 'IGNORE' / 'UNSCORED' (raw action)
# 数据源是同一个 news_events, 不需要新表, 不需要新 worker.
# 15s 轮询, limit 默认 20.
@app.get("/api/events/filter-stream")
async def api_events_filter_stream(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    hours: Annotated[int | None, Query(ge=1, le=8760)] = None,
):
    """门卫筛选双流 (passed/filtered), 给前端 GatekeeperFilterStream 用.

    字段对齐前端 FilterStream 类型:
      { passed: FilterItem[], filtered: FilterItem[], counts: {...}, today, generated_at }
    """
    # v455: 权威实现已在本进程（routers.events.get_gatekeeper_filter_stream），
    #   直调 async 端点；原先自转发到 8100 自身会回到同一线程池排队形成自循环超时。
    try:
        from routers.events import get_gatekeeper_filter_stream
        return await get_gatekeeper_filter_stream(limit=int(limit))
    except Exception as exc:
        print(f"[bridge] events/filter-stream 权威实现失败, 本地兜底: {exc}", file=sys.stderr)
    now = int(time.time())
    today_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    # 用 UTC 当日 (而非本地 date.today(), 否则非 UTC 时区会在 0:00 前算成明天, 未来边界)
    today_utc = datetime.now(timezone.utc).date()
    today_start = int(datetime.combine(today_utc, datetime_time.min, tzinfo=timezone.utc).timestamp())

    # sql 共用, 加 hours 时再 where
    base_sql = (
        "SELECT id, source, headline, body, action, score, ts "
        "FROM news_events WHERE 1=1"
    )
    params: list = []
    if hours is not None:
        base_sql += " AND ts >= ?"
        params.append(now - hours * 3600)
    base_sql += " ORDER BY ts DESC LIMIT ?"
    # 取两倍 limit 留给 passed/filtered 各一份
    params.append(limit * 2)

    def _row_to_filter_item(r) -> dict:
        # v303 Q3 三态化: 空字符串/NULL 显式归 HOLD (不再返 UNSCORED 给前端)
        raw_action = r["action"]
        action = (raw_action or "").upper().strip()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        score = float(r["score"] or 0.0)
        return {
            "id": r["id"],
            "source": r["source"] or "unknown",
            # v499: news_events.headline 里残留采集器写的 "[hash:xxxx] 标题" 前缀，
            #   首页「实时事件流」直接原样渲染（实拍能看到 [hash:ca2c787f…]）。
            #   news_text 那条链(v311)早就清洗了，这一条漏了。
            "content_preview": _strip_hash_prefix(r["headline"] or "")[:160],
            "timestamp": _ts_to_str(r["ts"]),
            "relevance_score": round(abs(score), 3),
            "status": action,
        }

    try:
        with db_cursor() as conn:
            rows = conn.execute(base_sql, params).fetchall()
            # counts: 当日 0:00 UTC 起算
            counts = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN UPPER(IFNULL(action,'')) IN ('BUY','SELL') THEN 1 ELSE 0 END) AS passed_today, "
                "  SUM(CASE WHEN UPPER(IFNULL(action,'')) NOT IN ('BUY','SELL') THEN 1 ELSE 0 END) AS filtered_today, "
                "  SUM(CASE WHEN UPPER(IFNULL(action,'')) IN ('BUY','SELL') THEN 1 ELSE 0 END) AS passed_total, "
                "  SUM(CASE WHEN UPPER(IFNULL(action,'')) NOT IN ('BUY','SELL') THEN 1 ELSE 0 END) AS filtered_total "
                "FROM news_events "
                "WHERE ts >= ?",
                (today_start,),
            ).fetchone()
        items = [_row_to_filter_item(r) for r in rows]
        passed = [it for it in items if it["status"] in ("BUY", "SELL")]
        filtered = [it for it in items if it["status"] not in ("BUY", "SELL")]
        # 截到 limit 条 (双流各 max 12 条, 前端会再 .slice)
        passed = passed[:limit]
        filtered = filtered[:limit]
        return {
            "passed": passed,
            "filtered": filtered,
            "counts": {
                "passed_today": int(counts["passed_today"] or 0),
                "filtered_today": int(counts["filtered_today"] or 0),
                "passed_total": int(counts["passed_total"] or 0),
                "filtered_total": int(counts["filtered_total"] or 0),
            },
            "today": today_str,
            "generated_at": _ts_to_str(now),
            "ok": True,
            "data_source": "news_events v300+ (action-based 派生)",
        }
    except Exception as e:
        # 失败时也要返 FilterStream 形状, 防止前端 .length 崩溃
        print(f"[api_events_filter_stream error] {e}", file=sys.stderr)
        return {
            "passed": [],
            "filtered": [],
            "counts": {"passed_today": 0, "filtered_today": 0,
                       "passed_total": 0, "filtered_total": 0},
            "today": today_str,
            "generated_at": _ts_to_str(now),
            "ok": False,
            "error": str(e),
        }


# ─── v292+ 因果图谱闭环: 实战反馈自动更新 hit/miss ───────────────────
# 背景: causal_chains 5+3 条链路此前 hit=0 miss=0,从未被实战验证过。
#       现在通过 decision_settle_now 钩入 + 批量回测两个入口,让 hit_rate 真正活起来。
#
# 算法 (3 因子联合判定):
#   1) asset 精确匹配 (BTC::xxx 只匹配 BTC 决策)
#   2) 关键词匹配: 在 text_blob (reflection_text + subcategory + asset+action 占位) 中
#      命中 trigger_keywords 任一关键词 (空关键词列表 → 不参与匹配, 视作方向-only)
#   3) 幅度区间: |outcome_pct| ∈ [magnitude_lo, magnitude_hi]
#   4) 方向匹配: expected_direction vs sign(outcome_pct)
#
# 综合:
#   方向对 + 幅度对 + 关键词命中 → hit_count++
#   方向反 + 幅度对 + 关键词命中 → miss_count++
#   方向对 + 幅度超出            → 不计 (噪声/小波段)
#   关键词未命中                  → 不计 (链不相关)


def _match_chains_for_outcome(conn, *, asset: str, outcome_pct: float, action: str = "",
                              text_blob: str = "", dry_run: bool = False) -> dict:
    """对一次 (asset, outcome_pct, action) 匹配所有 active chains, 写 hit/miss.
    conn 内调用 (调用方负责事务).
    返回 {matched, hits, misses, details}.
    """
    try:
        chains = conn.execute(
            "SELECT chain_id, asset, expected_direction, magnitude_lo, magnitude_hi, trigger_keywords "
            "FROM causal_chains WHERE active=1 AND asset=?",
            (asset,),
        ).fetchall()
    except Exception as e:
        print(f"[_match_chains_for_outcome SELECT err] {e}", file=sys.stderr)
        return {"matched": 0, "hits": 0, "misses": 0, "details": [], "error": str(e)}

    try:
        from causal_chain_evaluator import meaningful_keyword_hit
    except Exception:  # evaluator 不可用 → 退回宽松匹配 (旧行为)
        meaningful_keyword_hit = None

    blob = (text_blob or "").lower()
    hits = misses = matched = 0
    details: list[dict] = []
    for c in chains:
        keywords = [k.strip().lower() for k in (c["trigger_keywords"] or "").split(",") if k.strip()]
        if not keywords:
            # 无关键词 → 视作方向-only (保持原行为)
            kw_hits, kw_matched = [], True
        elif meaningful_keyword_hit is None:
            kw_hits, kw_matched = keywords, True
        else:
            # 与 evaluator._kw_match 同规则: 至少命中一个"有意义"的关键词。
            # 否则 'buy'/'sell' 这类高频词会让同资产的所有链同时命中,
            # 命中量被放大几十倍, hit_rate 失去意义。
            kw_hits = meaningful_keyword_hit(blob, keywords, c["asset"] or "")
            kw_matched = bool(kw_hits)
        if not kw_matched:
            details.append({"chain_id": c["chain_id"], "skip_reason": "kw_no_match"})
            continue
        matched += 1
        # 方向
        if c["expected_direction"] == "UP":
            dir_match = outcome_pct > 0
        elif c["expected_direction"] == "DOWN":
            dir_match = outcome_pct < 0
        else:  # BOTH
            dir_match = outcome_pct != 0
        # 幅度
        mag = abs(outcome_pct)
        mag_match = c["magnitude_lo"] <= mag <= c["magnitude_hi"]
        if dir_match and mag_match:
            if not dry_run:
                conn.execute(
                    "UPDATE causal_chains SET hit_count=hit_count+1, "
                    "last_verified_ts=strftime('%s','now'), updated_at=strftime('%s','now') "
                    "WHERE chain_id=?",
                    (c["chain_id"],),
                )
            hits += 1
            details.append({"chain_id": c["chain_id"], "verdict": "hit", "mag": round(mag, 2),
                            "kws_hit": kw_hits[:3]})
        elif not dir_match and mag_match:
            if not dry_run:
                conn.execute(
                    "UPDATE causal_chains SET miss_count=miss_count+1, "
                    "last_verified_ts=strftime('%s','now'), updated_at=strftime('%s','now') "
                    "WHERE chain_id=?",
                    (c["chain_id"],),
                )
            misses += 1
            details.append({"chain_id": c["chain_id"], "verdict": "miss", "mag": round(mag, 2),
                            "kws_hit": kw_hits[:3]})
        else:
            details.append({"chain_id": c["chain_id"], "skip_reason": "out_of_mag_range",
                            "mag": round(mag, 2), "need": f"{c['magnitude_lo']}~{c['magnitude_hi']}"})
    return {"matched": matched, "hits": hits, "misses": misses, "details": details}


# ─── /api/v291/causal-chains/score-batch (活图谱闭环) ───
@app.post("/api/v291/causal-chains/score-batch")
def causal_chains_score_batch(payload: dict | None = None):
    """批量回测: 遍历 trade_reflections_v2 中所有 settled=1 的历史记录,
    给 active causal_chains 打分 (hit/miss). 用于启动时一次性激活链路,
    之后新结算由 settle-now 钩子增量更新。
    """
    payload = payload or {}
    reset = bool(payload.get("reset", False))  # 是否先清零 hit/miss
    asset_filter = payload.get("asset")  # 可选: 只回测某资产
    only_active = bool(payload.get("only_active", True))

    with write_db_cursor() as conn:
        if reset:
            if only_active:
                conn.execute("UPDATE causal_chains SET hit_count=0, miss_count=0, "
                             "last_verified_ts=NULL WHERE active=1")
            else:
                conn.execute("UPDATE causal_chains SET hit_count=0, miss_count=0, last_verified_ts=NULL")
            print(f"[score-batch] reset hit/miss (only_active={only_active})", flush=True)

        sql = ("SELECT id, decision_id, asset, subcategory, reflection_text, actual_outcome_pct "
               "FROM trade_reflections_v2 WHERE settled=1 AND actual_outcome_pct IS NOT NULL")
        params: list = []
        if asset_filter:
            sql += " AND asset=?"
            params.append(asset_filter)
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql, params).fetchall()
        n_total = len(rows)

        per_asset: dict = {}
        hits = misses = 0
        no_match = 0
        for r in rows:
            asset = r["asset"]
            outcome = r["actual_outcome_pct"]
            # v292+ 关键: 把 asset 名字拼进 text_blob, 这样链路的 trigger_keywords
            # 包含资产别名 (如 skhynix/hynix, ada/cardano) 时能命中
            text_blob = " ".join(filter(None, [
                asset or "", r["subcategory"] or "", r["reflection_text"] or "",
            ]))
            m = _match_chains_for_outcome(conn, asset=asset, outcome_pct=outcome,
                                          action="", text_blob=text_blob, dry_run=False)
            hits += m["hits"]
            misses += m["misses"]
            if m["matched"] == 0:
                no_match += 1
            if asset not in per_asset:
                per_asset[asset] = {"scored": 0, "hits": 0, "misses": 0, "no_chain_match": 0}
            per_asset[asset]["scored"] += 1
            if m["matched"] == 0:
                per_asset[asset]["no_chain_match"] += 1
            else:
                per_asset[asset]["hits"] += m["hits"]
                per_asset[asset]["misses"] += m["misses"]

        cur = conn.execute(
            "SELECT chain_id, asset, hit_count, miss_count, last_verified_ts FROM causal_chains "
            "WHERE active=1 ORDER BY asset ASC"
        ).fetchall()

    chains_out = []
    for r in cur:
        h, m_ = r["hit_count"], r["miss_count"]
        n = h + m_
        rate = round(h / n, 3) if n > 0 else None
        chains_out.append({
            "chain_id": r["chain_id"], "asset": r["asset"],
            "hit_count": h, "miss_count": m_, "hit_rate": rate,
            "last_verified_ts": r["last_verified_ts"],
        })

    return {
        "ok": True,
        "scanned": n_total,
        "hits_added": hits, "misses_added": misses,
        "no_chain_match_reflections": no_match,
        "per_asset": per_asset,
        "per_chain_current": chains_out,
        "reset": reset,
        "data_source": "trade_reflections_v2 → causal_chains v292+ 闭环",
    }


# ─── /api/v291/causal-chains/score-single (单条决策的 hook 验证) ───
@app.post("/api/v291/causal-chains/score-single")
def causal_chains_score_single(payload: dict):
    """手工指定 outcome_pct + 文本, 看哪条 chain 会匹配 (dry-run)."""
    asset = (payload.get("asset") or "").upper()
    outcome_pct = float(payload.get("outcome_pct", 0))
    text_blob = payload.get("text_blob") or ""
    with db_cursor() as conn:
        m = _match_chains_for_outcome(conn, asset=asset, outcome_pct=outcome_pct,
                                      action="", text_blob=text_blob, dry_run=True)
    return {"ok": True, "dry_run": True, "asset": asset, "outcome_pct": outcome_pct,
            "matched": m["matched"], "hits": m["hits"], "misses": m["misses"],
            "details": m["details"]}


# ─── /api/v291/event-move-attribution (归因深度 · C 选项) ───
@app.post("/api/v291/event-move-attribution/rebuild")
def event_move_attribution_rebuild(payload: dict | None = None):
    """触发 calc_event_move_attribution.py 重算事件↔涨跌配对。
    让归因页 "事件标注" 面板有真实数据, 不再是 honest_note "无 event_move_attribution 真实事件源"。

    payload.days: 回溯天数 (默认 30)
    """
    import subprocess
    payload = payload or {}
    days = int(payload.get("days", 30))
    script = (Path(__file__).resolve().parent / "calc_event_move_attribution.py").as_posix()
    try:
        proc = subprocess.run(
            ["python3", script, "--days", str(days)],
            capture_output=True, text=True, timeout=120,
        )
        ok = (proc.returncode == 0)
        out = proc.stdout[-2000:]  # 末尾 2KB
        err = proc.stderr[-500:] if proc.stderr else ""
    except Exception as e:
        ok = False
        out = ""
        err = str(e)
    # 跑完再查总数 + 资产分布
    # v308 P1-fix3: 计数也用同一套过滤 —— 污染行不该被算进"重建后有多少行",
    # 否则界面上的行数与 /api/v291/event-move-attribution 的 n 对不上。
    with db_cursor() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) <= ?",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchone()[0]
        raw_total = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution"
        ).fetchone()[0]
        per_asset = conn.execute(
            "SELECT asset, COUNT(*) AS n FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) <= ? "
            "GROUP BY asset ORDER BY n DESC",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchall()
    return {
        "ok": ok,
        "script": "calc_event_move_attribution.py",
        "args": {"days": days},
        "returncode": proc.returncode if 'proc' in dir() else None,
        "stdout_tail": out,
        "stderr_tail": err,
        "rows_total": total,
        "rows_total_raw": raw_total,
        "per_asset": [{"asset": r["asset"], "n": r["n"]} for r in per_asset],
        "data_source": "critical_price_moves × news_events → event_move_attribution (v292+ 归因补全)",
    }


# ─── G14: 归因上下文 (量能 / 盘口 / 资金流 / 形态) ────────────────────────
# 背景: 库里明明有 kline_bars(量能+形态)、orderbook_depth(盘口)、funding_rate(资金流)
# 和 tech_signals(技术形态), 但归因接口只返回"归因结论"本身, 不带任何行情依据 ——
# 归因页看到的是结论, 看不到依据, 用户无法判断这条归因可不可信 (需求 #6)。
ATTRIB_SYMBOL_MAP = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "BNB": "BNBUSDT",
}
_ATTRIB_LIVE_CACHE: dict = {}     # asset -> (ts, dict)
_ATTRIB_LIVE_TTL = 300            # 实时盘口/资金流缓存 5 分钟

# ── v321.3 P2: 外网熔断 (归因端点的实时盘口/资金费率) ───────────────────────
# 实测: 归因端点 limit=20 冷启动要 **45 秒** (http=000 超时), limit=100 命中缓存
# 只要 3.2s —— 真凶是 `_attribution_live_context`: Binance 盘口
# (fetch_orderbook_metrics **没有**超时预算) + 资金费率, 而**外网根本不通**
# (实测 Binance / OKX / Bybit / Yahoo 全部 timeout), 每次都在等 TCP 超时。
# 多个 asset × 每次十几秒 = 整个端点被拖死 → G14 判成「无数据」。
#
# 现在: 外网失败一次就在 ATTR_NET_BLOCK_SECS 内**不再试** (熔断), 直接用库内
# macro_snapshots 的真实序列兜底 (见 _attribution_flow_context)。
# 熔断有日志 (不静默), 外网恢复后自动重试。
ATTR_NET_BLOCK_SECS = 600.0
ATTR_LIVE_BUDGET_S = 3.0        # 实时盘口/资金费率的**硬预算** (秒)
_ATTR_NET_BLOCK_UNTIL = 0.0
_ATTR_NET_FAILURES = 0


def _attr_net_blocked() -> bool:
    """外网当前是否处于熔断期。"""
    return time.monotonic() < _ATTR_NET_BLOCK_UNTIL


def _attr_net_note_failure() -> None:
    global _ATTR_NET_BLOCK_UNTIL, _ATTR_NET_FAILURES
    _ATTR_NET_FAILURES += 1
    _ATTR_NET_BLOCK_UNTIL = time.monotonic() + ATTR_NET_BLOCK_SECS
    print(f"[bridge_server] ⚠️ 外网不可达, 归因实时盘口/资金费率熔断 "
          f"{int(ATTR_NET_BLOCK_SECS)}s (第 {_ATTR_NET_FAILURES} 次) —— "
          f"改用库内 macro_snapshots 的真实序列兜底", flush=True)


def _candle_pattern(o, h, l, c) -> str:
    """K 线形态 (供归因展示: 这波异动是什么形态)。"""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return ""
    rng = h - l
    if rng <= 0:
        return "平盘"
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    if body / rng < 0.12:
        return "十字星"
    if upper / rng > 0.45 and body / rng < 0.4:
        return "长上影线"
    if lower / rng > 0.45 and body / rng < 0.4:
        return "长下影线"
    return "阳线" if c > o else "阴线"


def _attribution_kline_context(conn, asset: str, move_ts: int) -> dict:
    """异动时刻前后的量能与形态 (来自 kline_bars 真实数据)。"""
    out: dict = {}
    if not asset or not move_ts:
        return out
    sym = asset.upper()
    try:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol=? AND timeframe='1m' AND ts BETWEEN ? AND ? ORDER BY ts",
            (sym, (int(move_ts) - 1800) * 1000, (int(move_ts) + 900) * 1000),
        ).fetchall()
    except Exception:
        return out
    if not rows:
        return out
    vols = [float(r[5] or 0.0) for r in rows]
    idx = min(range(len(rows)), key=lambda i: abs(int(rows[i][0]) - int(move_ts) * 1000))
    row = rows[idx]
    # SELECT 顺序: ts, open, high, low, close, volume
    bar_vol = float(row[5] or 0.0)
    prev_vols = [v for v in vols[:idx] if v > 0]
    avg_vol = (sum(prev_vols) / len(prev_vols)) if prev_vols else 0.0
    out["kline_open"] = float(row[1]) if row[1] is not None else None
    out["kline_high"] = float(row[2]) if row[2] is not None else None
    out["kline_low"] = float(row[3]) if row[3] is not None else None
    out["kline_close"] = float(row[4]) if row[4] is not None else None
    out["kline_pattern"] = _candle_pattern(row[1], row[2], row[3], row[4])
    out["kline_volume"] = bar_vol
    out["volume_ratio"] = round(bar_vol / avg_vol, 3) if avg_vol else None
    return out


def _attribution_live_context(asset: str) -> dict:
    """盘口 + 资金流 (实时, 进程内缓存; 外部不可达时降级为空字典)。

    v321.3 P2 (外网熔断): 实测归因端点 limit=20 冷启动要 **45 秒** (http=000
    超时), limit=100 命中缓存只要 3.2s —— 真凶是这里: Binance 盘口
    (fetch_orderbook_metrics **没有**超时预算) + 资金费率, 而**外网根本不通**
    (实测 Binance / OKX / Bybit / Yahoo 全部 timeout), 每次都在等 TCP 超时。
    多个 asset × 每次十几秒 = 整个端点被拖死 → G14 判成「无数据」。

    现在: 外网失败一次就在 ATTR_NET_BLOCK_SECS 内**不再试** (熔断), 直接用
    库内 macro_snapshots 的真实序列兜底 (见 _attribution_flow_context)。
    熔断有日志 (不静默), 外网恢复后自动重试。
    """
    now = int(time.time())
    cached = _ATTRIB_LIVE_CACHE.get(asset)
    if cached and now - cached[0] < _ATTRIB_LIVE_TTL:
        return cached[1]
    out: dict = {}
    symbol = ATTRIB_SYMBOL_MAP.get((asset or "").upper())
    # G14: 实时盘口/资金费率改成**后台异步**拉取 —— 它是"锦上添花", 不该阻塞
    # 归因页。实测: 同步等 (即使加了 3s 预算) 因为 `with ThreadPoolExecutor` 退出时
    # 会 wait=True 等线程跑完 → 10 个资产 × 3s = **30 秒** → G14 判成「无数据」。
    # 现在: 派一个后台线程去拉, 结果写进缓存; **本次请求直接用库内真实序列兜底**
    # (见 _attribution_flow_context)。下次请求就有实时数据了。
    if symbol and not _attr_net_blocked():
        import threading as _th
        _th.Thread(target=_attribution_live_fetch_bg, args=(asset, symbol),
                   daemon=True, name=f"attr-live-{symbol}").start()
    _ATTRIB_LIVE_CACHE[asset] = (now, out)
    return out


def _attribution_live_fetch_bg(asset: str, symbol: str) -> None:
    """后台拉盘口 + 资金费率, 成功写缓存 / 失败触发熔断。绝不抛到主线程。"""
    out: dict = {}
    try:
        from orderbook_depth import fetch_orderbook_metrics
        ob = fetch_orderbook_metrics(symbol)
        if ob.get("ok"):
            for k in ("bid_ask_imbalance", "depth_ratio", "spread_bps",
                      "top_bid_volume", "top_ask_volume", "wall_detected",
                      "buy_pressure", "sell_pressure"):
                if k in ob:
                    out["orderbook_" + k] = ob.get(k)
            out["orderbook_symbol"] = symbol
    except Exception as exc:
        print(f"[bridge_server] ⚠️ 盘口获取失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:100]} ({symbol})", flush=True)
    try:
        from funding_rate import get_funding_metrics
        fr = get_funding_metrics(symbol)
        if fr.get("ok"):
            for k in ("funding_rate", "funding_rate_pct", "open_interest",
                      "oi_change_pct", "annualized_funding_rate", "long_short_ratio"):
                if k in fr:
                    out[k] = fr.get(k)
    except Exception as exc:
        print(f"[bridge_server] ⚠️ 资金费率获取失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:100]} ({symbol})", flush=True)
    if not out:
        # 外网确实不通 → 熔断一段时间, 别每个资产都去等一次
        _attr_net_note_failure()
        return
    _ATTRIB_LIVE_CACHE[asset] = (int(time.time()), out)


def _attribution_tech_context(conn, asset: str, move_ts: int) -> dict:
    """技术形态上下文 (tech_signals, 只取 uses_future=0 的因果信号)。

    未来函数纪律: uses_future=1 的信号是"转折点"信号(用了未来 la 根 bar),
    不能用来解释异动 —— 这里只取纯因果行, 且时间上不晚于异动时刻。
    """
    out: dict = {}
    if not asset or not move_ts:
        return out
    try:
        row = conn.execute(
            "SELECT ts, trigger_type, score, vol_ratio, pct_1d, pct_3d, close "
            "FROM tech_signals WHERE asset=? AND uses_future=0 AND ts<=? AND ts>=? "
            "ORDER BY ts DESC LIMIT 1",
            (asset.upper(), int(move_ts), int(move_ts) - 7 * 86400),
        ).fetchone()
    except Exception:
        return out
    if not row:
        return out
    out["tech_pattern"] = row[1] or ""
    out["tech_score"] = row[2]
    if row[3] is not None:
        out["tech_vol_ratio"] = row[3]
    out["tech_pct_1d"] = row[4]
    out["tech_pct_3d"] = row[5]
    out["tech_close"] = row[6]
    return out


def _attribution_daily_context(conn, asset: str, move_ts: int) -> dict:
    """日线级别的量能/形态兜底 (给没有 1m 行情的品种: MU / SKHYNIX / NVDA …)。"""
    out: dict = {}
    if not asset or not move_ts:
        return out
    try:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_daily "
            "WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 20",
            (asset.upper(), int(move_ts)),
        ).fetchall()
    except Exception:
        return out
    if not rows:
        return out
    vols = [float(r[5] or 0.0) for r in rows if r[5]]
    if vols:
        out["daily_volume"] = vols[0]
        out["daily_volume_ratio"] = round(vols[0] / (sum(vols[1:]) / max(1, len(vols[1:]))), 3) \
            if len(vols) > 1 and sum(vols[1:]) else None
    out["daily_pattern"] = _candle_pattern(rows[0][1], rows[0][2], rows[0][3], rows[0][4])
    return out


_FLOW_METRIC_KEYS = (
    "BTC.cvd", "BTC.taker_buy_sell", "BTC.opt_wall", "BTC.opt_call_put_oi",
    "BTC.opt_skew", "BTC.ls_ratio", "BTC.top_ls_ratio",
    "ETH.cvd", "ETH.taker_buy_sell", "ETH.opt_wall", "ETH.opt_call_put_oi",
    "ETH.opt_skew", "ETH.ls_ratio", "ETH.top_ls_ratio",
    "WTI.cvd", "WTI.taker_buy_sell", "WTI.ls_ratio", "WTI.top_ls_ratio",
    "XAU.cvd", "XAU.taker_buy_sell", "XAU.ls_ratio", "XAU.top_ls_ratio",
    "agg.oi", "agg.funding", "agg.liquidation",
)
_FLOW_CACHE: dict = {"ts": 0.0, "value": {}}
_FLOW_CACHE_TTL_S = 60.0


def _flow_latest_values() -> dict:
    """一次查询取回所有资金流/挂单指标的最新值 (60s TTL 缓存)。

    v321.3 P1 性能修复: 之前每个资产都单独查一遍 macro_snapshots (10 个 key/资产),
    limit=100 时端点直接超时 (G14 判成「无数据」)。现在**一次查询 + 全资产复用**。
    """
    now = time.time()
    if (now - float(_FLOW_CACHE.get("ts", 0.0))) < _FLOW_CACHE_TTL_S:
        return _FLOW_CACHE["value"]
    out: dict = {}
    try:
        conn = db.get_connection()
        conn.row_factory = __import__("sqlite3").Row
        try:
            ph = ",".join("?" * len(_FLOW_METRIC_KEYS))
            rows = conn.execute(
                "SELECT metric_key, value, ts FROM macro_snapshots "
                f"WHERE metric_key IN ({ph}) AND status='ok' AND value IS NOT NULL "
                "ORDER BY ts DESC",
                _FLOW_METRIC_KEYS,
            ).fetchall()
            for r in rows:
                k = r["metric_key"]
                if k in out:
                    continue          # 已取到更新的 (ORDER BY ts DESC → 第一个最新)
                try:
                    out[k] = (float(r["value"]), int(r["ts"] or 0))
                except (TypeError, ValueError):
                    continue
        finally:
            conn.close()
    except Exception:
        out = {}
    _FLOW_CACHE.update({"ts": now, "value": out})
    return out


def _attribution_flow_context(asset: str) -> dict:
    """归因行的**真实**资金流 / 挂单量依据 (不依赖外网)。

    v321.3 P1 (需求 #6): 之前归因页的"资金流/挂单量"只有 Binance 盘口 + 资金费率
    两条外网路径, 而外网不可达 → 字段全空 → 页面看不到任何资金依据。
    现在改成从 **macro_snapshots 里已落库的真实观测**取 —— 不需要外网, 而且
    比盘口快照更适合做归因 (是**已结算的历史序列**, 不是某一秒的快照):

      · 资金流:  {S}.cvd (主动买卖累计差) / {S}.taker_buy_sell (主动买卖比)
      · 持仓:    agg.oi (总持仓) / agg.funding (综合资金费率)
      · 杠杆:    agg.liquidation (强平额) — 套利离场的直接证据
      · 挂单量:  {S}.opt_wall (期权大额挂单墙) / {S}.opt_call_put_oi (期权多空持仓)
      · 多空比:  {S}.ls_ratio / {S}.top_ls_ratio (大户多空比)
    取不到就**不返回该字段** —— 宁可缺, 也不编。
    """
    out: dict = {}
    sym = (asset or "").upper()
    base = ""
    for cand in ("BTC", "ETH", "WTI", "XAU"):
        if sym.startswith(cand):
            base = cand
            break
    if not base:
        # 非这四类的品种 → 只给全局 (agg.*) 的观测
        base = ""
    latest = _flow_latest_values()
    for key, (val, ts) in latest.items():
        if base and key.startswith(base + "."):
            out[key.replace(".", "_")] = round(val, 6)
            out[key.replace(".", "_") + "_ts"] = ts
        elif key.startswith("agg."):
            out[key.replace(".", "_")] = round(val, 6)
            out[key.replace(".", "_") + "_ts"] = ts
    # 派生: 主动买卖比 → 买压占比 (归因页最直观的资金方向)
    tb = out.get(f"{base}_taker_buy_sell") if base else None
    if tb is not None:
        out["taker_buy_ratio"] = round((tb + 1.0) / 2.0, 4) if -1 <= tb <= 1 else None
    return out


def _attribution_market_context(conn, asset: str, move_ts: int) -> dict:
    """归因行的行情依据: 量能 / 形态 / 盘口 / 资金流。"""
    ctx: dict = {}
    ctx.update(_attribution_kline_context(conn, asset, move_ts))
    ctx.update(_attribution_tech_context(conn, asset, move_ts))
    if "volume_ratio" not in ctx:
        ctx.update(_attribution_daily_context(conn, asset, move_ts))
    return ctx


# ─── /api/abnormal-attribution (AI 异动归因: 事件+形态+量能 → 可执行结论) ───
@app.get("/api/abnormal-attribution")
def abnormal_attribution_list(
    asset: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    only_actionable: bool = False,
    min_severity: Annotated[int, Query(ge=0, le=3)] = 0,
):
    """返回最近的 AI 异动归因 (需求 #1 / #6)。

    数据源: abnormal_attributions (AI worker 的多因子归因产物)
      · summary        —— 归因结论
      · drivers        —— 驱动因子 (事件 / 板块 / 技术 / 量能)
      · is_actionable  —— 是否给出可执行结论 (原 actionable 列 97% 是"观望")
      · action_text    —— 可执行结论文本 (带触发条件)
    并附带量能/形态上下文 (kline_bars / tech_signals), 让页面同时展示"依据"。
    """
    cols = set()
    with db_cursor() as conn:
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(abnormal_attributions)").fetchall()}
        except Exception:
            cols = set()
    optional = [c for c in ("is_actionable", "action_text", "action_source") if c in cols]
    sel = ("id, signal_id, asset, signal_kind, anchor_ts, summary, drivers_json, "
           "cross_json, actionable, severity_hint, source_model, used_news_n, created_at")
    if optional:
        sel += ", " + ", ".join(optional)
    sql = "SELECT %s FROM abnormal_attributions WHERE 1=1" % sel
    params: list[Any] = []
    if asset:
        sql += " AND asset=?"
        params.append(asset.upper())
    if only_actionable and "is_actionable" in cols:
        sql += " AND COALESCE(is_actionable,0)=1"
    if min_severity:
        sql += " AND COALESCE(severity_hint,0)>=?"
        params.append(int(min_severity))
    sql += " ORDER BY anchor_ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                drivers = json.loads(d.get("drivers_json") or "[]")
                if not isinstance(drivers, list):
                    drivers = []
            except (json.JSONDecodeError, TypeError):
                drivers = []
            try:
                cross = json.loads(d.get("cross_json") or "[]")
                if not isinstance(cross, list):
                    cross = []
            except (json.JSONDecodeError, TypeError):
                cross = []
            item = {
                "id": d.get("id"), "signal_id": d.get("signal_id"), "asset": d.get("asset"),
                "signal_kind": d.get("signal_kind"), "anchor_ts": d.get("anchor_ts"),
                "summary": d.get("summary"), "drivers": drivers,
                "cross_asset_likely": cross,
                "actionable": d.get("actionable"),
                "is_actionable": int(d.get("is_actionable") or 0),
                "action_text": d.get("action_text") or d.get("actionable"),
                "action_source": d.get("action_source") or "",
                "severity_hint": d.get("severity_hint"),
                "source_model": d.get("source_model"),
                "used_news_n": d.get("used_news_n"),
                "created_at": d.get("created_at"),
            }
            try:
                # v383: 复用同一个只读连接，避免每行新建连接（原 N+1 + 每行进新连接，
                # 30 行 → 30 次 connect/close，在写锁竞争下放大阻塞）。
                item.update(_attribution_market_context(
                    conn, str(d.get("asset") or ""), int(d.get("anchor_ts") or 0)))
            except Exception:
                pass
            items.append(item)
    n_actionable = sum(1 for i in items if i["is_actionable"])
    return {
        "ok": True, "n": len(items), "items": items,
        "n_actionable": n_actionable,
        "data_source": "abnormal_attributions (AI 多因子归因 + 量能/形态上下文)",
    }


@app.get("/api/v291/event-move-attribution")
def event_move_attribution_list(
    asset: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    min_score: float = 0.0,
):
    """读 event_move_attribution (供归因页事件标注面板消费)."""
    where: list[str] = []
    params: list[Any] = []
    if asset:
        where.append("asset=?")
        params.append(asset.upper())
    if min_score > 0:
        where.append("algo_score>=?")
        params.append(min_score)
    # v308 P1-fix3: 存量 event_move_attribution 行可能是在污染修复前算出来的
    # (move_pct 直接拷贝自 critical_price_moves)。过滤必须放在 SQL 层 ——
    # 若先 LIMIT 再在 Python 里剔除, 会被污染行占掉名额, 出现
    # "明明库里还有干净行, 却返回不满一页"。
    where.append("ABS(COALESCE(move_pct,0)) <= ?")
    params.append(MAX_PLAUSIBLE_MOVE_PCT)
    sql = ("SELECT id, move_id, news_event_id, asset, time_lag_sec, algo_score, algo_reasons, "
           "move_ts, move_pct, move_kind, duration_min, ev_source, ev_action, ev_ts, ev_news_id, "
           # ── v321.3 P1: 双重验证结果必须返回 (需求 #6) ──────────────────────
           # 旧实现只取规则引擎的字段 (algo_score/algo_reasons), **没取** ai_called /
           # ai_explanation / ai_confidence → 归因页看不到"AI 复核到底认不认这条归因",
           # 用户要的「双重验证自己检测一遍」在页面上等于不存在。
           # ai_called=0 → 该行还没被 AI 复核 (前端应显示"待复核", 不能当已验证)。
           "ai_called, ai_explanation, ai_confidence, ai_error "
           "FROM event_move_attribution "
           + ("WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY move_ts DESC LIMIT ?")
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        # rows_total 用**同一套过滤**统计, 否则 n(已过滤) 和 rows_total(未过滤)
        # 两个数字对不上, 前端分页会算错。原始总量见 data_quality。
        total = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) <= ?",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchone()[0]
        n_excluded = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) > ?",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchone()[0]
    items = [dict(r) for r in rows]
    # G14: 给每条归因补上量能/形态 (kline_bars) 与盘口/资金流 (实时, 按资产缓存)
    live_ctx: dict = {}
    flow_ctx: dict = {}
    for it in items:
        asset = str(it.get("asset") or "").upper()
        if not asset:
            continue
        try:
            with db_cursor() as conn:
                kctx = _attribution_market_context(conn, asset, int(it.get("move_ts") or 0))
        except Exception:
            kctx = {}
        if kctx:
            it.update(kctx)
        if asset not in live_ctx:
            live_ctx[asset] = _attribution_live_context(asset)
        if live_ctx[asset]:
            it.update(live_ctx[asset])
        # v321.3 P1: 外网不可达时盘口/资金费率为空 → 用**库内已落库的真实**资金流/
        # 挂单量补上 (需求 #6 要求的"资金进出/成交量/挂单量")。不需要外网。
        if asset not in flow_ctx:
            try:
                flow_ctx[asset] = _attribution_flow_context(asset)
            except Exception:
                flow_ctx[asset] = {}
        if flow_ctx[asset]:
            it.update(flow_ctx[asset])
    return {
        "ok": True,
        "n": len(items), "rows_total": total,
        "items": items,
        "context_fields": [
            "volume_ratio", "kline_volume", "kline_pattern", "kline_close",
            "orderbook_bid_ask_imbalance", "funding_rate", "open_interest",
            # v321.3: 外网不可达时的真实替代源 (库内已落库)
            "BTC_cvd", "BTC_taker_buy_sell", "taker_buy_ratio",
            "BTC_opt_wall", "BTC_opt_call_put_oi", "BTC_opt_skew",
            "BTC_ls_ratio", "BTC_top_ls_ratio",
            "agg_oi", "agg_funding", "agg_liquidation",
        ],
        "context_sources": {
            "live_orderbook": "Binance 盘口 (外网不可达时为空)",
            "flow_from_db": "macro_snapshots 已落库真实观测 (cvb/taker/期权/持仓/强平)",
            "note": "外网不可达时用库内真实序列兜底, 不编造数据",
        },
        "data_quality": {
            "implausible_move_rows_excluded": n_excluded,
            "max_plausible_move_pct": MAX_PLAUSIBLE_MOVE_PCT,
            "note": "已剔除 |move_pct|>90% 的量纲污染行 (价格序列混档, 非市场异动)",
        },
        "data_source": "event_move_attribution (v292+ 归因深度补全)",
    }


# ─── /api/v291/shadow-ledger-summary (影子样本 · 真实命中率) ───
@app.get("/api/v291/shadow-ledger-summary")
def shadow_ledger_summary(
    days: Annotated[int, Query(ge=1, le=365)] = 40,
):
    """影子账本总览：闸门没放行的单也在记账并结算，这里是它的真实表现。

    v335 需求背景（用户原话："一天几乎都没有单，我没有样本我怎么测试"）：
    统一置信度闸门 20 把 94% 的方向决策降级成 HOLD，一天剩 0~1 单 —— 统计层
    没有样本。"没放行"不等于"没预测"：决策时点的方向 + 当时的价 + 到期价都在库里。
    这里把三样拼起来的结果直接给出来：

    · totals: 累计样本 / 已结算 / 方向命中率 / 平均涨跌
    · by_asset: 按品种拆（谁在拖后腿）
    · by_direction: BUY / SELL 分开（看是不是系统性偏多）
    · gate_sweep: 同一批样本按不同门槛切 —— 门槛高到底有没有筛出东西
    · daily: 最近 14 天的每日命中率（样本是否稳定攒起来）
    """
    import datetime as _dt
    since = int(_dt.datetime.now().timestamp()) - days * 86400
    with db_cursor() as conn:
        has_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_ledger'"
        ).fetchone()
        if not has_tbl:
            return {"ok": False, "error": "shadow_ledger 表不存在（先跑 shadow_ledger.py --commit）"}
        tot = conn.execute(
            "SELECT COUNT(*), SUM(hit IS NOT NULL), SUM(hit), "
            "       AVG(move_pct), AVG(ABS(move_pct)) "
            "  FROM shadow_ledger WHERE decided_ts >= ?", (since,)).fetchone()
        n_all, n_settled, n_hit, avg_move, avg_abs = tot
        by_asset = conn.execute(
            "SELECT asset, COUNT(*), SUM(hit), AVG(ABS(move_pct)) "
            "  FROM shadow_ledger WHERE decided_ts >= ? AND hit IS NOT NULL "
            " GROUP BY asset ORDER BY 2 DESC", (since,)).fetchall()
        by_dir = conn.execute(
            "SELECT direction, COUNT(*), SUM(hit) "
            "  FROM shadow_ledger WHERE decided_ts >= ? AND hit IS NOT NULL "
            " GROUP BY direction", (since,)).fetchall()
        sweep = []
        for g in (0.0, 2.0, 5.0, 10.0, 20.0, 30.0):
            row = conn.execute(
                "SELECT COUNT(*), SUM(hit) FROM shadow_ledger "
                " WHERE decided_ts >= ? AND hit IS NOT NULL AND confidence >= ?",
                (since, g)).fetchone()
            sweep.append({"gate": g, "n": int(row[0] or 0),
                          "hit_rate": round(100 * (row[1] or 0) / row[0], 1) if row[0] else None})
        # v400: 样本/天按"真有样本的天数"算，不是按查询窗口算。
        # 原来除以 days（默认 40），库里只有 1 天样本时会把 10 条/天显示成 0.75 条/天，
        # 直接误导"样本够不够"的判断。
        covered = conn.execute(
            "SELECT COUNT(DISTINCT date(decided_ts,'unixepoch','+8 hours')) "
            "  FROM shadow_ledger WHERE decided_ts >= ? AND hit IS NOT NULL",
            (since,)).fetchone()[0]
        daily = conn.execute(
            "SELECT date(decided_ts,'unixepoch','+8 hours') d, COUNT(*), SUM(hit) "
            "  FROM shadow_ledger WHERE decided_ts >= ? AND hit IS NOT NULL "
            " GROUP BY 1 ORDER BY 1 DESC LIMIT 14", (since,)).fetchall()

    def _rate(h, n):
        return round(100 * h / n, 1) if n else None

    return {
        "ok": True,
        "days": days,
        "totals": {
            "samples": int(n_all or 0),
            "settled": int(n_settled or 0),
            "hit_rate": _rate(n_hit or 0, n_settled or 0),
            "avg_move_pct": round(avg_move, 4) if avg_move is not None else None,
            "avg_abs_move_pct": round(avg_abs, 3) if avg_abs is not None else None,
            "samples_per_day": round((n_settled or 0) / max(int(covered or 0), 1), 1),
            "days_covered": int(covered or 0),
            "target_samples": shadow_ledger.TARGET_SAMPLES,
            "remaining_to_target": max(shadow_ledger.TARGET_SAMPLES - int(n_settled or 0), 0),
        },
        "by_asset": [{"asset": a, "n": n, "hit_rate": _rate(h or 0, n),
                      "avg_abs_move_pct": round(m or 0, 3)}
                     for a, n, h, m in by_asset],
        "by_direction": [{"direction": d, "n": n, "hit_rate": _rate(h or 0, n)}
                         for d, n, h in by_dir],
        "gate_sweep": sweep,
        "sweep_verdict": shadow_ledger.sweep_verdict(sweep),
        "daily": [{"date": d, "n": n, "hit_rate": _rate(h or 0, n)}
                  for d, n, h in daily],
        "note": "命中率 50% = 抛硬币; 低于 50% 说明方向判断不如随机, 门槛再高也救不了",
    }


# ─── /api/v291/event-move-attribution-pending (待复核区) ───
@app.get("/api/v291/event-move-attribution-pending")
def event_move_attribution_pending_list(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    """待复核区：|move_pct|>=5% 的大异动里，证据不够硬、要等 AI 复核才进主表的行。

    v334 需求背景（需求 #6/#7）：这批行以前只存在于数据库里，归因页上完全看不到 ——
    用户看到的是"大行情没有归因"，却不知道系统其实已经找到了候选新闻，只是卡在
    复核闸门前。这里把它显式暴露出来：有候选新闻、有证据分、有等待原因。

    · why_pending: 为什么没进主表（没 AI 通道 / 证据分不够 / 弱匹配类型）
    · headline: 候选新闻标题（从 news_events 按 ev_news_id 回查），空则说明只匹配到正文
    """
    sql = (
        "SELECT p.id, p.move_id, p.asset, p.move_pct, p.move_kind, p.duration_min, "
        "       p.move_ts, p.algo_score, p.algo_reasons, p.time_lag_sec, "
        "       p.ev_source, p.ev_action, p.ev_news_id, "
        "       n.headline, n.source AS news_source\n"
        "  FROM event_move_attribution_pending p\n"
        "  LEFT JOIN news_events n ON n.id = COALESCE(p.ev_news_id, p.news_event_id)\n"
        " ORDER BY p.move_ts DESC LIMIT ?"
    )
    with db_cursor() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_pending").fetchone()[0]
        big = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_pending "
            "WHERE ABS(COALESCE(move_pct,0)) >= 5").fetchone()[0]
    items = []
    for r in rows:
        it = dict(r)
        score = float(it.get("algo_score") or 0.0)
        match = str(it.get("ev_action") or "").upper()
        if match != "ASSET":
            why = f"匹配类型是 {match or '未知'}（非资产直配），必须 AI 复核后才进主表"
        elif score < 0.35:
            why = f"证据分 {score:.2f} < 0.35（新闻只是碰巧提到这个品种），人工/AI 复核后再定"
        else:
            why = "AI 复核通道离线，大异动暂不入主表"
        it["why_pending"] = why
        items.append(it)
    return {
        "ok": True,
        "n": len(items),
        "pending_total": int(total or 0),
        "pending_big_moves": int(big or 0),
        "items": items,
        "note": "待复核行不计入归因覆盖率; 复核确认后由 attribution_ai_verify.py 迁入主表",
    }


# ─── /api/review/causal-graph (前端 alias → /api/v291/causal-graph) ───
@app.get("/api/review/causal-graph")
async def review_causal_graph_alias(
    asset: str | None = None,
    days: Annotated[int, Query(ge=1, le=3650)] = 30,
    min_confidence: float = 0.0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """前端 causal-graph-panel.tsx line 205 实际调用的端点。
    转发到 /api/v291/causal-graph (完整实现见 v291_causal_graph).
    v291 用 hours/asset 不用 days, 转换 days→hours, 忽略 min_confidence (v291 无此参数).
    """
    # v455: 权威实现已在本进程（routers.causal_graph.get_causal_graph），直调；
    #   原先自转发到 8100 自身形成自循环超时。失败再退回本地 v291 图。
    try:
        from routers.causal_graph import get_causal_graph
        return await get_causal_graph(asset=asset)
    except Exception as exc:
        print(f"[bridge] review/causal-graph 权威实现失败, 退回 v291: {exc}", file=sys.stderr)
    hours = (days or 30) * 24
    return v291_causal_graph(asset=asset or "BTC", hours=hours, limit=limit)


# ─── /api/v189/causal/accuracy-stats (前端 alias → /api/v291/causal-chains) ───
@app.get("/api/v189/causal/accuracy-stats")
def v189_causal_accuracy_stats_alias(
    asset: str | None = None,
    days: Annotated[int, Query(ge=1, le=3650)] = 30,
):
    """前端 causal-graph-panel.tsx line 792 实际调用的端点。
    转发到 /api/v291/causal-chains, 同时加 accuracy 聚合 (按 hit_rate 排序).
    """
    # 注: days 参数此 alias 不完全等价 (v291 没有 days 过滤), 但 causal_chains 是
    # 静态资产→链路映射, 不依赖时间窗口, 影响可忽略。
    res = v291_cross_asset_chains(active_only=True, asset=asset)
    chains = res.get("chains", [])
    # 补上 accuracy 字段别名, 兼容前端旧 schema
    for c in chains:
        h, m_ = c.get("hit_count", 0) or 0, c.get("miss_count", 0) or 0
        n = h + m_
        c["accuracy"] = round(h / n, 3) if n > 0 else None
        c["sample_size"] = n
    # v302+ R2 Round 5: 补 AccuracyResp 期望的字段 (前端 CausalAccuracyWidget 依赖)
    # 不补这些字段会导致 .coverage / .avg_weight_chain / .low_confidence_sample 运行时崩
    chains_with_samples = [c for c in chains if c.get("sample_size", 0) > 0]
    n_truth_insights = sum(1 for c in chains if c.get("sample_size", 0) > 0)
    n_causal_chains = len(chains)
    if chains_with_samples:
        avg_w = sum(c.get("weight", 0) or 0 for c in chains_with_samples) / len(chains_with_samples)
        low_conf = [c for c in chains_with_samples if (c.get("weight", 0) or 0) < 0.4]
        coverage = len(chains_with_samples) / max(1, n_causal_chains)
    else:
        avg_w = 0.0
        low_conf = []
        coverage = 0.0
    res.update({
        "asset": asset or "",
        "days": days,
        "n_truth_insights": n_truth_insights,
        "n_causal_chains": n_causal_chains,
        "coverage": round(coverage, 3),
        "avg_weight_chain": round(avg_w, 3),
        "low_confidence_chain_count": len(low_conf),
        "low_confidence_sample": [
            {
                "id": c.get("id", 0),
                "name": c.get("name", ""),
                "cause": c.get("cause", ""),
                "effect": c.get("effect", ""),
                "weight": c.get("weight", 0) or 0,
                "origin": c.get("origin", ""),
                "trigger_words": c.get("trigger_words", ""),
            }
            for c in low_conf[:5]
        ],
        "origin_breakdown_chain": {},
        "origin_breakdown_truth": {},
        "trust_breakdown": {
            "trusted": n_truth_insights,
            "pending": n_causal_chains - n_truth_insights,
        },
        "generated_at": res.get("ts", ""),
    })
    res["_aliased_from"] = "/api/v291/causal-chains"
    res["_alias_version"] = "v189→v291 (v292+ 活图谱 hit/miss 已激活)"
    return res


# ─── /api/v291/causal-chains/feedback (v302+ P0 因果图谱反馈闭环) ───
@app.get("/api/v291/causal-chains/feedback")
def v291_causal_chains_feedback(
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
    auto_run: bool = True,
):
    """因果图谱逐次事件反馈汇总 — 需求 8 核心:
    排除干扰 → 判断因果链对/错 → 纠正 → confidence 校准

    自动流程 (auto_run=True):
      1. evaluate_pending: 把已到期 (eval_deadline_ts<=now) 的 PENDING outcome 评估
         (查 ohlcv_daily T+5 真实涨跌, 写 status=HIT/MISS/NEUTRAL, 同步 causal_chains.hit/miss)
      2. 返回 feedback summary: 每条 chain 的 recent_outcomes + 校准差距

    返回: {window_days, summary, chains: [{chain_id, asset, stated_confidence,
            real_hit_rate, calibration_gap, needs_recalibration, recent_outcomes: [...]}]}
    """
    try:
        from causal_chain_evaluator import (
            evaluate_pending, backfill, get_feedback_summary
        )
    except ImportError:
        return {"ok": False, "error": "causal_chain_evaluator not found", "chains": []}
    evaluator_info = {"auto_run": auto_run}
    # 注意: evaluate_pending 会写 causal_chain_outcomes, 必须在 write_db_cursor 里跑,
    # 否则 db_cursor() 的 PRAGMA query_only=ON 会直接抛 "attempt to write a readonly database".
    if auto_run:
        with write_db_cursor() as wconn:
            evaluator_info["evaluated"] = evaluate_pending(wconn)
    with db_cursor() as conn:
        # 第二步: 汇总
        result = get_feedback_summary(conn, days=days)
    return {
        "ok": True,
        **result,
        "evaluator": evaluator_info,
        "data_source": "causal_chain_outcomes (v302+ 反馈闭环)",
    }


# ─── /api/v291/causal-chains/feedback/backfill (手动回填) ───
@app.post("/api/v291/causal-chains/feedback/backfill")
def v291_causal_chains_feedback_backfill(payload: dict | None = None):
    """手动回填过去 N 天的因果链 outcome."""
    payload = payload or {}
    days = int(payload.get("days", 90))
    try:
        from causal_chain_evaluator import backfill
    except ImportError:
        return {"ok": False, "error": "causal_chain_evaluator not found"}
    with write_db_cursor() as conn:
        result = backfill(conn, days=days)
    return {"ok": True, **result, "data_source": "causal_chain_evaluator.backfill"}


# ─── /api/v291/causal-chains/evaluate-now (手动触发 evaluator) ───
@app.post("/api/v291/causal-chains/evaluate-now")
def v291_causal_chains_evaluate_now(payload: dict | None = None):
    """立即跑一次 evaluate_pending (不依赖 6h 周期 worker).

    v302+ 支持 payload:
      {"force": true, "max_age_days": 30}
      force=True        → 忽略 deadline,评估所有 PENDING
      max_age_days=int  → 仅评估最近 N 天内 PENDING (默认不限制)
    """
    try:
        from causal_chain_evaluator import evaluate_pending
    except ImportError:
        return {"ok": False, "error": "causal_chain_evaluator not found"}
    payload = payload or {}
    force = bool(payload.get("force", False))
    max_age_days = payload.get("max_age_days")
    with write_db_cursor() as conn:
        result = evaluate_pending(conn, force=force, max_age_days=max_age_days)
    return {"ok": True, **result, "data_source": "causal_chain_evaluator.evaluate_pending"}


# ─── P1-B: /api/v291/market-regime (市场基本面 regime 检测) ───
MARKET_REGIME_TIMEOUT_S = 20.0   # 服务端硬超时 (前端 30s 先到, 这里必须更早放弃)
# v2026-10-27 (G17): 外部源 (Binance 资金费率 / CFTC) 偶尔不可达 → detect_regimes
# 跑不完 → 面板整块空白。这里加一层"最近一次成功结果"的缓存: 计算超时时返回
# 缓存 (stale=true + as_of 时间戳), 而不是把整个面板打成 degraded 空数据。
MARKET_REGIME_CACHE_MAX_AGE_S = 3600   # 缓存最多可用 1 小时


def _market_regime_cache_table(conn) -> None:
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS market_regime_cache ("
            "id INTEGER PRIMARY KEY CHECK (id=1),"
            "payload TEXT NOT NULL, computed_at INTEGER NOT NULL)"
        )
    except sqlite3.OperationalError:
        pass


def _market_regime_cache_load(conn) -> Optional[dict]:
    try:
        _market_regime_cache_table(conn)
        row = conn.execute(
            "SELECT payload, computed_at FROM market_regime_cache WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    age = int(time.time()) - int(row[1] or 0)
    if age > MARKET_REGIME_CACHE_MAX_AGE_S:
        return None
    data["stale"] = True
    data["as_of"] = int(row[1] or 0)
    data["cache_age_sec"] = age
    data["note"] = ("外部行情源不可达, 本次返回 %d 秒前的最近一次成功计算结果"
                    % age)
    return data


def _market_regime_cache_store(conn, payload: dict) -> None:
    try:
        _market_regime_cache_table(conn)
        conn.execute(
            "INSERT INTO market_regime_cache (id, payload, computed_at) VALUES (1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
            "computed_at=excluded.computed_at",
            (json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError as _silent_exc_612:
        print("[bridge_server] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                  % (type(_silent_exc_612).__name__, str(_silent_exc_612)[:120]), flush=True)


def _local_only_regimes() -> list:
    """**纯本地**的 regime 检测器 (只读 DB, 不碰外网) —— 毫秒级。

    为什么需要 (v321.3 P2):
      `detect_regimes` 里混着需要外网的检测器 (Binance 资金费率 / COT 拉取等)。
      外网不可达时整个函数**超时** → 端点回退到旧缓存 → **缓存里没有新加的
      A股 ETF regime** (缓存是它接入之前算的), 于是这个信号**永远出不来**。

      但 ETF 资金流检测是**纯 DB 查询** (macro_snapshots), 毫秒级返回,
      根本不该被外网拖死。所以: 超时路径上**单独补算**本地检测器,
      保证"能算的就算", 而不是"全盘皆输"。

    失败必须**静默降级** (返回 []) —— 补算是加分项, 不能反过来把兜底路径搞崩。
    """
    out: list = []
    try:
        from market import market_regime as _mr
        with db_cursor() as conn:
            out.extend(_mr._detect_cn_state_fund_flow(conn))
    except Exception as exc:
        print(f"[bridge_server] 本地 regime 补算跳过: "
              f"{type(exc).__name__}: {str(exc)[:100]}", flush=True)
        return []
    return out


def _merge_local_regimes(payload: dict) -> dict:
    """把本地补算的 regime 并进 payload (含 regime_counts / n_regimes 重算)。"""
    try:
        local = _local_only_regimes()
    except Exception:
        return payload
    if not local:
        return payload
    regimes = list(payload.get("regimes") or [])
    have = {(r.get("regime"), r.get("asset")) for r in regimes
            if isinstance(r, dict)}
    for r in local:
        key = (r.get("regime"), r.get("asset"))
        if key in have:
            continue
        regimes.append(r)
        have.add(key)
    payload["regimes"] = regimes
    counts: dict = {}
    for r in regimes:
        name = str(r.get("regime") or "?")
        counts[name] = counts.get(name, 0) + 1
    payload["regime_counts"] = counts
    payload["n_regimes"] = len(regimes)
    return payload


def _detect_regimes_sync(since_date: str) -> dict:
    """detect_regimes 的同步封装 (放到线程池里跑, 不阻塞事件循环)。

    v321.3 P1: 算完**无条件落库**。
    端点用 `asyncio.wait_for(..., timeout=20s)` 超时后, 调用方放弃了等待 ——
    但 run_in_threadpool 里那个**同步线程仍在跑** (wait_for 取消不了它)。
    以前算完不落库 → 兜底缓存永远是空的 → 超时后面板只能空白 (ok=false)。
    现在算完就写进 DB 缓存 → 超时后的下一次请求直接拿到 stale 数据,
    面板不再空白, 也不用重复计算。
    """
    from market.market_regime import detect_regimes
    with db_cursor() as conn:
        result = detect_regimes(conn, since_date=since_date)
    try:
        payload = {k: v for k, v in result.items() if not k.startswith("_")}
        with write_db_cursor() as wconn:
            _market_regime_cache_store(wconn, payload)
    except Exception as _store_exc:
        print(f"[bridge_server] regime cache store skipped: "
              f"{type(_store_exc).__name__}: {str(_store_exc)[:100]}", flush=True)
    return result


@app.get("/api/v291/gate-calibration")
def v291_gate_calibration():
    """闸门分组校准 (需求 #1) —— 每个 (资产, 方向) 上信号的真实判别力。

    回答的是「闸门为什么没有识别力」:
      · 整体拒对率 ≈ 基础概率 (+0.03pp) 看着没用
      · 但按 (资产, 方向) 拆开后差异很大 (同一资产不同方向甚至相反)
      · 每条都带样本量、命中率、基础概率、提升、95% 置信下界、是否显著
    applied=1 的分组才会真正影响 decision_guard 的门槛。
    """
    try:
        with db_cursor() as conn:
            try:
                rows = conn.execute(
                    "SELECT asset, action, n_samples, n_hits, hit_rate, base_rate, "
                    "       lift_pp, avg_abs_move, window_days, computed_at, applied, note "
                    "FROM gate_calibration ORDER BY lift_pp DESC"
                ).fetchall()
            except Exception:
                rows = []
    except Exception:
        rows = []
    items = []
    for r in rows:
        d = dict(r)
        # 95% 置信下界 (与 calibrate_gate 同一公式)
        try:
            import math
            n = int(d.get("n_samples") or 0)
            p0 = float(d.get("base_rate") or 0.5)
            hit = int(d.get("n_hits") or 0) / n if n else 0.0
            se = math.sqrt(max(p0 * (1 - p0), 1e-9) / n) if n else 0.0
            d["lift_ci95_low_pp"] = round((hit - p0) * 100 - 1.96 * se * 100, 3)
            d["significant"] = bool(d["lift_ci95_low_pp"] > 0
                                    or (hit - p0) * 100 + 1.96 * se * 100 < 0)
        except Exception:
            d["lift_ci95_low_pp"] = None
            d["significant"] = None
        items.append(d)
    return {
        "ok": True,
        "n": len(items),
        "items": items,
        "applied_count": sum(1 for x in items if x.get("applied")),
        "note": ("applied=1 的分组才会影响闸门门槛 (只对**统计显著**的分组自动生效; "
                 "其余可用 calibrate_gate.py --force-apply 显式采信)"),
        "data_source": "gate_calibration (由 calibrate_gate.py 用已结算的真实结果算出)",
    }


@app.get("/api/v291/decision-archive")
def v291_decision_archive(refresh: bool = False):
    """决策引用归档 (需求 #5「串联互通」防复发)。

    「重置交易历史」会删 ai_decisions / raw_news / news_events, 但 trade_decisions
    的指针还指着它们 → 死指针 (实测 1998 + 1783 条), 且上游已被物理删除、不可恢复。
    现在重置**前**先把决策的引用 + 语义归档到 decision_ref_archive (该表不参与
    重置), 重置后决策本身仍可追溯, 等上游重建还能自动接回。

    refresh=true → 立刻跑一次重建 (把能对上的引用对回去)
    """
    try:
        from signals import decision_ref_archive as _dra
    except Exception as _imp_exc:
        return {"ok": False, "error": f"{type(_imp_exc).__name__}: {str(_imp_exc)[:120]}"}
    with db_cursor() as conn:
        restored = None
        if refresh:
            try:
                restored = _dra.restore_refs(conn)
            except Exception as _rr_exc:
                print(f"[bridge_server] ⚠️ 引用重建失败（原先静默吞掉）: "
                      f"{type(_rr_exc).__name__}: {str(_rr_exc)[:120]}", flush=True)
                restored = {"error": str(_rr_exc)[:120]}
        stats = _dra.archive_stats(conn)
    return {
        "ok": True,
        "archive": stats,
        "restored": restored,
        "note": ("归档表不参与重置; 重置后决策本身(asset/action/结算结果)仍可追溯, "
                 "上游重建后可用 refresh=true 自动接回引用"),
        "data_source": "decision_ref_archive",
    }


@app.get("/api/v291/market-regime")
async def v291_market_regime(
    days: Annotated[int, Query(ge=1, le=3650)] = 365,
    since_date: str | None = None,
):
    """基于 CFTC COT 数据检测 7 类市场 regime:
       1. leverage_buildup / 2. deleveraging
       3. large_spec_long / 4. large_spec_short
       5. commercial_hedging
       6. risk_off_rotation (cross-asset)
       7. spec_extreme (反转预警)

    v308 P1 (服务端硬超时):
      detect_regimes 内部会拉 Binance 资金费率 (外部网络), 不可达时整条请求
      会拖过前端 30s 超时 → 面板空白。现在超时/异常一律返回结构化降级结果
      (ok=false + degraded=true), 不再 500, 也不再假装有数据。
    """
    from datetime import datetime, timedelta
    try:
        from market import market_regime
    except ImportError:
        return {"ok": False, "error": "market_regime not found"}
    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        result = await asyncio.wait_for(
            run_in_threadpool(_detect_regimes_sync, since_date),
            timeout=MARKET_REGIME_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        cached = None
        try:
            with db_cursor() as conn:
                cached = _market_regime_cache_load(conn)
        except Exception:
            cached = None
        if cached:
            cached["ok"] = True
            cached["degraded"] = False
            # v321.3 P2: 补算**纯本地** regime (ETF 资金流等) ——
            # 它们只读 DB、毫秒级返回, 不该因为外网超时被一起牺牲。
            cached = _merge_local_regimes(cached)
            return cached
        # v321.3 P2: 连缓存都没有时, 也别把**纯本地**的 regime 一起丢掉
        # (ETF 资金流等只读 DB, 毫秒级)。宁可"部分有", 不要"全空"。
        _local = _local_only_regimes()
        _counts: dict = {}
        for _r in _local:
            _n = str(_r.get("regime") or "?")
            _counts[_n] = _counts.get(_n, 0) + 1
        return {
            "ok": False, "degraded": True,
            "error": (f"市场基本面计算超时 ({MARKET_REGIME_TIMEOUT_S:.0f}s), 已放弃本次请求 "
                      f"(通常是外部行情源不可达)"),
            "regimes": _local, "regime_counts": _counts, "by_asset": {},
            "n_regimes": len(_local), "n_assets": 0,
            "funding": {"ok": False, "checked": [], "skipped": [],
                        "stale": [], "note": "整体超时, 资金面未参与判定"},
            "data_source": "market_regime.detect_regimes",
        }
    except Exception as exc:
        cached = None
        try:
            with db_cursor() as conn:
                cached = _market_regime_cache_load(conn)
        except Exception:
            cached = None
        if cached:
            cached["ok"] = True
            cached["degraded"] = False
            # v321.3 P2: 同上 —— 异常路径也补算纯本地 regime
            cached = _merge_local_regimes(cached)
            return cached
        return {
            "ok": False, "degraded": True,
            "error": f"市场基本面计算失败: {type(exc).__name__}: {exc}",
            "regimes": [], "regime_counts": {}, "by_asset": {},
            "n_regimes": 0, "n_assets": 0,
            "funding": {"ok": False, "checked": [], "skipped": [],
                        "stale": [], "note": "计算失败, 资金面未参与判定"},
            "data_source": "market_regime.detect_regimes",
        }
    # 加一个 regime 计数摘要
    counts: dict = {}
    for r in result.get("regimes", []):
        counts[r["regime"]] = counts.get(r["regime"], 0) + 1
    result["regime_counts"] = counts
    # v308 P1-bugfix: `{"ok": True, **result}` 的展开顺序是错的 ——
    # result 里若带 ok=False (例如 COT 无数据 → detect_regimes 返回
    # {"ok": False, "error": "no_cot_data"}), 会**覆盖**掉前面写死的 ok=True,
    # 让前端以为成功。改成显式取值, 保证 ok 只由 detect_regimes 决定。
    result["ok"] = bool(result.get("ok"))
    result["data_source"] = "market_regime.detect_regimes"
    # v2026-10-27 (G17): 成功结果落缓存 (外部源不可达时兜底返回)
    if result.get("ok"):
        try:
            with db_cursor() as conn:
                _market_regime_cache_store(conn, result)
        except Exception:
            pass
    return result


# ─── P1-C: /api/v291/orderbook-depth (挂单量数据源) ───
@app.get("/api/v291/orderbook-depth")
def v291_orderbook_depth(symbol: str = "BTCUSDT", refresh: bool = False):
    """从 Binance 公共 API 拉取挂单量 + 7 类指标 (60s in-memory 缓存).
       支持: BTCUSDT, ETHUSDT
    """
    try:
        from orderbook_depth import fetch_orderbook_metrics, SUPPORTED_SYMBOLS
    except ImportError:
        return {"ok": False, "error": "orderbook_depth not found"}
    metrics = fetch_orderbook_metrics(symbol, force_refresh=refresh)
    if metrics.get("ok"):
        return {"ok": True, **metrics,
                "supported": list(SUPPORTED_SYMBOLS),
                "data_source": "orderbook_depth.fetch_orderbook_metrics"}
    return {"ok": False, **metrics, "supported": list(SUPPORTED_SYMBOLS)}


# ─── P1-D: /api/v291/funding-rate (Binance 永续合约资金费率) ───
@app.get("/api/v291/funding-rate")
def v291_funding_rate(symbol: str = "BTCUSDT", refresh: bool = False, all_symbols: bool = False):
    """从 Binance Futures 拉取最近 90 笔 8h funding rate + 衍生指标.

    支持: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT
    all_symbols=true 时返回 4 标的的精简摘要 (用于市场状态面板联动).
    """
    try:
        from funding_rate import get_funding_metrics, get_all_funding, SUPPORTED_SYMBOLS
    except ImportError:
        return {"ok": False, "error": "funding_rate module not found"}

    if all_symbols:
        all_data = get_all_funding()
        # 精简版: 无 history
        summary = {}
        for sym, m in all_data.items():
            if m.get("ok"):
                summary[sym] = {k: v for k, v in m.items() if k != "history"}
            else:
                summary[sym] = m
        # 关键全局信号
        extremes_long = [s for s, m in all_data.items()
                        if m.get("ok") and m.get("signal") in ("long_pay", "long_pay_extreme")]
        extremes_short = [s for s, m in all_data.items()
                         if m.get("ok") and m.get("signal") in ("short_pay", "short_pay_extreme")]
        return {
            "ok": True,
            "all_symbols": summary,
            "supported": list(SUPPORTED_SYMBOLS),
            "n_extreme_long_pay": len(extremes_long),
            "n_extreme_short_pay": len(extremes_short),
            "extreme_long_pay_symbols": extremes_long,
            "extreme_short_pay_symbols": extremes_short,
            "data_source": "funding_rate.get_all_funding"
        }

    metrics = get_funding_metrics(symbol, force_refresh=refresh)
    if metrics.get("ok"):
        return {"ok": True, **metrics,
                "supported": list(SUPPORTED_SYMBOLS),
                "data_source": "funding_rate.get_funding_metrics"}
    return {"ok": False, **metrics, "supported": list(SUPPORTED_SYMBOLS)}


def _resolve_history_window(
    hours: Optional[int],
    from_ts: Optional[int],
    to_ts: Optional[int],
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """解析历史回溯的时间窗 → (hours, from_ts, to_ts), 供 history_review 调用。

    v309 P1: 前端 PeriodSelector 的「自定义」档走 periodToQuery(), 只发
    from_ts/to_ts **不发 hours**。若这里把 hours 兜底成 72, 自定义时段就被
    静默忽略 (返回近 72h 而非用户选的区间)。

    口径与 history_review.count_pending_news 一致 (它就是 hours=None 起手):
      · 显式给了 hours → 用 hours (模块内"hours 与 from/to 同时存在时 hours
        生效"的兼容约定);
      · 只给了 from_ts/to_ts → 用绝对区间 (hours 传 None);
      · 都没给 → 兜底 72h (与两个端点历史默认一致)。
    """
    if from_ts is not None and to_ts is not None and to_ts > from_ts:
        if hours is None:
            return None, int(from_ts), int(to_ts)
    if hours is None:
        hours = 72
    return int(hours), None, None


# ─── /api/history/news-trades (前端 history/page.tsx line 156 调用) ───
# v309 P1 (修 8100 端空列表 / 500):
#
# 旧实现 (v292+) 走的是 **news_events × trade_decisions** 这条路, 有三处硬伤:
#   ① 时间窗比较把 TEXT 列 created_at 与 epoch 整数直接比
#      (`created_at BETWEEN <epoch>`) —— SQLite 类型亲和性把整数当文本比, 恒假
#      → 一行 trade 都匹配不上 → 列表永远返回 [] (前端 8100 端一直空);
#      同一处 `int(t["created_at"])` 在真命中时也会直接抛 ValueError。
#   ② 详情端点 SELECT 了 trade_decisions 里**不存在的列** (action/score/
#      confidence/price_at_decision/evidence_json/rationale) → sqlite3
#      OperationalError → HTTP 500 (前端点新闻详情直接报错)。
#   ③ 数据源选错: 该端点的语义是"已结束的**模拟交易**按新闻归类"
#      (前端 TradeRow 用的是 pnl_pct/entry_ts/exit_ts/verdict/interval_minutes
#      这一套字段), 权威实现是 history_review.py (quick_sim_trades ⋈ ai_decisions
#      ⋈ raw_news), 8000 端口走的就是它。
#
# 现在 8100 直接复用与 8000 完全相同的权威实现 (history_review.news_trade_list),
# 保证两个端口返回**逐字段一致**的 shape, 前端无需区分端口。
@app.get("/api/history/news-trades")
def history_news_trades(
    hours: Optional[int] = Query(None, ge=1, le=8760),
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
    from_ts: Optional[int] = Query(None, ge=0),
    to_ts: Optional[int] = Query(None, ge=0),
):
    """历史新闻交易回溯列表 (已结束交易的新闻, 按最近结算时间倒序)。

    数据源与 8000 一致: history_review.news_trade_list
    (quick_sim_trades settled=1 ⋈ raw_news, 时间窗用 exit_ts epoch)。

    from_ts/to_ts: 自定义时段 (UTC 秒, 含两端), 与 hours 互斥 —— 前端
    PeriodSelector 选「自定义」时只发这两个参数 (api_server.py:11090 同样)。
    """
    try:
        import history_review
    except Exception as exc:  # pragma: no cover - 降级保护
        print(f"[bridge] history_review 导入失败: {exc}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=503, detail="history_review module unavailable")
    _h, _f, _t = _resolve_history_window(hours, from_ts, to_ts)
    return history_review.news_trade_list(hours=_h, limit=limit, from_ts=_f, to_ts=_t)


@app.get("/api/history/news-trades/pending-count")
def history_news_trade_pending_count(
    hours: Optional[int] = Query(None, ge=1, le=8760),
    from_ts: Optional[int] = Query(None, ge=0),
    to_ts: Optional[int] = Query(None, ge=0),
):
    """窗口内待判定交易数 (前端 review-history-panel 顶部徽章)。

    v309 P1: 必须注册在 /{news_id} **之前** —— Starlette 按注册顺序匹配,
    否则 "pending-count" 会被 {news_id} 捕获 → 422 (前端拿到的是 422 不是数据)。
    """
    try:
        import history_review
    except Exception as exc:  # pragma: no cover - 降级保护
        print(f"[bridge] history_review 导入失败: {exc}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=503, detail="history_review module unavailable")
    _h, _f, _t = _resolve_history_window(hours, from_ts, to_ts)
    return history_review.count_pending_news(hours=_h, from_ts=_f, to_ts=_t)


@app.get("/api/history/news-trades/{news_id}")
def history_news_trade_detail(news_id: int):
    """单条新闻的交易明细 + AI 归因 (前端 history/page.tsx line 189 调用)。

    v309 P1 (修 HTTP 500): 旧实现 SELECT 了 trade_decisions 里不存在的列
    (action/score/confidence/price_at_decision/evidence_json/rationale) →
    sqlite3.OperationalError → 500; 且新闻 id 空间是 news_events.news_id,
    与列表返回的 news_id 不一致。

    现在与 8000 走同一个权威实现 (history_review.news_trade_detail):
      · news_id = raw_news.id (与列表项 news_id 同一 id 空间);
      · 交易明细来自 quick_sim_trades ⋈ ai_decisions, 字段与前端 TradeRow 对齐;
      · 找不到时抛 404 (前端 `if (!r.ok) throw` 走错误分支, 与 8000 一致)。
    """
    try:
        import history_review
    except Exception as exc:  # pragma: no cover - 降级保护
        print(f"[bridge] history_review 导入失败: {exc}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=503, detail="history_review module unavailable")
    detail = history_review.news_trade_detail(news_id)
    if not detail:
        raise HTTPException(status_code=404, detail="news not found")
    return detail


@app.post("/api/history/news-trades/{news_id}/review")
def history_news_trade_review_post(news_id: int):
    """POST review 生成: v292+ 占位 (LLM review 暂未实现, 返 ok + 空 summary).

    真实实现需 news_reviews 表 + LLM 调用; 当前返 ok=True 避免前端 review 调用崩。
    """
    return {
        "ok": True,
        "summary": None,
        "generated_at": None,
        "_note": "v292+ news review LLM 暂未接入, summary=None 触发前端 '归因未生成' 渲染",
    }


# ─── v302+ P0: 补 catch-all 端点, 修前端 0 数据问题 ────────


@app.get("/api/v237/missed-signals")
def v237_missed_signals(
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
):
    """v237 漏检信号主列表 (前端 MissedSignalsPanel default tab 消费).
    v302+ 实现: 从 critical_price_moves 强信号 (algo>=0.7) 实时生成 MissedRow 形状.
    漏检定义: 强信号出现时 ±24h 内无 trade_decision 记录.

    v309 P1 (修 "所有强信号都被判成漏检" 的判定 bug):
      旧代码用 `ABS(created_at - ?) <= 86400` 找同期决策, 但 trade_decisions
      的现行时间列是 decision_ts (REAL epoch), created_at 是 TEXT
      ('2026-10-25 00:36:12') —— SQLite 做减法时把该串解析成前导数字 '2026',
      差值恒为巨大值 → **永远匹配不到** → 每个强信号都输出成"漏检",
      且每行还发一次 SQL (N+1 查询)。
      现在改用 _load_decision_ts_by_asset() 预载 decision_ts 并在内存里做
      ±24h 匹配 (与 by-dimension 端点同一口径), 顺带消除 N+1。
    """
    cutoff = int(time.time()) - days * 86400
    with db_cursor() as conn:
        has_dec = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_decisions'"
        ).fetchone() is not None
        rows = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min, created_at "
            "FROM critical_price_moves "
            "WHERE algo_score >= 0.7 AND ts >= ? "
            # v308 P1-fix3: 量纲污染行 (|move_pct|>90%) 不是市场异动, 必须剔除 ——
            # 否则这里会把 9867% 的假异动当成"强信号"渲染进 missed-signals 列表。
            "AND ABS(COALESCE(move_pct,0)) <= ? "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT, limit),
        ).fetchall()
        # 决策时间按资产预载一次 (epoch), 供 ±24h 内存匹配
        dec_by_asset = _load_decision_ts_by_asset(conn, cutoff - 86400) if has_dec else {}
        items: list[dict] = []
        for r in rows:
            if has_dec:
                _asset = (r["asset"] or "").upper()
                _dec_ts = dec_by_asset.get(_asset) or []
                td_hit = next(
                    (_dt for _dt in _dec_ts if abs(_dt - int(r["ts"])) <= 86400), None
                )
                if td_hit is not None:
                    continue  # 同期有决策覆盖 → 不是漏检
                ignored = (
                    f"强信号出现时, 同期 (±24h) 无 trade_decision 记录覆盖 — "
                    f"决策系统未识别该信号 (asset={r['asset']})"
                )
            else:
                ignored = "DB 中无 trade_decisions 表 — 上游决策源未接入, 所有强信号视为漏检"
            ptype = "A1_strong_buy_missed" if r["move_pct"] > 0 else "A2_strong_sell_missed"
            if (r["duration_min"] or 0) >= 4320:
                ptype = "A3_continuation_missed"
            items.append({
                "id": r["id"],
                "asset": r["asset"],
                # v309 P1: ts / created_at 都是真 epoch, 按项目口径渲染成**北京墙钟**
                # (旧代码用 gmtime → UTC, 比界面其它地方的时间早 8h)。
                "decision_date": time_utils.epoch_to_bj_str(int(r["ts"]))[:10],
                "decision_id": None,
                "signal_id": r["id"],
                "pattern_type": ptype,
                "signal": f"algo_score={r['algo_score']:.2f} move_pct={r['move_pct']:.2f} move_kind={r['move_kind']}",
                "ignored_reason": ignored,
                "confidence_at_time": r["algo_score"],
                "actual_outcome_5d": None,
                "actual_outcome_20d": None,
                "hit": None,
                "created_at": time_utils.epoch_to_bj_store(int(r["created_at"])),
            })
    return {
        "ok": True,
        "rows": items,
        "count": len(items),
        "limit": limit,
        "days": days,
        "data_source": "critical_price_moves × trade_decisions (v302+ 实时生成)",
    }


@app.get("/api/v237/judgment-quality")
def v237_judgment_quality(weeks: Annotated[int, Query(ge=1, le=520)] = 12):
    """v237 判断质量 (前端 quality tab 消费).
    v302+ 实现: 基于 trade_reflections_v2 (521 rows) 实时聚合.
    """
    cutoff = int(time.time()) - weeks * 7 * 86400
    # v309 P1: created_at 是 TEXT, 历史上有两种落库格式 (T 分隔=本机本地时间 /
    # 空格分隔=UTC) → 裸比较会漏行; 统一用 sql_bj_ts_expr() 归一化成北京墙钟再比。
    # cutoff 用北京时区换算 (项目约定: 裸时间串 = 北京时间)。
    cutoff_str = time_utils.epoch_to_bj_str(cutoff)
    ts_expr = time_utils.sql_bj_ts_expr("created_at")
    with db_cursor() as conn:
        reflections = conn.execute(
            "SELECT asset, dimension, subcategory, confidence_at_time, hit, actual_outcome_pct "
            f"FROM trade_reflections_v2 WHERE {ts_expr} >= ?",
            (cutoff_str,),
        ).fetchall()
    by_pattern: dict = {}
    n_total = 0
    n_hit = 0
    conf_sum = 0.0
    gap_sum = 0.0
    for r in reflections:
        n_total += 1
        if r["hit"]:
            n_hit += 1
        conf = r["confidence_at_time"] or 0
        conf_sum += conf
        actual = abs(r["actual_outcome_pct"] or 0)
        gap_sum += conf - min(1.0, actual / 10.0)
        key = f"{r['dimension']}|{r['subcategory']}"
        d = by_pattern.setdefault(key, {
            "pattern_type": key, "dimension": r["dimension"],
            "subcategory": r["subcategory"],
            "n": 0, "n_hit": 0, "hit_rate": 0.0,
            "avg_confidence": 0.0, "confidence_calibration": 0.0, "avg_edge_5d": 0.0,
        })
        d["n"] += 1
        if r["hit"]:
            d["n_hit"] += 1
        d["avg_confidence"] += conf
        d["avg_edge_5d"] += abs(r["actual_outcome_pct"] or 0)
    by_pattern_list = []
    for d in by_pattern.values():
        d["hit_rate"] = round(d["n_hit"] / d["n"], 3) if d["n"] else 0
        d["avg_confidence"] = round(d["avg_confidence"] / d["n"], 3) if d["n"] else 0
        d["avg_edge_5d"] = round(d["avg_edge_5d"] / d["n"], 2) if d["n"] else 0
        d["confidence_calibration"] = round(d["avg_confidence"] - d["hit_rate"], 3)
        by_pattern_list.append(d)
    by_pattern_list.sort(key=lambda x: -x["n"])
    overall = {
        "n": n_total, "n_hit": n_hit,
        "hit_rate": round(n_hit / n_total, 3) if n_total else 0,
        "avg_confidence": round(conf_sum / n_total, 3) if n_total else 0,
        "overconfidence_gap": round(gap_sum / n_total, 3) if n_total else 0,
    }
    return {
        "ok": True,
        "by_pattern": by_pattern_list,
        "overall": overall,
        "weeks": weeks,
        "n_reflections": n_total,
        "data_source": "trade_reflections_v2 (v302+ 实时聚合)",
    }


@app.get("/api/v291/critical-moves")
def v291_critical_moves(
    hours: Annotated[int, Query(ge=1, le=8760)] = 720,
    limit: Annotated[int, Query(ge=1, le=1000)] = 40,
    asset: str | None = None,
):
    """v291 关键点列表 (前端 EventMarketAttribution 顶部表消费).
    v302+ 实现: 从 critical_price_moves 直读, 支持 asset 过滤.
    """
    cutoff = int(time.time()) - hours * 3600
    # v308 P1-fix3: 量纲污染行 (|move_pct|>90%) 不是市场异动 → SQL 层直接剔除。
    # where 列表与 params 列表必须**同序**, 过滤条件插在 ts 之后、asset 之前。
    where = ["ts >= ?", "ABS(COALESCE(move_pct,0)) <= ?"]
    params: list[Any] = [cutoff, MAX_PLAUSIBLE_MOVE_PCT]
    if asset:
        where.append("asset=?")
        params.append(asset.upper())
    sql = (
        "SELECT id, asset, ts, start_price, peak_price, end_price, move_pct, "
        "move_kind, duration_min, algo_score, context_json, created_at "
        "FROM critical_price_moves WHERE " + " AND ".join(where) +
        " ORDER BY ts DESC LIMIT ?"
    )
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        # total 必须与 items 用**完全相同**的过滤 (含 asset) —— 否则界面上的
        # "共 N 条"会和下面的列表对不上 (旧代码 total 不带 asset 过滤)。
        # params[:-1] 去掉末尾的 limit。
        total = conn.execute(
            "SELECT COUNT(*) FROM critical_price_moves WHERE " + " AND ".join(where),
            params[:-1],
        ).fetchone()[0]
    items = []
    for r in rows:
        ctx_raw = r["context_json"]
        try:
            ctx = json.loads(ctx_raw) if ctx_raw else {}
        except Exception:
            ctx = {}
        items.append({
            "id": r["id"], "asset": r["asset"], "ts": r["ts"],
            "start_price": r["start_price"], "peak_price": r["peak_price"],
            "end_price": r["end_price"], "move_pct": r["move_pct"],
            "move_kind": r["move_kind"], "duration_min": r["duration_min"],
            "algo_score": r["algo_score"], "context": ctx,
            "created_at": r["created_at"],
        })
    return {
        "ok": True,
        "items": items,
        # v303: rows alias — 兼容前端 .rows ?? [] 历史契约 (Q4 改 event-market-attribution 后 8100 fallback 才发现)
        "rows": items,
        "n": len(items),
        "count": len(items),
        "total": total,
        "hours": hours,
        "asset": asset,
        "data_source": "critical_price_moves (v302+ 直读)",
    }


@app.get("/api/v291/stats")
def v291_stats(hours: Annotated[int, Query(ge=1, le=8760)] = 720):
    """v291 全局 stats (前端 EventMarketAttribution 头牌消费).
    v302+ 实现: critical_moves + event_move_attribution + causal_chains 聚合.
    """
    cutoff = int(time.time()) - hours * 3600
    with db_cursor() as conn:
        # v308 P1-fix3: 这两个计数直接显示在头牌卡片上, 污染行会让它们虚高 →
        # 与 items 一样套量纲过滤, 保证"数字"和"明细"口径一致。
        n_moves = conn.execute(
            "SELECT COUNT(*) FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchone()[0]
        n_attr = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE move_ts >= ? AND ABS(COALESCE(move_pct,0)) <= ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchone()[0]
        n_chains = conn.execute(
            "SELECT COUNT(*) FROM causal_chains WHERE active=1"
        ).fetchone()[0]
        n_out_total = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes"
        ).fetchone()[0]
        n_out_pend = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='PENDING'"
        ).fetchone()[0]
        n_out_hit = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='HIT'"
        ).fetchone()[0]
        n_out_weak = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='HIT_WEAK'"
        ).fetchone()[0]
        by_kind = conn.execute(
            "SELECT move_kind, COUNT(*) n FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ? GROUP BY move_kind",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchall()
    return {
        "ok": True,
        "hours": hours,
        "n_critical_moves": n_moves,
        "n_event_attributions": n_attr,
        "n_chains_active": n_chains,
        "n_outcomes_total": n_out_total,
        "n_outcomes_pending": n_out_pend,
        "n_outcomes_hit": n_out_hit,
        "n_outcomes_hit_weak": n_out_weak,
        "by_move_kind": {r["move_kind"]: r["n"] for r in by_kind},
        "data_source": "critical_price_moves + event_move_attribution + causal_chain_outcomes",
    }


@app.get("/api/v291/move-attributions")
def v291_move_attributions(
    move_id: int | None = None,
    asset: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
):
    """v291 事件-行情归因列表 (前端 EventMarketAttribution 弹窗消费).
    v302+ 实现: alias of /api/v291/event-move-attribution.
    """
    # v308 P1-fix3: 派生表 event_move_attribution 的 move_pct 是从源表直接拷贝的,
    # 源表过滤了这里也要过滤 —— 否则污染行照样漏到前端。
    # 注意 where / params 必须同序, 过滤条件放在最前。
    where: list[str] = ["ABS(COALESCE(move_pct,0)) <= ?"]
    params: list[Any] = [MAX_PLAUSIBLE_MOVE_PCT]
    if move_id is not None:
        where.append("move_id=?")
        params.append(move_id)
    if asset:
        where.append("asset=?")
        params.append(asset.upper())
    sql = (
        "SELECT id, move_id, news_event_id, asset, time_lag_sec, algo_score, algo_reasons, "
        "move_ts, move_pct, move_kind, duration_min, ev_source, ev_action, ev_ts, ev_news_id "
        "FROM event_move_attribution "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY move_ts DESC LIMIT ?"
    )
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        # total 与 items 用**完全相同**的过滤 (含 move_id/asset) —— 否则
        # "共 N 条"和下面的列表对不上 (旧代码 total 是整表计数, 完全没过滤)。
        # params[:-1] 去掉末尾的 limit。
        total = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution WHERE " + " AND ".join(where),
            params[:-1],
        ).fetchone()[0]
    return {
        "ok": True,
        "items": [dict(r) for r in rows],
        # v303: rows alias — 兼容前端 .rows ?? [] 历史契约 (Q4 改 event-market-attribution 后 8100 fallback 才发现)
        "rows": [dict(r) for r in rows],
        "n": len(rows),
        "count": len(rows),
        "total": total,
        "data_source": "event_move_attribution (v302+ 新增 alias /move-attributions)",
    }


# ohlcv_daily 是日线 → timeframe 映射成"回看几根 bar"


# ─── 新闻数据源 schema 适配 ─────────────────────────────
# 两个版本的新闻落在**不同的表/列**, 直接按 trident 结构查会抛
# `sqlite3.OperationalError: no such column: headline` → /api/market/movers 整个 500,
# 连带把已经算好的异动点/技术指标一起丢掉 (异动点逻辑本身没问题)。
#   trident : news_events(id, source, headline, body, action, score, assets, ts)
#   金融版  : news_events 是"决策关联新闻"(有 decision_id/is_noise/status, 无标题列),
#             真实新闻文本在 raw_news.content, 资产标注在 raw_news.gate_assets。
# 按实际列自动选源: 不改任何表结构, 不编造新闻; 无可用源时返回空列表, 前端不显示归因。
_HASH_PREFIX = re.compile(r"^\[hash:[0-9a-f]{4,}\]\s*", re.I)


def _strip_hash_prefix(text: Any) -> str:
    """去掉 raw_news.content 前面的内部指纹标记 [hash:xxxxxxxx]。"""
    return _HASH_PREFIX.sub("", str(text or "").strip())


# bridge 的 movers 每次都要现算（SQL 本身 ~1s），但 bridge 进程里跑着一堆后台刷新线程，
# 同一条查询实测被拖到 10~30s。前端是轮询调用的，120s 内复用同一份结果即可。


# ── v345: /api/market/prices (以前这里根本没有这个路由) ──
# 前端 hooks/use-binance-prices.ts 每 3s 打一次这个接口，而 Next 的
# /api/* 默认反代到 8100。8100 上没有它 → 404 / 卡住 → 五个报价
# (BTC/ETH/SOL/XAU/WTI) 永远停在「加载中」，用户看到的就是"行情没在跳"。
# 这里用**库里已有的 K 线**直接算：不走任何外网，50ms 内返回。
_PRICES_CACHE: dict = {"t": 0.0, "v": None}
_PRICE_SYMBOLS = ("BTC", "ETH", "SOL", "XAU", "WTI")


@app.get("/api/market/prices")
def market_prices():
    now = time.time()
    hit = _PRICES_CACHE.get("v")
    if hit and (now - _PRICES_CACHE["t"]) < 15.0:
        return hit
    items, newest = [], 0
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        for sym in _PRICE_SYMBOLS:
            # v477 修正: 旧实现用「相邻两根 K 线」的涨跌冒充 24h 涨跌,
            # 五个品种常年只显示 ±0.0x%, 与真实 24h 波动 (BTC 实测 -1.8%) 对不上,
            # 用户看到的就是"行情不反映市场"。同时 sourceCount 被写死为 1。
            # 现在主口径改为 market_ticks 的多源 tick (就是库里已经在跳的那张表):
            #   现价   = 最新 tick
            #   涨跌   = 现价 vs 24h 前最近一条 tick (真实 24h 涨跌幅)
            #   源数量 = 近 30 分钟内出现过的 distinct source
            # 取不到 tick 才退回 K 线, 且退回时按"日线对日线"计算, 不再用相邻分钟线。
            tick = None
            try:
                tick = conn.execute(
                    "SELECT ts, price, source_count FROM market_ticks "
                    "WHERE symbol=? AND price>0 ORDER BY ts DESC LIMIT 1",
                    (sym,)).fetchone()
            except Exception:
                tick = None
            if tick is not None:
                price = float(tick["price"] or 0)
                ts = int(tick["ts"] or 0)
                if price <= 0:
                    continue
                chg = None
                try:
                    past = conn.execute(
                        "SELECT price FROM market_ticks WHERE symbol=? AND price>0 "
                        "AND ts<=? ORDER BY ts DESC LIMIT 1",
                        (sym, ts - 86400)).fetchone()
                    if past and float(past["price"] or 0) > 0:
                        prev = float(past["price"])
                        chg = round((price - prev) / prev * 100.0, 2)
                except Exception:
                    chg = None
                src_n = 0
                try:
                    # v479：只数**独立外部源**。`median` 是自己把各源算中位数写回的行，
                    # `snapshot` / `snapshot-sync` 是聚合快照 —— 它们不是独立信源，
                    # 旧口径把它们算进 sourceCount，界面就出现"3 个源交叉验证"，
                    # 而真实独立源可能只有 1 个（数据可信度被虚抬）。
                    src_n = int(conn.execute(
                        "SELECT COUNT(DISTINCT source) FROM market_ticks "
                        "WHERE symbol=? AND ts>=? AND source IS NOT NULL "
                        "AND lower(source) NOT IN ('median','snapshot','snapshot-sync','agg','derived')",
                        (sym, ts - 1800)).fetchone()[0] or 0)
                except Exception:
                    src_n = 0
                if src_n <= 0:
                    src_n = int(tick["source_count"] or 1)
                newest = max(newest, ts)
                items.append({
                    "asset": sym, "price": price, "change24h": chg,
                    "sourceCount": src_n,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                })
                continue
            # —— 兜底: 无 tick 时用日线, 涨跌幅 = 当日收盘 vs 前一交易日收盘 ——
            daily = []
            intraday = []
            try:
                daily = conn.execute(
                    "SELECT ts, close FROM ohlcv_daily WHERE asset=? ORDER BY ts DESC LIMIT 2",
                    (sym,)).fetchall()
            except Exception:
                daily = []
            try:
                intraday = conn.execute(
                    "SELECT ts, close FROM ohlcv_intraday WHERE asset=? ORDER BY ts DESC LIMIT 2",
                    (sym,)).fetchall()
            except Exception:
                intraday = []
            d_ts = int(daily[0]["ts"] or 0) if daily else 0
            i_ts = int(intraday[0]["ts"] or 0) if intraday else 0
            row = intraday if i_ts >= d_ts else daily
            if not row:
                continue
            price = float(row[0]["close"] or 0)
            if price <= 0:
                continue
            chg = None
            prev_src = daily if d_ts >= i_ts else intraday
            if len(prev_src) >= 2:
                prev = float(prev_src[1]["close"] or 0)
                if prev > 0:
                    chg = round((price - prev) / prev * 100.0, 2)
            ts = int(row[0]["ts"] or 0)
            newest = max(newest, ts)
            items.append({
                "asset": sym, "price": price, "change24h": chg,
                "sourceCount": 1,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            })
        conn.close()
    except Exception as e:
        return {"items": [], "sources": {}, "updated_at": "", "error": str(e)[:80]}
    payload = {
        "items": items,
        "sources": {"db": {"status": "ok" if items else "unavailable"}},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest)) if newest else "",
    }
    _PRICES_CACHE["t"], _PRICES_CACHE["v"] = now, payload
    return payload






# ── v345: movers 预热循环 ────────────────────────────────────────────────
# 症状：/api/market/movers 首次查询实测 4~11s（31 个品种 × 异动+新闻配对），
#   这段时间前端拿不到品种列表 → 归因页停在「还没选品种」，K 线等于空白。
# 做法：后台线程每 90s 把常用周期先算好塞进 _BMOVERS_CACHE（TTL 120s），
#   前端首屏直接命中缓存 → 0 等待；算不动也不会拖累页面（异常只打日志）。








@app.get("/api/market/movers")
def market_movers(
    timeframe: str = "24h",
    limit: Annotated[int, Query(ge=1, le=1000)] = 20,
    full: bool = False,
):
    """行情涨跌排行 (前端 MoversPanel 主数据源)。

    2026-11-18: 实现整块搬到 `bridge_market.market_movers`（缓存 + 预热线程随模块走），
    这里只留签名和转发 —— 路由顺序、参数校验、前端契约都不变。
    """
    from bridge_market import market_movers as _mm

    return _mm(timeframe=timeframe, limit=limit, full=full)


@app.get("/api/tech/signals")
def tech_signals_api(symbol: str = "MU",
                     days: Annotated[int, Query(ge=1, le=3650)] = 90,
                     include_future: bool = True, min_score: float = 0.0,
                    with_decision: bool = True):
    """技术指标异动信号明细 (v303 R12；v457 起附带决策链结论)。

    参数:
      symbol         资产
      days           回看天数
      include_future 是否包含转折点(uses_future=1, 用户许可使用未来函数)
      min_score      分数下限

    返回每条信号的完整命中指标明细, 前端可在异动点上悬浮查看依据。
    没有数据时返回空数组 + ok, **不返回任何编造的占位信号**。

    v457：`with_decision=True` 时每条信号额外给出决策链结论
    （action / horizon / actionable / asset_class / vol_bucket）。
    这些字段**不是**另算一套，而是直接调 `tech_signal_bridge` 的现有函数，
    保证界面看到的和线上会下单的是同一个口径；算不出来就返回 null，不猜。
    """
    sym = (symbol or "").upper().replace("USDT", "").replace("-", "")
    cutoff = int(time.time()) - int(days) * 86400
    sql = (
        "SELECT ts, close, trigger_type, score, uses_future, pct_1d, pct_3d, "
        "vol_ratio, support_level, signal_json, merged_types, "
        "bars_available, data_max_ts "
        "FROM tech_signals WHERE asset=? AND ts >= ?"
    )
    params: list[Any] = [sym, cutoff]
    if not include_future:
        sql += " AND uses_future=0"
    if min_score > 0:
        sql += " AND score >= ?"
        params.append(min_score)
    sql += " ORDER BY ts ASC"

    bridge = None
    allow: dict[str, Any] = {}
    if with_decision:
        try:
            import tech_signal_bridge as bridge  # 延迟导入，避免拖慢服务启动
            allow = bridge.load_actionable()
        except Exception:
            bridge, allow = None, {}
    try:
        import tech_detector as _td  # 评级口径只有一处，前端不自己算
    except Exception:
        _td = None

    with db_cursor() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='tech_signals'").fetchone()
        if not exists:
            return {"ok": False, "asset": sym, "signals": [], "n": 0,
                    "reason": "tech_signals 表不存在, 请先运行 backfill_tech_signals.py"}
        rows = conn.execute(sql, tuple(params)).fetchall()
        max_ts = conn.execute(
            "SELECT MAX(ts) FROM ohlcv_daily WHERE asset=?", (sym,)).fetchone()[0]

        # v457：决策链结论与信号本身在同一个事务里算完，避免两次查询之间数据变动。
        derived: list[dict[str, Any]] = []
        for r in rows:
            if bridge is None:
                derived.append({})
                continue
            try:
                items = json.loads(r["signal_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                items = []
            cls = bridge.asset_class(sym)
            vb = bridge.vol_bucket(conn, sym, int(r["ts"]))
            horizon = bridge.resolve_horizon(items, allow, cls=cls, vol=vb) if items else None
            action, why = "HOLD", "无可用因子"
            if horizon is not None:
                action, why, _strength = bridge.decide_direction(
                    items, r["pct_1d"], None, allow)
            derived.append({
                "grade": (_td.grade_of(float(r["score"] or 0.0)) if _td else None),
                "asset_class": cls,
                "vol_bucket": vb,
                "actionable": horizon is not None,
                "horizon": horizon,
                "action": action,
                "decision_reason": why,
            })

    out: list[dict[str, Any]] = []
    for idx, r in enumerate(rows):
        try:
            sigs = json.loads(r["signal_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            sigs = []
        try:
            merged = json.loads(r["merged_types"] or "[]")
        except (json.JSONDecodeError, TypeError):
            merged = []
        out.append({
            "ts": r["ts"], "close": r["close"],
            "trigger_type": r["trigger_type"], "score": r["score"],
            "uses_future": int(r["uses_future"] or 0),
            "pct_1d": r["pct_1d"], "pct_3d": r["pct_3d"],
            "vol_ratio": r["vol_ratio"], "support_level": r["support_level"],
            "signals": sigs, "merged_types": merged,
            "bars_available": r["bars_available"],
            "data_max_ts": r["data_max_ts"],
            **(derived[idx] if idx < len(derived) else {}),
        })
    stale_days = ((int(time.time()) - max_ts) // 86400) if max_ts else None
    return {
        "ok": True, "asset": sym, "signals": out, "n": len(out),
        "include_future": include_future,
        "data_max_ts": max_ts,
        "data_stale_days": stale_days,
        "note": ("底层 OHLCV 数据滞后, 信号基于更早价格"
                 if (stale_days or 0) > 2 else None),
    }


# ──────────────────── v303 R15: 价格结构形态 (因果可识别) ────────────────────
@app.get("/api/tech/structure")
def tech_structure_api(
    symbol: str = "MU",
    days: Annotated[int, Query(ge=1, le=3650)] = 365,
    cooldown: int = 5,
    min_height_pct: float = 0.04,
):
    """R15+: 价格结构形态识别 (双顶/双底/吞没/锤子/射击之星/十字星).

    与 tech_signals 的关键区别:
      - tech_signals 含未来函数的转折点(用户许可); 这里**纯因果**
      - 输出形态是用户视觉可识别的 K 线结构, 不预报方向
      - 前端 K 线图叠加 marker 显示形态名称+颈线价

    参数:
      symbol           资产
      days             回看天数
      cooldown         同形态最小间隔(bar)
      min_height_pct   形态最小幅度(%)
    """
    from market.price_structure import CFG as PS_CFG, detect as detect_structure
    # 覆盖默认参数(只影响本次调用, 不改全局)
    PS_CFG["cooldown"] = max(1, min(cooldown, 60))
    PS_CFG["min_height_pct"] = max(0.005, min(min_height_pct, 0.20))

    sym = (symbol or "").upper().replace("USDT", "").replace("-", "")
    cutoff = int(time.time()) - days * 86400
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume "
            "FROM ohlcv_daily WHERE asset=? AND ts >= ? ORDER BY ts ASC",
            (sym, cutoff),
        ).fetchall()
    if not rows:
        return {"ok": False, "asset": sym, "structures": [], "n": 0,
                "reason": f"{sym} 无 OHLCV 数据"}
    bars = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in rows]
    sigs = detect_structure(bars)
    # 友好名称映射(英文->中文)
    NAME_ZH = {
        "hammer": "锤子线",
        "shooting_star": "射击之星",
        "bullish_engulfing": "看涨吞没",
        "bearish_engulfing": "看跌吞没",
        "doji": "十字星",
        "double_top": "双顶",
        "double_bottom": "双底",
    }
    out = [{
        "ts": s["ts"],
        "name": s["name"],
        "name_zh": NAME_ZH.get(s["name"], s["name"]),
        "open": s["open"], "high": s["high"],
        "low": s["low"], "close": s["close"],
        "height_pct": s["height_pct"],
        "info": s["info"],
        "uses_future": False,
    } for s in sigs]
    return {
        "ok": True, "asset": sym, "structures": out, "n": len(out),
        "bars_used": len(bars),
        "data_max_ts": rows[-1][0],
    }


@app.get("/api/tech/anomaly")
def tech_anomaly_api(
    symbol: str = "MU",
    days: Annotated[int, Query(ge=1, le=3650)] = 365,
    cooldown: int = 15,
    vol_surge_ratio: float = 1.4,
    interval: str = "1d",
    persist: bool = True,
):
    """R17: 真异常识别 — 形态+RSI+MACD+量能 组合门, MACD 缩量线筛噪音。

    用户截图(ETH)显示: 数百个密集圆点里只有 2 个是真异常, 其余是噪音。
    噪音集中在 MACD 柱体连续缩量(动能衰竭)区间 —— 本端点用该条件做噪音门。

    与 /api/tech/structure 的区别:
      - structure: 所有价格形态(密度高, 约 20%, 多数是噪音)
      - anomaly  : 组合门过滤后的真异常(密度 ~0.3%), 噪音已剔除

    v304: 新增 interval 参数 —— 分钟级周期从 ohlcv_intraday 取数
    (原实现写死 ohlcv_daily, 导致前端选 10m 时异常点也按日线算)。
    v304: 新增 persist —— 命中结果落 anomaly_history 表, 跨时段累积保留,
    供事后"发现与反思"; 前端另有 /api/tech/anomaly-history 读取历史。

    参数:
      symbol           资产
      days             回看天数
      cooldown         同方向信号最小间隔(bar)
      vol_surge_ratio  量比门槛
      interval         周期(1d 走日线; 10m 等走 intraday_store)
      persist          是否写入 anomaly_history
    """
    from true_anomaly import CFG as TA_CFG, detect as detect_anomaly
    from true_anomaly import thresholds as anomaly_thresholds

    # 阈值按资产自身分布自适应(见 true_anomaly._adaptive_thresholds)
    TA_CFG["cooldown"] = max(1, min(cooldown, 200))
    TA_CFG["adaptive"] = True
    # vol_surge_ratio 仅在关闭自适应时生效
    TA_CFG["vol_surge_ratio"] = max(1.0, min(vol_surge_ratio, 5.0))

    sym = (symbol or "").upper().replace("USDT", "").replace("-", "")
    src = "ohlcv_daily"
    rows = []
    if interval in intraday_store.FRONTEND_INTERVALS:
        res = intraday_store.read_bars(sym, interval, limit=3000, days=days)
        if not res.get("ok"):
            return {"ok": False, "asset": sym, "interval": interval,
                    "anomalies": [], "n": 0,
                    "reason": res.get("error", "分钟数据缺失")}
        bars = res["bars"]
        src = res.get("data_source", "ohlcv_intraday")
    else:
        cutoff = int(time.time()) - days * 86400
        with db_cursor() as conn:
            raw = conn.execute(
                "SELECT ts, open, high, low, close, volume "
                "FROM ohlcv_daily WHERE asset=? AND ts >= ? ORDER BY ts ASC",
                (sym, cutoff),
            ).fetchall()
        if not raw:
            return {"ok": False, "asset": sym, "anomalies": [], "n": 0,
                    "reason": f"{sym} 无 OHLCV 数据"}
        bars = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                 "close": r[4], "volume": r[5]} for r in raw]

    hits = detect_anomaly(bars)
    from market.price_structure import detect as detect_structure
    raw_n = len(detect_structure(bars))

    persisted = 0
    if persist and hits:
        try:
            persisted = intraday_store.save_anomalies(sym, interval, hits)
        except Exception as exc:
            persisted = -1
            print(f"[tech/anomaly] 落库失败 {sym}/{interval}: {exc}")

    return {
        "ok": True, "asset": sym, "interval": interval, "anomalies": hits,
        "n": len(hits), "bars_used": len(bars),
        "raw_patterns": raw_n,
        "noise_filtered": raw_n - len(hits),
        "density_pct": round(len(hits) / max(len(bars), 1) * 100, 3),
        "thresholds": anomaly_thresholds(bars),
        "persisted": persisted,
        "data_source": src,
        "data_max_ts": bars[-1]["ts"] if bars else None,
    }


@app.post("/api/intraday/backfill")
def intraday_backfill_api(
    asset: str = "ETH",
    interval: str = "1m",
    days: Annotated[int, Query(ge=1, le=30)] = 5,
):
    """回补分钟级 K 线。

    v308 修正两个缺陷:
      1. 美股(非 Binance 币种)改走 yfinance —— 原实现只认 9 个币种,
         MU 等美股永远拿不到分钟数据。
      2. **失败必须如实回报** —— 原实现 `except Exception: return
         {"ok": False, ...}` 会把真实错误(如"代码不在 Binance 列表")
         藏起来; 而 intraday_store 内部的游标分页又会吞异常, 导致最终
         返回 {"ok": True, "pages": 0, "page_failures": 21} 这种
         "假成功", 上层据此给出"再调一次回补"的误导性 hint。

    yfinance 分钟级限制: 上限 30 天, 且 period="1d" 对 1m 返回空,
    故默认 days 收到 5(实测 MU 1m/5d = 1950 根)。
    """
    try:
        res = intraday_store.backfill(asset, interval, days)
    except Exception as exc:
        return {"ok": False, "asset": asset, "interval": interval,
                "error": str(exc), "new_bars": 0}
    ok = bool(res.get("new_bars", 0) or res.get("pages", 0))
    if not ok:
        # 没拿到任何数据 → 不能报 ok:true
        res = {**res, "ok": False,
               "error": res.get("error")
               or "回补未取得任何数据(代码可能不存在/已退市, 或数据源不可达)"}
    return res


@app.post("/api/intraday/sync")
def intraday_sync_api(asset: str = "ETH", interval: str = "1m"):
    """增量续拉(库中最后一根往后接), 供定时任务调用形成历史累积。"""
    try:
        n = intraday_store.incremental(asset, interval)
    except Exception as exc:
        return {"ok": False, "asset": asset, "error": str(exc)}
    return {"ok": True, "asset": asset, "interval": interval, "new_bars": n}


@app.get("/api/tech/anomaly-history")
def tech_anomaly_history_api(
    symbol: str = "ETH", interval: str = "10m",
    limit: Annotated[int, Query(ge=1, le=10000)] = 3000,
):
    """读历史真异常点(anomaly_history 表, 跨时段累积, 永不删除)。

    与 /api/tech/anomaly 的区别: 后者是"对当前窗口重新计算",
    本端点是"读取已累积保存的历史记录", 支持回看任意历史区间,
    供用户事后做发现与反思。
    """
    sym = (symbol or "").upper().replace("USDT", "").replace("-", "")
    try:
        items = intraday_store.read_anomaly_history(sym, interval, limit)
    except Exception as exc:
        return {"ok": False, "asset": sym, "interval": interval,
                "anomalies": [], "n": 0, "error": str(exc)}
    return {
        "ok": True, "asset": sym, "interval": interval,
        "anomalies": items, "n": len(items),
        "first_ts": items[0]["ts"] if items else None,
        "last_ts": items[-1]["ts"] if items else None,
        "data_source": "anomaly_history (累积保存, 永不删除)",
    }


@app.get("/api/market/history/{symbol}")
def market_history(
    symbol: str, interval: str = "1d",
    limit: Annotated[int, Query(ge=1, le=5000)] = 180,
    days: Annotated[int | None, Query(ge=1, le=3650)] = None,
):
    """市场 K 线历史 (前端 MoversPanel / live-kline-chart 消费).

    v302+ : 日线从 ohlcv_daily 读。
    v304  : 分钟级周期(1m/3m/5m/10m/15m/30m/1h) 走 intraday_store。
            根因修复 —— 库里原本**只有 ohlcv_daily**, 所以前端选 10m
            实际拿到的是日线(界面"10m 却像日线")。
            Binance 无 10m 周期, 由 1m 本地聚合成 10m(10 根合并),
            详见 intraday_store.py 顶部说明。
    v303 Q1 cont: 每根 bar 同时给 `ts` 和 `time`(前端 HistoryBar 读 `b.time`)。
    """
    sym = symbol.upper().replace("USDT", "").replace("-", "")

    # ── 分钟级周期 → intraday_store ──
    if interval in intraday_store.FRONTEND_INTERVALS:
        # v345: 前端按「一屏 720 根」传 limit=720，10m 等于 5 天；换算回补天数后
        #   一次要拉多页 1m，实测直接把请求拖过前端 30s 超时 → K 线区只剩一行
        #   "请求超时"。10m 视图 300 根已经够看 2 天，这里统一封顶 400。
        if limit > 400:
            limit = 400
        # v342: 前端总是传 days=5，但 200 根 10m 只要 1.4 天的数据。按 days=5 回补
        #   要多打 4~5 页 1m（每页 2~3s，冷启动直接 30~60s 超时）。这里按
        #   limit×周期 折算真实需要的天数，只回补够用的那一段。
        _target_sec = intraday_store.FRONTEND_INTERVALS[interval][0]
        _need_days = int((limit * _target_sec + 86399) // 86400) + 1
        days = min(days or _need_days, _need_days)
        try:
            res = intraday_store.read_bars(
                sym, interval, limit=limit, days=days)
        except ValueError as exc:
            return {"ok": False, "symbol": sym, "interval": interval,
                    "bars": [], "items": [], "points": [], "count": 0,
                    "missing": str(exc)}
        if not res.get("ok"):
            # 该资产还没有分钟数据 → 明确回报, 而不是静默降级成日线。
            # v308: hint 带上 interval/days, 且按资产给出正确数据源
            # (美股走 yfinance, 不是 Binance)。
            src = "binance_us" if sym in intraday_store.ASSET_SYMBOLS \
                else "yfinance_us"
            return {"ok": False, "symbol": sym, "interval": interval,
                    "bars": [], "items": [], "points": [], "count": 0,
                    "missing": res.get("error", "分钟数据缺失"),
                    "data_source": res.get("data_source", src),
                    "hint": f"先调 POST /api/intraday/backfill"
                            f"?asset={sym}&interval=1m&days=5"}
        bars = []
        for b in res["bars"]:
            d = dict(b)
            d["time"] = d["ts"]
            bars.append(d)
        return {
            "ok": True, "symbol": sym, "interval": interval,
            "days": days, "limit": limit,
            "n": len(bars), "count": len(bars),
            "points": [{"time": b["time"], "close": b["close"]} for b in bars],
            "items": bars, "bars": bars, "missing": "",
            "data_source": res.get("data_source", "ohlcv_intraday"),
        }

    # ── 日线及以上 → ohlcv_daily(原逻辑不变) ──
    days_eff = days or max(limit * 2, 180)
    cutoff = int(time.time()) - days_eff * 86400
    with db_cursor() as conn:
        # v303 R11: 必须取**最新** limit 根, 不能 ORDER BY ts ASC LIMIT limit。
        # ASC + LIMIT 会返回窗口内最早的 limit 根 —— cutoff 是 360 天前而 limit=180,
        # 于是 180/360 根全落在 2025-10~2026-07, K线永远停在 3 个月前,
        # 而 news_events 只有最近 7 天 → 日历/新闻 marker 全部落在可视区外。
        # 正确做法: DESC 取最新 limit 根, 内存里反回升序。
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume, source "
            "FROM ohlcv_daily WHERE asset=? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (sym, cutoff, limit),
        ).fetchall()
        rows = list(reversed(rows))
    bars = []
    for r in rows:
        d = dict(r)
        d["time"] = d["ts"]
        bars.append(d)
    return {
        "ok": True,
        "symbol": sym,
        "interval": interval,
        "days": days_eff,
        "limit": limit,
        "n": len(bars),
        "count": len(bars),
        "points": [{"time": b["time"], "close": b["close"]} for b in bars],
        "items": bars,
        "bars": bars,
        "missing": "" if bars else f"ohlcv_daily 无 {sym} 数据",
        "data_source": "ohlcv_daily (v303 Q1 cont: bar 同时带 ts + time)",
    }




@app.get("/api/v189/calendar/events-for-asset")
def v189_calendar_events_for_asset(
    asset: str,
    hours: Annotated[int, Query(ge=1, le=8760)] = 720,
    star_min: int = 1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """2026-11-18: 实现已下沉到 `bridge_analytics`，这里只保留签名与参数校验。"""
    from bridge_analytics import v189_calendar_events_for_asset as _impl

    return _impl(asset, hours, star_min, limit)




# ─── v292+ R2 Round 3: catch-all 兜底路由 ──────────────────────────────
# 背景: 8000 api_server.py 磁盘源文件已删,前端所有 /api/* 经 Next.js rewrites
#       proxy 到 8100。但 bridge_server 只实现了 ~28 个 v237/v291/decision/news/cot
#       端点,其他 13+ 个老端点(calendar/status · market/movers · market/attribute ·
#       events/heatmap · market/depth/* · market/orderbook · abnormal/feedback-stats ·
#       macro/history · review/causal-graph · review/export · export/excel 等)
#       全部 404,导致 LiveDesk 等页面显示"HTTP 404"错误。
#
# 策略: 加一个低优先级 catch-all GET/POST(放在文件末尾),任何未匹配的具体路由
#       都会落到这里,按路径前缀猜结构返 ok:true + 空数据,前端组件就不会再
#       看到"HTTP 404"字样。空数据提示前端走"暂无数据"渲染分支(灰态/占位),
#       而不是"加载失败"分支(红字 404)。
#
# 注意: 这个 catch-all 必须在所有 @app.get("/api/...") 之后注册,否则会
#       抢在具体路由前面,导致已有端点被截胡。FastAPI 路由匹配是按注册顺序的。

import time as _time
_BRIDGE_START_TS = _time.time()

def _empty_response(path: str) -> dict:
    """按路径特征猜结构,返合理默认值。前端用 {items,count} 范式渲染。"""
    # v302+ R2 Round 5: 7 个未实现端点 — 沿用 ai/models 模式, 返前端期望的 shape 空数据
    # 注意: 这些 path-specific 必须在下面的通用 seg 匹配之前,否则会被 "events"/"signals" 等
    # 通用 seg 抢先返 {items,count} 通用空壳, 前端访问 .assets / .still_pending 等会崩
    if "events/heatmap" in path:
        # event-heatmap.tsx 期望 HeatResp {hours, bucket_count, buckets, assets, cells, ...}
        return {
            "ok": True, "hours": 24, "bucket_count": 0, "buckets": [],
            "assets": [], "cells": {}, "max_count": 0, "max_score": 0,
            "since_ts": 0, "now_ts": int(_time.time()), "generated_at": "",
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: events/heatmap 暂未实现, 返回空热力图",
        }
    if "review/signals" in path:
        # review-history-panel.tsx 期望 ReviewSignals {items, count, updated, still_pending, ...}
        return {
            "ok": True, "items": [], "count": 0,
            "updated": [], "still_pending": [],
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: review/signals 暂未实现, 返回空 signals",
        }
    if "reflection/clustering" in path or "reflection/failure-clustering" in path:
        # FailureClusteringPanel 期望 ReflectionClustering {days, total_errors, total_decisions, patterns}
        return {
            "ok": True, "days": 30, "min_count": 2,
            "total_errors": 0, "total_decisions": 0, "patterns": [],
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: reflection/clustering 暂未实现, 返回空聚类",
        }
    if "reflection/stock-decisions" in path:
        # StockReflectionPanel 期望 StockDecisionsResp {total, by_asset, by_action, days, limit, decisions}
        return {
            "ok": True, "total": 0, "by_asset": {}, "by_action": {},
            "days": 30, "limit": 200, "decisions": [],
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: reflection/stock-decisions 暂未实现",
        }
    if "cross-asset/anomaly" in path:
        # CrossAssetAnomalyPanel 期望 CrossAssetAnomalyResponse {symbols_aligned, ...}
        return {
            "ok": True, "ts": 0, "is_anomaly": False, "direction": "none",
            "classes_aligned": 0, "classes_total": 0,
            "symbols_aligned": [], "classes_aligned_names": [],
            "shape_consistent": False, "severity": "low",
            "message": "", "snapshot_ts": "", "macro_regime": "",
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: cross-asset/anomaly 暂未实现, 返回无异动",
        }
    if "ai/prompts" in path:
        # AiPromptsPanel 期望 PromptData {main, usage, custom}
        return {
            "ok": True,
            "main": {
                "system_prompt": {"content": "", "edited": False, "default": ""},
                "json_force_prompt": {"content": "", "edited": False, "default": ""},
                "market_structure_block": {"content": "", "edited": False, "default": ""},
                "doubao_prompt": {"content": "", "edited": False, "default": ""},
            },
            "usage": {
                "system_prompt": [], "json_force_prompt": [], "doubao_prompt": [],
                "market_structure_enabled": False,
            },
            "custom": [],
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: ai/prompts 暂未实现, 返回空 prompts",
        }
    # 列表类: /api/.../{list,recent,history,heatmap,movers,depth,orderbook,
    #                  events,alerts,export,feedback-stats,signals,chain...}
    if any(seg in path for seg in (
        "list", "recent", "history", "heatmap", "movers", "depth",
        "orderbook", "events", "alerts", "export", "feedback", "signals",
        "chain", "graph", "snapshots", "ledger", "feed",
    )):
        return {
            "ok": True,
            "items": [],
            "count": 0,
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: 此路径暂未实现, 返回空数据",
        }
    # 单条类: /api/.../{stats,context,summary,status,snapshot,attribute,detail,info}
    if any(seg in path for seg in (
        "stats", "context", "summary", "status", "snapshot", "attribute",
        "detail", "info", "config", "settings", "version",
    )):
        return {
            "ok": True,
            "data": None,
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: 此路径暂未实现, 返回空数据",
        }
    # v302+ R2 Round 4: /api/ai/models 特例 — 前端 dashboard-header.tsx 期望 {models, selected}
    # 兜底必须给同样 schema,否则 setModels(data.models) 会让 models state 变 undefined
    # 进而触发 "models.length" 运行时崩溃
    if "models" in path:
        return {
            "ok": True,
            "models": [],
            "selected": "",
            "_bridge_fallback": True,
            "_note": "bridge_server catch-all: ai/models 暂未实现, 返回空列表",
        }
    # 兜底: 任何路径都给个通用空壳,避免 UI 看到 HTTP 404
    return {
        "ok": True,
        "result": None,
        "_bridge_fallback": True,
        "_note": f"bridge_server catch-all: {path} 暂未实现",
    }


def _mark_unimplemented(payload: dict, path: str) -> dict:
    """把 catch-all 的空壳标记成「后端未实现」, 而不是假装成功。

    v308 P1: 原来 catch-all 一律返 {ok: true, ...空数据}, 前端按 ok=true 渲染
    「暂无数据」—— 看起来像"后端跑过了、确实没数据", 其实是假成功。
    现在统一 ok=false + error 说明原因, 同时保留原 shape 字段
    (items/count/data/...), 避免前端访问 undefined 字段崩溃。
    """
    note = payload.get("_note") or f"bridge_server catch-all: {path} 暂未实现"
    return {
        **payload,
        "ok": False,
        "error": f"后端未实现该接口 ({path}): {note}",
        "_implemented": False,
        "_bridge_fallback": True,
    }


# ─── v317: 异常信号归因（AI 归因 + 跨平台注意力证据） ───────────────────────
# 必须注册在 catch-all 之前 —— 否则前端归因抽屉只会拿到 _bridge_fallback 空壳，
# 表现为「信号不存在」，真实存在的数据被当成"没有"。
#
# id 空间说明（重要）：
#   前端列表走**本文件**的 /api/abnormal-signals → id 来自 critical_price_moves；
#   而 abnormal_attributions.signal_id 指向的是 abnormal_signals.id —— 两者不是同一空间。
#   所以这里先用 asset + 时间窗定位到 abnormal_signals.id，再取归因，
#   并把匹配方式随响应返回（attribution_match），不假装是 signal_id 直查。
@app.get("/api/abnormal-signals/{signal_id}/attribution")
async def abnormal_signal_attribution(signal_id: int):
    """v317：单条异常信号的归因 —— AI 归因 + 跨平台注意力证据。

    crowd_attention 是一条**独立**于 AI 归因的客观证据（真实抓取、带发布时间戳的
    跨平台提及），有就是有、没有就是 null，不参与也不改写 AI 结论。
    """
    import crowd_attention_lead as cal

    def _sync() -> Dict[str, Any]:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            mv = conn.execute(
                "SELECT id, asset, ts, duration_min, move_pct, move_kind, algo_score "
                "FROM critical_price_moves WHERE id=?",
                (int(signal_id),),
            ).fetchone()
            if not mv:
                return {"error": "not found", "signal_id": int(signal_id)}
            asset = str(mv[1] or "")
            ts = int(mv[2] or 0)
            dur_min = int(mv[3] or 0)
            anchor_ts = ts - dur_min * 60      # 秒，与本文件列表端点口径一致

            # ── AI 归因（跨表尽力匹配，失败就是 null，不编造） ──
            attr = None
            attr_match = "none"
            try:
                import abnormal_detector as _ad
                sig = conn.execute(
                    "SELECT id FROM abnormal_signals WHERE asset=? "
                    "AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) ASC LIMIT 1",
                    (asset, anchor_ts - 7200, ts + 7200, ts),
                ).fetchone()
                if sig:
                    attr = _ad.fetch_latest_attribution(conn, int(sig[0]))
                    if attr:
                        attr_match = "abnormal_signals_id"
            except Exception:
                attr = None

            # ── 跨平台注意力证据（v317） ──
            crowd_ev = None
            try:
                if asset and asset.upper() != "SECTOR" and anchor_ts:
                    # attribution_evidence 收的是**毫秒**
                    crowd_ev = cal.attribution_evidence(conn, asset, int(anchor_ts) * 1000)
            except Exception:
                crowd_ev = None

            return {
                "signal_id": int(signal_id),
                "asset": asset,
                "signal_kind": "multi_factor",
                "status": "active" if float(mv[6] or 0) >= 0.7 else "weak",
                "move_ts": ts,
                "move_pct": float(mv[4] or 0),
                "move_kind": str(mv[5] or ""),
                "duration_min": dur_min,
                "anchor_ts": anchor_ts,
                "attribution": attr,
                "attribution_match": attr_match,
                "crowd_attention": crowd_ev,
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_sync)






# ─── v499: 三条「挂名未实现」端点显式化 ────────────────────────────────────
# 之前它们落进 catch-all, 上游(8000)没起时返回 503 `_implemented:false`, 语义上
# 等于"暂时不可用、可重试", 监控/健康检查会把它们当成故障反复重试。实际是这三条
# 压根没有实现(全仓无任何 handler, 前端也无调用)。改为 **501 Not Implemented** +
# 明确文案, 让"没做"和"挂了"彻底分开。
_UNIMPLEMENTED_PATHS = {
    "discovery/asset-hunter": "资产猎手没有独立端点（该能力由发现页自身的数据流提供）",
    "divergence/radar": "背离雷达没有独立端点（信号由 /api/divergence/ledger 提供）",
    "macro/flows": "资金流没有独立端点（数据由 /api/macro/context + /api/macro/history 提供）",
}


def _unimplemented(path: str):
    note = _UNIMPLEMENTED_PATHS[path]
    return JSONResponse(status_code=501, content={
        "ok": False,
        "error": note,
        "path": f"/api/{path}",
        "_implemented": False,
        "status": "not_implemented",
    })


@app.get("/api/discovery/asset-hunter", include_in_schema=False)
def _unimplemented_asset_hunter():
    return _unimplemented("discovery/asset-hunter")


@app.get("/api/divergence/radar", include_in_schema=False)
def _unimplemented_divergence_radar():
    return _unimplemented("divergence/radar")


@app.get("/api/macro/flows", include_in_schema=False)
def _unimplemented_macro_flows():
    return _unimplemented("macro/flows")


@app.get("/api/{full_path:path}", include_in_schema=False)
async def _bridge_catch_all_get(full_path: str, request: Request):
    """GET 兜底: 先转给 8000, 真没有才返「未实现」空壳。

    v308 P1: 不再返 {ok:true} 假装成功 —— 明确标记「后端未实现」,
    前端会显示"该接口没接上", 而不是把空表当成"确实没有数据"。
    """
    status, payload = await asyncio.to_thread(
        _forward_to_api, full_path, request.url.query, "GET",
        b"", request.headers.get("content-type", ""))
    if status == 200 and payload is not None:
        return payload
    # v367 P0: 上游(8000)不可达时**显式报错**, 不再用 200 空壳冒充「后端确实没数据」。
    # 静默变空是本项目最贵的故障模式: 8000 被 OOM 杀掉的那 6 次, 页面全是「暂无数据」,
    # 没人知道是整个上游没了。503/502 会让前端 usePoll 拿到非 2xx → 面板显示真实错误。
    if status is None:
        return JSONResponse(status_code=503, content={
            "ok": False,
            "error": f"接口 /api/{full_path} 后端未实现或上游暂不可用",
            "path": f"/api/{full_path}",
            "_implemented": False,
            "_bridge_upstream_down": True,
            "retryable": True,
        })
    if status >= 500:
        return JSONResponse(status_code=502, content={
            "ok": False,
            "error": f"上游返回 {status}，接口 /api/{full_path} 失败",
            "path": f"/api/{full_path}",
            "_implemented": False,
            "_bridge_upstream_error": True,
            "upstream_status": status,
        })
    return _mark_unimplemented(_empty_response(full_path), full_path)

# ─── v499: 新闻列表缓存常驻预热 ──────────────────────────────────────
# 原因: bridge 每次重启后, 第一次 /api/news/recent 实测要 30s+ 才回
#   (四表 JOIN 首命中 + 启动期一堆预热线程抢 GIL), 前端 15s 拿不到就停在
#   「加载中…」, 要手动再刷一次才出内容。这个端点本身只要 0.06s,
#   所以直接在后台每 12s 跑一次塞进 TTL 缓存, 用户请求永远命中热缓存。
#   注: bridge 的 @app.on_event("startup") 在本进程不会被执行(lifespan 已接管),
#   所以必须用模块级线程, 不能挂 startup。
def _warm_news_loop() -> None:
    import json as _json
    import threading as _th
    key = "/api/news/recent?hours=24&limit=50"

    def _once() -> None:
        try:
            payload = news_recent(24, 50)
            body = _json.dumps(payload, ensure_ascii=False).encode()
            _CACHE_STORE[key] = (time.time(), body)
        except Exception as exc:  # pragma: no cover
            print(f"[warm] 新闻缓存预热失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)

    def _loop() -> None:
        while True:
            _once()
            time.sleep(12.0)

    _th.Thread(target=_loop, name="news-cache-warm", daemon=True).start()
    print("[warm] 新闻缓存预热线程已启动（12s 周期）", flush=True)


try:
    _warm_news_loop()
except Exception as _e:  # pragma: no cover
    print(f"[warm] 预热线程启动失败: {_e}", flush=True)



@app.post("/api/{full_path:path}", include_in_schema=False)
async def _bridge_catch_all_post(full_path: str, request: Request):
    """POST 兜底: 未实现的写操作。

    v308 P1: 原来返 {ok:true, noop:true}, UI 会提示"提交成功", 但库里什么都没写。
    现在 ok=false, 让调用方知道这次写入没有真的发生。
    """
    return _mark_unimplemented(
        {
            "noop": True,
            "_note": f"bridge_server catch-all POST: {full_path} 未实现, 没有真的写入",
        },
        full_path,
    )


# ─── 挂载 api_server 全量路由 (v170+ 功能恢复) ──────
# 说明: 2026-10 从桌面备份 (trident_backend_code_20261012_v3.tar.gz / 金融1.0)
# 恢复了 backend 全量源码, 其中 api_server.py 提供 349 个真实路由
# (归因复核 / 反思 / 推理审查 / 纸面交易 / 知识图谱 ...)。
# 下面把它的路由以优先级更高的方式挂到本 app 上, 使 8100 端口不再靠
# catch-all 返回空壳, 而是走真实实现。挂载失败时降级为原有 catch-all 行为。
_MOUNTED_API_SERVER = False
try:
    import api_server as _api_server  # noqa: WPS433

    # v320 P0: 把 api_server 的 /api/events/today **权威实现**接进本进程，
    # 让 8100 与 8000 用同一份代码、同一个 DB、同一个口径。
    # 8000 端该端点返回完整契约 (today/window/news_count/analyzed_count/
    # total_count/signal_count/live_signal_count/live_signal_window_hours/
    # settled_today)，而本文件原先的简化桩只返 2 个字段、且"今日"起点把
    # 本地日期和 UTC 零点混搭 → 前端卡片显示 窗口新闻 0 / 已分析 0。
    # 这里按 path 反查 api_server 的 endpoint 函数（不依赖函数名）。
    for _r in list(getattr(getattr(_api_server, "app", None), "router", None).routes
                    or []):
        _rp = getattr(_r, "path", "")
        _rm = getattr(_r, "methods", None) or set()
        if "GET" not in _rm:
            continue
        if _rp == "/api/events/today":
            globals()["_API_EVENTS_TODAY_ENDPOINT"] = getattr(_r, "endpoint", None)

    _existing = {
        (getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
        for r in app.router.routes
    }
    _new_routes = []
    for _route in _api_server.app.router.routes:
        _path = getattr(_route, "path", "")
        _methods = tuple(sorted(getattr(_route, "methods", []) or []))
        # 跳过 FastAPI 内建路由 (openapi/docs/redoc/swagger-oauth)
        if _path.startswith(("/openapi", "/docs", "/redoc", "/swagger")):
            continue
        # 已有同名同方法路由则跳过, 保留本文件自身的实现优先
        if (_path, _methods) in _existing:
            continue
        _new_routes.append(_route)

    # ── 顺序很关键 ──
    # Starlette 按注册顺序匹配, 本文件末尾的 catch-all ("/api/{full_path:path}")
    # 必须排在所有真实路由之后, 否则会先命中并吞掉全部请求。
    # 这里把新增路由插到 catch-all 之前, catch-all 仍然兜底未实现路径。
    _catch_all_idx = len(app.router.routes)
    for _i, _r in enumerate(app.router.routes):
        if getattr(_r, "path", "") == "/api/{full_path:path}":
            _catch_all_idx = _i
            break
    app.router.routes[_catch_all_idx:_catch_all_idx] = _new_routes
    _added = len(_new_routes)
    _MOUNTED_API_SERVER = True
    print(
        f"[bridge] ✅ 已挂载 api_server 路由 {_added} 个 "
        f"(共 {len(app.router.routes)} 条), 归因/反思/推理审查恢复",
        flush=True,
    )
    # v2026.10.26: 这里挂的是**路由**（不跑 api_server 的 lifespan），
    # 所以 /api/replay/positions 的后台刷新线程要在这里自己拉起来，
    # 否则第一次请求要走「无网络快版」，页面要等 30-60s 才有完整浮盈亏。
    try:
        _api_server._replay_ensure_refresher()
        print("[bridge] ✅ replay 持仓快照后台刷新已启动", flush=True)
    except Exception as _exc2:  # pragma: no cover - 降级保护
        print(f"[bridge] ⚠️ replay 后台刷新启动失败(忽略): {type(_exc2).__name__}: {str(_exc2)[:120]}", flush=True)
except Exception as _exc:  # pragma: no cover - 降级保护
    import traceback as _tb

    print(
        f"[bridge] ⚠️ api_server 挂载失败, 降级为 catch-all 兜底: "
        f"{type(_exc).__name__}: {str(_exc)[:160]}",
        flush=True,
    )
    _tb.print_exc()


# ────────────────────────────────────────────────────────────────────────────
# 2026-11-17: 版本号老路径 → 规范路径别名（本文件自有 27 条 + 挂进来的 api_server 路由）
#   前端实际打 8100，历史上都是 /api/v237、/api/v291 这种版本号路径。
#   这里为每条版本化路由追加 /api/<去掉版本号> 的别名（同一个 endpoint），
#   老路径保留；别名插在原路由之后、末尾 catch-all 之前，顺序不会被兜底吞掉。
# ────────────────────────────────────────────────────────────────────────────
try:
    from legacy_alias import add_canonical_aliases as _add_canonical_aliases

    _BRIDGE_CANON_ALIASES = _add_canonical_aliases(app)
    if _BRIDGE_CANON_ALIASES:
        print(
            f"[bridge] ✅ 版本号路径别名 {len(_BRIDGE_CANON_ALIASES)} 条（老路径保留）",
            flush=True,
        )
except Exception as _alias_exc:  # pragma: no cover - 别名失败不影响服务启动
    print(
        f"[bridge] ⚠️ 版本号路径别名安装失败: {type(_alias_exc).__name__}: {str(_alias_exc)[:120]}",
        flush=True,
    )


# ─── main ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("BRIDGE_PORT", "8100"))
    # 默认绑 0.0.0.0 而非 127.0.0.1:
    #   - 只听 IPv4 回环时, 浏览器把 localhost 解析到 [::1](IPv6) 就会得到
    #     ERR_CONNECTION_REFUSED —— IPv6 回环上无人监听。
    #   - 绑 0.0.0.0 可同时覆盖 127.0.0.1 / localhost / ::ffff:127.0.0.1。
    # 如需只允许本机访问, 设 BRIDGE_HOST=127.0.0.1。
    # v453 安全：默认只听回环（见 api_server 同名改动）。对外暴露需显式设 BRIDGE_HOST=0.0.0.0。
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
