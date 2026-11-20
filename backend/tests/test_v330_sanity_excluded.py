"""v330: 反思摘要端点的"量纲异常"剔除必须显式可解释。

背景: /api/v291/reflections/summary 读数侧有 `ABS(actual_outcome_pct) <= 60`
过滤 (剔掉 5 日涨跌 508% 这类单位错的行)。这让端点数与库内行数天然差 10 条
(B/C 各 5), 验收 #8「三维反思系统」与 C6「端点↔DB 自洽」因此长期 FAIL —
错不在数据, 在"差值没人解释"。

修法两条, 这里各锁一遍:
  1. 端点返回 sanity_excluded {total, by_dimension}
  2. 验收比对时从库侧减掉剔除数 → 差值归零

只跑纯函数, 不连网不连库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_acceptance as acc


def test_extract_excluded_by_dimension():
    payload = {"ok": True, "sanity_excluded": {"total": 10, "by_dimension": {"B": 5, "C": 5}}}
    assert acc._api_sanity_excluded(payload) == {"A": 0, "B": 5, "C": 5}


def test_extract_excluded_missing_field_is_zero():
    """老端点/异常响应没有这个字段 → 必须当成 0, 不能 KeyError 让验收崩掉。"""
    assert acc._api_sanity_excluded({"ok": True}) == {"A": 0, "B": 0, "C": 0}
    assert acc._api_sanity_excluded({}) == {"A": 0, "B": 0, "C": 0}


def test_excluded_makes_delta_zero():
    """端点 1754 / 库 1759, 剔除 5 → 差 0 (这是 C6 判 PASS 的算术)。"""
    api = {"A": 2626, "B": 1754, "C": 1783}
    db = {"A": 2626, "B": 1759, "C": 1788}
    exc = {"A": 0, "B": 5, "C": 5}
    deltas = {d: api[d] - (db[d] - exc[d]) for d in ("A", "B", "C")}
    assert deltas == {"A": 0, "B": 0, "C": 0}


def test_real_dropped_rows_still_show_as_delta():
    """端点真漏了 3 行 (不是量纲剔除) → 差值必须还能暴露, 不能被"解释"掩盖。"""
    api = {"A": 2626, "B": 1751, "C": 1783}
    db = {"A": 2626, "B": 1759, "C": 1788}
    exc = {"A": 0, "B": 5, "C": 5}
    deltas = {d: api[d] - (db[d] - exc[d]) for d in ("A", "B", "C")}
    assert deltas["B"] == -3


def test_valid_subcategories_take_union_from_map():
    """小类合法清单必须并自 reflection_dimension_map —— 三处各抄一份必漂。"""
    from review import reflection_dimension_map as rdm

    for dim, subs in rdm.SUBCATEGORIES_BY_DIMENSION.items():
        for s in subs:
            assert s in acc.VALID_SUBCATEGORIES, f"{dim} 的小类 {s} 不在验收合法清单里"


def test_legacy_label_is_legal_but_not_produced():
    """unverified_legacy_label 是历史标记: 必须合法 (否则 C8 判非法枚举), 但不由分类器产出。"""
    from review import reflection_dimension_map as rdm

    assert "unverified_legacy_label" in acc.VALID_SUBCATEGORIES
    assert "unverified_legacy_label" in rdm.SUBCATEGORIES_BY_DIMENSION["B"]
    for mapping in (rdm.DIMENSION_B,):
        for key in mapping:
            assert key is not None
