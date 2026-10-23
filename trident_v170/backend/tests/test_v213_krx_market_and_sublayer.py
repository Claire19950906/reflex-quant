# -*- coding: utf-8 -*-
"""v213 海力士市场时段 + review_agent sublayer 回归测试。

修复背景:
  1. market_hours.MARKETS 原只覆盖 WTI/XAU/BTC/美股。SKHYNIX 调
     status_for("SKHYNIX") 返回 None → review_agent 拿不到时段 → 兜底
     us_market_weekend(错,海力士不在美股)。
  2. review_agent._session_to_sublayer 原 SKHYNIX 分支缺失。
  3. review_agent._SUBLAYER_TWO_LEVEL[2] 缺 kr_market_*/kr_lunch_break,
     parse_sublayer 静默回退。

v213 修复:
  · market_hours 新增 kr_equity (KOSPI 09:00-15:30 KST, 11:30-12:30 午休)
  · status_for("SKHYNIX") -> kr_equity
  · review_agent 新增 SKHYNIX/000660 子分支 + 接入 market_hours
  · _SUBLAYER_TWO_LEVEL[2] 增 11 个韩股休市标签 + kr_lunch_break
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import market_hours as mh
import review_agent as ra


# ── A. market_hours: KRX 接入 ────────────────────────────────────

def test_market_hours_has_kr_equity():
    ids = [m["id"] for m in mh.MARKETS]
    assert "kr_equity" in ids, "kr_equity 缺失, 当前 ids=%s" % ids


def test_market_hours_kr_uses_kst():
    kr = next(m for m in mh.MARKETS if m["id"] == "kr_equity")
    assert str(kr["tz"]) == "Asia/Seoul"


def test_market_hours_kr_lunch_break_defined():
    kr = next(m for m in mh.MARKETS if m["id"] == "kr_equity")
    assert "lunch_break" in kr
    assert kr["lunch_break"] == ("11:30", "12:30")


def test_status_for_skhynix_returns_kr_equity():
    st = mh.status_for("SKHYNIX")
    assert st is not None
    assert st["id"] == "kr_equity"


def test_status_for_yahoo_ticker_skhynix():
    st = mh.status_for("000660")
    assert st is not None
    assert st["id"] == "kr_equity"


def test_status_for_nvda_still_us_equity():
    st = mh.status_for("NVDA")
    assert st is not None
    assert st["id"] == "us_equity"


def test_status_kr_weekend_is_closed():
    kr = next(m for m in mh.MARKETS if m["id"] == "kr_equity")
    sat = datetime(2026, 10, 3, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    res = mh._status_kr_equity(kr, sat)
    assert res["status"] == "closed"
    assert res["sub_status"] == "weekend"


def test_status_kr_lunch_break_recognized():
    kr = next(m for m in mh.MARKETS if m["id"] == "kr_equity")
    wed_noon = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    res = mh._status_kr_equity(kr, wed_noon)
    assert res["status"] == "open"
    assert res["sub_status"] == "lunch_break"


def test_status_kr_regular_morning():
    kr = next(m for m in mh.MARKETS if m["id"] == "kr_equity")
    wed_am = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    res = mh._status_kr_equity(kr, wed_am)
    assert res["status"] == "open"
    assert res["sub_status"] == "regular"


def test_is_kr_helper():
    assert mh.is_kr("SKHYNIX") is True
    assert mh.is_kr("000660") is True
    assert mh.is_kr("nvda") is False
    assert mh.is_kr("") is False


# ── B. review_agent: _session_to_sublayer 接管 SKHYNIX ────────────

def test_session_to_sublayer_skhynix_weekend_kr():
    res = ra._session_to_sublayer(
        "SKHYNIX", {"session": "non_active", "detail": "周末(星期六)入场"}
    )
    assert res == "market_closed:kr_market_weekend"


def test_session_to_sublayer_000660_weekend_kr():
    res = ra._session_to_sublayer(
        "000660", {"session": "non_active", "detail": "周末(星期日)入场"}
    )
    assert res == "market_closed:kr_market_weekend"


def test_session_to_sublayer_skhynix_late_night():
    res = ra._session_to_sublayer(
        "SKHYNIX", {"session": "non_active", "detail": "深夜低流动性(02:30)入场"}
    )
    assert res == "low_liquidity:asia_pre_us"


# ── C. review_agent: regression (NVDA / TSLA / BTC / AAPL) ──────

def test_session_to_sublayer_nvda_weekend_unchanged():
    res = ra._session_to_sublayer(
        "NVDA", {"session": "non_active", "detail": "周末(星期六)入场"}
    )
    assert res == "market_closed:us_market_weekend"


def test_session_to_sublayer_tsla_weekend_unchanged():
    res = ra._session_to_sublayer(
        "TSLA", {"session": "non_active", "detail": "周末(星期日)入场"}
    )
    assert res == "market_closed:us_market_weekend"


def test_session_to_sublayer_btc_weekend_unchanged():
    res = ra._session_to_sublayer(
        "BTC", {"session": "non_active", "detail": "周末(星期六)入场"}
    )
    assert res == "low_liquidity:crypto_late_weekend"


def test_session_to_sublayer_aapl_weekend_unchanged():
    res = ra._session_to_sublayer(
        "AAPL", {"session": "non_active", "detail": "周末(星期六)入场"}
    )
    assert res == "market_closed:us_market_weekend"


# ── D. parse_sublayer 认可新 sublayer ──────────────────────────

def test_parse_sublayer_kr_market_weekend_accepted():
    l1, l2 = ra.parse_sublayer(2, "market_closed:kr_market_weekend")
    assert l1 == "market_closed"
    assert l2 == "kr_market_weekend"


def test_parse_sublayer_kr_lunch_break_accepted():
    l1, l2 = ra.parse_sublayer(2, "low_liquidity:kr_lunch_break")
    assert l1 == "low_liquidity"
    assert l2 == "kr_lunch_break"


def test_parse_sublayer_kr_holiday_accepted():
    for sub in ("kr_market_holiday_seollal",
                "kr_market_holiday_chuseok",
                "kr_market_holiday_national",
                "kr_market_holiday_liberation",
                "kr_market_holiday_buddha",
                "kr_market_holiday_christmas"):
        l1, l2 = ra.parse_sublayer(2, "market_closed:" + sub)
        assert l1 == "market_closed"
        assert l2 == sub, "韩节假日 %s 未被接受；实测: %s" % (sub, l2)


# ── E. 集成：market_hours 给出 lunch_break 时 review_agent 也能传 ──

def test_integration_kr_lunch_break_propagates():
    """当 market_hours 返回 lunch_break 时，_session_to_sublayer 应走 kr_lunch_break。"""
    import market_hours as _mh
    original = _mh.status_for

    def fake_status_for(asset):
        return {
            "id": "kr_equity", "name": "韩国 KOSPI", "exchange": "KRX/KOSPI",
            "tag": "KR", "locale_now": "2026-10-02 12:00 KST",
            "status": "open", "sub_status": "lunch_break",
            "minutes_to_next": 30, "next_event": "午休结束",
        }
    _mh.status_for = fake_status_for
    try:
        res = ra._session_to_sublayer(
            "SKHYNIX", {"session": "non_active", "detail": "深夜低流动性(02:30)入场"}
        )
        assert res == "low_liquidity:kr_lunch_break", \
            "market_hours 返回 lunch_break 时必须走 kr_lunch_break；实测: %s" % res
    finally:
        _mh.status_for = original
