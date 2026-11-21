#!/usr/bin/env python3
"""
Trident Agent MVP — FastAPI Backend Server (SSE Edition)
=========================================================
v282+: HONESTY 边界 — 绝不允许捏造数据/结论。详见 _honesty.NEVER_FABRICATE_INVARIANT
=========================================================

Usage:
  cd backend/src_python
  python api_server.py
"""

from __future__ import annotations

# v282+: 引用诚实宪法真理源 — 任何 fallback 必须返回 None,严禁捏造 0% / 假价格
try:
    from ._honesty import NEVER_FABRICATE_INVARIANT
except (ImportError, ValueError):  # 作为脚本跑时(.失败)兜底
    pass

import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import os
import sqlite3
import re
import html
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

# 2026-11-18: 这两类名字只在注解里出现（本文件已 from __future__ import annotations,
# 注解是字符串、运行时不求值），所以放 TYPE_CHECKING 里即可 —— 让 F821 归零,
# 同时不引入运行期循环导入（routers.replay 会反过来 import api_server）。
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import pathlib
    from routers.replay import PaperTradingBody

# 服务器可能缺少本地 CA 根证书（SSL: CERTIFICATE_VERIFY_FAILED）：
# 统一用 certifi 的完整证书包作为 SSL 默认 CA（对 urllib/ssl 全部生效）
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass
import urllib.request
import urllib.error
import urllib.parse

# v332: 进程级雅虎熔断 —— 本进程里还散落着若干 `import yfinance as yf
# v332: 进程级雅虎熔断 —— 本进程里还散落着若干 `import yf_guard as _yfg; _yfg.disable()` 的调用点
# (行情快照 / ES / 美元指数)，每次无数据都要吃 DNS+TLS+重试，api.err.log 已累计
# 8 万行 "possibly delisted"。这里统一熔断成"立即返回空"，由各调用方原有的兜底
# 分支接管（新浪/腾讯/东财）。返回空不等于造假，调用方必须自己判空。
try:
    import yf_guard
    yf_guard.disable()
except Exception:
    pass

from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Annotated, Any, Dict, List, Literal, Optional, Set, Tuple
import importlib

import aiosqlite
import requests
from pydantic import BaseModel, Field
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, UploadFile, File, Form, Body, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response

import config
import db
from repository import news as _news_repo  # 数据访问层（news 域样板，2026-11-16）
from common import time_utils          # v309: 统一时间口径 (唯一入口, 详见 time_utils.py 文件头)
import event_status # v317: 事件分析状态协议 (8000/8100 唯一真相源)
import demo_mode # v300: 体验模式（demo / licensed / full）
import license_auth # v300: License + API Key 鉴权
import demo_audit # v300: 调用审计 + 水印
import evidence
from feeds import fred_feed
import id_reconcile
import macro_context
from review import review_agent
import history_review
import analysis_process
import causal_graph
import belief_store # v223
import decision_guard
from market import market_feeds
import circuit_breaker
import timeseries
import strategy_store
import paper_trading
from feeds import news_sources
import quick_sim

# v310 P2: /api/review/snapshot 的唯一实现 (8000 与 8100 共用, 同源同口径)
from review import review_snapshot
import backtest
from feeds import jin10_feed
from feeds import jin10_mcp
import macro_calendar_ff
import realtime_filter
import ai_prompts
import stock_news
import reuters_news
from feeds import rss_sources
import market_sentiment # v170+ 数据源加固：FNG + Truth Social
import market_sentiment.fng
import market_sentiment.trump_posts
from feeds import bls_feed
from feeds import news_organizer
import feishu_listener
import causal_chains
import kg_improvements
from feeds import quote_sources
import abnormal_detector
from review import gate_reflection
import free_sources
from feeds import source_status
import notification_channels
from feeds import qinghong_feed
from realtime_filter import evaluate_news
from engine.prices import _fetch_eastmoney_xau, _fetch_sina_xau, _get_current_price

# ── v321.3 P0: fd 泄漏防护 (根治「AI分析挂了」) ────────────────────────────────
# 背景: launchd 拉起的进程 fd 软上限只有 256 (`launchctl limit maxfiles`),
#       而 yfinance 1.7.0 的 peewee 缓存**每个线程各开一条连接且不释放**,
#       本进程每个请求又新建线程 → 每请求泄漏 ~4 个 fd → 约 60 个请求后
#       socket.accept() 全部失败 → 进程在、端口在、但不响应任何请求。
# 详见 yf_fd_guard.py 文件头 (含实测复现数据)。
#   · yf_fd_guard.install()  — 根治: yfinance 请求结束回收本线程缓存连接
#   · fd_guard.start_fd_monitor() — 纵深: 水位监控 + 自动回收 + DLQ 告警
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
# v321.3 P1: 出网代理 (需求 #6 挂单量/资金流)。必须在**任何** requests Session
# 创建之前设置 —— requests 只在 Session 创建时读一次代理。
try:
    config.apply_proxy_env()
except Exception as _proxy_err:
    print(f"[PROXY] 应用失败 (忽略): {type(_proxy_err).__name__}: {_proxy_err}",
          flush=True)

BASE_DIR = config.BASE_DIR

TZ_SHANGHAI = config.TZ_SHANGHAI

# v302+ P0+：窗口 SQL 过滤 DEBUG 输出 — 把每条 _resolve_window + _utc_ts_to_iso
# 生成的最终 ISO 过滤字符串打到 stderr，方便核对 dashboard "今日"数据准确性。
# 默认关闭；env WINDOW_DEBUG=1 开启。生产保持关闭避免日志噪音。
import logging as _logging
# v309 P1: 投资人门户相关端点 (/api/security/entry, /api/signal/{symbol}, ...)
# 直接用裸名 `logger`, 但本模块从未定义过它 → NameError → HTTP 500。
# 之前这两个端点一直 500 就是这个原因 (裸 logger 从未被 import/赋值)。
# 统一定义成模块级 logger, 与其它模块 (_logging.getLogger("trident.xxx")) 保持一致。
logger = _logging.getLogger("trident.api")
_WINDOW_DEBUG = (os.environ.get("WINDOW_DEBUG", "0").strip().lower()
                 in ("1", "true", "on", "yes"))
_window_dbg_log = _logging.getLogger("trident.window")
if not _window_dbg_log.handlers:
    _h = _logging.StreamHandler()
    _h.setFormatter(_logging.Formatter(
        "[WINDOW] %(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _window_dbg_log.addHandler(_h)
_window_dbg_log.setLevel(_logging.DEBUG if _WINDOW_DEBUG else _logging.WARNING)
_window_dbg_log.propagate = False


# v504：时间口径 helper 已下沉到 api_time（纯函数、有单测），这里只保留别名。
from api_time import _utc_ts_to_iso, _utc_ts_to_space_iso  # noqa: E402


def _resolve_window_with_log(hours, from_ts, to_ts, caller: str = ""):
    """_resolve_window 的 DEBUG wrapper：返回 (lo, hi, lo_iso, hi_iso)。

    把最终落到 SQL 的 ISO 过滤字符串打到 WINDOW 日志（env WINDOW_DEBUG=1 开启），
    方便核对"今日"窗口到底命中了什么、是否漏掉了时区。
    """
    from quick_sim import _resolve_window
    lo, hi = _resolve_window(hours, from_ts, to_ts)
    lo_iso = _utc_ts_to_iso(lo) if lo is not None else None
    hi_iso = _utc_ts_to_iso(hi) if hi is not None else None
    if _WINDOW_DEBUG:
        from datetime import datetime as _dt, timezone as _tz
        now_utc = _dt.now(_tz.utc)
        beijing_today = (_dt.now(TZ_SHANGHAI)).strftime("%Y-%m-%d")
        _window_dbg_log.debug(
            "%s lo=%s hi=%s lo_iso=%s hi_iso=%s (UTC now=%s / Shanghai today=%s)",
            caller or "?", lo, hi, lo_iso, hi_iso,
            now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), beijing_today,
        )
    return lo, hi, lo_iso, hi_iso


class TradingProcessManager:
    def __init__(self, script_path: str, cwd: str, log_path: str, popen_factory=subprocess.Popen):
        self.script_path = script_path
        self.cwd = cwd
        self.log_path = log_path
        self.popen_factory = popen_factory
        self._process = None
        self._log_stream = None
        self._lock = threading.Lock()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {
                "running": running,
                "pid": self._process.pid if running else None,
                "mode": "testnet" if config.BINANCE_USE_TESTNET else "live",
            }

    def start(self) -> Dict[str, Any]:
        with self._lock:
            # 修复：A1 — fail-closed 默认禁用。即使 NewsTrading.py 存在且 token 已配，
            # 也必须显式 TRADING_MANAGER_ENABLED=1 才允许起子进程（默认防误操作实盘）。
            if not _trading_manager_enabled():
                return {"started": False, "reason": "disabled_by_default",
                        "hint": "set TRADING_MANAGER_ENABLED=1 to enable"}
            if self._process is not None and self._process.poll() is None:
                return {"started": False, "reason": "already_running", "pid": self._process.pid}
            if not os.path.isfile(self.script_path):
                return {"started": False, "reason": "script_missing", "path": self.script_path}
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            self._process = self.popen_factory(
                [sys.executable, self.script_path], cwd=self.cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return {"started": True, "pid": self._process.pid}

    def stop(self, timeout: float = 8.0) -> Dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._close_log()
                return {"stopped": False, "reason": "not_running"}
            process.terminate()
        killed = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            killed = True
        with self._lock:
            self._process = None
            self._close_log()
        return {"stopped": True, "killed": killed}

    def logs(self, tail: int = 200) -> list[str]:
        count = min(max(int(tail), 1), 2000)
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as stream:
                return stream.readlines()[-count:]
        except FileNotFoundError:
            return []

    def _close_log(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None


_EVENT_SELECT = """
    SELECT
        rn.id AS news_id,
        rn.timestamp,
        rn.published_at,
        rn.source,
        rn.content_fingerprint,
        rn.first_seen_ts,
        rn.net_origin_info,
        rn.cross_net_first_seen_ts,
        rn.cross_net_source,
        rn.content AS news_text,
        rn.status AS analysis_status,
        -- v321 fix: 这两列必须 SELECT 出来，否则 _row_to_event 里的
        -- _safe(row, "is_noise"/"gate_unavailable", 0) 恒为 0，
        -- 导致噪声行/门卫冻结行被误显示为「待 AI 分析」。
        rn.is_noise,
        rn.gate_unavailable,
        ad.id AS decision_id,
        ad.paper_trading_run_id,
        ad.strategy_id,
        ad.strategy_version_id,
        ad.agent_model_id,
        ad.evidence_confidence,
        ad.evidence_action,
        ad.trade_gate_reason,
        ad.no_trade_reason,
        UPPER(ad.suggested_action) AS action,
        ad.sentiment_score AS score,
        ad.reasoning AS reason,
        ad.market_category,
        ad.target_asset,
        ad.created_at,
        ad.parent_id,
        ad.child_count,
        ad.reasoning_path,
        ad.asset_reasoning,
        ad.vip_tag,
        qs.entry_price,
        qs.exit_price,
        ad.partial_exit_price,
        ad.partial_exit_ratio,
        ad.partial_pnl_pct,
        ad.heat_score,
        ad.impact_score,
        ad.asset_impact,
        ad.peer_reviews,
        qs.max_price,
        qs.min_price,
        qs.max_price_time,
        qs.min_price_time,
        qs.verdict AS is_correct,
        qs.settled,
        ad.doubao_action,
        ad.doubao_reasoning,
        ad.extra_models_consensus,
        datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
        ad.cluster_size,
        ad.prediction_type,
        ad.event_phase,
        ad.market_confirmation,
        ad.direct_catalyst,
        ad.expected_horizon,
        ad.invalidation_condition,
        ad.decision_context,
        ad.mfe_pct,
        ad.mae_pct,
        qs.pnl_pct AS forward_pnl,
        ad.mfe_time_mins,
        ad.attention_score,
        ad.elasticity_window,
        ad.data_anchor_ts,
        ad.hard_data_evidence,
        ad.event_struct,
        ad.structured_input,
        ad.structured_check,
        ad.event_type_code,
        ad.snapshot_proximity
    FROM raw_news rn
    -- 修复：C1 — 去 N+1。原写法每行 rn 跑一次 correlated subquery，
    -- 改成 LEFT JOIN 上游预先聚合的 latest 子查询（按 news_id 取一条最大 id）。
    LEFT JOIN (
        SELECT news_id, MAX(id) AS latest_id FROM ai_decisions
        WHERE is_sibling = 0 OR is_sibling IS NULL
        GROUP BY news_id
    ) latest ON latest.news_id = rn.id
    LEFT JOIN ai_decisions ad ON ad.id = latest.latest_id
    LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
"""

# v170.5+: 启动期预加载 now±15 天内所有事件 → 内存 dict → /api/events 直接查,
# 省掉每次 await DB + 重复 row→event 转换。60s TTL 增量刷新以收纳 0~60s 内新增。
_EVENTS_15D_LOCK = threading.Lock()
_EVENTS_15D_CACHE: List[Dict[str, Any]] = []
_EVENTS_15D_LAST_LOAD: float = 0.0       # time.monotonic() 上次成功写入时间
_EVENTS_15D_TTL_S: float = 60.0          # 60s 后下一次请求触发增量 reload
_EVENTS_15D_WINDOW_DAYS: int = 15        # ±15 天窗口
_EVENTS_15D_WINDOW_S: int = 15 * 86400    # 秒数
# v321：reload 单飞锁。reload 一次要几秒（SQL + 4000+ 行转换），
# 期间到达的并发请求**各自**都会触发一次 reload → 线程池打满 → 接口看起来挂死。
# 用一把"非阻塞抢锁"保证同一时刻只有一个线程在 reload，
# 其余请求直接用旧缓存（stale-while-revalidate）。
_EVENTS_15D_RELOAD_LOCK = threading.Lock()
_EVENTS_15D_WAIT_S: float = 30.0          # 首加载（无旧缓存）时最多等多久


def _trading_manager_enabled() -> bool:
    """修复：A1 — 显式 opt-in 才启用交易管理器子进程。"""
    raw = (os.getenv("TRADING_MANAGER_ENABLED", "0") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


_TRADING_MANAGER = TradingProcessManager(
    os.path.join(config.PROJECT_DIR, "NewsTrading.py"),
    config.PROJECT_DIR,
    config.BINANCE_LOG_PATH,
)

_SSE_QUEUES: List[asyncio.Queue] = []


async def _broadcast_sse(event: Dict[str, Any]) -> None:
    """把一条事件推给所有在线 SSE 订阅者。

    v505 修复：5 处 push（金价/WTI 行情、快速模拟结算、异常信号）一直在调这个函数，
    但定义早前被删掉了 —— 只要走到那条路径就是 NameError，前端反而收不到任何更新。
    现在用 _SSE_QUEUES 兜住：没有订阅者就直接丢，队列满就把那个订阅者摘掉，
    广播本身永不抛错、永不阻塞调用方。
    """
    if not _SSE_QUEUES:
        return
    dead: List[asyncio.Queue] = []
    for q in list(_SSE_QUEUES):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _SSE_QUEUES.remove(q)
        except ValueError:
            pass

from api_time import _now_iso  # v504 下沉，别名保持调用点不变

# 2026-11-17: _format_time 已下沉到 common/time_utils.format_time（19 处调用共用）。
# 这里保留同名别名，本地调用与既有 monkeypatch 都不受影响。
from common.time_utils import format_time as _format_time
from api_format import (
    _safe,
    _normalize_analysis_status,
    _parse_peer_reviews,
    _strip_hold_override,
    gate_reason_fallback,
    _row_to_event,
)


from api_time import _age_hours_of  # v504 下沉，别名保持调用点不变
def _events_15d_load_sync() -> List[Dict[str, Any]]:
    """一次性拉 [now-15d, now+15d] 内 is_noise=0 的事件, 转 row→event 形态 + 副品种。

    raw_news.timestamp 的落库格式 (v309 审计确认):
      · engine/ingest.py 写 engine.utils._ts() → 'YYYY-MM-DDTHH:MM:SS.mmm+08:00'
        ('T' 分隔, **北京**时间, 带 +08:00 偏移后缀)
      · api_server.py 写 api_server._now_iso() → 'YYYY-MM-DDTHH:MM:SS+08:00'
        (同为 'T' 分隔北京时间)
      · db.py 的 DEFAULT 是 datetime('now') → 'YYYY-MM-DD HH:MM:SS' (**UTC**, 空格分隔)
        —— 仅当某条 INSERT 没显式给 timestamp 时才会用到 (当前两个写入方都显式给了)

    v309 修复: 原来 cutoff 用的是**空格分隔**串 ('%Y-%m-%d %H:%M:%S'), 而实际落库
    是 'T' 分隔 → 两个串在第 11 位就分叉 (' '(0x20) vs 'T'(0x54)), 字典序不再等于
    时间序, **边界日**的行会被判错方向:
      · 下界 `>= lo`: 'T' 串恒大于 ' ' 串 → 边界日**早于** lo 的行被**多算进来**
      · 上界 `<= hi`: 同理 → 边界日**晚于** hi 的行被**漏掉**
    而且原 cutoff 带的是 +08:00 的**北京**语义, 与实际落库的北京墙钟只差形态,
    直接和 naive 串比还会有 8h 漂移 (若列里混有 UTC 空格串)。

    现在统一用 time_utils.sql_bj_ts_expr() 归一化:
      · 'T' 分隔 (北京) → 只把分隔符换成空格, 字面值即北京墙钟 ✔
      · ' ' 分隔 (UTC)  → datetime(col,'+8 hours') 平移成北京墙钟 ✔
    cutoff 也用 time_utils.epoch_to_bj_str() (北京墙钟 + 空格分隔), 形态一致 →
    字典序 == 时间序。
    """
    now = int(time.time())
    # ±15 天窗口用 epoch 秒展开 (北京无夏令时, 与 calendar 视角等价)。
    # cutoff 走 time_utils 唯一入口 → 北京墙钟 + 空格分隔, 与上面的归一化表达式同形。
    lo_str = time_utils.epoch_to_bj_str(now - _EVENTS_15D_WINDOW_S)
    hi_str = time_utils.epoch_to_bj_str(now + _EVENTS_15D_WINDOW_S)

    _ts_expr = time_utils.sql_bj_ts_expr("rn.timestamp")
    # v317 fix：`ad.is_sibling = 0` 会把**完全没有决策**的新闻整行丢掉 ——
    #   LEFT JOIN 出来的 ad 全是 NULL，`NULL = 0` 求值为 NULL（非真）→ 行被过滤。
    #   实测 281 条"待 AI 分析"新闻因此从 /api/events 里凭空消失。
    #   口径对齐 _fetch_event_rows（SSE 增量路径）：只要不是"副品种决策"就保留，
    #   即 ad 为 NULL（还没分析）或 ad 是主决策（is_sibling=0 / 遗留 NULL）。
    base_where = (
        "WHERE rn.is_noise = 0 AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL) "
        f"AND {_ts_expr} >= ? AND {_ts_expr} <= ?"
    )
    sql = f"{_EVENT_SELECT} {base_where} ORDER BY rn.id DESC"
    with db.get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (lo_str, hi_str)).fetchall()
        # 副品种同 in(_fetch_event_rows) 行为
        siblings_by_news: Dict[int, List[Dict[str, Any]]] = {}
        if rows:
            _nid_params = [int(r["news_id"]) for r in rows]
            placeholders = ",".join("?" for _ in _nid_params)
            sib = conn.execute(
                f"""SELECT ad.id AS decision_id, UPPER(ad.suggested_action) AS action,
                           ad.sentiment_score AS score, UPPER(ad.target_asset) AS asset,
                           ad.market_category, ad.reasoning AS reason,
                           ad.no_trade_reason, ad.trade_gate_reason, ad.evidence_confidence,
                           ad.exec_state,
                           qs.entry_price, qs.exit_price,
                           qs.max_price, qs.min_price, qs.settled, qs.verdict AS is_correct,
                           qs.pnl_pct AS forward_pnl, ad.news_id
                    FROM ai_decisions ad
                    LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                    WHERE ad.news_id IN ({placeholders})
                      AND ad.is_sibling = 1
                    ORDER BY ad.id ASC""",
                tuple(_nid_params),
            ).fetchall()
            for srow in sib:
                _sib_reason = re.sub(r"^\[.*?\]\s*", "", srow["reason"] or "")
                _sib_reason = re.sub(r"^\[副品种\s*[^\]]*\]\s*", "", _sib_reason)
                siblings_by_news.setdefault(int(srow["news_id"]), []).append({
                    "decision_id": srow["decision_id"],
                    "action": (srow["action"] or "HOLD").upper(),
                    "score": round(srow["score"], 2) if srow["score"] is not None else 0.0,
                    "target_asset": (srow["asset"] or "NONE").upper(),
                    "market_category": (srow["market_category"] or "OTHER").upper(),
                    "reason": _sib_reason[:80],
                    "no_trade_reason": srow["no_trade_reason"] or "",
                    # v489：这条方向单到底落地了没有（''=已建仓；其余=未落地原因）
                    "exec_state": srow["exec_state"] or "",
                    "trade_gate_reason": srow["trade_gate_reason"] or "",
                    "evidence_confidence": srow["evidence_confidence"],
                    "entry_price": srow["entry_price"],
                    "exit_price": srow["exit_price"],
                    "max_price": srow["max_price"],
                    "min_price": srow["min_price"],
                    "settled": srow["settled"] or 0,
                    "is_correct": srow["is_correct"] or "",
                    "forward_pnl": srow["forward_pnl"],
                })
    events = [_row_to_event(row) for row in rows]
    for ev in events:
        ev["sibling_decisions"] = siblings_by_news.get(int(ev["news_id"]), [])
    return events


def _events_15d_reload_worker() -> None:
    """后台刷新 15 天事件缓存（供 _events_15d_get_cached 派发）。

    独立线程 + 独立短事务：reload 要跑几秒（SQL + 4000+ 行转换），
    绝不能让请求线程等它。
    """
    # v481 修：这里漏了 global。`_EVENTS_15D_CACHE` 是原地 clear/extend（不需要 global
    # 也能生效），但 `_EVENTS_15D_LAST_LOAD = time.monotonic()` 是**重新绑定**——没有
    # global 就写进函数局部变量，模块级 LAST_LOAD 永远停在 0.0 → 缓存**永远判过期**
    # → 每次请求都再起一个后台 reload，全表 SQL 被反复跑（单飞锁只限制并发，不限制次数）。
    global _EVENTS_15D_LAST_LOAD
    try:
        fresh = _events_15d_load_sync()
        with _EVENTS_15D_LOCK:
            _EVENTS_15D_CACHE.clear()
            _EVENTS_15D_CACHE.extend(fresh)
            _EVENTS_15D_LAST_LOAD = time.monotonic()
    except Exception as exc:
        print(f"[EVENTS-15D] preload 失败(沿用旧缓存): {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    finally:
        _EVENTS_15D_RELOAD_LOCK.release()


def _events_15d_get_cached(limit: int) -> List[Dict[str, Any]]:
    """读缓存; TTL 过期时**后台**刷新，请求方立即拿旧缓存。

    v321 修复：**单飞 + stale-while-revalidate + 后台刷新**。
    原实现里 TTL 一过期，**每个**并发请求都会各自跑一次完整 reload
    （SQL + 4000+ 行 row→event 转换，数秒级）→ 线程池被打满 →
    /api/events 表现为长时间无响应（实测 90s 超时）。

    现在：
      · 同一时刻只允许一个 reload（_EVENTS_15D_RELOAD_LOCK 非阻塞抢锁）；
      · reload 丢到**后台线程**跑，请求线程不等待 → 永远秒回；
      · 请求拿到的可能是"略旧"的缓存（最多差一个 TTL=60s），
        对前端的事件列表完全可接受；
      · 只有"连旧缓存都没有"（首次加载）时才等待刷新完成，最多 _EVENTS_15D_WAIT_S。
    reload 失败（DB 锁等）时沿用旧缓存，语义与原来一致。
    """
    global _EVENTS_15D_CACHE, _EVENTS_15D_LAST_LOAD
    with _EVENTS_15D_LOCK:
        stale = (not _EVENTS_15D_CACHE
                 or (time.monotonic() - _EVENTS_15D_LAST_LOAD) >= _EVENTS_15D_TTL_S)
    if stale:
        if _EVENTS_15D_RELOAD_LOCK.acquire(blocking=False):
            # 抢到刷新权 → 丢后台线程跑，请求方不等
            try:
                threading.Thread(
                    target=_events_15d_reload_worker,
                    daemon=True,
                    name="events15d-reload",
                ).start()
            except Exception:
                _EVENTS_15D_RELOAD_LOCK.release()
        with _EVENTS_15D_LOCK:
            has_cache = bool(_EVENTS_15D_CACHE)
        if not has_cache:
            # 首次加载：无缓存可退让，最多等 _EVENTS_15D_WAIT_S 秒
            deadline = time.monotonic() + _EVENTS_15D_WAIT_S
            while time.monotonic() < deadline:
                time.sleep(0.05)
                with _EVENTS_15D_LOCK:
                    if _EVENTS_15D_CACHE:
                        break
    with _EVENTS_15D_LOCK:
        # 已是 rn.id DESC 排序; 限位即 slice 前 N
        return list(_EVENTS_15D_CACHE[:max(1, int(limit))])


async def _fetch_event_rows(news_ids: List[int] | None = None, limit: int | None = None, lo_ts: Optional[int] = None, hi_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    params: List[Any] = []
    where = "WHERE rn.is_noise = 0"
    if news_ids is not None:
        if not news_ids:
            return []
        where += f" AND rn.id IN ({','.join('?' for _ in news_ids)})"
        params.extend(news_ids)
    # v362: 按 UTC 秒窗口过滤（与 /api/events/today 同口径，走 raw_news.ts INTEGER）
    if lo_ts is not None:
        where += " AND rn.ts >= ?"
        params.append(lo_ts)
    if hi_ts is not None:
        where += " AND rn.ts <= ?"
        params.append(hi_ts)
    sql = f"{_EVENT_SELECT} {where} ORDER BY rn.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(sql, params)
        rows = await cursor.fetchall()
        # ── 多品种：同 news 的副品种行（is_sibling=1）补进主事件 ──
        if rows:
            _nid_params = [int(r["news_id"]) for r in rows]
            placeholders = ",".join("?" for _ in _nid_params)
            sib = await connection.execute(
                f"""SELECT ad.id AS decision_id, UPPER(ad.suggested_action) AS action,
                           ad.sentiment_score AS score, UPPER(ad.target_asset) AS asset,
                           ad.market_category, ad.reasoning AS reason,
                           ad.no_trade_reason, ad.trade_gate_reason, ad.evidence_confidence,
                           ad.exec_state,
                           qs.entry_price, qs.exit_price,
                           qs.max_price, qs.min_price, qs.settled, qs.verdict AS is_correct,
                           qs.pnl_pct AS forward_pnl, ad.news_id
                    FROM ai_decisions ad
                    LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                    WHERE ad.news_id IN ({placeholders})
                      AND ad.is_sibling = 1
                    ORDER BY ad.id ASC""",
                tuple(_nid_params),
            )
            sib_rows = await sib.fetchall()
            await sib.close()
            siblings_by_news: Dict[int, List[Dict[str, Any]]] = {}
            for srow in sib_rows:
                # reason 落库形如 "[label] [副品种 XAU] 内容" → 剥两段前缀留正文
                _sib_reason = re.sub(r"^\[.*?\]\s*", "", srow["reason"] or "")
                _sib_reason = re.sub(r"^\[副品种\s*[^\]]*\]\s*", "", _sib_reason)
                siblings_by_news.setdefault(int(srow["news_id"]), []).append({
                    "decision_id": srow["decision_id"],
                    "action": (srow["action"] or "HOLD").upper(),
                    "score": round(srow["score"], 2) if srow["score"] is not None else 0.0,
                    "target_asset": (srow["asset"] or "NONE").upper(),
                    "market_category": (srow["market_category"] or "OTHER").upper(),
                    "reason": _sib_reason[:80],
                    "no_trade_reason": srow["no_trade_reason"] or "",
                    # v489：这条方向单到底落地了没有（''=已建仓；其余=未落地原因）
                    "exec_state": srow["exec_state"] or "",
                    "trade_gate_reason": srow["trade_gate_reason"] or "",
                    "evidence_confidence": srow["evidence_confidence"],
                    "entry_price": srow["entry_price"],
                    "exit_price": srow["exit_price"],
                    "max_price": srow["max_price"],
                    "min_price": srow["min_price"],
                    "settled": srow["settled"] or 0,
                    "is_correct": srow["is_correct"] or "",
                    "forward_pnl": srow["forward_pnl"],
                })
        await cursor.close()
    events = [_row_to_event(row) for row in rows]
    # 副品种挂到对应主事件（主行已由 _EVENT_SELECT 排除 is_sibling=1，必是主品种行）
    for ev in events:
        ev["sibling_decisions"] = siblings_by_news.get(int(ev["news_id"]), [])
    return events


# v2026.10.23: db_watcher 每轮循环都 aiosqlite.connect(...)，而 aiosqlite 每次 connect 都会
# 新建一个线程；Thread.start() 会阻塞调用方直到新线程真正跑起来（threading.py 里
# _started.wait()）—— 在事件循环线程上调用等于直接把 loop 卡住（看门狗实测单次最长 5s，
# 窗口内累计 62s，是修复完报价链路后的第二大阻塞源）。
# 这几个函数都是只读查询，改成在线程里用普通 sqlite3 走，语义与结果完全一致，
# 同时顺带省掉每轮新建线程的开销。
def _query_rows_sync(sql: str, params: tuple = ()) -> List[Any]:
    conn = db.get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def _query_rows(sql: str, params: tuple = ()) -> List[Any]:
    return await asyncio.to_thread(_query_rows_sync, sql, params)


async def _fetch_change_cursors() -> tuple[int, int]:
    rows = await _query_rows(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM raw_news),"
        "       (SELECT COALESCE(MAX(id), 0) FROM ai_decisions)"
    )
    if not rows:
        return 0, 0
    return int(rows[0][0]), int(rows[0][1])


async def _fetch_incremental_news_ids(raw_after: int, decision_after: int) -> List[int]:
    rows = await _query_rows(
        """
        SELECT id AS news_id FROM raw_news WHERE id > ? AND is_noise = 0
        UNION
        SELECT news_id FROM ai_decisions WHERE id > ?
        """,
        (raw_after, decision_after),
    )
    return [int(row[0]) for row in rows]


async def _fetch_event_snapshot() -> Dict[int, tuple[str, int | None]]:
    """全量事件快照 —— 与 _row_to_event 产出的 (status, decision_id) **逐字段同口径**。

    v317 fix：旧实现用 `MAX(ad.id)`（跨**所有**决策取最大，含副品种行）且直接拿
    raw_news.status 原值，于是和事件载荷对不上：
      · decision_id：快照取到副品种决策 id，载荷取主决策 id → 1219 行不一致
      · status：快照拿原始状态，载荷走 _normalize_analysis_status → 216 行不一致
    db_watcher 的对账逻辑是 `snapshot.get(id) != state`，不一致的行会被判成"状态变了"
    → 每 5 秒全量重推 1340 条事件（纯噪音，且让前端反复重渲染）。

    现在两边都走：主决策（is_sibling=0 或遗留 NULL）取 MAX(id)，
    status 走同一个 _normalize_analysis_status —— 口径唯一，永不再漂移。
    """
    rows = await _query_rows(
        """
        SELECT rn.id, rn.status, rn.is_noise, rn.gate_unavailable,
               (SELECT MAX(ad.id) FROM ai_decisions ad
                 WHERE ad.news_id = rn.id
                   AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL)) AS did
        FROM raw_news rn
        WHERE rn.is_noise = 0
        """
    )
    return {
        row[0]: (_normalize_analysis_status(
            row[1], row[4] is not None,
            is_noise=bool(row[2] or 0),
            gate_pending=int(row[3] or 0) == 1,
        ), row[4])
        for row in rows
    }


# v170+ 数据源加固：黄金 tick 入库异常节流（避免 1Hz 轮询刷爆日志）
_gold_tick_last_err_log_ts: float = 0.0
_wti_tick_last_err_log_ts: float = 0.0
_TICK_LOG_THROTTLE_SEC = 60.0


def _gold_tick_log_throttle() -> bool:
    """返回 True 表示需要打印（即距上次打印已超阈值）；调用方据此抑制重复日志。"""
    global _gold_tick_last_err_log_ts
    now = time.time()
    if now - _gold_tick_last_err_log_ts < _TICK_LOG_THROTTLE_SEC:
        return True  # 已节流
    _gold_tick_last_err_log_ts = now
    return False


def _wti_tick_log_throttle() -> bool:
    global _wti_tick_last_err_log_ts
    now = time.time()
    if now - _wti_tick_last_err_log_ts < _TICK_LOG_THROTTLE_SEC:
        return True
    _wti_tick_last_err_log_ts = now
    return False


# -- Gold price helpers ----------------------------------------------------

_last_gold_price: float | None = None

class _MT5State:
    def __init__(self):
        self.ok = False
        self.init_done = False

_mt5 = _MT5State()

def _mt5_init() -> None:
    if _mt5.init_done:
        return
    _mt5.init_done = True
    try:
        import MetaTrader5 as mt5_mod
        if not mt5_mod.initialize():
            print("[MT5] initialize() returned False")
            return
        _mt5.ok = True
        print("[MT5] connected — XAUUSD + WTIUSD — tick streaming active")
    except ImportError:
        print("[MT5] MetaTrader5 package not installed")
    except Exception as e:
        print(f"[MT5] init error: {type(e).__name__}: {e}")

def _mt5_read_tick() -> float | None:
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("XAUUSD")
        if tick and tick.bid and 500 < tick.bid < 10000:
            return tick.bid
        return None
    except Exception:
        return None

def _mt5_recheck() -> None:
    _mt5.ok = False
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("XAUUSD")
        if tick and tick.bid:
            _mt5.ok = True
    except Exception:
        pass

# _fetch_sina_xau / _fetch_eastmoney_xau 统一由 engine.prices 提供（见文件头 import）。
# 注意：engine.prices 版本超时 5s 且内部吞异常返回 None，与本模块 _http_fetch_gold 的
# try/except + 500<p<10000 过滤组合后行为等价。
def _http_fetch_gold() -> tuple[float | None, str]:
    for name, fn in [("Sina", _fetch_sina_xau), ("EastMoney", _fetch_eastmoney_xau)]:
        try:
            p = fn()
            if p is not None and 500 < p < 10000:
                return p, name
        except Exception:
            pass
    return None, ""


# XAU/WTI 24h 涨跌幅：EastMoney f170 字段，30s 缓存（涨跌幅变化慢，不必 1s 拉）
_change_cache: Dict[str, Dict[str, Any]] = {}

# v2026.10.23: 原来每次调用都 build_opener()，会新建 SSL context 并 load_default_certs()
# 重新读一遍系统 CA（LibreSSL 下实测单次可达数秒，直接阻塞事件循环）。opener 无状态，
# 建一次复用即可；ProxyHandler({}) 仍是"绕过代理"语义，与原来一致。
_EASTMONEY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_eastmoney_change(secid: str) -> Optional[float]:
    """EastMoney 涨跌幅%（f170）。失败返回 None。直连（与 engine.prices 一致，绕过代理）。

    v2026.10.08: 8s → 2s 硬截断（movers 端到端超时不能被单源拖到 16s；网络不通直接 None 走 Binance 兜底）。
    """
    try:
        req = urllib.request.Request(
            f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f170",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        resp = _EASTMONEY_OPENER.open(req, timeout=2)
        body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or {}
        # v252.2: 字段缺失/为 0.0 时返回 None — 0.0 是有效涨跌幅但 EastMoney 字段缺失
        # 也会回 0,无法区分。返回 None 让上游兜底走 Binance K 线。
        raw = data.get("f170")
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v == v else None  # NaN 过滤
    except Exception:
        return None


def _cached_change24h(asset: str) -> Optional[float]:
    """带 30s 缓存的 24h 涨跌幅（XAU/WTI）。

    优先 EastMoney f170；为 None 时兜底用 Binance 1d K 线（昨日收盘 → 现价）计算。

    v252.2: 失败返回 None — 不再 [value or 0.0] 假装 0% 涨跌,  # EXEMPT-HONESTY: docstring 解释
    前端按 "—" 显示。
    """
    now = time.monotonic()
    hit = _change_cache.get(asset)
    if hit and now - hit["ts"] < 30.0:
        return hit["value"]
    secid = "113.USDXAU" if asset == "XAU" else "113.USDWTI"
    value = _fetch_eastmoney_change(secid)
    if value is None:
        value = _fetch_eastmoney_change("113.CL00Y") if asset == "WTI" else None
    # 兜底：Binance 1d K 线算 24h 涨跌（今日进行中的 bar close vs 昨日 close）
    if value is None:
        try:
            bn = _fetch_binance_klines(asset, "1d")
            if bn and len(bn[0]) >= 2:
                prev_close = float(bn[0][-2]["close"])
                last_close = float(bn[0][-1]["close"])
                if prev_close > 0:
                    value = (last_close - prev_close) / prev_close * 100
        except Exception:
            value = None
    # v252.2: value 是 None 时不强制转 0,保持 None 让前端按 "—" 显示。
    # 缓存也存 None,30s 内不重复请求。
    _change_cache[asset] = {"ts": now, "value": value}
    return value


# v2026.10.23 — XAU/WTI 报价链路的事件循环阻塞治理
# 起因（py-spy 在 macOS 需 root 不可用，改用看门狗采样主线程栈定位）：/health p99 达
# 6-8s，而 /health 自身零 DB。归因全部集中在 _broadcast_gold / _broadcast_wti：
#   1) db.retry_on_busy(timeseries.record_market_snapshot) 直接跑在事件循环线程，
#      5s busy_timeout × 4 次尝试最坏 ~20s（XAU+WTI 相邻执行 → 实测单次连续阻塞 38s）；
#   2) _cached_change24h 是同步 HTTPS（EastMoney 2s 超时 + Binance 兜底）。
# 两者合计占采样窗口内事件循环阻塞总时长的绝大部分。
# 方案：走专用线程池（不与默认池抢 worker），写库单 worker 串行以保证顺序；
# 在途计数封顶做背压 —— engine 长期持写锁时丢弃新 tick，避免队列无界增长。
# 正常无争用时在途恒为 1，tick 密度与原来一致（仍然每 tick 都写）。
_QUOTE_NET_EXEC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote-net")
# 写库单独一个池且只有 1 个 worker：market_ticks 写入保持严格串行（与改造前"事件循环
# 上顺序执行"语义一致），避免 XAU/WTI 两个写并发抢 SQLite 写锁放大 busy 重试。
_QUOTE_DB_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quote-db")
# 预热：ThreadPoolExecutor 的第一次 submit 会在**调用线程**上做 Thread.start()，而
# Thread.start() 要阻塞到新线程真正跑起来（threading._started.wait()）。若把这笔一次性
# 开销留到第一个 tick，就等于又在事件循环线程上卡一下（测试里实测可达 450ms，且随机器
# 负载放大）。import 期事件循环还没启动，在这里付掉。
for _warm in range(2):
    _QUOTE_NET_EXEC.submit(lambda: None)
_QUOTE_DB_EXEC.submit(lambda: None)
_TICK_MAX_PENDING = 60
_tick_pending: Dict[str, int] = {"XAU": 0, "WTI": 0}
_tick_pending_lock = threading.Lock()


def _write_tick_sync(asset: str, payload: Dict[str, Any]) -> None:
    """线程内执行 tick 落库；无论成败都要把在途计数减回去。

    v440：改为**入队**（单写线程顺序落库），不再在请求线程里 retry 抢锁。
    依据：2026-11-17 22:05 哨兵记录 `_write_tick_sync → record_market_snapshot`
    31.6s commit，几乎全是 retry_on_busy 的退避等待。tick 是采样数据，
    丢几条无所谓，不值得让它和引擎抢锁、把在途线程占满。
    """
    try:
        if not timeseries.enqueue_market_snapshot(payload):
            # 队列满/关闭 → 同步兜底，保证不丢采样
            db.retry_on_busy(lambda: timeseries.record_market_snapshot(payload))
    finally:
        with _tick_pending_lock:
            if _tick_pending.get(asset, 0) > 0:
                _tick_pending[asset] -= 1


async def _persist_tick(asset: str, price: float, change, src: str) -> None:
    """把实时报价写进 market_ticks。写库走专用线程，不阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    with _tick_pending_lock:
        if _tick_pending.get(asset, 0) >= _TICK_MAX_PENDING:
            return  # 背压：engine 长时间占写锁时丢弃本次 tick，避免积压无界增长
        _tick_pending[asset] = _tick_pending.get(asset, 0) + 1
    payload = {"items": [{
        "asset": asset, "price": float(price), "change24h": change,
        "source": (src or "http").lower(), "sourceCount": 1,
    }]}
    await loop.run_in_executor(_QUOTE_DB_EXEC, _write_tick_sync, asset, payload)


async def _change24h_async(asset: str) -> Optional[float]:
    """_cached_change24h 的异步封装：内含同步 HTTPS，必须放线程里跑。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_QUOTE_NET_EXEC, _cached_change24h, asset)


async def _broadcast_gold(price: float, src: str):
    global _last_gold_price
    # v2026.10.23: 24h 涨跌改为在线程里取，避免同步 HTTPS 占住事件循环；
    # 原先同一 tick 会调两次（第二次 30s 缓存命中），这里取一次即可，值相同。
    change = await _change24h_async("XAU")
    if _last_gold_price is None or abs(price - _last_gold_price) >= 0.01:
        _last_gold_price = price
        await _broadcast_sse({
            "type": "price_update",
            "asset": "XAU",
            "price": round(price, 2),
            "change24h": change,
            "ts": time.time(),
        })
    # v170+ 数据源加固：实时报价持久化到 market_ticks（之前只 SSE 推送不写库 → tick 22 根根因）。
    # 受 SQLite 写锁抖动影响时 retry_on_busy 退避；不阻塞 SSE，也不阻塞事件循环。
    try:
        await _persist_tick("XAU", price, change, src)
    except Exception as exc:
        if not _gold_tick_log_throttle():
            print(f"[GOLD] tick入库异常: {type(exc).__name__}: {str(exc)[:80]}", flush=True)


async def gold_http_watcher():
    loop = asyncio.get_running_loop()
    await asyncio.sleep(2)  # 错峰：避开启动瞬间全量任务并发
    while True:
        try:
            price, src = await loop.run_in_executor(None, _http_fetch_gold)
            if price is not None:
                await _broadcast_gold(price, src)
        except Exception:
            pass
        await asyncio.sleep(1.0)

async def gold_mt5_watcher():
    print("[GOLD] MT5 watcher started")
    # v2026.10.23: 三个 MT5 同步调用全部放线程。
    # 看门狗实测它们会在事件循环线程上各卡 ≈2s：
    #   _mt5_init  —— 首次 import MetaTrader5（扫描 sys.path）+ initialize() 连终端；
    #   _mt5_read_tick / _mt5_recheck —— symbol_info_tick() 是 C 层 IPC，终端无响应时直接挂住。
    # MT5 的 Python API 只是对终端 IPC 的薄封装，不绑事件循环，换线程安全。
    loop = asyncio.get_running_loop()
    while True:
        try:
            if _mt5.ok:
                price = await loop.run_in_executor(None, _mt5_read_tick)
                if price is not None and 500 < price < 10000:
                    await _broadcast_gold(price, "MT5")
                    await asyncio.sleep(0.1)
                    continue
                await loop.run_in_executor(None, _mt5_recheck)
                if not _mt5.ok:
                    print("[GOLD] MT5 terminal unreachable — HTTP only")
                await asyncio.sleep(1.0)
                continue
            if not _mt5.init_done:
                await loop.run_in_executor(None, _mt5_init)
            if not _mt5.ok:
                _mt5.init_done = False
                await asyncio.sleep(30)
                continue
        except Exception as e:
            print(f"[GOLD] MT5 error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


# -- WTI price helpers ----------------------------------------------------

_last_wti_price: float | None = None

def _mt5_read_wti_tick() -> float | None:
    try:
        import MetaTrader5 as mt5_mod
        tick = mt5_mod.symbol_info_tick("WTIUSD")
        if tick and tick.bid and 30 < tick.bid < 200:
            return tick.bid
        return None
    except Exception:
        return None

# 注意：WTI 抓取与 engine.prices._fetch_wti_price 不同源（EastMoney secid=113.USDWTI、
# 有效区间 30–200、超时 8s、异常向上抛由 _http_fetch_wti 捕获）——为保持行为不变，
# 这两个函数保留在本模块，不做去重。
def _fetch_sina_wti() -> float | None:
    req = urllib.request.Request(
        "https://hq.sinajs.cn/list=hf_CL",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
    )
    resp = urllib.request.urlopen(req, timeout=8)
    text = resp.read().decode("gbk", errors="replace")
    if '="' in text:
        return float(text.split('="')[1].split(",")[0])
    return None

def _fetch_eastmoney_wti() -> float | None:
    req = urllib.request.Request(
        "https://push2.eastmoney.com/api/qt/stock/get?secid=113.USDWTI&fields=f43",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    resp = urllib.request.urlopen(req, timeout=8)
    body = json.loads(resp.read().decode("utf-8"))
    return float(body["data"]["f43"]) / 100.0

def _http_fetch_wti() -> tuple[float | None, str]:
    for name, fn in [("Sina", _fetch_sina_wti), ("EastMoney", _fetch_eastmoney_wti)]:
        try:
            p = fn()
            if p is not None and 30 < p < 200:
                return p, name
        except Exception:
            pass
    return None, ""

async def _broadcast_wti(price: float, src: str):
    global _last_wti_price
    # v2026.10.23: 同 _broadcast_gold —— 同步 HTTPS 与同步写库都移出事件循环。
    change = await _change24h_async("WTI")
    if _last_wti_price is None or abs(price - _last_wti_price) >= 0.01:
        _last_wti_price = price
        await _broadcast_sse({
            "type": "price_update",
            "asset": "WTI",
            "price": round(price, 2),
            "change24h": change,
            "ts": time.time(),
        })
    # v170+ 数据源加固：同 XAU —— WTI 实时报价持久化到 market_ticks（之前 tick=93 的根因）。
    try:
        await _persist_tick("WTI", price, change, src)
    except Exception as exc:
        if not _wti_tick_log_throttle():
            print(f"[WTI] tick入库异常: {type(exc).__name__}: {str(exc)[:80]}", flush=True)


async def wti_http_watcher():
    print("[WTI] HTTP watcher started (Sina -> EastMoney)")
    loop = asyncio.get_running_loop()
    await asyncio.sleep(2)  # 错峰：避开启动瞬间全量任务并发
    while True:
        try:
            price, src = await loop.run_in_executor(None, _http_fetch_wti)
            if price is not None:
                await _broadcast_wti(price, src)
        except Exception:
            pass
        await asyncio.sleep(1.0)

async def wti_mt5_watcher():
    print("[WTI] MT5 watcher started")
    while True:
        try:
            if _mt5.ok:
                price = _mt5_read_wti_tick()
                if price is not None and 30 < price < 200:
                    await _broadcast_wti(price, "MT5")
                    await asyncio.sleep(0.1)
                    continue
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[WTI] MT5 error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


# -- DB watcher ------------------------------------------------------------

async def db_watcher() -> None:
    snapshot = await _fetch_event_snapshot()
    raw_cursor, decision_cursor = await _fetch_change_cursors()
    interval = config.NEWS_WATCH_INTERVAL_MS / 1000.0
    next_reconcile = time.monotonic() + 5.0
    await asyncio.sleep(1)  # 错峰：避开启动瞬间全量任务并发
    while True:
        try:
            current_raw, current_decision = await _fetch_change_cursors()
            changed_ids = await _fetch_incremental_news_ids(raw_cursor, decision_cursor)
            if changed_ids:
                events = await _fetch_event_rows(changed_ids)
                for event in reversed(events):
                    await _broadcast_sse(event)
                    snapshot[event["news_id"]] = (
                        str(event["analysis_status"]).upper(),
                        event["decision_id"],
                    )
            raw_cursor = max(raw_cursor, current_raw)
            decision_cursor = max(decision_cursor, current_decision)

            # 状态更新没有独立递增 ID；低频全量对账用于恢复丢失通知并保持最终一致。
            if time.monotonic() >= next_reconcile:
                current = await _fetch_event_snapshot()
                reconciled_ids = [
                    news_id for news_id, state in current.items()
                    if snapshot.get(news_id) != state
                ]
                if reconciled_ids:
                    events = await _fetch_event_rows(reconciled_ids)
                    for event in reversed(events):
                        await _broadcast_sse(event)
                snapshot = current
                next_reconcile = time.monotonic() + 5.0
        except Exception:
            pass
        await asyncio.sleep(interval)


# -- Kline helpers ---------------------------------------------------------

# 注：_mock_klines 已移除 —— 图表/K线一律取真实数据源，缺真实 K 线直接 503“行情缺失”，
# 禁止用随机数造假 K 线凑指标（此前 XAU/WTI 拿不到币安合约时就用 mock 假数据兜底）。


# -- Schema migration ------------------------------------------------------

def _migrate_schema() -> None:
    """Ensure schema is up to date. Fast path: schema ready → skip entirely.

    引擎可能正高频写库（INSERT/UPDATE 频繁）；已就绪时用只读检查跳过整个
    migrate，避免启动期在写锁竞争下等待 busy_timeout(30s)×多次重试。
    确实缺表/缺列时才执行 migrate，用短超时快速重试（最坏 ~13s，正常秒级）。
    migrate 后顺带预置因果链种子（幂等）——不依赖手工跑 seed 脚本，
    升级部署直接重启 API 因果链库即有内容。

    P2 启动钩子：migrate 完跑 run_p2_migration_if_needed()，
    默认 dry_run（只打日志，不动 DB）；真跑需设环境变量 P2_DROP_COLUMNS=1
    + 显式调用 real_run=True（双保险防误触）。

    v170-P0+: 启动钩子加 id_reconcile.ensure_schema() 给 5 链路表加
    trade_uuid / quarantine 列 + 建 alerts 表；幂等。
    """
    # 2026-11-16：启动前已由 scripts/prestart_migrate.py 完成迁移并写标记。
    # 标记新鲜 → 这里一个字都不写，只读。让"接口启动"不再被写锁拖住
    # （实测启动期写库曾把启动卡到 9 分钟，8000 端口全程不可用）。
    try:
        import startup_marker as _pre
        if _pre.marker_fresh():
            # v392：跳过整段迁移，但仍要补「最近几版新增的列」——否则新列永远不存在
            try:
                # v421：conn 原本只在下面第 1502 行才赋值，这里引用它必抛
                # UnboundLocalError，被 except 吞掉 → 每次启动都打一条"补列检查
                # 失败"，补列逻辑从未真正执行过（logs/bridge_20261116.log 有证）。
                conn = db.get_connection()
                _added = db.ensure_latest_columns(conn)
                if _added:
                    print(f"[API] schema 标记新鲜但补列: {', '.join(_added)}", flush=True)
                else:
                    print("[API] schema 标记新鲜 → 跳过启动期写库", flush=True)
            except Exception as _exc:
                print(f"[API] 补列检查失败（继续启动）: {type(_exc).__name__}: "
                      f"{str(_exc)[:80]}", flush=True)
            return
    except Exception:
        pass

    conn = db.get_connection()
    try:
        if db.schema_ready(conn):
            _seed_causal_defaults(conn)
            _auto_import_seed_config(conn)
            try:
                db.run_p2_migration_if_needed(conn)  # P2: 默认 dry_run
            except Exception as _p2exc:
                print(f"[API] P2 启动钩子跳过: {type(_p2exc).__name__}: {str(_p2exc)[:80]}",
                      flush=True)
            try:
                id_reconcile.ensure_schema(conn)
            except Exception as _ir_exc:
                print(f"[API] id_reconcile.ensure_schema 跳过: "
                      f"{type(_ir_exc).__name__}: {str(_ir_exc)[:80]}", flush=True)
            return
    finally:
        conn.close()
    for attempt in range(10):
        conn = db.get_connection()
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            db.migrate(conn)
            _seed_causal_defaults(conn)
            _auto_import_seed_config(conn)
            try:
                db.run_p2_migration_if_needed(conn)  # P2: 默认 dry_run
            except Exception as _p2exc:
                print(f"[API] P2 启动钩子跳过: {type(_p2exc).__name__}: {str(_p2exc)[:80]}",
                      flush=True)
            try:
                id_reconcile.ensure_schema(conn)
            except Exception as _ir_exc:
                print(f"[API] id_reconcile.ensure_schema 跳过: "
                      f"{type(_ir_exc).__name__}: {str(_ir_exc)[:80]}", flush=True)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 9:
                raise
            time.sleep(1.0)
        finally:
            conn.close()


def _auto_import_seed_config(conn=None) -> None:
    """补齐随包配置种子库（幂等；只在目标库缺行时写入，不覆盖本机改动）。

    部署时最容易漏的一步是导入 `seed_config.db`，漏了提示库（品种规则/经验/
    因果链/过滤词）就是空的，且症状隐蔽。这里在 API 启动时补一次；引擎侧
    `engine.py` 与 `init_db.py` 也有同样的调用（失败不阻断启动）。
    """
    try:
        import seed_restore
        own = conn is None
        if own:
            conn = db.get_connection()
        try:
            seed_restore.auto_import_from_seed(conn)
        finally:
            if own:
                conn.close()
    except Exception as exc:
        print(f"[SEED] 自动导入跳过（{type(exc).__name__}）", flush=True)


def _seed_causal_defaults(conn) -> None:
    """幂等预置因果链种子；失败静默跳过（不阻塞 API 启动）。"""
    try:
        import causal_chains
        seeded = causal_chains.seed_defaults(conn)
        if seeded.get("added"):
            print(f"[API] 因果链种子新增 {seeded['added']} 条，库内共 {seeded.get('total', 0)} 条")
    except Exception as exc:
        print(f"[API] 因果链种子预置跳过: {type(exc).__name__}: {str(exc)[:80]}")


# -- Lifespan --------------------------------------------------------------

# v342: 启动期线程错峰。2GB 机器上 20+ 个后台线程同时初始化，
# 每个线程各自开连接/预载缓存，内存峰值叠加 → 被 jetsam 以 -9 杀掉。
# 这里在进程启动后 90 秒窗口内，给每次 Thread.start() 前插入小延迟，
# 把峰值摊平成一条缓坡；90 秒后恢复原生行为，运行期零影响。
#
# v399 修正（真实递归 bug）：原来无条件
#     _ORIG_THREAD_START = threading.Thread.start
# 一旦本模块被导入两次（脚本方式 `api_server` + 包方式 `src_python.api_server`
# 在同一个进程里并存，pytest 与 uvicorn reload 都会发生），第二次捕获到的
# 是**已经包过的**包装器，于是 start() 调用自己 → RecursionError 无限递归。
# 实测后果：tests/test_security_p1_fixes.py 整组 5 failed + 5 errors，
# 且任何"先 import 一次再 import 一次"的进程都建不了线程。
# 修正：包装器打标记，只包一次。
if os.getenv("TRIDENT_STARTUP_STAGGER", "1") == "1" \
        and not getattr(threading.Thread.start, "_trident_staggered", False):
    _ORIG_THREAD_START = threading.Thread.start
    _STAGGER_T0 = time.time()
    _STAGGER_WIN_S = float(os.getenv("TRIDENT_STARTUP_STAGGER_WIN", "90"))
    _STAGGER_GAP_S = float(os.getenv("TRIDENT_STARTUP_STAGGER_GAP", "0.35"))

    def _staggered_thread_start(self):
        try:
            if time.time() - _STAGGER_T0 < _STAGGER_WIN_S:
                time.sleep(_STAGGER_GAP_S)
        except Exception:
            pass
        return _ORIG_THREAD_START(self)

    _staggered_thread_start._trident_staggered = True
    threading.Thread.start = _staggered_thread_start


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v393：记录启动耗时，避免"服务起不来"和"启动慢"混为一谈
    _t_boot = time.time()
    # P3 安全加固：CORS 启动期自检
    # 1. IS_FULL 模式下若 CORS_ALLOW_ORIGINS 仍默认 localhost → 警告
    # 2. 任何模式下出现 "*" → 立即拒绝启动（与 allow_credentials=True 不兼容）
    _boot_cors()

    # ── v367 双守护守卫 ────────────────────────────────────────────────
    # 背景：8100(bridge) 已经能接管本文件的 lifespan(BRIDGE_HOST_API_LIFESPAN=1)，
    # 前端也直连 8100。若此时 8000 又被拉起（人工/另一个会话/launchd），两套预热
    # 线程 + watcher 会同时写同一个 SQLite，内存翻倍、锁冲突飙升。
    # 这里在启动时探测 8100：只要 8100 在线且它确实在跑守护，本进程就跳过后台线程，
    # 只提供 HTTP 接口（请求路径不受影响）。TRIDENT_SKIP_DAEMONS_IF_BRIDGE=0 可关。
    _boot_detect_upstream(_t_boot)


    # v2026.10.23: 把默认线程池预热到满宽。
    # 背景：asyncio.to_thread / run_in_executor(None, ...) 用的是 loop 的默认 executor，
    # 而它默认懒建线程 —— 每次 submit 发现没有空闲 worker 就走 _adjust_thread_count() →
    # Thread.start()，而 Thread.start() 要阻塞到新线程真正跑起来（threading._started.wait()），
    # 这一步是发生在**事件循环线程**上的。看门狗实测 db_watcher 提交查询时正卡在这里。
    # 做法：让 N 个 worker 各自卡在 Event.wait() 上（于是每次 submit 必然发现无空闲
    # worker 而扩容），全部 submit 完再一次性放行。此时阻塞无害——还没对外服务。
    # 运行期池子已是满宽，就再也不会有 Thread.start() 落到事件循环线程上。
    await _boot_subsystems(_t_boot)
    # 诊断：DISABLE_ALL_WATCHERS=1 时不起任何后台任务（排查事件循环卡死用）
    if os.getenv("DISABLE_ALL_WATCHERS", "0") == "1":
        print(f"[STARTUP] ready in {time.time() - _t_boot:.1f}s (watchers off)", flush=True)
        yield
        return
    # 飞书金十接收器（自建应用长连接读群；阻塞 ws → daemon 线程，不阻塞事件循环）
    feishu_thread = None
    if config.FEISHU_LISTENER_ENABLED and not os.getenv("DISABLE_NEWS_WATCHER", "0") == "1":
        feishu_thread = threading.Thread(
            target=feishu_listener.listener_main_loop,
            name="feishu_listener",
            daemon=True,
        )
        feishu_thread.start()
    # v176.7: Tree News WebSocket → engine webhook 桥接（阻塞 ws → daemon 线程）。
    # 原设计：treenews_bridge.py 作为独立子进程，需 launchd / 手动 nohup 启动；
    # 改集成到 api_server lifespan：依赖 engine.py 的 :9000 webhook 已起，缺则静默重试。
    # DISABLE_TREE_NEWS=1 跳过；DISABLE_NEWS_WATCHER=1 同样跳过。
    treenews_thread = None
    if (not os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            and not os.getenv("DISABLE_TREE_NEWS", "0") == "1"):
        try:
            from feeds import treenews_bridge as _tnb
            treenews_thread = threading.Thread(
                target=_tnb.main,
                name="treenews_bridge",
                daemon=True,
            )
            treenews_thread.start()
        except Exception as exc:
            print(f"[TREENEWS] daemon 启动失败: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    tasks = [
        asyncio.create_task(db_watcher(), name="db_watcher"),
        asyncio.create_task(gold_http_watcher(), name="gold_http"),
        asyncio.create_task(gold_mt5_watcher(), name="gold_mt5"),
        asyncio.create_task(wti_http_watcher(), name="wti_http"),
        asyncio.create_task(wti_mt5_watcher(), name="wti_mt5"),
        *(
            [] if os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(news_source_watcher(), name="news_sources")]
        ),
        # 金十 MCP 独立高频采集（专属节奏；无 token 时静默退出）
        *(
            [] if os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(jin10_mcp_watcher(), name="jin10_mcp")]
        ),
        # 金十财经日历发布检测（公布 → 事件进实时信号台 + AI 分析）
        *(
            [] if os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(jin10_calendar_watcher(), name="jin10_calendar")]
        ),
        # 美股新闻（Yahoo 个股新闻 → 翻译入库 → AI 分析 → 美股模拟交易）
        *(
            [] if not config.STOCK_NEWS_ENABLED or os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(stock_news_watcher(), name="stock_news")]
        ),
        # Reuters 路透社（Google News RSS → 翻译入库 → AI 分析）
        *(
            [] if not config.REUTERS_NEWS_ENABLED or os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(reuters_news_watcher(), name="reuters_news")]
        ),
        # 多源 RSS 新闻（Yahoo Finance 官方 + FXStreet/MarketWatch 经 Google News 聚合）
        *(
            [] if not config.RSS_NEWS_ENABLED or os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(rss_news_watcher(), name="rss_news")]
        ),
        # 晴红·标卡 第三方聚合资讯（试用评估源；到点自动关闭，未配 key 静默退出）
        *(
            [] if not config.QINGHONG_NEWS_ENABLED or os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(qinghong_news_watcher(), name="qinghong_news")]
        ),
        # 诊断开关：DISABLE_FRED_WATCHER=1 时跳过 FRED 轮询（排查 CPU 满载用）
        *(
            [] if os.getenv("DISABLE_FRED_WATCHER", "0") == "1"
            else [asyncio.create_task(fred_watcher(), name="fred_watcher")]
        ),
        # v2026-11-16 三源增补：CBOE 尾部风险 / 稳定币供给 / 事件概率
        *(
            [] if os.getenv("DISABLE_EXTRA_SOURCES", "0") == "1"
            else [asyncio.create_task(extra_sources_watcher(), name="extra_sources")]
        ),
        # v170+ 数据源加固：Fear&Greed + Truth Social 市场情绪聚合
        *(
            [] if os.getenv("DISABLE_MARKET_SENTIMENT", "0") == "1"
            else [asyncio.create_task(market_sentiment_watcher(), name="market_sentiment")]
        ),
        # BLS 劳工统计局官方发布（CPI/非农/失业率一手源；无 key 时 watcher 静默退出）
        *(
            [] if not config.BLS_API_KEY or os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(bls_watcher(), name="bls_watcher")]
        ),
        *(
            [] if os.getenv("DISABLE_QUICK_SIM_WATCHER", "0") == "1"
            else [asyncio.create_task(quick_sim_watcher(), name="quick_sim")]
        ),
        # v176.7 P0-5: 增量扫描 missed_signal（每 5min 扫 BTC/ETH/SOL/GOLD/WTI）
        # 把已 settle 的 HOLD 决策对照 abnormal_signals，给漏检案例打标 1。
        # 维护 missed_signal_scan_state 表（每 asset last_decision_id 增量）。
        # DISABLE_MISSED_SIGNAL_WATCHER=1 跳过。
        *(
            [] if os.getenv("DISABLE_MISSED_SIGNAL_WATCHER", "0") == "1"
            else [asyncio.create_task(_missed_signal_scan_watcher(), name="missed_signal_scan")]
        ),
        asyncio.create_task(review_watcher(), name="review_watcher"),
        # 公司级财报日更：引擎自带调度（macOS TCC 挡住了 launchd 读 ~/Desktop）
        asyncio.create_task(equity_fundamentals_daily_watcher(), name="equity_daily"),
        # 基本面权重影子账本每日结算（v448）：讨回"0.08 到底有没有用"的证据
        *(
            [] if os.getenv("DISABLE_FUNDAMENTAL_SHADOW_WATCHER", "0") == "1"
            else [asyncio.create_task(fundamental_shadow_daily_watcher(), name="fund_shadow")]
        ),
        # Phase 2 异常签名（多因子 + 全板块异动）：多资产 3m K 线统一调起并落库 abnormal_signals。
        # 纯聚合 + 落库，零 LLM 调用；DISABLE_ABNORMAL_SIG_WATCHER=1 时跳过。
        *(
            [] if os.getenv("DISABLE_ABNORMAL_SIG_WATCHER", "0") == "1"
            else [asyncio.create_task(abnormal_signature_watcher(), name="abnormal_sig")]
        ),
        # 新闻数据整理：按平台分文档自动导出（与新闻源同开关，无新新闻自动跳过）
        *(
            [] if os.getenv("DISABLE_NEWS_WATCHER", "0") == "1"
            else [asyncio.create_task(news_export_watcher(), name="news_export")]
        ),
        # 引擎 / 服务自身健康心跳（影响所有判断的"心脏"通道）：
        # 每 30s 探测 engine_webhook (9000) + db 可写性 + api_server 自身，失败时把状态写进 source_status。
        # 前端 SourceStatusAlerts 组件据此弹 toast。**纯探测/写内存，0 LLM 调用，不影响任何模型推理。**
        asyncio.create_task(infra_health_watcher(), name="infra_health"),
        # v170: ForexFactory 多源宏观日历后台同步（每 30min）。
        # 与 engine/macro_calendar_notifier.py 的 jin10 主链路并行；FF 失败不阻断。
        *(
            [] if os.getenv("DISABLE_MACRO_FF_WATCHER", "0") == "1"
            else [asyncio.create_task(macro_calendar_ff.macro_calendar_ff_sync_loop(), name="macro_calendar_ff")]
        ),
        # v176.7 P1-2: K 线主动补全 watcher（每 30min 拉一次所有 asset × timeframe 组合）。
        # 解决 SOL 1h/4h/1d 等时间帧被前端从未访问导致 kline_bars 稀疏的问题：
        # 真实源成功即落库，失败静默退库内历史；零 LLM 调用，纯网络 + 落库。
        # DISABLE_KLINE_BACKFILL_WATCHER=1 跳过（默认开）。
        *(
            [] if os.getenv("DISABLE_KLINE_BACKFILL_WATCHER", "0") == "1"
            else [asyncio.create_task(_kline_backfill_watcher(), name="kline_backfill")]
        ),
        # v176.7 P3-A: signal_chain_archive 全链路快照补全 watcher（每 10min）。
        # P3 缺口: archive_signal_chain 函数定义存在但 lifespan 未挂，导致 signal_chain_archive=0。
        # 这里每 10min 扫最近 24h 的 ai_decisions，幂等 INSERT OR REPLACE。
        # DISABLE_CHAIN_ARCHIVE_WATCHER=1 跳过（默认开）。
        *(
            [] if os.getenv("DISABLE_CHAIN_ARCHIVE_WATCHER", "0") == "1"
            else [asyncio.create_task(_chain_archive_watcher(), name="chain_archive")]
        ),
        # v176.7 P3-B: reverse_attributions 反向归因 watcher（每 30min）。
        # P3 缺口: reverse_attributions 表存在但无 INSERT 路径 → 永远 0 行。
        # 这里扫最近 24h 的 abnormal_signals（异动点），对照 ±1h 内 ai_decisions，
        # 计算 coverage_ratio / missed / forward_win / forward_loss 写入 reverse_attributions。
        # DISABLE_REVERSE_ATTR_WATCHER=1 跳过（默认开）。
        *(
            [] if os.getenv("DISABLE_REVERSE_ATTR_WATCHER", "0") == "1"
            else [asyncio.create_task(_reverse_attribution_watcher(), name="reverse_attribution")]
        ),
    ]

    # ── v189: 反思 worker 自动循环 ──
    # 原 api_server 启动时未把反思 worker 加进 lifespan，导致 paper_reflection_worker /
    # structured_reflection_worker / gate_reflection_worker 三者均只在手动 endpoint 中
    # 被调用；launchd 也未跑它们。本补丁在 lifespan 中拉起：
    #   - ~~structured_reflection_worker (async，每 60s 扫一轮)~~
    #     → 2025-10 已转移到 engine/main.py 独家运行，api 这份会导致双份抢同一批
    #       pending（双倍 LLM 成本）。engine 重启不会立即拉起，但冷启动 hook
    #       `_rescue_deactivated_insights` 会跑一次性自愈 + reflection_worker
    #       每 30s 处理一批，所以不丢活儿。
    #   - paper_reflection_worker.daemon_loop (同步 5min，跑在独立线程)
    #   - gate_reflection_worker.daemon_loop (同步 30min，跑在独立线程)
    # 诊断开关 DISABLE_REFLECTION_WORKERS=1 时全部跳过（排查 LLM 慢/卡死用）
    _boot_reflection_workers()

    print(f"[STARTUP] ready in {time.time() - _t_boot:.1f}s", flush=True)
    yield
    await _boot_shutdown(tasks)




# ── lifespan 分段（2026-11-18 从 740 行拆出，同模块内联，不改调用顺序）──
# _boot_cors
def _boot_cors() -> None:
    """CORS 中间件 + 反爬虫/蜜罐：配置非法直接抛，早失败好过运行期才发现。"""
    try:
        origins = list(config.CORS_ALLOW_ORIGINS)
        if "*" in origins:
            raise RuntimeError(
                "CORS_ALLOW_ORIGINS contains '*' which is incompatible with "
                "allow_credentials=True. Set explicit origins in CORS_ALLOW_ORIGINS."
            )
        # 仅本地开发端口；full 模式视为配置遗漏
        _LOCAL_DEV_PORTS = {
            "http://localhost:3000", "http://127.0.0.1:3000",
            "http://localhost:3001", "http://127.0.0.1:3001",
            "http://localhost:3002", "http://127.0.0.1:3002",
            "http://localhost:3030", "http://127.0.0.1:3030",
        }
        if demo_mode.IS_FULL and all(o in _LOCAL_DEV_PORTS for o in origins):
            print(
                "[LIFESPAN] ⚠️  CORS_ALLOW_ORIGINS is still localhost-only in IS_FULL mode. "
                "Production deployments MUST set CORS_ALLOW_ORIGINS=https://your-domain.com "
                "in .env to avoid being lock-out from the dashboard.",
                flush=True,
            )
    except RuntimeError:
        raise  # hard fail on '*'
    except Exception as exc:
        print(f"[LIFESPAN] CORS self-check skipped: {type(exc).__name__}: {exc}", flush=True)



# ── lifespan 分段（2026-11-18 从 740 行拆出，同模块内联，不改调用顺序）──
# _boot_detect_upstream
def _boot_detect_upstream(_t_boot: float) -> None:
    """探测 8100(bridge) 是否托管本进程 —— 托管时跳过所有后台守护线程，避免双守护写冲突。"""
    try:
        _skip_daemons = False
        # v377: 当 lifespan 被 8100(bridge) 托管时，本进程**就是** 8100 ——
        # 原来的守卫会去探测 8100 端口，可那时 uvicorn 还没 bind，
        # 于是空等 8 轮 ×12s(实测白等 96s，bridge 启动 120s+ 的主因)。
        # 托管模式下直接跳过：既不需要等自己，也不需要跳过后台线程。
        _hosted_in_bridge = os.getenv("TRIDENT_HOSTED_IN_BRIDGE") == "1"
        if _hosted_in_bridge:
            print("[LIFESPAN] 本进程即 8100（托管模式）→ 跳过等待/守卫，直接起守护", flush=True)
        if not _hosted_in_bridge and os.getenv("TRIDENT_SKIP_DAEMONS_IF_BRIDGE", "1") != "0":
            _bridge_hosts = False
            for _p in (
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
            ):
                try:
                    with open(_p, "r", encoding="utf-8", errors="ignore") as _f:
                        for _line in _f:
                            if _line.strip().startswith("BRIDGE_HOST_API_LIFESPAN"):
                                _bridge_hosts = _line.split("=", 1)[1].strip() == "1"
                                break
                except Exception:
                    pass
            if _bridge_hosts:
                # v373: 原来用 /api/health 判定 —— 但 bridge 启动要 60s+,
                #   api 这时探活失败就自己也跑起全套 daemon, 结果**两套守护同时写库**,
                #   内存翻倍(实测 api 431MB + bridge 422MB)、锁冲突飙升。
                #   改成"端口是否已 bind": bridge 的 uvicorn 一 bind 就算在位;
                #   仍探不到则等 15s 再看一次, 还不行才自己托管(避免彻底没人采集)。
                import socket as _sock
                def _bridge_port_up() -> bool:
                    try:
                        with _sock.create_connection(
                                ("127.0.0.1", config.BRIDGE_PORT), timeout=1.5):
                            return True
                    except Exception:
                        return False
                _skip_daemons = _bridge_port_up()
                if not _skip_daemons:
                    # v375: 15s 不够 —— bridge 启动要 60s+, 实测 bridge 重启时
                    #   api 15s 复查失败就接管守护, 结果又变成双守护(api 飙到 641MB)。
                    #   改成 8 次 × 12s ≈ 96s 的耐心等待, 覆盖 bridge 完整启动窗口。
                    import time as _t
                    for _wait_i in range(8):
                        _t.sleep(12)
                        if _bridge_port_up():
                            _skip_daemons = True
                            break
                        print(f"[LIFESPAN] 等待 8100 就绪 ({_wait_i + 1}/8)...", flush=True)
        if _skip_daemons:
            print("[LIFESPAN] ⏭ 8100 已在线并接管守护 → 本进程(8000)跳过所有后台线程，"
                  "只提供接口（避免双守护写冲突）", flush=True)
            # v374: 但 /api/market/movers 是页面必点项, 冷算要 20s+(api 不再跑全量守护后
            #   缓存没人预热 → 用户看到 spinner)。这里只单独保留一个极轻的预热线程。
            try:
                def _movers_only_warm() -> None:
                    while True:
                        try:
                            _mv = _fetch_movers_from_bridge("24h")
                            if _mv:
                                _MOVERS_CACHE[_movers_cache_key("24h", 20, False)] = (
                                    time.monotonic(), _mv)
                        except Exception as _mwx:
                            print(f"[MOVERS-WARM] {type(_mwx).__name__}: {str(_mwx)[:60]}", flush=True)
                        time.sleep(90)
                threading.Thread(target=_movers_only_warm, name="movers-warm-only",
                                 daemon=True).start()
                print("[LIFESPAN] movers 单独预热线程已启动", flush=True)
                print(f"[STARTUP] ready in {time.time() - _t_boot:.1f}s (bridge 接管守护)", flush=True)
            except Exception as _mwexc:
                print(f"[LIFESPAN] movers 预热线程启动失败(忽略): {type(_mwexc).__name__}", flush=True)
            yield
            return
    except GeneratorExit:  # pragma: no cover
        raise
    except Exception as _gx:
        print(f"[LIFESPAN] 双守护守卫检查失败(忽略): {type(_gx).__name__}: {_gx}", flush=True)



# ── lifespan 分段（2026-11-18 从 740 行拆出，同模块内联，不改调用顺序）──
# _boot_subsystems
async def _boot_subsystems(_t_boot: float) -> None:
    """启动期各子系统预加载：WAL 守护、审计清理、模拟盘默认盘、事件 15d 预热、数据源注册、
    交易执行器、发现页预热、联通中心、关键词库、replay 快照刷新、movers 预热。
    每一项都独立 try/except，单个失败不影响整体启动。"""
    try:
        _max_def_workers = min(32, (os.cpu_count() or 1) + 4)
        _warm_release = threading.Event()

        def _warm_worker() -> None:
            _warm_release.wait(timeout=30.0)

        _warm_loop = asyncio.get_running_loop()
        _warm_pending = [_warm_loop.run_in_executor(None, _warm_worker)
                         for _ in range(_max_def_workers)]
        _warm_release.set()
        await asyncio.gather(*_warm_pending)
        print(f"[LIFESPAN] 默认线程池已预热: workers={_max_def_workers}", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] 默认线程池预热失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)

    # v189-4: 启动 WAL 自动 checkpoint 守护 — 防止 ai_call_stats 高频写
    # 导致 WAL 一直膨胀 (曾触发 db-wal=460MB). 每 60s 一次 PASSIVE checkpoint.
    try:
        db.start_wal_checkpoint_daemon(interval_seconds=60.0)
    except Exception as exc:
        print(f"[LIFESPAN] WAL checkpoint daemon 启动失败: {type(exc).__name__}: {str(exc)[:80]}")
    # P2: 启动 audit log 自动 purge 守护 — 防止 trident_audit.db 无界增长。
    # 默认 6 小时跑一次，保留 30 天（可由 TRIDENT_AUDIT_RETENTION_DAYS 调整）。
    # purge 内部 try/except，失败仅写 warning 不抛错，所以不需要这里的 except。
    try:
        import threading as _threading

        _audit_retention_days = int(os.getenv("TRIDENT_AUDIT_RETENTION_DAYS", "30"))
        _audit_purge_interval_h = float(os.getenv("TRIDENT_AUDIT_PURGE_INTERVAL_HOURS", "6"))

        def _audit_purge_loop() -> None:
            import time as _time
            from demo_audit import purge_old_records
            while True:
                try:
                    _time.sleep(_audit_purge_interval_h * 3600)
                    removed = purge_old_records(retention_days=_audit_retention_days)
                    if removed:
                        print(f"[AUDIT-PURGE] 删除 {removed} 条 > {_audit_retention_days} 天前的审计记录", flush=True)
                except Exception as _exc:  # pragma: no cover - 守护线程绝不退出
                    print(f"[AUDIT-PURGE-DAEMON] error: {type(_exc).__name__}: {_exc}", flush=True)

        if _audit_retention_days > 0:
            _t = _threading.Thread(target=_audit_purge_loop, name="audit-purge-daemon", daemon=True)
            _t.start()
            print(f"[LIFESPAN] audit purge daemon 已启动: retention={_audit_retention_days}d, interval={_audit_purge_interval_h}h", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] audit purge daemon 启动失败: {type(exc).__name__}: {str(exc)[:80]}")
    _migrate_schema()
    # v200+: 模拟盘默认开启 — 反思 / 经验 / 日报三大链路依赖 active_run,
    # 若 is_running=0 则 ai_decisions.paper_trading_run_id 全 NULL, 反思链全断.
    # 兜底开关 PAPER_TRADING_AUTO_START=0 时函数内直接跳过.
    try:
        paper_trading.ensure_default_running()
    except Exception as exc:
        print(f"[LIFESPAN] ensure_default_running 跳过: {type(exc).__name__}: {str(exc)[:80]}")
    # v170.5+: 启动期预加载 now±15d 事件到 _EVENTS_15D_CACHE，
    # /api/events 直接查内存 dict，跳过每次 await DB + row→event 转换。
    # 60s TTL 增量刷新，preload 失败不阻塞启动（首次 /api/events 命中时再尝试）。
    # v380: 原来只写了"不阻塞启动"，实际是裸 await——库 1.4GB + 采集器并发写时
    #   这次大查询能挂到分钟级，lifespan 不完成 → uvicorn 已 bind 却不响应,
    #   全站接口 000(实测 2026-11-16 页面全"加载中")。这里加硬超时，
    #   超时即走 except 分支, 语义仍是"首次 /api/events 时再加载"。
    try:
        n = await asyncio.wait_for(
            asyncio.to_thread(_events_15d_load_sync),
            timeout=float(os.getenv("EVENTS_PRELOAD_TIMEOUT_S", "8") or 8),
        )
        with _EVENTS_15D_LOCK:
            _EVENTS_15D_CACHE.clear()
            _EVENTS_15D_CACHE.extend(n)
            _EVENTS_15D_LAST_LOAD = time.monotonic()
        print(f"[LIFESPAN] _EVENTS_15D_CACHE 预热完成: {len(n)} 条 (窗口 ±{_EVENTS_15D_WINDOW_DAYS}d)", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] _EVENTS_15D_CACHE 预热失败(将在首次 /api/events 时重试): {type(exc).__name__}: {str(exc)[:80]}")
    # v189-7: Coinglass + LSR 中转 API watcher (daemon thread, 同步 urllib)
    # 不开 COINGLASS_ENABLED 或 key 缺失时静默跳过, 不影响主循环.
    # v176.7 P0-fix: 先设置 untracked 守护，再调 startup_self_check。
    # 否则 startup_self_check 内部会调 report(False, error="network ...")，覆盖守护。
    try:
        from feeds import source_status as _ss
        # 先设 untracked（守护前置）
        _ss.report(
            "coinglass_lsr", False,
            error="未启用（vip2.coinglass.site 网络层被阻，Python 3.14 ssl 与站点 TLS 握手不兼容）",
            detail="untracked — 网络恢复或换中转后恢复",
        )
        from feeds import coinglass_client as _cg_client
        skip_reason = _cg_client.startup_self_check()
        if skip_reason:
            print(f"[LIFESPAN] Coinglass watcher 跳过: {skip_reason}", flush=True)
        else:
            from feeds import coinglass_lsr as _cg_lsr
            _cg_thread = threading.Thread(
                target=_cg_lsr.start_watcher,
                daemon=True,
                name="coinglass-lsr-watcher-launcher",
            )
            _cg_thread.start()
            print("[LIFESPAN] coinglass_lsr watcher 已挂入 lifespan", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] coinglass_lsr watcher 挂入失败: {type(exc).__name__}: {str(exc)[:80]}")
    # v433（2026-11-18）：执行器自恢复。
    # 背景：api 每次重启（launchd kickstart / memory-guard 重启）后 trade_executor 不会
    # 自动拉起，status 显示 mode=paper 但 running=false，只有 keep_executor.sh 每 5 分钟
    # 兜一次底 —— 中间这几分钟恰好错过建仓窗口。这里在 lifespan 里按配置模式自动启动。
    try:
        _exch_mode = (os.getenv("TRIDENT_EXCHANGE_MODE") or "paper").strip().lower()
        if _exch_mode in ("paper", "live_dry"):
            from routers import exchange as _exch_router
            if _exch_router._TRADE_EXECUTOR_STATE.get("task") is None:
                await _exch_router._start_trade_executor(_exch_mode)
                print(f"[LIFESPAN] trade_executor 自启 mode={_exch_mode}", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] trade_executor 自启失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    # v345.8: 发现页三个重接口后台预热（asset-scout 68s / fed-impact 22s / cycle-scan 9s）
    #   不预热 → 每次首屏都要现算，页面 40-60s。预热后请求吃缓存。
    try:
        import discovery as _disc
        # v367: 间隔可配（默认仍 300s，不改既有行为）。这台 8G 机器上 asset-scout 单轮
        # 实测 68s，占空比 23% 且持续抢 GIL，把 /api/health 都能拖到 16s；
        # 运维可设 DISC_PREWARM_INTERVAL_S=900 把它降到 7%（发现页首屏多等一次预热）。
        _disc.start_disc_prewarm(float(os.getenv("DISC_PREWARM_INTERVAL_S", "300")) or 300.0)
        print("[LIFESPAN] 发现页重接口预热线程已启动", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] 发现页预热启动失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    # 信息源面板：预注册全部数据源清单（行情/宏观/新闻），采集方 report 时再更新状态
    try:
        source_status.register_known_sources()
        # 需求8：免费公开数据源总清单（含 KuCoin/BIS/CNN 等新增源 + 无公开 API 源如实标注）
        free_sources.register_known_sources()
        # v281 全局联通中心：预注册通知通道清单（飞书/Slack/Discord/邮件/微信）
        notification_channels.register_known_channels()
        # 已主动停用的源：立即上报状态，避免面板误显示为“未采集(灰)”
        # v2026.10.23: 用 areport（线程内落库）。原来同步 report() 在 lifespan 里跑，
        # 实测启动期被 engine 写锁卡住 ~28s（5 次 × 5s busy_timeout），启动窗口全程阻塞。
        if not config.REUTERS_NEWS_ENABLED:
            await source_status.areport(
                "reuters_news", False,
                error="已停用（Google News RSS 滞后2h + 与金十内容重复）")
        # v176.7：spdr_gld (SPDR GLD ETF 持仓吨数) 暂无免费稳定数据源（spdrgoldshares.com
        # 反爬虫 + etf.com Cloudflare + gold.org 需 JS 渲染），暂标 untracked。
        # 后续可接入 etfapi.io / Vickers 等付费 API。明确状态避免面板误判为 error。
        await source_status.areport(
            "spdr_gld", False,
            error="未接入（SPDR 官方反爬虫 + gold.org 需 JS；待 etfapi.io/Vickers 等付费 API）",
            detail="untracked — 接入即恢复",
        )
        # v176.7：ig (IG Markets 散户情绪) 凭据失效（identifier/password 错误率 100%）。
        # 暂标 untracked，待用户提供新凭据后恢复。明确状态避免面板误判为 error。
        await source_status.areport(
            "ig", False,
            error="未启用（IG identifier/password 凭据失效，待用户提供新凭据）",
            detail="untracked — 配置新凭据后恢复",
        )
        # v176.7：coinglass_lsr (Coinglass LSR 中转 API) 网络层被阻。
        # vip2.coinglass.site 在 Python 3.14 urllib3 与 LibreSSL 均 TCP RST/SSL EOF，
        # 非脚本问题，标 untracked。
        await source_status.areport(
            "coinglass_lsr", False,
            error="未启用（vip2.coinglass.site 网络层被阻，Python 3.14 ssl 与站点 TLS 握手不兼容）",
            detail="untracked — 网络恢复或换中转后恢复",
        )
        # v176.7：okx_swap.WTI-USDT-SWAP OI 在 OKX 不存在（原油无永续合约），
        # fetch_okx_open_interest 已短路 return None 不报 error，
        # 显式 untracked 守护防止历史 last_error 残留在面板误导。
        await source_status.areport(
            "okx_swap:WTI-USDT-SWAP", False,
            error="未启用（OKX 期货端无 WTI 永续合约，原油走 domestic_spot 现货价）",
            detail="untracked — OKX 期货端没有 WTI",
        )
    except Exception as exc:
        print(f"[SOURCES] 数据源清单预注册失败: {type(exc).__name__}: {str(exc)[:80]}")
    # 提示词库：启动时加载 filter_keywords（内置种子 + 人工增删）到 L1 过滤内存
    try:
        await asyncio.to_thread(realtime_filter.load_keywords_from_db)
    except Exception as exc:
        print(f"[FILTER] 关键词库加载失败: {type(exc).__name__}: {str(exc)[:80]}")
    # v2026.10.26: /api/replay/positions 持仓快照后台刷新提前拉起——
    # 否则第一个请求只能走「无网络快版」，页面要等 30-60s 才有完整浮盈亏。
    try:
        _replay_ensure_refresher()
        print("[LIFESPAN] replay 持仓快照后台刷新已启动", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] replay 后台刷新启动失败(忽略): {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    # v341: /api/market/movers 冷启动要 40 秒+（778KB、逐品种回查），
    # 缓存只救得了第二次请求。这里在后台每 120 秒预热一次，
    # 用户第一次打开页面就能命中热缓存，而不是干等。
    try:
        def _movers_warm_loop() -> None:
            interval = 120.0
            while True:
                _t0 = time.monotonic()
                try:
                    _MOVERS_CACHE["3m"] = (time.monotonic(), _movers_sync("3m"))
                except Exception as exc:
                    print(f"[MOVERS-WARM] 预热失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                # v367: 同 market/all —— 重算吃力就后退, 别把请求线程饿死
                _elapsed = time.monotonic() - _t0
                interval = min(interval * 2.0, 900.0) if _elapsed > 8.0 else 120.0
                time.sleep(interval)

        threading.Thread(target=_movers_warm_loop,
                         name="movers-warm", daemon=True).start()
        print("[LIFESPAN] movers 缓存预热已启动", flush=True)
    except Exception as exc:
        print(f"[LIFESPAN] movers 预热启动失败(忽略): {type(exc).__name__}: {str(exc)[:80]}", flush=True)



# ── lifespan 分段（2026-11-18 从 740 行拆出，同模块内联，不改调用顺序）──
# _boot_reflection_workers
def _boot_reflection_workers() -> None:
    """反思/周期/归因等后台 worker 的统一挂载（可被 DISABLE_REFLECTION_WORKERS=1 整体关掉）。"""
    if os.getenv("DISABLE_REFLECTION_WORKERS", "0") != "1":
        # v202-fix: structured_reflection_worker 已在 engine/main.py:96 独家挂载，
        # 这里删除重复挂载避免双份抢同一批 pending 浪费 LLM 调用。
        # try:
        #     from engine import structured_reflection as _sr
        #     tasks.append(asyncio.create_task(_sr.structured_reflection_worker(), name="structured_reflection"))
        #     print("[LIFESPAN] structured_reflection_worker 已挂入 lifespan")
        # except Exception as exc:
        #     print(f"[LIFESPAN] structured_reflection_worker 挂入失败: {type(exc).__name__}: {exc}")
        print("[LIFESPAN] structured_reflection_worker 由 engine/main.py 独家挂载（避免双份并行）")

        try:
            import paper_reflection_worker as _prw
            _prw_thread = threading.Thread(target=_prw.daemon_loop, kwargs={"interval_s": 300}, daemon=True, name="paper_reflection")
            _prw_thread.start()
            print("[LIFESPAN] paper_reflection_worker daemon (300s) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] paper_reflection_worker 启动失败: {type(exc).__name__}: {exc}")

        try:
            from review import gate_reflection_worker as _grw
            _grw_thread = threading.Thread(target=_grw.daemon_loop, kwargs={"interval_s": 1800}, daemon=True, name="gate_reflection")
            _grw_thread.start()
            print("[LIFESPAN] gate_reflection_worker daemon (1800s) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] gate_reflection_worker 启动失败: {type(exc).__name__}: {exc}")

        # v189-fix: LLM 深度反思 worker (5min/次, batch=5)
        # 旧实现只在手动 endpoint /api/v188/trade-reflections/enrich-pending 触发,
        # 导致 trade_reflections_v2.llm_depth_reflection 长期填充率 0%。
        try:
            import trade_reflection_llm as _trllm
            _trllm_thread = threading.Thread(
                target=_trllm.daemon_loop, kwargs={"interval_s": 300, "batch_size": 5},
                daemon=True, name="trade_reflection_llm",
            )
            _trllm_thread.start()
            print("[LIFESPAN] trade_reflection_llm daemon (300s, batch=5) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] trade_reflection_llm 启动失败: {type(exc).__name__}: {exc}")

        # v189-fix: review_agent 复盘 daemon (24h/次, scope=week)
        # 旧实现只在手动 endpoint /api/review/run 触发,
        # 导致 review_reports 长期停更、改进建议(improvements 字段)缺失。
        try:
            from review import review_agent as _ra
            _ra_thread = threading.Thread(
                target=_ra.daemon_loop, kwargs={"interval_s": 86400, "scope": "week"},
                daemon=True, name="review_daemon",
            )
            _ra_thread.start()
            print("[LIFESPAN] review_agent daemon (86400s, scope=week) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] review_agent daemon 启动失败: {type(exc).__name__}: {exc}")

        # L3.1 reflection_scheduler daemon (10min/次)
        # 跑 6 步流水线: 错误码聚合 → KG proposal → 告警(并自动 resolve 落到阈值以下的告警) →
        # 经验验证 → 反思周报。reflection_alert.check_and_alert_error_code 在此被定期调用,
        # 否则阈值回升到阈值以下时 E014/E020 告警永远不会自动关。
        try:
            from review import reflection_scheduler as _rsched
            _rsched_thread = threading.Thread(
                target=_rsched.daemon,
                kwargs={"interval": 600},
                daemon=True, name="reflection_scheduler",
            )
            _rsched_thread.start()
            print("[LIFESPAN] reflection_scheduler daemon (600s) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] reflection_scheduler 启动失败: {type(exc).__name__}: {exc}")

        # L3.4 meta_reflection_loop daemon (1h/次)
        # 对 level=1 的反思做"自循环反思": 检测 reasoning_logic_blindspot / no_baseline_new /
        # invalidated_history 等, 写 reflection_cycles 表并把 secondary_reflection_id 回写 v2。
        # 旧实现只通过手动 CLI 触发 → 几乎从没跑过(1 行)。挂入 lifespan 后, v232+ 元反思
        # 才能持续产出, reflection_cycles 表才不会再停滞。
        try:
            import meta_reflection_loop as _mrl
            _mrl_thread = threading.Thread(
                target=_mrl._daemon_loop,
                kwargs={"interval_s": 3600, "window_days": 7},
                daemon=True, name="meta_reflection_loop",
            )
            _mrl_thread.start()
            print("[LIFESPAN] meta_reflection_loop daemon (3600s) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] meta_reflection_loop 启动失败: {type(exc).__name__}: {exc}")

        # v190 #4: weekly_insight_apply daemon (7 天/次)
        # 把 active=1 AND kind=1 AND source_samples>=10 的 review_insights 推到
        # strategy_param_adjustments。threshold 高于 v182 reflection 的 5,
        # 避免「软启发」自动放大成参数调整。
        # v200+ Q5: 自适应门槛 — 数据稀疏 (total < 30) → 5; 充足 → 10.
        try:
            _conn0 = db.get_connection()
            _total_insights = _conn0.execute(
                "SELECT COUNT(*) AS n FROM review_insights WHERE active=1 AND kind=1"
            ).fetchone()["n"] or 0
            _conn0.close()
        except Exception:
            _total_insights = 0
        try:
            _adapt_min, _adapt_conf = config.adaptive_min_samples(int(_total_insights))
        except Exception:
            _adapt_min = int(getattr(config, "WEEKLY_INSIGHT_MIN_SAMPLES", 10) or 10)
            _adapt_conf = "normal"
        try:
            import weekly_insight_apply as _wia
            _wia_thread = threading.Thread(
                target=_wia._daemon_loop,
                kwargs={"min_samples": _adapt_min},
                daemon=True, name="weekly_insight_apply",
            )
            _wia_thread.start()
            print(f"[LIFESPAN] weekly_insight_apply daemon (604800s) 已启动线程, "
                  f"min_samples={_adapt_min} (adaptive, confidence={_adapt_conf}, "
                  f"total_insights={_total_insights})")
        except Exception as exc:
            print(f"[LIFESPAN] weekly_insight_apply 启动失败: {type(exc).__name__}: {exc}")

        # v199: SPA Apply daemon (10min/次)
        # 把满足阈值的 pending strategy_param_adjustments 推到 status='applied',
        # 并同步到 ai_decisions.applied_setting_changes (闭环跑通)。
        try:
            import spa_apply_worker as _saw
            _saw_thread = threading.Thread(
                target=_saw.daemon_loop,
                kwargs={"interval_s": int(getattr(config, "SPA_APPLY_INTERVAL_S", 600) or 600)},
                daemon=True, name="spa_apply_worker",
            )
            _saw_thread.start()
            print("[LIFESPAN] spa_apply_worker daemon (600s) 已启动线程")
        except Exception as exc:
            print(f"[LIFESPAN] spa_apply_worker 启动失败: {type(exc).__name__}: {exc}")

        # v226 P0-1: Fed Rate 异动推送 daemon（5 分钟/次）。
        # push_fed_anomaly_if_new() 内部已带去重（severity 升级允许重推），
        # 无 anomaly 时 silent。DISABLE_FED_PUSH=1 时跳过（调试用）。
        if os.getenv("DISABLE_FED_PUSH", "0") != "1":
            try:
                import fed_rate_expectation as _fre
                _fre_thread = threading.Thread(
                    target=_fre.daemon_loop,
                    kwargs={"interval_s": int(os.getenv("FED_PUSH_INTERVAL_S", 300))},
                    daemon=True, name="fed_rate_push",
                )
                _fre_thread.start()
                print(f"[LIFESPAN] fed_rate_push daemon "
                      f"(interval={os.getenv('FED_PUSH_INTERVAL_S', 300)}s) 已启动线程")
            except Exception as exc:
                print(f"[LIFESPAN] fed_rate_push 启动失败: {type(exc).__name__}: {exc}")

        # v276: Fed 事件事前通知 daemon(5 分钟/次, 30min/24h 窗口提醒)。
        # 默认开启; DISABLE_FED_PRE_NOTIFIER=1 关闭。
        if os.getenv("DISABLE_FED_PRE_NOTIFIER", "0") != "1":
            try:
                import fed_pre_event_notifier as _fpen
                _fpen_thread = threading.Thread(
                    target=_fpen.daemon_loop,
                    kwargs={"interval_s": int(os.getenv("FED_PRE_INTERVAL_S", 300))},
                    daemon=True, name="fed_pre_event_notifier",
                )
                _fpen_thread.start()
                print(f"[LIFESPAN] fed_pre_event_notifier daemon "
                      f"(interval={os.getenv('FED_PRE_INTERVAL_S', 300)}s) 已启动线程")
            except Exception as exc:
                print(f"[LIFESPAN] fed_pre_event_notifier 启动失败: "
                      f"{type(exc).__name__}: {exc}")

        # v277: FOMC 高等级事件强制升 5 星 daemon。
        # 启动时立即跑一次 + 每小时循环（FF/jin10 同步后被覆盖时自动恢复 5 星）。
        if os.getenv("DISABLE_FOMC_MINUTES_UPGRADE", "0") != "1":
            try:
                import fomc_minutes_upgrade as _fmu
                _fmu.sync_to_db(verbose=False)
                _fmu_thread = threading.Thread(
                    target=_fmu.daemon_loop,
                    kwargs={"interval_s": int(os.getenv("FOMC_UPGRADE_INTERVAL_S", 3600))},
                    daemon=True,
                    name="fomc_minutes_upgrade",
                )
                _fmu_thread.start()
                print(f"[LIFESPAN] fomc_minutes_upgrade daemon "
                      f"(interval={os.getenv('FOMC_UPGRADE_INTERVAL_S', 3600)}s) 已启动")
            except Exception as exc:
                print(f"[LIFESPAN] fomc_minutes_upgrade 启动失败: "
                      f"{type(exc).__name__}: {exc}")

        # v226 P0-2: yfinance 韩股 + 山寨币 行情拉取 daemon（5 分钟/次）。
        # 默认覆盖 000660.KS（海力士）+ SOL-USD/DOGE-USD/XRP-USD/ADA-USD/BNB-USD。
        # 数据落 external_indicators 表，供 decision_guard 趋势因子 + abnormal_detector 读取。
        # DISABLE_YFINANCE_DAEMON=1 跳过（调试 / 数据源受限时）。
        if os.getenv("DISABLE_YFINANCE_DAEMON", "0") != "1":
            try:
                from feeds import yfinance_feed as _yff
                _yff_thread = threading.Thread(
                    target=_yff.daemon_loop,
                    kwargs={
                        "interval_s": int(os.getenv("YFINANCE_INTERVAL_S", 300)),
                        "lookback_days": int(os.getenv("YFINANCE_LOOKBACK_DAYS", 60)),
                    },
                    daemon=True, name="yfinance_feed",
                )
                _yff_thread.start()
                print(f"[LIFESPAN] yfinance_feed daemon "
                      f"(interval={os.getenv('YFINANCE_INTERVAL_S', 300)}s, "
                      f"lookback={os.getenv('YFINANCE_LOOKBACK_DAYS', 60)}d) 已启动线程")
            except Exception as exc:
                print(f"[LIFESPAN] yfinance_feed daemon 启动失败: {type(exc).__name__}: {exc}")

        # v226 P1-2: 周期反转信号推送 daemon（5 分钟/次）。
        # 对 BTC/ETH/SOL/DOGE/XRP/ADA/BNB 调 cycle_analyst.predict_reversal()，
        # prob≥0.66 且 votes≥2 → engine.alerts.send_important_news。
        # 同一 (asset, direction) 5h 内不重复推。
        # DISABLE_REVERSAL_DAEMON=1 跳过。
        if os.getenv("DISABLE_REVERSAL_DAEMON", "0") != "1":
            try:
                import cycle_reversal_watcher as _crw
                _crw_thread = threading.Thread(
                    target=_crw.daemon_loop,
                    kwargs={"interval_s": int(os.getenv("REVERSAL_INTERVAL_S", 300))},
                    daemon=True, name="cycle_reversal_watcher",
                )
                _crw_thread.start()
                print(f"[LIFESPAN] cycle_reversal_watcher daemon "
                      f"(interval={os.getenv('REVERSAL_INTERVAL_S', 300)}s, "
                      f"prob_threshold={os.getenv('REVERSAL_PROB_THRESHOLD', 0.66)}, "
                      f"min_votes={os.getenv('REVERSAL_MIN_VOTES', 2)}) 已启动线程")
            except Exception as exc:
                print(f"[LIFESPAN] cycle_reversal_watcher 启动失败: {type(exc).__name__}: {exc}")



# ── lifespan 分段（2026-11-18 从 740 行拆出，同模块内联，不改调用顺序）──
# _boot_shutdown
async def _boot_shutdown(tasks: list) -> None:
    """优雅关闭：先停交易管理器，再 cancel + gather 所有后台 task，最后停 coinglass watcher。"""
    await asyncio.to_thread(_TRADING_MANAGER.stop)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # v189-7: 优雅关闭 Coinglass watcher (lifespan 退出前)
    try:
        from feeds import coinglass_lsr as _cg_lsr
        _cg_lsr.stop_watcher(timeout_s=3.0)
    except Exception as exc:
        print(f"[LIFESPAN] coinglass_lsr watcher 停止失败: {type(exc).__name__}: {str(exc)[:80]}")


# -- FastAPI app definition ------------------------------------------------

app = FastAPI(title="Trident Agent API", version="4.0.0", lifespan=lifespan)

def _include_flat(app_: FastAPI, router_: APIRouter) -> None:
    """把 APIRouter 的路由**摊平**进 app，而不是用 app.include_router。

    为什么：FastAPI 0.163 起，include_router 不再把子路由直接塞进 app.routes，
    而是包一个 `_IncludedRouter`（path 为空、真路由藏在 .original_router 里）。
    对 uvicorn 自己没影响，但 `bridge_server.py` 末尾那段「手工把 api_server 的
    路由挂进 8100」的循环只认带 .path/.methods 的 APIRoute —— 被包起来的路由
    在 8100 上会**静默消失**（不报错、不 404 提示，直接落到 catch-all）。

    所以这里按老语义逐个 append，保证「api_server 有的路径，bridge 挂载后也一定有」。
    要求 router 里的 path 已经是绝对路径（本项目的切片都是绝对路径，不传 prefix）。
    """
    for _r in router_.routes:
        app_.router.routes.append(_r)


# ── v300: 启动横幅 + 模式自检（让运维一眼看到当前是 full / demo / licensed）──
demo_mode.assert_full_guard_localhost("127.0.0.1")
demo_mode.print_startup_banner()
license_auth.startup_self_check()
# v430：顺带看护 9001（算法本体）。com.trident.master 在本机 bootstrap 会卡
# xpcproxy，只能靠手工实例；8000 是 launchd 托管的，重启后由它把 9001 拉起来。
try:
    import master_keeper as _mk
    _mk.ensure_keeper()
except Exception as _mk_exc:
    print("[MASTER-KEEPER] 未启动: %s" % _mk_exc)

app.add_middleware(
    CORSMiddleware,
    # P1 安全修复：使用环境变量白名单 + 显式 methods/headers 白名单
    # 默认 ALLOW_ORIGINS 在 config.py 已限定为 localhost:*（开发端口）
    # 生产部署务必设置 CORS_ALLOW_ORIGINS=https://your-domain.com
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    # 显式 methods 白名单（替代 "*"）— 防止 PUT/DELETE/OPTIONS 等未用方法被滥用
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    # 显式 headers 白名单（替代 "*"）— 仅放行业务必须的 header
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Trident-API-Key",      # 主鉴权 header
        "X-Requested-With",       # 防 CSRF（传统 header）
        "X-Forwarded-For",        # 反代传递客户端 IP
        "X-Trident-Watermark",    # demo 模式水印
    ],
    # 浏览器缓存 CORS 预检结果 1 小时（减少 OPTIONS 风暴）
    max_age=3600,
    # P1 安全修复：若 '*' 出现在 CORS_ALLOW_ORIGINS（错误配置），自动拒绝，
    # 因为 allow_credentials=True 不允许通配 origin。
    # CORSMiddleware 本身会校验，这里补一行断言以便运维启动时立即看到。
)


# ── v300: 体验模式中间件（限流 + 延迟 + 水印头 + 审计）──────────────────
@app.middleware("http")
async def trident_demo_middleware(request: Request, call_next):
    """每个响应都过这里：审计在所有模式下都生效，watermark 仅 demo/licensed。

    职责：
      1. **审计（全模式）**：记录到 ``trident_audit.db`` — 含 method/path/ip/key/status/latency
         full 模式也审计（之前 IS_FULL 直接 bypass 导致 P2 缺口）；高噪音端点 (health/visitor)
         跳过，避免 DB 爆炸。
      2. demo 模式限流（按 IP / API Key，60s 滑动窗口 + 每日配额）
      3. 注入水印头 ``X-Trident-Watermark``（仅 demo/licensed）
      4. demo 模式下注入 ``X-Trident-Mode: demo`` 头便于客户端调试
    """
    # P2 安全加固：审计即使在 full 模式也跑 (之前 IS_FULL 完全透传 → 审计缺失)。
    # 但 demo 模式的限流 / 水印 / 模式头仅 demo/licensed 生效，避免 full 模式额外副作用。
    path = request.url.path

    # 高噪音端点 → 不审计（不写入 DB，但中间件仍透传）。其他端点都审计。
    _AUDIT_SKIP_PREFIXES = (
        "/health",
        "/api/health",  # /api/health/public + /api/health/internal + /api/health/security
        "/api/visitor/",
        "/api/agent_behavior/",
        "/api/anti_bot/",
        "/api/heartbeat",
        "/favicon.ico",
        "/robots.txt",
    )
    should_audit = not any(path.startswith(p) for p in _AUDIT_SKIP_PREFIXES)

    # 取 client IP（兼容反代 X-Forwarded-For；与 _get_client_ip 同样的语义）
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else "unknown"))

    # API Key 元数据（不强制校验，只读取用于审计）
    api_key_id = None
    api_token = request.headers.get("X-Trident-API-Key")
    if api_token:
        meta = license_auth.verify_api_key(api_token)
        if meta:
            api_key_id = meta.get("name") or api_token[:12]

    # demo 模式：限流 + 延迟 + 水印
    if not demo_mode.IS_FULL:
        allowed, reason = demo_mode.check_rate_limit(client_ip, api_key_id)
        if not allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "reason": reason,
                         "message": "Demo 模式限流，请稍后再试"},
            )

    # 透传到业务
    t0 = time.time()
    wm = demo_mode.make_watermark(client_ip, api_key_id)
    try:
        response = await call_next(request)
    except Exception as e:
        if should_audit:
            try:
                # v361 P0: 审计是同步 sqlite 写, 直接在事件循环里写会把**所有**请求排队
                #   (实测 8 个并发请求全部 30s 超时, 连 /api/health 都排不进去)。
                _audit_enqueue(dict(
                    method=request.method, path=path,
                    client_ip=client_ip, api_key_id=api_key_id,
                    status=500, latency_ms=(time.time() - t0) * 1000,
                    watermark=wm, mode=demo_mode.TRIDENT_MODE, note=str(e)[:200],
                ))
            except Exception:
                pass  # 审计失败不影响错误传播
        raise

    # demo/licensed: 加水印 + 模式头
    if not demo_mode.IS_FULL:
        response.headers["X-Trident-Watermark"] = wm
        response.headers["X-Trident-Mode"] = demo_mode.TRIDENT_MODE
        if demo_mode.IS_DEMO:
            response.headers["X-Trident-Demo-RPM"] = str(demo_mode.DEMO_RPM)
            response.headers["X-Trident-Demo-Days"] = str(demo_mode.DEMO_HISTORY_DAYS)

    # P2 审计 (所有模式 + 过滤后的 path)
    if should_audit:
        try:
            _audit_enqueue(dict(
                method=request.method, path=path,
                client_ip=client_ip, api_key_id=api_key_id,
                status=response.status_code,
                latency_ms=(time.time() - t0) * 1000,
                watermark=wm, mode=demo_mode.TRIDENT_MODE, note="",
            ))
        except Exception as _audit_err:
            # 审计失败 → 记到 logger（不抛错，避免审计影响业务可用性）
            _logging.getLogger("trident.audit").warning(
                "audit_record_failed: %s path=%s", _audit_err, path,
            )

    return response


 # ── 结构切片（2026-11-16）：meta 域三条只读探针已搬到 routers/meta.py ──
 # 搬走的是 /api/health/security、/health、/metrics；逻辑逐字未改，
 # 只把 @app.get 换成 @router.get 并 include 回来。后续新闻/决策/行情等域照此推进。
from routers.meta import router as _meta_router
# 2026-11-18: 运维类后台任务（重置/备份/两个看门狗）已下沉到 api_maintenance。
# 这里接回同名函数 → `api_server._clear_trading_traces` 这类引用与 monkeypatch 照旧有效。
from api_maintenance import (
    _auto_backup_trading_data,
    _clear_trading_traces,
    _reverse_attribution_watcher,
    infra_health_watcher,
)
# 2026-11-18: 模型通道探测 + 健康度缓存已下沉到 model_probe（12 函数 / 539 行）。
# 函数与状态常量都接回同名 —— 状态是就地改的 dict/lock，import 回来仍是同一对象；
# 测试里的 srv._MODEL_HEALTH_CACHE.update(...) / srv._probe_dead_state_reset() 照旧有效。
from model_probe import (
    _ENGINE_HEALTH_STALE_SEC,
    _MODEL_HEALTH_CACHE,
    _MODEL_HEALTH_LAST_ERROR,
    _MODEL_HEALTH_LOCK,
    _MODEL_HEALTH_REFRESH_MIN_GAP,
    _MODEL_HEALTH_TTL,
    _PROBE_DEAD_STATE,
    _PROBE_DEAD_WINDOW_S,
    _PROBE_SAME_KEY_STAGGER,
    _QUOTA_EXHAUSTED_MARKERS,
    _classify_probe_failure,
    _ensure_model_health_thread,
    _funnel_overlay,
    _is_local_base,
    _kick_model_health_refresh,
    _model_health_refresh_loop,
    _model_health_skeleton,
    _probe_channel,
    _probe_dead_state_reset,
    _probe_model_channels,
    _probe_model_channels_now,
    _probe_specs_deduped,
)
_include_flat(app, _meta_router)

from routers.history import router as _history_router
_include_flat(app, _history_router)

from routers.review import router as _review_router
_include_flat(app, _review_router)

from routers.ai import router as _ai_router
_include_flat(app, _ai_router)

from routers.news import router as _news_router
_include_flat(app, _news_router)

from routers.admin import router as _admin_router
_include_flat(app, _admin_router)

from routers.discovery import router as _discovery_router
_include_flat(app, _discovery_router)

from routers.reflection import router as _reflection_router
_include_flat(app, _reflection_router)

from routers.decisions import router as _decisions_router
_include_flat(app, _decisions_router)

from routers.obs import router as _obs_router
_include_flat(app, _obs_router)

from routers.views import router as _views_router
_include_flat(app, _views_router)

from routers.experience import router as _experience_router
_include_flat(app, _experience_router)

from routers.market import router as _market_router
_include_flat(app, _market_router)

from routers.ops import router as _ops_router
_include_flat(app, _ops_router)

from routers.llm import router as _llm_router
_include_flat(app, _llm_router)

from routers.coinglass import router as _coinglass_router
_include_flat(app, _coinglass_router)

from routers.exchange import router as _exchange_router
_include_flat(app, _exchange_router)

from routers.portal import router as _portal_router
_include_flat(app, _portal_router)

from routers.macro import router as _macro_router
_include_flat(app, _macro_router)

from routers.beliefs import router as _beliefs_router
_include_flat(app, _beliefs_router)

from routers.strategies import router as _strategies_router
_include_flat(app, _strategies_router)
from routers.strategies import _strategy_or_404  # 回测/优化器端点仍在入口文件里用

from routers.causal import router as _causal_router
_include_flat(app, _causal_router)

from routers.smart_money import router as _sm_router
_include_flat(app, _sm_router)

# 知名投资人 13F 持仓（SEC 公开季度申报，只读；不是链上聪明钱）
from routers.institutional import router as _inst_router
_include_flat(app, _inst_router)

from routers.analysis_process import router as _ap_router
_include_flat(app, _ap_router)

# v414: 分析产能体检（决策终态分布 + 重试清单，只读）
from routers.analysis_yield import router as _analysis_yield_router
_include_flat(app, _analysis_yield_router)

# v422：标的路由诊断（未识别 TOP 榜 + 别名表），只读；不动决策链
from routers.targets import router as _targets_router
_include_flat(app, _targets_router)

# v422：信号可执行卡片（止损/仓位/目标），只读
from routers.actionable import router as _actionable_router
_include_flat(app, _actionable_router)

# v423：成本–持有期匹配（"到底能不能赚钱"的复检器），只读
from routers.profit import router as _profit_router
_include_flat(app, _profit_router)

# v424：结构性指标优势体检（资金费率/持仓/多空比），只读
from routers.edge import router as _edge_router
_include_flat(app, _edge_router)

# v448：开仓闸（位置分位 / 同向确认），默认影子模式、只读端点
from routers.guards import router as _guards_router
_include_flat(app, _guards_router)

# v449：单押注检测（相关性聚簇）+ 正期望择优/停单，只读
from routers.bets import router as _bets_router
_include_flat(app, _bets_router)

# v490 P1-2：时刻→事件反向检索（需求 #6：给定时刻+品种，反查什么事件影响涨跌），只读
from routers.event_reverse import router as _event_reverse_router
_include_flat(app, _event_reverse_router)

# v461：样本工厂（5m 真实结算 + 时间分段），只读
from routers.factory import router as _factory_router
_include_flat(app, _factory_router)

# v462：模拟盘账本（持仓标记 / 敞口 / 成交 / 对账），只读
from routers.ledger import router as _ledger_router
_include_flat(app, _ledger_router)

from routers.causal_graph import router as _cg_router
_include_flat(app, _cg_router)

from routers.event_atoms import router as _ea_router
_include_flat(app, _ea_router)

from routers.filter import router as _filter_router
_include_flat(app, _filter_router)

from routers.reasoning import router as _reason_router
_include_flat(app, _reason_router)

from routers.signal_weights import router as _sw_router
_include_flat(app, _sw_router)

from routers.kg import router as _kg_router
_include_flat(app, _kg_router)

from routers.kb import router as _kb_router
_include_flat(app, _kb_router)

from routers.data_health import router as _dh_router
_include_flat(app, _dh_router)

from routers.timeseries import router as _ts_router
_include_flat(app, _ts_router)

# ── 背离雷达（2026-11-16）：多指标 / 多周期 / 无未来函数 ──
# 只读 ohlcv，不写下单；引擎在 divergence_engine.py。
from routers.divergence import router as _div_router
_include_flat(app, _div_router)

# ── v2 能力扩展（2026-11-16）：情景树 / 研报摄入 ──
# 旁路只读 + 独立 research_notes 表，不碰下单链路；逻辑在 capabilities.py。
from routers.capabilities import router as _cap_router
_include_flat(app, _cap_router)

# ── Trident 2.0 功能层（2026-11-16）：64 项候选能力统一出口 ──
# 只写 t2_* 纸面账本，不下真实单；首次请求时 ensure_schema（幂等）。
from routers.events import router as _ev_router
_include_flat(app, _ev_router)

from routers.agent_chat import router as _ac_router
_include_flat(app, _ac_router)

from routers.replay import router as _rp_router
_include_flat(app, _rp_router)

from routers.review_run import router as _rvr_router
_include_flat(app, _rvr_router)


async def get_replay_signals(*args, **kwargs):
    """兼容薄壳：原内部助手 _replay_collect_context / _replay_build_fast 还在 await 它。"""
    from routers.replay import get_replay_signals as _f
    return await _f(*args, **kwargs)


async def get_replay_stats(*args, **kwargs):
    """兼容薄壳：/api/dashboard/overview 等仍通过 _api() 回引它。"""
    from routers.replay import get_replay_stats as _f
    return await _f(*args, **kwargs)


from routers.trading_v201 import router as _tv_router
_include_flat(app, _tv_router)

from routers.backtest import router as _bt_router
_include_flat(app, _bt_router)

from routers.overview import router as _ov_router
_include_flat(app, _ov_router)

from routers.indicators import router as _ind_router
_include_flat(app, _ind_router)

from routers.health import router as _health_router
_include_flat(app, _health_router)

from routers.connectivity import router as _conn_router
_include_flat(app, _conn_router)

from routers.sources import router as _sources_router
_include_flat(app, _sources_router)

from routers.charts_export import router as _ce_router
_include_flat(app, _ce_router)

from routers.backups import router as _backups_router
_include_flat(app, _backups_router)

from routers.trading import router as _trading_router
_include_flat(app, _trading_router)

from routers.calendar import router as _cal_router
_include_flat(app, _cal_router)

from routers.abnormal import router as _abn_router
_include_flat(app, _abn_router)

from routers.market_quotes import router as _mq_router
_include_flat(app, _mq_router)

from routers.policy import router as _policy_router
_include_flat(app, _policy_router)

from routers.paper import router as _paper_router
_include_flat(app, _paper_router)

from routers.trident2 import router as _trident2_router
_include_flat(app, _trident2_router)

# ── v182 反思改进·adjustments 审批端点 ──
# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# ── v201 #1: 策略级反思 (strategy_reflection) ─────────────────────────────────
# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# 2026-11-17: 本条已迁到 routers/paper.py。


# v232.6 P1: 反思 -> policy 闭环 - 候选人工审 endpoint
# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


# v190 #4: weekly_insight_apply admin endpoints
# 2026-11-17: 本条已迁到 routers/policy.py。


# 2026-11-17: 本条已迁到 routers/policy.py。


@app.get("/api/admin/insight/weekly-status")
def admin_weekly_status():
    """\xe6\x9f\xa5\xe7\x9c\x8b weekly \xe7\x8a\xb6\xe6\x80\x81\xe3\x80\x82"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT kind, source_samples,
                   CASE WHEN source_samples >= 10 THEN 'n>=10'
                        WHEN source_samples >= 6  THEN 'n=6-9'
                        WHEN source_samples >= 1  THEN 'n=1-5'
                        ELSE 'n=0' END AS bucket
              FROM review_insights WHERE active=1
        """).fetchall()
        from collections import Counter
        c = Counter((int(r["kind"]), r["bucket"]) for r in rows)
        bucket_table: dict[str, dict[str, int]] = {"0": {}, "1": {}}
        for (k, b), n in c.items():
            bucket_table[str(k)][b] = int(n)
        last_promote = conn.execute("""
            SELECT MAX(last_suggested_at) AS ts, COUNT(*) AS n
              FROM strategy_param_adjustments
             WHERE note='v190_weekly_promote'
        """).fetchone()
        pending = conn.execute("""
            SELECT COUNT(*) AS n FROM strategy_param_adjustments
             WHERE status='pending'
        """).fetchone()
        # v191 #1: 看真实数据中模糊映射对 n>=5 kind=1 insight 的覆盖率
        # v200+ Q5: 自适应门槛 — 数据稀疏 (total < 30) → 5; 充足 → 10.
        # 动态读 review_insights 总样本数喂给 adaptive_min_samples().
        _total_insights = conn.execute(
            "SELECT COUNT(*) AS n FROM review_insights WHERE active=1 AND kind=1"
        ).fetchone()["n"] or 0
        try:
            _adapt_min, _adapt_conf = config.adaptive_min_samples(int(_total_insights))
        except Exception:
            _adapt_min = int(getattr(config, "WEEKLY_INSIGHT_MIN_SAMPLES", 10) or 10)
            _adapt_conf = "normal"
        kind1_rows = conn.execute("""
            SELECT id, rule FROM review_insights
             WHERE active=1 AND kind=1 AND source_samples >= ?
        """, (_adapt_min,)).fetchall()
        try:
            import weekly_insight_apply as _wia
            recoverable = sum(1 for r in kind1_rows if _wia._extract_param_key(r["rule"]))
        except Exception:
            recoverable = -1  # 模块加载失败占位
        return {
            "active_insights": bucket_table,
            "last_promote_at": last_promote["ts"] if last_promote else None,
            "promoted_count": int(last_promote["n"] or 0) if last_promote else 0,
            "pending_adjustments": int(pending["n"] or 0) if pending else 0,
            "min_samples_threshold": _adapt_min,
            # v200+ Q5: 自适应门槛 — 暴露 confidence 供运维观察
            "adaptive_confidence": _adapt_conf,
            # v191 #1: 模糊映射可恢复的 n>=threshold kind=1 insight 数 (运维可见)
            "v191_recoverable_candidates": recoverable,
            "v191_total_candidates": len(kind1_rows),
        }
    finally:
        conn.close()


# 2026-11-17: 本条已迁到 routers/events.py 或 routers/admin.py。


# -- REST routes -----------------------------------------------------------

# 2026-11-17: 13 条 /api/ai/* 端点（模型选择 / 提示词库 / 品种规则）已迁到 routers/ai.py。

# 2026-11-17: 因果链库 8 条端点 + 4 个请求模型已迁到 routers/causal.py。
# （并行线新增的 /api/causal/quality、/api/causal/export 两条留在原位，未随批搬。）


# === v407：因果图谱质量体检 + 导出 ===
# 背景（2026-11-17 实测）：
#   · 旧统计把 expected_direction='BOTH' 的链混进方向命中率 —— BOTH 的 direction_hit
#     其实是"幅度过没过线"，实测 462 条 BOTH 命中率 93%，把整体命中率抬成假象；
#   · 全库 confidence 恒 1.0、hit_count 恒 0，外部看到的"图谱质量"是空的。
# 这个端点只输出可复核的口径：BOTH 单列、方向只算 UP/DOWN、按证据档位分层。
# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# === v222：用户主动输入因果假设 ===
# 旧版：因果图谱完全由 AI 自动生成；用户无法直接表达「我怀疑 X 才是 BTC 下跌的根因」。
# 新版：用户提供 (asset, label, chain[]) → 系统 _upsert_node + 立即 verify_hypothesis 跑回看 30 天事实，
#       给出 CONFIRMED / CONTRADICTED / UNCONFIRMED + 证据条目数 + 命中的 fact_ids。
#       用户可以拿这个结果去对照实际走势，养成「先立假设再让系统证伪」的决策习惯。


# 2026-11-17: 信念系统 / 知识缺口 / 外部数据接入 14 条端点已迁到 routers/beliefs.py。


# 2026-11-17: 本条已迁到 routers/events.py 或 routers/admin.py。


# 2026-11-17: 本条已迁到 routers/events.py 或 routers/admin.py。


# ── 门卫筛选双流 — 给前端"门卫筛选"面板用 ──────────────────────────────

# 返回两条最新新闻流：
#   * passed    — 通过 L1/L2 筛子的 (is_noise=0)，即实际"被门卫晒进来"的
#   * filtered  — 被 L1/L2 筛掉的 (is_noise=1)，即"被门卫没晒进来"的
# 纯 SQL、无 LLM、零 token 成本；15s 缓存避免轮询放大查询。
_GATEKEEPER_FILTER_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_GATEKEEPER_FILTER_TTL_S = 15.0


def _gatekeeper_filter_sync(rows: int = 30) -> Dict[str, Any]:
    """一次拉两类：passed + filtered。轻字段：id/source/content_preview(80字)/timestamp/relevance_score。"""
    rows = max(5, min(int(rows), 100))
    today = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    # content_preview 用 SQLite SUBSTR 截前 80 字
    base_select = """
        SELECT id, source,
               substr(content, 1, 80) AS content_preview,
               timestamp,
               relevance_score,
               status
        FROM raw_news
    """
    sql_passed = f"{base_select} WHERE is_noise = 0 ORDER BY id DESC LIMIT ?"
    sql_filtered = f"{base_select} WHERE is_noise = 1 ORDER BY id DESC LIMIT ?"
    with db.get_connection() as conn:
        conn.row_factory = sqlite3.Row
        passed_rows = [dict(r) for r in conn.execute(sql_passed, (rows,)).fetchall()]
        filtered_rows = [dict(r) for r in conn.execute(sql_filtered, (rows,)).fetchall()]
        # 今日计数（同一连接，避免短时间差到四行）
        # v499: raw_news.content 带着采集器写的 "[hash:xxxx] 标题" 指纹前缀，
        #   以前只清洗了 news_text 那条链，首页「实时事件流」这条漏了 ——
        #   实拍能在标题前看到 [hash:ca2c787f1ee6…]。这里统一剥掉。
        for _lst in (passed_rows, filtered_rows):
            for _it in _lst:
                _it["content_preview"] = re.sub(
                    r"^\[hash:[0-9a-fA-F]{4,}\]\s*", "", str(_it.get("content_preview") or "")
                )
        # v469（2026-11-19）：timestamp 列存 unix 秒串，substr 文本日期比较恒不匹配；
        # 改用 ts 按上海日聚合，与 /api/events/today 同口径。
        c_passed = conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE is_noise = 0 AND date(datetime(ts, 'unixepoch', '+8 hours')) = ?",
            (today,),
        ).fetchone()[0]
        c_filtered = conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE is_noise = 1 AND date(datetime(ts, 'unixepoch', '+8 hours')) = ?",
            (today,),
        ).fetchone()[0]
    return {
        "passed": passed_rows,
        "filtered": filtered_rows,
        "counts": {
            "passed_today": int(c_passed or 0),
            "filtered_today": int(c_filtered or 0),
            "passed_total": len(passed_rows),
            "filtered_total": len(filtered_rows),
        },
        "today": today,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _cached_gatekeeper_filter(limit: int = 30) -> Dict[str, Any]:
    now = time.monotonic()
    v = _GATEKEEPER_FILTER_CACHE["value"]
    if v is None or now >= _GATEKEEPER_FILTER_CACHE["expires"]:
        v = _gatekeeper_filter_sync(rows=limit)
        _GATEKEEPER_FILTER_CACHE["value"] = v
        _GATEKEEPER_FILTER_CACHE["expires"] = now + _GATEKEEPER_FILTER_TTL_S
    return v


# 2026-11-17: 本条已迁到 routers/events.py 或 routers/admin.py。


# ── 事件热力图 — 给归因页"asset × hour"事件密度网格 ────────────────────
# 返回结构：buckets（最近 N 小时横轴）+ assets（纵轴）+ cells[asset][idx] = 计数/分数
# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。

# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。


# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。


# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。


# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。


_MARKET_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
# 后台刷新的互斥标记：同一时刻只允许一个线程去外网抓，避免 stale 路径并发打满源
_MARKET_REFRESH_LOCK = threading.Lock()
_MARKET_REFRESHING = False
_QUICK_SIM_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
# 2026-11-17: BLS 面板短缓存（_BLS_SNAPSHOT_CACHE）已随 macro 簇迁到 routers/macro.py。
# 2026-11-17: 平台新闻抓取助手与 5 条 /api/news/<平台> 端点已搬出。
#   · 助手集群（TechFlow/东财/BlockBeats/金十 的同步抓取 + 分页/解析工具 + 20s 缓存）
#     下沉到 feeds/platform_news.py，这里按原名导入，既有内部调用不受影响；
#   · 端点进 routers/news.py。
from feeds.platform_news import (
    _BLOCKBEATS_CACHE, _EASTMONEY_CACHE, _JIN10_CACHE, _JIN10_CALENDAR_CACHE,
    _NEWS_CACHE_TTL, _TECHFLOW_CACHE, _CRYPTO_KEYWORDS,
    _techflow_value, _paged_url, _extract_news_rows, _fetch_paged_json,
    _is_crypto_relevant, _fetch_techflow_sync, _fetch_eastmoney_news_sync,
    _fetch_blockbeats_sync, _fetch_blockbeats_fallback_sync,
    _fetch_jin10_sync, _fetch_jin10_calendar_sync,
)
_QINGHONG_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}


def _cached_quick_sim() -> Dict[str, Any]:
    """快速模拟评测统计 — 2s 缓存（与前端 5s 轮询对齐，留足窗口拉新数据）。
    之前 15s 缓存 + 前端 10s 轮询 → 反思页「已结算」数字体感滞后 5-15s，用户卡死。
    v170.4 P0+: 短缓存 + SSE 主动推 has_settlement 触发前端立即重拉。
    """
    now = time.monotonic()
    if _QUICK_SIM_CACHE["value"] is None or now >= _QUICK_SIM_CACHE["expires"]:
        _QUICK_SIM_CACHE["value"] = quick_sim.snapshot()
        _QUICK_SIM_CACHE["expires"] = now + 2.0
    return _QUICK_SIM_CACHE["value"]


def _persist_market_snapshot(payload: Dict[str, Any]) -> None:
    """行情快照落库（旁路：分析上下文历史）。写失败不影响价格响应——绝不让 500。

    v2026.10.26: 原来在**请求线程里同步**落库，engine 持 60s 写锁时
    db.retry_on_busy 会按 busy_timeout=5s × 3 次重试 + 退避阻塞 ~15-22s，
    把 /api/market/prices 和 /api/replay/positions 一起拖死。
    改成后台线程落库 → 响应立即返回，锁竞争只影响历史写入。
    """
    try:
        if not timeseries.enqueue_market_snapshot(payload):
            db.retry_on_busy(lambda: timeseries.record_market_snapshot(payload))
    except Exception as exc:
        print(f"[MARKET] snapshot 落库失败(忽略): {type(exc).__name__}: {str(exc)[:80]}", flush=True)


def _refresh_market_prices_inline() -> Dict[str, Any]:
    """真正去外网多源抓一次，并落缓存 + 异步落库。"""
    now = time.monotonic()
    payload = market_feeds.fetch_market_prices()
    _MARKET_CACHE["value"] = payload
    # v2026.10.08: TTL 15s→30s（market_feeds 内部单源 1.5s timeout × 5 sources = 2-3s，
    # 15s 缓存导致下一次刷新卡顿；30s 让两次连续 movers 请求不重复 fetch）。
    _MARKET_CACHE["expires"] = now + 30.0
    # 快照落库是旁路（分析上下文历史），写失败不影响价格响应——绝不让 500
    # v189-8: 包 retry_on_busy,5s busy_timeout 拿不到锁自动重试,消除
    # 197 次 [MARKET] snapshot 落库失败 (engine 持 60s 锁时 API 5s 直接丢).
    # v2026.10.26: 落库挪到后台线程 —— 锁竞争最长阻塞 ~22s，不能占着请求线程。
    threading.Thread(
        target=_persist_market_snapshot, args=(payload,), daemon=True, name="market-snapshot-persist",
    ).start()
    return payload


def _refresh_market_prices_bg() -> None:
    """后台刷新（同一时刻只跑一个），供 allow_stale 路径使用。"""
    global _MARKET_REFRESHING
    with _MARKET_REFRESH_LOCK:
        if _MARKET_REFRESHING:
            return
        _MARKET_REFRESHING = True
    try:
        _refresh_market_prices_inline()
    except Exception as exc:
        print(f"[MARKET] 后台刷新失败(保留旧值): {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    finally:
        with _MARKET_REFRESH_LOCK:
            _MARKET_REFRESHING = False


def _cached_market_prices(allow_stale: bool = False) -> Dict[str, Any]:
    """外网多源行情快照（8000 本地口径，含 source / 全品种）。

    2026-11-17: 加 allow_stale —— 本机 8100 在线时 8000 不跑行情守护，这份缓存没人预热，
    内部调用（atmosphere 预热、事件标注、quick-sim、复盘回放）每 30s 就要吃一次
    13.5s 冷算。allow_stale=True 时：有旧值就立刻返回旧值，同时在后台刷一次。
    端点层不要传 True（前端要新鲜值，那里本来就走 8100 转发）。
    """
    now = time.monotonic()
    expired = _MARKET_CACHE["value"] is None or now >= _MARKET_CACHE["expires"]
    if not expired:
        return _MARKET_CACHE["value"]
    if allow_stale and _MARKET_CACHE["value"] is not None:
        threading.Thread(target=_refresh_market_prices_bg, daemon=True,
                         name="market-refresh-bg").start()
        return _MARKET_CACHE["value"]
    return _refresh_market_prices_inline()


def _fetch_prices_from_bridge() -> Optional[Dict[str, Any]]:
    """从 8100 取用库里现成 K 线算出的价格（实测 1s）。

    为什么不让 8000 自己算：本机外网多源版 _cached_market_prices 冷启动要 13.5s
    （5 个源 × 1.5s 超时 + 缓存刷新），而 8100 读 ohlcv_daily 只要 1s。
    两边的返回字段集本来就不同（8000 多一个 status），既然前端实际打的是 8100，
    就按 8100 的为准，8000 只做转发。
    """
    try:
        import urllib.request as _ur
        _opener = _ur.build_opener(_ur.ProxyHandler({}))
        with _opener.open(config.BRIDGE_BASE_URL + "/api/market/prices", timeout=8) as _resp:
            _d = json.loads(_resp.read())
        if isinstance(_d, dict) and _d.get("items"):
            return _d
    except Exception:
        return None
    return None


# 2026-11-17: 本条已迁到 routers/market_quotes.py（助手仍在本文件）。


# P2-13: 价格反查接口 — 按 ts 查询某品种在某时刻的最近 K 线价格
#   用途：反查 ai_decisions.entry_price 与决策时刻的市场真实价是否一致
# 2026-11-17: 本条已迁到 routers/market_quotes.py（助手仍在本文件）。


def _candles_for_asset(asset: str, timeframe: str, limit: int = 5):
    """v476：按 ASSET_ROUTE 的 market 分发 K 线。

    此前 `_price_at_sync` 拿到 symbol 就无脑 `fetch_okx_candles` —— 美股
    （META/NVDA/AMZN…）的 symbol 是 'META'，会被当成 OKX 合约发出去，
    于是 source_status 里堆满 `okx_swap:META` 这类 100% 失败源（NVDA 累计 785 次全灭）。
    美股必须走 Yahoo/腾讯，加密走币安，贵金属才走 OKX。
    """
    try:
        from feeds import quote_sources as _qs
    except Exception:
        return None
    mkt = _qs.market_of(asset)
    sym = _qs.symbol_of(asset)
    if not sym:
        return None
    if mkt == "okx_swap":
        return _qs.fetch_okx_candles(sym, _qs.okx_bar(timeframe), limit)
    if mkt == "yahoo":
        iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m",
              "4h": "1h", "1d": "1d"}.get(timeframe, "60m")
        # v479：Yahoo 常 429/断连 → 日线用多源（Yahoo→腾讯），日内仍走 Yahoo（腾讯只有日线）
        if iv in ("1d", "1wk", "1mo"):
            return _qs.fetch_daily_multisource(asset, interval=iv, limit=limit)
        rows = _qs.fetch_yahoo_candles(sym, interval=iv, range_or_period="5d", limit=limit)
        return rows
    if mkt == "binance_fapi":
        try:
            bars, _last = _fetch_binance_klines(asset, timeframe)
            return [[int(b["time"]) * 1000, b["open"], b["high"], b["low"],
                     b["close"], b["volume"]] for b in bars][-int(limit):]
        except Exception:
            return None
    if mkt == "domestic_spot":
        # v479：先要真 OHLC（Yahoo 期货连续合约，WTI=CL=F），拿不到才退回单点现货价。
        # 原实现只有一根"当前价"当 K 线，指标全是平的，等于没数据。
        try:
            iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m",
                  "4h": "60m", "1d": "1d"}.get(timeframe, "1d")
            rows = _qs.fetch_daily_multisource(asset, interval=iv, limit=limit)
            if rows:
                return rows
        except Exception:
            pass
        px = _get_current_price(asset)
        return [[int(time.time() * 1000), px, px, px, px, 0]] if px else None
    return None


def _price_at_sync(asset: str, ts_iso: str, timeframe: str) -> Dict[str, Any]:
    """P2-13 后向反查：ts 时刻附近的最近 K 线价格。"""
    try:
        from feeds import quote_sources as _qs
        inst = _qs.symbol_of(asset)
        if not inst:
            return {"ok": False, "error": f"unknown asset: {asset}", "code": 400}
        # 解析 ts
        from datetime import datetime
        try:
            dt = datetime.strptime(ts_iso[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            try:
                dt = datetime.strptime(ts_iso[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return {"ok": False, "error": f"bad ts format: {ts_iso}", "code": 400}
        ts_ms = int(dt.timestamp() * 1000)
        # v476：按市场分发（美股走 Yahoo，不再错发 OKX）
        bars = _candles_for_asset(asset, timeframe, 5)
        if not bars:
            return {"ok": False, "error": "no kline data", "code": 404}
        # bars = [[ts_ms, open, high, low, close, volume], ...]
        prev = None
        for b in bars:
            try:
                b_ts = int(b[0])
                if b_ts <= ts_ms:
                    prev = b
                else:
                    break
            except Exception:
                continue
        if not prev:
            # 用最新一根（ts 之后才有的数据 = 数据滞后）
            prev = bars[-1]
        prev_ts = int(prev[0])
        return {
            "ok": True,
            "asset": asset,
            "inst": inst,
            "timeframe": timeframe,
            "query_ts": ts_iso,
            "match_ts_ms": prev_ts,
            "match_ts_iso": datetime.fromtimestamp(prev_ts / 1000).strftime("%Y-%m-%dT%H:%M:%S"),
            "open": float(prev[1]),
            "high": float(prev[2]),
            "low": float(prev[3]),
            "close": float(prev[4]),
            "volume": float(prev[5]),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "code": 500}


def _fetch_movers_from_bridge(tf: str, limit: int = 20,
                              full: bool = False) -> Optional[Dict[str, Any]]:
    """从 8100(bridge) 取已算好的 movers 结果（同库同算法，只是不占 api 内存/CPU）。

    v398-fix: 参数**原样透传**。之前只传 timeframe，且把任何非 10m 值一律映射成 3m，
    而 8100 的默认是 24h 完整版 —— 结果是「同一个 URL，打 8000 拿到 3KB 的 partial
    轻量版、打 8100 拿到 465KB 完整版」（对拍实测），前端换 base URL 就换了一份数据。
    """
    try:
        import urllib.request as _ur
        _opener = _ur.build_opener(_ur.ProxyHandler({}))
        _url = (config.BRIDGE_BASE_URL + "/api/market/movers"
                f"?timeframe={tf}&limit={int(limit)}&full={'true' if full else 'false'}")
        with _opener.open(_url, timeout=20) as _resp:
            _raw = _resp.read()
        _d = json.loads(_raw)
        if isinstance(_d, dict) and _d.get("items"):
            return _d
    except Exception:
        return None
    return None


_MOVERS_TF_OK = ("1m", "3m", "10m", "1h", "4h", "24h")
_MOVERS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_MOVERS_CACHE_TTL_S = 120.0


def _movers_cache_key(tf: str, limit: int, full: bool) -> str:
    """缓存键必须带上全部入参：只用 timeframe 做键会让不同 limit 互相污染。"""
    return f"{tf}|{int(limit)}|{int(bool(full))}"


# 2026-11-17: 本条已迁到 routers/market_quotes.py（助手仍在本文件）。


# 2026-11-17: 本条已迁到 routers/market_quotes.py（助手仍在本文件）。


def _gate_reflection_sync(asset: str, days: int) -> Dict[str, Any]:
    """拉该品种 3m 异动点后交给 gate_reflection 做纯查询复盘；异动点复用现有缓存与兜底。"""
    abnormals = _detect_5m_abnormals(asset)
    return gate_reflection.gate_reflection(asset, days, abnormals)


def _wti_change24h_from_kline() -> Optional[float]:
    """WTI 无免费永续 K 线 → 用库内 kline_bars(1d) 最近两个日线 close 算 24h 涨跌。

    属真实历史数据兜底（非 mock），与 EastMoney 网络不可用解耦。
    """
    try:
        with db.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT close FROM kline_bars WHERE symbol='WTI' AND timeframe='1d' "
                "ORDER BY ts DESC LIMIT 2",
            ).fetchall()
        if len(rows) >= 2 and rows[0]["close"] and rows[1]["close"]:
            prev_close = float(rows[1]["close"])
            last_close = float(rows[0]["close"])
            if prev_close > 0:
                return (last_close - prev_close) / prev_close * 100
    except Exception:
        pass
    return None


# 需求4：3 分钟异动检测缓存（供 /api/market/movers 反推星标）
_5M_ABNORMAL_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_5M_ABNORMAL_TTL_S = 30.0

# 需求2：3 分钟异动 AI 归因缓存（后台线程排队归因，不阻塞 movers；前端 30s 轮询自动拿到归因结果）
_5M_ATTRIB_CACHE: Dict[str, Dict[str, Any]] = {}
_5M_ATTRIB_LOCK = threading.Lock()
_5M_ATTRIB_TTL_S = 600.0
_5M_ATTRIB_PENDING: set = set()

# 需求：星标事件 AI 归因缓存 —— 从高分新闻里找出行情主要驱动事件（前台 30s 轮询命中即展示）
_MARKET_DRIVER_CACHE: Dict[str, Dict[str, Any]] = {}
_MARKET_DRIVER_LOCK = threading.Lock()
_MARKET_DRIVER_TTL_S = 600.0
_MARKET_DRIVER_PENDING: set = set()


def _to_sec_ts(v: Any) -> int:
    """把 ms / s 混用的时间锚统一成秒。

    v336 P0-6: 同一个字段存在两种单位 —— `abnormal_signals.anchor_ts` 8072 行是
    毫秒（K 线 ts_ms），79 行是秒；而所有 `news_events.ts` / `raw_news.ts` 都是秒。
    多因子归因调用链 `api_server` → `_abnormal_news(asset, sig["anchor_ts"])` 传的是
    毫秒，窗口比较 `ne.ts >= (1.79e12 - 1800)` 恒为假 → **新闻上下文永远取回 0 条**，
    模型只能在没有任何新闻的情况下"归因"。实测：真锚 1794624153 用秒口径取到 5 条，
    用毫秒口径 0 条。
    """
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        return 0
    if v >= 10 ** 11:   # 1e11 秒 ≈ 5138 年，任何"秒"都不会这么大
        v //= 1000
    return v


def _abnormal_news(asset: str, anchor_ts: int, window_s: int = 1800) -> List[Dict[str, Any]]:
    """异动时间锚往前 window_s 秒内的该品种公开新闻（非噪声，取可读原文）。"""
    anchor_ts = _to_sec_ts(anchor_ts)
    lo = int(anchor_ts) - window_s
    out: List[Dict[str, Any]] = []
    try:
        with db.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ne.ts, ne.asset, ne.action, ne.score, rn.content, rn.source
                   FROM news_events ne
                   LEFT JOIN raw_news rn ON rn.id = ne.news_id
                   WHERE ne.is_noise = 0 AND ne.asset = ? AND ne.ts >= ? AND ne.ts <= ?
                   ORDER BY ne.ts DESC
                   LIMIT 12""",
                (asset, lo, int(anchor_ts)),
            ).fetchall()
        for r in rows:
            content = re.sub(r"^\[hash:\w+\]\s*", "", r["content"] or "").strip()
            if not content:
                continue
            out.append({
                "ts": int(r["ts"]),
                "ts_str": datetime.fromtimestamp(int(r["ts"])).strftime("%H:%M:%S"),
                "content": content[:160],
                "source": r["source"] or "",
            })
    except Exception as exc:
        print(f"[ATTRIB] 新闻查询失败(忽略): {type(exc).__name__}: {exc}", flush=True)
    return out


def _attrib_key(asset: str, ts: int) -> str:
    return f"{asset}:{int(ts)}"


def _queue_attribution(asset: str, abnormals: List[Dict[str, Any]]) -> None:
    """对最新异动点排队 AI 归因（后台线程），写 _5M_ATTRIB_CACHE，不阻塞 movers 响应。"""
    if not abnormals:
        return
    latest = abnormals[0]
    ts = int(latest.get("ts") or 0)
    if not ts:
        return
    key = _attrib_key(asset, ts)
    with _5M_ATTRIB_LOCK:
        if key in _5M_ATTRIB_PENDING:
            return
        hit = _5M_ATTRIB_CACHE.get(key)
        if hit and time.monotonic() - hit["ts"] < _5M_ATTRIB_TTL_S:
            return
        _5M_ATTRIB_PENDING.add(key)
    news = _abnormal_news(asset, ts)

    def _run() -> None:
        try:
            from engine import ai_worker as _aw
            attr = _aw.attribute_5m_abnormal(
                asset, ts, latest.get("pct_change"),
                latest.get("vol_ratio"), latest.get("trigger_type"), news,
            )
            with _5M_ATTRIB_LOCK:
                _5M_ATTRIB_CACHE[key] = {"ts": time.monotonic(), "data": attr}
        except Exception as exc:
            print(f"[ATTRIB] {asset} 归因失败: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with _5M_ATTRIB_LOCK:
                _5M_ATTRIB_PENDING.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def _queue_driver_analysis(asset: str, events: List[Dict[str, Any]]) -> None:
    """对星标事件（高分 BUY/SELL 新闻）后台排队 AI 归因 → 找出行情主要驱动事件。

    写 _MARKET_DRIVER_CACHE，不阻塞 movers 响应；前端 30s 轮询命中即展示。
    轻量：仅取 |score| 最高的少数事件做一次归因，TTL 内不重复调用。
    """
    asset = (asset or "").upper()
    scored = [e for e in (events or []) if (e.get("action") or "HOLD") in ("BUY", "SELL")]
    if not scored:
        return
    scored.sort(key=lambda e: abs(float(e.get("score") or 0.0)), reverse=True)
    top = scored[:8]
    key = asset
    with _MARKET_DRIVER_LOCK:
        if key in _MARKET_DRIVER_PENDING:
            return
        hit = _MARKET_DRIVER_CACHE.get(key)
        if hit and time.monotonic() - hit["ts"] < _MARKET_DRIVER_TTL_S:
            return
        _MARKET_DRIVER_PENDING.add(key)

    def _run() -> None:
        try:
            from engine import ai_worker as _aw
            result = _aw.attribute_market_drivers(asset, top)
            with _MARKET_DRIVER_LOCK:
                _MARKET_DRIVER_CACHE[key] = {"ts": time.monotonic(), "data": result}
        except Exception as exc:
            print(f"[DRIVER] {asset} 归因失败: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with _MARKET_DRIVER_LOCK:
                _MARKET_DRIVER_PENDING.discard(key)

    threading.Thread(target=_run, daemon=True).start()


_JIN10_ABNORMAL_CODE = {"XAU": "XAUUSD", "WTI": "USOIL"}

# ── K 线结果短 TTL 缓存 ──────────────────────────────────────────────────────
# 背景: feedback-stats / abnormal 等端点每次请求都直连外网拉 5m/3m/10m K 线,
#   外网冷启一次 5~10s, 同一资产 60s 内被反复拉取 → 端点稳定偏慢。
# K 线是分钟级数据, 60s 缓存不影响新鲜度; 仅缓存**成功**结果, 失败不缓存以免
# 掩盖外网故障; 缓存条目带时间戳, 过期即回源。
_KLINE_CACHE: Dict[str, tuple[float, Optional[List[List[Any]]]]] = {}
_KLINE_CACHE_TTL_S = 60.0


def _kline_cache_get(key: str) -> Optional[List[List[Any]]]:
    hit = _KLINE_CACHE.get(key)
    if not hit:
        return None
    ts, candles = hit
    if candles is None or time.time() - ts > _KLINE_CACHE_TTL_S:
        _KLINE_CACHE.pop(key, None)
        return None
    return candles


def _kline_cache_put(key: str, candles: Optional[List[List[Any]]]) -> None:
    if candles:
        _KLINE_CACHE[key] = (time.time(), candles)


def _fetch_3m_candles(asset: str) -> Optional[List[List[Any]]]:
    """拉资产 3m 升序 K 线；OKX 被墙时金十 get_kline 1m 兜底聚合为 3m。失败返回 None。

    v221：拉取窗口 48 → 144（约 7h），覆盖 24h 10m 视图下大阴/大阳回溯。
    """
    asset = (asset or "").upper()
    _ck = f"3m:{asset}"
    _hit = _kline_cache_get(_ck)
    if _hit is not None:
        return _hit
    candles = None
    try:
        r = quote_sources.route(asset)
        if r and r["market"] == "okx_swap":
            candles = quote_sources.fetch_okx_candles(r["symbol"], "3m", 144)
        elif r and r["market"] == "binance_fapi":
            # 币安现货 vision 直连（国内稳定、3m 真实 K 线；fapi 走代理慢且易失败）
            candles = quote_sources.fetch_binance_spot_candles(r["symbol"], "3m", 144)
    except Exception:
        candles = None
    # OKX 被墙（无代理）→ 金十 get_kline 1m 兜底聚合成 3m（国内 mcp.jin10.com 直连）
    if candles is None and asset in _JIN10_ABNORMAL_CODE:
        try:
            payload = jin10_mcp.get_kline(_JIN10_ABNORMAL_CODE[asset], count=300)
            inner = payload.get("data") if payload.get("status") == "ok" else None
            kls = (inner or {}).get("klines") or []
            bars_1m: List[Dict[str, Any]] = []
            for k in kls:
                try:
                    o = float(k["open"]); h = float(k["high"])
                    lo = float(k["low"]); c = float(k["close"]); v = int(k.get("volume") or 0)
                except (TypeError, ValueError, KeyError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                bars_1m.append({"time": int(k["time"]), "open": o, "high": h,
                                "low": lo, "close": c, "volume": v})
            bars_3m = _aggregate_bars(bars_1m, 180)
            candles = [[int(b["time"]) * 1000, b["open"], b["high"], b["low"], b["close"], b["volume"]]
                       for b in bars_3m]
        except Exception:
            candles = None
    _kline_cache_put(_ck, candles)
    return candles


def _fetch_10m_candles(asset: str) -> Optional[List[List[Any]]]:
    """v170: 拉资产 10m 升序 K 线；币安/OKX 均无 10m 原生 → 拉 5m 两两聚合为 10m。

    优先 5m 原生 → _aggregate_bars(bars_5m, 600)；OKX 失败时金十 get_kline 1m 兜底聚合成 10m。
    v221：拉取窗口 96 → 288（约 24h），覆盖 24h 10m 视图下大阴/大阳回溯。
    失败返回 None。
    """
    asset = (asset or "").upper()
    _ck = f"10m:{asset}"
    _hit = _kline_cache_get(_ck)
    if _hit is not None:
        return _hit
    candles = None
    src_log: List[str] = []
    try:
        r = quote_sources.route(asset)
        mkt = (r or {}).get("market")
        sym = (r or {}).get("symbol")
        if mkt == "okx_swap":
            c5 = quote_sources.fetch_okx_candles(sym, "5m", 288)
        elif mkt == "binance_fapi":
            c5 = quote_sources.fetch_binance_spot_candles(sym, "5m", 288)
        elif mkt == "yahoo":
            # v307: yahoo 路由（美股/韩股，如 SKHYNIX→000660.KS）原本直接落 else→None，
            #   导致这些标的永远拉不到 K 线 → 量能异动(Abnormal_volume)一条都产不出。
            #   Yahoo 无原生 5m → 取 5m 聚合为 10m；再用 15m 原生兜底。
            c5 = quote_sources.fetch_yahoo_candles(sym, "5m", limit=288)
            src_log.append(f"yahoo:5m={'ok' if c5 else 'none'}")
            if not c5:
                c15 = quote_sources.fetch_yahoo_candles(sym, "15m", limit=192)
                src_log.append(f"yahoo:15m={'ok' if c15 else 'none'}")
                c5 = None
                if c15:
                    bars_10m_direct = [
                        {"time": int(row[0] // 1000), "open": row[1], "high": row[2],
                         "low": row[3], "close": row[4], "volume": row[5]}
                        for row in c15
                    ]
                    candles = [[int(b["time"]) * 1000, b["open"], b["high"], b["low"],
                                b["close"], b["volume"]] for b in bars_10m_direct]
            # v307 韩股专用兜底：Yahoo 对 KRX 持续 429 → 走 Naver（KRX 本土源，单日 325 根 1m）。
            #   Naver 分钟线是「close + 累计量」，聚合前先补 OHLC，量在 _aggregate_bars 里会正常求和。
            if not candles and not c5 and quote_sources.NAVER_SYMBOL_FOR_ASSET.get(asset):
                n_sym = quote_sources.NAVER_SYMBOL_FOR_ASSET[asset]
                n_1m = quote_sources.fetch_naver_candles(n_sym, "minute", days_back=1, limit=600)
                src_log.append(f"naver:1m={0 if not n_1m else len(n_1m)}")
                if n_1m:
                    bars_n = [
                        {"time": int(r0[0] // 1000), "open": r0[1], "high": r0[2],
                         "low": r0[3], "close": r0[4], "volume": r0[5]}
                        for r0 in n_1m
                    ]
                    bars_10m_naver = _aggregate_bars(bars_n, 600)
                    candles = [[int(b["time"]) * 1000, b["open"], b["high"], b["low"],
                                b["close"], b["volume"]] for b in bars_10m_naver]
                    src_log.append(f"naver:10m={len(bars_10m_naver)}")
        else:
            c5 = None
        if c5:
            bars_5m: List[Dict[str, Any]] = []
            for row in c5:
                # 兼容 [time_ms, o, h, l, c, v] 与 dict 两种形态
                if isinstance(row, dict):
                    bars_5m.append({
                        "time": int(row.get("time", 0)) // 1000 if int(row.get("time", 0)) > 1e12 else int(row.get("time", 0)),
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low":  float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0)),
                    })
                else:
                    t_ms = int(row[0]); o = float(row[1]); h = float(row[2])
                    lo = float(row[3]); c = float(row[4]); v = float(row[5])
                    bars_5m.append({"time": t_ms // 1000, "open": o, "high": h, "low": lo, "close": c, "volume": v})
            bars_10m = _aggregate_bars(bars_5m, 600)
            candles = [[int(b["time"]) * 1000, b["open"], b["high"], b["low"], b["close"], b["volume"]]
                       for b in bars_10m]
            src_log.append(f"agg5m={len(bars_10m)}")
    except Exception as e:
        src_log.append(f"exc:{type(e).__name__}")
        candles = None
    # 兜底：金十 1m → 10m
    if candles is None and asset in _JIN10_ABNORMAL_CODE:
        try:
            payload = jin10_mcp.get_kline(_JIN10_ABNORMAL_CODE[asset], count=600)
            inner = payload.get("data") if payload.get("status") == "ok" else None
            kls = (inner or {}).get("klines") or []
            bars_1m: List[Dict[str, Any]] = []
            for k in kls:
                try:
                    o = float(k["open"]); h = float(k["high"])
                    lo = float(k["low"]); c = float(k["close"]); v = int(k.get("volume") or 0)
                except (TypeError, ValueError, KeyError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                bars_1m.append({"time": int(k["time"]), "open": o, "high": h,
                                "low": lo, "close": c, "volume": v})
            bars_10m = _aggregate_bars(bars_1m, 600)
            candles = [[int(b["time"]) * 1000, b["open"], b["high"], b["low"], b["close"], b["volume"]]
                       for b in bars_10m]
            src_log.append(f"jin10={len(bars_10m)}")
        except Exception:
            candles = None
    # v307 最后兜底：本地 kline_bars 落库数据（外网全挂时仍能跑异动扫描）
    if not candles:
        local = _fetch_local_kline_bars(asset, "10m", limit=288)
        if local:
            candles = local
            src_log.append(f"local={len(local)}")
    if src_log:
        print(f"[fetch_10m] {asset} {' '.join(src_log)} -> {0 if not candles else len(candles)}", flush=True)
    _kline_cache_put(_ck, candles)
    return candles


def _fetch_local_kline_bars(asset: str, timeframe: str, limit: int = 288) -> Optional[List[List[Any]]]:
    """v307：从本地 kline_bars 表取 K 线 → [[ts_ms, o, h, l, c, v], ...] 升序。

    外网数据源(yahoo/okx/binance/金十)全挂时的兜底，保证异动扫描不至于完全瞎。
    """
    try:
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM kline_bars "
                "WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT ?",
                (asset.upper(), timeframe, int(limit)),
            ).fetchall()
        if not rows:
            return None
        out: List[List[Any]] = []
        for r in reversed(rows):
            # v307: get_connection() 的 row_factory 各版本不一致（dict / tuple）→ 两种都兼容
            try:
                ts, o, h, lo, c, v = r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]
            except (TypeError, KeyError, IndexError):
                try:
                    ts, o, h, lo, c, v = tuple(r)[:6]
                except (TypeError, ValueError):
                    continue
            if any(x is None for x in (ts, o, h, lo, c)):
                continue
            ts_i = int(ts)
            ts_ms = ts_i * 1000 if ts_i < 1e12 else ts_i
            out.append([ts_ms, float(o), float(h), float(lo), float(c), float(v or 0)])
        return out or None
    except Exception as e:
        print(f"[fetch_local_kline] {asset} {timeframe} 失败: {type(e).__name__}: {e}", flush=True)
        return None


def _detect_5m_abnormals(asset: str, timeframe: str = "3m") -> List[Dict[str, Any]]:
    """需求4：拉资产 K 线检测异动（单根涨跌≥2.5% 或 量>近20根均值1.5倍，任一触发）。

    v170: 支持 timeframe 选择 —— 3m (默认, 历史兼容) 或 10m (10 分钟 K 线聚合)。
    10m 走 _fetch_10m_candles: 5m 原生 → _aggregate_bars(600) 聚合；OKX 失败时金十 1m 兜底。

    返回最近若干异动点（ts 秒、pct_change、vol_ratio、trigger_type）；无数据/失败返回 []。
    不造假：XAU/WTI 在 OKX 熔断时拿不到 → 金十 get_kline 1m 兜底聚合（国内直连）。
    """
    asset = (asset or "").upper()
    # v221.6: 不再硬编码资产白名单 —— 原来只放行 BTC/ETH/SOL/XAU/WTI，
    #   导致韩股(SKHYNIX)等已接入 _fetch_10m_candles 的品种一律 return []，
    #   前端异动点恒空。改为「该品种能拉到 K 线就检测」。
    tf = "10m" if timeframe == "10m" else "3m"
    now = time.monotonic()
    hit = _5M_ABNORMAL_CACHE.get((asset, tf))
    if hit and now - hit["ts"] < _5M_ABNORMAL_TTL_S:
        return hit["data"]
    candles = _fetch_10m_candles(asset) if tf == "10m" else _fetch_3m_candles(asset)
    data: List[Dict[str, Any]] = []
    if candles:
        # v221：透传 timeframe（→ interval_sec）让阈值自适应（10m=1.5%/1.2×，3m=2.5%/1.5×）
        interval_sec = 600 if tf == "10m" else 180
        data = abnormal_detector.scan_abnormals(candles, limit=8, interval_sec=interval_sec)
        data = [{**a, "ts": int(a["ts"] // 1000), "interval": tf} for a in data]  # 毫秒→秒，对齐前端 bars.time
    # 需求2：附加已完成的 AI 归因（缓存命中则带上；新归因由 _movers_sync 后台排队完成）
    for a in data:
        hit = _5M_ATTRIB_CACHE.get(_attrib_key(asset, a["ts"]))
        if hit and time.monotonic() - hit["ts"] < _5M_ATTRIB_TTL_S:
            a["attribution"] = hit["data"]
    _5M_ABNORMAL_CACHE[(asset, tf)] = {"ts": now, "data": data}
    return data


# ── Phase 2 异常签名（多因子 + 全板块异动）后台落库 ─────────────────────────
_ABNORMAL_SIG_ASSETS = ("BTC", "ETH", "XAU", "SOL", "WTI")
_ABNORMAL_SIG_TTL_S = 300.0        # 同一签名 5 分钟内去重，不重复 INSERT
# v334: multi_factor / sector 是"状态型"签名（复合打分、全板块异动），状态不变就不算新事件。
#   原去重键带 anchor_ts（每根 K 线都变）→ 同一标的同一方向一天能写上百条，
#   实测 99.2% 是冗余，把信号流和统计全淹了。这两类改为：键去掉锚、冷却放宽到 30 分钟。
_ABNORMAL_STATE_KINDS = ("multi_factor", "sector")
_ABNORMAL_STATE_TTL_S = 1800.0
# 这两类在冷却窗口内不再新插入，而是**原地更新那条 open 记录**（方向翻转也算同一条状态），
# 否则方向每根 K 线翻一次照样会写满。状态型签名本来就该是"一条会变的记录"，不是流水。
_ABNORMAL_SIG_INTERVAL_S = 15.0    # 后台轮询间隔
_abnormal_sig_lock = threading.Lock()
_abnormal_sig_seen: Dict[str, float] = {}


def _abnormal_signature_sync(timeframe: str = "3m") -> Dict[str, Any]:
    """Phase 2：BTC/ETH/XAU（+SOL/WTI）统一拉 K 线 → 多因子 + 全板块异动 → 落库。

    v170: timeframe=10m 时改拉 _fetch_10m_candles（5m 原生聚合）；3m 为历史兼容默认。

    返回本轮摘要（triggered / inserted）；纯聚合 + 落库，零 LLM 调用。
    去重键 = asset|signal_kind|锚，TTL 内重复轮询不重复写库。
    """
    by_asset: Dict[str, List[Any]] = {}
    fetch_fn = _fetch_10m_candles if timeframe == "10m" else _fetch_3m_candles
    for asset in _ABNORMAL_SIG_ASSETS:
        candles = fetch_fn(asset)
        if candles:
            by_asset[asset] = candles
    if not by_asset:
        return {"triggered": 0, "inserted": 0, "signals": []}
    # v221：透传 timeframe（→ interval_sec）让阈值自适应（10m=1.5%/1.2×，3m=2.5%/1.5×）
    interval_sec = 600 if timeframe == "10m" else 180
    result = abnormal_detector.eval_assets(by_asset, interval_sec=interval_sec)
    inserted = 0
    rows: List[Dict[str, Any]] = []
    sector_info: Optional[Dict[str, Any]] = None
    try:
        sector_info = result.get("sector") or None
    except Exception:
        sector_info = None
    conn = None
    try:
        conn = db.get_connection()
        now_mono = time.monotonic()
        for sig in result["signals"]:
            kind = sig["signal_kind"]
            state_like = kind in _ABNORMAL_STATE_KINDS
            if state_like:
                # 状态型：键只看资产+类型，方向变化视作同一条记录刷新
                key = f"{sig['asset']}|{kind}"
                ttl = _ABNORMAL_STATE_TTL_S
            else:
                anchor_key = sig.get("anchor_ts") if sig.get("anchor_ts") is not None else sig.get("ts")
                key = f"{sig['asset']}|{kind}|{anchor_key}"
                ttl = _ABNORMAL_SIG_TTL_S
            refresh_row: Optional[int] = None
            with _abnormal_sig_lock:
                seen = _abnormal_sig_seen.get(key)
                if isinstance(seen, tuple):
                    last_mono, last_rid = seen
                else:                          # 兼容旧的纯时间戳写法
                    last_mono, last_rid = seen, None
                if last_mono is not None and now_mono - last_mono < ttl:
                    if state_like and last_rid:
                        refresh_row = last_rid     # 原地刷新，不新增
                    else:
                        continue
                if refresh_row is None:
                    _abnormal_sig_seen[key] = (now_mono, last_rid)
            if refresh_row:
                # 状态型签名：原地刷新方向/明细，保持"一条会变的记录"
                try:
                    conn.execute(
                        """UPDATE abnormal_signals
                              SET direction=?, detail_json=?, hint=?, ts=?, anchor_ts=?
                            WHERE id=? AND status='open'""",
                        (str(sig.get("direction") or ""),
                         json.dumps(sig.get("detail") or {}, ensure_ascii=False),
                         str(sig.get("hint") or ""),
                         int(sig.get("ts") or 0),
                         sig.get("anchor_ts"),
                         int(refresh_row)))
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    print(f"[ABNORMAL-SIG] refresh skip: {type(exc).__name__}: {str(exc)[:60]}")
                continue
            try:
                # v189-8: 包 retry_on_busy,signal 写失败时 5s+退避重试,避免
                # 多 signal 串行 commit 时被 engine 抢锁导致部分 signal 丢失.
                def _record_and_commit():
                    rid = abnormal_detector.record_signal(
                        conn, ts=sig["ts"], asset=sig["asset"], signal_kind=sig["signal_kind"],
                        direction=sig["direction"], anchor_ts=sig["anchor_ts"],
                        detail=sig["detail"], hint=sig["hint"],
                    )
                    # v189-7: 每条 signal 单独 commit,避免 N 个 signal 一个事务持锁 ~N×5ms.
                    conn.commit()
                    return rid
                rid = db.retry_on_busy(_record_and_commit)
            except sqlite3.OperationalError as exc:
                # 单条 signal 入库失败 (lock) 不影响其他 signal.
                print(f"[ABNORMAL-SIG] skip (lock): {type(exc).__name__}: {str(exc)[:80]}")
                continue
            if rid:
                if state_like:
                    with _abnormal_sig_lock:
                        _abnormal_sig_seen[key] = (now_mono, rid)
                inserted += 1
                rows.append({"id": rid, "asset": sig["asset"],
                             "signal_kind": sig["signal_kind"], "direction": sig["direction"]})
                # v182.3：multi_factor 信号自动排队 AI 归因（后台线程，非阻塞）
                if sig["signal_kind"] == "multi_factor":
                    _queue_multi_factor_attribution(rid, sig, sector_info)
    except Exception as exc:
        print(f"[ABNORMAL-SIG] 落库失败: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return {"triggered": len(result["signals"]), "inserted": inserted, "signals": rows}


# v182.3: 多因子异动 AI 归因后台任务
_MF_ATTRIB_PENDING: set = set()
_mf_attrib_lock = threading.Lock()

# v318：多因子归因去重窗口（分钟）。
# 线上实测（trident_event_bus.db）：35,802 条 multi_factor 信号里只有 4,332 个
# 不同的 (asset, anchor_ts)，其中 30,800 条 anchor_ts 为空 —— 同一个持续状态被
# 扫描循环反复上报，每次都排一次 LLM 归因。纯噪音 + 白烧 token。
# 现在同品种同方向的异动在窗口内只归因一次。
_MF_ATTRIB_DEDUP_MINUTES = 30


def _mf_attrib_dedup_hit(conn, asset: str, direction: str, minutes: int) -> bool:
    """同品种同方向的近期异动是否已经做过 AI 归因（是则应跳过）。"""
    try:
        cutoff = (
            datetime.now() - timedelta(minutes=max(1, int(minutes)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            """SELECT COUNT(*) FROM abnormal_signals s
               JOIN abnormal_attributions a ON a.signal_id = s.id
               WHERE s.signal_kind='multi_factor'
                 AND s.asset = ?
                 AND IFNULL(s.direction, '') = IFNULL(?, '')
                 AND s.created_at > ?""",
            (str(asset or ""), str(direction or ""), cutoff),
        ).fetchone()
        return bool(row and int(row[0] or 0) > 0)
    except Exception as exc:
        print(f"[ABN-ATTR] dedup check error (按未命中处理): {exc}", flush=True)
        return False


def _queue_multi_factor_attribution(signal_id: int, sig: Dict[str, Any],
                                    sector_info: Optional[Dict[str, Any]]) -> None:
    """Phase 2 多因子签名后台归因：拉近 30min 新闻 → 调 LLM → 写 abnormal_attributions。

    失败/异常一律静默吞掉，不阻塞 watcher 主循环。前端通过
    /api/abnormal-signals/{id}/attribution 单独获取。

    v318：两道降噪闸门 ——
      ① 只有「≥2 个独立证据族」的异动才排 LLM（单家族多为常态噪音）；
      ② 同品种同方向在去重窗口内已归因过就跳过。
    两条都不影响信号落库（信号仍全部记录，供回查与权重评估）。
    """
    asset = sig.get("asset") or ""
    if not asset or not signal_id:
        return
    detail = sig.get("detail") or {}
    factors = detail.get("factors") or []

    # ① 单家族（未交叉验证）→ 不排 LLM
    if not bool(detail.get("corroborated")):
        print(
            f"[ABN-ATTR] skip (single-family) signal_id={signal_id} asset={asset} "
            f"families={detail.get('families')}",
            flush=True,
        )
        return

    # ② 去重窗口内同品种同方向已归因 → 跳过
    try:
        _dc = db.get_connection()
        try:
            if _mf_attrib_dedup_hit(
                _dc, asset, sig.get("direction") or "", _MF_ATTRIB_DEDUP_MINUTES
            ):
                print(
                    f"[ABN-ATTR] skip (dedup) signal_id={signal_id} asset={asset} "
                    f"dir={sig.get('direction')}",
                    flush=True,
                )
                return
        finally:
            _dc.close()
    except Exception as _dedup_err:
        print(f"[ABN-ATTR] dedup check failed (继续归因): {_dedup_err}", flush=True)

    with _mf_attrib_lock:
        if signal_id in _MF_ATTRIB_PENDING:
            return
        _MF_ATTRIB_PENDING.add(int(signal_id))

    def _run() -> None:
        try:
            anchor_ts = sig.get("anchor_ts")
            if anchor_ts is None:
                anchor_ts = sig.get("ts")
            anchor_ts = int(anchor_ts or 0)
            news = _abnormal_news(asset, anchor_ts, window_s=1800)
            try:
                from engine.attribute_multi_factor import attribute_multi_factor
                result = attribute_multi_factor(
                    asset=asset, anchor_ts=anchor_ts,
                    direction=sig.get("direction") or "",
                    factors=factors, indicators=detail,
                    sector_info=sector_info, news_before=news,
                )
            except Exception as exc:
                print(f"[ABN-ATTR] import/call fail: {type(exc).__name__}: {exc}", flush=True)
                return
            if "error" in result:
                print(f"[ABN-ATTR] signal_id={signal_id} {asset}: {result['error']}", flush=True)
                return
            c = None
            try:
                c = db.get_connection()
                abnormal_detector.record_attribution(
                    c, signal_id=int(signal_id), asset=asset,
                    signal_kind="multi_factor", anchor_ts=anchor_ts,
                    summary=result.get("summary", ""),
                    drivers=result.get("drivers", []),
                    cross_asset_likely=result.get("cross_asset_likely", []),
                    actionable=result.get("actionable", "观望"),
                    severity_hint=result.get("severity_hint", 1),
                    source_model="ai_worker_roster",
                    used_news_n=len(news),
                )
                c.commit()
                print(f"[ABN-ATTR] signal_id={signal_id} {asset} summary={result.get('summary', '')[:60]!r}", flush=True)
            except Exception as exc:
                print(f"[ABN-ATTR] record fail: {type(exc).__name__}: {exc}", flush=True)
            finally:
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[ABN-ATTR] unhandled: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with _mf_attrib_lock:
                _MF_ATTRIB_PENDING.discard(int(signal_id))

    threading.Thread(target=_run, daemon=True).start()


async def abnormal_signature_watcher() -> None:
    print("[ABNORMAL-SIG] watcher started", flush=True)
    while True:
        started = time.monotonic()
        try:
            summary = await asyncio.to_thread(_abnormal_signature_sync)
            if summary["inserted"]:
                print(f"[ABNORMAL-SIG] 落库 {summary['inserted']} 条异常签名", flush=True)
        except Exception as exc:
            print(f"[ABNORMAL-SIG] error: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, _ABNORMAL_SIG_INTERVAL_S - elapsed))


# 2026-11-17: 本条已迁到 routers/abnormal.py（签名扫描助手仍在本文件）。


# 2026-11-17: 本条已迁到 routers/abnormal.py（签名扫描助手仍在本文件）。


# 2026-11-17: 本条已迁到 routers/abnormal.py（签名扫描助手仍在本文件）。


# 2026-11-17: 本条已迁到 routers/abnormal.py（签名扫描助手仍在本文件）。


# ── v170: 异常签名全量重算（10m interval）─────────────────────────────────
# 2026-11-17: 本条已迁到 routers/abnormal.py（签名扫描助手仍在本文件）。


# ── v221.5: 历史回填 + 反馈统计 ─────────────────────────────────────────────
#   backfill: 用户切 10m 时已有历史 K 线（最多 ~144 根 = 24h），重跑一次扫描写入 abnormal_signals。
#   feedback-stats: 给前端 4 个量化指标（forward_precision / reverse_recall / missed_count / pass_block_rate），
#                   闭环「异动检测」→「事后验证」→「标注可靠性」。
# 2026-11-17: 本条已迁到 routers/abnormal.py。


# 2026-11-17: 本条已迁到 routers/abnormal.py。


# ── v221.3: 拐点 marker + 事前氛围核实 ───────────────────────────────────
#   用户 2026-10-05 反馈"归因页缺拐点标识 + 真实原因 + 内部回写"：
#     · 拐点 = swing / cum4 触发（用户视角的"反转点"）
#     · 事前氛围 = 拐点前 K 小时窗口内的同向新闻密度 + 同向资产 preheat 比例
#       → 高氛围 = 已预期（市场早有铺垫，拐点是兑现）
#       → 低氛围 = 凭空突袭（无铺垫，拐点是意外）
#     · 内部回写 = 拐点+原因回写到 ai_decisions.reasoning（或 reflection）
_SWING_POINTS_TTL_S = 60.0
_SWING_POINTS_CACHE: Dict[str, Dict[str, Any]] = {}


def _pre_event_atmosphere(
    asset: str,
    ts: int,
    *,
    pre_window_hours: int = 4,
) -> Dict[str, Any]:
    """v221.3：计算拐点前 ``pre_window_hours`` 小时窗口的氛围分数。

    维度 1：同向新闻密度
      查 raw_news 在 [ts - pre_window_hours*3600, ts] 之间、与该资产相关的
      非噪声新闻条数 + 其中「方向=拐点方向」的占比。

    维度 2：同向资产 preheat 比例
      同一时段，其他资产中 preheat 比例 >= 0.6×（前 24h 涨幅）的比例。

    返回 atmosphere_score ∈ [0,1] + label ∈ {expected, surprise}。
    """
    asset_up = (asset or "").upper()
    if not asset_up:
        return {"atmosphere_score": 0.0, "label": "surprise", "pre_window_hours": pre_window_hours}
    pre_from = int(ts - pre_window_hours * 3600)
    pre_to = int(ts)
    out: Dict[str, Any] = {
        "pre_window_hours": pre_window_hours,
        "same_dir_news_count": 0,
        "total_news_count": 0,
        "same_dir_news_ratio": 0.0,
        "preheat_ratio": 0.0,
        "atmosphere_score": 0.0,
        "label": "surprise",
    }
    try:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN UPPER(COALESCE(ai.suggested_action, '')) IN ('BUY','SELL')
                              AND UPPER(COALESCE(ai.target_asset, '')) = ?
                             THEN 1 ELSE 0 END) AS same_dir
                  FROM raw_news rn
                  LEFT JOIN ai_decisions ai
                    ON ai.news_id = rn.id
                   AND UPPER(ai.target_asset) = ?
                 WHERE rn.timestamp BETWEEN ? AND ?
                   AND UPPER(COALESCE(rn.asset, '')) = ?
                """,
                (asset_up, asset_up, pre_from, pre_to, asset_up),
            )
            row = cur.fetchone()
            if row:
                total = int(row[0] or 0)
                same = int(row[1] or 0)
                out["total_news_count"] = total
                out["same_dir_news_count"] = same
                out["same_dir_news_ratio"] = round(same / total, 4) if total else 0.0
        finally:
            conn.close()
    except Exception as exc:
        print(f"[ATMOS] news scan failed: {type(exc).__name__}: {exc}", flush=True)

    # 维度 2：同向资产 preheat — 复用 _cached_market_prices 的 24h change24h
    try:
        # 2026-11-17: 后台分析路径用 allow_stale —— 有旧值立刻返回，不阻塞 13.5s 冷算
        prices = _cached_market_prices(allow_stale=True) or {}
        items = prices.get("items") or []
        preheat_count = 0
        total_assets = 0
        for it in items:
            chg = it.get("change24h")
            if chg is None:
                continue
            total_assets += 1
            if abs(float(chg)) >= 0.6:
                preheat_count += 1
        out["preheat_ratio"] = round(preheat_count / total_assets, 4) if total_assets else 0.0
    except Exception as exc:
        print(f"[ATMOS] preheat scan failed: {type(exc).__name__}: {exc}", flush=True)

    score = (out["same_dir_news_ratio"] + out["preheat_ratio"]) / 2.0
    out["atmosphere_score"] = round(score, 4)
    out["label"] = "expected" if score >= 0.5 else "surprise"
    return out


def _swing_points_sync(
    asset: str,
    timeframe: str = "3m",
    pre_window_hours: int = 4,
) -> List[Dict[str, Any]]:
    """v221.3：拉资产 K 线 → 提取 swing/cum4 → 算事前氛围 → 返拐点 marker 列表。

    用于归因页拐点图层（与 pct/volume/both 的 belowBar marker 区分）。
    返回字段：ts, trigger_type, pct_change, range_pct, direction, atmosphere
    """
    asset_up = (asset or "").upper()
    if not asset_up:
        return []
    cache_key = f"{asset_up}|{timeframe}|{pre_window_hours}"
    now = time.monotonic()
    hit = _SWING_POINTS_CACHE.get(cache_key)
    if hit and now - hit["ts"] < _SWING_POINTS_TTL_S:
        return hit["data"]

    fetch_fn = _fetch_10m_candles if timeframe == "10m" else _fetch_3m_candles
    candles = fetch_fn(asset_up)
    if not candles:
        _SWING_POINTS_CACHE[cache_key] = {"ts": now, "data": []}
        return []
    interval_sec = 600 if timeframe == "10m" else 180
    raw = abnormal_detector.scan_abnormals(candles, limit=200, interval_sec=interval_sec)
    points: List[Dict[str, Any]] = []
    for r in raw:
        # v221.6: 原来只保留 swing/cum4，把 volume/pct/big_candle 全部丢弃 ——
        #   前端靠这个接口拿 belowBar 异动圆点，量能点因此恒为空。现在全量返回。
        ts_sec = int(r["ts"] // 1000)
        pct = float(r.get("pct_change") or 0.0)
        direction = "bullish" if pct > 0 else ("bearish" if pct < 0 else "neutral")
        ttype = r.get("trigger_type")
        item = {
            "ts": ts_sec,
            "trigger_type": ttype,
            "pct_change": round(pct, 4),
            "vol_ratio": r.get("vol_ratio"),
            "direction": direction,
        }
        # 事前氛围 + range_pct 仅对拐点类有意义，其余类型不额外做慢查询
        if ttype in ("swing", "cum4"):
            item["range_pct"] = r.get("range_pct")
            item["atmosphere"] = _pre_event_atmosphere(
                asset_up, ts_sec, pre_window_hours=pre_window_hours
            )
        points.append(item)
    _SWING_POINTS_CACHE[cache_key] = {"ts": now, "data": points}
    return points


# 2026-11-17: 本条已迁到 routers/abnormal.py。


# ── v221.3: 拐点 → ai_decisions.reasoning 内部回写 ─────────────────────────
#   用户 2026-10-05 反馈第三腿："归因页缺拐点标识 + 真实原因 + 内部回写"。
#   设计：
#     · 给每个 swing point 找相邻（同 asset + ±30min）的 ai_decisions，
#       在 reasoning 末尾追加 `[v221.3 swing: ...]` 标记 + 氛围 label。
#     · 幂等：每个决策已存在 tag → 跳过，不重复追加。
#     · 限速：每个拐点最多写 N 条决策（避免一次性污染大量）。
#     · 不引入新表（铁律 #1），仅 UPDATE reasoning 字段。
#     · 不抢锁：用短事务 + 不触发反射循环。
_SWING_WRITEBACK_TAG_PREFIX = "[v221.3 swing:"


def _format_swing_annotation(point: Dict[str, Any]) -> str:
    """v221.3：把一个 swing point 格式化成一行的 reasoning 后缀。

    示例：``[v221.3 swing: type=swing dir=bullish atmos=expected score=0.62 ts=2026-10-05 12:34:56]``
    """
    ts_sec = int(point.get("ts") or 0)
    iso = (
        datetime.fromtimestamp(ts_sec, TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        if ts_sec
        else "n/a"
    )
    atmos = point.get("atmosphere") or {}
    return (
        f"[v221.3 swing: type={point.get('trigger_type') or 'n/a'} "
        f"dir={point.get('direction') or 'n/a'} "
        f"atmos={atmos.get('label') or 'n/a'} "
        f"score={atmos.get('atmosphere_score', 0.0):.2f} "
        f"ts={iso}]"
    )


def _writeback_swing_to_decisions(
    asset: str,
    points: List[Dict[str, Any]],
    *,
    match_window_sec: int = 1800,
    max_per_point: int = 5,
) -> Dict[str, Any]:
    """v221.3：把 swing points 内部回写到相邻 ai_decisions.reasoning。

    Args:
        asset: 资产（已 .upper()）
        points: _swing_points_sync 返回的拐点列表
        match_window_sec: 决策 created_at 与 swing.ts 的允许偏差（默认 30 分钟）
        max_per_point: 每个拐点最多写多少条决策（默认 5，避免一次性污染大量）

    Returns: {"scanned": N, "matched": M, "updated": K, "skipped_existing": S, "errors": E}
      · scanned: 总决策扫描数（所有 swing point 加和）
      · matched: 找到匹配但已有 tag 的决策数（跳过）
      · updated: 实际 UPDATE 行数
      · skipped_existing: 同 matched 别名（保留字段兼容）
    """
    asset_up = (asset or "").upper()
    out: Dict[str, Any] = {
        "asset": asset_up,
        "scanned": 0,
        "matched": 0,
        "updated": 0,
        "skipped_existing": 0,
        "errors": 0,
    }
    if not asset_up or not points:
        return out

    try:
        conn = db.get_connection()
    except Exception as exc:
        print(f"[WB-SWING] db open failed: {type(exc).__name__}: {exc}", flush=True)
        out["errors"] += 1
        return out
    try:
        for pt in points:
            ts_sec = int(pt.get("ts") or 0)
            if ts_sec <= 0:
                continue
            win_lo = ts_sec - match_window_sec
            win_hi = ts_sec + match_window_sec
            try:
                cur = conn.execute(
                    """
                    SELECT id, reasoning
                      FROM ai_decisions
                     WHERE target_asset = ?
                       AND CAST(strftime('%s', created_at) AS INTEGER)
                           BETWEEN ? AND ?
                     ORDER BY ABS(CAST(strftime('%s', created_at) AS INTEGER) - ?) ASC
                     LIMIT ?
                    """,
                    (asset_up, win_lo, win_hi, ts_sec, max_per_point),
                )
            except Exception as exc:
                print(
                    f"[WB-SWING] SELECT failed for ts={ts_sec}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                out["errors"] += 1
                continue
            rows = cur.fetchall()
            out["scanned"] += len(rows)
            if not rows:
                continue
            annotation = _format_swing_annotation(pt)
            for row in rows:
                decision_id = row["id"] if hasattr(row, "keys") else row[0]
                existing = row["reasoning"] if hasattr(row, "keys") else row[1]
                existing = (existing or "").strip()
                if _SWING_WRITEBACK_TAG_PREFIX in existing:
                    out["matched"] += 1
                    out["skipped_existing"] += 1
                    continue
                new_reasoning = (existing + "\n\n" + annotation).strip() if existing else annotation
                try:
                    conn.execute(
                        "UPDATE ai_decisions SET reasoning = ? WHERE id = ?",
                        (new_reasoning, decision_id),
                    )
                    out["updated"] += 1
                except Exception as exc:
                    print(
                        f"[WB-SWING] UPDATE failed for id={decision_id}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    out["errors"] += 1
        try:
            conn.commit()
        except Exception as exc:
            print(f"[WB-SWING] COMMIT failed: {type(exc).__name__}: {exc}", flush=True)
            out["errors"] += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# 2026-11-17: 本条已迁到 routers/abnormal.py。

def _movers_sync(timeframe: str = "3m") -> Dict[str, Any]:
    """聚合排行与事件标注数据（同步，供 asyncio.to_thread 调用）。

    排行价格/涨跌幅：
      · 加密 BTC/ETH/SOL：market_feeds 多所中位数（24h% 各所中位数）
      · 黄金/原油 XAU/WTI：进程内存实时价 + EastMoney f170 的 24h 涨跌幅
    事件标注：news_events（非噪声、有具体资产）LEFT JOIN raw_news 取原文，
      时间窗口最近 30 天，与前端 4h K 线 30 天窗口对齐；3m/10m 短窗仍按 t0~t1 过滤。
    """
    now_iso = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    items: List[Dict[str, Any]] = []

    # 1) 加密三币：直接复用 15s 缓存的聚合行情
    # 2026-11-17: 事件标注用 allow_stale（同上），冷启动不拖住建图
    crypto = _cached_market_prices(allow_stale=True)
    for it in crypto.get("items") or []:
        chg = it.get("change24h")
        items.append({
            "asset": str(it.get("asset", "")),
            "price": it.get("price"),
            "change24h": round(float(chg), 4) if chg is not None else None,
            "source": it.get("source", ""),
            "source_count": it.get("sourceCount", 0),
        })

    # 2) 黄金/原油：watcher 持续更新的内存实时价 + 24h 涨跌（EastMoney；WTI 兜底库内日线）
    for asset in ("XAU", "WTI"):
        price = _last_gold_price if asset == "XAU" else _last_wti_price
        chg = _cached_change24h(asset)
        src = "EastMoney"
        if not chg and asset == "WTI":
            chg = _wti_change24h_from_kline()
            if chg is not None:
                src = "库内日线"
        if price is None:
            # v367: 内存实时价没起来时，用 cn 链(腾讯/新浪)补一个真实现价
            try:
                _p, _ = _yahoo_price_change24h(asset)
                if _p:
                    price, src = _p, "cn"
            except Exception:
                pass
        if price is not None:
            items.append({
                "asset": asset,
                "price": price,
                "change24h": round(float(chg), 4) if chg else None,
                "source": src,
                "source_count": None,
            })

    # 海力士 cap（v??）：归因页 K 线 + 全盘 movers 都新增 SKHYNIX（韩国海力士）+ US_HYNIX/MU（美国海力士）
    #   价格与 24h 涨跌走 yfinance（quote_sources.YAHOO_SYMBOL_FOR_ASSET 已映射 000660.KS / MU），
    #   Yahoo 直连有限速+熔断（quote_sources 内部），失败时该品种仍出现但 price/change=null（前端降级展示）。
    #
    # v2026.10.08 bugfix: 顺序两个 _yahoo_price_change24h 在 Yahoo/yfinance 限速时累计 6-10s，
    #   触发浏览器 fetch abort（"Failed to fetch"）。改成单层 ThreadPoolExecutor 并行，
    #   4s 超时 — 内层线程未完成时直接丢弃（线程在后台继续跑，结果入下次缓存），
    #   外层 pool 不阻塞等所有 inner thread。
    # v367: 韩股 SKHYNIX 已移出交易范围 → 不再出现在 movers（原来恒为 None 还占位）
    _HYNIX_MOVER_TICKERS = (
        ("MU", "cn"),   # Micron — 美股口径的"美国海力士"，走腾讯/新浪直连
    )
    import concurrent.futures as _cf
    def _hynix_one(asset_src):
        asset, src = asset_src
        try:
            price, chg = _yahoo_price_change24h(asset)
        except Exception as exc:
            print(f"[MOVERS] {asset} 行情拉取异常: {type(exc).__name__}: {exc}", flush=True)
            return (asset, src, None, None)
        return (asset, src, price, chg)
    hynix_results: Dict[str, tuple] = {}
    pool = _cf.ThreadPoolExecutor(max_workers=len(_HYNIX_MOVER_TICKERS))
    try:
        future_to_asset = {
            pool.submit(_hynix_one, ticker): ticker[0]
            for ticker in _HYNIX_MOVER_TICKERS
        }
        try:
            for fut in _cf.as_completed(future_to_asset, timeout=2.0):
                asset_name = future_to_asset[fut]
                try:
                    asset, src, price, chg = fut.result()
                except Exception as exc:
                    print(f"[MOVERS] {asset_name} 拉取异常: {type(exc).__name__}: {exc}", flush=True)
                    hynix_results[asset_name] = (None, None, "Yahoo Finance")
                    continue
                hynix_results[asset_name] = (price, chg, src)
        except _cf.TimeoutError:
            print(f"[MOVERS] 海力士行情拉取 2s 总超时，未完成的放后台继续", flush=True)
    finally:
        # wait=False：未完成的线程在后台继续跑（结果入下次缓存），主请求立即返回
        pool.shutdown(wait=False)
    # 任何还没在结果里的 asset 都置空
    for asset, src in _HYNIX_MOVER_TICKERS:
        if asset not in hynix_results:
            hynix_results[asset] = (None, None, src)
    for asset, src in _HYNIX_MOVER_TICKERS:
        price, chg, src_label = hynix_results.get(asset, (None, None, src))
        items.append({
            "asset": asset,
            "price": price,
            "change24h": round(chg, 4) if chg is not None else None,
            "source": src_label if price is not None else None,
            "source_count": None,
        })

    # 3) 每个品种最近 30 天的关联新闻事件（多源并列，供前端标注在 K 线时间轴 + 知识图谱存档）
    #    用户 2026-10-03 反馈：「至少显示生成近 30 天的这三类所有事件」「作为知识图谱参考」
    #    → 视野从 72h 扩到 30d，前端图表窗口切到 4h×180=30d 可看全；本地存档自动覆盖整 30 天
    events_by_asset: Dict[str, List[Dict[str, Any]]] = {}
    horizon_ts = int(time.time()) - 30 * 86400
    try:
        with db.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ne.ts, ne.asset, ne.action, ne.score, ne.news_id,
                          rn.content, rn.source
                   FROM news_events ne
                   LEFT JOIN raw_news rn ON rn.id = ne.news_id
                   WHERE ne.is_noise = 0 AND ne.asset != 'NONE' AND ne.ts >= ?
                   ORDER BY ne.ts DESC
                   LIMIT 5000""",
                (horizon_ts,),
            ).fetchall()
            # v20251014: 拉最近 24h 的 signal_weight 映射 (news_id + asset → weight)
            #   用于前端 K 线 marker 颜色饱和度 (strong / medium / weak / cold).
            #   失败/无表/无数据 → 返回空 dict, 不影响主流程.
            sw_by_key: Dict[tuple, Dict[str, Any]] = {}
            try:
                sw_rows = conn.execute(
                    """SELECT news_id, target_asset, signal_weight, weight_tier,
                              weight_explain, cold_signal
                       FROM ai_decisions
                       WHERE ts >= ? AND is_sibling = 0
                             AND signal_weight IS NOT NULL
                             AND suggested_action IN ('BUY','SELL')""",
                    (int(time.time()) - 24 * 3600,),
                ).fetchall()
                for sw in sw_rows:
                    key = (int(sw["news_id"]), (sw["target_asset"] or "").upper())
                    sw_by_key[key] = {
                        "signal_weight": sw["signal_weight"],
                        "weight_tier": sw["weight_tier"] or "",
                        "weight_explain": sw["weight_explain"] or "",
                        "cold_signal": bool(sw["cold_signal"]),
                    }
            except sqlite3.OperationalError:
                pass  # signal_weight columns not yet migrated
            for r in rows:
                asset = (r["asset"] or "NONE").upper()
                content = re.sub(r"^\[hash:\w+\]\s*", "", r["content"] or "").strip()
                if not content:
                    continue
                ev: Dict[str, Any] = {
                    "ts": int(r["ts"]),
                    "action": (r["action"] or "HOLD").upper(),
                    "score": round(float(r["score"]), 3) if r["score"] is not None else 0.0,
                    "content": content[:160],
                    "source": r["source"] or "",
                }
                # v20251014: 尝试附上 signal_weight (key = news_id + asset)
                try:
                    news_id = int(r["news_id"]) if r["news_id"] is not None else None
                except Exception:
                    news_id = None
                sw = sw_by_key.get((news_id, asset)) if news_id is not None else None
                ev["signal_weight"] = sw["signal_weight"] if sw else None
                ev["weight_tier"] = sw["weight_tier"] if sw else ""
                ev["weight_explain"] = sw["weight_explain"] if sw else ""
                ev["cold_signal"] = sw["cold_signal"] if sw else False
                events_by_asset.setdefault(asset, []).append(ev)
    except Exception as exc:
        print(f"[MOVERS] 事件查询失败(忽略): {type(exc).__name__}: {exc}", flush=True)

    # v2026.10.08 bugfix: 顺序 _detect_5m_abnormals 在 5 个币种冷启动时累计 5-10s
    #   （Binance/OKX K 线拉取串行）。改成预热异动缓存后再读取：先在并行池里触发
    #   异动检测，结果落 _5M_ABNORMAL_CACHE 后再串行读出来（每品种 < 100ms）。
    ab_assets = [m["asset"] for m in items if m["asset"] in ("BTC", "ETH", "SOL", "XAU", "WTI")]
    if ab_assets:
        _pool = _cf.ThreadPoolExecutor(max_workers=len(ab_assets))
        try:
            _futs = {a: _pool.submit(_detect_5m_abnormals, a, timeframe) for a in ab_assets}
            for fut in _cf.as_completed(_futs, timeout=2.0):
                a = _futs[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[MOVERS] {a} 异动预热失败/超时: {type(exc).__name__}: {exc}", flush=True)
        except _cf.TimeoutError:
            print(f"[MOVERS] 异动预热 2s 总超时", flush=True)
        except Exception:
            pass
        finally:
            _pool.shutdown(wait=False)

    for m in items:
        m["events"] = events_by_asset.get(m["asset"], [])
        # 需求4：异动检测 → 反推星标（时间锚 + 触发类型）。
        # v170: timeframe 从 /api/market/movers?timeframe= 透传（3m/10m）
        # 现在异动数据已预热，命中 _5M_ABNORMAL_CACHE 直接返回，~毫秒级
        m["abnormals"] = _detect_5m_abnormals(m["asset"], timeframe=timeframe)
        # 需求2：对最新异动点后台排队 AI 归因（不阻塞响应）
        _queue_attribution(m["asset"], m["abnormals"])
        # 需求：星标事件 AI 归因 → 找出行情主要驱动事件（后台排队，缓存命中即带）
        _queue_driver_analysis(m["asset"], m["events"])
        hit = _MARKET_DRIVER_CACHE.get(m["asset"])
        if hit and time.monotonic() - hit["ts"] < _MARKET_DRIVER_TTL_S:
            m["drivers"] = hit["data"]

    # 按涨跌幅从高到低（无涨跌幅的置底）
    items.sort(key=lambda x: (x["change24h"] is None, -(x["change24h"] or 0.0)))
    return {"status": "ok", "items": items, "updated_at": now_iso}


# 2026-11-17: 本条已迁到 routers/market_quotes.py。


# 2026-11-17: 本条已迁到 routers/market_quotes.py。


# v278 — 实时订单簿深度（归因 K 线叠加用）
#   CRYPTO → Binance 现货 /api/v3/depth（国内直连 data-api.binance.vision，limit 5-100）
#   XAU/XAG/WTI → OKX 永续 /api/v5/market/books?sz=20（sni 阻断时直接 None，不报错）
#   缓存 10s 避免每帧重拉。
_DEPTH_CACHE: Dict[str, Dict[str, Any]] = {}
_DEPTH_CACHE_LOCK = threading.Lock()
_DEPTH_TTL_S = 10.0


def _depth_sync(asset: str, limit: int = 100) -> Dict[str, Any]:
    asset = (asset or "").upper()
    try:
        limit = int(limit or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(5, min(limit, 100))
    if not asset:
        return {"status": "error", "asset": asset, "error": "missing asset", "bids": [], "asks": []}
    with _DEPTH_CACHE_LOCK:
        hit = _DEPTH_CACHE.get(asset)
        if hit and time.monotonic() - hit["ts"] < _DEPTH_TTL_S:
            return hit["data"]
    # 路由
    try:
        from feeds import quote_sources as _qs
    except Exception:
        _qs = None  # type: ignore
    data: Dict[str, Any] = {"status": "ok", "asset": asset, "bids": [], "asks": [], "ts_ms": int(time.time() * 1000), "source": ""}
    if _qs:
        # 走 asset 路由表（route() 返回 { market, symbol }）
        try:
            r = _qs.route(asset)
        except Exception:
            r = None
        if r and r.get("market") == "binance_fapi" and r.get("symbol"):
            ob = _qs.fetch_binance_spot_orderbook(r["symbol"], limit=limit)
            if ob:
                data["bids"] = ob["bids"]
                data["asks"] = ob["asks"]
                data["source"] = f"binance_spot:{r['symbol']}"
        elif r and r.get("market") == "okx_swap" and r.get("symbol"):
            ob = _qs.fetch_okx_orderbook(r["symbol"], sz=min(limit, 20))
            if ob:
                data["bids"] = ob["bids"]
                data["asks"] = ob["asks"]
                data["source"] = f"okx_swap:{r['symbol']}"
        if not data["bids"] and not data["asks"]:
            # fallback：Binance 现货镜像直接按 symbol 试一次（仅对 USDT 交易对生效）
            sym_map = {
                "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
                "BNB": "BNBUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT",
            }
            sym = sym_map.get(asset)
            if sym and _qs:
                ob = _qs.fetch_binance_spot_orderbook(sym, limit=limit)
                if ob:
                    data["bids"] = ob["bids"]
                    data["asks"] = ob["asks"]
                    data["source"] = f"binance_spot:{sym}"
    if not data["bids"] and not data["asks"]:
        data["status"] = "unavailable"
    with _DEPTH_CACHE_LOCK:
        _DEPTH_CACHE[asset] = {"ts": time.monotonic(), "data": data}
    return data


# 2026-11-17: 本条已迁到 routers/market_quotes.py。


def _attribute_sync(asset: str, ts: int) -> Dict[str, Any]:
    asset = (asset or "").upper()
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if not asset or not ts:
        return {"asset": asset, "ts": ts, "attribution": {"error": "missing asset/ts"}}

    key = _attrib_key(asset, ts)
    with _5M_ATTRIB_LOCK:
        hit = _5M_ATTRIB_CACHE.get(key)
        if hit and time.monotonic() - hit["ts"] < _5M_ATTRIB_TTL_S:
            return {"asset": asset, "ts": ts, "attribution": hit["data"]}

    # 从 3m 异动缓存里取该点涨跌/量比/触发类型（取不到就传未知，AI 不依赖单点数字）
    pct = vol = None
    trig = ""
    ab_hit = _5M_ABNORMAL_CACHE.get(asset)
    if ab_hit and time.monotonic() - ab_hit["ts"] < _5M_ABNORMAL_TTL_S:
        for a in ab_hit["data"]:
            if int(a.get("ts") or 0) == ts:
                pct = a.get("pct_change")
                vol = a.get("vol_ratio")
                trig = a.get("trigger_type") or ""
                break

    news = _abnormal_news(asset, ts)
    try:
        from engine import ai_worker as _aw
        attr = _aw.attribute_5m_abnormal(asset, ts, pct, vol, trig, news)
    except Exception as exc:
        attr = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    with _5M_ATTRIB_LOCK:
        _5M_ATTRIB_CACHE[key] = {"ts": time.monotonic(), "data": attr}
    return {"asset": asset, "ts": ts, "attribution": attr}


def _store_klines(asset: str, timeframe: str, bars: List[Dict[str, Any]]) -> None:
    """把实时拉到的 K 线落库（ts 毫秒）—— 供实时源失败时回退显示历史。幂等、静默。"""
    if not bars:
        return
    try:
        # v189-6: 单 INSERT 多 VALUES 替 executemany.
        # executemany N=100 持锁 ~N×5ms; 单 INSERT + 单 commit ≈ 5-10ms.
        rows = [
            (asset, timeframe, int(int(b["time"]) * 1000),
             b.get("open"), b.get("high"), b.get("low"),
             b.get("close"), b.get("volume"))
            for b in bars
        ]
        placeholders = ",".join(["(?,?,?,?,?,?,?,?)"] * len(rows))
        params: list = []
        for r in rows:
            params.extend(r)
        with db.get_connection() as conn:
            conn.execute(
                f"INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume) "
                f"VALUES {placeholders} "
                f"ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET "
                f"open=excluded.open, high=excluded.high, low=excluded.low, "
                f"close=excluded.close, volume=excluded.volume",
                params,
            )
            conn.commit()
    except Exception as _kl_exc:
        # v322: K 线落库失败 → 实时源再失败时没有兜底数据，图表直接空
        print(f"[KLINE-CACHE] ⚠️ K线落库失败: {type(_kl_exc).__name__}: "
              f"{str(_kl_exc)[:120]}", flush=True)


def _load_klines_from_db(asset: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    """从库读历史 K 线（ts 毫秒 → time 秒），供实时源失败时兜底。"""
    try:
        with db.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM kline_bars "
                "WHERE symbol=? AND timeframe=? ORDER BY ts ASC",
                (asset, timeframe),
            ).fetchall()
        return [
            {"time": int(int(r["ts"]) // 1000),
             "open": round(r["open"], 4) if r["open"] is not None else None,
             "high": round(r["high"], 4) if r["high"] is not None else None,
             "low": round(r["low"], 4) if r["low"] is not None else None,
             "close": round(r["close"], 4),
             "volume": r["volume"]}
            for r in rows if r["close"] is not None
        ][-max(1, int(limit)):]
    except Exception:
        return []


def _history_sync(asset: str, interval: str, limit: int, days_back: int = 0) -> Dict[str, Any]:
    """v221.7: 加 days_back 参数支持 30 天跨周回溯。

    days_back=0（或 ≤2）→ 走原 _fetch_binance_klines 单页路径（默认行为）
    days_back≥7 → 走 _paged_history 分页拉取（30d × 144/d ≈ 4320 bars）

    v221.7 cache: 30 天扫描 2160 bars + scan_anomalies ~10s，外层缓存 300s
    避免前端刷新反复打。key = asset:interval:limit:days_back（不含 ts，前端刷新友好）。
    """
    asset = (asset or "").upper()
    # 别名归一：图表/库内统一用标准代码，避免 OIL/GOLD 与 WTI/XAU 落库分裂
    if asset == "OIL":
        asset = "WTI"
    elif asset == "GOLD":
        asset = "XAU"
    interval = interval if interval in ("1m", "3m", "5m", "10m", "15m", "1h", "4h", "1d") else "1h"

    # v221.7: 外层缓存 — 30d scan_anomalies ~10s，必须缓存避免反复打
    # v2026.10.08 bugfix: 失败结果用短 TTL，避免「归因页 5min 锁死空数据」：
    #   之前所有结果都按 300s 缓存，yfinance 一次失败后归因页连续 5min 拿不到 K 线，
    #   即便 yfinance 已恢复。分桶：成功 300s，失败 20s。
    outer_key = f"{asset}:{interval}:{limit}:{days_back}"
    outer_now = time.monotonic()
    outer_hit = _HISTORY_SYNC_CACHE.get(outer_key)
    if outer_hit:
        cached_data = outer_hit["data"]
        is_failure = not cached_data.get("bars") and cached_data.get("missing")
        outer_ttl = 20.0 if is_failure else _HISTORY_SYNC_TTL_S
        if outer_now - outer_hit["ts"] < outer_ttl:
            return cached_data

    bars: List[Dict[str, Any]] = []
    missing = ""

    # v221.7: days_back 决定拉取窗口
    use_paged = days_back and days_back >= 7
    bars_per_day = {
        "1m": 1440, "3m": 480, "5m": 288, "10m": 144,
        "15m": 96, "1h": 24, "4h": 6, "1d": 1,
    }
    if use_paged:
        total_needed = days_back * bars_per_day.get(interval, 144)
        # 上限 8000 防滥用（5m 30d = 8640，超过单次可拉到 6×1000）
        total_needed = min(total_needed, 8000)

    # 有真实源或金十兜底的品种：BTC/ETH/SOL（币安）、XAU/XAG（OKX 永续）、WTI/OIL（金十 MCP 1m）
    # 海力士 cap（v??）：新增 海力士/海力士同行（美股） Yahoo Finance 兜底 K 线
    _YAHOO_HISTORY_ASSETS = (
        # 韩国海力士 (KOSPI) + 美国海力士同行 Micron（中文圈用语）
        "SKHYNIX", "US_HYNIX", "MU",
        # 其他股票类资产（与已有 ASSET_ROUTE yahoo 行保持一致）
        "NVDA", "TSLA", "AAPL", "MSTR", "MSFT", "AMZN", "GOOGL",
        "COIN", "PLTR", "META", "SMCI", "MARA", "RIOT",
    )
    if asset in ("BTC", "ETH", "SOL", "XAU", "XAG", "WTI"):
        if use_paged and asset in ("BTC", "ETH", "SOL"):
            # v221.7: 30 天场景走分页拉取
            src_interval = "5m" if interval == "10m" else interval
            bars = _paged_history(asset, src_interval, total_needed)
            if not bars:
                # 分页失败 → 回退原单页路径
                bn = _fetch_binance_klines(asset, src_interval)
                bars = (bn[0] or []) if bn else []
        else:
            # 原路径：单页拉取（≤24h）
            src_interval = "5m" if interval == "10m" else interval
            bn = _fetch_binance_klines(asset, src_interval)
            if bn:
                bars = bn[0] or []
        # v221.7 fix: 无论分页还是单页，10m 路径都要补做 5m→10m 聚合（src_interval 总是 5m when interval=10m）
        if interval == "10m" and bars:
            bars = _aggregate_bars(bars, 600)
        if bars and asset in ("BTC", "ETH", "SOL"):
            _store_klines(asset, interval, bars)
        if not bars:
            # 实时源失败 → 回退库内历史 K 线，结合当时情况归因（不空图）
            tf = ("1d" if interval == "1d" else interval) if asset == "WTI" else interval
            bars = _load_klines_from_db(asset, tf, total_needed if use_paged else limit)
            if bars:
                missing = f"{asset} 实时 K 线不可用，已回退库内历史（{len(bars)} 根，可能滞后）"
            else:
                missing = f"{asset} 实时与库内 K 线均不可用"
    elif asset in _YAHOO_HISTORY_ASSETS:
        # 海力士 cap（v??）：股票类（Yahoo Finance 兜底）—— K 线走 quote_sources.fetch_yahoo_candles
        #   Yahoo chart v8 国内直连可用；分时 1m/5m/15m/60m 全部支持（用户选 10m 时做 5m→10m 聚合）。
        bars = _fetch_yahoo_history_bars(asset, interval, total_needed if use_paged else limit)
        if interval == "10m" and bars:
            bars = _aggregate_bars(bars, 600)
        if not bars:
            missing = f"{asset} Yahoo Finance 实时 K 线不可用"
    else:
        missing = f"{asset} 无免费历史 K 线源"

    if not use_paged:
        bars = bars[-max(1, int(limit)):]
    # days_back 模式不截断（paged_history 已按 total_needed 截断）

    points = [
        {"time": int(b["time"]), "close": b["close"]}
        for b in bars if b.get("close") is not None
    ]

    # Q5: 扫最近 K 线的 5 种异常（MACD金叉/零轴/RSI钝化/量能突破/吞没形态）
    #  给前端 chart markers 用 —— 柱子底下（belowBar）显示
    anomalies: List[Dict[str, Any]] = []
    try:
        # bars 里 time 是秒 → 包成 _candle_parts 期望的 [ts_ms, o, h, l, c, v] 形态
        raw = [
            [int(b["time"]) * 1000, b.get("open"), b.get("high"),
             b.get("low"), b.get("close"), b.get("volume")]
            for b in bars
            if b.get("close") is not None
        ]
        interval_sec_map = {"1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900,
                            "1h": 3600, "4h": 14400, "1d": 86400}
        isec = interval_sec_map.get(interval, 180)
        # v221.7: 30 天场景放开 limit + scan_window 让所有 markers 都出来
        if use_paged:
            scan_limit = min(500, max(50, len(bars) // 8))
            scan_window = len(bars)
        else:
            # v221.10: 4 类行情 marker（volume_spike/big_bullish/big_bearish/reversal_p）
            #   是用户必看的 4 类行情，独立维度追加在末尾。
            #   提升 scan_limit 到 30，让单 indicator + 4 类 marker 都能完整显示（单 12 + 4 类 12 = 24）
            scan_limit = 30
            scan_window = 60
        anomalies = abnormal_detector.scan_anomalies(
            raw, limit=scan_limit, scan_window=scan_window, interval_sec=isec)
    except Exception:
        anomalies = []

    result = {
        "asset": asset,
        "interval": interval,
        "count": len(points),
        "points": points,
        "bars": bars,
        "anomalies": anomalies,
        "missing": missing,
    }
    # v221.7: 写外层缓存 — 下次同 key 直接返回（避免 30d 扫描 10s 重复打）
    _HISTORY_SYNC_CACHE[outer_key] = {"ts": outer_now, "data": result}
    return result


# 2026-11-17: 本条已迁到 routers/timeseries.py。


# 2026-11-17: 本条已迁到 routers/timeseries.py。


# 2026-11-17: 本条已迁到 routers/timeseries.py。


# 2026-11-17: 本条已迁到 routers/timeseries.py。


# 2026-11-17: 本条已迁到 routers/timeseries.py。


# 深潮相关性关键词白名单：只保留加密/行情相关新闻，
# 过滤纯科技/AI/芯片/公司类内容（降采集量，避免 AI 分析积压）。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/calendar.py。


# ── v275: 归因页「蓝色事件 K 线影响验证」专用端点 ──────────────────────────
# 用途: 用户 2026-10-06 反馈:「归因页深化能力 — 看蓝色事件对市场是否有影响,
#       根据 k 线判断正确与否和影响程度级别」。
# 做法: 复用 events-for-asset 拉日历，再为每个 ★3+ 事件拉 5m K 线，扫描事件后
#       window_min 分钟内的 |max_high - pre_close| / |pre_close - min_low|，
#       给出 verdict: follow(跟随) / reverse(反向) / neutral(中性) / no_reaction(无反应)
# 缓存: 5 分钟 (避免每次图表刷新重算)。key = asset:hours:star_min:window_min
# 2026-11-17: 本条已迁到 routers/calendar.py。

# 2026-11-17: 本条已迁到 routers/calendar.py。


# 2026-11-17: 本条已迁到 routers/calendar.py。


# 2026-11-17: 本条已迁到 routers/calendar.py。


# 2026-11-17: 本条已迁到 routers/calendar.py。


# ── 晴红·标卡（QingHong）第三方聚合资讯——试用评估源（到点自动关）──────────
def _fetch_qinghong_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _QINGHONG_CACHE["value"] is not None and now < _QINGHONG_CACHE["expires"]:
        return _QINGHONG_CACHE["value"]
    try:
        payload = qinghong_feed.fetch_qinghong_flash()
        value = {
            "status": payload.get("status", "unavailable"),
            "items": payload.get("items", []),
            "error": payload.get("error", ""),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"QingHong unavailable: {type(exc).__name__}"}
    # 信息源面板：晴红聚合源在线/离线/到期自动关闭实时标色
    source_status.report(
        "qinghong_flash",
        value.get("status") == "ok",
        error=value.get("error", "") or "",
        detail=f"items={len(value.get('items') or [])}",
    )
    _QINGHONG_CACHE.update({"value": value, "expires": now + config.QINGHONG_POLL_SECONDS})
    return value


# 2026-11-17: 本条已迁到 routers/news.py 或 routers/indicators.py。


async def qinghong_news_watcher() -> None:
    """晴红·标卡试用聚合源：到点自动关闭 + 质量/时效评估。

    每 QINGHONG_POLL_SECONDS 拉一次（默认 45s），入库走标准新鲜度闸/配额。
    超过 QINGHONG_EXPIRES_AT 后自动停止采集，并在信息源面板标记 offline。
    """
    if not config.QINGHONG_NEWS_ENABLED or not config.QINGHONG_API_KEY:
        return
    await asyncio.sleep(15)  # 错峰：晚于其它新闻源启动
    while True:
        try:
            if qinghong_feed.is_expired():
                await source_status.areport(
                    "qinghong_flash", False,
                    error=f"试用 key 已于 {config.QINGHONG_EXPIRES_AT} 到期，自动关闭",
                )
                print(f"[QINGHONG] 到点自动关闭：{config.QINGHONG_EXPIRES_AT}，停止采集", flush=True)
                break
            settings = await asyncio.to_thread(news_sources.get_settings)
            if bool(settings.get("enabled", True)) and int(settings.get("remaining") or 0) > 0:
                payload = await asyncio.to_thread(_fetch_qinghong_sync)
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    items = payload.get("items") or []
                    if items:
                        inserted = await asyncio.to_thread(_ingest_external_news_sync, items)
                        if inserted:
                            print(f"[QINGHONG] 入库 {inserted} 条（{len(items)} 条候选）", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[QINGHONG] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(config.QINGHONG_POLL_SECONDS)


async def _news_report() -> Dict[str, Any]:
    """汇总外部新闻源、入库状态、AI 信号和模拟交易结果。

    v421（2026-11-17）：路由整改把这几个取数函数搬进了 `routers/news.py`，
    `_news_report` 还按老名字直接调用 → NameError → `/api/news/report` 永远 500
    （实测 2026-11-17 12:5x，返回 "Internal Server Error"）。
    这里改成函数内 import：routers.news 又通过 `_api()` 反向依赖本模块，
    放模块顶层会循环导入。
    """
    from routers.news import (
        get_techflow_news,
        get_eastmoney_news,
        get_blockbeats_news,
    )

    techflow, eastmoney, blockbeats, events, quick_evaluation = await asyncio.gather(
        get_techflow_news(),
        get_eastmoney_news(),
        get_blockbeats_news(),
        _fetch_event_rows(limit=200),
        asyncio.to_thread(quick_sim.snapshot),
    )
    report_items = []
    for event in events:
        report_items.append({
            "news_id": event.get("news_id"),
            "source": event.get("source", ""),
            "timestamp": event.get("timestamp", ""),
            "content": event.get("news_text", ""),
            "status": event.get("analysis_status", "PENDING"),
            "decision_id": event.get("decision_id"),
            "paper_trading_run_id": event.get("paper_trading_run_id"),
            "strategy_id": event.get("strategy_id"),
            "strategy_version_id": event.get("strategy_version_id"),
            "agent_model_id": event.get("agent_model_id"),
            "asset": event.get("target_asset", "NONE"),
            "action": event.get("action", "HOLD"),
            "score": event.get("score"),
            "evidence_confidence": event.get("evidence_confidence"),
            "evidence_action": event.get("evidence_action", "HOLD"),
            "trade_gate_reason": event.get("trade_gate_reason", ""),
            "entry_price": event.get("entry_price"),
            "settled": bool(event.get("settled")),
            "is_correct": event.get("is_correct", ""),
            "forward_pnl": event.get("forward_pnl"),
            "reason": event.get("reason", ""),
        })
    directional = [item for item in report_items if item["action"] in ("BUY", "SELL")]
    evidence_passed = [
        item for item in directional
        if item["evidence_action"] == item["action"]
        and item["trade_gate_reason"] == "证据充分，允许输出方向性结论"
    ]
    completed_directional = [
        item for item in directional if item["status"] not in ("PENDING", "PROCESSING")
    ]
    gate_reasons: Dict[str, int] = {}
    for item in completed_directional:
        if item in evidence_passed:
            continue
        reason_key = item["trade_gate_reason"] or "尚未写入闸门结论"
        gate_reasons[reason_key] = gate_reasons.get(reason_key, 0) + 1
    return {
        "generated_at_ms": int(time.time() * 1000),
        "sources": {
            "techflow": techflow,
            "eastmoney": eastmoney,
            "blockbeats": blockbeats,
        },
        "pipeline": {
            "source_items": sum(
                len(payload.get("items", []))
                for payload in (techflow, eastmoney, blockbeats)
                if isinstance(payload, dict)
            ),
            "stored_reports": len(report_items),
            "analyzed": sum(item["decision_id"] is not None for item in report_items),
            "signals": len(directional),
            "evidence_passed": len(evidence_passed),
            "evidence_rejected": len(completed_directional) - len(evidence_passed),
            "executed": sum(item["entry_price"] is not None for item in report_items),
            "open_trades": sum(item["entry_price"] is not None and not item["settled"] for item in report_items),
            "settled_trades": sum(item["settled"] for item in report_items),
        },
        "gate_reasons": gate_reasons,
        "quick_sim": quick_evaluation,
        "reports": report_items,
    }


# 2026-11-17: 本条已迁到 routers/news.py 或 routers/indicators.py。


# v176：dashboard 一键全部已读（UNREAD → DONE）—— 前端"全部已读"按钮调用
# 2026-11-17: 本条已迁到 routers/news.py。


# v176：ai_decisions UNREAD 自动翻牌（用户访问 dashboard 或某条决策详情时把 UNREAD → REVIEWED）
# 2026-11-17: 本条已迁到 routers/decisions.py。


# 2026-11-17: 本条已迁到 routers/decisions.py。


def _ingest_external_news_sync(items: List[Dict[str, Any]], *, bypass_gate: bool = False) -> int:
    """外部新闻源入库。

    bypass_gate=True 时跳过每日配额与噪声过滤（用于 FRED 官方数据发布/趋势事件）——
    宏观数据发布是高优先级事件，不受普通新闻配额和总开关影响，保证必达。

    新鲜度闸（NEWS_INGEST_MAX_AGE_HOURS，默认 3h）：条目带平台发布时间且距今超过
    阈值 → 判定"过时新闻"直接拒收（不入库/不进信号台/不占 AI 配额），防平台补发
    历史旧闻或 feed 缓存滞后。仅对带 published_at 的条目生效；实时推送类
    （WS/webhook 无平台时间字段）按"到达即发布"放行。bypass_gate 的高优先级
    事件（FRED/金十日历公布）同样过闸——它们若携带超旧时间说明源端异常。
    """
    inserted = 0
    rejected_stale = 0
    max_age_hours = float(getattr(config, "NEWS_INGEST_MAX_AGE_HOURS", 3) or 3)
    conn = db.get_connection()
    try:
        for item in items:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if not title:
                continue
            source = str(item.get("source") or "External")
            # ── 新鲜度闸：平台发布时间距今 > 阈值 → 过时新闻拒收 ──
            # published_at 先统一换算为北京时间串（带时区换算 / 无时区按北京），
            # 保证闸门比较与后续交易年龄 / 导出 / 展示全部同一时区。
            # 豁免：fresh_event=True 的"公布检测"事件（金十日历/FRED）——
            # 它们 actual/观测刚出现即生成（检测到公布=新鲜），其 pub_time/观测日
            # 是"计划/数据所属时间"可能早于检测时刻，不宜作新鲜度依据。
            pub_raw = str(item.get("published_at") or "").strip()
            pub_bj = config.fmt_bj(pub_raw) if pub_raw else ""
            is_fresh_event = bool(item.get("fresh_event") or item.get("bypass_freshness"))
            if pub_bj and not is_fresh_event:
                age_h = _age_hours_of(pub_bj)
                if age_h is not None and age_h > max_age_hours:
                    rejected_stale += 1
                    print(
                        f"[NEWS-INGEST] 拒收过时新闻: source={source} 发布时间={pub_bj}"
                        f"（距今 {age_h:.1f}h > {max_age_hours:.0f}h）: {title[:60]}",
                        flush=True,
                    )
                    continue
            external_key = f"{source}:{item.get('id') or title}"
            marker = hashlib.sha256(external_key.encode("utf-8")).hexdigest()[:24]
            if conn.execute("SELECT 1 FROM raw_news WHERE content LIKE ? LIMIT 1", (f"[hash:{marker}]%",)).fetchone():
                continue
            content = f"[hash:{marker}] {title}"
            if summary and summary not in title:
                content += f"\n{summary}"
            if bypass_gate:
                # FRED 官方事件：绕过配额/总开关/噪声过滤，直接高相关入库
                filtered = {"is_noise": 0, "relevance_score": 0.90}
            else:
                if not news_sources.allow_ingest(source, conn):
                    continue
                filtered = evaluate_news(title, summary, published_at=pub_bj)
                if filtered["is_noise"]:
                    # 噪音新闻不写库（status 有 CHECK 约束，且噪音本就不该进 raw_news，
                    # 避免前端出现永远不会被分析的"待AI分析"假积压）
                    continue
            ts_epoch = int(time.time())
            # v211 P1-A：把外部新闻源（finhub/yahoo_stock）的 target_asset_hint 注入 gate，
            # 让 gate_main_asset = 「个股」/「韩股」/「台股」类标签传递到 AI worker ，
            # 后续 LLM 拿到正确主品种提示（避免海力士/三星新闻被 LLM 误归 NONE）。
            gate_payload = dict(filtered.get("gate") or {})
            _tah = str(item.get("target_asset_hint") or "").strip()
            if _tah:
                # 把 ticker 提示写入 gate_assets 与 gate_main_asset（与 gate 四步语义兼容）
                _ga = list(gate_payload.get("assets") or [])
                if _tah not in _ga:
                    _ga.append(_tah)
                gate_payload["assets"] = _ga
                # main_asset 保留 gate 原判断（rule-based）；只在空时用 hint 兜底
                if not str(gate_payload.get("main_asset") or "").strip():
                    gate_payload["main_asset"] = "个股"
            try:
                news_id = db.insert_raw_news(
                    conn,
                    source=source,
                    content=content[:1000],
                    timestamp=_now_iso(),
                    status="PENDING",
                    is_noise=0,
                    relevance_score=float(filtered["relevance_score"]),
                    ts=ts_epoch,
                    published_at=pub_bj[:64],
                    gate=gate_payload,
                    gate_unavailable=int(filtered.get("gate_unavailable") or 0),
                )
                try:
                    # v189-8: 包 retry_on_busy,timeseries 写失败时 5s+退避重试,
                    # 避免 ingest 多条新闻时被 engine 抢锁导致 timeseries 漏写.
                    db.retry_on_busy(lambda: timeseries.record_news_event(
                        news_id,
                        source=source,
                        is_noise=0,
                        status="PENDING",
                        ts=ts_epoch,
                        # v310: 传原文，news_events 才有 headline/body（否则恒为空）
                        content=content[:1000],
                        connection=conn,
                    ))
                except sqlite3.OperationalError as exc:
                    # v189-7: 单条新闻 commit 后另一条 timeseries 失败不应让 raw_news 回滚
                    # (raw_news 已独立 commit). 只记日志继续下一条.
                    print(f"[NEWS-SOURCES] timeseries skip (raw_news 已入库 news_id={news_id}): {exc}")
                # v189-7: 单条 commit,避免 ingest N=100 条时整批单事务持锁几百 ms.
                # 单条 5-10ms 持锁窗口 vs 整批 ~500ms+,其他 writer 等不到锁会失败.
                conn.commit()
                inserted += 1
            except sqlite3.OperationalError as exc:
                # v189-7: 单条新闻 ingest 失败 (lock / 唯一约束冲突等) 不影响其他条目.
                print(f"[NEWS-INGEST] skip (lock/conflict): {type(exc).__name__}: {str(exc)[:80]}")
                continue
        if rejected_stale:
            print(
                f"[NEWS-INGEST] 本轮拒收过时新闻 {rejected_stale} 条（> {max_age_hours:.0f}h），"
                f"入库 {inserted} 条",
                flush=True,
            )
    finally:
        conn.close()
    return inserted


async def news_source_watcher() -> None:
    await asyncio.sleep(8)  # 错峰：启动后先让 API 就绪，再开始外部源抓取
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            remaining = int(settings.get("remaining") or 0)
            enabled = bool(settings.get("enabled", True))
            jobs = []
            if enabled and remaining > 0:
                # 单源开关只影响模拟盘交易层（news_sources.source_enabled），
                # 采集推送不受单源开关控制：全部源照常抓取入库。
                # 金十不走这里：独立 jin10_mcp_watcher 高频采集（专属节奏），避免双拉。
                jobs.append(asyncio.to_thread(_fetch_techflow_sync))
                jobs.append(asyncio.to_thread(_fetch_eastmoney_news_sync))
                jobs.append(asyncio.to_thread(_fetch_blockbeats_sync))
            payloads = await asyncio.gather(*jobs, return_exceptions=True) if jobs else []
            items: List[Dict[str, Any]] = []
            for payload in payloads:
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    items.extend(payload.get("items") or [])
            if remaining > 0 and items:
                await asyncio.to_thread(_ingest_external_news_sync, items[:remaining])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[NEWS-SOURCES] {type(exc).__name__}: {exc}")
        await asyncio.sleep(config.NEWS_SOURCE_POLL_SECONDS)


# === 引擎 / 服务自身健康心跳 ============================================
# 探测项:
#   - engine_webhook (9000): engine.py 主循环是否存活（任何模型推理/落库的源头）
#   - api_server: 本进程自我健康（uvicorn 进程在 /health 端点响应说明事件循环未卡死）
#   - trident_event_bus_db: SQLite 可写性（写一行立即回滚；损坏=全局失忆）
# 探测间隔 30s；纯网络/磁盘探测，无 LLM 调用，绝不影响模型推理。
# 2026-11-18: infra_health_watcher 已迁到 api_maintenance.py。



async def jin10_mcp_watcher() -> None:
    """金十 MCP 快讯采集 — 独立高频 watcher（专属节奏 JIN10_MCP_POLL_SECONDS）。

    与 news_source_watcher 分离：金十快讯是秒级更新，全局 15s 轮询太慢。
    走 _fetch_jin10_sync（带缓存节流，API 端点与采集共用同一份缓存，不会双拉）。
    受新闻总开关与每日配额约束（allow_ingest）；无 token 时静默退出。
    """
    if not config.JIN10_MCP_TOKEN:
        return
    await asyncio.sleep(6)  # 错峰：启动后先让 API 就绪
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            enabled = bool(settings.get("enabled", True))
            # 金十快讯是核心实时驱动源，豁免每日 300 条配额（见
            # news_sources._QUOTA_EXEMPT_SOURCES），只要总开关开着就持续采集推送，
            # 不再受 remaining 拦截；入库层 allow_ingest 对金十同步豁免。
            if enabled:
                payload = await asyncio.to_thread(_fetch_jin10_sync)
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    items = payload.get("items") or []
                    if items:
                        await asyncio.to_thread(_ingest_external_news_sync, items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[JIN10-MCP] {type(exc).__name__}: {exc}")
        await asyncio.sleep(config.JIN10_MCP_POLL_SECONDS)


async def stock_news_watcher() -> None:
    """美股新闻（Yahoo 个股新闻）：每 STOCK_NEWS_POLL_SECONDS 轮询映射股票的新新闻，
    翻译后入库（source='yahoo_stock'）→ AI 自动分析（带 [美股行情] 上下文）→ 美股模拟交易。
    受新闻总开关与每日配额约束；yfinance 拉不到/翻译失败均静默降级。
    """
    if not config.STOCK_NEWS_ENABLED:
        return
    await asyncio.sleep(18)  # 错峰：晚于其它新闻源启动
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            if bool(settings.get("enabled", True)) and int(settings.get("remaining") or 0) > 0:
                raw_items = await asyncio.to_thread(stock_news.poll_stock_news)
                if raw_items:
                    ingest_items = await asyncio.to_thread(stock_news.to_ingest_items, raw_items)
                    if ingest_items:
                        inserted = await asyncio.to_thread(_ingest_external_news_sync, ingest_items)
                        if inserted:
                            print(f"[STOCK-NEWS] 入库 {inserted} 条美股新闻（{len(raw_items)} 条候选）", flush=True)
                    stock_news.mark_polled(raw_items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[STOCK-NEWS] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(config.STOCK_NEWS_POLL_SECONDS)


async def reuters_news_watcher() -> None:
    """Reuters 路透社（Google News RSS site:reuters.com）→ 翻译 → 入库。

    免费通道无 key；英文标题经 webhook 翻译兜底链转中文后入库（source='reuters'）
    → AI 自动分析。受新闻总开关与每日配额约束；Google News 不可达时静默降级。
    间隔 REUTERS_NEWS_POLL_SECONDS（默认 60s；聚合源延迟分钟级，无需高频）。
    """
    if not config.REUTERS_NEWS_ENABLED:
        return
    await asyncio.sleep(20)  # 错峰：晚于其它新闻源启动
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            if bool(settings.get("enabled", True)) and int(settings.get("remaining") or 0) > 0:
                raw_items = await asyncio.to_thread(reuters_news.poll_reuters_news)
                if raw_items:
                    ingest_items = await asyncio.to_thread(reuters_news.to_ingest_items, raw_items)
                    if ingest_items:
                        inserted = await asyncio.to_thread(_ingest_external_news_sync, ingest_items)
                        if inserted:
                            print(f"[REUTERS] 入库 {inserted} 条路透快讯（{len(raw_items)} 条候选）", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[REUTERS] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(config.REUTERS_NEWS_POLL_SECONDS)


async def rss_news_watcher() -> None:
    """多源 RSS 新闻（Yahoo Finance 官方 + Google News 聚合 FXStreet / MarketWatch）→ 翻译 → 入库。

    英文标题经 webhook 翻译兜底链转中文后入库（source=yahoo_finance / fxstreet / marketwatch）
    → AI 自动分析。受新闻总开关与每日配额约束；任一源不可达时静默降级（不影响其它源）。
    间隔 RSS_NEWS_POLL_SECONDS（默认 600s；RSS 分钟级更新，聚合源另有 ~2h 收录滞后，无需高频）。
    """
    if not config.RSS_NEWS_ENABLED:
        return
    await asyncio.sleep(24)  # 错峰：晚于其它新闻源启动
    while True:
        try:
            settings = await asyncio.to_thread(news_sources.get_settings)
            if bool(settings.get("enabled", True)) and int(settings.get("remaining") or 0) > 0:
                raw_items = await asyncio.to_thread(rss_sources.poll_rss_news)
                if raw_items:
                    ingest_items = await asyncio.to_thread(rss_sources.to_ingest_items, raw_items)
                    if ingest_items:
                        inserted = await asyncio.to_thread(_ingest_external_news_sync, ingest_items)
                        if inserted:
                            print(f"[RSS] 入库 {inserted} 条多源新闻（{len(raw_items)} 条候选）", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[RSS] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(config.RSS_NEWS_POLL_SECONDS)


async def jin10_calendar_watcher() -> None:
    """金十财经日历发布检测（计划时刻感知调度，非固定 30s 全天轮询）。

    日历条目一旦"公布"（actual 出现实际值）且星级 ≥ JIN10_CALENDAR_MIN_STAR，
    生成事件进新闻流（source='jin10_calendar'）→ 实时信号台 SSE 自动显示
    → AI worker 自动分析 → 模拟盘交易。去重靠 [hash:] 前缀（同条目只触发一次），
    与 FRED 发布事件同一模式。无 token 时静默退出。

    调度策略（省 MCP 配额——曾 1500 次/日耗尽）：
      · 先解析未来 12h 内将公布的高星级条目计划时间（next_upcoming_release）
      · 有即将公布 → 密集轮询（JIN10_CALENDAR_POLL_SECONDS，默认 30s）
      · 无即将公布（本时段无高影响数据）→ 低频保活（5 分钟），不空转烧配额
      · 公布时刻临近（≤2min）自动保持密集，actual 一出现即推

    bypass_gate=True：宏观数据公布是高优先级事件，跳过每日配额与噪声过滤，
    保证必达（与 FRED 官方发布事件同级，不受 300 条/日配额拦截）。
    """
    if not config.JIN10_MCP_TOKEN:
        return
    await asyncio.sleep(12)  # 错峰：晚于快讯 watcher 启动
    while True:
        try:
            # 最新公布窗口 = max(模拟盘新闻时效, 60 分钟)/60 小时：
            # 复用后台可调参数（900s=15min → 窗口 60min 保底，防数据延迟公布被误杀）；
            # 参数调 ≥60min 时窗口随参数走。
            try:
                from paper_trading import get_settings as _get_pt
                # v2026.10.23: get_settings() 自己开连接查 SQLite，是同步的。
                # 看门狗实测它在事件循环线程上卡 ≈2s（写锁争用时 db.get_connection 会等）。
                _secs = int((await asyncio.to_thread(_get_pt)).get(
                    "news_trade_max_age_seconds") or 0)
            except Exception:
                _secs = 0
            # v423 修正：日历**入库**窗口 1h → 24h。
            # 事故（2026-11-17）：夜间公布的经济数据（加拿大10月CPI、美国11月纽约联储
            # 制造业指数等，距今 14–23h）全被"公布超过 1h 就跳过"拦掉，当日
            # jin10_calendar 入库 0 条，而昨日同期 30 条。数据本来就有，是窗口只覆盖
            # "刚刚公布"的那一小时。
            # 注意：入库窗口 ≠ 交易窗口。事件带 published_at 入库，交易侧仍按
            # news_trade_max_age_seconds 判新鲜度，旧数据不会污染交易。
            window_h = max(_secs, 24 * 3600) / 3600.0
            events = await asyncio.to_thread(
                jin10_mcp.calendar_events, config.JIN10_CALENDAR_MIN_STAR, float(window_h),
            )
            if events:
                await asyncio.to_thread(_ingest_external_news_sync, events, bypass_gate=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[JIN10-CAL] {type(exc).__name__}: {exc}")

        # ── 掐点调度：未来有高星级将公布 → 密集；否则低频保活 ──
        try:
            upcoming = await asyncio.to_thread(
                jin10_mcp.next_upcoming_release, config.JIN10_CALENDAR_MIN_STAR, 12.0
            )
            if upcoming is not None:
                # 有即将公布：公布前 ≤2min 用最短密集间隔，否则用常规日历轮询间隔
                gap_s = upcoming - time.time()
                delay = min(config.JIN10_CALENDAR_POLL_SECONDS, max(10, int(gap_s)))
                await asyncio.sleep(delay)
            else:
                # 本时段无临近高影响数据 → 低频保活（5min），不空转烧 MCP 配额
                await asyncio.sleep(300)
        except Exception:
            await asyncio.sleep(config.JIN10_CALENDAR_POLL_SECONDS)


# ── 新闻数据整理：按平台分文档自动导出（对比平台时效用）──────────────
# 进程内水位：只在上次导出后有新新闻入库时才再生成一批（避免空批次刷屏）。
_NEWS_EXPORT_STATE: Dict[str, Any] = {"last_news_id": 0, "lock": threading.Lock()}


def _news_export_run_if_new() -> Dict[str, Any]:
    """有新新闻则生成一批按平台分文件的导出文档；无新新闻返回 skipped。"""
    conn = db.get_connection()
    try:
        max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM raw_news").fetchone()[0])
    finally:
        conn.close()
    with _NEWS_EXPORT_STATE["lock"]:
        if max_id <= _NEWS_EXPORT_STATE["last_news_id"] and _NEWS_EXPORT_STATE["last_news_id"] > 0:
            return {"skipped": True, "reason": "no new news since last export"}
        _NEWS_EXPORT_STATE["last_news_id"] = max_id
    return news_organizer.export_news_batch()


async def news_export_watcher() -> None:
    """新闻数据整理自动导出 — 按可动态配置的间隔（页面输入框改后立即生效）。

    每批生成 backend/news_exports/batch_<时间戳>/，内部按平台分 CSV 文件
    （含平台发布时间 vs 我们采集时间，供去各新闻平台对比时效）。
    只导出窗口内的新闻；清理超保留数的老批次。
    """
    await asyncio.sleep(18)  # 错峰：晚于新闻源 watcher，首轮即有数据
    while True:
        try:
            settings = await asyncio.to_thread(news_organizer.get_export_settings)
            result = await asyncio.to_thread(_news_export_run_if_new)
            if result.get("skipped"):
                print("[NEWS-EXPORT] 无新新闻，跳过本批", flush=True)
            else:
                print(
                    f"[NEWS-EXPORT] 已整理 {result.get('total_news', 0)} 条"
                    f" → {result.get('batch', '')}（{len(result.get('files', []))} 个平台文件）",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[NEWS-EXPORT] {type(exc).__name__}: {exc}", flush=True)
        try:
            settings = await asyncio.to_thread(news_organizer.get_export_settings)
            interval = int(settings.get("interval_minutes") or 30)
        except Exception:
            interval = 30
        await asyncio.sleep(max(1, interval) * 60)


# FRED 水位记忆：只推"观测日期变化"的真发布；重启/首启只建基线不推存量。
# 键 = 事件 id（fred-{series}-{date} / fred-trend-{series}-{date}），
# 值 = id 尾部日期（YYYY-MM-DD）——同一 series 观测日变化才算新发布。
_FRED_SEEN: Dict[str, str] = {}
_FRED_SEEN_LOCK = threading.Lock()


def _fred_date_key(eid: str) -> str:
    """从事件 id 提取观测日期：'fred-cpi-2026-08-01' → '2026-08-01'。"""
    import re as _re
    m = _re.search(r"(\d{4}-\d{2}-\d{2})$", eid or "")
    return m.group(1) if m else (eid or "")


def _fred_series_key(eid: str) -> str:
    """事件 id → series 水位键：去掉尾部观测日期，保留前缀。
    'fred-CPIAUCSL-2026-08-01' → 'fred-CPIAUCSL'；'fred-trend-T10Y2Y-2026-07-05' → 'fred-trend-T10Y2Y'。"""
    import re as _re
    return _re.sub(r"-?\d{4}-\d{2}-\d{2}$", "", eid or "") or (eid or "")


def _fred_new_only(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤：同 series 的观测日期比上次见过的更新 → 新发布才推。

    首轮调用（某 series 首次见到）只建基线、不推送 —— FRED 的意义是
    "检测到新数据发布"，不是"补发当前最新值"（月度指标观测日滞后一个月，
    首启全推会把旧观测当新事件刷屏）。
    """
    out: List[Dict[str, Any]] = []
    with _FRED_SEEN_LOCK:
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid:
                out.append(ev)
                continue
            series_key = _fred_series_key(eid)
            date_key = _fred_date_key(eid)
            prev = _FRED_SEEN.get(series_key)
            if prev is None:
                _FRED_SEEN[series_key] = date_key  # 首次见到：建基线
                print(f"[FRED] 基线 {series_key[:40]} @ {date_key}（不推存量，等下次真发布）", flush=True)
                continue
            if date_key != prev:
                _FRED_SEEN[series_key] = date_key  # 观测日期变化 = 真发布
                out.append(ev)
    return out


async def market_sentiment_watcher() -> None:
    """v170+ 数据源加固：市场情绪聚合（Fear & Greed Index + Truth Social Trump）。

    两条源每日合计 ~10-25 条结构化信号：FNG 当日一次 + Truth Social 当日新推 0-20 条。
    间隔 MARKET_SENTIMENT_POLL_SECONDS（默认 3600s=1h，Truth Social 反爬轻、FNG 每日一更，
    1h 是节奏与新闻价值的最佳折中）。
    入库走 _ingest_external_news_sync(bypass_gate=True)：FNG/Trump 都是高相关性结构化信号，
    不走新闻总开关/每日配额（与 FRED 共享 bypass_gate=True 的策略）。
    """
    await asyncio.sleep(20)  # 错峰：晚于其它新闻源启动
    while True:
        try:
            items: List[Dict[str, Any]] = []

            # (a) Fear & Greed Index —— 加密恐慌贪婪
            try:
                fng_items = await asyncio.to_thread(market_sentiment.fng.fetch_fng, 5)
                if fng_items:
                    items.extend(market_sentiment.fng.to_ingest_items(fng_items))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[MARKET-SENTIMENT] FNG {type(exc).__name__}: {str(exc)[:80]}", flush=True)

            # (b) Truth Social Trump —— 政策预期驱动信号
            try:
                posts = await asyncio.to_thread(
                    market_sentiment.trump_posts.fetch_with_retry, 10,
                )
                if posts:
                    items.extend(market_sentiment.trump_posts.to_ingest_items(posts))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[MARKET-SENTIMENT] Trump {type(exc).__name__}: {str(exc)[:80]}", flush=True)

            if items:
                inserted = await asyncio.to_thread(
                    _ingest_external_news_sync, items, bypass_gate=True,
                )
                if inserted:
                    print(
                        f"[MARKET-SENTIMENT] 入库 {inserted} 条情绪信号"
                        f"（FNG + Trump 候选 {len(items)}）",
                        flush=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[MARKET-SENTIMENT] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(config.MARKET_SENTIMENT_POLL_SECONDS)


async def extra_sources_watcher() -> None:
    """三源增补：CBOE（VIX/SKEW 尾部风险定价）、DefiLlama（稳定币供给）、Polymarket（事件概率）。

    日度级别更新，一小时一轮即可；本机对这三家都会间歇性 SSL 断连，采集器内部已带退避重试。
    取不到就写 unavailable（带错误原因），不写 0、不沿用旧值。
    """
    import extra_sources

    await asyncio.sleep(45)  # 错峰启动，避开行情首轮
    while True:
        try:
            res = await asyncio.to_thread(extra_sources.refresh, None, True)
            if res.get("written"):
                print(f"[EXTRA-SRC] 落库 {res['written']} 条（ok {res['ok']}/{res['total']}）", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[EXTRA-SRC] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(3600)


async def fred_watcher() -> None:
    """FRED 官方宏观数据发布检测（独立 10s 轮询，不受新闻源 15s 间隔限制）。

    - release：CPI/非农/利率/美债等即时影响行情的指标发布 → 事件进新闻流
    - trend：收益率/期限利差/M2 显著趋势变化 → 事件进新闻流
    - 开关逻辑与其它新闻源一致：受新闻总开关与每日配额约束
      （bypass_gate=False 走 allow_ingest）；单源开关（fred）只影响模拟盘交易层。
    - 水位记忆：只推观测日期变化（真发布）；重启/首启建基线不推存量旧观测。
    """
    await asyncio.sleep(15)  # 错峰：FRED 网络请求放最后，避免启动瞬间并发
    while True:
        try:
            releases, trends = await asyncio.to_thread(fred_feed.check_once)
            new_releases = _fred_new_only(releases)
            new_trends = _fred_new_only(trends)
            if new_releases:
                await asyncio.to_thread(_ingest_external_news_sync, new_releases)
            if new_trends:
                await asyncio.to_thread(_ingest_external_news_sync, new_trends)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[FRED-WATCHER] {type(exc).__name__}: {exc}")
        await asyncio.sleep(config.FRED_POLL_SECONDS)


# BLS 水位记忆：只推 (series, 年月) 变化（真发布）；重启/首启建基线不推存量旧观测。
# 键 = 事件 id（bls-{series}-{year}-{period}，如 bls-CUUR0000SA0-2026-M07）。
_BLS_SEEN: Dict[str, str] = {}
_BLS_SEEN_LOCK = threading.Lock()


def _bls_new_only(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤：同 series 的数据期比上次见过的更新 → 新发布才推。

    首轮调用（某 series 首次见到）只建基线、不推送 —— BLS 的意义是
    "检测到官方发布"，不是"补发当前最新值"（月度指标数据期滞后一期，
    首启全推会把旧数据当新事件刷屏）。
    """
    out: List[Dict[str, Any]] = []
    with _BLS_SEEN_LOCK:
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid:
                out.append(ev)
                continue
            # 事件 id 形如 bls-CUUR0000SA0-2026-M07 → 去掉尾部的 -2026-M07 留 series 前缀
            import re as _re
            m = _re.search(r"-(\d{4}-M\d{2})$", eid)
            period_key = m.group(1) if m else (eid or "")
            series_key = _re.sub(r"-?\d{4}-M\d{2}$", "", eid) or eid
            prev = _BLS_SEEN.get(series_key)
            if prev is None:
                _BLS_SEEN[series_key] = period_key  # 首次见到：建基线
                print(f"[BLS] 基线 {series_key[:40]} @ {period_key}（不推存量，等下次真发布）", flush=True)
                continue
            if period_key != prev:
                _BLS_SEEN[series_key] = period_key  # 数据期变化 = 真发布
                out.append(ev)
    return out


# ── BLS 发布窗口调度：掐官方发布时间（美东 8:30）密集轮询 ──────────────
# BLS 的 CPI/非农/失业率固定在美东时间 8:30 发布（工作日）。纯定时轮询要么
# 错过时效（间隔大）、要么平时空转烧配额。方案 A：工作日美东 8:30 前后进入
# "密集窗口"（90s 轮询），窗口外低频（30min）；周末不发布 → 跳过窗口。
# 美东 8:30 = 北京 20:30(夏令时) / 21:30(冬令时)，随美国 DST 切换自动换算。
import calendar as _calendar

# 美国夏令时：3 月第二个周日 ~ 11 月第一个周日（EDT=UTC-4），其余 EST=UTC-5。
def _us_eastern_offset(dt: datetime) -> int:
    """按给定 UTC 时刻判断美国东部当前偏移（-4 EDT / -5 EST）。"""
    year = dt.year
    # 3 月第二个周日
    march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
    days_to_second_sun = (7 - march_first.weekday()) % 7 + 7
    dst_start = march_first + timedelta(days=days_to_second_sun)
    # 11 月第一个周日
    nov_first = datetime(year, 11, 1, tzinfo=timezone.utc)
    days_to_first_sun = (7 - nov_first.weekday()) % 7
    dst_end = nov_first + timedelta(days=days_to_first_sun)
    return -4 if dst_start <= dt < dst_end else -5


def _is_us_workday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon-Fri


def _bls_window_active(dt: datetime) -> bool:
    """是否处于"发布外围窗口"：美东工作日 07:30 ~ 10:30（8:30 发布 ± 更大范围）。"""
    if not _is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return 7 * 60 + 30 <= minutes <= 10 * 60 + 30


def _bls_core_window_active(dt: datetime) -> bool:
    """是否处于"发布黄金窗口"：美东工作日 08:30 ~ 08:50（8:30 发布后 20 分钟）。

    BLS API 在 8:30 发布时几乎同步更新（1-2 分钟可见），5s 级密集只放在发布后
    黄金捕获窗，避免发布前空转烧配额。
    """
    if not _is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return 8 * 60 + 30 <= minutes <= 8 * 60 + 50


def _bls_mid_window_active(dt: datetime) -> bool:
    """是否处于"发布前后中频窗"：美东工作日 08:15~08:30 / 08:50~09:05（黄金窗外两侧）。"""
    if not _is_us_workday(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    return (8 * 60 + 15 <= minutes < 8 * 60 + 30) or (8 * 60 + 50 < minutes <= 9 * 60 + 5)


def _bls_sleep_seconds(now_utc: datetime) -> int:
    """按当前时间决定下一次轮询间隔（分级，配额 ~459 次/日 < 500）：
    黄金窗（8:30-8:50）5s；中频窗（发布前后）20s；外围窗口 90s；其余 30min。"""
    et = now_utc + timedelta(hours=_us_eastern_offset(now_utc))
    if _bls_core_window_active(et):
        return 5
    if _bls_mid_window_active(et):
        return 20
    if _bls_window_active(et):
        return 90
    return 1800


async def bls_watcher() -> None:
    """BLS 劳工统计局官方数据发布检测（source='bls'，发布窗口智能调度）。

    CPI/非农/失业率等官方一手数据：BLS 比 FRED 更接近发布时刻。
    - 水位记忆只推 (series, 年月) 变化；首启建基线不推存量。
    - 智能轮询（分级窗口）：美东 8:30 发布后黄金窗（08:30-08:50）每 5s 密集 →
      发布后最快 5s 内捕获；发布前后中频窗（08:15-09:05）每 20s；外围窗口
      07:30-10:30 每 90s；窗口外 30min 低频防漏；周末不发布 → 跳过窗口。
      配额核算：黄金窗 20min×5s≈240 次 + 中频 30min×20s≈90 次 + 外围 2h×90s
      ≈80 次 + 窗口外 ≈ 460 次/日 < v2 免费 500 次/日配额。
    - 未配置 BLS_API_KEY 时静默退出。
    """
    if not config.BLS_API_KEY:
        return
    await asyncio.sleep(18)  # 错峰：晚于 FRED/新闻源启动
    while True:
        try:
            events = await asyncio.to_thread(bls_feed.check_once)
            new_events = _bls_new_only(events)
            if new_events:
                # bypass_gate=True：BLS 官方发布是高优先级事件（同 FRED/金十日历），
                # 跳过每日配额与噪声过滤保证必达 → 立即进实时信号台触发 AI 分析。
                await asyncio.to_thread(_ingest_external_news_sync, new_events, bypass_gate=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[BLS-WATCHER] {type(exc).__name__}: {exc}")
        delay = _bls_sleep_seconds(datetime.now(timezone.utc))
        await asyncio.sleep(delay)


def _market_price_from_cache(asset: str) -> float | None:
    """只读多源行情缓存，避免在 quick_sim 写库时再次打开同一 SQLite。"""
    payload = _MARKET_CACHE.get("value") or {}
    for item in payload.get("items") or []:
        if str(item.get("asset") or "").upper() != (asset or "").upper():
            continue
        price = item.get("price")
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    return None


def _quick_sim_price(asset: str) -> float | None:
    """快速模拟与前端看板共用行情：优先多源缓存，再回退到引擎价格源。"""
    return _market_price_from_cache(asset) or _get_current_price(asset)


async def quick_sim_watcher() -> None:
    """快速模拟评测循环：新方向性信号按真实行情建仓，到期按真实行情结算。

    v170.4 P0+: 每次结算主动 SSE 广播 settlement 事件，让反思页立即重拉
    /api/review/snapshot（避免等轮询窗口）。
    """
    await asyncio.sleep(5)  # 错峰：避开启动瞬间全量任务并发
    while True:
        try:
            # 2026-11-17: 这里只是"顺手热一下缓存"，用 allow_stale 免得把循环卡 13.5s
            await asyncio.to_thread(_cached_market_prices, True)
            result = await asyncio.to_thread(quick_sim.run_cycle, _quick_sim_price)
            for trade in result["opened"]:
                print(f"[QUICK-SIM] OPEN #{trade['decision_id']} {trade['action']} {trade['asset']} @ {trade['entry_price']}")
            for trade in result["settled"]:
                print(f"[QUICK-SIM] SETTLE #{trade['id']} {trade['action']} {trade['asset']} {trade['pnl_pct']:+.4f}% -> {trade['verdict']}")
            # v170.4 P0+: 任何结算/建仓变化都广播，前端反思页立即响应
            if result.get("settled") or result.get("opened"):
                await _broadcast_sse({
                    "type": "quick_sim_update",
                    "settled_count": len(result.get("settled", [])),
                    "opened_count": len(result.get("opened", [])),
                    "settled": [
                        {
                            "id": t.get("id"),
                            "asset": t.get("asset"),
                            "action": t.get("action"),
                            "pnl_pct": t.get("pnl_pct"),
                            "verdict": t.get("verdict"),
                        }
                        for t in result.get("settled", [])
                    ],
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[QUICK-SIM] {type(exc).__name__}: {exc}")
        await asyncio.sleep(config.QUICK_SIM_POLL_SECONDS)


# 2026-11-17: 本条已迁到 routers/sources.py。


# 2026-11-17: 本条已迁到 routers/sources.py。


# ══════════════════════════════════════════════════════════════════
# v281: 全局联通中心 — 聚合展示 + 测试钩子
# ──────────────────────────────────────────────────────────────────


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/connectivity.py。


# 2026-11-17: 本条已迁到 routers/charts_export.py。


# 2026-11-17: 本条已迁到 routers/charts_export.py。


# 2026-11-17: 本条已迁到 routers/charts_export.py。


async def _do_export_excel():
    """Core Excel export logic, separated for clean error handling."""
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                ad.id,
                rn.timestamp,
                rn.source,
                rn.content AS news_text,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.reasoning_path,
                ad.reasoning,
                ad.extra_models_consensus,
                ad.target_asset,
                ad.vip_tag,
                qs.entry_price, qs.exit_price, qs.max_price, qs.min_price,
                qs.max_price_time, qs.min_price_time,
                datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
                qs.verdict AS is_correct,
                ad.cluster_size,
                ad.prediction_type,
                ad.event_phase,
                ad.market_confirmation,
                ad.expected_horizon,
                ad.invalidation_condition,
                ad.decision_context,
                qs.mfe_pct,
                qs.mae_pct,
                qs.pnl_pct AS forward_pnl,
                qs.mfe_time_mins
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id AND qs.settled = 1
            WHERE date(ad.created_at) >= date('now', 'localtime', '-5 days')
              AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
              AND UPPER(ad.target_asset) IN ('XAU', 'GOLD', 'BTC', 'WTI')
              AND qs.entry_price IS NOT NULL
            ORDER BY ad.id DESC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

    import xlsxwriter
    output = io.BytesIO()
    wb = xlsxwriter.Workbook(output)
    ws = wb.add_worksheet("Signals")
    headers = [
        "ID", "时间", "新闻内容", "品种", "方向", "评分", "入场价", "最高价", "最低价", "出场价",
        "最大浮盈%", "最大浮亏%", "到达极值(min)", "强影响", "胜负", "标签", "主模型归因", "extra_models_consensus",
    ]
    for c, h in enumerate(headers):
        ws.write(0, c, h)

    # Impact thresholds: MFE must exceed asset-specific percentage
    IMPACT_THRESHOLDS = {"BTC": 2.0, "XAU": 1.0, "GOLD": 1.0, "WTI": 1.5}

    for r, row in enumerate(rows, start=1):
        asset = (row["target_asset"] or "").upper()
        action_raw = (row["action"] or "").upper()
        entry = row["entry_price"]
        exit_p = row["exit_price"]
        max_p = row["max_price"]
        min_p = row["min_price"]
        max_ptime = row["max_price_time"] or 0
        min_ptime = row["min_price_time"] or 0
        entry_time_str = row["entry_time"] or ""
        raw_verdict = (row["is_correct"] or "").strip().upper()
        if raw_verdict == "WIN":
            verdict = "正确"
        elif raw_verdict == "LOSS":
            verdict = "错误"
        else:
            verdict = raw_verdict or "—"
        # ── Derived impact metrics (defensive against div-by-zero) ──
        mfe_str = "—"
        mae_str = "—"
        time_to_extreme = "—"
        high_impact = "否"

        if entry and entry > 0:
            try:
                if action_raw == "BUY":
                    if max_p is not None and max_p > 0:
                        mfe_val = (max_p - entry) / entry * 100
                        mfe_str = f"{mfe_val:+.2f}%"
                    if min_p is not None and min_p > 0:
                        mae_val = (min_p - entry) / entry * 100
                        mae_str = f"{mae_val:+.2f}%"
                    if max_ptime > 0:
                        try:
                            et_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                            et_unix = int(et_dt.timestamp())
                            minutes = round((max_ptime - et_unix) / 60, 1)
                            if minutes >= 0:
                                time_to_extreme = f"{minutes}"
                        except (ValueError, TypeError, OSError):
                            pass
                elif action_raw == "SELL":
                    if entry > 0:
                        if min_p is not None and min_p > 0:
                            mfe_val = (entry - min_p) / entry * 100
                            mfe_str = f"{mfe_val:+.2f}%"
                        if max_p is not None and max_p > 0:
                            mae_val = (entry - max_p) / entry * 100
                            mae_str = f"{mae_val:+.2f}%"
                    if min_ptime > 0:
                        try:
                            et_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                            et_unix = int(et_dt.timestamp())
                            minutes = round((min_ptime - et_unix) / 60, 1)
                            if minutes >= 0:
                                time_to_extreme = f"{minutes}"
                        except (ValueError, TypeError, OSError):
                            pass

                if mfe_str != "—":
                    mfe_number = float(mfe_str.replace("%", "").replace("+", ""))
                    threshold = IMPACT_THRESHOLDS.get(asset, 2.0)
                    if mfe_number > threshold:
                        high_impact = "是"
            except Exception:
                pass

        ws.write(r, 0, row["id"])
        ws.write(r, 1, _format_time(row["timestamp"]))
        ws.write(r, 2, re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', (row["news_text"] or "")[:200]))
        ws.write(r, 3, asset)
        action_display = "多" if action_raw == "BUY" else ("空" if action_raw == "SELL" else action_raw)
        ws.write(r, 4, action_display)
        ws.write(r, 5, round(row["score"], 2) if row["score"] else 0)
        ws.write(r, 6, entry)
        ws.write(r, 7, max_p)
        ws.write(r, 8, min_p)
        ws.write(r, 9, exit_p)
        ws.write(r, 10, mfe_str)
        ws.write(r, 11, mae_str)
        ws.write(r, 12, time_to_extreme)
        ws.write(r, 13, high_impact)
        ws.write(r, 14, verdict)
        ws.write(r, 15, (row["vip_tag"] or "").replace("[", "").replace("]", ""))
        # 主模型归因: 优先完整推导链，回退到短结论
        ws.write(r, 16, ((row["reasoning_path"] or row["reasoning"] or "")[:2000]).strip())
        # ── extra_models_consensus: 直接从 DB 列写入，已经是合法 JSON ──
        raw_consensus = _safe(row, "extra_models_consensus") or ""
        if isinstance(raw_consensus, str):
            raw_consensus = raw_consensus.strip()
        if raw_consensus:
            # 校验是否为合法 JSON，非法则写入原始字符串
            try:
                parsed = json.loads(raw_consensus) if isinstance(raw_consensus, str) else raw_consensus
                ws.write(r, 17, json.dumps(parsed, ensure_ascii=False))
            except (json.JSONDecodeError, TypeError, ValueError):
                ws.write(r, 17, str(raw_consensus)[:2000])
        else:
            ws.write(r, 17, "")
    wb.close()
    data = output.getvalue()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trident_signals.xlsx"},
    )


# ── P2 安全加固：per-IP rate limit (滑动窗口 in-memory) ────────────────────
# 设计要点:
#   1. 单进程 in-memory (dict[ip, deque[timestamps]]); 多实例/多 worker 需升级到 Redis.
#   2. X-Forwarded-For 仅在 TRUSTED_PROXIES 列表 (默认 ["127.0.0.1", "::1"]) 内才信任;
#      否则直接取 request.client.host, 防客户端伪造 XFF 绕过。
#   3. 鉴权用户 (X-Trident-API-Key 有效) 享有 RATE_LIMIT_AUTH_MULTIPLIER 倍配额,
#      防止 admin/CI 被误伤。
#   4. 关闭开关: RATE_LIMIT_ENABLED=0; 单项关闭: RATE_LIMIT_*_PER_MIN=0。
#   5. 超出限流返回 429 + Retry-After header + JSON 错误体 (前端可统一处理).
import threading
import time as _time
from collections import defaultdict, deque


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 单例限流器 (懒加载，避免 config 未就绪时崩溃)
# 2026-11-17: 本条已迁到 routers/agent_chat.py。

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# ---------------------------------------------------------------------------
# File Upload — Agent Chat attachments
# ---------------------------------------------------------------------------

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# ---------------------------------------------------------------------------
# Agent Chat (Data Copilot) — Text-to-SQL + DB query
# ---------------------------------------------------------------------------

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# ── Agent LLM config: Aiping OpenAI-compatible endpoint ──

# 2026-11-17: _agent_llm_client 已下沉到 common/llm.py，这里保留同名别名。
from common.llm import agent_llm_client as _agent_llm_client


# 2026-11-17: _agent_llm_model 已下沉到 common/llm.py，这里保留同名别名。
from common.llm import agent_llm_model as _agent_llm_model

# ── DB Schema description for the Agent LLM ──

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# P1 安全修复：在构造 LLM 输入前对用户消息进行净化
# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# P1+ 注入模式黑名单：命中后包裹 ``>>>suspicious:...<<<``，让 LLM 看到结构化标记后
# 更容易判断"这是 data 不是 command"。攻击者可绕过 (unicode 同形字 / 拼音 / 谐音)，
# 属于纵深防御的额外一层，不是替代品；主防御仍是 _SCHEMA_TEXT 中的 untrusted 规则 +
# delimiter + LLM 自身判断。
# 2026-11-17: 注入模式与包裹助手已迁到 routers/agent_chat.py。


# 2026-11-17: 注入模式与包裹助手已迁到 routers/agent_chat.py。


# ── LLM-powered Text-to-SQL ──

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# ── Data-to-Text: second LLM pass for natural-language summary ──

# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# ── P0 安全：Agent Chat SQL 表名白名单 ───────────────────────────────
# 只允许 Data Copilot 访问这些只读表。任何 sqlite_master / pragma / attach
# 或其他敏感表（api_keys、license 等）一律拒绝 — 防止 LLM 提示词注入导致
# 数据外泄。表名解析用纯 regex，移除分隔符/注释/引号后匹配 FROM/JOIN 子句。
# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 显式拒绝的关键字 — 即便表名白名单通过也禁止（DML/危险 pragma 痕迹）
# 2026-11-17: 本条已迁到 routers/agent_chat.py。


import re as _re_sql_table

# 匹配 FROM/JOIN 后的标识符；剥离反引号/双引号/方括号
# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: 本条已迁到 routers/agent_chat.py。


# 2026-11-17: macro 簇（context/history/calendar + 助手与缓存）已迁到 routers/macro.py。


# 2026-11-17: macro 簇（context/history/calendar + 助手与缓存）已迁到 routers/macro.py。


# 2026-11-17: macro 簇（context/history/calendar + 助手与缓存）已迁到 routers/macro.py。


# health 用的表清单快照：启动时算一次，请求时不再打 DB。
# 8100 那边同样是启动快照（v345 的教训：引擎写锁一紧，连健康检查都排队）。
try:
    with sqlite3.connect(str(config.DB_PATH), timeout=5) as _ht_conn:
        _HEALTH_TABLES = [r[0] for r in _ht_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
except Exception:  # pragma: no cover
    _HEALTH_TABLES = []


# 2026-11-17: 本条已迁到 routers/health.py。


def _decision_engine_health() -> Dict[str, Any]:
    """v340: 决策引擎健康快照 —— 一个接口看清"在跑但不出决策"。

    背景 (2026-11-15 实测): LLM 通道全因额度熔断后，引擎照常打日志、
    /api/health 也照常返回 ok，但 ai_decisions 一条都不出。运维只能靠翻日志。
    """
    out: Dict[str, Any] = {"ok": False}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        import pipeline_stall_guard as _psg
        d = _psg.check_stall(conn, stall_hours=2)
        out.update({
            "ok": True,
            "stalled": bool(d.get("stalled")),
            "last_decision": d.get("last_decision"),
            "idle_hours": d.get("idle_hours"),
            "since_epoch_hours": d.get("since_epoch_hours"),
            "news_since": d.get("news_since"),
            "decisions_24h": d.get("decisions_24h"),
            "summary": _psg.format_report(d),
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# 2026-11-17: 本条已迁到 routers/health.py。


# 2026-11-17: 本条已迁到 routers/health.py。


# ══════════════════════════════════════════════════════════════════
# v226: Fed 利率预期 + 跨资产异常 — 两个独立查询端点
# ──────────────────────────────────────────────────────────────────


# ── v361 P1: 审计改成"入队 + 单线程写" ─────────────────────────────────────
# 原实现每个请求在事件循环里同步 demo_audit.record(); 1.4G 主库 + 审计库写锁竞争时,
# 一个请求就能把后续所有请求（含 /api/health）堵死 20s+, 表现为"端口在、进程 0% CPU、就是不响应"。
# 现在请求路径只做一次 queue.put_nowait（无锁、不阻塞），写库丢给唯一的后台线程。
# ── v361 P2: 卡死取证 —— SIGUSR1 时把所有线程的栈打到 api.err.log ──────────
# 本机没有 root、py-spy 用不了。faulthandler 是进程内自带的, kill -USR1 即可打印。
import faulthandler as _faulthandler
import signal as _signal
try:
    _faulthandler.register(_signal.SIGUSR1, all_threads=True)
except Exception:
    pass

import queue as _audit_queue_mod
import threading as _audit_threading

_AUDIT_Q: "_audit_queue_mod.Queue[dict]" = _audit_queue_mod.Queue(maxsize=5000)


def _audit_enqueue(rec: dict) -> None:
    try:
        _AUDIT_Q.put_nowait(rec)
    except Exception:
        pass   # 队列满就丢审计, 绝不拖慢业务


def _audit_writer_loop() -> None:
    while True:
        rec = _AUDIT_Q.get()
        try:
            demo_audit.record(**rec)
        except Exception:
            pass


_audit_threading.Thread(target=_audit_writer_loop, daemon=True,
                        name="audit-writer").start()


# 2026-11-17: 本条已迁到 routers/macro.py。


# 2026-11-17: 本条已迁到 routers/macro.py。


# 2026-11-17: 本条已迁到 routers/macro.py。


# -- LLM 通道健康状态（OpenRouter / MiniMax / nassaapi） ----------------------

# 2026-11-18: _MODEL_HEALTH_CACHE 已迁到 model_probe.py。

# v379: 30s → 180s。探测是 10 个通道 × 每轮各发一次真实请求，
# 30s 一轮 ≈ 20 次/分钟，而 LLM_MAX_RPM=30 —— 也就是说 2/3 的额度被"体检"吃掉了，
# 真实决策反而抢不到名额、被闸门拦下，面板还把通道误判成掉线。
# 3 分钟一轮 ≈ 3.3 次/分钟，既够看状态，也把额度留给决策。
# 2026-11-18: _MODEL_HEALTH_TTL 已迁到 model_probe.py。

# 引擎侧 llm_health.json 的"陈旧"阈值（秒）。引擎只在策略触发开仓前探测一次，
# 间隔本来就可能很长；但一份超过 30 分钟的快照不该再被当成实时状态展示。
# 2026-11-18: _ENGINE_HEALTH_STALE_SEC 已迁到 model_probe.py。



# ── v323: 「额度耗尽」单独成类 (2026-11-10 实测) ────────────────────────
# 实测 (2026-11-10, 4 家远程通道全挂, 逐个直连拿到的真实响应):
#   minimax 账户1 → HTTP 429 {"type":"rate_limit_error","message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)"}
#   minimax 账户2 → HTTP 402 {"type":"insufficient_balance_error","message":"当前已达到 Token Plan 用量上限。… (2067)"}
#   xinshu        → HTTP 429 {"code":"API_KEY_QUOTA_EXHAUSTED","message":"API key 额度已用完"}
#   openrouter    → HTTP 401 {"error":{"message":"User not found."}}   ← key 已失效
#
# 旧口径把 429 一律归 rate_limit (语义=瞬时限流, 可重试), 402 归 api_error,
# 401 归 auth → 面板上看不出「套餐已经用完, 要充值」, 探针还会每 60s 再打一次
# 必然失败的通道。现在把「不会自愈、必须人处理」的两类单独分出来:
#   quota_exhausted —— 套餐/余额用完 (402 或带额度标记的 429) → 需充值 / 换 key
#   auth            —— key 失效 (401/403)                     → 需换 key
# 注意: 只有 body 里出现**明确额度标记**时才归 quota_exhausted —— 普通 429 限流
# 仍然归 rate_limit (瞬时, 可重试), 不能把两者混为一谈。
# 2026-11-18: _QUOTA_EXHAUSTED_MARKERS 已迁到 model_probe.py。



# 2026-11-18: _classify_probe_failure 已迁到 model_probe.py。



# ── v323: 探针侧的「不会自愈」静默窗口 ────────────────────────────────────
# quota_exhausted (套餐/余额用完) / auth (key 失效) 探测也不会好, 反复打只是
# 浪费额度 + 让面板反复闪红。命中后进入静默窗口, 窗口内**零网络请求**,
# 窗口过后自动复探 → 充值/换 key 之后能自愈, 不必重启进程。
# 2026-11-18: _PROBE_DEAD_WINDOW_S 已迁到 model_probe.py。

# 2026-11-18: _PROBE_DEAD_STATE 已迁到 model_probe.py。



# 2026-11-18: _probe_dead_state_reset 已迁到 model_probe.py。



# 2026-11-18: _is_local_base 已迁到 model_probe.py。



# 2026-11-18: _probe_channel 已迁到 model_probe.py。



# 同一账户(key)两次探测之间的最小间隔(秒)。
# MiniMax 的 Token Plan 是**账户级**配额：把同 key 的多个通道并发探测 = 自己和自己
# 抢配额 → 面板出现并不存在的"通道不可用"(429)。去重之后（见 _probe_specs_deduped）
# 同一 key 最多 3 组，再错开 0.8s，就不会再自造限流。
# 2026-11-18: _PROBE_SAME_KEY_STAGGER 已迁到 model_probe.py。



# 2026-11-18: _probe_specs_deduped 已迁到 model_probe.py。



# ─── v366 P0: 通道探测搬到后台线程，请求路径绝不发网络调用 ──────────────
# 事故（2026-11-15 22:0x 实测）：
#   本地 Ollama 探测超时是 (10, 120)，冷启动/swap 时单次实测 90s 不返回；
#   而 /api/connectivity/status 与 /api/health/models 把探测**同步跑在请求线程**里
#   → 两个接口双双 45~60s 无响应、前端联通中心永远转圈。
# 改法：缓存有值就直接返回（附带缓存年龄/是否过期），过期也只是**踢一次后台刷新**；
#   缓存为空时立刻返回同结构的占位（warming_up=true），并由后台线程去填。
#   任何外部调用（curl/前端/巡检）都不会再被模型探测拖住。
# 2026-11-18: _MODEL_HEALTH_LOCK 已迁到 model_probe.py。

# 2026-11-18: _MODEL_HEALTH_THREAD 已随 _ensure_model_health_thread 迁到 model_probe.py。
# 2026-11-18: _MODEL_HEALTH_LAST_ERROR 已迁到 model_probe.py。

# 2026-11-18: _MODEL_HEALTH_REFRESH_MIN_GAP 已迁到 model_probe.py。



# 2026-11-18: _model_health_skeleton 已迁到 model_probe.py。



# 2026-11-18: _funnel_overlay 已迁到 model_probe.py。



# 2026-11-18: _model_health_refresh_loop 已迁到 model_probe.py。



# 2026-11-18: _ensure_model_health_thread 已迁到 model_probe.py。



# 2026-11-18: _kick_model_health_refresh 已迁到 model_probe.py。



# 2026-11-18: _probe_model_channels 已迁到 model_probe.py。



# 2026-11-18: _probe_model_channels_now 已迁到 model_probe.py。



# 2026-11-17: 本条已迁到 routers/health.py。


# -- AI 交易复盘（Review） ------------------------------------------------

def _run_review_sync(
    scope: str,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """执行复盘：总结 + 失败归类 + 经验入库（分来源）+ 因果链自动沉淀。

    v290 P0+（反思系统时段统一）：新增 hours/from_ts/to_ts 窗口参数；
    窗口非空时 review_agent.run_review 按 created_at ∈ [lo, hi] 过滤，
    与顶部 ReviewOverview / 块 A / 块 B 同源同窗。scope 仍保留作为 fallback。
    """
    # ── 解析窗口(与 /api/replay/stats、/api/review/snapshot 走同一份 _resolve_window) ──
    from quick_sim import _resolve_window
    lo, hi = _resolve_window(hours, from_ts, to_ts)

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        report = review_agent.run_review(conn, scope, lo=lo, hi=hi)
        rid = review_agent.save_report(conn, scope, report)
        # 归因复核闭环：失败归因明细落库（source='review', ref_id=report_id）。
        # 去重（跨期复盘同一失败交易只计一次）在 save_attribution_details 内按 trade_id 完成，
        # 避免多次复盘累计漂移导致 FAILURE STATS 数据不同步。
        review_agent.save_attribution_details(conn, "review", rid, report.get("_attribution_rows") or [])
        # 经验规则分来源入库：历史交易 origin='real'；快速评测提炼 origin='quick_sim'
        by_origin = report.get("insights_by_origin") or {}
        added_real = review_agent.save_insights(
            conn, by_origin.get("real") or [], source_samples=int(report.get("samples") or 0),
            origin="real",
        )
        added_qs = review_agent.save_insights(
            conn, by_origin.get("quick_sim") or [], source_samples=int(report.get("quick_sim_samples") or 0),
            origin="quick_sim",
        )
        # 复盘提炼的实证因果链 → 自动补充因果链库（cause+effect 去重）
        causal_result = causal_chains.add_chains_dedup(
            report.get("causal_chains_auto") or [], conn
        )
        report["report_id"] = rid
        report["new_insights"] = added_real + added_qs
        report["new_insights_real"] = added_real
        report["new_insights_quick_sim"] = added_qs
        report["causal_added"] = causal_result.get("added", 0)
        report["causal_skipped"] = causal_result.get("skipped", 0)
        report.pop("_attribution_rows", None)
        return report
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# v203+: 分析过程聚合接口 —— 单条新闻从入库到经验沉淀的全链路
# ─────────────────────────────────────────────────────────────

# 2026-11-17: 本条已迁到 routers/news.py 或 routers/indicators.py。


# 2026-11-17: 本条已迁到 routers/analysis_process.py。


# 2026-11-17: 本条已迁到 routers/analysis_process.py。


# 2026-11-17: 本条已迁到 routers/analysis_process.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 8 条 /api/review/attributions*、reports、insights 端点已迁到 routers/review.py
# （只依赖 review_agent 的那组）。/api/review/run 仍留在这里，因为它用 _run_review_sync，
# 而该函数还被 12977 行后台任务复用。

# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# ── P2 数据健康（多源 fallback）API ────────────────────────────────────
# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# ── P3 LLM 自述推理质量报告 API ────────────────────────────────────
# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# ── v275+ 「8 段推理链断点定位」支持接口 ──────────────────────────────────
# 设计意图：避免 /reasoning/review/list 每次返回全量 reasoning 文本(膨胀 payload);
# 仅在用户点击 8 段 dot 时按需拉。
# 返回三档原文(优先级: reasoning_path > reasoning > asset_reasoning):
#   - reasoning_path : 当时的「八步推理」分段文本(优先级最高)
#   - reasoning      : 当时的「决策 reasoning」字段(兜底)
#   - asset_reasoning: 当时的「逐品种解释」(兜底 2)
# - segments.positions: 用后端 _ANALYSIS_STEPS 同样规则算出每段在文本中的字符索引,
#                      前端不重复判定 present/missing,保证与 review/list 完全一致。
# 2026-11-17: 本条已迁到 routers/reasoning.py。


# 2026-11-17: 本条已迁到 routers/reasoning.py。


# ── v275+ 「AI 追问方向建议」接口（自由追问，不强制层数） ─────────────────
# 设计意图：用户在反思卡里写了一段后点 "💡 让 AI 给追问方向" →
# 后端走 ai_worker.chat reflection 池 → 一次性给 N 个不同方向的追问建议。
# 用户可挑 1-N 个接进自己的反思，也可全忽略。不强制任何固定层数。
# 输入：当前反思文本 + 错误码 + decision 上下文(可选)。
# 输出：JSON { questions: [...], model, error } — 2-3 个候选问句。
# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/data_health.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# v302+ P2 PR-17: 失败模式聚类 — 按 error_code 聚合,带趋势/品种/示例
# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# ── KB Facade API（v182.2）────────────────────────────────────────────────
# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/kb.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# ── 路线 A：股票类 ai_decisions 反思通道（v276）───────────────────────────
# 动机：paper_trading.TRACK_ASSETS["stocks"] 只覆盖 13 只有 Binance USDT 永续的
# 美股（如 NVDA/TSLA）。SKHYNIX/三星/台积电/小米/MU 等无 USDT 永续 → 没有
# paper-trading 结算 → /review 看不到任何股票反思。本通道绕过 paper-trading
# 结算要求，直接对 market_category='STOCK' 的 ai_decisions 跑 E001-E027
# 程序化校验器，落库 decision_errors（origin='stock_reflection_panel'）。
#
# 关键不变量：
#   - 不修改 paper_trading.py / ai_decisions 表任何字段
#   - 反射检查器（reflection_checkers.run_all_checkers）只读决策行 + 可选连接，
#     不要求 settled=1；其内部的 completeness 过滤（E019）已兜底脏决策
#   - save_errors 用 INSERT OR IGNORE，重复跑同一决策不会产生重复行
#   - origin='stock_reflection_panel' 与 'checker' 区分，方便 /api/reflection/recent
#     查询过滤；不污染现有反思统计
# ──────────────────────────────────────────────────────────────────────────

# 2026-11-17: 本条已迁到 routers/reflection.py。

# 2026-11-17: 本条已迁到 routers/reflection.py。

# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# 2026-11-17: 本条已迁到 routers/reflection.py。


# ── v372: 聚合类 GET 的 4 秒 TTL 缓存 ─────────────────────────────────
# 实测最近 2000 次请求里 /api/review/snapshot 470 次、/api/events/today 420 次、
# /api/review/insights/count 173 次 —— 前端多个页面各自定时轮询，每次都全量查库，
# 而库里同时有决策/行情在写，是 database is locked 的主要来源。
# 这些接口都是"看板聚合"，4 秒内的重复请求直接复用结果完全可接受。


# 2026-11-17: 4 秒 TTL 缓存已下沉到 common/ttl_cache.py（insights/count 也用它），
# 这里保留同名别名，snapshot / events/today 的调用点不变。
from common.ttl_cache import ttl_get as _ttl_get
from common.ttl_cache import ttl_put as _ttl_put


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# ─── v276+ 因果图谱扩展：跨资产神经 / 链条种子 / 反例持久化 ─────────────────────


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# 2026-11-17: 本条已迁到 routers/causal_graph.py。


# ─── v202 段 4 · 因果图谱改进候选（KG Improvements）─────────────────────
# /review 段 4 面板调用的 4 个端点（前端 AnalysisImprovePanel 已修 URL）


# 2026-11-17: 本条已迁到 routers/kg.py。


# 2026-11-17: 本条已迁到 routers/kg.py。


# 2026-11-17: 本条已迁到 routers/kg.py。


# 2026-11-17: 本条已迁到 routers/kg.py。


# 2026-11-17: 本条已迁到 routers/kg.py。


# 2026-11-17: 本条已迁到 routers/review.py。


# 2026-11-17: 本条已迁到 routers/review.py。


# 2026-11-17: 本条已迁到 routers/review.py。


# 2026-11-17: 本条已迁到 routers/review.py。


# 2026-11-17: 本条已迁到 routers/review.py。


# -- 提示词库（第 1 层采集入库过滤） -----------------------------------------

# 2026-11-17: 本条已迁到 routers/filter.py。


# 2026-11-17: 本条已迁到 routers/filter.py。


# 2026-11-17: 本条已迁到 routers/filter.py。


# 2026-11-17: 本条已迁到 routers/filter.py。


# -- 历史新闻交易回溯（History News Trade Review） ---------------------------

# 2026-11-17: 6 条 /api/history/news-trades 端点已整体迁到 routers/history.py
# （列表 / pending-count / rejudge-batch / 详情 / AI 复盘 / 重判）。
# 注意：pending-count 与 rejudge-batch 依赖「排在 {news_id} 之前」的注册顺序，
# 迁移时已在 routers/history.py 内原样保持。

def _review_check_due() -> bool:
    """同步检查是否需要今日复盘（放 to_thread，避免事件循环阻塞）。"""
    conn = db.get_connection()
    try:
        last = conn.execute(
            "SELECT MAX(generated_at) FROM review_reports WHERE scope='week'"
        ).fetchone()[0]
    finally:
        conn.close()
    now_dt = datetime.now(config.TZ_SHANGHAI)
    need = True
    if last:
        try:
            last_dt = datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")
            need = last_dt.date() != now_dt.date()
        except ValueError:
            need = True
    return need and now_dt.hour >= int(getattr(config, "REVIEW_HOUR", 0))


# v176.7 P0-5: missed_signal 增量扫描 watcher
# 入口已挂入 lifespan（search/missed 名称）。每 _MISSED_SIG_INTERVAL_S 秒一轮，
# 对 BTC/ETH/SOL/GOLD/WTI 调 missed_signal_detector.detect_for_recent(incremental=True)。
# 首次扫描允许走全量 (window_hours=72)，后续走增量。
_MISSED_SIG_ASSETS = ("BTC", "ETH", "SOL", "GOLD", "WTI")
_MISSED_SIG_INTERVAL_S = 300  # 5min
_MISSED_SIG_INITIAL_WINDOW_HOURS = 72


async def _missed_signal_scan_watcher() -> None:
    """missed_signal 自动扫描：定期按 asset 增量扫 HOLD 决策，对照 abnormal_signals 打标。"""
    await asyncio.sleep(15)  # 错峰：避开启动期 DB 写锁高峰
    is_initial = True
    while True:
        try:
            def _cycle() -> Dict[str, Any]:
                import missed_signal_detector as _msd
                totals = {"scanned": 0, "marked": 0, "per_asset": {}}
                for asset in _MISSED_SIG_ASSETS:
                    r = _msd.detect_for_recent(
                        asset=asset,
                        window_hours=_MISSED_SIG_INITIAL_WINDOW_HOURS if is_initial else 24,
                        incremental=not is_initial,
                    )
                    totals["scanned"] += int(r.get("scanned") or 0)
                    totals["marked"] += int(r.get("marked") or 0)
                    totals["per_asset"][asset] = {
                        "scanned": int(r.get("scanned") or 0),
                        "marked": int(r.get("marked") or 0),
                        "incremental": r.get("incremental"),
                    }
                return totals
            out = await asyncio.to_thread(_cycle)
            is_initial = False
            if out["scanned"] or out["marked"]:
                print(
                    f"[MISSED-SIG] scanned={out['scanned']} marked={out['marked']} per_asset={out['per_asset']}",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[MISSED-SIG] cycle crashed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(_MISSED_SIG_INTERVAL_S)


# v176.7 P1-2: K 线主动补全（解决前端从未访问某 timeframe 导致 kline_bars 稀疏）。
# 每 30min 拉一次所有 (asset, timeframe) 组合；真实源成功即落库，失败静默。
_KLINE_BACKFILL_ASSETS = ("BTC", "ETH", "SOL", "XAU", "XAG", "WTI")
_KLINE_BACKFILL_TIMEFRAMES = ("1h", "4h", "1d")
_KLINE_BACKFILL_INTERVAL_S = 1800  # 30min
_KLINE_BACKFILL_LIMIT = 200        # 每帧拉 200 根历史（1h=8.3天、4h=33天、1d=200天）


async def _kline_backfill_watcher() -> None:
    """v176.7 P1-2: 主动拉全量 (asset, timeframe) → kline_bars 兜底。

    之前 kline_bars 只能被动落库（用户访问 /api/klines/{symbol} 时才存）。
    SOL 1h/4h/1d 因前端只切 10m，从未被拉过 → 表里 145 条 → 现在主动每 30min 全量。
    真实源成功即落库；调用 _fetch_binance_klines 内部已有多源兜底 (fapi → spot → bybit → bitstamp)。
    失败静默，不污染 source_status（K 线走 _store_klines 不走 report）。
    """
    await asyncio.sleep(45)  # 错峰
    while True:
        try:
            def _cycle() -> Dict[str, int]:
                totals = {"ok": 0, "fail": 0}
                for asset in _KLINE_BACKFILL_ASSETS:
                    for tf in _KLINE_BACKFILL_TIMEFRAMES:
                        try:
                            r = _fetch_binance_klines(asset, tf)
                            if r and r[0]:
                                bars = r[0]
                                _store_klines(asset, tf, bars)
                                totals["ok"] += 1
                            else:
                                totals["fail"] += 1
                        except Exception:
                            totals["fail"] += 1
                return totals
            out = await asyncio.to_thread(_cycle)
            if out["ok"] or out["fail"]:
                print(
                    f"[KLINE-BACKFILL] ok={out['ok']} fail={out['fail']} "
                    f"({len(_KLINE_BACKFILL_ASSETS)} assets × {len(_KLINE_BACKFILL_TIMEFRAMES)} tfs)",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[KLINE-BACKFILL] cycle crashed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(_KLINE_BACKFILL_INTERVAL_S)


# v176.7 P3-A: signal_chain_archive 全链路快照补全 watcher
# 之前 archive_signal_chain 函数定义存在但 lifespan 未挂 → signal_chain_archive 永远 0 行。
# 这里每 10min 扫最近 24h 的 ai_decisions，幂等 INSERT OR REPLACE。
# 零 LLM 调用，纯 SQL 拼装；failure 不抛错不影响主链路。
_CHAIN_ARCHIVE_INTERVAL_S = 600  # 10min
_CHAIN_ARCHIVE_WINDOW_HOURS = 24


async def _chain_archive_watcher() -> None:
    """v176.7 P3-A: 把最近 24h 的 ai_decisions 全链路快照写入 signal_chain_archive。

    第一次跑会全量回填 24h 的所有决策；之后增量按 created_at 滑动。
    signal_chain_archive 是 v189 反思系统的素材源（decision_layer_signals / reverse_attributions 都基于它）。
    """
    await asyncio.sleep(60)  # 错峰
    while True:
        try:
            def _cycle() -> Dict[str, int]:
                import daily_archive as _da
                totals = {"scanned": 0, "archived": 0, "missing": 0}
                # v490 P0-1 长事务拆短：原先整批复用一个连接、遍历 24h 全部决策后
                # 末尾才一次 commit —— 首条 INSERT 起写锁就被整轮归档占住，实测出现
                # 193s / 314.6s 的长事务（探针 stack 即此 lambda）。改为：只读扫描用一个
                # 连接取 id 列表；每条归档各自取短连接、内部 commit，写锁持有时长
                # = 单条 upsert（毫秒级），不再跨整批。
                scan_conn = db.get_connection()
                try:
                    rows = scan_conn.execute(
                        "SELECT id FROM ai_decisions WHERE created_at > datetime('now', ?)",
                        (f"-{_CHAIN_ARCHIVE_WINDOW_HOURS} hours",),
                    ).fetchall()
                finally:
                    scan_conn.close()
                for (dec_id,) in rows:
                    totals["scanned"] += 1
                    one = db.get_connection()
                    try:
                        ok = _da.archive_signal_chain(one, int(dec_id))
                    finally:
                        one.close()
                    if ok:
                        totals["archived"] += 1
                    else:
                        totals["missing"] += 1
                return totals
            # v189-8: 整轮扫描共用一个连接，一次 SQLITE_BUSY 会让本轮已归档的进度全部
            # 作废(下个周期从头再来)。_cycle 自建连接且按时间窗幂等,整体做有界重试,
            # 最坏 ≈ 3×5s + 0.35s。
            out = await asyncio.to_thread(lambda: db.retry_on_busy(_cycle))
            print(
                f"[CHAIN-ARCHIVE] scanned={out['scanned']} archived={out['archived']} "
                f"missing={out['missing']} window={_CHAIN_ARCHIVE_WINDOW_HOURS}h",
                flush=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[CHAIN-ARCHIVE] cycle crashed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(_CHAIN_ARCHIVE_INTERVAL_S)


# v176.7 P3-B: reverse_attributions 反向归因 watcher
# 之前 reverse_attributions 表存在但无 INSERT 路径 → 永远 0 行。
# 这里每 30min 扫最近 24h 的 abnormal_signals（异动点），对照 ±1h 内的 ai_decisions：
# - related_decision_ids = ±1h 内 created_at 的 ai_decision id 列表
# - missed_signal_count = related 里 missed_signal=1 的数量
# - forward_win/loss_count = related 里 is_correct=WIN/LOSS 的数量
# - coverage_ratio = related_count / abnormal_count（决策覆盖异动率）
# 全部 UPSERT（UNIQUE(asset, abnormal_ts)）。
# 2026-11-18: _REVERSE_ATTR_* 常量已随 _reverse_attribution_watcher 迁到 api_maintenance.py。



# 2026-11-18: _reverse_attribution_watcher 已迁到 api_maintenance.py。



async def review_watcher() -> None:
    """每日自动复盘：每天 REVIEW_HOUR 点后执行一次 week 复盘（每天最多一次）。

    注意：所有 DB 访问放 to_thread；启动先延迟 60s，避免启动期与引擎
    写库撞锁导致同步 busy_timeout 阻塞事件循环（会让所有请求断开）。
    """
    await asyncio.sleep(60)
    while True:
        try:
            if await asyncio.to_thread(_review_check_due):
                result = await asyncio.to_thread(_run_review_sync, "week")
                print(f"[REVIEW] 每日自动复盘完成: {result.get('samples')} 笔交易, 新经验 {result.get('new_insights')} 条", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[REVIEW] {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------

# 公司级财报日更（v406-fix）—— 自带调度，不依赖 launchd
# ---------------------------------------------------------------------------

# 为什么不再只靠 launchd：
#   实测 `launchctl kickstart` 退出码 126，launchd 错误日志写着
#   "shell-init: getcwd: Operation not permitted" +
#   "/bin/bash: .../cron/run_equity_fundamentals.sh: Operation not permitted"
#   —— macOS TCC 不给后台守护进程 ~/Desktop 访问权。也就是说 plist 装好了、
#   load 了、runs 也涨了，但脚本一次都执行不了（"装了但没生效"）。
#   引擎进程（api_server）由用户会话启动，本来就有项目目录权限，且 24h 在跑，
#   所以把"每天跑一次"这件事交给它自己。launchd 保留：万一引擎没起，且用户
#   给过全盘访问，它仍能兜底。两者靠同一个 state 文件互斥：谁先跑谁写日期，
#   后到的当天直接跳过，不会重复烧 AV 配额。
_EQUITY_DAILY_STATE = "equity_fundamentals.last_run"


def _equity_daily_state_path() -> "pathlib.Path":
    import pathlib as _pl
    base = os.getenv("TRIDENT_LOG_DIR") or os.path.expanduser("~/.trident/log")
    return _pl.Path(base) / _EQUITY_DAILY_STATE


def _equity_daily_due(now: Optional[Any] = None, last_run: Optional[str] = None) -> bool:
    """今天该不该跑？（纯函数，便于测试）

    规则：本地时间已过 EQUITY_DAILY_HOUR:MINUTE，且状态文件里记的日期不是今天。
    用本地日期而不是"距上次 N 小时"：季报一天一次就够，跑早了没意义，
    跑晚了（引擎重启后补跑）要能补上。
    """
    import datetime as _dt
    n = now or _dt.datetime.now()
    if last_run is None:
        try:
            last_run = _equity_daily_state_path().read_text(encoding="utf-8").strip()
        except Exception:
            last_run = ""
    today = n.strftime("%Y-%m-%d")
    if (last_run or "").strip() == today:
        return False
    return (n.hour, n.minute) >= (config.EQUITY_DAILY_HOUR, config.EQUITY_DAILY_MINUTE)


def _equity_daily_run_sync() -> Dict[str, Any]:
    """跑一次日更脚本（子进程，超时 30 分钟），成功才写日期。"""
    import subprocess as _sp
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "scripts", "cron_equity_fundamentals.py")
    if not os.path.exists(script):
        return {"ok": False, "reason": "脚本不存在: %s" % script}
    try:
        # 与 cron/run_equity_fundamentals.sh 对齐：固定关注清单 + 近 7 天占位决策里
        # 反解出的 A股/港股（--cn-auto 走东财，不耗 AV 配额）。
        cp = _sp.run([_sys.executable, script, "--show",
                      "--cn", "600519.SH,00700.HK", "--cn-auto", "10"],
                     cwd=root, capture_output=True, text=True, timeout=1800)
    except _sp.TimeoutExpired:
        return {"ok": False, "reason": "超时 1800s"}
    tail = (cp.stdout or "").strip().splitlines()[-3:]
    return {"ok": cp.returncode == 0, "code": cp.returncode,
            "tail": " | ".join(tail), "err": (cp.stderr or "").strip()[-300:]}


async def equity_fundamentals_daily_watcher() -> None:
    """每 10 分钟看一眼：到点且今天没跑过 → 跑一次。"""
    await asyncio.sleep(90)  # 错峰：避开启动期写库高峰
    if not config.EQUITY_DAILY_AUTO_ENABLED:
        print("[EQUITY-DAILY] 已关闭（EQUITY_DAILY_AUTO_ENABLED=0）", flush=True)
        return
    while True:
        try:
            if await asyncio.to_thread(_equity_daily_due):
                res = await asyncio.to_thread(_equity_daily_run_sync)
                if res.get("ok"):
                    try:
                        _equity_daily_state_path().parent.mkdir(parents=True, exist_ok=True)
                        _equity_daily_state_path().write_text(
                            __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
                            encoding="utf-8")
                        print("[EQUITY-DAILY] 日更完成: %s" % res.get("tail"), flush=True)
                    except Exception as exc:
                        print("[EQUITY-DAILY] 写状态失败: %s" % exc, flush=True)
                else:
                    # 失败不写日期 → 下一轮（10 分钟后）会重试。配额耗尽属预期，
                    # 脚本自己 exit 0，不会反复重试烧配额。
                    print("[EQUITY-DAILY] 本轮失败(不记日期，稍后重试): %s %s"
                          % (res.get("reason") or res.get("code"), res.get("err", "")[:200]),
                          flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[EQUITY-DAILY] %s: %s" % (type(exc).__name__, exc), flush=True)
        await asyncio.sleep(config.EQUITY_DAILY_CHECK_S)


_SHADOW_DAILY_STATE = "fundamental_shadow.last_run"


def _shadow_daily_state_path() -> "pathlib.Path":
    """状态文件与财报日更同目录（~/.trident/log），便于一起诊断。"""
    import pathlib as _pl
    base = os.getenv("TRIDENT_LOG_DIR") or os.path.expanduser("~/.trident/log")
    return _pl.Path(base) / _SHADOW_DAILY_STATE


def _shadow_daily_due(now: Optional[Any] = None, last_run: Optional[str] = None) -> bool:
    """今天该不该结算？（纯函数，便于测试）

    埋在 06:20 之后：财报日更 05:10 先把日历/三表刷完，6 点后再拿日线结算，
    避免拿"昨天的日历"解释"今天的行情"。
    """
    import datetime as _dt
    n = now or _dt.datetime.now()
    if last_run is None:
        try:
            last_run = _shadow_daily_state_path().read_text(encoding="utf-8").strip()
        except Exception:
            last_run = ""
    if (last_run or "").strip() == n.strftime("%Y-%m-%d"):
        return False
    return (n.hour, n.minute) >= (6, 20)


def _shadow_daily_run_sync() -> Dict[str, Any]:
    """跑一次结算（子进程 5 分钟），成功才写日期 —— 失败次日重试，不空转。"""
    import subprocess as _sp
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "src_python", "fundamental_shadow.py")
    if not os.path.exists(script):
        return {"ok": False, "reason": "脚本不存在: %s" % script}
    try:
        cp = _sp.run([_sys.executable, script, "--days", "30"],
                     cwd=root, capture_output=True, text=True, timeout=300)
    except _sp.TimeoutExpired:
        return {"ok": False, "reason": "超时 300s"}
    tail = (cp.stdout or "").strip().splitlines()[-2:]
    return {"ok": cp.returncode == 0, "code": cp.returncode,
            "tail": " | ".join(tail), "err": (cp.stderr or "").strip()[-300:]}


async def fundamental_shadow_daily_watcher() -> None:
    """基本面权重影子账本：每天 06:20 后结算一次（引擎自带调度，绕开 launchd 的 TCC 限制）。"""
    await asyncio.sleep(120)
    while True:
        try:
            if _shadow_daily_due():
                res = await asyncio.to_thread(_shadow_daily_run_sync)
                if res.get("ok"):
                    try:
                        _shadow_daily_state_path().parent.mkdir(parents=True, exist_ok=True)
                        _shadow_daily_state_path().write_text(
                            __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
                            encoding="utf-8")
                        print("[FUND-SHADOW] 结算完成: %s" % res.get("tail"), flush=True)
                    except Exception as exc:
                        print("[FUND-SHADOW] 写状态失败: %s" % exc, flush=True)
                else:
                    print("[FUND-SHADOW] 本轮失败(不记日期，稍后重试): %s %s"
                          % (res.get("reason") or res.get("code"), res.get("err", "")[:200]),
                          flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[FUND-SHADOW] %s: %s" % (type(exc).__name__, exc), flush=True)
        await asyncio.sleep(1800)


# ---------------------------------------------------------------------------
# Trading System API — 精简接口供外部交易系统对接
# ---------------------------------------------------------------------------

_SIGNALS_LEGACY_KEYS = ("action", "settled")


def _fetch_signals_from_bridge(asset: str, from_ts: int, to_ts: int,
                               hide_neutral: int, limit: int) -> Optional[Dict[str, Any]]:
    """从 8100 取信号（dict: ok/source/signals[]，带盈亏/置信度/命中）。

    v398: 8100 是前端实际入口，它返回的是**对象**、8000 返回的是**列表** ——
    同一个 URL 两种顶层形状，前端按 8100 写就会在 8000 上崩。这里统一转发到 8100。

    参数只透传「客户端确实给了」的那些：hide_neutral 默认 -1 表示未指定，
    此时不传，让 8100 用自己的默认值（0=不过滤），而不是把 8000 的历史默认(1)带过去。
    否则同一条无参请求，两端的过滤口径还是不一样。
    """
    try:
        import urllib.parse as _up
        import urllib.request as _ur
        _q = {"limit": int(limit)}
        if asset:
            _q["asset"] = asset
        if from_ts > 0:
            _q["from_ts"] = int(from_ts)
        if to_ts > 0:
            _q["to_ts"] = int(to_ts)
        if hide_neutral >= 0:
            _q["hide_neutral"] = int(hide_neutral)
        _opener = _ur.build_opener(_ur.ProxyHandler({}))
        _url = config.BRIDGE_BASE_URL + "/api/trading/signals?" + _up.urlencode(_q)
        with _opener.open(_url, timeout=15) as _resp:
            _d = json.loads(_resp.read())
        if isinstance(_d, dict) and isinstance(_d.get("signals"), list):
            return _d
    except Exception:
        return None
    return None


# 2026-11-17: 本条已迁到 routers/trading_v201.py。


# 2026-11-17: 本条已迁到 routers/trading_v201.py。


# ─── /api/v201/event/opportunities (v319+: 时点口径的事件机会卡) ───
# 2026-11-17: 本条已迁到 routers/trading_v201.py。


# ─── /api/v201/trade-points (v318: 下次交易点日历, 含 T+0/T+1 与滑点) ───
# 2026-11-17: 本条已迁到 routers/trading_v201.py。


# 2026-11-17: 本条已迁到 routers/trading_v201.py。


def _authorize_trading_manager(token: str | None) -> None:
    # 修复：A1 — 默认禁用。哪怕 token 已配，未显式 TRADING_MANAGER_ENABLED=1 也直接 503。
    if not _trading_manager_enabled():
        raise HTTPException(
            status_code=503,
            detail="TRADING_MANAGER 默认禁用（fail-closed）。如需启用：环境变量 TRADING_MANAGER_ENABLED=1 + 配置 TRADING_MANAGER_TOKEN。",
        )
    expected = (config.TRADING_MANAGER_TOKEN or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="TRADING_MANAGER_TOKEN 未配置，交易管理器已禁用（fail-closed）")
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid X-Trading-Token")


# 2026-11-17: 本条已迁到 routers/trading.py（manager 句柄仍在本文件）。


# 2026-11-17: 本条已迁到 routers/trading.py（manager 句柄仍在本文件）。


# 2026-11-17: 本条已迁到 routers/trading.py（manager 句柄仍在本文件）。


# 2026-11-17: 本条已迁到 routers/trading.py（manager 句柄仍在本文件）。


# -- Replay endpoints (信号复盘看板 · 模拟盘) ----------------------------

def _build_simulated_klines(
    *,
    entry_time_ts: int,
    entry_price: float,
    max_price: float | None,
    min_price: float | None,
    exit_price: float | None,
    asset: str,
    hours_before: int = 6,
    hours_after: int = 6,
) -> list[dict]:
    """
    Build a simulated 1-hour K-line series around the signal's entry_time.

    The series is anchored on ``entry_price`` and shaped so that the bar
    covering the entry moment has open == entry_price, and the highest /
    lowest extreme prices in the post-entry window match the recorded
    ``max_price`` / ``min_price`` (if available). This is a *paper-trading*
    visual reconstruction — it is NOT real market data, and the frontend
    surfaces a "模拟盘" badge next to the chart.
    """
    import math
    import random

    if entry_price <= 0:
        entry_price = 100.0

    asset_upper = (asset or "").upper()
    base_jitter = max(entry_price * 0.0035, 0.01)

    # 30 分钟粒度：6 小时前后各 12 根，+1 根入场 bar，共 25 根，图表更饱满
    step_minutes = 30
    bars_before = max(hours_before * 60 // step_minutes, 2)
    bars_after = max(hours_after * 60 // step_minutes, 2)
    total = bars_before + 1 + bars_after  # +1 for the entry bar

    # Anchor on the entry time slot
    entry_dt = datetime.fromtimestamp(entry_time_ts, TZ_SHANGHAI).replace(
        minute=0, second=0, microsecond=0
    )
    start_dt = entry_dt - timedelta(minutes=bars_before * step_minutes)

    rng = random.Random(int(entry_time_ts) ^ hash(asset_upper) & 0xFFFFFFFF)

    # Walk forward and let max/min guide the post-entry path
    bars: list[dict] = []
    last_close = entry_price
    # Pre-entry path: gentle mean-reversion around entry_price
    for i in range(bars_before):
        ts = int((start_dt + timedelta(minutes=i * step_minutes)).timestamp())
        drift = rng.uniform(-base_jitter, base_jitter)
        o = last_close + drift * 0.3
        c = o + drift * 0.7
        h = max(o, c) + rng.uniform(0, base_jitter * 0.4)
        lo = min(o, c) - rng.uniform(0, base_jitter * 0.4)
        bars.append({
            "time": ts,
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(lo, 4),
            "close": round(c, 4),
            "volume": int(rng.uniform(2000, 12000)),
        })
        last_close = c

    # Entry bar (the trigger bar) — open at entry_price, bias close toward
    # the action direction so the chart visually "moves" on the signal.
    o = entry_price
    direction_bias = 1.0 if asset_upper in {"XAU", "GOLD"} else 1.0
    c = o + rng.uniform(-base_jitter * 0.6, base_jitter * 0.6) * direction_bias
    bars.append({
        "time": int(entry_dt.timestamp()),
        "open": round(o, 4),
        "high": round(max(o, c) + base_jitter * 0.3, 4),
        "low":  round(min(o, c) - base_jitter * 0.3, 4),
        "close": round(c, 4),
        "volume": int(rng.uniform(8000, 25000)),
    })
    last_close = c

    # Post-entry path: force the high/low extremes (if recorded) to appear.
    target_high = max_price if (max_price and max_price > 0) else None
    target_low = min_price if (min_price and min_price > 0) else None
    ext_pos = rng.randrange(1, bars_after + 1)  # where the high lives
    ext_low_pos = rng.randrange(1, bars_after + 1)  # where the low lives

    for i in range(1, bars_after + 1):
        ts = int((entry_dt + timedelta(minutes=i * step_minutes)).timestamp())
        o = last_close
        # Determine this bar's high/low targets
        this_high = None
        this_low = None
        if target_high is not None and i == ext_pos:
            this_high = target_high
        if target_low is not None and i == ext_low_pos:
            this_low = target_low

        if this_high is not None or this_low is not None:
            # Build an extreme bar that respects both bounds.
            hi = this_high if this_high is not None else (o + base_jitter * 0.5)
            lo = this_low if this_low is not None else (o - base_jitter * 0.5)
            # Make sure high >= low
            if hi < lo:
                hi, lo = lo, hi
            c = (hi + lo) / 2 + rng.uniform(-base_jitter * 0.2, base_jitter * 0.2)
        else:
            drift = rng.uniform(-base_jitter, base_jitter)
            c = o + drift
            hi = max(o, c) + rng.uniform(0, base_jitter * 0.4)
            lo = min(o, c) - rng.uniform(0, base_jitter * 0.4)

        bars.append({
            "time": ts,
            "open": round(o, 4),
            "high": round(hi, 4),
            "low": round(lo, 4),
            "close": round(c, 4),
            "volume": int(rng.uniform(3000, 18000)),
        })
        last_close = c

    return bars


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


def _paper_metrics(signal: Dict[str, Any], current_price: Optional[float]) -> Dict[str, Any]:
    entry = float(signal.get("entry_price") or 0.0)
    action = str(signal.get("action") or "").upper()
    settled = int(signal.get("settled") or 0)
    high = float(signal.get("max_price") or entry)
    low = float(signal.get("min_price") or entry)
    if current_price is not None and current_price > 0:
        high = max(high, current_price)
        low = min(low, current_price)
    pnl: Optional[float] = None
    mfe = 0.0
    mae = 0.0
    if settled:
        # 已结算：直接用引擎权威 forward_pnl / mfe_pct / mae_pct ——
        # 引擎结算时已按分级止盈加权（partial_pnl_pct×已平比例 + 剩余按出场价），
        # 不能拿 (exit-entry) 重算，否则分级止盈交易口径错乱（可能方向都反）。
        fwd = signal.get("forward_pnl")
        pnl = float(fwd) if fwd is not None else None
        mfe = float(signal.get("mfe_pct") or 0.0)
        mae = float(signal.get("mae_pct") or 0.0)
    elif entry > 0:
        if action == "BUY":
            if current_price is not None:
                pnl = (current_price - entry) / entry * 100
            mfe = (high - entry) / entry * 100
            mae = (entry - low) / entry * 100
        elif action == "SELL":
            if current_price is not None:
                pnl = (entry - current_price) / entry * 100
            mfe = (entry - low) / entry * 100
            mae = (high - entry) / entry * 100
    params = signal.get("strategy_params") or strategy_store.default_params()
    # v386 口径统一：单笔盈亏必须用**这笔仓位自己的名义金额**
    # （quick_sim_trades.notional_usdt 是结算账本的权威口径）。
    # 之前一律用 strategy_params.notional_usdt（5000），而引擎按 100 结算，
    # 导致前端账户 -215.28 USDT / 漏斗日报 -4.31 USDT 两个数字同时"正确"——
    # 同一套账两套口径，直接摧毁"可审计"的可信度。
    trade_notional = signal.get("notional_usdt")
    try:
        notional = float(trade_notional) if trade_notional else float(params.get("notional_usdt") or 0.0)
    except (TypeError, ValueError):
        notional = float(params.get("notional_usdt") or 0.0)
    # notional 为下单名义金额：盈亏 = 名义金额 × 涨跌幅（杠杆只影响保证金占用，
    # 不影响盈亏金额本身，避免 5x 杠杆把盈亏虚增 5 倍）。
    return {
        **signal,
        "current_price": current_price,
        "current_pnl_pct": round(pnl, 4) if pnl is not None else None,
        "current_pnl_usdt": round(notional * pnl / 100, 4) if pnl is not None else None,
        "live_mfe_pct": round(max(0.0, mfe), 4),
        "live_mae_pct": round(max(0.0, mae), 4),
        "pricing_status": "LIVE" if current_price is not None else "UNAVAILABLE",
        "paper_status": "SETTLED" if settled else "TRACKING",
    }


# 2026-11-17: PaperTradingBody 已随 replay/settings 端点迁到 routers/replay.py。


def _combined_replay_settings() -> Dict[str, Any]:
    trading = paper_trading.get_settings()
    news = news_sources.get_settings()
    return {**trading, "news_settings": news}


def _apply_replay_settings(body: PaperTradingBody) -> Dict[str, Any]:
    current = paper_trading.get_settings()
    next_running = current["is_running"] if body.is_running is None else bool(body.is_running)
    next_tracks = current["tracks"] if body.tracks is None else list(body.tracks)
    if body.is_running is not None or body.tracks is not None or body.gate_enabled is not None \
            or body.news_trade_max_age_seconds is not None \
            or body.tp_enabled is not None or body.tp1_mult is not None or body.tp2_mult is not None \
            or body.tp1_ratio is not None or body.tp2_ratio is not None \
            or body.buy_min_score is not None or body.sell_min_score is not None \
            or body.sl_mult is not None \
            or body.market_structure_enabled is not None:
        paper_trading.set_settings(
            next_running,
            next_tracks,
            gate_enabled=body.gate_enabled,
            news_trade_max_age_seconds=body.news_trade_max_age_seconds,
            tp_enabled=body.tp_enabled,
            tp1_mult=body.tp1_mult,
            tp2_mult=body.tp2_mult,
            tp1_ratio=body.tp1_ratio,
            tp2_ratio=body.tp2_ratio,
            buy_min_score=body.buy_min_score,
            sell_min_score=body.sell_min_score,
            sl_mult=body.sl_mult,
            market_structure_enabled=body.market_structure_enabled,
        )
    if body.news_enabled is not None or body.news_daily_target is not None or body.news_sources is not None:
        news_sources.set_settings(
            enabled=body.news_enabled,
            daily_target=body.news_daily_target,
            sources=body.news_sources,
        )
    return _combined_replay_settings()


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# -- Replay reset (一键重置模拟盘) --------------------------------------

BACKUP_DIR = os.path.join(config.BASE_DIR, "backups")


# 2026-11-18: _auto_backup_trading_data 已迁到 api_maintenance.py。



# 2026-11-18: _clear_trading_traces 已迁到 api_maintenance.py。



def _reset_replay_sync(force: bool = False) -> Dict[str, Any]:
    """一键重置模拟盘：停止 → 自动备份 → 清空交易痕迹 → 失效统计缓存。

    v482: 加**重置冷却**。此前每次重置都会把决策/反思/验证样本清回 0，实测
    11-19 一天内被清了多次 —— 而「洞察验证」要同一参数键攒够 10 条决策才判得动，
    重置一频繁就永远攒不够，所有闭环指标恒为 0。默认 15 分钟内拒绝第二次重置
    （`REPLAY_RESET_COOLDOWN_S` 可调，设为 0 关闭；`force=1` 可强制越过）。
    """
    cooldown = int(os.getenv("REPLAY_RESET_COOLDOWN_S", "900") or 0)
    _state_dir = os.path.join(os.path.expanduser("~"), ".trident", "state")
    _stamp = os.path.join(_state_dir, "last_replay_reset.ts")
    last = 0.0
    try:
        with open(_stamp, "r", encoding="utf-8") as _fh:
            last = float((_fh.read() or "0").strip())
    except Exception:
        last = 0.0
    if cooldown > 0 and last and not force:
        gap = time.time() - last
        if gap < cooldown:
            left = int(cooldown - gap)
            return {
                "ok": False,
                "error": "reset_cooldown",
                "message": (f"距上次重置只有 {int(gap)} 秒（冷却 {cooldown} 秒）→ "
                            f"已拒绝。验证样本需要连续累积，频繁重置会让洞察/归因"
                            f"永远为 0；确有需要可用 ?force=1 越过。"),
                "retry_after_s": left,
                "last_reset_ts": last,
            }
    settings = paper_trading.get_settings()
    if settings["is_running"]:
        paper_trading.set_settings(False, settings["tracks"])
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        backup = _auto_backup_trading_data(conn)
        cleared = _clear_trading_traces(conn)
        conn.commit()
        # 大 DELETE 后主动 checkpoint(TRUNCATE) 收缩 WAL，避免后续自动 checkpoint
        # 长时间阻塞写流（重置后 database is locked 刷屏的主因）
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _QUICK_SIM_CACHE["value"] = None
    # v482: 记录本次重置时刻（冷却判定 + 事后追溯「数据从哪一刻起算」）
    try:
        os.makedirs(_state_dir, exist_ok=True)
        with open(_stamp, "w", encoding="utf-8") as _fh:
            _fh.write(f"{time.time():.3f}\n")
    except Exception:
        pass
    return {
        "ok": True,
        "message": "模拟盘已重置（彻底归零）：全部交易历史与历史新闻已删除（信号/持仓/结算/反思/归因/新闻），统计从 0 开始，界面不再出现重置前旧闻；复盘经验保留；历史数据已自动备份。",
        "backup": backup,
        "cleared": cleared,
    }


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


def _backup_download_path(filename: str) -> Optional[str]:
    """把请求的文件名解析到 BACKUP_DIR 内的安全路径（防路径穿越），不存在返回 None。"""
    safe = os.path.basename(filename)
    if safe != filename or not safe:
        return None
    path = os.path.join(BACKUP_DIR, safe)
    return path if os.path.isfile(path) else None


# 2026-11-17: 本条已迁到 routers/backups.py（BACKUP_DIR 仍在本文件）。


# 2026-11-17: 本条已迁到 routers/backups.py（BACKUP_DIR 仍在本文件）。


# 2026-11-17: 本条已迁到 routers/backups.py（BACKUP_DIR 仍在本文件）。


# 2026-11-17: /api/news/exports*、sources 端点已迁到 routers/news.py。
def _safe_current_price(asset: str) -> Optional[float]:
    """取价兜底：任何单品种异常都不能让整个 pairs 面板 500。"""
    try:
        return _get_current_price(asset)
    except Exception:
        return None


# ── v2026.10.26 /api/replay/positions 持仓浮盈亏取价：30s TTL ────────────────
# 原价每次请求都现打网络（XAU → OKX 3s + 新浪/东财 5s ≈ 12.5s），页面必卡。
# 30s 内的价足够算浮盈亏（页面本身 10s 轮询），且**不碰**引擎建仓/结算的取价路径。
_REPLAY_PRICE_CACHE: Dict[str, Tuple[Optional[float], float]] = {}
_REPLAY_PRICE_NEG_CACHE: Dict[str, float] = {}
_REPLAY_PRICE_TTL_S = 30.0
_REPLAY_PRICE_NEG_TTL_S = 10.0
_REPLAY_PRICE_LOCK = threading.Lock()


def _replay_current_price(asset: str, fetch: bool = True) -> Optional[float]:
    """replay 面板用的持仓现价（30s TTL 正/负缓存）。

    fetch=False → 只读缓存（只用**新鲜**的缓存价），请求路径绝不为它等网络。
    """
    key = (asset or "").upper()
    now = time.time()
    with _REPLAY_PRICE_LOCK:
        hit = _REPLAY_PRICE_CACHE.get(key)
        if hit and now - hit[1] < _REPLAY_PRICE_TTL_S:
            return hit[0]
        if not fetch:
            return None
        neg_ts = _REPLAY_PRICE_NEG_CACHE.get(key)
        if neg_ts and now - neg_ts < _REPLAY_PRICE_NEG_TTL_S:
            return None
    try:
        price = _get_current_price(asset)
    except Exception:
        price = None
    if price is not None and float(price) > 0:
        with _REPLAY_PRICE_LOCK:
            _REPLAY_PRICE_CACHE[key] = (float(price), time.time())
        return float(price)
    with _REPLAY_PRICE_LOCK:
        _REPLAY_PRICE_NEG_CACHE[key] = time.time()
    return None


# ── v2026.10.26 pairs 面板取价：缓存 + 限并发 + 硬截止 ─────────────────────
# 背景：/api/replay/positions 原来对 13 只美股**逐个串行**取价（每只 Yahoo+Polygon
# 8-11s），整个接口 ~207s；浏览器先超时断开，/replay「持仓与盈亏记录」永远停在「加载中…」。
# 教训：直接 15 线程并发会被 Yahoo 限流，反而更多品种变 UNAVAILABLE（实测 8/13 → 5/13）。
# 现在：60s 缓存 + 6 线程并发 + 10s 硬截止。
#   - 截止前取到的照常展示；取不到的按「无价」(UNAVAILABLE) 诚实展示，与取价失败同义。
#   - pairs 只是交易台行情展示，**不参与账户权益计算**（权益只用持仓浮盈亏那次取价），
#     所以这里的缓存/超时不会影响任何账务数字。
_PAIR_PRICE_CACHE: Dict[str, Tuple[Optional[float], float, Optional[float]]] = {}  # asset -> (price, ts, change24h)
_PAIR_PRICE_NEG_CACHE: Dict[str, float] = {}                        # asset -> 失败 ts
_PAIR_PRICE_LOCK = threading.Lock()
_PAIR_PRICE_TTL_S = 60.0
_PAIR_PRICE_NEG_TTL_S = 20.0
_PAIR_PRICE_WORKERS = 6
_PAIR_PRICE_DEADLINE_S = 10.0
_PAIR_CHANGE: Dict[str, Optional[float]] = {}                      # asset -> change24h（展示用）


def _pair_prices(assets: List[str], fetch: bool = True) -> Dict[str, Optional[float]]:
    """取 pairs 面板用的品种价：缓存优先 → 限并发取价 → 硬截止兜底。

    fetch=False → 只读缓存（请求路径绝不为它等网络）。
    """
    out: Dict[str, Optional[float]] = {}
    todo: List[str] = []
    now = time.time()
    with _PAIR_PRICE_LOCK:
        for asset in assets:
            hit = _PAIR_PRICE_CACHE.get(asset)
            if hit and now - hit[1] < _PAIR_PRICE_TTL_S:
                out[asset] = hit[0]
                continue
            neg_ts = _PAIR_PRICE_NEG_CACHE.get(asset)
            if neg_ts and now - neg_ts < _PAIR_PRICE_NEG_TTL_S:
                out[asset] = None
                continue
            todo.append(asset)
    if not todo or not fetch:
        for asset in todo:
            out.setdefault(asset, None)
        return out

    fetched: Dict[str, Optional[float]] = {}
    # v2026.10.26: 美股先走东财批量（一次请求 13 只，0.3s）——
    # Yahoo 对本机 IP 全 403，yfinance 每只要等 8-15s 且必然失败。
    batch = _us_stock_quotes(todo)
    if batch:
        now = time.time()
        with _PAIR_PRICE_LOCK:
            for symbol, quote in batch.items():
                fetched[symbol] = quote["price"]
                _PAIR_PRICE_CACHE[symbol] = (quote["price"], now, quote.get("change24h"))
                _PAIR_CHANGE[symbol] = quote.get("change24h")

    rest = [asset for asset in todo if asset not in fetched]
    if rest:
        pool = ThreadPoolExecutor(max_workers=min(_PAIR_PRICE_WORKERS, len(rest)))
        try:
            futures = {pool.submit(_safe_current_price, asset): asset for asset in rest}
            done, _pending = futures_wait(futures, timeout=_PAIR_PRICE_DEADLINE_S)
            for fut in done:
                asset = futures[fut]
                try:
                    fetched[asset] = fut.result()
                except Exception:
                    fetched[asset] = None
        finally:
            # wait=False：不能等所有线程跑完，否则硬截止形同虚设
            pool.shutdown(wait=False)

    now = time.time()
    with _PAIR_PRICE_LOCK:
        for asset in todo:
            price = fetched.get(asset)
            if price is not None and price > 0:
                # v2026.10.26: 缓存带上涨跌幅（东财批量才有），展示面板直接用
                _PAIR_PRICE_CACHE[asset] = (price, now, _PAIR_CHANGE.get(asset))
                out[asset] = price
            elif asset in fetched:
                # 明确取价失败 → 短负缓存，避免每次请求都重打 Yahoo
                _PAIR_PRICE_NEG_CACHE[asset] = now
                out[asset] = None
            else:
                # 超时未回：本次按无价展示，下次重试（不写负缓存，避免一次抖动锁死 20s）
                out[asset] = None
    return out


# ── v2026.10.26 pairs 面板品种表（赛道 → 品种/合约）─────────────────────────
# crypto 三品种固定来自行情快照 market_feeds.ASSETS(BTC/ETH/SOL)，不重复走网络。
_PAIR_SYMBOLS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "crypto": (("BTC", "BTC/USDT"), ("ETH", "ETH/USDT"), ("SOL", "SOL/USDT")),
    "gold": (("XAU", "XAU/USD"),),
    "oil": (("WTI", "WTI/USD"),),
    "stocks": (
        ("NVDA", "NVDA/USDT"), ("TSLA", "TSLA/USDT"), ("AAPL", "AAPL/USDT"),
        ("MSTR", "MSTR/USDT"), ("MSFT", "MSFT/USDT"), ("AMZN", "AMZN/USDT"),
        ("GOOGL", "GOOGL/USDT"), ("COIN", "COIN/USDT"), ("PLTR", "PLTR/USDT"),
        ("META", "META/USDT"), ("SMCI", "SMCI/USDT"), ("MARA", "MARA/USDT"),
        ("RIOT", "RIOT/USDT"),
    ),
}
_PAIR_CRYPTO_ASSETS = ("BTC", "ETH", "SOL")
_PAIR_STOCK_ASSETS = tuple(asset for asset, _s in _PAIR_SYMBOLS.get("stocks", ()))

# ── v2026.10.26 pairs 面板美股行情：东财批量接口 ────────────────────────────
# 根因（实测 2026.10.26）：Yahoo v8/chart 与 yfinance 对本机 IP **全部 403 /
# YFRateLimitError**（Yahoo 返回 <html lang="zh"> 封锁页），13 只美股一条都取不到
# → pairs 面板 0/13 LIVE，而且每次还要等 yfinance 的 30d/1y 历史重试（8-15s/只）。
# 东财批量接口一次请求拿全部 13 只（实测 0.27s，带涨跌幅），国内直连不受 Yahoo 限流影响。
# ⚠️ 仅用于**展示**面板，不参与账户权益计算（权益只用持仓浮盈亏那次取价）。
_EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_EASTMONEY_MARKET_IDS = ("105", "106")   # 105=纳斯达克 106=纽交所
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _us_stock_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """一次请求拿多只美股现价 + 涨跌幅（东财 ulist.np 批量）。失败返回 {}。"""
    wanted = [str(sym).upper() for sym in symbols if str(sym).upper() in _PAIR_STOCK_ASSETS]
    if not wanted:
        return {}
    secids = [f"{mkt}.{sym}" for sym in wanted for mkt in _EASTMONEY_MARKET_IDS]
    url = (
        f"{_EASTMONEY_QUOTE_URL}?fltt=2&invt=2&fields=f12,f13,f14,f2,f3"
        f"&secids={','.join(secids)}"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        body = json.loads(_NO_PROXY_OPENER.open(req, timeout=6).read().decode("utf-8"))
    except Exception as exc:
        print(f"[PAIRS] 东财美股批量取价失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return {}
    rows = ((body or {}).get("data") or {}).get("diff") or []
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("f12") or "").upper()
        if not symbol or symbol not in wanted:
            continue
        try:
            price = float(row.get("f2"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        try:
            change = float(row.get("f3"))
        except (TypeError, ValueError):
            change = None
        out[symbol] = {"price": price, "change24h": change, "name": row.get("f14")}
    return out


def _pair_missing_assets(
    settings: Dict[str, Any],
    exclude: Optional[Set[str]] = None,
) -> List[str]:
    """pairs 面板里**需要走网络**取价的品种。

    排除：行情快照固定覆盖的 crypto 三品种、已被持仓取价覆盖的品种（exclude）。
    这样 pairs 取价可以和行情快照/持仓取价并发，不用等快照先回来。
    """
    skip = {str(item).upper() for item in (exclude or set())}
    missing: List[str] = []
    for track in settings.get("tracks", []):
        for asset, _symbol in _PAIR_SYMBOLS.get(track, ()):
            if asset in skip or asset in _PAIR_CRYPTO_ASSETS or asset in missing:
                continue
            missing.append(asset)
    return missing


def _replay_pairs(
    settings: Dict[str, Any],
    market: Optional[Dict[str, Any]],
    known_prices: Optional[Dict[str, float]] = None,
    fetched: Optional[Dict[str, Optional[float]]] = None,
) -> List[Dict[str, Any]]:
    """组装 pairs 面板（纯本地拼装，不做网络 IO）。"""
    item_map = {
        str(item.get("asset") or "").upper(): item
        for item in ((market or {}).get("items") or [])
    }
    known = {str(key).upper(): value for key, value in (known_prices or {}).items()}
    fetched = fetched or {}
    pairs: List[Dict[str, Any]] = []
    for track in settings.get("tracks", []):
        for asset, symbol in _PAIR_SYMBOLS.get(track, ()):
            item = item_map.get(asset)
            if item:
                price = item.get("price")
                change = item.get("change24h")
                source = item.get("source")
                source_count = int(item.get("sourceCount") or 1)
            elif asset in known:
                price = known[asset]
                change = _PAIR_CHANGE.get(asset)
                source = "fallback"
                source_count = 1 if price else 0
            else:
                price = fetched.get(asset)
                change = _PAIR_CHANGE.get(asset)
                source = "fallback"
                source_count = 1 if price else 0
            pairs.append({
                "asset": asset,
                "symbol": symbol,
                "track": track,
                "price": round(float(price), 6) if isinstance(price, (int, float)) and price > 0 else None,
                "change24h": round(float(change), 4) if change is not None else None,
                "source": source,
                "source_count": source_count,
                "status": "LIVE" if price else "UNAVAILABLE",
            })
    return pairs


def _llm_performance_sync(model_id: str) -> Dict[str, Any]:
    """当前模型表现（按 agent_model_id 归因）+ 全部已结算汇总（含模型为空的历史单）。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS settled,
                      SUM(CASE WHEN forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN forward_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                      SUM(CASE WHEN forward_pnl IS NULL OR forward_pnl = 0 THEN 1 ELSE 0 END) AS holds,
                      AVG(forward_pnl) AS avg_pnl,
                      SUM(forward_pnl) AS total_pnl
               FROM ai_decisions
               WHERE settled=1 AND entry_price>0 AND agent_model_id=?""",
            (model_id,),
        ).fetchone()
        settled = int(row["settled"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        decided = wins + losses
        accuracy = wins / decided * 100 if decided else None
        total_pnl = float(row["total_pnl"] or 0.0)
        result = {
            "model_id": model_id,
            "settled": settled,
            "wins": wins,
            "losses": losses,
            "holds": int(row["holds"] or 0),
            "accuracy_pct": round(accuracy, 2) if accuracy is not None else None,
            "avg_pnl_pct": round(float(row["avg_pnl"] or 0.0), 4) if settled else None,
            "total_pnl_pct": round(total_pnl, 4),
            "profitable": total_pnl > 0 if settled else None,
            "verdict": "样本不足，继续收集真实结算结果" if decided < 10 else ("历史结算表现为正" if total_pnl > 0 else "历史结算表现未盈利"),
            "method": "以该 LLM 已建立且真实结算的模拟交易计算，不使用模型自评",
        }
        # 全部已结算汇总（不过滤 agent_model_id——含模型为空的旧历史单，样本不遗漏）
        all_row = conn.execute(
            """SELECT COUNT(*) AS settled,
                      SUM(CASE WHEN forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN forward_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                      SUM(CASE WHEN forward_pnl IS NULL OR forward_pnl = 0 THEN 1 ELSE 0 END) AS holds,
                      AVG(forward_pnl) AS avg_pnl,
                      SUM(forward_pnl) AS total_pnl
               FROM ai_decisions
               WHERE settled=1 AND entry_price>0""",
        ).fetchone()
        a_settled = int(all_row["settled"] or 0)
        a_wins = int(all_row["wins"] or 0)
        a_losses = int(all_row["losses"] or 0)
        a_decided = a_wins + a_losses
        result["all_models"] = {
            "settled": a_settled,
            "wins": a_wins,
            "losses": a_losses,
            "holds": int(all_row["holds"] or 0),
            "accuracy_pct": round(a_wins / a_decided * 100, 2) if a_decided else None,
            "avg_pnl_pct": round(float(all_row["avg_pnl"] or 0.0), 4) if a_settled else None,
            "total_pnl_pct": round(float(all_row["total_pnl"] or 0.0), 4),
            "note": "全部已结算汇总（含历史未记录模型字段的单）",
        }
        return result
    finally:
        conn.close()


def _run_feedback_sync(run_id: Optional[int]) -> Dict[str, Any]:
    if not run_id:
        return {"stage": "STOPPED", "message": "模拟操盘未启动", "evaluated": 0, "passed": 0, "rejected": 0, "latest": []}
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT started_at FROM paper_trading_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return {"stage": "STOPPED", "message": "运行记录不存在", "evaluated": 0, "passed": 0, "rejected": 0, "latest": []}
        rows = conn.execute(
            """SELECT id, target_asset, suggested_action, evidence_confidence,
                      evidence_action, trade_gate_reason, entry_price, created_at
               FROM ai_decisions
               WHERE created_at>=? AND paper_trading_run_id=?
               ORDER BY id DESC LIMIT 20""",
            (run["started_at"], run_id),
        ).fetchall()
        latest = [dict(row) for row in rows]
        # 累计统计（运行以来全部信号，不受 LIMIT 20 窗口限制）：
        # 检测=收到信号总数，通过=闸门放行累计，拒绝=未通过累计。
        totals = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN evidence_confidence>=?
                                    OR trade_gate_reason LIKE '证据充分%'
                               THEN 1 ELSE 0 END) AS passed
               FROM ai_decisions
               WHERE created_at>=? AND paper_trading_run_id=?""",
            (config.EVIDENCE_CONFIDENCE_GATE, run["started_at"], run_id),
        ).fetchone()
        evaluated = int(totals["total"] or 0)
        passed = int(totals["passed"] or 0)
        rejected = evaluated - passed
        # 最近快照的闸门结果：仅用于当前状态提示（stage/message），不影响累计数字
        snap_passed = sum(
            1 for item in latest
            if (item["evidence_confidence"] or 0) >= config.EVIDENCE_CONFIDENCE_GATE
            or str(item["trade_gate_reason"] or "").startswith("证据充分")
        )
        snap_rejected = len(latest) - snap_passed
        # "已建仓"按最近快照的真实入场记录判定（证据通过 ≠ 真建仓：行情价取不到时不建仓）
        executed = sum(
            1 for item in latest
            if item["entry_price"] and float(item["entry_price"] or 0) > 0
        )
        stage = "POSITION_OPEN" if executed else ("SIGNAL_REJECTED" if snap_rejected else "WAITING_NEWS")
        message = (
            "已自动建仓并跟踪实时盈亏" if executed
            else "已收到信号，但未通过 LLM 多证据与策略闸门" if snap_rejected
            else "运行正常，正在等待所选赛道的新新闻信号"
        )
        return {"stage": stage, "message": message, "evaluated": evaluated, "passed": passed, "rejected": rejected, "executed": executed, "latest": latest[:5]}
    finally:
        conn.close()


# ── v2026.10.26 /api/replay/positions：后台刷新 + 请求侧只读快照 ──────────────
# 根因（实测）：一次请求要打 3 路外部行情 —— 持仓现价(XAU：OKX 3s + 新浪/东财 5s，
#   12-25s) + 行情快照(4 个交易所源 2-28s) + 13 只美股 pairs(yfinance 8-15s/只)，
#   单次 25-45s；而前端每 10s 轮询且 usePollRefresh **没有 in-flight 守卫**，
#   请求会重叠堆积（最多 4 个并发）→ 机器 load 102/4 核 → 越刷越慢，永远转圈。
# 改法：重活丢后台线程按 30s 周期刷新，请求侧只读快照（~ms 返回）。
# 诚实性：快照带 updated_at_ms / snapshot_age_ms；快照未就绪时先返回「无网络快版」，
#   拿不到价的持仓浮盈亏留空（unpriced_positions 计数），**不用陈旧价冒充实时价**。
_REPLAY_SNAPSHOTS: Dict[int, Tuple[Dict[str, Any], float]] = {}   # limit -> (payload, ts)
_REPLAY_WANTED_LIMITS: Set[int] = set()
_REPLAY_LOCK = threading.Lock()
_REPLAY_REFRESH_INTERVAL_S = 30.0
_REPLAY_REFRESH_MIN_GAP_S = 10.0
_REPLAY_SNAPSHOT_MAX_AGE_S = 300.0
_REPLAY_REFRESH_THREAD: Optional[threading.Thread] = None
# 后台刷新算出来的三个面板数据（反馈 / LLM 表现 / 快速模拟）——
# 快版直接复用，避免请求线程重复算（quick_sim.snapshot() 要扫全量已结算单，很重）。
_REPLAY_PANEL_CACHE: Dict[str, Any] = {
    "strategy_feedback": None,
    "llm_performance": None,
    "quick_sim": None,
}


def _replay_want_limit(limit: int) -> None:
    with _REPLAY_LOCK:
        _REPLAY_WANTED_LIMITS.add(int(limit))
        while len(_REPLAY_WANTED_LIMITS) > 4:
            _REPLAY_WANTED_LIMITS.pop()


def _replay_get_snapshot(limit: int) -> Optional[Dict[str, Any]]:
    with _REPLAY_LOCK:
        hit = _REPLAY_SNAPSHOTS.get(int(limit))
    if not hit:
        return None
    payload, ts = hit
    if time.time() - ts > _REPLAY_SNAPSHOT_MAX_AGE_S:
        return None
    out = dict(payload)
    out["snapshot_age_ms"] = int((time.time() - ts) * 1000)
    return out


def _replay_store_snapshot(limit: int, payload: Dict[str, Any]) -> None:
    with _REPLAY_LOCK:
        _REPLAY_SNAPSHOTS[int(limit)] = (payload, time.time())


def _replay_market_cache_peek() -> Dict[str, Any]:
    """只读行情快照缓存，不触发网络。"""
    value = _MARKET_CACHE.get("value")
    return value if isinstance(value, dict) else {}


async def _replay_fetch_market_context(
    assets: List[str],
    trading_settings: Dict[str, Any],
    fetch: bool,
) -> tuple[Dict[str, Any], Dict[str, float], Dict[str, Optional[float]]]:
    """取 (行情快照, 持仓现价, pairs 补充价)。

    fetch=False → 全走缓存，请求路径零等待。
    fetch=True  → 三路并发（互不依赖），网络 IO 全部发生在后台刷新里。
    """
    if not fetch:
        market = _replay_market_cache_peek()
        prices = {}
        for asset in assets:
            price = _replay_current_price(asset, fetch=False)
            if price is not None:
                prices[asset] = price
        fetched = _pair_prices(
            _pair_missing_assets(trading_settings, exclude=set(assets)), fetch=False,
        )
        return market, prices, fetched

    # 2026-11-17: 复盘回放允许旧值（后台刷新），优先保证回放不被行情抓取拖死
    market_future = asyncio.to_thread(_cached_market_prices, True)
    price_future = asyncio.gather(
        *(asyncio.to_thread(_replay_current_price, asset) for asset in assets),
        return_exceptions=True,
    )
    pair_missing = _pair_missing_assets(trading_settings, exclude=set(assets))
    pair_future = asyncio.to_thread(_pair_prices, pair_missing)

    values = await price_future
    prices = {
        asset: value for asset, value in zip(assets, values)
        if isinstance(value, (int, float)) and value > 0
    }
    try:
        market = await market_future
    except Exception as exc:
        print(f"[REPLAY] 行情快照失败(忽略): {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        market = {}
    fetched = await pair_future
    # 行情快照没覆盖到的 crypto（快照整体失败）→ 补一次，仍走 pairs 的缓存/截止约束
    snapshot_assets = {str(item.get("asset") or "").upper() for item in (market or {}).get("items") or []}
    enabled_tracks = {str(track) for track in trading_settings.get("tracks", [])}
    crypto_gap = [
        asset for asset in _PAIR_CRYPTO_ASSETS
        if "crypto" in enabled_tracks
        and asset not in snapshot_assets
        and asset not in prices
    ]
    if crypto_gap:
        fetched.update(await asyncio.to_thread(_pair_prices, crypto_gap))
    return market, prices, fetched


async def _replay_collect_context(limit: int) -> tuple:
    """一次刷新里需要的全部上下文：DB 信号 + 三路取价 + 三个面板数据。"""
    trading_settings = paper_trading.get_settings()
    current = strategy_store.get_current_strategy()
    news_settings = news_sources.get_settings()
    signals = await get_replay_signals(settled=-1, limit=limit)
    assets = sorted({str(item["asset"]).upper() for item in signals if not item["settled"]})
    market, prices, fetched = await _replay_fetch_market_context(assets, trading_settings, fetch=True)
    feedback = await asyncio.to_thread(_run_feedback_sync, trading_settings.get("active_run_id"))
    model_id = str((trading_settings.get("active_run") or {}).get("model_id") or config.get_selected_ai_model_id())
    llm_performance = await asyncio.to_thread(_llm_performance_sync, model_id)
    quick_evaluation = await asyncio.to_thread(_cached_quick_sim)
    with _REPLAY_LOCK:
        _REPLAY_PANEL_CACHE["strategy_feedback"] = feedback
        _REPLAY_PANEL_CACHE["llm_performance"] = llm_performance
        _REPLAY_PANEL_CACHE["quick_sim"] = quick_evaluation
    return signals, current, trading_settings, news_settings, market, prices, fetched, feedback, llm_performance, quick_evaluation


async def _replay_refresh_once(limits: List[int]) -> None:
    """一次后台刷新：DB 信号 + 三路网络取价 → 组装各 limit 的快照。"""
    for limit in limits:
        (signals, current, trading_settings, news_settings, market, prices,
         fetched, feedback, llm_performance, quick_evaluation) = await _replay_collect_context(limit)
        payload = _replay_assemble(
            signals, current, trading_settings, news_settings, market, prices,
            fetched, feedback, llm_performance, quick_evaluation,
        )
        payload["snapshot_ready"] = True
        _replay_store_snapshot(limit, payload)


def _replay_refresh_loop() -> None:
    """后台刷新线程：按 30s 周期把快照刷新（慢刷新也不重叠、不压垮机器）。"""
    while True:
        started = time.monotonic()
        try:
            with _REPLAY_LOCK:
                limits = sorted(_REPLAY_WANTED_LIMITS) or [200]
            asyncio.run(_replay_refresh_once(limits))
        except RuntimeError as exc:
            # v321.3 P2: 进程退出时 daemon 线程还在跑, asyncio.run 的 loop 已关闭
            # → "cannot schedule new futures after shutdown"。这是**退出噪音**,
            # 不是业务故障 → 直接结束循环, 不再重试、不再刷日志。
            if "shutdown" in str(exc).lower():
                break
            print(f"[REPLAY] 后台刷新失败(忽略): {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        except Exception as exc:
            print(f"[REPLAY] 后台刷新失败(忽略): {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(_REPLAY_REFRESH_MIN_GAP_S, _REPLAY_REFRESH_INTERVAL_S - elapsed))


def _replay_ensure_refresher() -> None:
    global _REPLAY_REFRESH_THREAD
    with _REPLAY_LOCK:
        if _REPLAY_REFRESH_THREAD is not None and _REPLAY_REFRESH_THREAD.is_alive():
            return
        _REPLAY_REFRESH_THREAD = threading.Thread(
            target=_replay_refresh_loop, daemon=True, name="replay-positions-refresh",
        )
        _REPLAY_REFRESH_THREAD.start()


async def _replay_build_fast(limit: int) -> Dict[str, Any]:
    """快照未就绪时的「无网络快版」：DB 持仓 + 缓存行情，浮盈亏拿不到就留空。

    只做「一次 DB 读 + 本地拼装」，三个面板数据直接复用后台刷新算好的结果
    （自己重算 quick_sim.snapshot() 太重，会把请求线程拖到几十秒）。
    """
    _t0 = time.monotonic()
    trading_settings = paper_trading.get_settings()
    current = strategy_store.get_current_strategy()
    news_settings = news_sources.get_settings()
    signals = await get_replay_signals(settled=-1, limit=limit)
    _t_db = time.monotonic() - _t0
    assets = sorted({str(item["asset"]).upper() for item in signals if not item["settled"]})
    market, prices, fetched = await _replay_fetch_market_context(assets, trading_settings, fetch=False)
    with _REPLAY_LOCK:
        panels = dict(_REPLAY_PANEL_CACHE)
    payload = _replay_assemble(
        signals, current, trading_settings, news_settings, market, prices,
        fetched, panels.get("strategy_feedback"), panels.get("llm_performance"), panels.get("quick_sim"),
    )
    payload["snapshot_ready"] = False
    payload["snapshot_age_ms"] = None
    _total = time.monotonic() - _t0
    if _total > 1.0:
        print(f"[REPLAY] 快版耗时 {_total:.2f}s（DB {_t_db:.2f}s）", flush=True)
    return payload


def _replay_assemble(
    signals: List[Dict[str, Any]],
    current: Dict[str, Any],
    trading_settings: Dict[str, Any],
    news_settings: Any,
    market: Dict[str, Any],
    prices: Dict[str, float],
    fetched: Dict[str, Optional[float]],
    feedback: Any,
    llm_performance: Any,
    quick_evaluation: Any,
) -> Dict[str, Any]:
    """纯本地拼装（无网络 IO）—— 后台刷新和「无网络快版」共用。"""
    pairs = _replay_pairs(trading_settings, market, prices, fetched)
    positions = [
        _paper_metrics(
            item,
            float(item["exit_price"]) if item["settled"] and item.get("exit_price") else prices.get(str(item["asset"]).upper()),
        )
        for item in signals
    ]
    realized = sum(
        float(item.get("current_pnl_usdt") or 0.0)
        for item in positions if item["settled"]
    )
    tracking = [item for item in positions if not item["settled"]]
    unrealized_values = [item.get("current_pnl_usdt") for item in tracking]
    unpriced = sum(value is None for value in unrealized_values)
    # EXEMPT-HONESTY: 未实现 PnL 求和 — DB NULL 在数学求和里等同 0 是行业标准语义,
    # 真正"无法定价"的数量已单独放在 unpriced 字段(诚实暴露给前端)
    unrealized = sum(float(value or 0.0) for value in unrealized_values)

    def _position_margin(item: Dict[str, Any]) -> float:
        params = item.get("strategy_params") or strategy_store.default_params()
        notional = float(params.get("notional_usdt") or 0.0)
        leverage = float(params.get("leverage") or 1.0)
        # 保证金占用 = 名义本金 / 杠杆
        return notional / max(leverage, 1.0)

    used_margin = sum(_position_margin(item) for item in tracking)
    initial = config.PAPER_INITIAL_EQUITY_USDT
    equity = initial + realized + unrealized
    return {
        "current_strategy": current,
        "trading_settings": trading_settings,
        "news_settings": news_settings,
        "pairs": pairs,
        "market_status": market.get("status", "unavailable"),
        "market_sources": market.get("sources", {}),
        "strategy_feedback": feedback,
        "llm_performance": llm_performance,
        "quick_sim": quick_evaluation,
        "positions": positions,
        "account": {
            "initial_equity_usdt": round(initial, 4),
            "realized_pnl_usdt": round(realized, 4),
            "unrealized_pnl_usdt": round(unrealized, 4) if not unpriced else None,
            "current_equity_usdt": round(equity, 4) if not unpriced else None,
            "used_margin_usdt": round(used_margin, 4),
            "available_equity_usdt": round(equity - used_margin, 4) if not unpriced else None,
            "unpriced_positions": unpriced,
        },
        "updated_at_ms": int(time.time() * 1000),
        "is_paper_trading": True,
        "pricing_note": "实时浮盈亏按当前行情估算，未计手续费、滑点和资金费率。",
    }


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


_BINANCE_KLINE_CACHE: Dict[str, Dict[str, Any]] = {}
_BINANCE_KLINE_TTL_S = 5.0
_JIN10_KLINE_TTL_S = 30.0   # 金十 1m 级数据：缓存 30s，防高频打爆 MCP 配额


# 资产 → 真实数据源路由（见 quote_sources.ASSET_ROUTE，单一事实来源）：
#   加密 BTC/ETH/SOL → 币安永续 fapi；黄金 XAU/GOLD/XAG → OKX 永续；
#   原油 WTI/OIL、美股 → 无免费永续合约（实时价走国内现货 / Yahoo）。
# 币安 fapi 只上市加密币，XAUUSDT/CLUSDT/NVDAUSDT 等均不存在 —— 此前把它们硬编码到
# 币安 fapi 导致请求必 400，再被 mock/兜底造假数据，现已拆掉（缺真实 K 线 → 行情缺失）。
_BINANCE_BASE = {"fapi": "https://fapi.binance.com/fapi/v1"}


# ── 币安 fapi 熔断（仿 OKX 熔断模式）：fapi 国内常被 GFW 阻断，连续失败后短路 ──
# 短路时直接返回 None，让 _fetch_binance_klines 走 data-api.binance.vision spot 镜像兜底（国内稳定）。
# 根因诊断：fapi.binance.com:443 频繁 SSL/TLS 失败（GFW SNI 阻断），每次浪费 3s+ 才超时。
_binance_breaker_lock = threading.Lock()
_binance_fapi_fail_streak = 0
_binance_fapi_cooldown_until = 0.0

_BINANCE_FAPI_BREAKER_AFTER = 2           # 连续失败几次后打开熔断
_BINANCE_FAPI_BREAKER_COOLDOWN_S = 60.0   # 熔断冷却时长（秒）


def _binance_kline_session(market: str) -> Any:
    """币安合约请求会话（fapi 走 env HTTP_PROXY，与模拟盘交易/实时价同一通道）。"""
    import requests as _req
    s = _req.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _aggregate_bars(bars_1m: List[Dict[str, Any]], period_sec: int) -> List[Dict[str, Any]]:
    """把连续 1m bar 按 period_sec（900/3600/14400）聚合为粗周期 OHLCV。

    bar 的 time 为秒级；按 time // period_sec 归桶，桶内开=首open、收=末close、高低=极值、量=累加。
    不完整桶也保留（最后一根未走完），保持与真实行情一致性。
    """
    if not bars_1m:
        return []
    out: Dict[int, List[Dict[str, Any]]] = {}
    for b in bars_1m:
        bucket = int(b["time"]) // period_sec
        out.setdefault(bucket, []).append(b)
    agg: List[Dict[str, Any]] = []
    for bucket in sorted(out):
        grp = out[bucket]
        agg.append({
            "time": bucket * period_sec,
            "open": grp[0]["open"],
            "high": max(g["high"] for g in grp),
            "low": min(g["low"] for g in grp),
            "close": grp[-1]["close"],
            "volume": sum(int(g.get("volume") or 0) for g in grp),
        })
    return agg


# 海力士 cap（v??）：股票类（Yahoo Finance 兜底）K 线统一入口
#   - 把 Yahoo v8 chart 的 [ts_ms, o, h, l, c, v] 形态转成与币安/OKX 一致的 {time, open, ...} dict 列表，
#     让 _history_sync 的存储/异常扫描/打点逻辑复用，不为股票另写一遍。
#   - Yahoo 限速+熔断由 quote_sources 内部处理；失败返回 []，调用方标"Yahoo Finance 不可用"。
# v231: 改走 multi_source_fetcher — yahoo 失败时自动切 yfinance / asia 兜底，
#   把"数据缺失"这个借口从反思系统里彻底拔掉。
def _fetch_yahoo_history_bars(asset: str, interval: str, limit: int) -> List[Dict[str, Any]]:
    """股票类（NVDA / TSLA / SKHYNIX / US_HYNIX / MU ...）K 线。

    v231 起走 multi_source_fetcher.fetch_stock_history_cached：
      yahoo → yfinance → asia 三级 fallback，每级失败都记 feed_health 表。
      全部失败才返回 [] — 调用方此时才能合理标"数据缺失"。

    返回 bars 形态与 _fetch_binance_klines 一致：[{time: 秒, open, high, low, close, volume}, ...]。
    """
    asset = (asset or "").upper()
    try:
        import multi_source_fetcher as _msf
        rows = _msf.fetch_stock_history_cached(asset, interval=interval, limit=limit)
    except Exception:
        return []
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            ts_ms, o, h, lo, c, v = r
        except (ValueError, TypeError):
            continue
        if c is None or o is None or h is None or lo is None or c <= 0:
            continue
        out.append({
            "time": int(int(ts_ms) // 1000),
            "open": round(float(o), 4),
            "high": round(float(h), 4),
            "low": round(float(lo), 4),
            "close": round(float(c), 4),
            "volume": int(float(v)) if v is not None else 0,
        })
    return out


# 海力士 cap（v??）：movers 板块用的"股票最新价 + 24h 涨跌%"读取器。
#   复用 quote_sources 的 Yahoo 数据展（5d 1d bar → 取末根收盘 / 24h 前 bar 收盘 → 百分比）。
#   Yahoo chart 直接提供 24h 价格区间，无需额外计算；失败返回 (None, None) 让前端降级。
#   进程内缓存 30s（避免每个 movers 拉取周期都打 Yahoo）。
# v231: 改走 multi_source_fetcher — yahoo 失败时自动切 yfinance / asia 兜底，
#   把"数据缺失"这个借口从反思系统里彻底拔掉。
def _yahoo_price_change24h(asset: str) -> Tuple[Optional[float], Optional[float]]:
    """从 Yahoo Finance v8 chart 取股票最新价 + 24h 涨跌%。失败返回 (None, None)。

    v231 起走 multi_source_fetcher.fetch_stock_price_cached：
      yahoo → yfinance → asia (→ equity for 美股) 多级 fallback。
      全部失败才返回 (None, None) — 调用方才能合理标"数据缺失"。
    """
    asset = (asset or "").upper()
    try:
        import multi_source_fetcher as _msf
        price, prev = _msf.fetch_stock_price_cached(asset)
    except Exception:
        return None, None
    if price is None or price <= 0:
        return None, None
    if prev is None or prev <= 0 or prev == price:
        # prev_close 拿不到或等于 price — 让前端按 0% 处理（不假装涨跌幅）
        return (price, None)
    chg_pct = (price / prev - 1.0) * 100.0
    return (price, chg_pct)


def _jin10_klines_bars(asset: str, interval: str) -> Optional[tuple]:
    """金十 MCP 分钟 K 线兜底：XAU/WTI/XAG 走 mcp.jin10.com（国内直连，不依赖 VPN）。

    金十 get_kline 仅提供 1m 级（近 100 根 / 24h 内）：
    - 1m 直接返回；
    - 15m/1h/4h 用 1m 聚合（数据量有限，仅作近端参考）；
    - 1d 金十不支持 → None（调用方回退库内历史）。
    返回 (bars, last_bar)，与币安/OKX bars 同构（time 秒级、volume 为成交量）。
    进程内缓存 30s（分钟级数据无需秒级刷新，且避免高频打爆金十 MCP 配额）。
    """
    code = jin10_mcp.JIN10_KLINE_CODE.get((asset or "").upper())
    if not code:
        return None
    if interval not in ("1m", "3m", "5m", "10m", "15m", "1h", "4h"):
        return None
    key = f"jin10:{code}:{interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _JIN10_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    # v2026.10.08: 硬截断 3s（jin10_mcp 内置 token 轮换，HTTP 20s timeout，3 token = 60s 累计）。
    # 用 ThreadPoolExecutor(1) + shutdown(wait=False) 真正立刻返回。
    import concurrent.futures as _cf
    def _do():
        try:
            return jin10_mcp.get_kline(code, count=100)
        except Exception:
            return None
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_do)
        try:
            payload = fut.result(timeout=3.0)
        except _cf.TimeoutError:
            return None
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False)
    if payload is None:
        return None
    inner = payload.get("data") if payload.get("status") == "ok" else None
    kls = (inner or {}).get("klines") or []
    if not kls:
        return None
    bars_1m: List[Dict[str, Any]] = []
    for k in kls:
        try:
            o = float(k["open"]); h = float(k["high"])
            lo = float(k["low"]); c = float(k["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if min(o, h, lo, c) <= 0:
            continue
        bars_1m.append({
            "time": int(k["time"]),
            "open": round(o, 4), "high": round(h, 4),
            "low": round(lo, 4), "close": round(c, 4),
            "volume": int(k.get("volume") or 0),
        })
    if not bars_1m:
        return None
    bars_1m.sort(key=lambda b: b["time"])
    if interval == "1m":
        bars = bars_1m
    else:
        bars = _aggregate_bars(bars_1m, {"3m": 180, "5m": 300, "10m": 600, "15m": 900, "1h": 3600, "4h": 14400}[interval])
    if not bars:
        return None
    _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": bars[-1]}
    return bars, bars[-1]


def _fetch_binance_klines(asset: str, interval: str) -> Optional[tuple]:
    """图表 K 线统一入口：按资产路由真实数据源（绝不 mock 凑数）。

    - 加密 BTC/ETH/SOL → 币安永续 fapi（走代理）→ 现货镜像直连 → Bybit 永续
                          → Bitstamp 现货 → 返回 None（caller 退库内历史）
    - 黄金 XAU/GOLD/XAG → OKX 永续（国内直连）→ 失败金十 MCP 1m 兜底
    - 原油 WTI/OIL → 金十 MCP 1m（USOIL，国内直连；无 OKX/Yahoo 免费源）
    - 美股 NVDA/TSLA/.../SKHYNIX/US_HYNIX/MU → Yahoo Finance v8 chart
                            （multi_source_fetcher 内部 yahoo→yfinance→asia 兜底）

    v192：BTC/ETH/SOL 加 Bybit + Bitstamp 多源链，规避 Binance 全家桶被 GFW
    阻断时只能退库内历史的问题（任意源成功 → 返回实时 K 线）。
    v?? 海力士 cap：yahoo 市场（美股/海力士）也接入真实 K 线，归因页 /review
    的 US_HYNIX/SKHYNIX/MU/NVDA/... 不再退化为「模拟反推」。
    返回 (bars, last_bar)。
    """
    r = quote_sources.route(asset)
    if not r:
        return None
    if r["market"] == "okx_swap":
        okx = _okx_klines_bars(r["symbol"], interval)
        if okx is not None:
            return okx
        # OKX 被墙（SSL_ERROR_SYSCALL）→ 金十 MCP 兜底（XAU/XAG 现货 1m 级，国内直连）
        return _jin10_klines_bars(asset, interval)
    if r["market"] == "binance_fapi":
        sym = r["symbol"]
        alt = _BINANCE_ALT_SYMBOLS.get(sym) or {}
        bybit_sym = alt.get("bybit")
        bs_sym = alt.get("bitstamp")

        # v2026.10.08: 4 源并行（每源 3s timeout）→ 总耗时 3s（以前 4×3=12s 串行）。
        # 谁先成功就用谁；全部超时/失败才 None。
        import concurrent.futures as _cf
        attempts = [
            ("fapi",     lambda: _binance_fapi_klines(sym, interval)),
            ("spot",     lambda: _binance_spot_klines(sym, interval)),
            ("bybit",    (lambda: _bybit_klines_bars(bybit_sym, interval)) if bybit_sym else (lambda: None)),
            ("bitstamp", (lambda: _bitstamp_klines_bars(bs_sym, interval)) if bs_sym else (lambda: None)),
        ]
        ex = _cf.ThreadPoolExecutor(max_workers=len(attempts))
        try:
            futs = {ex.submit(fn): name for name, fn in attempts}
            for fut in _cf.as_completed(futs, timeout=3.5):
                try:
                    res = fut.result()
                    if res is not None:
                        return res
                except Exception:
                    continue
        except _cf.TimeoutError:
            pass
        finally:
            ex.shutdown(wait=False)
        return None
    if r["market"] == "domestic_spot":
        # WTI/OIL 无免费永续 → 金十 MCP 兜底（USOIL 现货 1m，国内直连）
        return _jin10_klines_bars(asset, interval)
    if r["market"] == "yahoo":
        # 海力士 cap（v??）：股票类（Yahoo Finance 兜底）— 多源链 yahoo→yfinance→asia
        # 由 multi_source_fetcher.fetch_stock_history_cached 内部完成；这里只负责把
        # 前端 interval(1m/15m/1h/4h/1d) 翻译成 Yahoo chart 支持的 interval，并把
        # _fetch_yahoo_history_bars 返回的 bars 转成 (bars, last_bar) 元组，
        # 与币安/OKX 同构。归因页 /review 修 US_HYNIX 实时 K 线可用。
        iv_norm = {"1m": "1m", "15m": "15m", "1h": "60m", "4h": "60m", "1d": "1d"}.get(interval, "60m")
        limit = {"1m": 240, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
        asset_up = (asset or "").upper()
        bars = _fetch_yahoo_history_bars(asset_up, iv_norm, limit)
        if bars:
            return (bars, bars[-1])
        return None
    return None


def _binance_fapi_klines(symbol: str, interval: str) -> Optional[tuple]:
    """币安永续真实 K 线（fapi，走代理；5s 缓存）。volume=quoteVolume（USDT 成交额）。

    interval 支持 1m/3m/5m/15m/1h/4h/1d（币安原生）；10m 非原生 → 上层用 5m 聚合。

    v176.5f: 加 fapi 熔断器（仿 OKX 模式）。fapi 国内常被 GFW 阻断，连续失败后短路
    60s，节省每次浪费的 3s+ 超时；短路时直接 None，让调用方走 spot 镜像（data-api.binance.vision，
    国内稳定 0.6s 直连）兜底。
    """
    global _binance_fapi_fail_streak, _binance_fapi_cooldown_until
    b_interval = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    # ── 熔断检查：冷却期内直接短路（返回 None → 调用方走 spot 镜像） ──
    with _binance_breaker_lock:
        if time.time() < _binance_fapi_cooldown_until:
            return None
    try:
        session = _binance_kline_session("fapi")
        url = f"{_BINANCE_BASE['fapi']}/klines?symbol={symbol}&interval={b_interval}&limit={limit}"
        # P1 修复: 国内网络 Binance fapi 常被 GFW 阻断(SNI/SSL 失败),15s 太长导致 dashboard
        # ticker 价格静止. 改 3s 快速失败 → 走 spot 镜像(国内稳定) → 再走 DB kline_bars 兜底.
        # 配合 _BINANCE_KLINE_TTL_S=5s 缓存, 成功请求命中缓存不再重试外网.
        resp = session.get(url, timeout=3)
        if resp.status_code != 200:
            with _binance_breaker_lock:
                _binance_fapi_fail_streak += 1
                if _binance_fapi_fail_streak >= _BINANCE_FAPI_BREAKER_AFTER:
                    _binance_fapi_cooldown_until = time.time() + _BINANCE_FAPI_BREAKER_COOLDOWN_S
                if _binance_fapi_fail_streak == _BINANCE_FAPI_BREAKER_AFTER:
                    print(
                        f"[BINANCE-FAPI-BREAKER] 连续失败 {_binance_fapi_fail_streak} 次（{symbol}），"
                        f"熔断 {_BINANCE_FAPI_BREAKER_COOLDOWN_S}s（status={resp.status_code}）",
                        flush=True,
                    )
            return None
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        bars: List[Dict[str, Any]] = []
        for k in data:
            o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": int(int(k[0]) // 1000),
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[7])),  # quoteVolume（USDT 成交额）
            })
        if not bars:
            return None
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        # ── 成功后重置熔断 ──
        with _binance_breaker_lock:
            _binance_fapi_fail_streak = 0
            _binance_fapi_cooldown_until = 0.0
        return bars, last
    except Exception as _e:
        # ── 失败计数（超时/SSL 等） ──
        with _binance_breaker_lock:
            _binance_fapi_fail_streak += 1
            if _binance_fapi_fail_streak >= _BINANCE_FAPI_BREAKER_AFTER:
                _binance_fapi_cooldown_until = time.time() + _BINANCE_FAPI_BREAKER_COOLDOWN_S
            if _binance_fapi_fail_streak == _BINANCE_FAPI_BREAKER_AFTER:
                print(
                    f"[BINANCE-FAPI-BREAKER] 连续失败 {_binance_fapi_fail_streak} 次（{symbol}），"
                    f"熔断 {_BINANCE_FAPI_BREAKER_COOLDOWN_S}s | err={type(_e).__name__}: {str(_e)[:80]}",
                    flush=True,
                )
        return None


def _binance_spot_klines(symbol: str, interval: str) -> Optional[tuple]:
    """币安现货(USDT) 真实 K 线 → 与 fapi bars 同构（time 秒级、volume=quoteVolume）。

    国内直连（data-api.binance.vision），作为永续 fapi 代理失败时的兜底。5s 缓存。
    interval 支持 1m/3m/5m/15m/1h/4h/1d（币安原生）；10m 非原生 → 上层用 5m 聚合。
    """
    b_interval = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"spot:{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False  # 强制直连，不走 env 代理
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={b_interval}&limit={limit}"
        # P1 修复: 同 _binance_fapi_klines, 国内网络 data-api.binance.vision 也偶发阻断.
        # 15s 太长, 改 3s 让前端 ticker 价格静止/加载中状态快速消失, 走 DB 兜底.
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        bars: List[Dict[str, Any]] = []
        for k in data:
            o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": int(int(k[0]) // 1000),
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[7])),  # quoteVolume（USDT 成交额）
            })
        if not bars:
            return None
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


# ── 多源 fallback 链（v192 — 国内服务器友好）────────────────────────────────────
# 国内服务器常见 GFW 阻断 Binance 全家桶（fapi/data-api/api）。在 _binance_fapi + _binance_spot
# 都连不上时，串接 Bybit（国内直连）/ Bitstamp（国际备用）作为实时兜底，最后才退到 DB。
# 任一源成功即返回，不再连环失败。OKX 已有专用路径（黄金/白银），这里只补 BTC/ETH/SOL。
_BINANCE_ALT_SYMBOLS: Dict[str, Dict[str, str]] = {
    "BTCUSDT": {"bybit": "BTCUSDT",  "bitstamp": "btcusd"},
    "ETHUSDT": {"bybit": "ETHUSDT",  "bitstamp": "ethusd"},
    "SOLUSDT": {"bybit": "SOLUSDT",  "bitstamp": "solusd"},
}


# ── v221.7 — 分页拉取（30 天 K 线 / 跨周历史）──────────────────────────────────────
# 币安 / Bybit 单次 K 线上限 1000，要拉 30d 10m（≈4320 bars）必须分页。
# 优先 Bybit（国内可直连，endTime cursor 支持好）→ 币安现货 → 库内回退。
_PAGED_HISTORY_CACHE: Dict[str, Dict[str, Any]] = {}
_PAGED_HISTORY_TTL_S = 300.0  # 5 分钟缓存：30 天拉取慢，前端反复刷新会爆
# v221.7: _history_sync 外层缓存（含 anomalies 计算结果）— 30 天 scan_anomalies ~10s
_HISTORY_SYNC_CACHE: Dict[str, Dict[str, Any]] = {}
_HISTORY_SYNC_TTL_S = 300.0  # 5 分钟缓存


def _paged_history(
    asset: str, interval: str, total_bars: int,
    end_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """v221.7: 跨多页拉取 K 线（30 天 / 跨周回溯用）。

    Args:
        asset: BTC / ETH / SOL 等
        interval: 5m / 10m / 1h / ...（10m 走 5m 聚合）
        total_bars: 目标根数（30d × 144/d for 10m = 4320）
        end_ts: 截止时间 epoch 秒（默认 = now）

    Returns:
        bars 升序列表 [{time, open, high, low, close, volume}, ...]
        单页拉取失败 → 至少返回已拉到的（best-effort）。
    """
    if total_bars <= 0:
        return []
    cache_key = f"{asset}:{interval}:{total_bars}:{end_ts or 0}"
    now = time.monotonic()
    cached = _PAGED_HISTORY_CACHE.get(cache_key)
    if cached and now - cached["ts"] < _PAGED_HISTORY_TTL_S:
        return cached["bars"]

    r = quote_sources.route(asset)
    if not r:
        return []
    src_interval = "5m" if interval == "10m" else interval
    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }.get(src_interval, 300_000)

    bars: List[Dict[str, Any]] = []
    seen_ts: set = set()

    # ── 1) Bybit（国内可直连）──
    if r["market"] == "binance_fapi":
        sym = r["symbol"]
        alt = _BINANCE_ALT_SYMBOLS.get(sym) or {}
        bybit_sym = alt.get("bybit")
        if bybit_sym:
            bars = _bybit_paged(bybit_sym, src_interval, total_bars, end_ts,
                                interval_ms, seen_ts)
    # ── 2) Bybit 失败 → 币安现货（endTime cursor） ──
    if not bars and r["market"] == "binance_fapi":
        bars = _binance_spot_paged(r["symbol"], src_interval, total_bars,
                                    end_ts, interval_ms, seen_ts)
    # ── 3) OKX / 黄金白银 —— 复用 _okx_klines_bars 单页 ──
    if not bars and r["market"] == "okx_swap":
        okx = _okx_klines_bars(r["symbol"], src_interval)
        if okx:
            bars = list(okx[0])[-total_bars:]

    if bars:
        if interval == "10m":
            bars = _aggregate_bars(bars, 600)
        try:
            _store_klines(asset, interval, bars)
        except Exception:
            pass

    _PAGED_HISTORY_CACHE[cache_key] = {"ts": now, "bars": bars}
    return bars


def _bybit_paged(
    symbol: str, interval: str, total_bars: int, end_ts: Optional[int],
    interval_ms: int, seen_ts: set,
) -> List[Dict[str, Any]]:
    """Bybit v5 分页拉取，endTime cursor 向历史回溯。"""
    b_interval = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15",
        "1h": "60", "4h": "240", "1d": "D",
    }.get(interval, "60")
    PAGE = 1000
    bars: List[Dict[str, Any]] = []
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        cursor_end = int(end_ts * 1000) if end_ts else None
        attempts = 0
        while len(bars) < total_bars and attempts < 12:
            attempts += 1
            params = (f"category=linear&symbol={symbol}"
                      f"&interval={b_interval}&limit={PAGE}")
            if cursor_end is not None:
                params += f"&end={cursor_end}"
            url = f"https://api.bybit.com/v5/market/kline?{params}"
            resp = s.get(url, timeout=8)
            if resp.status_code != 200:
                break
            payload = resp.json() or {}
            rows = (payload.get("result") or {}).get("list") or []
            if not rows:
                break
            page_added = 0
            oldest_ms = cursor_end
            for k in rows:
                try:
                    ts_ms = int(k[0])
                    o = float(k[1]); h = float(k[2])
                    lo = float(k[3]); c = float(k[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                if ts_ms in seen_ts:
                    continue
                seen_ts.add(ts_ms)
                bars.append({
                    "time": ts_ms // 1000,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(lo, 4), "close": round(c, 4),
                    "volume": int(float(k[5] or 0)),
                })
                page_added += 1
                if oldest_ms is None or ts_ms < oldest_ms:
                    oldest_ms = ts_ms
            bars.sort(key=lambda b: b["time"])
            if page_added == 0:
                break
            cursor_end = oldest_ms - interval_ms
        return bars[-total_bars:]
    except Exception:
        return bars[-total_bars:] if bars else []


def _binance_spot_paged(
    symbol: str, interval: str, total_bars: int, end_ts: Optional[int],
    interval_ms: int, seen_ts: set,
) -> List[Dict[str, Any]]:
    """Binance 现货分页拉取（data-api.binance.vision，国内直连）。"""
    b_interval = interval
    PAGE = 1000
    bars: List[Dict[str, Any]] = []
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        cursor_end = int(end_ts * 1000) if end_ts else None
        attempts = 0
        while len(bars) < total_bars and attempts < 12:
            attempts += 1
            params = f"symbol={symbol}&interval={b_interval}&limit={PAGE}"
            if cursor_end is not None:
                params += f"&endTime={cursor_end}"
            url = f"https://data-api.binance.vision/api/v3/klines?{params}"
            resp = s.get(url, timeout=8)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            page_added = 0
            oldest_ms = cursor_end
            for k in data:
                try:
                    ts_ms = int(k[0])
                    o = float(k[1]); h = float(k[2])
                    lo = float(k[3]); c = float(k[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if min(o, h, lo, c) <= 0:
                    continue
                if ts_ms in seen_ts:
                    continue
                seen_ts.add(ts_ms)
                bars.append({
                    "time": ts_ms // 1000,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(lo, 4), "close": round(c, 4),
                    "volume": int(float(k[7] or 0)),
                })
                page_added += 1
                if oldest_ms is None or ts_ms < oldest_ms:
                    oldest_ms = ts_ms
            bars.sort(key=lambda b: b["time"])
            if page_added == 0:
                break
            cursor_end = oldest_ms - interval_ms
        return bars[-total_bars:]
    except Exception:
        return bars[-total_bars:] if bars else []


def _bybit_klines_bars(symbol: str, interval: str) -> Optional[tuple]:
    """Bybit v5 线性永续 K 线（api.bybit.com，国内可直连，3s 缓存）。

    interval 支持 1/3/5/15/60/240/D（Bybit 粒度：1/3/5/15/30/60/120/240/360/720/D/W）。
    返回与 Binance bars 同构的 (bars, last)。失败 → None，调用方继续下一源。
    """
    b_interval = {"1m": "1", "3m": "3", "5m": "5", "10m": "5", "15m": "15",
                  "1h": "60", "4h": "240", "1d": "D"}.get(interval, "60")
    limit = {"1m": 200, "3m": 200, "5m": 200, "10m": 200, "15m": 200,
             "1h": 200, "4h": 200, "1d": 200}.get(interval, 200)
    key = f"bybit:{symbol}:{b_interval}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = (f"https://api.bybit.com/v5/market/kline?category=linear"
               f"&symbol={symbol}&interval={b_interval}&limit={limit}")
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            return None
        bars: List[Dict[str, Any]] = []
        for k in rows:
            try:
                o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
                ts_ms = int(k[0])
            except (TypeError, ValueError, IndexError):
                continue
            if min(o, h, lo, c) <= 0:
                continue
            bars.append({
                "time": ts_ms // 1000,
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k[5] or 0)),
            })
        if not bars:
            return None
        bars.sort(key=lambda b: b["time"])  # Bybit 默认倒序，升序输出
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


def _bitstamp_klines_bars(symbol: str, interval: str) -> Optional[tuple]:
    """Bitstamp 现货 K 线（国际备用，3s 缓存，限频紧 600 req/10min）。

    step 取值：60/180/300/900/1800/3600/7200/14400/21600/43200/86400/259200。
    返回与 Binance bars 同构的 (bars, last)。失败 → None。
    """
    step = {"1m": 60, "3m": 180, "5m": 300, "10m": 300, "15m": 900,
            "1h": 3600, "4h": 14400, "1d": 86400}.get(interval, 3600)
    limit = {"1m": 60, "3m": 60, "5m": 60, "10m": 60, "15m": 60,
             "1h": 60, "4h": 60, "1d": 60}.get(interval, 60)
    key = f"bitstamp:{symbol}:{step}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = True
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = f"https://www.bitstamp.net/api/v2/ohlc/{symbol}/?step={step}&limit={limit}"
        resp = s.get(url, timeout=3)
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        rows = (payload.get("data") or {}).get("ohlc") or []
        if not rows:
            return None
        bars: List[Dict[str, Any]] = []
        for k in rows:
            try:
                ts_str = k.get("timestamp")
                o = float(k.get("open") or 0)
                h = float(k.get("high") or 0)
                lo = float(k.get("low") or 0)
                c = float(k.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if min(o, h, lo, c) <= 0:
                continue
            try:
                ts = int(ts_str)
            except (TypeError, ValueError):
                continue
            bars.append({
                "time": ts,  # Bitstamp timestamp 已是秒级
                "open": round(o, 4), "high": round(h, 4),
                "low": round(lo, 4), "close": round(c, 4),
                "volume": int(float(k.get("volume") or 0)),
            })
        if not bars:
            return None
        bars.sort(key=lambda b: b["time"])
        last = bars[-1]
        _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
        return bars, last
    except Exception:
        return None


def _okx_klines_bars(inst_id: str, interval: str) -> Optional[tuple]:
    """OKX 永续真实 K 线 → 与币安 bars 同构（time 秒级、volume=成交量）。5s 缓存。

    interval 支持 1m/3m/5m/15m/1h/4h/1d（OKX 原生）；10m 非原生 → 上层用 5m 聚合。
    """
    bar = quote_sources.okx_bar(interval)
    # v220: bumped 5m from 288 -> 1440 to support 10m x 720 bars (~5 days) on chart
    limit = {"1m": 720, "3m": 288, "5m": 1440, "15m": 96, "1h": 48, "4h": 36, "1d": 120}.get(interval, 48)
    key = f"okx:{inst_id}:{bar}"
    now = time.monotonic()
    cached = _BINANCE_KLINE_CACHE.get(key)
    if cached and now - cached["ts"] < _BINANCE_KLINE_TTL_S:
        return cached["bars"], cached["last"]
    rows = quote_sources.fetch_okx_candles(inst_id, bar, limit)
    if not rows:
        return None
    bars: List[Dict[str, Any]] = []
    for r in rows:
        # rows: [ts_ms, open, high, low, close, volume]
        bars.append({
            "time": int(int(r[0]) // 1000),
            "open": round(r[1], 4), "high": round(r[2], 4),
            "low": round(r[3], 4), "close": round(r[4], 4),
            "volume": int(r[5]),
        })
    last = bars[-1]
    _BINANCE_KLINE_CACHE[key] = {"ts": now, "bars": bars, "last": last}
    return bars, last


def _fetch_real_klines(asset: str, entry_ts: int, interval: str = "1h") -> Optional[List[Dict[str, Any]]]:
    """按资产拉取真实 K 线（统一币安永续合约，与模拟盘成交价同一市场口径）。

    全品种（BTC/ETH/SOL/XAU/WTI/美股）都走币安永续 fapi —— K 线与模拟盘
    入场/止损/出场价同源，图上标注不再错位（此前 XAU/WTI 用 Yahoo 国际期货、
    加密用现货，与交易价(币安永续)不是同一市场导致价格对不上）。
    全失败返回 None，由调用方降级模拟 K 线。

    interval: 1m / 1h / 4h / 1d。窗口：1m→±6h、1h→±24h、4h→±72h、1d→±60 天。

    v?? 海力士 cap：解除"非 binance_fapi/okx_swap 即返回 None"的限制 — yahoo 市场
    的美股 / SKHYNIX / US_HYNIX / MU 也走真实 Yahoo Finance K 线。
    """
    asset_up = str(asset or "").upper()
    r = quote_sources.route(asset_up)
    if r is None:
        return None
    interval = interval if interval in ("1m", "15m", "1h", "4h", "1d") else "1h"
    bn = _fetch_binance_klines(asset_up, interval)
    if bn is not None:
        return bn[0]
    return None


_LIVE_PRICE_CACHE: Dict[str, Dict[str, Any]] = {}
_LIVE_PRICE_TTL_S = 2.0


def _live_price_sync(asset: str) -> Optional[float]:
    """轻量实时价：3 秒进程内缓存，避免高频轮询打爆外部价格源。"""
    key = str(asset or "").upper()
    now = time.monotonic()
    cached = _LIVE_PRICE_CACHE.get(key)
    if cached and now - cached["ts"] < _LIVE_PRICE_TTL_S:
        return cached["price"]
    price = _get_current_price(key)
    if isinstance(price, (int, float)) and price > 0:
        _LIVE_PRICE_CACHE[key] = {"price": float(price), "ts": now}
        return float(price)
    return None


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# 2026-11-17: 本条已迁到 routers/replay.py / routers/review_run.py。


# -- Strategy library + honest backtest ------------------------------------

# 2026-11-17: 策略库端点/请求模型/_strategy_or_404 已迁到 routers/strategies.py。


# 2026-11-17: BacktestBody 已随回测端点迁到 routers/backtest.py。


# 2026-11-17: OptimizeBody 已随 optimize 端点迁到 routers/backtest.py。
# 2026-11-17: 本条已迁到 routers/backtest.py。


# 2026-11-17: 本条已迁到 routers/backtest.py。


def _llm_optimize_sync(params: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    response = _agent_llm_client("optimizer").chat.completions.create(
        model=_agent_llm_model("optimizer"), temperature=0.0, max_tokens=700,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "你是本平台策略参数优化器。只输出 JSON。"
                "只能引用已给出的回测报告数字，禁止编造胜率、行情或未发生的成交。"
                "样本不足或回撤过大时必须更保守。"
                "字段：signal_threshold, notional_usdt, leverage, trailing_callback_rate, "
                "holding_horizon_minutes, min_event_strength, asset_filter, require_direct_catalyst, note。"
            )},
            {"role": "user", "content": json.dumps({"current": params, "report": {
                k: report[k] for k in (
                    "sample_size", "taken", "skipped", "wins", "losses", "winrate",
                    "total_pnl_usdt", "max_drawdown_usdt", "profit_factor",
                    "insufficient_sample", "by_asset", "params",
                ) if k in report
            }}, ensure_ascii=False)},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    note = str(result.pop("note", "") or "LLM 基于已结算回测报告给出的下一组参数")
    return {"params": strategy_store.clamp_params(result), "note": note, "mode": "llm"}


# 2026-11-17: 本条已迁到 routers/backtest.py。


# ===========================================================================
# v172 事件结构化输出层（v173+ 结构化输入/校验的可视化出口）
# ===========================================================================

import event_atom as _event_atom
import event_impact_curve as _event_impact_curve
import dim_tables as _dim_tables


# 2026-11-17: _serialize_event_atom_row 已搬进 routers/news.py（atom 三兄弟用它），
# 这里按原名导入，/api/event_atoms 等仍在用。
from routers.news import _serialize_event_atom_row


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/news.py。


# 2026-11-17: 本条已迁到 routers/news.py。


# 2026-11-17: 本条已迁到 routers/news.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/event_atoms.py。


# 2026-11-17: 本条已迁到 routers/overview.py。


# ── v173: 结构化校验真实性飞轮 ─────────────────────────────────────────
# 2026-11-17: 本条已迁到 routers/overview.py。


# 2026-11-17: 本条已迁到 routers/overview.py。


# ============================================================================
# v186 反思系统 API
# ============================================================================

# 2026-11-17: v186 经验库 8 条端点已迁到 routers/experience.py。

# 2026-11-17: v188 反思 CRUD 11 条端点已并入 routers/reflection.py。

# 2026-11-17: 市场状态/跨市场行情 9 条端点已迁到 routers/market.py。

# 2026-11-17: 运维/回测/日志 9 条端点已迁到 routers/ops.py。

# 2026-11-17: 本条已迁到 routers/indicators.py。


# 2026-11-17: 本条已迁到 routers/indicators.py。


# v188+ 免费零延迟金融数据聚合 (DXY/US10Y/白银/板块ETF/Coinglass/财报日历)
# 2026-11-17: 本条已迁到 routers/indicators.py。


# -- Entry point -----------------------------------------------------------


# ══════════════════════════════════════════════════════════════════
# v187: 多通道 LLM token 用量监控 API
# 4 个端点: by-purpose / by-key / by-call-site / timeline
# 默认查最近 24h；days 参数可调（最大 7 天）。
# ──────────────────────────────────────────────────────────────────
# 2026-11-17: v187 LLM 调用统计 5 条端点已迁到 routers/llm.py。

# ══════════════════════════════════════════════════════════════════
# v189-7: Coinglass + LSR 中转 API 数据接入 (行情衍生品多空比)
# 4 个端点 (sync def 自动跑在 threadpool):
#   - GET /api/v189/coinglass/lsr/latest     最新 N 条样本 (按 symbol/mode 过滤)
#   - GET /api/v189/coinglass/lsr/history    历史曲线 (since_ts 之后)
#   - GET /api/v189/coinglass/health         健康度 + watcher 状态 + 配额/过期
#   - GET /api/v189/coinglass/lsr/ranking    排行榜 (实时从 LSR 拉, 不落 DB)
# ──────────────────────────────────────────────────────────────────


# 2026-11-17: v189 Coinglass 4 条端点已迁到 routers/coinglass.py。

# ──────────────────────────────────────────────────────────────────────────────
# 2026-11-17: v190 实盘通道管理 9 条端点已迁到 routers/exchange.py。
# 注：原 if __name__ == "__main__": 块（带 uvicorn.run）已被移至文件末尾，
# 因为 v170-P0+ admin 端点定义在原 __main__ 之后；uvicorn.run 同步阻塞会
# 阻止后续装饰器执行，导致 admin 路由无法注册。
__main_block_moved_to_end = True


# ─────────────────────────────────────────────────────────────────────────────
# v170-P0+ admin 端点：id_reconcile（trade_uuid 全链路 + 对账 + 应急隔离）
# ─────────────────────────────────────────────────────────────────────────────

# 2026-11-17: 6 条 /api/admin/id-reconcile/* 已迁到 routers/admin.py。
# ─────────────────────────────────────────────────────────────────────────────
# 2026-11-17: /api/discovery/* 已迁到 routers/discovery.py（连带 read-state 助手）。
# ─────────────────────────────────────────────────────────────────────────────
# 2026-11-17: v230 反思深度增强 8 条端点已迁到 routers/reflection.py。
# ─────────────────────────────────────────────────────────────────────────────
# NOTE(v300-fix): __main__ 块已移到本文件末尾 (EOF)。
# 原因：此前 __main__ 位于此处，其后追加的 v300 段里还有 20 个 @app.* 装饰器，
# uvicorn.run() 同步阻塞导致这些端点在 `python api_server.py` 启动方式下
# 永远不会被注册（/api/signal-weights 等 404 的根因）。
# ══════════════════════════════════════════════════════════════════════
# v300: 反爬虫 + 访客仪表盘（默认全关，TRIDENT_ANTI_BOT=1 启用）
# ══════════════════════════════════════════════════════════════════════
# 设计原则：
#   - 不改任何已有路由、@app.* 装饰器、数据库 schema
#   - 仅在文件末尾追加 1 个 if 块，按需挂载中间件 / 注册新路由
#   - 全关时 (TRIDENT_ANTI_BOT != "1")，本块 0 副作用
# 2026-11-17: 整个 v300 反爬块（中间件 + 蜜罐 + 6 条端点 + /admin）已搬出到
# routers/antibot.py 的 mount_antibot(app, include_flat)。这里只留一行挂载调用，
# 因为中间件和蜜罐都需要 app 实例本身，不适合塞进纯 APIRouter。
from routers.antibot import mount_antibot as _mount_antibot
_mount_antibot(app, _include_flat)


# =============================================================
# 2026-11-17: v300.1 投资人 portal 6 条端点已迁到 routers/portal.py。

# ════════════════════════════════════════════════════════════════════════
# 聪明钱追踪(v285+ · 公开免鉴权 Hyperliquid Info API)
#
# 用途:/discover 新增「段 0.5 · 聪明钱」,支持把多个 Hyperliquid 钱包加进
#      watchlist,每 30s 拉 userFills + clearinghouseState,做 diff 后在
#      前端弹「刚刚开 BTC 多 0.5x」提醒。
#
# 设计取舍:
#  · 不上 WebSocket(SSE 维护成本高、且 Hyperliquid WebSocket 需要鉴权):
#    前端每 30s 轮询即可,后端 5s 内存缓存吸收瞬时洪峰。
#  · 不持久化:watchlist 在前端 localStorage;后端无状态,故障重启不丢历史。
#  · 地址校验:只允许 0x + 40 位 hex(不区分大小写),挡住 90% 的乱输入。
#  · Hyperliquid Info API 是公开免鉴权,但仍做超时 + 5xx 友好返回,不让
#    链上接口抖动把 /discover 整页拉死。
# ════════════════════════════════════════════════════════════════════════
import re as _re
# 2026-11-17: 本条已迁到 routers/smart_money.py。

# 2026-11-17: 本条已迁到 routers/smart_money.py。

# 2026-11-17: 本条已迁到 routers/smart_money.py。

# 2026-11-17: 本条已迁到 routers/smart_money.py。

# 2026-11-17: 本条已迁到 routers/smart_money.py。


# 2026-11-17: 本条已迁到 routers/smart_money.py。


# 2026-11-17: 本条已迁到 routers/smart_money.py。


# 2026-11-17: 本条已迁到 routers/smart_money.py。


from routers.portal import _MASTER_URL

if _MASTER_URL:
    print(f"[trident v300.1] ✅ master proxy ENABLED  →  {_MASTER_URL}")
else:
    print(f"[trident v300.1] ⚠️  TRIDENT_MASTER_URL 没设 — Fly.io 镜像会返回 503")
    print(f"[trident v300.1]    设: fly secrets set TRIDENT_MASTER_URL=https://xxx.trycloudflare.com")


# =============================================================================
# v20251014: signal_weight API (new)
#   GET /api/signal-weights?asset=BTC&hours=24&limit=50
#   GET /api/signal-weights/{decision_id}
# =============================================================================


# 2026-11-17: 本条已迁到 routers/signal_weights.py。


# 2026-11-17: 本条已迁到 routers/signal_weights.py。


# 2026-11-17: 本条已迁到 routers/signal_weights.py。


# 2026-11-17: 本条已迁到 routers/signal_weights.py。


# ─────────────────────────────────────────────────────────────────────────────
# __main__：必须在所有 @app.* 装饰器执行完之后再调用 uvicorn.run，
# 否则 uvicorn 同步阻塞会让后续端点永远不被注册。
# (v300-fix: 本块从 16924 行移到 EOF —— 之前其后追加的 v300 段含 20 个路由装饰器，
#  在 `python api_server.py` 启动方式下全部丢失。)
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# v316 Crowd Pulse —— 已整体迁移（2026-11-16）
# 9 条端点搬到 routers/crowd.py：ranking / audience / sources / items / topics /
# subjects / collect / attention-lead / pending-attention。逻辑逐字未改，
# 只把 @app. 换成 @router.；include_router 放在原位，注册顺序不变。
# ─────────────────────────────────────────────────────────────────────────────
from routers.crowd import router as _crowd_router
_include_flat(app, _crowd_router)

# 2026-11-17: v342 多周期结算读数 → routers/decisions.py；观测层 → routers/obs.py。
# /api/debug/mem 留在原地（要读 api_server 内的模块级缓存对象）。
@app.get("/api/trading/health", tags=["trading"])
def api_trading_health():
    """v479：交易段健康度（只读）。账户、杠杆、持仓浮盈、对账 block、告警数、行情延迟。"""
    try:
        import trading_health as _th
        db = os.path.join(BASE_DIR, "trident_event_bus.db")
        return _th.build(db)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "ts": int(time.time())}


@app.get("/api/debug/mem", tags=["meta"])
async def get_debug_mem():
    """只读内存体检：进程 RSS + 各模块级缓存的条目数。

    存在的理由：之前判断"哪个缓存占内存"全靠猜，砍错地方既没省下内存又动了行为。
    这里把 RSS 和主要缓存的规模摊开，优化时按实测下手；不返回业务数据，
    也不给前端页面用，只在排查时手动调。
    """
    try:
        import resource as _res
        import sys as _sys
        _raw = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss
        _rss_mb = round(_raw / (1024 * 1024), 1) if _sys.platform == "darwin" else round(_raw / 1024, 1)
        _caches = {}
        for _name, _obj in (
            ("events_15d", _EVENTS_15D_CACHE),
            ("abnormal_5m", _5M_ABNORMAL_CACHE),
            ("attrib_5m", _5M_ATTRIB_CACHE),
            ("market_driver", _MARKET_DRIVER_CACHE),
            ("swing_points", _SWING_POINTS_CACHE),
            ("depth", _DEPTH_CACHE),
            ("movers", _MOVERS_CACHE),
        ):
            try:
                _caches[_name] = len(_obj)
            except Exception:
                _caches[_name] = None
        return {"ok": True, "rss_peak_mb": _rss_mb, "caches": _caches}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


# 2026-11-17: /api/v1 视图段 11 条端点已迁到 routers/views.py。

# 2026-11-17: 兼容挂回 —— agent_chat 那簇私有件已随端点上迁到 routers/agent_chat.py，
# 这里把名字重新暴露到 api_server 命名空间（同一批对象，不是复制），
# 让既有测试与 ~/.trident 下的运维脚本继续按 `api_server._xxx` 取用。
from routers import agent_chat as _agent_chat_mod

for _compat_name in (
    "_IPRateLimiter", "_TRUSTED_PROXIES", "_get_client_ip",
    "_AGENT_CHAT_LIMITER", "_UPLOAD_LIMITER", "_rate_limit_check",
    "_rate_limit_dep_agent_chat", "_rate_limit_dep_upload",
    "_SCHEMA_TEXT", "_SUMMARIZE_PROMPT", "_TABLE_REF_RE",
    "_AGENT_CHAT_ALLOWED_TABLES", "_AGENT_CHAT_FORBIDDEN_KEYWORDS",
    "_INJECTION_PATTERNS", "_wrap_injection_patterns",
    "_extract_table_names", "_sanitize_user_message_for_llm",
    "_time_context", "_llm_text_to_sql_sync", "_sql_uses_only_allowed_tables",
    "_clean_rows_for_frontend", "_summarize_results_sync",
):
    globals()[_compat_name] = getattr(_agent_chat_mod, _compat_name)
del _compat_name, _agent_chat_mod

# 2026-11-17: 同理，replay 的 K 线读取助手已随端点上迁到 routers/replay.py。
# 既有测试按 `api_server.replay_kline_live(...)` / `get_replay_signal_kline(...)` 直接调用。
from routers import replay as _replay_mod

replay_kline_live = _replay_mod.replay_kline_live
get_replay_signal_kline = _replay_mod.get_replay_signal_kline
del _replay_mod

# ────────────────────────────────────────────────────────────────────────────
# 2026-11-17: 版本号老路径 → 规范路径别名
#   79 条端点嵌在 /api/v1…v275 里，前端也被迫按版本号调用。这里只**加别名**
#   （/api/v186/experiences → /api/experiences，指向同一个 endpoint），
#   老路径原样保留并在 legacy_namespaces.json 里冻结，不允许再新增前缀。
# ────────────────────────────────────────────────────────────────────────────
try:
    from legacy_alias import add_canonical_aliases as _add_canonical_aliases

    _CANON_ALIASES = _add_canonical_aliases(app)
    if _CANON_ALIASES:
        print(f"[api] ✅ 版本号路径别名 {len(_CANON_ALIASES)} 条（老路径保留）", flush=True)
except Exception as _alias_exc:  # pragma: no cover - 别名失败不影响服务启动
    print(
        f"[api] ⚠️ 版本号路径别名安装失败: {type(_alias_exc).__name__}: {str(_alias_exc)[:120]}",
        flush=True,
    )

if __name__ == "__main__":
    import uvicorn
    # 注意：`uvicorn api_server:app` 不会执行这里 —— 自动入库挂在 lifespan 的
    # _migrate_schema() 里，两种启动方式都覆盖。
    _auto_import_seed_config()
    # v453 安全：默认只听回环。此前默认 0.0.0.0，同一局域网/VPN 内任何机器都能直连
    # 包括 /api/admin/* 这类写端点。要对外提供访问必须显式设 API_HOST=0.0.0.0。
    _host = os.getenv("API_HOST", "127.0.0.1")
    _port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run(app, host=_host, port=_port, reload=False, log_level="info")

def __getattr__(name: str):
    """懒读库路径（v475）：`DB_PATH` 不再在 import 时快照。

    原来这里写的是 `DB_PATH = config.DB_PATH` —— 只要本模块是在**别人把
    config.DB_PATH 改坏的那一刻**被首次 import，之后整场测试/整个进程就一直在
    读那个库（典型症状：test_structured_api_v172 单跑绿、全量 6 条全红 count=0）。
    改成属性访问时现读，`db.get_db_path()` 的语义与之一致。
    """
    if name == "DB_PATH":
        return config.DB_PATH
    raise AttributeError(name)
