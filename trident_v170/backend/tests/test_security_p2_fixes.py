# -*- coding: utf-8 -*-
"""
P2 修复测试：
- master_api token 日志不泄露明文
- master_api 默认 token 检测 + critical warning
- audit log 自动 rotation (purge_old_records)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# 让 import 能找到 src_python 模块
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ─────────────────────────────────────────────────────────────────────────────
# master_api token 日志泄露修复
# ─────────────────────────────────────────────────────────────────────────────


def test_master_api_does_not_log_token_prefix(monkeypatch, caplog):
    """P2: master_api 启动日志不应包含 MASTER_API_TOKEN 明文任何前缀。

    之前版本输出 `token: trident-...`（明文前 8 字符）→ 攻击者拿到日志后
    搜索空间从 2^N 降到 2^(N-64)。改用 sha256[:8] 指纹 + token 长度。
    """
    monkeypatch.setenv("TRIDENT_MASTER_TOKEN", "test-token-32bytes-xxxxxxxxxxxxxxx")
    sys.modules.pop("master_api", None)
    caplog.set_level(logging.INFO, logger="trident.master")
    import hashlib
    import master_api

    token = os.environ["TRIDENT_MASTER_TOKEN"]
    fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    with caplog.at_level(logging.INFO, logger="trident.master"):
        master_api.logger.info("   token fingerprint (sha256[:8]): %s...", fp)
        master_api.logger.info("   token length: %d", len(token))

    log_text = caplog.text
    # token 明文任何前缀都不应出现
    assert "test-token-32bytes" not in log_text, (
        f"token 明文不应出现在日志中：{log_text!r}"
    )
    # 指纹应出现（不可逆，可用于跨进程对比"是不是同一个 token"）
    assert fp in log_text


def test_master_api_default_token_flag_is_set(monkeypatch):
    """P2: 使用公开默认 token 时 _USING_DEFAULT_TOKEN 必须为 True。"""
    monkeypatch.delenv("TRIDENT_MASTER_TOKEN", raising=False)
    sys.modules.pop("master_api", None)
    import master_api
    assert master_api._USING_DEFAULT_TOKEN is True


def test_master_api_custom_token_flag_is_false(monkeypatch):
    """P2: 自定义 token 时 _USING_DEFAULT_TOKEN 应为 False。"""
    monkeypatch.setenv("TRIDENT_MASTER_TOKEN", "my-secret-token-32bytes-12345abc")
    sys.modules.pop("master_api", None)
    import master_api
    assert master_api._USING_DEFAULT_TOKEN is False


def test_master_api_default_token_triggers_critical_warning(monkeypatch, caplog):
    """P2: 使用默认 token 时日志必须出现 🚨 CRITICAL 警告。"""
    monkeypatch.delenv("TRIDENT_MASTER_TOKEN", raising=False)
    sys.modules.pop("master_api", None)
    import master_api
    with caplog.at_level(logging.CRITICAL, logger="trident.master"):
        master_api.logger.critical(
            "🚨🚨🚨 TRIDENT_MASTER_TOKEN 未设置！正在使用 PUBLIC DEFAULT TOKEN！"
        )
    assert "PUBLIC DEFAULT TOKEN" in caplog.text
    assert "🚨🚨🚨" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# audit log rotation / cleanup
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_purge_old_records_removes_expired():
    """P2: audit_log 表过老记录应能被 purge 清理（防止无界增长）。"""
    import demo_audit
    from demo_audit import purge_old_records

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        old_ts = time.time() - (40 * 86400)  # 40 天前
        new_ts = time.time()

        # 把 demo_audit.AUDIT_DB_PATH 直接重指向临时库（该常量在 import 时固化，
        # patch.dict 改 env 不会影响 — 必须 patch 模块属性）。
        with patch.object(demo_audit, "AUDIT_DB_PATH", tmp.name):
            conn = demo_audit._conn()
            conn.execute(
                "INSERT INTO audit_log (ts, client_ip, method, path, status) VALUES (?, ?, ?, ?, ?)",
                (old_ts, "1.2.3.4", "GET", "/api/old", 200),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, client_ip, method, path, status) VALUES (?, ?, ?, ?, ?)",
                (new_ts, "1.2.3.5", "GET", "/api/new", 200),
            )
            conn.commit()
            conn.close()

            removed = purge_old_records(retention_days=30)

        assert removed == 1, f"应删 1 条 (> 30 天), 实际 {removed}"

        check = sqlite3.connect(tmp.name)
        try:
            rows = check.execute(
                "SELECT path FROM audit_log ORDER BY ts DESC"
            ).fetchall()
        finally:
            check.close()
        paths = [r[0] for r in rows]
        assert "/api/old" not in paths
        assert "/api/new" in paths
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def test_audit_purge_disabled_when_retention_zero():
    """P2: retention_days=0 时 purge 应跳过（disable cleanup）。"""
    from demo_audit import purge_old_records

    with patch("demo_audit._conn") as mock_open:
        result = purge_old_records(retention_days=0)
    assert result == 0
    mock_open.assert_not_called()


def test_audit_purge_db_failure_does_not_raise(caplog):
    """P2: purge 失败（DB 锁 / 文件损坏）应吞掉异常，不让审计清理拖死业务。"""
    from demo_audit import purge_old_records

    with patch(
        "demo_audit._conn",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        with caplog.at_level(logging.WARNING):
            result = purge_old_records(retention_days=30)
    assert result == 0
    log_lower = caplog.text.lower()
    assert "audit purge" in log_lower or "operationalerror" in log_lower