#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_api_map.py — 从 routers/ 生成一页 API 地图（docs/API-MAP.md）。

为什么要生成而不是手写：422 个端点手写必错，且改一次就过期。
这个脚本只读源码里的 `@router.<method>("path")`，不导入任何模块，跑起来不碰库。

用法：
    python3 scripts/gen_api_map.py            # 写到 ../docs/API-MAP.md
    python3 scripts/gen_api_map.py --check    # 只比对，不写；与磁盘不一致时返回 1
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTERS = os.path.join(ROOT, "src_python", "routers")
OUT_DEFAULT = os.path.join(os.path.dirname(ROOT), "docs", "API-MAP.md")

_PAT = re.compile(r'@router\.(get|post|put|delete|patch)\("([^"]+)"')
_DOMAIN_ORDER = ["(root)", "api", "admin", "internal", "ops"]


def collect() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for fn in sorted(os.listdir(ROUTERS)):
        if not fn.endswith(".py") or fn.startswith("__"):
            continue
        src = open(os.path.join(ROUTERS, fn), encoding="utf-8").read()
        hits = _PAT.findall(src)
        if hits:
            out[fn[:-3]] = [(m.upper(), p) for m, p in hits]
    return out


def domain_of(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[1] if len(parts) > 1 else "(root)"


def render() -> str:
    rows = collect()
    total = sum(len(v) for v in rows.values())
    by_domain: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for name, eps in rows.items():
        # 一个 router 可能跨多个前缀，按出现最多的域归类
        doms = defaultdict(int)
        for _, p in eps:
            doms[domain_of(p)] += 1
        dom = max(doms, key=doms.get)
        by_domain[dom].append((name, len(eps)))

    lines = [
        "# API 地图",
        "",
        "> 本文件由 `backend/scripts/gen_api_map.py` 生成，**不要手改**。",
        f"> 统计：{len(rows)} 个路由文件，{total} 个端点。",
        "",
        "## 按域分布",
        "",
        "| 域 | 路由文件 | 端点数 |",
        "|---|---|---|",
    ]
    for dom in sorted(by_domain, key=lambda d: (-sum(n for _, n in by_domain[d]), d)):
        files = by_domain[dom]
        lines.append(f"| `/{dom if dom != '(root)' else ''}` | {len(files)} | {sum(n for _, n in files)} |")

    lines += ["", "## 路由文件 → 端点数", "", "| 文件 | 端点 | 代表路径 |", "|---|---|---|"]
    for name in sorted(rows, key=lambda n: -len(rows[n])):
        sample = rows[name][0][1]
        lines.append(f"| `routers/{name}.py` | {len(rows[name])} | `{rows[name][0][0]} {sample}` |")

    lines += [
        "",
        "## 怎么用这页",
        "",
        "1. 先按域找，`/api/...` 是对前端开放的，`/ops/...` 与 `/admin/...` 是运维口径。",
        "2. 找到文件后直接读那个 router，业务逻辑大多在它调用的 `src_python/*.py` 里。",
        "3. 想确认端点总数有没有变：`python3 scripts/architecture_baseline.py`。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    text = render()
    if args.check:
        cur = open(args.out, encoding="utf-8").read() if os.path.exists(args.out) else ""
        if cur != text:
            print("API-MAP.md 已过期，请重新生成：python3 scripts/gen_api_map.py")
            return 1
        print("API-MAP.md 与源码一致")
        return 0

    open(args.out, "w", encoding="utf-8").write(text)
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
