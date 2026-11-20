# -*- coding: utf-8 -*-
"""v322: raw_news.status = 'FILTERED' 的**真实 schema** 回归单测。

事故背景（2026-11-07 生产实测）
------------------------------
「待分析」4850 条永不下降。根因不是逻辑，是**数据库约束**：

  raw_news.status 的 CHECK 只允许 ('PENDING','PROCESSING','DONE','FAILED')
  而 ai_worker._expire_stale_news / scripts/cleanup_noise_pending.py
  把超龄僵尸行标成 'FILTERED'
  → 每条 UPDATE 都被数据库拒绝（CHECK constraint failed）
  → 异常又被 `except Exception: return 0` 吞掉
  → 库里 0 条 FILTERED，日志一行都没有
  → 前端「待分析」只涨不降

为什么之前的单测没抓到：测试用的是**自己造的假表**（没有 CHECK 约束），
生产用的是带 CHECK 的真表 —— 两者不一致，测试全绿、生产全炸。
本文件一律用**真实 DDL**（db._CREATE_RAW_NEWS）。

本文件锁死的不变量
------------------
A. db._CREATE_RAW_NEWS（新建库的真相源）必须允许 'FILTERED'
B. 用真实 DDL 建的表上，_expire_stale_news 必须真的把行标成 FILTERED
C. 迁移 _upgrade_raw_news_status_check 必须：
   · 放宽 CHECK（老库 → 新库）
   · 不丢行、不丢列、不丢索引
   · **不触发 ON DELETE CASCADE**（ai_decisions 有 FK → raw_news）
   · 不改写其它表里的外键引用（legacy_alter_table）
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

import db
from engine import ai_worker


def _make_real_raw_news(conn: sqlite3.Connection) -> None:
    """用**真实 DDL** 建 raw_news（+ 线上那些 ALTER 补出来的列）。"""
    conn.execute(db._CREATE_RAW_NEWS)
    db._ensure_columns(conn, "raw_news", db._RAW_NEWS_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_status ON raw_news(status)")
    conn.commit()


# ─────────────────── A. DDL 真相源 ───────────────────

def test_create_ddl_allows_filtered():
    """新建库的 DDL 必须允许 'FILTERED' —— 否则新库也会踩同一个坑。"""
    assert "'FILTERED'" in db._CREATE_RAW_NEWS, (
        "db._CREATE_RAW_NEWS 必须允许 status='FILTERED'，"
        "否则新建库上归档会被 CHECK 拒绝")


def test_status_module_knows_filtered():
    """event_status 的合法状态集合要认得 FILTERED（API 侧不误判成未知）。"""
    import event_status
    assert "FILTERED" in event_status.VALID_STATUSES, (
        "event_status.VALID_STATUSES 必须包含 FILTERED —— "
        "否则 API 会把归档行显示成「待 AI 分析」")


# ─────────────────── B. 真实 DDL 上归档必须真的生效 ───────────────────

@pytest.fixture()
def real_conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "real.db"))
    c.row_factory = sqlite3.Row
    _make_real_raw_news(c)
    yield c
    c.close()


def test_expire_works_on_real_schema(real_conn):
    """真实 DDL 上，超龄行必须真的被标成 FILTERED（不是被 CHECK 拒绝）。"""
    import time as _t
    now_ts = int(_t.time())
    real_conn.execute(
        "INSERT INTO raw_news (source, content, timestamp, ts, status, is_noise,"
        " gate_unavailable, retry_count) VALUES (?,?,?,?,?,?,?,?)",
        ("s", "old news", "2026-01-01 00:00:00", now_ts - 14 * 86400,
         "PENDING", 0, 0, 0),
    )
    real_conn.execute(
        "INSERT INTO raw_news (source, content, timestamp, ts, status, is_noise,"
        " gate_unavailable, retry_count) VALUES (?,?,?,?,?,?,?,?)",
        ("s", "fresh news", "2026-01-01 00:00:00", now_ts,
         "PENDING", 0, 0, 0),
    )
    real_conn.commit()

    n = ai_worker._expire_stale_news(real_conn)
    real_conn.commit()

    assert n == 1, "超龄行必须被归档（真实 schema 下也要成功）"
    got = real_conn.execute(
        "SELECT status FROM raw_news WHERE content='old news'").fetchone()
    assert got["status"] == "FILTERED"
    got2 = real_conn.execute(
        "SELECT status FROM raw_news WHERE content='fresh news'").fetchone()
    assert got2["status"] == "PENDING", "新鲜行不得被归档"


def test_expire_failure_is_not_silent(real_conn, capsys):
    """归档失败必须打日志 —— 静默吞异常正是这次事故能藏两天的原因。"""
    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    n = ai_worker._expire_stale_news(_Boom())
    out = capsys.readouterr().out
    assert n == 0
    assert "归档超龄新闻失败" in out, (
        "归档失败必须留痕（事故教训：静默吞异常 → 排查两天才定位）")


# ─────────────────── C. 迁移函数（重建表放宽 CHECK）───────────────────

_LEGACY_RAW_NEWS = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    ts INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    is_noise INTEGER NOT NULL DEFAULT 0,
    relevance_score REAL NOT NULL DEFAULT 0.0
);
"""

_LEGACY_CHILD = """
CREATE TABLE ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE
);
"""


@pytest.fixture()
def legacy_conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "legacy.db"))
    c.row_factory = sqlite3.Row
    c.execute(_LEGACY_RAW_NEWS)
    c.execute(_LEGACY_CHILD)
    # 模拟线上的 ALTER 补列（重建表时必须保留）
    c.execute("ALTER TABLE raw_news ADD COLUMN net_origin_info TEXT DEFAULT ''")
    c.execute("ALTER TABLE raw_news ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    c.execute("CREATE INDEX idx_raw_news_status ON raw_news(status)")
    c.execute("PRAGMA foreign_keys=ON")
    for i in (1, 2, 3):
        c.execute(
            "INSERT INTO raw_news (source, content, status) VALUES (?,?,?)",
            ("src", f"news-{i}", "PENDING"))
        c.execute("INSERT INTO ai_decisions (news_id) VALUES (?)", (i,))
    c.commit()
    yield c
    c.close()


def test_migration_widens_check(legacy_conn):
    """老库跑完迁移 → 允许写 'FILTERED'。"""
    db._upgrade_raw_news_status_check(legacy_conn)
    sql = legacy_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_news'"
    ).fetchone()[0]
    assert "'FILTERED'" in sql
    legacy_conn.execute("UPDATE raw_news SET status='FILTERED' WHERE id=1")
    legacy_conn.commit()
    got = legacy_conn.execute("SELECT status FROM raw_news WHERE id=1").fetchone()
    assert got["status"] == "FILTERED"


def test_migration_keeps_rows_columns_index(legacy_conn):
    """迁移不得丢行、丢列、丢索引。"""
    db._upgrade_raw_news_status_check(legacy_conn)
    n = legacy_conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    assert n == 3, "行数必须不变"
    cols = {r[1] for r in legacy_conn.execute("PRAGMA table_info(raw_news)")}
    assert "net_origin_info" in cols, "ALTER 补出来的列不能丢"
    assert "retry_count" in cols, "ALTER 补出来的列不能丢"
    idx = legacy_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='raw_news' AND name='idx_raw_news_status'"
    ).fetchone()
    assert idx is not None, "索引必须在重建后补回来"


def test_migration_does_not_cascade_delete_children(legacy_conn):
    """⚠️ ai_decisions 有 ON DELETE CASCADE —— 重建表时绝不能删掉决策记录。

    触发条件：db.get_connection() 里 PRAGMA foreign_keys=ON，
    而 DROP TABLE 旧表会触发隐式 DELETE FROM → 级联删子表行。
    """
    before = legacy_conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    assert before == 3
    db._upgrade_raw_news_status_check(legacy_conn)
    after = legacy_conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    assert after == before, (
        "重建 raw_news 把 ai_decisions 级联删光了 —— 迁移必须先关 foreign_keys")


def test_migration_keeps_foreign_key_pointing_at_raw_news(legacy_conn):
    """外键引用不能被 ALTER TABLE RENAME 改写到临时表名。"""
    db._upgrade_raw_news_status_check(legacy_conn)
    sql = legacy_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_decisions'"
    ).fetchone()[0]
    assert "REFERENCES raw_news(id)" in sql, (
        "外键引用被改写了 —— 必须用 legacy_alter_table=ON")
    assert "raw_news__legacy" not in sql
    # 外键仍然有效：新插入的决策行不被拒
    legacy_conn.execute("INSERT INTO ai_decisions (news_id) VALUES (1)")
    legacy_conn.commit()


def test_migration_is_idempotent(legacy_conn):
    """重复跑不得炸、不得丢数据。"""
    db._upgrade_raw_news_status_check(legacy_conn)
    n1 = legacy_conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    db._upgrade_raw_news_status_check(legacy_conn)
    n2 = legacy_conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
    assert n1 == n2 == 3
