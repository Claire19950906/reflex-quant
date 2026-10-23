"""
Q4 fix verification: event_move_attribution 覆盖率从 1.5% → 30%+
通过新增 Tier 2 (spillover) + Tier 3 (macro_fallback) 实现
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
SCRIPT = BACKEND / "src_python" / "calc_event_move_attribution.py"


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_cli_args_accept_tiers():
    """CLI 必须支持 --enable-spillover 和 --enable-macro-fallback。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert "--enable-spillover" in result.stdout, "missing --enable-spillover"
    assert "--enable-macro-fallback" in result.stdout, "missing --enable-macro-fallback"
    assert "--macro-threshold" in result.stdout, "missing --macro-threshold"


def test_spillover_action_label_exists(conn):
    """spillover ev_action 必须存在于表中 (Tier 2 启用证据)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM event_move_attribution WHERE ev_action='SPILLOVER'"
    ).fetchone()[0]
    assert n > 0, "no SPILLOVER rows — Tier 2 not active"


def test_macro_action_label_exists(conn):
    """macro ev_action 必须存在 (Tier 1 macro + Tier 3 macro_fallback)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM event_move_attribution WHERE ev_action='MACRO'"
    ).fetchone()[0]
    assert n > 0, "no MACRO rows"


def test_coverage_exceeds_30pct(conn):
    """Q4 覆盖率必须 >= 30% (审计要求)."""
    # 计算方式: 90d 内 critical_price_moves 中, 至少有一行 event_move_attribution 引用的比例
    cutoff = conn.execute(
        "SELECT strftime('%s','now','-90 days')"
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM critical_price_moves WHERE ts>=?", (cutoff,)
    ).fetchone()[0]
    covered = conn.execute(
        "SELECT COUNT(DISTINCT ema.move_id) FROM event_move_attribution ema "
        "JOIN critical_price_moves cpm ON ema.move_id = cpm.id WHERE cpm.ts>=?",
        (cutoff,),
    ).fetchone()[0]
    pct = round(100 * covered / max(total, 1), 1)
    print(f"\n[Q4 coverage] {covered}/{total} = {pct}%")
    assert pct >= 30.0, f"coverage {pct}% below 30% target"


def test_low_confidence_macro_has_low_score(conn):
    """Tier 3 macro_fallback 应有低置信度 (algo_score <= 0.55 上限) — 防止伪造高置信."""
    high_score_macros = conn.execute(
        "SELECT COUNT(*) FROM event_move_attribution "
        "WHERE ev_action='MACRO' AND algo_score > 0.55"
    ).fetchone()[0]
    assert high_score_macros == 0, \
        f"Tier 3 macro_fallback should cap at 0.55, found {high_score_macros} rows above"


def test_multi_tier_attribution_per_move(conn):
    """部分 move 应有多层归因 (Tier 1 + Tier 2 同时命中, 因为 TOP_N_PER_MOVE=3)."""
    multi = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT move_id FROM event_move_attribution "
        "  GROUP BY move_id HAVING COUNT(*) >= 2"
        ")"
    ).fetchone()[0]
    assert multi > 0, "no moves with multi-tier attribution"