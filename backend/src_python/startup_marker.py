#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动标记：说明"迁移已完成、schema 就绪"，供接口进程在启动时判断是否需要写库。

写入方：backend/scripts/prestart_migrate.py（launchd 启动脚本里先跑）
读取方：api_server._migrate_schema()（标记新鲜就完全跳过写库）

为什么放在 src_python 而不是 scripts：
    api 的 sys.path 里只有 src_python；scripts 下的模块它 import 不到。
    与其在 api 里用 importlib 按路径加载（脆弱），不如给"标记"本身一个模块。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
MARKER = BACKEND / "runtime" / "schema_ready.json"
FRESH_S = int(os.getenv("SCHEMA_MARKER_FRESH_S", "600"))


def marker_fresh() -> bool:
    try:
        return (time.time() - MARKER.stat().st_mtime) <= FRESH_S
    except Exception:
        return False


def write_marker(user_version=None, elapsed_s=None) -> None:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps({
        "ready": True,
        "user_version": user_version,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": elapsed_s,
    }, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    print("fresh:", marker_fresh(), "path:", MARKER)
