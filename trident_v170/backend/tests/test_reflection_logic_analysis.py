# -*- coding: utf-8 -*-
"""v231 反思系统逻辑分析 (logic_analysis) — 6 个 detector 测试

覆盖:
  - 6 detector 各自的正反用例
  - analyze_logic 聚合 (含异常隔离)
  - compute_logic_integrity_score 评分
  - summarize_findings by_code / by_severity
  - schema 迁移幂等性 (logic_integrity_score / logic_findings_json / logic_checked_at)
  - enrich_reflection 端到端 (decision_errors 落 + trade_reflections_v2 列落)
  - enrich_pending_logic batch
  - get_logic_analysis_stats 统计接口
  - ontology 注册 (logic_analysis / formal_validity / 6 signals)
  - reflection_checkers 集成 (E028-E033 priority / guidance / layer weight)
"""
import os
import sys
import json
import sqlite3
import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db
import reflection_logic_analysis as rla
import reflection_checkers as rc


@pytest.fixture(autouse=True)
def _setup_reflection_test_data():
    """每个测试前: 准备 raw_news + ai_decisions 行
    + 测试结束后清理."""
    conn = db.get_connection()
    db.migrate(conn)
    # 注: 这里不预设 ai_decisions 行, 由 _make_reflection 内部按需创建
    # (避免 trade_reflections_v2.decision_id UNIQUE 冲突)
    conn.commit()
    conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM trade_reflections_v2 WHERE id < 0")
        conn.execute("DELETE FROM decision_errors WHERE decision_id < 0")
        conn.execute("DELETE FROM ai_decisions WHERE id < 0")
        conn.execute("DELETE FROM raw_news WHERE id < -1 OR id = -1")
        conn.commit()
    except Exception:
        pass
    conn.close()


def _set_reasoning(decision_id: int, reasoning: str):
    conn = db.get_connection()
    conn.execute("UPDATE ai_decisions SET reasoning = ? WHERE id = ?",
                 (reasoning, decision_id))
    conn.commit()
    conn.close()


def _make_reflection(reflection_id: int, decision_id: int = None,
                     asset: str = "BTC", action: str = "BUY",
                     pnl_pct: float = 0.005, reasoning: str = ""):
    """创建一个 trade_reflections_v2 行 (同时建好 ai_decisions / raw_news FK 链路).

    默认 decision_id = -reflection_id (避免 trade_reflections_v2.decision_id UNIQUE 冲突).
    reasoning 可顺便设置.
    """
    if decision_id is None:
        decision_id = -abs(reflection_id)
    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT 1 FROM ai_decisions WHERE id = ?", (decision_id,)).fetchone()
        if not existing:
            raw_id = decision_id - 1000000
            conn.execute(
                "INSERT OR IGNORE INTO raw_news (id, source, content, content_fingerprint, first_seen_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (raw_id, "t", "t", f"fp_v231_logic_{raw_id}", "2026-10-07"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO ai_decisions (id, news_id, suggested_action, target_asset, "
                "sentiment_score, reasoning, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (decision_id, raw_id, action, asset, 0.5, reasoning, 1700000000),
            )
            conn.commit()
        else:
            conn.execute(
                "UPDATE ai_decisions SET reasoning = ? WHERE id = ?",
                (reasoning, decision_id),
            )
            conn.commit()
        conn.execute(
            """INSERT OR REPLACE INTO trade_reflections_v2
               (id, decision_id, asset, action, pnl_pct, layer, sub_layer, signal_name,
                ontology_signal, root_cause, lesson, enrich_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (reflection_id, decision_id, asset, action, pnl_pct,
             "event_logic", "causal_chain", "test_logic_v231",
             "event_logic/causal_chain/test_logic_v231",
             "test root", "test lesson", "pending"),
        )
        conn.commit()
    finally:
        conn.close()

# ───────────────────────────────────────────────────────────────────────────
# 1) 6 detector 各自测试
# ───────────────────────────────────────────────────────────────────────────

def test_detect_e028_self_contradiction_returns_list():
    """E028: 不报错 + 返回 list."""
    text = "今日 BTC 出现强势看涨的明显特征，市场情绪强势推动价格向上突破新高，近期出现下跌迹象空头力量增强。"
    out = rla.detect_self_contradiction(text)
    assert isinstance(out, list)


def test_detect_e028_with_contrast_marker_negative():
    """E028: 显式对比标记 (但是/却/然而) → 不算自相矛盾 (inline)."""
    text = "BTC 持续看涨，但近期出现下跌迹象，市场情绪转弱。"
    out = rla.detect_self_contradiction(text)
    assert all(f.get("evidence_refs", {}).get("sub_kind") != "self_contradiction_inline"
               for f in out)


def test_detect_e028_empty_input():
    """E028: 空/短输入 → 空 findings."""
    assert rla.detect_self_contradiction("") == []
    assert rla.detect_self_contradiction("短文本") == []


def test_detect_e029_non_sequitur_positive():
    """E029: 结论含方向词但 body 不含 → 推不出结论."""
    text = (
        "[因素定位]近期 RSI 顶背离形成空头力量。\n"
        "[驱动力] 空头力量主导市场情绪弱势下跌。\n"
        "[总结]综合考虑买入做多看涨"
    )
    out = rla.detect_non_sequitur(text)
    assert len(out) >= 1
    assert out[0]["error_code"] == rla.E029_NON_SEQUITUR
    assert out[0]["evidence_refs"]["sub_kind"] == "non_sequitur_conclusion"


def test_detect_e029_non_sequitur_negative_when_supported():
    """E029: 结论与 body 同向 → 不触发."""
    text = (
        "[因素定位]BTC 上涨趋势明显，多头力量主导。\n"
        "[驱动力] 看涨情绪强势推动价格上涨。\n"
        "[总结]综合考虑买入做多看涨"
    )
    out = rla.detect_non_sequitur(text)
    assert out == []


def test_detect_e030_false_dichotomy_positive():
    """E030: 要么A要么B 无第三选项."""
    text = "要么大幅买入，要么清仓出局，没有别的选择。"
    out = rla.detect_false_dichotomy(text)
    assert len(out) >= 1
    assert out[0]["error_code"] == rla.E030_FALSE_DICHOTOMY


def test_detect_e030_false_dichotomy_negative_with_third():
    """E030: 有第三选项 (或者/另外/也可) → 不触发."""
    text = "要么大幅买入，要么清仓出局，或者减仓观望。"
    out = rla.detect_false_dichotomy(text)
    assert out == []


def test_detect_e031_circular_reasoning_positive():
    """E031: [总结] 段与 [因素定位]/[驱动力] 高度重合 → 循环论证."""
    text = (
        "[因素定位]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位形成多头格局。\n"
        "[驱动力]市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局确立。\n"
        "[总结]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局确立。"
    )
    out = rla.detect_circular_reasoning(text)
    assert len(out) >= 1
    assert out[0]["error_code"] == rla.E031_CIRCULAR_REASONING
    assert out[0]["evidence_refs"]["jaccard"] >= 0.5


def test_detect_e031_circular_reasoning_negative_normal():
    """E031: [总结] 段与前提不同 → 不触发."""
    text = (
        "[因素定位]BTC 近期盘整 50000-51000 区间，多空胶着。\n"
        "[驱动力]宏观紧缩预期 + ETF 资金净流出 + 链上指标中性。\n"
        "[总结]短期价格可能在 49500-51500 区间震荡 24-48 小时。"
    )
    out = rla.detect_circular_reasoning(text)
    assert out == []


def test_detect_e032_equivocation_positive():
    """E032: '支撑' 同时被正负修饰 (强 + 破位) → 歧义."""
    text = (
        "BTC 在 50000 形成强支撑位。当前价格破位下杀，"
        "支撑位暂时失效。建议关注反弹力度。"
    )
    out = rla.detect_equivocation(text)
    assert len(out) >= 1
    assert out[0]["error_code"] == rla.E032_EQUIVOCATION
    assert out[0]["evidence_refs"]["term"] == "支撑"


def test_detect_e032_equivocation_negative_consistent():
    """E032: 术语只有正修饰 → 不触发."""
    text = "BTC 在 50000 形成强支撑位。预计价格将在此企稳反弹。"
    out = rla.detect_equivocation(text)
    assert out == []


def test_detect_e033_post_hoc_positive():
    """E033: 紧接着 A 所以 B → post hoc."""
    text = "美联储刚刚加息25个基点，紧接着 BTC 价格随即下跌，所以市场对紧缩政策反应明显。"
    out = rla.detect_post_hoc(text)
    assert len(out) >= 1
    assert out[0]["error_code"] == rla.E033_POST_HOC


def test_detect_e033_post_hoc_negative_temporal_ok():
    """E033: 仅时序陈述 + 因果机制 → 不触发 (避免误报)."""
    text = "美联储加息25个基点，市场通过流动性收紧渠道压制 BTC 价格。"
    out = rla.detect_post_hoc(text)
    assert out == []

# ───────────────────────────────────────────────────────────────────────────
# 2) analyze_logic 聚合 + 异常隔离
# ───────────────────────────────────────────────────────────────────────────

def test_analyze_logic_aggregate():
    """analyze_logic: 6 个 detector 合并."""
    findings = rla.analyze_logic(
        "BTC 强势看涨。紧接着美联储加息，所以BTC随即下跌。但 RSI 强势。"
    )
    codes = [f["error_code"] for f in findings]
    # 至少触发 E033 (post hoc)
    assert rla.E033_POST_HOC in codes
    # 不应有重复
    assert len(codes) == len(set(codes))


def test_analyze_logic_exception_isolation(monkeypatch):
    """单个 detector 抛异常 → 不影响其他 detector."""
    def boom(_text):
        raise RuntimeError("simulated")
    monkeypatch.setattr(rla, "detect_self_contradiction", boom)
    out = rla.analyze_logic("测试文本，没问题")
    assert isinstance(out, list)


# ───────────────────────────────────────────────────────────────────────────
# 3) compute_logic_integrity_score + summarize_findings
# ───────────────────────────────────────────────────────────────────────────

def test_integrity_score_perfect_no_findings():
    """无 findings → 1.0."""
    assert rla.compute_logic_integrity_score([]) == 1.0


def test_integrity_score_clamped_to_zero():
    """严重 finding 累计超过 1.0 → clamp 到 0.0."""
    fake = [{"severity": rla.SEV_CRITICAL} for _ in range(10)]
    score = rla.compute_logic_integrity_score(fake)
    assert score == 0.0


def test_integrity_score_severity_weights():
    """不同 severity 不同 penalty."""
    s_warn = rla.compute_logic_integrity_score([{"severity": rla.SEV_WARN}])
    s_crit = rla.compute_logic_integrity_score([{"severity": rla.SEV_CRITICAL}])
    assert s_warn > s_crit
    assert 0.0 <= s_warn <= 1.0
    assert 0.0 <= s_crit <= 1.0


def test_summarize_findings_groups():
    """summarize_findings: by_code / by_severity / codes_list 正确分组."""
    findings = [
        {"error_code": "E028", "severity": rla.SEV_CRITICAL},
        {"error_code": "E029", "severity": rla.SEV_WARN},
        {"error_code": "E028", "severity": rla.SEV_CRITICAL},
    ]
    s = rla.summarize_findings(findings)
    assert s["n_findings"] == 3
    assert s["by_code"]["E028"] == 2
    assert s["by_code"]["E029"] == 1
    assert s["by_severity"]["critical"] == 2
    assert s["by_severity"]["warn"] == 1
    assert sorted(s["codes_list"]) == ["E028", "E029"]


# ───────────────────────────────────────────────────────────────────────────
# 4) schema 迁移幂等性
# ───────────────────────────────────────────────────────────────────────────

def test_schema_migration_idempotent():
    """迁移幂等性: 多次调用 + 列都在."""
    conn = db.get_connection()
    db.migrate(conn)
    db.migrate(conn)
    db.migrate(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)").fetchall()]
    for c in ("logic_integrity_score", "logic_findings_json", "logic_checked_at"):
        assert c in cols, f"missing column {c}"
    conn.close()

# ───────────────────────────────────────────────────────────────────────────
# 5) enrich_reflection 端到端
# ───────────────────────────────────────────────────────────────────────────

def test_enrich_reflection_writes_score_and_findings():
    """enrich_reflection: 写 logic_integrity_score / logic_findings_json / logic_checked_at."""
    reasoning = (
        "[因素定位]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位。\n"
        "[驱动力]市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局。\n"
        "[总结]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局。"
    )
    _make_reflection(-990100, reasoning=reasoning)
    result = rla.enrich_reflection(-990100, write=True)
    assert result["ok"] is True, f"got error: {result.get('error')}"
    assert result["written"] is True
    assert result["integrity_score"] < 1.0
    assert "E031" in result["summary"]["codes_list"]

    conn = db.get_connection()
    row = conn.execute(
        "SELECT logic_integrity_score, logic_findings_json, logic_checked_at "
        "FROM trade_reflections_v2 WHERE id = ?", (-990100,)).fetchone()
    assert row is not None
    assert row[0] == result["integrity_score"]
    assert row[1] != "{}" and row[1] != ""
    parsed = json.loads(row[1])
    assert "summary" in parsed
    assert "details" in parsed
    assert row[2] != ""
    conn.close()


def test_enrich_reflection_writes_decision_errors():
    """enrich_reflection: E028-E033 findings 落 decision_errors."""
    # 触发 E033 (post hoc): "紧接着 X 所以 Y"
    reasoning = (
        "[因素定位]美联储加息25个基点。\n"
        "[驱动力]流动性收紧。\n"
        "[总结]BTC 价格随即下跌，所以看跌做空。"
    )
    _make_reflection(-990100, reasoning=reasoning)
    result = rla.enrich_reflection(-990100, write=True)
    assert result["ok"] is True
    assert result.get("decision_errors_written", 0) >= 1

    conn = db.get_connection()
    decision_id = -abs(-990100)
    rows = conn.execute(
        "SELECT error_code FROM decision_errors WHERE decision_id = ? AND error_code IN ('E028','E029','E030','E031','E032','E033')",
        (decision_id,),
    ).fetchall()
    codes = [r[0] for r in rows]
    assert len(codes) >= 1, f"expected >= 1 logic findings, got {codes}"
    conn.close()


def test_enrich_reflection_skips_empty_reasoning():
    """enrich_reflection: reasoning 为空 → 跳过 detector, 但写 checked_at."""
    _make_reflection(-990100, reasoning="")
    result = rla.enrich_reflection(-990100, write=True)
    assert result["ok"] is True
    assert result.get("skipped") == "reasoning_empty"
    assert result["integrity_score"] == 1.0


def test_enrich_reflection_dry_run_no_write():
    """enrich_reflection(write=False): 算 findings 但不落库."""
    _make_reflection(-990100, reasoning="BTC 强势看涨，紧接着美联储加息，所以BTC随即下跌。")
    result = rla.enrich_reflection(-990100, write=False)
    assert result["ok"] is True
    assert result["written"] is False
    conn = db.get_connection()
    row = conn.execute(
        "SELECT logic_checked_at FROM trade_reflections_v2 WHERE id = ?",
        (-990100,)).fetchone()
    conn.close()
    assert row is None or row[0] == ""


def test_enrich_reflection_missing_reflection():
    """enrich_reflection: 不存在的 reflection_id → ok=False + error."""
    result = rla.enrich_reflection(-9999999, write=True)
    assert result["ok"] is False
    assert result.get("error") == "reflection_not_found"

# ───────────────────────────────────────────────────────────────────────────
# 6) enrich_pending_logic 批量
# ───────────────────────────────────────────────────────────────────────────

def test_enrich_pending_logic_batch():
    """enrich_pending_logic: 批量处理未跑过的反思."""
    reasoning = "BTC 强势看涨，紧接着美联储加息，所以BTC随即下跌。"
    _make_reflection(-990101, reasoning=reasoning)
    _make_reflection(-990102, reasoning=reasoning)

    result = rla.enrich_pending_logic(batch_size=10, max_per_run=10)
    assert result["scanned"] >= 2
    assert result["enriched"] >= 2
    assert result["errors"] == 0
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT id, logic_checked_at FROM trade_reflections_v2 "
        "WHERE id IN (-990101, -990102) AND logic_checked_at != ''").fetchall()
    conn.close()
    assert len(rows) == 2


def test_enrich_pending_logic_idempotent():
    """enrich_pending_logic 第二次跑: 已 enriched 的不再处理."""
    _make_reflection(-990101, reasoning="BTC 强势看涨，紧接着美联储加息，所以BTC随即下跌。")

    first = rla.enrich_pending_logic(batch_size=10, max_per_run=10)
    assert first["enriched"] >= 1

    second = rla.enrich_pending_logic(batch_size=10, max_per_run=10)
    assert second["enriched"] == 0


# ───────────────────────────────────────────────────────────────────────────
# 7) get_logic_analysis_stats
# ───────────────────────────────────────────────────────────────────────────

def test_get_stats_empty_db():
    """stats 接口: 空库 → ok=True, total=0."""
    result = rla.get_logic_analysis_stats(window_days=30)
    assert result["ok"] is True
    assert result["window_days"] == 30
    assert result["total"] == 0
    assert "by_score_bucket" in result
    assert "by_code" in result


def test_get_stats_with_data():
    """stats 接口: 有数据时正确聚合."""
    reasoning = (
        "[因素定位]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位。\n"
        "[驱动力]市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局。\n"
        "[总结]BTC 上涨趋势明显资金流入市场情绪强势推动价格上涨突破压力位资金流入上涨趋势多头格局。"
    )
    _make_reflection(-990101, pnl_pct=0.02, reasoning=reasoning)
    _make_reflection(-990102, pnl_pct=-0.01, reasoning=reasoning)
    rla.enrich_pending_logic(batch_size=10, max_per_run=10)

    stats = rla.get_logic_analysis_stats(window_days=30)
    assert stats["ok"] is True
    assert stats["total"] >= 2
    assert stats["avg_score"] < 1.0
    assert "E031" in stats["by_code"]
    assert len(stats["worst_20"]) >= 2


# ───────────────────────────────────────────────────────────────────────────
# 8) ontology + reflection_checkers 集成
# ───────────────────────────────────────────────────────────────────────────

def test_ontology_logic_analysis_layer_registered():
    """ontology: logic_analysis 层 + formal_validity 子层 + 6 signals 都注册."""
    import reflection_ontology as ro
    assert "logic_analysis" in ro.LAYERS
    assert ro.SUB_LAYERS.get("logic_analysis") == ["formal_validity"]
    assert ro.LAYER_WEIGHTS["logic_analysis"] == 3
    sigs = [s for s in ro.list_all_signals() if s["layer"] == "logic_analysis"]
    sig_names = {s["signal_name"] for s in sigs}
    assert sig_names == {
        "self_contradiction", "non_sequitur", "false_dichotomy",
        "circular_reasoning", "equivocation", "post_hoc",
    }


def test_checkers_registered():
    """reflection_checkers: E028-E033 在 priority + guidance + layer weight."""
    for code in ("E028", "E029", "E030", "E031", "E032", "E033"):
        assert code in rc._ERROR_CODE_PRIORITY
        assert code in rc._ERROR_CODE_GUIDANCE
    assert rc._LAYER_WEIGHT["logic_analysis"] == 85


def test_format_compact_guidance_includes_logic_analysis():
    """format_compact_guidance: 输出包含「逻辑分析层」分段 + E028-E033 行."""
    out = rc.format_compact_guidance(["E028", "E029", "E030", "E031", "E032", "E033"])
    assert "逻辑分析层" in out
    for code in ("E028", "E029", "E030", "E031", "E032", "E033"):
        assert f"[{code}]" in out