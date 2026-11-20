# -*- coding: utf-8 -*-
"""v316 回归锁：聚合触发条件 / cluster_size / prompt 量纲 / SELL 闸门白名单冲突。

背景（2026-10-30 对生产库 6200 条 ai_decisions 的实测审计）：
  1) sentiment_score 有 94.6%（5866/6200）塌陷为 0.0；
  2) ai_worker 的聚合触发条件是
        `action in ("BUY","SELL") and abs(score) >= _AGG_MIN_SCORE(0.25)`
     → 聚合几乎从不触发 → parent_id 恒 None → cluster_size 恒 1；
  3) 前置 decision_guard 评估还把 cluster_size 写死为 1
     → cluster_heat 因子（权重 0.10）永久为 0 → final_score 更无区分度
     → 更多 HOLD（自我强化死循环）。
  4) prompt 量纲自相矛盾：主 prompt 要求 0-100 整数（50=中性），
     重试提示却要求「±0.03~0.10」（-1~1 量纲）。
  5) SELL_GATE_BLOCKED_ASSETS 默认含 WTI，与 SIM_ALLOWED_ASSETS=XAU,WTI 冲突
     → WTI 只能做多、永远不能做空（trade_decisions 实测 SELL 拒签 1974/2111 次）。

本文件把这些"已经修好的东西"钉住，防止回退。
全部离线：源码级断言 + decision_guard 纯函数调用（无网络、无 LLM）。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AI_WORKER = os.path.join(SRC, "engine", "ai_worker.py")


def _ai_worker_src() -> str:
    return pathlib.Path(AI_WORKER).read_text(encoding="utf-8")


# ── 1. 聚合触发条件不再被 sentiment_score 卡住 ──────────────────────────────
def test_aggregation_trigger_not_gated_by_sentiment_score():
    """聚合判据必须是「方向相同」，不能要求 |sentiment_score| >= 0.25。"""
    src = _ai_worker_src()
    assert 'action in ("BUY", "SELL") and abs(score) >= _AGG_MIN_SCORE' not in src, (
        "聚合触发条件又退回到依赖 sentiment_score 绝对值 —— sentiment_score 94.6% 为 0，"
        "会让聚合永不触发 → cluster_size 恒 1 → cluster_heat 因子永久为 0"
    )
    assert 'if action in ("BUY", "SELL"):' in src


def test_agg_min_score_constant_still_defined():
    """_AGG_MIN_SCORE 常量必须保留（config.py 仍 export，被 score 加权逻辑引用）。"""
    cfg = pathlib.Path(os.path.join(SRC, "config.py")).read_text(encoding="utf-8")
    assert "_AGG_MIN_SCORE" in cfg


# ── 2. 前置评估不再把 cluster_size 写死为 1 ────────────────────────────────
def test_pre_guard_cluster_size_not_hardcoded():
    """前置 evaluate_decision 必须传真实同族数量，不能写死 1。"""
    src = _ai_worker_src()
    assert '"cluster_size": 1,' not in src, (
        "前置 decision_guard 评估又把 cluster_size 写死成 1 —— "
        "入库的 suggested_action/evidence_action/evidence_confidence 会永久丢掉 "
        "cluster_heat 因子（权重 0.10）的贡献"
    )
    assert '"cluster_size": _cluster_hint_pre,' in src


# ── 3. cluster_heat 因子确实随 cluster_size 变化（行为级） ────────────────
def _decision_row(cluster_size: int) -> dict:
    return {
        "sentiment_score": 0.0,
        "target_asset": "XAU",
        "market_confirmation": "unknown",
        "decision_context": json.dumps({
            "assets": {
                "XAU": {
                    "change_24h_pct": 0.0,
                    "funding_rate_pct": 0.0,
                    "stats_7d": {"trend": "Unknown", "atr_pct": 0.0},
                }
            }
        }),
        "cluster_size": cluster_size,
        "impact_score": 5.0,
        "heat_score": 5.0,
        "asset_impact": 5.0,
    }


def test_cluster_heat_factor_responds_to_cluster_size():
    import decision_guard

    solo = decision_guard.evaluate_decision(_decision_row(1))
    trio = decision_guard.evaluate_decision(_decision_row(3))
    five = decision_guard.evaluate_decision(_decision_row(5))

    assert solo["factors"]["cluster_heat"]["score"] == 0.0
    assert trio["factors"]["cluster_heat"]["score"] == pytest.approx(0.5)
    assert five["factors"]["cluster_heat"]["score"] == pytest.approx(1.0)


def test_cluster_heat_has_weight_and_raises_final_score():
    """v318：cluster_heat 改成**质量因子**。

    v316 的原始诉求「聚合命中必须让决策有区分度」依然成立，但作用点从
    final_score 换成了 confidence：
      · 聚合同族新闻**不代表方向更强**（同一事件被 3 家媒体报 ≠ 更看多），
        所以它不再参与方向分；
      · 但它代表**佐证**（同一族有其它新闻独立出现），所以要抬高置信度。

    旧的断言（cluster_heat 权重 0.10 且抬高 final_score）在 v318 已不成立：
    质量因子在 directions 之外计算，且不再污染方向。
    """
    import decision_guard

    solo = decision_guard.evaluate_decision(_decision_row(1))
    trio = decision_guard.evaluate_decision(_decision_row(3))

    # 不再参与方向
    assert solo["final_score"] == trio["final_score"]
    # 不在方向权重里（方向权重和必须为 1.0）
    assert "cluster_heat" not in solo["weights"]
    assert abs(sum(solo["weights"].values()) - 1.0) < 1e-9
    # 但仍是因子，且标记为质量因子
    assert solo["factors"]["cluster_heat"]["role"] == "quality"

    # 佐证效应：同族新闻越多 → 置信度越高（需要真实方向分才有 base）
    solo_row = _decision_row(1)
    solo_row["sentiment_score"] = 0.5
    trio_row = _decision_row(3)
    trio_row["sentiment_score"] = 0.5
    assert (
        decision_guard.evaluate_decision(trio_row)["confidence"]
        > decision_guard.evaluate_decision(solo_row)["confidence"]
    )


# ── 4. prompt 量纲一致 ─────────────────────────────────────────────────────
def test_sentiment_prompt_scale_consistent():
    """主 prompt 0-100 与重试提示必须同量纲；旧的 ±0.03~0.10 不得残留。"""
    src = _ai_worker_src()
    assert "±0.03~0.10" not in src, (
        "重试提示仍要求 sentiment_score 给 ±0.03~0.10（-1~1 量纲），"
        "与主 prompt 的 0-100 整数自相矛盾"
    )
    assert "0-100" in src


# ── 5. SELL 闸门拒签清单不得与交易白名单冲突 ───────────────────────────────
def _read_env(path: str) -> str:
    p = pathlib.Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _env_value(text: str, key: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return None


@pytest.mark.parametrize("env_name", [".env", ".env.example"])
def test_sell_gate_blocked_assets_does_not_conflict_with_sim_allowlist(env_name):
    """WTI 不能既在 SIM_ALLOWED_ASSETS 白名单、又被 SELL_GATE 无条件拒签。"""
    text = _read_env(os.path.join(BACKEND, env_name))
    if not text.strip():
        pytest.skip("%s 不存在（CI 环境无本地 .env）" % env_name)

    blocked = _env_value(text, "SELL_GATE_BLOCKED_ASSETS")
    allowed = _env_value(text, "SIM_ALLOWED_ASSETS")
    if blocked is None or allowed is None:
        pytest.skip("%s 未显式配置这两个 key" % env_name)

    blocked_set = {a.strip().upper() for a in blocked.split(",") if a.strip()}
    allowed_set = {a.strip().upper() for a in allowed.split(",") if a.strip()}
    overlap = blocked_set & allowed_set
    assert not overlap, (
        "SELL_GATE_BLOCKED_ASSETS=%s 与 SIM_ALLOWED_ASSETS=%s 冲突：%s "
        "会导致该资产只能单向交易（WTI 曾因此 SELL 100%% 被拒签）"
        % (blocked, allowed, sorted(overlap))
    )
