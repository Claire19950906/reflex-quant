"""trident 事件总线 —— 数据库体检与修复工具 (v300-audit)

背景
----
生产库 `trident_event_bus.db` 曾涨到 3.66GB，WAL 468MB，且多个接口稳定超时。
根因有两类，都在这里被收敛：

1. **单行超大 blob**
   `asset_driver_nodes` 第 1 行的 `evidence` 字段长到 **872,422,822 字节**。
   该表任何全表扫描（例如 `MAX(updated_at)`）都要把这 872MB 读一遍 →
   `/api/analysis/kg-stats`、`/api/review/causal-graph/stats` 稳定 60s+ 超时。
   → `cap_oversized_blobs()` 用服务端 `substr()` 截断，不把 blob 读进 Python 内存。

2. **schema 漂移**
   `news_events` 被迁移到新 schema 后丢失 `headline/body/assets/fetched_at`，
   导致 SSE 事件流报错、三条新闻入库全部失败、因果图谱 news_to_move 恒空。
   → `backfill_news_events_legacy()` 从 raw_news 回填内容。

用法
----
    python3 db_maintenance.py                 # 只体检，不写库
    python3 db_maintenance.py --apply --vacuum   # 体检 + 修复 + 回收空间

注意
----
`--vacuum` 需要独占数据库（会短暂阻塞所有写者），建议先停掉 api/bridge 服务。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

DB_PATH = (
    (getattr(db, "get_db_path", None) or (lambda: None))()
    or os.getenv("TRIDENT_DB_PATH")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "trident_event_bus.db")
)

# ── 需要看管的"大文本列"：(表, 列, 上限字节) ────────────────────────────────
BLOB_GUARD: List[Tuple[str, str, int]] = [
    ("asset_driver_nodes", "evidence",        256 * 1024),
    ("asset_driver_edges", "evidence",        256 * 1024),
    ("causal_chains",      "evidence",        256 * 1024),
    ("kg_improvements",    "rationale",       256 * 1024),
    ("ai_decisions",       "reasoning_path",  256 * 1024),
    ("ai_decisions",       "decision_context", 256 * 1024),
    ("raw_news",           "content",         256 * 1024),
    ("news_events",        "body",             64 * 1024),
]

# 高频写入的历史表：按保留期裁剪（表, 时间列, 保留天数）
PRUNE_RULES: List[Tuple[str, str, int]] = [
    ("source_status_history", "ts", 14),
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _file_size(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _wal_size(path: str) -> float:
    return _file_size(path + "-wal")



# ── 1) 巨型 blob 体检 ──────────────────────────────────────────────────────
def scan_oversized_blobs(conn: sqlite3.Connection) -> List[Dict]:
    """找出超过上限的大文本行（只读，不改库）。"""
    found: List[Dict] = []
    for table, col, limit in BLOB_GUARD:
        if not _table_exists(conn, table) or col not in _columns(conn, table):
            continue
        t0 = time.time()
        try:
            rows = conn.execute(
                f"SELECT id, LENGTH(\"{col}\") AS n FROM \"{table}\" "
                f"WHERE LENGTH(\"{col}\") > ? ORDER BY n DESC LIMIT 20",
                (limit,),
            ).fetchall()
        except sqlite3.Error as e:
            print(f"  [!] {table}.{col} 扫描失败: {e}", flush=True)
            continue
        dt = time.time() - t0
        if rows:
            total = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(\"{col}\")),0) FROM \"{table}\" "
                f"WHERE LENGTH(\"{col}\") > ?", (limit,)
            ).fetchone()
            found.append({
                "table": table, "col": col, "limit": limit,
                "rows": [tuple(r) for r in rows],
                "n_over": total[0], "bytes_over": total[1],
            })
            print(f"  [X] {table}.{col}: {total[0]} 行超限, 合计 {total[1]/1e6:.1f} MB "
                  f"(扫描 {dt:.1f}s)", flush=True)
            for rid, n in rows[:5]:
                print(f"        id={rid}  len={n:,}", flush=True)
        else:
            print(f"  [ok] {table}.{col}: 无超限行 (扫描 {dt:.1f}s)", flush=True)
    return found


def cap_oversized_blobs(conn: sqlite3.Connection, limit_override: Optional[int] = None) -> int:
    """用服务端 substr() 截断超限 blob（不把大 blob 读进 Python 内存）。

    截断后追加标记，便于识别是历史被裁剪过的行。
    """
    fixed = 0
    for table, col, limit in BLOB_GUARD:
        lim = limit_override or limit
        if not _table_exists(conn, table) or col not in _columns(conn, table):
            continue
        try:
            cur = conn.execute(
                f"UPDATE \"{table}\" SET \"{col}\" = "
                f"  substr(\"{col}\", 1, ?) || '...[TRUNCATED by db_maintenance]' "
                f"WHERE LENGTH(\"{col}\") > ?",
                (lim, lim),
            )
            if cur.rowcount and cur.rowcount > 0:
                fixed += cur.rowcount
                print(f"  [FIX] {table}.{col}: 截断 {cur.rowcount} 行 (上限 {lim} B)", flush=True)
        except sqlite3.Error as e:
            print(f"  [!] {table}.{col} 截断失败: {e}", flush=True)
    conn.commit()
    return fixed



# ── 2) news_events 旧列回填 ───────────────────────────────────────────────
def backfill_news_events_legacy(conn: sqlite3.Connection) -> int:
    """从 raw_news 回填 news_events 的 headline / body / assets / fetched_at。

    news_events 被迁移到新 schema 时丢了旧列，旧列内容可以从 raw_news 复原：
      headline   ← raw_news.content 前 300 字
      body       ← raw_news.content 301~2300 字
      assets     ← raw_news.gate_assets（门卫抽出的品种 JSON）
      fetched_at ← news_events.ts
    """
    if not _table_exists(conn, "news_events"):
        return 0
    have = _columns(conn, "news_events")
    missing = {"headline", "body", "assets", "fetched_at"} - have
    if missing:
        print(f"  [!] news_events 仍缺列 {sorted(missing)} —— 先跑 db.migrate()", flush=True)
        return 0
    if not _table_exists(conn, "raw_news"):
        return 0
    cur = conn.execute(
        """
        UPDATE news_events
           SET headline   = COALESCE(NULLIF(headline, ''),
                        (SELECT substr(r.content, 1, 300) FROM raw_news r WHERE r.id = news_events.news_id)),
               body       = COALESCE(NULLIF(body, ''),
                        (SELECT substr(r.content, 301, 2000) FROM raw_news r WHERE r.id = news_events.news_id)),
               assets     = COALESCE(NULLIF(assets, ''),
                        (SELECT NULLIF(r.gate_assets, '') FROM raw_news r WHERE r.id = news_events.news_id)),
               fetched_at = CASE WHEN COALESCE(fetched_at, 0) = 0 THEN news_events.ts ELSE fetched_at END
         WHERE COALESCE(headline, '') = ''
            OR COALESCE(assets, '') = ''
            OR COALESCE(fetched_at, 0) = 0
        """
    )
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    print(f"  [FIX] news_events 回填 {n} 行 (headline/body/assets/fetched_at)", flush=True)
    return n


# ── 3) 高频历史表裁剪 ──────────────────────────────────────────────────────
def prune_history_tables(conn: sqlite3.Connection) -> int:
    """按保留期裁剪高频写入的历史表。"""
    total = 0
    for table, ts_col, keep_days in PRUNE_RULES:
        if not _table_exists(conn, table) or ts_col not in _columns(conn, table):
            continue
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - keep_days * 86400))
        try:
            cur = conn.execute(f"DELETE FROM \"{table}\" WHERE \"{ts_col}\" < ?", (cutoff,))
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            total += n
            print(f"  [PRUNE] {table}: 删除 {n} 行 (保留 {keep_days} 天, cutoff={cutoff})",
                  flush=True)
        except sqlite3.Error as e:
            print(f"  [!] {table} 裁剪失败: {e}", flush=True)
    conn.commit()
    return total


# ── 4) WAL / VACUUM ────────────────────────────────────────────────────────
def checkpoint_wal(conn: sqlite3.Connection) -> Tuple[int, int, int]:
    """执行 WAL checkpoint (TRUNCATE)，把 WAL 合并回主库并清零。"""
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"  [CKPT] wal_checkpoint(TRUNCATE) -> {tuple(row) if row else None}", flush=True)
    return tuple(row) if row else (0, 0, 0)


def vacuum_db(conn: sqlite3.Connection) -> bool:
    """VACUUM 回收 freelist 空间（需要独占库）。"""
    print("  [VACUUM] 开始（3GB+ 库可能要几分钟）…", flush=True)
    t0 = time.time()
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
        print(f"  [VACUUM] 完成，用时 {time.time()-t0:.1f}s", flush=True)
        return True
    except sqlite3.Error as e:
        print(f"  [!] VACUUM 失败: {e}", flush=True)
        return False


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="trident DB 体检与修复")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只体检）")
    ap.add_argument("--vacuum", action="store_true", help="修复后执行 VACUUM 回收空间")
    ap.add_argument("--checkpoint", action="store_true", help="修复后执行 WAL checkpoint")
    ap.add_argument("--blob-limit", type=int, default=None, help="覆盖 blob 上限（字节）")
    ap.add_argument("--keep-days", type=int, default=None, help="覆盖历史表保留天数")
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    args = ap.parse_args()

    if args.keep_days is not None:
        for i, (t, c, _d) in enumerate(PRUNE_RULES):
            PRUNE_RULES[i] = (t, c, args.keep_days)

    print(f"DB = {args.db}")
    print(f"  主库 { _file_size(args.db):.1f} MB | WAL { _wal_size(args.db):.1f} MB")
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row

    print("\n[1/5] schema 迁移（补 news_events 旧列 + asset_driver_nodes 索引）…")
    t0 = time.time()
    try:
        db.migrate(conn)
        print(f"  [ok] migrate 完成 ({time.time()-t0:.1f}s)", flush=True)
    except Exception as e:
        print(f"  [!] migrate 失败: {type(e).__name__}: {e}", flush=True)

    print("\n[2/5] 巨型 blob 体检…")
    scan_oversized_blobs(conn)

    print("\n[3/5] news_events 回填体检…")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM news_events WHERE COALESCE(headline,'')='' "
            "OR COALESCE(assets,'')='' OR COALESCE(fetched_at,0)=0"
        ).fetchone()
        print(f"  待回填行数 = {row[0]}", flush=True)
    except sqlite3.Error as e:
        print(f"  [!] {e}", flush=True)

    print("\n[4/5] 历史表体检…")
    for table, ts_col, keep_days in PRUNE_RULES:
        if _table_exists(conn, table) and ts_col in _columns(conn, table):
            row = conn.execute(f"SELECT COUNT(*) FROM \"{table}\"").fetchone()
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - keep_days * 86400))
            old = conn.execute(
                f"SELECT COUNT(*) FROM \"{table}\" WHERE \"{ts_col}\" < ?", (cutoff,)
            ).fetchone()
            print(f"  {table}: 共 {row[0]} 行, 超过 {keep_days} 天的 {old[0]} 行", flush=True)

    if not args.apply:
        print("\n(体检模式，未写库。加 --apply 执行修复)")
        conn.close()
        return 0

    print("\n[5/5] 执行修复…")
    cap_oversized_blobs(conn, args.blob_limit)
    backfill_news_events_legacy(conn)
    prune_history_tables(conn)

    if args.checkpoint:
        checkpoint_wal(conn)
    if args.vacuum:
        vacuum_db(conn)
        checkpoint_wal(conn)

    print(f"\n完成。主库 { _file_size(args.db):.1f} MB | WAL { _wal_size(args.db):.1f} MB")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
