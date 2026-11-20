"""HTTP 数据源：GET {base_url}/{rules,drivers,history,kline}.json

失败不抛异常（门禁不能因为数据源抖动就崩），但会把错误记进 `errors`，
由 CheckContext 转成 not_applicable 的原因——不允许静默当"已审过"。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List

from .base import VERSION, DriversData, HistoryData, KlineData, RulesData

FILES = {
    "rules": "rules.json",
    "drivers": "drivers.json",
    "history": "history.json",
    "kline": "kline.json",
}


class HttpBundle:
    def __init__(self, base_url: str, *, timeout: float = 10.0, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.errors: List[str] = []
        self.label = f"http:{self.base_url}"

    def _read(self, key: str) -> Dict[str, Any]:
        url = f"{self.base_url}/{FILES[key]}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as exc:
            self.errors.append(f"{FILES[key]}: {type(exc).__name__}: {exc}")
            return {}
        if not isinstance(payload, dict):
            self.errors.append(f"{FILES[key]}: 顶层必须是对象")
            return {}
        if payload.get("version") != VERSION:
            self.errors.append(f"{FILES[key]}: version 应为 {VERSION}")
        return payload

    @staticmethod
    def _rows(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        rows = payload.get(key) or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def rules(self) -> RulesData:
        return RulesData(rules=self._rows(self._read("rules"), "rules"))

    def drivers(self) -> DriversData:
        return DriversData(nodes=self._rows(self._read("drivers"), "nodes"))

    def history(self) -> HistoryData:
        payload = self._read("history")
        return HistoryData(
            samples=self._rows(payload, "samples"),
            trades=self._rows(payload, "trades"),
            decisions=self._rows(payload, "decisions"),
        )

    def kline(self) -> KlineData:
        return KlineData(bars=self._rows(self._read("kline"), "bars"))
