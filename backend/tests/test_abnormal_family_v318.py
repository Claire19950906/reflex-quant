"""v318 — 异常归因降噪回归测试。

背景（线上实测 trident_event_bus.db）：
  · 35,802 条 multi_factor 信号里只有 4,332 个不同的 (asset, anchor_ts)，
    其中 30,800 条 anchor_ts 为空 —— 同一持续状态被扫描循环反复上报，
    每次都排一次 LLM 归因。
  · 12.6% 的信号只有**单一**证据族（多为 RSI 超买这类常态噪音）。
  · RSI / KDJ / MFI 是同一段价格算出来的动量指标，原实现把它们当 3 票，
    方向被单一族灌满（oscillator/trend/volume 三族命中合计 9.6 万次）。

覆盖：
  · 因子 → 独立家族映射
  · 同族只算一票
  · ≥2 个独立族才算 corroborated（决定是否排 LLM 归因）
  · 去重窗口命中判断
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import abnormal_detector


def _uptrend(n: int = 40, step: float = 1.0, vol: float = 1000.0):
    """构造持续上涨的 K 线：[ts, open, high, low, close, volume]。"""
    out = []
    for i in range(n):
        close = 100.0 + i * step
        out.append([
            1_700_000_000_000 + i * 180_000,
            round(close - 0.2, 4),
            round(close + 0.3, 4),
            round(close - 0.3, 4),
            round(close, 4),
            vol,
        ])
    return out


# ─────────────────────── 1. 因子 → 家族映射 ───────────────────────

def test_factor_family_mapping():
    f = abnormal_detector.factor_family
    # 三个动量指标同属一族
    assert f("RSI超买") == "oscillator"
    assert f("RSI顶背离") == "oscillator"
    assert f("RSI超买钝化") == "oscillator"
    assert f("KDJ超买(J=95)") == "oscillator"
    assert f("MFI超买(88)") == "oscillator"
    # 趋势族
    assert f("MACD金叉") == "trend"
    assert f("MACD上穿零轴") == "trend"
    assert f("MA5/MA20金叉") == "trend"
    # 其余族
    assert f("布林上轨突破") == "volatility"
    assert f("ATR波动率扩张") == "volatility"
    assert f("量价顶背离") == "volume"
    assert f("放量向上突破") == "volume"
    assert f("看涨吞没") == "candlestick"
    assert f("锤子线") == "candlestick"
    assert f("单根异动/vol_spike") == "price"


def test_unknown_factor_does_not_merge_with_others():
    a = abnormal_detector.factor_family("某个没见过的因子")
    b = abnormal_detector.factor_family("另一个没见过的因子")
    assert a != b


# ─────────────────────── 2. 同族只算一票 ───────────────────────

def test_same_family_counts_one_vote():
    res = abnormal_detector.multi_factor(_uptrend())
    assert res["triggered"] is True
    families = res["families"]
    # 家族不重复
    assert len(families) == len(set(families))
    # 票数 ≤ 家族数（同一族只算一票；mixed 族不参与方向票）
    assert res["bull_votes"] + res["bear_votes"] <= len(families)
    # 同族多因子不得变成多票
    oscillator_factors = [
        f for f in res["factors"]
        if f.startswith(("RSI", "KDJ", "MFI"))
    ]
    if len(oscillator_factors) >= 2:
        assert res["family_votes"].get("oscillator") in ("bullish", "bearish")
        assert res["bull_votes"] + res["bear_votes"] <= len(res["factors"])


def test_corroborated_requires_two_families():
    res = abnormal_detector.multi_factor(_uptrend())
    assert res["corroborated"] == (len(res["families"]) >= 2)


def test_hint_only_when_corroborated():
    res = abnormal_detector.multi_factor(_uptrend())
    if res["corroborated"]:
        assert res["hint"]
    else:
        assert res["hint"] == ""


# ─────────────── 2b. 族票方向口径（纯函数，直接锁定语义）───────────────

def test_family_vote_majority_wins():
    """族票多者胜：2 个 bullish 族压过 1 个 bearish 族。"""
    r = abnormal_detector.resolve_family_direction(
        {"oscillator": -4, "trend": 1, "volume": 1}
    )
    assert r["direction"] == "bullish"
    assert r["bull_votes"] == 2 and r["bear_votes"] == 1


def test_single_family_cannot_stampede_direction():
    """v318 初衷：单一族灌满（oscillator 4 票）压不过 3 个独立族。"""
    r = abnormal_detector.resolve_family_direction(
        {"oscillator": -4, "trend": 1, "volume": 1, "price": 1}
    )
    assert r["direction"] == "bullish"


def test_family_tie_broken_by_intra_family_strength():
    """族票平手时用族内净票强度破平：oscillator 净 -4 vs trend 净 +1 → bearish。

    这保住既有语义「上涨趋势里 RSI 超买 = 动能衰竭」。
    """
    r = abnormal_detector.resolve_family_direction({"oscillator": -4, "trend": 1})
    assert r["direction"] == "bearish"
    assert r["bull_votes"] == 1 and r["bear_votes"] == 1


def test_family_tie_with_equal_strength_is_neutral():
    """族票平手且族内强度相等 → 方向不明（None）。"""
    r = abnormal_detector.resolve_family_direction({"oscillator": -1, "trend": 1})
    assert r["direction"] is None


def test_mixed_family_is_evidence_but_not_direction():
    """族内多空抵消 → mixed：仍算独立证据，但不参与方向票。"""
    r = abnormal_detector.resolve_family_direction(
        {"trend": 0, "oscillator": -2, "volume": 1}
    )
    assert r["family_votes"]["trend"] == "mixed"
    assert r["bull_votes"] == 1 and r["bear_votes"] == 1
    # mixed 不参与方向：oscillator(-2) vs volume(+1) 平手 → 强度破平 → bearish
    assert r["direction"] == "bearish"
    assert "trend" in r["families"]


def test_resolve_family_direction_empty():
    r = abnormal_detector.resolve_family_direction({})
    assert r["direction"] is None
    assert r["families"] == []
    assert r["bull_votes"] == 0 and r["bear_votes"] == 0


def test_multi_factor_direction_matches_family_semantics():
    """multi_factor 的方向必须与族票口径一致（不能各算各的）。"""
    res = abnormal_detector.multi_factor(_uptrend())
    expected = abnormal_detector.resolve_family_direction(
        res.get("family_net") or {}
    )["direction"]
    assert res["direction"] == expected


def test_multi_factor_exposes_family_net():
    """族内净票必须回传，供落库审计与方向复算。"""
    res = abnormal_detector.multi_factor(_uptrend())
    net = res.get("family_net")
    assert isinstance(net, dict) and net
    for fam in res["families"]:
        assert fam in net
        assert net[fam] != 0 or res["family_votes"].get(fam) == "mixed"


# ─────────────────────── 3. 去重窗口 ───────────────────────

def _seed_signals(conn):
    import db
    db.migrate(conn)
    now = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
    old = conn.execute(
        "SELECT datetime('now','localtime','-3 hours')").fetchone()[0]
    rows = []
    for asset, direction, created in (("BTC", "bullish", now), ("ETH", "bullish", now),
                                      ("SOL", "bullish", old), ("XAU", "bearish", now)):
        cur = conn.execute(
            """INSERT INTO abnormal_signals
               (ts, asset, signal_kind, direction, anchor_ts, detail_json, hint, created_at)
               VALUES (?, ?, 'multi_factor', ?, NULL, '{}', '', ?)""",
            (1_700_000_000, asset, direction, created),
        )
        rows.append(int(cur.lastrowid))
    conn.execute(
        """INSERT INTO abnormal_attributions
           (signal_id, asset, signal_kind, anchor_ts, summary, drivers_json, cross_json,
            actionable, severity_hint, source_model, used_news_n, created_at)
           VALUES (?, 'BTC', 'multi_factor', NULL, 'x', '[]', '[]', '观望', 1, 'test', 0, ?)""",
        (rows[0], now),
    )
    # 3 小时前的 SOL 信号也已归因过（用于验证放宽窗口后能命中）
    conn.execute(
        """INSERT INTO abnormal_attributions
           (signal_id, asset, signal_kind, anchor_ts, summary, drivers_json, cross_json,
            actionable, severity_hint, source_model, used_news_n, created_at)
           VALUES (?, 'SOL', 'multi_factor', NULL, 'x', '[]', '[]', '观望', 1, 'test', 0, ?)""",
        (rows[2], old),
    )
    conn.commit()
    return rows


def test_dedup_hit_detects_recent_same_asset_same_direction():
    from api_server import _mf_attrib_dedup_hit
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_signals(conn)
    # BTC/bullish 刚归因过 → 命中
    assert _mf_attrib_dedup_hit(conn, "BTC", "bullish", 30) is True
    # ETH/bullish 在窗口内但没归因过 → 不命中
    assert _mf_attrib_dedup_hit(conn, "ETH", "bullish", 30) is False
    # 方向不同 → 不命中
    assert _mf_attrib_dedup_hit(conn, "BTC", "bearish", 30) is False
    # 窗口放宽到 12 小时 → 3 小时前的也命中
    assert _mf_attrib_dedup_hit(conn, "SOL", "bullish", 720) is True
    conn.close()
