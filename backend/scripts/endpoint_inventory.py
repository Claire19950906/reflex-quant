#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""endpoint_inventory.py — 后端路由 × 前端引用 只读盘点（P2-2）。

只做静态分析，不发请求、不删路由。产出：
  1. 后端注册的全部 HTTP 路由（方法 + 路径 + 定义文件）；
  2. 前端源码里出现的 /api 路径（含 API_BASE 拼接的相对路径）；
  3. 匹配结果：前端零引用候选、路径重复定义。

口径与限制（务必读）：
  · 静态字符串匹配，前端模板/动态路径（${var}）无法完全展开，
    「零引用」是候选清单，必须人工复核后再决定下线；
  · 后端路由若在运行时按配置注册、或由 bridge 手工转发，静态抽取可能漏；
  · 健康检查 / 内置文档 / 调试端点即使零引用也可能被探针或排障使用，
    下线前先确认无外部 cron / launchd 调用。

用法：
  python3 scripts/endpoint_inventory.py            # 人读汇总
  python3 scripts/endpoint_inventory.py --json    # 机读
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"
SRC = BACKEND / "src_python"

DECOR = re.compile(
    r'@(?:app|router)\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'
)
PREFIX = re.compile(r'APIRouter\(\s*prefix\s*=\s*["\']([^"\']+)["\']')
FE_PATH = re.compile(r'["\'`](/api/[A-Za-z0-9_:/-]+)["\'`]')
FE_REL = re.compile(r'["\'`](/[A-Za-z0-9_:/-]{3,})["\'`]')


def _norm(path: str) -> str:
    """路径归一化：去掉尾部斜杠、把 FastAPI 参数 :id/{id} 统一。"""
    p = path.rstrip("/") or "/"
    p = re.sub(r"\{[^}]+\}", "{}", p)
    return p


def collect_backend():
    routes = []
    for py in SRC.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        prefixes = [m.group(1) for m in PREFIX.finditer(text)]
        local_prefix = prefixes[-1] if prefixes else ""
        for m in DECOR.finditer(text):
            method, raw = m.group(1).upper(), m.group(2)
            full = (local_prefix + raw) if local_prefix else raw
            routes.append({
                "method": method,
                "path": _norm(full),
                "raw": raw,
                "file": str(py.relative_to(BACKEND)),
            })
    return routes


def collect_frontend():
    paths = set()
    exts = ("*.ts", "*.tsx", "*.js", "*.jsx")
    for ext in exts:
        for f in FRONTEND.rglob(ext):
            if "node_modules" in f.parts:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in FE_PATH.finditer(text):
                paths.add(_norm(m.group(1)))
            # API_BASE 拼相对路径：/xxx 会被加上 /api 前缀
            for m in FE_REL.finditer(text):
                rel = m.group(1)
                if rel.startswith("/api/"):
                    continue
                paths.add(_norm("/api" + rel))
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--unreferenced-only", action="store_true")
    args = ap.parse_args()

    routes = collect_backend()
    fe = collect_frontend()

    # 重复定义：同方法同路径出现多次
    seen = {}
    duplicates = []
    for r in routes:
        key = (r["method"], r["path"])
        if key in seen:
            duplicates.append({"method": r["method"], "path": r["path"],
                              "files": [seen[key], r["file"]]})
        else:
            seen[key] = r["file"]

    unreferenced = [r for r in routes
                    if r["path"] not in fe and not any(
                        r["path"].startswith(p.rstrip("/")) for p in fe)]

    summary = {
        "backend_routes": len(routes),
        "frontend_paths": len(fe),
        "duplicate_routes": len(duplicates),
        "unreferenced_candidates": len(unreferenced),
    }

    if args.json:
        print(json.dumps({"summary": summary, "duplicates": duplicates,
                          "unreferenced": unreferenced}, ensure_ascii=False, indent=2))
        return 0

    print("== 路由 × 前端引用 盘点（静态，需人工复核）==")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\n== 重复定义（同方法同路径多文件）==")
    if duplicates:
        for d in duplicates[:30]:
            print(f"  {d['method']:6} {d['path']}")
            for f in d["files"]:
                print(f"        - {f}")
    else:
        print("  无")
    if not args.unreferenced_only:
        print("\n提示：加 --unreferenced-only 查看前端零引用候选。")
    print("\n== 前端零引用候选（前 40，须复核再下线）==")
    for r in unreferenced[:40]:
        print(f"  {r['method']:6} {r['path']:55} {r['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
