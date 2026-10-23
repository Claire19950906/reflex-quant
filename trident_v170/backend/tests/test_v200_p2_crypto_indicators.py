"""v200 P2: 加密货币盯盘指标优先级体系（BTC/ETH/SOL 专属知识库）

将领域专家整理的加密盯盘知识注入 prompt，让 LLM 拥有：
1. 三资产对比（黄金/原油/加密）的优先级
2. 加密特有的四层盯盘体系（宏观 → 衍生品 → 链上 → 技术）
3. OI/资金费率/爆仓 等加密衍生品特有指标的解读规则
4. 三资产联动观察规律
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
from engine import ai_worker as _aw


class TestP2CryptoIndicatorKnowledgeBase:
    """v200 P2: 加密盯盘指标优先级知识库嵌入 prompt"""

    def setup_method(self):
        self.src = open(_aw.__file__).read()

    def test_p2_knowledge_section_exists(self):
        """v200 P2 知识库章节存在"""
        assert "## 3.5. 各品种盯盘优先级体系（v200 P2 知识库）" in self.src

    def test_three_assets_overview_present(self):
        """三资产一句话总览"""
        assert "黄金：10年期TIPS实际利率第一" in self.src
        assert "原油：库存+地缘航运第一" in self.src
        assert "加密：TIPS+NDX 第一" in self.src

    def test_macro_layer_indicators(self):
        """宏观层四要素都在"""
        assert "10年期TIPS实际收益率" in self.src
        assert "美元指数 DXY" in self.src
        assert "CME FedWatch" in self.src
        assert "纳指100" in self.src

    def test_derivatives_unique_layer_indicators(self):
        """衍生品特有层四要素都在（OI 是核心）"""
        assert "OI 未平仓持仓量" in self.src
        assert "⭐核心" in self.src
        assert "资金费率 Funding Rate" in self.src
        assert "8h 费率 >0.03%" in self.src
        assert "爆仓数据" in self.src
        assert "BTC.D 市值占比" in self.src

    def test_oi_four_scenarios(self):
        """OI 四象限解读"""
        # 价涨+OI 涨
        assert "价涨+OI 涨" in self.src
        assert "新增杠杆多头进场" in self.src
        # 价涨+OI 跌
        assert "价涨+OI 跌" in self.src
        assert "空头平仓" in self.src
        # 价跌+OI 涨
        assert "价跌+OI 涨" in self.src
        assert "新增空头开空" in self.src
        # 价跌+OI 跌
        assert "价跌+OI 跌" in self.src
        assert "多头止损爆仓去杠杆" in self.src

    def test_funding_rate_thresholds(self):
        """资金费率阈值"""
        assert "8h 费率 >0.03%" in self.src
        assert ">0.1% 高度拥挤" in self.src

    def test_on_chain_indicators(self):
        """链上指标三要素"""
        assert "交易所储备量" in self.src
        assert "MVRV Z-score" in self.src
        assert "稳定币总供应量" in self.src

    def test_technical_indicators(self):
        """盘面技术指标（与黄金原油共用）"""
        assert "RSI" in self.src
        assert "MACD" in self.src
        assert "VWAP" in self.src
        assert "ATR" in self.src
        # 加密特异
        assert "加密波动极大" in self.src

    def test_cross_asset_correlation_rules(self):
        """三资产联动观察规律"""
        # 实际利率下行 → 黄金优先
        assert "实际利率下行" in self.src
        assert "黄金优先涨" in self.src
        assert "BTC 跟黄金共振上涨" in self.src
        # 地缘冲突 → 原油 → 利空加密
        assert "地缘冲突推升原油" in self.src
        assert "利空加密" in self.src
        # 地缘缓和 → 利好黄金 + BTC
        assert "地缘缓和" in self.src
        assert "利好黄金" in self.src
        assert "同时利好 BTC" in self.src

    def test_risk_disclaimer_present(self):
        """加密风险提示必读"""
        assert "加密货币在国内不受法律保护" in self.src
        assert "杠杆衍生品风险极高" in self.src
        assert "不构成任何交易建议" in self.src

    def test_priority_ordering(self):
        """优先级顺序：宏观 > 衍生品 > 链上 > 技术"""
        idx_macro = self.src.find("【宏观层】（与黄金共用")
        idx_deriv = self.src.find("【加密衍生品特有指标】")
        idx_chain = self.src.find("【链上指标】")
        idx_tech = self.src.find("【盘面技术指标】")
        assert 0 < idx_macro < idx_deriv < idx_chain < idx_tech, \
            f"priority order broken: macro={idx_macro} deriv={idx_deriv} chain={idx_chain} tech={idx_tech}"


class TestP2PromptNotBroken:
    """P2 注入后 prompt 结构完整、未破坏现有逻辑"""

    def test_asset_classification_section_intact(self):
        """## 3. 资产归类 段未受影响"""
        src = open(_aw.__file__).read()
        assert "## 3. 资产归类（选 target_asset，最重要）" in src
        # P1-A HK_STOCK
        assert "v200 P1-A: 港股" in src
        # P1-D 聚合性市场信号
        assert "v200 P1-D: 聚合性市场信号归类" in src

    def test_seven_section_reasoning_intact(self):
        """七段式 reasoning_path 未受影响"""
        src = open(_aw.__file__).read()
        for seg in ["[总框架]", "[因素定位]", "[驱动力]", "[水位博弈]",
                    "[跨资产资金]", "[反共识结论]", "[逻辑自检]", "[总结]"]:
            assert seg in src, f"{seg} missing after P2 inject"

    def test_existing_crypto_already_in_prompt(self):
        """v200 P2 后，旧有 crypto 数据字段描述仍在"""
        src = open(_aw.__file__).read()
        assert "crypto.btc_dominance" in src
        assert "oi_usd_bybit" in src
        assert "btc_etf_net" in src