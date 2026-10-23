"""v302+ · MINIMAX_MAIN_WEIGHT / MINIMAX2_WEIGHT env 覆盖测试。

覆盖:
  · 默认（未设）→ MINIMAX_MAIN_WEIGHT=2 / MINIMAX2_WEIGHT=4
  · MINIMAX_MAIN_WEIGHT=0 → 主通道从 roster 摘除
  · MINIMAX2_WEIGHT=0 → #2 通道从 roster 摘除
  · 两者均为 0 → roster 为空（极端情况,生产应避免）
  · 空字符串 → 回落到默认 2
  · 负数 → 摘除（防止误设 -1 投票反向）

这些测试不调用 _build_model_pool()，只验证 config.AI_MODEL_ROSTER 的内容。
"""
from __future__ import annotations

import importlib
import sys

import pytest

sys.path.insert(0, "src_python")


@pytest.fixture
def reloaded_config(monkeypatch):
    """每个测试都用干净 env 重载 config 模块,monkeypatch 自动清理。"""
    yield monkeypatch
    import config
    importlib.reload(config)


def test_default_weights(reloaded_config):
    """未设 env 时默认主=2, #2=4。"""
    reloaded_config.delenv("MINIMAX_MAIN_WEIGHT", raising=False)
    reloaded_config.delenv("MINIMAX2_WEIGHT", raising=False)
    import config
    importlib.reload(config)
    ids = {m["id"]: m["weight"] for m in config.AI_MODEL_ROSTER}
    if "minimax/MiniMax-M3" in ids:
        assert ids["minimax/MiniMax-M3"] == 2, f"main weight should be 2, got {ids['minimax/MiniMax-M3']}"
    if "minimax2/MiniMax-M3" in ids:
        assert ids["minimax2/MiniMax-M3"] == 4, f"#2 weight should be 4, got {ids['minimax2/MiniMax-M3']}"


def test_main_weight_zero_disables_channel(reloaded_config):
    """MINIMAX_MAIN_WEIGHT=0 → 主通道从 roster 完全摘除。"""
    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "0")
    reloaded_config.delenv("MINIMAX2_WEIGHT", raising=False)
    import config
    importlib.reload(config)
    ids = [m["id"] for m in config.AI_MODEL_ROSTER]
    assert "minimax/MiniMax-M3" not in ids, "main channel should be removed when weight=0"
    # #2 仍在
    assert any(m["id"] == "minimax2/MiniMax-M3" for m in config.AI_MODEL_ROSTER)


def test_main2_weight_zero_disables_channel(reloaded_config):
    """MINIMAX2_WEIGHT=0 → #2 通道从 roster 摘除。"""
    reloaded_config.setenv("MINIMAX2_WEIGHT", "0")
    reloaded_config.delenv("MINIMAX_MAIN_WEIGHT", raising=False)
    import config
    importlib.reload(config)
    ids = [m["id"] for m in config.AI_MODEL_ROSTER]
    assert "minimax2/MiniMax-M3" not in ids, "#2 channel should be removed when weight=0"


def test_both_zero_yields_empty_minimax_pool(reloaded_config):
    """极端:两者都为 0 → minimax 系列在 roster 里都被摘除。"""
    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "0")
    reloaded_config.setenv("MINIMAX2_WEIGHT", "0")
    import config
    importlib.reload(config)
    ids = [m["id"] for m in config.AI_MODEL_ROSTER]
    minimax_ids = [i for i in ids if "minimax" in i]
    assert minimax_ids == [], f"all minimax channels should be removed, got {minimax_ids}"


def test_empty_weight_string_falls_back_to_default(reloaded_config):
    """空字符串 → 回落到默认 2。"""
    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "")
    import config
    importlib.reload(config)
    ids = {m["id"]: m["weight"] for m in config.AI_MODEL_ROSTER}
    if "minimax/MiniMax-M3" in ids:
        assert ids["minimax/MiniMax-M3"] == 2, (
            f"empty env should fall back to 2, got {ids['minimax/MiniMax-M3']}"
        )


def test_negative_weight_disables_channel(reloaded_config):
    """负数 → 摘除（防止误设 -1 投票反向）。"""
    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "-3")
    import config
    importlib.reload(config)
    ids = [m["id"] for m in config.AI_MODEL_ROSTER]
    assert "minimax/MiniMax-M3" not in ids, "negative weight should disable channel"
