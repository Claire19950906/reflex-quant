# -*- coding: utf-8 -*-
"""数据源健康告警 (engine/data_source_alert.py) 单元测试。"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

# 测试环境必须在 import config 之前注入
_TEST_ENV = {
    "SOURCE_ALERT_ENABLED": "1",
    "SOURCE_ALERT_THRESHOLD": "3",
    "SOURCE_ALERT_INTERVAL": "60",
    "SOURCE_ALERT_SILENT_MIN": "10",
    "SOURCE_ALERT_HEARTBEAT_MIN": "0",
    "FEISHU_WEBHOOK_URL": "https://hooks.test.example/webhook",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import config  # noqa: E402
import source_status  # noqa: E402
from engine import data_source_alert as dsa  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_module_state():
    dsa.reset_state()
    yield
    dsa.reset_state()


@pytest.fixture
def fake_snapshot(monkeypatch):
    captured = {"calls": 0, "last": []}

    def _fake():
        captured["calls"] += 1
        return list(captured["last"])

    monkeypatch.setattr(source_status, "snapshot", _fake)
    monkeypatch.setattr(dsa, "source_status", source_status)
    return captured


@pytest.fixture
def fake_feishu(monkeypatch):
    sent = []

    def _fake(payload, url, timeout=10.0):
        sent.append({"payload": payload, "url": url, "timeout": timeout})
        return True

    monkeypatch.setattr(dsa, "_send_feishu_sync", _fake)
    # 同步执行回调,便于测试断言
    monkeypatch.setattr(dsa, "_fire_and_forget", lambda fn, *a, **k: fn(*a, **k))
    return sent


def _make_entry(source_id, *, status="online", last_result="ok",
                consecutive_failures=0, last_error="",
                category="行情", label=None, **extra):
    if label is None:
        label = source_id
    return {
        "source_id": source_id,
        "category": category,
        "label": label,
        "status": status,
        "last_result": last_result,
        "last_ok_ts": time.time() if last_result == "ok" else None,
        "last_fail_ts": time.time() if last_result != "ok" else None,
        "last_error": last_error,
        "consecutive_failures": consecutive_failures,
        "total_ok": 10 if last_result == "ok" else 0,
        "total_fail": 0 if last_result == "ok" else consecutive_failures,
        "last_latency_ms": 100.0,
        "detail": "",
        "secs_since_ok": 5.0 if last_result == "ok" else None,
        "secs_since_fail": 5.0 if last_result != "ok" else None,
        **extra,
    }


# ── _should_alert 判定 ────────────────────────────────────────────────────

class TestShouldAlert:

    def test_first_observation_no_flip(self):
        assert dsa._should_alert(
            "bls", now=time.time(), threshold=3,
            consecutive_failures=0, last_result="ok", status_now="online",
        ) is None

    def test_ok_to_error_is_flip(self):
        dsa._last_state["bls"] = "online"
        result = dsa._should_alert(
            "bls", now=time.time(), threshold=3,
            consecutive_failures=1, last_result="error", status_now="error",
        )
        assert result == "flip"

    def test_threshold_trigger(self):
        dsa._last_state["coingecko"] = "error"
        dsa._last_alert_ts["coingecko"] = 0.0
        result = dsa._should_alert(
            "coingecko", now=time.time(), threshold=3,
            consecutive_failures=3, last_result="error", status_now="error",
        )
        assert result == "threshold"

    def test_silent_period_blocks(self):
        dsa._last_state["bls"] = "online"
        dsa._last_alert_ts["bls"] = time.time() - 60
        result = dsa._should_alert(
            "bls", now=time.time(), threshold=3,
            consecutive_failures=1, last_result="error", status_now="error",
        )
        assert result is None

    def test_heartbeat_source_immediate_alert(self):
        dsa._last_state["api_server"] = "online"
        result = dsa._should_alert(
            "api_server", now=time.time(), threshold=3,
            consecutive_failures=1, last_result="error", status_now="error",
        )
        assert result == "flip"

    def test_heartbeat_source_after_recovery_no_duplicate(self):
        dsa._last_state["api_server"] = "error"
        dsa._last_alert_ts["api_server"] = time.time() - 60
        result = dsa._should_alert(
            "api_server", now=time.time(), threshold=3,
            consecutive_failures=5, last_result="error", status_now="error",
        )
        assert result is None

    def test_heartbeat_source_recovery_alert(self):
        dsa._last_state["api_server"] = "error"
        dsa._last_alert_ts["api_server"] = 0.0
        result = dsa._should_alert(
            "api_server", now=time.time(), threshold=3,
            consecutive_failures=0, last_result="ok", status_now="online",
        )
        assert result == "flip"


# ── _build_alert_card ────────────────────────────────────────────────────

class TestBuildAlertCard:

    def test_flip_to_error(self):
        entry = _make_entry("bls", status="error", last_result="error",
                            consecutive_failures=3, last_error="401 unauthorized")
        card = dsa._build_alert_card("bls", entry, "flip")
        assert card["msg_type"] == "interactive"
        assert "已宕机" in card["card"]["header"]["title"]["content"]
        assert card["card"]["header"]["template"] == "red"
        body = json.dumps(card, ensure_ascii=False)
        assert "401 unauthorized" in body
        assert "bls" in body

    def test_threshold_alert(self):
        entry = _make_entry("coingecko", status="error", last_result="error",
                            consecutive_failures=5)
        card = dsa._build_alert_card("coingecko", entry, "threshold")
        assert "持续失败" in card["card"]["header"]["title"]["content"]
        assert card["card"]["header"]["template"] == "orange"

    def test_flip_to_recovery(self):
        entry = _make_entry("bls", status="online", last_result="ok",
                            consecutive_failures=0)
        card = dsa._build_alert_card("bls", entry, "flip")
        assert "已恢复" in card["card"]["header"]["title"]["content"]
        assert card["card"]["header"]["template"] == "green"


# ── _build_heartbeat_card ───────────────────────────────────────────────

class TestBuildHeartbeatCard:

    def test_basic_stats(self):
        stats = {"online": 8, "error": 3, "stale": 1, "unknown": 0}
        card = dsa._build_heartbeat_card(stats, [], [])
        assert card["msg_type"] == "interactive"
        assert "心跳摘要" in card["card"]["header"]["title"]["content"]
        body = json.dumps(card, ensure_ascii=False)
        assert "**总计**: 12" in body
        assert "**在线**: 8" in body
        assert "**异常**: 3" in body

    def test_with_newly_failed_and_recovered(self):
        stats = {"online": 5, "error": 2, "stale": 0, "unknown": 0}
        card = dsa._build_heartbeat_card(stats, ["币安"], ["FRED", "CoinGecko"])
        body = json.dumps(card, ensure_ascii=False)
        assert "新异常" in body
        assert "FRED" in body
        assert "CoinGecko" in body
        assert "新恢复" in body
        assert "币安" in body


# ── _tick_once 全链路 ───────────────────────────────────────────────────

class TestTickOnce:

    def test_disabled_returns_skipped(self, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_ALERT_ENABLED", False)
        result = dsa._tick_once()
        assert result.get("skipped") == "disabled"

    def test_no_feishu_url_returns_skipped(self, monkeypatch):
        monkeypatch.setattr(config, "FEISHU_WEBHOOK_URL", "")
        result = dsa._tick_once()
        assert result.get("skipped") == "no-feishu-url"

    def test_first_observation_no_alerts(self, fake_snapshot, fake_feishu):
        """首次轮询,online 源不触发任何告警。"""
        fake_snapshot["last"] = [
            _make_entry("binance_fapi:BTCUSDT", status="online", last_result="ok"),
            _make_entry("fred", status="online", last_result="ok"),
        ]
        result = dsa._tick_once()
        assert result["alerts"] == 0
        assert result["heartbeat"] is False
        assert len(fake_feishu) == 0
        assert result["stats"]["online"] == 2
        assert result["stats"]["error"] == 0

    def test_error_flip_triggers_alert(self, fake_snapshot, fake_feishu):
        dsa._last_state["bls"] = "online"
        dsa._last_alert_ts["bls"] = 0.0
        fake_snapshot["last"] = [
            _make_entry("bls", status="error", last_result="error",
                        consecutive_failures=1, last_error="401"),
        ]
        result = dsa._tick_once()
        assert result["alerts"] == 1
        assert len(fake_feishu) == 1
        assert "已宕机" in fake_feishu[0]["payload"]["card"]["header"]["title"]["content"]

    def test_heartbeat_source_immediate(self, fake_snapshot, fake_feishu):
        dsa._last_state["api_server"] = "online"
        dsa._last_alert_ts["api_server"] = 0.0
        fake_snapshot["last"] = [
            _make_entry("api_server", status="error", last_result="error",
                        consecutive_failures=1, last_error="timeout"),
        ]
        result = dsa._tick_once()
        assert result["alerts"] == 1

    def test_silent_period_blocks_duplicate(self, fake_snapshot, fake_feishu):
        dsa._last_state["bls"] = "error"
        dsa._last_alert_ts["bls"] = time.time()
        fake_snapshot["last"] = [
            _make_entry("bls", status="error", last_result="error",
                        consecutive_failures=10, last_error="still broken"),
        ]
        result = dsa._tick_once()
        assert result["alerts"] == 0
        assert len(fake_feishu) == 0

    def test_threshold_alert(self, fake_snapshot, fake_feishu):
        dsa._last_state["coingecko"] = "error"
        dsa._last_alert_ts["coingecko"] = 0.0
        fake_snapshot["last"] = [
            _make_entry("coingecko", status="error", last_result="error",
                        consecutive_failures=5, last_error="timeout"),
        ]
        result = dsa._tick_once()
        assert result["alerts"] == 1
        assert "持续失败" in fake_feishu[0]["payload"]["card"]["header"]["title"]["content"]

    def test_heartbeat_sent_after_interval(self, fake_snapshot, fake_feishu, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_ALERT_HEARTBEAT_MIN", 1)
        dsa._last_heartbeat_ts = time.time() - 120
        fake_snapshot["last"] = [
            _make_entry("bls", status="online", last_result="ok"),
        ]
        result = dsa._tick_once()
        assert result["heartbeat"] is True
        heartbeat_msgs = [m for m in fake_feishu
                          if "心跳摘要" in m["payload"]["card"]["header"]["title"]["content"]]
        assert len(heartbeat_msgs) == 1


# ── data_source_watcher 异步主循环 ─────────────────────────────────────

class TestDataSourceWatcher:

    def test_starts_and_can_be_cancelled(self, monkeypatch):
        monkeypatch.setattr(config, "SOURCE_ALERT_INTERVAL", 30)

        async def _run():
            task = asyncio.create_task(dsa.data_source_watcher(asyncio.get_running_loop()))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True

        assert asyncio.run(_run()) is True


# ── config 字段 ──────────────────────────────────────────────────────────

class TestConfig:

    def test_source_alert_defaults(self):
        assert hasattr(config, "SOURCE_ALERT_ENABLED")
        assert hasattr(config, "SOURCE_ALERT_THRESHOLD")
        assert hasattr(config, "SOURCE_ALERT_INTERVAL")
        assert hasattr(config, "SOURCE_ALERT_SILENT_MIN")
        assert hasattr(config, "SOURCE_ALERT_HEARTBEAT_MIN")
        assert config.SOURCE_ALERT_THRESHOLD >= 1
        assert config.SOURCE_ALERT_INTERVAL >= 30
        assert config.SOURCE_ALERT_SILENT_MIN >= 1