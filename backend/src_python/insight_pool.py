#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
insight_pool.py — v398: 洞察样本池口径（降级决策不进洞察 / 技术错误不进错因分母）
================================================================================

为什么需要这个模块
------------------
2026-11-16 实测，两个口径问题让"细节洞察"的分数不可读：

1. **降级决策污染洞察池**
   494 条决策里 442 条（89.5%）`analysis_degraded=1`；100 条带反思的决策里
   84 条来自降级路径。这些行的内容是"没有真分析"，不是"分析错了" ——
   拿它们当洞察样本，等于用"没看见"去证明"看得不准"。

2. **技术故障与技术错因共用分布表**
   同一天 499 条 `C/data_feed_timeout` 反射由一次抓取失败（11-15 23:22）
   批量写入，占全部反思 82.5%，把 S10 细分集中度顶到 88.2%。
   技术故障应该单独计数，不该参与"错因细分"的分布。

口径定义（单一权威，写入端与记分卡共用）
----------------------------------------
- 洞察池 = `analysis_degraded != 1` 的决策；降级决策只计入"降级率"，不写反思。
- 错因分布池 = dimension ∈ {A, B} 的反思；C 类（技术错误）单独列出但不进分母。

设计约束：零外部依赖（只用 sqlite3 类型注解），可被 pytest 直接 import；
缺列 / 缺表一律降级为"不拦"，不让旧库或极简测试表整体失效。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Optional

# 技术错误维度：与"交易错因"区分，不进 S10 分母
TECHNICAL_DIMENSION = "C"
BUSINESS_DIMENSIONS = ("A", "B")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return column in cols


def is_degraded_decision(conn: sqlite3.Connection, decision_id: Optional[int]) -> bool:
    """该决策是否走在降级路径（analysis_degraded=1）。

    保守语义：缺列、缺表、查不到 id 一律返回 False（照常写入），
    避免因为口径模块不认识旧库而悄悄停掉整条反思链。
    """
    if not decision_id:
        return False
    if not _has_column(conn, "ai_decisions", "analysis_degraded"):
        return False
    try:
        row = conn.execute(
            "SELECT COALESCE(analysis_degraded, 0) FROM ai_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    try:
        return int(row[0] or 0) == 1
    except (TypeError, ValueError):
        return False


def reflection_pool_stats(conn: sqlite3.Connection) -> Dict[str, object]:
    """错因分布池的构成，用于把"排除了多少"显式写进报告。

    返回:
      business_n     A+B 类反思行数（S10 的分母）
      technical_n    C 类技术错误行数（被排除）
      other_n        维度为空或非 A/B/C 的行数（同样不计入分母）
      top_sub        最大小类名
      top_n          最大小类行数
      top_share      最大小类占 business_n 的比例（%）
    """
    empty = {"business_n": 0, "technical_n": 0, "other_n": 0,
             "top_sub": "", "top_n": 0, "top_share": 0.0}
    try:
        rows = conn.execute(
            "SELECT COALESCE(dimension, ''), COALESCE(subcategory, ''), COUNT(*) "
            "FROM trade_reflections_v2 GROUP BY 1, 2"
        ).fetchall()
    except sqlite3.Error:
        return empty

    business = 0
    technical = 0
    other = 0
    by_sub: Dict[str, int] = {}
    for dim, sub, cnt in rows:
        if dim in BUSINESS_DIMENSIONS and sub:
            business += int(cnt)
            by_sub[sub] = by_sub.get(sub, 0) + int(cnt)
        elif dim == TECHNICAL_DIMENSION:
            technical += int(cnt)
        else:
            other += int(cnt)

    top_sub, top_n = "", 0
    if by_sub:
        top_sub, top_n = max(by_sub.items(), key=lambda kv: kv[1])
    share = (top_n / business * 100.0) if business else 0.0
    return {"business_n": business, "technical_n": technical, "other_n": other,
            "top_sub": top_sub, "top_n": top_n, "top_share": round(share, 1)}


def s10_detail(stats: Dict[str, object]) -> str:
    """S10 的 detail 文案：把被排除的技术错误条数明确写出来。"""
    if not stats or not stats.get("business_n"):
        return (f"A/B 两类反思为空（排除 C 类技术错误 "
                f"{int(stats.get('technical_n') or 0) if stats else 0} 条）")
    return (f"分母 = A/B 交易错因 {stats['business_n']} 条；"
            f"排除 C 类技术错误 {stats['technical_n']} 条、无维度 {stats['other_n']} 条；"
            f"最大类 {stats['top_sub'] or '—'} {stats['top_n']} 条")
