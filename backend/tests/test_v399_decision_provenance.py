# -*- coding: utf-8 -*-
"""v399 回归：决策来源留痕（asset_route / hard_data_status）+ S20 指标。"""
from __future__ import annotations

import calendar
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import capability_scorecard as cs
from signals import decision_provenance as dp


# 时钟钉死：hard_status 在 v409 之后会看"决策时刻是否休市"。
# 不钉死就会周末跑挂（周六 CME 全关 → market_closed，而用例期待 feed_stale）。
_OPEN_TS = calendar.timegm((2026, 11, 18, 12, 0, 0))    # 周三 12:00 UTC，CME 开市
_CLOSED_TS = calendar.timegm((2026, 11, 21, 12, 0, 0))  # 周六 12:00 UTC，CME 休市


def _metric(mid):
    return [m for m in cs.METRICS if m.id == mid][0]


def test_route_tag_none_and_decided():
    assert dp.route_tag("NONE") == "target_none"
    assert dp.route_tag("") == "target_none"
    assert dp.route_tag("btc") == "decided:crypto"
    assert dp.route_tag("XAU") == "decided:metal"
    assert dp.route_tag("CN_STOCK") == "decided:stock"


def test_hard_status_buckets():
    assert dp.hard_status("BTC", "BTC 20:13 close=63980") == "ok"
    assert dp.hard_status("NONE", "") == "missing:none_target"
    # v409 改了桶口径：CN_STOCK/US_STOCK/HK_STOCK 这类桶标签不是"本地没有源"，
    # 而是"公司认出来了、新闻里没解析出代码" → unresolved_target。
    # 两者修法不同（前者要接数据源，后者要补代码解析），所以不能混成一个桶。
    assert dp.hard_status("CN_STOCK", "") == "missing:unresolved_target"
    # 开市时段取不到价 = 断档；休市时段没价是市场事实，两者必须分开
    assert dp.hard_status("XAU", "", ts=_OPEN_TS) == "missing:feed_stale"
    assert dp.hard_status("XAU", "", ts=_CLOSED_TS) == "missing:market_closed"
    # v406: SKHYNIX 现在能反解成韩股代码 000660.KS（asset_kinds 的别名表），
    # 所以状态不再是"符号不认识"，而是"标的真实存在、本地没接韩股源"。
    # 这比笼统的 unknown_symbol 更准确，也直接指出缺口在"接源"而非"解析"。
    assert dp.hard_status("SKHYNIX", "") == "missing:no_local_feed"

    # N/A 视为空，不能算 ok
    assert dp.hard_status("BTC", "N/A", ts=_OPEN_TS) == "missing:feed_stale"
    # 7×24 品种不存在"休市"，周末也必须算断档
    assert dp.hard_status("BTC", "", ts=_CLOSED_TS) == "missing:feed_stale"


def test_tags_pair():
    assert dp.tags("WTI", "", ts=_OPEN_TS) == ("decided:oil", "missing:feed_stale")


def test_s20_hard_data_share():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ai_decisions(id INTEGER, target_asset TEXT, "
                 "hard_data_evidence TEXT, hard_data_status TEXT, analysis_degraded INTEGER)")
    conn.executemany("INSERT INTO ai_decisions VALUES (?,?,?,?,?)", [
        (1, "BTC", "close=1", "ok", 0),
        (2, "XAU", "", "missing:feed_stale", 0),
        (3, "NONE", "", "missing:none_target", 1),     # 降级不进分母
    ])
    res = cs.evaluate_metric(conn, _metric("S20"))
    assert res.n == 2
    assert abs(res.value - 50.0) < 0.2
    assert res.status == cs.ST_THIN


def test_registry_has_s20_unique():
    ids = [m.id for m in cs.METRICS]
    assert len(ids) == len(set(ids))
    assert "S20" in ids
