#!/usr/bin/env python3
"""
Trident Agent MVP — Database Layer
===================================

Single authoritative schema + migration entry point for trident_event_bus.db.

Consolidates what used to live in three places:
  - backend/init_db.py                     (two CREATE TABLE statements)
  - engine.py  :: _ensure_db_exists()      (~20 incremental ALTER TABLEs)
  - api_server.py :: _migrate_schema()     (ALTER TABLE subset)

`migrate(conn)` is idempotent: CREATE TABLE IF NOT EXISTS with the full
latest column set, then a per-column existence check (PRAGMA table_info)
before any ALTER TABLE — no try/except swallowing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config

# v171 时钟守护：carry-over / signal_state 的年龄计算改用 monotonic 双源校验，
# 检测 wall-clock 跳变（SIGINT / 离线复盘 / 跨夜运行）→ 防止衰减系数爆炸。
try:
    from clock import safe_now as _safe_now  # type: ignore
except Exception:  # pragma: no cover
    def _safe_now() -> int:  # type: ignore
        return int(time.time())


def get_db_path() -> str:
    """Return the event-bus DB path (TRIDENT_DB_PATH override aware)."""
    return config.DB_PATH


# ── v189-8 连接泄漏定位器 (opt-in, 默认关闭) ──────────────────────────────────
# 用途: 之前用 "grep get_connection vs .close 计数是否相等" 判断泄漏是不可靠的 ——
# 计数相等不代表没有泄漏 (异常路径 / 循环内累积 / 非本模块创建的连接都会让该方法失效),
# 已实测出现 6 处"修复"后真实进程 fd 仍持续增长的情况。
#
# 本追踪器在 TRIDENT_TRACE_CONN=1 时记录每个连接的**创建调用栈**, close() 时注销,
# 于是 dump_live_connections() 能直接给出"当前仍存活但从未关闭的连接"来自哪些代码。
# 关闭开关时: 不注册全局 traceback, 不使用 Connection 子类, 常驻开销 ≈ 一个 os.environ 读取。
#
# 用法:
#   1) launchctl setenv TRIDENT_TRACE_CONN 1   (或 plist 加 EnvironmentVariables)
#   2) launchctl kickstart -k gui/$(id -u)/com.trident.api
#   3) 等几分钟, 然后调用 db.dump_live_connections() 输出聚合结果
#   4) launchctl setenv TRIDENT_TRACE_CONN 0 并重启还原
_TRACE_CONN = os.environ.get("TRIDENT_TRACE_CONN") == "1"
_TRACE_MAX = 5000            # 防止长时间运行把 dict 撑爆
_live_conns: "Dict[int, Tuple[str, float]]" = {}   # id(conn) -> (创建栈, 创建时间)
_trace_lock = threading.Lock()


class _TracedConnection(sqlite3.Connection):
    """仅在追踪开启时作为 sqlite3.connect(factory=...) 使用, 用于捕获 close()。

    同样修正 with 语句: __exit__ 时既要注销追踪记录, 也要真正 close。
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return sqlite3.Connection.__exit__(self, exc_type, exc, tb)
        finally:
            try:
                self.close()
            except Exception:
                pass

    def close(self) -> None:
        if _TRACE_CONN:
            with _trace_lock:
                _live_conns.pop(id(self), None)
        super().close()


def _record_open(conn: sqlite3.Connection) -> None:
    import traceback
    stack = "".join(traceback.format_stack(limit=12)[:-1])
    with _trace_lock:
        if len(_live_conns) < _TRACE_MAX:
            _live_conns[id(conn)] = (stack, time.time())


def dump_live_connections() -> List[Dict[str, Any]]:
    """返回按创建位置聚合的存活连接列表 (仅追踪模式下有内容)。"""
    if not _TRACE_CONN:
        return []
    now = time.time()
    agg: "Dict[str, Dict[str, Any]]" = {}
    with _trace_lock:
        items = list(_live_conns.items())
    for cid, (stack, ts) in items:
        # 只取业务侧帧 (跳过 db.py 自身), 避免每个调用点各自成组
        frames = [
            ln.strip() for ln in stack.splitlines()
            if "File " in ln and "/db.py" not in ln
        ]
        key = frames[-1] if frames else "<unknown>"
        rec = agg.setdefault(key, {"count": 0, "oldest_age_s": 0.0, "example": frames[-4:]})
        rec["count"] += 1
        rec["oldest_age_s"] = max(rec["oldest_age_s"], now - ts)
    out = sorted(agg.values(), key=lambda r: r["count"], reverse=True)
    return out


def reset_connection_tracking() -> None:
    with _trace_lock:
        _live_conns.clear()


_TRACE_OUT = os.environ.get("TRIDENT_TRACE_OUT", "/tmp/trident_conn_leaks.log")
_TRACE_INTERVAL = float(os.environ.get("TRIDENT_TRACE_INTERVAL", "60"))


def _trace_reporter() -> None:  # pragma: no cover - 后台守护线程
    """周期性把存活连接快照落盘。engine 无 Web 层, 端点方案覆盖不到, 故用文件。"""
    while True:
        time.sleep(_TRACE_INTERVAL)
        try:
            groups = dump_live_connections()
            import os as _os
            import tempfile as _tf
            with _trace_lock:
                total = len(_live_conns)
            lines = [
                f"=== {time.strftime('%H:%M:%S')} pid={_os.getpid()} "
                f"live_total={total} cap={_TRACE_MAX} ==="
            ]
            for r in groups[:20]:
                lines.append(
                    f"  count={r['count']:<5} age={r['oldest_age_s']:>8.1f}s  "
                    f"{r['example'][-1] if r['example'] else '<unknown>'}"
                )
            fd_dir = "/dev/fd"
            try:
                lines.append(f"  [os fd count]={len(_os.listdir(fd_dir))}")
            except OSError:
                pass
            with open(_TRACE_OUT, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass


def start_connection_tracing() -> bool:
    """若 TRIDENT_TRACE_CONN=1 则启动快照线程。重复调用安全。"""
    if not _TRACE_CONN:
        return False
    global _trace_thread
    with _trace_lock:
        if _trace_thread is not None and _trace_thread.is_alive():
            return True
        _trace_thread = threading.Thread(
            target=_trace_reporter, name="conn-trace", daemon=True
        )
        _trace_thread.start()
        return True


_trace_thread: Optional[threading.Thread] = None


class _ManagedConnection(sqlite3.Connection):
    """让 `with db.get_connection() as conn:` 真正关闭连接。

    v189-8 根因之一: sqlite3.Connection 的上下文管理器 (__enter__/__exit__) 只负责
    事务的 commit/rollback, **不会调用 close()**。所以全库 11 处
    `with db.get_connection() as conn:` (api_server.py) 每次执行都泄漏一个 fd,
    靠 grep 计数 close() 永远查不出来 —— 追踪器一开就抓到 api_server.py:3760 count=4。

    这里让 __exit__ 在原有 commit/rollback 语义之外再关闭连接, 11 处调用点无需改动。
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def get_connection() -> sqlite3.Connection:
    # v189-8: TRIDENT_TRACE_CONN=1 时叠加 Traced (捕获 close); 两种模式都用 _ManagedConnection
    # 以修正 with 语句不关连接的既有缺陷。
    factory = _TracedConnection if _TRACE_CONN else _ManagedConnection
    conn = sqlite3.connect(get_db_path(), timeout=60.0, factory=factory)
    if _TRACE_CONN:
        _record_open(conn)
        start_connection_tracing()
    conn.execute("PRAGMA journal_mode=WAL;")
    # v176.2: busy_timeout 从 2s → 30s.
    # 2s 触发大量 "database is locked" 在 ai_worker batch_claim / source_status.report;
    # reflection_loop 高频写入让锁竞争加剧。30s 让高频写等得到锁。
    # v186 audit 修复: 30s → 60s. engine + api_server + watchdog 三进程并发,
    # forward_tracker 在 _track_cycle 期间持有 write 锁 30-49s (BATCH FAILED 实证),
    # 30s busy_timeout 等不到释放就 SQLITE_BUSY. 60s 覆盖最坏持锁窗口.
    # v189-7: 60s → 5s. 60s busy_timeout 在 WAL 高竞争下会让 worker 在同步线程中
    # 阻塞 60s (此时整个 API 端点 hang 60s,前端假死); 改为 5s 后,持锁 >5s 的长事务
    # 会快速失败并抛 OperationalError,上层调用方(execute_with_retry/record_*)自
    # 带 retry,失败一次重试或直接降级不会让用户感知 60s 卡顿. 配合 v189-6 多 VALUES
    # 单事务修复,正常持锁窗口 ≈ 5-10ms,5s timeout 完全够用.
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # v189-4: synchronous=FULL 默认 → 每次 commit fsync 磁盘,极慢.
    # 改为 NORMAL: commit 时不 fsync (只 WAL 写入 fsync),性能提升 10-100x.
    # WAL + NORMAL 是 SQLite 推荐配置 (官方文档 https://www.sqlite.org/wal.html)
    conn.execute("PRAGMA synchronous=NORMAL;")
    # v189-4: temp_store=MEMORY → 临时表/索引走内存,不写磁盘
    conn.execute("PRAGMA temp_store=MEMORY;")
    # v189-4: cache_size=-64000 → 64MB 页面缓存 (负数=KB)
    conn.execute("PRAGMA cache_size=-64000;")
    # v186: WAL 自动 checkpoint 节流 — 减少 checkpoint 时同步阻塞
    conn.execute("PRAGMA wal_autocheckpoint=1000;")  # 默认 1000 pages ≈ 4MB
    return conn


def execute_with_retry(conn_factory, sql: str, params: tuple = (), *,
                       max_retries: int = 3, base_delay: float = 0.05) -> sqlite3.Cursor:
    """v186: 通用 SQLite 写重试包装. 解决 api_server 持锁 49s+ 导致 forward_tracker /
    ai_worker / reflection 失败的根因 (database is locked).

    - conn_factory: 接受一个 callable 返回 sqlite3.Connection (单次使用即关).
      使用方应在 retry 间隙关闭旧 conn,避免连接泄漏.
    - max_retries: 默认 3 次 (0.05, 0.1, 0.2s 指数退避) — v189-7 调整,
      配合 busy_timeout=5s 后单次最坏阻塞 ≈ 3×5s+0.35s ≈ 15s.
    - busy_timeout 仍由 conn 自身 PRAGMA 决定 (db.py / utils.py 都已 5s).
    - 读操作也可以走这个获得 retry 弹性 (OperationalError 都会重试).
    """
    import sqlite3 as _sq3
    delay = base_delay
    last_exc = None
    for attempt in range(max_retries + 1):
        conn = conn_factory()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except _sq3.OperationalError as e:
            last_exc = e
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                if attempt < max_retries:
                    import time as _t
                    _t.sleep(delay)
                    delay = min(delay * 2, 2.0)  # cap at 2s
                    continue
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # 重试耗尽仍然失败
    raise last_exc if last_exc else RuntimeError("execute_with_retry: exhausted")


# v189-8: 通用重试工具 — 任意函数（含自定义 conn 生命周期）遇到 SQLITE_BUSY/locked
# 自动重试 max_retries 次,指数退避(base_delay, base_delay*2, ...);非锁类
# OperationalError (如 "no such table") 直接上抛不重试. 配合 execute_with_retry,
# 覆盖所有 lock-prone 写点 (market_snapshot / abnormal_sig / batch_claim / etc.).
def retry_on_busy(fn, *, max_retries: int = 3, base_delay: float = 0.05):
    """Run fn(); retry on sqlite3.OperationalError containing 'locked' or 'busy'.

    使用方示例:
        try:
            rid = db.retry_on_busy(lambda: _record_one_signal(conn, sig))
        except sqlite3.OperationalError as exc:
            log(...)
            continue

    注意: 调用方应保证 fn() 内部每次执行都自管理事务边界（conn.commit()），
    否则一个事务持锁跨重试调用会让锁释放不到 5s 内、其他 writer 还是会 SQLITE_BUSY。
    """
    delay = base_delay
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                last_exc = e
                if attempt < max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 2.0)  # cap 2s, 配合 5s busy_timeout 整体 ~10s 兜底
                    continue
                raise
            raise  # 非锁类错误（schema 缺列 / 唯一约束等）直接上抛,不浪费重试
    raise last_exc if last_exc else RuntimeError("retry_on_busy: exhausted")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def content_fingerprint(content: str) -> str:
    """归一化内容指纹：剥 [hash:] 前缀 → 去所有空白/标点 → 小写 → sha256 前 32 位。

    用于跨源相似新闻溯源（同事件不同源措辞差异仍归同簇）。保留字母数字（含量级数字），
    避免「加息25」与「加息50」被误并成一个簇；中文汉字按 unicode word 保留。
    """
    if not content:
        return ""
    text = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", content)
    text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    text = text.lower()
    return hashlib.sha256(text[:200].encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# v173 决策证据链 helpers（接入：ai_worker INSERT ai_decisions 后调用）
# ---------------------------------------------------------------------------

def _evidence_columns(conn: sqlite3.Connection) -> set[str]:
    """decision_evidence 表实际列名集合（兼容未迁移的旧库）。"""
    try:
        return table_columns(conn, "decision_evidence")
    except sqlite3.OperationalError:
        return set()


def record_evidence(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    news_id: int,
    used_for: str = "context",
) -> bool:
    """把"这条决策用了哪条新闻作证据"写入 decision_evidence（v173 全文溯源基石）。

    自动从 raw_news 抓取当前 published_at / ts / content_fingerprint 快照写入：
      · 防新闻后续被更新/删除时丢失溯源字段
      · 防决策回查时被"事后的篡改"污染
      · `used_for` 标注证据用途（context / trigger / confirm / cross_check）

    返回 True 表示写入成功，False 表示降级（表不存在或 DB 异常）。调用方收到 False
    不应阻断主流程——evidence 是可观测性而非业务关键路径。
    """
    cols = _evidence_columns(conn)
    if not cols:
        return False
    try:
        row = conn.execute(
            "SELECT published_at, ts, content_fingerprint FROM raw_news WHERE id = ?",
            (int(news_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    published_at_snap = str((row[0] if row else "") or "")[:64]
    fetched_at_snap = int((row[1] if row else 0) or 0)
    hash_snap = str((row[2] if row else "") or "")[:64]
    now = int(_safe_now())
    try:
        conn.execute(
            """INSERT OR REPLACE INTO decision_evidence
               (decision_id, news_id, used_for, captured_at,
                published_at_snapshot, fetched_at_snapshot, content_hash_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(decision_id), int(news_id), str(used_for)[:32], now,
             published_at_snap, fetched_at_snap, hash_snap),
        )
        return True
    except sqlite3.OperationalError:
        return False


def record_evidence_batch(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    news_ids: Iterable[int],
    used_for: str = "context",
) -> int:
    """批量记录证据链。返回成功条数（失败的截图已用 try/except 降级）。"""
    n_ok = 0
    for nid in news_ids:
        if nid is None:
            continue
        try:
            if record_evidence(conn, decision_id=int(decision_id),
                               news_id=int(nid), used_for=used_for):
                n_ok += 1
        except Exception:
            continue
    return n_ok


def fetch_evidence(
    conn: sqlite3.Connection,
    decision_id: int,
) -> List[Dict[str, Any]]:
    """读取某条决策的证据链（join raw_news 一次性拿到原文 + 溯源字段）。

    返回 [{news_id, used_for, captured_at, published_at, fetched_at, content_hash,
           source, content, ...}] —— 用于 /api/decisions/:id 全文溯源面板。
    """
    try:
        rows = conn.execute(
            """SELECT de.news_id, de.used_for, de.captured_at,
                      de.published_at_snapshot, de.fetched_at_snapshot,
                      de.content_hash_snapshot,
                      rn.source, rn.content, rn.published_at AS current_published_at,
                      rn.ts AS current_fetched_at,
                      rn.content_fingerprint AS current_hash
               FROM decision_evidence de
               LEFT JOIN raw_news rn ON rn.id = de.news_id
               WHERE de.decision_id = ?
               ORDER BY de.captured_at ASC""",
            (int(decision_id),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# v174 carry-over 信号残留 helpers（指数衰减 + TTL）
# ---------------------------------------------------------------------------

def compute_decay(initial_impact: float, decay_lambda: float,
                  age_seconds: float) -> float:
    """指数衰减：initial_impact * exp(-λ * age)。

    age_seconds 可以为负（未来的时钟漂移 → 返回 > initial_impact 时仍 clamp 到 initial_impact）。
    decay_lambda ≤ 0 时按"无衰减"处理（用 0.0 替代 λ 避免异常）。
    """
    if initial_impact <= 0:
        return 0.0
    lam = max(float(decay_lambda or 0.0), 0.0)
    age = max(float(age_seconds or 0.0), 0.0)
    return float(initial_impact) * (2.718281828459045 ** (-lam * age))


def safe_age_seconds(created_or_last_seen: Optional[int], *,
                     now: Optional[int] = None,
                     max_age: int = 30 * 86400) -> int:
    """安全 age（已 clamp）— 调用方透明替换 `max(now - ts, 0)`。

    Args:
        created_or_last_seen: 事件/信号的创建或最后观察时刻（epoch sec）
        now: 测试可注入；None=自动 safe_now()（检测 wall-clock 跳变）
        max_age: clamp 上限（默认 30 天，carry-over 完全衰减的合理上限）

    Returns:
        [0, max_age] 范围内的整数秒数；输入异常 → 返回 max_age（视为"无残留"）
    """
    if created_or_last_seen is None:
        return max_age
    try:
        ts = int(created_or_last_seen)
    except (TypeError, ValueError):
        return max_age
    if ts <= 0:
        return max_age

    current = int(now) if now is not None else _safe_now()
    try:
        age_raw = current - ts
    except Exception:
        return max_age

    if age_raw < 0:
        return 0  # 时间回退防御
    if age_raw > max_age:
        return max_age
    return int(age_raw)


def _signal_columns(conn: sqlite3.Connection) -> set[str]:
    """signal_state 表实际列集合（兼容未迁移的旧库）。"""
    try:
        return table_columns(conn, "signal_state")
    except Exception:
        return set()


def record_signal(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    asset: str,
    impact: float,
    decay_lambda: float = 1.0e-4,
    direction: str = "NEUTRAL",
    reason: str = "",
    ttl_seconds: int = 86400,
    now: Optional[int] = None,
) -> bool:
    """Upsert carry-over 信号。

    · 同 signal_id 已存在：用 max(impact) + last_seen_at 刷新（不去衰减，因为是同源新闻续报）
    · 不存在：新插入
    · now 为 None 时用 clock.safe_now()（带 wall-clock 跳变检测；测试可注入固定时间）
    """
    cols = _signal_columns(conn)
    if not cols:
        return False
    ts = int(now if now is not None else _safe_now())
    sid = str(signal_id)[:128]
    ast = str(asset or "")[:20]
    direction_str = str(direction or "NEUTRAL")[:10]
    reason_str = str(reason or "")[:200]
    try:
        existing = conn.execute(
            "SELECT impact, last_seen_at FROM signal_state WHERE signal_id=?",
            (sid,),
        ).fetchone()
        if existing:
            new_impact = max(float(existing["impact"] or 0.0), float(impact))
            # upsert 时刷新 created_at + ttl_seconds → 残留窗口从续报时刻重新计
            # （carry-over 续报 = "事件还没完"，TTL 重置）
            conn.execute(
                """UPDATE signal_state
                   SET impact=?, last_seen_at=?, created_at=?,
                       direction=?, reason=?
                   WHERE signal_id=?""",
                (new_impact, ts, ts, direction_str, reason_str, sid),
            )
        else:
            conn.execute(
                """INSERT INTO signal_state
                   (signal_id, asset, impact, decay_lambda, created_at,
                    last_seen_at, ttl_seconds, direction, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, ast, float(impact), float(decay_lambda), ts,
                 ts, int(ttl_seconds), direction_str, reason_str),
            )
        return True
    except sqlite3.OperationalError:
        return False


def get_active_signals(
    conn: sqlite3.Connection,
    *,
    asset: Optional[str] = None,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """列出当前仍活跃（TTL 内）的 carry-over 信号 + 当前残留强度。

    返回 [{signal_id, asset, impact, decay_lambda, created_at, last_seen_at,
           ttl_seconds, direction, reason, age_seconds, current_impact}]
    """
    cols = _signal_columns(conn)
    if not cols:
        return []
    ts = int(now if now is not None else _safe_now())
    sql = (
        "SELECT signal_id, asset, impact, decay_lambda, created_at, "
        "last_seen_at, ttl_seconds, direction, reason "
        "FROM signal_state WHERE (created_at + ttl_seconds) > ?"
    )
    params: List[object] = [ts]
    if asset:
        sql += " AND asset = ?"
        params.append(str(asset)[:20])
    sql += " ORDER BY last_seen_at DESC"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        age = max(ts - int(r["last_seen_at"] or ts), 0)
        cur_impact = compute_decay(
            float(r["impact"] or 0.0),
            float(r["decay_lambda"] or 0.0),
            float(age),
        )
        d = dict(r)
        d["age_seconds"] = int(age)
        d["current_impact"] = round(float(cur_impact), 6)
        out.append(d)
    return out


def purge_expired_signals(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
) -> int:
    """删除 TTL 到期的信号；返回删除条数。降级返回 0（旧库 / 异常）。"""
    cols = _signal_columns(conn)
    if not cols:
        return 0
    ts = int(now if now is not None else _safe_now())
    try:
        cur = conn.execute(
            "DELETE FROM signal_state WHERE (created_at + ttl_seconds) <= ?",
            (ts,),
        )
        return int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        return 0


def insert_raw_news(
    conn: sqlite3.Connection,
    *,
    source: str,
    content: str,
    timestamp: str,
    status: str = "PENDING",
    is_noise: int = 0,
    relevance_score: float = 0.0,
    ts: Optional[int] = None,
    published_at: str = "",
    gate: Optional[dict] = None,
) -> int:
    """写入 raw_news。生产库若尚未 migrate 出 ts/published_at 列，自动降级，避免整批入库失败。

    published_at = 新闻平台声称的发布时间（原文/解析后的字符串，无则空串）。
    采集入库时刻是 timestamp/ts —— 两者分离后，前端/导出可对比"平台几点发布 vs 我们几点抓到"。
    """
    existing = table_columns(conn, "raw_news")
    columns = ["source", "content", "timestamp", "status", "is_noise", "relevance_score"]
    values: List[object] = [source, content, timestamp, status, int(is_noise), float(relevance_score)]
    if "ts" in existing:
        columns.append("ts")
        values.append(int(ts if ts is not None else _safe_now()))
    if "published_at" in existing:
        columns.append("published_at")
        values.append(str(published_at or "")[:64])
    # 全网原始溯源：归一化指纹 + 同指纹最早采集时刻（跨源相似新闻归同簇，记录全网最早出现）
    if "content_fingerprint" in existing or "first_seen_ts" in existing:
        fp = content_fingerprint(content)
        first_seen = timestamp
        if fp:
            try:
                row = conn.execute(
                    "SELECT timestamp FROM raw_news WHERE content_fingerprint=? ORDER BY timestamp ASC LIMIT 1",
                    (fp,),
                ).fetchone()
                if row and row[0]:
                    first_seen = min(timestamp, row[0])
            except sqlite3.OperationalError:
                pass
        if "content_fingerprint" in existing:
            columns.append("content_fingerprint")
            values.append(fp)
        if "first_seen_ts" in existing:
            columns.append("first_seen_ts")
            values.append(first_seen)
    # 门卫(L3)标签：gate 为 None 时不写（调用失败降级 / 旧调用方无 gate）
    if gate:
        gate_cols = {
            "gate_core_impact": str(gate.get("core_impact") or "")[:300],
            "gate_is_material": 1 if gate.get("is_material") else 0,
            "gate_assets": json.dumps(gate.get("assets") or [], ensure_ascii=False)[:120],
            "gate_main_asset": str(gate.get("main_asset") or "")[:20],
            "gate_kind": str(gate.get("kind") or "")[:10],
            "gate_timing": str(gate.get("timing") or "")[:10],
            "gate_strength": int(gate.get("strength") or 0),
            "gate_verdict": str(gate.get("verdict") or "")[:10],
            "gate_impact_1to100": int(gate.get("impact_1to100") or 0),
        }
        for col, val in gate_cols.items():
            if col in existing:
                columns.append(col)
                values.append(val)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO raw_news ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    # 乱序/回填：若本行更早入库，把同簇已有行的 first_seen_ts 拉平到全局最早（全网溯源时间一致）
    if "content_fingerprint" in existing and "first_seen_ts" in existing and fp:
        try:
            conn.execute(
                "UPDATE raw_news SET first_seen_ts = CASE "
                "WHEN first_seen_ts IS NULL OR first_seen_ts > ? THEN ? ELSE first_seen_ts END "
                "WHERE content_fingerprint = ?",
                (first_seen, first_seen, fp),
            )
        except sqlite3.OperationalError:
            pass
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Schema — latest complete definitions
# ---------------------------------------------------------------------------

_CREATE_RAW_NEWS = """
    CREATE TABLE IF NOT EXISTS raw_news (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT    NOT NULL,
        content         TEXT    NOT NULL,
        timestamp       TEXT    NOT NULL DEFAULT (datetime('now')),
        ts              INTEGER,
        status          TEXT    NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
        is_noise        INTEGER NOT NULL DEFAULT 0,
        relevance_score REAL    NOT NULL DEFAULT 0.0,
        published_at    TEXT    NOT NULL DEFAULT '',
        gate_core_impact  TEXT  DEFAULT '',
        gate_is_material  INTEGER DEFAULT 0,
        gate_assets       TEXT  DEFAULT '',
        gate_main_asset   TEXT  DEFAULT '',
        gate_kind         TEXT  DEFAULT '',
        gate_timing       TEXT  DEFAULT '',
        gate_strength     INTEGER DEFAULT 0,
        gate_verdict      TEXT  DEFAULT '',
        gate_impact_1to100 INTEGER DEFAULT 0,           -- v181: L3 自评 1-100 影响力打分（<20 一律排除）
        content_fingerprint TEXT  NOT NULL DEFAULT '',  -- 归一化内容指纹（跨源相似溯源）
        first_seen_ts      TEXT   NOT NULL DEFAULT ''   -- 全网最早出现时间（同指纹最早采集时刻）
    );
"""

_CREATE_AI_DECISIONS = """
    CREATE TABLE IF NOT EXISTS ai_decisions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id           INTEGER NOT NULL,
        sentiment_score   REAL    NOT NULL,
        suggested_action  TEXT    NOT NULL,
        reasoning         TEXT    NOT NULL,
        created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
        status            TEXT    NOT NULL DEFAULT 'UNREAD'
            CHECK (status IN ('UNREAD', 'APPROVED', 'REJECTED', 'REVIEWED', 'AUTO_APPROVED')),
        market_category   TEXT    NOT NULL DEFAULT '',
        target_asset      TEXT    NOT NULL DEFAULT 'NONE',
        parent_id         INTEGER DEFAULT NULL,
        child_count       INTEGER DEFAULT 0,
        aggregation_key   TEXT    DEFAULT '',
        cluster_size      INTEGER DEFAULT 1,
        reasoning_path    TEXT    DEFAULT '',
        vip_tag           TEXT    DEFAULT '',
        doubao_action     TEXT    DEFAULT 'HOLD',
        doubao_reasoning  TEXT    DEFAULT '',
        extra_models_consensus TEXT DEFAULT '',
        entry_price       REAL    DEFAULT NULL,
        exit_price        REAL    DEFAULT NULL,
        max_price         REAL    DEFAULT NULL,
        min_price         REAL    DEFAULT NULL,
        max_price_time    INTEGER DEFAULT 0,
        min_price_time    INTEGER DEFAULT 0,
        is_correct        TEXT    DEFAULT '',
        settled           INTEGER DEFAULT 0,
        entry_time        TEXT    DEFAULT '',
        prediction_type        TEXT    DEFAULT 'continuation',
        event_phase            TEXT    DEFAULT 'mid',
        market_confirmation    TEXT    DEFAULT 'unknown',
        expected_horizon       TEXT    DEFAULT '1-3d',
        invalidation_condition TEXT    DEFAULT '',
        decision_context       TEXT    DEFAULT '{}',
        mfe_pct           REAL    DEFAULT NULL,
        mae_pct           REAL    DEFAULT NULL,
        forward_pnl       REAL    DEFAULT NULL,
        mfe_time_mins     REAL    DEFAULT NULL,
        event_strength    TEXT    DEFAULT 'medium',
        direct_catalyst   INTEGER DEFAULT 0,
        timeframe_match   TEXT    DEFAULT 'intraday',
        paper_trading_run_id INTEGER DEFAULT NULL,
        agent_model_id       TEXT    DEFAULT '',
        evidence_confidence  REAL    DEFAULT NULL,
        evidence_action      TEXT    DEFAULT 'HOLD',
        trade_gate_reason    TEXT    DEFAULT '',
        partial_exit_price   REAL    DEFAULT NULL,
        partial_exit_ratio   REAL    DEFAULT 0.0,
        partial_pnl_pct      REAL    DEFAULT 0.0,
        insight_hit_ids      TEXT    DEFAULT '',
        no_trade_reason      TEXT    DEFAULT '',
        heat_score           REAL    DEFAULT NULL,
        impact_score         REAL    DEFAULT NULL,
        asset_impact         REAL    DEFAULT NULL,
        is_sibling           INTEGER NOT NULL DEFAULT 0,  -- 1=多品种副决策行（同新闻主决策的其它品种）
        asset_reasoning      TEXT    DEFAULT '',  -- AI 逐品种解释"为什么该新闻关联此品种"（主行存全量）
        asset_nature         TEXT    DEFAULT '',  -- 八步①资产性质（避险/投资/投机/抗通胀等）
        factor_ranking       TEXT    DEFAULT '',  -- 八步②影响涨跌因素排行(JSON)
        impact_magnitude     REAL    DEFAULT NULL,  -- 八步③影响数量级 1-100
        verification         TEXT    DEFAULT NULL,  -- 八步⑧其他模型校验回写(JSON)
        attention_score      REAL    DEFAULT NULL,  -- 需求5：0-100 全市场关注度得分
        elasticity_window    TEXT    DEFAULT '',     -- 需求5：弹性窗参数 JSON 字符串
        data_anchor_ts       TEXT    DEFAULT '',     -- 需求5：结论依据行情数据对应时间戳
        hard_data_evidence   TEXT    DEFAULT '',     -- 需求5：逐条结论附带时间的硬数据截图
        event_struct         TEXT    DEFAULT '',     -- 规则抽取事件原子 JSON（event_struct 抽取，供规则 vs LLM 交叉校验标黄）
        FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE
    );
"""

# Incremental columns for DBs created by older CREATE TABLE versions.
# (name, column definition) — added via ALTER TABLE only when missing.
_RAW_NEWS_COLUMNS: List[Tuple[str, str]] = [
    ("is_noise",        "INTEGER NOT NULL DEFAULT 0"),
    ("relevance_score", "REAL    NOT NULL DEFAULT 0.0"),
    ("ts",              "INTEGER"),
    # 引擎认领(claim)开始处理的时间：看门狗用它判断 PROCESSING 是否卡死（区别于新闻入库 ts）
    ("processing_at",   "TEXT DEFAULT NULL"),
    # 新闻平台声称的发布时间（采集器解析出的原文时间；无则空串）。
    # timestamp/ts 是"我们采集入库时刻"——两者分离用于平台时效对比。
    ("published_at",    "TEXT NOT NULL DEFAULT ''"),
    # ── 门卫(L3)四步标签：跟着新闻走，八段式/决策/复盘共用 ──
    ("gate_core_impact",  "TEXT DEFAULT ''"),
    ("gate_is_material",  "INTEGER DEFAULT 0"),
    ("gate_assets",       "TEXT DEFAULT ''"),
    ("gate_main_asset",   "TEXT DEFAULT ''"),
    ("gate_kind",         "TEXT DEFAULT ''"),
    ("gate_timing",       "TEXT DEFAULT ''"),
    ("gate_strength",     "INTEGER DEFAULT 0"),
    ("gate_verdict",      "TEXT DEFAULT ''"),
    # v181: L3 自评 1-100 影响力打分（<20 一律排除）
    ("gate_impact_1to100","INTEGER DEFAULT 0"),
    # 归一化内容指纹 + 全网最早出现时间（跨源相似新闻溯源与时效判断）
    ("content_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("first_seen_ts",       "TEXT NOT NULL DEFAULT ''"),
    # AI 自由回答的"全网溯源信息"（自然语言文本），无确切信息为 "无法确认" 或空串
    ("net_origin_info",     "TEXT DEFAULT ''"),
    # ── 全网溯源时间（AI 推断的事件原始发生时间）──
    # 三态取值：
    #   - "YYYY-MM-DD HH:MM:SS"   AI 给出的精确事件原始时间（需配合 cross_net_source 作依据）
    #   - "AMBIGUOUS"             AI 判定存在两种可能时使用（不能给出唯一时间）
    #   - ""                      AI 拒绝回答或无溯源能力时为空
    # 三层防护（ai_worker._validate_cross_net_ts）：
    #   ① 格式正则 ② 不能未来 ③ 不能晚于自己入库(±60s)
    # 第四层（source ≥4 字符 / AMBIGUOUS 必须含"候选1"或"/"或"或"）由 ai_worker 校验
    ("cross_net_first_seen_ts", "TEXT NOT NULL DEFAULT ''"),
    ("cross_net_source",        "TEXT NOT NULL DEFAULT ''"),
    # v176.7: 失败重试计数——超时/DB 锁退回 PENDING 时 +1，超上限标 FAILED，
    # 打断"退回 → ORDER BY id ASC 重新认领 → 又失败"的死循环（否则同一条新闻反复分析）。
    ("retry_count",             "INTEGER NOT NULL DEFAULT 0"),
]

# v3xx 修复(v300-audit): news_events 曾经被迁移到"新 schema"
# (news_id / decision_id / asset / is_noise / status)，但旧列
# headline / body / assets / fetched_at 一起丢了，于是三处同时炸掉：
#   1) bridge_server 的 /api/events/stream → "no such column: headline"（前端事件流全挂）
#   2) 东财 / 技术流 / CFTC 三条新闻入库 → "table news_events has no column named headline"
#      （INSERT 全部失败，新闻流从此不再增长）
#   3) 因果图谱 causal_graph 的 news_to_move 分支取不到任何新闻 → 图谱缺"新闻→行情"边
# 这里只补列、不动既有数据，让新旧两套写侧同时可用。
_NEWS_EVENTS_LEGACY_COLUMNS: List[Tuple[str, str]] = [
    ("headline",   "TEXT    DEFAULT ''"),
    ("body",       "TEXT    DEFAULT ''"),
    ("assets",     "TEXT    DEFAULT ''"),
    ("fetched_at", "INTEGER DEFAULT 0"),
]

_AI_DECISIONS_COLUMNS: List[Tuple[str, str]] = [
    ("market_category",  "TEXT    NOT NULL DEFAULT ''"),
    ("target_asset",     "TEXT    NOT NULL DEFAULT 'NONE'"),
    ("parent_id",        "INTEGER DEFAULT NULL"),
    ("child_count",      "INTEGER DEFAULT 0"),
    ("aggregation_key",  "TEXT    DEFAULT ''"),
    ("cluster_size",     "INTEGER DEFAULT 1"),
    ("reasoning_path",   "TEXT    DEFAULT ''"),
    ("vip_tag",          "TEXT    DEFAULT ''"),
    ("doubao_action",    "TEXT    DEFAULT 'HOLD'"),
    ("doubao_reasoning", "TEXT    DEFAULT ''"),
    ("extra_models_consensus", "TEXT    DEFAULT ''"),
    ("entry_price",      "REAL    DEFAULT NULL"),
    ("exit_price",       "REAL    DEFAULT NULL"),
    ("max_price",        "REAL    DEFAULT NULL"),
    ("min_price",        "REAL    DEFAULT NULL"),
    ("max_price_time",   "INTEGER DEFAULT 0"),
    ("min_price_time",   "INTEGER DEFAULT 0"),
    ("is_correct",       "TEXT    DEFAULT ''"),
    ("settled",          "INTEGER DEFAULT 0"),
    ("entry_time",       "TEXT    DEFAULT ''"),
    ("prediction_type",        "TEXT    DEFAULT 'continuation'"),
    ("event_phase",            "TEXT    DEFAULT 'mid'"),
    ("market_confirmation",    "TEXT    DEFAULT 'unknown'"),
    ("expected_horizon",       "TEXT    DEFAULT '1-3d'"),
    ("invalidation_condition", "TEXT    DEFAULT ''"),
    ("decision_context",       "TEXT    DEFAULT '{}'"),
    ("mfe_pct",          "REAL    DEFAULT NULL"),
    ("mae_pct",          "REAL    DEFAULT NULL"),
    ("forward_pnl",      "REAL    DEFAULT NULL"),
    ("mfe_time_mins",    "REAL    DEFAULT NULL"),
    ("event_strength",   "TEXT    DEFAULT 'medium'"),
    ("direct_catalyst",  "INTEGER DEFAULT 0"),
    ("timeframe_match",  "TEXT    DEFAULT 'intraday'"),
    ("strategy_id",      "INTEGER DEFAULT NULL"),
    ("strategy_version_id", "INTEGER DEFAULT NULL"),
    ("paper_trading_run_id", "INTEGER DEFAULT NULL"),
    ("agent_model_id", "TEXT DEFAULT ''"),
    ("evidence_confidence", "REAL DEFAULT NULL"),
    ("evidence_action", "TEXT DEFAULT 'HOLD'"),
    ("trade_gate_reason", "TEXT DEFAULT ''"),
    # AI 判定方向性信号(BUY/SELL)但未实际建仓时的原因（模拟盘未运行/闸门拒绝/新闻过期/行情不可用等）
    ("no_trade_reason", "TEXT DEFAULT ''"),
    ("holding_horizon_minutes", "INTEGER DEFAULT NULL"),
    ("partial_exit_price", "REAL DEFAULT NULL"),
    ("partial_exit_ratio", "REAL DEFAULT 0.0"),
    ("partial_pnl_pct", "REAL DEFAULT 0.0"),
    ("insight_hit_ids", "TEXT DEFAULT ''"),
    # 影响评估（AI 分析产出，0-10 数值）：舆情热度 / 市场影响力 / 对该金融产品影响程度
    ("heat_score", "REAL DEFAULT NULL"),
    ("impact_score", "REAL DEFAULT NULL"),
    ("asset_impact", "REAL DEFAULT NULL"),
    # 多模型交叉验证：验证模型结果 JSON（高价值新闻触发，主模型外的 2 个模型独立分析）
    ("peer_reviews", "TEXT DEFAULT NULL"),
    ("ts", "INTEGER"),
    ("is_sibling", "INTEGER NOT NULL DEFAULT 0"),
    ("asset_reasoning", "TEXT DEFAULT ''"),
    # 1 = 该行方向单的 asset_impact 低于 config.MIN_ASSET_IMPACT_FOR_TRADE（"影响小"标记）。
    # 默认阈值 0 = 不拦也不标记；用于先观察"影响小的单子"真实表现，再决定是否硬拦。
    ("low_impact", "INTEGER NOT NULL DEFAULT 0"),
    # ── 八步推演新增字段（阶段1）──
    ("asset_nature", "TEXT DEFAULT ''"),       # 第一步：资产性质（避险/投资/投机/抗通胀等）
    ("factor_ranking", "TEXT DEFAULT ''"),     # 第二步：影响涨跌因素排行（JSON 数组字符串）
    ("impact_magnitude", "REAL DEFAULT NULL"), # 第三步：新闻对主品种影响数量级 1-100
    ("verification", "TEXT DEFAULT NULL"),     # 第八步：其他模型校验回写（JSON）
    # ── 需求5 新增子模块输出：关注度得分 / 弹性窗 / 原始数据戳 / 硬数据截图 ──
    ("attention_score", "REAL DEFAULT NULL"),      # 0-100 全市场关注度得分
    ("elasticity_window", "TEXT DEFAULT ''"),      # 弹性窗参数 JSON 字符串（minutes/decay/peak_after_minutes）
    ("data_anchor_ts", "TEXT DEFAULT ''"),         # 结论依据的行情数据对应时间戳
    ("hard_data_evidence", "TEXT DEFAULT ''"),     # 逐条结论附带时间的硬数据截图（品种+时间+数值）
    # 规则事件结构化抽取结果 JSON（event_struct.extract_events），落库供"规则 vs LLM"交叉校验标黄
    ("event_struct", "TEXT DEFAULT ''"),
    # ── 结构化输入层 (v173+)：原推理路径不动，旁路补 2 列 ──
    # structured_input: LLM 调用前拼好的 6 字段 JSON payload（meta/events/snapshot/
    #                    relations/baseline/text_summary）。读侧可视化用,不是 LLM 输入。
    # structured_check: LLM 返回后,reasoning 文本与 structured_input 的引用比对
    #                    JSON（mentioned/missing/fabricated/check_at）。
    ("structured_input", "TEXT DEFAULT ''"),
    ("structured_check", "TEXT DEFAULT ''"),
    # ── v174 强制三字段 (v172 增量)：LLM 推理新口径 ──
    # event_type_code: 必须从 dim_event_type 25 个选 1，LLM 自由字符串会被 JSON Schema 拒绝。
    #                  在 ai_worker.py L172 / INSERT L5768 写入（_ensure_columns 自动 ALTER 老库补列）。
    # snapshot_proximity: LLM reasoning 引用的行情快照距事件时间的分钟差，数字串或 "N/A"。
    #                     ai_worker.py L186 / L190 写入，可能是 "5" / "30" / "N/A" 等文本。
    # 上一版 INSERT 引用了这两列但 schema 缺，老库会 raise OperationalError → 已修复。
    ("event_type_code", "TEXT DEFAULT ''"),
    ("snapshot_proximity", "TEXT DEFAULT ''"),
    # ── v176.2 新增：决策间长期影响传递（避免每次只看本条，丢掉"上次同品种怎么判"的积累）──
    # prev_decision_id: 同 target_asset 的上一条 ai_decisions.id（用于"上次的判断是什么"反查）
    # prev_action_same_asset: 上次同品种的 suggested_action（BUY/SELL/HOLD），让 LLM 知道历史方向
    # prev_score_same_asset: 上次同品种的 sentiment_score
    # regime_at_decision: 决策当时的总市场风格（trending_up/range_bound/volatile/...），来自 market_style_snapshot
    # bias_after_reflection: 反思后对本品种的判断偏移（"上次错哪/这次要更谨慎"），来自 decision_reflection
    ("prev_decision_id", "INTEGER DEFAULT NULL"),
    ("prev_action_same_asset", "TEXT DEFAULT ''"),
    ("prev_score_same_asset", "REAL DEFAULT NULL"),
    ("regime_at_decision", "TEXT DEFAULT ''"),
    ("bias_after_reflection", "TEXT DEFAULT ''"),
    # v176.2 (backlog item 15): event_atom ↔ ai_decisions 双向索引
    # JSON array of int: e.g. "[372,373]". 一条新闻可能产生多个 event_atom (LLM fallback + 规则)。
    # 反向查找: ai_decisions.event_atom_ids LIKE '%372%' 拿决策; event_atom.raw_news_id 反查新闻。
    ("event_atom_ids", "TEXT DEFAULT ''"),
    # ── v176.5f: 闸门归因记录器（gate_chain 落库）──
    # gate_chain:      JSON 数组 [{"gate","verdict","reason"}] 闸门调用链
    # original_action: LLM 给出的原始方向单 (BUY/SELL/HOLD)
    # ungated_action:  如果所有闸门都不拦,会用该 action（影子模式）
    # 与 ai_worker.py _gr = GateRecorder() 同步 — INSERT 缺这 3 列会导致
    # OperationalError → 整批 ai_decisions persist 失败、新闻状态卡 PROCESSING → FAILED。
    # 历史 bug：2026-09-23 43 条新闻 FAILED 全因此，Batch done 仍报 0 failed（计数只看 LLM 调用）。
    ("gate_chain", "TEXT DEFAULT ''"),
    ("original_action", "TEXT DEFAULT ''"),
    ("ungated_action", "TEXT DEFAULT ''"),
    # ── v181 结构化产出摘要（准优先）：7 项摘要 JSON 落库，供前端卡片渲染 ──
    # 格式: {"target_asset", "final_action", "key_factors", "fund_driver",
    #        "consensus_vs_us", "self_check", "asset_classification"}
    ("structured_summary", "TEXT DEFAULT ''"),
    # ── v181 结算口径诊断：verdict 判定时的边缘情形标签（逗号分隔），归因时据此隔离 ──
    # slow_reaction / threshold_multiplied / both_idle / entry_missing / window_truncated
    ("settle_flags", "TEXT DEFAULT ''"),
    # ── v20251014 信号权重评估（6 维综合，对应 weight = base × consensus × source × reaction × regime × time）──
    # signal_weight: 0~1 实时信号综合权重（取代 LLM score 单一信号强度的不足）
    #   写入时机: ai_worker INSERT 后立即算一次(无市场反应 → 仅 score × consensus);
    #            signal_weight_cron 每 5min 用最新 abnormal_signals 复算(含 reaction_mult);
    #            settlement 后用 forward_pnl 反向校准 source_stats (闭环)。
    # weight_tier: strong(>0.7) / medium(0.3-0.7) / weak(0.1-0.3) / ignored(<0.1)
    #   前端按 tier 决定 K 线标记颜色深度 + 是否显示。
    # weight_explain: 6 维系数明细 (调试/前端悬浮窗用)
    #   格式: "base=0.7 × consensus=1.2 × source=0.8 × reaction=1.0 × regime=1.0 × time=0.9 = 0.61"
    # weight_updated_at: 最近一次复算时间 (UTC 秒); 用作前端 stale 检测。
    # cold_signal: 1 = 信号发出 30min 后 abnormal.pct_change < 0.2% (市场不关心 → 自动降级)
    ("signal_weight", "REAL DEFAULT NULL"),
    ("weight_tier", "TEXT DEFAULT ''"),
    ("weight_explain", "TEXT DEFAULT ''"),
    ("weight_updated_at", "INTEGER DEFAULT NULL"),
    ("cold_signal", "INTEGER NOT NULL DEFAULT 0"),
]


# ---------------------------------------------------------------------------
# PR-5 baseline 真实数据：backtest_event_outcomes 缓存表
# ---------------------------------------------------------------------------
# 目的：把「同 type_code + asset 的历史 4h 表现」缓存成一张表。
#     ai_decisions INSERT 时 structured_input.baseline block 直接读这张表,
#     不在主路径做 JOIN+聚合(避免每次决策都做 5 表 JOIN)。
# 写入方:周期性刷新任务(API server idle / 调度器)调 refresh_backtest_event_outcomes();
#     ai_worker hook 也可兜底触发(轻量、不阻塞)。
# 读出方:structured_input._baseline_block。
# 字段:per (type_code, asset) 聚合 win_n / loss_n / sample_n / avg_pnl_4h / last_updated。

_CREATE_BACKTEST_EVENT_OUTCOMES = """
    CREATE TABLE IF NOT EXISTS backtest_event_outcomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        type_code       TEXT    NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        sample_n        INTEGER NOT NULL DEFAULT 0,
        win_n           INTEGER NOT NULL DEFAULT 0,
        loss_n          INTEGER NOT NULL DEFAULT 0,
        avg_pnl_4h      REAL    NOT NULL DEFAULT 0.0,
        last_updated    INTEGER NOT NULL,
        UNIQUE (type_code, asset)
    );
"""


# v20251014: 信号权重评估系统
#   3 张新表:
#     1. signal_weight_history  决策每次权重变化 (insert 时 + cron 复算时各记一行)
#     2. source_stats           按 (source × asset) 聚合历史 win_rate, 闭环校准 source_mult
#     3. signal_weight_cron_log cron 任务执行日志 (调试/审计)
_CREATE_SIGNAL_WEIGHT_HISTORY = """
    CREATE TABLE IF NOT EXISTS signal_weight_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        computed_at     INTEGER NOT NULL,                  -- UTC 秒
        trigger_kind    TEXT    NOT NULL DEFAULT 'insert', -- insert | cron_react | cron_settle | manual
        signal_weight   REAL    NOT NULL,
        weight_tier     TEXT    NOT NULL DEFAULT '',
        base_mult       REAL    NOT NULL DEFAULT 1.0,
        consensus_mult  REAL    NOT NULL DEFAULT 1.0,
        source_mult     REAL    NOT NULL DEFAULT 1.0,
        reaction_mult   REAL    NOT NULL DEFAULT 1.0,
        regime_mult     REAL    NOT NULL DEFAULT 1.0,
        time_mult       REAL    NOT NULL DEFAULT 1.0,
        explain         TEXT    NOT NULL DEFAULT '',
        abnormal_id     INTEGER DEFAULT NULL,              -- 反应系数所用的 abnormal_signals.id
        source_stats_id INTEGER DEFAULT NULL               -- 源系数所用的 source_stats.id
    );
"""

_CREATE_SOURCE_STATS = """
    CREATE TABLE IF NOT EXISTS source_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        news_source     TEXT    NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        sample_n        INTEGER NOT NULL DEFAULT 0,
        win_n           INTEGER NOT NULL DEFAULT 0,
        loss_n          INTEGER NOT NULL DEFAULT 0,
        avg_forward_pnl REAL    NOT NULL DEFAULT 0.0,
        hit_rate        REAL    NOT NULL DEFAULT 0.0,        -- win / (win+loss) ∈ [0,1]; sample<5 时为 NULL
        source_mult     REAL    NOT NULL DEFAULT 1.0,        -- 命中率→权重 (0.3-1.5)
        last_updated    INTEGER NOT NULL,
        UNIQUE (news_source, asset)
    );
"""

_CREATE_SIGNAL_WEIGHT_CRON_LOG = """
    CREATE TABLE IF NOT EXISTS signal_weight_cron_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      INTEGER NOT NULL,
        finished_at     INTEGER,
        processed_n     INTEGER NOT NULL DEFAULT 0,
        updated_n       INTEGER NOT NULL DEFAULT 0,
        cold_n          INTEGER NOT NULL DEFAULT 0,
        error_msg       TEXT    NOT NULL DEFAULT ''
    );
"""

_CREATE_IDX_SWH_DECISION = "CREATE INDEX IF NOT EXISTS idx_swh_decision ON signal_weight_history(decision_id);"
_CREATE_IDX_SWH_COMPUTED = "CREATE INDEX IF NOT EXISTS idx_swh_computed ON signal_weight_history(computed_at);"
_CREATE_IDX_SS_SOURCE    = "CREATE INDEX IF NOT EXISTS idx_ss_source    ON source_stats(news_source, asset);"


def refresh_backtest_event_outcomes(conn: sqlite3.Connection,
                                     days: int = 30,
                                     min_sample: int = 3) -> int:
    """按 (type_code, asset) 聚合近 N 天 signal_performance 的样本/胜率/平均 4h 收益。

    P2-12 增强：拆分 1h / 4h / 24h 三窗口的 avg_pnl + win_rate，存到 backtest_event_outcomes。
    三窗口分桶依据：sp.ts - ad.ts（决策时刻）= elapsed_minutes，按 60/240/1440 分桶。

    写入 backtest_event_outcomes 缓存表(覆盖式 upsert)。失败回 0。

    数据链路：
        event_atom.raw_news_id → raw_news.id → ai_decisions.news_id → signal_performance.decision_id
        event_atom.event_type_id → dim_event_type.code (= type_code)
        event_atom.asset_id → dim_asset.code (= asset,asset 可能为空字符串)

    is_correct = 'WIN'  -> win_n +=1
    is_correct = 'LOSS' -> loss_n +=1
    avg_pnl_4h = AVG(signal_performance.forward_pnl) WHERE forward_pnl IS NOT NULL

    注：本函数读路径只走 5 表 JOIN + GROUP BY，small DB 上 < 100ms。
        写入走 INSERT ... ON CONFLICT REPLACE 覆盖式 upsert（idempotent）。
    """
    if conn is None:
        return 0
    # v176.5: 调用者可能没设 row_factory=Row. 用 SELECT 索引会 IndexError.
    # 这里临时设置避免后续 r["key"] 取值失败 (不会改 conn.row_factory 类型)
    _orig_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        now = int(_safe_now())
        cutoff = now - days * 86400
        # P2-12 关键改动：JOIN 时计算 elapsed_minutes（sp.ts - ad.ts），
        # 然后按 1h/4h/24h 三窗口聚合。两个独立 SQL（避免 OR 复杂度）
        # v176.5: 直接从 signal_performance 表聚合, 不 JOIN ai_decisions/event_atom.
        #   原因 1: sp.decision_id 大多数指向已被 truncate 的旧 ai_decisions.id, JOIN 158/159 不匹配.
        #   原因 2: sp 表本身就有 asset + action 列, 足够做 (type_code, asset) 聚合.
        #   原因 3: type_code 从 sp.action 派生 (BUY→bullish, SELL→bearish, HOLD→neutral).
        #          真正 type_code 用 ad.event_type_code (左连接, 不命中也保留)
        #   v176.5: 排除 sp.is_correct='HOLD' 的未结样本 (146/159 都是 HOLD, 会污染 baseline)
        def _agg(bucket_min: int) -> list:
            return conn.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(ad.event_type_code), ''), 'market_event') AS type_code,
                       UPPER(COALESCE(NULLIF(TRIM(sp.asset), ''), 'NONE')) AS asset,
                       SUM(CASE WHEN UPPER(COALESCE(sp.is_correct, '')) = 'WIN' THEN 1 ELSE 0 END) AS win_n,
                       SUM(CASE WHEN UPPER(COALESCE(sp.is_correct, '')) = 'LOSS' THEN 1 ELSE 0 END) AS loss_n,
                       COUNT(sp.id) AS sample_n,
                       AVG(sp.forward_pnl) AS avg_pnl
                FROM signal_performance sp
                LEFT JOIN ai_decisions ad ON ad.id = sp.decision_id
                WHERE sp.ts >= ?
                  AND (sp.ts - COALESCE(ad.ts, sp.ts)) BETWEEN 0 AND ?
                  AND UPPER(COALESCE(sp.is_correct, '')) IN ('WIN', 'LOSS')
                GROUP BY COALESCE(NULLIF(TRIM(ad.event_type_code), ''), 'market_event'),
                         UPPER(COALESCE(NULLIF(TRIM(sp.asset), ''), 'NONE'))
                """,
                (cutoff, bucket_min * 60),
            ).fetchall()

        rows_1h = _agg(1)
        rows_4h = _agg(4)
        rows_24h = _agg(24)

        # 合并成 {(type_code, asset): {1h: stats, 4h: stats, 24h: stats}}
        agg: dict = {}
        for bucket_rows, label in ((rows_1h, "1h"), (rows_4h, "4h"), (rows_24h, "24h")):
            for r in bucket_rows:
                key = (r["type_code"], r["asset"])
                if key not in agg:
                    agg[key] = {"win_n": 0, "loss_n": 0, "sample_n": 0}
                sub = agg[key]
                # 同 (type, asset) 的不同窗口样本可能重复：去重一次（按 sp.id 不可行，
                # 这里近似：分窗口独立计算，对外不重复计 win/loss）
                sub[f"{label}_win"] = int(r["win_n"] or 0)
                sub[f"{label}_loss"] = int(r["loss_n"] or 0)
                sub[f"{label}_sample"] = int(r["sample_n"] or 0)
                sub[f"{label}_avg_pnl"] = float(r["avg_pnl"]) if r["avg_pnl"] is not None else 0.0
        # 顶层 win/loss/sample 用 4h 作主参照（保持 schema 兼容）
        rows = rows_4h
    except sqlite3.Error:
        return 0

    written = 0
    try:
        for r in rows:
            sample_n = int(r["sample_n"] or 0)
            key = (r["type_code"], r["asset"])
            sub = agg.get(key, {})
            if sample_n < min_sample:
                # 样本不足也写,但 avg_pnl_4h / win_rate 用 None 表示不可信(用 0 代替即可)
                avg_pnl = 0.0
            else:
                avg_pnl = float(r["avg_pnl"]) if r["avg_pnl"] is not None else 0.0
            # P2-12: 1h/24h 窗口也写入新列（先尝试 ALTER，再 INSERT）。
            # 兼容旧库：先 ensure columns 存在（idempotent）
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(backtest_event_outcomes)").fetchall()}
                for col, typ in (
                    ("avg_pnl_1h",  "REAL DEFAULT 0.0"),
                    ("avg_pnl_24h", "REAL DEFAULT 0.0"),
                    ("win_rate_1h", "REAL DEFAULT 0.0"),
                    ("win_rate_4h", "REAL DEFAULT 0.0"),
                    ("win_rate_24h","REAL DEFAULT 0.0"),
                    ("sample_1h",   "INTEGER DEFAULT 0"),
                    ("sample_24h",  "INTEGER DEFAULT 0"),
                ):
                    if col not in cols:
                        try:
                            conn.execute(f"ALTER TABLE backtest_event_outcomes ADD COLUMN {col} {typ}")
                        except sqlite3.Error:
                            pass
            except sqlite3.Error:
                pass
            def _safe_avg(key_):
                v = sub.get(key_)
                return float(v) if v is not None else 0.0
            def _safe_wr(key_):
                w = sub.get(key_ + "_win") or sub.get("win_n") or 0
                s = sub.get(key_ + "_sample") or sub.get("sample_n") or 0
                if not s:
                    return 0.0
                try:
                    return float(w) / float(s)
                except Exception:
                    return 0.0
            conn.execute(
                """
                INSERT INTO backtest_event_outcomes
                  (type_code, asset, sample_n, win_n, loss_n, avg_pnl_4h, last_updated,
                   avg_pnl_1h, avg_pnl_24h, win_rate_1h, win_rate_4h, win_rate_24h,
                   sample_1h, sample_24h)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(type_code, asset) DO UPDATE SET
                  sample_n = excluded.sample_n,
                  win_n    = excluded.win_n,
                  loss_n   = excluded.loss_n,
                  avg_pnl_4h = excluded.avg_pnl_4h,
                  avg_pnl_1h = excluded.avg_pnl_1h,
                  avg_pnl_24h = excluded.avg_pnl_24h,
                  win_rate_1h = excluded.win_rate_1h,
                  win_rate_4h = excluded.win_rate_4h,
                  win_rate_24h = excluded.win_rate_24h,
                  sample_1h = excluded.sample_1h,
                  sample_24h = excluded.sample_24h,
                  last_updated = excluded.last_updated
                """,
                (
                    str(r["type_code"] or ""),
                    str(r["asset"] or ""),
                    sample_n,
                    int(r["win_n"] or 0),
                    int(r["loss_n"] or 0),
                    avg_pnl,
                    now,
                    _safe_avg("1h_avg_pnl"),
                    _safe_avg("24h_avg_pnl"),
                    _safe_wr("1h"),
                    _safe_wr("4h"),
                    _safe_wr("24h"),
                    int(sub.get("1h_sample") or 0),
                    int(sub.get("24h_sample") or 0),
                ),
            )
            written += 1
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        # v176.5: 恢复调用方的 row_factory, 避免污染外部连接
        try:
            conn.row_factory = _orig_row_factory
        except Exception:
            pass
    return written

_INDEXES = [
    # v3xx 修复(v300-audit): asset_driver_nodes.updated_at 原来没有索引，
    # MAX(updated_at) 只能全表扫描；该表存在单行 800MB+ 的 evidence blob，
    # 一次扫描要读几百 MB → /api/analysis/kg-stats 与
    # /api/review/causal-graph/stats 稳定超时 60s+。加索引后 MAX 走索引末端，O(log n)。
    "CREATE INDEX IF NOT EXISTS idx_adn_updated_at ON asset_driver_nodes(updated_at);",
    "CREATE INDEX IF NOT EXISTS idx_ai_decisions_status ON ai_decisions(status);",
    "CREATE INDEX IF NOT EXISTS idx_raw_news_status ON raw_news(status);",
    "CREATE INDEX IF NOT EXISTS idx_ai_parent ON ai_decisions(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_ai_agg_key ON ai_decisions(aggregation_key);",
    "CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_ts ON market_ticks(symbol, ts);",
    "CREATE INDEX IF NOT EXISTS idx_factor_snapshots_asset_ts ON factor_snapshots(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_factor_snapshots_news ON factor_snapshots(news_id);",
    "CREATE INDEX IF NOT EXISTS idx_signal_perf_asset_ts ON signal_performance(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_strategy_versions_strategy ON strategy_versions(strategy_id, version);",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_ai_strategy_version ON ai_decisions(strategy_version_id);",
    "CREATE INDEX IF NOT EXISTS idx_ai_paper_run ON ai_decisions(paper_trading_run_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_runs_status ON paper_trading_runs(status, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_quick_sim_settled ON quick_sim_trades(settled, entry_ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_ts ON hermes_observations(ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_asset ON hermes_observations(asset, ts);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_obs_news ON hermes_observations(news_id);",
    "CREATE INDEX IF NOT EXISTS idx_hermes_skills_asset ON hermes_skills(asset, action);",
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_ts ON macro_snapshots(ts);",
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_key ON macro_snapshots(category, metric_key, ts);",
    # 事件结构化索引（PR-3）— 按时间/类型/资产/新闻的查询是热点路径
    "CREATE INDEX IF NOT EXISTS idx_event_atom_time ON event_atom(event_time);",
    "CREATE INDEX IF NOT EXISTS idx_event_atom_type_time ON event_atom(event_type_id, event_time);",
    "CREATE INDEX IF NOT EXISTS idx_event_atom_asset_time ON event_atom(asset_id, event_time);",
    "CREATE INDEX IF NOT EXISTS idx_event_atom_news ON event_atom(raw_news_id);",
    "CREATE INDEX IF NOT EXISTS idx_event_time_cand_news ON event_time_candidates(raw_news_id);",
    # 事件影响曲线索引（v171）
    "CREATE INDEX IF NOT EXISTS idx_impact_curve_atom ON event_impact_curve(event_atom_id);",
    "CREATE INDEX IF NOT EXISTS idx_impact_curve_asset_ts ON event_impact_curve(asset_id, sample_ts);",
    "CREATE INDEX IF NOT EXISTS idx_impact_curve_label ON event_impact_curve(window_label, sample_ts);",
    # event_atom 增量列索引（v171 UTC 时间双轨）
    "CREATE INDEX IF NOT EXISTS idx_event_atom_utc_epoch ON event_atom(event_time_utc_epoch);",
    # event_impact_curve 范围查询索引（归档用）
    "CREATE INDEX IF NOT EXISTS idx_impact_curve_sample_ts ON event_impact_curve(sample_ts);",
    # event_impact_stat 日期范围查询
    "CREATE INDEX IF NOT EXISTS idx_event_impact_stat_local ON event_impact_stat(date_local);",
    "CREATE INDEX IF NOT EXISTS idx_beo_type_asset ON backtest_event_outcomes(type_code, asset);",
    # v173: 结构化校验真实性飞轮 — 按 (status, asset, event_type_code, verdict) 分组聚合
    # v177: 反思系统 — decision_errors 表（程序化校验 + LLM 反思共用错误码存储）
    # 每个 ai_decision 可对应多条 decision_errors（一笔决策可触发多个错误码）
    # origin: 'checker'=程序化校验器产出 / 'llm'=LLM 反思产出
    # severity: 1=info(可忽略) / 2=warn(下次留意) / 3=critical(必须改)
    "CREATE TABLE IF NOT EXISTS decision_errors ("
    "    id              INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    decision_id     INTEGER NOT NULL,"
    "    error_code      TEXT    NOT NULL,"
    "    origin          TEXT    NOT NULL DEFAULT 'checker',"
    "    severity        INTEGER NOT NULL DEFAULT 2,"
    "    detail          TEXT    NOT NULL DEFAULT '',"
    "    evidence_refs   TEXT    NOT NULL DEFAULT '{}',"
    "    source          TEXT    NOT NULL DEFAULT '',"
    "    weight          INTEGER NOT NULL DEFAULT 0,"  # v189 Item 8: 层权重 = severity*10 + layer_weight
    "    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),"
    "    UNIQUE(decision_id, error_code, origin, source)"
    ");",
    # v189 Item 8: 跨层排序权重索引（排序热路径）
    "CREATE INDEX IF NOT EXISTS idx_dec_errors_weight ON decision_errors(weight DESC, decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_dec_errors_decision ON decision_errors(decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_dec_errors_code ON decision_errors(error_code);",
    "CREATE INDEX IF NOT EXISTS idx_struct_ref_status ON structured_reflections(status, asset, event_type_code, verdict);",
    "CREATE INDEX IF NOT EXISTS idx_struct_ref_decision ON structured_reflections(decision_id);",
    # v176.2 (item 15): event_atom ↔ ai_decisions 双向索引
    # ai_decisions.event_atom_ids 是 JSON array (e.g. "[372,373]"), 单新闻多 atom 场景
    # 反查索引用 LIKE '%id%' 兜底 (高频路径以 news_id JOIN event_atom 为主, 这里只补决策维度)
    "CREATE INDEX IF NOT EXISTS idx_ai_decisions_atom_ids ON ai_decisions(event_atom_ids);",
    "CREATE INDEX IF NOT EXISTS idx_ai_decisions_news_atom ON ai_decisions(news_id);",

    # v187: 多通道 LLM token 用量监控表
    # 每条 LLM HTTP 调用一行(成功+失败都记)。purpose=main/gate/anomaly/reflection/advisor/sql/optimizer
    # key_alias=minimax1/minimax2/minimax-gate/minimax-anomaly/minimax-reflect/local/...
    # model_id 完整 rostry id (e.g. minimax-gate/MiniMax-M2.7-highspeed)
    # tokens_in/tokens_out/cost_cny: prompt/completion 实际 token,折算人民币成本(按模型定价估算)
    # latency_ms: 单次 HTTP 耗时
    # status: ok / http_error / network_error / timeout / parse_error
    "CREATE TABLE IF NOT EXISTS ai_call_stats ("
    "    id           INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    ts           INTEGER NOT NULL,"
    "    purpose      TEXT    NOT NULL,"
    "    key_alias    TEXT    NOT NULL,"
    "    model_id     TEXT    NOT NULL,"
    "    tokens_in    INTEGER NOT NULL DEFAULT 0,"
    "    tokens_out   INTEGER NOT NULL DEFAULT 0,"
    "    latency_ms   INTEGER NOT NULL DEFAULT 0,"
    "    status       TEXT    NOT NULL DEFAULT 'ok',"
    "    error_code   TEXT    NOT NULL DEFAULT '',"
    "    call_site    TEXT    NOT NULL DEFAULT ''"
    ");",
    "CREATE INDEX IF NOT EXISTS idx_ai_call_stats_ts ON ai_call_stats(ts);",
    "CREATE INDEX IF NOT EXISTS idx_ai_call_stats_purpose ON ai_call_stats(purpose, ts);",
    "CREATE INDEX IF NOT EXISTS idx_ai_call_stats_key ON ai_call_stats(key_alias, ts);",
    "CREATE INDEX IF NOT EXISTS idx_ai_call_stats_site ON ai_call_stats(call_site, ts);",
]


# ---------------------------------------------------------------------------
# Time-series tables — local persistent history
# ---------------------------------------------------------------------------
# All three use an INTEGER epoch-second `ts` as the time axis plus a symbol/asset
# partition key, so range scans and downsampling run off one composite index.

_CREATE_MARKET_TICKS = """
    CREATE TABLE IF NOT EXISTS market_ticks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol       TEXT    NOT NULL,
        ts           INTEGER NOT NULL,
        price        REAL    NOT NULL,
        change24h    REAL    NOT NULL DEFAULT 0.0,
        source       TEXT    NOT NULL DEFAULT 'median',
        source_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE (symbol, ts)
    );
"""

_CREATE_FACTOR_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS factor_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id       INTEGER NOT NULL,
        asset         TEXT    NOT NULL DEFAULT 'NONE',
        ts            INTEGER NOT NULL,
        final_score   REAL    NOT NULL DEFAULT 0.0,
        confidence    REAL    NOT NULL DEFAULT 0.0,
        raw_action    TEXT    NOT NULL DEFAULT 'HOLD',
        gated_action  TEXT    NOT NULL DEFAULT 'HOLD',
        p_value       REAL    NOT NULL DEFAULT 1.0,
        chi_square    REAL    NOT NULL DEFAULT 0.0,
        sample_size   INTEGER NOT NULL DEFAULT 0,
        contradictions INTEGER NOT NULL DEFAULT 0,
        factors       TEXT    NOT NULL DEFAULT '{}',
        evidence      TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_NEWS_EVENTS = """
    CREATE TABLE IF NOT EXISTS news_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id     INTEGER NOT NULL,
        decision_id INTEGER,
        ts          INTEGER NOT NULL,
        source      TEXT    NOT NULL DEFAULT '',
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        score       REAL    NOT NULL DEFAULT 0.0,
        is_noise    INTEGER NOT NULL DEFAULT 0,
        status      TEXT    NOT NULL DEFAULT 'PENDING',
        UNIQUE (news_id, ts)
    );
"""

_CREATE_SIGNAL_PERFORMANCE = """
    CREATE TABLE IF NOT EXISTS signal_performance (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL,
        asset       TEXT    NOT NULL DEFAULT 'NONE',
        ts          INTEGER NOT NULL,
        action      TEXT    NOT NULL DEFAULT 'HOLD',
        is_correct  TEXT    NOT NULL DEFAULT '',
        forward_pnl REAL,
        mfe_pct     REAL,
        mae_pct     REAL,
        UNIQUE (decision_id, ts)
    );
"""

_CREATE_STRATEGIES = """
    CREATE TABLE IF NOT EXISTS strategies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slug        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
        is_current  INTEGER NOT NULL DEFAULT 0,
        current_version_id INTEGER DEFAULT NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_STRATEGIES_COLUMNS: List[Tuple[str, str]] = [
    ("is_current", "INTEGER NOT NULL DEFAULT 0"),
    ("current_version_id", "INTEGER DEFAULT NULL"),
]

_CREATE_STRATEGY_VERSIONS = """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        version     INTEGER NOT NULL,
        params      TEXT    NOT NULL DEFAULT '{}',
        source      TEXT    NOT NULL DEFAULT 'manual'
            CHECK (source IN ('manual', 'seed', 'llm', 'backtest', 'reflection')),
        note        TEXT    NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (strategy_id, version),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
    );
"""

_CREATE_PAPER_TRADING_SETTINGS = """
    CREATE TABLE IF NOT EXISTS paper_trading_settings (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        is_running  INTEGER NOT NULL DEFAULT 1,  -- 默认开启模拟盘 (v172+)
        tracks      TEXT    NOT NULL DEFAULT '[]',
        started_at  TEXT,
        active_run_id INTEGER DEFAULT NULL,
        gate_enabled INTEGER NOT NULL DEFAULT 1,
        news_enabled INTEGER NOT NULL DEFAULT 1,
        news_daily_target INTEGER NOT NULL DEFAULT 300,
        news_sources TEXT NOT NULL DEFAULT '{"financialjuice":true,"tree_news":true,"techflow":true,"eastmoney":true,"blockbeats":true,"jin10":true,"fred":true,"finnhub":true,"feishu_jin10":true}',
        news_trade_max_age_seconds INTEGER NOT NULL DEFAULT 2700,
        tp_enabled INTEGER NOT NULL DEFAULT 1,
        tp1_mult REAL NOT NULL DEFAULT 0.6,
        tp2_mult REAL NOT NULL DEFAULT 1.0,
        tp1_ratio REAL NOT NULL DEFAULT 0.5,
        tp2_ratio REAL NOT NULL DEFAULT 0.3,
        sl_mult REAL NOT NULL DEFAULT 0.5,
        buy_min_score REAL NOT NULL DEFAULT 0.4,
        sell_min_score REAL NOT NULL DEFAULT 0.3,
        market_structure_enabled INTEGER NOT NULL DEFAULT 1,
        news_export_settings TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_PAPER_SETTINGS_COLUMNS: List[Tuple[str, str]] = [
    ("active_run_id", "INTEGER DEFAULT NULL"),
    ("gate_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("news_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("news_daily_target", "INTEGER NOT NULL DEFAULT 300"),
    ("news_sources", "TEXT NOT NULL DEFAULT '{\"financialjuice\":true,\"tree_news\":true,\"techflow\":true,\"eastmoney\":true,\"blockbeats\":true,\"jin10\":true,\"fred\":true,\"finnhub\":true,\"feishu_jin10\":true}'"),
    ("news_trade_max_age_seconds", "INTEGER NOT NULL DEFAULT 2700"),
    ("tp_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("tp1_mult", "REAL NOT NULL DEFAULT 0.6"),
    ("tp2_mult", "REAL NOT NULL DEFAULT 1.0"),
    ("tp1_ratio", "REAL NOT NULL DEFAULT 0.5"),
    ("tp2_ratio", "REAL NOT NULL DEFAULT 0.3"),
    ("sl_mult", "REAL NOT NULL DEFAULT 0.5"),
    ("buy_min_score", "REAL NOT NULL DEFAULT 0.4"),
    ("sell_min_score", "REAL NOT NULL DEFAULT 0.3"),
    ("quick_sim_reset_at", "TEXT DEFAULT NULL"),
    ("market_structure_enabled", "INTEGER NOT NULL DEFAULT 1"),
    # 新闻数据整理参数（JSON）：{"interval_minutes":30,"window_hours":24,"keep_batches":30}
    ("news_export_settings", "TEXT NOT NULL DEFAULT '{}'"),
]

_CREATE_PAPER_TRADING_RUNS = """
    CREATE TABLE IF NOT EXISTS paper_trading_runs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        tracks              TEXT    NOT NULL DEFAULT '[]',
        strategy_id         INTEGER NOT NULL,
        strategy_version_id INTEGER NOT NULL,
        agent_version       TEXT    NOT NULL,
        model_id            TEXT    NOT NULL,
        spec_version        TEXT    NOT NULL,
        activation_reason   TEXT    NOT NULL DEFAULT '',
        status              TEXT    NOT NULL DEFAULT 'RUNNING'
            CHECK (status IN ('RUNNING', 'STOPPED')),
        started_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        stopped_at          TEXT,
        FOREIGN KEY (strategy_id) REFERENCES strategies(id),
        FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions(id)
    );
"""

_CREATE_QUICK_SIM_TRADES = """
    CREATE TABLE IF NOT EXISTS quick_sim_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id         INTEGER NOT NULL UNIQUE,
        news_id             INTEGER,
        asset               TEXT    NOT NULL,
        action              TEXT    NOT NULL CHECK (action IN ('BUY', 'SELL')),
        score               REAL,
        evidence_confidence REAL,
        gate_passed         INTEGER NOT NULL DEFAULT 0,
        gate_reason         TEXT    NOT NULL DEFAULT '',
        strategy_id         INTEGER,
        strategy_version_id INTEGER,
        paper_trading_run_id INTEGER,
        notional_usdt       REAL    NOT NULL DEFAULT 0.0,
        entry_price         REAL    NOT NULL,
        entry_ts            INTEGER NOT NULL,
        horizon_minutes     INTEGER NOT NULL,
        exit_price          REAL,
        max_price           REAL,   -- 持仓期间最高价（过程质量，归因用事实反证）
        min_price           REAL,   -- 持仓期间最低价（过程质量，归因用事实反证）
        sl_price            REAL DEFAULT NULL,  -- v230+ P0.2 part B: TP1 后的追踪止损（breakeven-move）；NULL=沿用 entry×(1-sl_mult) 默认
        exit_ts             INTEGER,
        pnl_pct             REAL,
        pnl_usdt            REAL,
        verdict             TEXT    NOT NULL DEFAULT '',
        settled             INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        mfe_pct             REAL    DEFAULT NULL,  -- 持仓期间最大浮盈（用于归因分析）
        mae_pct             REAL    DEFAULT NULL,  -- 持仓期间最大浮亏（用于归因分析）
        mfe_time_mins       REAL    DEFAULT NULL,  -- 到达 MFE 的耗时（分钟）—— 用于判定入场是否太早/太晚
        max_price_time      INTEGER DEFAULT 0,
        min_price_time      INTEGER DEFAULT 0,
        cost_bps            REAL    DEFAULT 0.0,   -- v301+ P0+: 本笔结算时扣减的交易成本（bps）
        FOREIGN KEY (decision_id) REFERENCES ai_decisions(id) ON DELETE CASCADE
    );
"""

_QUICK_SIM_TRADES_COLUMNS: List[Tuple[str, str]] = [
    ("max_price", "REAL"),
    ("min_price", "REAL"),
    ("mfe_pct", "REAL DEFAULT NULL"),
    ("mae_pct", "REAL DEFAULT NULL"),
    # v200.x: api_server.py 6582 / 6781 / engine/reflection.py 118 都在 SELECT qs.mfe_time_mins。
    # 老库 ALTER 后才能跑，否则 /api/replay/positions 直接 500 → 模拟页 K 线空白。
    ("mfe_time_mins", "REAL DEFAULT NULL"),
    # v200.1: 持仓期间最高/最低价的时间戳（用于 forward-test MFE/MAE 时刻定位）
    # _EVENT_SELECT 里 qs.max_price_time/min_price_time 需要这两列，否则查询 OperationalError 被吞 → /api/events 返回 []
    ("max_price_time", "INTEGER DEFAULT 0"),
    ("min_price_time", "INTEGER DEFAULT 0"),
    # v230+ P0.2 part B: TP1 后的追踪止损（breakeven-move）。NULL = 沿用 entry×(1-sl_mult) 默认；TP1 命中后写入 entry*(1±offset)
    ("sl_price", "REAL DEFAULT NULL"),
    # v301+ P0+: 显式成本记账。老行默认 0 = "未扣成本"，下游审计可一眼看出哪批是 v300 之前的快照
    ("cost_bps", "REAL DEFAULT 0.0"),
]

_CREATE_HERMES_OBSERVATIONS = """
    CREATE TABLE IF NOT EXISTS hermes_observations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id          INTEGER,
        decision_id      INTEGER,
        ts               INTEGER NOT NULL,
        source           TEXT    NOT NULL DEFAULT '',
        asset            TEXT    NOT NULL DEFAULT 'NONE',
        observation_type TEXT    NOT NULL DEFAULT 'news',
        content          TEXT    NOT NULL DEFAULT '',
        payload          TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_HERMES_SKILLS = """
    CREATE TABLE IF NOT EXISTS hermes_skills (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_key        TEXT    NOT NULL UNIQUE,
        asset            TEXT    NOT NULL,
        action           TEXT    NOT NULL,
        prediction_type  TEXT    NOT NULL DEFAULT '',
        sample_size      INTEGER NOT NULL DEFAULT 0,
        wins             INTEGER NOT NULL DEFAULT 0,
        losses           INTEGER NOT NULL DEFAULT 0,
        win_rate         REAL,
        avg_pnl          REAL,
        note             TEXT    NOT NULL DEFAULT '',
        updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_MACRO_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS macro_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         INTEGER NOT NULL,
        category   TEXT    NOT NULL,
        source     TEXT    NOT NULL DEFAULT '',
        metric_key TEXT    NOT NULL,
        value      REAL,
        unit       TEXT    NOT NULL DEFAULT '',
        status     TEXT    NOT NULL DEFAULT 'unavailable',
        payload    TEXT    NOT NULL DEFAULT '{}'
    );
"""

_CREATE_KLINE_BARS = """
    CREATE TABLE IF NOT EXISTS kline_bars (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT    NOT NULL,
        timeframe  TEXT    NOT NULL,               -- '1d' | '4h'（与 market_snapshot._MTF_PERIODS 对齐）
        ts         INTEGER NOT NULL,               -- 开盘时间 epoch ms
        open       REAL,
        high       REAL,
        low        REAL,
        close      REAL,
        volume     REAL,
        fetched_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(symbol, timeframe, ts)
    );
"""

_CREATE_KLINE_SNAPSHOTS = """
    CREATE TABLE IF NOT EXISTS kline_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        timeframe     TEXT NOT NULL,
        close         REAL,
        pos_pct_7d    REAL,                        -- 现价在近N日区间位置 0~1（<0.2低位/>0.8高位/0.4~0.6中性）
        range_high_7d REAL,
        range_low_7d  REAL,
        atr_pct       REAL,
        trend         TEXT,                        -- 趋势（复用 _compute_period_stats 判定）
        status        TEXT NOT NULL DEFAULT 'unavailable',  -- ok | unavailable
        payload       TEXT NOT NULL DEFAULT '{}',
        ts            INTEGER NOT NULL,
        UNIQUE(symbol, timeframe)
    );
"""

# v176.2：市场风格持久化（_compute_market_style 之前只输出不存，前端拿不到历史）
# 写入方：engine 每 5 分钟调用一次 _snapshot_market_style，存当前 (asset, regime, layer1..5_alerts)
# 读出方：ai_decisions INSERT 时给 regime_at_decision 字段赋值；前端 /api/market/regime/history 端点拉历史
_CREATE_MARKET_STYLE_SNAPSHOT = """
    CREATE TABLE IF NOT EXISTS market_style_snapshot (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL DEFAULT '',      -- XAU/WTI/BTC/...
        regime          TEXT    NOT NULL DEFAULT 'unknown',  -- trending_up / trending_down / range_bound / volatile / crisis
        layer1_alerts   TEXT    NOT NULL DEFAULT '',      -- 价格结构 alert (JSON array)
        layer2_alerts   TEXT    NOT NULL DEFAULT '',      -- 资金流 alert
        layer3_alerts   TEXT    NOT NULL DEFAULT '',      -- 持仓结构 alert
        layer4_alerts   TEXT    NOT NULL DEFAULT '',      -- 宏观/利率 alert
        layer5_alerts   TEXT    NOT NULL DEFAULT '',      -- 地缘/事件 alert
        style_block     TEXT    NOT NULL DEFAULT '',      -- market_style_block 输出（注入 prompt 的整块）
        trigger         TEXT    NOT NULL DEFAULT '',      -- 本次变化的触发原因（"BTC 1h 跌穿 50EMA" 等）
        ts              INTEGER NOT NULL
    );
"""

# v176.2：决策反思表（"上次错哪/这次要怎么调整"）
# 写入方：engine 每 N 条决策 / 每小时触发一次 _reflect_recent_decisions，写入本表
# 读出方：ai_decisions INSERT 时给 bias_after_reflection 字段赋值；engine 把反思注入到下次同品种 prompt
# 设计：bias_text ≤200 字，L1-L2 prompt 末尾追加一段，反思条目本身是 self-loop 不是规则
_CREATE_DECISION_REFLECTION = """
    CREATE TABLE IF NOT EXISTS decision_reflection (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL DEFAULT '',      -- XAU/WTI/BTC/...
        window          TEXT    NOT NULL DEFAULT '1h',    -- 1h / 4h / 1d
        n_decisions     INTEGER NOT NULL DEFAULT 0,       -- 本窗口观察的决策条数
        win_n           INTEGER NOT NULL DEFAULT 0,       -- 真实"对"数（事后 K 线方向与决策一致）
        loss_n          INTEGER NOT NULL DEFAULT 0,
        flat_n          INTEGER NOT NULL DEFAULT 0,
        avg_pnl_pct     REAL    NOT NULL DEFAULT 0.0,
        bias_text       TEXT    NOT NULL DEFAULT '',      -- 反思结论（例：XAU 1h 窗口连 3 次 BUY 都错，下次起降一档）
        trigger         TEXT    NOT NULL DEFAULT '',      -- 触发原因（"3 连亏" / "胜率 < 30%" / "连续反向"）
        ts              INTEGER NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    );
"""

_CREATE_BACKTEST_RUNS = """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id    INTEGER NOT NULL,
        version_id     INTEGER NOT NULL,
        params         TEXT    NOT NULL DEFAULT '{}',
        result         TEXT    NOT NULL DEFAULT '{}',
        sample_size    INTEGER NOT NULL DEFAULT 0,
        taken          INTEGER NOT NULL DEFAULT 0,
        winrate        REAL,
        total_pnl_usdt REAL,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
        FOREIGN KEY (version_id) REFERENCES strategy_versions(id) ON DELETE CASCADE
    );
"""

_CREATE_REVIEW_REPORTS = """
    CREATE TABLE IF NOT EXISTS review_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scope       TEXT    NOT NULL,              -- today | week | all
        generated_at TEXT   NOT NULL DEFAULT (datetime('now')),
        summary     TEXT    NOT NULL DEFAULT '{}', -- 复盘总结 JSON
        failures    TEXT    NOT NULL DEFAULT '[]', -- 失败原因归类 JSON
        insights    TEXT    NOT NULL DEFAULT '[]', -- 沉淀的经验规则 JSON
        samples     INTEGER NOT NULL DEFAULT 0     -- 覆盖的交易笔数
    );
"""

_CREATE_REVIEW_INSIGHTS = """
    CREATE TABLE IF NOT EXISTS review_insights (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        rule       TEXT    NOT NULL,               -- 经验规则文本（注入 AI prompt）
        asset      TEXT    DEFAULT '',
        scope      TEXT    DEFAULT 'all',
        active     INTEGER NOT NULL DEFAULT 1,     -- 1=生效回灌 / 0=停用
        source_samples INTEGER DEFAULT 0,          -- 产生该经验时的复盘样本数（可靠性代理）
        origin     TEXT    DEFAULT 'real',         -- 来源：real=历史交易复盘 / quick_sim=快速评测提炼 / rule=归因规则
        kind            INTEGER NOT NULL DEFAULT 1, -- 0=硬铁律(强制，不参与胜率停用) / 1=软启发(可验证)
        counter_factual TEXT    DEFAULT '',          -- 反例：什么条件下该经验失效
        created_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

_REVIEW_INSIGHTS_COLUMNS = [
    ("source_samples", "INTEGER DEFAULT 0"),
    ("origin", "TEXT DEFAULT 'real'"),
    # 1 = 人工在界面上停用的。自动自愈（"已停用但无命中样本"回滚）必须跳过这类，
    # 否则用户手动停用的经验会在下次重启时被"复活"（实测踩过）。
    ("manual_deactivated", "INTEGER NOT NULL DEFAULT 0"),
    ("kind", "INTEGER NOT NULL DEFAULT 1"),
    ("counter_factual", "TEXT DEFAULT ''"),
    # R5 路径 A：insight 事后因果验证记录（insight_verifier 计算 → apply_verification 写入）。
    # 铁律1：schema 变更一律在此定义，经 db.migrate() → _ensure_columns 幂等补齐。
    ("verification_score", "REAL"),
    ("verification_verdict", "TEXT DEFAULT ''"),
    ("verification_method", "TEXT DEFAULT ''"),
    ("verification_sample_n", "INTEGER DEFAULT 0"),
    ("verification_kline_n", "INTEGER DEFAULT 0"),
    ("verified_at", "TEXT DEFAULT ''"),
]

# v173：结构化校验真实性飞轮 — 监听 ai_decisions.structured_check verdict，
# 当同一 (asset, event_type_code, verdict) 累计 ≥ N 笔时 → 自动沉淀为 review_insights，
# 让 review_agent._evaluate_insights 复用现有"停用/复活/自愈"逻辑。
_CREATE_STRUCTURED_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS structured_reflections (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        news_id         INTEGER,
        asset           TEXT    NOT NULL DEFAULT '',
        event_type_code TEXT    NOT NULL DEFAULT '',
        verdict         TEXT    NOT NULL DEFAULT '',   -- fabricated / partial / ok
        score_coverage  REAL    NOT NULL DEFAULT 0.0,
        score_fabrication REAL  NOT NULL DEFAULT 0.0,
        entities_fabricated TEXT NOT NULL DEFAULT '[]', -- JSON list
        events_missing  TEXT    NOT NULL DEFAULT '[]',  -- JSON list
        status          TEXT    NOT NULL DEFAULT 'pending',  -- pending / promoted / failed / skipped
        retry_count     INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT    NOT NULL DEFAULT '',
        promoted_insight_id INTEGER,                  -- review_insights.id when promoted
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(decision_id)                          -- 一个 decision 只入一次
    );
"""

# 历史新闻回溯：单条新闻的交易 AI 归因总结缓存（避免重复调用 LLM）
_CREATE_NEWS_TRADE_REVIEWS = """
    CREATE TABLE IF NOT EXISTS news_trade_reviews (
        news_id      INTEGER PRIMARY KEY,            -- 关联 raw_news.id
        summary      TEXT    NOT NULL DEFAULT '{}',  -- AI 总结 JSON（含成功/失败原因）
        generated_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
"""

# 归因复核队列：逐条归因明细 + 人工确认/纠错状态，纠错自动回灌为经验规则
_CREATE_ATTRIBUTION_REVIEWS = """
    CREATE TABLE IF NOT EXISTS attribution_reviews (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source         TEXT    NOT NULL,             -- news=新闻回溯归因 / review=复盘归因
        ref_id         INTEGER NOT NULL,             -- news_id 或 review_reports.id
        trade_id       INTEGER,                      -- quick_sim_trades.id 或 ai_decisions.id
        asset          TEXT    DEFAULT '',
        action         TEXT    DEFAULT '',
        verdict        TEXT    DEFAULT '',           -- WIN/LOSS
        pnl_pct        REAL,
        reason_code    INTEGER,
        reason_text    TEXT    DEFAULT '',
        confidence     REAL,
        human_status   TEXT    NOT NULL DEFAULT 'pending',  -- pending/agree/corrected
        corrected_code INTEGER,
        corrected_text TEXT    DEFAULT '',
        core_impact    TEXT    DEFAULT '',           -- 门卫四步核心影响（这条筋）
        reasons_json   TEXT    DEFAULT '',           -- 完整多原因归因数组(JSON)
        note           TEXT    DEFAULT '',
        rule_pushed    INTEGER NOT NULL DEFAULT 0,   -- 是否已回灌为 review_insights
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        reviewed_at    TEXT
    );
"""

# 提示词库：第 1 层（采集入库噪声过滤）的关键词，人工可增删（内置词可停用）
_CREATE_FILTER_KEYWORDS = """
    CREATE TABLE IF NOT EXISTS filter_keywords (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT    NOT NULL CHECK (kind IN ('junk', 'core')),
        keyword    TEXT    NOT NULL,
        builtin    INTEGER NOT NULL DEFAULT 0,   -- 1=内置种子（展示可停用）
        active     INTEGER NOT NULL DEFAULT 1,   -- 0=已停用（软删除）
        note       TEXT    NOT NULL DEFAULT '',
        created_at TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (kind, keyword)
    );
"""

# 单笔反思：每笔模拟交易结算后的即时归因（LLM），供复盘聚合与经验沉淀
_CREATE_TRADE_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS trade_reflections (
        decision_id   INTEGER PRIMARY KEY,       -- 关联 ai_decisions.id
        news_id       INTEGER,
        asset         TEXT    NOT NULL DEFAULT '',
        action        TEXT    NOT NULL DEFAULT '',
        verdict       TEXT    NOT NULL DEFAULT '',
        pnl_pct       REAL,
        reason_code   INTEGER,
        reason_name   TEXT    NOT NULL DEFAULT '',
        reason_text   TEXT    NOT NULL DEFAULT '',
        confidence    REAL,
        status        TEXT    NOT NULL DEFAULT 'pending',  -- pending | done | failed
        created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        reflected_at  TEXT,
        retry_count   INTEGER NOT NULL DEFAULT 0,    -- LLM 失败重试次数（避免瞬时网络抖动永久丢失反思）
        last_error    TEXT    NOT NULL DEFAULT '',   -- 最近一次 LLM 失败原因
        last_error_at TEXT,                           -- 最近一次失败时间（用于限速重试）
        entry_accuracy REAL,                          -- v175: 买点识别准确性 0-1（反思评估维度）
        trend_fit      REAL                           -- v175: K线走势吻合度 0-1（反思评估维度）
    );
"""

# ══════════════════════════════════════════════════════════════════
# v188 反思系统增强 —— 4 张缺失表 DDL + decision_layer_signals 聚合表
# 之前 workers 读写 experience_entries / experience_applications /
# gate_reflections_cycle / trade_reflections_v2 4 张表都假定存在，但
# DDL 不在 migrate() 里 —— 新建/迁移过的库会 OperationalError。本补丁
# 全部补上 + 新增 decision_layer_signals / reverse_attributions 颗粒度统计基础表。
# ══════════════════════════════════════════════════════════════════

_CREATE_EXPERIENCE_ENTRIES = """
    CREATE TABLE IF NOT EXISTS experience_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        applicable_assets TEXT DEFAULT '',
        applicable_kinds TEXT DEFAULT '',
        applicable_actors TEXT DEFAULT '',
        weight REAL DEFAULT 1.0,
        priority INTEGER DEFAULT 50,
        source TEXT DEFAULT 'manual',
        status TEXT DEFAULT 'active',
        apply_count INTEGER DEFAULT 0,
        hit_count INTEGER DEFAULT 0,
        last_applied_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
"""

_CREATE_EXPERIENCE_APPLICATIONS = """
    CREATE TABLE IF NOT EXISTS experience_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experience_id INTEGER,
        decision_id INTEGER,
        applied_action TEXT,
        applied_at TEXT DEFAULT (datetime('now')),
        llm_invoked INTEGER DEFAULT 0,
        UNIQUE(experience_id, decision_id, applied_action)
    );
"""

_CREATE_GATE_REFLECTIONS_CYCLE = """
    CREATE TABLE IF NOT EXISTS gate_reflections_cycle (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        cycle_ts INTEGER NOT NULL,
        cycle_iso TEXT DEFAULT '',
        days INTEGER DEFAULT 3,
        precision REAL,
        pass_winrate REAL,
        n_judged INTEGER DEFAULT 0,
        n_decided INTEGER DEFAULT 0,
        n_win INTEGER DEFAULT 0,
        n_loss INTEGER DEFAULT 0,
        n_abnormal INTEGER DEFAULT 0,
        n_covered INTEGER DEFAULT 0,
        n_miss INTEGER DEFAULT 0,
        recall REAL,
        forward_json TEXT DEFAULT '[]',
        reverse_json TEXT DEFAULT '[]',
        summary_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(asset, cycle_ts)
    );
"""

_CREATE_TRADE_REFLECTIONS_V2 = """
    CREATE TABLE IF NOT EXISTS trade_reflections_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER UNIQUE,
        paper_run_id INTEGER,
        asset TEXT,
        action TEXT,
        entry_price REAL,
        exit_price REAL,
        pnl_pct REAL,
        mfe_pct REAL,
        mae_pct REAL,
        settled_at TEXT,
        analysis_layer_scores TEXT,
        filter_layer_chain TEXT,
        reasoning_chain TEXT,
        counterfactual TEXT,
        error_layer TEXT,
        error_sub_layer TEXT,
        error_signal TEXT,
        root_cause TEXT,
        lesson TEXT,
        verification TEXT,
        applied_experience_ids TEXT DEFAULT '',
        depth_score INTEGER DEFAULT 0,
        llm_depth_reflection TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
"""

_CREATE_DECISION_LAYER_SIGNALS = """
    CREATE TABLE IF NOT EXISTS decision_layer_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        layer TEXT NOT NULL,
        sub_layer TEXT NOT NULL,
        signal_name TEXT NOT NULL,
        asset TEXT DEFAULT '*',
        action TEXT DEFAULT '*',
        n_total INTEGER DEFAULT 0,
        n_loss INTEGER DEFAULT 0,
        n_win INTEGER DEFAULT 0,
        last_seen_ts INTEGER DEFAULT 0,
        last_decision_id INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(layer, sub_layer, signal_name, asset, action)
    );
"""

_CREATE_REVERSE_ATTRIBUTIONS = """
    CREATE TABLE IF NOT EXISTS reverse_attributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        abnormal_ts INTEGER NOT NULL,
        abnormal_pct REAL,
        abnormal_kind TEXT DEFAULT '',
        related_decision_ids TEXT DEFAULT '',
        missed_signal_count INTEGER DEFAULT 0,
        forward_win_count INTEGER DEFAULT 0,
        forward_loss_count INTEGER DEFAULT 0,
        coverage_ratio REAL,
        detail_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(asset, abnormal_ts)
    );
"""


# ── v232.6 P1: 反思→policy 闭环 (让"反思"真正影响"决策") ──
# 仪表盘给的是事实（E001 累计亏 81%），但 ai_worker 下次遇到同样的信号不会知道这件事。
# 这两张表把"反思结论"翻译成"可审核的策略补丁候选",由人工审过才允许 apply。
# 设计原则：
#   - 候选生成 zero-risk：只写新表，不改 ai_worker 行为
#   - 闭环受控：apply 必须有 reviewer + rationale + 有效期
#   - 同 target_module + 同 patch_payload_hash UNIQUE，防止无限重复生成

_CREATE_POLICY_PATCH_QUEUE = """
    CREATE TABLE IF NOT EXISTS policy_patch_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patch_type TEXT NOT NULL,        -- 'prompt_addition' | 'threshold_change' | 'rule_addition' | 'pattern_warning'
        target_module TEXT NOT NULL,     -- 'ai_worker' | 'risk_manager' | 'decision_prompt' | 'gate'
        patch_payload TEXT NOT NULL,     -- json.dumps({...具体补丁内容})
        patch_payload_hash TEXT NOT NULL,-- sha1(patch_payload) — 去重用
        source_kind TEXT NOT NULL,       -- 'error_code_pnl' | 'sibling_diff' | 'reflection_pattern' | 'llm_self_low'
        source_reflection_ids TEXT DEFAULT '[]',   -- json list of reflection ids
        source_error_codes  TEXT DEFAULT '[]',     -- json list of error codes
        sample_size INTEGER DEFAULT 0,            -- 支撑该候选的样本量
        cumulative_pnl_impact REAL DEFAULT 0.0,   -- 累计 pnl 影响 (负=亏)
        confidence_score REAL DEFAULT 0.0,         -- 0..1 自评置信度
        rationale TEXT DEFAULT '',                 -- 人类可读的"为什么提这个补丁"
        status TEXT NOT NULL DEFAULT 'pending'    -- 'pending' | 'approved' | 'rejected' | 'applied' | 'expired'
            CHECK (status IN ('pending','approved','rejected','applied','expired')),
        created_at INTEGER NOT NULL,
        reviewed_at INTEGER,
        reviewer_note TEXT DEFAULT '',
        applied_at INTEGER,
        expires_at INTEGER,                       -- 自动过期时间 (90d 后 expired 状态)
        supersedes_id INTEGER,                    -- 旧 patch 被新 patch 替换
        applied_count INTEGER DEFAULT 0,          -- 该补丁被 ai_worker / risk 注入的次数
        last_apply_at INTEGER,                    -- 最近一次注入时间
        UNIQUE(target_module, patch_payload_hash)
    );
"""

_CREATE_IDX_PPQ_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_ppq_status_module
        ON policy_patch_queue(status, target_module, created_at DESC);
"""

_CREATE_IDX_PPQ_PENDING = """
    CREATE INDEX IF NOT EXISTS idx_ppq_pending_expires
        ON policy_patch_queue(status, expires_at)
        WHERE status IN ('pending','approved');
"""

_CREATE_SIBLING_DIFF_RULES = """
    CREATE TABLE IF NOT EXISTS sibling_diff_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_signature TEXT NOT NULL,    -- sha1(fingerprint + diff_features + asset)
        fingerprint TEXT NOT NULL,       -- content_fingerprint 同组的根
        asset TEXT NOT NULL,             -- 目标品种
        shared_features TEXT NOT NULL,    -- json: 兄弟决策共有的 feature vector
        winner_only_features TEXT DEFAULT '{}',  -- json: 赢家有输家没有的
        loser_only_features  TEXT DEFAULT '{}',  -- json: 输家有赢家没有的
        winner_decision_ids TEXT DEFAULT '[]',   -- json list
        loser_decision_ids  TEXT DEFAULT '[]',    -- json list
        n_pairs INTEGER DEFAULT 0,                 -- 兄弟对数
        consistency_score REAL DEFAULT 0.0,        -- 0..1, diff 重复出现的比例
        confidence_score REAL DEFAULT 0.0,        -- 自评: 0.4*consistency + 0.3*sample + 0.3*pnl_gap
        pnl_gap_avg REAL DEFAULT 0.0,             -- 赢家 - 输家的 pnl 差
        discovered_at INTEGER NOT NULL,
        last_seen_at INTEGER,                      -- 最近一次同 fingerprint 出现
        status TEXT NOT NULL DEFAULT 'candidate'  -- 'candidate' | 'promoted' | 'rejected'
            CHECK (status IN ('candidate','promoted','rejected')),
        promoted_at INTEGER,
        promoted_to TEXT DEFAULT '',              -- 'patch:id:42' 指向 policy_patch_queue 的某行
        UNIQUE(rule_signature)
    );
"""

_CREATE_IDX_SDR_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_sdr_status_asset
        ON sibling_diff_rules(status, asset, discovered_at DESC);
"""

_CREATE_IDX_SDR_FINGERPRINT = """
    CREATE INDEX IF NOT EXISTS idx_sdr_fingerprint
        ON sibling_diff_rules(fingerprint, last_seen_at DESC);
"""


# ── v270 P2: 多源数据 fallback 健康日志（feed_health） ──
# 每次按 feed_redund_policy 走多源尝试时记一行 — 用于仪表盘 + 反思面板的「数据系统判断」项
# 关键设计：status 只能是 4 个枚举 (success / degraded / failed / skipped)
#          没有 severity/level — UI 全部平铺
_CREATE_FEED_HEALTH = """
    CREATE TABLE IF NOT EXISTS feed_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        feed_kind TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        latency_ms INTEGER DEFAULT 0,
        value REAL,
        error TEXT,
        ts TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_CREATE_IDX_FEED_HEALTH_ASSET_TS = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_asset_ts "
    "ON feed_health(asset, ts);"
)
_CREATE_IDX_FEED_HEALTH_KIND = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_kind "
    "ON feed_health(feed_kind);"
)
_CREATE_IDX_FEED_HEALTH_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_feed_health_status "
    "ON feed_health(status, ts);"
)


# ── v1 重构：分类未归类事件表（人工复核 / 规则补全队列）──
# 任何 market_classifier.infer_market_category() 抛 UnclassifiedError 时，
# ai_worker._record_unclassified_event() 会把该事件写到这里，状态 'pending'。
# 人工 / 规则补全流程完成后，UPDATE 为 'resolved' 或 'dismissed'。
#
# 字段设计要点：
#   - news_id 链回 raw_news.id（标题/正文/原文链接）
#   - target_asset / llm_market_category / llm_target_asset 记 LLM 实际产生的值
#   - reason 写 market_classifier 抛出的 reason（MISSING/UNKNOWN/CAT_MISMATCH 等）
#   - layer 写具体层（B_sanitize_step12 / C_sanitize_step5 / D_sibling_infer 等）
#   - resolved_category / resolved_target_asset / resolver / note 由人工/规则补全填写
_CREATE_UNCLASSIFIED_EVENTS = """
    CREATE TABLE IF NOT EXISTS unclassified_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        news_id INTEGER DEFAULT 0,
        title TEXT DEFAULT '',
        source TEXT DEFAULT '',
        published_at TEXT DEFAULT '',
        target_asset TEXT DEFAULT '',
        llm_market_category TEXT DEFAULT '',
        llm_target_asset TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        layer TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'resolved', 'dismissed')),
        resolved_at TEXT DEFAULT '',
        resolved_category TEXT DEFAULT '',
        resolved_target_asset TEXT DEFAULT '',
        resolver TEXT DEFAULT '',
        note TEXT DEFAULT ''
    );
"""

_CREATE_IDX_UNCLASSIFIED_PENDING = """
    CREATE INDEX IF NOT EXISTS idx_unclassified_pending
        ON unclassified_events(status, created_at DESC);
"""

_CREATE_IDX_UNCLASSIFIED_NEWS = """
    CREATE INDEX IF NOT EXISTS idx_unclassified_news
        ON unclassified_events(news_id);
"""

_UNCLASSIFIED_EVENTS_COLUMNS = [
    # 兼容老库（如果有的话）
]

_EXPERIENCE_ENTRIES_V188_COLUMNS = [
    ("control_apply_count", "INTEGER DEFAULT 0"),
    ("applied_win_rate",    "REAL"),
    ("control_win_rate",    "REAL"),
    ("lift",                "REAL"),
    ("verified_count",      "INTEGER DEFAULT 0"),
]

# experience_applications: v188 加 llm_invoked 列(IF NOT EXISTS 已存在则补列)
_EXPERIENCE_APPLICATIONS_V188_COLUMNS = [
    ("llm_invoked", "INTEGER DEFAULT 0"),
]

# v188 P1-5: decision_layer_signals 复合索引 + 单列索引
_DECISION_LAYER_SIGNALS_V188_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dls_last_seen_ts ON decision_layer_signals(last_seen_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dls_layer ON decision_layer_signals(layer)",
    "CREATE INDEX IF NOT EXISTS idx_dls_layer_signal ON decision_layer_signals(layer, sub_layer, signal_name)",
    "CREATE INDEX IF NOT EXISTS idx_dls_asset_action ON decision_layer_signals(asset, action)",
]

# v188 P1-4: missed_signal 扫描进度表(增量扫描 — 避免每次全扫 11s/50)
_CREATE_MISSED_SIGNAL_SCAN_STATE = """
    CREATE TABLE IF NOT EXISTS missed_signal_scan_state (
        asset TEXT PRIMARY KEY,
        last_decision_id INTEGER NOT NULL DEFAULT 0,
        last_scan_ts INTEGER NOT NULL DEFAULT 0,
        last_scan_count INTEGER NOT NULL DEFAULT 0,
        last_marked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    )
"""
_MISSED_SIGNAL_SCAN_STATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_msss_updated_at ON missed_signal_scan_state(updated_at)",
]


# ──────────────────────────────────────────────────────────────────────────────
# v189: 回测每日重置 (run_id 隔离) + 反思系统每日归档
# ──────────────────────────────────────────────────────────────────────────────

# 注意：backtest_runs 沿用最早的「单次回测结果」schema（见上方 1444 行定义，含
# id/strategy_id/version_id），strategy_store.py 依赖它 SELECT * ... ORDER BY id。
# v189 的「每日重置登记表」应使用独立的 backtest_daily_runs（见下方
# _CREATE_BACKTEST_DAILY_RUNS），不能覆盖 backtest_runs —— 否则 fresh DB 会缺
# id/strategy_id，且 _INDEXES 的 idx_backtest_runs_strategy 会因缺列而崩溃。

# v189-1: 给 4 张主表加 run_id 列 + 索引
_BACKTEST_V189_COLUMNS = [
    ("ai_decisions",              [("run_id", "TEXT DEFAULT ''")]),
    ("decision_layer_signals",    [("run_id", "TEXT DEFAULT ''")]),
    ("trade_reflections_v2",      [("run_id", "TEXT DEFAULT ''")]),
    ("experience_applications",   [("run_id", "TEXT DEFAULT ''")]),
]
_BACKTEST_V189_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ad_run_id ON ai_decisions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_dls_run_id ON decision_layer_signals(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_trv2_run_id ON trade_reflections_v2(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_eap_run_id ON experience_applications(run_id)",
]


# v189-1-补: 每日重置登记表 (与原 backtest_runs 共存, 不破坏旧 schema)
_CREATE_BACKTEST_DAILY_RUNS = """
    CREATE TABLE IF NOT EXISTS backtest_daily_runs (
        run_id TEXT PRIMARY KEY,
        asset_class TEXT NOT NULL DEFAULT 'all',
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        trigger TEXT DEFAULT 'auto_daily',
        decision_count INTEGER DEFAULT 0,
        archived INTEGER DEFAULT 0,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
"""
_BACKTEST_DAILY_RUNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bdr_archived_started ON backtest_daily_runs(archived, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_bdr_asset_class ON backtest_daily_runs(asset_class)",
]

# v189-2: 反思系统每日归档 — 3 张聚合表
_CREATE_REFLECTION_DAILY_JOURNAL = """
    CREATE TABLE IF NOT EXISTS reflection_daily_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_date TEXT NOT NULL,                -- YYYY-MM-DD (本地日)
        run_id TEXT NOT NULL,
        n_decisions INTEGER DEFAULT 0,
        n_wins INTEGER DEFAULT 0,
        n_losses INTEGER DEFAULT 0,
        win_rate REAL,
        pnl_usdt REAL,
        ontology_breakdown TEXT DEFAULT '{}',       -- JSON: {narrative_bias: 5, ...}
        unknown_rate REAL,                         -- v188 unknown 兜底占比
        llm_invoked_count INTEGER DEFAULT 0,        -- llm_invoked=1 的应用次数
        top_experiences TEXT DEFAULT '',            -- 文本摘要 (top 5 应用经验)
        top_missed_signals TEXT DEFAULT '',         -- 文本摘要 (top 5 missed)
        reflections_summary TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(journal_date, run_id)
    )
"""
_REFLECTION_DAILY_JOURNAL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rdj_date ON reflection_daily_journal(journal_date)",
    "CREATE INDEX IF NOT EXISTS idx_rdj_run_id ON reflection_daily_journal(run_id)",
]

_CREATE_REFLECTION_CYCLE_LOG = """
    CREATE TABLE IF NOT EXISTS reflection_cycle_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        reflection_id INTEGER,
        outcome TEXT DEFAULT 'pending',             -- pending / win / loss
        llm_invoked INTEGER DEFAULT 0,
        latency_ms INTEGER,
        ontology_signal TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        -- v189-fix: 同一 decision+run_id 只允许一条, worker 重跑/锁失败重试不会重复写
        UNIQUE(decision_id, run_id)
    )
"""
_REFLECTION_CYCLE_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rcl_decision ON reflection_cycle_log(decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_run_id ON reflection_cycle_log(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_outcome ON reflection_cycle_log(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_created ON reflection_cycle_log(created_at)",
    # v189-fix: UNIQUE(decision_id, run_id) 防 worker 重跑/锁重试时重复写入.
    # 已存在的旧表没这个约束, CREATE UNIQUE INDEX 幂等兼容.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rcl_dec_run ON reflection_cycle_log(decision_id, run_id)",
]

_CREATE_EXPERIENCE_APPLICATION_DAILY = """
    CREATE TABLE IF NOT EXISTS experience_application_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        journal_date TEXT NOT NULL,
        run_id TEXT NOT NULL,
        experience_id INTEGER NOT NULL,
        n_applied INTEGER DEFAULT 0,
        n_llm_invoked INTEGER DEFAULT 0,
        applied_win_rate REAL,
        lift REAL,
        source TEXT DEFAULT 'paper_trading',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(journal_date, experience_id)
    )
"""
_EXPERIENCE_APPLICATION_DAILY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ead_date ON experience_application_daily(journal_date)",
    "CREATE INDEX IF NOT EXISTS idx_ead_exp ON experience_application_daily(experience_id)",
]

_TRADE_REFLECTIONS_V2_V188_COLUMNS = [
    ("llm_depth_reflection", "TEXT DEFAULT ''"),
    ("verification_method",  "TEXT DEFAULT ''"),
    # v188 reflection_ontology 三层分类(LOSS/WIN 都记录)
    ("layer",        "TEXT DEFAULT ''"),
    ("sub_layer",    "TEXT DEFAULT ''"),
    ("signal_name",  "TEXT DEFAULT ''"),
    # v188 验证方法 + 时间窗
    ("verification_window", "TEXT DEFAULT ''"),  # e.g. "hit_rate≥60% over 30d"
    ("verified_at",  "TEXT DEFAULT ''"),
]


# v190: ontology_signal 拼合列(只读便利) — 三层标签 "layer/sub_layer/signal_name" 拼成单个字符串
# 设计动机: error_layer/sub_layer/signal 三列每次 GROUP BY 要带 3 个字段, 业务侧常需要单列 WHERE。
# ontology_signal 列与 error_layer/sub_layer/signal 永远同步, 由 paper_reflection_worker
# INSERT 路径维护 + backfill 一次回填所有历史行。
_TRADE_REFLECTIONS_V2_V190_COLUMNS = [
    ("ontology_signal", "TEXT DEFAULT ''"),
]
_TRADE_REFLECTIONS_V2_V190_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trv2_ontology_signal ON trade_reflections_v2(ontology_signal)",
]


# ═══════════════════════════════════════════════════════════════════════════
# v200 P0+ "严谨兜底": ai_decisions 字段源头追溯 + completeness_score
# 设计动机: 之前 LLM 漏填字段时 ai_worker 写 "服务端兜底：LLM 未填 X" 占位字符串,
#   反思系统跑在脏数据上,污染传染给 review_insights → prompt 注入 → LLM 学坏.
# 修复: 每个字段打 source 标记(llm/rule/business_knowledge/historical_pattern/
#   llm_ensemble_consensus/missing), 算 completeness_score, 反思系统过滤
#   completeness<0.7 的脏决策不入深度分析.
# ═══════════════════════════════════════════════════════════════════════════
_AI_DECISIONS_V200_COMPLETENESS_COLUMNS = [
    # 字段级 source 标记(JSON): {"reasoning": "llm", "low_impact": "rule", ...}
    ("field_sources", "TEXT DEFAULT '{}'"),
    # 缺失字段列表(JSON 数组): ["reasoning", "factor_ranking"]
    ("missing_fields", "TEXT DEFAULT '[]'"),
    # 整体填全率 0~1: 1.0 = 全 LLM 真输出, 0.0 = 全缺失
    ("completeness_score", "REAL NOT NULL DEFAULT 1.0"),
    # 5 类 source 计数(方便 SQL 聚合)
    ("llm_field_count", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_field_count", "INTEGER NOT NULL DEFAULT 0"),
    ("business_kb_field_count", "INTEGER NOT NULL DEFAULT 0"),
    ("historical_field_count", "INTEGER NOT NULL DEFAULT 0"),
    # 历史兜底引用追溯(JSON): {"reasoning": ref_decision_id, ...}
    ("historical_ref_ids", "TEXT DEFAULT '{}'"),
    # LLM 兜底 retry 计数 (>=1 表示经过 1 次 retry 才完整)
    ("llm_retry_count", "INTEGER NOT NULL DEFAULT 0"),
]


_TRADE_REFLECTIONS_V2_V200_SOURCE_BREAKDOWN_COLUMNS = [
    # 反思时的 source 分布(JSON): {"llm": 12, "rule": 3, "fallback": 0}
    ("source_breakdown", "TEXT DEFAULT '{}'"),
    # 反思时的 completeness_score(快照)
    ("completeness_at_reflection", "REAL DEFAULT 1.0"),
    # 标记是否被完整性过滤跳过(1=跳过)
    ("skipped_low_completeness", "INTEGER NOT NULL DEFAULT 0"),
]


# ═══════════════════════════════════════════════════════════════════════════
# v230 反思深度增强：K线 / 跨资产 / 抢跑 / 运气 vs 信号 / 自循环
# 设计动机（用户反馈 4 条）：
#   1) 反思缺数据 — 当前 trade_reflections_v2 没有 K线上下文、跨资产联动量化、抢跑检测、运气 vs 信号
#   2) 反思太表层 — 之前 root_cause 多是关键词匹配(_ANALYSIS_STEPS), 没有站在逻辑/因果链
#   3) 不看K线/不联动资产 — 涨/跌核心逻辑不清楚
#   4) 不自循环 — meta_reflection_updater 只改 .md, 不实际再反思
#
# 新字段语义：
#   kline_at_decision        : 决策时点的 1h/15m/1d OHLC + ATR + pos_pct_7d (JSON)
#   kline_pattern            : K线形态(breakout_up / inside_bar / double_top / marubozu / doji...)
#   cross_asset_sync_score   : 与关联资产决策时刻的 1h 同步性 [-1,+1]
#   front_run_score          : 新闻 first_seen_ts 与价格异动时差(0=未抢跑, 1=完全抢跑)
#   luck_vs_signal_score     : [-1, +1]; +1=纯信号驱动, -1=纯运气
#   market_regime_detail     : 决策时的 vol/regime/session(美/欧/亚)/event_window(JSON)
#   deep_root_cause          : v230 新深度根因(替代/补充浅层 root_cause)
#   secondary_reflection_id  : 自循环 — 指向本反思的元反思 ID
#   secondary_reflection_at  : 元反思时间戳
# ═══════════════════════════════════════════════════════════════════════════
_TRADE_REFLECTIONS_V2_DEEP_COLUMNS = [
    ("kline_at_decision",       "TEXT DEFAULT ''"),    # JSON {ohlc_1h:{o,h,l,c}, ohlc_15m:{...}, ohlc_1d:{...}, atr_pct, pos_pct_7d, range_high_7d, range_low_7d}
    ("kline_pattern",           "TEXT DEFAULT ''"),    # breakout_up / breakout_down / inside_bar / engulfing_bull / engulfing_bear / doji / marubozu / double_top / double_bottom / unknown
    ("cross_asset_sync_score",  "REAL DEFAULT 0.0"),   # -1.0 ~ +1.0
    ("front_run_score",         "REAL DEFAULT 0.0"),   # 0.0 ~ 1.0
    ("luck_vs_signal_score",    "REAL DEFAULT 0.0"),   # -1.0 ~ +1.0
    ("market_regime_detail",    "TEXT DEFAULT ''"),    # JSON {regime, vol_pct, session, event_window, hours_to_event}
    ("deep_root_cause",         "TEXT DEFAULT ''"),    # v230 深度根因(深链路归因)
    ("deep_lesson",             "TEXT DEFAULT ''"),    # v230 深度教训(可注入 prompt)
    ("secondary_reflection_id", "INTEGER DEFAULT 0"),  # 自循环:元反思的 reflection_id
    ("secondary_reflection_at", "TEXT DEFAULT ''"),    # 元反思时间
    ("enriched_at",             "TEXT DEFAULT ''"),    # v230 enrich_reflection() 执行时间
    ("enrich_status",           "TEXT DEFAULT 'pending'"),  # pending / ok / partial / failed
    ("enrich_error",            "TEXT DEFAULT ''"),    # enrich 失败原因
]


# v232 反思"核心本质"内容审计 — 6 层内容审计输出列。
# 区别于 L1 (pattern match) / L2 (context) / L3 (meta-loop): 此层做
# reasoning_path 的*内容*审计、查 LLM 没引用的反方证据、按真实盘面验证
# reasoning 假说、跨决策系统性模式、判断 vs 执行分离、反思对反思的偏差审计。
# 设计动机: 现有 36 规则 + E001-E027 都只在条件命中时标 error_code,
# 不告诉用户"reasoning_path 哪一段逻辑塌了 / reasoning 假说本身真假 / 跨决策盲点"。
#
# 新字段语义:
#   core_chain_audit         : reasoning_path 8 段逐段审计 JSON
#                              [{seg, claim, link_verified, conflict_with, evidence_score}, ...]
#   core_counter_evidence    : 决策时点 DB 内可用但 LLM 未引用的证据摘要
#                              {omitted_count, omitted_signals_against, omitted_weight}
#   core_truth_score         : reasoning 假说 vs 实际盘面真伪 [0, 1]
#                              (0.5=中庸, 1=完全真, 0=完全假)
#   core_pattern_alert       : 跨决策系统性模式警报
#                             [{pattern_key, sample_n, loss_n, avg_pnl, alert_level}, ...]
#   core_judgment_breakdown  : 判断正确性 vs 执行正确性分离
#                              {direction_correct, sizing_correct, tp_correct, sl_correct,
#                               judgment_score, execution_score}
#   core_meta_audit          : 反思对反思的内容审计
#                              {self_classify_drift, layer_bias, signal_hit_drift}
#   core_overall_grade       : 综合评级 (A/B/C/D/F) — 给前端一个总览信号
#   core_enriched_at         : reflection_core.enrich_reflection_core() 执行时间
#   core_enrich_status       : pending / ok / partial / failed
#   core_enrich_error        : 失败原因
_TRADE_REFLECTIONS_V2_CORE_COLUMNS = [
    ("core_chain_audit",        "TEXT DEFAULT ''"),    # JSON: 链段审计
    ("core_counter_evidence",   "TEXT DEFAULT ''"),    # JSON: 反方证据
    ("core_truth_score",        "REAL DEFAULT 0.5"),   # 0.0 ~ 1.0 (0.5=中庸)
    ("core_pattern_alert",      "TEXT DEFAULT ''"),    # JSON: 跨决策模式
    ("core_judgment_breakdown", "TEXT DEFAULT ''"),    # JSON: 判断 vs 执行
    ("core_meta_audit",         "TEXT DEFAULT ''"),    # JSON: 反思自审
    ("core_overall_grade",      "TEXT DEFAULT 'C'"),   # A/B/C/D/F
    ("core_enriched_at",        "TEXT DEFAULT ''"),
    ("core_enrich_status",      "TEXT DEFAULT 'pending'"),  # pending / ok / partial / failed
    ("core_enrich_error",       "TEXT DEFAULT ''"),
]


# ═══════════════════════════════════════════════════════════════════════════
# v232.5 反思「数字落地」与「自检可信度」 — 让反思产出不再空话
# 设计动机（用户反馈 2026-10-06）：
#   反思系统的反思不够深入,没有抓到问题的核心本质。
#   - root_cause/lesson 是模板句子,没有量化锚点(pnl/盈亏比/相关性/ATR)
#   - LLM 5 句深度反思(llm_depth_reflection)也不强制引用真实数字
#   - 没有"反思对反思"的可信度自评
#   - 同批次/同 fingerprint 反思没被标记 → 假多样性
#
# 新字段语义：
#   evidence_strength_score   REAL  [0,5]
#       root_cause + lesson 里含有的「量化锚点」数量(0=空话,1=只pnl,5=完整)。
#       锚点定义:具体数值(pnl/价格/百分比)、具体比率(盈亏比/相关性/ATR)、
#                具体比较(vs 同 layer N 笔 vs 反向预期)。
#       计算:每命中 1 类 +1。>3 = 深度反思,0 = 空话反思。
#   reflection_self_score     REAL  [0,1]
#       反思自评可信度 = 0.4*evidence/5 + 0.3*completeness_at_reflection
#                       + 0.3*depth_score/10
#       <0.3 反思基本不可信(数据残缺),>0.7 高可信度。
#       前端按这个分数对反思结果染色,让用户看清哪条反思"硬证据"、哪条是"嘴炮"。
#   is_sibling                INTEGER  (0/1)
#       1 = 同 parent_id 或同 content_fingerprint(同批次/同新闻衍生)
#       0 = 独立决策
#       反思统计时 is_sibling=1 应单独成桶,避免假多样性放大胜率/失败率。
# ═══════════════════════════════════════════════════════════════════════════
_TRADE_REFLECTIONS_V2_V2325_GROUNDING_COLUMNS = [
    ("evidence_strength_score", "REAL DEFAULT 0.0"),    # [0, 5] 数字锚点计数
    ("reflection_self_score",   "REAL DEFAULT 0.0"),    # [0, 1] 反思自评可信度
    ("is_sibling",              "INTEGER DEFAULT 0"),    # 0=独立 1=同 batch/fingerprint
]


# ═══════════════════════════════════════════════════════════════════════════
# v231 反思「逻辑分析」 — 让反思检查 reasoning_path 的形式逻辑有效性
# 设计动机 (用户 v230 反馈 5):
#   反思系统只查事件链 / 推理质量, 不查*形式逻辑* — 同一 reasoning_path
#   内是否自相矛盾 / 结论能否从前提推出 / 是否循环论证 / 假二择一 /
#   关键术语歧义 / 后此谬因。
#
# 新字段语义:
#   logic_integrity_score   REAL  [0, 1]
#       整合度分, 越高越严密 (1.0 = 无错)。
#       公式: max(0.0, 1.0 - sum(severity_penalty))
#       severity_penalty: INFO=0.05, WARN=0.15, CRITICAL=0.30
#   logic_findings_json     TEXT
#       JSON: {summary: {by_code, by_severity, codes_list, n_findings},
#              details: [{error_code, severity, signal_name, detail, evidence_refs}]}
#   logic_checked_at        TEXT  ISO8601
#       空字符串或 NULL = 未跑过 enrich_logic
#
# 6 个检测器 (E028-E033) 详见 reflection_logic_analysis.py:
#   E028 self_contradiction       (CRITICAL)
#   E029 non_sequitur             (WARN)
#   E030 false_dichotomy          (WARN)
#   E031 circular_reasoning       (CRITICAL)
#   E032 equivocation             (WARN)
#   E033 post_hoc                 (WARN)
# ═══════════════════════════════════════════════════════════════════════════
_TRADE_REFLECTIONS_V2_V231_LOGIC_COLUMNS = [
    ("logic_integrity_score", "REAL DEFAULT 1.0"),     # [0, 1] 整合度分
    ("logic_findings_json",   "TEXT DEFAULT '{}'"),    # JSON findings + summary
    ("logic_checked_at",      "TEXT DEFAULT ''"),      # ISO8601, 空 = 未跑
]


# v230: 自循环反思表 — 元反思对元反思, 反思对反思
# 父反思 → 元反思 (level=1)
# 元反思 → 元元反思 (level=2)
# 链路可深可浅, 但默认 level<=2 防爆炸
_CREATE_REFLECTION_CYCLES = """
    CREATE TABLE IF NOT EXISTS reflection_cycles (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_reflection_id INTEGER NOT NULL,         -- 父反思 ID (trade_reflections_v2.id 或上一级 cycle.id)
        child_reflection_id  INTEGER DEFAULT 0,        -- 子反思 ID (可能不存在: 仅产出 finding)
        parent_layer         TEXT NOT NULL DEFAULT '',-- 父反思的 (layer/sub_layer/signal)
        parent_signal        TEXT NOT NULL DEFAULT '',
        cycle_level          INTEGER NOT NULL DEFAULT 1,  -- 1=第一次元反思, 2=元元反思, ...
        finding_kind         TEXT NOT NULL DEFAULT '',-- confirmed / refined / invalidated / contradictory / lucky_or_skill
        finding_detail       TEXT NOT NULL DEFAULT '',-- 元反思结论文本
        evidence_json        TEXT NOT NULL DEFAULT '{}',
        lookback_days        INTEGER NOT NULL DEFAULT 7,
        source_kind          TEXT NOT NULL DEFAULT 'live',  -- v231+ watermark: 'live' | 'demo' | 'stress_test' | 'replay'
        source_run_id        TEXT NOT NULL DEFAULT '',      -- v231+ 触发本 cycle 的运行 ID (空=live)
        source_watermark     TEXT NOT NULL DEFAULT '',      -- v231+ HTTP 响应水印 (base64url, demo 才有)
        created_at           TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_REFLECTION_CYCLES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rc_parent ON reflection_cycles(parent_reflection_id)",
    "CREATE INDEX IF NOT EXISTS idx_rc_level ON reflection_cycles(cycle_level)",
    "CREATE INDEX IF NOT EXISTS idx_rc_finding ON reflection_cycles(finding_kind)",
    "CREATE INDEX IF NOT EXISTS idx_rc_created ON reflection_cycles(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rc_source ON reflection_cycles(source_kind, created_at DESC)",
]


# v230: 决策时点的 K线缓存 — reflection_deep.py 计算后写这里, 避免重复查 kline_bars
_CREATE_KLINE_DECISION_CACHE = """
    CREATE TABLE IF NOT EXISTS kline_at_decision_cache (
        decision_id      INTEGER PRIMARY KEY,
        asset            TEXT NOT NULL DEFAULT '',
        decision_ts      INTEGER NOT NULL DEFAULT 0,
        ohlc_1h_json     TEXT NOT NULL DEFAULT '{}',
        ohlc_15m_json    TEXT NOT NULL DEFAULT '{}',
        ohlc_1d_json     TEXT NOT NULL DEFAULT '{}',
        atr_pct          REAL DEFAULT 0.0,
        pos_pct_7d       REAL DEFAULT NULL,    -- v300.5 P0+: 默认 NULL,不再 0.5 兜底 —— 0.5 会被读端误判为"价格中点"
        range_high_7d    REAL DEFAULT 0.0,
        range_low_7d     REAL DEFAULT 0.0,
        cross_asset_json TEXT NOT NULL DEFAULT '{}',
        pattern_label    TEXT NOT NULL DEFAULT 'unknown',
        pattern_detail   TEXT NOT NULL DEFAULT '',
        front_run_score  REAL DEFAULT 0.0,
        computed_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
"""

_KLINE_DECISION_CACHE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_kadc_asset ON kline_at_decision_cache(asset)",
    "CREATE INDEX IF NOT EXISTS idx_kadc_ts ON kline_at_decision_cache(decision_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_kadc_pattern ON kline_at_decision_cache(pattern_label)",
]


_DECISION_LAYER_SIGNALS_V200_SOURCE_COUNT_COLUMNS = [
    # 聚合时按 source 分桶计数(精确量化 LLM vs 我们自己模型贡献)
    ("llm_count", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_count", "INTEGER NOT NULL DEFAULT 0"),
    ("business_kb_count", "INTEGER NOT NULL DEFAULT 0"),
    ("historical_count", "INTEGER NOT NULL DEFAULT 0"),
]


# 监控 LLM 增量价值的周期评估表(每 24h daemon 算一次)
_CREATE_MONITOR_LLM_VALUE = """
    CREATE TABLE IF NOT EXISTS monitor_llm_value (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        window_days INTEGER NOT NULL,
        llm_n INTEGER NOT NULL DEFAULT 0,
        llm_win_rate REAL NOT NULL DEFAULT 0.0,
        llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
        llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
        not_llm_n INTEGER NOT NULL DEFAULT 0,
        not_llm_win_rate REAL NOT NULL DEFAULT 0.0,
        not_llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
        not_llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
        delta_win_rate REAL NOT NULL DEFAULT 0.0,
        delta_avg_pnl REAL NOT NULL DEFAULT 0.0,
        verdict TEXT NOT NULL DEFAULT 'insufficient_data',
            -- 'llm_positive_value' / 'llm_negative_value' / 'llm_neutral' / 'insufficient_data'
        action_recommended TEXT DEFAULT '',
        computed_at TEXT DEFAULT (datetime('now'))
    );
"""
_MONITOR_LLM_VALUE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mlv_verdict ON monitor_llm_value(verdict)",
    "CREATE INDEX IF NOT EXISTS idx_mlv_computed ON monitor_llm_value(computed_at)",
]


# v189 Item 8: decision_errors 加 weight 列（层权重 = severity*10 + layer_weight）
# 排序时统一按 weight DESC，避免硬编码 source='event_logic' 的 CASE WHEN
_DECISION_ERRORS_V189_COLUMNS = [
    ("weight", "INTEGER NOT NULL DEFAULT 0"),
]


# v190: 实盘交易通道 — trade_audit_log（每次 trade_executor 处理 decision 都落一条）
# 设计目的：trade_executor 与 quick_sim 完全排他。trade_executor 写 entry_price
# 时使用 `WHERE id=? AND entry_price IS NULL` 原子保护；任何被拒绝/跳过/出错
# 的决策也会写本表，fetch_pending 通过 NOT IN 子查询排重，避免无限循环。
_CREATE_TRADE_AUDIT_LOG = """
    CREATE TABLE IF NOT EXISTS trade_audit_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id   INTEGER NOT NULL,
        asset         TEXT NOT NULL DEFAULT '',
        symbol        TEXT NOT NULL DEFAULT '',
        side          TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL,                  -- ORDER_PLACED / ADAPTER_SKIPPED / RISK_REJECTED / ADAPTER_ERROR
        notional_usdt REAL    DEFAULT 0.0,
        quantity      REAL    DEFAULT 0.0,
        filled_price  REAL    DEFAULT 0.0,
        order_id      TEXT    DEFAULT '',
        adapter       TEXT    DEFAULT '',             -- mock / binance
        mode          TEXT    DEFAULT '',             -- off / paper / live_dry / live_real
        reason        TEXT    DEFAULT '',
        raw_json      TEXT    DEFAULT NULL,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""
_TRADE_AUDIT_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tal_decision ON trade_audit_log(decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_tal_status ON trade_audit_log(status)",
    "CREATE INDEX IF NOT EXISTS idx_tal_created ON trade_audit_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tal_asset_symbol ON trade_audit_log(asset, symbol)",
]


# ── v191 每日归档 3 张表 ──────────────────────────────────────────────
# 设计动机：v190 实盘通道落 audit 后,每条决策的全链路分散在 4-5 张表
# (raw_news → ai_decisions → trade_audit_log → settled outcomes →
# trade_reflections),运营复盘需要一次 JOIN 才能拼出来,效率低、易丢上下文。
# 落地三件事:
#   1) daily_reset_log         — 每天数据重置的事件(系统启动/mode 切换/跨日清理)
#   2) signal_chain_archive    — 每条 ai_decisions 的全链路快照(幂等 INSERT)
#   3) daily_reflection_summary— 每日聚合(数量/胜率/PnL/触发原因分布)
#
# 与已有 reflection_daily_journal 互补:后者聚焦"经验应用"维度,
# 本表聚焦"信号全链路 + 实盘执行 + 结算"维度。

_CREATE_DAILY_RESET_LOG = """
    CREATE TABLE IF NOT EXISTS daily_reset_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        reset_date    TEXT NOT NULL,
        reset_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        trigger       TEXT NOT NULL DEFAULT 'auto_daily'
            CHECK (trigger IN ('auto_daily', 'manual', 'startup', 'mode_change', 'kill_switch')),
        run_id        TEXT NOT NULL DEFAULT '',
        trade_mode    TEXT NOT NULL DEFAULT '',
        cleared_decisions   INTEGER DEFAULT 0,
        cleared_audit_rows  INTEGER DEFAULT 0,
        notes         TEXT DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""
_DAILY_RESET_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_drl_date ON daily_reset_log(reset_date)",
    "CREATE INDEX IF NOT EXISTS idx_drl_run_id ON daily_reset_log(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_drl_trigger ON daily_reset_log(trigger)",
    "CREATE INDEX IF NOT EXISTS idx_drl_created ON daily_reset_log(created_at)",
]


_CREATE_SIGNAL_CHAIN_ARCHIVE = """
    CREATE TABLE IF NOT EXISTS signal_chain_archive (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id       INTEGER NOT NULL UNIQUE,
        run_id            TEXT NOT NULL DEFAULT '',
        decision_ts       TEXT NOT NULL DEFAULT '',
        decision_date     TEXT NOT NULL DEFAULT '',
        news_id           INTEGER,
        news_published_at TEXT DEFAULT '',
        news_source       TEXT DEFAULT '',
        news_title        TEXT DEFAULT '',
        sentiment_score   REAL,
        target_asset      TEXT NOT NULL DEFAULT '',
        suggested_action  TEXT NOT NULL DEFAULT '',
        market_category   TEXT DEFAULT '',
        reasoning         TEXT DEFAULT '',
        agent_model_id    TEXT DEFAULT '',
        bias_after_reflection TEXT DEFAULT '',
        audit_status      TEXT DEFAULT '',
        audit_reason      TEXT DEFAULT '',
        order_id          TEXT DEFAULT '',
        symbol            TEXT DEFAULT '',
        side              TEXT DEFAULT '',
        notional_usdt     REAL DEFAULT 0.0,
        quantity          REAL DEFAULT 0.0,
        filled_price      REAL DEFAULT 0.0,
        entry_price       REAL,
        adapter           TEXT DEFAULT '',
        mode              TEXT DEFAULT '',
        is_correct        TEXT DEFAULT '',
        exit_price        REAL,
        forward_pnl       REAL,
        mfe_pct           REAL,
        mae_pct           REAL,
        settled_at        TEXT DEFAULT '',
        reflection_layer  TEXT DEFAULT '',
        reflection_signal TEXT DEFAULT '',
        experience_ids    TEXT DEFAULT '',
        raw_payload       TEXT DEFAULT NULL,
        created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""
_SIGNAL_CHAIN_ARCHIVE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sca_run_id ON signal_chain_archive(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_sca_decision_date ON signal_chain_archive(decision_date)",
    "CREATE INDEX IF NOT EXISTS idx_sca_asset ON signal_chain_archive(target_asset)",
    "CREATE INDEX IF NOT EXISTS idx_sca_audit_status ON signal_chain_archive(audit_status)",
    "CREATE INDEX IF NOT EXISTS idx_sca_is_correct ON signal_chain_archive(is_correct)",
    "CREATE INDEX IF NOT EXISTS idx_sca_reflection_layer ON signal_chain_archive(reflection_layer)",
]


_CREATE_DAILY_REFLECTION_SUMMARY = """
    CREATE TABLE IF NOT EXISTS daily_reflection_summary (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        summary_date      TEXT NOT NULL,
        run_id            TEXT NOT NULL DEFAULT '',
        trade_mode        TEXT DEFAULT '',
        n_decisions       INTEGER DEFAULT 0,
        n_trades          INTEGER DEFAULT 0,
        n_skipped         INTEGER DEFAULT 0,
        n_risk_rejected   INTEGER DEFAULT 0,
        n_adapter_errors  INTEGER DEFAULT 0,
        n_pending         INTEGER DEFAULT 0,
        n_wins            INTEGER DEFAULT 0,
        n_losses          INTEGER DEFAULT 0,
        n_hold            INTEGER DEFAULT 0,
        n_unsettled       INTEGER DEFAULT 0,
        win_rate          REAL,
        total_pnl_usdt    REAL DEFAULT 0.0,
        avg_pnl_usdt      REAL DEFAULT 0.0,
        avg_mfe_pct       REAL,
        avg_mae_pct       REAL,
        max_drawdown_pct  REAL,
        asset_breakdown   TEXT DEFAULT '{}',
        action_breakdown  TEXT DEFAULT '{}',
        layer_breakdown   TEXT DEFAULT '{}',
        key_takeaways     TEXT DEFAULT '',
        lessons_learned   TEXT DEFAULT '',
        next_day_focus    TEXT DEFAULT '',
        reset_log_id      INTEGER,
        generated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE(summary_date, run_id)
    );
"""
_DAILY_REFLECTION_SUMMARY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_drs_date ON daily_reflection_summary(summary_date)",
    "CREATE INDEX IF NOT EXISTS idx_drs_run_id ON daily_reflection_summary(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_drs_reset_log ON daily_reflection_summary(reset_log_id)",
]


# trade_reflections 的迁移列：retry_count / last_error / last_error_at
# 旧数据库没有这些字段，_ensure_columns 会在每次 migrate() 时补上。
# 这样能在 schema 不破坏的前提下给反射加"重试 + 限速"机制。
_TRADE_REFLECTIONS_COLUMNS = [
    ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT NOT NULL DEFAULT ''"),
    ("last_error_at", "TEXT"),
    ("entry_accuracy", "REAL"),   # v175: 买点识别准确性 0-1（反思评估维度）
    ("trend_fit", "REAL"),         # v175: K线走势吻合度 0-1（反思评估维度）
    # ── v181 分层归因：layer/sublayer 对应 23 类失败 → 6 层 → 具名子层 ──
    ("layer", "TEXT DEFAULT ''"),
    ("sublayer", "TEXT DEFAULT ''"),
    # verdict_uncertain: 结算口径边缘情形（settle_flags 非空）→ 1，归因时不沉淀经验
    ("verdict_uncertain", "INTEGER DEFAULT 0"),
    # ── v182 反思改进：每笔反思产出的结构化参数调优建议（自动写库） ──
    ("improvement_suggestion", "TEXT DEFAULT ''"),       # 结构化 JSON 字符串（primary_param + secondary_params + evidence_summary）
    ("suggestion_param_key",    "TEXT DEFAULT ''"),       # primary_param.key（建议调哪个参数：buy_min_score/tp1_mult/...）
    ("suggestion_direction",    "TEXT DEFAULT ''"),       # primary_param.direction：up/down/on/off
    ("suggestion_magnitude",    "REAL    DEFAULT NULL"),  # primary_param.magnitude：调整幅度 0~1
    ("suggestion_confidence",   "REAL    DEFAULT NULL"),  # 建议置信度 0~1
    ("suggestion_rationale",    "TEXT DEFAULT ''"),       # primary_param.rationale：50 字内调整依据
]


# ── v182 反思改进·建议池：按 (asset × action × param_key × direction) 累计达到阈值后的候选项 ──
# 状态机：pending → approved → applied（写 strategy_version / paper_trading_settings）
#                     ↘ rejected（不再升级，never re-activate）
_CREATE_STRATEGY_PARAM_ADJUSTMENTS = """
    CREATE TABLE IF NOT EXISTS strategy_param_adjustments (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        asset        TEXT    NOT NULL DEFAULT '*',     -- 资产类 / '*' = 全局
        action       TEXT    NOT NULL DEFAULT '*',     -- BUY/SELL/'*'
        param_key    TEXT    NOT NULL DEFAULT '',      -- 建议参数 key（buy_min_score/tp1_mult/...）
        direction    TEXT    NOT NULL DEFAULT '',      -- up / down / on / off
        magnitude    REAL    DEFAULT NULL,             -- 建议调整幅度 0~1
        sample_size  INTEGER NOT NULL DEFAULT 0,       -- 累计样本笔数
        avg_confidence REAL  DEFAULT NULL,             -- 平均置信度 0~1
        evidence_summary TEXT NOT NULL DEFAULT '',     -- 累计证据（最近 N 笔 rationale 拼接，最多 2KB）
        first_suggested_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_suggested_at  TEXT NOT NULL DEFAULT (datetime('now')),
        status       TEXT    NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'applied')),
        applied_strategy_version_id INTEGER DEFAULT NULL,  -- 关联 add_version() 返回 id
        applied_setting_change TEXT DEFAULT '',            -- 关联 settings 改动（key=value,key=value）
        note         TEXT    NOT NULL DEFAULT '',            -- 审批备注 / 拒绝原因
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (asset, action, param_key, direction, status)
    );
"""

_STRATEGY_PARAM_ADJUSTMENTS_COLUMNS: List[Tuple[str, str]] = [
    # 增量列（基线字段已在 _CREATE_*；以下为追加迁移列，预留扩展）
    ("applied_setting_change", "TEXT DEFAULT ''"),
    ("note",                   "TEXT NOT NULL DEFAULT ''"),
]


# v173 决策证据链：每条 ai_decision 用了哪些 raw_news 作证据 + 当时的溯源快照
# 用于 /api/decisions/:id 全文溯源：决策 → 新闻原文 + 平台发布时间 + 抓到时间 + 内容指纹
_CREATE_DECISION_EVIDENCE = """
    CREATE TABLE IF NOT EXISTS decision_evidence (
        decision_id   INTEGER NOT NULL,         -- 关联 ai_decisions.id
        news_id       INTEGER NOT NULL,         -- 关联 raw_news.id
        used_for      TEXT    NOT NULL DEFAULT 'context',  -- context | trigger | confirm | cross_check
        captured_at   INTEGER NOT NULL,         -- 接入时间 (epoch sec)
        published_at_snapshot TEXT NOT NULL DEFAULT '',  -- 接入时这条新闻的"原始发布时间"快照（防新闻后续被更新/删除时丢失）
        fetched_at_snapshot   INTEGER DEFAULT 0,         -- 接入时这条新闻的"我们抓取时间"快照
        content_hash_snapshot TEXT NOT NULL DEFAULT '',  -- 接入时的内容指纹（防篡改）
        PRIMARY KEY (decision_id, news_id, used_for)
    );
"""
_DECISION_EVIDENCE_COLUMNS = []  # 新表无迁移列（v173 一次性落地）


# v174 信号残留表：新闻事件的影响随时间指数衰减（carry-over 长期影响）
# 用于：同事件多波新闻（如非农初值 → 修正值）、政策决议（声明 → 发布会），
# 第一波新闻已经下决策后，第二波新闻要参考"残留影响"避免重复建仓。
#   impact：初始强度（0.0-1.0）
#   decay_lambda：指数衰减系数（单位 1/秒）；λ 越大衰减越快
#   ttl_seconds：TTL（默认 24h），到期直接清；期内残留 = impact * exp(-λ * age)
_CREATE_SIGNAL_STATE = """
    CREATE TABLE IF NOT EXISTS signal_state (
        signal_id     TEXT    PRIMARY KEY,        -- 事件指纹：content_fingerprint / cause_signature
        asset         TEXT    NOT NULL DEFAULT '',  -- 主品种（XAU/BTC/...）
        impact        REAL    NOT NULL DEFAULT 0.0,  -- 初始影响强度（0.0-1.0）
        decay_lambda  REAL    NOT NULL DEFAULT 1.0e-4,  -- 指数衰减系数
        created_at    INTEGER NOT NULL,          -- 创建时刻（epoch sec）
        last_seen_at  INTEGER NOT NULL,          -- 最近一次观察到同指纹的时刻（用于去重 + 触发衰减刷新）
        ttl_seconds   INTEGER NOT NULL DEFAULT 86400,  -- 24h TTL；到期 purger 清掉
        direction     TEXT    NOT NULL DEFAULT 'NEUTRAL',  -- BULL | BEAR | NEUTRAL
        reason        TEXT    NOT NULL DEFAULT ''  -- 残留原因（一句话说明，方便人工审查）
    );
"""
_SIGNAL_STATE_COLUMNS = [
    # v174 一次性完整建表，迁移列预留扩展位
    ("reason", "TEXT NOT NULL DEFAULT ''"),
]

# 用户自定义 AI 分析 prompt 库：启用的条目注入每次 AI 分析的 system prompt（可增删/启停）
_CREATE_AI_PROMPTS = """
    CREATE TABLE IF NOT EXISTS ai_prompts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL DEFAULT '',        -- 显示名
        content    TEXT    NOT NULL,                   -- prompt 内容（注入 AI 分析的指令）
        enabled    INTEGER NOT NULL DEFAULT 1,         -- 1=注入 0=停用
        kind       TEXT    NOT NULL DEFAULT 'custom',  -- 预留：custom / template
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# AI 主 prompt 覆盖表：用户可在页面编辑主 prompt（system_prompt / json_force_prompt /
# market_structure_block / doubao_prompt），存这里覆盖代码内置默认值；无记录时引擎用代码默认。
_CREATE_AI_MAIN_PROMPTS = """
    CREATE TABLE IF NOT EXISTS ai_main_prompts (
        kind       TEXT PRIMARY KEY,  -- system_prompt / json_force_prompt / market_structure_block / doubao_prompt
        name       TEXT NOT NULL DEFAULT '',
        content    TEXT NOT NULL,
        base_version INTEGER NOT NULL DEFAULT 0,  -- 保存时代码内置主 prompt 代际（落后即屏蔽该覆盖）
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# ai_main_prompts 增量列（老库补 base_version：保存时代码内置主 prompt 代际）
_AI_MAIN_PROMPTS_COLUMNS: List[Tuple[str, str]] = [
    ("base_version", "INTEGER NOT NULL DEFAULT 0"),
]

# 品种专项分析规则表：按金融品类（XAU/WTI/BTC/ETH/美股代码…）单独设置分析规则。
# AI 分析时按 target_asset / sibling 品种套用对应规则；asset='all' 为全品种兜底。
_CREATE_AI_ASSET_RULES = """
    CREATE TABLE IF NOT EXISTS ai_asset_rules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        asset      TEXT    NOT NULL,                   -- 品种代码（XAU/WTI/BTC/ETH/NVDA…）或 all=全品种兜底
        asset_name TEXT    NOT NULL DEFAULT '',        -- 显示名（黄金/原油/比特币…，可选）
        content    TEXT    NOT NULL,                   -- 该品种专项分析规则（用户自由输入）
        enabled    INTEGER NOT NULL DEFAULT 1,         -- 1=生效 0=停用
        priority   INTEGER NOT NULL DEFAULT 0,         -- 同品种多条规则排序（小的在前）
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# 因果链库：宏观/市场因果知识条目（如"美元加息 → 黄金/原油/加密承压或走强"）。
# AI 分析新闻时，命中 trigger_words 的启用条目被注入到八段式之后的新段落
# [因果链分析]（因：…；果：…），并引导"总结"与八段式互相印证。
_CREATE_CAUSAL_CHAINS = """
    CREATE TABLE IF NOT EXISTS causal_chains (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL DEFAULT '',   -- 显示名（可选）
        cause         TEXT    NOT NULL,               -- 因：如"美元加息/降息"
        effect        TEXT    NOT NULL,               -- 果：如"黄金/原油/加密承压或走强"
        trigger_words TEXT    NOT NULL DEFAULT '',    -- 触发词（逗号/空格分隔）：新闻命中才注入
        note          TEXT    NOT NULL DEFAULT '',    -- 备注（可选）
        enabled       INTEGER NOT NULL DEFAULT 1,     -- 1=参与注入 0=停用
        origin        TEXT    NOT NULL DEFAULT 'manual',  -- manual=手工添加 / auto=复盘自动沉淀
        weight        REAL    NOT NULL DEFAULT 1,     -- 权重：越大在新闻分析中占比越大（命中排序优先+注入标注）
        mechanism     TEXT    NOT NULL DEFAULT '',    -- 传导机理：A 为什么会造成 B（中间物理/金融机理）
        precondition  TEXT    NOT NULL DEFAULT '',    -- 生效前提：什么条件下 A→B 成立
        invalidation  TEXT    NOT NULL DEFAULT '',    -- 失效边界：什么场景下 A→B 失效/反转
        created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# 因果链库增量列（老库补 origin / weight / mechanism / precondition / invalidation）
_CAUSAL_CHAINS_COLUMNS: List[Tuple[str, str]] = [
    ("origin", "TEXT NOT NULL DEFAULT 'manual'"),
    ("weight", "REAL NOT NULL DEFAULT 1"),
    ("mechanism", "TEXT NOT NULL DEFAULT ''"),
    ("precondition", "TEXT NOT NULL DEFAULT ''"),
    ("invalidation", "TEXT NOT NULL DEFAULT ''"),
]

# 品种涨跌驱动因果图谱（问题3）：节点 = 因素/资产/宏观变量/结论（神经元），
# 边 = 因果/条件/证实/证伪（突触，带权重）。由 causal_graph 模块的 5-why 推理循环
# 生成并持续更新（新旧结论互相推理 → 上调置信度或证伪失效，越用越准）。
_CREATE_ASSET_DRIVER_NODES = """
    CREATE TABLE IF NOT EXISTS asset_driver_nodes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset       TEXT    NOT NULL DEFAULT 'all',   -- 归属品种（XAU/WTI/BTC/ETH...）或 all=跨品种宏观
        label       TEXT    NOT NULL,                 -- 因素/节点名（归一化后，用于同义合并）
        kind        TEXT    NOT NULL DEFAULT 'factor',-- asset=品种 / factor=因素 / core=核心驱动 / condition=条件因素
        is_core     INTEGER NOT NULL DEFAULT 0,       -- 1=核心因素（最底层驱动）
        confidence  REAL    NOT NULL DEFAULT 0.5,     -- 置信度（新数据证实上调/证伪下调）
        evidence    TEXT    NOT NULL DEFAULT '',      -- 支撑证据摘要
        version     INTEGER NOT NULL DEFAULT 1,       -- 演进版本（每次被重新推理 +1）
        active      INTEGER NOT NULL DEFAULT 1,       -- 0=已被证伪/失效
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (asset, label)
    );
"""

_CREATE_ASSET_DRIVER_EDGES = """
    CREATE TABLE IF NOT EXISTS asset_driver_edges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id     INTEGER NOT NULL,                 -- 上游节点（因）
        to_id       INTEGER NOT NULL,                 -- 下游节点（果）
        kind        TEXT    NOT NULL DEFAULT 'causes',-- causes=因果 / conditions=条件触发 / supports=证实 / contradicts=证伪
        weight      REAL    NOT NULL DEFAULT 1.0,     -- 边权重（证据越多越强）
        evidence    TEXT    NOT NULL DEFAULT '',
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (from_id, to_id, kind)
    );
"""

# ── v202 段4 · 因果图谱改进候选（KG Improvements）─────────────────────────
# 区别于「模拟盘参数调整」（paper-adjustments）：本表专门管分析层的因果图谱/因果链库
# 的「加边 / 删边 / 改权重 / 启停规则」候选。每条 proposal 由归因复核 / 决策错误码
# / 反思系统自动挖出，人工在 /review 段4 审批后写回 causal_graph.json / causal_chains.json。
#
#   kind   : add_edge | remove_edge | reweight | enable_rule | disable_rule
#   target : "asset_driver_edges#<from>#<to>" 或 "causal_chains#<id>" 之类
#   payload: JSON 字符串，存 kind 需要的参数（weight / reason 等）
#   status : pending → approved (写图谱) | rejected (留作存档)
_KG_PROPOSAL_KINDS = ("add_edge", "remove_edge", "reweight", "enable_rule", "disable_rule")
_KG_PROPOSAL_STATUSES = ("pending", "approved", "rejected")

_CREATE_KG_IMPROVEMENTS = """
    CREATE TABLE IF NOT EXISTS kg_improvements (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT    NOT NULL,                -- 改进动作：见 _KG_PROPOSAL_KINDS
        target        TEXT    NOT NULL DEFAULT '',     -- 操作目标定位串
        reason        TEXT    NOT NULL DEFAULT '',     -- 触发理由（LLM 摘要或人工备注）
        confidence    REAL    NOT NULL DEFAULT 0.0,    -- 0..1 自动挖掘的可信度
        sample_size   INTEGER NOT NULL DEFAULT 0,       -- 支撑样本数
        impact        TEXT    NOT NULL DEFAULT '',      -- 预期效果摘要
        payload       TEXT    NOT NULL DEFAULT '{}',   -- JSON：kind 需要的额外参数
        status        TEXT    NOT NULL DEFAULT 'pending'  -- pending | approved | rejected
            CHECK (status IN ('pending', 'approved', 'rejected')),
        source        TEXT    NOT NULL DEFAULT 'auto',  -- auto=触发标注 / manual=人工 / system=系统
        first_seen_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        resolved_at   TEXT,                            -- 审批/驳回时间
        note          TEXT    NOT NULL DEFAULT '',     -- 审批人备注（驳回原因等）
        applied_at    TEXT,                            -- reconcile worker 应用时间（v302+）
        apply_error   TEXT    NOT NULL DEFAULT ''      -- reconcile worker 应用失败原因
    );
"""
_CREATE_IDX_KG_IMPROVEMENTS_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_kg_improvements_status "
    "ON kg_improvements(status, last_seen_at DESC);"
)

# ── P1：稳定运行可观测性 ──
# Worker 心跳表：每个 worker task（ingest/ai/reflection/forward）每个 tick 写一行。
#   看门狗 / /metrics 扫描 stale_heartbeats → 报警卡死的 worker。
#   last_status=ok / busy / error → 区分"在跑但慢" vs "真死"。
#   last_error 仅当 status=error 时非空。
_CREATE_WORKER_HEARTBEAT = """
    CREATE TABLE IF NOT EXISTS worker_heartbeat (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_name  TEXT    NOT NULL,                -- 'ingest' | 'ai' | 'reflection' | 'forward'
        last_tick_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        last_status  TEXT    NOT NULL DEFAULT 'ok'
            CHECK (last_status IN ('ok', 'busy', 'error')),
        last_error   TEXT    NOT NULL DEFAULT '',
        processed    INTEGER NOT NULL DEFAULT 0,      -- 本轮处理条目数（采样速率观察）
        detail       TEXT    NOT NULL DEFAULT '',     -- JSON：worker 自定义负载摘要
        UNIQUE (worker_name)
    );
"""

# DLQ 表：永久死信队列。Ingest 解析失败 / reflection MAX_RETRY 耗尽 / ai_worker LLM 永久不可达
# 都写一行，原 payload JSON 留痕，便于人工补数 / 周报回顾。
# 注意：DLQ ≠ 重试；DLQ 是"我已经放弃了，重试也救不了"的占位。
_CREATE_DLQ_EVENTS = """
    CREATE TABLE IF NOT EXISTS dlq_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT    NOT NULL,                -- 'ingest_parse' | 'reflection_retry_exhausted' | 'ai_unreachable' | ...
        source       TEXT    NOT NULL DEFAULT '',     -- 出处标识（'ingest:cointelegraph' / 'reflection:trade_reflections' 等）
        payload      TEXT    NOT NULL DEFAULT '',     -- 原 JSON 负载（截断 4KB 防止 DB 膨胀）
        last_error   TEXT    NOT NULL DEFAULT '',     -- 最后一次失败的错误文本
        retry_count  INTEGER NOT NULL DEFAULT 0,      -- 已经重试几次后才到 DLQ
        created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        resolved_at  TEXT,                           -- 人工 / 后续修复后再处理的时间
        resolved     INTEGER NOT NULL DEFAULT 0       -- 0=待处理，1=已恢复
    );
"""

# v175 异常签名（Phase 2）：全板块异动 / 多因子触发的落库历史。
# 触发后由异常链路写入，供「重要新闻判定」与复盘页追溯；status 生命周期：
#   open -> attributed（已完成归因注入）或 dismissed（人工否掉）。
_CREATE_ABNORMAL_SIGNALS = """
    CREATE TABLE IF NOT EXISTS abnormal_signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          INTEGER NOT NULL,               -- 触发 unix 秒（时间锚）
        asset       TEXT    NOT NULL DEFAULT '',    -- 资产；全板块异动记 'SECTOR'
        signal_kind TEXT    NOT NULL DEFAULT '',    -- 'single' | 'multi_factor' | 'sector'
        direction   TEXT    NOT NULL DEFAULT '',    -- bullish | bearish | ''（未明）
        anchor_ts   INTEGER,                        -- 触发 K 线 ts_ms（single/multi 有，sector 为 NULL）
        detail_json TEXT    NOT NULL DEFAULT '{}',  -- 因子详情（factors/rsi/divergence/同向资产…）
        hint        TEXT    NOT NULL DEFAULT '',    -- 归因注入提示语（只在触发时生成）
        status      TEXT    NOT NULL DEFAULT 'open',-- open | attributed | dismissed
        created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# v182.3 Phase 2 异常归因（AI 一句话归因 + 驱动候选 + 跨品种关联 + 行动建议）。
#   - 同一 signal_id 可多次写入（保留多次归因历史），前端默认取最新。
#   - summary 限长 300 字；drivers_json / cross_json 是 JSON 字符串。
#   - severity_hint: 1=轻度 / 2=中度 / 3=严重。
_CREATE_ABNORMAL_ATTRIBUTIONS = """
    CREATE TABLE IF NOT EXISTS abnormal_attributions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id       INTEGER NOT NULL,
        asset           TEXT    NOT NULL DEFAULT '',
        signal_kind     TEXT    NOT NULL DEFAULT '',
        anchor_ts       INTEGER,
        summary         TEXT    NOT NULL DEFAULT '',
        drivers_json    TEXT    NOT NULL DEFAULT '[]',
        cross_json      TEXT    NOT NULL DEFAULT '[]',
        actionable      TEXT    NOT NULL DEFAULT '观望',
        severity_hint   INTEGER NOT NULL DEFAULT 1,
        source_model    TEXT    NOT NULL DEFAULT '',
        used_news_n     INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (signal_id) REFERENCES abnormal_signals(id) ON DELETE CASCADE
    );
"""

_CREATE_IDX_ABN_ATTR_SIGNAL = "CREATE INDEX IF NOT EXISTS idx_abn_attr_signal ON abnormal_attributions(signal_id);"
_CREATE_IDX_ABN_ATTR_ASSET_TS = "CREATE INDEX IF NOT EXISTS idx_abn_attr_asset_ts ON abnormal_attributions(asset, anchor_ts);"

# P0-3 数据源状态落库表（供"信息源监控面板"前端轮询）。
#   current  表：UPSERT 实时状态（online/error/stale/unknown），last_tick_at 给 staleness 判
#   history 表：每次 report() 追加一行（含 detail / latency），保留 7 天滚动窗口
_CREATE_SOURCE_STATUS_CURRENT = """
    CREATE TABLE IF NOT EXISTS source_status (
        source_id         TEXT PRIMARY KEY,
        category          TEXT NOT NULL DEFAULT '',
        label             TEXT NOT NULL DEFAULT '',
        description       TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'unknown'
            CHECK (status IN ('online', 'error', 'stale', 'unknown')),
        last_result       TEXT NOT NULL DEFAULT '',
        last_ok_ts        TEXT,
        last_fail_ts      TEXT,
        last_error        TEXT NOT NULL DEFAULT '',
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        total_ok          INTEGER NOT NULL DEFAULT 0,
        total_fail        INTEGER NOT NULL DEFAULT 0,
        last_latency_ms   REAL,
        last_tick_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        detail            TEXT NOT NULL DEFAULT ''
    );
"""
_CREATE_SOURCE_STATUS_HISTORY = """
    CREATE TABLE IF NOT EXISTS source_status_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id   TEXT NOT NULL,
        ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        ok          INTEGER NOT NULL,
        latency_ms  REAL,
        error       TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT ''
    );
"""
_CREATE_IDX_SOURCE_STATUS_HISTORY = """
    CREATE INDEX IF NOT EXISTS idx_source_status_history_ts
        ON source_status_history(source_id, ts);
"""

# ────────────────────────────────────────────────────────────────────
# 事件结构化 — 字典表（PR-1）
# ────────────────────────────────────────────────────────────────────

_CREATE_DIM_ASSET = """
    CREATE TABLE IF NOT EXISTS dim_asset (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT    NOT NULL UNIQUE,        -- 自然键：'BTC' / 'ETH' / 'XAU' / 'WTI' / 'SPX' / '000300.SS'
        name        TEXT    NOT NULL DEFAULT '',    -- 显示名：'Bitcoin' / '原油' / '沪深300'
        kind        TEXT    NOT NULL DEFAULT 'crypto'  -- crypto | commodity | equity_index | fx | bond
            CHECK (kind IN ('crypto','commodity','equity_index','fx','bond','other')),
        venue       TEXT    NOT NULL DEFAULT '',    -- 主交易场所：'Binance' / 'CME' / 'Sina'
        active      INTEGER NOT NULL DEFAULT 1,     -- 0=停用，1=启用
        created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

_CREATE_DIM_EVENT_TYPE = """
    CREATE TABLE IF NOT EXISTS dim_event_type (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        code            TEXT    NOT NULL UNIQUE,    -- 自然键：'rate_hike' / 'cpi' / 'nfp' / 'opec_cut'
        name_zh         TEXT    NOT NULL DEFAULT '',  -- 中文显示名：'加息'
        name_en         TEXT    NOT NULL DEFAULT '',  -- 英文：'Rate Hike'
        category        TEXT    NOT NULL DEFAULT 'macro'  -- macro | commodity | crypto | geopolitics
            CHECK (category IN ('macro','commodity','crypto','geopolitics','other')),
        -- 规则方向先验 JSON：{"XAU":"利空","BTC":"利空"} — 与 event_struct._DIRECTION_BIAS 对齐
        direction_bias  TEXT    NOT NULL DEFAULT '{}',
        active          INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
"""

# ────────────────────────────────────────────────────────────────────
# 事件结构化 — 实体表（PR-3）
# 注意：现有 news_events 表是「事件流时序」（每行=AI 决策信号），
# 新表 event_atom 是「标准化事件」（每行=客观事件，0~N 个/新闻），语义不同，不复用名。
# ────────────────────────────────────────────────────────────────────

_CREATE_EVENT_ATOM = """
    CREATE TABLE IF NOT EXISTS event_atom (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_news_id             INTEGER NOT NULL,
        event_type_id           INTEGER NOT NULL,
        asset_id                INTEGER NOT NULL DEFAULT 0,
        event_time              INTEGER NOT NULL,
        event_time_confidence   REAL    NOT NULL DEFAULT 1.0,
        event_time_source       TEXT    NOT NULL DEFAULT 'platform_published_at',
        magnitude_value         REAL,
        magnitude_unit          TEXT    NOT NULL DEFAULT '',
        actor                   TEXT    NOT NULL DEFAULT '',
        direction               TEXT    NOT NULL DEFAULT 'unknown'
            CHECK (direction IN ('bullish','bearish','neutral','mixed','unknown')),
        direction_confidence    REAL    NOT NULL DEFAULT 0.0,
        direction_source        TEXT    NOT NULL DEFAULT 'rule_bias',
        evidence_count          INTEGER NOT NULL DEFAULT 1,
        parser_version          TEXT    NOT NULL DEFAULT 'event_struct.v1',
        cluster_id              INTEGER,
        reasoning               TEXT    NOT NULL DEFAULT '',
        created_at              INTEGER NOT NULL,
        UNIQUE (raw_news_id, event_type_id, event_time, asset_id)
    );
"""

_CREATE_EVENT_TIME_CANDIDATES = """
    CREATE TABLE IF NOT EXISTS event_time_candidates (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_news_id     INTEGER NOT NULL,
        candidate_ts    INTEGER NOT NULL,
        tz              TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
        source          TEXT    NOT NULL DEFAULT ''
            CHECK (source IN ('platform_published_at','ai_inferred','cross_net','human_annotated','other')),
        confidence      REAL    NOT NULL DEFAULT 1.0,
        reasoning       TEXT    NOT NULL DEFAULT '',
        is_primary      INTEGER NOT NULL DEFAULT 0,
        parser_version  TEXT    NOT NULL DEFAULT 'event_struct.v1',
        created_at      INTEGER NOT NULL
    );
"""


# ── 事件影响曲线（PR-时间结构化） ─────────────────────────────────────────────
# 把"事件发布后 5m / 30m / 2h / 1d 价格分桶"结构化存储：
# - window_label: '5m' / '30m' / '2h' / '1d' / 'extended'（与 tz_utils.WINDOW_LABELS 对齐）
# - sample_ts: 采样 epoch 秒（UTC epoch，跨时区统一）
# - delta_pct: 该时刻相对事件发布价的变化百分比
# - volume_ratio: 量能比（>1 放量, ≈1 存量）
# 设计目的：复盘 / LLM 归因 / 异常检测的"价格-时间曲线"维度不再需要 join 行情即可得。
_CREATE_EVENT_IMPACT_CURVE = """
    CREATE TABLE IF NOT EXISTS event_impact_curve (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_atom_id   INTEGER NOT NULL,
        asset_id        INTEGER NOT NULL DEFAULT 0,
        window_label    TEXT    NOT NULL
            CHECK (window_label IN ('5m','30m','2h','1d','extended')),
        sample_ts       INTEGER NOT NULL,           -- UTC epoch seconds
        price           REAL,                       -- 该时刻价格（可能为 null 仅存 delta）
        delta_pct       REAL,                       -- 该时刻价格变化百分比（保留 4 位小数）
        volume_ratio    REAL,                       -- 量能比（>1 放量）
        note            TEXT    NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL,
        UNIQUE (event_atom_id, asset_id, window_label, sample_ts)
    );
"""

# v189-7: Coinglass + LSR 中转 API 落库
#   - coinglass_lsr_samples: 每次 watcher 拉到的单币信号原始快照
#   - coinglass_lsr_health:  每次拉取的元信息 (key 剩余次数/过期/拉取耗时)
# 数据来源: api/vip2.coinglass.site/api/lsr/signal/{SYM}?mode=trader|whale
# 模式 (trader/whale): trader=全体散户+大户 / whale=仅大户
# 比率 (ratio): 多头持仓/空头持仓, >1 多头多, <1 空头多
# delta_*_pct: 相对窗口变化 (2m/30m/4h), 三窗口同向即趋势强烈
_CREATE_COINGLASS_LSR_SAMPLES = """
    CREATE TABLE IF NOT EXISTS coinglass_lsr_samples (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,            -- 入库时刻 (epoch s)
        source_ts       REAL    NOT NULL,            -- 服务端 calculated_at (epoch s, 可为浮点)
        updated_at      REAL    NOT NULL,            -- 服务端 updated_at (epoch s)
        cache_age_s     REAL    NOT NULL DEFAULT 0,  -- 缓存新鲜度 (秒, <1s 为新鲜)
        mode            TEXT    NOT NULL,            -- 'trader' | 'whale'
        symbol          TEXT    NOT NULL,            -- 'BTC' | 'ETH' | 'SOL' (裸符号, 不用 BTCUSDT)
        rank            INTEGER NOT NULL DEFAULT 0,  -- 排行榜排名 (1~200+)
        ratio           REAL    NOT NULL DEFAULT 0,  -- 多/空持仓比
        ratio_pct       TEXT    NOT NULL DEFAULT '', -- 文本格式, 例 "+316.68%"
        ratio_30m       REAL    NOT NULL DEFAULT 0,  -- 30 分钟前 ratio (用于斜率)
        delta_2m        REAL    NOT NULL DEFAULT 0,
        delta_2m_pct    TEXT    NOT NULL DEFAULT '',
        delta_30m       REAL    NOT NULL DEFAULT 0,
        delta_30m_pct   TEXT    NOT NULL DEFAULT '',
        delta_4h        REAL    NOT NULL DEFAULT 0,
        delta_4h_pct    TEXT    NOT NULL DEFAULT '',
        oi_mcap_ratio   REAL    NOT NULL DEFAULT 0,
        ov_ratio        REAL    NOT NULL DEFAULT 0,
        ov_long_qty     REAL    NOT NULL DEFAULT 0,
        ov_short_qty    REAL    NOT NULL DEFAULT 0,
        ov_long_avg_entry  REAL NOT NULL DEFAULT 0,
        ov_short_avg_entry REAL NOT NULL DEFAULT 0,
        ov_long_whales_qty  REAL NOT NULL DEFAULT 0,
        ov_short_whales_qty REAL NOT NULL DEFAULT 0,
        ov_total_traders   INTEGER NOT NULL DEFAULT 0,
        ov_long_traders    INTEGER NOT NULL DEFAULT 0,
        ov_short_traders   INTEGER NOT NULL DEFAULT 0,
        ov_long_whales     INTEGER NOT NULL DEFAULT 0,
        ov_short_whales    INTEGER NOT NULL DEFAULT 0,
        ov_long_profit_traders  INTEGER NOT NULL DEFAULT 0,
        ov_short_profit_traders INTEGER NOT NULL DEFAULT 0,
        raw_payload     TEXT    NOT NULL DEFAULT '{}', -- 原始 JSON 备份 (调试用)
        UNIQUE (mode, symbol, source_ts)            -- 同币同源秒级幂等
    );
"""

_COAINGLASS_INDEX_NAME = "idx_coinglass_lsr_symbol_ts"
_COAINGLASS_INDEX_NAME_WHALE = "idx_coinglass_lsr_whale_symbol_ts"
_COINGLASS_LSR_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS {f'{_COAINGLASS_INDEX_NAME}'} "
    f"ON coinglass_lsr_samples(symbol, ts);",
    f"CREATE INDEX IF NOT EXISTS {f'{_COAINGLASS_INDEX_NAME_WHALE}'} "
    f"ON coinglass_lsr_samples(mode, symbol, ts);",
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_mode_ts "
    "ON coinglass_lsr_samples(mode, ts);",
]

# v189-7: Coinglass API 元信息/健康度 (key 剩余次数/过期日/拉取耗时)
_CREATE_COINGLASS_LSR_HEALTH = """
    CREATE TABLE IF NOT EXISTS coinglass_lsr_health (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,            -- 入库时刻 (epoch s)
        endpoint        TEXT    NOT NULL DEFAULT '', -- '/api/lsr/signal/BTC'
        ok              INTEGER NOT NULL DEFAULT 0,  -- 1=ok / 0=fail
        status          INTEGER NOT NULL DEFAULT 0,  -- HTTP 状态码
        latency_ms      INTEGER NOT NULL DEFAULT 0,  -- 本次拉取耗时
        rate_limit      INTEGER NOT NULL DEFAULT 0,  -- 配额 (X-Rate-Limit-Limit-User)
        rate_used       INTEGER NOT NULL DEFAULT 0,  -- 已用
        rate_remaining  INTEGER NOT NULL DEFAULT 0,  -- 剩余
        key_expires_at  TEXT    NOT NULL DEFAULT '', -- 过期日 (ISO8601)
        error_kind      TEXT    NOT NULL DEFAULT '', -- 错误种类 (rate_limited/transient/...)
        error_msg       TEXT    NOT NULL DEFAULT ''  -- 错误摘要
    );
"""
_COINGLASS_LSR_HEALTH_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_health_ts "
    "ON coinglass_lsr_health(ts);",
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_health_ok "
    "ON coinglass_lsr_health(ok, ts);",
]

_CREATE_EVENT_IMPACT_STAT = """
    CREATE TABLE IF NOT EXISTS event_impact_stat (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        date_local          TEXT    NOT NULL,
        date_epoch_start    INTEGER NOT NULL,
        impact_curve_filled INTEGER NOT NULL DEFAULT 0,
        event_atoms_total   INTEGER NOT NULL DEFAULT 0,
        fallback_used       INTEGER NOT NULL DEFAULT 0,
        avg_window_minutes  REAL    NOT NULL DEFAULT 0,
        clock_jumps         INTEGER NOT NULL DEFAULT 0,
        created_at          INTEGER NOT NULL,
        UNIQUE (date_local)
    );
"""

# event_atom 增量列：曲线 snapshot（事件落库时一次性写完的 JSON，访问比 join 快）
_EVENT_ATOM_COLUMNS = [
    ("curve_snapshot", "TEXT NOT NULL DEFAULT ''"),
    # 调度字段：UTC epoch + ISO Z 双轨（跨时区统一，前端展示按 display_tz 切）
    ("event_time_utc_epoch", "INTEGER"),
    ("event_time_utc_iso",   "TEXT NOT NULL DEFAULT ''"),
    ("event_time_display_tz", "TEXT NOT NULL DEFAULT 'Asia/Shanghai'"),
    # v171+ LLM 拒后 fallback 标记
    ("fallback_used", "INTEGER NOT NULL DEFAULT 0"),
]


# ── v201: 策略级反思（LLM 每周调 BuySellKpi + trade_reflections_v2 摘要 → 模式/建议） ──
# 状态机：pending → approved (应用 strategy_versions + paper_trading_settings) | rejected | expired
_CREATE_STRATEGY_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS strategy_reflections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        period_start TEXT   NOT NULL DEFAULT '',
        period_end  TEXT    NOT NULL DEFAULT '',
        period_days INTEGER NOT NULL DEFAULT 30,
        sample_size INTEGER NOT NULL DEFAULT 0,
        source_summary_json TEXT NOT NULL DEFAULT '',
        llm_summary         TEXT NOT NULL DEFAULT '',
        strategy_patterns_json TEXT NOT NULL DEFAULT '[]',
        recommendations_json   TEXT NOT NULL DEFAULT '[]',
        affected_dimension  TEXT DEFAULT '',
        evidence_summary    TEXT DEFAULT '',
        overall_confidence  REAL DEFAULT 0.5,
        status       TEXT    NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
        admin_note          TEXT DEFAULT '',
        approved_at         TEXT DEFAULT '',
        applied_strategy_version_id INTEGER DEFAULT NULL,
        llm_model_id        TEXT DEFAULT '',
        UNIQUE (period_start, period_end)
    );
"""
_STRATEGY_REFLECTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_strategy_reflections_status ON strategy_reflections(status, created_at DESC)",
]

# ── v201: 宏观事件 × K线影响（每 5min worker 写） ──
# impact_pct = max(|max_high - pre_close|, |pre_close - min_low|) / pre_close
# severity 阈值：>=0.008 high; >=0.003 medium; <0.003 low
# UNIQUE (event_source, event_id, asset) 幂等防重写
_CREATE_EVENT_KLINE_IMPACT = """
    CREATE TABLE IF NOT EXISTS event_kline_impact (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_source    TEXT    NOT NULL DEFAULT '',
        event_id        TEXT    NOT NULL DEFAULT '',
        event_title     TEXT    NOT NULL DEFAULT '',
        event_time      INTEGER NOT NULL DEFAULT 0,
        importance      TEXT    DEFAULT '',
        asset           TEXT    NOT NULL DEFAULT '',
        pre_event_close REAL    DEFAULT NULL,
        max_high        REAL    DEFAULT NULL,
        min_low         REAL    DEFAULT NULL,
        impact_pct      REAL    NOT NULL DEFAULT 0.0,
        impact_window_start INTEGER NOT NULL DEFAULT 0,
        impact_window_end   INTEGER NOT NULL DEFAULT 0,
        impact_severity TEXT    NOT NULL DEFAULT 'low'
            CHECK (impact_severity IN ('low', 'medium', 'high')),
        direction       TEXT    DEFAULT ''
            CHECK (direction IN ('up', 'down', 'neutral')),
        processed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (event_source, event_id, asset)
    );
"""
_EVENT_KLINE_IMPACT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_event_kline_impact_asset_time ON event_kline_impact(asset, event_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_event_kline_impact_severity ON event_kline_impact(impact_severity, event_time DESC)",
]


# ── v202: 宏观日历提前 2h + 即时提醒 状态表 ────────────────────────────────────
# 用途: macro_calendar_notifier worker 每 5min 扫描金十财经日历, 对 star≥3 的
#       事件提前 2h 推一次 + 事件当刻再推一次 (走 send_important_news →
#       Server酱/PushPlus/SMTP 三通道, 复用现有通知渠道).
# 幂等键: UNIQUE (event_id, window) 同一事件同一窗口 (lead_2h / at_time) 不重复推送.
# event_id = jin10-cal-{title}-{pub_time[:16]}, 与 jin10_mcp.calendar_events 稳定一致.
_CREATE_MACRO_NOTIFY_STATE = """
    CREATE TABLE IF NOT EXISTS macro_notify_state (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     TEXT    NOT NULL DEFAULT '',
        event_title  TEXT    NOT NULL DEFAULT '',
        event_time   INTEGER NOT NULL DEFAULT 0,  -- 秒级 UTC
        star         INTEGER NOT NULL DEFAULT 0,
        window       TEXT    NOT NULL DEFAULT 'lead_2h'
            CHECK (window IN ('lead_2h', 'at_time')),
        sent_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        channel      TEXT    NOT NULL DEFAULT 'all',  -- all / serverchan / mail / pushplus
        UNIQUE (event_id, window)
    );
"""
_MACRO_NOTIFY_STATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_macro_notify_state_time ON macro_notify_state(event_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_macro_notify_state_window ON macro_notify_state(window, sent_at DESC)",
]


# ── v170: 多源宏观日历存储（ForexFactory + jin10）───────────────────────────────
# 用途: macro_calendar_ff / macro_calendar_notifier 同步落库，
#       /api/macro/calendar 直接查这里出图（多源 cross-check）。
# 幂等键: UNIQUE (source, event_id) → 同一源同一事件不重复。
# event_id = source 内部稳定 ID（FF: ymd+title+country 哈希；jin10: cal-{title}-{pub_time[:16]}）。
# impact: high / med / low（FF 原始枚举；jin10 star 3+ → high / 2 → med / 1 → low）。
_CREATE_MACRO_CALENDAR = """
    CREATE TABLE IF NOT EXISTS macro_calendar (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT    NOT NULL DEFAULT 'jin10',  -- 'ff' | 'jin10'
        event_id     TEXT    NOT NULL DEFAULT '',
        country      TEXT    NOT NULL DEFAULT '',       -- FF: USD/EUR/CNY/JPY...; jin10: 国家名
        title        TEXT    NOT NULL DEFAULT '',
        impact       TEXT    NOT NULL DEFAULT 'low',     -- 'high' / 'med' / 'low' (FF 原始枚举；jin10 映射：star≥3→high, 2→med, 1→low)
        star         INTEGER NOT NULL DEFAULT 0,         -- 1-5 颗星 (v215+: FF impact 映射为 4/3/2; jin10 原值)
        event_time   INTEGER NOT NULL DEFAULT 0,         -- 秒级 UTC
        forecast     TEXT    NOT NULL DEFAULT '',
        previous     TEXT    NOT NULL DEFAULT '',
        actual       TEXT    NOT NULL DEFAULT '',
        fetched_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, event_id)
    );
"""
_MACRO_CALENDAR_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_macro_calendar_time ON macro_calendar(event_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_macro_calendar_source ON macro_calendar(source, event_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_macro_calendar_star ON macro_calendar(star DESC, event_time DESC)",
]

# v215: macro_calendar 加 star INTEGER 1-5 颗星 (老库 ALTER 自动补列)
_MACRO_CALENDAR_V215_COLUMNS = [
    ("star", "INTEGER NOT NULL DEFAULT 0"),
]


# v231+ (H4): quality_rejections 表 - 拦截持久化.
# 每次 pre_trade_check 返回 is_valid=False 时, 把每个触发规则 INSERT 一行.
# 设计要点:
#   - rule 单值 (R1/R2/R4/R7/R8/R9) 便于 GROUP BY 统计
#   - reason 完整文本 (如 'R7:confidence=0.0<20.0') 便于人工解读
#   - payload_json 存关键字段快照, 便于事后 forensic (不带 raw_news.content 等大字段)
#   - UNIQUE(decision_id, rule) 幂等: 同一 decision 被同一 rule 拦多次不重复
#   - filter_version 记录跑的是哪版 filter, 未来 filter 演进时区分历史数据
#   - created_at 是 audit 时间, 不是 decision 创建时间 (decision 创建时间在 payload_json 里)
_CREATE_QUALITY_REJECTIONS = """
    CREATE TABLE IF NOT EXISTS quality_rejections (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id     INTEGER NOT NULL,
        rule            TEXT    NOT NULL,            -- 'R1' / 'R2' / 'R4' / 'R7' / 'R8' / 'R9'
        reason          TEXT    NOT NULL,            -- 完整 reason (e.g. 'R7:confidence=0.0<20.0')
        payload_json    TEXT    NOT NULL DEFAULT '{}',  -- decision 关键字段快照
        filter_version  TEXT    NOT NULL DEFAULT 'v231+',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE(decision_id, rule)
    );
"""
_QUALITY_REJECTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_qr_rule ON quality_rejections(rule, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qr_decision ON quality_rejections(decision_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qr_created ON quality_rejections(created_at DESC)",
]


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    columns: List[Tuple[str, str]]) -> None:
    """ALTER TABLE ADD COLUMN for each missing column. Idempotent by design.

    顺序无关：若表尚不存在则直接跳过。migrate() 中部分表（如 decision_errors、
    ai_call_stats）是在尾部 `for stmt in _INDEXES` 里才 CREATE 的，其 CREATE TABLE
    已含最新列，无需 ALTER；只有当表已存在（老库）却缺列时才 ALTER 补齐。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, col_def in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")


def _upgrade_strategy_versions_source_check(conn: sqlite3.Connection) -> None:
    """v182: SQLite 不能 ALTER CHECK 约束，本函数检测 source 是否允许 'reflection'，
    不允许就以「重命名+重建+复制」的方式重建表，让 reflection 源能落库。

    重建后保留：所有 strategy_versions 行 + 索引（如存在）。
    """
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_versions'"
        ).fetchone()
        if not rows:
            return
        sql = str(rows[0] or "")
        if "'reflection'" in sql:
            return  # 已是新版本，无需升级
        print("[DB] v182: 升级 strategy_versions source CHECK → 允许 'reflection'", flush=True)
        # 1. 旧表重命名
        conn.execute("ALTER TABLE strategy_versions RENAME TO strategy_versions__legacy")
        # 2. 重建（应用 _CREATE_STRATEGY_VERSIONS）
        conn.execute(_CREATE_STRATEGY_VERSIONS)
        # 3. 复制所有行
        cols = [r[1] for r in conn.execute("PRAGMA table_info(strategy_versions__legacy)")]
        # 新表 id/strategy_id/version/params/source/note/created_at 这些列都在；安全拷贝全部
        col_list = ", ".join(cols)
        conn.execute(
            f"INSERT INTO strategy_versions ({col_list}) SELECT {col_list} FROM strategy_versions__legacy"
        )
        # 4. 删除旧表
        conn.execute("DROP TABLE strategy_versions__legacy")
    except Exception as exc:
        print(f"[DB] v182: upgrade strategy_versions CHECK 失败: {exc}", flush=True)


def schema_ready(conn: sqlite3.Connection) -> bool:
    """快速检查 schema 是否已完整（所有表 + 增量列存在）。只读，不申请写锁。

    引擎高频写库时，启动期调用方（如 api_server._migrate_schema）可用它跳过
    整个 migrate，避免在写锁竞争下等待 busy_timeout。
    """
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "raw_news", "ai_decisions", "market_ticks", "factor_snapshots",
            "news_events", "signal_performance", "strategies", "strategy_versions",
            "paper_trading_settings", "paper_trading_runs", "quick_sim_trades",
            "hermes_observations", "hermes_skills", "macro_snapshots",
            "backtest_runs", "review_reports", "review_insights",
            "news_trade_reviews", "ai_prompts", "ai_main_prompts",
            "causal_chains", "ai_asset_rules", "attribution_reviews",
            # P1 可观测性表
            "worker_heartbeat", "dlq_events",
            # v173 结构化校验真实性飞轮 + 决策证据链（漏检会导致 migrate 被跳过、表不落库）
            "structured_reflections", "decision_evidence",
            # v175 Phase 2 异常签名（漏检会导致 multi_factor / sector_anomaly 永不落库）
            "abnormal_signals",
            # v182.3 Phase 2 异常归因（漏检会导致 multi_factor AI attribution 永不落库）
            "abnormal_attributions",
            # v188 反思系统（漏检会导致 paper_reflection_worker / gate_reflection_worker 静默崩）
            "experience_entries", "experience_applications",
            "gate_reflections_cycle", "trade_reflections_v2",
            "decision_layer_signals", "reverse_attributions",
            # v189-2 反思系统每日归档（漏检会导致 reflection_cycle_log / daily_journal 永不落库）
            "reflection_cycle_log", "reflection_daily_journal",
            "experience_application_daily",
            # v189-1-补 每日重置登记表（与原 backtest_runs 共存）
            "backtest_daily_runs",
            # v202 宏观日历提前/即时通知 状态表（漏检会导致 macro_calendar_notifier 静默崩）
            "macro_notify_state",
        }
        if not required.issubset(tables):
            return False

        def _cols(table: str) -> set:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

        if not {"ts", "is_noise", "relevance_score", "published_at"}.issubset(_cols("raw_news")):
            return False
        # 全网溯源指纹：缺列会跳过 migrate，导致 ai_worker 落库/API 透传报错
        if not {"content_fingerprint", "first_seen_ts"}.issubset(_cols("raw_news")):
            return False
        # v170 全网溯源结构化字段：缺列会跳过 migrate，导致 ai_worker 5 层校验落库失败
        if not {"cross_net_first_seen_ts", "cross_net_source"}.issubset(_cols("raw_news")):
            return False
        # v171 事件影响曲线 + event_atom 时间双轨字段
        if "event_impact_curve" not in tables:
            return False
        if not {"curve_snapshot", "event_time_utc_epoch",
                "event_time_utc_iso", "event_time_display_tz"}.issubset(_cols("event_atom")):
            return False
        if "decision_context" not in _cols("ai_decisions"):
            return False
        # ai_decisions 增量列必须齐全，否则快速路径会跳过 migrate 导致缺列查询报错
        if not {"evidence_confidence", "evidence_action", "trade_gate_reason",
                "no_trade_reason", "heat_score", "impact_score",
                "asset_impact", "peer_reviews", "is_sibling", "asset_reasoning",
                "low_impact",
                # 需求5 新增子模块输出（漏检会导致老库跳过 migrate、缺列查询报错）
                "attention_score", "elasticity_window", "data_anchor_ts",
                "hard_data_evidence", "event_struct",
                # v181 结构化产出摘要（缺列会跳过 migrate 导致 INSERT 报错）
                "structured_summary"}.issubset(_cols("ai_decisions")):
            return False
        if "source_samples" not in _cols("review_insights") or "origin" not in _cols("review_insights"):
            return False
        if "origin" not in _cols("causal_chains") or "weight" not in _cols("causal_chains"):
            return False
        if not {"max_price", "min_price"}.issubset(_cols("quick_sim_trades")):
            return False
        if "market_structure_enabled" not in _cols("paper_trading_settings"):
            return False
        if "news_export_settings" not in _cols("paper_trading_settings"):
            return False
        if "base_version" not in _cols("ai_main_prompts"):
            return False
        if not {"core_impact", "reasons_json"}.issubset(_cols("attribution_reviews")):
            return False
        return True
    except sqlite3.Error:
        return False


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the full schema to `conn`. Safe to call on every startup."""
    conn.execute(_CREATE_RAW_NEWS)
    conn.execute(_CREATE_AI_DECISIONS)
    conn.execute(_CREATE_MARKET_TICKS)
    conn.execute(_CREATE_FACTOR_SNAPSHOTS)
    conn.execute(_CREATE_NEWS_EVENTS)
    conn.execute(_CREATE_SIGNAL_PERFORMANCE)
    conn.execute(_CREATE_BACKTEST_EVENT_OUTCOMES)  # PR-5 baseline 缓存
    # v20251014: 信号权重评估系统
    conn.execute(_CREATE_SIGNAL_WEIGHT_HISTORY)
    conn.execute(_CREATE_SOURCE_STATS)
    conn.execute(_CREATE_SIGNAL_WEIGHT_CRON_LOG)
    # v20261023: source_stats 幂等水位。该决策的结算结果是否已被计入 source_stats。
    # 无此列时定时回写会反复累加同一批决策，sample_n 虚增、命中率失真。
    _ensure_columns(conn, "ai_decisions", [
        ("source_stats_counted_ts", "INTEGER NOT NULL DEFAULT 0"),
    ])
    conn.execute(_CREATE_IDX_SWH_DECISION)
    conn.execute(_CREATE_IDX_SWH_COMPUTED)
    conn.execute(_CREATE_IDX_SS_SOURCE)
    conn.execute(_CREATE_STRATEGIES)
    conn.execute(_CREATE_STRATEGY_VERSIONS)
    conn.execute(_CREATE_PAPER_TRADING_SETTINGS)
    conn.execute(_CREATE_PAPER_TRADING_RUNS)
    conn.execute(_CREATE_QUICK_SIM_TRADES)
    conn.execute(_CREATE_HERMES_OBSERVATIONS)
    conn.execute(_CREATE_HERMES_SKILLS)
    conn.execute(_CREATE_MACRO_SNAPSHOTS)
    conn.execute(_CREATE_BACKTEST_RUNS)
    conn.execute(_CREATE_REVIEW_REPORTS)
    conn.execute(_CREATE_REVIEW_INSIGHTS)
    conn.execute(_CREATE_NEWS_TRADE_REVIEWS)
    conn.execute(_CREATE_STRUCTURED_REFLECTIONS)
    conn.execute(_CREATE_ATTRIBUTION_REVIEWS)
    conn.execute(_CREATE_FILTER_KEYWORDS)
    conn.execute(_CREATE_TRADE_REFLECTIONS)
    conn.execute(_CREATE_DECISION_EVIDENCE)   # v173: 决策证据链（全文溯源）
    conn.execute(_CREATE_SIGNAL_STATE)        # v174: carry-over 信号残留表
    conn.execute(_CREATE_AI_PROMPTS)
    conn.execute(_CREATE_AI_MAIN_PROMPTS)
    # v223: Belief Management System (worldview -> chain)
    try:
        import belief_store
        belief_store._ensure_beliefs_schema(conn)
    except Exception as _exc:
        print(f"[migrate] belief_store schema failed: {_exc}", flush=True)
    conn.execute(_CREATE_AI_ASSET_RULES)
    conn.execute(_CREATE_CAUSAL_CHAINS)
    conn.execute(_CREATE_ASSET_DRIVER_NODES)
    conn.execute(_CREATE_ASSET_DRIVER_EDGES)
    # v202 段4：因果图谱改进候选
    conn.execute(_CREATE_KG_IMPROVEMENTS)
    conn.execute(_CREATE_IDX_KG_IMPROVEMENTS_STATUS)
    conn.execute(_CREATE_KLINE_BARS)
    conn.execute(_CREATE_KLINE_SNAPSHOTS)
    # v176.2: 市场风格持久化 + 决策反思表
    conn.execute(_CREATE_MARKET_STYLE_SNAPSHOT)
    conn.execute(_CREATE_DECISION_REFLECTION)
    conn.execute(_CREATE_WORKER_HEARTBEAT)   # P1: worker heartbeat
    conn.execute(_CREATE_DLQ_EVENTS)          # P1: dead letter queue
    conn.execute(_CREATE_ABNORMAL_SIGNALS)    # v175: 异常签名（Phase 2 全板块异动/多因子）
    conn.execute(_CREATE_ABNORMAL_ATTRIBUTIONS)  # v182.3: 异常归因（AI 一句话归因+驱动+跨品种）
    conn.execute(_CREATE_IDX_ABN_ATTR_SIGNAL)
    conn.execute(_CREATE_IDX_ABN_ATTR_ASSET_TS)
    conn.execute(_CREATE_SOURCE_STATUS_CURRENT)   # P0-3: 数据源状态落库
    conn.execute(_CREATE_SOURCE_STATUS_HISTORY)   # P0-3: 数据源历史流水（保留 7d）
    conn.execute(_CREATE_IDX_SOURCE_STATUS_HISTORY)
    # 事件结构化（PR-1 字典表 + PR-3 实体表）— 独立于旧表，additive
    conn.execute(_CREATE_DIM_ASSET)
    conn.execute(_CREATE_DIM_EVENT_TYPE)
    conn.execute(_CREATE_EVENT_ATOM)
    conn.execute(_CREATE_EVENT_TIME_CANDIDATES)
    conn.execute(_CREATE_EVENT_IMPACT_CURVE)   # v171: 事件影响曲线（5m/30m/2h/1d）
    conn.execute(_CREATE_EVENT_IMPACT_STAT)    # v171+ 监控：每日 stat 表
    # v188: 反思系统 4 张缺失表 DDL + 2 张新表（必须放在 migrate() 内，
    # 否则新库直接 OperationalError 让 worker 全静默崩)
    conn.execute(_CREATE_EXPERIENCE_ENTRIES)
    conn.execute(_CREATE_EXPERIENCE_APPLICATIONS)
    conn.execute(_CREATE_GATE_REFLECTIONS_CYCLE)
    conn.execute(_CREATE_TRADE_REFLECTIONS_V2)
    conn.execute(_CREATE_DECISION_LAYER_SIGNALS)
    conn.execute(_CREATE_REVERSE_ATTRIBUTIONS)
    # v232.6 P1: 反思→policy 闭环 (policy_patch_queue + sibling_diff_rules)
    # 设计: 候选生成 zero-risk, apply 受控(需要人工审)
    conn.execute(_CREATE_POLICY_PATCH_QUEUE)
    conn.execute(_CREATE_IDX_PPQ_STATUS)
    conn.execute(_CREATE_IDX_PPQ_PENDING)
    conn.execute(_CREATE_SIBLING_DIFF_RULES)
    conn.execute(_CREATE_IDX_SDR_STATUS)
    conn.execute(_CREATE_IDX_SDR_FINGERPRINT)
    # v270 P2: 数据源健康日志 — 多源 fallback 跟踪
    conn.execute(_CREATE_FEED_HEALTH)
    conn.execute(_CREATE_IDX_FEED_HEALTH_ASSET_TS)
    conn.execute(_CREATE_IDX_FEED_HEALTH_KIND)
    conn.execute(_CREATE_IDX_FEED_HEALTH_STATUS)
    # v1 重构：未归类事件表（人工复核 / 规则补全队列）
    conn.execute(_CREATE_UNCLASSIFIED_EVENTS)
    conn.execute(_CREATE_IDX_UNCLASSIFIED_PENDING)
    conn.execute(_CREATE_IDX_UNCLASSIFIED_NEWS)
    _ensure_columns(conn, "event_atom", _EVENT_ATOM_COLUMNS)
    _ensure_columns(conn, "review_insights", _REVIEW_INSIGHTS_COLUMNS)
    # v200+: 显式写入 is_running=1 + 全赛道默认 (替代纯依赖 schema DEFAULT,
    # 避免未来某次 ALTER 改了默认值导致 fresh DB 起来后反思链又断)。
    # 注意: 此处仅保证"行存在 + 默认值合理"; 运行时是否开启仍以 ensure_default_running 为准。
    conn.execute(
        "INSERT OR IGNORE INTO paper_trading_settings "
        "(id, is_running, tracks, gate_enabled, news_enabled, news_trade_max_age_seconds, "
        " tp_enabled, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio, sl_mult, "
        " buy_min_score, sell_min_score, market_structure_enabled) "
        "VALUES (1, 1, ?, 1, 1, 2700, 1, 0.6, 1.0, 0.5, 0.3, 0.5, 0.4, 0.3, 1)",
        (json.dumps(["crypto", "gold", "oil", "stocks"]),),
    )

    # Backfill columns on databases created by older schema versions
    _ensure_columns(conn, "raw_news", _RAW_NEWS_COLUMNS)
    # v3xx 修复(v300-audit): 补回 news_events 丢失的 headline/body/assets/fetched_at。
    # 缺这几列时：SSE 事件流抛 "no such column: headline"，三条新闻入库全部 INSERT 失败，
    # 因果图谱 news_to_move 恒为空。
    _ensure_columns(conn, "news_events", _NEWS_EVENTS_LEGACY_COLUMNS)
    _ensure_columns(conn, "ai_decisions", _AI_DECISIONS_COLUMNS)
    # v3xx 修复: ai_decisions.status 未列入 _AI_DECISIONS_COLUMNS, 旧库缺该列,
    # 导致尾部 `_INDEXES` 建 idx_ai_decisions_status 时报
    # "no such column: status" 而整段 migrate 回滚(连带 attribution_reviews 等
    # 69 张表建不出来)。SQLite 的 ALTER TABLE ADD COLUMN 不支持带 CHECK 约束,
    # 故此处只补 DEFAULT + NOT NULL, 取值域由写侧保证。
    _ensure_columns(conn, "ai_decisions", [("status", "TEXT NOT NULL DEFAULT 'UNREAD'")])
    _ensure_columns(conn, "strategies", _STRATEGIES_COLUMNS)
    _ensure_columns(conn, "paper_trading_settings", _PAPER_SETTINGS_COLUMNS)
    _ensure_columns(conn, "causal_chains", _CAUSAL_CHAINS_COLUMNS)
    _ensure_columns(conn, "ai_main_prompts", _AI_MAIN_PROMPTS_COLUMNS)
    _ensure_columns(conn, "quick_sim_trades", _QUICK_SIM_TRADES_COLUMNS)
    _ensure_columns(conn, "attribution_reviews", [
        ("core_impact", "TEXT DEFAULT ''"),
        ("reasons_json", "TEXT DEFAULT ''"),
        ("failing_sources", "TEXT DEFAULT ''"),  # v222: 失败源清单 JSON 数组
    ])
    _ensure_columns(conn, "trade_reflections", _TRADE_REFLECTIONS_COLUMNS)
    _ensure_columns(conn, "decision_evidence", _DECISION_EVIDENCE_COLUMNS)
    _ensure_columns(conn, "signal_state", _SIGNAL_STATE_COLUMNS)
    # v188: 经验自适应闭环新增字段（lift / applied_win_rate / control_win_rate）
    _ensure_columns(conn, "experience_entries", _EXPERIENCE_ENTRIES_V188_COLUMNS)
    _ensure_columns(conn, "experience_applications", _EXPERIENCE_APPLICATIONS_V188_COLUMNS)
    # v188: trade_reflections_v2 加 llm_depth_reflection 列
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V188_COLUMNS)
    # v190: trade_reflections_v2 加 ontology_signal 拼合列 + 索引
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V190_COLUMNS)
    for _idx_sql in _TRADE_REFLECTIONS_V2_V190_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] trv2_v190 idx failed: {_e}", flush=True)
    # v200 P0+ 严谨兜底: ai_decisions + trade_reflections_v2 + decision_layer_signals 加完整性/source 列
    _ensure_columns(conn, "ai_decisions", _AI_DECISIONS_V200_COMPLETENESS_COLUMNS)
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V200_SOURCE_BREAKDOWN_COLUMNS)
    _ensure_columns(conn, "decision_layer_signals", _DECISION_LAYER_SIGNALS_V200_SOURCE_COUNT_COLUMNS)
    # v230 反思深度增强: K线/跨资产/抢跑/运气 vs 信号/自循环 13 个新列 + 2 张新表
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_DEEP_COLUMNS)
    # v232 反思"核心本质"内容审计: 6 层新列 (链段审计/反方证据/真伪评分/跨决策模式/
    # 判断 vs 执行/反思自审/综合评级). 由 reflection_core.enrich_reflection_core() 写入.
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_CORE_COLUMNS)
    # v232.5 反思「数字落地」+「自检可信度」 — 让反思产出不再空话。
    # 用户反馈 2026-10-06：反思不够深入, root_cause/lesson 没数字, LLM 反思也没引数字。
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V2325_GROUNDING_COLUMNS)
    # v231 反思「逻辑分析」 — reasoning_path 形式逻辑有效性 (6 detectors: E028-E033).
    # 落库列: logic_integrity_score / logic_findings_json / logic_checked_at.
    # 由 reflection_logic_analysis.enrich_reflection() 写入.
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V231_LOGIC_COLUMNS)
    # v302+ reconcile worker: 跟踪 kg_improvements.approved 行的实际应用状态
    _ensure_columns(conn, "kg_improvements", [
        ("applied_at",  "TEXT"),
        ("apply_error", "TEXT NOT NULL DEFAULT ''"),
    ])
    conn.execute(_CREATE_REFLECTION_CYCLES)
    for _idx_sql in _REFLECTION_CYCLES_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] reflection_cycles idx failed: {_e}", flush=True)
    # v231+ watermark transparency: 区分 demo / live / stress_test 写入
    _ensure_columns(conn, "reflection_cycles", [
        ("source_kind",      "TEXT NOT NULL DEFAULT 'live'"),
        ("source_run_id",    "TEXT NOT NULL DEFAULT ''"),
        ("source_watermark", "TEXT NOT NULL DEFAULT ''"),
    ])
    conn.execute(_CREATE_KLINE_DECISION_CACHE)
    for _idx_sql in _KLINE_DECISION_CACHE_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] kline_at_decision_cache idx failed: {_e}", flush=True)
    # v200 P0+ monitor_llm_value 表(LLM 增量价值周期评估)
    conn.execute(_CREATE_MONITOR_LLM_VALUE)
    for _idx_sql in _MONITOR_LLM_VALUE_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] monitor_llm_value idx failed: {_e}", flush=True)
    # v188 P1-5: decision_layer_signals 复合索引
    for _idx_sql in _DECISION_LAYER_SIGNALS_V188_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] dls idx failed: {_e}", flush=True)
    # v188 P1-4: missed_signal 扫描状态表
    conn.execute(_CREATE_MISSED_SIGNAL_SCAN_STATE)
    for _idx_sql in _MISSED_SIGNAL_SCAN_STATE_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] msss idx failed: {_e}", flush=True)
    # v189 Item 8: decision_errors 加 weight 列（老库 backfill）
    try:
        _ensure_columns(conn, "decision_errors", _DECISION_ERRORS_V189_COLUMNS)
    except Exception as _e:
        print(f"[MIGRATE] decision_errors weight col failed: {_e}", flush=True)
    # v189-1: 给 4 张主表加 run_id 列（backtest_runs 原表已由上方创建，勿在此重建）
    for _tname, _cols in _BACKTEST_V189_COLUMNS:
        try:
            _ensure_columns(conn, _tname, _cols)
        except Exception as _e:
            print(f"[MIGRATE] {_tname} run_id col failed: {_e}", flush=True)
    for _idx_sql in _BACKTEST_V189_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] v189 run_id idx failed: {_e}", flush=True)
    # v189-2: 反思系统每日归档 3 张表
    conn.execute(_CREATE_REFLECTION_DAILY_JOURNAL)
    for _idx_sql in _REFLECTION_DAILY_JOURNAL_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] rdj idx failed: {_e}", flush=True)
    conn.execute(_CREATE_REFLECTION_CYCLE_LOG)
    for _idx_sql in _REFLECTION_CYCLE_LOG_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] rcl idx failed: {_e}", flush=True)
    conn.execute(_CREATE_EXPERIENCE_APPLICATION_DAILY)
    for _idx_sql in _EXPERIENCE_APPLICATION_DAILY_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] ead idx failed: {_e}", flush=True)
    # v189-1-补: 每日重置登记表 (与旧 backtest_runs 共存)
    conn.execute(_CREATE_BACKTEST_DAILY_RUNS)
    for _idx_sql in _BACKTEST_DAILY_RUNS_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] bdr idx failed: {_e}", flush=True)
    # v182：先 CREATE 新表，再 ensure 增量列（保证表存在时列也能 extends）
    conn.execute(_CREATE_STRATEGY_PARAM_ADJUSTMENTS)
    _ensure_columns(conn, "strategy_param_adjustments", _STRATEGY_PARAM_ADJUSTMENTS_COLUMNS)
    _upgrade_strategy_versions_source_check(conn)  # v182: 让 source CHECK 允许 'reflection'
    # v189-7: Coinglass + LSR 中转 API 落库
    conn.execute(_CREATE_COINGLASS_LSR_SAMPLES)
    for _idx_sql in _COINGLASS_LSR_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] coinglass_lsr idx failed: {_e}", flush=True)
    conn.execute(_CREATE_COINGLASS_LSR_HEALTH)
    for _idx_sql in _COINGLASS_LSR_HEALTH_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] coinglass_lsr_health idx failed: {_e}", flush=True)

    # v201: 策略级反思 + 宏观 K线影响表
    conn.execute(_CREATE_STRATEGY_REFLECTIONS)
    for _idx_sql in _STRATEGY_REFLECTIONS_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] strategy_reflections idx failed: {_e}", flush=True)
    conn.execute(_CREATE_EVENT_KLINE_IMPACT)
    for _idx_sql in _EVENT_KLINE_IMPACT_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] event_kline_impact idx failed: {_e}", flush=True)

    # v190: 实盘交易 audit_log（trade_executor 每次处理决策都落一条）
    conn.execute(_CREATE_TRADE_AUDIT_LOG)
    for _idx_sql in _TRADE_AUDIT_LOG_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] trade_audit_log idx failed: {_e}", flush=True)

    # v191: 每日归档 3 张表 — daily_reset_log / signal_chain_archive / daily_reflection_summary
    conn.execute(_CREATE_DAILY_RESET_LOG)
    for _idx_sql in _DAILY_RESET_LOG_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] daily_reset_log idx failed: {_e}", flush=True)
    conn.execute(_CREATE_SIGNAL_CHAIN_ARCHIVE)
    for _idx_sql in _SIGNAL_CHAIN_ARCHIVE_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] signal_chain_archive idx failed: {_e}", flush=True)
    conn.execute(_CREATE_DAILY_REFLECTION_SUMMARY)
    for _idx_sql in _DAILY_REFLECTION_SUMMARY_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] daily_reflection_summary idx failed: {_e}", flush=True)

    # v202: 宏观日历提前 2h + 即时提醒 状态表 — macro_calendar_notifier 幂等防重
    conn.execute(_CREATE_MACRO_NOTIFY_STATE)
    for _idx_sql in _MACRO_NOTIFY_STATE_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] macro_notify_state idx failed: {_e}", flush=True)

    # v170: 多源宏观日历表 — ForexFactory + jin10 双源落库
    conn.execute(_CREATE_MACRO_CALENDAR)
    for _idx_sql in _MACRO_CALENDAR_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] macro_calendar idx failed: {_e}", flush=True)
    # v215: macro_calendar 加 star INTEGER 1-5 颗星 (老库 ALTER 自动补列)
    _ensure_columns(conn, "macro_calendar", _MACRO_CALENDAR_V215_COLUMNS)

    # v231+ (H4): 拦截持久化 - 把被 quality_filter 拒掉的 decision 落库, 用于 C1 假阳性
    # A/B 决策 / 后续审计. rule 字段单值 (R1/R2/R4/R7/R8/R9) 便于 GROUP BY.
    # UNIQUE(decision_id, rule) 让重复 audit 幂等 (INSERT OR IGNORE).
    conn.execute(_CREATE_QUALITY_REJECTIONS)
    for _idx_sql in _QUALITY_REJECTIONS_INDEXES:
        conn.execute(_idx_sql)

    for stmt in _INDEXES:
        conn.execute(stmt)

    conn.execute(
        """UPDATE raw_news
           SET ts = CAST(strftime('%s', timestamp) AS INTEGER)
           WHERE ts IS NULL AND timestamp IS NOT NULL AND length(timestamp) >= 19"""
    )
    # v303+ P0+【修复】ai_decisions.ts 全 NULL 导致 /api/events/today live_signal_count
    # 永远 0 的历史 BUG：旧版 backfill 用 `strftime('%s', created_at)` 但当 created_at
    # 短于 19 字符（早期行）时跳过；新 backfill 走辅助函数 _backfill_ai_decisions_ts()
    # 兼容 ≥10 字符 ISO 北京串 + 用 datetime() 容错解析, 同时输出日志方便排错.
    n = _backfill_ai_decisions_ts(conn)
    if n:
        print(f"[MIGRATE] ai_decisions.ts backfill 回填 {n} 条（created_at → UTC 秒）", flush=True)
    # v303+ P0+: 给 ai_decisions 加 BEFORE INSERT trigger 兜底, 任何代码路径 INSERT
    # 漏写 ts 都会被自动补 UTC 秒, 防止再出现"ts=NULL 导致窗口查询漏空"事故.
    _ensure_ai_decisions_autots_trigger(conn)
    _normalize_published_at(conn)
    conn.commit()


def _backfill_ai_decisions_ts(conn: sqlite3.Connection) -> int:
    """v303+ P0+【修复】把 ai_decisions.ts=NULL 的存量行从 created_at 解析成 UTC 秒.

    历史 BUG：ai_decisions.ts 列是 v302+ 加的, 但 INSERT 路径完全没写 → 全 NULL.
    旧 backfill SQL `strftime('%s', created_at)` 依赖 created_at 至少 19 字符
    (含 'YYYY-MM-DDTHH:MM:SS' 部分), 早期几行长格式短的行被漏掉.
    本函数:
      1) 快路径: 含显式时区 (含 '+' / '-' / 'Z') 的标准 ISO 串, SQLite strftime
         认末尾偏移, 一次过. 已在 live DB 验证:
         '2026-10-13T07:08:43+08:00' → 1791846523 (即 UTC 23:08:43). ✓
      2) 容错路径: 无时区串 (早期短串 / 19 字符标准格式但没时区) 走 Python
         datetime, 按 Beijing +08:00 解读, 不依赖 SQLite strftime 的本地时区行为
         (机器 tz 不是 UTC 时 SQLite strftime 会按本地 tz 解析, 与我们的 Beijing
         假设冲突).
    返回回填行数 (== 0 表示没活要干, 静默).
    """
    if "ai_decisions" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return 0
    if "ts" not in table_columns(conn, "ai_decisions"):
        return 0
    if "created_at" not in table_columns(conn, "ai_decisions"):
        return 0
    # 路径1: 含显式时区偏移 ('+'/'-'/'Z' 任一出现在日期后) — SQLite strftime 一次过.
    n1 = conn.execute(
        """UPDATE ai_decisions
              SET ts = CAST(strftime('%s', created_at) AS INTEGER)
            WHERE ts IS NULL
              AND created_at IS NOT NULL
              AND length(created_at) >= 19
              AND (instr(created_at, '+') > 10
                   OR instr(created_at, '-') > 10
                   OR instr(created_at, 'Z') > 10)"""
    ).rowcount
    # 路径2: 其余 (无时区的早期短串 / 标准 ISO 串) 走 Python 解析.
    rows = conn.execute(
        """SELECT id, created_at FROM ai_decisions
            WHERE ts IS NULL
              AND created_at IS NOT NULL
              AND length(created_at) >= 10
              AND NOT (length(created_at) >= 19
                       AND (instr(created_at, '+') > 10
                            OR instr(created_at, '-') > 10
                            OR instr(created_at, 'Z') > 10))"""
    ).fetchall()
    n2 = 0
    if rows:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        bj_offset = _tz(_td(hours=8))
        for row_id, raw in rows:
            try:
                s = str(raw)[:19].replace("T", " ")
                dt = _dt.strptime(s, "%Y-%m-%d %H:%M:%S")
                dt_bj = dt.replace(tzinfo=bj_offset)
                ts_utc = int(dt_bj.astimezone(_tz.utc).timestamp())
            except Exception:
                continue
            conn.execute(
                "UPDATE ai_decisions SET ts=? WHERE id=?", (ts_utc, row_id)
            )
            n2 += 1
    return int(n1 or 0) + int(n2 or 0)


_AI_DECISIONS_AUTOTS_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_ai_decisions_autots
AFTER INSERT ON ai_decisions
FOR EACH ROW
WHEN NEW.ts IS NULL
BEGIN
    -- SQLite BEFORE INSERT 触发器无法改 NEW.*, 必须用 AFTER INSERT + UPDATE 回写.
    -- 解析策略 (与 _backfill_ai_decisions_ts 对齐):
    --   1) created_at 含显式时区偏移 ('+'/'-'/'Z' 任一在日期后) → strftime 直接给 UTC 秒;
    --   2) 无时区串 → strftime 按 SQLite 本地 tz 解析再 -8*3600 矫正 (假设 Beijing);
    --   3) created_at 缺/过短 → 兜底用 NOW UTC 秒.
    UPDATE ai_decisions
       SET ts = CASE
           WHEN NEW.created_at IS NOT NULL
                AND length(NEW.created_at) >= 19
                AND (instr(NEW.created_at, '+') > 10
                     OR instr(NEW.created_at, '-') > 10
                     OR instr(NEW.created_at, 'Z') > 10) THEN
               CAST(strftime('%s', NEW.created_at) AS INTEGER)
           WHEN NEW.created_at IS NOT NULL AND length(NEW.created_at) >= 10 THEN
               CAST(strftime('%s', substr(NEW.created_at, 1, 19)) AS INTEGER) - 8 * 3600
           ELSE
               CAST(strftime('%s', 'now') AS INTEGER)
       END
     WHERE id = NEW.id;
END
"""


def _ensure_ai_decisions_autots_trigger(conn: sqlite3.Connection) -> None:
    """v303+ P0+: 创建 trg_ai_decisions_autots trigger, 给 ai_decisions.ts 兜底.

    旧 BUG 链路：v302+ 加了 ai_decisions.ts 列 (无 default), ai_worker.py 的
    INSERT 漏写 → 全 NULL → v302+ 窗口查询 (ts>=lo/hi/fresh_lo) 全漏 → 前端
    live_signal_count 永远 0. 本 trigger 在 SQLite 内核层把 NEW.ts 自动填好,
    与应用代码 INSERT 路径解耦: 任何忘写 ts 的 INSERT 都会被兜底, 不会再让
    前端归零.

    因 trigger DDL 可能在版本间调整, 每次 migrate() 都校验 DDL 与当前 trigger
    sql 是否一致: 不一致就 DROP 后重建. CREATE TRIGGER IF NOT EXISTS 不会覆盖
    已存在的 trigger, 必须先 DROP.
    """
    if "ai_decisions" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='trg_ai_decisions_autots'"
        ).fetchone()
        expected_sql = " ".join(_AI_DECISIONS_AUTOTS_TRIGGER_DDL.split())
        if existing and existing[0] and " ".join(existing[0].split()) == expected_sql:
            return  # 已是最新, no-op
        if existing:
            conn.execute("DROP TRIGGER IF EXISTS trg_ai_decisions_autots")
        conn.execute(_AI_DECISIONS_AUTOTS_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_ai_decisions_autots 创建失败: {_exc}", flush=True)


def _normalize_published_at(conn: sqlite3.Connection) -> int:
    """把存量 published_at 全部归一化为统一北京时间串（幂等回填）。

    早期入库的 published_at 可能是 UTC 原串（如 '2026-09-03T10:09:09.241Z'）
    或各种平台格式 —— 未归一化时展示/比较可能出错（UTC 当北京差 8 小时）。
    本函数用 config.parse_time_to_bj 统一换算（带时区→北京 / 无时区按北京 /
    epoch 秒毫秒→北京），结果与入库格式完全一致：'YYYY-MM-DD HH:MM:SS'。
    已归一化的行再次执行不变（幂等）。返回修正行数。
    """
    if "published_at" not in table_columns(conn, "raw_news"):
        return 0
    rows = conn.execute(
        "SELECT id, published_at FROM raw_news"
        " WHERE published_at IS NOT NULL AND published_at != ''"
    ).fetchall()
    fixed = 0
    for row_id, raw in rows:
        normalized = config.fmt_bj(raw)
        if not normalized or normalized == str(raw):
            continue
        conn.execute("UPDATE raw_news SET published_at=? WHERE id=?", (normalized, row_id))
        fixed += 1
    if fixed:
        print(f"[DB] published_at 归一化回填 {fixed} 条（存量平台时间 → 统一北京时间）", flush=True)
    return fixed


# ---------------------------------------------------------------------------
# SQL safety gate removed: the recovered assert_readonly_sql was dead code.
# db.py read paths do not interpolate external input (all bound parameters), so
# keeping an unused guard misled reviewers into thinking a safety net existed.
# ---------------------------------------------------------------------------


# ══════════════════════════════════════════════════════════════════
# v187: ai_call_stats token 用量监控（独立读写函数，零依赖）
# 写: log_ai_call() — 每次 LLM HTTP 调用结束都调用 1 次
# 读: query_ai_call_stats_by_purpose / by_key / by_call_site / timeline
# 所有读函数都接 since_ts (默认 24h 前) → now,便于前端聚合查询
# ──────────────────────────────────────────────────────────────────
def log_ai_call(
    *, purpose: str, key_alias: str, model_id: str,
    tokens_in: int = 0, tokens_out: int = 0,
    latency_ms: int = 0, status: str = "ok",
    error_code: str = "", call_site: str = "",
) -> None:
    """v187: 写入 1 条 LLM 调用统计。失败仅 print 不抛(不影响主链路)。"""
    try:
        import time as _t
        _sql = (
            "INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id,"
            " tokens_in, tokens_out, latency_ms, status, error_code, call_site)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        _params = (int(_t.time()), str(purpose)[:32], str(key_alias)[:32],
                   str(model_id)[:80], int(tokens_in), int(tokens_out),
                   int(latency_ms), str(status)[:16],
                   str(error_code)[:32], str(call_site)[:48])
        # v189-8: 走通用重试包装。锁竞争高峰期单次 5s busy_timeout 会耗尽并直接丢统计
        # (实测 engine.out.log 471 次 "log failed")；3 次指数退避最坏 ≈ 15s 上界,
        # 只在真被锁时触发,正常路径零额外延迟。
        execute_with_retry(get_connection, _sql, _params, max_retries=3)
    except Exception as exc:  # pragma: no cover
        # 监控失败不能让 AI 链路崩——静默吞掉,记录 stdout
        try:
            print(f"[ai_call_stats] log failed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        except Exception:
            pass


def query_ai_call_stats_by_purpose(since_ts: int) -> List[Dict[str, Any]]:
    """按 purpose 聚合 token 用量 (last N seconds)。"""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT purpose, COUNT(*) AS calls,"
            "       COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            "       COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            "       COALESCE(SUM(latency_ms), 0) AS latency_ms_total,"
            "       COALESCE(AVG(latency_ms), 0) AS latency_ms_avg,"
            "       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_calls"
            "  FROM ai_call_stats WHERE ts >= ? GROUP BY purpose ORDER BY tokens_in DESC",
            (int(since_ts),),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def query_ai_call_stats_by_key(since_ts: int) -> List[Dict[str, Any]]:
    """按 key_alias 聚合 (便于看每通道充值消耗)。"""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT key_alias, COUNT(*) AS calls,"
            "       COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            "       COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            "       COALESCE(AVG(latency_ms), 0) AS latency_ms_avg"
            "  FROM ai_call_stats WHERE ts >= ? GROUP BY key_alias ORDER BY tokens_in DESC",
            (int(since_ts),),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def query_ai_call_stats_by_call_site(since_ts: int) -> List[Dict[str, Any]]:
    """按调用站点 (call_site) 聚合 — 看哪个 endpoint 最费 token。"""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT call_site, COUNT(*) AS calls,"
            "       COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            "       COALESCE(SUM(tokens_out), 0) AS tokens_out"
            "  FROM ai_call_stats WHERE ts >= ? GROUP BY call_site ORDER BY tokens_in DESC",
            (int(since_ts),),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def query_ai_call_stats_timeline(since_ts: int, bucket_seconds: int = 3600) -> List[Dict[str, Any]]:
    """按时间桶聚合(默认 1 小时) — 看 24h 内 token 用量走势。"""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT (ts/?)*? AS bucket_ts,"
            "       COUNT(*) AS calls,"
            "       COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            "       COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            "       COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0) AS ok_calls"
            "  FROM ai_call_stats WHERE ts >= ?"
            " GROUP BY bucket_ts ORDER BY bucket_ts",
            (int(bucket_seconds), int(bucket_seconds), int(since_ts)),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


# === v189-4: WAL 自动 checkpoint 守护 ====================================
#
# 背景: db 文件 52MB, ai_call_stats 5M+ 行高频写入, WAL 一直膨胀到 460MB
#       api_server + engine 多进程并发写, 单连接 wal_autocheckpoint=1000 pages
#       经常 30s+ 不触发一次. 后台线程每 60s 强制 PASSIVE checkpoint 截断.
#
# PASSIVE 模式: 不阻塞 reader/writer, 仅在所有 reader 关闭时合并.
# TRUNCATE 模式: 强制截断 WAL, 但会等所有 reader 关闭. 在低峰期 (凌晨) 调用.
#
# 只启动一次 (跨进程 thread-safe).
_wal_checkpoint_started = False
_wal_checkpoint_lock = threading.Lock()


def start_wal_checkpoint_daemon(interval_seconds: float = 60.0) -> None:
    """启动后台线程, 每 interval_seconds 秒跑一次 wal_checkpoint(PASSIVE).
    业务进程 (api_server / engine) 启动时调用一次.
    """
    global _wal_checkpoint_started
    with _wal_checkpoint_lock:
        if _wal_checkpoint_started:
            return
        _wal_checkpoint_started = True

    def _loop() -> None:
        while True:
            try:
                time.sleep(interval_seconds)
                conn = get_connection()
                try:
                    # PASSIVE 不阻塞 reader/writer; 在 reader 都关闭的瞬间自动合并
                    cur = conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    busy, log_frames, checkpointed_frames = cur.fetchone()
                    if int(log_frames) > 500:  # > 500 frames ≈ 2MB 待 checkpoint
                        print(f"[WAL] checkpoint(PASSIVE) busy={busy} log={log_frames} ckpt={checkpointed_frames}")
                finally:
                    conn.close()
            except Exception as e:  # pragma: no cover - 守护线程绝不退出
                import traceback
                print(f"[WAL-CHECKPOINT-DAEMON] error: {type(e).__name__}: {e}")
                traceback.print_exc()

    t = threading.Thread(target=_loop, name="wal-checkpoint-daemon", daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# P2 迁移：ai_decisions 结算列清理
# ─────────────────────────────────────────────────────────────────────────────
# 原则：quick_sim_trades = 单一结算权威。ai_decisions 只保留决策上下文。
# 列出待删除列：settlement 数据已在 quick_sim_trades 镜像；删 ai_decisions 同名列
# 后所有 SQL 读 qs.*（无 NULL 兼容性问题）。
#
# 重要：这是「一次性」迁移函数，不会自动执行 ——
# 调用方必须：
#   a) 备份 DB
#   b) 确认所有 P2.1 SQL 改造已上线
#   c) 确认 quick_sim_trades 中所有 settled=1 行都已正确镜像结算字段
# 之后再调用 drop_settlement_columns_from_ai_decisions(conn)

_P2_SETTLEMENT_COLUMNS_TO_DROP = [
    # (列名, 原因/迁移目标)
    ("entry_price",        "→ quick_sim_trades.entry_price"),
    ("exit_price",         "→ quick_sim_trades.exit_price"),
    ("max_price",          "→ quick_sim_trades.max_price"),
    ("min_price",          "→ quick_sim_trades.min_price"),
    ("max_price_time",     "→ quick_sim_trades.max_price_time"),
    ("min_price_time",     "→ quick_sim_trades.min_price_time"),
    ("is_correct",         "→ quick_sim_trades.verdict"),
    ("settled",            "→ quick_sim_trades.settled"),
    ("entry_time",         "→ quick_sim_trades.entry_ts"),
    ("mfe_pct",            "→ quick_sim_trades.mfe_pct"),
    ("mae_pct",            "→ quick_sim_trades.mae_pct"),
    ("forward_pnl",        "→ quick_sim_trades.pnl_pct"),
    ("mfe_time_mins",      "→ quick_sim_trades.mfe_time_mins"),
]


def drop_settlement_columns_from_ai_decisions(conn: sqlite3.Connection,
                                              dry_run: bool = True) -> List[str]:
    """P2 迁移：从 ai_decisions 表 DROP 13 个结算列，让 quick_sim_trades 成为
    唯一结算权威。

    ⚠️ 一次性、不可逆迁移。运行前必须：
        1. 备份 DB（cp trident_event_bus.db backup.db）
        2. 确认所有 P2.1 读路径改造已上线（无 ad.* 结算字段引用）
        3. 确认 quick_sim_trades 中所有 settled=1 行的结算字段完整镜像

    Args:
        conn:  连接（会在上面执行 DROP COLUMN）
        dry_run: True = 仅打印将要删除的列，不实际执行。False = 真正 DROP。

    Returns:
        实际删除（或将要删除）的列名列表

    SQLite ≥ 3.35 支持 ALTER TABLE DROP COLUMN。本项目 Python sqlite3
    编译版本 ≥ 3.35（运行时验证过 3.50.4），故可执行。

    风险：如果有列被索引/触发器/视图引用，ALTER TABLE 会报错。本函数 try/catch
    单列级别降级，保证能 drop 尽可能多的列。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
    to_drop: List[str] = []
    skipped: List[str] = []
    for col, _reason in _P2_SETTLEMENT_COLUMNS_TO_DROP:
        if col not in existing:
            continue
        if dry_run:
            to_drop.append(col)
            continue
        try:
            conn.execute(f"ALTER TABLE ai_decisions DROP COLUMN {col}")
            to_drop.append(col)
        except sqlite3.Error as exc:
            skipped.append(col)
            print(f"[DB-P2] DROP COLUMN {col} 失败: {exc}", flush=True)
    if dry_run:
        print(f"[DB-P2] dry_run: 准备删除 {len(to_drop)} 列: {to_drop}", flush=True)
    else:
        conn.commit()
        print(f"[DB-P2] 已删除 {len(to_drop)} 列: {to_drop}", flush=True)
        if skipped:
            print(f"[DB-P2] 跳过 {len(skipped)} 列（被引用）: {skipped}", flush=True)
    return to_drop


# ===========================================================================
# P2 启动迁移钩子
# ===========================================================================
# 设计：
#   - PRAGMA user_version 做迁移标记位（SQLite 标准做法，避免新建元数据表）
#   - 默认行为是 dry_run：只在启动日志里提示「需要跑 P2」，不动 DB
#   - 真实 DROP 必须满足 *两个* 显式条件：
#       (a) 调用方传 real_run=True
#       (b) 环境变量 P2_DROP_COLUMNS=1（防误触兜底）
#   - 跑完 set user_version = P2_USER_VERSION，幂等保护
#
# user_version 演进历史（递增）：
#   0  : 历史版本（无 PRAGMA user_version 标记）
#   23 : P2 完成 — ai_decisions 已删 13 列结算字段

P2_USER_VERSION = 23


def _get_user_version(conn: sqlite3.Connection) -> int:
    """读取 PRAGMA user_version。"""
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """写入 PRAGMA user_version。"""
    conn.execute(f"PRAGMA user_version = {int(version)}")


def is_p2_migration_done(conn: sqlite3.Connection) -> bool:
    """P2 迁移是否已执行（PRAGMA user_version >= P2_USER_VERSION）。"""
    return _get_user_version(conn) >= P2_USER_VERSION


def is_p2_migration_needed(conn: sqlite3.Connection) -> bool:
    """P2 迁移是否仍需要（ai_decisions 是否还含任一待删列）。"""
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
    except sqlite3.Error:
        return False
    return any(col in existing for col, _ in _P2_SETTLEMENT_COLUMNS_TO_DROP)


def run_p2_migration_if_needed(conn: sqlite3.Connection, real_run: bool = False) -> dict:
    """启动钩子：检测 P2 状态，按需执行。

    三态返回（dict）：
        {"status": "already_done"}  — 已完成（user_version >= 23），跳过
        {"status": "not_needed"}    — 列已不在表中（手动清过），无需再跑
        {"status": "dry_run", "pending": [...]} — 启动默认：只打日志，不动 DB
        {"status": "migrated", "dropped": [...]} — 真跑了 P2，commit + 设 user_version

    参数：
        real_run=False（默认）：dry_run 模式 — 不修改 DB，仅日志提示
        real_run=True：执行真 DROP COLUMN
        同时检查环境变量 P2_DROP_COLUMNS；只有 real_run=True *且* env=1 才生效
        （双保险，防止误调函数 + 防误设环境变量）

    Raises:
        RuntimeError: 当 real_run=True 但 P2_DROP_COLUMNS 环境变量不为 "1"
    """
    if is_p2_migration_done(conn):
        return {"status": "already_done", "user_version": _get_user_version(conn)}

    if not is_p2_migration_needed(conn):
        # 列已不存在但 user_version 没升（手动清过）— 顺手补一下标记位
        _set_user_version(conn, P2_USER_VERSION)
        conn.commit()
        print(f"[DB-P2] ai_decisions 列已清理，标记 user_version={P2_USER_VERSION}",
              flush=True)
        return {"status": "not_needed", "user_version": P2_USER_VERSION}

    pending = [c for c, _ in _P2_SETTLEMENT_COLUMNS_TO_DROP]
    if not real_run:
        print(f"[DB-P2] 启动 dry_run 提示：ai_decisions 还有 {len(pending)} 个结算列待删 "
              f"({pending})。真跑需设环境变量 P2_DROP_COLUMNS=1 + 显式调用 "
              f"db.run_p2_migration_if_needed(conn, real_run=True)。", flush=True)
        return {"status": "dry_run", "pending": pending}

    env_ok = os.environ.get("P2_DROP_COLUMNS", "") == "1"
    if not env_ok:
        raise RuntimeError(
            "P2 真跑被拦截：real_run=True 但环境变量 P2_DROP_COLUMNS != '1'。"
            "请先备份 DB，然后设环境变量再调用。"
        )

    dropped = drop_settlement_columns_from_ai_decisions(conn, dry_run=False)
    _set_user_version(conn, P2_USER_VERSION)
    conn.commit()
    print(f"[DB-P2] P2 迁移完成，user_version={P2_USER_VERSION}，已删 {len(dropped)} 列",
          flush=True)
    return {"status": "migrated", "dropped": dropped, "user_version": P2_USER_VERSION}
