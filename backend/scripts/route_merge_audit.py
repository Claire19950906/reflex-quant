#!/usr/bin/env python3
"""route_merge_audit.py — 双 server 路由重复的「分叉程度」分诊

architecture_baseline.py 只回答「有多少条重复」；收敛时要回答的是「哪一条能删、
哪一条删了就换口径」。本脚本把每条重复路由的两份实现都取出来做结构比对，
给出相似度、行数、函数名，作为合并决策的证据。

只读：只解析源码，不 import、不连库、不发请求。

用法：
  python3 backend/scripts/route_merge_audit.py
  python3 backend/scripts/route_merge_audit.py --json > /tmp/dups.json
  python3 backend/scripts/route_merge_audit.py --min-similarity 0.85   # 只看可安全合并的

判据（写死在这里，避免口径漂移）：
  * 只比对 @app.<method>("<字面路径>")；参数化路径按字面保留。
  * 相似度 = difflib 比率（去注释、压空白后），≥0.9 视为「同一实现的影子副本」，
    0.5~0.9 视为「同源已分叉」，<0.5 视为「两套独立逻辑」。
  * 本工具不判断谁对 —— 口径由人定，工具只负责把差异摆出来。
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import sys
from typing import Any, Dict, List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "backend", "src_python")
SERVERS = {
    "api_server": os.path.join(SRC, "api_server.py"),
    "bridge_server": os.path.join(SRC, "bridge_server.py"),
}
HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def _route_of(dec: ast.AST) -> tuple | None:
    """@app.get("/x") → ("GET", "/x")；认不出的返回 None。"""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    if dec.func.attr.lower() not in HTTP_METHODS:
        return None
    if not isinstance(dec.func.value, ast.Name) or dec.func.value.id not in {"app", "application"}:
        return None
    if not dec.args or not isinstance(dec.args[0], ast.Constant):
        return None
    if not isinstance(dec.args[0].value, str):
        return None
    return dec.func.attr.upper(), dec.args[0].value


def collect(path: str) -> Dict[tuple, Dict[str, Any]]:
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    out: Dict[tuple, Dict[str, Any]] = {}
    for node in ast.walk(ast.parse(src, filename=path)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            key = _route_of(dec)
            if key is None:
                continue
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            body = re.sub(r"#.*", "", body)
            body = re.sub(r"\s+", " ", body).strip()
            out[key] = {
                "func": node.name,
                "line": node.lineno,
                "lines": node.end_lineno - node.lineno + 1,
                "code": body,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            }
    return out


def audit(min_similarity: float = 0.0) -> List[Dict[str, Any]]:
    impls = {name: collect(p) for name, p in SERVERS.items() if os.path.exists(p)}
    api, br = impls.get("api_server", {}), impls.get("bridge_server", {})
    rows: List[Dict[str, Any]] = []
    for key in sorted(set(api) & set(br)):
        a, b = api[key], br[key]
        if a["code"] == b["code"]:
            bucket = "identical"
        else:
            ratio = difflib.SequenceMatcher(None, a["code"], b["code"]).ratio()
            bucket = "shadow" if ratio >= 0.9 else ("forked" if ratio >= 0.5 else "divergent")
        row = {
            "method": key[0],
            "path": key[1],
            "similarity": 1.0 if a["code"] == b["code"] else round(
                difflib.SequenceMatcher(None, a["code"], b["code"]).ratio(), 3),
            "bucket": bucket,
            "api_server": {"func": a["func"], "line": a["line"], "lines": a["lines"]},
            "bridge_server": {"func": b["func"], "line": b["line"], "lines": b["lines"]},
        }
        if row["similarity"] >= min_similarity:
            rows.append(row)
    rows.sort(key=lambda r: -r["similarity"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="双 server 重复路由分诊")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--min-similarity", type=float, default=0.0)
    args = ap.parse_args()

    rows = audit(args.min_similarity)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    buckets = {"identical": 0, "shadow": 0, "forked": 0, "divergent": 0}
    for r in rows:
        buckets[r["bucket"]] += 1

    print(f"重复路由 {len(rows)} 条   "
          f"[完全一致 {buckets['identical']} / 影子(≥0.9) {buckets['shadow']} / "
          f"同源分叉(0.5~0.9) {buckets['forked']} / 两套逻辑(<0.5) {buckets['divergent']}]\n")
    print(f"{'相似度':<7}{'类型':<11}{'方法':<6}{'路径':<48}{'api_server':<32}{'bridge_server'}")
    print("-" * 140)
    for r in rows:
        a = f'{r["api_server"]["func"]} ({r["api_server"]["lines"]}行)'
        b = f'{r["bridge_server"]["func"]} ({r["bridge_server"]["lines"]}行)'
        print(f'{r["similarity"]:<7}{r["bucket"]:<11}{r["method"]:<6}{r["path"]:<48}{a:<32}{b}')
    print("\n处置：identical/shadow 可删影子副本（另一份自动接管）；"
          "forked/divergent 需先定口径再合并，勿机械删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
