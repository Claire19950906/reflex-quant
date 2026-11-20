#!/usr/bin/env python3
"""
Trident Agent MVP — Local Time-Series Store
============================================

本地时序持久化层，建在 db.py 的三张时序表之上：

  - market_ticks       行情 tick（多交易所中位数价 + 24h 涨跌）
  - factor_snapshots   多因子分与统一置信度快照（含卡方检验结果）
  - signal_performance 已结算信号的前向表现（pnl / mfe / mae）

时间轴统一用 INTEGER epoch 秒 `ts`，配合 (symbol|asset, ts) 复合索引做区间扫描。
OHLC 重采样直接用 SQL 按桶分组完成，不把原始 tick 拉进 Python。

schema 归 db.py 管，本模块只做读写，不含任何 CREATE/ALTER。
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import db

# 支持的重采样粒度（秒）
BUCKET_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# 默认保留期：超过该天数的 tick 会被 prune_market_ticks 清掉
# v466：90 → 15 天。tick 是秒级旁路采样，回测走 kline_bars / ohlcv_daily，
# 15 天滚动窗口足以覆盖近期行情复盘；与 scripts/retention_prune.py 的 market_ticks 窗口保持一致。
DEFAULT_RETENTION_DAYS = 15


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    connection = db.get_connection()
    connection.row_factory = sqlite3.Row
    return connection


def _bucket_seconds(interval: str) -> int:
    if interval not in BUCKET_SECONDS:
        raise ValueError(f"unsupported interval: {interval}")
    return BUCKET_SECONDS[interval]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

# ──────────────────────────────────────────────────────────────────────────────
# v440 P0-0e：行情采样异步写（单写线程 + 队列）
#
# 事故（2026-11-17 22:05 长事务哨兵）：
#   pid=9076 api_server._write_tick_sync → record_market_snapshot  31.6s commit
# 31.6s 里几乎没有"写"的时间，全是 db.retry_on_busy 的退避等待：
# 7 个品种每隔几秒各写一次 tick + 快照，和引擎批次写抢同一把写锁，谁抢到谁等。
#
# 这类数据是"采样"：丢几条不影响任何决策（tick 每分钟几十条，快照是旁路历史）。
# 让它排队去抢锁不值得 —— 改为入队即返回，由**一个**专用线程顺序落库，
# 天然只有一个写入者，不再和引擎/门卫抢。
# ──────────────────────────────────────────────────────────────────────────────
_MARKET_Q: "queue.Queue[dict]" = None  # type: ignore[assignment]
_MARKET_SYNC: bool = False          # 测试/单次脚本可置 True 强制同步写
_MARKET_STATS = {"written": 0, "failed": 0, "dropped": 0}
_MARKET_Q_MAX = int(os.getenv("TRIDENT_MARKET_Q_MAX", "500"))


def _market_worker() -> None:
    while True:
        payload = _MARKET_Q.get()
        if payload is None:
            break
        try:
            record_market_snapshot(payload)
            _MARKET_STATS["written"] += 1
        except Exception as _e:
            # 采样写失败直接丢（不能把线程弄死，更不能把调用方拖住）
            _MARKET_STATS["failed"] += 1
            # v483: 丢了要留账。原来是纯计数，进程一重启就归零，行情缺口永远查不出来。
            try:
                import silent_ledger
                silent_ledger.record(
                    "market_ticks.write",
                    f"{type(_e).__name__}: {str(_e)[:120]}",
                )
            except Exception:
                pass
        finally:
            try:
                _MARKET_Q.task_done()
            except Exception:
                pass


def _market_queue() -> "queue.Queue[dict]":
    global _MARKET_Q
    if _MARKET_Q is None:
        _MARKET_Q = queue.Queue(maxsize=_MARKET_Q_MAX)
        threading.Thread(target=_market_worker, name="market-tick-writer", daemon=True).start()
    return _MARKET_Q


def enqueue_market_snapshot(payload: Dict[str, Any]) -> bool:
    """入队一条行情采样并立刻返回。返回 False 表示未入队（调用方已在同步路径写过）。

    队列满 / 未启用异步 → 返回 False，由 record_market_snapshot 同步兜底（不丢采样）。
    """
    if _MARKET_SYNC or not payload or not payload.get("items"):
        return False
    if os.getenv("TRIDENT_MARKET_ASYNC_WRITE", "1") != "1":
        return False
    try:
        q = _market_queue()
    except Exception:
        return False
    try:
        q.put_nowait(payload)
        return True
    except Exception:
        _MARKET_STATS["dropped"] += 1
        return False


def flush_market_writes(timeout: float = 10.0) -> Dict[str, int]:
    """把队列里的行情采样刷完（进程退出前/测试用）。"""
    if _MARKET_Q is not None:
        try:
            _MARKET_Q.join()
        except Exception:
            pass
    return dict(_MARKET_STATS)


def market_writer_stats() -> Dict[str, int]:
    st = dict(_MARKET_STATS)
    try:
        st["queued"] = _MARKET_Q.qsize() if _MARKET_Q is not None else 0
    except Exception:
        st["queued"] = -1
    return st


def record_market_snapshot(payload: Dict[str, Any],
                           connection: Optional[sqlite3.Connection] = None) -> int:
    """把一次 /api/market/prices 聚合结果落成 tick。

    同一 (symbol, ts) 重复写入会被 UNIQUE 约束吞掉（幂等），返回实际写入条数。
    """
    items = payload.get("items") or []
    if not items:
        return 0
    ts = _now()
    # v489：change24h 允许 NULL —— 取不到昨收就是"不知道"，不能写成 0.00%。
    # 旧写法 `float(item.get("change24h") or 0.0)` 会把 None 变成 0，前端于是把
    # "没数据"显示成"今天没动"（实测 WTI 15.5 万条 100% 是假的 0）。
    rows = [
        (str(item["asset"]).upper(), ts, float(item["price"]),
         (float(item["change24h"]) if item.get("change24h") is not None else None),
         str(item.get("source") or "median"), int(item.get("sourceCount") or 0))
        for item in items
    ]
    owned = connection is None
    connection = connection or _connect()
    try:
        # v189-6: 单 INSERT 多 VALUES + 单 commit。
        #   v189-5 用 7×execute+commit，每个 commit 触发 fsync → 持锁 ~10-30ms ×7 = 70-200ms
        #   还是会让其他 writer 等几十 ms；用单 INSERT 多 VALUES 让 SQLite 单事务原子处理,
        #   总持锁 ≈ 5-10ms（一次 INSERT 解析 + 一次 commit fsync）。
        #   数据语义不变: OR IGNORE 让同一 (symbol, ts) 幂等。
        placeholders = ",".join(["(?,?,?,?,?,?)"] * len(rows))
        params: List[Any] = []
        for row in rows:
            params.extend(row)
        cur = connection.execute(
            f"""INSERT OR IGNORE INTO market_ticks(symbol, ts, price, change24h, source, source_count)
                VALUES {placeholders}""",
            params,
        )
        if owned:
            connection.commit()
        return max(0, cur.rowcount or 0)
    finally:
        if owned:
            connection.close()


def record_factor_snapshot(news_id: int, asset: str, analysis: Dict[str, Any],
                           connection: Optional[sqlite3.Connection] = None) -> int:
    """把一次多因子 + 证据校验结果落成时序快照，返回新行 id。"""
    significance = analysis.get("significance") or {}
    row = (
        int(news_id), str(asset or "NONE").upper(), _now(),
        float(analysis.get("final_score") or 0.0),
        float(analysis.get("confidence") or 0.0),
        str(analysis.get("raw_action") or "HOLD"),
        str(analysis.get("action") or "HOLD"),
        float(significance.get("p_value") or 1.0),
        float(significance.get("chi_square") or 0.0),
        int(significance.get("sample_size") or 0),
        len(analysis.get("contradictions") or []),
        json.dumps({name: item.get("score") for name, item in (analysis.get("factors") or {}).items()}, ensure_ascii=False),
        json.dumps({name: item.get("evidence_score") for name, item in (analysis.get("evidence") or {}).items()}, ensure_ascii=False),
    )
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute(
            """INSERT INTO factor_snapshots(news_id, asset, ts, final_score, confidence, raw_action,
                                            gated_action, p_value, chi_square, sample_size,
                                            contradictions, factors, evidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        if owned:
            connection.commit()
        return int(cursor.lastrowid)
    finally:
        if owned:
            connection.close()


def record_news_event(
    news_id: int,
    source: str = "",
    is_noise: int = 0,
    status: str = "PENDING",
    decision_id: Optional[int] = None,
    asset: str = "NONE",
    action: str = "HOLD",
    score: float = 0.0,
    ts: Optional[int] = None,
    content: Optional[str] = None,
    assets: Optional[str] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """把一条新闻/信号按时间戳写入 SQLite 时序表 news_events。

    v310 修复：补写 headline / body / assets / fetched_at 四个旧列。
    历史病根 —— news_events 被迁到"新 schema"时丢了这四列，db.py 的 v300 修复
    只用 ADD COLUMN 把列补了回来，但本函数的 INSERT 一直没写它们，于是所有
    新入库的新闻这四列恒为 '' / 0：SSE 事件流、新闻列表拿不到正文，
    "三条新闻入库全部失败"的症状在列补回来之后依然存在（只是不再报
    "no such column" 而是拿到空串，静默降级）。

    内容来源优先级：
      1. 调用方显式传入 content / assets（最准，调用方手上就有原文）；
      2. 否则按 news_id 从 raw_news 回查 content / gate_assets
         （写入方即上游，raw_news.id == news_events.news_id）。

    截断口径与 db_maintenance.backfill_news_events_legacy 保持一致：
      headline = content 前 300 字，body = 后续 2000 字，assets = gate_assets。
    """
    owned = connection is None
    connection = connection or _connect()
    try:
        ts_value = int(ts or _now())
        if content is None or assets is None:
            try:
                row = connection.execute(
                    "SELECT content, gate_assets FROM raw_news WHERE id = ?",
                    (int(news_id),),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None:
                if content is None:
                    content = row[0] or ""
                if assets is None:
                    assets = row[1] or ""
        content_text = str(content or "")
        cursor = connection.execute(
            """INSERT OR REPLACE INTO news_events(
                   news_id, decision_id, ts, source, asset, action, score, is_noise, status,
                   headline, body, assets, fetched_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(news_id),
                decision_id,
                ts_value,
                str(source or ""),
                str(asset or "NONE").upper(),
                # v314: action 枚举归一 (见 db.normalize_news_action) —— 原来只做
                # .upper(), 'E020'/'bullish' 这类非法值会直接落库 (验收 C8 FAIL),
                # None 也会落成 NULL (验收 #13 FAIL)。
                db.normalize_news_action(action),
                float(score or 0.0),
                int(is_noise or 0),
                str(status or "PENDING"),
                content_text[:300],
                content_text[300:2300],
                db.normalize_news_assets(assets),
                ts_value,
            ),
        )
        if owned:
            connection.commit()
        return int(cursor.lastrowid)
    finally:
        if owned:
            connection.close()


def query_news_events(
    start: Optional[int] = None,
    end: Optional[int] = None,
    asset: Optional[str] = None,
    limit: int = 2000,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """按时间区间读取新闻信号时序。"""
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT news_id, decision_id, ts, source, asset, action, score, is_noise, status
                 FROM news_events WHERE ts BETWEEN ? AND ?"""
        params: List[Any] = [start, end]
        if asset:
            sql += " AND asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 20000)))
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        if owned:
            connection.close()


def record_signal_performance(rows: Iterable[Dict[str, Any]],
                              connection: Optional[sqlite3.Connection] = None) -> int:
    """把已结算信号的前向表现落成时序点，(decision_id, ts) 幂等。"""
    ts = _now()
    values = [
        (int(row["decision_id"]), str(row.get("asset") or "NONE").upper(), ts,
         str(row.get("action") or "HOLD").upper(), str(row.get("is_correct") or ""),
         row.get("forward_pnl"), row.get("mfe_pct"), row.get("mae_pct"))
        for row in rows
    ]
    if not values:
        return 0
    owned = connection is None
    connection = connection or _connect()
    try:
        # v189-6: 单 INSERT 多 VALUES 替 executemany。
        # executemany + 单 commit 在 N 大时持锁 ~N×5ms; 多 VALUES 单 INSERT 让 SQLite
        # 单事务原子完成,持锁 ≈ 5-10ms 一次。
        placeholders = ",".join(["(?,?,?,?,?,?,?,?)"] * len(values))
        params: List[Any] = []
        for v in values:
            params.extend(v)
        cursor = connection.execute(
            f"""INSERT OR IGNORE INTO signal_performance(decision_id, asset, ts, action, is_correct,
                                                         forward_pnl, mfe_pct, mae_pct)
                VALUES {placeholders}""",
            params,
        )
        connection.commit()
        return cursor.rowcount
    finally:
        if owned:
            connection.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_ticks(symbol: str, start: Optional[int] = None, end: Optional[int] = None,
                limit: int = 500, connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """按时间区间取原始 tick，返回按时间升序排列的列表。"""
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT symbol, ts, price, change24h, source, source_count
               FROM market_ticks WHERE symbol=? AND ts BETWEEN ? AND ?
               ORDER BY ts DESC LIMIT ?""",
            (symbol.upper(), start, end, max(1, min(limit, 5000))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        if owned:
            connection.close()


def resample_ohlc(symbol: str, interval: str = "1m", limit: int = 200,
                  start: Optional[int] = None, end: Optional[int] = None,
                  connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """把 tick 按时间桶下采样成 OHLC，聚合完全在 SQL 内完成。

    open/close 用桶内首末 tick 的价格（按 ts 排序），high/low 用极值。
    """
    bucket = _bucket_seconds(interval)
    end = end if end is not None else _now()
    start = start if start is not None else 0
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT (ts / ?) * ? AS bucket_ts,
                      MIN(price) AS low, MAX(price) AS high,
                      COUNT(*) AS ticks,
                      (SELECT price FROM market_ticks o
                        WHERE o.symbol=m.symbol AND (o.ts / ?) * ? = (m.ts / ?) * ?
                        ORDER BY o.ts ASC LIMIT 1) AS open,
                      (SELECT price FROM market_ticks c
                        WHERE c.symbol=m.symbol AND (c.ts / ?) * ? = (m.ts / ?) * ?
                        ORDER BY c.ts DESC LIMIT 1) AS close
               FROM market_ticks m
               WHERE m.symbol=? AND m.ts BETWEEN ? AND ?
               GROUP BY bucket_ts ORDER BY bucket_ts DESC LIMIT ?""",
            (bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket, bucket,
             symbol.upper(), start, end, max(1, min(limit, 2000))),
        ).fetchall()
        return [
            {"ts": row["bucket_ts"], "open": row["open"], "high": row["high"],
             "low": row["low"], "close": row["close"], "ticks": row["ticks"]}
            for row in reversed(rows)
        ]
    finally:
        if owned:
            connection.close()


def query_factor_history(asset: Optional[str] = None, limit: int = 100,
                         connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """取多因子置信度时序，用于回溯因子有效性衰减。"""
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT news_id, asset, ts, final_score, confidence, raw_action, gated_action,
                        p_value, chi_square, sample_size, contradictions, factors, evidence
                 FROM factor_snapshots"""
        params: List[Any] = []
        if asset:
            sql += " WHERE asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        rows = connection.execute(sql, params).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["factors"] = json.loads(item["factors"] or "{}")
            item["evidence"] = json.loads(item["evidence"] or "{}")
            result.append(item)
        return result
    finally:
        if owned:
            connection.close()


def rolling_winrate(asset: Optional[str] = None, window: int = 20,
                    connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """按最近 `window` 条已结算信号算滚动胜率与累计盈亏。"""
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT is_correct, forward_pnl FROM signal_performance"""
        params: List[Any] = []
        if asset:
            sql += " WHERE asset=?"
            params.append(asset.upper())
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(window, 1000)))
        rows = connection.execute(sql, params).fetchall()
        total = len(rows)
        wins = sum(1 for row in rows if row["is_correct"] == "WIN")
        losses = sum(1 for row in rows if row["is_correct"] == "LOSS")
        decided = wins + losses
        pnl = sum(row["forward_pnl"] or 0.0 for row in rows)
        return {
            "asset": (asset or "ALL").upper(),
            "window": total,
            "wins": wins,
            "losses": losses,
            "decided": decided,
            "winrate": round(wins / decided, 4) if decided else 0.0,
            "cumulative_pnl": round(pnl, 4),
        }
    finally:
        if owned:
            connection.close()


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------

def prune_market_ticks(retention_days: int = DEFAULT_RETENTION_DAYS,
                       connection: Optional[sqlite3.Connection] = None) -> int:
    """删除超过保留期的 tick，返回删除条数。因子与信号表不清理（样本量本身就是资产）。"""
    cutoff = _now() - max(1, retention_days) * 86400
    owned = connection is None
    connection = connection or _connect()
    try:
        cursor = connection.execute("DELETE FROM market_ticks WHERE ts < ?", (cutoff,))
        connection.commit()
        return cursor.rowcount
    finally:
        if owned:
            connection.close()
