# -*- coding: utf-8 -*-
"""反思报告里"给证据 / 判错配 / 判时段"几个纯函数的契约测试（全离线）。

这批函数有个共同点：它们决定反思报告**说什么**，而不是怎么算。
写错不会崩，只会让报告悄悄骗人 —— 所以每条都对着一个具体的撒谎方式写断言：
  · 结算盈亏拿不到 → 不许判 WIN，只能 HOLD
  · LLM 给的 confidence 超出 0-1 → 必须夹紧，不能原样落库
  · evidence 引用了本期不存在的 #id → 必须标 fabricated（不删，但要点出来）
  · 加密 24h 交易 → 不许被判成"非活跃时段入场"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from review import review_agent as ra  # noqa: E402


# ── 真实持仓成败判定 ─────────────────────────────────────────────────────

def test_verdict_by_pnl_sign_only():
    assert ra._verdict_by_pnl(1.2) == "WIN"
    assert ra._verdict_by_pnl(0.01) == "WIN"
    assert ra._verdict_by_pnl(-0.01) == "LOSS"
    assert ra._verdict_by_pnl(0) == "HOLD"          # 平盘不算赢
    assert ra._verdict_by_pnl("0.5") == "WIN"       # 字符串数字要能吃


def test_verdict_by_pnl_dirty_value_is_hold_not_win():
    assert ra._verdict_by_pnl(None) == "HOLD"
    assert ra._verdict_by_pnl("") == "HOLD"
    assert ra._verdict_by_pnl("n/a") == "HOLD"
    assert ra._verdict_by_pnl(object()) == "HOLD"


# ── audit prompt 的交易行渲染 ─────────────────────────────────────────────

def test_format_trades_empty_placeholder():
    assert ra._format_trades_for_audit([]) == "（本期无可参考交易）"
    assert ra._format_trades_for_audit(None) == "（本期无可参考交易）"


def test_format_trades_strips_hash_and_marks_missing_pnl():
    trades = [
        {"id": 11, "asset": "BTC", "action": "BUY", "is_correct": "WIN",
         "forward_pnl": 1.234, "decision_context": '{"status": "ok"}',
         "news_text": "[hash:ab12cd] 美联储降息"},
        {"decision_id": 12, "asset": "ETH", "action": "SELL", "is_correct": "LOSS",
         "forward_pnl": None, "decision_context": "not-json", "news_text": ""},
    ]
    out = ra._format_trades_for_audit(trades)
    assert "#11 BTC BUY WIN pnl=+1.23%" in out
    assert "hash:" not in out and "ab12cd" not in out   # 哈希前缀必须剥掉
    assert "| ok |" in out                              # ctx JSON 取 status
    assert "#12 ETH SELL LOSS pnl=?" in out             # 缺盈亏不能写成 0.00%
    assert "共 2 笔" not in out                         # 不超上限不显示省略行


def test_format_trades_truncates_and_reports_total():
    trades = [{"id": i, "asset": "BTC", "action": "BUY"} for i in range(20)]
    out = ra._format_trades_for_audit(trades, max_n=3)
    assert out.count("\n") == 3                          # 3 行 + 1 行省略提示
    assert "仅展示前 3 条" in out and "共 20 笔" in out


def test_format_agg_none_winrate_is_na():
    assert ra._format_agg_for_audit({}) == "（无聚合数据）"
    agg = {"total": 10, "decided": 8, "wins": 5, "losses": 3,
           "winrate": None, "total_pnl": -1.5,
           "by_asset": {"BTC": {"total": 6, "pnl": -2.0}, "ETH": {"total": 4, "pnl": 0.5}}}
    out = ra._format_agg_for_audit(agg)
    assert "胜率 N/A" in out                             # 没有胜率不许编 0.0%
    assert "总盈亏 -1.50%" in out
    assert out.index("BTC(") < out.index("ETH(")        # 按笔数降序


def test_format_agg_by_asset_caps_at_five():
    agg = {"total": 6, "winrate": 0.5,
           "by_asset": {a: {"total": i + 1, "pnl": 0.1 * i}
                        for i, a in enumerate(["A", "B", "C", "D", "E", "F"])}}
    out = ra._format_agg_for_audit(agg)
    assert "A(" not in out.split("品种: ")[1]           # 只有 1 笔的被截掉
    assert out.count("笔/") == 5


# ── LLM 返回的 audit 结构规范化 ───────────────────────────────────────────

def test_normalize_non_dict_is_parse_failed():
    out = ra._normalize_audit_payload("not json", "claim")
    assert out["stop_reason"] == "parse_failed"
    assert out["evidence"] == [] and out["factual"] is None


def test_normalize_clamps_confidence_and_drops_nan():
    assert ra._normalize_audit_payload({"confidence": 1.8}, "c")["confidence"] == 1.0
    assert ra._normalize_audit_payload({"confidence": -3}, "c")["confidence"] == 0.0
    assert ra._normalize_audit_payload({"confidence": float("nan")}, "c")["confidence"] is None
    assert ra._normalize_audit_payload({"confidence": "high"}, "c")["confidence"] is None


def test_normalize_bool_from_strings_only_explicit():
    assert ra._normalize_audit_payload({"factual": "yes"}, "c")["factual"] is True
    assert ra._normalize_audit_payload({"factual": "False"}, "c")["factual"] is False
    assert ra._normalize_audit_payload({"factual": "maybe"}, "c")["factual"] is None
    assert ra._normalize_audit_payload({"factual": 1}, "c")["factual"] is None


def test_normalize_evidence_accepts_string_and_drops_blanks():
    assert ra._normalize_audit_payload({"evidence": "只有一条"}, "c")["evidence"] == ["只有一条"]
    out = ra._normalize_audit_payload({"evidence": ["a", "  ", None, 3]}, "c")
    assert out["evidence"] == ["a", "None", "3"]      # 空串丢掉，非空转字符串
    out2 = ra._normalize_audit_payload({"next_question": "   ", "stop_reason": "  "}, "c")
    assert out2["next_question"] is None and out2["stop_reason"] is None


# ── 证据里的伪造 #id ─────────────────────────────────────────────────────

def test_fabricated_evidence_flags_unseen_ids():
    trades = [{"id": 7}, {"decision_id": 9}]
    audit = {"evidence": ["见 #7 的资金流", "见 #404 的持仓", "没有编号的推断"]}
    assert ra._validate_audit_evidence(audit, trades) == ["见 #404 的持仓"]


def test_fabricated_evidence_one_flag_per_item():
    audit = {"evidence": ["#404 和 #500 都不在"]}
    assert ra._validate_audit_evidence(audit, []) == ["#404 和 #500 都不在"]  # 同一条只标一次


def test_fabricated_evidence_ignores_non_string_and_plain_text():
    audit = {"evidence": [{"text": "#404"}, "全是文字没有 id"]}
    assert ra._validate_audit_evidence(audit, [{"id": 1}]) == []
    assert ra._validate_audit_evidence({}, []) == []


# ── 非活跃时段判定 ───────────────────────────────────────────────────────

def test_detect_session_crypto_never_non_active():
    for a in ("BTC", "btc", "ETH", "SOLUSDT"):
        assert ra._detect_session(a, "2026-11-21T03:00:00") is None


def test_detect_session_weekend_and_night():
    out = ra._detect_session("XAU", "2026-11-21T10:00:00")   # 周六
    assert out["session"] == "non_active" and "周末" in out["detail"]
    out = ra._detect_session("XAU", "2026-11-23T23:30:00")   # 周一深夜
    assert out["session"] == "non_active" and "23:30" in out["detail"]
    out = ra._detect_session("XAU", "2026-11-23T03:05:00")
    assert out["session"] == "non_active"
    assert ra._detect_session("XAU", "2026-11-23T07:00:00") is None   # 07:00 已算活跃
    assert ra._detect_session("XAU", "2026-11-23T22:59:00") is None


def test_detect_session_dirty_input_is_none():
    assert ra._detect_session("XAU", "") is None
    assert ra._detect_session("XAU", None) is None
    assert ra._detect_session("XAU", "not-a-time") is None
    assert ra._detect_session("XAU", "2026-11-23") is None            # 只有日期也当无信息


# ── 新闻主体与交易品种错配 ──────────────────────────────────────────────

def test_cross_asset_mismatch_equity_news_on_crypto():
    assert ra._is_cross_asset_mismatch({"asset": "BTC", "news_text": "[美股 NVDA] 财报超预期"})
    assert ra._is_cross_asset_mismatch({"asset": "XAU", "content": "英伟达带动科技股上涨"})


def test_cross_asset_mismatch_ignores_non_crypto_commodity_and_clean_news():
    assert ra._is_cross_asset_mismatch({"asset": "AAPL", "news_text": "[美股 NVDA] 涨"}) is False
    assert ra._is_cross_asset_mismatch({"asset": "BTC", "news_text": "比特币突破 10 万美元"}) is False
    assert ra._is_cross_asset_mismatch({"asset": "BTC", "news_text": ""}) is False
    assert ra._is_cross_asset_mismatch({}) is False


# ── 官员表态识别 ─────────────────────────────────────────────────────────

def test_official_statement_zh_and_en():
    assert ra._is_official_statement("美联储主席：将维持利率不变") is True
    assert ra._is_official_statement("白宫表示将重新评估关税") is True
    assert ra._is_official_statement("US treasury official: tariffs to stay") is True
    assert ra._is_official_statement("苹果发布新一代 iPhone") is False
    assert ra._is_official_statement("") is False
    assert ra._is_official_statement(None) is False
