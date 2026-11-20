#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v504：api_server 模块级常量不许"只用不定义"。

真实事故（2026-11-21）：时间口径切片把 `_EVENT_SELECT` 和整组
`_EVENTS_15D_*` 常量连同一个函数一起搬走了，结果 `/api/events` 一调用就
`AttributeError`、事件缓存 `NameError`。全量套件里只有 12 条用例探头，
而且报的是"AttributeError: _EVENT_SELECT"——看不出根因。

这条守卫的判据很朴素：凡是形如 `_UPPER_NAME` 的模块级常量，
只要在函数体/模块体里被**读**过，就必须在本模块里被**赋值**或**导入**。
不实现这套"搬代码顺手删常量"的回归。
"""
import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
API = BACKEND / "src_python" / "api_server.py"

CONST_RE = re.compile(r"^_?[A-Z][A-Z0-9_]{1,}$")


def _defined_names(tree: ast.Module) -> set[str]:
    """本模块里**任何位置**被赋过值/导入过/定义过的名字。

    故意放宽到全树：很多常量是在 try/except 或函数内 `from x import *` 之外
    的分支里绑定的（例如 `from api_format import (...)` 在模块中段）。
    这条守卫要抓的是"彻底没有任何定义"，不是作用域精度。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
            continue
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        # 顶层 class / def 也算定义
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    # 函数/分支内的绑定（含 for 目标、with as、异常名、参数）也算"有定义"
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
            for a in node.args.args + node.args.kwonlyargs:
                out.add(a.arg)
        elif isinstance(node, ast.ClassDef):
            out.add(node.name)
        elif isinstance(node, (ast.withitem,)):
            continue
    return out


def _loaded_names(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.add(node.id)
    return out


def test_no_upper_constant_is_used_without_definition():
    src = API.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(API))
    used = {n for n in _loaded_names(tree) if CONST_RE.match(n)}
    defined = _defined_names(tree)
    missing = sorted(n for n in used if n not in defined)
    # 允许：内置名、从 star-import 里来的（本仓库不用 star import，若出现请显式列出）
    allowed: set[str] = set()
    assert not (set(missing) - allowed), (
        "这些模块级常量被读但没定义（典型原因：搬代码时把常量块一起删了）: "
        f"{sorted(set(missing) - allowed)}")


def test_event_select_constant_exists_and_is_sql():
    src = API.read_text(encoding="utf-8")
    assert re.search(r"^_EVENT_SELECT\s*=", src, re.M), "事件查询主 SELECT 常量丢了 → /api/events 必崩"
    # 用 AST 取常量本身（89 行，字符串切 2000 字会被截断）
    tree = ast.parse(src, filename=str(API))
    value = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_EVENT_SELECT" for t in node.targets):
            value = node.value
    assert isinstance(value, ast.Constant) and "FROM raw_news" in value.value, \
        "常量内容不对，不像是事件查询"


def test_events_15d_cache_constants_exist():
    src = API.read_text(encoding="utf-8")
    for name in ("_EVENTS_15D_LOCK", "_EVENTS_15D_CACHE", "_EVENTS_15D_LAST_LOAD",
                 "_EVENTS_15D_TTL_S", "_EVENTS_15D_WINDOW_DAYS", "_EVENTS_15D_WINDOW_S",
                 "_EVENTS_15D_RELOAD_LOCK", "_EVENTS_15D_WAIT_S"):
        assert re.search(rf"^{name}\s*(:\s*[^=]+)?=", src, re.M), \
            f"{name} 丢了 → 事件缓存加载会 NameError"


def test_time_helpers_come_from_api_time_not_redefined():
    src = API.read_text(encoding="utf-8")
    for fn in ("_utc_ts_to_iso", "_utc_ts_to_space_iso", "_now_iso", "_age_hours_of"):
        assert f"def {fn}(" not in src, f"{fn} 仍在 api_server 里定义 → 会出现两份实现"
    assert "from api_time import" in src
