# -*- coding: utf-8 -*-
"""本轮新增（溯源 / 三值锚点 / 报告期）离线单测。

覆盖：
  - db.content_fingerprint 归一化等同 + 量级区分
  - db.insert_raw_news 的全网 first_seen_ts（同指纹收敛到最早）
  - event_struct.extract_data_points 三值锚点
  - macro_context.report_date_of / _as_of_suffix 报告期提取（no-lookahead 入口）
"""

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from db import content_fingerprint, insert_raw_news, migrate
from event_struct import extract_data_points, to_data_points_block


def test_content_fingerprint_normalizes_and_distinguishes_magnitude():
    a = content_fingerprint("[hash:abc123] 美联储宣布加息 25 个基点")
    b = content_fingerprint("美联储宣布加息25个基点")
    c = content_fingerprint("美联储宣布加息 50 个基点")
    assert a == b          # 空格 / 标点差异 → 同簇
    assert a != c          # 量级不同 → 不同簇


def test_extract_data_points_grabs_three_values():
    pts = extract_data_points("美国 10 月 CPI 同比上涨 3.2%，高于预期 3.0%，前值 3.1%")
    assert pts["actual"] == 3.2      # 兜底：同比上涨 3.2% 无「实际值」锚词
    assert pts["forecast"] == 3.0
    assert pts["prev"] == 3.1
    assert to_data_points_block(pts) != ""
    # 无 actual 锚词且无同比/环比时，actual 标 None（数据不全标 N/A）
    pts2 = extract_data_points("非农新增就业 15.6 万人，预期 16 万人，前值 15 万人")
    assert pts2["actual"] is None


def test_insert_raw_news_tracks_first_seen_to_earliest():
    conn = sqlite3.connect(":memory:")
    try:
        migrate(conn)
        insert_raw_news(conn, source="jin10",
                        content="[hash:a] 美联储宣布加息 25 个基点",
                        timestamp="2026-09-16 10:00:00", is_noise=0, relevance_score=0.9)
        insert_raw_news(conn, source="reuters",
                        content="美联储宣布加息25个基点",
                        timestamp="2026-09-16 09:30:00", is_noise=0, relevance_score=0.9)
        rows = conn.execute(
            "SELECT timestamp, first_seen_ts, content_fingerprint FROM raw_news ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        # 同指纹
        assert rows[0][2] == rows[1][2] != ""
        # 第二条更早（09:30），first_seen 收敛到 09:30
        assert rows[1][1] == "2026-09-16 09:30:00"
        # 回写：更早行入库后，旧行（10:00）的 first_seen 也被拉平到最早 09:30
        assert rows[0][1] == "2026-09-16 09:30:00"
    finally:
        conn.close()


def test_report_date_of_extracts_obs_period():
    import macro_context
    assert macro_context.report_date_of({"payload": {"observation_date": "2026-09-14"}}) == "2026-09-14"
    assert macro_context.report_date_of({"payload": {"report_date": "2026-09-11"}}) == "2026-09-11"
    assert macro_context.report_date_of({"payload": {"observation_period": "2026-M07"}}) == "2026-M07"
    assert macro_context.report_date_of({"payload": {}}) == ""
    assert macro_context._as_of_suffix({"payload": {"as_of": "2026-09-10"}}) == "@2026-09-10"
    # 归一化：BLS observation_period '2026-M07' → '2026-07'
    assert macro_context._as_of_suffix({"payload": {"observation_period": "2026-M07"}}) == "@2026-07"
    # no-lookahead：未来日期标注防前视
    assert macro_context._as_of_suffix({"payload": {"observation_date": "2999-01-01"}}).endswith("⚠未来(勿用)")