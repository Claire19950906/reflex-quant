#!/usr/bin/env python3
"""calc_event_move_attribution.py - v291 enhanced.
Link critical_price_moves with news_events into event_move_attribution.

Four-tier matching (v291+):
  1) Direct asset tag hit (+0.15)
  2) Headline/body semantic hit (incl. synonyms / Chinese names) (+0.10)
  3) Cross-asset semantic: same group + same direction (+0.03)
  4) Macro news: no asset tag + high score (+0.05)
"""
from __future__ import annotations
import os
import re
import sqlite3
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

import epoch_gate
from typing import Any, Dict, List

try:
    from cem_constants import ASSET_SYNONYMS, CROSS_ASSET_GROUPS, normalize_asset_tag
except ImportError:
    ASSET_SYNONYMS = {
        "BTC": ["BTC", "bitcoin"], "ETH": ["ETH", "ethereum"],
        "SOL": ["SOL", "solana"], "XAU": ["XAU", "gold"],
        "WTI": ["WTI", "crude oil", "crude"],
        "NVDA": ["NVDA", "nvidia"], "TSLA": ["TSLA", "tesla", "elon musk"],
        "MU": ["MU", "micron"], "SKHYNIX": ["SKHYNIX", "sk hynix", "000660.KS"],
        "ADA": ["ADA", "cardano"], "DOGE": ["DOGE", "dogecoin"],
        "XRP": ["XRP", "ripple"], "BNB": ["BNB", "binance coin"],
        "AAPL": ["AAPL", "apple"], "MSFT": ["MSFT", "microsoft"],
        "GOOGL": ["GOOGL", "google", "alphabet"],
        "AMZN": ["AMZN", "amazon"], "META": ["META", "facebook"],
    }
    CROSS_ASSET_GROUPS = {
        "crypto":  ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
        "metal":   ["XAU"], "energy": ["WTI"],
        "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "memory":  ["MU", "SKHYNIX"],
    }
    # v332 fallback: 与 cem_constants 同口径 (导入失败时也必须能做代码归一,
    # 否则 SKHYNIX 这类"新闻用原始交易所代码"的标的又会整批丢归因)
    _TAG_ALIASES = {"000660": "SKHYNIX", "000660.KS": "SKHYNIX", "SK HYNIX": "SKHYNIX",
                    "HYNIX": "SKHYNIX", "005930": "SAMSUNG", "005930.KS": "SAMSUNG",
                    "INDIVIDUAL_STOCK": "", "US_STOCK": "", "个股": ""}

    def normalize_asset_tag(tag: str) -> str:
        if not tag:
            return ""
        t = str(tag).strip()
        if not t:
            return ""
        return _TAG_ALIASES.get(t, _TAG_ALIASES.get(t.upper(), t.upper()))

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("TRIDENT_DB_PATH") or (BACKEND_DIR / "trident_event_bus.db"))

TIME_WINDOW_SEC = 432000  # 5 days (v291+ 扩展: news 实时采集, moves 历史回填, 跨度大)
MACRO_NEWS_SCORE = 0.7

# ── v323 P0-2【归因三道闸】────────────────────────────────────────────
# 背景（2026-11-11 只读巡检实测）：
#   · 173 个异动 → 498 条归因行（平均 **2.9 事件/异动**）→ 一个异动挂多条"原因"，
#     统计上回答不了"到底是哪一条新闻造成的"。
#   · **负延迟 25 条**（事件发生在异动**之后**）→ 把"结果"当"原因"。
#   · AI 判「不成立」的行仍留在主表（ETH -3.31%，置信 0.94–0.97）。
#
# 三道闸：
#   闸一  一异动只挂 **1** 条主归因；其余进 candidates 表（只作展示，不参与统计）
#   闸二  时间纪律：事件必须发生在异动**之前**（lag>=0），且不得早于该形态的
#         归因窗口上限；早 1~6 小时的降权 ×0.5
#   闸三  大异动（|move_pct|>=5%）**必须先过 AI 复核**才准进主表（先落 pending 区）
#
# ── v325 闸二按形态分级（慢涨/慢跌归因 0 覆盖的根因）──────────────────────
# 实测（2026-11-13 只读）：slow_rise 111 条 → 归因 **0** 条；slow_fall 141 → 5 条。
# 根因是口径自相矛盾：`_window_for_kind` 给慢形态 ±3d 的候选窗口，
# 但 `_apply_lag_gates` 对**所有**形态硬切 lag>6h —— 慢涨本来就是几天内逐步走出来的，
# 真正起作用的新闻几乎必然早于 6h，于是候选在闸门里被全部丢掉。
# 修法：闸二上限跟着形态走（快 6h / 慢 72h），远距离不再"一刀切丢弃"而是分档降权，
#       并保留 lag<0 的绝对禁止（事后拟合永远不算因果）。
TOP_N_PER_MOVE = 1              # 闸一：主表每个异动只放 top1
CANDIDATES_PER_MOVE = 3         # 落选候选另存 candidates 表（展示用）
LAG_MAX_SEC = 21600             # 闸二默认上限（`_apply_lag_gates` 不传参时的向后兼容口径）
LAG_MAX_FAST_SEC = 21600        # 快涨/快跌：±6h（与默认同值，显式命名避免漂移）
LAG_MAX_SLOW_SEC = 259200       # 慢涨/慢跌：±72h（覆盖 3d 形态周期）
LAG_MAX_DEFAULT_SEC = 86400     # 形态未知：±1d（与 `MOVE_WINDOW_DEFAULT_SEC` 同口径）
LAG_PENALTY_SEC = 3600          # 闸二：早于异动超过 1h → 分数 ×0.5
LAG_PENALTY_FACTOR = 0.5
LAG_PENALTY_MID_FACTOR = 0.35   # 6h~24h 档：再乘 0.35（慢形态专用档位）
LAG_PENALTY_FAR_FACTOR = 0.25   # 24h~72h 档：再乘 0.25
LAG_MID_SEC = 21600             # 6h
LAG_FAR_SEC = 86400             # 24h
BIG_MOVE_ABS_PCT = 5.0          # 闸三：|move_pct|>=5% 的大异动强制 AI 复核
# v334 降级放行门槛。实测 38 条被放行的大异动里 21 条分数 <0.35（最低 0.11,
# 新闻挂在 32 小时前）—— 那是"标题里碰巧出现了这个代码", 不是因果。中等证据
# 以上才配绕过 AI 复核, 其余的进待复核排队等通道恢复。
DEGRADE_MIN_SCORE = 0.35
# 判断"AI 复核在线"需要看到多久以内的成功记录（见 _ai_verify_available）
AI_VERIFY_FRESH_DAYS = 14


def _ai_verify_available(conn: sqlite3.Connection | None = None) -> bool:
    """AI 复核通道**真的能用**吗（不是"配了个 key 就算"）。

    v334: 降级放行是个**自动开关**，不是给主表永久开的洞：
      · AI 通道恢复 → 闸三恢复原口径（大异动必须复核），放行自然关闭；
      · AI 再挂 → 自动重新降级，大异动不至于集体消失。

    判断依据不能只看 `ARK_API_KEY` 在不在 —— key 在但配额烧光（minimax 现在
    就是这个状态）会被误判成"在线"，于是闸三重新锁死、待复核区无声堆积。
    所以要求"最近 AI_VERIFY_FRESH_DAYS 天内真的写出过复核结果"才算在线。
    这道证据也解决了鸡生蛋问题：待复核区里的行会被 `attribution_ai_verify.py`
    复核并迁入主表，第一笔成功后开关就自动翻回在线。
    """
    if not (os.environ.get("ARK_API_KEY") or "").strip():
        return False
    if conn is None:
        return True
    try:
        recent = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ai_called=1 AND move_ts >= ?",
            (int(time.time()) - AI_VERIFY_FRESH_DAYS * 86400,),
        ).fetchone()[0]
    except sqlite3.Error:
        return True
    return int(recent or 0) > 0


def route_insert(ins: tuple, verdict: str | None, ai_online: bool) -> str:
    """v323 闸三的分流判定（纯函数，便于测试）。

    返回 'skip' / 'main' / 'pending'：
      · AI 判 reject → skip（不成立的行不许进任何区）
      · AI 判 confirm/partial → main
      · 小异动 → main
      · 大异动且 AI 不在线、但 match=ASSET 且分数 >= DEGRADE_MIN_SCORE → main（降级放行）
      · 其余 → pending（等 AI 通道恢复后复核）
    """
    if verdict == "reject":
        return "skip"
    if verdict in ("confirm", "partial"):
        return "main"
    if abs(float(ins[11] or 0.0)) < BIG_MOVE_ABS_PCT:
        return "main"
    if (not ai_online
            and (ins[16] or "").upper() == "ASSET"
            and float(ins[4] or 0.0) >= DEGRADE_MIN_SCORE):
        return "main"
    return "pending"
LOW_SCORE_MIN = 0.15            # v325：主表归因的最低证据门槛（低于此值只可能是巧合）

# v308 P1-fix3: 量纲污染阈值, 与 bridge_server.MAX_PLAUSIBLE_MOVE_PCT 保持一致。
# |move_pct| > 90% 对任何资产的一日/多日涨跌都不成立 → 不是市场异动, 是数据污染。
# 两边必须同值, 否则"生产端拦一道、消费端又按另一个阈值算"会出现口径分裂。
MAX_PLAUSIBLE_MOVE_PCT = 90.0

# ── v321.5 P0: 窗口自适应 + macro 系统性门槛 + semantic 只认标题 ──────────
# 触发: AI 双重验证实跑 728 条归因, 判定 confirm 47 / partial 121 / reject 560 (77%)。
# 按匹配类型拆: match=macro 311 条**零确认** · match=semantic 119 条**零确认**
# · match=asset 299 条确认率 15.7%。
#
# 三个根因 (都是口径问题, 不是模型问题):
#  1. 所有形态共用一个 TIME_WINDOW_SEC=5 天。快涨/快跌本身只持续 ~60 分钟,
#     拿 5 天窗口里的任何一条新闻去解释它在时间上就不成立。
#  2. macro 分支零门槛: 只要新闻没品种标签且分够高, 就能配给**任何**品种的**任何**异动。
#     宏观新闻能不能解释某个品种, 前提是"这次是全市场一起动"。
#  3. semantic 分支把正文也算进来, 正文里顺带提到的品种名会被当成归因依据。
#
# 修法: 窗口按 move_kind 自适应; macro 要求同 15 分钟桶内 ≥3 品种同时异动;
#       semantic 只认标题。
MOVE_WINDOW_FAST_SEC = 3 * 3600        # 快涨/快跌: ±3h
MOVE_WINDOW_SLOW_SEC = 3 * 86400       # 慢涨/慢跌: ±3d
MOVE_WINDOW_DEFAULT_SEC = 86400        # 形态未知: ±1d
MACRO_SYSTEMIC_MIN_ASSETS = 3          # macro 归因要求同桶内同时异动的品种数
MACRO_BUCKET_SEC = 900                 # 系统性判定桶宽 (15 分钟)
MACRO_SCORE_CAP = 0.30                 # macro 归因的分数上限 (弱关联, 不许冒充实证)


def _window_for_kind(move_kind: str, duration_min: int) -> int:
    """按异动形态给归因窗口。快形态用小时级, 慢形态用天级。"""
    k = (move_kind or "").lower()
    if k.startswith("fast"):
        return MOVE_WINDOW_FAST_SEC
    if k.startswith("slow"):
        return MOVE_WINDOW_SLOW_SEC
    if duration_min and int(duration_min) <= 180:
        return MOVE_WINDOW_FAST_SEC
    return MOVE_WINDOW_DEFAULT_SEC


def _lag_max_for_kind(move_kind: str, duration_min: int) -> int:
    """闸二的时间上限也按形态走 —— 与 `_window_for_kind` 同口径。

    否则会出现"窗口给 3 天、闸门只放 6 小时"的自相矛盾（慢形态必然 0 覆盖）。
    """
    k = (move_kind or "").lower()
    if k.startswith("slow"):
        return LAG_MAX_SLOW_SEC
    if k.startswith("fast"):
        return LAG_MAX_FAST_SEC
    if duration_min and int(duration_min) <= 180:
        return LAG_MAX_FAST_SEC
    return LAG_MAX_DEFAULT_SEC


def _slow_weak_blocked(match_type: str, is_slow: bool) -> bool:
    """慢形态只认直接命中本标的 (asset / semantic), `cross` 一律不进主表。

    慢涨/慢跌持续数天, "同组同向"的巧合概率极高。实测反例:
    BNB −5.07% 被挂到「tBTC 跨链桥恢复」、XRP −4.44% 被挂到「Machi 卖出无聊猿」。
    假因果会直接污染归因统计与反思结论, 比"没有归因"更有害。
    """
    return bool(is_slow and str(match_type or "").lower() == "cross")


# 标题方向词（只用于慢形态的方向一致性校验, 不参与快形态口径）
_UP_WORDS = ("上涨", "涨超", "涨逾", "大涨", "反弹", "走高", "飙升", "拉升", "新高",
             # v332: 中文财经标题极高频的"模糊多头"词, 之前漏掉导致方向矛盾漏放
             # (实测「SK海力士涨8.6%」解释 −7.08% 的下跌仍被判通过)。
             "走强", "上扬", "攀升", "上行", "突破", "站上", "冲高", "高开",
             "利好", "涨")
_DOWN_WORDS = ("下跌", "跌破", "跌超", "跌逾", "大跌", "跳水", "走低", "重挫", "暴跌",
               "回落", "新低", "失守",
               "走弱", "走软", "下挫", "承压", "利空", "低开", "收跌", "深跌",
               "跌")


_HASH_PREFIX_RE = re.compile(r"^\s*\[hash:[0-9a-fA-F]+\]\s*")

# v342: 来源别名归一 —— 同一家媒体在库里被拆成两个 source，统计和权重都会失准
SOURCE_ALIASES = {
    "东方财富": "eastmoney",
    "TechFlow 深潮": "techflow",
    "feishu_jin10": "jin10",
    "techflow深潮": "techflow",
}

# v342: 来源可信度（依据本库实测均分/命中率，见 _source_trust 的注释）
SOURCE_TRUST = {
    "eastmoney": 1.2,
    "techflow": 1.05,
    "jin10": 0.95,
    "finnhub": 0.85,
    "reuters_business": 0.80,
    "WEB:tree_news": 0.70,
    "WEB:Blogs": 0.0,
    "jin10_calendar": 0.0,
    "sina": 0.90,
    "wscn": 0.90,
}


def _strip_hash_prefix(raw) -> str:
    """剥掉新闻标题开头的跨源去重指纹 `[hash:xxxx]`。

    实测近 7 天 jin10 / finnhub / 东方财富 / tree_news 等来源的 headline
    100% 带这个前缀（指纹被写进了标题列）。之前归因把它当"无标题"整条丢弃，
    等于把大部分新闻源废掉了 —— 实际去掉前缀后正文是完整的。
    """
    s = _HASH_PREFIX_RE.sub("", str(raw or "")).strip()
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.splitlines()[0]).strip()


def _source_trust(raw_src) -> float:
    """v342: 来源可信度系数（乘在证据分上，不是硬删）。

    数据来自本库实测 `news_events` 的均分与 ≥0.5 占比：
        eastmoney 0.54 / 东方财富 0.475 / techflow 0.35 / jin10 0.34
        finnhub 0.30 / feishu_jin10 0.30 / reuters_business 0.23
        WEB:tree_news 0.20 / 空来源 0.21 / TechFlow深潮 0.25
        WEB:Blogs 0.09 / jin10_calendar 0.004
    结论：同一套打分在不同源上的"命中率"差 6 倍，一视同仁等于让噪音源靠篇数
    挤掉真证据。系数只调权重：高质量源 ×1.2，中性 ×1.0，噪音 ×0.7，
    日历/博客聚合 ×0（与"事件驱动"无关，不参与归因）。
    """
    s = str(raw_src or "").strip()
    s = SOURCE_ALIASES.get(s, s)
    if s == "" or s.lower() in ("unknown", "web", "none"):
        return 0.9
    return SOURCE_TRUST.get(s, 1.0)


def _headline_direction(headline: str) -> int:
    """从标题读出涨跌方向: 1=偏多 / -1=偏空 / 0=读不出或方向混杂。"""
    h = headline or ""
    up = any(w in h for w in _UP_WORDS)
    dn = any(w in h for w in _DOWN_WORDS)
    if up and not dn:
        return 1
    if dn and not up:
        return -1
    return 0


def _direction_blocked(headline: str, move_pct: float) -> bool:
    """标题方向与异动方向相反 → 不是原因（全形态通用）。

    依据（v325 实测残留）:
      · SOL **fast_rise +1.57%** ←「SOL 跌破 95 美元, 24 小时跌幅 0.84%」
      · MU  **fast_rise +1.62%** ←「AI 硬件与半导体板块大幅下挫, Lumentum 跌超 11%」
    "利好解释下跌 / 利空解释上涨"只可能是循环论证或时间错配。
    方向混杂（同时含涨跌词，如「纳指跌、芯片涨」）时 `_headline_direction` 返回 0,
    不做判断 —— 避免误杀真实的跨资产事件。
    """
    hd = _headline_direction(headline)
    if hd == 0:
        return False
    move_dir = 1 if (move_pct or 0) > 0 else -1
    return hd != move_dir


_PRICE_ECHO_VERBS = ("跌破", "突破", "涨破", "涨超", "跌超", "涨逾", "跌逾",
                     "失守", "站上", "跌穿", "涨穿")
_PRICE_ECHO_MARKS = ("美元", "关口", "24 小时", "24小时", "24h")


def _is_price_echo(headline: str) -> bool:
    """标题是不是"行情播报"（价格本身的结果, 而不是原因）。

    例: 「SOL 跌破 95 美元，24 小时跌幅 0.84%」「BNB 跌破 700 美元」。
    这类标题天然滞后于价格, 拿它解释同一波涨跌属循环论证。
    """
    h = headline or ""
    if not any(v in h for v in _PRICE_ECHO_VERBS):
        return False
    return any(m in h for m in _PRICE_ECHO_MARKS)


# ── v337: 存量归因治理 (行情播报剔除 / 孤儿迁移) ────────────────────────
# 背景: `_is_price_echo` 从 v325 就写好了, 但**从未被调用** —— 存量 268 条归因里
# 107 条(39.9%)指向"跌破/涨破/24小时跌幅"这类自指标题, 属循环论证。
# 这里补两条治理路径, 都把行**迁到 rejected 留痕**, 不直接删除。
_REJECT_COLUMNS = (
    "move_id", "news_event_id", "asset", "time_lag_sec", "algo_score", "algo_reasons",
    "ai_called", "ai_explanation", "ai_confidence", "ai_verdict", "move_ts", "move_pct",
    "move_kind", "duration_min", "ev_news_id", "ev_source", "ev_action", "ev_ts",
)


def _relocate_to_rejected(conn: sqlite3.Connection, ids: List[int], reason: str,
                          dry_run: bool = False) -> int:
    """把主表里的指定行迁到 rejected 表 (先插后删, 单事务), 返回迁移行数。"""
    if not ids:
        return 0
    if dry_run:
        return len(ids)
    cols = ",".join(_REJECT_COLUMNS)
    placeholders = ",".join(["?"] * len(_REJECT_COLUMNS))
    moved = 0
    # 用 SAVEPOINT 而不是 BEGIN IMMEDIATE: 调用方可能已经处在事务里
    # (pytest / 手动脚本), BEGIN 会报 "cannot start a transaction within a transaction"。
    # SAVEPOINT 在事务外等价于 BEGIN, RELEASE 等价于 COMMIT。
    conn.execute("SAVEPOINT v337_relocate")
    try:
        for rid in ids:
            row = conn.execute(
                "SELECT %s FROM event_move_attribution WHERE id=?" % cols, (int(rid),)
            ).fetchone()
            if not row:
                continue
            conn.execute(
                "INSERT INTO event_move_attribution_rejected "
                "(orig_row_id,%s,reject_reason,created_at) "
                "VALUES (?,%s,?,datetime('now','localtime'))" % (cols, placeholders),
                (int(rid),) + tuple(row) + (reason,),
            )
            conn.execute("DELETE FROM event_move_attribution WHERE id=?", (int(rid),))
            moved += 1
        conn.execute("RELEASE v337_relocate")
    except Exception:
        conn.execute("ROLLBACK TO v337_relocate")
        conn.execute("RELEASE v337_relocate")
        raise
    return moved


def purge_price_echo(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """把"行情播报类"归因迁出主表 (循环论证, 不是因果)。"""
    ids: List[int] = []
    for rid, headline in conn.execute(
        "SELECT a.id, e.headline FROM event_move_attribution a "
        "JOIN news_events e ON a.news_event_id=e.id"
    ).fetchall():
        if _is_price_echo(headline):
            ids.append(int(rid))
    moved = _relocate_to_rejected(conn, ids, "price_echo", dry_run=dry_run)
    return {"matched": len(ids), "moved": moved, "dry_run": bool(dry_run)}


def repair_orphans(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """归因行的 move_id 已不存在 (异动被重建/删除) → 迁出主表。

    这类行 join 不上 critical_price_moves, 在覆盖率和方向一致率统计里
    会以 NULL 形式混进分母, 让指标失真 (实测 87/268 = 32.5%)。
    """
    ids = [
        int(r[0]) for r in conn.execute(
            "SELECT a.id FROM event_move_attribution a "
            "LEFT JOIN critical_price_moves m ON a.move_id=m.id WHERE m.id IS NULL"
        ).fetchall()
    ]
    moved = _relocate_to_rejected(conn, ids, "orphan_move", dry_run=dry_run)
    return {"matched": len(ids), "moved": moved, "dry_run": bool(dry_run)}


# ── v309 P0-2: 技术信号确认项 (tech_signals) ──────────────────────────────
# 背景: 归因打分此前只看"新闻匹配", 完全没考虑**技术面**是否已经给出信号。
# 一波行情若是技术面 (背离/量能/支撑破位) 先动、新闻后到, 那么"新闻是起因"
# 这个归因就该被打折 —— 否则会把技术驱动的行情误记成消息驱动。
#
# 未来函数纪律 (与 tech_detector.py 一致):
#   · tech_signals.uses_future=1 的行是"转折点"信号, 用了未来 la 根 bar,
#     **绝不能**参与打分 (会引入未来信息, 让归因分虚高);
#   · 只用 uses_future=0 的纯因果信号;
#   · 时间上只认"发生在异动之前或当时"的信号 (signal.ts <= move.ts + 容忍窗口),
#     之后的信号对解释这波异动无因果意义。
#
# 权重设计: 确认项只做**小幅调整** (上限 ±0.08), 不喧宾夺主 ——
# 归因主体仍是"哪条新闻解释了这波异动", 技术项只是方向性微调。
TECH_CONFIRM_MAX_BONUS = 0.05      # 技术面同向确认时的最大加分
TECH_CONFIRM_MAX_PENALTY = 0.03     # 技术面反向时的最大扣分
TECH_SIGNAL_WINDOW_SEC = 6 * 3600  # 技术信号相对异动的时间容忍窗口 (6h)
TECH_SIGNAL_MIN_SCORE = 0.45       # 低于此分的技术信号不参与 (噪声太大)


def _load_tech_signals(conn, cutoff: int) -> dict:
    """加载技术信号 → {asset: [(ts, score, trigger_type), ...]}。

    只取 uses_future=0 的纯因果信号 (未来函数行直接丢弃)。
    表不存在 (未跑 backfill_tech_signals) 时返回空 dict, 不影响主流程。
    """
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT asset, ts, score, trigger_type FROM tech_signals "
            "WHERE ts >= ? AND uses_future = 0",
            (int(cutoff),),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            asset = (r[0] or "").upper()
            ts = int(r[1])
            score = float(r[2] or 0.0)
            ttype = str(r[3] or "")
        except (TypeError, ValueError, IndexError):
            continue
        if not asset or ts <= 0:
            continue
        out.setdefault(asset, []).append((ts, score, ttype))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def _tech_confirmation(tech_by_asset: dict, asset: str, move_ts: int,
                        move_pct: float) -> tuple:
    """算这波异动的技术面确认项 → (调整量, 说明文本)。

    规则:
      · 找该 asset 在 [move_ts - 6h, move_ts] 内、score 达标的因果技术信号;
      · 取**距 move_ts 最近**的那条 (最近的技术信号对当下最有解释力);
      · 同向 (技术信号方向与 move 方向一致) → 加分 (技术面也在这走, 新闻归因更可信);
      · 反向 → 扣分 (技术面当时不支持这个方向, 新闻归因更可疑);
      · 无信号 → (0.0, "")。

    未来函数纪律 (关键): 上界是 **move_ts 本身**, 不是 move_ts + 窗口。
    之前实现用 [move_ts-6h, move_ts+6h], 采信了异动**之后**才产生的技术信号
    —— 那正是"事后看很准、事前不可能知道"的信息, 属于未来函数,
    会让归因分虚高 (典型表现: 涨完之后出现 bottom 信号 → 反而被当成看涨确认)。
    因果归因只能用"异动发生前已经存在"的技术信号。
    """
    sigs = tech_by_asset.get((asset or "").upper()) or []
    if not sigs:
        return (0.0, "")
    lo = move_ts - TECH_SIGNAL_WINDOW_SEC
    hi = move_ts          # 只认异动当时及之前 → 无未来函数
    best = None
    best_dist = None
    for ts, score, ttype in sigs:
        if ts < lo or ts > hi:
            continue
        if score < TECH_SIGNAL_MIN_SCORE:
            continue
        dist = abs(ts - move_ts)
        if best_dist is None or dist < best_dist:
            best = (ts, score, ttype)
            best_dist = dist
    if best is None:
        return (0.0, "")
    ts, score, ttype = best
    # 技术信号方向: 触发类型含 divergence/volume/pct 时用 score 方向无法判定,
    # 故以"信号与异动同资产同时出现"为方向代理 —— 这里用异动方向本身,
    # 真正的方向信息在 tech_signals.trigger_type 之外, 保守起见只按
    # "同向出现=确认" 处理, 反向扣分仅在 trigger_type 明确带方向时启用。
    direction_hint = ""
    if "top" in ttype or "bear" in ttype or "down" in ttype:
        direction_hint = "down"
    elif "bottom" in ttype or "bull" in ttype or "up" in ttype:
        direction_hint = "up"
    if direction_hint:
        move_dir = "up" if (move_pct or 0) > 0 else "down"
        if direction_hint == move_dir:
            adj = TECH_CONFIRM_MAX_BONUS * min(1.0, score)
            return (adj, f"tech_confirm:{ttype}@{score:.2f}")
        adj = -TECH_CONFIRM_MAX_PENALTY * min(1.0, score)
        return (adj, f"tech_diverge:{ttype}@{score:.2f}")
    # 方向不可判定 → 只作"存在技术信号"的弱确认
    adj = TECH_CONFIRM_MAX_BONUS * 0.5 * min(1.0, score)
    return (adj, f"tech_present:{ttype}@{score:.2f}")


# ── v490 P1-2: 资金面确认项（OI / 多空比 / 费率 / 量能）────────────────────
# 背景（2026-11-19 复核）：归因理由 39 条没有一条出现资金/量能/流/OI/挂单关键词，
# 主流程只靠新闻时间窗+方向+标的匹配；flow_snapshots 已在采 OI/lsratio/funding/
# volume，但没接进归因判据。这里与技术确认同构：只取异动**之前**已存在的快照
# （无未来函数），同向资金行为加分、背离扣分，无数据则 0 调整。
FLOW_CONFIRM_MAX_BONUS = 0.045
FLOW_CONFIRM_MAX_PENALTY = 0.03
FLOW_SNAP_WINDOW_SEC = 6 * 3600
# 多空比/费率显著变动阈值（相对异动前基准的相对幅度），低于此视为平淡
FLOW_LSRATIO_MOVE = 0.08
FLOW_FUNDING_MOVE = 0.0005


def _load_flow_snapshots(conn: sqlite3.Connection, cutoff: int) -> dict:
    """加载资金面快照 → {asset: {"oi": [(ts, v)], "lsratio": [...], "funding": [...], "volume": [...]}}。

    表不存在/为空时返回空 dict，不阻断主流程。
    """
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT asset, kind, ts, value FROM flow_snapshots WHERE ts >= ?",
            (int(cutoff),),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            asset = (r[0] or "").upper()
            kind = str(r[1] or "").strip().lower()
            ts = int(r[2])
            val = float(r[3])
        except (TypeError, ValueError, IndexError):
            continue
        if not asset or kind not in ("oi", "lsratio", "funding", "volume"):
            continue
        out.setdefault(asset, {}).setdefault(kind, []).append((ts, val))
    for asset, groups in out.items():
        for kind, seq in groups.items():
            seq.sort(key=lambda x: x[0])
    return out


def _nearest_flow(seq: list, move_ts: int):
    """取异动时点之前、距离最近的一条快照；取不到返回 None。"""
    best = None
    best_dist = None
    for ts, val in seq:
        if ts > move_ts:
            continue
        dist = move_ts - ts
        if best_dist is None or dist < best_dist:
            best, best_dist = (ts, val), dist
    return best


def _flow_confirmation(flow_by_asset: dict, asset: str, move_ts: int,
                        move_pct: float) -> tuple:
    """资金面确认 → (调整量, 说明文本)。

    口径（保守、有界）：
      · 多空比 lsratio：异动前多空比抬升（多头占优）且价格上涨 → 同向确认；
        多空比走低而价格上涨 → 背离（上涨缺乏多头资金支撑）。
      · 费率 funding：费率走高与上涨同向（多头愿意付费持币）；费率走低/转负
        而上涨 → 弱背离。
      · OI：OI 与价格同向增 → 新资金进场推动；OI 降而价格涨 → 空头回补型上涨，
        记弱确认（区分不出强弱，只备注）。
      · volume：仅作存在性备注，不参与方向加减分。
    无任一指标 → (0.0, "")。
    """
    groups = flow_by_asset.get((asset or "").upper())
    if not groups:
        return (0.0, "")
    move_dir = 1 if (move_pct or 0) > 0 else -1
    # 取窗口内最早一条作基准、异动前最近一条作现价，算资金行为方向
    def _trend(kind: str):
        seq = groups.get(kind) or []
        if not seq:
            return None
        base = seq[0][1]
        near = _nearest_flow(seq, move_ts)
        if near is None:
            return None
        return base, near[1]

    evidence: list[str] = []
    score = 0.0
    # 多空比方向
    lr = _trend("lsratio")
    if lr is not None:
        base, near = lr
        if base and abs(base) > 1e-6:
            delta = (near - base) / abs(base)
            if abs(delta) >= FLOW_LSRATIO_MOVE:
                lr_dir = 1 if delta > 0 else -1
                if lr_dir == move_dir:
                    score += FLOW_CONFIRM_MAX_BONUS * 0.6
                    evidence.append(f"lsratio_{'多增' if move_dir>0 else '空增'}")
                else:
                    score -= FLOW_CONFIRM_MAX_PENALTY * 0.6
                    evidence.append(f"lsratio_diverge({delta:+.0%})")
    # 费率方向
    fd = _trend("funding")
    if fd is not None:
        base, near = fd
        delta = near - base
        if abs(delta) >= FLOW_FUNDING_MOVE:
            f_dir = 1 if delta > 0 else -1
            if f_dir == move_dir:
                score += FLOW_CONFIRM_MAX_BONUS * 0.4
                evidence.append("funding_align")
            else:
                score -= FLOW_CONFIRM_MAX_PENALTY * 0.4
                evidence.append("funding_diverge")
    # OI 方向（备注型，弱加减）
    oi = _trend("oi")
    if oi is not None:
        base, near = oi
        if base and abs(base) > 1e-6:
            oi_delta = (near - base) / abs(base)
            oi_dir = 1 if oi_delta > 0 else -1
            if oi_dir == move_dir:
                score += FLOW_CONFIRM_MAX_BONUS * 0.3
                evidence.append("oi_inflow")
            else:
                evidence.append("oi_decline")
    vol = groups.get("volume")
    if vol:
        evidence.append("vol_present")
    score = max(-FLOW_CONFIRM_MAX_PENALTY, min(FLOW_CONFIRM_MAX_BONUS, score))
    if not evidence:
        return (0.0, "")
    note = "flow:" + "/".join(evidence)
    return (round(score, 4), note)


def _apply_lag_gates(scored: list, max_lag_sec: int | None = None) -> tuple:
    """v323 P0-2 闸二：时间纪律硬过滤（**必须在 _rank_candidates 之前**调用）。

    四条规则（依据 11-11 只读巡检：负延迟 25 条、>6 小时 78 条）：
      1) lag < 0  → 事件发生在异动**之后** = 把"结果"当"原因" → 直接丢弃，
                     不进候选（原来只是排序靠后, 仍然留在主表里）。
      2) lag > max_lag_sec → 事件太早 → 丢弃。上限**按形态给定**
                     （快 6h / 慢 72h，见 v325 —— 旧口径对所有形态都切 6h，
                     把慢涨/慢跌的候选全部杀光）。
      3) 1h < lag <= 6h   → 分数 ×0.5（弱化但保留）。
      4) 6h < lag <= 24h  → 再 ×0.35；24h < lag <= 72h → 再 ×0.25。
                     慢形态的距离衰减用档位表达，而不是"要么全留要么全丢"。

    scored 元素: (score, n_id, n_src, lag, match_type, is_retrospective, headline)
    返回: (过滤后的 list, 统计 dict)
    """
    limit = LAG_MAX_SEC if max_lag_sec is None else int(max_lag_sec)
    kept = []
    st = {"dropped_negative_lag": 0, "dropped_too_far": 0, "lag_penalized": 0}
    for item in scored:
        score = item[0]
        lag = item[3]
        if lag < 0:
            st["dropped_negative_lag"] += 1
            continue
        if lag > limit:
            st["dropped_too_far"] += 1
            continue
        if lag > LAG_FAR_SEC:                       # 24h~72h
            score = round(float(score) * LAG_PENALTY_FAR_FACTOR, 4)
            st["lag_penalized"] += 1
        elif lag > LAG_MID_SEC:                     # 6h~24h
            score = round(float(score) * LAG_PENALTY_MID_FACTOR, 4)
            st["lag_penalized"] += 1
        elif lag > LAG_PENALTY_SEC:                 # 1h~6h
            score = round(float(score) * LAG_PENALTY_FACTOR, 4)
            st["lag_penalized"] += 1
        kept.append((score,) + tuple(item[1:]))
    return kept, st


def _rank_candidates(scored: list) -> list:
    """候选排序: **事前归因优先**。

    v310 P1 修复。旧排序只按 score 倒序 (tuple 倒序), 而 recency 只看 |lag| ——
    异动之后紧跟着发布的那条新闻 (lag<0, 事后拟合) 往往 |lag| 更小、recency 更高,
    于是把真正的触发因素 (lag>=0, 事前) 挤出 TOP_N。验收 #12 的口径是
    "真因果 = ev_news_id 有值 + time_lag_sec>=0 + 不是 spillover/macro_fallback/retro",
    所以候选排序必须让事前候选整体排在事后候选前面 (同一组内仍按 score 倒序)。

    scored 元素: (score, n_id, n_src, lag, match_type, is_retrospective)
    """
    return sorted(scored, key=lambda x: (x[5], -x[0], -x[1]))


def headline_contains_asset(headline, asset):
    if not headline:
        return False
    h = headline.upper()
    for kw in ASSET_SYNONYMS.get(asset, [asset]):
        if kw.upper() in h or kw in headline:
            return True
    return False


def build_news_asset_set(n_assets: str, n_asset: str) -> set[str]:
    """把新闻的资产标签(复数列 + 权威单数列)整理成大写集合。

    v332: 这里**保留原始标签**, 不做别名替换 —— 因为 "INDIVIDUAL_STOCK" / "US_STOCK"
    这类"资产桶"标签要留在集合里参与 cross / macro 的既有门槛判断 (桶非空 ⇒ 不算
    无标签宏观新闻)。代码归一 (000660 → SKHYNIX) 只发生在直配判定里。
    """
    s = {a.strip().upper() for a in (n_assets or "").split(",") if a.strip()}
    main = (n_asset or "").strip().upper()
    if main and main not in ("NONE", "AGGREGATE", "ALL"):
        s.add(main)
    return s


def asset_direct_match(move_asset: str, news_asset_set: set[str]) -> bool:
    """品种直配: 异动资产或其归一代码命中新闻标签(标签也先归一)。

    v332 背景: 新闻侧按交易所原始代码打标 (韩国 000660), 异动侧用平台代码
    (SKHYNIX), 字符串永不相等 → 直配通道对 SKHYNIX 全灭, 只剩标题弱匹配,
    又被 v321.5 的弱匹配过滤器丢光 (实测 82 条异动 / 45 条窗口内真新闻 → 归因 0)。
    这里只做**代码→平台代码**归一, 不引入任何新启发式。
    """
    m = (move_asset or "").strip().upper()
    if not m or not news_asset_set:
        return False
    m_norm = normalize_asset_tag(m)
    for t in news_asset_set:
        if t == m or normalize_asset_tag(t) == m_norm:
            return True
    return False


def _tag_evidence(conn, rows, news_idx: int = 14, reasons_idx: int = 5):
    """给每行归因打 evidence=<level>（v336）—— 只加标记，不改分数与筛选。"""
    if not rows:
        return rows
    try:
        import evidence_source as es
    except Exception:
        return rows
    ids = [r[news_idx] for r in rows if len(r) > news_idx and r[news_idx]]
    ctx = es.fetch_news_ctx(conn, ids)
    out = []
    levels = []
    for r in rows:
        try:
            nid = int(r[news_idx])
            hl, bd = ctx.get(nid, ("", ""))
            lv = es.classify_evidence(hl, bd, r[2])
            rr = list(r)
            rr[reasons_idx] = es.tag_reason(rr[reasons_idx], lv)
            r = tuple(rr)
            levels.append(lv)
        except Exception:
            pass
        out.append(r)
    if levels:
        s = es.summarize(levels)
        print(f"[INFO] 证据分层: headline={s['headline']} cross={s['cross_asset']} "
              f"body_only={s['body_only']} macro={s['macro']}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--enable-spillover", action="store_true",
                    help="Tier 2: 同组资产 24h spillover 归因 (Q4 覆盖率提升)")
    ap.add_argument("--enable-macro-fallback", action="store_true",
                    help="Tier 3: |move|>threshold 且无 news 时的 macro 归因")
    ap.add_argument("--macro-threshold", type=float, default=5.0,
                    help="macro fallback 触发阈值 (默认 |move_pct|>=5.0)")
    ap.add_argument("--spillover-window", type=int, default=86400,
                    help="spillover 时间窗 (默认 86400=24h)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算并打印统计, 不 DELETE / 不 INSERT")
    ap.add_argument("--allow-weak-attribution", action="store_true",
                    help="允许 semantic / macro 弱匹配进入归因表 (默认关闭)。v321.5 实测: 这两类被 AI 复核 84 条 0 确认 (macro 58/60 不成立, semantic 22/24 不成立)")
    ap.add_argument("--purge-price-echo", action="store_true",
                    help="v337 治理: 把'行情播报类'归因(跌破/涨破/24小时跌幅)迁到 rejected 表, 不重算")
    ap.add_argument("--repair-orphans", action="store_true",
                    help="v337 治理: 把 move_id 已不存在的归因行迁到 rejected 表, 不重算")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    cutoff = int(datetime.now(tz=timezone.utc).timestamp()) - args.days * 86400
    print(f"[INFO] Calc event_move_attribution since {datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    # v2026-10-27: 这个作业每 5 分钟跑一次, 很容易撞上 engine/ai_worker 的写锁 ——
    # 默认 5s busy_timeout 之外再加 30s 排队, 避免整轮作业因为一次锁直接失败
    # (失败 = 归因表停更, 而门禁 G01/G14 都依赖它)。
    conn.execute("PRAGMA busy_timeout=30000")
    # v321 P0 (锁冲突修复): 上面那条 busy_timeout 其实**救不了这个作业**。
    #   原因: python sqlite3 默认 isolation_level="" → 隐式 **deferred** 事务,
    #   写锁是在**第一条 DML (第 245 行的 DELETE)** 才去拿的。而本作业在
    #   DELETE 之前已经做过 SELECT —— SQLite 在 deferred 事务里把读锁升级成
    #   写锁时**不会重试**, 直接抛 SQLITE_BUSY, busy_timeout 对"升级"无效。
    #   实测: err.log 里 308 次 "[ERROR] database is locked", 每一次都意味着
    #   **整轮归因作废** (先 DELETE 再 INSERT, 失败 → 归因表停更)。
    #   修法: 手动事务控制 (isolation_level=None) + BEGIN IMMEDIATE ——
    #   BEGIN IMMEDIATE 在事务开始时就申请写锁, 此时 busy_timeout **会**等待重试。
    conn.isolation_level = None
    # ── v337 治理模式: 只迁移存量, 不重算 (跑完直接退出) ──────────────────
    if args.purge_price_echo or args.repair_orphans:
        result: Dict[str, Any] = {}
        if args.repair_orphans:
            result["orphans"] = repair_orphans(conn, dry_run=args.dry_run)
        if args.purge_price_echo:
            result["price_echo"] = purge_price_echo(conn, dry_run=args.dry_run)
        print("[V337-GOVERN]", result)
        return 0
    try:
        # v308 P1-fix3: 这里必须剔除量纲污染行 (|move_pct|>90%)。
        # 本表的 move_pct 是**直接拷贝**自 critical_price_moves.move_pct,
        # 所以污染行会被原样搬进 event_move_attribution, 再经 /api/v201/impact/by-asset
        # 以 "critical" severity 出现在影响面板, 还会把 price_react 顶到 0.3 上限
        # 抬高归因分数。源头不拦, 下游怎么过滤都是徒劳。
        # v337: 归档窗口纪律 —— 新闻库只有近期归档 (实测起点 2026-10-03),
        # 早于归档的异动**不存在可归因对象**, 留在分母里会让覆盖率永远到不了标
        # (实测 slow_rise 303 条里只有 16 条落在归档期内)。
        # 这里把 cutoff 抬到归档起点, 让"归因覆盖率"只统计**本可归因**的样本;
        # 归档前的异动由验收层单独标为"无归档, 不可归因", 不进分母。
        try:
            _news_start = conn.execute("SELECT MIN(ts) FROM news_events").fetchone()[0]
        except sqlite3.Error:
            _news_start = None
        if _news_start and cutoff < int(_news_start) - 86400:
            print(
                f"[INFO] 新闻归档起点 {datetime.fromtimestamp(int(_news_start), tz=timezone.utc).isoformat()}, "
                f"cutoff 由 {datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()} 上移"
            )
            cutoff = int(_news_start) - 86400
        # v340 纪元闸门: cutoff 不能越过 data_epoch，否则会把清空前的旧异动
        # 重新归因一遍（实测 30 行里有 19 行 move_ts 早于纪元）。
        _cut_old = cutoff
        cutoff = epoch_gate.clamp_since(cutoff, epoch_gate.epoch_ts(conn)) or cutoff
        if cutoff != _cut_old:
            print(f"[INFO] cutoff 抬到纪元: "
                  f"{datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()}")
        moves = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, duration_min, algo_score "
            "FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ? ORDER BY ts DESC",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchall()
        # v342: 新闻预取窗要比 cutoff 再往前推一个慢形态窗口。
        # 否则慢涨/慢跌的 ts 被纪元钳到 11-15 09:10 后，其 ±3d 归因窗口整个落在
        # 纪元之前，新闻一条都取不到 → 慢形态永远 0 归因（实测 3 条慢跌 0 认领）。
        news_cut = int(cutoff) - MOVE_WINDOW_SLOW_SEC
        news = conn.execute(
            "SELECT id, source, headline, body, assets, asset, score, ts, action "
            "FROM news_events WHERE ts >= ? ORDER BY ts DESC",
            (news_cut,),
        ).fetchall()
        print(f"[INFO] Loaded {len(moves)} moves, {len(news)} news events")
        if not moves or not news:
            print("[INFO] Nothing to do")
            return 0
        # ── G01: 先快照已有的 AI 双重验证结果 (必须在 DELETE 之前!) ──────────
        # 本脚本每 5 分钟重算一次并先 DELETE 再 INSERT, 而 AI 复核一次要花十几秒
        # (LLM 调用), 如果不保留, 每轮都会把 ai_called 清零 → "AI 双重验证"
        # 永远显示 0 次执行。这里按 (move_id, news_event_id) 把上一轮的复核
        # 结果带回来, 新出现的归因行才需要重新复核 (由 attribution_ai_verify 增量补)。
        # ⚠ 快照必须在 DELETE 之前取 —— 取在之后就只剩空表 (这正是之前
        # "保留 AI 复核结果: 0 行" 的原因)。
        ai_cache = {}
        # v323 P0-2: ai_verdict 也要带回来 —— 本作业是"先 DELETE 再 INSERT"的
        # 全量重算, 不带判定的话每轮都会把"AI 判过不成立"的行重新塞回主表,
        # rejected 表就白留痕了 (闸四)。
        ai_verdict_cache = {}
        try:
            for r in conn.execute(
                "SELECT move_id, news_event_id, ai_called, ai_explanation, ai_confidence, "
                "ai_error, ai_verdict "
                "FROM event_move_attribution WHERE COALESCE(ai_called,0)=1"
            ).fetchall():
                ai_cache[(r[0], r[1])] = (r[2], r[3], r[4], r[5])
                if r[6]:
                    ai_verdict_cache[(r[0], r[1])] = r[6]
        except sqlite3.OperationalError:
            ai_cache = {}
        # 待复核区里**已经复核过**的判定也带回来 (否则大异动每轮都要重跑 LLM)
        try:
            for r in conn.execute(
                "SELECT move_id, news_event_id, ai_verdict "
                "FROM event_move_attribution_pending "
                "WHERE COALESCE(ai_called,0)=1 AND ai_verdict IS NOT NULL"
            ).fetchall():
                ai_verdict_cache[(r[0], r[1])] = r[2]
        except sqlite3.OperationalError:
            pass
        # AI 判「不成立」的留痕 -> 这些 key 之后不再进主表/待复核区
        try:
            for r in conn.execute(
                "SELECT move_id, news_event_id FROM event_move_attribution_rejected"
            ).fetchall():
                ai_verdict_cache[(r[0], r[1])] = "reject"
        except sqlite3.OperationalError:
            pass
        # v321.5: dry-run 只算不写 —— 改口径时必须能先验证"会砍掉多少、留下什么",
        # 而不是直接 DELETE 再 INSERT 把 700+ 行存量归因换掉。
        #
        # v323 P0-3 (锁行为修复 v2): **这里不再开写事务**。
        # 旧实现是 "BEGIN IMMEDIATE → DELETE → 计算(几分钟) → INSERT → COMMIT",
        # 也就是**整个匹配计算期间都占着全库写锁**。本作业每 5 分钟跑一次,
        # 而计算本身要几十秒到几分钟 → 写锁长期被占 → engine / ai_worker /
        # attribution_ai_verify / signal-weight 全都撞 "database is locked"。
        # 实测 err.log 里 308 次锁失败, 且 ai_call_stats 每次都写不进去
        # (LLM 调用成功但统计丢了)。
        # 新做法: **先算后写** —— 上面所有 SELECT 都在只读连接上跑完,
        # 写锁只在最后 DELETE + INSERT 那几秒里持有 (见文件末尾 _persist)。
        cur_del = None
        cur_purge = None
        if args.dry_run:
            print("[DRY-RUN] 只统计不落库 (不 DELETE / 不 INSERT)")
        else:
            # v323 P0-3: 结束读阶段的事务 (isolation_level=None 下必须显式
            # ROLLBACK), 否则下面的 BEGIN IMMEDIATE 会报
            # "cannot start a transaction within a transaction"。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        asset_group_map = {a: g for g, members in CROSS_ASSET_GROUPS.items() for a in members}
        # v321.5: macro 系统性门槛的数据基础 —— 15 分钟桶 → 该桶内异动的品种集合。
        # macro 新闻没有品种标签, 只有"全市场一起动"时它才有资格解释某个品种。
        _move_bucket_assets: dict = {}
        for _m in moves:
            _b = int((_m[2] or 0) // MACRO_BUCKET_SEC)
            _move_bucket_assets.setdefault(_b, set()).add((_m[1] or "").upper())
        inserts = []
        # v323 P0-2 闸一: 落选候选 (展示用, 不参与统计)
        candidate_rows = []
        match_stats = {"asset": 0, "semantic": 0, "cross": 0, "macro": 0,
                       "spillover": 0, "macro_fallback": 0}
        # v309 P0-2: 加载因果技术信号 (uses_future=0), 参与打分微调
        tech_by_asset = _load_tech_signals(conn, cutoff)
        tech_applied = 0
        if tech_by_asset:
            print(f"[INFO] Loaded tech signals for {len(tech_by_asset)} assets "
                  f"(causal only, uses_future=0)")
        # v490 P1-2: 加载资金面快照（OI/多空比/费率/量能），参与归因判据
        flow_by_asset = _load_flow_snapshots(conn, cutoff)
        flow_applied = 0

        for m in moves:
            m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
            m_group = asset_group_map.get(m_asset)
            # v309 P0-2: 本波异动的技术面确认项 (整波共用一个调整量)
            _tech_adj, _tech_note = _tech_confirmation(
                tech_by_asset, m_asset or "", int(m_ts or 0), float(m_pct or 0.0))
            if _tech_note:
                tech_applied += 1
            # v490 P1-2: 本波异动的资金面确认项（整波共用，无未来函数）
            _flow_adj, _flow_note = _flow_confirmation(
                flow_by_asset, m_asset or "", int(m_ts or 0), float(m_pct or 0.0))
            if _flow_note:
                flow_applied += 1
            # v321.5: 窗口按形态自适应 (快形态 µ±3h / 慢形态 ±3d)
            win_sec = _window_for_kind(m_kind, m_dur)
            # v325: 闸二上限也必须按形态 —— 否则慢形态的候选会被 6h 硬闸全部丢掉
            lag_limit = _lag_max_for_kind(m_kind, m_dur)
            _is_slow = str(m_kind or "").lower().startswith("slow")
            scored = []
            # v342: 慢形态的 cross 候选单独缓冲 —— 只进 candidates 供复核, 绝不进主表
            slow_cross_local = []
            for n in news:
                n_id, n_src, n_headline, n_body, n_assets, n_asset, n_score, n_ts, n_action = n
                # v342: 标题里的去重指纹先剥掉，否则整条新闻会被当成"无标题"丢掉
                n_headline = _strip_hash_prefix(n_headline)
                # v342: 按来源可信度缩放证据分 —— 实测各源均分差 6 倍
                #   （eastmoney 0.54 / techflow 0.35 / finnhub 0.30 / WEB:Blogs 0.086 /
                #    jin10_calendar 0.004）。原来一视同仁，噪音源靠"篇数多"挤掉真证据；
                #   日历类（值日/节假日）与博客聚合与"事件驱动"无关，直接不参与归因。
                _trust = _source_trust(n_src)
                if _trust <= 0:
                    continue
                n_score = float(n_score or 0.0) * _trust
                # v291+ 双向归因:
                #   news before move (lag > 0)  → "事前归因" (新闻是触发因素)
                #   news after move  (lag < 0)  → "事后归因" (新闻是对已发生事件的报道/解释)
                # 两种都接受, 但事前归因 recency 系数略高
                lag = m_ts - n_ts
                abs_lag = abs(lag)
                if abs_lag > win_sec:
                    continue
                is_retrospective = lag < 0
                match_type = None
                n_asset_set = build_news_asset_set(n_assets, n_asset)
                n_asset_list = sorted(n_asset_set)
                # v310: 单数 `asset` 列才是权威品种，复数 `assets` 只是历史列 ——
                # v332: 两列都在 build_news_asset_set 里做代码归一 (000660 → SKHYNIX),
                # 归一后仍走原来的 "asset 直配" 通道, 不新增启发式。
                if asset_direct_match(m_asset, n_asset_set):
                    match_type = "asset"
                elif headline_contains_asset(n_headline or "", m_asset):
                    # v321.5: 只认标题。正文里顺带提到的品种名不是归因依据 ——
                    # 实测走正文的 semantic 归因 119 条被 AI 零确认。
                    match_type = "semantic"
                elif m_group and n_asset_list:
                    n_in_same_group = [a for a in n_asset_list if a in asset_group_map and asset_group_map[a] == m_group]
                    if n_in_same_group:
                        n_act = (n_action or "").upper()
                        move_dir = 1 if (m_pct or 0) > 0 else -1
                        news_dir = 1 if n_act in ("BUY",) else (-1 if n_act in ("SELL",) else 0)
                        if news_dir == 0 or news_dir == move_dir:
                            match_type = "cross"
                if match_type is None:
                    if not n_asset_list and (n_score or 0) >= MACRO_NEWS_SCORE:
                        # v321.5: 系统性门槛。没有品种标签的宏观新闻, 只有在
                        # "同一 15 分钟桶里 ≥3 个品种同时异动"时才算够格解释其中单个品种,
                        # 否则它解释的是整个市场, 不是这个标的 —— 旧口径零门槛导致
                        # 311 条 macro 归因 0 条被 AI 确认。
                        _bucket = int((m_ts or 0) // MACRO_BUCKET_SEC)
                        if len(_move_bucket_assets.get(_bucket, ())) >= MACRO_SYSTEMIC_MIN_ASSETS:
                            match_type = "macro"
                if match_type is None:
                    continue
                # v337: 行情播报类标题 = 价格的结果, 不是原因 (循环论证)。
                # 例:「BTC 跌破 75,000 美元，24 小时跌幅 1.08%」解释 BTC −2.61% 的下跌。
                # `_is_price_echo` 从 v325 就写好但**从未被调用**, 导致存量 39.9% 归因
                # 是自指标题。这里补上闸门。
                if _is_price_echo(n_headline):
                    match_stats["price_echo_skipped"] = \
                        match_stats.get("price_echo_skipped", 0) + 1
                    continue
                # v325: 慢形态只认"直接命中本标的"(asset / semantic)。
                # 依据（修复后试算抽查）：慢涨/慢跌一开放 72h 窗口, `cross`
                # (同组同向) 立刻把无关新闻挂上来 —— 实测 BNB −5.07% 被挂到
                # 「tBTC 跨链桥恢复」、XRP −4.44% 被挂到「Machi 卖出无聊猿」。
                # 慢形态本身持续几天, 同组同向的巧合概率极高, 假因果比缺归因更伤。
                if _slow_weak_blocked(match_type, _is_slow):
                    # v342: 原来这里直接 continue，慢形态的"同板块同向"证据被整个丢掉，
                    # 结果是 BTC/ETH 这类没有直接命中新闻的慢跌永远 0 归因。
                    # 现在改成落 candidates 表待复核（不进主表、不进任何统计口径），
                    # 页面上标 slow_cross_review，人/AI 可以认领确认。
                    # 待复核候选必须先过质量闸：标题被哈希脱敏（[hash:...]）的、
                    # 时间差超过 24h 的直接丢，否则复核队列会被垃圾候选淹没
                    # （实测：BTC 慢跌 rank1 是 "[hash:...] NEW: Meta employee"）。
                    _hl_txt = (n_headline or "").strip()
                    if not _hl_txt or _hl_txt.startswith("[hash:") or "[hash:" in _hl_txt[:20]:
                        match_stats["slow_cross_hash_skipped"] = \
                            match_stats.get("slow_cross_hash_skipped", 0) + 1
                        continue
                    if abs(lag) > 86400:
                        match_stats["slow_cross_far_skipped"] = \
                            match_stats.get("slow_cross_far_skipped", 0) + 1
                        continue
                    # 新闻自身得分太低的（≈0 分的行业快讯）不进复核队列：
                    # 实测这类会把「先声药业进医保」「胡塞释放人员」挂到 ETH 慢跌上。
                    if (n_score or 0) < 0.5:
                        match_stats["slow_cross_lowscore_skipped"] = \
                            match_stats.get("slow_cross_lowscore_skipped", 0) + 1
                        continue
                    _sc = round(min(0.60,
                                    0.25 + (n_score or 0) * 0.2
                                    + min(0.3, abs(float(m_pct or 0)) / 20.0) * 0.5), 3)
                    slow_cross_local.append(
                        (_sc, n_id, n_src, lag, match_type, _hl_txt))
                    match_stats["slow_cross_candidates"] = \
                        match_stats.get("slow_cross_candidates", 0) + 1
                    continue
                # v325: 慢形态方向矛盾（标题说跌却解释上涨）同样丢弃
                if _direction_blocked(n_headline, m_pct):
                    match_stats["direction_skipped"] = \
                        match_stats.get("direction_skipped", 0) + 1
                    continue
                # v321.5: 弱匹配 (semantic / macro) 默认不进归因表。
                # 依据: 重算后 AI 复核 84 条弱匹配, confirm 0 / 部分 4 / 不成立 80。
                # 保留开关是为了以后能"只作候选展示"而不污染"真因果"统计。
                if match_type in ("semantic", "macro") and not args.allow_weak_attribution:
                    match_stats.setdefault("weak_skipped", 0)
                    match_stats["weak_skipped"] += 1
                    continue
                base = 0.35
                if match_type == "asset":
                    base += 0.15
                elif match_type == "semantic":
                    base += 0.10
                elif match_type == "macro":
                    base += 0.05
                price_react = min(0.3, abs(m_pct) / 20.0)
                news_impact = (n_score or 0) * 0.3
                score = base + news_impact + price_react
                # v309 P0-2: 技术面确认项 (整波统一, 有界微调)
                score += _tech_adj
                # v490 P1-2: 资金面确认项（OI/多空比/费率/量能）并入打分
                score += _flow_adj
                recency = max(0.5, 1.0 - abs_lag / win_sec)
                if is_retrospective:
                    recency *= 0.85
                score = round(max(0.01, min(0.99, score * recency)), 3)
                if match_type == "macro":
                    # v321.5: macro 是"弱关联", 不许拿高分冒充"实证归因"
                    score = round(min(score, MACRO_SCORE_CAP), 3)
                # v323 P0-2: 带上 headline —— 闸三要求"空标题不许入表",
                # 而标题只在候选里才有 (主表不存标题)。
                scored.append((score, n_id, n_src, lag, match_type, is_retrospective, n_headline))
                match_stats[match_type] += 1
            # v310 P1: **事前归因优先** (见 _rank_candidates)。
            # v323 P0-2 闸二: 时间纪律硬过滤必须排在排序**之前** ——
            # 负延迟(事件晚于异动)是把"结果"当"原因", 直接丢掉;
            # 早于异动超过 6h 的事件在时间上也不成立。
            scored, gate_stats = _apply_lag_gates(scored, lag_limit)
            for _gk, _gv in gate_stats.items():
                match_stats[_gk] = match_stats.get(_gk, 0) + _gv
            scored = _rank_candidates(scored)
            for score, n_id, n_src, lag, match_type, is_retrospective, n_headline in scored[:TOP_N_PER_MOVE]:
                # v323 P0-2 闸三: 空标题不许入表 —— 没有标题的事件解释不了任何异动,
                # 只会污染"哪条新闻造成了这一波"的统计 (实测最大异动 SKHYNIX
                # -16.25% 的归因 headline 为空)。
                if not (n_headline or "").strip():
                    match_stats["blank_headline_skipped"] = match_stats.get("blank_headline_skipped", 0) + 1
                    continue
                # v325: 最低证据门槛 —— 分数 < LOW_SCORE_MIN 的归因不进主表。
                # 实测残留: BNB +3.01% ←「美伊谈判」(score 0.094)、
                # ADA +3.74% ←「Linux 内核漏洞」(score 0.086)。
                # 这类"勉强归因"只是在凑覆盖率; 大异动例外 —— 它们进待复核区由 AI 判。
                # 不进主表 ≠ 不对用户可见: 低分项落到 candidates 表, 页面上标为弱候选。
                if score < LOW_SCORE_MIN and abs(float(m_pct or 0.0)) < BIG_MOVE_ABS_PCT:
                    match_stats["low_score_skipped"] = match_stats.get("low_score_skipped", 0) + 1
                    candidate_rows.append((
                        m_id, n_id, m_asset, lag, score,
                        f"match={match_type}; lag={lag}s; low_score<{LOW_SCORE_MIN}",
                        n_id, n_src, match_type.upper(), None,
                        m_ts, m_pct, m_kind, m_dur, 1,
                    ))
                    continue
                reasons = f"match={match_type}; lag={lag}s; {'retro' if is_retrospective else 'predictive'}"
                if _tech_note:
                    reasons += f"; {_tech_note}"
                if _flow_note:
                    reasons += f"; {_flow_note}"
                inserts.append((
                    m_id, n_id, m_asset, lag, score, reasons,
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    n_id, n_src, match_type.upper(),
                    None, n_ts,
                ))
            # v323 P0-2 闸一: 落选候选另存 candidates 表 (展示用, 不参与统计)
            for _rank, (_sc, _nid, _nsrc, _lag, _mt, _retro, _hl) in enumerate(
                    scored[TOP_N_PER_MOVE:TOP_N_PER_MOVE + CANDIDATES_PER_MOVE],
                    start=TOP_N_PER_MOVE + 1):
                if not (_hl or "").strip():
                    continue
                candidate_rows.append((
                    m_id, _nid, m_asset, _lag, _sc,
                    f"match={_mt}; lag={_lag}s; rank={_rank}",
                    _nid, _nsrc, _mt.upper(), None,
                    m_ts, m_pct, m_kind, m_dur, _rank,
                ))
            # v342: 慢形态 cross 候选落表（每个异动最多 3 条，按分数取头部）
            for _rank, (_sc, _nid, _nsrc, _lag, _mt, _hl) in enumerate(
                    sorted(slow_cross_local, reverse=True)[:CANDIDATES_PER_MOVE], start=1):
                if not (_hl or "").strip():
                    continue
                candidate_rows.append((
                    m_id, _nid, m_asset, _lag, _sc,
                    f"match={_mt}; lag={_lag}s; slow_cross_review; rank={_rank}",
                    _nid, _nsrc, _mt.upper(), None,
                    m_ts, m_pct, m_kind, m_dur, _rank,
                ))
        if not inserts:
            print("[INFO] No attribution rows generated")
            return 0

        # === Tier 2: 同组资产 24h spillover (Q4 覆盖率提升) ===
        if args.enable_spillover:
            spillover_window = args.spillover_window
            # 按 (asset_group, day_bucket) 分组, 找 group 内有大幅 move 的同伴
            from collections import defaultdict
            group_moves: dict = defaultdict(list)  # group -> [(move, day_bucket, magnitude)]
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                m_group = asset_group_map.get(m_asset)
                if not m_group:
                    continue
                if abs(m_pct or 0) < 3.0:  # 仅显著 move 触发 spillover
                    continue
                day_bucket = int(m_ts) // spillover_window
                group_moves[(m_group, day_bucket)].append(m)
            # 对每个 move, 找同组同窗口的"显著同伴", 生成 spillover 归因
            t1_count = sum(1 for ins in inserts if ins[15] is not None)
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                m_group = asset_group_map.get(m_asset)
                if not m_group:
                    continue
                # 是否已有 Tier 1 归因?
                already = any(ins[0] == m_id for ins in inserts)
                if already:
                    continue  # Tier 1 已覆盖, 不再叠加
                day_bucket = int(m_ts) // spillover_window
                peers = [p for p in group_moves.get((m_group, day_bucket), [])
                         if p[0] != m_id]
                if not peers:
                    continue
                # 用最强 peer 当 spillover 来源 (algo_score = 弱相关)
                peer = max(peers, key=lambda p: abs(p[3] or 0))
                p_id, p_asset, p_ts, p_pct, p_kind, p_dur, p_algo = peer
                spillover_score = round(min(0.99, 0.30 + abs(p_pct or 0) / 30.0), 3)
                lag = m_ts - p_ts
                inserts.append((
                    m_id, None, m_asset, lag, spillover_score,
                    f"match=spillover; group={m_group}; peer={p_asset}; lag={lag}s",
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    None, "spillover_calc", "SPILLOVER",
                    None, p_ts,
                ))
                match_stats["spillover"] += 1

        # === Tier 3: macro_fallback (无任何归因且 |move|>threshold 时兜底) ===
        if args.enable_macro_fallback:
            th = args.macro_threshold
            covered_move_ids = {ins[0] for ins in inserts}
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                if m_id in covered_move_ids:
                    continue  # 已 Tier 1/2 覆盖
                if abs(m_pct or 0) < th:
                    continue
                # 低置信度 macro 兜底, 标 algo_score = 0.30 + magnitude_bonus
                macro_score = round(min(0.55, 0.30 + abs(m_pct or 0) / 50.0), 3)
                inserts.append((
                    m_id, None, m_asset, 0, macro_score,
                    f"match=macro_fallback; |move_pct|={abs(m_pct):.1f}>={th}; no news matched",
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    None, "macro_fallback", "MACRO",
                    None, m_ts,
                ))
                match_stats["macro_fallback"] += 1

        # ── G01: 把快照里的 AI 复核结果带回新行 ────────────────────────────
        if ai_cache:
            restored = 0
            new_inserts = []
            for ins in inserts:
                key = (ins[0], ins[1])
                cached = ai_cache.get(key)
                if cached:
                    ins = (ins[0], ins[1], ins[2], ins[3], ins[4], ins[5],
                           cached[0], cached[1], cached[2], cached[3]) + tuple(ins[10:])
                    restored += 1
                new_inserts.append(ins)
            inserts = new_inserts
            print(f"[INFO] 保留 AI 复核结果: {restored} 行")

        # ── v323 P0-2 闸三: 大异动强制复核 ───────────────────────────────
        # |move_pct|>=5% 的异动, 在拿到 AI 判定 (confirm/partial) 之前
        # **不许进主表** —— 先落 pending 区, 复核通过后再迁入。
        # 已经有判定的行按判定分流: confirm/partial -> 主表; reject -> 跳过。
        _main_rows = []
        _pending_rows = []
        _skipped_rejected = 0
        _degraded_direct = 0
        _ai_online = _ai_verify_available(conn)
        for ins in inserts:
            _verdict = ai_verdict_cache.get((ins[0], ins[1]))
            if _verdict == "reject":
                _skipped_rejected += 1
                continue
            _row = tuple(ins) + (_verdict,)
            _bucket = route_insert(ins, _verdict, _ai_online)
            if _bucket == "skip":
                _skipped_rejected += 1
            elif _bucket == "pending":
                _pending_rows.append(_row)
            else:
                _main_rows.append(_row)
                if (_verdict is None
                        and abs(float(ins[11] or 0.0)) >= BIG_MOVE_ABS_PCT):
                    _degraded_direct += 1
        if _skipped_rejected:
            print(f"[INFO] v323 跳过 AI 已判不成立的行: {_skipped_rejected}")
        if _degraded_direct:
            print(f"[INFO] v334 AI 复核不在线 → asset 直配的大异动降级放行: "
                  f"{_degraded_direct} 行 (ai_called=0)")

        if args.dry_run:
            total_moves = len(moves)
            covered = len({ins[0] for ins in inserts})
            cov_pct = round(100 * covered / max(total_moves, 1), 1)
            print(f"[DRY-RUN] would insert {len(inserts)} rows "
                  f"(coverage {covered}/{total_moves} = {cov_pct}%)")
            print(f"[DRY-RUN] Match types: {match_stats}")
            print(f"[DRY-RUN] v323 分流: 主表 {len(_main_rows)} / 待复核区 {len(_pending_rows)} "
                  f"/ 跳过(AI判不成立) {_skipped_rejected} / 落选候选 {len(candidate_rows)}")
            by_type: dict = {}
            for ins in inserts:
                by_type[ins[16]] = by_type.get(ins[16], 0) + 1
            print(f"[DRY-RUN] By ev_action: {by_type}")
            # v325: 按形态看覆盖 —— 慢涨/慢跌长期 0 覆盖是本次修复的验收口径
            _covered_ids = {ins[0] for ins in inserts}
            _kind_total: dict = {}
            _kind_cov: dict = {}
            for m in moves:
                _k = m[4] or "unknown"
                _kind_total[_k] = _kind_total.get(_k, 0) + 1
                if m[0] in _covered_ids:
                    _kind_cov[_k] = _kind_cov.get(_k, 0) + 1
            _kind_pct = {k: f"{_kind_cov.get(k,0)}/{_kind_total[k]}"
                         f"({round(100*_kind_cov.get(k,0)/_kind_total[k],1)}%)"
                         for k in sorted(_kind_total)}
            print(f"[DRY-RUN] By move_kind coverage: {_kind_pct}")
            # v325: 慢形态归因抽查 —— 慢涨/慢跌的距离衰减是否合理, 要看真实 lag 与分数
            _head_by_id = {n[0]: n[2] for n in news}
            _slow_rows = [ins for ins in inserts
                          if str(ins[12] or "").lower().startswith("slow")]
            for ins in sorted(_slow_rows, key=lambda x: -float(x[4] or 0))[:8]:
                print(f"[DRY-RUN][slow] {ins[2]:<8} lag={round(ins[3]/3600,1):>6}h "
                      f"score={ins[4]} move={ins[11]:+.2f}% "
                      f"| {str(_head_by_id.get(ins[1]) or '')[:60]}")
            return 0

        # ── v323 P0-3: 落库阶段 —— 写锁**只在这里**持有 ─────────────────────
        # 顺序: BEGIN IMMEDIATE → DELETE(窗口内旧行 + 越界污染行) →
        #       INSERT 主表 / 待复核区 / 落选候选 → COMMIT。
        # 整段只有几百毫秒, 不会再把全库写锁按几分钟。
        # busy_timeout 对 BEGIN IMMEDIATE **有效** (它会等待重试),
        # 所以这里的锁冲突会自动排队而不是直接失败。
        _main_rows = _tag_evidence(conn, _main_rows, 14, 5)
        _pending_rows = _tag_evidence(conn, _pending_rows, 14, 5)
        candidate_rows = _tag_evidence(conn, candidate_rows, 6, 5)
        _persist_attempts = 0
        while True:
            _persist_attempts += 1
            try:
                conn.execute("BEGIN IMMEDIATE")
                # v340: 重建时**保留行 id**。旧实现 DELETE+INSERT 会让窗口内每一行
                # 换一个新 id —— 外部一旦按 id 记账（AI 复核队列、前端书签、审计留痕）
                # 就全部失效：队列按 src_id 认领，重建一次旧记录就变"gone"、重新入队、
                # attempts 归零，同一条永远跑不到 12 次上限。id 稳定后这些都不再发生。
                _id_map_main: dict[tuple, int] = {}
                _id_map_pend: dict[tuple, int] = {}
                try:
                    for r in conn.execute(
                        "SELECT id, move_id, news_event_id FROM event_move_attribution "
                        "WHERE move_ts >= ?", (cutoff,)).fetchall():
                        _id_map_main[(r[1], r[2])] = int(r[0])
                    for r in conn.execute(
                        "SELECT id, move_id, news_event_id FROM event_move_attribution_pending "
                        "WHERE move_ts >= ?", (cutoff,)).fetchall():
                        _id_map_pend[(r[1], r[2])] = int(r[0])
                except sqlite3.OperationalError:
                    pass
                cur_del = conn.execute(
                    "DELETE FROM event_move_attribution WHERE move_ts >= ?", (cutoff,))
                print(f"[INFO] Cleared {cur_del.rowcount} prior rows")
                # v308 P1-fix3: 窗口外的存量污染行也要清掉。
                # 上面的 DELETE 只覆盖 move_ts >= cutoff (本次重算窗口), 而更早的
                # 污染行 (比如 backfill 修好之前算的 +9867%) 不会被碰到 —— 它们照样
                # 会被 /api/v291/event-move-attribution 读出来展示。
                # |move_pct|>90% 的行按定义就不是真实异动, 跨窗口删掉是安全的。
                cur_purge = conn.execute(
                    "DELETE FROM event_move_attribution WHERE ABS(COALESCE(move_pct,0)) > ?",
                    (MAX_PLAUSIBLE_MOVE_PCT,),
                )
                if cur_purge.rowcount:
                    print(f"[INFO] Purged {cur_purge.rowcount} out-of-window implausible rows")
                # v323 P0-2: 待复核区 / 落选候选 同样按窗口清。
                # 已复核过的判定保存在 ai_verdict_cache 里, 下一轮会直接进主表,
                # 不会重复跑 LLM。
                conn.execute("DELETE FROM event_move_attribution_pending WHERE move_ts >= ?",
                             (cutoff,))
                conn.execute("DELETE FROM event_move_attribution_candidates WHERE move_ts >= ?",
                             (cutoff,))
                # 主表 (19 列 + ai_verdict)
                if _main_rows:
                    # 新行显式分配 id（不能留 NULL 自增 —— 同一批里自增出来的号
                    # 可能和旧 id 撞车）。旧行复用旧 id，新行接在最大值之后。
                    _next_id = max(
                        [int(conn.execute(
                            "SELECT COALESCE(MAX(id),0) FROM event_move_attribution"
                        ).fetchone()[0] or 0)]
                        + list(_id_map_main.values()) + [0]) + 1
                    _rows_with_id = []
                    for r in _main_rows:
                        _rid = _id_map_main.get((r[0], r[1]))
                        if _rid is None:
                            _rid = _next_id
                            _next_id += 1
                        else:
                            _next_id = max(_next_id, _rid + 1)
                        _rows_with_id.append((_rid,) + tuple(r))
                    conn.executemany(
                        "INSERT INTO event_move_attribution "
                        "(id,move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
                        "ai_called,ai_explanation,ai_confidence,ai_error,"
                        "move_ts,move_pct,move_kind,duration_min,"
                        "ev_news_id,ev_source,ev_action,ev_score,ev_ts,ai_verdict) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _rows_with_id,
                    )
                # 待复核区 (19 列 + ai_verdict) —— 大异动在这里等 AI 判定
                if _pending_rows:
                    _next_pid = max(
                        [int(conn.execute(
                            "SELECT COALESCE(MAX(id),0) FROM event_move_attribution_pending"
                        ).fetchone()[0] or 0)]
                        + list(_id_map_pend.values()) + [0]) + 1
                    _prows_with_id = []
                    for r in _pending_rows:
                        _pid = _id_map_pend.get((r[0], r[1]))
                        if _pid is None:
                            _pid = _next_pid
                            _next_pid += 1
                        else:
                            _next_pid = max(_next_pid, _pid + 1)
                        _prows_with_id.append((_pid,) + tuple(r))
                    conn.executemany(
                        "INSERT INTO event_move_attribution_pending "
                        "(id,move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
                        "ai_called,ai_explanation,ai_confidence,ai_error,"
                        "move_ts,move_pct,move_kind,duration_min,"
                        "ev_news_id,ev_source,ev_action,ev_score,ev_ts,ai_verdict) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _prows_with_id,
                    )
                # 落选候选 (展示用, 不参与统计)
                if candidate_rows:
                    _now_ts = int(datetime.now(tz=timezone.utc).timestamp())
                    conn.executemany(
                        "INSERT INTO event_move_attribution_candidates "
                        "(move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
                        "ev_news_id,ev_source,ev_action,ev_ts,"
                        "move_ts,move_pct,move_kind,duration_min,cand_rank,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9],
                          c[10], c[11], c[12], c[13], c[14], _now_ts) for c in candidate_rows],
                    )
                conn.commit()
                # v338: 归因是 DELETE+INSERT **整表重建** —— 规则自检必须紧跟其后。
                # 手动打标下一轮就会被清空 (验收 #12 实测从 59.4% 掉到 34.6%)。
                # 失败不阻断归因: rule_verdict 缺失时验收脚本会退回旧口径。
                try:
                    import time as _t
                    from attribution_rule_verify import verify_all as _rv
                    for _rv_attempt in range(3):
                        try:
                            _rv(conn)
                            conn.commit()
                            break
                        except Exception as _e_inner:
                            if _rv_attempt == 2:
                                raise
                            print(f"[WARN] rule_verify retry {_rv_attempt + 1}: "
                                  f"{type(_e_inner).__name__}: {_e_inner}", flush=True)
                            _t.sleep(2)
                except Exception as _rv_exc:
                    print(f"[WARN] rule_verify skipped: {type(_rv_exc).__name__}: {_rv_exc}",
                          flush=True)
                break
            except sqlite3.OperationalError as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if _persist_attempts >= 8:
                    # 注意: 拿不到 BEGIN IMMEDIATE 就意味着 **DELETE 也没执行**,
                    # 表里的旧数据完好 —— 这一轮只是"没更新", 不是"数据丢了"。
                    # (旧实现是 BEGIN 成功 → DELETE 成功 → INSERT 失败 = 数据真丢)
                    print(f"[WARN] 落库放弃(写锁被占用 {8 * 15}s): {exc} —— "
                          f"本轮不更新, 旧数据保留, 等下一轮 cron", flush=True)
                    return 0
                wait = min(5 * _persist_attempts, 30)
                print(f"[WARN] 写锁占用, {wait}s 后重试 (第 {_persist_attempts} 次)", flush=True)
                time.sleep(wait)
        # v308 P1-fix3: 计数也用同一套过滤 —— 表里可能还有更早窗口算出来的
        # 存量污染行, 直接 COUNT(*) 会让日志里的"表总量"对不上实际干净行数。
        after = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) <= ?",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchone()[0]
        by_asset = conn.execute(
            "SELECT asset, COUNT(*) FROM event_move_attribution "
            "WHERE ABS(COALESCE(move_pct,0)) <= ? GROUP BY asset ORDER BY asset",
            (MAX_PLAUSIBLE_MOVE_PCT,),
        ).fetchall()
        print(f"[OK] Inserted {len(_main_rows)} main + {len(_pending_rows)} pending "
              f"+ {len(candidate_rows)} candidates (table total: {after})")
        for a, c in by_asset:
            print(f"  {a}: {c}")
        print(f"[INFO] Match types: {match_stats}")
        if tech_applied:
            print(f"[INFO] v309 P0-2: tech confirmation applied to {tech_applied}/{len(moves)} moves")
        if flow_applied:
            print(f"[INFO] v490 P1-2: flow confirmation applied to {flow_applied}/{len(moves)} moves")
        # Q4 覆盖率计算
        total_moves = len(moves)
        covered = len({ins[0] for ins in inserts})
        cov_pct = round(100 * covered / max(total_moves, 1), 1)
        print(f"[Q4] Coverage: {covered}/{total_moves} moves = {cov_pct}% (target >=30%)")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
