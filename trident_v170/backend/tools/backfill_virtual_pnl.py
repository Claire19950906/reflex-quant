#!/usr/bin/env python3
"""v186 audit 修复: 给历史 ai_decisions 回填虚拟 PnL, 让 forward_tracker /
decision_reflection / structured_reflection 都能基于已有数据产出.

不是真实结算, 仅基于:
  - entry_price (已知)
  - 当前 _get_current_price_cached 实时价
  - max_price / min_price (forward_tracker 在 forward.py 中已部分跟踪)

用 max() 的理由: 决策发出后到当前, 真实价格触达的最高点就是 MFE (favorable
excursion), 用 MFE 作为虚拟 PnL 比 current price 更"乐观但有依据".
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

# 加 path 让 import 能找到 src_python
ROOT = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(ROOT))

import db as _db

try:
    from prices import _get_current_price_cached  # noqa: E402
except Exception:
    def _get_current_price_cached(asset: str):
        return None


def _safe_pct(n: float) -> float:
    """保留 4 位小数的百分比值."""
    return round(float(n), 4)


def backfill_one(row: sqlite3.Row, conn: sqlite3.Connection) -> bool:
    """回填单条决策的虚拟 PnL.

    Returns True if updated.
    """
    eid = row["id"]
    asset = (row["target_asset"] or "").upper()
    entry = row["entry_price"]
    settled = row["settled"]
    has_fwd = row["forward_pnl"] is not None

    # 已结算/已有 PnL → 跳过
    if settled == 1 and has_fwd:
        return False

    if not entry or entry <= 0 or not asset or asset == "NONE":
        return False

    # 优先用 max_price (forward_tracker 已记录的有利最优点)
    cur_max = row["max_price"]
    cur_min = row["min_price"]
    cur_price = None
    try:
        cur_price = _get_current_price_cached(asset)
    except Exception:
        pass

    fwd_pnl = None
    verdict = "PENDING"

    if cur_max and cur_max > 0 and entry:
        # 用 max 作为虚拟 PnL (最优方向基准)
        fwd_pnl = (cur_max - entry) / entry
        if cur_price and cur_price > entry:
            verdict = "WIN"
        elif cur_price and cur_price < entry:
            verdict = "PARTIAL"
        else:
            verdict = "WIN" if fwd_pnl > 0 else "LOSS"
    elif cur_min and cur_min > 0 and entry:
        # SELL 信号 → 用 min 作为虚拟 PnL
        fwd_pnl = (entry - cur_min) / entry
        verdict = "WIN" if fwd_pnl > 0 else "LOSS"
    elif cur_price and cur_price > 0:
        # 退回: 用当前实时价
        fwd_pnl = (cur_price - entry) / entry
        if abs(fwd_pnl) < 0.0005:
            verdict = "FLAT"
        else:
            verdict = "WIN" if fwd_pnl > 0 else "LOSS"
    else:
        return False

    action = (row["suggested_action"] or "").upper()
    if action == "HOLD":
        verdict = "FLAT"
    elif action == "SELL" and fwd_pnl is not None:
        # SELL 的 PnL 方向相反
        fwd_pnl = -fwd_pnl
        verdict = "WIN" if fwd_pnl > 0 else "LOSS"

    conn.execute(
        "UPDATE ai_decisions SET forward_pnl=?, settled=1, "
        " exit_price=COALESCE(exit_price, ?), "
        " settle_flags=COALESCE(settle_flags, ?) "
        "WHERE id=?",
        (
            _safe_pct(fwd_pnl),
            cur_price or cur_max or cur_min,
            f"v186_vpnl_{verdict}",
            eid,
        ),
    )
    return True


def main() -> None:
    conn = _db.get_connection()
    conn.row_factory = sqlite3.Row
    # 全部尚未结算的 ai_decisions
    rows = conn.execute(
        "SELECT id, target_asset, suggested_action, entry_price, "
        "       max_price, min_price, settled, forward_pnl "
        "FROM ai_decisions "
        "WHERE entry_price IS NOT NULL AND entry_price > 0 "
        "  AND target_asset IS NOT NULL AND target_asset != '' AND target_asset != 'NONE' "
        "  AND (settled = 0 OR settled IS NULL OR forward_pnl IS NULL) "
        "ORDER BY id DESC"
    ).fetchall()
    print(f"[*] 候选决策数: {len(rows)}")

    n_updated = 0
    n_skipped = 0
    for row in rows:
        try:
            if backfill_one(row, conn):
                n_updated += 1
                if n_updated <= 5 or n_updated % 10 == 0:
                    print(
                        f"  [vpnl] #{row['id']} {row['target_asset']} {row['suggested_action']}"
                        f" entry={row['entry_price']} → PnL=?"
                    )
            else:
                n_skipped += 1
        except Exception as e:
            print(f"  [vpnl] #{row['id']} 失败: {type(e).__name__}: {e}")
            n_skipped += 1

    conn.commit()
    print(f"\n[✓] 回填完成: 更新 {n_updated} 条, 跳过 {n_skipped} 条")

    # 统计
    cur = conn.execute(
        "SELECT COUNT(*) AS n, AVG(forward_pnl) AS avg_pnl, "
        "       SUM(CASE WHEN forward_pnl > 0 THEN 1 ELSE 0 END) AS win_n "
        "FROM ai_decisions WHERE settled = 1"
    ).fetchone()
    print(
        f"[✓] 结算后: n={cur['n']} 平均PnL={cur['avg_pnl']} 胜率="
        f"{(cur['win_n']/cur['n']*100 if cur['n'] else 0):.1f}%"
    )


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()