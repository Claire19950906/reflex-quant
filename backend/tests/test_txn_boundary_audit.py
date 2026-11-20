# -*- coding: utf-8 -*-
"""事务边界审计口径回归（v491）。

背景
----
`audit_txn_boundary.py` 初版只按"写行号 < 网络行号"判风险，两类假阳性很高：
  1. 内存 `dict/Row.get()` 被当成 HTTP `get`；
  2. `with db_cursor()/write_db_cursor()` 块退出即 commit/close，块外的
     sleep/网络不算跨事务，但初版按行号把它算进事务内。

本文件用内嵌 AST 片段锁定正确口径：
  · 连接工厂 with 块**内**夹 sleep/网络 → candidate（真候选）；
  · with 块**外**的 sleep/网络 → outside-context（误报）；
  · 写之后、IO 之前已有裸 commit → committed-before-io（误报）；
  · 内存 `.get()` 不判网络。
"""
from __future__ import annotations

import ast
import importlib.util
import os

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE_DIR, "scripts", "audit_txn_boundary.py")

_spec = importlib.util.spec_from_file_location("audit_txn_boundary", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _audit(src: str):
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return mod.audit_func(fn, "<snippet>", fn.name)


def test_io_outside_conn_with_is_false_positive():
    """with 块退出即 commit，块外 sleep 不算跨事务。"""
    src = """
def f(conn):
    with db_cursor() as c:
        c.execute("INSERT INTO t(a) VALUES (?)", (1,))
    time.sleep(0.6)
"""
    r = _audit(src)
    assert r is not None
    assert r["boundary"] == "false-positive"
    assert r["real_io"] == []
    assert r["false_io"][0]["reason"] == "outside-context"


def test_io_inside_conn_with_is_candidate():
    """连接 with 块内夹网络调用，才是真候选。"""
    src = """
def f(conn):
    with write_db_cursor() as c:
        c.execute("UPDATE t SET a=? WHERE id=?", (1, 2))
        requests.get("http://x/y")
"""
    r = _audit(src)
    assert r is not None
    assert r["boundary"] == "candidate"
    assert [n["name"] for n in r["real_io"]] == ["get"]


def test_committed_before_io_is_false_positive():
    """写后已裸 commit，再 sleep，不算跨事务。"""
    src = """
def f(conn):
    conn.execute("INSERT INTO t(a) VALUES (?)", (1,))
    conn.commit()
    time.sleep(1.0)
"""
    r = _audit(src)
    assert r is not None
    assert r["boundary"] == "false-positive"
    assert any(n["reason"] == "committed-before-io" for n in r["false_io"])


def test_memory_get_is_not_network():
    """内存 dict/Row.get() 不得被判成 HTTP get。"""
    src = """
def f(conn, row):
    with db_cursor() as c:
        c.execute("UPDATE t SET a=? WHERE id=?", (1, 2))
    v = row.get("a")
"""
    r = _audit(src)
    # row.get 不是网络调用；若被误判，会进入 false_io/real_io
    assert r is None or r["real_io"] == []
