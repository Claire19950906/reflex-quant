# -*- coding: utf-8 -*-
"""发现页（discovery）的展示口径契约测试（全离线）。

这些函数决定用户第一眼看到什么：
  · 「相关性」到底是算出来的还是猜的（method 字段必须如实）
  · 周期阶段的中文与一行 hint 不能把 unknown 说成具体结论
  · 事件标题不能把 `[hash:xxx]` 直接摊在面板上
  · 财报闸门在「没有财报数据」时只许说未核验，不许给结论
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import discovery  # noqa: E402


# ── 品种归桶 ──────────────────────────────────────────────────────────────

def test_bucket_of_crypto_vs_stock():
    assert discovery._bucket_of("BTCUSDT") == "altcoin"
    assert discovery._bucket_of("eth") == "altcoin"
    assert discovery._bucket_of("SOL") == "altcoin"
    assert discovery._bucket_of("000660") == "news_stock"
    assert discovery._bucket_of("SK.HYNIX") == "news_stock"
    assert discovery._bucket_of("AAPL") == "news_stock"   # 不认识也归新闻股，不丢


# ── Fed 方向与影响 ────────────────────────────────────────────────────────

def test_derive_direction_thresholds():
    assert discovery._derive_direction_from_ust2y(6) == "hawkish"
    assert discovery._derive_direction_from_ust2y(5) == "hawkish"     # 边界含 5
    assert discovery._derive_direction_from_ust2y(4.9) == "neutral"
    assert discovery._derive_direction_from_ust2y(-5) == "dovish"
    assert discovery._derive_direction_from_ust2y(0) == "neutral"


def test_fed_impacts_fallback_is_labelled_heuristic():
    out = discovery._compute_fed_impacts(25.0)
    assert out["n_real_correlation"] == 0
    assert len(out["impacts"]) == len(discovery.IMPACT_CATALOG)
    dxy = next(i for i in out["impacts"] if i["symbol"] == "DXY")
    assert dxy["method"] == "heuristic" and dxy["sample_size"] == 0
    assert dxy["correlation"] == -1.0                                 # 美元与利率反向
    assert "样本不足" in dxy["note"]


def test_fed_impacts_clamps_extreme_change():
    out = discovery._compute_fed_impacts(100.0)                       # 4 倍阈值
    btc = next(i for i in out["impacts"] if i["symbol"] == "BTC")
    assert btc["correlation"] == 1.0                                  # 夹到 ±1，不外推
    dxy = next(i for i in out["impacts"] if i["symbol"] == "DXY")
    assert dxy["correlation"] == -1.0


def test_fed_impacts_real_correlation_wins_and_is_counted():
    real = {"BTC": {"correlation": 0.42, "sample_size": 88, "method": "pearson"}}
    out = discovery._compute_fed_impacts(26.0, real)
    btc = next(i for i in out["impacts"] if i["symbol"] == "BTC")
    assert btc["correlation"] == 0.42 and btc["method"] == "pearson"
    assert btc["sample_size"] == 88
    assert out["n_real_correlation"] == 1
    assert "1/11" in out["note"]


def test_fed_impacts_real_corr_missing_fields_not_crash():
    """上游 correlation 结构缺字段时，不能整个接口 500 —— 该品种回落 heuristic。"""
    out = discovery._compute_fed_impacts(10.0, {"BTC": {"correlation": 0.3}})
    btc = next(i for i in out["impacts"] if i["symbol"] == "BTC")
    assert btc["method"] == "heuristic"
    assert out["n_real_correlation"] == 0


# ── 周期阶段展示 ──────────────────────────────────────────────────────────

def test_phase_cn_known_and_unknown():
    assert discovery._phase_cn("markup") == "拉升"
    assert discovery._phase_cn("distribution") == "派发"
    assert discovery._phase_cn("unknown") == "未知"
    assert discovery._phase_cn("weird") == "weird"                     # 不许丢成 None


def test_phase_hint_reversal_needs_high_prob():
    hi = discovery._phase_hint({"phase": "distribution"}, {"prob": 0.6, "direction": "down"})
    assert "派发中" in hi and "60%" in hi
    lo = discovery._phase_hint({"phase": "distribution"}, {"prob": 0.4, "direction": "neutral"})
    assert "派发中" not in lo                                          # 低概率不给反转结论
    assert "顺势做多" in discovery._phase_hint({"phase": "markup"}, {})
    assert "区间震荡" in discovery._phase_hint({"phase": "ranging"}, {})


# ── 事件标题 ──────────────────────────────────────────────────────────────

def test_event_title_strips_hash_prefix():
    t = discovery._event_title("[hash:ab12cd] 美联储降息 25bp", "", "")
    assert t == "美联储降息 25bp"
    assert discovery._event_title("[hash:abc123]  ", "理由文本", "") == "理由文本"
    assert discovery._event_title("", "", "白宫") == "白宫"
    assert discovery._event_title("", "", "") == "（无标题事件）"
    assert len(discovery._event_title("长" * 200, "", "")) == 90


# ── 受影响标的 hint ───────────────────────────────────────────────────────

def test_format_affected_hint_shapes():
    assert discovery._format_affected_hint([]) == ""
    assert discovery._format_affected_hint([{"asset": "BTC", "action": "BUY"}]) == "BTC → BUY"
    many = [{"asset": a, "action": "BUY"} for a in ("BTC", "ETH", "SOL", "XRP", "DOGE")]
    hint = discovery._format_affected_hint(many)
    assert hint.count("·") >= 3 and "+2条" in hint                    # 只列 3 个，余下计数


# ── 元认知：偏差与假设类型 ────────────────────────────────────────────────

def test_detect_bias_reports_each_bias_once():
    found = discovery._detect_bias("美联储肯定总是降息，所以 BTC 一定涨")
    types = [f["type"] for f in found]
    assert "confirmation_bias" in types
    assert "anchoring" in types
    assert types.count("confirmation_bias") == 1                      # 同 bias 只报一次
    assert discovery._detect_bias("BTC 可能震荡") == []


def test_detect_bias_scope_overreach():
    found = discovery._detect_bias("绝大部分山寨币都会跟涨")
    assert any(f["type"] == "scope_overreach" for f in found)


def test_detect_hypothesis_type_order():
    assert discovery._detect_hypothesis_type("美联储加息对 BTC 的影响") == "fed_macro"
    assert discovery._detect_hypothesis_type("BTC 减半") == "crypto"
    assert discovery._detect_hypothesis_type("黄金和原油") == "commodity"
    assert discovery._detect_hypothesis_type("今天天气不错") == "other"


def test_parse_hypothesis_cause_mapping():
    fed = discovery._parse_hypothesis("美联储降息")
    assert fed == {"cause": "fed_rate_expectation", "effect": "asset_movement", "type": "fed_macro"}
    crypto = discovery._parse_hypothesis("比特币突破前高")
    assert crypto["cause"] == "btc_trend"
    other = discovery._parse_hypothesis("天气")
    assert other["cause"] == "unknown"                                # 不认识就不编原因


# ── 财报闸门 ──────────────────────────────────────────────────────────────

def test_fundamental_gate_no_data_is_unverified():
    out = discovery._fundamental_gate({}, "BTC")
    assert out["verified"] is False and out["ok"] is None
    assert "未核验" in out["note"]


def test_fundamental_gate_rules():
    pack = {"summary": {"BTC": {"ttm": {"net_income": -1e9},
                                "yoy": {"revenue": -25.0},
                                "latest": {"period_end": "2026-09-30"}}}}
    out = discovery._fundamental_gate(pack, "btc")                    # 小写也要能对上
    assert out["verified"] is True and out["ok"] is False
    assert "十亿美元" in out["note"] and "营收同比" in out["note"]
    assert out["period_end"] == "2026-09-30"
    good = {"summary": {"ETH": {"ttm": {"net_income": 2e9}, "yoy": {"revenue": 10.0}}}}
    assert discovery._fundamental_gate(good, "ETH")["ok"] is True


# ── 进程内缓存 ────────────────────────────────────────────────────────────

def test_disc_cache_ttl():
    discovery._DISC_CACHE.clear()
    discovery._disc_cache_put("k", {"v": 1})
    assert discovery._disc_cache_get("k", ttl=60) == {"v": 1}
    assert discovery._disc_cache_get("k", ttl=0) is None              # ttl 0 = 立刻过期
    assert discovery._disc_cache_get("missing") is None
    discovery._DISC_CACHE.clear()
