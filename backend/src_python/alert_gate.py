"""推送闸门：一条提醒值不值得发到手机上。

问题（2026-11-19 实测）：同一条「[周期反转] BTC 反弹 66% ｜0 天」在 24 小时内推了 666 次，
平均 2 分钟一封，邮件像短信轰炸。根因不是信号算错，是**去重只存在内存里**——
进程一重启（看门狗昨天重启了 797 次）内存清空，同一条信号就再推一遍。

本模块把去重落到库里，重启也不丢；再叠两道闸：
  ① 指纹窗口：同一件事 N 小时内只发一次（跨进程、跨重启有效）
  ② 品类节流：同一类提醒最快 N 秒一条，密集时只发第一条

被挡下的不丢，记进 alert_gate_log（hits 累加），事后能查"那天为什么没收到"。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from typing import Tuple

import db

# 同一件事多久内不重复发
WINDOW_SEC = {
    "reversal": 12 * 3600,   # 周期反转是慢变量，一天一次足够
    "fed": 6 * 3600,         # 美联储预期变动，6 小时一次
    "strong_buy": 4 * 3600,
    "generic": 2 * 3600,
}
# 同一类提醒的最短间隔（防止多品种/多条同时刷屏）
MIN_GAP_SEC = {
    "reversal": 600,
    "fed": 1800,
    "strong_buy": 900,
    "generic": 900,
}

# ── 全局限噪（2026-11-19 用户口径："只把重要信息发我"）──
# 日额上限：当天最多发几条。超过之后无论多"新"都只进站内日志，不推手机。
DAILY_CAP = int(os.environ.get("ALERT_DAILY_CAP", "6") or 6)
# 夜间静默：这段时间只放"🔴高"和强买入；其余压到早上再算（仍受日额约束）。
QUIET_START = os.environ.get("ALERT_QUIET_START", "23:00")
QUIET_END = os.environ.get("ALERT_QUIET_END", "07:00")
# 强买入/🔴高：夜间也放
HIGH_MARKERS = ("🔴", "强买入")

DDL = """
CREATE TABLE IF NOT EXISTS alert_gate_log (
  fp TEXT PRIMARY KEY,
  kind TEXT,
  title TEXT,
  first_ts INTEGER,
  last_ts INTEGER,
  hits INTEGER DEFAULT 0,
  sent INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alert_gate_last ON alert_gate_log(last_ts DESC);
"""

_KIND_GAP_CACHE: dict = {}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db.get_db_path(), timeout=10)
    c.executescript(DDL)
    return c


def normalize(title: str) -> str:
    """把标题里会变的部分抹掉，只留"这是哪件事"。

    例：「[周期反转] ETH 反弹 66% ｜ 0 天」→「周期反转ETH反弹天」
    这样 66%→70%、0天→1天 都算同一件事，不会被当成新信号再推一遍。
    """
    s = str(title or "")
    s = re.sub(r"[0-9]+(?:\.[0-9]+)?", "", s)      # 数字（价格/百分比/天数）
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)       # 标点、空格、符号
    return s[:80]


def fingerprint(kind: str, title: str) -> str:
    return hashlib.sha1(f"{kind}|{normalize(title)}".encode("utf-8")).hexdigest()[:16]


def infer_kind(title: str) -> str:
    """从标题前缀认品类，这样调用方不用改签名（老代码仍是两参数调用）。"""
    t = str(title or "")
    if "[周期反转]" in t or "周期反转" in t:
        return "reversal"
    if "美联储" in t or "Fed" in t or "FOMC" in t:
        return "fed"
    if "强买入" in t:
        return "strong_buy"
    return "generic"


def _hhmm_now() -> str:
    return time.strftime("%H:%M")


def _in_quiet(hhmm: str) -> bool:
    """夜间静默判定，支持跨零点（23:00–07:00）。"""
    try:
        if QUIET_START <= QUIET_END:
            return QUIET_START <= hhmm < QUIET_END
        return hhmm >= QUIET_START or hhmm < QUIET_END
    except Exception:
        return False


def _is_high(title: str) -> bool:
    t = str(title or "")
    return any(m in t for m in HIGH_MARKERS)


def today_sent_count(conn: sqlite3.Connection) -> int:
    day_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    return int(conn.execute(
        "SELECT COUNT(*) FROM alert_gate_log WHERE sent=1 AND last_ts>=?", (day_start,)
    ).fetchone()[0])


def allow(kind: str, title: str) -> Tuple[bool, str]:
    """返回 (能不能发, 原因)。原因用于日志：ok / dup / rate / quiet / cap。"""
    kind = (kind or "generic").strip() or "generic"
    if kind == "generic":
        kind = infer_kind(title)
    window = WINDOW_SEC.get(kind, WINDOW_SEC["generic"])
    min_gap = MIN_GAP_SEC.get(kind, MIN_GAP_SEC["generic"])
    fp = fingerprint(kind, title)
    now = int(time.time())

    try:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_ts, hits FROM alert_gate_log WHERE fp=?", (fp,)
            ).fetchone()
            if row and now - int(row[0]) < window:
                conn.execute(
                    "UPDATE alert_gate_log SET hits=hits+1, last_ts=last_ts WHERE fp=?", (fp,)
                )
                conn.commit()
                return False, f"dup({window//3600}h内已发过,累计{int(row[1])+1}次)"

            # 夜间静默：非高优先级压到白天
            if _in_quiet(_hhmm_now()) and not _is_high(title):
                return False, f"quiet({QUIET_START}-{QUIET_END} 仅发🔴高/强买入)"

            # 日额上限
            if DAILY_CAP > 0 and today_sent_count(conn) >= DAILY_CAP:
                return False, f"cap(今日已达 {DAILY_CAP} 条上限)"

            gap_row = conn.execute(
                "SELECT MAX(last_ts) FROM alert_gate_log WHERE kind=? AND sent=1", (kind,)
            ).fetchone()
            if gap_row and gap_row[0] and now - int(gap_row[0]) < min_gap:
                conn.execute(
                    "UPDATE alert_gate_log SET hits=hits+1 WHERE fp=?", (fp,)
                )
                conn.commit()
                return False, f"rate({min_gap//60}分钟内同类已有推送)"

            conn.execute(
                """INSERT INTO alert_gate_log(fp,kind,title,first_ts,last_ts,hits,sent)
                   VALUES(?,?,?,?,?,0,1)
                   ON CONFLICT(fp) DO UPDATE SET
                     last_ts=excluded.last_ts, sent=1,
                     title=excluded.title, hits=alert_gate_log.hits""",
                (fp, kind, str(title)[:200], now, now),
            )
            conn.commit()
            return True, "ok"
        finally:
            conn.close()
    except Exception as e:
        # 闸门自己坏了不能把推送全堵死 —— 放行，但要留痕
        print(f"[alert_gate] 闸门异常，放行: {type(e).__name__}: {e}", flush=True)
        return True, "gate_error"


def dry_run() -> bool:
    return os.getenv("ALERTS_DRY_RUN", "0") == "1"
