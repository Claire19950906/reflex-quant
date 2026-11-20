#!/usr/bin/env python3
"""v434 回归：门卫标题指纹缓存（转发/回放去重）。

守三件事（来自 2026-11-17 实测：同一条消息被 jin10 / feishu_jin10 / 东方财富
三个源同时抓到，标题只差 `[hash:xxx]` 前缀和标点 → 同一条新闻付 2–3 次门卫）：
1. 指纹抹平 hash 前缀 / 标点 / 空白 / 大小写 / 全半角；
2. 太短的标题不缓存（"非农" 这种会误命中别的新闻）；
3. 缓存返回**副本**（调用方改 dict 不能污染缓存），关闭开关时整体失效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import realtime_filter as rf


def test_fp_same_across_reposts(monkeypatch):
    monkeypatch.delenv("GATE_FP_CACHE", raising=False)
    a = "[hash:6c518b4c0e99ac7b] 美联储暗示9月降息"
    b = "美联储暗示９月降息。"          # 全角数字 + 句号 + 无 hash 前缀
    c = "  美联储 暗示9月降息  "         # 空格
    fp_a, fp_b, fp_c = (rf._gate_title_fp(x) for x in (a, b, c))
    assert fp_a and fp_a == fp_b == fp_c


def test_fp_differs_for_different_news(monkeypatch):
    monkeypatch.delenv("GATE_FP_CACHE", raising=False)
    assert (rf._gate_title_fp("美联储暗示9月降息")
            != rf._gate_title_fp("美联储暗示9月加息"))


def test_fp_short_title_not_cached(monkeypatch):
    monkeypatch.delenv("GATE_FP_CACHE", raising=False)
    assert rf._gate_title_fp("非农") is None


def test_fp_disabled_by_env(monkeypatch):
    monkeypatch.setenv("GATE_FP_CACHE", "0")
    assert rf._gate_title_fp("美联储暗示9月降息") is None


def test_fp_cache_returns_copy(monkeypatch):
    monkeypatch.delenv("GATE_FP_CACHE", raising=False)
    monkeypatch.setenv("GATE_CACHE_TTL_S", "600")
    fp = rf._gate_title_fp("美联储暗示9月降息")
    gate = {"verdict": "可交易", "study": [1, 2]}
    rf._gate_fp_put(fp, gate)
    got = rf._gate_fp_get(fp)
    assert got == gate
    got["verdict"] = "排除"
    got["study"].append(3)
    again = rf._gate_fp_get(fp)
    assert again["verdict"] == "可交易" and again["study"] == [1, 2]


def test_fp_cache_miss_when_absent(monkeypatch):
    monkeypatch.delenv("GATE_FP_CACHE", raising=False)
    assert rf._gate_fp_get(rf._gate_title_fp("一条从未判定过的新闻标题")) is None


# ─────────────────── 通道故障期免重试 ───────────────────

def test_net_fail_streak_opens_skip_window(monkeypatch):
    monkeypatch.setenv("GATE_RETRY_SKIP_AFTER", "3")
    monkeypatch.setenv("GATE_RETRY_SKIP_S", "300")
    rf._gate_net_ok()
    assert rf._gate_retry_worthwhile() is True
    rf._gate_net_fail()
    rf._gate_net_fail()
    assert rf._gate_retry_worthwhile() is True, "没到阈值还应该重试"
    rf._gate_net_fail()
    assert rf._gate_retry_worthwhile() is False, "连续 3 条无返回 → 进入免重试窗口"


def test_net_recovery_resets_window(monkeypatch):
    monkeypatch.setenv("GATE_RETRY_SKIP_AFTER", "3")
    rf._gate_net_ok()
    for _ in range(3):
        rf._gate_net_fail()
    assert rf._gate_retry_worthwhile() is False
    rf._gate_net_ok()
    assert rf._gate_retry_worthwhile() is True
