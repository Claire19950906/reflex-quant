# -*- coding: utf-8 -*-
"""realtime_filter._harden_verdict —— v181 重构后二分类规则回归单测。

v181 重构（取消"只能看"）：
  - 三分类（可交易/只能看/排除）→ 二分类（可交易/排除）
  - 分析类（带观点/预测/评论）→ 一律排除（落网之鱼）
  - 总结类 + 已定价 → 排除（保留）
  - verdict="只能看" → 当非法值 → 排除
  - 新增 impact_1to100 阈值（< NEWS_GATE_IMPACT_MIN 即排除）

根因：原"分析类 → 只能看"虽落入了 ai_decisions，但被 decision_guard 强制 HOLD，
只是让前端展示"AI 观望"假象。现在直接排除，不再让分析类新闻进入 ai_decisions。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import realtime_filter as rt


def _h(**kw):
    base = dict(
        verdict="可交易", is_material=True, assets=["黄金"], core="动了供需",
        kind="总结类", timing="即时", strength=8, impact_1to100=70,
    )
    base.update(kw)
    return base


def test_analysis_class_now_excluded():
    # v423 修正：分析类若实质达标（有重量 + 有标的 + 有核心影响 + impact 达标），
    # 类型只是修辞形态、不覆盖实质结论 → 放行；只有不达标时才按观点稿排除。
    assert rt._harden_verdict(**_h(kind="分析类", impact_1to100=70)) == "可交易"
    # 不达标（影响力分低于阈值）的分析类观点稿仍排除
    assert rt._harden_verdict(**_h(kind="分析类", impact_1to100=10)) == "排除"


def test_analysis_class_no_promotion():
    # 不达标时分析类不升级：无论 verdict 写什么，都按排除处理
    assert rt._harden_verdict(**_h(kind="分析类", verdict="排除", impact_1to100=10)) == "排除"
    assert rt._harden_verdict(**_h(kind="分析类", verdict="可交易", impact_1to100=10)) == "排除"
    # 达标时即使标"分析类"也放行（v423 防误杀）
    assert rt._harden_verdict(**_h(kind="分析类", verdict="可交易", impact_1to100=70)) == "可交易"


def test_summary_class_tradable_stays_tradable():
    # 总结类 + 即时 + 力度足 → 保持可交易（v181 不误伤）
    assert rt._harden_verdict(**_h(kind="总结类")) == "可交易"


def test_summary_priced_in_excluded():
    # 已定价 + 影响力分低于阈值 → 排除；达标时 v423 仍优先放行
    assert rt._harden_verdict(**_h(kind="总结类", timing="已定价", impact_1to100=10)) == "排除"
    assert rt._harden_verdict(**_h(kind="总结类", timing="已定价", impact_1to100=70)) == "可交易"


def test_not_material_excluded():
    assert rt._harden_verdict(**_h(is_material=False)) == "排除"


def test_no_assets_excluded():
    assert rt._harden_verdict(**_h(assets=[])) == "排除"


def test_no_core_excluded():
    assert rt._harden_verdict(**_h(core="")) == "排除"


def test_legacy_watch_label_now_treated_as_invalid():
    # 老数据残留的"只能看"——v181 二分类后是非法 verdict → 排除
    assert rt._harden_verdict(**_h(verdict="只能看")) == "排除"


def test_invalid_verdict_excluded():
    assert rt._harden_verdict(**_h(verdict="随便")) == "排除"


def test_impact_below_threshold_excluded():
    # 新规则：< NEWS_GATE_IMPACT_MIN（默认 20）一律排除
    assert rt._harden_verdict(**_h(impact_1to100=10)) == "排除"
    assert rt._harden_verdict(**_h(impact_1to100=19)) == "排除"


def test_impact_at_threshold_kept():
    # >= 阈值（默认 20）放行
    assert rt._harden_verdict(**_h(impact_1to100=20)) == "可交易"
    assert rt._harden_verdict(**_h(impact_1to100=85)) == "可交易"


def test_impact_zero_skips_threshold_check():
    # 0 / 缺失 → 跳过阈值检查（不阻塞可交易，让其他规则生效）
    assert rt._harden_verdict(**_h(impact_1to100=0)) == "可交易"