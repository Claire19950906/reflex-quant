# -*- coding: utf-8 -*-
"""Crowd Subjects 回归测试（v319「具体话题」层）。

覆盖本轮修复的每一个缺陷，全部离线（内存库，不碰生产库）：

  P0  **行序一致性**：候选集必须与主循环的行严格同序同条。
      旧实现里 build_subject_map 自己查一遍 title（无 ORDER BY），
      而主循环查 ORDER BY collected_at DESC, id DESC ——
      两次查询行序不保证一致，挖掘短语的提及数会算到别的条目上。

  P1  **冷热路径口径一致**：首次请求与缓存命中返回的主体数量必须相同。
      旧实现冷路径不截断（345 条）、热路径截断（40 条）。

  P2  **缓存键隔离**：min_mentions / item_limit 不同的请求不能命中同一份缓存。

  P3  **词典主体不被拆分、不漏匹配**：
      "三季报" 必须归并成一张卡；"英特尔"必须能在正文里匹配到。

  P4  **结构化主体生效**：THS 个股名/概念、gnews #话题标签#、
      百度财经"公司名：内容"前缀，都要成为主体。

  P5  **噪音过滤**：市场状态词（收官/预期/降幅/高开…）不能成为主体；
      说话人/栏目（"专家：…"/"业内：…"）不能被当成公司。

  P6  **权威主体豁免门槛**：THS 热榜里只上榜 1 次的个股也是有效主体，
      不该按讨论频次（min_mentions）被砍掉。

  P7  **分类正确**：省份归"地区"、政府机构归"机构"，不该一律"公司"。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import crowd_subjects as csub


# ─────────────────── 构造内存库 ───────────────────

SCHEMA = """
CREATE TABLE crowd_source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, external_id TEXT, item_kind TEXT, title TEXT, body TEXT,
    url TEXT, media_channel TEXT, heat REAL, rank_no INTEGER,
    published_at TEXT, collected_at TEXT, day TEXT, month TEXT,
    topic_key TEXT, meta_json TEXT
);
"""


def _mk(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO crowd_source_items (id, source, title, body, url, heat, "
        "published_at, collected_at, day, month, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _row(i, source, title, day="2026-11-01", heat=100.0, meta=None, body=""):
    return (i, source, title, body, "http://x/%d" % i, heat,
            day + " 09:00:00", day + " 09:00:00", day, day[:7],
            json.dumps(meta or {}, ensure_ascii=False))


@pytest.fixture()
def conn():
    rows = [
        # THS 热榜：个股名 + 概念标签（结构化信号）
        _row(1, "ths_hot", "002407 多氟多", meta={
            "name": "多氟多", "market": "深A", "change_pct": 1.0,
            "concepts": ["氟化工概念", "人形机器人"]}),
        _row(2, "ths_hot", "600241 时代万恒", meta={
            "name": "时代万恒", "market": "沪A", "change_pct": 10.0,
            "concepts": ["国企改革", "锂电池概念"]}),
        # gnews：#话题标签#
        _row(3, "gnews", "美股讯号 | 英特尔盘前重挫超26% ｜#美股资讯# #英特尔# - 搜狐网"),
        _row(4, "gnews", "苹果发布新品 ｜#美股资讯# #苹果# - 搜狐网"),
        # 百度财经："公司名：内容" 前缀
        _row(5, "baidu_finance", "博雅生物：前三季度整体采浆量518.9吨"),
        _row(6, "baidu_finance", "专家：后市或将震荡"),
        _row(7, "baidu_finance", "广东：工业增加值回升"),
        # 普通新闻：状态词应被过滤
        _row(8, "baidu_finance", "A股今日收官，三季报陆续披露"),
        _row(9, "gnews", "市场预期稳定，估值回升，创业板指高开"),
        _row(10, "gnews", "英伟达与英特尔同时大涨，光模块板块爆发"),
    ]
    c = _mk(rows)
    yield c
    c.close()


# ─────────────────── P0 行序一致性 ───────────────────

def test_候选集与主循环行序一致(conn):
    """build_subject_map 必须复用主循环的行，而不是自己再查一遍。"""
    rows = conn.execute(
        "SELECT id, source, title, body, meta_json FROM crowd_source_items "
        "WHERE day BETWEEN ? AND ? ORDER BY collected_at DESC, id DESC",
        ("2026-11-01", "2026-11-01")).fetchall()
    structured = csub._structured_entities(rows)
    subj, per_item_cands, _mined, _sa = csub.build_subject_map(
        conn, "2026-11-01", "2026-11-01", min_docs=1, rows=rows,
        structured=structured)
    assert len(per_item_cands) == len(rows), "候选集长度必须与 rows 一致"

    # 候选集内容必须来自**同一条**标题：逐条核对
    for i, r in enumerate(rows):
        t = csub._normalize_title(r["title"] or "")
        if not t:
            continue
        blocks = csub._block_spans(t)
        expected = set(csub._candidates_cn(t, blocks))
        expected.update(csub._candidates_en(t))
        assert per_item_cands[i] == expected, "第 %d 行候选集错位" % i


def test_挖掘短语命中归属正确(conn):
    """一条提到"光模块"的标题，光模块的提及必须记在这一条上。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"]: c for c in data["topics"]}
    assert "光模块" in names, "光模块应被识别"
    card = names["光模块"]
    assert card["mentions"] == 1, "光模块只该在标题命中的那一条上计一次"
    assert card["items"][0]["title"].startswith("英伟达与英特尔")


# ─────────────────── P1 冷热路径口径 ───────────────────

def test_冷热路径返回数量一致(conn):
    csub._CACHE.clear()
    cold = csub.subject_overview_cached(conn, days=1, limit=5)
    warm = csub.subject_overview_cached(conn, days=1, limit=5)
    assert cold["count"] == warm["count"], "冷热路径口径不一致"
    assert cold["count"] == len(cold["topics"])
    assert len(cold["topics"]) <= 5, "必须按 limit 截断"


def test_冷路径也做排序截断(conn):
    csub._CACHE.clear()
    d = csub.subject_overview_cached(conn, days=1, limit=3)
    assert len(d["topics"]) == 3
    # rank_no 必须连续且从 1 开始
    assert [c["rank_no"] for c in d["topics"]] == [1, 2, 3]


# ─────────────────── P2 缓存键隔离 ───────────────────

def test_缓存键按min_mentions隔离(conn):
    csub._CACHE.clear()
    csub.subject_overview_cached(conn, days=1, limit=50, min_mentions=1)
    csub.subject_overview_cached(conn, days=1, limit=50, min_mentions=99)
    assert len(csub._CACHE) == 2, "不同 min_mentions 必须用不同缓存键"

    # 注意：count 是**截断后**的数量，两次都是 15（limit=50 但只有 15 个主体）。
    # 要验证的是缓存内容随阈值变化 —— 读缓存里的全量卡片。
    cached = list(csub._CACHE.values())
    n_lo = len(cached[0][1]["topics"])
    n_hi = len(cached[1][1]["topics"])
    assert n_lo >= n_hi, "min_mentions 高 → 全量主体数应不更多"
    # 本样本里的主体都是权威主体（豁免门槛），所以两者应相等；
    # 若不等也必须单调（高阈值不会给出更多主体）。


def test_缓存键按item_limit隔离(conn):
    csub._CACHE.clear()
    csub.subject_overview_cached(conn, days=1, limit=50, item_limit=1)
    csub.subject_overview_cached(conn, days=1, limit=50, item_limit=5)
    assert len(csub._CACHE) == 2, "不同 item_limit 必须用不同缓存键"


# ─────────────────── P3 词典主体 ───────────────────

def test_词典主体归并且不拆分(conn):
    """多来源提到的同一主体必须归到一张卡，提及数累加。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "三季报" in names
    card = next(c for c in data["topics"] if c["display_name"] == "三季报")
    assert card["mentions"] == 1, "三季报只在一条里出现"
    assert card["topic_kind"] == "事件"


def test_词典主体在正文中被匹配到(conn):
    """英特尔出现在两条标题里，两处都要计入。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"]: c for c in data["topics"]}
    assert "英特尔" in names, "英特尔必须被识别"
    assert names["英特尔"]["mentions"] >= 2, "英特尔在两条标题里出现"


def test_别名归一到规范名(conn):
    """#英特尔# 标签与正文里的"英特尔"必须归到同一张卡。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "英特尔" in names
    # 不能同时存在 "Intel" 这样的别名牌
    assert not any(n.lower() == "intel" for n in names)


# ─────────────────── P4 结构化主体 ───────────────────

def test_ths个股名成为主体(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "多氟多" in names, "THS 个股名应成为主体"
    assert "时代万恒" in names


def test_ths概念标签成为主体(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    kinds = {c["display_name"]: c["topic_kind"] for c in data["topics"]}
    assert "氟化工概念" in kinds, "THS 概念标签应成为主体"
    assert kinds["氟化工概念"] == "概念"


def test_gnews话题标签成为主体(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "苹果" in names, "#苹果# 标签应成为主体"


def test_百度财经公司名前缀成为主体(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "博雅生物" in names, "「公司名：内容」前缀应成为主体"


# ─────────────────── P5 噪音过滤 ───────────────────

def test_市场状态词不成为主体(conn):
    """收官/预期/估值/高开 描述的是行情，不是"大家在聊什么"。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    for noise in ("收官", "预期", "估值", "高开", "稳定", "大盘"):
        assert noise not in names, "状态词 %r 不该成为主体" % noise


def test_说话人前缀不被当成公司(conn):
    """"专家：…" 的前缀是说话人，不是主体。"""
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    names = {c["display_name"] for c in data["topics"]}
    assert "专家" not in names, "说话人不该成为主体"


# ─────────────────── P6 权威主体豁免门槛 ───────────────────

def test_权威主体豁免min_mentions(conn):
    """THS 个股名只出现 1 次，但它是数据源确认过的主体，不该被砍掉。"""
    data = csub.subject_overview(conn, days=1, min_mentions=5)
    names = {c["display_name"] for c in data["topics"]}
    assert "多氟多" in names, "权威主体应豁免 min_mentions"
    assert "时代万恒" in names


def test_非权威低频主体被门槛过滤(conn):
    """挖掘出的低频长尾仍走 min_mentions 门槛。"""
    data = csub.subject_overview(conn, days=1, min_mentions=50)
    names = {c["display_name"] for c in data["topics"]}
    assert "多氟多" in names, "权威主体豁免"
    # 频次门槛确实生效：非权威主体不该以低频次留在列表里
    for c in data["topics"]:
        if c["topic_kind"] == "议题":
            assert c["mentions"] >= 50, \
                "%s 提及 %d 低于门槛却未被豁免" % (c["display_name"], c["mentions"])


# ─────────────────── P7 分类正确 ───────────────────

def test_省份归地区政府机构归机构(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    kinds = {c["display_name"]: c["topic_kind"] for c in data["topics"]}
    if "广东" in kinds:
        assert kinds["广东"] == "地区", "省份不该标成公司"
    if "统计局" in kinds:
        assert kinds["统计局"] == "机构", "政府机构不该标成公司"


def test_公司分类正确(conn):
    data = csub.subject_overview(conn, days=1, min_mentions=1)
    kinds = {c["display_name"]: c["topic_kind"] for c in data["topics"]}
    assert kinds.get("博雅生物") == "公司"


# ─────────────────── 结构化抽取单元测试 ───────────────────

def test_结构化抽取按行对齐():
    rows = [
        {"source": "ths_hot", "title": "002407 多氟多",
         "meta_json": json.dumps({"name": "多氟多", "concepts": ["人形机器人"]},
                                 ensure_ascii=False)},
        {"source": "gnews", "title": "x ｜#英特尔# y",
         "meta_json": "{}"},
        {"source": "baidu_finance", "title": "专家：后市震荡", "meta_json": "{}"},
    ]
    kinds, per_item = csub._structured_entities(rows)
    assert "多氟多" in kinds and kinds["多氟多"] == "个股"
    assert "人形机器人" in kinds and kinds["人形机器人"] == "概念"
    assert "英特尔" in kinds and kinds["英特尔"] == "话题"
    assert "专家" not in kinds, "说话人不是主体"
    assert "多氟多" in per_item[0]
    assert "英特尔" in per_item[1]
    assert not per_item[2]


def test_通用词判定():
    assert csub._is_generic_name("美股资讯")
    assert csub._is_generic_name("A股收评")
    assert csub._is_generic_name("经济日报")
    assert not csub._is_generic_name("英特尔")
    assert not csub._is_generic_name("人形机器人")


def test_前缀主体分类():
    assert csub._prefix_subject_kind("博雅生物") == "公司"
    assert csub._prefix_subject_kind("广东") == "地区"
    assert csub._prefix_subject_kind("统计局") == "机构"
    assert csub._prefix_subject_kind("专家") is None
    assert csub._prefix_subject_kind("业内") is None
