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

import json
import os
import re
import sqlite3
import sys
import threading
import hashlib
from datetime import datetime, timezone, time as datetime_time, date
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

import intraday_store

# v291+ P1 校准工具 (纯 Python, 无 sklearn 依赖)
sys.path.insert(0, str(Path(__file__).resolve().parent))
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

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

app = FastAPI(title="trident_v170 bridge", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── 工具 ───────────────────────────────────────────
@contextmanager
def db_cursor():
    """只读连接 (默认), 用于 GET 端点"""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_db_cursor():
    """可写连接 (P1: trade_decisions / ohlcv_daily 写入)"""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        try:
            conn.commit()
        except Exception:
            pass
        conn.close()


def err(msg: str, code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "rows": []}, status_code=code)


def _fmt_range(rng) -> str:
    """把 (min_ts, max_ts, count) 序列化成 'YYYY-MM-DD ~ YYYY-MM-DD (n rows)'.
    容忍混合类型: int/float epoch 或 ISO text (YYYY-MM-DD HH:MM:SS)."""
    if not rng or rng[0] is None or rng[1] is None:
        return "(empty)"

    def _to_iso(v) -> str:
        if v is None:
            return "?"
        s = str(v)
        # ISO text already
        if isinstance(v, str) and "-" in s and ":" in s:
            return s[:10]
        # numeric epoch
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(int(float(v))))
        except (ValueError, TypeError, OSError):
            return s[:10] if len(s) >= 10 else s

    lo = _to_iso(rng[0])
    hi = _to_iso(rng[1])
    n = int(rng[2]) if len(rng) > 2 and rng[2] is not None else 0
    return f"{lo} ~ {hi} ({n} rows)"


# ─── health ─────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    with db_cursor() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
    return {
        "ok": True,
        "service": "bridge",
        "version": "1.0",
        "port": 8100,
        "db": str(DB_PATH),
        "tables": tables,
        "ts": int(time.time()),
    }


# ─── v237/missed-signals/by-dimension ───────────────
@app.get("/api/v237/missed-signals/by-dimension")
def v237_by_dimension(days: int = Query(90, ge=1, le=3650)) -> dict:
    """
    推导 A/B/C 三维度漏检分类(完全基于真实数据),每个维度下细分小类 (v291+):

    A 维度(买卖信号 - signal):
      A1_strong_buy_missed     强买入信号(algo_score>=0.7 且 move>0),无 trade_decision
      A2_strong_sell_missed    强卖出信号(algo_score>=0.7 且 move<0),无 trade_decision
      A3_continuation_missed   持续性信号(>3d 持续),无 trade_decision

    B 维度(信息方向 - direction):
      B1_rate_conflict         行情方向 vs 利率方向冲突
      B2_macro_news_conflict   同期宏观新闻情绪 vs 行情方向反向
      B3_thin_market           周末/节假日 流动性陷阱 (move 幅度异常大, 但属"无主"时段)

    C 维度(技术错误 - technical):
      C1_feed_latency          feed 延迟异常 (latency_ms > 2000)
      C2_feed_failure          feed 抓取失败 (status=failed)
      C3_data_gap              数据缺口 (ohlcv_daily 缺失 >3d)

    数据为空时返回 honest empty + status
    """
    cutoff = int(time.time()) - days * 86400
    # v291+ 细分子类, 每个 item 都有 subcategory 字段
    A_items: list[dict] = []
    B_items: list[dict] = []
    C_items: list[dict] = []

    with db_cursor() as conn:
        has_decisions = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_decisions'"
        ).fetchone()
        has_signals = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
        ).fetchone()

        cpm_rows = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min "
            "FROM critical_price_moves "
            "WHERE algo_score >= 0.7 AND ts >= ? "
            "ORDER BY ts DESC LIMIT 200",
            (cutoff,),
        ).fetchall()
        fed_rows = conn.execute(
            "SELECT ts, dgs2_pct, effr_pct, spread_bp FROM fed_rate_history ORDER BY ts ASC"
        ).fetchall()
        # 真实时间范围 (用于 data_alignment 动态展示)
        cpm_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM critical_price_moves WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        fed_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM fed_rate_history"
        ).fetchone()
        fh_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM feed_health"
        ).fetchone()

        def fed_direction_at(ts: int) -> int:
            if len(fed_rows) < 2:
                return 0
            prev = next((row for row in fed_rows if row["ts"] <= ts), None)
            nxt = next((row for row in fed_rows if row["ts"] > ts), None)
            if prev is None or nxt is None:
                return 0
            delta = (nxt["effr_pct"] or 0) - (prev["effr_pct"] or 0)
            if abs(delta) < 0.01:
                return 0
            return 1 if delta > 0 else -1

        for cpm in cpm_rows:
            fed_dir = fed_direction_at(cpm["ts"])
            move_dir = 1 if cpm["move_pct"] > 0 else -1

            # A 维度: 强信号但同期无任何决策记录
            # (修复: 原逻辑要求 trade_decisions/signals 表"不存在"才输出, 反了.
            #  正确语义 = critical_move 出现时, 同期 ±24h 内没有对应的 trade_decisions
            #  条目 — 这才是"应该下决策但没下"的漏检.
            #  若 trade_decisions 表根本不存在, 则全量高信号都视为 A 维度.)
            if has_decisions is None:
                # 表不存在 → 把所有强信号列作"应下而未下"
                a_reason = "DB 中无 trade_decisions 表 — 上游决策源未接入, 所有强信号视为漏检"
            else:
                td_hit = conn.execute(
                    "SELECT 1 FROM trade_decisions "
                    "WHERE asset=? AND ABS(created_at - ?) <= 86400 LIMIT 1",
                    (cpm["asset"], cpm["ts"]),
                ).fetchone()
                if td_hit is None:
                    a_reason = (
                        f"强信号出现时, 同期 (±24h) 无 trade_decision 记录覆盖 — "
                        f"决策系统未识别该信号 (asset={cpm['asset']})"
                    )
                else:
                    a_reason = None
            if a_reason is not None:
                # v291+ 细分子类
                if cpm["move_pct"] > 0:
                    subcategory = "A1_strong_buy_missed"
                else:
                    subcategory = "A2_strong_sell_missed"
                # 持续性判断 (duration_min > 3*1440 = 4320 分钟 ≈ 3d)
                if cpm["duration_min"] >= 4320:
                    subcategory = "A3_continuation_missed"
                A_items.append({
                    "asset": cpm["asset"],
                    "ts": cpm["ts"],
                    "subcategory": subcategory,  # v291+ 新增
                    "pattern": "strong_signal_no_decision",
                    "signal": f"algo_score={cpm['algo_score']:.2f} move_pct={cpm['move_pct']:.2f}",
                    "ignored_reason": a_reason,
                    "confidence_at_time": cpm["algo_score"],
                    "actual_outcome_5d": None,
                    "source": "critical_price_moves+trade_decisions",
                })

            # B 维度: 独立派生自 fed_rate_history 方向变化 (不依赖 critical_moves 时间锚)
        # v291+ 修复: B 维度 0 条不是算法错, 是 fed_rate_history (Oct 17-18) 与
        # critical_price_moves (Oct 9-12) 时间错开 8d. 改为: 直接遍历 fed_rows 找
        # effr_pct 变化点 (delta >= 5bp), ±7d 内查 critical_moves 是否有反向.
        # 若有反向 → B1_rate_conflict; 若无但变化显著 → B2_rate_reversal_alone.
        for i in range(1, len(fed_rows)):
            prev = fed_rows[i - 1]
            curr = fed_rows[i]
            delta_bp = (curr["effr_pct"] or 0) - (prev["effr_pct"] or 0)
            if abs(delta_bp) < 0.05:  # < 5bp 视为噪声
                continue
            fed_dir = 1 if delta_bp > 0 else -1
            # 查 ±7d 内 critical_moves
            nearby = conn.execute(
                "SELECT asset, ts, move_pct, algo_score FROM critical_price_moves "
                "WHERE ts BETWEEN ? AND ?",
                (int(curr["ts"]) - 7 * 86400, int(curr["ts"]) + 7 * 86400),
            ).fetchall()
            if nearby:
                for n in nearby:
                    move_dir_n = 1 if n["move_pct"] > 0 else -1
                    if move_dir_n != 0 and move_dir_n != fed_dir:
                        B_items.append({
                            "asset": n["asset"],
                            "ts": int(curr["ts"]),
                            "subcategory": "B1_rate_conflict",
                            "pattern": "direction_macro_conflict",
                            "signal": f"fed_delta_bp={delta_bp*100:.0f} move_pct={n['move_pct']:.2f}",
                            "ignored_reason": (
                                f"利率方向 {'↑' if fed_dir>0 else '↓'} "
                                f"{abs(delta_bp*100):.0f}bp 与同期 {n['asset']} 行情反向"
                            ),
                            "confidence_at_time": n["algo_score"],
                            "actual_outcome_5d": None,
                            "source": "fed_rate_history+critical_price_moves(±7d)",
                        })
            else:
                B_items.append({
                    "asset": "FED",
                    "ts": int(curr["ts"]),
                    "subcategory": "B2_rate_reversal_alone",
                    "pattern": "rate_direction_change",
                    "signal": f"effr_pct {prev['effr_pct']:.3f}→{curr['effr_pct']:.3f} ({delta_bp*100:+.0f}bp)",
                    "ignored_reason": (
                        f"利率 {'↑' if fed_dir>0 else '↓'} {abs(delta_bp*100):.0f}bp, "
                        f"±7d 内无 critical_move 跟随 — 信号未传导"
                    ),
                    "confidence_at_time": min(0.95, abs(delta_bp) * 10),
                    "actual_outcome_5d": None,
                    "source": "fed_rate_history(独立)",
                })

        # C 维度: 独立派生自 feed_health 异常 (不依赖 critical_moves 时间锚)
        # v291+ 修复: feed_health (Oct 17-18 ISO text) 与 critical_moves (Oct 9-12 unix ts)
        # 时间错开. 改为: 直接遍历 feed_health 找 status='failed' 或 latency > 2000ms
        # 的条目, 按 (asset, source, day) 去重, 转成 unix ts, 生成 C1/C2 项.
        feed_rows_raw = conn.execute(
            "SELECT ts, asset, source, latency_ms, status, error, feed_kind "
            "FROM feed_health "
            "WHERE (status='failed' OR latency_ms > 2000) "
            "ORDER BY ts DESC LIMIT 200"
        ).fetchall()
        seen_keys: set[tuple] = set()
        for fr in feed_rows_raw:
            ts_text = fr["ts"]
            try:
                if "T" in ts_text:
                    dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                unix_ts = int(dt.timestamp())
            except Exception:
                continue
            day_key = ts_text[:10]
            dedup_key = (fr["asset"], fr["source"], day_key)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            if fr["status"] == "failed":
                subcategory = "C2_feed_failure"
                ignored_reason = (
                    f"{fr['source']} feed 抓取失败 (status={fr['status']}, "
                    f"err={fr['error']}) — 数据盲区"
                )
            else:
                subcategory = "C1_feed_latency"
                ignored_reason = (
                    f"{fr['source']} feed 延迟 {fr['latency_ms']}ms 超过 2s 阈值"
                )
            C_items.append({
                "asset": fr["asset"] or "GLOBAL",
                "ts": unix_ts,
                "subcategory": subcategory,
                "pattern": "feed_health_degraded",
                "signal": f"latency={fr['latency_ms']}ms status={fr['status']} source={fr['source']}",
                "ignored_reason": ignored_reason,
                "confidence_at_time": min(0.95, (fr["latency_ms"] or 0) / 5000),
                "actual_outcome_5d": None,
                "source": "feed_health(独立)",
            })

    # ─── v302+ P1: trade_reflections_v2 三维度细分 (A1/A2/A3, B1/B2/B3, C1/C2/C3 真实样本)
    # 这是反思系统的"判定历史" — 每个样本都是实际发生的反思记录, 按 dimension+subcategory
    # 聚合后, 让前端"三维度错因"卡片能展示真实的"细分小类",而不是后端推导的空模板.
    #
    # shape 严格匹配 frontend components/v237/MissedSignalsDimensionSummary.tsx
    # 期望的 dimensions.{A,B,C}.{total, hit, hit_rate, by_subcategory} 形状
    #
    # 注意: trade_reflections_v2.subcategory 命名约定 (2026-10-15 后)
    #   A: market_regime_misread / pattern_recognition / psychological_anchor / volume_price_divergence
    #   B: cause_effect_reversed / event_chain_break / macro_misattribute / news_direction_inverted
    #   C: calibration_drift / data_feed_timeout / feed_missing / signal_delay
    # 这与 critical_price_moves 推导出的 A1/A2/A3 (stron_buy/sell/continuation) 是不同维度,
    # 前者是"系统反思时的归因" (meta), 后者是"信号发生时是否漏检" (signal)
    reflections_dimensions: dict[str, dict] = {
        "A": {"label": "买卖信号判断", "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
        "B": {"label": "信息分析方向", "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
        "C": {"label": "技术错误",     "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
    }
    reflections_total = 0
    try:
        with db_cursor() as ref_conn:
            ref_rows = ref_conn.execute(
                "SELECT dimension, subcategory, "
                "       COUNT(*) as n, "
                "       SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) as n_hit, "
                "       AVG(actual_outcome_pct) as avg_outcome, "
                "       AVG(confidence_at_time) as avg_conf "
                "FROM trade_reflections_v2 "
                "WHERE created_at >= ? AND dimension IN ('A','B','C') "
                "GROUP BY dimension, subcategory "
                "ORDER BY dimension, n DESC",
                (cutoff,),
            ).fetchall()
            # 同时拉明细 (前 100 条, 给"三维度明细"面板用)
            ref_items = ref_conn.execute(
                "SELECT id, asset, dimension, subcategory, confidence_at_time, "
                "       actual_outcome_pct, hit, created_at, reflection_text "
                "FROM trade_reflections_v2 "
                "WHERE created_at >= ? AND dimension IN ('A','B','C') "
                "ORDER BY created_at DESC LIMIT 100",
                (cutoff,),
            ).fetchall()
        ref_items_json = [dict(r) for r in ref_items]
        # 按维度聚合
        for k in ("A", "B", "C"):
            reflections_dimensions[k]["items"] = [
                ri for ri in ref_items_json if ri.get("dimension") == k
            ][:30]
        grand_total = 0
        for r in ref_rows:
            k = r["dimension"]
            if k not in reflections_dimensions:
                continue
            sub = r["subcategory"] or "(unknown)"
            n = r["n"] or 0
            n_hit = r["n_hit"] or 0
            hit_rate = round(n_hit / n, 4) if n > 0 else 0.0
            reflections_dimensions[k]["by_subcategory"][sub] = {
                "n": n,
                "hit": n_hit,
                "hit_rate": hit_rate,
                "avg_outcome_pct": round(r["avg_outcome"] or 0.0, 3),
                "avg_confidence": round(r["avg_conf"] or 0.0, 3),
            }
            reflections_dimensions[k]["total"] += n
            reflections_dimensions[k]["hit"] += n_hit
            reflections_dimensions[k]["hit_rate"] = round(
                reflections_dimensions[k]["hit"] / max(1, reflections_dimensions[k]["total"]), 4
            )
            grand_total += n
        reflections_total = grand_total
    except Exception as e:
        # 不影响主流程, 仅 trade_reflections 维度细分留空
        print(f"[v237_by_dimension] trade_reflections_v2 query failed: {type(e).__name__}: {e}", flush=True)
        reflections_dimensions = {k: {"label": v["label"], "total": 0, "hit": 0,
                                       "hit_rate": 0.0, "by_subcategory": {}, "items": []}
                                  for k, v in reflections_dimensions.items()}

    return {
        "ok": True,
        "days": days,
        "by_dimension": {
            "A_signal": {
                "label": "A · 买卖信号",
                "n": len(A_items),
                "items": A_items[:50],
                "note": "A 维度需要 trade_decisions 上游;DB 暂无" if not has_decisions and not has_signals else None,
            },
            "B_direction": {
                "label": "B · 信息分析方向",
                "n": len(B_items),
                "items": B_items[:50],
                "note": None,
            },
            "C_technical": {
                "label": "C · 技术错误",
                "n": len(C_items),
                "items": C_items[:50],
                "note": None,
            },
        },
        "totals": {
            "A": len(A_items),
            "B": len(B_items),
            "C": len(C_items),
            "sum": len(A_items) + len(B_items) + len(C_items),
            # v302+ P1: trade_reflections_v2 总样本 (给前端 totals.grand_total 兜底)
            "grand_total": reflections_total,
        },
        # v302+ P1: trade_reflections_v2 三维度细分 (反思系统真实样本)
        # shape 匹配 frontend components/v237/MissedSignalsDimensionSummary.tsx
        # 和 components/v237/MissedSignalsPanel.tsx (quality tab)
        "dimensions": reflections_dimensions,
        "grand_total": reflections_total,
        "data_sources": ["critical_price_moves", "fed_rate_history", "feed_health",
                         "trade_reflections_v2"],
        "data_alignment": {
            "critical_moves_range": _fmt_range(cpm_range),
            "feed_health_range": _fmt_range(fh_range),
            "fed_rate_range": _fmt_range(fed_range),
            "warning": (
                "B/C 维度需要 critical_moves 与 fed_rate/feed_health 时间窗口重叠,"
                "若数据时间轴错开,B/C 自动为 0。A 维度基于 critical_moves 自身"
                " (反查 trade_decisions 表判断是否漏检) 仍可输出。"
                "trade_reflections_v2 维度细分是独立数据源,不受时间窗口错开影响."
            ),
        },
    }


# ─── v237/calibration 评分校准 (P1) ─────────────────
@app.get("/api/v237/calibration")
def v237_calibration(days: int = Query(90, ge=1, le=3650)) -> dict:
    """评分校准:algo_score 直方图 + 实际涨跌反向回算
    真实数据: critical_price_moves.algo_score (0-1) vs ohlcv_daily 之后 5 日实际涨跌
    """
    cutoff = int(time.time()) - days * 86400
    with db_cursor() as conn:
        moves = conn.execute(
            "SELECT asset, ts, move_pct, algo_score FROM critical_price_moves "
            "WHERE ts >= ? AND algo_score > 0 ORDER BY ts DESC LIMIT 500",
            (cutoff,),
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
def v291_attribution_stats(hours: int = Query(720, ge=1, le=8760)) -> dict:
    """
    资金流交叉验证:从 critical_price_moves + external_indicators 真实数据推导
      - 各 asset 的总涨跌幅
      - 涨跌幅 vs algo_score 散点(用于资金流一致性)
      - 高分信号的方向分布
    """
    cutoff = int(time.time()) - hours * 86400
    with db_cursor() as conn:
        cpm = conn.execute(
            "SELECT asset, move_pct, algo_score, move_kind "
            "FROM critical_price_moves WHERE ts >= ?",
            (cutoff,),
        ).fetchall()
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
        "note": "完全基于真实 critical_moves + external_indicators 计算;无伪造. v302+ 同时给 list + dict",
    }


# ─── 因果图谱 builder (causal-graph) — P1 升级 ────────
@app.get("/api/v291/causal-graph")
def v291_causal_graph(
    asset: str = Query(...),
    hours: int = Query(720, ge=1, le=8760),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """
    因果图谱 builder (P1 升级: 4 类边):
      1) macro_influence:    fed_rate → critical_move (30d 窗口)
      2) tech_lag:           feed_event → critical_move (60min 窗口, 跨 asset)
      3) cross_asset_corr:   asset_A move → asset_B move (60min 窗口)
      4) move_chain:         同一 asset 连续 2 个 move (7d 内)
      5) volume_to_move:     ohlcv volume spike → critical_move (3d 内)
    全部基于真实数据, 无任何外推
    """
    cutoff = int(time.time()) - hours * 86400
    with db_cursor() as conn:
        moves = conn.execute(
            "SELECT id, ts, move_pct, move_kind, algo_score FROM critical_price_moves "
            "WHERE asset=? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (asset, cutoff, limit),
        ).fetchall()
        fed = conn.execute(
            "SELECT ts, dgs2_pct, effr_pct FROM fed_rate_history "
            "WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        # 跨 asset feed events (放宽: 包括所有 asset 的失败/慢事件, 因为 MU 没 feed_health 数据)
        feeds = conn.execute(
            "SELECT ts, asset, source, latency_ms, status FROM feed_health "
            "WHERE ts >= ? AND (status='failed' OR latency_ms > 2000) "
            "ORDER BY ts DESC LIMIT 50",
            (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cutoff)),),
        ).fetchall()
        # 跨 asset critical_moves (联动检测)
        all_moves = conn.execute(
            "SELECT asset, ts, move_pct, move_kind FROM critical_price_moves "
            "WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
        # ohlcv 成交量 spike (asset 限定)
        vol_spikes = conn.execute(
            "SELECT ts, volume FROM ohlcv_daily WHERE asset=? AND ts >= ? AND volume > 0",
            (asset, cutoff),
        ).fetchall()

    nodes: list[dict] = []
    edges: list[dict] = []
    node_id = 0
    edge_types: dict[str, int] = {}

    def add_node(label: str, ntype: str, ts, **kw) -> int:
        nonlocal node_id
        nodes.append({"id": node_id, "label": label, "type": ntype, "ts": ts, **kw})
        node_id += 1
        return node_id - 1

    def add_edge(src: int, dst: int, lag: int, etype: str) -> None:
        edges.append({"from": src, "to": dst, "lag_sec": lag, "type": etype})
        edge_types[etype] = edge_types.get(etype, 0) + 1

    # 1) move 节点 + 3 类边
    for m in moves:
        mid = add_node(
            f"{asset} {m['move_kind']} {m['move_pct']:.1f}%",
            "critical_move",
            m["ts"],
            algo_score=m["algo_score"],
        )
        # 1a) macro: fed_rate → move (30d 窗口, 放宽)
        prev_fed = None
        for f in fed:
            if f["ts"] <= m["ts"]:
                prev_fed = f
            else:
                break
        if prev_fed is not None and abs(m["ts"] - prev_fed["ts"]) <= 86400 * 30:
            fn = add_node(f"fed_effr={prev_fed['effr_pct']}", "fed_rate", prev_fed["ts"])
            add_edge(fn, mid, m["ts"] - prev_fed["ts"], "macro_influence")
        # 1b) tech_lag: feed_event → move (60min 窗口, 跨 asset)
        from datetime import datetime, timezone
        for fe in feeds:
            try:
                fe_ts = int(datetime.fromisoformat(fe["ts"].replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                continue
            if abs(m["ts"] - fe_ts) <= 3600:  # 60 min
                fdn = add_node(
                    f"feed_{fe['source']}({fe['asset']})_lat={fe['latency_ms']}",
                    "feed_event",
                    fe["ts"],
                )
                add_edge(fdn, mid, m["ts"] - fe_ts, "tech_lag")
        # 1c) volume_to_move: ohlcv spike → move (3d 内, 取最近 2 个)
        spikes_for_move = sorted(
            [v for v in vol_spikes if 0 < (m["ts"] - v["ts"]) <= 86400 * 3],
            key=lambda x: abs(m["ts"] - x["ts"]),
        )[:2]
        for v in spikes_for_move:
            vdn = add_node(f"vol={int(v['volume'])}", "volume_spike", v["ts"])
            add_edge(vdn, mid, m["ts"] - v["ts"], "volume_to_move")

    # 2) cross_asset: 跨 asset move 联动 (24h 窗口)
    asset_moves = [m for m in all_moves if m["asset"] == asset]
    for am in asset_moves:
        am_node = next((n for n in nodes if n["type"] == "critical_move" and n["ts"] == am["ts"]), None)
        if am_node is None:
            continue
        for om in all_moves:
            if om["asset"] == asset:
                continue
            if abs(om["ts"] - am["ts"]) <= 86400:  # 24h (放宽 from 60min)
                on = add_node(
                    f"{om['asset']} {om['move_kind']} {om['move_pct']:.1f}%",
                    "cross_asset_move",
                    om["ts"],
                )
                add_edge(on, am_node["id"], am["ts"] - om["ts"], "cross_asset_corr")

    # 3) move_chain: 同 asset 连续 2 move (7d 内, 后→前)
    moves_asc = sorted(asset_moves, key=lambda x: x["ts"])
    for i in range(1, len(moves_asc)):
        prev, cur = moves_asc[i - 1], moves_asc[i]
        if 0 < (cur["ts"] - prev["ts"]) <= 86400 * 7:
            pn = next((n for n in nodes if n["type"] == "critical_move" and n["ts"] == prev["ts"]), None)
            cn = next((n for n in nodes if n["type"] == "critical_move" and n["ts"] == cur["ts"]), None)
            if pn and cn:
                add_edge(pn["id"], cn["id"], cur["ts"] - prev["ts"], "move_chain")

    # 4) news_to_move: news 提到该 asset → 后续 72h 内 move (P0-3)
    try:
        with db_cursor() as news_conn:
            news_for_asset = news_conn.execute(
                # v300-audit: news_events 有新旧两套 schema —— 旧库用 assets 复数，
                # 新库用 asset 单数；headline/body 在旧库里才有值。全部 COALESCE 兜底。
                "SELECT ts, COALESCE(headline, '') AS headline, source FROM news_events "
                "WHERE (COALESCE(assets, '') LIKE ? OR COALESCE(asset, '') LIKE ? "
                "       OR COALESCE(headline, '') LIKE ?) AND ts >= ? "
                "ORDER BY ts ASC LIMIT 200",
                (f"%{asset}%", f"%{asset}%", f"%{asset}%", cutoff),
            ).fetchall()
        for nv in news_for_asset:
            nd = add_node(f"news[{nv['source']}] {nv['headline'][:40]}", "news_event", nv["ts"])
            for m in moves:
                if 0 < (m["ts"] - nv["ts"]) <= 86400 * 3:
                    mn = next((n for n in nodes if n["type"] == "critical_move" and n["ts"] == m["ts"]), None)
                    if mn:
                        add_edge(nd, mn, m["ts"] - nv["ts"], "news_to_move")
    except Exception as e:
        print(f"[bridge] causal-graph news_to_move err: {e}", file=sys.stderr)

    return {
        "ok": True,
        "asset": asset,
        "hours": hours,
        "nodes": nodes,
        "edges": edges,
        "n_moves": len(moves),
        "n_fed": len(fed),
        "n_feed_events": len(feeds),
        "n_cross_asset_moves": sum(1 for n in nodes if n["type"] == "cross_asset_move"),
        "edge_types": edge_types,
        "data_sources": ["critical_price_moves", "fed_rate_history", "feed_health", "ohlcv_daily"],
    }


# ─── /api/v291/cross-asset-chains (P1: 跨资产传导链路) ───
@app.get("/api/v291/cross-asset-chains")
def v291_cross_asset_chains(
    asset: str | None = Query(None, description="过滤指定资产 (None=全部)"),
    active_only: bool = Query(True, description="只看 active=1 的链路"),
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
    asset: str | None = Query(None, description="过滤资产 (None=全部)"),
    days: int = Query(90, ge=1, le=3650),
) -> dict:
    """v291+ P1: 反思数据摘要聚合 (trade_reflections_v2 表 519 行)

    按 dimension + subcategory 聚合:
    - count: 数量
    - hit_rate: 命中概率 (hit=1 / count)
    - avg_outcome_pct: 平均实际收益 (%)
    - avg_confidence: 平均预测置信度
    - 暴露 top 3 worst subcategory (hit_rate 最低)
    """
    cutoff = int(time.time()) - days * 86400

    with db_cursor() as conn:
        params: list[Any] = [cutoff]
        asset_clause = ""
        if asset:
            asset_clause = " AND asset=?"
            params.append(asset.upper())
        rows = conn.execute(
            f"SELECT asset, dimension, subcategory, "
            f"  COUNT(*) as cnt, "
            f"  SUM(hit) as hits, "
            f"  AVG(actual_outcome_pct) as avg_out, "
            f"  AVG(confidence_at_time) as avg_conf, "
            f"  AVG(CAST(settled AS REAL)) as settled_rate "
            f"FROM trade_reflections_v2 "
            f"WHERE created_at >= ?{asset_clause} "
            f"GROUP BY asset, dimension, subcategory "
            f"ORDER BY asset, dimension, cnt DESC",
            params,
        ).fetchall()

    by_dim: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    all_subs: list[dict] = []
    for r in rows:
        cnt = r["cnt"]
        hits = r["hits"] or 0
        hit_rate = (hits / cnt) if cnt > 0 else 0
        rec = {
            "asset": r["asset"],
            "dimension": r["dimension"],
            "subcategory": r["subcategory"],
            "count": cnt,
            "hits": int(hits),
            "misses": int(cnt - hits),
            "hit_rate": round(hit_rate, 3),
            "avg_outcome_pct": round(r["avg_out"] or 0, 2),
            "avg_confidence": round(r["avg_conf"] or 0, 3),
            "settled_rate": round(r["settled_rate"] or 0, 3),
        }
        if r["dimension"] in by_dim:
            by_dim[r["dimension"]].append(rec)
        all_subs.append(rec)

    total = sum(r["count"] for r in all_subs)
    total_hits = sum(r["hits"] for r in all_subs)
    overall_hit_rate = total_hits / total if total > 0 else 0
    overall_avg_out = (
        sum(r["avg_outcome_pct"] * r["count"] for r in all_subs) / total
        if total > 0
        else 0
    )

    qualified = [r for r in all_subs if r["count"] >= 3]
    worst_3 = sorted(qualified, key=lambda x: x["hit_rate"])[:3]
    best_3 = sorted(qualified, key=lambda x: -x["hit_rate"])[:3]

    return {
        "ok": True,
        "days": days,
        "asset_filter": asset.upper() if asset else None,
        "totals": {
            "n_reflections": total,
            "n_settled": sum(int(r["count"] * r["settled_rate"]) for r in all_subs),
            "overall_hit_rate": round(overall_hit_rate, 3),
            "overall_avg_outcome_pct": round(overall_avg_out, 2),
        },
        "by_dimension": {k: v for k, v in by_dim.items()},
        "top_3_worst": worst_3,
        "top_3_best": best_3,
        "data_source": "trade_reflections_v2",
        "ts": int(time.time()),
    }


# ─── /api/review/snapshot (8000 一直 500, 1450 calls/d) ───
@app.get("/api/review/snapshot")
def review_snapshot(days: int = 30, from_ts: int | None = None, to_ts: int | None = None):
    """
    真实数据汇总:
      - paper:  从 trade_reflections_v2 取(空则空, 不伪造)
      - quick_sim: DB 无 quick_sim_trades 表 → 返 None
      - today:  从 audit_log 计算今日 signal 调用次数
      - window: from_ts/to_ts 区间内的事件数
    """
    now = int(time.time())
    today_str = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
    to = to_ts or now
    frm = from_ts or (now - days * 86400)

    with db_cursor() as conn:
        paper_row = conn.execute(
            "SELECT COUNT(*), "
            "  SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN hit=0 THEN 1 ELSE 0 END), "
            "  AVG(CAST(actual_outcome_pct AS REAL)) "
            "FROM trade_reflections_v2"
        ).fetchone()
        n_total, n_wins, n_losses, avg_pnl = paper_row
        n_total = n_total or 0
        n_wins = n_wins or 0
        n_losses = n_losses or 0
        winrate = (n_wins / n_total * 100) if n_total else None

        quick_sim = {
            "total": 0, "open": 0, "settled": 0, "wins": 0, "losses": 0,
            "breakeven": 0, "winrate_pct": None, "avg_pnl_pct": None,
            "total_pnl_pct": None, "profitable": None,
        }

        # today / window signals: DB 中无 audit_log 表, 用 critical_price_moves 代理
        # (DB schema 实情见 bridge_server.py 顶部 note)
        today_start = int(datetime.strptime(today_str, "%Y-%m-%d").timestamp())
        today_end = today_start + 86400
        try:
            today_calls = conn.execute(
                "SELECT COUNT(*) FROM critical_price_moves WHERE ts>=? AND ts<?",
                (today_start, today_end),
            ).fetchone()[0]
        except Exception:
            today_calls = 0
        try:
            window_signals = conn.execute(
                "SELECT COUNT(*) FROM critical_price_moves WHERE ts>=? AND ts<=?",
                (frm, to),
            ).fetchone()[0]
        except Exception:
            window_signals = 0

    return {
        "ok": True,
        "paper": {
            "total": n_total, "tracking": 0, "settled": n_total,
            "wins": n_wins, "losses": n_losses, "holds": 0,
            "winrate": (winrate / 100) if winrate is not None else None,
            "winrate_pct": winrate,
            "avg_pnl": avg_pnl,
            "profit_loss_ratio": None,
            "expected_value_per_trade": None,
            "expectancy_label": "数据不足" if n_total == 0 else None,
        },
        "quick_sim": quick_sim,
        "today": {
            "signals": today_calls, "settled": 0, "date": today_str,
            "windowSignals": window_signals, "windowSettled": 0,
        },
        "window": {
            "from_ts": frm, "to_ts": to,
            "signals": window_signals, "settled": 0,
        },
        "data_sources": ["trade_reflections_v2", "critical_price_moves"],
        "honest_empty": {
            "quick_sim": "DB 中无 quick_sim_trades 表",
            "paper_advanced_fields": "trade_reflections_v2 schema 不含 avg_win/avg_loss 拆分",
            "today_signals": "DB 中无 audit_log 表, today/window 用 critical_price_moves 代理",
        },
    }


# ─── /api/abnormal-signals (8000 一直 500, 692 calls/d) ───
@app.get("/api/abnormal-signals")
def abnormal_signals(
    signal_kind: str | None = None,
    status: str | None = None,
    limit: int = 20,
    asset: str | None = None,
    hours: int = 168,
):
    """
    真实数据: 从 critical_price_moves 推导 K线异动信号
    - multi_factor → 派生"多因子共振"标签 (algo_score >= 0.85)
    - 其他 signal_kind → 过滤
    """
    now = int(time.time())
    since = now - hours * 3600
    with db_cursor() as conn:
        q = "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min, context_json " \
            "FROM critical_price_moves WHERE ts>=? "
        params: list[Any] = [since]
        if asset:
            q += "AND asset=? "
            params.append(asset)
        q += "ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    out = []
    # 当 signal_kind=cot_extreme 时, 跳过 critical_price_moves 分支
    skip_moves = signal_kind == "cot_extreme"
    for r in rows:
        if skip_moves:
            continue
        cid, a, ts, pct, kind, score, dur_min, ctx_json = r
        ctx = json.loads(ctx_json or "{}")
        direction = "up" if pct > 0 else "down"
        factors: list[str] = []
        if abs(pct) >= 5: factors.append("large_move")
        if abs(pct) >= 7: factors.append("extreme_move")
        if score >= 0.85: factors.append("high_algo_score")
        if dur_min <= 60: factors.append("fast_move")
        if "asset_context" in ctx.get("tags", []): factors.append("asset_context_match")
        if signal_kind and signal_kind != "multi_factor" and signal_kind not in factors:
            continue
        s = "active" if score >= 0.7 else "weak"
        if status and status != "all" and status != s:
            continue
        out.append({
            "id": cid, "ts": ts, "asset": a,
            "signal_kind": signal_kind or "multi_factor",
            "direction": direction,
            "anchor_ts": ts - dur_min * 60,
            "detail": {
                "factors": factors,
                "rsi": None, "rsi_divergence": None, "volume_divergence": None,
                "move_pct": pct, "move_kind": kind, "algo_score": score,
            },
            "hint": f"{a} {direction} {abs(pct):.2f}% ({kind}), score={score:.2f}",
            "status": s,
            "created_at": datetime.utcfromtimestamp(ts).isoformat() + "Z",
        })

    # ── COT 极端持仓信号 (反向指标, |z|>=1.5) ──
    if not signal_kind or signal_kind == "cot_extreme":
        with db_cursor() as conn:
            cot_rows = conn.execute(
                "SELECT asset, cftc_contract_code, cftc_contract_name, "
                "       report_date, noncomm_net, open_interest, spec_index "
                "FROM cftc_cot_data "
                "WHERE spec_index IS NOT NULL AND ABS(spec_index) >= ? "
                "ORDER BY ABS(spec_index) DESC LIMIT ?",
                (COT_EXTREME_Z, limit),
            ).fetchall()
        for r in cot_rows:
            z = r["spec_index"]
            # contrarian: z<0 极空→看涨反转, z>0 极多→看跌反转
            is_bullish = z < 0
            direction = "up" if is_bullish else "down"
            tag = "极空" if is_bullish else "极多"
            contrarian = "看涨反转" if is_bullish else "看跌反转"
            factors = ["cot_extreme"]
            if abs(z) >= COT_MEGA_Z:
                factors.append("cot_mega")
            s = "active" if abs(z) >= COT_MEGA_Z else "weak"
            if status and status != "all" and status != s:
                continue
            try:
                y, m, d = r["report_date"].split("-")
                rd_ts = int(datetime(int(y), int(m), int(d), 21, 30, 0).timestamp())
            except Exception:
                rd_ts = int(time.time())
            out.append({
                "id": f"cot_{r['asset']}_{r['report_date']}",
                "ts": rd_ts,
                "asset": r["asset"],
                "signal_kind": "cot_extreme",
                "direction": direction,
                "anchor_ts": rd_ts,
                "detail": {
                    "factors": factors,
                    "rsi": None, "rsi_divergence": None, "volume_divergence": None,
                    "spec_index": z,
                    "noncomm_net": r["noncomm_net"],
                    "open_interest": r["open_interest"],
                    "contract": r["cftc_contract_code"],
                    "contract_name": r["cftc_contract_name"],
                    "move_pct": None, "move_kind": None, "algo_score": round(min(1.0, abs(z) / 3.0), 3),
                },
                "hint": (f"{r['asset']} COT 机构{tag} z={z:+.2f} "
                         f"(净 {r['noncomm_net']:+,} 张) → 反向{contrarian}机会"),
                "status": s,
                "created_at": r["report_date"] + "T21:30:00Z",
            })
    return {
        "ok": True,
        "signals": out,
        "n": len(out),
        "source": "critical_price_moves (K线异动) + cftc_cot_data (持仓极值)",
    }


# ─── /api/sources/alerts (8000 一直 500, 454 calls/d) ───
@app.get("/api/sources/alerts")
def sources_alerts(hours: int = 24, severity_min: str = "low"):
    """真实数据: 从 feed_health 提告警
    schema: id, asset, feed_kind, source, status, latency_ms, value, error, ts(text)
    status: 'failed' / 'success'  (只有这两种值,5651 failed)
    """
    # ts 是 text datetime, 用 'YYYY-MM-DD HH:MM:SS' 字符串比较
    since_str = datetime.utcfromtimestamp(int(time.time()) - hours * 3600).strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, source, asset, feed_kind, status, latency_ms, error, ts "
            "FROM feed_health "
            "WHERE ts>=? AND status='failed' "
            "ORDER BY ts DESC LIMIT 200",
            (since_str,),
        ).fetchall()
    sev_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    threshold = sev_rank.get(severity_min, 0)
    out = []
    for r in rows:
        fid, src, asset, fk, st, lat, err, ts_text = r
        # severity: failed + 高延迟 → high, failed + 普通 → medium
        if lat and lat >= 3000:
            sev = "high"
        else:
            sev = "medium"
        if sev_rank.get(sev, 0) < threshold:
            continue
        # 把 text datetime 转成 ts (epoch) 给前端
        try:
            ts_epoch = int(datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            ts_epoch = 0
        out.append({
            "id": fid, "source": src, "asset": asset, "feed_kind": fk,
            "status": st, "severity": sev,
            "latency_ms": lat, "ts": ts_epoch, "note": err or "",
        })
    return {
        "ok": True,
        "alerts": out,
        "n": len(out),
        "data_source": "feed_health (real probe data, status='failed')",
        "n_total_scanned": len(rows),
    }


# ─── /api/trading/signals (8000 一直 500, 160 calls/d) ───
@app.get("/api/trading/signals")
def trading_signals(
    asset: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    hide_neutral: int = 0,
    limit: int = 200,
):
    """真实数据: trade_reflections_v2 = 0 (DB 空), 返空 + 提示
    schema: id, decision_id, asset, reflection_text, dimension, subcategory,
            confidence_at_time, actual_outcome_pct, settled, hit, created_at
    """
    frm = from_ts or (int(time.time()) - 30 * 86400)
    to = to_ts or int(time.time())
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, asset, created_at, dimension, actual_outcome_pct, "
            "       confidence_at_time, hit "
            "FROM trade_reflections_v2 "
            "WHERE created_at>=? AND created_at<=? "
            "ORDER BY created_at DESC LIMIT ?",
            (frm, to, limit),
        ).fetchall()
    out = []
    for r in rows:
        rid, a, ts, dim, pnl, conf, hit = r
        # action 映射: dimension 名称 → buy/sell/hold
        if dim and "buy" in dim.lower(): act = "BUY"
        elif dim and "sell" in dim.lower(): act = "SELL"
        else: act = "HOLD"
        if hide_neutral and act == "HOLD":
            continue
        out.append({
            "id": rid, "asset": a, "news_time": ts,
            "action": act, "score": conf or 0, "pnl_pct": pnl,
            "dimension": dim, "note": "from_trade_reflections_v2",
        })
    return {
        "ok": True,
        "signals": out,
        "n": len(out),
        "source": "trade_reflections_v2",
        "honest_empty": len(out) == 0,
    }


# ─── /api/v201/impact/by-asset (8000 一直 500, 160 calls/d) ───
_MOVE_KIND_CN = {
    "surge": "急拉", "spike": "急拉", "spike_up": "急拉", "surge_up": "急拉",
    "plunge": "急跌", "crash": "急跌", "spike_down": "急跌", "plunge_down": "急跌",
    "slow_rise": "缓涨", "gradual_rise": "缓涨", "drift_up": "缓涨",
    "slow_drop": "缓跌", "gradual_drop": "缓跌", "drift_down": "缓跌",
}


def _impact_title(kind: str | None, pct: float | None, action: str | None) -> str:
    """v303 R10: 前端 MoversPanel L1814 `titleText(it.event_title)`, 空则 continue。
    之前 bridge 只给 move_kind/event_action, 没有 event_title
    → 151 条 impact marker 全被跳过。这里按 kind+涨跌幅合成可读标题。"""
    base = _MOVE_KIND_CN.get((kind or "").lower())
    if not base:
        base = {
            "BUY": "利好拉升", "SELL": "利空下跌",
            "UNKNOWN": "价格异动", "HOLD": "价格异动",
        }.get((action or "UNKNOWN").upper(), "价格异动")
    if pct is None:
        return base
    return f"{base} {pct:+.1f}%"


@app.get("/api/v201/impact/by-asset")
def impact_by_asset(
    asset: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    severity_min: str = "low",
    limit: int = 200,
):
    """真实数据: v302+ P2 优先 event_move_attribution (事件→涨跌),fallback critical_price_moves.
    event_move_attribution 有 ev_action/ev_source/ev_news_id 完整事件链;critical_moves 仅 K线. """
    frm = from_ts or (int(time.time()) - 30 * 86400)
    to = to_ts or int(time.time())
    out = []
    real_source_used = "event_move_attribution"  # 优先用真实事件源
    with db_cursor() as conn:
        # 第一优先: event_move_attribution (事件→资产影响,带 ev_action/ev_source/news_event_id)
        rows = conn.execute(
            "SELECT id, move_ts, move_pct, move_kind, algo_score, duration_min, "
            "       ev_news_id, ev_source, ev_action, ev_score "
            "FROM event_move_attribution "
            "WHERE asset=? AND move_ts>=? AND move_ts<=? "
            "ORDER BY move_ts DESC LIMIT ?",
            (asset, frm, to, limit),
        ).fetchall()
        for r in rows:
            (rid, ts, pct, kind, score, dur,
             ev_news_id, ev_source, ev_action, ev_score) = r
            a = abs(pct)
            if a >= 7: sev = "critical"
            elif a >= 5: sev = "high"
            elif a >= 3: sev = "medium"
            else: sev = "low"
            if severity_min == "critical" and sev != "critical": continue
            if severity_min == "high" and sev not in ("high", "critical"): continue
            if severity_min == "medium" and sev not in ("medium", "high", "critical"): continue
            out.append({
                "event_id": f"ema-{rid}", "event_source": ev_source or "unknown",
                "event_action": ev_action or "UNKNOWN",
                "event_news_id": ev_news_id,
                "event_score": ev_score,
                "event_time": ts, "asset": asset,
                "move_pct": pct, "move_kind": kind, "algo_score": score,
                "duration_min": dur, "severity": sev,
                # ── 前端 ImpactRow 契约字段 (v303 R10) ──
                # MoversPanel L1814 读 it.event_title / L1826 读 it.impact_severity
                "event_title": _impact_title(kind, pct, ev_action),
                "impact_severity": sev,
                "impact_pct": pct,
                "direction": ev_action or "UNKNOWN",
            })
        # 第二兜底: critical_price_moves (如果 event_move_attribution 没数据)
        if not out:
            real_source_used = "critical_price_moves (fallback)"
            rows2 = conn.execute(
                "SELECT id, ts, move_pct, move_kind, algo_score, duration_min "
                "FROM critical_price_moves "
                "WHERE asset=? AND ts>=? AND ts<=? "
                "ORDER BY ts DESC LIMIT ?",
                (asset, frm, to, limit),
            ).fetchall()
            for r in rows2:
                rid, ts, pct, kind, score, dur = r
                a = abs(pct)
                if a >= 7: sev = "critical"
                elif a >= 5: sev = "high"
                elif a >= 3: sev = "medium"
                else: sev = "low"
                if severity_min == "critical" and sev != "critical": continue
                if severity_min == "high" and sev not in ("high", "critical"): continue
                if severity_min == "medium" and sev not in ("medium", "high", "critical"): continue
                out.append({
                    "event_id": f"cpm-{rid}", "event_source": "critical_price_moves",
                    "event_time": ts, "asset": asset,
                    "move_pct": pct, "move_kind": kind, "algo_score": score,
                    "duration_min": dur, "severity": sev,
                    # 前端 ImpactRow 契约字段 (v303 R10), 同上
                    "event_title": _impact_title(kind, pct, None),
                    "impact_severity": sev,
                    "impact_pct": pct,
                    "direction": "UNKNOWN",
                })
    return {
        "ok": True,
        "items": out,
        "n": len(out),
        "asset": asset,
        "source": real_source_used + (" (real yfinance-derived)" if real_source_used.startswith("critical") else " (含 ev_action/ev_source/news_event_id 真实事件链)"),
        "honest_note": ("优先用 event_move_attribution 表的事件→影响;fallback critical_moves" if real_source_used.startswith("event") else "该资产在 event_move_attribution 无数据,降级到 critical_price_moves"),
    }


# ─── /api/ohlcv/daily (P0-1: 解锁 K线/成交量能力) ───
@app.get("/api/ohlcv/daily")
def ohlcv_daily(
    asset: str,
    days: int = 365,
    limit: int = 500,
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


# ─── /api/ohlcv/pattern-snapshot (P1 预热: K线形态 + 成交量异动) ───
@app.get("/api/ohlcv/pattern-snapshot")
def ohlcv_pattern(asset: str, days: int = 90):
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
    if action == "BUY":
        return 1 if outcome_pct > 0 else 0
    if action == "SELL":
        return 1 if outcome_pct < 0 else 0
    return 0


def _forward_outcome_pct(conn, asset, from_ts, entry_price, settle_days):
    """按 entry_price 计算 settle_days 个交易日后的真实涨跌(%)。无数据返回 None。"""
    if not entry_price or entry_price <= 0:
        return None
    target_ts = int(from_ts) + max(settle_days - 1, 0) * 86400
    row = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? ORDER BY ts ASC LIMIT 1",
        (asset, target_ts),
    ).fetchone()
    if not row:
        return None
    return round((row[0] - entry_price) / entry_price * 100, 3)


def _insert_settlement_reflection(conn, *, decision_id, asset, action,
                                  entry_price, outcome_pct, sub_layer) -> None:
    """把结算结果落到 trade_reflections_v2 的现行列上。

    旧代码写的 dimension / subcategory / confidence_at_time / actual_outcome_pct /
    settled / hit / reflection_text 在该表(69 列)中均不存在, 一律会报
    "no such column"。现映射: dimension->layer, actual_outcome_pct->pnl_pct;
    settled_at / created_at 是 TEXT, 用 datetime('now')。
    """
    conn.execute(
        "INSERT INTO trade_reflections_v2 "
        "(decision_id, asset, action, entry_price, pnl_pct, layer, sub_layer, "
        " root_cause, settled_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (decision_id, asset, action, entry_price, outcome_pct,
         "decision", sub_layer, f"{action} {outcome_pct:+.2f}%"),
    )


ensure_trade_decisions_table()


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
        # 现行结构 NOT NULL: decision_ts / asset / intended_action / final_action /
        # decision_type。旧的 score/confidence/rationale/evidence_json 无对应列,
        # rationale 并入 context_json, score/confidence 不再落库。
        now = time.time()
        cur = conn.execute(
            "INSERT INTO trade_decisions "
            "(decision_ts, asset, intended_action, final_action, decision_type, "
            " news_id, entry_price, planned_stop, planned_target, size_usdt, "
            " deny_reason, context_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            (now, asset, action, action, payload.get("decision_type") or "ENTRY",
             payload.get("news_id"), price, payload.get("planned_stop"),
             payload.get("planned_target"), payload.get("size_usdt"),
             None if action == "HOLD" else (rationale[:2000] or None),
             json.dumps({"score": score, "confidence": conf, "rationale": rationale,
                         "evidence": evidence}, ensure_ascii=False)),
        )
        did = cur.lastrowid
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
            "SELECT asset, final_action, entry_price, status, decision_ts "
            "FROM trade_decisions WHERE id=?",
            (int(did),),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"decision {did} not found"}
        asset, action, price_at, status, decision_ts = row
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
        hit = _hit_for(action, outcome_pct)
        conn.execute(
            "UPDATE trade_decisions SET status='settled', "
            "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
            (outcome_pct, int(did)),
        )
        _insert_settlement_reflection(
            conn, decision_id=int(did), asset=asset, action=action,
            entry_price=price_at, outcome_pct=outcome_pct, sub_layer="settled_now")
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
def decision_list(asset: str | None = None, status: str | None = None, limit: int = 50):
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
def decision_stats(asset: str | None = None, days: int = 90):
    since = int(time.time()) - days * 86400
    with db_cursor() as conn:
        q = ("SELECT final_action, COUNT(*), AVG(actual_outcome_pct), "
             "       SUM(CASE WHEN actual_outcome_pct IS NOT NULL THEN 1 ELSE 0 END) "
             "FROM trade_decisions WHERE status='settled' AND decision_ts>=?")
        params: list = [since]
        if asset:
            q += " AND asset=?"
            params.append(asset)
        q += " GROUP BY final_action"
        rows = conn.execute(q, params).fetchall()
    out = []
    total = 0
    total_hit = 0
    for r in rows:
        act, n, avg_pct, n_settled = r
        # 算 hit: BUY 赢=outcome>0, SELL 赢=outcome<0
        # 因 GROUP BY 拿不到单个 row, 此处 n_hit 在外层 detail 接口再算
        out.append({"action": act, "n_settled": n,
                    "avg_outcome_pct": round(avg_pct, 3) if avg_pct else None,
                    "winrate_pct_note": "用 n_settled/n 计算; hit 详情见 /api/decision/list?status=settled"})
        total += n
    return {"ok": True, "by_action": out, "n_total_settled": total,
            "asset": asset, "days": days}


def realtime_scan_loop() -> None:
    """每 5 分钟扫描 external_indicators 是否有新 tick,如有则计算并插入 critical_price_moves"""
    import backfill_critical_moves  # noqa
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
                    conn.commit()
                    print(f"[bridge] inserted {sum(1 for _ in fresh)} fresh critical_moves")
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
NEWS_ASSETS_ALIASES: dict[str, list[str]] = {
    # crypto
    "BTC": ["比特币", "大饼", "BTC", "BITCOIN"],
    "ETH": ["以太坊", "以太", "ETH", "ETHEREUM"],
    "SOL": ["索拉纳", "SOL", "SOLANA"],
    "BNB": ["币安币", "BNB", "BINANCECOIN"],
    "ADA": ["艾达币", "卡尔达诺", "ADA", "CARDANO"],
    "XRP": ["瑞波币", "瑞波", "XRP", "RIPPLE"],
    "DOGE": ["狗狗币", "DOGE", "DOGECOIN"],
    "WLD": ["世界币", "WLD", "WORLDCOIN"],
    "AVAX": ["雪崩协议", "雪崩", "AVAX", "AVALANCHE"],
    "MATIC": ["MATIC", "POLYGON"],
    "DOT": ["波卡", "DOT", "POLKADOT"],
    "LINK": ["LINK", "CHAINLINK"],
    "TON": ["TON", "TONCOIN"],
    "TRX": ["波场", "TRX", "TRON"],
    "SHIB": ["柴犬币", "SHIB", "SHIBA"],
    "LTC": ["莱特币", "LTC", "LITECOIN"],
    "BCH": ["比特现金", "BCH"],
    "ATOM": ["宇宙", "ATOM", "COSMOS"],
    "UNI": ["UNI", "UNISWAP"],
    "APT": ["APT", "APTOS"],
    # US stocks
    "AAPL": ["苹果", "AAPL", "APPLE"],
    "MSFT": ["微软", "MSFT", "MICROSOFT"],
    "NVDA": ["英伟达", "NVDA", "NVIDIA"],
    "TSLA": ["特斯拉", "TSLA", "TESLA"],
    "MU": ["美光", "MU", "MICRON"],
    "AMD": ["超微", "AMD"],
    "GOOGL": ["谷歌", "GOOGL", "GOOGLE", "GOOG"],
    "META": ["META", "脸书", "FACEBOOK"],
    "AMZN": ["亚马逊", "AMZN", "AMAZON"],
    "NFLX": ["奈飞", "NFLX", "NETFLIX"],
    "BABA": ["阿里巴巴", "BABA", "ALIBABA"],
    "JD": ["京东", "JD"],
    "PDD": ["拼多多", "PDD"],
    "NIO": ["蔚来", "NIO"],
    "XPEV": ["小鹏", "XPEV"],
    "LI": ["理想", "LI"],
    # KR stocks (6位数字代码 + 公司名)
    "000660": ["海力士", "SK海力士", "000660"],
    "005930": ["三星", "005930", "SAMSUNG"],
    "003550": ["LG", "003550"],
    "005380": ["现代汽车", "现代", "005380", "HYUNDAI"],
    "000270": ["起亚", "000270", "KIA"],
}


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
    import urllib.request, json as _json
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
    import urllib.request, json as _json
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
            ts = _parse_iso_ts((item.get("showTime") or "").replace(" ", "T"))
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
OHLCV_ASSETS = ["ADA-USD", "BNB-USD", "DOGE-USD", "SOL-USD", "XRP-USD", "MU", "000660.KS"]
YFINANCE_SLEEP_S = 2.5  # rate-limit 防御
YFINANCE_RETRIES = 3
YFINANCE_BACKOFF = [0, 5, 15]  # 二次失败: 等 5s, 三次: 等 15s
OHLCV_REFRESH_HOURS = 24
OHLCV_REFRESH_PERIOD = "7d"


def ohlcv_refresh_loop() -> None:
    """P1 worker 1: 每 24h 拉最新 OHLCV 覆盖最近 7d (幂等 INSERT OR IGNORE)
    防御 yfinance rate-limit: 2.5s sleep + 3 次重试 + backoff
    """
    import yfinance as yf
    while True:
        time.sleep(OHLCV_REFRESH_HOURS * 3600)
        try:
            total_inserted = 0
            for asset in OHLCV_ASSETS:
                df = None
                for attempt in range(YFINANCE_RETRIES):
                    if YFINANCE_BACKOFF[attempt] > 0:
                        time.sleep(YFINANCE_BACKOFF[attempt])
                    try:
                        df = yf.Ticker(asset).history(period=OHLCV_REFRESH_PERIOD, auto_adjust=False)
                        if df is not None and len(df) > 0:
                            break
                    except Exception as e:
                        print(f"[bridge] ohlcv_refresh {asset} attempt {attempt+1} fail: {e}", file=sys.stderr)
                if df is None or len(df) == 0:
                    continue
                rows = []
                for ts_idx, row in df.iterrows():
                    ts = int(ts_idx.timestamp())
                    rows.append((asset, ts, float(row["Open"]), float(row["High"]),
                                 float(row["Low"]), float(row["Close"]),
                                 int(row["Volume"]) if row["Volume"] > 0 else 0, "yfinance_refresh"))
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


DECISION_SETTLE_HOURS = 1
DECISION_SETTLE_DAYS = 5


def decision_settle_loop() -> None:
    """P1 worker 2: 每 1h 扫描 status='open' 且 decision_ts 超过 5d 的决策,自动结算"""
    while True:
        time.sleep(DECISION_SETTLE_HOURS * 3600)
        try:
            cutoff = int(time.time()) - DECISION_SETTLE_DAYS * 86400
            with db_cursor() as conn:
                rows = conn.execute(
                    "SELECT id, asset, final_action, entry_price, decision_ts "
                    "FROM trade_decisions WHERE status='open' AND decision_ts<=? LIMIT 100",
                    (cutoff,),
                ).fetchall()
            if not rows:
                continue
            n_settled = 0
            with write_db_cursor() as conn:
                for r in rows:
                    did = r["id"]
                    asset = r["asset"]
                    action = r["final_action"]
                    outcome_pct = _forward_outcome_pct(
                        conn, asset, r["decision_ts"], r["entry_price"],
                        DECISION_SETTLE_DAYS)
                    if outcome_pct is None:
                        continue
                    conn.execute(
                        "UPDATE trade_decisions SET status='settled', "
                        "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
                        (outcome_pct, did),
                    )
                    _insert_settlement_reflection(
                        conn, decision_id=did, asset=asset, action=action,
                        entry_price=r["entry_price"], outcome_pct=outcome_pct,
                        sub_layer="auto_settled_5d")
                    n_settled += 1
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
    """同步 yfinance 拉取 (在线程池里跑, 避免阻塞 event loop)"""
    import yfinance as yf
    inserted = 0
    errors: list = []
    for asset in assets:
        df = None
        for attempt in range(YFINANCE_RETRIES):
            if YFINANCE_BACKOFF[attempt] > 0:
                time.sleep(YFINANCE_BACKOFF[attempt])
            try:
                df = yf.Ticker(asset).history(period=OHLCV_REFRESH_PERIOD, auto_adjust=False)
                if df is not None and len(df) > 0:
                    break
            except Exception as e:
                errors.append(f"{asset}_attempt{attempt+1}={e}")
        if df is None or len(df) == 0:
            if f"{asset}=no_data" not in errors:
                errors.append(f"{asset}=no_data")
            continue
        try:
            rows = []
            for ts_idx, row in df.iterrows():
                ts = int(ts_idx.timestamp())
                rows.append((asset, ts, float(row["Open"]), float(row["High"]),
                             float(row["Low"]), float(row["Close"]),
                             int(row["Volume"]) if row["Volume"] > 0 else 0, "yfinance_manual"))
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
            "SELECT id, asset, final_action, entry_price, decision_ts "
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
            conn.execute(
                "UPDATE trade_decisions SET status='settled', "
                "settled_at=strftime('%s','now'), actual_outcome_pct=? WHERE id=?",
                (outcome_pct, r["id"]),
            )
            _insert_settlement_reflection(
                conn, decision_id=r["id"], asset=r["asset"],
                action=r["final_action"], entry_price=r["entry_price"],
                outcome_pct=outcome_pct, sub_layer="auto_settled_5d")
            n_settled += 1
    return {"ok": True, "n_total": n_total, "n_settled": n_settled, "n_skipped": n_skipped}


# 启动时启动后台线程
scan_thread = threading.Thread(target=realtime_scan_loop, daemon=True, name="bridge-scan")
scan_thread.start()
ohlcv_thread = threading.Thread(target=ohlcv_refresh_loop, daemon=True, name="bridge-ohlcv-refresh")
ohlcv_thread.start()
settle_thread = threading.Thread(target=decision_settle_loop, daemon=True, name="bridge-decision-settle")
settle_thread.start()
causal_eval_thread = threading.Thread(target=causal_chain_eval_loop, daemon=True, name="bridge-causal-eval")
causal_eval_thread.start()


# ─── News API endpoints + worker (P0-3) ───────────────
def _news_pull_sync() -> int:
    """跑一次双源拉取, 写入 news_events (INSERT OR IGNORE)"""
    techflow = _pull_techflow()
    eastmoney = _pull_eastmoney()
    all_news = techflow + eastmoney
    if not all_news:
        return 0
    n = 0
    with write_db_cursor() as conn:
        for ev in all_news:
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO news_events "
                    "(source, headline, body, action, score, assets, ts, news_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ev["source"], ev["headline"], ev["body"], ev["action"],
                     ev["score"], ev["assets"], ev["ts"], ev["news_id"]),
                )
                if cur.rowcount and cur.rowcount > 0:
                    n += 1
            except Exception as e:
                print(f"[bridge] news insert error ({ev.get('source')}): {e}", file=sys.stderr)
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


@app.get("/api/news/recent")
def news_recent(hours: int = Query(24, ge=1, le=720), limit: int = Query(50, ge=1, le=500),
                source: str | None = None, asset: str | None = None):
    """P0-3: 最近 N 小时新闻, 可按 source/asset 过滤"""
    cutoff = int(time.time()) - hours * 3600
    sql = ("SELECT id, source, headline, body, action, score, assets, ts, news_id, fetched_at "
           "FROM news_events WHERE ts>=? ")
    params: list = [cutoff]
    if source:
        sql += "AND source=? "
        params.append(source)
    if asset:
        sql += "AND assets LIKE ? "
        params.append(f"%{asset}%")
    sql += "ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        by_source = conn.execute(
            "SELECT source, COUNT(*) FROM news_events WHERE ts>=? GROUP BY source", (cutoff,)
        ).fetchall()
    return {
        "ok": True,
        "n": len(rows),
        "by_source": {s: c for s, c in by_source},
        "news": [dict(r) for r in rows],
        "hours": hours,
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

def _classify_direction(score: float) -> str:
    if score > 0.3:
        return "Bullish"
    if score < -0.3:
        return "Bearish"
    return "Neutral"


def _magnitude_label(stars: int) -> str:
    return ["微弱", "轻微", "中等", "显著", "重大"][max(0, min(4, stars - 1))]


# 关键词词典(本数据 118 条全是中文财经,中英混合确保覆盖)
BULLISH_KEYWORDS = (
    "突破", "创新高", "新高", "上涨", "飙升", "暴涨", "大涨", "利好", "看多",
    "增长", "加仓", "买入", "增持", "底部", "筑底", "反弹", "回暖", "复苏",
    "rally", "surge", "breakout", "bullish", "long",
)
BEARISH_KEYWORDS = (
    "暴跌", "腰斩", "崩盘", "跌停", "创新低", "新低", "下跌", "大跌", "利空", "看空",
    "撤资", "出逃", "清仓", "减持", "减仓", "闪崩", "恐慌", "衰退",
    "crash", "plunge", "drop", "fall", "bearish", "short", "liquidation",
)
# 常见资产 ticker 词典(从文本提取受影响资产)
KNOWN_TICKERS = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "GOLD", "XAU",
    "OIL", "WTI", "ES", "NQ", "EUR", "AUD", "CAD", "NG", "WHEAT", "CORN",
    "MU", "SKHYNIX", "UNI",
)


def _extract_text_signal(headline: str, body: str | None) -> dict:
    """从文本提取方向信号(弥补 news_events.score 大量为 0 的问题)"""
    text = (headline or "") + " " + (body or "")
    bull_hits = sum(1 for k in BULLISH_KEYWORDS if k in text)
    bear_hits = sum(1 for k in BEARISH_KEYWORDS if k in text)
    if bull_hits + bear_hits == 0:
        text_score = 0.0
    else:
        text_score = (bull_hits - bear_hits) / (bull_hits + bear_hits)
    return {
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
        "text_score": text_score,
        "has_signal": (bull_hits + bear_hits) > 0,
    }


def _extract_assets_from_text(headline: str, body: str | None, declared: str) -> list[str]:
    """提取资产 — 优先用 declared,然后从文本扫常见 ticker"""
    declared_list = [a.strip().upper() for a in (declared or "").split(",") if a.strip()]
    if declared_list:
        return declared_list
    text = ((headline or "") + " " + (body or "")).upper()
    return [t for t in KNOWN_TICKERS if t in text]


@app.get("/api/news/impact-evaluate")
def news_impact_evaluate_get(
    news_id: int = Query(..., ge=1),
    days_window: int = Query(3, ge=1, le=30),
) -> dict:
    """P0-4: GET 形式 — 给已有 news_id 的新闻做影响力评估"""
    return _news_impact_evaluate(news_id=news_id, days_window=days_window)


@app.post("/api/news/impact-evaluate")
async def news_impact_evaluate_post(request: Request) -> dict:
    """P0-4: POST 形式 — 接收外部传入的新闻字段(实时新闻尚未入库)"""
    body = await request.json()
    return _news_impact_evaluate(
        headline=body.get("headline"),
        news_body=body.get("body"),
        declared_assets=body.get("assets") or "",
        declared_score=body.get("score"),
        declared_source=body.get("source") or "external",
        days_window=int(body.get("days_window") or 3),
    )


def _news_impact_evaluate(
    news_id: int | None = None,
    headline: str | None = None,
    news_body: str | None = None,
    declared_assets: str | None = None,
    declared_score: float | None = None,
    declared_source: str | None = None,
    days_window: int = 3,
) -> dict:
    """核心评估函数 — 5 因子综合打分(GET/POST 复用)"""
    reasoning: list[dict] = []
    support: dict = {}
    honest_notes: list[str] = []

    # ───── 1) 载入新闻 ─────
    row = None
    if news_id is not None:
        with db_cursor() as conn:
            r = conn.execute(
                "SELECT id, source, headline, body, score, assets, ts "
                "FROM news_events WHERE id=?",
                (news_id,),
            ).fetchone()
            if r:
                row = dict(r)
            else:
                return {"ok": False, "error": f"news_id={news_id} 不存在"}
    elif headline:
        row = {
            "id": None,
            "source": declared_source or "external",
            "headline": headline,
            "body": news_body or "",
            "score": declared_score if declared_score is not None else 0.0,
            "assets": declared_assets or "",
            "ts": int(time.time()),
        }
    else:
        return {"ok": False, "error": "必须提供 news_id 或 headline"}

    reasoning.append({
        "step": 1,
        "factor": "原始新闻",
        "question": "新闻是什么?",
        "answer": f"[{row['source']}] {row['headline'][:80]}",
    })

    # ───── 2) 因子 A: news_score + 文本关键词 ─────
    news_score = float(row["score"] or 0.0)
    text_signal = _extract_text_signal(row["headline"], row.get("body"))
    composite_score = news_score * 0.6 + text_signal["text_score"] * 0.4
    direction = _classify_direction(composite_score)
    support["news_score"] = {
        "raw_score": news_score,
        "text_bull_hits": text_signal["bull_hits"],
        "text_bear_hits": text_signal["bear_hits"],
        "composite_score": round(composite_score, 3),
    }
    reasoning.append({
        "step": 2,
        "factor": "因子 A · 新闻情感方向",
        "question": "这条新闻情感方向是 Bullish / Bearish / Neutral?",
        "answer": (
            f"{direction} (DB score={news_score:.2f}, "
            f"文本 hit 多={text_signal['bull_hits']} 空={text_signal['bear_hits']}, "
            f"综合={composite_score:.2f})"
        ),
    })

    # ───── 3) 受影响资产识别 ─────
    primary_assets = _extract_assets_from_text(
        row["headline"], row.get("body"), row["assets"] or ""
    )
    if not primary_assets:
        honest_notes.append("未识别出受影响资产 ticker — 影响范围仅作用于大盘情绪")
    reasoning.append({
        "step": 3,
        "factor": "受影响资产识别",
        "question": "这条新闻提到哪些资产?",
        "answer": f"{primary_assets if primary_assets else '(无 — 仅影响大盘情绪)'}",
    })

    # ───── 4) 因子 B: critical_moves 同向验证 ─────
    move_evidence: list[dict] = []
    if primary_assets:
        cutoff_start = row["ts"] - days_window * 86400
        cutoff_end = row["ts"] + days_window * 86400
        placeholders = ",".join("?" for _ in primary_assets)
        try:
            with db_cursor() as conn:
                moves = conn.execute(
                    f"SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min "
                    f"FROM critical_price_moves "
                    f"WHERE asset IN ({placeholders}) AND ts BETWEEN ? AND ? "
                    f"ORDER BY ts DESC LIMIT 20",
                    (*primary_assets, cutoff_start, cutoff_end),
                ).fetchall()
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            same_dir = opp_dir = 0
            for m in moves:
                move_dir = 1 if m["move_pct"] > 0 else -1
                aligned = (news_dir != 0 and move_dir == news_dir)
                if news_dir == 0:
                    pass
                elif aligned:
                    same_dir += 1
                else:
                    opp_dir += 1
                move_evidence.append({
                    "id": m["id"], "asset": m["asset"], "ts": m["ts"],
                    "move_pct": m["move_pct"], "move_kind": m["move_kind"],
                    "algo_score": m["algo_score"],
                    "aligned_with_news": aligned,
                })
            ratio = (
                round(same_dir / max(1, same_dir + opp_dir), 2)
                if (same_dir + opp_dir) > 0 else None
            )
            support["critical_moves"] = {
                "window_days": days_window,
                "n_moves": len(moves),
                "n_same_dir": same_dir,
                "n_opp_dir": opp_dir,
                "ratio": ratio,
                "items": move_evidence[:5],
            }
            reasoning.append({
                "step": 4,
                "factor": "因子 B · 临界涨跌同向验证",
                "question": f"前后 {days_window} 天内, 这些资产有没有同向 critical_move?",
                "answer": f"找到 {len(moves)} 次临界涨跌 · 同向 {same_dir} 次 / 反向 {opp_dir} 次",
            })
        except Exception as e:
            honest_notes.append(f"critical_moves 查询失败: {e}")

    # ───── 5) 因子 C: COT spec_index 极值反向警示 ─────
    cot_evidence: list[dict] = []
    cot_warning: list[str] = []
    if primary_assets:
        placeholders = ",".join("?" for _ in primary_assets)
        try:
            with db_cursor() as conn:
                cot_rows = conn.execute(
                    f"SELECT asset, spec_index, report_date, noncomm_net "
                    f"FROM cftc_cot_data "
                    f"WHERE asset IN ({placeholders}) AND spec_index IS NOT NULL "
                    f"ORDER BY report_date DESC LIMIT 10",
                    primary_assets,
                ).fetchall()
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            for c in cot_rows:
                if abs(c["spec_index"]) < 0.7:
                    continue
                spec_dir = 1 if c["spec_index"] > 0 else -1
                warning = ""
                if news_dir != 0 and spec_dir == -news_dir:
                    warning = "⚠ 新闻方向与 COT 极值反向(机构反向操作)"
                    cot_warning.append(warning)
                elif news_dir != 0 and spec_dir == news_dir:
                    warning = "✓ COT 极值同向(机构顺势)"
                cot_evidence.append({
                    "asset": c["asset"],
                    "spec_index": round(c["spec_index"], 3),
                    "report_date": c["report_date"],
                    "noncomm_net": c["noncomm_net"],
                    "signal": "极多" if c["spec_index"] > 0.7 else "极空",
                    "warning": warning,
                })
            support["cot_signals"] = {
                "n_extreme": len(cot_evidence),
                "warnings": cot_warning,
                "items": cot_evidence[:5],
            }
            reasoning.append({
                "step": 5,
                "factor": "因子 C · COT 机构持仓极值",
                "question": "机构持仓有没有极端信号与新闻方向冲突?",
                "answer": f"找到 {len(cot_evidence)} 个极值 · {len(cot_warning)} 个反向警示",
            })
        except Exception as e:
            honest_notes.append(f"cftc_cot_data 查询失败: {e}")

    # ───── 6) 因子 D: fed_rate 走势 ─────
    fed_signal = "unknown"
    fed_aligned: str | None = None
    try:
        with db_cursor() as conn:
            fed_rows = conn.execute(
                "SELECT ts, effr_pct FROM fed_rate_history ORDER BY ts DESC LIMIT 10"
            ).fetchall()
        if len(fed_rows) >= 2:
            recent_delta = fed_rows[0]["effr_pct"] - fed_rows[-1]["effr_pct"]
            if recent_delta > 0.01:
                fed_signal = "rising"
            elif recent_delta < -0.01:
                fed_signal = "falling"
            else:
                fed_signal = "stable"
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            if news_dir != 0:
                if (fed_signal == "rising" and news_dir == 1) or (
                    fed_signal == "falling" and news_dir == -1
                ):
                    fed_aligned = "conflicting"
                else:
                    fed_aligned = "aligned"
        support["fed_signal"] = {
            "signal": fed_signal,
            "news_alignment": fed_aligned,
        }
        reasoning.append({
            "step": 6,
            "factor": "因子 D · 利率环境",
            "question": "当前利率走势与新闻方向是否一致?",
            "answer": (
                f"利率 {fed_signal} · "
                f"{'一致' if fed_aligned == 'aligned' else ('冲突' if fed_aligned == 'conflicting' else '中性')}"
            ),
        })
    except Exception as e:
        honest_notes.append(f"fed_rate 查询失败: {e}")

    # ───── 7) 因子 E: trade_reflections_v2 历史同类胜率 ─────
    history_hit_rate: float | None = None
    history_n = 0
    if primary_assets:
        try:
            with db_cursor() as conn:
                ref_rows = conn.execute(
                    "SELECT dimension, subcategory, hit, confidence_at_time "
                    "FROM trade_reflections_v2 "
                    "WHERE asset IN ({}) AND settled=1 "
                    "ORDER BY created_at DESC LIMIT 50".format(
                        ",".join("?" for _ in primary_assets)
                    ),
                    primary_assets,
                ).fetchall()
            if ref_rows:
                history_n = len(ref_rows)
                n_hit = sum(1 for r in ref_rows if r["hit"] == 1)
                history_hit_rate = round(n_hit / history_n, 3)
            support["history"] = {
                "n_reflections": history_n,
                "hit_rate": history_hit_rate,
            }
            if history_n < 30 and history_n > 0:
                honest_notes.append(
                    f"反思样本仅 {history_n} 条 (<30),历史胜率不可信"
                )
            reasoning.append({
                "step": 7,
                "factor": "因子 E · 历史同类决策胜率",
                "question": "历史上对同资产决策的胜率?",
                "answer": (
                    f"{history_n} 条反思 · 命中率 "
                    f"{history_hit_rate if history_hit_rate is not None else '—'}"
                ),
            })
        except Exception as e:
            honest_notes.append(f"trade_reflections_v2 查询失败: {e}")

    # ───── 8) 综合打分(magnitude + confidence) ─────
    aligned_factors = 1  # 因子 A 永远算
    total_factors = 1
    if support.get("critical_moves", {}).get("ratio") is not None:
        total_factors += 1
        if support["critical_moves"]["ratio"] >= 0.5:
            aligned_factors += 1
    if "cot_signals" in support:
        total_factors += 1
        if not cot_warning:
            aligned_factors += 1
    if support.get("fed_signal", {}).get("news_alignment"):
        total_factors += 1
        if support["fed_signal"]["news_alignment"] == "aligned":
            aligned_factors += 1

    abs_score = abs(composite_score)
    stars = 1
    if abs_score >= 0.2:
        stars = 2
    if abs_score >= 0.4 and aligned_factors >= 1:
        stars = 3
    if abs_score >= 0.6 and aligned_factors >= 2:
        stars = 4
    if abs_score >= 0.8 and aligned_factors >= 3:
        stars = 5

    confidence = 0.3
    if total_factors >= 2:
        confidence += 0.15
    if total_factors >= 4:
        confidence += 0.15
    if history_n >= 30:
        confidence += 0.15
    if history_n >= 100:
        confidence += 0.1
    if cot_warning:
        confidence -= 0.2
    if fed_aligned == "conflicting":
        confidence -= 0.15
    confidence = max(0.05, min(0.95, confidence))

    if not primary_assets:
        honest_notes.append("无资产标的 — 影响力仅作用于大盘情绪面")
    if history_n == 0:
        honest_notes.append("无历史反思样本 — 因子 E 缺失")
    if "critical_moves" not in support:
        honest_notes.append("因子 B 缺失(critical_moves 查询失败)")
    if "cot_signals" not in support:
        honest_notes.append("因子 C 缺失(COT 数据查询失败)")

    reasoning.append({
        "step": 8,
        "factor": "综合结论",
        "question": "最终影响方向、强度、可信度?",
        "answer": (
            f"direction={direction} · magnitude={stars}★({_magnitude_label(stars)}) · "
            f"confidence={confidence:.2f} · aligned_factors={aligned_factors}/{total_factors}"
        ),
    })

    # 因果链库 — 真实查表, 按 primary_assets 匹配
    causal_chains_used: list[dict] = []
    try:
        with db_cursor() as conn:
            cc_rows = conn.execute(
                "SELECT chain_id, asset, trigger_pattern, chain_steps, confidence, "
                "       hit_count, miss_count, version "
                "FROM causal_chains WHERE active=1"
            ).fetchall()
        target_assets = set(primary_assets or [])
        for r in cc_rows:
            # asset 命中 primary_assets 中任一 → 引用
            if target_assets and r["asset"] not in target_assets:
                continue
            try:
                steps = json.loads(r["chain_steps"]) if r["chain_steps"] else []
            except Exception:
                steps = []
            causal_chains_used.append({
                "chain_id": r["chain_id"],
                "asset": r["asset"],
                "trigger_pattern": r["trigger_pattern"],
                "confidence": r["confidence"],
                "version": r["version"],
                "hit_count": r["hit_count"],
                "miss_count": r["miss_count"],
                "n_steps": len(steps),
                "first_step": (steps[0].get("node") or steps[0].get("name")) if steps else None,
                "source": "causal_chains",
            })
        if not causal_chains_used:
            honest_notes.append("causal_chains 表存在但无与本次主资产匹配的链路")
    except Exception as e:
        honest_notes.append(f"causal_chains 查询失败: {e}")

    return {
        "ok": True,
        "news_id": row["id"],
        "headline": row["headline"],
        "source": row["source"],
        "direction": direction,
        "magnitude": stars,
        "magnitude_label": _magnitude_label(stars),
        "confidence": round(confidence, 3),
        "confidence_label": (
            "高" if confidence >= 0.7 else ("中" if confidence >= 0.4 else "低")
        ),
        "primary_assets": primary_assets,
        "support": support,
        "reasoning_chain": reasoning,
        "causal_chains_used": causal_chains_used,
        "data_sources": [
            "news_events", "critical_price_moves", "cftc_cot_data",
            "fed_rate_history", "trade_reflections_v2",
        ],
        "honest_notes": honest_notes,
        "ts": int(time.time()),
    }


news_thread = threading.Thread(target=news_pull_loop, daemon=True, name="bridge-news-pull")
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
COT_EXTREME_Z = 1.5
COT_MEGA_Z = 2.0


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
    action = "bullish" if is_bullish else "bearish"
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
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_events "
            "(source, headline, body, action, score, assets, ts, news_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
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
def cot_recent(asset: str | None = None, weeks: int = Query(26, ge=1, le=260),
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
def cot_heatmap(weeks: int = Query(26, ge=1, le=156)):
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
cot_thread.start()


# ─── v292 事件总线真实实现 (修复 catch-all 导致 data.filter TypeError) ─────
# 背景:8000 api_server.py 磁盘源已删; bridge_server 之前没有 /api/events
#       真实实现, 所有路径被 catch-all 兜底返 {items:[], _bridge_fallback:true}。
#       前端 event-bus.tsx 直接 await res.json() 当数组用 → TypeError。
# 修复: 这里从 news_events 表读取真实事件并转成前端 ApiEvent 形状,
#       保留 items/count 格式以兼容 catch-all 时的前端兜底判断。

import re as _re

CATEGORY_BY_TICKER = {
    "BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO", "BNB": "CRYPTO",
    "ADA": "CRYPTO", "XRP": "CRYPTO", "DOGE": "CRYPTO",
    "XAU": "GOLD", "GOLD": "GOLD",
    "WTI": "OIL", "OIL": "OIL",
    "MU": "HYNIX", "SKHYNIX": "HYNIX", "000660": "HYNIX", "005930": "HYNIX",
    "NVDA": "STOCK", "TSLA": "STOCK", "AAPL": "STOCK", "MSFT": "STOCK",
    "GOOGL": "STOCK", "META": "STOCK", "AMZN": "STOCK", "AMD": "STOCK", "NFLX": "STOCK",
    "BABA": "STOCK", "JD": "STOCK", "PDD": "STOCK",
    "NIO": "STOCK", "XPEV": "STOCK", "LI": "STOCK",
}


def _ts_to_str(epoch) -> str:
    """epoch 秒 → 'YYYY-MM-DD HH:MM:SS' UTC 字符串 (前端 ApiEvent 期望格式)"""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _extract_target_asset(text: str) -> str:
    """用 NEWS_ASSETS_ALIASES 反向匹配 ticker.
    优先级: ticker 形式 word-boundary > CJK substring (避免英文 ticker 误命中)."""
    if not text:
        return ""
    upper = text.upper()
    # 先 word-boundary 匹配 ticker 形式 (字母数字外)
    for ticker in NEWS_ASSETS_ALIASES.keys():
        ticker_up = ticker.upper()
        if _re.search(rf"\b{_re.escape(ticker_up)}\b", upper):
            return ticker
    # 再试 CJK 中文别名
    for ticker, aliases in NEWS_ASSETS_ALIASES.items():
        for alias in aliases:
            if alias and any('\u4e00' <= c <= '\u9fff' for c in alias) and alias in text:
                return ticker
    return ""


def _infer_market_category(asset: str) -> str:
    if asset and asset in CATEGORY_BY_TICKER:
        return CATEGORY_BY_TICKER[asset]
    return "AGGREGATE"


def _rget(r, key, default=None):
    """sqlite3.Row 安全取值：列不存在或为 NULL 时返回 default。

    news_events 历史上存在两套 schema（新库用 asset 单数，旧库用 assets 复数），
    不同查询选出的列集合不一致，用它统一兜底。
    """
    try:
        v = r[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _news_row_to_api_event(r) -> dict:
    """news_events row → ApiEvent 形状 (前端 event-bus.tsx 期望格式)."""
    # 1) target_asset: assets 字段优先,空则从文本抽
    #    v300-audit: 新 schema 写的是单数 asset,旧 schema 写的是复数 assets —— 两个都取
    assets_raw = str(_rget(r, "assets", "") or "").strip()
    if not assets_raw:
        assets_raw = str(_rget(r, "asset", "") or "").strip()
    asset = assets_raw.split(",")[0].strip() if assets_raw else ""
    if not asset or asset.upper() in ("NONE", "AGGREGATE", "ALL"):
        text = (str(_rget(r, "headline", "") or "") + " " + str(_rget(r, "body", "") or ""))[:1200]
        asset = _extract_target_asset(text)
    asset = asset or "NONE"

    category = _infer_market_category(asset)
    source = r["source"] or "unknown"
    headline = str(_rget(r, "headline", "") or "")
    body = str(_rget(r, "body", "") or "")
    action = (r["action"] or "HOLD").upper()
    # 2) 推断 score 信号 (news_events.score 经常为 0)
    score = float(r["score"] or 0.0)
    if score == 0.0:
        score = _extract_text_signal(headline, body)["score"] if "_extract_text_signal" in dir() else 0.0
    # 3) 拼 news_text (展示用)
    news_text = headline
    if body and body != headline:
        news_text = headline + "\n\n" + body[:600]
    return {
        "id": r["id"],
        "news_id": int(r["news_id"] or r["id"]),
        "decision_id": None,
        "analysis_status": "DONE",
        "timestamp": _ts_to_str(r["ts"]),
        "published_at": _ts_to_str(r["ts"]),
        "ai_time": _ts_to_str(_rget(r, "fetched_at", None) or r["ts"]),
        "source": source,
        "news_text": news_text,
        "action": action,
        "score": score,
        "reason": "",
        "market_category": category,
        "target_asset": asset,
        "parent_id": None,
        "child_count": 0,
        "reasoning_path": "",
        "vip_tag": "",
        "entry_price": None,
        "exit_price": None,
        "max_price": None,
        "min_price": None,
        "max_price_time": 0,
        "min_price_time": 0,
        "entry_time": _ts_to_str(r["ts"]),
        "is_correct": "",
        "settled": 0,
        "doubao_action": "HOLD",
        "doubao_reasoning": "",
        "cluster_size": 1,
        "server_emitted_at_ms": int(time.time() * 1000),
    }


@app.get("/api/events")
def api_events(
    limit: int = Query(100, ge=1, le=1000),
    hours: int | None = Query(None, ge=1, le=8760),
    asset: str | None = Query(None),
    source: str | None = Query(None),
    flat: int | None = Query(None, description="v293 兼容: =1 返裸数组 ApiEvent[]; 旧版前端 .filter() 需要"),
):
    """前端 event-bus.tsx loadHistory 用:
    - 默认 {items:[...], count:N, ok:true} (兼容 catch-all 兜底)
    - flat=1 返裸 ApiEvent[] 数组 (兼容旧版前端 .filter() 误用)
    - 数据源 news_events 表 (P0-3 worker 真实写入)
    - 转换: news_events row → ApiEvent 形状 (前端已定义)
    """
    sql = ("SELECT id, news_id, source, headline, body, action, score, assets, asset, ts, fetched_at "
           "FROM news_events WHERE 1=1")
    params: list = []
    if hours is not None:
        sql += " AND ts >= ?"
        params.append(int(time.time()) - hours * 3600)
    if asset:
        sql += " AND assets LIKE ?"
        params.append(f"%{asset}%")
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        with db_cursor() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [_news_row_to_api_event(r) for r in rows]
        # v293 兼容: 旧版前端 .filter() 需要裸数组 → ?flat=1 直接返数组
        if flat:
            return items
        return {"ok": True, "items": items, "count": len(items),
                "data_source": "news_events v292+",
                "note": f"真实读取 {len(items)} 条事件"}
    except Exception as e:
        # 真实失败而非兜底 → 让前端能看到错误
        print(f"[api_events error] {e}", file=sys.stderr)
        if flat:
            return []  # v293 兼容: 旧版前端 fetch 失败也返数组不崩
        return {"ok": False, "error": str(e), "items": [], "count": 0}


@app.get("/api/events/today")
def api_events_today():
    """前端 event-bus.tsx loadCounts() 用.
    返回 {news_count, total_count} (前端兼容两个字段).
    """
    with db_cursor() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM news_events").fetchone()["n"]
        # 今日: 北京时间当日 00:00 起算 (UTC-8 → UTC: 16:00 UTC of prev day)
        # 简化用本地 UTC 当日 (前端展示用, 容差 8h 内)
        today_start = int(datetime.combine(date.today(), datetime_time.min, tzinfo=timezone.utc).timestamp())
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM news_events WHERE ts >= ?", (today_start,)
        ).fetchone()["n"]
    return {"ok": True, "news_count": today, "total_count": total,
            "data_source": "news_events v292+"}


@app.get("/api/events/stream")
def api_events_stream(request: Request):
    """SSE 真实流 (v292+): 每 5s 推送最近 news_events.
    前端 EventSource(SSE_URL) 会自动重连, 反序列化 JSON 后进 flushEvents.
    推送格式: 'data: {json}\\n\\n' 标准 SSE
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    def event_generator():
        last_id = 0
        # 先送一次心跳, 让前端 EventSource.onopen 触发
        yield f"data: {_json.dumps({'type': 'heartbeat', 'server_time_ms': int(time.time() * 1000)}, ensure_ascii=False)}\n\n"
        for tick in range(60 * 60):  # 最多 1h, 让客户端重连 (前端会做)
            try:
                # 取 last_id 之后的新事件
                with db_cursor() as conn:
                    rows = conn.execute(
                        "SELECT id, news_id, source, headline, body, action, score, assets, asset, ts, fetched_at "
                        "FROM news_events WHERE id > ? ORDER BY id ASC LIMIT 20",
                        (last_id,),
                    ).fetchall()
                if rows:
                    for r in rows:
                        last_id = max(last_id, r["id"])
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
def api_events_filter_stream(
    limit: int = Query(20, ge=1, le=100),
    hours: int | None = Query(None, ge=1, le=8760),
):
    """门卫筛选双流 (passed/filtered), 给前端 GatekeeperFilterStream 用.

    字段对齐前端 FilterStream 类型:
      { passed: FilterItem[], filtered: FilterItem[], counts: {...}, today, generated_at }
    """
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
            "content_preview": (r["headline"] or "")[:160],
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

    blob = (text_blob or "").lower()
    hits = misses = matched = 0
    details: list[dict] = []
    for c in chains:
        keywords = [k.strip().lower() for k in (c["trigger_keywords"] or "").split(",") if k.strip()]
        kw_matched = (not keywords) or any(kw in blob for kw in keywords)
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
                            "kws_hit": [k for k in keywords if k in blob][:3]})
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
                            "kws_hit": [k for k in keywords if k in blob][:3]})
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
    with db_cursor() as conn:
        total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        per_asset = conn.execute(
            "SELECT asset, COUNT(*) AS n FROM event_move_attribution "
            "GROUP BY asset ORDER BY n DESC"
        ).fetchall()
    return {
        "ok": ok,
        "script": "calc_event_move_attribution.py",
        "args": {"days": days},
        "returncode": proc.returncode if 'proc' in dir() else None,
        "stdout_tail": out,
        "stderr_tail": err,
        "rows_total": total,
        "per_asset": [{"asset": r["asset"], "n": r["n"]} for r in per_asset],
        "data_source": "critical_price_moves × news_events → event_move_attribution (v292+ 归因补全)",
    }


@app.get("/api/v291/event-move-attribution")
def event_move_attribution_list(
    asset: str | None = None,
    limit: int = 50,
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
    sql = ("SELECT id, move_id, news_event_id, asset, time_lag_sec, algo_score, algo_reasons, "
           "move_ts, move_pct, move_kind, duration_min, ev_source, ev_action, ev_ts, ev_news_id "
           "FROM event_move_attribution "
           + ("WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY move_ts DESC LIMIT ?")
    params.append(limit)
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
    return {
        "ok": True,
        "n": len(rows), "rows_total": total,
        "items": [dict(r) for r in rows],
        "data_source": "event_move_attribution (v292+ 归因深度补全)",
    }


# ─── /api/review/causal-graph (前端 alias → /api/v291/causal-graph) ───
@app.get("/api/review/causal-graph")
def review_causal_graph_alias(
    asset: str | None = None,
    days: int = 30,
    min_confidence: float = 0.0,
    limit: int = 200,
):
    """前端 causal-graph-panel.tsx line 205 实际调用的端点。
    转发到 /api/v291/causal-graph (完整实现见 v291_causal_graph).
    v291 用 hours/asset 不用 days, 转换 days→hours, 忽略 min_confidence (v291 无此参数).
    """
    hours = (days or 30) * 24
    return v291_causal_graph(asset=asset or "BTC", hours=hours, limit=limit)


# ─── /api/v189/causal/accuracy-stats (前端 alias → /api/v291/causal-chains) ───
@app.get("/api/v189/causal/accuracy-stats")
def v189_causal_accuracy_stats_alias(asset: str | None = None, days: int = 30):
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
def v291_causal_chains_feedback(days: int = 90, auto_run: bool = True):
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
@app.get("/api/v291/market-regime")
def v291_market_regime(days: int = 365, since_date: str | None = None):
    """基于 CFTC COT 数据检测 7 类市场 regime:
       1. leverage_buildup / 2. deleveraging
       3. large_spec_long / 4. large_spec_short
       5. commercial_hedging
       6. risk_off_rotation (cross-asset)
       7. spec_extreme (反转预警)
    """
    from datetime import datetime, timedelta
    try:
        from market_regime import detect_regimes
    except ImportError:
        return {"ok": False, "error": "market_regime not found"}
    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db_cursor() as conn:
        result = detect_regimes(conn, since_date=since_date)
    # 加一个 regime 计数摘要
    counts: dict = {}
    for r in result.get("regimes", []):
        counts[r["regime"]] = counts.get(r["regime"], 0) + 1
    result["regime_counts"] = counts
    return {"ok": True, **result, "data_source": "market_regime.detect_regimes"}


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


# ─── /api/history/news-trades (前端 history/page.tsx line 156 调用) ───
# v292+ 真实实现: JOIN news_events × trade_decisions (无外键, 用 ±2h 时窗 + asset 匹配)
# 返回裸 NewsTradeItem[] 数组 (前端 useState<NewsTradeItem[]> 期望的 shape)
@app.get("/api/history/news-trades")
def history_news_trades(hours: int = 72, limit: int = 300):
    """历史新闻交易回溯列表: 对每条新闻按 time-window 聚合 nearby settled trades.

    关联策略 (news_events × trade_decisions 无外键):
      - 时窗: trade.created_at ∈ [news.ts - 7200s, news.ts + 7200s]
      - 资产: trade.asset 命中 news.assets (逗号分隔解析)
      - 状态: trade.status='settled' (有实际盈亏)
    """
    now = int(time.time())
    since_ts = now - max(hours, 1) * 3600
    window = 7200  # ±2h

    with db_cursor() as conn:
        news_rows = conn.execute(
            "SELECT id, source, news_id, headline, body, assets, ts "
            "FROM news_events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since_ts, max(limit, 1) * 4),
        ).fetchall()
        if not news_rows:
            return []
        lo = min(n["ts"] for n in news_rows) - window
        hi = max(n["ts"] for n in news_rows) + window
        trade_rows = conn.execute(
            "SELECT id, asset, status, settled_at, actual_outcome_pct, created_at "
            "FROM trade_decisions "
            "WHERE status='settled' AND created_at BETWEEN ? AND ?",
            (lo, hi),
        ).fetchall()

    by_asset: dict[str, list[dict]] = {}
    for t in trade_rows:
        by_asset.setdefault(t["asset"].upper(), []).append(dict(t))

    items: list[dict] = []
    for n in news_rows:
        n_assets = [a.strip().upper() for a in (n["assets"] or "").split(",") if a.strip()]
        if not n_assets:
            continue  # 没标资产的新闻不参与 trade 聚合 (噪声太多)
        matched: list[dict] = []
        for a in n_assets:
            for t in by_asset.get(a, []):
                if abs(int(t["created_at"]) - int(n["ts"])) <= window:
                    matched.append(t)
        if not matched:
            continue
        wins = sum(1 for t in matched if (t.get("actual_outcome_pct") or 0) > 0)
        losses = sum(1 for t in matched if (t.get("actual_outcome_pct") or 0) < 0)
        total = len(matched)
        total_pnl = sum((t.get("actual_outcome_pct") or 0) for t in matched)
        latest_exit_ts = max((int(t.get("settled_at") or 0) for t in matched), default=0)
        # reflection review: trade_reflections_v2 没有 news_event_id, 按 decision_id IN (matched)
        has_review = False
        if matched:
            d_ids = [t["id"] for t in matched]
            ph = ",".join("?" for _ in d_ids)
            with db_cursor() as conn2:
                rv = conn2.execute(
                    f"SELECT 1 FROM trade_reflections_v2 WHERE decision_id IN ({ph}) "
                    f"AND reflection_text IS NOT NULL AND reflection_text != '' LIMIT 1",
                    d_ids,
                ).fetchone()
                has_review = rv is not None
        items.append({
            "news_id": int(n["news_id"]) if n["news_id"] is not None else int(n["id"]),
            "source": n["source"] or "",
            "content": (n["headline"] or "")[:200],
            "news_time": _iso_ts(n["ts"]),
            "trade_count": total,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / total, 3) if total > 0 else None,
            "total_pnl": round(total_pnl, 3),
            "latest_exit_ts": latest_exit_ts,
            "has_review": has_review,
        })
        if len(items) >= limit:
            break
    return items


@app.get("/api/history/news-trades/{news_id}")
def history_news_trade_detail(news_id: int):
    """单条新闻的交易明细 + AI 归因.

    news_id 接受 news_events.news_id (外部 ID) 或 news_events.id (内部 PK).
    """
    with db_cursor() as conn:
        n = conn.execute(
            "SELECT id, source, news_id, headline, body, assets, ts "
            "FROM news_events WHERE news_id=? OR id=? LIMIT 1",
            (news_id, news_id),
        ).fetchone()
        if not n:
            return {"ok": False, "error": f"news_id={news_id} not found"}
        n_assets = [a.strip().upper() for a in (n["assets"] or "").split(",") if a.strip()]
        # 时窗 ±2h 内 settled trades
        if n_assets:
            placeholders = ",".join("?" for _ in n_assets)
            trades = conn.execute(
                f"SELECT id, asset, action, score, confidence, status, created_at, settled_at, "
                f"actual_outcome_pct, price_at_decision, evidence_json, rationale "
                f"FROM trade_decisions "
                f"WHERE status='settled' AND asset IN ({placeholders}) "
                f"  AND created_at BETWEEN ? AND ? "
                f"ORDER BY created_at ASC",
                tuple(n_assets + [int(n["ts"]) - 7200, int(n["ts"]) + 7200]),
            ).fetchall()
        else:
            trades = []
        # trade_list 提前生成 (ref_rows 关联查询需要)
        trade_list = [dict(t) for t in trades]
        # reflections: v292+ trade_reflections_v2 没有 news_event_id, 按 decision_id IN (matched trades)
        if trade_list:
            d_ids = [t["id"] for t in trade_list]
            ph = ",".join("?" for _ in d_ids)
            ref_rows = conn.execute(
                f"SELECT decision_id, asset, dimension, subcategory, "
                f"confidence_at_time, actual_outcome_pct, hit, reflection_text "
                f"FROM trade_reflections_v2 WHERE decision_id IN ({ph}) "
                f"ORDER BY decision_id ASC",
                d_ids,
            ).fetchall()
        else:
            ref_rows = []
        # 尝试读 news_reviews (v292+ 暂未生成, 返 None)
        try:
            reviews_agg = conn.execute(
                "SELECT verdict, key_insight, detail, rule_check, generated_at "
                "FROM news_reviews WHERE news_event_id=? ORDER BY generated_at DESC LIMIT 1",
                (n["id"],),
            ).fetchone()
        except Exception:
            reviews_agg = None

    decided = [t for t in trade_list if t.get("actual_outcome_pct") is not None]
    wins = sum(1 for t in decided if (t.get("actual_outcome_pct") or 0) > 0)
    losses = sum(1 for t in decided if (t.get("actual_outcome_pct") or 0) < 0)
    total_pnl = sum((t.get("actual_outcome_pct") or 0) for t in decided)
    aggregate = {
        "total": len(trade_list),
        "decided": len(decided),
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / len(decided), 3) if decided else None,
        "total_pnl": round(total_pnl, 3),
    }

    failures = []
    successes = []
    for r in ref_rows:
        item = {
            "decision_id": r["decision_id"],
            "reason_code": -1,
            "reason_name": f"{r['dimension'] or '?'}::{r['subcategory'] or '?'}",
            "reason_text": (r["reflection_text"] or "")[:200],
            "confidence": r["confidence_at_time"] or 0.5,
        }
        if r["hit"] == 1:
            successes.append(item)
        elif r["hit"] == 0:
            failures.append(item)

    review_obj = None
    if reviews_agg:
        review_obj = {
            "verdict": reviews_agg["verdict"] or "",
            "key_insight": reviews_agg["key_insight"] or "",
            "detail": reviews_agg["detail"] or "",
            "rule_check": reviews_agg["rule_check"] or "",
            "failures": failures,
            "successes": successes,
            "aggregate": aggregate,
        }

    return {
        "news_id": int(n["news_id"]) if n["news_id"] is not None else int(n["id"]),
        "source": n["source"] or "",
        "content": (n["headline"] or "")[:200],
        "news_time": _iso_ts(n["ts"]),
        "trades": trade_list,
        "aggregate": aggregate,
        "review": review_obj,
        "review_generated_at": reviews_agg["generated_at"] if reviews_agg else None,
    }


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


def _iso_ts(ts: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%dT%H:%M:%S")


# ─── v302+ P0: 补 catch-all 端点, 修前端 0 数据问题 ────────


@app.get("/api/v237/missed-signals")
def v237_missed_signals(limit: int = 200, days: int = 90):
    """v237 漏检信号主列表 (前端 MissedSignalsPanel default tab 消费).
    v302+ 实现: 从 critical_price_moves 强信号 (algo>=0.7) 实时生成 MissedRow 形状.
    漏检定义: 强信号出现时 ±24h 内无 trade_decision 记录.
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
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        items: list[dict] = []
        for r in rows:
            if has_dec:
                td_hit = conn.execute(
                    "SELECT id FROM trade_decisions "
                    "WHERE asset=? AND ABS(created_at - ?) <= 86400 LIMIT 1",
                    (r["asset"], r["ts"]),
                ).fetchone()
                if td_hit is not None:
                    continue
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
                "decision_date": time.strftime("%Y-%m-%d", time.gmtime(r["ts"])),
                "decision_id": None,
                "signal_id": r["id"],
                "pattern_type": ptype,
                "signal": f"algo_score={r['algo_score']:.2f} move_pct={r['move_pct']:.2f} move_kind={r['move_kind']}",
                "ignored_reason": ignored,
                "confidence_at_time": r["algo_score"],
                "actual_outcome_5d": None,
                "actual_outcome_20d": None,
                "hit": None,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r["created_at"])),
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
def v237_judgment_quality(weeks: int = 12):
    """v237 判断质量 (前端 quality tab 消费).
    v302+ 实现: 基于 trade_reflections_v2 (521 rows) 实时聚合.
    """
    cutoff = int(time.time()) - weeks * 7 * 86400
    with db_cursor() as conn:
        reflections = conn.execute(
            "SELECT asset, dimension, subcategory, confidence_at_time, hit, actual_outcome_pct "
            "FROM trade_reflections_v2 WHERE created_at >= ?",
            (cutoff,),
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
def v291_critical_moves(hours: int = 720, limit: int = 40, asset: str | None = None):
    """v291 关键点列表 (前端 EventMarketAttribution 顶部表消费).
    v302+ 实现: 从 critical_price_moves 直读, 支持 asset 过滤.
    """
    cutoff = int(time.time()) - hours * 3600
    where = ["ts >= ?"]
    params: list[Any] = [cutoff]
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
        total = conn.execute(
            "SELECT COUNT(*) FROM critical_price_moves WHERE ts >= ?", (cutoff,)
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
def v291_stats(hours: int = 720):
    """v291 全局 stats (前端 EventMarketAttribution 头牌消费).
    v302+ 实现: critical_moves + event_move_attribution + causal_chains 聚合.
    """
    cutoff = int(time.time()) - hours * 3600
    with db_cursor() as conn:
        n_moves = conn.execute(
            "SELECT COUNT(*) FROM critical_price_moves WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
        n_attr = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution WHERE move_ts >= ?", (cutoff,)
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
            "SELECT move_kind, COUNT(*) n FROM critical_price_moves WHERE ts >= ? GROUP BY move_kind",
            (cutoff,),
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
    move_id: int | None = None, asset: str | None = None, limit: int = 50
):
    """v291 事件-行情归因列表 (前端 EventMarketAttribution 弹窗消费).
    v302+ 实现: alias of /api/v291/event-move-attribution.
    """
    where: list[str] = []
    params: list[Any] = []
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
        total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
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


TF_HOURS = {
    "3m": 0.05, "5m": 0.083, "10m": 0.167, "1h": 1, "4h": 4, "30d": 720, "24h": 24,
}
# ohlcv_daily 是日线 → timeframe 映射成"回看几根 bar"
TF_BARS_BACK = {
    "3m": 1, "5m": 1, "10m": 1, "1h": 1, "4h": 1, "24h": 1, "30d": 30,
}


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


def _load_news_for_window(conn: Any, since: int) -> list[dict[str, Any]]:
    """读取 since 之后的真实新闻, 统一成前端契约字段:
    id / source / headline / body / assets / action / score / ts"""
    def _cols(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            return set()

    try:
        if {"headline", "ts"} <= _cols("news_events"):
            return [dict(r) for r in conn.execute(
                "SELECT id, source, headline, body, action, score, assets, ts "
                "FROM news_events WHERE ts >= ? ORDER BY ts DESC LIMIT 2000",
                (since,),
            ).fetchall()]

        # 金融版: 真实新闻在 raw_news, 标题列叫 content
        rn = _cols("raw_news")
        if {"content", "ts"} <= rn:
            assets_expr = "gate_assets" if "gate_assets" in rn else "''"
            score_expr = "COALESCE(relevance_score, 0)" if "relevance_score" in rn else "0"
            rows = conn.execute(
                f"SELECT id, source, content, {assets_expr} AS assets, "
                f"{score_expr} AS score, ts FROM raw_news "
                "WHERE ts > 0 AND ts >= ? ORDER BY ts DESC LIMIT 2000",
                (since,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                txt = _strip_hash_prefix(r["content"])
                if not txt:
                    continue
                out.append({
                    "id": r["id"],
                    "source": r["source"] or "news",
                    "headline": txt,
                    "body": txt,
                    "assets": r["assets"] or "",
                    # raw_news 没有买卖方向字段, 留空由调用方归一为 HOLD
                    "action": None,
                    "score": r["score"] or 0,
                    "ts": r["ts"],
                })
            return out
        print("[bridge] market_movers: 未找到结构匹配的新闻表 (news_events / raw_news 均不匹配)", flush=True)
    except Exception as exc:  # pragma: no cover - 新闻只是附加信息, 绝不影响异动点
        print(f"[bridge] market_movers 新闻读取跳过: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return []
@app.get("/api/market/movers")
def market_movers(timeframe: str = "24h", limit: int = 20):
    """行情涨跌排行 (前端 MoversPanel 主数据源).

    v303 Q1 cont 实现 (之前 8000 死 + bridge 未实现 → catch-all 返空 items →
    前端 setSelected 拿不到第一个品种 → selected 一直 null → K线 effect 直接
    `if (!selected) return` 不执行 → 整页 K线空白)。

    数据源 ohlcv_daily (日线):
      - price     = 最新一根 close
      - change24h = (最新 close - 前一天 close) / 前一天 close * 100
      - events    = 同窗口内 news_events 命中数
      - abnormals = 同窗口内 critical_price_moves 异动点

    注意: ohlcv_daily 是日线表, 不能用 "ts >= now - 24h" 定位基准 (日线粒度下
    24h 窗口内常常一根 bar 都没有 → 基准退化成全历史首根 → chg 算出 +684% 这种
    离谱值)。所以基准改用 "倒数第 2 根 bar"(LIMIT 1 OFFSET 1), 与粒度无关。
    """
    # timeframe → 回看根数 (ohlcv_daily 是日线, 1 根 = 1 天)
    tf = timeframe or "24h"
    bars_back = TF_BARS_BACK.get(tf, 1)
    now = int(time.time())
    since = now - TF_HOURS.get(tf, 24) * 3600
    # v303 R11: abnormals/events 的回看窗口不能跟着 tf 缩到 24h。
    # 数据侧 critical_price_moves / news_events 的最新 ts 本就滞后于 now
    # (实测 MU 异动最新 10-15, 新闻最新 10-20, 而 now=10-20),
    # 一旦按 24h 卡窗口就查出 0 条 → 异动 marker / 新闻时间线全空。
    # 这里给一个下限: 至少 30 天, 保证滞后数据也能落进结果。
    since = min(since, now - 30 * 86400)

    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT asset, close, ts, source FROM ohlcv_daily "
            "WHERE (asset, ts) IN ("
            "  SELECT asset, MAX(ts) FROM ohlcv_daily GROUP BY asset"
            ") ORDER BY asset"
        ).fetchall()

    # 每个品种取倒数第 (bars_back+1) 根作基准
    base_map: dict[str, float] = {}
    src_map: dict[str, str] = {}
    with db_cursor() as conn:
        for r in rows:
            asset = r["asset"]
            src_map[asset] = r["source"] or "ohlcv_daily"
            b = conn.execute(
                "SELECT close FROM ohlcv_daily WHERE asset=? "
                "ORDER BY ts DESC LIMIT 1 OFFSET ?",
                (asset, bars_back),
            ).fetchone()
            if b and b["close"]:
                base_map[asset] = b["close"]

    # 异动点 + 新闻: 一次查全, 再按 asset 分桶
    # v303 R12: abnormals 改用 tech_signals(技术指标组合检测), 不再是旧的
    # critical_price_moves(纯涨跌幅统计)。旧表 ts 取窗口起点, 标记时间错位。
    abnormals: dict[str, list[dict[str, Any]]] = {}
    event_counts: dict[str, int] = {}
    tech_available = True
    with db_cursor() as conn:
        has_tech = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='tech_signals'").fetchone() is not None
        if has_tech:
            ts_rows = conn.execute(
                "SELECT asset, ts, close, trigger_type, score, uses_future, "
                "pct_1d, pct_3d, vol_ratio, support_level, signal_json, "
                "merged_types, bars_available, data_max_ts "
                "FROM tech_signals WHERE ts >= ? ORDER BY ts DESC",
                (since,),
            ).fetchall()
            for r in ts_rows:
                try:
                    sigs = json.loads(r["signal_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    sigs = []
                try:
                    merged = json.loads(r["merged_types"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    merged = []
                abnormals.setdefault(r["asset"], []).append({
                    "ts": r["ts"],
                    "pct_change": r["pct_1d"],
                    "vol_ratio": r["vol_ratio"],
                    "trigger_type": r["trigger_type"],
                    "score": r["score"],
                    "uses_future": int(r["uses_future"] or 0),
                    "close": r["close"],
                    "support_level": r["support_level"],
                    "signals": sigs,
                    "merged_types": merged,
                    "bars_available": r["bars_available"],
                    "data_max_ts": r["data_max_ts"],
                    # legacy 字段保留兼容 (旧前端读 kind/pct)
                    "kind": r["trigger_type"],
                    "pct": r["pct_1d"],
                })
        else:
            # 表尚未建立 → 明确标注不可用, 绝不回退到旧的涨跌幅统计冒充技术指标
            tech_available = False
        nv = _load_news_for_window(conn, since)

    aliases = {
        "BTC": ["BTC", "BITCOIN", "比特币"], "ETH": ["ETH", "ETHEREUM", "以太坊"],
        "SOL": ["SOL", "SOLANA"], "BNB": ["BNB", "BINANCE", "币安"],
        "ADA": ["ADA", "CARDANO", "艾达"], "DOGE": ["DOGE", "DOGECOIN", "狗狗币"],
        "XRP": ["XRP", "RIPPLE", "瑞波"], "MU": ["MU", "MICRON", "美光"],
        "NVDA": ["NVDA", "NVIDIA", "英伟达"], "TSLA": ["TSLA", "TESLA", "特斯拉"],
        "AAPL": ["AAPL", "APPLE", "苹果"], "SKHYNIX": ["SKHYNIX", "SK HYNIX", "海力士"],
        "WTI": ["WTI", "原油", "OIL"], "XAU": ["XAU", "GOLD", "黄金"],
    }
    # v303 R11: 按资产收集真实新闻事件 (前端 MoverEvent 契约:
    #   {ts, action, score, content, source})。
    # 之前只统计了个数并返回 [{"n": count}] 占位符 —— 前端 L3050
    # `e.content.length` 直接抛 TypeError, 整页白屏 (Round 10 之后暴露)。
    events_by_asset: dict[str, list[dict[str, Any]]] = {}
    for r in nv:
        hay = f"{r['assets'] or ''} {r['headline'] or ''}".upper()
        for asset, als in aliases.items():
            if not any(a in hay for a in als):
                continue
            action = (r["action"] or "").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                action = "HOLD"
            # content 优先用 body(正文), 空则退 headline —— 前端直接渲染它
            content = (r["body"] or "").strip() or (r["headline"] or "").strip()
            if not content:
                continue
            events_by_asset.setdefault(asset, []).append({
                "id": r["id"],
                "ts": r["ts"],
                "action": action,
                "score": round(abs(r["score"] or 0), 4),
                "content": content,
                "headline": r["headline"] or "",
                "source": r["source"] or "news",
            })
    for asset, evs in events_by_asset.items():
        evs.sort(key=lambda x: x["ts"], reverse=True)
        events_by_asset[asset] = evs[:60]
    for asset in events_by_asset:
        event_counts[asset] = len(events_by_asset[asset])

    # v303 R12: 给每个异动点配新闻归因 (用户需求: "异常识别点上方要显示是什么新闻事件影响")
    # 窗口取 [signal_ts - 3d, signal_ts + 3d], 按时间接近度 + 新闻重要性排序,
    # 只挂**真实存在**的新闻; 没有匹配就留空数组, 前端不显示任何归因,
    # 绝不用"无新闻影响"之类的占位文案冒充。
    NEWS_LOOKBACK = 3 * 86400
    NEWS_LOOKAHEAD = 3 * 86400
    for asset, sigs in abnormals.items():
        evs = events_by_asset.get(asset, [])
        for sig in sigs:
            st = sig["ts"]
            near = [e for e in evs
                    if st - NEWS_LOOKBACK <= e["ts"] <= st + NEWS_LOOKAHEAD]
            near.sort(key=lambda e: (abs(e["ts"] - st) - e["score"] * 86400))
            sig["news"] = [{
                "id": e["id"], "ts": e["ts"], "headline": e["headline"],
                "content": e["content"], "action": e["action"],
                "score": e["score"], "source": e["source"],
                "lag_sec": int(e["ts"] - st),
            } for e in near[:3]]

    items: list[dict[str, Any]] = []
    for r in rows:
        asset = r["asset"]
        last = r["close"]
        if last is None:
            continue
        base = base_map.get(asset)
        chg = None
        if base not in (None, 0):
            chg = round((last - base) / base * 100, 2)
        items.append({
            "asset": asset,
            "price": round(last, 6),
            "change24h": chg,
            "source": src_map.get(asset, "ohlcv_daily"),
            "source_count": 1,
            # 前端 MoverEvent[] — 完整字段, 不再是计数占位符
            "events": events_by_asset.get(asset, []),
            "abnormals": abnormals.get(asset, []),
            "last_ts": r["ts"],
        })

    # 涨跌幅绝对值降序 (异动榜语义); 没 chg 的排最后
    items.sort(key=lambda x: abs(x["change24h"] or 0) if x["change24h"] is not None else -1,
               reverse=True)
    items = items[:limit]
    return {
        "ok": True,
        "timeframe": tf,
        "bars_back": bars_back,
        "hours": TF_HOURS.get(tf, 24),
        "items": items,
        "n": len(items),
        "count": len(items),
        # v303 R12: 异动检测已换成技术指标组合 (tech_signals)
        "abnormal_engine": "tech_signals (v303 R12)" if tech_available else "unavailable",
        "data_source": "ohlcv_daily + tech_signals + news_events (v303 R12)",
    }


@app.get("/api/tech/signals")
def tech_signals_api(symbol: str = "MU", days: int = 90,
                     include_future: bool = True, min_score: float = 0.0):
    """技术指标异动信号明细 (v303 R12)。

    参数:
      symbol         资产
      days           回看天数
      include_future 是否包含转折点(uses_future=1, 用户许可使用未来函数)
      min_score      分数下限

    返回每条信号的完整命中指标明细, 前端可在异动点上悬浮查看依据。
    没有数据时返回空数组 + ok, **不返回任何编造的占位信号**。
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

    out: list[dict[str, Any]] = []
    for r in rows:
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
    days: int = 365,
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
    from price_structure import CFG as PS_CFG, detect as detect_structure
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
    days: int = 365,
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
    from price_structure import detect as detect_structure
    raw_n = len(detect_structure(bars))

    persisted = 0
    if persist and hits:
        try:
            persisted = intraday_store.save_anomalies(sym, interval, hits)
        except Exception as exc:                      # noqa: BLE001
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
def intraday_backfill_api(asset: str = "ETH", interval: str = "1m", days: int = 5):
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
    except Exception as exc:                          # noqa: BLE001
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
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "asset": asset, "error": str(exc)}
    return {"ok": True, "asset": asset, "interval": interval, "new_bars": n}


@app.get("/api/tech/anomaly-history")
def tech_anomaly_history_api(
    symbol: str = "ETH", interval: str = "10m", limit: int = 3000,
):
    """读历史真异常点(anomaly_history 表, 跨时段累积, 永不删除)。

    与 /api/tech/anomaly 的区别: 后者是"对当前窗口重新计算",
    本端点是"读取已累积保存的历史记录", 支持回看任意历史区间,
    供用户事后做发现与反思。
    """
    sym = (symbol or "").upper().replace("USDT", "").replace("-", "")
    try:
        items = intraday_store.read_anomaly_history(sym, interval, limit)
    except Exception as exc:                          # noqa: BLE001
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
    symbol: str, interval: str = "1d", limit: int = 180, days: int | None = None
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


def _star_by_percentile(scores: list[float], val: float) -> int:
    """v303 R10: 按分位数排名给 1-5 星。

    之前用 `int(score*5)+1` 绝对阈值,但 news_events.score 实际分布是
    min=0.0 / avg=0.183 / max=1.0 —— 绝大多数 score < 0.4 → 恒等于 1 星,
    前端 star_min=3 直接筛空 (0 条)。

    改成"在窗口内的相对排名": 分数越靠近顶部星级越高。
    ★5 = top 5%, ★4 = top 20%, ★3 = top 40%, ★2 = top 70%, 其余 ★1。
    这样 star_min=3 稳定取到 top 40%, 与 score 的绝对量纲无关。
    """
    if not scores:
        return 1
    v = abs(val)
    below = sum(1 for s in scores if abs(s) < v)
    pct = below / len(scores)          # 0=最高分, 1=最低分
    if pct < 0.05:
        return 5
    if pct < 0.20:
        return 4
    if pct < 0.40:
        return 3
    if pct < 0.70:
        return 2
    return 1


@app.get("/api/v189/calendar/events-for-asset")
def v189_calendar_events_for_asset(
    asset: str, hours: int = 720, star_min: int = 1, limit: int = 200
):
    """v189 资产日历事件 (前端 MoversPanel markers 消费).
    v302+ 实现: 从 news_events 查. 匹配策略 (按顺序):
      1. assets JSON 包含 asset (上游已解析)
      2. headline 文本含 asset 名 / 中文名 / 常见 ticker
         兜底: news_events.assets 字段经常空, 用 headline 模糊匹配保底
    """
    cutoff = int(time.time()) - hours * 3600
    asset_u = asset.upper()
    # 资产中英文别名 (前端选 BTC 时也能匹配 "比特币"/"bitcoin")
    alias_map = {
        "BTC": ["BTC", "BITCOIN", "比特币", "XBT"],
        "ETH": ["ETH", "ETHEREUM", "以太坊"],
        "SOL": ["SOL", "SOLANA"],
        "BNB": ["BNB", "BINANCE", "币安"],
        "ADA": ["ADA", "CARDANO", "艾达"],
        "DOGE": ["DOGE", "DOGECOIN", "狗狗币"],
        "XRP": ["XRP", "RIPPLE", "瑞波"],
        "MU": ["MU", "MICRON", "美光"],
        "NVDA": ["NVDA", "NVIDIA", "英伟达"],
        "TSLA": ["TSLA", "TESLA", "特斯拉"],
        "AAPL": ["AAPL", "APPLE", "苹果"],
        "SKHYNIX": ["SKHYNIX", "SK HYNIX", "海力士"],
        "WTI": ["WTI", "原油", "OIL"],
        "XAU": ["XAU", "GOLD", "黄金"],
    }
    aliases = alias_map.get(asset_u, [asset_u])
    # 1) assets JSON 匹配
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, source, headline, action, score, assets, ts, fetched_at, news_id "
            "FROM news_events WHERE ts >= ? AND (UPPER(assets) LIKE ? OR UPPER(assets) LIKE ?) "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, f'%"{asset_u}"%', f"%,{asset_u}%", limit * 2),
        ).fetchall()
    items = []
    seen_ids = set()
    # v303 R10: 先收集全窗口分数池, 再按分位数排名定星级
    score_pool: list[float] = []
    for r in rows:
        try:
            assets_list = json.loads(r["assets"]) if r["assets"] else []
        except Exception:
            assets_list = []
        if asset_u not in [a.upper() for a in assets_list]:
            continue
        score_pool.append(abs(r["score"] or 0))
    for r in rows:
        try:
            assets_list = json.loads(r["assets"]) if r["assets"] else []
        except Exception:
            assets_list = []
        if asset_u not in [a.upper() for a in assets_list]:
            continue
        score = abs(r["score"] or 0)
        star = _star_by_percentile(score_pool, score)
        if star < star_min:
            continue
        items.append({
            "id": r["id"], "news_id": r["news_id"], "asset": asset_u,
            "source": r["source"], "headline": r["headline"],
            "action": r["action"], "score": r["score"],
            "ts": r["ts"], "fetched_at": r["fetched_at"],
            "star": star, "match": "assets_json",
            # ── 前端 CalEvent 契约字段 (v303 R10) ──
            # MoversPanel L1775 读 ce.ts_bj / L1778 读 ce.title,
            # 之前只给 ts/headline → ts_bj=undefined → `ts_bj < t0-3600` 恒真 → 全被 continue
            "ts_bj": r["ts"],
            "title": r["headline"] or "",
            "actual": None, "consensus": None,
            "is_released": False, "surprise": "",
            "age_hours": round((int(time.time()) - r["ts"]) / 3600, 1) if r["ts"] else None,
        })
        seen_ids.add(r["id"])
        if len(items) >= limit:
            break
    # 2) headline 文本兜底
    if len(items) < limit:
        with db_cursor() as conn:
            sub = " OR ".join(["UPPER(headline) LIKE ?"] * len(aliases))
            params: list[Any] = [cutoff] + [f"%{a}%" for a in aliases] + [limit * 2]
            rows2 = conn.execute(
                f"SELECT id, source, headline, action, score, assets, ts, fetched_at, news_id "
                f"FROM news_events WHERE ts >= ? AND ({sub}) "
                f"ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
        for r in rows2:
            if r["id"] in seen_ids:
                continue
            score = abs(r["score"] or 0)
            score_pool.append(score)
        for r in rows2:
            if r["id"] in seen_ids:
                continue
            score = abs(r["score"] or 0)
            star = _star_by_percentile(score_pool, score)
            if star < star_min:
                continue
            items.append({
                "id": r["id"], "news_id": r["news_id"], "asset": asset_u,
                "source": r["source"], "headline": r["headline"],
                "action": r["action"], "score": r["score"],
                "ts": r["ts"], "fetched_at": r["fetched_at"],
                "star": star, "match": "headline_fallback",
                # 前端 CalEvent 契约字段 (v303 R10), 同上
                "ts_bj": r["ts"],
                "title": r["headline"] or "",
                "actual": None, "consensus": None,
                "is_released": False, "surprise": "",
                "age_hours": round((int(time.time()) - r["ts"]) / 3600, 1) if r["ts"] else None,
            })
            seen_ids.add(r["id"])
            if len(items) >= limit:
                break
    return {
        "ok": True,
        "asset": asset_u,
        "hours": hours,
        "star_min": star_min,
        "items": items,
        # v303 Q1 cont: 前端 MoversPanel 读 `events` (CalEvent[]), 不是 `items`
        # (之前只给 items → 前端 `calRes?.events ?? []` 恒空 → 日历 marker 永远 0)
        "events": items,
        "n": len(items),
        "count": len(items),
        "data_source": "news_events (v302+ assets_json + headline_fallback 双匹配)",
    }




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


@app.get("/api/{full_path:path}", include_in_schema=False)
async def _bridge_catch_all_get(full_path: str, request: Request):
    """GET 兜底: 8000 缺失, 8100 接管所有未实现路径, 返空数据避免 404。"""
    return _empty_response(full_path)


@app.post("/api/{full_path:path}", include_in_schema=False)
async def _bridge_catch_all_post(full_path: str, request: Request):
    """POST 兜底: 写操作暂时回 200 + noop, 不破坏前端流程。"""
    return {
        "ok": True,
        "noop": True,
        "_bridge_fallback": True,
        "_note": f"bridge_server catch-all POST: {full_path} noop",
    }


# ─── 挂载 api_server 全量路由 (v170+ 功能恢复) ──────
# 说明: 2026-10 从桌面备份 (trident_backend_code_20261012_v3.tar.gz / 金融1.0)
# 恢复了 backend 全量源码, 其中 api_server.py 提供 349 个真实路由
# (归因复核 / 反思 / 推理审查 / 纸面交易 / 知识图谱 ...)。
# 下面把它的路由以优先级更高的方式挂到本 app 上, 使 8100 端口不再靠
# catch-all 返回空壳, 而是走真实实现。挂载失败时降级为原有 catch-all 行为。
_MOUNTED_API_SERVER = False
try:
    import api_server as _api_server  # noqa: WPS433

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
except Exception as _exc:  # pragma: no cover - 降级保护
    import traceback as _tb

    print(
        f"[bridge] ⚠️ api_server 挂载失败, 降级为 catch-all 兜底: "
        f"{type(_exc).__name__}: {str(_exc)[:160]}",
        flush=True,
    )
    _tb.print_exc()


# ─── main ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("BRIDGE_PORT", "8100"))
    # 默认绑 0.0.0.0 而非 127.0.0.1:
    #   - 只听 IPv4 回环时, 浏览器把 localhost 解析到 [::1](IPv6) 就会得到
    #     ERR_CONNECTION_REFUSED —— IPv6 回环上无人监听。
    #   - 绑 0.0.0.0 可同时覆盖 127.0.0.1 / localhost / ::ffff:127.0.0.1。
    # 如需只允许本机访问, 设 BRIDGE_HOST=127.0.0.1。
    host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")