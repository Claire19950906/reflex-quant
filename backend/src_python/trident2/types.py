# -*- coding: utf-8 -*-
"""功能层结果类型 —— 诚实口径的唯一出口。

任何功能函数的返回值都必须经过 ``FeatureResult`` 构造：
- ``status`` 只允许 ok / no_data / not_configured / error;
- ``value`` 只能是「真实算出来的数」或空字典，禁止用 0 / '' / 'N/A' 顶替缺失;
- ``evidence`` 必须写清来源表、查询条件、时间锚点，事后能复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"

VALID_STATUS = {STATUS_OK, STATUS_NO_DATA, STATUS_NOT_CONFIGURED, STATUS_ERROR}

SEVERITIES = ("info", "watch", "warn", "critical")

# 告警分级（F35）：severity → 级别 + 默认冷却窗口
SEVERITY_TO_LEVEL = {
    "critical": "P1",
    "warn": "P2",
    "watch": "P3",
    "info": "P3",
}
DEFAULT_COOLDOWN_SEC = {
    "P1": 10 * 60,
    "P2": 30 * 60,
    "P3": 30 * 60,
}


def _clean(obj: Any) -> Any:
    """把值转成可 JSON 序列化的形式，同时挡住 NaN / inf（它们不是有效统计值）。"""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    return str(obj)


@dataclass
class FeatureResult:
    feature_id: str
    status: str
    headline: str = ""
    value: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    anchor_ts: Optional[int] = None
    sample_size: Optional[int] = None
    severity: str = "info"
    action: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)
    run_ts: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUS:
            raise ValueError(f"invalid status: {self.status}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid severity: {self.severity}")
        # 硬约束：ok 必须有值；no_data 不能带值（防止把空值当结论）
        if self.status == STATUS_OK and not self.value:
            raise ValueError(f"{self.feature_id}: status=ok 但 value 为空，属于无证据结论")
        if self.status != STATUS_OK and self.value:
            raise ValueError(f"{self.feature_id}: status={self.status} 不应携带 value")

    @property
    def level(self) -> Optional[str]:
        if self.severity == "info":
            return None
        return SEVERITY_TO_LEVEL[self.severity]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["value"] = _clean(self.value)
        d["evidence"] = _clean(self.evidence)
        d["action"] = _clean(self.action)
        d["notes"] = _clean(self.notes)
        return d

    @classmethod
    def ok(cls, feature_id: str, headline: str, value: Dict[str, Any], **kw: Any) -> "FeatureResult":
        return cls(feature_id=feature_id, status=STATUS_OK, headline=headline, value=value, **kw)

    @classmethod
    def no_data(cls, feature_id: str, headline: str, reason: str, **kw: Any) -> "FeatureResult":
        notes = list(kw.pop("notes", []) or [])
        notes.append(reason)
        return cls(feature_id=feature_id, status=STATUS_NO_DATA, headline=headline, notes=notes, **kw)

    @classmethod
    def not_configured(cls, feature_id: str, headline: str, reason: str, **kw: Any) -> "FeatureResult":
        notes = list(kw.pop("notes", []) or [])
        notes.append(reason)
        return cls(feature_id=feature_id, status=STATUS_NOT_CONFIGURED, headline=headline, notes=notes, **kw)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """返回百分比变化（%）。任一值缺失或 old=0 时返回 None —— 不补 0。"""
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0
