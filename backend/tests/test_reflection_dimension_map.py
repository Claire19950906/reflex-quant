# -*- coding: utf-8 -*-
"""
test_reflection_dimension_map.py — v309 P0-1 三维度映射表单测
====================================================================
映射表是「A 买卖信号 / B 信息分析方向 / C 技术错误」口径的**单一权威**
(reflection_dimension_map.py), 前端 MissedSignalsDimensionSummary.tsx 按
A/B/C 配色, 所以:
  · 每个 E 码必须映射到**合法**的 (维度, 小类) 组合;
  · 小类必须属于它所属维度的受控枚举 (否则前端按 A/B/C 配色会串桶);
  · 三个维度都必须被用到 (否则某张卡恒为 0, 等于没实现)。

纯离线: 不碰 DB / config / dotenv。
"""
from __future__ import annotations

import os
import sys

import pytest

_SRC_PYTHON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

from review import reflection_dimension_map as rdm


# ── 1. 每个 E 码的映射必须合法 ──────────────────────────────────────────
def test_all_error_codes_map_to_valid_dimension():
    for code, (dim, sub) in rdm.E_CODE_TO_DIMENSION.items():
        assert dim in ("A", "B", "C"), f"{code} 维度非法: {dim}"
        assert rdm.is_valid_subcategory(dim, sub), \
            f"{code} 的小类 {sub} 不属于维度 {dim}"


def test_every_dimension_is_used():
    """三个维度都要有 E 码映射 —— 否则前端某张卡恒为 0。"""
    used = {v[0] for v in rdm.E_CODE_TO_DIMENSION.values()}
    assert used == {"A", "B", "C"}, f"未覆盖全部维度: {used}"


def test_every_subcategory_is_reachable():
    """12 个受控小类都应至少被一条路径 (E码 / reason_code / ontology) 用到。

    注意: 不是每个小类都必须有 E 码 —— 例如 data_feed_timeout (接口超时)
    在 E 码体系里没有对应检查器, 它由 reason_code 21 (系统通道故障) 与
    ontology 的 data/latency 覆盖。口径不留死桶即可。
    """
    used = {v[1] for v in rdm.E_CODE_TO_DIMENSION.values()}
    used |= {v[1] for v in rdm.REASON_CODE_TO_DIMENSION.values()}
    used |= {v[1] for v in rdm.LAYER_TO_DIMENSION.values()}
    used |= {v[1] for v in rdm.SUB_LAYER_TO_DIMENSION.values()}
    # v326: deny_reason 是第四条路径 (机器记录的拒单机制)
    used |= {m[1] for _pat, m, _note in rdm.DENY_REASON_PATTERNS}
    all_subs = {s for subs in rdm.SUBCATEGORIES_BY_DIMENSION.values() for s in subs}
    # v326: unverified_legacy_label 不是分类器产出的小类, 而是历史行的
    # "证据不足"标记 (由 recompute_b_labels 写入)。它必须合法 (否则 C8 判非法枚举),
    # 但不需要任何分类路径产出它 —— 所以从可达性断言里排除。
    used -= {"unverified_legacy_label"}
    all_subs -= {"unverified_legacy_label"}
    assert used == all_subs, f"未被任何路径覆盖的小类: {all_subs - used}"


# ── 2. 已知错误码的期望归类 (防口径被无意改坏) ─────────────────────────
@pytest.mark.parametrize("code,expect_dim", [
    ("E001a", "C"),   # 缺品种事实 → 数据缺失
    ("E002", "C"),    # 数据过期 → 信号慢
    ("E009", "A"),    # 置信虚高 → 心理锚定
    ("E013", "A"),    # 叙事偏置 → 心理锚定
    ("E014", "B"),    # 推理链断裂 → 事件链断裂
    ("E016", "A"),    # 跨资产方向错 → 市场状态读错
    ("E023", "C"),    # 置信未校准 → 校准漂移
    ("E027", "C"),    # 置信-证据量不匹配 → 校准漂移
    ("E033", "B"),    # 后此谬因 → 因果倒置
])
def test_known_error_codes_dimension(code, expect_dim):
    hit = rdm.dimension_for_error_code(code)
    assert hit is not None, f"{code} 无映射"
    assert hit[0] == expect_dim, f"{code} 应归 {expect_dim}, 实际 {hit[0]}"


def test_unknown_error_code_returns_none():
    """未知码不猜 —— 返回 None 由调用方跳过。"""
    assert rdm.dimension_for_error_code("E999") is None
    assert rdm.dimension_for_error_code("") is None
    assert rdm.dimension_for_error_code(None) is None


# ── 3. 归一化: 大小写 / 短码 / 数字都应命中同一映射 ─────────────────────
def test_code_normalization():
    assert rdm.dimension_for_error_code("e014") == rdm.dimension_for_error_code("E014")
    assert rdm.dimension_for_error_code(" E014 ") == rdm.dimension_for_error_code("E014")
    assert rdm.dimension_for_error_code("014") == rdm.dimension_for_error_code("E014")
    assert rdm.dimension_for_error_code("14") == rdm.dimension_for_error_code("E014")


# ── 3b. 子码 (E001a/E001f) 归一 —— 回归测试 ────────────────────────────────
# 真实踩过的坑: _norm_code 原来做整体 .upper(), 把 'E001a' 变成 'E001A',
# 而字典键是小写后缀 'E001a' → 查不到 → E001a 全部误判为"未知码"。
# 子码归一必须是: 数字部分大写 + 后缀字母小写。
@pytest.mark.parametrize("code", ["E001a", "e001a", "E001A", " e001A "])
def test_subcode_normalization_hits_mapping(code):
    hit = rdm.dimension_for_error_code(code)
    assert hit is not None, f"{code!r} 归一后查不到映射 (子码大小写处理错了)"
    assert hit[0] == "C", f"{code!r} 应归 C, 实际 {hit[0]}"


def test_all_subcodes_are_reachable():
    """字典里所有带字母后缀的 E 码, 用自身及各种大小写变体都必须能查到。"""
    for key in rdm.E_CODE_TO_DIMENSION:
        if not key[-1].isalpha():
            continue
        base = rdm.E_CODE_TO_DIMENSION[key]
        for variant in (key, key.lower(), key.upper(), key[:-1] + key[-1].upper()):
            assert rdm.dimension_for_error_code(variant) == base, \
                f"{variant!r} 归一后与字典键 {key!r} 不一致"


# ── 3c. 多错误码的确定性 (回归) ───────────────────────────────────────────
# decision_errors 查询无 ORDER BY → 同一组错误码的返回顺序不保证稳定。
# resolve_dimension 必须按 E 码字典定义顺序排序, 否则同一笔决策两次运行
# 可能落到不同维度 → 三维度统计口径漂移。
def test_resolve_dimension_is_order_independent():
    codes = ["E014", "E001a", "E009", "E002"]
    expected = rdm.resolve_dimension(error_codes=codes)
    assert expected is not None
    # 所有排列都必须得到同一个结果
    import itertools
    for perm in itertools.permutations(codes):
        assert rdm.resolve_dimension(error_codes=list(perm)) == expected, \
            f"错误码顺序 {perm} 得到了不同结果 {rdm.resolve_dimension(error_codes=list(perm))} != {expected}"


def test_resolve_dimension_dedupes_codes():
    a = rdm.resolve_dimension(error_codes=["E014", "E014", "e014"])
    b = rdm.resolve_dimension(error_codes=["E014"])
    assert a == b


# ── 4. reason_code 映射 (review_agent 23 类失败框架) ────────────────────
def test_all_reason_codes_map_to_valid_dimension():
    for code, (dim, sub) in rdm.REASON_CODE_TO_DIMENSION.items():
        assert dim in ("A", "B", "C"), f"reason {code} 维度非法: {dim}"
        assert rdm.is_valid_subcategory(dim, sub), \
            f"reason {code} 的小类 {sub} 不属于维度 {dim}"


def test_reason_code_out_of_range_returns_none():
    assert rdm.dimension_for_reason_code(999) is None
    assert rdm.dimension_for_reason_code("abc") is None
    assert rdm.dimension_for_reason_code(None) is None


# ── 5. 优先级: E 码 > reason_code > ontology ──────────────────────────
def test_priority_error_code_wins_over_reason_code():
    hit = rdm.resolve_dimension(
        error_codes=["E014"], reason_code=6, layer="analysis", sub_layer="llm_output")
    assert hit is not None
    assert hit == rdm.dimension_for_error_code("E014")


def test_priority_reason_code_wins_over_ontology():
    hit = rdm.resolve_dimension(
        error_codes=[], reason_code=6, layer="analysis", sub_layer="llm_output")
    assert hit is not None
    assert hit == rdm.dimension_for_reason_code(6)


def test_fallback_to_ontology():
    hit = rdm.resolve_dimension(
        error_codes=[], reason_code=None, layer="data", sub_layer="latency")
    assert hit is not None
    assert hit == (rdm.DIMENSION_C, "data_feed_timeout")


def test_resolve_returns_none_when_nothing_known():
    assert rdm.resolve_dimension(error_codes=[], reason_code=None,
                                 layer="", sub_layer="") is None


# ── 6. classify_reflection_row: 兼容两种行结构 ─────────────────────────
def test_classify_reflection_row_from_ontology():
    dim, sub = rdm.classify_reflection_row(
        {"layer": "data", "sub_layer": "coverage", "signal_name": "x"})
    assert dim == "C"
    assert sub == "feed_missing"


def test_classify_reflection_row_from_reason_code():
    dim, sub = rdm.classify_reflection_row({"reason_code": 6})
    assert dim == "B"


def test_classify_reflection_row_empty_is_blank():
    """无法归类时返回空, 不能塞进默认桶。"""
    assert rdm.classify_reflection_row({}) == ("", "")
    assert rdm.classify_reflection_row({"layer": "", "sub_layer": ""}) == ("", "")
    assert rdm.classify_reflection_row(None) == ("", "")


# ── 7. ontology 层映射自身合法 ─────────────────────────────────────────
def test_all_ontology_mappings_valid():
    for layer, (dim, sub) in rdm.LAYER_TO_DIMENSION.items():
        assert dim in ("A", "B", "C"), f"layer {layer} 维度非法"
        assert rdm.is_valid_subcategory(dim, sub), f"layer {layer} 小类非法"
    for (layer, sub_layer), (dim, sub) in rdm.SUB_LAYER_TO_DIMENSION.items():
        assert dim in ("A", "B", "C")
        assert rdm.is_valid_subcategory(dim, sub)
        # sub_layer 精化必须与它所属 layer 的默认归属同维度, 否则
        # 同一个 layer 会在 A/B/C 之间摇摆, 统计口径自相矛盾。
        base = rdm.LAYER_TO_DIMENSION.get(layer)
        if base:
            assert base[0] == dim, \
                f"{layer}/{sub_layer} 维度 {dim} 与 layer 默认 {base[0]} 不一致"


# ── 8. describe_mapping 可导出 (供 API 端点用) ─────────────────────────
def test_describe_mapping_shape():
    d = rdm.describe_mapping()
    assert set(d["dimensions"]) == {"A", "B", "C"}
    assert "error_codes" in d and d["error_codes"]
    assert "reason_codes" in d and d["reason_codes"]
    for _k, v in d["dimensions"].items():
        assert v["label"] and v["desc"]
        # v326: A 类 +3 机制小类 (7), B 类 +1 历史标记 (5)。
        # v409: C 类 +1 结构性缺口 feed_no_source (4 → 5) ——
        #       "该品种本地没有行情源"与"有源但断档"必须分开, 前者改代码解决不了。
        expect = {"A": 7, "B": 5, "C": 5}[_k]
        assert len(v["subcategories"]) == expect, f"{_k} 小类数量变了: {v['subcategories']}"
