# -*- coding: utf-8 -*-
"""v409 — sizing 换算 + 降级原因码跟随标的。

守的边界：
1. 没有上下文（ATR% / 权益）时，sizing.* 一律 observe_only，且原因写清缺什么；
2. 有 ATR% 且文案给了百分比，能换算出 sl_mult 并变成可执行假设；
3. 模型判成 NONE、路由补了代码 → 原因记 asset_late_bound（不是 target_none）；
   桶标签仍算 target_none（它本来就没有代码）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import insight_hypothesis as ih
import param_key_aliases as pka


def test_convert_requires_context():
    canon, val, bucket, why = pka.convert_value("sizing.stop_loss_pct", 1.5, None)
    assert canon is None and bucket == "unmappable"
    assert "atr_pct" in why
    assert pka.missing_context("sizing.stop_loss_pct", {}) == ("atr_pct",)


def test_convert_with_context():
    canon, val, bucket, why = pka.convert_value(
        "sizing.stop_loss_pct", 1.5, {"atr_pct": 0.5})
    assert canon == "sl_mult" and bucket == "strategy"
    assert abs(float(val) - 3.0) < 1e-9
    canon2, val2, _, _ = pka.convert_value(
        "sizing.position_weight", 0.1, {"equity_usdt": 10000})
    assert canon2 == "notional_usdt" and abs(float(val2) - 1000.0) < 1e-6


def test_classify_sizing_without_context_is_observe_only():
    c = ih.classify("[sizing.stop_loss_pct] 止损放宽到 1.5%", "BTC", 40, context=None)
    assert c["observe_only"] == 1
    assert "atr_pct" in c["observe_reason"]


def test_classify_sizing_with_context_becomes_executable():
    c = ih.classify("[sizing.stop_loss_pct] 止损放宽到 1.5%", "BTC", 40,
                    context={"atr_pct": 0.5})
    assert c["observe_only"] == 0
    assert c["param_bucket"] == "strategy"
    assert "sl_mult" in c["hypothesis"]


def test_classify_one_uses_market_context():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kline_snapshots(symbol TEXT, timeframe TEXT, "
                 "atr_pct REAL, ts INTEGER)")
    conn.execute("INSERT INTO kline_snapshots VALUES ('BTC','1h',0.5,1)")
    conn.execute("CREATE TABLE review_insights (id INTEGER PRIMARY KEY, rule TEXT, "
                 "asset TEXT, source_samples INTEGER, insight_classified_at TEXT, "
                 "insight_hypothesis TEXT, insight_param_key TEXT, insight_param_bucket TEXT, "
                 "insight_direction INTEGER, insight_window_h INTEGER, observe_only INTEGER, "
                 "observe_reason TEXT)")
    cur = conn.execute("INSERT INTO review_insights (rule, asset, source_samples, "
                       "insight_classified_at, observe_only) VALUES "
                       "('[sizing.stop_loss_pct] 止损放宽到 1.5%','BTC',40,'',0)")
    assert ih.classify_one(conn, cur.lastrowid) == 1
    row = conn.execute("SELECT observe_only, insight_param_bucket FROM review_insights "
                       "WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row[0] == 0 and row[1] == "strategy"


def test_degraded_reason_follows_asset():
    from engine.ai_worker import classify_analysis_degraded as f
    txt = "[总框架] 目标资产 NONE（market_category=）；该标的不在核心监控清单"
    assert f(txt, "", "XAU") == (1, "asset_late_bound")
    assert f(txt, "", "NONE") == (1, "target_none")
    assert f(txt, "", "CN_STOCK") == (1, "target_none")
    assert f(txt, "", "") == (1, "target_none")


# ── v409.1：asset 空串时从正文认品种（活体 1267 那条就是这么退化的） ──────────

def test_asset_from_text_reads_symbol_in_body():
    assert ih.asset_from_text("[sizing.stop_loss_pct] WTI 波动大，止损收窄至 0.6%~0.8%") == "WTI"
    assert ih.asset_from_text("[sizing.stop_loss_pct] 黄金波动放大，止损收窄到 1.0%") == "XAU"
    assert ih.asset_from_text("[sizing.take_profit_pct] 盈利单兑现过少，分批止盈") == ""


def test_asset_from_text_uses_word_boundary():
    # "SOLUTION" 里的 SOL 不能当品种
    assert ih.asset_from_text("[sizing.stop_loss_pct] SOLUTION 方案下止损收窄到 1.0%") == ""


def test_market_context_falls_back_to_rule_text():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kline_snapshots(symbol TEXT, timeframe TEXT, "
                 "atr_pct REAL, ts INTEGER)")
    conn.execute("INSERT INTO kline_snapshots VALUES ('WTI','1h',0.455,1)")
    ctx = ih.market_context(conn, "", "[sizing.stop_loss_pct] WTI 波动大，止损收窄至 0.6%")
    assert abs(ctx["atr_pct"] - 0.455) < 1e-9
    # 正文没有品种 → 不给 atr_pct（继续 observe_only）；
    # v409.4 起 equity_usdt 不依赖 asset，会一直在上下文里（纸盘初始权益）
    ctx2 = ih.market_context(conn, "", "[sizing.stop_loss_pct] 止损收窄至 0.6%")
    assert "atr_pct" not in ctx2
    assert ctx2.get("equity_usdt", 0) > 0


def test_classify_empty_asset_but_text_has_symbol_becomes_executable():
    c = ih.classify("[sizing.stop_loss_pct] WTI 波动大，止损收窄至 0.6%", "", 40,
                    context={"atr_pct": 0.455})
    assert c["observe_only"] == 0
    assert c["param_bucket"] == "strategy"
    assert "sl_mult" in c["hypothesis"]


def test_classify_empty_asset_no_symbol_stays_observe_only():
    c = ih.classify("[sizing.take_profit_pct] 盈利单兑现过少，分批止盈", "", 40, context={})
    assert c["observe_only"] == 1
    assert "atr_pct" in c["observe_reason"]


# ── v409.1：百分比语义门（账户风险占比 ≠ 价格止损幅度） ───────────────────────

def test_account_risk_pct_is_not_converted_to_sl_mult():
    # 「账户0.3%—0.5%」是风险占比；前面 14 字内没有止损/止盈幅度词 → 拒绝
    c = ih.classify("[sizing.stop_loss_pct] WTI单笔风险控制在账户0.3%—0.5%，按止损距离反推仓位",
                    "WTI", 40, context={"atr_pct": 0.452})
    assert c["observe_only"] == 1
    assert "账户风险" in c["observe_reason"] or "仓位比例" in c["observe_reason"]


def test_out_of_range_sl_mult_is_rejected():
    # 3.2%/0.452 ≈ 7.08 倍 ATR，超出 0.2~4 → 疑似账户回撤预算，拒绝
    c = ih.classify("[sizing.stop_loss_pct] 单笔止损须预先设定，按约3.2%亏损反推并执行",
                    "WTI", 40, context={"atr_pct": 0.452})
    assert c["observe_only"] == 1
    assert "超出可解释区间" in c["observe_reason"]


def test_in_range_sl_mult_still_converts():
    c = ih.classify("[sizing.stop_loss_pct] WTI 波动大，单笔止损收窄至 0.6%",
                    "WTI", 40, context={"atr_pct": 0.452})
    assert c["observe_only"] == 0
    assert "sl_mult" in c["hypothesis"]
