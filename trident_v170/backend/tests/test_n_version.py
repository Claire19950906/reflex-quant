"""v175 — N-version 共识算法测试。

覆盖：required_majority 数学 / aggregate_votes 共识 + 多数失败降级 / 空投票 /
单模型回退 / 异步 call_with_consensus / 异常降级 / timeout 降级 / 同步 wrapper。
"""
from __future__ import annotations

import asyncio
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import n_version


# ────────────────────────────────────────────────────────────────────
# 1. required_majority 数学
# ────────────────────────────────────────────────────────────────────

def test_required_majority_thresholds():
    """多数门槛：(N+1)//2，确保 N=1/2/3/5 都正确。"""
    assert n_version.required_majority(1) == 1
    assert n_version.required_majority(2) == 1
    assert n_version.required_majority(3) == 2
    assert n_version.required_majority(5) == 3
    assert n_version.required_majority(7) == 4


# ────────────────────────────────────────────────────────────────────
# 2. aggregate_votes 投票聚合
# ────────────────────────────────────────────────────────────────────

def test_aggregate_consensus_three_of_three():
    """3 票全一致 → consensus + confidence=1.0。"""
    votes = [
        {"answer": "BUY", "confidence": 0.6, "model": "or"},
        {"answer": "BUY", "confidence": 0.7, "model": "m2"},
        {"answer": "BUY", "confidence": 0.8, "model": "m3"},
    ]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "BUY"
    assert abs(r["confidence"] - 1.0) < 1e-6
    assert r["reason"] == "consensus"
    assert sorted(r["sources"]) == ["m2", "m3", "or"]


def test_aggregate_consensus_two_of_three():
    """3 票中 2 票 BUY，1 票 SELL → consensus BUY + confidence=2/3≈0.667。"""
    votes = [
        {"answer": "BUY", "confidence": 0.6, "model": "or"},
        {"answer": "BUY", "confidence": 0.7, "model": "m2"},
        {"answer": "SELL", "confidence": 0.9, "model": "m3"},
    ]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "BUY"
    assert abs(r["confidence"] - 0.6667) < 0.001
    assert r["reason"] == "consensus"
    assert "or" in r["sources"] and "m2" in r["sources"]


def test_aggregate_no_consensus_returns_best():
    """3 票各异（1/1/1）→ 无共识 → 返回 confidence 最高的答案。"""
    votes = [
        {"answer": "BUY", "confidence": 0.5, "model": "or"},
        {"answer": "SELL", "confidence": 0.9, "model": "m2"},
        {"answer": "HOLD", "confidence": 0.7, "model": "m3"},
    ]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "SELL"
    assert abs(r["confidence"] - 0.9) < 1e-6
    assert r["reason"] == "no_consensus"
    assert r["sources"] == ["m2"]


def test_aggregate_empty_votes():
    """空 votes → no_valid_votes + answer=""。"""
    r = n_version.aggregate_votes([])
    assert r["answer"] == ""
    assert r["reason"] == "no_valid_votes"
    assert r["confidence"] == 0.0


def test_aggregate_single_model_fallback():
    """单模型返回 → reason=single_model_fallback + confidence=原 confidence。"""
    votes = [{"answer": "BUY", "confidence": 0.7, "model": "or"}]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "BUY"
    assert abs(r["confidence"] - 0.7) < 1e-6
    assert r["reason"] == "single_model_fallback"


def test_aggregate_skips_empty_answers():
    """含空 answer 的 vote 应跳过，不计入投票。"""
    votes = [
        {"answer": "", "confidence": 0.5, "model": "or"},
        {"answer": "BUY", "confidence": 0.7, "model": "m2"},
    ]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "BUY"
    assert r["reason"] == "single_model_fallback"


def test_aggregate_case_insensitive():
    """answer 大小写不敏感归一。"""
    votes = [
        {"answer": "buy", "confidence": 0.5, "model": "or"},
        {"answer": "BUY", "confidence": 0.6, "model": "m2"},
        {"answer": "Buy", "confidence": 0.7, "model": "m3"},
    ]
    r = n_version.aggregate_votes(votes)
    assert r["answer"] == "BUY"
    assert r["reason"] == "consensus"


# ────────────────────────────────────────────────────────────────────
# 3. 异步 call_with_consensus 端到端
# ────────────────────────────────────────────────────────────────────

def _mk_models(labels):
    return [{"label": l, "id": f"model-{l}"} for l in labels]


async def _async_caller_factory(answers):
    """生成 caller：依次返回 answers 中的值（每调一次 next）。"""
    iter_holder = {"i": 0}

    async def _caller(prompt, model_cfg):
        i = iter_holder["i"]
        iter_holder["i"] = i + 1
        if i < len(answers):
            a = answers[i]
        else:
            a = {"answer": "BUY", "confidence": 0.5}
        if isinstance(a, Exception):
            raise a
        return {**a, "model": model_cfg.get("label")}

    return _caller


def test_async_call_three_models_all_agree():
    """3 模型全 agree BUY → consensus。"""
    async def _run():
        answers = [
            {"answer": "BUY", "confidence": 0.7},
            {"answer": "BUY", "confidence": 0.8},
            {"answer": "BUY", "confidence": 0.6},
        ]
        caller = await _async_caller_factory(answers)
        r = await n_version.call_with_consensus(
            "test prompt", _mk_models(["or", "m2", "m3"]), caller,
        )
        return r

    r = asyncio.run(_run())
    assert r["answer"] == "BUY"
    assert r["reason"] == "consensus"


def test_async_call_partial_failure_one_fails():
    """3 路 1 失败 + 2 一致 → consensus 仍成立（fail 不计入 votes）。"""
    async def _run():
        async def caller(prompt, model_cfg):
            if model_cfg["label"] == "m2":
                raise RuntimeError("m2 down")
            return {"answer": "BUY", "confidence": 0.7, "model": model_cfg["label"]}
        r = await n_version.call_with_consensus(
            "test", _mk_models(["or", "m2", "m3"]), caller,
        )
        return r

    r = asyncio.run(_run())
    assert r["answer"] == "BUY"
    assert r["reason"] == "consensus"
    assert r["used_models"] == ["or", "m3"]


def test_async_call_all_fail_returns_empty():
    """3 路全 fail → reason=all_failed + answer=""。"""
    async def _run():
        async def caller(prompt, model_cfg):
            raise RuntimeError(f"{model_cfg['label']} down")
        r = await n_version.call_with_consensus(
            "test", _mk_models(["or", "m2", "m3"]), caller,
        )
        return r

    r = asyncio.run(_run())
    assert r["answer"] == ""
    assert r["reason"] == "all_failed"
    assert r["confidence"] == 0.0


def test_async_call_timeout_is_failure():
    """单路 timeout（> timeout_per_model）→ 视为失败 → 不计入 votes。"""
    async def _run():
        async def caller(prompt, model_cfg):
            if model_cfg["label"] == "m2":
                await asyncio.sleep(5.0)  # 永远比 timeout 长
                return {"answer": "BUY"}
            return {"answer": "BUY", "confidence": 0.7, "model": model_cfg["label"]}
        r = await n_version.call_with_consensus(
            "test", _mk_models(["or", "m2"]), caller,
            timeout_per_model=0.2,
        )
        return r

    r = asyncio.run(_run())
    # m2 timeout, or 成功 → 单模型返回 → fallback
    assert r["answer"] == "BUY"
    assert r["reason"] == "single_model_fallback"
    assert r["used_models"] == ["or"]


def test_async_call_empty_models_returns_empty():
    """空 models 列表 → aggregate_votes([]) → no_valid_votes。"""
    async def _run():
        async def caller(prompt, model_cfg):
            return {"answer": "BUY"}
        r = await n_version.call_with_consensus("test", [], caller)
        return r

    r = asyncio.run(_run())
    assert r["reason"] == "no_valid_votes"
