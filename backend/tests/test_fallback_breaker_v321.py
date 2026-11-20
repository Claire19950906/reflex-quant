# -*- coding: utf-8 -*-
"""v321: OpenRouter 兜底通道的「开关一致性 + 熔断」回归单测。

事故背景（2026-11-04 生产实测）：
  · .env 里的 OPENROUTER_API_KEY 已失效 → 任何请求返回
    HTTP 401 {"error":{"message":"User not found.","code":401}}。
  · config.py 早已把 OPENROUTER_ENABLED 默认设为 0（主分析池据此停用），
    但 realtime_filter 的兜底路径**只看 key 非空**，无视这个开关。
  · urllib 的 timeout 是每次 socket 操作的上限、不是总耗时上限：
    实测一次 401 要 27.9s。而门卫/L2 的兜底是同步阻塞跑在**入库主链路**上的
    → 「MiniMax 池全挂 + OpenRouter key 死」= 每条新闻白等 27 秒
    → 整条 ingest 管道被拖成爬行（2026-11-04 02:36~03:17 事故的放大器）。

v321 的三条不变量（本文件逐条锁死）：
  1. 开关一致性：OPENROUTER_ENABLED=0 时，兜底**完全不发请求**。
  2. 熔断：401/403（key 永久失效）→ 冷却期内兜底直接跳过；429 → 短冷却。
  3. L2 不得因兜底不可用而整层失效 —— 池子是主路径，必须照跑。
"""

from __future__ import annotations

import io
import os
import sys
import urllib.error
from pathlib import Path

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import config
import realtime_filter as rt


@pytest.fixture(autouse=True)
def _clean_breaker():
    """每个用例前后清掉 OpenRouter 熔断状态，避免用例间互相污染。"""
    rt._OR_BREAK_UNTIL = 0.0
    rt._OR_BREAK_REASON = ""
    yield
    rt._OR_BREAK_UNTIL = 0.0
    rt._OR_BREAK_REASON = ""


# ─────────────────── 不变量 1：开关一致性 ───────────────────

def test_fallback_skipped_when_switch_off(monkeypatch):
    """OPENROUTER_ENABLED=0 → 兜底不发请求（哪怕 key 还在 .env 里）。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-dead-key")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", False)

    assert rt._openrouter_ready() is False


def test_fallback_allowed_when_switch_on(monkeypatch):
    """OPENROUTER_ENABLED=1 + key 非空 + 未熔断 → 兜底可用。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-live-key")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)

    assert rt._openrouter_ready() is True


def test_fallback_skipped_when_key_missing(monkeypatch):
    """key 为空 → 兜底不可用（无论开关状态）。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)

    assert rt._openrouter_ready() is False


def test_l3_never_calls_openrouter_when_switch_off(monkeypatch):
    """门卫池子全挂 + 开关关闭 → 不发兜底请求，直接降级返回 None。"""
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", False)
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-dead-key")
    monkeypatch.setattr(rt, "_call_pool_sync", lambda *a, **k: None)

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return None

    monkeypatch.setattr(rt, "_post_openrouter", _boom)

    assert rt._l3_semantic_gate("美联储暗示降息", "body") is None
    assert called["n"] == 0, "开关关闭时兜底不应发出任何请求"


# ─────────────────── 不变量 2：熔断 ───────────────────

def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/chat/completions",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"User not found.","code":401}}'),
    )


def test_breaker_trips_on_401(monkeypatch):
    """401 → 熔断打开，后续兜底直接跳过（不再每条白等 27 秒）。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-dead-key")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)

    class _Opener:
        def open(self, req, timeout=None):
            raise _http_error(401)

    monkeypatch.setattr(rt.urllib.request, "build_opener", lambda *a, **k: _Opener())

    assert rt._post_openrouter({"model": "x", "messages": []}, 1.0) is None
    assert rt._or_breaker_reason(), "401 必须打开熔断"
    assert rt._openrouter_ready() is False, "熔断期内兜底必须直接跳过"


def test_breaker_trips_on_429_with_short_cooldown(monkeypatch):
    """429 → 也熔断，但冷却比 401 短（配额可能很快恢复）。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-key")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)
    monkeypatch.setattr(rt, "_OR_BREAK_QUOTA_COOLDOWN_S", 60.0)

    class _Opener:
        def open(self, req, timeout=None):
            raise _http_error(429)

    monkeypatch.setattr(rt.urllib.request, "build_opener", lambda *a, **k: _Opener())

    assert rt._post_openrouter({"model": "x", "messages": []}, 1.0) is None
    assert "429" in rt._or_breaker_reason()


def test_no_breaker_on_network_error(monkeypatch):
    """网络抖动不熔断（瞬时故障不该永久禁用兜底）。"""
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-key")
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(rt.urllib.request, "build_opener", lambda *a, **k: _Opener())

    assert rt._post_openrouter({"model": "x", "messages": []}, 1.0) is None
    assert rt._or_breaker_reason() == "", "网络故障不应打开熔断"
    assert rt._openrouter_ready() is True


# ─────────────────── 不变量 3：L2 不因兜底失效而整层挂掉 ───────────────────

def test_l2_still_uses_pool_when_openrouter_disabled(monkeypatch):
    """OpenRouter 不可用时，L2 仍要走池子 —— 不能整层返回 None。

    回归点：v321 之前 L2 顶部有 `if not _OPENROUTER_KEY: return None` 早退，
    位置在池子调用**之前**；若把它改成检查 OPENROUTER_ENABLED，
    生产（开关=0）会让 L2 彻底失效 → 所有噪音放行给主分析 → 烧光配额。
    """
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", False)
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-dead-key")

    seen = {"n": 0}

    def _pool_ok(*a, **k):
        seen["n"] += 1
        return "1"

    monkeypatch.setattr(rt, "_call_pool_sync", _pool_ok)

    assert rt._l2_classify_haiku("美联储暗示降息", "body") == 1
    assert seen["n"] == 1, "OpenRouter 不可用时，L2 仍必须调用池子"


def test_l2_returns_none_when_pool_fails_and_fallback_disabled(monkeypatch):
    """池子也挂 + 兜底不可用 → 返回 None（放行给 AI），不抛异常。"""
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", False)
    monkeypatch.setattr(rt, "_OPENROUTER_KEY", "sk-or-v1-dead-key")
    monkeypatch.setattr(rt, "_call_pool_sync", lambda *a, **k: None)

    assert rt._l2_classify_haiku("某条新闻", "") is None
