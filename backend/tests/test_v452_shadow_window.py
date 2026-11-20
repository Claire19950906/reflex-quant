# -*- coding: utf-8 -*-
"""v452：影子账本「入账窗口」回归 —— 锁住 created_at 时间窗的旧病。

背景：v451 里 `tech_shadow_signals` 长期 0 行，日志每小时
`{"added": 0, "skipped": 2}`。根因不是规则太严，而是 sync 用
`created_at >= now-1h` 取候选：created_at 是整点批量写入的（全库只有几个值），
信号落库时刻与调度一错开就整轮空转，被拒候选下一轮还要再算一遍。

本测试锁四件事：
  1. created_at 很旧、但尚未评估过的信号必须能被捞到；
  2. 同一候选只评估一次（重复跑不再烧 CPU），入账幂等；
  3. 被拒的候选也写进评估台账，并带结论；
  4. 评估台账为空时只回看 BOOTSTRAP_DAYS，不把全史扫一遍。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import cron_tech_shadow as cts
import tech_signal_bridge as tsb


DAY = 86400


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tech_signals (asset TEXT, ts INTEGER, close REAL, "
        "trigger_type TEXT, score REAL, pct_1d REAL, signal_json TEXT, "
        "uses_future INTEGER, created_at INTEGER)")
    return conn


def _sig(conn, asset: str, ts: int, score: float = 0.8,
         created_at: int | None = None, kind: str = "rsi_oversold") -> None:
    conn.execute(
        "INSERT INTO tech_signals VALUES (?,?,?,?,?,?,?,?,?)",
        (asset, int(ts), 100.0, "support", score, 1.0,
         json.dumps([{"k": kind, "w": 1.2, "v": 30}]), 0,
         int(created_at if created_at is not None else ts)))
    conn.commit()


def _patch(monkeypatch, actionable: bool = True) -> None:
    monkeypatch.setattr(tsb, "load_actionable",
                        lambda: {"bull": [], "bear": [], "allow_all": actionable})
    monkeypatch.setattr(tsb, "resolve_horizon",
                        lambda items, allow, horizons=None, cls=None, vol=None:
                        5 if actionable else None)
    monkeypatch.setattr(tsb, "decide_direction",
                        lambda items, pct, res=None, allow=None: ("BUY", "test", 0.8))
    monkeypatch.setattr(tsb, "regime_ok", lambda *a, **k: True)
    monkeypatch.setattr(tsb, "multi_tf_resonance", lambda conn, asset: None)


def test_old_created_at_signal_is_picked_up(monkeypatch):
    """created_at 在 120 天前，但 K 线在 20 天前 —— 旧窗口必然漏掉，新窗口必须捞到。"""
    conn = _db()
    now = int(time.time())
    _sig(conn, "BTC", now - 20 * DAY, created_at=now - 120 * DAY)
    _patch(monkeypatch)
    cts._ensure_schema(conn)

    counts = cts.sync(conn, 0, 0.58)
    assert counts["added"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM tech_shadow_signals").fetchone()[0] == 1


def test_second_run_does_not_re_evaluate(monkeypatch):
    conn = _db()
    now = int(time.time())
    _sig(conn, "BTC", now - 5 * DAY)
    _patch(monkeypatch)
    cts._ensure_schema(conn)

    assert cts.sync(conn, 0, 0.58)["added"] == 1
    counts = cts.sync(conn, 0, 0.58)
    assert counts["added"] == 0
    assert sum(counts.values()) == 0, "已评估过的候选不该再进评估队列"
    assert conn.execute(
        "SELECT COUNT(*) FROM tech_shadow_eval").fetchone()[0] == 1


def test_rejected_candidate_is_recorded(monkeypatch):
    conn = _db()
    now = int(time.time())
    _sig(conn, "ETH", now - 3 * DAY, kind="div_bottom")
    _patch(monkeypatch, actionable=False)
    cts._ensure_schema(conn)

    counts = cts.sync(conn, 0, 0.58)
    assert counts["not_actionable"] == 1
    assert counts["added"] == 0
    row = conn.execute(
        "SELECT verdict FROM tech_shadow_eval WHERE asset='ETH'").fetchone()
    assert row[0] == "not_actionable"
    # 第二轮不能再看见它
    assert sum(cts.sync(conn, 0, 0.58).values()) == 0


def test_online_window_is_bounded(monkeypatch):
    """线上固定回看 ONLINE_DAYS：老信号不扫，显式回填才覆盖历史。"""
    conn = _db()
    now = int(time.time())
    _sig(conn, "OLD", now - 90 * DAY)      # 超出 ONLINE_DAYS，线上不看
    _sig(conn, "NEW", now - 2 * DAY)       # 线上应当抓到
    _patch(monkeypatch)
    cts._ensure_schema(conn)

    assert cts.sync(conn, 0, 0.58)["added"] == 1
    # 显式回填才覆盖历史
    assert cts.sync(conn, 90, 0.58)["added"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM tech_shadow_signals").fetchone()[0] == 2


def test_below_min_score_is_not_evaluated(monkeypatch):
    conn = _db()
    now = int(time.time())
    _sig(conn, "SOL", now - 2 * DAY, score=0.45)
    _patch(monkeypatch)
    cts._ensure_schema(conn)

    assert sum(cts.sync(conn, 0, 0.58).values()) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tech_shadow_eval").fetchone()[0] == 0
