#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coinglass + LSR 数据落地 + 后台 watcher（v189-7）。

职责:
- 从 CoinglassClient 拿 trader/whale 单币信号, INSERT 到 coinglass_lsr_samples
- 每次拉取后 INSERT 一条 coinglass_lsr_health (用于面板 + 配额告警)
- 后台 daemon 线程: 每 COINGLASS_LSR_POLL_SECONDS 拉一次所有币 + 两种 mode
- API 端点: 供前端读最新信号 / 历史曲线 / 健康度

设计要点:
- watcher 是 daemon thread, 进程退出自动结束
- 配额预警: 头 remaining ≤ COINGLASS_RATE_WARN_REMAINING 时延长 sleep, 避免打满
- 失败退避: 同币连续失败 3 次降低拉取频率 (每分钟一次 → 5 分钟一次)
- source_status.report 由 client.startup_self_check + 每次 watcher 循环完成一次
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import coinglass_client as cg_client
import db
import source_status

SOURCE_ID = cg_client.SOURCE_ID  # = "coinglass_lsr"

_DEFAULT_TRADER_SYMBOLS: Tuple[str, ...] = ("BTC", "ETH", "SOL")
_DEFAULT_WHALE_SYMBOLS: Tuple[str, ...] = ("BTC", "ETH")  # whale 模式只拉核心币
_FAIL_BACKOFF_THRESHOLD = 3
_FAIL_BACKOFF_SLEEP_S = 300.0
_STATUS_LOG_EVERY_S = 30.0

_watcher_state: Dict[str, Any] = {
    "thread": None,
    "stop_event": threading.Event(),
    "started_at": None,
    "total_runs": 0,
    "total_ok": 0,
    "total_fail": 0,
    "last_run_ts": None,
    "last_error": "",
    "consecutive_failures": 0,
    "current_sleep_s": 0.0,
}
_state_lock = threading.Lock()


def _to_float(raw: Any, default: float = 0.0) -> float:
    """安全转 float, 失败回退 default."""
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _to_int(raw: Any, default: int = 0) -> int:
    """安全转 int, 失败回退 default."""
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _parse_lsr_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从 LSR API 单币响应抽取需要落库的字段."""
    return {
        "ratio": _to_float(payload.get("ratio")),
        "ratio_pct": str(payload.get("ratio_pct") or payload.get("trader_ratio_pct") or ""),
        "ratio_30m": _to_float(payload.get("ratio_30m")),
        "delta_2m": _to_float(payload.get("delta_2m") or payload.get("trader_delta_2m")),
        "delta_2m_pct": str(payload.get("delta_2m_pct") or payload.get("trader_delta_2m_pct") or ""),
        "delta_30m": _to_float(payload.get("delta_30m") or payload.get("trader_delta_30m")),
        "delta_30m_pct": str(payload.get("delta_30m_pct") or payload.get("trader_delta_30m_pct") or ""),
        "delta_4h": _to_float(payload.get("delta_4h") or payload.get("trader_delta_4h")),
        "delta_4h_pct": str(payload.get("delta_4h_pct") or payload.get("trader_delta_4h_pct") or ""),
        "rank": _to_int(payload.get("rank")),
        "oi_mcap_ratio": _to_float(payload.get("oi_mcap_ratio")),
        "ov_ratio": _to_float(payload.get("ov_ratio")),
        "ov_long_qty": _to_float(payload.get("ov_long_qty")),
        "ov_short_qty": _to_float(payload.get("ov_short_qty")),
        "ov_long_avg_entry": _to_float(payload.get("ov_long_avg_entry")),
        "ov_short_avg_entry": _to_float(payload.get("ov_short_avg_entry")),
        "ov_long_whales_qty": _to_float(payload.get("ov_long_whales_qty")),
        "ov_short_whales_qty": _to_float(payload.get("ov_short_whales_qty")),
        "ov_total_traders": _to_int(payload.get("ov_total_traders")),
        "ov_long_traders": _to_int(payload.get("ov_long_traders")),
        "ov_short_traders": _to_int(payload.get("ov_short_traders")),
        "ov_long_whales": _to_int(payload.get("ov_long_whales")),
        "ov_short_whales": _to_int(payload.get("ov_short_whales")),
        "ov_long_profit_traders": _to_int(payload.get("ov_long_profit_traders")),
        "ov_short_profit_traders": _to_int(payload.get("ov_short_profit_traders")),
        "source_ts": _to_float(payload.get("calculated_at") or payload.get("updated_at") or 0),
        "updated_at": _to_float(payload.get("updated_at") or payload.get("calculated_at") or 0),
        "cache_age_s": _to_float(payload.get("cache_age_seconds")),
    }


def insert_lsr_sample(
    *,
    mode: str,
    symbol: str,
    parsed: Dict[str, Any],
    raw_payload: Dict[str, Any],
) -> int:
    """INSERT 一条 coinglass_lsr_samples. 返回 rowid, -1 表示 UNIQUE 命中忽略."""
    now = int(time.time())
    payload_json = json.dumps(raw_payload, ensure_ascii=False)
    try:
        conn = db.get_connection()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO coinglass_lsr_samples
              (ts, source_ts, updated_at, cache_age_s, mode, symbol, rank,
               ratio, ratio_pct, ratio_30m,
               delta_2m, delta_2m_pct, delta_30m, delta_30m_pct,
               delta_4h, delta_4h_pct,
               oi_mcap_ratio, ov_ratio,
               ov_long_qty, ov_short_qty,
               ov_long_avg_entry, ov_short_avg_entry,
               ov_long_whales_qty, ov_short_whales_qty,
               ov_total_traders, ov_long_traders, ov_short_traders,
               ov_long_whales, ov_short_whales,
               ov_long_profit_traders, ov_short_profit_traders,
               raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, parsed["source_ts"], parsed["updated_at"], parsed["cache_age_s"],
                mode, symbol, parsed["rank"],
                parsed["ratio"], parsed["ratio_pct"], parsed["ratio_30m"],
                parsed["delta_2m"], parsed["delta_2m_pct"],
                parsed["delta_30m"], parsed["delta_30m_pct"],
                parsed["delta_4h"], parsed["delta_4h_pct"],
                parsed["oi_mcap_ratio"], parsed["ov_ratio"],
                parsed["ov_long_qty"], parsed["ov_short_qty"],
                parsed["ov_long_avg_entry"], parsed["ov_short_avg_entry"],
                parsed["ov_long_whales_qty"], parsed["ov_short_whales_qty"],
                parsed["ov_total_traders"], parsed["ov_long_traders"], parsed["ov_short_traders"],
                parsed["ov_long_whales"], parsed["ov_short_whales"],
                parsed["ov_long_profit_traders"], parsed["ov_short_profit_traders"],
                payload_json,
            ),
        )
        rowid = int(cur.lastrowid or 0)
        conn.commit()
        conn.close()
        return rowid if rowid > 0 else -1
    except Exception as exc:
        print(f"[COINGLASS] insert_lsr_sample failed: {exc}", flush=True)
        return -1


def insert_health(
    *,
    endpoint: str,
    ok: bool,
    status: int,
    latency_ms: int,
    rate_limit: int,
    rate_used: int,
    rate_remaining: int,
    key_expires_at: str,
    error_kind: str,
    error_msg: str,
) -> int:
    """INSERT 一条 coinglass_lsr_health."""
    now = int(time.time())
    try:
        conn = db.get_connection()
        cur = conn.execute(
            """
            INSERT INTO coinglass_lsr_health
              (ts, endpoint, ok, status, latency_ms,
               rate_limit, rate_used, rate_remaining,
               key_expires_at, error_kind, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, endpoint, 1 if ok else 0, int(status or 0), int(latency_ms or 0),
                int(rate_limit or 0), int(rate_used or 0), int(rate_remaining or 0),
                str(key_expires_at or ""), str(error_kind or ""), str(error_msg or "")[:200],
            ),
        )
        rowid = int(cur.lastrowid or 0)
        conn.commit()
        conn.close()
        return rowid
    except Exception as exc:
        print(f"[COINGLASS] insert_health failed: {exc}", flush=True)
        return 0


def _symbols_for_mode(mode: str) -> Tuple[str, ...]:
    """拉哪些币. trader 全量, whale 只拉核心币 (节约配额)."""
    all_syms = config.COINGLASS_SYMBOLS or _DEFAULT_TRADER_SYMBOLS
    if mode == "whale":
        return tuple(s for s in _DEFAULT_WHALE_SYMBOLS if s in all_syms) or all_syms
    return all_syms


def _run_once(client: cg_client.CoinglassClient) -> Tuple[int, int, int, int, int]:
    """执行一次拉取循环. 返回 (ok_count, fail_count, rate_remaining, rate_used, rate_limit).

    所有异常被吞掉记 health 行, 不抛 (避免线程崩).
    """
    ok_count = 0
    fail_count = 0
    last_remaining = 0
    last_used = 0
    last_limit = 0

    for mode in ("trader", "whale"):
        for sym in _symbols_for_mode(mode):
            endpoint = f"/api/lsr/signal/{sym}?mode={mode}"
            t0 = time.time()
            try:
                # call_with_backoff 包装 fn, 返回的是 fn 的返回值 (本例: (body, headers, etag))
                body, headers, _etag = cg_client.call_with_backoff(
                    lambda: client.get(f"/api/lsr/signal/{sym}", params={"mode": mode})
                )
                latency_ms = int((time.time() - t0) * 1000)
                rate_info = cg_client.extract_rate_limit(headers)
                last_remaining = int(rate_info.get("remaining") or 0)
                last_used = int(rate_info.get("used") or 0)
                last_limit = int(rate_info.get("limit") or 0)
                parsed = _parse_lsr_signal(body)
                rowid = insert_lsr_sample(
                    mode=mode, symbol=sym, parsed=parsed, raw_payload=body
                )
                insert_health(
                    endpoint=endpoint, ok=True, status=200, latency_ms=latency_ms,
                    rate_limit=last_limit, rate_used=last_used,
                    rate_remaining=last_remaining,
                    key_expires_at=str(rate_info.get("expires_at") or ""),
                    error_kind="", error_msg="",
                )
                if rowid > 0:
                    ok_count += 1
                # rowid == -1 时为 UNIQUE 命中, 缓存秒级幂等, 不算 ok/fail
            except cg_client.CoinglassAPIError as exc:
                latency_ms = int((time.time() - t0) * 1000)
                insert_health(
                    endpoint=endpoint, ok=False,
                    status=int(exc.status or 0), latency_ms=latency_ms,
                    rate_limit=last_limit, rate_used=last_used,
                    rate_remaining=last_remaining,
                    key_expires_at="", error_kind=exc.kind,
                    error_msg=str(exc)[:200],
                )
                fail_count += 1
                print(
                    f"[COINGLASS] fetch {endpoint} failed: {exc}",
                    flush=True,
                )
                if exc.kind in ("unauthorized", "forbidden_plan"):
                    time.sleep(2.0)
            except Exception as exc:
                latency_ms = int((time.time() - t0) * 1000)
                insert_health(
                    endpoint=endpoint, ok=False, status=0, latency_ms=latency_ms,
                    rate_limit=0, rate_used=0, rate_remaining=0,
                    key_expires_at="", error_kind="exception",
                    error_msg=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
                fail_count += 1
                print(
                    f"[COINGLASS] fetch {endpoint} unexpected: {exc}",
                    flush=True,
                )
    return ok_count, fail_count, last_remaining, last_used, last_limit


def _watcher_loop() -> None:
    """watcher 主循环. daemon thread, 进程退出自动结束."""
    if not config.COINGLASS_ENABLED:
        print("[COINGLASS] watcher skipped: COINGLASS_ENABLED=0", flush=True)
        return
    if not config.COINGLASS_API_KEY:
        print("[COINGLASS] watcher skipped: API key missing", flush=True)
        return
    client = cg_client.CoinglassClient()
    base_sleep = float(config.COINGLASS_LSR_POLL_SECONDS)
    last_status_log = 0.0
    print(
        f"[COINGLASS] watcher started (poll={base_sleep}s, "
        f"trader_symbols={_symbols_for_mode('trader')}, "
        f"whale_symbols={_symbols_for_mode('whale')}, "
        f"key={config.coinglass_key_masked()})",
        flush=True,
    )
    # 启动后立刻拉一次
    try:
        ok, fail, remaining, used, limit = _run_once(client)
        with _state_lock:
            _watcher_state["total_runs"] += 1
            _watcher_state["total_ok"] += ok
            _watcher_state["total_fail"] += fail
            _watcher_state["last_run_ts"] = int(time.time())
            if fail == 0 and ok > 0:
                _watcher_state["consecutive_failures"] = 0
                _watcher_state["last_error"] = ""
            else:
                _watcher_state["consecutive_failures"] += 1
            _watcher_state["current_sleep_s"] = base_sleep
        source_status.report(SOURCE_ID, ok > 0, latency_ms=None)
        if ok + fail > 0:
            print(
                f"[COINGLASS] first cycle ok={ok} fail={fail} "
                f"rate_used={used}/{limit} remaining={remaining}",
                flush=True,
            )
    except Exception as exc:
        print(f"[COINGLASS] first cycle crashed: {exc}", flush=True)

    while not _watcher_state["stop_event"].is_set():
        sleep_s = base_sleep
        with _state_lock:
            if _watcher_state["consecutive_failures"] >= _FAIL_BACKOFF_THRESHOLD:
                sleep_s = _FAIL_BACKOFF_SLEEP_S
            _watcher_state["current_sleep_s"] = sleep_s
        if _watcher_state["stop_event"].wait(timeout=sleep_s):
            break
        now = time.time()
        if now - last_status_log >= _STATUS_LOG_EVERY_S:
            with _state_lock:
                snap = dict(_watcher_state)
            print(
                f"[COINGLASS] heartbeat runs={snap['total_runs']} "
                f"ok={snap['total_ok']} fail={snap['total_fail']} "
                f"consec_fail={snap['consecutive_failures']} "
                f"next_sleep={snap['current_sleep_s']}s",
                flush=True,
            )
            last_status_log = now
        try:
            ok, fail, remaining, used, limit = _run_once(client)
            with _state_lock:
                _watcher_state["total_runs"] += 1
                _watcher_state["total_ok"] += ok
                _watcher_state["total_fail"] += fail
                _watcher_state["last_run_ts"] = int(time.time())
                if fail == 0 and ok > 0:
                    _watcher_state["consecutive_failures"] = 0
                    _watcher_state["last_error"] = ""
                else:
                    _watcher_state["consecutive_failures"] += 1
                    if fail > 0:
                        _watcher_state["last_error"] = f"cycle fail={fail}"
                if remaining and remaining <= config.COINGLASS_RATE_WARN_REMAINING:
                    _watcher_state["current_sleep_s"] = base_sleep * 2
                else:
                    _watcher_state["current_sleep_s"] = base_sleep
            source_status.report(SOURCE_ID, ok > 0, latency_ms=None)
        except Exception as exc:
            print(f"[COINGLASS] cycle crashed: {exc}", flush=True)
            with _state_lock:
                _watcher_state["total_fail"] += 1
                _watcher_state["consecutive_failures"] += 1
                _watcher_state["last_error"] = str(exc)[:120]
            source_status.report(SOURCE_ID, False, error=str(exc)[:120])
    print("[COINGLASS] watcher stopped", flush=True)


def start_watcher() -> Optional[threading.Thread]:
    """启动后台 watcher 线程. 幂等: 已运行时不重启."""
    if not config.COINGLASS_ENABLED:
        print("[COINGLASS] disabled (COINGLASS_ENABLED=0)", flush=True)
        return None
    if not config.COINGLASS_API_KEY:
        print("[COINGLASS] disabled (API key missing)", flush=True)
        return None
    with _state_lock:
        existing = _watcher_state.get("thread")
        if existing and existing.is_alive():
            return existing
        _watcher_state["stop_event"].clear()
        t = threading.Thread(
            target=_watcher_loop, name="coinglass-lsr-watcher", daemon=True
        )
        t.start()
        _watcher_state["thread"] = t
        _watcher_state["started_at"] = int(time.time())
    print("[COINGLASS] watcher thread launched", flush=True)
    return t


def stop_watcher(timeout_s: float = 5.0) -> None:
    """停止 watcher (lifespan 关闭时调用)."""
    with _state_lock:
        t = _watcher_state.get("thread")
    if not t or not t.is_alive():
        return
    _watcher_state["stop_event"].set()
    t.join(timeout=timeout_s)


def watcher_status() -> Dict[str, Any]:
    """返回 watcher 实时状态 (供 API endpoint)."""
    with _state_lock:
        snap = dict(_watcher_state)
    thread = snap.get("thread")
    snap["thread_alive"] = bool(thread and thread.is_alive())
    snap.pop("thread", None)
    snap.pop("stop_event", None)
    return snap


def get_latest(
    *,
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """查询最新 N 条样本. symbol/mode 可选."""
    limit = max(1, min(200, int(limit)))
    where: List[str] = []
    params: List[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol.upper())
    if mode:
        where.append("mode = ?")
        params.append(mode)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, ts, source_ts, mode, symbol, ratio, ratio_pct, "
        "delta_2m, delta_2m_pct, delta_30m, delta_30m_pct, delta_4h, delta_4h_pct, "
        "ov_ratio, ov_long_qty, ov_short_qty, cache_age_s "
        f"FROM coinglass_lsr_samples{where_sql} "
        "ORDER BY ts DESC LIMIT ?"
    )
    params.append(limit)
    conn = db.get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    cols = [
        "id", "ts", "source_ts", "mode", "symbol", "ratio", "ratio_pct",
        "delta_2m", "delta_2m_pct", "delta_30m", "delta_30m_pct",
        "delta_4h", "delta_4h_pct", "ov_ratio", "ov_long_qty", "ov_short_qty",
        "cache_age_s",
    ]
    return [dict(zip(cols, row)) for row in rows]


def get_latest_samples_by_symbols(
    *,
    symbols: Tuple[str, ...] = ("BTC", "ETH", "SOL", "XAU"),
    mode: str = "trader",
    max_cache_age_s: int = 3600,
) -> Dict[str, Dict[str, Any]]:
    """v200+: 一次性取多个 symbol 的最新一条样本，按 symbol 返回 {symbol: sample}。

    用窗口子查询保证每个 symbol 只取最新一条（避免老数据混入）。
    对 microstructure_block 注入 prompt 用：避免 N 次单独查询。

    Args:
        symbols: 要取的品种列表（默认 BTC/ETH/SOL/XAU — 覆盖加密+贵金属）
        mode: "trader"(散户) | "whale"(大户)。trader 模式覆盖样本最广
        max_cache_age_s: 缓存新鲜度阈值；超过此值的样本视为 stale，返回时会带 stale=True

    Returns:
        {symbol: {ratio, delta_4h_pct, whale_long_qty, whale_short_qty, ov_long_qty,
                  ov_short_qty, oi_mcap, ts, cache_age_s, stale: bool}}
        symbol 不存在/无样本 → 不在 dict 中（不报错）
    """
    if not symbols:
        return {}
    syms = tuple(str(s).strip().upper() for s in symbols if s)
    if not syms:
        return {}
    placeholders = ",".join("?" * len(syms))
    conn = db.get_connection()
    sql = (
        "SELECT s.symbol, s.ratio, s.ratio_pct, "
        "       s.delta_2m, s.delta_2m_pct, s.delta_30m, s.delta_30m_pct, "
        "       s.delta_4h, s.delta_4h_pct, "
        "       s.ov_ratio, s.ov_long_qty, s.ov_short_qty, "
        "       s.ov_long_whales_qty, s.ov_short_whales_qty, "
        "       s.oi_mcap_ratio, s.ts, s.cache_age_s "
        "FROM coinglass_lsr_samples s "
        "JOIN ("
        f"  SELECT symbol, MAX(ts) AS max_ts FROM coinglass_lsr_samples "
        f"  WHERE mode = ? AND symbol IN ({placeholders}) GROUP BY symbol"
        ") m ON m.symbol = s.symbol AND m.max_ts = s.ts "
        f"WHERE s.mode = ? AND s.symbol IN ({placeholders})"
    )
    params = (mode,) + syms + (mode,) + syms
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = str(r[0] or "").upper()
        if not sym:
            continue
        cache_age = int(r[16] or 0) if r[16] is not None else 0
        out[sym] = {
            "symbol": sym,
            "ratio": float(r[1] or 0.0),
            "ratio_pct": str(r[2] or ""),
            "delta_2m": float(r[3] or 0.0),
            "delta_2m_pct": str(r[4] or ""),
            "delta_30m": float(r[5] or 0.0),
            "delta_30m_pct": str(r[6] or ""),
            "delta_4h": float(r[7] or 0.0),
            "delta_4h_pct": str(r[8] or ""),
            "ov_ratio": float(r[9] or 0.0),
            "ov_long_qty": float(r[10] or 0.0),
            "ov_short_qty": float(r[11] or 0.0),
            "whale_long_qty": float(r[12] or 0.0),
            "whale_short_qty": float(r[13] or 0.0),
            "oi_mcap": float(r[14] or 0.0),
            "ts": int(r[15] or 0),
            "cache_age_s": cache_age,
            "stale": cache_age > max_cache_age_s,
            "source": "coinglass_lsr",
        }
    return out


def get_history(
    *,
    symbol: str,
    mode: str = "trader",
    since_ts: int,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """历史曲线查询: since_ts 之后的所有样本 (用于趋势分析)."""
    limit = max(1, min(5000, int(limit)))
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT ts, source_ts, ratio, delta_2m, delta_30m, delta_4h, "
        "ratio_pct, delta_2m_pct, delta_30m_pct, delta_4h_pct, cache_age_s "
        "FROM coinglass_lsr_samples "
        "WHERE symbol = ? AND mode = ? AND ts >= ? "
        "ORDER BY ts ASC LIMIT ?",
        (symbol.upper(), mode, int(since_ts), limit),
    ).fetchall()
    conn.close()
    cols = [
        "ts", "source_ts", "ratio", "delta_2m", "delta_30m", "delta_4h",
        "ratio_pct", "delta_2m_pct", "delta_30m_pct", "delta_4h_pct",
        "cache_age_s",
    ]
    return [dict(zip(cols, row)) for row in rows]


def get_ranking(
    *,
    mode: str = "trader",
    sort: str = "delta_2m",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    since_minutes: int = 30,
) -> List[Dict[str, Any]]:
    """排行榜: 每个 (mode, symbol) 在 since_minutes 窗口内最新一条, 按 sort 排序.

    用窗口子查询保证每个 symbol 只出一条最新样本, 避免老数据混入.
    sort 字段: ratio / delta_2m / delta_30m / delta_4h / ts.
    """
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    since_minutes = max(1, min(1440, int(since_minutes)))
    since_ts = int(time.time()) - since_minutes * 60
    direction = "DESC" if order == "desc" else "ASC"
    conn = db.get_connection()
    sql = (
        "SELECT s.symbol, s.mode, s.ts, s.source_ts, "
        "       s.ratio, s.ratio_pct, "
        "       s.delta_2m, s.delta_2m_pct, "
        "       s.delta_30m, s.delta_30m_pct, "
        "       s.delta_4h, s.delta_4h_pct, "
        "       s.ov_long_qty, s.ov_short_qty, s.cache_age_s "
        "FROM coinglass_lsr_samples s "
        "JOIN ("
        "  SELECT symbol, MAX(ts) AS max_ts "
        "  FROM coinglass_lsr_samples "
        "  WHERE mode = ? AND ts >= ? "
        "  GROUP BY symbol"
        ") m ON m.symbol = s.symbol AND m.max_ts = s.ts "
        "WHERE s.mode = ? AND s.ts >= ? "
        f"ORDER BY s.{sort} {direction} "
        "LIMIT ? OFFSET ?"
    )
    rows = conn.execute(
        sql, (mode, since_ts, mode, since_ts, limit, offset)
    ).fetchall()
    conn.close()
    cols = [
        "symbol", "mode", "ts", "source_ts",
        "ratio", "ratio_pct",
        "delta_2m", "delta_2m_pct",
        "delta_30m", "delta_30m_pct",
        "delta_4h", "delta_4h_pct",
        "ov_long_qty", "ov_short_qty", "cache_age_s",
    ]
    return [dict(zip(cols, row)) for row in rows]


def get_health_summary(*, since_ts: Optional[int] = None) -> Dict[str, Any]:
    """健康度摘要 (供面板 / 健康端点)."""
    if since_ts is None:
        since_ts = int(time.time()) - 3600
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), "
        "MAX(latency_ms), "
        "AVG(latency_ms), "
        "MAX(rate_remaining), "
        "MIN(rate_remaining), "
        "MAX(key_expires_at) "
        "FROM coinglass_lsr_health WHERE ts >= ?",
        (int(since_ts),),
    ).fetchone()
    last_error_row = conn.execute(
        "SELECT error_kind, error_msg, ts FROM coinglass_lsr_health "
        "WHERE ok=0 AND ts >= ? ORDER BY ts DESC LIMIT 1",
        (int(since_ts),),
    ).fetchone()
    conn.close()
    if not row:
        return {"since_ts": int(since_ts), "total": 0}
    (total, ok_c, fail_c, max_lat, avg_lat, max_rem, min_rem, expires) = row
    summary = {
        "since_ts": int(since_ts),
        "total": int(total or 0),
        "ok": int(ok_c or 0),
        "fail": int(fail_c or 0),
        "ok_rate": round(int(ok_c or 0) / max(int(total or 0), 1), 3),
        "max_latency_ms": int(max_lat or 0),
        "avg_latency_ms": int(avg_lat or 0),
        "min_remaining": int(min_rem or 0),
        "max_remaining": int(max_rem or 0),
        "key_expires_at": str(expires or ""),
        "last_error": (
            {
                "kind": str(last_error_row[0] or ""),
                "msg": str(last_error_row[1] or "")[:160],
                "ts": int(last_error_row[2] or 0),
            }
            if last_error_row
            else None
        ),
    }
    summary["watcher"] = watcher_status()
    return summary