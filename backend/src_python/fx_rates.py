#!/usr/bin/env python3
"""汇率采集器（Frankfurter / 欧洲央行参考价）。

为什么需要：
  · 系统里有黄金、原油、BTC、A 股、美股，但没有"美元强弱"这把共同的尺子。
    美元一走强，以美元计价的一切资产都会被动下跌；缺这条，归因会把
    "美元推动"误判成"资产自身逻辑"。
  · Frankfurter 用的是欧洲央行每日参考价，来源干净、无需 key、口径稳定。

写入 external_indicators，source='frankfurter'：
  fx_usdcny   USD/CNY
  fx_eurusd   EUR/USD
  fx_usdjpy   USD/JPY
  fx_usdhkd   USD/HKD（港股联动）
  dxy_ecb_computed 美元指数（按 ICE 官方六币权重 × ECB 参考汇率计算，非官方报价）

限制：ECB 每日更新一次（16:00 CET 左右），一天两轮足够。
"""
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

URL = "https://api.frankfurter.dev/v1/latest"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "application/json"}
TIMEOUT = 12
PAIRS = {
    "fx_usdcny": "CNY",
    "fx_eurusd": "EUR",
    "fx_usdjpy": "JPY",
    "fx_usdhkd": "HKD",
}


TENCENT_URL = "https://qt.gtimg.cn/q=fxEURUSD,fxUSDJPY,fxGBPUSD,fxUSDCAD,fxUSDSEK,fxUSDCHF,fxUSDCNY,fxUSDHKD"
TENCENT_REFERER = {"Referer": "https://gu.qq.com/", "User-Agent": HEADERS["User-Agent"]}


def _fetch_tencent() -> Optional[Dict[str, Any]]:
    """腾讯外汇兜底。

    v333: Frankfurter（ECB 的第三方包装）在这台机器上开始出现 SSL EOF，
    而 ECB 官方 XML 直接被墙（000）。腾讯外汇免 key、国内直连、覆盖 DXY 六个币种，
    且报价日期与官方一致，可作为降级主源 —— 指标名与算法都不变，只是换了取数通道。

    腾讯返回的是"货币对"报价（EURUSD / USDJPY ...），这里统一换成「1 USD = x 外币」，
    与 Frankfurter 的 rates 语义完全一致，下游无需改动。
    """
    last = None
    for attempt in range(3):
        try:
            r = requests.get(TENCENT_URL, headers=TENCENT_REFERER, timeout=TIMEOUT)
            r.raise_for_status()
            text = r.content.decode("gbk", errors="ignore")
            quotes = {}
            for line in text.splitlines():
                if "=" not in line or '"' not in line:
                    continue
                code = line.split("=")[0].replace("v_", "").strip()
                fields = line.split('"')[1].split("~")
                if len(fields) > 3:
                    quotes[code] = fields[3]
            date_s = None
            if quotes:
                for line in text.splitlines():
                    fields = line.split('"')[-2].split("~") if line.count('"') >= 2 else []
                    if len(fields) > 12:
                        date_s = fields[-1]
                        break
            def _f(code):
                v = quotes.get(code)
                return float(v) if v else None
            eur, jpy, gbp = _f("fxEURUSD"), _f("fxUSDJPY"), _f("fxGBPUSD")
            cad, sek, chf = _f("fxUSDCAD"), _f("fxUSDSEK"), _f("fxUSDCHF")
            cny, hkd = _f("fxUSDCNY"), _f("fxUSDHKD")
            rates = {}
            if eur: rates["EUR"] = round(1.0 / eur, 6)
            if jpy: rates["JPY"] = jpy
            if gbp: rates["GBP"] = round(1.0 / gbp, 6)
            if cad: rates["CAD"] = cad
            if sek: rates["SEK"] = sek
            if chf: rates["CHF"] = chf
            if cny: rates["CNY"] = cny
            if hkd: rates["HKD"] = hkd
            if len(rates) >= 5:
                return {"rates": rates, "date": date_s, "_source": "tencent_fx"}
            last = RuntimeError(f"腾讯外汇字段不足: {sorted(rates)}")
        except Exception as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    print(f"[ERROR] 腾讯外汇兜底失败: {type(last).__name__}: {last}", flush=True)
    return None


def _fetch() -> Optional[Dict[str, Any]]:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(URL, params={"base": "USD"}, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            data["_source"] = "frankfurter"
            return data
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[WARN] Frankfurter 拉取失败: {type(last).__name__}: {last} —— 降级腾讯外汇", flush=True)
    return _fetch_tencent()


# ICE 美元指数官方权重（固定，1999 年至今未变）。
# 用 ECB 参考汇率按该公式计算，因此是「按官方公式算出来的美元指数」，
# 不是 ICE 官方报价，指标名刻意写成 dxy_ecb_computed 以免混用。
DXY_WEIGHTS = {"EUR": -0.576, "JPY": 0.136, "GBP": -0.119,
               "CAD": 0.091, "SEK": 0.042, "CHF": 0.036}
DXY_CONST = 50.14348112


def _computed_dxy(rates: dict) -> Optional[float]:
    """用主请求已返回的 rates 计算，避免为 6 个币种再发一次请求。"""
    try:
        # Frankfurter 的 rates 是「1 USD = x 外币」
        eur = 1.0 / float(rates["EUR"])
        jpy = float(rates["JPY"])
        gbp = 1.0 / float(rates["GBP"])
        cad, sek, chf = float(rates["CAD"]), float(rates["SEK"]), float(rates["CHF"])
        expo = (eur ** DXY_WEIGHTS["EUR"]) * (jpy ** DXY_WEIGHTS["JPY"]) \
            * (gbp ** DXY_WEIGHTS["GBP"]) * (cad ** DXY_WEIGHTS["CAD"]) \
            * (sek ** DXY_WEIGHTS["SEK"]) * (chf ** DXY_WEIGHTS["CHF"])
        return round(DXY_CONST * expo, 3)
    except Exception as exc:
        print(f"[WARN] 计算美元指数失败: {type(exc).__name__}: {exc}", flush=True)
        return None


def main() -> int:
    data = _fetch()
    if not data:
        return 1

    rates = data.get("rates") or {}
    src = data.get("_source", "frankfurter")
    ts = int(time.time())
    written = 0

    for indicator, code in PAIRS.items():
        value = rates.get(code)
        if value is None:
            continue
        if indicator == "fx_eurusd":
            value = 1.0 / float(value) if float(value) else None  # 接口是 USD->EUR
        if value is None:
            continue
        external_data.ingest_external(
            asset="FX", indicator=indicator,
            records=[{"ts": ts, "value": float(value)}],
            source=src,
            meta={"unit": "ratio", "ref_date": data.get("date"),
                  "channel": "ECB 参考汇率（经 Frankfurter）" if src == "frankfurter" else "腾讯外汇（国内直连兜底）"},
        )
        written += 1

    eur = rates.get("EUR")
    if eur:
        # 旧的 fx_dxy_proxy（只用 EUR 单因子、把 HKD 也算进去）偏差明显：
        # 同一时刻它给 106.23，按 ICE 官方六币权重的结果是 102.98，差 3 个点。
        # 既然能按官方公式算，就不该再写粗糙代理，这里停写，改由 dxy_ecb_computed 承担。
        pass

    dxy = _computed_dxy(data.get("rates") or {})
    if dxy:
        external_data.ingest_external(
            asset="FX", indicator="dxy_ecb_computed",
            records=[{"ts": int(time.time()), "value": dxy}],
            source=src,
            meta={"unit": "index", "method": "ICE 官方权重 × 六币汇率",
                  "channel": "ECB 参考汇率（经 Frankfurter）" if src == "frankfurter" else "腾讯外汇（国内直连兜底）",
                  "note": "非 ICE 官方报价，为按官方公式计算值", "base": "USD"},
        )
        print(f"  计算美元指数 dxy_ecb_computed={dxy}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] 汇率 {data.get('date')} "
          f"USD/CNY={rates.get('CNY')} EUR/USD={1/float(rates.get('EUR',1)):.4f} "
          f"写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
