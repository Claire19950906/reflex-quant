#!/usr/bin/env python3
"""把代码内置 _SYSTEM_PROMPT 与 ai_main_prompts 表覆盖做差异检查。

背景：v173 把 system_prompt / doubao_prompt / json_force_prompt 等放进 DB 表
（ai_main_prompts）让用户能在 /api/ai/prompts 页面编辑。但代码每次升级
加新字段（v170 的 cross_net_* 就是新增的），如果用户没手动去页面同步，
LLM 仍然按用户旧覆盖跑 — 静默失配。

这个脚本做的事：
  1. 读 src_python/engine/ai_worker.py 的 _SYSTEM_PROMPT 常量（最新代码版）
  2. 读 DB ai_main_prompts 表里 kind='system_prompt' 的覆盖（可空）
  3. 输出对齐报告：
        - DB 缺失覆盖 → 提示"用户从未编辑过，可执行 --init 首次写入"
        - DB 覆盖与代码 hash 一致 → 提示"已对齐"
        - DB 覆盖与代码 hash 不一致 → 列出"代码里有但 DB 没有的字段说明"
          （这是用户要手动去 /api/ai/prompts 页面补的内容）
        - 提供 --force 把代码常量写入 DB（会覆盖用户的修改，谨慎）
        - 提供 --extract-keywords 把代码里出现的关键字段名抠出来，方便核对

幂等：除 --force / --init 之外的所有操作只读。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import sqlite3
import sys

DB_PATH = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db"),
)
AI_WORKER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src_python", "engine", "ai_worker.py",
)
KEY_FIELDS = (
    "asset_nature", "factor_ranking", "impact_magnitude", "asset_reasoning",
    "data_anchor_ts", "hard_data_evidence", "net_origin_info",
    "cross_net_first_seen_ts", "cross_net_source",
    "attention_score", "elasticity_window",
    "sibling_decisions",
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _load_code_prompt(path: str = AI_WORKER_PATH) -> str:
    """从 ai_worker.py 直接抽 _SYSTEM_PROMPT = \"\"\"...\"\"\" 块。

    不通过 import ai_worker（导入会触发大量网络/DB 初始化）。
    """
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(
        r'^_SYSTEM_PROMPT\s*=\s*"""(.*?)"""',
        src, flags=re.DOTALL | re.MULTILINE,
    )
    if not m:
        raise RuntimeError(f"未找到 _SYSTEM_PROMPT 块 in {path}")
    return m.group(1)


def _load_db_override(conn: sqlite3.Connection, kind: str):
    row = conn.execute(
        "SELECT content, length(content), updated_at, base_version "
        "FROM ai_main_prompts WHERE kind = ?",
        (kind,),
    ).fetchone()
    if not row:
        return None
    return {
        "content": row[0],
        "length": row[1],
        "updated_at": row[2],
        "base_version": row[3],
    }


def _keyword_presence(text: str) -> dict:
    return {k: (k in text) for k in KEY_FIELDS}


def _report(code_text: str, db_text: str | None) -> str:
    code_h = _hash(code_text)
    lines = []
    lines.append(f"代码常量: sha256={code_h}  长度={len(code_text)}  字")
    if db_text is None:
        lines.append("DB 覆盖: （无覆盖 — 代码常量生效中）")
        return "\n".join(lines)

    db_h = _hash(db_text)
    lines.append(f"DB 覆盖:  sha256={db_h}  长度={len(db_text)}  字")
    lines.append("")
    if code_h == db_h:
        lines.append("✅ 完全一致（已对齐）")
        return "\n".join(lines)

    lines.append("⚠️  hash 不一致 — 代码里有但 DB 没有的关键字段：")
    code_kw = _keyword_presence(code_text)
    db_kw = _keyword_presence(db_text)
    missing = [k for k in KEY_FIELDS if code_kw[k] and not db_kw[k]]
    if missing:
        for k in missing:
            lines.append(f"   ❌ {k}  — 代码里有说明，DB 覆盖里没出现")
    else:
        lines.append("   （DB 覆盖包含所有关键字段；hash 差异来自其他文本/顺序）")

    extras = [k for k in KEY_FIELDS if not code_kw[k] and db_kw[k]]
    if extras:
        lines.append("")
        lines.append("⚠️  DB 覆盖里多余字段（代码已删除/改名的字段）：")
        for k in extras:
            lines.append(f"   ⚠ {k}")
    return "\n".join(lines)


def _force_sync(conn: sqlite3.Connection, kind: str, code_text: str) -> int:
    """强制把代码常量写入 DB（覆盖用户修改）。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    cur = conn.execute(
        "SELECT 1 FROM ai_main_prompts WHERE kind = ?", (kind,),
    ).fetchone()
    if cur:
        conn.execute(
            "UPDATE ai_main_prompts SET content = ?, updated_at = ?, "
            "base_version = (SELECT MAX(COALESCE(base_version, 0), 5) FROM ai_main_prompts) "
            "WHERE kind = ?",
            (code_text, now, kind),
        )
        return conn.total_changes
    conn.execute(
        "INSERT INTO ai_main_prompts (kind, content, updated_at, base_version) "
        "VALUES (?, ?, ?, 5)",
        (kind, code_text, now),
    )
    return conn.total_changes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "sync")
    p.add_argument("--kind", default="system_prompt", help="覆盖种类（默认 system_prompt）")
    p.add_argument("--db", default=DB_PATH, help="DB 路径")
    p.add_argument("--force", action="store_true",
                   help="强制把代码常量写入 DB（覆盖用户修改，谨慎！）")
    p.add_argument("--init", action="store_true",
                   help="若 DB 无覆盖，写入代码常量；有覆盖则跳过（保护用户编辑）")
    args = p.parse_args()

    try:
        code_text = _load_code_prompt()
    except Exception as e:
        print(f"FAILED: 读 ai_worker.py 失败: {e}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        db = _load_db_override(conn, args.kind)
        print(_report(code_text, db["content"] if db else None))

        if args.force:
            print()
            print(f"⚠️  --force 触发：将覆盖 {args.kind} 现有内容")
            n = _force_sync(conn, args.kind, code_text)
            conn.commit()
            print(f"✅ 已写入，total_changes={n}")
            return 0

        if args.init:
            if db:
                print()
                print(f"ℹ️  DB 已有 {args.kind} 覆盖（用户编辑过），--init 跳过以保护用户内容")
                print(f"    如确需覆盖请加 --force")
                return 0
            n = _force_sync(conn, args.kind, code_text)
            conn.commit()
            print()
            print(f"✅ --init：首次写入完成，total_changes={n}")
            return 0
    finally:
        conn.close()

    # 不一致时给出操作建议
    print()
    print("💡 建议：")
    print("   1) 若需补齐关键字段 → 去 /api/ai/prompts 页面手动加缺失字段说明")
    print("   2) 若用户编辑已损坏 → python3 scripts/sync_prompts.py --force")
    print("   3) 若从未编辑过 → python3 scripts/sync_prompts.py --init")
    return 0


if __name__ == "__main__":
    sys.exit(main())