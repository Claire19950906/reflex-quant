"""price_trigger_backtest.py — 只读回测：价格触发信号到底有没有用。

不写主库、不改任何阈值。用途：验证"某品种在 30 分钟内涨跌超过 X%"
这种纯价格触发，在 5 个品种上历史上能不能稳定过 50%。
"""
import sqlite3
import time
import statistics
import sys
sys.path.insert(0, ".")
import config

DB = config.DB_PATH
SYMBOLS = ("BTC", "ETH", "SOL", "WTI", "XAU")
WINDOW = 1800          # 触发窗口 30 分钟
THRESH = float(sys.argv[1]) if len(sys.argv)>1 else 0.005   # 触发阈值
FWD = {"1h": 3600, "4h": 4 * 3600, "8h": 8 * 3600}


def load(sym):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND price>0 ORDER BY ts", (sym,)
    ).fetchall()
    c.close()
    return [(int(t), float(p)) for t, p in rows if t and p]


def series_forward(ticks, i, sec):
    """取 i 之后 sec 秒内第一笔价（不允许回退）。"""
    t0 = ticks[i][0]
    target = t0 + sec
    lo, hi = i + 1, len(ticks) - 1
    if lo > hi:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if ticks[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid
    if ticks[lo][0] - t0 > sec * 2:      # 抽搐太远不算
        return None
    return ticks[lo][1]


def run():
    print(f"触发: {WINDOW//60} 分钟内涨跌 ≥ {THRESH*100:.1f}%   前向: {', '.join(FWD)}\n")
    for sym in SYMBOLS:
        ticks = load(sym)
        if len(ticks) < 500:
            print(f"{sym}: 数据不足 ({len(ticks)})")
            continue
        events = []
        j = 0
        for i in range(len(ticks)):
            # 找窗口起点：i 之前 WINDOW 秒内的第一笔
            while j + 1 < i and ticks[j + 1][0] < ticks[i][0] - WINDOW:
                j += 1
            base = ticks[j][1]
            if base <= 0:
                continue
            move = (ticks[i][1] - base) / base
            if abs(move) < THRESH:
                continue
            # 冷却：同一方向 30 分钟内不重复触发
            if events and ticks[i][0] - events[-1]["ts"] < WINDOW and \
               (move > 0) == events[-1]["up"]:
                continue
            events.append({"ts": ticks[i][0], "idx": i, "up": move > 0, "move": move,
                          "price": ticks[i][1]})
        print(f"■ {sym}: {len(ticks):,} 笔报价, 触发 {len(events)} 次")
        for label, sec in FWD.items():
            hit = tot = 0
            rets = []
            for e in events:
                fwd = series_forward(ticks, e["idx"], sec)
                if fwd is None:
                    continue
                r = (fwd - e["price"]) / e["price"]
                tot += 1
                rets.append(r * 100 if e["up"] else -r * 100)   # 顺触发方向
                if (r > 0) == e["up"]:
                    hit += 1
            if tot:
                print(f"    {label}: 顺向命中 {hit}/{tot} = {hit/tot*100:.1f}%  "
                      f"平均收益 {statistics.mean(rets):+.2f}%")
            else:
                print(f"    {label}: 无样本")


if __name__ == "__main__":
    run()
