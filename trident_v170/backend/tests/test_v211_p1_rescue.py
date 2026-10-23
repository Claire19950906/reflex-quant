#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1 修复 — 海力士新闻修复回归测试。

覆盖 3 个修复:
- P1-A: stock_news.STOCK_TICKERS 扩 ticker（含韩股/台股/半导体）
- P1-B: _rescue_target_asset_from_content 服务端兜底
- P1-C: _ASSET_ALIASES + TARGET_TO_MARKET + llm_schema 扩 ticker
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))


class TestP1ASourceExpansion:
    """P1-A: stock_news 应扩 ticker，覆盖韩股/台股/半导体个股"""

    def test_extended_stock_tickers_exists(self):
        from stock_news import _EXTENDED_STOCK_TICKERS
        assert isinstance(_EXTENDED_STOCK_TICKERS, list)
        assert len(_EXTENDED_STOCK_TICKERS) > 0
        tickers = [e["ticker"] for e in _EXTENDED_STOCK_TICKERS]
        assert "000660.KS" in tickers  # SK Hynix
        assert "005930.KS" in tickers  # Samsung
        assert "2330.TW" in tickers  # TSMC
        assert "MU" in tickers  # Micron

    def test_extended_stock_tickers_have_required_fields(self):
        from stock_news import _EXTENDED_STOCK_TICKERS
        for ext in _EXTENDED_STOCK_TICKERS:
            assert "ticker" in ext
            assert "market_label" in ext
            assert "target_asset_hint" in ext
            assert ext["ticker"]
            assert ext["market_label"] in ("韩股", "台股", "美股")
            assert ext["target_asset_hint"]

    def test_no_overlap_with_us_tickers(self):
        from stock_news import STOCK_TICKERS, _EXTENDED_STOCK_TICKERS
        ext = {e["ticker"] for e in _EXTENDED_STOCK_TICKERS}
        # 美股 ticker 不应在 _EXTENDED 里（避免重复抓）
        assert len(ext & set(STOCK_TICKERS)) == 0


class TestP1BRescueFunction:
    """P1-B: _rescue_target_asset_from_content 服务端兜底"""

    def test_rescue_hynix_chinese(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("SK海力士三季度利润创新高，HBM业务增长", "NONE") == "SKHYNIX"

    def test_rescue_hynix_english(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("SK Hynix earnings beat expectations", "NONE") == "SKHYNIX"

    def test_rescue_hynix_code(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("000660 业绩超预期", "NONE") == "SKHYNIX"

    def test_rescue_samsung(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("三星电子发布Q3业绩", "OTHER") == "SAMSUNG"

    def test_rescue_micron(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("美光科技 Micron 业绩超预期", "NONE") == "MU"

    def test_rescue_tsmc(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("台积电拟扩大美国布局", "NONE") == "TSMC"

    def test_rescue_intel(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("英特尔股价下跌", "NONE") == "INTC"

    def test_rescue_keeps_existing_correct(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("比特币突破新高", "BTC") == "BTC"
        assert _rescue_target_asset_from_content("原油价格", "WTI") == "WTI"

    def test_rescue_empty_content(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("", "NONE") == "NONE"

    def test_rescue_no_match(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("天气真好", "NONE") == "NONE"

    def test_rescue_coinbase_true_positive(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("Coinbase 发布 Q3 财报，COIN 盘后上涨", "NONE") == "COIN"

    def test_rescue_cointelegraph_false_positive_blocked(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        r = _rescue_target_asset_from_content("MiCA重点从规则制定转向监管 Cointelegraph 快讯", "NONE")
        assert r == "NONE", f"应不匹配 COIN，实际 {r!r}"

    def test_rescue_blockchain_false_positive_blocked(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        r = _rescue_target_asset_from_content("区块链公司 Blockchain.com 瞄准5亿美元IPO", "NONE")
        assert r == "NONE", f"应不匹配 COIN，实际 {r!r}"

    def test_rescue_other_tickers(self):
        from engine.ai_worker import _rescue_target_asset_from_content
        assert _rescue_target_asset_from_content("英伟达 NVDA 发布新一代 GPU", "NONE") == "NVDA"
        assert _rescue_target_asset_from_content("特斯拉大涨", "NONE") == "TSLA"
        assert _rescue_target_asset_from_content("苹果公司业绩", "NONE") == "AAPL"
        assert _rescue_target_asset_from_content("META 财报", "NONE") == "META"


class TestP1CTickerDictionary:
    """P1-C: ticker 字典扩展（_ASSET_ALIASES + TARGET_TO_MARKET + llm_schema）"""

    def test_asset_aliases_has_new_tickers(self):
        from engine.ai_worker import _ASSET_ALIASES
        assets = [a for a, _ in _ASSET_ALIASES]
        for expected in ("MU", "INTC", "TSMC", "SAMSUNG", "SKHYNIX"):
            assert expected in assets, f"{expected} 缺 _ASSET_ALIASES"

    def test_target_to_market_has_new_tickers(self):
        import market_classifier
        for expected in ("MU", "INTC", "TSMC", "SAMSUNG", "SKHYNIX"):
            assert expected in market_classifier.TARGET_TO_MARKET, f"{expected} 缺 TARGET_TO_MARKET"
            assert market_classifier.TARGET_TO_MARKET[expected] == "STOCK"

    def test_llm_schema_has_new_tickers(self):
        import llm_schema
        enum = llm_schema.AI_DECISION_SCHEMA["properties"]["target_asset"]["enum"]
        for expected in ("MU", "INTC", "TSMC", "SAMSUNG"):
            assert expected in enum, f"{expected} 缺 llm_schema enum"

    def test_validate_pair_new_tickers(self):
        import market_classifier
        for t in ("SKHYNIX", "MU", "INTC", "TSMC", "SAMSUNG"):
            assert market_classifier.validate_pair(t, "STOCK") == (t, "STOCK")


class TestP1AIngestHelper:
    """P1-A: _ingest_external_news_sync 应把 target_asset_hint 注入 gate"""

    def test_ingest_code_uses_target_asset_hint(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "src_python", "api_server.py")).read()
        # 用基于行号的简单查找：找到 def _ingest_external_news_sync 起，
        # 往后扫直到下一个「列首 def 」出现
        m = re.search(r"^def _ingest_external_news_sync\(", src, re.MULTILINE)
        assert m, "_ingest_external_news_sync not found"
        start = m.start()
        # 找下一个 0-indent 的 def（即 function-level）
        rest = src[start + 1:]
        m2 = re.search(r"^def ", rest, re.MULTILINE)
        end = (start + 1 + m2.start()) if m2 else len(src)
        body = src[start:end]
        assert "target_asset_hint" in body, "_ingest_external_news_sync 应使用 target_asset_hint"
        assert "gate_payload" in body, "应构建 gate_payload 注入 ticker 提示"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
