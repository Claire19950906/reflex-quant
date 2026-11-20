# -*- coding: utf-8 -*-
"""v421 仓库完整性守卫：把"整仓 import 被改坏"这类事故变成一条会红的测试。

为什么要有这个文件（2026-11-17 事故）：
    某个批量改写脚本把 600+ 文件、3599 处 `import X` 改成了
    `from signals import X`（signals 是信号域包，拿不出 os/json/db/urllib）。
    结果：整个 backend 无法 import，`from signals import urllib.request` 连语法
    都不合法；线上服务还活着，只是因为进程在 08:27 就把旧代码载进内存了 ——
    一旦重启，全站直接起不来。而**没有任何一条测试会因为这件事变红**。

所以这里用三条断言补上缺口：
    1. 不许出现"signals 包拿不出"的 `from signals import X`（跑修复脚本的同一判据）；
    2. 全仓 .py 必须能 ast.parse（语法级错误一条都不许有）；
    3. api_server 能 import、openapi 能生成、同一 (方法,路径) 没有被注册两次
       （schema 前向引用类错误、路由静默覆盖都会在这里炸）。
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src_python"))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".next", ".pytest_cache"}

sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(REPO, "backend", "scripts"))

from fix_signals_imports import PKG_DIR, fix_text, domain_modules, walk


def test_no_broken_signals_imports():
    dom = domain_modules()
    offenders = []
    for path in walk():
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if "from signals import" not in text:
            continue
        _new, n = fix_text(text, dom)
        if n:
            offenders.append(f"{os.path.relpath(path, REPO)} ×{n}")
    assert not offenders, (
        f"{len(offenders)} 个文件的 import 被改成 signals 包拿不出的形态，"
        f"跑 `python3 backend/scripts/fix_signals_imports.py --apply` 修：\n"
        + "\n".join(offenders[:10])
    )


def test_all_python_files_parse():
    bad = []
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(base, fn)
            try:
                ast.parse(open(p, encoding="utf-8").read(), filename=p)
            except SyntaxError as exc:
                bad.append(f"{os.path.relpath(p, REPO)}:{exc.lineno} {exc.msg}")
    assert not bad, f"{len(bad)} 个文件语法不合法：\n" + "\n".join(bad[:10])


def test_openapi_generates_and_no_duplicate_routes():
    import api_server
    from collections import Counter

    schema = api_server.app.openapi()  # 前向引用/模型定义在函数体内 → 这里会抛
    assert len(schema["paths"]) > 100
    c = Counter()
    for r in api_server.app.routes:
        if hasattr(r, "methods") and hasattr(r, "path"):
            for m in (r.methods or []):
                c[(m, r.path)] += 1
    dups = [f"{m} {p} ×{n}" for (m, p), n in c.items() if n > 1]
    assert not dups, "同一 (方法,路径) 被注册多次（后者会静默覆盖前者）：\n" + "\n".join(dups[:8])


def test_domain_package_dir_exists():
    # 防止 signals 被判成"空壳"后有人把它删掉，导致上面判据全部失效
    assert os.path.isdir(PKG_DIR) and os.path.exists(os.path.join(PKG_DIR, "__init__.py"))
