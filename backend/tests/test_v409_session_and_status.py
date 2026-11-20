# -*- coding: utf-8 -*-
"""v409 — 休市感知 + 硬数据状态分支（顺序不能错）。

三个必须成立的行为：
1. CME 系在每日 22:00–23:00 UTC / 周末停盘时判 closed，加密 7×24 恒 open；
2. `hard_status` 在休市时记 `missing:market_closed`，不再冒充 `feed_stale`；
3. 桶标签（CN_STOCK/US_STOCK…）记 `missing:unresolved_target` ——
   这条分支曾被我写在 `no_local_feed` 之后，永远走不到（死分支），
   本测试就是防它再被写回去。
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import market_session as ms
from signals import decision_provenance as dp


def _ts(y, m, d, hh, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0))


def test_cme_daily_maintenance_closed():
    # 2026-11-16 是周一：22:05 UTC 在维护窗口内，23:05 已开盘
    assert ms.session_state("XAU", _ts(2026, 11, 16, 22, 5)) == "closed"
    assert ms.session_state("WTI", _ts(2026, 11, 16, 22, 5)) == "closed"
    assert ms.session_state("XAU", _ts(2026, 11, 16, 23, 5)) == "open"


def test_weekend_closed_and_crypto_always_open():
    assert ms.session_state("XAU", _ts(2026, 11, 15, 10)) == "closed"   # 周日 10:00 UTC
    assert ms.session_state("XAU", _ts(2026, 11, 13, 23)) == "closed"   # 周五 23:00 UTC
    assert ms.session_state("BTC", _ts(2026, 11, 15, 10)) == "open"
    assert ms.session_state("ETH", _ts(2026, 11, 13, 23)) == "open"


def test_unknown_asset_not_guessed():
    assert ms.session_state("NVDA", _ts(2026, 11, 16, 22, 5)) == "unknown"


def test_hard_status_market_closed_instead_of_stale():
    closed = _ts(2026, 11, 16, 22, 5)
    opened = _ts(2026, 11, 16, 23, 5)
    assert dp.hard_status("XAU", "", closed) == "missing:market_closed"
    assert dp.hard_status("XAU", "", opened) == "missing:feed_stale"


def test_bucket_label_is_unresolved_not_no_feed():
    assert dp.hard_status("CN_STOCK", "") == "missing:unresolved_target"
    assert dp.hard_status("INDIVIDUAL_STOCK", "") == "missing:unresolved_target"
    assert dp.hard_status("ETF", "") == "missing:no_local_feed"


def test_real_ticker_without_feed_is_no_feed():
    assert dp.hard_status("NVDA", "") == "missing:no_local_feed"
    assert dp.hard_status("600519.SH", "") == "missing:no_local_feed"


def test_error_code_mapping_for_new_statuses():
    from review.reflection_checkers import _classify_missing_hard_evidence as f
    code, sub, note = f("missing:market_closed")
    assert (code, sub) == ("E001j", "missing_market_closed")
    assert "休市" in note
    assert f("missing:unresolved_target")[0] == "E001i"
    assert f("missing:market_closed")[0] != f("missing:feed_stale")[0]
