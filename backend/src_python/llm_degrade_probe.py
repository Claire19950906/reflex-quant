#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_degrade_probe — 把"降级"拆成可量的几类（v342）。

为什么要这个
------------
实测 272 条决策里，`sentiment_score=0` 占 89%、BUY 只有 2 条。看上去像
"模型判断无方向"，但读 reasoning_path 才发现大多数是**降级兜底**：

    模型没给出可用输出 → 服务端按模板补八段 → 分值自然是 0

问题在于：兜底有几条不同路径，之前它们长得都不一样却都没被计数，
所以"到底哪一步坏的"一直只能猜。本模块把兜底结果分成 5 类并落表，
让"猜"变成"读数字"。

5 类（按 reasoning_path 特征串判，不看分数）
--------------------------------------------
    full_degrade_in_scope    完整降级 · 核心品种（"LLM 未返回可用分析"）
    full_degrade_out_scope   完整降级 · 范围外（"**不在**核心监控清单"）
    reasoning_summary_only   只拿到 reasoning 摘要（"[驱动力] LLM 真东西摘要"）
    short_summary            只有简短摘要（"[LLM 摘要]"）
    ok                       真实八段推演

只读归类，不改判定；`record` 在写决策的同一事务里落一行，失败绝不影响主流程。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Optional

ARCH_FULL_IN = "full_degrade_in_scope"
ARCH_FULL_OUT = "full_degrade_out_scope"
ARCH_RS_SUMMARY = "reasoning_summary_only"
ARCH_SHORT = "short_summary"
ARCH_OK = "ok"

ARCHETYPES = (ARCH_FULL_IN, ARCH_FULL_OUT, ARCH_RS_SUMMARY, ARCH_SHORT, ARCH_OK)

# 特征串（顺序敏感：先判降级，再判摘要，最后 ok）
_MARKERS = (
    (ARCH_FULL_IN, "LLM 未返回可用分析"),
    (ARCH_FULL_OUT, "**不在**核心监控清单"),
    (ARCH_FULL_OUT, "不在核心监控清单（BTC/XAU/WTI/ETH/SOL）"),
    (ARCH_RS_SUMMARY, "LLM 真东西摘要"),
    (ARCH_SHORT, "[LLM 摘要]"),
)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS llm_degrade_probe (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id   INTEGER,
    asset     TEXT,
    archetype TEXT NOT NULL,
    raw_len   INTEGER,
    raw_head  TEXT,
    path_len  INTEGER,
    rsn_len   INTEGER,
    action    TEXT,
    score     REAL,
    ts        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ldp_ts ON llm_degrade_probe(ts, archetype);
"""


# 主 worker 是单线程顺序处理，用模块级"最后一次原始输出"即可把 raw_len / raw_head
# 带到落库那一侧（_presanitize_for_schema 手里只有文本、没有 conn）。
_LAST: Dict[str, Any] = {"raw_len": 0, "raw_head": "",
                         "empty_retry_failed": 0, "empty_retry_ok": 0}


def note_retry_failed(err: str = "") -> None:
    """空输出重试也失败 —— 记一次（用于判断是"模型稳定不返回"还是偶发）。"""
    _LAST["empty_retry_failed"] = int(_LAST.get("empty_retry_failed") or 0) + 1


def note_retry_ok() -> None:
    _LAST["empty_retry_ok"] = int(_LAST.get("empty_retry_ok") or 0) + 1


def note_raw(cleaned: str) -> None:
    """在 presanitize 入口登记"这次的 LLM 原始输出"长什么样。

    为什么需要它：降级只能说明"没拿到可用输出"，但**空输出**（模型真没回）、
    **think 未闭合被剥光**、**JSON 被截断** 三种情况的处置完全不同。
    留 80 字开头 + 长度，就能事后区分，而不用重跑 LLM 或开 dump。
    """
    try:
        t = str(cleaned or "")
        _LAST["raw_len"] = len(t)
        _LAST["raw_head"] = t[:80]
    except Exception:
        pass


def last_raw() -> Dict[str, Any]:
    return dict(_LAST)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_SQL)


def classify_path(path: str) -> str:
    """只看 reasoning_path 判兜底类型（纯函数，便于单测与历史回判）。"""
    p = str(path or "")
    for arch, marker in _MARKERS:
        if marker in p:
            return arch
    return ARCH_OK


def classify_result(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ARCH_OK
    return classify_path(str(result.get("reasoning_path") or ""))


_SCHEMA_READY = {"done": False}


def record(conn: sqlite3.Connection, *, news_id: Optional[int], asset: str,
           result: Dict[str, Any], raw_len: int = 0,
           commit: bool = False) -> str:
    """落一行探针（幂等性按 news_id+ts 自然去重不重要，这里只做统计）。

    实测坑：探针表从未被建过 —— init_schema 没人调用，record 又把 sqlite3.Error
    静默吞掉，于是"记录降级"这件事一直在空转（表不存在，一行没落）。
    改成首次调用时自愈建表，避免依赖某个启动路径有没有记得调它。
    """
    arch = classify_result(result)
    if not _SCHEMA_READY["done"]:
        try:
            init_schema(conn)
            _SCHEMA_READY["done"] = True
        except sqlite3.Error:
            pass
    path = str((result or {}).get("reasoning_path") or "")
    rsn = str((result or {}).get("reasoning") or "")
    score = (result or {}).get("sentiment_score")
    try:
        conn.execute(
            "INSERT INTO llm_degrade_probe (news_id, asset, archetype, raw_len, raw_head, "
            "path_len, rsn_len, action, score, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(news_id) if news_id is not None else None,
             str(asset or "").upper(), arch,
             int(raw_len or 0) or int(_LAST.get("raw_len") or 0),
             str(_LAST.get("raw_head") or "")[:80],
             len(path), len(rsn),
             str((result or {}).get("suggested_action") or "").upper(),
             float(score) if isinstance(score, (int, float)) else None,
             int(time.time())))
    except sqlite3.Error as exc:
        # v399：允许失败，但不许不留痕（v322 审计门禁要求）
        print(f"[DEGRADE-PROBE] 探针归档写库失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    if commit:
        conn.commit()
    return arch


def stats(conn: sqlite3.Connection, days: int = 30) -> Dict[str, Any]:
    """探针表的历史分布（只统计写过的行）。"""
    cutoff = int(time.time()) - int(days) * 86400
    try:
        rows = conn.execute(
            "SELECT archetype, COUNT(*) n, AVG(raw_len) avg_raw, AVG(path_len) avg_path, "
            "       SUM(CASE WHEN action='HOLD' THEN 1 ELSE 0 END) holds "
            "FROM llm_degrade_probe WHERE ts>=? GROUP BY archetype",
            (cutoff,)).fetchall()
    except sqlite3.Error:
        return {"ok": True, "days": days, "by_archetype": {}, "note": "探针表不存在"}
    total = sum(int(r[1]) for r in rows)
    return {"ok": True, "days": days, "total": total,
            "by_archetype": {r[0]: {"n": int(r[1]),
                                    "avg_raw_len": round(float(r[2] or 0), 1),
                                    "avg_path_len": round(float(r[3] or 0), 1),
                                    "holds": int(r[4] or 0)} for r in rows},
            "raw_samples": _raw_samples(conn, cutoff)}


def _raw_samples(conn: sqlite3.Connection, cutoff: int) -> Dict[str, Any]:
    """每类兜底给一条原始输出开头 —— 一眼分清空输出 / think / 截断。"""
    out: Dict[str, Any] = {}
    try:
        for arch in ARCHETYPES:
            row = conn.execute(
                "SELECT raw_len, raw_head FROM llm_degrade_probe "
                "WHERE ts>=? AND archetype=? ORDER BY id DESC LIMIT 1",
                (cutoff, arch)).fetchone()
            if row:
                out[arch] = {"raw_len": int(row[0] or 0), "raw_head": row[1] or ""}
    except sqlite3.Error:
        pass
    return out


def classify_existing(conn: sqlite3.Connection) -> Dict[str, int]:
    """对历史 ai_decisions 行做同样的归类（只读，用于立即可用的基线）。"""
    out: Dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT reasoning_path FROM ai_decisions").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        arch = classify_path(r[0] if isinstance(r[0], (str, type(None)))
                             else r[0])
        out[arch] = out.get(arch, 0) + 1
    return out
