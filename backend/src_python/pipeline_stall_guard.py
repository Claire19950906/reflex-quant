"""决策链停摆检测 —— 让"14 小时零产出"这种事一定会叫出来。

背景（FIX-8，2026-11-09）
------------------------
实测：`ai_decisions` 最新一条是 05:03，而同期 `raw_news` 一直在进。
整条链静默停了 14 小时，**没有任何告警** —— 比任何指标失真都危险：
看板上"最近决策"还是个正常数字，因为它显示的是"最后一条"而不是"多久以前"。

判活口径（重要）
----------------
必须用 `MAX(id)` 或 `MAX(datetime(created_at))`，**不能**用 `MAX(created_at)`：
该列混存 `2026-11-09T05:03:36+08:00` 与 `2026-10-10 19:14:58` 两种格式，
字符串比较下凌晨 4 点会排在下午 1 点之后（`T` > 空格），取到的"最新"是假的（见 FIX-9）。
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

SHANGHAI = timezone(timedelta(hours=8))

#: 超过这个小时数没有任何新决策 → 判停摆
DEFAULT_STALL_HOURS = 2


def _parse(ts: Optional[str]) -> Optional[datetime]:
    """解析混合格式时间列，返回带时区的 datetime；不可解析返回 None。"""
    if not ts:
        return None
    s = str(ts).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 裸串按北京时间解读（与 time_utils 的口径一致）
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt


def check_stall(
    conn: sqlite3.Connection,
    *,
    stall_hours: float = DEFAULT_STALL_HOURS,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """返回停摆诊断。

    字段：
      stalled         bool      是否判停摆
      last_decision   str|None  最新一条决策的时间（原始值）
      idle_hours      float|None 距今多少小时
      news_since      int       停摆期间又进了多少条新闻（说明"不是没数据，是没处理"）
      decisions_24h   int       近 24 小时决策条数
    """
    now = now or datetime.now(SHANGHAI)

    row = conn.execute(
        "SELECT id, created_at FROM ai_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_id, last_ts = (row[0], row[1]) if row else (None, None)
    last_dt = _parse(last_ts)

    idle_hours = None
    if last_dt is not None:
        idle_hours = round((now - last_dt).total_seconds() / 3600.0, 2)

    news_since = 0
    if last_dt is not None:
        news_since = int(
            conn.execute(
                "SELECT COUNT(*) FROM raw_news WHERE datetime(timestamp) > datetime(?)",
                (last_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchone()[0]
        )

    decisions_24h = int(
        conn.execute(
            "SELECT COUNT(*) FROM ai_decisions"
            " WHERE datetime(created_at) > datetime('now', '-24 hours')"
        ).fetchone()[0]
    )

    stalled = bool(idle_hours is not None and idle_hours > stall_hours)

    # v340: "一条决策都没有"也必须能被判成停摆 —— 但只在**系统确实在跑**的前提下。
    # 判据: 库里存在 data_epoch（说明系统已经上线过，不是首次安装）。
    # 背景 (2026-11-15 实测): 纪元清空后 ai_decisions = 0，通道又全部因额度熔断，
    # 引擎照常打日志但一条决策都不出；而这里 last_dt is None → stalled=False，
    # 于是"零产出"永远不会告警，系统看起来完全正常。
    epoch_idle_h: float | None = None
    if last_dt is None:
        try:
            _ep = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
            if _ep and _ep[0]:
                _ep_dt = _parse(_ep[0])
                if _ep_dt is not None:
                    epoch_idle_h = round((now - _ep_dt).total_seconds() / 3600.0, 2)
                    # 纪元之后有没有新新闻进来（有 = 不是没数据，是没处理）
                    try:
                        news_since = int(conn.execute(
                            "SELECT COUNT(*) FROM raw_news WHERE datetime(timestamp)"
                            " > datetime(?)",
                            (_ep_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),),
                        ).fetchone()[0])
                    except Exception:
                        news_since = 0
                    stalled = bool(epoch_idle_h > stall_hours)
                    idle_hours = epoch_idle_h
        except Exception:
            epoch_idle_h = None

    return {
        "stalled": stalled,
        "last_decision": last_ts,
        "last_decision_id": last_id,
        "idle_hours": idle_hours,
        "news_since": news_since,
        "decisions_24h": decisions_24h,
        "stall_hours": stall_hours,
        "since_epoch_hours": epoch_idle_h,
    }


def format_report(d: Dict[str, object]) -> str:
    """人读一行结论。"""
    if d["last_decision"] is None:
        if d.get("since_epoch_hours") is not None:
            return (
                f"[STALL] 纪元以来一条决策都没有（已 {d['since_epoch_hours']}h），"
                f"期间进了 {d.get('news_since', 0)} 条新闻 → 不是没数据，是没处理"
            )
        return "[STALL] 库里没有任何决策记录（ai_decisions 为空）"
    if not d["stalled"]:
        return (
            f"[OK] 决策链在跑：最新 #{d['last_decision_id']} "
            f"@ {d['last_decision']}（{d['idle_hours']}h 前），近 24h {d['decisions_24h']} 条"
        )
    return (
        f"[STALL] 决策链已停 {d['idle_hours']}h（阈值 {d['stall_hours']}h）"
        f"｜最新 #{d['last_decision_id']} @ {d['last_decision']}"
        f"｜期间又进了 {d['news_since']} 条新闻 → **不是没数据，是没处理**"
    )


if __name__ == "__main__":  # pragma: no cover - 手工巡检入口
    import os
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db"
    )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        d = check_stall(conn)
        print(format_report(d))
        raise SystemExit(1 if d["stalled"] else 0)
    finally:
        conn.close()
