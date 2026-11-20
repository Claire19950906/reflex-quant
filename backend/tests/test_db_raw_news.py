"""raw_news 入库边界用例（时间戳归一 + 重复筛 + 门卫标签落库）。

为什么盯这一层：`raw_news` 是整条链的入口，它写坏一次，后面所有统计都在错的基础上算：
- `timestamp` 一列曾混存 ISO 文本 / epoch 字符串 / `YYYY-MM-DD HH:MM:SS` 三种格式，
  SQL 里 `now - timestamp` 对 ISO 行按 0 算 → 实测跑出「积压 49 万小时」的假数；
- 重复快讯被多源转发，不筛掉就烧门卫额度、把真快讯挤在队尾；
- 门卫标签里的主品种写成中文「黄金」，下游只认代码 → 整条走完主分析仍判 NONE。

用内存库 + `migrate()` 跑真实表结构，不碰演示库/生产库。
"""

import sqlite3
import time

import pytest

import db


@pytest.fixture(scope="module")
def _schema_conn():
    """建一次表结构就够了 —— migrate() 约 1.3 秒，每个用例重建会把 19 条拖成 25 秒。"""
    c = sqlite3.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture()
def conn(_schema_conn):
    _schema_conn.execute("DELETE FROM raw_news")   # 每个用例从空表开始
    return _schema_conn


def _row(conn, rid, *cols):
    sql = "SELECT " + ", ".join(cols) + " FROM raw_news WHERE id=?"
    return conn.execute(sql, (rid,)).fetchone()


BASE = 1_700_000_000


# ── 表结构 ────────────────────────────────────────────────────────────────

def test_migrate_gives_raw_news_all_boundary_columns(conn):
    cols = db.table_columns(conn, "raw_news")
    assert {"timestamp", "ts", "published_at", "content_fingerprint",
            "first_seen_ts", "filter_reason", "gate_main_asset", "gate_unavailable"} <= cols


# ── 时间戳归一 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("timestamp,ts,expect", [
    ("2026-11-17T14:06:40+08:00", None, db.coerce_epoch_ts("2026-11-17T14:06:40+08:00")),
    (str(BASE), None, BASE),
    ("2026-11-17 14:06:40", None, db.coerce_epoch_ts("2026-11-17 14:06:40")),
])
def test_timestamp_always_lands_as_epoch_string(conn, timestamp, ts, expect):
    rid = db.insert_raw_news(conn, source="s", content=f"内容{timestamp}", timestamp=timestamp, ts=ts)
    got_ts_str, got_ts = _row(conn, rid, "timestamp", "ts")
    assert got_ts_str == str(expect) and got_ts == expect


def test_explicit_ts_wins_over_timestamp_text(conn):
    rid = db.insert_raw_news(conn, source="s", content="x",
                             timestamp="2026-11-17T14:06:40+08:00", ts=BASE)
    assert _row(conn, rid, "ts")[0] == BASE


def test_missing_time_falls_back_to_now(conn):
    rid = db.insert_raw_news(conn, source="s", content="无时间", timestamp="")
    (t,) = _row(conn, rid, "ts")
    assert abs(int(t) - int(time.time())) < 300


# ── 链接 ──────────────────────────────────────────────────────────────────

def test_url_is_extracted_from_content_when_absent(conn):
    rid = db.insert_raw_news(conn, source="s", content="快讯正文 https://example.com/a 后续", timestamp=str(BASE))
    assert _row(conn, rid, "url")[0] == "https://example.com/a"


def test_explicit_url_beats_content_extraction(conn):
    rid = db.insert_raw_news(conn, source="s", content="正文 https://example.com/a",
                             timestamp=str(BASE), url="https://given.example/b")
    assert _row(conn, rid, "url")[0] == "https://given.example/b"


def test_no_url_stays_empty(conn):
    rid = db.insert_raw_news(conn, source="s", content="没有链接的正文", timestamp=str(BASE))
    assert _row(conn, rid, "url")[0] == ""


# ── 重复筛（零成本，入库前） ─────────────────────────────────────────────

def test_duplicate_content_is_filtered_with_reason(conn):
    db.insert_raw_news(conn, source="a", content="白银大涨 3% 突破关键位", timestamp=str(BASE), ts=BASE)
    rid = db.insert_raw_news(conn, source="b", content="白银大涨 3% 突破关键位", timestamp=str(BASE + 600))
    status, is_noise, reason, first_seen = _row(conn, rid, "status", "is_noise", "filter_reason", "first_seen_ts")
    assert status == "FILTERED" and is_noise == 1
    assert reason == "dup_fingerprint"
    # 同簇的最早采集时刻必须指向第一条，不是自己
    assert first_seen == str(BASE)


def test_punctuation_and_case_differences_still_count_as_duplicate(conn):
    db.insert_raw_news(conn, source="a", content="Fed cuts rates 25bp", timestamp=str(BASE))
    rid = db.insert_raw_news(conn, source="b", content="fed  cuts rates，25bp！", timestamp=str(BASE + 60))
    assert _row(conn, rid, "status")[0] == "FILTERED"


def test_different_magnitude_is_not_a_duplicate(conn):
    db.insert_raw_news(conn, source="a", content="加息25个基点", timestamp=str(BASE))
    rid = db.insert_raw_news(conn, source="b", content="加息50个基点", timestamp=str(BASE + 60))
    assert _row(conn, rid, "status")[0] == "PENDING"


def test_already_filtered_rows_skip_the_prefilter(conn):
    rid = db.insert_raw_news(conn, source="a", content="重复内容", timestamp=str(BASE),
                             ts=BASE, status="FILTERED")
    assert _row(conn, rid, "status", "filter_reason") == ("FILTERED", "")
    # 已知口径（不是期望，是记录）：重复判定只比对**非 FILTERED** 的历史行，
    # 所以"第一条自己就是 FILTERED"时，后来的同内容不会被判重复 → 仍走 PENDING。
    rid2 = db.insert_raw_news(conn, source="b", content="重复内容", timestamp=str(BASE + 1))
    assert _row(conn, rid2, "status")[0] == "PENDING"


# ── 门卫标签落库 ─────────────────────────────────────────────────────────

def test_gate_fields_are_persisted(conn):
    rid = db.insert_raw_news(
        conn, source="s", content="美联储利率决议落地", timestamp=str(BASE),
        gate={"core_impact": "利率路径", "is_material": True, "assets": ["黄金", "XAU"],
              "main_asset": "黄金", "kind": "macro", "timing": "now", "strength": 3,
              "verdict": "BUY", "impact_1to100": 70})
    core, material, assets, main_asset, strength, verdict, impact = _row(
        conn, rid, "gate_core_impact", "gate_is_material", "gate_assets",
        "gate_main_asset", "gate_strength", "gate_verdict", "gate_impact_1to100")
    assert core == "利率路径" and material == 1
    assert assets == '["黄金", "XAU"]'
    # 主品种必须归一成代码，否则下游「按 main_asset 填池」认不出来
    assert main_asset == "XAU"
    assert strength == 3 and verdict == "BUY" and impact == 70


def test_gate_unavailable_keeps_three_states(conn):
    # 0 正常 / 1 冻结待回放 / 2 降级放行 —— 压成布尔会把放行行冻死
    for flag in (0, 1, 2):
        rid = db.insert_raw_news(conn, source="s", content=f"标记{flag}", timestamp=str(BASE + flag),
                                 gate_unavailable=flag)
        assert _row(conn, rid, "gate_unavailable")[0] == flag


def test_no_gate_leaves_gate_columns_neutral(conn):
    rid = db.insert_raw_news(conn, source="s", content="无门卫", timestamp=str(BASE))
    assert _row(conn, rid, "gate_unavailable", "gate_main_asset") == (0, "")


# ── 其它边界 ─────────────────────────────────────────────────────────────

def test_published_at_and_score_are_stored(conn):
    rid = db.insert_raw_news(conn, source="s", content="正文", timestamp=str(BASE),
                             published_at="2026-11-17 14:06:40", relevance_score=0.9, is_noise=1)
    published_at, score, is_noise = _row(conn, rid, "published_at", "relevance_score", "is_noise")
    assert published_at == "2026-11-17 14:06:40"
    assert score == pytest.approx(0.9) and is_noise == 1


def test_long_published_at_is_truncated(conn):
    rid = db.insert_raw_news(conn, source="s", content="正文", timestamp=str(BASE),
                             published_at="2026" * 100)
    assert len(_row(conn, rid, "published_at")[0]) == 64


def test_two_distinct_news_both_stay_pending(conn):
    r1 = db.insert_raw_news(conn, source="s", content="美联储降息", timestamp=str(BASE))
    r2 = db.insert_raw_news(conn, source="s", content="欧洲央行加息", timestamp=str(BASE + 60))
    assert _row(conn, r1, "status")[0] == "PENDING"
    assert _row(conn, r2, "status")[0] == "PENDING"
    assert conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0] == 2
