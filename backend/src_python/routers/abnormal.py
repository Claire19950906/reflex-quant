# -*- coding: utf-8 -*-
"""异常签名路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点 + 1 个查询助手：
  /api/abnormal-signals（默认 open 态、可按 asset/signal_kind/since_ts 过滤）
  /api/abnormal-signals/{id}/attribution（含 v317 crowd_attention 独立证据）
  /api/abnormal-signals/{id}/similar（v182.4 KB 相似事件检索）
  /api/admin/abnormal-signals/recompute-10m（全量重算）
重算那条要清 _abnormal_sig_seen / _5M_ABNORMAL_CACHE 并回调 _abnormal_signature_sync，
这些仍留在 api_server（watcher 线程在用），通过 _api() 延迟回引。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

import abnormal_detector
import db

router = APIRouter()


def _api():
    """延迟回引 api_server（模块导入期不可用，函数调用期一定已就绪）。"""
    import api_server
    return api_server


@router.get("/api/abnormal-signals")
async def get_abnormal_signals(
    asset: Optional[str] = None,
    signal_kind: Optional[str] = None,
    since_ts: Optional[int] = None,
    status: str = "open",
    limit: int = 50,
):
    """Phase 2 异常签名反查（「重要新闻判定」/ 复盘页追溯入口）。

    默认只返回 status='open' 的活跃签名，按时间倒序；
    支持 asset / signal_kind / since_ts 过滤；status='all' 可忽略状态过滤做全量回溯。
    """
    return await asyncio.to_thread(
        _abnormal_signals_sync, asset, signal_kind, since_ts, status, limit
    )


def _abnormal_signals_sync(
    asset: Optional[str],
    signal_kind: Optional[str],
    since_ts: Optional[int],
    status: str,
    limit: int,
) -> Dict[str, Any]:
    conn = db.get_connection()
    try:
        status_val = (status or "").strip()
        status_filter = None if status_val.lower() in ("all", "") else status_val
        rows = abnormal_detector.query_recent_signals(
            conn,
            asset=(asset or None),
            signal_kind=(signal_kind or None),
            since_ts=since_ts,
            status=status_filter,
            limit=max(1, min(int(limit or 50), 200)),
        )
        return {"count": len(rows), "signals": rows}
    finally:
        conn.close()


@router.get("/api/abnormal-signals/{signal_id}/attribution")
async def get_abnormal_signal_attribution(signal_id: int):
    """取单条异常签名的 AI 归因（v182.3+）。

    返回最新一条归因记录；若无归因 → {"attribution": null, "status": "pending"}。

    v317：额外返回 crowd_attention —— 这次异动**之前**有没有跨平台注意力。
    它是独立于 AI 归因的一条证据（来自真实抓取的人群注意力数据），
    有就是有，没有就是 null，不参与 AI 归因结论的改写。
    """
    def _sync() -> Dict[str, Any]:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT id, asset, signal_kind, status, ts, anchor_ts "
                "FROM abnormal_signals WHERE id=?",
                (int(signal_id),),
            ).fetchone()
            if not row:
                return {"error": "not found", "signal_id": int(signal_id)}
            attr = abnormal_detector.fetch_latest_attribution(conn, int(signal_id))
            # v317: 注意力证据（拿不到就 None，不伪造）
            crowd_ev = None
            try:
                import crowd_attention_lead as _cal
                asset = str(row[1] or "")
                anchor = row[5] if row[5] else (int(row[4] or 0) * 1000)
                if asset and asset.upper() != "SECTOR" and anchor:
                    crowd_ev = _cal.attribution_evidence(conn, asset, int(anchor))
            except Exception:
                crowd_ev = None
            return {
                "signal_id": int(signal_id),
                "asset": row[1],
                "signal_kind": row[2],
                "status": row[3],
                "attribution": attr,
                "crowd_attention": crowd_ev,
            }
        finally:
            conn.close()
    return await asyncio.to_thread(_sync)


@router.get("/api/abnormal-signals/{signal_id}/similar")
async def get_abnormal_signal_similar(
    signal_id: int,
    since: str = "30d",
    max_results: int = 5,
    min_overlap: int = 1,
):
    """v182.4 KB 相似历史事件检索（asset + direction + 因子 Jaccard）。"""
    import kb_facade as _kb
    def _sync() -> Dict[str, Any]:
        return _kb.find_similar_signals(
            int(signal_id), since=since, max_results=int(max_results),
            min_factor_overlap=int(min_overlap),
        )
    return await asyncio.to_thread(_sync)


@router.post("/api/admin/abnormal-signals/recompute-10m")
async def post_recompute_abnormal_signals_10m():
    """v170: 用户切换到 10m 时点按按钮触发 —— 全量重算 + 重写 abnormal_signals 表。

    流程：
      1. DELETE abnormal_signals WHERE signal_kind IN ('multi_factor', 'sector')
         （保留用户手动 status 切换过的）
      2. 调用 _api()._abnormal_signature_sync(timeframe='10m') → 重跑一遍 → 落库
      3. 清空 _api()._abnormal_sig_seen / _api()._5M_ABNORMAL_CACHE 强制下一轮重算
    """
    def _sync() -> Dict[str, Any]:
        deleted = 0
        try:
            with db.get_connection() as conn:
                cur = conn.execute(
                    "DELETE FROM abnormal_signals WHERE signal_kind IN ('multi_factor','sector')"
                )
                deleted = cur.rowcount or 0
                conn.commit()
        except Exception as exc:
            print(f"[RECOMP] DELETE 失败: {type(exc).__name__}: {exc}", flush=True)
        # 清缓存强制下一轮重算
        with _api()._abnormal_sig_lock:
            _api()._abnormal_sig_seen.clear()
        _api()._5M_ABNORMAL_CACHE.clear()
        result = _api()._abnormal_signature_sync(timeframe="10m")
        return {"deleted": deleted, "inserted": result.get("inserted", 0), "triggered": result.get("triggered", 0)}
    return await asyncio.to_thread(_sync)


# ─── 异动回填 / 反馈统计 / 拐点图层（2026-11-17 从 api_server 追加）──────────

@router.post("/api/abnormal/backfill")
async def post_abnormal_backfill(
    asset: str = "BTC",
    interval: str = "10m",
    lookback_bars: int = 144,
):
    """v221.5: 回填历史 10m K 线的异动 signals 到 abnormal_signals 表。

    适用场景：用户切到 10m 时（v170 行为）或新部署时，已有 ~144 根 10m K 线
    （最多 24h），但 abnormal_signals 表为空。把池中 K 线扫一遍写库，补全历史展示。
    """
    def _sync() -> Dict[str, Any]:
        interval_sec = 600 if interval == "10m" else 180
        try:
            fetch_fn = _api()._fetch_10m_candles if interval == "10m" else _api()._fetch_3m_candles
            candles = fetch_fn(asset)
        except Exception as exc:
            return {"error": f"fetch candles failed: {exc}", "asset": asset}
        if not candles:
            return {"error": "no candles available", "asset": asset}
        candles = candles[-max(1, min(int(lookback_bars), 500)):]
        try:
            ab_list = abnormal_detector.scan_abnormals(candles, limit=200, interval_sec=interval_sec)
            an_list = abnormal_detector.scan_anomalies(candles, limit=200, scan_window=80, interval_sec=interval_sec)
        except Exception as exc:
            return {"error": f"scan failed: {exc}", "asset": asset}
        inserted = 0
        with db.get_connection() as conn:
            for sig in ab_list:
                kind = f"abnormal_{sig.get('trigger_type', 'unknown')}"
                ts_ms = int(sig.get("ts", 0))
                if ts_ms <= 0:
                    continue
                cur = conn.execute(
                    "SELECT 1 FROM abnormal_signals WHERE asset=? AND signal_kind=? AND ts=? LIMIT 1",
                    (asset, kind, ts_ms),
                )
                if cur.fetchone():
                    continue
                rid = abnormal_detector.record_signal(
                    conn,
                    ts=ts_ms, asset=asset, signal_kind=kind,
                    direction=sig.get("direction", "neutral"),
                    anchor_ts=ts_ms,
                    detail={"pct_change": sig.get("pct_change"),
                            "trigger_type": sig.get("trigger_type"),
                            "clustered": sig.get("clustered", False),
                            "cluster_count": sig.get("cluster_count", 1)},
                    hint=f"v221.5 backfill {sig.get('trigger_type')}",
                )
                if rid:
                    inserted += 1
                    conn.commit()
            for sig in an_list:
                kind = f"anomaly_{sig.get('indicator', 'unknown')}"
                ts_ms = int(sig.get("ts", 0))
                if ts_ms <= 0:
                    continue
                cur = conn.execute(
                    "SELECT 1 FROM abnormal_signals WHERE asset=? AND signal_kind=? AND ts=? LIMIT 1",
                    (asset, kind, ts_ms),
                )
                if cur.fetchone():
                    continue
                rid = abnormal_detector.record_signal(
                    conn,
                    ts=ts_ms, asset=asset, signal_kind=kind,
                    direction=sig.get("direction", "neutral"),
                    anchor_ts=ts_ms,
                    detail={"indicator": sig.get("indicator"),
                            "type": sig.get("type", "")},
                    hint=f"v221.5 backfill {sig.get('indicator')}",
                )
                if rid:
                    inserted += 1
                    conn.commit()
        return {
            "asset": asset,
            "interval": interval,
            "bars_scanned": len(candles),
            "abnormal_found": len(ab_list),
            "anomaly_found": len(an_list),
            "inserted": inserted,
        }
    return await asyncio.to_thread(_sync)


@router.get("/api/abnormal/feedback-stats")
async def get_abnormal_feedback_stats(
    asset: str = "BTC",
    interval: str = "10m",
    lookback_bars: int = 144,
    forward_window: int = 5,
    big_move_pct: float = 0.6,
):
    """v221.5: 异动检测反馈统计（4 个指标）。

    计算方法：
      - 拉近 lookback_bars 根 K 线
      - 对 scan_abnormals 输出的每个 marker，验证其后 forward_window 根 K 线内
        是否出现 ≥ big_move_pct% 移动（forward_precision 分母）
      - 对近 lookback_bars 根 K 线内每个 |pct| ≥ big_move_pct% 的 bar，检查是否
        被扫描到（reverse_recall）

    4 个指标：
      1. forward_precision — 异动 markers 中「事后被验证」的比例（%）
      2. reverse_recall — 实际大幅波动 bars 中「被检测」的比例（%）
      3. missed_count — 漏检的 |pct| ≥ big_move_pct% bars 数（last lookback 内）
      4. pass_block_rate — 与 forward_precision 同义（v221.6 区分 TP/FP/FN）
    """
    def _sync() -> Dict[str, Any]:
        interval_sec = 600 if interval == "10m" else 180
        try:
            fetch_fn = _api()._fetch_10m_candles if interval == "10m" else _api()._fetch_3m_candles
            candles = fetch_fn(asset)
        except Exception as exc:
            return {"error": f"fetch candles failed: {exc}",
                    "forward_precision": 0.0, "reverse_recall": 0.0,
                    "missed_count": 0, "pass_block_rate": 0.0}
        if not candles:
            return {"error": "no candles", "forward_precision": 0.0,
                    "reverse_recall": 0.0, "missed_count": 0, "pass_block_rate": 0.0}
        candles = candles[-max(1, min(int(lookback_bars), 500)):]
        try:
            ab_list = abnormal_detector.scan_abnormals(candles, limit=200, interval_sec=interval_sec)
        except Exception:
            ab_list = []
        candle_idx: Dict[int, int] = {int(c[0]): i for i, c in enumerate(candles)}
        verified = 0
        total_markers = len(ab_list)
        for m in ab_list:
            ts = int(m.get("ts", 0))
            i = candle_idx.get(ts)
            if i is None:
                continue
            future = candles[i: i + int(forward_window)]
            if not future:
                continue
            max_pct = 0.0
            for fc in future:
                op, cl = fc[1], fc[4]
                if op and op > 0:
                    pct = abs((cl - op) / op * 100.0)
                    max_pct = max(max_pct, pct)
            if max_pct >= big_move_pct:
                verified += 1
        forward_precision = (verified / total_markers * 100.0) if total_markers else 0.0
        big_bars: List[Tuple[int, float]] = []
        for c in candles:
            op, cl = c[1], c[4]
            if op and op > 0:
                pct = abs((cl - op) / op * 100.0)
                if pct >= big_move_pct:
                    big_bars.append((int(c[0]), pct))
        detected_ts = {int(m["ts"]) for m in ab_list if m.get("ts")}
        big_detected = sum(1 for ts, _ in big_bars if ts in detected_ts)
        reverse_recall = (big_detected / len(big_bars) * 100.0) if big_bars else 0.0
        missed_count = len(big_bars) - big_detected
        return {
            "asset": asset,
            "interval": interval,
            "lookback_bars": len(candles),
            "total_markers": total_markers,
            "forward_precision": round(forward_precision, 2),
            "reverse_recall": round(reverse_recall, 2),
            "missed_count": int(missed_count),
            "pass_block_rate": round(forward_precision, 2),
        }
    return await asyncio.to_thread(_sync)


@router.get("/api/abnormal/swing-points")
async def get_swing_points(
    asset: str,
    timeframe: str = "3m",
    pre_window_hours: int = 4,
):
    """v221.3：归因页拐点图层 — swing/cum4 触发 + 事前氛围核实。

    Query params:
      asset            — 品种（BTC/ETH/SOL/XAU/WTI）
      timeframe        — 3m（默认）/ 10m
      pre_window_hours — 事前氛围窗口小时数（默认 4）

    返回：每个拐点一条（含氛围分数 + label），无数据返 []。
    """
    return await asyncio.to_thread(_api()._swing_points_sync, asset, timeframe, pre_window_hours)


@router.post("/api/admin/swing-writeback")
async def post_swing_writeback(
    asset: str,
    timeframe: str = "3m",
    pre_window_hours: int = 4,
    match_window_sec: int = 1800,
    max_per_point: int = 5,
):
    """v221.3：手动触发拐点 → ai_decisions 内部回写。

    流程：
      1. 调 _api()._swing_points_sync 取当前拐点列表（走 60s 缓存）
      2. 调 _api()._writeback_swing_to_decisions 把拐点回写到相邻决策
      3. 返回回写统计（scanned/matched/updated/errors）

    Query params:
      asset              — 品种
      timeframe          — 3m（默认）/ 10m
      pre_window_hours   — 事前氛围窗口（默认 4）
      match_window_sec   — 决策↔拐点允许偏差（默认 1800s = 30min）
      max_per_point      — 每个拐点最多写多少条决策（默认 5）
    """
    def _run() -> Dict[str, Any]:
        points = _api()._swing_points_sync(asset, timeframe, pre_window_hours)
        stats = _api()._writeback_swing_to_decisions(
            asset,
            points,
            match_window_sec=match_window_sec,
            max_per_point=max_per_point,
        )
        return {
            "points_count": len(points),
            "writeback": stats,
        }
    return await asyncio.to_thread(_run)
