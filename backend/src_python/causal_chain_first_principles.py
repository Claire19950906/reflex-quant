# -*- coding: utf-8 -*-
"""因果链"第一性原理三问" (mechanism / precondition / invalidation)。

背景 (G07)
----------
causal_chains 表**有** mechanism / precondition / invalidation 三列, 但写入端从来没写过
(356 条链三列全是空串) —— 也就是需求 #8 说的"问到不能再问"根本没实现:
链只有一句 cause 和一句 effect, 既说不清为什么成立, 也说不清什么时候作废。

三问定义
--------
  mechanism    —— 这条链为什么成立? (传导路径: 触发条件 → 中间机制 → 价格/信号)
  precondition —— 在什么前提下才成立? (必须同时满足的条件, 可核对)
  invalidation —— 出现什么情况这条链就作废? (可观测的证伪信号)

实现
----
  · LLM 优先 (engine.ai_worker.chat, MiniMax-M3): 生成真正可读的三问;
  · 规则兜底 (derive_*): LLM 不可用/超时时不留空, 用链自身字段拼出可用文本。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

FIELD_LABELS = {
    "mechanism": "机制",
    "precondition": "前提",
    "invalidation": "证伪",
}

SYSTEM_PROMPT = (
    "你是量化研究里的因果链审查员。用户给你一条交易系统的因果链 (因→果), "
    "你要用第一性原理把它拆成三问: 机制 / 前提 / 证伪。"
    "要求: 只输出 JSON, 不要解释, 不要 markdown 代码块以外的内容。"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 回复里抠出 JSON (容忍 ```json 包裹 / <think> 块 / 前后废话)。"""
    if not text:
        return None
    raw = text.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"```json", "", raw)
    raw = re.sub(r"```", "", raw).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def derive_mechanism(chain: Dict[str, Any]) -> str:
    """规则兜底: 从链自身字段拼出机制说明。"""
    cause = _clean(chain.get("cause") or "")[:200]
    effect = _clean(chain.get("effect") or "")[:200]
    triggers = _clean(chain.get("trigger_words") or "")[:120]
    asset = _clean(chain.get("asset") or "") or "目标资产"
    direction = _clean(chain.get("expected_direction") or "") or "未限定方向"
    lo = chain.get("magnitude_lo")
    hi = chain.get("magnitude_hi")
    mag = ""
    try:
        if lo is not None and hi is not None:
            mag = f", 预期幅度 {float(lo):.1f}~{float(hi):.1f}%"
    except (TypeError, ValueError):
        mag = ""
    parts = []
    if triggers:
        parts.append(f"当新闻/信号出现「{triggers}」时触发")
    if cause:
        parts.append(f"因由是{cause}")
    if effect:
        parts.append(f"传导结果为{effect}")
    tail = f"作用对象 {asset}, 方向 {direction}{mag}"
    body = "；".join(parts) if parts else "该链由系统自动提炼, 机制尚未被人工确认"
    return _clean(f"{body}。{tail}。")


def derive_precondition(chain: Dict[str, Any]) -> str:
    """规则兜底: 前提条件。"""
    asset = _clean(chain.get("asset") or "") or "该资产"
    direction = _clean(chain.get("expected_direction") or "")
    conf = chain.get("confidence")
    bits = [
        f"{asset} 正常可交易且流动性未显著萎缩",
        "触发信号与链的 trigger_words 同向 (不是反向噪声)",
    ]
    if direction and direction != "BOTH":
        bits.append(f"价格方向与链预期一致 ({direction})")
    if conf is not None:
        try:
            bits.append(f"链自身置信度不低于 {float(conf):.2f}")
        except (TypeError, ValueError):
            pass
    bits.append("当前市场状态(regime)未切换到与本链相斥的状态")
    return _clean("前提条件: " + "；".join(bits) + "。")


def derive_invalidation(chain: Dict[str, Any]) -> str:
    """规则兜底: 证伪条件。"""
    asset = _clean(chain.get("asset") or "") or "该资产"
    lo = chain.get("magnitude_lo")
    try:
        thr = float(lo) if lo is not None else 1.0
    except (TypeError, ValueError):
        thr = 1.0
    thr = max(0.5, thr)
    return _clean(
        f"证伪条件: {asset} 在触发后 1 小时内反向波动超过 {thr:.1f}%, "
        f"或出现与 trigger_words 同频但方向相反的信号, 或市场状态切换到相斥 regime, "
        f"或链的命中率(hit_rate)连续低于 30% —— 任一成立则本链失效, 需重估权重。"
    )


def build_prompt(chain: Dict[str, Any]) -> str:
    trig = _clean(chain.get("trigger_words") or "")[:300]
    cause = _clean(chain.get("cause") or "")[:400]
    effect = _clean(chain.get("effect") or "")[:400]
    asset = _clean(chain.get("asset") or "")
    direction = _clean(chain.get("expected_direction") or "")
    lo, hi = chain.get("magnitude_lo"), chain.get("magnitude_hi")
    mag = ""
    try:
        if lo is not None and hi is not None:
            mag = f"{float(lo):.1f}~{float(hi):.1f}%"
    except (TypeError, ValueError):
        pass
    return (
        "因果链: " + _clean(chain.get("name") or "") + "\n"
        f"触发词: {trig}\n因: {cause}\n果: {effect}\n"
        f"资产: {asset}  预期方向: {direction}  预期幅度: {mag}\n\n"
        "请输出严格 JSON (三个字段都用中文, 每个 1~2 句, 必须具体可核对):\n"
        '{"mechanism": "为什么这条链成立: 触发条件→中间机制→价格影响的传导路径", '
        '"precondition": "成立前提: 必须同时满足的条件", '
        '"invalidation": "证伪条件: 出现什么可观测信号就说明这条链不成立"}\n'
    )


def llm_first_principles(chain: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """调用 LLM 生成三问; 失败返回 None (调用方走规则兜底)。"""
    try:
        from engine.ai_worker import chat
    except Exception:
        return None
    try:
        raw = chat(
            build_prompt(chain),
            purpose="advisor",
            system=SYSTEM_PROMPT,
            max_tokens=700,
            temperature=0.2,
            call_site="causal_first_principles",
        )
    except Exception:
        return None
    data = extract_json(raw or "")
    if not data:
        return None
    out: Dict[str, str] = {}
    for key in ("mechanism", "precondition", "invalidation"):
        val = _clean(str(data.get(key) or ""))
        if not val:
            return None
        out[key] = val[:1000 if key == "mechanism" else 600]
    return out


def first_principles_for_chain(chain: Dict[str, Any], use_llm: bool = True) -> Dict[str, str]:
    """给一条链产出三问 (LLM 优先, 规则兜底)。"""
    if use_llm:
        got = llm_first_principles(chain)
        if got:
            got["source"] = "llm"
            return got
    return {
        "mechanism": derive_mechanism(chain),
        "precondition": derive_precondition(chain),
        "invalidation": derive_invalidation(chain),
        "source": "rule",
    }


__all__ = [
    "FIELD_LABELS", "SYSTEM_PROMPT", "extract_json",
    "derive_mechanism", "derive_precondition", "derive_invalidation",
    "build_prompt", "llm_first_principles", "first_principles_for_chain",
]
