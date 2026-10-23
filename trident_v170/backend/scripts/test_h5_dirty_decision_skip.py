# -*- coding: utf-8 -*-
"""H5 (v231): reflection 缓存失效标记 — 脏 data (quality filter 拒过) decision 的
反思不应再调 LLM 重新生成 lesson, 否则会用脏 evidence 写出错的 lesson.

3 个验证点:
1. sample_quality_filter.is_decision_dirty / get_dirty_decision_ids
2. trade_reflection_llm.enrich_one 跳过脏 data
3. trade_reflection_llm.reproduce 拒绝跑 (返 skipped_reason=decision_dirty)

设计: file-backed isolated DB (不污染生产), mock LLM, 插 1 个 PASS decision +
2 个脏 decision (R1/R4/R7 各拒), 验证 LLM 只对 clean 那个调用.
"""
import os
import sys
import sqlite3
import tempfile


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, '..', 'src_python')
    sys.path.insert(0, src)

    db_path = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False).name
    prev_db = os.environ.get('TRIDENT_DB_PATH')
    os.environ['TRIDENT_DB_PATH'] = db_path
    os.environ['QUICK_SIM_QUALITY_FILTER'] = 'on'
    try:
        import db  # noqa: E402
        import config  # noqa: E402
        import sample_quality_filter as sqf  # noqa: E402
        import trade_reflection_llm as trf  # noqa: E402

        call_log = []
        trf._call_llm_depth = lambda prompt: call_log.append(prompt[:50]) or None

        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        db.migrate(c)

        c.execute(
            "INSERT INTO raw_news (source, content, timestamp, published_at) "
            "VALUES ('t','x',datetime('now'),'')"
        )
        nid = c.execute('SELECT last_insert_rowid()').fetchone()[0]

        for did, reason, asset, conf in [
            (1, config.TRADE_GATE_PASSED_REASON, 'XAU', 30.0),
            (2, '证据不足', 'XAU', 30.0),  # → R1 拒
            (3, config.TRADE_GATE_PASSED_REASON, 'BTC', 5.0),  # → R4+R7 拒
        ]:
            c.execute(
                """INSERT INTO ai_decisions
                   (id, news_id, sentiment_score, suggested_action, reasoning,
                    target_asset, evidence_confidence, trade_gate_reason,
                    event_strength, event_phase, evidence_action, status, created_at)
                   VALUES (?, ?, 0.0, 'BUY', '', ?, ?, ?, 'medium', 'mid', 'BUY',
                           'AUTO_APPROVED', datetime('now', 'localtime'))""",
                (did, nid, asset, conf, reason),
            )
            c.execute(
                """INSERT INTO quick_sim_trades
                   (decision_id, asset, action, gate_passed, entry_price,
                    entry_ts, horizon_minutes, verdict, pnl_pct)
                   VALUES (?, ?, 'BUY', 1, 100.0, 1700000000, 60, 'CORRECT', -0.01)""",
                (did, asset),
            )
        c.commit()

        sqf.audit_recent_rejections(c)
        c.commit()
        rows = c.execute(
            "SELECT decision_id, rule FROM quality_rejections ORDER BY decision_id, rule"
        ).fetchall()
        assert any(r['decision_id'] == 2 and r['rule'] == 'R1' for r in rows), 'expected 2/R1'
        assert any(r['decision_id'] == 3 and r['rule'] == 'R4' for r in rows), 'expected 3/R4'
        assert any(r['decision_id'] == 3 and r['rule'] == 'R7' for r in rows), 'expected 3/R7'
        print('[1] audit 落库: 2→R1, 3→R4+R7 OK')

        assert sqf.is_decision_dirty(c, 1) is False
        assert sqf.is_decision_dirty(c, 2) is True
        assert sqf.is_decision_dirty(c, 3) is True
        assert sqf.get_dirty_decision_ids(c) == {2, 3}
        print('[2] is_decision_dirty: 1=False 2=True 3=True, get_dirty_decision_ids={2, 3} OK')

        os.environ['QUICK_SIM_QUALITY_FILTER'] = 'off'
        assert sqf.is_decision_dirty(c, 2) is False
        assert sqf.get_dirty_decision_ids(c) == set()
        os.environ['QUICK_SIM_QUALITY_FILTER'] = 'on'
        print('[3] off 路径: 不脏 OK')

        for rid, did in [(100, 1), (101, 2), (102, 3)]:
            c.execute(
                """INSERT INTO trade_reflections_v2
                   (id, decision_id, asset, action, pnl_pct, mfe_pct, mae_pct,
                    settled_at, analysis_layer_scores, filter_layer_chain,
                    reasoning_chain, counterfactual, error_layer, error_sub_layer,
                    error_signal, root_cause, lesson, verification,
                    applied_experience_ids, depth_score, llm_depth_reflection,
                    created_at)
                   VALUES (?, ?, 'XAU', 'BUY', -0.01, 0.005, -0.008,
                           datetime('now'), '', '', '', '', 'analysis',
                           'entry', 'sig1', 'cause', 'lesson', '', '', 4, '',
                           datetime('now'))""",
                (rid, did),
            )
        c.commit()

        for rid in (100, 101, 102):
            trf.enrich_one(rid)
        assert len(call_log) == 1, f'期望只 1 次 LLM 调用, 实际 {len(call_log)}'
        print(f'[4] enrich_one: LLM 调用次数 {len(call_log)} (clean r100 调, '
              f'脏 r101/r102 跳过) OK')

        call_log.clear()
        out100 = trf.reproduce(100)
        out101 = trf.reproduce(101)
        out102 = trf.reproduce(102)
        assert out100.get('skipped_reason') is None, f'clean 应无 skip, 实际 {out100}'
        assert out101.get('skipped_reason') == 'decision_dirty', f'脏 r101, 实际 {out101}'
        assert out102.get('skipped_reason') == 'decision_dirty', f'脏 r102, 实际 {out102}'
        assert len(call_log) == 1, f'期望 100 调 1 次, 实际 {len(call_log)}'
        print('[5] reproduce: r100 ok=False skipped=None (LLM mock 失败), '
              'r101/r102 skipped_reason=decision_dirty OK')

        c.close()
        print('\nH5 全部验证通过 (enrich_one + reproduce 缓存失效生效)')
        return 0
    finally:
        if prev_db is not None:
            os.environ['TRIDENT_DB_PATH'] = prev_db
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == '__main__':
    sys.exit(main())
