# -*- coding: utf-8 -*-
"""v406 — E001a 按原因拆细。

背景：2026-11-17 实测 464 条错误码里 395 条（85%）都是 E001a"缺硬数据"，
一个码吃掉分布，读不出"缺的是哪一种"。本版把无标的不再报错，并把"缺硬数据"
拆成 无本地源（E001g）/ 断档（E001h）/ 未知符号（E001i），剩余原因未明的仍记 E001a。

验收：
1. target_asset=NONE 的决策不再产出 E001a 系错误码（它不是缺事实，是没有可查对象）；
2. hard_data_status 能落到对应子码；
3. 新码在维度映射表、错误码台账注册表、C 维子类枚举里都存在，不会成为"野码"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
for p in (str(BACKEND), str(BACKEND / "src_python")):
    if p not in sys.path:
        sys.path.insert(0, p)

from review import reflection_checkers as rc
from review import reflection_dimension_map as rdm
import error_code_ledger_build as ledger


def _dec(**kw):
    base = {
        "id": 1,
        "target_asset": "BTC",
        "hard_data_evidence": "",
        "hard_data_status": "",
        "data_anchor_ts": "2026-11-17T06:00:00Z",
        "factor_ranking": "",
        "suggested_action": "HOLD",
        "news_id": "n1",
        "event_atom_ids": "",
        "sentiment_score": 0,
        "completeness_score": 1.0,
    }
    base.update(kw)
    return base


def _codes(decision):
    return [e["error_code"] for e in rc.check_data(decision)]


def test_none_target_does_not_emit_data_missing_codes():
    """无标的决策：不报 E001a/g/h/i —— 没有可查对象不等于缺事实。"""
    codes = _codes(_dec(target_asset="NONE", hard_data_status="missing:none_target"))
    for code in ("E001a", "E001g", "E001h", "E001i"):
        assert code not in codes, f"无标的决策不应报 {code}: {codes}"


def test_no_local_feed_maps_to_e001g():
    errs = rc.check_data(_dec(hard_data_status="missing:no_local_feed"))
    e = [x for x in errs if x["error_code"].startswith("E001")][0]
    assert e["error_code"] == "E001g"
    assert e["evidence_refs"]["fixable"] == 0
    assert e["severity"] == rc.SEVERITY_INFO  # 结构性缺口，不是当轮可修的错误


def test_feed_stale_maps_to_e001h():
    errs = rc.check_data(_dec(hard_data_status="missing:feed_stale"))
    e = [x for x in errs if x["error_code"].startswith("E001")][0]
    assert e["error_code"] == "E001h"
    assert e["evidence_refs"]["fixable"] == 1


def test_unknown_symbol_maps_to_e001i():
    errs = rc.check_data(_dec(hard_data_status="missing:unknown_symbol"))
    e = [x for x in errs if x["error_code"].startswith("E001")][0]
    assert e["error_code"] == "E001i"
    assert e["evidence_refs"]["fixable"] == 1


def test_unlabelled_status_falls_back_to_e001a():
    """原因没标注时不硬猜，保留 E001a 这一档。"""
    errs = rc.check_data(_dec(target_asset="XAU", hard_data_status=""))
    e = [x for x in errs if x["error_code"].startswith("E001")][0]
    assert e["error_code"] == "E001a"
    assert e["evidence_refs"]["sub_kind"] == "missing_hard_evidence"


def test_new_codes_are_registered_everywhere():
    """新码必须同时出现在维度映射 + 台账注册表 + C 维子类枚举里。"""
    for code in ("E001g", "E001h", "E001i"):
        assert code in rdm.E_CODE_TO_DIMENSION
        assert code in ledger.REGISTRY
        dim, sub = rdm.E_CODE_TO_DIMENSION[code]
        assert dim == rdm.DIMENSION_C
        assert sub in rdm.SUBCATEGORIES_BY_DIMENSION[rdm.DIMENSION_C]


def test_structural_subcategory_exists():
    assert "feed_no_source" in rdm.SUBCATEGORIES_BY_DIMENSION[rdm.DIMENSION_C]
    assert rdm.E_CODE_TO_DIMENSION["E001g"][1] == "feed_no_source"


def test_with_evidence_no_missing_code():
    """有可解析事实时不报任何 E001* 缺数据码。"""
    dec = _dec(hard_data_evidence="BTC price=90000 ts=2026-11-17T05:00:00Z")
    codes = _codes(dec)
    assert not [c for c in codes if c.startswith("E001")]
