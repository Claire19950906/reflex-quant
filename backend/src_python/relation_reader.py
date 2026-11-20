"""关系识别 —— 把"价格 / 持仓量 / 成交量 / 多空比"放在一起读, 而不是单看一个。

为什么必须这样读 (Claire 指出的要点):
  同一个"价格上涨", 背后可能是两种完全相反的事:
    新多头进场 (钱在加) → 趋势还会继续
    空头被迫平仓 (钱在撤) → 涨势快到头了
  只看价格分不出来, 必须配上持仓量(OI)和成交量。这也是交易里最老的一张表:

             持仓量增加              持仓量减少
    价格涨   新多进场 · 趋势健康       空头平仓 · 涨势虚, 将尽
    价格跌   新空进场 · 下跌健康       多头离场 · 跌势尾声, 可能反弹

  再加一层"成交量": 放量=有人真在换手, 缩量=只是少数人在动。
  再加一层"多空账户比": 散户站哪边, 决定谁是最后的接盘者。

用法:
  python3 relation_reader.py --read --asset BTC --hours 24
  python3 relation_reader.py --corr --days 14           # 五个品种的相关性
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
ASSETS = ("BTC", "ETH", "SOL", "XAU", "WTI")


# ── 四象限表 ───────────────────────────────────────────────────────────────
QUADRANT = {
    (True, True):  ("价涨·仓增", "新多头进场, 趋势健康", "健康上涨"),
    (True, False): ("价涨·仓减", "空头被迫平仓推动, 涨势虚, 快到头", "空头回补"),
    (False, True): ("价跌·仓增", "新空头进场, 下跌健康", "健康下跌"),
    (False, False): ("价跌·仓减", "多头认输离场, 跌势尾声, 易反弹", "多头离场"),
}


def _price_at(conn: sqlite3.Connection, asset: str, ts: int) -> float | None:
    r = conn.execute("SELECT price FROM market_ticks WHERE symbol=? AND ts<=? "
                     "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    return float(r[0]) if r else None


SINA_ASSETS = ("XAU", "WTI")     # 这两个品种的持仓量来自新浪国内期货, 不是 Bybit


def _flow_at(conn: sqlite3.Connection, asset: str, kind: str, ts: int) -> float | None:
    """XAU/WTI 走新浪国内期货 (真实交易所持仓), 加密三兄弟走 Bybit。"""
    if asset in SINA_ASSETS:
        r = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind=? "
                         "AND source LIKE 'sina:%' AND ts<=? ORDER BY ts DESC LIMIT 1",
                         (asset, kind, ts)).fetchone()
        if r:
            return float(r[0])
    r = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind=? "
                     "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, kind, ts)).fetchone()
    return float(r[0]) if r else None


def _volume_stats(conn: sqlite3.Connection, asset: str, ts: int,
                  hours: int) -> tuple[float | None, float | None]:
    """(窗口内均量, 之前同时长均量) —— 用 1m K 线, 数据缺失时返回 None。"""
    t0, t1 = ts - hours * 3600, ts
    cur = conn.execute("SELECT avg(volume) FROM kline_bars WHERE symbol=? AND "
                       "timeframe='1m' AND ts>=? AND ts<=?",
                       (asset, t0 * 1000, t1 * 1000)).fetchone()
    prev = conn.execute("SELECT avg(volume) FROM kline_bars WHERE symbol=? AND "
                        "timeframe='1m' AND ts>=? AND ts<=?",
                        (asset, (t0 - hours * 3600) * 1000, t0 * 1000)).fetchone()
    return (cur[0] if cur and cur[0] else None, prev[0] if prev and prev[0] else None)


def read(conn: sqlite3.Connection, asset: str, ts: int | None = None,
         hours: int = 24) -> dict:
    """读一个品种在窗口内的价格/持仓/量/多空比关系。"""
    ts = ts or int(datetime.datetime.now().timestamp())
    t0 = ts - hours * 3600
    p0, p1 = _price_at(conn, asset, t0), _price_at(conn, asset, ts)
    o0, o1 = _flow_at(conn, asset, "oi", t0), _flow_at(conn, asset, "oi", ts)
    v_cur, v_prev = _volume_stats(conn, asset, ts, hours)
    ls = _flow_at(conn, asset, "lsratio", ts)
    fund = _flow_at(conn, asset, "funding", ts)

    out: dict = {"asset": asset, "hours": hours}
    if not (p0 and p1 and p0):
        return {**out, "error": "行情不足"}
    price_chg = (p1 - p0) / p0 * 100
    oi_chg = ((o1 - o0) / o0 * 100) if (o0 and o1 and o0) else None
    vol_chg = ((v_cur - v_prev) / v_prev * 100) if (v_cur and v_prev) else None

    if oi_chg is None:
        quad, meaning, tag = ("持仓数据不足", "只能看价和量", "数据缺口")
    else:
        quad, meaning, tag = QUADRANT[(price_chg > 0, oi_chg > 0)]

    notes = [f"{tag}: {meaning}"]
    if asset in SINA_ASSETS:
        notes.append("持仓量来自国内期货 (WTI←上海原油SC0 / XAU←沪金AU0), "
                     "真实交易所口径, 但别当 COMEX 绝对水位")
    if vol_chg is not None:
        if vol_chg > 30:
            notes.append(f"成交量放大 {vol_chg:+.0f}%, 有人真在换手 → 结论更可信")
        elif vol_chg < -30:
            notes.append(f"成交量萎缩 {vol_chg:+.0f}%, 只是少数人在动 → 结论打折")
    if ls is not None:
        if price_chg < 0 and ls > 0.65:
            notes.append(f"跌的同时 {ls*100:.0f}% 账户在做多 → 多头还在接刀, 跌势未完")
        if price_chg > 0 and ls > 0.65:
            notes.append(f"涨的同时 {ls*100:.0f}% 账户在做多 → 散户追高, 缺大户接力")
    if fund is not None and price_chg > 0 and fund > 0.0001:
        notes.append("涨且多头付费, 追多成本高")
    if fund is not None and price_chg < 0 and fund < -0.0001:
        notes.append("跌且空头付费, 做空拥挤")

    return {**out, "price_chg": round(price_chg, 2),
            "oi_chg": None if oi_chg is None else round(oi_chg, 2),
            "vol_chg": None if vol_chg is None else round(vol_chg, 0),
            "quadrant": quad, "meaning": meaning, "notes": notes,
            "funding": fund, "lsratio": ls}


def correlation(conn: sqlite3.Connection, days: int = 14,
                step_h: int = 4) -> dict[tuple[str, str], float]:
    """五个品种 4 小时涨跌的相关系数 —— 谁跟着谁, 谁对冲谁。"""
    step = step_h * 3600
    now = int(datetime.datetime.now().timestamp())
    series: dict[str, list[float]] = {}
    for a in ASSETS:
        vals = []
        for t in range(now - days * 86400, now, step):
            p0, p1 = _price_at(conn, a, t), _price_at(conn, a, t + step)
            if p0 and p1 and p0:
                vals.append((p1 - p0) / p0 * 100)
        series[a] = vals
    n = min(len(v) for v in series.values()) if series else 0
    series = {a: v[-n:] for a, v in series.items()}

    def corr(x: list[float], y: list[float]) -> float:
        if len(x) < 3:
            return 0.0
        mx, my = sum(x) / len(x), sum(y) / len(y)
        num = sum((i - mx) * (j - my) for i, j in zip(x, y))
        dx = sum((i - mx) ** 2 for i in x) ** 0.5
        dy = sum((j - my) ** 2 for j in y) ** 0.5
        return num / (dx * dy) if dx and dy else 0.0

    return {(a, b): round(corr(series[a], series[b]), 2)
            for a in ASSETS for b in ASSETS if a < b}


def _sina_series(conn: sqlite3.Connection, asset: str, kind: str,
                 src: str = "sina:%") -> dict[int, float]:
    return {ts: v for ts, v in conn.execute(
        "SELECT ts, value FROM flow_snapshots WHERE asset=? AND kind=? AND source LIKE ?",
        (asset, kind, src)).fetchall()}


def _trend(series: dict[int, float], ts: int, days: int = 20) -> float | None:
    """距 ts 往前 days 天的涨跌幅, 用来判断"环境在往哪边变"。"""
    past = [t for t in series if t <= ts - days * 86400]
    cur = [t for t in series if t <= ts]
    if not past or not cur:
        return None
    v0, v1 = series[max(past)], series[max(cur)]
    return (v1 - v0) / v0 * 100 if v0 else None


def gold_edge(conn: sqlite3.Connection) -> None:
    """黄金「价涨·持仓大减(>5%)」信号 × 宏观环境 的切分验证。

    宏观两把尺子 (都用 ETF 价格方向代替, 因为指数接口不可用):
      UUP 涨 = 美元走强;  TIP 涨 = 实际利率下行。
    """
    cl = _sina_series(conn, "XAU", "close")
    oi = _sina_series(conn, "XAU", "oi")
    uup = _sina_series(conn, "UUP", "close", "sina:us")
    tip = _sina_series(conn, "TIP", "close", "sina:us")
    ts = sorted(set(cl) & set(oi))
    rows = []
    for i in range(1, len(ts) - 3):
        p0, p1 = cl[ts[i - 1]], cl[ts[i]]
        o0, o1 = oi[ts[i - 1]], oi[ts[i]]
        if not (p0 and o0):
            continue
        pc = (p1 - p0) / p0 * 100
        oc = (o1 - o0) / o0 * 100
        if abs(oc) > 20:                     # 主力换月的持仓跳变, 剔除
            continue
        rows.append({"ts": ts[i], "up": pc > 0, "oi": oc, "big": abs(oc) > 5,
                     "fwd": (cl[ts[i + 3]] - cl[ts[i]]) / cl[ts[i]] * 100,
                     "uup": _trend(uup, ts[i]), "tip": _trend(tip, ts[i])})

    def stat(sel: list[float]) -> str:
        if not sel:
            return "n=  0"
        return (f"n={len(sel):4d}  后3日 {sum(sel)/len(sel):+6.2f}%  "
                f"胜率 {sum(1 for x in sel if x > 0)/len(sel)*100:5.1f}%")

    print("黄金「价涨 · 持仓大减(>5%)」信号 —— 按宏观环境切分")
    print(f"  {'全部样本基准':26s} {stat([r['fwd'] for r in rows])}")
    sig = [r for r in rows if r["up"] and r["big"] and r["oi"] < 0]
    print(f"  {'信号(不筛环境)':26s} {stat([r['fwd'] for r in sig])}")
    def easy(r: dict) -> bool:      # 美元走弱 + 实际利率下行
        return (r["uup"] is not None and r["tip"] is not None
                and r["uup"] < 0 and r["tip"] > 0)

    def tight(r: dict) -> bool:     # 美元走强 + 实际利率上行
        return (r["uup"] is not None and r["tip"] is not None
                and r["uup"] > 0 and r["tip"] < 0)

    def mixed(r: dict) -> bool:     # 两边打架或数据缺失
        return not (easy(r) or tight(r))

    for name, cond in (("美元走弱 + 实际利率下行", easy),
                       ("美元走强 + 实际利率上行", tight),
                       ("混合(其它)", mixed)):
        print(f"  {name:26s} {stat([r['fwd'] for r in sig if cond(r)])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", action="store_true")
    ap.add_argument("--corr", action="store_true")
    ap.add_argument("--gold-edge", action="store_true")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if a.read or not a.corr:
            assets = ASSETS if a.read and a.asset == "ALL" else (a.asset,)
            for asset in assets:
                r = read(conn, asset, hours=a.hours)
                print(f"\n=== {asset} 价·仓·量关系 · 近 {a.hours} 小时 ===")
                if r.get("error"):
                    print("  ", r["error"])
                    continue
                print(f"  价格 {r['price_chg']:+.2f}% | 持仓量 "
                      f"{'n/a' if r['oi_chg'] is None else format(r['oi_chg'], '+.2f') + '%'} | "
                      f"成交量 {'n/a' if r['vol_chg'] is None else format(r['vol_chg'], '+.0f') + '%'}")
                print(f"  组合: {r['quadrant']} → {r['meaning']}")
                for note in r["notes"][1:]:
                    print(f"  · {note}")
        if a.gold_edge:
            gold_edge(conn)
        if a.corr:
            print(f"\n=== 五品种 4h 涨跌相关性 · 近 {a.days} 天 ===")
            c = correlation(conn, a.days)
            pairs = sorted(c.items(), key=lambda kv: -abs(kv[1]))
            for (a1, a2), v in pairs[:8]:
                tag = ("同向联动" if v > 0.5 else "弱相关" if abs(v) <= 0.5
                       else "反向对冲")
                print(f"  {a1}-{a2}: {v:+.2f}  {tag}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
