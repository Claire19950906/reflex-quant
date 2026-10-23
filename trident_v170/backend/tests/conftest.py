# -*- coding: utf-8 -*-
"""pytest conftest — 全局 fixture（v171+ 时间结构化测试稳定基线）。

自动作用（autouse=True）：
- 重置 clock 模块状态（_last_wall/_last_mono/_jump_warned → 0）
- 重置 TRIDENT_TZ → 'UTC'（避免本地开发者 TZ 影响）
- 重置 logging.NullHandler 状态

非自动作用：
- _fresh_db() → :memory: db + migrate（v171+ 列就绪）
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# ── Phase B 回灌: 把 backend/src_python 注入 sys.path ────────────────────────
# 为什么需要：src_python 下的模块是「同级 import」（`import config` / `import db`
# / `from market_sentiment import fng`），不是包内相对导入。金融版原先靠每个测试
# 文件顶部各写一份 sys.path hack 维持，本目录其余文件未覆盖到，`pytest tests/xxx.py`
# 单跑就会 `ModuleNotFoundError: No module named 'config'`。
#
# 这里在 conftest 做全局注入，与 trident 侧 tests/conftest.py 保持一致；与既有的
# per-file hack 幂等共存（重复 insert 同一条路径无副作用）。
_SRC_PYTHON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

# 在任何测试模块 import config 之前，强制把所有测试的 SQLite 隔离到临时库。
# 背景：test_structured_api_v172.py 用 os.environ.setdefault 想隔离真实库，但 pytest
# 按字母序先收集的其它模块会先 import config，把 config.DB_PATH 固化成真实的
# trident_event_bus.db，令 setdefault 失效 → 后续测试顶层 db.migrate() 写真实库，
# 撞上运行中 engine/api 的写锁（"database is locked"），还会污染生产数据。
_ISOLATED_DB_DIR = tempfile.mkdtemp(prefix="trident_tests_")
os.environ["TRIDENT_DB_PATH"] = os.path.join(_ISOLATED_DB_DIR, "test_event_bus.db")


@pytest.fixture(autouse=True)
def _trident_test_baseline(monkeypatch):
    """每个测试前自动重置（clock 状态清零 + 清除 TRIDENT_TZ env）。

    注意：不强制设 TRIDENT_TZ=UTC，因为部分测试依赖默认 Asia/Shanghai。
    TZ 切换通过 set_tz fixture 或 测试内部手动设置。
    """
    # 1) 清除 TRIDENT_TZ env（确保各测试独立，不受外部 shell env 影响）
    monkeypatch.delenv("TRIDENT_TZ", raising=False)

    # 2) clock 状态清零
    try:
        import clock as _clock
        _clock._reset_state_for_tests()
    except Exception:
        pass

    yield

    # 测试结束后再清一次（防止 test 间状态泄漏）
    try:
        import clock as _clock
        _clock._reset_state_for_tests()
    except Exception:
        pass


@pytest.fixture
def fresh_db():
    """给需要 :memory: db 的测试一个便捷 fixture。"""
    import sqlite3
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@pytest.fixture
def set_tz():
    """切换 TRIDENT_TZ 的便捷 fixture（yield 后自动恢复）。"""
    def _set(name: str) -> None:
        os.environ["TRIDENT_TZ"] = name
    yield _set
    os.environ.pop("TRIDENT_TZ", None)