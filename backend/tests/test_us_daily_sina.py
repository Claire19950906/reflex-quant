"""us_daily_sina（新浪美股日 K）单测。

2026-11-20: 覆盖率 0%。这模块管的是 AMZN / META / GOOGL 的全历史日线——
yfinance 在这三只上返回 0 行，所以它们是整条跑道的空白。

真正有风险的不是"能不能抓到"，是**抓到之后怎么落**：
  · ts 必须落在当日 16:00 UTC（与既有 AAPL/NVDA 日线同口径），差一小时就一天两根；
  · 占位行（o=h=l=c 且量为 0）不是真 K 线，落进去会在指标上画出一条横线；
  · META 改名前的历史在 FB 名下，同一 ts 撞上时**以现名为准**，不能反着覆盖。
"""

import sqlite3
import sys
import types

import pytest

import us_daily_sina as uds


def _item(d, o, h, l, c, v="100"):
    return {"d": d, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v)}


# ── to_rows ───────────────────────────────────────────────────────────────

def test_to_rows_puts_ts_at_close_hour_utc():
    rows = uds.to_rows("AMZN", [_item("2026-11-13", 100, 102, 99, 101)])
    ts = rows[0][2]
    stamp = __import__("datetime").datetime.fromtimestamp(ts / 1000, __import__("datetime").timezone.utc)
    assert (stamp.hour, stamp.minute) == (16, 0)      # 收盘 16:00 UTC，与既有日线同口径
    assert rows[0][0] == "AMZN" and rows[0][1] == "1d"


def test_to_rows_drops_placeholder_rows():
    # 停牌日新浪会给 o=h=l=c 且量 0 的占位行，落进去就是一条假横线
    rows = uds.to_rows("X", [
        _item("2026-11-13", 100, 100, 100, 100, v=0),
        _item("2026-11-14", 100, 102, 99, 101),
    ])
    assert len(rows) == 1 and rows[0][6] == 101


def test_to_rows_drops_bad_prices_and_bad_dates():
    rows = uds.to_rows("X", [
        _item("bad-date", 1, 1, 1, 1),
        _item("2026-11-13", 0, 1, 1, 1),         # 0 价
        _item("2026-11-14", 1, 1, 2, 1),         # high < low
        _item("2026-11-16", 1, 2, 0.5, 1.5),
    ])
    assert len(rows) == 1 and rows[0][3] == 1


def test_to_rows_keeps_first_of_same_day_and_sorts():
    rows = uds.to_rows("X", [
        _item("2026-11-16", 3, 3, 3, 3),
        _item("2026-11-13", 1, 2, 0.5, 1.5),
        _item("2026-11-13", 9, 9, 9, 9),
    ])
    assert [r[6] for r in rows] == [1.5, 3.0]        # 同日只留第一根，且按时间升序
    assert rows[0][2] < rows[1][2]


def test_to_rows_missing_volume_defaults_zero():
    rows = uds.to_rows("X", [{"d": "2026-11-13", "o": "1", "h": "2", "l": "0.5", "c": "1.5"}])
    assert rows[0][7] == 0


# ── 落库 ──────────────────────────────────────────────────────────────────

def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, "
        "high REAL, low REAL, close REAL, volume INTEGER, fetched_at INTEGER, "
        "PRIMARY KEY (symbol, timeframe, ts))")
    return conn


def test_write_kline_inserts_then_upserts():
    conn = _mem_db()
    rows = uds.to_rows("X", [_item("2026-11-13", 1, 2, 0.5, 1.5)])
    assert uds.write_kline(conn, rows) == 1
    rows2 = uds.to_rows("X", [_item("2026-11-13", 1, 2, 0.5, 1.8)])
    uds.write_kline(conn, rows2)
    assert conn.execute("SELECT COUNT(*) FROM kline_bars").fetchone()[0] == 1
    assert conn.execute("SELECT close FROM kline_bars").fetchone()[0] == 1.8


def test_write_kline_retries_on_lock(monkeypatch):
    class LockedOnce:
        def __init__(self):
            self.calls = 0
            self.writes = 0

        def executemany(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            self.writes += 1
            cur = type("C", (), {"rowcount": 2})()
            return cur

        def commit(self):
            pass

    monkeypatch.setattr(uds.time, "sleep", lambda *_: None)
    assert uds.write_kline(LockedOnce(), [("X", "1d", 1, 1, 1, 1, 1, 1, 0)]) == 2


def test_write_kline_reraises_other_errors():
    class Boom:
        def executemany(self, *a, **k):
            raise sqlite3.OperationalError("no such table: kline_bars")

        def commit(self):
            pass

    with pytest.raises(sqlite3.OperationalError):
        uds.write_kline(Boom(), [("X", "1d", 1, 1, 1, 1, 1, 1, 0)])


# ── main：别名合并（FB→META、GOOG→GOOGL）────────────────────────────────────

def _patch(monkeypatch, tmp_path, responses, argv):
    monkeypatch.setattr(uds.sys, "argv", ["us_daily_sina.py"] + argv)
    monkeypatch.setattr(uds, "fetch", lambda sym, timeout=20: responses.get(sym, []))
    monkeypatch.setattr(uds.db, "get_db_path", lambda: str(tmp_path / "k.db"))


def test_main_dry_run_reports_rows(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, {"AMZN": [_item("2026-11-13", 1, 2, 0.5, 1.5)]},
           ["--symbols", "AMZN"])
    assert uds.main() == 0


def test_main_merges_alias_and_prefers_current_symbol(monkeypatch, tmp_path):
    same_day_meta = _item("2026-11-13", 100, 101, 99, 100.5)      # 现名
    old_day = _item("2012-05-18", 30, 33, 29, 32.5)               # 只在 FB 名下
    dup_day = _item("2026-11-13", 200, 201, 199, 200.5)           # 别名里同一天（旧数据）
    _patch(monkeypatch, tmp_path, {"META": [same_day_meta], "FB": [old_day, dup_day]},
           ["--symbols", "META"])
    conn = sqlite3.connect(str(tmp_path / "k.db"))
    conn.execute(
        "CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, "
        "high REAL, low REAL, close REAL, volume INTEGER, fetched_at INTEGER, "
        "PRIMARY KEY (symbol, timeframe, ts))")
    conn.commit()
    conn.close()

    assert uds.main() == 0          # dry-run 不写库

    # 真写一次：META 应比"只查 META"多出 2012 那根，且同日以现名为准
    writes = []

    def fake_write(conn_, rows_):
        writes.append(rows_)
        return len(rows_)

    _patch(monkeypatch, tmp_path, {"META": [same_day_meta], "FB": [old_day, dup_day]},
           ["--apply", "--symbols", "META"])
    monkeypatch.setattr(uds, "write_kline", fake_write)
    monkeypatch.setattr(uds, "refresh_ohlcv", lambda c, s: {"META": len(writes[-1])})
    assert uds.main() == 0
    rows = writes[0]
    assert len(rows) == 2                                         # 2012 一根 + 2026 一根
    by_ts = {r[2]: r for r in rows}
    newest = max(by_ts)
    assert by_ts[newest][6] == 100.5                              # 现名 META 的收盘价，不是 FB 的 200.5


def test_main_continues_after_symbol_failure(monkeypatch, tmp_path):
    def boom(sym, timeout=20):
        raise ValueError("格式异常")

    _patch(monkeypatch, tmp_path, {}, ["--symbols", "AMZN"])
    monkeypatch.setattr(uds, "fetch", boom)
    assert uds.main() == 0                                        # 单只失败不中断整批


def test_fetch_rejects_non_list(monkeypatch):
    class R:
        def read(self):
            return b'{"error": "rate limited"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(uds, "urlopen", lambda req, timeout=0: R())
    with pytest.raises(ValueError):
        uds.fetch("AMZN")
