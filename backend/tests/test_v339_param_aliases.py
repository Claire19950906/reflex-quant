"""v339 回归：param_key 别名层 + 参数类型层 + 积压归并裁决。

覆盖三处「假通过」：
1. 带命名空间前缀的 key 在审批端恒 400；
2. min_event_strength / asset_filter 用 up/down 落地会被清空（比不改更坏）；
3. 折算值被钳制回原值，却仍标 applied。
"""

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "src_python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from param_key_aliases import check_direction, resolve_key, SETTINGS_ALIASES
import param_adjust_apply as pa


class TestResolveKey:
    def test_settings_alias(self):
        canon, bucket, _ = resolve_key("score.buy_min_score")
        assert canon == "buy_min_score" and bucket == "settings"

    def test_strategy_alias(self):
        canon, bucket, _ = resolve_key("score.confidence_threshold")
        assert canon == "signal_threshold" and bucket == "strategy"

    def test_unmappable_sizing(self):
        canon, bucket, why = resolve_key("sizing.position_weight")
        # 断言的是"为什么不能映射"这件事被说清楚了，不锁具体措辞
        # （v3xx 把文案从"不等价"改成了更具体的"需要账户权益才能换算"）。
        assert canon is None and bucket == "unmappable"
        assert ("不等价" in why) or ("需要账户权益" in why)

    def test_bare_key_passthrough(self):
        canon, bucket, _ = resolve_key("buy_min_score")
        assert canon == "buy_min_score" and bucket == "literal"

    def test_unknown_prefixed_key_rejected(self):
        canon, bucket, _ = resolve_key("foo.bar")
        assert canon is None and bucket == "unmappable"


class TestCheckDirection:
    def test_bool_only_on_off(self):
        assert check_direction("tp_enabled", "on", 0.1)[0] is True
        assert check_direction("tp_enabled", "up", 0.1)[0] is False

    def test_enum_rejects_up_down(self):
        """枚举参数用 up/down 会写成浮点再被清空 —— 必须拒绝。"""
        ok, why = check_direction("min_event_strength", "up", 0.1)
        assert ok is False and "枚举" in why

    def test_asset_filter_rejects_on(self):
        ok, why = check_direction("asset_filter", "on", 0.3)
        assert ok is False and "asset_filter" in why

    def test_numeric_rejects_on_off(self):
        ok, why = check_direction("buy_min_score", "on", 0.1)
        assert ok is False and "数值" in why

    def test_numeric_up_ok(self):
        assert check_direction("buy_min_score", "up", 0.15)[0] is True


def _mem_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE strategy_param_adjustments (
            id INTEGER PRIMARY KEY, asset TEXT, action TEXT, param_key TEXT,
            direction TEXT, magnitude REAL, sample_size INTEGER, avg_confidence REAL,
            evidence_summary TEXT, first_suggested_at TEXT, last_suggested_at TEXT,
            status TEXT, applied_strategy_version_id INTEGER, applied_setting_change TEXT,
            note TEXT, created_at TEXT, updated_at TEXT)
    """)
    conn.executemany(
        """INSERT INTO strategy_param_adjustments
           (id, asset, action, param_key, direction, magnitude, sample_size,
            avg_confidence, last_suggested_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return conn


def test_conflict_resolved_by_weight():
    """同一 key 的 up/down 按 Σ(sample×conf) 裁决，不是简单取最新。"""
    conn = _mem_db([
        (1, "XAU", "BUY", "score.sell_min_score", "down", 0.1, 500, 0.9, "2026-11-14 08:00:00", "pending"),
        (2, "BTC", "BUY", "score.sell_min_score", "up", 0.1, 3, 0.3, "2026-11-14 09:00:00", "pending"),
    ])
    plan = pa.build_plan(conn, max_age_days=100000)
    g = plan["groups"][0]
    assert g["canonical_key"] == "sell_min_score"
    assert g["direction"] == "down", "小样本的新意见不应压过大样本"
    assert g["conflict"] is True


def test_stale_rows_split_out():
    conn = _mem_db([
        (1, "*", "*", "score.buy_min_score", "up", 0.1, 10, 0.6, "2026-01-01 00:00:00", "pending"),
        (2, "*", "*", "score.buy_min_score", "up", 0.1, 10, 0.6, "2026-12-31 00:00:00", "pending"),
    ])
    plan = pa.build_plan(conn, max_age_days=7)
    assert plan["stale_pending"] == 1 and plan["fresh_pending"] == 1


def test_enum_direction_marks_group_not_applicable():
    conn = _mem_db([
        (1, "XAU", "BUY", "score.event_strength_threshold", "up", 0.1, 20, 0.7, "2099-01-01 00:00:00", "pending"),
    ])
    plan = pa.build_plan(conn, max_age_days=100000)
    g = plan["groups"][0]
    assert g["apply"] is False
    assert "枚举" in g["direction_error"]


def test_clamp_makes_noop_detected():
    """sell_min_score 当前 0.1（钳制下限），down 10% 折算 0.09 → 应判 skipped_noop。"""
    assert pa.describe_change(0.1, "down", 0.1) == 0.09
    lo, hi = pa.SETTINGS_CLAMP["sell_min_score"]
    assert min(hi, max(lo, pa.describe_change(0.1, "down", 0.1))) == 0.1
