"""市场脉搏 —— 在决定下单前, 先看清"现在市场是什么状态、大家在赌什么"。

为什么要有它 (Claire 指出的两点):
  1) 系统读 K 线太粗糙: 只有"上涨/下跌/震荡", 看不出趋势衰减、超买超卖、量能背离。
  2) 没用上盘面情绪: 跨资产联动(风险偏好)、资金费率、持仓量、多空账户比、挂单 ——
     这些才是"市场共识与投资心理", 单单预期落差不够。

判读维度 (全部可解释, 每个都输出读数与含义):
  结构   均线排列、价格位置、20/50 均线斜率 —— 趋势是刚起还是快结束
  动能   RSI、1h/4h/1d 涨跌 —— 有没有背离、是否超买超卖
  波动   日内波动相对自身常态是放大还是收缩 —— 事件更容易跑出幅度还是被压回
  情绪   资金费率、多空账户比、持仓量变化 —— 是钱在进场, 还是已经挤满了
  联动   加密三兄弟 vs 黄金的 4h 相对强弱 —— 风险偏好开还是关

产出 `bull_score` / `bear_score` (0~100) 与一句人话总结, 供交易员决定"这个方向值不值得押"。
"""
from __future__ import annotations
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import sqlite3

ASSETS = ("BTC", "ETH", "SOL", "XAU", "WTI")
CRYPTO = ("BTC", "ETH", "SOL")


def _series(conn: sqlite3.Connection, asset: str, ts: int, hours: int = 72,
            step: int = 30) -> list[float]:
    """取 ts 之前 hours 小时的价, 按 step 分钟抽成一条均匀序列。"""
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts<=? AND ts>=? "
        "ORDER BY ts", (asset, ts, ts - hours * 3600)).fetchall()
    buckets: dict[int, float] = {}
    for t, p in rows:
        if p:
            buckets[int(t) // (step * 60)] = float(p)
    keys = sorted(buckets)
    return [buckets[k] for k in keys]


def _ma(xs: list[float], n: int) -> float:
    seg = xs[-n:] if len(xs) >= n else xs
    return sum(seg) / len(seg) if seg else 0.0


def _rsi(xs: list[float], n: int = 14) -> float:
    if len(xs) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        d = xs[i] - xs[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _flow(conn: sqlite3.Connection, asset: str, kind: str, ts: int,
          lookback_h: int = 24) -> tuple[float | None, float | None]:
    """返回 (当前值, lookback 小时前的值)。"""
    cur = conn.execute(
        "SELECT ts, value FROM flow_snapshots WHERE asset=? AND kind=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, kind, ts)).fetchone()
    old = conn.execute(
        "SELECT ts, value FROM flow_snapshots WHERE asset=? AND kind=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, kind, ts - lookback_h * 3600)).fetchone()
    return (float(cur[1]) if cur else None, float(old[1]) if old else None)


def long_pain(conn: sqlite3.Connection, asset: str, ts: int, days: int = 7) -> dict:
    """多头扛单压力 —— 跌了但多头不跑, 还在加杠杆, 这是强平踩踏的前夜。

    四个分量 (都有实测依据, 见 CHANGELOG v335):
      跌幅      价格从 days 天前跌了多少 (跌 11% → 55 分)
      费率      这段时间多头一直付钱给空头 (付 0.145% → 14.5 分)
      增仓      持仓量没降反而升 = 越跌越加 (1.43% → 4.3 分)
      加人      做多账户占比还在升 = 散户越跌越买 (升 2.76pp → 8.3 分)
    合计 0~100: >60 危险, >80 强平前夜。
    """
    since = ts - days * 86400
    p0 = conn.execute("SELECT price FROM market_ticks WHERE symbol=? AND ts<=? "
                      "ORDER BY ts DESC LIMIT 1", (asset, since)).fetchone()
    p1 = conn.execute("SELECT price FROM market_ticks WHERE symbol=? AND ts<=? "
                      "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    o0 = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind='oi' "
                      "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, since)).fetchone()
    o1 = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind='oi' "
                      "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    l0 = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind='lsratio' "
                      "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, since)).fetchone()
    l1 = conn.execute("SELECT value FROM flow_snapshots WHERE asset=? AND kind='lsratio' "
                      "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    fsum = conn.execute("SELECT sum(value) FROM flow_snapshots WHERE asset=? AND "
                        "kind='funding' AND ts>? AND ts<=?", (asset, since, ts)).fetchone()[0]
    if not (p0 and p1 and p0[0]):
        return {"score": 0, "parts": {}, "note": "数据不足"}
    drop = (p1[0] - p0[0]) / p0[0] * 100
    oi_chg = ((o1[0] - o0[0]) / o0[0] * 100) if (o0 and o1 and o0[0]) else 0.0
    ls_delta = ((l1[0] - l0[0]) * 100) if (l0 and l1) else 0.0
    fee = (fsum or 0) * 100
    parts = {"跌幅": max(0.0, -drop) * 5, "费率": max(0.0, fee) * 100,
             "增仓": max(0.0, oi_chg) * 3, "加人": max(0.0, ls_delta) * 3}
    score = min(100, sum(parts.values()))
    if score >= 80:
        note = "强平前夜: 多头深亏且还在加仓, 一次急跌会引发连环爆仓"
    elif score >= 60:
        note = "多头扛单明显: 反弹容易被打回, 追多性价比差"
    elif score >= 35:
        note = "多头有浮亏但在消化"
    else:
        note = "多头压力正常"
    return {"score": round(score), "parts": {k: round(v, 1) for k, v in parts.items()},
            "drop": round(drop, 2), "oi_chg": round(oi_chg, 2),
            "ls_delta": round(ls_delta, 2), "fee": round(fee, 3), "note": note}


def pulse(conn: sqlite3.Connection, asset: str, ts: int) -> dict:
    """给出此刻的多空两方面评分与一句人话。"""
    px = _series(conn, asset, ts)
    if len(px) < 30:
        return {"bull": 0, "bear": 0, "label": "数据不足", "text": "行情不够, 不判",
                "detail": {}, "veto": True}

    price = px[-1]
    ma20, ma50 = _ma(px, 40), _ma(px, 100)     # 30 分钟一根 → 约 20 小时 / 50 小时
    prev_ma20 = _ma(px[:-20], 40) if len(px) > 60 else ma20
    rsi = _rsi(px)
    ret_1h = (price - px[-2]) / px[-2] * 100 if len(px) > 2 else 0.0
    ret_4h = (price - px[-8]) / px[-8] * 100 if len(px) > 9 else 0.0
    ret_1d = (price - px[-48]) / px[-48] * 100 if len(px) > 49 else 0.0
    # 波动: 近 24 小时的 30 分钟波动 vs 近 3 天常态
    recent = [abs(px[i] - px[i - 1]) / px[i - 1] * 100 for i in range(-48, 0) if px[i - 1]]
    base = [abs(px[i] - px[i - 1]) / px[i - 1] * 100 for i in range(1, len(px)) if px[i - 1]]
    vol_ratio = (sum(recent) / len(recent)) / (sum(base) / len(base)) if base and recent else 1.0

    funding, funding_old = _flow(conn, asset, "funding", ts, 24)
    oi, oi_old = _flow(conn, asset, "oi", ts, 8)
    ls, ls_old = _flow(conn, asset, "lsratio", ts, 24)

    # 跨资产: 加密三兄弟 4h 平均 vs 黄金 4h → 风险偏好
    crypto_r, gold_r = [], None
    for a in CRYPTO:
        s = _series(conn, a, ts, hours=8)
        if len(s) > 9:
            crypto_r.append((s[-1] - s[-8]) / s[-8] * 100)
    g = _series(conn, "XAU", ts, hours=8)
    if len(g) > 9:
        gold_r = (g[-1] - g[-8]) / g[-8] * 100
    risk_on = (sum(crypto_r) / len(crypto_r) - gold_r) if crypto_r and gold_r is not None else 0.0

    bull = bear = 0.0
    d: dict[str, str] = {}

    # ① 结构
    if price > ma20 > ma50 and ma20 > prev_ma20:
        bull += 25
        d["结构"] = "均线多头排列且在抬升"
    elif price < ma20 < ma50 and ma20 < prev_ma20:
        bear += 25
        d["结构"] = "均线空头排列且在走低"
    elif price > ma20:
        bull += 10
        d["结构"] = "价格在 20 均线之上, 但排列未成形"
    else:
        bear += 10
        d["结构"] = "价格在 20 均线之下"

    # ② 动能 + 超买超卖
    if ret_1h > 0 and ret_4h > 0:
        bull += 20
        d["动能"] = f"1h {ret_1h:+.2f}% / 4h {ret_4h:+.2f}% 同向上行"
    elif ret_1h < 0 and ret_4h < 0:
        bear += 20
        d["动能"] = f"1h {ret_1h:+.2f}% / 4h {ret_4h:+.2f}% 同向下行"
    else:
        d["动能"] = f"1h 与 4h 打架 ({ret_1h:+.2f}% / {ret_4h:+.2f}%)"
    if rsi >= 72:
        bear += 12
        d["超买超卖"] = f"RSI {rsi:.0f} 超买, 上冲容易衰竭"
    elif rsi <= 28:
        bull += 12
        d["超买超卖"] = f"RSI {rsi:.0f} 超卖, 反弹概率高"
    else:
        d["超买超卖"] = f"RSI {rsi:.0f} 中性"

    # ③ 波动
    d["波动"] = f"近期波动是常态的 {vol_ratio:.2f} 倍"
    if vol_ratio > 1.5:
        d["波动"] += " (放大, 事件更容易跑出幅度)"
    elif vol_ratio < 0.6:
        d["波动"] += " (收缩, 消息常被压回)"

    # ④ 情绪: 资金费率
    if funding is not None:
        if funding > 0.0001:
            bear += 15
            d["资金费率"] = f"{funding*100:+.3f}% 多头拥挤, 追多性价比差"
        elif funding < -0.0001:
            bull += 15
            d["资金费率"] = f"{funding*100:+.3f}% 空头拥挤, 反弹弹药充足"
        else:
            d["资金费率"] = f"{funding*100:+.3f}% 正常"

    # ⑤ 情绪: 多空账户比
    if ls is not None:
        if ls >= 0.65:
            bear += 12
            d["多空比"] = f"{ls*100:.0f}% 账户在做多, 散户一边倒"
        elif ls <= 0.38:
            bull += 12
            d["多空比"] = f"{ls*100:.0f}% 账户在做多, 空头偏多"
        else:
            d["多空比"] = f"{ls*100:.0f}% 做多账户, 正常"

    # ⑥ 情绪: 持仓量变化 (8h)
    if oi and oi_old and oi_old > 0:
        oi_chg = (oi - oi_old) / oi_old * 100
        if oi_chg > 2 and ret_4h > 0:
            bull += 10
            d["持仓量"] = f"8h {oi_chg:+.1f}% 增仓上涨, 新钱进场"
        elif oi_chg > 2 and ret_4h < 0:
            bear += 10
            d["持仓量"] = f"8h {oi_chg:+.1f}% 增仓下跌, 空头加仓"
        elif oi_chg < -2:
            d["持仓量"] = f"8h {oi_chg:+.1f}% 减仓, 资金在撤"
        else:
            d["持仓量"] = f"8h {oi_chg:+.1f}% 平稳"

    # ⑦ 联动: 风险偏好
    if abs(risk_on) > 0.3:
        if risk_on > 0:
            bull += 10
            d["联动"] = f"加密强于黄金 {risk_on:+.2f}%, 风险偏好开"
        else:
            bear += 10
            d["联动"] = f"加密弱于黄金 {risk_on:+.2f}%, 避险模式"
    else:
        d["联动"] = f"加密与黄金基本同步 ({risk_on:+.2f}%)"

    # 价·仓·量 关系 (四象限): 同样涨跌, 配不同持仓变化, 含义完全相反
    try:
        from relation_reader import read as rel_read
        rel = rel_read(conn, asset, ts, hours=24)
        d["价仓关系"] = f"{rel.get('quadrant','?')} → {rel.get('meaning','')}"
        if rel.get("quadrant") in ("价涨·仓减", "价跌·仓减"):
            d["价仓关系"] += " (资金在撤, 结论打折)"
    except Exception:
        pass

    pain = long_pain(conn, asset, ts)
    d["多头压力"] = f"{pain['score']}/100 {pain['note']}"
    if pain["score"] >= 60:
        bear += 12
    elif pain["score"] >= 40:
        bear += 6

    bull, bear = min(100, bull), min(100, bear)
    net = bull - bear
    if net >= 30:
        label = "多头结构"
    elif net <= -30:
        label = "空头结构"
    else:
        label = "多空僵持"
    if abs(net) >= 20 and vol_ratio > 1.5:
        label += "·放量"
    text = (f"{label}: 结构[{d.get('结构','')}], 动能[{d.get('动能','')}], "
            f"情绪[资金{d.get('资金费率','')} / 多空{d.get('多空比','')} / "
            f"{d.get('持仓量','')}], 联动[{d.get('联动','')}]")
    return {"bull": round(bull), "bear": round(bear), "net": round(net),
            "label": label, "text": text, "detail": d,
            "pain": pain['score'], "pain_note": pain['note'],
            "rsi": round(rsi), "vol_ratio": round(vol_ratio, 2),
            "risk_on": round(risk_on, 2), "veto": False}


def explain(conn: sqlite3.Connection, asset: str, ts: int | None = None) -> None:
    """命令行用: 打印某一时刻的完整脉搏。"""
    import datetime
    ts = ts or int(datetime.datetime.now().timestamp())
    p = pulse(conn, asset, ts)
    print(f"\n=== {asset} 市场脉搏 @ "
          f"{datetime.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M} ===")
    print(f"多分 {p['bull']} / 空分 {p['bear']}  →  {p['label']}")
    for k, v in p["detail"].items():
        print(f"  {k}: {v}")
    print(f"\n一句话: {p['text']}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--ts", type=int, default=0)
    a = ap.parse_args()
    from pathlib import Path
    conn = sqlite3.connect(f"file:{BACKEND_DIR / 'trident_event_bus.db'}"
                           f"?mode=ro", uri=True)
    try:
        explain(conn, a.asset, a.ts or None)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
