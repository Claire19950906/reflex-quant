#!/usr/bin/env python3
"""Ablation V8: 数据完整性 audit + gate 过滤重算

V7 发现 #12 是 gate fail 但仍 trade 的事故样本。
V8 验证：是否所有 trade 都是这样？

不动分析层/verifier/KG/DB 表结构。纯诊断脚本。
"""
import sqlite3, statistics

DB = '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db'

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1. 数据完整性 audit
    print('=' * 80)
    print('1. 全部 trade 的 gate 状态 audit')
    print('=' * 80)
    all_trades = conn.execute(
        "SELECT t.id, t.asset, t.action, t.pnl_pct, t.entry_ts, t.exit_ts, "
        "d.evidence_confidence, d.evidence_action, d.trade_gate_reason "
        "FROM quick_sim_trades t "
        "LEFT JOIN ai_decisions d ON t.decision_id=d.id "
        "WHERE t.settled=1 ORDER BY t.id"
    ).fetchall()

    n_total = len(all_trades)
    n_gate_fail = 0
    n_evidence_hold = 0
    n_pnl_zero = 0
    n_clean = 0
    clean_samples = []

    for t in all_trades:
        gate = t['trade_gate_reason'] or ''
        ev = t['evidence_action'] or ''
        pnl = t['pnl_pct'] or 0
        is_fail = '低于闸门' in gate
        is_hold = ev == 'HOLD'
        is_zero = abs(pnl) < 0.0001
        is_clean = not is_fail and not is_hold and not is_zero
        if is_fail: n_gate_fail += 1
        if is_hold: n_evidence_hold += 1
        if is_zero: n_pnl_zero += 1
        if is_clean:
            n_clean += 1
            clean_samples.append(t)

    print('总样本数:        {}'.format(n_total))
    print('gate 失败:       {} / {}'.format(n_gate_fail, n_total))
    print('evidence=HOLD:   {} / {}'.format(n_evidence_hold, n_total))
    print('pnl=0 占位:      {} / {}'.format(n_pnl_zero, n_total))
    print('清洁样本:        {} / {}'.format(n_clean, n_total))
    print()

    print('所有 24 笔 trade 列表:')
    print('{:<3} {:<5} {:<5} {:>9} {:>6} {:<8} {}'.format(
        'id', 'asset', 'act', 'pnl%', 'conf', 'ev_act', 'gate_status'))
    print('-' * 90)
    for t in all_trades:
        gate = t['trade_gate_reason'] or ''
        is_fail = '低于闸门' in gate
        ev = t['evidence_action'] or ''
        flag = 'FAIL+HOLD' if is_fail and ev=='HOLD' else \
               'FAIL' if is_fail else \
               'HOLD' if ev=='HOLD' else 'CLEAN'
        print('{:<3} {:<5} {:<5} {:>+9.4f} {:>6.1f} {:<8} {}'.format(
            t['id'], t['asset'], t['action'],
            t['pnl_pct'] or 0, t['evidence_confidence'] or 0,
            ev, flag))

    # 2. V5 严格对照：全部 vs 清洁样本
    print()
    print('=' * 80)
    print('2. V5 严格对照（全部样本 vs 清洁样本）')
    print('=' * 80)

    from collections import defaultdict
    groups = defaultdict(lambda: {'all': [], 'clean': [], 'ctrl_all': [], 'ctrl_clean': []})

    for t in all_trades:
        ek = conn.execute(
            "SELECT open, close FROM kline_bars WHERE symbol=? AND timeframe='1h' "
            "AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (t['asset'], t['entry_ts']*1000)
        ).fetchone()
        xk = conn.execute(
            "SELECT open, close FROM kline_bars WHERE symbol=? AND timeframe='1h' "
            "AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (t['asset'], t['exit_ts']*1000)
        ).fetchone()
        if not ek or not xk: continue
        ctrl = (xk['close'] - ek['open']) / ek['open'] * 100

        gate = t['trade_gate_reason'] or ''
        ev = t['evidence_action'] or ''
        pnl = t['pnl_pct'] or 0
        is_clean = '低于闸门' not in gate and ev != 'HOLD' and abs(pnl) > 0.0001

        k = (t['asset'], t['action'])
        groups[k]['all'].append(pnl)
        groups[k]['ctrl_all'].append(ctrl)
        if is_clean:
            groups[k]['clean'].append(pnl)
            groups[k]['ctrl_clean'].append(ctrl)

    print('{:<5} {:<5} | {:>4} {:>4} | {:>9} {:>9} {:>9} | {:>4} | {:>9} {:>9} {:>9}'.format(
        'asset', 'action', 'nA', 'nC', 'actAvg', 'ctrlAvg', 'alpha',
        'winA', 'actC', 'ctrC', 'alpC'))
    print('-' * 110)
    for (asset, action), g in sorted(groups.items()):
        na, nc = len(g['all']), len(g['clean'])
        if not g['all']: continue
        a_avg = statistics.mean(g['all'])
        c_avg = statistics.mean(g['ctrl_all'])
        alpha = a_avg - c_avg
        wins = sum(1 for p in g['all'] if p > 0)
        win_pct = wins / na * 100

        if nc > 0:
            ac = statistics.mean(g['clean'])
            cc = statistics.mean(g['ctrl_clean'])
            alc = ac - cc
            print('{:<5} {:<5} | {:>4} {:>4} | {:>+9.4f} {:>+9.4f} {:>+9.4f} | {:>3.0f}% | {:>+9.4f} {:>+9.4f} {:>+9.4f}'.format(
                asset, action, na, nc, a_avg, c_avg, alpha, win_pct,
                ac, cc, alc))
        else:
            print('{:<5} {:<5} | {:>4} {:>4} | {:>+9.4f} {:>+9.4f} {:>+9.4f} | {:>3.0f}% | (无清洁样本)'.format(
                asset, action, na, nc, a_avg, c_avg, alpha, win_pct))

    # 3. 关键发现
    print()
    print('=' * 80)
    print('3. 关键发现')
    print('=' * 80)

    if n_gate_fail == n_total:
        print('🔥 全部 {} 笔 trade 的 gate 都失败！'.format(n_total))
        print('   → 24 笔 trade 全部是"gate fail 但仍 trade"的事故样本')
        print('   → V5/V6 的 alpha 结论完全基于这些事故样本 → 结论不可信')
    elif n_gate_fail > n_total * 0.5:
        print('  ⚠️ {} / {} 笔 gate 失败（> 50%）'.format(n_gate_fail, n_total))

    if n_clean == 0:
        print()
        print('  清洁样本 = 0 笔')
        print('  → 无法用"通过 gate + 非 HOLD + 非占位"的样本评估 AI alpha')
        print('  → 整个 quick_sim_trades 表的 24 笔都不是 AI 真实信号驱动的样本')

    print()
    print('4. 进一步诊断（如果清洁样本 > 0）')
    print('=' * 80)
    if clean_samples:
        for t in clean_samples:
            print('  #{} {} {} pnl={:+.4f}% conf={} ev_act={}'.format(
                t['id'], t['asset'], t['action'],
                t['pnl_pct'] or 0, t['evidence_confidence'] or 0,
                t['evidence_action']))
    else:
        print('  无清洁样本')

    print()
    print('=' * 80)
    print('5. 结论')
    print('=' * 80)
    if n_clean == 0:
        print('V5/V6 的所有"alpha 结论"都基于 gate 失败的样本，结论不成立。')
        print('需要先解决以下系统问题才能正确评估 AI alpha:')
        print('  1. gate 不通过时不应 trade（系统 bug）')
        print('  2. K 线 fallback 导致 pnl=0 占位（数据 bug）')
        print('  3. evidence_action=HOLD 但 trade 仍发生（执行 bug）')
    else:
        print('清洁样本 {} 笔，需要看小样本是否能反映真实 alpha。'.format(n_clean))

    conn.close()

if __name__ == '__main__':
    main()