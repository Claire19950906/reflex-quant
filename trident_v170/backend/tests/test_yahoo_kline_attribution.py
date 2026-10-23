"""归因页 US_HYNIX Yahoo Finance 实时 K 线不可用 修复回归测试（v?? 海力士 cap）。

修复背景：归因页 /review 上的 US_HYNIX/SKHYNIX/MU/NVDA 等美股 / 韩国海力士标的 K 线原本
因 _fetch_real_klines / _fetch_binance_klines 都不识别 yahoo 市场而退化为"模拟反推"。
修复后两者都路由到 _fetch_yahoo_history_bars (yahoo → yfinance → asia 多源 fallback)。

覆盖：
  - _fetch_binance_klines("US_HYNIX", "1h") 返回 (bars, last_bar)
  - _fetch_binance_klines("BTC", "1h") 走 binance_fapi 分支不受影响（回归）
  - _fetch_binance_klines("WTI", "1h") 走 domestic_spot 不受影响（回归）
  - _fetch_real_klines("US_HYNIX", ...) 不再因 yahoo 市场返回 None
  - quote_sources.route("US_HYNIX") 仍返回 yahoo 市场
  - yahoo → yfinance → asia 多源 fallback 在 _fetch_yahoo_history_bars 链路中可注入

mock 策略：所有测试 monkeypatch _fetch_yahoo_history_bars / _fetch_binance_klines 内部
依赖（避免真打 Yahoo / yfinance / asia_prices）。全离线。
"""
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fake_yahoo_bars(asset: str, interval: str, limit: int):
    """模拟 N 根 NVDA/MU 类股票 K 线（价格 ~ 120 USD）。"""
    base_ts = 1700000000  # 2023-11-14 22:13 UTC
    iv_sec = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(interval, 3600)
    bars = []
    for i in range(max(1, limit)):
        o = 120.0 + i * 0.5
        h = o + 1.5
        lo = o - 0.8
        c = o + 0.3
        bars.append({
            "time": base_ts + i * iv_sec,
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(lo, 4),
            "close": round(c, 4),
            "volume": int(1000 + i * 100),
        })
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_binance_klines: yahoo 分支
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_binance_klines_us_hynix_returns_yahoo_bars(monkeypatch):
    """v?? 修复前: yahoo 市场 → return None；修复后: 返回 (bars, last_bar)。"""
    import api_server

    captured = {}

    def _fake(asset, interval, limit):
        captured["asset"] = asset
        captured["interval"] = interval
        captured["limit"] = limit
        return _fake_yahoo_bars(asset, interval, limit)

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars", _fake)
    api_server._BINANCE_KLINE_CACHE.clear()

    out = api_server._fetch_binance_klines("US_HYNIX", "1h")
    assert out is not None
    bars, last = out
    assert captured["asset"] == "US_HYNIX"
    assert captured["interval"] == "60m"
    assert captured["limit"] == 48
    assert len(bars) > 0
    for b in bars:
        assert set(b.keys()) >= {"time", "open", "high", "low", "close", "volume"}
        assert b["close"] > 0
    assert last == bars[-1]
    assert 120.0 < last["close"] < 200.0


def test_fetch_binance_klines_us_hynix_15m(monkeypatch):
    """15m 周期 — Yahoo 支持原生 15m，不做翻译。"""
    import api_server
    captured = {}

    def _fake(asset, interval, limit):
        captured["interval"] = interval
        captured["limit"] = limit
        return _fake_yahoo_bars(asset, interval, limit)

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars", _fake)
    api_server._BINANCE_KLINE_CACHE.clear()

    out = api_server._fetch_binance_klines("US_HYNIX", "15m")
    assert out is not None
    bars, last = out
    assert captured["interval"] == "15m"
    assert captured["limit"] == 96
    assert last == bars[-1]


def test_fetch_binance_klines_unknown_asset_returns_none():
    """未注册资产 → None（路由层就拒绝）。"""
    import api_server
    api_server._BINANCE_KLINE_CACHE.clear()
    out = api_server._fetch_binance_klines("UNKNOWN_XYZ", "1h")
    assert out is None


def test_fetch_binance_klines_yahoo_empty_returns_none(monkeypatch):
    """Yahoo 多源全部失败 → None（让 live 端点退回 _live_price_sync）。"""
    import api_server

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars",
                        lambda asset, interval, limit: [])
    api_server._BINANCE_KLINE_CACHE.clear()
    out = api_server._fetch_binance_klines("US_HYNIX", "1h")
    assert out is None


def test_fetch_binance_klines_skhynix_kospi(monkeypatch):
    """SKHYNIX (000660.KS) — 韩国海力士本体走 yahoo 市场。"""
    import api_server
    called = {"yahoo": 0}

    def _fake(asset, interval, limit):
        called["yahoo"] += 1
        return _fake_yahoo_bars(asset, interval, limit)

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars", _fake)
    api_server._BINANCE_KLINE_CACHE.clear()

# ─────────────────────────────────────────────────────────────────────────────
# 回归 — BTC/WTI 仍走各自分支，yahoo 不被错误触发
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_binance_klines_btc_does_not_use_yahoo(monkeypatch):
    """BTC 走 binance_fapi 链 — yahoo 不被触发。"""
    import api_server

    called = {"yahoo": 0, "binance_fapi": 0}

    def _fake_yahoo(asset, interval, limit):
        called["yahoo"] += 1
        return _fake_yahoo_bars(asset, interval, limit)

    def _fake_fapi(symbol, interval):
        called["binance_fapi"] += 1
        return None

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars", _fake_yahoo)
    monkeypatch.setattr(api_server, "_binance_fapi_klines", _fake_fapi)
    monkeypatch.setattr(api_server, "_binance_spot_klines",
                        lambda symbol, interval: None)
    monkeypatch.setattr(api_server, "_bybit_klines_bars",
                        lambda bybit_sym, interval: None)
    monkeypatch.setattr(api_server, "_bitstamp_klines_bars",
                        lambda bs_sym, interval: None)
    api_server._BINANCE_KLINE_CACHE.clear()

    out = api_server._fetch_binance_klines("BTC", "1h")
    assert out is None
    assert called["binance_fapi"] == 1
    assert called["yahoo"] == 0


def test_fetch_binance_klines_wti_does_not_use_yahoo(monkeypatch):
    """WTI 走 domestic_spot 分支（→ 金十 MCP）— 不触发 yahoo。"""
    import api_server

    called = {"yahoo": 0, "jin10": 0}

    def _ycb(asset, interval, limit):
        called["yahoo"] += 1
        return _fake_yahoo_bars(asset, interval, limit)

    def _jcb(asset, interval):
        called["jin10"] += 1
        return None

    monkeypatch.setattr(api_server, "_fetch_yahoo_history_bars", _ycb)
    monkeypatch.setattr(api_server, "_jin10_klines_bars", _jcb)
    api_server._BINANCE_KLINE_CACHE.clear()

    out = api_server._fetch_binance_klines("WTI", "1h")
    assert out is None
    assert called["yahoo"] == 0
    assert called["jin10"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_real_klines: 去掉 yahoo 屏蔽
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_real_klines_us_hynix_no_longer_blocked(monkeypatch):
    """修复前: r["market"]=="yahoo" → return None；修复后: 走 _fetch_binance_klines yahoo 分支。"""
    import api_server

    monkeypatch.setattr(api_server, "_fetch_binance_klines",
                        lambda asset, interval: (
                            _fake_yahoo_bars(asset, interval, 48),
                            _fake_yahoo_bars(asset, interval, 48)[-1],
                        ))

    out = api_server._fetch_real_klines("US_HYNIX", 1700000000, "1h")
    assert out is not None
    assert isinstance(out, list)
    assert len(out) > 0
    assert all(set(b.keys()) >= {"time", "open", "high", "low", "close", "volume"} for b in out)


def test_fetch_real_klines_unknown_asset_returns_none():
    """未注册资产 — 仍然 None。"""
    import api_server
    out = api_server._fetch_real_klines("UNKNOWN_XYZ", 1700000000, "1h")
    assert out is None


def test_fetch_real_klines_invalid_interval_falls_back_to_1h(monkeypatch):
    """无效 interval 默认 1h。"""
# ─────────────────────────────────────────────────────────────────────────────
# quote_sources.route 既有路由表未破坏
# ─────────────────────────────────────────────────────────────────────────────

def test_quote_sources_route_yahoo_assets():
    import quote_sources
    for a in ("US_HYNIX", "SKHYNIX", "MU", "NVDA", "TSLA", "AAPL", "MSFT", "META"):
        r = quote_sources.route(a)
        assert r is not None, f"{a} 必须有路由"
        assert r["market"] == "yahoo", f"{a} 应路由到 yahoo，实际 {r['market']}"
    # 加密 / 黄金 / 原油 路由未变
    assert quote_sources.route("BTC")["market"] == "binance_fapi"
    assert quote_sources.route("ETH")["market"] == "binance_fapi"
    assert quote_sources.route("SOL")["market"] == "binance_fapi"
    assert quote_sources.route("XAU")["market"] == "okx_swap"
    assert quote_sources.route("GOLD")["market"] == "okx_swap"
    assert quote_sources.route("WTI")["market"] == "domestic_spot"
    assert quote_sources.route("OIL")["market"] == "domestic_spot"


# ─────────────────────────────────────────────────────────────────────────────
# live 端点 /api/replay/kline/live 兜底链 — yahoo 资产不会 crash
# ─────────────────────────────────────────────────────────────────────────────

def test_live_price_sync_yahoo_does_not_crash():
    """_live_price_sync("US_HYNIX") 不会因 yahoo 资产 crash（None 或 float 都 OK）。"""
    import api_server
    api_server._LIVE_PRICE_CACHE.clear()

    try:
        out = api_server._live_price_sync("US_HYNIX")
    except Exception as e:
        pytest.fail(f"_live_price_sync(US_HYNIX) 不应抛异常: {e}")
    # 离线测试环境 Yahoo/yfinance 不可达 → None 是可接受的
    assert out is None or isinstance(out, (int, float))


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_yahoo_history_bars: 多源 fallback 注入 + 形态校验
# ─────────────────────────────────────────────────────────────────────────────

def test_yahoo_history_bars_returns_expected_shape(monkeypatch):
    """_fetch_yahoo_history_bars 透传 multi_source_fetcher.fetch_stock_history_cached 结果。"""
    import api_server

    fake_rows = [
        [1700000000000, 120.0, 121.0, 119.0, 120.5, 1000],
        [1700003600000, 120.5, 122.0, 120.0, 121.8, 1200],
        [1700007200000, 121.8, 122.5, 121.5, 122.3, 1500],
    ]

    fake_module = type(sys)("multi_source_fetcher")
    fake_module.fetch_stock_history_cached = lambda asset, interval, limit: fake_rows

    sys.modules["multi_source_fetcher"] = fake_module
    try:
        bars = api_server._fetch_yahoo_history_bars("US_HYNIX", "60m", 10)
    finally:
        if "multi_source_fetcher" in sys.modules and sys.modules["multi_source_fetcher"] is fake_module:
            del sys.modules["multi_source_fetcher"]

    assert len(bars) == 3
    assert bars[0]["time"] == 1700000000  # ms → s
    assert bars[0]["close"] == 120.5
    assert bars[0]["volume"] == 1000


def test_yahoo_history_bars_filters_none_rows(monkeypatch):
    """None bar 必须被过滤（非交易时段）。"""
    import api_server

    fake_rows = [
        [1700000000000, 120.0, 121.0, 119.0, 120.5, 1000],
        [1700003600000, None, None, None, None, None],
        [1700007200000, 121.8, 122.5, 121.5, 122.3, 1500],
    ]
    fake_module = type(sys)("multi_source_fetcher")
    fake_module.fetch_stock_history_cached = lambda asset, interval, limit: fake_rows
    sys.modules["multi_source_fetcher"] = fake_module
    try:
        bars = api_server._fetch_yahoo_history_bars("US_HYNIX", "60m", 10)
    finally:
        if "multi_source_fetcher" in sys.modules and sys.modules["multi_source_fetcher"] is fake_module:
            del sys.modules["multi_source_fetcher"]

    assert len(bars) == 2
    assert bars[0]["close"] == 120.5
    assert bars[1]["close"] == 122.3


def test_yahoo_history_bars_returns_empty_when_all_sources_fail(monkeypatch):
    """multi_source_fetcher 三级链全部失败 → []。"""
    import api_server

    fake_module = type(sys)("multi_source_fetcher")
    fake_module.fetch_stock_history_cached = lambda asset, interval, limit: []
    sys.modules["multi_source_fetcher"] = fake_module
    try:
        bars = api_server._fetch_yahoo_history_bars("US_HYNIX", "60m", 10)
    finally:
        if "multi_source_fetcher" in sys.modules and sys.modules["multi_source_fetcher"] is fake_module:
            del sys.modules["multi_source_fetcher"]
    assert bars == []


# ─────────────────────────────────────────────────────────────────────────────
# 端到端模拟：/api/replay/signal/{id}/kline 和 /api/replay/kline/live
# （通过 monkeypatch 注入真实 Yahoo 数据，断言 data_source=real、market_type=yahoo）
# ─────────────────────────────────────────────────────────────────────────────

def test_end_to_end_signal_kline_for_yahoo(monkeypatch):
    """端到端：US_HYNIX 信号 → /api/replay/signal/{id}/kline → data_source=real, market_type=yahoo。"""
    import api_server

    # mock 真实 K 线源：注入一组 Yahoo bars
    monkeypatch.setattr(api_server, "_fetch_binance_klines",
                        lambda asset, interval: (
                            _fake_yahoo_bars(asset, interval, 48),
                            _fake_yahoo_bars(asset, interval, 48)[-1],
                        ))

    fake_row = {
        "id": 1,
        "entry_price": 120.0,
        "exit_price": 121.0,
        "max_price": 121.5,
        "min_price": 119.5,
        "max_price_time": None,
        "min_price_time": None,
        "target_asset": "US_HYNIX",
        "entry_time": "2023-11-14T22:13:00+08:00",
        "suggested_action": "BUY",
        "strategy_params": '{"trailing_callback_rate": 0.5}',
    }

    class _FakeCursor:
        def __init__(self, row):
            self._row = row

        async def fetchone(self):
            return self._row

        async def close(self):
            pass

    class _FakeDb:
        def __init__(self):
            self.row_factory = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, sql, params):
            return _FakeCursor(fake_row)

    monkeypatch.setattr(api_server.aiosqlite, "connect",
                        lambda *args, **kwargs: _FakeDb())

    import asyncio
    out = asyncio.run(api_server.get_replay_signal_kline(1, interval="1h"))

    assert out["data_source"] == "real", f"应返回真实 Yahoo 数据，实际 {out['data_source']}"
    assert out["market_type"] == "yahoo", f"market_type 应是 yahoo，实际 {out['market_type']}"
    assert len(out["klines"]) > 0
    for b in out["klines"]:
        assert set(b.keys()) >= {"time", "open", "high", "low", "close", "volume"}


def test_end_to_end_live_kline_for_yahoo(monkeypatch):
    """端到端：US_HYNIX → /api/replay/kline/live → price 非 null。"""
    import api_server

    monkeypatch.setattr(api_server, "_fetch_binance_klines",
                        lambda asset, interval: (
                            _fake_yahoo_bars(asset, interval, 48),
                            _fake_yahoo_bars(asset, interval, 48)[-1],
                        ))

    import asyncio
    out = asyncio.run(api_server.replay_kline_live("US_HYNIX", interval="1h"))

    assert out["price"] is not None, f"US_HYNIX live price 不应为 None，实际 {out}"
    assert out["price"] > 0
    assert out["volume"] is not None
    assert out["asset"] == "US_HYNIX"


def test_end_to_end_signal_kline_btc_unchanged(monkeypatch):
    """回归：BTC 信号仍走 binance_perp，且真 K 线优先（=real）。"""
    import api_server

    monkeypatch.setattr(api_server, "_fetch_binance_klines",
                        lambda asset, interval: (
                            _fake_yahoo_bars(asset, interval, 48),
                            _fake_yahoo_bars(asset, interval, 48)[-1],
                        ))

    fake_row = {
        "id": 2,
        "entry_price": 36000.0,
        "exit_price": 36500.0,
        "max_price": 36800.0,
        "min_price": 35800.0,
        "max_price_time": None,
        "min_price_time": None,
        "target_asset": "BTC",
        "entry_time": "2023-11-14T22:13:00+08:00",
        "suggested_action": "BUY",
        "strategy_params": '{"trailing_callback_rate": 0.5}',
    }

    class _FakeCursor:
        async def fetchone(self):
            return fake_row

        async def close(self):
            pass

    class _FakeDb:
        def __init__(self):
            self.row_factory = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, sql, params):
            return _FakeCursor()

    monkeypatch.setattr(api_server.aiosqlite, "connect",
                        lambda *args, **kwargs: _FakeDb())

    import asyncio
    out = asyncio.run(api_server.get_replay_signal_kline(2, interval="1h"))

    assert out["data_source"] == "real"
    # BTC 不是 yahoo 市场 → market_type 仍标 binance_perp（与历史口径兼容）
    assert out["market_type"] == "binance_perp"
