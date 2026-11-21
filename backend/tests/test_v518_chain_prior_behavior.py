#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v518：因果链先验（chain_prior）的行为测试。

这层决定"要不要因为一条链而放弃这笔单"，错的代价是拿噪声挡真实交易：
  ① 关键词必须干净：净化过的 trigger_keywords 优先，通用词/品种代号/单字全丢；
  ② 英文关键词必须词边界（"oil" 不能命中 "boil"）；
  ③ 只有强证据能定向：n≥12 且跑赢同品种基准 ≥10pp、p≤0.05；拿不到基准率退回 Wilson 下界>0.5；
  ④ weak 档只出 *_WEAK 标注，永远不否决；BOTH（幅度）链达标也只写"追不追"，永不转方向；
  ⑤ 按时间匹配默认不吃未来新闻（前视偏差），要事后解释才显式开。
"""
import math
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import chain_prior as cp  # noqa: E402

T = 1_760_000_000
_OPEN = []


@pytest.fixture(autouse=True)
def _clean():
    cp._BASE_RATE_CACHE.clear()
    yield
    while _OPEN:
        _OPEN.pop().close()
    cp._BASE_RATE_CACHE.clear()


def _conn():
    conn = sqlite3.connect(":memory:")
    _OPEN.append(conn)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE causal_chains (id TEXT PRIMARY KEY, chain_id TEXT, name TEXT,
            asset TEXT, trigger_words TEXT, trigger_keywords TEXT,
            expected_direction TEXT, weight REAL, confidence REAL,
            hit_count INTEGER, miss_count INTEGER, magnitude_lo REAL, magnitude_hi REAL,
            mechanism TEXT, enabled INTEGER, active INTEGER);
        CREATE TABLE causal_chain_outcomes (chain_id TEXT, status TEXT);
        CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE news_events (asset TEXT, ts INTEGER, headline TEXT);
        """
    )
    return conn


def _chain(conn, cid, asset="XAU", direction="UP", keywords="美联储,降息",
           words="", weight=1.0, conf=0.5, enabled=1, active=1, mechanism=""):
    conn.execute(
        "INSERT INTO causal_chains VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, cid, f"链{cid}", asset, words, keywords, direction, weight, conf,
         0, 0, 1.0, 3.0, mechanism, enabled, active),
    )


def _outcomes(conn, cid, hits, misses):
    conn.executemany("INSERT INTO causal_chain_outcomes VALUES (?,?)",
                     [(cid, "HIT")] * hits + [(cid, "MISS")] * misses)


def _daily(conn, asset, closes, start=T):
    for i, c in enumerate(closes):
        conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)",
                     (asset, start + i * 86400, float(c)))


def _prior(direction="UP", n=20, hits=14, lift=0.25, p=0.02, posterior=0.5,
           lower=None, **kw):
    p = {"direction": direction, "n_outcomes": n,
         "hit_rate": (hits / n) if n else 0.0, "posterior": posterior,
         "wilson_lower": cp._wilson_lower(hits, n) if lower is None else lower,
         "lift": lift, "p_value": p, "matched_keywords": ["x"]}
    p.update(kw)
    return p


# ── ① 关键词清洗 ───────────────────────────────────────────────
def test_keyword_set_drops_generic_tokens_short_parts_and_uses_clean_column():
    kws = cp._keyword_set({"trigger_keywords": "美联储,降息 BUY XAU 涨|oil",
                           "trigger_words": "美联储"})
    assert kws == {"美联储", "降息", "oil"}
    # trigger_keywords 为空才回退 trigger_words
    assert cp._keyword_set({"trigger_keywords": "", "trigger_words": "关税"}) == {"关税"}


def test_keyword_set_never_unions_both_columns():
    kws = cp._keyword_set({"trigger_keywords": "降息", "trigger_words": "美联储,降息"})
    assert kws == {"降息"}


# ── ② 词边界 ──────────────────────────────────────────────────
def test_english_keywords_need_word_boundaries():
    assert cp._hit("oil", "crude oil jumps") is True
    assert cp._hit("oil", "water begins to boil") is False
    assert cp._hit("rate", "corporate bond sale") is False
    assert cp._hit("降息", "美联储宣布降息25bp") is True  # 中文按子串


# ── ③ 统计口径 ─────────────────────────────────────────────────
def test_wilson_bounds_refuse_to_trust_two_out_of_two():
    assert cp._wilson_lower(0, 0) == 0.0 and cp._wilson_upper(0, 0) == 0.0
    assert cp._wilson_lower(2, 2) < 0.5          # 点估计 100%，下界只有 ~34%
    lo = cp._wilson_lower(11, 19)
    hi = cp._wilson_upper(11, 19)
    assert 0.33 < lo < 0.579 < hi < 0.79          # 实测区间 36%~77% 附近
    assert cp._wilson_upper(19, 19) <= 1.0


def test_binom_p_is_one_when_it_cannot_be_judged():
    assert cp._binom_p_greater(0, 0, 0.5) == 1.0
    assert cp._binom_p_greater(5, 10, 0.0) == 1.0
    assert cp._binom_p_greater(5, 10, 1.0) == 1.0
    assert cp._binom_p_greater(18, 20, 0.5) < 0.05
    assert cp._binom_p_greater(10, 20, 0.5) > 0.3


def test_base_rate_up_counts_drift_not_direction():
    conn = _conn()
    _daily(conn, "XAU", list(range(100, 60, -1)))  # 单边下跌：往后 5 根几乎都不涨
    rate = cp._base_rate_up(conn, "xau")
    assert rate is not None and rate < 0.5
    assert cp._base_rate_up(conn, "XAU") == rate  # 走缓存
    assert cp._base_rate_up(conn, "NOPE") is None


def test_base_rate_returns_none_without_enough_history():
    conn = _conn()
    _daily(conn, "XAU", [1, 2, 3])   # 只有 3 根 < horizon+1
    assert cp._base_rate_up(conn, "XAU") is None


def test_outcomes_ignore_both_chains_and_non_settled_status():
    conn = _conn()
    _chain(conn, "c1", direction="UP")
    _chain(conn, "c2", direction="BOTH")
    _outcomes(conn, "c1", 10, 5)
    conn.executemany("INSERT INTO causal_chain_outcomes VALUES (?,?)",
                     [("c1", "PENDING"), ("c1", "NO_DATA"),
                      ("c2", "HIT"), ("c2", "HIT")])
    ev = cp._evidence_from_outcomes(conn, ["c1", "c2"])
    assert ev["c1"] == (10, 15)   # 只算 HIT/HIT_WEAK/MISS
    assert "c2" not in ev        # BOTH 不参与方向口径


def test_enrich_evidence_fills_interval_lift_and_tier():
    conn = _conn()
    _chain(conn, "c1", direction="UP")
    _outcomes(conn, "c1", 14, 6)
    _daily(conn, "XAU", [10, 11, 12, 13, 15, 14, 16, 17, 18])
    priors = [{"chain_id": "c1", "asset": "XAU", "direction": "UP", "posterior": 0.4}]
    cp._enrich_evidence(conn, priors)
    p = priors[0]
    assert p["n_outcomes"] == 20 and p["hit_rate"] == pytest.approx(0.7)
    assert p["ci_low"] == p["wilson_lower"] and p["ci_high"] > p["ci_low"]
    assert p["base_rate"] is not None and p["lift"] == pytest.approx(0.7 - p["base_rate"], abs=5e-5)
    assert p["tier"] in ("strong", "weak")
    assert p["evidence_source"] == "causal_chain_outcomes"


def test_both_chain_keeps_magnitude_evidence_separate_from_direction():
    conn = _conn()
    _chain(conn, "c2", direction="BOTH")
    _outcomes(conn, "c2", 9, 1)
    priors = [{"chain_id": "c2", "asset": "XAU", "direction": "BOTH"}]
    cp._enrich_evidence(conn, priors)
    p = priors[0]
    assert p["mag_n"] == 10 and p["mag_hit_rate"] == pytest.approx(0.9)
    assert p["tier"] == "strong"          # 幅度档达标
    assert p["lift"] is None and p["p_value"] is None  # 方向证据恒空


# ── ④ 档位与合成方向 ───────────────────────────────────────────
def test_tier_strong_requires_twelve_samples_and_real_excess():
    assert cp.tier_of(_prior(n=11, hits=8, lift=0.3, p=0.01)) == "weak"
    assert cp.tier_of(_prior(n=12, hits=9, lift=0.25, p=0.02)) == "strong"
    assert cp.tier_of(_prior(n=12, hits=8, lift=0.05, p=0.02)) == "weak"   # 超额不够
    assert cp.tier_of(_prior(n=12, hits=8, lift=0.25, p=0.30)) == "weak"   # 不显著
    assert cp.tier_of(_prior(n=5, hits=4, lift=0.3, p=0.01)) == "weak"
    assert cp.tier_of(_prior(n=2, hits=2)) == "none"


def test_tier_falls_back_to_wilson_when_no_base_rate():
    # 拿不到基准率（该品种没行情）时退回 Wilson 下界>0.5，宁可不达标也不开闸
    assert cp.tier_of(_prior(n=40, hits=30, lift=None, p=None, lower=0.6)) == "strong"
    assert cp.tier_of(_prior(n=40, hits=30, lift=None, p=None, lower=0.49)) == "weak"


def test_aggregate_direction_never_upgrades_weak_to_a_real_direction():
    assert cp.aggregate_direction([]) == "UNKNOWN"
    strong_up = _prior(direction="UP", n=20, hits=15)
    weak_up = _prior(direction="UP", n=5, hits=4)
    assert cp.aggregate_direction([strong_up]) == "UP"
    assert cp.aggregate_direction([strong_up, _prior(direction="DOWN", n=20, hits=15)]) == "CONFLICT"
    assert cp.aggregate_direction([weak_up]) == "UP_WEAK"
    assert cp.aggregate_direction([weak_up, _prior(direction="DOWN", n=5, hits=3)]) == "WEAK_MIXED"
    assert cp.aggregate_direction([_prior(direction="UP", n=2, hits=2)]) == "UNKNOWN"


# ── ⑤ 否决闸门 ─────────────────────────────────────────────────
def test_veto_requires_opposite_direction_and_strong_tier():
    strong_down = _prior(direction="DOWN", n=20, hits=15)
    assert cp.conflict_veto([strong_down], "BUY") is strong_down
    assert cp.conflict_veto([strong_down], "SELL") is None
    assert cp.conflict_veto([_prior(direction="DOWN", n=5, hits=4)], "BUY") is None
    assert cp.conflict_veto([strong_down], "HOLD") is None
    assert cp.conflict_veto([strong_down], "") is None


def test_veto_exempts_statistically_significant_chains_from_posterior():
    p = _prior(direction="DOWN", n=20, hits=15, posterior=0.4, lift=0.2)
    assert cp.conflict_veto([p], "BUY", min_posterior=0.60) is p
    p2 = dict(p, lift=None)
    assert cp.conflict_veto([p2], "BUY", min_posterior=0.60) is None
    assert cp.conflict_veto([p], "BUY", min_hit_rate=0.95) is None


# ── ⑥ 匹配 ────────────────────────────────────────────────────
def test_headline_match_needs_asset_enabled_and_a_keyword():
    conn = _conn()
    _chain(conn, "c1", asset="XAU", keywords="降息")
    _chain(conn, "c2", asset="XAU", keywords="降息", enabled=0)
    _chain(conn, "c3", asset="XAU", keywords="降息", active=0)
    _chain(conn, "c4", asset="XAU", keywords="加息")
    _chain(conn, "c5", asset="BTC", keywords="降息")
    got = cp.match_by_headline(conn, "XAU", "美联储宣布降息")
    assert [p["chain_id"] for p in got] == ["c1"]
    assert got[0]["matched_keywords"] == ["降息"]
    assert cp.match_by_headline(conn, "", "美联储降息") == []


def test_headline_match_reserves_half_the_slots_for_direction_chains():
    conn = _conn()
    for i in range(3):
        _chain(conn, f"up{i}", direction="UP", keywords="降息")
        conn.execute("UPDATE causal_chains SET weight=? WHERE id=?", (1.0, f"up{i}"))
    for i in range(3):
        _chain(conn, f"both{i}", direction="BOTH", keywords="降息")
        conn.execute("UPDATE causal_chains SET weight=? WHERE id=?", (0.1, f"both{i}"))
    got = cp.match_by_headline(conn, "XAU", "降息", limit=4)
    dirs = [p["direction"] for p in got]
    assert dirs.count("BOTH") <= 2 and dirs.count("UP") >= 2


def test_rank_prefers_samples_over_a_pretty_posterior():
    rich = _prior(direction="UP", n=12, hits=9, posterior=0.4)
    shiny = _prior(direction="UP", n=0, hits=0, posterior=1.0)
    assert cp._rank([shiny, rich])[0] is rich


def test_rank_uses_magnitude_evidence_for_both_chains():
    both = _prior(direction="BOTH", n=0, hits=0, posterior=0.3,
                  mag_n=10, mag_hit_rate=0.9)
    dirp = _prior(direction="UP", n=2, hits=1, posterior=0.3)
    assert cp._rank([dirp, both])[0] is both


def test_time_match_never_uses_future_news_by_default():
    conn = _conn()
    _chain(conn, "c1", asset="XAU", keywords="降息")
    conn.execute("INSERT INTO news_events VALUES ('XAU', ?, '美联储降息')", (T + 600,))
    assert cp.match_by_time(conn, "XAU", T) == []
    got = cp.match_by_time(conn, "XAU", T, allow_future=True)
    assert len(got) == 1 and got[0]["match_type"] == "time_window"
    assert got[0]["matched_headline"] == "美联储降息"


def test_time_match_picks_the_nearest_past_news():
    conn = _conn()
    _chain(conn, "c1", asset="XAU", keywords="降息")
    conn.execute("INSERT INTO news_events VALUES ('XAU', ?, '美联储降息 近')", (T - 120,))
    conn.execute("INSERT INTO news_events VALUES ('XAU', ?, '美联储降息 远')", (T - 3000,))
    got = cp.match_by_time(conn, "XAU", T)
    assert got[0]["matched_headline"].endswith("近")
    assert cp.match_by_time(conn, "XAU", 0) == []


# ── ⑦ prompt 文本 ────────────────────────────────────────────
def test_prompt_block_is_empty_without_priors():
    assert cp.build_prompt_block([]) == ""


def test_prompt_block_separates_verified_from_candidate_evidence():
    strong = _prior(direction="UP", n=20, hits=15, p=0.01, lift=0.2,
                    tier="strong", asset="XAU", name="降息链", mechanism="实际利率↓")
    strong.update({"ci_low": 0.5, "ci_high": 0.85, "base_rate": 0.5})
    text = cp.build_prompt_block([strong])
    assert "已验证档" in text and "必须" in text
    assert "基准=50%" in text and "超额=+20pp" in text and "p=0.01" in text
    assert "机理：实际利率↓" in text
    weak = _prior(direction="UP", n=5, hits=4, tier="weak", asset="XAU", name="弱链")
    text2 = cp.build_prompt_block([weak])
    assert "候选档" in text2 and "方向仍以本次证据为准" in text2


def test_prompt_block_marks_magnitude_chains_as_not_directional():
    both = _prior(direction="BOTH", n=0, hits=0, tier="strong",
                  mag_n=10, mag_hit_rate=0.9, asset="XAU", name="幅度链")
    text = cp.build_prompt_block([both])
    assert "不可据此定向" in text and "幅度命中=90%(n=10)" in text


# ── ⑧ 列表标注 ───────────────────────────────────────────────
def test_annotate_chains_adds_evidence_without_touching_fields():
    conn = _conn()
    _chain(conn, "c1", direction="UP")
    _outcomes(conn, "c1", 10, 2)
    rows = [{"id": "c1", "asset": "XAU", "expected_direction": "UP", "confidence": 0.62}]
    out = cp.annotate_chains(conn, rows)
    row = out[0]
    assert row["confidence"] == 0.62           # 原字段语义不变
    assert row["evidence"]["n_outcomes"] == 12
    assert row["p_hit"] == pytest.approx(10 / 12, abs=1e-4)
    assert row["posterior"] == pytest.approx(0.62)


def test_annotate_chains_marks_magnitude_only_strength_explicitly():
    conn = _conn()
    _chain(conn, "c2", direction="BOTH")
    _outcomes(conn, "c2", 9, 1)
    rows = cp.annotate_chains(conn, [{"id": "c2", "asset": "XAU", "expected_direction": "BOTH"}])
    assert rows[0]["evidence"]["tier"] == "strong_mag"


def test_annotate_chains_returns_input_on_empty():
    assert cp.annotate_chains(_conn(), []) == []
