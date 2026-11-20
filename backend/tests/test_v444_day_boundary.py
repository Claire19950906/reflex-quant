"""v444：当日预算口径回归测试。

背景（2026-11-17 实测）：
`strftime('%s','now','start of day','localtime')` 里的 'start of day' 是先在 **UTC** 上取零点，
再把结果当本地时间转成 epoch → 实际边界落在**本地 08:00**（跨天后甚至落在昨天 16:00）。
后果：门卫/主分析的"当日已用"被低估 —— 同一时刻两种口径相差 4,400 次（1,535 vs 5,968）。
预算闸门以为还有额度 → 一路打到全站预算烧干，新新闻再拿不到门卫判定。
"""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"


def _sqlite_scalar(sql: str) -> str:
    """用内存库跑一条不依赖业务表的 SQL（只需要日期函数）。"""
    conn = sqlite3.connect(":memory:")
    try:
        return str(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def test_new_boundary_is_local_midnight():
    """新写法必须落在本地 00:00:00。"""
    got = _sqlite_scalar(
        "SELECT datetime(strftime('%s','now','localtime','start of day','utc'),"
        "'unixepoch','localtime')"
    )
    assert got.endswith("00:00:00"), got


def test_old_boundary_was_wrong():
    """旧写法不等于本地零点（偏移随时段而变，实测曾落在本地 16:00）。"""
    old = _sqlite_scalar(
        "SELECT datetime(strftime('%s','now','start of day','localtime'),"
        "'unixepoch','localtime')"
    )
    new = _sqlite_scalar(
        "SELECT datetime(strftime('%s','now','localtime','start of day','utc'),"
        "'unixepoch','localtime')"
    )
    if old == new:
        pytest.skip("本机时区偏移为 0，两种写法恰好等价")
    assert not old.endswith("00:00:00"), "旧写法不该落在本地零点：%s" % old
    assert new.endswith("00:00:00")


@pytest.mark.parametrize("fname", ["realtime_filter.py", "engine/ai_worker.py"])
def test_no_legacy_day_boundary_in_source(fname):
    """预算闸门里不能再出现旧的 epoch 口径。"""
    text = (SRC / fname).read_text(encoding="utf-8")
    # 只检查真正进 SQL 的字符串（注释/docstring 里提到历史写法不算违规）
    sql_uses = re.findall(
        r"[\"'](?:\s*(?:AND\s+)?ts>=)?strftime\('%s','now','start of day','localtime'\)[\"']",
        text,
    )
    assert not sql_uses, "%s 仍有旧日界 SQL %d 处" % (fname, len(sql_uses))


def test_gate_budget_used_today_uses_new_boundary():
    """门卫预算读数函数必须用新口径（源码级断言，避免表不存在时静默返回 0）。"""
    text = (SRC / "realtime_filter.py").read_text(encoding="utf-8")
    assert "localtime','start of day','utc'" in text
