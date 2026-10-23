"""多因子异动 AI 归因（v182.3+）。

与 engine.ai_worker.attribute_5m_abnormal 类似但针对 multi_factor 输出：
- 输入是多个技术因子同时触发（factors 数组 + 指标字典），不是单根涨跌
- 同时给 sector_info（板块级联状态），辅助判断是「单资产异动」还是「板块事件」
- 输出多了 cross_asset_likely + actionable + severity_hint

调用方式：
    from engine.attribute_multi_factor import attribute_multi_factor
    result = attribute_multi_factor(asset="BTC", anchor_ts=..., direction=..., ...)
失败返回 {"error": str} — 调用方静默降级。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional


def _build_indicators_block(indicators: Dict[str, Any]) -> str:
    lines = []
    for k in ("rsi", "kdj", "mfi", "bollinger_break", "vol_z", "ma_cross", "candle_pattern", "atr_break"):
        v = indicators.get(k)
        if v is None:
            continue
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines) or "  (无指标)"


def _build_factors_block(factors: List[str]) -> str:
    if not factors:
        return "  (无因子)"
    return "\n".join(f"  - {f}" for f in factors[:10])


def _build_sector_block(sector_info: Optional[Dict[str, Any]]) -> str:
    if not sector_info:
        return "  (无板块信息)"
    return (
        f"  - sector_triggered: {sector_info.get('triggered')}\n"
        f"  - same_dir_ratio: {sector_info.get('same_dir_ratio', 0):.0%}\n"
        f"  - same_dir_assets: {sector_info.get('same_dir_assets', [])[:5]}"
    )


def _build_news_block(news_before: Optional[List[Dict[str, Any]]]) -> str:
    news_list = news_before or []
    if not news_list:
        return "  （时间锚前窗口内无该品种公开新闻记录）"
    return "\n".join(
        f"  [{i + 1}] {n.get('ts_str', '')} | {n.get('source', '')} | {n.get('content', '')}"
        for i, n in enumerate(news_list[:12])
    )


def _build_user_text(
    asset: str, ts_str: str, direction: str,
    factor_lines: str, indicator_lines: str, sec_lines: str, news_lines: str, graph_block: str,
) -> str:
    return (
        f"资产：{asset}\n"
        f"异动时间：{ts_str}\n"
        f"方向：{direction or '未明'}\n\n"
        f"触发的技术因子：\n{factor_lines}\n\n"
        f"关键指标：\n{indicator_lines}\n\n"
        f"板块状态：\n{sec_lines}\n\n"
        f"时间锚前公开新闻：\n{news_lines}\n\n"
        f"{graph_block}"
    )


_SYSTEM_PROMPT = (
    "你是严谨的多因子异动归因助手。给定资产多因子技术异动（多个指标同时触发），"
    "结合该时段新闻与板块状态，判断：(1) 最可能驱动事件 (2) 跨品种关联 (3) 行动建议。\n\n"
    "硬性要求：\n"
    "0. 若输入含「品种涨跌本质」背景，优先用它判断异动是否由核心/条件驱动触发；"
    "时间窗内若无与该驱动相关的新闻，判为技术性波动或未捕捉因素，严禁硬归因。\n"
    "1. 只引用「新闻列表」里出现的来源与内容，加上公开常识；不确定时列多个候选并按可能性排序。\n"
    "2. 严禁编造新闻标题、来源、具体数字；没有依据就写「未见直接催化，属市场自发波动」并标 speculative=true。\n"
    "3. 只输出一个 JSON 对象，不要任何解释性文字，格式严格为：\n"
    '{"summary":"一句话归因结论","drivers":[{"cause":"原因","source":"来源","confidence":"high|medium|low","speculative":false}],'
    '"cross_asset_likely":["X1","X2"],"actionable":"顺势|观望|对冲|减仓","severity_hint":1|2|3}\n'
    "drivers 最多 3 个；summary 不超过 60 字；severity_hint 1=轻度 2=中度 3=严重。"
)


def _parse_attr_response(obj: Any) -> Optional[Dict[str, Any]]:
    """从 LLM 返回的 JSON dict 解析归因结果；结构不符返回 None。"""
    if not isinstance(obj, dict):
        return None
    summary = str(obj.get("summary") or "").strip()
    drivers_in = obj.get("drivers")
    if not isinstance(drivers_in, list):
        drivers_in = []
    out_drivers: List[Dict[str, Any]] = []
    for d in drivers_in[:3]:
        if not isinstance(d, dict):
            continue
        cause = str(d.get("cause") or "").strip()
        if not cause:
            continue
        out_drivers.append({
            "cause": cause,
            "source": str(d.get("source") or "").strip(),
            "confidence": str(d.get("confidence") or "medium").strip(),
            "speculative": bool(d.get("speculative", False)),
        })
    cross_in = obj.get("cross_asset_likely")
    if not isinstance(cross_in, list):
        cross_in = []
    cross = [str(x).strip() for x in cross_in[:5] if str(x).strip()]
    actionable = str(obj.get("actionable") or "观望").strip()
    sev = obj.get("severity_hint")
    try:
        sev_int = int(sev) if sev is not None else 1
        if sev_int not in (1, 2, 3):
            sev_int = 1
    except Exception:
        sev_int = 1
    return {
        "summary": summary,
        "drivers": out_drivers,
        "cross_asset_likely": cross,
        "actionable": actionable,
        "severity_hint": sev_int,
    }


def attribute_multi_factor(
    asset: str,
    anchor_ts: int,
    direction: str,
    factors: List[str],
    indicators: Dict[str, Any],
    sector_info: Optional[Dict[str, Any]] = None,
    news_before: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """多因子异动 AI 归因。失败返回 {"error": str}。"""
    from engine import ai_worker as _aw

    cfgs: List[Dict[str, Any]] = []
    for m in _aw.AI_MODEL_ROSTER:
        try:
            c = _aw._resolve_model_cfg(m)
            if c:
                cfgs.append(c)
        except Exception:
            continue
    mm = _aw._minimax_fallback_cfg()
    if mm:
        cfgs.append(mm)
    mm2 = _aw._minimax_fallback_cfg2()
    if mm2:
        cfgs.append(mm2)
    if not cfgs:
        return {"error": "no model available"}

    factor_lines = _build_factors_block(factors)
    indicator_lines = _build_indicators_block(indicators)
    sec_lines = _build_sector_block(sector_info)
    news_lines = _build_news_block(news_before)
    ts_str = datetime.fromtimestamp(int(anchor_ts)).strftime("%Y-%m-%d %H:%M:%S")

    graph_block = ""
    try:
        import causal_graph
        graph_block = causal_graph.inject_block(asset)
    except Exception:
        graph_block = ""

    user_text = _build_user_text(
        asset, ts_str, direction,
        factor_lines, indicator_lines, sec_lines, news_lines, graph_block,
    )

    for cfg in cfgs:
        payload: Dict[str, Any] = {
            "model": str(cfg.get("request_model") or cfg.get("id")),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 600,
            "temperature": 0.1,
            **(cfg.get("extra_body") or {}),
        }
        if cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key')}",
            "Content-Type": "application/json",
        }
        url = f"{cfg.get('api_base')}/chat/completions"
        session = _aw._LLM_PROXY_SESSION if cfg.get("use_proxy") else _aw._LLM_SESSION
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code >= 400:
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not raw:
                continue
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            obj = json.loads(cleaned)
            parsed = _parse_attr_response(obj)
            if parsed:
                return parsed
        except Exception:
            continue
    return {"error": "all attribution models failed"}