# -*- coding: utf-8 -*-
"""attribution_actionable.py — 把 AI 归因落成"可执行结论"标志 (需求 #1 / G10)。

背景
----
abnormal_attributions.actionable 列 97% 是"观望" —— 页面看上去像"AI 什么也没说",
但 AI 其实在 drivers 里已经把驱动写清楚了 (例: "量能Z异常为负并伴随MA5/MA20死叉,
表明缩量下跌与短期趋势转弱共振"), 只是没落成一条**可执行**的结论。
结果: 归因面板只能当"事后解释"看, 不能当"现在怎么办"用。

做法
----
  1. 新增三列: is_actionable (0/1), action_text (可执行结论), action_source (来源);
  2. 结论本身是具体动作 (顺势/减仓/对冲/加仓/止损…) → 直接算可执行;
  3. 结论是"观望"、但存在**非推测性**的 medium/high 驱动 → 从驱动里推出一个带
     触发条件的动作 (标记 action_source='derived_from_drivers', 不冒充 AI 原话);
  4. 其余 (全是推测/低置信) → is_actionable=0, 保持"观望"。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# 明确的可执行动作词
CONCRETE_ACTIONS = (
    "顺势", "减仓", "加仓", "对冲", "入场", "离场", "止损", "止盈", "买入", "卖出",
    "做多", "做空", "开仓", "平仓", "加杠杆", "降杠杆", "观望后", "逢低", "逢高",
    "突破跟", "破位跟", "跟随", "布局",
)
# 明确的"不给动作"词
NO_ACTION = ("观望", "观察", "等待", "不动", "无", "暂不", "待确认", "-")

BULL_WORDS = ("利好", "上涨", "走强", "上行", "买盘", "拉升", "提振", "反弹", "偏多",
              "突破", "流入", "净买入", "利多", "看多", "上涨")
BEAR_WORDS = ("利空", "下跌", "走弱", "下行", "卖压", "承压", "回落", "杀跌", "偏空",
              "破位", "流出", "净卖出", "利多", "看空", "抛压")

CONFIDENT = ("high", "medium", "高", "中")


def _is_concrete(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if any(w in t for w in NO_ACTION):
        # "观望"里可能带条件 (如"观望突破后顺势"), 这类仍算可执行
        return any(w in t for w in CONCRETE_ACTIONS)
    return any(w in t for w in CONCRETE_ACTIONS)


def _direction_of(text: str) -> str:
    t = text or ""
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    if bull > bear:
        return "偏多"
    if bear > bull:
        return "偏空"
    return "方向待定"


def _drivers_of(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [d for d in raw if isinstance(d, dict)]
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _confidence_rank(conf: str) -> int:
    c = str(conf or "").strip().lower()
    if c in ("high", "高"):
        return 3
    if c in ("medium", "中"):
        return 2
    if c in ("low", "低"):
        return 1
    return 0


def derive_action(conclusion: str, drivers_raw: Any,
                  severity_hint: Any = None) -> Dict[str, Any]:
    """从 AI 归因里推出"是否可执行 + 可执行结论"。"""
    conclusion = (conclusion or "").strip()
    drivers = _drivers_of(drivers_raw)
    if _is_concrete(conclusion):
        return {
            "is_actionable": 1,
            "action_text": conclusion[:160],
            "action_source": "ai_conclusion",
        }
    solid = [d for d in drivers
             if _confidence_rank(d.get("confidence")) >= 2 and not bool(d.get("speculative"))]
    if not solid:
        return {
            "is_actionable": 0,
            "action_text": (conclusion or "证据不足, 观望")[:160],
            "action_source": "insufficient_evidence",
        }
    solid.sort(key=lambda d: _confidence_rank(d.get("confidence")), reverse=True)
    cause = str(solid[0].get("cause") or "").strip()
    src = str(solid[0].get("source") or "").strip()
    direction = _direction_of(cause)
    cond = cause[:44].rstrip("。,，;；")
    if direction == "偏多":
        head = "顺势偏多"
    elif direction == "偏空":
        head = "顺势偏空"
    else:
        head = "确认后跟随"
    text = "%s（触发条件: %s%s）" % (head, cond, ("；依据: " + src) if src else "")
    return {
        "is_actionable": 1,
        "action_text": text[:160],
        "action_source": "derived_from_drivers",
    }


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(abnormal_attributions)").fetchall()}
    for name, decl in (("is_actionable", "INTEGER"), ("action_text", "TEXT"),
                       ("action_source", "TEXT")):
        if name not in cols:
            conn.execute("ALTER TABLE abnormal_attributions ADD COLUMN %s %s" % (name, decl))
    conn.commit()


def backfill(conn: sqlite3.Connection, batch: int = 2000) -> Dict[str, int]:
    """给所有归因行补 is_actionable / action_text / action_source (幂等)。"""
    ensure_columns(conn)
    total = conn.execute("SELECT COUNT(*) FROM abnormal_attributions").fetchone()[0]
    done = 0
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, actionable, drivers_json, severity_hint FROM abnormal_attributions "
            "WHERE id > ? ORDER BY id LIMIT ?", (last_id, batch)
        ).fetchall()
        if not rows:
            break
        for r in rows:
            res = derive_action(r[1], r[2], r[3])
            conn.execute(
                "UPDATE abnormal_attributions SET is_actionable=?, action_text=?, action_source=? "
                "WHERE id=?",
                (res["is_actionable"], res["action_text"], res["action_source"], int(r[0])),
            )
            last_id = int(r[0])
            done += 1
        conn.commit()
    n_act = conn.execute(
        "SELECT COUNT(*) FROM abnormal_attributions WHERE is_actionable=1").fetchone()[0]
    n_src = {
        r[0]: r[1] for r in conn.execute(
            "SELECT action_source, COUNT(*) FROM abnormal_attributions GROUP BY 1").fetchall()
    }
    return {"rows": total, "processed": done, "actionable": n_act, "by_source": n_src}


__all__ = [
    "CONCRETE_ACTIONS", "NO_ACTION", "derive_action", "ensure_columns", "backfill",
]
