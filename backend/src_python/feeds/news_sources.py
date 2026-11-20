"""新闻源开关与每日入库配额。

默认开启全部新闻源，每日目标 300 条（上海时区）。
关闭总开关或单个源后，对应入库会被拦截。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

import config
import db

SOURCE_KEYS = ("financialjuice", "tree_news", "techflow", "eastmoney", "blockbeats", "jin10", "fred", "finnhub", "feishu_jin10", "reuters", "bls", "yahoo_finance", "marketwatch", "fxstreet", "cointelegraph", "oilprice")
DEFAULT_DAILY_TARGET = 1200

# 每日配额豁免源：金十快讯是套利核心实时驱动源（与 FRED/金十日历宏观数据同级），
# 不应被每日新闻配额卡断推送——该配额本意是限制长尾美股/RSS 新闻的 AI 分析
# 总量，而非拦截金十/宏观这类必达的实时信号。豁免配额但仍受总开关 enabled 控制。
# 注意：豁免源必须同时从“当日已用量”计数中剔除，否则 400+ 条/日的金十会把
# 配额提前吃满，导致 eastmoney/finnhub/techflow/feishu 等源在当天余下时间被静默卡停
# （2026-11-13 实测：00:24 起 15 个源同时断流，原因是 281 条金十 + 306 条其它已超 300）。
_QUOTA_EXEMPT_SOURCES = frozenset({"jin10", "sina_flash", "wscn"})

# 单源日上限（公平预算）。全局 1200 条/日的配额一旦被一个高产源吃掉，其余源
# 就会整天断流——2026-11-13 实测东方财富 30 分钟 74 条（折合 3000+/日），
# 它会独占配额。这里给每个源设日上限，超出只停该源，不影响别人。
# v447（2026-11-18）：单源日上限 ×4。原值 1200 全局配额 + 单源 150–260 的配置
# 实测在 19:00 就把 15 个源全掐死（`[QUOTA] 1200/1200 用尽`），用户看到的现象
# 就是"新闻突然变少"。配额应按"能否消化"来定，不该小于正常的日间新闻量。
_SOURCE_DAILY_CAP = {
    "eastmoney": 1200, "techflow": 1000, "feishu_jin10": 1000, "finnhub": 1000,
    "tree_news": 900, "reuters": 900, "financialjuice": 900,
    "blockbeats": 700, "cointelegraph": 700,
    "marketwatch": 600, "fxstreet": 600, "oilprice": 600,
    "yahoo_finance": 700, "fred": 200, "bls": 60,
}
_DEFAULT_SOURCE_CAP = 700

_DEFAULT_SOURCES = {key: True for key in SOURCE_KEYS}
_QUOTA_WARNED_FOR: Optional[str] = None
_SOURCE_CAP_WARNED: Dict[str, str] = {}


def _today() -> str:
    from datetime import datetime
    return datetime.now(config.TZ_SHANGHAI).strftime("%Y-%m-%d")


def _normalize_source(source: str) -> Optional[str]:
    value = (source or "").lower()
    if "feishu" in value or "飞书" in (source or ""):
        return "feishu_jin10"
    if "jin10" in value or "金十" in (source or ""):
        return "jin10"
    if "techflow" in value or "深潮" in (source or ""):
        return "techflow"
    if "eastmoney" in value or "东方财富" in (source or ""):
        return "eastmoney"
    if "blockbeats" in value or "律动" in (source or ""):
        return "blockbeats"
    if "tree" in value or "telegram" in value or value.startswith("web:"):
        return "tree_news"
    if "financialjuice" in value or value.startswith("ws:") or "fj" in value:
        return "financialjuice"
    if "fred" in value or "stlouisfed" in value:
        return "fred"
    if "bls" in value or "劳工统计" in (source or ""):
        return "bls"
    if "fxstreet" in value:
        return "fxstreet"
    if "cointelegraph" in value:
        return "cointelegraph"
    if "oilprice" in value:
        return "oilprice"
    if "marketwatch" in value:
        return "marketwatch"
    if "yahoo_finance" in value or ("yahoo" in value and "stock" not in value):
        return "yahoo_finance"
    if "finnhub" in value or "yahoo_stock" in value or "美股" in (source or ""):
        return "finnhub"
    if "reuters" in value or "路透" in (source or ""):
        return "reuters"
    return None


def get_settings(connection=None) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                "SELECT news_enabled, news_daily_target, news_sources FROM paper_trading_settings WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            # 旧库尚未补 news_* 列时按默认全开，避免 watcher 整轮停采。
            row = None
        enabled = True
        daily_target = DEFAULT_DAILY_TARGET
        sources = dict(_DEFAULT_SOURCES)
        if row is not None:
            if row[0] is not None:
                enabled = bool(row[0])
            if row[1]:
                daily_target = max(1, int(row[1]))
            try:
                stored = json.loads(row[2] or "{}")
                if isinstance(stored, dict):
                    for key in SOURCE_KEYS:
                        if key in stored:
                            sources[key] = bool(stored[key])
            except (TypeError, json.JSONDecodeError):
                pass
        today = _today()
        # 口径修正：
        #   1) 原始写法只认 timestamp 文本日期，而新浪/华尔街见闻等新采集器写的是
        #      数值 epoch（ts），那些行根本不进计数——等于绕开配额；这里两种格式都算。
        #   2) 豁免源必须剔除，否则高产的实时源会把配额吃满、静默卡停其余源。
        from datetime import datetime
        start_epoch = int(datetime.strptime(today, "%Y-%m-%d")
                          .replace(tzinfo=config.TZ_SHANGHAI).timestamp())
        exempt = tuple(_QUOTA_EXEMPT_SOURCES)
        ph = ",".join("?" for _ in exempt)
        sql = f"""SELECT COUNT(*) FROM raw_news
                  WHERE (
                        (typeof(ts)='integer' AND ts>=?)
                     OR (typeof(ts)!='integer' AND substr(timestamp,1,10)=?)
                  )
                  AND lower(coalesce(source,'')) NOT IN ({ph})"""
        today_count = int(conn.execute(sql, (start_epoch, today, *exempt)).fetchone()[0])
        remaining = max(0, daily_target - today_count)
        return {
            "enabled": enabled,
            "daily_target": daily_target,
            "today_count": today_count,
            "remaining": remaining,
            "sources": sources,
            "today": today,
        }
    finally:
        if own:
            conn.close()


def set_settings(
    *,
    enabled: Optional[bool] = None,
    daily_target: Optional[int] = None,
    sources: Optional[Dict[str, Any]] = None,
    connection=None,
) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        current = get_settings(conn)
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        next_target = current["daily_target"] if daily_target is None else max(1, int(daily_target))
        next_sources = dict(current["sources"])
        if isinstance(sources, dict):
            for key in SOURCE_KEYS:
                if key in sources:
                    next_sources[key] = bool(sources[key])
        conn.execute(
            """UPDATE paper_trading_settings
               SET news_enabled=?, news_daily_target=?, news_sources=?
               WHERE id=1""",
            (int(next_enabled), next_target, json.dumps(next_sources, ensure_ascii=False)),
        )
        if own:
            conn.commit()
        return get_settings(conn)
    finally:
        if own:
            conn.close()



_SOURCE_COUNT_CACHE: Dict[str, Any] = {"date": None, "mono": 0.0, "counts": {}}


def _today_source_counts(conn, today: str, start_epoch: int) -> Dict[str, int]:
    """当日各（归一化后的）源已入库条数，60 秒缓存，避免逐条新闻都做全表扫描。"""
    if (_SOURCE_COUNT_CACHE["date"] == today
            and time.monotonic() - _SOURCE_COUNT_CACHE["mono"] < 60):
        return _SOURCE_COUNT_CACHE["counts"]
    rows = conn.execute(
        """SELECT source, COUNT(*) FROM raw_news
           WHERE ((typeof(ts)='integer' AND ts>=?)
                OR (typeof(ts)!='integer' AND substr(timestamp,1,10)=?))
           GROUP BY source""",
        (start_epoch, today),
    ).fetchall()
    counts: Dict[str, int] = {}
    for src, n in rows:
        key = _normalize_source(src) or str(src or "").lower()
        counts[key] = counts.get(key, 0) + int(n)
    _SOURCE_COUNT_CACHE.update({"date": today, "mono": time.monotonic(), "counts": counts})
    return counts


def allow_ingest(source: str, connection=None) -> bool:
    """入库前检查：总开关与当日配额（单源开关不拦采集——推送照常入库）。

    单源开关（sources.*）只影响模拟盘交易层（见 source_enabled），
    关闭某源不影响该源新闻正常采集与展示。
    """
    own = connection is None
    conn = connection or db.get_connection()
    try:
        settings = get_settings(conn)
        if not settings["enabled"]:
            return False
        key = _normalize_source(source)
        if key in _QUOTA_EXEMPT_SOURCES:
            return True
        if settings["remaining"] <= 0:
            # 配额打满时必须留痕：此前是静默 return False，导致 15 个源整天断流
            # 却没有任何日志，只能靠事后发现“同一时刻集体停”。
            global _QUOTA_WARNED_FOR
            if _QUOTA_WARNED_FOR != settings["today"]:
                _QUOTA_WARNED_FOR = settings["today"]
                print(f"[QUOTA] {settings['today']} 新闻配额已用尽 "
                      f"({settings['today_count']}/{settings['daily_target']})，"
                      f"非豁免源暂停入库，直至次日重置", flush=True)
            return False

        # 单源公平预算：防止一个高产源独占全天配额，把其余源饿死。
        from datetime import datetime
        start_epoch = int(datetime.strptime(settings["today"], "%Y-%m-%d")
                          .replace(tzinfo=config.TZ_SHANGHAI).timestamp())
        cap = _SOURCE_DAILY_CAP.get(key, _DEFAULT_SOURCE_CAP)
        used = _today_source_counts(conn, settings["today"], start_epoch).get(key, 0)
        if used >= cap:
            if _SOURCE_CAP_WARNED.get(key) != settings["today"]:
                _SOURCE_CAP_WARNED[key] = settings["today"]
                print(f"[CAP] 源 {source} 已达当日上限 {used}/{cap}，该源今日暂停入库",
                      flush=True)
            return False
        return True
    finally:
        if own:
            conn.close()


def source_enabled(source: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    """交易层源过滤：该新闻源是否允许参与模拟盘交易。

    按复盘页单源开关判断；无法识别的源默认放行（与入库行为一致）。
    settings 可复用已取到的 get_settings() 结果，避免重复查库。
    """
    key = _normalize_source(source)
    if key is None:
        return True
    if settings is None:
        settings = get_settings()
    return bool(settings["sources"].get(key, True))
