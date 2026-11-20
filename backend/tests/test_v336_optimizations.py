# -*- coding: utf-8 -*-
"""v336 优化模块的纯函数测试（全离线，不碰生产库）。

覆盖：
  · opt_metrics   —— Wilson / 独立信号去重 / 桶标的剔除
  · opt_direction —— no_direction 细分 / 两段式预筛
  · opt_sector_beta —— 板块 beta 判定
  · opt_move_shape  —— 冲击形状（spike=a攻击 / 匀速=drift / 冲高回落=irregular）
  · opt_weight_diag —— Spearman（含并列秩）
  · opt_asset_cover —— 严格品种抽取（歧义/公司名陷阱）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import opt_metrics
import opt_direction
import opt_sector_beta
import opt_move_shape
import opt_weight_diag
import opt_asset_cover


# ---------------------------------------------------------------- opt_metrics

def test_wilson_interval_contains_point_estimate():
    lo, hi = opt_metrics.wilson(157, 391)
    assert 0 < lo < 40.2 < hi < 100


def test_wilson_empty_sample():
    assert opt_metrics.wilson(0, 0) == (None, None)


def test_dedupe_collapses_same_hour_same_direction():
    rows = [
        {"asset": "BTC", "direction": "BUY", "ts": 1000, "hit": 1},
        {"asset": "BTC", "direction": "BUY", "ts": 1100, "hit": 0},  # 同一小时 → 丢弃
        {"asset": "BTC", "direction": "BUY", "ts": 4000, "hit": 1},  # 下一小时 → 保留
        {"asset": "BTC", "direction": "SELL", "ts": 1100, "hit": 1},  # 反方向 → 保留
        {"asset": "ETH", "direction": "BUY", "ts": 1100, "hit": 1},   # 换品种 → 保留
    ]
    out = opt_metrics.dedupe_independent(rows)
    assert len(out) == 4
    assert out[0]["hit"] == 1  # 保留首条


def test_dedupe_drops_rows_without_direction():
    rows = [{"asset": "BTC", "direction": "HOLD", "ts": 1000, "hit": 1}]
    assert opt_metrics.dedupe_independent(rows) == []


def test_split_tradable_excludes_bucket_assets():
    rows = [{"asset": "BTC"}, {"asset": "US_STOCK"}, {"asset": "NONE"}]
    ok, bucket = opt_metrics.split_tradable(rows)
    assert len(ok) == 1 and len(bucket) == 2


def test_summarize_reports_compression():
    rows = [{"asset": "BTC", "direction": "BUY", "ts": 1000 + i, "hit": 0} for i in range(10)]
    rep = opt_metrics.summarize(rows, "t")
    assert rep["naive"]["n"] == 10
    assert rep["independent"]["n"] == 1
    assert rep["compression"] == 10.0
    assert rep["naive"]["pct"] == 0.0


# ---------------------------------------------------------------- opt_direction

def test_no_direction_asset_role_mislabel():
    # BTC 明明在核心品种里，却被判成"副品种" —— 这是角色错标，不是判断
    r = "[MiniMax-M3] [副品种 BTC] 事件不直接命中本系统核心监控品种（BTC/XAU/WTI/ETH/SOL）"
    assert opt_direction.classify_no_direction(r, "BTC") == opt_direction.REASON_ASSET_ROLE


def test_no_direction_model_hold():
    r = "[MiniMax-M3] 观望：该新闻不足以给出明确方向"
    assert opt_direction.classify_no_direction(r, "XAU") == opt_direction.REASON_MODEL_HOLD


def test_no_direction_schema_empty():
    assert opt_direction.classify_no_direction("", "XAU") == opt_direction.REASON_SCHEMA


def test_prefilter_blocks_bucket_and_noise():
    ok, why = opt_direction.prefilter_worth_llm({"asset": "NONE", "is_noise": 0, "headline": "美联储降息预期升温"})
    assert (ok, why) == (False, "asset_bucket")
    ok, why = opt_direction.prefilter_worth_llm({"asset": "BTC", "is_noise": 1, "headline": "比特币突破十万"})
    assert (ok, why) == (False, "noise")


def test_prefilter_keeps_directional_event():
    ok, why = opt_direction.prefilter_worth_llm({"asset": "BTC", "is_noise": 0, "headline": "比特币 ETF 净流入创新高"})
    assert (ok, why) == (True, "ok")


def test_prefilter_blocks_headline_without_cue():
    ok, why = opt_direction.prefilter_worth_llm({"asset": "BTC", "is_noise": 0, "headline": "某公司发布季度报告摘要"})
    assert (ok, why) == (False, "no_directional_cue")


# ---------------------------------------------------------------- opt_sector_beta

def test_classify_beta_strong():
    c = opt_sector_beta.classify_beta(2.0, 1.8)
    assert c["label"] == "sector_beta" and c["ratio"] == 0.9


def test_classify_beta_opposite_sign_is_idiosyncratic():
    c = opt_sector_beta.classify_beta(2.0, -1.8)
    assert c["label"] == "idiosyncratic" and c["same_sign"] is False


def test_classify_beta_tilted():
    c = opt_sector_beta.classify_beta(2.0, 0.8)
    assert c["label"] == "beta_tilted"


def test_classify_beta_no_data():
    assert opt_sector_beta.classify_beta(2.0, None)["label"] == "no_data"


# ---------------------------------------------------------------- opt_move_shape

def _bars(path, start=1794500000, step=60):
    return [(start + i * step, v) for i, v in enumerate(path)]


def test_fit_shape_detects_attack():
    # 前 4 根冲到峰值，然后横住 —— 典型冲击（单根跳变 <50%，不是跳空）
    path = [100, 100.5, 101.2, 101.8, 102] + [102] * 19
    f = opt_move_shape.fit_shape(_bars(path))
    assert f["label"] == "attack"
    assert f["t80"] <= 0.25


def test_fit_shape_detects_gap():
    # 一根直接跳到位（单根跳变 ≥50% 全程）—— 跳空，不是连续冲击
    path = [100, 100.1, 102] + [102] * 21
    f = opt_move_shape.fit_shape(_bars(path))
    assert f["label"] == "gap"


def test_fit_shape_detects_drift():
    # 全程匀速爬升 —— 慢涨
    path = [100 + i * 0.2 for i in range(24)]
    f = opt_move_shape.fit_shape(_bars(path))
    assert f["label"] == "drift"
    assert f["t80"] >= 0.60


def test_fit_shape_detects_round_trip():
    # 冲高后又回到起点 —— 不是单向冲击
    path = [100 + i for i in range(8)] + [108 - i for i in range(1, 9)]
    f = opt_move_shape.fit_shape(_bars(path))
    assert f["label"] == "irregular"
    assert f["retrace"] >= 0.5


def test_fit_shape_needs_enough_bars():
    assert opt_move_shape.fit_shape(_bars([100, 101, 102])) is None


def test_shape_to_kind_maps_direction():
    assert opt_move_shape.shape_to_kind("attack", 2.0) == "fast_rise"
    assert opt_move_shape.shape_to_kind("attack", -2.0) == "fast_fall"
    assert opt_move_shape.shape_to_kind("drift", -2.0) == "slow_fall"
    assert opt_move_shape.shape_to_kind("irregular", 2.0) is None


# ---------------------------------------------------------------- opt_weight_diag

def test_spearman_monotonic():
    xs = list(range(20))
    ys = [v * 3 for v in xs]
    assert opt_weight_diag.spearman(xs, ys) == 1.0


def test_spearman_inverse():
    xs = list(range(20))
    ys = [-v for v in xs]
    assert opt_weight_diag.spearman(xs, ys) == -1.0


def test_spearman_with_ties_is_bounded():
    xs = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    ys = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    rho = opt_weight_diag.spearman(xs, ys)
    assert rho is not None and 0.9 < rho <= 1.0


def test_spearman_small_sample_returns_none():
    assert opt_weight_diag.spearman([1, 2, 3], [1, 2, 3]) is None


# ---------------------------------------------------------------- opt_asset_cover

def test_extract_subject_unique():
    one, cands = opt_asset_cover.extract_subject("比特币ETF净流入创历史新高")
    assert one == "BTC" and cands == ["BTC"]


def test_extract_subject_strips_prefix():
    one, _ = opt_asset_cover.extract_subject("[hash:abc123] 金价逆势下挫")
    assert one == "XAU"


def test_extract_subject_ambiguous_returns_none():
    one, cands = opt_asset_cover.extract_subject("比特币与以太坊同步上涨")
    assert one is None and set(cands) == {"BTC", "ETH"}


def test_extract_subject_company_trap():
    one, cands = opt_asset_cover.extract_subject("标普全球完成对某公司的收购")
    assert one is None


def test_classify_none_reason_calendar():
    assert opt_asset_cover.classify_none_reason("jin10_calendar", "美国CPI公布") == "calendar"


# ---------------------------------------------------------------- gate_audit 细分

def test_gate_audit_no_direction_carries_subcode():
    from review import gate_audit
    row = {"asset": "BTC", "action": "HOLD", "trade_gate_reason": "passed",
           "evidence_action": "HOLD", "evidence_confidence": 0.5, "status": "UNREAD"}
    p, code, detail = gate_audit.classify(
        row, passed_reason="passed", allowed_assets=[], conf_gate=20.0,
        reasoning="[MiniMax] [副品种 BTC] 事件不直接命中本系统核心监控品种（BTC/XAU/WTI/ETH/SOL）")
    assert (p, code) == (0, gate_audit.REJECT_NO_DIRECTION)
    assert "sub=asset_role_mislabel" in detail


def test_gate_audit_subcode_does_not_change_pass_verdict():
    from review import gate_audit
    row = {"asset": "BTC", "action": "BUY", "trade_gate_reason": "passed",
           "evidence_action": "BUY", "evidence_confidence": 60.0, "status": "UNREAD"}
    p, code, detail = gate_audit.classify(
        row, passed_reason="passed", allowed_assets=["BTC"], conf_gate=20.0)
    assert p == 1 and code == ""


# ---------------------------------------------------------------- 记分卡 S11 契约

def test_scorecard_s11_returns_counts_not_percent(tmp_conn=None):
    """Metric.query 必须返回 (命中数, 样本数)；返回百分比会被 evaluate_metric 再除一次。"""
    import sqlite3
    import capability_scorecard as cs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trade_decisions(asset TEXT, intended_action TEXT, "
                 "decision_ts REAL, actual_outcome_pct REAL, status TEXT)")
    # 同一小时同品种同方向 2 条（第 2 条必被去重丢掉），下一小时 1 条反向
    conn.executemany("INSERT INTO trade_decisions VALUES (?,?,?,?,?)", [
        ("BTC", "BUY", 1000, 1.0, "settled"),
        ("BTC", "BUY", 1100, 1.0, "settled"),
        ("BTC", "BUY", 5000, -1.0, "settled"),
    ])
    m = [x for x in cs.METRICS if x.id == "S11"][0]
    res = cs.evaluate_metric(conn, m)
    # 去重后 2 条独立信号，命中 1 条 → 50.0%（若误返回 50.0 会被算成 0.68%）
    assert res.n == 2
    assert 49.9 < res.value < 50.1


def test_scorecard_s12_counts_real_analysis_only():
    import sqlite3
    import capability_scorecard as cs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ai_decisions(id INTEGER, reasoning_path TEXT)")
    conn.executemany("INSERT INTO ai_decisions VALUES (?,?)", [
        (1, "[总框架] 目标资产 XAU；事件不直接命中本系统核心监控品种（BTC/XAU/WTI/ETH/SOL）"),
        (2, "[总框架] 目标资产 BTC；本次 **LLM 未返回可用分析**（空输出或解析前被截断）"),
        (3, "[总框架] 目标资产 NONE | 服务端兜底：因果图谱已注入"),
        (4, "[总框架] XAU 实际盘面：美元走弱 + 避险资金流入\n[总结] 看多"),
    ])
    m = [x for x in cs.METRICS if x.id == "S12"][0]
    res = cs.evaluate_metric(conn, m)
    assert res.n == 4
    assert 24.9 < res.value < 25.1  # 4 条里只有 1 条是真分析


# ---------------------------------------------------------------- 兜底文案说实话

def test_degraded_template_core_asset_admits_empty_output():
    """核心品种走了兜底，必须承认"LLM 空输出"，不能写"不在监控范围"。"""
    import engine.ai_worker as w

    text = w.degraded_reasoning_template(
        asset="BTC", market_category="CRYPTO", action="HOLD", score_s="+0.05",
        summary_line="观望")
    assert "LLM 未返回可用分析" in text
    assert "分析降级" in text
    # 旧的假理由必须消失（这正是 3692 条核心品种被误标的根因）
    assert "事件不直接命中本系统核心监控品种" not in text
    assert "不在核心监控清单 →" not in text
    # 八段结构不能破：下游按段名解析
    for seg in ("[总框架]", "[因素定位]", "[驱动力]", "[水位博弈]",
                "[跨资产资金]", "[反共识结论]", "[逻辑自检]", "[总结]"):
        assert seg in text


def test_degraded_template_off_scope_keeps_scope_statement():
    import engine.ai_worker as w

    text = w.degraded_reasoning_template(
        asset="NONE", market_category="MACRO", action="HOLD", score_s="未评估")
    assert "不在**核心监控清单" in text
    for seg in ("[总框架]", "[因素定位]", "[驱动力]", "[水位博弈]",
                "[跨资产资金]", "[反共识结论]", "[逻辑自检]", "[总结]"):
        assert seg in text


def test_asset_in_core_scope():
    import engine.ai_worker as w

    for a in ("btc", "XAU", "wti", "eth", "SOL"):
        assert w.asset_in_core_scope(a) is True
    for a in ("NONE", "CN_STOCK", "", "NVDA"):
        assert w.asset_in_core_scope(a) is False


# ---------------------------------------------------------------- 慢异动上下文补齐

def _ctx_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_style_snapshot(asset TEXT, ts INTEGER, regime TEXT, "
                 "layer1_alerts TEXT, layer2_alerts TEXT, layer3_alerts TEXT)")
    conn.execute("CREATE TABLE kline_bars(symbol TEXT, timeframe TEXT, ts INTEGER, volume REAL)")
    conn.execute("CREATE TABLE crowd_topic_daily(day TEXT, sentiment TEXT, "
                 "influence_tier TEXT, asset_class TEXT)")
    conn.execute("CREATE TABLE liquidation_heat(ts INTEGER, total_usd REAL, "
                 "long_liq_usd REAL, short_liq_usd REAL)")
    return conn


def test_enrich_context_fills_missing_fields():
    import fast_move_detector as fmd

    conn = _ctx_db()
    ts = 1_794_000_000
    conn.execute("INSERT INTO market_style_snapshot VALUES (?,?,?,?,?,?)",
                 ("BTC", ts - 600, "trending_down", "a", "b", "c"))
    conn.execute("INSERT INTO market_style_snapshot VALUES (?,?,?,?,?,?)",
                 ("BTC", ts + 600, "trending_up", "x", "y", "z"))  # 未来信息，必须不可见
    for i in range(20):
        conn.execute("INSERT INTO kline_bars VALUES (?,?,?,?)",
                     ("BTC", "1h", (ts - i * 3600) * 1000, 10.0 + i))
    conn.execute("INSERT INTO crowd_topic_daily VALUES (?,?,?,?)",
                 ("2026-11-01", "看多", "tier1", "CRYPTO"))

    ctx = fmd.enrich_context(conn, "BTC", ts, {"k_days": 1})
    assert ctx["regime"] == "trending_down"          # 取触发时刻之前那条
    assert ctx["vol_ratio"] is not None
    assert ctx["crowd_sentiment"] == "看多"
    assert "market_phase" in ctx and "reasons" in ctx
    assert "k_days" in ctx                            # 原始字段保留


def test_enrich_context_never_overwrites_observed_values():
    import fast_move_detector as fmd

    conn = _ctx_db()
    ts = 1_794_000_000
    conn.execute("INSERT INTO market_style_snapshot VALUES (?,?,?,?,?,?)",
                 ("BTC", ts - 600, "trending_down", "", "", ""))
    base = {"regime": "volatile_high", "vol_ratio": 3.0}
    ctx = fmd.enrich_context(conn, "BTC", ts, base)
    assert ctx["regime"] == "volatile_high"
    assert ctx["vol_ratio"] == 3.0


def test_degraded_context_survives_missing_tables():
    import fast_move_detector as fmd

    import sqlite3
    conn = sqlite3.connect(":memory:")  # 四张表全无
    ctx = fmd.enrich_context(conn, "BTC", 1_794_000_000, {"k_days": 3})
    assert ctx["k_days"] == 3
    assert "market_phase" in ctx


def test_suppress_overlapping_keeps_strongest():
    import backfill_core_slow_moves as bsm

    moves = [
        {"ts": 1000000, "move_kind": "slow_rise", "move_pct": 3.1},
        {"ts": 1010000, "move_kind": "slow_rise", "move_pct": 4.8},  # <2天，保留幅度更大
        {"ts": 1400000, "move_kind": "slow_rise", "move_pct": 3.0},  # 隔开，独立保留
        {"ts": 1410000, "move_kind": "slow_fall", "move_pct": -3.2}, # 反向，独立保留
    ]
    out = bsm.suppress_overlapping(moves)
    assert len(out) == 3
    rises = [m for m in out if m["move_kind"] == "slow_rise"]
    assert abs(rises[0]["move_pct"]) == 4.8


# ---------------------------------------------------------------- 归因证据分层

def test_evidence_headline_when_subject_matches():
    import evidence_source as es

    assert es.classify_evidence("比特币跌破 77000 美元", "", "BTC") == es.HEADLINE


def test_evidence_cross_asset_when_headline_points_elsewhere():
    import evidence_source as es

    # 标题主语是 BTC，异动发生在 SOL —— 传导链，不是"标题级命中"
    assert es.classify_evidence("比特币跌破 77000 美元", "", "SOL") == es.CROSS_ASSET


def test_evidence_body_only_when_only_body_mentions():
    import evidence_source as es

    assert es.classify_evidence("美联储降息预期升温", "……比特币随之反弹……",
                                "BTC") == es.BODY_ONLY


def test_evidence_macro_when_nothing_mentions():
    import evidence_source as es

    assert es.classify_evidence("美国 10 月 CPI 超预期", "市场重新定价加息路径",
                                "BTC") == es.MACRO


def test_evidence_tag_is_idempotent():
    import evidence_source as es

    once = es.tag_reason("match=asset; lag=1175s", es.HEADLINE)
    twice = es.tag_reason(once, es.HEADLINE)
    assert once == twice
    assert es.read_tag(once) == es.HEADLINE
    # 换档要替换而不是叠加
    swapped = es.tag_reason(once, es.MACRO)
    assert es.read_tag(swapped) == es.MACRO
    assert swapped.count("evidence=") == 1


def test_evidence_summarize_counts():
    import evidence_source as es

    s = es.summarize([es.HEADLINE, es.MACRO, es.MACRO])
    assert s[es.HEADLINE] == 1 and s[es.MACRO] == 2 and s["total"] == 3


# ---------------------------------------------------------------- 影子权重代理

def test_reaction_proxy_agrees_and_opposes():
    import opt_weight_shadow as ws

    idx = {"BTC": ([1000, 5000], ["fast_rise|2.0", "fast_fall|-2.0"])}
    mult, kind = ws.reaction_proxy(idx, "BTC", 5100, "SELL", 3600)
    assert mult == 1.3 and kind == "fast_fall"      # 顺跌势做空 → 加分
    mult, kind = ws.reaction_proxy(idx, "BTC", 5100, "BUY", 3600)
    assert mult == 0.7                              # 逆势做多 → 扣分
    mult, _ = ws.reaction_proxy(idx, "BTC", 5100, "HOLD", 3600)
    assert mult == 1.0


def test_reaction_proxy_respects_window():
    import opt_weight_shadow as ws

    idx = {"BTC": ([1000], ["fast_rise|2.0"])}
    assert ws.reaction_proxy(idx, "BTC", 1000 + 7200, "BUY", 3600) == (None, None)
    assert ws.reaction_proxy(idx, "ETH", 1000, "BUY", 3600) == (None, None)


def test_regime_proxy_maps_known_regimes_only():
    import opt_weight_shadow as ws

    idx = {"BTC": ([1000], ["trending_up"]), "ETH": ([1000], ["unknown_state"])}
    assert ws.regime_proxy(idx, "BTC", 1200) == (1.2, "trending_up")
    assert ws.regime_proxy(idx, "ETH", 1200) == (None, "unknown_state")


# ---------------------------------------------------------------- 权重档位开关

def test_compose_weight_legacy_matches_manual_formula():
    from signals import signal_weight_evaluator as swe

    base, imp, cons, src, react, reg, tm = 1.2, 1.6, 0.9, 1.1, 1.0, 0.85, 1.0
    qual = (cons * src * react * reg * tm) ** 0.2
    raw = (imp ** 0.85) * ((base * qual) ** 0.15)
    assert abs(swe.compose_weight(base, imp, cons, src, react, reg, tm, profile="legacy")
               ["weight"] - raw / (1.0 + raw)) < 1e-12


def test_compose_weight_impact_led_ignores_dead_dims():
    from signals import signal_weight_evaluator as swe

    a = swe.compose_weight(1.2, 1.6, 0.9, 1.1, 1.0, 1.0, 1.0, profile="impact_led")["weight"]
    b = swe.compose_weight(1.2, 1.6, 0.3, 0.4, 1.5, 0.7, 1.0, profile="impact_led")["weight"]
    assert a == b, "impact_led 档不应受 consensus/source/reaction/regime 影响"
    c = swe.compose_weight(1.5, 1.6, profile="impact_led")["weight"]
    assert c > a, "impact_led 仍须对 base 单调"


def test_compose_weight_unknown_profile_falls_back():
    from signals import signal_weight_evaluator as swe

    r = swe.compose_weight(1.2, 1.6, profile="no_such_profile")
    assert r["profile"] == "legacy"


def test_compose_weight_is_strictly_monotone_in_impact():
    from signals import signal_weight_evaluator as swe

    ws = [swe.compose_weight(1.0, 0.8 + i * 0.2)["weight"] for i in range(6)]
    assert all(a < b for a, b in zip(ws, ws[1:]))
    assert 0.0 <= ws[0] and ws[-1] <= 1.0


# ------------------------------------------------------- impact_score 缺省兜底

def test_resolve_impact_score_falls_back_to_gate():
    from engine.ai_worker import resolve_impact_score as r

    assert r(None, 0) == (None, "unavailable")
    assert r(5.0, 0) == (None, "unavailable")
    assert r(5.0, 73) == (7.3, "gate")
    assert r(None, 100) == (10.0, "gate")


def test_resolve_impact_score_keeps_model_judgement():
    from engine.ai_worker import resolve_impact_score as r

    assert r(8.5, 73) == (8.5, "model")
    assert r(1.0, 95) == (1.0, "model")
    assert r("bad", 0) == (None, "unavailable")


def test_resolve_impact_score_clamps_gate_range():
    from engine.ai_worker import resolve_impact_score as r

    assert r(5.0, 140)[0] <= 10.0
    assert r(5.0, -3) == (None, "unavailable")


def test_classify_none_reason_blog():
    assert opt_asset_cover.classify_none_reason("WEB:Blogs", "随便聊聊") == "blog"


def test_classify_none_reason_no_subject():
    assert opt_asset_cover.classify_none_reason("jin10", "某公司宣布更换CEO") == "no_subject"

# ── P0-4: 认领幂等（已经有决策的新闻不再重复分析）────────────────────────

def test_claim_sql_skips_news_with_existing_decision():
    """v336: 同一新闻被反复分析（812 组 / 2096 行复制品, 23.6%）。

    落库成功但 raw_news 没置 DONE → 看门狗回收 PROCESSING → 回 PENDING
    → 同一份 LLM 输出被再写一遍。认领 SQL 必须显式排除"已有决策"的新闻，
    否则每轮重试都在给统计分母灌水。
    """
    from engine import ai_worker as aw

    for sql in (aw._BATCH_CLAIM_SQL, aw._BATCH_CLAIM_FRESH_SQL):
        assert "NOT EXISTS" in sql
        assert "ai_decisions" in sql
        assert "d.news_id = raw_news.id" in sql


def test_claim_dedup_can_be_bypassed_for_replay():
    """回归重放历史新闻时需要能关掉幂等（否则重放一条都认领不到）。"""
    from engine import ai_worker as aw
    assert aw._ALLOW_REANALYZE is False
    assert aw._CLAIM_DEDUP != ""

# ── P0-6: 异动时间锚单位混用（新闻窗口恒为空）──────────────────────────

def test_to_sec_ts_normalizes_milliseconds():
    """anchor_ts 既存毫秒（8072 行）又存秒（79 行），而 news_events.ts 全是秒。

    多因子归因把毫秒锚直接喂给「往前 1800 秒」的新闻窗口 →
    `ne.ts >= 1.79e12-1800` 恒为假 → 新闻上下文永远 0 条。
    实测同一锚：秒口径 5 条，毫秒口径 0 条。
    """
    from api_server import _to_sec_ts

    assert _to_sec_ts(1794624153) == 1794624153      # 秒：原样
    assert _to_sec_ts(1794624153000) == 1794624153    # 毫秒：除以 1000
    assert _to_sec_ts(None) == 0
    assert _to_sec_ts("bad") == 0
    assert _to_sec_ts(0) == 0

# ── P0-7: external_indicators 时间戳混单位（新鲜度审计失明）──────────────

def test_external_indicator_ts_normalized_to_seconds():
    """表里 52746 行秒 + 27383 行毫秒。下游用 `now - ts` 算年龄，
    毫秒行的 age 是 -1.6e12 → 永远判"新鲜"，停更几个月也报 ok。
    """
    from external_data import _to_sec_ts

    assert _to_sec_ts(1794630581) == 1794630581
    assert _to_sec_ts(1794630581000) == 1794630581
    assert _to_sec_ts(1794630581000000) == 1794630581   # 微秒
    assert _to_sec_ts(None) == 0


def test_freshness_audit_reads_millisecond_rows_as_stale(tmp_path):
    """读端也要兜一层：老数据没归一时不能把毫秒行当"刚更新"。"""
    import sqlite3
    import freshness_audit as fa

    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE external_indicators("
                 "asset TEXT, indicator TEXT, value REAL, ts INTEGER, source TEXT)")
    import time
    now = int(time.time())
    # 60 天前（毫秒表示），阈值 96h → 必须报 late，而不是 ok
    conn.execute("INSERT INTO external_indicators VALUES (?,?,?,?,?)",
                 ("BTC", "cm_supply", 1.0, (now - 60 * 86400) * 1000, "coinmetrics"))
    conn.commit()
    rows = [r for r in fa.check_expected(conn, now)
            if r["indicator"] == "cm_supply"]
    assert rows and rows[0]["status"] == "late"

# ── P0-8: 反思的"反向证据"窗口恒为空（字符串格式/时区错配）──────────────

def test_counter_evidence_window_matches_local_iso_news():
    """raw_news.first_seen_ts 存 '2026-11-14T15:36:51+08:00'（T + 北京时间）。

    原实现用 UTC 的空格分隔串去比：'T'(0x54) > ' '(0x20) →
    `first_seen_ts <= '... 03:00:00'` 同日内恒假 → 窗口内**永远 0 条**，
    "被省略的反向证据"恒等于 0（审计形同虚设）。
    实测同一小时：旧口径 0 条 / 新口径 59 条。
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from review import reflection_core as rc

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE raw_news(id INTEGER PRIMARY KEY, title TEXT, content TEXT,"
                 " impact_score REAL, sentiment_score REAL, first_seen_ts TEXT)")
    decision_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    local = datetime.fromtimestamp(decision_ms // 1000 - 600,
                                   tz=timezone(timedelta(hours=8)))
    conn.execute("INSERT INTO raw_news VALUES (1,?,?,?,?,?)",
                 ("BTC 暴跌 触发止损", "比特币跌破关键支撑", 8.0, -0.7,
                  local.strftime("%Y-%m-%dT%H:%M:%S+08:00")))
    conn.commit()

    out = rc.audit_counter_evidence(conn, "", "", "", decision_ms, "BTC", "BUY")
    assert out["note"] != "no related news", out
