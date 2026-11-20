#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_time_semantics —— 时间字段口径审计（只读）。

为什么需要它：
    今天两次把监控结论读错，根因都是"时间字段格式不统一"，而且没人能一眼看出来：
        ai_decisions.created_at   = '2026-11-16T08:38:02.458+08:00'   （ISO，T 分隔）
        decision_errors.created_at= '2026-11-16 00:33:54'             （空格分隔）
        raw_news.ts               = 1794794970                        （epoch 整数）
        raw_news.first_seen_ts    = 空串 / ISO 混着
    跨格式做字符串比较不会报错，只会**恒真或恒假** —— 监控因此静默失效。

这个脚本做两件事：
    1. 扫描库里所有带 created_at / updated_at / *_at 的列，按样本判定真实格式；
    2. 扫描代码里 `xxx_at >= datetime(...)` / `>= ?` 的写法，标出"表格式与比较方式不匹配"的嫌疑点。

用法：python3 backend/scripts/audit_time_semantics.py [--json]
退出码：发现"疑似不匹配"时返回 1，可挂 CI。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
DB = BACKEND / "trident_event_bus.db"

ISO_T = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
SPACE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
EPOCH = re.compile(r"^1[0-9]{9}$")


def classify(value: str) -> str:
    v = str(value)
    if ISO_T.match(v):
        return "iso_t"
    if SPACE.match(v):
        return "space"
    if EPOCH.match(v):
        return "epoch"
    if v == "":
        return "empty"
    return "other"


def scan_tables(conn: sqlite3.Connection) -> dict:
    out = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")]
        except Exception:
            continue
        at_cols = [c for c in cols if c.endswith("_at") or c.endswith("_ts")]
        for c in at_cols:
            try:
                rows = conn.execute(
                    f"SELECT {c} FROM '{t}' WHERE {c} IS NOT NULL AND {c} != '' "
                    f"ORDER BY rowid DESC LIMIT 60").fetchall()
            except Exception:
                continue
            kinds = {}
            for (v,) in rows:
                k = classify(v)
                kinds[k] = kinds.get(k, 0) + 1
            if kinds:
                out[f"{t}.{c}"] = kinds
    return out


_TBL = re.compile(r"(?:FROM|JOIN|UPDATE)\s+([a-z_0-9]+)(?:\s+(?:AS\s+)?([a-z_]+))?", re.I)


def _table_for_alias(text: str, line_no: int, alias: str) -> str | None:
    """按别名反查表名：在该行往前找最近的 FROM/JOIN，别名要对得上。

    不这么做就会张冠李戴 —— 第一版曾把 trade_reflections_v2 的查询
    误报成 ai_decisions 的问题，这种误报比漏报更浪费人。
    """
    lines = text.splitlines()
    start = max(0, line_no - 60)
    best = None
    for i in range(start, line_no):
        for tm in _TBL.finditer(lines[i]):
            tbl, al = tm.group(1), tm.group(2)
            if al == alias:
                best = tbl
            elif al is None and alias == tbl:
                best = tbl
    if best:
        return best
    # 没有别名精确匹配时，退回"该行里出现的表名"
    for tm in re.finditer(r"[a-z_0-9]+", lines[line_no - 1]):
        name = tm.group(0)
        if name in ("created_at", "updated_at", alias):
            continue
    return None


def scan_code(shapes: dict) -> list:
    """找 `_at >= datetime(...)` 的写法，判断该表格式是否与比较方式冲突。"""
    suspects = []
    src_dirs = [BACKEND / "src_python", BACKEND / "scripts"]
    pat = re.compile(r"([a-z_]+)\.(created_at|updated_at)\s*>=\s*datetime\(")
    for d in src_dirs:
        for p in d.rglob("*.py"):
            if p.name.startswith("audit_time_semantics"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if not m:
                    continue
                alias = m.group(1)
                table = _table_for_alias(text, i, alias)
                key = f"{table}.created_at" if table else None
                kinds = shapes.get(key, {}) if key else {}
                if kinds.get("iso_t"):
                    suspects.append({
                        "file": str(p.relative_to(REPO)),
                        "line": i,
                        "table": table,
                        "format": "iso_t",
                        "code": line.strip()[:110],
                    })
    return suspects


def main() -> int:
    if not DB.exists():
        print(f"[ERR] 库不存在 {DB}")
        return 2
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        shapes = scan_tables(conn)
    finally:
        conn.close()
    suspects = scan_code(shapes)

    if "--json" in sys.argv:
        print(json.dumps({"shapes": shapes, "suspects": suspects},
                         ensure_ascii=False, indent=1))
        return 1 if suspects else 0

    print("=[1] 时间列格式分布（近 60 行样本）")
    for k, v in sorted(shapes.items()):
        if set(v) == {"empty"}:
            continue
        print(f"  {k:52s} {v}")

    print("\n=[2] 疑似格式不匹配的代码位置")
    if not suspects:
        print("  无")
    for s in suspects:
        print(f"  {s['file']}:{s['line']}  table={s['table']}  format={s['format']}")
        print(f"      {s['code']}")
    return 1 if suspects else 0


if __name__ == "__main__":
    sys.exit(main())
