# -*- coding: utf-8 -*-
"""v406 P4：行情断流时的本地兜底（market_snapshot._fetch_local_recent）。

背景（2026-11-16 实测）：XAU 87 条、WTI 97 条决策被判 `missing:feed_stale`，
而库里 `kline_bars` 的 XAU 1m 有 41,647 根、最新一根只 1.1 小时前 ——
本地明明有新鲜数据，只是 okx_swap 分支拉不到 OKX 就直标 `unavailable`。
WTI 更彻底：`knowledge_base.fetch_klines` 的 WTI 分支只走 Yahoo CL=F，
本地兜底只给 1d 挂了 FRED。

这里锁四条纪律（它们决定了这套兜底是"接线"还是"造假"）：
1. 只读真实存在的 bar，一根都没有就返回 None。
2. 超过该周期允许年龄就不用（宁可标 N/A，不给旧价格）。
3. 未来时间（时钟/时区异常）同样不用。
4. 返回升序 ms 时间戳，与交易所口径一致（下游 _compute_period_stats 依赖它）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import db as dbmod
import market_snapshot as ms


class _FakeConn:
    def __init__(self, bars=None, intra=None):
        self.bars = bars or []
        self.intra = intra or []
        self.closed = False

    def execute(self, sql, params=()):
        class _Cur:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        if "FROM kline_bars" in sql:
            return _Cur(self.bars)
        return _Cur(self.intra)

    def close(self):
        self.closed = True


def _patch(monkeypatch, rows=None, intra=None):
    conn = _FakeConn(rows, intra)
    monkeypatch.setattr(dbmod, "get_connection", lambda: conn)
    return conn


def _row(age_s, close=100.0, ms=True):
    ts = time.time() - age_s
    ts = ts * 1000.0 if ms else ts
    return (ts, close, close + 1, close - 1, close, 10.0)


def test_fresh_local_used(monkeypatch):
    _patch(monkeypatch, rows=[_row(600, 3888.0), _row(4200, 3880.0)])
    out = ms._fetch_local_recent("XAU", "1h", 96)
    assert out is not None and len(out) == 2
    # 升序（最新的在最后），且时间戳是毫秒
    assert out[-1][0] > out[0][0]
    assert out[-1][0] > 1e11
    assert out[-1][4] == 3888.0


def test_stale_local_refused(monkeypatch):
    # 1m 允许 2h；这里给一根 4.3h 前的（实测 WTI 1m 就是这个状态）
    _patch(monkeypatch, rows=[_row(4.3 * 3600)])
    assert ms._fetch_local_recent("WTI", "1m", 96) is None


def test_stale_limit_is_per_interval(monkeypatch):
    # 同一根 4.3h 前的 bar：1m 不许用，4h（允许 7 天）可以用
    rows = [_row(4.3 * 3600)]
    _patch(monkeypatch, rows=rows)
    assert ms._fetch_local_recent("WTI", "1m", 96) is None
    assert ms._fetch_local_recent("WTI", "4h", 96) is not None


def test_future_timestamp_refused(monkeypatch):
    _patch(monkeypatch, rows=[_row(-7200)])
    assert ms._fetch_local_recent("XAU", "1h", 96) is None


def test_no_rows_returns_none(monkeypatch):
    _patch(monkeypatch, rows=[], intra=[])
    assert ms._fetch_local_recent("XAU", "1h", 96) is None


def test_falls_back_to_intraday_and_normalises_seconds(monkeypatch):
    # kline_bars 空 → 用 ohlcv_intraday，且秒级时间戳要归一成毫秒
    _patch(monkeypatch, rows=[], intra=[_row(300, 3869.0, ms=False)])
    out = ms._fetch_local_recent("XAU", "1h", 96)
    assert out is not None and len(out) == 1
    assert out[0][0] > 1e11
