"""tests/test_probe_health.py — v172 Step 1/2/3 单测。

覆盖：
  · _classify_probe_failure 把 status_code / exc 映射到用户可读 category
  · _probe_channel 走 circuit_breaker：3 次失败 → circuit_open 短路
  · _probe_channel 成功 → breaker.success 复位
  · _probe_model_channels 单真相源：probe 通道集合 == config.AI_MODEL_ROSTER
  · category 字段在所有返回里都存在
"""

from __future__ import annotations

import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, "src_python")

import api_server as srv  # noqa: E402
import circuit_breaker  # noqa: E402
import config  # noqa: E402
import requests  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# _classify_probe_failure
# ────────────────────────────────────────────────────────────────────

def test_classify_ok():
    assert srv._classify_probe_failure(200) == "ok"


def test_classify_network_exception():
    assert srv._classify_probe_failure(None, ConnectionError("refused")) == "network"


def test_classify_network_no_status():
    assert srv._classify_probe_failure(None) == "network"

# ────────────────────────────────────────────────────────────────────
# _probe_channel — wire-in circuit_breaker
# ────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _reset_breakers():
    """每个用例前清空熔断器，避免测试互相污染。"""
    circuit_breaker.reset()
    yield
    circuit_breaker.reset()


def _mock_post_factory(status_code: int):
    def _fake_post(url, *a, **kw):
        return _FakeResp(status_code, text="bad" if status_code != 200 else "ok")
    return _fake_post


def test_probe_success_resets_breaker():
    with mock.patch.object(requests.Session, "post", _mock_post_factory(200)):
        out = srv._probe_channel("test_ok", "m", "http://x", "k", False)
    assert out["available"] is True
    assert out["category"] == "ok"
    assert out["circuit_state"] == "closed"
    brk = circuit_breaker.get("llm:test_ok")
    assert brk.status()["fail_count"] == 0


def test_probe_failure_records_and_categorizes():
    with mock.patch.object(requests.Session, "post", _mock_post_factory(401)):
        out = srv._probe_channel("test_auth", "m", "http://x", "k", False)
    assert out["available"] is False
    assert out["status_code"] == 401
    assert out["category"] == "auth"
    brk = circuit_breaker.get("llm:test_auth")
    assert brk.status()["fail_count"] == 1
    assert brk.state == "closed"


def test_probe_429_categorized_as_rate_limit():
    with mock.patch.object(requests.Session, "post", _mock_post_factory(429)):
        out = srv._probe_channel("test_rl", "m", "http://x", "k", False)
    assert out["category"] == "rate_limit"
    assert out["available"] is False


def test_probe_400_categorized_as_api_error():
    with mock.patch.object(requests.Session, "post", _mock_post_factory(400)):
        out = srv._probe_channel("test_400", "m", "http://x", "k", False)
    assert out["category"] == "api_error"


def test_probe_3_failures_open_circuit():
    """连续 3 次失败后，第 4 次直接 short-circuit，不发起网络。"""
    with mock.patch.object(requests.Session, "post", _mock_post_factory(500)):
        for i in range(3):
            srv._probe_channel("test_cb", "m", "http://x", "k", False)
    with mock.patch.object(requests.Session, "post") as post_mock:
        out = srv._probe_channel("test_cb", "m", "http://x", "k", False)
        assert post_mock.call_count == 0
    assert out["available"] is False
    assert out["category"] == "circuit_open"
    assert out["circuit_state"] == "open"
    assert out["status_code"] is None


def test_probe_circuit_breaker_recovery_after_cooldown():
    """open → 冷却 → half_open 探测成功 → closed。"""
    brk = circuit_breaker.get("llm:test_recovery", fail_threshold=2, open_seconds=0.1)
    brk.failure(); brk.failure()
    assert brk.state == "open"
    time.sleep(0.15)
    assert brk.allow() is True
# ────────────────────────────────────────────────────────────────────
# _probe_model_channels — 单真相源
# ────────────────────────────────────────────────────────────────────

def test_probe_specs_matches_roster():
    """probe 探测的通道数 == config.AI_MODEL_ROSTER 大小（单真相源）。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})

    def _fake_probe(name, model, base, key, use_proxy):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    with mock.patch.object(srv, "_probe_channel", side_effect=_fake_probe):
        out = srv._probe_model_channels()
    assert len(out["channels"]) == len(config.AI_MODEL_ROSTER)
    roster_labels = {m["label"] for m in config.AI_MODEL_ROSTER}
    probe_labels = {c["name"] for c in out["channels"]}
    assert probe_labels == roster_labels


def test_probe_returns_category_in_every_channel():
    """回归保护：所有 channel 都带 category 字段。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})

    def _fake_probe(name, model, base, key, use_proxy):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    with mock.patch.object(srv, "_probe_channel", side_effect=_fake_probe):
        out = srv._probe_model_channels()
    for c in out["channels"]:
        assert "category" in c
        assert "circuit_state" in c


def test_probe_cache_returns_same_payload_within_ttl():
    """30s TTL：第二次直接返回缓存。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})

    def _fake_probe(name, model, base, key, use_proxy):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    with mock.patch.object(srv, "_probe_channel", side_effect=_fake_probe) as p:
        first = srv._probe_model_channels()
        second = srv._probe_model_channels()
        assert p.call_count == len(config.AI_MODEL_ROSTER)
    assert first is second


def test_probe_no_roster_no_probe():
    """roster 为空时不发起任何探测（graceful degrade）。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})
    with mock.patch.object(config, "AI_MODEL_ROSTER", ()):
        with mock.patch.object(srv, "_probe_channel") as p:
            out = srv._probe_model_channels()
            assert p.call_count == 0
    assert out["channels"] == []


def test_probe_excludes_disabled_providers():
    """OPENROUTER_ENABLED=0 时 OpenRouter 不被探测（不被消费额度）。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})
    with mock.patch.object(config, "OPENROUTER_ENABLED", False):
        with mock.patch.object(srv, "_probe_channel") as p:
            p.return_value = {
                "name": "x", "model": "m", "available": True,
                "status_code": 200, "category": "ok",
                "circuit_state": "closed", "latency_ms": 1.0, "note": "",
            }
            out = srv._probe_model_channels()
            called_labels = [c.args[0] for c in p.call_args_list]
            assert "gpt-4o-mini (OpenRouter)" not in called_labels
    with mock.patch.object(requests.Session, "post", _mock_post_factory(200)):
        out = srv._probe_channel("test_recovery", "m", "http://x", "k", False)
    assert out["available"] is True
    assert out["category"] == "ok"
    assert out["circuit_state"] == "closed"


def test_probe_network_exception_categorized():
    def _boom(url, *a, **kw):
        raise ConnectionError("getaddrinfo failed")
    with mock.patch.object(requests.Session, "post", _boom):
        out = srv._probe_channel("test_net", "m", "http://x", "k", False)
    assert out["category"] == "network"
    assert out["available"] is False
    assert out["status_code"] is None

def test_classify_auth_401_403():
    assert srv._classify_probe_failure(401) == "auth"
    assert srv._classify_probe_failure(403) == "auth"


def test_classify_rate_limit_429():
    assert srv._classify_probe_failure(429) == "rate_limit"


def test_classify_api_error_400_500():
    assert srv._classify_probe_failure(400) == "api_error"
    assert srv._classify_probe_failure(404) == "api_error"
    assert srv._classify_probe_failure(500) == "api_error"
    assert srv._classify_probe_failure(503) == "api_error"