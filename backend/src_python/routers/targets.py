# -*- coding: utf-8 -*-
"""标的路由诊断端点（v422，只读 + 一个可写别名文件）。

回答一个一直没被量化的问题：**那些 routing_miss 的新闻，到底提到了什么品种？**
11-18 实测：一天 228 条决策里 115 条是 routing_miss（"看懂了但没落到品种"），
这行决策不可能变成交易，信号数自然上不去。

端点：
  GET  /api/targets/unresolved  未识别 TOP 榜（按品种聚合 + 样本 + 上限估计）
  GET  /api/targets/aliases     当前生效的别名表（默认表 + 配置文件）
  POST /api/targets/aliases     往 config/target_aliases.json 追加别名（不写生产库）

只读连接（mode=ro）——除别名 JSON 外不产生任何写入。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, List

from fastapi import APIRouter, Query
from pydantic import BaseModel

import db
import target_resolver

router = APIRouter()

_ALIAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "target_aliases.json"
)


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_unresolved(conn: sqlite3.Connection, days: int, limit: int) -> List[tuple]:
    """捞「没落到品种」的新闻原文。

    两个来源，口径不同、都算未识别：
      1) ai_decisions.decision_kind='routing_miss'（模型自己判 NONE）
      2) unclassified_events 里 status='pending'（清洗层判 MISSING_TARGET_ASSET）
    每条只取一次（按 news_id 去重，先到先得），避免同一条新闻被两个来源重复计数。
    """
    since = int(time.time()) - days * 86400
    seen: set[int] = set()
    rows: List[tuple] = []

    cur = conn.execute(
        """
        SELECT d.news_id, COALESCE(n.source, ''), COALESCE(n.content, ''),
               COALESCE(datetime(d.ts, 'unixepoch', 'localtime'), '')
        FROM ai_decisions d
        LEFT JOIN raw_news n ON n.id = d.news_id
        WHERE d.decision_kind = 'routing_miss' AND d.ts >= ?
        ORDER BY d.ts DESC
        LIMIT ?
        """,
        (since, limit),
    )
    for r in cur.fetchall():
        nid = r[0]
        if nid in seen:
            continue
        seen.add(nid)
        rows.append((nid, r[1], r[2], r[3]))
    cur.close()

    cur = conn.execute(
        """
        SELECT u.news_id, COALESCE(n.source, ''), COALESCE(n.content, ''),
               COALESCE(u.created_at, '')
        FROM unclassified_events u
        LEFT JOIN raw_news n ON n.id = u.news_id
        WHERE u.status = 'pending' AND COALESCE(u.reason, '') LIKE 'MISSING_TARGET%'
        ORDER BY u.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    for r in cur.fetchall():
        nid = r[0]
        if nid in seen or not nid:
            continue
        seen.add(nid)
        rows.append((nid, r[1], r[2], r[3]))
    cur.close()
    return rows


_TARGET_REASON = {
    "XAU": "黄金是最常被写成中文的品种：新闻用「金价/黄金」而枚举只有 XAU",
    "XAG": "白银同理，枚举只有 XAG，原文写「银价」",
    "WTI": "原油：枚举只有 WTI，原文写「油价/布伦特/OPEC」",
    "BTC": "比特币/BTC 混用",
    "ETH": "以太坊/ETH 混用",
    "NVDA": "英伟达常以中文名出现，枚举里只有 ticker NVDA",
    "SKHYNIX": "海力士常写成「SK海力士」，枚举里是 SKHYNIX",
    "TSMC": "台积电常以中文名出现",
    "SAMSUNG": "三星电子常以中文名出现",
    "MU": "美光常以中文名出现",
    "CN_STOCK": "A股/沪深/上证这类说法在枚举里只有 CN_STOCK",
    "HK_STOCK": "港股/恒生同理",
    "US_STOCK": "美股/纳指/标普同理",
    "US_BOND": "美债/收益率曲线同理",
}


@router.get("/api/targets/unresolved")
async def targets_unresolved(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(2000, ge=100, le=20000),
):
    """未识别 TOP 榜：哪些品种被新闻反复提到，却被判成了 NONE。"""
    t0 = time.time()
    conn = _ro_conn()
    try:
        rows = _fetch_unresolved(conn, days, limit)
    finally:
        conn.close()
    summary = target_resolver.summarize_unresolved(rows)
    # 给每个品种补一句"为什么它会被漏"，前端不用自己编解释
    for it in summary["targets"]:
        it["why"] = _TARGET_REASON.get(str(it["target"]), "")
    return {
        "ok": True,
        "data": {
            **summary,
            "window_days": days,
            "scanned": len(rows),
            "elapsed_s": round(time.time() - t0, 2),
            "caveat": (
                "recover_rate_upper 是**上限**：只代表原文里出现了品种词，"
                "不代表这条新闻值得交易。真要做到闭环，还需要模型复核一遍。"
            ),
        },
    }


@router.get("/api/targets/aliases")
async def targets_aliases():
    """当前生效的别名表（默认表 + 配置文件合并后的结果）。"""
    aliases = target_resolver.load_aliases()
    override_exists = os.path.exists(_ALIAS_PATH)
    return {
        "ok": True,
        "data": {
            "count": len(aliases),
            "targets": {k: v for k, v in sorted(aliases.items())},
            "override_path": _ALIAS_PATH,
            "override_exists": override_exists,
            "source": "default + override" if override_exists else "default",
        },
    }


class AliasAddBody(BaseModel):
    target: str
    aliases: List[str]


@router.post("/api/targets/aliases")
async def targets_add_alias(body: AliasAddBody):
    """追加别名到 config/target_aliases.json（**不动生产库、不动决策链**）。"""
    target = (body.target or "").strip().upper()
    if not target:
        return {"ok": False, "error": "target 不能为空"}
    known = set(target_resolver.DEFAULT_ALIASES)
    if target not in known:
        return {"ok": False, "error": f"未知 target '{target}'；合法值: {sorted(known)}"}
    bad = [a for a in body.aliases if not str(a).strip()]
    if not body.aliases or bad:
        return {"ok": False, "error": "aliases 不能为空"}

    os.makedirs(os.path.dirname(_ALIAS_PATH), exist_ok=True)
    cur: Dict[str, List[str]] = {}
    if os.path.exists(_ALIAS_PATH):
        try:
            with open(_ALIAS_PATH, encoding="utf-8") as f:
                cur = json.load(f) or {}
        except Exception as exc:
            return {"ok": False, "error": f"现有配置读不出来，拒绝覆盖: {type(exc).__name__}"}
    lst = cur.setdefault(target, [])
    added = []
    for a in body.aliases:
        a = str(a).strip()
        if a and a not in lst:
            lst.append(a)
            added.append(a)
    tmp = _ALIAS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, _ALIAS_PATH)
    return {"ok": True, "data": {"target": target, "added": added, "total": len(lst),
                                 "path": _ALIAS_PATH}}
