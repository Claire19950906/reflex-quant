# -*- coding: utf-8 -*-
"""yfinance_feed 单元测试 — 用 mock 替代真实网络，避免 pytest 拉远端数据。

覆盖：
  - symbol → asset 映射（_YF_TO_ASSET）
  - cache 命中 / 失效
  - ingest 失败容错（写入返回 ok=False 而不是崩）
  - DEFAULT_* 默认值正确
"""
from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

import yfinance_feed as yf


def test_default_kr_symbols_includes_hynix():
    """默认韩股 symbol 必须包含 SK 海力士（000660.KS）。"""
    assert "000660.KS" in yf.DEFAULT_KR_SYMBOLS


def test_default_altcoin_symbols_covers_major_coins():
    """默认山寨币必须覆盖 SOL/DOGE/XRP（决策影响最大）。"""
    for sym in ("SOL-USD", "DOGE-USD", "XRP-USD"):
        assert sym in yf.DEFAULT_ALTCOIN_SYMBOLS, f"missing {sym}"


def test_yf_to_asset_mapping():
    """symbol → asset code 映射必须正确（LLM target_asset 用）。"""
    assert yf._YF_TO_ASSET["000660.KS"] == "SKHYNIX"
    assert yf._YF_TO_ASSET["SOL-USD"] == "SOL"
    assert yf._YF_TO_ASSET["DOGE-USD"] == "DOGE"


def test_cache_roundtrip():
    """缓存写入 + 读取必须命中，reset 后才能再次写入。"""
    yf.reset_cache()
    sample = [{"ts": 1700000000, "value": 123.45}]
    yf._cache_put("TEST", sample)
    hit = yf._cache_get("TEST")
    assert hit == sample
    yf.reset_cache()
    assert yf._cache_get("TEST") is None


def test_fetch_history_uses_cache_when_available():
    """fetch_history 在缓存命中时不应触发 yfinance。"""
    yf.reset_cache()
    sample = [{"ts": 1700000000, "value": 999.0}]
    yf._cache_put("000660.KS", sample)
    with patch.object(yf, "_fetch_yfinance") as mocked:
        out = yf.fetch_history("000660.KS", use_cache=True)
    mocked.assert_not_called()
    assert out == sample


def test_ingest_one_returns_ok_false_on_network_failure():
    """yfinance 网络失败时，_ingest_one 应返回 {ok: False, error: ...}，不抛异常。"""
    yf.reset_cache()
    with patch.object(yf, "fetch_history",
                      side_effect=RuntimeError("network timeout")):
        result = yf._ingest_one("000660.KS")
    assert result["ok"] is False
    assert "network timeout" in result["error"]
    assert result["symbol"] == "000660.KS"
    assert result["asset"] == "SKHYNIX"


def test_ingest_one_writes_to_external_indicators(tmp_db):
    """成功 ingest 时，external_indicators 应有新行。"""
    yf.reset_cache()
    fake_rows = [
        {"ts": int(time.time()) - 86400 * 2, "value": 100.0},
        {"ts": int(time.time()) - 86400, "value": 110.0},
        {"ts": int(time.time()), "value": 120.0},
    ]
    with patch.object(yf, "fetch_history", return_value=fake_rows):
        result = yf._ingest_one("000660.KS")
    assert result["ok"] is True
    assert result["written"] == 3

    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute(
        "SELECT asset, indicator, value FROM external_indicators WHERE asset='SKHYNIX' ORDER BY ts"
    )
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 3
    assert rows[0][0] == "SKHYNIX"
    assert rows[0][1] == "000660.KS"


def test_ingest_one_replaces_existing_rows(tmp_db):
    """同一 (asset, indicator) 二次 ingest 时，旧行应被 DELETE 替换，不重复。"""
    yf.reset_cache()
    fake_rows = [{"ts": int(time.time()), "value": 100.0}]
    with patch.object(yf, "fetch_history", return_value=fake_rows):
        r1 = yf._ingest_one("000660.KS")
        r2 = yf._ingest_one("000660.KS")
    assert r1["ok"] and r2["ok"]
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM external_indicators WHERE asset='SKHYNIX' AND indicator='000660.KS'"
    )
    (n,) = cur.fetchone()
    conn.close()
    assert n == 1, f"expected 1 row after replace, got {n}"


def test_ingest_kr_and_altcoin_batch(tmp_db):
    """ingest_kr_equity + ingest_altcoin 应分别返回 1 + 5 行结果。"""
    yf.reset_cache()
    fake = [{"ts": int(time.time()), "value": 1.0}]
    with patch.object(yf, "fetch_history", return_value=fake):
        kr = yf.ingest_kr_equity()
        alt = yf.ingest_altcoin()
    assert len(kr) == 1
    assert len(alt) == 5
    assert all(r["ok"] for r in kr)
    assert all(r["ok"] for r in alt)


def test_ingest_all_aggregates(tmp_db):
    """ingest_all 返回 {kr_ok, alt_ok, ...} 聚合。"""
    yf.reset_cache()
    fake = [{"ts": int(time.time()), "value": 1.0}]
    with patch.object(yf, "fetch_history", return_value=fake):
        out = yf.ingest_all()
    assert out["kr_ok"] == 1 and out["kr_fail"] == 0
    assert out["alt_ok"] == 5 and out["alt_fail"] == 0
    assert "ts" in out


# ── HYNIX 关键词扩展（v226） ────────────────────────────────────────────


def test_hynix_keywords_includes_hbm3_h200():
    """v226 扩展：HBM3 / HBM3E / H200 / HBM4 等海力士特有产品词必须命中。"""
    from asia_prices import detect_hynix, _HYNIX_KEYWORDS, _HYNIX_STRONG_KEYWORDS

    # 海力士特有产品词必须进 strong 集合（精准触发提醒用）
    for kw in ("hbm3", "hbm3e", "h200", "hbm4"):
        assert kw in _HYNIX_STRONG_KEYWORDS, f"{kw} 应为强关键词"
        assert kw in _HYNIX_KEYWORDS

    # detect_hynix 必须命中
    for txt in ("hbm3e 量产", "H200 大卖", "海力士 hbm4 进展"):
        assert detect_hynix(txt), f"应命中：{txt}"


def test_hynix_keywords_preserves_existing_terms():
    """扩展不能丢掉原有强关键词（向后兼容）。"""
    from asia_prices import _HYNIX_STRONG_KEYWORDS, _HYNIX_WEAK_KEYWORDS, _HYNIX_KEYWORDS
    # 原 strong 5 个不能丢
    for kw in ("海力士", "sk hynix", "skhynix", "hynix", "000660"):
        assert kw in _HYNIX_STRONG_KEYWORDS
    # 原 weak 6 个不能丢
    for kw in ("hbm", "高带宽内存", "存储芯片", "dram", "nand", "内存芯片"):
        assert kw in _HYNIX_WEAK_KEYWORDS


# ── pytest fixtures ─────────────────────────────────────────────────────

import os
import pytest


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """隔离 SQLite 到 tmp 文件，避免污染生产库。"""
    db_path = tmp_path / "test_event_bus.db"
    monkeypatch.setenv("TRIDENT_DB_PATH", str(db_path))
    # 同时改 yfinance_feed._db_path 的实现
    import db as _db
    monkeypatch.setattr(_db, "get_db_path", lambda: str(db_path))
    return str(db_path)


def test_daemon_loop_calls_ingest_and_sleeps(monkeypatch):
    """daemon_loop: 第一次 ingest → sleep 60s（最小），用 monkeypatch 短路 sleep。"""
    calls = {"ingest": 0, "sleep_durations": []}

    def fake_ingest(lookback_days=60):
        calls["ingest"] += 1
        return {"kr_ok": 1, "kr_fail": 0, "alt_ok": 2, "alt_fail": 0,
                "kr": [{}] * 1, "altcoin": [{}] * 2, "ts": 0}

    def fake_sleep(s):
        calls["sleep_durations"].append(s)
        raise StopIteration

    monkeypatch.setattr(yf, "ingest_all", fake_ingest)
    monkeypatch.setattr(yf.time, "sleep", fake_sleep)
    monkeypatch.setenv("YFINANCE_INTERVAL_S", "60")
    try:
        yf.daemon_loop()
    except StopIteration:
        pass
    assert calls["ingest"] == 1
    assert calls["sleep_durations"] == [60]


def test_daemon_loop_interval_min_60(monkeypatch):
    """daemon_loop: 任何 < 60s 的间隔被强制抬到 60s。"""
    sleep_durations = []

    def fake_ingest(lookback_days=60):
        return {"kr_ok": 0, "kr_fail": 0, "alt_ok": 0, "alt_fail": 0,
                "kr": [], "altcoin": [], "ts": 0}

    def fake_sleep(s):
        sleep_durations.append(s)
        raise StopIteration

    monkeypatch.setattr(yf, "ingest_all", fake_ingest)
    monkeypatch.setattr(yf.time, "sleep", fake_sleep)
    monkeypatch.setenv("YFINANCE_INTERVAL_S", "5")
    try:
        yf.daemon_loop()
    except StopIteration:
        pass
    assert sleep_durations[0] == 60


def test_daemon_loop_logs_ok_count(monkeypatch, capsys):
    """daemon_loop: 成功 ingest → 打印 kr=N/N alt=N/N 行。"""
    monkeypatch.setattr(yf, "ingest_all",
                        lambda lookback_days=60: {
                            "kr_ok": 1, "kr_fail": 0, "alt_ok": 5, "alt_fail": 0,
                            "kr": [{}] * 1, "altcoin": [{}] * 5, "ts": 1234567890})

    def fake_sleep(s):
        raise StopIteration
    monkeypatch.setattr(yf.time, "sleep", fake_sleep)
    monkeypatch.setenv("YFINANCE_INTERVAL_S", "60")
    try:
        yf.daemon_loop()
    except StopIteration:
        pass
    captured = capsys.readouterr()
    assert "kr=1/1" in captured.out
    assert "alt=5/5" in captured.out