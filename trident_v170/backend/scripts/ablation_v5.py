#!/usr/bin/env python3
"""Ablation V5: 严格 1H 对照（entry_hour 起点 → exit_hour 终点）"""
import sqlite3, statistics

DB = '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db'

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        "SELECT id, entry_ts, exit_ts, entry_price, exit_price, ROUND(pnl_pct,4) pnl "
        "FROM quick_sim_trades WHERE asset='XAU' AND settled=1 AND action='BUY' "
        "ORDER BY entry_ts"
    ).fetchall()
    print('XAU BUY trade 总数:', len(trades))
    print()

    actual_pnls = []
    ctrl_pnls = []
    for t in trades:
        # entry 所在 1H K 线
        ek = conn.execute(
            "SELECT ts, open, close FROM kline_bars "
            "WHERE symbol='XAU' AND timeframe='1h' AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (t['entry_ts']*1000,)
        ).fetchone()
        # exit 所在 1H K 线
        xk = conn.execute(
            "SELECT ts, open, close FROM kline_bars "
            "WHERE symbol='XAU' AND timeframe='1h' AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (t['exit_ts']*1000,)
        ).fetchone()
        if not ek or not xk:
            print('#{}: K线不足'.format(t['id']))
            continue
        ctrl_entry = ek['open']
        ctrl_exit = xk['close']
        ctrl_pnl = (ctrl_exit - ctrl_entry) / ctrl_entry * 100
        actual_pnl = t['pnl']
        slippage = (t['entry_price'] - ctrl_entry) / ctrl_entry * 100
        diff = actual_pnl - ctrl_pnl
        actual_pnls.append(actual_pnl)
        ctrl_pnls.append(ctrl_pnl)
        print('#{:<3} actual={:+.4f}% ctrl={:+.4f}% diff={:+.4f}% slippage={:+.4f}%'.format(
            t['id'], actual_pnl, ctrl_pnl, diff, slippage))

    print()
    print('实验组 (actual) 平均 pnl:', round(statistics.mean(actual_pnls), 4), '%')
    print('控制组 (K线 entry_hour->exit_hour) 平均 pnl:', round(statistics.mean(ctrl_pnls), 4), '%')
    alpha = statistics.mean(actual_pnls) - statistics.mean(ctrl_pnls)
    print('alpha (实际 - 控制): {:+.4f}%'.format(alpha))
    if len(actual_pnls) >= 3:
        r = statistics.correlation(actual_pnls, ctrl_pnls)
        print('actual vs ctrl 相关性: {:+.3f}'.format(r))

    # WIN rate 对照
    actual_win = sum(1 for p in actual_pnls if p > 0) / len(actual_pnls)
    ctrl_win = sum(1 for p in ctrl_pnls if p > 0) / len(ctrl_pnls)
    print('WIN rate: actual={:.1f}% ctrl={:.1f}% diff={:+.1f}%'.format(
        100*actual_win, 100*ctrl_win, 100*(actual_win-ctrl_win)))

    conn.close()

if __name__ == '__main__':
    main()
