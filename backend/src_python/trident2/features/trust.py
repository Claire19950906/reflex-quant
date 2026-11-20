# -*- coding: utf-8 -*-
"""结论可信类（4 项）：F59 / F60 / F61 / F62。

这一组回答同一个问题：系统说的话能不能当真。
凡是样本不够、字段缺失，一律写清缺什么，不拿「看起来还行」糊过去。
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Dict, List, Optional

from .. import store
from ..registry import feature
from ..types import FeatureResult
from ._util import correlation, mean, pct_change, stdev
from .research import _phi

MIN_BUCKET_N = 10


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


# ── F59 置信度校准 ──────────────────────────────────────────────────────
@feature("F59")
def confidence_calibration(ctx, buckets: int = 5) -> FeatureResult:
    # v404 隔离：校准只做真实已结算决策。历史回放（t2_sim_trades）的"置信度"是
    # 新闻/决策评分代理，且样本可重复生成 —— 拿它算 ECE 等于自证可信。
    usable: List[tuple] = []
    conf_source = ""
    rows = [dict(r) for r in ctx.q(
        "SELECT id, evidence_confidence, is_correct, settled, forward_pnl, suggested_action "
        "FROM ai_decisions WHERE evidence_confidence IS NOT NULL AND settled=1",
        note="calibration_rows")]
    for r in rows:
        conf = _num(r["evidence_confidence"])
        outcome = None
        if r.get("is_correct") is not None:
            v = str(r["is_correct"]).strip().upper()
            if v in ("1", "TRUE", "WIN", "CORRECT", "YES", "T"):
                outcome = 1.0
            elif v in ("0", "FALSE", "LOSS", "WRONG", "NO", "F"):
                outcome = 0.0
        elif r.get("forward_pnl") is not None:
            pnl = float(r["forward_pnl"])
            direction = 1.0 if (r.get("suggested_action") or "").upper() == "BUY" else -1.0
            outcome = 1.0 if direction * pnl > 0 else 0.0
        if conf is not None and outcome is not None:
            usable.append((conf, outcome))
    conf_source = "ai_decisions.evidence_confidence（已结算决策）"
    if len(usable) < MIN_BUCKET_N:
        return FeatureResult.no_data(
            "F59", f"置信度校准：可用样本仅 {len(usable)} 条",
            f"需要 ≥{MIN_BUCKET_N} 条「置信度 + 实际对错」都齐全的真实样本，当前不足；"
            f"历史回放 {ctx.replay_count()} 条已隔离，不参与校准",
            evidence=ctx.evidence(table="ai_decisions"),
            sample_size=len(usable),
        )
    max_conf = max(c for c, _ in usable)
    scale = max_conf if max_conf > 1 else 1.0
    norm = [(c / scale, o) for c, o in usable]
    norm.sort()
    size = max(len(norm) // buckets, 1)
    curve = []
    ece = 0.0
    for i in range(0, len(norm), size):
        chunk = norm[i:i + size]
        if len(chunk) < 3:
            continue
        avg_conf = mean([c for c, _ in chunk])
        hit = mean([o for _, o in chunk])
        curve.append({"bucket": len(curve) + 1, "n": len(chunk),
                      "mean_confidence": round(avg_conf, 4),
                      "actual_hit_rate": round(hit, 4),
                      "gap": round(hit - avg_conf, 4)})
        ece += len(chunk) / len(norm) * abs(hit - avg_conf)
    over = [c for c in curve if c["gap"] <= -0.1]
    return FeatureResult.ok(
        "F59", f"置信度校准：ECE {ece:.3f}，{len(over)}/{len(curve)} 个分桶高估",
        {"n": len(usable), "ece": round(ece, 4), "curve": curve,
         "confidence_scale": {"raw_max": max_conf, "normalized_by": scale},
         "data_class": "live",
         "confidence_source": conf_source,
         "verdict": "高估" if ece >= 0.1 else ("低估" if ece <= -0.1 else "基本校准"),
         "caveat": f"置信度来源见 confidence_source；原始分最大 {max_conf:.2f}，"
                   f"超出 0–1 口径时按最大值归一；样本仅限真实已结算决策，历史回放不参与"},
        evidence=ctx.evidence(table="ai_decisions"), sample_size=len(usable),
        severity="warn" if ece >= 0.1 else "info",
    )


# ── F60 事件研究 ────────────────────────────────────────────────────────
@feature("F60")
def event_study(ctx, hours: int = 24 * 7, groups_min: int = 3) -> FeatureResult:
    events = [e for e in ctx.news(hours=hours)
              if e.get("asset") and str(e["asset"]).upper() in ctx.kline_symbols()]
    if not events:
        return FeatureResult.no_data(
            "F60", "事件研究：窗口内无事件", f"最近 {hours}h 无带标的新闻",
            evidence=ctx.evidence(table="news_events"),
        )
    samples: List[Dict[str, Any]] = []
    for e in events[:200]:
        asset = e["asset"].upper()
        ts = int(e["ts"])
        bars = ctx.q(
            "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe='1h' "
            "AND ts>=? AND ts<=? ORDER BY ts",
            (asset, ts * 1000, (ts + 25 * 3600) * 1000), note=f"event_bars:{asset}")
        if not bars:
            continue
        closes = [(int(b[0]) // 1000 if int(b[0]) > 10_000_000_000 else int(b[0]), b[1]) for b in bars]
        base = closes[0][1]
        def _ret(target_h: int) -> Optional[float]:
            for t, c in closes:
                if t >= ts + target_h * 3600:
                    return pct_change(c, base) if abs(t - (ts + target_h * 3600)) <= 5400 else None
            return None
        direction = 1.0 if (e.get("action") or "").upper() == "BUY" else (
            -1.0 if (e.get("action") or "").upper() == "SELL" else 0.0)
        samples.append({"action": e.get("action") or "HOLD", "direction": direction,
                        "asset": asset, "r1": _ret(1), "r4": _ret(4), "r24": _ret(24)})
    if not samples:
        return FeatureResult.no_data(
            "F60", "事件研究：事件与 K 线未对齐",
            "新闻缺少对应资产的小时级行情，无法计算异常收益",
            evidence=ctx.evidence(tables=["news_events", "kline_bars"]),
        )
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples:
        groups.setdefault(s["action"], []).append(s)
    out = []
    for action, items in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        entry = {"action": action, "n": len(items)}
        for key in ("r1", "r4", "r24"):
            vals = [s[key] * (s["direction"] or 1.0) for s in items if s[key] is not None]
            if len(vals) < 2:
                entry[key] = None
                continue
            m = mean(vals)
            sd = stdev(vals)
            tstat = (m / (sd / math.sqrt(len(vals)))) if sd else None
            entry[key] = {"n": len(vals), "mean_pct": round(m, 3),
                          "t_stat": round(tstat, 2) if tstat else None,
                          "p_value": round(2 * (1 - _phi(abs(tstat))), 4) if tstat else None,
                          "significant": bool(tstat and abs(tstat) >= 1.96)}
        out.append(entry)
    big = [g for g in out if isinstance(g.get("r24"), dict) and g["r24"]["significant"]]
    return FeatureResult.ok(
        "F60", f"事件研究：{len(samples)} 个事件，{len(big)} 组 24h 收益显著（|t|≥1.96）",
        {"groups": out, "window_hours": hours,
         "method": "方向调整后收益（BUY 取正、SELL 取反）相对事件首根 1h 收盘；t 检验双侧",
         "caveat": "未扣交易成本；同一事件重复计数未做去重，样本独立性有限"},
        evidence=ctx.evidence(tables=["news_events", "kline_bars"]), sample_size=len(samples),
        severity="watch" if big else "info",
    )


# ── F61 输入快照 ────────────────────────────────────────────────────────
@feature("F61")
def input_snapshot(ctx, capture: bool = True, limit: int = 30) -> FeatureResult:
    decisions = ctx.decisions(limit=limit)
    if not decisions:
        return FeatureResult.no_data(
            "F61", "输入快照：无决策", "ai_decisions 为空，没有需要快照的对象",
            evidence=ctx.evidence(table="ai_decisions"),
        )
    captured = 0
    for d in decisions:
        did = int(d["id"])
        if ctx.q("SELECT 1 FROM t2_decision_snapshots WHERE decision_id=? LIMIT 1", (did,),
                 note="snapshot_exists"):
            continue
        if not capture:
            continue
        asset = (d.get("target_asset") or "").upper() or None
        created = _num(d.get("created_at"))
        ts = int(created / 1000) if created and created > 10_000_000_000 else int(created or 0)
        market = {}
        if asset:
            t = ctx.latest_tick(asset)
            market = {"asset": asset, "price": t.get("price") if t else None,
                      "change24h": t.get("change24h") if t else None,
                      "tick_ts": t.get("ts") if t else None,
                      "price_age_sec": ctx.price_age_sec(asset) if t else None}
        news = [{"news_id": e.get("news_id") or e.get("id"), "ts": e["ts"],
                 "source": e.get("source"), "headline": (e.get("headline") or "")[:100]}
                for e in (ctx.news(hours=6) if ts else [])
                if (e.get("asset") or "").upper() == (asset or "")][:5]
        prompt_hash = _hash({"strategy_id": d.get("strategy_id"),
                             "action": d.get("suggested_action"),
                             "confidence": d.get("evidence_confidence"),
                             "invalidation": d.get("invalidation_condition"),
                             "market": market, "news_ids": [n["news_id"] for n in news]})
        versions = ctx.strategy_versions()
        code_version = (versions[0].get("version") or versions[0].get("name")) if versions else None
        ctx.conn.execute(
            "INSERT OR IGNORE INTO t2_decision_snapshots "
            "(decision_id, ts, market_json, news_json, prompt_hash, model, code_version, captured_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (did, ts or ctx.now, store._dump(market), store._dump(news), prompt_hash,
             None, code_version, ctx.now),
        )
        captured += 1
    if capture:
        ctx.conn.commit()
    snaps = [dict(r) for r in ctx.q(
        "SELECT * FROM t2_decision_snapshots ORDER BY id DESC LIMIT 500",
        note="decision_snapshots")]
    coverage = len(snaps) / max(len(ctx.decisions(limit=500)), 1)
    missing = []
    if not any(s.get("model") for s in snaps):
        missing.append("模型版本未落库（决策记录里没有 model 字段）")
    if not any(s.get("prompt_hash") for s in snaps):
        missing.append("prompt 原文未落库，只存了输入指纹 hash")
    return FeatureResult.ok(
        "F61", f"输入快照：已覆盖 {len(snaps)} 条决策（覆盖 {coverage:.0%}，本轮新增 {captured} 条）",
        {"snapshots": len(snaps), "coverage": round(coverage, 4), "captured_now": captured,
         "gaps": missing,
         "content": {"market_json": "价格/24h 涨跌/行情年龄", "news_json": "决策前 6h 同标的新闻",
                     "prompt_hash": "策略/动作/置信/失效条件/行情/新闻 ID 的 SHA256 前 16 位",
                     "code_version": "strategy_versions 最新版本"}},
        evidence=ctx.evidence(tables=["ai_decisions", "t2_decision_snapshots"]),
        sample_size=len(snaps), severity="watch" if missing else "info",
    )


# ── F62 Prompt 回归集 ───────────────────────────────────────────────────
@feature("F62")
def prompt_regression(ctx, seed: bool = True, target_size: int = 100) -> FeatureResult:
    rows = ctx.eval_set()
    seeded = 0
    if seed and len(rows) < target_size:
        hist = [dict(r) for r in ctx.q(
            "SELECT id, created_at, target_asset, suggested_action, forward_pnl, is_correct, "
            "evidence_confidence, impact_score, settled FROM ai_decisions "
            "WHERE settled=1 ORDER BY id DESC LIMIT ?", (target_size * 2,), note="eval_seed")]
        existing = {(r.get("event_ts"), r.get("asset")) for r in rows}
        for h in hist:
            created = _num(h.get("created_at"))
            ts = int(created / 1000) if created and created > 10_000_000_000 else int(created or 0)
            key = (ts, (h.get("target_asset") or "").upper())
            if key in existing:
                continue
            expected = {"decision_id": h["id"], "action": h.get("suggested_action"),
                        "forward_pnl": h.get("forward_pnl"), "is_correct": h.get("is_correct")}
            ctx.conn.execute(
                "INSERT INTO t2_eval_set (event_ts, asset, headline, expected_json, prompt_id, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (ts, key[1], None, store._dump(expected), "baseline", ctx.now),
            )
            seeded += 1
            if len(rows) + seeded >= target_size:
                break
        ctx.conn.commit()
        rows = ctx.eval_set()
    if not rows:
        return FeatureResult.no_data(
            "F62", "Prompt 回归集：空集",
            "t2_eval_set 为空且历史已结算决策不足以构建评测集",
            evidence=ctx.evidence(table="t2_eval_set"),
        )
    with_expected = [r for r in rows if r.get("expected_json")]
    run_ids = {r.get("run_id") for r in rows if r.get("run_id")}
    filled_runs = [r for r in rows if r.get("output_json")]
    return FeatureResult.ok(
        "F62", f"Prompt 回归集：{len(rows)} 条（本轮种子 {seeded} 条），"
              f"已跑 {len(filled_runs)} 条 / {len(run_ids)} 个版本",
        {"size": len(rows), "seeded_now": seeded, "with_expected": len(with_expected),
         "run_ids": sorted(run_ids), "output_filled": len(filled_runs),
         "target_size": target_size,
         "usage": "改 prompt 后对同一批事件重跑，比较 output_json 与 expected_json 的分歧率",
         "missing": ["回跑执行器尚未接：需要把批量重跑接到 LLM 调用层后才有分歧率"]},
        evidence=ctx.evidence(table="t2_eval_set"), sample_size=len(rows),
        severity="watch" if seeded else "info",
    )
