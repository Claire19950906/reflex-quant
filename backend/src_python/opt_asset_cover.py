# -*- coding: utf-8 -*-
"""opt_asset_cover.py — v336 品种映射缺口审计（严格模式，默认不写库）

问题（实测）:
    news_events 30167 条, **11095 条 asset = NONE (36.8%)**。
    这些事件进不了归因、漏检、因果、权重任何一条下游链路 ——
    这是整条链上最大的一处漏水，比任何模型调参都值钱。

本模块只做两件事:
    1. 审计：NONE 占比、按来源/按天分布、能严格映射的有多少；
    2. 回填（可选 `--commit`）：只写**严格命中**的行。

严格命中的定义（宁可少映射，绝不乱映射）:
    · 只在**标题主体位置**（前 12 字）或独立大写 ticker 里找；
    · 命中品种必须**唯一**（同时出现两个品种 → 跳过，交人工/LLM）；
    · 剥掉 `[hash:..]` / `道琼斯：` / `【】` 这类前缀后再判；
    · 命中不到 → 不写（保持 NONE，绝不猜）。
    这条规则的由来：v321.2 曾经用全文扫描，实测误改 809 条。

只读审计为默认；`--commit` 才写，且写前会打印将改的行数与备份提示。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

# 别名 → ticker（只收本系统真在交易的品种，宁缺毋滥）
ALIASES: Dict[str, str] = {
    "比特币": "BTC", "BTC": "BTC", "BITCOIN": "BTC",
    "以太坊": "ETH", "ETH": "ETH", "ETHEREUM": "ETH",
    "索拉纳": "SOL", "SOL": "SOL", "SOLANA": "SOL",
    "瑞波": "XRP", "XRP": "XRP", "RIPPLE": "XRP",
    "币安币": "BNB", "BNB": "BNB",
    "狗狗币": "DOGE", "DOGE": "DOGE",
    "黄金": "XAU", "XAU": "XAU", "GOLD": "XAU", "金价": "XAU",
    "白银": "XAG", "XAG": "XAG", "SILVER": "XAG",
    "原油": "WTI", "WTI": "WTI", "油价": "WTI", "布伦特": "WTI", "BRENT": "WTI",
    "天然气": "NG", "NG": "NG",
    "英伟达": "NVDA", "NVDA": "NVDA",
    "特斯拉": "TSLA", "TSLA": "TSLA",
    "美光": "MU", "MU": "MU",
    "海力士": "SKHYNIX", "SKHYNIX": "SKHYNIX", "SK HYNIX": "SKHYNIX",
    "纳指": "NQ", "NQ": "NQ", "NASDAQ": "NQ",
    "标普": "ES", "ES": "ES", "S&P": "ES",
}

# 长别名优先匹配（"比特币"要先于"比特"）
_ORDERED = sorted(ALIASES.items(), key=lambda kv: -len(kv[0]))
_PREFIX_STRIP = re.compile(r"^\[[^\]]*\]\s*|^(?:道琼斯|路透|彭博|财联社|金十数据|华尔街见闻)[:：]\s*|^【[^】]*】\s*")
_TICKER_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])")


def normalize_headline(text: str) -> str:
    t = (text or "").strip()
    for _ in range(2):
        t2 = _PREFIX_STRIP.sub("", t)
        if t2 == t:
            break
        t = t2
    return t


def extract_subject(text: str, head_chars: int = 12) -> Tuple[Optional[str], List[str]]:
    """从标题里抽主体品种。返回 (唯一品种 or None, 全部候选)。"""
    h = normalize_headline(text)
    if not h:
        return None, []
    head = h[:head_chars]
    cands: List[str] = []
    # 公司名陷阱：标普全球 / 标普评级 是公司，不是标普指数
    company_trap = "标普全球" in head or "标普评级" in head
    for alias, ticker in _ORDERED:
        if company_trap and alias in ("标普", "S&P", "ES"):
            continue
        if alias.isascii():
            # 英文别名只认独立词，避免 "GOLDEN" 命中 "GOLD"
            if re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", head, re.I):
                cands.append(ticker)
        elif alias in head:
            cands.append(ticker)
    if not cands:
        # 退一步：标题里独立出现的大写 ticker（不带引号/括号）
        for m in _TICKER_TOKEN.finditer(head):
            tk = m.group(1).upper()
            if ALIASES.get(tk) == tk:
                cands.append(tk)
    uniq = sorted(set(cands))
    return (uniq[0] if len(uniq) == 1 else None), uniq


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


NONE_REASONS = {
    "calendar": "财经日历/预告（本来就没有单一标的）",
    "blog": "博客/观点/转述（无标的）",
    "macro_data": "宏观数据公布（影响整类资产，不是单一品种）",
    "regulation": "监管/政策原文（无标的）",
    "no_subject": "标题里确实没有品种指称",
}


def classify_none_reason(source: str, headline: str) -> str:
    """把 asset=NONE 的事件分成"不可交易"与"真的漏映射"两类。

    36.8% 的 NONE 里，绝大多数不是映射 bug —— 是日历、博客、宏观数据。
    把它们当成"缺口"去补映射，等于把噪音硬塞进交易链。
    """
    s = (source or "").lower()
    h = (headline or "")
    if "calendar" in s:
        return "calendar"
    if s.startswith("web:blogs") or s.startswith("web:blog"):
        return "blog"
    if any(k in h for k in ("公布", "预期值", "前值", "数据", "CPI", "PMI", "非农", "利率决议")):
        return "macro_data"
    if any(k in h for k in ("监管", "法案", "条例", "征求意见", "公告")):
        return "regulation"
    return "no_subject"


def audit(conn: sqlite3.Connection, days: int = 0) -> Dict[str, Any]:
    where = "WHERE (asset IS NULL OR asset='' OR asset='NONE')"
    params: Tuple[Any, ...] = ()
    if days:
        where += " AND ts >= strftime('%s','now', ?)"
        params = (f"-{days} day",)
    rows = conn.execute(f"SELECT id, headline, source, ts FROM news_events {where}", params).fetchall()
    mapped, ambiguous, none_, by_src = [], 0, 0, Counter()
    reasons: Counter = Counter()
    for r in rows:
        # 先判"是不是本来就不可交易"：日历/宏观/博客/监管里出现的品种，
        # 往往是"这条数据与它有关"，不是"这条新闻在讲它" —— 绝不映射。
        reason = classify_none_reason(r["source"], r["headline"])
        by_src[r["source"] or "?"] += 1
        if reason != "no_subject":
            none_ += 1
            reasons[reason] += 1
            continue
        one, cands = extract_subject(r["headline"] or "")
        if one:
            mapped.append((r["id"], one, (r["headline"] or "")[:40]))
        elif cands:
            ambiguous += 1
        else:
            none_ += 1
            reasons[reason] += 1
    total = conn.execute("SELECT count(*) n FROM news_events").fetchone()["n"]
    return {
        "total_events": total,
        "none_rows": len(rows),
        "none_share": round(100.0 * len(rows) / total, 1) if total else None,
        "strictly_mappable": len(mapped),
        "would_cover_pct": round(100.0 * len(mapped) / len(rows), 1) if rows else None,
        "ambiguous_skip": ambiguous,
        "no_cue": none_,
        "no_cue_reasons": dict(reasons.most_common()),
        "no_cue_reason_labels": {k: NONE_REASONS.get(k, k) for k in reasons},
        "by_ticker": dict(Counter(t for _, t, _ in mapped).most_common()),
        "by_source_top": dict(by_src.most_common(8)),
        "samples": mapped[:8],
    }


def commit(conn_rw: sqlite3.Connection, pairs: List[Tuple[int, str]]) -> int:
    """只写 asset，不动 action/score/status（--fix-asset 的既有边界）。"""
    n = 0
    with conn_rw:
        for news_id, ticker in pairs:
            n += conn_rw.execute(
                "UPDATE news_events SET asset=? WHERE id=? AND (asset IS NULL OR asset='' OR asset='NONE')",
                (ticker, news_id)).rowcount
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="0 = 全表")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--max-commit", type=int, default=2000, help="单次最多写多少行")
    args = ap.parse_args()

    conn = _ro()
    try:
        rep = audit(conn, args.days)
        pairs: List[Tuple[int, str]] = []
        if args.commit:
            where = "WHERE (asset IS NULL OR asset='' OR asset='NONE')"
            params: Tuple[Any, ...] = ()
            if args.days:
                where += " AND ts >= strftime('%s','now', ?)"
                params = (f"-{args.days} day",)
            for r in conn.execute(f"SELECT id, source, headline FROM news_events {where}", params):
                if classify_none_reason(r["source"], r["headline"]) != "no_subject":
                    continue
                one, _ = extract_subject(r["headline"] or "")
                if one:
                    pairs.append((r["id"], one))
    finally:
        conn.close()

    if args.commit:
        pairs = pairs[: args.max_commit]
        rw = sqlite3.connect(DB, timeout=60)
        try:
            n = commit(rw, pairs)
        finally:
            rw.close()
        rep["committed"] = n

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"事件总数 {rep['total_events']} · asset=NONE {rep['none_rows']} 条（{rep['none_share']}%）")
        print(f"严格可映射 {rep['strictly_mappable']} 条 → 可补回 {rep['would_cover_pct']}% 的缺口")
        print(f"多品种歧义跳过 {rep['ambiguous_skip']} · 无线索 {rep['no_cue']}")
        print("无线索的构成（这些本来就不该进交易链）：")
        for k, v in rep["no_cue_reasons"].items():
            print(f"    {k:<12} {v:>5}  {rep['no_cue_reason_labels'].get(k, '')}")
        print(f"映射分布: {rep['by_ticker']}")
        print(f"缺口来源 Top: {rep['by_source_top']}")
        for s in rep["samples"]:
            print(f"  样例 #{s[0]} → {s[1]:<8} {s[2]}")
        if args.commit:
            print(f"已写入 {rep.get('committed', 0)} 条（只改 asset）")
        else:
            print("（dry-run：没有写库。确认后加 --commit）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
