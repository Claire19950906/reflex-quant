"""ust_curve_treasury（财政部官方收益率曲线）单测。

2026-11-20: 覆盖率 0%。这模块的注释里全是踩过的坑，正好可以一条条钉成用例：

1. `_looks_like_csv` 原来只判 `"Date" in text[:200]`，结果抓回 HTML 错误页时，
   页头里的 `Date: Mon, 16 Nov 2026...` 也能命中 → "抓取成功但解析出 0 个点"。
2. `parse` 里空值不补零（补零会让曲线画出一条假的 0% 线）。
3. `derive_spreads` 只在同日两腿都有值时作差，且单独命名 `*_gov`，不覆盖 FRED 官方序列。
"""

import time

import pytest

import ust_curve_treasury as ust


CSV_OK = """Date,1 Mo,2 Mo,3 Mo,4 Mo,6 Mo,1 Yr,2 Yr,3 Yr,5 Yr,7 Yr,10 Yr,20 Yr,30 Yr
11/14/2026,5.32,5.30,5.28,5.20,5.05,4.80,4.55,4.40,4.35,4.42,4.50,4.90,4.95
11/13/2026,5.30,5.29,5.27,5.19,5.04,4.79,4.53,4.39,4.34,4.41,4.49,4.89,
"""


# ── 抓取结果判定 ──────────────────────────────────────────────────────────

def test_looks_like_csv_accepts_real_csv():
    assert ust._looks_like_csv(CSV_OK) is True
    assert ust._looks_like_csv("\ufeff" + CSV_OK) is True     # 带 BOM 也算


def test_looks_like_csv_rejects_html_error_page():
    # 这就是那个 bug：错误页页头带 "Date:"，不能被当成 CSV
    html = "<html><head><title>Error</title></head>\n<body>Date: Mon, 16 Nov 2026 10:00:00 GMT</body></html>"
    assert ust._looks_like_csv(html) is False
    assert ust._looks_like_csv("") is False
    assert ust._looks_like_csv("Date,1 Mo\n") is True        # 只有表头也算 CSV


# ── 解析 ──────────────────────────────────────────────────────────────────

def test_parse_maps_columns_and_types():
    series = ust.parse(CSV_OK)
    assert series["us_dgs10"][0]["value"] == 4.50
    assert series["us_dgs2"][0]["value"] == 4.55
    assert series["us_t_3m"][0]["value"] == 5.28
    assert series["us_dgs10"][0]["ts"] > 1700000000
    # 两个日期 → 每个有值的指标各 2 条
    assert len(series["us_dgs10"]) == 2


def test_parse_skips_empty_cells_without_filling_zero():
    series = ust.parse(CSV_OK)
    # 11/13 的 30Y 是空值 → 该指标只有 1 条，绝不能补一个 0
    assert len(series["us_t_30y"]) == 1
    assert all(r["value"] != 0 for r in series["us_t_30y"])


def test_parse_accepts_iso_date_format():
    csv_iso = "Date,10 Yr\n2026-11-14,4.50\n"
    assert ust.parse(csv_iso)["us_dgs10"][0]["value"] == 4.50


def test_parse_skips_bad_dates_and_non_numeric():
    csv_bad = "Date,10 Yr\nnot-a-date,4.5\n11/14/2026,abc\n11/15/2026,4.6\n"
    series = ust.parse(csv_bad)
    assert len(series["us_dgs10"]) == 1
    assert series["us_dgs10"][0]["value"] == 4.6


def test_parse_empty_text_returns_empty():
    assert ust.parse("Date,10 Yr\n") == {}


# ── 利差推导 ──────────────────────────────────────────────────────────────

def test_derive_spreads_only_on_common_days():
    t1, t2 = 1700006400, 1700092800
    series = {
        "us_dgs10": [{"ts": t1, "value": 4.50}, {"ts": t2, "value": 4.60}],
        "us_dgs2": [{"ts": t1, "value": 4.20}],                  # t2 缺 2Y
        "us_t_3m": [{"ts": t1, "value": 5.30}, {"ts": t2, "value": 5.31}],
    }
    out = ust.derive_spreads(series)
    assert out["us_t10y2y_gov"] == [{"ts": t1, "value": 0.3}]
    assert [r["ts"] for r in out["us_t10y3m_gov"]] == [t1, t2]   # 两天都有 10Y 与 3M
    assert out["us_t10y3m_gov"][0]["value"] == -0.8


def test_derive_spreads_named_gov_not_overwriting_fred():
    out = ust.derive_spreads({"us_dgs10": [{"ts": 1, "value": 4.5}],
                              "us_dgs2": [{"ts": 1, "value": 4.0}]})
    assert "us_t10y2y" not in out        # FRED 的序列名不能被动
    assert "us_t10y2y_gov" in out


def test_derive_spreads_empty_when_leg_missing():
    assert ust.derive_spreads({"us_dgs10": [{"ts": 1, "value": 4.5}]}) == {}


# ── 落库 ──────────────────────────────────────────────────────────────────

def test_ingest_filters_by_days_and_reports_counts(monkeypatch):
    calls = []

    def fake_ingest(**kw):
        calls.append(kw)
        return len(kw["records"])

    monkeypatch.setattr(ust.external_data, "ingest_external", fake_ingest)
    now = int(time.time())
    series = {
        "us_dgs10": [{"ts": now, "value": 4.5}, {"ts": now - 200 * 86400, "value": 4.2}],
        "us_t_3m": [{"ts": now - 200 * 86400, "value": 5.1}],     # 全部超期 → 不写
    }
    stats = ust.ingest(series, days=90)
    assert stats == {"us_dgs10": 1}
    assert calls[0]["source"] == "treasury_gov"
    assert calls[0]["meta"]["official"] is True
    assert len(calls[0]["records"]) == 1


# ── 抓取（网络打桩）──────────────────────────────────────────────────────────

class _R:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def test_fetch_curve_returns_csv(monkeypatch):
    monkeypatch.setattr(ust, "_session", lambda: type("S", (), {
        "get": lambda self, url, timeout=0: _R(200, CSV_OK)})())
    assert ust.fetch_curve(2026) == CSV_OK


def test_fetch_curve_falls_back_to_curl_on_exception(monkeypatch):
    class Boom:
        def get(self, url, timeout=0):
            raise OSError("SSLEOFError")
    monkeypatch.setattr(ust, "_session", lambda: Boom())
    monkeypatch.setattr(ust, "_curl_fetch", lambda url, timeout=25: CSV_OK)
    monkeypatch.setattr(ust.time, "sleep", lambda *_: None)
    assert ust.fetch_curve(2026) == CSV_OK


def test_fetch_curve_raises_after_retries(monkeypatch):
    monkeypatch.setattr(ust, "_session", lambda: type("S", (), {
        "get": lambda self, url, timeout=0: _R(503, "<html>down</html>")})())
    monkeypatch.setattr(ust.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        ust.fetch_curve(2026, retries=2)
    assert "HTTP 503" in str(exc.value)


def test_curl_fetch_rejects_html_and_nonzero(monkeypatch):
    class Out:
        def __init__(self, rc, text):
            self.returncode = rc
            self.stdout = text

    monkeypatch.setattr(ust.subprocess, "run", lambda *a, **k: Out(1, CSV_OK))
    assert ust._curl_fetch("http://x") is None
    monkeypatch.setattr(ust.subprocess, "run", lambda *a, **k: Out(0, "<html>error</html>"))
    assert ust._curl_fetch("http://x") is None
    monkeypatch.setattr(ust.subprocess, "run", lambda *a, **k: Out(0, CSV_OK))
    assert ust._curl_fetch("http://x") == CSV_OK
