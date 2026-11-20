#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_vol.py —— 加密期权波动率：ATM IV、25Δ 偏度、期限结构

为什么加：
  加密原先只有 DVOL（一条全市场综合波动率指数），看不出"偏度"和"期限结构"。
  币安欧式期权（eapi）直接给每个行权价的 markIV 与 delta，公开免费、不用 key，
  所以能自己算三条真正有交易含义的量：
    1) crypto_atm_iv     最近有流动性的到期日、平值附近的隐含波动率
    2) crypto_25d_skew   25Δ 看跌 IV 减 25Δ 看涨 IV（正=市场更怕跌）
    3) crypto_term_slope 次近到期 IV 减 近月 IV（正=远期更贵，通常伴随事件溢价）

口径（写清楚，免得被当成精确 greeks）：
  - delta 直接用交易所返回的值，不自己重算 Black-76；
  - "25Δ"取 |delta| 最接近 0.25 的那一个合约，命中偏差会写进 meta；
  - 近月只在剩余天数 ≥ 7 天里挑，太近的合约 IV 会被到期效应扭曲；
  - 每个标的必须有足够合约（>20 张）才算，否则跳过不写。

用法：
  python3 crypto_vol.py
  python3 crypto_vol.py --dry-run
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import external_data

MARK_URL = "https://eapi.binance.com/eapi/v1/mark"
SPOT_URL = "https://api.binance.com/api/v3/ticker/price?symbol={sym}"
SOURCE = "binance_options"
# crypto 用币安现货做 ATM 参照；贵金属期权只有一个月度到期、合约也少，
# 所以单独放宽"最少剩余天数"和"最少合约数"，否则永远算不出来。
TARGETS = {
    "BTC": {"spot": "binance:BTCUSDT", "prefix": "crypto", "min_days": 7, "min_contracts": 20},
    "ETH": {"spot": "binance:ETHUSDT", "prefix": "crypto", "min_days": 7, "min_contracts": 20},
    "XAU": {"spot": "kline:XAU", "prefix": "xau", "min_days": 2, "min_contracts": 10},
    "XAG": {"spot": "kline:XAG", "prefix": "xag", "min_days": 2, "min_contracts": 10},
}


def curl_json(url: str, tries: int = 4) -> Optional[object]:
    cmd = ["curl", "-s", "-m", "30", "--noproxy", "*", "--compressed",
           "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", url]
    for i in range(tries):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if out.stdout.strip():
                return json.loads(out.stdout)
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def parse_symbol(sym: str) -> Optional[Tuple[str, datetime, float, str]]:
    """"BTC-261225-85000-C" -> (标的, 到期日, 行权价, C/P)"""
    parts = sym.split("-")
    if len(parts) != 4:
        return None
    base, ymd, strike, typ = parts
    try:
        expiry = datetime.strptime(ymd, "%y%m%d")
        k = float(strike)
    except ValueError:
        return None
    return base, expiry, k, typ


def pick_near(items: List[Tuple[float, float]], target: float) -> Optional[Tuple[float, float]]:
    """在 [(delta, iv)] 里挑 |delta| 最接近 target 的一个"""
    if not items:
        return None
    return min(items, key=lambda t: abs(abs(t[0]) - target))


def analyse(base: str, rows: List[dict], spot: float,
            min_days: int = 7, min_contracts: int = 20) -> Optional[Dict[str, float]]:
    by_exp: Dict[datetime, List[Tuple[float, float, str, float]]] = {}
    for r in rows:
        p = parse_symbol(str(r.get("symbol", "")))
        if not p or p[0] != base:
            continue
        _, expiry, strike, typ = p
        iv = r.get("markIV")
        delta = r.get("delta")
        if iv in (None, "") or delta in (None, ""):
            continue
        by_exp.setdefault(expiry, []).append((strike, float(iv), typ, float(delta)))

    now = datetime.now()
    exps = sorted(e for e in by_exp
                  if (e - now).days >= min_days and len(by_exp[e]) >= min_contracts)
    if not exps:
        return None

    res: Dict[str, float] = {}
    near, far = exps[0], (exps[1] if len(exps) > 1 else None)

    # --- ATM IV：近月里行权价最贴现货的看涨/看跌 IV 取平均
    near_rows = by_exp[near]
    atm = min(near_rows, key=lambda t: abs(t[0] - spot))
    atm_ivs = [iv for (k, iv, _t, _d) in near_rows if abs(k - spot) <= spot * 0.01]
    res["atm_iv"] = round(sum(atm_ivs) / len(atm_ivs) * 100, 2) if atm_ivs else round(atm[1] * 100, 2)

    # --- 25Δ skew：看跌 |delta|≈0.25 与看涨 delta≈0.25 的 IV 之差
    puts = [(d, iv) for (_k, iv, t, d) in near_rows if t == "P"]
    calls = [(d, iv) for (_k, iv, t, d) in near_rows if t == "C"]
    pp, cc = pick_near(puts, 0.25), pick_near(calls, 0.25)
    if pp and cc:
        res["skew_25d"] = round((pp[1] - cc[1]) * 100, 2)
        res["_skew_delta_gap"] = round(abs(abs(pp[0]) - 0.25) + abs(abs(cc[0]) - 0.25), 3)

    # --- 期限结构
    if far:
        far_rows = by_exp[far]
        far_atm = [iv for (k, iv, _t, _d) in far_rows if abs(k - spot) <= spot * 0.01]
        if far_atm:
            res["term_slope"] = round(
                (sum(far_atm) / len(far_atm) - sum(atm_ivs) / len(atm_ivs)) * 100, 2) if atm_ivs else 0.0
    res["_expiry_days"] = float((near - now).days)
    res["_contracts"] = float(len(near_rows))
    return res


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    marks = curl_json(MARK_URL)
    if not marks:
        print("[FATAL] 币安期权 mark 取不到，本次不写库")
        return 1

    ts = int(time.time()) * 1000
    import sqlite3
    import db
    conn = sqlite3.connect(db.get_db_path(), timeout=30)

    for base, cfg in TARGETS.items():
        spot = None
        src = cfg["spot"]
        if src.startswith("binance:"):
            payload = curl_json(SPOT_URL.format(sym=src.split(":", 1)[1]), tries=5)
            if payload and "price" in payload:
                spot = float(payload["price"])
        else:                                  # 用本机日线收盘做参照（贵金属）
            row = conn.execute("SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1d' "
                               "AND close IS NOT NULL ORDER BY ts DESC LIMIT 1",
                               (src.split(":", 1)[1],)).fetchone()
            spot = float(row[0]) if row else None
        if not spot:
            print(f"  {base} 拿不到现货价，跳过（不猜 ATM）")
            continue
        out = analyse(base, marks, spot, cfg["min_days"], cfg["min_contracts"])
        if not out:
            print(f"  {base} 近月合约不足，跳过")
            continue
        meta_extra = {"spot": spot, "expiry_days": out.get("_expiry_days"),
                      "contracts": out.get("_contracts"),
                      "skew_delta_gap": out.get("_skew_delta_gap")}
        print(f"  {base} spot={spot:,.2f}  近月剩 {int(out.get('_expiry_days', 0))} 天  "
              f"ATM IV {out['atm_iv']}%", end="")
        if "skew_25d" in out:
            print(f" | 25Δ skew {out['skew_25d']:+.2f}", end="")
        if "term_slope" in out:
            print(f" | 期限结构 {out['term_slope']:+.2f}", end="")
        print()

        for key, suffix in (("atm_iv", "atm_iv"), ("skew_25d", "25d_skew"),
                            ("term_slope", "term_slope")):
            if key not in out:
                continue
            if not args.dry_run:
                external_data.ingest_external(
                    asset=base, indicator=f"{cfg['prefix']}_{suffix}",
                    records=[{"ts": ts, "value": out[key]}],
                    source=SOURCE, meta=dict(meta_extra, spot_source=cfg["spot"]))

    if not args.dry_run:
        print(f"  已写入（source={SOURCE}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
