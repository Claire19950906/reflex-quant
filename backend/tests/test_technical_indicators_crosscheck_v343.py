"""v343 L1 交叉对账：用**另一条实现路径**重算同一批指标，逐值比对。

为什么要"另一条实现"而不是复算原函数
------------------------------------
性质测试能证明"不会崩、边界不越界"，但不能证明"平滑方式写对了"——
把 Wilder 递推写成简单算术平均，单测照样全绿，图上一看就歪。
这里在测试里独立实现一遍（用累计平均形式 navg = avg - avg/p + x/p，
原实现用的是累计和形式 S = S - S/p + x），两条路径若能对上 0.05 以内，
说明错的是口径之外的细节，而不是平滑本身。

覆盖：EMA / ATR / RSI / ADX(+DI,-DI) / Donchian / 已实现波动 / 布林带宽。
数据：固定种子的伪随机游走（可复现，不连库）。
"""
import random
import statistics
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_indicators as ti


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------

def _series(n=320, seed=20261116, start=100.0):
    """带波动率聚簇的随机游走 → [ts, o, h, l, c, v]。"""
    rng = random.Random(seed)
    candles = []
    price = start
    vol = 1.0
    for i in range(n):
        vol = max(0.3, min(3.0, vol * 0.9 + abs(rng.gauss(0, 1)) * 0.2))
        price += rng.gauss(0, vol)
        o = price + rng.gauss(0, 0.3)
        c = price
        h = max(o, c) + abs(rng.gauss(0, 0.4))
        l = min(o, c) - abs(rng.gauss(0, 0.4))
        candles.append([1_760_000_000 + i * 14400, o, h, l, c, 100 + rng.random() * 50])
    return candles


CANDLES = _series()


# ---------------------------------------------------------------------------
# 独立实现（故意换一种写法）
# ---------------------------------------------------------------------------

def ref_ema(values, period):
    k = 2.0 / (period + 1.0)
    out = []
    prev = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def ref_atr(candles, period):
    trs = []
    for i, (_, o, h, l, c, _v) in enumerate(candles):
        if i == 0:
            trs.append(h - l)
        else:
            pc = candles[i - 1][4]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    avg = sum(trs[:period]) / period
    out[period - 1] = avg
    for i in range(period, len(candles)):
        avg = avg - avg / period + trs[i] / period     # 累计平均形式
        out[i] = avg
    return out


def ref_rsi(values, period):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = _rsi(ag, al)
    for i in range(period, len(gains)):
        ag = ag - ag / period + gains[i] / period
        al = al - al / period + losses[i] / period
        out[i + 1] = _rsi(ag, al)
    return out


def _rsi(ag, al):
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def ref_adx(candles, period):
    n = len(candles)
    tr, pdm, ndm = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        _, o, h, l, c, _v = candles[i]
        pc = candles[i - 1][4]
        ph = candles[i - 1][2]
        pl = candles[i - 1][3]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    adx_out, pdi_out, ndi_out = [None] * n, [None] * n, [None] * n
    atr_avg = sum(tr[1:period + 1]) / period
    pdm_avg = sum(pdm[1:period + 1]) / period
    ndm_avg = sum(ndm[1:period + 1]) / period
    dx = [None] * n
    for i in range(period, n):
        if i > period:
            atr_avg = atr_avg - atr_avg / period + tr[i] / period
            pdm_avg = pdm_avg - pdm_avg / period + pdm[i] / period
            ndm_avg = ndm_avg - ndm_avg / period + ndm[i] / period
        pdi = 100.0 * pdm_avg / atr_avg if atr_avg else 0.0
        ndi = 100.0 * ndm_avg / atr_avg if atr_avg else 0.0
        pdi_out[i], ndi_out[i] = pdi, ndi
        denom = pdi + ndi
        dx[i] = 100.0 * abs(pdi - ndi) / denom if denom > 0.01 else 0.0
    first = 2 * period - 1
    if first >= n:
        return {"adx": adx_out, "plus_di": pdi_out, "minus_di": ndi_out}
    avg_dx = sum(dx[period:first + 1]) / len(dx[period:first + 1])
    adx_out[first] = avg_dx
    for i in range(first + 1, n):
        avg_dx = (avg_dx * (period - 1) + dx[i]) / period
        adx_out[i] = avg_dx
    return {"adx": adx_out, "plus_di": pdi_out, "minus_di": ndi_out}


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

def test_ema_matches_independent_recursion():
    closes = [c[4] for c in CANDLES]
    got = ti.ema(closes, 12)
    ref = ref_ema(closes, 12)
    diffs = [abs(g - r) for g, r in zip(got, ref) if g is not None]
    assert diffs and max(diffs) < 1e-3


def test_atr_matches_independent_wilder():
    got = ti.atr(CANDLES, 14)
    ref = ref_atr(CANDLES, 14)
    assert got[13] is not None and ref[13] is not None
    diffs = [abs(g - r) for g, r in zip(got, ref) if g is not None]
    assert diffs and max(diffs) < 1e-6


def test_rsi_matches_independent_wilder():
    closes = [c[4] for c in CANDLES]
    got = ti.rsi(closes, 14)
    ref = ref_rsi(closes, 14)
    diffs = [abs(g - r) for g, r in zip(got, ref) if g is not None]
    assert len(diffs) > 250
    assert max(diffs) < 0.05


def test_adx_matches_independent_wilder():
    """ADX 最容易被写错的地方：DI 归一化用 ATR 而不是 TR 单根、DX 首窗长度、ADX 二次平滑。"""
    got = ti.adx(CANDLES, 14)
    ref = ref_adx(CANDLES, 14)
    for key, tol in (("adx", 0.06), ("plus_di", 0.06), ("minus_di", 0.06)):
        diffs = [abs(g - r) for g, r in zip(got[key], ref[key]) if g is not None]
        assert len(diffs) > 250, key
        assert max(diffs) < tol, (key, max(diffs))


def test_donchian_matches_rolling_extremes():
    got = ti.donchian(CANDLES, 20)
    for i in (19, 100, len(CANDLES) - 1):
        w = CANDLES[i - 19:i + 1]
        hi = max(c[2] for c in w)
        lo = min(c[3] for c in w)
        assert abs(got["upper"][i] - hi) < 1e-6
        assert abs(got["lower"][i] - lo) < 1e-6
        pos = (CANDLES[i][4] - lo) / (hi - lo) if hi > lo else 0.5
        assert abs(got["position"][i] - pos) < 1e-4


def test_realized_vol_matches_manual_stdev():
    """口径必须是"收益率总体标准差"(分母 n)，不是样本标准差(n-1)。"""
    got = ti.realized_vol(CANDLES, 20)
    closes = [c[4] for c in CANDLES]
    rets = [0.0] + [(closes[i] - closes[i - 1]) / closes[i - 1] * 100.0
                    for i in range(1, len(closes))]
    i = 200
    w = rets[i - 19:i + 1]
    ref = statistics.pstdev(w)
    assert abs(got[i] - ref) < 1e-4


def test_bollinger_width_matches_manual():
    got = ti.bollinger_width([c[4] for c in CANDLES], 20)
    closes = [c[4] for c in CANDLES]
    i = 150
    w = closes[i - 19:i + 1]
    mid = sum(w) / 20
    std = statistics.pstdev(w)
    ref = (2.0 * 2.0 * std) / abs(mid) * 100.0
    assert abs(got[i] - ref) < 1e-4


def test_ma_slope_matches_manual_percent():
    closes = [c[4] for c in CANDLES]
    got = ti.ma_slope(closes, 20, 5)
    avg_now = sum(closes[-20:]) / 20
    avg_prev = sum(closes[-25:-5]) / 20
    # 口径是"每根"：跨度 5 根的总变化要摊到单根，否则会差 5 倍
    ref = (avg_now - avg_prev) / avg_prev * 100.0 / 5.0
    assert abs(got - ref) < 1e-3


@pytest.mark.parametrize("period", [7, 14, 21])
def test_rsi_period_consistency(period):
    """不同周期都要成立：参数化能抓住写死 14 的硬编码。"""
    closes = [c[4] for c in CANDLES]
    got = ti.rsi(closes, period)
    ref = ref_rsi(closes, period)
    diffs = [abs(g - r) for g, r in zip(got, ref) if g is not None]
    assert max(diffs) < 0.05
    assert got[period - 1] is None     # 前 period 根必须是 None，不造数
