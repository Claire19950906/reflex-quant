# -*- coding: utf-8 -*-
"""v406 P3：交易池外前置分流（pool_out_reason 规则 3b）回归。

背景：323 条进过决策链的新闻里 287 条带降级标记，其中大量是"产品发布 / 人事任免 /
司法 / 文体"，与 BTC/XAU/WTI/ETH/SOL 无关，却照样过门卫 + 主分析两次 LLM，
最后判 NONE 记一条假 HOLD。v396 的 L0-POOL 只覆盖 8.7%（黑名单要求"公司行为 + 公司主体"）。

v406 加规则 3b：**没有任何锚点**（池内品种 / 宏观 / 地缘供给 / 交易所链上 / 监管）
且命中非交易类主体词 → 直接池外。实测覆盖 14.6%（47/323），严格误伤 0 条
（"严格误伤"= 被挡但存在未降级、且 LLM 真绑到池内品种的决策）。

这个用例锁三件事：
1. 该挡的挡（产品发布、外国大选、司法）。
2. 不该挡的不挡（地缘、交易所、宏观、监管）—— regressions 会直接变成丢信号。
3. 纯白名单是开关（默认关），打开后才连"没有主体词"的也挡。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

from feeds import news_router


def _r(title, content=""):
    return news_router.pool_out_reason(title, content)


# ── 1. 该挡 ────────────────────────────────────────────────────────────
def test_product_launch_blocked():
    assert _r("OPPO正式推出全新一代潮流手机OPPO Reno17系列",
              "11月16日，OPPO正式推出全新一代潮流手机。") == "非交易类主体（推出）"


def test_foreign_election_blocked():
    assert _r("佛得角现任总统若泽·马里亚·内韦斯宣布赢得大选") == "非交易类主体（大选）"


def test_judicial_blocked():
    assert _r("闲鱼通报钓鱼竿涉色情交易等四起谣言：已报警，将起诉") == "非交易类主体（起诉）"


def test_personnel_blocked_without_anchor():
    assert _r("澳大利亚再次任命布鲁斯·米勒为外国投资审查委员会主席") == "非交易类主体（任命）"


# ── 2. 不该挡（锚点优先）──────────────────────────────────────────────
def test_geopolitics_and_supply_pass():
    assert _r("【俄外长：欧盟不考虑俄利益“绝对行不通”】") == ""
    assert _r("沙特阿拉伯：一架无人机在麦地那地区被拦截并摧毁") == ""
    assert _r("【美媒：美军“罗斯福”号航母预计明日抵达中东 三艘航母将同时部署】") == ""


def test_crypto_exchange_pass():
    assert _r("《纽约邮报》：币安目标是在本周末前全面恢复提现功能") == ""


def test_macro_pass():
    assert _r("美联储宣布降息25个基点") == ""
    assert _r("美国10月CPI同比上涨3.2%，高于预期") == ""


def test_regulator_pass():
    assert _r("随着参议员挑战CFTC，预测市场监管争议升温") == ""


def test_pool_asset_pass_even_with_launch_word():
    # 有池内锚点时，主体词不能把新闻挡掉（"上市首发"常出现在 ETF/黄金新闻里）
    assert _r("黄金ETF新品上市首发，金价触及两周高点") == ""


# ── 3. 纯白名单是开关 ─────────────────────────────────────────────────
def test_whitelist_is_opt_in(monkeypatch):
    # v407 起「别国本地统计」（埃及最低工资/以色列CPI）由市场归属规则默认判池外，
    # 不再用这个样例验证白名单开关；这里换成一条真的没有任何锚点的文体稿。
    title = "意大利北部小镇举办年度松露节"
    assert _r(title) == ""                       # 默认关：无主体词 → 放行
    monkeypatch.setenv("NEWS_POOL_WHITELIST", "1")
    assert _r(title) == "无池内锚点"              # 打开后：无锚点一律池外


def test_whitelist_still_respects_anchor(monkeypatch):
    monkeypatch.setenv("NEWS_POOL_WHITELIST", "1")
    assert _r("中东局势升级，霍尔木兹海峡通行受阻") == ""


# ── 4. 空串/异常输入不炸 ──────────────────────────────────────────────
def test_empty_input():
    assert _r("") == ""
    assert _r("", "") == ""
