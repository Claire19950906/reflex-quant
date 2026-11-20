#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v500：推送闸门的行为测试（alert_gate 从 0% 覆盖起步）。

为什么补这一块：README 敢写"别被自己吵死"，靠的就是这个闸门。
之前它一条测试都没有 —— 改一行去重逻辑，没有任何东西会红。

覆盖的四道闸：指纹窗口（跨重启） / 夜间静默 / 日额上限 / 品类节流，
外加"闸门自己坏了要放行"这条兜底。
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alert_gate as ag
import config


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """每个用例一个空库，避免用例之间互相污染指纹。"""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gate.db"), raising=False)
    monkeypatch.setattr(ag, "DAILY_CAP", 6, raising=False)
    # 时钟必须钉死：这套用例跑在凌晨就是"夜间静默"，白天才是正常放行。
    # 真实教训：全量套件在北京时间 04:5x 跑过一次，5 条用例全红，
    # 原因只是"现在几点"，不是逻辑错。
    monkeypatch.setattr(ag, "_hhmm_now", lambda: "12:00")
    ag._KIND_GAP_CACHE.clear()
    return ag


# ── 归一化：数字变了仍是同一件事 ────────────────────────────────
def test_normalize_strips_numbers_and_punctuation():
    a = ag.normalize("[周期反转] ETH 反弹 66% ｜ 0 天")
    b = ag.normalize("[周期反转] ETH 反弹 70% ｜ 1 天")
    assert a == b
    assert "66" not in a and "%" not in a


def test_fingerprint_stable_across_price_changes(gate):
    assert gate.fingerprint("reversal", "BTC 反弹 5%") == gate.fingerprint("reversal", "BTC 反弹 9%")


def test_fingerprint_differs_across_kinds(gate):
    assert gate.fingerprint("reversal", "BTC 反弹") != gate.fingerprint("fed", "BTC 反弹")


def test_infer_kind_from_title(gate):
    assert gate.infer_kind("[周期反转] BTC") == "reversal"
    assert gate.infer_kind("美联储降息预期升温") == "fed"
    assert gate.infer_kind("FOMC 点阵图转向") == "fed"
    assert gate.infer_kind("强买入 ETH") == "strong_buy"
    assert gate.infer_kind("普通提醒") == "generic"


# ── 窗口去重：同一条 12 小时内只发一次 ─────────────────────────
def test_same_alert_twice_is_deduped(gate):
    ok1, why1 = gate.allow("reversal", "[周期反转] BTC 反弹 66%")
    assert ok1 and why1 == "ok"
    ok2, why2 = gate.allow("reversal", "[周期反转] BTC 反弹 70%")
    assert not ok2
    assert why2.startswith("dup")


def test_dup_hits_accumulate(gate):
    gate.allow("reversal", "周期反转 BTC 66%")
    gate.allow("reversal", "周期反转 BTC 70%")
    gate.allow("reversal", "周期反转 BTC 71%")
    conn = gate._conn()
    try:
        row = conn.execute("SELECT hits FROM alert_gate_log").fetchone()
    finally:
        conn.close()
    assert row[0] == 2      # 两次被挡，hits 累加


# ── 品类节流：同类别的不同消息，密集时只发第一条 ────────────────
def test_rate_limit_blocks_second_of_same_kind(gate):
    assert gate.allow("generic", "第一条普通提醒")[0] is True
    ok, why = gate.allow("generic", "第二条完全不同的普通提醒")
    assert not ok and why.startswith("rate")


def test_other_kind_not_blocked_by_rate(gate):
    assert gate.allow("generic", "第一条普通提醒")[0] is True
    # 不同品类走各自的 min_gap；reversal 的 600s 未到但 fed 从未发过
    assert gate.allow("fed", "美联储降息预期升温")[0] is True


# ── 日额上限 ───────────────────────────────────────────────────
def test_daily_cap_blocks_sixth_alert(gate, monkeypatch):
    monkeypatch.setattr(gate, "DAILY_CAP", 1, raising=False)
    assert gate.allow("reversal", "周期反转 A")[0] is True
    # 换品类绕过 min_gap，但绕不过日额
    ok, why = gate.allow("fed", "美联储 B")
    assert not ok and why.startswith("cap")


# ── 夜间静默 ───────────────────────────────────────────────────
def test_quiet_hours_block_low_priority(gate, monkeypatch):
    monkeypatch.setattr(gate, "_hhmm_now", lambda: "03:20")
    ok, why = gate.allow("generic", "普通提醒")
    assert not ok and why.startswith("quiet")


def test_quiet_hours_let_high_priority_through(gate, monkeypatch):
    monkeypatch.setattr(gate, "_hhmm_now", lambda: "03:20")
    assert gate.allow("reversal", "🔴 周期反转 BTC")[0] is True


def test_quiet_window_crosses_midnight(gate):
    assert gate._in_quiet("23:30") is True
    assert gate._in_quiet("06:30") is True
    assert gate._in_quiet("12:00") is False


def test_is_high_markers(gate):
    assert gate._is_high("🔴 急跌") is True
    assert gate._is_high("强买入 ETH") is True
    assert gate._is_high("普通提醒") is False


# ── 兜底：闸门自己坏了不能把推送全堵死 ────────────────────────────
def test_gate_failure_fails_open(gate, monkeypatch):
    def boom():
        raise sqlite3_error()
    monkeypatch.setattr(gate, "_conn", boom)
    ok, why = gate.allow("generic", "普通提醒")
    assert ok is True and why == "gate_error"


def sqlite3_error():
    import sqlite3
    return sqlite3.OperationalError("simulated")


def test_dry_run_flag(monkeypatch):
    monkeypatch.setenv("ALERTS_DRY_RUN", "1")
    assert ag.dry_run() is True
    monkeypatch.setenv("ALERTS_DRY_RUN", "0")
    assert ag.dry_run() is False
