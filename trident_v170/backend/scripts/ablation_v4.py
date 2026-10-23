#!/usr/bin/env python3
"""Ablation V4: 4 项纯计算因果检查（不依赖 LLM，不瞎编）"""
import sqlite3, statistics, datetime

DB = '/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db'

def get_macro_series(conn, metric_key, start_ts=None, end_ts=None):
    q = "SELECT ts, value FROM macro_snapshots WHERE metric_key = ?"
    params = [metric_key]
    if start_ts:
        q += " AND ts >= ?"
        params.append(start_ts)
    if end_ts:
        q += " AND ts <= ?"
        params.append(end_ts)
    q += " ORDER BY ts"
    return conn.execute(q, params).fetchall()

def _valid_rows(rows):
    """过滤掉 value 为 None 的行"""
    return [r for r in rows if r[1] is not None]

def funding_jump(conn, asset, entry_ts, window_sec=3600):
    rows = _valid_rows(get_macro_series(conn, asset + '.funding', entry_ts - window_sec, entry_ts))
    if len(rows) < 2: return None
    return rows[-1][1] - rows[0][1]

def oi_change(conn, asset, entry_ts, window_sec=3600):
    rows = _valid_rows(get_macro_series(conn, asset + '.oi', entry_ts - window_sec, entry_ts))
    if len(rows) < 2 or rows[0][1] == 0: return None
    return (rows[-1][1] - rows[0][1]) / rows[0][1] * 100

def ls_ratio_change(conn, asset, entry_ts, window_sec=3600):
    rows = _valid_rows(get_macro_series(conn, asset + '.ls_ratio', entry_ts - window_sec, entry_ts))
    if len(rows) < 2: return None
    return rows[-1][1] - rows[0][1]

def cot_change(conn, metric_key, entry_ts, window_sec=3600):
    rows = _valid_rows(get_macro_series(conn, metric_key, entry_ts - window_sec, entry_ts))
    if len(rows) < 2: return None
    return rows[-1][1] - rows[0][1]

def news_density(conn, entry_ts, window_sec=1800):
    return conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE ts BETWEEN ? AND ? AND status != 'noise'",
        (entry_ts - window_sec, entry_ts + window_sec)
    ).fetchone()[0]

def fetch_xau_1h_pnl(conn, entry_ts):
    rows = conn.execute(
        "SELECT ts, open, close FROM kline_bars "
        "WHERE symbol='XAU' AND timeframe='1h' "
        "AND ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 2",
        (entry_ts * 1000, (entry_ts + 7200) * 1000)
    ).fetchall()
    if len(rows) < 2: return None
    return (rows[-1][2] - rows[0][1]) / rows[0][1] * 100

def f4(v):
    if v is None: return float('nan')
    return v

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    trades = conn.execute(
        "SELECT id, entry_ts, ROUND(pnl_pct, 5) pnl, entry_price "
        "FROM quick_sim_trades "
        "WHERE asset='XAU' AND settled=1 AND action='BUY' "
        "ORDER BY entry_ts"
    ).fetchall()
    print('XAU BUY trade 总数:', len(trades))
    print()

    hdr = '{:>3} {:<17} {:>9} | {:>9} {:>9} {:>9} {:>7} {:>7} {:>8} {:>8} {:>5} {:>8}'.format(
        '#', 'entry_time', 'pnl%', 'fundXAU', 'fundBTC', 'fundETH',
        'oiXAU%', 'lsXAU', 'goldCOT', 'dxyCOT', 'news', 'k1h%')
    print(hdr)
    print('-' * len(hdr))

    rows_data = []
    for t in trades:
        ets = t['entry_ts']
        fx = funding_jump(conn, 'XAU', ets)
        fb = funding_jump(conn, 'BTC', ets)
        fe = funding_jump(conn, 'ETH', ets)
        oi = oi_change(conn, 'XAU', ets)
        ls = ls_ratio_change(conn, 'XAU', ets)
        gc = cot_change(conn, 'gold.cot_comm_net', ets)
        dc = cot_change(conn, 'dxy.cot_comm_net', ets)
        nd = news_density(conn, ets)
        k1 = fetch_xau_1h_pnl(conn, ets)
        dt = datetime.datetime.fromtimestamp(
            ets, datetime.timezone(datetime.timedelta(hours=8))
        ).strftime('%m-%d %H:%M')
        print('{:>3} {:<17} {:>+9.4f} | {:>+9.6f} {:>+9.6f} {:>+9.6f} {:>+7.2f} {:>+7.3f} {:>+8.0f} {:>+8.0f} {:>5} {:>+8.3f}'.format(
            '#' + str(t['id']), dt, t['pnl'], f4(fx), f4(fb), f4(fe),
            f4(oi), f4(ls), f4(gc), f4(dc), nd, f4(k1)))
        rows_data.append({
            'id': t['id'], 'pnl': t['pnl'], 'fx': fx, 'fb': fb, 'fe': fe,
            'oi': oi, 'ls': ls, 'gc': gc, 'dc': dc, 'nd': nd, 'k1': k1,
        })

    print()
    print('=' * 80)
    print('汇总统计')
    print('=' * 80)

    valid = [r for r in rows_data if r['fx'] is not None]
    print('有 macro 数据的 trade:', len(valid), '/', len(rows_data))
    if not valid: return

    pnl_vals = [r['pnl'] for r in valid]
    fx_vals = [r['fx'] for r in valid]

    if len(valid) >= 3:
        try:
            r = statistics.correlation(fx_vals, pnl_vals)
            print()
            print('[XAU funding 1H jump] vs [trade pnl] 相关性 r = {:+.3f}'.format(r))
            print('  funding 变化均值 = {:+.6f}, 区间 = [{:+.6f}, {:+.6f}]'.format(
                statistics.mean(fx_vals), min(fx_vals), max(fx_vals)))
        except Exception as e:
            print('funding 相关性:', e)

    fb_vals = [r['fb'] for r in valid if r['fb'] is not None]
    fe_vals = [r['fe'] for r in valid if r['fe'] is not None]
    if len(fb_vals) >= 3:
        try:
            r = statistics.correlation([fx_vals[i] for i in range(len(fb_vals))], fb_vals)
            print()
            print('[XAU funding] vs [BTC funding] 相关性 r = {:+.3f}'.format(r))
        except Exception as e:
            print('XAU-BTC funding 相关:', e)
    if len(fe_vals) >= 3:
        try:
            r = statistics.correlation([fx_vals[i] for i in range(len(fe_vals))], fe_vals)
            print('[XAU funding] vs [ETH funding] 相关性 r = {:+.3f}'.format(r))
        except Exception as e:
            print('XAU-ETH funding 相关:', e)

    nd_vals = [r['nd'] for r in valid]
    print()
    print('新闻密度 (entry ±30min):')
    print('  均值 = {:.1f} 条, 区间 = {} ~ {} 条'.format(
        statistics.mean(nd_vals), min(nd_vals), max(nd_vals)))
    if len(valid) >= 3:
        try:
            r = statistics.correlation(nd_vals, pnl_vals)
            print('  vs trade pnl 相关性 r = {:+.3f}'.format(r))
        except Exception:
            pass

    oi_vals = [r['oi'] for r in valid if r['oi'] is not None]
    if oi_vals and len(oi_vals) >= 3:
        try:
            r = statistics.correlation(oi_vals, pnl_vals[:len(oi_vals)])
            print()
            print('[XAU oi 1H 变化率] vs [trade pnl] 相关性 r = {:+.3f}'.format(r))
            print('  oi 变化均值 = {:+.2f}%'.format(statistics.mean(oi_vals)))
        except Exception:
            pass

    gc_vals = [r['gc'] for r in valid if r['gc'] is not None]
    if gc_vals:
        print()
        print('[黄金 COT 商业持仓净变化]: 均值 = {:+.0f}, 区间 = [{:+.0f}, {:+.0f}]'.format(
            statistics.mean(gc_vals), min(gc_vals), max(gc_vals)))

    dc_vals = [r['dc'] for r in valid if r['dc'] is not None]
    if dc_vals:
        print('[DXY COT 商业持仓净变化]: 均值 = {:+.0f}, 区间 = [{:+.0f}, {:+.0f}]'.format(
            statistics.mean(dc_vals), min(dc_vals), max(dc_vals)))

    k1_vals = [r['k1'] for r in valid if r['k1'] is not None]
    if k1_vals:
        k1_avg = statistics.mean(k1_vals)
        pnl_avg = statistics.mean(pnl_vals[:len(k1_vals)])
        print()
        print('[同期 K 线 1H 自然 BUY 模拟] vs [实际 trade]:')
        print('  K 线 1H 模拟平均 pnl = {:+.4f}%'.format(k1_avg))
        print('  实际 trade 平均 pnl   = {:+.4f}%'.format(pnl_avg))
        alpha = pnl_avg - k1_avg
        print('  alpha = {:+.4f}%'.format(alpha))
        if alpha < -0.005:
            print('  -> AI 反向 alpha (trade 比同期自然走势差)')
        elif alpha > 0.005:
            print('  -> AI 有正向 alpha')
        else:
            print('  -> AI 无显著 alpha')

    conn.close()

if __name__ == '__main__':
    main()
