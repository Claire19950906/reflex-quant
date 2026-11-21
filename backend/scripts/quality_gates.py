#!/usr/bin/env python3
"""结构质量闸门（CI 常驻，防止已修的债重新长回来）。

三个检查，任一不过 exit 1：

1. loop-blocking   路由端点若是 async def 却体内零 await、又同步读写 SQLite，
                   就会把整个事件循环卡住（一条慢查询 = 全站转圈，且不报错）。
                   2026-11-18 已全仓清零（41 处改 def），这里锁死。
                   `--allow-async-nowait` 只报阻塞、不报"async 但完全没 await"，
                   因为后者只是白搭一个协程，不会卡住别人。

2. shims           顶层兼容垫片（`sys.modules[__name__] = real`）必须为 0。
                   P3 已把 93 个垫片全删，留一个就是在暗示"import 路径还没改完"。

3. alias-honeypot  版本号路径别名层不允许给"下划线子路径"（蜜罐）生成别名，
                   否则蜜罐会被规范名暴露出来。

用法:
    python backend/scripts/quality_gates.py                     # 全跑
    python backend/scripts/quality_gates.py --only loop-blocking
    python backend/scripts/quality_gates.py --json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
SRC = ROOT / 'backend' / 'src_python'

ROUTE_DECOS = ('.get(', '.post(', '.put(', '.delete(', '.patch(', '.websocket(')
DB_HINTS = ('sqlite3', 'db_cursor', 'conn.execute', 'cursor.execute', 'db.query',
            'db.get_connection', 'read_sql', 'to_sql')


def _iter_py(base: Path) -> List[Path]:
    return sorted(p for p in base.rglob('*.py') if '__pycache__' not in p.parts)


def _deco_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ''


def _is_route(node: ast.AST) -> bool:
    decos = getattr(node, 'decorator_list', []) or []
    return any(any(k in _deco_text(d) for k in ROUTE_DECOS) for d in decos)


def _read_text(path: Path, errors: str = 'strict') -> str:
    """按原样读源码，保留行尾（行号才对得上）。

    不用 Path.read_text(newline=...)：`newline` 是 Python 3.13 才加的参数，
    3.11 上直接 TypeError —— CI 就是被这一行打红的，本机 3.14 却全绿。
    """
    with path.open('r', encoding='utf-8', newline='', errors=errors) as fh:
        return fh.read()


def _has_await(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            return True
    return False


def check_loop_blocking() -> List[Dict[str, Any]]:
    """async 路由端点体内零 await 且同步操作 SQLite → 阻塞事件循环。"""
    bad: List[Dict[str, Any]] = []
    for path in _iter_py(SRC):
        try:
            tree = ast.parse(_read_text(path))
        except SyntaxError as e:
            bad.append({'check': 'loop-blocking', 'file': str(path.relative_to(ROOT)),
                        'func': '<syntax>', 'line': e.lineno, 'reason': f'SyntaxError: {e.msg}'})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route(node):
                continue
            if _has_await(node):
                continue
            body = ast.unparse(node)
            hits = [h for h in DB_HINTS if h in body]
            if hits:
                bad.append({'check': 'loop-blocking',
                            'file': str(path.relative_to(ROOT)),
                            'func': node.name, 'line': node.lineno,
                            'reason': 'async 但零 await，且同步访问 ' + ','.join(sorted(hits))})
    return bad


def check_shims() -> List[Dict[str, Any]]:
    """顶层兼容垫片必须为 0。"""
    bad: List[Dict[str, Any]] = []
    for path in sorted(SRC.glob('*.py')):
        txt = _read_text(path, errors='ignore')
        if 'sys.modules[__name__] =' in txt:
            bad.append({'check': 'shims', 'file': str(path.relative_to(ROOT)),
                        'func': '<module>', 'line': 0,
                        'reason': '残留兼容垫片 sys.modules[__name__] = ...'})
    return bad


def check_alias_honeypot() -> List[Dict[str, Any]]:
    """别名层不得给下划线子路径（蜜罐）建规范名。"""
    alias = SRC / 'legacy_alias.py'
    if not alias.exists():
        return [{'check': 'alias-honeypot', 'file': 'backend/src_python/legacy_alias.py',
                 'func': '<module>', 'line': 0, 'reason': '别名层文件不存在'}]
    txt = _read_text(alias, errors='ignore')
    # 允许出现（因为要跳过它），但必须出现"跳过"判断；缺了就是真放行
    if 'startswith' not in txt or '_' not in txt:
        return [{'check': 'alias-honeypot', 'file': 'backend/src_python/legacy_alias.py',
                 'func': '<module>', 'line': 0,
                 'reason': '未看到"下划线子路径跳过"的判断'}]
    return []


# 3.13 才加、3.11 上会 TypeError 的参数。CI 跑 3.11，本机可能是 3.14，
# 这类差异在本机永远跑不出来，只能靠这条检查提前拦住。
PY313_ONLY_KWARGS = {'read_text': {'newline'}, 'write_text': {'newline'}}


def check_py311_compat() -> List[Dict[str, Any]]:
    """只能用 3.11 就有的 API：挡掉 3.13+ 才加的关键字参数。"""
    bad: List[Dict[str, Any]] = []
    base_list = [SRC, ROOT / 'backend' / 'scripts']
    for base in base_list:
        for path in _iter_py(base):
            try:
                tree = ast.parse(_read_text(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                allowed = PY313_ONLY_KWARGS.get(node.func.attr)
                if not allowed:
                    continue
                for kw in node.keywords:
                    if kw.arg in allowed:
                        bad.append({'check': 'py311-compat',
                                    'file': str(path.relative_to(ROOT)),
                                    'func': node.func.attr, 'line': node.lineno,
                                    'reason': f'{node.func.attr}({kw.arg}=...) 是 3.13+ 参数，CI 的 3.11 会 TypeError'})
    return bad


CHECKS = {
    'loop-blocking': check_loop_blocking,
    'shims': check_shims,
    'alias-honeypot': check_alias_honeypot,
    'py311-compat': check_py311_compat,
}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='结构质量闸门')
    ap.add_argument('--only', action='append', choices=sorted(CHECKS),
                    help='只跑指定检查（可多次）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args(argv)

    selected = args.only or list(CHECKS)
    results: Dict[str, List[Dict[str, Any]]] = {}
    for name in selected:
        results[name] = CHECKS[name]()

    total = sum(len(v) for v in results.values())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for name, items in results.items():
            if items:
                print(f'[{name}] ✗ {len(items)} 处')
                for it in items[:20]:
                    print(f'   {it["file"]}:{it.get("line")}  {it["func"]}  {it["reason"]}')
                if len(items) > 20:
                    print(f'   ... 另有 {len(items) - 20} 处')
            else:
                print(f'[{name}] ✓ 0 处')
    # v505：失败时再吐一遍 GitHub 注解（::error file=...,line=...::原因）。
    # 这样即使拿不到 Actions 完整日志，check-run 的 annotations 里也能读到具体是哪一处。
    if total and not args.json:
        for name, items in results.items():
            for it in items:
                print(f'::error file={it["file"]},line={it.get("line") or 1},'
                      f'title={name}::{it["reason"]}')
    return 0 if total == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
