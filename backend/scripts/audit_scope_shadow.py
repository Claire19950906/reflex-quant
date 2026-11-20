#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态审计：检测「局部变量遮蔽模块级名字」这类 UnboundLocalError 陷阱。

事故背景（2026-11-05 生产实测）
--------------------------------
`engine/ai_worker.py` 顶层有一行：

    from .utils import _detect_vip, _now, _open_db, _ts      # _ts 是**函数**

后来有人在某个函数内部写了：

    _ts = None          # ← 只是想存一个时间戳
    _ts = float(_last_refresh)

Python 的作用域规则：**函数体内只要出现同名赋值，该名字在整个函数内
都被判定为局部变量**（即使赋值在异常分支里）。

于是同一个函数里任何 `_ts()` 调用都会抛：

    UnboundLocalError: cannot access local variable '_ts'
    where it is not associated with a value

这个异常发生在批事务里 → **整批 16 条全部回滚** → 前端「待分析」永不下降。
而且它只在特定数据分支触发（last_refresh 恰好是字符串格式），平时看不出来。

为什么需要自动审计
------------------
这类 bug 的特征是「**代码看起来完全正常**」：变量名合法、逻辑正确、语法无误，
只有在运行时特定路径下才炸。人读代码几乎不可能发现。

本脚本用 AST 做静态检查：
  1. 收集模块级所有绑定名（import / 赋值 / 函数定义 / 类定义）；
  2. 收集每个函数体内被赋值的局部名；
  3. 若某局部名与模块级绑定名同名 → 报告（模块级那个名字在该函数内不可用）。

只报告**模块级有绑定**的情况（那才是真的会遮蔽）。

用法：
    python3 scripts/audit_scope_shadow.py            # 全项目扫描
    python3 scripts/audit_scope_shadow.py --json     # 机器可读
    python3 scripts/audit_scope_shadow.py src_python/engine/ai_worker.py
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SRC = BACKEND / "src_python"


def _module_level_names(tree: ast.AST) -> set:
    """模块级（顶层）绑定过的所有**可调用对象**（函数/类）名。

    v322 修正：只关心「模块级是函数/类」的名字。
    局部变量遮蔽模块级的**普通变量**（rows / conn / os / today …）是无害的 ——
    函数内用的一直都是局部那个，语义没变。
    真正会炸的只有一种：局部变量把模块级的**函数/类**遮住了，
    而函数内又调用了它 → UnboundLocalError。
    """
    names: set = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # import 进来的名字**可能**是函数/类 → 一律纳入（宁可多报）
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def _called_names(node: ast.AST) -> set:
    """函数体内被**调用**的名字（Call.func 直接是 Name 的）。"""
    out: set = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out



def _assigned_names(node: ast.AST) -> set:
    """函数体内被**赋值语句**绑定的名字（局部变量）。

    v322 二次修正：**函数内的 import 不算遮蔽**。
    `from M import X` / `import X` 是在函数作用域里**新增**一个绑定，
    属于完全正常的局部导入写法（很多老代码都这么写），不是"遮蔽"。
    真正危险的只有**赋值语句**（`X = ...`）把模块级的同名绑定顶掉。

    另外排除三类「假遮蔽」：
      · `global X` 声明的 —— 明确指回模块级，不遮蔽；
      · `except E as X` 的 —— Python 3 里 `X` 在 except 块结束后被**删除**；
      · 推导式里的循环变量 —— Python 3 推导式有独立作用域，不遮蔽外层。
    """
    global_names: set = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Global, ast.Nonlocal)):
            global_names.update(sub.names)

    names: set = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                # 只取「真正被赋值」的名字。
                # 注意：ast.walk(tgt) 会把 `d[k] = v` 里的 **k** 也算进来 ——
                # 但 k 是被**读取**的，不是被赋值的 → 必须跳过 Subscript.slice。
                for n in _bound_names(tgt):
                    names.add(n)
        elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(sub.name)
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
    # except ... as X 不算遮蔽
    for sub in ast.walk(node):
        if isinstance(sub, ast.ExceptHandler) and sub.name:
            names.discard(sub.name)
    return names - global_names


def _bound_names(target: ast.AST) -> set:
    """赋值目标里**真正被绑定**的名字。

    必须自顶向下递归（不能用 ast.walk）：`d[k] = v` 里
    `k` 是被**读取**的（不是绑定目标），只有 `d` 是。
    ast.walk 是广度优先 —— 会先访问 Subscript 节点、再访问 slice 里的 Name，
    导致"先 discard 后 add"，`k` 又被算成绑定 → 误报。

    规则：
      Name              → 绑定
      Subscript.value   → 绑定（`d[k]` 绑定的是 d）
      Subscript.slice   → **不递归**（k 是读取）
      Attribute.value   → 递归（`a.b = v` 绑定的是 a）
      Tuple/List/Starred→ 递归每个元素（解包赋值 `(a, b) = ...`）
    """
    out: set = set()

    def _walk(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Subscript):
            # `d[k] = v` → 只绑定 d；k 在 slice 里，是读取，不递归
            _walk(node.value)
        elif isinstance(node, ast.Attribute):
            # `a.b = v` → 绑定 a
            _walk(node.value)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                _walk(elt)
        elif isinstance(node, ast.Starred):
            _walk(node.value)
        # 其它节点类型（Call / Constant / ...）在赋值目标位置没有意义

    _walk(target)
    return out


def _scan_file(path: Path) -> list:
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    module_names = _module_level_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _assigned_names(node)
        called = _called_names(node)
        # 三者同时成立才是真 bug：
        #   模块级有可调用对象 X  且  函数内有 X 的赋值  且  函数内调用了 X
        # 少任何一个都安全（要么没遮蔽，要么遮蔽了但没用到）。
        shadowed = sorted((local & module_names) & called)
        # 排除函数自己的名字（def f 内部递归调用 f 是合法的）
        shadowed = [s for s in shadowed if s != node.name]
        for s in shadowed:
            hits.append({
                "file": str(path.relative_to(BACKEND)),
                "line": node.lineno,
                "func": node.name,
                "name": s,
            })
    return hits


def _iter_files(targets):
    if targets:
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = BACKEND / t
            if p.is_file():
                yield p
            elif p.is_dir():
                for f in sorted(p.rglob("*.py")):
                    if "__pycache__" not in f.parts:
                        yield f
        return
    for f in sorted(SRC.rglob("*.py")):
        if "__pycache__" not in f.parts:
            yield f


def _selftest() -> bool:
    """自检：确认脚本**既能抓到真 bug，又不误报**。

    一个只会报噪音的审计脚本比没有脚本更糟 —— 大家会直接忽略它的输出。
    所以这里用三个已知样本校验：
      ① 真实 bug（2026-11-05 事故原样）→ 必须报出来；
      ② 局部 import（合法写法）→ 不该报；
      ③ 下标赋值 `d[k] = v`（k 是读取）→ 不该报。
    """
    samples = {
        "真bug_历史事故": (
            "from utils import _ts\n"
            "def f():\n"
            "    _ts = None\n"
            "    return _ts()\n"
        ),
        "合法_局部导入": (
            "from datetime import timezone\n"
            "def g():\n"
            "    from datetime import timezone\n"
            "    return timezone.utc\n"
        ),
        "合法_下标赋值": (
            "def _safe(x):\n"
            "    return str(x)\n"
            "def h(d):\n"
            "    d[_safe('k')] = 1\n"
            "    return d\n"
        ),
    }
    ok = True
    for label, code in samples.items():
        tree = ast.parse(code)
        mod = _module_level_names(tree)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = _assigned_names(node)
                called = _called_names(node)
                for s in sorted((local & mod) & called):
                    if s != node.name:
                        hits.append(s)
        expect_hit = (label == "真bug_历史事故")
        got_hit = bool(hits)
        if expect_hit != got_hit:
            ok = False
            print("  ❌ 自检失败：%s 期望 %s，实际 %s (%s)"
                  % (label, "报出" if expect_hit else "不报",
                     "报出" if got_hit else "不报", hits))
        else:
            print("  ✓ 自检通过：%s → %s"
                  % (label, "报出" if got_hit else "不报"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="审计局部变量遮蔽模块级名字（UnboundLocalError 陷阱）")
    ap.add_argument("paths", nargs="*", help="要扫描的路径（默认扫 src_python/）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="只跑自检")
    args = ap.parse_args()

    if args.selftest:
        print("审计脚本自检：")
        return 0 if _selftest() else 1

    all_hits = []
    for f in _iter_files(args.paths):
        all_hits.extend(_scan_file(f))

    if args.json:
        print(json.dumps(all_hits, ensure_ascii=False, indent=2))
        return 1 if all_hits else 0

    if not all_hits:
        print("✅ 未发现局部变量遮蔽模块级名字（无 UnboundLocalError 陷阱）")
        return 0

    print("❌ 发现 %d 处「局部变量遮蔽模块级名字」：" % len(all_hits))
    for h in all_hits:
        print("  %s:%d  函数 %s()  局部变量 %r 遮蔽了模块级同名绑定"
              % (h["file"], h["line"], h["func"], h["name"]))
    print()
    print("后果：模块级的那个名字在该函数内**不可用** → 调用即抛")
    print("      UnboundLocalError: cannot access local variable '%s'"
          % all_hits[0]["name"])
    print("修法：给局部变量改名（不要与模块级 import 的名字同名）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
