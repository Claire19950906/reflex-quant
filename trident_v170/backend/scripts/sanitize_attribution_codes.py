#!/usr/bin/env python3
"""一次性数据清洗：把 attribution_reviews 表里越界的 reason_code / reasons_json 修复到 6。

背景：trident_event_bus.db 历史数据写入过 _REASON_NAMES 范围（1-23）之外的 code（如 24、0、-1），
导致 stats 面板顶部出现「未知(24)」并干扰主因聚合。已备份于 backups/。

幂等：已清洗过的行不会被再改。完成后会把修改的行数打印出来。
"""
from __future__ import annotations

import json
import sqlite3
import sys

REASON_CODE_MIN = 1
REASON_CODE_MAX = 23
FALLBACK_CODE = 6  # 模型判断错误 — 通用兜底，越界码的合理去向
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "trident_event_bus.db"


def _is_valid(c) -> bool:
    try:
        n = int(c)
    except (TypeError, ValueError):
        return False
    return REASON_CODE_MIN <= n <= REASON_CODE_MAX


def main() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 1. reason_code 越界 → 6
        cur = conn.execute(
            "UPDATE attribution_reviews SET reason_code = ? "
            "WHERE reason_code IS NOT NULL AND (reason_code < ? OR reason_code > ?)",
            (FALLBACK_CODE, REASON_CODE_MIN, REASON_CODE_MAX),
        )
        rc_fix = cur.rowcount
        print(f"reason_code 修复: {rc_fix} 行")

        # 2. reasons_json 越界 → 6 + 在 text 追加 sanitized 标记
        rows = conn.execute(
            "SELECT id, reasons_json FROM attribution_reviews "
            "WHERE reasons_json IS NOT NULL AND reasons_json != ''"
        ).fetchall()
        json_fix = 0
        for r in rows:
            try:
                arr = json.loads(r["reasons_json"])
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            changed = False
            for it in arr:
                if isinstance(it, dict) and not _is_valid(it.get("code")):
                    it["code"] = FALLBACK_CODE
                    note = it.get("text") or ""
                    if "[sanitized" not in note:
                        it["text"] = (note + " [sanitized: out-of-range→6]").strip()
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE attribution_reviews SET reasons_json = ? WHERE id = ?",
                    (json.dumps(arr, ensure_ascii=False), r["id"]),
                )
                json_fix += 1
        print(f"reasons_json 修复: {json_fix} 行")

        conn.commit()
        print("OK 清洗完成。")
        return 0
    except Exception as e:
        conn.rollback()
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())