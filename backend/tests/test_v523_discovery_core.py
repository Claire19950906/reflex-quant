# -*- coding: utf-8 -*-
"""discovery 的纯判定函数契约测试（全离线）。

这一层把"数字"变成"结论"：相关系数、Fed 方向、周期阶段、假设归类。
其中 `_pearson` 有一条明确的反规则 —— **算不出来时返回 None，不许返回 0
冒充"不相关"**。返回 0 会让"没有数据"和"确实无关"在报告里长得一样。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import discovery as dc  # noqa: E402


def test_bucket_of_routes_crypto_and_news_stocks():
    assert dc._bucket_of("BTCUSDT") == "altcoin"
    assert dc._bucket_of("eth") == "altcoin"
    assert dc._bucket_of("000660") == "news_stock"
    assert dc._bucket_of("海力士") == "news_stock"
    assert dc._bucket_of("AAPL") == "news_stock"


def test_pearson_matches_sign_and_refuses_to_fake_zero():
    assert abs(dc._pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9
    assert abs(dc._pearson([1, 2, 3, 4], [8, 6, 4, 2]) + 1.0) < 1e-9
    # 样本不足 / 长度不等 / 方差为 0 → None，而不是 0
    assert dc._pearson([1, 2], [1, 2]) is None
    assert dc._pearson([1, 2, 3], [1, 2]) is None
    assert dc._pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_fed_direction_thresholds_are_inclusive_at_five_bp():
    assert dc._derive_direction_from_ust2y(5) == "hawkish"
    assert dc._derive_direction_from_ust2y(4.9) == "neutral"
    assert dc._derive_direction_from_ust2y(-5) == "dovish"
    assert dc._derive_direction_from_ust2y(-4.9) == "neutral"
    assert dc._derive_direction_from_ust2y(0) == "neutral"


def test_phase_cn_translates_and_passes_through_unknown_labels():
    assert dc._phase_cn("markup") == "拉升"
    assert dc._phase_cn("distribution") == "派发"
    assert dc._phase_cn("unknown") == "未知"
    assert dc._phase_cn("custom_phase") == "custom_phase"


def test_phase_hint_uses_probability_only_where_it_matters():
    assert "派发" in dc._phase_hint({"phase": "distribution"}, {"prob": 0.6})
    assert "反弹" in dc._phase_hint({"phase": "accumulation"}, {"prob": 0.55})
    assert dc._phase_hint({"phase": "markup"}, {}) == "顺势做多,关注上方阻力"
    assert dc._phase_hint({"phase": "markdown"}, {}) == "顺势做空,等待止跌信号"
    assert dc._phase_hint({"phase": "ranging"}, {}) == "区间震荡,高抛低吸"
    # 低概率的派发不能给出"反转下挫"的结论
    assert "反转" not in dc._phase_hint({"phase": "distribution"}, {"prob": 0.2})


def test_detect_bias_flags_universal_quantifiers():
    assert dc._detect_bias("") == []
    found = dc._detect_bias("这是绝大部分情况下都会发生的事情")
    assert any(f["type"] == "scope_overreach" for f in found)


def test_disc_cache_respects_ttl():
    dc._DISC_CACHE.clear()
    dc._disc_cache_put("k", {"v": 1})
    assert dc._disc_cache_get("k") == {"v": 1}
    assert dc._disc_cache_get("k", ttl=0) is None     # ttl 0 = 立即过期
    assert dc._disc_cache_get("missing") is None
    dc._DISC_CACHE.clear()
