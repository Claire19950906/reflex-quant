#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cboe_skew.py —— CBOE SKEW 指数（标普尾部风险定价，公开免费）

为什么加：
  VIX 量的是"未来 30 天的平均波动"，SKEW 量的是"同样是 30 天，左尾比右尾贵多少"。
  两个一起看才分得清"市场只是躁动"还是"真在花钱买崩盘保险"。
  本机原先只有 VIX 与 VIX 期限比，缺这一根。

口径：SKEW 是风险中性偏度的近似（CBOE 官方算法），数值越高说明虚值看跌相对更贵。
      它不预测方向，只反映保险的价格。

接口注意：cdn.cboe.com 这个端点会先回 307，**必须 -L 跟随重定向**，否则拿到的只是跳转页。

用法：
  python3 cboe_skew.py
  python3 cboe_skew.py --dry-run
"""
import argparse
import json
import subprocess
import time
from typing import Optional

import external_data

URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SKEW.json"
SOURCE = "cboe_skew"


# Cloudflare 会挑着请求拦（实测连打三次：200 / 000 / 307 各一次），
# 所以单发一次的成功率约 1/3。用 curl 自带的重试别自己写循环：
# `--retry-all-errors` 会把 000、5xx、以及"回了但没内容"的情况都算作失败重试。
URLS = [
    "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SKEW.json",
    "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_SKEW.json",  # 307 的目标域，直连更稳
]


def fetch(tries: int = 6) -> Optional[dict]:
    for url in URLS:
        cmd = ["curl", "-sL", "-m", "20", "--noproxy", "*", "--compressed",
               "--retry", str(tries), "--retry-delay", "2", "--retry-all-errors",
               "--retry-max-time", "90",
               "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
               "-H", "Referer: https://www.cboe.com/", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            body = out.stdout.strip()
            # 重定向页是 HTML，别把跳转页当数据解析
            if body.startswith("{") and '"data"' in body:
                return json.loads(body)
        except Exception as e:
            print(f"  {url.split('/')[2]} 异常 {type(e).__name__}")
    return None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    payload = fetch()
    if not payload:
        print("[FATAL] SKEW 取不到，本次不写库")
        return 1

    d = payload.get("data") or {}
    raw = d.get("current_price")
    if raw in (None, ""):
        print(f"[FATAL] 返回里没有价格字段（data keys={list(d)[:6]}），不写库")
        return 1

    value = float(str(raw).replace(",", ""))
    stamp = payload.get("timestamp") or ""
    # 用返回的时间戳（美东盘后报价时间）打点，取到天
    try:
        ts = int(time.mktime(time.strptime(stamp[:10], "%Y-%m-%d"))) * 1000
    except ValueError:
        ts = int(time.time()) // 86400 * 86400000
    print(f"  SKEW {value:.2f}  @ {stamp}")

    if not args.dry_run:
        external_data.ingest_external(
            asset="US_EQ", indicator="us_skew_index",
            records=[{"ts": ts, "value": round(value, 2)}], source=SOURCE,
            meta={"symbol": "^SKEW", "cn": "CBOE 标普尾部风险偏度", "quote_time": stamp})
        print("  已写入（source=cboe_skew）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
