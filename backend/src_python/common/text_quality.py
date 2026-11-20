#!/usr/bin/env python3
"""文本质量判定 —— v396 P3：把"写得长"和"写得啰嗦"分开。

背景事故（2026-11-16 实测）
------------------------
`OVERANALYSIS` 惩罚按 **字数** 扣置信度：reasoning_path 超过 800 字 → confidence ×0.86。
全库 52 条决策因此被降级，其中 #11485 是全库唯一一条 BTC SELL，置信度 17.9 被压到
20 分闸门以下 → 变成 HOLD。

方向完全反了：**推演完整 = 证据充分**，不该被当成过度分析；
真正该罚的是"同一句话换三种说法反复讲"。

判定口径
--------
1. 长度本身不罚：只要结构完整（≥5 个段落标题）且重复率低，长就是好事。
2. 重复率 = 重复句子的字数占比。≥15% 才算"啰嗦"。
3. 罚分上限 25%（原来是 60%），且按重复率线性放大，不按字数线性放大。
"""
from __future__ import annotations

import re
from typing import Tuple

_SENT_SPLIT = re.compile(r"[。！？\n]+")
_HEADER = re.compile(r"[\[\【][^\]\】]{2,20}[\]\】]")


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s)


def duplicate_ratio(text: str) -> float:
    """重复句子的字数占比（0.0–1.0）。短句（<8 字）不计入，避免"是的""因此"误判。"""
    if not text:
        return 0.0
    parts = [_normalize(p) for p in _SENT_SPLIT.split(text)]
    parts = [p for p in parts if len(p) >= 8]
    if not parts:
        return 0.0
    total = sum(len(p) for p in parts)
    seen = set()
    dup_chars = 0
    for p in parts:
        if p in seen:
            dup_chars += len(p)
        else:
            seen.add(p)
    return dup_chars / total if total else 0.0


def overanalysis_penalty(reasoning_path: str, threshold: int = 1800,
                         base_pen: float = 10.0) -> Tuple[float, str]:
    """返回 (折扣系数 1.0–0.75, 说明)。1.0 表示不罚。

    Args:
        reasoning_path: 模型输出的八段推演全文
        threshold: 超过这个字数才进入评估（不是"必然受罚"）
        base_pen: 基础罚分百分点（默认 10 = 10%）

    Returns:
        (factor, note)。factor 直接乘到 confidence 上；note 为空表示不罚。
    """
    text = str(reasoning_path or "")
    n = len(text)
    if n <= max(0, int(threshold)):
        return 1.0, ""

    dup = duplicate_ratio(text)
    sections = len(_HEADER.findall(text))

    # 长 + 结构完整 + 不重复 = 认真推演，不罚。
    if dup < 0.15 and sections >= 5:
        return 1.0, ""

    # 罚分只看重复率（上限 25%），不看超了多少字。
    ratio = min(0.25, max(0.0, float(base_pen) / 100.0 + dup * 0.5))
    note = (f"啰嗦惩罚：{n}字 / 重复率{dup * 100:.0f}% / 段落{sections} "
            f"→ confidence ×{1.0 - ratio:.2f}")
    return 1.0 - ratio, note
