"""opt_iso_ts_audit.py —— ISO 文本时间列审计（只读）

背景：库里 112 个时间列存的是**ISO 字符串**（如 `2026-11-13T00:16:31.115+08:00`），
而绝大多数时间窗口用的是**秒级整数**。两者一旦被直接比较：

  · `created_at >= 1794000000`   → 字符串与整数比较，SQLite 按类型排序，恒为真/假
  · `created_at BETWEEN lo AND hi` → 同上
  · 多个写入方格式不同（有 T 有空格 / 有偏移无偏移）→ 字典序排序错乱

本脚本做两件事（都不改数据）：
  1. 每个 ISO 列采样，统计出现了几种格式
  2. 在源码里 grep 该列的比较式，把"和数字/参数比"的可疑点列出来，人眼复核

用法：python3 opt_iso_ts_audit.py
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "trident_event_bus.db"
SRC = Path(__file__).resolve().parent

SHAPE = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+\d{2}:\d{2}$"), "ISO-T.frac+off"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+\d{2}:\d{2}$"), "ISO-T+off"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"), "ISO-T"),
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "ISO-space"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "date-only"),
    (re.compile(r"^[0-9a-f]{8}-"), "uuid?"),
]


def shape(v: str) -> str:
    for rx, name in SHAPE:
        if rx.match(v):
            return name
    return "other"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    mixed: list[tuple[str, str, Counter]] = []
    iso_cols: list[tuple[str, str]] = []
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for c in cols:
            if not (c == "ts" or c.endswith("_ts") or c.endswith("_at") or c.endswith("_time")):
                continue
            sample = conn.execute(
                f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL AND {c}<>'' "
                f"ORDER BY id DESC LIMIT 60" if "id" in cols else
                f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL AND {c}<>'' LIMIT 60"
            ).fetchall()
            if not sample:
                continue
            kinds = Counter(shape(str(r[0])) for r in sample)
            if not any(k.startswith(("ISO", "date")) for k in kinds):
                continue
            iso_cols.append((t, c))
            if len([k for k in kinds if k in ("ISO-T.frac+off", "ISO-T+off", "ISO-T",
                                              "ISO-space", "date-only")]) > 1:
                mixed.append((t, c, kinds))

    print(f"ISO 文本时间列 {len(iso_cols)} 个 · 其中**同列出现多种格式** {len(mixed)} 个")
    for t, c, k in mixed[:20]:
        print(f"  混格式 {t}.{c}: {dict(k)}")

    print("\n源码里对该列做比较的语句（疑似数字/字符串混比）:")
    hits = 0
    for t, c in iso_cols:
        pat = re.compile(rf"{re.escape(c)}\s*(>=|<=|>|<)\s*(\?|\d+)")
        for f in SRC.glob("*.py"):
            for ln, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if c not in line:
                    continue
                if pat.search(line):
                    hits += 1
                    print(f"  {f.name}:{ln}  {line.strip()[:110]}")
    if not hits:
        print("  （没有直接和数字/参数比较的写法）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
