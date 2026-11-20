# -*- coding: utf-8 -*-
"""
test_time_utils.py — v309 P1: 时间口径唯一入口的单测

为什么要有这个文件:
  v309 P0 把 epoch 整数换成字符串比较, 看似修好了, 但实际上:
    · trade_reflections_v2.created_at 列里**同时有两种落库格式**
      ('T' 分隔=本机本地时间 / ' ' 分隔=UTC)
    · 直接字典序比较在同一日期内必然错乱 ('T' 的 ASCII 比 ' ' 大)
    · 而且校验脚本第 8 项只 grep 了一个字符串 → **假 PASS**
  这类 bug 只有"用真实数据形状做行为测试"才拦得住, 所以这里覆盖:
    1. epoch ↔ 北京串 ↔ epoch 的往返自洽
    2. 两种落库格式的归一化表达式给出**相同且正确**的窗口结果
    3. UTC 语义列 (feed_health.ts) 的归一化
    4. 反向断言: 证明检查本身有效 (不用归一化就会错)

纯离线: 不碰 DB / config / dotenv。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_SRC_PYTHON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

from common import time_utils


# ── 1. 往返自洽 ────────────────────────────────────────────────────────────
def test_epoch_to_bj_str_roundtrip():
    """epoch → 北京串 → epoch 必须回到同一个 epoch。"""
    ts = time_utils.bj_str_to_epoch("2026-10-25 08:00:00")
    assert ts is not None
    assert time_utils.epoch_to_bj_str(ts) == "2026-10-25 08:00:00"


def test_bj_str_uses_beijing_offset():
    """北京 08:00 == UTC 00:00 —— 这是整个项目的时间口径基准。"""
    ts = time_utils.bj_str_to_epoch("2026-10-25 08:00:00")
    assert time_utils.utc_str(ts) == "2026-10-25 00:00:00"


def test_store_format_is_t_separated():
    """落库格式必须是 'T' 分隔 —— 与 paper_reflection_worker 既有数据同格式。"""
    ts = time_utils.bj_str_to_epoch("2026-10-25 08:00:00")
    assert time_utils.epoch_to_bj_store(ts) == "2026-10-25T08:00:00"


@pytest.mark.parametrize("text", [
    "2026-10-25 08:00:00",   # 空格 (比较格式)
    "2026-10-25T08:00:00",   # T 分隔 (落库格式)
])
def test_both_formats_parse_to_same_instant(text):
    """两种落库格式必须解析成同一时刻。"""
    assert time_utils.bj_str_to_epoch(text) == time_utils.bj_str_to_epoch(
        "2026-10-25 08:00:00")


def test_utc_semantic_differs_from_bj_semantic():
    """同一串文本, 按 UTC 解读与按北京解读必须差 8 小时 (别混用口径)。"""
    a = time_utils.utc_str_to_epoch("2026-10-25 00:00:00")
    b = time_utils.bj_str_to_epoch("2026-10-25 00:00:00")
    assert a is not None and b is not None
    assert a - b == 8 * 3600


def test_iso_with_offset_is_respected():
    """带 Z / +08:00 的 ISO 必须按自带时区换算, 不能当裸串。"""
    z = time_utils.bj_str_to_epoch("2026-10-25T00:00:00Z")
    off = time_utils.bj_str_to_epoch("2026-10-25T08:00:00+08:00")
    assert z == off, "Z 与 +08:00 应指向同一时刻"


def test_unparseable_returns_none():
    for bad in ("", "   ", None, "not-a-time", "2026-13-45 99:99:99"):
        assert time_utils.bj_str_to_epoch(bad) is None, f"{bad!r} 不该解析成功"


# ── 2. 窗口过滤 (行为测试: 复现真实的两格式混存) ──────────────────────────
def _make_table(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", rows)
    return conn


def test_window_filter_handles_both_storage_formats():
    """两种格式混存时, 窗口过滤必须给出正确结果。

    数据形状来自真实库:
      · id=1 '2026-10-25T01:00:00'  ← paper_reflection_worker (本地时间=北京)
      · id=2 '2026-10-25 09:00:00'  ← DB DEFAULT / bridge_server (UTC)
    北京窗口 08:00~18:00 内应命中 id=2 (UTC 09:00 = 北京 17:00),
    不应命中 id=1 (北京 01:00, 在窗口外)。
    """
    conn = _make_table([
        (1, "2026-10-25T01:00:00"),
        (2, "2026-10-25 09:00:00"),
        (3, "2026-10-25T09:00:00"),   # 北京 09:00 → 窗口内
        (4, "2026-10-25 01:00:00"),   # UTC 01:00 = 北京 09:00 → 窗口内
        (5, "2026-10-24T23:00:00"),   # 北京 10-24 23:00 → 窗口外
    ])
    expr = time_utils.sql_bj_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?",
        ("2026-10-25 08:00:00", "2026-10-25 18:00:00"))}
    assert got == {2, 3, 4}, f"窗口过滤结果错: 期望 {{2,3,4}}, 实际 {got}"
    conn.close()


def test_naive_string_compare_is_wrong():
    """反向断言: 不做归一化的直接字典序比较**必须**是错的。

    这条保证上面的测试不是"恒真"—— 如果哪天归一化表达式退化失效,
    这条会先炸。

    朴素字典序比较有两个方向的错:
      · **漏掉**: 'T'(0x54) > ' '(0x20), 所以同一天的 'T' 行必然 > 空格 cutoff
        的上界 → 无论真实时间多早多晚, 一律被排除。
      · **多算**: 空格行的字面值被当成北京墙钟, 而实际它是 UTC → 差 8 小时,
        窗口边界上会多命中。
    本用例刻意让两个方向都暴露, 且断言朴素结果与归一化结果**不相等**。
    """
    conn = _make_table([
        (1, "2026-10-25T09:00:00"),   # 北京 09:00 → 窗口内 (朴素: 漏掉)
        (2, "2026-10-25 01:00:00"),   # UTC 01:00 = 北京 09:00 → 窗口内 (朴素: 漏掉)
        (3, "2026-10-25 11:00:00"),   # UTC 11:00 = 北京 19:00 → 窗口外 (朴素: 多算)
        (4, "2026-10-25T20:00:00"),   # 北京 20:00 → 窗口外 (两边都排除)
    ])
    lo, hi = "2026-10-25 08:00:00", "2026-10-25 18:00:00"
    naive = {r[0] for r in conn.execute(
        "SELECT id FROM t WHERE created_at >= ? AND created_at <= ?", (lo, hi))}
    expr = time_utils.sql_bj_ts_expr("created_at")
    normalized = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?", (lo, hi))}
    assert normalized == {1, 2}, f"归一化后应命中 {{1,2}}, 实际 {normalized}"
    assert naive == {3}, f"朴素比较应只命中 {{3}}, 实际 {naive}"
    assert naive != normalized, "朴素比较与归一化结果相同 → 反证用例已失效"
    conn.close()


def test_empty_and_null_created_at_are_excluded():
    """created_at 为空/NULL 的行不该进任何时间窗口。"""
    conn = _make_table([(1, ""), (2, None), (3, "2026-10-25 09:00:00")])
    expr = time_utils.sql_bj_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?",
        ("2026-10-25 00:00:00", "2026-10-25 23:59:59"))}
    assert got == {3}, f"空/NULL 行未被排除: {got}"
    conn.close()


# ── 3. UTC 语义列 (feed_health.ts) ─────────────────────────────────────────
def test_utc_ts_expr_normalizes_iso_and_space():
    """feed_health.ts 的两种格式都要归一化成 UTC 墙钟。"""
    conn = _make_table([
        (1, "2026-10-25T09:00:00Z"),
        (2, "2026-10-25 09:00:00"),
    ])
    expr = time_utils.sql_utc_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?",
        ("2026-10-25 08:00:00", "2026-10-25 10:00:00"))}
    assert got == {1, 2}, f"UTC 归一化结果错: {got}"
    conn.close()


def test_utc_ts_expr_stays_in_utc_not_beijing():
    """UTC 归一化**不能**偷偷 +8 小时 —— cutoff 也是 UTC 墙钟, 口径必须一致。"""
    conn = _make_table([(1, "2026-10-25 09:00:00")])
    expr = time_utils.sql_utc_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ?",
        ("2026-10-25 08:00:00",))}
    assert got == {1}
    # 北京墙钟 cutoff (17:00) 不应命中 09:00 的 UTC 行
    got2 = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ?", ("2026-10-25 17:00:00",))}
    assert got2 == set(), "UTC 列被当成北京列了"
    conn.close()


# ── 4. SQLite date 函数行为 (整个修复的基石, 必须钉死) ─────────────────────
def test_sqlite_datetime_semantics():
    """钉死 SQLite 的 datetime() 行为 —— 归一化表达式依赖这几条语义。

    1. 无时区时间串按 **UTC** 解读, 但输出数值 == 原串字面数值
    2. '+8 hours' / '-8 hours' 是**墙钟平移** (与假设 UTC 还是本地无关,
       这正是本修复只依赖 "+8 hours 平移" 而不依赖时区假设的原因)
    3. 解析不了的串 → NULL
    4. 带毫秒的串也能解析 (输出被截断到秒)
    """
    conn = sqlite3.connect(":memory:")
    one = lambda sql: conn.execute("SELECT " + sql).fetchone()[0]

    # 1) 无时区串 → datetime() 输出数值 == 字面数值
    assert one("datetime('2026-10-25 09:00:00')") == "2026-10-25 09:00:00"
    # 2) 墙钟平移 (关键: 平移量与时区假设无关)
    assert one("datetime('2026-10-25 09:00:00', '+8 hours')") == "2026-10-25 17:00:00"
    assert one("datetime('2026-10-25 09:00:00', '-8 hours')") == "2026-10-25 01:00:00"
    # 跨日平移也必须正确
    assert one("datetime('2026-10-25 20:00:00', '+8 hours')") == "2026-10-26 04:00:00"
    # 3) 解析失败 → NULL
    assert one("datetime('not-a-time')") is None
    assert one("datetime('')") is None
    # 4) 带毫秒 → 截断到秒
    assert one("datetime('2026-10-25 09:00:00.123')") == "2026-10-25 09:00:00"
    conn.close()


def test_normalized_expr_output_shape_is_identical_for_both_formats():
    """归一化后, 两种落库格式的**同一时刻**必须落到同一个值。

    这是字典序比较成立的前提。

    注意: 两种格式的墙钟数字**不同**也能表示同一时刻 ——
      '2026-10-25T09:00:00'  = 北京 09:00 (T 格式 = 北京墙钟)
      '2026-10-25 01:00:00'  = UTC 01:00  = 北京 09:00 (空格格式 = UTC 墙钟)
    所以拿"数字相同"的两行去比是错的, 必须拿"同一时刻"的两种写法去比。
    """
    conn = _make_table([
        (1, "2026-10-25T09:00:00"),   # T 分隔 (北京本地) → 北京 09:00
        (2, "2026-10-25 01:00:00"),   # 空格 (UTC)        → 北京 09:00 (同一时刻)
    ])
    expr = time_utils.sql_bj_ts_expr("created_at")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t"))
    assert rows[1] == rows[2] == "2026-10-25 09:00:00", \
        f"同一时刻的两种写法归一化后不一致: {rows[1]!r} vs {rows[2]!r}"
    conn.close()


def test_normalized_expr_output_shape_is_always_19_chars():
    """归一化输出必须恒为 'YYYY-MM-DD HH:MM:SS' (19 字符) —— 字典序比较的前提。"""
    conn = _make_table([
        (1, "2026-10-25T09:00:00"),
        (2, "2026-10-25 01:00:00"),
        (3, "2026-10-25T09:00:00.123"),   # 带毫秒
        (4, "2026-10-25 01:00:00.456"),   # 带毫秒
    ])
    expr = time_utils.sql_bj_ts_expr("created_at")
    for rid, val in conn.execute(f"SELECT id, {expr} FROM t"):
        assert val is not None, f"id={rid} 归一化成了 NULL"
        assert len(val) == 19, f"id={rid} 归一化结果长度 {len(val)} != 19: {val!r}"
        assert val[4] == "-" and val[10] == " ", f"id={rid} 形态不对: {val!r}"
    conn.close()


def test_unparseable_timestamp_is_excluded_not_treated_as_zero():
    """解析不了的 created_at → NULL → 被窗口排除 (不能当成 0 而命中)。"""
    conn = _make_table([
        (1, "garbage-value"),
        (2, "2026-10-25 09:00:00"),
    ])
    expr = time_utils.sql_bj_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?",
        ("2026-10-25 00:00:00", "2026-10-25 23:59:59"))}
    assert got == {2}, f"无法解析的行未被排除: {got}"
    conn.close()


# ── 5. localtime 语义列 (causal_chains.created_at) ─────────────────────────
# db.py 里 causal_chains.created_at 的 DEFAULT 是 datetime('now','localtime')
# → 落库是**本机墙钟** (空格分隔), 既不是 UTC 也不是固定北京。
# 这类列只能用 local_str() + sql_local_ts_expr() 配对过滤。
def test_local_str_returns_machine_wall_clock():
    """local_str() 必须产出**本机**墙钟, 而不是 UTC 或固定北京。"""
    import time as _time
    ts = 1793000000
    expected = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(ts))
    assert time_utils.local_str(ts) == expected


def test_local_str_is_distinct_from_utc_and_bj_as_needed():
    """local_str() 产出的墙钟必须与 UTC / 北京墙钟区分得开。

    口径关系:
      · BJ 与 UTC 永远差 8 小时 (北京 = UTC+8) → 必然不同
      · local 取决于**本机时区**: 本机是 UTC 时 local == utc;
        本机是北京时 local == bj; 其他时区则三者互不相同。

    所以断言只能是"至少两种不同" —— 不能断言 local 必须同时不同于两者
    (那在本机恰好是 UTC/北京时会假失败)。
    """
    ts = 1793000000
    utc = time_utils.utc_str(ts)
    bj = time_utils.epoch_to_bj_str(ts)
    loc = time_utils.local_str(ts)
    assert bj != utc, "BJ 与 UTC 必须差 8 小时"
    assert len({utc, bj, loc}) >= 2, (
        f"三种口径至少要有两种不同, 否则说明实现退化成单一墙钟: "
        f"utc={utc!r} bj={bj!r} local={loc!r}")


def test_sql_local_ts_expr_does_not_shift_the_clock():
    """sql_local_ts_expr() 只统一形态, **绝不做时区平移**。

    错误做法: 把 localtime 列当成 UTC (sql_utc_ts_expr) 或当成 UTC 再 +8h
    (sql_bj_ts_expr) → 凭空偏移 8 小时, 窗口边界整天错位。
    """
    conn = _make_table([
        (1, "2026-10-25 09:00:00"),   # 空格分隔 (DB DEFAULT localtime 的形状)
        (2, "2026-10-25T09:00:00"),   # T 分隔
    ])
    expr = time_utils.sql_local_ts_expr("created_at")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t"))
    assert rows[1] == "2026-10-25 09:00:00", f"空格行被平移了: {rows[1]!r}"
    assert rows[2] == "2026-10-25 09:00:00", f"T 行被平移了: {rows[2]!r}"
    conn.close()


def test_substr_based_normalizers_do_not_null_out_garbage():
    """substr 型归一化 (UTC / LOCAL) 不做解析校验 → 脏数据原样透传。

    这与 BJ 口径 (走 datetime(), 解析失败 → NULL → 行被排除) 是**有意**的差异:
      · sql_bj_ts_expr  → datetime(col,'+8 hours') → 解析失败返回 NULL
      · sql_utc_ts_expr / sql_local_ts_expr → 纯 substr() → 脏数据原样参与字典序比较

    本测试把这个差异钉死, 避免有人误以为两者行为一致。
    """
    conn = _make_table([(1, "garbage-value")])
    utc_v = conn.execute(
        f"SELECT {time_utils.sql_utc_ts_expr('created_at')} FROM t").fetchone()[0]
    loc_v = conn.execute(
        f"SELECT {time_utils.sql_local_ts_expr('created_at')} FROM t").fetchone()[0]
    bj_v = conn.execute(
        f"SELECT {time_utils.sql_bj_ts_expr('created_at')} FROM t").fetchone()[0]
    assert utc_v == "garbage-value", f"UTC 口径应原样透传: {utc_v!r}"
    assert loc_v == "garbage-value", f"LOCAL 口径应原样透传: {loc_v!r}"
    assert bj_v is None, f"BJ 口径走 datetime() → 解析失败应为 NULL: {bj_v!r}"
    conn.close()


def test_sql_local_ts_expr_output_is_always_19_chars():
    """归一化输出必须恒为 'YYYY-MM-DD HH:MM:SS' → 字典序比较才成立。"""
    conn = _make_table([
        (1, "2026-10-25T09:00:00"),
        (2, "2026-10-25 09:00:00"),
        (3, "2026-10-25T09:00:00.123"),
        (4, "2026-10-25 09:00:00.456"),
    ])
    expr = time_utils.sql_local_ts_expr("created_at")
    for rid, val in conn.execute(f"SELECT id, {expr} FROM t"):
        assert val is not None, f"id={rid} 归一化成了 NULL"
        assert len(val) == 19, f"id={rid} 归一化结果长度 {len(val)} != 19: {val!r}"
        assert val[10] == " ", f"id={rid} 形态不对: {val!r}"
    conn.close()


def test_local_window_filter_matches_stored_wall_clock():
    """用 local_str() 生成的 cutoff 过滤 localtime 列 → 必须命中边界行。

    这是 causal_accuracy_stats 里 causal_chains 那半边的核心行为。
    """
    import time as _time
    now = int(_time.time())
    stored = time_utils.local_str(now)          # 落库形状 (本机墙钟)
    cutoff = time_utils.local_str(now - 3600)   # cutoff 也是本机墙钟
    conn = _make_table([
        (1, time_utils.local_str(now - 7200)),  # 窗口外
        (2, stored),                            # 窗口内 (边界, 含)
        (3, time_utils.local_str(now + 7200)),  # 窗口外
    ])
    expr = time_utils.sql_local_ts_expr("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ?", (cutoff,))}
    assert got == {2, 3}, f"localtime 窗口过滤结果不对: {got}"
    conn.close()


def test_sql_local_ts_expr_is_not_equal_to_utc_or_bj_exprs():
    """反证: local 语义列绝不能用 utc / bj 归一化 (否则凭空偏 8 小时)。"""
    conn = _make_table([(1, "2026-10-25 09:00:00")])
    local_expr = time_utils.sql_local_ts_expr("created_at")
    utc_expr = time_utils.sql_utc_ts_expr("created_at")
    bj_expr = time_utils.sql_bj_ts_expr("created_at")
    local_v = conn.execute(f"SELECT {local_expr} FROM t").fetchone()[0]
    utc_v = conn.execute(f"SELECT {utc_expr} FROM t").fetchone()[0]
    bj_v = conn.execute(f"SELECT {bj_expr} FROM t").fetchone()[0]
    assert local_v == "2026-10-25 09:00:00"
    assert utc_v == "2026-10-25 09:00:00", "UTC 语义对空格串不做平移 (预期)"
    assert bj_v == "2026-10-25 17:00:00", "BJ 语义把空格串当 UTC 再 +8h"
    assert bj_v != local_v, "BJ 口径与 local 口径结果不同 (说明不能混用)"
    conn.close()


# ── 6. 语义自洽性: 每个归一化函数必须与它自己的 cutoff 口径配对 ──────────
# 这是整个 v309 审计的核心不变式:
#   归一化函数的输出语义  ==  cutoff 生成函数的语义
# 只要配对正确, 字典序 == 时间序, 窗口筛选必然正确。
@pytest.mark.parametrize("expr_fn,cutoff_fn,store_fn,label", [
    # BJ 口径: 列里是 'T' 分隔的北京墙钟 (paper_reflection_worker 的写法)
    (time_utils.sql_bj_ts_expr, time_utils.epoch_to_bj_str,
     time_utils.epoch_to_bj_store, "BJ"),
    # UTC 口径: 列里是空格分隔的 UTC 墙钟 (db.py DEFAULT datetime('now'))
    (time_utils.sql_utc_ts_expr, time_utils.utc_str,
     time_utils.utc_str, "UTC"),
    # LOCAL 口径: 列里是空格分隔的本机墙钟 (db.py DEFAULT datetime('now','localtime'))
    (time_utils.sql_local_ts_expr, time_utils.local_str,
     time_utils.local_str, "LOCAL"),
])
def test_normalizer_must_pair_with_its_own_cutoff(expr_fn, cutoff_fn, store_fn, label):
    """同一时刻: 用配套 cutoff 过滤该口径的落库串 → 必须命中。

    这是整个 v309 审计的核心不变式:
        归一化函数的输出语义  ==  cutoff 生成函数的语义
    配对正确 → 字典序 == 时间序 → 窗口筛选必然正确。

    注意: 落库串必须用**该列语义对应的格式**:
      · BJ 列   → 'T' 分隔 (sql_bj_ts_expr 会把空格串当 UTC 再 +8h)
      · UTC 列  → 空格分隔
      · LOCAL 列 → 空格分隔
    """
    import time as _time
    now = int(_time.time())
    cutoff = cutoff_fn(now)
    stored = store_fn(now)
    conn = _make_table([(1, stored)])
    expr = expr_fn("created_at")
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ?", (cutoff,))}
    assert got == {1}, (
        f"{label} 口径: 配套 cutoff 没能命中自己的落库串 "
        f"(stored={stored!r} cutoff={cutoff!r})")
    conn.close()

def test_all_three_cutoff_flavors_are_distinguishable():
    """三种 cutoff 口径在非 UTC 时区下必须互不相同 (防止有人把它们混用)。"""
    ts = 1793000000
    bj = time_utils.epoch_to_bj_str(ts)
    utc = time_utils.utc_str(ts)
    loc = time_utils.local_str(ts)
    # BJ 与 UTC 永远差 8 小时
    assert bj != utc
    # 至少有两种不同 → 若本机恰好是 UTC 或北京, 允许三者中出现相同值,
    # 但不能三者全同 (那说明实现退化成单一墙钟)。
    assert len({bj, utc, loc}) >= 2, f"三种口径全同, 说明实现有问题: {bj!r}"


