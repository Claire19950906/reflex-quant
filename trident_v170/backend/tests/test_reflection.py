"""反思系统（engine/reflection.py）回归测试 — 覆盖本轮 P0/P1/P2/P2.3/P3 修复：

  P0  · reflection_worker 命中验证改用 review_agent._evaluate_insights
  P1  · process_once 批量 LLM（按 WIN/LOSS 分组，1-2 次调用替代 5 次）
  P1  · LLM 失败不再直接 'failed'，加 retry_count + last_error + 限速重试（≤3 次）
  P2  · _maybe_emit_insight 三键联合（asset + action + reason_name）+ ESCAPE
  P2  · 触发门槛从 3 提到 5（与 _evaluate_insights 最小样本一致）
  P2.3· 反思 LLM 注入生效经验（_active_insights → prompt）
  P3  · process_once 时间预算（PROCESS_BUDGET_SECONDS），超时塞回重试
  P0  · _insight_hit_validation 已废弃，调用转发到 review_agent._evaluate_insights
"""

import os
import sys
import sqlite3
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import reflection as rfl  # noqa: E402
import review_agent as ra  # noqa: E402
import history_review as hr  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """隔离 SQLite：用 db.migrate() 建完整 schema 的临时 DB，让 reflection / review_agent 走它。"""
    db_path = str(tmp_path / "test_reflections.db")
    import db as _db
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    try:
        # 与生产同路径 db.migrate()，避免手写表清单漂移（曾缺 trade_reflections_v2 /
        # suggestion_param_key 等 → _aggregate_improvements 抛异常 → process_once 标 failed）。
        _db.migrate(fresh)
        fresh.commit()
    finally:
        fresh.close()

    # 把 reflection / db 全部指向 tmp DB
    monkeypatch.setattr("config.DB_PATH", db_path)
    monkeypatch.setattr("engine.reflection.config.DB_PATH", db_path)

    def _conn():
        c = sqlite3.connect(db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(_db, "get_connection", _conn)
    monkeypatch.setattr(rfl, "_open_db", _conn)
    monkeypatch.setattr(ra, "db", _db)

    return db_path


def _seed_ai_decision(conn: sqlite3.Connection, decision_id: int = 1, *,
                      news_id: int = 100, asset: str = "XAU", action: str = "BUY",
                      decision_context: str = '{"status":"ok"}') -> None:
    """插一笔最小可用的 ai_decisions + raw_news 行（reflection._load_trade_dict 需要）。"""
    conn.execute(
        "INSERT OR IGNORE INTO raw_news (id, content, timestamp) VALUES (?, ?, datetime('now'))",
        (news_id, "测试新闻内容" * 5),
    )
    # ai_decisions 无 action 列；asset/action 仅用于 trade_reflections 表
    conn.execute(
        """INSERT OR REPLACE INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, decision_context, reasoning, is_correct)
           VALUES (?, ?, 0.5, 'BUY', ?, ?, 'LOSS')""",
        (decision_id, news_id, decision_context, "测试 reasoning" * 5),
    )
    conn.commit()


def _seed_reflection(conn: sqlite3.Connection, decision_id: int, *,
                     asset: str = "XAU", action: str = "BUY", verdict: str = "LOSS",
                     reason_name: str = "", status: str = "pending",
                     retry_count: int = 0) -> None:
    """插一条 trade_reflections 行（process_once 会按 status 选）。"""
    conn.execute(
        """INSERT OR REPLACE INTO trade_reflections
           (decision_id, news_id, asset, action, verdict, pnl_pct,
            reason_name, status, retry_count)
           VALUES (?, 100, ?, ?, ?, -1.5, ?, ?, ?)""",
        (decision_id, asset, action, verdict, reason_name, status, retry_count),
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# P0: reflection_worker 命中验证改用 review_agent._evaluate_insights
# ─────────────────────────────────────────────────────────────────────────────

def test_insight_hit_validation_delegates_to_evaluate_insights(tmp_db_path):
    """reflection._insight_hit_validation 已废弃，必须转发到 review_agent._evaluate_insights。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 故意种一条"误杀但无样本"的非人工停用经验
        conn.execute(
            """INSERT INTO review_insights (rule, active, kind, manual_deactivated)
               VALUES (?, 0, 1, 0)""",
            ("历史误杀经验",),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        res = rfl._insight_hit_validation(conn)
        assert "rescued_no_sample" in res
        assert any("历史误杀经验" in s for s in res["rescued_no_sample"])
        # 复活后 active 应该是 1
        row = conn.execute(
            "SELECT active FROM review_insights WHERE rule=?", ("历史误杀经验",)
        ).fetchone()
        assert int(row["active"]) == 1
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# P1: process_once 批量 LLM（按 WIN/LOSS 分组，1-2 次调用）
# ─────────────────────────────────────────────────────────────────────────────

def test_process_once_batches_by_verdict(tmp_db_path, monkeypatch):
    """3 笔 LOSS + 2 笔 WIN → _analyze_trades_llm 应被调 2 次（LOSS 一次、WIN 一次）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        for did, asset, action, verdict in [
            (1, "XAU", "BUY", "LOSS"),
            (2, "WTI", "SELL", "LOSS"),
            (3, "BTC", "BUY", "LOSS"),
            (4, "XAU", "BUY", "WIN"),
            (5, "WTI", "SELL", "WIN"),
        ]:
            _seed_ai_decision(conn, did, asset=asset, action=action)
            _seed_reflection(conn, did, asset=asset, action=action, verdict=verdict)
    finally:
        conn.close()

    call_log = []

    def _fake_analyze(trades, framework, names, kind):
        call_log.append((kind, len(trades)))
        return [
            {
                "decision_id": t["decision_id"],
                "reason_code": 7 if kind == "失败" else 5,
                "reason_name": "信号质量差" if kind == "失败" else "趋势成立",
                "reason_text": "题材误判" if kind == "失败" else "趋势成立",
                "confidence": 0.8,
            }
            for t in trades
        ]

    monkeypatch.setattr(rfl.history_review, "_analyze_trades_llm", _fake_analyze)

    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 5
    assert len(call_log) == 2, f"应为 2 次 LLM 调用（LOSS/WIN 各一），实际 {call_log}"
    # 验证分组正确
    kind_map = {k: c for k, c in call_log}
    assert kind_map["失败"] == 3
    assert kind_map["成功"] == 2

    # 验证 5 笔都被标 'done'
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT decision_id, status, reason_name FROM trade_reflections ORDER BY decision_id"
        ).fetchall()
        assert all(r[1] == "done" for r in rows)
        for did, name in [(1, "信号质量差"), (2, "信号质量差"), (3, "信号质量差"),
                          (4, "趋势成立"), (5, "趋势成立")]:
            row = conn.execute(
                "SELECT reason_name FROM trade_reflections WHERE decision_id=?", (did,)
            ).fetchone()
            assert row[0] == name, f"decision={did} expected {name} got {row[0]}"
    finally:
        conn.close()


def test_process_once_no_pending_returns_zero(tmp_db_path, monkeypatch):
    """没有任何 pending 时 process_once 返回 0 且不调 LLM。"""
    monkeypatch.setattr(
        rfl.history_review, "_analyze_trades_llm",
        lambda *a, **k: pytest.fail("不应被调用"),
    )
    import asyncio
    assert rfl.process_once(asyncio.new_event_loop()) == 0


def test_process_once_hold_marks_done_without_llm(tmp_db_path, monkeypatch):
    """HOLD verdict 直接标 'done'，不进 LLM。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, verdict="HOLD")
    finally:
        conn.close()

    called = []
    monkeypatch.setattr(
        rfl.history_review, "_analyze_trades_llm",
        lambda *a, **k: called.append((a, k)) or [],
    )
    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 1
    assert called == []  # 没调 LLM
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM trade_reflections WHERE decision_id=1"
        ).fetchone()
        assert row[0] == "done"
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# P1: LLM 失败重试机制（retry_count + last_error + 限速）
# ─────────────────────────────────────────────────────────────────────────────

def test_process_once_llm_failure_marks_failed_with_retry_count(tmp_db_path, monkeypatch):
    """LLM 抛异常 → 整批标 failed + retry_count=1 + last_error 记录。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, verdict="LOSS")
    finally:
        conn.close()

    def _boom(*a, **k):
        raise RuntimeError("simulated LLM outage")
    monkeypatch.setattr(rfl.history_review, "_analyze_trades_llm", _boom)

    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 1

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT status, retry_count, last_error FROM trade_reflections
               WHERE decision_id=1"""
        ).fetchone()
        assert row["status"] == "failed"
        assert int(row["retry_count"]) == 1
        assert "simulated LLM outage" in row["last_error"]
    finally:
        conn.close()


def test_process_once_retries_only_expired_backoff(tmp_db_path, monkeypatch):
    """retry_count 已达 MAX_RETRY 不再被选；失败 < 1h 不被选；失败超 1h 才被选。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_ai_decision(conn, 2)
        _seed_ai_decision(conn, 3)
        # (1) 已达 MAX_RETRY 上限 — 不应被选
        _seed_reflection(conn, 1, verdict="LOSS", status="failed", retry_count=rfl.MAX_RETRY)
        conn.execute(
            """UPDATE trade_reflections
               SET last_error_at=datetime('now','-2 hours')
               WHERE decision_id=1"""
        )
        # (2) retry_count=1 且失败超 1h — 应被重试
        _seed_reflection(conn, 2, verdict="LOSS", status="failed", retry_count=1)
        conn.execute(
            """UPDATE trade_reflections
               SET last_error_at=datetime('now','-2 hours')
               WHERE decision_id=2"""
        )
        # (3) retry_count=2 但失败 < 1h — 不应被选
        _seed_reflection(conn, 3, verdict="LOSS", status="failed", retry_count=2)
        conn.execute(
            """UPDATE trade_reflections
               SET last_error_at=datetime('now','-30 minutes')
               WHERE decision_id=3"""
        )
        conn.commit()
    finally:
        conn.close()

    seen_ids = []

    def _fake_analyze(trades, framework, names, kind):
        seen_ids.extend(t["decision_id"] for t in trades)
        return [
            {"decision_id": t["decision_id"], "reason_code": 7,
             "reason_name": "信号质量差", "reason_text": "x", "confidence": 0.7}
            for t in trades
        ]
    monkeypatch.setattr(rfl.history_review, "_analyze_trades_llm", _fake_analyze)

    import asyncio
    rfl.process_once(asyncio.new_event_loop())
    # 只 2 应该被重试
    assert 2 in seen_ids
    assert 1 not in seen_ids, "retry_count=MAX_RETRY 不应被选"
    assert 3 not in seen_ids, "1h 内失败不应被重试"


def test_retry_count_capped_at_max(tmp_db_path):
    """_mark_failed 不能让 retry_count 超过 MAX_RETRY。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, status="failed", retry_count=rfl.MAX_RETRY)
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._mark_failed(conn, 1, "test error")
        conn.commit()
        row = conn.execute(
            "SELECT retry_count, last_error FROM trade_reflections WHERE decision_id=1"
        ).fetchone()
        assert int(row["retry_count"]) == rfl.MAX_RETRY, "retry_count 不能超过 MAX_RETRY"
        assert row["last_error"] == "test error"
    finally:
        conn.close()


def test_successful_reflection_resets_retry_state(tmp_db_path, monkeypatch):
    """成功归因后 retry_count 应清零、last_error 清空。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, verdict="LOSS", status="failed", retry_count=2)
        conn.execute(
            """UPDATE trade_reflections
               SET last_error='prior error', last_error_at=datetime('now','-2 hours')
               WHERE decision_id=1"""
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        rfl.history_review, "_analyze_trades_llm",
        lambda trades, fw, names, kind: [
            {"decision_id": t["decision_id"], "reason_code": 7,
             "reason_name": "信号质量差", "reason_text": "ok", "confidence": 0.9}
            for t in trades
        ],
    )
    import asyncio
    rfl.process_once(asyncio.new_event_loop())

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT status, retry_count, last_error, last_error_at
               FROM trade_reflections WHERE decision_id=1"""
        ).fetchone()
        assert row["status"] == "done"
        assert int(row["retry_count"]) == 0
        assert row["last_error"] == ""
        assert row["last_error_at"] is None
    finally:
        conn.close()


def test_max_retry_constant_sane():
    """防止误改重试上限（默认 3，配合 1h 限速）。"""
    assert 1 <= rfl.MAX_RETRY <= 5
    assert rfl.RETRY_BACKOFF_SECONDS >= 60


# ─────────────────────────────────────────────────────────────────────────────
# P2: _maybe_emit_insight 三键联合 + ESCAPE + 门槛 5
# ─────────────────────────────────────────────────────────────────────────────

def test_emit_insight_requires_three_key_match(tmp_db_path):
    """触发必须满足 asset+action+reason_name 三键联合（不能跨品种）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 6 笔 XAU BUY 失败（"信号质量差"）— 足够触发
        for did in range(1, 7):
            _seed_ai_decision(conn, did, asset="XAU", action="BUY")
            _seed_reflection(conn, did, asset="XAU", action="BUY", verdict="LOSS",
                             reason_name="信号质量差", status="done")
        # 5 笔 WTI BUY 失败（"信号质量差"）— 同样满足门槛，但不应影响 XAU 规则
        for did in range(7, 12):
            _seed_ai_decision(conn, did, asset="WTI", action="BUY")
            _seed_reflection(conn, did, asset="WTI", action="BUY", verdict="LOSS",
                             reason_name="信号质量差", status="done")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 触发一次 XAU BUY 反思
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "信号质量差", "题材误判")
        conn.commit()
        rules = [r["rule"] for r in conn.execute(
            "SELECT rule FROM review_insights WHERE active=1"
        ).fetchall()]
        xau_rules = [r for r in rules if r.startswith("XAU")]
        wti_rules = [r for r in rules if r.startswith("WTI")]
        assert len(xau_rules) >= 1, f"XAU BUY 应有规则，实际 {rules}"
        assert len(wti_rules) == 0, f"WTI 不该有规则（XAU 触发时），实际 {rules}"
    finally:
        conn.close()


def test_emit_insight_threshold_is_five(tmp_db_path):
    """门槛是 5，不是 3（旧值）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 4 笔同类失败 — 不应触发（门槛 5）
        for did in range(1, 5):
            _seed_ai_decision(conn, did, asset="XAU", action="BUY")
            _seed_reflection(conn, did, asset="XAU", action="BUY", verdict="LOSS",
                             reason_name="信号质量差", status="done")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "信号质量差", "题材误判")
        conn.commit()
        # 4 笔不足以触发
        assert conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # 第 5 笔加入后触发
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 5, asset="XAU", action="BUY")
        _seed_reflection(conn, 5, asset="XAU", action="BUY", verdict="LOSS",
                         reason_name="信号质量差", status="done")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "信号质量差", "题材误判")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_emit_insight_escape_prevents_wildcard_match(tmp_db_path):
    """ESCAPE 让 reason_name 中的 % / _ 当字面字符处理（防通配符漏出）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 已有一条"经验 A 100% 命中"的经验（rule 含 %）
        conn.execute(
            """INSERT INTO review_insights (rule, asset, active)
               VALUES (?, ?, 1)""",
            ("XAU BUY单亏损主因[经验 A 100% 命中]：…", "XAU"),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        for did in range(1, 6):
            _seed_ai_decision(conn, did, asset="XAU", action="BUY")
            _seed_reflection(conn, did, asset="XAU", action="BUY", verdict="LOSS",
                             reason_name="经验 A 100% 命中", status="done")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "经验 A 100% 命中", "...")
        conn.commit()
        # 没有重复创建（去重生效）；% 被当作字面字符
        cnt = conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE rule LIKE '%主因[经验 A 100% 命中]%'"
        ).fetchone()[0]
        assert cnt == 1, f"% 漏出成通配符导致不期望的匹配，实际 cnt={cnt}"
    finally:
        conn.close()


def test_emit_insight_empty_reason_name_noop(tmp_db_path):
    """reason_name='' 时 _maybe_emit_insight 必须早退（防空字段生成空规则）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "", "")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM review_insights"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_emit_insight_origin_is_reflection(tmp_db_path):
    """沉淀的经验 origin='reflection'（区别于 review / rule / quick_sim）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        for did in range(1, 6):
            _seed_ai_decision(conn, did, asset="XAU", action="BUY")
            _seed_reflection(conn, did, asset="XAU", action="BUY", verdict="LOSS",
                             reason_name="信号质量差", status="done")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rfl._maybe_emit_insight(conn, "XAU", "BUY", "信号质量差", "题材误判")
        conn.commit()
        row = conn.execute(
            "SELECT origin FROM review_insights WHERE rule LIKE '%信号质量差%'"
        ).fetchone()
        assert row["origin"] == "reflection"
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# P2.3: 反思 LLM 注入生效经验（关闭"反思→沉淀→新反思回查"闭环）
# ─────────────────────────────────────────────────────────────────────────────

def test_load_trade_dict_attaches_active_insights(tmp_db_path):
    """_load_trade_dict 必须把生效经验挂到 trade dict['_active_insights']（供 _analyze_trades_llm 注入 prompt）。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 种一条 source_samples=10 的生效经验（达到 _REVIEW_INSIGHTS_MIN_SAMPLES 门槛）
        conn.execute(
            """INSERT INTO review_insights
               (rule, asset, active, kind, source_samples, origin)
               VALUES (?, ?, 1, 1, 10, 'seeded')""",
            ("XAU BUY 看到美联储加息不要追多", "XAU"),
        )
        _seed_ai_decision(conn, 1, asset="XAU")
        _seed_reflection(conn, 1, asset="XAU", verdict="LOSS")
        row = conn.execute(
            "SELECT * FROM trade_reflections WHERE decision_id=1"
        ).fetchone()
        trade = rfl._load_trade_dict(conn, row)
        assert "_active_insights" in trade
        assert isinstance(trade["_active_insights"], list)
        assert any("XAU" in str(x.get("rule") or "") for x in trade["_active_insights"])
        # 至少包含 id 和 rule 字段
        first = trade["_active_insights"][0]
        assert "id" in first
        assert "rule" in first
    finally:
        conn.close()


def test_load_trade_dict_handles_missing_insights_gracefully(tmp_db_path, monkeypatch):
    """load_active_insight_items 抛错时不阻断反思主流程（降级返回空列表）。"""
    def _boom(*a, **k):
        raise RuntimeError("simulated DB error")
    monkeypatch.setattr(rfl.review_agent, "load_active_insight_items", _boom)

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1)
        row = conn.execute(
            "SELECT * FROM trade_reflections WHERE decision_id=1"
        ).fetchone()
        trade = rfl._load_trade_dict(conn, row)  # 不应抛
        assert trade["_active_insights"] == []
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# P3: process_once 时间预算（防止 LLM 慢响应阻塞 worker 30s 节奏）
# ─────────────────────────────────────────────────────────────────────────────

def test_process_budget_constant_sane():
    """PROCESS_BUDGET_SECONDS 应 < worker sleep 间隔（30s），且 >0。"""
    assert 0 < rfl.PROCESS_BUDGET_SECONDS < 30


def test_process_once_budget_skips_llm_when_db_load_exceeds(tmp_db_path, monkeypatch):
    """DB 加载阶段已超预算 → 整批塞回重试队列（status=failed），不调 LLM。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, verdict="LOSS")
        _seed_ai_decision(conn, 2)
        _seed_reflection(conn, 2, verdict="LOSS")
    finally:
        conn.close()

    def _slow_load(conn, row):
        time.sleep(rfl.PROCESS_BUDGET_SECONDS + 0.1)  # 模拟 DB 慢查询
        return {"decision_id": int(row["decision_id"]), "asset": "XAU", "action": "BUY",
                "is_correct": "LOSS", "pnl_pct": 0.0, "content": "", "core_impact": "",
                "decision_context": "{}", "_active_insights": []}

    monkeypatch.setattr(rfl, "_load_trade_dict", _slow_load)
    monkeypatch.setattr(
        rfl.history_review, "_analyze_trades_llm",
        lambda *a, **k: pytest.fail("LLM 不应在预算耗尽时被调用"),
    )

    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 2

    # 验证：2 笔都被标 failed（不 done），且 retry_count=1，last_error 包含预算提示
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT decision_id, status, retry_count, last_error FROM trade_reflections ORDER BY decision_id"
        ).fetchall()
        for r in rows:
            assert r["status"] == "failed", f"决策 {r['decision_id']} 应标 failed，实际 {r['status']}"
            assert r["retry_count"] == 1
            assert "process_budget_exceeded" in (r["last_error"] or "")
    finally:
        conn.close()


def test_process_once_budget_skips_win_group_after_loss_finished(tmp_db_path, monkeypatch):
    """损失组 LLM 后预算耗尽 → 成功组不进 LLM，塞回重试。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_ai_decision(conn, 1)
        _seed_reflection(conn, 1, verdict="LOSS")
        _seed_ai_decision(conn, 2)
        _seed_reflection(conn, 2, verdict="WIN")
    finally:
        conn.close()

    def _slow_loss_llm(trades, framework, names, kind):
        # 损失组耗尽预算
        time.sleep(rfl.PROCESS_BUDGET_SECONDS + 0.1)
        return [{"decision_id": t["decision_id"], "reason_code": 7, "reason_name": "信号质量差",
                 "reason_text": "x", "confidence": 0.5} for t in trades]

    monkeypatch.setattr(rfl.history_review, "_analyze_trades_llm", _slow_loss_llm)

    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 2

    # 验证：LOSS 笔被标 done（因为损失组 LLM 正常返回了），WIN 笔被塞回 failed
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        loss_row = conn.execute(
            "SELECT status, reason_name FROM trade_reflections WHERE decision_id=1"
        ).fetchone()
        assert loss_row["status"] == "done"
        assert loss_row["reason_name"] == "信号质量差"

        win_row = conn.execute(
            "SELECT status, retry_count, last_error FROM trade_reflections WHERE decision_id=2"
        ).fetchone()
        assert win_row["status"] == "failed"
        assert win_row["retry_count"] == 1
        assert "process_budget_exceeded_before_win_llm" in (win_row["last_error"] or "")
    finally:
        conn.close()


def test_process_once_budget_does_not_break_normal_fast_path(tmp_db_path, monkeypatch):
    """正常快路径：DB 快 + LLM 快 → 5 笔全部 done，不影响业务。"""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        for did, verdict in [(1, "LOSS"), (2, "LOSS"), (3, "WIN"), (4, "WIN"), (5, "LOSS")]:
            _seed_ai_decision(conn, did)
            _seed_reflection(conn, did, verdict=verdict)
    finally:
        conn.close()

    def _fast_llm(trades, framework, names, kind):
        return [{"decision_id": t["decision_id"], "reason_code": 7 if kind == "失败" else 5,
                 "reason_name": "x", "reason_text": "y", "confidence": 0.5} for t in trades]

    monkeypatch.setattr(rfl.history_review, "_analyze_trades_llm", _fast_llm)

    import asyncio
    n = rfl.process_once(asyncio.new_event_loop())
    assert n == 5

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        done = conn.execute("SELECT COUNT(*) FROM trade_reflections WHERE status='done'").fetchone()[0]
        assert done == 5
    finally:
        conn.close()