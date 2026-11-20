"""资金流仪表 —— 记录"钱往哪走"的读数, 供房间交易员使用。

为什么需要它 (v335 实测):
  交易员只看价格/成交量/趋势, 都是**结果**。BTC 房间 19 天 145 次"追已动价格"的机会,
  扣掉 0.08% 手续费后收益归零 —— 因为没有任何"别人还没吃完"的信息。
  真正的领先信息是资金结构:
    · 资金费率 (funding): 多头拥挤 = 大家都在押涨, 费率被抬得很高, 往往是反抽前夜
    · 持仓量 (open interest, OI): 钱是新开仓进场, 还是平仓跑路
    · 订单簿深度: 上方有厚卖墙还是空无一物

数据源: Binance 在本机被墙, 改用 Bybit 公共 API (已验证可用)。
  · funding 历史: 8 小时一笔, 200 笔 ≈ 66 天
  · OI 历史: 1 小时一笔, 200 笔 ≈ 8 天

用法:
  python3 market_flow.py --fetch --commit            # 拉取并落库
  python3 market_flow.py --report                    # 看库里有多少、当前读数
  python3 market_flow.py --test-funding --days 20    # 资金费率极值能否反着做
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import datetime
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB_PATH = BACKEND_DIR / "trident_event_bus.db"
BYBIT = "https://api.bybit.com"

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

# 费率极值阈值 (Bybit 返回的是小数, 0.0001 = 0.01%)
FUNDING_CROWD = 0.0001      # |费率| ≥ 0.01% 视为方向拥挤


def _get(path: str, params: dict, retries: int = 4) -> dict:
    """Bybit 偶尔会 SSL 中途断开, 做几次退避重试再说失败。"""
    import time
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BYBIT}{path}?{qs}"
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("bybit fetch failed")


def fetch_funding(symbol: str, limit: int = 200) -> list[tuple[int, float]]:
    """8 小时一笔的资金费率历史 → [(ts_sec, rate)]。"""
    d = _get("/v5/market/funding/history",
             {"category": "linear", "symbol": symbol, "limit": limit})
    lst = (d.get("result") or {}).get("list") or []
    return [(int(x["fundingRateTimestamp"]) // 1000, float(x["fundingRate"])) for x in lst]


def fetch_ls_ratio(symbol: str, interval: str = "1h", limit: int = 200) -> list[tuple[int, float]]:
    """多空账户比 (long/short ratio) → [(ts_sec, buyRatio)]。

    Bybit 的 account-ratio 是"做多账户占比", 0.72 意味七成账户在做多 —— 典型的情绪拥挤读数。
    """
    d = _get("/v5/market/account-ratio",
             {"category": "linear", "symbol": symbol,
              "period": interval, "limit": limit})
    lst = (d.get("result") or {}).get("list") or []
    return [(int(x["timestamp"]) // 1000, float(x["buyRatio"])) for x in lst]


def fetch_oi(symbol: str, interval: str = "1h", limit: int = 200) -> list[tuple[int, float]]:
    """持仓量历史 → [(ts_sec, oi_coin)]。"""
    d = _get("/v5/market/open-interest",
             {"category": "linear", "symbol": symbol,
              "intervalTime": interval, "limit": limit})
    lst = (d.get("result") or {}).get("list") or []
    return [(int(x["timestamp"]) // 1000, float(x["openInterest"])) for x in lst]


GOLD_PROXY = ("XAUTUSDT", "PAXGUSDT")   # 代币化黄金, 1 枚≈1 盎司, 给 XAU 补持仓量


def fetch_gold_oi(interval: str = "1h", limit: int = 200) -> list[tuple[int, float]]:
    """Bybit 没有 XAUUSDT 合约, 用代币化黄金 XAUT+PAXG 的持仓量相加当代理。

    注意: 这反映的是"在加密交易所里交易黄金"的那批人, 规模和 COMEX 期货
    差好几个数量级, 只能看趋势方向, 不能当绝对水位。字段已在 CHANGELOG 标注。
    """
    agg: dict[int, float] = {}
    for sym in GOLD_PROXY:
        try:
            for ts, v in fetch_oi(sym, interval, limit):
                agg[ts] = agg.get(ts, 0.0) + v
        except Exception:
            continue
    return sorted(agg.items())


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS flow_snapshots (
        asset TEXT NOT NULL, kind TEXT NOT NULL,      -- funding / oi
        ts INTEGER NOT NULL, value REAL NOT NULL,
        source TEXT DEFAULT 'bybit',
        fetched_at INTEGER,
        PRIMARY KEY (asset, kind, ts))""")
    conn.commit()


def store(conn: sqlite3.Connection, asset: str, kind: str, rows: list[tuple[int, float]]) -> int:
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for ts, v in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO flow_snapshots(asset,kind,ts,value,source,fetched_at) "
            "VALUES(?,?,?,?,?,?)", (asset, kind, ts, v, "bybit", now))
        n += cur.rowcount
    conn.commit()
    return n


def report(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            "SELECT asset, kind, count(*), min(ts), max(ts) FROM flow_snapshots "
            "GROUP BY asset, kind ORDER BY asset").fetchall()
    except sqlite3.OperationalError:
        print("还没有 flow_snapshots 表, 先跑 --fetch --commit")
        return
    if not rows:
        print("库里 0 条资金流读数 —— 交易员现在看不到任何资金面")
        return
    print("已有资金流读数:")
    for asset, kind, n, mn, mx in rows:
        print(f"  {asset} {kind:7s} {n:4d} 条  "
              f"{datetime.datetime.fromtimestamp(mn):%m-%d %H:%M} → "
              f"{datetime.datetime.fromtimestamp(mx):%m-%d %H:%M}")
    print("\n当前读数:")
    for asset in SYMBOLS:
        r = conn.execute(
            "SELECT value, ts FROM flow_snapshots WHERE asset=? AND kind='funding' "
            "ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        o = conn.execute(
            "SELECT value, ts FROM flow_snapshots WHERE asset=? AND kind='oi' "
            "ORDER BY ts DESC LIMIT 1", (asset,)).fetchone()
        if r:
            tag = ("多头拥挤" if r[0] > FUNDING_CROWD
                   else "空头拥挤" if r[0] < -FUNDING_CROWD else "正常")
            print(f"  {asset}: 资金费率 {r[0]*100:+.4f}% ({tag})  "
                  f"OI {o[0]:,.0f}" if o else f"  {asset}: 资金费率 {r[0]*100:+.4f}% ({tag})")


def _kline(conn: sqlite3.Connection, asset: str, ts_ms: int) -> float | None:
    r = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1m' "
        "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, ts_ms)).fetchone()
    return float(r[0]) if r else None


def test_funding(conn: sqlite3.Connection, days: int) -> None:
    """资金费率极值 → 反着做, 8 小时后结算。看这个信号有没有优势。

    逻辑: 费率被抬得很高 = 多头排队拥挤, 后面容易反抽 → 反向做空;
          费率为大负 = 空头拥挤 → 反向做多。
    """
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    print(f"\n=== 资金费率极值反向测试 · 近 {days} 天 ===")
    print(f"{'品种':6s} {'信号':10s} {'次数':>4s} {'8h后命中':>8s} {'平均收益':>9s} {'扣费后':>8s}")
    for asset in SYMBOLS:
        rows = conn.execute(
            "SELECT ts, value FROM flow_snapshots WHERE asset=? AND kind='funding' "
            "AND ts>=? ORDER BY ts", (asset, since)).fetchall()
        for label, cond in (("多头拥挤", lambda v: v > FUNDING_CROWD),
                            ("空头拥挤", lambda v: v < -FUNDING_CROWD)):
            hits = tot = 0
            pnl = 0.0
            for ts, v in rows:
                if not cond(v):
                    continue
                p0 = _kline(conn, asset, ts * 1000)
                p1 = _kline(conn, asset, (ts + 8 * 3600) * 1000)
                if not p0 or not p1 or p0 == 0:
                    continue
                sign = -1.0 if v > 0 else 1.0     # 反着做
                mv = (p1 - p0) / p0 * 100 * sign
                tot += 1
                pnl += mv
                hits += 1 if mv > 0 else 0
            if tot:
                print(f"{asset:6s} {label:10s} {tot:4d} {100*hits/tot:7.1f}% "
                      f"{pnl/tot:+8.3f}% {pnl/tot-0.08:+7.3f}%")
            else:
                print(f"{asset:6s} {label:10s} {tot:4d}   (与 K 线不重叠, 无可用样本)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--test-funding", action="store_true")
    ap.add_argument("--days", type=int, default=20)
    a = ap.parse_args()
    conn = sqlite3.connect(DB_PATH)
    try:
        if a.fetch:
            ensure_table(conn)
            for asset, sym in SYMBOLS.items():
                try:
                    f = fetch_funding(sym)
                    o = fetch_oi(sym)
                    l = fetch_ls_ratio(sym)
                except Exception as e:
                    print(f"  {asset} 拉取失败: {e}")
                    continue
                if a.commit:
                    nf = store(conn, asset, "funding", f)
                    no = store(conn, asset, "oi", o)
                    nl = store(conn, asset, "lsratio", l)
                    print(f"  {asset} 新增 funding {nf} / OI {no} / 多空比 {nl} 条")
                else:
                    print(f"  {asset} 拉到 funding {len(f)} 条 / OI {len(o)} 条 (未写库)")
            # XAU 用代币化黄金的持仓量做代理 (Bybit 无 XAUUSDT 合约)
            try:
                g = fetch_gold_oi()
            except Exception as e:
                print(f"  XAU 代理持仓拉取失败: {e}")
                g = []
            if g:
                if a.commit:
                    ng = store(conn, "XAU", "oi", g)
                    print(f"  XAU 新增 OI(代理 XAUT+PAXG) {ng} 条")
                else:
                    print(f"  XAU 拉到代理 OI {len(g)} 条 (未写库)")
        if a.report:
            report(conn)
        if a.test_funding:
            test_funding(conn, a.days)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
