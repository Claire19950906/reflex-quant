# -*- coding: utf-8 -*-
"""v321.2 修复项回归测试 (全离线, 不碰生产库)。

覆盖 4 个修复:
  1. news_score_worker._extract_codes_strict  —— 标题区精确品种提取 (防误判)
  2. news_score_worker._resolve_asset           —— 标题交叉校验 (防资产错配)
  3. capability_acceptance._market_beta_benchmark —— 验收 #2 的 β 剥离基准
  4. engine/main.strategy_reflection_loop        —— 策略反思调度接线
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ── 1. 标题区精确品种提取 ────────────────────────────────────────────────────
class TestExtractCodesStrict:
    def test_chinese_bitcoin_name(self):
        from news_score_worker import _extract_codes_strict
        assert _extract_codes_strict("比特币跌破8.4万美元") == {"BTC"}

    def test_bare_bit_prefix_does_not_match(self):
        """'比特' 单独出现 (如比特流) 不能命中 BTC。"""
        from news_score_worker import _extract_codes_strict
        assert _extract_codes_strict("比特流编码技术升级") == set()

    def test_ticker_must_be_standalone_word(self):
        from news_score_worker import _extract_codes_strict
        assert _extract_codes_strict("NVDA 财报超预期") == {"NVDA"}
        assert "NVDA" not in _extract_codes_strict("NVDAXX 股票")

    def test_prompt_line_in_body_is_ignored(self):
        """v321.2 核心: 正文尾部的 '[美股 NVDA]' 提示行不能作为品种指称。

        这正是首轮 --fix-asset 造成 809 条误改的根因
        (例: "马斯克/Model S" 被判成 NVDA)。
        """
        from news_score_worker import _extract_codes_strict
        title = ("[hash:abc123] 埃隆·马斯克正在缩减Cybertruck的产能，"
                 "以生产Cybercab\n[美股 NVDA] 来源:Finnhub")
        assert "NVDA" not in _extract_codes_strict(title)

    def test_hash_prefix_stripped(self):
        from news_score_worker import _extract_codes_strict
        assert _extract_codes_strict("[hash:deadbeef] 比特币ETF净流出") == {"BTC"}
# ── 2. 资产交叉校验 ──────────────────────────────────────────────────────────
class TestResolveAssetCrossCheck:
    """_resolve_asset: 已有 asset 不再无条件信任, 与标题交叉校验。"""

    @staticmethod
    def _row(asset):
        class R(dict):
            def __getitem__(self, k):
                return dict.get(self, k)
        return R({"asset": asset, "news_id": 1, "headline": "", "body": ""})

    @staticmethod
    def _ctx():
        return {"raw": {}, "sca": {}, "hermes": {}, "ai_sent": {}}

    def test_wrong_gate_label_corrected_by_title(self):
        """标题讲 BTC 但 asset 记成 WTI → 改成 BTC (这是 400 条错配的修复)。"""
        from news_score_worker import _resolve_asset
        row = self._row("WTI")
        text = "比特币现货 ETF 本周净流出 12.8 亿美元"
        assert _resolve_asset(row, text, self._ctx()) == "BTC"

    def test_consistent_asset_kept(self):
        from news_score_worker import _resolve_asset
        row = self._row("BTC")
        assert _resolve_asset(row, "比特币持续走高", self._ctx()) == "BTC"

    def test_multiple_assets_conservative(self):
        """标题提多个品种 → 保守保留原值, 不猜。"""
        from news_score_worker import _resolve_asset
        row = self._row("WTI")
        assert _resolve_asset(row, "比特币与以太坊双双大跌", self._ctx()) == "WTI"

    def test_no_title_match_keeps_original(self):
        """标题没提任何品种 → 保留原值 (板块标签是有意义的信息, 不改)。"""
        from news_score_worker import _resolve_asset
        row = self._row("个股")
        text = "为什么人工智能的模糊定义对投资者至关重要"
        assert _resolve_asset(row, text, self._ctx()) == "个股"

    def test_body_prompt_line_not_used(self):
        """正文尾部的 ticker 提示行不能触发改写。"""
        from news_score_worker import _resolve_asset
        row = self._row("个股")
        text = "马斯克正在缩减Cybertruck的产能\n[美股 NVDA] 来源:Finnhub"
        assert _resolve_asset(row, text, self._ctx()) == "个股"


# ── 3. 验收 #2 的 β 剥离基准 ────────────────────────────────────────────────
class TestMarketBetaBenchmark:
    def test_benchmark_computed_from_cross_section(self):
        import capability_acceptance as ca
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE ai_decisions (ts INTEGER, forward_move_pct REAL)")
        # 同一小时: 5 个 +2.0, 1 个 -10.0 → 中位数 = 2.0 (抗极值)
        for i in range(5):
            conn.execute("INSERT INTO ai_decisions VALUES (?, ?)", (1000 + i, 2.0))
        conn.execute("INSERT INTO ai_decisions VALUES (?, ?)", (1005, -10.0))
        bench = ca._market_beta_benchmark(conn)
        assert bench
        assert list(bench.values())[0] == pytest.approx(2.0)
        conn.close()

    def test_missing_table_returns_empty(self):
        import capability_acceptance as ca
        conn = sqlite3.connect(":memory:")
        assert ca._market_beta_benchmark(conn) == {}
        conn.close()

    def test_excess_uses_signed_value(self):
        """v321.2 P0 回归: 超额必须用**带符号**的收益减基准。

        基准是同小时所有决策收益的**中位数** (有正有负)。若拿 ABS 后的值
        去减, 溢出会变成 |x| - median(|x|) —— 符号信息全丢, 普跌窗口里
        相关性直接归零 (实测 ρ 从 +0.2215 掉到 +0.0537)。

        这里构造一个符号敏感的场景: 同小时 3 条决策收益 +2/+2/-10,
        中位数 = +2。若用 ABS 算基准 (median(2,2,10)=2) 恰好相同, 所以
        再加一条 +5 的, 让两种基准不同:
          带符号中位数 = median(-10, 2, 2, 5) = 2
          ABS 中位数   = median(2, 2, 5, 10) = 3.5
        """
        import capability_acceptance as ca
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ai_decisions (id INTEGER, ts INTEGER, forward_move_pct REAL, "
            "signal_weight REAL, decision_space TEXT, trigger_kind TEXT)")
        # 4 条决策, 同一小时 (ts=1000..1003)
        data = [(1, 1000, 2.0, 0.9), (2, 1001, 2.0, 0.7),
                (3, 1002, -10.0, 0.3), (4, 1003, 5.0, 0.5)]
        for did, ts, fmv, w in data:
            conn.execute(
                "INSERT INTO ai_decisions VALUES (?,?,?,?,?,?)",
                (did, ts, fmv, w, "ai", "insert"))
        conn.execute("CREATE TABLE signal_weight_history "
                     "(id INTEGER, ai_decision_id INTEGER, decision_space TEXT, "
                     "trigger_kind TEXT, signal_weight REAL, weight_tier TEXT)")
        for did, ts, fmv, w in data:
            conn.execute(
                "INSERT INTO signal_weight_history VALUES (?,?,?,?,?,?)",
                (did, did, "ai", "insert", w, "medium"))
        bench = ca._market_beta_benchmark(conn)
        assert list(bench.values())[0] == pytest.approx(2.0), \
            "基准必须用带符号收益的中位数"

        stats = ca._weight_eval_stats(conn)
        # 超额口径必须被算出来 (说明用了 fmv_signed 而不是 ABS 后的值)
        assert stats["ex_ante_n_excess"] == 4
        assert stats["rho_excess"] is not None
        conn.close()


# ── 5. belief 资产锚点匹配 (#4B) ─────────────────────────────────────────────
class TestBeliefAssetAnchor:
    """v321.2: belief 专属事实源 (ai_decisions/news_events) 按资产查出,
    thesis 提到该资产即算匹配 —— 否则 9/14 条 belief 恒 0 证据。"""

    @staticmethod
    def _fact(source, asset, direction="up", sid=1):
        return {"asset": asset, "indicator": source, "direction": direction,
                "source": source, "source_id": sid, "timestamp": "1000",
                "magnitude": 0.5, "regime_tags": []}

    def test_thesis_mentioning_asset_matches(self):
        """thesis 说 BTC → BTC 的 ai_decisions 事实应被匹配。

        方向: thesis "比特币持续上涨" = up, 事实 up → 记为支持。
        """
        from belief_store import _verify_belief_chain
        facts = [self._fact("ai_decisions", "BTC", "up", 1)]
        out = _verify_belief_chain("比特币持续上涨动能充足", facts)
        assert out["matched_facts"], "thesis 提到 BTC 却没匹配上 BTC 的事实"
        assert out["support_count"] >= 1
        assert out["chain_direction"] == "up"

    def test_thesis_mentioning_chinese_alias(self):
        from belief_store import _verify_belief_chain
        facts = [self._fact("ai_decisions", "BTC", "up", 1)]
        out = _verify_belief_chain("比特币跌破关键支撑", facts)
        assert out["matched_facts"], "中文别名 (比特币) 应能匹配"

    def test_other_asset_not_matched(self):
        """thesis 说 ETH, 事实是 BTC → 不该匹配 (不能跨资产乱配)。"""
        from belief_store import _verify_belief_chain
        facts = [self._fact("ai_decisions", "BTC", "up", 1)]
        out = _verify_belief_chain("以太坊质押收益率先回落", facts)
        assert not out["matched_facts"]

    def test_direction_comparison_preserved(self):
        """匹配上了不等于算支持 —— 方向相反仍应记为 contradict。"""
        from belief_store import _verify_belief_chain
        facts = [self._fact("ai_decisions", "BTC", "down", 1)]
        out = _verify_belief_chain("比特币持续上涨动能充足", facts)
        assert out["matched_facts"], "应匹配"
        assert out["contradict_count"] >= 1, "方向相反必须记为反驳"

    def test_external_indicators_unaffected(self):
        """external_indicators 事实不走资产锚点 (它的锚点是 indicator 名)。"""
        from belief_store import _verify_belief_chain
        facts = [{"asset": "BTC", "indicator": "^VIX", "direction": "up",
                  "source": "external_indicators", "source_id": 9,
                  "timestamp": "1000", "magnitude": 0.5, "regime_tags": []}]
        out = _verify_belief_chain("比特币持续上涨动能充足", facts)
        assert not out["matched_facts"]


# ── 6. 策略反思调度接线 ──────────────────────────────────────────────────────
class TestStrategyReflectionWiring:
    def test_loop_registered_in_engine_tasks(self):
        """engine/main.py 的 tasks 列表必须包含 strategy_reflection_loop。

        回归: 以前**完全没有调度注册** → strategy_reflections 停在 2026-09-30。
        """
        path = os.path.join(_SRC, "engine", "main.py")
        text = open(path, encoding="utf-8").read()
        assert "strategy_reflection_loop" in text, "strategy_reflection_loop 未定义"
        assert 'name="strategy_reflection_loop"' in text, \
            "strategy_reflection_loop 未注册进 tasks (调度缺口回归)"

    def test_loop_is_async(self):
        """必须是 async —— 原 run_strategy_reflection_daemon 是阻塞式 time.sleep,
        直接放进 asyncio 会卡死整个事件循环。"""
        import ast
        path = os.path.join(_SRC, "engine", "main.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
        assert "strategy_reflection_loop" in fns
        assert isinstance(fns["strategy_reflection_loop"], ast.AsyncFunctionDef), \
            "strategy_reflection_loop 必须是 async"


    def test_empty_and_none(self):
        from news_score_worker import _extract_codes_strict
        assert _extract_codes_strict("") == set()
        assert _extract_codes_strict(None) == set()

    def test_multiple_assets_returns_all(self):
        from news_score_worker import _extract_codes_strict
        got = _extract_codes_strict("比特币与以太坊双双大跌")
        assert got == {"BTC", "ETH"}
