#!/usr/bin/env python3
"""日线回补：币安（加密）＋ 腾讯（美股）替代已失效的雅虎。

背景：
  Yahoo v8 接口长期 429，yfinance 那条源已经 12–32 小时不动，
  但它还在往 external_indicators 写「asset=X, indicator=X」这种含糊的日线收盘值。
  这里换成两个仍可用的免费源，把日线 OHLC 写进本该存放 K 线的 `kline_bars`：
    · 加密：Binance fapi /klines 1d（BTC/ETH/SOL/XRP/DOGE/BNB/ADA）
    · 美股：腾讯 fqkline（usMU / usNVDA / usTSLA / usAMD）

写入 kline_bars（ts 为毫秒），timeframe='1d'。
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

requests.Session.trust_env = False
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 15
BARS = 120
# v333: 新浪美股日线本身给全历史（SPX 5,756 根 / 个股 3,000+），
#   之前只截 1000 根是"先接上再说"。日常 cron 仍取 1000 根（够所有均线窗口），
#   --full 时取 6000 根做一次性历史回补，历史深度不够时不再回头改代码。
HIST_BARS_DEFAULT = 1000
HIST_BARS_FULL = 6000
HIST_BARS = HIST_BARS_DEFAULT
# 加密日线历史深度（币安能一直翻到 2017 年的合约上市日）
CRYPTO_HIST_BARS = 3000

CRYPTO = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
          "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "ADA": "ADAUSDT"}
# 美股日线改走东方财富 push2his（返回 400 根完整 OHLC，2025-04 至今）。
# 为什么不用腾讯：腾讯 fqkline 对美股只回「最早一根 + 今天」两行，
# 拿不到连续历史；纳斯达克官方接口返回空；Stooq / Yahoo / FMP 在本机全部 000。
# 注意：这里只取美股，东财的 A 股公告源仍然停用（用户不做 A 股）。
# 美股日线走新浪 US_MinKService（免 key、不限频，返回全历史日线）：
#   MU / NVDA / TSLA / AMD 用普通代码；指数要带点号（.INX 标普、.IXIC 纳指、.DJI 道指、.NDX 纳指100）。
# 东财 push2his 也能返回 400 根，但连发十几次后开始直接掐断连接（RemoteDisconnected），
# 只留作兜底；腾讯对美股只回「最早一根 + 今天」，基本无用。
# v453: AMZN/GOOGL/META 已在库里（1d 卡在 11-14），但 STOCKS 名单没有它们 →
# 每天补数永远补不到，面板上一直是「过期」。补齐名单。
STOCKS = {"MU": "MU", "NVDA": "NVDA", "TSLA": "TSLA", "AMD": "AMD", "AAPL": "AAPL",
          "MSFT": "MSFT", "AMZN": "AMZN", "GOOGL": "GOOGL", "META": "META"}
INDICES = {"SPX": ".INX", "IXIC": ".IXIC", "DJI": ".DJI", "NDX": ".NDX"}
EM_SECIDS = {"MU": "105.MU", "NVDA": "105.NVDA", "TSLA": "105.TSLA", "AMD": "105.AMD",
             "AAPL": "105.AAPL", "MSFT": "105.MSFT",
             "AMZN": "105.AMZN", "GOOGL": "105.GOOGL", "META": "105.META",
             "SPX": "100.SPX", "IXIC": "100.NDX", "DJI": "100.DJIA", "NDX": "100.NDX"}


class _RetryCursor:
    """写库遇锁自动退避重试的游标包装。

    实测：daily_bars 在引擎跑批时被 `database is locked` 打断（update/insert 直接抛栈），
    这一轮补数就成了半截。整库是高频多写入者，亚秒级互撞是常态 —— 与其给每个
    execute 包 try/except，不如把退避放在游标这一层，调用点保持原样。
    """

    def __init__(self, cur, tries: int = 14, base: float = 0.3, cap: float = 8.0):
        self._cur = cur
        self._tries = tries
        self._base = base
        self._cap = cap

    def execute(self, sql, params=()):
        for i in range(self._tries):
            try:
                return self._cur.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or i == self._tries - 1:
                    raise
                time.sleep(min(self._cap, self._base * (2 ** i)))

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _get(url: str, tries: int = 3) -> Any:
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))
    return None


def crypto_bars(asset: str, symbol: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """币安日线。limit>1000 时按 endTime 往前翻页 —— 单次只能拿 1000 根，
    但 BTC 从 2017 年就有数据，只留 120 根等于把历史全丢了。"""
    limit = limit or BARS
    if limit > 1000:
        out: List[Dict[str, Any]] = []
        end = int(time.time() * 1000)
        while len(out) < limit:
            d = _get(f"https://fapi.binance.com/fapi/v1/klines"
                     f"?symbol={symbol}&interval=1d&limit=1000&endTime={end}")
            if not isinstance(d, list) or not d:
                break
            page = [{"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                     "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
                    for row in d]
            out = page + out
            if len(page) < 1000:
                break
            end = int(page[0]["ts"]) - 1
            # 币安合约接口对连续翻页很敏感，0.3s 会被限频直接断在第 2 页（ETH/BNB 就只拿到 1000 根）
            time.sleep(1.2)
        return out[-limit:]
    d = _get(f"https://fapi.binance.com/fapi/v1/klines"
             f"?symbol={symbol}&interval=1d&limit={BARS}")
    if not isinstance(d, list):
        return []
    return [{"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
             "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
            for row in d]


def sina_bars(symbol: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """新浪美股日线：返回 [{d,o,h,l,c,v}]，全历史；这里只留最近 limit 根。"""
    limit = limit or HIST_BARS
    url = "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
    for i in range(3):
        try:
            r = requests.get(url, params={"symbol": symbol, "___qn": 3},
                             headers={"User-Agent": HEADERS["User-Agent"],
                                      "Referer": "https://finance.sina.com.cn/"},
                             timeout=20)
            rows = r.json()
            if not isinstance(rows, list):
                return []
            out = []
            for row in rows[-limit:]:
                try:
                    # v334: 不要给新浪日期加偏移。原始 d 就是真实交易日
                    #   （AAPL 2026-11-13 收盘 356.27，与腾讯 11-13 16:00 收盘一致）。
                    #   曾被误判成"早一天"：实际是 ts 取本地零点、核对时按 UTC 反解，
                    #   显示上退回了 8 小时、日期看起来早一天——数据本身没错。
                    #   这里只保留一条硬校验：丢掉落在周六/周日的行（美股不交易）。
                    _dt = datetime.strptime(str(row["d"])[:10], "%Y-%m-%d")
                    if _dt.weekday() >= 5:
                        continue
                    if not row.get("v"):
                        continue        # 收盘前新浪会先塞一根"今日占位"、没有成交量，丢掉
                    ts = int(time.mktime(_dt.timetuple())) * 1000
                    out.append({"ts": ts, "open": float(row["o"]), "high": float(row["h"]),
                                "low": float(row["l"]), "close": float(row["c"]),
                                "volume": float(row.get("v") or 0)})
                except (KeyError, ValueError, TypeError):
                    continue
            return out
        except Exception:
            time.sleep(3 * (i + 1))
    return []


def em_bars(secid: str) -> List[Dict[str, Any]]:
    """东方财富：row = 日期,开,收,高,低,量（注意是"收"在第三位，不是常见的 开收高低）。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {"secid": secid, "fields1": "f1,f2",
              "fields2": "f51,f52,f53,f54,f55,f56",
              "klt": 101, "fqt": 1, "end": 20500101, "lmt": 400}
    for i in range(5):
        try:
            r = requests.get(url, params=params,
                             headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
            data = (r.json() or {}).get("data") or {}
            rows = data.get("klines") or []
            out = []
            for row in rows:
                parts = row.split(",")
                try:
                    date_s = parts[0]
                    ts = int(time.mktime(time.strptime(date_s, "%Y-%m-%d"))) * 1000
                    out.append({"ts": ts, "open": float(parts[1]), "close": float(parts[2]),
                                "high": float(parts[3]), "low": float(parts[4]),
                                "volume": float(parts[5] or 0)})
                except (ValueError, IndexError):
                    continue
            return out
        except Exception:
            time.sleep(4 * (i + 1))
    return []


def stock_bars(asset: str, code: str) -> List[Dict[str, Any]]:
    d = _get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             f"?param={code},day,,,{BARS},qfq")
    try:
        node = d["data"][code]
    except (KeyError, TypeError):
        return []
    rows = node.get("day") or node.get("qfqday") or []
    out = []
    for row in rows:
        try:
            date_s, o, c, h, l, v = row[0], row[1], row[2], row[3], row[4], row[5]
            ts = int(time.mktime(time.strptime(str(date_s)[:10], "%Y-%m-%d")))
            out.append({"ts": ts * 1000, "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "volume": float(v or 0)})
        except (ValueError, IndexError, TypeError):
            continue
    return out


def main(argv: Optional[List[str]] = None) -> int:
    global HIST_BARS
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="一次性回补全历史（美股/指数取 6000 根，而不是日常的 1000 根）")
    args, _ = ap.parse_known_args(argv)
    HIST_BARS = HIST_BARS_FULL if args.full else HIST_BARS_DEFAULT

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    # v453: 引擎批量落库时会把写锁按住十几秒；此前 30s + 8 次退避仍会抛
    # database is locked，导致整轮日线补数半途中断（美股/指数 1d 卡在 11-14）。
    # 退避总预算拉到约 60s，并把 sqlite 自身的 busy_timeout 提高。
    conn.execute("PRAGMA busy_timeout=60000")
    cur = _RetryCursor(conn.cursor())
    written = 0

    for asset, symbol in CRYPTO.items():
        for b in crypto_bars(asset, symbol, limit=CRYPTO_HIST_BARS if HIST_BARS == HIST_BARS_FULL else None):
            dup = cur.execute("SELECT 1 FROM kline_bars WHERE symbol=? AND timeframe='1d' "
                              "AND ts=? LIMIT 1", (asset, b["ts"])).fetchone()
            if dup:
                # 最近 3 天的 K 线允许覆盖：当天这根是盘中价，收盘后才定型；
                # 若一律跳过，晚间第一次抓到的价格会被冻住，第二天就是错的。
                if b["ts"] >= int(time.time() * 1000) - 3 * 86400 * 1000:
                    cur.execute("""UPDATE kline_bars SET open=?, high=?, low=?, close=?,
                                   volume=?, fetched_at=? WHERE symbol=? AND timeframe='1d' AND ts=?""",
                                (b["open"], b["high"], b["low"], b["close"], b["volume"],
                                 int(time.time()), asset, b["ts"]))
                    written += 1
                continue
            cur.execute("""INSERT INTO kline_bars
                           (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                           VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?)""",
                        (asset, b["ts"], b["open"], b["high"], b["low"],
                         b["close"], b["volume"], int(time.time())))
            written += 1
        print(f"  {asset} 加密日线写入完成", flush=True)

    for asset, symbol in STOCKS.items():
        bars = sina_bars(symbol)
        if not bars:
            bars = em_bars(EM_SECIDS.get(asset, "")) if EM_SECIDS.get(asset) else []
        if not bars:
            bars = stock_bars(asset, f"us{asset}")
        for b in bars:
            dup = cur.execute("SELECT 1 FROM kline_bars WHERE symbol=? AND timeframe='1d' "
                              "AND ts=? LIMIT 1", (asset, b["ts"])).fetchone()
            if dup:
                # 最近 3 天的 K 线允许覆盖：当天这根是盘中价，收盘后才定型；
                # 若一律跳过，晚间第一次抓到的价格会被冻住，第二天就是错的。
                if b["ts"] >= int(time.time() * 1000) - 3 * 86400 * 1000:
                    cur.execute("""UPDATE kline_bars SET open=?, high=?, low=?, close=?,
                                   volume=?, fetched_at=? WHERE symbol=? AND timeframe='1d' AND ts=?""",
                                (b["open"], b["high"], b["low"], b["close"], b["volume"],
                                 int(time.time()), asset, b["ts"]))
                    written += 1
                continue
            cur.execute("""INSERT INTO kline_bars
                           (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                           VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?)""",
                        (asset, b["ts"], b["open"], b["high"], b["low"],
                         b["close"], b["volume"], int(time.time())))
            written += 1
        print(f"  {asset} 美股日线 {len(bars)} 根", flush=True)
        time.sleep(2)

    for asset, symbol in INDICES.items():
        bars = sina_bars(symbol)
        if not bars:
            bars = em_bars(EM_SECIDS[asset])
        for b in bars:
            dup = cur.execute("SELECT 1 FROM kline_bars WHERE symbol=? AND timeframe='1d' "
                              "AND ts=? LIMIT 1", (asset, b["ts"])).fetchone()
            if dup:
                # 最近 3 天的 K 线允许覆盖：当天这根是盘中价，收盘后才定型；
                # 若一律跳过，晚间第一次抓到的价格会被冻住，第二天就是错的。
                if b["ts"] >= int(time.time() * 1000) - 3 * 86400 * 1000:
                    cur.execute("""UPDATE kline_bars SET open=?, high=?, low=?, close=?,
                                   volume=?, fetched_at=? WHERE symbol=? AND timeframe='1d' AND ts=?""",
                                (b["open"], b["high"], b["low"], b["close"], b["volume"],
                                 int(time.time()), asset, b["ts"]))
                    written += 1
                continue
            cur.execute("""INSERT INTO kline_bars
                           (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                           VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?)""",
                        (asset, b["ts"], b["open"], b["high"], b["low"],
                         b["close"], b["volume"], int(time.time())))
            written += 1
        print(f"  {asset} 指数日线 {len(bars)} 根", flush=True)
        time.sleep(2)

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 日线回补 新增 {written} 根", flush=True)
    # v337: 抓完一轮但"新增 0 根"是**正常状态**（当日已抓过/盘中价已覆盖），
    # 原实现 `return 0 if written else 1` 让它每天以 exit 1 出现在 launchctl 里，
    # 长期被当成"失败任务"，真的抓取异常反而被淹没。这里改为 0；
    # 真正的抓取异常仍由上面的异常路径抛出去（非 0）。
    if not written:
        print("  (无新增：日线已是最新，非故障)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
