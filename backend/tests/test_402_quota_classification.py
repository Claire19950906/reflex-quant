# -*- coding: utf-8 -*-
"""402（余额/套餐耗尽）错误分类回归。

背景（2026-11-19 第三轮深挖）：
  · MiniMax 返回 HTTP 402 `insufficient_balance_error`（已达 Token Plan 用量上限），
    属"额度耗尽"类，应进 quota 熔断（30 分钟），而不是被当成 key 失效的 auth 熔断。
  · 旧口径把 401/402/403 一并归入 auth，且日志误写"24 小时"，与探针层
    `model_probe._classify_probe_failure`（402 → quota_exhausted）不一致。

本文件锁死不变量：
  1. 过滤器 `_post_openai_compat` 收到 402 → err_sink.kind == "quota"（不是 auth）。
  2. 401/403 → err_sink.kind == "auth"。
  3. 探针分类口径：402 → quota_exhausted；401/403 → auth。
"""

from __future__ import annotations

import os
import sys
import urllib.error
from pathlib import Path

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import realtime_filter as rt
import model_probe


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x/v1/chat/completions",
        code=code, msg="Payment Required", hdrs={}, fp=__import__("io").BytesIO(body),
    )


def _run_post(monkeypatch, code: int, body: bytes = b"") -> dict:
    import urllib.request
    sink: dict = {}
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *_a, **_k: _Opener(code, body))
    rt._post_openai_compat("http://x/v1", "k", "m",
                           {"messages": []}, 1.0, err_sink=sink)
    return sink


class _Opener:
    def __init__(self, code: int, body: bytes):
        self._code, self._body = code, body

    def open(self, *_a, **_k):
        raise _http_error(self._code, self._body)


def test_filter_402_classified_quota(monkeypatch):
    sink = _run_post(monkeypatch, 402,
                     b'{"type":"error","error":{"type":"insufficient_balance_error"}}')
    assert sink["kind"] == "quota", f"402 应归 quota，实际 {sink.get('kind')}"


def test_filter_401_classified_auth(monkeypatch):
    sink = _run_post(monkeypatch, 401, b'{"error":{"message":"User not found."}}')
    assert sink["kind"] == "auth"


def test_filter_403_classified_auth(monkeypatch):
    sink = _run_post(monkeypatch, 403, b"")
    assert sink["kind"] == "auth"


def test_probe_classifier_402_quota():
    assert model_probe._classify_probe_failure(402) == "quota_exhausted"
    assert model_probe._classify_probe_failure(401) == "auth"
    assert model_probe._classify_probe_failure(403) == "auth"
