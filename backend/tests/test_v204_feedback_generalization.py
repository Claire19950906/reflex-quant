#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v204 反馈层泛化 + R8 测试.

所有测试用临时 SQLite DB, 不污染生产数据.
"""
from __future__ import annotations
import os
import sys
import sqlite3
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'src_python'))


def make_temp_db():
    fd, path = tempfile.mkstemp(suffix='.sqlite', dir='/tmp')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn, path


_SCHEMA = """
CREATE TABLE ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target_asset TEXT,
    suggested_action TEXT,
    trade_gate_reason TEXT DEFAULT '',
    evidence_action TEXT DEFAULT '',
    evidence_confidence REAL DEFAULT 0,
    reasoning_json TEXT DEFAULT ''
);
CREATE TABLE decision_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    error_code TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE quick_sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER, asset TEXT, action TEXT,
    pnl_pct REAL, settled INTEGER DEFAULT 1
);
CREATE TABLE review_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT DEFAULT '', counter_factual TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    verified_at TEXT DEFAULT ''
);
CREATE TABLE decision_layer_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT, sub_layer TEXT, signal_name TEXT,
    asset TEXT, action TEXT,
    n_total INTEGER DEFAULT 0, n_loss INTEGER DEFAULT 0, n_win INTEGER DEFAULT 0,
    last_seen_ts INTEGER, last_decision_id INTEGER, updated_at TEXT
);
CREATE TABLE kg_improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, target TEXT, reason TEXT, confidence REAL,
    sample_size INTEGER, impact TEXT,
    payload TEXT, status TEXT, source TEXT,
    first_seen_at TEXT, last_seen_at TEXT
);
CREATE TABLE reflection_alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, source TEXT, message TEXT, detail TEXT,
    resolved INTEGER DEFAULT 0, resolved_at TEXT, resolved_note TEXT DEFAULT ''
);
"""


def insert_ai_decision(conn, target_asset='XAU', suggested_action='BUY',
                       ts='2026-10-05 12:00:00', gate='', ev_act='BUY',
                       conf=70.0):
    cur = conn.execute(
        "INSERT INTO ai_decisions (created_at, target_asset, suggested_action, "
        "trade_gate_reason, evidence_action, evidence_confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, target_asset, suggested_action, gate, ev_act, conf))
    conn.commit()
    return cur.lastrowid


def insert_trade(conn, decision_id, asset='XAU', action='BUY', pnl=0.5):
    cur = conn.execute(
        "INSERT INTO quick_sim_trades (decision_id, asset, action, pnl_pct, settled) "
        "VALUES (?, ?, ?, ?, 1)",
        (decision_id, asset, action, pnl))
    conn.commit()
    return cur.lastrowid


def insert_decision_error(conn, decision_id, error_code):
    cur = conn.execute(
        "INSERT INTO decision_errors (decision_id, error_code) VALUES (?, ?)",
        (decision_id, error_code))
    conn.commit()
    return cur.lastrowid


def insert_insight(conn, asset='XAU', active=1, created_at='2026-10-05 12:00:00'):
    """插入一条 review_insights。

    v310 修: created_at 必须**显式**给成与决策同一时刻 —— 原先走表默认值
    datetime('now','localtime') (今天), 而决策写死在 '2026-10-05 12:00:00' → 两者
    相差 24 天, 落在 ±48h 关联窗口外 → 一条 insight 都关联不上。
    """
    cur = conn.execute(
        "INSERT INTO review_insights (asset, active, created_at) VALUES (?, ?, ?)",
        (asset, active, created_at))
    conn.commit()
    return cur.lastrowid


class TestV204Policy(unittest.TestCase):
    """1-4: 策略表 + 默认启用 + 映射."""

    def test_01_policy_table_shape(self):
        from review.reflection_feedback_config import DEFAULT_FEEDBACK_POLICY
        self.assertGreaterEqual(len(DEFAULT_FEEDBACK_POLICY), 13)
        for code, p in DEFAULT_FEEDBACK_POLICY.items():
            for k in ('enabled', 'feedback_level', 'veto_insights',
                      'kg_threshold', 'kg_window_hours',
                      'alert_threshold', 'alert_window_hours',
                      'layer', 'sub_layer', 'applies_to', 'description'):
                self.assertIn(k, p, '{} missing key {}'.format(code, k))

    def test_02_event_logic_enabled(self):
        from review.reflection_feedback_config import get_enabled_codes
        enabled = set(get_enabled_codes())
        for code in ('E014', 'E015', 'E016', 'E017', 'E018', 'E020'):
            self.assertIn(code, enabled)

    def test_03_medium_disabled(self):
        from review.reflection_feedback_config import get_enabled_codes
        enabled = set(get_enabled_codes())
        for code in ('E001', 'E002', 'E019'):
            self.assertNotIn(code, enabled)

    def test_04_layer_signal_mapping(self):
        from review.reflection_feedback_config import get_layer_signal
        self.assertEqual(get_layer_signal('E020'),
                          ('data', 'sample_integrity', 'E020'))
        self.assertEqual(get_layer_signal('E014'),
                          ('event_logic', 'reasoning_broken', 'E014'))
        self.assertEqual(get_layer_signal('E016'),
                          ('event_logic', 'cross_asset_wrong', 'E016'))
        self.assertEqual(get_layer_signal('E018'),
                          ('event_logic', 'historical_counterfactual', 'E018'))
        self.assertEqual(get_layer_signal('E004'),
                          ('causal', 'broken', 'E004'))


class TestV204R8(unittest.TestCase):
    """5-8: sample_quality_filter R8 同批次检查."""

    def setUp(self):
        self.conn, self._tmp_path = make_temp_db()
        import db
        self._orig_get_conn = db.get_connection
        db.get_connection = lambda: self.conn

    def tearDown(self):
        import db
        db.get_connection = self._orig_get_conn
        try:
            os.remove(self._tmp_path)
        except Exception:
            pass

    def test_05_r8_keeps_best_of_batch(self):
        """v453 P0-5 起 R8 = 同批保留最优 N 名, 不再是"超量整批拒".

        为什么改断言：旧语义（兄弟数 > 3 → 整批全拒）实测会把一个 4 兄弟批次
        的 4 条决策**全部**拒掉（当日 59 行拒单全出自同一个决策）。v453 改成
        按 evidence_confidence 排名、同分按 id 小者优先，只放前 N 名。
        回滚开关仍在：SAMPLE_R8_MODE=count（见下一条用例）。
        """
        from sample_quality_filter import audit_trades
        ids = [insert_ai_decision(self.conn, target_asset='XAU',
                                   ts='2026-10-05 12:00:0{}'.format(i))
               for i in range(5)]
        t_first = insert_trade(self.conn, ids[0], asset='XAU', action='BUY', pnl=0.5)
        t_last = insert_trade(self.conn, ids[-1], asset='XAU', action='BUY', pnl=0.5)
        audit = audit_trades(conn=self.conn)
        samples = {s['trade_id']: s for s in audit['samples']}
        # 同分同批 → id 最小的排第 1, 放行
        self.assertTrue(samples[t_first]['is_valid_for_alpha'])
        self.assertEqual(samples[t_first]['flags'].get('R8_rank'), 1)
        # 第 5 名 > 上限 3 → 挡下, 理由写明排名
        self.assertFalse(samples[t_last]['is_valid_for_alpha'])
        self.assertEqual(samples[t_last]['flags'].get('R8_rank'), 5)
        self.assertTrue(any('R8:' in r for r in samples[t_last]['reasons']))
        self.assertGreaterEqual(samples[t_last]['flags'].get('R8_siblings', 0), 4)

    def test_05b_r8_count_mode_rollback_switch(self):
        """SAMPLE_R8_MODE=count 应回到旧语义：兄弟数 > 3 即拒（批次第一条也拒）。"""
        from unittest import mock
        from sample_quality_filter import audit_trades
        did = insert_ai_decision(self.conn, target_asset='XAU',
                                  ts='2026-10-05 12:00:00')
        for i in range(4):
            insert_ai_decision(self.conn, target_asset='XAU',
                                ts='2026-10-05 12:00:0{}'.format(i))
        tid = insert_trade(self.conn, did, asset='XAU', action='BUY', pnl=0.5)
        with mock.patch.dict(os.environ, {'SAMPLE_R8_MODE': 'count'}):
            audit = audit_trades(conn=self.conn)
        sample = next(s for s in audit['samples'] if s['trade_id'] == tid)
        self.assertFalse(sample['is_valid_for_alpha'])
        self.assertTrue(any('R8:同批次兄弟决策' in r for r in sample['reasons']))
        self.assertGreaterEqual(sample['flags'].get('R8_siblings', 0), 4)

    def test_06_r8_allows_few_siblings(self):
        from sample_quality_filter import audit_trades
        did = insert_ai_decision(self.conn, target_asset='XAU',
                                  ts='2026-10-05 12:00:00')
        for i in range(2):
            insert_ai_decision(self.conn, target_asset='XAU',
                                ts='2026-10-05 12:00:0{}'.format(i))
        tid = insert_trade(self.conn, did, asset='XAU', action='BUY', pnl=0.5)
        audit = audit_trades(conn=self.conn)
        sample = next(s for s in audit['samples'] if s['trade_id'] == tid)
        self.assertTrue(sample['is_valid_for_alpha'])
        self.assertLessEqual(sample['flags'].get('R8_siblings', 0), 3)

    def test_07_r8_can_be_disabled(self):
        from sample_quality_filter import audit_trades
        did = insert_ai_decision(self.conn, target_asset='XAU',
                                  ts='2026-10-05 12:00:00')
        for i in range(5):
            insert_ai_decision(self.conn, target_asset='XAU',
                                ts='2026-10-05 12:00:0{}'.format(i))
        tid = insert_trade(self.conn, did, asset='XAU', action='BUY', pnl=0.5)
        audit = audit_trades(max_batch_siblings=0, conn=self.conn)
        sample = next(s for s in audit['samples'] if s['trade_id'] == tid)
        self.assertTrue(sample['is_valid_for_alpha'])

    def test_08_r8_only_same_asset(self):
        from sample_quality_filter import audit_trades
        did = insert_ai_decision(self.conn, target_asset='XAU',
                                  ts='2026-10-05 12:00:00')
        for i in range(5):
            insert_ai_decision(self.conn, target_asset='WTI',
                                ts='2026-10-05 12:00:0{}'.format(i))
        tid = insert_trade(self.conn, did, asset='XAU', action='BUY', pnl=0.5)
        audit = audit_trades(conn=self.conn)
        sample = next(s for s in audit['samples'] if s['trade_id'] == tid)
        self.assertTrue(sample['is_valid_for_alpha'])



class TestV204FeedbackLayer(unittest.TestCase):
    """9-15: kg/verifier/analysis/alert 泛化."""

    def setUp(self):
        self.conn, self._tmp_path = make_temp_db()
        import db
        self._orig_get_conn = db.get_connection
        db.get_connection = lambda: self.conn

    def tearDown(self):
        import db
        db.get_connection = self._orig_get_conn
        try:
            os.remove(self._tmp_path)
        except Exception:
            pass

    def test_09_kg_submit_non_e020(self):
        from kg_feedback import submit_error_code_proposal, _has_pending_proposal
        for _ in range(5):
            did = insert_ai_decision(self.conn, target_asset='XAU')
            insert_decision_error(self.conn, did, 'E014')
        result = submit_error_code_proposal('E014', sample_size=5,
                                              window_hours=1, conn=self.conn)
        self.assertTrue(result['submitted'])
        self.assertTrue(_has_pending_proposal('E014', conn=self.conn))

    def test_10_kg_threshold_from_policy(self):
        from kg_feedback import check_and_submit_error_code_proposal
        for _ in range(2):
            did = insert_ai_decision(self.conn, target_asset='XAU')
            insert_decision_error(self.conn, did, 'E014')
        r = check_and_submit_error_code_proposal('E014', conn=self.conn)
        self.assertFalse(r['submitted'])
        self.assertIn('未达阈值', r['reason'])
        self.assertEqual(r['threshold'], 3)

    def test_11_verifier_e014_note_only(self):
        """E014 = 推理链断裂，按 v409.6 策略**只标 note 不 veto**。

        历史坑 (2026-11-18 修测试): 原断言是 `veto_applied is True` + `active==0`,
        那是 v204 当时的策略。v409.6 把 E014 的 veto 明确改成 False
        （见 review/reflection_feedback_config.py：E014 描述的是**那条决策自己的推理链**断了,
        不是"某条洞察带偏了它", 用 veto 连带杀掉洞察等于误伤), 于是这条断言变成
        "锁死一个已被有意推翻的旧行为"。真正该锁的是:
        标记必须落库(insights_marked>=1)、counter_factual 带 E014_CONTAMINATED、
        而 active 保持 1 —— 也就是"标了但不封杀"。
        """
        from verifier_feedback import apply_error_code_to_insights
        ins = insert_insight(self.conn, asset='XAU')
        did = insert_ai_decision(self.conn, target_asset='XAU')
        insert_decision_error(self.conn, did, 'E014')
        result = apply_error_code_to_insights('E014', conn=self.conn)
        self.assertEqual(result['error_code'], 'E014')
        self.assertFalse(result['veto_applied'])
        self.assertGreaterEqual(result['insights_marked'], 1)
        row = self.conn.execute(
            "SELECT counter_factual, active FROM review_insights WHERE id=?",
            (ins,)).fetchone()
        self.assertEqual(row[1], 1)
        self.assertIn('E014_CONTAMINATED', row[0])

    def test_12_verifier_e005_no_veto(self):
        from verifier_feedback import apply_error_code_to_insights
        ins = insert_insight(self.conn, asset='XAU')
        did = insert_ai_decision(self.conn, target_asset='XAU')
        insert_decision_error(self.conn, did, 'E005')
        result = apply_error_code_to_insights('E005', conn=self.conn)
        self.assertFalse(result['veto_applied'])
        row = self.conn.execute(
            "SELECT counter_factual, active FROM review_insights WHERE id=?",
            (ins,)).fetchone()
        self.assertEqual(row[1], 1)
        self.assertIn('E005_CONTAMINATED', row[0])

    def test_13_analysis_layer_uses_policy(self):
        from analysis_layer_feedback import record_error_code_to_layer_signals
        did = insert_ai_decision(self.conn, target_asset='XAU')
        insert_decision_error(self.conn, did, 'E014')
        result = record_error_code_to_layer_signals('E014', conn=self.conn)
        self.assertEqual(result['layer'], 'event_logic')
        self.assertEqual(result['sub_layer'], 'reasoning_broken')
        self.assertEqual(result['signal_name'], 'E014')

    def test_14_alert_e014_warn(self):
        from review.reflection_alert import check_and_alert_error_code
        for _ in range(6):
            did = insert_ai_decision(self.conn, target_asset='XAU')
            insert_decision_error(self.conn, did, 'E014')
        r = check_and_alert_error_code('E014', conn=self.conn)
        self.assertEqual(r['error_code'], 'E014')
        self.assertEqual(r['threshold'], 5)
        self.assertTrue(r['alerted'])
        self.assertEqual(r['level'], 'WARN')

    def test_15_alert_e014_critical(self):
        from review.reflection_alert import check_and_alert_error_code
        for _ in range(11):
            did = insert_ai_decision(self.conn, target_asset='XAU')
            insert_decision_error(self.conn, did, 'E014')
        r = check_and_alert_error_code('E014', conn=self.conn)
        self.assertTrue(r['alerted'])
        self.assertEqual(r['level'], 'CRITICAL')




class TestV204BackwardCompat(unittest.TestCase):
    """16-17: 旧 E020 函数名 + scheduler 多码遍历."""

    def setUp(self):
        self.conn, self._tmp_path = make_temp_db()
        import db
        self._orig_get_conn = db.get_connection
        db.get_connection = lambda: self.conn

    def tearDown(self):
        import db
        db.get_connection = self._orig_get_conn
        try:
            os.remove(self._tmp_path)
        except Exception:
            pass

    def test_16_e020_wrappers(self):
        from verifier_feedback import apply_e020_to_insights
        from kg_feedback import check_and_submit_e020_proposal
        from analysis_layer_feedback import record_e020_to_layer_signals
        from review.reflection_alert import check_and_alert_e020
        insert_insight(self.conn, asset='XAU')
        for _ in range(4):
            did = insert_ai_decision(self.conn, target_asset='XAU',
                                      gate='低于闸门 30.0')
            insert_decision_error(self.conn, did, 'E020')
        r = apply_e020_to_insights(conn=self.conn)
        self.assertEqual(r['error_code'], 'E020')
        r2 = check_and_submit_e020_proposal(conn=self.conn)
        self.assertFalse(r2['submitted'])
        r3 = record_e020_to_layer_signals(conn=self.conn)
        self.assertEqual(r3['layer'], 'data')
        r4 = check_and_alert_e020(conn=self.conn)
        self.assertFalse(r4['alerted'])

    def test_17_scheduler_iterates_codes(self):
        from review import reflection_scheduler as rs
        from review.reflection_scheduler import run_one_cycle
        for _ in range(3):
            did = insert_ai_decision(self.conn, target_asset='XAU')
            insert_decision_error(self.conn, did, 'E014')
            insert_decision_error(self.conn, did, 'E020')
        result = run_one_cycle(conn=self.conn)
        # v310: 断言调度器自己声明的版本常量 (原来硬编码 'v204', 调度器升级到 v231
        # 之后就一直挂, 与被测行为"是否遍历错误码"无关)。
        self.assertEqual(result.get('version'), rs.CYCLE_VERSION)
        self.assertIn('E014', result['enabled_codes'])
        self.assertIn('E020', result['enabled_codes'])
        self.assertIn('E014', result['step1_verifier_by_code'])
        self.assertEqual(result['step3_kg'].get('error_code'), 'E020')


if __name__ == '__main__':
    unittest.main(verbosity=2)
