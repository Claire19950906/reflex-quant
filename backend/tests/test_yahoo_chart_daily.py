"""yahoo_chart_daily（Yahoo chart API 补日线）单测。

2026-11-20: 覆盖率 0%。这模块看着是"网络脚本"，其实净是**对齐逻辑**——
而它注释里写的那句就是对过 bug 的结论：Yahoo 的 quote 数组里缺失点是 null，
必须**按索引对齐后成对丢弃**，不能先 zip 再过滤，否则之后所有行都错位一格。
那正是"K 线错一天、回测全歪"的典型来源。所以这里测的就是对齐。
"""

import sqlite3
import sys
import types

import pytest

import yahoo_chart_daily as ycd


def _ts_days(n, start=1700000000):
    # 每天一根
    return [start + i * 86400 for i in range(n)]


# ── to_rows：索引对齐 / 非法行 / 同日去重 ─────────────────────────────────

def test_to_rows_aligns_by_index_and_drops_nulls():
    ts = _ts_days(4)
    quote = {
        "open":  [10.0, None, 12.0, 13.0],
        "high":  [11.0, None, 13.0, 14.0],
        "low":   [9.0,  None, 11.0, 12.0],
        "close": [10.5, None, 12.5, 13.5],
        "volume": [100, None, 300, 400],
    }
    rows = ycd.to_rows("000660.KS", ts, quote)
    # 第 0 天保留、第 1 天整根丢、第 2/3 天必须还是各自那天的价（不能错位成 12.0/13.0 对应错日）
    assert len(rows) == 3
    assert [r[3] for r in rows] == [10.0, 12.0, 13.0]
    assert [r[0] for r in rows] == ["000660.KS"] * 3
    assert [r[1] for r in rows] == ["1d"] * 3


def test_to_rows_ts_is_milliseconds():
    ts = [1700000000]
    quote = {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]}
    rows = ycd.to_rows("X", ts, quote)
    # kline_bars 的 ts 是**毫秒**，库里所有 K 线同一个口径
    assert rows[0][2] == 1700000000 * 1000


def test_to_rows_skips_bad_prices():
    ts = _ts_days(4)
    quote = {
        "open":  [0.0, 10.0, 10.0, 10.0],     # 0 价 → 丢
        "high":  [1.0, 9.0, 12.0, 12.0],      # high<low → 丢
        "low":   [1.0, 11.0, 9.0, 9.0],
        "close": [1.0, 10.0, 10.0, 10.0],
        "volume": [1, 1, 1, 1],
    }
    rows = ycd.to_rows("X", ts, quote)
    assert len(rows) == 2
    assert [r[3] for r in rows] == [10.0, 10.0]


def test_to_rows_keeps_first_bar_of_same_day():
    # 夏令时切换会有同日两根，只留第一根，避免一天两根把 OHLC 聚合算歪
    same = 1700000000
    ts = [same, same + 3600, same + 86400]
    quote = {
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
        "close": [1.0, 2.0, 3.0], "volume": [1, 2, 3],
    }
    rows = ycd.to_rows("X", ts, quote)
    assert len(rows) == 2
    assert rows[0][3] == 1.0


def test_to_rows_missing_volume_array_defaults_zero():
    ts = _ts_days(2)
    quote = {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0]}
    rows = ycd.to_rows("X", ts, quote)
    assert [r[7] for r in rows] == [0, 0]


def test_to_rows_short_arrays_do_not_crash():
    # quote 数组比 ts 短 → 多出来的 ts 直接跳过，不能抛 IndexError 打断整批
    ts = _ts_days(3)
    quote = {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}
    assert len(ycd.to_rows("X", ts, quote)) == 1


def test_to_rows_sorted_by_ts():
    ts = [1700000000 + 2 * 86400, 1700000000, 1700000000 + 86400]
    quote = {
        "open": [3.0, 1.0, 2.0], "high": [3.0, 1.0, 2.0], "low": [3.0, 1.0, 2.0],
        "close": [3.0, 1.0, 2.0], "volume": [1, 1, 1],
    }
    rows = ycd.to_rows("X", ts, quote)
    assert [r[2] for r in rows] == sorted(r[2] for r in rows)


# ── write_kline：落库与锁重试 ─────────────────────────────────────────────

def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER, "
        "open REAL, high REAL, low REAL, close REAL, volume INTEGER, "
        "fetched_at INTEGER, PRIMARY KEY (symbol, timeframe, ts))"
    )
    return conn


def test_write_kline_inserts_and_upserts():
    conn = _mem_db()
    ts = _ts_days(2)
    quote = {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
             "close": [1.0, 2.0], "volume": [10, 20]}
    rows = ycd.to_rows("X", ts, quote)
    assert ycd.write_kline(conn, rows) == 2
    # 同一天再来一次（价格变了）→ 覆盖，不新增
    quote2 = {"open": [1.5, 2.5], "high": [1.5, 2.5], "low": [1.5, 2.5],
              "close": [1.5, 2.5], "volume": [11, 21]}
    rows2 = ycd.to_rows("X", ts, quote2)
    ycd.write_kline(conn, rows2)
    assert conn.execute("SELECT COUNT(*) FROM kline_bars").fetchone()[0] == 2
    assert conn.execute("SELECT close FROM kline_bars ORDER BY ts").fetchone()[0] == 1.5


def test_write_kline_retries_when_locked(monkeypatch):
    class LockedOnce:
        def __init__(self):
            self.calls = 0

        def executemany(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            class Cur:
                rowcount = 1
            return Cur()

        def commit(self):
            pass

    monkeypatch.setattr(ycd.time, "sleep", lambda *_: None)
    assert ycd.write_kline(LockedOnce(), [("X", "1d", 1, 1, 1, 1, 1, 1, 0)]) == 1


def test_write_kline_reraises_non_lock_errors():
    class Boom:
        def executemany(self, *a, **k):
            raise sqlite3.OperationalError("no such table: kline_bars")

        def commit(self):
            pass

    with pytest.raises(sqlite3.OperationalError):
        ycd.write_kline(Boom(), [("X", "1d", 1, 1, 1, 1, 1, 1, 0)])


# ── refresh_ohlcv：同日只留最新一根 ───────────────────────────────────────

def _patch_build_rows(monkeypatch, rows):
    mod = types.ModuleType("backfill_ohlcv_local")
    mod.build_rows = lambda conn: (rows, {})
    monkeypatch.setitem(sys.modules, "backfill_ohlcv_local", mod)


def _mem_ohlcv_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, open REAL, high REAL, "
        "low REAL, close REAL, volume REAL, source TEXT, PRIMARY KEY (asset, ts))"
    )
    return conn


def test_refresh_ohlcv_dedupes_same_day_and_drops_stale(monkeypatch):
    # 用整日边界做基准，否则 +3600/+7200 会跨到第二天（本用例曾被这个坑绊过一次）
    day = 1700006400          # 2023-11-15T00:00:00Z
    later_ts = day + 3600
    # build_rows 给出同日两根（不同源）→ 应只留 ts 更大的那根
    rows = [
        ("X", day, 1.0, 1.0, 1.0, 1.0, 5, "yahoo"),
        ("X", later_ts, 2.0, 2.0, 2.0, 2.0, 6, "yahoo"),
        ("Y", day, 3.0, 3.0, 3.0, 3.0, 7, "yahoo"),
    ]
    _patch_build_rows(monkeypatch, rows)
    conn = _mem_ohlcv_db()
    # 先塞一根同日的旧行（ts 与规范的都不同），应被清掉
    conn.execute("INSERT INTO ohlcv_daily VALUES ('X', ?, 9,9,9,9,9,'old')", (day + 7200,))

    counts = ycd.refresh_ohlcv(conn, {"X", "Y"})
    assert counts == {"X": 1, "Y": 1}
    xs = conn.execute("SELECT ts, close FROM ohlcv_daily WHERE asset='X'").fetchall()
    assert len(xs) == 1 and xs[0][0] == later_ts and xs[0][1] == 2.0


def test_refresh_ohlcv_ignores_other_symbols(monkeypatch):
    _patch_build_rows(monkeypatch, [("Z", 1700000000, 1, 1, 1, 1, 1, "y")])
    conn = _mem_ohlcv_db()
    assert ycd.refresh_ohlcv(conn, {"X"}) == {"X": 0}


# ── fetch / main：网络层用桩，不打真网 ─────────────────────────────────────

class _Resp:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(**kw):
    import json
    block = {"timestamp": [1700000000], "indicators": {"quote": [{"open": [1], "high": [2],
             "low": [0.5], "close": [1.5], "volume": [10]}]}}
    block.update(kw)
    return json.dumps({"chart": {"result": [block]}}).encode()


def test_fetch_returns_ts_and_quote(monkeypatch):
    monkeypatch.setattr(ycd, "urlopen", lambda req, timeout=0: _Resp(_payload()))
    ts, quote = ycd.fetch("000660.KS")
    assert ts == [1700000000]
    assert quote["close"] == [1.5]


def test_fetch_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(b"")            # 空响应 = 被限流
        return _Resp(_payload())

    monkeypatch.setattr(ycd, "urlopen", flaky)
    monkeypatch.setattr(ycd.time, "sleep", lambda *_: None)
    ts, _ = ycd.fetch("X", tries=3)
    assert ts == [1700000000] and calls["n"] == 2


def test_fetch_raises_after_all_retries(monkeypatch):
    monkeypatch.setattr(ycd, "urlopen", lambda req, timeout=0: _Resp(b""))
    monkeypatch.setattr(ycd.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        ycd.fetch("X", tries=2)
    assert "0 字节" in str(exc.value) or "空响应" in str(exc.value)


def test_fetch_empty_result_raises(monkeypatch):
    import json
    monkeypatch.setattr(ycd, "urlopen",
                        lambda req, timeout=0: _Resp(json.dumps({"chart": {"result": []}}).encode()))
    monkeypatch.setattr(ycd.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        ycd.fetch("X", tries=1)


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(ycd.sys, "argv", ["yahoo_chart_daily.py", "--symbols", "X,Y"])
    monkeypatch.setattr(ycd, "fetch", lambda sym, rng="10y": (
        [1700000000], {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]}))
    monkeypatch.setattr(ycd.db, "get_db_path", lambda: str(tmp_path / "k.db"))
    writes = []
    monkeypatch.setattr(ycd, "write_kline", lambda *a, **k: writes.append(1) or 0)
    assert ycd.main() == 0
    assert writes == []


def test_main_continues_when_one_symbol_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ycd.sys, "argv", ["yahoo_chart_daily.py", "--symbols", "BAD,GOOD"])

    def fake_fetch(sym, rng="10y"):
        if sym == "BAD":
            raise RuntimeError("boom")
        return [1700000000], {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]}

    monkeypatch.setattr(ycd, "fetch", fake_fetch)
    monkeypatch.setattr(ycd.db, "get_db_path", lambda: str(tmp_path / "k.db"))
    assert ycd.main() == 0        # 一只失败不影响另一只
