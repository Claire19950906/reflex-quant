#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v226: 美联储利率预期 — 从 2Y UST + EFFR 用第一性原理推导。

第一性原理推导（无套利定价）：

  ① 无套利：2 年期零息债券收益率（2Y yield）等于市场对接下来 24 个月每个
     月的短期利率的几何平均预期（按月复利折现回来）。

  ② 短期利率（Fed funds rate）由美联储设定，等于有效联邦基金利率 EFFR (DFF)。

  ③ 因此：2Y yield − EFFR = 市场对未来 24 个月累计 Fed 行为的市场定价。

  ④ 量化：每次 Fed 议息会议标准操作 = ±25bp；所以累计预期升降息次数
     n_steps = round((2Y - EFFR) × 4)
       · n_steps = +3 → 市场预期未来 24 月累计加息 75bp
       · n_steps = -2 → 市场预期未来 24 月累计降息 50bp

  ⑤ 异常检测：2Y yield 24h 移动 > 10bp 视为 "Fed 路径定价重估"，触发推送。

数据源优先级（v226）：
  1. FRED DGS2 + DFF（官方日度，最权威；需 FRED_API_KEY）
  2. yfinance ^FVX (5Y) + 硬编码 EFFR 5.33%（仅兜底；2025-12 区间）
  3. factor_snapshots 表内部 BTC_factor 中的 rate 列（最差兜底）
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import config
import db
import http_retry

logger = logging.getLogger("fed_rate_expectation")

_CACHE: Dict[str, Any] = {}
_TTL = 60.0  # 60s 缓存（与 treasury_yield_feed 一致）

# 标准 Fed 操作步长（bp）
_FED_STEP_BP = 25

# 兜底说明:
#   · v252.1 起**禁用硬编码兜底**。之前 _EFFR_FALLBACK_PCT=5.33% 在 yfinance 抽风时被偷偷使用,
#     导致 spread_bp=0 → direction='neutral' → narrative="Fed 按兵不动" — 这是误导(真实 0)。
#   · 现在:拿不到真实数据 → 返回 ok=False + error,前端明示"无法获取"。
#   · 仅 EFFR 在 ① FRED DFF 成功 ② yfinance ^IRX 成功 时才使用,否则 None。
_EFFR_FALLBACK_PCT = None  # 兜底值禁用 — 永远不返回 fake 数据
_DGS2_FALLBACK_PCT = None  # 同上


# ─── 数据获取 ─────────────────────────────────────────────────────────

def _fetch_dgs2_fred() -> Optional[float]:
    """从 FRED 拉 DGS2 (2Y UST yield)，无 key 返回 None。"""
    api_key = config.FRED_API_KEY
    if not api_key:
        return None
    try:
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DGS2&api_key={api_key}&file_type=json&sort_order=desc&limit=5"
        )
        session = http_retry.new_retry_session(trust_env=False)
        resp = session.get(url, timeout=6.0)
        resp.raise_for_status()
        obs = (resp.json() or {}).get("observations") or []
        for o in obs:
            v = o.get("value")
            if v and v != ".":
                return float(v)
    except Exception as exc:
        logger.warning("FRED DGS2 fetch failed: %s", exc)
    return None


def _fetch_dff_fred() -> Optional[float]:
    """从 FRED 拉 DFF (effective Fed funds rate)。"""
    api_key = config.FRED_API_KEY
    if not api_key:
        return None
    try:
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DFF&api_key={api_key}&file_type=json&sort_order=desc&limit=5"
        )
        session = http_retry.new_retry_session(trust_env=False)
        resp = session.get(url, timeout=6.0)
        resp.raise_for_status()
        obs = (resp.json() or {}).get("observations") or []
        for o in obs:
            v = o.get("value")
            if v and v != ".":
                return float(v)
    except Exception as exc:
        logger.warning("FRED DFF fetch failed: %s", exc)
    return None


def _fetch_5y_yfinance() -> Optional[Dict[str, Any]]:
    """yfinance ^FVX (5Y UST yield) 作为 2Y 代理（当 FRED 不可用时）。

    v252.1: 多 period fallback + fast_info 双路,避免 yfinance ^FVX 偶尔 "delisted" 抽风。
    """
    import yfinance as yf
    s = yf.Ticker("^FVX")
    # 路 1: 多 period history()
    for p in ("5d", "30d", "60d", "1y"):
        try:
            hist = s.history(period=p, auto_adjust=False)
            if hist is not None and len(hist) >= 2:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                return {"last": last, "previous_close": prev,
                        "ts": int(time.time()), "source": "yfinance_5y_proxy"}
        except Exception:
            continue
    # 路 2: fast_info
    try:
        info = s.fast_info
        last = getattr(info, "last_price", None)
        prev = getattr(info, "previous_close_price", None)
        if last is not None:
            return {"last": float(last), "previous_close": float(prev) if prev else float(last),
                    "ts": int(time.time()), "source": "yfinance_5y_proxy_fastinfo"}
    except Exception:
        pass
    logger.warning("yfinance ^FVX fetch failed: both history() and fast_info returned nothing")
    return None


def _fetch_effr_proxy_yfinance() -> Optional[float]:
    """yfinance ^IRX (13W T-bill) 作为 EFFR 代理 — 仅在 FRED DFF 不可用时使用。

    误差:13W T-bill 比 EFFR 高 5-25bp(因 T-bill 不含隔夜拆借风险溢价),
    但远好于硬编码 5.33% — 至少反映**当前市场对短期利率的真实定价**。
    """
    import yfinance as yf
    s = yf.Ticker("^IRX")
    for p in ("5d", "30d", "60d", "1y"):
        try:
            hist = s.history(period=p, auto_adjust=False)
            if hist is not None and len(hist) >= 1:
                return float(hist["Close"].iloc[-1])
        except Exception:
            continue
    try:
        info = s.fast_info
        last = getattr(info, "last_price", None)
        if last is not None:
            return float(last)
    except Exception:
        pass
    logger.warning("yfinance ^IRX fetch failed: both history() and fast_info returned nothing")
    return None


# ─── 第一性原理推导 ───────────────────────────────────────────────────

def derive_expectation(dgs2_pct: float, dff_pct: float) -> Dict[str, Any]:
    """从 2Y yield + EFFR 推导未来 24 月 Fed 路径预期。"""
    spread_pct = dgs2_pct - dff_pct
    spread_bp = spread_pct * 100.0
    expected_change_bp = spread_bp
    expected_steps = expected_change_bp / _FED_STEP_BP

    if expected_change_bp > 5:
        direction = "hawkish"
        narrative = (
            f"市场预期未来 24 个月 Fed 累计加息约 {abs(expected_change_bp):.0f}bp "
            f"（约 {abs(expected_steps):.1f} 次标准加息）"
        )
        # v277: 人话版 + 市场影响（给前端"邮件箱"渲染用）
        email_subject = (
            f"鹰派信号:市场押注 Fed 未来 24 月还要加息 "
            f"{abs(expected_steps):.0f} 次,共 +{abs(expected_change_bp):.0f}bp"
        )
        market_impact_simple = (
            "美联储要继续加息 → 一般会推高美元、美债收益率(债价跌),"
            "压低股票、黄金、加密币(以美元计)。要小心风险资产短期回调。"
        )
    elif expected_change_bp < -5:
        direction = "dovish"
        narrative = (
            f"市场预期未来 24 个月 Fed 累计降息约 {abs(expected_change_bp):.0f}bp "
            f"（约 {abs(expected_steps):.1f} 次标准降息）"
        )
        email_subject = (
            f"鸽派信号:市场押注 Fed 未来 24 月会降息 "
            f"{abs(expected_steps):.0f} 次,共 {abs(expected_change_bp):.0f}bp"
        )
        market_impact_simple = (
            "美联储要降息 → 一般会推低美元/美债收益率(债价涨),"
            "利好股票(尤其科技)、黄金、加密币。新兴市场也常受益。"
        )
    else:
        direction = "neutral"
        narrative = "市场预期未来 24 个月 Fed 利率基本持稳（±5bp 内）"
        email_subject = "中性信号:市场认为 Fed 未来 24 月按兵不动,横盘震荡"
        market_impact_simple = (
            "美联储按兵不动 → 行情看个股/行业自身逻辑。"
            "宏观层面波动小,关注财报、地缘、油价等微观事件。"
        )

    chain = [
        "① 无套利: 2Y UST yield = 24 个月短期利率几何平均预期（折现回来）",
        "② 短期利率 = Fed funds rate (EFFR) — 由美联储设定",
        "③ 推导: 2Y − EFFR = 市场对未来 24 月累计 Fed 行为定价",
        "④ 量化: 标准 Fed 操作步长 25bp；累计次数 = spread_bp / 25",
    ]
    return {
        "current_effr_pct": round(dff_pct, 3),
        "dgs2_pct": round(dgs2_pct, 3),
        "spread_pct": round(spread_pct, 3),
        "spread_bp": round(spread_bp, 1),
        "expected_24m_change_bp": round(expected_change_bp, 1),
        "expected_step_count": round(expected_steps, 2),
        "direction": direction,
        "narrative": narrative,
        "email_subject": email_subject,           # v277: 人话版邮件主题
        "market_impact_simple": market_impact_simple,  # v277: 人话版市场影响
        "first_principles_chain": chain,
    }


# ─── 主入口 ──────────────────────────────────────────────────────────

def fetch_expectation() -> Dict[str, Any]:
    """主入口:返回当前 Fed 预期快照 + 因果链解释。

    v252.1: 禁止硬编码兜底值返回 — 拿不到真实数据 → ok=False + error。
    数据源优先级:
      1) FRED DGS2 + DFF(官方日度)
      2) yfinance ^FVX (5Y 代理 2Y) + yfinance ^IRX (13W 代理 EFFR)
      3) 全部失效 → ok=False,error 字段描述失败原因(前端显示"接口不可用")
    """
    now = time.time()
    cached = _CACHE.get("exp")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]

    # 1) 拉数据(FRED + yfinance 三路并行)
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_dgs2 = ex.submit(_fetch_dgs2_fred)
        f_dff = ex.submit(_fetch_dff_fred)
        f_5y = ex.submit(_fetch_5y_yfinance)
        f_irx = ex.submit(_fetch_effr_proxy_yfinance)
        dgs2_fred = f_dgs2.result()
        dff_fred = f_dff.result()
        yf_5y = f_5y.result()
        irx_yf = f_irx.result()

    dgs2 = None
    effr = None
    source = "unavailable"
    source_notes: List[str] = []

    # 路 1: FRED 官方双数据
    if dgs2_fred is not None and dff_fred is not None:
        dgs2 = dgs2_fred
        effr = dff_fred
        source = "fred"
    # 路 2: FRED 单边成功 + yfinance 补另一边
    elif dgs2_fred is not None and irx_yf is not None:
        dgs2 = dgs2_fred
        effr = irx_yf
        source = "fred_dgs2_yfinance_irx_proxy"
        source_notes.append("EFFR 用 ^IRX 13W T-bill 代理(高 5-25bp)")
    elif dff_fred is not None and yf_5y and yf_5y.get("last") is not None:
        dgs2 = yf_5y["last"]
        effr = dff_fred
        source = "fred_dff_yfinance_5y_proxy"
        source_notes.append("2Y 用 ^FVX 5Y 代理(高 30-50bp)")
    # 路 3: 全 yfinance 代理
    elif yf_5y and yf_5y.get("last") is not None and irx_yf is not None:
        dgs2 = yf_5y["last"]
        effr = irx_yf
        source = "yfinance_5y_irx_proxy"
        source_notes.append("2Y 用 ^FVX 5Y 代理, EFFR 用 ^IRX 13W 代理 — 误差 ±30bp")

    # 全部失败 → 显式报错(不再用 5.33% 假装)
    if dgs2 is None or effr is None:
        err_parts = []
        if dgs2_fred is None: err_parts.append("FRED DGS2 失败")
        if dff_fred is None: err_parts.append("FRED DFF 失败")
        if not yf_5y or yf_5y.get("last") is None: err_parts.append("yfinance ^FVX 失败")
        if irx_yf is None: err_parts.append("yfinance ^IRX 失败")
        msg = "; ".join(err_parts) if err_parts else "所有外部数据源失败"
        logger.warning("fed_rate_expectation: %s", msg)
        # 错误响应 — 前端会显示"接口不可用"
        out = {
            "ok": False,
            "ts": int(now),
            "data_source": "unavailable",
            "error": f"无法获取真实利率数据({msg})。可能是 yfinance 数据源问题,请稍后重试或配置 FRED_API_KEY。",
            "current": {"dgs2_pct": None, "effr_pct": None},
            "expectation": None,
            "history_24h": {"dgs2_change_bp": 0.0, "samples": 0},
            "anomaly": {"is_anomaly": False, "severity": "low", "message": "数据源不可用"},
        }
        _CACHE["exp"] = {"ts": now, "data": out}
        return out

    expectation = derive_expectation(dgs2, effr)
    if source_notes:
        expectation["data_quality_notes"] = source_notes

    # 2) 24h 异动检测
    history = _fetch_history_24h()
    anomaly = _detect_anomaly(expectation, history)

    out = {
        "ok": True,
        "ts": int(now),
        "data_source": source,
        "current": {"dgs2_pct": dgs2, "effr_pct": effr},
        "expectation": expectation,
        "history_24h": history,
        "anomaly": anomaly,
    }

    try:
        # v189-8: 锁竞争高峰期直接丢快照(实测 engine+api 合计 198 次
        # "persist fed snapshot failed")。_persist_snapshot 每次自建连接且按 ts 幂等,
        # 重试安全;3 次指数退避最坏 ≈ 15s 上界。
        db.retry_on_busy(lambda: _persist_snapshot(out))
    except Exception as exc:
        logger.warning("persist fed snapshot failed: %s", exc)

    _CACHE["exp"] = {"ts": now, "data": out}
    return out


# ─── 历史 + 异动 ──────────────────────────────────────────────────────

# v284: 关联新闻关键词(查询 raw_news 时匹配)
# 命中 → 说明该 6h 窗口有相关报道,推送给用户看;不命中 → 诚实标注"未关联"
_FED_NEWS_CORRELATION_KEYWORDS = (
    # 中文
    "美联储", "鲍威尔", "联储", "议息", "加息", "降息", "利率决议",
    # 英文
    "Fed", "FOMC", "Powell", "rate decision", "rate hike", "rate cut",
    "interest rate", "monetary policy", "central bank", "basis points",
)


def _correlate_recent_fed_news(window_hours: int = 6, limit: int = 3) -> List[Dict[str, Any]]:
    """v284: 关联近 window_hours 小时 raw_news 表里的 Fed 相关报道。

    返回: [{"ts": int, "title": str, "source": str, "impact_1to100": int}, ...]

    设计要点:
    · best-effort — 失败返 [],不能让 Fed 推送因 SQL 失败而漏报
    · 单独 short-lived cache(5 分钟),避免每次 fetch_expectation 都扫 raw_news
    · LIMIT 3 — 用户不需要 50 条标题
    · 使用 gate_impact_1to100 排序,优先展示重要新闻
    """
    try:
        cache_key = "fed_news_corr"
        cached = _CACHE.get(cache_key)
        now = time.time()
        if cached and now - cached["ts"] < 300:  # 5 分钟缓存
            return cached["data"]

        import sqlite3
        cutoff = int(now) - window_hours * 3600
        # 拼 OR LIKE 子句
        like_clauses = " OR ".join(
            f"content LIKE ?" for _ in _FED_NEWS_CORRELATION_KEYWORDS
        )
        like_params = [f"%{kw}%" for kw in _FED_NEWS_CORRELATION_KEYWORDS]

        conn = sqlite3.connect(db.get_db_path())
        try:
            rows = conn.execute(
                f"""
                SELECT id, ts, source, content, gate_impact_1to100
                  FROM raw_news
                 WHERE ts >= ?
                   AND status != 'FAILED'
                   AND is_noise = 0
                   AND ({like_clauses})
                 ORDER BY COALESCE(gate_impact_1to100, 0) DESC, ts DESC
                 LIMIT ?
                """,
                [cutoff, *like_params, limit],
            ).fetchall()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out = []
        for row in rows:
            # 截前 80 字符当标题(原始 content 太长)
            content = (row[3] or "").strip().replace("\n", " ")
            # 清理前缀标记:[hash:xxx] / [来源] 等
            import re
            content = re.sub(r"^\[hash:[a-f0-9]{8,}\]\s*", "", content)
            content = re.sub(r"^\[(jin\d|feishu|reuters|bloomberg|wsj|finhub)[^\]]*\]\s*",
                             "", content, flags=re.IGNORECASE)
            # 去掉尾部「[来自xxx快讯] 原文链接: ...」噪音
            content = re.sub(r"\s*\[来自[^\]]+\].*$", "", content)
            content = re.sub(r"\s*原文链接:.*$", "", content)
            if len(content) > 80:
                content = content[:80] + "..."
            out.append({
                "ts": int(row[1]),
                "title": content,
                "source": (row[2] or "unknown"),
                "impact_1to100": int(row[4] or 0),
            })
        _CACHE[cache_key] = {"ts": now, "data": out}
        return out
    except Exception as exc:
        logger.debug("[FED-EXP] correlate_recent_fed_news failed: %s: %s",
                     type(exc).__name__, exc)
        return []


def _fetch_history_24h() -> Dict[str, Any]:
    """拉过去 24h 内 fed_rate_history 表的快照，计算 dgs2_change_bp。"""
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
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
        conn.close()
    except Exception as exc:
        logger.warning("fed history query failed: %s", exc)
        return {"dgs2_change_bp": 0.0, "samples": 0}

    if len(rows) < 2:
        return {"dgs2_change_bp": 0.0, "samples": len(rows)}

    first = float(rows[0]["dgs2_pct"] or 0)
    last = float(rows[-1]["dgs2_pct"] or 0)
    change_bp = (last - first) * 100.0

    return {
        "dgs2_change_bp": round(change_bp, 2),
        "samples": len(rows),
        "first_ts": int(rows[0]["ts"]),
        "last_ts": int(rows[-1]["ts"]),
    }


def _resolve_thresholds() -> Dict[str, float]:
    """v276: 根据 VIX 自适应阈值(若开启 + VIX 有数据)。

    返回: { "yield_bp": float, "expect_bp": float, "regime": str, "vix": float|None }
    """
    base_yield = float(config.FED_RATE_YIELD_MOVE_BP)
    base_expect = float(config.FED_RATE_EXPECT_MOVE_BP)
    out = {
        "yield_bp": base_yield,
        "expect_bp": base_expect,
        "regime": "static",
        "vix": None,
    }
    if not getattr(config, "FED_ADAPTIVE_THRESHOLD_ENABLED", True):
        return out
    try:
        # v276: VIX 自适应 — 高波动降噪 / 低波动更敏感
        from fed_watch import _adaptive_thresholds as _at
        vix = None
        try:
            conn = sqlite3.connect(db.get_db_path())
            try:
                row = conn.execute(
                    "SELECT value FROM external_indicators "
                    "WHERE indicator='VIX' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row and row[0] is not None:
                    vix = float(row[0])
            finally:
                try: conn.close()
                except Exception: pass
        except Exception:
            pass
        if vix is None:
            return out  # VIX 缺失 → 走 static 默认
        result = _at(vix)
        out["yield_bp"] = result["yield_move_bp"]
        out["expect_bp"] = result["expect_move_bp"]
        out["regime"] = result["regime"]
        out["vix"] = vix
    except Exception as exc:
        logger.debug("[FED-EXP] adaptive threshold fallback: %s", exc)
    return out


def _detect_anomaly(expectation: Dict[str, Any],
                    history: Dict[str, Any]) -> Dict[str, Any]:
    """异动检测：2Y 移动 + 预期移动 双触发(v276 VIX 自适应阈值)。"""
    move_bp = history.get("dgs2_change_bp", 0.0)
    thr = _resolve_thresholds()
    yield_th = thr["yield_bp"]
    expect_th = thr["expect_bp"]

    is_yield_move = abs(move_bp) >= yield_th
    is_expect_move = abs(expectation["expected_24m_change_bp"]) >= expect_th
    is_anomaly = is_yield_move or is_expect_move

    if is_yield_move and is_expect_move:
        atype = "both"
    elif is_yield_move:
        atype = "yield_move"
    elif is_expect_move:
        atype = "expectation_shift"
    else:
        atype = "none"

    if abs(move_bp) >= yield_th * 2 or abs(expectation["expected_24m_change_bp"]) >= expect_th * 2:
        severity = "high"
    elif is_anomaly:
        severity = "med"
    else:
        severity = "low"

    direction_word = "上" if move_bp > 0 else "下"
    threshold_label = (
        f"[自适应 {thr['regime']} VIX={thr['vix']:.1f}]"
        if thr["regime"] != "static" else ""
    )
    message = ""
    if is_yield_move:
        message += (
            f"2Y UST 24h {direction_word} {abs(move_bp):.1f}bp "
            f"（阈值 {yield_th:.0f}bp{threshold_label}），"
        )
    if is_expect_move:
        message += (
            f"市场预期 24 月累计 {expectation['direction']} "
            f"{abs(expectation['expected_24m_change_bp']):.0f}bp"
        )

    return {
        "is_anomaly": is_anomaly,
        "type": atype,
        "yield_move_bp": move_bp,
        "expect_move_bp": expectation["expected_24m_change_bp"],
        "threshold_bp": yield_th,  # v276: 推送用哪个阈值
        "threshold_regime": thr["regime"],
        "threshold_vix": thr["vix"],
        "severity": severity,
        "message": message.strip("，") if message else "无显著异动",
        "trigger_reason": _build_trigger_reason(atype, move_bp, yield_th,
                                                 expectation["expected_24m_change_bp"],
                                                 expect_th, thr),  # v284: 人话原因
        "news_refs": _correlate_recent_fed_news(),  # v284: 关联新闻
    }


def _build_trigger_reason(atype: str, move_bp: float, yield_th: float,
                          expect_bp: float, expect_th: float,
                          thr: Dict[str, Any]) -> str:
    """v284: 把异常检测结果翻译成人话 — 用户想知道:
    是利率端动了 / 是预期端动了 / 双触发,各超阈值多少。

    例:
      · "2Y 利率 24h 上升 30bp(阈值 5bp),市场预期 24 月累计加息 114bp(阈值 25bp)"
      · "仅市场预期端:24 月累计加息 114bp 突破阈值 25bp"
    """
    parts = []
    regime_label = ""
    if thr.get("regime") != "static" and thr.get("vix"):
        regime_label = f"(VIX={thr['vix']:.1f},自适应 {thr['regime']} 模式)"

    if atype == "yield_move" or atype == "both":
        direction = "上升" if move_bp > 0 else "下降"
        parts.append(
            f"2Y 美债收益率 24h {direction} {abs(move_bp):.0f} 个点"
            f"（阈值 {yield_th:.0f} 个点）"
        )
    if atype == "expectation_shift" or atype == "both":
        direction_word = "加息" if expect_bp > 0 else "降息"
        parts.append(
            f"市场预期 24 月累计{direction_word} {abs(expect_bp):.0f} 个点"
            f"（阈值 {expect_th:.0f} 个点）"
        )
    if atype == "none":
        return f"无显著异动（2Y 利率 24h {move_bp:+.0f} 个点,预期 24m {expect_bp:+.0f} 个点）"

    return " + ".join(parts) + (f" {regime_label}" if regime_label else "")


# ─── 持久化 ──────────────────────────────────────────────────────────

def _persist_snapshot(out: Dict[str, Any]) -> None:
    """写一条 fed_rate_history 快照（同一分钟内幂等）。"""
    # v189-8: 原用裸 sqlite3.connect(),绕过了 get_connection() 的 WAL / busy_timeout=5s /
    # synchronous=NORMAL 配置,锁竞争高峰期必失败。改走统一连接工厂。
    conn = db.get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fed_rate_history (
              ts INTEGER PRIMARY KEY,
              dgs2_pct REAL,
              effr_pct REAL,
              spread_bp REAL,
              source TEXT
            )
            """,
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fed_ts ON fed_rate_history(ts DESC)"
        )
        ts = int(out["ts"])
        exists = conn.execute(
            "SELECT 1 FROM fed_rate_history WHERE ts = ?", (ts,)
        ).fetchone()
        if exists:
            return
        conn.execute(
            "INSERT INTO fed_rate_history(ts, dgs2_pct, effr_pct, spread_bp, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                out["current"]["dgs2_pct"],
                out["current"]["effr_pct"],
                out["expectation"]["spread_bp"],
                out["data_source"],
            ),
        )
        cutoff = ts - 7 * 86400
        conn.execute("DELETE FROM fed_rate_history WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─── 异动推送 wire-in（v226 → alerts）──────────────────────────────────
#
# 设计：内存级去重，避免短时间内重复推送；进程重启后重新评估（避免持久化脏数据）。
# 推送内容由 engine.alerts.format_fed_anomaly_message 统一格式化，
# 走 send_important_news → Server酱/邮件/PushPlus 三通道。

# 去重窗口：同一异常在 4 小时内不重复推送
_DEDUP_WINDOW_SEC = 4 * 3600

# 内存级 last_alert 状态：{(severity, anomaly_type): last_pushed_ts}
# severity: "high"|"med"|"low"; anomaly_type: "yield_move"|"expectation_shift"|"both"|"none"
_alert_state: Dict[str, float] = {}


def _state_key(anomaly: Dict[str, Any]) -> str:
    """异动状态的归一化 key：仅 severity + type 进入（避免方向波动触发重复推送）。"""
    return f"{anomaly.get('severity', 'low')}::{anomaly.get('type', 'none')}"


def _should_push(anomaly: Dict[str, Any]) -> bool:
    """判定当前异动是否需要新推送：① 非 none；② 首次出现 / severity 升级 / 超过 dedup 窗口。"""
    if not anomaly.get("is_anomaly"):
        return False
    key = _state_key(anomaly)
    last_ts = _alert_state.get(key, 0.0)
    now = time.time()
    return (now - last_ts) >= _DEDUP_WINDOW_SEC


def mark_pushed(anomaly: Dict[str, Any]) -> None:
    """记录已推送（暴露给测试 / 调用方手动重置）。"""
    _alert_state[_state_key(anomaly)] = time.time()


def push_fed_anomaly_if_new() -> Dict[str, Any]:
    """主入口：检测当前 Fed 异动，按需推送。

    Returns
    -------
    dict {
      "pushed": bool,
      "reason": "anomaly" | "dedup" | "no_anomaly",
      "anomaly": dict,
      "expectation_summary": str,
    }
    """
    result: Dict[str, Any] = {
        "pushed": False,
        "reason": "no_anomaly",
        "anomaly": {},
        "expectation_summary": "",
    }
    try:
        snap = fetch_expectation()
    except Exception as exc:
        logger.warning("fetch_expectation failed: %s", exc)
        result["reason"] = "fetch_failed"
        return result

    anomaly = snap.get("anomaly") or {}
    exp = snap.get("expectation") or {}
    result["anomaly"] = anomaly
    result["expectation_summary"] = exp.get("narrative", "")

    if not _should_push(anomaly):
        result["reason"] = "dedup" if anomaly.get("is_anomaly") else "no_anomaly"
        return result

    # 延迟导入避免循环依赖（alerts → fed_rate_expectation）
    try:
        from engine.alerts import format_fed_anomaly_message, send_important_news
    except ImportError:
        # 兼容：模块名 engine 在 src_python 是子包
        try:
            from engine import alerts as _alerts  # type: ignore
        except ImportError:
            logger.warning("engine.alerts 不可用，跳过推送")
            return result
        format_fed_anomaly_message = _alerts.format_fed_anomaly_message
        send_important_news = _alerts.send_important_news

    title = format_fed_anomaly_message(anomaly, exp, title_only=True,
                                       data_source=snap.get("data_source"))
    body = format_fed_anomaly_message(anomaly, exp, title_only=False,
                                      data_source=snap.get("data_source"))
    try:
        send_important_news(title, body)
        mark_pushed(anomaly)
        result["pushed"] = True
        result["reason"] = "anomaly"
    except Exception as exc:
        logger.warning("fed alert push failed: %s", exc)
        result["reason"] = "push_failed"

    return result


def reset_alert_state() -> None:
    """测试 / 手动重置推送状态（CLI 调试用）。"""
    _alert_state.clear()


# ─── Daemon ───────────────────────────────────────────────────────────

def daemon_loop(interval_s: int = 300) -> None:
    """周期推送守护（默认 5 分钟）。

    每次调用 push_fed_anomaly_if_new()：
      · reason=anomaly → 已推送，日志记 pushed=True
      · reason=no_anomaly → 无异常，silent
      · reason=dedup → 窗口内已推过，silent
      · reason=fetch_failed / push_failed → 日志记 warning

    间隔可由环境变量 FED_PUSH_INTERVAL_S 覆盖（最少 60s）。
    守护为阻塞循环；daemon=True 时由 lifespan 启动线程。
    """
    interval_s = max(60, int(os.environ.get("FED_PUSH_INTERVAL_S", interval_s)))
    print(f"[FED-PUSH] daemon started | interval={interval_s}s | "
          f"dedup_window={_DEDUP_WINDOW_SEC}s", flush=True)
    while True:
        try:
            r = push_fed_anomaly_if_new()
            if r.get("pushed"):
                a = r.get("anomaly", {}) or {}
                print(f"[FED-PUSH] @ {time.time():.0f} PUSHED "
                      f"direction={a.get('direction')} severity={a.get('severity')} "
                      f"change={a.get('change_bp')}bp", flush=True)
            elif r.get("reason") == "push_failed":
                print(f"[FED-PUSH] push_failed: anomaly={r.get('anomaly')}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[FED-PUSH] loop err: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_s)


# ─── CLI 调试 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    print(json.dumps(fetch_expectation(), ensure_ascii=False, indent=2))
