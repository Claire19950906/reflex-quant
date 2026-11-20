"""v482 反思三修：反思的"判断对不对"改由 4h/24h 真值回答。

背景：
    反思行的 pnl_pct 来自 20~40 分钟纸上单（毛幅中位 0.09%，往返成本 0.3%），
    用它判对错 = 用噪声判对错。4h/24h 真值早就在 shadow_reflections 里，只是没回接。

验收：
  1) 非模板反思按 decision_id join 影子行，写 truth_4h_verdict / truth_4h_net_pct；
  2) 净收益 = 方向符号 × 实际涨跌 − 往返成本（BUY +0.5% ETH → +0.2）；
  3) 没有影子行 → 留空，不写 0（判不了 ≠ 判错）；
  4) 模板行不盖章（它不在反思统计里了）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import db
import shadow_reflections as _sr
from cost_model import apply_costs
from reflection_horizon_truth import stamp, report


def _setup_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # shadow_reflections 由影子模块自建，24h 列由 db.migrate 补齐
    _sr._ensure_schema(c)
    db.migrate(c)
    db.ensure_latest_columns(c)
    return c


def _reflection(c, rid, decision_id, asset="ETH", action="BUY",
                origin="paper"):
    c.execute(
        """INSERT INTO trade_reflections_v2
           (id, decision_id, asset, action, layer, sub_layer, signal_name,
            pnl_pct, settled, origin, created_at)
           VALUES (?, ?, ?, ?, 'trade', 'quick_sim', 'momentum',
                   -0.3, 1, ?, datetime('now'))""",
        (rid, decision_id, asset, action, origin),
    )


def _shadow(c, decision_id, asset="ETH", direction="BUY",
            verdict="direction_ok", move=0.5,
            verdict_24h=None, move_24h=None):
    c.execute(
        """INSERT INTO shadow_reflections
           (source, source_id, decision_id, asset, claimed_direction, verdict,
            realized_move_pct, verdict_24h, move_pct_24h, created_at)
           VALUES ('shadow_ledger', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (decision_id, decision_id, asset, direction, verdict, move,
         verdict_24h or '', move_24h),
    )


def test_stamp_ok_and_wrong_with_net():
    c = _setup_db()
    _reflection(c, 1, 1001, asset="ETH", action="BUY")
    _shadow(c, 1001, asset="ETH", direction="BUY",
            verdict="direction_ok", move=0.5,
            verdict_24h="direction_wrong", move_24h=-0.4)
    _reflection(c, 2, 1002, asset="BTC", action="SELL")
    _shadow(c, 1002, asset="BTC", direction="SELL",
            verdict="direction_wrong", move=0.6)

    res = stamp(c, commit=True)
    assert res["matched"] == 2
    assert res["written"] == 2

    r1 = c.execute(
        "SELECT * FROM trade_reflections_v2 WHERE id=1").fetchone()
    assert r1["truth_4h_verdict"] == "direction_ok"
    assert r1["truth_4h_net_pct"] == pytest.approx(
        float(apply_costs(0.5, "ETH", "BUY")), abs=1e-4)   # +0.5 - 0.3 = +0.2
    assert r1["truth_24h_verdict"] == "direction_wrong"
    assert r1["truth_24h_net_pct"] == pytest.approx(
        float(apply_costs(-0.4, "ETH", "BUY")), abs=1e-4)  # -0.4 - 0.3 = -0.7
    assert r1["truth_stamped_at"]

    r2 = c.execute(
        "SELECT * FROM trade_reflections_v2 WHERE id=2").fetchone()
    assert r2["truth_4h_verdict"] == "direction_wrong"
    # SELL 遇到 +0.6% 上涨 → 毛 -0.6，扣 30bp → -0.9
    assert r2["truth_4h_net_pct"] == pytest.approx(
        float(apply_costs(-0.6, "BTC", "SELL")), abs=1e-4)


def test_no_shadow_row_stays_empty_not_zero():
    c = _setup_db()
    _reflection(c, 11, 1011)
    res = stamp(c, commit=True)
    assert res["matched"] == 0
    r = c.execute("SELECT * FROM trade_reflections_v2 WHERE id=11").fetchone()
    assert r["truth_4h_verdict"] == ""
    assert r["truth_4h_net_pct"] is None, "判不了必须留 NULL，不能写 0"


def test_template_rows_not_stamped():
    c = _setup_db()
    _reflection(c, 21, 1021, origin="template")
    _shadow(c, 1021)
    res = stamp(c, commit=True)
    assert res["matched"] == 0
    r = c.execute("SELECT * FROM trade_reflections_v2 WHERE id=21").fetchone()
    assert r["truth_4h_verdict"] == ""


def test_stamp_idempotent_and_updates_later_24h():
    c = _setup_db()
    _reflection(c, 31, 1031)
    _shadow(c, 1031, verdict="direction_ok", move=0.5,
            verdict_24h=None, move_24h=None)
    stamp(c, commit=True)
    # 24h 结果迟到：影子行更新后再次盖章应补上，而不是报错/覆盖成空
    c.execute("UPDATE shadow_reflections SET verdict_24h='direction_ok',"
              " move_pct_24h=1.2 WHERE decision_id=1031")
    stamp(c, commit=True)
    r = c.execute("SELECT * FROM trade_reflections_v2 WHERE id=31").fetchone()
    assert r["truth_24h_verdict"] == "direction_ok"
    assert r["truth_24h_net_pct"] == pytest.approx(
        float(apply_costs(1.2, "ETH", "BUY")), abs=1e-4)


def test_report_separates_unstamped():
    c = _setup_db()
    _reflection(c, 41, 1041)
    _reflection(c, 42, 1042)
    _shadow(c, 1041)
    stamp(c, commit=True)
    rep = report(c)
    assert rep["real_reflections"] == 2
    assert rep["truth_4h"].get("direction_ok") == 1
    assert rep["truth_4h"].get("(未盖章)") == 1
