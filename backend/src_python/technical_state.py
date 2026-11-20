"""technical_state.py —— 技术指标 → 市场状态读数（共享模块）。

为什么单独一个模块
------------------
同一套读数要在四个地方用到：CLI 脚本、market_snapshot 摘要、ai_worker 的 prompt、
decision_guard 的反方证据。过去 `market_snapshot.py` 里有一套自己的 `_rsi14/_adx14`，
与 `technical_indicators.py` 并行演进——两边口径迟早会分叉。

这里只做一件事：**把取数与合成集中到一处**，让四处共用同一份计算。
取数全部只读本库，缺什么写进 `missing`，绝不补零、绝不猜方向。

数据口径（容易搞错的几个）
--------------------------
- `kline_bars.ts` 是**毫秒**，这里是秒，出库即换算；
- `external_indicators` 里币安 OI 实测只有约 3 天深度 → 窗口自适应(7d/3d/1d/6h)，
  并把**实际窗口**写进读数：用一个 3 日数字却写"7 日变化"是撒谎；
- 价格变化必须与 OI 变化取**同一个日历窗口**，否则"3 天跌 6%"会被读成"刚跌 0.8%"；
- `macro_snapshots` 的 `{资产}.cvd` 是主动买卖差百分比，按时间桶累加成近似 CVD，
  与交易所原生 CVD 不等价，只用于判"主动盘跟不跟得上价格"。
"""
from __future__ import annotations

import copy
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import technical_indicators as ti

_TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900,
               "1h": 3600, "4h": 14400, "1d": 86400}
_DAY = 86400

# OI 窗口候选：从长到短，取第一个"端点真有数据且断档不超标"的窗口
_OI_WINDOWS: Tuple[Tuple[float, str], ...] = (
    (7.0, "7 日"), (3.0, "3 日"), (1.0, "24 小时"), (0.25, "6 小时"),
)

# ── 合成方向偏置的权重 ────────────────────────────────────────────────────
# 只对**本模块自己已经声明了偏向**的子维度加权，不另立模型。
# 缺维度是**丢权重**（不参与），不是给 0 分——否则缺数据会被读成中性。
_BIAS_WEIGHTS: Dict[str, float] = {
    "trend": 0.35,        # ADX 强度 × 方向
    "leverage": 0.20,     # 价/OI 四象限（加仓还是平仓）
    "flow": 0.20,         # 主动买占比
    "donchian": 0.15,     # 区间位置
    "positioning": 0.10,  # 散户 vs 大户（逆向）
    "value_area": 0.10,   # 价格在价值区上/下
}

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 90.0


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def get_db_path() -> str:
    """与 db.py 同源：TRIDENT_DB_PATH 可覆盖，否则用工程内默认库。"""
    try:
        import db as _db
        return str(_db.get_db_path())
    except Exception:
        from pathlib import Path
        return str(Path(__file__).resolve().parents[1] / "trident_event_bus.db")


def _ro_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path or get_db_path()}?mode=ro", uri=True)


def load_candles(conn: sqlite3.Connection, asset: str, timeframe: str,
                 limit: int, asof_ts: Optional[int] = None) -> List[List[float]]:
    """最近 limit 根蜡烛 → [ts_sec, o, h, l, c, v]，升序。

    asof_ts（秒）用于**因果回放**：只取该时点及之前的 K 线，绝不看未来。
    """
    # v408：按 ts 去重。实测 XAU 的 kline_bars 在 2026-09-19~10-22 段有 74%~89% 的
    # 重复收盘、甚至出现同一 ts 多行（gap=0），直接用会把"横盘半小时的采样"
    # 当成多根独立 K 线，ADX/价值区/ATR 全部失真。这里每个 ts 只取最后写入的一行。
    if asof_ts is None:
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM (
                   SELECT ts, open, high, low, close, volume,
                          ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn
                   FROM kline_bars WHERE symbol=? AND timeframe=?)
               WHERE rn=1 ORDER BY ts DESC LIMIT ?""",
            (asset, timeframe, limit)).fetchall()
    else:
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM (
                   SELECT ts, open, high, low, close, volume,
                          ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn
                   FROM kline_bars WHERE symbol=? AND timeframe=? AND ts<=?)
               WHERE rn=1 ORDER BY ts DESC LIMIT ?""",
            (asset, timeframe, int(asof_ts) * 1000, limit)).fetchall()
    # v409：volume 允许缺失。COMEX 日线（XAU/XAG/HG）成交量一律 NULL —— 缺失就标缺失，
    # 但 OHLC 齐全就仍然是合法的一根 K 线，不能被整体丢掉（之前 XAU 日线因此算出 0 根）。
    candles = [[int(r[0]) // 1000, r[1], r[2], r[3], r[4], r[5]] for r in rows
               if None not in (r[1], r[2], r[3], r[4])]
    return list(reversed(candles))


def latest(conn: sqlite3.Connection, asset: str, indicator: str,
           asof_ts: Optional[int] = None) -> Optional[Tuple[float, int]]:
    if asof_ts is None:
        row = conn.execute(
            """SELECT value, ts FROM external_indicators
               WHERE asset=? AND indicator=? AND value IS NOT NULL
               ORDER BY ts DESC LIMIT 1""", (asset, indicator)).fetchone()
    else:
        row = conn.execute(
            """SELECT value, ts FROM external_indicators
               WHERE asset=? AND indicator=? AND value IS NOT NULL AND ts<=?
               ORDER BY ts DESC LIMIT 1""", (asset, indicator, int(asof_ts))).fetchone()
    return (float(row[0]), int(row[1])) if row else None


def metric_latest(conn: sqlite3.Connection, metric_key: str,
                  asof_ts: Optional[int] = None) -> Optional[Tuple[float, int]]:
    """macro_snapshots 口径（{资产}.cvd / {资产}.ls_ratio 等）。"""
    if asof_ts is None:
        row = conn.execute(
            """SELECT value, ts FROM macro_snapshots WHERE metric_key=? AND value IS NOT NULL
               ORDER BY ts DESC LIMIT 1""", (metric_key,)).fetchone()
    else:
        row = conn.execute(
            """SELECT value, ts FROM macro_snapshots WHERE metric_key=? AND value IS NOT NULL
               AND ts<=? ORDER BY ts DESC LIMIT 1""", (metric_key, int(asof_ts))).fetchone()
    return (float(row[0]), int(row[1])) if row else None


def asof(conn: sqlite3.Connection, asset: str, indicator: str,
         ts: int) -> Optional[Tuple[float, int]]:
    row = conn.execute(
        """SELECT value, ts FROM external_indicators
           WHERE asset=? AND indicator=? AND value IS NOT NULL AND ts<=?
           ORDER BY ts DESC LIMIT 1""", (asset, indicator, ts)).fetchone()
    return (float(row[0]), int(row[1])) if row else None


def history(conn: sqlite3.Connection, asset: str, indicator: str,
            since: int, limit: int = 500, asof_ts: Optional[int] = None) -> List[float]:
    if asof_ts is None:
        rows = conn.execute(
            """SELECT value FROM external_indicators
               WHERE asset=? AND indicator=? AND value IS NOT NULL AND ts>=?
               ORDER BY ts DESC LIMIT ?""", (asset, indicator, since, limit)).fetchall()
    else:
        rows = conn.execute(
            """SELECT value FROM external_indicators
               WHERE asset=? AND indicator=? AND value IS NOT NULL AND ts>=? AND ts<=?
               ORDER BY ts DESC LIMIT ?""",
            (asset, indicator, since, int(asof_ts), limit)).fetchall()
    return [float(r[0]) for r in rows]


def cvd_series(conn: sqlite3.Connection, asset: str, bucket_sec: int,
               closes_by_bucket: Dict[int, float],
               max_points: int = 200, asof_ts: Optional[int] = None
               ) -> Tuple[Optional[List[float]], Optional[List[float]]]:
    """主动买卖差(%) → 按 K 线时间桶取末值 → 累加成近似 CVD，与收盘价对齐。

    只保留两侧都有的桶，保证两个序列严格等长：错位会让背离判定变成假信号。
    """
    if asof_ts is None:
        rows = conn.execute(
            """SELECT ts, value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL ORDER BY ts""",
            (f"{asset}.cvd",)).fetchall()
    else:
        rows = conn.execute(
            """SELECT ts, value FROM macro_snapshots
               WHERE metric_key=? AND value IS NOT NULL AND ts<=? ORDER BY ts""",
            (f"{asset}.cvd", int(asof_ts))).fetchall()
    if not rows:
        return (None, None)
    per_bucket: Dict[int, float] = {}
    for ts, v in rows:
        per_bucket[int(ts) // bucket_sec * bucket_sec] = float(v)
    buckets = sorted(b for b in per_bucket if b in closes_by_bucket)[-max_points:]
    if len(buckets) < 3:
        return (None, None)
    cum = 0.0
    series: List[float] = []
    for b in buckets:
        cum += per_bucket[b]
        series.append(round(cum, 4))
    return (series, [closes_by_bucket[b] for b in buckets])


def oi_change(conn: sqlite3.Connection, asset: str, asof_ts: Optional[int] = None
              ) -> Tuple[Optional[float], Optional[float], str, Optional[str], Optional[float]]:
    """持仓量变化 %：币安 OI 张数优先，退化到 USD 口径。

    返回 (变化%, 绝对值, 来源, 实际窗口标签, 窗口天数)。不做跨窗口拼接。
    """
    for ind in ("bn_open_interest", "okx_open_interest_usd", "bn_open_interest_usd"):
        now = latest(conn, asset, ind, asof_ts=asof_ts)
        if not now:
            continue
        for days, label in _OI_WINDOWS:
            prev = asof(conn, asset, ind, now[1] - int(days * _DAY))
            if not prev or prev[0] == 0:
                continue
            gap = abs(now[1] - prev[1])
            if gap > days * _DAY * 1.2:   # 允许 20% 采集断档，超了换更短窗口
                continue
            return (round((now[0] - prev[0]) / prev[0] * 100.0, 4), now[0], ind, label, days)
    return (None, None, "", None, None)


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------

def build_state(conn: sqlite3.Connection, asset: str, timeframe: str = "4h",
                bars: int = 240, asof_ts: Optional[int] = None) -> Dict[str, Any]:
    """算一个资产的六维状态读数。K 线不足 30 根直接报 error（不硬算）。

    asof_ts（秒）→ **严格因果**：只用该时点之前的数据，供 L3 历史回放使用。
    """
    candles = load_candles(conn, asset, timeframe, bars, asof_ts=asof_ts)
    if len(candles) < 30:
        return {"error": f"{asset} {timeframe} 只有 {len(candles)} 根 K 线，不足以计算状态读数"}

    tf_sec = _TF_SECONDS.get(timeframe, 14400)
    closes_by_bucket = {c[0] // tf_sec * tf_sec: c[4] for c in candles}

    # —— 杠杆结构 ——
    oi_chg, oi_abs, oi_src, oi_window, oi_window_days = oi_change(conn, asset, asof_ts=asof_ts)
    funding = (latest(conn, asset, "bn_funding_rate", asof_ts=asof_ts)
               or latest(conn, asset, "okx_funding_rate", asof_ts=asof_ts)
               or latest(conn, asset, "bybit_funding_rate", asof_ts=asof_ts))
    funding_hist: Optional[List[float]] = None
    if funding:
        funding_hist = history(conn, asset, "bn_funding_rate", funding[1] - 120 * _DAY,
                               asof_ts=asof_ts)

    # —— 持仓分歧 ——
    top = latest(conn, asset, "bn_top_position_ratio", asof_ts=asof_ts)
    retail = latest(conn, asset, "bn_long_short_ratio", asof_ts=asof_ts)
    if not retail:
        for ind in ("bybit_long_short_ratio", "okx_long_short_ratio"):
            retail = latest(conn, asset, ind, asof_ts=asof_ts)
            if retail:
                break
    if not retail:
        # 黄金/原油没有交易所多空比，但有券商客户持仓多空比
        retail = metric_latest(conn, f"{asset}.ls_ratio", asof_ts=asof_ts)

    # —— 主动性 ——
    tb = latest(conn, asset, "okx_taker_buy", asof_ts=asof_ts)
    ts_ = latest(conn, asset, "okx_taker_sell", asof_ts=asof_ts)
    taker_pair: Tuple[Optional[float], Optional[float]] = (None, None)
    if tb and ts_ and abs(tb[1] - ts_[1]) <= 900:   # 同一 1h 窗口
        taker_pair = (tb[0], ts_[0])
    cvd, aligned_closes = cvd_series(conn, asset, tf_sec, closes_by_bucket, asof_ts=asof_ts)

    # 四象限必须同窗口：价格变化区间对齐到 OI 的实际窗口
    price_chg: Optional[float] = None
    if oi_window_days:
        target = candles[-1][0] - int(oi_window_days * _DAY)
        anchor = next((c for c in reversed(candles) if c[0] <= target), None) or candles[0]
        if anchor[4]:
            price_chg = round((candles[-1][4] - anchor[4]) / anchor[4] * 100.0, 4)

    state = ti.market_state(
        candles,
        price_change_pct=price_chg,
        oi_change_pct=oi_chg,
        funding_rate=funding[0] if funding else None,
        funding_history=funding_hist,
        top_long_short_ratio=top[0] if top else None,
        retail_long_short_ratio=retail[0] if retail else None,
        taker_buy=taker_pair[0], taker_sell=taker_pair[1],
        cvd=cvd,
        cvd_closes=aligned_closes,
        interval_sec=tf_sec,
    )
    # K 线体检：XAU 的 kline_bars 实测 55%~67% 的相邻收盘完全相同、时间间隔不规整，
    # 这种"假序列"仍能算出漂亮的 ADX/价值区，但那些数字没有意义。读数里必须带出体检结果，
    # 否则会拿假 K 线的结论去做判断（v408 回测前先做这一步时才发现）。
    _closes = [c[4] for c in candles]
    _dup = sum(1 for i in range(1, len(_closes)) if _closes[i] == _closes[i - 1]) / max(1, len(_closes) - 1) * 100
    _gaps = sorted(candles[i][0] - candles[i - 1][0] for i in range(1, len(candles))) or [0]
    _gap_main = _gaps[len(_gaps) // 2]
    _gap_share = sum(1 for g in _gaps if g == _gap_main) / len(_gaps) * 100

    state["_meta"] = {
        "asset": asset, "timeframe": timeframe, "bars": len(candles),
        "data_quality": {"ok": _dup <= 20 and _gap_share >= 50,
                         "dup_close_pct": round(_dup, 1),
                         "gap_main_sec": _gap_main, "gap_main_share_pct": round(_gap_share, 1)},
        "candle_last_ts": candles[-1][0],
        "oi_source": oi_src, "oi_abs": oi_abs, "oi_window": oi_window,
        "funding_raw": funding[0] if funding else None,
        "funding_hist_n": len(funding_hist) if funding_hist else 0,
        "top_long_short_ratio": top[0] if top else None,
        "retail_long_short_ratio": retail[0] if retail else None,
        "cvd_points": len(cvd) if cvd else 0,
        "cvd_aligned": bool(aligned_closes),
    }
    state["_trend_detail"] = {
        "aroon_up": ti.aroon(candles)["up"][-1],
        "aroon_down": ti.aroon(candles)["down"][-1],
        "supertrend_dir": ti.supertrend(candles)["direction"][-1],
        "supertrend_line": ti.supertrend(candles)["line"][-1],
    }
    # v412：风险档位（仓位系数 + 止损 ATR 倍数）——纯执行建议，不进方向分
    state["risk"] = risk_profile(state, timeframe)
    return state


def bias_of(state: Dict[str, Any]) -> Dict[str, Any]:
    """把六个子维度各自声明的偏向加权合成一个 [-1, 1] 的盘面偏置。

    重点：**这不是预测模型**，只是把已经算出来的读法汇总成一个标量，
    方便闸门比较\"新闻方向 vs 盘面偏置\"；解释必须回到子维度。
    权重只在**真算出来**的维度上归一，缺维度丢权重而不是记 0 分。

    v408 回测结论（`scripts/state_bias_validation.py`，因果重算、无前视）：
      日线 BTC 两段各 600 样本、ETH 600 样本，偏置方向命中 46%~50%，
      与"无脑做多"基线（48%~52%）无可分离差异，IC ≈ 0。
      → **偏置只能当描述与反方证据，不能当方向策略**；`usable` 的含义是
      "读得出来"，不是"预测得准"。闸门那边只允许它做折减，不允许它反向开仓。
    """
    if "error" in state:
        return {"bias": None, "coverage": 0.0, "usable": False,
                "components": {}, "missing": ["全部"]}

    comps: Dict[str, float] = {}

    trend = state.get("trend") or {}
    adx = float(trend.get("adx") or 0.0)
    plus_di = float(trend.get("plus_di") or 0.0)
    minus_di = float(trend.get("minus_di") or 0.0)
    if trend.get("trend_dir") in ("up", "down") and adx > 0:
        # ADX 20 以下趋势不可用，40 以上按满强度计
        comps["trend"] = (1.0 if trend["trend_dir"] == "up" else -1.0) * min(1.0, adx / 40.0)
    elif adx >= 20 and (plus_di + minus_di) > 0:
        # 摆动结构 mixed（没有 HH/LL 定论）时，用 DI 的偏向给半个强度：
        # 否则「ADX 44 但结构混乱」会完全不参与合成，只剩单维度决定偏置。
        comps["trend"] = ((plus_di - minus_di) / (plus_di + minus_di)) * 0.5

    lev = state.get("leverage") or {}
    if lev.get("bias"):
        lev_bias = {"bull": 1.0, "bear": -1.0, "bull_weak": 0.5, "bear_weak": -0.5}.get(lev["bias"])
        if lev_bias is not None:
            comps["leverage"] = lev_bias * float(lev.get("strength") or 1.0)

    flow = (state.get("flow") or {}).get("taker") or {}
    if flow.get("bias"):
        flow_bias = {"bull": 1.0, "bear": -1.0}.get(flow["bias"])
        if flow_bias is not None:
            comps["flow"] = flow_bias * float(flow.get("strength") or 0.0)

    don = trend.get("donchian_position")
    if don is not None:
        comps["donchian"] = (float(don) - 0.5) * 2.0

    pos = state.get("positioning") or {}
    if pos.get("bias"):
        # 散户追多/追空按逆向计，但只给半强度：这是群体位置，不是价格方向
        pos_bias = {"bull": 1.0, "bear": -1.0}.get(pos["bias"])
        if pos_bias is not None:
            comps["positioning"] = pos_bias * 0.5

    va = state.get("value_area") or {}
    pos_va = va.get("position")
    if pos_va in ("above_value", "below_value"):
        comps["value_area"] = 0.5 if pos_va == "above_value" else -0.5

    w_sum = sum(_BIAS_WEIGHTS[k] for k in comps if k in _BIAS_WEIGHTS)
    if not w_sum:
        return {"bias": None, "coverage": 0.0, "usable": False,
                "components": {}, "missing": ["全部"]}
    bias = sum(comps[k] * _BIAS_WEIGHTS[k] for k in comps if k in _BIAS_WEIGHTS) / w_sum
    total_w = sum(_BIAS_WEIGHTS.values())
    value = round(max(-1.0, min(1.0, bias)), 3)
    coverage = round(w_sum / total_w, 3)
    return {
        "bias": value,
        "coverage": coverage,
        # usable 的门槛比"有数"更严：覆盖不到一半维度、或偏置绝对值不够大，
        # 都只当描述，不给闸门拿去当反方证据——单维度推出来的偏置很容易是噪声。
        "usable": bool(coverage >= 0.5 and abs(value) >= 0.45),
        "components": {k: round(v, 3) for k, v in comps.items()},
        "missing": [k for k in _BIAS_WEIGHTS if k not in comps],
    }


def for_asset(asset: str, timeframe: str = "4h", bars: int = 240,
              db_path: Optional[str] = None, use_cache: bool = True) -> Dict[str, Any]:
    """给快照/闸门用的轻封装：自己开只读连接，带 90s 缓存，永不抛异常。"""
    key = f"{asset}|{timeframe}|{bars}|{db_path or get_db_path()}"
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and now - hit["ts"] < _CACHE_TTL:
                return hit["state"]
    try:
        conn = _ro_conn(db_path)
        try:
            state = build_state(conn, asset, timeframe, bars)
        finally:
            conn.close()
    except Exception as exc:
        state = {"error": f"{type(exc).__name__}: {str(exc)[:120]}", "missing": ["全部"]}
    if "error" not in state:
        state["bias_detail"] = bias_of(state)
        state["risk"] = risk_profile(state, timeframe)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = {"ts": now, "state": state}
    return state


# ---------------------------------------------------------------------------
# 风控画像（v412）
# ---------------------------------------------------------------------------
# 依据是实测，不是拍脑袋：v411 在 BTC/XAU/ETH 上逐时点算过"5 根内的最大不利偏移
# （MAE）÷ 当时的 ATR%"，中位比值是 **日线 ≈0.42~0.52、4h ≈0.48~1.32**。
# 也就是说：5 根之内通常要挨 0.5~1.3 倍 ATR 的逆风，**止损放在 1.5 倍 ATR 以内会被
# 正常波动打掉**。所以这里给的是"档位 + ATR 倍数"，而不是拍一个固定百分比。
_ADVERSE_ATR = {"1d": 0.45, "4h": 0.9, "1h": 0.55}      # 实测中位：5 根内逆风 ≈ 这么多倍 ATR%
# v418：5 根内「顺风中位 ÷ 逆风中位」（赔率）。实测见 reports/intraday_risk/conclusions.md
#  1h：BTC 0.62~0.87、XAU 0.49~0.77、ETH 0.81~1.19 → 取 0.70
#  1d：BTC 0.96~1.41、XAU 1.08~1.92、XAG 1.07~1.49 → 取 1.32
#  4h：只有 80 个采样点、且全落在单边下跌段（BTC 0.28~0.36）→ 不给数，标"未测"
_ODDS_TYPICAL = {"1h": 0.70, "1d": 1.32}
# v419：赔率随持有期的走势（3/5/10/20 根，见 reports/intraday_risk/conclusions.md）
#  1h：BTC 0.81→0.56、XAU 0.66→0.59、ETH 0.84→0.59 —— **越拿越差**
#  4h：BTC 0.53→0.23、ETH 0.44→0.17 —— 同样递减（样本仅 80 点）
#  1d：BTC 1.34→1.37、XAU 1.56→1.53、XAG 1.25→1.41 —— 平稳 >1
# 结论：日内不是"该做波段"，而是"方向本身没有边际"，拿久了只是把逆风摊得更大。
_MIN_HOLD_BARS = {"1d": 3, "1h": None, "4h": None}
# v420：来回成本。实测 news_replay 2028 笔的 cost_bps 中位 = 30bp（含双边手续费+滑点）。
# 这条是硬地板：预期空间连成本都不够时，数学上必亏，任何"指标好看"都救不回来。
_COST_BPS = 30.0
_ODDS_SAMPLE = {
    "1h": "BTC/XAU/ETH 各约 280~320 个采样点（1h，5 根前瞻）",
    "1d": "BTC/XAU/XAG 各约 1150 个采样点（10 年日线，5 根前瞻）",
    "4h": "样本不足：BTC/ETH 各 80 点且全在下跌段，不给数",
}
_RISK_LEVELS = {
    "低": {"size_factor": 1.0, "stop_atr_multiple": 1.5},
    "中": {"size_factor": 0.8, "stop_atr_multiple": 2.0},
    "高": {"size_factor": 0.5, "stop_atr_multiple": 3.0},
}

# v414：日线按 **ATR 绝对百分比** 分档（不是分位、不是打分）。
# 实测依据（scripts/risk_tier_validation.py + 手工 ATR 三分核对）：
#   XAU 1d  |MAE5| 中位：低(ATR<1.5%)0.46% / 中(1.5~3%)0.87% / 高(≥3%)1.63% → 3.54 倍
#   XAG 1d  0.83 / 1.10 / 1.97 → 2.38 倍；BTC 1d 中 1.13 / 高 1.96 → 1.73 倍（无低档样本）
#   4h 上同样分档没有关系（BTC 0.80、ETH 1.00）→ 日内不做仓位缩放，只调止损。
_RISK_BANDS_DAILY = ((3.0, "高"), (1.5, "中"), (0.0, "低"))
_RISK_BANDS_INTRADAY = ((2.0, "高"), (1.0, "中"), (0.0, "低"))
_INTRADAY_STOP_ATR = 2.0  # 4h 实测 |MAE5|÷ATR 中位 0.9~1.3 → 2 倍留余量

# v417：把"止损距离"换算成"仓位金额"的锚 —— 单笔最多亏多少本金。
# 这是资金管理里唯一不需要预测能力的部分：止损放 2×ATR（3.8%）时，
# 想让"被打掉只亏 1% 本金"，仓位就只能开到 1% / 3.8% ≈ 26% 名义敞口。
_MAX_RISK_PCT = 1.0
_POSITION_FLOOR_PCT = 1.0    # 不低于 1% 名义敞口（再小就没有交易意义）
_POSITION_CAP_PCT = 100.0    # 不加杠杆，上限 100%

# v479：**收益侧验证结论**（reports/tech_engine/risk_sizing_replay.md，只读回测）。
# v414 只验过风险侧（5 根内 |MAE|），这次把收益侧补上了，结论是**不支持**：
#   1) 降回撤是真的：16/16 个标的 MDD 变小；
#   2) 但离散三档提升不了风险调整收益：Calmar 仅 4/16 高于满仓，符号翻转后 0/16；
#   3) 连续等风险版样本内好看（9/16、翻转后优于离散档 15/16），
#      可**时间外滚动窗口胜率只有 26.5%**（98 个窗口赢 26 个）→ 典型的样本内拟合。
# 所以 size_factor 必须继续只当「资金风险对齐」，不许被拿来当收益增强；
# 任何想改仓位口径的改动，先过时间外检验这一关（不能只看翻转对照）。
_RISK_SIZING_EVIDENCE: Dict[str, Any] = {
    "as_of": "2026-11-19",
    "report": "backend/reports/tech_engine/risk_sizing_replay.md",
    "scope": "16 标的：XAU/XAG/HG 十年日线 + 13 只美股/指数近四年，2600+/1000+ 根",
    "risk_side": "MDD 16/16 变小（档位识别波动成立）",
    "return_side_discrete": "Calmar 仅 4/16 优于满仓；符号翻转后 0/16 → 等价于降杠杆",
    "return_side_continuous": "样本内 9/16，时间外滚动窗口胜率 30.8%（20/65，已剔除基准过弱的窗）→ 样本内拟合",
    "verdict": "not_supported",
    "rule": "size_factor 只做资金风险对齐，不得当收益增强；改仓位口径必须先过时间外检验",
}


def risk_profile(state: Dict[str, Any], timeframe: Optional[str] = None) -> Dict[str, Any]:
    """把状态读数翻成"该用多大仓位、止损放几倍 ATR"——**不改任何方向分**。

    为什么不做成方向信号：v408~v411 已经证明偏置没有方向优势（五个资产、七个时段）。
    但"局面有多险"是可测的：波动在扩张、价格离开价值区、趋势极端、杠杆在收缩、
    多空一侧拥挤 —— 这些同时出现时，5 根内的逆风明显更大，仓位和止损就该跟着变。

    返回：
      level（低/中/高）、size_factor（建议仓位系数）、stop_atr_multiple（止损 ATR 倍数）、
      atr_pct、expected_adverse_pct（按实测倍数估算的"常见逆风"）、drivers（命中原因）、note。
    """
    if not isinstance(state, dict) or state.get("error"):
        return {"available": False, "detail": "无状态读数"}

    vo = state.get("volatility") or {}
    tr = state.get("trend") or {}
    va = state.get("value_area") or {}
    lev = state.get("leverage") or {}

    score = 0
    drivers: List[str] = []

    bbw = vo.get("bb_width_percentile")
    if bbw is not None:
        if bbw >= 80:
            score += 2
            drivers.append(f"波动扩张（带宽 {bbw:.0f} 分位）")
        elif bbw >= 60:
            score += 1
            drivers.append(f"波动偏阔（带宽 {bbw:.0f} 分位）")

    rvp = vo.get("realized_vol_percentile")
    if rvp is not None and rvp >= 80:
        score += 1
        drivers.append(f"已实现波动 {rvp:.0f} 分位")

    adx = tr.get("adx")
    if adx is not None and adx >= 35:
        score += 1
        drivers.append(f"趋势极端（ADX {adx:.0f}）")

    if va.get("position") in ("below_value", "above_value"):
        score += 1
        drivers.append("价格已离开价值区")

    if lev.get("label") in ("减仓下跌", "加仓下跌", "减仓上涨"):
        score += 1
        drivers.append(f"{lev['label']}（杠杆在撤）")

    if lev.get("crowding") in ("空头拥挤", "多头拥挤"):
        score += 1
        drivers.append(lev["crowding"])

    # v414：**档位改由 ATR 分位决定，不再由打分决定**。
    # 原因（实测，见 reports/risk_tiers/latest.md）：按原打分制分档后，高档的 5 根 |MAE|
    # 反而更小（BTC 4h 高/低=0.44、ETH 0.33、XAG 0.39），因为打分里 ADX/带宽/价格位置
    # 都是**滞后**指标——它在"已经动完"之后才亮，把事后平静期标成了高风险。
    # 而 ATR 分位本身在日线是有区分力的（ATR 高/低三分：BTC 1d 2.48×、XAU 2.15×、XAG 2.08×）。
    # 所以：仓位与止损只跟波动尺度走（可验证），打分只留作解释（say why it feels risky）。
    atr = vo.get("atr_pct")
    atr_pctile = vo.get("atr_pct_percentile")
    tf = timeframe or (state.get("_meta") or {}).get("timeframe") or "4h"
    is_daily = str(tf).endswith("d")
    bands = _RISK_BANDS_DAILY if is_daily else _RISK_BANDS_INTRADAY
    level = "低"
    if atr is not None:
        for threshold, name in bands:
            if float(atr) >= threshold:
                level = name
                break
    equal_size = None   # v415：等风险仓位（仅日内计算，日线由档位本身缩仓）
    if is_daily:
        cfg = _RISK_LEVELS[level]
        size_basis = "atr_band_daily" if atr is not None else "fallback"
    else:
        # 日内：档位只描述"波动有多大"，仓位不缩（实测无区分力，缩仓位=迷信）
        cfg = {"size_factor": 1.0, "stop_atr_multiple": _INTRADAY_STOP_ATR}
        size_basis = "uniform_intraday" if atr is not None else "fallback"
        # v415：日内 1h 实测（BTC/XAU/ETH 各 400 点）—— 绝对值上"波动大→逆风大"
        # （上/下三分位 1.7~3.5 倍），但按 ATR 归一后高波动档反而更小（0.44~0.55 × ATR，
        # 低波动 0.48~0.58）。所以：固定 ATR 倍数止损在日内不会被高波动更容易打掉，
        # 该按"等风险"调的是**仓位金额**（ATR 越贵、同样的钱挨的百分比越大）。
        # 这条只做资金风险对齐，没有收益侧验证，所以单独给 equal_risk_size，不动 size_factor。
        if atr and vo.get("atr_pct_median"):
            raw = float(vo["atr_pct_median"]) / float(atr)
            equal_size = round(min(1.5, max(0.5, raw)), 2)
    k = _ADVERSE_ATR.get(tf, 0.9)
    expected = round(atr * k, 2) if atr else None

    # v417：止损距离 → 仓位金额（按单笔 _MAX_RISK_PCT% 本金风险）
    stop_pct = (round(float(atr) * float(cfg["stop_atr_multiple"]), 2) if atr else None)
    position_pct = None
    if stop_pct:
        # 基准敞口 = 单笔风险预算 / 止损距离；再乘以**已验证的档位系数**
        # （日线 0.5/0.8/1.0、日内 1.0），避免"档位"和"风险预算"各说一套。
        raw_pos = _MAX_RISK_PCT / stop_pct * 100.0 * float(cfg["size_factor"])
        position_pct = round(min(_POSITION_CAP_PCT, max(_POSITION_FLOOR_PCT, raw_pos)), 1)
    # 等风险口径再叠一层（仅日内有 equal_size）：同样 1% 预算、波动贵时敞口更小
    position_pct_equal = None
    if position_pct is not None and equal_size:
        # 等风险口径：基准敞口（未乘 size_factor，日内 size_factor=1.0）再乘等风险系数
        base_pos = _MAX_RISK_PCT / stop_pct * 100.0
        position_pct_equal = round(min(_POSITION_CAP_PCT,
                                       max(_POSITION_FLOOR_PCT, base_pos * equal_size)), 1)

    # v418：赔率基准 —— "赚多少才值得挨这波逆风"
    odds = _ODDS_TYPICAL.get(tf)
    cost_pct = round(_COST_BPS / 100.0, 2)
    need = None
    if expected is not None:
        need = float(expected) / odds if odds else float(expected)
    # v420：成本地板 —— 低于来回成本的要求空间等于"保证亏"，直接抬到成本线
    require_space = None
    require_basis = None
    if need is not None:
        require_space = round(max(need, cost_pct), 2)
        if need < cost_pct:
            require_basis = "cost"          # 赔率/逆风要求低于成本 → 成本地板生效
        else:
            require_basis = "odds" if odds else "adverse"   # 4h 无赔率时是逆风线

    # v419：持有期侧 —— 赔率曲线决定"要不要拿久一点"，而不是只决定"要不要做"
    min_hold = _MIN_HOLD_BARS.get(tf)
    if min_hold:
        holding_note = (f"该周期赔率在 {min_hold} 根处即 >1，且 3~20 根稳定在 1.2~1.6："
                        "拿久一点不会变差，方向可持有")
    elif tf in ("1h", "4h"):
        holding_note = ("该周期 3~20 根赔率一路往下（1h 0.84→0.56、4h 0.53→0.23）："
                        "拿久不会变好，只会把逆风摊得更大 → 日内不做方向，只按逆风预算控仓")
    else:
        holding_note = None

    note = (f"{level}波动档：建议仓位×{cfg['size_factor']}、止损放 {cfg['stop_atr_multiple']} 倍 ATR"
            + (f"（≈{round(atr * cfg['stop_atr_multiple'], 2)}%）" if atr else "")
            + (f"；5 根内常见逆风 ≈{expected}%" if expected is not None else "")
            + (f"；ATR {atr:.2f}%" if atr is not None else "")
            + (f"（{atr_pctile:.0f} 分位）" if atr_pctile is not None else ""))
    return {
        "available": True,
        "level": level,
        "score": score,
        "size_factor": cfg["size_factor"],
        "stop_atr_multiple": cfg["stop_atr_multiple"],
        "atr_pct": atr,
        "atr_pct_percentile": round(atr_pctile, 2) if atr_pctile is not None else None,
        "expected_adverse_pct": expected,
        # v417：执行层要的是"开多大"，不是"乘几倍"——这里给出以本金为分母的名义敞口
        "stop_pct": stop_pct,
        "max_risk_pct": _MAX_RISK_PCT,
        "position_pct": position_pct,
        "position_pct_equal_risk": position_pct_equal,
        "drivers": drivers,
        "note": note,
        # v414：口径与验证状态必须随身携带，否则下游会把"高波动档"误读成"高综合风险"
        "size_basis": size_basis,
        "atr_median_pct": vo.get("atr_pct_median"),
        "atr_vs_median": (round(float(atr) / float(vo["atr_pct_median"]), 2)
                          if atr and vo.get("atr_pct_median") else None),
        "equal_risk_size": equal_size,
        "equal_risk_note": ("等风险仓位：当前 ATR 是近 60 根中位的 {:.2f} 倍，"
                            "同样的钱要按 ×{} 调才保持同等资金风险（只对齐风险，不预测方向）"
                            .format(float(atr) / float(vo["atr_pct_median"]), equal_size)
                            if (atr and vo.get("atr_pct_median") and equal_size) else None),
        # v418：赔率侧（顺风）—— 只有逆风预算会把"看起来能赚"的单子一律判 HOLD，
        # 补上实测赔率才说得清"要赚多少才算够本"
        "odds_typical": odds,
        "odds_sample": _ODDS_SAMPLE.get(tf),
        # v419：持有期侧
        "min_hold_bars": min_hold,
        "holding_note": holding_note,
        "require_space_pct": require_space,
        # v420：成本地板与依据
        "round_trip_cost_pct": cost_pct,
        "require_basis": require_basis,
        "odds_note": (f"该周期实测赔率 {odds}（顺风中位÷逆风中位），"
                      f"所以要让期望为正，预期空间至少要到 5 根内常见逆风的 "
                      f"{round(1.0 / float(odds), 2)} 倍（≈{require_space}%）"
                      if (odds and require_space is not None) else
                      ("该周期赔率样本不足（4h 只有 80 点且全在下跌段），"
                       "只按「预期空间 ≥ 常见逆风」这条保守线卡，不额外放宽" if odds is None else None)),
        "validated_timeframes": ["1d"],
        # v479：收益侧验证不成立 —— 这句话必须随读数一起走，否则下游会把 size_factor 当 alpha
        # v486：深拷贝。旧版返回模块级 dict 的引用，任何调用方改一下
        # r["sizing_evidence"]["verdict"] 就会永久污染所有后续读数。
        "sizing_evidence": copy.deepcopy(_RISK_SIZING_EVIDENCE),
        "validation_note": ("仓位按 ATR 绝对百分比分档只在日线有实测支撑（XAU 1d 3.54×、"
                            "XAG 2.38×、BTC 1.73×）；4h 上分档与 5 根 |MAE| 无关系（BTC 0.80×、"
                            "ETH 1.00×），所以日内只按 ATR 调止损、不缩仓位。"
                            "**收益侧（v479）已验证不成立**：降回撤 16/16，但 Calmar 仅 4/16、"
                            "时间外滚动胜率 30.8%（20/65）—— size_factor 只做风险对齐，不做收益增强"),
    }


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def summary_line(state: Dict[str, Any], asset: str) -> str:
    """一行版读数，塞进 market_snapshot 的摘要（LLM 每轮都会看到）。"""
    if "error" in state:
        return f"{asset} 状态读数不可用（{state['error'][:40]}）"
    text = state.get("readout") or "（无可用维度）"
    bd = state.get("bias_detail") or {}
    tag = ""
    if bd.get("bias") is not None:
        tag = f" [偏置 {bd['bias']:+.2f}/覆盖 {bd['coverage']:.0%}]"
    missing = state.get("missing") or []
    if missing:
        tag += f" [缺: {'、'.join(missing)}]"
    return text + tag


def block(assets: List[str], timeframe: str = "4h", max_chars: int = 900) -> str:
    """给 LLM 用的整段读数（含使用要求）。无可用资产 → 空串，调用方不注入。"""
    rows: List[str] = []
    for asset in assets:
        state = for_asset(asset, timeframe=timeframe)
        if "error" in state:
            rows.append(f"  - {asset}: 不可用（{state['error'][:60]}）")
            continue
        bd = state.get("bias_detail") or {}
        meta = state.get("_meta") or {}
        bias_txt = f"偏置 {bd.get('bias'):+.2f}" if bd.get("bias") is not None else "偏置 N/A"
        line = f"  - {asset} {timeframe}: {state.get('readout') or '（无可用维度）'}"
        line += f"\n    合成{bias_txt}（覆盖 {bd.get('coverage', 0):.0%}）"
        if meta.get("oi_window"):
            line += f" | OI 窗口 {meta['oi_window']}"
        miss = state.get("missing") or []
        if miss:
            line += f" | 缺: {'、'.join(miss)}"
        risk = state.get("risk") or {}
        if risk.get("available"):
            line += f"\n    风控档位 {risk.get('level')}：仓位×{risk.get('size_factor')}、"
            line += f"止损 {risk.get('stop_atr_multiple')} 倍 ATR"
            if risk.get("expected_adverse_pct") is not None:
                line += f"（5 根内常见逆风 ≈{risk['expected_adverse_pct']}%）"
            # v418/v419：赔率与持有期 —— 光有逆风会把"看起来能赚"的单子一律判 HOLD
            if risk.get("require_space_pct") is not None:
                line += (f"\n    赔率    该周期实测 {risk.get('odds_typical')}，"
                         f"预期空间需 ≥{risk['require_space_pct']}% 才够本")
            if risk.get("holding_note"):
                line += f"\n    持有    {risk['holding_note']}"
        rows.append(line)
    if not rows:
        return ""
    header = ("═══ 技术状态读数（本地 K 线 + 持仓/主动成交/多空比）═══\n"
              "用途：判断现在是趋势还是震荡、谁在加杠杆、谁在追。这是**状态描述**，不是入场信号。\n")
    footer = ("\n要求：[水位博弈] 段必须引用本次读数中的 ≥1 个具体维度（趋势阶段/波动分位/价值区位置/"
              "价-OI 象限/主动买占比/大户 vs 散户）；\n"
              "  - 「合成偏置」由各维度已声明的偏向加权而成，不是预测，不得当作方向结论；\n"
              "  - 标「缺」的维度是没数据，不是中性，禁止用中性口径替代；\n"
              "  - 读数与新闻方向相反时，必须在 [逻辑自检] 里说明为何仍按新闻方向行动；\n"
              "  - 风控档位必须按读数里的 size_factor / stop_atr_multiple 写执行口径"
              "（日线高档才是 0.5× 仓位；日内档位只调止损、不缩仓位），"
              "不得因为方向成立就忽略止损。\n")
    text = header + "\n".join(rows) + footer
    if len(text) > max_chars:
        text = text[: max_chars - 40] + "\n...（读数已截断）"
    return text
