#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prestart_migrate —— 把"写库的启动工作"从接口进程里挪出来。

事故背景（2026-11-16 反复出现）：
    api 启动时会做 migrate + 种子写入 + id_reconcile.ensure_schema。
    这些都要拿写锁；而引擎/采集正在高频写库时，只能排队等 busy_timeout，
    实测曾卡 9 分钟、今天又卡了近 2 分钟 —— 期间 8000 端口完全不可用。

做法：
    1. 由 launchd 的启动脚本先跑本脚本（拿到写锁才做迁移与种子）；
    2. 成功后写 runtime/schema_ready.json（含时间与 schema 版本）；
    3. api 启动时只看这个标记：新鲜就完全跳过写库，不新鲜才自己兜底做。
       → "接口启动"与"写库准备"解耦，锁竞争不再能拖垮端口可用性。

退出码：0 成功（或无需迁移）；1 失败（api 仍可启动，走兜底路径）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

MARKER = BACKEND / "runtime" / "schema_ready.json"
# 标记有效期：超过这个时间就认为可能改过 schema，允许 api 自己做一次兜底
FRESH_S = int(os.getenv("SCHEMA_MARKER_FRESH_S", "600"))


def main() -> int:
    t0 = time.time()
    try:
        import db
    except Exception as exc:
        print(f"[PRESTART] 无法导入 db: {type(exc).__name__}: {exc}", flush=True)
        return 1

    conn = None
    try:
        conn = db.get_connection()
        conn.execute("PRAGMA busy_timeout=2000")
        ready = db.schema_ready(conn)
        if not ready:
            print("[PRESTART] schema 未就绪 → 执行 migrate", flush=True)
            db.migrate(conn)
        version = None
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except Exception:
            pass
        elapsed = round(time.time() - t0, 2)
        import startup_marker as _sm
        _sm.write_marker(user_version=version, elapsed_s=elapsed)
        print(f"[PRESTART] schema 就绪 (version={version}, {elapsed}s) → 已写标记", flush=True)
        return 0
    except Exception as exc:
        print(f"[PRESTART] 失败（api 将走兜底路径）: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def marker_fresh() -> bool:
    """供 api 调用：标记是否存在且新鲜。"""
    try:
        age = time.time() - MARKER.stat().st_mtime
        return age <= FRESH_S
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
