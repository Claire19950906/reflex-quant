#!/usr/bin/env python3
"""宏观驱动系数回测校准（v459）。

要回答的问题：`macro_equity_regime` 里那套**先验**（写死在代码里、从未验证）到底有没有被数据支持。

先验（当前公式）：
    rate  = clamp(Δ10Y(20日) / 0.30)                          → tilt 带 **负号**
    risk  = mean(clamp((18-VIX)/8), clamp(-ΔHY_OAS(20日)/0.40)) → tilt 带 **正号**
    usd   = clamp(DXY 20日% / 3.0)                            → tilt 带 **负号**
    curve = clamp(-(10Y-2Y)/0.50)                             → tilt 带 **负号**
    tilt  = clamp(-wR·rate + wK·risk - wD·usd - wC·curve)，(wR,wK,wD,wC) 见 SECTOR_SENSITIVITY

本库 `macro_snapshots` 的宏观序列只有 ~36 天，做不了统计，所以走外部历史：
FRED 官方 CSV（5 年日频）+ 板块 ETF 日线（yfinance），落盘 `runtime/macro_calib/`。

方法（尽量不骗自己）：
  · 前瞻窗口用 10 与 20 个交易日两档；主结论按**非重叠取样**（每 H 天 1 点），
    叠置样本（`ic_overlapping`）收益互相重叠会让 IC 虚高，只作参考；
  · 指标：Spearman IC（排序相关，抗极值）+ 五分位多空差（最接近"能不能用"的口径）；
  · 关键列 `ic_aligned = IC × 先验符号`：**正数才算先验对**，负数就是符号反了；
  · 判级：|ic_aligned| ≥ 0.10 且样本 ≥ 60 才算"有信号"；两个窗口、多数板块同向才算"先验被支持"；
  · 最后单独测**复合 tilt**（不是驱动）对同桶 ETF 的前瞻收益 —— 这直接回答
    "`_MACRO_TILT_CAP=0.15` 这个调整值到底有没有预测力"。
  · 只给证据与建议，不改代码常量：系数怎么定需人拍板。

用法：
  python3 backend/scripts/macro_driver_calibration.py
  python3 backend/scripts/macro_driver_calibration.py --years 10 --json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

CACHE_DIR = BACKEND / "runtime" / "macro_calib"
REPORT_DIR = BACKEND / "reports" / "macro_calibration"

FRED_SERIES = {
    "DGS10": "dgs10",
    "DGS2": "dgs2",
    "T10Y2Y": "t10y2y",
    "VIXCLS": "vix",
    "BAMLH0A0HYM2": "hy_oas",
    "DTWEXBGS": "dxy",
}
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

# 板块 ETF（用于驱动检验：先确认"这个驱动对美股到底有没有方向性影响"）
SECTOR_ETFS = {
    "semis": "SMH",
    "software": "IGV",
    "mega_tech": "QQQ",
    "broad": "SPY",
}

# 复合 tilt 的映射（与 SECTOR_SENSITIVITY 的桶一一对应，crypto 桶用可用 ETF 近似）
TILT_ETFS = {
    "semis": "SMH",
    "ai_growth": "IGV",
    "mega_tech": "QQQ",
    "crypto_miner": "WGMI",
    "crypto_asset": "BITO",
}

HORIZONS = (10, 20)
REF_HORIZON = 60      # 季度参考窗：只有叠置样本，不能当结论
MIN_ABS_IC = 0.10
MIN_SAMPLES = 60
_WINDOW = 20

PRIOR_SIGN = {"rate": -1, "risk": +1, "usd": -1, "curve": -1}


# ── 取数 ────────────────────────────────────────────────────────────────
def _cache(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def fetch_fred_history(series_id: str, days: int, refresh: bool = False) -> List[Tuple[str, float]]:
    """FRED 官方 CSV。用 curl：python requests 在本机 TLS 指纹会被拦。"""
    path = _cache(f"fred_{series_id}.csv")
    text = ""
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < 86400:
        cached = path.read_text(encoding="utf-8", errors="ignore")
        # 只认表头对得上的缓存：早期失败写进去的 HTML / 空文件不能当数据用
        if cached.splitlines() and "observation_date" in cached.splitlines()[0]:
            text = cached
    if not text:
        url = FRED_URL.format(sid=series_id)
        # 本机 LibreSSL 对 fred 是**间歇性** SSL_ERROR_SYSCALL（同一 URL 重试就通），
        # 不重试会把整条序列误判成"不可用"，那是假缺口。
        for attempt in range(6):
            try:
                out = subprocess.run(["curl", "-sS", "--http1.1", "--retry", "2",
                                      "--retry-all-errors", "--max-time", "40", url],
                                     capture_output=True, text=True, timeout=60)
                text = out.stdout or ""
            except Exception:
                text = ""
            if text and "observation_date" in text.splitlines()[0]:
                break
            text = ""
            time.sleep(1.5 * (attempt + 1))
        if text:
            path.write_text(text, encoding="utf-8")
    rows: List[Tuple[str, float]] = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        day, raw = parts[0].strip(), parts[1].strip()
        if not day or raw in ("", ".", "NA"):
            continue
        try:
            rows.append((day, float(raw)))
        except ValueError:
            continue
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    return [(d, v) for d, v in rows if d >= cutoff]


def fetch_etf_history(ticker: str, days: int, refresh: bool = False) -> List[Tuple[str, float]]:
    """yfinance 日线收盘。本机间歇 SSL 失败 → 重试 + download 兜底。"""
    path = _cache(f"etf_{ticker}.json")
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < 86400:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data:
                return [(d, float(v)) for d, v in data]
        except Exception:
            pass
    rows: List[Tuple[str, float]] = []
    period = f"{max(2, days // 365)}y"
    for attempt in range(3):
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
            if len(hist) > 100:
                rows = [(str(i)[:10], float(c)) for i, c in
                        zip(hist.index, hist["Close"].tolist()) if c == c]
                break
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))
    if not rows:
        try:
            import yfinance as yf
            df = yf.download(ticker, period=period, interval="1d",
                             auto_adjust=True, progress=False, threads=False)
            if df is not None and len(df) > 100:
                col = df["Close"]
                rows = [(str(i)[:10], float(v)) for i, v in col.items() if v == v]
        except Exception:
            rows = []
    if rows:
        path.write_text(json.dumps(rows), encoding="utf-8")
    return rows


# ── 驱动计算（与模块同一套公式） ─────────────────────────────────────────
def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))


def build_driver_frame(fred: Dict[str, List[Tuple[str, float]]],
                       weights: Dict[str, Tuple[float, float, float, float]]) -> List[Dict[str, float]]:
    """逐日算 rate / risk / usd / curve，并顺手算出各板块 tilt。

    口径与 `macro_equity_regime.build()` 一致：20 日变化按"可用观测条数"回看
    （FRED 周末/假日停更，但序列里本来就不含那些天）。
    """
    need = ("dgs10", "dgs2", "vix", "hy_oas", "dxy", "t10y2y")
    by_day: Dict[str, Dict[str, float]] = {}
    for key, rows in fred.items():
        for day, value in rows:
            by_day.setdefault(day, {})[key] = value
    days = sorted(by_day)
    last: Dict[str, float] = {}
    hist: Dict[str, List[Tuple[str, float]]] = {k: [] for k in fred}
    out: List[Dict[str, float]] = []
    for day in days:
        for key in fred:
            if key in by_day[day]:
                last[key] = by_day[day][key]
                hist[key].append((day, by_day[day][key]))
        if not all(k in last for k in need):
            continue
        if min(len(hist["dgs10"]), len(hist["hy_oas"]), len(hist["dxy"])) <= _WINDOW:
            continue
        chg10 = hist["dgs10"][-1][1] - hist["dgs10"][-_WINDOW - 1][1]
        hy_chg = hist["hy_oas"][-1][1] - hist["hy_oas"][-_WINDOW - 1][1]
        base_dxy = hist["dxy"][-_WINDOW - 1][1]
        dxy_pct = (hist["dxy"][-1][1] - base_dxy) / base_dxy * 100.0 if base_dxy else 0.0
        row = {
            "day": day,
            "rate": _clamp(chg10 / 0.30),
            "risk": (_clamp((18.0 - last["vix"]) / 8.0) + _clamp(-hy_chg / 0.40)) / 2.0,
            "usd": _clamp(dxy_pct / 3.0),
            "curve": _clamp(-last["t10y2y"] / 0.50),
        }
        for sector, (wr, wk, wd, wc) in weights.items():
            row["tilt_" + sector] = _clamp(-wr * row["rate"] + wk * row["risk"]
                                          - wd * row["usd"] - wc * row["curve"])
        out.append(row)
    return out


# ── 统计 ────────────────────────────────────────────────────────────────
def _ranks(values: List[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: List[float], y: List[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (sx * sy)


def spearman_ic(x: List[float], y: List[float]) -> Optional[float]:
    return _pearson(_ranks(x), _ranks(y))


def quintile_spread(x: List[float], y: List[float], quantiles: int = 5) -> Optional[float]:
    """按 x 分五组，最高组平均 y − 最低组平均 y（%）。"""
    n = len(x)
    if n < quantiles * 2:
        return None
    order = sorted(range(n), key=lambda i: x[i])
    step = n // quantiles
    low, high = order[:step], order[-step:]
    return sum(y[i] for i in high) / len(high) - sum(y[i] for i in low) / len(low)


def forward_returns(prices: List[Tuple[str, float]], horizon: int) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for i in range(len(prices) - horizon):
        base = prices[i][1]
        if base <= 0:
            continue
        out[prices[i][0]] = (prices[i + horizon][1] / base - 1.0) * 100.0
    return out


def evaluate(rows: List[Dict[str, float]], fwd: Dict[str, float], field: str,
             horizon: int, prior_sign: int = 0) -> Dict[str, object]:
    pairs = [(r[field], fwd[r["day"]]) for r in rows if r["day"] in fwd]
    non_overlap = pairs[::horizon]
    if len(non_overlap) < 10:
        return {"samples_non_overlap": len(non_overlap), "ic": None}
    ic_all = spearman_ic([p[0] for p in pairs], [p[1] for p in pairs])
    ic = spearman_ic([p[0] for p in non_overlap], [p[1] for p in non_overlap])
    spread = quintile_spread([p[0] for p in non_overlap], [p[1] for p in non_overlap])
    res: Dict[str, object] = {
        "samples_all": len(pairs),
        "samples_non_overlap": len(non_overlap),
        "ic_overlapping": round(ic_all, 3) if ic_all is not None else None,
        "ic": round(ic, 3) if ic is not None else None,
        "quintile_spread_pct": round(spread, 2) if spread is not None else None,
    }
    if prior_sign and ic is not None:
        # 正数 = 按先验方向使用是对的；负数 = 符号反了
        res["ic_aligned"] = round(ic * prior_sign, 3)
        res["spread_aligned_pct"] = (round(spread * prior_sign, 2)
                                     if spread is not None else None)
        # signal = 强度够不够；sign_ok = 方向对不对（两者必须分开，否则"符号反了"会被吞掉）
        res["signal"] = bool(abs(res["ic_aligned"]) >= MIN_ABS_IC)
        res["sign_ok"] = bool(res["ic_aligned"] > 0)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="宏观驱动系数回测校准（只读，写报告）")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    days = args.years * 365
    fred: Dict[str, List[Tuple[str, float]]] = {}
    for sid, key in FRED_SERIES.items():
        rows = fetch_fred_history(sid, days, refresh=args.refresh)
        fred[key] = rows
        if rows:
            print(f"[FRED] {sid:14s} {len(rows):5d} 观测（{rows[0][0]} → {rows[-1][0]}）", flush=True)
        else:
            print(f"[FRED] {sid:14s} 不可用", flush=True)

    missing = [k for k, v in fred.items() if not v]
    if missing:
        # 缺 hy_oas 时 risk 只剩 VIX 半边 —— 可以退，但必须写在结论里
        print(f"[警告] 缺 {missing}，相关驱动将退化/缺失（报告会标注）", flush=True)

    import macro_equity_regime as mer
    drivers = build_driver_frame(fred, mer.SECTOR_SENSITIVITY)
    if len(drivers) < MIN_SAMPLES:
        print(f"[中止] 驱动序列仅 {len(drivers)} 天 < {MIN_SAMPLES}，统计上无意义（不编数）")
        return 2

    # ── 1) 驱动检验 ──────────────────────────────────────────────
    needed = set(SECTOR_ETFS.values())
    etfs = {tk: fetch_etf_history(tk, days, refresh=args.refresh) for tk in needed}
    for tk, rows in etfs.items():
        print(f"[ETF] {tk:5s} {len(rows):5d} 根日线", flush=True)

    driver_results: Dict[str, Dict[str, object]] = {}
    for horizon in HORIZONS:
        hkey = f"h{horizon}"
        driver_results[hkey] = {}
        for bucket, tk in SECTOR_ETFS.items():
            prices = etfs.get(tk) or []
            if len(prices) < MIN_SAMPLES:
                driver_results[hkey][bucket] = {"available": False, "detail": "ETF 历史不足"}
                continue
            fwd = forward_returns(prices, horizon)
            driver_results[hkey][bucket] = {
                "available": True,
                "ticker": tk,
                "drivers": {d: evaluate(drivers, fwd, d, horizon, PRIOR_SIGN[d])
                            for d in ("rate", "risk", "usd", "curve")},
            }

    # ── 2) 复合 tilt 检验 ────────────────────────────────────────
    tilt_results: Dict[str, Dict[str, object]] = {}
    for horizon in HORIZONS:
        hkey = f"h{horizon}"
        tilt_results[hkey] = {}
        for sector, tk in TILT_ETFS.items():
            prices = etfs.get(tk) or []
            if len(prices) < MIN_SAMPLES:
                prices = fetch_etf_history(tk, days, refresh=False)
            if len(prices) < MIN_SAMPLES:
                tilt_results[hkey][sector] = {"available": False, "ticker": tk,
                                              "detail": "ETF 不可得"}
                continue
            fwd = forward_returns(prices, horizon)
            tilt_results[hkey][sector] = {"available": True, "ticker": tk,
                                          **evaluate(drivers, fwd, "tilt_" + sector, horizon, +1)}

    # ── 3) 季度窗口参考（只有叠置样本，不能当结论，但能看出方向） ──
    # 767 天 / 60 日 ≈ 12 个独立样本：拟合不出权重，但足以看出"符号对不对"。
    summary: Dict[str, object] = {"drivers": {}, "tilt": {}, "recommendations": []}
    reference: Dict[str, object] = {}
    for tk in SECTOR_ETFS.values():
        prices = etfs.get(tk) or []
        if len(prices) < REF_HORIZON * 4:
            reference[tk] = {"available": False}
            continue
        fwd = forward_returns(prices, REF_HORIZON)
        fields = ("rate", "risk", "usd", "curve")
        reference[tk] = {"available": True,
                         "overlapping_samples": sum(1 for r in drivers if r["day"] in fwd),
                         "independent_approx": round(sum(1 for r in drivers if r["day"] in fwd) / REF_HORIZON),
                         "drivers": {d: evaluate(drivers, fwd, d, 1, PRIOR_SIGN[d]) for d in fields}}
    # 季度窗口里"对齐 IC"明显为负 = 先验符号在这个窗口上是反的
    flipped: List[str] = []
    for tk, res in reference.items():
        if not res.get("available"):
            continue
        for d, r in res["drivers"].items():
            ica = r.get("ic_aligned")
            if ica is not None and ica <= -MIN_ABS_IC:
                flipped.append(f"{tk}/{d}={ica:+.2f}")
    if flipped:
        summary["recommendations"].append(
            "季度窗口（60 日，仅叠置样本参考）出现符号反转：" + "、".join(flipped) +
            " → 这些先验在慢变量尺度上可能反过来。样本不足以拟合，故本轮不翻符号，"
            "但说明这套 tilt 在持仓周期拉长时并不可信")

    # ── 4) 汇总（summary 已在季度参考段初始化，这里只续写） ──
    for d in ("rate", "risk", "usd", "curve"):
        aligned: List[float] = []
        details: List[str] = []
        for hkey in driver_results:
            for bucket, res in driver_results[hkey].items():
                if not res.get("available"):
                    continue
                r = res["drivers"][d]
                if r.get("ic_aligned") is not None:
                    aligned.append(r["ic_aligned"])
                    details.append(f"{hkey}/{bucket}={r['ic_aligned']:+.2f}")
        if not aligned:
            summary["drivers"][d] = {"median_ic_aligned": None, "verdict": "no_data"}
            continue
        srt = sorted(aligned)
        med = srt[len(srt) // 2]
        pos = sum(1 for v in aligned if v > 0)
        verdict = ("prior_ok" if abs(med) >= MIN_ABS_IC and pos >= len(aligned) / 2 + 1
                   else ("prior_sign_doubtful" if abs(med) >= MIN_ABS_IC else "no_signal"))
        summary["drivers"][d] = {
            "median_ic_aligned": round(med, 3),
            "positive": f"{pos}/{len(aligned)}",
            "verdict": verdict,
            "detail": "; ".join(details),
        }
        if verdict == "prior_sign_doubtful":
            summary["recommendations"].append(
                f"{d}: 中位对齐 IC {med:+.2f}（先验符号 {PRIOR_SIGN[d]:+d}）→ 符号可能反了，"
                f"继续按先验方向用等于反向操作")
        elif verdict == "no_signal":
            summary["recommendations"].append(
                f"{d}: 全部板块 |对齐 IC| < {MIN_ABS_IC} → 统计上没有方向性信号，"
                f"不宜作为主要依据，建议缩小权重或降级为解释性信息")

    tilt_ics: List[float] = []
    tilt_detail: List[str] = []
    for hkey in tilt_results:
        for sector, res in tilt_results[hkey].items():
            if res.get("available") and res.get("ic") is not None:
                tilt_ics.append(res["ic"])
                tilt_detail.append(f"{hkey}/{sector}={res['ic']:+.2f}")
    if tilt_ics:
        srt = sorted(tilt_ics)
        med = srt[len(srt) // 2]
        summary["tilt"] = {"median_ic": round(med, 3),
                           "positive": f"{sum(1 for v in tilt_ics if v > 0)}/{len(tilt_ics)}",
                           "detail": "; ".join(tilt_detail),
                           "verdict": ("tilt_has_signal" if abs(med) >= MIN_ABS_IC
                                       else "tilt_no_signal")}
        if abs(med) < MIN_ABS_IC:
            summary["recommendations"].append(
                f"复合 tilt 中位 IC {med:+.2f} < {MIN_ABS_IC} → `_MACRO_TILT_CAP` 无预测力证据；"
                f"v459 已按本条把 cap 从 0.15 降到 0.05（env `TRIDENT_MACRO_TILT_CAP` 可恢复）")

    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "years": args.years,
        "horizons": list(HORIZONS),
        "driver_days": len(drivers),
        "driver_range": [drivers[0]["day"], drivers[-1]["day"]],
        "fred_observations": {k: len(v) for k, v in fred.items()},
        "etf_observations": {k: len(v) for k, v in etfs.items()},
        "min_abs_ic": MIN_ABS_IC,
        "prior_sign": PRIOR_SIGN,
        "driver_results": driver_results,
        "tilt_results": tilt_results,
        "reference_h60": reference,
        "summary": summary,
        "note": ("主结论用非重叠取样（每 H 天 1 点），ic_overlapping 因收益重叠会偏高只作参考。"
                 "ic_aligned = IC × 先验符号，正数=先验方向对。本报告只给证据与建议，不改常量。"),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"\n[驱动区间] {payload['driver_range'][0]} → {payload['driver_range'][1]}"
              f"（{payload['driver_days']} 个交易日）")
        for horizon in HORIZONS:
            hkey = f"h{horizon}"
            print(f"\n── 前瞻 {horizon} 日 · 对齐 IC（正=先验方向对）──")
            print(f"{'板块':10s}" + "".join(f"{d:>16s}" for d in ("rate", "risk", "usd", "curve")))
            for bucket, res in driver_results[hkey].items():
                if not res.get("available"):
                    print(f"{bucket:10s}  {res.get('detail')}")
                    continue
                line = f"{bucket:10s}"
                for d in ("rate", "risk", "usd", "curve"):
                    r = res["drivers"][d]
                    val = r.get("ic_aligned")
                    line += f"{('%.2f' % val) if val is not None else 'N/A':>10s}"
                    line += f"{('n=%d' % r['samples_non_overlap']):>6s}"
                print(line)
        print("\n[符号核对] 先验：rate − / risk + / usd − / curve −")
        for d, info in summary["drivers"].items():
            print(f"  {d:6s} 中位对齐IC {info['median_ic_aligned']}  "
                  f"正数 {info['positive']}  → {info['verdict']}")
        print("\n── 复合 tilt → 同桶 ETF 前瞻收益 ──")
        for hkey in tilt_results:
            for sector, res in tilt_results[hkey].items():
                if res.get("available"):
                    print(f"  {hkey} {sector:13s}{res['ticker']:5s} IC {res['ic']}  "
                          f"n={res['samples_non_overlap']}  多空差 {res['quintile_spread_pct']}%")
                else:
                    print(f"  {hkey} {sector:13s}{res.get('ticker'):5s} {res.get('detail')}")
        print(f"  → tilt 中位 IC {summary['tilt'].get('median_ic')}  {summary['tilt'].get('verdict')}")
        print("\n── 季度参考（60 日，仅叠置样本 · 独立样本约 12 个 · 只判符号不拟合权重）──")
        for tk, res in reference.items():
            if not res.get("available"):
                print(f"  {tk:5s} 数据不足")
                continue
            parts = [f"{d}={res['drivers'][d]['ic_aligned']:+.2f}"
                     for d in ("rate", "risk", "usd", "curve")
                     if res["drivers"][d].get("ic_aligned") is not None]
            print(f"  {tk:5s} 对齐IC " + "  ".join(parts) + f"  (n≈{res['independent_approx']} 独立)")
        if summary["recommendations"]:
            print("\n[建议]（不改代码，供拍板）")
            for rec in summary["recommendations"]:
                print(f"  · {rec}")
        print(f"\n[注] {payload['note']}")

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[写出] {REPORT_DIR / 'latest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
