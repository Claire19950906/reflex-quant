"""us_release_schedule（FOMC 前瞻日程）单测。

2026-11-20: 这个模块在覆盖率表里是 0%。它不是"不重要"，是"没人敢测"——
要网络、要写库。其实拆开看只有三块：抠 HTML、算天数、写库。抠和算都是纯的，
写库打个桩就能测。

测的重点是真出过 bug 的那条：Fed 页面上 `(Released February 18, 2026)` 这种
"会议纪要发布日"是**单日期**，不是会议。早先版本把它当会议，2026 年凭空多出
一场议息，金价的前瞻窗口就提前了。
"""

import datetime as dt

import pytest

import us_release_schedule as urs


HTML_TWO_YEARS = """
<html><body>
<h2>2026 FOMC Meetings</h2>
<p>January 27-28</p>
<p>March 17-18*</p>
<p>(Released February 18, 2026)</p>
<p>April 28-29</p>
<p>June 16-17*</p>
<h2>2027 FOMC Meetings</h2>
<p>January 26-27</p>
<p>March 16-17*</p>
</body></html>
"""

HTML_ONLY_PAST = """
<h2>2026 FOMC Meetings</h2>
<p>January 27-28</p>
<p>March 17-18</p>
"""

HTML_EMPTY = "<html><body><p>页面结构变了，一个日期都没有</p></body></html>"


def test_parse_meetings_takes_second_day_of_range():
    parsed = urs.parse_meetings(HTML_TWO_YEARS)
    dates = [d for _, d in parsed]
    # 决议日 = 会议第二天
    assert dt.datetime(2026, 1, 28) in dates
    assert dt.datetime(2026, 3, 18) in dates
    assert dt.datetime(2026, 4, 29) in dates


def test_parse_meetings_ignores_single_date_release_notes():
    # "February 18" 是纪要发布日，不是会议 —— 不能被算成一场
    parsed = urs.parse_meetings(HTML_TWO_YEARS)
    assert dt.datetime(2026, 2, 18) not in [d for _, d in parsed]


def test_parse_meetings_splits_years_correctly():
    parsed = urs.parse_meetings(HTML_TWO_YEARS)
    years = {y for y, _ in parsed}
    assert 2026 in years and 2027 in years
    # 2027 场次不能被截断（早先按字符数切窗口会丢下半年）
    assert dt.datetime(2027, 1, 27) in [d for _, d in parsed]
    assert dt.datetime(2027, 3, 17) in [d for _, d in parsed]


def test_parse_meetings_empty_html_returns_nothing():
    assert urs.parse_meetings(HTML_EMPTY) == []


def test_parse_meetings_dedupes_same_meeting():
    # 同一场被解析成两个相邻日期 → 只留靠后的（决议日）
    html = "<h2>2026 FOMC Meetings</h2><p>January 27-28</p><p>January 28-29</p>"
    parsed = urs.parse_meetings(html)
    assert len(parsed) == 1
    assert parsed[0][1] == dt.datetime(2026, 1, 29)


def _patch_fetch(monkeypatch, html):
    monkeypatch.setattr(urs, "fetch_html", lambda timeout=20: html)


def test_main_dry_run_does_not_write(monkeypatch):
    _patch_fetch(monkeypatch, HTML_TWO_YEARS)
    calls = []
    monkeypatch.setattr(urs.external_data, "ingest_external",
                        lambda **kw: calls.append(kw))
    assert urs.main(["--dry-run"]) == 0
    assert calls == []


def test_main_writes_two_indicators(monkeypatch):
    _patch_fetch(monkeypatch, HTML_TWO_YEARS)
    calls = []
    monkeypatch.setattr(urs.external_data, "ingest_external",
                        lambda **kw: calls.append(kw))
    assert urs.main([]) == 0
    assert len(calls) == 2
    assert {c["indicator"] for c in calls} == {"fomc_days_to_next", "fomc_meetings_90d"}
    # 天数必须是非负整数，且 meta 带下一次日期
    days = calls[0]["records"][0]["value"]
    assert days >= 0 and days == int(days)
    assert calls[0]["meta"]["next_meeting"]


def test_main_returns_1_when_nothing_parsed(monkeypatch):
    _patch_fetch(monkeypatch, HTML_EMPTY)
    assert urs.main([]) == 1


def test_main_returns_1_when_no_upcoming(monkeypatch):
    _patch_fetch(monkeypatch, HTML_ONLY_PAST)
    assert urs.main([]) == 1


def test_fetch_html_raises_after_three_failures(monkeypatch):
    class Fail:
        returncode = 35
        stdout = ""
        stderr = "TLS handshake failed"

    monkeypatch.setattr(urs.subprocess, "run", lambda *a, **k: Fail())
    monkeypatch.setattr(urs.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as exc:
        urs.fetch_html()
    assert "curl 失败" in str(exc.value)
