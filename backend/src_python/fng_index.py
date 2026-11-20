#!/usr/bin/env python3
"""恐惧与贪婪指数采集器 —— 直接对应"市场共识与心理预期"。

为什么需要：
  · 系统的情绪维度过去只有"新闻情感分"（模型给每条新闻打 -100~100）。
    那是"媒体说了什么"，不是"市场在怕还是在贪"。
  · alternative.me 的 FNG 指数用波动率、市场动量、社交媒体热度、
    主导地位、成交量五个分量算出一个 0-100 的数，是市场心理的直接读数。
  · 有了它，"利好消息但指数极贪"这类背离才能被识别出来。

写入 external_indicators，source='alternative_me'：
  fng_value        0-100 的读数
  fng_class        文字档位（映射成数值：极恐 0 / 恐惧 25 / 中性 50 / 贪婪 75 / 极贪 100，
                   便于画刻度，真实档位放 meta）
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

URL = "https://api.alternative.me/fng/?limit=2"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 12

# 档位 -> 刻度值（仅用于画图对齐，不改变原始读数）
BAND_SCALE = {
    "Extreme Fear": 0,
    "Fear": 25,
    "Neutral": 50,
    "Greed": 75,
    "Extreme Greed": 100,
}


def _get() -> Optional[Dict[str, Any]]:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            rows = (r.json() or {}).get("data") or []
            return rows[0] if rows else None
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[ERROR] FNG 拉取失败: {type(last).__name__}: {last}", flush=True)
    return None


def main() -> int:
    row = _get()
    if not row:
        return 1
    try:
        value = float(row["value"])
    except Exception:
        return 1
    band = str(row.get("value_classification") or "").strip()
    ts = int(time.time())

    written = 0
    for indicator, val, meta in (
        ("fng_value", value, {"band": band, "raw_ts": row.get("timestamp")}),
        ("fng_class", float(BAND_SCALE.get(band, 50)), {"band": band}),
    ):
        try:
            external_data.ingest_external(
                asset="MARKET", indicator=indicator,
                records=[{"ts": ts, "value": val}],
                source="alternative_me", meta=meta,
            )
            written += 1
        except Exception as exc:
            print(f"[WARN] {indicator} 写入失败: {type(exc).__name__}: {exc}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] FNG={value:.0f} ({band}) | 写入 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
