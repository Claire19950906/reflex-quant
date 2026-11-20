# -*- coding: utf-8 -*-
"""v310 P1 —— 验收 #4 / #10 / #12 / #13 / C4 / C8 的行为回归测试。

覆盖四处改动:
  1. news_score_worker         —— import re / 方向幅度语义 / 三态 / 文本恢复 / 品种映射
  2. abnormal_detector         —— multi_factor 签名写进 detail_json 的反应幅度
  3. causal_chain_evaluator    —— T+5 取价锚点从"向前回看"改成"向后数"
  4. calc_event_move_attribution —— 归因候选排序: 事前优先于事后拟合

全部离线: 只用内存库 / 纯函数, 不连网络、不调 LLM、不碰生产 DB。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import abnormal_detector as ad
import calc_event_move_attribution as attr
import causal_chain_evaluator as cce
import news_score_worker as nw
from signals import signal_weight_evaluator as swe


# ── 1. news_score_worker ────────────────────────────────────────────────────

def test_module_imports_re():
    """回归: `_primary_asset` 用 `re.split`, 但模块曾经没 import re → NameError。"""
    assert nw.re is not None
    assert nw._primary_asset("XAU, BTC", "") == "XAU"


def test_decision_values_only_three_states():
    """C8: 只认 BUY/SELL/HOLD —— 'IGNORE' 不是合法枚举。"""
    assert "IGNORE" not in nw.DECISION_VALUES
    assert set(nw.DECISION_VALUES) == {"BUY", "SELL", "HOLD"}


def test_score_is_magnitude_not_signed():
    """score 语义 = 方向幅度: HOLD=0.0, BUY/SELL 都是 **正数**。

    旧版 SELL 记负分 → `score > 0` 数不到 SELL 行 (验收 #13 口径)。
    """
    lex = nw._load_lexicons()
    action, score = nw._score_event("黄金价格暴跌创新低 利空", None, lex)
    assert action == "SELL"
    assert score > 0, "SELL 的分数必须是幅度(正数), 不能是负数"
    assert score <= 1.0

    action, score = nw._score_event("比特币暴涨创新高 利好", None, lex)
    assert action == "BUY"
    assert score > 0

    action, score = nw._score_event("今天发布了新版本说明文档", None, lex)
    assert action == "HOLD"
    assert score == 0.0, "HOLD 的分数必须是 0, 不能带符号"


def test_llm_verdict_wins_over_keywords():
    """LLM 已经给过方向结论时优先用它。"""
    lex = nw._load_lexicons()
    # 文本是看跌的, 但 LLM 判 BUY (情绪分 0) → 采信 LLM
    action, score = nw._score_event("黄金暴跌创新低 利空", ("BUY", 0.0), lex)
    assert action == "BUY"
    assert score >= nw.DIRECTION_THRESHOLD, "方向结论至少带一个最小方向置信度"
    # LLM 判 HOLD 但情绪分够强 → 用情绪分定方向
    action, score = nw._score_event("今天天气不错", ("HOLD", 0.8), lex)
    assert action == "BUY" and score > 0
    # LLM 判 HOLD 且情绪分为 0 → 回落到关键词表
    action, score = nw._score_event("黄金暴跌创新低 利空", ("HOLD", 0.0), lex)
    assert action == "SELL"


def test_text_recovery_chain():
    """headline/body 为空时, 从 raw_news / signal_chain_archive / hermes 恢复正文。"""
    conn = _mk_news_db()
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action) VALUES (1, 77, 100, 'PENDING', '')")
    conn.execute("INSERT INTO raw_news (id, content, gate_main_asset) VALUES (77, '原油价格暴跌创新低 利空', 'WTI')")
    conn.execute("INSERT INTO hermes_observations (id, news_id, observation_type, content) "
                 "VALUES (1, 77, 'news', '原油价格暴跌创新低 利空')")
    conn.commit()
    ctx = nw._load_context(conn)
    row = conn.execute("SELECT * FROM news_events WHERE id=1").fetchone()
    text = nw._event_text(row, ctx)
    assert "原油价格暴跌" in text
    action, score = nw._score_event(text, nw._llm_verdict(row, ctx), nw._load_lexicons())
    assert action == "SELL" and score > 0
    conn.close()


def test_hermes_decision_text_is_excluded():
    """hermes 的 'decision' 类观测是系统自己写的结论, 不能当新闻正文。"""
    conn = _mk_news_db()
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action) VALUES (1, 5, 100, 'PENDING', '')")
    conn.execute("INSERT INTO hermes_observations (id, news_id, observation_type, content) "
                 "VALUES (1, 5, 'decision', 'XAU SELL score=-0.075')")
    conn.commit()
    ctx = nw._load_context(conn)
    row = conn.execute("SELECT * FROM news_events WHERE id=1").fetchone()
    assert "XAU SELL score" not in nw._event_text(row, ctx)
    conn.close()


def test_asset_resolution_order():
    """品种映射: 已有 asset > LLM target_asset > gate_main_asset > 正文白名单 > NONE。"""
    conn = _mk_news_db()
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action) VALUES (1, 1, 1, 'PENDING', '')")
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action, asset) "
                 "VALUES (2, 2, 1, 'PENDING', '', 'BTC')")
    conn.execute("INSERT INTO signal_chain_archive (news_id, target_asset) VALUES (1, 'ETH')")
    conn.execute("INSERT INTO raw_news (id, content, gate_main_asset) VALUES (3, '...', 'XAU')")
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action) VALUES (3, 3, 1, 'PENDING', '')")
    conn.execute("INSERT INTO news_events (id, news_id, ts, status, action) VALUES (4, 4, 1, 'PENDING', '')")
    conn.commit()
    ctx = nw._load_context(conn)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM news_events")}
    assert nw._resolve_asset(rows[1], "", ctx) == "ETH"
    assert nw._resolve_asset(rows[2], "", ctx) == "BTC"
    assert nw._resolve_asset(rows[3], "", ctx) == "XAU"
    assert nw._resolve_asset(rows[4], "黄金价格大涨", ctx) == "XAU"
    conn.close()

def _mk_news_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY, news_id INTEGER, decision_id INTEGER, ts INTEGER,
            source TEXT, asset TEXT, action TEXT, score REAL, is_noise INTEGER,
            status TEXT, headline TEXT, body TEXT, assets TEXT, fetched_at INTEGER
        );
        CREATE TABLE raw_news (
            id INTEGER PRIMARY KEY, content TEXT, gate_main_asset TEXT
        );
        CREATE TABLE signal_chain_archive (
            id INTEGER PRIMARY KEY, news_id INTEGER, news_title TEXT,
            sentiment_score REAL, target_asset TEXT, suggested_action TEXT
        );
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY, news_id INTEGER, sentiment_score REAL,
            suggested_action TEXT
        );
        CREATE TABLE hermes_observations (
            id INTEGER PRIMARY KEY, news_id INTEGER, observation_type TEXT, content TEXT
        );
        """
    )
    return conn


def _seed_file_db(path, rows):
    """把若干 news_events 行写进一个文件库 (带全部上下文表)。"""
    conn = _mk_news_db()
    for r in rows:
        conn.execute(
            "INSERT INTO news_events (id, news_id, ts, status, action, score, headline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", r
        )
    conn.commit()
    dst = sqlite3.connect(str(path))
    conn.backup(dst)
    dst.close()
    conn.close()


def test_run_once_drains_backlog_and_fixes_enums(tmp_path, monkeypatch):
    """C4/C8/#13: PENDING 积压被打掉, 脏 action 被收敛, score 变成方向幅度。"""
    db = tmp_path / "news.db"
    _now = int(time.time())
    _seed_file_db(db, [
        (1, 10, _now, "PENDING", "", 0.0, "黄金价格暴跌创新低 利空"),
        (2, 11, _now, "PENDING", "IGNORE", 0.0, "比特币暴涨创新高 利好"),
        (3, 12, 100, "DONE", "HOLD", 0.0, "今天发布了新版本说明文档"),
    ])
    monkeypatch.setattr(nw, "DB_PATH", db)
    # v332 起 _llm_verdict 返回 None（模型不可用）时**按设计跳过不写假结论**，
    # 所以不打桩会得到 updated=0。这里给它一个「有情绪分、无方向」的桩，
    # 让用例真正测到关键词兜底路径（lexicon 决定 BUY/SELL）。
    monkeypatch.setattr(nw, "_llm_verdict", lambda r, ctx: ("", 0.0))
    out = nw.run_once(limit=100)
    assert out["ok"] is True
    assert out["updated"] == 2, "两条脏 action 必须被处理; 已 DONE 的合法行默认不重算"

    chk = sqlite3.connect(str(db))
    chk.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in chk.execute("SELECT * FROM news_events")}
    assert rows[1]["status"] == "DONE" and rows[2]["status"] == "DONE"
    assert rows[1]["action"] == "SELL" and rows[1]["score"] > 0
    assert rows[2]["action"] == "BUY" and rows[2]["score"] > 0
    assert rows[3]["action"] == "HOLD" and rows[3]["score"] == 0.0
    bad = chk.execute("SELECT COUNT(*) FROM news_events "
                       "WHERE action IS NOT NULL AND action NOT IN ('BUY','SELL','HOLD')").fetchone()[0]
    assert bad == 0
    chk.close()


def test_rescore_done_flag(tmp_path, monkeypatch):
    """--rescore-done: 连 DONE 的行一起重算。"""
    db = tmp_path / "news2.db"
    _seed_file_db(db, [
        (1, 20, 100, "DONE", "HOLD", 0.0, "比特币暴涨创新高 利好"),
    ])
    monkeypatch.setattr(nw, "DB_PATH", db)
    # 同上：不桩住 LLM 就会走「模型不可用 → 跳过」分支，updated 恒为 0
    monkeypatch.setattr(nw, "_llm_verdict", lambda r, ctx: ("", 0.0))
    out = nw.run_once(limit=100, rescore_done=True)
    assert out["updated"] == 1
    chk = sqlite3.connect(str(db))
    action, score = chk.execute("SELECT action, score FROM news_events WHERE id=1").fetchone()
    chk.close()
    assert action == "BUY" and score > 0


# ── 2. abnormal_detector (#4 反应乘子) ─────────────────────────────────────

def test_multi_factor_signal_carries_reaction_amplitude():
    """#4: multi_factor 签名必须带 pct_change / vol_ratio / trigger_type。

    signal_weight_evaluator.get_recent_abnormal 只取 `detail_json LIKE '%pct_change%'`
    的行; multi_factor 占 abnormal_signals 的 85%, 之前这些行全都没有 pct_change
    → reaction_mult 恒等于 1.0 (恒等空转)。
    """
    candles = []
    base = 1_700_000_000_000
    for i in range(40):
        close = 100.0 + i * 0.2
        candles.append([base + i * 180_000, close, close + 0.3, close - 0.3, close, 1000.0 + i])
    # 最后一根大幅放量拉高 → 触发 multi_factor
    candles[-1][1] = 130.0
    candles[-1][2] = 131.0
    candles[-1][3] = 129.0
    candles[-1][4] = 130.0
    candles[-1][5] = 9000.0
    out = ad.eval_assets({"BTC": candles})
    sigs = [s for s in out["signals"] if s["signal_kind"] == "multi_factor"]
    assert sigs, "构造的极端 K 线应当触发 multi_factor"
    detail = sigs[0]["detail"]
    assert "pct_change" in detail
    assert "vol_ratio" in detail
    assert "trigger_type" in detail
    assert detail["pct_change"] is not None


# ── 3. causal_chain_evaluator (#10 前向锚点) ───────────────────────────────

def _ohlcv_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    for i in range(10):
        conn.execute("INSERT INTO ohlcv_daily (asset, ts, close) VALUES (?, ?, ?)",
                     ("BTC", 1000 + i * 86400, 100.0 + i))
    conn.commit()
    return conn


def test_get_close_t_plus_n_is_forward_from_anchor():
    """回归: 旧实现锚在 start_ts-14天 → 取到的是**触发之前**的 K 线。"""
    conn = _ohlcv_db()
    close, ts = cce.get_close_t_plus_n(conn, "BTC", 1000, 4)
    assert ts == 1000 + 4 * 86400, "必须从锚点往后数, 不能往前找"
    assert close == 104.0
    # 旧实现 (锚点 -14 天) 在同一份数据上会取到别处 —— 这里只锁"往后数"这一条
    close0, ts0 = cce.get_close_t_plus_n(conn, "BTC", 1000, 0)
    assert ts0 == 1000 and close0 == 100.0
    conn.close()


def test_t0_bar_is_last_close_before_trigger():
    conn = _ohlcv_db()
    trigger = 1000 + 2 * 86400 + 100     # 第 3 根日线收盘后 100 秒
    close, ts = cce.get_t0_bar(conn, "BTC", trigger)
    assert ts == 1000 + 2 * 86400
    assert close == 102.0
    conn.close()


# ── 4. calc_event_move_attribution (#12 事前优先) ───────────────────────────

def test_rank_candidates_puts_predictive_first():
    """事前候选 (lag>=0) 必须整体排在事后拟合候选 (lag<0) 前面。"""
    scored = [
        (0.90, 1, "s", -60, "asset", True),    # 事后, 分数高
        (0.40, 2, "s", 120, "asset", False),   # 事前, 分数低
        (0.80, 3, "s", -30, "asset", True),    # 事后
        (0.85, 4, "s", 600, "asset", False),   # 事前, 分数高
    ]
    ranked = attr._rank_candidates(scored)
    flags = [x[5] for x in ranked]
    assert flags == [False, False, True, True], "事前候选必须全部排在前面"
    assert ranked[0][1] == 4, "事前组内仍按 score 倒序"


def test_rank_candidates_stable_within_group():
    scored = [
        (0.50, 10, "s", 100, "asset", False),
        (0.50, 11, "s", 100, "asset", False),
    ]
    ranked = attr._rank_candidates(scored)
    assert [x[1] for x in ranked] == [11, 10], "同分同组按 n_id 倒序 (与旧 tuple 倒序一致)"
# ── 5. signal_weight_evaluator (#4 反应乘子真的在动) ─────────────────────────


def _sw_db() -> sqlite3.Connection:
    """最小 signal_weight 闭环库: ai_decisions + abnormal_signals + history。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, news_id INTEGER, target_asset TEXT,
        suggested_action TEXT, sentiment_score REAL, impact_magnitude REAL,
        heat_score REAL, attention_score REAL, extra_models_consensus TEXT,
        peer_reviews TEXT, regime_at_decision TEXT, expected_horizon TEXT,
        ts INTEGER, signal_weight REAL, weight_tier TEXT, weight_explain TEXT,
        weight_updated_at INTEGER, cold_signal INTEGER, is_sibling INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE abnormal_signals (
        id INTEGER PRIMARY KEY, ts INTEGER, asset TEXT, signal_kind TEXT,
        direction TEXT, anchor_ts INTEGER, detail_json TEXT, hint TEXT,
        status TEXT DEFAULT 'open')""")
    conn.execute("""CREATE TABLE signal_weight_history (
        id INTEGER PRIMARY KEY, decision_id INTEGER, computed_at INTEGER,
        trigger_kind TEXT, signal_weight REAL, weight_tier TEXT,
        base_mult REAL, consensus_mult REAL, source_mult REAL,
        reaction_mult REAL, regime_mult REAL, time_mult REAL,
        explain TEXT, abnormal_id INTEGER, source_stats_id INTEGER)""")
    conn.commit()
    return conn


def test_check_aligned_hold_is_not_penalised():
    """HOLD 没有方向主张 —— 市场动了不等于"与决策相反"。

    旧实现对 HOLD 一律判 not-aligned → reaction_mult 走 0.5/0.7 的惩罚分支。
    """
    assert swe._check_aligned("bullish", "HOLD") is True
    assert swe._check_aligned("bearish", "HOLD") is True
    assert swe._check_aligned("bullish", "BUY") is True
    assert swe._check_aligned("bearish", "BUY") is False


def test_reaction_candidate_ids_only_after_window_closed():
    """反应窗口已关闭 + 窗口内有带 pct_change 的异动信号 → 该决策要补算。"""
    conn = _sw_db()
    now = 1_700_000_000
    dec_ts = now - 600                      # 窗口 [dec_ts, dec_ts+300] 已关闭
    conn.execute(
        "INSERT INTO ai_decisions (id, target_asset, suggested_action, ts) "
        "VALUES (1, 'XAU', 'HOLD', ?)", (dec_ts,))
    conn.execute(
        "INSERT INTO abnormal_signals (ts, asset, signal_kind, direction, "
        "detail_json, status) VALUES (?, 'XAU', 'multi_factor', 'bullish', "
        "'{\"pct_change\": 0.05}', 'open')", (dec_ts + 60,))
    conn.commit()
    assert swe._reaction_candidate_ids(conn, now) == [1]
    # 窗口还没关闭 → 不补算(信号可能还没来)
    assert swe._reaction_candidate_ids(conn, dec_ts + 100) == []
    conn.close()


def test_batch_recompute_applies_reaction_multiplier():
    """端到端: 反应乘子必须真的写进 signal_weight_history(≠ 1.0)。"""
    conn = _sw_db()
    now = 1_700_000_000
    dec_ts = now - 600
    conn.execute(
        "INSERT INTO ai_decisions (id, target_asset, suggested_action, ts, "
        "sentiment_score, heat_score) VALUES (1, 'XAU', 'HOLD', ?, 0.6, 50)",
        (dec_ts,))
    conn.execute(
        "INSERT INTO abnormal_signals (ts, asset, signal_kind, direction, "
        "detail_json, status) VALUES (?, 'XAU', 'multi_factor', 'bullish', "
        "'{\"pct_change\": 0.05, \"vol_ratio\": 1.0}', 'open')", (dec_ts + 60,))
    conn.commit()
    stats = swe.batch_recompute(conn, hours=24, now_ts=now)
    assert stats["updated"] >= 1, "反应窗口关闭的 HOLD 决策必须被重算"
    row = conn.execute(
        "SELECT reaction_mult, abnormal_id FROM signal_weight_history "
        "WHERE decision_id=1 ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["reaction_mult"] != 1.0, "反应乘子不能恒等于 1.0 (验收 #4)"
    assert row["abnormal_id"] is not None, "必须记下命中的异动信号 id"
    conn.close()


def test_reaction_candidate_not_recomputed_twice():
    """已经补算过的决策不再重复补算(避免每轮 cron 重写同一批历史行)。"""
    conn = _sw_db()
    now = 1_700_000_000
    dec_ts = now - 600
    conn.execute(
        "INSERT INTO ai_decisions (id, target_asset, suggested_action, ts) "
        "VALUES (1, 'XAU', 'HOLD', ?)", (dec_ts,))
    conn.execute(
        "INSERT INTO abnormal_signals (ts, asset, signal_kind, direction, "
        "detail_json, status) VALUES (?, 'XAU', 'multi_factor', 'bullish', "
        "'{\"pct_change\": 0.05}', 'open')", (dec_ts + 60,))
    conn.commit()
    swe.batch_recompute(conn, hours=24, now_ts=now)
    assert swe._reaction_candidate_ids(conn, now) == []
    conn.close()





# ── 6. causal_chain_evaluator 状态落库 / 预测快照刷新 (#10) ──────────────────


def _cce_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE causal_chains (
        id INTEGER PRIMARY KEY, name TEXT, trigger_words TEXT, active INTEGER DEFAULT 1,
        expected_direction TEXT, magnitude_lo REAL, magnitude_hi REAL)""")
    conn.execute("""CREATE TABLE causal_chain_outcomes (
        id INTEGER PRIMARY KEY, chain_id TEXT, trigger_ts INTEGER, status TEXT,
        trigger_source TEXT, trigger_ref_id INTEGER, trigger_headline TEXT,
        trigger_keywords_matched TEXT,
        predicted_direction TEXT, predicted_mag_lo REAL, predicted_mag_hi REAL,
        eval_deadline_ts INTEGER, actual_close_at_trigger REAL,
        actual_close_at_5d REAL, actual_pct_5d REAL, direction_hit INTEGER,
        magnitude_hit INTEGER, notes TEXT, evaluated_at INTEGER)""")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    # 触发日之后只有 2 根日线 → 凑不满 T+5
    for i in range(3):
        conn.execute("INSERT INTO ohlcv_daily (asset, ts, close) VALUES ('XAU', ?, ?)",
                     (1000 + i * 86400, 100.0 + i))
    conn.execute("INSERT INTO causal_chains (id, name, trigger_words, expected_direction, "
                 "magnitude_lo, magnitude_hi) VALUES (1, '冲突→黄金上行', 'XAU,冲突', 'UP', 1.0, 8.0)")
    conn.execute("INSERT INTO causal_chain_outcomes (id, chain_id, trigger_ts, status, "
                 "predicted_direction, predicted_mag_lo, predicted_mag_hi, eval_deadline_ts) "
                 "VALUES (1, '1', 1000, 'HIT', 'UP', 1.0, 8.0, ?)",
                 (int(time.time()) + 7 * 86400,))
    conn.commit()
    return conn


def _cce_row(conn):
    return conn.execute(
        """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                  trigger_headline, trigger_keywords_matched, predicted_direction,
                  NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                  eval_deadline_ts
           FROM causal_chain_outcomes WHERE id=1""").fetchone()


def test_evaluate_one_resets_stale_verdict_to_pending():
    """#10: 前向取价还没到 T+5 → 状态必须显式写回 PENDING, 不能留着旧结论。

    旧实现直接 return 不动表, 于是上一轮(旧锚点)算出来的 HIT 一直躺在表里,
    被当成新结论统计。
    """
    conn = _cce_db()
    res = cce.evaluate_one(conn, _cce_row(conn), force=True)
    assert res["status"] == "PENDING"
    row = conn.execute("SELECT status, evaluated_at FROM causal_chain_outcomes WHERE id=1").fetchone()
    assert row["status"] == "PENDING", "状态必须落库, 不能保留上一轮的 HIT"
    assert row["evaluated_at"], "落库时必须写 evaluated_at"
    conn.close()


def test_evaluate_one_marks_expired_when_chain_asset_gone():
    """链的资产没有行情 → EXPIRED, 并写回表。"""
    conn = _cce_db()
    conn.execute("UPDATE causal_chains SET trigger_words='ZZZ,冲突' WHERE id=1")
    conn.commit()
    res = cce.evaluate_one(conn, _cce_row(conn), force=True)
    assert res["status"] == "EXPIRED"
    row = conn.execute("SELECT status FROM causal_chain_outcomes WHERE id=1").fetchone()
    assert row["status"] == "EXPIRED"
    conn.close()


def test_resnapshot_predictions_refreshes_stale_direction():
    """预测快照必须跟链表当前口径一致(快照冻结结论, 不冻结推断逻辑版本)。"""
    conn = _cce_db()
    conn.execute("UPDATE causal_chain_outcomes SET predicted_direction='DOWN' WHERE id=1")
    conn.commit()
    out = cce.resnapshot_predictions(conn)
    assert out["updated"] == 1
    row = conn.execute("SELECT predicted_direction FROM causal_chain_outcomes WHERE id=1").fetchone()
    assert row["predicted_direction"] == "UP"
    conn.close()


