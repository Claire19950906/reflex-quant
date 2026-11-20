"""macro_context 解析与渲染用例（外部数据 → 宏观结论的那一层）。

这个模块把十几个外部源（币安/OKX/Bybit、FRED、CFTC COT、SPDR、Deribit）拼成给 LLM 的宏观段。
它不报错的错法有两种，都在这一层：
- **解析失败当 0/当有值**：源挂了应该输出 `N/A (note)`，不能编一个数；
- **口径算错**：隐含利率 = 100 − ZQ 期货价，基差 = mark/index − 1 折 bp，阈值 12.5bp 决定 hike/cut。
这些错在界面上只表现为"今天宏观看起来还行"，然后影响当天要不要下单。

所有 fetch_* 都接受注入的 `get`，所以这里不发一个网络请求。
"""

import datetime

import pytest

import macro_context as mc


@pytest.fixture(autouse=True)
def _offline_ig(monkeypatch):
    """fetch_structure 末尾会拉 IG 客户情绪（要登录、会走真网络），单测里一律置空。

    不置空时这条会白等 9~38 秒（凭据缺失 → 连接超时），51 条用例能从 1 秒拖成一分钟。
    """
    from feeds import ig_feed, quote_sources

    monkeypatch.setattr(ig_feed, "fetch_ig_sentiment", lambda: [], raising=False)
    # 注意：fetch_structure 的 get 注入**没覆盖** OKX 那三连 ——
    # _okx_structure_rows 直接调 quote_sources.fetch_okx_*(inst_id)，不带 get。
    # 不打桩就会走真网络，单条用例白等 3~30 秒。
    monkeypatch.setattr(quote_sources, "fetch_okx_funding", lambda inst: 0.0001, raising=False)
    monkeypatch.setattr(quote_sources, "fetch_okx_ticker", lambda inst: {"last": 4200.0}, raising=False)
    monkeypatch.setattr(quote_sources, "fetch_okx_open_interest", lambda inst: 1234, raising=False)


def _layer(category, metric_key, value, status="ok", unit="", **payload):
    return {
        "category": category, "source": "binance_fapi", "metric_key": metric_key,
        "value": value, "unit": unit, "status": status, "payload": payload,
    }


# ── 基础清洗与行结构 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (1, 1.0), ("2.5", 2.5), (None, None), ("abc", None), (float("nan"), None), ([], None),
])
def test_num_never_returns_nan(raw, expect):
    assert mc._num(raw) == expect


def test_metric_marks_none_value_as_unavailable_but_keeps_explicit_status():
    row = mc._metric("fed", "nyfed", "effr", None, unit="%")
    assert row["status"] == "unavailable" and row["value"] is None
    # 显式传了非 ok 的 status（比如缺口）要保留，不能又被改成 unavailable 之外的别的
    gap = mc._metric("fed", "nyfed", "effr", None, status="stale")
    assert gap["status"] == "stale"


def test_metric_label_goes_into_payload():
    row = mc._metric("structure", "binance_fapi", "BTC.mark", 100.0, label="BTC")
    assert row["payload"]["label"] == "BTC"
    assert row["value"] == 100.0 and row["status"] == "ok"


def test_fail_carries_note_for_render():
    row = mc._fail("structure", "none", "WTI.mark", "WTI 无免费永续合约")
    assert row["status"] == "unavailable"
    assert row["payload"]["note"] == "WTI 无免费永续合约"


# ── CFTC COT ──────────────────────────────────────────────────────────────

def test_cot_url_quotes_names_and_orders_by_report_date():
    url = mc._cftc_cot_url("CRUDE OIL", "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE")
    assert "commodity_name=CRUDE%20OIL" in url
    assert "market_and_exchange_names=WTI-PHYSICAL" in url
    assert url.endswith("&$order=report_date_as_yyyy_mm_dd%20desc&$limit=1")
    # 不传 market 时不能拼出空参数
    assert "market_and_exchange_names" not in mc._cftc_cot_url("GOLD", None)


@pytest.mark.parametrize("rec,expect", [
    ({"noncomm_positions_long_all": "300", "noncomm_positions_short_all": "100"}, 3.0),
    ({"noncomm_positions_long_all": 0, "noncomm_positions_short_all": 100}, None),   # 多头为 0 → 不算
    ({"noncomm_positions_long_all": 100}, None),
    (None, None),
])
def test_cot_spec_ratio(rec, expect):
    assert mc._cftc_spec_ratio(rec) == expect


def test_cot_latest_reads_first_record_and_swallows_errors():
    ok = mc._cftc_latest("GOLD", None, get=lambda url, t: [{"report_date_as_yyyy_mm_dd": "2026-11-11"}])
    assert ok["report_date_as_yyyy_mm_dd"] == "2026-11-11"

    def _boom(url, t):
        raise RuntimeError("network down")

    assert mc._cftc_latest("GOLD", None, get=_boom) is None
    assert mc._cftc_latest("GOLD", None, get=lambda url, t: []) is None
    assert mc._cftc_latest("GOLD", None, get=lambda url, t: "not a list") is None


# ── 情绪层 ────────────────────────────────────────────────────────────────

def test_sentiment_parses_index_and_classification():
    rows = mc.fetch_sentiment(get=lambda url, t: {
        "data": [{"value": "17", "value_classification": "Extreme Fear"}]})
    assert rows[0]["value"] == pytest.approx(17.0)
    assert rows[0]["payload"]["classification"] == "Extreme Fear"
    assert rows[0]["status"] == "ok"


@pytest.mark.parametrize("payload", [{"data": []}, {}, None, [1, 2]])
def test_sentiment_empty_payload_is_unavailable_not_zero(payload):
    rows = mc.fetch_sentiment(get=lambda url, t: payload)
    assert rows[0]["value"] is None
    assert rows[0]["status"] == "unavailable"


def test_sentiment_exception_is_unavailable():
    def _boom(url, t):
        raise TimeoutError("t/o")

    rows = mc.fetch_sentiment(get=_boom)
    assert rows[0]["status"] == "unavailable"
    assert "TimeoutError" in rows[0]["payload"]["note"]


# ── 美联储预期：隐含利率 = 100 − ZQ 价 ────────────────────────────────────

def _fed_get(effr="4.33", zq_price="95.68"):
    def get(url, t):
        if "newyorkfed" in url:
            return {"refRates": [{"percentRate": effr, "effectiveDate": "2026-11-19", "type": "EFFR"}]}
        return {"chart": {"result": [{"meta": {"regularMarketPrice": zq_price}}]}}
    return get


def test_fed_computes_implied_rate_and_spread():
    rows = mc.fetch_fed(get=_fed_get())
    effr = next(r for r in rows if r["metric_key"] == "effr")
    implied = next(r for r in rows if r["metric_key"] == "implied_ff")
    spread = next(r for r in rows if r["metric_key"] == "next_move_bp")
    assert effr["value"] == pytest.approx(4.33)
    assert implied["value"] == pytest.approx(4.32)          # 100 − 95.68
    assert spread["value"] == pytest.approx(-1.0)           # (4.32 − 4.33) × 100 bp
    assert spread["payload"]["next_move"] == "hold"          # |−1| 未过 12.5bp


@pytest.mark.parametrize("zq,expect_move", [
    ("95.50", "hike"),   # 隐含 4.50 vs EFFR 4.33 → +17bp
    ("96.20", "cut"),    # 隐含 3.80 vs 4.33 → −53bp
    ("95.68", "hold"),   # −1bp
])
def test_fed_next_move_thresholds(zq, expect_move):
    rows = mc.fetch_fed(get=_fed_get(zq_price=zq))
    spread = next(r for r in rows if r["metric_key"] == "next_move_bp")
    assert spread["payload"]["next_move"] == expect_move


def test_fed_falls_back_to_last_valid_close():
    rows = mc.fetch_fed(get=lambda url, t: {"chart": {"result": [{
        "meta": {},
        "indicators": {"quote": [{"close": [None, None, 96.0]}]}}]}})
    implied = next(r for r in rows if r["metric_key"] == "implied_ff")
    assert implied["value"] == pytest.approx(4.0)


def test_fed_failure_rows_are_explicit():
    def _boom(url, t):
        raise ValueError("bad")

    rows = mc.fetch_fed(get=_boom)
    keys = {r["metric_key"] for r in rows}
    assert {"effr", "implied_ff", "next_move_bp"} <= keys
    assert all(r["status"] == "unavailable" for r in rows)


# ── 盘面结构：币安永续 ────────────────────────────────────────────────────

def test_structure_computes_funding_pct_and_basis_bps():
    def get(url, t):
        if "premiumIndex" in url:
            return {"markPrice": "101.0", "indexPrice": "100.0", "lastFundingRate": "0.0001"}
        if "openInterest" in url:
            return {"openInterest": "12345"}
        if "globalLongShortAccountRatio" in url or "topLongShortPositionRatio" in url:
            return [{"longShortRatio": "1.8", "longAccount": "0.64", "shortAccount": "0.36"}]
        return {}

    rows = mc.fetch_structure(get=get)
    mark = next(r for r in rows if r["metric_key"] == "BTC.mark")
    funding = next(r for r in rows if r["metric_key"] == "BTC.funding")
    basis = next(r for r in rows if r["metric_key"] == "BTC.basis_bps")
    oi = next(r for r in rows if r["metric_key"] == "BTC.oi")
    ls = next(r for r in rows if r["metric_key"] == "BTC.ls_ratio")
    assert mark["value"] == pytest.approx(101.0)
    assert funding["value"] == pytest.approx(0.01)          # 0.0001 → 0.01%
    assert basis["value"] == pytest.approx(100.0)           # 1% → 100bp
    assert oi["value"] == pytest.approx(12345)
    assert ls["value"] == pytest.approx(1.8)
    assert ls["payload"]["longAccount"] == pytest.approx(0.64)


def test_structure_basis_is_na_when_index_missing():
    def get(url, t):
        if "premiumIndex" in url:
            return {"markPrice": "101.0", "lastFundingRate": "0.0001"}
        return []

    rows = mc.fetch_structure(get=get)
    basis = next(r for r in rows if r["metric_key"] == "BTC.basis_bps")
    assert basis["value"] is None and basis["status"] == "unavailable"


def test_structure_wti_has_no_perpetual_so_mark_is_a_gap():
    rows = mc.fetch_structure(get=lambda url, t: {})
    wti_mark = next(r for r in rows if r["metric_key"] == "WTI.mark")
    assert wti_mark["status"] == "unavailable"
    assert "无免费永续合约" in wti_mark["payload"]["note"]


# ── 展平与报告期 ──────────────────────────────────────────────────────────

def test_flatten_orders_by_category_not_input_dict_order():
    layers = {"fed": [_layer("fed", "effr", 4.3)], "structure": [_layer("structure", "BTC.mark", 100)]}
    rows = mc.flatten(layers)
    assert [r["metric_key"] for r in rows] == ["BTC.mark", "effr"]
    assert mc.flatten({}) == []
    assert mc.flatten({"unknown_layer": [_layer("x", "y", 1)]}) == []


@pytest.mark.parametrize("payload,expect", [
    ({"report_date": "2026-11-11"}, "2026-11-11"),
    ({"observation_period": "2026-M07"}, "2026-M07"),
    ({"as_of": " 2026-10-01 "}, "2026-10-01"),
    ({}, ""),
])
def test_report_date_of(payload, expect):
    assert mc.report_date_of({"payload": payload}) == expect


@pytest.mark.parametrize("raw,expect", [
    ("2026-M07", "2026-07"),      # BLS 的月份口径要归一，否则排序/比较全错
    ("2026-M12", "2026-12"),
    ("2026-11-11", "2026-11-11"),
    ("", ""),
    ("M07", "M07"),
])
def test_norm_as_of(raw, expect):
    assert mc._norm_as_of(raw) == expect


def test_as_of_suffix_flags_future_report_dates():
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    item = {"payload": {"report_date": future}}
    out = mc._as_of_suffix(item)
    assert "⚠未来" in out                      # 未发布的数据不能喂给 AI 当已知
    assert mc._as_of_suffix({"payload": {"report_date": "2020-01-02"}}) == "@2020-01-02"
    assert mc._as_of_suffix({}) == ""


def test_src_label_maps_and_falls_back():
    assert mc._src_label({"source": "binance_fapi"}) == "Binance"
    assert mc._src_label({"source": "某个内部源"}) == "某个内部源"
    assert mc._src_label({}) == ""


# ── 渲染 ──────────────────────────────────────────────────────────────────

def test_fmt_unavailable_shows_note_never_a_number():
    row = {"metric_key": "BTC.funding", "value": None, "status": "unavailable",
           "payload": {"note": "HTTPError"}}
    assert mc._fmt(row) == "BTC.funding=N/A (HTTPError)"
    row2 = {"metric_key": "BTC.funding", "value": None, "status": "unavailable", "payload": {}}
    assert mc._fmt(row2) == "BTC.funding=N/A (unavailable)"


def test_fmt_cvd_sign_labels_buy_and_sell_side():
    pos = {"metric_key": "BTC.cvd", "value": 1.5, "status": "ok", "payload": {"label": "BTC"}}
    neg = {"metric_key": "BTC.cvd", "value": -1.5, "status": "ok", "payload": {"label": "BTC"}}
    assert "净买盘" in mc._fmt(pos) and "+1.5%" in mc._fmt(pos)
    assert "净卖盘" in mc._fmt(neg)


def test_fmt_cot_net_position_direction():
    net_long = {"metric_key": "GOLD.cot_noncomm_net", "value": 12000, "status": "ok", "payload": {}}
    net_short = {"metric_key": "GOLD.cot_noncomm_net", "value": -3000, "status": "ok", "payload": {}}
    assert "净多" in mc._fmt(net_long) and "+12,000张" in mc._fmt(net_long)
    assert "净空" in mc._fmt(net_short)


@pytest.mark.parametrize("value,expect", [
    (2.5e12, "$2.50T"), (-1.2e9, "$-1.2B"), (3.4e6, "$3.4M"), (999, "$999"),
])
def test_flow_usd_scales(value, expect):
    assert mc._flow_usd(value) == expect


def test_flow_overview_says_na_instead_of_guessing():
    out = mc._flow_overview({})
    assert "美股杠杆/现货：N/A" in out
    assert "加密杠杆/现货：N/A" in out
    assert "BTC现货ETF净流入：N/A" in out
    assert "加密内部轮动：N/A" in out


def test_flow_overview_computes_cross_layer_ratios():
    layers = {
        "fred": [
            _layer("fred", "margin_debt", 1_000_000.0, unit="M USD"),
            _layer("fred", "us_equity_mcap", 50_000_000.0, unit="M USD"),
        ],
        "flow": [
            _layer("flow", "BTC.oi_usd_bybit", 20e9),
            _layer("flow", "crypto.total_mcap", 2e12),
            _layer("flow", "crypto.btc_dominance", 55.0),
            _layer("flow", "crypto.eth_dominance", 12.0),
        ],
    }
    out = mc._flow_overview(layers)
    assert "= 2.00%" in out                                  # 1e6*1e6 / 5e7*1e6
    assert "= 1.000%" in out                                 # 20e9 / 2e12
    assert "山寨+稳定币 33.00%" in out
