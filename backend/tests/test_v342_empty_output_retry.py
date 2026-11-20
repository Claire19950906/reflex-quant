# -*- coding: utf-8 -*-
"""v342 — 空输出判定与补一次重试（is_empty_llm_output）

钉死的四件事：
  1. 真空 / 只有 think / 只有空 code fence → 算空（会触发补一次）
  2. 一个正常 HOLD 的短 JSON（>10 字）→ 不算空（绝不重复烧额度）
  3. 八段推演文本 → 不算空
  4. 探针计数接口可在无表时调用（重试失败也能记）
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine.ai_worker import (
    rescue_budget_ok,
    merge_two_step,
    should_schema_retry,
    has_unclosed_think,
    is_empty_llm_output,
    strip_think_blocks,
)
import llm_degrade_probe as ldp


def test_truly_empty_variants():
    assert is_empty_llm_output("") is True
    assert is_empty_llm_output("   \n\t ") is True
    assert is_empty_llm_output(None) is True
    assert is_empty_llm_output("<think>想一下……</think>") is True
    assert is_empty_llm_output("```json\n```") is True


def test_short_hold_json_is_not_empty():
    payload = '{"sentiment_score": 0, "suggested_action": "HOLD"}'
    assert is_empty_llm_output(payload) is False


def test_full_analysis_is_not_empty():
    text = "[总框架] 目标资产 XAU；[因素定位] CPI 超预期；[总结] 观望"
    assert is_empty_llm_output(text) is False


def test_probe_counters_callable_without_table():
    ldp.note_retry_failed("ReadTimeout")
    ldp.note_retry_ok()
    snap = ldp.last_raw()
    assert snap["empty_retry_failed"] >= 1
    assert snap["empty_retry_ok"] >= 1


# ── 未闭合 <think> 抢救（v342 事故回归）────────────────────────────────────
# 现场日志实证：`原文 1455 字 → 剥 think 后只剩 0 字`，
# 因为老逻辑对未闭合 <think> 是"删到文末"，把后面的完整 JSON 一起删了。
_UNCLOSED = (
    '<think>We need output JSON only. Need assess news likely WTI bullish.\n'
    '{"target_asset":"WTI","sentiment_score":0.4,"suggested_action":"BUY"}'
)


def test_unclosed_think_keeps_json():
    assert has_unclosed_think(_UNCLOSED) is True
    out = strip_think_blocks(_UNCLOSED)
    assert out.startswith('{"target_asset"')
    import json
    assert json.loads(out)["target_asset"] == "WTI"


def test_paired_think_still_stripped():
    assert has_unclosed_think("<think>a</think>{}") is False
    assert strip_think_blocks('<think>想一下</think>\n{"a":1}').startswith('{"a"')
    assert strip_think_blocks("<think>纯思考没 JSON") == ""


def test_unclosed_think_with_json_is_not_empty():
    assert is_empty_llm_output(_UNCLOSED) is False


# ── 慢通道重试死开关（v342）────────────────────────────────────────────────
# 现场事实：MiniMax 被 `_is_slow_channel()` 判为慢通道 → schema 补全 retry
# **在生产通道上永远不触发**，缺字段只能降级。这里钉死放行规则。


def test_fast_channel_always_retries_on_gap():
    assert should_schema_retry(False, ["verification"]) is True


def test_slow_channel_rescued_when_target_missing():
    assert should_schema_retry(True, ["target_asset", "verification"]) is True


def test_slow_channel_rescued_at_three_gaps():
    assert should_schema_retry(True, ["verification", "fund_driver", "asset_nature"]) is True


def test_slow_channel_still_skips_small_gap():
    assert should_schema_retry(True, ["verification"]) is False
    assert should_schema_retry(True, []) is False


def test_env_kill_switch():
    assert should_schema_retry(True, ["target_asset"], allow_slow_retry=False) is False


# ── 两步抢救合并规则（v342）────────────────────────────────────────────────
# 第一步：只要 target_asset + 方向；第二步：只要八段 + 证据。


def test_two_step_merges_target_and_sections():
    base = {"target_asset": "NONE", "suggested_action": "HOLD", "sentiment_score": 0}
    a = {"target_asset": "WTI", "suggested_action": "BUY", "sentiment_score": 0.4,
         "market_category": "OIL"}
    b = {"reasoning_path": "[总框架] 目标资产 WTI；[因素定位] 俄炼厂遇袭；[总结] 偏多",
         "asset_nature": "能源品，受地缘供给扰动",
         "factor_ranking": [{"factor": "supply", "weight": 0.6}],
         "reasoning": "俄炼厂遇袭压制供给，短期偏多。"}
    out, notes = merge_two_step(base, a, b)
    assert out["target_asset"] == "WTI"
    assert out["suggested_action"] == "BUY"
    assert out["sentiment_score"] == 0.4
    assert out["reasoning_path"].startswith("[总框架]")
    assert out["factor_ranking"]
    assert "标的=WTI" in notes


def test_two_step_aborts_when_target_locked_fails():
    base = {"target_asset": "NONE"}
    a = {"target_asset": "OTHER"}
    b = {"reasoning_path": "[总框架] 目标资产 OTHER"}
    out, notes = merge_two_step(base, a, b)
    assert out["target_asset"] == "NONE"   # 原值不动
    assert notes == []


def test_two_step_does_not_overwrite_with_empty():
    base = {"reasoning_path": "[总框架] 旧内容", "asset_nature": "旧性质"}
    a = {"target_asset": "XAU"}
    b = {"reasoning_path": "", "asset_nature": "  ", "factor_ranking": []}
    out, _ = merge_two_step(base, a, b)
    assert out["reasoning_path"] == "[总框架] 旧内容"
    assert out["asset_nature"] == "旧性质"


# ── 两步抢救限流（v342）────────────────────────────────────────────────────


def test_rescue_budget_caps_within_window():
    st = {"window_start": 0, "used": 0}
    ok = [rescue_budget_ok(1000 + i, st, limit=6, window=600) for i in range(8)]
    assert sum(ok) == 6


def test_rescue_budget_resets_after_window():
    st = {"window_start": 1000.0, "used": 6}
    assert rescue_budget_ok(1700.0, st, limit=6, window=600) is True
