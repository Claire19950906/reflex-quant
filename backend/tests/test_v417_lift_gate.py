# -*- coding: utf-8 -*-
"""v417 · 因果链闸门改「超额口径」用例

锁四件事（都是 2026-11-18 实测出来的真问题）：
  1. 绝对命中率不可当闸门 —— 单边行情里 75% 命中可能仍然跑输基准（lift 为负）；
  2. 同样的 75%，在震荡行情里是 +25pp 的超额 → 应该入 strong 档；
  3. p 值门槛真的会挡人：超额够了但不显著（样本太薄 / 太接近基准）不许开闸；
  4. BOTH（幅度）链达标只能标 strong_mag，永远不许冒充"可定方向"。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import chain_prior as cp


def _make_conn(asset_series: dict[str, list[float]], chains: list[tuple],
               outcomes: list[tuple]) -> sqlite3.Connection:
    """搭一个最小真库：行情 + 链 + outcome。

    chains: [(id, asset, direction, hit, miss)]；outcomes: [(chain_id, status)]
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE causal_chains (id INTEGER, asset TEXT,"
                 " expected_direction TEXT, hit_count INTEGER, miss_count INTEGER,"
                 " confidence REAL)")
    conn.execute("CREATE TABLE causal_chain_outcomes (chain_id TEXT, status TEXT)")
    for asset, closes in asset_series.items():
        t0 = 1_700_000_000
        rows = [(asset, t0 + i * 86400, float(c)) for i, c in enumerate(closes)]
        conn.executemany("INSERT INTO ohlcv_daily VALUES (?,?,?)", rows)
    for cid, asset, d, hit, miss in chains:
        conn.execute("INSERT INTO causal_chains VALUES (?,?,?,?,?,?)",
                     (cid, asset, d, hit, miss, 0.5))
    for cid, status in outcomes:
        conn.execute("INSERT INTO causal_chain_outcomes VALUES (?,?)", (cid, status))
    return conn


def _hits_misses(cid, hits, misses):
    return [(str(cid), "HIT")] * hits + [(str(cid), "MISS")] * misses


def _rising(n=120):
    return [100 + i for i in range(n)]


def _choppy(n=120):
    """零漂移震荡序列（正弦，p_up(5 日窗口) ≈ 0.50）——
    用来验证"同样的命中率在震荡行情里才算超额"。"""
    import math
    return [100 + 5 * math.sin(i / 1.5) for i in range(n)]


def test_binom_p_is_sane():
    # 14/20 对基准 0.44 → 显著；10/20 对基准 0.5 → 就是抛硬币
    assert cp._binom_p_greater(14, 20, 0.44) < 0.05
    assert cp._binom_p_greater(10, 20, 0.5) > 0.3
    # 参数异常时永远不显著（宁可不开闸）
    assert cp._binom_p_greater(10, 0, 0.5) == 1.0
    assert cp._binom_p_greater(10, 20, 1.0) == 1.0


def test_high_hit_rate_in_one_way_market_is_not_strong():
    """单边上涨行情里，UP 链 15/20=75% 仍然跑输基准 → 不许入 strong。

    这是旧口径（Wilson 下界>0.5）最致命的地方：它会夸这条链"很强"，
    而它只是跟着行情漂。
    """
    conn = _make_conn({"RISE417": _rising()},
                      [(1, "RISE417", "UP", 15, 5)],
                      _hits_misses(1, 15, 5))
    rows = [{"id": 1, "asset": "RISE417", "expected_direction": "UP",
             "hit_count": 15, "miss_count": 5, "confidence": 0.5}]
    cp.annotate_chains(conn, rows)
    ev = rows[0]["evidence"]
    assert ev["base_rate"] is not None and ev["base_rate"] > 0.8
    assert ev["lift"] < 0
    assert rows[0]["tier"] == "weak"        # 有 20 个样本 → weak，但不是 strong


def test_same_hit_rate_in_choppy_market_is_strong():
    """同样的 15/20=75%，在震荡行情里就是 +30pp 以上的超额 → strong。"""
    conn = _make_conn({"CHOP417": _choppy()},
                      [(2, "CHOP417", "UP", 15, 5)],
                      _hits_misses(2, 15, 5))
    rows = [{"id": 2, "asset": "CHOP417", "expected_direction": "UP",
             "hit_count": 15, "miss_count": 5, "confidence": 0.5}]
    cp.annotate_chains(conn, rows)
    ev = rows[0]["evidence"]
    assert ev["base_rate"] < 0.55
    assert ev["lift"] > cp.MIN_LIFT_FOR_STRONG
    assert ev["p_value"] <= cp.MAX_P_VALUE_FOR_STRONG
    assert rows[0]["tier"] == "strong"


def test_thin_sample_never_strong():
    """超额漂亮但只有 6 个样本 → 不许 strong（3~11 只算 weak）。"""
    conn = _make_conn({"CHOP2_417": _choppy()},
                      [(3, "CHOP2_417", "UP", 6, 0)],
                      _hits_misses(3, 6, 0))
    rows = [{"id": 3, "asset": "CHOP2_417", "expected_direction": "UP",
             "hit_count": 6, "miss_count": 0, "confidence": 0.5}]
    cp.annotate_chains(conn, rows)
    assert rows[0]["tier"] == "weak"


def test_both_chain_strong_is_labelled_strong_mag():
    """BOTH（幅度）链即使有统计支撑，也只能标 strong_mag，不能冒充方向证据。"""
    conn = _make_conn({"CHOP3_417": _choppy()},
                      [(4, "CHOP3_417", "BOTH", 30, 0)],
                      _hits_misses(4, 30, 0))
    rows = [{"id": 4, "asset": "CHOP3_417", "expected_direction": "BOTH",
             "hit_count": 30, "miss_count": 0, "confidence": 0.5}]
    cp.annotate_chains(conn, rows)
    assert rows[0]["tier"] in ("strong_mag", "weak", "none")
    assert rows[0]["tier"] != "strong"


def test_no_base_rate_falls_back_to_wilson():
    """拿不到行情（基准率 None）时退回 v406 的 Wilson 口径，不许静默开闸。"""
    p = {"direction": "UP", "n_outcomes": 60, "hit_rate": 0.70, "wilson_lower": 0.58}
    assert cp._eligible(p) is True
    p_thin = {"direction": "UP", "n_outcomes": 10, "hit_rate": 1.0, "wilson_lower": 0.72}
    assert cp._eligible(p_thin) is False


def test_veto_fires_on_lift_evidence_even_if_posterior_low():
    """统计闸门已过，但 Beta(2,2) 后验只有 0.56 —— 不许用展示口径把它废掉。"""
    p = {"direction": "DOWN", "n_outcomes": 20, "hit_rate": 0.75,
         "posterior": 0.565, "chain_id": "7", "name": "d",
         "lift": 0.30, "p_value": 0.01}
    assert cp.conflict_veto([p], "BUY") is not None
    # 超额不够强的同一条链：不许否决
    weak = dict(p, lift=0.04, p_value=0.30)
    assert cp.conflict_veto([weak], "BUY") is None


def test_annotate_without_tables_does_not_crash():
    """老库/裁剪库没有 outcomes 表时，列表仍要能出来（证据留空）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE causal_chains (id INTEGER, expected_direction TEXT)")
    rows = [{"id": 9, "expected_direction": "UP"}]
    cp.annotate_chains(conn, rows)
    assert rows[0].get("tier") in ("none", None)
