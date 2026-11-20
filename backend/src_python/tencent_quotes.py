#!/usr/bin/env python3
"""腾讯行情采集器：给「新闻里出现、但系统没有价格源」的标的补行情与量能。

为什么需要这个：
  · 海外源全断（Yahoo 停在 35 天前、FRED 停在 37 天前），只有 Bybit 还通。
  · 但新闻里最常出现的标的其实是 MU / NVDA / TSLA / AMD 这类美股，
    以及 A 股指数、港股。这些标的一条价格都没有，于是「新闻说 MU 涨 6%」
    系统无法自己验证，只能信新闻 —— 归因和感知到的涨跌都缺少实证。
  · 腾讯行情接口（qt.gtimg.cn）本机可达，覆盖 A 股、美股个股/指数、港股，
    返回里带成交量/成交额，正好补「成交量、量能」这一维。

写入：external_indicators，source='tencent'
  tencent_price        最新价
  tencent_change_pct   当日涨跌幅(%)
  tencent_volume       成交量(股/手)
  tencent_amount       成交额(若接口给出)

字段位置（腾讯 v_ 串以 ~ 分隔）：
  [1] 名称 [2] 代码 [3] 最新价 [4] 昨收 [5] 今开 [6] 成交量(手)
  [32] 涨跌幅(%) [37] 成交额(万)
"""
import os
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

BASE = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}
TIMEOUT = 10

# 腾讯代码 -> (系统资产名, 说明)
TARGETS = {
    # 上证/深证已移出：这个账户主要做美元、黄金、加密、美股，A 股基本不做，
    # 留着只会往库里灌无关序列。需要时把两行加回来即可。
    "usNDX": ("NDX", "纳斯达克100"),
    "usMU": ("MU", "美光科技"),
    "usNVDA": ("NVDA", "英伟达"),
    "usTSLA": ("TSLA", "特斯拉"),
    "usAMD": ("AMD", "超威半导体"),
    "hk00700": ("TENCENT", "腾讯控股"),
    # 指数补充：雅虎指数序列已断 36 天，腾讯这条仍通，
    # 道指/纳指综合/标普 500 是判断"风险偏好"最直接的三条（注意标普代码带点号 us.INX）。
    "usDJI": ("DJI", "道琼斯工业"),
    "usIXIC": ("IXIC", "纳斯达克综合"),
    "us.INX": ("SPX", "标普500"),
}

# 纽约商品期货（腾讯 hf_ 前缀）。字段与股票完全不同：
# [0] 最新价 [1] 涨跌幅% [4] 最高 [5] 最低 [8] 昨收 [13] 名称。
# 用途是给已有的 XAU / XAG / WTI 做**第二家独立报价**，天然气则是新增品类。
COMMODITIES = {
    "hf_GC": ("XAU", "纽约黄金"),
    "hf_SI": ("XAG", "纽约白银"),
    "hf_CL": ("WTI", "纽约原油"),
    "hf_NG": ("NG", "美国天然气"),
}


def _fetch(codes: List[str]) -> str:
    r = requests.get(BASE + ",".join(codes), headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "gbk"
    return r.text


def _parse(text: str) -> Dict[str, List[str]]:
    """v_xxx="a~b~c"; -> {xxx: [a,b,c]}"""
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r'v_([a-zA-Z0-9_.]+)="([^"]*)"', text):
        code, body = m.group(1), m.group(2)
        out[code] = body.split("~")
    return out


def _parse_comma(text: str) -> Dict[str, List[str]]:
    """商品期货用逗号分隔（v_hf_GC="3897.13,0.30,..."），和个股的 ~ 分隔不同。"""
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r'v_([a-zA-Z0-9_.]+)="([^"]*)"', text):
        code, body = m.group(1), m.group(2)
        out[code] = [x.strip() for x in body.split(",")]
    return out


def _num(parts: List[str], idx: int) -> Optional[float]:
    try:
        v = parts[idx].strip()
        return float(v) if v not in ("", "-", "--") else None
    except (IndexError, ValueError):
        return None


def main() -> int:
    codes = list(TARGETS)
    try:
        text = _fetch(codes)
    except Exception as exc:
        print(f"[ERROR] 腾讯行情拉取失败: {type(exc).__name__}: {exc}", flush=True)
        return 1

    parsed = _parse(text)
    ts = int(time.time())
    written = 0
    miss = []

    for code, (asset, label) in TARGETS.items():
        parts = parsed.get(code)
        if not parts or len(parts) < 6 or not parts[3].strip():
            miss.append(code)
            continue
        price = _num(parts, 3)
        chg = _num(parts, 32)
        vol = _num(parts, 6)
        amt = _num(parts, 37)
        if not price:
            miss.append(code)
            continue
        payload = [
            ("tencent_price", price, {"label": label, "code": code}),
        ]
        if chg is not None:
            payload.append(("tencent_change_pct", chg, {"label": label, "code": code}))
        # 美股指数（道指/纳指综合/标普）的 [6]/[37] 并不是成交量与成交额，
        # 而是成分股总股数与总市值（实测 53 万亿 = 道指总市值）。
        # 标成"成交额"就是错标，宁可不写。个股与 A 股指数这两个字段是真实的。
        if asset not in ("DJI", "IXIC", "SPX"):
            if vol:
                payload.append(("tencent_volume", vol, {"label": label, "code": code}))
            if amt:
                payload.append(("tencent_amount", amt, {"label": label, "code": code}))
        for indicator, value, meta in payload:
            try:
                external_data.ingest_external(
                    asset=asset, indicator=indicator,
                    records=[{"ts": ts, "value": value}],
                    source="tencent", meta=meta,
                )
                written += 1
            except Exception as exc:
                print(f"[WARN] {asset}/{indicator} 写入失败: {type(exc).__name__}: {exc}", flush=True)

    # ── 商品期货：给贵金属与原油补第二家报价（双验证）──
    try:
        ctext = _fetch(list(COMMODITIES))
        cparsed = _parse_comma(ctext)
        for code, (asset, label) in COMMODITIES.items():
            arr = cparsed.get(code)
            if not arr or not arr[0].strip():
                print(f"[WARN] {label} 无报价", flush=True)
                continue
            try:
                last = float(arr[0])
            except ValueError:
                continue
            meta = {"label": label, "code": code, "venue": "COMEX/NYMEX"}
            try:
                external_data.ingest_external(asset=asset, indicator="hf_fut_price",
                                              records=[{"ts": ts, "value": last}],
                                              source="tencent", meta=meta)
                written += 1
                if len(arr) > 1 and arr[1].strip():
                    chg = float(arr[1])
                    external_data.ingest_external(asset=asset, indicator="hf_fut_chg_pct",
                                                  records=[{"ts": ts, "value": chg}],
                                                  source="tencent", meta=meta)
                    written += 1
            except Exception as exc:
                print(f"[WARN] 商品 {asset} 写入失败: {type(exc).__name__}: {exc}", flush=True)
    except Exception as exc:
        print(f"[WARN] 商品期货拉取失败: {type(exc).__name__}: {exc}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] 写入 {written} 条 | 覆盖 "
          f"{len(TARGETS)-len(miss)}/{len(TARGETS)} 标的"
          + (f" | 缺失: {miss}" if miss else ""), flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
