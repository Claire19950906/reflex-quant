"""盘口记录 —— 每小时存一份"谁在挂单挡着、墙有多厚"。

为什么需要:
  价格只是结果。盘口才是"现在就有人摆在那里的意愿":
    · 买卖盘不平衡 → 短期谁占上风
    · 单档巨量挂单(墙) → 某个价位有人硬挡, 或有人摆假墙诱多/诱空
    · 价差 → 流动性好坏, 太差时滑点会吃掉利润

数据来源: Bybit 公共接口 /v5/market/orderbook (category=linear, limit=50)。
  只能实时取, 没有历史 —— 所以从今天开始每小时存一份, 攒够才能回测。

强平数据说明 (诚实):
  Bybit 已不再公开强平历史接口, Binance 在本机被墙。
  因此"爆仓"只能用代理指标推:
    价格短时间急跌 + 持仓量同时骤降 = 多头被强平
    价格急涨 + 持仓量骤降 = 空头被强平
  这个代理在 market_pulse 里由 持仓量变化 与 4h 涨跌 组合判断。

用法:
  python3 depth_watch.py --fetch --commit     # 抓一份存库
  python3 depth_watch.py --report            # 看当前盘口与墙
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
WALL_MULT = 6.0     # 单档挂单量 > 平均的 6 倍, 视为"墙"


def snapshot(symbol: str, limit: int = 50) -> dict | None:
    from market.market_flow import _get
    d = _get("/v5/market/orderbook",
             {"category": "linear", "symbol": symbol, "limit": limit})
    r = (d.get("result") or {})
    bids, asks = r.get("b") or [], r.get("a") or []
    if not bids or not asks:
        return None
    bid_px = [float(x[0]) for x in bids]
    bid_qty = [float(x[1]) for x in bids]
    ask_px = [float(x[0]) for x in asks]
    ask_qty = [float(x[1]) for x in asks]
    best_bid, best_ask = bid_px[0], ask_px[0]
    mid = (best_bid + best_ask) / 2
    bid_vol, ask_vol = sum(bid_qty), sum(ask_qty)
    avg_bid = bid_vol / len(bid_qty)
    avg_ask = ask_vol / len(ask_qty)
    # 1% 范围内的挂单量 —— 判断"近处有没有人挡"
    near_bid = sum(q for p, q in zip(bid_px, bid_qty) if p >= mid * 0.99)
    near_ask = sum(q for p, q in zip(ask_px, ask_qty) if p <= mid * 1.01)
    return {
        "best_bid": best_bid, "best_ask": best_ask,
        "spread_bp": round((best_ask - best_bid) / mid * 10000, 2),
        "bid_vol": round(bid_vol, 2), "ask_vol": round(ask_vol, 2),
        "imbalance": round(bid_vol / (bid_vol + ask_vol), 3) if bid_vol + ask_vol else 0.5,
        "near_bid": round(near_bid, 2), "near_ask": round(near_ask, 2),
        "wall_bid": round(max(bid_qty), 2),
        "wall_ask": round(max(ask_qty), 2),
        "wall_bid_px": round(bid_px[bid_qty.index(max(bid_qty))], 2),
        "wall_ask_px": round(ask_px[ask_qty.index(max(ask_qty))], 2),
        "wall_bid_is_wall": max(bid_qty) > WALL_MULT * avg_bid,
        "wall_ask_is_wall": max(ask_qty) > WALL_MULT * avg_ask,
    }


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS depth_snapshots (
        asset TEXT, ts INTEGER,
        best_bid REAL, best_ask REAL, spread_bp REAL,
        bid_vol REAL, ask_vol REAL, imbalance REAL,
        near_bid REAL, near_ask REAL,
        wall_bid REAL, wall_ask REAL, wall_bid_px REAL, wall_ask_px REAL,
        fetched_at INTEGER,
        PRIMARY KEY (asset, ts))""")
    conn.commit()


def store(conn: sqlite3.Connection, asset: str, snap: dict) -> int:
    now = int(datetime.datetime.now().timestamp())
    cur = conn.execute(
        "INSERT OR IGNORE INTO depth_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset, now, snap["best_bid"], snap["best_ask"], snap["spread_bp"],
         snap["bid_vol"], snap["ask_vol"], snap["imbalance"],
         snap["near_bid"], snap["near_ask"], snap["wall_bid"], snap["wall_ask"],
         snap["wall_bid_px"], snap["wall_ask_px"], now))
    conn.commit()
    return cur.rowcount


def report(conn: sqlite3.Connection) -> None:
    print(f"{'品种':5s} {'买盘总量':>10s} {'卖盘总量':>10s} {'买方占比':>9s} "
          f"{'价差(bp)':>9s} {'近1%买/卖':>16s} 墙")
    for asset in SYMBOLS:
        try:
            s = snapshot(SYMBOLS[asset])
        except Exception:
            print(f"{asset:5s}  取不到盘口 (网络抖动)")
            continue
        if not s:
            print(f"{asset:5s}  返回空盘口")
            continue
        wall = []
        if s["wall_bid_is_wall"]:
            wall.append(f"买单墙 {s['wall_bid']:.1f}@{s['wall_bid_px']}")
        if s["wall_ask_is_wall"]:
            wall.append(f"卖单墙 {s['wall_ask']:.1f}@{s['wall_ask_px']}")
        print(f"{asset:5s} {s['bid_vol']:10.1f} {s['ask_vol']:10.1f} "
              f"{s['imbalance']*100:8.1f}% {s['spread_bp']:9.2f} "
              f"{s['near_bid']:7.1f}/{s['near_ask']:<7.1f} "
              f"{' / '.join(wall) if wall else '无(挂单分散)'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)  # v337: 避开 engine 写锁
        try:
            ensure_table(conn)
            for asset, sym in SYMBOLS.items():
                try:
                    s = snapshot(sym)
                except Exception as e:
                    print(f"  {asset} 取盘口失败: {e}")
                    continue
                if not s:
                    print(f"  {asset} 返回空盘口")
                    continue
                if a.commit:
                    # v337: 这里以前**只打印"已存盘口", 从未调用 store()** ——
                    # 于是 39 次小时级调度全部空转, depth_snapshots 一直是 0 行,
                    # 验收 #14 也因此判"订单簿不可用"。现在真正落库。
                    _rows = store(conn, asset, s)
                    print(f"  {asset} 已存盘口 (失衡 {s['imbalance']*100:.1f}% "
                          f"价差 {s['spread_bp']}bp, rows={_rows})")
        finally:
            conn.close()
    if a.report:
        conn2 = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            report(conn2)
        finally:
            conn2.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
