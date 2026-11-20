#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""classify_orphan_news.py — 给无品种的 PENDING 新闻分「粗桶」(v337)

问题
----
C4 唯一的 FAIL 项是"孤儿事件"：`news_events` 里 PENDING 且有 2,092 条
`asset` 为空/NONE。它们永远处理不了，因为下游按 asset 找模型。

为什么不能硬猜品种
------------------
2,092 条里只有 409 条真的是宏观（美联储/CPI/关税…）。其余是美股个股故事
（SeekingAlpha 的 dividend/stock movers）、小币种（Polkadot 的 dotUSD）。
把 Polkadot 硬塞给 BTC 会污染 BTC 的归因与权重，这比空着更糟。

所以这里只做**粗桶**分类，不做品种猜测：
  · CRYPTO  —— 稳定币/token/链上治理/交易所等，属于加密大类
  · MACRO   —— 央行/通胀/就业/关税/财政/能源政策，属于宏观大类
  · US_STOCK—— 个股/财报/评级/美股指数，属于美股大类
命中不了任何一类的（纯观点、无主体）继续留空——宁可承认"无主"，
也不要造一个假品种出来。

三桶都是 `dim_asset` 里真实存在的类别（US_STOCK 在 market_style_snapshot 里
就是按标的维度采样的一档）。同时写 `asset` 与 `assets` 两列，因为 C4 判的是
`asset`，而前端/归因读的是 `assets`。

用法:
    python3 classify_orphan_news.py --dry-run
    python3 classify_orphan_news.py --commit
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

_CRYPTO = re.compile(
    r"(稳定币|stablecoin|token|链上|onchain|区块链|blockchain|空投|airdrop|"
    r"质押|staking|DeFi|钱包|wallet|交易所|exchange|coinbase|binance|"
    r"Polkadot|DOT|XRP|Ripple|Cardano|ADA|Dogecoin|DOGE|BNB|Solana|Solana|"
    r"矿工|mining|哈希|hashrate|NFT|Web3|以太坊|比特币|Bitcoin|Ethereum|crypto)",
    re.I,
)
_MACRO = re.compile(
    r"(美联储|Fed\b|FOMC|央行|ECB|BOJ|通胀|CPI|PPI|非农|NFP|失业率|就业|"
    r"利率|降息|加息|美债|国债收益率|财政部|Treasury|GDP|PMI|关税|tariff|"
    r"制裁|sanction|OPEC|原油库存|EIA|财政|刺激|紧缩|流动性|liquidity|"
    r"商务部|国务院|发改委|证监会|政策|policy|regulation)",
    re.I,
)
_STOCK = re.compile(
    r"(stock|shares|equity|earnings|财报|营收|净利润|guidance|指引|"
    r"Nasdaq|S&P|S&P 500|Dow|道指|纳指|标普|IPO|上市|评级|目标价|"
    r"stock movers|dividend|股息|回购|并购|M&A|acquisition|merger|"
    r"Analyst|analyst)",
    re.I,
)

_BUCKETS = (("CRYPTO", _CRYPTO), ("MACRO", _MACRO), ("US_STOCK", _STOCK))


def classify(text: str) -> str:
    for name, pat in _BUCKETS:
        if pat.search(text or ""):
            return name
    return ""


def run(commit: bool = False, limit: int = 0) -> dict:
    conn = db.get_connection()
    stats = {"scanned": 0, "assigned": 0, "by_bucket": {}, "still_orphan": 0, "orphan_before": 0}
    try:
        cur = conn.cursor()
        stats["orphan_before"] = cur.execute(
            "SELECT COUNT(*) FROM news_events WHERE status='PENDING' "
            "AND (asset IS NULL OR asset='' OR asset='NONE')"
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT id, headline, body, assets FROM news_events
                WHERE status='PENDING' AND (asset IS NULL OR asset='' OR asset='NONE')
                ORDER BY ts DESC"""
        ).fetchall()
        if limit:
            rows = rows[:limit]
        for nid, headline, body, assets in rows:
            stats["scanned"] += 1
            bucket = classify((headline or "") + " " + (body or "")[:400])
            if not bucket:
                stats["still_orphan"] += 1
                continue
            stats["assigned"] += 1
            stats["by_bucket"][bucket] = stats["by_bucket"].get(bucket, 0) + 1
            if commit:
                cur.execute(
                    "UPDATE news_events SET asset=?, assets=? WHERE id=?",
                    (bucket, str(assets or bucket), nid),
                )
                if stats["assigned"] % 500 == 0:
                    conn.commit()
        if commit:
            conn.commit()
            stats["orphan_after"] = cur.execute(
                "SELECT COUNT(*) FROM news_events WHERE status='PENDING' "
                "AND (asset IS NULL OR asset='' OR asset='NONE')"
            ).fetchone()[0]
    finally:
        conn.close()
    return stats


def archive_stale(commit: bool = False, hours: int = 24) -> dict:
    """把超过 N 小时仍未处理的 PENDING 事件置为终态 FILTERED（超龄归档）。

    语义与 raw_news 的 'FILTERED' 一致：超过交易时效、系统主动不再分析。
    这类事件在 LLM 熔断期间会无限堆积，留在 PENDING 只会让"积压"这个指标失真。
    """
    conn = db.get_connection()
    st = {"cutoff_hours": hours, "archived": 0}
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM news_events WHERE status='PENDING' AND ts < ?",
            (int(__import__("time").time()) - hours * 3600,),
        ).fetchone()[0]
        st["archivable"] = n
        if commit and n:
            conn.execute(
                "UPDATE news_events SET status='FILTERED' WHERE status='PENDING' AND ts < ?",
                (int(__import__("time").time()) - hours * 3600,),
            )
            conn.commit()
            st["archived"] = n
    finally:
        conn.close()
    return st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--archive-hours", type=int, default=0,
                    help=">0 时把超龄 PENDING 归档为 FILTERED（仅与 --commit 同用生效）")
    a = ap.parse_args()
    if a.archive_hours > 0:
        st = archive_stale(commit=a.commit, hours=a.archive_hours)
        print(f"[archive-stale] hours={st['cutoff_hours']} archivable={st['archivable']} "
              f"archived={st['archived']}")
    out = run(commit=a.commit, limit=a.limit)
    print(f"[orphan-classify] scanned={out['scanned']} assigned={out['assigned']} "
          f"by_bucket={out['by_bucket']} still_orphan={out['still_orphan']} commit={a.commit}")
    print(f"  orphans: {out['orphan_before']} → {out.get('orphan_after', '(dry)')}")
