#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pytest 生产库写保护（2026-11-19 第三次事故后加的硬闸）。

事故史（同一个坑踩了三次）
--------------------------
· 11-16 18:51：backend/ 下直接 pytest，用例 fixture 对默认路径 DELETE/建表 →
  raw_news 19,214 → 6，ai_decisions 478 → 4。
· 11-19 12:15：test_write_guard.py 直连生产库 INSERT 探针新闻 → ai_worker 认领，
  决策产出归零。
· 11-19 16:47：某用例把 TRIDENT_DB_PATH 指回真库，同时 drop_guard_triggers()
  （无任何路径校验）把生产库三道 BEFORE INSERT 触发器摘掉；ai_decisions 390 → 4，
  raw_news 1,270 → 217。

之前的防护都写在 conftest 里（"没显式指定就指向临时库"），但只要用例自己
setenv 回真库、或走 monkeypatch，就绕过去了。所以在 sqlite3 内核层再加一道：
**凡是打开生产库的连接，写语句一律抛错，只读放行**。

实现：sqlite3.connect 的 factory 参数可以指定 Connection 子类，子类覆写
execute / executemany / executescript，命中写关键字即 raise。读（SELECT/PRAGMA）
不受影响，所以"拿真库做只读契约校验"的用例仍然能跑。
"""
from __future__ import annotations

import os
import re
import sqlite3

# backend/tests/_prod_db_guard.py → backend/trident_event_bus.db
PROD_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trident_event_bus.db",
)

_WRITE_RE = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|create|vacuum|reindex|attach|detach)\b",
    re.I,
)
# 允许的写：PRAGMA 里只有 wal_checkpoint / optimize 之类不改变数据语义的开关，
# 这里仍然禁掉 PRAGMA 写（journal_mode 等会改库文件），只放行纯读 PRAGMA。
_ALLOWED_PRAGMA = re.compile(r"^pragma\s+(table_info|index_list|foreign_key_list|"
                             r"database_list|compile_options|quick_check|integrity_check|"
                             r"user_version|journal_mode|page_count|freelist_count)\b", re.I)


class _ProdWriteBlocked(RuntimeError):
    pass


class GuardedConnection(sqlite3.Connection):
    """生产库连接：读放行，写抛错。"""

    def _guard(self, sql) -> None:
        if not isinstance(sql, str):
            return
        s = sql.strip()
        # PRAGMA 一律放行（busy_timeout / foreign_keys / table_info 等读配置，
        # 不改变行数据；真正危险的 DELETE/DROP 由下面的关键字拦截）
        if _ALLOWED_PRAGMA.match(s):
            return
        if _WRITE_RE.search(s):
            raise _ProdWriteBlocked(
                "pytest 禁止对生产库执行写语句: %s\n"
                "（地址 %s）。历史事故：用例 DELETE/建表把生产数据截断。"
                "要写请显式把 TRIDENT_DB_PATH 指到临时库；"
                "只读校验请用 sqlite3.connect('file:...?mode=ro', uri=True)。"
                % (s.splitlines()[0][:80], PROD_DB)
            )

    def execute(self, sql, *args, **kwargs):
        self._guard(sql)
        return super().execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._guard(sql)
        return super().executemany(sql, *args, **kwargs)

    def executescript(self, sql, *args, **kwargs):
        self._guard(sql)
        return super().executescript(sql, *args, **kwargs)


def install() -> None:
    """安装守卫（幂等）。"""
    if getattr(sqlite3.connect, "_prod_guard_installed", False):
        return
    _orig = sqlite3.connect

    def connect(database, *args, **kwargs):
        try:
            target = os.path.abspath(os.fspath(database))
        except TypeError:
            return _orig(database, *args, **kwargs)
        if target == PROD_DB and not kwargs.pop("_allow_prod_read", False):
            kwargs.setdefault("factory", GuardedConnection)
        return _orig(database, *args, **kwargs)

    connect._prod_guard_installed = True  # type: ignore[attr-defined]
    sqlite3.connect = connect  # type: ignore[assignment]
