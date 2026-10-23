#!/usr/bin/env python3
"""Ablation V7: 深挖 #12 大亏单（纯诊断，不动分析层）

#12 trade: 10-04 06:37:09 entry, 06:57:49 exit, pnl=-0.1597%
查找：entry 时刻的 K 线形态、新闻、funding/oi 突变
"""
import sqlite3, datetime

DB = '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db'

def ts2str(ts, fmt='%m-%d %H:%M:%S'):
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone(datetime.timedelta(hours=8))
    ).strftime(fmt)

def section(title):
    print()
    print('=' * 80)
    print(title)
    print('=' * 80)

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    t = conn.execute(
        "SELECT * FROM quick_sim_trades WHERE id=12"
    ).fetchone()

    section('#12 trade 详情')
    fields = [
        ('asset', t['asset']), ('action', t['action']),
        ('entry_ts', ts2str(t['entry_ts'])),
        ('exit_ts', ts2str(t['exit_ts'])),
        ('entry_price', t['entry_price']),
        ('exit_price', t['exit_price']),
        ('horizon_minutes', t['horizon_minutes']),
        ('pnl_pct', t['pnl_pct']),
        ('max_price (峰值)', t['max_price']),
        ('min_price (谷底)', t['min_price']),
        ('mfe_pct', t['mfe_pct']),
        ('mae_pct', t['mae_pct']),
        ('score', t['score']),
        ('evidence_confidence', t['evidence_confidence']),
        ('gate_passed', t['gate_passed']),
        ('gate_reason', t['gate_reason']),
    ]
    for k, v in fields:
        print('  {:<22}: {}'.format(k, v))

    section('1. 1H K 线 (entry ±2 小时)')
    k1h = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM kline_bars "
        "WHERE symbol='XAU' AND timeframe='1h' "
        "AND ts BETWEEN ? AND ? ORDER BY ts",
        ((t['entry_ts'] - 7200)*1000, (t['exit_ts'] + 7200)*1000)
    ).fetchall()
    for k in k1h:
        body = k['close'] - k['open']
        amp = k['high'] - k['low']
        label = ''
        if k['ts'] <= t['entry_ts']*1000 < k['ts']+3600000: label = '<-- entry'
        if k['ts'] <= t['exit_ts']*1000 < k['ts']+3600000: label = '<-- exit'
        print('  {} o={:>8.2f} h={:>8.2f} l={:>8.2f} c={:>8.2f} '
              'body={:+.2f} amp={:.2f} {}'.format(
                  ts2str(k['ts']/1000), k['open'], k['high'], k['low'], k['close'],
                  body, amp, label))

    section('2. 10m K 线 (entry ±1 小时)')
    k10 = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM kline_bars "
        "WHERE symbol='XAU' AND timeframe='10m' "
        "AND ts BETWEEN ? AND ? ORDER BY ts",
        ((t['entry_ts'] - 3600)*1000, (t['exit_ts'] + 1800)*1000)
    ).fetchall()
    if k10:
        for k in k10:
            body = k['close'] - k['open']
            amp = k['high'] - k['low']
            label = ''
            if k['ts'] <= t['entry_ts']*1000 < k['ts']+600000: label = '<-- entry'
            if k['ts'] <= t['exit_ts']*1000 < k['ts']+600000: label = '<-- exit'
            print('  {} o={:>8.2f} h={:>8.2f} l={:>8.2f} c={:>8.2f} '
                  'body={:+.2f} amp={:.2f} {}'.format(
                      ts2str(k['ts']/1000), k['open'], k['high'], k['low'], k['close'],
                      body, amp, label))
    else:
        print('  (10m K 线缺失，需补 10-03 22:50 → 10-04 数据)')

    section('3. 新闻 (entry ±30min raw_news)')
    news = conn.execute(
        "SELECT id, source, content, ts, gate_main_asset, gate_kind, "
        "gate_verdict, relevance_score, gate_impact_1to100 "
        "FROM raw_news WHERE ts BETWEEN ? AND ? AND status != 'noise' "
        "ORDER BY ts",
        (t['entry_ts'] - 1800, t['exit_ts'] + 1800)
    ).fetchall()
    if news:
        for n in news:
            content_short = (n['content'] or '')[:120]
            print('  {} [{}] main={} kind={} verdict={} rel={:.2f} impact={}'.format(
                ts2str(n['ts']), n['source'], n['gate_main_asset'],
                n['gate_kind'], n['gate_verdict'], n['relevance_score'] or 0,
                n['gate_impact_1to100'] or 0))
            print('    "{}"'.format(content_short))
    else:
        print('  (entry ±30min 无 raw_news)')

    section('4. funding/oi 突变 (entry 前 2H)')
    for metric in ['XAU.funding', 'XAU.oi', 'XAU.ls_ratio', 'XAU.ig_long_pct',
                   'BTC.funding', 'ETH.funding', 'WTI.funding']:
        rows = conn.execute(
            "SELECT ts, value FROM macro_snapshots "
            "WHERE metric_key=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (metric, (t['entry_ts']-7200), t['entry_ts'])
        ).fetchall()
        rows = [(r[0], r[1]) for r in rows if r[1] is not None]
        if len(rows) < 2:
            print('  {}: 数据不足 ({} 条)'.format(metric, len(rows)))
            continue
        first, last = rows[0][1], rows[-1][1]
        delta = last - first
        print('  {}: first={} last={} delta={:+.6f}'.format(
            metric, first, last, delta))

    section('5. AI reasoning (decision 表)')
    decision = conn.execute(
        "SELECT * FROM ai_decisions WHERE id=?", (t['decision_id'],)
    ).fetchone()
    if decision:
        for k in decision.keys():
            v = decision[k]
            if v is None: continue
            v_str = str(v)
            if len(v_str) > 250: v_str = v_str[:250] + '...'
            print('  {}: {}'.format(k, v_str))
    else:
        print('  decision {} 找不到'.format(t['decision_id']))

    section('6. news_events 关联')
    evs = conn.execute(
        "SELECT id, ts, source, score, status, is_noise, news_id "
        "FROM news_events WHERE decision_id=? OR "
        "(ts BETWEEN ? AND ? AND asset='XAU') ORDER BY ts",
        (t['decision_id'], t['entry_ts']-3600, t['entry_ts']+600)
    ).fetchall()
    if evs:
        for e in evs:
            print('  {} {} score={} status={} is_noise={} news_id={}'.format(
                ts2str(e['ts']), e['source'], e['score'], e['status'],
                e['is_noise'], e['news_id']))
    else:
        print('  (news_events 无关联)')

    conn.close()

if __name__ == '__main__':
    main()
