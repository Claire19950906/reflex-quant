#!/usr/bin/env python3
"""architecture_baseline.py — 端点基线冻结（拆分 api_server 前的护栏）

只读脚本：不改任何业务代码、不连数据库、不发请求。

产出（默认写到 backend/reports/baseline/）：
  endpoints.json      端点全量清单（文件 / 行号 / 方法 / 路径 / 处理函数）
  duplicates.json     两个 server 之间路径重叠（S-2 合并的量化依据）
                     注：routers/ 是切片的「新家」，只计入总数，不参与重复判定
  frontend_usage.json 每个端点在 frontend 里的字面引用数（0 = 前端零引用）
  baseline.md         人读摘要

用法：
  python3 backend/scripts/architecture_baseline.py
  python3 backend/scripts/architecture_baseline.py --out /tmp/baseline

判据说明（写进 baseline.md，避免口径漂移）：
  * 只统计 @app.<method>("<literal path>")；参数化路径按字面保留，不做正则归一。
  * 两个 server 的重复判定同样按字面路径，因此「/api/x/{id}」与「/api/x/{sid}」
    不会被判为重复 —— 这是保守口径，宁可少报。
  * 前端引用判定：在 frontend 的 .ts/.tsx 里搜索路径字面量（去掉参数段里的变量名，
    保留 {…} 之前的前缀），命中即算引用；这是近似值，用于发现明显失联的端点。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "backend", "src_python")
FRONTEND = os.path.join(REPO_ROOT, "frontend")

SERVERS = {
    "api_server": os.path.join(SRC, "api_server.py"),
    "bridge_server": os.path.join(SRC, "bridge_server.py"),
}

# 结构切片目标目录：从 api_server 搬到 routers/ 的端点仍要计入总数，
# 否则「搬走 = 消失」会把护栏的判据搞反（总数应该守恒，只有归属变化）。
ROUTERS_DIR = os.path.join(SRC, "routers")


# ---------------------------------------------------------------------------
# 1. 端点提取（AST，不用正则：正则会把注释里的示例也算进去）
# ---------------------------------------------------------------------------

def _decorator_call(node: ast.AST) -> Optional[ast.Call]:
    """@app.get(...) / @app.route(...) 这类装饰器的 Call 节点。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    # 只认 app.get / application.get 之类，避免把 router.get 漏掉
    if not isinstance(func.value, ast.Name):
        return None
    # router 也要认：结构切片后端点写在 routers/*.py 里是 @router.get(...)，
    # 不认就等于「拆出去的路由凭空消失」，护栏会给出假的下降。
    if func.value.id not in {"app", "application", "router"}:
        return None
    if func.attr.lower() not in HTTP_METHODS:
        return None
    return node


def _literal_path(call: ast.Call) -> Optional[str]:
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def extract_endpoints(path: str, server: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)

    # 记录「函数定义 → 最近的装饰器行号」
    rows: List[Dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = _decorator_call(dec)
            if call is None:
                continue
            route = _literal_path(call)
            if route is None:
                continue
            rows.append({
                "server": server,
                "file": os.path.basename(path),
                "line": dec.lineno,
                "method": call.func.attr.upper(),
                "path": route,
                "handler": node.name,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "has_params": "{" in route,
                "static_prefix": _static_prefix(route),
            })
    return rows


def _static_prefix(route: str) -> str:
    """'/api/x/{id}/y' -> '/api/x/' —— 前端字面量一般只写到第一段参数前。"""
    idx = route.find("{")
    return route if idx < 0 else route[:idx]


def _usage_keys(route: str) -> List[str]:
    """前端可能写出的几种字面形式（去重、按长度降序，避免短串先命中）。"""
    prefix = _static_prefix(route)
    candidates = {route, prefix}
    for p in (route, prefix):
        if p.startswith("/api/"):
            candidates.add(p[len("/api"):])

    # 2026-11-17: 版本号老路径与规范路径是**同一个 endpoint**（legacy_alias 注册的别名），
    # 前端已改用规范名，老路径因此看起来"零引用"——那是假信号。
    # 把规范名也当作这条路由的引用来源，避免零引用数量被版本号前缀灌水。
    m = re.match(r"^/api/v\d+/(.*)$", route)
    if m:
        canon = "/api/" + m.group(1)
        candidates.add(canon)
        candidates.add(canon[len("/api"):])
        candidates.add(_static_prefix(canon))
    # 去掉尾部 '{' 残留与重复斜杠
    cleaned = {c.rstrip("/{") for c in candidates if len(c) > 6}
    return sorted(cleaned, key=len, reverse=True)


# ---------------------------------------------------------------------------
# 2. 前端引用（近似）
# ---------------------------------------------------------------------------

def load_frontend_text() -> str:
    chunks: List[str] = []
    for root, dirs, files in os.walk(FRONTEND):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".next", ".next-prod", "dist"}]
        for name in files:
            if name.endswith((".ts", ".tsx", ".js", ".mjs")):
                try:
                    with open(os.path.join(root, name), "r", encoding="utf-8") as f:
                        chunks.append(f.read())
                except (UnicodeDecodeError, OSError):
                    continue
    return "\n".join(chunks)


def _count_refs(text: str, keys: List[str]) -> int:
    """按"路径边界"计数，避免短路径把别的路径算进来。

    实测坑：`/stats` 用 text.count 会把 `/decision/stats`、`/market/stats` 全算成它的
    引用（子串命中），零引用统计因此整体偏低；正则加了前后边界（前面不许接
    字母/数字/斜杠、后面不许接字母/数字/连字符）之后只认真实调用点。
    """
    n = 0
    for k in keys:
        n += len(re.findall(r"(?<![\w/-])" + re.escape(k) + r"(?![\w-])", text))
    return n


# ---------------------------------------------------------------------------
# 3. 汇总
# ---------------------------------------------------------------------------

def build(out_dir: str) -> Dict[str, Any]:
    endpoints: List[Dict[str, Any]] = []
    for server, path in SERVERS.items():
        if not os.path.exists(path):
            print(f"[WARN] 缺文件: {path}", file=sys.stderr)
            continue
        endpoints.extend(extract_endpoints(path, server))

    if os.path.isdir(ROUTERS_DIR):
        for name in sorted(os.listdir(ROUTERS_DIR)):
            if not name.endswith(".py") or name.startswith("_"):
                continue
            endpoints.extend(
                extract_endpoints(os.path.join(ROUTERS_DIR, name), "routers")
            )

    # 重复：同 method + 同字面 path 出现在两个 server
    by_key: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in endpoints:
        if row["server"] == "routers":   # routers/ 是归属地，不是第二个实现
            continue
        by_key[(row["method"], row["path"])].append(row)
    duplicates = [
        {"method": k[0], "path": k[1], "servers": [r["server"] for r in v],
         "handlers": [f'{r["file"]}:{r["line"]} {r["handler"]}' for r in v]}
        for k, v in by_key.items() if len({r["server"] for r in v}) > 1
    ]

    fe_text = load_frontend_text()
    usage: List[Dict[str, Any]] = []
    for row in endpoints:
        # 前端有两种写法：`${API_BASE}/market/movers`（去掉 /api）与 `/api/market/movers`。
        # 两种都要算，否则会把绝大多数在用端点误判成"零引用"。
        keys = _usage_keys(row["path"])
        hits = _count_refs(fe_text, keys)
        usage.append({
            "server": row["server"], "method": row["method"], "path": row["path"],
            "handler": row["handler"], "frontend_refs": hits,
        })

    by_server = Counter(r["server"] for r in endpoints)
    by_method = Counter(r["method"] for r in endpoints)
    async_ratio = {}
    for server in by_server:
        rows = [r for r in endpoints if r["server"] == server]
        async_ratio[server] = round(sum(1 for r in rows if r["is_async"]) / max(1, len(rows), ) * 100, 1)

    # 2026-11-18: catch-all（/api/{full_path:path}）不是"没人调用的端点"，它就是兜底路由，
    # 算进零引用等于凭空加 2 条。蜜罐路径（下划线开头）同理:它们是故意不让人正常调的。
    zero_ref = [
        u for u in usage
        if u["frontend_refs"] == 0
        and "{full_path" not in u["path"]
        and not re.search(r"/_[a-z]", u["path"])
    ]

    os.makedirs(out_dir, exist_ok=True)
    _dump(os.path.join(out_dir, "endpoints.json"), endpoints)
    _dump(os.path.join(out_dir, "duplicates.json"), duplicates)
    _dump(os.path.join(out_dir, "frontend_usage.json"), usage)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(endpoints),
        "by_server": dict(by_server),
        "by_method": dict(by_method),
        "async_ratio_pct": async_ratio,
        "duplicate_count": len(duplicates),
        "frontend_zero_ref_count": len(zero_ref),
    }

    md = [
        "# 端点基线（自动生成，勿手改）",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        f"- 端点总数：**{summary['total']}**",
        f"- 分布：{', '.join(f'{k} {v}' for k, v in by_server.items())}",
        f"- 方法：{', '.join(f'{k} {v}' for k, v in by_method.items())}",
        f"- async 占比：{', '.join(f'{k} {v}%' for k, v in async_ratio.items())}",
        f"- 两个 server 字面重复：**{len(duplicates)}**",
        f"- 前端零引用：**{len(zero_ref)}**",
        "",
        "## 字面重复（同 method + 同 path）",
        "",
    ]
    for d in duplicates[:60]:
        md.append(f"- `{d['method']} {d['path']}` — {', '.join(d['servers'])}")
    if len(duplicates) > 60:
        md.append(f"- …另有 {len(duplicates) - 60} 条，见 duplicates.json")
    md += ["", "## 前端零引用（前 60 条）", ""]
    for u in zero_ref[:60]:
        md.append(f"- `{u['method']} {u['path']}` — {u['server']} / {u['handler']}")
    if len(zero_ref) > 60:
        md.append(f"- …另有 {len(zero_ref) - 60} 条，见 frontend_usage.json")

    with open(os.path.join(out_dir, "baseline.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    _dump(os.path.join(out_dir, "summary.json"), summary)
    return summary


def _dump(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结端点基线（只读）")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "backend", "reports", "baseline"))
    args = ap.parse_args()

    summary = build(args.out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n产出目录: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
