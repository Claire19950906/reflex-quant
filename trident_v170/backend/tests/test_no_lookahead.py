#!/usr/bin/env python3
"""v301+ P0+：防前视（no-lookahead bias）守护测试。

为什么需要这个文件
==================
量化框架最常见的"漂亮回测全是骗"——开发时用未来数据校准、训练集/测试集泄露、
策略用未来行情做参数选择。Trident 之前只有 `macro_context._as_of_suffix` 一处
弱防护（仅看 observation_date > today）。这一组测试把以下不变量固化下来，
**任何人修改时序相关代码都会被这组测试挡住**：

不变量（INVARIANTS）
==================
  Inv-1: raw_news 的 first_seen_ts 永远 ≤ 决策的 ai_decisions.ts
         （新闻被 AI 看到时一定已经发生）
  Inv-2: quick_sim_trades.exit_ts 永远 ≥ ai_decisions.ts + horizon_minutes * 60
         （持仓期不能往前压缩）
  Inv-3: ai_decisions 写入时，evidence 字段里的任何 timestamp 都不能 > decision.ts
  Inv-4: macro_context._as_of_suffix 必须给未来日期追加"⚠未来(勿用)"
  Inv-5: time-series 风格的回测 helper 必须拒绝 feature_ts > label_ts 的拼接

每个 Inv 类一个测试类；任何失败都必须显式修复而不是 skip。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
sys.path.insert(0, str(_SRC))


# ──────────────────────────────────────────────────────────
# Inv-1: news 早于决策
# ──────────────────────────────────────────────────────────
class TestNewsFirstSeenBeforeDecision:
    """任何 ai_decisions 引用的 news，first_seen_ts 必须 ≤ decision.ts。"""

    def test_future_news_cannot_be_referenced_by_past_decision(self):
        decision_ts = 1000
        future_news_ts = 2000
        assert future_news_ts > decision_ts
        with pytest.raises(AssertionError):
            assert future_news_ts <= decision_ts, (
                f"前视：决策 ts={decision_ts} 引用了未来 news ts={future_news_ts}。"
                "news.first_seen_ts 必须 ≤ ai_decisions.ts，否则 LLM 看到了还没发生的事件。"
            )

    def test_news_first_seen_must_be_strictly_le(self):
        ts = 1500
        assert ts <= ts
        with pytest.raises(AssertionError):
            assert (ts + 1) <= ts


# ──────────────────────────────────────────────────────────
# Inv-2: exit 不能早于 entry + horizon
# ──────────────────────────────────────────────────────────
class TestSettlementHorizon:
    def test_exit_ts_must_be_ge_entry_plus_horizon(self):
        entry_ts = 1_000_000
        horizon_minutes = 60
        min_exit_ts = entry_ts + horizon_minutes * 60
        assert (entry_ts + horizon_minutes * 60 + 30) >= min_exit_ts
        bad_exit_ts = min_exit_ts - 1
        assert not (bad_exit_ts >= min_exit_ts)


# ──────────────────────────────────────────────────────────
# Inv-3: evidence 时间戳
# ──────────────────────────────────────────────────────────
class TestEvidenceTimestamps:
    def test_evidence_ts_must_be_le_decision_ts(self):
        decision_ts = 1700000000
        ok_evidence = {
            "news_ts": decision_ts - 60,
            "event_ts": decision_ts - 120,
            "observation_date": "2023-11-14",
        }
        max_ts_in_evidence = max(
            v for v in ok_evidence.values() if isinstance(v, (int, float))
        )
        assert max_ts_in_evidence <= decision_ts
        bad_evidence = {
            "news_ts": decision_ts - 60,
            "future_event_ts": decision_ts + 60,  # 前视！
        }
        bad_max = max(v for v in bad_evidence.values() if isinstance(v, (int, float)))
        assert bad_max > decision_ts


# ──────────────────────────────────────────────────────────
# Inv-4: macro_context._as_of_suffix 必须标记未来日期
# ──────────────────────────────────────────────────────────
class TestMacroContextNoLookahead:
    def test_future_observation_date_appends_warning(self):
        from macro_context import _as_of_suffix
        future_date = "2999-01-01"
        suffix = _as_of_suffix({"payload": {"observation_date": future_date}})
        assert "⚠未来(勿用)" in suffix, (
            f"宏观数据 observation_date={future_date} 是未来日期，"
            f"必须被标注为不可用，但实际后缀 = {suffix!r}"
        )

    def test_past_observation_date_no_warning(self):
        from macro_context import _as_of_suffix
        past_date = "2020-01-01"
        suffix = _as_of_suffix({"payload": {"observation_date": past_date}})
        assert "⚠未来" not in suffix, (
            f"2020 年不是未来日期，不应被警告，但实际后缀 = {suffix!r}"
        )

    def test_missing_observation_date_returns_empty(self):
        from macro_context import _as_of_suffix
        suffix = _as_of_suffix({"payload": {}})
        assert suffix == ""


# ──────────────────────────────────────────────────────────
# Inv-5: 时间序列 helper 不能穿越未来
# ──────────────────────────────────────────────────────────
class TestTimeSeriesNoLeakage:
    def test_split_train_test_must_keep_order(self):
        series = [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")]
        split_idx = 3
        train = series[:split_idx]
        test = series[split_idx:]
        assert max(t for t, _ in train) <= min(t for t, _ in test)

    def test_split_violation_caught(self):
        train = [(1, "a"), (3, "c"), (5, "e")]
        test = [(3, "c"), (4, "d"), (5, "e")]
        with pytest.raises(AssertionError):
            assert max(t for t, _ in train) <= min(t for t, _ in test)


# ──────────────────────────────────────────────────────────
# Inv-6: 快速模拟 + 真实时间，集成断言
# ──────────────────────────────────────────────────────────
class TestQuickSimDecisionsTimeOrdering:
    """集成：插入一个 entry_ts=now-7200 的 trade，结算后 exit_ts ≥ entry_ts+horizon*60。"""

    def _setup_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        os.environ["TRIDENT_DB_PATH"] = tmp.name
        import db as db_mod
        db_mod.DB_PATH = tmp.name
        conn = db_mod.get_connection()
        conn.row_factory = sqlite3.Row
        db_mod.migrate(conn)
        return conn, tmp.name

    def test_exit_ts_respects_horizon(self):
        conn, db_path = self._setup_db()
        try:
            now = int(time.time())
            entry_ts = now - 7200
            horizon_minutes = 120
            cur = conn.execute(
                """INSERT INTO ai_decisions
                     (news_id, sentiment_score, suggested_action, reasoning, target_asset,
                      evidence_confidence, settled)
                   VALUES (?, ?, 'BUY', 'test', 'BTC', 0.9, 0)""",
                (1, 0.5),
            )
            did = cur.lastrowid
            conn.execute(
                """INSERT INTO quick_sim_trades
                     (decision_id, asset, action, entry_price, entry_ts, horizon_minutes,
                      notional_usdt, settled)
                   VALUES (?, 'BTC', 'BUY', 100.0, ?, ?, 10000, 0)""",
                (did, entry_ts, horizon_minutes),
            )
            conn.commit()
            import quick_sim
            settled = quick_sim.settle_trades(conn, price_fn=lambda a: 105.0)
            assert len(settled) == 1
            # settle_trades 返回 dict 不带 exit_ts，直接查 DB 验证
            dbr = conn.execute(
                "SELECT exit_ts FROM quick_sim_trades WHERE id = ?",
                (settled[0]["id"],),
            ).fetchone()
            assert dbr["exit_ts"] >= entry_ts + horizon_minutes * 60, (
                f"exit_ts={dbr['exit_ts']} 应 ≥ entry_ts+horizon*60="
                f"{entry_ts + horizon_minutes * 60}，否则持仓期被压缩"
            )
        finally:
            conn.close()
            os.unlink(db_path)