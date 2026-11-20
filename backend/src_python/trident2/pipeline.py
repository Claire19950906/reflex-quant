# -*- coding: utf-8 -*-
"""运行管线：单跑 / 批量巡检 / 落库 / 告警。

设计原则：
- 任一功能抛异常只影响它自己（结果记 error，其他照跑）；
- 结果先落 ``t2_results`` 再发告警，保证「告警可回溯」；
- 批量巡检按优先级 P0 → P1 → P2，与飞书清单口径一致。
"""
from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Dict, List, Optional

from . import catalog, registry, store
from .context import Context
from .types import FeatureResult

logger = logging.getLogger("trident2.pipeline")


def run_one(conn, feature_id: str, now: Optional[int] = None,
            persist: bool = True, **kwargs: Any) -> FeatureResult:
    fn = registry.get(feature_id)
    if fn is None:
        return FeatureResult.not_configured(feature_id, "功能未实现",
                                            f"{feature_id} 在目录中但未注册实现")
    ctx = Context(conn, now=now)
    t0 = time.time()
    try:
        result = fn(ctx, **kwargs)
    except Exception as exc:
        logger.warning("feature %s failed: %s", feature_id, exc)
        result = FeatureResult(
            feature_id=feature_id, status="error", headline="执行失败",
            evidence={"error": str(exc), "traceback": traceback.format_exc(limit=3)},
            notes=[f"{type(exc).__name__}: {exc}"],
        )
    result.run_ts = int(now or time.time())
    result.evidence.setdefault("elapsed_ms", int((time.time() - t0) * 1000))
    if persist:
        store.record_result(conn, result)
        alert = store.emit_alert(conn, result, now=result.run_ts)
        if alert:
            logger.info("alert %s %s", alert["level"], alert["title"])
    return result


def run_many(conn, feature_ids: List[str], now: Optional[int] = None,
             persist: bool = True) -> List[FeatureResult]:
    out = []
    for fid in feature_ids:
        out.append(run_one(conn, fid, now=now, persist=persist))
    return out


def run_all(conn, priority: Optional[str] = None, persist: bool = True,
            now: Optional[int] = None) -> List[FeatureResult]:
    registry.load_all()
    specs = catalog.FEATURES
    if priority:
        specs = [s for s in specs if s.priority == priority]
    order = {"P0": 0, "P1": 1, "P2": 2}
    specs = sorted(specs, key=lambda s: (order.get(s.priority, 9), s.id))
    return run_many(conn, [s.id for s in specs], now=now, persist=persist)


def status_snapshot(conn) -> Dict[str, Any]:
    """给 /api/v2/features 用：目录 + 最近一次结果 + 是否有实现。"""
    registry.load_all()
    latest = store.latest_by_feature(conn)
    items = []
    for spec in catalog.FEATURES:
        d = spec.to_dict()
        d["implemented"] = registry.get(spec.id) is not None
        last = latest.get(spec.id)
        if last:
            d["last_run"] = {
                "run_ts": last.get("run_ts"),
                "status": last.get("status"),
                "severity": last.get("severity"),
                "headline": last.get("headline"),
                "value": last.get("value_json"),
                "sample_size": last.get("sample_size"),
                "anchor_ts": last.get("anchor_ts"),
            }
        items.append(d)
    return {
        "counts": catalog.summary(),
        "implemented": len([i for i in items if i["implemented"]]),
        "items": items,
    }
