#!/usr/bin/env python3
"""数据源状态注册表（线程安全）—— 供常驻“信息源面板”实时标色。

所有数据源（行情 / 宏观 / 新闻）在成功或失败时调用 ``report()`` 记录，面板通过
``snapshot()`` 拿到每个源的实时状态（online/offline/error/stale）并标色：
  - online（绿）  = 最近一次请求成功，且距今 ≤ STALE_S
  - error（红）   = 最近一次请求失败（有错误信息可用）
  - stale（黄）   = 有历史结果但已超过 STALE_S 未刷新（过期）
  - unknown（灰） = 已注册但从未产生过结果（未采集/未触发）

设计要点：
  - 无外部依赖、无 I/O，快照 O(n)，可被高频采集方与 API 并发调用。
  - 懒注册：report() 时若 source_id 未注册会自动补注册。
  - 各模块启动时可调用 register() 预置全量清单，保证面板未采集也显示。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

# 结果过期阈值（秒）：超过该时长未刷新 → stale（黄）
STALE_S = 300.0

# v382: 全员 5 分钟阈值是错的 —— 宏观源一天更新一次、jin10 快讯按需拉、
# OKX 在网络抖动时 5~15 分钟才补上一次。用同一个 300s 卡，它们永远显示"过期"，
# 面板健康度被无端拉到 70%，而数据其实在库里更新着。
# 按源的真实节奏给阈值（前缀匹配，取第一个命中的）。
_STALE_OVERRIDES = (
    ("bls",        86400.0),   # 美国劳工统计局：按发布节奏，一天一次
    ("fred",       86400.0),
    ("finnhub",    86400.0),
    ("ust_yields", 21600.0),   # 国债收益率：6 小时
    ("treasury",   86400.0),
    ("cboe",       21600.0),
    ("sec",        86400.0),
    ("fomc",       86400.0),
    ("jin10_mcp",   1800.0),   # 金十快讯：半小时
    ("okx_swap",     900.0),   # 永续行情：15 分钟（网络抖动时补数据有延迟）
)


def _stale_s(source_id: str) -> float:
    sid = (source_id or "").lower()
    for prefix, secs in _STALE_OVERRIDES:
        if sid.startswith(prefix):
            return secs
    return STALE_S

# v283+ 任务 5：数据源接入状态（接入方式/付费类型/可用性），用于在面板上把
#    「永远不工作的付费/无API 源」和「真坏了的源」明确区分。
#
#   active            = 已接入且会主动拉取（行情 binance_spot、okx_swap、jin10_mcp 等）
#   existing          = 既有模块已接入（quote_yahoo、flow_cftc、news_jin10 等 description 里 [existing] 标识）
#   free              = 免费公开 API（按需激活；当前未启用）
#   needs_key         = 需用户填 API key（finnhub、qinghong_flash 等）
#   needs_registration= 需注册账户（quote_oanda、quote_cme 等）
#   needs_login       = 需用户名/密码（ig 散户情绪等）
#   premium_tier      = 仅付费 plan 可用（coinglass V4 等）
#   no_public_api     = 无公开 API/仅终端/爬虫违 ToS（彭博、TradingView、Investing 等）
#   duplicate         = 与其他源功能重复，已停用（reuters_news 滞后 2h+ 与金十重复）
#   untracked         = 主动停用/特殊原因（用 last_error 说明）
ACCESS_STATES = (
    "active", "existing", "free", "needs_key", "needs_registration", "needs_login",
    "premium_tier", "no_public_api", "duplicate", "untracked", "needs_proxy",
)


# v367: 本机直连会被 reset、必须走境外代理的源（实测 fred / bls 直连 000，
# polygon 反而可直连）。这些不该计入「故障」，否则面板永远有 2-3 个红点。
_NEEDS_PROXY_SOURCES = {"fred", "bls", "finnhub"}

# 连接层失败特征（网络不可达 / TLS 中断 / DNS），归为 needs_proxy 而非故障
_NETWORK_ERR_HINTS = (
    "ReadTimeout", "ConnectTimeout", "timed out", "Timeout",
    "ConnectionError", "Connection aborted", "Connection refused",
    "SSLError", "RemoteDisconnected", "Max retries exceeded",
    "getaddrinfo", "Name or service not known", "Temporary failure",
)


def _parse_access_state(description: str) -> str:
    """从 free_sources 写入的 ``[state] note`` 描述中解析接入状态。

    找不到 ``[xxx]`` 前缀时返回 ``active``（历史 source_status.register_known_sources
    直接调用的行情/宏观/新闻类源都属于 active）。
    """
    if not description:
        return "active"
    s = description.strip()
    if s.startswith("[") and "]" in s:
        tag = s[1:s.index("]")].strip()
        if tag in ACCESS_STATES:
            return tag
    return "active"



# ── 全局注册表 ────────────────────────────────────────────────────────────
# 用 RLock（可重入）：report() 在持有锁时会对未注册源做懒 register()，register 也要拿锁，
# 若用普通 Lock 会自锁死锁（表现为“卡住/无响应”）。RLock 允许同一线程重复加锁。
_lock = threading.RLock()
_registry: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _parse_ts(v) -> Optional[float]:
    """兼容浮点 epoch / ISO 字符串 / None，返回 float 或 None。

    DB schema 的 last_ok_ts / last_fail_ts 是 TEXT（ISO 字符串），但内存里
    _registry 又是浮点（time.time()）。snapshot() 在做 now - last_X 时会
    触发 `TypeError: float - str` —— 这个函数统一解析。
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def register(
    source_id: str,
    *,
    category: str = "",
    label: str = "",
    description: str = "",
) -> None:
    """预注册一个数据源（幂等）。category/label 供面板分组与显示。

    v176.7 P0-fix 5: 若 DB 已有该 source_id 记录（api_server 守护已写入），
    从 DB 同步到内存 entry——避免 engine 独立进程的内存 entry 是空 detail，
    后续 report() 误以为源未被守护、覆盖 last_error。
    """
    with _lock:
        entry = _registry.get(source_id)
        if entry is None:
            # v176.7 P0-fix 5: 从 DB 反向加载（api_server 守护过的源带 untracked 标记）
            db_entry = None
            conn = None
            try:
                from db import get_connection
                conn = get_connection()
                conn.row_factory = __import__("sqlite3").Row
                row = conn.execute(
                    "SELECT category, label, description, status, last_error, last_fail_ts, detail "
                    "FROM source_status WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                if row is not None:
                    db_entry = {
                        "category": row["category"] or category,
                        "label": row["label"] or label,
                        "description": row["description"] or description,
                        "status": row["status"] or "unknown",
                        "last_result": None,
                        "last_ok_ts": None,
                        "last_fail_ts": row["last_fail_ts"],
                        "last_error": row["last_error"] or "",
                        "consecutive_failures": 0,
                        "total_ok": 0,
                        "total_fail": 0,
                        "last_latency_ms": None,
                        "updated_at": None,
                        "detail": row["detail"] or "",
                    }
            except Exception:
                pass
            finally:
                # v200 P0-C1: 原先 close() 写在 try 内, 表缺失/DB 异常时会被跳过 →
                # 每次懒注册泄漏 1 个 fd（与 v189-8 ai_worker 同类问题）。
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if db_entry is None:
                db_entry = {
                    "category": category,
                    "label": label,
                    "description": description,
                    "status": "unknown",
                    "last_result": None,
                    "last_ok_ts": None,
                    "last_fail_ts": None,
                    "last_error": "",
                    "consecutive_failures": 0,
                    "total_ok": 0,
                    "total_fail": 0,
                    "last_latency_ms": None,
                    "updated_at": None,
                    "detail": "",
                    # v283+ 任务 5：从 description 的 [state] 前缀解析，独立字段供 UI 渲染
                    "access_state": _parse_access_state(description),
                }
            else:
                # v283+：DB 反向加载的旧条目可能没 access_state，补上
                if "access_state" not in db_entry:
                    db_entry["access_state"] = _parse_access_state(db_entry.get("description") or description)
            db_entry["source_id"] = source_id
            _registry[source_id] = db_entry
        else:
            # v367: 带 [state] 标签的新描述要能覆盖 DB 里的旧描述 ——
            # 否则把 fred/bls 重新标成 [needs_proxy] 后，面板仍读旧 description，
            # access_state 解析不到标签，还是显示 error。
            if description.startswith("[") and entry.get("description") != description:
                entry["description"] = description
            # 补充首次注册时缺失的元信息
            if category and not entry["category"]:
                entry["category"] = category
            if label and not entry["label"]:
                entry["label"] = label
            if description and not entry["description"]:
                entry["description"] = description
            # v283+ 任务 5：注册时如有 description 但 entry 还未解析 access_state，补一次
            if "access_state" not in entry or entry.get("access_state") in (None, "active", ""):
                parsed = _parse_access_state(description or entry.get("description") or "")
                if parsed != "active" or entry.get("description"):
                    entry["access_state"] = parsed


def report(
    source_id: str,
    ok: bool,
    *,
    error: str = "",
    latency_ms: Optional[float] = None,
    detail: str = "",
    persist: bool = True,
) -> None:
    """记录一次数据源请求结果。ok=False 且 error 为空时自动抓当前异常文本不便，由调用方传入。

    v176.7: 检测"untracked"标记（detail 以 'untracked' 开头表示源已主动停用），
    调用方后续再 report(ok=False, error=...) 不会覆盖 untracked 状态——
    避免 daemon 内部的 watcher 反复报告同一 fail（如 ig_feed 401）覆盖 lifespan
    启动时显式设置的"未启用"标记。

    v200 P0-C1: 新增 persist 开关。内存更新（持 _lock，无 I/O）始终同步完成；
    只有结尾的 DB 落库可能因锁竞争阻塞 ~1.9s（_persist_report busy_timeout=500ms
    × retry_on_busy 3 次退避）。**同步调用方（engine 进程 / cron 脚本）保持
    persist=True 原行为不变**；事件循环内的调用方必须用 areport()，否则整条
    事件循环被卡住 —— 连零 DB 访问的 /health 也会排队，出现秒级延迟尖刺。
    """
    now = _now()
    with _lock:
        entry = _registry.get(source_id)
        if entry is None:
            register(source_id)
            entry = _registry[source_id]
        # v176.7：untracked 守护——detail 含 "untracked" 时跳过本次写入 last_error
        # 但仍统计 total_fail（fail 计数是真实流量指标）
        is_untracked = (entry.get("detail") or "").startswith("untracked")
        # v176.7 P0-fix 4: 即使 entry 当前 detail 是空（如 engine 独立进程无 lifespan 守护），
        # 如果 DB 已持久化的 last_error/detail 含 untracked，从 DB 反向恢复 entry detail 标记。
        if not is_untracked:
            db_is_untracked = (entry.get("last_error") or "").startswith("untracked")
            if db_is_untracked:
                is_untracked = True
                # 从 DB 同步 detail 标记（防下次守护检测又丢）
                if not entry.get("detail"):
                    entry["detail"] = "untracked — DB 持久化守护（详情见 last_error）"
        entry["updated_at"] = now
        entry["last_latency_ms"] = latency_ms
        if detail:
            # v176.7 P0-fix 3: 若 entry 已是 untracked 且本次 detail 不含 untracked，
            # 保留 entry 的 untracked 描述（不让普通 detail 覆盖守护标记）。
            if is_untracked and not detail.startswith("untracked"):
                pass  # 守护中，忽略普通 detail
            else:
                entry["detail"] = detail
            # detail 写入后再检测 untracked 状态（detail 可能在本次传入）
            is_untracked = (entry["detail"] or "").startswith("untracked")
        # v176.7 P0-fix 2: 区分「显式守护调用」（detail 含 untracked）vs「后续 fetcher 调用」
        # - 显式守护调用：强制覆盖 last_error（lifespan 守护能压过 daemon 早期 watcher 的残留）
        # - 后续 fetcher 调用：尊重已有守护（不覆盖 untracked 描述）
        is_explicit_guard = detail.startswith("untracked")
        if ok:
            entry["status"] = "online"
            entry["last_result"] = "ok"
            entry["last_ok_ts"] = now
            entry["last_error"] = ""
            entry["consecutive_failures"] = 0
            entry["total_ok"] += 1
        elif ((not is_untracked)
              and any(_k in (error or "") for _k in ("限速", "超时", "timed out", "Timeout",
                                                     "为空", "样本 <", "rate limit", "429", "Too Many",
                                                     "前收"))
              and bool(entry.get("last_ok_ts"))
              and (now - entry["last_ok_ts"]) <= 900):
            # v367: 偶发抖动(限速/超时/空样本) 且 15 分钟内有过成功 → 不翻红。
            # 实测 OKX XAU/XAG 的 candles 偶发被限速返空, 原逻辑一次失败就置 error,
            # 之后没人再触发探测 → 面板长期标红(12 个 error 里两个是这种假警报)。
            entry["last_fail_ts"] = now
            entry["last_error"] = (error or "")[:400]
            entry["consecutive_failures"] += 1
            entry["total_fail"] += 1
        else:
            # untracked 源：状态保持 "error" 但 last_error 维持 untracked 描述
            entry["status"] = "error" if not is_untracked else "error"
            entry["last_result"] = "error"
            entry["last_fail_ts"] = now
            if not is_untracked:
                entry["last_error"] = (error or "")[:400]
            else:
                # v176.7 P0-fix: untracked 时 last_error 也保持 untracked 描述
                # 让 _persist_report 的 INSERT 拿到的就是 untracked 字符串。
                if is_explicit_guard:
                    # 显式守护调用（lifespan 启动）→ 强制覆盖 last_error 为本次 error
                    # 修复 daemon 早期 watcher 调用顺序问题：lifespan 守护能压过残留
                    entry["last_error"] = (error or "")[:400] or "untracked"
                elif not entry.get("last_error"):
                    entry["last_error"] = entry.get("detail", "") or "untracked"
                # 否则：守护中（后续 fetcher 调），不动 last_error
            entry["consecutive_failures"] += 1
            entry["total_fail"] += 1

    # ── P0-3：落库到 source_status（current）+ history（前端的"信息源监控面板"用） ──
    # 高频调用（每个 K 线周期/每条新闻都触发），要"快失败、慢日志"，别因为落库阻塞行情。
    # v200 P0-C1: areport() 传 persist=False（内存已同步更新），落库由工作线程负责。
    if persist:
        _persist_with_retry(source_id, ok, latency_ms, error, detail)


def _persist_with_retry(
    source_id: str, ok: bool, latency_ms, error: str, detail: str
) -> None:
    """落库 + 指数退避重试；失败只告警一次，绝不影响内存状态。

    v174 高频路径用 500ms busy_timeout 做 fast-fail(不阻塞行情),代价是锁竞争高峰期
    直接放弃落库(实测 302 次 "persist skipped")。这里补 3 次指数退避重试:
    最坏 ≈ 3×0.5s + 0.35s ≈ 1.9s 硬上界,仍是快失败,不会变成长阻塞。
    """
    try:
        import db as _dbmod
        _dbmod.retry_on_busy(
            lambda: _persist_report(source_id, ok, latency_ms, error, detail)
        )
    except Exception as _e:
        # 持久化失败不影响内存里的状态 —— 监控不该变成主链路瓶颈
        if not getattr(_persist_with_retry, "_persist_warned", False):
            _persist_with_retry._persist_warned = True
            import sys as _sys
            print(
                f"[source_status] DB persist skipped (will not retry log): "
                f"{type(_e).__name__}: {str(_e)[:80]}",
                file=_sys.stderr, flush=True,
            )
        # v483: 静默丢写必须留账 —— 原来只在进程内 print 一次，事后无人能回答
        # "今天源状态丢了多少条"，而 source_status_history 正是判断"源是否健康"的依据。
        try:
            import silent_ledger
            silent_ledger.record(
                "source_status.persist",
                f"{type(_e).__name__}: {str(_e)[:120]}",
            )
        except Exception:
            pass


async def areport(
    source_id: str,
    ok: bool,
    *,
    error: str = "",
    latency_ms: Optional[float] = None,
    detail: str = "",
) -> None:
    """事件循环安全版 report()（v200 P0-C1）—— async 调用方一律用这个。

    report() 的内存更新无 I/O（持 RLock，微秒级），但结尾的 DB 落库在写锁竞争时
    最坏阻塞 ~1.9s。api_server 的后台 watcher（infra_health_watcher 每 30s 6 次、
    qinghong_news_watcher 等）直接在事件循环线程里调 report() 时，整条 loop 会被
    卡住：此时连不碰数据库的 /health 端点都要排队，表现为周期性秒级延迟尖刺。

    本函数把「内存更新」保持同步（read-after-write 语义不变，面板读 snapshot()
    立刻可见），只把「落库」丢给线程池，事件循环零阻塞。

    语义与 report() 完全一致（含「返回即已落库」）：to_thread 是被 await 的，
    调用方拿到结果时 DB 行已写好。唯一区别是**等待期间事件循环仍可服务其他请求**
    —— 这正是 /health 秒级延迟尖刺的解药。调用方自身仍会等落库完成，所以
    lifespan 里「写完 untracked 标记再启动 watcher」的顺序保证也可以用 areport，
    但那里是同步上下文，保持用 report() 更直观。
    """
    report(source_id, ok, error=error, latency_ms=latency_ms, detail=detail,
           persist=False)
    await asyncio.to_thread(_persist_with_retry, source_id, ok, latency_ms, error, detail)


def _persist_report(source_id: str, ok: bool, latency_ms, error: str, detail: str) -> None:
    """P0-3 新增：把 report() 结果落到 source_status + source_status_history。

    v174: 高频调用,必须 fast-fail. _persist_warned 标志位已存在;这里再降级:
    - conn.execute 失败时直接 return,不在外层 try 抛 (被 report() 的 except 已捕获).
    - 加 busy_timeout=500ms 局部设置,即使遇到锁也快速放弃,不阻塞行情.

    v176.7 P0-fix: 检查 entry.detail 是否含 "untracked"，若是则不覆盖 last_error/detail。
    """
    import time as _t
    import json as _json
    import sqlite3 as _sq
    try:
        from db import get_connection
    except Exception:
        return
    conn = None
    try:
        # 2026-11-16 撤回单写闸门。
        # 原做法：先排队拿 flock，再开 SQLite 写连接。
        # 实测后果：source_status 是高频写（约 1,500 行/分钟），拿到 flock 后仍要
        # 排队等 sqlite 写锁；一旦某次 INSERT 卡住，闸门被它占用数十秒 → 其他写者
        # 全部堵在闸门外，反而放大了竞争，并把 api 自身启动流程卡死
        # （database is locked，8000 端口整段不可用）。见 runtime/db_write_lock.json。
        # 结论：闸门只适合低频/批量写；高频小写入用短 busy_timeout 直接写。
        conn = get_connection()
        # v174: 局部更短 busy_timeout,绝不阻塞高频路径
        # v479：500ms 实测在采集密集时段不够（日志里反复出现
        # "[source_status] DB persist skipped ... database is locked"），
        # 而这条落库是在工作线程里跑的、且外层已有 3 次指数退避重试，
        # 所以把等待放到 1.2s（仍远小于一次行情周期），换来更少的静默丢写。
        # 需要恢复原行为：SOURCE_STATUS_BUSY_TIMEOUT_MS=500。
        try:
            import os as _os
            _bt = int(_os.getenv("SOURCE_STATUS_BUSY_TIMEOUT_MS", "1200") or 1200)
            conn.execute(f"PRAGMA busy_timeout={_bt};")
        except Exception:
            pass
        cat, label, desc = "", "", ""
        is_untracked = False
        with _lock:
            entry = _registry.get(source_id, {})
            cat = entry.get("category") or ""
            label = entry.get("label") or ""
            desc = entry.get("description") or ""
            # v176.7 P0-fix: 同样守护 DB 落库——untracked 源不覆盖 last_error/detail
            is_untracked = (entry.get("detail") or "").startswith("untracked")
        now_iso = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime())
        # v176.7 P0-fix: untracked 时,直接读 entry["last_error"] 作为落库值。
        # 上层 report() 已保证 untracked 源 entry["last_error"] 永远带 untracked 描述;
        # 这里不再用参数 error / detail 覆盖。
        persist_error = (error or "")[:400]
        persist_detail = (detail or "")[:400]
        if is_untracked and not ok:
            # untracked 源: 落库用 entry["last_error"] 而非本次 error 参数
            with _lock:
                e = _registry.get(source_id, {})
                persist_error = (e.get("last_error") or "")[:400]
                persist_detail = (e.get("detail") or "")[:400]
            if not persist_error:
                # 守护自洽: 如果 entry 里都没有,直接写 "untracked" 兜底
                persist_error = "untracked"
        # current 表：UPSERT
        conn.execute(
            """
            INSERT INTO source_status
              (source_id, category, label, description, status, last_result,
               last_ok_ts, last_fail_ts, last_error, consecutive_failures,
               total_ok, total_fail, last_latency_ms, last_tick_at, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              status=excluded.status,
              last_result=excluded.last_result,
              last_ok_ts=COALESCE(excluded.last_ok_ts, source_status.last_ok_ts),
              last_fail_ts=COALESCE(excluded.last_fail_ts, source_status.last_fail_ts),
              last_error=excluded.last_error,
              consecutive_failures=excluded.consecutive_failures,
              total_ok=source_status.total_ok + excluded.total_ok,
              total_fail=source_status.total_fail + excluded.total_fail,
              last_latency_ms=excluded.last_latency_ms,
              last_tick_at=excluded.last_tick_at,
              detail=excluded.detail
            """,
            (
                source_id, cat, label, desc,
                ("online" if ok else "error"),
                ("ok" if ok else "error"),
                now_iso if ok else None,
                now_iso if not ok else None,
                persist_error,
                (0 if ok else 1),
                (1 if ok else 0),
                (0 if ok else 1),
                latency_ms,
                now_iso,
                persist_detail,
            ),
        )
        # history 表：追加
        conn.execute(
            "INSERT INTO source_status_history (source_id, ok, latency_ms, error, detail) VALUES (?,?,?,?,?)",
            (source_id, 1 if ok else 0, latency_ms, (error or "")[:200], (detail or "")[:200]),
        )
        conn.commit()
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def snapshot() -> List[Dict[str, Any]]:
    """返回所有源的状态列表（含未采集的预注册源），按类别/ID 排序，供面板直接渲染。"""
    now = _now()
    with _lock:
        out: List[Dict[str, Any]] = []
        for entry in _registry.values():
            item = dict(entry)
            # v176.7 P0-fix 6: DB 反向加载的 last_X_ts 是 ISO 字符串，内存
            # _registry 里是浮点；snapshot() 直接相减会 TypeError。
            ok_ts = _parse_ts(item.get("last_ok_ts"))
            fail_ts = _parse_ts(item.get("last_fail_ts"))
            # 按最近结果与时间刷新实时状态字段
            if item["last_result"] is None:
                item["status"] = "unknown"
            else:
                last_ts = ok_ts if item["last_result"] == "ok" else fail_ts
                if item["last_result"] == "ok":
                    item["status"] = ("online" if (last_ts and now - last_ts <= _stale_s(item.get("source_id", "")))
                                      else "stale")
                else:
                    item["status"] = "error"
            item["secs_since_ok"] = round(now - ok_ts, 1) if ok_ts else None
            item["secs_since_fail"] = round(now - fail_ts, 1) if fail_ts else None
            # v283+ 任务 5：暴露接入状态字段（active/free/needs_key/...），UI 区分"真坏"和"故意不接"
            # 注意：description 可能被多次 register() 覆写（先 active 默认，后 free_sources 加 [state] 前缀），
            # 每次 snapshot 都重新解析一次 description 兜底，确保 UI 拿到最新值。
            desc_now = item.get("description") or ""
            state = _parse_access_state(desc_now)
            # v367: 已确认「从本机直连被阻断、只能走境外代理」的源，直接按 id 钉死，
            # 不依赖 DB 里的描述文本（DB 旧行会把 [needs_proxy] 标签冲掉）。
            if item.get("source_id") in _NEEDS_PROXY_SOURCES:
                state = "needs_proxy"
            # v371: 连接层失败(ReadTimeout / 连接被拒 / TLS 中断 / DNS)是网络可达性问题，
            # 不是源本身坏了。OKX 直连在国内时通时断，面板按"最后结果"把它打成 error，
            # 但每次失败后下一次成功又会翻绿 —— UI 上就成了红绿反复跳。
            # 这类统一按 needs_proxy 归类(与 fred/bls 同一处置：等代理或备用入口)，
            # 恢复后下一次成功照样自动翻绿。
            if item["status"] == "error":
                _err_txt = (item.get("last_error") or "")
                if any(_h in _err_txt for _h in _NETWORK_ERR_HINTS):
                    item["access_state"] = "needs_proxy"
                    item["status"] = "disabled"
            item["access_state"] = state
            # v367: 主动停用/缺凭据/无公开API 的源不该计入「故障」。
            # 实测 8 个 error 里 5 个是这种(ig 凭据失效 / qinghong key 到期 / spdr_gld 未接入 /
            # reuters 已停用 / OKX 无 WTI 合约), 面板把它们和「真挂了」混在一起, 导致
            # 误判"12 个源故障"。这里统一降级为 disabled, 让 error 只剩真故障。
            if state in ("needs_key", "needs_registration", "needs_login",
                         "premium_tier", "no_public_api", "duplicate", "untracked",
                         "needs_proxy"):
                if item["status"] in ("error", "unknown"):
                    item["status"] = "disabled"
            # 兜底：描述里没打 [state] 标签、但 last_error 已明确写「未启用/未接入/已停用/到期」
            # 的源，同样降级为 disabled（这批是老 register 调用，没带标签）。
            elif item["status"] == "error" and any(
                _k in (item.get("last_error") or "")
                for _k in ("未启用", "未接入", "已停用", "到期", "已关闭", "无此合约", "无此永续",
                           "未配置", "缺少 API", "missing api", "invalid api key")
            ):
                item["status"] = "disabled"
            # v367: 「注册了但从来没有上报过一次」的源 = 没有采集端接线，不是「未知」。
            # 实测 40 个 unknown 属于这类(alphavantage/bitget/coingecko/eia...), 面板上
            # 灰点等于告诉用户"可能没数据", 其实是"根本没接"。按 untracked 显式标注,
            # 并从健康度分母里剔除(否则 40 个灰点把整个数据源健康度稀释到 18%)。
            elif (item["status"] == "unknown"
                  and (item.get("total_ok", 0) or 0) == 0
                  and (item.get("total_fail", 0) or 0) == 0
                  and item.get("updated_at") in (None, "")):
                item["status"] = "disabled"
                item["access_state"] = "untracked"
            # 同步回内存 entry（避免下次 report() 重新走 active 路径覆盖）
            if entry.get("access_state") != state:
                entry["access_state"] = state
            out.append(item)
    out.sort(key=lambda x: (x["category"], x["source_id"]))
    return out


# 预注册行情 / 宏观 / 新闻三类的全量清单（面板脚手架；具体采集方再 report 结果）。
def register_known_sources() -> None:
    # 行情 —— 与 quote_sources.ASSET_ROUTE 对齐（加密/贵金属/原油/美股）
    for sid, cat, label, desc in (
        ("binance_fapi:BTCUSDT", "行情", "币安永续 BTCUSDT", "加密 BTC 合约行情/K线（fapi，走代理）"),
        ("binance_fapi:ETHUSDT", "行情", "币安永续 ETHUSDT", "加密 ETH 合约行情/K线（fapi，走代理）"),
        ("binance_fapi:SOLUSDT", "行情", "币安永续 SOLUSDT", "加密 SOL 合约行情/K线（fapi，走代理）"),
        ("okx_swap:XAU-USDT-SWAP", "行情", "OKX 黄金永续", "黄金 XAU 合约行情/K线/费率/持仓（直连）"),
        ("okx_swap:XAG-USDT-SWAP", "行情", "OKX 白银永续", "白银 XAG 合约行情/K线/费率/持仓（直连）"),
        ("domestic_spot:WTI", "行情", "WTI 原油现货", "新浪/东财 WTI 原油现货价（国内直连）"),
        ("domestic_spot:XAU", "行情", "黄金现货", "新浪/东财黄金现货价（国内直连、兜底）"),
        ("tencent:us", "行情", "腾讯美股报价", "qt.gtimg.cn 美股/韩股/外盘现货报价（国内直连，0.3-0.7s）"),
        ("tencent:us_kline", "行情", "腾讯美股日线", "web.ifzq.gtimg.cn 美股日线（Yahoo 429 时兜底）"),
        ("sina:hf", "行情", "新浪外盘期货", "hq.sinajs.cn hf_* 外盘现货/期货报价 + 日线"),
        ("naver:kr", "行情", "Naver 韩股日线", "api.finance.naver.com 韩股日线（Yahoo 限流时兜底）"),
        ("yahoo:chart", "行情", "Yahoo Finance", "query1/2.finance.yahoo.com K 线（第一优先，常 429/SSL 受限）"),
        ("polygon", "行情", "Polygon 美股前收", "美股行情兜底（Yahoo 失败时取上一交易日收盘价）"),
    ):
        register(sid, category=cat, label=label, description=desc)
    # 宏观
    for sid, cat, label, desc in (
        ("fred", "宏观", "FRED 联邦数据", "[needs_proxy] CPI/非农/利率/M2 + 10Y TIPS/美元/VIX + SOFR/逆回购/TGA/日债（本机直连被 reset，需境外代理）"),
        ("cftc_cot", "宏观", "CFTC COT 持仓", "黄金/原油/日元期货持仓周报（投机多空比、持仓量、净头寸）"),
        ("spdr_gld", "宏观", "SPDR GLD 持仓", "黄金 ETF 每日持仓吨数"),
        ("deribit_options", "宏观", "Deribit 期权", "BTC/ETH DVOL 波动率指数、Call/Put 持仓比、最大痛点"),
        ("eia", "宏观", "EIA 石油库存", "美国原油商业库存/汽油总库存周报（每周三发布，官方供需）"),
        ("cboe", "宏观", "CBOE 波动率/尾部风险", "VIX 与 SKEW 日度全历史（1990起，官方 CSV；恐慌与尾部风险定价，免key）"),
        ("defillama", "宏观", "DefiLlama 稳定币供给", "稳定币总供给（加密流动性水位，免key）"),
        ("polymarket", "宏观", "Polymarket 事件概率", "Fed/宏观/加密事件的前瞻隐含概率（免key）"),
        ("coinpaprika", "宏观", "CoinPaprika 加密全局", "BTC 市占率/加密总市值/24h量（资金轮动，免key）"),
        ("blockchain", "宏观", "Blockchain.com 链上", "BTC 哈希率/挖矿难度（链上基本面，日度，免key）"),
        ("coingecko", "宏观", "CoinGecko 市场宽度", "加密涨跌家数/ETH市占率/全市场24h涨跌（市场宽度，免key）"),
        ("fmp", "宏观", "FMP 美股基本面", "美股市值/PE/PEG/净利率/负债（免费key，TTM估值，低频缓存）"),
        ("alphavantage", "宏观", "Alpha Vantage 美股兜底", "FMP 失败时兜底拉美股估值（免费 25 次/日，配额极紧）"),
        ("bybit_fapi", "宏观", "Bybit 永续费率/OI", "BTC/ETH 永续资金费率/OI/基差（免key，多所对比）"),
        ("bitget_fapi", "宏观", "Bitget 永续费率", "BTC/ETH 永续资金费率（免key，多所对比）"),
        ("coinglass", "宏观", "Coinglass 衍生品聚合", "全网爆仓/全市场OI/资金费率/恐惧贪婪（V4，需付费 plan，免费 tier 降级）"),
        # v189-7: 新接入 Coinglass+LSR 中转 API（与官方 coinglass 不同, 是单币信号/排行榜）
        ("coinglass_lsr", "宏观", "Coinglass LSR 信号", "单币多空持仓比 trader/whale + 排行榜 + coins-markets（中转API试用 key，10/60s）"),
        ("ig", "宏观", "IG 客户情绪", "XAU/WTI/BTC 散户多空比（IG Markets，需 identifier+password 登录）"),
    ):
        register(sid, category=cat, label=label, description=desc)
    # 新闻
    for sid, cat, label, desc in (
        ("jin10_mcp", "新闻", "金十 MCP", "快讯/财经日历/行情（Bearer token，每日配额）"),
        ("reuters_news", "新闻", "路透新闻", "英文头条经翻译入库"),
        ("bls", "新闻", "BLS 劳工统计局", "[needs_proxy] CPI/非农/失业率官方发布（本机直连被 reset，需境外代理）"),
        ("finnhub", "新闻", "Finnhub 美股新闻", "美股新闻（需 key，否则 Yahoo 兜底）"),
        ("qinghong_flash", "新闻", "晴红·标卡聚合", "第三方聚合资讯（试用 key，到点自动关）"),
    ):
        register(sid, category=cat, label=label, description=desc)
    # 引擎 / 服务自身健康（影响所有判断的"心脏"通道）
    for sid, cat, label, desc in (
        ("engine_webhook", "引擎", "Engine Webhook", "engine.py 主循环（9000 端口），决策 / 模型推理 / 落库源头"),
        ("api_server", "引擎", "API Server", "api_server.py（8000 端口），前端读写 / SSE 推送 / 行情快照中转"),
        ("trident_event_bus_db", "引擎", "EventBus SQLite", "trident_event_bus.db（所有决策 / 新闻 / 行情落盘），损坏即全局失忆"),
    ):
        register(sid, category=cat, label=label, description=desc)