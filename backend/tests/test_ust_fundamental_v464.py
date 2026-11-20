"""v464 — 美债基本面分量回归测试。

钉死五件事：
  1. 符号口径：实际利率/通胀预期上行 = 债券价格利空；信用利差走阔 = 避险利多。
  2. 分位口径：同一个当前值（2.89%），历史不同结论必须不同 —— 分数来自分位不是绝对值。
  3. 曲线用 carry 视角（越陡=期限溢价越高=偏多），且这个符号写死在代码里。
  4. 长短久期两套口径：2Y/SHY 看政策利率，10Y/30Y/TLT 看实际利率与通胀预期。
  5. 分量不足 2 个判不可得，不做单读数凑分。

全部用内存假库，不碰生产库、不发网络请求。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import alt_fundamental as af
import fundamental_factor

_SCHEMA = """
CREATE TABLE macro_snapshots (
    ts INTEGER, category TEXT, source TEXT, metric_key TEXT,
    value REAL, unit TEXT, status TEXT, payload TEXT
);
CREATE TABLE external_indicators (
    id INTEGER PRIMARY KEY, asset TEXT, indicator TEXT, value REAL,
    ts INTEGER, source TEXT, regime_tags TEXT, meta TEXT, ingested_at INTEGER
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn, key, values, *, step_days=1, category="fred") -> None:
    now = int(time.time())
    n = len(values)
    for i, v in enumerate(values):
        ts = now - (n - 1 - i) * step_days * 86400
        conn.execute(
            "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ts, category, "test", key, float(v), "", "ok", "{}"))


def _ramp(start, end, n=60) -> list:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def _bond_db(*, dgs10_end=5.10, dgs30_end=5.46, tips_end=2.89, be_end=2.19,
             cot_end=-9000, hy_start=3.0, hy_end=3.43, dgs2_end=4.80,
             n=250) -> sqlite3.Connection:
    conn = _db()
    _seed(conn, "dgs10", _ramp(3.8, dgs10_end, n))
    _seed(conn, "dgs30", _ramp(4.4, dgs30_end, n))
    _seed(conn, "tips10y", _ramp(1.0, tips_end, n))
    _seed(conn, "t10yie", _ramp(2.2, be_end, n))
    _seed(conn, "hy_oas", _ramp(hy_start, hy_end, n))
    _seed(conn, "dgs2", _ramp(3.8, dgs2_end, n))
    _seed(conn, "tbond.cot_noncomm_net", _ramp(50000, cot_end, 40),
          step_days=7, category="cot")
    _seed(conn, "tnote2y.cot_noncomm_net", _ramp(50000, cot_end, 40),
          step_days=7, category="cot")
    return conn


# ─────────────── 1. 符号口径 ───────────────

def test_high_real_yield_and_hot_inflation_is_bearish():
    """实际利率冲到历史高位 + 通胀预期抬升 → 长久期债券基本面必须为负。"""
    out = af.build("UST", conn=_bond_db(tips_end=3.20, be_end=3.00), use_cache=False)
    assert out["available"] is True
    assert out["score"] < -0.15
    comps = out["components"]
    assert comps["real_yield"]["score"] < 0        # 实际利率高 = 债券挨打
    assert comps["breakeven"]["score"] < 0         # 通胀预期高 = 长端被抛


def test_low_real_yield_and_wide_credit_is_bullish():
    """镜像场景：实际利率塌到低位 + 通胀预期回落 + 信用利差走阔 → 必须为正。"""
    out = af.build("UST", conn=_bond_db(tips_end=0.30, be_end=1.40,
                                        hy_start=3.0, hy_end=4.5), use_cache=False)
    assert out["available"] is True
    assert out["score"] > 0.15
    comps = out["components"]
    assert comps["real_yield"]["score"] > 0
    assert comps["credit"]["score"] > 0            # 利差走阔 = 避险买国债


def test_credit_tightening_is_bearish_for_bonds():
    """信用利差快速收窄 = 风险偏好回升 → 国债避险需求下降（负分）。"""
    out = af.build("UST", conn=_bond_db(hy_start=5.0, hy_end=3.0), use_cache=False)
    assert out["components"]["credit"]["score"] < 0


# ─────────────── 2. 分位口径（不是绝对值） ───────────────

def test_same_level_different_history_flips_sign():
    """同为 2.89% 的实际利率：在 0.2→2.89 的历史里是高位（利空），
    在 5.0→2.89 的历史里是低位（利多）。分数必须翻转 —— 证明用的是分位。"""
    high_side = _db()
    _seed(high_side, "tips10y", _ramp(0.2, 2.89, 250))
    low_side = _db()
    _seed(low_side, "tips10y", _ramp(5.0, 2.89, 250))

    # 只有 tips10y 一个数据族，先补上曲线族（第二族）才够门槛
    steep = _ramp(0.0, 0.30, 250)
    for conn in (high_side, low_side):
        _seed(conn, "dgs10", [4.0] * 250)
        _seed(conn, "dgs30", [4.0 + s for s in steep])
    hi = af.build("UST", conn=high_side, use_cache=False)
    lo = af.build("UST", conn=low_side, use_cache=False)
    assert hi["components"]["real_yield"]["score"] < 0
    assert lo["components"]["real_yield"]["score"] > 0
    assert hi["score"] < lo["score"]


# ─────────────── 3. 曲线：carry 视角 ───────────────

def test_steeper_curve_is_bullish_in_carry_view():
    """30s10 越陡 = 期限溢价给的持有补偿越厚 = 做多久期偏多（正分）。
    同样的"平"和"陡"，陡的那个曲线分量必须更高。"""
    flat = _db()
    _seed(flat, "dgs10", _ramp(4.0, 5.10, 250))
    _seed(flat, "dgs30", _ramp(4.4, 5.46, 250))          # 30s10 从 0.4 收窄到 0.36
    steep = _db()
    _seed(steep, "dgs10", _ramp(5.2, 5.10, 250))
    _seed(steep, "dgs30", _ramp(4.4, 6.00, 250))         # 30s10 从 -0.8 走到 +0.9(高位)
    for conn in (flat, steep):
        _seed(conn, "tips10y", _ramp(2.0, 2.0, 250))

    f = af.build("UST", conn=flat, use_cache=False)["components"]["curve"]["score"]
    s = af.build("UST", conn=steep, use_cache=False)["components"]["curve"]["score"]
    assert s > f


# ─────────────── 4. 长短久期两套口径 ───────────────

def test_short_duration_follows_policy_rate():
    """2Y/SHY 是"现金替代"：2Y 收益率冲到历史高位 → 票息补偿厚 → 正分。"""
    out = af.build("US2Y", conn=_bond_db(dgs2_end=5.20), use_cache=False)
    assert out["available"] is True
    assert out["components"]["policy_rate"]["score"] > 0
    assert "curve" not in out["components"]        # 短久期不装曲线
    assert out.get("duration") == "短久期"


def test_long_duration_has_six_components_and_hard_gaps():
    out = af.build("TLT", conn=_bond_db(), use_cache=False)
    assert out.get("duration") == "长久期"
    assert set(out["components"]) >= {"real_yield", "real_yield_chg", "breakeven",
                                     "curve", "cot", "credit", "auction"}
    joined = " ".join(out.get("gaps") or [])
    # 拍卖已接入（v465），不再是缺口；剩下的硬缺口是 carry 与 TIC
    assert "TIC" in joined and "carry" in joined
    assert "拍卖需求" not in joined


# ─────────────── 5. 门槛与路由 ───────────────

def test_single_family_is_not_enough():
    """只有实际利率一族（没有曲线/COT/信用） → 判不可得，不硬凑分。"""
    conn = _db()
    _seed(conn, "tips10y", _ramp(1.0, 2.89, 250))
    out = af.build("UST", conn=conn, use_cache=False)
    assert out["available"] is False
    assert out["reason"] in ("insufficient", "thin")


def test_auction_demand_sign():
    """投标倍数处于历史高位 = 买家踊跃 = 利多债券；低位 = 需求疲弱 = 利空。"""
    strong = _db()
    _seed(strong, "ust.auction_btc_10y", _ramp(2.0, 2.90, 40), step_days=30, category="auction")
    _seed(strong, "ust.auction_btc_30y", _ramp(2.0, 2.90, 40), step_days=30, category="auction")
    weak = _db()
    _seed(weak, "ust.auction_btc_10y", _ramp(2.9, 2.20, 40), step_days=30, category="auction")
    _seed(weak, "ust.auction_btc_30y", _ramp(2.9, 2.20, 40), step_days=30, category="auction")

    hi = af.build("UST", conn=strong, use_cache=False)["components"]["auction"]["score"]
    lo = af.build("UST", conn=weak, use_cache=False)["components"]["auction"]["score"]
    assert hi > 0 and lo < 0
    assert hi > lo


def test_auction_shortduration_uses_2y():
    conn = _db()
    _seed(conn, "ust.auction_btc_2y", _ramp(2.0, 3.0, 40), step_days=30, category="auction")
    _seed(conn, "dgs2", _ramp(3.0, 4.8, 60))
    out = af.build("US2Y", conn=conn, use_cache=False)
    assert out["available"] is True
    assert out["components"]["auction"]["score"] > 0


def test_alias_and_router():
    """新闻里"美债/10Y/TLT"写法都要能路由到美债分量，不能当美股 ticker 报 no_data。"""
    assert af.canonical("美债") == "UST"
    assert af.canonical("10Y") == "US10Y"
    assert af.canonical("TLT") == "TLT"
    for symbol in ("UST", "TLT", "US2Y"):
        out = fundamental_factor.build(symbol, use_cache=False)
        assert out.get("kind") == "alt", symbol
        assert out.get("reason") != "no_data", symbol
