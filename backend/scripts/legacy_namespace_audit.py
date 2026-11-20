#!/usr/bin/env python3
"""legacy_namespace_audit.py — 版本号命名空间冻结 + 真重复审计

背景（P3 命名治理）：
  `api_server.py` 的历史把「版本号」当命名空间用 —— /api/v186、/api/v188、/api/v189…
  同一个概念在不同版本号下各有一份路径。这类前缀是**历史包袱**，不是设计：
  * 前端要按版本号记路径，换一版就得改调用点；
  * 文档里说不出「v188/indicators」和「/api/indicators」到底哪个是权威；
  * 新人不敢删，因为不知道谁还在调。

本脚本不删除任何东西，只做两件事：
  1. **冻结**：把当前的版本号前缀与条数落盘成基线。以后谁再新增一个版本号前缀、
     或往老前缀里加端点，CI 直接红 —— 「新增代码不许再开版本号」。
  2. **判重**：按「路径 + HTTP 方法」找真重复（只按路径会把 GET/POST、
     PUT/DELETE 误报成重复，实测 13 条里 12 条是这种误报）。

口径（写死，避免漂移）：
  * 版本号前缀 = 路径匹配 `^/api/v<数字>/`。
  * 重复 = 同一 (METHOD, path) 在最终路由表里出现 2 次以上。
    同一路径不同方法（GET+POST）是**正常设计**，不算重复。
  * 只解析源码 AST，不 import、不连库 —— 保证在 CI 里能跑。

用法：
  python3 backend/scripts/legacy_namespace_audit.py                 # 审计，超标 exit 1
  python3 backend/scripts/legacy_namespace_audit.py --write         # 重建基线（慎用）
  python3 backend/scripts/legacy_namespace_audit.py --json          # 机器可读
  python3 backend/scripts/legacy_namespace_audit.py --aliases       # 版本号老路径 → 规范路径映射
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "backend", "src_python")
BASELINE = os.path.join(REPO_ROOT, "backend", "reports", "baseline", "legacy_namespaces.json")
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
VERSION_RE = re.compile(r"^/api/(v\d+)/")


def _route_of(dec: ast.AST) -> Optional[Tuple[str, str]]:
    """@app.get("/x") / @router.post("/x") → ("GET", "/x")。"""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name) or func.value.id not in {"app", "application", "router"}:
        return None
    if func.attr.lower() not in HTTP_METHODS:
        return None
    if not dec.args:
        return None
    arg = dec.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return func.attr.upper(), arg.value
    return None


def _files() -> List[str]:
    out = []
    for name in ("api_server.py", "bridge_server.py"):
        p = os.path.join(SRC, name)
        if os.path.exists(p):
            out.append(p)
    routers = os.path.join(SRC, "routers")
    if os.path.isdir(routers):
        for name in sorted(os.listdir(routers)):
            if name.endswith(".py") and not name.startswith("_"):
                out.append(os.path.join(routers, name))
    return out


def collect() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in _files():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except SyntaxError as exc:  # 语法坏了要立刻可见，不能静默跳过
            print(f"[ERROR] 解析失败 {path}: {exc}", file=sys.stderr)
            raise
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                r = _route_of(dec)
                if not r:
                    continue
                rows.append({
                    "method": r[0], "path": r[1],
                    "file": os.path.relpath(path, REPO_ROOT),
                    "line": dec.lineno, "handler": node.name,
                })
    return rows


def _app_of(file: str) -> str:
    """8000 = api_server + routers/；8100 = bridge_server。"""
    return "bridge" if file.endswith("bridge_server.py") else "api"


def alias_map(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """版本号老路径 → 规范路径的静态映射（运行期由 legacy_alias.py 真正注册）。

    规则与 backend/src_python/legacy_alias.py 一致：
      * 去掉 `/api/v<数字>` 前缀；
      * 下划线子路径（/api/v1/_debug 等）是蜜罐，不生成别名；
      * 规范路径已被真实实现占用时不生成别名（保留真实实现）。
    """
    rows_by_app: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        rows_by_app[_app_of(r["file"])].append(r)

    mapped, skipped_honeypot, skipped_collision = [], [], []
    for app_name, items in rows_by_app.items():
        paths = {r["path"] for r in items}
        for r in items:
            m = VERSION_RE.match(r["path"])
            if not m:
                continue
            new = "/api" + r["path"][m.end(1):]
            if new[len("/api"):].startswith("/_"):
                skipped_honeypot.append({"app": app_name, "old": r["path"]})
            elif new in paths:
                skipped_collision.append({"app": app_name, "old": r["path"], "new": new})
            else:
                mapped.append({"app": app_name, "method": r["method"],
                               "old": r["path"], "new": new})
    return {
        "mapped": mapped,
        "skipped_honeypot": skipped_honeypot,
        "skipped_collision": skipped_collision,
        "unique_old": len({m["old"] for m in mapped}),
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    versions = [r for r in rows if VERSION_RE.match(r["path"])]
    by_prefix = Counter(VERSION_RE.match(r["path"]).group(1) for r in versions)

    # 跨 app 的同路径同方法（21 条）是双后端各自一份，属于 P1 已知范围，
    # 只有**同一个 app 内**出现两份才算真重复（后者永远匹配不到 = 死代码）。
    key_files: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key_files[(r["method"], r["path"])].append(r)

    dups = []
    cross_app = []
    for k, items in sorted(key_files.items()):
        apps = Counter(_app_of(r["file"]) for r in items)
        if any(n > 1 for n in apps.values()):
            dups.append({"method": k[0], "path": k[1], "count": len(items),
                         "files": sorted(f'{r["file"]}:{r["line"]} {r["handler"]}' for r in items)})
        elif len(items) > 1:
            cross_app.append({"method": k[0], "path": k[1],
                              "files": sorted(f'{r["file"]}:{r["line"]} {r["handler"]}' for r in items)})

    # 同路径不同方法：只在同一个 app 内算（cross-app 的成对出现已在上面单列）
    path_only = Counter((_app_of(r["file"]), r["path"]) for r in rows)
    path_collisions = sorted({p for (_a, p), n in path_only.items() if n > 1})

    return {
        "total_routes": len(rows),
        "version_prefixes": dict(sorted(by_prefix.items(), key=lambda kv: int(kv[1]) and int(kv[1]))),
        "version_route_count": len(versions),
        "duplicates": dups,
        "cross_app_overlaps": cross_app,
        "path_collisions_same_path_different_method": path_collisions,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="版本号命名空间冻结 + 真重复审计")
    ap.add_argument("--write", action="store_true", help="重建基线")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--aliases", action="store_true",
                    help="打印版本号老路径 → 规范路径映射（运行期由 legacy_alias.py 注册）")
    args = ap.parse_args()

    rows = collect()
    s = summarize(rows)

    if args.aliases:
        am = alias_map(rows)
        print("版本号老路径 → 规范路径（同一 endpoint，老路径保留）")
        for item in am["mapped"]:
            print(f"  [{item['app']}] {item['old']}  →  {item['new']}")
        print(f"\n映射 {am['unique_old']} 条；"
              f"跳过蜜罐 {len(am['skipped_honeypot'])} 条；"
              f"跳过已被真实实现占用 {len(am['skipped_collision'])} 条")
        for item in am["skipped_collision"]:
            print(f"  [保留真实实现] {item['old']} → {item['new']} 已存在")
        return 0

    if args.write:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        payload = {
            "version_prefixes": s["version_prefixes"],
            "version_route_count": s["version_route_count"],
            "note": "新增版本号前缀必须改这里并说明理由；老前缀里也不许再加端点。",
        }
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"已写基线: {BASELINE}")

    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    print(f"总路由 {s['total_routes']} 条   版本号前缀端点 {s['version_route_count']} 条")
    print("版本号前缀分布：")
    for k, v in s["version_prefixes"].items():
        print(f"  {k:8s} {v:3d}")

    ok = True
    if os.path.exists(BASELINE):
        base = json.load(open(BASELINE, encoding="utf-8"))
        allowed = base.get("version_prefixes", {})
        new_prefixes = sorted(set(s["version_prefixes"]) - set(allowed))
        grown = sorted(
            k for k, v in s["version_prefixes"].items()
            if k in allowed and v > allowed[k]
        )
        if new_prefixes:
            ok = False
            print(f"\n[FAIL] 出现新的版本号前缀：{new_prefixes}")
            print("       新增端点请用领域路径（/api/<域>/<动作>），不要再开版本号。")
        if grown:
            ok = False
            print(f"\n[FAIL] 老版本号前缀里又加了端点：{grown}")
        if ok:
            print("\n[OK] 版本号前缀未新增、未增长。")
    else:
        print(f"\n[WARN] 没找到基线 {BASELINE}，请先 --write 生成一次。")

    if s["duplicates"]:
        ok = False
        print(f"\n[FAIL] 同一 app 内真重复（同路径同方法）{len(s['duplicates'])} 条：")
        for d in s["duplicates"]:
            print(f"  {d['method']:6s} {d['path']}  ×{d['count']}")
            for f in d["files"]:
                print(f"      {f}")
        print("       处置：同 app 内后注册的那份永远匹配不到，应删（见 watermark 那条的处理）。")
    else:
        print("\n[OK] 同一 app 内真重复 0 条（按 路径+方法 判重）。")

    if s["cross_app_overlaps"]:
        print(f"\n[INFO] 跨 server 同路径同方法 {len(s['cross_app_overlaps'])} 条 —— "
              "8000/8100 各一份，属 P1 已知范围（bridge 侧优先转发 8000）。")

    coll = s["path_collisions_same_path_different_method"]
    if coll:
        print(f"\n[INFO] 同路径不同方法 {len(coll)} 组 —— 属正常设计，不是重复：")
        for p in coll[:20]:
            methods = sorted({r["method"] for r in rows if r["path"] == p})
            print(f"      {p}  ({'+'.join(methods)})")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
