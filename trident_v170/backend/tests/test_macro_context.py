"""宏观数据层新源单测：COT 持仓（CFTC）+ 黄金 ETF 持仓（SPDR）。

全离线：mock 注入 get / get_text，不触发真实网络与数据库。
覆盖本轮阶段3落地：
  - fetch_cot 净头寸计算（投机净多/商业净空/周变化/未平仓）
  - fetch_gold_etf 吨数列解析 + 日变化（正=流入/负=流出）
  - fetch_gold_etf 仅盎司列 → 吨换算
  - 失败/空记录 → unavailable（不抛异常、不阻塞主流程）
  - render_prompt_block 注入 COT/ETF 两层后的渲染
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import macro_context as mc


def _cftc_record() -> dict:
    return {
        "commodity_name": "GOLD",
        "report_date_as_yyyy_mm_dd": "2026-09-08T00:00:00.000",
        "open_interest_all": "500000",
        "noncomm_positions_long_all": "300000",
        "noncomm_positions_short_all": "100000",
        "comm_positions_long_all": "150000",
        "comm_positions_short_all": "250000",
        "change_in_noncomm_long_all": "5000",
        "change_in_noncomm_short_all": "-2000",
        "change_in_comm_long_all": "-1000",
        "change_in_comm_short_all": "3000",
    }


def _by_key(rows):
    return {r["metric_key"]: r for r in rows}


def test_fetch_cot_computes_net_positions():
    rows = mc.fetch_cot(lambda url, timeout: [_cftc_record()])
    by_key = _by_key(rows)
    assert by_key["gold.cot_noncomm_net"]["value"] == 200000.0
    assert by_key["gold.cot_noncomm_chg"]["value"] == 7000.0
    assert by_key["gold.cot_comm_net"]["value"] == -100000.0
    assert by_key["gold.cot_comm_chg"]["value"] == -4000.0
    assert by_key["gold.cot_oi"]["value"] == 500000.0
    assert by_key["gold.cot_noncomm_net"]["source"] == "cftc"
    assert by_key["gold.cot_noncomm_net"]["payload"]["report_date"] == "2026-09-08"


def test_fetch_cot_returns_unavailable_on_error():
    def boom(url, timeout):
        raise RuntimeError("boom")

    rows = mc.fetch_cot(boom)
    assert rows and all(r["status"] == "unavailable" for r in rows)


def test_fetch_cot_empty_records():
    rows = mc.fetch_cot(lambda url, timeout: [])
    assert rows and all(r["status"] == "unavailable" for r in rows)


def test_fetch_gold_etf_tonnes_and_change():
    def fake_text(url, timeout):
        return "Date,Total Tonnes in Trust\n2026-09-11,900.5\n2026-09-12,902.3\n"

    rows = mc.fetch_gold_etf(fake_text)
    by_key = _by_key(rows)
    assert by_key["gold.etf_holdings"]["value"] == 902.3
    assert abs(by_key["gold.etf_holdings_chg"]["value"] - 1.8) < 1e-9
    assert by_key["gold.etf_holdings"]["source"] == "spdr"


def test_fetch_gold_etf_ounces_conversion():
    def fake_text(url, timeout):
        return "Date,Total Troy Ounces in Trust\n2026-09-11,28900000\n2026-09-12,29000000\n"

    rows = mc.fetch_gold_etf(fake_text)
    by_key = _by_key(rows)
    expected = 29000000 / 32150.7465
    assert abs(by_key["gold.etf_holdings"]["value"] - expected) < 1e-6


def test_fetch_gold_etf_returns_unavailable_on_error():
    def boom(url, timeout):
        raise RuntimeError("boom")

    rows = mc.fetch_gold_etf(boom)
    assert rows and all(r["status"] == "unavailable" for r in rows)


def test_render_prompt_block_includes_cot_and_etf():
    layers = {
        "structure": [], "sentiment": [], "fed": [], "fred": [],
        "flow": [], "liquidation": [],
        "cot": [
            {"category": "cot", "source": "cftc", "metric_key": "gold.cot_noncomm_net",
             "value": 200000.0, "unit": "张", "status": "ok",
             "payload": {"report_date": "2026-09-08", "noncomm_long": 300000.0,
                         "noncomm_short": 100000.0}},
        ],
        "gold_etf": [
            {"category": "gold_etf", "source": "spdr", "metric_key": "gold.etf_holdings",
             "value": 902.3, "unit": "吨", "status": "ok",
             "payload": {"as_of": "2026-09-12", "note": ""}},
        ],
    }
    text = mc.render_prompt_block(layers)
    assert "COT持仓(CFTC)" in text
    assert "GOLD.COT(投机)净持仓=+200,000张（净多，CFTC）" in text
    assert "黄金ETF持仓(SPDR)" in text
    assert "GOLD.ETF持仓=902.3吨（截止2026-09-12）（SPDR）" in text