#!/usr/bin/env python3
"""影子否决「配对审计」—— 只读，不写库、不改下单

目的：shadow_veto_replay 只记录了"会不会被否决"，没记录"凭什么否决"。
本脚本把每条假想否决的**命中关键词 / 链的触发词 / 链主题**摊开来，
用于人工判断"新闻主题"和"链主题"是否真的对得上（质量审计，不看条数）。

用法：
  python3 scripts/shadow_veto_audit.py             # 只审计影子表里 would_veto=1 的行
  python3 scripts/shadow_veto_audit.py --days 30   # 改为按 ai_decisions 时间窗现算（不依赖影子表）
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import chain_prior

sys.path.insert(0, _HERE)
from shadow_veto_replay import clean_for_match, guard_priors

BACKEND = os.path.dirname(_HERE)
DB = os.path.join(BACKEND, "trident_event_bus.db")
OUT = os.path.join(BACKEND, "runtime", "shadow_veto_audit.csv")

WIN_TOKENS = ("1", "1.0", "WIN", "TRUE", "YES", "T")


def clean_headline(h: str) -> str:
    """去掉采集尾巴（[hash:] 前缀、链接、来源/原文），只留正文。

    必须与 shadow_veto_replay.clean_for_match 保持同一口径，否则审计重算
    和回放结果会对不上。
    """
    h = (h or "").strip()
    if h.startswith("[hash:"):
        i = h.find("]")
        if i != -1:
            h = h[i + 1:].strip()
    h = re.sub(r"https?://\S+", " ", h)
    h = re.sub(r"[\[【][^\]】]{0,20}(来源|快讯|来自)[^\]】]{0,30}[\]】]", " ", h)
    for sep in ("来源:", "来源：", "原文链接:", "原文链接：", "原文:", "原文："):
        i = h.find(sep)
        if i >= 0:
            h = h[:i]
    return re.sub(r"\s+", " ", h).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0,
                    help="0=审计影子表已有否决行；>0=按决策时间窗现算")
    ap.add_argument("--out", default=None)
    ap.add_argument("--db", default=None, help="指定库文件（默认 trident_event_bus.db）")
    args = ap.parse_args()
    db_path = args.db or DB
    out_path = args.out or OUT
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        if args.days > 0:
            since = int(time.time()) - args.days * 86400
            rows = conn.execute(
                """SELECT d.id AS decision_id, d.target_asset, d.suggested_action,
                          d.is_correct, d.ts, n.headline
                   FROM ai_decisions d
                   LEFT JOIN (SELECT news_id, headline FROM (
                                  SELECT news_id, headline,
                                         ROW_NUMBER() OVER (
                                             PARTITION BY news_id
                                             ORDER BY ts DESC, id DESC) AS _rn
                                    FROM news_events
                                   WHERE headline IS NOT NULL AND TRIM(headline) <> ''
                              ) WHERE _rn = 1) n
                     ON n.news_id = d.news_id
                   WHERE d.ts >= ? AND d.suggested_action IN ('BUY','SELL')
                   ORDER BY d.ts""", (since,)).fetchall()
            mode_note = f"现算近 {args.days} 天"
        else:
            rows = conn.execute(
                """SELECT s.decision_id, s.asset AS target_asset, s.action AS suggested_action,
                          d.is_correct, d.ts, s.headline
                   FROM causal_veto_shadow s
                   LEFT JOIN ai_decisions d ON d.id = s.decision_id
                   WHERE s.would_veto = 1
                   ORDER BY d.ts""").fetchall()
            mode_note = "审计影子表已否决行"

        audit = []
        for r in rows:
            asset = str(r["target_asset"] or "").strip().upper()
            action = str(r["suggested_action"] or "").strip().upper()
            headline = clean_for_match(str(r["headline"] or ""))
            pri = chain_prior.match_by_headline(conn, asset, headline, limit=5) if asset and headline else []
            pri, dropped = guard_priors(pri, headline)
            veto = chain_prior.conflict_veto(pri, action)
            if args.days > 0 and not veto:
                continue
            vchain = (veto or {}).get("chain_id")
            # 链的原始触发词（判断"链为什么会被这条新闻触发"）
            trig = ""
            if vchain:
                tr = conn.execute(
                    "SELECT trigger_words, trigger_keywords, mechanism FROM causal_chains"
                    " WHERE chain_id = ? LIMIT 1", (vchain,)).fetchone()
                if tr:
                    trig = " / ".join(x for x in [
                        str(tr["trigger_words"] or ""), str(tr["trigger_keywords"] or "")
                    ] if x)
            ic = str(r["is_correct"] or "").strip()
            settled = ic not in ("", "None")
            win = ic.upper() in WIN_TOKENS
            audit.append({
                "decision_id": r["decision_id"],
                "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(r["ts"] or 0))),
                "asset": asset,
                "action": action,
                "settled": "Y" if settled else "N",
                "outcome": ("WIN" if win else "LOSS") if settled else "",
                "headline": headline[:90],
                "matched_n": len(pri),
                "veto_chain_id": vchain or "",
                "veto_chain": (veto or {}).get("name", "")[:40],
                "veto_direction": (veto or {}).get("direction", ""),
                "veto_hit_rate": (veto or {}).get("hit_rate", ""),
                "chain_trigger_words": trig[:120],
                "matched_keywords": " | ".join((veto or {}).get("matched_keywords") or []),
                "guard_dropped": " ; ".join(f"{n}[{w}:{k}]" for n, w, k in dropped),
                "all_chains": " ; ".join(
                    f"{p['name'][:18]}({p.get('direction')})[{','.join(p.get('matched_keywords') or [])}]"
                    for p in pri),
            })

        fields = list(audit[0].keys()) if audit else ["decision_id"]
        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(audit)

        print(f"[{mode_note}] 审计 {len(audit)} 条否决样本 → {out_path}")
        if audit:
            print("\n凭证速览（看 matched_keywords 与 chain_trigger_words 是否同一主题）：")
            for a in audit:
                print(f"  #{a['decision_id']} {a['asset']} {a['action']} "
                      f"结果={a['outcome'] or '未结算'}")
                print(f"      新闻: {a['headline']}")
                print(f"      否决链: {a['veto_chain']} (方向 {a['veto_direction']}"
                      f", 命中率 {a['veto_hit_rate']})")
                print(f"      命中词: {a['matched_keywords']}")
                print(f"      链触发词: {a['chain_trigger_words']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
