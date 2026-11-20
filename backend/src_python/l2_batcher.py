#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""l2_batcher —— 门卫 L2 二分类的微批合并（把 N 次调用压成 1 次）。

为什么要它：
    门卫 L2 每次只判「这条新闻对宏观/加密交易者是否可操作」，输出一个 0/1。
    每判一条就要发一次请求，单次平均 10.8s —— 产能的天花板不在模型，在"一条一请求"。
    实测到货 267 条/小时、门卫约 220 条/小时，缺口约 18%，队列每小时涨 47 条。

怎么做：
    1. 调用方把 (title, content) 丢进队列，拿一个占位句柄，等结果。
    2. 后台线程攒批：最多 L2_BATCH_MAX 条（默认 8），或等 L2_BATCH_WINDOW_MS（默认 250ms）。
    3. 一次请求带 N 条编号标题，要求回 "1,0,1,..."。
    4. 解析成功 → 按顺序分发；条数对不上或解析失败 → **整批退回单条调用**。
       宁可多花一次钱，也不让噪音因为批处理出错而涌进主分析（那是配额黑洞）。

开关（默认开启，可一键退回旧行为）：
    L2_BATCH_ENABLED=0        关闭，回到一条一请求
    L2_BATCH_MAX=8            每批上限
    L2_BATCH_WINDOW_MS=250    等批窗口（等越久批越大、单条延迟越高）
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

_MAX = int(os.getenv("L2_BATCH_MAX", "8"))
_WINDOW_MS = int(os.getenv("L2_BATCH_WINDOW_MS", "250"))
_ENABLED = os.getenv("L2_BATCH_ENABLED", "1") not in ("0", "false", "False")

BATCH_SYSTEM = (
    "You are a financial news filter. "
    "For EACH numbered headline, decide: 1 = actionable for a macro/crypto trader, 0 = noise. "
    "Reply with ONLY the digits separated by commas, in the same order as the input. "
    "No words, no explanation."
)


class _Req:
    __slots__ = ("title", "content", "event", "result", "seq")

    def __init__(self, title: str, content: str, seq: int) -> None:
        self.title = title
        self.content = content
        self.event = threading.Event()
        self.result: Optional[int] = None
        self.seq = seq


_queue: List[_Req] = []
_qlock = threading.Lock()
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_seq_counter = 0

# 单条调用器（由 realtime_filter 注入，避免循环 import）
single_caller: Optional[Callable[[str, str], Optional[int]]] = None
# 批量调用器：(系统提示, 用户文本) -> 原始文本
batch_caller: Optional[Callable[[str, str], Optional[str]]] = None


def configure(single: Callable[[str, str], Optional[int]],
              batch: Callable[[str, str], Optional[str]]) -> None:
    """注入真正的 LLM 调用器。未注入时本模块整体退化为单条直调（不攒批）。"""
    global single_caller, batch_caller
    single_caller = single
    batch_caller = batch


def parse_digits(raw: str, expect: int) -> Optional[List[int]]:
    """从模型输出里取 expect 个 0/1。取不到或数量不符 → None（触发退回单条）。"""
    if not raw:
        return None
    # 剥掉推理模型偶发的 <think> 块（未闭合视为被截断，答案必然丢失）
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>")[0]
    hits = re.findall(r"[01]", cleaned)
    if len(hits) < expect:
        return None
    return [int(x) for x in hits[:expect]]


def _build_user(reqs: List[_Req]) -> str:
    lines = []
    for i, r in enumerate(reqs, 1):
        t = r.title.strip()[:200]
        body = (r.content or "").strip()[:120]
        lines.append(f"{i}. {t}" + (f"\n   摘要: {body}" if body else ""))
    return "\n".join(lines)


def _flush(reqs: List[_Req]) -> None:
    """真正执行一批。任何异常都必须落到单条兜底，不能把结果吞掉。"""
    got: Optional[List[int]] = None
    if batch_caller is not None and len(reqs) > 1:
        try:
            raw = batch_caller(BATCH_SYSTEM, _build_user(reqs))
            got = parse_digits(raw or "", len(reqs))
        except Exception:
            got = None

    if got is not None and len(got) == len(reqs):
        for r, v in zip(reqs, got):
            r.result = v
        return

    # 退回单条：批处理只在"省钱"上有价值，不能拿判定质量换
    for r in reqs:
        try:
            r.result = single_caller(r.title, r.content) if single_caller else None
        except Exception:
            r.result = None


def _worker_loop() -> None:
    while True:
        time.sleep(_WINDOW_MS / 1000.0)
        with _qlock:
            if not _queue:
                continue
            batch = _queue[:_MAX]
            del _queue[:len(batch)]
        try:
            _flush(batch)
        finally:
            for r in batch:
                r.event.set()


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="l2-batcher", daemon=True)
        _worker.start()


def classify(title: str, content: str = "", timeout_s: float = 4.0) -> Optional[int]:
    """对外唯一入口。未启用 / 未注入 / 超时 → 直接单条调用（语义不变）。"""
    if not _ENABLED or batch_caller is None:
        return single_caller(title, content) if single_caller else None

    global _seq_counter
    _ensure_worker()
    with _qlock:
        req = _Req(title, content, _seq_counter)
        _seq_counter += 1
        _queue.append(req)

    if not req.event.wait(timeout_s):
        # 超时：从队列里摘掉（可能已被取走，忽略异常），单条兜底
        with _qlock:
            try:
                _queue.remove(req)
            except ValueError:
                pass
        return single_caller(title, content) if single_caller else None
    return req.result
