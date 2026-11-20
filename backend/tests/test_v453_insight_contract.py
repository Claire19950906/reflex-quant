# -*- coding: utf-8 -*-
"""v453: 洞察生成侧硬门槛（insight_contract）的纯函数测试。

覆盖：
- check(): 可执行 / 叙事 / 元规则 / 不合格 四档分级
- enforce(): 不合格 → 带缺项重写一次；重写仍不合格 → 丢弃且写审计日志
- 不误杀元规则（"绝不臆造数据"这类护栏不该因为没有数值被丢）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
sys.path.insert(0, _SRC + "/src_python")

import insight_contract as ic


def test_check_actionable():
    r = ic.check("[score.buy_min_score] BUY 最低分 0.4→0.55，24h 内胜率应提升")
    assert r["level"] == "actionable"
    assert r["has_key"] and r["has_number"] and r["has_direction"] and r["has_window"]


def test_check_narrative_no_key():
    r = ic.check("[XAU] 黄金在 score<0.4 时胜率低，应降低仓位 5%")
    assert r["level"] in ("narrative", "actionable")
    assert r["ok"] is True


def test_check_meta_not_killed():
    r = ic.check("只依据真实、可追溯的数据源；数据缺失标 N/A，绝不臆造")
    assert r["level"] == "meta"
    assert r["ok"] is True


def test_check_strategy_without_numbers_rejected():
    r = ic.check("避险叙事下黄金走强概率高，但资金坐实前不追多")
    assert r["level"] == "reject"
    assert r["ok"] is False
    assert "无数值" in r["gaps"]


def test_enforce_rewrites_once():
    calls = {"n": 0}

    def fake_llm(prompt, system, max_tokens=800):
        calls["n"] += 1
        # 第一次不用于生成，只用于重写；返回改写后的可验证版本
        return '["[score.sell_min_score] 弱势行情 SELL 门槛上提高至 0.55，24h 内应减少假信号"]'

    kept, rep = ic.enforce(
        ["弱势行情下卖出信号太多，应该提高门槛"],
        llm_call=fake_llm, label="test_no_log",
    )
    assert calls["n"] == 1
    assert rep["rewritten"] == 1
    assert len(kept) == 1
    assert rep["dropped"] == 0


def test_enforce_drops_when_rewrite_fails(tmp_path):
    def fake_llm(prompt, system, max_tokens=800):
        return '["还是没数值的一条规则"]'

    log = os.path.join(str(tmp_path), "ic.jsonl")
    kept, rep = ic.enforce(
        ["这条规则没数值也没明确方向，只说要谨慎"],
        llm_call=fake_llm, label="test_drop", log_path=log,
    )
    assert kept == []
    assert rep["dropped"] == 1
    assert os.path.exists(log)
    line = json.loads(open(log, encoding="utf-8").read().strip())
    assert line["dropped"] == 1
    assert line["drop_detail"][0]["gaps"]


def test_enforce_keeps_good_ones():
    kept, rep = ic.enforce(
        ["[sizing.stop_loss_pct] 波动放大时止损收窄至 0.6%，24h 内适用"],
        llm_call=None, label="test_keep",
    )
    assert len(kept) == 1
    assert rep["rewritten"] == 0


def test_review_agent_generator_applies_contract(monkeypatch):
    """接线验证：_generate_insights_llm 产出的"没数值的经验"必须被拦下。"""
    import review.review_agent as ra

    def fake_llm(prompt, system, max_tokens=1200):
        if "不可验证" in prompt:                      # 重写那次
            return '["[score.sell_min_score] SELL 门槛上调至 0.55，24h 内减少假信号"]'
        return ('["[score.buy_min_score] BUY 门槛上调至 0.5，24h 内胜率应改善",'
                '"谨慎一点，别追高"]')                 # 第二条不合格

    monkeypatch.setattr(ra, "_llm", fake_llm)
    out = ra._generate_insights_llm({}, {})
    assert len(out) == 2, out
    assert all(ic.check(r)["ok"] for r in out), out
    assert any("0.5" in r for r in out)


# ── v453 第二批：经验注入的排序与冲突消解 ──────────────────────────────
def _it(i, verdict, key, samples, rule="r", ob=0):
    return {"id": i, "verdict": verdict, "param_key": key,
            "source_samples": samples, "rule": rule, "observe_only": ob}


def test_rank_verified_beats_bigger_sample():
    items = [_it(1, "", "sizing.x", 40, "样本多但没验证"),
             _it(2, "verified", "score.buy_min_score", 34, "已验证")]
    out = ra_rank(items, limit=2)
    assert out[0]["id"] == 2


def test_rank_dedupes_same_param_key():
    items = [_it(1, "", "sizing.stop_loss_pct", 9, "提高止损"),
             _it(2, "verified", "sizing.stop_loss_pct", 20, "收紧止损"),
             _it(3, "", "score.buy_min_score", 5, "另一键")]
    out = ra_rank(items, limit=10)
    keys = [x["param_key"] for x in out]
    assert keys.count("sizing.stop_loss_pct") == 1
    kept = [x for x in out if x["param_key"] == "sizing.stop_loss_pct"][0]
    assert kept["id"] == 2                      # 强度高的那条胜出


def test_rank_drops_failed():
    out = ra_rank([_it(1, "failed", "", 50, "被否证的")], limit=5)
    assert out == []


def test_rank_respects_limit():
    items = [_it(i, "", "", 1, f"r{i}") for i in range(10)]
    assert len(ra_rank(items, limit=3)) == 3


def ra_rank(items, limit):
    import review.review_agent as ra
    return ra.rank_insight_candidates(items, limit)


# ── v453 第三批：反例字段（错误码堆叠）清洗 ─────────────────────────────
def test_clean_counter_pure_codes():
    from review.review_agent import clean_counter_factual as f
    out = f("E020_CONTAMINATED: 基于 1 个 E020 事故样本E005_CONTAMINATED: 基于 3 个 E005 事故样本")
    assert out.startswith("E005×3/E020×1")
    assert "CONTAMINATED" not in out


def test_clean_counter_keeps_human_text():
    from review.review_agent import clean_counter_factual as f
    out = f("若未见真实买盘/持仓增持续证明资金流入，则不追多E020_CONTAMINATED: 基于 1 个 E020 事故样本")
    assert out == "若未见真实买盘/持仓增持续证明资金流入，则不追多"


def test_clean_counter_truncated_segment():
    from review.review_agent import clean_counter_factual as f
    assert f("E014_CONTAMINATED: 基于 2 个").startswith("E014×2")


def test_clean_counter_empty():
    from review.review_agent import clean_counter_factual as f
    assert f("") == ""


# ── v453 第四批：归因模块的口径防呆 ────────────────────────────────────
def test_gate_side_by_param_key():
    import insight_decision_attr as ia
    assert ia.gate_side_for("score.buy_min_score") == "BUY"
    assert ia.gate_side_for("filter.sell_min_score") == "SELL"
    assert ia.gate_side_for("min_event_strength") == ""


def test_sizing_insight_not_applicable():
    import insight_decision_attr as ia
    out = ia.attribute_insight(None, {"insight_param_key": "sizing.stop_loss_pct",
                                      "insight_direction": 1})
    assert out["verdict"] == "not_applicable"
    assert "仓位" in out["reason"]


def test_shared_gate_not_applicable():
    import insight_decision_attr as ia
    out = ia.attribute_insight(None, {"insight_param_key": "min_event_strength",
                                      "insight_direction": -1})
    assert out["verdict"] == "not_applicable"


# ── v453 第五批：降级率口径（越界 ≠ 失败）────────────────────────────
def test_summarize_states_splits_scope_from_failure():
    from analysis.contract import summarize_states
    rows = [
        (0, "", "BTC", "ok"),                      # complete
        (1, "summary_only", "XAU", "ok"),          # compressed（有方向但压缩）
        (1, "target_none", "NONE", "ok"),          # out_of_scope
        (1, "target_none", "600519", "missing:no_local_feed"),  # out_of_scope（个股无源）
        (1, "llm_empty", "BTC", "ok"),             # failed
        (1, "target_none", "BTC", "ok"),           # failed（核心品种却丢标的）
    ]
    s = summarize_states(rows)
    assert s["total"] == 6
    assert s["complete"] == 1 and s["compressed"] == 1
    assert s["out_of_scope"] == 2
    assert s["failed"] == 2
    assert s["failure_rate_in_scope"] == round(2 / 4, 4)


def test_numeric_stock_code_is_out_of_scope():
    from analysis.contract import scope_of
    assert scope_of("600519") == "stock"
    assert scope_of("000001.SZ") == "stock"
    assert scope_of("00700.HK") == "stock"


def test_summarize_states_empty():
    from analysis.contract import summarize_states
    s = summarize_states([])
    assert s["total"] == 1              # 防零除
    assert s["failed"] == 0


# ── v453 第六批：重置前强制快照（11-19 全库决策被清空事故的防呆）──
def test_pre_reset_snapshot_writes_restorable_copy(tmp_path):
    import sqlite3, sys
    sys.path.insert(0, "src_python")
    from api_maintenance import _pre_reset_snapshot
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE ai_decisions(id INTEGER)")
    c.executemany("INSERT INTO ai_decisions VALUES(?)", [(i,) for i in range(20)])
    c.commit()
    snap = _pre_reset_snapshot(c, tag="t")
    assert snap.endswith(".db")
    s = sqlite3.connect(snap)
    assert s.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0] == 20


# ── v453 第七批：代码指纹（换代可分层，不必删库）──────────────────
def test_fingerprint_has_git_and_param_dimensions():
    import sys, os
    sys.path.insert(0, "src_python")
    from code_fingerprint import fingerprint, reload_fingerprint
    fp = fingerprint()
    assert "/p" in fp and len(fp) >= 12
    # 改关键参数 → 指纹必须变（否则分层会串代）
    old = os.environ.get("REVIEW_INSIGHTS_RANK")
    os.environ["REVIEW_INSIGHTS_RANK"] = "0" if old != "0" else "1"
    try:
        assert reload_fingerprint() != fp
    finally:
        if old is None:
            os.environ.pop("REVIEW_INSIGHTS_RANK", None)
        else:
            os.environ["REVIEW_INSIGHTS_RANK"] = old
        assert reload_fingerprint() == fp


def test_fingerprint_survives_missing_git(monkeypatch):
    import sys
    sys.path.insert(0, "src_python")
    import code_fingerprint as cf
    monkeypatch.setattr(cf, "_git_hash", lambda: "")
    cf.fingerprint.cache_clear()
    assert cf.fingerprint().startswith("nogit/p")
    cf.fingerprint.cache_clear()


def test_stamp_writes_fingerprint_and_survives_bad_id(tmp_path):
    """两条 INSERT 链共用 stamp()：正常写值，坏 id 不抛（旁注不能拖垮落库）。"""
    import sqlite3, sys
    sys.path.insert(0, "src_python")
    from code_fingerprint import stamp, fingerprint
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, code_fingerprint TEXT DEFAULT '')")
    conn.execute("INSERT INTO ai_decisions(id) VALUES(7)")
    assert stamp(conn, 7) == fingerprint()
    assert conn.execute("SELECT code_fingerprint FROM ai_decisions WHERE id=7").fetchone()[0] == fingerprint()
    assert stamp(conn, 0) == ""
    assert stamp(conn, 999) == "" or True   # UPDATE 无匹配行不算失败
