#!/usr/bin/env python3
"""新闻处理水位（v444）—— LLM 通道恢复后**只分析之后进来的新闻**。

用户规则（2026-11-18 原话）：

> "以后所有的 LLM 通道恢复之后，都不去做历史的新闻……它只分析现在进来的，
>  包括现在到以后进来的新闻，他不用分析以前的新闻。否则这样如果堆积的多的话，
>  我这个打不住的。"

问题形态（2026-11-18 实测）：MiniMax 额度打满 → 门卫 5 小时发不出判定 →
`raw_news` 积压 216 条 PENDING + 460 条队列。通道一恢复，认领顺序会把这些
"已经过期、也不可能再成交"的旧闻一条条送去主分析 —— 额度全部烧在历史垃圾上，
新新闻反而排不上队。旧有机制（`NEWS_ANALYZE_MAX_AGE_HOURS` 6h /
v436 建仓窗口 45min）能挡住"很旧"的，但挡不住"断档期间积的中等年龄"。

本模块提供一个**持久化 epoch 水位** `floor_ts`：

  · 主分析连续 `NEWS_RECOVERY_GAP_MIN` 分钟没有一次成功调用 → 判定"断档"，
    把水位推进到**此刻**（= 恢复时刻），此后 `ts < floor_ts` 的新闻一律不分析；
  · 水位**只前进不后退**（跨进程共享一个 JSON 文件，原子替换写）；
  · 认领 / 门卫回放 / 超龄归档 / 面板计数 四处共用这一个口径；
  · 需要重放历史做回归时：显式 `NEWS_SKIP_BACKLOG_ON_RECOVERY=0` 或删掉状态文件。

为什么不直接用"固定时长"（比如只处理 15 分钟内的）：
用户要的是**相对恢复时刻**的语义 —— 断了 5 小时，那 5 小时内进的全不算；
断了 20 分钟，那 20 分钟内进的全不算。固定时长做不到这一点（断 5 小时时
"15 分钟内"的规则会把刚恢复前一刻的旧闻当成新新闻）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_RUNTIME_DIR = Path(os.getenv("TRIDENT_RATE_DIR") or
                    (Path(__file__).resolve().parent.parent / "runtime"))
_STATE_FILE = Path(os.getenv("NEWS_CUTOFF_STATE_PATH") or
                   (_RUNTIME_DIR / "news_cutoff.json"))
_LOCK_FILE = _STATE_FILE.with_name(_STATE_FILE.name + ".lock")

# 停摆期间"_expire_stale_news"每 60s 会被调用一次，日志别刷屏
_LAST_LOG: Dict[str, Any] = {"ts": 0.0, "text": ""}


# ── 配置读取（延迟到调用时，避免 import 期硬依赖 config）────────────────
def _cfg(name: str, default: Any) -> Any:
    try:
        import config  # noqa: WPS433
        return getattr(config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    """是否启用"恢复后不补历史"（默认启，用户 2026-11-18 定策）。"""
    try:
        return bool(int(_cfg("NEWS_SKIP_BACKLOG_ON_RECOVERY", 1)))
    except (TypeError, ValueError):
        return True


def gap_minutes() -> float:
    """多久没有一次成功的主分析调用就算"断档"（分钟）。"""
    try:
        return max(0.0, float(_cfg("NEWS_RECOVERY_GAP_MIN", 15) or 0))
    except (TypeError, ValueError):
        return 15.0


# ── 状态文件读写 ────────────────────────────────────────────────────────
class _FileLock:
    """跨进程排他锁（fcntl）。拿不到锁时降级为"直接读旧值"，不阻塞引擎。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = None

    def __enter__(self) -> "_FileLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+")
            import fcntl  # noqa: WPS433  (仅 POSIX；Windows 上跳过加锁)
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self.fh = None
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.fh is not None:
                import fcntl  # noqa: WPS433
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
        except Exception:
            pass
        self.fh = None


def get_state() -> Dict[str, Any]:
    """读状态；文件不存在/损坏一律返回空水位（floor_ts=0 表示不限制）。"""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"floor_ts": 0}
        return data
    except Exception:
        return {"floor_ts": 0}


def get_floor() -> int:
    """当前水位（epoch 秒）。0 = 不限制（全量处理）。"""
    try:
        return max(0, int(get_state().get("floor_ts") or 0))
    except Exception:
        return 0


def _write_state(state: Dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp.%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(str(tmp), str(_STATE_FILE))
    except Exception as exc:
        print("[CUTOFF] ⚠️ 水位写盘失败: %s: %s" % (type(exc).__name__, str(exc)[:100]),
              file=sys.stderr)


def advance_floor(ts: float, reason: str = "") -> int:
    """把水位推进到 ts（**只前进不后退**）。返回推进后的水位。

    推进是幂等的：同一时刻重复调用不会写出更低的值。跨进程用文件锁串行化。
    """
    try:
        new_ts = int(float(ts))
    except (TypeError, ValueError):
        return get_floor()
    with _FileLock(_LOCK_FILE):
        cur = get_floor()
        if new_ts <= cur:
            return cur
        _write_state({
            "floor_ts": new_ts,
            "floor_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_ts)),
            "reason": str(reason or "")[:120],
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return new_ts


def advance_to_now(reason: str = "") -> int:
    return advance_floor(time.time(), reason)


def is_stale(ts: Any, floor: Optional[int] = None) -> bool:
    """这条新闻是否在"不分析"的水位之下。floor=None 时读当前水位。"""
    _floor = get_floor() if floor is None else int(floor or 0)
    if _floor <= 0:
        return False
    if ts is None or ts == "":
        return False  # 没有时间戳 → 不在这里判死，交给原有超龄闸
    try:
        _ts = float(ts)
    except (TypeError, ValueError):
        return False
    if _ts <= 0:
        return False
    return _ts < _floor


# ── 恢复判定 ────────────────────────────────────────────────────────────
_HEALTHY_PURPOSES = ("main", "analysis")


def last_healthy_ts(conn: sqlite3.Connection) -> int:
    """最近一次**成功的主分析调用**时刻（epoch 秒）。0 = 从未成功过。

    只看 purpose∈('main','analysis')：门卫（gate）即使在额度耗尽时也会偶发成功
    （实测 16:26 就有 ok 的 gate 调用），它成功不代表"决策链活着"—— 只有主分析
    出结果才算恢复。这也正是 2026-11-18 事故里"门卫在响、决策为 0"的区分点。
    """
    try:
        # 用 `ORDER BY id DESC LIMIT 1` 而不是 MAX(ts)：ai_call_stats 有 40 万行，
        # 本函数每 60s 被调用一次，走主键倒序扫、命中第一条 ok 就停（毫秒级）；
        # MAX(ts) 是全表聚合（几十~上百毫秒），没必要每轮都付这个代价。
        row = conn.execute(
            "SELECT ts FROM ai_call_stats"
            " WHERE status='ok' AND purpose IN (%s)"
            " ORDER BY id DESC LIMIT 1"
            % ", ".join("?" * len(_HEALTHY_PURPOSES)),
            _HEALTHY_PURPOSES,
        ).fetchone()
        return int((row and row[0]) or 0)
    except sqlite3.Error:
        return 0


def maybe_advance_on_recovery(conn: sqlite3.Connection,
                              now: Optional[float] = None,
                              db_path: Optional[str] = None) -> Tuple[int, int]:
    """断档判定 + 水位推进。返回 (推进后的水位, 本次推进了多少秒)。

    语义（用户规则）：
      · 距上一次成功主分析 < gap → 链路正常，不动水位；
      · 距上一次成功主分析 ≥ gap → 说明断过档，**恢复时刻 = now**，
        此刻之前进的新闻全部不再分析。

    注意：断档期间本函数每 60s 会被调用一次，水位会持续被推到"当前时刻"，
    这是**有意的** —— 通道真正恢复的那一刻，水位恰好就是恢复时刻，
    不需要额外去"检测恢复事件"（少一个状态机就少一类漏判）。
    """
    if not enabled():
        return get_floor(), 0
    _gap = gap_minutes()
    if _gap <= 0:
        return get_floor(), 0
    _now = float(now if now is not None else time.time())

    _own_conn = False
    if conn is None:
        try:
            conn = sqlite3.connect(db_path or str(_RUNTIME_DIR.parent / "trident_event_bus.db"),
                                   timeout=5.0)
            _own_conn = True
        except sqlite3.Error:
            return get_floor(), 0
    try:
        _last_ok = last_healthy_ts(conn)
    finally:
        if _own_conn:
            try:
                conn.close()
            except Exception:
                pass

    _floor = get_floor()
    # 已有的水位本身就是"已知的最晚健康时刻"，别因为水位 > 最近成功时刻而反复推进
    _ref = max(_last_ok, _floor)
    if _ref <= 0:
        # 从没有过成功调用（新库/首次部署）：不推进，避免把首批历史数据当垃圾丢掉
        return _floor, 0

    _idle = _now - _ref
    if _idle < _gap * 60.0:
        return _floor, 0

    _new = advance_floor(_now, reason="llm_outage_recovery: idle %.0fmin" % (_idle / 60.0))
    if _new > _floor:
        _msg = ("[CUTOFF] 主分析断档 %.1f 分钟 → 水位推进到 %s"
                "（此前入库的新闻不再分析，只做 %s 之后进来的）"
                % (_idle / 60.0,
                   time.strftime("%H:%M:%S", time.localtime(_new)),
                   time.strftime("%H:%M:%S", time.localtime(_new))))
        _should_log = (_now - float(_LAST_LOG.get("ts") or 0.0) > 600.0
                       or _msg != _LAST_LOG.get("text"))
        if _should_log:
            _LAST_LOG["ts"] = _now
            _LAST_LOG["text"] = _msg
            print(_msg, flush=True)
    return _new, max(0, _new - _floor)


def backlog_cap() -> Tuple[int, int]:
    """(超限阈值, 保留条数)。阈值 0 = 关闭队列兜底。"""
    try:
        _cap = int(_cfg("NEWS_BACKLOG_CAP", 50) or 0)
    except (TypeError, ValueError):
        _cap = 50
    try:
        _keep = int(_cfg("NEWS_BACKLOG_KEEP", 20) or 0)
    except (TypeError, ValueError):
        _keep = 20
    return max(0, _cap), max(1, _keep)


def _keep_tradeable_clause(conn: sqlite3.Connection) -> str:
    """v453 P0-4：水位计数/取样时排除「门卫判可交易」的行。

    否则水位被这批真信号抬着走：11-18 实测 282 条 PENDING 里可交易占多数，
    兜底一推进（21:38），396 条可交易连同低价值行一起被归档。
    老库没有 gate_verdict 列时返回空串，退回旧行为。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_news)").fetchall()}
    except sqlite3.Error:
        return ""
    return " AND COALESCE(gate_verdict, '') <> '可交易'" if "gate_verdict" in cols else ""


def maybe_advance_on_backlog(conn: sqlite3.Connection,
                             now: Optional[float] = None) -> Tuple[int, int]:
    """队列兜底：待分析条数超过上限时，水位推进到"只保留最新 keep 条"。

    为什么需要（2026-11-18 定策延伸）：断档水位只管"彻底停摆后恢复"这一刻；
    可一旦分析速度追不上新闻流速（实测批 4 条/10 分钟 vs 到达 200 条/小时），
    队列会稳定堆到几百条，额度全烧在谁也赶不上的旧闻上 —— 正是用户说的
    "堆多了打不住"。所以补一条**容量**闸：队列超过 cap 就把老的推下去，
    只留最新 keep 条继续分析，留痕走同一个 `pre_recovery_skip`。

    返回 (推进后的水位, 本次推进秒数)。推进 0 表示没触发。
    """
    if not enabled():
        return get_floor(), 0
    _cap, _keep = backlog_cap()
    if _cap <= 0:
        return get_floor(), 0
    _now = float(now if now is not None else time.time())
    _floor = get_floor()
    _keep_tr = _keep_tradeable_clause(conn)
    try:
        _cnt = int(conn.execute(
            "SELECT COUNT(*) FROM raw_news"
            " WHERE status='PENDING' AND COALESCE(is_noise, 0) = 0"
            "   AND ts IS NOT NULL AND (? <= 0 OR ts >= ?)" + _keep_tr,
            (_floor, _floor),
        ).fetchone()[0] or 0)
    except sqlite3.Error:
        return _floor, 0
    if _cnt <= _cap:
        return _floor, 0
    # 取第 keep+1 新的一条，把水位压到它（这条及其更老的一起归档）
    try:
        row = conn.execute(
            "SELECT ts FROM raw_news"
            " WHERE status='PENDING' AND COALESCE(is_noise, 0) = 0"
            "   AND ts IS NOT NULL AND (? <= 0 OR ts >= ?)" + _keep_tr
            + " ORDER BY ts DESC LIMIT 1 OFFSET ?",
            (_floor, _floor, _keep),
        ).fetchone()
    except sqlite3.Error:
        return _floor, 0
    if not row or row[0] is None:
        return _floor, 0
    try:
        _cut = int(float(row[0])) + 1
    except (TypeError, ValueError):
        return _floor, 0
    _new = advance_floor(_cut, reason="queue_cap: %d pending > %d, keep %d" % (_cnt, _cap, _keep))
    if _new > _floor:
        _msg = ("[CUTOFF] 队列 %d 条超过上限 %d → 只保留最新 %d 条，"
                "水位推进到 %s" % (_cnt, _cap, _keep,
                                   time.strftime("%H:%M:%S", time.localtime(_new))))
        _should_log = (_now - float(_LAST_LOG.get("ts") or 0.0) > 300.0
                       or _msg != _LAST_LOG.get("text"))
        if _should_log:
            _LAST_LOG["ts"] = _now
            _LAST_LOG["text"] = _msg
            print(_msg, flush=True)
    return _new, max(0, _new - _floor)


def sql_floor_clause(ts_expr: str = "ts", param: str = "?") -> str:
    """给别处的 SQL 拼一个统一的"在水位之上"条件（0 = 不过滤）。

    用法：`... AND ` + news_cutoff.sql_floor_clause()，参数按 (floor, floor) 传。
    """
    return "(%s <= 0 OR (%s IS NOT NULL AND %s >= %s))" % (param, ts_expr, ts_expr, param)
