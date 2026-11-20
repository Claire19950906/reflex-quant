#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计：事务边界内夹网络/LLM 调用（长事务风险）。

起因（2026-11-19 工程体检）
--------------------------
SQLite 单写者：一个事务从第一条写语句到 commit 之间，整库写锁被占住。
如果这段区间里还夹着 requests/httpx/urlopen/LLM 调用或 sleep，事务时长
= 网络/LLM 耗时，其它 writer 只能排队，等不到就丢写。

本脚本用 AST 做**静态**检测（不运行、不改业务逻辑），按函数粒度找：
  1. 函数内出现过写库调用（execute/executemany + INSERT/UPDATE/DELETE）；
  2. 同一函数内又出现网络/LLM/sleep 调用；
  3. 给出写调用与网络调用的行号顺序，判断网络是否夹在写之后、commit 之前。

判定为「风险」需要：写调用行号 < 网络调用行号（先写、后网络）。
先网络后写（先取数再一次性落库）属于可接受模式，只在 verbose 里列出。

用法：
    python3 scripts/audit_txn_boundary.py
    python3 scripts/audit_txn_boundary.py --verbose
    python3 scripts/audit_txn_boundary.py --json out.json
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "src_python")

#: 网络 / 外部 IO 调用方法名或模块名片段
# 注意：裸 .get/.post 多为 dict/Row.get 内存方法，只有挂在明确 HTTP 客户端
# 模块上才判网络，否则假阳性极高（见 v491 审计复核）。
NETWORK_CALLS = {
    "post", "put", "delete", "request", "urlopen", "urlretrieve",
    "fetch", "call_llm", "chat", "complete", "invoke", "poll",
}
NETWORK_MODULES = {"requests", "httpx", "urllib", "openai", "aiohttp",
                   "websocket", "urlopen"}
HTTP_CLIENT_MODS = ("requests", "httpx", "session", "http", "client", "s")
LLM_HINT = ("llm", "openai", "minimax", "kimi", "chat", "complete", "model")
WRITE_SQL_WORDS = ("insert", "update", "delete", "replace")

#: 这些上下文管理器工厂在 __exit__ 里 commit + close，with 块退出即释放写锁；
#: 块外的 sleep/网络不算跨事务。
CONN_CONTEXT_FACTORIES = {"db_cursor", "write_db_cursor", "write_cursor"}

_DUP_RE = __import__("re").compile(r" \d+\.py$")


def _call_name(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _call_module(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        return fn.value.id
    return ""


def _is_network(call: ast.Call) -> bool:
    nm = _call_name(call)
    mod = _call_module(call)
    if mod in NETWORK_MODULES:
        return True
    if nm in ("urlopen", "urlretrieve"):
        return True
    if nm in NETWORK_CALLS and mod in HTTP_CLIENT_MODS:
        return True
    if any(h in nm.lower() for h in LLM_HINT) and mod in (
            "openai", "client", "llm", "model", "chat"):
        return True
    return False


def _is_sleep(call: ast.Call) -> bool:
    nm = _call_name(call)
    mod = _call_module(call)
    return nm in ("sleep", "wait", "wait_for") or mod == "time" and nm == "sleep"


def _write_sql(call: ast.Call) -> Optional[str]:
    nm = _call_name(call)
    if nm not in ("execute", "executemany", "executescript"):
        return None
    for a in call.args:
        sv: Optional[str] = None
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            sv = a.value
        elif isinstance(a, ast.JoinedStr):
            sv = "".join(v.value for v in a.values
                         if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if sv and any(w in sv.lower() for w in WRITE_SQL_WORDS):
            return sv.strip().splitlines()[0][:60]
    return None


def _context_factory(with_node: ast.AST) -> str:
    """取 with 语句上下文表达式的调用名（如 db_cursor()）。"""
    if not isinstance(with_node, (ast.With, ast.AsyncWith)):
        return ""
    for item in with_node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call):
            fn = expr.func
            if isinstance(fn, ast.Name):
                return fn.id
            if isinstance(fn, ast.Attribute):
                return fn.attr
    return ""


class FuncVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.writes: List[dict] = []
        self.networks: List[dict] = []
        self.commits: List[int] = []
        self.conn_withs: List[dict] = []

    def visit_With(self, node: ast.With) -> None:
        self._record_with(node)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._record_with(node)
        self.generic_visit(node)

    def _record_with(self, node: ast.AST) -> None:
        factory = _context_factory(node)
        if factory in CONN_CONTEXT_FACTORIES:
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            self.conn_withs.append({"start": node.lineno, "end": end,
                                    "factory": factory})

    def visit_Call(self, node: ast.Call) -> None:
        sql = _write_sql(node)
        if sql:
            self.writes.append({"line": node.lineno, "sql": sql})
        elif _is_network(node):
            self.networks.append({"line": node.lineno, "name": _call_name(node),
                                  "kind": "network"})
        elif _is_sleep(node):
            self.networks.append({"line": node.lineno, "name": "sleep",
                                  "kind": "sleep"})
        elif _call_name(node) == "commit":
            self.commits.append(node.lineno)
        self.generic_visit(node)


def _io_in_conn_with(io_line: int, conn_withs: List[dict]) -> Optional[dict]:
    for w in conn_withs:
        if w["start"] <= io_line <= w["end"]:
            return w
    return None


def audit_func(node: ast.AST, rel: str, func_name: str) -> Optional[dict]:
    v = FuncVisitor()
    v.visit(node)
    if not v.writes or not v.networks:
        return None
    first_write = min(w["line"] for w in v.writes)
    net_after_write = [n for n in v.networks if n["line"] > first_write]
    if not net_after_write:
        # 先网络后写：可接受模式，跳过
        return None

    # 边界分类：
    #   real            —— IO 落在某个连接工厂 with 块内（块退出才 commit），真候选
    #   outside_context —— IO 在所有连接 with 块之外（块已退出提交），误报
    #   committed_before_io —— 写之后、IO 之前已有裸 commit，误报
    real_io, false_io = [], []
    for n in net_after_write:
        w = _io_in_conn_with(n["line"], v.conn_withs)
        if w is not None:
            real_io.append(n)
        elif any(first_write < c < n["line"] for c in v.commits):
            n["reason"] = "committed-before-io"
            false_io.append(n)
        else:
            n["reason"] = "outside-context"
            false_io.append(n)

    if not real_io:
        boundary = "false-positive"
        real: List[dict] = []
    else:
        boundary = "candidate"
        real = real_io

    return {
        "file": rel,
        "function": func_name,
        "first_write_line": first_write,
        "writes": v.writes,
        "net_after_write": net_after_write,
        "real_io": real,
        "false_io": false_io,
        "commits": v.commits,
        "conn_withs": v.conn_withs,
        "boundary": boundary,
        "risk": "write-then-io",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    findings: List[dict] = []
    for root, _dirs, files in os.walk(SRC):
        if "__pycache__" in root:
            continue
        for fn in sorted(files):
            if not fn.endswith(".py") or _DUP_RE.search(fn):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, BASE_DIR)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    f = audit_func(node, rel, node.name)
                    if f:
                        findings.append(f)

    findings.sort(key=lambda x: (x["boundary"] != "candidate",
                                x["file"], x["first_write_line"]))
    n_real = sum(1 for f in findings if f["boundary"] == "candidate")
    for f in findings:
        tag = "🔴 候选" if f["boundary"] == "candidate" else "⚪ 误报"
        real_lines = ",".join(str(n["line"]) for n in f["real_io"])
        print("%s %s :: %s()  写@%d → 块内IO@%s"
              % (tag, f["file"], f["function"], f["first_write_line"], real_lines))
        if args.verbose:
            for w in f["writes"]:
                print("      write L%-5d %s" % (w["line"], w["sql"]))
            for n in f["net_after_write"]:
                mark = "块内" if n in f["real_io"] else "块外"
                print("      io    L%-5d %s [%s]" % (n["line"], n["name"], mark))
            if f["commits"]:
                print("      commit L%s" % ",".join(map(str, f["commits"])))
    print("=" * 70)
    print("扫描完成：候选真风险 %d 个，误报 %d 个（共 %d）"
          % (n_real, len(findings) - n_real, len(findings)))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, ensure_ascii=False, indent=2)
        print("明细已写入 %s" % args.json_out)

    # 只出清单、不挡 CI：风险需人工结合事务边界确认，退出码恒 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
