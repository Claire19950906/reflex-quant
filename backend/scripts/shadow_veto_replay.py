#!/usr/bin/env python3
"""v422 · 因果链否决的影子模式回放（不改 engine、不改下单）

背景：`chain_prior.conflict_veto` 在生产代码里**零调用** —— 真正进 prompt 的只有
`inject_block` 的文本，链库对方向几乎没有约束力（v421 实测：1786 次注入只有 88 次
配对到决策）。要接入真否决，会改变真实下单，风险高。

所以先做"只记录不干预"：拿**已经落库的决策**（含 headline、asset、action），
离线跑一遍 `match_by_headline` + `conflict_veto`，把"如果当时接入了否决会怎样"
写进 `causal_veto_shadow`。跑够样本（建议 ≥2 周）再看：
  · 假否决率 = 被否决但事后证明方向正确的比例；
  · 真否决率 = 被否决且事后方向确实反了的比例。
只有假否决率足够低，才值得接进决策路径。

用法：
  python3 scripts/shadow_veto_replay.py                  # dry-run，出 CSV
  python3 scripts/shadow_veto_replay.py --apply          # 写 causal_veto_shadow
"""
from __future__ import annotations

import argparse
import csv
import json
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

BACKEND = os.path.dirname(_HERE)
DB = os.path.join(BACKEND, "trident_event_bus.db")

DDL = """
CREATE TABLE IF NOT EXISTS causal_veto_shadow (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id   INTEGER NOT NULL,
  news_id       INTEGER,
  asset         TEXT,
  action        TEXT,
  headline      TEXT,
  matched_n     INTEGER NOT NULL DEFAULT 0,
  matched_chains TEXT,
  would_veto    INTEGER NOT NULL DEFAULT 0,
  veto_chain_id INTEGER,
  veto_name     TEXT,
  veto_direction TEXT,
  veto_hit_rate REAL,
  veto_lift     REAL,
  veto_keywords TEXT,
  created_at    TEXT,
  UNIQUE(decision_id)
)
"""
# v423: 老库没有 veto_keywords 列 —— 补上，用来留凭证（"凭什么否决"）
_MIGRATE_COLS = (("veto_keywords", "TEXT"),)


def clean_for_match(h: str) -> str:
    """匹配前清掉非正文噪声（v423）。

    news_events.headline 里常混着采集尾巴：`[hash:xxx]` 前缀、`来源:Finnhub`、
    `原文: https://…`、`[来自金十数据快讯]`。这些不是新闻语义，却会被关键词命中
    —— 实测 #12259 就是靠 URL 尾巴里的 "bitcoin-etf" 命中了"ETF 流出"链。
    """
    h = (h or "").strip()
    if h.startswith("[hash:"):
        i = h.find("]")
        if i != -1:
            h = h[i + 1:]
    h = re.sub(r"https?://\S+", " ", h)                     # 链接
    h = re.sub(r"[\[【][^\]】]{0,20}(来源|快讯|来自)[^\]】]{0,30}[\]】]", " ", h)
    for sep in ("来源:", "来源：", "原文链接:", "原文链接：", "原文:", "原文："):
        i = h.find(sep)
        if i >= 0:
            h = h[:i]
    return re.sub(r"\s+", " ", h).strip()


# ── 证据守卫（v424）：关键词命中 ≠ 语义成立 ───────────────────────────
# 清洗词表解决了"泛词乱触发"，但还剩两类关键词层看不到的错配：
# 1) 极性反转：#12010「ETF **终结了**连续的资金**流出**」命中"流出"，被
#    "流出→偏空"链否决，而新闻实际是资金回流（利好）。
# 2) 地缘降温：#11985「卡塔尔外交部…**调解**…与伊朗举行**会谈**」命中"伊朗"，
#    被"冲突升级→避险"链否决，而新闻是降温。
# 守卫只在影子回放里生效（不改生产匹配路径、不影响下单）。
_REVERSAL_MARKERS = (
    "终结", "结束", "终止", "停止", "逆转", "扭转", "放缓", "缓解", "解除",
    "取消", "搁置", "恢复", "达成", "签署", "降温", "缓和", "回落", "收敛",
)
_ESC_MARKERS = (
    "袭击", "爆炸", "开战", "升级", "加剧", "军事行动", "封锁", "空袭",
    "导弹", "伤亡", "宣战", "入侵",
)
_DEESC_MARKERS = (
    "会谈", "谈判", "调解", "停火", "停战", "缓和", "达成协议", "和解",
    "撤军", "解除制裁", "降温",
)
_GEO_CHAIN_HINTS = ("地缘", "冲突", "战争", "袭击", "避险", "军事")
# 子句切分：只看标点，不切空白（中文标题里空格常出现在数字/英文之间）
_CLAUSE_SPLIT = re.compile(r"[，。；、,;!?！？]+")


def _is_reversed(kw: str, headline: str) -> bool:
    """关键词是否处在"反转/否定"语境。

    做法：先去掉空白、按标点切子句，再看关键词所在子句里、关键词之前有没有
    反转词。不用固定字符窗口 —— 实测「终结了 23.5 亿美元的连续的资金流出」
    反转词距关键词 20 字，窗口法抓不到。
    """
    h = re.sub(r"\s+", "", headline.lower())
    k = kw.lower().replace(" ", "")
    if k not in h:
        return False
    for clause in _CLAUSE_SPLIT.split(h):
        if k not in clause:
            continue
        i = clause.find(k)
        if any(m in clause[:i] for m in _REVERSAL_MARKERS):
            return True
    return False


def guard_priors(pri, headline):
    """过滤掉证据不成立的链，返回 (保留的链, 丢弃记录)。"""
    kept, dropped = [], []
    deesc = any(m in headline for m in _DEESC_MARKERS)
    esc = any(m in headline for m in _ESC_MARKERS)
    for p in pri:
        name = p.get("name") or ""
        # 1) 地缘主题链 + 纯降温新闻（无任何升级信号）→ 升级类证据不成立
        if any(x in name for x in _GEO_CHAIN_HINTS) and deesc and not esc:
            dropped.append((name[:20], "geo_deescalation",
                            ",".join(p.get("matched_keywords") or [])))
            continue
        # 2) 命中的词里有处在反转语境的 → 整条链不作为否决依据
        #    （"宁可少否决，不可错否决"：链的机制依赖词的事件极性，极性反了
        #     剩下的实体词（如 "etf"）撑不起方向判断）
        kws = p.get("matched_keywords") or []
        reversed_kws = [k for k in kws if _is_reversed(k, headline)]
        if reversed_kws:
            dropped.append((name[:20], "polarity_reversed", ",".join(reversed_kws)))
            continue
        kept.append(p)
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-guard", dest="guard", action="store_false",
                    help="关闭证据守卫（极性反转/地缘降温），用于对照组")
    ap.set_defaults(guard=True)
    ap.add_argument("--out", default="runtime/shadow_veto.csv")
    ap.add_argument("--db", default=None,
                    help="指定库文件（默认 trident_event_bus.db）。"
                         "库被外部清空时可用备份快照复核历史口径。")
    args = ap.parse_args()
    db_path = args.db or DB
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BACKEND, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        since = int(time.time()) - args.days * 86400
        rows = [dict(r) for r in conn.execute(
            """SELECT d.id, d.news_id, d.target_asset, d.suggested_action,
                      d.is_correct, d.truth_verdict, n.headline
               FROM ai_decisions d
               LEFT JOIN (
                    -- news_events 里同一 news_id 可能有多份转载，必须先去重，
                    -- 否则一条决策会被 JOIN 成多行，否决率被重复计数。
                    -- v423: 原来用 MAX(headline) 去重 —— 那是字典序最大的一条，
                    -- 不一定是最新/最完整的那条，同一决策今天重算可能命中另一份，
                    -- 导致"影子表说否决、重算却否决不出来"（实测 #12259）。
                    -- 改为按 ts 最新、同 ts 按 id 最大取一条，保证回放可复现。
                    SELECT news_id, headline FROM (
                         SELECT news_id, headline,
                                ROW_NUMBER() OVER (
                                    PARTITION BY news_id
                                    ORDER BY ts DESC, id DESC
                                ) AS _rn
                           FROM news_events
                          WHERE headline IS NOT NULL AND TRIM(headline) <> ''
                    ) WHERE _rn = 1
               ) n ON n.news_id = d.news_id
               WHERE d.ts >= ? AND d.suggested_action IN ('BUY','SELL')
               ORDER BY d.ts""",
            (since,),
        ).fetchall()]
        out = []
        for r in rows:
            asset = str(r["target_asset"] or "").strip().upper()
            headline = clean_for_match(str(r["headline"] or ""))
            action = str(r["suggested_action"] or "").upper()
            pri = chain_prior.match_by_headline(conn, asset, headline, limit=5) if asset and headline else []
            dropped = []
            if args.guard and pri:
                pri, dropped = guard_priors(pri, headline)
            veto = chain_prior.conflict_veto(pri, action)
            out.append({
                "decision_id": r["id"], "news_id": r["news_id"], "asset": asset,
                "action": action, "headline": headline,
                "matched_n": len(pri),
                "matched_chains": " | ".join(p["name"][:28] for p in pri[:3]),
                "would_veto": 1 if veto else 0,
                "veto_chain_id": (veto or {}).get("chain_id", ""),
                "veto_name": (veto or {}).get("name", ""),
                "veto_direction": (veto or {}).get("direction", ""),
                "veto_hit_rate": (veto or {}).get("hit_rate", ""),
                "veto_lift": (veto or {}).get("lift", ""),
                "veto_keywords": ",".join((veto or {}).get("matched_keywords") or []),
                "guard_dropped": "; ".join(f"{n}[{w}:{k}]" for n, w, k in dropped),
                "is_correct": r["is_correct"], "truth_verdict": r["truth_verdict"] or "",
            })

        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0].keys()) if out else
                               ["decision_id", "would_veto"])
            w.writeheader()
            w.writerows(out)

        n = len(out)
        v = sum(1 for r in out if r["would_veto"])

        def _win(v) -> bool:
            # is_correct 历史上混着 1/0 与 'WIN'/'LOSS' 两种写法
            return str(v).strip().upper() in ("1", "1.0", "WIN", "TRUE", "YES", "T")

        settled = [r for r in out if str(r["is_correct"]).strip() not in ("", "None")]
        false_veto = sum(1 for r in settled if r["would_veto"] and _win(r["is_correct"]))
        true_veto = sum(1 for r in settled if r["would_veto"] and not _win(r["is_correct"]))
        print(f"近 {args.days} 天 BUY/SELL 决策 {n} 条 → 假想否决 {v} 条 ({v / n:.1%})；明细 {out_path}")
        if settled:
            print(f"  已结算 {len(settled)} 条：其中被否决且方向本是对的（假否决）{false_veto}，"
                  f"方向确实反了（真否决）{true_veto}")
        if not v:
            print("  当前没有任何决策会被否决 —— 说明链库对这批决策没有可用方向证据")
        if not args.apply:
            print("[dry-run] 未写库。确认后加 --apply。")
            return 0
        conn.execute(DDL)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(causal_veto_shadow)")}
        for name, typ in _MIGRATE_COLS:
            if name not in cols:
                conn.execute(f"ALTER TABLE causal_veto_shadow ADD COLUMN {name} {typ}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in out:
            conn.execute(
                "INSERT OR IGNORE INTO causal_veto_shadow"
                " (decision_id, news_id, asset, action, headline, matched_n, matched_chains,"
                "  would_veto, veto_chain_id, veto_name, veto_direction, veto_hit_rate,"
                "  veto_lift, veto_keywords, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["decision_id"], r["news_id"], r["asset"], r["action"], r["headline"],
                 r["matched_n"], r["matched_chains"], r["would_veto"],
                 # 数值列留空串会被 SQLite 当成类型错误 → INSERT OR IGNORE 静默吞掉整行
                 int(r["veto_chain_id"]) if str(r["veto_chain_id"]).strip() else None,
                 r["veto_name"] or None, r["veto_direction"] or None,
                 float(r["veto_hit_rate"]) if str(r["veto_hit_rate"]).strip() else None,
                 float(r["veto_lift"]) if str(r["veto_lift"]).strip() else None,
                 r["veto_keywords"] or None,
                 now),
            )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM causal_veto_shadow").fetchone()[0]
        print(f"已写入（去重）；影子表累计 {total} 行。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
