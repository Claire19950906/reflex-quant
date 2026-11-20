"""legacy adapter —— 把现有 reflection_checkers 挂进新包，不复制、不改写。

原则：audit 包不 import `db`。reflection_checkers 顶层 `import db` 只为拿
`db.get_connection()`（仅在调用方不传 conn 时才会用到）。这里预先注入一个
空壳 `db`，让审计层在完全没有数据库的机器上也能 import 成功。
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SRC_DIR = Path(__file__).resolve().parents[2] / "backend" / "src_python"

_loaded: Optional[Any] = None


def _no_db(*_a, **_k):
    raise RuntimeError(
        "audit: 该模块不应直接访问数据库；请通过 CheckContext 注入 conn"
    )


def load() -> Any:
    """返回 reflection_checkers 模块（进程内只加载一次）。"""
    global _loaded
    if _loaded is not None:
        return _loaded

    if not SRC_DIR.exists():
        raise RuntimeError(f"audit: 找不到反射内核目录 {SRC_DIR}")

    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    if "db" not in sys.modules:
        stub = types.ModuleType("db")
        stub.get_connection = _no_db  # type: ignore[attr-defined]
        stub.get_db_path = lambda: ":memory:"  # type: ignore[attr-defined]
        sys.modules["db"] = stub

    from review import reflection_checkers

    _loaded = reflection_checkers
    return _loaded


def remediation_for(code: str) -> str:
    """每条错误码的"下一步怎么改"。内核里已有 33 码的指导表。"""
    rc = load()
    table: Dict[str, str] = getattr(rc, "_ERROR_CODE_GUIDANCE", {}) or {}
    return table.get(code, "") or ""


def call_checker(fn: Callable, decision: Dict[str, Any], conn) -> List[Dict[str, Any]]:
    """按签名决定是否传 conn —— 避免 TypeError 猜测。"""
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        params = {}
    if "conn" in params:
        return list(fn(decision, conn=conn) or [])
    return list(fn(decision) or [])


def e019(decision: Dict[str, Any], completeness: float) -> Dict[str, Any]:
    rc = load()
    return rc._make_E019_low_completeness(decision, completeness)


def is_low_completeness(decision: Dict[str, Any], threshold: float = 0.7) -> Optional[float]:
    raw = decision.get("completeness_score")
    if raw is None:
        return None
    try:
        comp = float(raw)
    except (TypeError, ValueError):
        return None
    return comp if comp < threshold else None
