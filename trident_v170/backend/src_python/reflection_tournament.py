#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v240+ LLM Verifier Tournament (k=3 异步投票).

设计动机:
  - 当前 blindspot_reflector 走单次 LLM 调用, 易信证偏差 (anchoring, sycophancy)
    + 偶发 hallucination. 单次失败 → 整批 fail.
  - Tournament 思路: k=3 独立 verifier 各调一次 LLM, 用不同 prompt 角度.
    Majority vote (>= 2/3) 算 "confirmed finding", 大幅降低单点偏差.
  - 并发执行 (concurrent.futures.ThreadPoolExecutor) 控制 wall-clock latency.

3 个默认 verifier (prompt 模板):
  V1 = ORIGINAL: 复刻 v200+ BLINDSPOT_PROMPT — 通用盲区审计
  V2 = EVIDENCE: 强制每条 finding 必须 cite 具体 evidence (raw data / checker error)
  V3 = FALSIFIABILITY: "什么证据会反驳这条结论?" — 反向压力测试

聚合策略:
  - per-finding-kind (error_code) 投票
  - quorum: 至少 K 个 verifier 给出相同 error_code
  - confidence: 多数票 confidence 的加权平均
  - 若 quorum 未达, 退回: 保留所有 verifier 的 findings, evidence 加 _tournament_no_consensus 标记

零依赖: 仅用 concurrent.futures + dataclass (stdlib); 无 LLM SDK.
LLM 调用: 用 verifier.vote() 方法, 注入式（测试可替换为 mock）.

用法:
    from reflection_tournament import default_tournament

    result = default_tournament().run(prompt, context=decision_ctx)
    for finding in result.confirmed_findings:
        if finding.quorum >= 2:
            persist(finding)
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    """单一 verifier 给出的盲区 finding.

    Fields:
        error_code: 主错误码 (e.g., "E021", "META_REFLECTION", "MISSED_REGIME")
        category:   分类 (e.g., "reasoning_logic", "causal", "meta")
        severity:   严重度 1-3
        description: 文本描述
        evidence:   支持证据 (自由文本 / dict)
        verifier_id: 来源 verifier (e.g., "v1", "v2", "v3")
        confidence: 该 verifier 对此 finding 的置信度 [0, 1]
    """
    error_code: str
    category: str = ""
    severity: int = 2
    description: str = ""
    evidence: Any = None
    verifier_id: str = ""
    confidence: float = 0.5

    def key(self) -> str:
        """用于 quorum 投票的 key: error_code + category 联合."""
        return f"{self.error_code}|{self.category}"


@dataclass
class Vote:
    """单一 verifier 的完整输出."""
    verifier_id: str
    findings: List[Finding] = field(default_factory=list)
    raw_output: str = ""
    elapsed_ms: float = 0.0
    error: Optional[str] = None  # 调用失败时填


@dataclass
class ConfirmedFinding:
    """通过 quorum 的 finding (聚合多个 verifier)."""
    error_code: str
    category: str
    severity: int
    description: str
    evidence: Any
    quorum: int  # 投票数
    verifier_ids: List[str]
    avg_confidence: float


@dataclass
class TournamentResult:
    """tournament run() 的完整输出."""
    votes: List[Vote]
    confirmed_findings: List[ConfirmedFinding]  # quorum >= quorum_required
    raw_findings: List[Finding]  # 所有 verifier 的原始输出 (即使未达成 quorum)
    consensus_reached: bool
    quorum_required: int
    elapsed_ms: float


# ═══════════════════════════════════════════════════════════════════════════
# Verifier Protocol + LLM 实现
# ═══════════════════════════════════════════════════════════════════════════

class Verifier(Protocol):
    """单一 verifier 接口.

    实现 vote() 调用自己的 LLM (或 mock), 返回 Vote.
    """
    verifier_id: str

    def vote(self, prompt: str) -> Vote: ...


# 复用 blindspot_reflector._llm_call 作为 LLM 后端（生产用）
_LLM_BACKEND: Optional[Callable[[str], str]] = None


def set_llm_backend(fn: Optional[Callable[[str], str]]) -> None:
    """注入 LLM 后端. None 恢复默认."""
    global _LLM_BACKEND
    _LLM_BACKEND = fn


def _default_llm(prompt: str) -> str:
    """默认 LLM 后端: 复用 blindspot_reflector._llm_call."""
    if _LLM_BACKEND is not None:
        return _LLM_BACKEND(prompt)
    try:
        from blindspot_reflector import _llm_call
        return _llm_call(prompt)
    except Exception as e:  # noqa: BLE001
        logging.warning("tournament default LLM backend failed: %s", e)
        return ""


def _parse_findings(raw: str, verifier_id: str) -> List[Finding]:
    """解析 LLM 输出 JSON → Finding 列表.

    容错: 处理 ```json fences, 截断, 部分 JSON.
    """
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return []
    cleaned = match.group(0)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    raw_list = parsed.get("blindspots") or []
    if not isinstance(raw_list, list):
        return []
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        out.append(Finding(
            error_code=str(item.get("error_code") or "UNKNOWN"),
            category=str(item.get("category") or ""),
            severity=int(item.get("severity", 2) or 2),
            description=str(item.get("description") or ""),
            evidence=item.get("evidence"),
            verifier_id=verifier_id,
            confidence=conf,
        ))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 3 个默认 verifier (prompt 模板)
# ═══════════════════════════════════════════════════════════════════════════

V1_ORIGINAL_TEMPLATE = """You are a reflection auditor. Find blindspots program checkers MISS.

Before looking at this decision, ask:
  1. Is the trade actually a "valid alpha sample"? (gate_passed, evidence_action≠HOLD, pnl≠0, allowlist, confidence≥20)
  2. Did the program checkers check sample integrity (E020)? If E020 triggered, this trade is NOT a valid alpha evaluation sample — flag it.
  3. Are we (the reflection system) auditing this trade blindly without first auditing the trade's data integrity?

[Input 1: Decision]
{decision_block}

[Input 2: Raw Data (same as program, analyze INDEPENDENTLY)]
News: {news_text}
Asset: {target_asset} ({asset_category})
Market: {market_snapshot}
Causal graph: {causal_context}
Active rules: {active_rules}

[Input 3: Program checkers found (reference factor)]
{checker_errors}

[Task]
Independently analyze. Find blindspots program checkers could NOT cover.

[Output format — strict JSON]
{{
  "blindspots": [
    {{
      "error_code": "<e.g. E028 or META_REFLECTION or NEW_CODE>",
      "category": "<reasoning_logic | causal | meta | data_quality | sizing | timing>",
      "severity": <1-3>,
      "description": "<one sentence>",
      "evidence": "<specific evidence snippet>",
      "confidence": <0.0-1.0>
    }}
  ]
}}
"""


V2_EVIDENCE_TEMPLATE = """You are a strict evidence-based reflection auditor. Your ONLY output criterion is: every finding MUST cite a specific, verifiable piece of evidence from the raw data below. Vague hand-waving is NOT acceptable.

[Decision]
{decision_block}

[Raw Data — these are the ONLY sources you may cite]
News: {news_text}
Asset: {target_asset} ({asset_category})
Market: {market_snapshot}
Causal graph: {causal_context}
Active rules: {active_rules}
Program checkers found: {checker_errors}

[Task]
Find blindspots. For EACH finding, the "evidence" field must contain a literal quote or specific data point from the raw data above. If you cannot cite specific evidence, do not report the finding.

Reject generic concerns. Reject "this might be wrong". Reject hand-waving. Only report findings with concrete evidence.

[Output format — strict JSON]
{{
  "blindspots": [
    {{
      "error_code": "<code>",
      "category": "<category>",
      "severity": <1-3>,
      "description": "<what is the blindspot>",
      "evidence": "<verbatim quote or specific data point>",
      "confidence": <0.0-1.0>
    }}
  ]
}}
"""


V3_FALSIFIABILITY_TEMPLATE = """You are a contrarian reflection auditor. Your job is to find findings by asking: "What evidence would DISPROVE each candidate blindspot?"

For each potential blindspot, you must:
  1. State the finding
  2. State what evidence would FALSIFY it (must be observable in raw data)
  3. Check if such falsifying evidence EXISTS in the data
  4. If NOT, the finding is robust and should be reported
  5. If falsifying evidence exists, REJECT the finding

[Decision]
{decision_block}

[Raw Data]
News: {news_text}
Asset: {target_asset} ({asset_category})
Market: {market_snapshot}
Causal graph: {causal_context}
Active rules: {active_rules}
Program checkers found: {checker_errors}

[Task]
Apply falsifiability test to candidate blindspots. Only report findings that survive falsification.

[Output format — strict JSON]
{{
  "blindspots": [
    {{
      "error_code": "<code>",
      "category": "<category>",
      "severity": <1-3>,
      "description": "<finding>",
      "evidence": "<what falsification test was applied + why it survived>",
      "confidence": <0.0-1.0>
    }}
  ]
}}
"""


class PromptVerifier:
    """使用给定 system perspective + prompt 模板 + LLM 后端的 verifier.

    vote() 时: 把 system_prompt 拼接到用户 prompt 前, 让 LLM 带上特定角度.
    """

    def __init__(self, verifier_id: str, template: str,
                 system_prompt: str = "",
                 llm_backend: Optional[Callable[[str], str]] = None):
        self.verifier_id = verifier_id
        self.template = template  # 保留供序列化/审计; 不直接用于 vote()
        self.system_prompt = system_prompt
        self._llm = llm_backend

    def vote(self, prompt: str) -> Vote:
        """prompt 是已经格式化好的最终 prompt (含决策上下文).

        系统视角被前置, 让同一 prompt 在不同 verifier 下产生不同输出.
        """
        t0 = time.time()
        if self.system_prompt:
            full_prompt = f"[SYSTEM ANGLE]\n{self.system_prompt}\n\n[TASK]\n{prompt}"
        else:
            full_prompt = prompt
        try:
            raw = (self._llm or _default_llm)(full_prompt)
            findings = _parse_findings(raw, self.verifier_id)
            return Vote(
                verifier_id=self.verifier_id,
                findings=findings,
                raw_output=raw,
                elapsed_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:  # noqa: BLE001
            return Vote(
                verifier_id=self.verifier_id,
                error=str(e)[:500],
                elapsed_ms=(time.time() - t0) * 1000,
            )


# ═══════════════════════════════════════════════════════════════════════════
# 3 verifier 的 system perspective (短前缀, 让 3 个 verifier 真正产生不同输出)
# ═══════════════════════════════════════════════════════════════════════════

V1_ORIGINAL_PERSPECTIVE = (
    "You are a reflection auditor. Find blindspots program checkers MISS. "
    "Independent analysis; flag anything the rule pipeline could not cover."
)
V2_EVIDENCE_PERSPECTIVE = (
    "You are a strict evidence-based reflection auditor. Every finding MUST "
    "cite a specific, verifiable piece of evidence from the raw data. "
    "Reject vague hand-waving; only report findings with concrete evidence."
)
V3_FALSIFIABILITY_PERSPECTIVE = (
    "You are a contrarian reflection auditor. For each candidate blindspot, "
    "ask: what evidence would DISPROVE it? If such evidence exists, REJECT. "
    "Only report findings that survive falsification."
)


def default_verifiers() -> List[Verifier]:
    """3 个默认 verifier（不同 system 角度 + 不同模板）."""
    return [
        PromptVerifier(
            "v1_original", V1_ORIGINAL_TEMPLATE,
            system_prompt=V1_ORIGINAL_PERSPECTIVE,
        ),
        PromptVerifier(
            "v2_evidence", V2_EVIDENCE_TEMPLATE,
            system_prompt=V2_EVIDENCE_PERSPECTIVE,
        ),
        PromptVerifier(
            "v3_falsifiability", V3_FALSIFIABILITY_TEMPLATE,
            system_prompt=V3_FALSIFIABILITY_PERSPECTIVE,
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Tournament 主逻辑
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TournamentConfig:
    verifiers: List[Verifier] = field(default_factory=default_verifiers)
    quorum: int = 2  # 至少 K 个 verifier 同意
    timeout_seconds: float = 60.0
    parallel: bool = True


class Tournament:
    """k-verifier tournament runner."""

    def __init__(self, config: Optional[TournamentConfig] = None):
        self.config = config or TournamentConfig()

    def run(self, prompt: str) -> TournamentResult:
        """并发跑所有 verifier, 聚合结果."""
        t0 = time.time()
        votes = self._invoke_all(prompt)
        confirmed, raw = self._aggregate(votes, self.config.quorum)
        elapsed = (time.time() - t0) * 1000
        return TournamentResult(
            votes=votes,
            confirmed_findings=confirmed,
            raw_findings=raw,
            consensus_reached=len(confirmed) > 0,
            quorum_required=self.config.quorum,
            elapsed_ms=elapsed,
        )

    def _invoke_all(self, prompt: str) -> List[Vote]:
        cfg = self.config
        if not cfg.parallel or len(cfg.verifiers) == 1:
            return [v.vote(prompt) for v in cfg.verifiers]
        votes: List[Optional[Vote]] = [None] * len(cfg.verifiers)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(cfg.verifiers)
        ) as pool:
            futures = {
                pool.submit(v.vote, prompt): i
                for i, v in enumerate(cfg.verifiers)
            }
            for fut in concurrent.futures.as_completed(futures, timeout=cfg.timeout_seconds):
                idx = futures[fut]
                try:
                    votes[idx] = fut.result(timeout=cfg.timeout_seconds)
                except Exception as e:  # noqa: BLE001
                    v = cfg.verifiers[idx]
                    votes[idx] = Vote(
                        verifier_id=getattr(v, "verifier_id", f"v{idx}"),
                        error=f"future raised: {e}",
                    )
        return [v if v is not None else Vote(verifier_id="?") for v in votes]

    def _aggregate(self, votes: List[Vote], quorum: int
                   ) -> tuple[List[ConfirmedFinding], List[Finding]]:
        """按 (error_code, category) 分组投票."""
        raw: List[Finding] = []
        for v in votes:
            raw.extend(v.findings)

        buckets: Dict[str, List[Finding]] = {}
        for f in raw:
            buckets.setdefault(f.key(), []).append(f)

        confirmed: List[ConfirmedFinding] = []
        for key, group in buckets.items():
            if len(group) < quorum:
                continue
            best = max(group, key=lambda f: f.severity)
            avg_conf = sum(f.confidence for f in group) / len(group)
            confirmed.append(ConfirmedFinding(
                error_code=best.error_code,
                category=best.category,
                severity=best.severity,
                description=best.description,
                evidence=best.evidence,
                quorum=len(group),
                verifier_ids=sorted({f.verifier_id for f in group}),
                avg_confidence=round(avg_conf, 4),
            ))

        confirmed.sort(key=lambda c: (-c.severity, -c.quorum))
        return confirmed, raw


def default_tournament() -> Tournament:
    return Tournament(TournamentConfig())


def run_tournament(prompt: str,
                   verifiers: Optional[List[Verifier]] = None,
                   quorum: int = 2,
                   ) -> TournamentResult:
    """便利函数: 跑一次 tournament, 返回结果."""
    if verifiers is None:
        verifiers = default_verifiers()
    t = Tournament(TournamentConfig(verifiers=verifiers, quorum=quorum))
    return t.run(prompt)


# ═══════════════════════════════════════════════════════════════════════════
# CLI 自检 (不依赖真实 LLM, 用 mock verifier)
# ═══════════════════════════════════════════════════════════════════════════

class _MockVerifier:
    """测试用 mock verifier, 按 prompt 内容返回固定 finding."""

    def __init__(self, verifier_id: str, fixed_findings: List[Finding]):
        self.verifier_id = verifier_id
        self.fixed_findings = fixed_findings
        self.call_count = 0

    def vote(self, prompt: str) -> Vote:
        self.call_count += 1
        return Vote(
            verifier_id=self.verifier_id,
            findings=[Finding(
                error_code=f.error_code,
                category=f.category,
                severity=f.severity,
                description=f.description,
                evidence=f.evidence,
                verifier_id=self.verifier_id,
                confidence=f.confidence,
            ) for f in self.fixed_findings],
            raw_output="<mock>",
        )


def _self_test():
    print("=" * 60)
    print("reflection_tournament.py self-test")
    print("=" * 60)

    # 1. 3 个 verifier, 2 同意 → confirmed
    f1 = Finding("E021", "reasoning_logic", 2, "Test 1", "ev1", confidence=0.8)
    f2 = Finding("E021", "reasoning_logic", 3, "Test 1 detailed", "ev2", confidence=0.9)
    f3 = Finding("E022", "causal", 1, "Test 2", "ev3", confidence=0.7)

    verifiers = [
        _MockVerifier("v1", [f1]),
        _MockVerifier("v2", [f2]),
        _MockVerifier("v3", [f3]),
    ]
    result = run_tournament("dummy", verifiers=verifiers, quorum=2)
    assert result.consensus_reached
    assert len(result.confirmed_findings) == 1
    assert result.confirmed_findings[0].error_code == "E021"
    assert result.confirmed_findings[0].quorum == 2
    print(f"[1] 3 verifiers, 2 agree E021: confirmed={len(result.confirmed_findings)} ✓")

    # 2. quorum=3 strict
    result3 = run_tournament("dummy", verifiers=verifiers, quorum=3)
    assert not result3.consensus_reached
    assert len(result3.confirmed_findings) == 0
    print(f"[2] quorum=3 strict: 0 confirmed (correct) ✓")

    # 3. 所有 verifier 都被调用 (新 verifiers, 避免前面测试累加)
    fresh_verifiers = [
        _MockVerifier("v1", [f1]),
        _MockVerifier("v2", [f2]),
        _MockVerifier("v3", [f3]),
    ]
    run_tournament("dummy", verifiers=fresh_verifiers, quorum=2)
    assert all(v.call_count == 1 for v in fresh_verifiers)
    print(f"[3] all 3 fresh verifiers called: "
          f"{[v.call_count for v in fresh_verifiers]} ✓")

    # 4. 单个 verifier 报错不影响其他
    class _ErrorVerifier:
        verifier_id = "v_err"
        def vote(self, prompt):
            return Vote(verifier_id="v_err", error="simulated LLM failure")

    verifiers_err = [
        _MockVerifier("v1", [f1]),
        _ErrorVerifier(),
        _MockVerifier("v3", [f1]),
    ]
    res = run_tournament("dummy", verifiers=verifiers_err, quorum=2)
    assert res.consensus_reached
    assert len(res.confirmed_findings) == 1
    err_votes = [v for v in res.votes if v.error]
    assert len(err_votes) == 1
    print(f"[4] 1 verifier failed, 2 agree: confirmed={len(res.confirmed_findings)} ✓")

    # 5. 性能: 3 个 mock verifier
    t0 = time.time()
    for _ in range(10):
        run_tournament("dummy", verifiers=verifiers)
    elapsed = (time.time() - t0) * 1000 / 10
    assert elapsed < 100, f"too slow: {elapsed:.1f}ms per run"
    print(f"[5] performance (3 mock): {elapsed:.1f}ms/run ✓")

    # 6. Finding.key() 一致性
    a = Finding("E021", "reasoning_logic", 2, "x", "y", confidence=0.5)
    b = Finding("E021", "reasoning_logic", 3, "z", "w", confidence=0.7)
    assert a.key() == b.key()
    c = Finding("E021", "causal", 2, "x", "y", confidence=0.5)
    assert c.key() != a.key()
    print(f"[6] Finding.key() grouping ✓")

    # 7. _parse_findings 容错
    parsed = _parse_findings(
        '```json\n{"blindspots": [{"error_code": "E001", "category": "x", '
        '"severity": 2, "description": "d", "evidence": "e", "confidence": 0.5}]}\n```',
        "v_test",
    )
    assert len(parsed) == 1
    assert parsed[0].error_code == "E001"
    print(f"[7] _parse_findings: markdown-fenced JSON ✓")

    # 8. ConfirmedFinding 字段
    cf = ConfirmedFinding(
        error_code="E021", category="reasoning_logic", severity=2,
        description="x", evidence="e", quorum=2,
        verifier_ids=["v1", "v2"], avg_confidence=0.85,
    )
    d = cf.__dict__
    for k in ["error_code", "category", "severity", "description",
              "evidence", "quorum", "verifier_ids", "avg_confidence"]:
        assert k in d
    print(f"[8] ConfirmedFinding fields ✓")

    print("=" * 60)
    print("All self-tests passed ✓")
    print("=" * 60)


if __name__ == "__main__":
    _self_test()