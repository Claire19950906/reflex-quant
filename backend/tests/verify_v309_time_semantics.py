#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_v309_time_semantics.py — v309 时间口径审计的**独立可执行**验证脚本

为什么单独写这个文件 (而不是只靠 pytest):
  1. 它**不依赖 pytest**, 任何环境都能直接 `python3 verify_v309_time_semantics.py` 跑,
     便于在 CI/生产机上快速核对 SQLite 的实际比较语义。
  2. 它把"三种时间口径"的配对规则写成**一张表**, 一眼能看出哪张表该用哪套:
        列的落库语义             归一化函数            cutoff 函数
        ──────────────────────  ────────────────────  ─────────────────────
        BJ   ('T' 分隔北京墙钟)  sql_bj_ts_expr()      epoch_to_bj_str()
        UTC  (' ' 分隔 UTC 墙钟)  sql_utc_ts_expr()     utc_str()
        LOCAL(' ' 分隔本机墙钟)  sql_local_ts_expr()   local_str()
     配对正确 → 归一化输出的形态与语义都与 cutoff 一致 → 字典序 == 时间序。
  3. 它直接对着 **内存 SQLite** 跑真实的 SQL, 验证的是**行为**而非注释。

退出码: 0 = 全部通过; 1 = 有断言失败。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

_SRC_PYTHON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

from common import time_utils

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        _FAILURES.append(label)


def _table(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", rows)
    return conn


def _ids(conn, expr: str, lo: str, hi: str | None = None) -> set:
    if hi is None:
        sql = f"SELECT id FROM t WHERE {expr} >= ?"
        params = (lo,)
    else:
        sql = f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?"
        params = (lo, hi)
    return {r[0] for r in conn.execute(sql, params)}


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def verify_sqlite_basics() -> None:
    section("0. SQLite date 函数基础语义 (归一化表达式的基石)")
    conn = sqlite3.connect(":memory:")
    one = lambda sql: conn.execute("SELECT " + sql).fetchone()[0]
    check("无时区串按 UTC 解读 → 字面值不变",
          one("datetime('2026-10-25 09:00:00')") == "2026-10-25 09:00:00")
    check("'+8 hours' 是墙钟平移",
          one("datetime('2026-10-25 09:00:00', '+8 hours')") == "2026-10-25 17:00:00")
    check("跨日平移正确",
          one("datetime('2026-10-25 20:00:00', '+8 hours')") == "2026-10-26 04:00:00")
    check("解析失败 → NULL", one("datetime('not-a-time')") is None)
    check("带毫秒 → 截断到秒",
          one("datetime('2026-10-25 09:00:00.123')") == "2026-10-25 09:00:00")
    conn.close()


def verify_byte_order() -> None:
    section("1. 字节序: 'T' 与 ' ' 分隔的比较方向")
    check("'T'(0x54) > ' '(0x20)", "T" > " ")
    conn = _table([(1, "2026-10-25T08:00:00")])
    got = _ids(conn, "created_at", "2026-10-25 12:00:00")
    check("空格 cutoff 对 'T' 存储行 → >= 判 TRUE (边界日多算)",
          got == {1}, f"got={got}")
    conn.close()
    conn2 = _table([(1, "2026-10-25 08:00:00")])
    got2 = _ids(conn2, "created_at", "2026-10-25T12:00:00")
    check("'T' cutoff 对空格存储行 → >= 判 FALSE (边界日漏掉)",
          got2 == set(), f"got={got2}")
    conn2.close()


def verify_bj_semantics() -> None:
    section("2. BJ 口径 (raw_news.timestamp / trade_reflections_v2)")
    conn = _table([
        (1, "2026-10-25T16:00:00.000+08:00"),   # engine/ingest.py _ts()
        (2, "2026-10-25T16:00:00+08:00"),        # api_server _now_iso()
        (3, "2026-10-25 08:00:00"),              # db.py DEFAULT (UTC) → 北京 16:00
    ])
    expr = time_utils.sql_bj_ts_expr("created_at")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t"))
    check("三种写法归一化到同一北京墙钟",
          rows[1] == rows[2] == rows[3] == "2026-10-25 16:00:00", f"{rows}")
    for rid, val in rows.items():
        check(f"id={rid} 归一化输出 19 字符", len(val) == 19, f"{val!r}")
    conn.close()


def verify_utc_semantics() -> None:
    section("3. UTC 口径 (review_insights.created_at)")
    # 用 now 构造 fixture, 保证 cutoff 区间一定罩住该行 (不能用硬编码 epoch)
    now = int(time.time())
    stored = time_utils.utc_str(now)
    conn = _table([(1, stored)])   # db.py DEFAULT datetime('now') → 空格分隔 UTC
    expr = time_utils.sql_utc_ts_expr("created_at")
    val = conn.execute(f"SELECT {expr} FROM t").fetchone()[0]
    check("UTC 空格串不被平移", val == stored, f"{val!r} vs {stored!r}")
    check("UTC cutoff (utc_str) 命中同行",
          _ids(conn, expr, time_utils.utc_str(now - 3600),
               time_utils.utc_str(now + 3600)) == {1})
    # 本机时区非 UTC 时, 拿 local cutoff 去比 UTC 列必须打不中
    if time_utils.local_str(now) != time_utils.utc_str(now):
        check("LOCAL cutoff 打不中 UTC 列 (所以不能混用)",
              _ids(conn, expr, time_utils.local_str(now),
                   time_utils.local_str(now + 3600)) != {1})
    # 固定值回归: db.py DEFAULT 的形状
    conn2 = _table([(1, "2026-10-25 08:00:00")])
    val2 = conn2.execute(f"SELECT {expr} FROM t").fetchone()[0]
    check("UTC 固定样例不被平移", val2 == "2026-10-25 08:00:00", f"{val2!r}")
    conn.close()
    conn2.close()


def verify_local_semantics() -> None:
    section("4. LOCAL 口径 (causal_chains.created_at)")
    now = int(time.time())
    stored = time_utils.local_str(now)
    conn = _table([(1, stored)])
    expr = time_utils.sql_local_ts_expr("created_at")
    val = conn.execute(f"SELECT {expr} FROM t").fetchone()[0]
    check("localtime 墙钟不被平移", val == stored, f"{val!r} vs {stored!r}")
    check("local cutoff (local_str) 命中同行",
          _ids(conn, expr, time_utils.local_str(now - 3600),
               time_utils.local_str(now + 3600)) == {1})
    # 关键反证: 绝不能拿 BJ 口径去比 localtime 列
    bj_val = conn.execute(
        f"SELECT {time_utils.sql_bj_ts_expr('created_at')} FROM t").fetchone()[0]
    check("BJ 口径会平移 localtime 列 (所以不能混用)",
          bj_val != val, f"bj={bj_val!r} local={val!r}")
    # 本机时区非 UTC 时, 拿 UTC cutoff 去比 localtime 列必须打不中
    if time_utils.utc_str(now) != time_utils.local_str(now):
        check("UTC cutoff 打不中 localtime 列 (所以不能混用)",
              _ids(conn, expr, time_utils.utc_str(now),
                   time_utils.utc_str(now + 3600)) != {1})
    conn.close()


def verify_pairing_invariant() -> None:
    section("5. 配对不变式: 每个归一化函数配自己的 cutoff")
    now = int(time.time())
    cases = [
        ("BJ", time_utils.sql_bj_ts_expr, time_utils.epoch_to_bj_str,
         time_utils.epoch_to_bj_store),
        ("UTC", time_utils.sql_utc_ts_expr, time_utils.utc_str,
         time_utils.utc_str),
        ("LOCAL", time_utils.sql_local_ts_expr, time_utils.local_str,
         time_utils.local_str),
    ]
    for label, expr_fn, cutoff_fn, store_fn in cases:
        conn = _table([(1, store_fn(now))])
        expr = expr_fn("created_at")
        got = _ids(conn, expr, cutoff_fn(now))
        check(f"{label}: 配套 cutoff 命中自己的落库串", got == {1}, f"got={got}")
        conn.close()


def verify_cutoff_flavors() -> None:
    section("6. 三种 cutoff 口径互不相同 (防混用)")
    ts = 1793000000
    bj, utc, loc = (time_utils.epoch_to_bj_str(ts), time_utils.utc_str(ts),
                    time_utils.local_str(ts))
    check("BJ != UTC (永远差 8 小时)", bj != utc, f"{bj!r} vs {utc!r}")
    check("至少两种不同", len({bj, utc, loc}) >= 2, f"{bj!r}/{utc!r}/{loc!r}")


def verify_unparseable_excluded() -> None:
    section("7. 无法解析的值的行为")
    # 注意两种归一化实现的差别 (这是**已知且有意**的差异, 不是 bug):
    #   · sql_bj_ts_expr 走 datetime(col,'+8 hours') → 解析失败返回 NULL → 行被排除
    #   · sql_utc_ts_expr / sql_local_ts_expr 走纯 substr() → 不做解析校验,
    #     无法解析的值会原样参与字典序比较 (按字符序, 不是时间序)
    # 前者是 BJ 口径的既有行为 (P1 已钉死); 后两者是纯形态归一化, 只保证
    # "同形同语义" 的列能正确比较, 不对脏数据做额外处理。
    conn = _table([(1, "garbage-value"), (2, "2026-10-25 09:00:00")])
    bj_expr = time_utils.sql_bj_ts_expr("created_at")
    got_bj = _ids(conn, bj_expr, "2026-10-25 00:00:00", "2026-10-25 23:59:59")
    check("BJ 口径: 无法解析的行 → NULL → 被排除",
          got_bj == {2}, f"got={got_bj}")
    conn.close()

    # substr 型归一化: 脏数据不产生 NULL, 按字典序参与比较 → 行为可预测但非时间序
    conn2 = _table([(1, "garbage-value")])
    for expr_fn, label in ((time_utils.sql_utc_ts_expr, "UTC"),
                           (time_utils.sql_local_ts_expr, "LOCAL")):
        expr = expr_fn("created_at")
        val = conn2.execute(f"SELECT {expr} FROM t").fetchone()[0]
        check(f"{label} 口径: 脏数据原样透传 (substr 语义, 不返回 NULL)",
              val == "garbage-value", f"{val!r}")
    conn2.close()


def verify_substr_vs_datetime_difference() -> None:
    section("8. 两种归一化实现的差异 (BJ 用 datetime 校验, UTC/LOCAL 用 substr)")
    conn = _table([(1, "2026-10-25 09:00:00")])
    bj = conn.execute(
        f"SELECT {time_utils.sql_bj_ts_expr('created_at')} FROM t").fetchone()[0]
    utc = conn.execute(
        f"SELECT {time_utils.sql_utc_ts_expr('created_at')} FROM t").fetchone()[0]
    loc = conn.execute(
        f"SELECT {time_utils.sql_local_ts_expr('created_at')} FROM t").fetchone()[0]
    check("BJ 口径把空格串当 UTC 再 +8h",
          bj == "2026-10-25 17:00:00", f"{bj!r}")
    check("UTC 口径把空格串原样当 UTC", utc == "2026-10-25 09:00:00", f"{utc!r}")
    check("LOCAL 口径把空格串原样当本机墙钟", loc == "2026-10-25 09:00:00", f"{loc!r}")
    # 不变式: **只有 BJ 口径会做时区平移**; UTC/LOCAL 都只做*形态*归一化。
    #   · BJ 列存的是北京墙钟 → 空格串按 UTC 解读后必须 +8h 才回到北京墙钟
    #   · UTC/LOCAL 列存的**已经是**目标时区的墙钟 → 再平移就是双重偏移
    check("只有 BJ 口径会平移 (UTC/LOCAL 都保持原值)",
          bj != utc and bj != loc, f"{bj!r}/{utc!r}/{loc!r}")
    check("UTC 与 LOCAL 的表达式等价 → 二者的区别只在配套 cutoff",
          utc == loc, f"{utc!r} vs {loc!r}")
    conn.close()


def main() -> int:
    print("v309 时间口径验证 —— 独立可执行检查")
    print(f"time_utils 来自: {time_utils.__file__}")
    print(f"本机时区 (localtime 口径基准): {time.strftime('%Z %z')}")
    verify_sqlite_basics()
    verify_byte_order()
    verify_bj_semantics()
    verify_utc_semantics()
    verify_local_semantics()
    verify_pairing_invariant()
    verify_cutoff_flavors()
    verify_unparseable_excluded()
    verify_substr_vs_datetime_difference()

    print("\n" + "=" * 60)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项:")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
