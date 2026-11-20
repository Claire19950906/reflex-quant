#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair_exec_state.py — 历史决策 exec_state / no_trade_reason 收口回填 (v490 P0-1)

为什么需要
----------
ai_worker 落库分支此前只在"建仓成功 / 风险拒绝 / 缺价 / 条件不满足"四种情形写
exec_state；其余路径（观望 HOLD、闸门降级、LLM 失败、标的越界等）落库后
exec_state 保持 NULL、no_trade_reason 可能为空。实测 11-19 库里 24 条决策里
3 条状态空值，下游成绩单 / 反思 / 权重训练只能在那一小撮留痕样本上算，形成
幸存者偏差。

本脚本对存量决策做幂等回填：
  · 有 entry_price            → exec_state='' （已建仓，不覆盖原因）
  · 方向单(BUY/SELL)且有价外原因 → not_traded（原因已在 no_trade_reason）
  · 方向单且无原因             → not_traded（原因补"未记录未成交原因"）
  · HOLD / 无方向              → hold
只补状态与空原因，不改 entry_price / exit_price / 盈亏等任何行情字段。

用法:
    python3 repair_exec_state.py            # dry-run，只打印待回填分布
    python3 repair_exec_state.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db  # noqa: E402


def _classify(action: str, asset: str, entry_price, reason: str) -> str:
    if entry_price is not None:
        return ""  # 已建仓
    act = (action or "HOLD").strip().upper()
    if act in ("BUY", "SELL") and asset not in ("", "NONE"):
        return "not_traded"
    return "hold"


def _apply_with_retry(conn, todo, retries: int = 12, wait_sec: float = 3.0) -> int:
    """SQLite 单写者：锁争用时退避重试，避免常驻 daemon 期间写不进去。"""
    import time
    last: Exception | None = None
    conn.execute("PRAGMA busy_timeout=30000")
    for attempt in range(1, retries + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for new_state, new_reason, did in todo:
                conn.execute(
                    "UPDATE ai_decisions SET exec_state=?, no_trade_reason=? WHERE id=?",
                    (new_state, new_reason, did),
                )
            conn.commit()
            return len(todo)
        except sqlite3.OperationalError as exc:
            last = exc
            conn.rollback()
            if attempt < retries:
                time.sleep(wait_sec * attempt)
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库；默认 dry-run")
    args = ap.parse_args()

    conn = sqlite3.connect(str(db.get_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")

    rows = conn.execute(
        """SELECT id, suggested_action, target_asset, entry_price,
                  COALESCE(no_trade_reason, '') AS reason,
                  COALESCE(exec_state, '') AS state
             FROM ai_decisions"""
    ).fetchall()

    todo: list[tuple] = []
    dist: dict[str, int] = {}
    for r in rows:
        state = (r["state"] or "").strip()
        reason = (r["reason"] or "").strip()
        new_state = _classify(r["suggested_action"], r["target_asset"] or "",
                              r["entry_price"], reason)
        # 已建仓的空状态不补原因；未建仓方向单若仍无原因，补一条占位说明
        new_reason = reason
        if new_state == "not_traded" and not new_reason:
            new_reason = "未记录未成交原因（历史回填）"
        # 空 state 视为待回填：即使 new_state 与空串同形（已执行="")，
        # 也要把空 state 落库成 ""，否则前端/统计仍把它当"没状态"。
        if state == new_state and state != "":
            continue
        todo.append((new_state, new_reason, r["id"]))
        dist[new_state or "executed"] = dist.get(new_state or "executed", 0) + 1

    print(f"扫描决策 {len(rows)} 条，待回填 {len(todo)} 条；分布: {dist}")
    if not todo:
        conn.close()
        return 0

    if not args.apply:
        print("(dry-run，未写库 — 加 --apply 生效)")
        conn.close()
        return 0

    n = _apply_with_retry(conn, todo)
    print(f"已回填 {n} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
