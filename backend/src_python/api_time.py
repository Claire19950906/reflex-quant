# -*- coding: utf-8 -*-
"""api_time.py — api_server 里的四个时间口径 helper（纯函数，已下沉单测）。

为什么单独拎出来：库里 TEXT 时间列有三种格式（'T' 分隔 UTC、
'T' 分隔北京时间、空格分隔），字典序比较必须与被比较列的格式**完全一致**，
否则窗口过滤会整段漏数据。这套规则以前只写在 api_server 的注释里，
谁都可能改错；下沉成独立模块后可以单测直接钉住。

调用方仍通过 api_server 的同名别名访问，行为不变。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import config

TZ_SHANGHAI = config.TZ_SHANGHAI

def _utc_ts_to_iso(ts: int) -> str:
    """UTC unix 秒 → 与 SQLite 文本字段同口径的 ISO 字符串（无时区后缀）。

    v180 P0+：review snapshot 窗口过滤要把 UTC 秒与 ai_decisions.created_at /
    ai_decisions.entry_time / raw_news.timestamp（TEXT，全部用 'T' 分隔）做比较。

    v302+ P0+【修复】改用 'T' 分隔（与存储格式完全一致）。旧版用空格 "%Y-%m-%d %H:%M:%S"
    时，由于 SQLite 字符串比较按字节序，'T' (0x54) > ' ' (0x20)，导致
    `entry_time >= '2026-10-09 16:00:00'` 这种过滤会把 2026-10-09 整天
    （甚至更早）全部漏进窗口，造成 dashboard "今日" 显示全期数据。
    改 'T' 分隔后，字符串比较与时间序一致，过滤正确。

    v309 审计更正: 本 helper 产出的 'T' 分隔串**只能**用于 'T' 分隔存储的列。
    库里的 TEXT 时间列实际有三种格式，不能混用:

      · ai_decisions.created_at  → ai_worker 写 _ts() = **北京时间**
                                   ('2026-10-25T16:00:00.000+08:00')
                                   带 '+08:00' 偏移后缀 → 与 naive UTC cutoff
                                   比会有 8h 漂移。**窗口过滤请用 ts (INTEGER)**
                                   (get_review_snapshot / get_replay_stats 已改)。
      · raw_news.timestamp       → engine/ingest.py 写 engine.utils._ts(), api_server 写 _now_iso()
                                    → 都是 **'T' 分隔北京时间** (带 '+08:00' 偏移后缀)。
                                    **不是 UTC** —— 旧注释写 "UTC" 是错的, 已更正。
                                    比较请用 time_utils.sql_bj_ts_expr() 归一化。
      · causal_chains.created_at  → **空格** 分隔**本机 localtime**
                                    (db.py DEFAULT datetime('now','localtime'))
                                    → 用 time_utils.sql_local_ts_expr() + local_str();
                                    既不是 UTC 也不是固定北京。
      · review_insights.created_at → **空格** 分隔 UTC (db.py DEFAULT
                                   datetime('now')) → 用 _utc_ts_to_space_iso。

    规则: **cutoff 的分隔符 + 时区语义必须与被比较列完全一致**，
    否则字典序比较不成立。优先用 INTEGER epoch 列 (ts / exit_ts) 过滤,
    彻底绕开 TEXT 格式问题。
    """
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _utc_ts_to_space_iso(ts: int) -> str:
    """UTC unix 秒 → **空格分隔** 的 ISO 字符串 ('YYYY-MM-DD HH:MM:SS')。

    为什么需要这个 (和 _utc_ts_to_iso 并存, 不能互相替代):

      库的 TEXT 时间列有两种落库格式, 各表的 DEFAULT 不同:

        · ai_decisions.created_at
            → 'T' 分隔 ('2026-10-25T08:00:00')
            → 用 _utc_ts_to_iso() (T 分隔) 比, 字典序与时间序一致 ✔
          (但注意 ai_worker 写的是**北京**时间带 +08:00 后缀, 见 _utc_ts_to_iso
           的 v309 注释 —— 窗口过滤优先用 ts (INTEGER))
        · raw_news.timestamp
            → 'T' 分隔**北京**时间 ('2026-10-25T16:00:00.000+08:00')
            → **不要**用本 helper (UTC 空格) 比; 用
              time_utils.sql_bj_ts_expr() 归一化后再比。
        · review_insights.created_at
            → db.py 的 DEFAULT 是 datetime('now') → **空格** 分隔
              ('2026-10-25 08:00:00')
            → 必须用**空格** cutoff 比, 用 'T' cutoff 会错 ✘

      为什么 'T' cutoff 比空格存储会错:
        SQLite 字符串比较按字节序, ' '(0x20) < 'T'(0x54)。
        `created_at >= '2026-10-25T08:00:00'` 对一行 '2026-10-25 08:00:00'
        求值为 **False** —— cutoff 当天的行被整片漏掉;
        `<=` 方向则相反, 会把当天的行全放进来。

      根因是 v302+ 那次修复把 cutoff 统一成了 'T' 分隔, 但那张表
      (review_insights) 的存储格式没跟着改 → 修复对它是反效果。

      规则: **cutoff 的分隔符必须与被比较列的落库格式一致**。
      新增/改动任何 TEXT 时间列时, 先确认它属于哪种格式, 再选 helper。
    """
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")



def _now_iso() -> str:
    """完整 ISO 北京时间串（秒级，含 +08:00 时区），区别于 engine.utils._now（HH:MM:SS 日志时钟）。"""
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")

# 2026-11-17: _format_time 已下沉到 common/time_utils.format_time（19 处调用共用）。

def _age_hours_of(value: Any) -> Optional[float]:
    """平台发布时间串距今的小时数（统一按北京时间换算；无法解析返回 None = 视为新鲜）。

    走 config.parse_time_to_bj：带时区（+08:00/Z/±HH:MM）换算到北京，
    无时区串按北京时间解读——避免把 UTC 当北京而差 8 小时。
    """
    dt = config.parse_time_to_bj(value)
    if dt is None:
        return None
    return (datetime.now(TZ_SHANGHAI) - dt).total_seconds() / 3600.0


__all__ = ["_utc_ts_to_iso", "_utc_ts_to_space_iso", "_now_iso", "_age_hours_of"]
