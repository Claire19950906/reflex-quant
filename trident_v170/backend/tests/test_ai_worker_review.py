"""核心模块纯函数测试：ai_worker 幻觉防护、history_review 数据函数、forward 配置兜底。

聚焦不依赖外部服务 / DB / 网络的纯函数，覆盖：
  · reconcile_score_action —— LLM 方向/分数对齐（HOLD 归零、符号翻转、非法兜底）
  · direction_consistent    —— 方向一致性校验
  · _norm_score_action      —— 分数/方向规范化（BUY/SELL 同向弱兜底，符号一致）
  · _direction_leaning      —— 文本方向词倾向
  · _expand_multi_asset_results —— 多品种展开的透传/去重
  · history_review._bucket/_parse_ts/_fmt_ts/_clean_news —— 数据函数边界
  · forward._get_tp_settings —— 分级止盈兜底结构
"""

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine import ai_worker as w
import history_review as hr


# ──────────────────── ai_worker: 方向/分数对齐 ────────────────────

def test_reconcile_hold_neutralizes():
    assert w.reconcile_score_action(0.8, "HOLD") == (0.0, "HOLD")
    assert w.reconcile_score_action(-0.8, "HOLD") == (0.0, "HOLD")


def test_reconcile_flips_sign_to_match_action():
    assert w.reconcile_score_action(-0.8, "BUY") == (0.8, "BUY")
    assert w.reconcile_score_action(0.8, "SELL") == (-0.8, "SELL")


def test_reconcile_clamp():
    assert w.reconcile_score_action(1.5, "BUY") == (1.0, "BUY")
    assert w.reconcile_score_action(-1.5, "SELL") == (-1.0, "SELL")


def test_reconcile_invalid_action_and_score():
    assert w.reconcile_score_action(0.8, "XYZ") == (0.0, "HOLD")
    # 非法分数 → 0 → BUY 弱分兜底 0.05
    assert w.reconcile_score_action("not-a-number", "BUY") == (0.05, "BUY")


def test_reconcile_zero_buy_floor():
    assert w.reconcile_score_action(0.0, "BUY") == (0.05, "BUY")
    assert w.reconcile_score_action(0.0, "SELL") == (-0.05, "SELL")


def test_direction_consistent():
    assert w.direction_consistent("BUY", 0.8) is True
    assert w.direction_consistent("BUY", -0.8) is False
    assert w.direction_consistent("SELL", -0.8) is True
    assert w.direction_consistent("SELL", 0.8) is False
    assert w.direction_consistent("HOLD", 0.8) is True
    assert w.direction_consistent("BUY", "not-a-number") is False


def test_norm_score_action_clamp_and_hold():
    # 2026-10-08: 0-100 整数 100 → 1.0 (a 档 /100 clamp 上限)
    # 旧测试用 1.5 是测 "clamp 上限"，但 P2 #10 3-tier 把它判为 0-100 整数（1.5/100=0.015）
    # 改用 100 直接表达 "已经规范化到 -1~1 范围"的 1.0 上限（clamp 结果）
    assert w._norm_score_action(100, "BUY") == (1.0, "BUY")
    # -1.5: 0-100 范围里 SELL 强信号 → -1 (c 档 -1 已经 < 0 直接 clamp)
    assert w._norm_score_action(-1.5, "SELL") == (-1.0, "SELL")
    # 显式 0-100 整数 -100 (极端 SELL) → c 档 -1.0
    assert w._norm_score_action(-100, "SELL") == (-1.0, "SELL")
    # v200 P1-G: HOLD 不再强制 score=0；仅 < 0.05 弱信号才归 0
    assert w._norm_score_action(0.8, "HOLD") == (0.8, "HOLD")
    # 弱信号(<0.05) 仍归 0
    assert w._norm_score_action(0.03, "HOLD") == (0.0, "HOLD")
    assert w._norm_score_action(0.0, "BUY") == (0.05, "BUY")


def test_norm_score_action_sell_sign_consistency():
    # SELL 方向单兜底分数应为负，与 BUY 的 +0.05 对称（已修复符号不一致）
    assert w._norm_score_action(0.0, "SELL") == (-0.05, "SELL")
    assert w._norm_score_action(0.0, "BUY") == (0.05, "BUY")
    # 与 reconcile_score_action 的正确口径保持一致
    assert w._norm_score_action(0.0, "SELL")[0] == w.reconcile_score_action(0.0, "SELL")[0]
    assert w._norm_score_action(0.0, "BUY")[0] == w.reconcile_score_action(0.0, "BUY")[0]


def test_sanitize_llm_result_whitelist():
    # 整箱白名单过滤：缺字段补默认、非法夹取/白名单回落，绝不整条丢弃
    # 2026-10-08: 1.8 → 0-100 整数 1.8/100 = 0.018 (P2 #10 3-tier)
    # 改用 100 测试 "0-100 clamp 上限" → 1.0
    out = w._sanitize_llm_result({
        "reasoning": "买", "sentiment_score": 100, "suggested_action": "buy",
        "target_asset": "btc", "market_category": "crypto",
    })
    assert out["reasoning"] == "买"
    assert out["sentiment_score"] == 1.0          # 0-100 整数 100 / 100 = 1.0 (clamp 上限)
    assert out["suggested_action"] == "BUY"       # 白名单归一大写
    assert out["target_asset"] == "BTC"           # 大写截断
    assert out["market_category"] == "CRYPTO"     # 白名单归一

    # 幻觉字段类型：数值给字符串、方向非法、reasoning/资产给列表、分类给数字
    out2 = w._sanitize_llm_result({
        "sentiment_score": "not-a-number",
        "suggested_action": "SHORT",
        "reasoning": ["不是字符串"],
        "target_asset": ["XAU"],
        "market_category": 123,
    })
    assert out2["sentiment_score"] == 0.0
    assert out2["suggested_action"] == "HOLD"
    assert out2["reasoning"] == ""
    assert out2["target_asset"] == "NONE"
    # 2026-10-08: v1 重构后非法 market_category 留空（让前端渲染「待复核」徽章）
    # 旧 v1 之前 fallback "OTHER" 已废弃；其他字段仍保持合法默认值
    assert out2["market_category"] == ""
    # 检查是否正确打上 unclassified 标记 + quarantined_at
    assert out2.get("_unclassified_layer") == "C_sanitize_step5"
    assert "INVALID_AT_STEP5" in out2.get("_unclassified_reason", "")
    assert out2.get("quarantined_at") is not None

    # 空输入/非 dict：返回字段齐全的安全 dict（整箱兜底，绝不 KeyError）
    out3 = w._sanitize_llm_result(None)
    assert out3["reasoning"] == ""
    assert out3["sentiment_score"] == 0.0
    assert out3["suggested_action"] == "HOLD"
    assert out3["target_asset"] == "NONE"
    # None 输入 → 同样空 mc 走 unclassified 兜底
    assert out3["market_category"] == ""
    assert out3["direct_catalyst"] is False
    assert out3["reasoning_path"] == ""


def test_action_leaning():
    assert w._action_leaning("BUY") == 1
    assert w._action_leaning("SELL") == -1
    assert w._action_leaning("HOLD") == 0
    assert w._action_leaning("xyz") == 0
    assert w._action_leaning(None) == 0


def test_direction_leaning():
    assert w._direction_leaning("利好，上涨") == 1
    assert w._direction_leaning("下跌，承压抛售") == -1
    assert w._direction_leaning("利好 但 下跌") == 0  # 正负相抵
    assert w._direction_leaning("") == 0
    assert w._direction_leaning(None) == 0


def test_expand_multi_asset_no_siblings_passthrough():
    item = {"news_id": 1, "result": {"target_asset": "XAU"}}
    assert w._expand_multi_asset_results([item]) == [item]
    assert w._expand_multi_asset_results([]) == []
    assert w._expand_multi_asset_results([{"result": {"sibling_decisions": "x"}}]) == [
        {"result": {"sibling_decisions": "x"}}
    ]
    assert w._expand_multi_asset_results([{"result": {"sibling_decisions": []}}]) == [
        {"result": {"sibling_decisions": []}}
    ]


# ──────────────────── history_review: 数据函数 ────────────────────

def test_clean_news_strips_hash_and_truncates():
    assert hr._clean_news("[hash:abc123] 新闻内容") == "新闻内容"
    assert hr._clean_news("正常文本") == "正常文本"
    assert hr._clean_news(None) == ""
    assert hr._clean_news("x" * 200, max_len=10) == "x" * 10


def test_fmt_ts_empty_and_invalid():
    assert hr._fmt_ts(None) == ""
    assert hr._fmt_ts(0) == ""          # 0 被当缺失
    assert hr._fmt_ts("bad") == ""
    assert ":" in hr._fmt_ts(1700000000)  # 有效时间戳有 HH:MM


def test_parse_ts():
    assert hr._parse_ts(None) is None
    assert hr._parse_ts("") is None
    assert hr._parse_ts("garbage") is None
    assert hr._parse_ts("2024-01-15 10:30:00") is not None
    assert hr._parse_ts("2024-01-15T10:30:00+08:00") is not None


def test_bucket_win_loss_pnl_fallback():
    assert hr._bucket({"is_correct": "WIN"}) == "WIN"
    assert hr._bucket({"verdict": "LOSS"}) == "LOSS"
    assert hr._bucket({"pnl_pct": 1.5}) == "WIN"
    assert hr._bucket({"pnl_pct": -1.5}) == "LOSS"
    assert hr._bucket({"pnl_pct": 0.0}) is None
    assert hr._bucket({}) is None
    assert hr._bucket({"pnl_pct": "abc"}) is None


# ──────────────────── forward: 分级止盈兜底 ────────────────────

def test_forward_tp_settings_defaults():
    forward = pytest.importorskip("engine.forward")
    s = forward._get_tp_settings()
    assert isinstance(s, dict)
    assert "tp1_mult" in s and "tp2_mult" in s
    assert "tp1_ratio" in s and "tp2_ratio" in s