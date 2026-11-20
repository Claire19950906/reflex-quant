#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v416：枚举字段的常见误返在**校验前**归一，避免每条分析多付一次补全调用。

背景（2026-11-17 实测日志）
--------------------------
    [V174-SCHEMA] JSON Schema 校验失败 (3 错) |
      [asset_classification] {'primary':'贵金属',...,'key_drivers':[...]} is not of type 'string'
      [consensus_vs_us] '与既定 SELL 判断一致；偏鹰通胀风险表态强化…' is not one of ['一致','反共识']

schema 是严格枚举，模型却把这两个字段写成对象/长句 → 整条校验失败 →
触发「schema 精简补全」再调一次模型。当日 schema_fill_lean 105 次 > 主调用 102 次，
**每条分析都在多付一次调用**。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))

from llm_schema import coerce_enum_fields


def _real_log_sample():
    """日志里的真实误返样本。"""
    return {
        "asset_classification": {
            "primary": "贵金属", "instrument": "XAU", "currency": "美元计价",
            "income_character": "非生息资产",
            "key_drivers": ["实际利率", "美元", "澳元利率预期", "避险需求", "央行购金"],
        },
        "consensus_vs_us": "与既定 SELL 判断一致；偏鹰通胀风险表态强化了实际利率上行压制黄金的逻辑",
    }


def test_object_asset_classification_coerced_to_enum():
    r = _real_log_sample()
    coerce_enum_fields(r)
    assert r["asset_classification"] == "避险/抗通胀"


def test_long_sentence_consensus_coerced():
    r = _real_log_sample()
    coerce_enum_fields(r)
    assert r["consensus_vs_us"] == "一致"


def test_negative_consensus_not_misread_as_agree():
    """"与共识不一致" 不能被 "一致" 抢先命中。"""
    r = {"consensus_vs_us": "与市场共识不一致，属于反共识判断"}
    coerce_enum_fields(r)
    assert r["consensus_vs_us"] == "反共识"


def test_already_valid_values_untouched():
    r = {"asset_classification": "宏观锚", "consensus_vs_us": "一致"}
    assert coerce_enum_fields(r) == 0
    assert r == {"asset_classification": "宏观锚", "consensus_vs_us": "一致"}


def test_unknown_value_left_alone():
    """归一不了就不要动 —— 让补全流程去拿真值，别把分类猜错。"""
    r = {"asset_classification": {"primary": "未知"}, "consensus_vs_us": "看不清"}
    assert coerce_enum_fields(r) == 0
    assert r["asset_classification"] == {"primary": "未知"}


def test_non_dict_result_does_not_crash():
    assert coerce_enum_fields(None) == 0
    assert coerce_enum_fields("not a dict") == 0


def test_agreement_phrasing_without_the_word_yizhi():
    """长句里没有"一致"二字，但用"认同/成立"表达同向 —— 也要归成"一致"。"""
    r = {"consensus_vs_us": "本次分析认同新闻所给核心影响，即偏鹰纪要可能推高实际利率并促使黄金多头卖出"}
    coerce_enum_fields(r)
    assert r["consensus_vs_us"] == "一致"


def test_disagreement_phrasing_wins_over_agreement_words():
    """"与共识不同"必须先于"认同/符合"被判定。"""
    r = {"consensus_vs_us": "该判断与主流共识不同，我们认为市场尚未定价"}
    coerce_enum_fields(r)
    assert r["consensus_vs_us"] == "反共识"


def test_overlong_fund_driver_is_truncated_not_rejected():
    """超 80 字的 fund_driver 截断即可，不该因此重调一次模型。"""
    r = {"fund_driver": "潜在主力资金为配置黄金的全球宏观基金、贵金属 ETF、避险资金及利率交易资金；"
                        "核心观察指标包括澳大利亚国债收益率、澳元、美元指数、美国实际利率、"
                        "黄金 ETF 流量和期货净仓位，相关数据未披露。"}
    assert len(r["fund_driver"]) > 80
    coerce_enum_fields(r)
    assert len(r["fund_driver"]) == 80


def test_short_fund_driver_untouched():
    r = {"fund_driver": "实际利率上行压制黄金"}
    assert coerce_enum_fields(r) == 0
