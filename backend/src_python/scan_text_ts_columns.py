#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_text_ts_columns.py — 扫描"TEXT 时间列 vs cutoff 格式"的一致性
=========================================================================
为什么需要这个文件:
  v309 P2 在 api_server.py 里发现了一个真 bug:
    review_insights.created_at 存的是**空格**分隔 UTC ('2026-10-25 08:00:00'),
    但 endpoint 用 'T' 分隔的 cutoff 去比 → 字节序 ' '(0x20) < 'T'(0x54)
    → **cutoff 当天的行被整片漏掉**。

  这个 bug 的可怕之处在于它**极难靠肉眼发现**:
    · SQL 看起来完全正常 (就是 created_at >= ?)
    · 端点返回 200, 有数据, 只是数量偏少
    · 只有在"窗口恰好落在当天"时才会显形
    · v302+ 那次修复反而让它更容易发生 (把 cutoff 从空格改成 T)

  所以必须**机械地**把全项目所有"比较 TEXT 时间列"的地方列出来, 逐个核对
  cutoff 格式与该列落库格式是否一致。靠人记是不行的。

扫描方式 (AST, 不是 grep):
  1. 找出所有 SQL 字符串常量里出现 `created_at` / `timestamp` / `ts` 比较的
     (函数, SQL) 对;
  2. 在同一个函数里找 cutoff 是怎么来的:
       · _utc_ts_to_iso(...)     → 'T' 分隔
       · _utc_ts_to_space_iso(...) → ' ' 分隔
       · 直接写字面量           → 手工判断
       · 变量传递               → 追一层
  3. 打印对照表, 由人确认每一行。

输出: 一张"函数 | 被比较列 | cutoff 来源 | cutoff 分隔符"的表。
     脚本**不自动判定对错** —— 各表的落库格式只能逐个确认。
     但它保证**不漏看**, 这是 grep/肉眼都做不到的。

退出码: 0 = 扫描完成 (不代表没有 bug); 1 = 扫描失败。
"""
from __future__ import annotations

import ast
import os
import re
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(SRC)
ROOT = os.path.dirname(BACKEND)

# 会被当成"时间列"的名字
TIME_COL_RE = re.compile(r"\b(created_at|updated_at|timestamp|entry_time|"
                          r"settled_at|resolved_at|reviewed_at|last_tick_at|ts)\b")

# SQL 比较运算符
CMP_RE = re.compile(r"\b(created_at|updated_at|timestamp|entry_time|"
                     r"settled_at|resolved_at|reviewed_at|last_tick_at|ts)\s*"
                     r"(>=|<=|>|<|=)\s*(\?|:[a-z_]+)")

# cutoff helper → 分隔符
HELPER_FMT = {
    "_utc_ts_to_iso": "T",
    "_utc_ts_to_space_iso": " ",
    "_utc_str": " ",
    "epoch_to_bj_str": " ",
    "epoch_to_bj_store": "T",
    "bj_now_str": "T",
    "utc_str_to_epoch": "(epoch)",
    "bj_str_to_epoch": "(epoch)",
}


def _helper_calls(seg: str) -> list[tuple[str, str]]:
    """在代码片段里找 cutoff 相关的 helper 调用 → [(helper, 分隔符)]。"""
    out: list[tuple[str, str]] = []
    for name, fmt in HELPER_FMT.items():
        if re.search(rf"\b{re.escape(name)}\s*\(", seg):
            out.append((name, fmt))
    return out


def _string_constants(node: ast.AST) -> list[str]:
    """取节点内所有 str 常量 (排除 docstring)。"""
    docstrings: set[int] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Module, ast.FunctionDef,
                            ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(sub, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    out = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and id(sub) not in docstrings):
            out.append(sub.value)
    return out


def scan_file(path: str) -> list[tuple[str, str, str, str]]:
    """返回 [(函数名, 被比较列, cutoff 来源, cutoff 分隔符)]。"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        print(f"  !! 解析失败 {path}: {exc}", file=sys.stderr)
        return []

    results: list[tuple[str, str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if not seg:
            continue

        # 该函数里所有 SQL 字符串
        sqls = [s for s in _string_constants(node)
                if any(kw in s.upper() for kw in ("SELECT", "FROM", "WHERE"))]
        if not sqls:
            continue

        # 找出被比较的时间列
        cols: set[str] = set()
        for sql in sqls:
            for m in CMP_RE.finditer(sql):
                cols.add(m.group(1))
        if not cols:
            continue

        # cutoff 来源
        helpers = _helper_calls(seg)
        if helpers:
            for hname, hfmt in helpers:
                for col in sorted(cols):
                    results.append((node.name, col, hname, hfmt))
        else:
            # 没有 helper → 手工判断 (可能是字面量或远端传来的变量)
            for col in sorted(cols):
                results.append((node.name, col, "(字面量/变量 - 需人工确认)", "?"))
    return results


def main() -> int:
    targets = [
        os.path.join(SRC, "api_server.py"),
        os.path.join(SRC, "bridge_server.py"),
        os.path.join(SRC, "review_agent.py"),
    ]
    targets = [t for t in targets if os.path.exists(t)]
    if not targets:
        print("  没找到可扫描的文件", file=sys.stderr)
        return 1

    for path in targets:
        rows = scan_file(path)
        print(f"\n=== {os.path.relpath(path, ROOT)} — {len(rows)} 个时间列比较点 ===")
        if not rows:
            print("  (无)")
            continue
        # 按函数分组打印
        cur_fn = None
        for fn, col, src_helper, fmt in rows:
            if fn != cur_fn:
                cur_fn = fn
                print(f"\n  [{fn}]")
            flag = ""
            if fmt == "T":
                flag = "  ⚠ 'T' 分隔 cutoff"
            elif fmt == " ":
                flag = "  (空格分隔 cutoff)"
            print(f"      列={col:<16} cutoff={src_helper}{flag}")

    print("\n" + "=" * 66)
    print("核对规则:")
    print("  1. 该列的**落库格式**是什么? (看 db.py 的 DEFAULT, 或写入方的 strftime)")
    print("     · 'T' 分隔 → 必须配 'T' 分隔 cutoff")
    print("     · ' ' 分隔 → 必须配 ' ' 分隔 cutoff")
    print("  2. ⚠ 标记的行: cutoff 是 'T' 分隔 —— 若该列存的是空格分隔, 就是 bug")
    print("  3. 混合存储的列 (两种格式都有) → 必须用归一化表达式 (time_utils)")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
