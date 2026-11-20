#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coinmetrics.py —— 链上指标（CoinMetrics Community API，免费、免 key）

为什么加：本机原有的链上数据只有 Blockchair 的 mempool / 手续费 / 日交易数，
缺的是"钱到底有没有在动"这一层 —— 活跃地址、调整后转账价值、已实现市值、NVT。
这四个是不用 key 就能拿到的机构级口径，CoinMetrics 的 community 端点直接给。

口径说明（避免误读）：
  - TxTfrValAdjUSD 是剔除找零与内部转账后的转账价值，跟交易所成交量不是一回事；
  - CapRealUSD 是"已实现市值"（按每枚币上次链上移动时的价格累计），
    和市值（价格×存量）不同，它更能反映持币成本；
  - NVTAdj = 市值 / 90 日均调整后转账价值，是链上的"市盈率"。

写入 asset='BTC' / 'ETH'，source='coinmetrics'。缺失值一律不写，不用 0 顶。

用法：
  python3 coinmetrics.py                 # 最近 400 天
  python3 coinmetrics.py --days 1500
  python3 coinmetrics.py --dry-run
"""
import argparse
import time
from typing import Dict, List, Optional

import external_data

BASE = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
SOURCE = "coinmetrics"

# usdt/usdc 只取流通量：稳定币供应量是加密市场的"干火药"，
# 增发通常领先于风险偏好回升，是这套系统原先没有的一根尺子。
ASSETS = ["btc", "eth", "usdt", "usdc"]
STABLE_ONLY = {"usdt", "usdc"}

# CoinMetrics 指标名 -> (我们库里的指标名, 中文义)
METRICS: Dict[str, tuple] = {
    "AdrActCnt":        ("cm_active_addr", "活跃地址数"),
    "TxTfrValAdjUSD":   ("cm_transfer_usd", "调整后链上转账额（USD）"),
    "CapRealUSD":       ("cm_realized_cap", "已实现市值（USD）"),
    "NVTAdj":           ("cm_nvt_adj", "NVT（市值/90日转账额）"),
    "IssTotUSD":        ("cm_issuance_usd", "当日增发折美元"),
    "SplyCur":          ("cm_supply", "流通量"),
    "HashRate":         ("cm_hashrate", "算力（仅 BTC）"),
}


def fetch(asset: str, start: str, tries: int = 8) -> Optional[List[dict]]:
    """逐组请求；某指标在当前套餐不可用时，从报错里读出指标名、剔掉再重试。

    两个必须注意的点：
      1) community 免费层不是全指标开放（TxTfrValAdjUSD 就报 not available），
         所以不能假设目录全可用，让接口自己说哪些能拿；
      2) URL 必须在每次重试时重新拼 —— 剔掉指标后仍用旧 URL 会一直撞同一个 403。
    """
    import json
    import subprocess
    import urllib.parse

    keep = list(METRICS.keys())
    if asset in STABLE_ONLY:
        keep = ["SplyCur"]                    # 稳定币没有地址数/算力这类指标
    for _ in range(tries):
        if not keep:
            return None
        params = {"assets": asset, "metrics": ",".join(keep), "frequency": "1d",
                  "start_time": start, "page_size": "10000", "format": "json"}
        url = BASE + "?" + urllib.parse.urlencode(params)
        # --noproxy 必须显式给：本机办公代理会把这域名拦掉
        cmd = ["curl", "-s", "-m", "45", "--noproxy", "*", "--compressed",
               "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
               "-H", "Accept: application/json", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
            if not out.stdout.strip():
                print(f"  {asset} 空响应（curl 退出码 {out.returncode}），重试")
                time.sleep(2)
                continue
            payload = json.loads(out.stdout)
            if payload.get("data"):
                dropped = set(METRICS) - set(keep)
                if dropped:
                    print(f"  {asset} 免费层不含：{', '.join(sorted(dropped))}")
                return payload["data"]
            msg = str(payload.get("error", {}).get("message", ""))[:200]
            hit = next((m for m in keep if m in msg), None)
            if hit:
                keep.remove(hit)
                print(f"  {asset} 指标 {hit} 不可用，剔除后重试")
            else:
                print(f"  {asset} 无 data：{msg}")
                return None
        except Exception as e:
            print(f"  {asset} 异常 {type(e).__name__}，重试")
            time.sleep(2)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
    total = 0

    for asset in ASSETS:
        rows = fetch(asset, start)
        if not rows:
            print(f"  {asset.upper()} 拿不到，跳过（不补假数）")
            continue
        print(f"  {asset.upper()} 拿到 {len(rows)} 天：{rows[0]['time'][:10]} ~ {rows[-1]['time'][:10]}")

        for cm_key, (name, cn) in METRICS.items():
            if cm_key == "HashRate" and asset != "btc":
                continue                       # 算力只有 PoW 有意义
            records = []
            nulls = 0
            for row in rows:
                raw = row.get(cm_key)
                if raw in (None, "", "null"):
                    nulls += 1                 # 缺失日不写，保持空洞
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    nulls += 1
                    continue
                # 时间统一取该日 UTC 零点
                ts = int(time.mktime(time.strptime(row["time"][:10], "%Y-%m-%d"))) * 1000
                records.append({"ts": ts, "value": val})
            if not records:
                print(f"    {name:<18} 全为空，跳过")
                continue
            latest = records[-1]["value"]
            if not args.dry_run:
                external_data.ingest_external(asset=asset.upper(), indicator=name,
                                              records=records, source=SOURCE,
                                              meta={"metric": cm_key, "cn": cn, "unit": "raw"})
                total += len(records)
            suffix = f"，缺失 {nulls} 天" if nulls else ""
            print(f"    {name:<18} {len(records):>4} 天，最新 {latest:,.2f}{suffix}")

    if not args.dry_run:
        print(f"  写入 {total} 行 → external_indicators(source={SOURCE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
