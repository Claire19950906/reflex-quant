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


@pytest.fixture
def _no_dotenv(monkeypatch):
    """reload config 时**不**再加载 .env —— 让测试看到「代码默认值」。

    v322 修复：config.py 在 import 时执行 load_dotenv(.env)，把 .env 里的
    MINIMAX2_WEIGHT=1 灌进 os.environ。原测试 delenv 之后再 reload，
    load_dotenv 又把 .env 的值灌回来 → "未设 env 时默认 #2=4" 永远断言不到
    （实测 got 1）。这不是代码 bug，是测试**依赖了生产 .env 的具体取值**。

    这里把 config.load_dotenv 打成 no-op，测试才能真正验证「代码里的默认值」。
    """
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    yield


def test_default_weights(reloaded_config, _no_dotenv):
    """未设 env 时默认主=2, #2=4（**代码默认值**，与 .env 无关）。"""
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
    """MINIMAX_MAIN_WEIGHT=0 → 主通道从 roster 完全摘除；#2 通道不受影响。

    v321: 原断言硬要求 "minimax2/MiniMax-M3" 一定在 roster 里，
    但 #2 是否入池取决于环境变量 MINIMAX2_WEIGHT —— 生产 .env 里是 0
    （账户2 的配额已专供门卫，见 config.py 的 minimax-gate 段），
    于是这条断言变成了"看环境"的必失败项。
    改成**相对基线**比较：关掉主通道不应改变 #2 通道的在场状态。
    这样无论环境里 #2 是开着还是关着，真正的不变量都被验证到。
    """
    import config
    importlib.reload(config)
    baseline_ids = {m["id"] for m in config.AI_MODEL_ROSTER}

    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "0")
    importlib.reload(config)
    ids = {m["id"] for m in config.AI_MODEL_ROSTER}

    assert "minimax/MiniMax-M3" not in ids, "main channel should be removed when weight=0"
    # #2 通道的在场状态不受主通道权重影响
    assert (("minimax2/MiniMax-M3" in ids)
            == ("minimax2/MiniMax-M3" in baseline_ids)), \
        "minimax2 channel presence must not depend on MINIMAX_MAIN_WEIGHT"


def test_main2_weight_zero_disables_channel(reloaded_config):
    """MINIMAX2_WEIGHT=0 → #2 通道从 roster 摘除。"""
    reloaded_config.setenv("MINIMAX2_WEIGHT", "0")
    reloaded_config.delenv("MINIMAX_MAIN_WEIGHT", raising=False)
    import config
    importlib.reload(config)
    ids = [m["id"] for m in config.AI_MODEL_ROSTER]
    assert "minimax2/MiniMax-M3" not in ids, "#2 channel should be removed when weight=0"


def test_both_zero_yields_empty_minimax_pool(reloaded_config):
    """极端:所有 minimax 通道权重都为 0 → minimax 系列在 roster 里都被摘除。

    v310: v314 P0 新增了**两个同行评审通道** (minimax-peer / minimax-peer2),
    各自有独立权重 MINIMAX_PEER_WEIGHT / MINIMAX_PEER2_WEIGHT (默认 1)。
    只把主通道 (#main / #2) 置 0 时, 评审通道仍在 roster 里 → 断言不再成立。
    这里把 4 个 minimax 通道的权重全部置 0, 才是"空 minimax 池"的真实语义。
    """
    reloaded_config.setenv("MINIMAX_MAIN_WEIGHT", "0")
    reloaded_config.setenv("MINIMAX2_WEIGHT", "0")
    reloaded_config.setenv("MINIMAX_PEER_WEIGHT", "0")
    reloaded_config.setenv("MINIMAX_PEER2_WEIGHT", "0")
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
