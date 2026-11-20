#!/usr/bin/env python3
"""OKX 杠杆与多空采集器 —— 给 Bybit 做第二份源。

为什么需要：
  · 现在判断"市场是谁在推"只有 Bybit 一家。单一交易所的持仓和资金费率
    会被该所的做市与套利结构带偏（同一时刻两所资金费率常差 1-3 个基点）。
  · 只有一份源时，"多空比 71:29"无法判断是全市场共识还是单所特征。
  · 两所同向 = 真共识；两所背离 = 跨所套利在动。这正是"谁在投、杠杆多少"
    这一条能力从"有数据"升级到"能判断"的分界线。

写入 external_indicators，source='okx'：
  okx_funding_rate       资金费率（小数，0.0001 = 0.01%）
  okx_open_interest_usd  持仓名义（美元）
  okx_long_short_ratio   多空账户比（>1 多头账户多）

接口需要容错：实测 funding 端点偶发 SSL_ERROR_SYSCALL，故三次退避重试。
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

BASE = "https://www.okx.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 12

# 系统资产 -> OKX 永续合约
ASSETS = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
}


def _get(path: str, params: Optional[Dict[str, str]] = None) -> Optional[Any]:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(BASE + path, params=params or {}, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if str(body.get("code")) != "0":
                print(f"[WARN] OKX {path} code={body.get('code')} msg={body.get('msg')}", flush=True)
                return None
            return body.get("data")
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[WARN] OKX {path} 失败: {type(last).__name__}: {last}", flush=True)
    return None


def _funding(inst: str) -> Optional[float]:
    rows = _get("/api/v5/public/funding-rate", {"instId": inst})
    if not rows:
        return None
    try:
        return float(rows[0]["fundingRate"])
    except Exception:
        return None


def _oi_usd(inst: str) -> Optional[float]:
    rows = _get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
    if not rows:
        return None
    try:
        return float(rows[0]["oiUsd"])
    except Exception:
        return None


def _ls_ratio(ccy: str) -> Optional[float]:
    rows = _get("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                {"ccy": ccy, "period": "1H"})
    if not rows:
        return None
    try:
        return float(rows[0][1])
    except Exception:
        return None


def main() -> int:
    ts = int(time.time())
    written = 0
    ok_assets: List[str] = []

    for asset, inst in ASSETS.items():
        any_ok = False
        for indicator, value, meta in (
            ("okx_funding_rate", _funding(inst), {"inst": inst}),
            ("okx_open_interest_usd", _oi_usd(inst), {"inst": inst}),
            ("okx_long_short_ratio", _ls_ratio(asset), {"inst": inst}),
        ):
            if value is None:
                continue
            try:
                external_data.ingest_external(
                    asset=asset, indicator=indicator,
                    records=[{"ts": ts, "value": float(value)}],
                    source="okx", meta=meta,
                )
                written += 1
                any_ok = True
            except Exception as exc:
                print(f"[WARN] {asset}/{indicator} 写入失败: {type(exc).__name__}: {exc}", flush=True)
        if any_ok:
            ok_assets.append(asset)

    print(f"[{time.strftime('%H:%M:%S')}] 写入 {written} 条 | 覆盖 "
          f"{len(ok_assets)}/{len(ASSETS)} 标的 {ok_assets}", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
