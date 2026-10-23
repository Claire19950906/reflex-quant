"""v175 — N-version 共识 + 降级。

对**关键决策**（复盘、重大事件分析、单笔归因校对）调 N 路独立 LLM 求多数表决。
普通扫描继续走单模型，避免费用爆炸。

设计目标：
  · N 路独立并发调用，每路 timeout + 异常均安全降级
  · 多数表决：至少 (N+1)/2 模型产出同一方向 → consensus 成立
  · 一致率不足：返回 best_answer + low_confidence + reason="no_consensus"
  · 至少 1 路 model 成功就至少返回 best_answer（绝不返回空）
"""
from __future__ import annotations

import asyncio
from collections import Counter
def aggregate_votes(
    votes: List[Dict[str, Any]],
    *,
    required_majority: Optional[int] = None,
) -> Dict[str, Any]:
    """N-version 投票聚合。"""
    if not votes:
        return {
            "answer": "",
            "confidence": 0.0,
            "sources": [],
            "used_models": [],
            "reason": "no_valid_votes",
            "vote_counts": {},
        }
    n = len(votes)
    req = required_majority if required_majority is not None else (n + 1) // 2
    counts: Counter = Counter()
    sources_by_answer: Dict[str, List[str]] = {}
    for v in votes:
        a = str(v.get("answer") or "").strip()
        if not a:
            continue
        norm = a.upper()
        counts[norm] += 1
        sources_by_answer.setdefault(norm, []).append(str(v.get("model") or "?"))
    used_models = [str(v.get("model") or "?") for v in votes]
    # "有效 votes"：answer 非空的数量（参与投票的实际样本）
    n_valid = sum(counts.values())
    if not counts:
        return {
            "answer": "",
            "confidence": 0.0,
            "sources": [],
            "used_models": used_models,
            "reason": "all_empty_answers",
            "vote_counts": {},
        }
    top_answer, top_count = counts.most_common(1)[0]
    # 单模型永远视为 fallback（无共识意义）
    if n_valid == 1:
        only = votes[0] if votes else {"answer": top_answer}
        return {
            "answer": top_answer,
            "confidence": round(float(only.get("confidence") or 0.0), 4),
            "sources": sources_by_answer.get(top_answer, [str(only.get("model") or "?")]),
            "used_models": used_models,
            "reason": "single_model_fallback",
            "vote_counts": dict(counts),
        }
    req = required_majority if required_majority is not None else (n_valid + 1) // 2
    if top_count >= req:
        confidence = round(top_count / n_valid, 4)
        return {
            "answer": top_answer,
            "confidence": float(confidence),
            "sources": sources_by_answer[top_answer],
            "used_models": used_models,
            "reason": "consensus",
            "vote_counts": dict(counts),
        }
    best = max(
        (v for v in votes if str(v.get("answer") or "").strip()),
        key=lambda v: float(v.get("confidence") or 0.0),
        default=None,
    )
    if best is None:
        best = votes[0]
    return {
        "answer": str(best.get("answer") or "").strip().upper(),
        "confidence": round(float(best.get("confidence") or 0.0), 4),
        "sources": [str(best.get("model") or "?")],
        "used_models": used_models,
        "reason": (
            "single_model_fallback" if n == 1 else "no_consensus"
        ),
        "vote_counts": dict(counts),
    }


def required_majority(n: int) -> int:
    """多数门槛 = (N+1)//2。N=1→1；N=2→1；N=3→2；N=5→3。"""
    return max(1, (n + 1) // 2)
async def _gather_votes(
    prompt: str,
    models: List[Dict[str, Any]],
    caller: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    *,
    timeout_per_model: float = 10.0,
) -> List[Dict[str, Any]]:
    """并发调 N 路，每路 timeout=timeout_per_model 秒。"""
    async def _one(model_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            res = await asyncio.wait_for(
                caller(prompt, model_cfg),
                timeout=timeout_per_model,
            )
            if not isinstance(res, dict):
                return None
            if not str(res.get("answer") or "").strip():
                return None
            res.setdefault("model", str(model_cfg.get("label") or model_cfg.get("id") or "?"))
            res.setdefault("confidence", 0.5)
            return res
        except Exception:
            return None

    results = await asyncio.gather(
        *[_one(m) for m in models], return_exceptions=False,
    )
    return [r for r in results if r is not None]


async def call_with_consensus(
    prompt: str,
    models: List[Dict[str, Any]],
    caller: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
    *,
    timeout_per_model: float = 10.0,
    required_majority: Optional[int] = None,
) -> Dict[str, Any]:
    """N-version 共识端到端：并发调 N 路 → aggregate_votes 求多数 → 返回 result。"""
    if not models:
        return aggregate_votes([])
    votes = await _gather_votes(
        prompt, models, caller, timeout_per_model=timeout_per_model,
    )
    if not votes:
        return {
            "answer": "",
            "confidence": 0.0,
            "sources": [],
            "used_models": [],
            "reason": "all_failed",
            "vote_counts": {},
        }
    agg = aggregate_votes(votes, required_majority=required_majority)
    return agg


def call_with_consensus_sync(
    prompt: str,
    models: List[Dict[str, Any]],
    caller: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    *,
    timeout_per_model: float = 10.0,
    required_majority: Optional[int] = None,
) -> Dict[str, Any]:
    """同步版：caller 是普通函数（不返回 coroutine）。"""
    async def _async_caller(p: str, m: Dict[str, Any]) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, caller, p, m)

    return asyncio.run(
        call_with_consensus(
            prompt, models, _async_caller,
            timeout_per_model=timeout_per_model,
            required_majority=required_majority,
        )
    )
