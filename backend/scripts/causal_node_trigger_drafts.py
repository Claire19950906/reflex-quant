#!/usr/bin/env python3
"""给 99 条 annotation（注释类）节点起草"待人工确认"的建链素材。

背景：`causal_node_chain_backfill.py` 实测可建链 0 条，因为这批节点没有触发词表。
本脚本不写库、不建链，只输出一份人可勾选的清单：
  · 每条给 3 个候选触发词（取自新闻标题里真实在用的术语，且必须在该节点文本中出现）；
  · 方向候选（`_infer_expect_direction` 的推断，推不出写"待定"）；
  · 前置条件留空给人填。

用法：python3 scripts/causal_node_trigger_drafts.py [--db ...]
输出：runtime/node_backfill/node_trigger_drafts_<时间>.csv
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_graph as cg

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_DB = os.path.join(BASE, "trident_event_bus.db")
OUT_DIR = os.path.join(BASE, "runtime", "node_backfill")

_CJK = r"\u4e00-\u9fa5"

# 这些片段是"新闻里常见但不是术语"，挖出来给人勾只会耗人工
_JUNK = re.compile("美元|亿元|万亿|%|释放|上升|下降|意味|数十|多个|报道|消息|表示|认为|"
                   "我们|他们|今日|昨日|本周|上周|今年|去年|盘中|收盘|开盘|涨跌|上涨|下跌|"
                   "的交易|分钟内|的|向市场|油官方|这一|那|其|并|则|就|都|也|还")


def news_vocab(conn, min_count=8):
    """从新闻标题挖真实术语：ASCII 词（OPEC/ETF/FOMC…）+ 中文 2~6 字 n-gram。"""
    counts = Counter()
    for (title,) in conn.execute("SELECT headline FROM news_events WHERE headline IS NOT NULL"):
        t = str(title)
        for word in re.findall(r"[A-Za-z][A-Za-z+]{1,9}", t):
            counts[word.upper()] += 1
        for chunk in re.split(r"[^%s]+" % _CJK, t):
            for n in range(2, 7):
                for i in range(len(chunk) - n + 1):
                    counts[chunk[i:i + n]] += 1
    return {g: c for g, c in counts.items() if c >= min_count and len(g) >= 2}


def pick_candidates(text, vocab, chain_vocab, limit=3):
    """挑"在这段文本里真实出现"的术语：链库在用词优先，其余按长度。"""
    hits = []
    for g, c in vocab.items():
        if g not in text or _JUNK.search(g):
            continue
        hits.append((1 if g in chain_vocab else 0, len(g), c, g))
    hits.sort(reverse=True)
    chosen = []
    for _cv, _n, _c, g in hits:
        if any(g in other or other in g for other in chosen):
            continue
        chosen.append(g)
        if len(chosen) >= limit:
            break
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row

    vocab = news_vocab(conn)
    # 链库在用触发词 = 被验证过"能匹配上新闻"的词，优先级最高
    chain_vocab = {}
    for r in conn.execute("SELECT trigger_words FROM causal_chains"):
        for w0 in re.split(r"[,，\s|]+", str(r["trigger_words"] or "")):
            w0 = w0.strip()
            if 2 <= len(w0) <= 12:
                chain_vocab[w0] = chain_vocab.get(w0, 0) + 1
    vocab.update(chain_vocab)

    # 各品种在用的触发词，候选不足时兜底（这些词链库至少认得出）
    asset_words = {}
    for r in conn.execute("SELECT asset, trigger_words FROM causal_chains WHERE enabled=1"):
        a = str(r["asset"] or "").upper()
        for w0 in re.split(r"[,，\s|]+", str(r["trigger_words"] or "")):
            w0 = w0.strip()
            if 2 <= len(w0) <= 12:
                asset_words.setdefault(a, Counter())[w0] += 1

    rows = conn.execute(
        "SELECT id, asset, label, evidence FROM asset_driver_nodes "
        "WHERE active=1 AND kind='condition' ORDER BY asset, id").fetchall()

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "node_trigger_drafts_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["节点id", "品种", "节点标签", "机制摘要（前 60 字）",
                    "候选触发词1", "候选触发词2", "候选触发词3",
                    "方向候选", "前置条件（待填）", "备注"])
        n_has = n_dir = 0
        for r in rows:
            ev = cg._parse_evidence(r["evidence"] or "")
            label = str(r["label"] or "")
            chain = [str(x) for x in (ev.get("chain") or []) if str(x).strip()]
            body = " ".join(chain)
            text = label + " " + body

            cands = pick_candidates(text, vocab, chain_vocab)
            fill = [x for x, _ in (asset_words.get(str(r["asset"] or "").upper()) or Counter()).most_common(6)
                    if x not in cands]
            filled = []
            while len(cands) + len(filled) < 3 and fill:
                filled.append(fill.pop(0))
            n_from_text = len(cands)
            cands = (cands + filled + ["", "", ""])[:3]

            exp = cg._infer_expect_direction(chain, label, str(r["asset"] or ""))
            direction = {"up": "看涨", "down": "看跌"}.get(exp, "待定")
            if direction != "待定":
                n_dir += 1

            notes = []
            if filled:
                notes.append("第 %d 个起为品种兜底词（非文本挖出）" % (n_from_text + 1))
            if not cands[0]:
                notes.append("无候选，须人工补")
            if direction == "待定":
                notes.append("方向推不出，须人工定")

            w.writerow([r["id"], r["asset"], label[:40], body[:60],
                        cands[0], cands[1], cands[2], direction, "", "；".join(notes)])
            if n_from_text:
                n_has += 1

    print("[drafts] 词表 %d 个术语（来自新闻标题）" % len(vocab))
    print("[drafts] 节点 %d 条：文本挖出候选 %d，方向可推 %d" % (len(rows), n_has, n_dir))
    print("[drafts] 清单: %s" % path)
    conn.close()


if __name__ == "__main__":
    main()
