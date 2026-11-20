# -*- coding: utf-8 -*-
"""信息源健康度（v422，纯计算，不连库）。

为什么不是"又一个在线/离线小灯"：
  现在的 `/api/sources/status` 只有 status=online/error/stale 三态，**分不清**
  「这个源一直没接 key」「这个源三天前停更了」「这个源能出但一半请求失败」。
  而 11-18 实测：`raw_news` 里 49 个源，近 24 小时只有 8 个在稳定出货，
  其余全在"看起来注册了、其实没有量"的状态 —— 这就是"新闻总量少"的表面原因。

输入：saource_status 的内存注册表快照（可为空）+ DB 里按源聚合的入库统计。
输出：每源一个健康分、一个分类、一句可执行的建议。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

# 分类 → 建议（"该怎么办"必须跟分类一起下发，不然前端只能自己编）
SUGGESTIONS = {
    "healthy": "正常，不用管",
    "flaky": "能出但失败率高：先降频率/加退避，仍不好就换同内容的备源",
    "stalled": "24h 没出货：查上游是否改版/限流；确认改版则改解析或换源",
    "dead": "近 7 天 0 条且报错：停用或换源，别再让它占着注册位",
    "not_enabled": "未启用（缺 key/需注册）：想用就去申请，不想用就从面板上标成「已放弃」",
    "no_data": "注册了但从未入库：要么是解析没接上，要么是源本身没内容",
}

_SEVERITY = {"dead": 0, "stalled": 1, "flaky": 2, "no_data": 3, "not_enabled": 4, "healthy": 5}

_NOT_ENABLED_STATES = {
    "needs_key", "needs_registration", "needs_login", "premium_tier",
    "no_public_api", "untracked",
}


def _idle_hours(last_ts: Optional[float], now: float) -> Optional[float]:
    if not last_ts:
        return None
    return max(0.0, (now - float(last_ts)) / 3600.0)


def _score(status: Optional[str], ok: int, fail: int, n24: int, idle_h: Optional[float]) -> int:
    """0–100。失败率扣 50 分权重，鲜度扣 30 分权重；没数据时按分类给固定分。"""
    total = (ok or 0) + (fail or 0)
    ok_ratio = (ok or 0) / total if total else None
    base = 100.0
    if ok_ratio is not None:
        base -= (1 - ok_ratio) * 50.0
    if idle_h is not None:
        base -= min(1.0, idle_h / 48.0) * 30.0
    if n24 <= 0:
        base -= 20.0
    if status == "error":
        base -= 20.0
    return max(0, min(100, int(round(base))))


def _classify(status: Optional[str], access_state: Optional[str],
              n24: int, n7: int, ok: int, fail: int) -> str:
    if access_state in _NOT_ENABLED_STATES:
        return "not_enabled"
    if n24 > 0:
        total = (ok or 0) + (fail or 0)
        if total >= 5 and (fail or 0) / total >= 0.5:
            return "flaky"
        return "healthy"
    if n7 > 0:
        return "stalled"
    if status == "error":
        return "dead"
    return "no_data"


def build_health(registry: Iterable[Dict[str, Any]],
                 db_stats: Dict[str, Dict[str, Any]],
                 now: Optional[float] = None) -> Dict[str, Any]:
    """合并注册表与库内入库量，产出每源健康度。

    registry：source_status.snapshot()（**空列表也能算**，只用 DB 侧）
    db_stats：{source: {"n24": int, "n7": int, "last_ts": float, "last_at": str}}
    """
    now = now if now is not None else time.time()
    reg: Dict[str, Dict[str, Any]] = {}
    for s in registry:
        for key in (str(s.get("source_id") or ""), str(s.get("label") or "")):
            k = key.strip().lower()
            if k:
                reg[k] = s

    # 源集合 = 注册表 + 库内实际出现过的源（两边都要，缺一边就会漏报）
    names: Dict[str, Dict[str, Any]] = {}
    for s in registry:
        sid = str(s.get("source_id") or "").strip()
        if not sid:
            continue
        names[sid] = {"source_id": sid, "label": s.get("label") or sid,
                      "category": s.get("category") or "", "from_registry": True}
    for src, st in (db_stats or {}).items():
        src = str(src or "").strip()
        if not src:
            continue
        slot = names.setdefault(src, {"source_id": src, "label": src,
                                      "category": "", "from_registry": False})
        slot["from_db"] = True
        slot.update({
            "n24": int(st.get("n24") or 0),
            "n7": int(st.get("n7") or 0),
            "last_ts": st.get("last_ts"),
            "last_at": st.get("last_at") or "",
        })

    items: List[Dict[str, Any]] = []
    for src, meta in names.items():
        st = reg.get(src.lower()) or reg.get((meta["label"] or "").lower()) or {}
        status = st.get("status")
        access_state = st.get("access_state")
        ok = int(st.get("total_ok") or 0)
        fail = int(st.get("total_fail") or 0)
        n24 = int(meta.get("n24") or 0)
        n7 = int(meta.get("n7") or 0)
        last_ts = meta.get("last_ts")
        idle_h = _idle_hours(last_ts, now)
        cls = _classify(status, access_state, n24, n7, ok, fail)
        total_req = ok + fail
        items.append({
            "source": src,
            "label": meta["label"],
            "category": meta.get("category") or "",
            "class": cls,
            "score": _score(status, ok, fail, n24, idle_h if n24 else
                            (_idle_hours(last_ts, now) if n7 else None)),
            "in_registry": bool(meta.get("from_registry")),
            "status": status or "unknown",
            "access_state": access_state or "",
            "n24": n24,
            "n7": n7,
            "ok": ok,
            "fail": fail,
            "fail_rate": round(fail / total_req, 3) if total_req else None,
            "last_at": meta.get("last_at") or "",
            "idle_h": round(idle_h, 1) if idle_h is not None else None,
            "last_error": (st.get("last_error") or "")[:120],
            "consecutive_failures": int(st.get("consecutive_failures") or 0),
            "suggestion": SUGGESTIONS.get(cls, ""),
        })

    items.sort(key=lambda x: (_SEVERITY.get(x["class"], 9), x["score"], -x["n7"]))
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["class"]] = counts.get(it["class"], 0) + 1

    # "以前出货、现在不出货"的源 —— 停更应该被单独点出来，混在死源里看不见
    stalled = [it for it in items if it["class"] == "stalled"]
    total_n24 = sum(it["n24"] for it in items)
    active = [it for it in items if it["n24"] > 0]
    return {
        "generated_at": now,
        "source_total": len(items),
        "counts": counts,
        "active_24h": len(active),
        "intake_24h": total_n24,
        # 前 3 个源贡献了多少——集中度越高，一个源挂掉对总量影响越大
        "concentration_top3": round(
            sum(it["n24"] for it in sorted(items, key=lambda x: -x["n24"])[:3]) / total_n24, 3
        ) if total_n24 else None,
        "stalled": [{"source": it["source"], "label": it["label"], "n7": it["n7"],
                     "idle_h": it["idle_h"]} for it in stalled[:20]],
        "items": items,
        "caveat": (
            "「接口返回 200 但 items 为空」这类源侧故障没法从库里看出来，"
            "本面板只对「有没有真的入库」负责；接口层用 site_audit.py 的 empty 分类看。"
        ),
    }
