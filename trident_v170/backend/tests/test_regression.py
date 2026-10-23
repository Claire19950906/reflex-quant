"""回归测试：失败归因框架（23 类 + 可观测性 + 硬条件）、经验命中归一化、胜率分母口径。

覆盖本轮落地：
  · 失败归因 17→23 类扩展（_REASON_NAMES 1-17 兼容不变，18-23 新增）
  · reason_observability 标签（auto/llm/hidden）
  · _rule_based_failure 硬条件归因（decision_context 空 → code 1）
  · normalize_insight_hit_ids（AI 采纳经验 id 归一化）
  · _aggregate 胜率分母 = WIN/(WIN+LOSS)，HOLD 不计入分母
  · reflection 依赖的 history_review._analyze_trades_llm 存在（防修复回归）
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import review_agent as r
import history_review


def test_reason_framework_23_classes_continuous():
    assert len(r._REASON_NAMES) == 23
    assert sorted(r._REASON_NAMES) == list(range(1, 24))


def test_reason_names_legacy_compatible_and_extended():
    # 1-17 历史名称完全不变，避免历史 reason_name 数据分裂
    assert r._REASON_NAMES[1] == "数据缺失"
    assert r._REASON_NAMES[7] == "信号质量差"
    assert r._REASON_NAMES[17] == "模型推理降级"
    # 18-23 新增
    assert r._REASON_NAMES[18] == "数据错误"
    assert r._REASON_NAMES[19] == "联动数据缺失/错误"
    assert r._REASON_NAMES[20] == "成交滑点/成本"
    assert r._REASON_NAMES[21] == "系统通道故障"
    assert r._REASON_NAMES[22] == "新闻已定价/抢跑"
    assert r._REASON_NAMES[23] == "经验误用/过拟合"


def test_reason_observability_labels():
    assert r.reason_observability(1) == "auto"
    assert r.reason_observability(5) == "hidden"
    assert r.reason_observability(22) == "hidden"
    assert r.reason_observability(6) == "llm"
    assert r.reason_observability(999) == "llm"  # 未知 code 默认 llm


def test_rule_based_failure_data_missing():
    assert r._rule_based_failure({})["reason_code"] == 1
    assert r._rule_based_failure({"decision_context": "{}"})["reason_code"] == 1
    assert r._rule_based_failure({"decision_context": ""})["reason_code"] == 1
    assert r._rule_based_failure({"decision_context": '{"status":"ok"}'}) is None


def test_normalize_insight_hit_ids():
    assert r.normalize_insight_hit_ids([3, 1, 3, "2", 0, -1, "x"]) == [1, 2, 3]
    assert r.normalize_insight_hit_ids("[1,2,3]") == [1, 2, 3]
    assert r.normalize_insight_hit_ids("not-json") == []
    assert r.normalize_insight_hit_ids(None) == []
    assert r.normalize_insight_hit_ids(12) == []
    assert r.normalize_insight_hit_ids([]) == []


def test_aggregate_winrate_excludes_hold():
    trades = [
        {"is_correct": "WIN", "forward_pnl": 1.0, "asset": "XAU"},
        {"is_correct": "LOSS", "forward_pnl": -1.0, "asset": "WTI"},
        {"is_correct": "HOLD", "forward_pnl": 0.0, "asset": "BTC"},
    ]
    agg = r._aggregate(trades)
    assert agg["decided"] == 2
    assert agg["winrate"] == 0.5  # 1 WIN / (1 WIN + 1 LOSS)，HOLD 不计入分母


def test_history_review_has_analyze_trades_llm():
    # reflection 修复底座：单笔反思归因必须调用 history_review._analyze_trades_llm
    assert hasattr(history_review, "_analyze_trades_llm")


def test_rule_based_failure_stats_report_news_thematic_error():
    # 事后统计/评估性负面损失消息 + 避险 BUY + 实亏 → 确定性归因为题材误判（7），
    # 不允许再被 LLM 机械归为入场时机（16）。
    hit = r._rule_based_failure({
        "decision_context": '{"status":"ok"}',
        "action": "BUY",
        "asset": "XAU",
        "is_correct": "LOSS",
        "news_text": "据统计，今年俄罗斯空袭对乌克兰基础设施造成的破坏累计已达100亿美元",
    })
    assert hit is not None
    assert hit["reason_code"] == 7


def test_rule_based_failure_official_statement_not_stats():
    # 官员/机构「发言表态」是即时事件催化，不是媒体整理的事后统计，
    # 不应被硬归 7（交给 LLM 判断题材/方向）。
    assert r._rule_based_failure({
        "decision_context": '{"status":"ok"}', "action": "BUY", "asset": "XAU",
        "is_correct": "LOSS",
        "news_text": "乌克兰经济部长：由于俄罗斯空袭，基础设施损失预计将在今年达到100亿美元",
    }) is None
    # 无冒号的引语动词表态同样不命中
    assert r._rule_based_failure({
        "decision_context": '{"status":"ok"}', "action": "BUY", "asset": "XAU",
        "is_correct": "LOSS",
        "news_text": "乌克兰经济部长表示，基础设施损失预计将达到100亿美元",
    }) is None


def test_rule_based_failure_stats_report_not_false_positive():
    # 方向非避险看涨 / 非避险品种 → 不硬归因，交 LLM
    assert r._rule_based_failure({
        "decision_context": '{"status":"ok"}', "action": "SELL", "asset": "XAU",
        "is_correct": "LOSS", "news_text": "空袭造成基础设施破坏估计达100亿美元",
    }) is None
    assert r._rule_based_failure({
        "decision_context": '{"status":"ok"}', "action": "BUY", "asset": "BTC",
        "is_correct": "LOSS", "news_text": "空袭造成基础设施破坏估计达100亿美元",
    }) is None


def test_rule_based_failure_instant_catalyst_not_misattributed():
    # 即时催化（突发袭击/爆炸/停产）不是「事后统计」，不应被硬归 7
    assert r._rule_based_failure({
        "decision_context": '{"status":"ok"}', "action": "BUY", "asset": "WTI",
        "is_correct": "LOSS", "news_text": "无人机袭击导致炼油厂爆炸停产，供应中断",
    }) is None


def test_failure_framework_no_naive_timing():
    # code 16 必须显式带「仅在方向判对时」前提；code 6 显式覆盖「方向本身判错」
    fw = r._FAILURE_FRAMEWORK
    assert "仅在方向/题材判断正确" in fw
    assert "把中性/事后统计/评估性消息" in fw


def test_reasoning_gist_extracts_summary():
    import json as _json
    sample = _json.dumps({
        "驱动力": "地缘避险",
        "总结": "避险情绪支撑但技术面未突破，谨慎看多（BUY）",
    }, ensure_ascii=False)
    gist = history_review._reasoning_gist(sample)
    assert "谨慎看多" in gist
    assert not gist.startswith(" ")
    assert history_review._reasoning_gist(None) == ""
def test_trade_fact_derives_mfe_mae_from_extremes():
    # BUY：最高价=最大浮盈、最低价=最大浮亏
    f = r._trade_fact({"action": "BUY", "entry_price": 100, "exit_price": 105,
                       "max_price": 110, "min_price": 95})
    assert "MFE+10.00%/MAE+5.00%" in f
    # SELL：最低价=最大浮盈、最高价=最大浮亏
    f2 = r._trade_fact({"action": "SELL", "entry_price": 2000, "exit_price": 1950,
                        "max_price": 2050, "min_price": 1950})
    assert "MFE+2.50%/MAE+2.50%" in f2
    # 已有 mfe_pct/mae_pct 时不换算（真实持仓）
    f3 = r._trade_fact({"entry_price": 100, "exit_price": 98, "mfe_pct": 1.2, "mae_pct": -0.5,
                        "max_price": 999, "min_price": 1})
    assert "MFE+1.20%/MAE-0.50%" in f3


def test_build_attribution_rows_alignment():
    trades = [{"id": 1, "asset": "XAU", "action": "BUY", "verdict": "LOSS", "forward_pnl": -0.5}]
    results = [{"decision_id": 1, "reason_code": 16, "reason_text": "追高", "confidence": 0.7}]
    rows = r.build_attribution_rows(trades, results)
    assert rows[0]["trade_id"] == 1 and rows[0]["reason_code"] == 16
    # history_review 变体：decision_id 匹配、trade.id 落库、pnl_pct 兜底
    trades2 = [{"id": 99, "decision_id": 1, "asset": "WTI", "action": "SELL",
                "verdict": "LOSS", "pnl_pct": -1.0}]
    rows2 = r.build_attribution_rows(trades2, results, match_id_key="decision_id", stored_id_key="id")
    assert rows2[0]["trade_id"] == 99 and rows2[0]["reason_code"] == 16
    assert rows2[0]["pnl_pct"] == -1.0


def test_attribution_feedback_closes_loop():
    import sqlite3
    import db
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    try:
        rows = r.build_attribution_rows(
            [{"id": 1, "asset": "XAU", "action": "BUY", "verdict": "LOSS", "forward_pnl": -0.5}],
            [{"decision_id": 1, "reason_code": 16, "reason_text": "追高", "confidence": 0.7}],
        )
        r.save_attribution_details(conn, "news", 93, rows)
        aid = conn.execute("SELECT id FROM attribution_reviews").fetchone()[0]
        res = r.apply_attribution_feedback(conn, aid, "corrected", corrected_code=7, note="统计性消息")
        assert res["ok"] and res["pushed_rule"]
        ins = conn.execute("SELECT rule FROM review_insights WHERE origin='rule'").fetchone()
        assert ins and "应判" in ins[0] and "信号质量差" in ins[0]
        row = conn.execute(
            "SELECT human_status, corrected_code, rule_pushed FROM attribution_reviews"
        ).fetchone()
        assert row == ("corrected", 7, 1)
    finally:
        conn.close()


def test_quick_sim_extremes_tracked_and_settled():
    import sqlite3
    import time
    import db
    import quick_sim
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    now = int(time.time())
    try:
        conn.execute(
            "INSERT INTO quick_sim_trades (decision_id, asset, action, entry_price, entry_ts,"
            " horizon_minutes, notional_usdt, max_price, min_price)"
            " VALUES (1,'XAU','BUY',2000.0,?,60,1000,2000.0,2000.0)", (now,))
        conn.execute(
            "INSERT INTO quick_sim_trades (decision_id, asset, action, entry_price, entry_ts,"
            " horizon_minutes, notional_usdt, max_price, min_price)"
            " VALUES (2,'BTC','SELL',100.0,?,2,1000,100.0,100.0)", (now - 120,))
        conn.commit()
        px = {"XAU": 2050.0, "BTC": 95.0}
        quick_sim.update_open_extremes(conn, lambda a: px[a])
        row = conn.execute(
            "SELECT max_price, min_price FROM quick_sim_trades WHERE decision_id=1"
        ).fetchone()
        assert row == (2050.0, 2000.0)
        settled = quick_sim.settle_trades(conn, lambda a: px[a])
        assert settled[0]["verdict"] == "WIN"
        row = conn.execute(
            "SELECT exit_price, max_price, min_price FROM quick_sim_trades WHERE decision_id=2"
        ).fetchone()
        # SELL entry100 -> exit95 盈利；期间低价 95 已计入 min_price、高价仍 100
        assert row == (95.0, 100.0, 95.0)
    finally:
        conn.close()