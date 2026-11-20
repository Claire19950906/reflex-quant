#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v505：提醒账本的行为测试（alert_ledger 从 0% 起步）。

这块回答的是"我推给你的提醒，事后到底对不对"。它有三条不能松的口径：
  ① 认不出资产/方向就不许猜（返回 None / "none"），否则结算会拿别的品种算账；
  ② 结算不了的记 unknown，绝不拿未来数据或邻近代价凑；
  ③ Fed 提醒是"多资产方向"，转鹰→风险资产应跌、转鸽→应涨，单独一条口径。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alert_ledger as al


# ── 认品种：认得准，认不出就别猜 ───────────────────────────────
@pytest.mark.parametrize("title,expect", [
    ("BTC 反弹 6%", "BTC"),
    ("btc 突破前高", "BTC"),
    ("ETH 转鸽信号", "ETH"),
    ("WTI 库存超预期", "WTI"),
    ("XAU 冲高回落", "XAU"),
])
def test_detect_asset_known_symbols(title, expect):
    assert al.detect_asset(title) == expect


@pytest.mark.parametrize("title", ["美联储降息预期", "某公司财报", "", None])
def test_detect_asset_returns_none_when_unknown(title):
    assert al.detect_asset(title) is None


def test_detect_asset_does_not_match_substrings_of_other_words():
    # "SOLO" 不应命中 SOL（\b 边界）；否则会把不相干的标题记成 SOL
    assert al.detect_asset("SOLO 乐队演出") is None


# ── 认方向：冲突就是不认 ───────────────────────────────────────
@pytest.mark.parametrize("title", ["反弹", "买入信号", "转鸽", "降息", "↑", "利多", "做多"])
def test_detect_direction_up(title):
    assert al.detect_direction(title) == "up"


@pytest.mark.parametrize("title", ["回落", "卖出", "转鹰", "加息", "↓", "利空", "做空"])
def test_detect_direction_down(title):
    assert al.detect_direction(title) == "down"


def test_detect_direction_conflict_is_none():
    # 同时出现涨/跌关键词 → 不认方向（这正是 Fed 提醒的典型形态）
    assert al.detect_direction("降息 ↑ 但转鹰 ↓") == "none"
    assert al.detect_direction("美联储 转鸽 转鹰") == "none"


def test_detect_direction_no_keywords_is_none():
    assert al.detect_direction("某公司发布财报") == "none"


# ── 记账 ──────────────────────────────────────────────────────
@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "LEDGER_DB", str(tmp_path / "ledger.db"), raising=False)
    monkeypatch.setattr(al, "_price_at", lambda ts, sym, tolerance_sec=900: 100.0, raising=False)
    return al


def test_record_writes_row_with_ref_price(ledger):
    rid = ledger.record("generic", "BTC 反弹 6%", now_ts=1000)
    assert rid == 1
    conn = ledger._ledger_conn()
    try:
        row = conn.execute("SELECT asset, direction, ref_price, settle_note FROM alert_ledger").fetchone()
    finally:
        conn.close()
    assert row[0] == "BTC" and row[1] == "up" and row[2] == 100.0
    assert row[3] is None


def test_record_marks_missing_price_or_asset(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_price_at", lambda *a, **k: None, raising=False)
    ledger.record("generic", "某公司财报", now_ts=1000)
    conn = ledger._ledger_conn()
    try:
        note = conn.execute("SELECT settle_note FROM alert_ledger").fetchone()[0]
    finally:
        conn.close()
    assert note == "no_price_or_asset"


def test_record_fed_hawkish_is_down_on_proxy(ledger, monkeypatch):
    monkeypatch.setenv("ALERT_LEDGER_FED_PROXY", "BTC")
    ledger.record("fed", "美联储转鹰 ↑↓", now_ts=1000)
    conn = ledger._ledger_conn()
    try:
        row = conn.execute("SELECT asset, direction FROM alert_ledger").fetchone()
    finally:
        conn.close()
    assert row == ("BTC", "down")


def test_record_fed_dovish_is_up(ledger):
    ledger.record("fed", "美联储转鸽 ↑↓", now_ts=1000)
    conn = ledger._ledger_conn()
    try:
        row = conn.execute("SELECT direction FROM alert_ledger").fetchone()
    finally:
        conn.close()
    assert row[0] == "up"


def test_record_never_raises_on_broken_db(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DB", "/nonexistent/dir/x.db", raising=False)
    assert ledger.record("generic", "BTC 反弹", now_ts=1000) is None


# ── 结算 ──────────────────────────────────────────────────────
def _insert(conn, **kw):
    base = dict(kind="generic", title="t", asset="BTC", direction="up", push_ts=0,
                ref_price=100.0, horizon_min=240, hit=None, settle_note=None)
    base.update(kw)
    conn.execute(
        "INSERT INTO alert_ledger(%s) VALUES(%s)" % (
            ",".join(base), ",".join("?" * len(base))),
        tuple(base.values()))
    conn.commit()


def test_settle_hit_and_miss(ledger, monkeypatch):
    conn = ledger._ledger_conn()
    try:
        _insert(conn, asset="BTC", direction="up", push_ts=1, ref_price=100.0)
        _insert(conn, asset="ETH", direction="down", push_ts=1, ref_price=100.0)
    finally:
        conn.close()

    # 出口价：BTC 涨到 110（up 命中），ETH 涨到 110（down 打脸）
    prices = {"BTC": 110.0, "ETH": 110.0}
    monkeypatch.setattr(ledger, "_price_at", lambda ts, sym, tolerance_sec=900: prices.get(sym), raising=False)
    stats = ledger.settle()
    assert stats["checked"] == 2 and stats["hit"] == 1 and stats["miss"] == 1

    conn = ledger._ledger_conn()
    try:
        rows = conn.execute("SELECT asset, move_pct, hit FROM alert_ledger ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows[0][1] == pytest.approx(10.0) and rows[0][2] == 1
    assert rows[1][1] == pytest.approx(10.0) and rows[1][2] == 0


def test_settle_unknown_when_no_direction(ledger, monkeypatch):
    conn = ledger._ledger_conn()
    try:
        _insert(conn, direction="none", push_ts=1, ref_price=100.0)
    finally:
        conn.close()
    stats = ledger.settle()
    assert stats["unknown"] == 1 and stats["hit"] == 0


def test_settle_unknown_when_no_exit_price(ledger, monkeypatch):
    conn = ledger._ledger_conn()
    try:
        _insert(conn, push_ts=1, ref_price=100.0)
    finally:
        conn.close()
    monkeypatch.setattr(ledger, "_price_at", lambda *a, **k: None, raising=False)
    stats = ledger.settle()
    assert stats["unknown"] == 1
    conn = ledger._ledger_conn()
    try:
        note = conn.execute("SELECT settle_note FROM alert_ledger").fetchone()[0]
    finally:
        conn.close()
    assert note == "no_exit_price"


def test_settle_ignores_rows_not_yet_due(ledger, monkeypatch):
    import time as _t
    conn = ledger._ledger_conn()
    try:
        _insert(conn, push_ts=int(_t.time()), horizon_min=240)   # 还没到期
    finally:
        conn.close()
    assert ledger.settle()["checked"] == 0


def test_settle_is_idempotent(ledger, monkeypatch):
    conn = ledger._ledger_conn()
    try:
        _insert(conn, push_ts=1, ref_price=100.0)
    finally:
        conn.close()
    monkeypatch.setattr(ledger, "_price_at", lambda *a, **k: 110.0, raising=False)
    ledger.settle()
    stats2 = ledger.settle()          # 已结算的不再重复计入
    assert stats2["checked"] == 0


# ── 报告 ──────────────────────────────────────────────────────
def test_report_prints_hit_rate(ledger, capsys):
    import time as _t
    now = int(_t.time())
    conn = ledger._ledger_conn()
    try:
        _insert(conn, push_ts=now - 60, hit=1)
        _insert(conn, push_ts=now - 60, hit=0, asset="ETH")
    finally:
        conn.close()
    ledger.report(days=30)
    out = capsys.readouterr().out
    assert "50" in out        # 1 命中 / 2 条 = 50%
