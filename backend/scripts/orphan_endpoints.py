#!/usr/bin/env python3
"""孤儿端点审计：前端不引用的端点，到底还有没有人在用。

判定两个证据，缺一不敢算「可删」：
  1. code_refs —— 仓库里除了它自己的定义文件之外，还有没有代码/脚本/文档提到这个地址
     （前端、scripts、tests、docs、deploy、其它 py 模块都算；没人提到 = 没有调用方）
  2. log_hits  —— 服务日志里有没有人真的请求过（含路径前缀，兼容 /api/x/{id} 这种动态段）

三级结论：
  alive   —— 日志里被访问过（有人在用，别动）
  wired   —— 没人访问但代码里有引用（可能是 cron/脚本/外部工具按需调用，留着）
  orphan  —— 既没人访问也没人引用（候选删除，人工再确认一次再删）

用法：
  python3 backend/scripts/orphan_endpoints.py                # 人看的表
  python3 backend/scripts/orphan_endpoints.py --json         # 机器读
  python3 backend/scripts/orphan_endpoints.py --verdict orphan
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = ROOT
LOG_DIR = os.path.expanduser("~/.trident/log")
LOG_FILES = ("api.out.log", "api.err.log", "bridge.out.log", "bridge.err.log",
             "api_server.out", "bridge_server.out", "bridge_server2.out")

# 扫描范围：真正会「调用自己服务」的地方
SCAN_DIRS = ("frontend", "scripts", "deploy", "docs", "backend/scripts",
             "backend/tests", "backend/src_python", "dashboard", "ui-redesign",
             # 仓库外但真的会调本机端点的地方 —— 漏了它们就会把在用的端点判成孤儿：
             os.path.expanduser("~/.trident/wrappers"),
             os.path.expanduser("~/.trident/scripts"),
             os.path.expanduser("~/Library/LaunchAgents"))
SKIP_PARTS = {"node_modules", ".next", ".git", "dist", "build", "__pycache__",
              ".shots", "runtime", "backend/reports", ".venv", "venv"}

PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-./{}%:]+")
LOG_REQ_RE = re.compile(r'(?:GET|POST|PUT|DELETE|PATCH)\s+(/api/[^\s?\"]+)')


def load_endpoints() -> List[Dict[str, Any]]:
    """从基线里取前端零引用的端点。"""
    usage = os.path.join(ROOT, "backend/reports/baseline/frontend_usage.json")
    with open(usage, encoding="utf-8") as f:
        rows = json.load(f)
    return [r for r in rows if not r.get("frontend_refs")]


def hit_paths() -> Counter:
    """扫服务日志，统计每个被请求过的 /api 路径出现次数。"""
    seen: Counter = Counter()
    for name in LOG_FILES:
        fp = os.path.join(LOG_DIR, name)
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "/api/" not in line:
                        continue
                    for m in LOG_REQ_RE.finditer(line):
                        seen[m.group(1)] += 1
        except OSError:
            continue
    return seen


def code_refs() -> Dict[str, List[str]]:
    """扫仓库源码，统计每个 /api 路径被哪些文件提到（不含 baseline 产物本身）。"""
    refs: Dict[str, List[str]] = {}
    for d in SCAN_DIRS:
        base = d if os.path.isabs(d) else os.path.join(REPO, d)
        if not os.path.isdir(base):
            continue
        for pat in ("**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.sh",
                    "**/*.md", "**/*.yml", "**/*.yaml", "**/*.json", "**/*.html"):
            for fp in glob.glob(os.path.join(base, pat), recursive=True):
                if any(p in fp for p in SKIP_PARTS):
                    continue
                if os.path.getsize(fp) > 2_000_000:
                    continue
                try:
                    txt = open(fp, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                for p in set(PATH_RE.findall(txt)):
                    try:
                        rel = os.path.relpath(fp, REPO)
                    except ValueError:
                        rel = fp
                    refs.setdefault(p, []).append(rel)
    return refs


def owner_file(handler: str) -> str:
    """端点定义在哪个文件（用来把「自身的装饰器」从引用里扣掉）。"""
    for d, _, files in os.walk(os.path.join(REPO, "backend/src_python")):
        if "__pycache__" in d:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(d, f)
            try:
                txt = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if re.search(rf"def\s+{re.escape(handler)}\s*\(", txt):
                return os.path.relpath(fp, REPO)
    return ""


def verdict_of(path: str, hits: Counter, refs: Dict[str, List[str]], owner: str) -> Dict[str, Any]:
    # 日志命中：精确 + 前缀（动态段 {id} 拆成前缀比）
    log_hits = hits.get(path, 0)
    if not log_hits and "{" in path:
        prefix = path.split("{")[0]
        log_hits = sum(c for p, c in hits.items() if p.startswith(prefix))
    # 代码引用：扣掉自身定义文件
    files = [f for f in refs.get(path, []) if f != owner]
    if not files and "{" in path:
        prefix = path.split("{")[0]
        files = sorted({f for p, fl in refs.items() if p.startswith(prefix)
                        for f in fl if f != owner})
    if log_hits:
        v = "alive"
    elif files:
        v = "wired"
    else:
        v = "orphan"
    return {"log_hits": log_hits, "code_refs": files, "verdict": v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verdict", choices=("alive", "wired", "orphan"))
    args = ap.parse_args()

    eps = load_endpoints()
    hits = hit_paths()
    refs = code_refs()
    out = []
    for ep in eps:
        owner = owner_file(ep["handler"])
        v = verdict_of(ep["path"], hits, refs, owner)
        out.append({**ep, "owner": owner, **v})

    if args.verdict:
        out = [r for r in out if r["verdict"] == args.verdict]

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    counts = Counter(r["verdict"] for r in out)
    print(f"零引用端点 {len(eps)} 条 → alive {counts['alive']} / wired {counts['wired']} / orphan {counts['orphan']}\n")
    for v in ("orphan", "wired", "alive"):
        rows = [r for r in out if r["verdict"] == v]
        if not rows:
            continue
        print(f"── {v}（{len(rows)}）" + ("：没人访问也没人引用，可删候选" if v == "orphan" else "") + "\n")
        for r in rows:
            extra = (f" 日志{r['log_hits']}次" if r["log_hits"] else "")
            if r["code_refs"]:
                extra += " 引用:" + ",".join(r["code_refs"][:2])
            print(f"   {r['method']:6} {r['path']:52} {r['server']:12}{extra}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
