#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feedback Chain Test: 让 E020 实际写入 decision_errors + 推到 L2.2 + L2.3

跑 24 个 settled decision → 触发 check_sample_integrity → save_errors 写入 → L2.2 feedback → L2.3 kg proposal

这是 L2 完整端到端验证.
"""
import sys, os
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(THIS_DIR)
sys.path.insert(0, os.path.join(BACKEND, 'src_python'))
sys.path.insert(0, BACKEND)

import db
import reflection_checkers as rc
import analysis_layer_feedback as alf


def main():
    conn = db.get_connection()
    import sqlite3
    conn.row_factory = sqlite3.Row

    # 1) 取所有有 settled=1 trade 的 decision
    settled = conn.execute("""
      SELECT DISTINCT d.id FROM ai_decisions d
      JOIN quick_sim_trades t ON t.decision_id = d.id
      WHERE t.settled = 1 ORDER BY d.id DESC
    """).fetchall()
    settled_ids = [r[0] for r in settled]
    print('settled decisions (有 settled trade): {}'.format(len(settled_ids)))

    # 2) 跑 check_sample_integrity + save_errors (写 E020 到 decision_errors)
    written = 0
    for did in settled_ids:
        dec = conn.execute("SELECT * FROM ai_decisions WHERE id = ?", (did,)).fetchone()
        dec_dict = dict(dec) if dec else {}
        errors = rc.check_sample_integrity(dec_dict, conn=conn)
        inserted = rc.save_errors(errors, conn=conn)
        written += inserted
    conn.commit()
    print('E020 written to decision_errors: {}'.format(written))

    # 3) L2.2 feedback: E020 → decision_layer_signals
    print()
    result = alf.record_e020_to_layer_signals(limit=500, conn=conn)
    alf.print_feedback_report(result)

    # 4) 验证：查 decision_layer_signals 表
    print()
    rows = conn.execute("""
      SELECT layer, sub_layer, signal_name, asset, action, n_total, last_decision_id
      FROM decision_layer_signals
      WHERE signal_name = 'E020' ORDER BY n_total DESC
    """).fetchall()
    print('[verify] decision_layer_signals E020 行:')
    for r in rows:
        print('  layer={}/{}/signal={} asset={} action={} n_total={} last_dec={}'.format(
            r[0], r[1], r[2], r[3], r[4], r[5], r[6]))


if __name__ == '__main__':
    main()