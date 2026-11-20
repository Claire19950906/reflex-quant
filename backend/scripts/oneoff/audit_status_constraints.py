#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计：代码里写入的 status 值，是否都在数据库 CHECK 约束允许范围内。

起因（2026-11-07 生产事故）
--------------------------
raw_news.status 的 CHECK 只允许 ('PENDING','PROCESSING','DONE','FAILED')，
而代码把超龄行标成 'FILTERED' → 每条 UPDATE 都被数据库拒绝 →
异常被 except 吞掉 → 「待分析」4851 条永不下降，**日志一行都没有**。

这个脚本把同一类 bug 一次性扫干净：
  1. 读线上库所有带 status 的 CHECK 约束 → 得到每个表的合法值集合
  2. 在源码里找所有写这些表 status 列的 SQL → 提取字符串字面量
  3. 报告「写入了 CHECK 不允许的值」

用法：
    python3 scripts/audit_status_constraints.py            # 扫线上库
    python3 scripts/audit_status_constraints.py --db x.db  # 指定库
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import re
import sqlite3
import sys

BASE_DIR = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE_DIR, "src_python")

# 代码里常见的状态列名（大小写不敏感匹配）
STATUS_COLS = ("status", "state", "analysis_status")


def collect_checks(conn: sqlite3.Connection):
    """返回 {table: {col: set(allowed)}} —— 只取形如 CHECK (col IN (...)) 的约束。"""
    out = {}
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    for tbl, sql in rows:
        sql = sql or ""
        # CHECK (status IN ('A','B'))  /  CHECK ("status" IN ("A","B"))
        for m in re.finditer(
                r"CHECK\s*\(\s*[\"']?(\w+)[\"']?\s+IN\s*\(([^)]*)\)\s*\)", sql, re.I):
            col = m.group(1)
            vals = set()
            for v in re.findall(r"['\"]([^'\"]+)['\"]", m.group(2)):
                vals.add(v)
            if vals:
                out.setdefault(tbl, {})[col] = vals
    return out


def _strip_py_comments(text: str) -> str:
    """去掉 # 注释（保留字符串内的 #）。避免注释里的示例 SQL 造成误报。"""
    out = []
    in_s = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_s:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == in_s:
                in_s = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_s = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def collect_writes(src_root: str):
    """扫描源码里写**受 CHECK 约束的列**的 SQL 字面量。

    ⚠️ 必须扫**全文**（DOTALL），不能逐行 —— 本项目绝大多数 SQL 是多行拼接的，
    逐行扫描会漏掉 90%（第一版就是这么漏的）。
    """
    out = []
    for root, _dirs, files in os.walk(src_root):
        if "__pycache__" in root or os.sep + "tests" in root:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                raw = open(path, encoding="utf-8").read()
            except Exception:
                continue
            text = _strip_py_comments(raw)
            # 记录字符偏移 → 行号
            line_of = []
            ln = 1
            for ch in text:
                line_of.append(ln)
                if ch == "\n":
                    ln += 1

            def _line(pos):
                return line_of[pos] if 0 <= pos < len(line_of) else 0

            # ── UPDATE t SET col = 'lit' ──
            # ⚠️ 不要在末尾要求 `(;|$)` —— 本项目的 SQL 多数是多行字符串拼接，
            #    后面没有分号，那样会**整段匹配失败**（第一版就是这么漏掉 FILTERED 的）。
            for m in re.finditer(
                    r"UPDATE\s+(?:OR\s+\w+\s+)?([A-Za-z_][\w]*)\s+"
                    r"((?:(?!;).){0,200}?)\bSET\b((?:(?!;)(?!WHERE).){0,400})",
                    text, re.I | re.S):
                tbl = m.group(1).lower()
                setpart = m.group(3)
                for sm in re.finditer(
                        r"\b([A-Za-z_][\w]*)\s*=\s*'([^']*)'", setpart):
                    col, val = sm.group(1).lower(), sm.group(2)
                    out.append((path, _line(m.start() + m.start(3) + sm.start()),
                                tbl, col, val))
            # ── INSERT INTO t (cols...) VALUES ('a','b') ──
            for m in re.finditer(
                    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)"
                    r"\s*VALUES\s*\(([^)]*)\)", text, re.I | re.S):
                tbl = m.group(1).lower()
                cols = [c.strip().strip('"\'').lower()
                        for c in m.group(2).split(",")]
                vals = re.findall(r"'([^']*)'|\"([^\"]*)\"",
                                  m.group(3))
                flat = [a if a != "" else b for a, b in vals]
                if len(flat) != len(cols):
                    continue
                for c, v in zip(cols, flat):
                    if v:
                        out.append((path, _line(m.start()), tbl, c, v))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "trident_event_bus.db"))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("DB 不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        checks = collect_checks(conn)
        print("=== 带 status CHECK 约束的表 ===")
        for tbl, cols in sorted(checks.items()):
            for col, vals in sorted(cols.items()):
                print("  %-28s %-14s -> %s" % (tbl, col, ", ".join(sorted(vals))))
        if not checks:
            print("  （无）")

        writes = collect_writes(SRC)
        print("\n=== 代码里写入「受 CHECK 约束的列」的字面量 ===")
        bad = []
        seen = set()
        ok_n = 0
        for path, line, tbl, col, val in writes:
            allowed = checks.get(tbl, {}).get(col)
            if allowed is None:
                continue          # 该列没有 CHECK 约束 → 不在本次审计范围
            key = (tbl, col, val)
            if key in seen:
                continue
            seen.add(key)
            if val in allowed:
                ok_n += 1
                print("  ✅ %-24s %-22s %-16s = %s"
                      % (os.path.relpath(path, BASE_DIR), tbl, col, val))
            else:
                bad.append((path, line, tbl, col, val, sorted(allowed)))
                print("  ❌ %-24s %-22s %-16s = %-20s 不在允许值内"
                      % (os.path.relpath(path, BASE_DIR), tbl, col, val))
        print("\n" + "=" * 70)
        print("检查了 %d 个受约束的写入点，%d 个通过" % (ok_n + len(bad), ok_n))
        if bad:
            print("❌ 发现 %d 处「写入 CHECK 不允许的值」：" % len(bad))
            for path, line, tbl, col, val, allowed in bad:
                print("  %s:%s  %s.%s = %r" % (os.path.relpath(path, BASE_DIR),
                                               line, tbl, col, val))
                print("      允许值: %s" % ", ".join(allowed))
            return 1
        print("✅ 未发现「写入 CHECK 不允许的值」")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
