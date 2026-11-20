# _edit_helper.py — v1.0
# 前端 edit/save/toggle/delete HTTP 405 时的安全替代品 (后端 0 个 PUT/PATCH/DELETE)
# 用法:
#   python3 _edit_helper.py set causal-chain BTC::fed_liquidity_tightening --kv magnitude_lo=5 confidence=0.55
#   python3 _edit_helper.py toggle causal-chain BTC::regulatory_crackdown
#   python3 _edit_helper.py list causal-chain BTC::fed_liquidity_tightening
#   python3 _edit_helper.py list signal-cal MU
from pathlib import Path
import os
import sqlite3
import sys
import json
import time
import argparse

DB = os.path.join(
    str(Path(__file__).resolve().parents[2]),
    "trident_event_bus.db",
)
ALLOWED = {
    'causal-chain': ('causal_chains', 'chain_id'),
    'signal-cal':   ('signal_calibration', 'id'),
    'judgment-cal': ('judgment_calibration', 'id'),
    'event-attr':   ('event_move_attribution', 'id'),
}
NUMERIC = {'magnitude_lo','magnitude_hi','confidence','hit_rate','avg_edge_5d',
           'avg_confidence','confidence_calibration','active','n_signals','n_hit','algo_score'}


def setup_audit(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS edit_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        target_type TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL,
        before_json TEXT, after_json TEXT, note TEXT)""")


def backup_row(conn, tbl, key, val):
    row = conn.execute(f"SELECT * FROM {tbl} WHERE {key}=?", (val,)).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute(f"SELECT * FROM {tbl} LIMIT 1").description]
    return dict(zip(cols, row))


def do_set(obj, target_id, kv_pairs, note=''):
    tbl, key = ALLOWED[obj]
    conn = sqlite3.connect(DB, timeout=10)
    setup_audit(conn)
    try:
        before = backup_row(conn, tbl, key, target_id)
        if not before:
            print(f'  [ERR] {obj} {target_id} 不存在')
            return False
        sets, vals = [], []
        for kv in kv_pairs:
            if '=' not in kv:
                print(f'  [ERR] 格式错误: {kv} (应是 k=v)')
                return False
            k, v = kv.split('=', 1)
            if k in NUMERIC:
                v = float(v) if '.' in v else int(v)
            elif v.lower() in ('true', 'false'):
                v = (v.lower() == 'true')
            sets.append(f'{k}=?')
            vals.append(v)
        sql = f"UPDATE {tbl} SET {', '.join(sets)} WHERE {key}=?"
        vals.append(target_id)
        n = conn.execute(sql, vals).rowcount
        after = backup_row(conn, tbl, key, target_id)
        conn.execute(
            "INSERT INTO edit_audit_log (ts,target_type,target_id,action,before_json,after_json,note) VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), obj, target_id, 'set',
             json.dumps(before, default=str), json.dumps(after, default=str),
             note or ' '.join(kv_pairs)))
        conn.commit()
        print(f'  [OK] {obj} {target_id}: {n} 行已更新')
        for fld in ('magnitude_lo', 'magnitude_hi', 'confidence', 'hit_rate', 'active', 'algo_score'):
            if fld in before and before.get(fld) != after.get(fld):
                print(f'       {fld}: {before.get(fld)} -> {after.get(fld)}')
        return True
    finally:
        conn.close()


def do_toggle(obj, target_id, note=''):
    if obj != 'causal-chain':
        print('  [WARN] toggle 暂只支持 causal-chain')
        return False
    conn = sqlite3.connect(DB, timeout=10)
    setup_audit(conn)
    try:
        before = backup_row(conn, 'causal_chains', 'chain_id', target_id)
        if not before:
            print('  [ERR] 不存在')
            return False
        new_active = 0 if before.get('active', 1) == 1 else 1
        conn.execute("UPDATE causal_chains SET active=?, updated_at=strftime('%s','now') WHERE chain_id=?",
                     (new_active, target_id))
        after = backup_row(conn, 'causal_chains', 'chain_id', target_id)
        conn.execute(
            "INSERT INTO edit_audit_log (ts,target_type,target_id,action,before_json,after_json,note) VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), obj, target_id, 'toggle',
             json.dumps(before, default=str), json.dumps(after, default=str), note))
        conn.commit()
        print(f'  [OK] {target_id}: active {before.get("active")} -> {new_active}')
        return True
    finally:
        conn.close()


def do_list(obj, target_id):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        tbl, key = ALLOWED[obj]
        if obj == 'causal-chain':
            row = conn.execute(
                f"SELECT chain_id, asset, trigger_pattern, magnitude_lo, magnitude_hi, confidence, active, hit_count, miss_count FROM {tbl} WHERE chain_id=?",
                (target_id,)).fetchone()
            if not row:
                print('  [WARN] 不存在')
                return
            for k, v in zip(['chain_id','asset','trigger','mag_lo','mag_hi','conf','active','hit','miss'], row):
                print(f'  {k:>10}: {v}')
        elif obj in ('signal-cal', 'judgment-cal'):
            rows = conn.execute(f"SELECT * FROM {tbl} WHERE id=? OR asset=?", (target_id, target_id)).fetchall()
            if not rows:
                print('  [WARN] 不存在')
                return
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {tbl} LIMIT 1").description]
            for r in rows:
                for k, v in zip(cols, r):
                    print(f'  {k:>22}: {v}')
                print()
        elif obj == 'event-attr':
            row = conn.execute(
                f"SELECT id, asset, move_pct, move_kind, algo_score, ai_explanation FROM {tbl} WHERE id=?",
                (target_id,)).fetchone()
            if not row:
                print('  [WARN] 不存在')
                return
            for k, v in zip(['id','asset','pct','kind','score','ai_expl'], row):
                val = str(v) if v is not None else 'None'
                if len(val) > 200:
                    val = val[:200] + '...'
                print(f'  {k:>12}: {val}')
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description='前端 edit 按钮 405 时的安全替代品')
    sub = ap.add_subparsers(dest='action', required=True)
    p_set = sub.add_parser('set')
    p_set.add_argument('obj')
    p_set.add_argument('id')
    p_set.add_argument('--kv', nargs='+', required=True, help='field=value 列表')
    p_set.add_argument('--note', default='')
    p_tog = sub.add_parser('toggle')
    p_tog.add_argument('obj')
    p_tog.add_argument('id')
    p_tog.add_argument('--note', default='')
    p_lis = sub.add_parser('list')
    p_lis.add_argument('obj')
    p_lis.add_argument('id')
    args = ap.parse_args()
    if args.obj not in ALLOWED:
        print(f'可用对象: {", ".join(ALLOWED.keys())}')
        return 1
    if args.action == 'set':
        do_set(args.obj, args.id, args.kv, args.note)
    elif args.action == 'toggle':
        do_toggle(args.obj, args.id, args.note)
    elif args.action == 'list':
        do_list(args.obj, args.id)
    return 0


if __name__ == '__main__':
    sys.exit(main())


