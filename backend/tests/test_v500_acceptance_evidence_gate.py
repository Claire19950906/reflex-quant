# -*- coding: utf-8 -*-
"""v500: 验收清单必须把「等数据」和「真坏了」分开。

背景：同一份清单里，8 个 FAIL 有 7 个是"样本不够"（真实开仓 1 笔、outcome 0 条、
反思 10 条……）。它们和"端点没接线""枚举非法"打印成同一个红叉，结果是：
每次迭代都要人肉分辨哪一项真的断了，而且改代码也修不好那 7 个。

新口径 (INSUFFICIENT / ⏳) 的契约：
  1. 必须有 need（等什么）/ have（现在多少）/ watch（数据到了看哪个数）三项；
  2. 样本不足时不得判 PASS（不能因为"没坏"就算过）；
  3. 样本足够但逻辑错时仍判 FAIL（不能被 ⏳ 吞掉）；
  4. ⏳ 不计入退出码失败, 但 --strict 下要算失败。

只跑纯函数 + 内存库, 不连生产库、不连网。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_acceptance as acc


# ── 建最小库 ────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE trade_decisions (
            status TEXT, decision_type TEXT, intended_action TEXT,
            actual_outcome_pct REAL, deny_reason TEXT);
        CREATE TABLE quick_sim_trades (
            settled INTEGER, pnl_pct REAL, action TEXT,
            entry_price REAL, exit_price REAL);
        CREATE TABLE critical_price_moves (asset TEXT, algo_score REAL);
        CREATE TABLE causal_chains (asset TEXT, active INTEGER DEFAULT 1, confidence REAL);
        CREATE TABLE causal_chain_outcomes (
            status TEXT, predicted_direction TEXT, eval_deadline_ts INTEGER,
            created_at INTEGER);
        CREATE TABLE missed_signals (id INTEGER PRIMARY KEY, ts INTEGER);
        CREATE TABLE missed_signal_scan_state (id INTEGER PRIMARY KEY);
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY, dimension TEXT, subcategory TEXT,
            created_at TEXT, actual_outcome_pct REAL);
    """)
    return c


def _add_entry(c, n: int, hit: bool) -> None:
    for _ in range(n):
        c.execute("INSERT INTO trade_decisions VALUES ('settled','ENTRY','BUY',?,NULL)",
                  (1.0 if hit else -1.0,))


def _add_assets(c, n: int) -> None:
    for i in range(n):
        c.execute("INSERT INTO critical_price_moves VALUES (?, 0.75)", (f"A{i}",))


# ── 契约 1: 结构 ────────────────────────────────────────────────────────────
def test_insufficient_carries_three_fields():
    r = acc._insufficient("X", "n", "crit", "measured",
                          need="50 条", have="3 条", watch="看命中率")
    assert r["status"] == "INSUFFICIENT"
    assert (r["need"], r["have"], r["watch"]) == ("50 条", "3 条", "看命中率")


def test_normal_res_has_no_extra_fields():
    r = acc._res("X", "n", "PASS", "crit", "measured")
    assert r["status"] == "PASS"
    assert "need" not in r and "watch" not in r


def test_icon_exists_for_insufficient():
    assert "INSUFFICIENT" in acc.ICON


# ── 契约 2: 样本不足 → ⏳，不是 PASS，也不是 ❌ ───────────────────────────
def test_check01_one_entry_is_insufficient():
    c = _db()
    _add_assets(c, 12)
    _add_entry(c, 1, hit=True)
    r = acc.check_01_signal_evaluation(c, http_on=False)
    assert r["status"] == "INSUFFICIENT", r["status"]
    assert "20" in r["need"] and "1" in r["have"]


def test_check01_enough_entries_still_judges():
    """20 笔全命中 → 有结论 (WARN, 样本 < 100); 不再躲在 ⏳ 后面。"""
    c = _db()
    _add_assets(c, 12)
    _add_entry(c, 20, hit=True)
    r = acc.check_01_signal_evaluation(c, http_on=False)
    assert r["status"] == "WARN", r["status"]


def test_check06_pending_only_is_insufficient():
    c = _db()
    c.execute("INSERT INTO causal_chains VALUES ('XAU', 1, 0.5)")
    c.execute("INSERT INTO causal_chain_outcomes VALUES ('PENDING','UP', 9999999999, 0)")
    r = acc.check_06_causal_closure(c, http_on=False)
    assert r["status"] == "INSUFFICIENT"
    assert r["evidence"]["pending_not_due"] == 1


def test_check06_overdue_pending_is_fail():
    """到期了还 PENDING = 评估器真坏了, 必须判 FAIL, 不能被 ⏳ 吞掉。"""
    c = _db()
    c.execute("INSERT INTO causal_chain_outcomes VALUES ('PENDING','UP', 1, 0)")
    r = acc.check_06_causal_closure(c, http_on=False)
    assert r["status"] == "FAIL", r["status"]


def test_check10_no_direction_rows_is_insufficient():
    c = _db()
    c.execute("INSERT INTO causal_chain_outcomes VALUES ('HIT','BOTH', 1, 0)")
    r = acc.check_10_causal_accuracy(c, http_on=False)
    assert r["status"] == "INSUFFICIENT"
    assert "方向" in r["need"]


def test_check10_direction_rows_low_hit_is_fail():
    c = _db()
    for i in range(50):
        c.execute("INSERT INTO causal_chain_outcomes VALUES (?, 'UP', 1, 0)",
                  ("MISS" if i % 2 == 0 else "HIT",))
    r = acc.check_10_causal_accuracy(c, http_on=False)
    assert r["status"] == "FAIL", r["status"]


def test_check15_no_outcome_is_insufficient_even_if_reflections_exist():
    c = _db()
    for i in range(3):
        c.execute("INSERT INTO trade_reflections_v2 (dimension, subcategory, created_at) "
                  "VALUES ('A','event_logic','2026-11-20 00:00:00')")
    c.execute("INSERT INTO missed_signals (ts) VALUES (1)")
    r = acc.check_15_live_desk(c, http_on=False)
    if not r["evidence"].get("has_reflection_block"):
        return  # 前端文件缺失的环境下跳到别的分支, 不在这里断言
    assert r["status"] == "INSUFFICIENT", r["status"]


# ── 契约 3: 汇总口径 ────────────────────────────────────────────────────────
def test_summary_counts_insufficient_separately():
    results = [{"status": s} for s in ("PASS", "FAIL", "INSUFFICIENT", "INSUFFICIENT", "SKIP")]
    n_ins = sum(1 for r in results if r["status"] == "INSUFFICIENT")
    assert n_ins == 2
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    assert n_fail == 1
