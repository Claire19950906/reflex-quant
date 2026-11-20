"""期货持仓采集 —— 给黄金和原油补上"真实持仓量"。

为什么需要这一层:
  四象限读法(价涨仓增/价涨仓减/价跌仓增/价跌仓减)必须有持仓量才能算。
  国内加密交易所(Bybit)没有黄金原油合约, CFTC/CME/Yahoo 在这台机器上都不通,
  但**新浪财经的国内期货日线接口是通的**, 而且带 v(成交量) 和 p(持仓量)。

口径说明 (必须记住):
  WTI ← 上海原油期货 SC0 (上海国际能源交易中心), 单位 元/桶
  XAU ← 上海黄金期货 AU0 (上海期货交易所), 单位 元/克
  这两个是国内合约, 不是 COMEX/NYMEX, 但价格与境外品种高度联动;
  持仓量反映的是"国内资金在这两个品种上的仓位", 拿来做四象限的趋势判断够用,
  不能当境外市场的绝对水位。

用法:
  python3 futures_flow.py --fetch --commit     # 采一次, 写成整段历史
  python3 futures_flow.py --report             # 看存了多少、最新读数
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

KLINE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
         "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}")
SYMS = {"WTI": "SC0", "XAU": "AU0"}      # 资产 → 新浪国内期货主力连续
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
           "Referer": "https://finance.sina.com.cn"}


def _get(url: str, tries: int = 4) -> str:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    return ""


def fetch_daily(sym: str) -> list[dict]:
    """返回 [{date, close, volume, oi}] —— oi 是持仓量(手)。"""
    raw = _get(KLINE.format(sym=sym))
    m = re.search(r"\((\[.*?\])\)", raw, re.S) or re.search(r"(\[\{.*?\}\])", raw, re.S)
    if not m:
        return []
    rows = json.loads(m.group(1))
    out = []
    for r in rows:
        try:
            out.append({"date": r["d"],
                        "close": float(r["c"]),
                        "volume": float(r["v"]),
                        "oi": float(r["p"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def ts_of(date_str: str) -> int:
    """日线日期 → 收盘时间戳(当日 15:00 北京时间)。"""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return int(d.replace(hour=15).timestamp())


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS flow_snapshots (
        asset TEXT NOT NULL, kind TEXT NOT NULL,
        ts INTEGER NOT NULL, value REAL NOT NULL,
        source TEXT DEFAULT 'bybit',
        fetched_at INTEGER,
        PRIMARY KEY (asset, kind, ts))""")


def store(conn: sqlite3.Connection, asset: str, kind: str,
          rows: list[tuple[int, float]], source: str) -> int:
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for ts, v in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO flow_snapshots(asset,kind,ts,value,source,fetched_at) "
            "VALUES(?,?,?,?,?,?)", (asset, kind, ts, v, source, now))
        n += cur.rowcount
    conn.commit()
    return n


def report(conn: sqlite3.Connection) -> None:
    for asset in SYMS:
        rows = conn.execute(
            "SELECT kind, count(*), min(ts), max(ts) FROM flow_snapshots "
            "WHERE asset=? AND source LIKE 'sina:%' GROUP BY kind", (asset,)).fetchall()
        if not rows:
            print(f"  {asset}: 还没有期货持仓数据, 先跑 --fetch --commit")
            continue
        for kind, n, mn, mx in rows:
            print(f"  {asset} {kind:7s} {n:5d} 条  "
                  f"{datetime.datetime.fromtimestamp(mn):%Y-%m-%d} → "
                  f"{datetime.datetime.fromtimestamp(mx):%Y-%m-%d}")
        for kind in ("oi", "volume"):
            r = conn.execute(
                "SELECT value, ts FROM flow_snapshots WHERE asset=? AND kind=? AND "
                "source LIKE 'sina:%' ORDER BY ts DESC LIMIT 1", (asset, kind)).fetchone()
            if r:
                print(f"   最新 {kind}: {r[0]:,.0f} @ "
                      f"{datetime.datetime.fromtimestamp(r[1]):%Y-%m-%d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_table(conn)
        if a.fetch:
            for asset, sym in SYMS.items():
                try:
                    rows = fetch_daily(sym)
                except Exception as e:
                    print(f"  {asset}({sym}) 拉取失败: {e}")
                    continue
                if not rows:
                    print(f"  {asset}({sym}) 返回空数据")
                    continue
                oi = [(ts_of(r["date"]), r["oi"]) for r in rows]
                vol = [(ts_of(r["date"]), r["volume"]) for r in rows]
                px = [(ts_of(r["date"]), r["close"]) for r in rows]
                if a.commit:
                    no = store(conn, asset, "oi", oi, f"sina:{sym}")
                    nv = store(conn, asset, "volume", vol, f"sina:{sym}")
                    np_ = store(conn, asset, "close", px, f"sina:{sym}")
                    print(f"  {asset}({sym}) 持仓量 +{no} / 成交量 +{nv} / 收盘价 +{np_} "
                          f"(共 {len(rows)} 个交易日)")
                else:
                    print(f"  {asset}({sym}) 拉到 {len(rows)} 个交易日 (未写库)")
        if a.report:
            report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
