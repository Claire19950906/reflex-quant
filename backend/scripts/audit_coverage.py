#!/usr/bin/env python3
"""v420 · 因果图谱品种覆盖审计（只读）

问题：链库只覆盖极少数品种（实测 XAU 235 / WTI 180 / BTC 31 / ETH 24 / SKHYNIX 2 / SOL 1），
但行情库有 36+ 品种、新闻库每天在产美股/个股新闻。对没链的品种，`inject_block`
只能输出"本品种 0 条链跑赢基准，勿据之以定向" —— 等于图谱在这些品种上是**瞎的**。

本脚本按品种把三件事对齐：有没有行情、有没有新闻、有没有能用的链（asset 正确 + 有触发词 + 启用），
再补上注入记录，输出一张"哪里瞎了"的表。只读，不改库。

status 口径：
  covered       启用链 ≥1 且存在样本（能进入证据层）
  thin          启用链 ≥1 但都没样本（有链未验证）
  blind_news    有新闻（近 30 天 ≥1 条）但一条可用链都没有  ← 最该补的
  blind_quiet   有行情有新闻历史，但近 30 天无新闻
  price_only    有行情，无任何新闻
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BACKEND, "trident_event_bus.db")

DEAD_ASSETS = {"", "UNKNOWN", "NONE", "NULL", "MACRO"}
# 非 ticker 的"资产分类标签"（新闻里常见），不该当成品种统计
LABELS = {"NONE", "MACRO", "CRYPTO", "US_STOCK", "INDIVIDUAL_STOCK", "CN_STOCK",
          "A_STOCK", "个股", "美股", "原油", "黄金", "加密主流", "山寨", ""}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _universe(conn) -> set[str]:
    """品种全集 = 有行情的 ∪ 有新闻的 ∪ 有链的。"""
    out: set[str] = set()
    for sql in (
        "SELECT DISTINCT asset FROM ohlcv_daily",
        "SELECT DISTINCT asset FROM news_events",
        "SELECT DISTINCT asset FROM causal_chains",
    ):
        for r in conn.execute(sql):
            a = str(r[0] or "").strip().upper()
            if a and a not in LABELS:
                out.add(a)
    return out


def build(conn, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    uni = sorted(_universe(conn))

    news_all, news_win = {}, {}
    for r in conn.execute("SELECT asset, COUNT(*) n, MAX(ts) last FROM news_events GROUP BY asset"):
        news_all[str(r["asset"] or "").strip().upper()] = (r["n"], r["last"])
    for r in conn.execute("SELECT asset, COUNT(*) n FROM news_events WHERE ts >= ? GROUP BY asset", (since,)):
        news_win[str(r["asset"] or "").strip().upper()] = r["n"]

    price = {str(r["asset"]).strip().upper(): r["n"]
             for r in conn.execute("SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset")}

    chains_total, chains_ok, chains_sampled = {}, {}, {}
    for r in conn.execute(
        """SELECT asset, COUNT(*) n,
                  SUM(CASE WHEN COALESCE(enabled,0)=1 AND COALESCE(active,0)=1
                            AND COALESCE(trigger_words,'')<>'' THEN 1 ELSE 0 END) usable
           FROM causal_chains GROUP BY asset"""
    ):
        a = str(r["asset"] or "").strip().upper()
        chains_total[a] = r["n"]
        chains_ok[a] = int(r["usable"] or 0)
    for r in conn.execute(
        """SELECT c.asset, COUNT(o.id) n FROM causal_chain_outcomes o
           JOIN causal_chains c ON CAST(c.id AS TEXT)=o.chain_id
           GROUP BY c.asset"""
    ):
        chains_sampled[str(r["asset"] or "").strip().upper()] = r["n"]

    inject = {str(r["asset"]).strip().upper(): r["n"]
              for r in conn.execute("SELECT asset, COUNT(*) n FROM causal_injection_log GROUP BY asset")}

    rows = []
    for a in uni:
        n_news, last = news_all.get(a, (0, None))
        n_win = news_win.get(a, 0)
        n_px = price.get(a, 0)
        tot, ok, smp = chains_total.get(a, 0), chains_ok.get(a, 0), chains_sampled.get(a, 0)
        if ok and smp:
            status = "covered"
        elif ok:
            status = "thin"
        elif n_win:
            status = "blind_news"
        elif n_news or n_px:
            status = "blind_quiet"
        else:
            status = "price_only"
        rows.append({
            "asset": a, "status": status,
            "chains_total": tot, "chains_usable": ok, "outcomes_n": smp,
            "news_total": n_news, "news_30d": n_win,
            "news_last": time.strftime("%Y-%m-%d", time.localtime(last)) if last else "",
            "ohlcv_days": n_px, "inject_n": inject.get(a, 0),
        })
    order = {"blind_news": 0, "covered": 1, "thin": 2, "blind_quiet": 3, "price_only": 4}
    rows.sort(key=lambda r: (order[r["status"]], -r["news_30d"], -r["ohlcv_days"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runtime/coverage_audit.csv")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BACKEND, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = _conn()
    try:
        rows = build(conn, args.days)
    finally:
        conn.close()

    fields = ["status", "asset", "chains_total", "chains_usable", "outcomes_n",
              "news_total", "news_30d", "news_last", "ohlcv_days", "inject_n"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"品种 {len(rows)} 个 → {out_path}")
    for st in ("covered", "thin", "blind_news", "blind_quiet", "price_only"):
        print(f"  {st:12s} {counts.get(st, 0)}")
    print("\n最该补（有新闻、零可用链）前 12：")
    for r in [x for x in rows if x["status"] == "blind_news"][:12]:
        print(f"  {r['asset']:<10} 近{r['days'] if False else 30}天新闻 {r['news_30d']:>4} 条"
              f" | 累计 {r['news_total']:>5} | 行情 {r['ohlcv_days']:>4} 天"
              f" | 注入记录 {r['inject_n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
