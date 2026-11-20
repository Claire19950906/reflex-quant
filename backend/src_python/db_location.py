#!/usr/bin/env python3
"""db_location.py — 两个服务共用的 SQLite 主库路径（唯一来源）。

原来 api_server / bridge_server 各自算一遍 DB_PATH：必须指向**同一个**库，
否则 8000 上的"真实持仓战绩"和 8100 读出来的对不上。这里收成一处：
优先用 config.DB_PATH（已含 TRIDENT_DB_PATH 覆盖），config 不可用时回退到
backend/trident_event_bus.db。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

try:  # pragma: no cover - 取决于部署环境
    import config as _trident_config

    DB_PATH = Path(_trident_config.DB_PATH)
except Exception as _exc:
    print(f"[paths] config 不可用, 回退默认 DB 路径: {_exc}", file=sys.stderr)
    DB_PATH = BACKEND_DIR / "trident_event_bus.db"

__all__ = ["BACKEND_DIR", "DB_PATH"]
