# -*- coding: utf-8 -*-
"""Agent LLM 通道选择（从 api_server.py 下沉，2026-11-17）。

这两个包装器本身很短，但被 5 处端点共用（策略顾问、SQL copilot、优化器、反思）。
留在入口文件里会让任何用到 LLM 的 router 都必须反查 api_server 形成环，所以下沉。

口径与原来完全一致：
  · purpose=advisor/sql/optimizer → 走 minimax-reflect 共享 key，选专用模型；
  · 没配 key 时**抛 RuntimeError**（调用方靠这个异常回退到规则版），不静默降级；
  · 模型名取 request_model → id → config.AIPING_MODEL 三级回退。
"""
from __future__ import annotations

import config


def agent_llm_client(purpose: str = "advisor"):
    """Return the OpenAI-compatible client for a given purpose (v187)."""
    import openai

    m = config.pick_model_for_purpose(purpose)
    api_key = str(m.get("api_key") or "").strip()
    if not api_key or api_key == "local-no-auth":
        raise RuntimeError(f"No API key available for purpose={purpose} (id={m.get('id')})")
    return openai.OpenAI(base_url=str(m["api_base"]).rstrip("/"), api_key=api_key)


def agent_llm_model(purpose: str = "advisor") -> str:
    """Return the actual model name to send (purpose 路由专用模型)."""
    m = config.pick_model_for_purpose(purpose)
    return str(m.get("request_model") or m.get("id") or config.AIPING_MODEL)
