#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v176.11 fix 回归测试：_build_evidence_for_asset / _compute_market_style 路由选择。

背景：
  - 旧版无脑用 fetch_okx_candles → BTC/ETH/SOL 走 OKX 必然失败（OKX 上没有 BTC-USDT-SWAP
    这种合约），导致 hard_data_evidence 总是 "BTC N/A | 服务端兜底：未抓 K 线"。
  - 修复：按 quote_sources.route(asset) 的 market 字段选 fetcher：
      binance_fapi/binance_spot → fetch_binance_spot_candles（spot 国内直连稳定）
      okx_swap                  → fetch_okx_candles（XAU/XAG 已有国内现货兜底）
      yahoo / domestic_spot     → 返回空（未实现 K 线 fetcher，标记为 N/A 更诚实）

用例：
  1. BTC/ETH/XAU/SOL（4 个监控品种）→ 必须返回非空 evidence（含 close= / pct=）
  2. WTI/NVDA（无 fetcher 资产）→ 返回空（诚实的 N/A）
  3. 未在路由表的资产（INDIVIDUAL_STOCK）→ 返回空（避免误用别币种 K 线）
  4. _compute_market_style 同 bug 同样修复
"""
import os
import re
import sys
import unittest

# 让 tests/ 目录可直接 import engine.ai_worker
# 用相对路径定位「本仓库」的 src_python —— 原先这里硬编码了另一个仓库的绝对路径,
# 导致本测试实际导入的是别处的 ai_worker, 根本没测到本仓库的代码。
# tests/conftest.py 已做全局注入, 这里只做兜底且保持幂等。
_SRC_PYTHON = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python")
)
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

from engine.ai_worker import _build_evidence_for_asset, _compute_market_style


class TestV17611SnapshotRouting(unittest.TestCase):
    """v176.11 fix: snapshot 路由按市场选 fetcher，不再无脑走 OKX"""

    # ── 1. 4 个监控品种：必须拿到真实 K 线 ──
    def test_btc_returns_real_evidence(self):
        r = _build_evidence_for_asset('BTC')
        self.assertTrue(r, 'BTC evidence 应非空（旧版走 OKX 必然失败）')
        self.assertRegex(r, r'BTC .* close=\d+(\.\d+)? pct=[+-]?\d+(\.\d+)?%')
        self.assertIn('1H close=', r, '应包含 1H K 线')

    def test_eth_returns_real_evidence(self):
        r = _build_evidence_for_asset('ETH')
        self.assertTrue(r, 'ETH evidence 应非空')
        self.assertRegex(r, r'ETH .* close=\d+(\.\d+)? pct=')

    def test_xau_returns_real_evidence(self):
        r = _build_evidence_for_asset('XAU')
        self.assertTrue(r, 'XAU evidence 应非空（OKX 主源 + 国内现货兜底）')
        self.assertRegex(r, r'XAU .* close=\d+(\.\d+)?')

    def test_sol_returns_real_evidence(self):
        r = _build_evidence_for_asset('SOL')
        self.assertTrue(r, 'SOL evidence 应非空')
        self.assertRegex(r, r'SOL .* close=\d+(\.\d+)?')

    # ── 2. 无 fetcher 的资产：返回空（诚实 N/A） ──
    def test_wti_returns_empty(self):
        """WTI 走 domestic_spot，无 K 线 fetcher → 返回空字符串"""
        r = _build_evidence_for_asset('WTI')
        self.assertFalse(r, f'WTI 应返回空（未实现 candles fetcher），实际: {r!r}')

    def test_nvda_returns_empty(self):
        """NVDA 走 yahoo，无 K 线 fetcher → 返回空字符串"""
        r = _build_evidence_for_asset('NVDA')
        self.assertFalse(r, f'NVDA 应返回空，实际: {r!r}')

    # ── 3. 路由表外的资产：返回空（避免误用别币种 K 线） ──
    def test_unknown_asset_returns_empty(self):
        """INDIVIDUAL_STOCK 不在 ASSET_ROUTE → 返回空，绝不兜底到别币种"""
        r = _build_evidence_for_asset('INDIVIDUAL_STOCK')
        self.assertFalse(r, f'未知资产应返回空，实际: {r!r}')

    def test_case_insensitive_routing(self):
        """大小写不敏感（route 内部已 .upper()）"""
        r = _build_evidence_for_asset('btc')
        self.assertTrue(r, '小写 btc 也应能拿到 evidence')

    # ── 4. _compute_market_style 同样修复：BTC/ETH/XAU/SOL 应有风格标签 ──
    def test_compute_market_style_for_monitored_assets(self):
        for asset in ['BTC', 'ETH', 'XAU', 'SOL']:
            r = _compute_market_style(asset)
            self.assertNotEqual(
                r, 'unknown|n/a|n/a',
                f'{asset} 应能算出市场风格，旧版走 OKX 必然返回 unknown'
            )
            self.assertRegex(r, r'^(trending_up|trending_down|mean_revert|volatile_high|volatile_low)\|')

    def test_compute_market_style_unknown_asset(self):
        """路由表外的资产 → unknown|n/a|n/a"""
        self.assertEqual(_compute_market_style('INDIVIDUAL_STOCK'), 'unknown|n/a|n/a')
        self.assertEqual(_compute_market_style('CN_STOCK'), 'unknown|n/a|n/a')


if __name__ == '__main__':
    unittest.main(verbosity=2)