"""Tests for backtest_stats — 回测显著性分析。

覆盖:
- _is_win 映射(True/False/None 三态)
- _bootstrap_ci 二项胜率 CI
- _spearman 等级相关
- _percentile 分位数
- _equity_curve 回撤与夏普
- _bucket_hour 小时提取
- _extract_row_features 拉平
- _group_by 分组
- analyze 端到端
- format_markdown 渲染
- CLI main(--format md/json/--asset/--out/缺 db)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import backtest_stats as bs  # noqa: E402


def _row(rid, action="BUY", asset="BTCUSDT", category="CRYPTO",
         sentiment=0.5, forward_pnl=0.01, mfe=0.02, mae=-0.005,
         is_correct="TRUE", created_at="2025-09-26 21:00:00",
         event_strength="medium", prediction_type="continuation"):
    return {
        "id": rid,
        "target_asset": asset,
        "market_category": category,
        "suggested_action": action,
        "sentiment_score": sentiment,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "forward_pnl": forward_pnl,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "is_correct": is_correct,
        "settled": 1,
        "created_at": created_at,
        "prediction_type": prediction_type,
        "event_strength": event_strength,
        "reasoning": "test",
    }


class TestIsWin(unittest.TestCase):
    def test_true_variants(self):
        for s in ("TRUE", "true", "yes", "1", "WIN", "correct", "T"):
            self.assertIs(bs._is_win(s), True, f"expected True for {s!r}")

    def test_false_variants(self):
        for s in ("FALSE", "false", "no", "0", "LOSS", "wrong", "F"):
            self.assertIs(bs._is_win(s), False, f"expected False for {s!r}")

    def test_unknown_variants(self):
        for s in ("", "  ", "maybe", "0.5"):
            self.assertIsNone(bs._is_win(s), f"expected None for {s!r}")

    def test_none_safe(self):
        self.assertIsNone(bs._is_win(None))


class TestBootstrapCI(unittest.TestCase):
    def test_empty(self):
        p, lo, hi = bs._bootstrap_ci([])
        self.assertEqual((p, lo, hi), (0.0, 0.0, 0.0))

    def test_all_wins(self):
        p, lo, hi = bs._bootstrap_ci([True] * 30)
        self.assertEqual(p, 1.0)
        self.assertGreaterEqual(lo, 0.95)
        self.assertEqual(hi, 1.0)

    def test_all_losses(self):
        p, lo, hi = bs._bootstrap_ci([False] * 30)
        self.assertEqual(p, 0.0)
        self.assertEqual(lo, 0.0)
        self.assertLessEqual(hi, 0.05)

    def test_balanced(self):
        samples = [True, False] * 25
        p, lo, hi = bs._bootstrap_ci(samples, n_bootstrap=2000)
        self.assertAlmostEqual(p, 0.5, places=2)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)

    def test_ci_includes_point_estimate(self):
        samples = [True] * 7 + [False] * 3
        p, lo, hi = bs._bootstrap_ci(samples, n_bootstrap=2000)
        self.assertLessEqual(lo, p)
        self.assertGreaterEqual(hi, p)


class TestSpearman(unittest.TestCase):
    def test_perfect_positive(self):
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        ic = bs._spearman(x, y)
        self.assertAlmostEqual(ic, 1.0, places=4)

    def test_perfect_negative(self):
        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        ic = bs._spearman(x, y)
        self.assertAlmostEqual(ic, -1.0, places=4)

    def test_too_few_samples(self):
        self.assertIsNone(bs._spearman([1, 2], [3, 4]))
        self.assertIsNone(bs._spearman([1], [1]))

    def test_zero_variance(self):
        ic = bs._spearman([1, 2, 3], [5, 5, 5])
        self.assertIsNone(ic)

    def test_ties_handled_correctly(self):
        """ties 走 numpy 路径时平均秩处理应与纯 Python 一致。"""
        x = [1, 1, 2, 3, 3, 3, 4]
        y = [4, 4, 3, 2, 1, 0, -1]
        ic = bs._spearman(x, y)
        # 强负相关,数值上 ≈ -0.964(average tie 处理不让它到 -1)
        self.assertIsNotNone(ic)
        self.assertLess(ic, -0.9)

    def test_rank_average_np_matches_python(self):
        """_rank_average_np 对 ties 的处理与纯 Python rank 等价。"""
        import numpy as np
        x = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
        xa = np.asarray(x, dtype=np.float64)
        ranks_np = bs._rank_average_np(xa)
        # 手动算:1-based 平均秩
        # sorted x with indices: 1(1),4(2),3(3),9(4),2(5),6(6),5(7),8(8),10(9) -> 1,1,2,2,3,3,3,3,4,5,5
        # wait let me recount
        # values: [3,1,4,1,5,9,2,6,5,3,5]
        # sorted: [1,1,2,3,3,4,5,5,5,6,9]
        # indices: [1,3,6,0,9,2,4,7,10,5,8]
        # ranks: 1, 1.5(avg 1,2), 3, 4(avg 4,5), 4(avg 4,5), 6, 7(avg 7,8,9), 7, 7, 10, 11
        # 1-based, sorted positions 1..11
        # group at sorted[0..1] (val 1): pos 1,2, avg rank = 1.5
        # group at sorted[2] (val 2): pos 3, rank = 3
        # group at sorted[3..4] (val 3): pos 4,5, avg rank = 4.5
        # group at sorted[5] (val 4): pos 6, rank = 6
        # group at sorted[6..8] (val 5): pos 7,8,9, avg rank = 8
        # group at sorted[9] (val 6): pos 10, rank = 10
        # group at sorted[10] (val 9): pos 11, rank = 11
        # 映射回原顺序 [3,1,4,1,5,9,2,6,5,3,5]:
        # idx 0: 3 → 4.5
        # idx 1: 1 → 1.5
        # idx 2: 4 → 6
        # idx 3: 1 → 1.5
        # idx 4: 5 → 8
        # idx 5: 9 → 11
        # idx 6: 2 → 3
        # idx 7: 6 → 10
        # idx 8: 5 → 8
        # idx 9: 3 → 4.5
        # idx 10: 5 → 8
        expected = [4.5, 1.5, 6, 1.5, 8, 11, 3, 10, 8, 4.5, 8]
        np.testing.assert_allclose(ranks_np, expected)


class TestPercentile(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bs._percentile([], 0.5), 0.0)

    def test_endpoints(self):
        v = [1, 2, 3, 4, 5]
        self.assertEqual(bs._percentile(v, 0.0), 1)
        self.assertEqual(bs._percentile(v, 1.0), 5)

    def test_median(self):
        v = [1, 2, 3, 4, 5, 6]
        self.assertAlmostEqual(bs._percentile(v, 0.5), 3.5)


class TestEquityCurve(unittest.TestCase):
    def test_empty(self):
        eq = bs._equity_curve([])
        self.assertEqual(eq["n"], 0)
        self.assertEqual(eq["total_pnl"], 0.0)

    def test_monotonic_up(self):
        eq = bs._equity_curve([0.01, 0.02, 0.03])
        self.assertAlmostEqual(eq["total_pnl"], 0.06, places=4)
        self.assertEqual(eq["max_drawdown"], 0.0)

    def test_drawdown(self):
        eq = bs._equity_curve([0.05, -0.10, 0.02])
        self.assertAlmostEqual(eq["total_pnl"], -0.03, places=4)
        self.assertAlmostEqual(eq["max_drawdown"], 0.10, places=4)

    def test_sharpe_basic(self):
        eq = bs._equity_curve([0.01] * 10)
        self.assertEqual(eq["sharpe"], 0.0)

    def test_equity_constant_numerical_stability(self):
        """numpy 路径下 std_p 为浮点极小值时不应爆炸(epsilon 兜底)。"""
        # 真实场景:多个 trade 收益完全相同(std 浮点 ≈ 1e-18)
        eq = bs._equity_curve([0.001] * 100)
        self.assertEqual(eq["sharpe"], 0.0)
        self.assertEqual(eq["total_pnl"], round(0.001 * 100, 4))
        self.assertEqual(eq["max_drawdown"], 0.0)

    def test_equity_large_input(self):
        """n=10000 走 numpy 路径不应崩,数值与纯 Python 一致。"""
        import random as _r
        _r.seed(7)
        pnls = [_r.gauss(0.001, 0.02) for _ in range(10000)]
        eq = bs._equity_curve(pnls)
        self.assertGreater(eq["n"], 0)
        self.assertGreater(eq["sharpe"], 0)  # mean=0.001 > 0
        self.assertGreater(eq["max_drawdown"], 0)


class TestBucketHour(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(bs._bucket_hour("2025-09-26 21:35:00"), 21)
        self.assertEqual(bs._bucket_hour("2025-09-26T08:00:00Z"), 8)

    def test_empty(self):
        self.assertIsNone(bs._bucket_hour(""))

    def test_garbage(self):
        self.assertIsNone(bs._bucket_hour("not-a-date"))


class TestExtractRowFeatures(unittest.TestCase):
    def test_basic(self):
        rows = [_row(1, action="BUY", forward_pnl=0.05, mfe=0.08, mae=-0.02)]
        feats = bs._extract_row_features(rows)
        self.assertEqual(len(feats), 1)
        f = feats[0]
        self.assertEqual(f["action"], "BUY")
        self.assertEqual(f["asset"], "BTCUSDT")
        self.assertIs(f["win"], True)
        self.assertEqual(f["forward_pnl"], 0.05)
        self.assertEqual(f["mfe_pct"], 0.08)


class TestGroupBy(unittest.TestCase):
    def test_groups(self):
        rows = [
            {"a": "x", "v": 1}, {"a": "x", "v": 2},
            {"a": "y", "v": 3}, {"a": None, "v": 4},
        ]
        g = bs._group_by(rows, "a")
        self.assertEqual(set(g.keys()), {"x", "y", "unknown"})
        self.assertEqual(len(g["x"]), 2)
        self.assertEqual(len(g["y"]), 1)
        self.assertEqual(len(g["unknown"]), 1)


class TestAnalyze(unittest.TestCase):
    def test_empty(self):
        stats = bs.analyze([])
        self.assertEqual(stats["total_decisions"], 0)
        self.assertEqual(stats["win"], 0)
        self.assertEqual(stats["overall_win_rate"], 0.0)

    def test_all_win(self):
        rows = [_row(i, forward_pnl=0.01, is_correct="TRUE") for i in range(20)]
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=500)
        self.assertEqual(stats["total_decisions"], 20)
        self.assertEqual(stats["win"], 20)
        self.assertEqual(stats["loss"], 0)
        self.assertEqual(stats["overall_win_rate"], 1.0)
        self.assertEqual(stats["significance"], "显著正期望")
        self.assertIn("BUY", stats["by_action"])
        self.assertIn("BTCUSDT", stats["by_asset"])

    def test_mixed_with_unknown(self):
        rows = []
        for i in range(10):
            rows.append(_row(i, is_correct="TRUE", forward_pnl=0.01))
        for i in range(10, 20):
            rows.append(_row(i, is_correct="FALSE", forward_pnl=-0.01))
        for i in range(20, 25):
            rows.append(_row(i, is_correct="", forward_pnl=None))
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=500)
        self.assertEqual(stats["total_decisions"], 25)
        self.assertEqual(stats["win"], 10)
        self.assertEqual(stats["loss"], 10)
        self.assertEqual(stats["unknown"], 5)
        self.assertAlmostEqual(stats["overall_win_rate"], 0.5, places=2)

    def test_grouping_dimensions(self):
        rows = [
            _row(1, action="BUY", asset="BTCUSDT", category="CRYPTO"),
            _row(2, action="BUY", asset="BTCUSDT", category="CRYPTO"),
            _row(3, action="SELL", asset="ETHUSDT", category="CRYPTO"),
            _row(4, action="BUY", asset="XAU", category="GOLD"),
        ]
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=200)
        self.assertEqual(set(stats["by_action"].keys()), {"BUY", "SELL"})
        self.assertEqual(set(stats["by_asset"].keys()), {"BTCUSDT", "ETHUSDT", "XAU"})
        self.assertEqual(set(stats["by_category"].keys()), {"CRYPTO", "GOLD"})
        self.assertIn("21", stats["by_hour"])

    def test_mfe_mae_distribution(self):
        rows = [
            _row(i, mfe=0.01 * (i + 1), mae=-0.005 * (i + 1))
            for i in range(10)
        ]
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=100)
        self.assertIsNotNone(stats["mfe_distribution"])
        self.assertEqual(stats["mfe_distribution"]["n"], 10)
        self.assertGreater(stats["mfe_distribution"]["median"], 0)


class TestFormatMarkdown(unittest.TestCase):
    def test_basic_render(self):
        rows = [_row(i, forward_pnl=0.01, is_correct="TRUE") for i in range(5)]
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=100)
        md = bs.format_markdown(stats, lookback_days=30)
        self.assertIn("# 回测显著性报告", md)
        self.assertIn("## 1. 样本概览", md)
        self.assertIn("## 2. 按 Action 分组", md)
        self.assertIn("## 3. 按 Asset 分组", md)
        self.assertIn("| MFE%", md)
        self.assertIn("不构成投资建议", md)


class TestFormatJson(unittest.TestCase):
    def test_roundtrip(self):
        rows = [_row(i, forward_pnl=0.01) for i in range(3)]
        feats = bs._extract_row_features(rows)
        stats = bs.analyze(feats, n_bootstrap=50)
        j = bs.format_json(stats)
        parsed = json.loads(j)
        self.assertEqual(parsed["total_decisions"], 3)


class TestCLI(unittest.TestCase):
    def _make_db(self, tmpdir: str) -> str:
        path = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE ai_decisions (
                id INTEGER PRIMARY KEY,
                target_asset TEXT,
                market_category TEXT,
                suggested_action TEXT,
                sentiment_score REAL,
                entry_price REAL,
                exit_price REAL,
                forward_pnl REAL,
                mfe_pct REAL,
                mae_pct REAL,
                is_correct TEXT,
                settled INTEGER,
                created_at TEXT,
                prediction_type TEXT,
                event_strength TEXT,
                reasoning TEXT
            )
        """)
        for i in range(5):
            conn.execute(
                "INSERT INTO ai_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, "BTCUSDT", "CRYPTO", "BUY", 0.5, 100.0, 101.0, 0.01, 0.02,
                 -0.005, "TRUE", 1, "2025-09-26 21:00:00", "continuation", "medium", ""),
            )
        conn.commit()
        conn.close()
        return path

    def test_cli_json_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(tmp)
            rc = bs.main(["--days", "365", "--format", "json",
                          "--n-bootstrap", "100", "--db", db])
            self.assertEqual(rc, 0)

    def test_cli_md_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(tmp)
            out = os.path.join(tmp, "report.md")
            rc = bs.main(["--days", "365", "--format", "md",
                          "--n-bootstrap", "100", "--db", db, "--out", out])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("# 回测显著性报告", content)

    def test_cli_missing_db(self):
        rc = bs.main(["--days", "30", "--db", "/nonexistent/path.db"])
        self.assertEqual(rc, 1)

    def test_cli_asset_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._make_db(tmp)
            rc = bs.main(["--days", "365", "--format", "json",
                          "--n-bootstrap", "50", "--db", db, "--asset", "BTCUSDT"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()