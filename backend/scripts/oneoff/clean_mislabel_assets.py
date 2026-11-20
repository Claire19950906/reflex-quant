#!/usr/bin/env python3
"""清理"挂错名字"的资产行：A50 与 CN-SZC。

问题（不是猜，是看数据看出来的）：
  · A50    最后一根 close = 3634.35  —— 这是**上证指数**的量级（A50 期货在 1.3 万以上）
  · CN-SZC 最后一根 close = 10968.79 —— 这是**深证成指**的量级
  也就是说这两个 asset 名下装的根本不是同名标的，代码注释里早就写了
  "留着就是在库里放假数据"。

为什么直接删而不是改名：
  · 两者各只有 1 根 K 线（且 src=kline_bars_1d，来自快照聚合），没有任何分析价值
  · ohlcv_refresh_cn.py 的 SKIP_ASSETS 已把它们排除 → 永远不会再增长
  · 改名会引入两个"1 根 K 线的新资产"，噪音更大

安全措施：
  · 删除前把要删的行原样导出到 runtime/backup/mislabel_assets_<ts>.json（可回滚）
  · dim_asset 行保留但 active=0（历史外键不断裂）
  · --dry-run 只看不删（默认行为），--apply 才真删

用法：
  python3 src_python/clean_mislabel_assets.py            # dry-run
  python3 src_python/clean_mislabel_assets.py --apply
"""
from __future__ import annotations
from pathlib import Path

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))
import db # noqa: E402

TARGETS = {
    "A50": "上证指数量级（3634），不是富时中国 A50",
    "CN-SZC": "深证成指量级（10968），不是中证系列",
}

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime", "backup")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row

    payload = {"ts": int(time.time()), "targets": {}, "rows": []}
    total = 0
    for code, reason in TARGETS.items():
        rows = [dict(r) for r in conn.execute(
            "SELECT asset, ts, open, high, low, close, volume, source FROM ohlcv_daily "
            "WHERE asset=? ORDER BY ts", (code,))]
        payload["targets"][code] = {"reason": reason, "ohlcv_rows": len(rows)}
        payload["rows"].extend(rows)
        total += len(rows)
        last = rows[-1] if rows else None
        print(f"  {code:<8} {len(rows)} 根 K 线"
              + (f"  最后一根 close={last['close']}" if last else "  （已无数据）")
              + f"   ← {reason}")

    if not args.apply:
        print(f"\n[dry-run] 将删除 {total} 行 ohlcv_daily，并把 dim_asset 对应行置 active=0")
        print("确认无误后加 --apply 执行。")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"mislabel_assets_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    deleted = conn.execute(
        "DELETE FROM ohlcv_daily WHERE asset IN ('A50','CN-SZC')").rowcount
    dim = conn.execute(
        "UPDATE dim_asset SET active=0 WHERE code IN ('A50','CN-SZC')").rowcount
    conn.commit()

    left = conn.execute(
        "SELECT asset, COUNT(*) n FROM ohlcv_daily WHERE asset IN ('A50','CN-SZC') "
        "GROUP BY asset").fetchall()
    print(f"\n已删除 ohlcv_daily {deleted} 行；dim_asset 置 inactive {dim} 行")
    print("剩余同代码行:", [dict(r) for r in left] or "无（已清空）")
    print("回滚备份:", path)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
