# -*- coding: utf-8 -*-
"""机制族（mechanism family）—— 把"名字不同、讲的是同一件事"的因果链归到一起。

为什么单独拆成一个小模块：
  - `causal_graph`（176KB）和 `causal_chain_evaluator` 都需要这个判断；
  - 评估器在写库主循环里跑，不能为了一个字符串匹配去 import 整个因果图谱；
  - 口径必须**只有一份**，否则"分析时不重复"和"入库时不扇出"会各说各话。

实测依据（2026-11-18 只读库）：
  XAU 有 4 条链跑赢同品种同向基准（+21~+31pp、p<0.05），名字分别是
  「中东冲突升级推升避险与供应风险溢价」「地缘升级与航运受阻推升黄金风险溢价」
  「地缘冲突→黄金避险传导」「中东地缘冲突升级→黄金避险溢价>原油供应溢价」——
  字面重合率只有 0.2（字符串去重抓不到），但都在 geopolitics 族里，
  对决策而言是**一个信号**，不是四个。
"""
from __future__ import annotations

from typing import Tuple

# 顺序即优先级：先命中者归族（地缘/避险类新闻常常同时含"冲突"与"避险"，
# 归 geopolitics 更准确 —— 冲突是原因，避险是机制）。
FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("geopolitics", ("地缘", "冲突", "战争", "袭击", "空袭", "停火", "和谈", "霍尔木兹",
                     "红海", "航运", "制裁", "封锁", "中东", "军事", "爆炸")),
    ("rates", ("利率", "加息", "降息", "美联储", "fomc", "收益率", "缩表", "扩表",
               "流动性", "美元", "鲍威尔", "实际利率", "央行")),
    ("supply", ("供给", "供需", "库存", "opec", "减产", "增产", "产能", "产量",
                "osp", "断供", "检修")),
    ("inflation", ("通胀", "cpi", "ppi", "物价", "通缩")),
    ("regulation", ("监管", "sec", "etf", "合规", "政策", "批准", "立法", "处罚")),
    ("risk_appetite", ("风险偏好", "拥挤", "情绪", "资金流", "避险", "恐慌", "去杠杆")),
)

FAMILY_LABELS = {
    "geopolitics": "地缘",
    "rates": "利率",
    "supply": "供需",
    "inflation": "通胀",
    "regulation": "监管",
    "risk_appetite": "风险偏好",
    "other": "其它",
}


def family_of(name: str) -> str:
    """链名/因果文本 → 机制族；判不出归 'other'（不硬猜）。"""
    text = str(name or "").lower()
    for fam, words in FAMILIES:
        if any(w in text for w in words):
            return fam
    return "other"
