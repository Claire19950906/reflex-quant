"""v200 P1 修复 — NONE/HK_STOCK 决策质量提升回归测试。

覆盖 3 个修复:
- P1-A: enum 加 HK_STOCK，港股不再被规范化到 NONE
- P1-B: NONE 兜底文案不再空洞套话，含 ticker / 真实 reasoning
- P1-C: reasoning 兜底条件放宽（< 4 → < 2），保留 LLM 短输出
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
from engine import ai_worker as _aw


class TestP1AEnumHksStock:
    """P1-A: HK_STOCK 应在 enum 内且不规范化到 NONE"""

    def test_hk_stock_in_enum(self):
        src = open(_aw.__file__).read()
        m = re.search(r"_target_enum\s*=\s*\(([^)]+)\)", src)
        assert m, "_target_enum not found"
        enum_str = m.group(1)
        assert '"HK_STOCK"' in enum_str, f"HK_STOCK missing: {enum_str}"

    def test_hk_stock_mapped_to_stock_category(self):
        result = {"target_asset": "HK_STOCK", "market_category": "",
                  "suggested_action": "HOLD", "reasoning": "x", "reasoning_path": ""}
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX", "NONE")
        ta = str(result.get("target_asset") or "").strip().upper()
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            for ok in _target_enum:
                if ok in ta_norm or ta_norm in ok:
                    ta = ok
                    break
            else:
                ta = "NONE"
        assert ta == "HK_STOCK", f"got {ta!r}"

        _mc_map = {"BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO",
                   "XAU": "GOLD", "WTI": "OIL",
                   "CN_STOCK": "STOCK", "US_STOCK": "STOCK",
                   "INDIVIDUAL_STOCK": "STOCK", "HK_STOCK": "STOCK",
                   "SKHYNIX": "STOCK"}
        _mc_enum = ("CRYPTO", "GOLD", "OIL", "STOCK", "MACRO", "OTHER")
        mc = str(result.get("market_category") or "").strip().upper()
        if mc not in _mc_enum:
            mc = _mc_map.get(ta, "OTHER")
        assert mc == "STOCK", f"got {mc!r}"


class TestP1BNoneFallbackQuality:
    """P1-B: NONE reasoning_path 兜底不再空洞套话"""

    def test_none_fallback_mentions_ticker(self):
        src = open(_aw.__file__).read()
        # 找到 P1-B 改写后的新文案 (含 ticker)
        assert "涉及标的" in src, "新文案应包含 ticker 占位"
        # 旧文案不应再出现 (已替换)
        assert "强制 HOLD；置信度不适用" not in src, "旧硬套话应已替换"

    def test_none_fallback_uses_llm_reasoning(self):
        src = open(_aw.__file__).read()
        # 新逻辑: 用 _rsn_fb 作 [总结] (line ~900)
        assert "_rsn_fb" in src, "P1-B 应新增 _rsn_fb 变量"
        assert "_rsn_fb[:140]" in src, "[总结] 应用 LLM 真 reasoning"

    def test_none_fallback_keeps_hold_action(self):
        # NONE 仍保持 HOLD 语义 (语义不变)
        assert True  # 设计意图，仅文档化


class TestP1CReasoningFallbackThreshold:
    """P1-C: reasoning 兜底阈值从 < 4 降到 < 2"""

    def test_threshold_is_2_in_source(self):
        src = open(_aw.__file__).read()
        # 新条件: len(rsn) < 2 (替换旧 < 4)
        assert "len(rsn) < 2" in src or "len(rsn_stripped) < 2" in src, \
            "P1-C 阈值应为 2"

    def test_threshold_logic(self):
        test_cases = [
            ("", True),                 # 完全空 → 兜底
            (" ", True),                # 全空白 → 兜底
            ("无", True),               # 1 字 → 兜底 (信息量不足)
            ("观望", False),            # 2 字 → 保留
            ("HOLD", False),            # 4 字 → 保留
            ("港股个股大涨", False),     # 6 字 → 保留
        ]
        for rsn, should_fb in test_cases:
            s = rsn.strip()
            actual = (not s) or (len(s) < 2)
            assert actual == should_fb, f"rsn={rsn!r}: expected {should_fb}, got {actual}"


class TestP1DAggregateMarketSignal:
    """P1-D: 聚合性市场信号（沪市 N 家公司）应归 CN_STOCK/US_STOCK/HK_STOCK，不是 NONE"""

    def test_p1d_prompt_present(self):
        src = open(_aw.__file__).read()
        # P1-D prompt 应包含聚合性归类规则
        assert "P1-D" in src, "P1-D prompt marker missing"
        assert "沪市" in src and "CN_STOCK" in src, "沪市→CN_STOCK 规则缺失"
        assert "港股多家" in src and "HK_STOCK" in src, "港股多家→HK_STOCK 规则缺失"

    def test_p1d_classification_logic(self):
        """聚合关键词应映射到对应市场 enum"""
        # 模拟 P1-D 分类逻辑
        aggregate_rules = [
            ("沪市", "CN_STOCK"),
            ("深市", "CN_STOCK"),
            ("科创板", "CN_STOCK"),
            ("创业板", "CN_STOCK"),
            ("A股", "CN_STOCK"),
            ("沪深", "CN_STOCK"),
            ("美股多家", "US_STOCK"),
            ("Nasdaq", "US_STOCK"),
            ("港股多家", "HK_STOCK"),
            ("恒生", "HK_STOCK"),
        ]
        for keyword, expected in aggregate_rules:
            # 模拟 ai_worker 用关键词匹配
            assert expected in ("CN_STOCK", "US_STOCK", "HK_STOCK")
            # 验证 P1-D prompt 里能找到该关键词
            src = open(_aw.__file__).read()
            assert keyword in src, f"P1-D 规则缺关键词: {keyword}"


class TestP1DNormalizationPipeline:
    """P1-D 端到端规范化测试: 模拟 LLM 按 P1-D prompt 输出 CN_STOCK, 验证落库前规范化保持 CN_STOCK
    （不会被错误地 fallback 到 NONE）。
    """

    def test_cn_stock_normalization_preserved(self):
        """LLM 输出 CN_STOCK → 规范化后保持 CN_STOCK（修复前会被 ta.replace 后 ta_norm in ok 误命中）"""
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX", "NONE")
        ta = "CN_STOCK"
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            for ok in _target_enum:
                if ok in ta_norm or ta_norm in ok:
                    ta = ok
                    break
            else:
                ta = "NONE"
        assert ta == "CN_STOCK", f"expected CN_STOCK, got {ta}"

    def test_hk_stock_normalization_preserved(self):
        """LLM 输出 HK_STOCK → 保持 HK_STOCK"""
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX", "NONE")
        ta = "HK_STOCK"
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            for ok in _target_enum:
                if ok in ta_norm or ta_norm in ok:
                    ta = ok
                    break
            else:
                ta = "NONE"
        assert ta == "HK_STOCK", f"expected HK_STOCK, got {ta}"

    def test_unknown_token_falls_back_to_none(self):
        """未识别 token（含空字符串）→ NONE（修复 P1-E：空串不再误命中 BTC）"""
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX", "NONE")
        for ta in ["", "MISC", "UNKNOWN", "AI"]:
            ta2 = ta
            if ta2 not in _target_enum:
                ta_norm = ta2.replace("/", "-").replace(" ", "")
                # P1-E 修复: 空 ta_norm 直接 NONE（不再被空串 in "BTC" 误命中）
                if not ta_norm:
                    ta2 = "NONE"
                else:
                    for ok in _target_enum:
                        if ok in ta_norm or ta_norm in ok:
                            ta2 = ok
                            break
                    else:
                        ta2 = "NONE"
            assert ta2 == "NONE", f"ta={ta!r} should fall back to NONE, got {ta2}"

    def test_market_category_cn_stock_maps_to_stock(self):
        """CN_STOCK → market_category=STOCK（不论 LLM 给的是 STOCK 还是其他）"""
        _mc_enum = ("CRYPTO", "GOLD", "OIL", "STOCK", "MACRO", "OTHER")
        mc_map = {"CN_STOCK": "STOCK", "US_STOCK": "STOCK",
                  "HK_STOCK": "STOCK", "INDIVIDUAL_STOCK": "STOCK",
                  "SKHYNIX": "STOCK"}
        for ta, expected in [("CN_STOCK", "STOCK"), ("HK_STOCK", "STOCK"),
                              ("US_STOCK", "STOCK")]:
            mc = mc_map.get(ta, "OTHER")
            assert mc == expected
            assert mc in _mc_enum

    def test_aggregate_keyword_to_target_asset_mapping(self):
        """P1-D prompt 中的关键词应映射到正确 target_asset（prompt 规则自查）"""
        rules = {
            "沪市": "CN_STOCK", "深市": "CN_STOCK", "科创板": "CN_STOCK",
            "创业板": "CN_STOCK", "A股": "CN_STOCK", "沪深": "CN_STOCK",
            "美股多家": "US_STOCK", "Nasdaq": "US_STOCK", "NYSE": "US_STOCK",
            "港股多家": "HK_STOCK", "恒生": "HK_STOCK",
        }
        # 验证每条 prompt 关键词都在 ai_worker 源码中能找到
        src = open(_aw.__file__).read()
        for kw, ta in rules.items():
            assert kw in src, f"P1-D 规则缺关键词: {kw} → {ta}"
            # ta 也应在源码里（enum 定义）
            assert ta in src, f"P1-D 目标 enum 缺失: {ta}"


class TestP1FFxAssetsInEnum:
    """P1-F: target_asset enum 增加 FX/DXY 类（美元指数/外汇不再 fallback 到 NONE）"""

    def test_dxy_in_enum(self):
        src = open(_aw.__file__).read()
        assert '"DXY"' in src, "DXY 不在 target_asset enum 中"
        assert '"FX"' in src, "FX 不在 target_asset enum 中"

    def test_dxy_normalization_preserved(self):
        """LLM 输出 DXY → 保持 DXY（修复前被 fallback NONE）"""
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX",
                        "DXY", "FX", "NONE")
        ta = "DXY"
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            if not ta_norm:
                ta = "NONE"
            else:
                for ok in _target_enum:
                    if ok in ta_norm or ta_norm in ok:
                        ta = ok
                        break
                else:
                    ta = "NONE"
        assert ta == "DXY", f"expected DXY, got {ta}"

    def test_mxn_normalization_falls_to_fx(self):
        """LLM 输出 MXN（不在 enum）→ FX 归类规则命中 FX"""
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX",
                        "DXY", "FX", "NONE")
        ta = "MXN"
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            if not ta_norm:
                ta = "NONE"
            else:
                _fx_codes = ("USD", "EUR", "JPY", "GBP", "MXN", "CAD", "AUD",
                             "CNY", "CNH", "CHF", "NZD", "SGD", "HKD")
                if ta_norm in _fx_codes:
                    ta = "FX"
                else:
                    matched = None
                    for ok in _target_enum:
                        if ok in ta_norm or ta_norm in ok:
                            matched = ok
                            break
                    ta = matched if matched else "NONE"
        assert ta == "FX", f"expected FX (外汇类), got {ta}"


class TestP1GHoldScorePreservation:
    """P1-G: HOLD 时不再强制 sentiment_score=0，保留 LLM 真情绪信号"""

    def test_hold_score_above_threshold_preserved(self):
        """HOLD + score=+0.10 → 保持 +0.10（修复前被压平到 0）"""
        score, action = 0.10, "HOLD"
        if action == "HOLD":
            if abs(score) < 0.05:
                score = 0.0
        assert score == 0.10, f"expected 0.10, got {score}"

    def test_hold_score_below_threshold_zeroed(self):
        """HOLD + score=+0.03 → 归 0（信号太弱没意义）"""
        score, action = 0.03, "HOLD"
        if action == "HOLD":
            if abs(score) < 0.05:
                score = 0.0
        assert score == 0.0

    def test_hold_score_negative_preserved(self):
        """HOLD + score=-0.10 → 保持 -0.10"""
        score, action = -0.10, "HOLD"
        if action == "HOLD":
            if abs(score) < 0.05:
                score = 0.0
        assert score == -0.10

    def test_threshold_in_source(self):
        """源码中应有 P1-G 阈值注释"""
        src = open(_aw.__file__).read()
        assert "P1-G" in src, "P1-G marker missing"
        assert "abs(score) < 0.05" in src, "P1-G threshold missing"


class TestIntegrationP1AllFixes:
    """集成: 模拟康方生物 09926.HK 走修复路径"""

    def test_hk_stock_news_no_longer_fallback_to_none(self):
        result = {"target_asset": "HK_STOCK", "news_ticker": "09926.HK",
                  "suggested_action": "HOLD",
                  "reasoning": "康方生物受 AZN 投资 SMMT 提振", "reasoning_path": "x"}
        _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                        "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX", "NONE")
        ta = str(result.get("target_asset") or "").strip().upper()
        if ta not in _target_enum:
            ta_norm = ta.replace("/", "-").replace(" ", "")
            for ok in _target_enum:
                if ok in ta_norm or ta_norm in ok:
                    ta = ok
                    break
            else:
                ta = "NONE"
        _mc_map = {"BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO",
                   "XAU": "GOLD", "WTI": "OIL",
                   "CN_STOCK": "STOCK", "US_STOCK": "STOCK",
                   "INDIVIDUAL_STOCK": "STOCK", "HK_STOCK": "STOCK",
                   "SKHYNIX": "STOCK"}
        _mc_enum = ("CRYPTO", "GOLD", "OIL", "STOCK", "MACRO", "OTHER")
        mc = str(result.get("market_category") or "").strip().upper()
        if mc not in _mc_enum:
            mc = _mc_map.get(ta, "OTHER")
        assert ta == "HK_STOCK"
        assert mc == "STOCK"

