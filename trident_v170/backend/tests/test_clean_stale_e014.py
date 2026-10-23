#!/usr/bin/env python3
"""test_clean_stale_e014.py — 测试历史 E014 误报清理工具

2026-10-08: 当 reflection_checkers._extract_reasoning_steps 修复后，
历史 132 笔误报 E014 需要清理。本测试验证 clean_stale_e014.py 的核心逻辑。

不依赖生产 DB：用一个 in-memory SQLite + 临时 ai_decisions 样本。
"""
import os
import sys
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BACKEND, "src_python"))
sys.path.insert(0, os.path.join(BACKEND, "tools"))

import reflection_checkers as rc
import clean_stale_e014 as cse


VALID_8_STEPS = (
    "[总框架] A [因素定位] B [驱动力] C [水位博弈] D "
    "[跨资产资金] E [反共识结论] F [逻辑自检] G [总结] H"
)
VALID_2_STEPS = "[总框架] A [因素定位] B"
EMPTY_RP = ""


def _make_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reasoning_path TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE decision_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            error_code TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'checker',
            severity INTEGER NOT NULL DEFAULT 2,
            detail TEXT NOT NULL DEFAULT '',
            evidence_refs TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            weight INTEGER NOT NULL DEFAULT 0,
            UNIQUE(decision_id, error_code, origin, source)
        )
    """)
    conn.commit()
    return conn, path


def _seed(conn, n_full: int, n_partial: int, n_empty: int):
    full_ids, partial_ids, empty_ids = [], [], []
    for _ in range(n_full):
        cur = conn.execute(
            "INSERT INTO ai_decisions (reasoning_path) VALUES (?)",
            (VALID_8_STEPS,),
        )
        did = cur.lastrowid
        full_ids.append(did)
        conn.execute(
            "INSERT OR IGNORE INTO decision_errors "
            "(decision_id, error_code, origin, source) "
            "VALUES (?, 'E014', 'checker', 'event_logic')",
            (did,),
        )
    for _ in range(n_partial):
        cur = conn.execute(
            "INSERT INTO ai_decisions (reasoning_path) VALUES (?)",
            (VALID_2_STEPS,),
        )
        did = cur.lastrowid
        partial_ids.append(did)
        conn.execute(
            "INSERT OR IGNORE INTO decision_errors "
            "(decision_id, error_code, origin, source) "
            "VALUES (?, 'E014', 'checker', 'event_logic')",
            (did,),
        )
    for _ in range(n_empty):
        cur = conn.execute(
            "INSERT INTO ai_decisions (reasoning_path) VALUES (?)",
            (EMPTY_RP,),
        )
        did = cur.lastrowid
        empty_ids.append(did)
        conn.execute(
            "INSERT OR IGNORE INTO decision_errors "
            "(decision_id, error_code, origin, source) "
            "VALUES (?, 'E014', 'checker', 'event_logic')",
            (did,),
        )
    conn.commit()
    return full_ids, partial_ids, empty_ids


def test_extract_reasoning_steps_finds_all_8():
    """核心：_extract_reasoning_steps 修复后能识别 8 段（不是只识别 1 段）。"""
    steps = rc._extract_reasoning_steps(VALID_8_STEPS)
    assert len(steps) == 8, f"期望 8 段，实际 {len(steps)}: {steps}"
    for expected in ("总框架", "因素定位", "驱动力", "水位博弈",
                     "跨资产资金", "反共识结论", "逻辑自检", "总结"):
        assert expected in steps, f"缺失段名: {expected}"


def test_classify_decisions_separates_fp_tp_empty():
    """_classify_decisions 应该把 8段/2段/空 正确分类。"""
    conn, path = _make_temp_db()
    try:
        full_ids, partial_ids, empty_ids = _seed(
            conn, n_full=3, n_partial=2, n_empty=1)
        cls = cse._classify_decisions(conn)
        assert len(cls["false_positive"]) == 3
        assert set(cls["false_positive"]) == set(full_ids)
        assert len(cls["true_positive"]) == 2
        assert set(cls["true_positive"]) == set(partial_ids)
        assert len(cls["empty"]) == 1
        assert set(cls["empty"]) == set(empty_ids)
    finally:
        os.unlink(path)


def test_delete_errors_only_removes_fp():
    """_delete_errors 应该只删除 false positive 的 E014 记录。"""
    conn, path = _make_temp_db()
    try:
        full_ids, partial_ids, empty_ids = _seed(
            conn, n_full=5, n_partial=2, n_empty=1)
        before_total = conn.execute(
            "SELECT COUNT(*) FROM decision_errors WHERE error_code='E014'"
        ).fetchone()[0]
        assert before_total == 8

        deleted = cse._delete_errors(conn, full_ids)
        assert deleted == 5

        after = conn.execute(
            "SELECT decision_id FROM decision_errors WHERE error_code='E014'"
        ).fetchall()
        remaining_ids = {r[0] for r in after}
        assert remaining_ids == set(partial_ids) | set(empty_ids)
    finally:
        os.unlink(path)


def test_count_errors_to_delete_accurate():
    conn, path = _make_temp_db()
    try:
        full_ids, _, _ = _seed(conn, n_full=7, n_partial=0, n_empty=0)
        n = cse._count_errors_to_delete(conn, full_ids)
        assert n == 7
    finally:
        os.unlink(path)


def test_empty_decision_ids_handles_gracefully():
    conn, path = _make_temp_db()
    try:
        n = cse._count_errors_to_delete(conn, [])
        assert n == 0
        deleted = cse._delete_errors(conn, [])
        assert deleted == 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failures.append((name, e))
                print(f"  FAIL  {name}: {e}")
    if failures:
        print(f"\n{len(failures)} tests failed")
        sys.exit(1)
    print(f"\nAll tests passed")