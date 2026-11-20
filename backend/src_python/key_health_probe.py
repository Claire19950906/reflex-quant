# -*- coding: utf-8 -*-
"""key_health_probe — 用真实通道发一次最小请求，验证配额是否恢复（只读，不写决策）。

为什么需要它：`ai_call_stats` 里能看 429 的次数，但那是**事后统计**；
用户说"key 恢复了"，需要一次**当下**的验证 —— 真的能过，而不是历史里恰好没有 429。

用法：python key_health_probe.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db


def probe(alias_purpose: str = "gate") -> int:
    pool = [m for m in config.AI_MODEL_ROSTER
            if alias_purpose in (m.get("purpose") or [])]
    if not pool:
        print(f"没有 purpose={alias_purpose} 的通道")
        return 2
    m = pool[0]
    base = str(m.get("api_base") or "").rstrip("/")
    key = str(m.get("api_key") or "")
    model = str(m.get("request_model") or m.get("id") or "")
    label = str(m.get("label") or m.get("id"))
    if not base or not key:
        print(f"{label} 缺 api_base/api_key")
        return 2

    # v343: 探活也是真实计费请求，必须过速率闸门（与 api_server._probe_channel 一致）。
    # 之前这里裸发，被 audit_llm_gates.py 标为"未受保护的 LLM 调用点"
    # (test_gate_deferred_v320 因此一直红)。
    try:
        import llm_budget as _lb
        _ok, _why = _lb.consume_rate_slot(purpose="probe")
        if not _ok:
            print(f"{label} 速率闸门拦截: {_why}")
            return 4
    except Exception:
        pass  # 闸门自身异常 → 照常探，不因保护机制故障停摆

    import requests
    t0 = time.time()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "只回复两个字：可用"}]},
            timeout=30,
        )
    except Exception as e:
        print(f"{label} 请求异常: {type(e).__name__}: {e}")
        return 3

    ms = int((time.time() - t0) * 1000)
    if resp.status_code != 200:
        body = resp.text[:120]
        print(f"{label} HTTP {resp.status_code} | {body}")
        print("→ 配额/鉴权未恢复" if resp.status_code in (401, 402, 403, 429) else "→ 其它错误")
        _log(m, label, resp.status_code, 0, 0, ms)
        return 1

    body = resp.json()
    choices = body.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    usage = body.get("usage") or {}
    print(f"{label} HTTP 200 | {ms}ms | tokens_in={usage.get('prompt_tokens')} "
          f"tokens_out={usage.get('completion_tokens')} | 回复={text[:20]!r}")
    print("→ 通道可用 ✅")
    _log(m, label, 200, int(usage.get("prompt_tokens") or 0),
         int(usage.get("completion_tokens") or 0), ms)
    return 0


def _log(m, label, status, tin, tout, ms):
    try:
        db.log_ai_call(
            purpose="probe", key_alias=str(m.get("key_alias") or label),
            model_id=str(m.get("id") or "?"), tokens_in=tin, tokens_out=tout,
            latency_ms=ms, status="ok" if status == 200 else "http_error",
            error_code="" if status == 200 else str(status),
            call_site="key_health_probe",
        )
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1] if len(sys.argv) > 1 else "gate"))
