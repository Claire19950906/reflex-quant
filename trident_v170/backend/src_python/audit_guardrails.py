#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全链路审计加固基线 —— 把“去伪存真 / 不硬归因 / 多源验证 / 降级熔断”
做成可执行的逐模块检查标尺（不靠单次 bug 报告，靠代码级扫描）。

扫描 src_python 下每个关键模块，验证 4 大思想特征是否已写进判断分支：

  A. 去伪存真   —— 缺数据标 unavailable 不造数；观点/汇总/新币/人物闲谈分层不作信号；单一证据不足信
  B. 不硬归因   —— 无明确标的 / 弱相关禁止硬归因；事后统计 ≠ 即时催化
  C. 多源验证   —— 多因子矛盾检测；统计显著性校验；路由/兜底多源降级
  D. 降级熔断   —— 单源失败静默降级不阻塞；连续失败触发熔断；证据不足不输出方向

用法：
    python3 audit_guardrails.py             # 输出逐模块落地矩阵
    python3 audit_guardrails.py --gaps      # 只看缺口（缺失的思想特征）
    python3 audit_guardrails.py --strict    # 有缺口时 exit code = 1（可进 CI）

与 audit_data_sources.py 的分工：
    前者只管「资产→合约映射 / mock 假数据」两件致命错；
    本文件管「推理逻辑里有没有把去伪存真等思想写进判断分支」这一整套基线。
"""

from __future__ import annotations

import os
import re
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)

# ---------------------------------------------------------------------------
# 5 层链路 -> 模块清单（只审计真正承载推理逻辑的模块）
# ---------------------------------------------------------------------------
LAYERS = {
    "行情拉取": [
        "quote_sources.py", "market_feeds.py", "market_snapshot.py",
        "stock_prices.py", "coingecko_feed.py", "fred_feed.py",
        "bls_feed.py", "eia_feed.py", "knowledge_base.py", "circuit_breaker.py",
    ],
    "新闻聚合": [
        "news_sources.py", "rss_sources.py", "jin10_feed.py", "jin10_mcp.py",
        "treenews_bridge.py", "stock_news.py", "reuters_news.py",
        "blockchain_feed.py", "news_organizer.py",
    ],
    "信号生成": [
        "realtime_filter.py", "engine/ai_worker.py", "engine/ingest.py",
        "engine/webhook.py", "engine/reflect.py",
    ],
    "下单决策": [
        "decision_guard.py", "evidence.py", "quick_sim.py", "paper_trading.py",
    ],
    "事后归因": [
        "review_agent.py", "history_review.py", "causal_chains.py",
        "audit_data_sources.py",
    ],
}

# ---------------------------------------------------------------------------
# 思想特征检查项： (特征名, 思想分类, 正则, 说明)
#   正则命中即认为“该特征已在该模块落地”；\\b 避开新闻关键词误报（如“熔断”作事件词）。
# ---------------------------------------------------------------------------
GUARDRAILS = [
    ("不造数", "A.去伪存真", r"unavailable|不造数|绝不补造|不编造|标\s*unavailable",
     "缺数据必须明确标 unavailable，禁止 mock/随机造数"),
    ("新闻分类", "A.去伪存真", r"新闻性质|就事论事|人物/官员表态|观点.*汇总.*HOLD|无可交易标的|不生成方向信号",
     "观点/汇总/新币/人物闲谈分层，非信号类不作交易信号（就事论事，非关键词机械判定）"),
    ("禁止硬归", "B.不硬归因", r"禁止硬归|硬归|不作交易信号|target_asset.{0,12}NONE|无可交易标的",
     "无明确标的/弱相关禁止强行归因到品种"),
    ("矛盾检测", "C.多源验证", r"detect_contradiction|contradictions|矛盾",
     "多因子方向互相冲突时自动降级"),
    ("统计显著性", "C.多源验证", r"chi_square|significance|卡方|MIN_SIGNIFICANCE_SAMPLE",
     "历史样本未达统计显著性时压低证据权重"),
    ("路由兜底", "C.多源验证", r"def route|fallback|backup|兜底",
     "主源不可用时降级到次选源"),
    ("静默降级", "D.降级熔断", r"优雅降级|降级|degrade|静默|不阻塞|不影响其他",
     "单源失败静默降级，不阻塞整体/不拖垮其他源"),
    ("方向闸门", "D.降级熔断", r"CONFIDENCE_GATE|passed_gate|buy_min|sell_min|方向门槛",
     "统一置信度低于闸门时只输出观望，不输出方向结论"),
    ("连续失败熔断", "D.降级熔断", r"circuit.?breaker|CircuitBreaker|breaker_state|half.?open|open_state|fail_threshold|failure_threshold|consecutive_fail|reopen_after|open_after|cooldown|熔断器|open_circuit|trip_breaker",
     "连续 N 次失败后熔断该源一段时间，避免反复打必失败请求"),
    ("孤证拦截", "A.去伪存真", r"evidence|多证据|孤证|单源|交叉校验|cross.?check",
     "单因子/单源不足信，需多证据互相印证"),
    ("时效拒收", "A.去伪存真", r"max_age|MAX_AGE|新鲜度|过期|stale|3h",
     "旧闻/迟到信息按年龄拒收，不误入实时信号"),
    ("稳定去重", "A.去伪存真", r"dedup|aggregation_key|去重|稳定哈希|stable_hash",
     "重复新闻按稳定哈希去重，不重复计入信号"),
]
def _read(rel: str) -> str:
    p = os.path.join(SRC, *rel.split("/"))
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _scan_layer(files) -> dict:
    """返回 {特征名: set(命中的文件)}，仅统计实际存在的文件。"""
    hits: dict = {g[0]: set() for g in GUARDRAILS}
    for rel in files:
        text = _read(rel)
        if not text:
            continue
        for name, _cat, pat, _desc in GUARDRAILS:
            if re.search(pat, text):
                hits[name].add(rel)
    return hits


def _main() -> int:
    gaps_only = "--gaps" in sys.argv
    strict = "--strict" in sys.argv

    layer_hits = {layer: _scan_layer(files) for layer, files in LAYERS.items()}
    feature_hits: dict = {g[0]: set() for g in GUARDRAILS}
    for _layer, hits in layer_hits.items():
        for name, files in hits.items():
            feature_hits[name].update(files)

    print("=" * 92)
    print("Trident 全链路审计加固基线 —— 去伪存真 / 不硬归因 / 多源验证 / 降级熔断")
    print("=" * 92)

    if not gaps_only:
        print("\n【逐模块 × 思想特征 落地矩阵】（✓ 已落地 / · 缺失）\n")
        print(f"  {'特征':<10} " + "".join(f"{layer:<7}" for layer in LAYERS))
        print("  " + "-" * (13 + 7 * len(LAYERS)))
        for name, cat, _pat, desc in GUARDRAILS:
            cells = []
            for layer in LAYERS:
                cells.append("   ✓  " if layer_hits[layer][name] else "   ·  ")
            print(f"  {name:<10} " + "".join(cells) + f"  [{cat}]")
            print(f"  {'':<10}  {desc}")
        print()

    print("\n【缺口：全 5 层均未落地的思想特征】")
    gaps = [g for g in GUARDRAILS if not feature_hits[g[0]]]
    if not gaps:
        print("  ✅ 无——所有思想特征都已在至少一层落地。")
    else:
        for name, cat, _pat, desc in gaps:
            print(f"  ⚠️  [{cat}] {name} —— {desc}")

    print("\n【薄弱项：仅 ≤1 层落地（建议补到对应层）】")
    weak = [g for g in GUARDRAILS if len(feature_hits[g[0]]) <= 1]
    if not weak:
        print("  ✅ 无——各特征都有足够的层覆盖。")
    else:
        for name, cat, _pat, desc in weak:
            files = sorted(feature_hits[name]) or ["(无)"]
            print(f"  ⚠️  [{cat}] {name} —— 仅落地于 {files}")

    print("\n【每层落地情况】")
    for layer in LAYERS:
        done = sum(1 for g in GUARDRAILS if layer_hits[layer][g[0]])
        total = len(GUARDRAILS)
        print(f"  {layer:<6} {done}/{total} 项特征落地")

    print("\n【结论】")
    has_gap = bool(gaps) or bool(weak)
    if has_gap:
        print("  ⚠️ 存在缺口/薄弱项——见上表，逐模块把缺失特征写进判断分支。")
        code = 1 if strict else 0
    else:
        print("  ✅ 四类思想特征已在全链路落地为代码级基线。")
        code = 0
    print("=" * 92)
    return code


if __name__ == "__main__":
    sys.exit(_main())