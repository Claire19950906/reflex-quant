"""v170 全网溯源弱信号抽取（Fallback 2.5）单测。

覆盖 _extract_cross_net_from_text 的 4 个时间模式、14 个来源模式、跨年保护、空字符串、坏格式。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine.ai_worker import _extract_cross_net_from_text


# ─── 时间抽取 ───


def test_ts_yyyymmdd_with_colon():
    cn_ts, _, _ = _extract_cross_net_from_text(
        "美联储在 2026年9月19日 22:00 紧急宣布降息50个基点"
    )
    assert cn_ts.startswith("2026-09-19 22:00:00")


def test_ts_yyyymmdd_with_chi():
    cn_ts, _, _ = _extract_cross_net_from_text(
        "美联储2026年9月19日22时00分宣布降息"
    )
    assert cn_ts.startswith("2026-09-19 22:00:00")


def test_ts_dash_format():
    cn_ts, _, _ = _extract_cross_net_from_text("据报道 2026-09-19 22:00 路透社消息")
    assert cn_ts.startswith("2026-09-19 22:00:00")


def test_ts_yyyymmdd_only():
    cn_ts, _, _ = _extract_cross_net_from_text("2026年9月19日发生了一件大事")
    assert cn_ts.startswith("2026-09-19 00:00:00")


def test_ts_short_md_hm():
    """M月D日HH:MM 格式 → 跨年保护（年份补当前年；若月日比今天晚 30+ 天回退一年）"""
    import datetime
    now = datetime.datetime.now()
    if now.month == 1:
        # 1 月时「12月X日」会触发跨年回退：months=12 > 当前月 1 → 跨年
        cn_ts, _, _ = _extract_cross_net_from_text("12月25日 18:00 圣诞事件")
        # 跨年保护：当前年-1
        expected_year = now.year - 1
    else:
        cn_ts, _, _ = _extract_cross_net_from_text("1月5日 18:00 测试事件")
        # 非跨年：当年
        expected_year = now.year
    assert cn_ts.startswith(f"{expected_year:04d}-")


def test_ts_invalid_year_returns_empty():
    cn_ts, _, _ = _extract_cross_net_from_text("公元前1989年1月1日发生远古事件")
    assert cn_ts == ""


def test_ts_invalid_hour_returns_empty():
    cn_ts, _, _ = _extract_cross_net_from_text("2026年99月1日 12:00 异常月")
    assert cn_ts == ""


def test_ts_no_pattern_returns_empty():
    cn_ts, _, _ = _extract_cross_net_from_text("今天发生了某些事情，没有具体时间")
    assert cn_ts == ""


# ─── 来源抽取 ───


def test_src_official_news():
    _, cn_src, _ = _extract_cross_net_from_text("美联储官网新闻稿称...")
    assert "美联储" in cn_src


def test_src_official_announce():
    _, cn_src, _ = _extract_cross_net_from_text("沙特能源部宣布减产200万桶/日")
    assert cn_src in ("沙特能源部", "能源部") or "沙特能源部" in cn_src or "能源部" in cn_src


def test_src_jv_reported():
    _, cn_src, _ = _extract_cross_net_from_text("据路透社报道，英伟达财报超预期")
    assert "路透社" in cn_src


def test_src_xinhua():
    _, cn_src, _ = _extract_cross_net_from_text("新华社消息，今晚美联储降息")
    assert "新华社" in cn_src


def test_src_english_reuters():
    _, cn_src, _ = _extract_cross_net_from_text("Breaking: Reuters reports NVDA earnings beat")
    assert "Reuters" in cn_src


def test_src_no_match_returns_empty():
    _, cn_src, _ = _extract_cross_net_from_text("今天发生了一些乱七八糟的事情")
    assert cn_src == ""


def test_src_too_short_filtered():
    """'据'单字不应被当成来源"""
    _, cn_src, _ = _extract_cross_net_from_text("据报道，某地发生了某事")
    assert cn_src != "据"


# ─── net_origin_info 拼装 ───


def test_noi_assembled_with_both():
    _, _, noi = _extract_cross_net_from_text("美联储官网新闻稿 2026年9月19日 22:00 宣布降息")
    assert "美联储" in noi
    assert "2026-09-19" in noi
    assert len(noi) <= 200


def test_noi_assembled_with_only_src():
    _, _, noi = _extract_cross_net_from_text("美联储官网新闻稿称将降息")
    assert "美联储" in noi


def test_noi_empty_when_no_signal():
    _, _, noi = _extract_cross_net_from_text("今天天气不错")
    assert noi == ""


# ─── 边界 ───


def test_empty_string():
    assert _extract_cross_net_from_text("") == ("", "", "")


def test_none_input():
    assert _extract_cross_net_from_text(None) == ("", "", "")


def test_real_m3_pushong_long_text():
    """模拟 MiniMax-M3 八段推演长文 → 应至少抽到时间和来源"""
    long_text = """
    【事件溯源】
    根据路透社报道，2026年9月19日 22:00 美联储紧急宣布将联邦基金利率目标区间下调50个基点。
    这是2026年以来首次降息，标志着美联储货币政策的重大转向。
    美联储主席鲍威尔在新闻发布会上表示，通胀已显著缓和至2.1%。
    """
    cn_ts, cn_src, noi = _extract_cross_net_from_text(long_text)
    assert cn_ts.startswith("2026-09-19 22:00:00")
    assert "路透社" in cn_src or "美联储" in cn_src
    assert noi  # 非空