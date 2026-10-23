#!/usr/bin/env python3
"""Ablation V6: 扩样本 - 全资产全方向 V5 严格对照

不动分析层/verifier/KG/DB 表结构。纯诊断脚本。
"""
import sqlite3, statistics

DB = '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db'

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 所有 settled trade
    trades = conn.execute(
        "SELECT id, asset, action, entry_ts, exit_ts, entry_price, exit_price, "
        "ROUND(pnl_pct, 4) pnl, horizon_minutes "
        "FROM quick_sim_trades WHERE settled=1 "
        "ORDER BY asset, action, entry_ts"
    ).fetchall()

    # 按 (asset, action) 分组
    from collections import defaultdict
    groups = defaultdict(list)
    for t in trades:
        groups[(t['asset'], t['action'])].append(t)

    print('全资产全方向 V5 严格对照（entry_hour 起点 → exit_hour 终点）')
    print('=' * 100)

    summary = []
    for (asset, action), grp in sorted(groups.items()):
        actual_pnls = []
        ctrl_pnls = []
        wins_a = wins_c = 0
        for t in grp:
            ek = conn.execute(
                "SELECT ts, open, close FROM kline_bars "
                "WHERE symbol=? AND timeframe='1h' AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (asset, t['entry_ts']*1000)
            ).fetchone()
            xk = conn.execute(
                "SELECT ts, open, close FROM kline_bars "
                "WHERE symbol=? AND timeframe='1h' AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (asset, t['exit_ts']*1000)
            ).fetchone()
            if not ek or not xk:
                continue
            ctrl_pnl = (xk['close'] - ek['open']) / ek['open'] * 100
            actual_pnls.append(t['pnl'])
            ctrl_pnls.append(ctrl_pnl)
            if t['pnl'] > 0: wins_a += 1
            if ctrl_pnl > 0: wins_c += 1

        if not actual_pnls:
            print('{:<5} {:<5} n={:>3} (无 K 线对照)'.format(asset, action, len(grp)))
            continue

        n = len(actual_pnls)
        a_avg = statistics.mean(actual_pnls)
        c_avg = statistics.mean(ctrl_pnls)
        alpha = a_avg - c_avg
        a_win = wins_a / n
        c_win = wins_c / n
        win_alpha = a_win - c_win

        # 盈亏比
        win_pnls = [p for p in actual_pnls if p > 0]
        loss_pnls = [p for p in actual_pnls if p < 0]
        win_avg = statistics.mean(win_pnls) if win_pnls else 0
        loss_avg = statistics.mean(loss_pnls) if loss_pnls else 0
        pl_ratio = (win_avg / abs(loss_avg)) if loss_avg != 0 else float('inf')

        # 评级
        if alpha > 0.01 and win_alpha > 0.05:
            verdict = '正向alpha'
        elif alpha < -0.01 and win_alpha < -0.05:
            verdict = '反向alpha'
        elif win_alpha > 0.10:
            verdict = '方向alpha'
        else:
            verdict = 'no_alpha'

        print('{:<5} {:<5} n={:>3} | actual pnl={:+.4f}% ctrl pnl={:+.4f}% alpha={:+.4f}% | '
              'WIN a={:.0f}% c={:.0f}% Δ={:+.0f}% | 盈亏比={:.2f} | {}'.format(
                  asset, action, n, a_avg, c_avg, alpha,
                  100*a_win, 100*c_win, 100*win_alpha,
                  pl_ratio, verdict))
        summary.append({
            'asset': asset, 'action': action, 'n': n,
            'a_avg': a_avg, 'c_avg': c_avg, 'alpha': alpha,
            'a_win': a_win, 'c_win': c_win, 'win_alpha': win_alpha,
            'pl_ratio': pl_ratio, 'verdict': verdict,
        })

    print()
    print('=' * 100)
    print('汇总表')
    print('=' * 100)
    print('{:<6} {:<6} {:>3} | {:>9} {:>9} {:>9} | {:>5} {:>5} {:>5} | {:>6} | {}'.format(
        'asset', 'action', 'n', 'actual', 'ctrl', 'alpha',
        'awin', 'cwin', 'Δwin', 'plr', 'verdict'))
    print('-' * 100)
    for s in summary:
        print('{:<6} {:<6} {:>3} | {:>+9.4f} {:>+9.4f} {:>+9.4f} | {:>4.0f}% {:>4.0f}% {:>+4.0f}% | {:>5.2f} | {}'.format(
            s['asset'], s['action'], s['n'],
            s['a_avg'], s['c_avg'], s['alpha'],
            100*s['a_win'], 100*s['c_win'], 100*s['win_alpha'],
            s['pl_ratio'], s['verdict']))

    # 关键发现
    print()
    print('=' * 100)
    print('关键发现')
    print('=' * 100)
    pos = [s for s in summary if 'alpha' in s['verdict'] and s['verdict'] != 'no_alpha']
    print('有 alpha 的组: {} / {}'.format(len(pos), len(summary)))
    if pos:
        for s in pos:
            print('  {} {}: alpha={:+.4f}% win_alpha={:+.1f}% n={}'.format(
                s['asset'], s['action'], s['alpha'], 100*s['win_alpha'], s['n']))

    conn.close()

if __name__ == '__main__':
    main()
