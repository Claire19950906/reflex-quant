#!/usr/bin/env python3
"""v396：kline 聚合 cron 的三条收敛（读量 / 硬超时 / 可审计耗时）。

背景：2026-11-16 实测该 cron 以 85% CPU 连跑 17 分钟，期间 bridge 启动被写锁
卡了 6 分钟起不来。这里锁住三件事，防止再退化：
1. 同一批 tick 只读一次，1h/4h/1d 共用（不再三倍搬运）；
2. 整轮有硬上限（SIGALRM），到点必退，已 commit 的批次保留；
3. 每轮耗时写进 kline_aggregate.log，次日可查。
"""
from __future__ import annotations

import os
import sqlite3
import time
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import cron_kline_aggregate as cka


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL, "
        "UNIQUE(symbol, ts))"
    )
    conn.execute("CREATE INDEX idx_market_ticks_symbol_ts ON market_ticks(symbol, ts)")
    return conn


def _seed(conn, symbol="WTI", base=1794800000):
    # 两个 1h 桶：第一个桶 10→12→11，第二个桶 11→13→9
    prices = [
        (base + 0, 10.0), (base + 60, 12.0), (base + 120, 11.0),
        (base + 3600, 11.0), (base + 3660, 13.0), (base + 3720, 9.0),
    ]
    for ts, p in prices:
        conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (symbol, ts, p))


def test_aggregate_rows_ohlc_semantics():
    conn = _conn()
    _seed(conn)
    ticks = cka.fetch_ticks(conn, "WTI", 1794799000)
    out = cka.aggregate_rows(ticks, 3600)
    base = 1794800000
    b0 = (base // 3600) * 3600
    b1 = b0 + 3600
    assert list(out[b0]) == [10.0, 12.0, 10.0, 11.0, 3], out[b0]
    assert list(out[b1]) == [11.0, 13.0, 9.0, 9.0, 3], out[b1]


def test_aggregate_ticks_matches_rows_parity():
    """老接口（每周期各查一次）与新接口（读一次复用）必须逐桶一致。"""
    conn = _conn()
    _seed(conn)
    for tf_sec in (3600, 14400, 86400):
        old = cka.aggregate_ticks(conn, "WTI", tf_sec, 1794799000)
        new = cka.aggregate_rows(cka.fetch_ticks(conn, "WTI", 1794799000), tf_sec)
        assert old == new


def test_fetch_ticks_filters_nonpositive_and_window():
    conn = _conn()
    conn.execute("INSERT INTO market_ticks VALUES ('WTI', 100, 0.0)")
    conn.execute("INSERT INTO market_ticks VALUES ('WTI', 101, -1.0)")
    conn.execute("INSERT INTO market_ticks VALUES ('WTI', 102, 88.5)")
    assert cka.fetch_ticks(conn, "WTI", 100) == [(102, 88.5)]


def test_hard_timeout_exits_subprocess():
    """硬超时必须真的把进程掐掉（不是靠循环里的软检查）。"""
    code = (
        "import sys, time;"
        f"sys.path.insert(0, {str(SRC)!r});"
        "import cron_kline_aggregate as cka;"
        "cka._setup_hard_timeout(1);"
        "print('sleeping', flush=True);"
        "time.sleep(30)"
    )
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=20)
    elapsed = time.time() - t0
    assert "HARD-TIMEOUT" in (proc.stdout + proc.stderr), (proc.stdout, proc.stderr)
    assert proc.returncode == 0
    # 必须真的被掐断：远早于 sleep(30)
    assert elapsed < 5, elapsed


def test_perf_log_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cka._log_perf("unit-test line")
    log = tmp_path / ".trident" / "log" / "kline_aggregate.log"
    assert log.exists()
    assert "unit-test line" in log.read_text(encoding="utf-8")
