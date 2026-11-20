# -*- coding: utf-8 -*-
"""环境闸门测试：MA200 站位 + SELL 侧 ADX 确认只依赖信号当根及之前的数据。"""
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import tech_signal_bridge as tsb


def _conn(series, asset="T"):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    c.executemany("INSERT INTO ohlcv_daily VALUES (?, ?, ?)",
                  [(asset, i, float(v)) for i, v in enumerate(series)])
    return c


def setup_function():
    tsb._MA200_CACHE.clear()


def test_ma200_above_below():
    up = _conn(list(range(1, 201)), "UP")
    down = _conn(list(range(200, 0, -1)), "DOWN")
    assert tsb.ma200_state(up, "UP", 199) == "above"
    assert tsb.ma200_state(down, "DOWN", 199) == "below"


def test_short_history_is_unknown():
    c = _conn(list(range(1, 200)))     # 只有 199 根
    assert tsb.ma200_state(c, "T", 198) is None


def test_buy_requires_above_ma200():
    up = _conn(list(range(1, 201)), "UP")
    down = _conn(list(range(200, 0, -1)), "DOWN")
    allow = {"regime": {"buy_require_ma200_above": True}}
    assert tsb.regime_ok(up, "UP", 199, "BUY", [], allow) is True
    assert tsb.regime_ok(down, "DOWN", 199, "BUY", [], allow) is False


def test_sell_requires_below_and_adx_bear():
    up = _conn(list(range(1, 201)), "UP")
    down = _conn(list(range(200, 0, -1)), "DOWN")
    allow = {"regime": {"sell_require_ma200_below_adx_bear": True}}
    items = [{"k": "kdj_overbought"}, {"k": "adx_strong_bear", "role": "context"}]
    assert tsb.regime_ok(down, "DOWN", 199, "SELL", items, allow) is True
    assert tsb.regime_ok(down, "DOWN", 199, "SELL", [{"k": "kdj_overbought"}], allow) is False
    assert tsb.regime_ok(up, "UP", 199, "SELL", items, allow) is False


def test_no_config_means_no_gate():
    down = _conn(list(range(200, 0, -1)), "DOWN")
    assert tsb.regime_ok(down, "DOWN", 199, "BUY", [], {}) is True
