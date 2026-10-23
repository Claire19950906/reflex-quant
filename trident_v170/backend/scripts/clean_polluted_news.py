#!/usr/bin/env python3
"""清洗被 MiniMax-M3 ` thinking` 标签污染的英文新闻。

背景：翻译模块（webhook._translate_title_sync）在 MiniMax-M3 未禁用 thinking 时，
会把 `<thinking>The user wants me to translate...` 整段混进翻译正文，导致 raw_news.content
变成 "thinking 段 + 英文标题 + 半截中文翻译" 的污染文本，下游主分析拿到无意义内容
→ 空 reasoning → 服务端兜底。

用法：
    python3 clean_polluted_news.py --dry-run    # 只打印清洗预览，不写库
    python3 clean_polluted_news.py              # 实际清洗 + 重置 PENDING 重新分析
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

DB = str(Path(__file__).resolve().parents[1] / "trident_event_bus.db")

# thinking 标签：MiniMax-M3 输出 `<thinking>...</thinking>`（开 `<think>`，闭 `</think>`）
THINK_OPEN = "<think"
THINK_CLOSE = "</think"
HASH_RE = re.compile(r"^(\[hash:[0-9a-f]{16,40}\]\s*)")
EN_TITLE_RE = re.compile(r'"([^"\r\n]{8,400})')
ZH_RE = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff0-9%+．.\s，。：；、（）()\-—]{4,}")
SRC_RE = re.compile(r"(原文|来源|快讯)")
# thinking 残文特征（说明翻译被 256 token 截断，无实质内容）
GARBAGE_RE = re.compile(r"^\s*(user wants|i can|i am|let me|here|sure|certainly|the user|translate)", re.IGNORECASE)


def clean_content(content: str) -> str:
    """从污染 content 里萃取可用正文：优先中文翻译，否则英文标题，去掉 thinking 段。"""
    hash_prefix = ""
    mh = HASH_RE.match(content)
    if mh:
        hash_prefix = mh.group(1)
        content = content[mh.end():]

    # 剥离 thinking 段：`<thinking>` ... `</thinking>`（或截断时只有开标签）
    ti = content.find(THINK_OPEN)
    if ti >= 0:
        tc = content.find(THINK_CLOSE, ti + 4)
        if tc >= 0:
            content = content[tc + len("</think>"):]  # 闭标签之后是真实翻译
        else:
            content = content[ti + len("<think>"):]   # 无闭标签（截断），跳开标签，取残文

    en_title = ""
    m = EN_TITLE_RE.search(content)
    if m:
        en_title = m.group(1).strip()

    zh_blocks = ZH_RE.findall(content)
    zh_longest = max(zh_blocks, key=lambda s: len(s.strip())).strip() if zh_blocks else ""

    src_lines = [l.strip() for l in content.split("\n") if SRC_RE.search(l)]
    src = src_lines[-1] if src_lines else ""

    # 组合正文：中文 >= 8 字符优先（完整翻译），否则英文原标题，否则去掉 thinking 后的残文
    if len(zh_longest) >= 8:
        body = zh_longest
    elif en_title:
        body = en_title
    else:
        body = re.sub(r"\s+", " ", content).strip()[:200]

    if src and src not in body:
        body = f"{body}\n{src}"

    # 若仍是 thinking 残文（翻译被截断、无实质），返回空 → 调用方标记 is_noise
    if GARBAGE_RE.match(body) and len(zh_longest) < 8 and not en_title:
        return ""

    return hash_prefix + body.strip()[:1000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content FROM raw_news WHERE instr(content, ?) > 0 ORDER BY id", (THINK_OPEN,)
    ).fetchall()
    print(f"发现 {len(rows)} 条被 `<thinking>` 污染的新闻\n")

    changed = 0
    noised = 0
    for r in rows:
        nid, old = r["id"], r["content"]
        new = clean_content(old)
        if new == old:
            continue
        print(f"#{nid}")
        print(f"  旧: {old[:80]!r}")
        if not new:
            # 翻译被彻底截断、无实质内容 → 标记为噪声，不再分析
            noised += 1
            print(f"  [NOISE] 无法恢复实质内容，标记 is_noise=1")
            if not args.dry_run:
                conn.execute("UPDATE raw_news SET is_noise=1, status='DONE' WHERE id=?", (nid,))
            continue
        changed += 1
        print(f"  新: {new[:80]!r}")
        if not args.dry_run:
            # 重置为 PENDING 重新分析，并清除旧的空分析记录
            conn.execute(
                "UPDATE raw_news SET content=?, is_noise=0, status='PENDING', retry_count=0, processing_at=NULL WHERE id=?",
                (new, nid),
            )
            conn.execute(
                "DELETE FROM ai_decisions WHERE news_id=? AND (reasoning LIKE '%服务端兜底%' OR reasoning='') AND entry_price IS NULL",
                (nid,),
            )

    if not args.dry_run:
        conn.commit()
        print(f"\n已清洗并重置 {changed} 条，标记噪声 {noised} 条。")
    else:
        print(f"\n[dry-run] {changed} 条将被清洗重置，{noised} 条标记噪声。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())