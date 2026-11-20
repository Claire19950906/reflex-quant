"""engine/worker_helpers 的判定口径用例（LLM 输出 → 决策字段那一层）。

worker_helpers 是"把模型吐的文本变成可入库的决策"的地方，几处判错都是真事故：
- `strip_think_blocks` 曾经把未闭合 `<think>` 删到文末，连带 1455 字的完整 JSON 一起削没；
- `reconcile_score_action` 决定了"AI 说观望"会不会被分数抬成方向单——这正是对外卖点里
  「剔掉不该做的单」的第一道口；
- `_safe_child_count` 曾因一个脏值 `'OIL|WTI|BUY'` 让整批 16 条落库作废、队头反复占位。
"""

import json

import pytest

from engine import worker_helpers as wh


# ── think 块剥离 ──────────────────────────────────────────────────────────

def test_closed_think_pair_is_removed_json_kept():
    raw = "<think>想了一堆</think>{\"action\": \"BUY\"}"
    assert wh.strip_think_blocks(raw) == '{"action": "BUY"}'


def test_unclosed_think_keeps_the_json_after_it():
    # v342 事故：未闭合时不能从标签删到文末，否则 JSON 一起没
    raw = "<think>推理没闭合 {\"action\": \"SELL\", \"score\": -0.6}"
    out = wh.strip_think_blocks(raw)
    assert out.startswith("{") and '"action"' in out


def test_unclosed_think_without_json_only_drops_the_tag():
    raw = "<think>只有推理没有结构化输出"
    assert wh.strip_think_blocks(raw) == ""


def test_plain_text_passes_through():
    assert wh.strip_think_blocks("普通文本") == "普通文本"
    assert wh.strip_think_blocks(None) == ""


# ── 空输出判定 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "   ", "<think>只有思考</think>", "```json\n{}\n```"])
def test_empty_llm_output_detects_nothing_useful(raw):
    assert wh.is_empty_llm_output(raw) is True


def test_empty_llm_output_false_when_meaningful_text():
    assert wh.is_empty_llm_output("这是一段足够长的正常分析文本，超过十个字") is False


# ── 分数与方向对齐（"别把观望抬成方向单"）────────────────────────────────

def test_hold_always_zeroes_score():
    # AI 说观望就观望：分数再高也不能被改写成 BUY
    assert wh.reconcile_score_action(0.8, "HOLD") == (0.0, "HOLD")
    assert wh.reconcile_score_action(-0.8, "hold") == (0.0, "HOLD")


def test_buy_with_negative_score_flips_to_positive():
    score, action = wh.reconcile_score_action(-0.4, "BUY")
    assert action == "BUY" and score == pytest.approx(0.4)


def test_buy_with_near_zero_score_gets_weak_same_direction():
    # 只有"分数 ≤ 0（含 0）"才会补一个同向弱分；已经是正的极小值原样保留，不再抬
    assert wh.reconcile_score_action(0.0, "BUY") == (0.05, "BUY")
    assert wh.reconcile_score_action(-0.0005, "BUY")[0] == pytest.approx(0.05)
    assert wh.reconcile_score_action(0.0005, "BUY")[0] == pytest.approx(0.0005)


def test_sell_with_positive_score_turns_negative():
    score, action = wh.reconcile_score_action(0.6, "SELL")
    assert action == "SELL" and score == pytest.approx(-0.6)
    assert wh.reconcile_score_action(0.0, "SELL") == (-0.05, "SELL")


def test_score_is_clamped_and_garbage_action_becomes_hold():
    assert wh.reconcile_score_action(5.0, "BUY")[0] == pytest.approx(1.0)
    assert wh.reconcile_score_action("abc", "SELL")[0] == -0.05
    assert wh.reconcile_score_action(0.5, "随便什么") == (0.0, "HOLD")


@pytest.mark.parametrize("action,score,expect", [
    ("HOLD", 0.9, True), ("BUY", 0.3, True), ("BUY", -0.3, False),
    ("SELL", -0.3, True), ("SELL", 0.3, False), ("BUY", 0.0, False), ("乱写", 0.0, False),
])
def test_direction_consistent(action, score, expect):
    assert wh.direction_consistent(action, score) is expect


# ── 计数列脏值 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expect", [
    (3, 3), ("12", 12), (None, 0), ("", 0),
    ("OIL|WTI|BUY", 0),      # v322：聚合键错位写进 child_count，int() 曾炸掉整批
    (3.7, 3), ([1, 2], 0),
])
def test_safe_child_count_never_explodes(value, expect):
    assert wh._safe_child_count(value) == expect


# ── 占位名判定 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expect", [
    ("", True), ("N/A", True), ("none", True), ("服务端兜底", True),
    ("因素名", True), ("待定", True),
    ("美联储降息预期", False), ("BTC", False),
])
def test_looks_placeholder(name, expect):
    assert wh._looks_placeholder(name) is expect


# ── 未闭合 think / 骨架结果 ──────────────────────────────────────────────

def test_has_unclosed_think():
    assert wh.has_unclosed_think("<think>没闭合") is True
    assert wh.has_unclosed_think("<think>闭合了</think>") is False
    assert wh.has_unclosed_think("没有思考块") is False


def test_skeleton_result_detects_empty_body():
    # 实测 tokens_out=33 能返回合法 JSON，但正文合计只有几个字
    assert wh.is_skeleton_result({"target_asset": "BTC", "action": "HOLD"}) is True
    assert wh.is_skeleton_result("not a dict") is True
    full = {"reasoning": "这条新闻影响通胀预期，",
            "reasoning_path": "通胀→利率→美元→黄金，",
            "asset_nature": "避险资产与利率敏感，",
            "asset_reasoning": "金价与美债实际利率负相关"}
    assert wh.is_skeleton_result(full) is False


# ── 字段来源标记与完整度扣分 ──────────────────────────────────────────────

def test_mark_field_source_accumulates_json():
    res = {}
    wh._mark_field_source(res, "reasoning", "llm")
    wh._mark_field_source(res, "asset_nature", "rule:alias")
    assert json.loads(res["field_sources"]) == {"reasoning": "llm", "asset_nature": "rule:alias"}


def test_mark_field_source_recovers_from_corrupt_json():
    res = {"field_sources": "不是 json"}
    wh._mark_field_source(res, "reasoning", "llm")
    assert json.loads(res["field_sources"]) == {"reasoning": "llm"}


def test_mark_field_missing_deducts_weight_once_and_floors_at_zero():
    res = {}
    wh._mark_field_missing(res, "reasoning_path")          # weight 0.25
    assert json.loads(res["missing_fields"]) == ["reasoning_path"]
    assert res["completeness_score"] == pytest.approx(0.75)

    # 已知口径（缺陷，未改）：判据只检查字段是否已在 missing_fields 里，
    # 不检查"这一笔是否已经扣过分"——同一字段再标一次会**再扣一次**权重。
    # 结果是重复标记时 completeness 掉得比设计快（0.75 → 0.5），不是幂等的。
    wh._mark_field_missing(res, "reasoning_path")
    assert res["completeness_score"] == pytest.approx(0.5)

    for _ in range(20):
        wh._mark_field_missing(res, "reasoning")           # 扣到底不出现负数
    assert res["completeness_score"] == 0.0
    # v461 修复：0 分是合法值，不能再被 `or 1.0` 重置回 1.0 重新起扣
    wh._mark_field_missing(res, "verification")
    assert res["completeness_score"] == 0.0


def test_count_sources_groups_rule_and_kb_variants():
    counts = wh._count_sources({
        "a": "llm", "b": "rule", "c": "rule:alias", "d": "business_knowledge",
        "e": "historical_pattern", "f": "llm_ensemble_consensus", "g": "missing", "h": "别的",
    })
    assert counts["llm"] == 1 and counts["rule"] == 2 and counts["business_kb"] == 1
    assert counts["historical"] == 1 and counts["ensemble"] == 1 and counts["missing"] == 1


# ── schema 重试的放行口径 ────────────────────────────────────────────────

@pytest.mark.parametrize("is_slow,missing,allow,expect", [
    (False, ["target_asset"], True, True),        # 快通道：有缺口就重试
    (False, [], True, False),                     # 没缺口不重试
    (True, ["target_asset"], True, True),         # 慢通道只救"必然是废数据"的
    (True, ["reasoning", "asset_nature", "verification"], True, True),   # 缺口 >= 3
    (True, ["reasoning"], True, False),           # 慢通道小缺口不烧调用
    (True, ["target_asset"], False, False),       # 全局关掉
])
def test_should_schema_retry(is_slow, missing, allow, expect):
    assert wh.should_schema_retry(is_slow, missing, allow_slow_retry=allow) is expect
