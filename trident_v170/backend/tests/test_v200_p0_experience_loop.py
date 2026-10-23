# -*- coding: utf-8 -*-
"""v200 P0 经验闭环修复测试 - 三件套: A1 关键词扩展 / A2 SPA boost / A3 注入回写。

A1: weekly_insight_apply._KEYWORD_TO_PARAM 扩展后，原本 skipped_no_param 的
    XAU BUY 反向 / 阈值调整 / 演员经验 / 同向预期证伪 等规则能成功抽取 param_key。
A2: spa_apply_worker 高样本 boost - sample_size >= 28 即使 avg_confidence 偏低
    也能推进到 applied，applied_setting_change 字段标 [boosted] 痕迹。
A3: ai_prompts.load_recent_insights_block(return_ids=True) 返回 (text, ids) tuple,
    缓存命中路径也返回 tuple。ai_worker 注入后命中 ids 写回 ai_decisions.insight_hit_ids。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))


@pytest.fixture
def spa_db():
    """给 spa_apply_worker / ai_prompts 测试用的内存 DB fixture。

    fresh_db 走 db.migrate(conn) 会因 strategy_versions 重建失败 (no such column: strategy_id)
    导致 6+ 测试 setup error。本 fixture 走捷径: 手工 CREATE 必需表, 不跑全 migrate。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE strategy_param_adjustments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asset       TEXT NOT NULL DEFAULT '*',
            action      TEXT NOT NULL DEFAULT '*',
            param_key   TEXT NOT NULL DEFAULT '',
            direction   TEXT NOT NULL DEFAULT 'up',
            magnitude   REAL NOT NULL DEFAULT 0.0,
            sample_size INTEGER NOT NULL DEFAULT 0,
            avg_confidence REAL NOT NULL DEFAULT 0.0,
            status      TEXT NOT NULL DEFAULT 'pending',
            applied_setting_change TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT 'rule',
            note        TEXT NOT NULL DEFAULT '',
            counter_factual TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE review_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT NOT NULL DEFAULT '',
            asset TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            source_samples INTEGER NOT NULL DEFAULT 0,
            kind INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            manual_deactivated INTEGER NOT NULL DEFAULT 0,
            counter_factual TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


class TestA1KeywordExpansion:
    def test_xau_buy_reverse_close_keyword(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key(
            "XAU BUY 持续反向 BUY 信号关闭, 应在 score>+0.15 时大幅下调 BUY 置信度"
        ) == "score.buy_min_score"

    def test_xau_buy_confidence_down_keyword(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key(
            "XAU BUY 在 score>+0.15 时7战6负, BUY 置信度应大幅下调"
        ) == "score.confidence_threshold"

    def test_threshold_up_down_keywords(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key("BUY 阈值上调 0.05") == "score.buy_min_score"
        assert _extract_param_key("事件强度阈值下调") == "score.event_strength_threshold"

    def test_cross_asset_falsification_keyword(self):
        from weekly_insight_apply import _extract_param_key
        # 单独"同向预期证伪"应映射到 contradiction_check
        assert _extract_param_key(
            "原油黄金同向预期证伪 - 不可同向"
        ) == "filter.contradiction_check.sentiment_vs_market"

    def test_news_driven_filter_keyword(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key(
            "新闻驱动的信号质量过滤应加强"
        ) == "filter.news_quality.partial_skipped"

    def test_actor_winrate_keyword(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key(
            "模型 minimax/MiniMax-M3 在加密 SELL 方向胜率80%, 该方向可适度提高权重"
        ) == "sizing.position_weight"

    def test_sell_close_keyword(self):
        from weekly_insight_apply import _extract_param_key
        assert _extract_param_key("SELL 关闭, 反向 SELL 信号失效") == "score.sell_min_score"

    def test_keyword_table_size_at_least_50(self):
        from weekly_insight_apply import _KEYWORD_TO_PARAM
        assert len(_KEYWORD_TO_PARAM) >= 50, (
            f"扩展后关键词应>=50, 实际 {len(_KEYWORD_TO_PARAM)}"
        )


class TestA2HighSampleBoost:
    def test_effective_confidence_low_n_passes_through(self):
        from spa_apply_worker import _effective_confidence
        assert _effective_confidence(20, 0.40) == 0.40
        assert _effective_confidence(5, 0.40) == 0.40

    def test_effective_confidence_high_n_boosted(self):
        from spa_apply_worker import (
            _effective_confidence, HIGH_SAMPLE_THRESHOLD, HIGH_SAMPLE_BOOST_CONF,
        )
        assert HIGH_SAMPLE_THRESHOLD == 28
        assert HIGH_SAMPLE_BOOST_CONF == 0.65
        assert _effective_confidence(30, 0.40) == 0.65
        assert _effective_confidence(46, 0.23) == 0.65

    def test_effective_confidence_high_n_already_high_unchanged(self):
        from spa_apply_worker import _effective_confidence
        assert _effective_confidence(40, 0.80) == 0.80
        assert _effective_confidence(50, 0.65) == 0.65

    def test_effective_confidence_invalid_input(self):
        from spa_apply_worker import _effective_confidence
        assert _effective_confidence(None, None) == 0.0
        assert _effective_confidence("abc", "def") == 0.0
        assert _effective_confidence(0, 0.0) == 0.0


    def test_apply_spa_to_db_records_boost_tag(self, spa_db):
        from spa_apply_worker import _apply_spa_to_db
        conn = spa_db
        cur = conn.execute("""
            INSERT INTO strategy_param_adjustments
              (asset, action, param_key, direction, magnitude, sample_size,
               avg_confidence, status, applied_setting_change, created_at, updated_at)
            VALUES ('XAU', 'BUY', 'score.confidence_threshold', 'down', 0.05,
                    36, 0.40, 'pending', '', datetime('now'), datetime('now'))
        """)
        spa_id = cur.lastrowid
        conn.commit()
        ok = _apply_spa_to_db(
            conn, spa_id, "score.confidence_threshold", "down", 0.05,
            "XAU", 36, 0.40, effective_conf=0.65,
        )
        assert ok is True
        row = conn.execute(
            "SELECT status, applied_setting_change FROM strategy_param_adjustments WHERE id=?",
            (spa_id,),
        ).fetchone()
        assert row["status"] == "applied"
        assert "[boosted]" in row["applied_setting_change"]
        assert "0.40" in row["applied_setting_change"]
        assert "0.65" in row["applied_setting_change"]

    def test_apply_spa_to_db_no_boost_tag_when_eff_equals_raw(self, spa_db):
        from spa_apply_worker import _apply_spa_to_db
        conn = spa_db
        cur = conn.execute("""
            INSERT INTO strategy_param_adjustments
              (asset, action, param_key, direction, magnitude, sample_size,
               avg_confidence, status, applied_setting_change, created_at, updated_at)
            VALUES ('*', '*', 'score.event_strength_threshold', 'up', 0.07,
                    30, 0.80, 'pending', '', datetime('now'), datetime('now'))
        """)
        spa_id = cur.lastrowid
        conn.commit()
        ok = _apply_spa_to_db(
            conn, spa_id, "score.event_strength_threshold", "up", 0.07,
            "*", 30, 0.80, effective_conf=0.80,
        )
        assert ok is True
        row = conn.execute(
            "SELECT applied_setting_change FROM strategy_param_adjustments WHERE id=?",
            (spa_id,),
        ).fetchone()
        assert "[boosted]" not in row["applied_setting_change"]


class TestA3InsightHitIdsReturn:
    def test_return_ids_false_returns_str(self, spa_db, monkeypatch):
        from ai_prompts import load_recent_insights_block
        import ai_prompts as _ap
        monkeypatch.setattr(_ap, "_connect", lambda: spa_db)
        _ap._INSIGHTS_CACHE.clear()
        result = load_recent_insights_block(target_asset="XAU")
        assert isinstance(result, str)
        assert result == ""

    def test_return_ids_true_returns_tuple(self, spa_db, monkeypatch):
        from ai_prompts import load_recent_insights_block
        import ai_prompts as _ap
        monkeypatch.setattr(_ap, "_connect", lambda: spa_db)
        _ap._INSIGHTS_CACHE.clear()
        result = load_recent_insights_block(target_asset="XAU", return_ids=True)
        assert isinstance(result, tuple)
        assert len(result) == 2
        text, ids = result
        assert isinstance(text, str)
        assert isinstance(ids, list)

    def test_return_ids_with_active_insight(self, spa_db, monkeypatch):
        from ai_prompts import load_recent_insights_block
        conn = spa_db
        conn.execute("""
            INSERT INTO review_insights
              (rule, asset, scope, source_samples, kind, active,
               manual_deactivated, counter_factual, created_at, updated_at)
            VALUES ('XAU BUY 在避险情绪下表现中性, 应维持观望', 'XAU', '',
                    25, 1, 1, 0, '', datetime('now'), datetime('now'))
        """)
        conn.commit()
        # ai_prompts.load_recent_insights_block 用全局 _connect()，
        # 测试需 monkeypatch 让它走 spa_db
        import ai_prompts as _ap
        monkeypatch.setattr(_ap, "_connect", lambda: spa_db)
        # 清缓存避免旧结果
        _ap._INSIGHTS_CACHE.clear()
        text, ids = load_recent_insights_block(target_asset="XAU", return_ids=True)
        assert isinstance(text, str)
        assert len(text) > 0
        assert "XAU" in text
        assert isinstance(ids, list)
        assert len(ids) >= 1
        for i in ids:
            assert isinstance(i, int)

    def test_cache_hit_returns_tuple(self, spa_db, monkeypatch):
        from ai_prompts import load_recent_insights_block
        conn = spa_db
        conn.execute("""
            INSERT INTO review_insights
              (rule, asset, scope, source_samples, kind, active,
               manual_deactivated, counter_factual, created_at, updated_at)
            VALUES ('BTC SELL 快速评测胜率高', 'BTC', '',
                    20, 1, 1, 0, '', datetime('now'), datetime('now'))
        """)
        conn.commit()
        import ai_prompts as _ap
        monkeypatch.setattr(_ap, "_connect", lambda: spa_db)
        _ap._INSIGHTS_CACHE.clear()
        r1 = load_recent_insights_block(target_asset="BTC", return_ids=True)
        r2 = load_recent_insights_block(target_asset="BTC", return_ids=True)
        assert isinstance(r1, tuple) and isinstance(r2, tuple)
        assert r1[1] == r2[1]



class TestIntegration:
    """集成验证: 在真实 trident_event_bus.db 上,三个 P0 修复都已生效。"""

    @pytest.fixture
    def prod_conn(self):
        root = Path(__file__).resolve().parents[1]
        db_path = root / "trident_event_bus.db"
        if not db_path.exists():
            pytest.skip(f"生产 DB 不存在: {db_path}")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def test_applied_spa_count_increased(self, prod_conn):
        n = prod_conn.execute(
            "SELECT COUNT(*) AS n FROM strategy_param_adjustments WHERE status='applied'"
        ).fetchone()["n"]
        assert n >= 7, f"applied SPA 应>=7, 实际 {n}"

    def test_xau_specific_spa_applied(self, prod_conn):
        row = prod_conn.execute(
            "SELECT id, applied_setting_change FROM strategy_param_adjustments "
            "WHERE asset='XAU' AND status='applied' LIMIT 1"
        ).fetchone()
        assert row is not None, "XAU 应至少有 1 条 applied SPA"
        assert "XAU" in row["applied_setting_change"]

    def test_applied_spa_records_boost_tag(self, prod_conn):
        row = prod_conn.execute(
            "SELECT applied_setting_change FROM strategy_param_adjustments "
            "WHERE status='applied' AND applied_setting_change LIKE '%[boosted]%' LIMIT 1"
        ).fetchone()
        assert row is not None, "至少应有 1 条 SPA 标 [boosted]"

    def test_weekly_insight_apply_dry_run_param_recovery(self):
        root = Path(__file__).resolve().parents[1]
        # 子进程需脱离 conftest 的 TRIDENT_DB_PATH 隔离环境
        env = os.environ.copy()
        env.pop("TRIDENT_DB_PATH", None)
        proc = subprocess.run(
            ["python3", "src_python/weekly_insight_apply.py", "--dry-run"],
            cwd=str(root),
            capture_output=True, text=True, timeout=30,
            env=env,
        )
        m = re.search(r"skipped_no_param=(\d+)", proc.stdout)
        assert m, (
            f"无法解析 weekly 输出 (rc={proc.returncode}). "
            f"stdout={proc.stdout[:200]!r} stderr={proc.stderr[:200]!r}"
        )
        n = int(m.group(1))
        assert n < 30, f"A1 后应 < 30, 实际 {n}"

    def test_review_insights_xau_present(self, prod_conn):
        n = prod_conn.execute(
            "SELECT COUNT(*) AS n FROM review_insights "
            "WHERE upper(asset)='XAU' AND active=1 AND kind=1"
        ).fetchone()["n"]
        assert n >= 10, f"XAU 专属 insight 应>=10, 实际 {n}"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
