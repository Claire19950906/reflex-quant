"""文件数据源：一个目录四个文件，缺文件=空数据源（不抛异常）。

    providers/
    ├── rules.json      {"version":"providers.v1","rules":[...]}
    ├── drivers.json    {"version":"providers.v1","nodes":[...]}
    ├── history.json    {"version":"providers.v1","samples":[...],"trades":[...],"decisions":[...]}
    └── kline.json      {"version":"providers.v1","bars":[...]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .base import VERSION, DriversData, HistoryData, KlineData, RulesData

FILES = {
    "rules": "rules.json",
    "drivers": "drivers.json",
    "history": "history.json",
    "kline": "kline.json",
}


class FileBundle:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.errors: List[str] = []
        self.label = f"file:{self.root}"

    # ── 内部 ────────────────────────────────────────────────
    def _read(self, key: str) -> Dict[str, Any]:
        path = self.root / FILES[key]
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            return {}
        if not isinstance(payload, dict):
            self.errors.append(f"{path.name}: 顶层必须是对象")
            return {}
        if payload.get("version") != VERSION:
            self.errors.append(f"{path.name}: version 应为 {VERSION}，实际 {payload.get('version')!r}")
        return payload

    @staticmethod
    def _rows(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        rows = payload.get(key) or []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # ── Bundle 接口 ────────────────────────────────────────
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
