#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chain_ingest_ab.py — 因果链「触发词净化 + 品种绑定」前后对照（只读）

回答一个问题：**修完之后到底改变了什么，还是只是改了个函数？**

口径
----
· 读取一律 `mode=ro`，不写生产库；"修后"是在内存里用同一套纯函数重算。
· 匹配模拟复用 `chain_prior._hit` 的词边界规则 / `chain_keywords` 的系统 token 表，
  对每条链的两个版本分别跑一遍真实头条（news_events 抽样）。
· `纯噪声命中` = 命中的 token 全是系统 token / 品种代码 —— 这种匹配对判断毫无信息，
  却会在 96% 污染的词表下大量出现。

用法
----
    python3 scripts/chain_ingest_ab.py                 # 默认读 config.DB_PATH
    python3 scripts/chain_ingest_ab.py --db /tmp/x.db --per-asset 150 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC_PY = BACKEND / "src_python"
if str(SRC_PY) not in sys.path:
    sys.path.insert(0, str(SRC_PY))

import chain_keywords as ck
import chain_prior

SYSTEM = {t.lower() for t in ck.SYSTEM_TOKENS}
TICKERS = set(ck.ALL_TICKER_ALIASES)

# 修前生产口径：chain_prior 原本自带的通用词表（不含 ai_worker/checklist/partial/
# geopolitical_conflict 这些，这正是漏进来的那批）。修后 = 原表 ∪ 系统 token ∪ 品种别名。
BASELINE_GENERIC = {
    "buy", "sell", "hold", "verdict", "prompt", "asset", "market_event", "event",
    "news", "message", "true", "false", "null",
    "btc", "eth", "sol", "xau", "wti", "nvda", "mu", "ada", "doge", "xrp", "bnb",
    "skhynix", "tsla", "crude", "gold", "silver", "bitcoin", "ethereum", "solana",
    "加密资产", "美股", "个股", "原油", "黄金", "消息", "市场", "行情", "事件",
}
AFTER_GENERIC = BASELINE_GENERIC | SYSTEM | TICKERS


def _open_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_system_token(raw: str) -> bool:
    return any(tok.lower().strip() in SYSTEM for tok in ck.split_tokens(raw))


def _meaningful(tok: str) -> bool:
    low = tok.lower().strip()
    if not low or low in SYSTEM or low in TICKERS:
        return False
    if low.isascii():
        return len(low) >= 4   # 与 bridge 的 meaningful_keyword_hit 同口径
    return len(low) >= 2


def _match(text: str, tokens, blacklist) -> list[str]:
    lower = (text or "").lower()
    hits = []
    for t in tokens:
        t = t.strip()
        if not t or t.lower() in blacklist:
            continue
        if chain_prior._hit(t.lower(), lower):
            hits.append(t)
    return hits


def run(db_path: str, per_asset: int = 150, sample_days: int = 14) -> dict:
    import time
    conn = _open_ro(db_path)
    try:
        chains = [dict(r) for r in conn.execute(
            "SELECT id, name, cause, effect, trigger_words, weight, enabled,"
            " asset, trigger_keywords, expected_direction FROM causal_chains"
        ).fetchall()]
        priced = {r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily").fetchall() if r[0]}

        # ── 头条抽样：每个资产取最近 per_asset 条真实 headline ──────────────
        since = int(time.time()) - sample_days * 86400
        rows = conn.execute(
            "SELECT asset, headline FROM news_events"
            " WHERE headline IS NOT NULL AND headline<>'' AND ts>=?"
            " ORDER BY ts DESC", (since,)).fetchall()
    finally:
        conn.close()

    headlines = defaultdict(list)
    for r in rows:
        a = (r["asset"] or "").upper()
        if len(headlines[a]) < per_asset:
            headlines[a].append(r["headline"])

    # ── 修前 / 修后 两条版本 ────────────────────────────────────────────
    before, after, changes = [], [], []
    for ch in chains:
        raw = ch["trigger_words"] or ""
        b_tokens = ck.split_tokens(raw)
        b_asset = (ch["asset"] or "UNKNOWN").upper()
        res = ck.clean_keywords(raw)
        decision = ck.decide_asset(
            ch["name"] or "", ch["cause"] or "", ch["effect"] or "", raw,
            priced_assets=priced or None,
        )
        a_asset = decision.asset
        before.append({"id": ch["id"], "asset": b_asset, "tokens": b_tokens,
                       "system": _has_system_token(raw), "name": ch["name"]})
        after.append({"id": ch["id"], "asset": a_asset, "tokens": list(res.kept),
                      "system": _has_system_token(res.joined),
                      "strength": decision.strength, "name": ch["name"]})
        if a_asset != b_asset:
            changes.append({"id": ch["id"], "from": b_asset, "to": a_asset,
                            "strength": decision.strength})

    # ── 结构指标 ───────────────────────────────────────────────────────
    def _summary(rows_):
        return {
            "avg_tokens": round(sum(len(r["tokens"]) for r in rows_) / len(rows_), 2),
            "system_token_chains": sum(1 for r in rows_ if r["system"]),
            "unknown_asset": sum(1 for r in rows_ if r["asset"] in ("", "UNKNOWN", "ALL")),
            "zero_token_chains": sum(1 for r in rows_ if not r["tokens"]),
            "asset_dist": dict(Counter(r["asset"] or "UNKNOWN" for r in rows_).most_common(10)),
        }

    # ── 匹配模拟（用真实头条）──────────────────────────────────────────
    pairs_before = pairs_after = noise_before = noise_after = 0
    per_asset_stat = {}
    for asset, items in sorted(headlines.items()):
        if not items:
            continue
        b_chains = [c for c in before if c["asset"] == asset]
        a_chains = [c for c in after if c["asset"] == asset]
        nb, na = 0, 0
        for h in items:
            hb = [_match(h, c["tokens"], BASELINE_GENERIC) for c in b_chains]
            hits_b = [x for x in hb if x]
            nb += len(hits_b)
            for x in hits_b:
                if not any(_meaningful(t) for t in x):
                    noise_before += 1
            ha = [_match(h, c["tokens"], AFTER_GENERIC) for c in a_chains]
            hits_a = [x for x in ha if x]
            na += len(hits_a)
            for x in hits_a:
                if not any(_meaningful(t) for t in x):
                    noise_after += 1
        pairs_before += nb
        pairs_after += na
        per_asset_stat[asset] = {
            "headlines": len(items),
            "chains_before": len(b_chains), "chains_after": len(a_chains),
            "pairs_before": nb, "pairs_after": na,
            "avg_hits_before": round(nb / len(items), 3),
            "avg_hits_after": round(na / len(items), 3),
        }

    return {
        "db": db_path,
        "chains": len(chains),
        "priced_assets": len(priced),
        "before": _summary(before),
        "after": _summary(after),
        "asset_changed": len(changes),
        "asset_newly_bound": sum(1 for c in changes
                                 if c["from"] in ("UNKNOWN", "ALL", "") and c["to"] != "UNKNOWN"),
        "asset_lost": sum(1 for c in changes
                          if c["to"] == "UNKNOWN" and c["from"] not in ("UNKNOWN", "ALL", "")),
        "sample": {
            "assets": len(per_asset_stat),
            "pairs_before": pairs_before, "pairs_after": pairs_after,
            "noise_pairs_before": noise_before, "noise_pairs_after": noise_after,
            "per_asset": per_asset_stat,
        },
        "changes": changes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="默认 config.DB_PATH")
    ap.add_argument("--per-asset", type=int, default=150)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-changes", type=int, default=0, help="打印前 N 条品种变更")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        import config
        db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print(f"[ERR] 库不存在: {db_path}", file=sys.stderr)
        return 2

    res = run(db_path, per_asset=args.per_asset, sample_days=args.days)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0

    b, a, s = res["before"], res["after"], res["sample"]
    print(f"库: {res['db']}  链: {res['chains']}  有行情资产: {res['priced_assets']}")
    print("─" * 78)
    print(f"{'指标':<26}{'修前':>20}{'修后':>20}")
    for key, label in (("avg_tokens", "平均触发词数"),
                       ("system_token_chains", "含系统 token 的链"),
                       ("unknown_asset", "UNKNOWN 资产"),
                       ("zero_token_chains", "无任何关键词（失效）")):
        print(f"{label:<26}{b[key]:>20}{a[key]:>20}")
    print(f"{'品种判定变更':<26}{'':>20}{res['asset_changed']:>20}")
    print(f"{'  · UNKNOWN→有品种':<30}{'':>12}{res['asset_newly_bound']:>20}")
    print(f"{'  · 有品种→UNKNOWN':<30}{'':>12}{res['asset_lost']:>20}")
    print("─" * 78)
    print(f"头条抽样: {s['assets']} 个资产 / 每资产 150 条")
    print(f"{'头条-链 命中配对':<26}{s['pairs_before']:>20}{s['pairs_after']:>20}")
    print(f"{'  · 纯噪声配对':<30}{s['noise_pairs_before']:>12}{s['noise_pairs_after']:>20}")
    print("─" * 78)
    print(f"{'资产':<12}{'头条':>8}{'链(前)':>8}{'链(后)':>8}{'平均命中(前)':>14}{'平均命中(后)':>14}")
    for asset, st in sorted(s["per_asset"].items(), key=lambda kv: -kv[1]["headlines"])[:10]:
        print(f"{asset:<12}{st['headlines']:>8}{st['chains_before']:>8}{st['chains_after']:>8}"
              f"{st['avg_hits_before']:>14}{st['avg_hits_after']:>14}")

    if args.show_changes:
        print("\n品种变更明细（前 N 条）:")
        for c in res["changes"][:args.show_changes]:
            print(f"  #{c['id']} {c['from']} → {c['to']}  ({c['strength']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
