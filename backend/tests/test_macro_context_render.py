"""macro_context 的 prompt 渲染用例（模型实际读到的宏观段）。

这一层的特殊性：它不是给用户看的，是**给模型看的**。写错一个数不会有人投诉，
只会在某天悄悄把方向带偏。所以这里测的不是"有没有字"，而是三条底线：
- 全部源挂掉时，输出的是"禁止补造宏观数字"，不是一个看着还行的空段；
- 缺失项汇总成一行「数据缺口」，模型知道哪些没有；
- 周度/日度数据必须挂滞后标记，别被当即时信号用。
"""

import pytest

import macro_context as mc


def _row(metric_key, value, freshness=None, unit="", source="binance_fapi", status="ok"):
    payload = {}
    if freshness:
        payload["freshness"] = freshness
    return {"category": "structure", "source": source, "metric_key": metric_key,
            "value": value, "unit": unit, "status": status, "payload": payload}


# ── 全空 / 部分可用 ──────────────────────────────────────────────────────

def test_all_sources_down_forbids_fabrication():
    out = mc.render_prompt_block({})
    assert "全部外部源不可用" in out
    assert "禁止补造宏观数字" in out
    assert "盘面结构:" not in out


def test_layers_with_only_unavailable_rows_short_circuit():
    # 全部不可用时走的是提前返回：整段只有一句"禁止补造"，不再逐层点名缺口
    layers = {"structure": [_row("BTC.mark", None, status="unavailable")]}
    out = mc.render_prompt_block(layers)
    assert "全部外部源不可用" in out
    assert "盘面结构" not in out


def test_available_rows_are_rendered_and_missing_layers_listed_as_gaps():
    layers = {"structure": [_row("BTC.mark", 101.0)], "fed": [_row("effr", None, status="unavailable")]}
    out = mc.render_prompt_block(layers)
    assert "盘面结构: BTC.mark=101" in out        # 数值后带来源标注（Binance）
    assert "数据缺口" in out and "美联储加息预期" in out
    assert "禁止臆造" in out


def test_weekly_data_is_marked_as_lagging():
    layers = {"structure": [_row("XAU.oi", 5000, freshness="weekly")]}
    out = mc.render_prompt_block(layers)
    assert "周度" in out


def test_closing_line_forbids_filling_missing_fields():
    out = mc.render_prompt_block({"structure": [_row("BTC.mark", 101.0)]})
    assert "缺失字段标 N/A，不得补造" in out
    assert "最终仍输出 20 字段 JSON" in out


def test_multiple_items_in_one_layer_are_pipe_separated():
    layers = {"structure": [_row("BTC.mark", 101.0), _row("BTC.funding", 0.01, unit="%")]}
    out = mc.render_prompt_block(layers)
    assert "BTC.mark=101（Binance） | BTC.funding=0.01%（Binance）" in out


# ── COT 压缩 ─────────────────────────────────────────────────────────────

def test_cot_block_keeps_spec_position_and_folds_the_rest():
    items = [
        _row("GOLD.cot_noncomm_net", 12000, source="cftc"),
        _row("GOLD.cot_comm_net", -3000, source="cftc"),
        _row("GOLD.cot_oi", 450000, source="cftc"),
    ]
    block = mc._cot_block(items)
    assert "投机" in block and "净多" in block
    assert "其余 2 项（商业头寸/未平仓量）已折叠" in block      # 数据不丢，只是不展开
    assert "450,000" not in block


def test_cot_block_with_nothing_to_keep_still_says_where_to_look():
    block = mc._cot_block([_row("GOLD.cot_oi", 450000, source="cftc")])
    assert block.startswith("COT持仓(CFTC): ")
    assert "其余 1 项" in block


# ── 跨资产资金速览进 prompt ──────────────────────────────────────────────

def test_flow_overview_is_appended_when_layers_have_data():
    layers = {
        "fred": [_row("margin_debt", 1_000_000, source="fred"),
                 _row("us_equity_mcap", 50_000_000, source="fred")],
        "flow": [_row("crypto.total_mcap", 2e12, source="coingecko")],
        "structure": [_row("BTC.oi_usd_bybit", 20e9, source="bybit_fapi")],
    }
    out = mc.render_prompt_block(layers)
    assert "跨资产资金速览" in out
    assert "美股杠杆/现货" in out
    assert "加密杠杆/现货" in out


def test_crypto_leverage_ratio_is_aimed_at_market_cap():
    layers = {"flow": [_row("crypto.total_mcap", 1e12), _row("BTC.oi_usd_bybit", 30e9)]}
    out = mc._flow_overview(layers)
    assert "= 3.000%" in out


def test_flow_overview_lists_strongest_long_and_short_cot():
    layers = {"cot": [
        _row("GOLD.cot_noncomm_net", 20000, source="cftc"),
        _row("WTI.cot_noncomm_net", -15000, source="cftc"),
        _row("JPY.cot_noncomm_net", 8000, source="cftc"),
    ]}
    out = mc._flow_overview(layers)
    assert "最强净多" in out and "GOLD净多20,000张" in out
    assert "最强净空" in out and "WTI净空15,000张" in out
