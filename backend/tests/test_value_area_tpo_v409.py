"""v409：成交量缺失的 K 线（COMEX 日线）+ 价值区 TPO 兜底。

背景：XAU/XAG/HG 的日线来自新浪 COMEX，成交量新浪给 0 —— `metals_daily.py`
按"缺失就标缺失"写 NULL，不拿 0 冒充。之前 `load_candles` 要求 volume 非空，
于是 XAU 日线被整体丢成 0 根；`volume_profile` 也把所有无成交量 bar 跳过，
value_area 维度永远拿不到。这两条都在 v409 修掉。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_indicators as ind
import technical_state as st


def _candles(n, *, volume=None):
    """n 根横跨 100~110 的震荡蜡烛，volume=None 表示成交量缺失。"""
    out = []
    for i in range(n):
        base = 100.0 + (i % 7) * 1.5
        out.append([1_760_000_000 + i * 86400, base - 0.8, base + 1.2, base - 1.0, base,
                    volume])
    return out


# ---------------------------------------------------------------------------
# volume_profile：TPO 兜底
# ---------------------------------------------------------------------------

def test_value_area_uses_tpo_when_volume_missing():
    vp = ind.volume_profile(_candles(60, volume=None), lookback=60)
    assert vp["weight"] == "tpo"
    assert vp["poc"] is not None
    assert vp["bars"] == 60
    assert vp["position"] in ("above_value", "below_value", "in_value")


def test_value_area_uses_volume_when_present():
    vp = ind.volume_profile(_candles(60, volume=10.0), lookback=60)
    assert vp["weight"] == "volume"
    assert vp["poc"] is not None


def test_value_area_reports_empty_when_too_few_bars():
    vp = ind.volume_profile(_candles(5, volume=None), lookback=60)
    assert vp["weight"] is None
    assert vp["poc"] is None


def test_tpo_and_volume_poc_can_differ():
    """两种口径问的问题不同，POC 不该被当成同一个数。

    价格单调上行、成交量全压在底部（前 20 根放量 100 倍）：
      · 成交量口径的 POC 应该落在底部（那里才是换手最密集的地方）；
      · TPO 口径每根等权，POC 落在中段（用的是"价格被反复经过"的频次）。
    """
    candles = []
    for i in range(60):
        base = 100.0 + i
        candles.append([1_760_000_000 + i * 86400, base - 0.8, base + 1.2, base - 1.0,
                        base, 100.0 if i < 20 else 1.0])
    vp_vol = ind.volume_profile(candles, lookback=60)
    vp_tpo = ind.volume_profile([c[:5] + [None] for c in candles], lookback=60)
    assert vp_vol["weight"] == "volume" and vp_tpo["weight"] == "tpo"
    # 同样的价格序列，两种口径给出的价值区上沿不同 —— 这正是必须把口径标出来的理由
    assert vp_vol["vah"] != vp_tpo["vah"]


# ---------------------------------------------------------------------------
# load_candles：volume 为 NULL 的 bar 不能整根丢掉
# ---------------------------------------------------------------------------

def _mk_db(tmp_path):
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE external_indicators (asset TEXT, indicator TEXT, ts INTEGER, value REAL);
        CREATE TABLE macro_snapshots (metric_key TEXT, ts INTEGER, value REAL);
    """)
    now = 1_760_000_000_000
    step = 86400 * 1000
    for i in range(40):
        ts = now - (39 - i) * step
        px = 4000.0 + i
        conn.execute("INSERT INTO kline_bars VALUES ('XAU','1d',?,?,?,?,?,NULL)",
                     (ts, px, px + 5, px - 5, px + 1))
    conn.commit()
    return conn


def test_load_candles_keeps_null_volume_bars(tmp_path):
    conn = _mk_db(tmp_path)
    candles = st.load_candles(conn, "XAU", "1d", limit=40)
    assert len(candles) == 40
    assert candles[0][0] == 1_760_000_000 - 39 * 86400   # 秒，不是毫秒
    assert all(c[5] is None for c in candles)            # 成交量保持缺失，不补 0


def test_xau_daily_state_reads_with_tpo_value_area(tmp_path):
    conn = _mk_db(tmp_path)
    state = st.build_state(conn, "XAU", timeframe="1d", bars=40)
    va = state.get("value_area") or {}
    assert va.get("poc") is not None
    assert va.get("weight") == "tpo"          # 无成交量 → TPO 口径，且明确标出来
    assert "value_area" not in (state.get("missing") or [])
