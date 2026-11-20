"""evidence_source.py —— 归因的"证据来源"分层（v336 P1-1）

问题（2026-11-14 只读实测 event_move_attribution 242 行）：
  标题主语 = 标的           84 条  (34.7%)  ← 真命中
  标题主语是别的品种         41 条  (16.9%)  ← 跨品种传导
  标题里没有主语(宏观/汇总) 117 条  (48.3%)  ← 只能算背景，不能算"原因"

  系统把这三类都记成同一种"归因"，score 只差几个小数。于是正文里出现一句
  "BTC 报价 xxx" 就能把一条完全无关的新闻挂成 BTC 暴跌的原因
  （干跑实测：`BTC -7.56%` ← 「艾罗能源拟每10股派31.25元」score=0.798）。

本模块只做一件事：给每行归因标出证据强度，**不改变任何筛选/打分逻辑**。
四级（从强到弱）：
  headline    标题主语就是这个标的          → 可直接用于因果学习
  cross_asset 标题点了这个标的但主语是别的  → 传导链，需另行验证
  body_only   只有正文提到（正文常夹带行情）→ 噪音高，降权/单列
  macro       正文标题都没提到              → 宏观背景，不构成单一原因
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from opt_asset_cover import ALIASES, extract_subject
except Exception:
    ALIASES = {}
    def extract_subject(text: str, head_chars: int = 12):  # type: ignore
        return None, []

HEADLINE = "headline"
CROSS_ASSET = "cross_asset"
BODY_ONLY = "body_only"
MACRO = "macro"
LEVELS = (HEADLINE, CROSS_ASSET, BODY_ONLY, MACRO)


def mentions(text: str, asset: str) -> bool:
    """文本里是否出现该品种的别名（英文别名按独立词匹配）。"""
    if not text:
        return False
    for alias, ticker in ALIASES.items():
        if ticker != asset:
            continue
        if alias.isascii():
            if re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.I):
                return True
        elif alias in text:
            return True
    return False


def classify_evidence(headline: str, body: str, asset: str) -> str:
    """判定这条归因的证据强度（四级之一）。"""
    strict = str(asset or "").strip().upper()
    head = headline or ""
    subj, cands = extract_subject(head) if head else (None, [])
    if subj == strict:
        return HEADLINE
    # 标题点的是**别的**品种（如"比特币跌破77000"挂在 SOL 上）—— 这是传导链。
    # 注意：不能要求"标题必须提到本标的"才算传导，那样会把跨品种驱动全归成 macro。
    if subj or cands:
        return CROSS_ASSET
    if mentions(head, strict):
        return CROSS_ASSET
    if mentions(body or "", strict):
        return BODY_ONLY
    return MACRO


def tag_reason(reasons: str, level: str) -> str:
    """把 evidence=<level> 追进 algo_reasons（幂等：已标过就替换）。"""
    r = str(reasons or "")
    r = re.sub(r";?\s*evidence=[a-z_]+", "", r).strip(" ;")
    return (r + "; evidence=" + level).lstrip("; ").strip()


def read_tag(reasons: str) -> Optional[str]:
    m = re.search(r"evidence=([a-z_]+)", str(reasons or ""))
    return m.group(1) if m else None


def summarize(levels: Iterable[str]) -> Dict[str, int]:
    out = {k: 0 for k in LEVELS}
    for lv in levels:
        if lv in out:
            out[lv] += 1
    out["total"] = sum(out[k] for k in LEVELS)
    return out


def fetch_news_ctx(conn, ids: Sequence[int]) -> Dict[int, Tuple[str, str]]:
    """批量取 headline/body（只取用到的 id，避免把 3 万条 body 读进内存）。"""
    ctx: Dict[int, Tuple[str, str]] = {}
    ids = [int(i) for i in ids if i]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("SELECT id, headline, body FROM news_events WHERE id IN (%s)"
             % ",".join("?" * len(chunk)))
        try:
            for r in conn.execute(q, chunk):
                ctx[int(r[0])] = (r[1] or "", r[2] or "")
        except Exception:
            continue
    return ctx
