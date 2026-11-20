"""GUB deep audit - validate real data quality, not just PASS status"""
from pathlib import Path
import os
import sqlite3
import json
import urllib.request
import urllib.error
from collections import Counter

DB = os.path.join(
    str(Path(__file__).resolve().parents[2]),
    "trident_event_bus.db",
)
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row


def hdr(t): print('\n' + '='*70 + '\n' + t + '\n' + '='*70)


# A1: BTC attribution data quality
# v308-audit-exempt: 数据质量审计脚本 —— 刻意读**未过滤**的原始数据来发现污染,
#   加了 ABS(move_pct)<=90 这个审计就永远发现不了污染 (等于把自己变瞎)。
hdr('A1. BTC attribution data quality (Round 3 added 15 rows)')
rows = conn.execute("""SELECT a.id, a.move_ts, a.move_pct, a.move_kind, a.algo_score,
       a.ev_action, a.time_lag_sec, a.ev_news_id, e.headline
FROM event_move_attribution a LEFT JOIN news_events e ON a.ev_news_id=e.id
WHERE a.asset='BTC' ORDER BY a.move_ts DESC""").fetchall()
print(f'  BTC total: {len(rows)}')
unique = len(set(r['ev_news_id'] for r in rows if r['ev_news_id']))
print(f'  unique news: {unique} (ratio {len(rows)}/{unique}={len(rows)/max(unique,1):.1f}x)')
ev_act = Counter(r['ev_action'] for r in rows)
print(f'  ev_action dist: {dict(ev_act)}')
lags = [r['time_lag_sec'] for r in rows if r['time_lag_sec'] is not None]
if lags:
    neg = sum(1 for l in lags if l < 0); pos = sum(1 for l in lags if l > 0)
    print(f'  time_lag: min={min(lags)} max={max(lags)} mean={sum(lags)/len(lags):.0f} ({sum(lags)/len(lags)/86400:.1f}d)')
    print(f'  direction: {neg} negative (news first) vs {pos} positive (move first)')
for r in rows[:5]:
    h = (r['headline'] or 'NULL')[:40]
    print(f'    #{r["id"]}  {r["move_pct"]:+.2f}%  {r["move_kind"]:10}  lag={r["time_lag_sec"]:>10}  news={h}')


# A2: all assets
hdr('A2. 13 assets attribution health')
for r in conn.execute("SELECT asset, COUNT(*) c, COUNT(DISTINCT ev_news_id) u FROM event_move_attribution GROUP BY asset ORDER BY c DESC").fetchall():
    ratio = r['c'] / max(r['u'], 1)
    flag = ' WARN dup' if ratio > 1.5 else ''
    print(f'  {r["asset"]:10}  rows={r["c"]:>3}  unq={r["u"]:>3}  ratio={ratio:.2f}x{flag}')


# A3: outcomes
hdr('A3. causal_chain outcomes (eval credibility)')
outcomes = conn.execute("""SELECT chain_id, status, actual_pct_5d,
       predicted_direction, predicted_mag_lo, predicted_mag_hi, direction_hit, magnitude_hit
FROM causal_chain_outcomes ORDER BY id""").fetchall()
print(f'  status: {dict(Counter(r["status"] for r in outcomes))}')
for r in outcomes:
    a5 = r["actual_pct_5d"]
    a5s = f'{a5:+.2f}%' if a5 is not None else 'NULL'
    plo = r["predicted_mag_lo"]
    phi = r["predicted_mag_hi"]
    plo_s = f'{plo:.1f}' if plo is not None else '?'
    phi_s = f'{phi:.1f}' if phi is not None else '?'
    print(f'    {r["chain_id"]:48}  {r["status"]:10}  actual_5d={a5s}  pred={r["predicted_direction"]:5}  [{plo_s},{phi_s}]  dir_hit={r["direction_hit"]}')


# A4: market_regime (computed on-the-fly by market_regime.py)
hdr('A4. market regime real-time computation')
try:
    from market.market_regime import detect_regimes
    with conn:
        r = detect_regimes(conn, since_date='2026-07-01')
    print(f'  detect_regimes() returned {len(r.get("regimes", []))} regimes:')
    for x in r.get('regimes', [])[:5]:
        print(f'    {x.get("asset"):10}  {x.get("regime"):25}  conf={x.get("confidence")}  since={x.get("since")}')
    if not r.get('regimes'):
        print(f'  raw: {str(r)[:400]}')
except Exception as e:
    print(f'  ERR: {e}')


# A5: 3-dim missed-signal
hdr('A5. 3-dim (A=signal B=direction C=tech) real data')
print('  dim x subcategory:')
for r in conn.execute("SELECT dimension, subcategory, COUNT(*) c FROM trade_reflections_v2 WHERE subcategory IS NOT NULL GROUP BY dimension, subcategory ORDER BY dimension, c DESC").fetchall():
    print(f'    {r["dimension"]}  {r["subcategory"]:30}  n={r["c"]}')
no_sub = conn.execute("SELECT dimension, COUNT(*) FROM trade_reflections_v2 WHERE subcategory IS NULL GROUP BY dimension").fetchall()
print('  (no subcategory, historical):')
for r in no_sub:
    print(f'    {r[0]}  n={r[1]}')


# A6: news impact
hdr('A6. news_impact_evaluate end-to-end (5 latest)')
for r in conn.execute("SELECT id, headline FROM news_events ORDER BY id DESC LIMIT 5").fetchall():
    try:
        d = json.loads(urllib.request.urlopen(f'http://localhost:8100/api/news/impact-evaluate?news_id={r["id"]}', timeout=20).read())
        notes = d.get('honest_notes', [])
        chains_n = len(d.get('causal_chains_used', []))
        print(f'  #{r["id"]}: {r["headline"][:45]}')
        print(f'      dir={d.get("direction")} mag={d.get("magnitude")} conf={d.get("confidence")} chains_used={chains_n} notes={len(notes)}')
        if notes:
            for n in notes[:3]:
                print(f'        - {n}')
    except Exception as e:
        print(f'  #{r["id"]}: ERR {e}')


# A7: attribution page
hdr('A7. /v291/event-move-attribution real output (BTC)')
try:
    d = json.loads(urllib.request.urlopen('http://localhost:8100/api/v291/event-move-attribution?asset=BTC&limit=5', timeout=5).read())
    items = d.get('items') if isinstance(d, dict) else d
    if isinstance(items, list):
        for it in items[:5]:
            h = (it.get('headline') or it.get('ev_headline') or '')[:50]
            print(f'  id={it.get("id")}  pct={it.get("move_pct"):+.2f}  {it.get("move_kind"):10}  algo={it.get("algo_score")}  ev_act={it.get("ev_action")}  h={h}')
    else:
        print(f'  raw: {str(d)[:300]}')
except Exception as e:
    print(f'  ERR {e}')


# A8: cross-table JOIN health
hdr('A8. cross-table JOIN health')
joins = [
    ('causal_chains <-> outcomes',
     "SELECT COUNT(*) FROM causal_chains c INNER JOIN causal_chain_outcomes o ON c.chain_id=o.chain_id"),
    ('critical_moves <-> attribution',
     "SELECT COUNT(*) FROM critical_price_moves m INNER JOIN event_move_attribution a ON m.asset=a.asset AND ABS(m.ts-a.move_ts)<86400"),
    ('news <-> attribution',
     "SELECT COUNT(*) FROM news_events n INNER JOIN event_move_attribution a ON n.id=a.ev_news_id"),
    ('COT <-> attribution',
     "SELECT COUNT(*) FROM cftc_cot_data c INNER JOIN event_move_attribution a ON c.asset=a.asset"),
    ('reflections <-> chain',
     "SELECT COUNT(*) FROM trade_reflections_v2 r WHERE r.chain_id IS NOT NULL"),
    ('ohlcv <-> critical_moves',
     "SELECT COUNT(DISTINCT m.asset) FROM critical_price_moves m INNER JOIN ohlcv_daily o ON m.asset=o.asset"),
    ('critical_moves by asset',
     "SELECT asset, COUNT(*) FROM critical_price_moves GROUP BY asset ORDER BY 2 DESC"),
    ('reflections by dimension',
     "SELECT dimension, COUNT(*) FROM trade_reflections_v2 GROUP BY dimension"),
]
for name, sql in joins:
    try:
        rs = conn.execute(sql).fetchall()
        if len(rs) == 1 and len(rs[0]) == 1:
            print(f'  {name:30}  n={rs[0][0]}')
        else:
            print(f'  {name:30}')
            for r in rs[:8]:
                # Row object -> tuple via keys
                vals = tuple(r[k] for k in r.keys())
                print(f'      {vals}')
    except Exception as e:
        print(f'  {name:30}  ERR {e}')


# A9: regime HTTP diagnose
hdr('A9. regime HTTP failure diagnose')
for path in ['v201/regime/current', 'regime/current', 'macro/context', 'cot/recent']:
    try:
        d = json.loads(urllib.request.urlopen(f'http://localhost:8100/api/{path}', timeout=3).read())
        if isinstance(d, dict):
            print(f'  /api/{path:25}  ok (keys={list(d.keys())[:5]})')
        else:
            print(f'  /api/{path:25}  ok (type={type(d).__name__})')
    except urllib.error.HTTPError as e:
        print(f'  /api/{path:25}  HTTP {e.code}')
    except Exception as e:
        print(f'  /api/{path:25}  ERR {type(e).__name__}')

conn.close()
print('\n' + '='*70)
print('audit done')

