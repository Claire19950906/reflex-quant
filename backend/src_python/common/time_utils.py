#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
time_utils.py — v309 P1: 全项目时间口径唯一入口 (北京时间墙钟)
=====================================================================
为什么要有这个文件:
  v309 之前, 同一条"时间"在项目里有 **四种** 互不兼容的写法:
    1. trade_reflections_v2.created_at
       · paper_reflection_worker 写 'YYYY-MM-DDTHH:MM:SS'  (T 分隔, 本机 localtime)
       · bridge_server / DB DEFAULT 写 'YYYY-MM-DD HH:MM:SS' (空格分隔, **UTC**)
    2. feed_health.ts
       · feed_probe 写 'YYYY-MM-DD HH:MM:SS' (**UTC**)
       · 历史行是 ISO 'YYYY-MM-DDTHH:MM:SS' (可能带 Z)
    3. critical_price_moves.ts / external_indicators.ts / ohlcv_daily.ts
       · INTEGER epoch 秒 (真 epoch, 无歧义)
    4. 各处 datetime.utcfromtimestamp() → 拼 'Z' 的 ISO 串

  于是反复出现三类**互相矛盾**的 bug:
    · 拿 epoch 整数去比 TEXT 列 → SQLite 类型亲和性把整数当文本比, 过滤恒假/恒真
    · 拿北京墙钟串去比 UTC 墙钟串 → 整整差 8 小时
    · 拿 'T' 分隔串去比 ' ' 分隔串 → 同一日期内字典序错乱 (边界日失真)

统一约定 (本文件是唯一实现, 别的模块一律调用这里的函数):
  · **裸时间串 (无时区) 一律视为北京时间** —— 与 config.parse_time_to_bj 同口径
  · 落库格式 = 'YYYY-MM-DDTHH:MM:SS' (T 分隔, 北京) —— 与既有 worker 数据同格式,
    避免历史数据出现第三种格式
  · 比较/过滤格式 = 'YYYY-MM-DD HH:MM:SS' (空格分隔, 北京) —— SQL 里统一用
    sql_bj_ts_expr() 归一化后比较, 两种落库格式都能正确过滤
  · 需要 epoch 时用 epoch_to_bj_str() / bj_str_to_epoch(), **不要自己拼 strftime**

  ── 但**不是所有**时间列都是北京墙钟! ────────────────────────────────
  三种列语义各自配一套 (归一化表达式 + cutoff), 配错就是整窗失真:
      列语义            归一化函数            cutoff 函数         典型列
      ────────────────  ────────────────────  ──────────────────  ──────────────────
      BJ  (北京墙钟)    sql_bj_ts_expr()      epoch_to_bj_str()  trade_reflections_v2
                                                      .created_at / raw_news.timestamp
      UTC (UTC 墙钟)    sql_utc_ts_expr()     utc_str()          review_insights.created_at
                                                                    / feed_health.ts
      LOCAL(本机墙钟)   sql_local_ts_expr()   local_str()        causal_chains.created_at
                                            (DEFAULT datetime('now','localtime'))

  关键: **归一化表达式只统一形态, 时区语义由 cutoff 承载**。sql_utc_ts_expr()
  与 sql_local_ts_expr() 的 SQL 完全相同 (都不平移); 只有 sql_bj_ts_expr() 会 +8h
  (因为它的列存的是北京墙钟, 而 SQLite 对裸串按 UTC 解读)。所以真正决定正确性的
  是"这列配哪个 cutoff", 而不只是"用了哪个表达式"。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── 北京时间 (全项目唯一时区口径) ────────────────────────────────────────────
BJ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 落库格式 (T 分隔) —— 与 paper_reflection_worker 既有数据一致
TS_FMT_STORE = "%Y-%m-%dT%H:%M:%S"
# 比较/过滤格式 (空格分隔) —— SQL 归一化后的统一形态
TS_FMT_CMP = "%Y-%m-%d %H:%M:%S"


# ── 生成 ─────────────────────────────────────────────────────────────────────
def bj_now_str() -> str:
    """当前北京时间, 落库格式 ('YYYY-MM-DDTHH:MM:SS')。"""
    return datetime.fromtimestamp(time.time(), BJ).strftime(TS_FMT_STORE)


def epoch_to_bj_store(epoch: float) -> str:
    """epoch 秒 → 北京时间, 落库格式 ('YYYY-MM-DDTHH:MM:SS')。"""
    return datetime.fromtimestamp(float(epoch), BJ).strftime(TS_FMT_STORE)


def epoch_to_bj_str(epoch: float) -> str:
    """epoch 秒 → 北京时间, 比较格式 ('YYYY-MM-DD HH:MM:SS')。"""
    return datetime.fromtimestamp(float(epoch), BJ).strftime(TS_FMT_CMP)


def utc_str(epoch: float) -> str:
    """epoch 秒 → UTC, 比较格式 ('YYYY-MM-DD HH:MM:SS')。仅用于本来就是 UTC 的列。"""
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(TS_FMT_CMP)


def local_str(epoch: float) -> str:
    """epoch 秒 → **本机 localtime**, 比较格式 ('YYYY-MM-DD HH:MM:SS')。

    仅用于 db.py 里 DEFAULT 写成 datetime('now','localtime') 的列
    (如 causal_chains.created_at)。这类列的墙钟 = **本机时区**, 既不是 UTC
    也不是固定的北京时间 → 不能用 utc_str() / epoch_to_bj_str()。

    注意: 本机时区由操作系统决定。若部署机器时区不是 Asia/Shanghai,
    则该列的墙钟与北京相差一个固定偏移 —— 但**写入方和比较方都用同一个
    本机 localtime 口径**, 所以窗口筛选依然自洽 (这是 localtime 语义的正确用法)。
    """
    return datetime.fromtimestamp(float(epoch)).strftime(TS_FMT_CMP)


# ── 解析 ─────────────────────────────────────────────────────────────────────
def parse_ts_to_bj(value: Any) -> Optional[datetime]:
    """任意常见时间串 → 北京时间 aware datetime; 无法解析返回 None。

    支持: 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DDTHH:MM:SS' (裸串按北京解读)、
          带 'Z' 或 '+08:00' 的 ISO (按其自带时区换算)、epoch 秒/毫秒。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (10, 13):
        secs = float(s) / 1000.0 if len(s) == 13 else float(s)
        try:
            return datetime.fromtimestamp(secs, BJ)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in (TS_FMT_CMP, TS_FMT_STORE, "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ)      # 裸串 = 北京 (项目约定)
    return dt.astimezone(BJ)


def bj_str_to_epoch(value: Any) -> Optional[int]:
    """任意时间串 → epoch 秒 (裸串按北京解读); 无法解析返回 None。"""
    dt = parse_ts_to_bj(value)
    return int(dt.timestamp()) if dt is not None else None


def utc_str_to_epoch(value: Any) -> Optional[int]:
    """任意时间串 → epoch 秒 (裸串按 **UTC** 解读); 无法解析返回 None。

    给"本来就是 UTC 语义"的列用 (如 feed_health.ts)。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (10, 13):
        secs = float(s) / 1000.0 if len(s) == 13 else float(s)
        try:
            return int(datetime.fromtimestamp(secs, timezone.utc).timestamp())
        except (ValueError, OSError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in (TS_FMT_CMP, TS_FMT_STORE, "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)   # 裸串 = UTC (该列的语义)
    return int(dt.astimezone(timezone.utc).timestamp())


def is_store_format(value: Any) -> bool:
    """该串是否为落库格式 ('T' 分隔)。用于归一化脚本分类。"""
    return "T" in str(value or "")


# ── SQL 片段 ─────────────────────────────────────────────────────────────────
def sql_bj_ts_expr(col: str) -> str:
    """把 TEXT 时间列归一化成 **北京墙钟 + 空格分隔**, 供 SQL 直接比较。

    覆盖两种历史落库格式 (写入方已全部枚举, 见文件头):
      · 'T' 分隔 → paper_reflection_worker 写的是**本机本地时间**(= 北京)
      · ' ' 分隔 → bridge_server / DB DEFAULT 写的是 **UTC**
      · 纯数字串 (10/13 位) → 历史脏数据里有人把 epoch 秒**直接当字符串**写进来
        (实测 trade_reflections_v2 有 19 行 created_at='1792668960'), 旧写法走
        ELSE 分支被 SQLite 当 Julian day 解析 → NULL → 这些行被时间窗静默排除
        (端点计数比库里少 19 条, 验收报"端点↔库不一致")。现在显式识别并换算。

    实现说明:
      · T 分隔分支用**纯字符串操作** (replace + substr), 行为完全确定:
        字面数值本身就是北京墙钟, 只把分隔符统一成空格。
      · 空格分隔分支要 +8 小时 (UTC → 北京), 这一步必须做**日期加法**,
        用 SQLite 的 datetime(col, '+8 hours') 完成。SQLite 的 date 函数
        对 'YYYY-MM-DD HH:MM:SS' 的解析是标准行为 (ISO-8601 兼容),
        且无时区串按 UTC 解读 —— 正好把 UTC 墙钟平移成北京墙钟。
        若该行解析失败 → datetime() 返回 NULL → strftime(NULL) 也返回 NULL
        → 该行被窗口自然排除 (不会被当成 0 而错误命中)。
      · 两条分支输出形态一致 ('YYYY-MM-DD HH:MM:SS'), 字典序比较可靠,
        不再受 'T' vs ' ' 的 ASCII 差影响, 也不依赖 SQLite 类型亲和性。

    注: 这套语义已被 backend/tests/test_time_utils.py 的
    test_sqlite_datetime_semantics 钉死 (行为测试, 不是注释)。
    """
    return (
        f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
        # T 分隔 (北京本地时间): 字面数值即北京墙钟, 只统一分隔符
        f"WHEN {col} LIKE '%T%' THEN substr(replace({col}, 'T', ' '), 1, 19) "
        # epoch 数字串 (脏数据形态): unixepoch 解析 → 北京墙钟
        f"WHEN {col} NOT GLOB '*[^0-9]*' AND length({col}) IN (10, 13) "
        f"     THEN strftime('%Y-%m-%d %H:%M:%S', "
        f"          datetime(CAST({col} AS REAL) / "
        f"                CASE WHEN length({col}) = 13 THEN 1000.0 ELSE 1.0 END, "
        f"                'unixepoch', '+8 hours')) "
        # 空格分隔 (UTC): +8h → 北京墙钟
        f"ELSE strftime('%Y-%m-%d %H:%M:%S', datetime({col}, '+8 hours')) END"
    )


def sql_bj_ts_ge(col: str) -> str:
    """`expr >= ?` 片段 (expr 由 sql_bj_ts_expr 生成)。"""
    return f"{sql_bj_ts_expr(col)} >= ?"


def sql_bj_ts_between(col: str) -> str:
    """`expr >= ? AND expr <= ?` 片段。"""
    e = sql_bj_ts_expr(col)
    return f"{e} >= ? AND {e} <= ?"


def sql_utc_ts_expr(col: str) -> str:
    """把 **UTC 语义** 的 TEXT 时间列归一化成 'YYYY-MM-DD HH:MM:SS' (UTC 墙钟)。

    给 feed_health.ts 这类列用 (由 feed_probe 写的是 UTC)。
    兼容两种历史落库格式:
      · 'T' 分隔 (可能带 Z / ±HH:MM) → 剥掉分隔符与时区后缀
      · ' ' 分隔 → 已是 UTC 墙钟, 原样

    注意: 归一化后仍是 **UTC 墙钟**, 所以 cutoff 也要用 utc_str() 生成,
    不要用 epoch_to_bj_str() (那是北京墙钟, 差 8 小时)。

    ⚠️ 本表达式**只做形态统一, 不做任何时区平移** —— 它与 sql_local_ts_expr()
    的 SQL 完全相同。UTC 与 LOCAL 两个口径的差异**全部体现在配套 cutoff 上**
    (utc_str() vs local_str()), 不在表达式上。

    实现:
      T 分隔 → replace('T',' ') 后再截掉尾部时区后缀 (Z / +HH:MM),
              取前 19 字符 ('YYYY-MM-DD HH:MM:SS')
      空格   → substr(1,19) 截掉毫秒等尾巴
    """
    return (
        f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
        f"WHEN {col} LIKE '%T%' THEN "
        f"  substr(replace(replace({col}, 'T', ' '), 'Z', ''), 1, 19) "
        f"ELSE substr({col}, 1, 19) END"
    )


def sql_local_ts_expr(col: str) -> str:
    """把 **本机 localtime 语义** 的 TEXT 时间列归一化成 'YYYY-MM-DD HH:MM:SS' (本机墙钟)。

    给 db.py 里 DEFAULT 写成 datetime('now','localtime') 的列用
    (如 causal_chains.created_at)。这类列的墙钟 = 本机时区, 既不是 UTC
    也不是固定北京。

    ⚠️ 关于"能不能用 sql_utc_ts_expr()":
    归一化表达式**只做形态统一, 不做任何时区平移** —— 本函数与
    sql_utc_ts_expr() 的 SQL **完全相同** (纯 substr/replace)。所以
    拿 sql_utc_ts_expr() 去比 localtime 列, 归一化结果也是一样的,
    表达式层面并不会"把它当成 UTC"。

    真正的区别在**配套 cutoff**:
      · localtime 列 → 必须配 local_str() (本机墙钟)
      · 若配了 utc_str() (UTC 墙钟), 就会整体差一个时区偏移 → 窗口失真
    之所以仍单列一个函数, 是为了:
      1. 调用处一眼能看出这列是 localtime 语义 (可读性 / 防误用);
      2. 防御未来有人把 sql_utc_ts_expr() 改成会做平移的实现
         (那时 localtime 列会跟着一起坏掉)。两个函数各自独立演进。

    归一化只做**形态**统一 (分隔符 + 截掉毫秒/时区尾巴), **不做任何时区平移**:
      · 'T' 分隔 → replace('T',' ') 后取前 19 字符
      · ' ' 分隔 → substr(1,19) 截掉毫秒等尾巴

    对应的 cutoff 必须用 local_str() 生成 (同样是本机墙钟) → 形态与语义都一致,
    字典序 == 时间序。
    """
    return (
        f"CASE WHEN {col} IS NULL OR {col} = '' THEN NULL "
        f"WHEN {col} LIKE '%T%' THEN "
        f"  substr(replace(replace({col}, 'T', ' '), 'Z', ''), 1, 19) "
        f"ELSE substr({col}, 1, 19) END"
    )

def format_time(value: Any) -> str:
    """任意时间串 → 'YYYY-MM-DD HH:MM:SS'（统一北京时间）；无法解析原样截断。

    从 api_server._format_time 下沉（2026-11-17）：19 处调用、跨域共用，
    放在这里比留在 api_server 里让 router 反查更合理。
    走 config.parse_time_to_bj：带时区换算到北京 / 无时区串按北京时间解读。
    注意不能对 naive 串直接 astimezone（会被当系统本地时区而偏移）。
    """
    import config # 局部导入：config 体量大，time_utils 的轻量调用方不必拖上它
    if not value:
        return "——"
    dt = config.parse_time_to_bj(value)
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return (str(value) or "")[:19] or "——"

