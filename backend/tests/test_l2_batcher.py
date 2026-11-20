#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门卫 L2 微批的三条契约测试。

重点不是"批处理更快"，而是**批处理出错时不会污染判定**：
    · 解析成功 → 按顺序分发，一条都不能错位
    · 解析失败 / 条数不符 → 整批退回单条，结果必须仍然完整
    · 关闭开关 → 完全等同旧行为（一条一调用）
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src_python"))

import l2_batcher


def _digits_by_title(user_text, table):
    """按提示词里每条标题的语义回 0/1，而不是按固定位次回。

    为什么这样写：三个线程是并发入队的，**谁先入队不保证**，所以「第几条」
    和「哪个标题」不一定对齐。要验的是「解析结果按入队顺序分发回各自的请求」，
    那就让假模型按标题语义作答，最后每个请求拿到的值仍必须是自己那一条的。
    提示词格式见 l2_batcher._build_user：`1. 标题` / `2. 标题`。
    """
    import re
    out = []
    for line in user_text.split("\n"):
        m = re.match(r"\s*\d+\.\s*(.*)$", line)
        if not m:
            continue
        title = m.group(1).strip()
        out.append(str(table.get(title, 1)))
    return ",".join(out)


def _run_three(configure_fn):
    """并发发起 3 条，返回 (结果列表, 批调用次数, 单条调用次数)。

    攒批是按时间窗切的（默认 250ms），三个线程如果谁晚到一点就会被切成两批：
    第一批 2 条、第二批 1 条，而 configure_fn 对 2 条也照样回 3 个数字 →
    条数不符 → 整批退回单条 → 结果变成 [1,1,1]。这不是产品 bug，是测试自己的竞态。
    所以这里做两件事：① 三条用 Barrier 同时入队；② 临时把窗口放宽到 600ms。
    """
    calls = {"batch": 0, "single": 0}

    def single(title, content=""):
        calls["single"] += 1
        return 1

    def batch(system, user):
        calls["batch"] += 1
        return configure_fn(user)

    l2_batcher.configure(single=single, batch=batch)
    results = [None] * 3
    threads = []
    barrier = threading.Barrier(3, timeout=8)
    old_window = l2_batcher._WINDOW_MS
    l2_batcher._WINDOW_MS = 600

    def worker(i, title):
        barrier.wait()
        results[i] = l2_batcher.classify(title, "")

    try:
        for i, t in enumerate(["美联储降息", "某交易所送USDT", "非农数据超预期"]):
            th = threading.Thread(target=worker, args=(i, t))
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=8)
    finally:
        l2_batcher._WINDOW_MS = old_window
    return results, calls


def test_batch_distributes_in_order():
    # 假模型按标题语义作答（谁先入队都成立），结果仍必须各自归位
    table = {"美联储降息": 1, "某交易所送USDT": 0, "非农数据超预期": 1}
    results, calls = _run_three(lambda user: _digits_by_title(user, table))
    assert results == [1, 0, 1], results
    assert calls["batch"] == 1, calls      # 三次调用压成一次
    assert calls["single"] == 0, calls


def test_parse_failure_falls_back_to_single():
    # 模型回了无法解析的内容 → 整批退回单条，结果仍然必须完整
    results, calls = _run_three(lambda user: "<think>思考中")
    assert results == [1, 1, 1], results
    assert calls["single"] == 3, calls


def test_wrong_count_falls_back():
    # 3 条只回了 2 个判定 → 不许错位复用，退回单条
    results, calls = _run_three(lambda user: "1,0")
    assert results == [1, 1, 1], results
    assert calls["single"] == 3, calls


def test_disabled_matches_old_behavior(monkeypatch):
    monkeypatch.setenv("L2_BATCH_ENABLED", "0")
    import importlib
    importlib.reload(l2_batcher)
    calls = {"single": 0}

    def single(title, content=""):
        calls["single"] += 1
        return 0

    l2_batcher.configure(single=single, batch=None)
    assert l2_batcher.classify("任意标题") == 0
    assert calls["single"] == 1

    # 关键：reload 读到的是模块级常量，env 撤掉后不重新 reload 会把"关闭态"
    # 留给后续用例（实测导致 test_batch_distributes_in_order 拿到 [1,0,1]）。
    monkeypatch.delenv("L2_BATCH_ENABLED", raising=False)
    importlib.reload(l2_batcher)


if __name__ == "__main__":
    test_batch_distributes_in_order()
    test_parse_failure_falls_back_to_single()
    test_wrong_count_falls_back()
    print("3 条契约测试通过")
