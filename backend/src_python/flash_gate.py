"""快讯类采集器（sina_flash / wscn）的门卫接入 —— 让"直插"变成"过闸"。

## 修的是什么

`sina_flash.py` / `wscn_flash.py` 原来是**绕过整条过滤器**的裸 INSERT：

```sql
INSERT INTO raw_news (source, content, timestamp, ts, published_at, status,
                      gate_kind, content_fingerprint, ...)
VALUES (?,?,?,?,?, 'PENDING', 'real_news', ?, ...)
```

后果（2026-11-17 实测）：近 3 小时非噪音新闻 134 条里，**76 条（57%）`gate_verdict` 为空、
`gate_unavailable=0`** —— 即既没判噪音、也没过门卫，直接进主分析队列。近 1 小时产生的
8 条决策里有 6 条来自这批，其中 5 条 `target_none`。来源分布：sina_flash 59 条、wscn 28 条。

这类"裸入库"在库里长得和"门卫判过的新闻"几乎一样（都是 `status='PENDING'`、`is_noise=0`），
所以面板、归档语义、降级率统计全都被它污染 —— 又一个"看不见就没法修"的洞。

## 接入原则

1. **失败不阻断入库**：过滤链路任何异常 → 退回原来的裸入库行为（新闻不能因为过滤器挂了而不入库）。
2. **判噪音就不入库**：与 `api_server._ingest_external_news_sync` 一致（噪音本就不该进 raw_news）。
3. **开关**：`FLASH_FEEDS_GATE=0` 一键回到旧行为，不需要改代码。
4. **门卫不可用时照常写库**：`gate_unavailable` 由 `evaluate_news` 给出（1=冻结待回放、2=降级放行），
   主分析认领 SQL 已经按这个标记区分 —— 不再有"看起来正常其实没判"的新闻。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def enabled() -> bool:
    """开关：默认开。显式设 0/false/off 才关。"""
    return str(os.getenv("FLASH_FEEDS_GATE", "1")).strip().lower() not in {"0", "false", "off", "no"}


def gate_columns(body: str, published_at: str = "", ts: Optional[int] = None,
                 _evaluate: Any = None) -> Optional[Dict[str, Any]]:
    """跑一遍过滤器，返回要写进 raw_news 的门卫相关列。

    Returns:
        None                → 这条是噪音，调用方应跳过入库
        dict                → 要写入的列（`gate_verdict` 等）

    过滤器不可用（import 失败 / 抛异常）时返回 `gate_kind_only()` 的结果，
    保证采集器永远能入库（宁可少过滤，不可丢新闻）。
    """
    body = (body or "").strip()
    if not body:
        return None
    evaluate = _evaluate
    if evaluate is None:
        try:
            from realtime_filter import evaluate_news as evaluate  # noqa: WPS433 (延迟导入)
        except Exception:
            return gate_kind_only()
    try:
        res = evaluate(body[:300], body, published_at=published_at or "", ingested_ts=ts)
    except Exception:
        return gate_kind_only()
    if not isinstance(res, dict):
        return gate_kind_only()
    if int(res.get("is_noise") or 0) == 1:
        return None
    gate = res.get("gate") or {}
    verdict = str(res.get("gate_verdict") or gate.get("verdict") or "")
    return {
        "is_noise": 0,
        "relevance_score": float(res.get("relevance_score") or 0.0),
        "gate_verdict": verdict,
        "gate_unavailable": int(res.get("gate_unavailable") or 0),
        "gate_strength": int(gate.get("strength") or 0),
        "gate_main_asset": str(gate.get("main_asset") or "") or None,
        # v453 P1-4：这些行**照常入库进分析队列**，`filter_reason` 只用于"没进队列"
        # 的原因（stale_expired / pre_recovery_skip / prefilter_scope…）。
        # 原来这里写 `flash_gated`，导致 `status='DONE' AND filter_reason<>''` 居然
        # 有 141 条（78 flash_gated + 63 flash_gate_unavailable），把漏斗口径搞乱。
        # 门卫结论已经完整写在 gate_verdict / gate_unavailable / gate_strength，
        # 不需要在 filter_reason 上重复。
        "filter_reason": "",
    }


def gate_kind_only() -> Dict[str, Any]:
    """过滤器不可用时的退化列值：与旧行为等价，但不伪装成"门卫判过"。"""
    return {
        "is_noise": 0,
        "relevance_score": 0.0,
        "gate_verdict": "",
        "gate_unavailable": 0,
        "gate_strength": 0,
        "gate_main_asset": None,
        # v453 P1-4：退化路径也照常入库，不留"被过滤"的假原因
        "filter_reason": "",
    }
