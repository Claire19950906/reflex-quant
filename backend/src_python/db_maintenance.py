"""trident 事件总线 —— 数据库体检与修复工具 (v300-audit / v310)

背景
----
生产库 `trident_event_bus.db` 先后踩过两类坑，都在这里被收敛：

1. **单行超大 blob**
   `asset_driver_nodes` 第 1 行的 `evidence` 字段长到 **872,422,822 字节**。
   该表任何全表扫描（例如 `MAX(updated_at)`）都要把这 872MB 读一遍 →
   `/api/analysis/kg-stats`、`/api/review/causal-graph/stats` 稳定 60s+ 超时。
   → `cap_oversized_blobs()` 用服务端 `substr()` 截断，不把 blob 读进 Python 内存。
   → 写侧已在 `causal_graph.py` 加 64KB 上限（`_MAX_EVIDENCE_BYTES`），
     根因已封堵；本模块的截断只是存量兜底。

2. **schema 漂移**
   `news_events` 被迁移到新 schema 后丢失 `headline/body/assets/fetched_at`，
   导致 SSE 事件流报错、三条新闻入库全部失败、因果图谱 news_to_move 恒空。
   → `backfill_news_events_legacy()` 从 raw_news 回填内容（存量）。
   → v310 修复写侧：`timeseries.record_news_event()` 的 INSERT 一直**没写**这四列
     （db.py 只用 ADD COLUMN 把列补了回来），所以新入库的新闻这四列恒为 ''/0，
     属于静默降级。已在 timeseries.py 补上，并让 api_server / webhook 直接传原文。

3. **反思三维度缺失 (v309 P0-1)**
   `trade_reflections_v2.dimension/subcategory` 大量为空且存在非法值
   （如 `decision_engine`），导致 `/api/v237/reflection-dimension-stats` 的
   `n_reflections` 近乎为 0，维度维度的复盘统计整片失效。
   → `backfill_reflection_dimensions()` 依 `reflection_dimension_map` 解析回填；
     解析不出的一律留空，**绝不塞默认桶**。

4. **assets 列口径错乱 (v310 补充)**
   `news_events` 有两个品种列，职责不同：
     - `asset`（单数）：**权威**，写侧每次都填，值为规范品种码
       （WTI / XAU / BTC / ETH / CN_STOCK / NONE…）；
     - `assets`（复数）：**历史列**，消费方按逗号 split 取品种码
       （bridge_server._news_row_to_api_event、calc_event_move_attribution）。

   但 `raw_news.gate_assets` 是门卫标签的 JSON 数组，混着板块类别
   （美股/原油/黄金/个股/加密主流/山寨）和品种码（TSLA/META/NVDA…）。
   v300 的回填把它直接灌进 `assets`，于是：
     - split 消费方拿到 `["美股"` 这种垃圾值，顶掉单数列里已规范好的品种；
     - json.loads 消费方（bridge 的星级筛选）解析不了逗号串，静默丢行。

   → 口径统一：`assets` 只放**品种码**，逗号分隔
     （`db.normalize_news_assets()`，板块类别不入此列；
      消费方在 assets 为空时会回落到单数 `asset` 列）。
   → 过滤一律同时匹配两列，否则 WTI/XAU 这类只有单数列有值的品种
     一条都查不到（WTI 1280 行 / XAU 1044 行，此前全查不到）。

5. **eastmoney 时间戳快 8 小时 (v310 补充)**
   东财的 `showTime` 是北京时间，而 `bridge_server._parse_iso_ts()` 一律
   `replace(tzinfo=timezone.utc)` 按 UTC 解释 —— 290 行东财新闻的 ts 全部
   比真实时间快 8 小时，63 行的 ts 直接落在「未来」，
   与行情异动做时间窗对齐（news_ts vs move_ts）整体错位 8 小时。
   → 新增 `bridge_server._parse_iso_ts_cst()` 按 UTC+8 解析（东财专用，
     techflow 的 created_at 本就是 UTC，不动）；
   → 存量 290 行的 ts / fetched_at 统一回拨 28800 秒。
   → 代码与存量必须**同时生效**：只改一边会让 UNIQUE(news_id, ts) 失配
     而插出重复行（已验证：平移后冲突数 0、全库未来 ts 归零）。

6. **bridge 撞锁即丢新闻 (v310 补充)**
   `bridge_server._news_pull_sync()` 原来逐条 INSERT，撞到写锁只 `print`
   一句就 `continue` —— 那条新闻**永久丢失**（不会补写）。
   `bridge.err.log` 里累计 3112 条 "news insert error ... database is locked"
   （首条可追到 2026-10-24，是长期既有问题，非本次改动引入）。
   → 拆成 `_insert_news_batch()` + 整批重试：锁错误上抛，
     调用方按 0.5/1/2s 退避重试 3 次；非锁类错误仍只跳过单条。
   → 效果：修复后一个完整拉取周期内 insert_error 增量 **0**，
     撞锁时锁计数 +1 但新闻成功入库（此前是直接丢）。

2026-10-26 现场处置记录
-----------------------
- 库曾涨到 **7782.4MB**，其中约 **7159MB 是已删除行留下的 freelist**（有效数据仅 623MB）。
  停写 → `VACUUM INTO` 备份 → 原地 VACUUM → **623.0MB**，回收 7.16GB，行数 118/118 表一致。
- `backend/` 下 12 个历史 `.bak` 共 13.77GB，已清到只剩最新的一个（其余不在 git 索引内）。
- `source_status_history` 按 14 天保留期裁剪（见 PRUNE_RULES）。
- v310 修复执行结果（`--apply --checkpoint --vacuum`）：
  - news_events 回填 7248 行；`fetched_at` 填充率 0% → **100%**；
    headline 空值 6360 → 4313（余下的 raw_news 原文已被轮转删除，物理性丢失）。
  - trade_reflections_v2：dimension 空 274 → 16，非法值 19 个已清除；
    `/api/v237/reflection-dimension-stats` 的 n_reflections **2 → 276**。
  - source_status_history 375,280 → 207,465 行（删 167,815 行超 14 天的）。
  - 118 张表逐行对照，**只有 source_status_history 一张表行数变化**。
- assets 口径归一（`clean_assets.py`）：门卫 JSON 行 5030 → 300 行转品种码
  + 4730 行清空；归一后全库残留 JSON 行 = 0。
  单数 `asset` 列经与回填前备份逐行对照，**10376 行不一致数为 0**（未动过）。
- 最终验收（服务 20:13 重启加载全部修复后，跑满 2 个拉取周期）：
  - news_events 10,464 行，headline 6,151（58%），fetched_at 10,457（**99%**）；
  - 全库 ts 落在未来 = **0**；assets 残留 JSON = **0**；
  - 本周期新增 10 行，headline 10/10、fetched_at 10/10、ts 无未来值；
  - trade_reflections_v2 295 行 / dimension 非空 279（空值仍是 16，未新增）；
  - `/api/v237/reflection-dimension-stats` n_reflections = 276；
  - 前端 300 条事件里 target_asset 垃圾值 = **0**；
  - 15 个 launchd job 全部 exit 0。

用法
----
    python3 db_maintenance.py                 # 只体检，不写库
    python3 db_maintenance.py --apply --vacuum   # 体检 + 修复 + 回收空间

注意
----
`--vacuum` 需要独占数据库（会短暂阻塞所有写者），建议先停掉 api/bridge 服务。
`--apply` 里的 `prune_history_tables()` 会按保留期真删数据，**跑之前先备份**。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

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



# ── 1b) 反思三维度回填 ────────────────────────────────────────────────────
def backfill_reflection_dimensions(conn: sqlite3.Connection, apply: bool = False) -> Dict[str, int]:
    """把 trade_reflections_v2 的 dimension / subcategory 回填成合法取值。

    v309 P0-1: /api/v237/missed-signals/by-dimension 读的是
    dimension ∈ {A,B,C} + 12 个合法 subcategory。实际库里的取值是:
      · 274 行 dimension=''            —— 没写过
      · 19 行 dimension='decision_engine' + subcategory='BUY|SELL' —— 非法值
    两者都会被前端的 by-dimension 统计直接丢掉 (n=0)。

    口径走 reflection_dimension_map.py (单一权威, 零依赖):
      1. decision_errors.error_code    (E001a-E033, 最精确)
      2. attribution_reviews.reason_code (LLM 失败框架)
      3. v188 ontology layer/sub_layer   (兜底)
    解析不出来就写空串 —— 绝不塞默认桶, 那会让三维度统计失真。

    **不改变行数** (验收分母不变): 只 UPDATE 已有的行。
    """
    from review import reflection_dimension_map as rdm

    stats = {"total": 0, "resolved": 0, "unresolved": 0, "updated": 0,
             "cleared_illegal": 0}
    if not _table_exists(conn, "trade_reflections_v2"):
        print("  [!] trade_reflections_v2 不存在, 跳过", flush=True)
        return stats
    cols = _columns(conn, "trade_reflections_v2")
    need = {"id", "decision_id", "dimension", "subcategory"}
    if not need <= cols:
        print(f"  [!] 缺列 {sorted(need - cols)} —— 先跑 db.migrate()", flush=True)
        return stats

    rows = conn.execute(
        "SELECT id, decision_id, layer, sub_layer, signal_name, "
        "       error_layer, error_sub_layer, error_signal, "
        "       dimension, subcategory "
        "FROM trade_reflections_v2"
    ).fetchall()
    stats["total"] = len(rows)
    for r in rows:
        (rid, did, layer, sub_layer, signal_name,
         e_layer, e_sub_layer, e_signal, dimension, subcategory) = r
        codes: List[str] = []
        reason_code = None
        try:
            for x in conn.execute(
                "SELECT error_code FROM decision_errors WHERE decision_id = ?", (did,)
            ).fetchall():
                if x and x[0]:
                    codes.append(x[0])
        except sqlite3.Error:
            pass
        try:
            x = conn.execute(
                "SELECT reason_code FROM attribution_reviews "
                "WHERE trade_id = ? ORDER BY id DESC LIMIT 1", (did,)
            ).fetchone()
            if x and x[0] is not None:
                reason_code = x[0]
        except sqlite3.Error:
            pass
        hit = rdm.resolve_dimension(
            error_codes=codes,
            reason_code=reason_code,
            layer=layer or e_layer or "",
            sub_layer=sub_layer or e_sub_layer or "",
            signal_name=signal_name or e_signal or "",
        )
        new_dim = new_sub = ""
        if hit:
            d, s = hit
            if rdm.is_valid_subcategory(d, s):
                new_dim, new_sub = d, s
        if new_dim:
            stats["resolved"] += 1
        else:
            stats["unresolved"] += 1
        if (new_dim != (dimension or "")) or (new_sub != (subcategory or "")):
            stats["updated"] += 1
            if (dimension or "") and dimension not in ("A", "B", "C"):
                stats["cleared_illegal"] += 1
            if apply:
                conn.execute(
                    "UPDATE trade_reflections_v2 SET dimension=?, subcategory=? WHERE id=?",
                    (new_dim, new_sub, rid),
                )
    conn.commit()
    print(f"  共 {stats['total']} 行 | 可解析 {stats['resolved']} | "
          f"解析不出 {stats['unresolved']} | 需更新 {stats['updated']} "
          f"(其中清除非法值 {stats['cleared_illegal']})", flush=True)
    if not apply:
        print("  (体检模式, 未写库)", flush=True)
    return stats


# ── 2) news_events 旧列回填 ───────────────────────────────────────────────
def backfill_news_events_legacy(conn: sqlite3.Connection) -> int:
    """从 raw_news 回填 news_events 的 headline / body / assets / fetched_at。

    news_events 被迁移到新 schema 时丢了旧列，旧列内容可以从 raw_news 复原：
      headline   ← raw_news.content 前 300 字
      body       ← raw_news.content 301~2300 字
      assets     ← raw_news.gate_assets 里**属于品种代码**的部分（逗号分隔）
      fetched_at ← news_events.ts

    assets 为什么要多一道归一（v310 补充）
    ------------------------------------
    `raw_news.gate_assets` 是门卫(L3)标签，JSON 数组，值混着两类东西：
      - 板块类别（中文）：美股 / 原油 / 黄金 / 个股 / 加密主流 / 山寨
      - 品种代码：TSLA / META / NVDA / 000660 …
    而 `news_events.assets` 的消费方一律按**逗号 split** 取品种代码
    （bridge_server._news_row_to_api_event / calc_event_move_attribution），
    直接把 JSON 数组灌进去会让它们拿到 `["美股"` 这种垃圾值，
    还顶掉单数 `asset` 列里已经规范好的品种。
    → 只取品种代码，用 db.normalize_news_assets 归一；板块类别不入此列，
      消费方在 assets 为空时会回落到单数 asset 列。
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
    rows = conn.execute(
        """
        SELECT ne.id, ne.ts,
               COALESCE(ne.headline, '')   AS headline,
               COALESCE(ne.body, '')       AS body,
               COALESCE(ne.assets, '')     AS assets,
               COALESCE(ne.fetched_at, 0)  AS fetched_at,
               COALESCE(rn.content, '')    AS content,
               COALESCE(rn.gate_assets, '') AS gate_assets
          FROM news_events ne
          LEFT JOIN raw_news rn ON rn.id = ne.news_id
         WHERE COALESCE(ne.headline, '') = ''
            OR COALESCE(ne.assets, '') = ''
            OR COALESCE(ne.fetched_at, 0) = 0
        """
    ).fetchall()
    n = 0
    for (rid, ts, headline, body, assets, fetched_at, content, gate_assets) in rows:
        new_headline = headline or (content[:300] if content else "")
        new_body = body or (content[300:2300] if content else "")
        new_assets = assets or db.normalize_news_assets(gate_assets)
        new_fetched = fetched_at or int(ts or 0)
        if (new_headline == headline and new_body == body
                and new_assets == assets and new_fetched == fetched_at):
            continue
        conn.execute(
            "UPDATE news_events SET headline=?, body=?, assets=?, fetched_at=? WHERE id=?",
            (new_headline, new_body, new_assets, new_fetched, rid),
        )
        n += 1
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

    print("\n[3/5] 反思三维度体检 (dimension / subcategory)…")
    try:
        backfill_reflection_dimensions(conn, apply=False)
    except Exception as e:
        print(f"  [!] 维度体检失败: {type(e).__name__}: {e}", flush=True)

    print("\n[4/5] news_events 回填体检…")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM news_events WHERE COALESCE(headline,'')='' "
            "OR COALESCE(assets,'')='' OR COALESCE(fetched_at,0)=0"
        ).fetchone()
        print(f"  待回填行数 = {row[0]}", flush=True)
    except sqlite3.Error as e:
        print(f"  [!] {e}", flush=True)

    print("\n[5/5] 历史表体检…")
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

    print("\n[6/6] 执行修复…")
    # 先修维度（只 UPDATE 已有行, 行数不变 → 验收分母不变）,
    # 再做 blob 截断兜底, 最后裁历史表。
    backfill_reflection_dimensions(conn, apply=True)
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
