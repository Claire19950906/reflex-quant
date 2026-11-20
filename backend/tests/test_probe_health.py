"""tests/test_probe_health.py — v172 Step 1/2/3 单测。

覆盖：
  · _classify_probe_failure 把 status_code / exc 映射到用户可读 category
  · _probe_channel 走 circuit_breaker：3 次失败 → circuit_open 短路
  · _probe_channel 成功 → breaker.success 复位
  · _probe_model_channels 单真相源：probe 通道集合 == config.AI_MODEL_ROSTER
  · category 字段在所有返回里都存在

2026-11-18: 探测/健康度 12 个函数已从 api_server 下沉到 model_probe，
所以 `patch.object(srv, ...)` 不再影响内部调用（内部解析的是 model_probe 名字），
这里统一改为 `patch.object(mprobe, ...)`；状态缓存 `srv._MODEL_HEALTH_CACHE` 仍是同一对象。
"""

from __future__ import annotations

import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, "src_python")

import api_server as srv
import circuit_breaker
import config
import requests
import model_probe as mprobe  # 2026-11-18: 探测逻辑已下沉，patch 点跟着搬


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
    # v323: 探针的「静默窗口」(quota_exhausted / auth) 也是跨用例状态, 必须一起清
    srv._probe_dead_state_reset()
    yield
    circuit_breaker.reset()
    srv._probe_dead_state_reset()


@pytest.fixture(autouse=True)
def _allow_budget_gate(monkeypatch):
    """本文件测的是**探针语义**，不是额度闸门 —— 默认放行额度闸门与速率闸门。

    v322：_probe_channel 现在也走额度闸门（面板点一次「测试通道」= 一次真实计费请求）。
    而 llm_budget 默认 fail-closed（DB 读不到账本就拦截，避免闸门自己失效），
    测试环境没有真实的 ai_call_stats 表 → 探针被误拦成 budget_blocked，
    掩盖了被测的分类语义（rate_limit / api_error / network）。
    额度闸门本身由 scripts/test_budget_gate.py 与 test_gate_deferred_v320.py 覆盖。
    """
    import llm_budget

    monkeypatch.setattr(llm_budget, "check",
                        lambda est_tokens=0, purpose="probe": (True, ""))
    monkeypatch.setattr(llm_budget, "consume_rate_slot",
                        lambda purpose="probe", window_s=60.0: (True, ""))


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

    def _fake_probe(name, model, base, key, use_proxy, breaker_key=None, **kwargs):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    # v366 起 _probe_model_channels 是"只读缓存"入口（请求路径，永不发网络），
    # 想验证探测覆盖必须打 _probe_model_channels_now（后台线程真正跑的那个）。
    with mock.patch.object(mprobe, "_probe_channel", side_effect=_fake_probe):
        out = srv._probe_model_channels_now()
    assert len(out["channels"]) == len(config.AI_MODEL_ROSTER)
    roster_labels = {m["label"] for m in config.AI_MODEL_ROSTER}
    probe_labels = {c["name"] for c in out["channels"]}
    assert probe_labels == roster_labels


def test_probe_returns_category_in_every_channel():
    """回归保护：所有 channel 都带 category 字段。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})

    def _fake_probe(name, model, base, key, use_proxy, breaker_key=None, **kwargs):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    with mock.patch.object(mprobe, "_probe_channel", side_effect=_fake_probe):
        out = srv._probe_model_channels_now()
    for c in out["channels"]:
        assert "category" in c
        assert "circuit_state" in c


def test_probe_cache_returns_same_payload_within_ttl():
    """30s TTL：第二次直接返回缓存（**零请求**）。

    v322 修正断言：原来断言 `p.call_count == len(config.AI_MODEL_ROSTER)`，
    但 `_probe_specs_deduped` 会按 (api_key, request_model) **去重**
    （同 key 同 model 的多个通道共享一次探测，账户级配额下这是必须的 ——
    并发探测同一个 key 会自己和自己抢 Token Plan）。
    所以真实调用次数是「去重后的组数」，不是「通道数」。

    真正要锁死的不变量是：
      ① 返回的通道**覆盖 roster 全部**（去重不能丢通道）；
      ② 第二次调用**零请求**（缓存生效）。
    """
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})

    def _fake_probe(name, model, base, key, use_proxy, breaker_key=None, **kwargs):
        return {
            "name": name, "model": model, "available": True,
            "status_code": 200, "category": "ok",
            "circuit_state": "closed", "latency_ms": 1.0, "note": "",
        }
    with mock.patch.object(mprobe, "_probe_channel", side_effect=_fake_probe) as p:
        first = srv._probe_model_channels_now()
        after_first = p.call_count
    # v366 架构：_probe_model_channels 只读缓存。把刚才的真结果灌进缓存，
    # 第二次调用必须命中缓存、零请求。
    srv._MODEL_HEALTH_CACHE.update({"ts": time.monotonic(), "value": dict(first)})
    # 2026-11-18 修脏: 这条断言原来会被**后台探测线程**打穿 —— _probe_model_channels()
    # 内部会 _ensure_model_health_thread() / 过期时 _kick_model_health_refresh(),
    # 线程一旦在这几行之间刷新缓存, p2.call_count 就不再是 0, 于是"整批跑红、单独跑绿"。
    # 要锁的不变量只是「缓存命中路径本身不发探测」, 所以把线程与刷新入口一起关掉。
    with mock.patch.object(mprobe, "_ensure_model_health_thread", lambda: None), \
         mock.patch.object(mprobe, "_kick_model_health_refresh", lambda *a, **k: None), \
         mock.patch.object(mprobe, "_probe_channel", side_effect=_fake_probe) as p2:
        second = srv._probe_model_channels()
        assert p2.call_count == 0, "30s TTL 内第二次必须零请求（缓存命中）"

    # ① 去重不能丢通道 —— 结果仍须覆盖 roster 全部
    assert len(first["channels"]) == len(config.AI_MODEL_ROSTER)
    assert {c["name"] for c in first["channels"]} == {
        m["label"] for m in config.AI_MODEL_ROSTER
    }
    # ② 探测请求数 = 去重后的组数（≤ 通道数，且确实做了去重）
    assert 0 < after_first <= len(config.AI_MODEL_ROSTER)
    assert {c["name"] for c in second["channels"]} == {c["name"] for c in first["channels"]}


def test_probe_no_roster_no_probe():
    """roster 为空时不发起任何探测（graceful degrade）。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})
    # 注意：前面的用例会启动后台探测线程，线程一跑就会调用 _probe_channel，
    # 所以"call_count == 0"在这里不可靠（是测试污染，不是产品行为）。
    # 真正要锁的不变量是"roster 为空时，本函数产出的通道列表为空"，直接断言结果。
    with mock.patch.object(config, "AI_MODEL_ROSTER", ()):
        out = srv._probe_model_channels_now()
    assert out["channels"] == []


def test_probe_excludes_disabled_providers():
    """OPENROUTER_ENABLED=0 时 OpenRouter 不被探测（不被消费额度）。"""
    srv._MODEL_HEALTH_CACHE.update({"ts": 0.0, "value": None})
    with mock.patch.object(config, "OPENROUTER_ENABLED", False):
        with mock.patch.object(mprobe, "_probe_channel") as p:
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


# ────────────────────────────────────────────────────────────────────
# v323: 「额度耗尽」单独成类 + 本地通道超时 + 静默窗口
# ────────────────────────────────────────────────────────────────────
#
# 事故背景 (2026-11-10 实测, 4 家远程通道全挂):
#   minimax 账户1 → 429 "已达到 Token Plan 用量上限"
#   minimax 账户2 → 402 insufficient_balance
#   xinshu        → 429 API_KEY_QUOTA_EXHAUSTED
#   openrouter    → 401 User not found
# 旧口径把 429 一律归 rate_limit (可重试) → 面板看不出「套餐用完, 要充值」。
# 另外本地 Ollama 通道被误判 network (冷启动 25s > 探针 read timeout 15s)。

def test_classify_402_is_quota_exhausted():
    """402 = 余额不足, 不会自愈 → quota_exhausted (不是 api_error)。"""
    assert srv._classify_probe_failure(402) == "quota_exhausted"


def test_classify_429_with_quota_marker_is_quota_exhausted():
    """带额度标记的 429 → quota_exhausted (实测 minimax / xinshu 原文)。"""
    minimax_body = ('{"type":"error","error":{"type":"rate_limit_error","message":'
                     '"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)"}}')
    assert srv._classify_probe_failure(429, body=minimax_body) == "quota_exhausted"
    xinshu_body = '{"code":"API_KEY_QUOTA_EXHAUSTED","message":"API key 额度已用完"}'
    assert srv._classify_probe_failure(429, body=xinshu_body) == "quota_exhausted"


def test_classify_plain_429_stays_rate_limit():
    """没有额度标记的普通 429 → 仍然是 rate_limit (瞬时限流, 可重试)。"""
    assert srv._classify_probe_failure(429, body='{"error":"too many requests"}') == "rate_limit"
    assert srv._classify_probe_failure(429, body="") == "rate_limit"


def test_classify_timeout_exception():
    """超时单列 (与「连不上」区分)。"""
    exc = requests.exceptions.ReadTimeout("read timed out")
    assert srv._classify_probe_failure(None, exc) == "timeout"
    # 非超时的网络异常仍然是 network
    assert srv._classify_probe_failure(None, ConnectionError("refused")) == "network"


def test_probe_local_channel_uses_longer_timeout():
    """本地 Ollama 通道 → read timeout 放宽到 120s (实测冷启动 25~74s)。

    旧的 (6, 15) 会把冷启动必然打成 network, 连续 3 次后熔断摘掉本地兜底通道。
    """
    captured = {}

    def _capture(url, *a, **kw):
        captured["timeout"] = kw.get("timeout")
        return _FakeResp(200, text="ok")

    with mock.patch.object(requests.Session, "post", _capture):
        out = srv._probe_channel("local_probe", "qwen2.5:3b",
                                 "http://localhost:11434/v1", "k", False)
    assert out["available"] is True
    assert captured["timeout"] == (10, 120)


def test_probe_local_timeout_does_not_trip_breaker():
    """本地通道超时 → 归类 timeout, 但**不计入熔断失败**。

    冷启动 25~74s 是本地模型的常态 (内存紧张时更久)。若把超时算进熔断失败,
    连续 3 次冷启动就会把本地兜底通道摘掉 3 分钟 —— 慢 ≠ 坏。
    """
    def _timeout(url, *a, **kw):
        raise requests.exceptions.ReadTimeout("read timed out")

    with mock.patch.object(requests.Session, "post", _timeout):
        out = srv._probe_channel("local_slow", "qwen2.5:3b",
                                 "http://localhost:11434/v1", "k", False)
    assert out["category"] == "timeout"
    assert out["available"] is False
    brk = circuit_breaker.get("llm:local_slow")
    assert brk.status()["fail_count"] == 0, "本地超时不该计入熔断失败"
    assert brk.state == "closed"


def test_probe_remote_timeout_does_trip_breaker():
    """远程通道超时仍然计入熔断失败 (远程慢 = 真有问题)。"""
    def _timeout(url, *a, **kw):
        raise requests.exceptions.ReadTimeout("read timed out")

    with mock.patch.object(requests.Session, "post", _timeout):
        out = srv._probe_channel("remote_slow", "m", "https://api.minimax.chat/v1", "k", False)
    assert out["category"] == "timeout"
    brk = circuit_breaker.get("llm:remote_slow")
    assert brk.status()["fail_count"] == 1


def test_probe_remote_channel_keeps_short_timeout():
    """远程通道维持 (6, 15) —— 不因为本地放宽而被一起拖慢。"""
    captured = {}

    def _capture(url, *a, **kw):
        captured["timeout"] = kw.get("timeout")
        return _FakeResp(200, text="ok")

    with mock.patch.object(requests.Session, "post", _capture):
        srv._probe_channel("remote_probe", "m", "https://api.minimax.chat/v1", "k", False)
    assert captured["timeout"] == (6, 15)


def test_probe_quota_exhausted_enters_silent_window():
    """额度耗尽 → 进入静默窗口, 窗口内零网络请求 (不再反复打必然失败的通道)。"""
    body = '{"error":{"message":"已达到 Token Plan 用量上限"}}'

    def _quota(url, *a, **kw):
        return _FakeResp(429, text=body)

    with mock.patch.object(requests.Session, "post", _quota):
        out = srv._probe_channel("quota_ch", "m", "https://api.minimax.chat/v1", "k", False)
    assert out["category"] == "quota_exhausted"
    assert out["available"] is False

    # 第二次探测: 必须**零请求**, 直接返回已知分类
    with mock.patch.object(requests.Session, "post") as post_mock:
        out2 = srv._probe_channel("quota_ch", "m", "https://api.minimax.chat/v1", "k", False)
        assert post_mock.call_count == 0
    assert out2["category"] == "quota_exhausted"
    assert out2["available"] is False
    assert "额度" in out2["note"] or "充值" in out2["note"]


def test_probe_silent_window_expires():
    """静默窗口过期后自动复探 → 充值/换 key 之后能自愈, 不必重启进程。"""
    def _quota(url, *a, **kw):
        return _FakeResp(429, text='{"error":{"message":"已达到 Token Plan 用量上限"}}')

    with mock.patch.object(requests.Session, "post", _quota):
        srv._probe_channel("quota_exp", "m", "https://api.minimax.chat/v1", "k", False)
    # 手动把窗口推到过去
    srv._PROBE_DEAD_STATE["llm:quota_exp"]["until"] = 0.0
    with mock.patch.object(requests.Session, "post", _mock_post_factory(200)):
        out = srv._probe_channel("quota_exp", "m", "https://api.minimax.chat/v1", "k", False)
    assert out["available"] is True
    assert out["category"] == "ok"


def test_probe_auth_enters_silent_window():
    """key 失效 (401) 同样进静默窗口 —— 探测也不会好。"""
    with mock.patch.object(requests.Session, "post", _mock_post_factory(401)):
        out = srv._probe_channel("auth_ch", "m", "https://openrouter.ai/api/v1", "k", False)
    assert out["category"] == "auth"
    with mock.patch.object(requests.Session, "post") as post_mock:
        out2 = srv._probe_channel("auth_ch", "m", "https://openrouter.ai/api/v1", "k", False)
        assert post_mock.call_count == 0
    assert out2["category"] == "auth"