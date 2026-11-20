"""v456 单测：口径体检看得见口径差、同业分组自动扩、财库票屏蔽 PB、A/B 不编数。

全部离线（不联网、不碰生产库）：分组缓存与 industry 抓取都 monkeypatch 掉。
"""

import os
import sqlite3
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(BACKEND, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import equity_fundamentals as ef
import fundamental_shadow as fs
import peer_valuation as pv


# ── 1) 同业分组自动化 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("industry,sector,expected", [
    ("Semiconductors", "Technology", "semis"),
    ("Semiconductor Equipment & Materials", "Technology", "semis"),
    ("Software - Infrastructure", "Technology", "software"),
    ("Internet Content & Information", "Communication Services", "mega_tech"),
    ("Auto Manufacturers", "Consumer Cyclical", "ev_auto"),
])
def test_industry_rules_map_to_buckets(industry, sector, expected):
    assert pv._bucket_from_industry(industry, sector) == expected


def test_unknown_industry_falls_back_to_gics_bucket():
    # 命中不了具体行业 → 归 GICS 粗桶（照用，但调用方会标 coarse）
    assert pv._bucket_from_industry("Trucking", "Industrials") == "gics:industrials"


def test_no_industry_and_no_sector_means_no_guess():
    assert pv._bucket_from_industry("", "") is None


def test_manual_group_wins_over_cache(monkeypatch):
    monkeypatch.setattr(pv, "_SECTOR_CACHE", {"NVDA": {"bucket": "software", "ts": 10 ** 10}})
    assert pv.sector_of("NVDA") == "semis"          # 人工复核的分组优先
    assert pv.sector_kind("NVDA") == "manual"


def test_auto_bucket_cached_and_used_offline(monkeypatch):
    monkeypatch.setattr(pv, "_SECTOR_CACHE", {"ZZZ": {"bucket": "semis", "ts": 10 ** 10}})
    assert pv.sector_of("ZZZ", allow_network=False) == "semis"
    assert "ZZZ" in pv.members_of("semis")


def test_members_of_includes_manual_plus_auto(monkeypatch):
    monkeypatch.setattr(pv, "_SECTOR_CACHE", {"AMD2": {"bucket": "semis", "ts": 10 ** 10}})
    members = pv.members_of("semis")
    assert "NVDA" in members and "AMD2" in members
    assert members == sorted(members)


# ── 2) 财库票屏蔽 PB ──────────────────────────────────────────────────────
def test_treasury_vehicle_pb_is_excluded_with_reason():
    reason = pv.pb_excluded("MSTR")
    assert reason and "财库" in reason
    assert pv.pb_excluded("NVDA") is None


def _fake_multiples(table):
    def _f(t, conn=None, allow_network=False):
        return table.get(str(t).upper(),
                         {"pe": None, "pb": None, "source": "", "ticker": str(t).upper()})
    return _f


def test_peer_pb_median_skips_treasury_vehicle(monkeypatch):
    """MSTR 的 PB 不能进同业中位数 —— 否则整组被「市值/币价」拉歪。"""
    monkeypatch.setattr(pv, "_SECTOR_CACHE", {})
    monkeypatch.setattr(pv, "SECTORS", {"grp": ("AAA", "BBB", "CCC", "MSTR")})
    monkeypatch.setattr(pv, "multiples", _fake_multiples({
        "AAA": {"pe": 20.0, "pb": 4.0, "source": "snapshot", "ticker": "AAA"},
        "BBB": {"pe": 22.0, "pb": 6.0, "source": "snapshot", "ticker": "BBB"},
        "CCC": {"pe": 24.0, "pb": 5.0, "source": "snapshot", "ticker": "CCC"},
        "MSTR": {"pe": 40.0, "pb": 0.7, "source": "snapshot", "ticker": "MSTR"},
    }))
    out = pv.build("AAA", conn=None, allow_network=False)
    assert out["available"]
    # 剔除 MSTR 后中位数 = 5.0/6.0 → 5.5；含进去会变成 5.0
    assert out["components"]["peer_pb_median"] == pytest.approx(5.5)


def test_treasury_vehicle_skips_own_pb_reading(monkeypatch):
    monkeypatch.setattr(pv, "_SECTOR_CACHE", {})
    monkeypatch.setattr(pv, "SECTORS", {"grp": ("MSTR", "BBB", "CCC")})
    monkeypatch.setattr(pv, "multiples", _fake_multiples({
        "MSTR": {"pe": 40.0, "pb": 0.7, "source": "snapshot", "ticker": "MSTR"},
        "BBB": {"pe": 30.0, "pb": 6.0, "source": "snapshot", "ticker": "BBB"},
        "CCC": {"pe": 50.0, "pb": 5.5, "source": "snapshot", "ticker": "CCC"},
    }))
    out = pv.build("MSTR", conn=None, allow_network=False)
    assert "rel_pb" not in out["components"]         # 自身 PB 不参与
    assert out["components"].get("pb_excluded")
    assert "rel_pe" in out["components"]             # PE 仍可用


# ── 3) 决策埋点 + 仓位影子 ────────────────────────────────────────────────
def _guard(score=0.42, available=True):
    return {"factors": {"fundamental": {"available": available, "score": score, "weight": 0.08},
                        "news_sentiment": {"available": True, "score": 0.6, "weight": 0.32}},
            "thresholds": {"buy": 0.25, "sell": -0.25}}


def test_record_writes_decision_snapshot():
    conn = sqlite3.connect(":memory:")
    fs.record(conn, 424242, None, "US_STOCK", _guard(), "BUY", components={"ticker": "NVDA"})
    row = conn.execute("""SELECT ticker, fund_score, sizing_mult
                          FROM decision_fundamental_snapshot WHERE decision_id=424242""").fetchone()
    conn.close()
    assert row[0] == "NVDA"
    assert row[1] == pytest.approx(0.42)
    assert row[2] == pytest.approx(1.084)      # 1 + 0.20*0.42


@pytest.mark.parametrize("score,expected", [
    (0.0, 1.0), (0.5, 1.1), (1.0, 1.2), (-1.0, 0.8), (-2.0, 0.8), (2.0, 1.2),
])
def test_sizing_multiplier_is_bounded(score, expected):
    assert fs.sizing_multiplier(score, True) == pytest.approx(expected)


def test_sizing_multiplier_is_one_when_unavailable():
    assert fs.sizing_multiplier(0.9, False) == 1.0
    assert fs.sizing_multiplier(None, True) == 1.0


# ── 4) 口径体检的判级 ────────────────────────────────────────────────────
def test_drift_grading_is_three_tier():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "drift", os.path.join(BACKEND, "scripts", "fundamental_drift_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._grade(0.01) == "OK"
    assert mod._grade(0.10) == "WARN"       # 供应商口径差异，不天天报警
    assert mod._grade(0.30) == "FAIL"       # 明显有一边错了


# ── 5) A/B 统计：样本不足要说不作结论 ────────────────────────────────────
def test_ab_stats_and_min_samples_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ab", os.path.join(BACKEND, "scripts", "fundamental_ab_replay.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = [{"ret": 0.10, "sign_with": 1.0, "sign_without": 0.0},
            {"ret": -0.05, "sign_with": 1.0, "sign_without": 0.0}]
    st = mod._stats(rows)
    assert st["n"] == 2
    assert st["hit_rate"] == pytest.approx(0.5)
    assert st["avg_edge"] == pytest.approx(0.025)
    assert st["avg_edge_without"] == pytest.approx(0.0)
    assert mod._MIN_SAMPLES >= 30          # 门槛不许被随手调低
