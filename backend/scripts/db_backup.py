#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_backup —— 在线备份 + 恢复演练（1.4GB 单文件库的保命工具）。

为什么必须先做这件事（2026-11-16）：
    库里只有一个 trident_event_bus.db（1,426MB，WAL），而 `ai_decisions`
    曾从 1.1 万行掉到 327 行、原因至今没查清。也就是说：**数据已经丢过一次**，
    而当时没有任何可回退的备份。架构再怎么优化，都不如先回答"能不能恢复"。

做法：
    · 用 sqlite3 官方 backup API（在线热备，WAL 下安全），不 `cp` 文件；
    · 备完立刻 **验证**：打开副本、`PRAGMA integrity_check`、比对关键表行数；
    · `--drill`：把备份还原到一个临时路径并比对，证明"备份真的能用"；
    · 保留 N 份（默认 7），按时间戳轮转。

用法：
    python3 backend/scripts/db_backup.py                 # 备份 + 验证
    python3 backend/scripts/db_backup.py --drill         # 备份 + 验证 + 还原演练
    python3 backend/scripts/db_backup.py --list          # 看现有备份
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = Path(os.getenv("TRIDENT_DB_PATH") or (BACKEND / "trident_event_bus.db"))
KEEP = int(os.getenv("TRIDENT_BACKUP_KEEP", "7"))
BACKUP_DIR = Path(os.getenv("TRIDENT_BACKUP_DIR") or (Path.home() / ".trident" / "backups"))

# 关键表：备份后逐表比行数，任何一张对不上就判失败
KEY_TABLES = ("raw_news", "ai_decisions", "ai_call_stats", "kline_bars", "worker_heartbeat")


def _counts(db: Path) -> dict:
    out = {}
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except Exception as exc:
        return {"__error__": str(exc)}
    try:
        for t in KEY_TABLES:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None
    finally:
        c.close()
    return out


def _backup(dst: Path) -> None:
    src = sqlite3.connect(str(DB), timeout=30)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dst), timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000;")
        src.backup(out)          # 官方 API：一致性快照，不阻塞读
    finally:
        out.close()
        src.close()


def _rotate() -> list:
    files = sorted(BACKUP_DIR.glob("trident_event_bus_*.db"), key=lambda p: p.stat().st_mtime)
    removed = []
    while len(files) > KEEP:
        victim = files.pop(0)
        try:
            victim.unlink()
            removed.append(victim.name)
        except Exception:
            pass
    return removed


def main() -> int:
    args = sys.argv[1:]
    if "--list" in args:
        if not BACKUP_DIR.exists():
            print("[BACKUP] 还没有备份目录")
            return 0
        for p in sorted(BACKUP_DIR.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
            print(f"  {p.name}  {p.stat().st_size / 1048576:.1f}MB  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}")
        return 0

    if not DB.exists():
        print(f"[BACKUP] 源库不存在: {DB}")
        return 1

    t0 = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"trident_event_bus_{stamp}.db"
    if dst.exists():
        dst = BACKUP_DIR / f"trident_event_bus_{stamp}_1.db"

    src_counts = _counts(DB)
    try:
        _backup(dst)
    except Exception as exc:
        print(f"[BACKUP] 失败: {type(exc).__name__}: {exc}")
        return 1

    # 验证副本：完整性 + 关键表行数
    try:
        c = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=15)
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
    except Exception as exc:
        print(f"[BACKUP] 副本打不开: {type(exc).__name__}: {exc}")
        return 1
    dst_counts = _counts(dst)
    diff = {k: (src_counts.get(k), dst_counts.get(k)) for k in KEY_TABLES
            if src_counts.get(k) != dst_counts.get(k)}

    size_mb = dst.stat().st_size / 1048576
    print(f"[BACKUP] 完成 {dst.name} {size_mb:.1f}MB 用时 {time.time() - t0:.1f}s "
          f"integrity={integrity}")
    if diff:
        print(f"[BACKUP] ⚠️ 行数不一致（源, 副本）: {diff}")
        return 1

    removed = _rotate()
    if removed:
        print(f"[BACKUP] 轮转删除: {', '.join(removed)}")

    if "--drill" in args:
        drill = BACKUP_DIR / "_drill_restore.db"
        try:
            if drill.exists():
                drill.unlink()
            src = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=20)
            out = sqlite3.connect(str(drill))
            src.backup(out)
            out.close()
            src.close()
            dc = _counts(drill)
            same = all(dc.get(k) == dst_counts.get(k) for k in KEY_TABLES)
            print(f"[DRILL] 还原到 {drill.name} → 关键表一致: {same}")
            print(f"[DRILL] 计数: {dc}")
            drill.unlink()
            if not same:
                return 1
        except Exception as exc:
            print(f"[DRILL] 还原演练失败: {type(exc).__name__}: {exc}")
            return 1

    print("[BACKUP] 校验通过（关键表行数与源库一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
