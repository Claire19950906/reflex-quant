"""market_data_fallback — 境外行情接口被网络 RST 时的国内可达兜底（v342）

背景（2026-11-14 本机实测）：
  · `query1.finance.yahoo.com`  → 不可达（SSL EOF），yfinance 整链哑火
  · `push2his.eastmoney.com`    → 不可达（RemoteDisconnected / curl 000）
  · `api.coingecko.com`         → **Python requests 被 RST**，但系统 curl 200
  · `api.binance.com`           → 同上：requests 被 RST，curl 200
  · `stock2.finance.sina.com.cn`（期货日线）/ `qt.gtimg.cn`（实时）→ curl 200
根因不是代码，是本机出网：Python 用 OpenSSL，TLS 指纹被中途 reset；系统 curl
走 SecureTransport，同样的 URL 能通。所以这里统一用 `curl` 子进程取数，
作为 yfinance / 东财 / CoinGecko 之后的最后一级降级。

只做「取数 + 解析」，不碰数据库，不改变调用方的落库口径。
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import List, Optional, Tuple

HTTP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 新浪全球期货日线：XAU→COMEX黄金, XAG→白银, WTI→原油
SINA_FUTURES_SYMBOL = {
    "XAU": "GC",
    "XAG": "SI",
    "WTI": "CL",
}

# Binance 现货日线
BINANCE_SYMBOL = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT",
}


def _curl_text(url: str, timeout: int = 15, referer: Optional[str] = None) -> str:
    """用系统 curl 取文本。失败抛 RuntimeError（调用方负责降级）。"""
    cmd = [
        "curl", "-s", "-m", str(timeout),
        "-A", HTTP_UA,
        "-H", "Accept: */*",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(f"curl rc={proc.returncode} {proc.stderr.strip()[:100]}")
    return proc.stdout


def _json_from_curl(url: str, timeout: int = 15, referer: Optional[str] = None):
    return json.loads(_curl_text(url, timeout=timeout, referer=referer))


def _ts_of_date(date_str: str) -> Optional[int]:
    """'2026-11-14' → UTC 零点秒。"""
    try:
        return int(
            time.mktime(time.strptime(date_str, "%Y-%m-%d")) - time.timezone
        )
    except Exception:
        return None


def fetch_sina_futures_daily(asset: str, keep_days: int = 420) -> List[Tuple]:
    """新浪全球期货日线 → [(ts, o, h, l, c, vol)]，按时间升序。

    返回空列表表示：该 asset 无映射 / 取数失败 / 解析失败 —— 调用方继续降级。
    """
    sym = SINA_FUTURES_SYMBOL.get((asset or "").upper())
    if not sym:
        return []
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        f"var%20_{sym}=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}"
    )
    text = ""
    for _ in range(2):
        try:
            text = _curl_text(url, timeout=15, referer="https://finance.sina.com.cn")
            break
        except Exception:
            time.sleep(0.6)
    if not text:
        return []
    start = text.find("([")
    end = text.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        rows = json.loads(text[start + 1:end + 1])
    except Exception:
        return []
    out: List[Tuple] = []
    cutoff = time.time() - keep_days * 86400
    for r in rows:
        ts = _ts_of_date(str(r.get("date") or ""))
        if ts is None or ts < cutoff:
            continue
        try:
            out.append((
                ts,
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                int(float(r.get("volume") or 0)),
            ))
        except Exception:
            continue
    return out


def fetch_binance_daily(asset: str, keep_days: int = 420) -> List[Tuple]:
    """Binance 现货日线 → [(ts, o, h, l, c, vol)]（日线开=UTC 00:00）。"""
    sym = BINANCE_SYMBOL.get((asset or "").upper())
    if not sym:
        return []
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={sym}&interval=1d&limit={min(max(keep_days, 30), 1000)}"
    )
    data = None
    # Binance 对突发请求会返回空数组/超时: 3 次重试 + 1.5s 退避
    for _ in range(3):
        try:
            data = _json_from_curl(url, timeout=15)
            if isinstance(data, list) and data:
                break
        except Exception:
            pass
        time.sleep(1.5)
    if not isinstance(data, list):
        return []
    out: List[Tuple] = []
    cutoff = time.time() - keep_days * 86400
    for k in data:
        try:
            ts = int(k[0]) // 1000
            if ts < cutoff:
                continue
            out.append((
                ts,
                float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                int(float(k[5])),
            ))
        except Exception:
            continue
    return out
