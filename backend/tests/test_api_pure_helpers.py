"""api_server 里那批"不碰网络、不碰数据库"的纯函数单测。

2026-11-20: 这个文件只测一类东西——**纯函数**。它们原来混在 9 千行的入口文件里，
谁都不敢动，因为没人知道改了会碎哪儿。抽不出来（有的还带着模块级依赖）就先测，
测住行为再谈重构。

挑的都是真出过事故的口径：时间戳单位混用（毫秒当秒用 → 新闻上下文永远 0 条）、
美东夏令时偏移、BLS 发布窗口配额、路径穿越、单笔盈亏口径。
"""

import datetime as dt
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import api_server as api

TZ = dt.timezone(dt.timedelta(hours=8))


# ── 时间戳口径 ──────────────────────────────────────────────────────────────

def test_utc_ts_to_iso_uses_T_separator():
    # 'T' 分隔才能和库里 TEXT 列做字典序比较；空格分隔会整片漏窗口。
    assert api._utc_ts_to_iso(0) == "1970-01-01T00:00:00"
    assert "T" in api._utc_ts_to_iso(1794624153)
    assert " " not in api._utc_ts_to_iso(1794624153)


def test_utc_ts_to_space_iso_uses_space_separator():
    assert api._utc_ts_to_space_iso(0) == "1970-01-01 00:00:00"
    out = api._utc_ts_to_space_iso(1794624153)
    assert "T" not in out and " " in out


def test_to_sec_ts_normalises_ms_and_s():
    # 真锚 1794624153（秒）与它的毫秒形式必须归一成同一个值
    assert api._to_sec_ts(1794624153) == 1794624153
    assert api._to_sec_ts(1794624153000) == 1794624153
    assert api._to_sec_ts("1794624153000") == 1794624153
    assert api._to_sec_ts(None) == 0
    assert api._to_sec_ts("not a number") == 0


def test_age_hours_of_unparseable_is_none():
    # 解析不了就返回 None（=视为新鲜），而不是抛异常把整条链路打断
    assert api._age_hours_of("") is None
    assert api._age_hours_of("这不是时间") is None
    hours = api._age_hours_of("2020-01-01T00:00:00+08:00")
    assert hours is not None and hours > 0


# ── 缓存键 / 事件 id 解析 ───────────────────────────────────────────────────

def test_movers_cache_key_includes_all_args():
    assert api._movers_cache_key("1h", 20, False) == "1h|20|0"
    assert api._movers_cache_key("1h", 100, True) == "1h|100|1"
    # 只用 tf 做键会让不同 limit 互相污染 —— 这里锁住"键里必须有 limit"
    assert api._movers_cache_key("1h", 20, False) != api._movers_cache_key("1h", 100, False)


def test_fred_keys():
    assert api._fred_date_key("fred-cpi-2026-08-01") == "2026-08-01"
    assert api._fred_date_key("no-date") == "no-date"
    assert api._fred_series_key("fred-CPIAUCSL-2026-08-01") == "fred-CPIAUCSL"
    assert api._fred_series_key("fred-trend-T10Y2Y-2026-07-05") == "fred-trend-T10Y2Y"


# ── 美东时区 / BLS 发布窗口 ────────────────────────────────────────────────

def _utc(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)


def test_us_eastern_offset_follows_dst():
    assert api._us_eastern_offset(_utc(2026, 1, 15)) == -5   # 冬令时
    assert api._us_eastern_offset(_utc(2026, 7, 15)) == -4   # 夏令时
    # 2026 年 3 月第二个周日是 3/8；之前 -5，之后 -4
    assert api._us_eastern_offset(_utc(2026, 3, 7, 12)) == -5
    assert api._us_eastern_offset(_utc(2026, 3, 9, 12)) == -4
    # 11 月第一个周日是 11/1
    assert api._us_eastern_offset(_utc(2026, 10, 31, 12)) == -4
    assert api._us_eastern_offset(_utc(2026, 11, 2, 12)) == -5


def test_is_us_workday():
    assert api._is_us_workday(dt.datetime(2026, 11, 20)) is True    # 周五
    assert api._is_us_workday(dt.datetime(2026, 11, 21)) is False   # 周六
    assert api._is_us_workday(dt.datetime(2026, 11, 22)) is False   # 周日


def test_bls_windows_only_on_workday():
    wed_0830 = dt.datetime(2026, 11, 18, 8, 30)
    sat_0830 = dt.datetime(2026, 11, 21, 8, 30)
    assert api._bls_core_window_active(wed_0830) is True
    assert api._bls_core_window_active(sat_0830) is False
    assert api._bls_core_window_active(dt.datetime(2026, 11, 18, 8, 51)) is False
    assert api._bls_window_active(dt.datetime(2026, 11, 18, 7, 30)) is True
    assert api._bls_window_active(dt.datetime(2026, 11, 18, 7, 29)) is False
    assert api._bls_window_active(dt.datetime(2026, 11, 18, 10, 30)) is True
    assert api._bls_mid_window_active(dt.datetime(2026, 11, 18, 8, 20)) is True
    assert api._bls_mid_window_active(dt.datetime(2026, 11, 18, 8, 30)) is False
    assert api._bls_mid_window_active(dt.datetime(2026, 11, 18, 9, 0)) is True
    assert api._bls_mid_window_active(dt.datetime(2026, 11, 18, 9, 6)) is False


def test_bls_sleep_seconds_tiers():
    # 汇率换算走美东：UTC 13:30 = 夏令时 09:30（外围窗口内）
    assert api._bls_sleep_seconds(_utc(2026, 7, 15, 12, 30)) == 5    # 08:30 ET
    assert api._bls_sleep_seconds(_utc(2026, 7, 15, 12, 20)) == 20   # 08:20 ET
    assert api._bls_sleep_seconds(_utc(2026, 7, 15, 13, 30)) == 90   # 09:30 ET
    assert api._bls_sleep_seconds(_utc(2026, 7, 15, 20, 0)) == 1800  # 16:00 ET


# ── 每日任务到期判断 ───────────────────────────────────────────────────────

def test_equity_daily_due_respects_hour_and_state():
    early = dt.datetime(2026, 11, 20, 4, 0)
    late = dt.datetime(2026, 11, 20, 6, 0)
    assert api._equity_daily_due(now=early, last_run="") is False
    assert api._equity_daily_due(now=late, last_run="") is True
    assert api._equity_daily_due(now=late, last_run="2026-11-20") is False
    assert api._equity_daily_due(now=late, last_run="2026-11-19") is True


# ── 单笔盈亏口径 ───────────────────────────────────────────────────────────

def test_paper_metrics_buy_tracking():
    sig = {"entry_price": 100.0, "action": "BUY", "settled": 0,
           "max_price": 104.0, "min_price": 97.0, "notional_usdt": 100.0}
    out = api._paper_metrics(sig, 102.0)
    assert out["current_pnl_pct"] == pytest.approx(2.0)
    assert out["current_pnl_usdt"] == pytest.approx(2.0)   # 名义金额 × 涨跌幅，不被杠杆放大
    assert out["live_mfe_pct"] == pytest.approx(4.0)
    assert out["live_mae_pct"] == pytest.approx(3.0)
    assert out["pricing_status"] == "LIVE"
    assert out["paper_status"] == "TRACKING"


def test_paper_metrics_sell_tracking():
    sig = {"entry_price": 100.0, "action": "SELL", "settled": 0,
           "max_price": 104.0, "min_price": 97.0, "notional_usdt": 100.0}
    out = api._paper_metrics(sig, 98.0)
    assert out["current_pnl_pct"] == pytest.approx(2.0)
    assert out["live_mfe_pct"] == pytest.approx(3.0)   # 空头 MFE = 向下空间
    assert out["live_mae_pct"] == pytest.approx(4.0)


def test_paper_metrics_settled_uses_engine_numbers():
    # 已结算必须用引擎权威值，不能拿 (exit-entry) 重算，否则分级止盈口径会错
    sig = {"entry_price": 100.0, "action": "BUY", "settled": 1,
           "forward_pnl": 1.37, "mfe_pct": 2.5, "mae_pct": 0.4, "notional_usdt": 1000.0}
    out = api._paper_metrics(sig, 101.0)
    assert out["current_pnl_pct"] == pytest.approx(1.37)
    assert out["current_pnl_usdt"] == pytest.approx(13.7)
    assert out["paper_status"] == "SETTLED"


def test_paper_metrics_no_price_is_unavailable():
    sig = {"entry_price": 100.0, "action": "BUY", "settled": 0, "notional_usdt": 100.0}
    out = api._paper_metrics(sig, None)
    assert out["current_pnl_pct"] is None
    assert out["pricing_status"] == "UNAVAILABLE"


# ── K 线聚合 ───────────────────────────────────────────────────────────────

def _bar(t, o, h, l, c, v):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_aggregate_bars_buckets_ohlcv():
    bars = [
        _bar(0, 10, 12, 9, 11, 5),
        _bar(60, 11, 13, 10, 12, 6),
        _bar(120, 12, 12, 8, 9, 7),
        _bar(900, 9, 10, 9, 10, 8),
    ]
    agg = api._aggregate_bars(bars, 900)
    assert len(agg) == 2
    assert agg[0]["time"] == 0
    assert agg[0]["open"] == 10 and agg[0]["close"] == 9
    assert agg[0]["high"] == 13 and agg[0]["low"] == 8
    assert agg[0]["volume"] == 18
    assert agg[1]["open"] == 9 and agg[1]["close"] == 10
    assert api._aggregate_bars([], 900) == []


# ── pairs 面板拼装 ─────────────────────────────────────────────────────────

def test_pair_missing_assets_skips_crypto_and_excluded():
    settings = {"tracks": ["crypto", "stocks"]}
    missing = api._pair_missing_assets(settings, exclude={"NVDA", "TSLA"})
    # crypto 三品种固定来自行情快照，不该再走网络
    for a in ("BTC", "ETH", "SOL"):
        assert a not in missing
    assert "NVDA" not in missing and "TSLA" not in missing
    assert "AAPL" in missing


def test_replay_pairs_prefers_market_then_known_then_fetched():
    settings = {"tracks": ["gold", "oil"]}
    market = {"items": [{"asset": "XAU", "price": 2650.5, "change24h": 1.24, "source": "okx", "sourceCount": 2}]}
    pairs = api._replay_pairs(settings, market, known_prices={"WTI": 70.2}, fetched={})
    by_asset = {p["asset"]: p for p in pairs}
    assert by_asset["XAU"]["price"] == pytest.approx(2650.5)
    assert by_asset["XAU"]["source"] == "okx" and by_asset["XAU"]["source_count"] == 2
    assert by_asset["XAU"]["status"] == "LIVE"
    assert by_asset["WTI"]["price"] == pytest.approx(70.2)
    assert by_asset["WTI"]["source"] == "fallback"
    # 没价 → 明确 UNAVAILABLE，不给假价
    pairs2 = api._replay_pairs(settings, None, known_prices={}, fetched={"XAU": None})
    assert all(p["price"] is None and p["status"] == "UNAVAILABLE" for p in pairs2)


def test_replay_want_limit_keeps_at_most_four():
    for n in (10, 20, 30, 40, 50):
        api._replay_want_limit(n)
    assert len(api._REPLAY_WANTED_LIMITS) <= 4


# ── 安全边界 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["../etc/passwd", "..\\windows\\system32", "", "/etc/hosts"])
def test_backup_download_path_blocks_traversal(name):
    assert api._backup_download_path(name) is None


def test_backup_download_path_rejects_missing_file():
    assert api._backup_download_path("definitely-not-here.zip") is None


def test_authorize_trading_manager_fail_closed(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(api, "_trading_manager_enabled", lambda: False)
    with pytest.raises(HTTPException) as exc:
        api._authorize_trading_manager("anything")
    assert exc.value.status_code == 503

    monkeypatch.setattr(api, "_trading_manager_enabled", lambda: True)
    monkeypatch.setattr(api.config, "TRADING_MANAGER_TOKEN", "", raising=False)
    with pytest.raises(HTTPException) as exc2:
        api._authorize_trading_manager("anything")
    assert exc2.value.status_code == 503


def test_authorize_trading_manager_rejects_wrong_token(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(api, "_trading_manager_enabled", lambda: True)
    monkeypatch.setattr(api.config, "TRADING_MANAGER_TOKEN", "secret-token", raising=False)
    with pytest.raises(HTTPException) as exc:
        api._authorize_trading_manager("wrong")
    assert exc.value.status_code == 401
    # 对的 token 不抛
    api._authorize_trading_manager("secret-token")


# ── 路由摊平 ───────────────────────────────────────────────────────────────

def test_include_flat_makes_routes_visible_on_app():
    # bridge_server 只认带 .path 的 APIRoute；被 include_router 包起来的路由会静默消失
    app = FastAPI()
    router = APIRouter()

    @router.get("/api/ping-flat")
    def _ping():
        return {"ok": True}

    api._include_flat(app, router)
    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert "/api/ping-flat" in paths
    assert TestClient(app).get("/api/ping-flat").json() == {"ok": True}


def test_format_swing_annotation_shape():
    out = api._format_swing_annotation({
        "ts": 1794624153, "trigger_type": "swing", "direction": "bullish",
        "atmosphere": {"label": "expected", "atmosphere_score": 0.62},
    })
    assert out.startswith("[v221.3 swing:")
    assert "type=swing" in out and "dir=bullish" in out
    assert "atmos=expected" in out and "score=0.62" in out
    # 缺字段时不能炸
    empty = api._format_swing_annotation({})
    assert "type=n/a" in empty and "ts=n/a" in empty
