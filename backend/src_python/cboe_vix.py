#!/usr/bin/env python3
"""CBOE 官方延迟报价（VIX / VVIX）。

为什么需要：
  · VIX 之前只能走 FRED 的 VIXCLS，而 FRED 对本机 IP 限流，几小时才更新一次，
    期权恐慌指标这种日内变量，几小时前的值基本没用。
  · CBOE 官网的 delayed_quotes 接口是该指标**发布方自己**的数据，不需要 key，
    更新到分钟级，比任何二手转述都可靠。

写入 external_indicators asset=US_MACRO，source=cboe：
  us_vix_cboe    VIX 指数（延迟约 15 分钟，官方口径）
  us_vix_change  VIX 当日涨跌（点）
"""
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

requests.Session.trust_env = False
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://www.cboe.com/"}
TIMEOUT = 15
SYMBOLS = {"_VIX": ("us_vix_cboe", "VIX"),
           "_VVIX": ("us_vvix_cboe", "VVIX"),
           # 近月（9 日）VIX：当它高于 30 日期 VIX 时，说明恐慌集中在眼前，
           # 这是期限结构倒挂，比单看 VIX 数值更能区分"急跌"和"慢跌"。
           "_VIX9D": ("us_vix9d_cboe", "VIX9D")}


def _quote(sym: str) -> Optional[Dict[str, Any]]:
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            return (r.json() or {}).get("data") or {}
        except Exception as exc:
            if attempt == 2:
                print(f"[WARN] CBOE {sym} 失败: {type(exc).__name__}: {exc}", flush=True)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def _fred_fallback() -> Optional[float]:
    """CBOE 不通时的退路：FRED 的 VIXCLS 单发 CSV。

    两条路都是真数据，只是一条更新到分钟级、一条是日频，
    所以 fallback 写库时 meta 里标明来源差异，不混为同一口径。
    """
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        last = [x for x in r.text.strip().splitlines() if "," in x][-1]
        val = float(last.split(",")[1])
        return val if val > 0 else None
    except Exception:
        return None


def main() -> int:
    ts = int(time.time())
    written = 0
    for sym, (indicator, label) in SYMBOLS.items():
        d = _quote(sym)
        if not d:
            continue
        try:
            price = float(d["current_price"])
        except (KeyError, TypeError, ValueError):
            continue
        meta = {"unit": "index", "label": label,
                "quote_time": d.get("timestamp"), "delayed": True}
        external_data.ingest_external(
            asset="US_MACRO", indicator=indicator,
            records=[{"ts": ts, "value": round(price, 3)}],
            source="cboe", meta=meta,
        )
        written += 1
        try:
            chg = float(d.get("price_change"))
            external_data.ingest_external(
                asset="US_MACRO", indicator=indicator.replace("_cboe", "_change"),
                records=[{"ts": ts, "value": round(chg, 3)}],
                source="cboe", meta=meta,
            )
            written += 1
        except (KeyError, TypeError, ValueError):
            pass
        print(f"[{time.strftime('%H:%M:%S')}] CBOE {label}={price}"
              f" | 涨跌 {d.get('price_change')} | 报价时间 {d.get('timestamp')}", flush=True)
    if written == 0:
        # CBOE 偶发整段 TLS 被 reset（本机网络对境外站点普遍如此），
        # 退到 FRED 日频值，宁可慢也不空着。
        val = _fred_fallback()
        if val:
            external_data.ingest_external(
                asset="US_MACRO", indicator="us_vix_cboe",
                records=[{"ts": ts, "value": round(val, 3)}],
                source="fred_csv",
                meta={"unit": "index", "label": "VIX", "delayed": True,
                      "fallback": "fred_csv", "note": "CBOE 不通，退回 FRED 日频值"},
            )
            written += 1
            print(f"[FALLBACK] VIX 取 FRED 日频值 {val}", flush=True)

    # 期限结构比值：VIX9D / VIX，>1 即近端压力大于远端
    try:
        rows = {}
        for ind in ("us_vix9d_cboe", "us_vix_cboe"):
            r = external_data.list_indicators(asset="US_MACRO", indicator=ind, limit=1)
            if r:
                rows[ind] = float(r[0]["value"])
        if len(rows) == 2 and rows["us_vix_cboe"]:
            ratio = round(rows["us_vix9d_cboe"] / rows["us_vix_cboe"], 4)
            external_data.ingest_external(
                asset="US_MACRO", indicator="us_vix_term_ratio",
                records=[{"ts": ts, "value": ratio}],
                source="cboe",
                meta={"unit": "VIX9D/VIX", "note": ">1 表示近端恐慌强于远端（期限倒挂）"},
            )
            written += 1
            print(f"  VIX 期限比 VIX9D/VIX={ratio}", flush=True)
    except Exception as exc:
        print(f"[WARN] 期限比计算失败: {type(exc).__name__}: {exc}", flush=True)

    print(f"共写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
