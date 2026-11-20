"""宏观环境采集 —— 给黄金信号加"宏观底色"。

为什么用 ETF 而不是指数:
  美元指数(DXY)和美债实际收益率, 直连接口(CFTC/Yahoo/东财/FX 历史)在这台机器上
  全都不可用; 但新浪的美股日线接口是通的, 而这两件事都有现成的 ETF 载体:

  UUP  = Invesco 美元指数看涨基金   → 涨 = 美元走强
  TIP  = iShares 抗通胀债券 ETF     → 涨 = 实际利率下行 (通胀保值债更值钱)
  TLT  = iShares 20 年期美债 ETF    → 涨 = 长端利率下行

  价格方向比收益率数字更好用: 我们要判断的是"环境在往哪边变", 不是利率的绝对值。

用法:
  python3 macro_flow.py --fetch --commit
  python3 macro_flow.py --report
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

USDAILY = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
           "var%20t=/US_MinKService.getDailyK?symbol={sym}")
SYMS = {"UUP": "美元指数(UUP)", "TIP": "实际利率反向(TIP)", "TLT": "长债(TLT)"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
           "Referer": "https://finance.sina.com.cn"}


def _get(url: str, tries: int = 4) -> str:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    return ""


def fetch_daily(sym: str) -> list[dict]:
    raw = _get(USDAILY.format(sym=sym))
    m = re.search(r"\((\[.*?\])\)", raw, re.S) or re.search(r"(\[\{.*?\}\])", raw, re.S)
    if not m:
        return []
    out = []
    for r in json.loads(m.group(1)):
        try:
            out.append({"date": r["d"], "close": float(r["c"]), "volume": float(r["v"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def ts_of(date_str: str) -> int:
    """美股收盘 → 北京时间次日凌晨 4:00, 这里统一取当日 16:00 纽约=次日 05:00 北京。"""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return int(d.replace(hour=21).timestamp())      # 简化: 当日 21:00 北京时间


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS flow_snapshots (
        asset TEXT NOT NULL, kind TEXT NOT NULL,
        ts INTEGER NOT NULL, value REAL NOT NULL,
        source TEXT DEFAULT 'bybit', fetched_at INTEGER,
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
    for sym, label in SYMS.items():
        rows = conn.execute(
            "SELECT count(*), min(ts), max(ts) FROM flow_snapshots WHERE asset=? AND "
            "kind='close' AND source LIKE 'sina:us'", (sym,)).fetchone()
        if not rows or not rows[0]:
            print(f"  {sym} ({label}): 还没有数据")
            continue
        n, mn, mx = rows
        cur = conn.execute("SELECT value, ts FROM flow_snapshots WHERE asset=? AND "
                           "kind='close' AND source LIKE 'sina:us' ORDER BY ts DESC LIMIT 1",
                           (sym,)).fetchone()
        chg = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind='close' "
                           "AND source LIKE 'sina:us' AND ts<=? ORDER BY ts DESC LIMIT 1",
                           (sym, cur[1] - 20 * 86400)).fetchone()
        chg20 = f"{(cur[0]-chg[0])/chg[0]*100:+.1f}% (20日)" if chg and chg[0] else "n/a"
        print(f"  {sym} ({label}): {n} 条  {datetime.datetime.fromtimestamp(mn):%Y-%m-%d} → "
              f"{datetime.datetime.fromtimestamp(mx):%Y-%m-%d}  最新 {cur[0]:.2f} {chg20}")


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
            for sym in SYMS:
                try:
                    rows = fetch_daily(sym)
                except Exception as e:
                    print(f"  {sym} 拉取失败: {e}")
                    continue
                if not rows:
                    print(f"  {sym} 返回空")
                    continue
                px = [(ts_of(r["date"]), r["close"]) for r in rows]
                if a.commit:
                    n = store(conn, sym, "close", px, "sina:us")
                    print(f"  {sym} 收盘价 +{n} 条 (共 {len(rows)} 个交易日)")
                else:
                    print(f"  {sym} 拉到 {len(rows)} 个交易日 (未写库)")
        if a.report:
            report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
