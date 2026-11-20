# -*- coding: utf-8 -*-
"""仓库根 conftest —— 只做一件事：给 pytest 装「生产库写保护」。

背景（2026-11-19 第三次事故）：从仓库根跑 `python -m pytest backend/tests ...` 时，
个别用例把 TRIDENT_DB_PATH 指回生产库、并 drop 掉守卫触发器，把
ai_decisions（390 → 4）和 raw_news（1,270 → 217）截断。

保护实现见 backend/tests/_prod_db_guard.py：sqlite3.connect 对生产库路径注入
GuardedConnection，读放行、写（INSERT/UPDATE/DELETE/DROP/CREATE…）抛错。
"""
from __future__ import annotations

import importlib.util
import os

_GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "backend", "tests", "_prod_db_guard.py")
if os.path.exists(_GUARD):
    _spec = importlib.util.spec_from_file_location("_root_prod_db_guard", _GUARD)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.install()
# 让子进程也继承"禁止写生产库"（17:14 那轮 pytest 就是靠 subprocess 继续写库的）
os.environ.setdefault("TRIDENT_BLOCK_PROD_WRITE", "1")

