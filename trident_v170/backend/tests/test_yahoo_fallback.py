"""Yahoo Finance K 线兜底层测试（v2026.10.05 新增）。

覆盖：
  - fetch_yahoo_candles 解析成功路径（含 None bar 过滤、limit 截尾）
  - fetch_yahoo_candles 在 Yahoo 不可达时返回 None（不合成扁平假 bar）
  - fetch_yahoo_candles ^VIX 等特殊 ticker 正确转义
  - yahoo_interval OKX→Yahoo interval 映射
  - YAHOO_SYMBOL_FOR_ASSET 覆盖 XAU/XAG/WTI/BTC/ETH/SOL/HG/DXY/VIX
  - fetch_asset_candles_with_yahoo_fallback 优先 OKX / 失败回退 Yahoo
  - 限速（_YAHOO_MIN_GAP_S）与熔断（_yahoo_cooldown_until）行为
  - market_snapshot._fetch_asset_klines okx_swap 分支改用兜底函数
  - engine/ai_worker._build_evidence_for_asset / _compute_market_style
    okx_swap 分支改用兜底函数（OKX 失败时仍能拿真实 K 线）
  - knowledge_base._fetch_yahoo_wti_klines 复用 quote_sources.fetch_yahoo_candles
"""
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import quote_sources  # noqa: E402


class _DummyStatus:
    def __init__(self):
        self.calls = []

    def report(self, sid, ok, **kw):
        self.calls.append((sid, ok, kw))


@pytest.fixture(autouse=True)
def _reset_yahoo_state():
    """每个测试前重置 Yahoo 限速/熔断/失败计数。"""
    saved = {
        "_yahoo_session": quote_sources._yahoo_session,
        "_yahoo_last_call_ts": quote_sources._yahoo_last_call_ts,
        "_yahoo_cooldown_until": quote_sources._yahoo_cooldown_until,
        "_yahoo_fail_streak": quote_sources._yahoo_fail_streak,
    }
    quote_sources._yahoo_session = None
    quote_sources._yahoo_last_call_ts = 0.0
    quote_sources._yahoo_cooldown_until = 0.0
    quote_sources._yahoo_fail_streak = 0
    yield
    for k, v in saved.items():
        setattr(quote_sources, k, v)


def _fake_yahoo_payload(raw_timestamps=None):
    """构造一个真实的 Yahoo v8 chart 响应（含一个 None close bar 模拟非交易时段）。"""
    return {
        "chart": {
            "result": [{
                "meta": {"regularMarketPrice": 4160.5, "currency": "USD"},
                "timestamp": raw_timestamps or [1700000000, 1700003600, 1700007200, 1700010800, 1700014400],
                "indicators": {
                    "quote": [{
                        "open":  [4150.0, 4155.0, None, 4168.0, 4172.0],
                        "high":  [4160.0, 4162.0, None, 4175.0, 4178.0],
                        "low":   [4148.0, 4152.0, None, 4165.0, 4170.0],
                        "close": [4158.0, 4159.0, None, 4173.0, 4176.0],
                        "volume":[1000,    1200, None,    1500,   1600],
                    }],
                    "adjclose": [{}],
                },
            }],
            "error": None,
        }
    }


def test_fetch_yahoo_candles_success_basic(monkeypatch):
    status = _DummyStatus()
    monkeypatch.setattr(quote_sources, "source_status", status)
    monkeypatch.setattr(quote_sources, "_yahoo_get_json",
                        lambda params: _fake_yahoo_payload())
    rows = quote_sources.fetch_yahoo_candles("GC=F", interval="60m",
                                             range_or_period="5d", limit=10)
    assert rows is not None
    assert len(rows) == 4
    r0 = rows[0]
    assert len(r0) == 6
    assert r0[0] == 1700000000 * 1000
    assert r0[4] == 4158.0
    for r in rows:
        assert r[4] is not None and r[4] > 0
    assert any(ok for sid, ok, _ in status.calls if sid.startswith("yahoo:"))


def test_fetch_yahoo_candles_limit_truncates_tail(monkeypatch):
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json",
                        lambda params: _fake_yahoo_payload())
    rows = quote_sources.fetch_yahoo_candles("GC=F", interval="60m",
                                             range_or_period="5d", limit=2)
    assert rows is not None
    assert len(rows) == 2
    assert rows[-1][0] == 1700014400 * 1000


def test_fetch_yahoo_candles_yahoo_unreachable(monkeypatch):
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json", lambda params: None)
    rows = quote_sources.fetch_yahoo_candles("GC=F", interval="60m", range_or_period="5d")
    assert rows is None


def test_fetch_yahoo_candles_empty_result(monkeypatch):
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json",
                        lambda params: {"chart": {"result": [], "error": None}})
    rows = quote_sources.fetch_yahoo_candles("GC=F", interval="60m", range_or_period="5d")
    assert rows is None


def test_fetch_yahoo_candles_caret_ticker_escaped(monkeypatch):
    captured_params = {}

    def _capture(params):
        captured_params.update(params)
        return _fake_yahoo_payload()

    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json", _capture)
    quote_sources.fetch_yahoo_candles("^VIX", interval="60m", range_or_period="5d", limit=3)
    assert captured_params["symbol"] == "%5EVIX"


def test_fetch_yahoo_candles_period_overrides_range(monkeypatch):
    captured = {}

    def _capture(params):
        captured.update(params)
        return _fake_yahoo_payload()

    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json", _capture)
    quote_sources.fetch_yahoo_candles(
        "GC=F", interval="60m", range_or_period="5d",
        period1=1700000000, period2=1700100000,
    )
    assert captured.get("period1") == "1700000000"
    assert captured.get("period2") == "1700100000"
    assert "range" not in captured


def test_fetch_yahoo_candles_all_none_returns_none(monkeypatch):
    payload = _fake_yahoo_payload()
    payload["chart"]["result"][0]["indicators"]["quote"][0] = {
        "open": [None] * 5, "high": [None] * 5, "low": [None] * 5,
        "close": [None] * 5, "volume": [None] * 5,
    }
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "_yahoo_get_json", lambda params: payload)
    rows = quote_sources.fetch_yahoo_candles("GC=F", interval="60m", range_or_period="5d")
    assert rows is None


def test_yahoo_interval_mapping():
    assert quote_sources.yahoo_interval("1m") == "1m"
    assert quote_sources.yahoo_interval("5m") == "5m"
    assert quote_sources.yahoo_interval("15m") == "15m"
    assert quote_sources.yahoo_interval("1H") == "60m"
    assert quote_sources.yahoo_interval("1h") == "60m"
    assert quote_sources.yahoo_interval("4h") == "60m"
    assert quote_sources.yahoo_interval("1D") == "1d"
    assert quote_sources.yahoo_interval("1d") == "1d"
    assert quote_sources.yahoo_interval("1W") == "1wk"
    assert quote_sources.yahoo_interval("unknown_bar") == "60m"


def test_yahoo_symbol_mapping_covers_main_assets():
    required = ["XAU", "GOLD", "XAG", "SILVER", "WTI", "OIL", "HG",
                "BTC", "ETH", "SOL", "DXY", "VIX"]
    for a in required:
        assert a in quote_sources.YAHOO_SYMBOL_FOR_ASSET, f"missing {a}"
        assert quote_sources.YAHOO_SYMBOL_FOR_ASSET[a]


def test_yahoo_symbol_mapping_xau_uses_gold_futures():
    assert quote_sources.YAHOO_SYMBOL_FOR_ASSET["XAU"] == "GC=F"
    assert quote_sources.YAHOO_SYMBOL_FOR_ASSET["XAG"] == "SI=F"
    assert quote_sources.YAHOO_SYMBOL_FOR_ASSET["WTI"] == "CL=F"


def test_with_fallback_okx_success_no_yahoo_call(monkeypatch):
    """OKX 成功时不走 Yahoo。"""
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    yahoo_called = []

    def _no_yahoo(*a, **k):
        yahoo_called.append((a, k))
        return None

    monkeypatch.setattr(quote_sources, "fetch_okx_candles",
                        lambda sym, bar, limit: [[1700000000000, 1, 2, 3, 4, 5]])
    monkeypatch.setattr(quote_sources, "fetch_yahoo_candles", _no_yahoo)
    rows = quote_sources.fetch_asset_candles_with_yahoo_fallback("XAU", "1H", 96)
    assert rows is not None and len(rows) == 1
    assert yahoo_called == []


def test_with_fallback_okx_fails_uses_yahoo(monkeypatch):
    """OKX 失败 → 自动降级 Yahoo → 拿真实 K 线。"""
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "fetch_okx_candles",
                        lambda sym, bar, limit: None)
    monkeypatch.setattr(quote_sources, "fetch_yahoo_candles",
                        lambda sym, **kw: [[1700000000000, 4150, 4160, 4148, 4158, 1000]])
    rows = quote_sources.fetch_asset_candles_with_yahoo_fallback("XAU", "1H", 96)
    assert rows is not None and len(rows) == 1
    assert rows[0][4] == 4158


def test_with_fallback_both_fail_returns_none(monkeypatch):
    """OKX + Yahoo 都失败 → 诚实返回 None（不合成扁平假 K 线）。"""
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "fetch_okx_candles",
                        lambda sym, bar, limit: None)
    monkeypatch.setattr(quote_sources, "fetch_yahoo_candles",
                        lambda sym, **kw: None)
    rows = quote_sources.fetch_asset_candles_with_yahoo_fallback("XAU", "1H", 96)
    assert rows is None


def test_with_fallback_no_yahoo_mapping_returns_none(monkeypatch):
    """WTI 没有 OKX 永续（route 返回的 symbol 为空）→ 早退 None。"""
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    monkeypatch.setattr(quote_sources, "fetch_okx_candles",
                        lambda sym, bar, limit: None)
    monkeypatch.setattr(quote_sources, "route",
                        lambda a: {"market": "okx_swap", "symbol": ""})
    rows = quote_sources.fetch_asset_candles_with_yahoo_fallback("WTI", "1H", 96)
    assert rows is None


def test_yahoo_throttle_respects_min_gap(monkeypatch):
    """_yahoo_throttle 应至少 sleep _YAHOO_MIN_GAP_S - gap 时间。"""
    # 模拟"刚刚调用过"：把 _yahoo_last_call_ts 设为当前时间 - 1s
    quote_sources._yahoo_last_call_ts = quote_sources.time.time() - 1.0
    t0 = quote_sources.time.time()
    quote_sources._yahoo_throttle()
    elapsed = quote_sources.time.time() - t0
    # 间隔阈值 = 3s，刚刚才过去 1s → 应至少 sleep 2s
    assert elapsed >= quote_sources._YAHOO_MIN_GAP_S - 1.0 - 0.1


def test_yahoo_breaker_via_get_json(monkeypatch):
    """_yahoo_get_json 内部维护的熔断：3 次失败后短路（独立验证逻辑见 test_yahoo_get_json_circuit_breaker）。
    这里验证 fetch_yahoo_candles 在 3 次失败后熔断打开 → 后续调用早退不再发 HTTP。"""
    quote_sources._yahoo_fail_streak = 0
    quote_sources._yahoo_cooldown_until = 0.0
    quote_sources._yahoo_last_call_ts = 0.0
    # 加速：把 sleep 设为 0
    monkeypatch.setattr(quote_sources.time, "sleep", lambda s: None)

    class _Resp:
        status_code = 429

        def json(self): return {}

    class _Sess:
        def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(quote_sources, "_yahoo", lambda: _Sess())
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())

    # 调 3 次 fetch_yahoo_candles（每次内部 _yahoo_get_json 一次）→ streak=3 → 熔断开
    for _ in range(3):
        quote_sources.fetch_yahoo_candles("GC=F", interval="60m", range_or_period="5d")
    # 熔断应已打开
    assert quote_sources._yahoo_cooldown_until > quote_sources.time.time()
    assert quote_sources._yahoo_fail_streak >= quote_sources._YAHOO_BREAKER_AFTER

    # 第 4 次：熔断中 → 早退，不再发 HTTP
    session_calls = [0]

    class _CountingSess(_Sess):
        def get(self, *a, **k):
            session_calls[0] += 1
            return _Resp()

    monkeypatch.setattr(quote_sources, "_yahoo", lambda: _CountingSess())
    r4 = quote_sources.fetch_yahoo_candles("GC=F", interval="60m", range_or_period="5d")
    assert r4 is None
    assert session_calls[0] == 0, "熔断中 _yahoo().get 不应被调用"


def test_yahoo_get_json_circuit_breaker(monkeypatch):
    """_yahoo_get_json 自身也维护 _yahoo_fail_streak / _yahoo_cooldown_until。"""
    quote_sources._yahoo_fail_streak = 0
    quote_sources._yahoo_cooldown_until = 0.0

    class _Resp:
        status_code = 429

        def json(self): return {}

    class _Sess:
        def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(quote_sources, "_yahoo", lambda: _Sess())
    monkeypatch.setattr(quote_sources.time, "sleep", lambda s: None)
    monkeypatch.setattr(quote_sources, "source_status", _DummyStatus())
    for _ in range(3):
        quote_sources._yahoo_get_json({"symbol": "GC=F"})
    assert quote_sources._yahoo_cooldown_until > 0
    assert quote_sources._yahoo_fail_streak >= quote_sources._YAHOO_BREAKER_AFTER


def test_okx_to_yahoo_interval_covers_main_bars():
    expected = {"1m", "3m", "5m", "15m", "30m", "1H", "1h", "4h", "1D", "1d", "1W", "1w"}
    for bar in expected:
        assert bar in quote_sources._OKX_TO_YAHOO_INTERVAL
        v = quote_sources._OKX_TO_YAHOO_INTERVAL[bar]
        assert v in {"1m", "5m", "15m", "30m", "60m", "1d", "1wk"}


def test_market_snapshot_okx_uses_yahoo_fallback(monkeypatch):
    """market_snapshot._fetch_asset_klines okx_swap 分支应调 fetch_asset_candles_with_yahoo_fallback。"""
    import market_snapshot
    called = []

    def _fake_fallback(asset_id, okx_bar, limit):
        called.append((asset_id, okx_bar, limit))
        return [[1700000000000, 1, 2, 3, 4, 5]]

    monkeypatch.setattr(market_snapshot.quote_sources, "fetch_asset_candles_with_yahoo_fallback",
                        _fake_fallback)
    monkeypatch.setattr(market_snapshot.quote_sources, "route",
                        lambda a: {"market": "okx_swap", "symbol": "XAU-USDT-SWAP"})
    rows = market_snapshot._fetch_asset_klines("XAU", "1h", 24)
    assert rows is not None
    assert len(called) == 1
    assert called[0][0] == "XAU"


def test_market_snapshot_binance_branch_unchanged(monkeypatch):
    """binance_fapi 分支不应走 Yahoo（仅 OKX 资产需要）。"""
    import market_snapshot
    yahoo_called = []

    def _fake_fallback(asset_id, okx_bar, limit):
        yahoo_called.append(asset_id)
        return None

    monkeypatch.setattr(market_snapshot.quote_sources, "fetch_asset_candles_with_yahoo_fallback",
                        _fake_fallback)
    monkeypatch.setattr(market_snapshot.quote_sources, "route",
                        lambda a: {"market": "binance_fapi", "symbol": "BTCUSDT"})
    monkeypatch.setattr(market_snapshot, "_fetch_fapi_klines_sync",
                        lambda s, iv, l: [[1700000000000, 1, 2, 3, 4, 5]])
    rows = market_snapshot._fetch_asset_klines("BTC", "1h", 24)
    assert rows is not None
    assert yahoo_called == [], "BTC 不应触发 Yahoo 兜底"


def test_ai_worker_build_evidence_uses_yahoo_fallback(monkeypatch):
    """_build_evidence_for_asset 在 okx_swap 分支应走 fetch_asset_candles_with_yahoo_fallback。"""
    import quote_sources as qs_real
    import engine.ai_worker as ai_worker
    called = []

    def _fake_fallback(asset_id, okx_bar, limit):
        called.append((asset_id, okx_bar, limit))
        return [[1700000000000, 4150, 4160, 4148, 4158, 1000]]

    # ai_worker 函数内部 `import quote_sources as _qs`，所以 patch 真正的 quote_sources 模块
    monkeypatch.setattr(qs_real, "route",
                        lambda a: {"market": "okx_swap", "symbol": "XAU-USDT-SWAP"})
    monkeypatch.setattr(qs_real, "fetch_asset_candles_with_yahoo_fallback",
                        _fake_fallback)
    text = ai_worker._build_evidence_for_asset("XAU")
    assert text
    assert len(called) == 2
    assert ("XAU", "1m", 2) in called
    assert ("XAU", "1H", 2) in called


def test_ai_worker_market_style_uses_yahoo_fallback(monkeypatch):
    """_compute_market_style okx_swap 分支应走 fetch_asset_candles_with_yahoo_fallback。"""
    import quote_sources as qs_real
    import engine.ai_worker as ai_worker
    called = []

    def _fake_fallback(asset_id, okx_bar, limit):
        called.append((asset_id, okx_bar, limit))
        rows = []
        for i in range(24):
            rows.append([1700000000000 + i * 3600 * 1000, 4150, 4160, 4148, 4158 + i, 1000])
        return rows

    monkeypatch.setattr(qs_real, "route",
                        lambda a: {"market": "okx_swap", "symbol": "XAU-USDT-SWAP"})
    monkeypatch.setattr(qs_real, "fetch_asset_candles_with_yahoo_fallback",
                        _fake_fallback)
    style = ai_worker._compute_market_style("XAU")
    assert style != "unknown|n/a|n/a"
    assert len(called) == 1
    assert called[0] == ("XAU", "1H", 24)


def test_knowledge_base_wti_uses_quote_sources_yahoo(monkeypatch):
    """knowledge_base._fetch_yahoo_wti_klines 应调 quote_sources.fetch_yahoo_candles。"""
    import knowledge_base as kb
    import quote_sources as qs_real
    called = []

    def _fake_yahoo(yahoo_sym, **kw):
        called.append((yahoo_sym, kw))
        return [[1700000000000, 80.0, 81.0, 79.0, 80.5, 1000]]

    monkeypatch.setattr(kb, "source_status", _DummyStatus())
    monkeypatch.setattr(qs_real, "fetch_yahoo_candles", _fake_yahoo)
    rows = kb._fetch_yahoo_wti_klines("1d", 5)
    assert rows is not None and len(rows) == 1
    assert called[0][0] == "CL=F"
