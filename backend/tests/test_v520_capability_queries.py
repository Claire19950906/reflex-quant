#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v520：记分卡的指标查询层（`_q_*`）行为测试。

记分卡算错数，几乎都错在**分母**：把 HOLD 当方向、把"桶标签"当品种、
把同一根 K 线上的几十条新闻当独立样本、把没归因的异动算成"归因失败"。
所以这一批不测 SQL 语法，只钉口径：
  ① 分子分母必须是同一把尺子（窗口对齐、去重、按格加权）；
  ② 缺表/缺列的旧库一律降级成 (0, 0)，不许抛异常掀翻整张表；
  ③ `Metric.query` 契约是返回**计数** (k, n)，不是百分比 —— 返回百分比会被再除一次。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import capability_scorecard as cs  # noqa: E402

DDL = """
CREATE TABLE dim_asset(id INTEGER PRIMARY KEY, code TEXT);
CREATE TABLE event_atom(id INTEGER PRIMARY KEY, event_time INTEGER,
                        direction TEXT, asset_id INTEGER);
CREATE TABLE event_impact_curve(event_atom_id INTEGER, window_label TEXT,
                                delta_pct REAL);
CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, decision_type TEXT,
                             intended_action TEXT, actual_outcome_pct REAL);
CREATE TABLE causal_chain_outcomes(id INTEGER PRIMARY KEY, status TEXT,
                                   direction_hit INTEGER,
                                   predicted_direction TEXT);
CREATE TABLE event_move_attribution(id INTEGER PRIMARY KEY, move_id INTEGER,
                                   asset TEXT, move_ts INTEGER, ai_called INTEGER,
                                   ai_explanation TEXT, time_lag_sec INTEGER,
                                   ev_news_id INTEGER, algo_reasons TEXT);
CREATE TABLE critical_price_moves(id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER);
CREATE TABLE news_events(id INTEGER PRIMARY KEY, ts INTEGER, asset TEXT);
CREATE TABLE missed_signals(id INTEGER PRIMARY KEY, actual_outcome_5d REAL,
                            hit INTEGER);
CREATE TABLE trade_reflections_v2(id INTEGER PRIMARY KEY, decision_id INTEGER,
                                  dimension TEXT, subcategory TEXT);
CREATE TABLE decision_errors(id INTEGER PRIMARY KEY, decision_id INTEGER);
CREATE TABLE review_insights(id INTEGER PRIMARY KEY, observe_only INTEGER,
                             active INTEGER, manual_deactivated INTEGER);
CREATE TABLE signal_weight_history(id INTEGER PRIMARY KEY, ai_decision_id INTEGER,
                                  trigger_kind TEXT, decision_space TEXT,
                                  signal_weight REAL);
CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, suggested_action TEXT,
                          target_asset TEXT, truth_verdict TEXT,
                          forward_move_pct REAL, quarantined_at INTEGER,
                          analysis_degraded INTEGER, decision_kind TEXT,
                          reasoning_path TEXT, impact_score REAL,
                          hard_data_evidence TEXT, hard_data_status TEXT,
                          analysis_degraded_reason TEXT, asset_route TEXT);
"""


def _ins(conn, sql, rows=None):
    # rows 为空时直接把多值 INSERT 执行一次（executemany + 空列表不会插任何行）
    if rows:
        conn.executemany(sql, rows)
    else:
        conn.execute(sql)
    conn.commit()


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    yield conn
    conn.close()


@pytest.fixture()
def bare():
    """只有连接、没有任何表 —— 旧库/半成品库的降级路径。"""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


# ── ① 按格加权：同一根 K 线上的几十条新闻算一个样本 ───────────────────
def test_dir_event_1d_weights_by_date_cell_not_by_event(db):
    _ins(db, "INSERT INTO dim_asset(id, code) VALUES (1,'XAU'),(2,'WTI')", [])
    _ins(db, "INSERT INTO event_atom(id, event_time, direction, asset_id) VALUES "
             "(1,1762000000,'bullish',1),"
             "(2,1762003600,'bullish',1),"
             "(3,1762010000,'bullish',1),"
             "(4,1762000000,'bearish',2),"
             "(5,1762100000,'bullish',1),"
             "(6,1762100000,'neutral',1)", [])
    _ins(db, "INSERT INTO event_impact_curve(event_atom_id, window_label, delta_pct) "
             "VALUES (1,'1d',0.4),(2,'1d',0.6),(3,'1d',0.8),(4,'1d',-0.3),"
             "(5,'1d',-0.9),(6,'1d',0.5),(1,'5m',5.0)", [])
    k, n = cs._q_dir_event_1d(db)
    # 三格：11-01/XAU 全多且均涨 → 中；11-01/WTI 全空且均跌 → 中；11-02/XAU 多但跌 → 不中
    assert (k, n) == (2.0, 3)
    assert n == 3          # 4 条新闻只算 1 个样本：分母是格数，不是条数


def test_dir_event_1d_is_empty_without_samples(db):
    assert cs._q_dir_event_1d(db) == (0.0, 0)


# ── ② 方向命中：只有 ENTRY、已结算、方向明确的单子 ─────────────────────
def test_open_direction_counts_only_settled_entries(db):
    _ins(db, "INSERT INTO trade_decisions(id, decision_type, intended_action, "
             "actual_outcome_pct) VALUES "
             "(1,'ENTRY','BUY',2.0),"
             "(2,'ENTRY','SELL',-1.5),"
             "(3,'ENTRY','SELL',0.8),"
             "(4,'HOLD','BUY',0.0),"
             "(5,'ENTRY','BUY',NULL),"
             "(6,'ENTRY','BUY',0.0)", [])
    # HOLD、未结算都出分母；平价既不算中也不算不中
    assert cs._q_open_direction(db) == (2.0, 4)


def test_causal_direction_drops_both_but_any_keeps_it(db):
    _ins(db, "INSERT INTO causal_chain_outcomes(id, status, direction_hit, "
             "predicted_direction) VALUES "
             "(1,'HIT',1,'BOTH'),"
             "(2,'HIT',1,'LONG'),"
             "(3,'MISS',0,'SHORT'),"
             "(4,'EXPIRED',1,'LONG'),"
             "(5,'HIT_WEAK',NULL,'LONG')", [])
    # BOTH 是"波动命中"不是方向命中；EXPIRED / direction_hit=NULL 不进分母
    assert cs._q_causal_direction(db) == (1.0, 2)
    assert cs._q_causal_any(db) == (2.0, 3)


# ── ③ 归因：确认句 / 真因果 / 覆盖率窗口 ───────────────────────────────
def test_attr_confirm_only_counts_ai_called_rows(db):
    _ins(db, "INSERT INTO event_move_attribution(id, ai_called, ai_explanation) VALUES "
             "(1,1,'[确认] 新闻与异动同源'),"
             "(2,1,'[确认] 上游减产'),"
             "(3,1,'[推测] 可能是资金面'),"
             "(4,0,'[确认] 没调 AI 的旧行')", [])
    assert cs._q_attr_confirm(db) == (2.0, 3)


def test_attr_causal_share_needs_prior_news_and_link(db):
    _ins(db, "INSERT INTO event_move_attribution(id, time_lag_sec, ev_news_id) VALUES "
             "(1,0,7),(2,120,NULL),(3,-60,8)", [])
    # 只有"新闻在异动前"且"确实对上了事件"才算真因果
    assert cs._q_attr_causal_share(db) == (1.0, 3)


def test_attr_coverage_aligns_window_and_falls_back_to_asset_time(db):
    _ins(db, "INSERT INTO news_events(id, ts) VALUES (1,1000)", [])
    _ins(db, "INSERT INTO critical_price_moves(id, asset, ts) VALUES "
             "(1,'XAU',1500),"
             "(2,'WTI',900),"
             "(3,'BTC',1600),"
             "(4,'ETH',1700)", [])
    _ins(db, "INSERT INTO event_move_attribution(id, move_id, asset, move_ts) VALUES "
             "(11,1,'XAU',1500),"
             "(12,99,'BTC',1600),"
             "(13,2,'WTI',900)", [])
    # WTI 那条在新闻存档之前：永远不可能被归因，留在分母里必然虚低
    # BTC 那条只能靠 (asset, ts) 兜底命中（回填删旧行 + 重插会换 id）
    assert cs._q_attr_coverage(db) == (2.0, 3)


def test_attr_coverage_is_zero_when_no_news_archive(db):
    _ins(db, "INSERT INTO critical_price_moves(id, asset, ts) VALUES (1,'XAU',1500)", [])
    assert cs._q_attr_coverage(db) == (0.0, 0)


# ── ④ 真值覆盖率：HOLD 没有方向，桶标签永远结算不了 ────────────────────
def test_truth_coverage_excludes_hold_bucket_and_quarantined(db):
    _ins(db, "INSERT INTO ai_decisions(id, suggested_action, target_asset, "
             "truth_verdict, quarantined_at) VALUES "
             "(1,'BUY','XAU','HIT',0),"
             "(2,'BUY','ETH','MISS',0),"
             "(3,'BUY','WTI',NULL,0),"
             "(4,'SELL','NONE','HIT',0),"
             "(5,'HOLD','XAU','HIT',0),"
             "(6,'BUY','BTC','HIT',1)", [])
    # 有方向 4 条，其中桶标签 1 条、隔离复制品 1 条出分母 → n=3，已判 2 条
    assert cs._q_truth_coverage(db) == (2.0, 3)


def test_truth_bucket_gap_reports_the_unsettleable_batch(db):
    _ins(db, "INSERT INTO ai_decisions(id, suggested_action, target_asset, "
             "quarantined_at) VALUES "
             "(1,'BUY','NONE',0),(2,'SELL','none',0),(3,'BUY','',0),(4,'BUY','XAU',0)", [])
    assert cs._q_truth_bucket_gap(db) == (3.0, 3)


def test_market_truth_coverage_covers_hold_too(db):
    _ins(db, "INSERT INTO ai_decisions(id, target_asset, forward_move_pct) VALUES "
             "(1,'XAU',1.2),(2,'WTI',NULL),(3,'NONE',3.0),(4,'',1.0),(5,NULL,2.0)", [])
    # 只排除"没有标的"的行；HOLD 之后市场也会走，照样进分母
    assert cs._q_market_truth_coverage(db) == (1.0, 2)


def test_missed_settle_requires_outcome(db):
    _ins(db, "INSERT INTO missed_signals(id, actual_outcome_5d, hit) VALUES "
             "(1,2.0,1),(2,-1.0,0),(3,NULL,NULL),(4,0.5,NULL)", [])
    # 有 5 日结果的 3 条进分母，只有 2 条判了 hit（0.5% 但未判 → 分母里的缺口）
    assert cs._q_missed_settle(db) == (2.0, 3)


# ── ⑤ 反思：C 类是技术故障，不算"交易错因细分" ────────────────────────
def test_reflection_concentration_drops_technical_failures(db):
    _ins(db, "INSERT INTO trade_reflections_v2(id, decision_id, dimension, subcategory) "
             "VALUES (1,1,'A','pattern_recognition'),(2,2,'A','pattern_recognition'),"
             "(3,3,'A','pattern_recognition'),(4,4,'A','position_sizing'),"
             "(5,5,'B','pattern_recognition'),(6,6,'B','pattern_recognition'),"
             "(7,7,'C','data_feed_timeout'),(8,8,'C','data_feed_timeout'),"
             "(9,9,'A',''),(10,10,'A',NULL)", [])
    assert cs._q_reflection_concentration(db) == (5.0, 6)


def test_error_code_coverage_joins_by_decision_id(db):
    _ins(db, "INSERT INTO trade_reflections_v2(id, decision_id, dimension, subcategory) "
             "VALUES (1,11,'A','p1'),(2,12,'A','p1'),(3,13,'B','p2'),"
             "(4,14,'C','p3'),(5,15,'A','')", [])
    _ins(db, "INSERT INTO decision_errors(id, decision_id) VALUES (1,11),(2,13),(3,14)", [])
    assert cs._q_error_code_coverage(db) == (2.0, 3)


# ── ⑥ 池子：降级 / 无标的 / 隔离 不能混成一个分母 ─────────────────────
def test_real_analysis_share_penalises_fallback_templates(db):
    _ins(db, "INSERT INTO ai_decisions(id, reasoning_path, analysis_degraded, "
             "decision_kind, quarantined_at) VALUES "
             "(1,'事件不直接命中核心监控品种',0,'analysis',0),"
             "(2,'LLM 未返回可用分析',0,'analysis',0),"
             "(3,'服务端兜底模板',0,'analysis',0),"
             "(4,'通胀超预期压制降息预期，利多美元利空金',0,'analysis',0),"
             "(5,'通胀超预期压制降息预期',1,'analysis',0),"
             "(6,'通胀超预期压制降息预期',0,'routing_miss',0),"
             "(7,'通胀超预期压制降息预期',0,'analysis',1)", [])
    # 降级、无标的、隔离三类出分母，只剩 4 条里 1 条真分析
    assert cs._q_real_analysis_share(db) == (1.0, 4)


def test_impact_real_share_treats_default_5_as_missing(db):
    _ins(db, "INSERT INTO ai_decisions(id, impact_score, analysis_degraded, "
             "decision_kind) VALUES (1,5.0,0,'analysis'),(2,7.5,0,'analysis'),"
             "(3,NULL,0,'analysis'),(4,9.0,1,'analysis')", [])
    # 5.0 是"模型没返回"的兜底值，不能当"判断为中性"
    assert cs._q_impact_real_share(db) == (1.0, 3)


def test_hard_data_share_counts_non_empty_evidence(db):
    _ins(db, "INSERT INTO ai_decisions(id, hard_data_evidence, analysis_degraded) VALUES "
             "(1,'CFTC 持仓 +12%',0),(2,'',0),(3,NULL,0),(4,'CPI 3.2%',1)", [])
    assert cs._q_hard_data_share(db) == (1.0, 3)


def test_no_target_share_is_case_insensitive(db):
    _ins(db, "INSERT INTO ai_decisions(id, target_asset) VALUES "
             "(1,'NONE'),(2,'none'),(3,''),(4,'XAU'),(5,NULL)", [])
    assert cs._q_no_target_share(db) == (2.0, 5)


def test_degraded_share_counts_only_analysis_pool(db):
    _ins(db, "INSERT INTO ai_decisions(id, analysis_degraded, decision_kind, "
             "quarantined_at) VALUES "
             "(1,1,'analysis',0),(2,1,'analysis',0),(3,0,'analysis',0),"
             "(4,1,'routing_miss',0),(5,1,'analysis',1),(6,0,'analysis',0)", [])
    assert cs._q_degraded_share(db) == (2.0, 4)


# ── ⑦ 观察型洞察：模块取不到要退回查库，不能静默成 n=0 ─────────────────
def test_observe_only_share_falls_back_to_the_table(db, monkeypatch):
    def boom():
        raise ImportError("no module")
    monkeypatch.setattr(cs, "_insight_hypothesis", boom)
    _ins(db, "INSERT INTO review_insights(id, observe_only, active, manual_deactivated) "
             "VALUES (1,1,1,0),(2,0,1,0),(3,1,0,0),(4,1,1,1)", [])
    assert cs._q_observe_only_share(db) == (1.0, 2)


def test_observe_only_share_uses_module_stats_when_available(db, monkeypatch):
    class Stub:
        @staticmethod
        def pool_stats(conn):
            return {"observe_only": 3, "active": 10}
    monkeypatch.setattr(cs, "_insight_hypothesis", lambda: Stub)
    assert cs._q_observe_only_share(db) == (3.0, 10)


def test_observe_only_share_degrades_when_stats_query_fails(db, monkeypatch):
    class Stub:
        @staticmethod
        def pool_stats(conn):
            raise sqlite3.Error("no such table")
    monkeypatch.setattr(cs, "_insight_hypothesis", lambda: Stub)
    # 统计查询炸了也要退回直接查库（这里是空表 → 0 样本，而不是抛异常）
    assert cs._q_observe_only_share(db) == (0.0, 0)


# ── ⑧ 权重：并列值本身就是问题，所以报"有多少个不同值" ───────────────
def test_weight_resolution_counts_distinct_values_in_the_pool(db):
    _ins(db, "INSERT INTO ai_decisions(id, forward_move_pct, target_asset) VALUES "
             "(1,1.0,'XAU'),(2,2.0,'XAU'),(3,3.0,'WTI'),(4,NULL,'XAU'),(5,1.0,'NONE')", [])
    _ins(db, "INSERT INTO signal_weight_history(id, ai_decision_id, trigger_kind, "
             "decision_space, signal_weight) VALUES "
             "(1,1,'insert','ai',0.40),(2,2,'insert','ai',0.40),"
             "(3,3,'insert','ai',0.55),(4,4,'insert','ai',0.70),"
             "(5,5,'insert','ai',0.90),"
             "(6,1,'update','ai',0.11),(7,1,'insert','manual',0.22)", [])
    # 无行情、无标的、非 insert、非 ai 空间都出分母 → 3 对里只有 2 个不同权重
    assert cs._q_weight_resolution(db) == (2.0, 3)


def test_evidence_headline_share_is_headline_only(db):
    _ins(db, "INSERT INTO event_move_attribution(id, algo_reasons) VALUES "
             "(1,'evidence=headline; lag=12'),"
             "(2,'evidence=headline; lag=3'),"
             "(3,'evidence=body; lag=1')", [])
    # 正文里出现不算"原因"，只算背景
    assert cs._q_evidence_headline_share(db) == (2.0, 3)


# ── ⑨ 两个外挂模块的契约：返回计数、不返回百分比 ───────────────────────
def test_signal_weight_prefers_bucket_rho_and_falls_back(db, monkeypatch):
    class StubCa:
        payload = {}

        @staticmethod
        def _weight_eval_stats(conn):
            return StubCa.payload
    monkeypatch.setitem(sys.modules, "capability_acceptance", StubCa)
    StubCa.payload = {"rho_bucket": 0.288, "rho_bucket_n": 40,
                      "rho_excess": 0.197, "ex_ante_n_excess": 500}
    assert cs._q_signal_weight(db) == (0.288, 40)
    StubCa.payload = {"rho_bucket": None, "rho_bucket_n": 0,
                      "rho_excess": 0.197, "ex_ante_n_excess": 500}
    assert cs._q_signal_weight(db) == (0.197, 500)     # 退化到超额口径
    StubCa.payload = {"rho_bucket": None, "rho_bucket_n": 0,
                      "rho_excess": None, "ex_ante_n_excess": 500}
    assert cs._q_signal_weight(db) == (0.0, 500)       # 没值也不许返回 None

    class Boom:
        @staticmethod
        def _weight_eval_stats(conn):
            raise RuntimeError("炸了")
    monkeypatch.setitem(sys.modules, "capability_acceptance", Boom)
    assert cs._q_signal_weight(db) == (0.0, 0)


def test_independent_direction_returns_counts_not_pct(db, monkeypatch):
    class Stub:
        @staticmethod
        def load_trade_rows(conn):
            return []

        @staticmethod
        def summarize(rows, tag):
            return {"independent": {"k": 9, "n": 30}}
    monkeypatch.setitem(sys.modules, "opt_metrics", Stub)
    # 返回 9 而不是 30.0：百分比由 evaluate_metric 自己算，这里再除一次就错
    assert cs._q_independent_direction(db) == (9.0, 30)


def test_independent_direction_degrades_to_nan_when_module_missing(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "opt_metrics", None)
    k, n = cs._q_independent_direction(db)
    assert n == 0 and k != k          # NaN：明确表示"算不出来"，不是 0 分


def test_shadow_queries_are_safe_without_the_judgement_module(db, monkeypatch):
    monkeypatch.setattr(cs, "_jb", None)
    assert cs._q_shadow_hit(db) == (0.0, 0)
    assert cs._q_shadow_ba(db) == (0.0, 0)
    assert cs._q_shadow_ba_ci(db) == (None, None)
    assert cs._q_shadow_net(db) == (0.0, 0)
    assert cs._q_shadow_net_ci(db) == (None, None)


# ── ⑩ 旧库 / 半成品库：缺表一律 (0, 0)，不许抛 ─────────────────────────
@pytest.mark.parametrize("fn", [
    cs._q_real_analysis_share, cs._q_evidence_headline_share, cs._q_weight_resolution,
    cs._q_impact_real_share, cs._q_hard_data_share, cs._q_no_target_share,
    cs._q_error_code_coverage, cs._q_degraded_share, cs._q_observe_only_share,
])
def test_missing_tables_degrade_to_zero_samples(bare, fn, monkeypatch):
    monkeypatch.setattr(cs, "_insight_hypothesis", lambda: None)
    assert fn(bare) == (0.0, 0)


# ── ⑪ detail 说明：结构性事实必须写出来，不能被静默略过 ───────────────
def test_s17_detail_splits_degraded_reflections_out(db):
    _ins(db, "INSERT INTO ai_decisions(id, analysis_degraded) VALUES (1,0),(2,0),(3,1)", [])
    _ins(db, "INSERT INTO trade_reflections_v2(id, decision_id, dimension, subcategory) "
             "VALUES (1,1,'A','p1'),(2,2,'A','p1'),(3,3,'A','p1')", [])
    _ins(db, "INSERT INTO decision_errors(id, decision_id) VALUES (1,1)", [])
    txt = cs._s17_detail(db)
    assert "非降级池 1/2" in txt and "另有 1 条" in txt and "降级决策" in txt


def test_s20_detail_branches_on_column_presence(db):
    # 旧库没有 hard_data_status 列 → 必须说"未回填"，不能假装数据是 0
    db.execute("ALTER TABLE ai_decisions DROP COLUMN hard_data_status")
    assert cs._s20_detail(db) == "hard_data_status 未回填（旧库）"
    db.execute("ALTER TABLE ai_decisions ADD COLUMN hard_data_status TEXT")
    _ins(db, "INSERT INTO ai_decisions(id, target_asset, hard_data_evidence, "
             "hard_data_status, analysis_degraded) VALUES "
             "(1,'XAU','x','ok',0),(2,'CN_STOCK','','no_local_feed',0)", [])
    txt = cs._s20_detail(db)
    assert "ok 1" in txt and "缺得最多的品种: CN_STOCK" in txt


def test_s18_detail_reports_route_field_gap(db):
    _ins(db, "INSERT INTO ai_decisions(id, target_asset, analysis_degraded_reason, "
             "asset_route) VALUES (1,'NONE','no_route',NULL),(2,'none','',NULL),"
             "(3,'XAU','',NULL)", [])
    txt = cs._s18_detail(db)
    assert "no_route 1" in txt and "asset_route 为空 2/2" in txt


# ── ⑫ 参考口径：天花板与"可满足样本上的真实表现" ─────────────────────
def test_attr_ceiling_needs_same_asset_news_inside_the_window(db):
    _ins(db, "INSERT INTO news_events(id, ts, asset) VALUES "
             "(1,1400,'XAU'),"        # 异动前 100 秒 → 算"可归因"
             "(2,5200,'XAU'),"        # 比 m.ts 晚 4000 秒 → 超出 +3600 窗口
             "(3,5600,'WTI')", [])    # 同样只落在窗口之外
    _ins(db, "INSERT INTO critical_price_moves(id, asset, ts) VALUES "
             "(1,'XAU',1500),"
             "(2,'WTI',1600),"
             "(3,'ETH',900)", [])     # 新闻存档之前 → 出分母
    # XAU 命中；WTI 有同品种新闻但时间在 +3600 之外 → 不命中；ETH 出分母
    assert cs._q_attr_ceiling(db) == (1.0, 2)


def test_attr_on_satisfiable_takes_only_satisfiable_moves(db):
    _ins(db, "INSERT INTO news_events(id, ts, asset) VALUES (1,1400,'XAU')", [])
    _ins(db, "INSERT INTO critical_price_moves(id, asset, ts) VALUES "
             "(1,'XAU',1500),(2,'WTI',1600)", [])
    _ins(db, "INSERT INTO event_move_attribution(id, move_id, asset, move_ts) VALUES "
             "(11,1,'XAU',1500)", [])
    # 分母只剩"有同品种新闻、本该归得了"的那一条，且它确实归上了
    assert cs._q_attr_on_satisfiable(db) == (1.0, 1)


def test_weight_resolution_with_asset_splits_out_no_asset(db):
    _ins(db, "INSERT INTO ai_decisions(id, forward_move_pct, target_asset) VALUES "
             "(1,1.0,'XAU'),(2,2.0,'WTI'),(3,1.0,'NONE'),(4,NULL,'XAU')", [])
    _ins(db, "INSERT INTO signal_weight_history(id, ai_decision_id, trigger_kind, "
             "decision_space, signal_weight) VALUES "
             "(1,1,'insert','ai',0.3),(2,2,'insert','ai',0.4),"
             "(3,3,'insert','ai',0.1),(4,4,'insert','ai',0.6)", [])
    # 无标的与无行情都出分母：只剩 2 对，2 个不同值
    assert cs._q_weight_resolution_with_asset(db) == (2.0, 2)
    assert cs._q_weight_resolution_with_asset(sqlite3.connect(":memory:")) == (0.0, 0)


def test_causal_balanced_averages_up_and_down_rates(db):
    _ins(db, "INSERT INTO causal_chain_outcomes(id, status, direction_hit, "
             "predicted_direction) VALUES "
             "(1,'HIT',1,'UP'),(2,'MISS',0,'UP'),(3,'MISS',0,'UP'),"
             "(4,'HIT',1,'DOWN'),(5,'HIT',1,'DOWN'),"
             "(6,'HIT',1,'BOTH'),(7,'HIT',NULL,'UP')", [])
    k, n = cs._q_causal_balanced(db)
    # UP 1/3、DOWN 2/2 → 平衡 = (0.333+1)/2 = 0.667，n 只数 UP/DOWN 的 5 条
    assert n == 5
    assert k == pytest.approx(0.6666666 * 5, rel=1e-3)


def test_causal_balanced_is_one_sided_when_only_one_direction(db):
    _ins(db, "INSERT INTO causal_chain_outcomes(id, status, direction_hit, "
             "predicted_direction) VALUES (1,'HIT',1,'UP'),(2,'HIT',1,'UP')", [])
    # 只有一个方向时"平衡"等于该方向的命中率 —— 它不是纠偏，只是如实报
    assert cs._q_causal_balanced(db) == (2.0, 2)


def test_reference_block_degrades_on_empty_database(bare):
    # 参考口径里的两条带窗口查询本身不吞异常（会抛 no such table），
    # 但对外契约由 compute_reference 保证：空库/旧库全部 pct=None，不许抛到报告层
    out = cs.compute_reference(bare)
    assert [r["id"] for r in out] == [r[0] for r in cs.REFERENCE_METRICS]
    assert all(r["pct"] is None and r["den"] == 0 for r in out)


# ── ⑭ 全表体检：任何一个指标的空库行为都只能是"样本不足"，不能崩 ──────
def test_metric_catalogue_is_self_describing():
    ids = [m.id for m in cs.METRICS]
    assert len(ids) == len(set(ids))
    for m in cs.METRICS:
        assert m.definition.strip(), f"{m.id} 没有口径说明，delta 无法判断口径变更"
        assert m.gate > 0 and callable(m.query)
        assert m.kind in ("ratio", "share", "spearman", "expectancy")


def test_every_metric_degrades_to_thin_on_empty_database(bare):
    # 空库是常态（新装、回滚、测试库），任何一个查询炸出去都会让整张表拿不到
    for m in cs.METRICS:
        r = cs.evaluate_metric(bare, m)
        assert r.status == cs.ST_THIN, f"{m.id} 在空库上给出了 {r.status}"
        assert r.n == 0 and r.value is None


# ── ⑬ 端到端：查询 → evaluate_metric 必须再除一次分母 ──────────────────
def test_query_feeds_evaluate_metric_as_counts(db):
    _ins(db, "INSERT INTO ai_decisions(id, suggested_action, target_asset, "
             "truth_verdict, quarantined_at) VALUES "
             "(1,'BUY','XAU','HIT',0),(2,'BUY','ETH','HIT',0),(3,'BUY','WTI',NULL,0)", [])
    m = cs.Metric(id="S8", name="真值覆盖率", kind="ratio", target=60.0, gate=3,
                  baseline=50.0, higher_better=True, unit="%",
                  query=cs._q_truth_coverage, definition="已判胜负/可结算有方向")
    r = cs.evaluate_metric(db, m)
    assert r.n == 3
    assert r.value == pytest.approx(200.0 / 3.0)      # 2/3 → 66.7%，不是 2
    # 点估计过线，但 3 个样本的区间下界压不住基准 → 只算接近达标
    assert r.status == cs.ST_NEAR
