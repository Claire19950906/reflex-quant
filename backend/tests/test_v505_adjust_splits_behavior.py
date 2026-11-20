#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v505：拆股复权的行为测试（adjust_splits 从 0% 起步）。

这块算错，历史价和回测会一起错——库里那 3 次真拆股（NVDA 4:1、NVDA 10:1、
TSLA 3:1）以前就被当成暴跌写进了曲线，出现 -75% / -90% / -67%。
所以必须钉住三条：
  ① 只缩放拆股日**之前**的行，拆股日之后一律不动；
  ② 已复权过的不能二次缩放（幂等），否则历史价会被再砍一刀；
  ③ dry-run 一行都不许写库。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import adjust_splits as adj

DAY = "2024-06-09"          # 用库里真实存在过的那次 NVDA 10:1 当样本日
CUT = None                  # 在 fixture 里算


def _ts(day):
    return adj.ts_of(day)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "bus.db"
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, open REAL, high REAL,
                                 low REAL, close REAL, volume INTEGER, source TEXT);
        CREATE TABLE data_quality_flags(ts INTEGER, source TEXT, asset TEXT,
                                        indicator TEXT, rule TEXT, note TEXT);
    """)
    c.close()
    monkeypatch.setattr(adj, "DB_PATH", str(path), raising=False)
    monkeypatch.setattr(adj, "BACKEND", str(tmp_path), raising=False)   # 备份文件别写进仓库
    monkeypatch.setattr(adj, "EVENTS", [("NVDA", DAY, 10)], raising=False)
    return path


def _insert(path, asset, ts, close, source="yahoo"):
    c = sqlite3.connect(str(path))
    c.execute("INSERT INTO ohlcv_daily VALUES(?,?,?,?,?,?,?,?)",
              (asset, ts, close, close, close, close, 1000, source))
    c.commit()
    c.close()


def _prices(path, asset="NVDA"):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT ts, close FROM ohlcv_daily WHERE asset=? ORDER BY ts",
                         (asset,)).fetchall()
    finally:
        c.close()


# ── 时间锚点 ─────────────────────────────────────────────────
def test_ts_of_is_utc_midnight():
    import datetime as dt
    assert _ts("2024-06-09") == int(
        dt.datetime(2024, 6, 9, tzinfo=dt.timezone.utc).timestamp())


# ── dry-run：一行都不写 ──────────────────────────────────────
def test_dry_run_does_not_touch_prices(db, capsys):
    cut = _ts(DAY)
    _insert(db, "NVDA", cut - 86400, 1200.0)
    _insert(db, "NVDA", cut + 86400, 120.0)
    assert adj.main.__name__ == "main"
    import sys as _s
    argv_bak = _s.argv
    _s.argv = ["adjust_splits.py", "--dry-run"]
    try:
        assert adj.main() == 0
    finally:
        _s.argv = argv_bak
    assert [p for _t, p in _prices(db)] == [1200.0, 120.0]      # 没变
    out = capsys.readouterr().out
    assert "dry-run" in out


# ── apply：只动拆股日之前 ────────────────────────────────────
def test_apply_scales_only_prices_before_split(db):
    cut = _ts(DAY)
    _insert(db, "NVDA", cut - 2 * 86400, 1100.0)
    _insert(db, "NVDA", cut - 86400, 1200.0)
    _insert(db, "NVDA", cut + 86400, 120.0)
    _insert(db, "NVDA", cut + 2 * 86400, 121.0)

    import sys as _s
    argv_bak = _s.argv
    _s.argv = ["adjust_splits.py", "--apply"]
    try:
        assert adj.main() == 0
    finally:
        _s.argv = argv_bak

    got = dict(_prices(db))
    assert got[cut - 2 * 86400] == pytest.approx(110.0)    # 1100 / 10
    assert got[cut - 86400] == pytest.approx(120.0)        # 1200 / 10
    assert got[cut + 86400] == pytest.approx(120.0)        # 之后不动
    assert got[cut + 2 * 86400] == pytest.approx(121.0)


def test_apply_writes_flag_and_backup(db, tmp_path):
    cut = _ts(DAY)
    _insert(db, "NVDA", cut - 86400, 1200.0)
    _insert(db, "NVDA", cut + 86400, 120.0)

    import sys as _s
    argv_bak = _s.argv
    _s.argv = ["adjust_splits.py", "--apply"]
    try:
        adj.main()
    finally:
        _s.argv = argv_bak

    c = sqlite3.connect(str(db))
    try:
        flag = c.execute("SELECT rule, note FROM data_quality_flags").fetchone()
    finally:
        c.close()
    assert flag[0] == "split_adjusted" and "10:1" in flag[1]

    backups = list(Path(tmp_path).glob("ohlcv_daily_split_backup_*.jsonl"))
    assert backups, "apply 必须先备份原始行"
    assert backups[0].read_text(encoding="utf-8").count("\n") >= 1


# ── 幂等：已复权的不许再砍一刀 ────────────────────────────────
def test_rerun_does_not_scale_twice(db):
    cut = _ts(DAY)
    _insert(db, "NVDA", cut - 86400, 1200.0)
    _insert(db, "NVDA", cut + 86400, 120.0)

    import sys as _s
    argv_bak = _s.argv
    try:
        for _ in range(2):
            _s.argv = ["adjust_splits.py", "--apply"]
            adj.main()
    finally:
        _s.argv = argv_bak

    got = dict(_prices(db))
    assert got[cut - 86400] == pytest.approx(120.0)        # 仍是 120，不是 12
    c = sqlite3.connect(str(db))
    try:
        n = c.execute("SELECT COUNT(*) FROM data_quality_flags").fetchone()[0]
    finally:
        c.close()
    assert n == 1, "同一个拆股只能记一条复权标记"


# ── 边界：库里没有该品种不该炸 ────────────────────────────────
def test_apply_with_empty_table_does_not_crash(db):
    import sys as _s
    argv_bak = _s.argv
    _s.argv = ["adjust_splits.py", "--apply"]
    try:
        assert adj.main() == 0
    finally:
        _s.argv = argv_bak


def test_has_flags_table_detects_presence(db):
    c = sqlite3.connect(str(db))
    try:
        assert adj._has_flags_table(c) is True
    finally:
        c.close()

    c2 = sqlite3.connect(":memory:")
    try:
        assert adj._has_flags_table(c2) is False
    finally:
        c2.close()
