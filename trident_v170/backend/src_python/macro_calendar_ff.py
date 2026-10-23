"""v170: ForexFactory financial calendar fetcher (multi-source macro calendar).

Data: https://nfs.faireconomy.media/ff_calendar_thisweek.xml (public, no key).
Schema: <event date time country title impact forecast previous actual />
Impact enum: Holiday / Low / Medium / High / Non-Economic.
"""
from __future__ import annotations

import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("macro_calendar_ff")

# v189-8: sync_to_db 每累计多少行提交一次, 把写锁窗口从"整个 events 列表"
# 收敛到最多 _COMMIT_EVERY 行, 避免与 engine/api 的高频写互相饿死的秒级持锁。
_COMMIT_EVERY = 25

_FF_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://nfs.faireconomy.media/ff_calendar_thisweekextended.xml",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",
)

_FF_IMPACT_MAP = {
    "high": "high", "High": "high", "HIGH": "high",
    "medium": "med", "Medium": "med", "MEDIUM": "med",
    "low": "low", "Low": "low", "LOW": "low",
    "holiday": "low", "Holiday": "low", "HOLIDAY": "low",
    "non-economic": "low", "Non-Economic": "low", "NON-ECONOMIC": "low",
}

# v215: FF impact → 1-5 颗星 (与金十 star 对齐)
# FF 三档 (high/med/low) 映射到星 4/3/2 (留 5 给金十独占 + 留 1 给冗余)
_FF_IMPACT_TO_STAR = {
    "high": 4,
    "med": 3,
    "low": 2,
}


def ff_impact_to_star(impact: str) -> int:
    """v215: FF impact 枚举 → 1-5 颗星。

    FF 只有 high/med/low 三档，映射到 4/3/2 颗星：
      · high → 4  (核心宏观事件)
      · med  → 3  (重要但非决定性)
      · low  → 2  (节假日/低影响)
    5 星和 1 星仅 jin10 独占（金十独有事件 cross-check 不到 FF 时显示），统一显示用 max(star)。
    """
    return _FF_IMPACT_TO_STAR.get(str(impact or "").lower(), 0)


# v283: 顶级宏观事件关键词(命中直接 5 星)
# 用户视角:FOMC 议息/纪要/声明/主席讲话 与 CPI/非农 同级 → 必须 5 星
# 不依赖后续 race-prone 二次升级
_HIGH_PRIORITY_MACRO_KEYWORDS = (
    "FOMC Meeting Minutes",       # 会议纪要
    "FOMC Press Conference",      # 发布会
    "FOMC Statement",             # 利率决议声明
    "FOMC Meeting",               # 利率决议会议
    "FOMC Member",                # FOMC 委员讲话
    "FOMC Chair",                 # FOMC 主席讲话
    "Fed Chair Speaks",           # Fed 主席讲话
    "Fed Chair Powell",           # 鲍威尔讲话
    "Fed Funds Rate",             # 联邦基金利率决议
    "Federal Reserve Rate",       # 联邦储备利率
    "Interest Rate Decision",     # 利率决议
    "Rate Decision",              # 利率决定
    "美联储",                     # 中文
    "议息会议",                   # 中文
)


def _is_high_priority_macro(title: str) -> bool:
    """v283: 命中顶级宏观关键词 → True(返回 True 的事件应升 5 星)。

    与 fomc_minutes_upgrade.py 中的关键词列表保持一致;
    区别是这里在源头就把 star 写到 5,避免被 ON CONFLICT 二次覆盖。
    """
    if not title:
        return False
    title_l = title.lower()
    return any(k.lower() in title_l for k in _HIGH_PRIORITY_MACRO_KEYWORDS)

_FF_HTTP_TIMEOUT = 8.0
_FF_MAX_RETRIES = 1


def _parse_ff_datetime(date, time_str):
    if not date:
        return 0
    t = (time_str or "12:00am").strip().lower()
    if not t:
        t = "12:00am"
    try:
        if "am" in t or "pm" in t:
            clock = t.replace("am", "").replace("pm", "").strip()
            meridiem = "am" if "am" in t else "pm"
            hh, mm = (clock.split(":") + ["0"])[:2]
            hh_i = int(hh)
            if meridiem == "pm" and hh_i < 12:
                hh_i += 12
            if meridiem == "am" and hh_i == 12:
                hh_i = 0
            mm_i = int(mm or 0)
        else:
            hh_i, mm_i = (t.split(":") + ["0"])[:2]
            hh_i, mm_i = int(hh_i), int(mm_i)
    except Exception:
        hh_i, mm_i = 12, 0
    try:
        # FF date format is MM-DD-YYYY (e.g., "09-27-2026")
        parts = date.split("-")
        if len(parts) == 3:
            mo_s, d_s, y_s = parts
            mo_i, d_i, y_i = int(mo_s), int(d_s), int(y_s)
        else:
            return 0
    except Exception:
        return 0
    is_dst = 3 <= mo_i <= 10 or (mo_i == 11 and d_i <= 7)
    utc_offset_h = 4 if is_dst else 5
    import calendar as _cal
    days_before = _cal.timegm((y_i, mo_i, d_i, hh_i, mm_i, 0, 0, 0, 0))
    return days_before + utc_offset_h * 3600


def _event_id(date, country, title):
    raw = f"{date}|{country}|{title}".encode("utf-8")
    return "ff-" + hashlib.md5(raw).hexdigest()[:12]


def fetch_ff_events(timeout=_FF_HTTP_TIMEOUT, max_retries=_FF_MAX_RETRIES, max_age_days=7):
    out = []
    cutoff = time.time() - max_age_days * 86400
    seen_ids = set()
    for url in _FF_URLS:
        for retry in range(max_retries + 1):
            try:
                r = requests.get(url, timeout=timeout,
                                 headers={"User-Agent": "trident-macro-cal/1.0"})
                r.raise_for_status()
                root = ET.fromstring(r.content)
                for ev in root.findall("event"):
                    # FF XML uses nested elements with CDATA, not attributes
                    title_el = ev.find("title")
                    country_el = ev.find("country")
                    date_el = ev.find("date")
                    time_el = ev.find("time")
                    impact_el = ev.find("impact")
                    forecast_el = ev.find("forecast")
                    previous_el = ev.find("previous")
                    actual_el = ev.find("actual")
                    title = (title_el.text or "").strip() if title_el is not None else ""
                    country = (country_el.text or "").strip().upper() if country_el is not None else ""
                    date = (date_el.text or "").strip() if date_el is not None else ""
                    time_str = (time_el.text or "").strip() if time_el is not None else ""
                    impact_raw = (impact_el.text or "").strip() if impact_el is not None else ""
                    impact = _FF_IMPACT_MAP.get(impact_raw, "low")
                    forecast = (forecast_el.text or "").strip() if forecast_el is not None else ""
                    previous = (previous_el.text or "").strip() if previous_el is not None else ""
                    actual = (actual_el.text or "").strip() if actual_el is not None else ""
                    if not title:
                        continue
                    ts = _parse_ff_datetime(date, time_str)
                    if ts <= 0:
                        continue
                    if ts < cutoff:
                        continue
                    eid = _event_id(date, country, title)
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    # v283: FOMC / Fed 主席讲话等顶级宏观事件 → 直接在源头升 5 星
                    # 旧问题:fomc_minutes_upgrade 升级后被本 sync 的
                    #         ON CONFLICT ... SET star = excluded.star 覆盖回 4 星
                    # 新解:命中关键词 → star=5 直接写入,无需 race-prone 二次升级
                    star_v = ff_impact_to_star(impact)
                    if _is_high_priority_macro(title):
                        star_v = 5
                    out.append({
                        "source": "ff",
                        "event_id": eid,
                        "country": country,
                        "title": title,
                        "impact": impact,
                        "star": star_v,
                        "event_time": ts,
                        "forecast": forecast,
                        "previous": previous,
                        "actual": actual,
                    })
                break
            except Exception as exc:
                if retry >= max_retries:
                    logger.warning("[FF] fetch %s failed: %s: %s", url, type(exc).__name__, exc)
                    continue
                time.sleep(0.5 * (retry + 1))
    out.sort(key=lambda e: e["event_time"])
    return out


def sync_to_db(events=None):
    import db as _db
    if events is None:
        events = fetch_ff_events()
    inserted = 0
    conn = None
    batch = 0
    try:
        conn = _db.get_connection()
        for ev in events:
            try:
                # 保持 v174 原设计: 每行 fast-fail,不在此层加逐行重试。
                # sync_to_db 在 API 启动期被调用,逐行 5s busy_timeout × 3 重试
                # (最坏 15s/行)会把启动拖到数分钟 —— 实测端口 8000 迟迟不绑定。
                # 整体级重试交给调用方 macro_calendar_notifier 错峰调度。
                conn.execute(
                    """
                    INSERT INTO macro_calendar
                        (source, event_id, country, title, impact, star,
                         event_time, forecast, previous, actual)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, event_id) DO UPDATE SET
                        country = excluded.country,
                        title   = excluded.title,
                        impact  = excluded.impact,
                        star    = excluded.star,
                        event_time = excluded.event_time,
                        forecast = excluded.forecast,
                        previous = excluded.previous,
                        actual   = excluded.actual,
                        fetched_at = datetime('now')
                    """,
                    (ev["source"], ev["event_id"], ev.get("country", ""),
                     ev.get("title", ""), ev.get("impact", "low"),
                     int(ev.get("star", 0)),  # v215: 1-5 颗星
                     int(ev.get("event_time", 0)),
                     ev.get("forecast", ""), ev.get("previous", ""),
                     ev.get("actual", "")),
                )
                inserted += 1
                batch += 1
            except Exception as exc:
                logger.warning("[FF] upsert failed: %s: %s", type(exc).__name__, exc)
                # v189-8: 单行失败必须回滚。原实现只 continue, 连接仍停留在未提交
                # 事务中, 后续行在同一事务内继续等 busy_timeout, 把持锁窗口从"整个
                # events 列表"放大成"多次 5s 等待叠加" (历史上 1049 次 [FF] upsert
                # failed 即此模式)。
                try:
                    conn.rollback()
                except Exception:
                    pass
                batch = 0
                continue
            if batch >= _COMMIT_EVERY:
                try:
                    conn.commit()
                except Exception as exc:
                    logger.warning("[FF] batch commit failed: %s: %s", type(exc).__name__, exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                batch = 0
        conn.commit()
    except Exception as exc:
        logger.warning("[FF] sync_to_db failed: %s: %s", type(exc).__name__, exc)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return {"fetched": len(events), "inserted": inserted}


_sync_started = False


async def macro_calendar_ff_sync_loop(interval_seconds=1800.0):
    global _sync_started
    if _sync_started:
        return
    _sync_started = True
    import asyncio as _asyncio
    print(f"[FF-CAL] started: poll every {interval_seconds}s", flush=True)
    while True:
        try:
            # v2026.10.23: fetch_ff_events() 是同步 requests（且带多 URL 重试，异常站点
            # 单次可拖到 80s+），sync_to_db() 是同步写库。原先两者直接跑在事件循环线程上，
            # 看门狗实测单次连续阻塞 83s —— /health 这类零 DB 端点也被一起卡住。
            # 改走线程池，事件循环不再被占。
            evs = await _asyncio.to_thread(fetch_ff_events)
            r = await _asyncio.to_thread(sync_to_db, evs)
            print(f"[FF-CAL] sync: fetched={r['fetched']} inserted={r['inserted']}", flush=True)
        except Exception as exc:
            print(f"[FF-CAL] sync loop error: {type(exc).__name__}: {exc}", flush=True)
        try:
            await _asyncio.sleep(interval_seconds)
        except Exception:
            break


if __name__ == "__main__":
    import json as _json
    print("=== macro_calendar_ff self-test ===")
    evs = fetch_ff_events()
    print(f"fetched: {len(evs)}")
    for ev in evs[:5]:
        print(_json.dumps(ev, ensure_ascii=False))
    if evs:
        r = sync_to_db(evs[:5])
        print(f"sync_to_db: {r}")