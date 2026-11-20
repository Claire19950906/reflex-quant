"""P0 数据清理（2026-11-19）：清除测试/假数据，重置纸面账户。

背景：
  - ai_decisions 有 4 条 reasoning='test' 的探针行
  - quick_sim_trades id=379 是 entry=100/exit=101 的假单，污染 sim_equity_snapshots
  - t2_orders 有 decision_id=-990101/-990102 的 mock 单
  - t2_positions 52-55 以 exit_price=100 平仓（入场 63106），造成 -19968 假亏损
  - reflection_cycles 25 条 parent_signal=test_meta_* 的测试环

动作：
  1) 把所有待删行完整导出到 backups/p0_cleanup_<ts>/（JSON，可回溯）
  2) 删除上述假数据
  3) 以最新真实 tick 价平掉残存的 5 笔超杠杆持仓（note=p0_reset_mark_to_market）
  4) 账户重置为 PAPER_INITIAL_EQUITY_USDT(=10000) + 真实已实现盈亏

只读校验：脚本不触碰行情、新闻、K 线、决策主流程。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

DB = os.path.join(BACKEND, "trident_event_bus.db")
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT = os.path.join(BACKEND, "backups", f"p0_cleanup_{TS}")
os.makedirs(OUT, exist_ok=True)

INITIAL = float(os.getenv("PAPER_INITIAL_EQUITY_USDT", "10000"))


def dump(conn, table, where, name=None):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    rows = conn.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()
    path = os.path.join(OUT, f"{name or table}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([dict(zip(cols, r)) for r in rows], f, ensure_ascii=False, indent=2, default=str)
    print(f"  backup {table:24s} n={len(rows):4d} -> {os.path.basename(path)}")
    return rows


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"[1] 备份到 {OUT}")

    dump(conn, "ai_decisions", "trim(reasoning)='test'")
    dump(conn, "quick_sim_trades", "id=379 OR (entry_price=100 AND notional_usdt=0)")
    dump(conn, "sim_equity_snapshots", "note LIKE '%BTC BUY LOSS -5.000%'")
    dump(conn, "t2_orders", "decision_id < 0")
    dump(conn, "t2_positions", "id IN (52,53,54,55) AND exit_price=100")
    dump(conn, "reflection_cycles", "parent_signal LIKE 'test_%'")
    dump(conn, "t2_account", "id=1", name="t2_account_before")

    print("[2] 删除假数据")
    n1 = conn.execute("DELETE FROM ai_decisions WHERE trim(reasoning)='test'").rowcount
    n2 = conn.execute(
        "DELETE FROM quick_sim_trades WHERE id=379 OR (entry_price=100 AND notional_usdt=0)"
    ).rowcount
    n3 = conn.execute(
        "DELETE FROM sim_equity_snapshots WHERE note LIKE '%BTC BUY LOSS -5.000%'"
    ).rowcount
    n4 = conn.execute("DELETE FROM t2_orders WHERE decision_id < 0").rowcount
    n5 = conn.execute(
        "DELETE FROM t2_positions WHERE id IN (52,53,54,55) AND exit_price=100"
    ).rowcount
    n6 = conn.execute("DELETE FROM reflection_cycles WHERE parent_signal LIKE 'test_%'").rowcount
    conn.commit()
    print(f"  ai_decisions={n1} quick_sim={n2} sim_equity={n3} t2_orders={n4} "
          f"t2_positions={n5} reflection_cycles={n6}")

    print("[3] 账户归零 -> 干净起始本金")
    conn.execute(
        "UPDATE t2_account SET cash=?, equity=?, realized_pnl=0, updated_at=? WHERE id=1",
        (INITIAL, INITIAL, int(time.time())),
    )
    conn.commit()

    print("[4] 以真实 tick 价平掉超杠杆持仓")
    from trident2 import store as t2store

    open_rows = conn.execute(
        "SELECT id, asset, side, qty, entry_price FROM t2_positions "
        "WHERE status='open' ORDER BY id"
    ).fetchall()
    for p in open_rows:
        tick = conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (p["asset"],),
        ).fetchone()
        if not tick or not tick[0]:
            print(f"  SKIP pos#{p['id']} {p['asset']}: 无行情")
            continue
        res = t2store.close_position(
            conn, p["id"], exit_price=float(tick[0]),
            note="p0_reset_mark_to_market",
        )
        print(f"  close pos#{p['id']} {p['asset']} {p['side']} @ {float(tick[0]):.4f} "
              f"pnl={res['realized_pnl']:+.2f}")

    print("[5] 重算账户")
    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM t2_positions WHERE status='closed'"
    ).fetchone()[0]
    open_cnt = conn.execute("SELECT COUNT(*) FROM t2_positions WHERE status='open'").fetchone()[0]
    gross = conn.execute(
        "SELECT COALESCE(SUM(qty*entry_price),0) FROM t2_positions WHERE status='open'"
    ).fetchone()[0]
    equity = INITIAL + float(realized)
    cash = equity - float(gross)  # 无保证金占用时：现金=权益-持仓名义
    conn.execute(
        "UPDATE t2_account SET cash=?, equity=?, realized_pnl=?, updated_at=? WHERE id=1",
        (cash, equity, float(realized), int(time.time())),
    )
    conn.commit()
    acc = dict(conn.execute("SELECT * FROM t2_account WHERE id=1").fetchone())
    print(f"  realized={realized:.2f} open={open_cnt} gross={gross:.2f}")
    print(f"  account -> cash={acc['cash']:.2f} equity={acc['equity']:.2f}")

    conn.close()
    print(f"[OK] 备份目录: {OUT}")


if __name__ == "__main__":
    main()
