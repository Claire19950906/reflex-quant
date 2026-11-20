#!/usr/bin/env python3
"""bridge_panels.py — 桥服务的"面板类"只读端点（2026-11-18）。

收的是三个常年挂在 bridge_server 里、但只做"读库 + 组装面板"的端点：
  * `abnormal_signals`            异动信号面板（带 120s 内存缓存 + 真发上游转发）
  * `v237_reflection_dimension_stats`  反思维度统计
  * `v291_causal_graph`           因果图节点/边

跟 bridge_analytics 的区别：这里有**可变状态**（`_ABN_SIG_CACHE`）和
**出网调用**（`_forward_to_api`），所以单独放一个模块，缓存所有权跟着一起搬，
不再让 bridge_server 既当装配岗又当缓存宿主。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Query

from bridge_builders import MAX_PLAUSIBLE_MOVE_PCT, db_cursor
from bridge_news import _parse_asset_list
from common import time_utils

from fastapi.responses import JSONResponse

def _api_server_route_paths():
    """本进程已挂载的 api_server 路由路径集合（懒加载，缓存）。

    8100 进程内通过 lifespan/路由挂载托管了 api_server 全套端点；catch-all 转发前
    先反查这张表，已在本进程实现的路径不再自转发（否则会回到自身 catch-all 形成
    自循环直到超时）；只有真正未挂载的路径才需要走 HTTP 兜底。
    """
    cached = getattr(_api_server_route_paths, "_cache", None)
    if cached is not None:
        return cached
    paths: set[str] = set()
    try:
        import api_server as _as
        for _r in getattr(getattr(_as, "app", None), "router", None).routes or []:
            p = getattr(_r, "path", "")
            if p:
                paths.add(p)
    except Exception:
        pass
    _api_server_route_paths._cache = paths  # type: ignore[attr-defined]
    return paths


def _forward_to_api(full_path: str, query: str = "", method: str = "GET",
                    body: bytes = b"", ctype: str = "", timeout: float = 25.0):
    """把未在 bridge 实现的路径转给同进程托管的 api_server（8100）。

    背景: bridge 只实现了一部分路由，api_server 的端点在本进程内挂载；原来的
    catch-all 一律返空壳, 造成"数据其实有、前端却显示暂无数据"的静默丢失。
    返回 (status, payload) ; 连不上/超时返回 (None, None) 走空壳兜底。
    """
    import urllib.request as _u, urllib.error as _ue
    # 8100 进程内已挂载 api_server 全套路由；未在路由表中的路径即后端未实现，
    # 直接交给空壳兜底，不再自转发到 8100 自身（否则会回到本进程 catch-all 形成
    # 自循环直到超时）。
    if f"/api/{full_path}" not in _api_server_route_paths():
        return None, None
    url = f"{UPSTREAM_API}/api/{full_path}" + (f"?{query}" if query else "")
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trident-bridge"}
    if ctype:
        hdrs["Content-Type"] = ctype
    req = _u.Request(url, data=body if method != "GET" else None, headers=hdrs, method=method)
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, None
    except _ue.HTTPError as e:
        # 404/405 说明 8000 也没这条 → 交给空壳兜底, 不要假装成功
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        print(f"[bridge] 转发 {full_path} 失败: {type(e).__name__}: {e}", flush=True)
        return None, None


def err(msg: str, code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "rows": []}, status_code=code)


COT_EXTREME_Z = 1.5


COT_MEGA_Z = 2.0


# v455: 8000 api_server 已停用；8100 进程内通过 lifespan 托管了 api_server 全套路由，
#   catch-all 未命中的路径自转发到 8100 即可命中真实实现，不再回退到已下线的 8000。
import config as _config
UPSTREAM_API = _config.BRIDGE_BASE_URL


_ABN_SIG_TTL_S = 30.0


_ABN_SIG_CACHE: dict[tuple, tuple[float, dict]] = {}


def abnormal_signals(
    signal_kind: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 20,
    asset: str | None = None,
    hours: Annotated[int, Query(ge=1, le=8760)] = 168,
):
    """
    真实数据: 从 critical_price_moves 推导 K线异动信号
    - multi_factor → 派生"多因子共振"标签 (algo_score >= 0.85)
    - 其他 signal_kind → 过滤

    v340: 8000 的 /api/abnormal-signals 早就通了（原来 500 的注释已过期）。
      这里继续用 critical_price_moves 派生，会让前端看到的和库里 abnormal_signals
      不是同一批东西（status 还叫 active/weak）。所以先转发 8000，
      转发失败（超时/未实现）再退回本地派生，不要让前端白屏。
    """
    # 前端每 30s 轮询一次，而每次转发都要占用一个到 8000 的连接、等它算完；
    # 8000 忙起来（启动刷新/LLM 归因）时这一等就是十几秒，前端就卡住。
    # 这里缓存 30s：数据本身也是按分钟级更新的，没有语义损失。
    _ck = (signal_kind, status, int(limit), asset, int(hours))
    _hit = _ABN_SIG_CACHE.get(_ck)
    if _hit and (time.time() - _hit[0]) < _ABN_SIG_TTL_S:
        return _hit[1]

    # cot_extreme 是 bridge 自己用 cftc_cot_data 派生的，api_server 没有这一类，
    # 必须留在本地；其余信号走进程内直调 query_recent_signals（v456：原先 _forward_to_api
    # 自转发回 8100 自身，形成同进程 HTTP 回环，叠加外网/线程池占用时直接超时到 25s）。
    if signal_kind != "cot_extreme":
        since_ts = int(time.time()) - int(hours) * 3600
        try:
            import abnormal_detector as _ad
            import db as _db
            _conn = _db.get_connection()
            try:
                _rows = _ad.query_recent_signals(
                    _conn, asset=(asset or None), signal_kind=(signal_kind or None),
                    since_ts=since_ts, status=(status or "open"), limit=max(1, int(limit)),
                )
            finally:
                _conn.close()
            _payload = {"count": len(_rows), "signals": _rows}
            _ABN_SIG_CACHE[_ck] = (time.time(), _payload)
            return _payload
        except Exception as _exc:
            print(f"[bridge] abnormal-signals 进程内直调失败, 本地派生兜底: {type(_exc).__name__}: {_exc}",
                  flush=True)

    now = int(time.time())
    since = now - hours * 3600
    with db_cursor() as conn:
        q = "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min, context_json " \
            "FROM critical_price_moves WHERE ts>=? " \
            "AND ABS(COALESCE(move_pct,0)) <= ? "
        params: list[Any] = [since, MAX_PLAUSIBLE_MOVE_PCT]
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


def v237_reflection_dimension_stats(
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
) -> Any:
    """A/B/C 三维度**实时统计** (直接读 trade_reflections_v2, 不经 by-dimension)。

    与 /api/v237/missed-signals/by-dimension 的区别: 那个端点的 A/B/C 是
    "漏检"语义 (从 critical_price_moves 推导), 本端点是"反思错因"语义
    (从 trade_reflections_v2 推导), 两者是不同的东西, 不要混用。
    """
    cutoff = int(time.time()) - days * 86400
    # v309 P1: created_at 是 TEXT, 历史上有两种落库格式 (T 分隔=本机本地时间 /
    # 空格分隔=UTC) → 统一用 time_utils.sql_bj_ts_expr() 归一化再比。
    # cutoff 用北京时区换算 (项目约定: 裸时间串 = 北京时间)。
    cutoff_str = time_utils.epoch_to_bj_str(cutoff)
    ts_expr = time_utils.sql_bj_ts_expr("created_at")
    with db_cursor() as conn:
        try:
            rows = conn.execute(
                "SELECT dimension, subcategory, COUNT(*) as n, "
                "       SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) as n_hit, "
                "       AVG(actual_outcome_pct) as avg_outcome, "
                "       AVG(confidence_at_time) as avg_conf "
                "FROM trade_reflections_v2 "
                f"WHERE {ts_expr} >= ? AND dimension IN ('A','B','C') "
                "GROUP BY dimension, subcategory",
                (cutoff_str,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return err(f"trade_reflections_v2 查询失败 (可能缺 v309 列, 需先 migrate): {exc}")
        # v404：区分"已结算样本"与"全部样本"。hit 只对结算行有意义 ——
        # 未结算的行 hit 恒 0，混进分母会把胜率无脑拉低。
        try:
            _r_settled = conn.execute(
                "SELECT COUNT(*) AS n FROM trade_reflections_v2 "
                f"WHERE {ts_expr} >= ? AND dimension IN ('A','B','C') AND settled = 1",
                (cutoff_str,),
            ).fetchone()
            _n_settled = int(_r_settled["n"] or 0) if _r_settled else 0
        except sqlite3.OperationalError:
            _n_settled = 0
    # v399：把「推理反思」和「基础设施反思」分开报。
    # 分类口径不新造：直接复用 v398 的 insight_pool（A/B = 交易错因 / C = 技术故障），
    # 单一真值源，避免同一个概念在两处各定义一遍。
    # 为什么要分：实测 605 条反思里 499 条是 data_feed_timeout（hit 全 0），
    # 混在一起算会让"反思命中率"永远趋 0，且看起来像"84% 的错误都是技术错误"。
    try:
        import insight_pool as _ip
        _biz_dims = tuple(_ip.BUSINESS_DIMENSIONS)
        _tech_dim = _ip.TECHNICAL_DIMENSION
    except Exception:
        _biz_dims, _tech_dim = ("A", "B"), "C"
    by_dim: dict = {"A": [], "B": [], "C": []}
    _n_reasoning = 0
    _n_infra = 0
    _hit_reasoning = 0
    _hit_infra = 0
    for r in rows:
        n = int(r["n"] or 0)
        hits = int(r["n_hit"] or 0)
        dim = str(r["dimension"] or "")
        is_reasoning = dim in _biz_dims
        if is_reasoning:
            _n_reasoning += n
            _hit_reasoning += hits
        else:
            _n_infra += n
            _hit_infra += hits
        by_dim.setdefault(dim, []).append({
            "subcategory": r["subcategory"],
            # 每条样本标明自己是「推理反思」还是「基础设施反思」，
            # 前端据此分开显示（断流 499 条不该和"看反方向"并排算命中率）
            "sample_class": "reasoning" if is_reasoning else "infra",
            "n": n,
            "hits": hits,
            "hit_rate": round(hits / n, 4) if n else 0.0,
            "avg_outcome_pct": round(float(r["avg_outcome"] or 0.0), 3),
            "avg_confidence": (
                # confidence_at_time 现在按诚实口径写 NULL（没算过不写 0），
                # AVG 会自动忽略 NULL；这里保留 None 而不是造一个 0。
                None if r["avg_conf"] is None
                else round(float(r["avg_conf"]), 3)
            ),
        })
    total = sum(int(r["n"] or 0) for r in rows)
    return {
        "ok": True,
        "days": days,
        "n_reflections": total,
        # 分开报：推理样本 N/命中率，基础设施样本 N/命中率。
        # n_reflections 仍是总数（向后兼容），但看"反思有没有用"只该看推理那一列。
        "n_reasoning": _n_reasoning,
        "n_infra": _n_infra,
        "n_settled": _n_settled,
        # ⚠ 语义澄清（v404）：hit = 1 if 该笔结算 WIN else 0（paper_reflection_worker.py）；
        # 它衡量的是"被审这笔交易赢没赢"，**不是"反思有没有帮到系统"**。
        # 所以下面两个字段其实是"被审交易的胜率"，不是反思命中率。
        "trade_won_rate_reasoning": round(_hit_reasoning / _n_reasoning, 4) if _n_reasoning else None,
        "trade_won_rate_infra": round(_hit_infra / _n_infra, 4) if _n_infra else None,
        # 反思有效性必须靠"带教训 vs 不带教训"的对照实验回答，当前无样本 → None。
        # 铁律 4：不能拿 hit 顶替，也不能用 0 冒充"已验证无效"。
        "reflection_effective_rate": None,
        # 兼容旧字段名（前端/旧调用方还在读）；值同 trade_won_rate_*，但含义已更正。
        "hit_rate_reasoning": round(_hit_reasoning / _n_reasoning, 4) if _n_reasoning else None,
        "hit_rate_infra": round(_hit_infra / _n_infra, 4) if _n_infra else None,
        "sample_class_note": (
            "推理类=A/B 交易错因；基础设施类=C 技术故障（行情/抓取/延迟）。"
            "注意：hit 记的是**交易胜负**（WIN→1），不是反思有效性；"
            "reflection_effective_rate 需对照实验，当前为 null（未验证 ≠ 无效）"
        ),
        "by_dimension": by_dim,
        "data_source": "trade_reflections_v2 (v309 P0-1 三维度列)",
        "ts": int(time.time()),
    }


def v291_causal_graph(
    asset: Annotated[str, Query()],
    hours: Annotated[int, Query(ge=1, le=8760)] = 720,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
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
    # v309 P1: 同 attribution-stats —— hours 是小时, 原来乘 86400 (天) → 大 24 倍。
    cutoff = int(time.time()) - hours * 3600
    with db_cursor() as conn:
        moves = conn.execute(
            "SELECT id, ts, move_pct, move_kind, algo_score FROM critical_price_moves "
            "WHERE asset=? AND ts >= ? AND ABS(COALESCE(move_pct,0)) <= ? "
            "ORDER BY ts DESC LIMIT ?",
            (asset, cutoff, MAX_PLAUSIBLE_MOVE_PCT, limit),
        ).fetchall()
        fed = conn.execute(
            "SELECT ts, dgs2_pct, effr_pct FROM fed_rate_history "
            "WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        # 跨 asset feed events (放宽: 包括所有 asset 的失败/慢事件, 因为 MU 没 feed_health 数据)
        # v309 P1: feed_health.ts 有 'T'/' ' 两种格式混存 → 用 UTC 归一化表达式过滤;
        # cutoff 也统一用 time_utils.utc_str (原来手写 time.gmtime + strftime)。
        feeds = conn.execute(
            "SELECT ts, asset, source, latency_ms, status FROM feed_health "
            f"WHERE {time_utils.sql_utc_ts_expr('ts')} >= ? "
            "AND (status='failed' OR latency_ms > 2000) "
            "ORDER BY ts DESC LIMIT 50",
            (time_utils.utc_str(cutoff),),
        ).fetchall()
        # 跨 asset critical_moves (联动检测)
        all_moves = conn.execute(
            "SELECT asset, ts, move_pct, move_kind FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ? ORDER BY ts ASC",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
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

