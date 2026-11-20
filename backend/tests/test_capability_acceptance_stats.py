"""capability_acceptance 的统计口径用例（64 项能力验收的算分器）。

这层的数字直接决定"这项能力算不算过"，但算错不会报错：
- 秩相关不分并列、不分桶 → ρ 被小时之间的波动量级差盖住，权重有效与否判不出来；
- 小样本命中率不带置信下界 → 16/25=64% 被当成定论（实际下界只有 45%）；
- 规则型拒绝被算进"方向判别力" → 闸门看起来什么都能拦，其实拦的是黑名单。

所以这里测的是口径本身，不是函数能不能跑。
"""

import pytest

import capability_acceptance as ca


# ── 百分比 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("num,den,expect", [
    (1, 4, 25.0), (0, 4, 0.0), (5, 0, 0.0), (2, 3, 66.67),
])
def test_pct_guards_zero_denominator(num, den, expect):
    assert ca._pct(num, den) == expect


# ── Spearman ──────────────────────────────────────────────────────────────

def test_spearman_perfect_monotone_is_plus_one():
    assert ca._spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert ca._spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_needs_three_points_and_equal_length():
    assert ca._spearman([1, 2], [1, 2]) == 0.0
    assert ca._spearman([1, 2, 3], [1, 2]) == 0.0
    assert ca._spearman([], []) == 0.0


def test_spearman_averages_tied_ranks():
    # 并列必须取平均秩，否则 [1,1,2] 会被排成 1,2,3，ρ 凭空变高
    assert ca._spearman([1, 1, 2], [3, 3, 4]) == pytest.approx(1.0)
    assert ca._spearman([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0      # 一边全并列 → 无相关


def test_spearman_partial_correlation():
    assert 0.7 < ca._spearman([1, 2, 3, 4], [1, 3, 2, 4]) < 0.9


# ── 桶内秩相关 ────────────────────────────────────────────────────────────

def test_bucket_spearman_ignores_buckets_below_min_n():
    out = ca.bucket_spearman([(0, 1.0, 0.1), (0, 2.0, 0.2)])
    assert out == {"rho": None, "n": 0, "hours": 0}
    # 3 个样本够 min_n=3，但不够默认的 5 —— 桶不够格就当没样本，不给一个"看起来有"的 ρ
    assert ca.bucket_spearman([(0, 1.0, 0.1), (0, 2.0, 0.2), (0, 3.0, 0.3)])["rho"] is None


def test_bucket_spearman_measures_weight_vs_deviation_from_median():
    # 桶内先去方向（|move − 桶内中位数|），再看权重能不能区分"这一小时里谁影响更大"
    pairs = [(0, 1.0, 0.0), (0, 2.0, 0.0), (0, 3.0, 1.0)]
    out = ca.bucket_spearman(pairs, min_n=3)
    assert out["rho"] == pytest.approx(0.866, abs=0.01)
    assert out["n"] == 3 and out["hours"] == 1


def test_bucket_spearman_drops_the_sign_on_purpose():
    # 同一组数据把涨跌全取负，ρ 不变 —— 这说明它问的是"权重能否区分影响大小"，
    # 不是"权重能否预测方向"。两件事别混着解读。
    up = [(0, 1.0, 0.0), (0, 2.0, 0.0), (0, 3.0, 1.0)]
    down = [(0, 1.0, 0.0), (0, 2.0, 0.0), (0, 3.0, -1.0)]
    assert ca.bucket_spearman(up, min_n=3)["rho"] == ca.bucket_spearman(down, min_n=3)["rho"]


def test_bucket_spearman_aggregate_lies_between_bucket_values():
    # 桶 0：权重与偏离无相关（ρ≈0）；桶 1：ρ≈0.77 → 加权汇总必须落在两者之间
    pairs = [(0, 1.0, 0.0), (0, 2.0, 0.5), (0, 3.0, 1.0),
             (1, 1.0, 0.0), (1, 2.0, 0.0), (1, 3.0, 0.5), (1, 4.0, 1.0)]
    out = ca.bucket_spearman(pairs, min_n=3)
    assert out["hours"] == 2 and out["n"] == 7
    assert 0.0 < out["rho"] < 0.78
    assert out["rho"] == pytest.approx(0.4426, abs=0.01)


# ── Wilson 置信下界 ───────────────────────────────────────────────────────

def test_wilson_lower_guards_empty_sample():
    assert ca._wilson_lower(0, 0) == 0.0


def test_wilson_lower_is_below_point_estimate():
    # 16/25 = 64% 的 95% 下界只有约 45% —— 小样本不能当定论
    lb = ca._wilson_lower(16, 25)
    assert 0.42 < lb < 0.47 and lb < 0.64


def test_wilson_lower_tightens_with_sample_size():
    assert ca._wilson_lower(64, 100) < ca._wilson_lower(640, 1000) < 0.64


def test_wilson_lower_near_one_for_perfect_large_sample():
    assert ca._wilson_lower(1000, 1000) == pytest.approx(0.99, abs=0.01)


# ── 方向命中 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action,move,expect", [
    ("BUY", 1.2, 1), ("BUY", -1.2, 0), ("SELL", -0.5, 1), ("SELL", 0.5, 0),
    ("buy", 0.1, 1),                                    # 大小写不敏感
    ("BUY", 0.0, 0),                                    # 不动不算命中
    ("HOLD", 2.0, None), ("", 2.0, None), (None, 2.0, None),
    ("BUY", None, None),                                # 没价格 → 不可判定
])
def test_direction_hit(action, move, expect):
    assert ca._direction_hit(action, move) is expect


# ── 规则型拒绝 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expect", [
    ("asset_BTC_blacklist", True),
    ("R8:同批次兄弟决策>3", True),
    ("high_strength_gate", True),
    ("HIGH_STRENGTH", True),                            # 大小写不敏感
    ("direction_conflict", False),
    ("", False),
])
def test_rule_rejects_are_not_counted_as_direction_judgement(reason, expect):
    # 黑名单/强度/同批去重是风控拦截，混进"方向判别力"会让闸门看起来什么都能拦
    assert ca._is_rule_reject(reason) is expect


# ── 验收门槛常量 ─────────────────────────────────────────────────────────

def test_thresholds_are_explicit_numbers_not_magic_inline():
    assert ca.THRESHOLDS["settled_samples_min"] >= 10
    assert 0 < ca.THRESHOLDS["entry_hit_rate_min"] <= 1
    assert ca.THRESHOLDS["news_score_positive_pct_min"] == 25.0   # 2026-11-20 用户决策放宽后的值


def test_valid_news_actions_stay_within_three_values():
    # 全库统计按这三个值分组，写进别的值统计会静默丢行
    assert ca.VALID_NEWS_ACTIONS == {"BUY", "SELL", "HOLD"}


def test_valid_dimensions_are_abc_and_subcategories_nonempty():
    assert ca.VALID_DIMENSIONS == {"A", "B", "C"}
    assert len(ca.VALID_SUBCATEGORIES) >= 12
