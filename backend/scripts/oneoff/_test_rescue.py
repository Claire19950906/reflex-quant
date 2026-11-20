#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1-B 测试 _rescue_target_asset_from_content（word-boundary 改进后）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from engine.ai_worker import _rescue_target_asset_from_content

cases = [
    # (content, current_target, expected_result, description)
    # === 反例：之前误判 ===
    ("[hash:df617f6c45afb9e19685c7e5] MiCA重点从规则制定转向监管 Cointelegraph 快讯", "NONE", "NONE", "FP1 Cointelegraph 不应匹配 COIN"),
    ("[hash:64c725705c7f594ba5373042] 区块链公司 Blockchain.com 瞄准5亿美元IPO", "NONE", "NONE", "FP2 Blockchain.com 不应匹配 COIN"),
    # === 正例：真正涉及 ticker ===
    ("Coinbase 发布 Q3 财报，COIN 盘后上涨 5%", "NONE", "COIN", "TP1 Coinbase 应匹配 COIN"),
    ("Micron 美光 MU 发布强劲业绩", "NONE", "MU", "TP2 Micron 应匹配 MU"),
    ("SK海力士三季度利润创新高，HBM业务增长", "NONE", "SKHYNIX", "TP3 海力士 应匹配 SKHYNIX"),
    ("美光科技第四季度经调整营收542.3亿美元", "NONE", "MU", "TP4 美光 应匹配 MU"),
    ("台积电据报拟扩大美国布局", "NONE", "TSMC", "TP5 台积电 应匹配 TSMC"),
    # === 已正确填写的 target_asset 不被覆盖 ===
    ("比特币突破 10 万美元，机构持仓增加", "BTC", "BTC", "BTC 不被覆盖"),
    ("原油价格刷新年内新高", "WTI", "WTI", "WTI 不被覆盖"),
    # === 空内容 ===
    ("", "NONE", "NONE", "空内容"),
    # === 不匹配 ===
    ("今天天气真好，气温适宜", "NONE", "NONE", "天气新闻"),
    # === 海力士别名 ===
    ("三星 HBM 业务增长强劲，000660 大涨", "NONE", "SKHYNIX", "000660 命中 SKHYNIX"),
    ("sk hynix 财报超预期", "NONE", "SKHYNIX", "sk hynix 命中 SKHYNIX"),
    # === 三星电子 ===
    ("三星电子发布Q3业绩，存储芯片需求回暖", "OTHER", "SAMSUNG", "三星电子 命中 SAMSUNG"),
    ("005930 上涨", "NONE", "SAMSUNG", "005930 命中 SAMSUNG"),
    # === NVDA ===
    ("英伟达 NVDA 发布新一代 GPU", "NONE", "NVDA", "NVDA 应匹配 NVDA"),
]

passed = 0
failed = 0
for content, current, expected, desc in cases:
    got = _rescue_target_asset_from_content(content, current)
    ok = (got == expected)
    marker = "✓" if ok else "✗"
    print(f"  [{marker}] {desc}")
    if not ok:
        print(f"      got={got!r} expected={expected!r}")
        print(f"      content: {content[:80]!r}")
        failed += 1
    else:
        passed += 1

print(f"\n=== {passed} passed, {failed} failed (total {len(cases)}) ===")
sys.exit(1 if failed else 0)
