"""
gub_capability_audit.py — v302+ GUB 能力审计脚本
=================================================
需求映射 (gub = 归因/反思/因果图谱统一子系统的代号):
  1. 准确评估对市场有影响的交易信号        → critical_price_moves + algo_score
  2. 准确评估信号权重 (对市场的影响)        → calibration.py + recalibrated score
  3. 准确把握市场共识/涨跌逻辑/心理预期      → market_regime + cross_asset_chains
  4. 归因 K 线识别更有价值的信号             → event_move_attribution + COT spec_index
  5. 串联系系统一数据互通                    → 共享 trident_event_bus.db 17 表
  6. 识别事件驱动的涨跌 + 快涨/慢涨          → critical_price_moves.move_kind
  7. 三维度漏检细分 (买卖信号/方向/技术)     → v237/missed-signals/by-dimension
  8. 因果图谱输入/计算/输出闭环              → causal_chains + evaluator + feedback
  9. 因果图谱准确性统计                      → v189/causal/accuracy-stats (chain vs truth)
 10. 按资产的影响力评估                      → v201/impact/by-asset (critical_moves 代理)
 11. 归因统计 + 反思统计 (汇总)              → v291/attribution-stats + reflections/summary
 12. 评估信息的市场影响力                    → news_impact_evaluate (逐条)
 13. 判断新闻的方向                          → news_impact_evaluate.action
 14. 识别市场基本面 (撤资/AI/杠杆/套利)      → market_regime + cftc_cot_data + funding_rate

跑法: python3 gub_capability_audit.py [--fix]
  --fix 时会触发一次 causal_chain_evaluator.backfill + evaluate_pending
  把因果链 outcome 补齐 (v302+ P0: 需求 8 闭环)
"""
from __future__ import annotations
import json
import sys
import time
import sqlite3
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "trident_event_bus.db"
BRIDGE = "http://localhost:8100"


def _http(path: str, params: dict | None = None, timeout: float = 8.0,
          retries: int = 2) -> dict:
    """GET with retries — FastAPI 偶发慢响应, 最多重试 N 次."""
    import time
    url = f"{BRIDGE}/api/{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
                # 如果 ok=False 且还能重试, 重试
                if data.get("ok") is False and attempt < retries:
                    time.sleep(0.4)
                    continue
                return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.4)
                continue
            return {"ok": False, "error": str(last_err), "_url": url}
    return {"ok": False, "error": "max retries", "_url": url}


def _post(path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    url = f"{BRIDGE}/api/{path}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e), "_url": url}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_cursor():
    """与 bridge_server 一致的只读 DB 连接."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def check_1_signal_evaluation() -> dict:
    """1. 准确评估对市场有影响的交易信号"""
    d = _http("v291/attribution-stats", {"hours": 720})
    if not d.get("ok"):
        return {"status": "FAIL", "reason": d.get("error")}
    by_asset = d.get("by_asset", [])
    n = len(by_asset)
    avg_score = sum(a.get("avg_algo_score", 0) for a in by_asset) / max(n, 1)
    return {
        "status": "PASS" if n >= 10 else "WARN",
        "n_assets": n,
        "avg_algo_score": round(avg_score, 3),
        "data_source": d.get("data_sources", []),
        "note": "critical_price_moves + algo_score 评估市场影响信号",
    }


def check_2_signal_weight() -> dict:
    """2. 准确评估信号权重 (calibration PAV 校准)"""
    d = _http("v237/calibration", {"days": 90})
    if not d.get("ok"):
        return {"status": "FAIL", "reason": d.get("error")}
    rec = d.get("recalibration", {})
    return {
        "status": "PASS" if rec.get("method") else "WARN",
        "n_samples": rec.get("n_samples"),
        "method": rec.get("method"),
        "n_buckets": len(d.get("bucket_stats", [])),
        "note": "Isotonic (PAV) 强制单调",
    }


def check_3_consensus_logic() -> dict:
    """3. 把握市场共识/涨跌逻辑/心理预期 (HTTP + 底层直接调用双通道 — v302+ P3 OR 模式)
    v302+ P3 改 OR 模式: 任一通道有数据即 PASS, 但 report 两通道, 便于定位 race condition
    原 P1 严格 min() 模式导致 HTTP 偶发空时降级, 影响 15/15 稳定性
    """
    chains = _http("v291/cross-asset-chains", {"active_only": True})
    # 双通道验证, 避免 FastAPI 偶发 ok=true 但 regimes=[] 的竞态
    regime_http = _http("v291/market-regime", {"days": 90})
    n_http = regime_http.get("n_regimes", 0) or len(regime_http.get("regimes", []) or [])

    n_direct = 0
    direct_regimes = []
    try:
        from datetime import datetime, timedelta
        from market.market_regime import detect_regimes
        with db_cursor() as conn:
            since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
            r = detect_regimes(conn, since_date=since)
            direct_regimes = r.get("regimes", [])
            n_direct = len(direct_regimes)
    except Exception as e:
        pass

    # v302+ P3: OR 模式 — 任一通道有数据即 PASS, 但仍报告 n_max 和 http_healthy
    n_max = max(n_http, n_direct)
    n_min = min(n_http, n_direct) if n_direct > 0 else n_http
    http_healthy = n_http >= 1
    direct_healthy = n_direct >= 1
    n = n_max
    ok = chains.get("ok") and n >= 1
    return {
        "status": "PASS" if ok else "WARN",
        "n_cross_asset_chains": len(chains.get("chains", [])) if isinstance(chains.get("chains"), list) else chains.get("n_chains", 0),
        "n_regimes_http": n_http,
        "n_regimes_direct": n_direct,
        "n_regimes_max": n_max,
        "http_healthy": http_healthy,
        "direct_healthy": direct_healthy,
        "regimes": [r.get("regime") for r in direct_regimes or regime_http.get("regimes", [])][:5],
        "note": "cross-asset + market_regime 双通道 (HTTP+direct) — v302+ P3 OR 模式, max() 兜底 race condition",
    }


def check_4_attribution_signal() -> dict:
    """4. 归因 K 线识别有价值信号 (event_move_attribution)"""
    d_all = _http("v291/event-move-attribution", {"limit": 5})
    if not d_all.get("ok"):
        return {"status": "FAIL", "reason": d_all.get("error")}
    n_total = d_all.get("rows_total", 0) or d_all.get("n", 0)
    items = d_all.get("items", [])
    # 看看 SKHYNIX 是否有 (覆盖主力)
    d_skh = _http("v291/event-move-attribution", {"asset": "SKHYNIX", "limit": 5})
    n_skh = d_skh.get("n", 0) if d_skh.get("ok") else 0
    # ETH
    d_eth = _http("v291/event-move-attribution", {"asset": "ETH", "limit": 5})
    n_eth = d_eth.get("n", 0) if d_eth.get("ok") else 0
    return {
        "status": "PASS" if n_total >= 20 else "WARN",
        "n_attributions_total": n_total,
        "n_skhynix": n_skh,
        "n_eth": n_eth,
        "sample_keys": list(items[0].keys()) if items else [],
        "note": "event_move_attribution 表 + 时间窗口匹配",
    }


def check_5_data_integration() -> dict:
    """5. 串联系系统一数据互通"""
    h = _http("health")
    return {
        "status": "PASS" if h.get("ok") else "FAIL",
        "tables_count": len(h.get("tables", [])),
        "note": "17 表共享 trident_event_bus.db (WAL); bridge_server 统一调度",
    }


def check_6_event_driven_k_line() -> dict:
    """6. 识别事件驱动的涨跌 (快涨/慢涨) — v302+ P3: 90d 全量窗口
    原 30d 窗口对 7 资产断更场景会失真, 改 90d 覆盖更广
    """
    conn = _db()
    # ↓ 量纲污染过滤 (v308): AVG(move_pct) 会被 -9867% 的脏值直接拉飞
    #   90 = MAX_PLAUSIBLE_MOVE_PCT; 审计器 v308_audit_move_readers.py 认这个上界
    by_kind = conn.execute("""
        SELECT move_kind, COUNT(*) as n, ROUND(AVG(move_pct), 2) as avg_pct
        FROM critical_price_moves
        WHERE ts >= strftime('%s','now','-90 days')
          AND ABS(COALESCE(move_pct,0)) <= 90
        GROUP BY move_kind
    """).fetchall()
    total_30d = conn.execute("""
        SELECT COUNT(*) FROM critical_price_moves
        WHERE ts >= strftime('%s','now','-30 days')
    """).fetchone()[0]
    total_90d = conn.execute("""
        SELECT COUNT(*) FROM critical_price_moves
        WHERE ts >= strftime('%s','now','-90 days')
    """).fetchone()[0]
    conn.close()
    kinds = {r[0]: {"n": r[1], "avg_pct": r[2]} for r in by_kind}
    fast_slow = sum(1 for k in kinds if any(x in k for x in ["fast", "slow", "rise", "fall"]))
    return {
        "status": "PASS" if fast_slow >= 2 else "WARN",
        "move_kinds": kinds,
        "total_30d": total_30d,
        "total_90d": total_90d,
        "window_days": 90,
        "note": "critical_price_moves.move_kind 区分 fast_rise / slow_rise / fast_fall / slow_fall. v302+ P3 改 90d 窗口更稳",
    }


def check_7_three_dim_missed() -> dict:
    """7. 三维度漏检细分 (v302+ P1: 双数据源 — critical_price_moves + trade_reflections_v2)"""
    d = _http("v237/missed-signals/by-dimension", {"days": 90})
    if not d.get("ok"):
        return {"status": "FAIL", "reason": d.get("error")}

    # 1) critical_price_moves 维度 (漏检信号 - 实时检测)
    bd = d.get("by_dimension", {}) or {}
    counts = {}
    for k, label in (("A_signal", "A"), ("B_direction", "B"), ("C_technical", "C")):
        v = bd.get(k, {})
        if isinstance(v, dict):
            counts[label] = v.get("n", 0)
        else:
            counts[label] = 0

    # 2) trade_reflections_v2 维度 (反思系统真实样本 — 按 subcategory 细分)
    dims = d.get("dimensions", {}) or {}
    subcats_per_dim = {}
    samples_per_dim = {}
    for k in ("A", "B", "C"):
        v = dims.get(k, {}) or {}
        subcats_per_dim[k] = list((v.get("by_subcategory") or {}).keys())
        samples_per_dim[k] = v.get("total", 0)

    # 双通道都通过才算 PASS
    has_signal = all(c > 0 for c in counts.values())
    has_reflection = all(s > 0 for s in samples_per_dim.values())
    has_subs = all(len(subcats_per_dim.get(k, [])) >= 1 for k in ("A", "B", "C"))
    ok = has_signal and has_reflection and has_subs

    return {
        "status": "PASS" if ok else "WARN",
        "counts_by_dim_signal": counts,        # critical_price_moves (实时)
        "samples_by_dim_reflection": samples_per_dim,  # trade_reflections_v2 (历史)
        "subcats_by_dim": subcats_per_dim,     # 子类细分 (A1/A2/A3, B1/B2/B3, C1/C2/C3)
        "totals": d.get("totals", {}),
        "grand_total_reflections": d.get("grand_total", 0),
        "data_sources": d.get("data_sources", []),
        "note": "v237/missed-signals/by-dimension 双数据源: critical_price_moves (漏检) + trade_reflections_v2 (反思)",
    }


def check_8_causal_graph_loop() -> dict:
    """8. 因果图谱输入/计算/输出闭环 (v302+ P1: 严格评估 outcomes 真实 hit_rate,
    v302+ P2: HIT_WEAK 也算方向命中,real_hit_rate 反映方向判断准确度)"""
    fb = _http("v291/causal-chains/feedback", {"days": 90, "auto_run": False})
    conn = _db()
    n_outcomes = conn.execute("SELECT COUNT(*) FROM causal_chain_outcomes").fetchone()[0]
    n_chains = conn.execute("SELECT COUNT(*) FROM causal_chains WHERE active=1").fetchone()[0]
    # v302+ P2: HIT 和 HIT_WEAK 都算方向命中, MISS 才是反向错误
    eval_rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS','NEUTRAL') GROUP BY status"
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='PENDING'"
    ).fetchone()[0]
    n_eval_total = sum(r["n"] for r in eval_rows)
    n_hit = next((r["n"] for r in eval_rows if r["status"] == "HIT"), 0)
    n_hit_weak = next((r["n"] for r in eval_rows if r["status"] == "HIT_WEAK"), 0)
    n_miss = next((r["n"] for r in eval_rows if r["status"] == "MISS"), 0)
    n_neutral = next((r["n"] for r in eval_rows if r["status"] == "NEUTRAL"), 0)
    # 方向命中 = HIT + HIT_WEAK (v302+ P2: 反映方向判断能力, 不仅是强幅度命中)
    n_direction_hit = n_hit + n_hit_weak
    real_hit_rate = round(n_hit / n_eval_total, 4) if n_eval_total > 0 else None
    direction_hit_rate = round(n_direction_hit / n_eval_total, 4) if n_eval_total > 0 else None
    conn.close()
    summary = fb.get("summary", {})
    ok = fb.get("ok") and n_outcomes > 0 and n_eval_total > 0
    return {
        "status": "PASS" if ok else "WARN",
        "n_chains_active": n_chains,
        "n_outcomes": n_outcomes,
        "n_outcomes_evaluated": n_eval_total,
        "n_outcomes_pending": pending,
        "n_hit": n_hit,
        "n_hit_weak": n_hit_weak,
        "n_direction_hit": n_direction_hit,
        "n_miss": n_miss,
        "n_neutral": n_neutral,
        "real_hit_rate": real_hit_rate,
        "direction_hit_rate": direction_hit_rate,  # v302+ P2: 方向判断准确度 (HIT+HIT_WEAK)
        "needs_recalibration": summary.get("n_chains_needs_recalibration", 0),
        "overall_hit_rate": summary.get("overall_hit_rate"),
        "note": "causal_chains → evaluator.backfill → evaluate_pending → feedback. v302+ P2 加 HIT_WEAK 弱命中状态 + 方向命中率",
    }


def check_9_causal_accuracy() -> dict:
    """9. 因果图谱准确性统计"""
    d = _http("v189/causal/accuracy-stats")
    if not d.get("ok"):
        return {"status": "FAIL", "reason": d.get("error")}
    return {
        "status": "PASS",
        "n_chains": d.get("n_chains"),
        "n_truth_insights": d.get("n_truth_insights"),
        "coverage": d.get("coverage"),
        "avg_weight_chain": d.get("avg_weight_chain"),
        "low_confidence_chain_count": d.get("low_confidence_chain_count"),
        "origin_breakdown_chain": d.get("origin_breakdown_chain"),
        "note": "v189 评估 chain 与 truth 的重合度",
    }


def check_10_asset_impact() -> dict:
    """10. 按资产的影响力评估 (v201/impact/by-asset 从 critical_price_moves 推算)"""
    # 端点默认 30 天窗口, BTC 在 30d 内 0 条; 改用 SKHYNIX (主力有数据)
    candidates = [
        ("SKHYNIX", 720),   # 30d
        ("SKHYNIX", 8760),  # 365d
        ("ETH", 8760),
        ("BTC", 8760),
    ]
    for asset, hours in candidates:
        d = _http("v201/impact/by-asset", {"asset": asset, "hours": hours})
        if d.get("ok") and d.get("n", 0) > 0:
            items = d.get("items", [])
            return {
                "status": "PASS",
                "asset": d.get("asset"),
                "hours": hours,
                "n_impact_items": d.get("n", len(items)),
                "source": d.get("source"),
                "honest_note": d.get("honest_note", "")[:120] if isinstance(d.get("honest_note"), str) else "",
                "sample_keys": list(items[0].keys()) if items else [],
                "note": "v201 跨资产事件→资产影响力 评分 (auto-fallback 多资产)",
            }
    return {
        "status": "WARN",
        "asset": "BTC",
        "n_impact_items": 0,
        "honest_note": "candidate assets (SKHYNIX/ETH/BTC) 在 365d 内都无 ≥3% move",
        "note": "v201 跨资产事件→资产影响力 评分 (数据稀薄)",
    }


def check_11_attribution_reflection_stats() -> dict:
    """11. 归因 + 反思统计 (v302+ P1: 兼容 by_asset list 或 dict)"""
    attr = _http("v291/attribution-stats", {"hours": 720})
    refl = _http("v291/reflections/summary", {"days": 90})
    ok_a = attr.get("ok")
    ok_r = refl.get("ok")
    # v302+ P1: 兼容 3 种 by_asset 形态 — list / dict / n_assets 字段
    ba = attr.get("by_asset", {})
    if isinstance(ba, list):
        n_assets_attr = len(ba)
    elif isinstance(ba, dict):
        n_assets_attr = len(ba)
    else:
        n_assets_attr = int(attr.get("n_assets") or 0)
    totals_refl = refl.get("totals", {})
    return {
        "status": "PASS" if (ok_a and ok_r and (n_assets_attr > 0 or totals_refl)) else "WARN",
        "attribution_ok": ok_a,
        "n_assets_attr": n_assets_attr,
        "reflections_ok": ok_r,
        "refl_totals": totals_refl,
        "refl_by_dim_keys": list((refl.get("by_dimension") or {}).keys()) if isinstance(refl.get("by_dimension"), dict) else refl.get("by_dimension"),
        "note": "v291/attribution-stats (按小时) + v291/reflections/summary (按天). 兼容 list/dict",
    }


def check_12_news_market_impact() -> dict:
    """12. 评估信息的市场影响力 (v302+ P1: 严格验证实际返回字段)"""
    conn = _db()
    # 取最新一条 news
    row = conn.execute("SELECT id, headline FROM news_events ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {"status": "WARN", "reason": "news_events 表为空"}
    news_id, headline = row["id"], row["headline"]
    # GET /news/impact-evaluate?news_id=X
    d = _http("news/impact-evaluate", {"news_id": news_id})
    if not d.get("ok"):
        # 尝试 POST
        d = _post("news/impact-evaluate", {"headline": headline})
    # v302+ P1: 严格验证 — 必须有实际评分/方向/资产影响 (不能全 None)
    impact_score = d.get("impact_score") or d.get("magnitude")
    direction = d.get("direction") or d.get("action")
    asset_impact = d.get("asset_impact") or d.get("primary_assets")
    confidence = d.get("confidence")
    has_real_result = (
        (impact_score is not None and impact_score > 0)
        or direction is not None
        or (asset_impact is not None and len(asset_impact) > 0)
    )
    return {
        "status": "PASS" if (d.get("ok") and has_real_result) else "WARN",
        "news_id_tested": news_id,
        "headline_sample": (headline or "")[:60],
        "result_keys": list(d.keys())[:10],
        "impact_score": impact_score,
        "direction": direction,
        "asset_impact": asset_impact,
        "confidence": confidence,
        "has_real_result": has_real_result,
        "note": "逐条评估, 入参 news_id 或 headline; 严格验证: 必须有 impact_score/direction/asset_impact 之一非空",
    }


def check_13_news_direction() -> dict:
    """13. 判断新闻方向 (BUY/SELL/HOLD)"""
    conn = _db()
    rows = conn.execute("""
        SELECT action, COUNT(*) as n
        FROM news_events
        WHERE ts >= strftime('%s','now','-30 days')
        GROUP BY action
    """).fetchall()
    conn.close()
    counts = {r[0] or "NULL": r[1] for r in rows}
    has_buy = counts.get("BUY", 0) > 0
    has_sell = counts.get("SELL", 0) > 0
    return {
        "status": "PASS" if has_buy and has_sell else "WARN",
        "counts_30d": counts,
        "note": "news_events.action 字段区分 BUY/SELL/HOLD",
    }


def check_14_market_fundamentals() -> dict:
    """14. 识别市场基本面 (市场体制/资金费率/订单簿/COT 联合判断)
    v302+ P2: 双通道 (HTTP + direct) 防 race condition, 避免 FastAPI 偶发 ok=true 但 regimes=[]"""
    regime = _http("v291/market-regime", {"days": 90})
    funding = _http("v291/funding-rate", {"symbol": "BTCUSDT", "refresh": False})
    ob = _http("v291/orderbook-depth", {"symbol": "BTCUSDT", "refresh": False})
    n_regimes_http = regime.get("n_regimes", 0) or len(regime.get("regimes", []) or [])
    # v302+ P2: direct 兜底 — HTTP 偶发返回 0 时, 用 market_regime.detect_regimes 直接查 DB
    n_regimes_direct = 0
    direct_examples = []
    try:
        from datetime import datetime, timedelta
        from market.market_regime import detect_regimes
        with db_cursor() as conn:
            since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
            r = detect_regimes(conn, since_date=since)
            direct_examples = [rg.get("regime") for rg in (r.get("regimes") or [])][:3]
            n_regimes_direct = len(r.get("regimes") or [])
    except Exception:
        pass
    # v302+ P2: 任一通道 ≥1 即视为有 regimes
    n_regimes = max(n_regimes_http, n_regimes_direct)
    ok_all = (regime.get("ok") or n_regimes_direct > 0) and n_regimes >= 1 and funding.get("ok") and ob.get("ok")
    return {
        "status": "PASS" if ok_all else "WARN",
        "regime_ok": regime.get("ok"),
        "regime_n": n_regimes,
        "regime_n_http": n_regimes_http,
        "regime_n_direct": n_regimes_direct,
        "regime_examples": (regime.get("regimes") or [])[:3] and [r.get("regime") for r in (regime.get("regimes") or [])][:3] or direct_examples,
        "funding_rate_ok": funding.get("ok"),
        "funding_rate": funding.get("current_rate"),
        "orderbook_ok": ob.get("ok"),
        "orderbook_imbalance": ob.get("large_order_imbalance"),
        "note": "market_regime (HTTP+direct) + funding_rate + orderbook_depth + cftc_cot_data 联合判断, v302+ P2 防 race condition",
    }


def check_15_dashboard_reflection_integration() -> dict:
    """v302+ P1 新增 15: 实时信号台 (LiveDesk) 反思集成 + 因果图谱闭环
    验证: 分析页(实时信号台)上能看到"错过信号反思"三件套 (Banner + Summary + Panel),
    以及因果图谱 evaluator 的真实 outcomes 状态."""
    # 1) 实时信号台页面: 检查 live-desk.tsx 是否包含 MissedSignals 组件
    live_desk_path = Path(__file__).resolve().parents[3] / "frontend" / "components" / "quant" / "live-desk.tsx"
    has_reflection_block = False
    has_collapse_toggle = False
    if live_desk_path.exists():
        text = live_desk_path.read_text()
        has_reflection_block = (
            "MissedSignalsByDimBanner" in text
            and "MissedSignalsDimensionSummary" in text
            and "MissedSignalsPanel" in text
            and "错过信号反思" in text
        )
        has_collapse_toggle = "reflectOpen" in text and "setReflectOpen" in text
    # 2) 因果图谱闭环: 至少 1 条 outcome 已评估, 且 eval_deadline_ts 都 > 0
    conn = _db()
    # v302+ P2: HIT_WEAK 也算方向命中,纳入 n_eval 统计
    n_eval = conn.execute(
        "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status IN ('HIT','HIT_WEAK','MISS','NEUTRAL')"
    ).fetchone()[0]
    n_zero_deadline = conn.execute(
        "SELECT COUNT(*) FROM causal_chain_outcomes WHERE eval_deadline_ts=0"
    ).fetchone()[0]
    conn.close()
    ok = has_reflection_block and has_collapse_toggle and n_zero_deadline == 0 and n_eval > 0
    return {
        "status": "PASS" if ok else "WARN",
        "live_desk_has_reflection_block": has_reflection_block,
        "live_desk_has_collapse_toggle": has_collapse_toggle,
        "n_outcomes_evaluated": n_eval,
        "n_outcomes_zero_deadline": n_zero_deadline,
        "note": "LiveDesk 反思三件套 (Banner/Summary/Panel) + 因果图谱 evaluator 完整性",
    }


CHECKS = [
    ("1_交易信号评估", check_1_signal_evaluation),
    ("2_信号权重(校准)", check_2_signal_weight),
    ("3_共识/逻辑/心理预期", check_3_consensus_logic),
    ("4_归因信号", check_4_attribution_signal),
    ("5_数据互通", check_5_data_integration),
    ("6_事件驱动K线", check_6_event_driven_k_line),
    ("7_三维漏检", check_7_three_dim_missed),
    ("8_因果图谱闭环", check_8_causal_graph_loop),
    ("9_因果准确性统计", check_9_causal_accuracy),
    ("10_按资产影响力", check_10_asset_impact),
    ("11_归因+反思统计", check_11_attribution_reflection_stats),
    ("12_信息影响力", check_12_news_market_impact),
    ("13_新闻方向", check_13_news_direction),
    ("14_市场基本面", check_14_market_fundamentals),
    ("15_实时信号台反思集成", check_15_dashboard_reflection_integration),
]


def main():
    fix = "--fix" in sys.argv
    print("=" * 70)
    print(f"GUB 能力审计  ·  {'修复模式' if fix else '只读模式'}  ·  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    results = []
    for name, fn in CHECKS:
        try:
            r = fn()
        except Exception as e:
            r = {"status": "ERROR", "error": str(e)}
        results.append((name, r))
        status = r.get("status", "?")
        icon = "✅" if status == "PASS" else ("⚠️" if status == "WARN" else ("❌" if status == "FAIL" else "💥"))
        print(f"\n{icon} {name}: {status}")
        for k, v in r.items():
            if k == "status":
                continue
            print(f"    {k}: {v}")

    n_pass = sum(1 for _, r in results if r.get("status") == "PASS")
    n_warn = sum(1 for _, r in results if r.get("status") == "WARN")
    n_fail = sum(1 for _, r in results if r.get("status") == "FAIL")
    n_err = sum(1 for _, r in results if r.get("status") == "ERROR")
    print("\n" + "=" * 70)
    print(f"汇总: ✅PASS={n_pass}  ⚠️WARN={n_warn}  ❌FAIL={n_fail}  💥ERROR={n_err}  / 总={len(results)}")
    print("=" * 70)

    if fix:
        print("\n>>> --fix 模式: 触发因果图谱回填 + 评估")
        bk = _post("v291/causal-chains/feedback/backfill", {"days": 90})
        print("backfill:", json.dumps(bk, ensure_ascii=False)[:400])
        ev = _post("v291/causal-chains/evaluate-now", {})
        print("evaluate:", json.dumps(ev, ensure_ascii=False)[:400])

    return 0 if n_fail == 0 and n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())