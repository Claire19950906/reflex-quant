#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v201 #2: 事件 -> K线影响 (Event->Kline Impact) worker。

设计目标
--------
把宏观/重大新闻事件 (news_events + raw_news) 与对应资产后续 4h K线 (market_ticks) 关联,
计算该事件引发的实际价格波动幅度。供前端 MacroKlineLink 组件展示"这条新闻带来多大行情"。

数据源
------
- news_events: id, news_id, ts, source, asset, score, status
- raw_news:    title (content 前 80 字), gate_strength (1-5 重要性代理), gate_main_asset
- market_ticks: symbol, ts, price (每分钟)

输出 event_kline_impact (v201 schema in db.py):
  pre_event_close, max_high, min_low, impact_pct, direction, impact_severity

使用
----
    python3 src_python/engine/event_kline_impact_worker.py --once
    python3 src_python/engine/event_kline_impact_worker.py --daemon
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from typing import Any, List, Optional, Tuple

# 允许 `python3 src_python/engine/event_kline_impact_worker.py --once` 独立运行
# (engine 包内被 engine.py 导入时, src_python/ 已经在 sys.path 里)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

_WINDOW_BEFORE_SEC = 60
_WINDOW_AFTER_SEC = 4 * 3600
_SEVERITY_THRESHOLDS = (0.003, 0.01)   # 0.3% / 1% → medium / high
_DAEMON_INTERVAL_SEC = 15 * 60
_LOOKBACK_DAYS = 7

# v323 写入前置校验（修三个静默缺陷）
#  1) 前收盘最多允许陈旧 6 小时：品种停盘/断源时，旧价会被当成"事件前价格"，
#     再和窗口末价比 → 算出假的暴涨暴跌。
_PRE_MAX_GAP_SEC = 6 * 3600
#  2) 窗口结束后再等 5 分钟才落库：旧实现事件刚发生、只采到几分钟数据就写入，
#     且 UNIQUE 幂等使其**永不重算** → 386 行「最高价=最低价」的假平盘就是这么来的。
_WINDOW_SETTLE_SEC = 300
#  3) 窗口内至少 2 笔真实报价，否则 amplitude 无意义。
_MIN_TICKS = 2
#  3b) 实测发现：窗口名义 4 小时，但行情源提前断掉，只有 54 秒有报价 ——
#      这种行会把"几十秒的跳动"标成"4 小时事件影响"。覆盖率不足 30 分钟不落库。
_MIN_COVERAGE_SEC = 1800
# v338: 事件标题清洗 —— raw_news.content 带 [hash:...] 去重指纹前缀
_HASH_PREFIX_RE = re.compile(r"^\s*\[hash:[0-9a-fA-F]+\]\s*")
#  批量提交，减少写锁竞争（旧实现每写一行 commit 一次）。
_COMMIT_EVERY = 50


def _clean_headline(raw: Any, limit: int = 120) -> str:
    """把新闻原文清洗成可读标题。

    实测 5,561 条 event_kline_impact 的 event_title 全部是空串或
    ``[hash:xxxx]`` 开头（raw_news.content 落库时带了去重指纹前缀），
    归因页只能显示哈希号 → 用户看不到"到底是什么事件"。

    规则:
      1. 剥掉 ``[hash:...]`` 去重前缀
      2. 只取首行（多源正文常是 "标题\\n正文" 两段）
      3. 压平空白、截断
    """
    s = str(raw or "").strip()
    s = _HASH_PREFIX_RE.sub("", s)
    if not s:
        return ""
    s = s.splitlines()[0].strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def _classify_severity(impact_pct: float) -> str:
    abs_pct = abs(impact_pct)
    if abs_pct >= _SEVERITY_THRESHOLDS[1]:
        return "high"
    if abs_pct >= _SEVERITY_THRESHOLDS[0]:
        return "medium"
    return "low"


def _market_bench_move(conn: sqlite3.Connection, symbol: str,
                       t_start: int, t_end: int,
                       min_symbols: int = 3) -> float:
    """同期其他品种的等权（中位数）涨跌幅 —— 市场基准。

    为什么需要（v322 实测）
    ---------------------
    方向判定若不剥离市场漂移，"整个市场都在涨"会让每个事件的 direction 都是 up，
    **判别力归零**。用超额方向（该品种 - 同期市场组合）才是真判别力。

    与 causal_chain_evaluator._market_benchmark_pct 同一思路（等权中位数、抗极值）。
    """
    rows = conn.execute(
        """SELECT m1.symbol AS symbol,
                  (SELECT price FROM market_ticks m2
                    WHERE m2.symbol = m1.symbol AND m2.ts < ?
                    ORDER BY m2.ts DESC LIMIT 1) AS pre,
                  (SELECT price FROM market_ticks m3
                    WHERE m3.symbol = m1.symbol AND m3.ts <= ?
                    ORDER BY m3.ts DESC LIMIT 1) AS post
           FROM (SELECT DISTINCT symbol FROM market_ticks) m1""",
        (t_start, t_end),
    ).fetchall()
    moves = []
    for r in rows:
        if r["symbol"] == symbol:
            continue
        pre, post = r["pre"], r["post"]
        if not pre or not post or pre <= 0:
            continue
        moves.append((post - pre) / pre)
    if len(moves) < min_symbols:
        return 0.0
    moves.sort()
    mid = len(moves) // 2
    if len(moves) % 2:
        return moves[mid]
    return (moves[mid - 1] + moves[mid]) / 2.0


def _classify_direction(pre: float, close_after: float,
                        bench_move: float = 0.0) -> str:
    """方向 = **窗口末价相对事件前收盘的超额变化**的符号。

    ⚠️ v322 修复（旧实现是错的）
    ----------------------------
    旧实现用**窗口内的最高价/最低价**猜方向：
        up_move = (max_high - pre) / pre ; dn_move = (pre - min_low) / pre
        谁大判谁
    → 这是用"**振幅**"猜方向，不是"事件后价格往哪走"。
      先冲高再回落的窗口也会判成 up → 实测 up/down = 1887/1959（几乎 1:1），
      **完全没有判别力**。

    现在：direction 看的是 **窗口末价 vs 事件前收盘**（真实的后续方向），
    再减去**同期市场组合收益**（剥离 beta）→ 才是超额方向。
    """
    if pre <= 0 or close_after <= 0:
        return "neutral"
    move = (close_after - pre) / pre - bench_move
    if move > 0.001:
        return "up"
    if move < -0.001:
        return "down"
    return "neutral"


# ── v453b: 类别名 → 具体标的的解析 ───────────────────────────────────────────
# 台账里最大的一坨"算不出影响"不是没行情，而是事件本身只标了类别：
# 个股 507 / CN_STOCK 21 / US_STOCK 17 / 加密主流 14 / 美股 14 / INDIVIDUAL_STOCK 6。
# 这些事件的标题里往往已经写明了公司或币种（"英伟达财报超预期"），
# 只是抽取层给的是类别名。这里做最小限度回填：标题命中的规范标的**唯一**时才绑定，
# 命中多个就放弃 —— 宁可不算，也不瞎猜（铁律：不造数据）。
_CATEGORY_ASSETS = {
    "个股", "INDIVIDUAL_STOCK", "US_STOCK", "CN_STOCK", "美股", "A股", "港股",
    "加密主流", "山寨", "加密货币", "CRYPTO", "STOCK", "EQUITY", "其他",
}

_ASSET_CUES = {
    "NVDA": ("nvidia", "英伟达", "nvda"),
    "AMD": ("amd", "超威", "advanced micro"),
    "TSLA": ("tesla", "特斯拉", "tsla"),
    "AAPL": ("apple", "苹果公司", "aapl"),
    "MSFT": ("microsoft", "微软", "msft"),
    "AMZN": ("amazon", "亚马逊", "amzn"),
    "GOOGL": ("alphabet", "google", "谷歌", "googl"),
    "META": ("meta platforms", "facebook", "脸书"),
    "MU": ("micron", "美光"),
    "BTC": ("bitcoin", "比特币", "btc"),
    "ETH": ("ethereum", "以太坊"),
    "SOL": ("solana",),
    "XAU": ("gold", "黄金", "金价"),
    "XAG": ("silver", "白银", "银价"),
    "WTI": ("crude", "原油", "wti"),
}


def _resolve_category_asset(asset: str, title: str, tradable: set) -> str:
    """类别名 → 有行情的规范标的；解析不出来就原样返回（不猜）。"""
    up = str(asset or "").upper()
    if up in tradable or up not in _CATEGORY_ASSETS:
        return up
    text = str(title or "").lower()
    if not text:
        return up
    hits = [sym for sym, cues in _ASSET_CUES.items()
            if sym in tradable and any(c in text for c in cues)]
    # 唯一命中才绑定；多标的并列说明这条是"板块级"事件，交给人工，不硬塞
    return hits[0] if len(hits) == 1 else up


def _tradable_symbols(conn: sqlite3.Connection) -> set:
    """v323-4 / v453: 只处理**有行情源**的品种。

    实测 500 条待处理事件里 443 条挂在 `INDIVIDUAL_STOCK` / `个股` / `加密主流`
    / `US_STOCK` / `山寨` 这类**类别名**上，行情表只有 BTC/ETH/SOL/WTI/XAU 五个 symbol
    → 它们永远算不出结果，却按 ts DESC 排在队首，把真正可算的事件挤在 500 条窗口外。

    v453: 可算集合不再只看 market_ticks —— 股票类没有 tick 但有 1m K 线，
    走 _fetch_window_bars 兜底同样能算影响，所以并入"近 3 天有 1m K 线的标的"。
    """
    syms: set = set()
    try:
        syms |= {str(r[0]).upper() for r in conn.execute(
            "SELECT DISTINCT symbol FROM market_ticks")}
    except sqlite3.OperationalError:
        pass
    try:
        cutoff_ms = (int(time.time()) - 3 * 86400) * 1000
        syms |= {str(r[0]).upper() for r in conn.execute(
            "SELECT DISTINCT symbol FROM kline_bars WHERE timeframe = '1m' AND ts >= ?",
            (cutoff_ms,))}
    except sqlite3.OperationalError:
        pass
    return syms

_TRADABLE_CACHE = {"at": 0.0, "syms": set()}


def _tradable_cached(conn: sqlite3.Connection) -> set:
    """v453b: 逐事件调 _tradable_symbols 会每行一次全表 DISTINCT（228 行就 228 次），
    这里按 5 分钟 TTL 缓存一份，够用且不会拖慢每轮。"""
    if time.time() - _TRADABLE_CACHE["at"] < 300 and _TRADABLE_CACHE["syms"]:
        return _TRADABLE_CACHE["syms"]
    syms = _tradable_symbols(conn)
    _TRADABLE_CACHE["at"] = time.time()
    _TRADABLE_CACHE["syms"] = syms
    return syms


def _fetch_pending_events(conn: sqlite3.Connection,
                          lookback_days: int) -> List[sqlite3.Row]:
    """未入库 event_kline_impact 的事件（UNIQUE=source+id+asset）。"""
    cutoff = int(time.time()) - lookback_days * 86400
    tradable = _tradable_symbols(conn)
    if not tradable:
        return []
    marks = ",".join("?" * len(tradable))
    return conn.execute(
        f"""SELECT ne.id AS event_id,
                  ne.source AS event_source,
                  ne.news_id,
                  ne.ts AS event_time,
                  ne.asset,
                  ne.score,
                  ne.status,
                  COALESCE(NULLIF(ne.headline, ''), rn.content) AS title,
                  COALESCE(rn.gate_strength, 0) AS importance_score
             FROM news_events ne
             LEFT JOIN raw_news rn ON rn.id = ne.news_id
        LEFT JOIN event_kline_impact eki
               ON eki.event_source = ne.source
              AND eki.event_id = CAST(ne.id AS TEXT)
              AND eki.asset = ne.asset
            WHERE ne.status IN ('PENDING', 'DONE')
              AND ne.ts >= ?
              AND ne.asset != 'NONE'
              AND UPPER(ne.asset) IN ({marks})
              AND eki.id IS NULL
            ORDER BY ne.ts DESC
            LIMIT 500""",
        (cutoff, *sorted(tradable)),
    ).fetchall()


def _fetch_window_bars(conn: sqlite3.Connection,
                       symbol: str,
                       t_start: int,
                       t_end: int) -> Tuple[List[Tuple[int, float]], float, int]:
    """v453: tick 缺失时的兜底 —— 用 kline_bars 的 1m 收盘价当窗口价序列。

    为什么需要：
      股票/指数（NVDA/AAPL/MU/NDX…）没有 tick 源（tick 只覆盖 BTC/ETH/SOL/
      WTI/XAU），事件挂在这些标的上时 process_one_event 一律返回 None，
      台账里堆成"无行情源的事件"（实测 个股=507、NVDA=139）。
      但它们有 1m K 线（本轮已把 intraday 双写进 kline_bars），
      足够算 4 小时窗口的极值/收盘/方向。

    口径：返回结构与 _fetch_window_ticks 一致，调用方无需分叉；
    落库时用 basis='kline_1m' 标记来源，tick 与 bar 可区分、可回溯。
    """
    pre = conn.execute(
        """SELECT ts, close FROM kline_bars
            WHERE symbol = ? AND timeframe = '1m' AND ts < ? AND close > 0
            ORDER BY ts DESC LIMIT 1""",
        (symbol, t_start * 1000),
    ).fetchone()
    if not pre:
        return [], 0.0, 0
    pre_close = float(pre[1])
    pre_ts = int(int(pre[0]) // 1000)
    rows = conn.execute(
        """SELECT ts, close FROM kline_bars
            WHERE symbol = ? AND timeframe = '1m'
              AND ts >= ? AND ts <= ? AND close > 0
            ORDER BY ts ASC""",
        (symbol, (t_start - _PRE_MAX_GAP_SEC) * 1000, t_end * 1000),
    ).fetchall()
    return [(int(int(r[0]) // 1000), float(r[1])) for r in rows], pre_close, pre_ts


def _fetch_window_ticks(conn: sqlite3.Connection,
                         symbol: str,
                         t_start: int,
                         t_end: int) -> Tuple[List[Tuple[int, float]], float, int]:
    """返回 ([(ts, price), ...], pre_event_close, pre_ts)。

    v323: 一并返回**前收盘的时间戳**，供上游判断它是否陈旧 ——
    旧实现只要"事件前最后一笔"，不管这笔离事件多久，停盘品种会拿到几天前的价格。
    """
    pre = conn.execute(
        """SELECT ts, price FROM market_ticks
            WHERE symbol = ? AND ts < ?
            ORDER BY ts DESC LIMIT 1""",
        (symbol, t_start),
    ).fetchone()
    pre_close = float(pre["price"]) if pre else 0.0
    pre_ts = int(pre["ts"]) if pre else 0

    rows = conn.execute(
        """SELECT ts, price FROM market_ticks
            WHERE symbol = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC""",
        (symbol, t_start, t_end),
    ).fetchall()
    return [(int(r["ts"]), float(r["price"])) for r in rows], pre_close, pre_ts


def _ensure_quality_columns(conn: sqlite3.Connection) -> None:
    """v323: 新增覆盖度字段，把"这条记录靠不靠谱"写进库，而不是只判不记。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_kline_impact)")}
    for name, ddl in (
        ("coverage_sec", "ALTER TABLE event_kline_impact ADD COLUMN coverage_sec INTEGER DEFAULT NULL"),
        ("pre_gap_sec", "ALTER TABLE event_kline_impact ADD COLUMN pre_gap_sec INTEGER DEFAULT NULL"),
        # v453: 价格来源（tick / kline_1m），让"这条影响是用什么算的"可回溯
        ("basis", "ALTER TABLE event_kline_impact ADD COLUMN basis TEXT DEFAULT 'tick'"),
        # v453b: 原始类别名（个股/US_STOCK…），区分"原生标的"与"由标题回填"
        ("asset_raw", "ALTER TABLE event_kline_impact ADD COLUMN asset_raw TEXT DEFAULT ''"),
    ):
        if name not in cols:
            try:
                conn.execute(ddl)
                conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise


def process_one_event(conn: sqlite3.Connection,
                      row: sqlite3.Row) -> Optional[int]:
    """处理单个事件 -> 写 event_kline_impact。返回新插入 id；无足够 ticks 返回 None。"""
    asset_raw = str(row["asset"] or "").upper()
    tradable = _tradable_cached(conn)
    symbol = _resolve_category_asset(asset_raw, row["title"], tradable)
    if not symbol:
        return None
    event_time = int(row["event_time"] or 0)
    if event_time <= 0:
        return None
    t_start = event_time - _WINDOW_BEFORE_SEC
    t_end = event_time + _WINDOW_AFTER_SEC

    basis = "tick"
    ticks, pre_close, pre_ts = _fetch_window_ticks(conn, symbol, t_start, t_end)
    if not ticks or pre_close <= 0 or pre_ts <= 0:
        # v453: 没有 tick 的标的（股票/指数）用 1m K 线兜底，别让事件白挂着
        bars, b_pre, b_pre_ts = _fetch_window_bars(conn, symbol, t_start, t_end)
        if bars and b_pre > 0 and b_pre_ts > 0:
            ticks, pre_close, pre_ts, basis = bars, b_pre, b_pre_ts, "kline_1m"
    if not ticks or pre_close <= 0 or pre_ts <= 0:
        return None

    # v323-1: 前收盘陈旧校验 —— 停盘/断源时不许拿旧价冒充"事件前价格"
    pre_gap = t_start - pre_ts
    if pre_gap > _PRE_MAX_GAP_SEC:
        return None

    # v323-2: 窗口未走完不落库（旧实现只采到几分钟就写死，之后再不会重算）
    if int(time.time()) < t_end + _WINDOW_SETTLE_SEC:
        return None

    if len(ticks) < _MIN_TICKS:
        return None

    # v453: 单笔坏 tick 消毒 —— 实测 BTC 窗口里混进一笔 price=100 的 tick
    #       （source='test'，真实价 62,414），算出 impact=99.8%，
    #       直接把"事件影响"变成假数据。规则：偏离窗口中位数 5 倍以上的一律当坏点剔除。
    raw_prices = [p for _, p in ticks]
    if len(raw_prices) >= 5:
        med = sorted(raw_prices)[len(raw_prices) // 2]
        if med > 0:
            ticks = [(t, p) for t, p in ticks if 0.2 * med <= p <= 5.0 * med]
    prices = [p for _, p in ticks]
    # v323-6: 窗口里价格一笔没变 → 不是"事件没影响"，是**行情源在重复最后一价**。
    #         这类行会算出 amplitude=0，方向却靠扣基准变成 up/down（实测 249 行），
    #         属于假数据，直接不写。
    if len(set(prices)) < 2:
        return None
    max_high = max(prices)
    min_low = min(prices)
    up_move = (max_high - pre_close) / pre_close
    dn_move = (pre_close - min_low) / pre_close
    # v323-3: impact_pct 口径统一为**幅度（恒非负）**，方向只由 direction 列承担。
    #         库里 3,596 行是负数（旧写入残留），已在修复脚本里归一为绝对值。
    impact_pct = max(0.0, max(up_move, dn_move))
    # v322: 方向 = **窗口末价** vs 事件前收盘 的**超额**变化
    # （旧实现用窗口内极值猜方向 → 用的是振幅不是方向 → up/down 几乎 1:1）
    close_after = prices[-1]
    bench_move = _market_bench_move(conn, symbol, t_start, t_end)
    direction = _classify_direction(pre_close, close_after, bench_move)
    severity = _classify_severity(impact_pct)
    coverage_sec = int(ticks[-1][0] - ticks[0][0])
    # v323-7: 报价覆盖不足 30 分钟 → 不足以代表"事件后 4 小时"，不落库
    if coverage_sec < _MIN_COVERAGE_SEC:
        return None

    raw_imp = int(row["importance_score"] or 0)
    if raw_imp >= 4:
        importance = "high"
    elif raw_imp >= 2:
        importance = "medium"
    else:
        importance = "low"

    title = _clean_headline(row["title"])

    cur = conn.execute(
        """INSERT OR IGNORE INTO event_kline_impact
           (event_source, event_id, event_title, event_time, importance,
            asset, pre_event_close, max_high, min_low, impact_pct,
            impact_window_start, impact_window_end,
            impact_severity, direction, coverage_sec, pre_gap_sec, basis, asset_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(row["event_source"] or "")[:32],
            str(int(row["event_id"])),
            title,
            event_time,
            importance,
            symbol,
            pre_close,
            max_high,
            min_low,
            round(impact_pct, 6),
            t_start,
            t_end,
            severity,
            direction,
            coverage_sec,
            pre_gap,
            basis,
            asset_raw,
        ),
    )
    # v323: 不再逐行 commit（旧实现每行一次提交，500 行/轮会反复撞写锁），
    # 由 run_once / 修复流程批量提交。
    return int(cur.lastrowid or 0) if cur.rowcount > 0 else None


def _repair_zero_range(conn: sqlite3.Connection, log: bool = True) -> dict:
    """v323: 自愈「最高价=最低价」的假平盘行。

    这些行是旧版在数据未落地时抢先写入、又被 UNIQUE 幂等永久锁定的占位。
    现在逐条回到源事件重算：能算就覆盖，算不出（价格确实不支持）就删。
    原表已整表备份至 runtime/backup_impact_20261112.db。
    """
    rows = conn.execute(
        """SELECT id, event_source, event_id, asset FROM event_kline_impact
            WHERE max_high = min_low"""
    ).fetchall()
    fixed = removed = 0
    for r in rows:
        try:
            src = conn.execute(
                """SELECT ne.id AS event_id, ne.source AS event_source, ne.news_id,
                          ne.ts AS event_time, ne.asset, ne.score, ne.status,
                          rn.content AS title,
                          COALESCE(rn.gate_strength, 0) AS importance_score
                     FROM news_events ne
                     LEFT JOIN raw_news rn ON rn.id = ne.news_id
                    WHERE ne.id = ? AND ne.asset = ?""",
                (int(r["event_id"]), r["asset"]),
            ).fetchone()
            conn.execute("DELETE FROM event_kline_impact WHERE id = ?", (r["id"],))
            rid = process_one_event(conn, src) if src else None
            if rid:
                fixed += 1
            else:
                removed += 1
        except Exception as exc:
            if log:
                print(f"[KLINE-IMPACT] repair {r['id']} failed: {type(exc).__name__}: {exc}",
                      flush=True)
    conn.commit()
    if log and (fixed or removed):
        print(f"[KLINE-IMPACT] repair: fixed={fixed} removed={removed}", flush=True)
    return {"fixed": fixed, "removed": removed}


def _drop_misattributed(conn: sqlite3.Connection, log: bool = True) -> int:
    """v323: 清掉「事件当时挂的这个品种，事后已被改正」的残留行。

    UNIQUE 是 (event_source, event_id, asset)，所以 news_events.asset 被
    news_score_worker 改正后，旧错配行既不会被覆盖也不会消失 —— 这就是
    「一个事件挂多个品种」的来源。
    """
    rows = conn.execute(
        """SELECT eki.id, eki.event_id, eki.asset
             FROM event_kline_impact eki
             JOIN news_events ne ON ne.id = CAST(eki.event_id AS INTEGER)
            WHERE ne.asset != eki.asset
              AND ne.asset <> 'NONE'
              AND ne.asset <> ''"""
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM event_kline_impact WHERE id = ?", (r["id"],))
    conn.commit()
    if log and rows:
        print(f"[KLINE-IMPACT] drop_misattributed: {len(rows)}", flush=True)
    return len(rows)


def _unmeasurable_backlog(conn: sqlite3.Connection, lookback_days: int) -> dict:
    """v323 / v453b: 把"永远算不了"的事件按标签计数，让它可见。

    v453b 修正口径：原来只按 ne.asset 判断，于是"个股=507"里混着大量
    **标题已写明英伟达/比特币**、只是抽取层给了类别名的事件 —— 现在这些
    会被 _resolve_category_asset 绑到具体标的上并正常测量，不该再算进"无源"。
    所以这里逐条试解析，只统计解析后仍然落不到可交易标的上的那些。
    """
    cutoff = int(time.time()) - lookback_days * 86400
    tradable = _tradable_cached(conn)
    rows = conn.execute(
        """SELECT ne.asset,
                  COALESCE(NULLIF(ne.headline, ''), rn.content) AS title
            FROM news_events ne
            LEFT JOIN raw_news rn ON rn.id = ne.news_id
            LEFT JOIN event_kline_impact eki
                   ON eki.event_source = ne.source
                  AND eki.event_id = CAST(ne.id AS TEXT)
                  AND (eki.asset = ne.asset OR eki.asset_raw = ne.asset)
           WHERE ne.status IN ('PENDING','DONE') AND ne.ts >= ?
             AND ne.asset != 'NONE' AND eki.id IS NULL""",
        (cutoff,),
    ).fetchall()
    out: dict = {}
    for asset, title in rows:
        sym = _resolve_category_asset(str(asset or ""), title, tradable)
        if str(sym).upper() in tradable:
            continue          # 能绑到有行情的标的 → 不算"无源"
        out[str(asset)] = out.get(str(asset), 0) + 1
    return out

def run_once(lookback_days: int = _LOOKBACK_DAYS,
             log: bool = True,
             repair: bool = True) -> dict:
    # docstring in English only to avoid SyntaxError on legacy encoders
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA busy_timeout=8000")
        except sqlite3.OperationalError:
            pass
        _ensure_quality_columns(conn)
        rows = _fetch_pending_events(conn, lookback_days)
        seen = len(rows)
        written = 0
        skipped = 0
        for i, row in enumerate(rows, 1):
            # v453: 撞锁时要**立刻 rollback 释放写锁**，否则这条连接嘴里一直衔着
            # 一个未提交事务（WAL 里同一时刻只允许一个写者），全系统其它写者
            # （api 启动迁移、source_status、行情落库）都会被它拖到 busy_timeout 超时。
            # 实测这条 worker 是 engine.err.log 里 3846 条 database is locked 的主要来源，
            # 且它重试 6 次仍在失败 —— 不是等得不够久，是它自己就是占锁的人。
            # 改法：每条事件处理完立刻 commit（synchronous=NORMAL，代价可忽略），
            # 失败立刻 rollback 再退避重试，把写锁持有窗口压到单条事件的毫秒级。
            for attempt in range(6):
                try:
                    rid = process_one_event(conn, row)
                    if rid:
                        written += 1
                    else:
                        skipped += 1
                    conn.commit()
                    break
                except sqlite3.OperationalError as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if "locked" in str(exc).lower() and attempt < 5:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    skipped += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if log:
                        print(
                            f"[KLINE-IMPACT] event_id={row['event_id']} "
                            f"asset={row['asset']} failed: "
                            f"{type(exc).__name__}: {exc}", flush=True,
                        )
                    break
                except Exception as exc:
                    skipped += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if log:
                        print(
                            f"[KLINE-IMPACT] event_id={row['event_id']} "
                            f"asset={row['asset']} failed: "
                            f"{type(exc).__name__}: {exc}", flush=True,
                        )
                    break
            if i % _COMMIT_EVERY == 0:
                conn.commit()
        conn.commit()

        rep = {"fixed": 0, "removed": 0}
        dropped = 0
        if repair:
            dropped = _drop_misattributed(conn, log=log)
            rep = _repair_zero_range(conn, log=log)

        if log:
            print(
                f"[KLINE-IMPACT] run_once: seen={seen} "
                f"written={written} skipped={skipped} "
                f"repair_fixed={rep['fixed']} repair_removed={rep['removed']} "
                f"dropped_misattr={dropped}",
                flush=True,
            )
        backlog = _unmeasurable_backlog(conn, lookback_days)
        if log and backlog:
            top = sorted(backlog.items(), key=lambda kv: -kv[1])[:6]
            print(
                "[KLINE-IMPACT] 无行情源的事件（不进队列，需补数据源）: "
                + ", ".join(f"{k}={v}" for k, v in top),
                flush=True,
            )
        return {
            "events_seen": seen, "written": written, "skipped": skipped,
            "repair_fixed": rep["fixed"], "repair_removed": rep["removed"],
            "dropped_misattributed": dropped,
            "unmeasurable_assets": backlog,
        }
    finally:
        conn.close()


def run_daemon(interval_sec: int = _DAEMON_INTERVAL_SEC) -> None:
    print(f"[KLINE-IMPACT] daemon 启动: interval={interval_sec}s", flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(
                f"[KLINE-IMPACT] daemon 失败: {type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(int(interval_sec))


def main() -> None:
    p = argparse.ArgumentParser(description="v201 #2 event_kline_impact worker")
    p.add_argument("--daemon", action="store_true", help="15min 循环")
    p.add_argument("--once", action="store_true", help="立即跑一次（默认）")
    p.add_argument("--lookback-days", type=int, default=_LOOKBACK_DAYS)
    p.add_argument("--interval-sec", type=int, default=_DAEMON_INTERVAL_SEC)
    args = p.parse_args()
    if args.daemon:
        run_daemon(interval_sec=args.interval_sec)
    else:
        run_once(lookback_days=args.lookback_days)


if __name__ == "__main__":
    main()