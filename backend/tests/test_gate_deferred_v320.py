# -*- coding: utf-8 -*-
"""v320: 门卫不可用时的"不可判定 ≠ 是噪音"回归单测。

事故背景（2026-11-01）：
  MiniMax 两个账户 Token Plan 全部耗尽 → gate 池全池 429 持续 ~3h15m（2651 次失败）
  → v176.9 的 fail-closed 把"门卫不可用"当作"是噪音"（is_noise=1），
    这 3 小时的新闻 100% 被永久销毁 → 零交易信号且数据不可恢复。

v320 的三条不变量（本文件逐条锁死）：
  1. 门卫不可用 → is_noise 保持 0、status 不被置 FILTERED、relevance_score 不清零，
     改用 gate_unavailable=1 标记"未判定、保留待回放"。
  2. 门卫正常 → gate_unavailable=0；verdict="排除" → is_noise=1（真噪音仍被过滤）。
  3. 通道配额耗尽（429/401）→ 熔断打开后不再对同一通道发起真实调用，
     且熔断跳过不污染 ai_call_stats 埋点。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import circuit_breaker
import config
import realtime_filter as rt
import pytest


# ======================================================================
# v322: 全域额度闸门 —— 「任何 LLM 入口都必须先过闸门」
#
# 事故背景（2026-11-04）：一天烧掉 9.19M token / 4582 次请求。
# 根因不是单个 bug，而是**保护只加了一半** —— 主分析和门卫有闸门，
# 而 review_agent / judge_worker / webhook 翻译 / attribute_multi_factor /
# gate_replay_guard 探针 / api_server 通道体检 这些路径全是裸 HTTP，
# 完全绕过闸门与熔断器；而且 review 的 429 还会 sleep(2) 重试。
#
# 下面这组测试锁死「闸门拦截时一个字节都不发出去」这条不变量。
# ======================================================================


def _install_blocking_budget(monkeypatch):
    """把所有 llm_budget.check 换成「拦截」，并记录调用次数。"""
    import llm_budget
    calls = []

    def _blocked(est_tokens=0, purpose="main"):
        calls.append((est_tokens, purpose))
        return False, "测试：额度已用尽"

    monkeypatch.setattr(llm_budget, "check", _blocked)
    return calls


class _BoomSession:
    """任何 HTTP 调用都让测试失败 —— 用来证明「一个字节都没发出去」。"""

    def __init__(self):
        self.called = 0

    def post(self, *a, **kw):
        self.called += 1
        raise AssertionError("闸门拦截后不应发出任何 HTTP 请求")

    def get(self, *a, **kw):
        self.called += 1
        raise AssertionError("闸门拦截后不应发出任何 HTTP 请求")


def test_review_agent_llm_blocked_by_budget(monkeypatch):
    """review_agent._llm：额度用光 → 不发请求（此前是裸 requests.post）。"""
    import llm_budget
    from review import review_agent
    from engine import ai_worker as _aw

    calls = _install_blocking_budget(monkeypatch)

    class _FakeCfg(dict):
        pass

    fake = _FakeCfg({
        "label": "MiniMax-M3", "id": "MiniMax-M3", "api_base": "https://api.minimax.chat/v1",
        "api_key": "sk-test", "request_model": "MiniMax-M3", "use_proxy": False,
    })
    monkeypatch.setattr(review_agent.config, "AI_MODEL_ROSTER", [fake])
    monkeypatch.setattr(_aw, "_resolve_model_cfg", lambda m: fake)
    monkeypatch.setattr(_aw, "_minimax_fallback_cfg", lambda: None)

    boom = _BoomSession()
    monkeypatch.setattr(_aw, "_LLM_SESSION", boom)
    monkeypatch.setattr(_aw, "_LLM_PROXY_SESSION", boom)

    out = review_agent._llm("prompt", "system")
    assert out is None
    assert boom.called == 0, "闸门拦截后不应发出 HTTP 请求"
    assert calls, "review 路径必须走额度闸门"
    assert calls[0][1] == "review"


def test_review_agent_llm_does_not_retry_on_429(monkeypatch):
    """review 路径遇到 429 只打一次 —— 重试就是白烧钱（事故根因之一）。"""
    import llm_budget
    from review import review_agent
    from engine import ai_worker as _aw

    monkeypatch.setattr(llm_budget, "check", lambda est_tokens=0, purpose="main": (True, ""))

    fake = {
        "label": "MiniMax-M3", "id": "MiniMax-M3", "api_base": "https://api.minimax.chat/v1",
        "api_key": "sk-test", "request_model": "MiniMax-M3", "use_proxy": False,
    }
    monkeypatch.setattr(review_agent.config, "AI_MODEL_ROSTER", [fake])
    monkeypatch.setattr(_aw, "_resolve_model_cfg", lambda m: fake)
    monkeypatch.setattr(_aw, "_minimax_fallback_cfg", lambda: None)

    calls = []

    class _Session:
        def post(self, *a, **kw):
            calls.append(1)

            class _R:
                status_code = 429
                text = "rate limited"
            return _R()

    monkeypatch.setattr(_aw, "_LLM_SESSION", _Session())
    monkeypatch.setattr(_aw, "_LLM_PROXY_SESSION", _Session())

    out = review_agent._llm("prompt", "system")
    assert out is None
    assert len(calls) == 1, f"429 不应重试，实际打了 {len(calls)} 次"


def test_gate_replay_guard_probe_blocked_by_budget(monkeypatch):
    """gate_replay_guard 探针：额度用光 → 一个探针都不发（此前每 5 分钟烧一次）。"""
    import llm_budget
    from engine import gate_replay_guard as grg

    calls = _install_blocking_budget(monkeypatch)

    out = grg.probe_channels()
    assert out["ok"] is False
    assert "额度闸门" in out["reason"]
    assert out["channels"] == [], "闸门拦截时不应探测任何通道"
    assert calls


def test_judge_worker_remote_fallback_blocked_by_budget(monkeypatch):
    """judge_worker 远程兜底：额度用光 → 不发 requests.post。"""
    import llm_budget
    import judge_worker
    import config as _config

    calls = _install_blocking_budget(monkeypatch)

    def _fake_post(url, *a, **kw):
        # 本地 Ollama 路径：让它"连不上"（普通失败 → 走远程兜底）
        if "/api/generate" in str(url):
            raise ConnectionError("ollama down")
        # 远程路径：闸门已经拦了，绝不该走到这里
        raise AssertionError("闸门拦截后不应发出任何远程 HTTP 请求")

    monkeypatch.setattr("requests.post", _fake_post)
    monkeypatch.setattr(_config, "MINIMAX2_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(_config, "MINIMAX_BASE_URL",
                        "https://api.minimax.chat/v1", raising=False)

    out = judge_worker._call_judge_llm("新闻正文", "XAU", "做多", 0.8, "推理")
    assert out is None
    assert calls, "judge 路径必须走额度闸门"
    assert calls[0][1] == "judge"


def test_audit_script_finds_no_unprotected_llm_call_sites():
    """回归防线：审计脚本必须报告「零未保护调用点」。"""
    import subprocess
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    script = backend / "scripts" / "audit_llm_gates.py"
    if not script.exists():
        pytest.skip("审计脚本不存在")
    proc = subprocess.run(
        ["python3", str(script), "--json"],
        capture_output=True, text=True, cwd=str(backend), timeout=300,
    )
    assert proc.returncode == 0, (
        "存在未受额度闸门保护的 LLM 调用点：\n" + proc.stdout[-4000:]
    )



@pytest.fixture(autouse=True)
def _allow_budget_gate(monkeypatch):
    """本文件测的是**熔断/门卫语义**，不是额度闸门 —— 默认放行额度闸门。

    v322：llm_budget 默认 fail-closed（DB 读不到账本就拦截，避免闸门自己失效），
    而测试环境没有真实的 ai_call_stats 表 → 会被误拦，掩盖被测语义。
    额度闸门本身由 scripts/test_budget_gate.py 与
    test_budget_gate_blocks_gate_call 单独覆盖。
    """
    import llm_budget
    monkeypatch.setattr(llm_budget, "check",
                        lambda est_tokens=0, purpose="gate": (True, ""))


def _reset_breakers():
    circuit_breaker.reset()


# ───────────────────────── 不变量 1：门卫不可用 ≠ 噪音 ─────────────────────────

def test_gate_unavailable_not_treated_as_noise(monkeypatch):
    """门卫 LLM 不可用 → 不是噪音；命中规则兜底时降级放行（v342 契约）。

    历史：v320 时代门卫不可用一律 gate_unavailable=1（冻结待回放）。
    v342 起加了一条"[TRIDENT_GATE_RULE_FALLBACK] 规则兜底"：
    关键词命中的高价值新闻不再无条件冻结，改为 **降级放行** 并标
    gate_unavailable=2 + gate_rule_approved=1（否则配额打满时数据永久积压）。
    本用例的输入正是 L1 命中"美联储"的 CORE 高分新闻，因此走的是 2 分支。

    不变量不变：不得当噪音（is_noise=0）、不得清零 L1 打分、不得标 FILTERED、
    判定必须写"未判定"。这些断言原样保留。
    """
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": None)

    result = {
        "is_noise": 0,
        "relevance_score": 0.9,   # L1 命中的 CORE 高分
        "status": "PENDING",
        "layer": "L1",
        "verdict": "CORE",
        "matched_keyword": "美联储",
    }
    out = rt._attach_gate(dict(result), "美联储暗示降息", "body")

    # v449（2026-11-18，用户定策）：门卫 LLM 不可用不再一律写「未判定」，改由规则判决。
    # 命中规则 → 可交易（gate_unavailable=2 + gate_rule_approved=1 + gate_rule_only=1）；
    # 规则判无信号 → 按噪音归档（is_noise=1 / FILTERED）。这是有意推翻 v320 契约。
    assert out["gate_unavailable"] in (1, 2), (
        "门卫不可用必须标记 gate_unavailable=1(冻结待回放) 或 2(规则兜底放行)，"
        f"实际={out.get('gate_unavailable')}")
    assert out["gate_unavailable"] == 2, "规则命中时必须降级放行，不许冻结"
    assert out.get("gate_rule_approved") in (1, True), (
        "走 2 分支必须同时标 gate_rule_approved=1，否则无法区分"
        "'规则放行' 与 '鉴权失效降级放行'")
    assert out.get("gate_rule_only") == 1, "v449：必须标注这条判定来自规则而非 LLM"
    assert out["is_noise"] == 0, "规则兜底放行 → 不是噪音"
    assert out["relevance_score"] == 0.9, "不可判定不应清零 L1 打分"
    assert out["status"] != "FILTERED", "规则兜底放行的新闻不得被归档"
    # v450（2026-11-19）推翻了 v449 这一条：规则兜底放行**不再伪造「可交易」**。
    # 实测 v449 那版把兜底行全部写成可交易，当日 648 条"可交易"里只有 6 条真过门，
    # 队列被假信号灌满、方向判断被稀释。现在的契约是：兜底放行 = 未判定（仍进主分析）。
    assert out["gate_verdict"] == "未判定", (
        "v450：规则兜底只负责放行，不得伪造可交易标签"
        f"（实际={out.get('gate_verdict')!r}）")
    assert out["gate"]["reason"] in ("gate-llm-unavailable", "gate-rule-fallback")


def test_gate_unavailable_rule_verdict_decides_instead_of_undecided(monkeypatch):
    """v449 契约：门卫不可用时由规则给真结论，不再把「未判定」泄漏出去。

    v427：网络/超时/限流默认走降级放行 gate_unavailable=2（不冻结），避免错过
    45 分钟建仓窗口；v449 在此基础上要求规则必须给出「可交易 / 无信号」二者之一，
    判无信号则按噪音归档（用户定策，见 AGENTS.md v444/v449 段）。
    """
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": None)
    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.5, "status": "PENDING"},
                          "标题", "")
    assert out["gate_unavailable"] == 2, "默认不冻结，必须降级放行"
    assert out.get("gate_rule_only") == 1, "判定必须来自规则"
    # v450：兜底只负责放行，判定如实写「未判定」；只有规则明确判无信号才归档。
    assert out["gate_verdict"] in ("未判定", "无信号"), (
        f"规则兜底必须给出可解释判定（未判定/无信号）：{out.get('gate_verdict')!r}")
    if out["gate_verdict"] == "无信号":
        assert out["is_noise"] == 1 and out["status"] == "FILTERED", (
            "规则判无信号 → 按噪音归档（有意行为，不是漏数据）")
    else:
        assert out["is_noise"] == 0 and out["status"] != "FILTERED", (
            "规则兜底放行 → 必须留在队列等主分析")


def test_gate_freeze_on_timeout_env_keeps_frozen(monkeypatch):
    """开关回退旧行为：FREEZE_ON_TIMEOUT=1 时网络故障仍冻结 =1 待回放。"""
    monkeypatch.setenv("TRIDENT_GATE_FREEZE_ON_TIMEOUT", "1")
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": None)
    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.5, "status": "PENDING"},
                          "标题", "")
    assert out["is_noise"] == 0
    assert out["gate_unavailable"] == 1

# ───────────────────────── 不变量 2：门卫正常路径不受影响 ─────────────────────────

def test_gate_ok_sets_unavailable_zero(monkeypatch):
    """门卫正常返回 → gate_unavailable=0。"""
    gate = {"verdict": "可交易", "is_material": True, "assets": ["黄金"],
            "core_impact": "供需变动", "main_asset": "黄金", "kind": "总结类",
            "timing": "即时", "strength": 8, "impact_1to100": 70}
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": gate)

    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.9, "status": "PENDING"},
                          "标题", "")
    assert out["gate_unavailable"] == 0
    assert out["is_noise"] == 0
    assert out["gate_verdict"] == "可交易"


def test_gate_exclude_still_marks_noise(monkeypatch):
    """门卫判"排除" → 仍是真噪音（is_noise=1），行为不变。"""
    gate = {"verdict": "排除", "is_material": False, "assets": [], "core_impact": "",
            "main_asset": None, "kind": "总结类", "timing": "已定价",
            "strength": 1, "impact_1to100": 5}
    monkeypatch.setattr(rt, "_l3_semantic_gate", lambda t, c="": gate)

    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.9, "status": "PENDING"},
                          "标题", "")
    assert out["is_noise"] == 1
    assert out["status"] == "FILTERED"
    assert out["gate_unavailable"] == 0

# ───────────────────────── 不变量 3：通道熔断 ─────────────────────────

def _fake_pool_entry(alias="minimax2", model="MiniMax-M3"):
    return [{
        "id": f"{alias}/{model}",
        "label": f"{model} (fake)",
        "api_base": "https://example.invalid/v1",
        "api_key": "fake-key",
        "request_model": model,
        "extra_body": {},
        "key_alias": alias,
        "is_local": False,
        "timeout": 1.0,
        "use_proxy": False,
        "purpose": ["gate"],
    }]


def test_quota_failure_opens_circuit_and_skips_call(monkeypatch):
    """配额失败 → 熔断打开 → 后续不再对该通道发起真实调用。

    v322 变更：配额类 fail_threshold 由 **2 降到 1**（一次失败即熔断）。
    理由：429/401 的语义是"配额没了/key 失效"，重试结果可预测，
    再打一次纯属烧用户额度 —— 这正是 2026-11-04 额度被打穿的主因之一。
    """
    _reset_breakers()
    calls = []

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        calls.append(model)
        if err_sink is not None:
            err_sink.update({"http_code": 429, "kind": "quota", "model": model})
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    # 锁住新阈值：防止有人把 fail_threshold 改回 2（那会重新打开漏钱口）
    assert rt._llm_breaker("minimax2", "quota").fail_threshold == 1, \
        "配额熔断阈值必须是 1（一次失败即熔断）"

    assert rt._call_pool_sync("sys", "user") is None
    assert len(calls) == 1, "首次应真实调用一次"

    # 熔断阈值=1 → 第一次配额失败后即熔断打开，第二次起不再发请求
    assert rt._call_pool_sync("sys", "user") is None
    assert len(calls) == 1, "配额失败一次即熔断，不应再发第二次请求"

    # 第三次：熔断已开 → 不再发起真实调用
    assert rt._call_pool_sync("sys", "user") is None
    assert len(calls) == 1, "熔断打开后不应再对耗尽额度的通道发起调用"

    brk = circuit_breaker.get("llm_quota:minimax2")
    assert brk.state == "open"
    _reset_breakers()


def test_circuit_open_not_logged_as_ai_call(monkeypatch):
    """熔断跳过不是一次真实调用 → 不应写 ai_call_stats 埋点。"""
    _reset_breakers()
    logged = []
    import db as _db

    monkeypatch.setattr(_db, "log_ai_call", lambda **kw: logged.append(kw))

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        if err_sink is not None:
            err_sink.update({"http_code": 429, "kind": "quota", "model": model})
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    rt._call_pool_sync("sys", "user")   # 真实失败 → 写 1 条埋点
    assert len(logged) == 1

    # v322: 阈值=1 → 第一次失败即熔断，后续调用直接跳过，不产生埋点
    rt._call_pool_sync("sys", "user")   # 熔断已开 → 跳过
    rt._call_pool_sync("sys", "user")   # 仍跳过
    assert len(logged) == 1, "熔断跳过不应产生埋点"
    _reset_breakers()


def test_auth_failure_uses_long_cooldown(monkeypatch):
    """401/402/403 走 auth 熔断：冷却期足够长，冷却期内不再重复打请求。

    历史：v322 把 auth 冷却定成 24h（一天 144 次白打 → 改成一天 1 次）。
    2026-11-16 又调回 **15 分钟**（realtime_filter._AUTH_COOLDOWN_S，可用
    GATE_AUTH_COOLDOWN_S 覆盖）：实测"一次 401 后 key 其实已恢复，门卫却继续
    黑箱一整天"，而熔断器本身支持 half_open 单飞探测、一次只花 1 个 token。
    所以契约从"≥24h"变成"一个有限的、可配置的冷却窗口"。

    本用例守护的不变量（与 v322 相同）：**冷却期内不得对失效通道重复发请求**。
    """
    _reset_breakers()
    calls = []

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        calls.append(model)
        if err_sink is not None:
            err_sink.update({"http_code": 401, "kind": "auth", "model": model})
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    brk_a = rt._llm_breaker("minimax2", "auth")
    assert brk_a is not None
    # 冷却窗口必须是一个有限的正值（不是 0/秒级），且不短于 10 分钟 ——
    # 否则 401 通道会被高频重复打；上限不设，允许运维把 GATE_AUTH_COOLDOWN_S 调更长。
    assert 600.0 <= brk_a.open_seconds <= 86400.0, (
        f"鉴权熔断冷却应在 10 分钟~24 小时之间（实测 {brk_a.open_seconds}）")

    assert rt._call_pool_sync("sys", "user") is None
    assert len(calls) == 1

    # auth 熔断已开 → 后续不再发请求
    assert rt._call_pool_sync("sys", "user") is None
    assert len(calls) == 1, "鉴权失败后不应再对失效通道发起调用"

    assert circuit_breaker.get("llm_auth:minimax2").state == "open"
    _reset_breakers()


def test_budget_gate_blocks_gate_call(monkeypatch):
    """v322: 额度闸门命中时，门卫**不发任何请求**（零额度消耗）。"""
    _reset_breakers()
    calls = []

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        calls.append(model)
        return "ok"

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    import llm_budget
    monkeypatch.setattr(llm_budget, "check",
                        lambda est_tokens=0, purpose="gate": (False, "测试：预算耗尽"))

    rt._call_pool_sync("sys", "user")
    assert calls == [], "额度闸门命中时不应发出任何请求"
    _reset_breakers()


def test_gate_cache_avoids_second_call(monkeypatch):
    """v322: 门卫结果缓存 —— 同样的新闻第二次调用**零请求**。

    背景（2026-11-04）：门卫 4278 次调用 / 3.68M token，占全天 40%。
    门卫是 temperature=0 的确定性判断，缓存不改变判定结果，
    但能省掉重复新闻 / 多源转载 / gate-replay 回放的重复付费。
    """
    _reset_breakers()
    calls = []

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        calls.append(model)
        return "1"

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    # 清缓存 + 确认缓存开启
    with rt._GATE_CACHE_LOCK:
        rt._GATE_CACHE.clear()
    monkeypatch.setattr(config, "GATE_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "GATE_CACHE_MAX_ENTRIES", 10)
    monkeypatch.setattr(config, "GATE_CACHE_TTL_S", 600.0)

    first = rt._call_pool_sync("sys", "同一篇新闻", label="CACHE-T")
    assert first == "1"
    assert len(calls) == 1, "首次应真实调用一次"

    second = rt._call_pool_sync("sys", "同一篇新闻", label="CACHE-T")
    assert second == "1", "缓存命中必须返回与首次完全相同的结果"
    assert len(calls) == 1, "缓存命中时不应再发出任何请求（零额度消耗）"

    # 不同新闻 → 正常调用
    rt._call_pool_sync("sys", "另一篇新闻", label="CACHE-T")
    assert len(calls) == 2, "不同新闻不应命中缓存"

    with rt._GATE_CACHE_LOCK:
        rt._GATE_CACHE.clear()
    _reset_breakers()


def test_gate_cache_respects_max_entries(monkeypatch):
    """v322: 缓存有条数上限（本机只有 8GB RAM，不能无限增长）。"""
    _reset_breakers()
    monkeypatch.setattr(config, "GATE_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "GATE_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(config, "GATE_CACHE_TTL_S", 600.0)
    with rt._GATE_CACHE_LOCK:
        rt._GATE_CACHE.clear()

    for i in range(10):
        rt._gate_cache_put(f"key{i}", f"val{i}")

    with rt._GATE_CACHE_LOCK:
        n = len(rt._GATE_CACHE)
    assert n <= 3, f"缓存条数必须受限，实际 {n}"
    _reset_breakers()


def test_network_failure_uses_short_cooldown(monkeypatch):
    """网络抖动走 net 熔断（短冷却），不污染 quota 熔断（长冷却）。"""
    _reset_breakers()

    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        if err_sink is not None:
            err_sink.update({"http_code": None, "kind": "network", "model": model})
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    rt._call_pool_sync("sys", "user")
    rt._call_pool_sync("sys", "user")
    rt._call_pool_sync("sys", "user")

    assert circuit_breaker.get("llm_quota:minimax2").state == "closed"
    assert circuit_breaker.get("llm_net:minimax2").state == "open"
    _reset_breakers()

# ───────────────────────── 通道路由 + 落库 + 认领 ─────────────────────────

def test_gate_pool_not_empty():
    """gate 池必须有通道（否则回退到主池，通道一挂门卫就彻底没救）。"""
    pool = config.get_pool_for_purpose("gate")
    assert pool, "gate 池为空 —— 说明专用通道因 key 守卫被跳过"


def test_insert_raw_news_writes_gate_unavailable():
    """落库时 gate_unavailable 标记必须写入。"""
    import db as _db

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source TEXT, content TEXT, timestamp TEXT, status TEXT, "
        "is_noise INTEGER, relevance_score REAL, gate_unavailable INTEGER DEFAULT 0)"
    )
    nid = _db.insert_raw_news(
        conn, source="s", content="c", timestamp="t", status="PENDING",
        is_noise=0, relevance_score=0.5, gate_unavailable=1,
    )
    row = conn.execute("SELECT gate_unavailable FROM raw_news WHERE id=?", (nid,)).fetchone()
    assert row[0] == 1
    conn.close()


def test_claim_excludes_deferred_news():
    """ai_worker 认领 SQL 必须排除 gate_unavailable=1（冻结待回放），但**放行 2**。

    v3215(FIX-8b)：门槛从 `= 0` 放宽到 `IN (0, 2)` ——
    gu=2 表示"门卫鉴权失效 → 明确降级放行"，必须能被认领，否则标记了也进不了分析。
    """
    src = Path(SRC) / "engine" / "ai_worker.py"
    text = src.read_text(encoding="utf-8")
    assert "COALESCE(gate_unavailable, 0) IN (0, 2)" in text, \
        "认领条件必须放行 gate_unavailable=2，同时排除 1"
    assert 'COALESCE(gate_unavailable, 0) = 0"' not in text, \
        "旧门槛 `= 0` 会把降级放行的行排除在分析之外（放了又拦）"


def test_deferred_news_not_claimed_behavior():
    """语义验证：未判定新闻不被认领，正常新闻照常认领。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "status TEXT, is_noise INTEGER, gate_unavailable INTEGER DEFAULT 0, "
        "processing_at TEXT)"
    )
    conn.execute("INSERT INTO raw_news (status,is_noise,gate_unavailable) "
                 "VALUES ('PENDING',0,1)")   # 未判定
    conn.execute("INSERT INTO raw_news (status,is_noise,gate_unavailable) "
                 "VALUES ('PENDING',0,0)")   # 正常
    rows = conn.execute(
        "SELECT id FROM raw_news WHERE status='PENDING' AND is_noise=0 "
        "AND COALESCE(gate_unavailable, 0) = 0"
    ).fetchall()
    assert [r[0] for r in rows] == [2]
    conn.close()
