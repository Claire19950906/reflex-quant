# -*- coding: utf-8 -*-
"""因果链评估器的判定层契约测试（全离线，不碰生产库）。

这层为什么值得单独锁：
`causal_chain_evaluator` 决定"哪条因果链是对的、哪条是错的"，它的结论会被
`causal_chains.hit/miss` 与后续置信度校准直接消费。判定层里有一个方向写反，
系统会**非常自信地把错的链喂给 AI**，而报告里看不出任何异常。

三条最容易复发的历史缺陷，这里逐条钉死：
  1. 用 effect（复盘结论）推方向 = 数据泄漏，hit rate 虚高；必须只读 name
  2. 用 mechanism 词表补方向：v322 实测命中率 41.9%，低于只信 name 的 50%，
     已回滚 —— 现在 mechanism 参数保留但**故意不参与**推断
  3. 触发词命中只靠 ticker/短词（buy/sell）会让所有链同时命中，量被放大

其余锁的是"同一件事"的判定口径（长稿 vs 短讯用包含率、不用 Jaccard）、
分位数插值、以及评估窗口（脉冲 1 根 bar / 趋势 4 根 bar）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import causal_chain_evaluator as cce  # noqa: E402


# ── 关键词切分 / 方向推断 ─────────────────────────────────────────────────

def test_split_keywords_trims_and_drops_empties():
    assert cce._split_keywords("") == []
    assert cce._split_keywords(None) == []
    assert cce._split_keywords(" 霍尔木兹 , 油轮 ,,停火 ") == ["霍尔木兹", "油轮", "停火"]


def test_infer_direction_never_reads_mechanism():
    # v322 的教训：机制文本里的涨跌词是"相对别的资产"说的，纯词表会把方向搞反。
    # 这条断言就是防止有人"顺手"把 mechanism 再喂进推断。
    assert cce._infer_direction("地缘冲突→贵金属", "推升 买盘 流入") == "BOTH"
    assert cce._infer_direction("地缘冲突→贵金属") == "BOTH"


def test_infer_direction_neutral_words_win_over_single_char_direction_words():
    # "震荡" 出现就判双向，不能被同句里的 "涨" 抢走方向
    assert cce._infer_direction("原油震荡上行") == "BOTH"
    assert cce._infer_direction("走势不明朗 双向") == "BOTH"


def test_infer_direction_action_words_beat_conclusion_words():
    # 操作词优先于结论词：句子里有 "利空"（触发原因），但结论是"做多"
    assert cce._infer_direction("美俄石油交易利空→WTI做多") == "UP"
    # 多个操作词时取**最后**出现的那个（后者才是最终结论）
    assert cce._infer_direction("先做多后做空 SELL") == "DOWN"


def test_infer_direction_falls_back_to_conclusion_word_count():
    assert cce._infer_direction("金价走弱 承压 回落") == "DOWN"
    assert cce._infer_direction("降息预期 提振 黄金") == "UP"
    assert cce._infer_direction("") == "BOTH"


def test_direction_from_mechanism_is_conditional_and_honest():
    assert cce._direction_from_mechanism("买盘涌入推升价格") == "UP"
    assert cce._direction_from_mechanism("卖压抛售压低价格") == "DOWN"
    # 条件性链条（若…则涨；若…则跌）必须承认自己判不出，不能硬给一边
    assert cce._direction_from_mechanism("若停火则回落 若升级则推升") == ""
    assert cce._direction_from_mechanism("", "") == ""


# ── 触发词命中 ────────────────────────────────────────────────────────────

def test_meaningful_keyword_hit_rejects_bare_tickers_and_short_ascii():
    # 只有 ticker 命中 → 视为未命中（否则所有链一起命中）
    assert cce.meaningful_keyword_hit("BTC 大涨", ["BTC"]) == []
    # "buy" 这类 3 字母高频词也不算有意义命中
    assert cce.meaningful_keyword_hit("we buy the dip", ["buy"]) == []
    # 中文关键词 >= 2 字才算
    assert cce.meaningful_keyword_hit("霍尔木兹海峡封锁", ["霍尔木兹"]) == ["霍尔木兹"]


def test_meaningful_keyword_hit_does_not_let_asset_certify_itself():
    # 门控语义：资产名/ticker 可以出现在命中列表里，但**不能单独构成命中**
    hits = cce.meaningful_keyword_hit("WTI 原油库存下降", ["WTI", "原油库存"], asset="WTI")
    assert "原油库存" in hits
    assert cce.meaningful_keyword_hit("WTI 小幅波动", ["WTI"], asset="WTI") == []


def test_meaningful_keyword_hit_keeps_mixed_hits():
    hits = cce.meaningful_keyword_hit("BTC 与 美联储缩表", ["BTC", "缩表"])
    assert hits == ["BTC", "缩表"]      # 有一个有意义命中，就把所有命中都返回


# ── 语气 / 同一件事 ───────────────────────────────────────────────────────

def test_tone_of_counts_and_ties_as_unknown():
    assert cce._tone_of("冲突升级 空袭 导弹袭击") == "escalate"
    assert cce._tone_of("双方达成停火 恢复谈判") == "ease"
    assert cce._tone_of("升级 停火") == ""          # 平票 = 判不出，宁可不标
    assert cce._tone_of("drone attack escalates") == "escalate"


def test_char_bigrams_strip_punctuation_and_reject_too_short():
    assert cce._char_bigrams("停火！") == set()      # 去掉标点只剩 2 字，无二元组
    assert cce._char_bigrams("霍尔木兹 封锁") == cce._char_bigrams("霍尔木兹，封锁")
    assert "停火" in cce._char_bigrams("停火协议")


def test_same_story_catches_long_article_vs_short_flash():
    long_article = "据外媒报道，霍尔木兹海峡附近发生油轮遇袭事件，油价短线跳涨，市场担忧供应中断"
    short_flash = "霍尔木兹海峡油轮遇袭，供应中断担忧升温"
    assert cce._same_story(short_flash, [long_article]) is True
    assert cce._same_story("美联储宣布加息 25 个基点", [long_article]) is False
    assert cce._same_story("", [long_article]) is False


def test_chain_names_similar_uses_shorter_denominator():
    a = "霍尔木兹海峡封锁→原油上涨"
    b = "霍尔木兹海峡封锁持续→原油走强"
    assert cce._chain_names_similar(a, b) is True
    assert cce._chain_names_similar(a, "美联储缩表→美元走强") is False
    assert cce._chain_names_similar("", a) is False


# ── 分位数 / 序列取价 ─────────────────────────────────────────────────────

def test_percentile_linear_interpolation():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert cce._percentile(vals, 50) == 3.0
    assert cce._percentile(vals, 10) == 1.4
    assert cce._percentile(vals, 90) == 4.6
    assert cce._percentile([], 50) == 0.0
    assert cce._percentile([7.0], 90) == 7.0


def test_close_at_or_before_takes_last_bar_not_first():
    series = [(100, 10.0), (200, 11.0), (300, 12.0)]
    assert cce._close_at_or_before(series, 250) == 11.0
    assert cce._close_at_or_before(series, 300) == 12.0
    assert cce._close_at_or_before(series, 50) is None
    assert cce._close_at_or_before([], 100) is None


# ── 时间解析 / 评估窗口 ───────────────────────────────────────────────────

def test_parse_ts_matches_local_strptime_and_returns_zero_on_garbage():
    ts = cce._parse_ts("2026-11-21 09:30:00")
    assert ts == int(time.mktime(time.strptime("2026-11-21 09:30:00", "%Y-%m-%d %H:%M:%S")))
    assert cce._parse_ts("") == 0
    assert cce._parse_ts(None) == 0
    assert cce._parse_ts("not-a-date") == 0


def _chains_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, name TEXT, "
                 "cause TEXT, effect TEXT, mechanism TEXT)")
    conn.executemany("INSERT INTO causal_chains (id,name,cause,effect,mechanism) VALUES (?,?,?,?,?)",
                     rows)
    return conn


def test_chain_horizon_bars_pulse_1_trend_4():
    cce._HORIZON_CACHE.clear()
    conn = _chains_conn([
        (1, "地缘冲突突发声明", "袭击", "", "一次性冲击"),
        (2, "实际利率与流动性长期周期", "央行缩表", "", "结构性"),
        (3, "没有方向词的链", "", "", ""),
    ])
    assert cce.chain_horizon_bars(conn, 1) == cce._HORIZON_PULSE_BARS
    assert cce.chain_horizon_bars(conn, 2) == cce._HORIZON_TREND_BARS
    assert cce.chain_horizon_bars(conn, 3) == cce._HORIZON_TREND_BARS   # 判不出按趋势
    assert cce.chain_horizon_bars(conn, 99) == cce._HORIZON_TREND_BARS  # 链不存在不崩
    assert cce.chain_horizon_bars(conn, "abc") == cce._HORIZON_TREND_BARS
    assert cce._HORIZON_CACHE.get(1) == cce._HORIZON_PULSE_BARS         # 命中缓存
    conn.close()
