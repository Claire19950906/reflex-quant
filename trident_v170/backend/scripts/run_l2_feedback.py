#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 反思系统 → 其他系统 反馈 chain runner

一次性跑完:
  1. L2.1 verifier_feedback:  E020 → review_insights (counter_factual + active=0)
  2. L2.2 analysis_layer:    E020 → decision_layer_signals
  3. L2.3 kg_improvements:   E020 累计超阈值 → kg_improvements proposal

Usage:
    python3 scripts/run_l2_feedback.py
"""
import sys, os
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(THIS_DIR)
sys.path.insert(0, os.path.join(BACKEND, 'src_python'))
sys.path.insert(0, BACKEND)

import db
import reflection_checkers as rc
import verifier_feedback as vfb
import analysis_layer_feedback as alf
import kg_feedback as kgf


def main():
    conn = db.get_connection()
    import sqlite3
    conn.row_factory = sqlite3.Row

    print('=' * 80)
    print('[L2 Chain Runner] 反思系统主动反馈给其他系统')
    print('=' * 80)

    # 0) 准备工作: 把 E020 写入 decision_errors (确保有数据可推)
    settled = conn.execute("""
      SELECT DISTINCT d.id FROM ai_decisions d
      JOIN quick_sim_trades t ON t.decision_id = d.id
      WHERE t.settled = 1 ORDER BY d.id DESC
    """).fetchall()
    settled_ids = [r[0] for r in settled]
    written = 0
    for did in settled_ids:
        dec = conn.execute("SELECT * FROM ai_decisions WHERE id = ?", (did,)).fetchone()
        dec_dict = {k: dec[k] for k in dec.keys()} if dec else {}
        errors = rc.check_sample_integrity(dec_dict, conn=conn)
        inserted = rc.save_errors(errors, conn=conn)
        written += inserted
    conn.commit()
    print('step 0: E020 written to decision_errors: {}'.format(written))
    print()

    # 1) L2.1 verifier feedback
    print('[Step 1/3] L2.1 verifier_feedback')
    r1 = vfb.apply_e020_to_insights(limit=500, conn=conn)
    vfb.print_feedback_report(r1)
    print()

    # 2) L2.2 analysis_layer
    print('[Step 2/3] L2.2 analysis_layer_feedback')
    r2 = alf.record_e020_to_layer_signals(limit=500, conn=conn)
    alf.print_feedback_report(r2)
    print()

    # 3) L2.3 kg_improvements
    print('[Step 3/3] L2.3 kg_feedback')
    r3 = kgf.check_and_submit_e020_proposal(threshold=5, window_hours=1, conn=conn)
    kgf.print_feedback_report(r3)

    print()
    print('=' * 80)
    print('[L2 Chain 完成]')
    print('  L2.1: {} insights marked'.format(r1['insights_marked']))
    print('  L2.2: {} signals upserted'.format(r2['signals_upserted']))
    print('  L2.3: submitted={} proposal_id={}'.format(r3['submitted'], r3['proposal_id']))
    print('=' * 80)


if __name__ == '__main__':
    main()