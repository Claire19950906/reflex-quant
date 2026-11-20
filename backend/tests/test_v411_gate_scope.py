"""v411b：门卫调用面收紧开关（GATE_CALL_SCOPE）单元测试。

默认 all → 行为不变（每条非噪音都进门卫）。
pool → 只对"命中池内资产/宏观驱动锚点"的新闻调门卫，其余按未判定放行（gate_unavailable=2）。

⚠️ 实测结论（2026-11-17 近 3h 103 条非噪音）：pool 会跳过 50 条（48.5%），
但被跳过的正是 target_none 的主要来源（美国 AI 出口计划 / 巴西芯片措施 这类"有国名无品种"的稿子）。
所以开关默认关，代码留着是为了在配额彻底打满时有手段，而不是因为它更好。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python"))

import realtime_filter as rf


def test_default_never_skips(monkeypatch):
    monkeypatch.delenv("GATE_CALL_SCOPE", raising=False)
    assert rf._gate_scope_should_skip("美国公布下一阶段 AI 出口计划") is False


def test_pool_skips_without_anchor(monkeypatch):
    monkeypatch.setenv("GATE_CALL_SCOPE", "pool")
    assert rf._gate_scope_should_skip("美国公布下一阶段 AI 出口计划") is True
    assert rf._gate_scope_should_skip("巴西无法立即评估美国芯片措施的影响") is True


def test_pool_keeps_asset_anchored(monkeypatch):
    monkeypatch.setenv("GATE_CALL_SCOPE", "pool")
    assert rf._gate_scope_should_skip("OPEC 宣布减产 200 万桶/日") is False
    assert rf._gate_scope_should_skip("美联储官员称降息节奏取决于通胀") is False
    assert rf._gate_scope_should_skip("比特币ETF 单日净流入 5 亿美元") is False


def test_unknown_value_behaves_like_all(monkeypatch):
    """写错值（如 pool→pools）不能悄悄收紧，必须按 all 走。"""
    monkeypatch.setenv("GATE_CALL_SCOPE", "pools")
    assert rf._gate_scope_should_skip("美国公布 AI 出口计划") is False


def test_empty_text_does_not_crash(monkeypatch):
    monkeypatch.setenv("GATE_CALL_SCOPE", "pool")
    assert rf._gate_scope_should_skip("") in (True, False)
