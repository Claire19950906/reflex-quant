#!/usr/bin/env python3
"""v173 结构化校验真实性飞轮 — 监听 ai_decisions.structured_check verdict。

工作流（每 WORKER_INTERVAL_SECONDS 一批）：
1. 拉 pending structured_reflections 批次（≤ BATCH_SIZE 笔）
2. 按 (asset, event_type_code, verdict) 分组聚合
3. 同组计数 ≥ PROMOTE_THRESHOLD → 触发 review_insights 沉淀（kind=1 软启发，
   可被 _evaluate_insights 复用"停用/复活/自愈"逻辑）
4. 飞书告警：复用 alerts.send_feishu_alert 推 truth 卡片（每日同组节流）
5. 单笔失败计入 retry_count（≤ MAX_RETRY 才永久 failed），避免抖动永久丢失

设计要点：
- 与 engine/reflection.py（基于 PnL 的 trade 反思）平行运行，两套独立 worker 但共享 review_insights 表
- 不调 LLM：所有信号来自 ai_decisions.structured_check 已经写好的 verdict + score
- 沉淀经验 kind=1（软启发），origin='truth' 区分于 'real' (PnL反思) / 'quick_sim' / 'rule'
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import db

from .utils import _now
from .alerts import send_feishu_alert


# ── 参数 ─────────────────────────────────────────────────────────────────────
PROMOTE_THRESHOLD = 3            # 同组累计 ≥ N 笔 → 沉淀
BATCH_SIZE = 20                  # 单批上限
MAX_RETRY = 3                    # 单笔最大重试次数
WORKER_INTERVAL_SECONDS = 60.0   # worker 轮询间隔
ALERT_THROTTLE_PER_DAY = 1       # 同组每日最多推几次飞书
PROCESS_BUDGET_SECONDS = 15.0    # 单次 process_once wall-clock 上限
PROMOTABLE_VERDICTS = ("fabricated", "partial")  # 仅这两种入飞轮


# ── DB helper ────────────────────────────────────────────────────────────────

def _open_db() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


# ── 入队（供 ai_worker 调用）────────────────────────────────────────────────

def enqueue(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    news_id: Optional[int],
    asset: str,
    event_type_code: str,
    verdict: str,
    score_coverage: float,
    score_fabrication: float,
    entities_fabricated: List[str],
    events_missing: List[str],
) -> bool:
    """ai_worker 生成 structured_check 后调用，把 fabricated/partial 决策入飞轮。

    幂等：同 decision_id 只入一次（UNIQUE 约束）。
    返回 True 表示成功入队；False 表示未入队（verdict=ok 或重复）。
    """
    if verdict not in PROMOTABLE_VERDICTS:
        return False
    try:
        conn.execute(
            """INSERT OR IGNORE INTO structured_reflections
               (decision_id, news_id, asset, event_type_code, verdict,
                score_coverage, score_fabrication,
                entities_fabricated, events_missing, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                       datetime('now'), datetime('now'))""",
            (
                int(decision_id),
                int(news_id) if news_id is not None else None,
                str(asset or ""),
                str(event_type_code or ""),
                str(verdict or ""),
                float(score_coverage or 0.0),
                float(score_fabrication or 0.0),
                json.dumps(list(entities_fabricated or []), ensure_ascii=False),
                json.dumps(list(events_missing or []), ensure_ascii=False),
            ),
        )
        return True
    except Exception as exc:
        print(f"  [STRUCT-REFLECT] enqueue 失败 d={decision_id}: {type(exc).__name__}: {str(exc)[:80]}")
        return False


# ── 状态变更 helper ──────────────────────────────────────────────────────────

def _mark_promoted(conn: sqlite3.Connection, ids: List[int], insight_id: int) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE structured_reflections
            SET status='promoted', promoted_insight_id=?,
                updated_at=datetime('now')
            WHERE id IN ({placeholders})""",
        [int(insight_id)] + list(int(i) for i in ids),
    )


def _mark_failed(conn: sqlite3.Connection, ids: List[int], err: str) -> None:
    if not ids:
        return
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for i in ids:
        row = conn.execute(
            "SELECT retry_count FROM structured_reflections WHERE id = ?", (int(i),)
        ).fetchone()
        cur = int((row["retry_count"] if row else 0) or 0)
        final_retry = min(cur + 1, MAX_RETRY)
        conn.execute(
            """UPDATE structured_reflections
               SET status='failed', retry_count=?, last_error=?, updated_at=?
               WHERE id=?""",
            (final_retry, err[:200], now_iso, int(i)),
        )


def _mark_skipped(conn: sqlite3.Connection, ids: List[int], reason: str) -> None:
    """未触发沉淀（阈值未到 / 已存在 active insight）→ 标 skipped 留底。"""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE structured_reflections
            SET status='skipped', last_error=?, updated_at=datetime('now')
            WHERE id IN ({placeholders})""",
        [reason[:200]] + list(int(i) for i in ids),
    )


def _fetch_pending(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    """拉一批 pending 反思（按 id 升序，老的优先）。"""
    return conn.execute(
        """SELECT * FROM structured_reflections
           WHERE status = 'pending'
           ORDER BY id ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()


# ── 沉淀 + 告警 ──────────────────────────────────────────────────────────────

def _build_insight_text(asset: str, event_type_code: str, verdict: str,
                        count: int, missing: List[str], fabricated: List[str]) -> str:
    """生成 review_insights.rule 文本。"""
    if verdict == "fabricated":
        entities = ", ".join(fabricated[:5]) if fabricated else "(未识别具体实体)"
        return (
            f"[真实性告警] Asset={asset or '未指定'} · LLM 反复在 reasoning 中虚构实体 "
            f"(典型: {entities})，过去 {count} 笔同类决策均触发 fabricated 校验。"
            f"建议: 对该资产降低 confidence、或在 prompt 中加入反幻觉约束。"
        )
    elif verdict == "partial":
        missing_str = ", ".join(missing[:5]) if missing else event_type_code or "(未识别事件类型)"
        return (
            f"[覆盖度告警] Asset={asset or '未指定'} 经常漏报事件类型 {missing_str}，"
            f"过去 {count} 笔同类决策 verdict=partial。"
            f"建议: ai_worker prompt 中显式列出该资产的核心事件类型 checklist。"
        )
    return f"[自动沉淀] Asset={asset} · verdict={verdict} · 累计 {count} 次"


def _check_alert_throttle(conn: sqlite3.Connection, group_key: Tuple[str, str, str]) -> bool:
    """检查今日同组飞书告警节流（避免刷屏）。

    用 review_insights 当审计日志：origin='truth' 且当日同 asset+event_type_code 已写入过 → 阻断。
    """
    asset, event_type_code, verdict = group_key
    if not asset:
        return False  # 空 asset 永远不发告警（无意义）
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM review_insights
               WHERE origin='truth' AND asset=? AND date(created_at)=date(?)
                 AND (rule LIKE ? OR rule LIKE ?)""",
            (asset, today, f"%{event_type_code}%", f"%Asset={asset}%"),
        ).fetchone()
        n = int(row["n"] if row else 0)
        return n < ALERT_THROTTLE_PER_DAY
    except Exception:
        return True


def _promote_to_insights(conn: sqlite3.Connection,
                         group: Dict[Tuple[str, str, str], List[sqlite3.Row]]
                         ) -> List[Dict[str, Any]]:
    """同 (asset, event_type_code, verdict) 累计 ≥ PROMOTE_THRESHOLD → 写 review_insights。"""
    promoted = []
    for key, rows in group.items():
        if len(rows) < PROMOTE_THRESHOLD:
            continue
        asset, event_type_code, verdict = key

        # 聚合 fabricated / missing（去重）
        all_fab: List[str] = []
        all_miss: List[str] = []
        for r in rows:
            try:
                all_fab.extend(json.loads(r["entities_fabricated"] or "[]"))
            except Exception:
                pass
            try:
                all_miss.extend(json.loads(r["events_missing"] or "[]"))
            except Exception:
                pass
        all_fab = list(dict.fromkeys(all_fab))
        all_miss = list(dict.fromkeys(all_miss))

        rule_text = _build_insight_text(asset, event_type_code, verdict, len(rows), all_miss, all_fab)
        try:
            cur = conn.execute(
                """INSERT INTO review_insights
                   (rule, asset, scope, active, source_samples, origin, kind,
                    counter_factual, created_at)
                   VALUES (?, ?, 'auto_truth', 1, ?, 'truth', 1, ?, datetime('now'))""",
                (
                    rule_text,
                    asset or "",
                    int(len(rows)),
                    f"verdict={verdict}; 若后续同类 fabricated 率 < 5% 可考虑停用",
                ),
            )
            insight_id = int(cur.lastrowid or 0)
        except Exception as exc:
            print(f"  [STRUCT-REFLECT] 写入 review_insights 失败: {type(exc).__name__}: {exc}")
            _mark_failed(conn, [r["id"] for r in rows], f"insert_review_insights:{exc}")
            continue

        ids = [int(r["id"]) for r in rows]
        _mark_promoted(conn, ids, insight_id)
        promoted.append({
            "insight_id": insight_id,
            "rule": rule_text,
            "asset": asset,
            "event_type_code": event_type_code,
            "verdict": verdict,
            "count": len(rows),
            "row_ids": ids,
            "fabricated_entities": all_fab,
            "missing_events": all_miss,
        })
    return promoted


async def _send_truth_alert(promoted: List[Dict[str, Any]]) -> None:
    """飞书告警：复用 alerts.send_feishu_alert。"""
    for p in promoted:
        try:
            await send_feishu_alert(
                news_text=(
                    f"[v173 真实性飞轮] {p['rule']}\n"
                    f"缺失事件: {', '.join(p['missing_events'][:5]) or '(无)'}\n"
                    f"虚构实体: {', '.join(p['fabricated_entities'][:5]) or '(无)'}"
                ),
                action=f"VERDICT={p['verdict'].upper()}",
                score=float(PROMOTE_THRESHOLD),
                reason=(
                    f"Asset={p['asset'] or '未指定'} · event_type={p['event_type_code'] or '(未指定)'} · "
                    f"累计 {p['count']} 笔同类决策触发 → 已写入 review_insights "
                    f"(id={p['insight_id']}, origin=truth)"
                ),
                news_time=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            )
        except Exception as exc:
            print(f"  [STRUCT-REFLECT] 飞书告警失败 insight={p['insight_id']}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}")


# ── 主流程 ──────────────────────────────────────────────────────────────────

def process_once() -> Dict[str, Any]:
    """单次处理入口（也供测试直接调用）。

    Returns: {"processed": int, "promoted": int, "failed": int, "skipped": int, "insights": [...]}
    """
    started = time.monotonic()
    conn = _open_db()
    try:
        rows = _fetch_pending(conn, BATCH_SIZE)
        if not rows:
            return {"processed": 0, "promoted": 0, "failed": 0, "skipped": 0, "insights": []}

        # 分组
        groups: Dict[Tuple[str, str, str], List[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            key = (
                str(r["asset"] or ""),
                str(r["event_type_code"] or ""),
                str(r["verdict"] or ""),
            )
            groups[key].append(r)

        # 触发沉淀
        promoted = _promote_to_insights(conn, groups)
        conn.commit()

        # 标 skipped（未达阈值的同组 / 已存在 active insight 的同组）
        promoted_keys = {(p["asset"], p["event_type_code"], p["verdict"]) for p in promoted}
        skipped_ids: List[int] = []
        for key, grp_rows in groups.items():
            if key in promoted_keys:
                continue
            if time.monotonic() - started > PROCESS_BUDGET_SECONDS:
                continue
            asset_v, etc_v, verdict = key
            if verdict not in PROMOTABLE_VERDICTS:
                # 防御：非 fabricated/partial 入队（不应发生，但安全兜底）
                for r in grp_rows:
                    skipped_ids.append(int(r["id"]))
                continue
            # 若该 key 已经存在 active truth insight → 标 skipped 避免反复 pending 积压
            try:
                active = conn.execute(
                    """SELECT COUNT(*) AS n FROM review_insights
                       WHERE origin='truth' AND asset=? AND active=1
                         AND (rule LIKE ? OR rule LIKE ?)""",
                    (asset_v, f"%{etc_v}%", f"%Asset={asset_v or '未指定'}%"),
                ).fetchone()
                if int((active["n"] if active else 0) or 0) > 0:
                    for r in grp_rows:
                        skipped_ids.append(int(r["id"]))
                # 否则: 阈值未到且无 active insight → 留 pending 等下轮聚合
            except Exception:
                pass

        if skipped_ids:
            _mark_skipped(conn, skipped_ids, "threshold_not_reached_or_already_promoted")
            conn.commit()

        # 异步发飞书告警（节流检查在发送前）
        throttle_filtered = [
            p for p in promoted
            if _check_alert_throttle(conn, (p["asset"], p["event_type_code"], p["verdict"]))
        ]
        if throttle_filtered:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                asyncio.create_task(_send_truth_alert(throttle_filtered))
            else:
                # 测试路径 / 同步入口
                asyncio.run(_send_truth_alert(throttle_filtered))

        return {
            "processed": len(rows),
            "promoted": len(promoted),
            "failed": 0,
            "skipped": len(skipped_ids),
            "insights": promoted,
        }
    finally:
        conn.close()


# ── 后台 worker ──────────────────────────────────────────────────────────────

_worker_started_at: float = 0.0


async def structured_reflection_worker() -> None:
    """v173 真实性飞轮后台任务。

    与 engine/reflection.py（基于 PnL）平行运行；共享 review_insights 表。
    """
    global _worker_started_at
    _worker_started_at = time.monotonic()
    print(f"[STRUCT-REFLECT] Truth reflection worker started (threshold={PROMOTE_THRESHOLD}, "
          f"interval={WORKER_INTERVAL_SECONDS}s, batch={BATCH_SIZE})")
    while True:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, process_once)
            if res["processed"] > 0:
                print(f"  [STRUCT-REFLECT] 处理 {res['processed']} 笔 → "
                      f"promoted={res['promoted']} skipped={res['skipped']} failed={res['failed']}",
                      flush=True)
                for ins in res.get("insights", []):
                    print(f"    → insight#{ins['insight_id']} asset={ins['asset']} "
                          f"verdict={ins['verdict']} count={ins['count']}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  [STRUCT-REFLECT] worker error: {type(exc).__name__}: {str(exc)[:80]}")
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


def get_worker_status() -> Dict[str, Any]:
    """供 /api/health 或前端 dashboard 查看。"""
    age = (time.monotonic() - _worker_started_at) if _worker_started_at else 0.0
    return {
        "started": _worker_started_at > 0,
        "uptime_seconds": round(age, 1),
        "threshold": PROMOTE_THRESHOLD,
        "interval_seconds": WORKER_INTERVAL_SECONDS,
        "batch_size": BATCH_SIZE,
        "alert_throttle_per_day": ALERT_THROTTLE_PER_DAY,
        "max_retry": MAX_RETRY,
        "process_budget_seconds": PROCESS_BUDGET_SECONDS,
    }
