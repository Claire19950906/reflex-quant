"""v519 · weekly_reflection 的输入输出契约（0% → 行为覆盖）

这个模块每周一 9:00 由 cron 跑一次，把一周的交易数据压成两份文件：
人类周报 + 给 AI 的 prompt。它从来不进 API，所以 CI 的端点测试碰不到它；
一旦格式化层写错，人看到的数字和 AI 看到的数字就是错的，而且没人会发现。

这里只锁"输出必须长什么样"，不锁具体排版：
  · prompt 必须带上三条硬约束（最多 3 条建议 / 只做减法 / 样本 < 5 不下结论）
  · 零样本时不能除零、不能崩
  · 真实 vs 反事实的比例要算对
  · shadow 投影四情景时给出"最佳"与"多过信号是垃圾"的提示
"""
from __future__ import annotations

import weekly_reflection as wr


def _signals(**over):
    base = {
        "generated_at": "2026-11-21 09:00",
        "lookback_days": 7,
        "overall": {"n": 12, "win_rate": 41.7, "avg_pnl": -0.8, "sum_pnl": -9.6},
        "overall_truth": {"n": 3, "win_rate": 33.3, "avg_pnl": -2.1, "sum_pnl": -6.3},
        "per_asset_action": [
            {"asset": "XAU", "action": "BUY", "n": 6, "win_rate": 33.3,
             "avg_pnl": -1.2, "worst": -4.0, "best": 1.5},
        ],
        "per_asset_action_truth": [],
        "mfe_mae": [],
        "mfe_mae_truth": [],
        "decision_volume": {"BUY": 5, "HOLD": 7},
        "guard_breakdown": [{"guard": "no_direction", "n": 4}],
        "quality": {"alerts": []},
        "gate_breakdown": {"buy_or_sell": 5, "blocked": 4, "high_conf_blocked": 2,
                           "by_asset": [{"asset": "XAU", "n": 2, "rejected": 1, "passed": 1}]},
        "shadow_projection": {},
    }
    base.update(over)
    return base


def test_prompt_keeps_the_three_hard_rules():
    p = wr.generate_ai_prompt(_signals())
    assert "最多 3 条" in p
    assert "只做减法" in p
    assert "样本 < 5 笔" in p
    assert "最近 7 天" in p


def test_prompt_renders_per_asset_rows_and_guards():
    p = wr.generate_ai_prompt(_signals())
    assert "XAU" in p and "BUY" in p
    assert "no_direction" in p
    assert '"BUY": 5' in p


def test_prompt_surfaces_quality_alerts():
    p = wr.generate_ai_prompt(_signals(quality={"alerts": ["XAU 连续 3 天缺价"]}))
    assert "XAU 连续 3 天缺价" in p


def test_report_zero_samples_does_not_divide_by_zero():
    s = _signals(overall={"n": 0, "win_rate": 0, "avg_pnl": 0, "sum_pnl": 0},
                 overall_truth={}, per_asset_action=[])
    r = wr.format_human_report(s)
    assert "笔数: 0" in r
    assert "0.0% of 总" in r
    assert "100.0% of 总" in r


def test_report_computes_truth_vs_counterfactual_ratio():
    r = wr.format_human_report(_signals())
    assert "n=  3 ( 25.0% of 总)" in r
    assert "n=  9 ( 75.0% of 总)" in r


def test_report_without_truth_block_defaults_to_zero():
    r = wr.format_human_report(_signals(overall_truth={}))
    assert "n=  0 (  0.0% of 总)" in r


def test_report_gate_breakdown_zero_base_is_safe():
    s = _signals(gate_breakdown={"buy_or_sell": 0, "blocked": 3,
                                 "high_conf_blocked": 1, "by_asset": []})
    r = wr.format_human_report(s)
    # 基线 0 时不要求渲染闸门段，只要求不除零、不崩、报告还在
    assert "周报" in r and "各 guard 拒绝" in r


def test_report_shadow_projection_picks_best_and_warns():
    sp = {
        "total_buy_sell": 40,
        "lookback_days": 7,
        "scenarios": [
            {"label": "现状", "would_pass": 8, "projected_pnl_pct": -9.0, "win_rate_pct": 40},
            {"label": "仅放宽闸门", "would_pass": 14, "projected_pnl_pct": -4.0, "win_rate_pct": 45},
            {"label": "prompt 5x", "would_pass": 20, "projected_pnl_pct": -12.0, "win_rate_pct": 33},
            {"label": "双开", "would_pass": 26, "projected_pnl_pct": -15.0, "win_rate_pct": 30},
        ],
    }
    r = wr.format_human_report(_signals(shadow_projection=sp))
    assert "Shadow 投影" in r
    assert "💡 最佳" in r
    assert "仅放宽闸门" in r
    assert "低胜率垃圾" in r


def test_report_shadow_projection_single_scenario_has_no_best_hint():
    sp = {"total_buy_sell": 10, "lookback_days": 7,
          "scenarios": [{"label": "现状", "would_pass": 2, "projected_pnl_pct": -1.0,
                         "win_rate_pct": 50}]}
    r = wr.format_human_report(_signals(shadow_projection=sp))
    assert "Shadow 投影" in r
    assert "💡 最佳" not in r


def test_gather_signals_contract_when_schema_available():
    """结构契约：缺任何一个键，generate_ai_prompt / format_human_report 都会 KeyError。

    这个模块读的是完整生产 schema（quick_sim_trades 等由 db_schema 建）。
    CI 的临时库不一定建全，所以表缺时跳过，而不是把环境差异当成失败。
    """
    import pytest
    try:
        s = wr.gather_signals(lookback_days=7)
    except Exception as e:  # noqa: BLE001 — 缺表/缺 schema 时跳过
        pytest.skip(f"临时库 schema 不全：{e}")
    for k in ("generated_at", "lookback_days", "overall", "per_asset_action",
              "overall_truth", "per_asset_action_truth", "mfe_mae", "mfe_mae_truth",
              "decision_volume", "guard_breakdown", "quality", "gate_breakdown",
              "shadow_projection"):
        assert k in s, k
    assert s["lookback_days"] == 7
    assert isinstance(s["quality"].get("alerts"), list)


def test_report_renders_mfe_mae_with_and_without_nulls():
    rows = [
        {"asset": "XAU", "action": "BUY", "n": 6, "avg_mfe_pct": 2.4,
         "avg_mae_pct": -3.1, "mfe_realization_pct": 41.7, "avg_pnl_pct": 1.0},
        {"asset": "BTC", "action": "SELL", "n": 2, "avg_mfe_pct": None,
         "avg_mae_pct": None, "mfe_realization_pct": None, "avg_pnl_pct": None},
    ]
    r = wr.format_human_report(_signals(mfe_mae=rows))
    assert "MFE/MAE" in r
    assert "2.40" in r and "-3.10" in r and "41.7" in r
    assert "N/A" in r          # 空值不能变成 None 字符串或崩溃


def test_report_empty_decision_volume_shows_dash():
    r = wr.format_human_report(_signals(decision_volume={}, guard_breakdown=[]))
    assert "🚦 决策打点:" in r
    assert "(无)" in r
