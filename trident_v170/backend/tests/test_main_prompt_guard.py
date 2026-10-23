"""阶段4 主 prompt 覆盖机制防遮蔽加固回归测试。

背景：ai_main_prompts 覆盖表允许用户在页面编辑主 prompt（system_prompt 等），
编辑内容会遮蔽代码内置默认。风险：DB 里存了升级前的旧覆盖时，阶段1/3/4 对
内置 prompt 的改动会被旧内容遮蔽、不生效。

加固方案：覆盖表加 base_version 代际列（保存时代码内置主 prompt 的版本），
load 时过滤 base_version < ai_prompts.MAIN_PROMPT_VERSION 的旧覆盖，回退代码
内置默认。本测试在临时 sqlite 上验证：旧覆盖被忽略、新覆盖生效、保存写版本、
迁移补列。
"""
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import db
import ai_prompts


@pytest.fixture()
def prompts_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "prompts.db")

    def _fresh():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    # 只建被测表（新 schema 含 base_version），避免完整 migrate 的 config 依赖
    conn = _fresh()
    conn.execute(db._CREATE_AI_MAIN_PROMPTS)
    conn.commit()
    conn.close()

    monkeypatch.setattr(ai_prompts, "_connect", _fresh)
    ai_prompts._main_cache.update({"ts": 0.0, "data": {}})
    ai_prompts._warned_stale.clear()
    return db_path


def _insert(conn, kind, content, base_version):
    conn.execute(
        "INSERT OR REPLACE INTO ai_main_prompts "
        "(kind, name, content, base_version, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
        (kind, kind, content, base_version),
    )
    conn.commit()


def test_stale_override_ignored(prompts_db):
    conn = ai_prompts._connect()
    _insert(conn, "system_prompt", "旧版覆盖内容", 0)  # 代际落后于代码内置版
    conn.close()

    overrides = ai_prompts.load_main_prompt_overrides(ttl=0.0)
    assert "system_prompt" not in overrides


def test_save_writes_current_version_and_applies(prompts_db):
    ai_prompts.save_main_prompt("system_prompt", "用户最新自定义")

    overrides = ai_prompts.load_main_prompt_overrides(ttl=0.0)
    assert overrides.get("system_prompt") == "用户最新自定义"

    conn = ai_prompts._connect()
    row = conn.execute(
        "SELECT base_version FROM ai_main_prompts WHERE kind='system_prompt'"
    ).fetchone()
    conn.close()
    assert row is not None and int(row["base_version"]) == ai_prompts.MAIN_PROMPT_VERSION


def test_ensure_columns_adds_base_version(tmp_path):
    db_path = str(tmp_path / "old.db")
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    # 旧版表：无 base_version 列
    c.execute(
        "CREATE TABLE ai_main_prompts (kind TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
        "content TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '')"
    )
    c.commit()

    db._ensure_columns(c, "ai_main_prompts", db._AI_MAIN_PROMPTS_COLUMNS)
    cols = {r[1] for r in c.execute("PRAGMA table_info(ai_main_prompts)")}
    c.close()
    assert "base_version" in cols