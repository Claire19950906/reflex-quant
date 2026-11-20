# -*- coding: utf-8 -*-
"""v342: 查"品种与文案不一致"（只读）。

实测样例 #11724：target_asset=XAU，但八段开头写"目标资产 BTC 的核心驱动…"。
这类行会让复盘把两个品种的判断混在一起（XAU 的结论其实是 BTC 的推理）。
本脚本只统计、不改库。

用法：python audit_asset_text_mismatch.py [--limit 20]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db # noqa: E402

_KNOWN = ("XAU", "WTI", "BTC", "ETH", "SOL", "CN_STOCK", "US_STOCK", "HK_STOCK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, target_asset, reasoning_path FROM ai_decisions"
        ).fetchall()
        bad = []
        for r in rows:
            ta = str(r[1] or "").strip().upper()
            p = str(r[2] or "")
            if not ta or ta in ("NONE", "OTHER"):
                continue
            m = re.search(r"目标资产\s*([A-Za-z_]+)", p)
            stated = (m.group(1).upper() if m else "")
            if stated and stated in _KNOWN and stated != ta:
                bad.append((r[0], ta, stated))
        print(f"品种与文案不一致：{len(bad)} / {len(rows)} 条")
        for i, (did, ta, stated) in enumerate(bad[: args.limit], 1):
            print(f"  {i:>2}. #{did}  标记={ta}  文案写={stated}")
        return 1 if bad else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
