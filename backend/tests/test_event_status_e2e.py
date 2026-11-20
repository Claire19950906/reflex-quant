# -*- coding: utf-8 -*-
"""v321：真实 SQL 端到端验证 —— 标记进 DB → row→event → 状态 → 前端文案。

⚠ 重要：tests/conftest.py 会把 TRIDENT_DB_PATH 强制指向**临时隔离库**
（防止测试污染生产库 / 撞运行中 engine 的写锁）。所以本测试**不能**连生产库，
而是在隔离库里造真实场景跑完整链路。

覆盖的真实事故：
  1. fix9 —— _EVENT_SELECT 没 SELECT gate_unavailable → 冻结行误显示「待 AI 分析」
  2. 噪声行 is_noise=1 → 必须 SKIPPED
  3. 正常待分析行 → 必须 PENDING（不能被误判）
"""
import os
import sqlite3
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import api_server
import event_status


def _isolated_db_path():
    """复用 conftest 设好的隔离库（保证不碰生产数据）。"""
    p = os.environ.get("TRIDENT_DB_PATH")
    assert p, "conftest 应已设置 TRIDENT_DB_PATH 隔离库"
    return p


def _make_db():
    """建带**完整 schema** 的隔离库（用项目自己的 db.migrate，别手搓简化表）。

    手搓简化表会漏掉 _EVENT_SELECT 里的几十个 ad.* 列（paper_trading_run_id 等），
    报 "no such column" —— 那是测试的锅，不是产品的锅。
    """
    path = _isolated_db_path()
    import db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        db.migrate(conn)
    except Exception:
        # migrate 幂等失败（表已存在）可忽略，关键看列齐不齐
        pass
    conn.commit()
    return conn


def _seed(conn):
    """造四类行：噪声 / 门卫冻结 / 正常待分析 / 已完成 / 失败。"""
    conn.execute("DELETE FROM raw_news")
    conn.execute("DELETE FROM ai_decisions")
    rows = [
        (1, "PENDING", 1, 0),   # 噪声
        (2, "PENDING", 1, 1),   # 噪声 + 冻结（噪声优先）
        (3, "PENDING", 0, 1),   # 门卫冻结 ← 核心场景
        (4, "PENDING", 0, 0),   # 正常待分析
        (5, "DONE", 0, 0),      # 已完成
        (6, "FAILED", 0, 0),    # 失败
        (7, "PENDING", 0, 2),   # 降级放行（未判定但放行）—— 不能挂待回放
    ]
    for (rid, st, noise, gu) in rows:
        conn.execute(
            "INSERT INTO raw_news (id, timestamp, published_at, source, content, "
            "status, is_noise, gate_unavailable, relevance_score, "
            "content_fingerprint, first_seen_ts, cross_net_first_seen_ts, "
            "cross_net_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?)",
            (rid, "2026-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00",
             "s", "c%d" % rid, st, noise, gu,
             "fp%d" % rid, "2026-01-01T00:00:00+08:00",
             "2026-01-01T00:00:00+08:00", "s"))
    conn.commit()
    return rows


def _run_event_select(conn):
    """跑 _EVENT_SELECT。注意 id 被 alias 成 news_id（见 _EVENT_SELECT 首行）。"""
    sql = ("%s WHERE rn.is_noise = 0 AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL) "
           "ORDER BY rn.id" % api_server._EVENT_SELECT)
    return conn.execute(sql).fetchall()


class TestEventSelectEndToEnd:
    def test_event_select_includes_flag_columns(self):
        """fix9 回归：补的列必须真的出现在结果集里。"""
        conn = _make_db()
        try:
            _seed(conn)
            rows = _run_event_select(conn)
            assert rows, "应有可查询的行"
            keys = rows[0].keys()
            assert "gate_unavailable" in keys, \
                "gate_unavailable 未被 SELECT —— 冻结行会误显示「待 AI 分析」"
            assert "is_noise" in keys, "is_noise 未被 SELECT"
        finally:
            conn.close()

    def test_frozen_row_becomes_gate_pending(self):
        """核心回归：冻结行经 row→event 必须是 GATE_PENDING，不是 PENDING。"""
        conn = _make_db()
        try:
            _seed(conn)
            rows = {r["news_id"]: r for r in _run_event_select(conn)}
            frozen = rows[3]
            assert frozen["gate_unavailable"] == 1
            ev = api_server._row_to_event(frozen)
            assert ev["analysis_status"] == event_status.STATUS_GATE_PENDING, \
                "冻结行状态错误: %s" % ev["analysis_status"]
            assert ev["analysis_status"] != event_status.STATUS_PENDING
        finally:
            conn.close()

    def test_normal_pending_stays_pending(self):
        """正常待分析行仍是 PENDING（不能被误判成冻结/跳过）。"""
        conn = _make_db()
        try:
            _seed(conn)
            rows = {r["news_id"]: r for r in _run_event_select(conn)}
            ev = api_server._row_to_event(rows[4])
            assert ev["analysis_status"] == event_status.STATUS_PENDING
        finally:
            conn.close()

    def test_noise_row_is_excluded_from_event_list(self):
        """噪声行**不进事件列表**（SQL 的 WHERE rn.is_noise = 0 就滤掉了）。

        这比「进列表但标 SKIPPED」更干净 —— 噪声根本不该出现在待分析列表里。
        注意：即使它被过滤，_row_to_event 仍必须能正确处理 is_noise=1
        （别的路径会喂进来），所以下面单独断言状态推导。
        """
        conn = _make_db()
        try:
            _seed(conn)
            rows = {r["news_id"]: r for r in _run_event_select(conn)}
            assert 1 not in rows, "噪声行不应出现在事件列表"
            assert 2 not in rows, "噪声+冻结行也不应出现在事件列表"
            # 过滤只发生在 SQL 层；状态推导层仍要正确
            st = event_status.resolve_analysis_status(
                "PENDING", has_decision=False, is_noise=True, gate_pending=False)
            assert st == event_status.STATUS_SKIPPED
        finally:
            conn.close()

    def test_failed_row_stays_failed(self):
        """失败行 → FAILED（不因新逻辑被改写）。"""
        conn = _make_db()
        try:
            _seed(conn)
            rows = {r["news_id"]: r for r in _run_event_select(conn)}
            ev = api_server._row_to_event(rows[6])
            assert ev["analysis_status"] == event_status.STATUS_FAILED
        finally:
            conn.close()

    def test_degraded_passthrough_not_gate_pending(self):
        """=2 是降级放行（门卫未判定但明确放行），主分析照常认领。

        回归：修复前用 truthy 判定，=2 也被标成 GATE_PENDING「门卫待回放」，
        导致一批本该进主分析的行挂满待回放徽章、队列看着全卡住。
        """
        conn = _make_db()
        try:
            _seed(conn)
            rows = {r["news_id"]: r for r in _run_event_select(conn)}
            degraded = rows[7]
            assert degraded["gate_unavailable"] == 2
            ev = api_server._row_to_event(degraded)
            assert ev["analysis_status"] != event_status.STATUS_GATE_PENDING, \
                "降级放行行被误标门卫待回放: %s" % ev["analysis_status"]
            assert ev["analysis_status"] == event_status.STATUS_PENDING
        finally:
            conn.close()


class TestFrontendLabelMapping:
    """前端徽章文案必须能区分三种状态（否则用户还是分不清）。"""

    def test_three_states_have_distinct_labels(self):
        labels = {
            event_status.STATUS_SKIPPED:
                event_status.STATUS_LABELS[event_status.STATUS_SKIPPED],
            event_status.STATUS_GATE_PENDING:
                event_status.STATUS_LABELS[event_status.STATUS_GATE_PENDING],
            event_status.STATUS_PENDING:
                event_status.STATUS_LABELS[event_status.STATUS_PENDING],
        }
        assert len(set(labels.values())) == 3, labels
