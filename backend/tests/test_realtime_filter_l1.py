"""realtime_filter L1 关键词漏斗用例。

L1 是"新闻进库前第一道筛"：命中垃圾词直接 FILTERED，命中核心词直接放行不烧 L2 token，
两边都不中才升级到 L2 LLM。这条链判错的代价是双向的 ——
误杀 = 该看到的宏观事件没看到；漏放 = 噪音灌进主分析、白白诱导下单。

两个真实事故在这里固化成回归用例：
1. `数据整理` 把「金十数据整理：每日全球外汇市场要闻速递」整篇 FX 要闻打掉（v433 误杀）。
   现在汇总类垃圾词在命中池内品种/市场类别时让位。
2. 裸 `AMA` 会匹配英文标题里的普通单词，已从 JUNK 列表删除。
"""

import pytest

import realtime_filter as rf


def _l1(text):
    return rf._l1_classify(text)


# ── JUNK 优先 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "比特币空投活动开始，参与即送 0.1 BTC",
    "新用户注册即送 100 USDT",
    "本周 NFT 盲盒 mint 开放",
    "XX 交易所新币上线公告",
    "操盘必读：今日行情解读",
])
def test_marketing_and_listing_noise_is_filtered(text):
    out = _l1(text)
    assert out["verdict"] == "JUNK"
    assert out["status"] == "FILTERED"
    assert out["is_noise"] == 1
    # 命中词必须真的出现在原文里（防止把"没匹配上也判垃圾"写成通过）
    assert out["matched_keyword"] in text.lower()


def test_junk_wins_over_core_in_same_title():
    # 「比特币空投」同时含核心品种词与垃圾词 —— 垃圾词优先，不能因为提到 BTC 就放行
    out = _l1("比特币空投活动开始")
    assert out["verdict"] == "JUNK"


# ── CORE 放行 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "美联储宣布降息 25 个基点",
    "美国 9 月 CPI 同比 3.2%，高于预期",
    "人民银行开展 MLF 操作 5000 亿元",
    "美国 8 月非农就业人口 18.7 万",
])
def test_macro_core_keywords_pass_without_l2(text):
    out = _l1(text)
    assert out["verdict"] == "CORE"
    assert out["status"] == "PENDING"
    assert out["relevance_score"] == pytest.approx(0.90)
    assert out["matched_keyword"] in text.lower()


def test_core_matching_is_case_insensitive():
    assert _l1("FOMC meeting minutes released")["verdict"] == "CORE"
    assert _l1("fomc minutes")["verdict"] == "CORE"


# ── 不确定 → 升级 L2 ──────────────────────────────────────────────────────

def test_uncertain_text_returns_none_for_l2():
    assert _l1("某地今日天气预报：多云转晴") is None
    assert _l1("") is None


def test_hash_prefix_is_stripped_before_matching():
    # 原始标题前的 [hash:...] 标记不能干扰关键词匹配
    assert _l1("[hash:9f3a1b] 美联储维持利率不变")["verdict"] == "CORE"


# ── v433 回归：汇总类垃圾词让位 ───────────────────────────────────────────

def test_daily_fx_digest_is_not_killed_by_data_digest_word():
    # 事故原文：整篇 FX 要闻被 `数据整理` 打成垃圾
    out = _l1("金十数据整理：每日全球外汇市场要闻速递")
    assert out["verdict"] == "CORE"
    assert out["matched_keyword"] == "pool-keyword"
    assert out["relevance_score"] == pytest.approx(0.70)     # 低于直命核心词，仍进主分析
    assert out["status"] == "PENDING"


def test_digest_word_without_market_context_stays_junk():
    # 让位只给"池内资产/市场类别"语境，纯数据整理照拦
    out = _l1("本周数据整理：某地马拉松报名人数")
    assert out["verdict"] == "JUNK"
    assert out["matched_keyword"] == "数据整理"


def test_weekly_report_with_market_category_passes():
    assert _l1("贵金属周报：金银比价走阔")["verdict"] == "CORE"


# ── 截断口径 ──────────────────────────────────────────────────────────────

def test_only_first_500_chars_are_matched():
    # 关键词藏在 500 字之后 → L1 不给结论，交给 L2（不是静默丢进垃圾）
    long_text = "某某公司发布季度业务说明。" * 60 + "美联储降息"
    assert len(long_text) > 500
    assert _l1(long_text) is None
    # 同样内容把关键词提到开头就能命中
    assert _l1("美联储降息。" + "某某公司发布季度业务说明。" * 60)["verdict"] == "CORE"


# ── 活跃词表结构 ──────────────────────────────────────────────────────────

def test_active_keywords_are_lowercased_and_split_by_kind():
    assert set(rf._ACTIVE_KEYWORDS) == {"junk", "core"}
    assert all(w == w.lower() for w in rf._ACTIVE_KEYWORDS["junk"])
    assert all(w == w.lower() for w in rf._ACTIVE_KEYWORDS["core"])


def test_bare_ama_is_not_a_junk_word():
    # 3 字母缩写会误匹配英文标题里的普通单词，v433 已删除
    assert "AMA" not in rf.JUNK_WORDS
    assert "Ask Me Anything" in rf.JUNK_WORDS
