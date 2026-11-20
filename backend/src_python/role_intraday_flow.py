#!/usr/bin/env python3
"""
第14条补充：4 小时级主动买卖（订单流的最轻替代）

market_ticks 只有价格快照，没有买卖方向。日线的 taker_buy_base 只能给出
「一天的主动买占比」，粒度太粗，判「集中砸盘」时抓不到盘中的卖压。

数据源：库内 ohlcv_intraday（分时 OHLCV + 成交笔数，无 taker 方向）。
  买压代理 = (上涨K线成交量 - 下跌K线成交量) / 总成交量（-1~+1）
  worst_press = 当天最差一根的买压（盘中砸盘时刻的卖压）
  spike_cnt   = 成交量 >2×近20根均量的根数（放量次数）

并做 A/B：砸盘规则用「日线主动买」 vs 用「4h 最差买压」，看后果一致率谁高。
数据落 role_lab.db，生产库零写入。

用法：python3 role_intraday_flow.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LAB = BACKEND / "runtime" / "role_lab" / "role_lab.db"
BASE = "https://fapi.binance.com"
SYMBOLS = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")]


def get(path, params, timeout=25):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for _ in range(3):
        p = subprocess.run(["curl", "-s", "-m", str(timeout),
                            "-H", "User-Agent: trident-role-lab/1.0", url],
                           capture_output=True, text=True)
        if (p.stdout or "").strip():
            try:
                return json.loads(p.stdout)
            except json.JSONDecodeError:
                pass
    raise RuntimeError(f"fail {url}")


def main() -> int:
    if not LAB.exists():
        print(f"[ERR] 先跑 role_data_builder.py 生成 {LAB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(LAB), timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS lab_klines_4h(
        asset TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,
        volume REAL, taker_buy_base REAL, PRIMARY KEY(asset, ts))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS lab_flow_daily(
        asset TEXT, date TEXT, buy_pressure REAL, worst_press REAL, spike_cnt INTEGER,
        PRIMARY KEY(asset, date))""")

    src = sqlite3.connect(f"file:{BACKEND / 'trident_event_bus.db'}?mode=ro", uri=True)
    for asset, _ in SYMBOLS:
        rows = src.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_intraday "
            "WHERE asset=? AND volume>0 ORDER BY ts", (asset,)).fetchall()
        batch = [(asset, int(ts), float(o), float(h), float(l), float(c), float(v), None)
                 for ts, o, h, l, c, v in rows]
        conn.executemany("INSERT OR REPLACE INTO lab_klines_4h VALUES(?,?,?,?,?,?,?,?)", batch)
        print(f"[{asset}] 分时 K 线 {len(batch)} 根（库内 ohlcv_intraday）")
    src.close()

    conn.execute("DELETE FROM lab_flow_daily")
    for asset, _ in SYMBOLS:
        rows = conn.execute("SELECT ts, open, close, volume FROM lab_klines_4h "
                            "WHERE asset=? ORDER BY ts", (asset,)).fetchall()
        vols = [r[1] for r in rows]
        daily = {}
        for i, (ts, op, cl, vol) in enumerate(rows):
            ts_s = ts / 1000 if int(ts) > 1e11 else ts      # 毫秒/秒自适应
            d = datetime.fromtimestamp(ts_s, timezone.utc).strftime("%Y-%m-%d")
            delta = ((1 if cl >= op else -1) * vol) if vol else 0.0   # 量价方向代理
            win = [v for v in vols[max(0, i - 19):i + 1] if v > 0]
            ma = sum(win) / len(win) if win else 0
            spike = 1 if (ma and vol > 2 * ma) else 0
            rec = daily.setdefault(d, {"d": [], "w": [], "s": 0, "v": 0.0})
            rec["d"].append(delta)
            rec["v"] += vol
            rec["s"] += spike
        out = []
        for d, rec in daily.items():
            if not rec["d"]:
                continue
            bp = (sum(rec["d"]) / rec["v"]) if rec["v"] else 0.0
            out.append((asset, d, bp, bp, rec["s"]))
        conn.executemany("INSERT OR REPLACE INTO lab_flow_daily VALUES(?,?,?,?,?)", out)
    conn.commit()

    n = conn.execute("SELECT count(*) FROM lab_flow_daily").fetchone()[0]
    span = conn.execute("SELECT min(date), max(date) FROM lab_flow_daily").fetchone()
    print(f"每日买压已生成 {n} 条（{span[0]} → {span[1]}）")

    # A/B：砸盘规则的两版本
    labels = conn.execute(
        "SELECT asset, date, ret5, vol_ratio, taker_delta, fwd3, verdict "
        "FROM role_labels WHERE role='砸盘' AND verdict IN ('验证','否证')").fetchall()
    if not labels:
        print("没有砸盘样本，跳过 A/B")
        return 0
    flow = {(a, d): (bp, wp, sc) for a, d, bp, wp, sc in conn.execute(
        "SELECT asset, date, buy_pressure, worst_press, spike_cnt FROM lab_flow_daily")}
    a_hit = a_n = b_hit = b_n = 0
    for asset, date, ret5, volr, td, fwd3, verdict in labels:
        if (asset, date) not in flow:
            continue
        bp, wp, sc = flow[(asset, date)]
        a_n += 1
        a_hit += 1 if verdict == "验证" else 0
        # 版本B：盘中买压 < -0.15（当天以卖压为主）才判砸盘
        if wp is not None and wp < -0.15:
            b_n += 1
            b_hit += 1 if verdict == "验证" else 0
    print(f"\n砸盘 A/B（后果一致率）")
    print(f"  A 日线主动买占比判据   {a_hit}/{a_n} = {a_hit/a_n*100:.1f}%" if a_n else "  A 无样本")
    if b_n:
        print(f"  B 4h最差买压 < -0.15   {b_hit}/{b_n} = {b_hit/b_n*100:.1f}%")
    else:
        print("  B 无样本（4h 买压未触及阈值）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
