#!/usr/bin/env python3
"""
主力行为 / 角色识别 —— 数据补齐与标签落库

补齐 --role 检测项缺的五样东西：
  1 逐笔方向  → 用交易所日线的 taker_buy_base（主动买成交量）算「主动买卖占比」，替代逐笔
  2 历史长度  → 日线拉满 ~1500 天，资金费率分页拉满 24 个月
  3 角色标签  → 落 role_labels 表（每日一行，含证据与后果验证）
  4 资产级 OI → openInterestHist 按品种（交易所只给最近 30 天，此为该接口硬上限）
  5 人工抽检  → --review N 导出 N 条到 CSV，供人工核对（铁律7）

数据落在独立库，不动生产库：
  runtime/role_lab/role_lab.db

用法：
  python3 role_data_builder.py                 # 采集 + 打标签
  python3 role_data_builder.py --label-only     # 只重算标签（不联网）
  python3 role_data_builder.py --review 20      # 导出 20 条人工抽检清单
  python3 role_data_builder.py --to-main        # 把 role_labels 同步进主库 market_role_labels

判标签的规矩（写死，改即换尺）：
  吸筹 accumulation : |5日收益|<3%  + 量<0.8×20日均量 + 费率分位<30% + 主动买占比>50%
  派发 distribution : 5日收益≥3%   + 量<0.8×20日均量 + 费率分位>70% + 主动买占比<50%
  砸盘 dump         : 单日≤-2%     + 量≥1.5×20日均量 + 主动买占比<50%（卖压占优）
  去杠杆 deleverage : 费率分位>80%
后果验证（进场一律推到次日收盘，铁律2）：
  吸筹 → 10日后收益>0；派发 → 10日后<0；砸盘 → 3日后<0；去杠杆 → 3日后<0
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from db_location import DB_PATH
import random
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"
LAB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "role_lab")
LAB_DB = os.path.abspath(os.path.join(LAB_DIR, "role_lab.db"))
# v486：统一走 db_location（支持 TRIDENT_DB_PATH 覆盖）
MAIN_DB = str(DB_PATH)

# 品种 → 交易所合约。PAXGUSDT 作为黄金代理（无黄金期货合约）。
SYMBOLS = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("XAU", "PAXGUSDT")]


def _get(path: str, params: dict, timeout: int = 20, retries: int = 3):
    """走 curl 而不是 urllib：本机 Python 直连交易所会 SSL UNEXPECTED_EOF，curl 稳定。"""
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    last = ""
    for _ in range(retries):
        p = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-H", "User-Agent: trident-role-lab/1.0", url],
            capture_output=True, text=True)
        txt = (p.stdout or "").strip()
        if txt:
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                last = txt[:200]
        else:
            last = (p.stderr or "")[:200]
    raise RuntimeError(f"请求失败 {url} :: {last}")


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS lab_klines(
        asset TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, taker_buy_base REAL, quote_volume REAL, trades INTEGER,
        source TEXT, fetched_at TEXT,
        PRIMARY KEY(asset, date));
    CREATE TABLE IF NOT EXISTS lab_funding(
        asset TEXT, ts INTEGER, rate REAL, fetched_at TEXT,
        PRIMARY KEY(asset, ts));
    CREATE TABLE IF NOT EXISTS lab_oi(
        asset TEXT, date TEXT, oi REAL, oi_value REAL, fetched_at TEXT,
        PRIMARY KEY(asset, date));
    CREATE TABLE IF NOT EXISTS role_labels(
        asset TEXT, date TEXT, role TEXT, evidence TEXT,
        ret5 REAL, vol_ratio REAL, funding_rank REAL, taker_delta REAL,
        entry_date TEXT, fwd3 REAL, fwd10 REAL,
        verdict TEXT, checked_at TEXT,
        PRIMARY KEY(asset, date, role));
    """)


def fetch_klines(conn, asset, symbol, days=1500):
    rows = _get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": days})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for r in rows:
        d = datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        out.append((asset, d, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                    float(r[5]), float(r[9]), float(r[7]), int(r[8]), f"binance:{symbol}", now))
    conn.executemany("INSERT OR REPLACE INTO lab_klines VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", out)
    conn.commit()
    return len(out)


def fetch_funding(conn, asset, symbol, months=24):
    start = int(time.time() * 1000) - months * 30 * 86400 * 1000
    total = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    while True:
        rows = _get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": start, "limit": 1000})
        if not rows:
            break
        batch = [(asset, int(r["fundingTime"]), float(r["fundingRate"]), now) for r in rows]
        conn.executemany("INSERT OR REPLACE INTO lab_funding VALUES(?,?,?,?)", batch)
        conn.commit()
        total += len(batch)
        start = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    return total


def fetch_oi(conn, asset, symbol):
    try:
        rows = _get("/futures/data/openInterestHist",
                    {"symbol": symbol, "period": "1d", "limit": 500})
    except Exception as e:
        print(f"  [OI] {symbol} 取不到：{e}")
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    batch = []
    for r in rows:
        d = datetime.fromtimestamp(int(r["timestamp"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        batch.append((asset, d, float(r["sumOpenInterest"]), float(r["sumOpenInterestValue"]), now))
    conn.executemany("INSERT OR REPLACE INTO lab_oi VALUES(?,?,?,?,?)", batch)
    conn.commit()
    return len(batch)


def _pct_rank(vals, i, lookback=60):
    win = [v for v in vals[max(0, i - lookback + 1):i + 1] if v is not None]
    if len(win) < 10 or vals[i] is None:
        return None
    below = sum(1 for v in win if v < vals[i])
    return below / (len(win) - 1)


def build_labels(conn, asset):
    days = [r[0] for r in conn.execute(
        "SELECT date FROM lab_klines WHERE asset=? ORDER BY date", (asset,))]
    if len(days) < 40:
        return 0
    bars = {r[0]: r for r in conn.execute(
        "SELECT date, open, high, low, close, volume, taker_buy_base FROM lab_klines "
        "WHERE asset=? ORDER BY date", (asset,))}
    closes = [float(bars[d][4]) for d in days]
    vols = [float(bars[d][5]) for d in days]
    takers = [float(bars[d][6]) for d in days]

    # 资金费率按日聚合（8h 三点取均值）
    raw = conn.execute("SELECT ts, rate FROM lab_funding WHERE asset=? ORDER BY ts", (asset,)).fetchall()
    by_day = {}
    for ts, rate in raw:
        d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(float(rate))
    funding = [(sum(by_day[d]) / len(by_day[d]) if d in by_day else None) for d in days]

    vol_ma = []
    for i in range(len(vols)):
        w = [v for v in vols[max(0, i - 19):i + 1] if v > 0]
        vol_ma.append(sum(w) / len(w) if w else 0.0)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for i in range(20, len(days) - 1):
        r5 = (closes[i] / closes[i - 5] - 1) if closes[i - 5] else 0.0
        r1 = (closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0
        fq = _pct_rank(funding, i)
        vratio = (vols[i] / vol_ma[i]) if vol_ma[i] else 0.0
        tdelta = (2 * takers[i] / vols[i] - 1) if vols[i] else None   # 主动买占比偏离 50% 的幅度
        e = i + 1
        if e + 1 >= len(days):
            continue
        entry = closes[e]
        fwd3 = (closes[e + 3] / entry - 1) if e + 3 < len(days) else None
        fwd10 = (closes[e + 10] / entry - 1) if e + 10 < len(days) else None

        roles = []
        if abs(r5) < 0.03 and vratio < 0.8 and fq is not None and fq < 0.30 and (tdelta or 0) > 0:
            roles.append("吸筹")
        if r5 >= 0.03 and vratio < 0.8 and fq is not None and fq > 0.70 and (tdelta or 0) < 0:
            roles.append("派发")
        if r1 <= -0.02 and vratio >= 1.5 and (tdelta is None or tdelta < 0):
            roles.append("砸盘")
        if fq is not None and fq > 0.80:
            roles.append("去杠杆")

        for role in roles:
            if role == "吸筹":
                target, fwd = fwd10, fwd10
            elif role == "派发":
                target, fwd = fwd10, fwd10
            elif role == "砸盘":
                target, fwd = fwd3, fwd3
            else:
                target, fwd = fwd3, fwd3
            if fwd is None:
                verdict = "待验"
            else:
                ok = (fwd > 0) if role in ("吸筹",) else (fwd < 0)
                verdict = "验证" if ok else "否证"
            ev = (f"r5={r5:+.2%} volx={vratio:.2f} fq={'-' if fq is None else f'{fq:.2f}'} "
                  f"taker={'-' if tdelta is None else f'{tdelta:+.2f}'}")
            out.append((asset, days[i], role, ev, r5, vratio, fq, tdelta,
                        days[e], fwd3, fwd10, verdict, now))
    conn.executemany("INSERT OR REPLACE INTO role_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    conn.commit()
    return len(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label-only", action="store_true")
    ap.add_argument("--review", type=int, default=0)
    ap.add_argument("--to-main", action="store_true")
    args = ap.parse_args()

    os.makedirs(LAB_DIR, exist_ok=True)
    conn = sqlite3.connect(LAB_DB)
    ensure_schema(conn)

    if args.review:
        rows = conn.execute("SELECT asset,date,role,evidence,entry_date,fwd3,fwd10,verdict "
                            "FROM role_labels WHERE verdict!='待验'").fetchall()
        if not rows:
            print("没有可抽检的标签（先跑采集与打标）")
            return
        sample = random.sample(rows, min(args.review, len(rows)))
        path = os.path.join(LAB_DIR, "review_todo.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["品种", "日期", "系统判定角色", "证据", "次日进场日",
                        "3日收益", "10日收益", "系统结论", "人工核对(填:对/错/说不清)", "人工备注"])
            def pct(x):
                return f"{x * 100:+.2f}%" if x is not None else ""
            for r in sample:
                w.writerow([r[0], r[1], r[2], r[3], r[4], pct(r[5]), pct(r[6]),
                            r[7], "", ""])
        print(f"抽检清单已导出（{len(sample)} 条）：{path}")
        print("规则（铁律7）：一致率 ≥85% 可用 / 70–85% 须标注不确定性 / <70% 失效")
        return

    if args.to_main:
        if not os.path.exists(MAIN_DB):
            print(f"主库不存在：{MAIN_DB}")
            return
        dst = sqlite3.connect(MAIN_DB)
        dst.execute("""CREATE TABLE IF NOT EXISTS market_role_labels(
            asset TEXT, date TEXT, role TEXT, evidence TEXT, fwd3 REAL, fwd10 REAL,
            verdict TEXT, created_at TEXT, PRIMARY KEY(asset,date,role))""")
        rows = conn.execute("SELECT asset,date,role,evidence,fwd3,fwd10,verdict,checked_at "
                            "FROM role_labels").fetchall()
        dst.executemany("INSERT OR REPLACE INTO market_role_labels VALUES(?,?,?,?,?,?,?,?)", rows)
        dst.commit()
        print(f"已同步 {len(rows)} 条到主库 market_role_labels")
        return

    if not args.label_only:
        for asset, symbol in SYMBOLS:
            # 单源容错：K 线/费率/OI 任一失败只记日志，不中断该品种其余采集，
            # 也不影响其它品种。旧数据保留在 lab_* 表，等下一轮补采。
            n = f = o = 0
            try:
                n = fetch_klines(conn, asset, symbol)
            except Exception as e:
                print(f"  [K线] {asset}/{symbol} 采集失败（保留旧数据）：{e}")
            try:
                f = fetch_funding(conn, asset, symbol)
            except Exception as e:
                print(f"  [费率] {asset}/{symbol} 采集失败（保留旧数据）：{e}")
            try:
                o = fetch_oi(conn, asset, symbol)
            except Exception as e:
                print(f"  [OI] {asset}/{symbol} 采集失败（保留旧数据）：{e}")
            span = conn.execute("SELECT min(date), max(date) FROM lab_klines WHERE asset=?",
                                (asset,)).fetchone()
            print(f"[{asset}] 日线 {n} 根（{span[0]}→{span[1]}） 资金费率 {f} 点 资产级OI {o} 天")

    total = 0
    for asset, _ in SYMBOLS:
        total += build_labels(conn, asset)
    print(f"\n角色标签已落库：{total} 条 → {LAB_DB}")
    for role, n, v in conn.execute(
            "SELECT role, count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
            "FROM role_labels GROUP BY role").fetchall():
        acc = f"{v / n * 100:.1f}%" if n else "—"
        print(f"  {role:<6} n={n:<5} 验证={v:<5} 后果一致率={acc}")


if __name__ == "__main__":
    main()
