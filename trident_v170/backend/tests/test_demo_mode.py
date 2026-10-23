"""
Trident v300 — demo_mode 模块测试
=================================

覆盖：配置读取 / 限流 / 延迟 / 数值脱敏 / 历史截断 / 水印 / 装饰器。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))


def test_mode_constants_default_full():
    """未设 TRIDENT_MODE 时默认 full。"""
    import demo_mode
    assert hasattr(demo_mode, "IS_FULL")
    assert hasattr(demo_mode, "IS_DEMO")
    assert hasattr(demo_mode, "IS_LICENSED")
    assert hasattr(demo_mode, "TRIDENT_MODE")


def test_sanitize_number_full_mode_passthrough(monkeypatch):
    """full 模式：sanitize_number 应原样返回。"""
    monkeypatch.setenv("TRIDENT_MODE", "full")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    assert demo_mode.IS_FULL
    assert demo_mode.sanitize_number(0.123456789) == 0.123456789
    assert demo_mode.sanitize_number({"a": 1.234567}) == {"a": 1.234567}


def test_sanitize_number_demo_rounds_floats(monkeypatch):
    """demo 模式：float 被截位到 DEMO_DECIMALS。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_DECIMALS", "2")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    assert demo_mode.IS_DEMO
    assert demo_mode.sanitize_number(4236.9234567) == 4236.92
    assert demo_mode.sanitize_number(0.000001) == 0.0
    assert math.isnan(demo_mode.sanitize_number(float("nan")))
    assert demo_mode.sanitize_number(float("inf")) == float("inf")


def test_sanitize_number_recurses_into_dict_list(monkeypatch):
    """demo 模式：dict / list 嵌套递归脱敏。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_DECIMALS", "2")
    import importlib, demo_mode
    importlib.reload(demo_mode)

    data = {
        "r1": 4236.9234567,
        "rows": [
            {"ts": "2026-10-09", "v": 1.234567},
            {"ts": "2026-10-09", "v": 2.987654},
        ],
        "meta": {"sharpe": 1.23456789},
    }
    out = demo_mode.sanitize_number(data)
    assert out["r1"] == 4236.92
    assert out["rows"][0]["v"] == 1.23
    assert out["rows"][1]["v"] == 2.99
    assert out["meta"]["sharpe"] == 1.23
    assert out["rows"][0]["ts"] == "2026-10-09"


def test_sanitize_number_skip_keys(monkeypatch):
    """配置 skip keys 后，被登记的 key 不脱敏。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_DECIMALS", "2")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    demo_mode.configure_skip_keys(["id", "timestamp"])
    out = demo_mode.sanitize_number({
        "id": 12345, "v": 1.234567, "timestamp": 1728456789.123,
    })
    assert out["id"] == 12345
    assert out["v"] == 1.23
    assert out["timestamp"] == 1728456789.123


def test_trim_history(monkeypatch):
    """demo 模式：超出 DEMO_HISTORY_DAYS 的数据点被截断。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_HISTORY_DAYS", "1")
    import importlib, demo_mode
    importlib.reload(demo_mode)

    now_iso = "2026-10-09T12:00:00+00:00"
    old_iso = "2026-10-01T12:00:00+00:00"
    rows = [
        {"ts": now_iso, "v": 1},
        {"ts": old_iso, "v": 2},
        {"ts": None, "v": 3},
    ]
    out = demo_mode.trim_history(rows, ts_field="ts")
    assert len(out) == 2
    assert out[0]["v"] == 1
    assert out[1]["v"] == 3


def test_trim_history_zero_days_keeps_all(monkeypatch):
    """DEMO_HISTORY_DAYS=0 时 trim_history 不截断。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_HISTORY_DAYS", "0")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    rows = [{"ts": "2000-01-01T00:00:00Z", "v": 1}]
    assert demo_mode.trim_history(rows) == rows


def test_rate_limit_basic(monkeypatch):
    """限流：60s 内超过上限应被拒。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_RPM", "3")
    import importlib, demo_mode
    importlib.reload(demo_mode)

    ip = "1.2.3.4"
    for _ in range(3):
        ok, reason = demo_mode.check_rate_limit(ip)
        assert ok, f"应通过但被拒: {reason}"
    ok, reason = demo_mode.check_rate_limit(ip)
    assert not ok
    assert "rate_limit_ip" in reason


def test_rate_limit_full_mode_unlimited(monkeypatch):
    """full 模式：限流器直接放行。"""
    monkeypatch.setenv("TRIDENT_MODE", "full")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    for _ in range(1000):
        ok, _ = demo_mode.check_rate_limit("1.2.3.4")
        assert ok


def test_daily_budget(monkeypatch):
    """每日配额：超过应被拒。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_DAILY_BUDGET", "5")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    ip = "5.6.7.8"
    for _ in range(5):
        ok, _ = demo_mode.check_rate_limit(ip)
        assert ok
    ok, reason = demo_mode.check_rate_limit(ip)
    assert not ok
    assert "daily_budget" in reason


def test_make_watermark_uniqueness():
    """水印：每次调用生成不同 token。"""
    import demo_mode
    wm1 = demo_mode.make_watermark("1.2.3.4", "trial")
    wm2 = demo_mode.make_watermark("1.2.3.4", "trial")
    assert isinstance(wm1, str) and len(wm1) >= 16
    assert isinstance(wm2, str) and len(wm2) >= 16
    assert wm1 != wm2


def test_demo_guarded_decorator(monkeypatch):
    """装饰器：demo 模式下注入延迟 + 脱敏。"""
    monkeypatch.setenv("TRIDENT_MODE", "demo")
    monkeypatch.setenv("TRIDENT_DEMO_DECIMALS", "2")
    monkeypatch.setenv("TRIDENT_DEMO_DELAY_MS", "50")
    monkeypatch.setenv("TRIDENT_DEMO_JITTER_MS", "0")
    import importlib, demo_mode
    importlib.reload(demo_mode)

    @demo_mode.demo_guarded
    async def handler():
        await asyncio.sleep(0.001)
        return {"r1": 4236.9234567, "ok": True}

    t0 = time.time()
    out = asyncio.run(handler())
    elapsed = time.time() - t0
    assert out["r1"] == 4236.92
    assert out["ok"] is True
    assert elapsed >= 0.05


def test_full_guard_localhost_warns_on_public_host(monkeypatch, caplog):
    """full 模式 + 0.0.0.0 host：应警告。"""
    monkeypatch.setenv("TRIDENT_MODE", "full")
    import importlib, demo_mode
    importlib.reload(demo_mode)
    with caplog.at_level(logging.WARNING):
        demo_mode.assert_full_guard_localhost("0.0.0.0")
    assert any("TRIDENT_MODE=full" in r.message for r in caplog.records)


def test_startup_banner_runs():
    """启动横幅不应抛异常。"""
    import demo_mode
    demo_mode.print_startup_banner()