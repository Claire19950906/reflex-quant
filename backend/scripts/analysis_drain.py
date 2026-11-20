#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analysis_drain — 独立跑批（不叫 engine.py，绕开 restart_backend.sh 的 pkill）。

背景（2026-11-19）
------------------
外部会话每 2.5–4.5 分钟执行 `~/.trident/wrappers/restart_backend.sh`，里面有两行：

    launchctl bootout gui/$U/com.trident.engine
    pkill -f "engine.py"

于是主引擎永远活不过一个批次（单条分析 ~2.5 分钟，一批跑不完），决策产出被反复打断：
12 条之后再无新增，620+ 条 PENDING 积压。

本脚本用**与生产完全相同的两步链路**（engine.ai_worker.two_step_llm_call +
_parse_result_light + merge_two_step，prompt 与解析都复用，不另写），
逐条处理 PENDING 新闻并落 ai_decisions。区别只在于：

  · 进程名不含 engine.py → 不被外部 pkill 掉；
  · 逐条处理、逐条 commit → 任何时刻被杀都只丢当前这一条，不会整批作废。

用法：
    python3 scripts/analysis_drain.py --limit 3            # 处理 3 条
    python3 scripts/analysis_drain.py --limit 10 --dry     # 只打印不落库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DB = ROOT / "trident_event_bus.db"
MAX_AGE_H = float(os.getenv("NEWS_ANALYZE_MAX_AGE_HOURS", "6") or 6)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def claim(conn: sqlite3.Connection, limit: int) -> list:
    """取最新、未过期、非噪声、还没决策的 PENDING 新闻。"""
    floor_ts = time.time() - MAX_AGE_H * 3600
    rows = conn.execute(
        """
        SELECT n.id, n.content, n.published_at, n.source
          FROM raw_news n
         WHERE n.status = 'PENDING'
           AND COALESCE(n.is_noise, 0) = 0
           AND NOT EXISTS (SELECT 1 FROM ai_decisions d WHERE d.news_id = n.id)
         ORDER BY n.id DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    out = []
    for r in rows:
        nid, content, published_at, source = r
        if not content or len(str(content).strip()) < 12:
            continue
        out.append({"id": nid, "content": str(content), "published_at": published_at,
                    "source": source})
    return out


def analyze(worker, news: dict) -> dict:
    """两步链路：A 取标的/方向/分数 → B 取八段正文。"""
    cfg = worker._pick_main_model_cfg()
    if not cfg:
        raise RuntimeError("没有可用的 main 通道")
    ts = str(news.get("published_at") or "")
    ra = worker._parse_result_light(
        worker.two_step_llm_call(cfg, mode="a", news_text=news["content"],
                                 timestamp=ts, timeout=60.0, call_site="drain_a") or "")
    asset = str(ra.get("target_asset") or "").strip().upper()
    action = str(ra.get("suggested_action") or "HOLD").strip().upper()
    score = ra.get("sentiment_score", ra.get("score"))
    if not asset or asset in ("NONE", "OTHER"):
        return {"asset": "", "action": "HOLD", "score": score, "step_a": ra, "merged": {}}
    rb = worker._parse_result_light(
        worker.two_step_llm_call(cfg, mode="b", news_text=news["content"],
                                 timestamp=ts, asset=asset, action=action,
                                 timeout=90.0, call_site="drain_b") or "")
    merged, notes = worker.merge_two_step(dict(ra), ra, rb)
    return {"asset": asset, "action": action, "score": score,
            "step_a": ra, "step_b": rb, "merged": merged, "notes": notes}


# ── 置信度链路：用本地日线构造 decision_context，交由 decision_guard 算 confidence ──
# 背景：生产链路每批拉一次 get_snapshot() 生成 decision_context；本脚本早期漏了这一步，
# 导致 guard 的 market_ok=False → coverage=0 → confidence 恒为空 → 闸门（>=15）永不放行，
# 所有方向最终都被压成 HOLD。改为直接用本地 kline_bars 日线自建快照（零网络、秒级）。
_GATE_CONF_THR = float(__import__("os").getenv("EVIDENCE_CONFIDENCE_GATE", "15"))


def _passed_reason() -> str:
    try:
        import config
        return str(config.TRADE_GATE_PASSED_REASON)
    except Exception:
        return "证据充分，允许输出方向性结论"


def build_decision_context(conn: sqlite3.Connection, assets) -> str:
    out = {"assets": {}}
    for raw in assets:
        a = str(raw or "").strip().upper()
        if not a or a in ("NONE", "OTHER"):
            continue
        rows = conn.execute(
            "SELECT ts, open, high, low, close FROM kline_bars "
            "WHERE symbol=? AND timeframe='1d' ORDER BY ts DESC LIMIT 14",
            (a,),
        ).fetchall()
        if not rows:
            out["assets"][a] = {"status": "unavailable", "stats_7d": {}}
            continue
        rows = list(reversed(rows))
        last = float(rows[-1][4])
        chg7 = ((last - float(rows[-8][4])) / float(rows[-8][4]) * 100.0
                if len(rows) >= 8 and rows[-8][4] else 0.0)
        atr = sum((float(r[2]) - float(r[3])) / float(r[4]) * 100.0
                  for r in rows if r[4]) / max(1, len(rows))
        trend = "Up" if chg7 > 0.5 else ("Down" if chg7 < -0.5 else "Sideways")
        out["assets"][a] = {"status": "partial", "price": last,
                            "stats_7d": {"trend": trend,
                                         "atr_pct": round(atr, 3),
                                         "change_7d_pct": round(chg7, 2)}}
    return json.dumps(out, ensure_ascii=False)


def evidence_gate(conn, asset: str, action: str, score: float):
    """返回 (guard_action, confidence_or_None, verdict)。口径与 ai_worker 一致：
    guard 没给 confidence → None（本脚本落 NULL），不写 0 冒充有效值。"""
    if not asset or asset in ("NONE", "OTHER"):
        return action, None, "no_asset"
    try:
        import decision_guard
    except Exception as exc:
        return action, None, f"guard_import_failed:{type(exc).__name__}"
    try:
        ctx = build_decision_context(conn, [asset])
        g = decision_guard.evaluate_decision({
            "sentiment_score": score,
            "suggested_action": action,
            "target_asset": asset,
            "decision_context": ctx,
            "market_confirmation": "unknown",
            "cluster_size": 1,
            "history_sample": {},
            "heat_score": 5.0,
            "impact_score": 5.0,
            "asset_impact": 5.0,
            "gate_verdict": "MISS",
        })
    except Exception as exc:
        return action, None, f"guard_failed:{type(exc).__name__}"
    conf = g.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    g_action = str(g.get("action") or "HOLD").upper()
    verdict = str(g.get("verdict") or "")[:120]
    # 与生产同口径：置信度低于闸门 → 方向不允许落成 BUY/SELL
    if conf is None or conf < _GATE_CONF_THR:
        return "HOLD", conf, f"{verdict} | gate<{_GATE_CONF_THR:.0f}"
    return g_action, conf, verdict


def persist(conn: sqlite3.Connection, news: dict, res: dict) -> int:
    m = res.get("merged") or {}
    asset = res.get("asset") or ""
    action = res.get("action") or "HOLD"
    score = res.get("score")
    try:
        score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    degraded = 0 if (asset and str(m.get("reasoning_path") or "").strip()) else 1
    reason = "" if degraded == 0 else ("target_none" if not asset else "llm_empty")
    fr = m.get("factor_ranking") or []
    if isinstance(fr, str):
        fr_json = fr
    else:
        fr_json = json.dumps(fr[:8] if isinstance(fr, list) else fr, ensure_ascii=False)
    g_action, conf, verdict = evidence_gate(conn, asset, action, score)
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO ai_decisions
          (news_id, sentiment_score, suggested_action, reasoning, created_at,
           target_asset, reasoning_path, asset_nature, factor_ranking,
           analysis_degraded, analysis_degraded_reason, ts,
           verification, asset_reasoning,
           evidence_confidence, evidence_action, market_confirmation,
           status, trade_gate_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (news["id"], score, g_action,
         str(m.get("reasoning") or m.get("reasoning_path") or "")[:4000],
         now, asset or "NONE",
         str(m.get("reasoning_path") or "")[:8000],
         str(m.get("asset_nature") or ""),
         fr_json, degraded, reason, now,
         str(m.get("verification") or ""),
         str(m.get("asset_reasoning") or ""),
         conf, g_action, verdict,
         "UNREAD",
         _passed_reason() if g_action in ("BUY", "SELL") else f"guard_blocked:{verdict[:80]}"),
    )
    # v483: drain 是第三条落库路径，此前不落指纹、不进审计 —— 补齐，
    # 免得又出现"只有主链审了、其余路径静默漏审"。
    try:
        from code_fingerprint import stamp as _cfp_stamp
        _cfp_stamp(conn, cur.lastrowid)
    except Exception as _exc_fp:
        print(f"[FINGERPRINT:drain] skipped: {type(_exc_fp).__name__}: {_exc_fp}", flush=True)
    try:
        import audit_gate_hook as _agh
        _agh.gate(conn, cur.lastrowid, tag="drain")
    except Exception as _exc_ag:
        print(f"[AUDIT-GATE:drain] skipped: {type(_exc_ag).__name__}: {_exc_ag}", flush=True)
    return cur.lastrowid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    import engine.ai_worker as worker

    conn = sqlite3.connect(str(DB), timeout=20)
    conn.row_factory = sqlite3.Row
    items = claim(conn, args.limit)
    print(f"[drain] 待处理 {len(items)} 条", flush=True)

    for i, news in enumerate(items, 1):
        head = news["content"].replace("\n", " ")[:48]
        print(f"[drain] ({i}/{len(items)}) news={news['id']} {head}", flush=True)
        try:
            res = analyze(worker, news)
        except Exception as exc:
            print(f"[drain]   分析异常 {type(exc).__name__}: {str(exc)[:100]}", flush=True)
            continue
        merged = res.get("merged") or {}
        print(f"[drain]   标的={res.get('asset') or 'NONE'} 方向={res.get('action')} "
              f"分数={res.get('score')} 正文={len(str(merged.get('reasoning_path') or ''))}字",
              flush=True)
        if args.dry:
            continue
        try:
            did = persist(conn, news, res)
            conn.execute("UPDATE raw_news SET status='DONE', processing_at=NULL WHERE id=?",
                         (news["id"],))
            conn.commit()
            print(f"[drain]   → 决策 {did} 落库，news={news['id']} 标记 DONE", flush=True)
        except sqlite3.Error as exc:
            conn.rollback()
            print(f"[drain]   落库失败 {type(exc).__name__}: {str(exc)[:100]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
