# -*- coding: utf-8 -*-
"""v421 修复 `from signals import <模块>` 形态的错误导入（可重复执行）。

背景（2026-11-17 13:00）：仓库里有 600+ 个 `.py` 的 import 头被批量改成
    `from signals import os` / `from signals import urllib.request` / `from signals import db`
`signals/` 是**信号/决策领域包**（只有 decision_*/signal_* 等 14 个模块），
拿不出 `os/json/db/urllib` 这些名字 → 全仓 import 失败、服务无法重启
（`from signals import urllib.request` 更是纯语法错误）。实测：
    · 改前 git show HEAD:api_server.py 是正常的 `import asyncio`
    · 改后 `python3 -c "import api_server"` 直接 SyntaxError
    · 线上进程没死只是因为 08:27 已经把旧代码载进内存了

判定规则（不靠猜）：
    · `signals/` 包里真实存在的模块名 → 保持 `from signals import X`（这是对的）
    · 其余一律还原成 `import X`（与原 HEAD 语义完全一致，不是"改设计"）
    · 逗号多模块/别名/行尾注释/分号语句 一并展开，保留原来的缩进与注释

用法：
    python3 backend/scripts/fix_signals_imports.py --check   # 只报数量，退出码 1 表示有污染
    python3 backend/scripts/fix_signals_imports.py --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SRC = os.path.join(REPO, "backend", "src_python")
PKG_DIR = os.path.join(SRC, "signals")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".next", ".pytest_cache"}

SINGLE = re.compile(r"^(\s*)from signals import ([\w\.]+)\s*$")
ALIAS = re.compile(r"^(\s*)from signals import ([\w\.]+) as (\w+)(.*)$")
ANY = re.compile(r"^(\s*)from signals import (.+)$")


def domain_modules() -> set[str]:
    return {
        os.path.splitext(f)[0]
        for f in os.listdir(PKG_DIR)
        if f.endswith(".py") and f != "__init__.py"
    }


def split_body(body: str) -> tuple[list[str], str, str]:
    """把 `a, b as c  # 注释; 语句` 拆成 (模块列表, 行尾注释, 分号语句)。"""
    comment = ""
    i = body.find("#")
    if i != -1:
        comment, body = body[i:], body[:i]
    body = body.strip()
    stmt = ""
    j = body.find(";")
    if j != -1:
        stmt, body = body[j:], body[:j]
    return [x.strip() for x in body.split(",") if x.strip()], comment, stmt


def fix_text(text: str, dom: set[str]) -> tuple[str, int]:
    out, fixed = [], 0
    for line in text.split("\n"):
        m = SINGLE.match(line)
        if m:
            ind, mod = m.group(1), m.group(2)
            if mod.split(".")[0] in dom:
                out.append(line)
            else:
                out.append(f"{ind}import {mod}")
                fixed += 1
            continue
        m = ALIAS.match(line)
        if m:
            ind, mod, alias, rest = m.groups()
            if mod.split(".")[0] in dom:
                out.append(line)
            else:
                out.append(f"{ind}import {mod} as {alias}{rest}")
                fixed += 1
            continue
        m = ANY.match(line)
        if not m:
            out.append(line)
            continue
        ind, body = m.group(1), m.group(2)
        mods, comment, stmt = split_body(body)
        if not mods or any(x.split()[0].split(".")[0] in dom for x in mods):
            out.append(line)
            continue
        for k, mod in enumerate(mods):
            last = k == len(mods) - 1
            tail = f" {comment.strip()}" if (last and comment) else ""
            out.append(f"{ind}import {mod}{tail}{stmt if last else ''}")
        fixed += 1
    return "\n".join(out), fixed


def walk():
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(base, fn)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dom = domain_modules()
    total_files = total_lines = 0
    for path in walk():
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if "from signals import" not in text:
            continue
        new, n = fix_text(text, dom)
        if n:
            total_files += 1
            total_lines += n
            if args.apply and new != text:
                open(path, "w", encoding="utf-8").write(new)
    print(f"含错误导入的文件 {total_files} 个 / 共 {total_lines} 处"
          + ("（已修复）" if args.apply else "（未修，加 --apply）"))
    if args.check and total_lines:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
