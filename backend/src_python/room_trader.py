"""房间交易员 —— 门卫分好房之后, 房间里谁来看盘、来决定押不押。

为什么要有它:
  门卫(分房)只回答"这条消息跟谁有关", 不回答"该不该押"。现在全市场只有
  一个模型在做判断, 用同一套逻辑看黄金/原油/加密, 结果方向命中率 40%(不如抛硬币)。
  真实交易不是这么做的: 每个品种有自己的资金结构、波动尺度、拥挤程度。

  BTC 房间的交易员 = 一个**可解释的打分器**, 三层判断:
    1. 市场认不认     新闻出来后, 价格是否已经朝这个方向动 (只有认了才跟)
    2. 势对不对       1 小时与 4 小时趋势是否同向
    3. 钱挤不挤       成交量放大=资金进场; 资金费率极端=方向拥挤, 不追
  三条打分 → 总分 ≥ 阈值才下单, 并按分数决定仓位。每个结论都带理由码,
  能逐条回溯, 不是黑箱。

用法:
  python3 room_trader.py --backtest --asset BTC --days 14   # 历史回放, 看它行不行
  python3 room_trader.py --live --asset BTC                  # 当前盘面给建议
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# ── 判断参数 (可解释, 便于回测调) ──────────────────────────────────────────
CONFIRM_WINDOW_MIN = 30        # 看"新闻出来前半小时"市场有没有动
PRICED_IN_ATR = 0.8           # 已走幅度 ≥ 0.8×ATR 视为"跑完了", 不追
VOL_STRONG = 1.5              # 成交量放大到 1.5 倍 = 资金进场
VOL_THIN = 0.7                # 缩量到 0.7 倍 = 没人接
FUNDING_CROWD_PCT = 0.05       # |资金费率| ≥ 0.05% 视为方向拥挤
SCORE_BUY = 0.55              # 下单阈值
SCORE_WATCH = 0.40            # 观察阈值
HOLD_HORIZON_MIN = 240        # 持有 4 小时(与影子账本一致, 便于对比)

# 打分权重
W_CONFIRM = 0.45              # 市场已同向确认 (最重要: 新闻驱动本身无效, 确认才有效)
W_TREND_1H = 0.30
W_TREND_4H = 0.15
W_VOLUME = 0.20
P_CROWDED = 0.30              # 拥挤方向追单的惩罚
P_PRICED_IN = 0.50           # 已经跑完的惩罚


def _klines(conn: sqlite3.Connection, asset: str, until_ms: int,
            minutes: int = 120) -> list[tuple[int, float, float, float]]:
    """取 until 之前 minutes 分钟的 1m K 线: [(ts, close, volume, atr_approx)]."""
    since_ms = until_ms - minutes * 60_000
    rows = conn.execute(
        "SELECT ts, close, volume, high, low FROM kline_bars "
        "WHERE symbol=? AND timeframe='1m' AND ts>=? AND ts<=? ORDER BY ts",
        (asset, since_ms, until_ms)).fetchall()
    return [(int(r[0]), float(r[1]), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0))
            for r in rows]


def market_context(conn: sqlite3.Connection, asset: str, ts_ms: int,
                   funding_pct: float | None = None) -> dict | None:
    """交易员在下单前能看到什么: 价格/涨跌/波动/量能/资金费率。"""
    bars = _klines(conn, asset, ts_ms, minutes=300)
    if len(bars) < 30:
        return None

    def _at(minutes_ago: int) -> float:
        target = ts_ms - minutes_ago * 60_000
        best = min(bars, key=lambda b: abs(b[0] - target))
        return best[1]

    price = bars[-1][1]
    ret_30m = (price - _at(30)) / _at(30) * 100
    ret_1h = (price - _at(60)) / _at(60) * 100
    ret_4h = (price - _at(240)) / _at(240) * 100

    # ATR: 最近 30 根 1m 的平均高低差占价格百分比
    tail = bars[-30:]
    atr_pct = sum((b[3] - b[4]) / b[1] * 100 for b in tail if b[1]) / len(tail)

    # 量能: 最近 30 分钟 vs 前 240 分钟的均量
    recent_vol = sum(b[2] for b in bars[-30:]) / 30
    base_vol = sum(b[2] for b in bars[:-30]) / max(len(bars) - 30, 1)
    vol_ratio = recent_vol / base_vol if base_vol else 1.0

    return {"asset": asset, "ts_ms": ts_ms, "price": price,
            "ret_30m": round(ret_30m, 4), "ret_1h": round(ret_1h, 4),
            "ret_4h": round(ret_4h, 4), "atr_pct": round(atr_pct, 4),
            "vol_ratio": round(vol_ratio, 3), "funding_pct": funding_pct}


def decide(ctx: dict, news_direction: str) -> dict:
    """交易员的核心: 给这个方向打分, 决定押不押、押多少。纯函数, 可测试。

    关键设计 (由数据决定, 不是拍脑袋):
      · 新闻本身给的方向不可信 (实测 40%), 所以**没有市场确认一律不下手**;
      · 市场已经跑完(涨幅 ≥ 0.8×ATR) 不追, 追进去就是接盘;
      · 资金费率极端且与信号同向 = 方向拥挤, 扣分;
      · 成交量放大才说明有真钱进场, 缩量不动。
    """
    direction = news_direction.upper()
    if direction not in ("BUY", "SELL"):
        return {"action": "HOLD", "score": 0.0, "size_pct": 0.0,
                "reasons": ["无方向信号"]}

    sign = 1.0 if direction == "BUY" else -1.0
    score = 0.0
    reasons: list[str] = []

    # ① 市场确认: 前 30 分钟是否已朝这个方向动 (权重大, 因为这是唯一被验证有效的)
    moved = ctx["ret_30m"] * sign
    if moved > 0:
        score += W_CONFIRM
        reasons.append(f"市场已同向动 {abs(ctx['ret_30m']):.2f}% (确认)")
    else:
        reasons.append(f"市场未同向动 ({ctx['ret_30m']:+.2f}%) → 不给分")

    # ② 趋势: 1 小时 / 4 小时
    if ctx["ret_1h"] * sign > 0:
        score += W_TREND_1H
        reasons.append("1h 趋势同向")
    else:
        reasons.append("1h 趋势相反")
    if ctx["ret_4h"] * sign > 0:
        score += W_TREND_4H
        reasons.append("4h 趋势同向")

    # ③ 量能: 真有资金进场吗
    if ctx["vol_ratio"] >= VOL_STRONG:
        score += W_VOLUME
        reasons.append(f"成交量 {ctx['vol_ratio']:.2f}x (资金进场)")
    elif ctx["vol_ratio"] < VOL_THIN:
        score -= W_VOLUME
        reasons.append(f"成交量 {ctx['vol_ratio']:.2f}x (缩量, 没人接)")

    # ④ 拥挤: 资金费率极端且同向 → 方向已经挤满
    funding = ctx.get("funding_pct")
    if funding is not None:
        if abs(funding) >= FUNDING_CROWD_PCT and funding * sign > 0:
            score -= P_CROWDED
            reasons.append(f"资金费率 {funding:+.3f}% 方向拥挤, 不追")
        else:
            reasons.append(f"资金费率 {funding:+.3f}% 正常")

    # ⑤ 已定价: 已经跑完的行情不追 (追进去容易接盘)
    atr = max(ctx["atr_pct"], 1e-6)
    if moved > 0 and abs(ctx["ret_30m"]) >= PRICED_IN_ATR * atr:
        score -= P_PRICED_IN
        reasons.append("短时间内已走 ≥0.8×ATR, 视为已定价, 不追")

    score = max(0.0, min(1.0, score))
    if score >= SCORE_BUY:
        action = direction
        size = round(1.0 + (score - SCORE_BUY) / (1 - SCORE_BUY) * 4.0, 2)  # 1%~5%
    elif score >= SCORE_WATCH:
        action = "WATCH"
        size = 0.0
    else:
        action = "HOLD"
        size = 0.0
    return {"action": action, "score": round(score, 3), "size_pct": size,
            "reasons": reasons}


def _decision_rows(conn: sqlite3.Connection, asset: str, days: int) -> list[dict]:
    """近 N 天分进这个房间的决策 (含门卫规则分房的结果)。"""
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    rows = conn.execute(
        """SELECT d.id, d.ts, d.target_asset, d.ungated_action, d.asset_route
             FROM ai_decisions d
            WHERE d.target_asset = ? AND d.ts >= ?
              AND d.ungated_action IN ('BUY','SELL')
            ORDER BY d.ts""", (asset, since)).fetchall()
    return [{"id": r[0], "ts": r[1], "asset": r[2], "direction": r[3],
             "route": r[4]} for r in rows]


def backtest(conn: sqlite3.Connection, asset: str, days: int) -> None:
    rows = _decision_rows(conn, asset, days)
    if not rows:
        print(f"{asset} 房间近 {days} 天没有方向决策")
        return
    results = []
    for r in rows:
        ts_ms = r["ts"] * 1000
        ctx = market_context(conn, asset, ts_ms)
        if not ctx:
            continue
        d = decide(ctx, r["direction"])
        # 结算: 4 小时后真实涨跌
        fut = _klines(conn, asset, ts_ms + HOLD_HORIZON_MIN * 60_000, minutes=10)
        if not fut:
            continue
        exit_price = fut[-1][1]
        move = (exit_price - ctx["price"]) / ctx["price"] * 100
        results.append({**r, **d, "move_pct": round(move, 4),
                        "hit": 1 if ((d["action"] == "BUY" and move > 0)
                                     or (d["action"] == "SELL" and move < 0)) else 0})

    traded = [x for x in results if x["action"] in ("BUY", "SELL")]
    print(f"\n=== {asset} 房间回放 · 近 {days} 天 ===")
    print(f"进来 {len(results)} 条消息 → 交易员真正下手 {len(traded)} 条 "
          f"({100*len(traded)/max(len(results),1):.1f}%)")
    if traded:
        hit = sum(x["hit"] for x in traded)
        avg = sum(x["move_pct"] for x in traded) / len(traded)
        print(f"下手后的方向命中率 {100*hit/len(traded):.1f}% "
              f"(全部消息基准 {100*sum(x['hit'] for x in results)/len(results):.1f}%)")
        print(f"平均 4h 涨跌 {avg:+.3f}% (正负不重要时看绝对值 "
              f"{sum(abs(x['move_pct']) for x in traded)/len(traded):.2f}%)")
        print("\n最近 5 笔:")
        for x in traded[-5:]:
            print(f"  #{x['id']} {x['direction']} score={x['score']} "
                  f"size={x['size_pct']}% → 实际 {x['move_pct']:+.2f}% "
                  f"{'✓' if x['hit'] else '✗'}")
            print(f"       理由: {'; '.join(x['reasons'][:3])}")


def scan_price_opportunities(conn: sqlite3.Connection, asset: str, days: int,
                             step_min: int = 30) -> list[dict]:
    """盘面驱动扫描: 不等新闻给方向, 自己看"价格已经动 + 放量"的机会。

    为什么要有这条路径 (v335 实测结论):
      新闻驱动的方向判断命中率只有 40%(不如抛硬币), 而且一天只来一两条有方向的消息,
      样本根本不够。反过来, "30 分钟已走 ≥1×ATR 且成交放大 ≥1.5 倍" 这种
      **市场自己表态**的时刻, BTC 近 14 天出现 81 次(5.8 次/天), 4 小时顺势延续
      命中 54.3% —— 既有样本又过 50%。新闻在这里的作用从"发令枪"变成"解释"。

    规则(全部可解释):
      · 30 分钟涨跌幅 ≥ 1×ATR (最近 30 根 1m 的平均高低差)
      · 最近 30 分钟均量 ≥ 前 240 分钟均量的 1.5 倍
      · 方向 = 这半小时的方向 (顺势), 持有 4 小时
    """
    since_ms = (int(datetime.datetime.now().timestamp()) - days * 86400) * 1000
    rows = conn.execute(
        "SELECT ts, close, volume, high, low FROM kline_bars "
        "WHERE symbol=? AND timeframe='1m' AND ts>=? ORDER BY ts",
        (asset, since_ms)).fetchall()
    rows = [(int(r[0]), float(r[1]), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0))
            for r in rows]
    n = len(rows)
    out = []
    for i in range(300, n - HOLD_HORIZON_MIN, step_min):
        win = rows[i - 300:i]
        price = rows[i][1]
        atr = sum((r[3] - r[4]) / r[1] * 100 for r in win[-30:]) / 30
        r30 = (price - rows[i - 30][1]) / rows[i - 30][1] * 100
        vol = sum(r[2] for r in win[-30:]) / 30
        base = sum(r[2] for r in win[:-30]) / max(len(win) - 30, 1)
        vr = vol / base if base else 1.0
        if abs(r30) < atr or vr < VOL_STRONG:
            continue
        direction = "BUY" if r30 > 0 else "SELL"
        fut = rows[min(i + HOLD_HORIZON_MIN, n - 1)][1]
        move = (fut - price) / price * 100
        out.append({
            "ts": rows[i][0], "asset": asset, "direction": direction,
            "price": price, "ret_30m": round(r30, 4), "atr_pct": round(atr, 4),
            "vol_ratio": round(vr, 3), "move_pct": round(move, 4),
            "hit": 1 if ((direction == "BUY") == (move > 0)) else 0,
        })
    return out


def backtest_price(conn: sqlite3.Connection, asset: str, days: int) -> None:
    """盘面驱动模式的回放: 有几次机会、命中率多少、平均盈亏多少。"""
    items = scan_price_opportunities(conn, asset, days)
    print(f"\n=== {asset} 房间 · 盘面驱动回放 · 近 {days} 天 ===")
    if not items:
        print("没有符合条件的机会")
        return
    hit = sum(i["hit"] for i in items)
    avg = sum(i["move_pct"] for i in items) / len(items)
    print(f"机会 {len(items)} 次 ({len(items)/days:.1f} 次/天) · "
          f"4 小时顺势命中率 {100*hit/len(items):.1f}% · 平均 {avg:+.3f}%")
    print("(命中率 >50% 才说明这套规则有优势; 平均涨跌为正 = 顺势方向真能赚到)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--mode", default="news", choices=["news", "price"])
    ap.add_argument("--step", type=int, default=30)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if a.backtest and a.mode == "news":
            backtest(conn, a.asset, a.days)
        elif a.backtest and a.mode == "price":
            backtest_price(conn, a.asset, a.days)
        else:
            ap.print_help()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
