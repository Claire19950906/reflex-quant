# -*- coding: utf-8 -*-
"""v421 · 注入日志 ↔ 决策 对账（纯读，供 quality 端点与离线脚本共用）

为什么需要：
    `causal_injection_log` 记了 1782 次注入，但 `decision_id` **一次都没填**过
    （engine 四个埋点现场都拿不到 decision id）。于是没人能回答一个最基本的问题：
    这些注入里，**有多少真的进了下单决策？**

实测（2026-11-18）：
    · 注入 1782 次，modal 分布 attribute 1355 / batch 427；
    · 按 (品种, ±90s) 唯一配对，能对上 `ai_decisions` 的只有 88 次（4.9%）；
    · 也就是说 **约 95% 的注入不在决策路径上** —— 图谱主要被用在"反硬归因"里解释行情，
    而不是驱动下单。

这个结论比"再补几根链"重要：链再多，只要不进决策路径，对方向判断的影响就是零。
因此先把刻度摆出来（`injection` 块），再谈要不要把 chain_prior 接进否决链。
"""
from __future__ import annotations

import bisect
import time
from typing import Any, Dict, List

# 注入侧资产名常是中文标签（上游传的是品种中文名而不是 ticker）
_ALIAS = {
    "原油": "WTI", "黄金": "XAU", "白银": "XAG", "美股": "US_STOCK",
    "个股": "INDIVIDUAL_STOCK", "加密主流": "CRYPTO", "山寨": "CRYPTO",
    "宏观": "MACRO", "A股": "CN_STOCK", "港股": "HK_STOCK",
}

DEFAULT_WINDOW_S = 90


def norm_asset(asset: str) -> str:
    a = str(asset or "").strip()
    return _ALIAS.get(a, a.upper())


def to_epoch(ts: str) -> int:
    try:
        return int(time.mktime(time.strptime(str(ts).strip(), "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return 0


def pair(conn, window_s: int = DEFAULT_WINDOW_S, since_ts: int | None = None) -> List[Dict[str, Any]]:
    """逐条注入配对决策。返回每条的 {injection_id, asset, mode, decision_id, delta_s, status}。"""
    where = "" if since_ts is None else f" AND ts >= '{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since_ts))}'"
    inj = [dict(r) for r in conn.execute(
        f"SELECT id, ts, asset, mode FROM causal_injection_log WHERE decision_id IS NULL{where} ORDER BY id"
    ).fetchall()]
    dec = [dict(r) for r in conn.execute(
        "SELECT id, target_asset, ts FROM ai_decisions WHERE ts IS NOT NULL ORDER BY ts"
    ).fetchall()]
    by_asset: Dict[str, List[tuple]] = {}
    for d in dec:
        by_asset.setdefault(norm_asset(d["target_asset"]), []).append((int(d["ts"]), int(d["id"])))
    for k in by_asset:
        by_asset[k].sort()

    out = []
    for r in inj:
        a = norm_asset(r["asset"])
        t = to_epoch(r["ts"])
        status, hit_id, delta = "unmatched", None, None
        lst = by_asset.get(a) or []
        if t and lst:
            lo = bisect.bisect_left(lst, (t - window_s, -1))
            hi = bisect.bisect_right(lst, (t + window_s, 10 ** 12))
            cands = lst[lo:hi]
            if len(cands) == 1:
                hit_id, delta, status = cands[0][1], cands[0][0] - t, "matched"
            elif cands:
                nearest = min(cands, key=lambda c: abs(c[0] - t))
                hit_id, delta, status = nearest[1], nearest[0] - t, "ambiguous"
        out.append({"injection_id": int(r["id"]), "ts": r["ts"], "asset_raw": r["asset"],
                    "asset": a, "mode": r["mode"], "decision_id": hit_id,
                    "delta_s": delta, "status": status})
    return out


def summarize(conn, window_s: int = DEFAULT_WINDOW_S, days: int | None = None) -> Dict[str, Any]:
    """给 quality 端点用的汇总块。days=None 视为全表。"""
    since = None if days is None else int(time.time()) - int(days) * 86400
    rows = pair(conn, window_s=window_s, since_ts=since)
    total = len(rows)
    matched = sum(1 for r in rows if r["status"] == "matched")
    by_mode: Dict[str, int] = {}
    for r in rows:
        by_mode[r["mode"]] = by_mode.get(r["mode"], 0) + 1
    return {
        "total": total,
        "matched": matched,
        "ambiguous": sum(1 for r in rows if r["status"] == "ambiguous"),
        "unmatched": sum(1 for r in rows if r["status"] == "unmatched"),
        "matched_ratio": round(matched / total, 4) if total else 0.0,
        "by_mode": by_mode,
        "window_s": window_s,
        "note": ("按 (品种, ±窗口) 与 ai_decisions 唯一配对。配不上 ≠ 注入失败 —— "
                 "attribute/batch 两类注入主要发生在归因取证与批量注入路径，本来就不产生决策行；"
                 "这个比例只回答「图谱有多少次真正进了下单决策」。"),
    }
