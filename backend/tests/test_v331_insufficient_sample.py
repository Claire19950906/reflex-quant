"""v331: "证据不足"不能被打成"验证失败"。

背景: `real_signal_pct = composite × min(交易样本/60, 1)` 是**乘性封顶**,
n<=6 时理论上限只有 0.1 —— 恰好等于 partial 门槛, 于是"样本少"必然落到 failed。
2026-11-13 实测: 181 条 failed 全部 n<12、最高分 0.07, 一条都没真被证伪。
若按"清退 failed"一刀切, 会误杀这 181 条 (其中 175 条当时正被喂进 LLM prompt)。

修法三层, 各有单测:
  1. 判定口径  insight_verifier.decide_verdict → 证据不足单列 insufficient_sample
  2. 存量数据  relabel_failed_insights.classify → 假失败重标, 真失败才停用
  3. 提示词侧  ai_prompts 过滤 failed / insufficient_sample, 假教训不进决策
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import insight_verifier as iv
import relabel_failed_insights as rl


# ── 1. 判定口径 ────────────────────────────────────────────────────────────
def test_low_sample_high_kline_is_insufficient_not_failed():
    """n=6 时 real_signal_pct 上限 0.1 → 不能判 failed。"""
    assert iv.decide_verdict(0.07, 6, 0.8) == "insufficient_sample"
    assert iv.decide_verdict(0.02, 2, 0.9) == "insufficient_sample"


def test_kline_itself_contradicts_still_fails():
    """K 线侧已经否证 (score<=0.3) → 样本再少也算有证据的失败。"""
    assert iv.decide_verdict(0.02, 3, 0.1) == "failed"


def test_enough_sample_low_score_still_fails():
    """样本够 (n>=12) 且分数低 → 真证伪, 该判 failed。"""
    assert iv.decide_verdict(0.03, 60, 0.2) == "failed"


def test_thresholds_unchanged():
    assert iv.decide_verdict(0.75, 60, 0.8) == "verified"
    assert iv.decide_verdict(0.3, 60, 0.5) == "partial"


def test_no_kline_data_is_unknown_not_failed():
    assert iv.decide_verdict(0.0, 0, 0.0, kline_all_empty=True) == "unknown_no_data"


def test_min_trades_matches_relabel_script():
    """两处阈值必须一致, 否则"判定"与"存量重标"会各说各话。"""
    assert iv.MIN_TRADES_FOR_VERDICT == rl.MIN_TRADES_FOR_VERDICT


# ── 2. 存量处置分流 ─────────────────────────────────────────────────────────
def _row(n, verdict="failed", active=1):
    return {"verification_verdict": verdict, "active": active, "verification_sample_n": n}


def test_thin_sample_is_relabelled_not_retired():
    assert rl.classify(_row(0)) == rl.RELABEL
    assert rl.classify(_row(6)) == rl.RELABEL
    assert rl.classify(_row(11)) == rl.RELABEL


def test_full_sample_is_retired():
    assert rl.classify(_row(12)) == rl.RETIRE
    assert rl.classify(_row(60)) == rl.RETIRE


def test_inactive_or_other_verdict_is_skipped():
    assert rl.classify(_row(6, active=0)) == rl.SKIP
    assert rl.classify(_row(6, verdict="partial")) == rl.SKIP


def test_plan_buckets_are_disjoint_and_complete():
    rows = [_row(0), _row(5), _row(12), _row(30), _row(3, verdict="verified")]
    res = rl.plan(rows, rl.MIN_TRADES_FOR_VERDICT)
    assert len(res["relabel"]) == 2
    assert len(res["retire"]) == 2
    assert len(set(map(id, res["relabel"])) & set(map(id, res["retire"]))) == 0


# ── 3. 提示词侧不再注入假教训 ───────────────────────────────────────────────
def test_prompt_loader_filters_unverified_verdicts():
    """load_insight_rules 的 SQL 必须显式挡掉 failed 与 insufficient_sample。"""
    src = (Path(__file__).resolve().parents[1] / "src_python" / "ai_prompts.py").read_text(
        encoding="utf-8")
    assert "'failed', 'insufficient_sample'" in src
    assert "NOT IN ('failed', 'insufficient_sample')" in src


def test_filtered_query_excludes_thin_rows(tmp_path):
    """用内存库复现过滤效果: 只有 partial 那条能进 prompt。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE review_insights (id INTEGER PRIMARY KEY, rule TEXT, asset TEXT, "
                 "scope TEXT, source_samples INTEGER, kind INTEGER, counter_factual TEXT, "
                 "active INTEGER, manual_deactivated INTEGER, verification_verdict TEXT)")
    insert = ("INSERT INTO review_insights VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(insert, (1, "thin failed", "XAU", "", 10, 1, "", 1, 0, "failed"))
    conn.execute(insert, (2, "thin insufficient", "XAU", "", 10, 1, "", 1, 0, "insufficient_sample"))
    conn.execute(insert, (3, "solid partial", "XAU", "", 10, 1, "", 1, 0, "partial"))
    rows = conn.execute(
        "SELECT rule FROM review_insights WHERE active=1 AND manual_deactivated=0 AND kind=1 "
        "AND COALESCE(source_samples,0)>=1 "
        "AND COALESCE(verification_verdict,'') NOT IN ('failed','insufficient_sample')"
    ).fetchall()
    assert [r[0] for r in rows] == ["solid partial"]
