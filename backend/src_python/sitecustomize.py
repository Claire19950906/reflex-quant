# -*- coding: utf-8 -*-
"""sitecustomize.py — 全进程默认给 SQLite 连接 30s 等锁窗口。

为什么在这里做
--------------
这台机器有 100+ 个 launchd 任务同时写同一个 trident_event_bus.db，很多脚本自己
写死了 ``timeout=5`` 或 ``PRAGMA busy_timeout=5000``，高峰期排队 5 秒就放弃，
日志里就是成片的 ``database is locked``（实测最近 10 分钟仍有 134 次，
来自 tencent_kline / event_kline_impact / atom_backfill / jin10 / sina_flash…）。
逐个改 100 个文件不现实，也不该改业务逻辑；这里只把 **连接的默认等待时间**
抬到 30 秒，写不进去就排队，而不是立刻报错。

只做两件事，且都可关：
    · sqlite3.connect 未显式传 timeout 时，默认 30s（TRIDENT_BUSY_TIMEOUT_MS）；
    · 建连接后补一条 PRAGMA busy_timeout，覆盖脚本里写死的更小值。
"""
from __future__ import annotations

import os

_MS = os.environ.get("TRIDENT_BUSY_TIMEOUT_MS", "30000")
try:
    _ms_int = int(_MS)
except ValueError:
    _ms_int = 30000
if _ms_int <= 0:
    _ms_int = 30000

try:
    import sqlite3 as _sqlite3

    if not getattr(_sqlite3, "_trident_patched", False):
        _orig_connect = _sqlite3.connect
        _floor = _ms_int

        # sqlite3.Connection 是 C 扩展类型，不能直接给它 attribute 赋值（会 TypeError），
        # 但可以继承出子类，再用 factory= 让 connect 返回这个子类。
        class _TridentConnection(_sqlite3.Connection):  # type: ignore[misc]
            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql[:24].lower().strip().startswith(
                    "pragma busy_timeout"
                ):
                    try:
                        val = int(str(sql).split("=")[-1].strip(" ;)"))
                        if val < _floor:
                            sql = f"PRAGMA busy_timeout={_floor}"
                    except ValueError:
                        pass
                return super().execute(sql, *args, **kwargs)

        # ── v484: 暂停期写守（最后一道闸门）─────────────────────────────────
        # 前面两道（engine 自检、作业包装器闸门）都要求"走那个入口"。可 17:28 那轮
        # 是别的会话直接 `python xxx.py` 起的活儿，两道闸门都绕过了。
        # sitecustomize 是 PYTHONPATH 里有 src_python 的所有进程的**自动加载点**，
        # 把断写做在 sqlite3.connect 这一层，无论谁怎么起的进程都会被罩住：
        # DATA_PAUSED 存在 → 连生产库的写语句直接报错（读不受影响）。
        # 白名单按进程名放行：bridge/api 要保页面，守护类进程只读或只管进程。
        import sys as _sys

        _PROD_DB_PAUSE = os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "trident_event_bus.db"))
        _PAUSE_FLAG = os.path.join(os.path.expanduser("~"), ".trident", "state", "DATA_PAUSED")
        _WRITE_SQL_RE = __import__("re").compile(
            r"^\s*(insert|update|delete|replace|drop|alter|create|vacuum|reindex|attach|detach)\b",
            __import__("re").I)
        _ALLOW_WHEN_PAUSED = (
            "bridge_server.py", "master_api.py", "api_server.py",
            "memory_guard.py", "lock_watchdog.py", "engine_watchdog.py",
            "service_guard.py", "log_rotate.py", "doctor.py", "audit_jobs.py",
        )

        def _paused_now() -> bool:
            """暂停标记存在，且本进程不在白名单里。"""
            try:
                if os.environ.get("TRIDENT_IGNORE_PAUSE") == "1":
                    return False
                if not os.path.exists(_PAUSE_FLAG):
                    return False
                argv0 = os.path.basename(_sys.argv[0] or "")
                if argv0 in _ALLOW_WHEN_PAUSED:
                    return False
                if argv0 in ("", "pytest", "py.test") or "pytest" in argv0:
                    # pytest 走 tests/conftest 的守卫（它会给子进程设 TRIDENT_BLOCK_PROD_WRITE）
                    return False
                return True
            except Exception:
                return False

        class _PauseGuardConnection(_TridentConnection):  # type: ignore[misc]
            def _deny(self, sql):
                if isinstance(sql, str) and _WRITE_SQL_RE.match(sql):
                    raise _sqlite3.OperationalError(
                        "DATA_PAUSED：数据暂停中，禁止写生产库（%s）。"
                        "恢复请先 bash ~/.trident/wrappers/resume_data.sh"
                        % (sql.strip().splitlines()[0][:70],)
                    )

            def execute(self, sql, *args, **kwargs):
                self._deny(sql)
                return super().execute(sql, *args, **kwargs)

            def executemany(self, sql, *args, **kwargs):
                self._deny(sql)
                return super().executemany(sql, *args, **kwargs)

            def executescript(self, sql, *args, **kwargs):
                self._deny(sql)
                return super().executescript(sql, *args, **kwargs)

        def _is_prod_target(args, kwargs) -> bool:
            db = kwargs.get("database", args[0] if args else None)
            if db is None:
                return False
            if not isinstance(db, str):
                try:
                    db = os.fspath(db)
                except Exception:
                    return False
            db = db.split("?", 1)[0]
            if db.startswith("file:"):
                db = db[5:]
            return os.path.abspath(db) == _PROD_DB_PAUSE

        # ── v502: 长驻服务默认自动提交 ────────────────────────────────────
        # 事故（2026-11-20）：bridge 把写锁开了 27 分钟不提交 —— sqlite3 默认
        # 首次 INSERT 就开隐式事务，事务里只要夹一次取价/翻译（网络等待），
        # 锁就一直不释放，全站写入全部 database is locked、队列停摆。
        # 只对三个长驻服务生效；显式 BEGIN 的地方（认领/结算）不受影响。
        _AUTOCOMMIT_PROCS = ("bridge_server.py", "engine.py", "api_server.py")

        def _connect(*args, **kwargs):
            kwargs.setdefault("timeout", _ms_int / 1000.0)
            if "isolation_level" not in kwargs:
                try:
                    if os.path.basename(_sys.argv[0] or "") in _AUTOCOMMIT_PROCS:
                        kwargs["isolation_level"] = None
                except Exception:
                    pass
            kwargs.setdefault("factory", _TridentConnection)
            if _paused_now() and _is_prod_target(args, kwargs):
                if kwargs.get("factory") is _TridentConnection:
                    kwargs["factory"] = _PauseGuardConnection
            conn = _orig_connect(*args, **kwargs)
            try:
                conn.execute(f"PRAGMA busy_timeout={_ms_int}")
            except Exception:
                pass
            return conn

        _sqlite3.connect = _connect
        _sqlite3._trident_patched = True
except Exception:
    pass


# ── 卡死取证（v487）：引擎/桥接被同步调用卡住时，py-spy/lldb 在本机都要 root，
# 环境里拿不到。改用 faulthandler：向进程发 SIGUSR1 即在 stderr 打全线程栈，
# 零依赖、零开销（只在信号到达时写一次）。用法：kill -USR1 <pid>
try:
    import faulthandler as _fh
    import signal as _sig
    import os as _os
    if _os.getenv("TRIDENT_STACK_DUMP", "1") != "0":
        _fh.register(_sig.SIGUSR1, all_threads=True, chain=False)
        # 同时也兜住未捕获的段错误，方便事后定位
        _fh.enable()
        import sys as _sys
        _sys.stderr.write("[STACK-DUMP] SIGUSR1 registered pid=%d\n" % _os.getpid())
        _sys.stderr.flush()
except Exception:
    pass
