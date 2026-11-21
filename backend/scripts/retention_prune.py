#!/usr/bin/env python3
"""retention_prune.py — 追加型表的保留策略（默认 dry-run，不删任何数据）

背景（2026-11-15 实测）：
  trident_event_bus.db 1.4 GB / 151 表，但库里只有 **34 天** 历史
  （最早 ts = 2026-10-12）。体积不是"攒了几年"，而是写入速率高：
    source_status_history ~4.2 万行/天，macro_snapshots ~2.1 万行/天，
    market_ticks ~2.6 万行/天，ai_call_stats ~1.2 万行/天
  → 合计约 10 万行/天。按这个速率，一年就是 3,600 万行、数个 GB。

  所以本脚本的目标不是"一次瘦身一半"，而是**给增长装上阀门**：
  设定每张表的保留窗口，让库的体积进入稳态，而不是线性增长。

用法：
  # 只算不删（默认）
  python3 backend/scripts/retention_prune.py

  # 看某张表在不同窗口下会删多少
  python3 backend/scripts/retention_prune.py --table source_status_history --days 14

  # 真删（分批、每批 5000 行、每批一提交，尽量少占用写锁）
  python3 backend/scripts/retention_prune.py --apply

  # 真删后导出压缩副本（不在原地 VACUUM —— 原地 VACUUM 需要独占锁，
  # 会和运行中的 engine/api 正面冲突）
  python3 backend/scripts/retention_prune.py --apply --compact-to /tmp/trident_compact.db

安全约束：
  * 默认 dry-run；--apply 才写。
  * 只删 ts 早于 cutoff 的行，不做任何 UPDATE。
  * 每张表删除前打印一次样本（前 3 行），让人确认确实是要删的那批。
  * 不删"维表/事实表"（decisions、reflections、trades 等），本脚本只认下面 TABLES 里的追加型表。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(REPO_ROOT, "backend", "trident_event_bus.db")


# 追加型表清单：table -> (ts 列类型, 默认保留天数, 说明)
# 只允许列在这里的表被删除；新增表必须显式加入并写清理由。
#
# 刻意不在清单里：ai_decisions。它是决策事后归因的底账 —— 一旦被裁掉，
# "闸门拦对率"这类评估就再也没法重算（scripts/gate_baseline.py 依赖它）。
# 要加进来之前，先想清楚基线评估的样本从哪来。
TABLES: Dict[str, Dict[str, Any]] = {
    "source_status_history": {
        "ts_kind": "text", "days": 7,
        "why": "信息源逐次探活记录（约 5.5 万行/天），只在诊断近期故障时有意义；v479 从 14 天收到 7 天 —— 实测这张表 62MB + 38MB 索引，是库里第二大开销，而 7 天足够回看一周宕机",
    },
    "market_ticks": {
        "ts_kind": "int", "days": 15,
        "why": "秒级行情快照；回测走 kline_bars / ohlcv，不依赖它",
        # 已有 timeseries.py 的清理，二者窗口必须一致（不在两处各写一个数字）
        "managed_elsewhere": "timeseries.py::prune_ticks",
    },
    "macro_snapshots": {
        "ts_kind": "int", "days": 14,
        "why": "宏观指标快照（约 8.7 万行/天，多指标多快照膨胀）；v479 从 30 天收到 14 天 —— 实测 520k 行 / 81MB 占库最大，而页面宏观图只画近两周",
    },
    "ai_call_stats": {
        "ts_kind": "int", "days": 30,
        "why": "LLM 调用计量（token/延迟）；成本对账默认近 24h、看板回看 30 天足够",
    },
    "hermes_observations": {
        "ts_kind": "int", "days": 30, "ts_col": "ts",
        "why": "反思/观察流水；近期复盘与归因只需近 30 天",
    },
    "news_events": {
        "ts_kind": "int", "days": 30, "ts_col": "ts",
        "why": "新闻事件流水；决策与结算窗口内使用，30 天外不再回看",
    },
    "event_time_candidates": {
        "ts_kind": "int", "days": 30, "ts_col": "created_at",
        "why": "事件时点候选；归因回看窗口短，按入库时间 created_at 裁剪",
    },
    # ── 行情 / 指标历史（v470 纳入保留；实时决策只取近期，旧行只服务回测回看）──
    "ohlcv_daily": {
        "ts_kind": "int", "days": 400,
        "why": "日线 K 线；背离扫描最多取 3000 根（≈8.2 年），但库里 >1 年的多为早期脏/测试数据，"
               "保留近 400 天足够覆盖常规日线策略回看；本表无 id 主键，按 ts 分批删。",
    },
    "ohlcv_intraday": {
        "ts_kind": "int", "days": 180,
        "why": "日内 K 线；策略与背离回看均为近期窗口，180 天覆盖半年分钟级复盘；本表无 id 主键。",
    },
    "kline_bars": {
        "ts_kind": "int_ms", "days": 180,
        "why": "多源 K 线（ts 为毫秒）；近期行情与形态识别取最近若干根，180 天滚动窗口足够。",
    },
    "external_indicators": {
        "ts_kind": "int", "days": 180,
        "why": "外部指标流水（资金费率/持仓量/波动率等）； regime 与归因只看近期，180 天前不再回看。",
    },
    "flow_snapshots": {
        "ts_kind": "int", "days": 30,
        "why": "资金流/涨跌快照（秒级 ts）；面板回看 20 日涨跌，30 天覆盖；表内有早期测试脏时间戳，"
               "保留窗口外一并清掉。",
    },
    "feed_health": {
        "ts_kind": "text", "days": 14, "ts_col": "ts",
        "why": "信息源探活/延迟流水；面板按小时窗口、数据表按天分页 LIMIT 读取，14 天足够诊断近期故障。",
    },
    "crowd_source_items": {
        "ts_kind": "text_iso", "days": 30, "ts_col": "collected_at",
        "why": "情绪/舆情原始条目（source+external_id 唯一，重复采集只更新）；面板回看近 30 天热度，"
               "更早的原始条目不再回看；时间列为 ISO 8601 文本（带 T）。",
    },
}

BATCH = 5000


def _connect(path: str, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _table_bytes(conn: sqlite3.Connection) -> Dict[str, int]:
    """表 + 索引的磁盘占用。

    优先 dbstat（精确）；某些 Python 构建的 sqlite 没编译 DBSTAT vtab，
    退化为「采样 400 行算平均行宽 × 行数」的估算，宁可标成估算也不报 0。
    """
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
        ).fetchall()
        return {name: int(size or 0) for name, size in rows}
    except sqlite3.Error:
        out: Dict[str, int] = {}
        for table in TABLES:
            try:
                sample = conn.execute(
                    f'SELECT * FROM "{table}" LIMIT 400'
                ).fetchall()
                if not sample:
                    continue
                avg = sum(len(repr(r)) for r in sample) / len(sample)
                total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                out[table] = int(avg * total)   # 估算：不含索引
            except sqlite3.Error:
                continue
        out["__estimated__"] = 1
        return out


def _cutoff(kind: str, days: int) -> Any:
    now = datetime.now()
    start = now - timedelta(days=days)
    if kind == "int":
        return int(start.timestamp())
    if kind == "int_ms":
        return int(start.timestamp() * 1000)
    if kind == "text_iso":
        # ISO 8601 文本（带 T 分隔，如 2026-10-30T19:05:46）；cutoff 必须同格式才能字符串比较
        return start.strftime("%Y-%m-%dT%H:%M:%S")
    return start.strftime("%Y-%m-%d %H:%M:%S")


def _count(conn: sqlite3.Connection, table: str, cutoff: Any, ts_col: str = "ts") -> int:
    return conn.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{ts_col}" < ?', (cutoff,)
    ).fetchone()[0]


def _span(conn: sqlite3.Connection, table: str, ts_col: str = "ts") -> Dict[str, Any]:
    row = conn.execute(
        f'SELECT MIN("{ts_col}"), MAX("{ts_col}"), COUNT(*) FROM "{table}"'
    ).fetchone()
    lo, hi, total = row

    def _fmt(v: Any) -> Any:
        if not isinstance(v, int):
            return v
        try:
            if v > 1_000_000_000_000:  # 毫秒口径
                return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
            if v > 1_000_000_000:  # 秒口径
                return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            return v  # 脏时间戳原样保留，不让展示层炸掉
        return v

    return {"min_ts": _fmt(lo), "max_ts": _fmt(hi), "rows": total}


def plan(conn: sqlite3.Connection, only: Optional[str], days_override: Optional[int]) -> List[Dict[str, Any]]:
    sizes = _table_bytes(conn)
    estimated = bool(sizes.get("__estimated__"))
    out: List[Dict[str, Any]] = []
    for table, cfg in TABLES.items():
        if only and table != only:
            continue
        days = days_override if days_override is not None else cfg["days"]
        cutoff = _cutoff(cfg["ts_kind"], days)
        ts_col = cfg.get("ts_col", "ts")
        span = _span(conn, table, ts_col)
        to_delete = _count(conn, table, cutoff, ts_col)
        total = span["rows"] or 1
        frac = to_delete / total
        table_b = sizes.get(table, 0)
        idx_b = sum(v for k, v in sizes.items() if k.startswith(f"idx_{table}") or k.startswith(f"sqlite_autoindex_{table}"))
        out.append({
            "table": table,
            "days": days,
            "cutoff": cutoff,
            "ts_col": ts_col,
            "rows": span["rows"],
            "rows_to_delete": to_delete,
            "delete_pct": round(frac * 100, 1),
            "table_mb": round(table_b / 1048576, 1),
            "index_mb": round(idx_b / 1048576, 1),
            "est_freed_mb": round((table_b + idx_b) * frac / 1048576, 1),
            "min_ts": span["min_ts"],
            "max_ts": span["max_ts"],
            "why": cfg["why"],
            "managed_elsewhere": cfg.get("managed_elsewhere", ""),
            "size_estimated": estimated,
        })
    return out


def apply_table(conn: sqlite3.Connection, table: str, cutoff: Any, dry: bool,
                ts_col: str = "ts") -> int:
    """分批删除；返回实际删除行数。

    v466：BEGIN IMMEDIATE 撞锁时退避重试，不再一次 SQLITE_BUSY 就整批退出。
    删除是低优先级维护操作，让一让在线写者；单批拿不到锁就 sleep 后重试，最多 8 次。

    v470：部分行情表（ohlcv_daily / ohlcv_intraday）没有自增 id 主键。SQLite 的
    DELETE 不支持 LIMIT，无主键表回退为「按 ts 游标」分批：每批取 ts < cutoff 的
    前 BATCH 行的 ts 值，再 DELETE WHERE ts IN (...)；同 ts 多行情行靠下一批继续收敛。
    """
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    # 有显式 id 用 id；否则用 SQLite 隐式 rowid（每行都有，覆盖无主键行情表）。
    key_col = "id" if "id" in cols else "rowid"
    deleted = 0

    def _commit_batch(sql: str, bind: List[Any]) -> int:
        batch_ok = False
        for attempt in range(8):
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(sql, bind)
                    conn.execute("COMMIT")
                    batch_ok = True
                except sqlite3.Error:
                    conn.execute("ROLLBACK")
                    raise
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                backoff = min(0.5 * (2 ** attempt), 5.0)
                time.sleep(backoff)
        if not batch_ok:
            print(f"\n  [{table}] 批次抢锁失败，跳过本批（下一轮 retention 再删）", flush=True)
            return -1
        return cur.rowcount or 0

    while True:
        keys = [r[0] for r in conn.execute(
            f'SELECT {key_col} FROM "{table}" WHERE "{ts_col}" < ? LIMIT ?',
            (cutoff, BATCH),
        ).fetchall()]
        if not keys:
            break
        if dry:
            n = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{ts_col}" < ?', (cutoff,)
            ).fetchone()[0]
            deleted += n
            break
        placeholders = ",".join("?" * len(keys))
        sql = f'DELETE FROM "{table}" WHERE {key_col} IN ({placeholders})'
        bind: List[Any] = keys
        n = _commit_batch(sql, bind)
        if n < 0:
            break
        deleted += n
        print(f"  [{table}] 已删除 {deleted} 行 …", end="\r", flush=True)
        time.sleep(0.02)  # 让出写锁，避免把 engine 的分析循环饿死
    print()
    return deleted


def compact(conn: sqlite3.Connection, dest: str) -> None:
    if os.path.exists(dest):
        raise SystemExit(f"目标已存在，拒绝覆盖：{dest}")
    print(f"[compact] VACUUM INTO {dest} （不占原库写锁的常规锁，但会读全库）")
    conn.execute(f"VACUUM INTO '{dest}'")
    print(f"[compact] 完成：{os.path.getsize(dest) / 1048576:.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description="追加型表的保留策略（默认 dry-run）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--table", default=None, help="只处理某张表")
    ap.add_argument("--days", type=int, default=None, help="覆盖该表的保留天数")
    ap.add_argument("--apply", action="store_true", help="真的删除（不加则只统计）")
    ap.add_argument("--compact-to", default=None, help="删完后把库压缩导出到该路径")
    ap.add_argument("--json", default=None, help="把计划写成 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在：{args.db}")

    dry = not args.apply
    conn = _connect(args.db, readonly=dry)
    try:
        rows = plan(conn, args.table, args.days)
        if not rows:
            raise SystemExit("没有匹配的表")

        print(f"库：{args.db}  ({os.path.getsize(args.db)/1048576:.1f} MB)  模式：{'DRY-RUN（不删）' if dry else 'APPLY（真删）'}")
        print("-" * 112)
        head = f"{'表':<24}{'窗口':>6}{'总行':>12}{'待删':>12}{'占比':>8}{'表MB':>8}{'索引MB':>9}{'可回收MB':>11}"
        print(head)
        for r in rows:
            print(f"{r['table']:<24}{r['days']:>5}d{r['rows']:>12,}{r['rows_to_delete']:>12,}"
                  f"{r['delete_pct']:>7}%{r['table_mb']:>8}{r['index_mb']:>9}{r['est_freed_mb']:>11}")
        print("-" * 112)
        total_free = sum(r["est_freed_mb"] for r in rows)
        print(f"预计可回收：{total_free:.1f} MB（占库 {total_free/(os.path.getsize(args.db)/1048576)*100:.1f}%）")
        if rows and rows[0].get("size_estimated"):
            print("注：本机 sqlite 未编译 dbstat，表/索引体积为「采样 400 行 × 行数」的估算值，"
                  "仅用于判断量级；行数与待删数目是精确统计。")

        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"db": args.db, "mode": "apply" if args.apply else "dry-run",
                           "generated_at": datetime.now().isoformat(timespec="seconds"),
                           "tables": rows, "est_freed_mb_total": round(total_free, 1)}, f,
                          ensure_ascii=False, indent=2)
            print(f"计划已写入：{args.json}")

        if dry:
            print("\n未删除任何数据。要真删请追加 --apply。")
            return 0

        # APPLY：先给出每张表将被删的样本，再删
        for r in rows:
            if not r["rows_to_delete"]:
                continue
            ts_col = r.get("ts_col", "ts")
            sample = conn.execute(
                f'SELECT * FROM "{r["table"]}" WHERE "{ts_col}" < ? LIMIT 3', (r["cutoff"],)
            ).fetchall()
            print(f"\n[{r['table']}] cutoff={r['cutoff']} 样本(前 3 行)：{sample}")
            n = apply_table(conn, r["table"], r["cutoff"], dry=False, ts_col=ts_col)
            print(f"[{r['table']}] 删除完成：{n:,} 行")

        if args.compact_to:
            compact(conn, args.compact_to)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
