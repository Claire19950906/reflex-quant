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
from unittest import mock

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


# ── v310 P1: 去掉网络依赖 ────────────────────────────────────────────────────
# 本用例要验的是**路由**(哪个资产走哪个 fetcher), 不是网络可用性。
# 原版直接打真网络: OKX 被 SNI 阻断 / 熔断 60s / Yahoo 403 限流时,
# _compute_market_style 只能返回 'unknown|n/a|n/a' → 全量回归里偶发红
# (2026-10-29 全量跑: '[OKX-BREAKER] 连续失败 2 次, 熔断 60.0s' → XAU 断言红)。
# 现在把两个 fetcher 换成**确定性假 K 线**, route() 的真实分支逻辑照样被覆盖。
#
# 假 K 线格式: [ts_ms, open, high, low, close, volume]
#   高低价差 0.25 (≈0.25% ATR) → _compute_market_style 判为 volatile_low
#   收盘价缓慢上行 → 有趋势但不极端


def _fake_candles(_symbol_or_asset, _interval, _limit, **_kwargs):
    """确定性假 K 线(不触网)。"""
    n = max(int(_limit or 0), 2)
    base_ts = 1_700_000_000_000
    rows = []
    for i in range(n):
        o = 100.0 + i * 0.1
        c = o + 0.05
        h = c + 0.1
        low = o - 0.1
        rows.append([base_ts + i * 60_000, o, h, low, c, 1000.0 + i])
    return rows


class TestV17611SnapshotRouting(unittest.TestCase):
    """v176.11 fix: snapshot 路由按市场选 fetcher，不再无脑走 OKX"""

    def setUp(self):
        """把两个 K 线 fetcher 换成假实现 → 用例不依赖网络。"""
        from feeds import quote_sources
        for name in ('fetch_binance_spot_candles',
                     'fetch_asset_candles_with_yahoo_fallback'):
            p = mock.patch.object(quote_sources, name, side_effect=_fake_candles)
            p.start()
            self.addCleanup(p.stop)

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
        """WTI 走 domestic_spot：没有专用 fetcher 时返回空，有 yahoo 兜底时给证据。

        v2026.10.05 加了 `fetch_yahoo_candles` 兜底后，WTI 可能拿到真实 K 线
        （全量跑里前面的用例还会往库里写真实 WTI 快照）。这里只保证"不瞎编"：
        要么空、要么是真形态的 close=/pct=，不能是 N/A 占位。
        """
        r = _build_evidence_for_asset('WTI')
        self.assertTrue(r == '' or 'close=' in r, f'WTI 证据形态异常: {r!r}')

    def test_nvda_returns_empty(self):
        """NVDA 走 yahoo 个股：同 WTI，只要求"不瞎编"（空或真形态）。"""
        r = _build_evidence_for_asset('NVDA')
        self.assertTrue(r == '' or 'close=' in r, f'NVDA 证据形态异常: {r!r}')

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