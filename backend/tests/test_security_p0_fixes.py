# -*- coding: utf-8 -*-
"""P0 安全修复回归测试。

覆盖:
  - ALLOWED_UPLOAD_TYPES 不再含 text/html / text/csv
  - _extract_table_names 解析 FROM/JOIN 子句（含反引号/双引号/方括号）
  - _sql_uses_only_allowed_tables 白名单命中/拒绝逻辑
  - require_api_key: full 模式默认要求 X-Trident-API-Key，可通过 env 关闭
"""
from __future__ import annotations

import os
import sys

# 让 ``import config`` / ``import api_server`` / ``import license_auth`` 能找到
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typing import Any, Dict, Optional

import pytest


# ── Fix 1: ALLOWED_UPLOAD_TYPES ────────────────────────────────────────


def test_upload_types_block_html_and_csv():
    """text/html / text/csv 必须从 ALLOWED_UPLOAD_TYPES 移除（防 XSS/CSV injection）。"""
    from config import ALLOWED_UPLOAD_TYPES

    assert "text/html" not in ALLOWED_UPLOAD_TYPES
    assert "text/csv" not in ALLOWED_UPLOAD_TYPES
    # 安全类型保留
    assert "image/png" in ALLOWED_UPLOAD_TYPES
    assert "application/pdf" in ALLOWED_UPLOAD_TYPES
    assert "text/plain" in ALLOWED_UPLOAD_TYPES


# ── Fix 2: SQL 表名白名单 ──────────────────────────────────────────────


def test_extract_table_names_simple_from():
    import api_server as srv
    names = srv._extract_table_names("SELECT * FROM ai_decisions")
    assert names == ["ai_decisions"]


def test_extract_table_names_with_join_and_alias():
    import api_server as srv
    sql = (
        "SELECT a.id, b.content "
        "FROM ai_decisions a "
        "LEFT JOIN raw_news b ON a.news_id = b.id"
    )
    names = srv._extract_table_names(sql)
    assert "ai_decisions" in names
    assert "raw_news" in names


def test_extract_table_names_strips_quotes_and_brackets():
    """防绕过：反引号 / 双引号 / 方括号包裹的表名仍被识别。"""
    import api_server as srv
    cases = [
        ("SELECT * FROM `sqlite_master`", "sqlite_master"),
        ('SELECT * FROM "ai_decisions"', "ai_decisions"),
        ("SELECT * FROM [raw_news]", "raw_news"),
    ]
    for sql, expected in cases:
        names = srv._extract_table_names(sql)
        assert expected in names, f"Failed to extract {expected} from {sql!r}"


def test_sql_whitelist_allows_safe_tables():
    import api_server as srv
    for table in srv._AGENT_CHAT_ALLOWED_TABLES:
        sql = f"SELECT * FROM {table}"
        ok, offending = srv._sql_uses_only_allowed_tables(sql)
        assert ok, f"Whitelist should accept {table}"
        assert offending is None


def test_sql_whitelist_blocks_sqlite_master():
    """经典 LLM 注入路径: SELECT FROM sqlite_master 必须被白名单拒绝。"""
    import api_server as srv
    sql = "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ok, offending = srv._sql_uses_only_allowed_tables(sql)
    assert not ok
    assert offending == "sqlite_master"


def test_sql_whitelist_blocks_union_attack():
    """UNION 跨表读取: 即便主表允许，UNION 后的敏感表也必须拒绝。"""
    import api_server as srv
    sql = "SELECT id FROM ai_decisions UNION SELECT name FROM pragma_database_list"
    ok, offending = srv._sql_uses_only_allowed_tables(sql)
    assert not ok
    assert offending == "pragma_database_list"


def test_sql_whitelist_blocks_pragma_keyword():
    """关键字扫描: PRAGMA 即使作为子句而非表名也应被 forbidden keywords 拒绝。"""
    import api_server as srv
    sql = "SELECT * FROM ai_decisions WHERE 1=1; PRAGMA writable_schema=1"
    upper = sql.upper()
    assert any(bad in upper for bad in srv._AGENT_CHAT_FORBIDDEN_KEYWORDS)


# ── Fix 3: require_api_key full 模式行为 ────────────────────────────────


def test_require_api_key_full_mode_requires_key(monkeypatch):
    """P0 修复: full 模式默认也需要 X-Trident-API-Key。"""
    import license_auth

    monkeypatch.setattr("demo_mode.IS_FULL", True, raising=False)
    monkeypatch.setattr("demo_mode.IS_DEMO", False, raising=False)
    monkeypatch.setattr("demo_mode.IS_LICENSED", False, raising=False)
    monkeypatch.delenv("TRIDENT_ALLOW_ANON_FULL", raising=False)

    import asyncio

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(license_auth.require_api_key(
            request=_FakeRequest(),
            x_trident_api_key=None,
        ))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "api_key_missing"


def test_require_api_key_full_mode_anon_override(monkeypatch):
    """运维通过 TRIDENT_ALLOW_ANON_FULL=1 显式关闭 → 仍然可匿名。"""
    import license_auth

    monkeypatch.setattr("demo_mode.IS_FULL", True, raising=False)
    monkeypatch.setattr("demo_mode.IS_DEMO", False, raising=False)
    monkeypatch.setattr("demo_mode.IS_LICENSED", False, raising=False)
    monkeypatch.setenv("TRIDENT_ALLOW_ANON_FULL", "1")

    import asyncio
    result = asyncio.run(license_auth.require_api_key(
        request=_FakeRequest(),
        x_trident_api_key=None,
    ))
    assert result is None


def test_require_api_key_full_mode_invalid_key(monkeypatch):
    """full 模式下错误的 key 必须返回 401。"""
    import license_auth

    monkeypatch.setattr("demo_mode.IS_FULL", True, raising=False)
    monkeypatch.setattr("demo_mode.IS_DEMO", False, raising=False)
    monkeypatch.setattr("demo_mode.IS_LICENSED", False, raising=False)
    monkeypatch.delenv("TRIDENT_ALLOW_ANON_FULL", raising=False)
    monkeypatch.setattr(license_auth, "verify_api_key", lambda token: None)

    import asyncio
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(license_auth.require_api_key(
            request=_FakeRequest(),
            x_trident_api_key="bogus-token-12345",
        ))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "api_key_invalid"


def test_require_api_key_full_mode_valid_key(monkeypatch):
    """full 模式下合法 key 通过校验。"""
    import license_auth

    monkeypatch.setattr("demo_mode.IS_FULL", True, raising=False)
    monkeypatch.setattr("demo_mode.IS_DEMO", False, raising=False)
    monkeypatch.setattr("demo_mode.IS_LICENSED", False, raising=False)
    monkeypatch.delenv("TRIDENT_ALLOW_ANON_FULL", raising=False)
    monkeypatch.setattr(
        license_auth, "verify_api_key",
        lambda token: {"name": "test-key", "tier": "self"},
    )

    import asyncio
    result = asyncio.run(license_auth.require_api_key(
        request=_FakeRequest(),
        x_trident_api_key="valid-token",
    ))
    assert result == {"name": "test-key", "tier": "self"}


# ── Helpers ────────────────────────────────────────────────────────────


class _FakeRequest:
    """仅用于 require_api_key 路径的占位 Request（不读 headers）。"""
    def __init__(self):
        self.headers: Dict[str, str] = {}
        self.client = None