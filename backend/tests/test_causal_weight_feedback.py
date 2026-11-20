# -*- coding: utf-8 -*-
"""因果图谱权重闭环回写回归测试（全离线，不联网、不写生产库）。

覆盖 v310 P1 重做的四处关键行为:
  · credit 公式: HIT_WEAK 计 **0.5**（旧版算 1.5，把全局先验从 0.576 抬到 0.770）
  · 分层先验: (asset, expected_direction) 族 → asset 级 → 全局 三级回退
  · 退化率检测: 族命中率落在 [5%, 95%] 之外 = 系统性问题, 不做族先验
  · 门槛: 链自身样本 < MIN_CHAIN_SAMPLES 不改权重; weight 恒在 [0.10, 1.00]
"""
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from feeds import causal_weight_feedback as cwf


def _mem_db(chains=(), outcomes=()):
    """最小因果链库: chains=(id, asset, direction, weight), outcomes=(chain_id, status)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE causal_chains ("
                 "id INTEGER PRIMARY KEY, name TEXT DEFAULT '', "
                 "weight REAL DEFAULT 1.0, asset TEXT, expected_direction TEXT)")
    conn.execute("CREATE TABLE causal_chain_outcomes ("
                 "id INTEGER PRIMARY KEY, chain_id TEXT, status TEXT)")
    for cid, asset, direction, weight in chains:
        conn.execute("INSERT INTO causal_chains (id, name, weight, asset, expected_direction) "
                     "VALUES (?,?,?,?,?)",
                     (cid, f"chain-{cid}", weight, asset, direction))
    for chain_id, status in outcomes:
        conn.execute("INSERT INTO causal_chain_outcomes (chain_id, status) VALUES (?,?)",
                     (str(chain_id), status))
    return conn


# ---------------------------------------------------------------------------
# ① credit 公式: 弱命中折半
# ---------------------------------------------------------------------------

def test_weak_hit_counts_half_credit():
    conn = _mem_db(chains=[(1, "XAU", "BOTH", 1.0)],
                   outcomes=[(1, "HIT_WEAK"), (1, "MISS")])
    stats = cwf._chain_stats(conn)
    assert stats[1]["credit"] == 0.5, f"弱命中应计 0.5 分, 实际 {stats[1]['credit']}"
    assert stats[1]["n"] == 2
    conn.close()


def test_global_prior_matches_half_credit():
    """1 HIT + 1 HIT_WEAK → credit 1.5 / n 2 = 0.75 (旧版会算成 1.25/2=0.875)。"""
    conn = _mem_db(chains=[(1, "XAU", "BOTH", 1.0)],
                   outcomes=[(1, "HIT"), (1, "HIT_WEAK")])
    prior = cwf.compute_global_prior(cwf._chain_stats(conn))
    assert abs(prior - 0.75) < 1e-9, f"全局先验应为 0.75, 实际 {prior}"
    conn.close()


# ---------------------------------------------------------------------------
# ② 分层先验: 族 → asset → 全局
# ---------------------------------------------------------------------------

def test_family_prior_used_when_enough_samples():
    """XAU|UP 族有 20+ 样本且命中率正常 (非退化) → 用族先验。"""
    # 15 HIT + 6 MISS = 0.714, 落在 [5%, 95%] 内, 不是退化率
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)],
                   outcomes=[(1, "HIT")] * 15 + [(1, "MISS")] * 6)
    plan = cwf.plan_updates(conn)
    assert plan["plans"][0]["prior_kind"] == "family", \
        f"应走族先验, 实际 {plan['plans'][0]['prior_kind']}"
    conn.close()


def test_small_family_falls_back_to_global():
    """族样本不足 → 回退全局先验。"""
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=[(1, "HIT"), (1, "MISS")])
    plan = cwf.plan_updates(conn)
    assert plan["plans"][0]["prior_kind"] == "global"
    conn.close()


def test_asset_level_fallback_between_family_and_global():
    """族不可用但 asset 可用 → 回退到 asset 级先验。

    构造:
      chain1 XAU|UP   n=2  (1 HIT + 1 MISS)  → 族样本不足, 不可用
      chain2 XAU|DOWN n=21 (全 HIT)           → 族命中率 100%, 退化, 不可用
      chain3 XAU|BOTH n=4  (全 MISS)          → 族样本不足, 不可用
      asset XAU 聚合 n=27, credit=22 → rate 0.8148, 落在 [5%, 95%] 内 → **可用**
    """
    chains = [(1, "XAU", "UP", 1.0), (2, "XAU", "DOWN", 1.0), (3, "XAU", "BOTH", 1.0)]
    outcomes = ([(1, "HIT"), (1, "MISS")] + [(2, "HIT")] * 21 + [(3, "MISS")] * 4)
    conn = _mem_db(chains=chains, outcomes=outcomes)
    plan = cwf.plan_updates(conn)
    p = [x for x in plan["plans"] if x["chain_id"] == 1][0]
    assert p["prior_kind"] == "asset", f"应回退 asset 级, 实际 {p['prior_kind']}"
    # 退化的 XAU|DOWN 族必须被拒用 (即便它样本够多)
    p2 = [x for x in plan["plans"] if x["chain_id"] == 2][0]
    assert p2["prior_kind"] == "asset", f"退化族应弃用后回退 asset, 实际 {p2['prior_kind']}"
    assert "XAU|DOWN" in [d["family"] for d in plan["degenerate_families"]], \
        "退化的族必须被上报"
    conn.close()


# ---------------------------------------------------------------------------
# ③ 退化率检测
# ---------------------------------------------------------------------------

def test_degenerate_family_is_not_used_as_prior():
    """族命中率 0% → 退化 → 不做族先验, 且必须被单独上报。"""
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=[(1, "MISS")] * 21)
    plan = cwf.plan_updates(conn)
    assert plan["degenerate_families"], "退化族必须被上报"
    assert plan["degenerate_families"][0]["family"] == "XAU|UP"
    assert plan["plans"][0]["prior_kind"] != "family", "退化族不得用作先验"
    conn.close()


def test_degenerate_families_deduplicated():
    """同一退化族有 3 条链 → 只上报一次 (不是每链一条)。"""
    chains = [(1, "XAU", "UP", 1.0), (2, "XAU", "UP", 1.0), (3, "XAU", "UP", 1.0)]
    outcomes = [(1, "MISS")] * 7 + [(2, "MISS")] * 7 + [(3, "MISS")] * 7
    conn = _mem_db(chains=chains, outcomes=outcomes)
    plan = cwf.plan_updates(conn)
    fam_entries = [d for d in plan["degenerate_families"] if d["family"] == "XAU|UP"]
    assert len(fam_entries) == 1, "退化族应按族去重"
    conn.close()


def test_degenerate_asset_level_falls_back_to_global():
    """asset 级也退化 (整条资产线 0%) → 不做 asset 先验, 回退全局。"""
    conn = _mem_db(chains=[(1, "XAU", "UP", 1.0)], outcomes=[(1, "MISS")] * 21)
    plan = cwf.plan_updates(conn)
    assert plan["plans"][0]["prior_kind"] == "global", \
        f"asset 级退化应回退全局, 实际 {plan['plans'][0]['prior_kind']}"
    keys = [d["family"] for d in plan["degenerate_families"]]
    assert "asset:XAU" in keys, "asset 级退化必须被上报"
    conn.close()


# ---------------------------------------------------------------------------
# ④ 门槛与取值域
# ---------------------------------------------------------------------------

def test_insufficient_chain_samples_skipped():
    """链自身样本 < MIN_CHAIN_SAMPLES → 权重不动。"""
    conn = _mem_db(chains=[(1, "XAU", "BOTH", 1.0)], outcomes=[(1, "HIT")])
    plan = cwf.plan_updates(conn)
    assert plan["plans"][0]["reason"].startswith("skipped_sample")
    conn.close()


def test_weight_stays_in_confidence_domain():
    """无论证据多极端, weight 必须在 [WEIGHT_LO, WEIGHT_HI] 内 (下游按 confidence 夹到 [0,1])。"""
    chains = [(1, "XAU", "BOTH", 1.0), (2, "XAU", "DOWN", 1.0)]
    outcomes = [(1, "HIT")] * 5 + [(2, "MISS")] * 5
    conn = _mem_db(chains=chains, outcomes=outcomes)
    plan = cwf.plan_updates(conn)
    for p in plan["plans"]:
        assert cwf.WEIGHT_LO <= p["proposed_weight"] <= cwf.WEIGHT_HI, \
            f"weight 越界: {p['proposed_weight']}"
    conn.close()


def test_all_hits_never_exceed_one():
    """全命中也不该写出 >1 的值 (1.0 已是 confidence 上限)。"""
    conn = _mem_db(chains=[(1, "XAU", "BOTH", 1.0)], outcomes=[(1, "HIT")] * 8)
    plan = cwf.plan_updates(conn)
    assert plan["plans"][0]["proposed_weight"] <= 1.0
    conn.close()


def test_second_run_converges():
    """第一轮写完后再算一次 → 不应再有 update (幂等, 不抖动)。

    v406 把门槛从 2 提到 12（与方向闸门同口径），所以这里给 12 次 MISS：
    credit=0 对 12 条样本 → 收缩后夹到 WEIGHT_LO=0.10，与初值 1.0 差 0.9
    → 第一轮确实会 update（降权不设 Wilson 证，只在升权时验）。
    """
    conn = _mem_db(chains=[(1, "XAU", "BOTH", 1.0)], outcomes=[(1, "MISS")] * 12)
    first = cwf.plan_updates(conn)
    assert first["plans"][0]["reason"] == "update", "第一轮应当更新权重"
    w = first["plans"][0]["proposed_weight"]
    assert w == cwf.WEIGHT_LO, f"应夹到下限 {cwf.WEIGHT_LO}, 实际 {w}"
    conn.execute("UPDATE causal_chains SET weight=? WHERE id=1", (w,))
    second = cwf.plan_updates(conn)
    assert second["plans"][0]["reason"] != "update", "第二轮不应再改权重"
    conn.close()

