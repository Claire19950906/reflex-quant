# -*- coding: utf-8 -*-
"""因果图谱（涨跌驱动图）路由（结构切片，2026-11-17）。

从 api_server.py 搬来 9 条 /review/causal-graph* 端点 + 6 个请求模型。
逻辑逐字未改，全部薄封装 causal_graph 模块。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import causal_graph
import chain_prior
import causal_injection_audit
import common.time_utils as time_utils
import config
import db

router = APIRouter()


def _api():
    """延迟回引 api_server（因果图缓存与助手仍在原文件）。"""
    import api_server
    return api_server


class CausalGraphReason(BaseModel):
    assets: Optional[List[str]] = None


class CausalGraphVerify(BaseModel):
    asset: Optional[str] = None          # None = 全部历史品种
    since_ts: Optional[str] = None       # None = 全量
    facts: Optional[List[dict]] = None   # None = 自动从 DB 抽


class CausalGraphLinkRequest(BaseModel):
    asset: Optional[str] = None   # None = 全品种


class CausalGraphSeedRequest(BaseModel):
    asset: Optional[str] = None
    min_weight: float = 0.5
    limit: int = 200


class CausalGraphEvidenceRequest(BaseModel):
    asset: str                     # 必填
    persist_to_graph: bool = True  # 是否把 verify 结果落成 supports/contradicts 边


class CausalGraphWeightsRequest(BaseModel):
    since_days: int = 7
    min_evidence: int = 50


@router.get("/api/review/causal-graph")
async def get_causal_graph(asset: Optional[str] = None, include_inactive: bool = False):
    """品种涨跌驱动因果图谱（节点 + 边），供前端可视化。"""
    return await asyncio.to_thread(causal_graph.get_graph, asset, include_inactive)


@router.get("/api/review/causal-graph/knowledge/{asset}")
async def get_asset_knowledge(asset: str):
    """某品种的核心/条件因素 + 5-why 推理链（供展示 / 回灌）。"""
    return await asyncio.to_thread(causal_graph.get_knowledge, asset)


@router.post("/api/review/causal-graph/reason")
async def run_causal_graph_reason(payload: CausalGraphReason):
    """触发 5-why 推理并并入图谱（默认全部历史品种）。"""
    return await asyncio.to_thread(causal_graph.run_reasoning, payload.assets)


@router.post("/api/review/causal-graph/verify")
async def verify_causal_graph(payload: CausalGraphVerify):
    """手动触发假设核验（事件驱动闭环）。

    入参：
      asset: 指定品种（None = 全部历史品种）
      since_ts: 只核验此时间之后的事实（None = 全量）
      facts: 自定义事实列表（None = 自动从 ai_decisions / factor_snapshots 抽）

    返回：{asset: {node_id: {old_status, new_status, ...}, ...}, summary}
    """
    def _work():
        if payload.asset:
            results = {payload.asset: causal_graph.verify_all_pending(
                payload.asset, payload.since_ts, payload.facts
            )}
        else:
            assets = causal_graph.known_assets()
            results = {a: causal_graph.verify_all_pending(
                a, payload.since_ts, payload.facts
            ) for a in assets}
        # 汇总
        summary = {
            "assets_processed": len(results),
            "total_facts": sum(r.get("fact_count", 0) for r in results.values()),
            "total_verified": sum(r.get("verified_count", 0) for r in results.values()),
            "by_status": {},
        }
        for r in results.values():
            for node in r.get("results", []):
                st = node.get("new_status") or "UNKNOWN"
                summary["by_status"][st] = summary["by_status"].get(st, 0) + 1
        return {"summary": summary, "results": results}
    return await asyncio.to_thread(_work)


@router.post("/api/review/causal-graph/link-cross-asset")
async def link_cross_asset(payload: CausalGraphLinkRequest):
    """v276+: 建跨资产神经 — 让图谱里多个 asset 节点之间有 [correlates_with] 边。

    把 XAU/XAG/WTI/DXY/US10Y/SPX/NDX/CN_STOCK/US_STOCK 都连起来(16 对硬编码 +
    causal_chains 同 cause 共现的资产对)。
    """
    return await asyncio.to_thread(causal_graph.link_cross_asset_neurons, payload.asset)


@router.post("/api/review/causal-graph/seed-from-chains")
async def seed_from_chains(payload: CausalGraphSeedRequest):
    """v276+: 把 causal_chains 表里的链条快速种进图谱 — 免 LLM, 立即让 XAU/XAG/WTI
    等新接入资产也有「驱动因素」节点。"""
    return await asyncio.to_thread(
        causal_graph.seed_from_causal_chains,
        payload.asset, payload.min_weight, payload.limit,
    )


@router.post("/api/review/causal-graph/persist-evidence")
async def persist_evidence(payload: CausalGraphEvidenceRequest):
    """v276+: 反例持久化 — 把 verify 的 supports/contradicts 落成红色/绿色边。

    防御 confirmation bias: 用户能看到 "XAU 上行" 节点有 5 条绿线 +
    2 条红线, 立即识别"不是单边信念,有反例"。
    """
    def _work():
        r = causal_graph.verify_all_pending(payload.asset)
        results = r.get("results") or []
        if not payload.persist_to_graph:
            return {"verify_summary": r.get("summary"),
                    "results": results[:20]}
        ev_r = causal_graph.persist_evidence_edges(payload.asset, results)
        return {
            "verify_summary": r.get("summary"),
            "evidence_edges": ev_r,
            "asset": payload.asset,
        }
    return await asyncio.to_thread(_work)


@router.get("/api/review/causal-graph/stats")
async def causal_graph_stats():
    """v276+: 图谱健康度统计 — 节点/边按 (kind, asset) 分组, 给前端 dashboard 用。"""
    def _work():
        conn = causal_graph.db.get_connection() if hasattr(causal_graph, "db") else None
        # 直接走 causal_graph 模块内部的 db
        import db as _db
        c = _db.get_connection()
        try:
            by_kind = dict(c.execute(
                "SELECT kind, COUNT(*) FROM asset_driver_nodes GROUP BY kind"
            ).fetchall())
            by_asset = dict(c.execute(
                "SELECT asset, COUNT(*) FROM asset_driver_nodes "
                "WHERE asset IS NOT NULL AND asset != '' "
                "GROUP BY asset ORDER BY 2 DESC"
            ).fetchall())
            edge_by_kind = dict(c.execute(
                "SELECT kind, COUNT(*) FROM asset_driver_edges GROUP BY kind"
            ).fetchall())
            cross_asset_edges = c.execute(
                "SELECT COUNT(*) FROM asset_driver_edges WHERE kind='correlates_with'"
            ).fetchone()[0]
            evidence_edges = c.execute(
                "SELECT COUNT(*) FROM asset_driver_edges "
                "WHERE kind IN ('supports','contradicts')"
            ).fetchone()[0]
            latest = c.execute(
                "SELECT MAX(updated_at) FROM asset_driver_nodes"
            ).fetchone()[0]

            # v423: 录入质量直接进 stats —— 归因覆盖率 / 因子分布 / 单边节点 / 执行层噪声
            attributable = c.execute(
                "SELECT COUNT(*) FROM asset_driver_nodes "
                "WHERE active=1 AND asset!='MACRO' AND asset!='all' AND kind NOT IN ('asset','evidence')"
            ).fetchone()[0]
            attributed = c.execute(
                "SELECT COUNT(*) FROM asset_driver_nodes "
                "WHERE active=1 AND asset!='MACRO' AND asset!='all' AND kind NOT IN ('asset','evidence') "
                "AND factor_key IS NOT NULL AND factor_key!=''"
            ).fetchone()[0]
            by_family: dict = {}
            for fk, n in c.execute(
                "SELECT o.family, COUNT(*) FROM asset_driver_nodes n "
                "JOIN factor_ontology o ON o.key=n.factor_key "
                "WHERE n.active=1 AND n.asset!='MACRO' GROUP BY o.family ORDER BY 2 DESC"
            ).fetchall():
                by_family[fk or "-"] = n
            top_factors = [
                {"key": k, "canonical": causal_graph.factor_ontology.canonical(k)
                 if hasattr(causal_graph, "factor_ontology") else k, "n": n}
                for k, n in c.execute(
                    "SELECT factor_key, COUNT(*) FROM asset_driver_nodes "
                    "WHERE active=1 AND asset!='MACRO' AND factor_key IS NOT NULL "
                    "GROUP BY factor_key ORDER BY 2 DESC LIMIT 8"
                ).fetchall()
            ]
            unmatched_samples = [
                {"asset": a, "label": (l or "")[:60]}
                for a, l in c.execute(
                    "SELECT asset, label FROM asset_driver_nodes "
                    "WHERE active=1 AND asset!='MACRO' AND kind NOT IN ('asset','evidence') "
                    "AND (factor_key IS NULL OR factor_key='') "
                    "ORDER BY id DESC LIMIT 5"
                ).fetchall()
            ]
            single_degree = c.execute(
                "SELECT COUNT(*) FROM asset_driver_nodes n WHERE n.active=1 AND ("
                "  (SELECT COUNT(*) FROM asset_driver_edges e "
                "   WHERE e.from_id=n.id OR e.to_id=n.id) = 1)"
            ).fetchone()[0]
            return {
                "ok": True,
                "node_count": sum(by_kind.values()),
                "edge_count": sum(edge_by_kind.values()),
                "by_node_kind": by_kind,
                "by_asset": by_asset,
                "by_edge_kind": edge_by_kind,
                "cross_asset_edge_count": cross_asset_edges,
                "evidence_edge_count": evidence_edges,
                "last_activity": latest,
                "known_assets": causal_graph.known_assets(),
                "ontology": {
                    "attributable_nodes": attributable,
                    "attributed_nodes": attributed,
                    "coverage": round(attributed / attributable, 4) if attributable else 0.0,
                    "by_family": by_family,
                    "top_factors": top_factors,
                    "macro_count": by_kind.get("macro", 0),
                    "instance_of_edges": edge_by_kind.get("instance_of", 0),
                    "single_degree_nodes": single_degree,
                    "unmatched_samples": unmatched_samples,
                },
            }
        finally:
            c.close()
    return await asyncio.to_thread(_work)


@router.post("/api/review/causal-graph/update-weights")
async def update_cross_weights(payload: CausalGraphWeightsRequest):
    """v276+: 基于 abnormal_attributions 动态重算跨资产神经权重。

    输入 {since_days, min_evidence} → 直接 UPDATE asset_driver_edges.weight。
    返回 {updated, by_pair} 方便前端 stats bar 显示"哪几条神经变强/变弱"。
    """
    def _work():
        return causal_graph.update_cross_asset_weights(
            since_days=payload.since_days,
            min_evidence=payload.min_evidence,
        )
    return await asyncio.to_thread(_work)


# ─── 质量统计 / 导出 / v189 准确率与扩展（2026-11-17 从 api_server 追加）────


def _veto_shadow_summary(conn) -> Optional[Dict[str, Any]]:
    """v422 影子否决刻度：如果接入 conflict_veto，会拦掉多少单、拦对没拦对。

    只读 `causal_veto_shadow`（由 scripts/shadow_veto_replay.py 定期写入）。
    表不存在 / 查询失败 → 返回 None，绝不因为这一格影响体检主结果。
    `is_correct` 列历史上混着 1/0 与 'WIN'/'LOSS'，统一按字符串归一。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN would_veto=1 THEN 1 ELSE 0 END) v"
            " FROM causal_veto_shadow"
        ).fetchone()
        if not row:
            return None
        total, vetoed = int(row["n"] or 0), int(row["v"] or 0)
        if total == 0:
            return {"total": 0, "would_veto": 0, "settled": 0,
                    "false_veto": 0, "true_veto": 0, "note": "尚无影子样本，先跑 scripts/shadow_veto_replay.py"}
        rows = conn.execute(
            "SELECT s.would_veto, d.is_correct FROM causal_veto_shadow s"
            " JOIN ai_decisions d ON d.id = s.decision_id"
            # 注意：is_correct 大量是空串（未结算），只判 IS NOT NULL 会把未结算的
            # 也当成"方向反了"，实测把 13 条已结算算成 39 条、真否决 2 条算成 5 条
            " WHERE d.is_correct IS NOT NULL AND TRIM(CAST(d.is_correct AS TEXT)) <> ''"
        ).fetchall()
        settled = len(rows)
        win = {"1", "1.0", "WIN", "TRUE", "YES", "T"}
        false_veto = sum(1 for r in rows
                         if int(r["would_veto"] or 0) == 1 and str(r["is_correct"]).strip().upper() in win)
        true_veto = sum(1 for r in rows
                        if int(r["would_veto"] or 0) == 1 and str(r["is_correct"]).strip().upper() not in win)
        return {
            "total": total, "would_veto": vetoed,
            "veto_ratio": round(vetoed / total, 4) if total else 0.0,
            "settled": settled, "false_veto": false_veto, "true_veto": true_veto,
            "note": ("影子模式：只记录不拦截。已结算否决 <10 条时不足以判断，"
                     "别据此就接真否决。"),
        }
    except Exception:
        return None


@router.get("/api/causal/quality")
async def causal_quality(days: int = 30):
    """因果图谱质量体检：方向命中率拆口径 + 样本分层 + 有效链占比。"""

    def _run():
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            since = int(time.time()) - max(1, days) * 86400
            base = conn.execute(
                "SELECT COUNT(*) n,"
                " SUM(CASE WHEN enabled=1 AND COALESCE(active,1)=1 THEN 1 ELSE 0 END) on_chain,"
                " SUM(CASE WHEN trigger_words IS NULL OR trigger_words='' THEN 1 ELSE 0 END) no_kw,"
                " SUM(CASE WHEN expected_direction='BOTH' THEN 1 ELSE 0 END) both_dir,"
                " SUM(CASE WHEN hit_count+miss_count>=12 THEN 1 ELSE 0 END) n12,"
                " SUM(CASE WHEN hit_count+miss_count BETWEEN 3 AND 11 THEN 1 ELSE 0 END) weak,"
                " ROUND(AVG(confidence),3) avg_conf"
                " FROM causal_chains"
            ).fetchone()
            dir_rows = conn.execute(
                "SELECT predicted_direction, COUNT(*) n,"
                " ROUND(AVG(direction_hit),3) hit_rate"
                " FROM causal_chain_outcomes"
                " WHERE status IN ('HIT','HIT_WEAK','MISS')"
                "   AND CAST(trigger_ts AS INTEGER) >= ?"
                " GROUP BY predicted_direction", (since,)
            ).fetchall()
            status_rows = conn.execute(
                "SELECT status, COUNT(*) n FROM causal_chain_outcomes"
                " WHERE CAST(trigger_ts AS INTEGER) >= ? GROUP BY status", (since,)
            ).fetchall()
            # v414: 覆盖度 —— 有多少品种真有链（此前 36 品种只有 6 个有，界面上看不出来）
            asset_rows = conn.execute(
                "SELECT asset, COUNT(*) n FROM causal_chains"
                " WHERE enabled=1 AND COALESCE(active,1)=1 AND asset IS NOT NULL"
                " GROUP BY asset ORDER BY n DESC"
            ).fetchall()
            covered = []
            for r in asset_rows:
                # v417: 每个品种附上"闭眼猜涨"的基准率 —— 看绝对命中率会被行情漂移骗
                covered.append({"asset": r["asset"], "chains": r["n"],
                                "base_rate_up": chain_prior._base_rate_up(conn, r["asset"])})
            # v420: 图谱盲区 —— 有新闻、但一条可用链都没有的品种。
            # 对它们 inject_block 只能输出"本品种 0 条链跑赢基准"，等于图谱是瞎的；
            # 实测 58 个品种里 44 个属于这类（NVDA 近 30 天 611 条新闻、0 条链），
            # 不显式说出来，没人会知道"图谱覆盖 4 个品种"到底意味着什么。
            _DEAD = {"", "UNKNOWN", "NONE", "NULL", "MACRO"}
            _usable_assets = {
                str(r["asset"] or "").strip().upper()
                for r in conn.execute(
                    "SELECT DISTINCT asset FROM causal_chains"
                    " WHERE enabled=1 AND COALESCE(active,1)=1"
                    "   AND COALESCE(trigger_words,'')<>''"
                ).fetchall()
            } - _DEAD
            _labels = {"NONE", "MACRO", "CRYPTO", "US_STOCK", "INDIVIDUAL_STOCK",
                       "CN_STOCK", "A_STOCK", "ALL"}
            since_ts = int(time.time()) - int(days) * 86400
            news_assets, blind = 0, []
            for r in conn.execute(
                "SELECT asset, COUNT(*) n FROM news_events WHERE ts >= ? GROUP BY asset",
                (since_ts,),
            ).fetchall():
                a = str(r["asset"] or "").strip().upper()
                if not a or a in _labels or not a.isascii():
                    continue
                news_assets += 1
                if a not in _usable_assets:
                    blind.append({"asset": a, "news": int(r["n"] or 0)})
            blind.sort(key=lambda x: -x["news"])
            # v418（录入侧 P0）: 死链口径 —— 没品种或没触发词的链，`load_active_chains`
            # 会直接跳过，永远不会被评估，但它们在 UI 的"778 条链"里占着分母。
            # 实测 308 条（UNKNOWN 305 ∪ 零词 237，重叠 234），其中 288 条由
            # causal_chain_expander 自动建出（INSERT 漏了 asset 列 + 上游写死 NONE）。
            # 源头已加闸门（_dead_reason），这里把存量数字如实上屏。
            dead_row = conn.execute(
                "SELECT COUNT(*) n FROM causal_chains"
                " WHERE COALESCE(UPPER(asset),'') IN ('','NONE','NULL','UNKNOWN','MACRO')"
                "    OR COALESCE(TRIM(trigger_words),'')=''"
            ).fetchone()
            chains_dead = int(dead_row["n"] or 0) if dead_row else 0
            # v417: 否决力改用"超额"口径。
            # 旧口径（Wilson 下界>0.5）实测恒为 0：14/20=70% 的下界只有 0.481，
            # 要过 0.5 需要 n≈35~40，按现有产出永远等不到，strong 档等于永久关闭。
            # 新口径 = 命中率比"同品种同方向基准"高 ≥10pp 且单侧二项 p≤0.05。
            strong_rows = conn.execute(
                "SELECT id, name, asset, expected_direction, hit_count, miss_count"
                " FROM causal_chains"
                " WHERE enabled=1 AND COALESCE(active,1)=1"
                "   AND UPPER(COALESCE(expected_direction,'')) IN ('UP','DOWN')"
                "   AND hit_count + miss_count >= ?",
                (chain_prior.MIN_CHAIN_SAMPLES_FOR_STRONG,),
            ).fetchall()
            strong_chains = []
            for r in strong_rows:
                h = int(r["hit_count"] or 0)
                n = h + int(r["miss_count"] or 0)
                if n <= 0:
                    continue
                p_up = chain_prior._base_rate_up(conn, r["asset"])
                if p_up is None:
                    continue
                d = str(r["expected_direction"]).upper()
                p0 = p_up if d == "UP" else 1.0 - p_up
                hit_rate = h / n
                lift = hit_rate - p0
                p_value = chain_prior._binom_p_greater(h, n, p0)
                if (lift < chain_prior.MIN_LIFT_FOR_STRONG
                        or p_value > chain_prior.MAX_P_VALUE_FOR_STRONG):
                    continue
                strong_chains.append({
                    "chain_id": r["id"], "name": (r["name"] or "")[:48],
                    "asset": r["asset"], "direction": d, "n": n,
                    "hit_rate": round(hit_rate, 4), "base_rate": round(p0, 4),
                    "lift": round(lift, 4), "p_value": round(p_value, 4),
                })
            strong_chains.sort(key=lambda x: (-x["lift"], -x["n"]))
            # v417: 达标链按"机制族"合并成**独立信号** —— XAU 有 4 条地缘族链达标，
            # 它们是同一件事的 4 种写法（合计 n=66），不是 4 倍把握。
            signals: List[Dict[str, Any]] = []
            for c in strong_chains:
                fam = causal_graph._mechanism_family(c["name"])
                found = None
                for s in signals:
                    if s["asset"] == c["asset"] and s["direction"] == c["direction"] \
                            and s["family"] == fam:
                        found = s
                        break
                if found is not None:
                    found["chains"] += 1
                    found["merged_n"] += c["n"]
                    if c["lift"] > found["best"]["lift"]:
                        found["best"] = c
                    continue
                signals.append({"asset": c["asset"], "direction": c["direction"],
                                "family": fam, "chains": 1, "merged_n": c["n"],
                                "best": c})
            veto_ready = len(strong_chains)
            veto_signals = len(signals)
            edge = causal_graph._chain_library_edge(conn)
            try:
                from market.market_classifier import TARGET_TO_MARKET as _T2M
                universe = len([a for a in _api()._T2M if a and str(a).upper() != "ALL"])
            except Exception:
                universe = None
            return {
                "ok": True,
                "days": days,
                "chains_total": base["n"] if base else 0,
                "chains_effective": base["on_chain"] if base else 0,
                "chains_no_keyword": base["no_kw"] if base else 0,
                "chains_dead": chains_dead,
                "chains_usable": max(0, (base["n"] if base else 0) - chains_dead),
                "chains_both_direction": base["both_dir"] if base else 0,
                "chains_n_ge_12": base["n12"] if base else 0,
                "chains_n_ge_12_effective": len(strong_rows),
                "chains_weak_3_11": base["weak"] if base else 0,
                "avg_confidence": base["avg_conf"] if base else None,
                # v417: 整体超额 —— 绝对命中率没有意义，必须跟基准一起看
                "library_edge": edge,
                # v421: 注入到底有没有进下单决策（decision_id 全 NULL 的替代刻度）
                "injection": causal_injection_audit.summarize(conn, days=days),
                # v422: 影子否决 —— 如果接入 conflict_veto，会不会拦下单（只记录，不干预）
                "veto_shadow": _veto_shadow_summary(conn),
                "strong_chains": strong_chains[:10],
                "strong_signals": [
                    {"asset": s["asset"], "direction": s["direction"],
                     "family": s["family"], "chains": s["chains"],
                     "merged_n": s["merged_n"],
                     "name": s["best"]["name"], "n": s["best"]["n"],
                     "hit_rate": s["best"]["hit_rate"],
                     "base_rate": s["best"]["base_rate"],
                     "lift": s["best"]["lift"], "p_value": s["best"]["p_value"]}
                    for s in sorted(signals, key=lambda x: -x["best"]["lift"])[:10]
                ],
                "coverage": {
                    "assets_with_chains": len(covered),
                    "universe": universe,
                    "top": covered[:8],
                    # v420: 盲区 —— 有新闻没链，注入侧只能"本品种 0 条"
                    "news_assets": news_assets,
                    "blind_n": len(blind),
                    "blind_top": blind[:6],
                    "veto_ready_chains": veto_ready,
                    "veto_ready_signals": veto_signals,
                    "gate": {
                        "min_samples": chain_prior.MIN_CHAIN_SAMPLES_FOR_STRONG,
                        "min_lift_pp": round(chain_prior.MIN_LIFT_FOR_STRONG * 100, 1),
                        "max_p_value": chain_prior.MAX_P_VALUE_FOR_STRONG,
                        "note": "跑赢同品种同方向基准率≥10pp 且单侧二项 p≤0.05 才算可否决",
                    },
                },
                "direction_breakdown": [
                    {"direction": r["predicted_direction"], "n": r["n"],
                     "hit_rate": r["hit_rate"],
                     "note": ("幅度口径，不是方向命中" if (r["predicted_direction"] or "").upper() == "BOTH"
                              else "方向命中")}
                    for r in dir_rows
                ],
                "outcome_status": {r["status"]: r["n"] for r in status_rows},
                "note": "方向命中率只应看 UP/DOWN 两行；BOTH 是幅度链，不能并入。",
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


@router.get("/api/causal/export")
async def causal_export(what: str = "chains", limit: int = 5000, fmt: str = "csv"):
    """导出因果图谱四张表（chains / nodes / edges / outcomes），CSV 或 JSON。

    图页此前只有"看"，没有"拿走" —— 复盘会/外部评审需要能落成文件。
    """
    import csv
    import io

    tables = {
        "chains": "SELECT id, asset, name, cause, effect, expected_direction, confidence,"
                  " weight, hit_count, miss_count, enabled, COALESCE(active,1) AS active,"
                  " origin, trigger_words, trigger_keywords, invalidation, updated_at"
                  " FROM causal_chains ORDER BY id LIMIT ?",
        "nodes": "SELECT id, asset, label, kind, is_core, confidence, active, version, updated_at"
                 " FROM asset_driver_nodes ORDER BY id LIMIT ?",
        "edges": "SELECT id, from_id, to_id, kind, weight, updated_at"
                 " FROM asset_driver_edges ORDER BY id LIMIT ?",
        "outcomes": "SELECT id, chain_id, trigger_ts, predicted_direction, predicted_mag_lo,"
                    " predicted_mag_hi, actual_pct_5d, status, direction_hit, magnitude_hit, created_at"
                    " FROM causal_chain_outcomes ORDER BY id LIMIT ?",
    }
    key = (what or "chains").lower()
    if key not in tables:
        raise HTTPException(400, f"what 只支持 {sorted(tables)}")

    def _rows():
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(tables[key], (max(1, min(int(limit), 20000)),))
            cols = [d[0] for d in cur.description]
            return cols, [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    cols, rows = await asyncio.to_thread(_rows)
    if (fmt or "csv").lower() == "json":
        return JSONResponse({"ok": True, "what": key, "count": len(rows),
                             "columns": cols, "rows": rows})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    writer.writerows(rows)
    stamp = datetime.now(config.TZ_SHANGHAI).strftime("%Y%m%d-%H%M")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="causal-{key}-{stamp}.csv"'},
    )


@router.get("/api/v189/causal/accuracy-stats")
def causal_accuracy_stats(asset: Optional[str] = None, days: int = 30):
    """因果图谱准确性统计（v189）。

    数据源（基于现有表结构，无须 ALTER）：
      - review_insights（origin='truth'/'real'/'quick_sim'，active=1）：经验/真值基线
      - causal_chains：图谱上的因果链节点（weight + origin）

    指标：
      - coverage：review_insights 中带 asset 的条目被 causal_chains 覆盖的比例（粗匹配）
      - avg_weight_truth / avg_weight_chain：平均权重（>1.0 = 高优；<0.5 = 弱）
      - low_confidence_chain_count：weight < 0.5 的链路数（置灰候选）
      - origin_breakdown：causal_chains 按 origin (manual/auto) 拆分
      - truth_origin_breakdown：review_insights 按 origin (truth/real/quick_sim) 拆分
    """
    try:
        # v309 修复: 原来 cutoff 是**北京**墙钟 ('%Y-%m-%d %H:%M:%S'), 但两张表的
        # created_at 落库格式/时区语义都不同:
        #   · review_insights.created_at → db.py DEFAULT datetime('now') = **UTC**, 空格分隔
        #   · causal_chains.created_at   → db.py DEFAULT datetime('now','localtime')
        #                                = **本机 localtime**, 空格分隔 (不是固定的北京!)
        # 直接拿北京 cutoff 去比 → UTC 那张差 8h; localtime 那张差 (本机时区-北京) h。
        # 现在两张表各自归一化, cutoff 也用各自口径生成:
        #   · review_insights (UTC)     → sql_utc_ts_expr() + time_utils.utc_str()
        #   · causal_chains   (localtime)→ sql_local_ts_expr() + time_utils.local_str()
        #     (localtime 语义无法用 BJ/UTC 归一化函数表达, 故在 time_utils 里单列一个)
        since_epoch = int(time.time()) - days * 86400
        ri_since = time_utils.utc_str(since_epoch)          # review_insights: UTC 墙钟
        cl_since = time_utils.local_str(since_epoch)        # causal_chains:   本机 localtime 墙钟
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            truth_rows = conn.execute(
                """SELECT id, asset, rule, scope, origin, source_samples, kind, counter_factual, created_at
                     FROM review_insights
                    WHERE active=1 AND manual_deactivated=0
                      AND asset IS NOT NULL AND asset != ''
                      AND rule IS NOT NULL AND rule != ''
                      AND """ + time_utils.sql_utc_ts_expr("created_at") + " >= ?" + (" AND asset=?" if asset else ""),
                (ri_since,) + ((asset.upper(),) if asset else ()),
            ).fetchall()
            chain_rows = conn.execute(
                """SELECT id, name, cause, effect, trigger_words, enabled, origin, weight,
                          mechanism, precondition, invalidation, created_at
                     FROM causal_chains
                    WHERE """ + time_utils.sql_local_ts_expr("created_at") + " >= ?" + (" AND effect=?" if asset else ""),
                (cl_since,) + ((asset.upper(),) if asset else ()),
            ).fetchall()
        finally:
            conn.close()
        n_truth = len(truth_rows)
        n_chain = len(chain_rows)
        # 低权重链路（置灰候选）
        low_conf = [c for c in chain_rows if (c["weight"] or 1.0) < 0.5]
        # coverage：truth 按 asset 匹配，rule 关键词是否在 chain cause/effect 中
        # v202 R4-fix: 把 chain effect 文本归一化为标准资产代码集合（之前直接用
        # 完整中文文本，跟 insight.asset='XAU' 永远匹配不上，导致 0% 覆盖）。
        def _extract_asset_codes(text: str) -> set:
            """中文名/别名 → 标准资产代码。"""
            t = (text or "").upper()
            codes: set = set()
            # 直接代码命中
            for code in ("XAU", "ETH", "WTI", "BTC", "SOL", "NVDA", "TSLA",
                         "GOLD", "OIL", "BITCOIN", "ETHEREUM", "原油", "黄金",
                         "加密", "石油", "比特", "以太"):
                if code in t:
                    if code in ("GOLD", "黄金", "BITCOIN"):
                        codes.add("XAU" if code != "BITCOIN" else "BTC")
                    elif code in ("OIL", "原油", "石油"):
                        codes.add("WTI")
                    elif code in ("ETHEREUM", "以太"):
                        codes.add("ETH")
                    elif code in ("加密",):
                        codes.add("ETH")
                        codes.add("BTC")
                    else:
                        codes.add(code)
            return codes
        chain_idx = []
        for c in chain_rows:
            chain_idx.append({
                # v430：函数在本文件内定义，之前写成 _api()._extract_asset_codes
                # 直接 500（迁移到 router 时漏改），这里就地调用
                "assets": _extract_asset_codes(c["effect"] or ""),
                "text": ((c["cause"] or "") + " " + (c["effect"] or "") + " " + (c["trigger_words"] or "")).lower(),
            })
        covered = 0
        for t in truth_rows:
            ta = (t["asset"] or "").upper()
            if not ta:
                covered += 1
                continue
            trule = (t["rule"] or "").lower()
            for k in chain_idx:
                # v202 R4-fix-ext: 资产归一化（中文 → 代码）+ 关键词长度门槛 4 → 3
                if ta in k["assets"] and (any(w in k["text"] for w in trule.split() if len(w) >= 3)):
                    covered += 1
                    break
        coverage = round(covered / n_truth, 3) if n_truth else 0.0
        # origin 拆分
        from collections import Counter
        chain_origin = Counter((c["origin"] or "manual") for c in chain_rows)
        truth_origin = Counter((t["origin"] or "real") for t in truth_rows)
        avg_weight_chain = round(sum(c["weight"] or 1.0 for c in chain_rows) / n_chain, 3) if n_chain else 0.0
        # 按 source_samples 维度：≥5 样本为「可信赖」，否则「待验证」
        trust_breakdown = {
            "trusted": sum(1 for t in truth_rows if (t["source_samples"] or 0) >= 5),
            "pending": sum(1 for t in truth_rows if (t["source_samples"] or 0) < 5),
        }
        return {
            "ok": True,
            "asset": (asset or "ALL").upper(),
            "days": days,
            # v309: 两张表口径不同 → 各自给出 cutoff。
            # "since" 保留旧的**北京墙钟**语义 (给前端展示用, 不参与过滤);
            # 真正参与 SQL 过滤的是下面两个各自口径的 cutoff。
            "since": time_utils.epoch_to_bj_str(since_epoch),
            "since_review_insights_utc": ri_since,
            "since_causal_chains_local": cl_since,
            "n_truth_insights": n_truth,
            "n_causal_chains": n_chain,
            "coverage": coverage,
            "avg_weight_chain": avg_weight_chain,
            "low_confidence_chain_count": len(low_conf),
            "low_confidence_sample": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "cause": c["cause"],
                    "effect": c["effect"],
                    "weight": c["weight"],
                    "origin": c["origin"],
                    "trigger_words": c["trigger_words"][:80],
                }
                for c in low_conf[:10]
            ],
            "origin_breakdown_chain": dict(chain_origin),
            "origin_breakdown_truth": dict(truth_origin),
            "trust_breakdown": trust_breakdown,
            "generated_at": datetime.now(config.TZ_SHANGHAI).isoformat(sep=" ", timespec="seconds"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v189/causal/expand-from-insights")
def causal_expand_from_insights(
    days: int = 30,
    min_samples: int = 2,
    max_new: int = 5,
    dry_run: bool = False,
    reset: bool = False,
    backup_csv: str = "/tmp/causal_chains_trigger_backup.csv",
):
    """v202 R5：从 review_insights 反推 causal_chains.trigger_words。

    - days: 看最近 N 天的 review_insights
    - min_samples: 新建 chain 所需的最小样本数（默认 2）
    - max_new: 本次最多新建 chain 数（默认 5）
    - dry_run: 只返回结果不写库
    - reset: 清空现有 trigger_words 后只写白名单关键词（原内容会先备份到 CSV）
    """
    try:
        from causal_chain_expander import expand_from_insights
        started = time.time()

        # reset 模式：先备份
        if reset and not dry_run:
            import csv as _csv
            conn = db.get_connection()
            with open(backup_csv, "w", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                w.writerow(["id", "name", "trigger_words_before_reset", "effect"])
                for row in conn.execute("SELECT id, name, trigger_words, effect FROM causal_chains WHERE enabled=1"):
                    w.writerow([row[0], row[1], row[2], row[3]])
            conn.close()

        result = expand_from_insights(
            days=days,
            min_samples_for_new_chain=min_samples,
            max_new_chains=max_new,
            dry_run=dry_run,
            reset=reset,
        )
        result["elapsed_sec"] = round(time.time() - started, 2)
        result["reset_backup_csv"] = backup_csv if reset and not dry_run else None
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
