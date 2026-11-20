# -*- coding: utf-8 -*-
"""ai 配置域路由（结构切片，2026-11-17）。

从 api_server.py 搬来 13 条端点 + 2 个请求模型 + _ai_models_response 助手：
模型选择（/ai/models）、提示词库（/ai/prompts*）、品种规则（/ai/asset-rules*）。
逻辑逐字未改，只把 `@app.` 换成 `@router.`。依赖只有 config / ai_prompts / asyncio。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import ai_prompts
import config

router = APIRouter()


class AIModelSelection(BaseModel):
    model_id: str


def _ai_models_response() -> Dict[str, Any]:
    return {
        "models": [dict(model) for model in config.AI_MODEL_ROSTER],
        "selected": config.get_selected_ai_model_id(),
    }


@router.get("/api/ai/models")
async def get_ai_models():
    return _ai_models_response()


@router.put("/api/ai/models")
async def select_ai_model(selection: AIModelSelection):
    try:
        await asyncio.to_thread(config.write_selected_ai_model_id, selection.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported AI model") from exc
    return _ai_models_response()


# -- AI 分析 Prompt 库（主 prompt 只读展示 + 自定义 prompt 增删启停） -------

class AiPromptBody(BaseModel):
    name: str = ""
    content: str = ""


class AssetRuleBody(BaseModel):
    """品种专项分析规则（按金融品类单独设置）。"""
    asset: str = ""          # 品种代码（XAU/WTI/BTC/ETH/NVDA…）或 all
    asset_name: str = ""     # 显示名（可选，留空用内置映射）
    content: str = ""
    priority: int = 0


@router.get("/api/ai/prompts")
async def ai_prompts_view():
    """当前 AI 分析主 prompt（含页面编辑覆盖，标注来源）+ 使用状态 + 自定义 prompt 列表。"""
    from engine import ai_worker as _aw
    defaults = {
        "system_prompt": _aw._SYSTEM_PROMPT,
        "json_force_prompt": _aw._JSON_PROMPT_FORCE,
        "market_structure_block": _aw._MARKET_STRUCTURE_BLOCK,
        "doubao_prompt": _aw._DOUBAO_SYSTEM_PROMPT,
    }
    overrides = await asyncio.to_thread(ai_prompts.load_main_prompt_overrides, 0.0)
    main: Dict[str, Any] = {}
    for kind, dflt in defaults.items():
        ov = overrides.get(kind)
        main[kind] = {
            "content": ov if ov else dflt,
            "edited": bool(ov),
            "default": dflt,
        }
    # 使用状态：模型池（AI_MODEL_ROSTER 按 weight 展开）各自走哪个 prompt
    usage: Dict[str, Any] = {"system_prompt": [], "json_force_prompt": [], "doubao_prompt": []}
    try:
        from engine import ai_worker as _aw_mod
        for m in config.AI_MODEL_ROSTER:
            weight = max(1, int(m.get("weight", 1)))
            cfg = _aw_mod.build_model_config(m["id"])
            # 与 ai_worker 实际发送保持一致：主分析已“统一单通道”——除 Doubao 外
            # 所有模型都走 System Prompt（完整八段式），Prompt Force(json_force_prompt)
            # 通道已废弃。这里若按旧的 json_mode 判断会把这些模型全归到已废弃通道，
            # 导致提示词页把“生效通道”显示错。
            kind = (
                "doubao_prompt" if str(cfg.get("label", "")).lower().startswith("doubao")
                else "system_prompt"
            )
            usage[kind].append({"id": m["id"], "label": m["label"], "weight": weight})
    except Exception:
        pass
    # 盘面结构开关（附加块是否生效）
    try:
        from paper_trading import get_settings as _get_pt
        usage["market_structure_enabled"] = bool(_get_pt().get("market_structure_enabled", True))
    except Exception:
        usage["market_structure_enabled"] = True
    custom = await asyncio.to_thread(ai_prompts.list_prompts)
    asset_rules = await asyncio.to_thread(ai_prompts.list_asset_rules)
    return {
        "main": main,
        "usage": usage,
        "custom": custom,
        "asset_rules": asset_rules,
        "assets": ai_prompts.ASSET_DISPLAY,  # 品种代码→显示名（前端下拉）
    }


@router.put("/api/ai/prompts/main/{kind}")
async def ai_prompts_main_save(kind: str, body: AiPromptBody):
    """保存主 prompt 覆盖（kind: system_prompt/json_force_prompt/market_structure_block/doubao_prompt）。"""
    try:
        return await asyncio.to_thread(ai_prompts.save_main_prompt, kind, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/ai/prompts/main/{kind}")
async def ai_prompts_main_reset(kind: str):
    """删除主 prompt 覆盖 → 恢复代码内置默认。"""
    try:
        return await asyncio.to_thread(ai_prompts.reset_main_prompt, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/ai/prompts")
async def ai_prompts_add(body: AiPromptBody):
    try:
        return await asyncio.to_thread(ai_prompts.add_prompt, body.name, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/ai/prompts/{prompt_id}")
async def ai_prompts_del(prompt_id: int):
    return await asyncio.to_thread(ai_prompts.delete_prompt, prompt_id)


@router.patch("/api/ai/prompts/{prompt_id}/toggle")
async def ai_prompts_toggle(prompt_id: int):
    return await asyncio.to_thread(ai_prompts.toggle_prompt, prompt_id)


# -- 品种专项分析规则（ai_asset_rules：按金融品类单独设置分析规则）----------

@router.get("/api/ai/prompts/preview")
async def ai_prompts_preview(mode: str = "force"):
    """装配最终发给模型的 system prompt（含运行时分区），供提示库查看。

    提示库此前只显示主 prompt 常量原文，看不到运行时动态拼上的分区
    （盘面结构 / 用户自定义指令 / 品种专项规则 / 因果链 / 复盘经验），
    容易误以为"经验没加进 Prompt Force"。这里返回完整装配结果 + 分区明细。
    mode: force=非 JSON 模式（默认，主力通道） / system=JSON 模式
    """
    from engine import ai_worker as _aw
    mode = "system" if (mode or "").strip().lower() == "system" else "force"
    return await asyncio.to_thread(_aw.build_prompt_preview, mode)


@router.post("/api/ai/asset-rules")
async def ai_asset_rule_add(body: AssetRuleBody):
    """新增品种规则（立即生效，engine ≤5s 同步）。"""
    try:
        return await asyncio.to_thread(
            ai_prompts.add_asset_rule, body.asset, body.content,
            body.asset_name, body.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/ai/asset-rules/{rule_id}")
async def ai_asset_rule_update(rule_id: int, body: AssetRuleBody):
    """更新品种规则内容 / 显示名 / 顺序。"""
    try:
        return await asyncio.to_thread(
            ai_prompts.update_asset_rule, rule_id, body.content,
            body.asset_name, body.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/ai/asset-rules/{rule_id}")
async def ai_asset_rule_delete(rule_id: int):
    """删除品种规则。"""
    return await asyncio.to_thread(ai_prompts.delete_asset_rule, rule_id)


@router.patch("/api/ai/asset-rules/{rule_id}/toggle")
async def ai_asset_rule_toggle(rule_id: int):
    """切换品种规则启停。"""
    return await asyncio.to_thread(ai_prompts.toggle_asset_rule, rule_id)
