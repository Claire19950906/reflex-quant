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
import sys
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config

# 2026-11-18: 所有建表 / 建索引 SQL 常量搬到 db_schema.py（纯字符串，1,880 行）。
# 用 * 接回来是刻意的：外部有 `db._CREATE_AI_DECISIONS` 这类引用，名字必须仍在 db 命名空间。
from db_schema import *

# v171 时钟守护：carry-over / signal_state 的年龄计算改用 monotonic 双源校验，
# 检测 wall-clock 跳变（SIGINT / 离线复盘 / 跨夜运行）→ 防止衰减系数爆炸。
try:
    from clock import safe_now as _safe_now  # type: ignore
except Exception:  # pragma: no cover
    def _safe_now() -> int:  # type: ignore
        return int(time.time())


# v453: pytest 硬护栏 —— 测试进程**永远不许**写生产库。
# 事故：2026-11-19 10:28，另一个会话直接用 `python -m pytest tests` 跑全量，
# 它把 TRIDENT_DB_PATH 显式指到了生产 trident_event_bus.db（conftest 的
# "没指定才隔离"逻辑因此失效），结果写进来 4 条 source='test' 新闻、
# 4 条 reasoning='test' 的决策、1 条 entry_price=100 的假成交，
# 这些垃圾随后又触发体检告警。只靠 conftest 不够，连接层必须自己兜底。
_PYTEST_PROD_GUARD_DONE = False


def _pytest_prod_guard() -> None:
    global _PYTEST_PROD_GUARD_DONE
    if _PYTEST_PROD_GUARD_DONE:
        return
    _PYTEST_PROD_GUARD_DONE = True
    if "pytest" not in sys.modules:
        return
    try:
        path = str(config.DB_PATH)
        if not path.endswith("trident_event_bus.db"):
            return
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix="trident_pytest_prod_guard_"),
                           "trident_event_bus.db")
        print(f"[db] pytest 试图连生产库 {path} → 已强制改到临时库 {tmp}", flush=True)
        config.DB_PATH = tmp
        os.environ["TRIDENT_DB_PATH"] = tmp
    except Exception:
        pass


def get_db_path() -> str:
    """Return the event-bus DB path (TRIDENT_DB_PATH override aware)."""
    _pytest_prod_guard()
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

    # ── v345.10 慢提交档案（TRIDENT_WRITE_PROFILE=1 时开）──────────────────
    # 锁竞争的争论一直靠猜：到底是谁持锁 5s / 30s？这里直接在 commit 上量。
    # 只在超过阈值时记一行，常驻开销 = 两次 perf_counter，可长期开。
    # 输出: $TRIDENT_WRITE_PROFILE_LOG（默认 /tmp/trident_write_profile.log）
    # ── v433 P0-0 长事务哨兵（默认开，只在超阈值时记一行）─────────────────
    # 实测 2026-11-17：引擎整批写事务开了 20+ 分钟，外部写入全被 `database is locked`
    # 挡回（busy_timeout 2s/20s 都无效），api 启动被拖到 97s。已有的
    # TRIDENT_WRITE_PROFILE 只量 commit 的耗时 —— 而问题恰恰是「两次写之间
    # 事务一直开着」（commit 本身只有毫秒）。这里改量**整个事务的跨度**：
    # 第一条写语句记 t0 + 调用栈，commit/rollback 时算总时长，超 5s 记一行。
    # 输出：$TRIDENT_TXN_PROFILE_LOG（默认 /tmp/trident_txn_profile.log）
    _last_sql = ""
    _txn_t0 = None
    _txn_stack = ""
    _txn_kind = ""

    _WRITE_KW = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER",
                 "DROP", "BEGIN", "ATTACH", "DETACH", "VACUUM")

    def _txn_note_write(self, kind: str) -> None:
        if self._txn_t0 is not None:
            return
        import time as _t
        self._txn_t0 = _t.perf_counter()
        self._txn_kind = kind
        try:
            import traceback as _tb
            self._txn_stack = " | ".join(
                f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}"
                for f in _tb.extract_stack(limit=8)[:-2]
            )
        except Exception:
            self._txn_stack = ""

    def _txn_close(self, how: str) -> None:
        if self._txn_t0 is None:
            return
        import os as _os3
        import time as _t
        _dt = _t.perf_counter() - self._txn_t0
        self._txn_t0 = None
        if _os3.getenv("TRIDENT_TXN_PROFILE", "1") != "1":
            return
        if _dt < float(_os3.getenv("TRIDENT_TXN_SLOW_S", "5.0")):
            return
        _path = _os3.getenv("TRIDENT_TXN_PROFILE_LOG", "/tmp/trident_txn_profile.log")
        try:
            with open(_path, "a", encoding="utf-8") as _fh:
                _fh.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} pid={_os3.getpid()} "
                          f"txn_s={_dt:.1f} end={how} first_write={self._txn_kind} "
                          f"stack={self._txn_stack}\n")
        except Exception:
            pass

    def execute(self, sql, *args, **kwargs):
        try:
            _kw = str(sql).lstrip().split(None, 1)[0][:12].upper()
            self._last_sql = _kw
            if _kw in self._WRITE_KW:
                self._txn_note_write(_kw)
        except Exception:
            self._last_sql = "?"
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        import time as _t
        _t0 = _t.perf_counter()
        try:
            return super().commit()
        finally:
            _dt = _t.perf_counter() - _t0
            if (os.getenv("TRIDENT_WRITE_PROFILE") == "1"
                    and _dt >= float(os.getenv("TRIDENT_WRITE_SLOW_S", "1.0"))):
                import os as _os2
                _path = _os2.getenv("TRIDENT_WRITE_PROFILE_LOG",
                                    "/tmp/trident_write_profile.log")
                try:
                    with open(_path, "a", encoding="utf-8") as _fh:
                        _fh.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} "
                                  f"pid={_os2.getpid()} sql={self._last_sql} "
                                  f"commit_s={_dt:.2f}\n")
                except Exception:
                    pass
            self._txn_close("commit")

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._txn_close("rollback")

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


# ── v483: 测试进程的生产库写保护（应用侧，覆盖子进程）────────────────────────
# 历史事故（同一类三次）：pytest 用例把 TRIDENT_DB_PATH 指回生产库、或用 subprocess
# 拉起采集脚本，把 ai_decisions / raw_news 截断。tests/conftest.py 里的守卫只能罩住
# 测试进程本身，**shell out 出去的子进程不受它约束** —— 17:14 那轮 `pytest -q`
# 就靠子进程继续往生产库写新闻。所以在应用唯一的建档入口再拦一道：
# 环境变量 TRIDENT_BLOCK_PROD_WRITE=1（conftest 会设，子进程继承）且路径是生产库时，
# 所有写语句直接抛错，读不受影响。
_PROD_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "trident_event_bus.db")
_PROD_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|replace|drop|alter|create|vacuum|reindex|attach|detach)\b", re.I)


class _ProdWriteGuardConnection(_ManagedConnection):
    """只读守卫连接：命中写语句即抛 OperationalError（与 sqlite 原生错误同型，便于上层识别）。"""

    def _check(self, sql):
        if isinstance(sql, str) and _PROD_WRITE_RE.match(sql):
            raise sqlite3.OperationalError(
                "TRIDENT_BLOCK_PROD_WRITE=1：测试进程禁止写生产库 %s（SQL: %s）"
                % (_PROD_DB_PATH, sql.strip().splitlines()[0][:80])
            )

    def execute(self, sql, *a, **kw):
        self._check(sql)
        return super().execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        self._check(sql)
        return super().executemany(sql, *a, **kw)

    def executescript(self, sql, *a, **kw):
        self._check(sql)
        return super().executescript(sql, *a, **kw)


def _block_prod_write() -> bool:
    """当前进程是否处于"禁止写生产库"模式，且目标就是生产库。"""
    if str(os.getenv("TRIDENT_BLOCK_PROD_WRITE", "")).strip() not in ("1", "true", "yes", "on"):
        return False
    try:
        return os.path.abspath(get_db_path()) == os.path.abspath(_PROD_DB_PATH)
    except Exception:
        return False



_EMPTY_SHELL_CACHE: dict = {}


def _warn_if_empty_shell(conn: "sqlite3.Connection", path: str) -> None:
    """连上了一个「有文件、0 张表」的库 → 大声报警，别等写失败才发现。

    背景（2026-11-19 审计）：`backend/trident.db`、仓库根 `trident_event_bus.db`
    都是 4KB/0 字节、0 张表的空壳，而真库是 `backend/trident_event_bus.db`。
    连错库时所有 INSERT 都会报 `no such table: xxx`，然后被 `except: pass` 吞掉，
    表现为"数据莫名其妙不增长"，代码层面完全无声。

    这里**只告警不改行为**：故意不 raise —— 新建库/跑迁移的首个连接本来就是 0 表，
    raise 会把正常的初始化流程打死。告警同时记进丢写账本，事后可统计。
    """
    # pytest 下新建的测试库天然是 0 表，这类"空壳"是正常的：
    # 实测一次 `-k` 子集跑就刷了 40 条 db.empty_shell，把真告警淹了。
    # 豁免判定收成显式开关 TRIDENT_SHELL_GUARD：
    #   auto（默认）：pytest 进程自动豁免；on：强制开启（测试验证守卫用）；off：关闭。
    _mode = os.getenv("TRIDENT_SHELL_GUARD", "auto").strip().lower()
    if _mode in ("off", "0", "false", "no"):
        return
    if _mode == "auto" and ("pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST")):
        return
    try:
        st = os.stat(path)
        key = (path, int(st.st_mtime), st.st_size)
        cached = _EMPTY_SHELL_CACHE.get(path)
        if cached == key:
            return
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        _EMPTY_SHELL_CACHE[path] = key
        if n == 0:
            import sys as _sys
            print(f"[db] ⚠️  连到了空壳库：{path}（{st.st_size} 字节 / 0 张表）。"
                  f"真库应为 backend/trident_event_bus.db，请检查 TRIDENT_DB_PATH 与工作目录。",
                  file=_sys.stderr, flush=True)
            try:
                import silent_ledger
                silent_ledger.record("db.empty_shell", f"0 tables at {path}", force=True)
            except Exception:
                pass
    except sqlite3.Error:
        pass
    except Exception:
        pass

def get_connection() -> sqlite3.Connection:
    # v189-8: TRIDENT_TRACE_CONN=1 时叠加 Traced (捕获 close); 两种模式都用 _ManagedConnection
    # 以修正 with 语句不关连接的既有缺陷。
    factory = _TracedConnection if _TRACE_CONN else _ManagedConnection
    if _block_prod_write():
        factory = _ProdWriteGuardConnection
    conn = sqlite3.connect(get_db_path(), timeout=60.0, factory=factory)
    if _TRACE_CONN:
        _record_open(conn)
        start_connection_tracing()
    _warn_if_empty_shell(conn, get_db_path())
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
    # v323：常驻进程共用同一个库，5 秒等不到锁就抛 SQLITE_BUSY（实测引擎日志里
    # `[unclassified] record failed: database is locked` 反复出现 → 新闻落不了库
    # → 卡在 PROCESSING → 看门狗回收 → 再分析一遍，同一条新闻被处理 3 次）。
    # 改成可按进程配置：engine 用 20 秒（批处理可以等），api 保持 5 秒（不能卡住请求）。
    conn.execute(f"PRAGMA busy_timeout={int(os.getenv('TRIDENT_BUSY_TIMEOUT_MS', '20000'))};")
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
    # v428（2026-11-17）：1000 → 5000 pages（≈20MB）。
    # 实测写锁可用率只有 21.2%（90s 探 444 次拿到 94 次），1 小时 271 次
    # `database is locked`。autocheckpoint 每 4MB 触发一次，等于每几十次写
    # 就强制一次 checkpoint 抢锁；放宽到 20MB 可把 checkpoint 频率降到 1/5，
    # 写者排队次数随之下降。WAL 软上限已是 256MB，余量充足。
    conn.execute("PRAGMA wal_autocheckpoint=5000;")
    # v366: 给 -wal 设软上限 —— checkpoint 后 SQLite 会把 WAL 截到该尺寸，
    #   避免 reader 走开后 WAL 无限膨胀（实测一天能涨到 220MB+）。
    #
    # v417（2026-11-17）：从 64MB 抬到 256MB。原因是实测到一个**死结**：
    #   11-17 09:51 引擎落库卡死 7 分钟以上，栈停在 btreeBeginTrans +
    #   sqliteDefaultBusyCallback（等写锁）。同时 -wal 恰好停在 67108864 字节
    #   —— 正好等于这里的软上限，且 `PRAGMA wal_checkpoint(TRUNCATE)` 返回
    #   busy=1、64MB -> 64MB（一帧都截不掉）。
    #   写锁可用率实测只有 21.2%（90s 探 444 次，拿到 94 次），最长连续抢不到 60s。
    #   一旦 WAL 顶到上限，checkpoint 自己也要抢写锁 → 抢不到就截不动 →
    #   WAL 继续满 → 所有写者排队，形成自我锁死。
    #   抬到 256MB 后 WAL 有足够余量，checkpoint 能在锁间隙推进，不再顶天花板。
    #   代价是磁盘占用上限变高（实测 WAL 常态 < 60MB，256MB 只是天花板不是常态）。
    conn.execute("PRAGMA journal_size_limit=268435456;")  # 256MB（v417 原 64MB）
    return conn


def resolve_entry_price(conn: sqlite3.Connection, asset: str, ts: float,
                        max_lookback_sec: int = 30 * 86400) -> Optional[float]:
    """按决策时点回溯最近一根行情价, 供 trade_decisions.entry_price 落库。

    背景 (v310 P0): trade_decisions 2101 行里 2074 行 entry_price IS NULL,
    结算率因此只有 1.19% —— 没有入场价就算不出 forward 收益, 于是
    「结算 → 漏检 → 复盘」三条下游一起断, A 类 (买卖信号判断) 维度永远 0 样本。

    查表优先级 (asset 口径三表统一为大写短名 BTC/WTI/XAU/ETH, 与
    trade_decisions.asset 对齐):
      1. ohlcv_intraday — 分钟级, 最贴近决策时点
      2. ohlcv_daily    — 日线收盘
      3. market_ticks   — 快照价 (最粗, 兜底)

    返回 None 表示该品种在该时点无任何行情 (调用方应保留 NULL, 不猜价)。
    """
    if not asset or not ts:
        return None
    a = str(asset).strip().upper()
    if not a or a == "NONE":
        return None
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    lo = t - int(max_lookback_sec)

    for sql, params in (
        ("SELECT close FROM ohlcv_intraday WHERE asset=? AND ts<=? AND ts>=? "
         "ORDER BY ts DESC LIMIT 1", (a, t, lo)),
        ("SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? AND ts>=? "
         "ORDER BY ts DESC LIMIT 1", (a, t, lo)),
        ("SELECT price FROM market_ticks WHERE symbol=? AND ts<=? AND ts>=? "
         "ORDER BY ts DESC LIMIT 1", (a, t, lo)),
        # v432（2026-11-17）：加 kline_bars 兜底 —— 白银/原油的 1m 线走金十直连
        # 写进 kline_bars（symbol 口径、ts 为**毫秒**），不在这三张表里，
        # 于是 XAG 有 491 根 K 线、entry_price 仍是 NULL（结算率被压低）。
        # 注意 ts 单位换算：kline_bars.ts 是毫秒，其余三张表是秒。
        ("SELECT close FROM kline_bars WHERE symbol=? AND ts<=? AND ts>=? "
         "ORDER BY ts DESC LIMIT 1", (a, t * 1000, lo * 1000)),
    ):
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] is not None:
            try:
                val = float(row[0])
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    return None


# ─────────────────────────────────────────────────────────────────────────────
# v310 P1: 结算取价 —— **全系统唯一口径**
# ─────────────────────────────────────────────────────────────────────────────
# 背景 (2026-10-29 事故复盘):
#   1) entry_price 与 forward 价**不同源** → 收益幅度(0.6~3.6%)被口径噪声淹没,
#      极端时算出 ±90%~±400% 的"收益"(199 条结算行), 把「真实持仓业绩」均值
#      从 +1.45% 拉到 +117%。同一条决策两种口径还能算出正负相反的收益。
#   2) 同一笔结算被三个地方各算各的(bridge / decision_engine / backfill 脚本),
#      口径一旦漂移, 库里的数字就互相矛盾。
#
# 现在把口径收敛到本模块:
#   · forward 价: **分钟线优先, 日线回退** —— 与 resolve_entry_price 同源
#   · 收益上限: |pct| > MAX_PLAUSIBLE_OUTCOME_PCT 一律视为取价异常, **拒写**
#     (宁可这条决策晚点结算, 也不能让不可能的幅度进库污染统计)
SETTLE_DAYS = 5
MAX_PLAUSIBLE_OUTCOME_PCT = 90.0


def forward_price(conn: sqlite3.Connection, asset: str, from_ts: float,
                  settle_days: int = SETTLE_DAYS,
                  min_ts: Optional[float] = None) -> Optional[float]:
    """结算用远期价 —— 分钟线优先, 日线回退(与 resolve_entry_price 同源)。

    窗口: 决策时点后第 (settle_days-1) 天的第一根分钟线; 分钟线缺失时取
    该时点之后第一根日线。返回 None 表示该时点无任何可用行情。

    v319+: `min_ts` —— **交收约束下界** (T+1 品种的最早可平仓时点)。
      传了就用它把 target_ts 顶上去: 否则 T+1 股票会用"当天/次日"的价平仓,
      实盘根本做不到 (当日买入当日不能卖)。
    """
    if not asset or from_ts is None:
        return None
    a = str(asset).strip().upper()
    if not a or a == "NONE":
        return None
    try:
        target_ts = int(float(from_ts)) + max(int(settle_days) - 1, 0) * 86400
    except (TypeError, ValueError):
        return None
    if min_ts is not None:
        try:
            target_ts = max(target_ts, int(float(min_ts)))
        except (TypeError, ValueError):
            pass
    for sql, params in (
        ("SELECT close FROM ohlcv_intraday WHERE asset=? AND ts>=? AND ts<=? "
         "ORDER BY ts ASC LIMIT 1", (a, target_ts, target_ts + 86400)),
        ("SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? ORDER BY ts ASC LIMIT 1",
         (a, target_ts)),
    ):
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] is not None:
            try:
                val = float(row[0])
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    return None


# ─────────────────────────────────────────────────────────────────────────────
# v314 P0: 到期时刻取价 —— 结算价必须对应 horizon 到期那一刻
# ─────────────────────────────────────────────────────────────────────────────
# 背景 (2026-10-29 事故复盘, 实测): quick_sim 的结算价一直取「扫描当刻的现价」,
# exit_ts 也写成扫描时刻。结算链一旦停摆 (v313 之前被 cut_bleeding_trades 的
# TypeError 掐断整个 run_cycle, 累计 3414 次), 一笔 20 分钟的仓位会一直挂着,
# 直到某次扫描才用**几天后的现价**结算 —— 实测出现持有 12742.9 分钟 (8.85 天)
# 的仓位, 收益被放大到 ±50% 量级, 回填 forward_pnl 后把界面均值拉到 +117%。
#
# 口径: 结算价必须对应 **horizon 到期那一刻**:
#   1) ohlcv_intraday 中 ts 之后第一根 (forward —— 结算时刻即可知, 无未来函数)
#   2) ohlcv_intraday 中 ts 之前最近一根 (as-of 回退)
#   3) ohlcv_daily 日线 (兜底 —— 只允许**最近一根**且滞后不超过上限)
# 全部取不到 → None (调用方按「无价」处理, 不猜价)
PRICE_AT_TOLERANCE_SEC = 5 * 60
# v386 P0: 日线兜底最大滞后。此前该分支用 `ORDER BY ts ASC` +
# 6 天回看窗口, 取到的是窗口里**最老**的一根日线 ——
# 实测 2026-11-16 两笔 WTI(20 分钟 horizon) 因 1m 线断档(11-15~11-16 07:00
# 无数据) 被结算成 11-10 收盘 86.43 / 11-11 收盘 86.42, 凭空造出 -3.17%/笔
# 的"亏损", 并把胜率、权益、反思样本全部污染。
# 现在: 取**最近一根**(DESC), 且滞后超过上限一律当"无价"(不猜价)。
PRICE_AT_DAILY_MAX_LAG_SEC = 86400


def quote_health(conn: sqlite3.Connection, asset: str, now_ts: Optional[float] = None,
                 window_sec: int = 3600, min_ticks: int = 20) -> Dict[str, Any]:
    """报价健康度 —— v396 P2：冻结/过期报价在源头拦住。

    事故背景：WTI 报 88.985 从 11-15 08:14 冻到 11-16 06:49（22.5 小时 / 7,160 tick
    同价），系统照常建仓、照常结算，产出的是"不存在的行情下的交易"。

    判定口径（全部基于 market_ticks，最贴近撮合价的那张表）：
      · age_sec        —— 最后一笔 tick 距今多久（过期）
      · distinct_prices—— 窗口内出现过几个不同的价
      · frozen         —— 窗口内 tick 数够多但只有一个价 → 冻结

    返回 {} 表示该品种在窗口内完全没有 tick（调用方应视为"无数据"，不要当成健康）。
    """
    if not asset:
        return {}
    a = str(asset).strip().upper()
    if not a:
        return {}
    now = int(now_ts if now_ts is not None else time.time())
    lo = now - max(0, int(window_sec))
    try:
        rows = conn.execute(
            "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts>=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 5000", (a, lo, now)).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    prices = set()
    last_ts = 0
    for r in rows:
        try:
            last_ts = max(last_ts, int(r[0]))
            prices.add(round(float(r[1]), 6))
        except (TypeError, ValueError, IndexError):
            continue
    return {
        "asset": a,
        "last_ts": last_ts,
        "age_sec": max(0, now - last_ts),
        "n_ticks": len(rows),
        "distinct_prices": len(prices),
        "window_sec": int(window_sec),
        "frozen": len(rows) >= int(min_ticks) and len(prices) <= 1,
    }


def price_at(conn: sqlite3.Connection, asset: str, ts: float,
             tolerance_sec: int = PRICE_AT_TOLERANCE_SEC,
             daily_max_lag_sec: int = PRICE_AT_DAILY_MAX_LAG_SEC
             ) -> Optional[float]:
    """取 ts 时刻的行情价 —— 结算专用 (与 forward_price 的"之后"方向相反)。

    与 resolve_entry_price 的区别: resolve_entry_price 往**回**找 (入场价),
    price_at 往**前后各找一根** (到期价)。这是本次事故修复的核心口径:
    结算价 = 到期时刻的价, 不是扫描当刻的价。

    v386 P0: 日线兜底改为「最近一根 + 滞后上限」, 取不到就返回 None。
    """
    if not asset or ts is None:
        return None
    a = str(asset).strip().upper()
    if not a or a == "NONE":
        return None
    try:
        t = int(float(ts))
    except (TypeError, ValueError):
        return None
    tol = max(0, int(tolerance_sec))
    for sql, params in (
        # 1) 到期时刻**之前**最近一根 1 分钟线 (as-of —— 严格无未来函数:
        #    取到的价在到期瞬间即可知, 不会被"用了未来数据"质疑)
        #    注意: ohlcv_intraday 里混存 1m/3m/5m/10m/15m/1h/4h 多种 interval
        #    (实测 1m 82052 条, 4h 2720 条), 主键是 (asset, interval, ts),
        #    不过滤 interval 会取到 4h 的收盘价 —— 与到期时刻差 4 小时。
        ("SELECT close FROM ohlcv_intraday WHERE asset=? AND interval='1m' "
         "AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1", (a, t, t - tol)),
        # 2) 到期时刻之后第一根 1 分钟线 (forward —— 仅当到期前完全没有数据时用,
        #    例如刚建仓就到期; 结算发生在到期之后, 不构成未来函数)
        ("SELECT close FROM ohlcv_intraday WHERE asset=? AND interval='1m' "
         "AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT 1", (a, t, t + tol)),
        # 3) 日线兜底: 只取**到期时刻之前最近一根**, 且滞后不得超过 daily_max_lag_sec。
        #    旧实现 ORDER BY ts ASC + 6 天回看, 会拿到窗口里最老的日线(最多差 6 天),
        #    对 20 分钟 horizon 等于拿一周前的价格结算 —— 见 v386 P0 注释。
        ("SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? AND ts<=? "
         "ORDER BY ts DESC LIMIT 1",
         (a, t - max(0, int(daily_max_lag_sec)), t + tol)),
    ):
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] is not None:
            try:
                val = float(row[0])
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    return None


def forward_outcome_pct(conn: sqlite3.Connection, asset: str, from_ts: float,
                        entry_price: Optional[float], settle_days: int = SETTLE_DAYS
                        ) -> Optional[float]:
    """结算收益(%) —— forward 价与 entry_price 同源。

    无 entry_price / 无远期价 → None(调用方保持未结算, 不猜数)。
    **不做合理性过滤** —— 过滤由 is_implausible_outcome() 在写入点显式调用,
    这样调用方能区分"没数据"和"数据不合理"两种情况。
    """
    if not entry_price or float(entry_price) <= 0:
        return None
    fwd = forward_price(conn, asset, from_ts, settle_days)
    if fwd is None:
        return None
    return round((fwd - float(entry_price)) / float(entry_price) * 100, 3)


def is_implausible_outcome(pct: Optional[float]) -> bool:
    """|收益| 超过 ±90% 视为取价异常(绝不可能是 5 日真实波动)。"""
    if pct is None:
        return False
    try:
        return abs(float(pct)) > MAX_PLAUSIBLE_OUTCOME_PCT
    except (TypeError, ValueError):
        return False


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
# v433 时间戳归一（P0-4）
# ---------------------------------------------------------------------------
def coerce_epoch_ts(value: Any, *, default: Optional[int] = None) -> Optional[int]:
    """把任何时间表示统一成 epoch 秒（int）。无法解析时返回 default。

    为什么必须有这一个入口
    ----------------------
    2026-11-17 实测：`raw_news.timestamp` 22,340 行里 16,415 行是 ISO 字符串
    （`2026-11-17T14:06:40+08:00`）、5,922 行是 epoch 字符串、5 行是
    `YYYY-MM-DD HH:MM:SS`。SQLite 做 `now - timestamp` 时会把 ISO 文本当 0
    → 超龄、去重、窗口一律静默算错（实测跑出「积压 49 万小时」的假数）。

    规则
    ----
    · int/float / 纯数字串：直接当 epoch（13 位及以上按毫秒 → 秒）；
    · ISO-8601（含 T / Z / ±HH:MM）：按自带时区解析；
    · `YYYY-MM-DD HH:MM:SS`：按**本地时区**解析（这类历史数据都是本机写入）；
    · 其他一律 default，绝不猜。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        n = int(value)
        return n // 1000 if n >= 10_000_000_000 else n
    s = str(value).strip()
    if not s:
        return default
    if s.isdigit():
        n = int(s)
        return n // 1000 if len(s) >= 13 else n
    import datetime as _dt

    cleaned = s.replace("Z", "+00:00")
    for parse in (_dt.datetime.fromisoformat,
                  lambda x: _dt.datetime.strptime(x, "%Y-%m-%d %H:%M:%S"),
                  lambda x: _dt.datetime.strptime(x, "%Y/%m/%d %H:%M:%S")):
        try:
            dt = parse(cleaned)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            # naive → 固定 +08:00。这类历史数据都是本机（北京）写入的，语义就是东八区；
            # 用带参的 replace 而不是 astimezone()：后者跟运行环境走，CI 在 UTC 下
            # 会把 18:55 当成 UTC、整体差 8 小时，测试与回测窗口都会跟着漂。
            dt = dt.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return int(dt.timestamp())
    return default


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


_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._\-]{0,15}$")


def normalize_news_assets(raw: Optional[str]) -> str:
    """把 gate_assets / 旧 assets 归一成「逗号分隔的品种代码列表」。

    为什么必须是逗号串而不是 gate 的 JSON 数组
    ------------------------------------------
    `news_events.assets`（复数）的消费方按**逗号 split** 取品种代码：
      - `bridge_server._news_row_to_api_event()`：`assets_raw.split(",")[0]`
      - `calc_event_move_attribution.py`：`n_assets.split(",")`
    而 `raw_news.gate_assets` 是门卫(L3)标签，值是 **JSON 数组**，内容混着
    两类东西：
      - 板块类别（中文）：美股 / 原油 / 黄金 / 个股 / 加密主流 / 山寨
      - 品种代码：TSLA / META / NVDA / 000660 …

    把 JSON 数组直接灌进 `assets` 会出两个问题：
      1. 消费方 split 后拿到 `["美股"` 这种垃圾值，顶掉单数 `asset` 列里
         已经规范好的品种（'WTI' / 'BTC' / 'NONE'…）；
      2. `json.loads` 型的消费方（bridge_server 的星级筛选）又解析不了逗号串，
         静默把行丢掉。

    所以这里统一口径：**这一列只放品种代码，用逗号分隔**；
    板块类别不入此列 —— 消费方在 `assets` 为空时会回落到单数 `asset` 列
    （`_news_row_to_api_event` 的 assets → asset → 文本抽取 三级兜底）。
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    items: List[str] = []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = []
        if isinstance(parsed, list):
            for v in parsed:
                s = str(v or "").strip().upper()
                # 板块类别含中文，不是品种代码 —— 过滤掉
                if s and _TICKER_RE.match(s):
                    items.append(s)
    else:
        for v in text.split(","):
            s = v.strip().upper()
            # 板块类别含中文、残留的引号/方括号都不是品种代码 —— 一律过滤
            if s and _TICKER_RE.match(s):
                items.append(s)
    # 去重保序
    seen = set()
    out: List[str] = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return ",".join(out[:10])


# news_events.action 的合法枚举 (与 capability_acceptance.VALID_NEWS_ACTIONS 对齐)
VALID_NEWS_ACTIONS = ("BUY", "SELL", "HOLD")


def normalize_news_action(raw: Any) -> str:
    """把任意来源的 action 归一到 BUY / SELL / HOLD。

    为什么必须在**写入边界**归一
    --------------------------
    `news_events.action` 是全库被统计的枚举列 (验收 #13 / C8 直接按
    ('BUY','SELL','HOLD') 判定; 非法值会被下拉框过滤掉 → 统计静默失真),
    但写入方并不统一:
      · `bridge_server._cot_emit_news_event` 写的是 'bullish' / 'bearish'
        (COT 反向指标的语义标签) —— 不在枚举内;
      · `bridge_server._insert_news_batch` 直接透传上游 `ev["action"]`,
        上游给 None 就落成 NULL;
      · `timeseries.insert_news_event` 只做了 `.upper()`, 'E020' 之类照样落库。
    这些行会被 `news_score_worker` 当"非法行"重打分覆盖 —— 既在验收快照上
    制造非法枚举残留, 又把 COT 的反向解读语义冲掉 (反向信号被关键词打分
    覆盖成另一个方向)。

    口径: BUY/SELL 保留; 'bullish'/'bearish' → BUY/SELL (语义等价:
    极空 → 看涨反转 = BUY, 极多 → 看跌反转 = SELL); 其余一律 HOLD (无信号)。
    """
    if raw is None:
        return "HOLD"
    text = str(raw).strip().upper()
    if text in VALID_NEWS_ACTIONS:
        return text
    if text == "BULLISH":
        return "BUY"
    if text == "BEARISH":
        return "SELL"
    return "HOLD"


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
    url: str = "",
    gate: Optional[dict] = None,
    gate_unavailable: int = 0,
) -> int:
    """写入 raw_news。生产库若尚未 migrate 出 ts/published_at 列，自动降级，避免整批入库失败。

    published_at = 新闻平台声称的发布时间（原文/解析后的字符串，无则空串）。
    采集入库时刻是 timestamp/ts —— 两者分离后，前端/导出可对比"平台几点发布 vs 我们几点抓到"。
    """
    existing = table_columns(conn, "raw_news")
    # v433（P0-4）：timestamp 一律落成 epoch 秒字符串，和 ts 同源。
    # 原来调用方有的传 int、有的传 ISO，导致同一列三種格式并存：
    # SQL 里 `now - timestamp` 对 ISO 行按 0 算 → 超龄/窗口/去重全静默算错。
    _ts_int = coerce_epoch_ts(ts, default=None)
    if _ts_int is None:
        _ts_int = coerce_epoch_ts(timestamp, default=None)
    if _ts_int is None or _ts_int <= 0:
        _ts_int = int(_safe_now())
    timestamp_value = str(int(_ts_int))
    # v446（2026-11-18）：入库前零成本筛子 —— 重复 / 统计·分析类（非新闻）。
    # 用户指令："筛选吧重复的、垃圾信息、统计类分析类（非新闻类信息）筛出去"。
    # 实测入库 16,432 条/30 分钟，其中绝大多数是同一条快讯被多源转发 + 榜单/复盘噪音，
    # 它们把门卫额度烧光、把真快讯挤在队尾。命中就直接标 FILTERED（仍入库留痕，
    # filter_reason 记原因），既不进门卫也不进主分析 —— 零 LLM 成本。
    _pre_reason = ""
    if str(status).upper() == "PENDING":
        try:
            import news_keepfilter as _kf  # noqa: WPS433
            _fp_pre = (content_fingerprint(content)
                       if ("content_fingerprint" in existing or "first_seen_ts" in existing)
                       else "")
            _pre_reason = _kf.classify(content, conn=conn, fingerprint=_fp_pre,
                                       ts_int=_ts_int) or ""
            if _pre_reason:
                status = "FILTERED"
                is_noise = 1
        except Exception:
            _pre_reason = ""
    columns = ["source", "content", "timestamp", "status", "is_noise", "relevance_score"]
    values: List[object] = [source, content, timestamp_value, status,
                            int(is_noise), float(relevance_score)]
    if "ts" in existing:
        columns.append("ts")
        values.append(int(_ts_int))
    if "published_at" in existing:
        columns.append("published_at")
        values.append(str(published_at or "")[:64])
    # v311: 原文链接 —— 采集方给了就用, 没给就从正文里正则抽第一个 http(s) URL
    if "url" in existing:
        _u = str(url or "").strip()
        if not _u:
            _m = _URL_IN_TEXT_RE.search(str(content or ""))
            _u = _m.group(0)[:500] if _m else ""
        if _u:
            columns.append("url")
            values.append(_u)
    # 全网原始溯源：归一化指纹 + 同指纹最早采集时刻（跨源相似新闻归同簇，记录全网最早出现）
    if "content_fingerprint" in existing or "first_seen_ts" in existing:
        fp = content_fingerprint(content)
        first_seen = timestamp_value
        if fp:
            try:
                row = conn.execute(
                    "SELECT timestamp FROM raw_news WHERE content_fingerprint=? ORDER BY timestamp ASC LIMIT 1",
                    (fp,),
                ).fetchone()
                if row and row[0]:
                    first_seen = min(timestamp_value, row[0])
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
        # v453 P0-3：主品种落库前先归一（黄金→XAU、原油→WTI、GOLD→XAU…）。
        # 实测 11-18 可交易的 921 条里 109 条写的是中文名，下游「按 main_asset 填池」
        # 只认代码 → 这些行整条走完主分析仍判 NONE。
        _ma = str(gate.get("main_asset") or "")[:20]
        try:
            from analysis.contract import canonical_asset as _canon
            _ma = _canon(_ma)[:20]
        except Exception:
            pass
        gate_cols = {
            "gate_core_impact": str(gate.get("core_impact") or "")[:300],
            "gate_is_material": 1 if gate.get("is_material") else 0,
            "gate_assets": json.dumps(gate.get("assets") or [], ensure_ascii=False)[:120],
            "gate_main_asset": _ma,
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
    # v320: 门卫不可用标记（门卫 LLM 因通道故障未判定 → 保留待回放，不当噪音）
    # v3215(FIX-8b): 取值扩到 0/1/2，不能再压成布尔 ——
    #   0 正常判定 · 1 不可用(冻结待回放) · 2 鉴权失效/额度耗尽(降级放行)
    #   原来 `1 if gate_unavailable else 0` 会把 2 压成 1 → 放行的行被冻死。
    if "gate_unavailable" in existing:
        columns.append("gate_unavailable")
        try:
            values.append(max(0, min(2, int(gate_unavailable or 0))))
        except (TypeError, ValueError):
            values.append(0)
    # v441（P0-6）：入库当场就被归档的行，也要留下"为什么"。
    # 事故：门卫判『排除』的新闻在 realtime_filter 里就被置成 status='FILTERED'，
    # 走到这里入库时 filter_reason 一直是空串 —— 于是漏斗只能报"否掉 N 条"，
    # 无法区分"门卫判噪音"和"门卫故障/规则命中"。实测生产库因此积了 15,986 条空理由。
    # 判定顺序：门卫未判定 → 门卫判噪音 → 门卫判无信号 → 纯规则命中(无门卫结论)。
    if "filter_reason" in existing and status == "FILTERED":
        _verdict = str((gate or {}).get("verdict") or "")[:10]
        _gu = 0
        try:
            _gu = int(gate_unavailable or 0)
        except (TypeError, ValueError):
            _gu = 0
        if _pre_reason:
            _reason = _pre_reason
        elif _gu == 2 or _verdict == "未判定":
            _reason = "gate_undetermined"
        elif "排除" in _verdict:
            _reason = "gate_noise"
        elif "无信号" in _verdict:
            _reason = "gate_no_signal"
        elif int(is_noise or 0):
            _reason = "noise_rule"
        else:
            _reason = ""
        if _reason:
            columns.append("filter_reason")
            values.append(_reason)
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
        except sqlite3.OperationalError as _fs_exc:
            # v322: 拉平失败 → 同簇新闻的"全网最早出现时间"不一致（溯源显示偏差）
            print("[DB] ⚠️ first_seen_ts 拉平失败 (fp=%s): %s: %s"
                  % (str(fp)[:12], type(_fs_exc).__name__, str(_fs_exc)[:100]),
                  flush=True)
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Schema — latest complete definitions
# ---------------------------------------------------------------------------



# Incremental columns for DBs created by older CREATE TABLE versions.
# (name, column definition) — added via ALTER TABLE only when missing.
# v311: 从正文里抽原文链接（采集方没给 url 时的兜底）
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'\]\)}]+", re.I)


_RAW_NEWS_COLUMNS: List[Tuple[str, str]] = [
    # v406: status 原先只在 _CREATE_RAW_NEWS 里出现（相当于"只有新建库才有"）。
    # 旧库/薄表缺这一列时，尾部 _INDEXES 建 idx_raw_news_status 直接报
    # "no such column: status"，整段 migrate 中断（tech_signals 等都建不出来）。
    # 生产库取值是 PENDING/PROCESSING/DONE/FILTERED，默认 PENDING 与之一致。
    ("status",          "TEXT NOT NULL DEFAULT 'PENDING'"),
    ("is_noise",        "INTEGER NOT NULL DEFAULT 0"),
    ("relevance_score", "REAL    NOT NULL DEFAULT 0.0"),
    ("ts",              "INTEGER"),
    # 引擎认领(claim)开始处理的时间：看门狗用它判断 PROCESSING 是否卡死（区别于新闻入库 ts）
    ("processing_at",   "TEXT DEFAULT NULL"),
    # 新闻平台声称的发布时间（采集器解析出的原文时间；无则空串）。
    # timestamp/ts 是"我们采集入库时刻"——两者分离用于平台时效对比。
    ("published_at",    "TEXT NOT NULL DEFAULT ''"),
    # v311: 原文链接（采集器从正文/条目里解析出来的源站 URL）——用于前端「查看原文」。
    # 采集端拿不到时为空串，不阻塞入库。
    ("url",             "TEXT DEFAULT ''"),
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
    # v408: 为什么这条新闻不在分析队列里（留痕，不改任何判定）。
    #   历史问题：status='FILTERED' 同时表示"判了噪音"和"超龄没被认领就归档"，
    #   而后者 is_noise=0 —— 认领 SQL 只取 status='PENDING' AND is_noise=0，
    #   于是这批新闻结构性不可能被分析，也永远不会出现在任何计数里（实测 11-16
    #   一天 742/1115 条就这么消失，含 52 条门卫已判"可交易"的）。
    #   取值：''（未知/噪音路径）· stale_expired（超 NEWS_ANALYZE_MAX_AGE_HOURS 归档）
    ("filter_reason",   "TEXT NOT NULL DEFAULT ''"),
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
    # v320: 门卫(LLM)不可用标记。
    # 背景：v176.9 起门卫调用失败（网络/配额 429/401）时把新闻置 is_noise=1 当噪音丢掉，
    # 这在"单次抖动"下是对的（避免主分析跑出伪造的"AI 观望"假信号），
    # 但在"通道系统性故障"（如 MiniMax 额度耗尽，持续数小时全池 429）下，
    # 会把成百上千条不可判定的新闻永久销毁，LLM 恢复后也回放不了。
    # gate_unavailable=1 表示"门卫未判定（基础设施故障）"而非"是噪音"：
    #   · is_noise 保持 0（不当噪音）
    #   · ai_worker 认领时排除（gate_unavailable=0 才认领），避免伪造信号
    #   · 留在库里等 replay_deferred_gate.py 回放补判
    ("gate_unavailable",        "INTEGER NOT NULL DEFAULT 0"),
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
    # v399: 决策来源留痕（只描述事实, 不参与打分），判据在 decision_provenance.py。
    #   asset_route     —— target_none / decided:<class>；原来只有一次性脚本回填,
    #                      运行时从不写, 于是"S18 无标的"分不清是真没标的还是路由漏了。
    #   hard_data_status—— ok / missing:no_local_feed / missing:feed_stale / …
    #                      硬数据缺口从"空字符串"变成可统计的原因。
    ("asset_route",      "TEXT    DEFAULT ''"),
    ("hard_data_status", "TEXT    DEFAULT ''"),
    # v406: 决策种类 —— analysis（有标的、走正常分析链）/ routing_miss（target=NONE，
    #   没有可分析对象）。S16 降级率只在 analysis 池里算，routing_miss 由 S18 单列；
    #   否则"路由没打上"会被算成"模型没被问"，两个问题混成一个分数。
    ("decision_kind",    "TEXT    DEFAULT 'analysis'"),
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
    # v313 P1: 决策之后**市场实际**的涨跌幅 (用真实 K 线算, 单位 %)。
    # 与 forward_pnl 语义严格区分:
    #   forward_pnl     = 模拟盘这笔单的净盈亏 (只有实际开仓的 BUY/SELL 才有, HOLD 永远 NULL)
    #   forward_move_pct = 该决策之后市场本身走了多少 (任何决策都有, HOLD 也有)
    # 验收 #2 的 Spearman(权重, |实际涨跌|) 需要后者 —— 前者只覆盖 0.2% 的决策。
    ("forward_move_pct", "REAL    DEFAULT NULL"),
    # v315 P0: 由 forward_move_pct + suggested_action 推出的**方向判定**
    # (WIN/LOSS/BREAKEVEN)。只回答"这个来源这次方向对不对", 与
    # is_correct/settled (模拟盘这笔单结算了吗) 是两回事, 不混用。
    ("truth_verdict",    "TEXT    DEFAULT ''"),
    ("truth_verdict_ts", "INTEGER DEFAULT 0"),
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
    # v415: 命中来源。'model'=模型在 JSON 里真的声明采纳；'auto'=_auto_match_insights
    # 的关键词兜底（不是采纳，只是"文本相似"）。混在一起统计会把假回流当成绩效。
    ("insight_hit_source", "TEXT NOT NULL DEFAULT ''"),
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



# v20251014: 信号权重评估系统
#   3 张新表:
#     1. signal_weight_history  决策每次权重变化 (insert 时 + cron 复算时各记一行)
#     2. source_stats           按 (source × asset) 聚合历史 win_rate, 闭环校准 source_mult
#     3. signal_weight_cron_log cron 任务执行日志 (调试/审计)




# v383: 处置账（用户对单条信号的人工动作：静默品种 / 忽略该笔 / 反手）
# 为什么要落库：静默必须能被写入口读到（否则「静默」只是前端按钮），
# 反手需要留下时间和原决策 id，才能在复盘时算「人工干预相对系统信号的 lift」。


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
    except sqlite3.Error as _sw_exc:
        # v322: 写入失败 → 信号权重历史静默丢数据（命中率统计会悄悄失真）
        print("[DB] ⚠️ 信号权重历史写入失败: %s: %s"
              % (type(_sw_exc).__name__, str(_sw_exc)[:120]), flush=True)
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
    "    call_site    TEXT    NOT NULL DEFAULT '',"
    "    cached_tokens INTEGER NOT NULL DEFAULT 0,"
    "    error_body   TEXT    NOT NULL DEFAULT ''"
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






_STRATEGIES_COLUMNS: List[Tuple[str, str]] = [
    ("is_current", "INTEGER NOT NULL DEFAULT 0"),
    ("current_version_id", "INTEGER DEFAULT NULL"),
]



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
    # v396 P2: 扣费前的毛收益。pnl_pct 一直是"扣费后"的净值，导致
    # "策略本身有没有 alpha" 和 "成本吃掉多少" 混在一个数里，无法归因。
    ("gross_pnl_pct", "REAL DEFAULT NULL"),
]




# v405: 财报三件套（利润表 / 资产负债表 / 现金流量表）落库。
# 只存季度原值，不做任何"补数/插值"——缺字段就是 NULL，展示层标 N/A。


# v400: 财报日历 + 一致预期（FMP earnings-calendar 的 epsEstimated/revenueEstimated
# 与 epsActual/revenueActual）。有 estimate 才有"预期差"，只有日期不算。


# v406: A股/港股财务读数（东财口径）。与美股 `equity_fundamentals` 分表而不是共用：
#   ① 美股是"单季 → 自己加 TTM"，A股是"累计口径 → 自己减出 TTM"，两套口径混一张表
#      以后没人分得清某个数是单季还是累计；② 字段也不同（A股有 BPS/加权ROE，美股有 capex）。


# 估值分位的历史底料：美股用 macro_snapshots 的 eq.pe/eq.pb，
# A股/港股没有对应的宏观层条目 → 自己攒一张小表，同样按"样本 <60 不作结论"处理。




# v176.2：市场风格持久化（_compute_market_style 之前只输出不存，前端拿不到历史）
# 写入方：engine 每 5 分钟调用一次 _snapshot_market_style，存当前 (asset, regime, layer1..5_alerts)
# 读出方：ai_decisions INSERT 时给 regime_at_decision 字段赋值；前端 /api/market/regime/history 端点拉历史

# v176.2：决策反思表（"上次错哪/这次要怎么调整"）
# 写入方：engine 每 N 条决策 / 每小时触发一次 _reflect_recent_decisions，写入本表
# 读出方：ai_decisions INSERT 时给 bias_after_reflection 字段赋值；engine 把反思注入到下次同品种 prompt
# 设计：bias_text ≤200 字，L1-L2 prompt 末尾追加一段，反思条目本身是 self-loop 不是规则




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
    # v399 P2: 洞察 = 可测假设 or 只能观察。判据在 insight_hypothesis.py（纯函数）。
    # 没有参数键 / 单位不等价 / 没说方向 → observe_only=1，不进晋升队列。
    ("insight_hypothesis", "TEXT DEFAULT ''"),
    ("insight_param_key", "TEXT DEFAULT ''"),
    ("insight_param_bucket", "TEXT DEFAULT ''"),
    ("insight_direction", "INTEGER DEFAULT 0"),
    ("insight_window_h", "INTEGER DEFAULT 0"),
    ("observe_only", "INTEGER NOT NULL DEFAULT 0"),
    ("observe_reason", "TEXT DEFAULT ''"),
    ("insight_classified_at", "TEXT DEFAULT ''"),
]

# v173：结构化校验真实性飞轮 — 监听 ai_decisions.structured_check verdict，
# 当同一 (asset, event_type_code, verdict) 累计 ≥ N 笔时 → 自动沉淀为 review_insights，
# 让 review_agent._evaluate_insights 复用现有"停用/复活/自愈"逻辑。

# 历史新闻回溯：单条新闻的交易 AI 归因总结缓存（避免重复调用 LLM）

# 归因复核队列：逐条归因明细 + 人工确认/纠错状态，纠错自动回灌为经验规则

# 提示词库：第 1 层（采集入库噪声过滤）的关键词，人工可增删（内置词可停用）

# 单笔反思：每笔模拟交易结算后的即时归因（LLM），供复盘聚合与经验沉淀

# ══════════════════════════════════════════════════════════════════
# v188 反思系统增强 —— 4 张缺失表 DDL + decision_layer_signals 聚合表
# 之前 workers 读写 experience_entries / experience_applications /
# gate_reflections_cycle / trade_reflections_v2 4 张表都假定存在，但
# DDL 不在 migrate() 里 —— 新建/迁移过的库会 OperationalError。本补丁
# 全部补上 + 新增 decision_layer_signals / reverse_attributions 颗粒度统计基础表。
# ══════════════════════════════════════════════════════════════════

# ⚠️ v323 状态说明：experience_entries 目前**未接入任何写入方**（只有手动 API
# /api/v188/experiences/add 能插入），引擎注入提示词用的是 review_insights
# （review_agent.load_active_insight_items）。保留 DDL 以兼容历史，不要据此判断
# "反思进化是否工作"——那要看 review_insights.active = 1 的条数。







# ── v232.6 P1: 反思→policy 闭环 (让"反思"真正影响"决策") ──
# 仪表盘给的是事实（E001 累计亏 81%），但 ai_worker 下次遇到同样的信号不会知道这件事。
# 这两张表把"反思结论"翻译成"可审核的策略补丁候选",由人工审过才允许 apply。
# 设计原则：
#   - 候选生成 zero-risk：只写新表，不改 ai_worker 行为
#   - 闭环受控：apply 必须有 reviewer + rationale + 有效期
#   - 同 target_module + 同 patch_payload_hash UNIQUE，防止无限重复生成








# ── v270 P2: 多源数据 fallback 健康日志（feed_health） ──
# 每次按 feed_redund_policy 走多源尝试时记一行 — 用于仪表盘 + 反思面板的「数据系统判断」项
# 关键设计：status 只能是 4 个枚举 (success / degraded / failed / skipped)
#          没有 severity/level — UI 全部平铺



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

# v413: 反思来源/证据列。
# 之前一张表里混着三类东西：模板生成的 HOLD 记账(143 条, 同一句话)、模型写的
# 真反思(13 条)、以及影子结算样本(0 条)。不分开就无法回答「反思有多深」——
# 所有覆盖率/深度分都被模板行拉平。origin 让统计能按来源切片，evidence 让
# "凭什么这么判"可复核（空证据 = 可判定的缺失，不等于没问题）。
_REFLECTION_V413_COLUMNS = [
    ("trade_reflections_v2", [("origin", "TEXT NOT NULL DEFAULT ''"),
                              ("evidence", "TEXT NOT NULL DEFAULT ''")]),
]
_BACKTEST_V189_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ad_run_id ON ai_decisions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_dls_run_id ON decision_layer_signals(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_trv2_run_id ON trade_reflections_v2(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_eap_run_id ON experience_applications(run_id)",
]


# v189-1-补: 每日重置登记表 (与原 backtest_runs 共存, 不破坏旧 schema)
_BACKTEST_DAILY_RUNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bdr_archived_started ON backtest_daily_runs(archived, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_bdr_asset_class ON backtest_daily_runs(asset_class)",
]

# v189-2: 反思系统每日归档 — 3 张聚合表
_REFLECTION_DAILY_JOURNAL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rdj_date ON reflection_daily_journal(journal_date)",
    "CREATE INDEX IF NOT EXISTS idx_rdj_run_id ON reflection_daily_journal(run_id)",
]

_REFLECTION_CYCLE_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rcl_decision ON reflection_cycle_log(decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_run_id ON reflection_cycle_log(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_outcome ON reflection_cycle_log(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_rcl_created ON reflection_cycle_log(created_at)",
    # v189-fix: UNIQUE(decision_id, run_id) 防 worker 重跑/锁重试时重复写入.
    # 已存在的旧表没这个约束, CREATE UNIQUE INDEX 幂等兼容.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rcl_dec_run ON reflection_cycle_log(decision_id, run_id)",
]

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

# v482: 教训绑代码代次 —— 反思行记下产出它的那一代代码指纹(<git短哈希>/<参数哈希>)。
# 换代后旧教训可按指纹失效, 不再无限期进 prompt / 经验池。
# 空串 = 未知代次(迁移前的历史行), 学习侧默认不采信旧代, 只用于展示。
_TRADE_REFLECTIONS_V2_V482_COLUMNS = [
    ("code_fingerprint", "TEXT DEFAULT ''"),
]
_TRADE_REFLECTIONS_V2_V482_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trv2_fingerprint "
    "ON trade_reflections_v2(code_fingerprint)",
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


# ══════════════════════════════════════════════════════════════════════════
# v309 P0-1: trade_reflections_v2 三维度 (A/B/C) 归因列
# ══════════════════════════════════════════════════════════════════════════
# 背景: bridge_server.py 的 /api/v237/missed-signals/by-dimension 与
# /api/v291/reflections/summary 一直在读这些列 (dimension / subcategory /
# hit / actual_outcome_pct / confidence_at_time / reflection_text / settled),
# 但**权威 schema 里从来没定义过它们** → 迁移后依然缺列 → 端点查询直接
# OperationalError → 被 except 吞掉 → 前端"三维度错因"卡片恒为 n=0。
#
# 维度口径 (A 买卖信号 / B 信息分析方向 / C 技术错误) 与细分小类名的
# **单一权威定义**在 reflection_dimension_map.py, 本处只负责列结构。
#
# 语义:
#   dimension          'A' | 'B' | 'C'   (空串 = 无法归类, 统计时跳过)
#   subcategory        该维度下的细分小类 (12 个受控枚举之一)
#   hit                1 = 该反思对应的判断最终命中 (is_correct=WIN)
#   actual_outcome_pct 该笔实际结果 (百分数, 如 1.25 = +1.25%)
#   confidence_at_time 决策当时的置信度 (0-1)
#   reflection_text    反思正文摘要 (root_cause/lesson 摘录)
#   settled            1 = 已结算
# ══════════════════════════════════════════════════════════════════════════
_TRADE_REFLECTIONS_V2_V309_DIMENSION_COLUMNS = [
    ("dimension",          "TEXT NOT NULL DEFAULT ''"),
    ("subcategory",        "TEXT NOT NULL DEFAULT ''"),
    ("hit",                "INTEGER NOT NULL DEFAULT 0"),
    ("actual_outcome_pct", "REAL NOT NULL DEFAULT 0.0"),
    ("confidence_at_time", "REAL NOT NULL DEFAULT 0.0"),
    ("reflection_text",    "TEXT NOT NULL DEFAULT ''"),
    ("settled",            "INTEGER NOT NULL DEFAULT 0"),
    # v326: A 类细分的证据留痕 —— 记下"这条为什么被判成这个子类"
    # (如 deny_reason=asset_BTC / 同组 3 个资产同向异动), 没有证据就无法复核
    ("subcategory_evidence", "TEXT NOT NULL DEFAULT ''"),
]


# v404 (反思评审 2026-11-16): 错误码有效性台账 —— 33 个码至今没有一个回答过
# "加了它之后胜率/回撤/错单率改善了多少"。没有台账，码的数量只是叙事资产。
# tier: hard = 可机器验证且进闸门 / soft = 文本风格只提示 / research = 待验证假设不进统计

_ERROR_CODE_LEDGER_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ecl_tier ON error_code_ledger(tier, triggers_30d DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ecl_last ON error_code_ledger(last_seen_at DESC)",
]


# v482: 漏检信号记账独立表 —— 只记"该有信号但给了 HOLD"这件事本身。
# 为什么不放 trade_reflections_v2: 它没有成交、没有 pnl、永远判不出对错，
# 留在反思表里只会把"反思条数/深度分/低质短语"全部拉平（实测 52/56 = 93% 是它）。
_CREATE_MISSED_SIGNAL_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS missed_signal_reflections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL UNIQUE,   -- 一条决策只记一条，幂等 upsert
        asset TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT 'HOLD',
        dimension TEXT NOT NULL DEFAULT '',    -- A/B/C 维度（reflection_dimension_map）
        subcategory TEXT NOT NULL DEFAULT '',
        root_cause TEXT NOT NULL DEFAULT '',
        lesson TEXT NOT NULL DEFAULT '',
        verification TEXT NOT NULL DEFAULT '',
        move_pct REAL,                         -- 决策后 N 天实际涨跌（%）；None = 取不到
        hit INTEGER,                           -- 1 = 涨跌达到 HIT_MOVE_THRESHOLD_PCT
        confidence_at_time REAL,               -- 当时置信度；None = 没算过
        reflection_text TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT ''
    );
"""

_MISSED_SIGNAL_REFLECTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_msr_created ON missed_signal_reflections(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_msr_asset ON missed_signal_reflections(asset, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_msr_dim ON missed_signal_reflections(dimension, subcategory)",
]


# v230: 自循环反思表 — 元反思对元反思, 反思对反思
# 父反思 → 元反思 (level=1)
# 元反思 → 元元反思 (level=2)
# 链路可深可浅, 但默认 level<=2 防爆炸

_REFLECTION_CYCLES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rc_parent ON reflection_cycles(parent_reflection_id)",
    "CREATE INDEX IF NOT EXISTS idx_rc_level ON reflection_cycles(cycle_level)",
    "CREATE INDEX IF NOT EXISTS idx_rc_finding ON reflection_cycles(finding_kind)",
    "CREATE INDEX IF NOT EXISTS idx_rc_created ON reflection_cycles(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rc_source ON reflection_cycles(source_kind, created_at DESC)",
]


# v230: 决策时点的 K线缓存 — reflection_deep.py 计算后写这里, 避免重复查 kline_bars

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

_DAILY_RESET_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_drl_date ON daily_reset_log(reset_date)",
    "CREATE INDEX IF NOT EXISTS idx_drl_run_id ON daily_reset_log(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_drl_trigger ON daily_reset_log(trigger)",
    "CREATE INDEX IF NOT EXISTS idx_drl_created ON daily_reset_log(created_at)",
]


_SIGNAL_CHAIN_ARCHIVE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sca_run_id ON signal_chain_archive(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_sca_decision_date ON signal_chain_archive(decision_date)",
    "CREATE INDEX IF NOT EXISTS idx_sca_asset ON signal_chain_archive(target_asset)",
    "CREATE INDEX IF NOT EXISTS idx_sca_audit_status ON signal_chain_archive(audit_status)",
    "CREATE INDEX IF NOT EXISTS idx_sca_is_correct ON signal_chain_archive(is_correct)",
    "CREATE INDEX IF NOT EXISTS idx_sca_reflection_layer ON signal_chain_archive(reflection_layer)",
]


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

_STRATEGY_PARAM_ADJUSTMENTS_COLUMNS: List[Tuple[str, str]] = [
    # 增量列（基线字段已在 _CREATE_*；以下为追加迁移列，预留扩展）
    ("applied_setting_change", "TEXT DEFAULT ''"),
    ("note",                   "TEXT NOT NULL DEFAULT ''"),
]


# v173 决策证据链：每条 ai_decision 用了哪些 raw_news 作证据 + 当时的溯源快照
# 用于 /api/decisions/:id 全文溯源：决策 → 新闻原文 + 平台发布时间 + 抓到时间 + 内容指纹
_DECISION_EVIDENCE_COLUMNS = []  # 新表无迁移列（v173 一次性落地）


# v174 信号残留表：新闻事件的影响随时间指数衰减（carry-over 长期影响）
# 用于：同事件多波新闻（如非农初值 → 修正值）、政策决议（声明 → 发布会），
# 第一波新闻已经下决策后，第二波新闻要参考"残留影响"避免重复建仓。
#   impact：初始强度（0.0-1.0）
#   decay_lambda：指数衰减系数（单位 1/秒）；λ 越大衰减越快
#   ttl_seconds：TTL（默认 24h），到期直接清；期内残留 = impact * exp(-λ * age)
_SIGNAL_STATE_COLUMNS = [
    # v174 一次性完整建表，迁移列预留扩展位
    ("reason", "TEXT NOT NULL DEFAULT ''"),
]

# 用户自定义 AI 分析 prompt 库：启用的条目注入每次 AI 分析的 system prompt（可增删/启停）

# AI 主 prompt 覆盖表：用户可在页面编辑主 prompt（system_prompt / json_force_prompt /
# market_structure_block / doubao_prompt），存这里覆盖代码内置默认值；无记录时引擎用代码默认。

# ai_main_prompts 增量列（老库补 base_version：保存时代码内置主 prompt 代际）
_AI_MAIN_PROMPTS_COLUMNS: List[Tuple[str, str]] = [
    ("base_version", "INTEGER NOT NULL DEFAULT 0"),
]

# 品种专项分析规则表：按金融品类（XAU/WTI/BTC/ETH/美股代码…）单独设置分析规则。
# AI 分析时按 target_asset / sibling 品种套用对应规则；asset='all' 为全品种兜底。

# 因果链库：宏观/市场因果知识条目（如"美元加息 → 黄金/原油/加密承压或走强"）。
# AI 分析新闻时，命中 trigger_words 的启用条目被注入到八段式之后的新段落
# [因果链分析]（因：…；果：…），并引导"总结"与八段式互相印证。

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


# ── P1：稳定运行可观测性 ──
# Worker 心跳表：每个 worker task（ingest/ai/reflection/forward）每个 tick 写一行。
#   看门狗 / /metrics 扫描 stale_heartbeats → 报警卡死的 worker。
#   last_status=ok / busy / error → 区分"在跑但慢" vs "真死"。
#   last_error 仅当 status=error 时非空。

# DLQ 表：永久死信队列。Ingest 解析失败 / reflection MAX_RETRY 耗尽 / ai_worker LLM 永久不可达
# 都写一行，原 payload JSON 留痕，便于人工补数 / 周报回顾。
# 注意：DLQ ≠ 重试；DLQ 是"我已经放弃了，重试也救不了"的占位。

# v175 异常签名（Phase 2）：全板块异动 / 多因子触发的落库历史。
# 触发后由异常链路写入，供「重要新闻判定」与复盘页追溯；status 生命周期：
#   open -> attributed（已完成归因注入）或 dismissed（人工否掉）。

# v182.3 Phase 2 异常归因（AI 一句话归因 + 驱动候选 + 跨品种关联 + 行动建议）。
#   - 同一 signal_id 可多次写入（保留多次归因历史），前端默认取最新。
#   - summary 限长 300 字；drivers_json / cross_json 是 JSON 字符串。
#   - severity_hint: 1=轻度 / 2=中度 / 3=严重。


# P0-3 数据源状态落库表（供"信息源监控面板"前端轮询）。
#   current  表：UPSERT 实时状态（online/error/stale/unknown），last_tick_at 给 staleness 判
#   history 表：每次 report() 追加一行（含 detail / latency），保留 7 天滚动窗口

# ────────────────────────────────────────────────────────────────────
# 事件结构化 — 字典表（PR-1）
# ────────────────────────────────────────────────────────────────────



# ────────────────────────────────────────────────────────────────────
# 事件结构化 — 实体表（PR-3）
# 注意：现有 news_events 表是「事件流时序」（每行=AI 决策信号），
# 新表 event_atom 是「标准化事件」（每行=客观事件，0~N 个/新闻），语义不同，不复用名。
# ────────────────────────────────────────────────────────────────────




# ── 事件影响曲线（PR-时间结构化） ─────────────────────────────────────────────
# 把"事件发布后 5m / 30m / 2h / 1d 价格分桶"结构化存储：
# - window_label: '5m' / '30m' / '2h' / '1d' / 'extended'（与 tz_utils.WINDOW_LABELS 对齐）
# - sample_ts: 采样 epoch 秒（UTC epoch，跨时区统一）
# - delta_pct: 该时刻相对事件发布价的变化百分比
# - volume_ratio: 量能比（>1 放量, ≈1 存量）
# 设计目的：复盘 / LLM 归因 / 异常检测的"价格-时间曲线"维度不再需要 join 行情即可得。

# v189-7: Coinglass + LSR 中转 API 落库
#   - coinglass_lsr_samples: 每次 watcher 拉到的单币信号原始快照
#   - coinglass_lsr_health:  每次拉取的元信息 (key 剩余次数/过期/拉取耗时)
# 数据来源: api/vip2.coinglass.site/api/lsr/signal/{SYM}?mode=trader|whale
# 模式 (trader/whale): trader=全体散户+大户 / whale=仅大户
# 比率 (ratio): 多头持仓/空头持仓, >1 多头多, <1 空头多
# delta_*_pct: 相对窗口变化 (2m/30m/4h), 三窗口同向即趋势强烈

_COINGLASS_LSR_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS {f'{_COAINGLASS_INDEX_NAME}'} "
    f"ON coinglass_lsr_samples(symbol, ts);",
    f"CREATE INDEX IF NOT EXISTS {f'{_COAINGLASS_INDEX_NAME_WHALE}'} "
    f"ON coinglass_lsr_samples(mode, symbol, ts);",
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_mode_ts "
    "ON coinglass_lsr_samples(mode, ts);",
]

# v189-7: Coinglass API 元信息/健康度 (key 剩余次数/过期日/拉取耗时)
_COINGLASS_LSR_HEALTH_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_health_ts "
    "ON coinglass_lsr_health(ts);",
    "CREATE INDEX IF NOT EXISTS idx_coinglass_lsr_health_ok "
    "ON coinglass_lsr_health(ok, ts);",
]


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
_STRATEGY_REFLECTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_strategy_reflections_status ON strategy_reflections(status, created_at DESC)",
]

# ── v201: 宏观事件 × K线影响（每 5min worker 写） ──
# impact_pct = max(|max_high - pre_close|, |pre_close - min_low|) / pre_close
# severity 阈值：>=0.008 high; >=0.003 medium; <0.003 low
# UNIQUE (event_source, event_id, asset) 幂等防重写
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
_QUALITY_REJECTIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_qr_rule ON quality_rejections(rule, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qr_decision ON quality_rejections(decision_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qr_created ON quality_rejections(created_at DESC)",
]

# ── v316 Crowd Pulse：二级市场人群画像 / 权威度 / 月榜 ──────────────────
# 数据来源：crowd_sources.py 真实抓取（百度热搜·实时/财经榜、同花顺热门股榜、
# CoinGecko trending、Google News RSS）。每条原始条目都带 source+external_id+url，
# 可逐条回溯；不做任何编造填充。






_CROWD_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_csi_source ON crowd_source_items(source, collected_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_csi_day ON crowd_source_items(day)",
    "CREATE INDEX IF NOT EXISTS idx_csi_topic ON crowd_source_items(topic_key)",
    "CREATE INDEX IF NOT EXISTS idx_ctd_day ON crowd_topic_daily(day)",
    "CREATE INDEX IF NOT EXISTS idx_ctd_topic ON crowd_topic_daily(topic_key)",
    "CREATE INDEX IF NOT EXISTS idx_cmr_month ON crowd_monthly_rankings(month, authority_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cas_month ON crowd_audience_segments(month, dimension, rank_no)",
    "CREATE INDEX IF NOT EXISTS idx_csa_month ON crowd_source_authority(month, authority_score DESC)",
]

# ── v321.3 P1: 闸门分组校准 (需求 #1) ───────────────────────────────────────
# 背景: 闸门「整体拒对率 ≈ 基础概率」(实测 81.0% vs 81.9%, 提升 +0.03pp)
# 看起来毫无价值 —— 但那是**整体**数字。按分组看真相完全不同 (实测 321 条有
# 结算结果的决策):
#     WTI  +5.42pp / XAU +2.93pp / BTC +1.64pp / ETH **-15.31pp**
# 也就是说: 闸门在 ETH 上拦得对 (信号系统性反向), 却在 WTI/XAU/BTC 上
# **拦掉了有正增量的好信号**。整体数字把分组差异抹平了。
#
# 这张表把「某个资产×方向上的信号历史上准不准 / 比随机好多少」落库, 供:
#   · 闸门运行时按资产调整门槛 (decision_guard._direction_scores)
#   · 前端/研究端展示「这条信号的历史胜率」
#   · 校准脚本 (calibrate_gate.py) 定期刷新
#
# 诚实边界: 命中率与基础概率都是**实际结算结果**算出来的, 不是估计值;
# 样本不足 (n < MIN_SAMPLES) 的分组**不写表** —— 宁可没有, 不用小样本硬调。

_GATE_CALIBRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gcalib_lift ON gate_calibration(lift_pp DESC)",
]

# ── v321.4 P0: 闸门评估留痕 (需求 #1「闸门到底在拦什么」) ──────────────────
# ─────────────────────────────────────────────────────────────────────────────
# 问题: trade_decisions 只记"最终落地的动作", **被闸门拦下的候选与 HOLD 从不落库**
#   → 分数门槛 (buy_min/sell_min) 到底拒过谁, 数据层无从统计。
# 实测 (3243 条): ENTRY 51 / EXIT 5 / REJECT 3187, 而 REJECT 里 **分数型拒绝 0 条**
#   全是规则型 (asset_BTC 1451 / high_strength 733 / R8 同批次 813)。
#   "分数闸门没有增量"这个结论因此**无法证伪也无法证实** —— 不是没拒, 是没记。
#
# 处置: 每个被评估过的 AI 决策都写一行 (含未下单/被拦的), 保留当时读数与拒绝码。
# 边界: 这是**只读观测层**, 不参与下单判定; 写失败不得阻断交易链 (调用方 try/except)。

_GATE_EVALUATIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gate_eval_ts ON gate_evaluations(evaluated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_gate_eval_code ON gate_evaluations(reject_code, evaluated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_gate_eval_asset_ts ON gate_evaluations(asset, evaluated_at DESC)",
]


# 已建过表的连接（按连接对象记，不按进程记）
# v2026.11.17 修：v437 的「每进程只做一次 DDL」把 ``_GATE_EVAL_READY`` 做成进程级
#   全局标志，于是**同一进程里另开一个库**（测试的 :memory:、临时库、attached 库）
#   永远建不出表 → 观测写入全部 OperationalError: no such table。
#   DDL 的成本在「同一连接反复 CREATE」上，所以按连接去重就够了。
_GATE_EVAL_READY_CONNS = None  # 惰性初始化为 weakref.WeakSet，避免 import 期依赖 weakref


def _gate_eval_ready_conns():
    """返回「已建过表」的连接集合（惰性初始化，避免模块导入期依赖 weakref）。"""
    global _GATE_EVAL_READY_CONNS
    if _GATE_EVAL_READY_CONNS is None:
        import weakref
        _GATE_EVAL_READY_CONNS = weakref.WeakSet()
    return _GATE_EVAL_READY_CONNS


def ensure_gate_evaluations(conn: sqlite3.Connection) -> None:
    """幂等建表 (不打印)。供观测写入前调用, 不参与业务判定。

    v437 P0-0c：每条连接只做一次 DDL。
    事故（2026-11-17 21:36 长事务哨兵）：quick_sim 每轮都会调本函数，
    里面的 CREATE TABLE / CREATE INDEX 是**写操作**，会把整轮漏斗统计
    的写事务从 CREATE 开始计时（实测 6.9s，其中大部分是在等写锁）。
    DDL 只需一次，之后的调用直接返回。
    """
    try:
        seen = _gate_eval_ready_conns()
        if conn in seen:
            return
    except TypeError:  # pragma: no cover — 极端情况下连接不支持弱引用
        seen = None
    conn.execute(_CREATE_GATE_EVALUATIONS)
    for _idx_sql in _GATE_EVALUATIONS_INDEXES:
        conn.execute(_idx_sql)
    if seen is not None:
        try:
            seen.add(conn)
        except TypeError:  # pragma: no cover
            pass

# v321.3 P1: macro_snapshots 的 metric_key 索引
# ─────────────────────────────────────────────────────────────────────────────
# 实测: macro_snapshots 有 579448 行, 但**没有 metric_key 索引** →
# 每查一个指标 (WHERE metric_key=? AND status='ok' ORDER BY ts DESC) 都是
# **全表扫**。后果: market_regime 的三个新检测器 (state_fund 11.56s /
# leverage_overheat 2.35s) 把 /v291/market-regime 拖到 20s 硬超时
# → G17 判成「基本面面板实时为空」。
# 加了索引后单次查询 <10ms, 端点回到秒级。
_MACRO_SNAPSHOTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_metric_ts "
    "ON macro_snapshots(metric_key, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_macro_snapshots_metric_status_ts "
    "ON macro_snapshots(metric_key, status, ts DESC)",
]

# v321.3 P2: 行情/技术信号的查询索引
# ─────────────────────────────────────────────────────────────────────────────
# 归因端点 /v291/event-move-attribution 要给**每条**异动补量能/形态/盘口/资金流,
# 每条要查 kline_bars (44463 行) 与 tech_signals —— 没有索引时每次都是**全表扫**,
# limit=100 就是 200 次全表扫 → 端点超过 30s → G14 判成「无数据」。
# (这和 macro_snapshots 索引是同一类问题: 大表 + 按业务键查询 + 缺索引)
_KLINE_CONTEXT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_kline_bars_symbol_tf_ts "
    "ON kline_bars(symbol, timeframe, ts)",
    "CREATE INDEX IF NOT EXISTS idx_tech_signals_asset_ts "
    "ON tech_signals(asset, ts)",
]

# ── v321.3 P2: 决策引用归档 (需求 #5「串联互通」的防复发底座) ────────────────
# ─────────────────────────────────────────────────────────────────────────────
# 问题: 「重置交易历史」会删 ai_decisions / raw_news / news_events, 而
# trade_decisions 的两个指针 (news_id / ai_decision_id) 还指着它们 →
# 变成死指针。实测那次重置留下 1998 + 1783 条孤儿, 而那批上游数据
# (最早 2500 条决策的 news/move/ai_decisions) **被物理删除, 不可恢复**。
#
# 这里加一张**归档表**, 在重置前把「决策 + 它的引用 + 它的语义」存下来:
#   · 重置**不删**这张表 → 决策历史不随重置消失
#   · 归档里保留 (asset, decision_ts, action, entry_price, 结算结果) ——
#     即使引用断了, 决策本身仍可用于复盘与校准 (这是真数据, 不该被抹掉)
#   · 保留原始 news_id / ai_decision_id —— 等上游重建后可自动对齐回去

_DECISION_REF_ARCHIVE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dra_batch ON decision_ref_archive(batch)",
    "CREATE INDEX IF NOT EXISTS idx_dra_asset_ts ON decision_ref_archive(asset, decision_ts)",
]


# v392：最近几版新增的列清单（"标记新鲜就跳过迁移"会让新列永远建不出来，见 CHANGELOG）
_LATEST_COLUMNS: List[Tuple[str, str, str]] = [
    ("ai_decisions", "quarantined_at", "TEXT DEFAULT ''"),
    ("ai_decisions", "quarantine_reason", "TEXT DEFAULT ''"),
    ("ai_decisions", "insight_hit_source", "TEXT NOT NULL DEFAULT ''"),
    # v416: 影子反思的 24h 二次判决（新闻驱动的行情常越过 4h 结算窗口才走出来，
    # 只用 4h 会把"慢半拍但方向对"判成错）。只加列、不覆盖 4h 结论。
    ("shadow_reflections", "verdict_24h", "TEXT NOT NULL DEFAULT ''"),
    ("shadow_reflections", "effective_24h", "INTEGER"),
    ("shadow_reflections", "price_24h", "REAL"),
    ("shadow_reflections", "move_pct_24h", "REAL"),
    ("shadow_reflections", "settled_24h_at", "TEXT NOT NULL DEFAULT ''"),
    ("decision_actions", "scope", "TEXT DEFAULT 'asset'"),
    ("decision_actions", "event_type", "TEXT DEFAULT ''"),
    # MiniMax 前缀缓存命中条数（v345.12）；失败调用响应体/异常摘要（截断）：
    # 区分 402 套餐耗尽 vs 401 key 失效 vs 429 限速（v483）。
    ("ai_call_stats", "cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("ai_call_stats", "error_body", "TEXT NOT NULL DEFAULT ''"),
]


def ensure_latest_columns(conn: sqlite3.Connection) -> List[str]:
    """只读检查 + 仅补缺失列，返回这次真的补了哪些（最多几毫秒）。

    背景：API 启动时若 `startup_marker.marker_fresh()` 为真，整段 migrate 会被跳过，
    于是"代码里新加的列"在线上库永远不存在，直到有人手工跑迁移。这里把最近几版
    的列单独兜底，既避免重跑整段迁移，也不会把新列漏掉。
    """
    added: List[str] = []
    for table, col, ddl in _LATEST_COLUMNS:
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col in cols:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            added.append(f"{table}.{col}")
        except Exception as exc:
            print(f"[DB] ensure_latest_columns 跳过 {table}.{col}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
    if added:
        try:
            conn.commit()
        except Exception:
            pass
    return added


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


def _upgrade_event_time_candidates_source_check(conn: sqlite3.Connection) -> None:
    """v318：放宽 event_time_candidates.source 的 CHECK 约束。

    背景（线上实测）：CHECK 只允许
    (platform_published_at, ai_inferred, cross_net, human_annotated, other)，
    而 ai_worker 实际写入 'ai_worker_review'、回填脚本写 'backfill_v181'
    → INSERT 触发 CHECK 失败 → event_atom 把异常吞掉返回 -1 →
    **事件时间候选表长期只有 2 行**，事件时间溯源形同虚设。

    SQLite 的 ALTER TABLE 不支持修改 CHECK，只能重建表：
    重命名 → 建新表 → 拷数据 → 删旧表 → 重建索引。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='event_time_candidates'"
        ).fetchone()
        if not row:
            return
        sql = str(row[0] or "")
        if "'ai_worker_review'" in sql:
            return  # 已是新约束，无需升级
        print("[DB] v318: 放宽 event_time_candidates.source CHECK "
              "→ 允许 ai_worker_review/llm_fallback/backfill_v181", flush=True)
        conn.execute("ALTER TABLE event_time_candidates RENAME TO event_time_candidates__legacy")
        conn.execute(_CREATE_EVENT_TIME_CANDIDATES)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(event_time_candidates__legacy)")]
        col_list = ", ".join(cols)
        conn.execute(
            f"INSERT INTO event_time_candidates ({col_list}) "
            f"SELECT {col_list} FROM event_time_candidates__legacy"
        )
        conn.execute("DROP TABLE event_time_candidates__legacy")
        # 旧索引随旧表一起被删除，这里补回来（幂等）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_time_cand_news "
            "ON event_time_candidates(raw_news_id);"
        )
    except Exception as exc:
        print(f"[DB] v318: upgrade event_time_candidates CHECK 失败: {exc}", flush=True)


def _upgrade_raw_news_status_check(conn: sqlite3.Connection) -> None:
    """v322: 放宽 raw_news.status 的 CHECK 约束 → 允许 'FILTERED'。

    背景（2026-11-07 生产实测）
    ----------------------
    raw_news.status 的 CHECK 只允许 ('PENDING','PROCESSING','DONE','FAILED')，
    而 ai_worker._expire_stale_news / scripts/cleanup_noise_pending.py
    把「超龄 / 永远不会被分析」的僵尸行标成 'FILTERED'
    → **每一条 UPDATE 都被数据库拒绝**（CHECK constraint failed）
    → 异常又被 except 吞掉 → 库里一条 FILTERED 都没有
    → 前端「待分析」计数永远不降（实测 4850 条 PENDING 100% 满足归档条件）。

    SQLite 的 ALTER TABLE 不支持修改 CHECK，只能重建表：
    重命名 → 建新表 → 拷数据 → 删旧表 → 重建索引。

    ⚠️ 两个必须处理的陷阱（不处理就是数据事故）
    -------------------------------------------
    1. ai_decisions 有 `FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE`
       且 db.get_connection() 里 `PRAGMA foreign_keys=ON`
       → DROP 旧表会触发**隐式 DELETE FROM + 级联**，把几千条决策记录删光！
       → 必须先 `PRAGMA foreign_keys=OFF`（该 PRAGMA 在事务内是空操作 → 先 commit）。
    2. SQLite ≥ 3.25 默认 legacy_alter_table=OFF
       → ALTER TABLE RENAME 会**改写其它表里的外键引用**（指向临时表名）
       → 必须临时 `PRAGMA legacy_alter_table=ON`，让引用继续指向 raw_news。

    新表 DDL **从线上表自身的 SQL 文本派生**（只替换 CHECK 子句），
    不用 _CREATE_RAW_NEWS —— 因为线上表还有一堆 ALTER TABLE 补出来的列
    （processing_at / net_origin_info / cross_net_* / retry_count / url …），
    用固定 DDL 重建会把它们全丢掉。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_news'"
        ).fetchone()
        if not row:
            return
        live_sql = str(row[0] or "")
        if not live_sql:
            return
        if "'FILTERED'" in live_sql:
            return  # 已是新约束，幂等返回
        m = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", live_sql, re.I)
        if not m:
            print("[DB] v322: raw_news.status 未找到 CHECK 子句，跳过放宽", flush=True)
            return
        items = m.group(1).strip()
        new_sql = (live_sql[:m.start()]
                   + "CHECK (status IN (" + items + ", 'FILTERED'))"
                   + live_sql[m.end():])
        print("[DB] v322: 放宽 raw_news.status CHECK → 允许 'FILTERED'", flush=True)

        # 索引 DDL 备份（重建表后索引会随旧表一起消失）
        idx_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='raw_news' AND sql IS NOT NULL"
        ).fetchall()

        # PRAGMA foreign_keys 在事务内是空操作 → 先提交再关外键
        conn.commit()
        fk_was = int(conn.execute("PRAGMA foreign_keys").fetchone()[0] or 0)
        legacy_was = int(conn.execute("PRAGMA legacy_alter_table").fetchone()[0] or 0)
        if fk_was:
            conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        try:
            conn.execute("ALTER TABLE raw_news RENAME TO raw_news__legacy_v322")
            conn.execute(new_sql)
            cols = [r[1] for r in
                    conn.execute("PRAGMA table_info(raw_news__legacy_v322)")]
            col_list = ", ".join(cols)
            conn.execute(
                f"INSERT INTO raw_news ({col_list}) "
                f"SELECT {col_list} FROM raw_news__legacy_v322"
            )
            conn.execute("DROP TABLE raw_news__legacy_v322")
            for _iname, isql in idx_rows:
                try:
                    conn.execute(isql)
                except Exception as _iexc:
                    print(f"[DB] v322: 重建索引 {_iname} 失败: {_iexc}", flush=True)
            conn.commit()
        finally:
            try:
                conn.execute(f"PRAGMA legacy_alter_table={legacy_was}")
                if fk_was:
                    conn.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
        print("[DB] v322: raw_news.status CHECK 已放宽（允许 FILTERED）", flush=True)
    except Exception as exc:
        print(f"[DB] v322: 放宽 raw_news.status CHECK 失败: "
              f"{type(exc).__name__}: {exc}", flush=True)
        try:
            conn.rollback()
        except Exception:
            pass


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
        # v409.5: 单笔仓位上限列（缺列时让 migrate 跑起来去补，_ensure_columns 负责建）
        if "max_position_pct" not in _cols("paper_trading_settings"):
            return False
        if "news_export_settings" not in _cols("paper_trading_settings"):
            return False
        if "base_version" not in _cols("ai_main_prompts"):
            return False
        if not {"core_impact", "reasons_json"}.issubset(_cols("attribution_reviews")):
            return False
        # v309 P0-1: 三维度归因列必须齐全, 否则 /api/v237/missed-signals/by-dimension
        # 与 /api/v291/reflections/summary 读不到列会静默返回空 (前端三维度卡片恒 n=0),
        # 且 schema_ready 返 True 会让启动路径**跳过 migrate** → 老库永远补不上列。
        if not {"dimension", "subcategory", "hit", "actual_outcome_pct",
                "confidence_at_time", "reflection_text", "settled",
                # v404: 结算口径可追溯 + 反思有效性占位 (缺列必须走 migrate)
                "settle_source", "reflection_effective"}.issubset(
                    _cols("trade_reflections_v2")):
            return False
        return True
    except sqlite3.Error:
        return False




def migrate(conn: sqlite3.Connection) -> None:
    """Apply the full schema to `conn`. Safe to call on every startup."""
    conn.execute(_CREATE_RAW_NEWS)
    # v322: 线上老库的 raw_news.status CHECK 不允许 'FILTERED'
    # → 超龄归档的 UPDATE 全被拒 → 「待分析」永不下降。这里重建表放宽约束。
    _upgrade_raw_news_status_check(conn)
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
    # v315 P0: 方向判定列 (forward_move_pct + suggested_action → WIN/LOSS/BREAKEVEN)。
    # 没有这两列时 decision_truth.collect_truth_verdicts 会因缺列返回空,
    # source_stats 的"真实涨跌"回填通道就永远是空的。
    _ensure_columns(conn, "ai_decisions", [
        ("truth_verdict", "TEXT DEFAULT ''"),
        ("truth_verdict_ts", "INTEGER DEFAULT 0"),
    ])
    conn.execute(_CREATE_IDX_SWH_DECISION)
    conn.execute(_CREATE_IDX_SWH_COMPUTED)
    conn.execute(_CREATE_IDX_SS_SOURCE)
    # v383: 处置账（人工静默/忽略/反手）
    conn.execute(_CREATE_DECISION_ACTIONS)
    conn.execute(_CREATE_IDX_DEC_ACTIONS_DECISION)
    conn.execute(_CREATE_IDX_DEC_ACTIONS_ASSET)
    # v383: 人工忽略需要把该笔决策移出统计口径。线上老库已有这两列（脚本加的），
    # 但 db.py 从未声明过 —— 全新库会因缺列直接报错，这里补成正式迁移。
    _ensure_columns(conn, "ai_decisions", [
        ("quarantined_at", "TEXT DEFAULT ''"),
        ("quarantine_reason", "TEXT DEFAULT ''"),
        # v453: 代码指纹（git 短 hash + 关键参数哈希）。有了它，"这条决策属于哪一代
        # 算法"才可查，换代时不必再靠删库来保持口径干净。
        ("code_fingerprint", "TEXT DEFAULT ''"),
    ])
    # v390: 处置作用域（整品种 / 单事件类型）。老库补列，全新库由建表语句带出。
    _ensure_columns(conn, "decision_actions", [
        ("scope", "TEXT DEFAULT 'asset'"),
        ("event_type", "TEXT DEFAULT ''"),
    ])
    conn.execute(_CREATE_STRATEGIES)
    conn.execute(_CREATE_STRATEGY_VERSIONS)
    conn.execute(_CREATE_PAPER_TRADING_SETTINGS)
    conn.execute(_CREATE_PAPER_TRADING_RUNS)
    conn.execute(_CREATE_QUICK_SIM_TRADES)
    # v477: 成交行也带代码指纹 —— 决策行有 code_fingerprint，但成交/打点行没有，
    # 换代后只能按"时间"猜哪笔属于哪一代。这里补列（老库 ALTER，新库建表已含）。
    _ensure_columns(conn, "quick_sim_trades", [
        ("code_fingerprint", "TEXT DEFAULT ''"),
    ])
    _ensure_columns(conn, "trade_decisions", [
        ("code_fingerprint", "TEXT DEFAULT ''"),
    ])
    conn.execute(_CREATE_HERMES_OBSERVATIONS)
    conn.execute(_CREATE_HERMES_SKILLS)
    conn.execute(_CREATE_MACRO_SNAPSHOTS)
    # v405: 财报数据层（三表 + 财报日历）。增量迁移，不动既有表语义。
    conn.execute(_CREATE_EQUITY_FUNDAMENTALS)
    conn.execute(_CREATE_IDX_EQ_FUND)
    conn.execute(_CREATE_EQUITY_EARNINGS)
    conn.execute(_CREATE_IDX_EQ_EARNINGS)
    # v406: A股/港股财务 + 估值快照
    conn.execute(_CREATE_CN_EQUITY_FUNDAMENTALS)
    conn.execute(_CREATE_IDX_CN_EQ_FUND)
    conn.execute(_CREATE_CN_VALUATION_SNAPSHOTS)
    conn.execute(_CREATE_IDX_CN_VAL)
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
    _upgrade_event_time_candidates_source_check(conn)   # v318: 放宽 source CHECK
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
    # v342: 降级标记 —— "没分析"和"判断为观望"必须是两件事。
    # 实测 289/291 条 HOLD 里绝大多数是降级兜底，混在一起后：
    #   · 胜率/方向准确率统计被"假 HOLD"拖平
    #   · 前端看起来像"AI 一直在观望"，实际是"AI 根本没跑完"
    # 只加标记、不改任何判定与写入流程；统计侧用 analysis_degraded=0 过滤。
    _ensure_columns(conn, "ai_decisions", [
        ("analysis_degraded", "INTEGER DEFAULT 0"),
        ("analysis_degraded_reason", "TEXT"),
    ])
    _ensure_columns(conn, "strategies", _STRATEGIES_COLUMNS)
    _ensure_columns(conn, "paper_trading_settings", _PAPER_SETTINGS_COLUMNS)
    _ensure_columns(conn, "causal_chains", _CAUSAL_CHAINS_COLUMNS)
    _ensure_columns(conn, "ai_main_prompts", _AI_MAIN_PROMPTS_COLUMNS)
    _ensure_columns(conn, "quick_sim_trades", _QUICK_SIM_TRADES_COLUMNS)
    # v314: 权重历史新增 impact_mult (预期市场影响乘子)。老库缺列 → 幂等补齐,
    # DEFAULT 1.0 = 中性, 不改变历史行的语义。
    _ensure_columns(conn, "signal_weight_history", [
        ("impact_mult", "REAL NOT NULL DEFAULT 1.0"),
    ])
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
    # v482: 反思行记代码代次 —— 换代后旧教训可按指纹失效
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V482_COLUMNS)
    try:
        for _sql in _TRADE_REFLECTIONS_V2_V482_INDEXES:
            conn.execute(_sql)
    except Exception:
        pass
    # v190: trade_reflections_v2 加 ontology_signal 拼合列 + 索引
    _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V190_COLUMNS)
    # sim_equity_snapshots 只在 sim_equity.py 里建表，migrate 没建 →
    # 新建库上 fake_data 巡检更新该表时直接报错。补一次幂等建表。
    try:
        import sim_equity as _sim_eq
        conn.execute(_sim_eq._CREATE)
    except Exception as _exc:
        print(f"[DB] sim_equity_snapshots 建表失败（不阻断 migrate）: {_exc}", flush=True)

    # v406: shadow_ledger 原来自家 ensure_table 建，migrate 从没建过 ——
    # 新建库缺这张表，影子账本一跑就 no such table。
    try:
        import shadow_ledger as _shl
        _shl.ensure_table(conn)
    except Exception as _exc:
        print(f"[DB] shadow_ledger 建表失败（不阻断 migrate）: {_exc}", flush=True)

    # tech_signals 由独立脚本 migrate_tech_signals.py 建表，但 migrate() 从来没调它
    # → 新建库上 tech_signals 不存在，紧接着的 kline/tech 索引 CREATE 直接失败
    # （日志里那句"kline/tech 索引建立失败: no such table: main.tech_signals"），
    # 异动识别相关查询在新建库上全部不可用。这里补一次幂等调用。
    try:
        import migrate_tech_signals as _mts
        _mts.migrate(conn)
    except Exception as _exc:
        print(f"[DB] tech_signals 建表失败（不阻断 migrate）: {_exc}", flush=True)

    # ── 生产库 vs 建表语句的列差异（208 列 / 22 表）──────────────────────
    # schema 是一路 ALTER 长出来的：生产库里有的列，`CREATE TABLE IF NOT EXISTS`
    # 里没有。后果是新建库（单测/换机/灾备重建）"建表成功"，但 meta-loop 聚合统计、
    # fake_data 假数据巡检、v322 超龄归档一跑就 `no such column` ——
    # 生产上不报错、只有新建库才炸，是最难发现的一类漂移。
    # 数据来源：生产库 `pragma table_info` 与本文件 CREATE 语句的差集，类型取生产库原类型。
    _ensure_columns(conn, "abnormal_attributions", [
        ("is_actionable", "INTEGER"),
        ("action_text", "TEXT"),
        ("action_source", "TEXT"),
    ])
    _ensure_columns(conn, "ai_decisions", [
        ("strategy_id", "INTEGER"),
        ("strategy_version_id", "INTEGER"),
        ("holding_horizon_minutes", "INTEGER"),
        ("peer_reviews", "TEXT"),
        ("ts", "INTEGER"),
        ("low_impact", "INTEGER"),
        ("structured_input", "TEXT"),
        ("structured_check", "TEXT"),
        ("event_type_code", "TEXT"),
        ("snapshot_proximity", "TEXT"),
        ("prev_decision_id", "INTEGER"),
        ("prev_action_same_asset", "TEXT"),
        ("prev_score_same_asset", "REAL"),
        ("regime_at_decision", "TEXT"),
        ("bias_after_reflection", "TEXT"),
        ("event_atom_ids", "TEXT"),
        ("gate_chain", "TEXT"),
        ("original_action", "TEXT"),
        ("ungated_action", "TEXT"),
        ("structured_summary", "TEXT"),
        ("settle_flags", "TEXT"),
        ("persona_weight", "REAL"),
        ("persona_match_id", "INTEGER"),
        ("reflection_depth_score", "REAL"),
        ("actionable_suggestion", "TEXT"),
        ("missed_signal", "INTEGER"),
        ("run_id", "TEXT"),
        ("applied_setting_changes", "TEXT"),
        ("trade_uuid", "TEXT"),
        ("quarantined_at", "TEXT"),
        ("quarantine_reason", "TEXT"),
        ("field_sources", "TEXT"),
        ("missing_fields", "TEXT"),
        ("completeness_score", "REAL"),
        ("llm_field_count", "INTEGER"),
        ("rule_field_count", "INTEGER"),
        ("business_kb_field_count", "INTEGER"),
        ("historical_field_count", "INTEGER"),
        ("historical_ref_ids", "TEXT"),
        ("llm_retry_count", "INTEGER"),
        ("signal_weight", "REAL"),
        ("weight_tier", "TEXT"),
        ("weight_explain", "TEXT"),
        ("weight_updated_at", "INTEGER"),
        ("cold_signal", "INTEGER"),
        ("source_stats_counted_ts", "INTEGER"),
        ("forward_move_pct", "REAL"),
        ("truth_verdict", "TEXT"),
        ("truth_verdict_ts", "INTEGER"),
        ("asset_route", "TEXT"),
        ("analysis_degraded", "INTEGER"),
        ("analysis_degraded_reason", "TEXT"),
        ("hard_data_status", "TEXT"),
        ("decision_kind", "TEXT"),
    ])
    _ensure_columns(conn, "asset_driver_edges", [
        ("weight_raw", "REAL"),
    ])
    _ensure_columns(conn, "attribution_reviews", [
        ("failing_sources", "TEXT"),
    ])
    _ensure_columns(conn, "causal_chains", [
        ("chain_id", "TEXT"),
        ("asset", "TEXT"),
        ("trigger_pattern", "TEXT"),
        ("chain_steps", "TEXT"),
        ("confidence", "REAL"),
        ("version", "INTEGER"),
        ("hit_count", "INTEGER"),
        ("miss_count", "INTEGER"),
        ("source", "TEXT"),
        ("active", "INTEGER"),
        ("last_verified_ts", "INTEGER"),
        ("expected_direction", "TEXT"),
        ("magnitude_lo", "REAL"),
        ("magnitude_hi", "REAL"),
        ("trigger_keywords", "TEXT"),
    ])
    _ensure_columns(conn, "decision_errors", [
        ("decision_id", "INTEGER"),
        ("error_code", "TEXT"),
        ("origin", "TEXT"),
        ("severity", "INTEGER"),
        ("detail", "TEXT"),
        ("evidence_refs", "TEXT"),
        ("created_at", "TEXT"),
        ("trade_uuid", "TEXT"),
        ("weight", "INTEGER"),
    ])
    _ensure_columns(conn, "decision_layer_signals", [
        ("run_id", "TEXT"),
        ("llm_count", "INTEGER"),
        ("rule_count", "INTEGER"),
        ("business_kb_count", "INTEGER"),
        ("historical_count", "INTEGER"),
    ])
    _ensure_columns(conn, "decision_reflection", [
        ("layer_breakdown", "TEXT"),
        ("sub_signals", "TEXT"),
        ("reasoning_chain", "TEXT"),
        ("error_root_cause", "TEXT"),
        ("experience_applied", "TEXT"),
        ("depth_score", "INTEGER"),
        ("actionable_suggestion", "TEXT"),
    ])
    _ensure_columns(conn, "event_atom", [
        ("curve_snapshot", "TEXT"),
        ("event_time_utc_epoch", "INTEGER"),
        ("event_time_utc_iso", "TEXT"),
        ("event_time_display_tz", "TEXT"),
        ("fallback_used", "INTEGER"),
    ])
    _ensure_columns(conn, "event_impact_stat", [
        ("asset", "TEXT"),
        ("window_label", "TEXT"),
        ("samples", "INTEGER"),
        ("real_filled", "INTEGER"),
        ("avg_impact_pct", "REAL"),
        ("avg_abs_impact_pct", "REAL"),
        ("direction_hit_rate", "REAL"),
    ])
    _ensure_columns(conn, "event_kline_impact", [
        ("coverage_sec", "INTEGER"),
        ("pre_gap_sec", "INTEGER"),
    ])
    _ensure_columns(conn, "experience_applications", [
        ("run_id", "TEXT"),
    ])
    _ensure_columns(conn, "experience_entries", [
        ("control_apply_count", "INTEGER"),
        ("applied_win_rate", "REAL"),
        ("control_win_rate", "REAL"),
        ("lift", "REAL"),
        ("verified_count", "INTEGER"),
    ])
    _ensure_columns(conn, "news_events", [
        ("headline", "TEXT"),
        ("body", "TEXT"),
        ("assets", "TEXT"),
        ("fetched_at", "INTEGER"),
        ("decision_space", "TEXT"),
        ("ai_decision_id", "INTEGER"),
        ("trade_decision_id", "INTEGER"),
    ])
    _ensure_columns(conn, "paper_trading_settings", [
        ("quick_sim_reset_at", "TEXT"),
        # v409.5: 单笔仓位上限（占权益比例），NULL = 不设限（保持既有行为）
        ("max_position_pct", "REAL"),
    ])
    _ensure_columns(conn, "quick_sim_trades", [
        ("trade_uuid", "TEXT"),
        ("gross_pnl_pct", "REAL"),
    ])
    _ensure_columns(conn, "raw_news", [
        ("processing_at", "TEXT"),
        ("net_origin_info", "TEXT"),
        ("cross_net_first_seen_ts", "TEXT"),
        ("cross_net_source", "TEXT"),
        ("retry_count", "INTEGER"),
        ("url", "TEXT"),
        ("gate_unavailable", "INTEGER"),
    ])
    _ensure_columns(conn, "review_insights", [
        ("manual_deactivated", "INTEGER"),
        ("verification_score", "REAL"),
        ("verification_verdict", "TEXT"),
        ("verification_method", "TEXT"),
        ("verification_sample_n", "INTEGER"),
        ("verification_kline_n", "INTEGER"),
        ("verified_at", "TEXT"),
        ("insight_hypothesis", "TEXT"),
        ("insight_param_key", "TEXT"),
        ("insight_param_bucket", "TEXT"),
        ("insight_direction", "INTEGER"),
        ("insight_window_h", "INTEGER"),
        ("observe_only", "INTEGER"),
        ("observe_reason", "TEXT"),
        ("insight_classified_at", "TEXT"),
    ])
    _ensure_columns(conn, "signal_weight_history", [
        ("decision_space", "TEXT"),
        ("ai_decision_id", "INTEGER"),
        ("trade_decision_id", "INTEGER"),
    ])
    _ensure_columns(conn, "structured_reflections", [
        ("trade_uuid", "TEXT"),
    ])
    _ensure_columns(conn, "trade_reflections", [
        ("layer", "TEXT"),
        ("sublayer", "TEXT"),
        ("verdict_uncertain", "INTEGER"),
        ("improvement_suggestion", "TEXT"),
        ("suggestion_param_key", "TEXT"),
        ("suggestion_direction", "TEXT"),
        ("suggestion_magnitude", "REAL"),
        ("suggestion_confidence", "REAL"),
        ("suggestion_rationale", "TEXT"),
    ])
    _ensure_columns(conn, "trade_reflections_v2", [
        ("verification_method", "TEXT"),
        ("layer", "TEXT"),
        ("sub_layer", "TEXT"),
        ("signal_name", "TEXT"),
        ("verification_window", "TEXT"),
        ("verified_at", "TEXT"),
        ("run_id", "TEXT"),
        ("ontology_signal", "TEXT"),
        ("trade_uuid", "TEXT"),
        ("quarantined_at", "TEXT"),
        ("quarantine_reason", "TEXT"),
        ("mismatch_detail", "TEXT"),
        ("source_breakdown", "TEXT"),
        ("completeness_at_reflection", "REAL"),
        ("skipped_low_completeness", "INTEGER"),
        ("kline_at_decision", "TEXT"),
        ("kline_pattern", "TEXT"),
        ("cross_asset_sync_score", "REAL"),
        ("front_run_score", "REAL"),
        ("luck_vs_signal_score", "REAL"),
        ("market_regime_detail", "TEXT"),
        ("deep_root_cause", "TEXT"),
        ("deep_lesson", "TEXT"),
        ("secondary_reflection_id", "INTEGER"),
        ("secondary_reflection_at", "TEXT"),
        ("enriched_at", "TEXT"),
        ("enrich_status", "TEXT"),
        ("enrich_error", "TEXT"),
        ("core_chain_audit", "TEXT"),
        ("core_counter_evidence", "TEXT"),
        ("core_truth_score", "REAL"),
        ("core_pattern_alert", "TEXT"),
        ("core_judgment_breakdown", "TEXT"),
        ("core_meta_audit", "TEXT"),
        ("core_overall_grade", "TEXT"),
        ("core_enriched_at", "TEXT"),
        ("core_enrich_status", "TEXT"),
        ("core_enrich_error", "TEXT"),
        ("evidence_strength_score", "REAL"),
        ("reflection_self_score", "REAL"),
        ("is_sibling", "INTEGER"),
        ("logic_integrity_score", "REAL"),
        ("logic_findings_json", "TEXT"),
        ("logic_checked_at", "TEXT"),
        ("ai_decision_id", "INTEGER"),
        ("subcategory_evidence", "TEXT"),
        ("settle_source", "TEXT"),
        ("reflection_effective", "INTEGER"),
    ])

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
    # v309 P0-1: 三维度 (A/B/C) 归因列 — bridge_server 的 by-dimension /
    # reflections/summary 端点一直在读它们, 但 schema 从未定义过。
    # 幂等 ALTER: 老库自动补列, 新库由上面的 CREATE TABLE 建全。
    try:
        _ensure_columns(conn, "trade_reflections_v2", _TRADE_REFLECTIONS_V2_V309_DIMENSION_COLUMNS)
    except Exception as _e:
        print(f"[MIGRATE] trv2 v309 dimension cols failed: {_e}", flush=True)
    # v302+ reconcile worker: 跟踪 kg_improvements.approved 行的实际应用状态
    _ensure_columns(conn, "kg_improvements", [
        ("applied_at",  "TEXT"),
        ("apply_error", "TEXT NOT NULL DEFAULT ''"),
    ])
    # v404 (反思评审 2026-11-16): 结算口径可追溯 + 反思有效性占位 + 错误码台账。
    # 背景: 反思表里唯一的 hit=1 是 v396 认定的污染价 (86.42) 残留 —— 结算器修了,
    # 反思表没有回填通道; 且 hit 的语义是"交易胜负", 一直被当成"反思有效性"读。
    # 见 docs/archive/反思机制评审-2026-11-16.md (R3-b / §5.2)。
    _ensure_columns(conn, "trade_reflections_v2", [
        # 该行用的哪个价结算: tick / 1m / daily_fallback / ''(未回填, 不可信)
        ("settle_source", "TEXT NOT NULL DEFAULT ''"),
        # 反思是否真的"有用": 由带教训 vs 不带教训的对照实验填; NULL = 未验证, 不得参与统计
        ("reflection_effective", "INTEGER"),
    ])
    # v482 (反思三修 2026-11-19): 反思的"判断对不对"必须由 4h/24h 真值回答，
    # 不能再用 20~40 分钟纸上单的 pnl 代替（噪声 0.1%~0.3% < 往返成本 0.16%~0.30%）。
    # 列由 reflection_horizon_truth.py 从 shadow_reflections 盖章；空 = 判不了。
    _ensure_columns(conn, "trade_reflections_v2", [
        ("truth_4h_verdict",   "TEXT NOT NULL DEFAULT ''"),
        ("truth_4h_net_pct",   "REAL"),
        ("truth_24h_verdict",  "TEXT NOT NULL DEFAULT ''"),
        ("truth_24h_net_pct",  "REAL"),
        ("truth_stamped_at",   "TEXT NOT NULL DEFAULT ''"),
    ])
    # v482: 漏检信号记账从反思表**分出来**。反思表只留"有结算真值"的成交反思；
    # HOLD 漏检没有 pnl，混在同一张表里会把 90%+ 的"反思条数"变成模板。
    conn.execute(_CREATE_MISSED_SIGNAL_REFLECTIONS)
    for _idx_sql in _MISSED_SIGNAL_REFLECTIONS_INDEXES:
        conn.execute(_idx_sql)
    conn.execute(_CREATE_ERROR_CODE_LEDGER)
    for _idx_sql in _ERROR_CODE_LEDGER_INDEXES:
        try:
            conn.execute(_idx_sql)
        except Exception as _e:
            print(f"[MIGRATE] error_code_ledger idx failed: {_e}", flush=True)
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
    # v413: 反思来源/证据列（幂等；历史行 origin 留空由 migrate_reflection_origin.py 回填）
    for _tname, _cols in _REFLECTION_V413_COLUMNS:
        try:
            _ensure_columns(conn, _tname, _cols)
        except Exception as _e:
            print(f"[MIGRATE] {_tname} origin/evidence col failed: {_e}", flush=True)
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
    # v483: R5 扩链动作审计表（此前只在代码里 INSERT，表根本不存在）
    conn.execute(_CREATE_AUDIT_LOG)
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
    # v316 Crowd Pulse：人群画像 / 权威度 / 月榜（真实抓取，原始条目可回溯）
    conn.execute(_CREATE_CROWD_SOURCE_ITEMS)
    conn.execute(_CREATE_CROWD_TOPIC_DAILY)
    conn.execute(_CREATE_CROWD_MONTHLY_RANKINGS)
    conn.execute(_CREATE_CROWD_AUDIENCE_SEGMENTS)
    conn.execute(_CREATE_CROWD_SOURCE_AUTHORITY)
    conn.execute(_CREATE_CROWD_SOURCE_RUNS)
    for _idx_sql in _CROWD_INDEXES:
        conn.execute(_idx_sql)
    # v321.3 P1: 闸门分组校准 (需求 #1)
    conn.execute(_CREATE_GATE_CALIBRATION)
    for _idx_sql in _GATE_CALIBRATION_INDEXES:
        conn.execute(_idx_sql)
    # v321.4 P0: 闸门评估留痕 (含未下单/被拦的候选, 观测层)
    conn.execute(_CREATE_GATE_EVALUATIONS)
    for _idx_sql in _GATE_EVALUATIONS_INDEXES:
        conn.execute(_idx_sql)
    # v321.3 P1: macro_snapshots 指标索引 (否则每次查指标都是全表扫)
    for _idx_sql in _MACRO_SNAPSHOTS_INDEXES:
        try:
            conn.execute(_idx_sql)
        except sqlite3.Error as _mi_exc:
            print(f"[DB] ⚠️ macro_snapshots 索引建立失败: "
                  f"{type(_mi_exc).__name__}: {str(_mi_exc)[:100]}", flush=True)
    # v321.3 P2: 决策引用归档 (重置前备份, 重置不删)
    conn.execute(_CREATE_DECISION_REF_ARCHIVE)
    for _idx_sql in _DECISION_REF_ARCHIVE_INDEXES:
        conn.execute(_idx_sql)
    # v321.3 P2: 挂链来源标注 —— 精确匹配 / 同源键 / 语义推断 必须可区分,
    # 否则下游无法判断一条「决策→新闻」链是真的还是推断出来的。
    _ensure_columns(conn, "trade_decisions", [
        ("news_link_method", "TEXT"),        # exact_ai / same_news_id / semantic / none
        ("news_link_diff_sec", "INTEGER"),   # 语义对齐的时间差(秒)
    ])
    # v342 P0: 三条写入口径统一 —— 表里缺的列补上（幂等）。
    #   1) status：decision_engine / bridge_server 早就在 INSERT 里写 'open'，
    #      但迁移从未建这一列 → 全新库上这两条路径直接 OperationalError。
    #   2) decision_source / score / confidence / structure_score / confidence_detail：
    #      此前 score/confidence 只塞进 context_json（bridge）或干脆不落（algo 路径），
    #      "同一个字段不同口径"导致胜率/闸门统计无法分层。
    #      新增列一律可空，不回填、不改老数据，读数逻辑照旧可跑。
    _ensure_columns(conn, "trade_decisions", [
        ("status", "TEXT"),
        ("decision_source", "TEXT"),
        ("score", "REAL"),
        ("confidence", "REAL"),
        ("structure_score", "REAL"),
        ("confidence_detail", "TEXT"),
    ])
    # v342: 多周期结算表（一条决策 × 5 个周期各一条）。主口径（5 天）不变，
    # 这里只另存 15m/1h/4h/1d/5d 的读数，解决"日内判断用 5 天结算"的错配。
    # 幂等建表（脚本在 multi_horizon_settle.py，单一真相源，不在这里复制 DDL）。
    try:
        import multi_horizon_settle as _mhs
        _mhs.init_schema(conn)
        # v342 第二件：多周期判定表（方向错 / 节奏错 拆分的落点）
        import horizon_reflection as _hr
        _hr.init_schema(conn)
    except Exception as _mh_exc:
        print(f"[DB] ⚠️ 多周期结算表建立失败: {type(_mh_exc).__name__}: "
              f"{str(_mh_exc)[:100]}", flush=True)
    # v321.3 P2: 行情/技术信号索引 (否则归因端点 limit=100 会超时)
    for _idx_sql in _KLINE_CONTEXT_INDEXES:
        try:
            conn.execute(_idx_sql)
        except sqlite3.Error as _ki_exc:
            print(f"[DB] ⚠️ kline/tech 索引建立失败: "
                  f"{type(_ki_exc).__name__}: {str(_ki_exc)[:100]}", flush=True)
    # 老库补列（幂等；新库建表时已含 platform）
    _ensure_columns(conn, "crowd_topic_daily",
                    [("platform", "TEXT NOT NULL DEFAULT ''")])

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
    _ensure_ai_decisions_probe_guard(conn)
    _ensure_raw_news_probe_guard(conn)
    _ensure_quick_sim_orphan_guard(conn)
    _ensure_t2_order_dedup_guard(conn)
    _ensure_ai_decisions_trade_uuid(conn)
    _normalize_published_at(conn)
    # v323 P0-2【归因三道闸】建表 + 主表加 ai_verdict 列（幂等）。
    _ensure_event_move_attribution_v323(conn)
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




def _ensure_event_move_attribution_v323(conn: sqlite3.Connection) -> None:
    """v323 P0-2【归因三道闸】建表 + 主表加 ai_verdict 列（幂等）。

    背景（2026-11-11 只读巡检实测）：
      · 173 个异动 → 498 条归因行（平均 **2.9 事件/异动**）→ 一个异动挂多条"原因"，
        统计上无法回答"到底是哪一条新闻造成的"。
      · **负延迟 25 条**（事件发生在异动**之后**）→ 把"结果"当"原因"。
      · AI 判「不成立」的行（ETH -3.31%，置信 0.94–0.97）**仍留在主表**。

    四张表：
      event_move_attribution_candidates —— 一个异动的**落选候选**（展示用，不参与统计）
      event_move_attribution_rejected  —— AI 判「不成立」的留痕（审计用，只增不删）
      event_move_attribution_pending   —— |move_pct|>=5% 且**还没拿到 AI 判定**的暂存区
      event_move_attribution            —— 主表，加 ai_verdict 列（只允许 confirm/partial）

    本函数可重复调用：CREATE TABLE IF NOT EXISTS + 列存在性检查，不改动存量数据。
    """
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_move_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                move_id INTEGER,
                news_event_id INTEGER,
                asset TEXT,
                time_lag_sec INTEGER,
                algo_score REAL,
                algo_reasons TEXT,
                ai_called INTEGER DEFAULT 0,
                ai_explanation TEXT,
                ai_confidence REAL,
                ai_error TEXT,
                move_ts INTEGER,
                move_pct REAL,
                move_kind TEXT,
                duration_min INTEGER,
                ev_news_id INTEGER,
                ev_source TEXT,
                ev_action TEXT,
                ev_score REAL,
                ev_ts INTEGER,
                created_at INTEGER
            )
        """)
        cols = table_columns(conn, "event_move_attribution")
        if cols and "ai_verdict" not in cols:
            conn.execute("ALTER TABLE event_move_attribution ADD COLUMN ai_verdict TEXT")
        # v338: 机器自检（双重验证第一道）—— 资产/方向/时序三票制。
        # 不依赖 LLM 通道，可全量跑；AI 复核（ai_verdict）是第二道。
        for _col in ("rule_verdict", "rule_reason", "rule_checked_at"):
            if cols and _col not in cols:
                conn.execute(
                    "ALTER TABLE event_move_attribution ADD COLUMN %s %s"
                    % (_col, "INTEGER" if _col.endswith("_at") else "TEXT"))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_move_attribution_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                move_id INTEGER,
                news_event_id INTEGER,
                asset TEXT,
                time_lag_sec INTEGER,
                algo_score REAL,
                algo_reasons TEXT,
                ev_news_id INTEGER,
                ev_source TEXT,
                ev_action TEXT,
                ev_ts INTEGER,
                move_ts INTEGER,
                move_pct REAL,
                move_kind TEXT,
                duration_min INTEGER,
                cand_rank INTEGER,
                created_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_move_attribution_rejected (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orig_row_id INTEGER,
                move_id INTEGER,
                news_event_id INTEGER,
                asset TEXT,
                time_lag_sec INTEGER,
                algo_score REAL,
                algo_reasons TEXT,
                ai_called INTEGER,
                ai_explanation TEXT,
                ai_confidence REAL,
                ai_verdict TEXT,
                move_ts INTEGER,
                move_pct REAL,
                move_kind TEXT,
                duration_min INTEGER,
                ev_news_id INTEGER,
                ev_source TEXT,
                ev_action TEXT,
                ev_ts INTEGER,
                reject_reason TEXT,
                created_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_move_attribution_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                move_id INTEGER,
                news_event_id INTEGER,
                asset TEXT,
                time_lag_sec INTEGER,
                algo_score REAL,
                algo_reasons TEXT,
                ai_called INTEGER DEFAULT 0,
                ai_explanation TEXT,
                ai_confidence REAL,
                ai_error TEXT,
                move_ts INTEGER,
                move_pct REAL,
                move_kind TEXT,
                duration_min INTEGER,
                ev_news_id INTEGER,
                ev_source TEXT,
                ev_action TEXT,
                ev_score REAL,
                ev_ts INTEGER,
                created_at INTEGER,
                ai_verdict TEXT
            )
        """)
        pcols = table_columns(conn, "event_move_attribution_pending")
        if pcols and "ai_verdict" not in pcols:
            conn.execute("ALTER TABLE event_move_attribution_pending ADD COLUMN ai_verdict TEXT")
    except sqlite3.Error as exc:
        print(f"[MIGRATE] v323 归因三道闸建表失败: {exc}", flush=True)


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


def _ensure_ai_decisions_probe_guard(conn: sqlite3.Connection) -> None:
    """v464(P0+): 拦截探针决策行（reasoning='test'）。

    11-19 事故：pytest 用例直接连生产库，写入 4 条 reasoning='test' 的假决策，
    随后被 trade_executor 当成真决策下单并成交。测试用例本应走 conftest 的临时
    库，但直连写法会绕过它，所以在 SQLite 内核层加 BEFORE INSERT 触发器兜底：
    该行静默丢弃，不打断同批其它正常决策。
    """
    if "ai_decisions" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        conn.execute(_AI_DECISIONS_GUARD_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_ai_decisions_reject_probe 创建失败: {_exc}", flush=True)


def _ensure_raw_news_probe_guard(conn: sqlite3.Connection) -> None:
    """v465(P0+): 拦截探针新闻行（source='test' / content='test news…'）。

    11-19 12:15 事故：`tests/test_write_guard.py` 直连生产库写入 4 条 source='test'
    的假新闻，ai_worker 把它们当真新闻认领 → 模型对着"test news 9000"这种空内容
    分析 → 全部降级 llm_empty → 决策产出为 0、待分析队列 340 条长期不动。
    测试应走 conftest 临时库，但直连写法会绕过它，所以在内核层加触发器兜底：
    该行静默丢弃，不打断采集器同批的正常写入。
    """
    if "raw_news" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        conn.execute(_RAW_NEWS_PROBE_GUARD_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_raw_news_reject_probe 创建失败: {_exc}", flush=True)


def _ensure_quick_sim_orphan_guard(conn: sqlite3.Connection) -> None:
    """v466(P0+): 拦截孤儿成交（decision_id 在 ai_decisions 里不存在）。

    11-19 13:01 事故：又出现 decision_id=10009 的成交行 —— ai_decisions 里没有 10009、
    entry_price=100.0（探针价）、exit=101 却记 pnl=-5。来源是测试脚本直连生产库。
    quick_sim.py 的 Python 侧校验（v453）拦不住绕过 ORM 的直连写法，所以在内核层再拦一道。
    """
    if "quick_sim_trades" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        conn.execute(_QUICK_SIM_ORPHAN_GUARD_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_quick_sim_reject_orphan 创建失败: {_exc}", flush=True)


def _ensure_t2_order_dedup_guard(conn: sqlite3.Connection) -> None:
    """v468(P0+): 同一决策只允许一张有效委托（幂等护栏）。

    11-19 13:16 事故：两个引擎进程同时消费同一条决策 13093，各下一张卖单
    （t2_orders 62/63），生成两笔一模一样的 ETH 空单（positions 58/59），
    实际敞口翻倍。执行侧无法感知"另一进程已经下过单"，所以在库里做幂等兜底。
    """
    if "t2_orders" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        conn.execute(_T2_ORDER_DEDUP_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_t2_orders_one_per_decision 创建失败: {_exc}", flush=True)


def _ensure_ai_decisions_trade_uuid(conn: sqlite3.Connection) -> None:
    """v483: 决策行 trade_uuid 自动补位。

    之前 trade_uuid 只在 admin 接口手动 backfill，班次链路从不写入，导致
    ai_decisions / quick_sim_trades / structured_reflections 全为 NULL，
    "决策→成交→反思"没法按 uuid 串起来（口径上宣称的全链路可追溯是空的）。
    放在内核层做：任何写入路径（worker / drain / 脚本）新建决策都自动拿到
    16 位 hex，与 id_reconcile._gen_trade_uuid() 的格式一致。
    """
    if "ai_decisions" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    try:
        conn.execute(_AI_DECISIONS_TRADE_UUID_TRIGGER_DDL)
    except sqlite3.Error as _exc:
        print(f"[MIGRATE] trg_ai_decisions_fill_trade_uuid 创建失败: {_exc}", flush=True)


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
# ──────────────────────────────────────────────────────────────────
# v437 P0-0d：ai_call_stats 异步写入器
#   统计是"尽力而为"的数据，不该让门卫分类在写锁竞争上白等 12 秒。
#   单写线程 + 单连接顺序落库，天然避免多线程抢写锁。
# ──────────────────────────────────────────────────────────────────
_call_stats_q: "queue.Queue[tuple]" = None  # type: ignore[assignment]
_call_stats_sync: bool = False   # 测试/单次脚本可置 True 强制同步写
_call_stats_stats = {"written": 0, "failed": 0, "dropped": 0}


def _call_stats_worker() -> None:
    # v468: 合并批量提交。统计是尽力而为数据，旧实现每条一次 commit，
    # 近 1h 893 条 → 高频写 commit 争锁；改为凑满 _STATS_BATCH 条或等
    # _STATS_FLUSH_S 秒后一次多 VALUES 批量提交，单次持锁窗口覆盖整批。
    # v495: 凑批等待期间**不持有写连接** —— 旧实现连接在取第一条时就打开，
    # SQLite 隐式事务从首条写语句开始一直挂到 commit，1.5s 凑批等待期间
    # 连接处于"已写未提交"状态，持着写锁让其他 writer 白等（探针里
    # _call_stats_worker 是最高频长事务来源）。现在连接只在落库前取、
    # commit/rollback 后即关，等待阶段完全不碰库。
    _STATS_BATCH = int(os.getenv("TRIDENT_CALL_STATS_BATCH", "20"))
    _STATS_FLUSH_S = 1.5
    conn = None
    while True:
        batch: List[tuple] = []
        try:
            params = _call_stats_q.get()
            if params is None:
                break
            batch.append(params)
            # 非阻塞凑批：队列还有就继续取，凑满或短等待后落库。
            # 注意：此阶段不取连接，避免隐式事务跨等待占锁。
            deadline = time.monotonic() + _STATS_FLUSH_S
            while len(batch) < _STATS_BATCH:
                try:
                    item = _call_stats_q.get(timeout=max(0.0, deadline - time.monotonic()))
                    if item is None:
                        break
                    batch.append(item)
                except queue.Empty:
                    break
            conn = get_connection()
            if len(batch) == 1:
                conn.execute(_CALL_STATS_SQL, batch[0])
            else:
                placeholders = ",".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(batch))
                flat: List[Any] = []
                for item in batch:
                    flat.extend(item)
                conn.execute(
                    f"INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id,"
                    f" tokens_in, tokens_out, latency_ms, status, error_code, call_site,"
                    f" cached_tokens, error_body) VALUES {placeholders}",
                    flat,
                )
            conn.commit()
            _call_stats_stats["written"] += len(batch)
        except Exception:
            _call_stats_stats["failed"] += len(batch)
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            for _ in batch:
                try:
                    _call_stats_q.task_done()
                except Exception:
                    pass


def _call_stats_start() -> "queue.Queue[tuple]":
    global _call_stats_q
    if _call_stats_q is not None:
        return _call_stats_q
    _call_stats_q = queue.Queue(maxsize=20000)
    t = threading.Thread(target=_call_stats_worker, name="ai-call-stats-writer", daemon=True)
    t.start()
    return _call_stats_q


def _call_stats_enqueue(params: tuple) -> bool:
    """入队成功返回 True；未启用/队列满返回 False（由调用方退回同步写）。"""
    if _call_stats_sync:
        return False
    try:
        q = _call_stats_start()
    except Exception:
        return False
    try:
        q.put_nowait(params)
        return True
    except Exception:
        _call_stats_stats["dropped"] += 1
        return False


def flush_call_stats(timeout: float = 10.0) -> dict:
    """把队列里待写的统计刷完（进程退出前/测试里用）。"""
    if _call_stats_q is None:
        return dict(_call_stats_stats)
    try:
        _call_stats_q.join()
    except Exception:
        pass
    return dict(_call_stats_stats)


def call_stats_writer_stats() -> dict:
    st = dict(_call_stats_stats)
    try:
        st["queued"] = _call_stats_q.qsize() if _call_stats_q is not None else 0
    except Exception:
        st["queued"] = -1
    return st


_BURN_N = 0
_BURN_T0 = 0.0


def log_ai_call(
    *, purpose: str, key_alias: str, model_id: str,
    tokens_in: int = 0, tokens_out: int = 0,
    latency_ms: int = 0, status: str = "ok",
    error_code: str = "", call_site: str = "",
    cached_tokens: int = 0, error_body: str = "",
) -> None:
    """v187: 写入 1 条 LLM 调用统计。失败仅 print 不抛(不影响主链路)。

    v345.12: 增加 cached_tokens —— MiniMax 对相同前缀自动缓存（实测第2次起
    system 段 6,982/6,983 全部命中），但原表只记 prompt_tokens，看不出命中率。
    不记这一列，就无法判断"到底哪些 token 在按全价计费"。

    v437 P0-0d：改为**异步入队**，统计写入不再阻塞 AI 链路。
    事故（2026-11-17 长事务哨兵）：l2_batcher 的每次分类都会调本函数，
    函数内部 `execute_with_retry(max_retries=8)` 在锁竞争时会**同步等 12–14 秒**
    才回滚 —— 这 12 秒是白等的：它发生在门卫分类的关键路径上，直接拖慢新闻
    处理（实测长事务 12.9s/13.6s 全部来自这条 INSERT）。
    统计是"尽力而为"的数据，丢一条不影响任何决策，绝不值得让门卫等 10 秒。
    """
    # ── v501 零产出熔断 ───────────────────────────────────────────────
    # 事故：402 后各路径逐通道重试，tokens=0 但请求白扣，2 小时烧掉 1,900 次。
    # 规则：150 秒窗口内累计 25 次「失败且 0 token」→ 全局停 LLM 15 分钟；
    # 任意一次成功调用即清零。阈值远高于正常抖动（正常是成功/失败交替）。
    try:
        global _BURN_N, _BURN_T0
        if status == "ok" or (tokens_in + tokens_out) > 0:
            _BURN_N = 0
        else:
            now_s = time.time()
            if now_s - _BURN_T0 > 150:
                _BURN_N, _BURN_T0 = 0, now_s
            _BURN_N += 1
            if _BURN_N >= 25:
                try:
                    import llm_budget as _lb_burn
                    _lb_burn.note_quota_dead(15, "零产出熔断：%d 次失败/0 token" % _BURN_N)
                except Exception:
                    pass
                _BURN_N = 0
    except Exception:
        pass

    _sql = (
        "INSERT INTO ai_call_stats (ts, purpose, key_alias, model_id,"
        " tokens_in, tokens_out, latency_ms, status, error_code, call_site,"
        " cached_tokens, error_body)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    # v483：失败调用不允许 error_code 留空。历史 1,544 条空码无法反推，
    # 但新调用至少写 "unknown"，让"空码"不再继续增长。
    _status = str(status)[:16]
    _ec = str(error_code or "").strip()
    if not _ec and _status != "ok":
        _ec = "unknown"
    _eb = str(error_body or "")[:300]
    params = (int(time.time()), str(purpose)[:32], str(key_alias)[:32],
              str(model_id)[:80], int(tokens_in), int(tokens_out),
              int(latency_ms), _status,
              _ec[:32], str(call_site)[:48],
              int(cached_tokens or 0), _eb)
    if _call_stats_enqueue(params):
        return
    # 队列不可用（未启线程/已满）→ 退回同步写，保证不丢
    try:
        execute_with_retry(get_connection, _sql, params, max_retries=8)
    except Exception as exc:  # pragma: no cover
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
