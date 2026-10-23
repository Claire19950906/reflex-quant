"""P0 单元验证：模拟一笔新 quick_sim 结算，验证 ai_decisions 被正确回填。

策略：
1. 插入一个"测试 ai_decisions"（不带 entry_price）
2. 插入一个"测试 quick_sim_trades"（已到期、settled=0）
3. 直接调用 quick_sim.settle_trades() 用 fake_price_fn
4. 验证 ai_decisions 被反向回填（is_correct/settled/forward_pnl/mfe/mae/exit_price）
5. 清理测试数据
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src_python"))

import quick_sim

DB_PATH = "/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db"
TEST_DCID = 999999  # 特殊标记


def fake_price_fn(asset: str) -> float:
    # BTC_TEST: entry 50000, exit 51000 → pnl_pct = +2%
    return {"BTC_TEST": 51000.0}.get(asset) or 0.0


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("DELETE FROM ai_decisions WHERE id = ?", (TEST_DCID,))
        conn.execute("DELETE FROM quick_sim_trades WHERE decision_id = ?", (TEST_DCID,))
        conn.commit()

        now = int(time.time())
        # 1. 测试 ai_decisions
        conn.execute(
            """INSERT INTO ai_decisions
               (id, news_id, sentiment_score, suggested_action, reasoning,
                created_at, target_asset, market_category)
               VALUES (?, 999999, 0.75, 'BUY', 'P0 test', ?, 'BTC_TEST', 'crypto')""",
            (TEST_DCID, time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        before = conn.execute(
            """SELECT is_correct, settled, forward_pnl, entry_price FROM ai_decisions WHERE id=?""",
            (TEST_DCID,),
        ).fetchone()
        print(f"[before] ai_decisions[{TEST_DCID}]: is_correct={before['is_correct']!r} "
              f"settled={before['settled']} forward_pnl={before['forward_pnl']} "
              f"entry_price={before['entry_price']}")

        # 2. 测试 quick_sim_trades（已到期）
        conn.execute(
            """INSERT INTO quick_sim_trades
               (decision_id, news_id, asset, action, score, evidence_confidence,
                gate_passed, gate_reason, notional_usdt, entry_price, entry_ts,
                horizon_minutes, max_price, min_price)
               VALUES (?, 999999, 'BTC_TEST', 'BUY', 0.75, 0.8, 1, '测试', 1000.0,
                       50000.0, ?, 20, 50000.0, 50000.0)""",
            (TEST_DCID, now - 1500),
        )
        # 模拟 open_trades 同步 entry_price
        from datetime import datetime
        entry_iso = datetime.fromtimestamp(now - 1500).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE ai_decisions SET entry_price=?, entry_time=?, holding_horizon_minutes=?
               WHERE id=?""",
            (50000.0, entry_iso, 20, TEST_DCID),
        )
        conn.commit()

        # 3. 调用 settle_trades
        settled = quick_sim.settle_trades(conn, fake_price_fn)
        print(f"\n[settle_trades] returned {len(settled)} settlements: {settled}")

        # 4. 验证
        after = conn.execute(
            """SELECT is_correct, settled, forward_pnl, exit_price, mfe_pct, mae_pct,
                      entry_price, entry_time FROM ai_decisions WHERE id=?""",
            (TEST_DCID,),
        ).fetchone()
        print(f"\n[after] ai_decisions[{TEST_DCID}]:")
        print(f"  is_correct={after['is_correct']!r} settled={after['settled']} "
              f"forward_pnl={after['forward_pnl']:+.4f} exit_price={after['exit_price']}")
        print(f"  mfe_pct={after['mfe_pct']:+.4f} mae_pct={after['mae_pct']:+.4f}")
        print(f"  entry_price={after['entry_price']} entry_time={after['entry_time']!r}")

        # settle 时 max=max(50000,51000)=51000, min=min(50000,51000)=50000
        # BUY 50000→51000: mfe=(51000-50000)/50000*100=2.0%, mae=(50000-50000)/50000*100=0.0%
        checks = [
            ("is_correct == WIN", after["is_correct"] == "WIN"),
            ("settled == 1", after["settled"] == 1),
            ("forward_pnl ≈ +2.0%", abs(after["forward_pnl"] - 2.0) < 0.01),
            ("exit_price == 51000", after["exit_price"] == 51000.0),
            ("mfe_pct ≈ +2.0%", abs(after["mfe_pct"] - 2.0) < 0.01),
            ("mae_pct == 0.0", abs(after["mae_pct"]) < 0.01),
            ("entry_price == 50000", after["entry_price"] == 50000.0),
            ("entry_time not empty", bool(after["entry_time"])),
        ]
        print("\n[checks]")
        all_ok = True
        for label, ok in checks:
            mark = "✓" if ok else "✗"
            if not ok:
                all_ok = False
            print(f"  {mark} {label}")

        if not all_ok:
            print("\n[FAIL] 验证失败")
            return 1

        print("\n[PASS] P0 settle 反向回填验证成功！")
        return 0
    finally:
        conn.execute("DELETE FROM ai_decisions WHERE id = ?", (TEST_DCID,))
        conn.execute("DELETE FROM quick_sim_trades WHERE decision_id = ?", (TEST_DCID,))
        conn.commit()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())