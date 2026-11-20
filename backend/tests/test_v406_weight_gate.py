# -*- coding: utf-8 -*-
"""v406 P4：权重回写门槛与方向闸门对齐（12 条样本 + Wilson 下界）。

背景：`causal_weight_feedback` 原来 `MIN_CHAIN_SAMPLES = 2` —— 两个样本就敢改
"这条链有多准"；而决策侧 `chain_prior.MIN_CHAIN_SAMPLES_FOR_DIRECTION = 12`。
同一个库里两套置信门槛，等于用 A 的把握去支撑 B 的结论。

这里锁四件事：
1. 两处门槛必须同值（谁先改都会立刻红）。
2. Wilson 公式与 `chain_prior._wilson_lower` 完全一致（口径漂移是静默 bug）。
3. 2/2 全中在 95% 下界上过不了 0.5 —— 这就是"小样本不许改权重"的数学依据。
4. 样本够（14/14）时下界才真的站住。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import chain_prior
import feeds.causal_weight_feedback as fb


def test_min_samples_aligned_with_direction_gate():
    assert fb.MIN_CHAIN_SAMPLES == 12
    assert fb.MIN_CHAIN_SAMPLES == chain_prior.MIN_CHAIN_SAMPLES_FOR_DIRECTION


def test_wilson_formula_matches_chain_prior():
    # chain_prior 的版本只收整数 hits；在整数点上两者必须逐位一致
    for hits, n in ((0, 1), (1, 2), (2, 3), (3, 5), (7, 12), (12, 14), (14, 14), (160, 330)):
        a = fb._wilson_lower(hits, n)
        b = chain_prior._wilson_lower(hits, n)
        assert abs(a - b) < 1e-12, (hits, n, a, b)


def test_two_out_of_two_cannot_pass():
    # 2/2 点估计 100%，Wilson 下界只有 ~34% —— 旧门槛（n>=2）就是这么放行噪声的
    assert fb._wilson_lower(2, 2) < 0.5
    assert fb._wilson_lower(3, 3) < 0.6


def test_twelve_out_of_twelve_passes():
    assert fb._wilson_lower(12, 12) > 0.5


def test_credit_is_float_and_handled():
    # credit 是加权量（HIT 1.0 / HIT_WEAK 0.5），不是整数命中数
    assert 0.0 <= fb._wilson_lower(6.5, 14) <= 1.0
    assert fb._wilson_lower(6.5, 14) < fb._wilson_lower(14, 14)


def test_empty_sample_zeroes_out():
    assert fb._wilson_lower(0, 0) == 0.0


def test_sensitivity_table_shape():
    """门槛敏感性必须是绝对值口径（每个门槛各算一次），不能只给最终结论。"""
    import inspect
    src = inspect.getsource(fb.plan_updates)
    assert "threshold_sensitivity" in src
    # 四个门槛各算一次（阈值集合本身在源码里，防止有人只留一个"最终结论"）
    assert "for th in (2, 5, 12, 20)" in src
