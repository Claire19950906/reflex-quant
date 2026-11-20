"""v333: 真值覆盖率的分母错了 —— 把 HOLD 算进去, 90% 目标天生不可达。

实测: ai_decisions 8865 行, 其中 8476 行是 HOLD。HOLD 没有押方向, 就没有
"对/错", 不可能有 truth_verdict。老口径的分母是全表, 覆盖率上限 = 389/8865 = 4.4%,
90% 目标永远达不到 —— 它把"指标定义错"伪装成了"数据不够", 进而让人误判
"权重不可信"。

真实数字: 355 条可结算有方向决策, 已判 328 条 = 92.4%。
另有 34 条有方向但标的是 INDIVIDUAL_STOCK / NONE 这类「桶」, 永远结算不了,
移出分母但必须单独报出来 (上游打标缺口, 不能装作不存在)。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_scorecard as sc


def _mk():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, target_asset TEXT, "
        "suggested_action TEXT, truth_verdict TEXT, forward_move_pct REAL)"
    )
    return conn


def _add(conn, asset, action, verdict="", fmv=None):
    conn.execute("INSERT INTO ai_decisions (target_asset, suggested_action, truth_verdict, "
                 "forward_move_pct) VALUES (?,?,?,?)", (asset, action, verdict, fmv))


def test_hold_rows_never_count_in_denominator():
    conn = _mk()
    for _ in range(10):
        _add(conn, "BTC", "HOLD", "", 1.2)      # HOLD 有行情真值, 但没有胜负
    _add(conn, "BTC", "BUY", "WIN", 1.0)
    _add(conn, "BTC", "BUY", "", 1.0)           # 有方向未判
    n, d = sc._q_truth_coverage(conn)
    assert (n, d) == (1.0, 2)


def test_bucket_asset_excluded_from_denominator():
    conn = _mk()
    _add(conn, "BTC", "BUY", "WIN", 1.0)
    _add(conn, "INDIVIDUAL_STOCK", "BUY", "", 1.0)   # 桶: 永远结算不了
    _add(conn, "NONE", "SELL", "", 1.0)
    n, d = sc._q_truth_coverage(conn)
    assert (n, d) == (1.0, 1)                        # 分母只剩那条具体品种


def test_bucket_gap_is_reported_not_hidden():
    conn = _mk()
    _add(conn, "INDIVIDUAL_STOCK", "BUY", "", 1.0)
    _add(conn, "NONE", "SELL", "", 1.0)
    _add(conn, "US_STOCK", "BUY", "WIN", 1.0)
    assert sc._q_truth_bucket_gap(conn) == (3.0, 3)


def test_lowercase_bucket_asset_is_still_a_bucket():
    conn = _mk()
    _add(conn, "individual_stock", "BUY", "", 1.0)
    n, d = sc._q_truth_coverage(conn)
    assert d == 0


def test_market_truth_coverage_still_counts_hold():
    """参考口径: 行情真值是"决策后市场怎么走", HOLD 也算, 只是不判胜负。"""
    conn = _mk()
    _add(conn, "BTC", "HOLD", "", 1.0)
    _add(conn, "BTC", "BUY", "WIN", 1.0)
    _add(conn, "BTC", "HOLD", "", None)
    n, d = sc._q_market_truth_coverage(conn)
    assert (n, d) == (2.0, 3)


def test_bucket_rows_do_not_enter_market_truth_denominator():
    conn = _mk()
    _add(conn, "NONE", "HOLD", "", 1.0)
    _add(conn, "BTC", "HOLD", "", 1.0)
    n, d = sc._q_market_truth_coverage(conn)
    assert (n, d) == (1.0, 1)


def test_real_population_ratio_is_honest():
    """328/355 = 92.4%: 这个数字能对得上库里的真实分布 (用于防止口径再次漂移)。"""
    conn = _mk()
    for _ in range(328):
        _add(conn, "XAU", "BUY", "WIN", 0.5)
    for _ in range(27):
        _add(conn, "WTI", "SELL", "", 0.5)
    n, d = sc._q_truth_coverage(conn)
    assert (n, d) == (328.0, 355)
    assert round(n / d * 100, 1) == 92.4
