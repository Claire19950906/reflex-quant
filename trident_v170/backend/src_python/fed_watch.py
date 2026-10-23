#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v276: 美联储全景监控(Fed Watch)— 把所有 Fed 相关信息一站式汇总。

覆盖范围:
  ① 当前路径(Fed 利率预期 + 2Y/10Y yield 曲线)
  ② 下次 FOMC 会议 + 未来 7d Fed 事件日历
  ③ 下一组关键数据(CPI / NFP / PCE)发布日期
  ④ 自适应异动阈值(根据 VIX 动态调整)
  ⑤ 历史相关性:2Y yield 移动 vs BTC/SPX/DXY 24h 反应
  ⑥ 上次推送时间 + 推送通道状态

数据源优先级:
  - macro_calendar 表(FF + jin10 已同步,包含 FOMC/CPI/NFP/Powell讲话等)
  - fed_rate_expectation 当前路径(2Y - EFFR)
  - fed_rate_history 历史时序
  - external_indicators VIX(自适应阈值)
  - kline_bars BTC/SPX/DXY(相关性回测)

设计原则:
  - 只读 — 不写主表 / 不改 ai_worker 主路径
  - 失败优雅降级 — 任一子查询失败返回 null + error_msg,不影响其它
  - 60s 缓存(与 fed_rate_expectation 一致)
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Any, Dict, List, Optional

try:
    from . import db, config
except ImportError:
    import db  # type: ignore
    import config  # type: ignore


logger = logging.getLogger("fed_watch")

_CACHE: Dict[str, Any] = {}
_TTL = 60.0


# ─── 子查询工具 ──────────────────────────────────────────────────────────────


def _conn():
    """返回带 row_factory 的连接(每个查询独立,避免线程冲突)。"""
    c = sqlite3.connect(db.get_db_path())
    c.row_factory = sqlite3.Row
    return c


def _safe(fn, default=None):
    """子查询包裹器:失败返回 default,日志记 warning,不抛出。"""
    try:
        return fn()
    except Exception as exc:
        logger.warning("[FED-WATCH] %s failed: %s: %s",
                       fn.__name__, type(exc).__name__, exc)
        return default


def _pearson(xs: List[float], ys: List[float]) -> float:
    """Pearson 相关系数(0/0 输入返回 0)。"""
    n = len(xs)
    if n != len(ys) or n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx2 = sum((x - mx) ** 2 for x in xs)
    dy2 = sum((y - my) ** 2 for y in ys)
    if dx2 <= 0 or dy2 <= 0:
        return 0.0
    return num / math.sqrt(dx2 * dy2)


# ─── 数据采集 ────────────────────────────────────────────────────────────────


def _fetch_current_path() -> Dict[str, Any]:
    """当前 Fed 路径(从 fed_rate_expectation 拉)。失败返回 ok=False。"""
    try:
        import fed_rate_expectation as _fre
        snap = _fre.fetch_expectation()
        return {
            "ok": snap.get("ok", False),
            "ts": snap.get("ts"),
            "data_source": snap.get("data_source"),
            "current": snap.get("current"),
            "expectation": snap.get("expectation"),
            "anomaly": snap.get("anomaly"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _fetch_fed_events_7d() -> List[Dict[str, Any]]:
    """未来 7d Fed 相关事件(FOMC + Fed 官员讲话 + CPI/NFP + PCE 等)。

    关键词筛选:title 含 FOMC/Fed/美联储/Powell/鲍尔/CPI/NFP/PCE/通胀/非农/
              失业率/利率决议/议息会议/工資/inflation/payroll/unemployment。
    """
    keywords = (
        "FOMC", "Fed ", "Fed-", "Fed.", "美联储", "鲍尔", "鲍威", "Powell",
        "CPI", "PCE", "NFP", "非农", "失业率", "Unemployment", "Payroll",
        "议息会议", "利率决议", "通胀", "Inflation", "利率", "Rate Decision",
        "FOMC Minutes", "FOMC Press Conference", "FOMC Statement",
        "Fed Chair", "Fed Official",
    )
    conn = _conn()
    try:
        now = int(time.time())
        hi = now + 7 * 86400
        rows = conn.execute(
            """
            SELECT source, country, title, event_time, impact, star,
                   forecast, previous, actual
              FROM macro_calendar
             WHERE event_time BETWEEN ? AND ?
               AND country = 'USD'
               AND impact IN ('med', 'high')
             ORDER BY event_time ASC
             LIMIT 30
            """,
            (now, hi),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            title = (r["title"] or "").strip()
            if not any(k in title for k in keywords):
                continue
            out.append({
                "source": r["source"],
                "title": title,
                "event_time": int(r["event_time"]),
                "impact": r["impact"],
                "star": int(r["star"] or 0),
                "forecast": r["forecast"] or "",
                "previous": r["previous"] or "",
                "actual": r["actual"] or "",
                "minutes_to": int((int(r["event_time"]) - now) / 60),
            })
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fetch_next_fomc() -> Optional[Dict[str, Any]]:
    """从 macro_calendar 找最近的 FOMC 利率决议/会议。"""
    conn = _conn()
    try:
        now = int(time.time())
        row = conn.execute(
            """
            SELECT source, title, event_time, impact, star, forecast, previous
              FROM macro_calendar
             WHERE event_time > ?
               AND country = 'USD'
               AND (title LIKE '%FOMC%' OR title LIKE '%Fed Fund%'
                    OR title LIKE '%利率决议%' OR title LIKE '%议息会议%')
             ORDER BY event_time ASC LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        return {
            "source": row["source"],
            "title": row["title"],
            "event_time": int(row["event_time"]),
            "impact": row["impact"],
            "star": int(row["star"] or 0),
            "minutes_to": int((int(row["event_time"]) - now) / 60),
            "forecast": row["forecast"] or "",
            "previous": row["previous"] or "",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fetch_vix_level() -> Optional[float]:
    """拉 VIX 实时值(用于自适应阈值)。"""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT value FROM external_indicators
             WHERE indicator = 'VIX' ORDER BY ts DESC LIMIT 1
            """
        ).fetchone()
        return float(row["value"]) if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _adaptive_thresholds(vix: Optional[float]) -> Dict[str, Any]:
    """VIX 自适应 Fed 异动阈值。

    经验映射:
      VIX < 15  (低波动)   → 2Y 移动 8bp,预期 12bp(更敏感,捕捉小异动)
      VIX 15-25 (常态)     → 2Y 移动 10bp,预期 15bp(默认)
      VIX 25-35 (高波动)   → 2Y 移动 15bp,预期 22bp(降噪)
      VIX >= 35 (恐慌)     → 2Y 移动 25bp,预期 35bp(只看显著事件)
    """
    base_yield = float(config.FED_RATE_YIELD_MOVE_BP)
    base_expect = float(config.FED_RATE_EXPECT_MOVE_BP)
    if vix is None:
        return {
            "regime": "unknown",
            "vix": None,
            "yield_move_bp": base_yield,
            "expect_move_bp": base_expect,
            "note": "VIX 缺失,使用默认阈值",
        }
    if vix < 15:
        regime, ym, em = "low_vol", 8.0, 12.0
    elif vix < 25:
        regime, ym, em = "normal", base_yield, base_expect
    elif vix < 35:
        regime, ym, em = "high_vol", max(base_yield, 15.0), max(base_expect, 22.0)
    else:
        regime, ym, em = "panic", max(base_yield, 25.0), max(base_expect, 35.0)
    return {
        "regime": regime,
        "vix": round(vix, 2),
        "yield_move_bp": ym,
        "expect_move_bp": em,
        "note": f"VIX={vix:.1f} → {regime} 区间,2Y 阈值 {ym:.0f}bp / 预期 {em:.0f}bp",
    }


def _fetch_fed_history_24h() -> Dict[str, Any]:
    """从 fed_rate_history 取 24h 序列(用于折线图)。"""
    conn = _conn()
    try:
        cutoff = int(time.time()) - 86400
        rows = conn.execute(
            """
            SELECT ts, dgs2_pct, effr_pct, spread_bp
              FROM fed_rate_history
             WHERE ts >= ?
             ORDER BY ts ASC
            """,
            (cutoff,),
        ).fetchall()
        return {
            "ts": [int(r["ts"]) for r in rows],
            "dgs2_pct": [round(float(r["dgs2_pct"] or 0), 3) for r in rows],
            "effr_pct": [round(float(r["effr_pct"] or 0), 3) for r in rows],
            "spread_bp": [round(float(r["spread_bp"] or 0), 2) for r in rows],
            "samples": len(rows),
        }
    except Exception:
        return {"ts": [], "dgs2_pct": [], "effr_pct": [], "spread_bp": [], "samples": 0}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fetch_yield_curve() -> Dict[str, Any]:
    """从 treasury_yield_feed 拉 13W/5Y/10Y/30Y 当前 yield。"""
    try:
        import treasury_yield_feed as _tyf
        out = _tyf.fetch_yields()
        return {
            "ok": True,
            "ts": out.get("ts"),
            "yields": [
                {
                    "name": y.get("name"),
                    "yield_pct": y.get("yield_pct"),
                    "previous_close": y.get("previous_close"),
                    "change_pct": y.get("change_pct"),
                }
                for y in out.get("yields", [])
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _fetch_correlation_history() -> Dict[str, Any]:
    """回测 2Y yield 移动 vs BTC/SPX/DXY/GLD 24h 反应(Pearson)。

    数据源:
      - fed_rate_history: 90 天 yield 移动样本
      - kline_bars: BTC-USD, SPX, DXY, GLD 24h 收益率

    算法:
      1. 90 天 fed_rate_history 日度采样
      2. 每个采样日算 yield_spread_bp 当日变化 vs 资产 24h 收益率
      3. Pearson 相关系数 + 样本数
      4. 样本 < 10 → 返回 not_enough_data
    """
    assets = ("BTC-USD", "SPX", "DXY", "GLD")
    result: Dict[str, Any] = {
        "ok": False,
        "samples": 0,
        "correlations": {a: {"pearson": None, "samples": 0} for a in assets},
        "note": "",
    }
    try:
        conn = _conn()
        try:
            yield_rows = conn.execute(
                """
                SELECT date(ts, 'unixepoch') AS day,
                       AVG(spread_bp) AS spread_bp_avg
                  FROM fed_rate_history
                 WHERE ts >= ?
                 GROUP BY day
                 ORDER BY day ASC
                """,
                (int(time.time()) - 90 * 86400,),
            ).fetchall()
            if len(yield_rows) < 10:
                result["note"] = "yield 历史样本不足(<10 天)"
                return result
            yield_days = {r["day"]: float(r["spread_bp_avg"] or 0) for r in yield_rows}
            yield_change_by_day = {}
            days_sorted = sorted(yield_days.keys())
            for i in range(1, len(days_sorted)):
                prev = days_sorted[i - 1]
                curr = days_sorted[i]
                yield_change_by_day[curr] = yield_days[curr] - yield_days[prev]
            result["samples"] = len(days_sorted) - 1
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for asset in assets:
            try:
                conn2 = _conn()
                try:
                    rows = conn2.execute(
                        """
                        SELECT date(ts, 'unixepoch') AS day, close
                          FROM kline_bars
                         WHERE symbol = ? AND timeframe = '1d'
                           AND ts >= ?
                         ORDER BY ts ASC
                        """,
                        (asset, int(time.time()) - 95 * 86400),
                    ).fetchall()
                    if len(rows) < 10:
                        result["correlations"][asset] = {
                            "pearson": None,
                            "samples": 0,
                            "note": "K 线不足",
                        }
                        continue
                    day_close = {}
                    for r in rows:
                        day_close[r["day"]] = float(r["close"] or 0)
                    days_c = sorted(day_close.keys())
                    asset_returns = []
                    paired_yield = []
                    for i in range(1, len(days_c)):
                        prev = days_c[i - 1]
                        curr = days_c[i]
                        if day_close[prev] <= 0 or day_close[curr] <= 0:
                            continue
                        if curr not in yield_change_by_day:
                            continue
                        ret = (day_close[curr] - day_close[prev]) / day_close[prev] * 100
                        asset_returns.append(ret)
                        paired_yield.append(yield_change_by_day[curr])
                    if len(paired_yield) < 5:
                        result["correlations"][asset] = {
                            "pearson": None,
                            "samples": len(paired_yield),
                            "note": "对齐后样本不足",
                        }
                        continue
                    pearson = _pearson(paired_yield, asset_returns)
                    result["correlations"][asset] = {
                        "pearson": round(pearson, 3),
                        "samples": len(paired_yield),
                    }
                finally:
                    try:
                        conn2.close()
                    except Exception:
                        pass
            except Exception as exc:
                result["correlations"][asset] = {
                    "pearson": None,
                    "samples": 0,
                    "note": f"{type(exc).__name__}: {exc}",
                }

        result["ok"] = True
        result["note"] = (
            f"近 90 天 2Y-EFFR 价差日度变化 vs 资产 24h 收益率 Pearson 相关"
            f"（样本={result['samples']}天）"
        )
        return result
    except Exception as exc:
        result["note"] = f"{type(exc).__name__}: {exc}"
        return result


def _fetch_last_push() -> Dict[str, Any]:
    """上次 Fed 推送时间(用于状态显示)。"""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT ts, severity, anomaly_type FROM fed_notify_dedup
             ORDER BY ts DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return {"last_push_ts": None, "severity": None, "anomaly_type": None}
        return {
            "last_push_ts": int(row["ts"]),
            "severity": row["severity"],
            "anomaly_type": row["anomaly_type"],
        }
    except Exception:
        return {"last_push_ts": None, "note": "fed_notify_dedup 表未就绪"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─── 主入口 ──────────────────────────────────────────────────────────────────


def fetch_fed_watch() -> Dict[str, Any]:
    """Fed 全景监控主入口(60s 缓存)。

    Returns
    -------
    {
      "ok": bool,
      "ts": int,
      "current": { 当前 Fed 路径(2Y/EFFR/预期) },
      "next_fomc": { 下次 FOMC 信息 } | None,
      "events_7d": [ 未来 7d Fed 相关事件列表 ],
      "adaptive_threshold": { VIX + 自适应阈值 },
      "yield_curve": { 13W/5Y/10Y/30Y 当前值 },
      "history_24h": { fed_rate_history 24h 序列 },
      "correlation": { 2Y yield 移动 vs 资产相关性 },
      "last_push": { 上次推送状态 },
      "explain": { 人类可读的总结 }
    }
    """
    now = time.time()
    cached = _CACHE.get("watch")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]

    current = _safe(_fetch_current_path, default={"ok": False, "error": "fetch failed"})
    vix = _safe(_fetch_vix_level, default=None)
    adaptive = _adaptive_thresholds(vix)
    next_fomc = _safe(_fetch_next_fomc, default=None)
    events_7d = _safe(_fetch_fed_events_7d, default=[])
    yield_curve = _safe(_fetch_yield_curve, default={"ok": False})
    history = _safe(_fetch_fed_history_24h, default={"ts": [], "samples": 0})
    correlation = _safe(_fetch_correlation_history, default={"ok": False, "note": "failed"})
    last_push = _safe(_fetch_last_push, default={"last_push_ts": None})

    exp = (current or {}).get("expectation") or {}
    anomaly = (current or {}).get("anomaly") or {}
    direction = exp.get("direction", "neutral")
    direction_cn = {"hawkish": "偏加息", "dovish": "偏降息", "neutral": "中性"}.get(
        direction, direction
    )

    if anomaly.get("is_anomaly"):
        explain = (
            f"⚠️ 当前 Fed 路径 {direction_cn}（{exp.get('narrative','')}）。"
            f"24h 内出现异动(severity={anomaly.get('severity','low')})—— "
            f"已通过 Server酱/邮件/PushPlus 推送。"
        )
    elif events_7d:
        n = len(events_7d)
        first = events_7d[0]
        first_min = first.get("minutes_to", 0)
        explain = (
            f"当前 Fed 路径 {direction_cn}（{exp.get('narrative','')}）。"
            f"未来 7d 共 {n} 个 Fed 相关事件，最近的是 "
            f"{first.get('title')}({first_min // 60}h{first_min % 60}m 后)"
        )
    else:
        explain = (
            f"当前 Fed 路径 {direction_cn}（{exp.get('narrative','')}）。"
            f"近 24h 无显著异动，未来 7d 无 Fed 相关事件。"
        )

    out = {
        "ok": True,
        "ts": int(now),
        "current": current,
        "next_fomc": next_fomc,
        "events_7d": events_7d,
        "adaptive_threshold": adaptive,
        "yield_curve": yield_curve,
        "history_24h": history,
        "correlation": correlation,
        "last_push": last_push,
        "explain": explain,
    }

    _CACHE["watch"] = {"ts": now, "data": out}
    return out


# ─── CLI 调试 ────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_fed_watch(), ensure_ascii=False, indent=2, default=str))




