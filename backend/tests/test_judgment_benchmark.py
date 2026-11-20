#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断力基准层（v490）单元测试 —— 全部内存库，绝不碰生产库。

为什么这套测试重要
------------------
「判断能力 vs 市场」的整个价值在于**口径正确**，而口径错没错，用真实数据
是验不出来的（真实世界只有一个答案，而且掺杂了行情、样本量、成本三种噪音）。
所以这里用**已知答案的合成世界**来验：

    coin  方向随机/涨跌随机    → 必须判"无优势"
    bias  全喊 SELL + 65% 在跌 → 裸命中高，但必须判"beta / 无判别力"
    skill 两类各 62%           → 必须判"有判别力"
    anti  全喊 SELL + 35% 在跌 → 必须判"反向"（这是只有 BA' 能抓到的）

再加 4 组"接口契约"测试：空库/缺表/未结算行/两个 loader 的过滤口径 ——
保证数据还没进来时程序优雅降级，数据进来后不用改逻辑。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import judgment_benchmark as jb
import capability_scorecard as cs


# ── 工具 ─────────────────────────────────────────────────────────────────────
def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE shadow_ledger (
            decision_id INTEGER PRIMARY KEY, decided_ts INTEGER, asset TEXT,
            direction TEXT, action_after TEXT, confidence REAL, gate REAL,
            entry_ts INTEGER, entry_price REAL, horizon_min INTEGER,
            exit_ts INTEGER, exit_price REAL, move_pct REAL, hit INTEGER,
            settled_at INTEGER);
        CREATE TABLE shadow_reflections (
            id INTEGER PRIMARY KEY, source TEXT, source_id INTEGER, decision_id INTEGER,
            asset TEXT, claimed_direction TEXT, claimed_confidence REAL,
            realized_direction TEXT, realized_move_pct REAL, entry_ts INTEGER,
            exit_ts INTEGER, entry_price REAL, exit_price REAL, window_min REAL,
            followed INTEGER, verdict TEXT, verdict_basis TEXT, attribution TEXT,
            evidence TEXT, reflection_effective INTEGER, created_at TEXT,
            verdict_24h TEXT, effective_24h INTEGER, price_24h REAL,
            move_pct_24h REAL, settled_24h_at TEXT);
    """)
    return conn


def _insert_ledger(conn: sqlite3.Connection, rows) -> None:
    for i, (asset, direction, move) in enumerate(rows, start=1):
        hit = 1 if ((direction == "BUY" and move > 0) or (direction == "SELL" and move < 0)) else 0
        conn.execute(
            "INSERT INTO shadow_ledger (decision_id, decided_ts, asset, direction, confidence,"
            " entry_ts, horizon_min, move_pct, hit, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, 1700000000 + i, asset, direction, 0.6, 1700000000 + i, 240, move, hit, 1700010000),
        )
    conn.commit()


def _rows_from_fixture(world: str, n: int = 240):
    return [(s.asset, s.direction, s.move_pct) for s in jb.make_fixture(world, n=n)]


# ── 1. 四个已知世界：判定必须与设计一致 ──────────────────────────────────────
@pytest.mark.parametrize("world,expect_codes,ba_range", [
    ("coin", {"flat", "marginal"}, (46, 54)),
    ("bias", {"beta", "flat"}, (48, 52)),
    ("skill", {"edge"}, (58, 68)),
    ("anti", {"inverted"}, (48, 52)),
])
def test_world_verdict(world, expect_codes, ba_range):
    pack = jb.benchmark(jb.make_fixture(world, n=400))
    assert pack["verdict"]["code"] in expect_codes, pack["verdict"]
    assert ba_range[0] <= pack["ba_label"]["value"] <= ba_range[1]


def test_bias_world_proves_hit_rate_is_not_skill():
    """核心回归：裸命中 65%+ 的世界里，去偏置后必须是 50%（beta 陷阱）。"""
    pack = jb.benchmark(jb.make_fixture("bias", n=400))
    assert pack["hit_rate"] >= 60            # 裸命中率高
    assert 48 <= pack["ba_label"]["value"] <= 52   # 但判别力为零
    assert abs(pack["excess_vs_mix"]) <= 3   # 相对"同配比盲选"没有超额
    assert pack["baseline"]["always_sell"] >= 60


def test_single_direction_ba_is_exactly_fifty():
    """只喊一个方向时 BA 恒 50 —— 这是正确定义，不是 bug；BA' 才是能看出反指的量。"""
    up = [("BTC", "SELL", 0.5)] * 40        # 40 条全喊 SELL，行情涨
    down = [("BTC", "SELL", -0.5)] * 60      # 60 条全喊 SELL，行情跌
    pack = jb.benchmark([jb.Outcome(a, d, m) for a, d, m in up + down], gate=50)
    assert pack["ba_label"]["value"] == pytest.approx(50.0, abs=0.2)
    assert pack["ba_pred"] == pytest.approx(60.0, abs=0.5)   # 60/100 命中
    assert pack["verdict"]["code"] in {"beta", "flat"}


def test_inverted_world_is_flagged_even_when_ba_is_fifty():
    pack = jb.benchmark(jb.make_fixture("anti", n=400))
    assert pack["ba_label"]["value"] == pytest.approx(50.0, abs=1.5)
    assert pack["ba_pred"] < 45
    assert pack["verdict"]["code"] == "inverted"


# ── 2. 统计口径的算术必须精确（不依赖随机）────────────────────────────────
def test_expectancy_arithmetic_is_exact():
    """BUY +1.0% / SELL -1.0% 各 5 条 → 毛期望 100bps，扣 12bps 净 88bps。"""
    samples = [jb.Outcome("BTC", "BUY", 1.0) for _ in range(5)] + \
              [jb.Outcome("BTC", "SELL", -1.0) for _ in range(5)]
    pack = jb.net_expectancy(samples, fee_bps=12.0)
    assert pack["gross_bps"] == pytest.approx(100.0, abs=0.01)
    assert pack["net_bps"] == pytest.approx(88.0, abs=0.01)
    assert pack["n"] == 10


def test_cost_can_turn_positive_into_negative():
    """方向对、幅度小：毛 +5bps，扣 12bps → 净 -7bps。判对 ≠ 赚钱。"""
    samples = [jb.Outcome("XAU", "BUY", 0.05) for _ in range(10)]
    pack = jb.net_expectancy(samples, fee_bps=12.0)
    assert pack["gross_bps"] == pytest.approx(5.0, abs=0.01)
    assert pack["net_bps"] == pytest.approx(-7.0, abs=0.01)


def test_baseline_block_math():
    """3 涨 / 2 跌 / 1 平（平盘剔除），全喊 SELL → always_sell=40%。"""
    samples = [jb.Outcome("BTC", "SELL", 0.3), jb.Outcome("BTC", "SELL", 0.3),
               jb.Outcome("BTC", "SELL", 0.3), jb.Outcome("BTC", "SELL", -0.3),
               jb.Outcome("BTC", "SELL", -0.3), jb.Outcome("BTC", "SELL", 0.0)]
    pack = jb.benchmark(samples)
    assert pack["n_settled"] == 5 and pack["baseline"]["flat"] == 1
    assert pack["baseline"]["always_sell"] == pytest.approx(40.0, abs=0.1)
    assert pack["baseline"]["always_buy"] == pytest.approx(60.0, abs=0.1)


def test_ci_is_analytic_and_deterministic():
    a = jb.benchmark(jb.make_fixture("skill", n=300))
    b = jb.benchmark(jb.make_fixture("skill", n=300))
    assert a["ba_label"]["ci"] == b["ba_label"]["ci"]     # 可回归，不用随机种子


# ── 3. 数据适配契约：空库/缺表/过滤口径 ─────────────────────────────────────
def test_empty_db_degrades_gracefully():
    pack = jb.benchmark(jb.load_shadow_ledger(_mem_db()))
    assert pack["n_settled"] == 0
    assert pack["verdict"]["code"] == "empty"
    assert pack["ba_label"]["value"] is None


def test_missing_tables_return_empty():
    conn = sqlite3.connect(":memory:")           # 一张表都没有
    assert jb.load_shadow_ledger(conn) == []
    assert jb.load_shadow_reflections(conn) == []


def test_loader_ignores_unsettled_rows():
    conn = _mem_db()
    _insert_ledger(conn, [("BTC", "BUY", 0.4)])
    conn.execute("INSERT INTO shadow_ledger (decision_id, asset, direction, move_pct, hit, settled_at)"
                 " VALUES (99,'BTC','BUY',NULL,NULL,NULL)")     # 未结算
    conn.commit()
    samples = jb.load_shadow_ledger(conn)
    assert len(samples) == 1 and samples[0].asset == "BTC"


def test_reflection_loader_filters_undecidable():
    conn = _mem_db()
    for i, (v, mv) in enumerate([("direction_ok", 0.5), ("direction_wrong", -0.4),
                                 ("flat", 0.01), ("insufficient", None)], start=1):
        conn.execute("INSERT INTO shadow_reflections (id, asset, claimed_direction,"
                     " realized_move_pct, window_min, verdict) VALUES (?,?,?,?,?,?)",
                     (i, "BTC", "BUY", mv, 240, v))
    conn.commit()
    samples = jb.load_shadow_reflections(conn)
    assert len(samples) == 2                     # flat / insufficient 不算判得出
    assert {s.direction for s in samples} == {"BUY"}


def test_json_loader_roundtrip_and_bad_rows(tmp_path):
    p = tmp_path / "samples.jsonl"
    good = {"asset": "BTC", "direction": "SELL", "move_pct": -0.5}
    p.write_text("\n".join([json.dumps(good), "{坏行", json.dumps({"asset": "X"})]),
                 encoding="utf-8")
    samples = jb.load_json(p)
    assert len(samples) == 1 and samples[0].move_pct == pytest.approx(-0.5)
    assert jb.load_json(tmp_path / "missing.jsonl") == []


# ── 4. 与记分卡的接线：三个指标真的从同一批样本读出来了 ────────────────────
def test_scorecard_metrics_are_wired_and_consistent():
    conn = _mem_db()
    # 200 条、两类各 70% 判对 → S21≈70%、S22≈70%、S23 为正
    rows = []
    for i in range(200):
        if i % 2 == 0:
            rows.append(("BTC", "BUY", 0.8 if i % 10 < 7 else -0.8))
        else:
            rows.append(("XAU", "SELL", -0.8 if i % 10 < 7 else 0.8))
    _insert_ledger(conn, rows)

    metrics = {m.id: m for m in cs.METRICS}
    r21 = cs.evaluate_metric(conn, metrics["S21"])
    r22 = cs.evaluate_metric(conn, metrics["S22"])
    r23 = cs.evaluate_metric(conn, metrics["S23"])

    assert r21.n == 200 and r21.value == pytest.approx(70.0, abs=0.5)
    # BA = 两类召回平均，与裸命中同源但不等值（样本里多空配比不完全对称）
    assert r22.n == 200 and r22.value == pytest.approx(70.8, abs=1.5)
    assert r22.ci_low is not None and r22.ci_low > 50        # 真区间，不是 None
    assert r23.value is not None and r23.value > 0
    # 200 ≥ gate(200) → 允许下结论，状态不该再是"样本不足"
    assert r21.status != cs.ST_THIN and r22.status != cs.ST_THIN and r23.status != cs.ST_THIN


def test_scorecard_ignores_shadow_when_module_absent(monkeypatch):
    """可选依赖缺失时三个新指标降级为"样本不足"，既有指标不受影响。"""
    monkeypatch.setattr(cs, "_jb", None)
    conn = _mem_db()
    _insert_ledger(conn, [("BTC", "BUY", 0.5)])
    metrics = {m.id: m for m in cs.METRICS}
    for mid in ("S21", "S22", "S23"):
        r = cs.evaluate_metric(conn, metrics[mid])
        assert r.n == 0 and r.status == cs.ST_THIN


def test_selftest_all_green():
    assert jb.selftest(verbose=False) == 0


# ── 5. 每日反思成绩单的渲染：命中率旁边必须有基线/BA ──────────────────────
def test_daily_report_shows_baseline_next_to_hit_rate():
    """v490 回归：每日成绩单只报"命中率 56.7%"就是误导，必须带基线与 BA。"""
    import reflection_scorecard as rs

    pack = jb.benchmark(jb.make_fixture("bias", n=400))
    lines = rs._bench_lines({"bench": pack})
    text = "\n".join(lines)
    assert "同批市场基线" in text and "BA" in text
    assert "无脑做空" in text


def test_daily_report_zero_sample_explains_throughput():
    pack = jb.benchmark([])
    pack["throughput"] = {"window_hours": 24, "decisions": 15, "directional": 4,
                          "directional_per_hour": 0.2, "settleable": 0,
                          "settleable_per_hour": 0.0, "settled_total": 0,
                          "remaining_to_gate": 200, "eta_days": None}
    pack["verdict"] = jb.judge(pack)
    import reflection_scorecard as rs

    text = "\n".join(rs._bench_lines({"bench": pack}))
    assert "产能" in text
    assert "不等于能力为 0" in text
    assert "永远到不了" in text
