# -*- coding: utf-8 -*-
"""config 的基础口径测试：时间、开关、模型选择、自适应阈值。

config 是全系统唯一的时间口径入口。这里错 8 小时，下游所有"新鲜度/年龄/是否过期"
判断都会错，而且每处看起来都正常 —— 所以边界必须在这里钉死。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import config  # noqa: E402


# ── 时间口径 ──────────────────────────────────────────────────────────────

def test_naive_time_is_treated_as_beijing_and_z_is_converted():
    # 不带时区 = 已是北京时间，原样保留
    assert config.fmt_bj("2026-11-21 09:30:00") == "2026-11-21 09:30:00"
    # 带 Z 的 UTC 串要换算成北京（+8）
    assert config.fmt_bj("2026-11-21T01:30:00Z") == "2026-11-21 09:30:00"
    assert config.parse_time_to_bj("not-a-time") is None
    assert config.fmt_bj(None) == ""
    dt = config.parse_time_to_bj("2026-11-21 09:30:00")
    assert dt is not None and dt.utcoffset().total_seconds() == 8 * 3600


# ── env 开关 ──────────────────────────────────────────────────────────────

def test_env_bool_accepts_only_explicit_truthy(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config._env_bool("X_FLAG", False) is False
    assert config._env_bool("X_FLAG", True) is True
    for truthy in ("1", "true", " TRUE ", "yes", "on"):
        monkeypatch.setenv("X_FLAG", truthy)
        assert config._env_bool("X_FLAG") is True
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("X_FLAG", falsy)
        assert config._env_bool("X_FLAG", True) is False


# ── 模型标签与持有期分档 ──────────────────────────────────────────────────

def test_model_label_routes_by_prefix():
    assert config._model_label("DeepSeek-V3") == "DeepSeek-V3 (Aiping)"
    assert config._model_label("deepseek/chat") == "deepseek/chat (OpenRouter)"
    assert config._model_label("MiniMax-M3") == "MiniMax-M3 (nassaapi)"


def test_parse_horizon_tiers_ignores_garbage_and_floors_at_one():
    assert config._parse_horizon_tiers("high:60,medium:40,low:20") == {
        "high": 60, "medium": 40, "low": 20}
    assert config._parse_horizon_tiers(" HIGH : 60 ,bad,low:0,x:y") == {
        "high": 60, "low": 1}
    assert config._parse_horizon_tiers("") == {}
    assert config._parse_horizon_tiers(None) == {}


# ── 自适应阈值：数据越多越严格 ────────────────────────────────────────────

def test_adaptive_decay_and_protect_direction():
    assert config.adaptive_decay_factor(0) == (0.9, 0.95)     # 稀疏：宽容
    assert config.adaptive_decay_factor(20) == (0.7, 0.85)    # 充足：严格
    assert config.adaptive_decay_factor("bad") == (0.9, 0.95)
    assert config.adaptive_protect_version(0) == 3
    assert config.adaptive_protect_version(20) == 10
    assert config.adaptive_protect_version(50) >= config.adaptive_protect_version(20)


def test_adaptive_min_samples_flags_confidence():
    assert config.adaptive_min_samples(10) == (5, "low")
    assert config.adaptive_min_samples(50) == (10, "normal")
    assert config.adaptive_min_samples("bad") == (5, "low")


# ── key 脱敏 ──────────────────────────────────────────────────────────────

def test_coinglass_key_masked_never_leaks_full_key(monkeypatch):
    monkeypatch.setattr(config, "COINGLASS_API_KEY", "")
    assert config.coinglass_key_masked() == "<not_set>"
    monkeypatch.setattr(config, "COINGLASS_API_KEY", "cg_abcdef1234567890")
    masked = config.coinglass_key_masked()
    assert masked == "cg_abcde***"
    assert "1234567890" not in masked


# ── 模型池选择 ────────────────────────────────────────────────────────────

def _roster(monkeypatch):
    roster = [
        {"id": "A", "purpose": ["main"], "weight": 2, "api_base": "https://aiping.cn"},
        {"id": "B", "purpose": ["main"], "weight": 4, "api_base": "https://x"},
        {"id": "C", "purpose": ["reflect"], "weight": 1, "key_alias": "minimax-reflect"},
    ]
    monkeypatch.setattr(config, "AI_MODEL_ROSTER", roster)
    return roster


def test_pick_model_prefers_weight_then_explicit_id(monkeypatch):
    _roster(monkeypatch)
    top = config.pick_model_for_purpose("main")
    assert top["id"] == "B"                       # weight 最高的在前
    forced = config.pick_model_for_purpose("main", default_id="A")
    assert forced["id"] == "A"
    assert forced["json_mode"] is True            # aiping.cn 走 json 模式
    # purpose 不存在 → 回退主池
    assert config.get_pool_for_purpose("unknown")[0]["id"] == "B"
    assert config.key_alias_for_purpose("reflect") == "minimax-reflect"


def test_pick_model_raises_when_no_pool(monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL_ROSTER", [])
    try:
        config.pick_model_for_purpose("main")
    except RuntimeError as exc:
        assert "No AI model available" in str(exc)
    else:
        raise AssertionError("空池必须抛 RuntimeError，不能返回 None 让上层 AttributeError")
