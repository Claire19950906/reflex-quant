# -*- coding: utf-8 -*-
"""tests/test_signal_weight_diagnostics.py — 权重判别力诊断 (v324)

全部离线: 纯函数用例不需要 DB; 需要 DB 的用例走 tmp_path 临时库。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config
from signals import signal_weight_diagnostics as swd

# 本地通道是否启用：本机 .env 里 LOCAL_LLM_ENABLED=0 时 roster 里根本没有
# 三条本地通道，这条 v324 回归（id/key_alias 唯一）就无对象可验。
_LOCAL_ENABLED = bool(getattr(config, "LOCAL_LLM_ENABLED", False))


# ── spearman ──────────────────────────────────────────────────────────────
def test_spearman_perfect_monotonic():
    assert swd.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert swd.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_too_few_samples_returns_none():
    assert swd.spearman([1, 2], [1, 2]) is None
    assert swd.spearman([], []) is None


def test_spearman_constant_series_returns_none():
    """任一侧是常数 → 无秩 → None (不能返回 0 冒充"不相关")。"""
    assert swd.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert swd.spearman([1, 2, 3, 4], [5, 5, 5, 5]) is None


def test_spearman_ties_use_average_rank():
    """并列值取平均秩 —— 与 capability_acceptance._spearman 同口径。"""
    assert swd.spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)


def test_spearman_length_mismatch_returns_none():
    assert swd.spearman([1, 2, 3], [1, 2]) is None


# ── tier_label ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rho,expected", [
    (0.35, "达标"),
    (0.20, "达标"),
    (0.19, "接近达标"),
    (0.15, "接近达标"),
    (0.05, "未达线"),
    (-0.1, "反向"),
    (None, "不可判"),
])
def test_tier_label(rho, expected):
    assert swd.tier_label(rho, 0.20) == expected


# ── 乘子坍缩检测 ──────────────────────────────────────────────────────────
def test_detect_collapse_dead_constant():
    info = swd.detect_multiplier_collapse([1.0] * 100)
    assert info["status"] == "完全退化"
    assert info["distinct"] == 1
    assert info["mode_ratio"] == pytest.approx(1.0)


def test_detect_collapse_mostly_constant():
    """v324 实测形态: base 乘子 85.7% 是 0.55 → 已坍缩。"""
    vals = [0.55] * 857 + [round(0.5 + i / 1000.0, 4) for i in range(143)]
    info = swd.detect_multiplier_collapse(vals)
    assert info["status"] == "已坍缩"
    assert info["mode"] == pytest.approx(0.55)
    assert 0.8 <= info["mode_ratio"] < 0.95


def test_detect_collapse_healthy_dimension():
    info = swd.detect_multiplier_collapse([round(i / 100.0, 4) for i in range(100)])
    assert info["status"] == "有区分度"
    assert info["distinct"] == 100


def test_detect_collapse_no_data():
    info = swd.detect_multiplier_collapse([])
    assert info["status"] == "无数据"
    assert info["n"] == 0


def test_detect_collapse_ignores_none():
    info = swd.detect_multiplier_collapse([None, None, 1.0, 2.0])
    assert info["n"] == 2


# ── 分层切分 ──────────────────────────────────────────────────────────────
def test_slice_rho_empty():
    assert swd.slice_rho([])["status"] == "不可判"


def test_slice_rho_positive():
    rows = [(0.1 * i, 1.0 * i) for i in range(1, 30)]
    out = swd.slice_rho(rows, 0.20)
    assert out["rho"] == pytest.approx(1.0)
    assert out["status"] == "达标"
    assert out["n"] == 29


def test_by_tier_splits_correctly():
    rows = [("strong", 0.6 + i / 1000.0, 1.0 + i) for i in range(10)]
    rows += [("weak", 0.4 + i / 1000.0, 1.0 + i) for i in range(10)]
    out = swd.by_tier(rows)
    assert set(out.keys()) == {"strong", "weak"}
    assert out["strong"]["n"] == 10


def test_by_regime_splits_correctly():
    # by_regime 的行格式是 (权重, |超额|, regime)
    rows = [(0.5, 1.0, "低波") for _ in range(5)] + [(0.5, 2.0, "高波") for _ in range(7)]
    out = swd.by_regime(rows)
    assert out["低波"]["n"] == 5
    assert out["高波"]["n"] == 7


def test_by_month_groups_by_calendar_month():
    import time
    ts = int(time.mktime(time.strptime("2026-10-15", "%Y-%m-%d")))
    ts2 = int(time.mktime(time.strptime("2026-11-15", "%Y-%m-%d")))
    out = swd.by_month([(0.5, 1.0, ts), (0.6, 2.0, ts2)])
    assert "2026-10" in out and "2026-11" in out




# ── DB 层 (tmp_path 临时库) ───────────────────────────────────────────────
def _make_db(path: Path, decisions, history):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, ts INTEGER, forward_move_pct REAL)""")
    conn.execute("""CREATE TABLE signal_weight_history (
        id INTEGER PRIMARY KEY, ai_decision_id INTEGER, decision_space TEXT,
        trigger_kind TEXT, signal_weight REAL, weight_tier TEXT,
        base_mult REAL, consensus_mult REAL, source_mult REAL,
        reaction_mult REAL, regime_mult REAL, time_mult REAL, impact_mult REAL)""")
    conn.executemany(
        "INSERT INTO ai_decisions (id, ts, forward_move_pct) VALUES (?,?,?)", decisions)
    conn.executemany(
        "INSERT INTO signal_weight_history (ai_decision_id, decision_space, trigger_kind, "
        "signal_weight, weight_tier, base_mult, consensus_mult, source_mult, reaction_mult, "
        "regime_mult, time_mult, impact_mult) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", history)
    conn.commit()
    conn.close()


def test_collect_ex_ante_filters_to_insert_rows(tmp_path):
    db = tmp_path / "t.db"
    _make_db(
        db,
        [(i, 1794000000 + i * 60, 1.0 + i) for i in range(1, 21)],
        [(i, "ai", "insert", 0.5 + i / 100.0, "medium",
          0.55, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(1, 21)],
    )
    conn = swd._ro(db)
    try:
        out = swd.collect_ex_ante_samples(conn)
    finally:
        conn.close()
    assert out["samples"], "应取到 insert 行"
    assert all(s["tier"] == "medium" for s in out["samples"])
    assert out["multipliers"]["base_mult"][0] == pytest.approx(0.55)


def test_collect_ex_ante_excludes_post_hoc_rows(tmp_path):
    db = tmp_path / "t.db"
    _make_db(
        db,
        [(i, 1794000000 + i * 60, 1.0) for i in range(1, 6)],
        [
            (1, "ai", "insert", 0.5, "medium", 0.55, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            (2, "ai", "cron_react", 0.5, "medium", 0.55, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            (3, "trade", "backfill_trade", 0.5, "medium", 0.55, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        ],
    )
    conn = swd._ro(db)
    try:
        out = swd.collect_ex_ante_samples(conn)
    finally:
        conn.close()
    assert len(out["samples"]) == 1, "只应保留 trigger_kind='insert' + decision_space='ai'"


def test_compute_diagnostics_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    decs, hist = [], []
    for i in range(1, 61):
        decs.append((i, 1794000000 + i * 60, 0.01 * i))
        hist.append((i, "ai", "insert", 0.30 + 0.005 * i, "medium",
                     0.55, 1.0, 1.0, 1.0, 1.0, 1.0, 0.8 + i / 100.0))
    _make_db(db, decs, hist)
    conn = swd._ro(db)
    try:
        diag = swd.compute_diagnostics(conn)
    finally:
        conn.close()
    assert diag["sample_count"] == 60
    assert diag["overall"]["n"] == 60
    assert "base_mult" in diag["multiplier_collapse"]
    assert diag["multiplier_collapse"]["base_mult"]["status"] == "完全退化"
    text = swd.render_report(diag)
    assert "权重判别力诊断" in text
    assert "乘子坍缩自检" in text


# ── v324 回归: 本地通道 id / key_alias 必须唯一 ───────────────────────────
@pytest.mark.skipif(
    not _LOCAL_ENABLED,
    reason="本机 LOCAL_LLM_ENABLED=0（roster 里没有本地通道），无对象可验；开启后仍生效"
)
def test_local_channels_have_unique_ids_and_aliases():
    """v324 P0 回归。

    v322 想按用途给本地通道去重, 但判据写成「模型名 == LOCAL_LLM_FAST_MODEL」,
    而 .env 里三个本地模型名都是 qwen2.5:3b → 第一个 if 永远命中 →
    三个 id 全同 → 熔断器 (按 key_alias 命名) 串味, 一个通道坏三个一起被拉黑。
    """
    import config
    locals_ = [m for m in config.AI_MODEL_ROSTER if m.get("is_local")]
    assert len(locals_) == 3, "应有 3 个本地通道"
    ids = [m["id"] for m in locals_]
    aliases = [m["key_alias"] for m in locals_]
    assert len(set(ids)) == len(ids), "本地通道 id 必须唯一: %s" % ids
    assert len(set(aliases)) == len(aliases), "本地通道 key_alias 必须唯一: %s" % aliases
    assert all(a != "local" for a in aliases), "key_alias 不应再共用 'local'"

def test_vol_regime_buckets():
    assert swd._vol_regime(0.5, 1.0, 2.0) == "低波"
    assert swd._vol_regime(1.5, 1.0, 2.0) == "中波"
    assert swd._vol_regime(3.0, 1.0, 2.0) == "高波"
