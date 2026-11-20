# -*- coding: utf-8 -*-
"""v418（录入侧 P0）· 自动建链不许生产死链

2026-11-18 只读库实测：
  - 305 条 `asset=UNKNOWN`、237 条零触发词（重叠 234，合计 **308 条死链**）；
  - 其中 **288 条**的 note 是"由 R5 反推自动创建"，根因是 insert 漏写 asset 列，
    上游 insight 没定品种时还会写死字面 `NONE`；连去重都失效（#774 被建了两条）；
  - 这些链 `load_active_chains` 直接跳过 → 永远不评估，只在 UI 分母里占位。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import causal_chain_expander as ex


def test_dead_reason_blocks_no_asset():
    assert ex._dead_reason("NONE", "袭击,停火") == "no_asset"
    assert ex._dead_reason("UNKNOWN", "加息") == "no_asset"
    assert ex._dead_reason("", "加息") == "no_asset"
    assert ex._dead_reason("null", "加息") == "no_asset"
    assert ex._dead_reason("MACRO", "加息") == "no_asset"


def test_dead_reason_blocks_no_keywords():
    assert ex._dead_reason("XAU", "") == "no_trigger_words"
    assert ex._dead_reason("XAU", "   ") == "no_trigger_words"
    assert ex._dead_reason("NONE", "") in ("no_trigger_words", "no_asset")


def test_real_candidate_passes():
    """有品种、有触发词的正常候选必须放行（不能把闸门做成一刀切）。"""
    assert ex._dead_reason("XAU", "地缘冲突,避险") == ""
    assert ex._dead_reason("btc", "etf,监管") == ""


def test_expander_insert_writes_asset_column():
    """源码级回归：INSERT 必须带 asset，否则修完前的空档又会复发。"""
    src = Path(ex.__file__).read_text(encoding="utf-8")
    seg = src.split("INSERT INTO causal_chains")[1][:900]
    assert "asset" in seg.split("VALUES")[0], "INSERT 列清单里没有 asset —— 死链会继续产生"
    assert "_dead_reason(" in src, "入口闸门被删掉了"
