"""宏观日历提前 + 即时通知 worker（v202+v170）。

每 MACRO_NOTIFY_POLL_SECONDS 秒扫描一次金十财经日历（v170 增加 ForexFactory 源）：
  - 对 star ≥ MACRO_NOTIFY_MIN_STAR 且尚未公布的事件，事件前 MACRO_NOTIFY_LEAD_MINUTES
    推一条「提前通知」；
  - 事件当刻（±MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN 分钟）再推一条「即时通知」。

v170 多源融合：
  - 同一 poll 周期内，先拉 FF 入 macro_calendar（权威源：country/title/impact 标准化）；
  - 再查 macro_calendar 中是否有同标题/近时间 FF 记录做 cross-check；
  - 若 FF 有匹配 → 推送正文用 FF 的 impact 标注（高/中/低，比金十 star 更精细）；
  - 若 FF 无匹配 → 退回金十独有事件，star → impact 估算。

通知通道复用 engine.alerts.send_important_news(title, body) — SMTP 邮件 +
Server酱/PushPlus 微信两套通道，**不新加 SMTP / 公众号 API**（满足用户「复用现有渠道」要求）。

幂等：macro_notify_state 表 UNIQUE (event_id, window) — 同事件同窗口只推一次。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import db
import jin10_mcp
import macro_calendar_ff
import tz_utils

from .alerts import send_important_news
from .utils import _now


_WINDOW_DEFS: Dict[str, Dict[str, Any]] = {
    "lead_2h": {
        "title_prefix": "🗓️ 提前 2h｜",
        "body_prefix": "【财经日历提醒】距离公布还有约 2 小时：",
    },
    "at_time": {
        "title_prefix": "⏰ 现在｜",
        "body_prefix": "【财经日历提醒】数据正在公布：",
    },
}


def _parse_event_ts(pub_time: str) -> Optional[int]:
    """金十日历 pub_time → UTC 秒级时间戳。失败 → None。

    委托 config.parse_time_to_bj（无时区按北京时间，已带时区按 ISO 解析）。
    """
    try:
        dt = config.parse_time_to_bj(pub_time)
    except Exception:
        return None
    if dt is None:
        return None
    try:
        return int(dt.timestamp())
    except Exception:
        return None


def _record_notified(conn, event_id: str, title: str, event_time: int,
                     star: int, window: str) -> bool:
    """写 macro_notify_state。UNIQUE 冲突 → False，其它异常也 False。

    触发推送前先调一次确保唯一性。重复启动/重发不重复推。
    """
    try:
        conn.execute(
            "INSERT INTO macro_notify_state "
            "(event_id, event_title, event_time, star, window) VALUES (?, ?, ?, ?, ?)",
            (event_id, title, event_time, star, window),
        )
        conn.commit()
        return True
    except Exception as exc:
        if "UNIQUE" in str(exc).upper() or "constraint failed" in str(exc).lower():
            conn.rollback()
            return False
        conn.rollback()
        print(f"[MACRO-NOTIFY] record failed: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return False


def _format_title(prefix: str, raw_title: str, star: int) -> str:
    short = raw_title[:18].strip() if raw_title else "财经事件"
    stars = "★" * int(star) if star else "★"
    return f"{prefix}{stars} {short}"


def _format_body(prefix: str, pub_time: str, star: int,
                 consensus: Any, previous: Any,
                 ff_impact: Optional[str] = None,
                 ff_country: Optional[str] = None) -> str:
    """推送正文：prefix + 公布时间(BJ) + 预测/前值。"""
    lines: List[str] = [prefix.rstrip("：") + ":"]
    try:
        ts_disp = tz_utils.format_dual(pub_time) if pub_time else "—"
    except Exception:
        ts_disp = str(pub_time or "—")
    lines.append(f"公布时间：{ts_disp}")
    star_disp = int(star) if star else 0
    star_line = f"重要度：{'★' * star_disp} ({star_disp}/5)"
    if ff_impact:
        # v170: FF cross-check 命中时追加一行「FF评级」
        impact_disp = {"high": "高", "med": "中", "low": "低"}.get(
            str(ff_impact).lower(), ff_impact)
        country_disp = f" [{ff_country}]" if ff_country else ""
        star_line += f" ｜ ForexFactory: {impact_disp}{country_disp}"
    lines.append(star_line)
    if consensus not in (None, "", "—"):
        lines.append(f"市场预期：{consensus}")
    if previous not in (None, "", "—"):
        lines.append(f"前值：{previous}")
    lines.append("— 财经日历提醒（v170 多源融合：FF + 金十）")
    return "\n".join(lines)


def _lookup_ff_enrichment(raw_title: str, pub_ts: int, conn=None) -> Optional[Dict[str, Any]]:
    """v170: 在 macro_calendar 中查 FF 是否有同标题/近时间的记录 → 用作 cross-check。

    匹配规则：
      - 时间窗 ±60 分钟（FF XML 时间精度略低，宽容一些）；
      - 标题去大小写+去空白后精确匹配（FF/jin10 同事件通常一致，仅中英文略有差异，
        这里先做精确匹配，避免误合并）。
    返回: dict {impact, country, title, event_time} 或 None。
    """
    if not raw_title or pub_ts <= 0:
        return None
    norm_title = "".join(raw_title.lower().split())
    win_lo = pub_ts - 3600
    win_hi = pub_ts + 3600
    close_conn = False
    if conn is None:
        conn = db.get_connection()
        close_conn = True
    try:
        rows = conn.execute(
            """
            SELECT impact, country, title, event_time
              FROM macro_calendar
             WHERE source='ff' AND event_time BETWEEN ? AND ?
            """,
            (win_lo, win_hi),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass
    for r in rows:
        impact, country, title, ets = r
        if "".join(str(title or "").lower().split()) == norm_title:
            return {"impact": impact, "country": country,
                    "title": title, "event_time": int(ets)}
    return None


def _impact_to_star(impact: str) -> int:
    """FF impact (high/med/low) → star 数（仅做正文展示用，不参与阈值判断）。"""
    if not impact:
        return 0
    s = str(impact).lower()
    if s == "high":
        return 3
    if s == "med":
        return 2
    return 1


def _trigger_one(event: Dict[str, Any], window: str) -> None:
    """触发单条推送：写状态 + send_important_news。"""
    raw_title = str(event.get("title") or "").strip()
    pub_time = str(event.get("pub_time") or "").strip()
    star = int(event.get("star") or 0)
    # v170: 若 loop 注入了 display_star/ff_impact/ff_country，使用 display_star 替换 star（仅展示）
    display_star = int(event.get("display_star", star) or star)
    ff_impact = event.get("ff_impact")
    ff_country = event.get("ff_country")
    consensus = event.get("consensus")
    previous = event.get("previous")
    event_id = f"jin10-cal-{raw_title}-{pub_time[:16]}"
    event_ts = _parse_event_ts(pub_time) or 0
    wdef = _WINDOW_DEFS.get(window) or {}
    title = _format_title(wdef.get("title_prefix", ""), raw_title, display_star)
    body = _format_body(wdef.get("body_prefix", ""), pub_time, display_star,
                        consensus, previous, ff_impact, ff_country)

    conn = db.get_connection()
    try:
        if not _record_notified(conn, event_id, raw_title, event_ts,
                                max(star, display_star), window):
            return  # UNIQUE 冲突 → 已推送过
    finally:
        conn.close()

    try:
        send_important_news(title, body)
        print(f"[MACRO-NOTIFY] {window} ★{display_star} {raw_title[:30]} 推送已触发 @ {_now()}", flush=True)
    except Exception as exc:
        print(f"[MACRO-NOTIFY] {window} push failed: {type(exc).__name__}: {str(exc)[:80]}", flush=True)


async def macro_calendar_notifier_loop() -> None:
    """主循环：每 MACRO_NOTIFY_POLL_SECONDS 秒扫一次金十日历。

    设计：
    - 启动时立即跑一次 → 防止首次启动错过已经在 2h 窗口内的事件；
    - 后续按 MACRO_NOTIFY_POLL_SECONDS 节奏轮询；
    - horizon_hours 取 max(LEAD_MINUTES, 24)/60 + 1 保证提前量 + 当刻覆盖；
    - 任一事件失败不阻断其它事件。
    """
    if not config.MACRO_NOTIFY_ENABLED:
        print("[MACRO-NOTIFY] MACRO_NOTIFY_ENABLED=0，跳过启动", flush=True)
        return

    lead_min = int(config.MACRO_NOTIFY_LEAD_MINUTES)
    tolerance_min = int(config.MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN)
    horizon_h = max(24.0, lead_min / 60.0 + 2.0)

    print(
        f"[MACRO-NOTIFY] started: min_star={config.MACRO_NOTIFY_MIN_STAR} "
        f"lead_min={lead_min} tolerance={tolerance_min}min "
        f"poll={config.MACRO_NOTIFY_POLL_SECONDS}s horizon={horizon_h:.1f}h",
        flush=True,
    )

    while True:
        # v170: 每个 poll 周期先拉一次 FF 入 macro_calendar（best-effort，失败不阻断主链路）
        try:
            ff_evs = macro_calendar_ff.fetch_ff_events()
            if ff_evs:
                macro_calendar_ff.sync_to_db(ff_evs)
        except Exception as exc:
            print(f"[MACRO-NOTIFY] FF pre-sync failed: {type(exc).__name__}: {str(exc)[:80]}", flush=True)

        try:
            events = jin10_mcp.upcoming_events(
                min_star=config.MACRO_NOTIFY_MIN_STAR,
                horizon_hours=horizon_h,
            )
            now_ts = time.time()
            for ev in events:
                pub_raw = str(ev.get("pub_time") or "").strip()
                if not pub_raw:
                    continue
                pub_ts = _parse_event_ts(pub_raw)
                if pub_ts is None:
                    continue
                diff_min = (pub_ts - now_ts) / 60.0

                # v170: FF cross-check — 若 FF 库有同标题/近时间记录，把 impact 注入 event。
                # 不影响触发阈值（仍用 jin10 star），只影响推送正文「重要度」字段展示。
                ff_match = _lookup_ff_enrichment(
                    str(ev.get("title") or "").strip(), pub_ts)
                if ff_match:
                    ev = dict(ev)  # shallow copy 不污染 source
                    ff_star = _impact_to_star(ff_match.get("impact", ""))
                    ev["ff_impact"] = ff_match.get("impact", "")
                    ev["ff_country"] = ff_match.get("country", "")
                    # 若 FF 显示 high 而 jin10 star 偏低（≤2），提升推送正文显示但不触发（仅显示）
                    if ff_star > int(ev.get("star", 0) or 0):
                        ev["display_star"] = ff_star
                    else:
                        ev["display_star"] = int(ev.get("star", 0) or 0)

                # 提前 N 分钟窗口：在 [lead_min-10, lead_min] 区间触发，
                # 避免提前 1h/2h/3h 各推一次
                if 1.0 <= diff_min <= float(lead_min) \
                        and diff_min >= float(lead_min) - 10.0:
                    _trigger_one(ev, "lead_2h")

                # 事件当刻窗口：±tolerance_min
                if -float(tolerance_min) <= diff_min <= float(tolerance_min):
                    _trigger_one(ev, "at_time")
        except Exception as exc:
            print(f"[MACRO-NOTIFY] loop error: {type(exc).__name__}: {str(exc)[:120]}", flush=True)

        await asyncio.sleep(int(config.MACRO_NOTIFY_POLL_SECONDS))


def _self_test_dry_run() -> Dict[str, Any]:
    """不实际推送的烟雾测试。

    测试场景：
    - event_time = now + lead_min - 5min → 触发 lead_2h
    - event_time = now → 触发 at_time
    - star < min_star → 不触发任何窗口
    """
    now_ts = time.time()
    fake_events: List[Dict[str, Any]] = [
        {  # 提前 2h-5min → lead_2h
            "pub_time": datetime.fromtimestamp(
                now_ts + config.MACRO_NOTIFY_LEAD_MINUTES * 60 - 300,
                tz=timezone.utc).isoformat(),
            "star": 3, "title": "美国 CPI 同比", "consensus": "3.2%", "previous": "3.1%",
        },
        {  # 当刻 → at_time
            "pub_time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "star": 4, "title": "美联储利率决议", "consensus": "5.50%", "previous": "5.50%",
        },
        {  # 低于门槛 → 不触发
            "pub_time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "star": 1, "title": "小数据", "consensus": "—", "previous": "—",
        },
    ]
    triggered: List[Dict[str, Any]] = []
    for ev in fake_events:
        pub_ts = _parse_event_ts(ev["pub_time"])
        if pub_ts is None:
            continue
        diff_min = (pub_ts - now_ts) / 60.0
        if ev["star"] < config.MACRO_NOTIFY_MIN_STAR:
            continue
        if 1.0 <= diff_min <= float(config.MACRO_NOTIFY_LEAD_MINUTES) \
                and diff_min >= float(config.MACRO_NOTIFY_LEAD_MINUTES) - 10.0:
            triggered.append({**ev, "_window": "lead_2h"})
        if -float(config.MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN) <= diff_min \
                <= float(config.MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN):
            triggered.append({**ev, "_window": "at_time"})
    return {
        "min_star": config.MACRO_NOTIFY_MIN_STAR,
        "lead_min": config.MACRO_NOTIFY_LEAD_MINUTES,
        "tolerance_min": config.MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN,
        "triggered": triggered,
    }


if __name__ == "__main__":
    # CLI 自检：python -m engine.macro_calendar_notifier
    print("=== macro_calendar_notifier self-test ===")
    print(_self_test_dry_run())