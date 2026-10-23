#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v201 #1: 策略级反思（Strategy-level Reflection）。

设计目标
--------
v182 trade-level 反思 (`trade_reflections_v2`) 看单笔买卖；本模块看的是**策略组合表现**:
- 整体 BUY vs SELL 胜率 / 平均盈亏 / 持仓时长分布
- 不同 event_strength 下信号胜率是否合理
- 入场时机模式（<60s/300s/900s+）的胜率差
- per_asset_action 的强弱模式
- trade_reflections_v2 的 layer/sub_layer 累计分布（trade-level 已识别的系统性偏差）

输出供 admin 审批后批量应用到 strategy_versions / paper_trading_settings（同 v182 审批链路）。

实现要点
--------
- 复用 `review_agent._llm` + `_parse_json`：模型通道、代理/直连分流、熔断降级全自动继承
- 复用 `_V182_PARAM_KEYS` 白名单：禁止 LLM 自创 param_key
- 复用 `paper_trading.apply_adjustment_to_settings` + `strategy_store.add_version`：
  approve 一条 reflection = 循环其内部每条 recommendation 应用一次（同现有的 adjustments 审批）
- 数据源：`compute_buy_sell_kpi(conn, days, asset)` + `trade_reflections_v2` layer/sub_layer 分布

状态机
------
pending -> approved (循环 recommendation -> add_version / apply_settings)
        -> rejected
        -> expired (90 天未审批)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
import db
import paper_trading as _pt_mod
import review_agent
from paper_trading_kpi import compute_buy_sell_kpi
from review_agent import _V182_PARAM_KEYS, _parse_json
from strategy_store import add_version as _add_strategy_version

# 模块级常量（必须在 def 之前；_pt_mod 已 import）
_MIN_SAMPLE_SIZE = 30
_DEFAULT_PERIOD_DAYS = 30
_DAEMON_INTERVAL_DAYS = 7
_VALID_SEVERITIES = ("low", "medium", "high")
_VALID_DIRECTIONS = ("up", "down", "on", "off")
_SETTINGS_KEYS = set(_pt_mod._V182_SETTINGS_KEYS)


# ---- 数据查询 ----

def _reflection_layer_distribution(conn: sqlite3.Connection,
                                   period_start: str,
                                   period_end: str) -> Dict[str, Any]:
    """统计窗口内 trade_reflections_v2 的 layer/sub_layer 分布 + root_cause 关键词 top10。"""
    rows = conn.execute(
        """SELECT layer, sub_layer, root_cause
             FROM trade_reflections_v2
            WHERE created_at >= ? AND created_at < ?
              AND layer IS NOT NULL AND layer != ''""",
        (period_start, period_end),
    ).fetchall()
    layer_counts: Counter = Counter()
    sub_layer_counts: Counter = Counter()
    keyword_counts: Counter = Counter()
    for r in rows:
        layer = (r["layer"] or "").strip()
        sl = (r["sub_layer"] or "").strip()
        if layer:
            layer_counts[layer] += 1
        if layer and sl:
            sub_layer_counts[f"{layer}/{sl}"] += 1
        rc = (r["root_cause"] or "").strip()
        for kw in _extract_keywords(rc):
            keyword_counts[kw] += 1
    return {
        "layer_counts": dict(layer_counts.most_common()),
        "sub_layer_counts": dict(sub_layer_counts.most_common(15)),
        "root_cause_keywords_top10": dict(keyword_counts.most_common(10)),
        "total_reflections": len(rows),
    }


def _extract_keywords(text: str) -> List[str]:
    """从 root_cause 提取中文 2~3 字关键词 + 英文 4+ 词（粗粒度）。"""
    if not text:
        return []
    out: List[str] = []
    for block in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(0, len(block) - 1):
            out.append(block[i:i + 2])
        if len(block) >= 3:
            out.append(block[:3])
    for w in re.findall(r"[A-Za-z]{4,}", text):
        out.append(w.lower())
    return out[:7]


def _format_kpi_for_prompt(kpi: Dict[str, Any],
                           reflections_dist: Dict[str, Any]) -> str:
    """把 BuySellKpi + reflection 摘要压成 LLM 输入字符串。"""
    overall = kpi.get("overall") or {}
    overall_line = (
        f"总笔数 {kpi.get('total_trades', 0)} | 胜率 {overall.get('win_rate', 0):.2%} | "
        f"平均盈亏 {overall.get('avg_pnl_pct', 0):+.2%}"
    )
    paa_lines = []
    for s in (kpi.get("per_asset_action") or [])[:8]:
        paa_lines.append(
            f"  - {s.get('asset','?')}/{s.get('action','?')} "
            f"{s.get('count',0)}笔 胜率 {s.get('win_rate',0):.0%} "
            f"均盈亏 {s.get('avg_pnl_pct',0):+.2%}"
        )
    bs_lines = []
    for s in (kpi.get("by_event_strength") or []):
        bs_lines.append(
            f"  - {s.get('event_strength','?')}: {s.get('count',0)}笔 "
            f"胜率 {s.get('win_rate',0):.0%}"
        )
    bp = kpi.get("buy_pattern") or {}
    sp = kpi.get("sell_pattern") or {}
    wp = kpi.get("win_trigger_pattern") or {}
    lp = kpi.get("loss_trigger_pattern") or {}
    block = [
        f"【整体】{overall_line}",
        "【per_asset_action (Top 8)】",
        *(paa_lines or ["  (无数据)"]),
        "【by_event_strength】",
        *(bs_lines or ["  (无数据)"]),
        f"【buy_pattern】 涨后胜率 {bp.get('win_rate_after_up',0):.0%} "
        f"(n={bp.get('total_after_up',0)}) | 跌后胜率 {bp.get('win_rate_after_down',0):.0%} "
        f"(n={bp.get('total_after_down',0)})",
        f"【sell_pattern】 涨后胜率 {sp.get('win_rate_after_up',0):.0%} "
        f"(n={sp.get('total_after_up',0)}) | 跌后胜率 {sp.get('win_rate_after_down',0):.0%} "
        f"(n={sp.get('total_after_down',0)})",
        f"【win_trigger_pattern】 {wp.get('bucket_summary','(无)')}",
        f"【loss_trigger_pattern】 {lp.get('bucket_summary','(无)')}",
        f"【反思 layer 分布】 {reflections_dist.get('layer_counts',{})}",
        f"【root_cause 关键词 Top10】 "
        f"{reflections_dist.get('root_cause_keywords_top10',{})}",
    ]
    return "\n".join(block)[:2400]


# ---- 校验 ----

def _validate_recommendations(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if key not in _V182_PARAM_KEYS:
            continue
        direction = str(item.get("direction") or "")
        if direction not in _VALID_DIRECTIONS:
            continue
        try:
            magnitude = float(item.get("magnitude") or 0.05)
        except (TypeError, ValueError):
            continue
        magnitude = max(0.01, min(0.5, magnitude))
        try:
            confidence = float(item.get("confidence") or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(item.get("rationale") or "")[:300]
        out.append({
            "type": "param",
            "key": key,
            "direction": direction,
            "magnitude": round(magnitude, 4),
            "confidence": round(confidence, 3),
            "rationale": rationale,
        })
    return out


def _validate_patterns(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "medium")
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        out.append({
            "pattern": str(item.get("pattern") or "")[:200],
            "supporting_data": str(item.get("supporting_data") or "")[:200],
            "severity": severity,
        })
    if len(out) > 6:
        out = out[:6]
    if len(out) < 2:
        out.append({
            "pattern": "样本/数据不足，无法归纳系统性模式",
            "supporting_data": "建议继续积累样本",
            "severity": "low",
        })
    return out


def _validate_llm_payload(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    affected = str(raw.get("affected_dimension") or "")
    if affected not in ("asset_class", "action", "signal_layer", "model_quality", ""):
        affected = ""
    try:
        confidence = float(raw.get("overall_confidence") or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    return {
        "llm_summary": str(raw.get("llm_summary") or "")[:500],
        "strategy_patterns": _validate_patterns(raw.get("strategy_patterns")),
        "recommendations": _validate_recommendations(raw.get("recommendations")),
        "affected_dimension": affected,
        "evidence_summary": str(raw.get("evidence_summary") or "")[:300],
        "overall_confidence": round(confidence, 3),
    }


# ---- LLM 调用 ----

def _build_prompt(kpi_text: str, period_days: int, asset_filter: str) -> str:
    asset_clause = f"（仅 {asset_filter}）" if asset_filter else ""
    return (
        f"你是量化交易策略审查员，分析最近 {period_days} 天模拟盘交易数据 {asset_clause}。\n"
        "【输入】\n"
        f"{kpi_text}\n\n"
        "【输出严格 JSON】\n"
        "{\n"
        '  "llm_summary": "<=300字策略总结，指出最强模式 + 最弱模式 + 1 句行动建议>",\n'
        '  "strategy_patterns": [\n'
        '    {"pattern": "<模式描述>", "supporting_data": "<关键数字>", "severity": "low|medium|high"}\n'
        '  ],\n'
        '  "recommendations": [\n'
        '    {"type": "param", "key": "<必须从白名单选>", "direction": "up|down|on|off",\n'
        '     "magnitude": 0.05~0.5, "confidence": 0~1, "rationale": "<=100字理由>"}\n'
        '  ],\n'
        '  "affected_dimension": "asset_class|action|signal_layer|model_quality|""",\n'
        '  "evidence_summary": "<=150字证据汇总>",\n'
        '  "overall_confidence": 0~1\n'
        "}\n"
        "约束：\n"
        "- strategy_patterns 长度 2~6；severity ∈ {low, medium, high}\n"
        "- recommendations[].key 必须从白名单选（buy_min_score / sell_min_score / tp1_mult / tp2_mult / "
        "tp1_ratio / tp2_ratio / tp_enabled / gate_enabled / news_trade_max_age_seconds / "
        "market_structure_enabled / signal_threshold / notional_usdt / leverage / trailing_callback_rate / "
        "holding_horizon_minutes / min_event_strength / require_direct_catalyst / asset_filter）\n"
        "- magnitude ∈ [0.01, 0.5]；confidence ∈ [0, 1]\n"
        "- 只输出 JSON，不要 markdown 代码块或解释文字\n"
    )


# ---- 主入口 ----

def _trim_kpi(kpi: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "days": kpi.get("days"),
        "asset_filter": kpi.get("asset_filter"),
        "total_trades": kpi.get("total_trades"),
        "overall": kpi.get("overall"),
        "per_asset_action": (kpi.get("per_asset_action") or [])[:8],
        "by_event_strength": kpi.get("by_event_strength") or [],
        "buy_pattern": kpi.get("buy_pattern"),
        "sell_pattern": kpi.get("sell_pattern"),
    }
    return out


def generate_strategy_reflection(period_days: int = _DEFAULT_PERIOD_DAYS,
                                 asset_filter: Optional[str] = None,
                                 llm_model_id: str = "") -> int:
    """组装 BuySellKpi + reflections 摘要 -> LLM -> 写 strategy_reflections (pending).

    Returns: 新插入 reflection id；样本不足返回 0。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        kpi = compute_buy_sell_kpi(conn, days=int(period_days), asset=asset_filter)
        sample_size = int(kpi.get("total_trades") or 0)
        if sample_size < _MIN_SAMPLE_SIZE:
            print(
                f"[STRAT-REFLECT] 样本不足: {sample_size} < {_MIN_SAMPLE_SIZE}, "
                f"period_days={period_days}, asset={asset_filter}, 跳过",
                flush=True,
            )
            return 0

        end_dt = datetime.now(config.TZ_SHANGHAI)
        start_dt = end_dt - timedelta(days=int(period_days))
        period_start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        period_end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        refl_dist = _reflection_layer_distribution(conn, period_start, period_end)

        kpi_text = _format_kpi_for_prompt(kpi, refl_dist)
        prompt = _build_prompt(kpi_text, period_days, asset_filter or "")
        system = "你是量化交易策略审查员，只输出严格 JSON，不要 markdown 包装、不要解释。"
        raw = review_agent._llm(prompt, system, max_tokens=2000, timeout=120)
        parsed = _parse_json(raw) if isinstance(raw, str) else None
        validated = _validate_llm_payload(parsed)
        if not validated or not validated.get("llm_summary"):
            print("[STRAT-REFLECT] LLM 输出无效或为空，跳过入库", flush=True)
            return 0

        cur = conn.execute(
            """INSERT INTO strategy_reflections
               (period_start, period_end, period_days, sample_size,
                source_summary_json, llm_summary,
                strategy_patterns_json, recommendations_json,
                affected_dimension, evidence_summary, overall_confidence,
                status, llm_model_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                period_start, period_end, int(period_days), sample_size,
                json.dumps(
                    {"kpi": _trim_kpi(kpi), "reflection_dist": refl_dist},
                    ensure_ascii=False,
                )[:8000],
                validated["llm_summary"],
                json.dumps(validated["strategy_patterns"], ensure_ascii=False),
                json.dumps(validated["recommendations"], ensure_ascii=False),
                validated["affected_dimension"],
                validated["evidence_summary"],
                validated["overall_confidence"],
                "pending", llm_model_id or "",
            ),
        )
        conn.commit()
        rid = int(cur.lastrowid)
        print(
            f"[STRAT-REFLECT] ok: id={rid} sample={sample_size} "
            f"patterns={len(validated['strategy_patterns'])} "
            f"recs={len(validated['recommendations'])}",
            flush=True,
        )
        return rid
    except Exception as exc:
        print(f"[STRAT-REFLECT] generate 失败: {type(exc).__name__}: {exc}", flush=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


# ---- 审批 ----

def _get_current_strategy_safe(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    try:
        import strategy_store as _ss
        return _ss.get_current_strategy(conn)
    except Exception:
        return None


def approve_strategy_reflection(reflection_id: int,
                               apply_to_settings: bool = True,
                               note: str = "") -> Dict[str, Any]:
    """审批 reflection -> 循环每条 recommendation -> apply 到 settings 或 strategy_version."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT * FROM strategy_reflections
                WHERE id=? AND status='pending'""",
            (int(reflection_id),),
        ).fetchone()
        if row is None:
            return {"reflection_id": int(reflection_id), "status": "not_found_or_not_pending"}

        recs = json.loads(row["recommendations_json"] or "[]")
        if not recs:
            conn.execute(
                """UPDATE strategy_reflections
                    SET status='approved', admin_note=?, approved_at=datetime('now')
                    WHERE id=? AND status='pending'""",
                (note or "", int(reflection_id)),
            )
            conn.commit()
            return {"reflection_id": int(reflection_id), "status": "approved",
                    "applied_count": 0, "skipped_count": 0, "applied_details": []}

        applied_details: List[Dict[str, Any]] = []
        applied_count = 0
        skipped_count = 0

        for rec in recs:
            key = str(rec.get("key") or "")
            direction = str(rec.get("direction") or "")
            magnitude = float(rec.get("magnitude") or 0.05)
            if key not in _V182_PARAM_KEYS or direction not in _VALID_DIRECTIONS:
                skipped_count += 1
                continue
            adj = {
                "asset": "*",
                "action": "*",
                "param_key": key,
                "direction": direction,
                "magnitude": magnitude,
            }
            try:
                if apply_to_settings and key in _SETTINGS_KEYS:
                    change = _pt_mod.apply_adjustment_to_settings(adj, conn)
                    applied_details.append({
                        "key": key, "applied_to": "paper_trading_settings",
                        "old": change["old_value"], "new": change["new_value"],
                    })
                    applied_count += 1
                else:
                    cur = _get_current_strategy_safe(conn)
                    if cur is None:
                        skipped_count += 1
                        continue
                    sid = int(cur["id"])
                    cur_params = dict(cur.get("active_version", {}).get("params") or {})
                    cur_val = cur_params.get(key)
                    if direction in ("on", "off"):
                        cur_params[key] = 1 if direction == "on" else 0
                    else:
                        try:
                            base = float(cur_val or 0)
                        except (TypeError, ValueError):
                            base = 0.5
                        factor = 1.0 + (magnitude if direction == "up" else -magnitude)
                        cur_params[key] = round(base * factor, 4)
                    try:
                        ver = _add_strategy_version(
                            sid, cur_params, source="reflection",
                            note=f"v201 strategy_reflection approved: "
                                 f"{key} {direction} {magnitude}",
                            connection=conn,
                        )
                        applied_details.append({
                            "key": key, "applied_to": "strategy_version",
                            "new_version_id": int(ver["id"]),
                            "version": int(ver["version"]),
                        })
                        applied_count += 1
                    except ValueError:
                        skipped_count += 1
            except Exception as exc:
                print(
                    f"[STRAT-REFLECT] apply rec key={key} failed: "
                    f"{type(exc).__name__}: {exc}", flush=True,
                )
                skipped_count += 1

        new_status = "applied" if applied_count > 0 else "approved"
        applied_version_ids = [
            d.get("new_version_id") for d in applied_details
            if d.get("applied_to") == "strategy_version" and d.get("new_version_id")
        ]
        conn.execute(
            """UPDATE strategy_reflections
                SET status=?, admin_note=?, approved_at=datetime('now'),
                    applied_strategy_version_id=?
                WHERE id=? AND status='pending'""",
            (new_status, note or "",
             applied_version_ids[0] if applied_version_ids else None,
             int(reflection_id)),
        )
        conn.commit()
        return {
            "reflection_id": int(reflection_id),
            "status": new_status,
            "applied_count": applied_count,
            "skipped_count": skipped_count,
            "applied_details": applied_details,
        }
    except Exception as exc:
        conn.rollback()
        print(f"[STRAT-REFLECT] approve failed: {type(exc).__name__}: {exc}", flush=True)
        return {"reflection_id": int(reflection_id), "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def reject_strategy_reflection(reflection_id: int, note: str = "") -> bool:
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """UPDATE strategy_reflections
                SET status='rejected', admin_note=?, approved_at=datetime('now')
                WHERE id=? AND status='pending'""",
            (str(note or "")[:500], int(reflection_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


# ---- 周期 daemon + 过期清理 ----

def run_strategy_reflection_daemon(interval_days: int = _DAEMON_INTERVAL_DAYS,
                                   period_days: int = _DEFAULT_PERIOD_DAYS) -> None:
    print(
        f"[STRAT-REFLECT] daemon 启动: interval={interval_days}d, period={period_days}d",
        flush=True,
    )
    while True:
        try:
            rid = generate_strategy_reflection(period_days=period_days)
            if rid:
                print(f"[STRAT-REFLECT] daemon 生成 id={rid}", flush=True)
        except Exception as exc:
            print(f"[STRAT-REFLECT] daemon 失败: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(int(interval_days) * 86400)


def expire_old_reflections(days_old: int = 90) -> int:
    """status='pending' 且 created_at > days_old 天前 -> status='expired'."""
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """UPDATE strategy_reflections
                SET status='expired'
                WHERE status='pending'
                  AND created_at < datetime('now', ?)""",
            (f"-{int(days_old)} days",),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()
