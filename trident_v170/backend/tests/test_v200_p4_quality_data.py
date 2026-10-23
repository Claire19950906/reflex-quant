"""v200 P4: 优质数据补全 + 系统级 MarketDataService

修复前问题:
- 5 维数据分散在 market_snapshot.py / server_verdict.py 各处
- 其他模块（knowledge_base / blindspot / experience / 等）拿不到统一优质数据
- 各自重复拉 K 线 (5-10s/次) 或凭印象分析
- 没有跨品种对比表，LLM 决策无系统性"单品种 vs 多品种"参考

P4 修复:
1. server_verdict.py 加 4 个公共 API: get_snapshot_fresh_or_cached / get_verdict_for_asset /
   get_cross_asset_table / get_full_verdict_block
2. _verdict_for_asset 加 7d 涨跌 + OI 增减字段
3. build_verdict 末尾加 ═══ SERVER CROSS-ASSET COMPARE ═══ (4品种×5指标)
4. ai_worker._do_api_call 改调 get_full_verdict_block
5. ai_worker §3.5 措辞精修 (三轴联动 / OI 踩踏 / 资金费率阈值 / 5 条联动规律)
6. §3.5 末尾加 P4 跨品种对比使用规则
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
from engine import ai_worker as _aw
import server_verdict as sv


class TestP4PublicApiExists:
    """v200 P4: server_verdict.py 暴露 4 个公共 API"""

    def test_get_snapshot_fresh_or_cached_exists(self):
        assert hasattr(sv, "get_snapshot_fresh_or_cached")
        snap = sv.get_snapshot_fresh_or_cached()
        assert isinstance(snap, dict)
        assert "status" in snap

    def test_get_verdict_for_asset_exists(self):
        assert hasattr(sv, "get_verdict_for_asset")
        v = sv.get_verdict_for_asset("BTC", snap={"status": "down", "assets": {}})
        assert isinstance(v, dict)
        assert v["asset_id"] == "BTC"
        for k in ("price_str", "change_24h_str", "change_7d_str", "rsi_1h",
                  "rsi_1h_status", "vwap_pos", "volume_ratio", "funding_rate_pct",
                  "oi_change_pct", "trend_1d", "range_position"):
            assert k in v, f"verdict 字典缺少字段 {k}"

    def test_get_cross_asset_table_exists(self):
        assert hasattr(sv, "get_cross_asset_table")
        t = sv.get_cross_asset_table(snap={"status": "down", "assets": {}})
        assert isinstance(t, str)
        assert "SERVER CROSS-ASSET COMPARE" in t

    def test_get_full_verdict_block_exists(self):
        assert hasattr(sv, "get_full_verdict_block")
        block = sv.get_full_verdict_block(news_ts="", target_asset="BTC",
                                          snap={"status": "down", "assets": {}})
        assert isinstance(block, str)
        assert "SERVER CROSS-ASSET COMPARE" in block


class TestP4VerdictForAsset:
    """v200 P4: get_verdict_for_asset 返回结构化字段，含 7d + OI"""

    def _make_snap(self, asset_id="BTC", **overrides):
        asset = {
            "status": "ok", "price": 111234.5, "price_str": "$111234.50",
            "change_24h_pct": 1.23, "change_24h_str": "+1.23%",
            "funding_rate_pct": 0.0053, "funding_rate_str": "+0.0053%",
            "oi_change_pct": 2.1,
            "stats_7d": {
                "trend": "Bull", "trend_strength": "Strong",
                "return_7d_str": "+3.45%", "highest_7d": 115000, "lowest_7d": 105000,
            },
            "mtf": {
                "1h": {
                    "trend": "Mild Bull",
                    "indicators": {"rsi": 55.0, "vwap_pos": "above", "volume_ratio": 1.6},
                },
            },
        }
        asset.update(overrides)
        return {"status": "ok", "ts": 1.0, "summary": "fake", "assets": {asset_id: asset}}

    def test_verdict_basic_fields(self):
        v = sv.get_verdict_for_asset("BTC", self._make_snap())
        assert v["asset_id"] == "BTC"
        assert v["status"] == "ok"
        assert v["price"] == 111234.5
        assert v["change_24h_str"] == "+1.23%"
        assert v["change_7d_str"] == "+3.45%"
        assert v["oi_change_pct"] == 2.1

    def test_verdict_rsi_status_neutral(self):
        v = sv.get_verdict_for_asset("BTC", self._make_snap())
        assert v["rsi_1h_status"] == "中性"

    def test_verdict_rsi_status_overbought(self):
        snap = self._make_snap()
        snap["assets"]["BTC"]["mtf"]["1h"]["indicators"]["rsi"] = 75
        v = sv.get_verdict_for_asset("BTC", snap)
        assert v["rsi_1h_status"] == "超买"

    def test_verdict_rsi_status_oversold(self):
        snap = self._make_snap()
        snap["assets"]["BTC"]["mtf"]["1h"]["indicators"]["rsi"] = 25
        v = sv.get_verdict_for_asset("BTC", snap)
        assert v["rsi_1h_status"] == "超卖"

    def test_verdict_range_position_middle(self):
        v = sv.get_verdict_for_asset("BTC", self._make_snap())
        assert v["range_position"] == "区间中部"

    def test_verdict_range_position_top(self):
        snap = self._make_snap()
        snap["assets"]["BTC"]["price"] = 114000
        v = sv.get_verdict_for_asset("BTC", snap)
        assert v["range_position"] == "区间顶部"

    def test_verdict_range_position_bottom(self):
        snap = self._make_snap()
        snap["assets"]["BTC"]["price"] = 106000
        v = sv.get_verdict_for_asset("BTC", snap)
        assert v["range_position"] == "区间底部"

    def test_verdict_unavailable(self):
        snap = self._make_snap()
        snap["assets"]["BTC"]["status"] = "unavailable"
        # unavailable 时 mtf/stats_7d 应清空（实际 market_snapshot 行为）
        snap["assets"]["BTC"]["mtf"] = {}
        snap["assets"]["BTC"]["stats_7d"] = {}
        v = sv.get_verdict_for_asset("BTC", snap)
        assert v["status"] == "unavailable"
        assert v["rsi_1h_status"] == "N/A"
        assert v["range_position"] == "N/A"


class TestP4CrossAssetTable:
    """v200 P4: 跨品种对比表 (4品种×5指标)"""

    def _make_full_snap(self):
        return {
            "status": "ok", "ts": 1.0, "summary": "fake",
            "assets": {
                "BTC": {
                    "status": "ok", "price": 111234.5, "price_str": "$111234.50",
                    "change_24h_pct": 1.23, "change_24h_str": "+1.23%",
                    "funding_rate_pct": 0.0053, "funding_rate_str": "+0.0053%",
                    "oi_change_pct": 2.1,
                    "stats_7d": {"trend": "Bull", "return_7d_str": "+3.45%",
                                 "highest_7d": 115000, "lowest_7d": 105000},
                    "mtf": {"1h": {"indicators": {"rsi": 55.0, "vwap_pos": "above"}}},
                },
                "ETH": {
                    "status": "ok", "price": 3500, "price_str": "$3500",
                    "change_24h_pct": -0.5, "change_24h_str": "-0.50%",
                    "funding_rate_pct": 0.003, "funding_rate_str": "+0.0030%",
                    "oi_change_pct": -0.3,
                    "stats_7d": {"trend": "Sideways", "return_7d_str": "-1.20%",
                                 "highest_7d": 3600, "lowest_7d": 3400},
                    "mtf": {"1h": {"indicators": {"rsi": 45.0, "vwap_pos": "below"}}},
                },
                "XAU": {
                    "status": "ok", "price": 2050.0, "price_str": "$2050.00",
                    "change_24h_pct": 0.5, "change_24h_str": "+0.50%",
                    "funding_rate_pct": 0.0, "funding_rate_str": "N/A",
                    "oi_change_pct": None,
                    "stats_7d": {"trend": "Bull", "return_7d_str": "+1.20%",
                                 "highest_7d": 2080, "lowest_7d": 2010},
                    "mtf": {"1h": {"indicators": {"rsi": 60.0}}},
                },
                "WTI": {
                    "status": "unavailable", "price_str": "N/A",
                    "change_24h_str": "N/A", "funding_rate_str": "N/A",
                    "stats_7d": {}, "mtf": {},
                },
            },
        }

    def test_table_contains_all_four_assets(self):
        snap = self._make_full_snap()
        t = sv.get_cross_asset_table(snap)
        for asset_id in ("BTC", "ETH", "XAU", "WTI"):
            assert asset_id in t, f"跨品种表缺少 {asset_id}"

    def test_table_header_has_5_columns(self):
        snap = self._make_full_snap()
        t = sv.get_cross_asset_table(snap)
        for col in ("24h", "7d", "费率", "OI变动", "1H RSI"):
            assert col in t, f"表头缺少列 {col}"

    def test_table_oi_threshold_filter(self):
        snap = self._make_full_snap()
        t = sv.get_cross_asset_table(snap)
        eth_line = [l for l in t.split("\n") if "ETH" in l][0]
        assert "N/A" in eth_line

    def test_table_data_line_format(self):
        snap = self._make_full_snap()
        t = sv.get_cross_asset_table(snap)
        btc_line = [l for l in t.split("\n") if "BTC" in l][0]
        assert "+1.23%" in btc_line
        assert "+3.45%" in btc_line
        assert "+2.1%" in btc_line
        assert "55" in btc_line

    def test_table_unavailable_handling(self):
        snap = self._make_full_snap()
        t = sv.get_cross_asset_table(snap)
        wti_line = [l for l in t.split("\n") if "WTI" in l][0]
        assert "数据不可用" in wti_line

    def test_table_down_status(self):
        t = sv.get_cross_asset_table({"status": "down", "assets": {}})
        assert "⚠️" in t or "数据不可用" in t


class TestP4SingleVerdictHas7dAndOI:
    """v200 P4: _verdict_for_asset 文本含 7d + OI"""

    def test_single_verdict_includes_7d(self):
        asset = {
            "status": "ok", "price_str": "$111234.50", "change_24h_str": "+1.23%",
            "funding_rate_str": "+0.0053%", "oi_change_pct": 2.1,
            "stats_7d": {"trend": "Bull", "trend_strength": "Strong", "return_7d_str": "+3.45%"},
            "mtf": {"1h": {"indicators": {"rsi": 55, "vwap_pos": "above", "volume_ratio": 1.6}}},
        }
        line = sv._verdict_for_asset("BTC", asset)
        assert "7d" in line, f"verdict 文本缺 7d 字段: {line}"
        assert "+3.45%" in line

    def test_single_verdict_includes_oi_up(self):
        asset = {
            "status": "ok", "price_str": "$111234.50", "change_24h_str": "+1.23%",
            "funding_rate_str": "+0.0053%", "oi_change_pct": 2.1,
            "stats_7d": {"trend": "Bull", "return_7d_str": "+3.45%"},
            "mtf": {"1h": {"indicators": {"rsi": 55, "vwap_pos": "above", "volume_ratio": 1.6}}},
        }
        line = sv._verdict_for_asset("BTC", asset)
        assert "OI↑" in line

    def test_single_verdict_includes_oi_down(self):
        asset = {
            "status": "ok", "price_str": "$100", "change_24h_str": "-1.00%",
            "funding_rate_str": "N/A", "oi_change_pct": -3.5,
            "stats_7d": {"trend": "Bear", "return_7d_str": "-5.00%"},
            "mtf": {"1h": {"indicators": {"rsi": 35, "vwap_pos": "below", "volume_ratio": 0.5}}},
        }
        line = sv._verdict_for_asset("ETH", asset)
        assert "OI↓" in line

    def test_single_verdict_oi_filter_small(self):
        asset = {
            "status": "ok", "price_str": "$3500", "change_24h_str": "-0.50%",
            "funding_rate_str": "+0.003%", "oi_change_pct": -0.3,
            "stats_7d": {"trend": "Sideways", "return_7d_str": "-1.20%"},
            "mtf": {"1h": {"indicators": {"rsi": 45, "vwap_pos": "below", "volume_ratio": 0.9}}},
        }
        line = sv._verdict_for_asset("ETH", asset)
        assert "OI↑" not in line and "OI↓" not in line

    def test_single_verdict_no_double_dollar(self):
        asset = {
            "status": "ok", "price_str": "$111234.50", "change_24h_str": "+1.23%",
            "funding_rate_str": "+0.0053%", "oi_change_pct": 2.1,
            "stats_7d": {"trend": "Bull", "return_7d_str": "+3.45%"},
            "mtf": {"1h": {"indicators": {"rsi": 55, "vwap_pos": "above", "volume_ratio": 1.6}}},
        }
        line = sv._verdict_for_asset("BTC", asset)
        assert "$$" not in line, f"price_str 重复加 $: {line}"


class TestP4BuildVerdictHasCompareBlock:
    """v200 P4: build_verdict 输出含跨品种对比段"""

    def test_build_verdict_has_compare_header(self):
        snap = {
            "status": "ok", "ts": 1.0, "summary": "fake",
            "assets": {
                "BTC": {"status": "ok", "price_str": "$111234", "change_24h_str": "+1.23%",
                         "funding_rate_str": "+0.0053%", "stats_7d": {"trend": "Bull"},
                         "mtf": {"1h": {"indicators": {"rsi": 55}}}},
                "ETH": {"status": "unavailable"},
                "XAU": {"status": "unavailable"},
                "WTI": {"status": "unavailable"},
            },
        }
        text = sv.build_verdict(snap, news_ts="", target_asset="BTC")
        assert "SERVER CROSS-ASSET COMPARE" in text
        assert "BTC" in text


class TestP4Section35Aligned:
    """v200 P4: §3.5 措辞精修 + P4 跨品种对比使用规则"""

    def test_section35_has_three_axis_view(self):
        src = open(_aw.__file__).read()
        assert "三轴联动" in src

    def test_section35_oi_tread_explicit(self):
        src = open(_aw.__file__).read()
        assert "踩踏" in src

    def test_section35_funding_rate_threshold(self):
        src = open(_aw.__file__).read()
        assert "0.03%" in src
        assert "0.1%" in src

    def test_section35_linkage_law_expanded(self):
        src = open(_aw.__file__).read()
        assert "OI 急速下降" in src
        assert "DXY 与风险资产" in src

    def test_section35_p4_cross_asset_rule(self):
        src = open(_aw.__file__).read()
        assert "v200 P4: 跨品种对比使用规则" in src
        assert "5 指标 = 24h涨跌 / 7d涨跌" in src
        assert "决策结论必须基于本表" in src

    def test_section35_p4_diff_vs_user_original(self):
        src = open(_aw.__file__).read()
        assert "TIPS实际利率 + 纳指100 NDX" in src
        assert "OI 急速下降" in src
        assert "0.03%" in src
        assert "技术性反弹" in src


class TestP4AIWorkerUsesNewAPI:
    """v200 P4: ai_worker._do_api_call 改调 get_full_verdict_block"""

    def test_do_api_call_uses_get_full_verdict_block(self):
        src = open(_aw.__file__).read()
        assert "from server_verdict import get_full_verdict_block" in src
        assert "get_full_verdict_block(" in src


class TestP4BackwardCompat:
    """v200 P4: 旧的 build_verdict() 仍可用（向后兼容）"""

    def test_build_verdict_still_callable(self):
        snap = {"status": "ok", "ts": 1.0, "summary": "fake",
                "assets": {"BTC": {"status": "ok", "price_str": "$111234", "change_24h_str": "+1.23%",
                                    "funding_rate_str": "+0.0053%", "stats_7d": {"trend": "Bull"},
                                    "mtf": {"1h": {"indicators": {"rsi": 55}}}}}}
        text = sv.build_verdict(snap, news_ts="", target_asset="BTC")
        assert isinstance(text, str)
        assert ("SERVER VERDICT" in text or "SERVER MARKET VERDICT" in text
                or "SERVER CROSS-ASSET COMPARE" in text)