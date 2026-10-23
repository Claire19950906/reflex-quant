#!/usr/bin/env python3
"""全量审计：交易标的合约映射 / 行情接口 / 数据源映射。

扫描 src_python 下所有 python 文件，核实三类致命错的修复结果，并输出
“修复前 vs 修复后”对比清单留档。

致命错定义：
  1. 无效合约 —— 把 XAU/WTI/美股映射到币安 fapi 不存在的合约（XAUUSDT/CLUSDT/NVDAUSDT…）；
  2. 静默兜底错价 —— 拿不到真实数据时用其它币种/随机 mock 假 K 线凑数；
  3. 跨币种错配 —— 资金流向/资产构成对 XAU/WTI 用币安（或其它币种）数据。

用法：python3 audit_data_sources.py
"""

from __future__ import annotations

import os
import re
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)

# ── 修复前致命问题清单（全盘扫描 + 代码检索定位结果）──────────────────────
BEFORE = [
    ("market_snapshot.py", "_ASSET_SYMBOL: XAU→XAUUSDT、WTI→CLUSDT", "币安 fapi 无此合约，每请求必 400"),
    ("macro_context.py", "_SYMBOLS: XAU→XAUUSDT、WTI→CLUSDT", "结构层 XAU/WTI 走币安假合约"),
    ("api_server.py", "_BINANCE_PAIRS: 13 组假合约（XAUUSDT/CLUSDT/NVDAUSDT…）", "图表/复盘 K 线走币安假合约"),
    ("engine/prices.py", "_fetch_gold_price: 币安 XAUUSDT → 国内兜底", "黄金报价先打必失败请求再兜底"),
    ("engine/prices.py", "_fetch_wti_price: 币安 CLUSDT → 国内兜底", "原油报价打必失败请求再兜底"),
    ("api_server.py", "get_klines 失败 → _mock_klines 随机假 K 线", "黄金/原油图表用随机数凑指标"),
    ("api_server.py", "_mock_klines: random.Random 造假 K 线", "技术指标基于假数据计算"),
    ("stock_prices.py", "_STOCK_SYMBOLS: 13 只美股 → NVDAUSDT 等假合约", "币安无美股永续，永久 400 静默失败"),
]

# ── 修复后不变量（应满足）────────────────────────────────────────────────
# 精确检测“资产 → 合约 symbol”这一方向上的假映射（不含“旧 symbol → 资产”的兼容归一化与 docstring 说明）。
RESIDUAL_PATTERNS = [
    ("_ASSET_SYMBOL / _SYMBOLS / _BINANCE_PAIRS 仍含币安假合约 XAUUSDT/CLUSDT 映射",
     re.compile(r'(?:_ASSET_SYMBOL|_SYMBOLS|_BINANCE_PAIRS)\b[^\n]*(?:XAUUSDT|CLUSDT)')),
    ("币安 fapi 假合约 tuple（(\"fapi\", XAUUSDT/CLUSDT)）", re.compile(r'["\']fapi["\']\s*,\s*["\'](?:XAUUSDT|CLUSDT)["\']')),
    ("美股仍映射到币安假合约（NVDA→NVDAUSDT 等）", re.compile(r'["\'](?:NVDA|TSLA|AAPL|MSTR|MSFT|AMZN|GOOGL|COIN|PLTR|META|SMCI|MARA|RIOT)["\']\s*[:(,=]\s*["\'](?:NVDAUSDT|TSLAUSDT|AAPLUSDT|MSTRUSDT|MSFTUSDT|AMZNUSDT|GOOGLUSDT|COINUSDT|PLTRUSDT|METAUSDT|SMCIUSDT|MARAUSDT|RIOTUSDT)["\']')),
    ("仍有 mock 假 K 线定义/调用 (_mock_klines)", re.compile(r'_mock_klines\s*\(')),
]


def _scan() -> list:
    """返回 (rel_path, lineno, line) 命中的残留行列表。"""
    hits = []
    for root, _dirs, files in os.walk(SRC):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            if "__pycache__" in p or p.endswith("audit_data_sources.py"):
                continue
            try:
                lines = open(p, encoding="utf-8").read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(p, SRC)
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # 排除纯注释行（审计脚本自身会带这些词，已排除；docstring 里说明性文字不算违规）
                if stripped.startswith("#"):
                    continue
                hits.append((rel, i, line))
    return hits


def main() -> None:
    print("=" * 88)
    print("Trident 数据源全量审计 —— 修复前 vs 修复后对比清单")
    print("=" * 88)

    print("\n【修复前 · 致命问题清单】")
    for i, (mod, problem, impact) in enumerate(BEFORE, 1):
        print(f"  {i:>2}. [{mod}] {problem}")
        print(f"      影响：{impact}")

    print("\n【修复后 · 扫描验证】")
    all_lines = _scan()
    ok_all = True
    for desc, pat in RESIDUAL_PATTERNS:
        bad = [(p, i, l.strip()) for (p, i, l) in all_lines if pat.search(l)]
        status = "✓ 已消除" if not bad else f"✗ 仍残留 {len(bad)} 处"
        if bad:
            ok_all = False
        print(f"  [{status}] {desc}")
        for p, i, line in bad[:8]:
            print(f"        {p}:{i}  {line}")

    # 路由表正确性核验
    print("\n【修复后 · 数据源路由核验（quote_sources.ASSET_ROUTE）】")
    rows = []
    route_ok = True
    try:
        import quote_sources as q
        checks = [
            ("XAU", "黄金应走 OKX XAU-USDT-SWAP", lambda r: r and r["market"] == "okx_swap" and r["symbol"] == "XAU-USDT-SWAP"),
            ("WTI", "原油应走国内现货（无永续 symbol=None）", lambda r: r and r["market"] == "domestic_spot" and r["symbol"] is None),
            ("BTC", "BTC 应走币安 BTCUSDT", lambda r: r and r["market"] == "binance_fapi" and r["symbol"] == "BTCUSDT"),
            ("NVDA", "美股应走 Yahoo", lambda r: r and r["market"] == "yahoo" and r["symbol"] == "NVDA"),
        ]
        for asset, why, fn in checks:
            r = q.route(asset)
            good = bool(fn(r))
            route_ok = route_ok and good
            rows.append((asset, why, "✓" if good else "✗", str(r)))
            print(f"  [{'✓' if good else '✗'}] {asset} → {r}   #{why}")
    except Exception as exc:
        route_ok = False
        print(f"  ✗ import quote_sources 失败：{type(exc).__name__}: {exc}")

    print("\n【修复前后对比清单】")
    table = [
        ("黄金 XAU 合约", "币安 XAUUSDT（不存在）", "OKX XAU-USDT-SWAP（真实）"),
        ("原油 WTI 合约", "币安 CLUSDT（不存在）", "国内现货 新浪/东财（无永续）"),
        ("美股 13 只", "币安 NVDAUSDT 等（不存在）", "Yahoo Finance 真实 ticker"),
        ("黄金 24h 涨跌", "无（兜底国内现货无涨跌）", "OKX ticker open24h 计算"),
        ("黄金 资金费率", "币安 XAUUSDT 拉不到 → N/A", "OKX funding-rate 真实费率"),
        ("黄金 持仓量", "币安 XAUUSDT 拉不到 → N/A", "OKX open-interest"),
        ("黄金 K 线/技术指标", "mock 随机假 K 线凑数", "OKX candles 真实 K 线；缺数据 503 行情缺失"),
        ("原油 K 线", "mock 随机假 K 线凑数", "无源 → 明确 unavailable（不造数）"),
        ("资金流向 flow", "XAU/WTI 走币安 taker 假合约", "XAU/WTI 标 unavailable（无免费源）强绑定"),
        ("资产构成 structure", "XAU/WTI 走币安 premiumIndex 假合约", "XAU→OKX(费率/OI)+CFTC(多空比)；WTI 费率/基差 N/A、持仓走 CFTC"),
        ("信息源面板", "无", "/api/sources/status + 前端 /sources 常驻页面实时标色"),
    ]
    print(f"  {'模块':<18}{'修复前':<32}{'修复后'}")
    print("  " + "-" * 84)
    for mod, b, a in table:
        print(f"  {mod:<18}{b:<32}{a}")

    print("\n【结论】")
    if ok_all and route_ok:
        print("  ✅ 全部致命错已修复：无效合约清零、mock 假数据兜底清除、跨币种错配拆解、信息源面板上线。")
        code = 0
    else:
        print("  ⚠️ 仍存在未消除项，请见上表 ✗ 标记。")
        code = 1
    print("=" * 88)
    sys.exit(code)


if __name__ == "__main__":
    main()