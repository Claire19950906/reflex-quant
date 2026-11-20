# -*- coding: utf-8 -*-
"""v453: 洞察生成侧的硬门槛 —— 让每条洞察入库时就带数值、带方向、可映射。

为什么
------
`review_agent` 的生成提示词早就写了"必须可被验证"（⑦⑧：数值区间 + 具体数值单位 +
窗口 + 方括号参数键），实测模型仍常漏：全库 71 条 active 洞察里 66 条只能观察，
其中 22 条连参数键都没有。提示词是"软约束"，模型可以不听；于是需要一道**代码侧
的硬门槛**：

  1. 生成后逐条体检（`check`）：有没有数值、有没有方向、有没有参数键、有没有窗口；
  2. 不合格的**带着缺项清单让模型重写一次**（保留原意，不许新造结论）；
  3. 重写仍不合格 → **不入库**，写一条审计日志说明为什么丢。

设计边界（避免把"洞察"变成"编数字"）
------------------------------------
· 重写只允许把原文已有的量补全成可核对形式（如"胜率低"→"胜率 41%"必须来自样本），
  提示词里明确写"只改写形式，不得引入原文没有的数据"；
· 缺数值的规则宁可不入库，也不许"看起来可测"。这是系统一直守的 `NEVER_FABRICATE`。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# 参数键：允许带资产前缀（[XAU] [score.buy_min_score] ...），也允许裸键
_KEY_RE = re.compile(
    r"(?:\[[A-Z_*]+\]\s*)?\[?(sizing|score|filter|analysis)\.[a-z0-9_.]+\]?"
)
# 数值：0.4 / 40% / 0.6% / 3 笔 / 72h 都算；纯年份(2026)不算
_NUM_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:%|个百分点|倍|笔|小时|h|分钟|min|天|d))|(0\.\d+)|(\d+(?:\.\d+)?)"
)
_UP_WORDS = ("提高", "上调", "抬高", "放宽", "增加", "加大", "提升", "看涨", "做多")
_DOWN_WORDS = ("降低", "下调", "收窄", "收紧", "减少", "减小", "压缩", "看跌", "做空")
_WINDOW_RE = re.compile(r"(\d+)\s*(小时|h|分钟|min|天|d)")

# 元规则 / 治理类：讲流程、口径、数据纪律，不构成"参数动作"或"行情判断"。
# 这类洞察是系统的护栏（如"绝不臆造数据"），不该被数值门槛误杀。
_META_WORDS = ("数据源", "字段", "口径", "流程", "校验", "审计", "记录", "标注",
               "样本", "提示词", "模型通道", "结构", "评分", "推送", "可追溯",
               "绝不", "不得", "禁止", "不允许", "必须")
# 策略类：一旦出现这些词，就是在给行情判断或参数动作，必须能量化。
_STRATEGY_WORDS = ("仓位", "止损", "止盈", "胜率", "门槛", "信号", "买入", "卖出",
                   "建仓", "开仓", "平仓", "做多", "做空", "看涨", "看跌",
                   "置信度", "权重", "追高", "回调", "突破", "走强", "走弱")


def _has_number(rule: str) -> bool:
    for m in _NUM_RE.finditer(rule or ""):
        token = m.group(0).strip()
        # 过滤纯年份/纯 id 这类噪音：四位整数且没有单位
        if not m.group(1) and not m.group(2):
            if len(token) == 4 and token.startswith(("19", "20")):
                continue
            if token in ("0", "1"):
                continue
        return True
    return False


def check(rule: str) -> Dict[str, Any]:
    """体检一条洞察文本。返回 ok / level / 缺项清单。"""
    text = str(rule or "").strip()
    has_key = bool(_KEY_RE.search(text))
    has_num = _has_number(text)
    has_dir = any(w in text for w in _UP_WORDS) or any(w in text for w in _DOWN_WORDS)
    has_window = bool(_WINDOW_RE.search(text))

    gaps: List[str] = []
    if not has_num:
        gaps.append("无数值")
    if not has_dir:
        gaps.append("无方向(提高/降低或看涨/看跌)")
    if not has_key:
        gaps.append("无参数键")

    # 分级：
    #   全齐            → actionable（能直接进参数落地）
    #   数值+方向无键    → narrative（可存，但注定 observe_only）
    #   元规则/治理类    → meta（护栏类，允许不带数值，别误杀）
    #   其余（策略判断却没数值/没方向）→ reject（重写或丢弃）
    is_strategy = any(w in text for w in _STRATEGY_WORDS)
    is_meta = any(w in text for w in _META_WORDS)

    if has_num and has_dir and has_key:
        level = "actionable"
    elif has_num and has_dir:
        level = "narrative"
    elif is_meta and not is_strategy:
        level = "meta"
    else:
        level = "reject"
    return {
        "ok": level != "reject",
        "level": level,
        "has_key": has_key,
        "has_number": has_num,
        "has_direction": has_dir,
        "has_window": has_window,
        "gaps": gaps,
    }


_REWRITE_PROMPT = (
    "下面这些经验规则**不可验证**（缺数值 / 缺方向 / 缺参数键），请严格按原意改写为可验证形式，"
    "输出 JSON 数组（条数不变，顺序不变）。\n"
    "改写规则：\n"
    "①只允许把原文里已有的判断补成可核对的形式，**不得引入原文没有的数据**；"
    "原文没有具体数值的，用样本里能支持的量级（如「胜率低于 45%」），仍写不出就原样返回，不要编。\n"
    "②方向必须明确：提高/降低（参数）或看涨/看跌（方向）。\n"
    "③能落到参数的写方括号前缀：[score.buy_min_score] / [score.sell_min_score] / "
    "[sizing.position_weight] / [sizing.stop_loss_pct] / [sizing.take_profit_pct] / "
    "[sizing.max_position_pct] / [score.confidence_threshold]。\n"
    "④写明适用窗口（如 24h / 72h）。\n"
    "⑤每条 <=60 字。\n\n"
    "待改写：\n"
)


def enforce(
    rules: List[str],
    llm_call: Optional[Callable[..., Any]] = None,
    label: str = "",
    log_path: Optional[str] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """体检 + 一次重写 + 最终裁决。

    llm_call: 与 review_agent._llm 同签名 (prompt, system, max_tokens=...)->str|dict
    返回 (kept_rules, report)
    """
    raw = [str(r).strip() for r in (rules or []) if str(r).strip()]
    if not raw:
        return [], {"checked": 0, "kept": 0, "rewritten": 0, "dropped": 0, "drop_detail": []}

    reports = [check(r) for r in raw]
    kept = [r for r, c in zip(raw, reports) if c["ok"]]
    reject = [r for r, c in zip(raw, reports) if not c["ok"]]
    rewritten = 0
    unresolved = list(reject)

    if reject and llm_call is not None:
        payload = "\n".join(f"{i+1}. {r}\n   缺：{', '.join(check(r)['gaps'])}"
                            for i, r in enumerate(reject))
        try:
            resp = llm_call(
                _REWRITE_PROMPT + payload,
                "你是量化规则编辑，只做形式改写，绝不新增原文没有的数据。只输出 JSON 数组。",
                max_tokens=800,
            )
            # 兼容 _parse_json 返回 list，或字符串
            items = resp if isinstance(resp, list) else json.loads(str(resp))
            if isinstance(items, list):
                cand = [str(x).strip() for x in items if str(x).strip()]
                # 按位置对账：第 i 条改写稿若合格，就替换第 i 条原稿（不算丢弃）
                done = set()
                for i, c in enumerate(cand):
                    if i >= len(reject):
                        break
                    if check(c)["ok"]:
                        kept.append(c)
                        done.add(i)
                        rewritten += 1
                unresolved = [r for i, r in enumerate(reject) if i not in done]
        except Exception:
            pass

    dropped = unresolved

    report = {
        "checked": len(raw),
        "kept": len(kept),
        "rewritten": rewritten,
        "dropped": len(dropped),
        "drop_detail": [{"rule": r, "gaps": check(r)["gaps"]} for r in dropped],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
    }
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(report, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return kept, report
