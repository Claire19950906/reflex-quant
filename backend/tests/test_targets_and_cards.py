# -*- coding: utf-8 -*-
"""v422 新功能的三块纯计算：标的别名、可执行卡片、源健康度。

这三块都没有网络调用、没有数据库写入，所以用普通 pytest 就能钉住行为。
"""
from __future__ import annotations

import signal_cards
import source_health
import target_resolver


# ── ① 标的别名 ────────────────────────────────────────────────────────────
def test_resolve_chinese_and_ascii():
    hits = target_resolver.resolve("英伟达(NVDA)财报超预期，黄仁勋称需求强劲")
    targets = [t for t, _ in hits]
    assert "NVDA" in targets


def test_resolve_ascii_word_boundary():
    """'mu' 不该命中 'must' / 'music' —— 词边界是硬要求，否则榜单全是噪音。"""
    assert target_resolver.resolve_one("You must consider this music market") is None
    assert target_resolver.resolve_one("美光(MU)上调指引") == "MU"


def test_resolve_empty_text():
    assert target_resolver.resolve("") == []


def test_summarize_unresolved_counts_and_upper_rate():
    rows = [
        (1, "src", "金价突破新高，黄金ETF流入", "2026-11-18 00:00"),
        (2, "src", "油价大幅波动，OPEC 减产", "2026-11-18 00:01"),
        (3, "src", "美国住宅建筑商情绪恶化", "2026-11-18 00:02"),
    ]
    out = target_resolver.summarize_unresolved(rows)
    assert out["total"] == 3
    assert out["with_candidate"] == 2
    assert out["no_clue"] == 1
    assert round(out["recover_rate_upper"], 4) == round(2 / 3, 4)
    assert [i["target"] for i in out["targets"]] == ["XAU", "WTI"]


# ── ② 可执行卡片 ──────────────────────────────────────────────────────────
def _bars(n=20, price=100.0, rng=2.0, step_sec=86400):
    """造 n 根日线：high=price+rng, low=price-rng, close=price（ATR≈2rng）。"""
    out = []
    ts = 1_700_000_000
    for i in range(n):
        out.append((ts + i * step_sec, price + rng, price - rng, price))
    return out


def test_atr_needs_enough_bars():
    """K 线不足就返回 None，不拿一两根硬算。"""
    assert signal_cards.atr([(2, 1, 1.5)] * 5) is None


def test_build_plan_buy_stop_below_entry():
    plan = signal_cards.build_plan(100.0, "BUY", atr_value=2.0, risk_pct=1.0, atr_mult=1.5)
    assert plan["ok"] is True
    assert plan["stop"] == 97.0          # 100 - 1.5*2
    assert plan["stop_pct"] == 3.0
    assert plan["targets"][0] == 103.0   # 1R
    assert plan["size_pct"] == round(1.0 / 3.0 * 100, 2)
    assert plan["estimated"] is False


def test_build_plan_sell_stop_above_entry():
    plan = signal_cards.build_plan(100.0, "SELL", atr_value=2.0)
    assert plan["stop"] == 103.0
    assert plan["targets"][0] == 97.0


def test_build_plan_without_atr_is_estimated():
    plan = signal_cards.build_plan(100.0, "BUY", atr_value=None)
    assert plan["ok"] is True
    assert plan["estimated"] is True
    assert plan["stop_pct"] == 2.0       # 兜底 2%
    assert "兜底" in plan["why"]


def test_build_plan_rejects_missing_price_or_direction():
    assert signal_cards.build_plan(None, "BUY", 2.0)["ok"] is False
    assert signal_cards.build_plan(100.0, "HOLD", 2.0)["ok"] is False


def test_build_card_flags_stale_and_ready():
    bars = _bars(20)
    now = bars[-1][0] + 60 * 86400       # 60 天没更新
    card = signal_cards.build_card(
        {"asset": "XAU", "action": "BUY"}, bars, tf="1d", now_ts=now
    )
    assert card["market"]["bars"] == 20
    assert card["market"]["stale"] is True
    assert card["execute_ready"] is False
    assert any("未更新" in b for b in card["blocking"])


def test_build_card_no_bars_marks_not_ready():
    card = signal_cards.build_card({"asset": "CN_STOCK", "action": "BUY"}, [], tf="1d", now_ts=1)
    assert card["plan"]["ok"] is False
    assert card["execute_ready"] is False


# ── ③ 源健康度 ────────────────────────────────────────────────────────────
def _stats(n24, n7, last_ts=None):
    return {"n24": n24, "n7": n7, "last_ts": last_ts, "last_at": ""}


def test_source_health_classification():
    now = 1_800_000_000
    stats = {
        "sina_flash": _stats(900, 9000, now - 600),
        "eastmoney_ann": _stats(0, 26, now - 96 * 3600),
        "reuters_news": _stats(0, 0, None),
        "qinghong_flash": _stats(0, 0, None),
        "flaky_src": _stats(3, 10, now - 300),
    }
    reg = [
        {"source_id": "sina_flash", "label": "新浪", "status": "online",
         "total_ok": 900, "total_fail": 10},
        {"source_id": "eastmoney_ann", "label": "东财公告", "status": "stale",
         "total_ok": 26, "total_fail": 0},
        {"source_id": "reuters_news", "label": "路透", "status": "error",
         "total_ok": 0, "total_fail": 12},
        {"source_id": "qinghong_flash", "label": "青洪", "status": "error",
         "total_ok": 0, "total_fail": 3, "access_state": "needs_key"},
        {"source_id": "flaky_src", "label": "半残源", "status": "online",
         "total_ok": 3, "total_fail": 5},
    ]
    rep = source_health.build_health(reg, stats, now=now)
    by_src = {it["source"]: it for it in rep["items"]}
    assert by_src["sina_flash"]["class"] == "healthy"
    assert by_src["eastmoney_ann"]["class"] == "stalled"
    assert by_src["reuters_news"]["class"] == "dead"
    assert by_src["qinghong_flash"]["class"] == "not_enabled"
    assert by_src["flaky_src"]["class"] == "flaky"
    assert rep["counts"]["stalled"] >= 1
    assert rep["intake_24h"] == 903
    # 停更点名只含 stalled，不把死源混进去
    assert "eastmoney_ann" in [s["source"] for s in rep["stalled"]]


def test_source_health_works_without_registry():
    """注册表是内存态，CLI 里读出来是空的 —— 空也要能算（只靠 DB 侧）。"""
    now = 1_800_000_000
    rep = source_health.build_health([], {"x": _stats(10, 100, now - 600)}, now=now)
    assert rep["source_total"] == 1
    assert rep["items"][0]["class"] == "healthy"
