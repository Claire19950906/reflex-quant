"""
backfill_critical_moves.py
==========================

从 external_indicators 真实 yfinance 数据回填 critical_price_moves。
绝不伪造任何价格 — 全部从 DB 已有的真实日线推算。

检测规则 (基于真实日线数据,符合 v291 临界点定义):
  慢涨/慢跌 (slow_*) — 用 1d/3d/5d 累计涨跌幅检测
  快涨/快跌 (fast_*)  — v310 P0 新增: 用**真实 intraday 数据** (ohlcv_intraday)
                       按 1h 窗口检测。旧版本写的是"1d 粒度无法检测 5min,
                       留空待真实 intraday 数据接入" —— 而今 intraday 已有
                       110816 行 / 10 品种, 前置条件早已满足, 却一直没实现,
                       导致能力验收 #7 "事件驱动K线(快涨/慢涨)" 永远 FAIL。

阈值 (依据市场惯例 + 实测 intraday 分布, 可后续校准):
  slow_rise / slow_fall: |1d| ≥ 3%  OR  |3d| ≥ 5%  OR  |5d| ≥ 7%
  fast_rise / fast_fall: |1h| ≥ 1.5%   (duration_min = 60)
      · 与 slow 形成对照: 同样量级的幅度, 时间压缩 24 倍 → 才是"快"
      · 1.5% 的来历: 实测 1m bar 的 1h 窗口涨跌幅分布
        BTC p99≈2.4% · ETH p99≈3.6% · SOL p99≈4.2% · XAU p99≈1.1%
        → 1.5% 落在主流品种 p97~p99 之上, 只捕捉真实急拉/急跌, 不放水
      · 量纲跳变保护: |pct| > 90% 判为数据污染 (沿用 slow 口径)
  duration_min: slow=1440 (1天) / fast=60 (1小时)
  algo_score: 0.4~0.95 区间线性,综合幅度+持续性
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

import epoch_gate
import series_plan

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 检测阈值
# v342: 多日阈值下调（3d 5%→3.5%、5d 7%→5%）。
# 实测原口径把 BTC 的 5 日阈值抬到 12.9%：5 天跌 13% 才刚够线，
# 慢端每品种一年只出几次，等于没在监控"慢涨/慢跌"。
THRESH_1D_PCT = 3.0
THRESH_3D_PCT = 3.5
THRESH_5D_PCT = 5.0

# v310 P0: fast 异动 (真实 intraday)
THRESH_FAST_1H_PCT = 1.5
FAST_WINDOW_MIN = 60
# 量纲跳变硬闸: |pct| > 90% 对任何资产都不成立 → 判为数据污染, 不产出 move
MAX_PLAUSIBLE_PCT = 90.0
# interval 名 → 秒 (选"时间分辨率最细"的那一档)
INTERVAL_SECONDS = {
    "1m": 60, "2m": 120, "3m": 180, "5m": 300, "10m": 600, "15m": 900,
    "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
}

# 每类临界点的基准分数 (与 algo_score 公式加权)
BASE_SCORE = {
    "slow_rise": 0.5,
    "slow_fall": 0.55,  # 跌更敏感
    "fast_rise": 0.65,
    "fast_fall": 0.70,  # 快速异动信息量更大
}


JUMP_PCT = 50.0        # 单日超过这个幅度, 任何股/币都不成立（含 7:1 拆股后的 85.7%）
LOOKAHEAD = 3          # 往后看几根来判断"尖刺"还是"水平跳变"
RETURN_TOL = 30.0      # 回到原水平(30% 内) = 尖刺


CRYPTO_CODES = {"BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "BNB", "LTC", "AVAX",
                "LINK", "DOT", "MATIC", "TRX", "TON", "SHIB", "PEPE"}


def has_splits(asset: str) -> bool:
    """该品种是否会有拆股/并股这种"公司行为"。

    加密没有拆股：DOGE 从 0.002 涨到 0.7 是真的涨了 350 倍，不是 1:350 的拆股。
    把它当拆股"归平"，等于把真实牛市从历史里擦掉 —— 之前就是这么干的。
    """
    a = (asset or "").strip().upper()
    try:
        import asset_alias
        a = asset_alias.canonical(a)
    except Exception:
        pass
    base = a.replace("-USD", "").replace("-USDT", "").replace("USDT", "").strip("-")
    if a.endswith(("-USD", "-USDT")) or base in CRYPTO_CODES:
        return False
    return True


MAX_GAP_SEC = 6 * 86400   # 相邻两根超过 6 天 = 数据空洞，不是公司行为


def sanitize_price_series(
    prices: list[tuple[int, float]],
    asset: str = "",
    jump_pct: float = JUMP_PCT,
    allow_level_adjust: bool | None = None,
    max_gap_sec: int = MAX_GAP_SEC,
) -> list[tuple[int, float]]:
    """v341: 序列层面的脏点剔除 + 公司行为归平。

    实测（BTC, ohlcv_daily, source=kline_bars_1d）：
        2023-11-04  35069.7
        2023-11-06  94900.0   ← 脏点（2024 的价格错配到 2023 的日期）
        2023-11-07  94700.0   ← 同上（连着两根）
        2023-11-07  35406.5
    一根错日 K 线，会让 5 日窗口里几十个检测点全变成 "+170% 异动"；
    旧逻辑只在**单条 move** 上打 SKIP，脏点周围的窗口照样被污染。

    同时实测到另一类完全不是脏点的东西 —— **拆股**：
        AAPL 2014-06-09  645.57 → 93.70   （7:1）
        NVDA 2021-07-20  751.19 → 186.12  （4:1）
        NVDA 2024-06-10 1208.88 → 121.79  （10:1）
        TSLA 2020-08-31  2213.40 → 498.32 （5:1）
    这些若当成行情，就是"一天崩 85%"。

    区分只看形状，不猜业务：
      · 跳变后几根内回到原水平 → 尖刺（脏数据）→ 删掉这一段
      · 跳变后价格停在新水平 → 水平跳变（拆股/换源）→
        把跳变前的整段按 (新水平/旧水平) 归平，历史全部保留
    """
    work = list(prices)
    drops = 0
    if allow_level_adjust is None:
        allow_level_adjust = has_splits(asset)
    start = 1        # 已判定"真实跳变、不归平"的位置往后继续扫尖刺
    for _ in range(5):
        brk = -1
        for i in range(start, len(work)):
            a, b = work[i - 1][1], work[i][1]
            # v341: 只有**相邻交易日**之间的跳才可能是拆股/换源。
            # 实测 AAPL 有一处 32.20→91.13 跨了 808 天、NVDA 7.86→15.96 跨了 1720 天，
            # 那是数据空洞（中间没有 K 线），价格涨 2 倍是几年累积的真实涨幅；
            # 旧逻辑把它当"公司行为"把前面 1053 根价格整段 ×2.83 改写，等于篡改历史。
            if work[i][0] - work[i - 1][0] > max_gap_sec:
                continue
            if a > 0 and abs((b - a) / a * 100.0) > jump_pct:
                brk = i
                break
        if brk < 0:
            break
        before = work[brk - 1][1]
        # 往后看 LOOKAHEAD 根，找第一根"回到原水平"的
        back = None
        for j in range(brk, min(len(work), brk + LOOKAHEAD + 1)):
            if abs(work[j][1] - before) / before * 100.0 < RETURN_TOL:
                back = j
                break
        if back is not None:
            gone = back - brk          # back 那根是"回到原水平"的正常点，要留下
            work = work[:brk] + work[back:]
            drops += gone
            start = 1
            print(f"  [SANITIZE] {asset or '?'}: 尖刺 {gone} 根已剔除"
                  f"({before:.2f} → {work[brk - 1][1]:.2f} 恢复)")
        else:
            if not allow_level_adjust:
                # 加密这类没有拆股的品种：翻倍/腰斩是真实行情，一律不归平。
                print(f"  [REAL-MOVE] {asset or '?'}: {before:.4g}→{work[brk][1]:.4g} "
                      f"判定为真实行情（该品种无拆股），不做归平")
                start = brk + 1
                continue
            after = work[brk][1]
            factor = after / before
            for k in range(brk):
                ts_k, v_k = work[k]
                work[k] = (ts_k, v_k * factor)
            print(f"  [SANITIZE] {asset or '?'}: 跳变 {before:.2f}→{after:.2f} "
                  f"判为公司行为/换源, 前 {brk} 根按 ×{factor:.4f} 归平")
    if drops:
        print(f"  [SANITIZE] {asset or '?'}: 合计剔除尖刺 {drops} 根")
    return work


_NON_PRICE_HINT = re.compile(
    r"(pct|chg|change|_vol|volume|supply|mcap|dominance|rate|ratio|share|"
    r"index|count|yoy|days|spread|premium|balance|reserve|hashrate|"
    r"active_addr|issuance|cap_usd|inflow|outflow|_high|_low|_open)", re.I)
_PRICE_HINT = re.compile(r"(price|_close|close|last|au9999|^dxy$|^vix$)", re.I)


def _pick_price_indicator(avail: dict) -> str | None:
    """在"非标准命名"的指标里挑一个像价格的（只挑一个，不混序列）。"""
    cands = [ind for ind in avail
             if _PRICE_HINT.search(ind) and not _NON_PRICE_HINT.search(ind)]
    if not cands:
        return None
    return max(cands, key=lambda i: avail.get(i, 0))


def _to_daily(prices: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """把"每 90 秒一根"的序列压成日线（每个自然日取最后一根）。

    实测：NG 的价格指标 hf_fut_price 是分钟级（431 个点在几小时内），
    直接当"日线"用会把 k=1 变成"90 秒"，1d/3d/5d 的口径全废。
    """
    if len(prices) < 3:
        return prices
    deltas = sorted(prices[i][0] - prices[i - 1][0] for i in range(1, len(prices)))
    median = deltas[len(deltas) // 2]
    if median >= 6 * 3600:
        return prices
    by_day: dict[int, tuple[int, float]] = {}
    for ts, v in prices:
        day = int(ts) // 86400
        by_day[day] = (ts, v)          # 同一天保留最后一根
    return [by_day[d] for d in sorted(by_day)]


def _ohlcv_prices(conn, asset: str) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close>0 ORDER BY ts ASC",
        (asset,)).fetchall()
    return sanitize_price_series(_to_daily([(int(r["ts"]), float(r["close"])) for r in rows]),
                                 asset=asset)


def fetch_prices(conn, asset: str, prefer_indicator: str | None = None) -> list[tuple[int, float]]:
    """返回 [(ts, close), ...] 升序

    v308 P1-fix3 (数据污染修复):
      旧实现第一个 SQL 的白名单是
          indicator IN ('close','price','000660.KS','005930.KS')
      —— 后两个是**资产代码**, 却被写进了 indicator 列表。
      external_indicators 里同一个 asset 下混有多种 indicator, 于是这条查询
      会把不同**量纲**的 value 混进同一条价格序列: BTC 曾出现
          start=77.48 → end=7722.72  (move_pct=+9867%)
          start=85786 → end=102.20   (move_pct=-99.88%)
      这类行不是市场异动, 是数据污染, 会让 A 维度"漏检数"虚高、
      attribution-stats 的 avg_move_pct 变成 9187%。

      修复: 只接受真正的价格类 indicator, 并且**一个资产只取一个** indicator
      (按优先级), 避免同资产多序列互相穿插。找不到就回退到"该资产唯一的
      indicator", 仍找不到则返回空 (不猜)。
    """
    PRICE_INDICATORS = ("close", "price", "adj_close", "last")
    rows = conn.execute(
        "SELECT indicator, ts, value FROM external_indicators "
        "WHERE asset=? ORDER BY ts ASC",
        (asset,),
    ).fetchall()
    if not rows:
        return []
    # 该资产实际有哪些 indicator（只留"kind=price"的：收益率/存量/市值/比率
    # 都不是价格，混进来就会把 4.19% 的国债收益率当成"金价 4 块钱"）
    import series_namespace as _sn

    avail: dict[str, int] = {}
    for r in rows:
        if _sn.resolve(asset, r["indicator"]).kind != "price":
            continue
        avail[r["indicator"]] = avail.get(r["indicator"], 0) + 1
    # 按优先级选一个; 同优先级取样本多的
    chosen = prefer_indicator if prefer_indicator in avail else None
    for ind in ([] if chosen else PRICE_INDICATORS):
        if avail.get(ind):
            chosen = ind
            break
    if chosen is None:
        # v341: 有些品种的价格指标**不叫 close**，实测：
        #   NG          → hf_fut_price        （天然气期货价）
        #   DXY         → DXY                 （指数本身就叫这个）
        #   SGE_GOLD    → sge_au9999_cny_g    （上海金 元/克）
        #   UST2Y/5Y/30 → UST2Y ...           （收益率，不是价格，别当行情）
        # 以前只认 close/price/adj_close/last，这些品种被判"指标不明确"→
        # 一行异动都不出。这里按**指标名语义**再捞一次，仍然宁可漏不可错：
        # 明显是比率/变化率/存量/市值的名字一律排除。
        chosen = _pick_price_indicator(avail)
    if chosen is None:
        # 只有一个 indicator → 用它
        if len(avail) == 1:
            chosen = next(iter(avail))
        else:
            # 多个非价格 indicator 且无法判定 → 改走 ohlcv_daily 的 close
            # （v340: 原来直接 return []，导致 BTC/ETH/XAU/WTI/DJI/IXIC 这些
            #   "指标很多"的核心品种长期没有 slow 异动 —— 慢涨识别其实只覆盖了山寨币）
            daily = conn.execute(
                "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close>0 "
                "ORDER BY ts ASC", (asset,)).fetchall()
            if daily:
                print(f"  [FALLBACK] {asset}: external_indicators 指标不明确 "
                      f"({len(avail)} 个) → 改用 ohlcv_daily.close {len(daily)} 根")
                return sanitize_price_series(
                    _to_daily([(int(r["ts"]), float(r["close"])) for r in daily]),
                    asset=asset)
            why = (f"价格类指标 {sorted(avail)}" if avail
                   else "没有价格类指标（收益率/存量/市值都不算价格）")
            print(f"  [SKIP] {asset}: {why} 且无 ohlcv_daily")
            return []
    out: list[tuple[int, float]] = []
    for r in rows:
        if r["indicator"] != chosen:
            continue
        v = r["value"]
        if v is None or v <= 0:
            continue
        out.append((r["ts"], float(v)))
    out.sort(key=lambda x: x[0])
    res = sanitize_price_series(_to_daily(out), asset=asset)
    # v341: external_indicators 里有些“价格”其实只有最近几天快照
    # （XAU/WTI/XAG 压成日线后只剩 3 根），拿去算 1d/3d/5d 等于用 3 个点编故事。
    # 日线表有更长的历史就换它。
    # v341 补充：有些"价格"指标只有最近 60 个快照（DOGE/XRP 实测），过了 30 根
    # 的下限就会拿 60 个点算 1d/3d/5d，等于只覆盖最近两个月，前几年的行情全漏。
    # 判据从"绝对下限"改成"相对长度"：日线历史明显更长就换。
    if len(res) < 250:
        daily = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close>0 "
            "ORDER BY ts ASC", (asset,)).fetchall()
        if len(daily) > 2 * len(res) or (len(res) < 30 and len(daily) > len(res)):
            print(f"  [SWITCH] {asset}: 指标序列只有 {len(res)} 根 → 改用 ohlcv_daily {len(daily)} 根")
            return sanitize_price_series(
                _to_daily([(int(r["ts"]), float(r["close"])) for r in daily]),
                asset=asset)
    return res


def fetch_intraday_series(conn, asset: str) -> tuple[list[tuple[int, float]], int]:
    """返回该资产**时间分辨率最细**的 intraday 收盘价序列 [(ts, close), ...] 升序
    与该档的 bar 秒数。

    v310 P0: fast 异动必须用真实 intraday 数据 —— 1d 粒度检测不出快涨快跌。

    选档规则: 取 interval 秒数最小 (分辨率最细) 且样本 ≥ 100 的那一档。
      · 分辨率越细, 60min 窗口越贴近真实 (1m bar 覆盖 73976 行 / 9 品种);
      · closed=0 的 bar 是未收盘的半根 K 线 → 不用;
      · close<=0 / 非数的行 → 跳过 (不猜)。
    """
    cands = conn.execute(
        "SELECT interval, COUNT(*) n FROM ohlcv_intraday "
        "WHERE asset=? AND closed=1 GROUP BY 1", (asset,)).fetchall()
    best: tuple[int, str] | None = None
    for r in cands:
        iv = str(r["interval"] or "")
        sec = INTERVAL_SECONDS.get(iv)
        if not sec or int(r["n"] or 0) < 100:
            continue
        if best is None or sec < best[0]:
            best = (sec, iv)
    if best is None:
        return [], 0
    _sec, iv = best
    rows = conn.execute(
        "SELECT ts, close FROM ohlcv_intraday WHERE asset=? AND interval=? AND closed=1 "
        "AND close IS NOT NULL AND close > 0 ORDER BY ts", (asset, iv)).fetchall()
    out: list[tuple[int, float]] = []
    seen_ts: set[int] = set()
    for r in rows:
        ts = int(r["ts"] or 0)
        if ts <= 0 or ts in seen_ts:
            continue
        try:
            c = float(r["close"])
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        seen_ts.add(ts)
        out.append((ts, c))
    return out, best[0]


def detect_fast_moves(series: list[tuple[int, float]],
                      bar_sec: int = 60,
                      window_min: int = FAST_WINDOW_MIN,
                      thresh: float = THRESH_FAST_1H_PCT) -> list[dict]:
    """在 intraday 序列上检测"短时间内大幅变动"的快速异动。

    口径 (与 slow 互为对照):
      slow: |1d| ≥ 3%  (duration_min = 1440)
      fast: |1h| ≥ 1.5% (duration_min = 60)  ← 同等幅度, 时间压缩 24 倍

    四道保护:
      · 窗口按**真实时间戳**算 (不按 bar 数) → 1m/5m/15m 各粒度口径一致;
      · 窗口跨度不足 80% 目标时跳过 (数据稀疏时不能用残缺窗口冒充);
      · **bar 连续性 ≥ 50%**: 窗口内实际 bar 数必须 ≥ 该跨度"应有"的一半。
        实测 WTI 的 1m 只有 6979 根却横跨 6 个月 (平均每 2~3 分钟才一根, 且
        大量空洞) → 不校验时它会算出 91 条"1 小时涨 16%"的假 fast 异动,
        实际是跨天的涨跌幅。宁可漏, 不可伪造;
      · |pct| > 90% 判为量纲跳变 (数据污染) → 不产出 move。
    去重: 同资产内时间上重叠的窗口只保留幅度最大的那条 —— 否则 1m bar
          会把同一波行情重复记几十遍, 虚增 fast 数量。
    """
    if len(series) < 2 or bar_sec <= 0:
        return []
    win_sec = window_min * 60
    moves: list[dict] = []
    n = len(series)
    j = 0
    for i in range(n):
        ts_i, p_i = series[i]
        if p_i <= 0:
            continue
        target = ts_i - win_sec
        # 双指针: 找到时间上最接近 target 的下标 (且在 i 之前)
        while j + 1 < i and abs(series[j + 1][0] - target) <= abs(series[j][0] - target):
            j += 1
        if j >= i:
            continue
        ts_j, p_j = series[j]
        if p_j <= 0:
            continue
        span = ts_i - ts_j
        if span < win_sec * 0.8:
            continue
        # bar 连续性校验: 该跨度内应有的 bar 数 vs 实际 bar 数
        expected = span / float(bar_sec) + 1.0
        got = i - j + 1
        if got < expected * 0.5:
            continue
        pct = (p_i - p_j) / p_j * 100.0
        if abs(pct) > MAX_PLAUSIBLE_PCT:
            continue
        if abs(pct) < thresh:
            continue
        kind = "fast_rise" if pct > 0 else "fast_fall"
        moves.append({
            "ts": ts_i,
            "start_price": round(p_j, 6),
            "peak_price": round(max(p_j, p_i), 6),
            "end_price": round(p_i, 6),
            "move_pct": round(pct, 4),
            "move_kind": kind,
            "duration_min": window_min,
            "algo_score": round(min(0.95, BASE_SCORE[kind] + min(0.25, abs(pct) / 40.0)), 3),
            "context_json": json.dumps({
                "source": "ohlcv_intraday", "mode": "backfill",
                "window_min": window_min, "threshold_pct": thresh,
                "span_sec": span, "start_ts": ts_j,
                "bar_sec": bar_sec, "bars_in_window": got,
                "expected_bars": round(expected, 1),
                "continuity": round(got / expected, 3) if expected else 0.0,
            }, ensure_ascii=False),
        })
    # 去重: 重叠窗口只留幅度最大的
    moves.sort(key=lambda m: (m["ts"], -abs(m["move_pct"])))
    kept: list[dict] = []
    for m in moves:
        if kept and m["ts"] - kept[-1]["ts"] < win_sec:
            if abs(m["move_pct"]) > abs(kept[-1]["move_pct"]):
                kept[-1] = m
            continue
        kept.append(m)
    return kept


def compute_thresh_scale(prices: list[tuple[int, float]], window: int = 250) -> float:
    """v340: 按品种自身波动率标定临界点阈值。

    全局 3% 的问题是各品种差 20 倍：BTC 有 22.7% 的交易日 |1d|>=3%（等于天天报警），
    XAU 只有 1.6%（两个月才响一次）。同一个数字不可能同时是"异常"。

    做法：取最近 `window` 根日线的 |涨跌幅| 的 p95 作为该品种的"典型大波动"，
    除以全局基准 3% 得到缩放因子，夹在 [0.67, 3.5]：
        ETH p95 8.46% → 2.82 倍 → 1d 阈值 8.5%
        XAU p95 2.01% → 0.60 倍 → 1d 阈值 2.0%（下限）
        BTC p95 6.45% → 2.15 倍 → 6.5%
    3d/5d 阈值同比例缩放，保持"1d/3d/5d 相对关系"不变。
    """
    closes = [p for _, p in prices[-window - 1:] if p > 0]
    if len(closes) < 30:
        return 1.0
    rets = [abs((closes[i] - closes[i - 1]) / closes[i - 1] * 100.0)
            for i in range(1, len(closes))]
    rets.sort()
    p95 = rets[min(len(rets) - 1, int(len(rets) * 0.95))]
    # 下限 0.67 ≈ 2%（与 XAU/WTI 这类低波动品种的 p95 对齐）
    return max(0.67, min(3.5, p95 / THRESH_1D_PCT))


def compute_thresh_scale_series(
    prices: list[tuple[int, float]],
    window: int = 120,
    min_bars: int = 30,
    floor: float = 0.67,
    cap: float = 3.5,
) -> list[float]:
    """v341: 逐点波动率自适应阈值（滚动，不看未来）。

    v340 的 `compute_thresh_scale` 只算**一个**标量：用最近 250 日定一个数，
    然后套在那品种**全部历史**上。两个毛病：
      1) 2020 年 BTC 日波动 8%，2025 年只有 3% —— 拿今天的标尺量四年前，
         平静期全是假异动、剧烈期又全漏；
      2) 它读的是整段序列里"最新的"250 日 —— 用来判 2020 年那根 K 线时，
         事实上用了 2024 年的数据（前视偏差 lookahead）。

    这里改成逐根 K 线各自算：第 i 根的阈值只由 **[i-window, i]** 这一段决定。
    - 只往后看 → 无前视，回测口径与实盘一致
    - 历史不足 min_bars 时沿用上一个已知值（序列开头用 1.0）
    - 仍夹在 [floor, cap]，避免极端行情把阈值推到不可用

    返回与 prices 等长的 list[float]，第 i 项是该点的 scale。
    """
    n = len(prices)
    out: list[float] = [1.0] * n
    prev = 1.0
    for i in range(n):
        lo = max(0, i - window)
        closes = [p for _, p in prices[lo : i + 1] if p > 0]
        if len(closes) < min_bars + 1:
            out[i] = prev
            continue
        rets = sorted(
            abs((closes[j] - closes[j - 1]) / closes[j - 1] * 100.0)
            for j in range(1, len(closes))
        )
        p95 = rets[min(len(rets) - 1, int(len(rets) * 0.95))]
        prev = max(floor, min(cap, p95 / THRESH_1D_PCT))
        out[i] = prev
    return out


def detect_moves(
    prices: list[tuple[int, float]],
    scale: float = 1.0,
    scales: list[float] | None = None,
    thresh_1d: float | None = None,
    asset: str = "",
    min_end_ts: int | None = None,
) -> list[dict]:
    """thresh_1d: 品种级 1 日阈值（来自 asset_threshold_policy）。
    只收紧不放松：最终阈值 = max(政策阈值, 3% × 波动率自适应)，避免双重放大。

    min_end_ts: 窗口结束时间早于该时点的异动不记录（v342）。
      原来每个起点按 1d→3d→5d 试、第一个命中就 break，于是：
      重置后 11-13 这个起点先命中 1d（结束 11-14，早于纪元被过滤），
      同一启点的 3d/5d（结束 11-16，跨进纪元）根本没生成 → slow 恒为 0。
      现在命中但结束早于纪元的，继续试更长的 k。
    """
    """对单个 asset 的价格序列,检测出所有 critical moves

    v308 P1-fix3: 增加量纲跳变保护。
      即使 fetch_prices 选对了 indicator, 序列里仍可能存在个别
      量纲错误的点 (例如 BTC 序列里混入 102.2 这种明显不是 BTC 价的值)。
      一旦被当成"价格", 就会算出 move_pct=-99.88% 这种假异动。
      这里加一道硬闸: |pct| > 90% 的日级涨跌对任何资产都不成立 →
      判定为数据污染, 不产出 move。
    """
    moves = []
    n = len(prices)
    # v341: 90% 的"不可能涨跌"上限是按股票/指数定的。加密一天翻倍是常态
    # （DOGE 2021-01 单日 +128%、+323% 都是真实行情），按 90% 卡会把真行情
    # 全判成量纲污染丢掉。有拆股的市场仍用 90%，无拆股（加密）放宽到 500%。
    MAX_PLAUSIBLE_PCT = 90.0 if has_splits(asset) else 500.0
    for i in range(n):
        ts_i, p_i = prices[i]
        if p_i <= 0:
            continue
        # 计算 i+1d / i+3d / i+5d
        for k, base_thresh in [(1, THRESH_1D_PCT), (3, THRESH_3D_PCT), (5, THRESH_5D_PCT)]:
            # v341: 优先用逐点 scale（波动率自适应、无前视）；没有则退回标量
            s = scales[i] if scales is not None and i < len(scales) else scale
            thresh = base_thresh * s
            if thresh_1d:
                # 品种政策按同一比例放大到 3d/5d，取两者更严的一侧
                pol_thresh = thresh_1d * (base_thresh / THRESH_1D_PCT)
                thresh = max(thresh, pol_thresh)
            j = i + k
            if j >= n:
                continue
            ts_j, p_j = prices[j]
            if p_j <= 0:
                continue
            pct = (p_j - p_i) / p_i * 100.0
            if abs(pct) < thresh:
                continue
            # v342: 命中但窗口结束早于纪元 → 不记这条，继续试更长的 k，
            # 否则 1d 先命中就 break，跨纪元的 3d/5d 慢异动永远不产出。
            if min_end_ts and int(ts_j) < int(min_end_ts):
                continue
            # v308 P1-fix3: 量纲跳变 → 数据污染, 不是市场异动
            if abs(pct) > MAX_PLAUSIBLE_PCT:
                print(f"  [SKIP] implausible move {pct:.1f}% "
                      f"({p_i} → {p_j}) — 判定为量纲污染, 不产出异动")
                break
            # 区间极值
            window = [prices[m][1] for m in range(i, j + 1) if prices[m][1] > 0]
            if not window:
                continue
            peak = max(window) if pct > 0 else min(window)
            kind = "slow_rise" if pct > 0 else "slow_fall"
            duration_min = int((ts_j - ts_i) / 60)
            # score: 0.4 基础 + 幅度/10 封顶到 0.9
            score = min(0.9, BASE_SCORE[kind] + abs(pct) / 30.0)
            ctx = {
                "k_days": k,
                "window_high": max(window),
                "window_low": min(window),
                "n_window_pts": len(window),
                "source": "external_indicators:yfinance",
            }
            moves.append({
                "asset": None,  # 由调用者填
                "ts": ts_i,
                "start_price": p_i,
                "peak_price": peak,
                "end_price": p_j,
                "move_pct": round(pct, 3),
                "move_kind": kind,
                "duration_min": duration_min,
                "algo_score": round(score, 3),
                "context_json": json.dumps(ctx, ensure_ascii=False),
            })
            # 一个时间点只记最强信号(取 k 最大的,即窗口最长的)
            break
    return moves


def main() -> int:
    # v310 P0: 加 --dry-run —— 本脚本会先 DELETE 旧的 backfill 行再重插,
    # 阈值或逻辑有 bug 时会把现有数据搞坏。dry-run 只算不写。
    ap = argparse.ArgumentParser(description="回填 critical_price_moves (slow 日线 + fast intraday)")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印, 不写库")
    ap.add_argument("--since", default="",
                    help="只写入这个时点之后的异动 (YYYY-MM-DD 或 unix 秒)。"
                         "默认取 data_epoch —— 否则每小时重扫全历史, 清空的旧账会自己长回来")
    args = ap.parse_args()
    dry_run = args.dry_run

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    print(f"[INFO] Opening {DB_PATH}" + ("  (dry-run, 不写库)" if dry_run else ""))
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    # v308 P1-fix3: fetch_prices 用 r["indicator"] / r["ts"] / r["value"] 字典式访问,
    # 必须设 row_factory, 否则 sqlite3 默认返回 tuple → TypeError
    # ("tuple indices must be integers or slices, not str")。
    # 注意 bridge_server.db_cursor() 同样设了 row_factory, 两处保持一致。
    conn.row_factory = sqlite3.Row
    # 桥接进程可能同时在读/写同一张表 → 给写锁留等待窗口, 避免 "database is locked"
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # v291+ 也兼容 ohlcv_daily 表 (P0-1 后 ohlcv_daily 品种覆盖更全)
        ohlcv_assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily ORDER BY asset"
        ).fetchall()]
        # v310 P0: fast 异动的资产来自 ohlcv_intraday (可能与日线资产集合不同,
        # 例如 SKHYNIX / XAG 只有 intraday 数据) → 必须并进来, 否则这些品种的
        # 快涨快跌永远检测不到。
        intraday_assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_intraday ORDER BY asset"
        ).fetchall()]
        print(f"[INFO] ohlcv_daily {len(ohlcv_assets)} 个品种 / "
              f"ohlcv_intraday {len(intraday_assets)} 个品种（慢异动按序列排版，"
              f"快异动按 intraday 品种）")

        # 清空旧 backfill(保留手动写的)
        # v340: 删之前先把 (asset, ts, move_kind) → id 记下来，重新插入时复用。
        # 为什么必须复用: 这个作业每小时跑一次，旧实现是"整表删 backfill 行 + 重插"，
        # 每次都换一批新 id；而归因表按 move_id 关联、AI 复核队列按 src_id 认领，
        # 于是每跑一次就有大批归因行和队列条目判死（实测队列 168 条里 135 条 gone）。
        _old_ids: dict[tuple, int] = {}
        _id_pool: list[int] = []
        if dry_run:
            print("[INFO] dry-run: 跳过清理旧 backfill 行")
        else:
            try:
                _rows = conn.execute(
                    "SELECT id, asset, ts, move_kind FROM critical_price_moves "
                    "WHERE context_json LIKE '%backfill%' OR context_json LIKE '%external_indicators%' "
                    "OR context_json LIKE '%ohlcv_daily%'"
                ).fetchall()
                for _id, _a, _t, _k in _rows:
                    _key = (str(_a), int(_t or 0), str(_k or ""))
                    _old_ids.setdefault(_key, int(_id))
                    _id_pool.append(int(_id))
            except sqlite3.OperationalError:
                _old_ids, _id_pool = {}, []
            cur_del = conn.execute("DELETE FROM critical_price_moves WHERE context_json LIKE '%backfill%' OR context_json LIKE '%external_indicators%' OR context_json LIKE '%ohlcv_daily%'")
            print(f"[INFO] Cleared {cur_del.rowcount} prior backfill rows "
                  f"(记住了 {len(_old_ids)} 个 id 用于复用)")

        # v340: 纪元闸门 —— 只产出 since 之后的新异动
        since_ts = 0
        if args.since:
            raw = args.since.strip()
            if raw.isdigit():
                since_ts = int(raw)
            else:
                since_ts = int(datetime.strptime(raw, "%Y-%m-%d").timestamp())
        else:
            since_ts = int(epoch_gate.epoch_ts(conn) or 0)
        since_ts = int(epoch_gate.clamp_since(since_ts, epoch_gate.epoch_ts(conn)) or 0)
        if since_ts:
            print(f"[INFO] 纪元闸门: 只写入 ts >= {datetime.fromtimestamp(since_ts):%Y-%m-%d %H:%M} 的异动")

        all_moves: list[dict] = []
        # v341: 按“序列”遍历，而不是按 asset。asset 里混着抽屉（'*'/'fred'/'FX'），
        # 同一个序列会被数好几遍，聚合量会被当成品种。plan 里每条序列只有一个来源。
        plan = series_plan.build_plan(conn)
        series_plan.report(plan)
        for item in plan:
            label = item["series"]
            if item["source_asset"] is None:
                prices = _ohlcv_prices(conn, label)
            else:
                prices = fetch_prices(conn, item["source_asset"],
                                      prefer_indicator=item["indicator"])
            if len(prices) < 6:
                print(f"  [SKIP] {label}: only {len(prices)} pts")
                continue
            scales = compute_thresh_scale_series(prices)
            # 品种级阈值政策（来自回测矩阵）：只收紧、不放松，可用 TRIDENT_THRESH_POLICY=off 关掉
            pol = None
            try:
                import asset_threshold_policy as _atp
                pol = _atp.policy(label)
            except Exception as e:
                print(f"  [WARN] 阈值政策不可用({type(e).__name__})，按自适应阈值跑")
            moves = detect_moves(prices, scales=scales,
                                 thresh_1d=(pol or {}).get("thresh"), asset=label,
                                 min_end_ts=since_ts or None)
            scale = compute_thresh_scale(prices)  # 仅用于摘要打印
            for m in moves:
                m["asset"] = label
            all_moves.extend(moves)
            _s = sorted(scales)
            lo_s, hi_s = _s[len(_s) // 10], _s[len(_s) * 9 // 10]
            print(f"  [OK]   {label}: {len(prices)} pts -> {len(moves)} slow moves"
                  f" (自适应阈值 {THRESH_1D_PCT * lo_s:.1f}%~{THRESH_1D_PCT * hi_s:.1f}%, "
                  f"静态口径 ×{scale:.2f} → {THRESH_1D_PCT * scale:.1f}%)")

        # v310 P0: fast 异动 (真实 intraday 数据)
        fast_n = 0
        for asset in intraday_assets:
            series, bar_sec = fetch_intraday_series(conn, asset)
            if len(series) < 60:
                print(f"  [SKIP-FAST] {asset}: only {len(series)} bars")
                continue
            fmoves = detect_fast_moves(series, bar_sec=bar_sec)
            for m in fmoves:
                m["asset"] = asset
            all_moves.extend(fmoves)
            fast_n += len(fmoves)
            print(f"  [FAST] {asset}: {len(series)} bars -> {len(fmoves)} fast moves")

        if since_ts:
            # v341: 慢异动的 ts 锚在**窗口起点**，只看起点会把"跨纪元"的 3d/5d 慢涨整条丢掉
            # （实测纪元后才 0.4 天，slow 直接 =0，验收第 7 项假失败）。
            # 改成看**终点**：窗口结束时间 >= 纪元才要；起点早于纪元的把 ts 抬到纪元，
            # 这样既不丢慢异动，也不让"纪元前残留"复燃（duration_min 保留真实值）。
            def _keep(m) -> bool:
                _ts = int(m.get("ts") or 0)
                _end = _ts + int(m.get("duration_min") or 0) * 60
                return _end >= since_ts
            kept = []
            for m in all_moves:
                if not _keep(m):
                    continue
                _ts = int(m.get("ts") or 0)
                if _ts < since_ts:
                    m["ts"] = since_ts
                kept.append(m)
            print(f"[INFO] 纪元闸门过滤: {len(all_moves)} → {len(kept)} 条 (剔除历史 {len(all_moves)-len(kept)} 条)")
            all_moves = kept
        kept_fast = sum(1 for m in all_moves if str(m.get("move_kind", "")).startswith("fast"))
        print(f"[INFO] Total moves: {len(all_moves)} (fast={kept_fast}, slow={len(all_moves) - kept_fast})")
        if dry_run:
            by_kind: dict[str, int] = {}
            for m in all_moves:
                by_kind[m["move_kind"]] = by_kind.get(m["move_kind"], 0) + 1
            print("[INFO] dry-run 分类:", by_kind)
            print("[INFO] dry-run: 未写库")
            return 0

        # 批量插入 —— id 优先复用同 (asset, ts, move_kind) 的旧 id，没有才新分配。
        # 新 id 从 max(现有 id, 旧 id 全部) 之后往后排，避免和仍在库里的行撞主键。
        # v341: 同一 (asset, ts, kind) 可能已经有**别的来源**写过一行
        # （market_ticks 实时快涨 vs 我们从 ohlcv_intraday 回填），
        # 清理时只删自己写的行，于是两边各留一行 → 重复计权重。
        # 这里把"别人已经占住的 (asset, ts, kind)"收集起来，直接跳过不插入。
        _occupied: set[tuple] = set()
        try:
            for _a, _t, _k, _ctx in conn.execute(
                    "SELECT asset, ts, move_kind, context_json FROM critical_price_moves"):
                _d = str(_ctx or "")
                if "backfill" in _d or "ohlcv_intraday" in _d or "external_indicators" in _d:
                    continue
                _occupied.add((str(_a), int(_t or 0), str(_k or "")))
        except sqlite3.OperationalError:
            _occupied = set()
        _used = set(_id_pool)
        try:
            _base = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM critical_price_moves").fetchone()[0] or 0)
        except sqlite3.OperationalError:
            _base = 0
        if _id_pool:
            _base = max(_base, max(_id_pool))
        _next = _base
        _dedup: dict[tuple, int] = {}
        _out: list[tuple] = []
        for m in all_moves:
            _key = (str(m["asset"]), int(m.get("ts") or 0), str(m.get("move_kind") or ""))
            if _key in _occupied:
                continue          # 别的来源已经记过这条异动
            if _key in _dedup:
                # 同一 (品种, 时间, 类型) 出现两次（慢涨 + 快涨窗口重叠）→ 合并成一行，
                # 否则复用同一个 id 会撞主键。
                _i = _dedup[_key]
                if abs(float(m.get("move_pct") or 0)) > abs(float(_out[_i][5] or 0)):
                    _out[_i] = (_out[_i][0], m["asset"], m["ts"], m["start_price"],
                                m["peak_price"], m["end_price"], m["move_pct"], m["move_kind"],
                                m["duration_min"], m["algo_score"], m["context_json"])
                continue
            _mid = _old_ids.get(_key)
            if _mid is None:
                _next += 1
                _mid = _next
            _dedup[_key] = len(_out)
            _out.append((_mid, m["asset"], m["ts"], m["start_price"], m["peak_price"],
                         m["end_price"], m["move_pct"], m["move_kind"], m["duration_min"],
                         m["algo_score"], m["context_json"]))
        conn.executemany(
            "INSERT INTO critical_price_moves "
            "(id,asset,ts,start_price,peak_price,end_price,move_pct,move_kind,duration_min,algo_score,context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            _out,
        )
        conn.commit()
        print(f"[INFO] 写入 {len(_out)} 行 (复用旧 id {sum(1 for r in _out if r[0] in _used)} 行)")

        # 验证
        cnt = conn.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
        by_kind = conn.execute(
            "SELECT move_kind, COUNT(*) FROM critical_price_moves GROUP BY move_kind"
        ).fetchall()
        by_asset = conn.execute(
            "SELECT asset, COUNT(*) FROM critical_price_moves GROUP BY asset ORDER BY asset"
        ).fetchall()
        print(f"[OK] critical_price_moves total rows: {cnt}")
        for k, c in by_kind:
            print(f"  {k}: {c}")
        for a, c in by_asset:
            print(f"  {a}: {c}")

        # v341: 注册表跟着这轮数据一起重建 —— 它原来是我手动跑的，
        # 新指标进来不会自动更新，下游读到的是过期分类。
        try:
            import build_series_registry as _reg
            n_reg, added = _reg.refresh(conn)
            fr = _reg.freshness(conn)
            print(f"[REGISTRY] {n_reg} 条(新增 {added}) / 最新指标落后 "
                  f"{fr['stale_hours']}h / 未登记组合 {fr['missing']}")
            if fr["missing"]:
                print(f"[REGISTRY-WARN] 有 {fr['missing']} 个指标组合没进注册表")
            if fr["stale_hours"] is not None and fr["stale_hours"] > 24:
                print(f"[REGISTRY-WARN] 指标数据已 {fr['stale_hours']}h 没更新"
                      f"（数据源可能停了，不是分类问题）")
        except Exception as e:
            print(f"[REGISTRY-WARN] 注册表重建失败: {type(e).__name__}: {e}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())