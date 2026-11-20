"""事件预案交易员 —— 只做"预期 vs 实际"这种确定性信息, 先判盘面状态再决定怎么做。

设计思路 (Claire 的框架):
  1. 先看盘面处于什么状态: 上涨/下跌/震荡/高波动, 有没有资金拥挤 (技术指标 + 资金费率)
  2. 再等数据公布, 按"实际值 vs 预期值"分档:
       · 超预期 (beat)        → 通胀/就业强 = 鹰派 = 利空黄金/比特币; 库存多 = 利空油价
       · 低于预期 (miss)      → 反过来
       · 符合预期 (inline)    → 不交易, 因为没有新的信息
       · 延迟发布 / 未发布    → 不交易, 等确认 (延迟本身常是二次冲击源)
  3. 同样的"超预期", 在上涨趋势里和下跌趋势里做法不同:
       状态与方向同向 → 可持仓 24h; 逆向 → 只做 1~4h 或不做
  核心信念: 预期的落差是确定的、可量化的, 所以命中率应该明显高于"读新闻猜方向"(40%)。

用法:
  python3 event_playbook.py --events --days 40        # 解析出的事件清单
  python3 event_playbook.py --backtest --days 40      # 预案回测(按状态×意外方向分格)
"""
from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# 脉搏闸门: 事件方向必须被盘面情绪支持, 否则不做
PULSE_MIN_SUPPORT = 35          # 2026-11-14 定档: 35~40 才是最稳的区间, 低于 35 不做

ENTRY_DELAY_MIN = 15
FEE_ROUND_TRIP = 0.08
DEDUP_SEC = 6 * 3600

# ── 指标 → (类别, 受影响的品种, 强于预期时对品种的方向) ────────────────────
# 逻辑: 美国通胀/就业/消费强 = 鹰派(加息预期升) = 美元强 → 黄金/比特币受压
#       库存多 = 供给过剩 → 油价受压; 库存少 = 利多油价
INDICATORS: list[tuple[str, str, dict[str, str]]] = [
    (r"CPI|消费者物价", "通胀", {"XAU": "SELL", "BTC": "SELL"}),
    (r"PPI|生产者物价", "通胀", {"XAU": "SELL", "BTC": "SELL"}),
    (r"PCE|核心物价", "通胀", {"XAU": "SELL", "BTC": "SELL"}),
    (r"非农", "就业", {"XAU": "SELL", "BTC": "SELL"}),
    (r"初请失业金|续请失业金", "就业", {"XAU": "SELL", "BTC": "SELL"}),
    (r"失业率", "就业反向", {"XAU": "BUY", "BTC": "BUY"}),
    (r"零售销售", "消费", {"XAU": "SELL", "BTC": "SELL"}),
    (r"GDP", "增长", {"XAU": "SELL", "BTC": "BUY"}),
    (r"EIA.*(原油|石油|库存)|原油库存", "原油库存", {"WTI": "SELL"}),
    (r"EIA.*天然气|天然气库存", "天然气库存", {}),
    (r"钻井|钻机", "原油供给", {"WTI": "BUY"}),
    (r"ISM|PMI", "景气", {"XAU": "SELL"}),
    (r"消费者信心|密歇根", "信心", {"XAU": "SELL"}),
]

DELAY_RE = re.compile(r"推迟|延迟|延后|改期|未公布|暂缓")
US_RE = re.compile(r"美国|美\b|美金|美元|U\.S\.|US\b|美联储|Fed")
NUM = r"(-?[0-9]+(?:\.[0-9]+)?)"


def clean(h: str) -> str:
    h = re.sub(r"\[hash:[^\]]*\]", "", h or "")
    h = re.sub(r"\[来自[^\]]*\]", "", h)
    return h.strip()


def parse_release(text: str) -> dict | None:
    """从标题里抠出 实际/预期/前值 和指标名。

    库里两种常见格式:
      [金十公布] 美国至11月6日当周EIA原油库存(万桶) 实际 210 预测 -150 前值 -80
      美国10月CPI年率 2.6%，预期2.4%，前值2.5%
    坑: "11月6日" 的 11 会被当成实际值 —— 所以数字必须带单位/百分号，
        或者直接用"实际/预测"标签形式。
    """
    a = re.search(r"实际(?:值)?\s*" + NUM, text)
    f = re.search(r"(?:预测|预期|预期值)\s*" + NUM, text)
    if a and f:
        try:
            return {"actual": float(a.group(1)), "forecast": float(f.group(1)),
                    "raw_form": "labeled"}
        except ValueError:
            pass
    # 无标签形式: 数字后面必须紧跟 % 或单位词, 再跟"预期"
    m = re.search(
        NUM + r"\s*(?:%|％|亿|万|千|吨|桶|口|美元)?[^0-9，,。]{0,12}[，,]\s*"
        r"(?:预期|预测)\s*" + NUM, text)
    if m:
        pre = text[max(0, m.start() - 6):m.start()]
        if re.search(r"月|日|年", pre):
            return None
        try:
            return {"actual": float(m.group(1)), "forecast": float(m.group(2)),
                    "raw_form": "unlabeled"}
        except ValueError:
            return None
    return None


def classify(text: str) -> tuple[str, dict[str, str]] | None:
    """指标名 + 强于预期时各品种的方向。"""
    for pat, name, mapping in INDICATORS:
        if re.search(pat, text, re.I):
            return name, mapping
    return None


def detect(conn: sqlite3.Connection, days: int) -> list[dict]:
    """解析出"可交易的数据公布事件" (有实际值、有预期值、美国口径、指标可识别)。

    两个来源 (2026-11-14 补第二个):
      ① news_events.headline —— 原来只用这个, 40 天只捞出 26 个事件;
      ② raw_news 里的「[金十公布] ... 实际 X 预测 Y 前值 Z」—— 这是金十的
        结构化发布条目, 40 天里有 522 条、其中 196 条含"美国", 全部带三个数值。
         之前完全没读它, 这就是"样本只有 4 笔"的真正原因, 不是历史不够长。
    """
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    out: list[dict] = []
    seen: dict[tuple[str, int], int] = {}

    def handle(ts: int, raw_text: str) -> None:
        text = clean(raw_text)
        if not text or not US_RE.search(text):
            return
        cls = classify(text)
        if not cls:
            return
        name, mapping = cls
        parsed = parse_release(text)
        if not parsed:
            return
        key = (name, ts // DEDUP_SEC)
        if key in seen:
            return
        seen[key] = ts
        surprise = parsed["actual"] - parsed["forecast"]
        base = max(abs(parsed["forecast"]), 1e-6)
        rel = surprise / base
        bucket = ("符合预期" if abs(rel) < 0.03
                  else "超预期" if surprise > 0 else "低于预期")
        out.append({"ts": ts, "indicator": name, "mapping": mapping,
                    "actual": parsed["actual"], "forecast": parsed["forecast"],
                    "surprise": surprise, "rel": rel, "bucket": bucket,
                    "delayed": bool(DELAY_RE.search(text)),
                    "headline": text[:70]})

    rows = conn.execute("SELECT ts, headline FROM news_events WHERE ts>=? ORDER BY ts",
                        (since,)).fetchall()
    for ts, h in rows:
        handle(ts, h)

    rows2 = conn.execute(
        "SELECT ts, content FROM raw_news WHERE ts>=? AND content LIKE '%[金十公布]%' "
        "ORDER BY ts", (since,)).fetchall()
    for ts, c in rows2:
        handle(ts, (c or "")[:400])        # 三个数值都在前 200 字, 截断省时间

    return out


# ── 盘面状态 ───────────────────────────────────────────────────────────────
def market_state(conn: sqlite3.Connection, asset: str, ts: int) -> dict:
    """判断此刻盘面: 趋势 / 波动 / 位置 / 资金拥挤。

    用 tick 价算, 因为 tick 覆盖 40 天(kline 只有 19 天), 且口径与影子账本一致。
    """
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts<=? AND ts>=? "
        "ORDER BY ts", (asset, ts, ts - 3 * 86400)).fetchall()
    prices = [float(r[1]) for r in rows if r[1]]
    if len(prices) < 100:
        return {"regime": "unknown", "ret_1d": 0.0, "atr": 0.0, "pos": 0.0,
                "funding": None}
    px = prices[-1]
    ret_1d = (px - prices[-1440]) / prices[-1440] * 100 if len(prices) > 1440 else 0.0
    ret_4h = (px - prices[-240]) / prices[-240] * 100 if len(prices) > 240 else 0.0
    ma20 = sum(prices[-20 * 60:]) / min(len(prices), 20 * 60)
    ma50 = sum(prices[-50 * 60:]) / min(len(prices), 50 * 60)
    # 波动: 每 30 分钟一格的涨跌中位数
    steps = [abs(prices[i] - prices[i - 30]) / prices[i - 30] * 100
             for i in range(30, len(prices), 30) if prices[i - 30]]
    atr = sorted(steps)[len(steps) // 2] if steps else 0.0
    if atr >= 1.2:
        regime = "高波动"
    elif ret_1d > 1.0 and ma20 > ma50:
        regime = "上涨趋势"
    elif ret_1d < -1.0 and ma20 < ma50:
        regime = "下跌趋势"
    else:
        regime = "震荡"
    # v450: flow_snapshots 的写入方已失踪（该表 11-17 起停更），费率改读活源
    # external_indicators（binance 5 分钟一采）；读不到再回落老表，避免把两周前当现在。
    funding = None
    try:
        f = conn.execute(
            "SELECT value FROM external_indicators WHERE indicator='bn_funding_rate' "
            "AND asset=? AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
        if f:
            funding = float(f[0])
    except Exception:
        f = None
    if funding is None:
        f = conn.execute(
            "SELECT value FROM flow_snapshots WHERE asset=? AND kind='funding' "
            "AND ts<=? ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
        funding = float(f[0]) if f else None
    # 日波动: 每 30 分钟一格的"一天内"涨跌中位数 —— 止盈止损要按品种自己的尺度定,
    # 之前用 30 分钟波动, 黄金只有 0.02%, 结果全被地板值(0.5/0.35)卡死, 三个品种同一套数
    day_steps = [abs(prices[i] - prices[i - 480]) / prices[i - 480] * 100
                 for i in range(480, len(prices), 30) if prices[i - 480]]
    day_vol = sorted(day_steps)[len(day_steps) // 2] if day_steps else 0.0
    return {"regime": regime, "ret_1d": round(ret_1d, 3), "ret_4h": round(ret_4h, 3),
            "atr": round(atr, 3), "day_vol": round(day_vol, 3),
            "pos": round((px - ma50) / ma50 * 100, 2), "funding": funding}


def plan(event: dict, state: dict) -> dict:
    """预案: 给定"意外方向 + 盘面状态", 决定做不做、做多久、多大。

    规则(可解释):
      · 延迟/未发布 → 不做 (延迟本身常引发二次冲击, 方向不可预判)
      · 符合预期     → 不做 (没有新信息)
      · 超预期/低于预期 → 按指标映射得到方向; 与趋势同向 → 持有 24h 标准仓位
                          逆向 → 只做 4h、半仓; 高波动 → 减半持有时间
      · 资金费率与方向相反(即逆着拥挤方向做) → 加分
    """
    mapping = event["mapping"]
    if event["delayed"]:
        return {"action": "HOLD", "asset": None, "hold_h": 0, "size": 0,
                "why": ["数据延迟/未发布, 不预判"]}
    if event["bucket"] == "符合预期":
        return {"action": "HOLD", "asset": None, "hold_h": 0, "size": 0,
                "why": ["符合预期, 无新信息"]}
    sign = 1 if event["surprise"] > 0 else -1
    out = []
    for asset, base_dir in mapping.items():
        direction = base_dir if sign > 0 else ("BUY" if base_dir == "SELL" else "SELL")
        trend_dir = "BUY" if state["ret_4h"] > 0 else "SELL"
        aligned = direction == trend_dir
        hold = 24 if aligned else 4
        size = 1.0 if aligned else 0.5
        why = [f"{event['indicator']} {event['bucket']} → {direction}"]
        if state["regime"] == "高波动":
            hold = min(hold, 8)
            size *= 0.7
            why.append("高波动, 缩短持有并减仓")
        if not aligned:
            why.append(f"逆势(4h {state['ret_4h']:+.2f}%), 只做 {hold}h 半仓")
        funding = state.get("funding")
        if funding is not None:
            if direction == "BUY" and funding > 0.0001:
                why.append("多头拥挤, 做多性价比差")
                size *= 0.6
            elif direction == "SELL" and funding < -0.0001:
                why.append("空头拥挤, 做空性价比差")
                size *= 0.6
            elif (direction == "BUY" and funding < 0) or (direction == "SELL" and funding > 0):
                why.append("逆拥挤方向, 加分")
                size *= 1.2
        out.append({"action": direction, "asset": asset, "hold_h": hold,
                    "size": round(min(size, 1.5), 2), "why": why})
    return out[0] if out else {"action": "HOLD", "asset": None, "hold_h": 0,
                               "size": 0, "why": ["该指标不映射到交易品种"]}


def _price(conn: sqlite3.Connection, asset: str, ts: int, tol: int = 300) -> float | None:
    r = conn.execute("SELECT ts, price FROM market_ticks WHERE symbol=? AND ts<=? "
                     "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    if not r or ts - r[0] > tol:
        return None
    return float(r[1])


def _ticks_between(conn: sqlite3.Connection, asset: str, t0: int,
                   t1: int) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts>? AND ts<=? "
        "ORDER BY ts", (asset, t0, t1)).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows if r[1]]


TP_MULT = 2.0      # 止盈 = 2×该品种 30 分钟典型波动
SL_MULT = 1.5      # 止损 = 1.5×同上 (原 0.8 被正常波动打掉 12/19 笔, 已放宽)


def simulate_trade(conn: sqlite3.Connection, asset: str, direction: str,
                   entry_ts: int, hold_h: int, atr_hint: float,
                   tp_mult: float = 1.0, sl_mult: float = 0.6) -> dict | None:
    """带止盈止损的模拟: 先触哪个算哪个, 都没触就到期结算。

    为什么必须加止损: 按固定持有算时, 顺势那格命中 71.4% 却平均亏 0.64%,
    说明赢的小、亏的大 —— 典型"没止损"。
    止损放宽的理由: 0.8×波动那一版 19 笔里有 12 笔被止损打掉, 而事件后价格
    本来就会来回甩, 止损太紧等于把正常波动当失败。现在按 1.5 倍给空间。
    """
    entry = _price(conn, asset, entry_ts)
    if not entry:
        return None
    tp = max(0.45, tp_mult * atr_hint)
    sl = max(0.30, sl_mult * atr_hint)
    sign = 1.0 if direction == "BUY" else -1.0
    peak = 0.0
    for ts, px in _ticks_between(conn, asset, entry_ts, entry_ts + hold_h * 3600):
        move = (px - entry) / entry * 100 * sign
        peak = max(peak, move)
        if move >= tp:
            return {"pnl": tp, "outcome": "止盈", "atr": round(atr_hint, 3),
                    "tp": round(tp, 3), "sl": round(sl, 3), "peak": round(peak, 3)}
        if move <= -sl:
            return {"pnl": -sl, "outcome": "止损", "atr": round(atr_hint, 3),
                    "tp": round(tp, 3), "sl": round(sl, 3), "peak": round(peak, 3)}
    exit_ = _price(conn, asset, entry_ts + hold_h * 3600)
    if not exit_:
        return None
    move = (exit_ - entry) / entry * 100 * sign
    return {"pnl": move, "outcome": "到期", "atr": round(atr_hint, 3),
            "tp": round(tp, 3), "sl": round(sl, 3), "peak": round(max(peak, move), 3)}


def replay(conn: sqlite3.Connection, days: int) -> None:
    """逐笔回放: 每个事件在当时那一刻, 系统看到什么、怎么判、结果如何。

    这是"按当时的情况回测"的正确姿势 —— 所有判断只用公布时刻之前的信息
    (盘面状态取 ts 之前 3 天数据、预案不用未来价), 进场在公布后 15 分钟。
    """
    events = detect(conn, days)
    print(f"\n=== 逐笔回放 · 近 {days} 天 · {len(events)} 个事件 ===")
    print(f"{'时间':12s} {'指标':6s} {'实际/预期':17s} {'落差':6s} "
          f"{'当时盘面':22s} {'预案':16s} {'结果'}")
    print("-" * 118)
    for ev in events:
        t = datetime.datetime.fromtimestamp(ev["ts"]).strftime("%m-%d %H:%M")
        pair = f"{ev['actual']:g}/{ev['forecast']:g}"
        line_head = f"{t:12s} {ev['indicator']:6s} {pair:17s} {ev['bucket']:6s}"
        if not ev["mapping"]:
            print(f"{line_head} {'(该指标不映射品种)':22s} {'—':16s} 不做")
            continue
        for asset in ev["mapping"]:
            st = market_state(conn, asset, ev["ts"])
            p = plan(ev, st)
            state_txt = (f"{asset} {st['regime']} 4h{st['ret_4h']:+.2f}% "
                         f"波动{st['atr']:.2f}%")
            if p["action"] == "HOLD":
                print(f"{line_head} {state_txt[:22]:22s} "
                      f"{'不做':16s} {p['why'][0]}")
                continue
            sim = simulate_trade(conn, p["asset"], p["action"],
                                 ev["ts"] + ENTRY_DELAY_MIN * 60, p["hold_h"],
                                 st.get("day_vol") or st["atr"])
            if not sim:
                print(f"{line_head} {state_txt[:22]:22s} "
                      f"{p['action']} {p['hold_h']}h{'':6s} 缺行情, 无法结算")
                continue
            verdict = ("✔" if sim["pnl"] > 0 else "✘")
            print(f"{line_head} {state_txt[:22]:22s} "
                  f"{p['action']} {p['hold_h']}h 仓{p['size']:.1f} "
                  f"{verdict} {sim['outcome']} {sim['pnl']:+.2f}% "
                  f"(TP{sim['tp']:.2f}/SL{sim['sl']:.2f}, 最高{sim['peak']:+.2f}%)")
    print("-" * 118)
    print("提示: '逆势' 情形下预案只给 4h 半仓; '延迟/符合预期' 一律不做。")


def pulse_gate(conn: sqlite3.Connection, asset: str, ts: int, direction: str) -> tuple[bool, dict]:
    """事件方向是否被盘面情绪支持 (市场脉搏)。返回 (是否放行, 脉搏)。"""
    from market.market_pulse import pulse
    pl = pulse(conn, asset, ts)
    if pl.get("veto"):
        return True, pl                      # 行情不足时不做额外限制
    need = pl["bull"] if direction == "BUY" else pl["bear"]
    return need >= PULSE_MIN_SUPPORT, pl


def backtest(conn: sqlite3.Connection, days: int) -> None:
    events = detect(conn, days)
    print(f"\n=== 事件预案回测 · 近 {days} 天 ===")
    print(f"解析出可交易事件 {len(events)} 个")
    if not events:
        print("没有可解析的事件")
        return
    from collections import Counter
    print("按指标:", dict(Counter(e["indicator"] for e in events).most_common(8)))
    print("按落差:", dict(Counter(e["bucket"] for e in events)))

    # 逐事件按预案执行, 记录 (状态, 落差, 是否同向, 命中, 收益)
    rows = []
    for ev in events:
        for asset, _ in ev["mapping"].items():
            st = market_state(conn, asset, ev["ts"])
            p = plan(ev, st)
            if p["action"] == "HOLD" or not p["asset"]:
                continue
            sim = simulate_trade(conn, p["asset"], p["action"],
                                 ev["ts"] + ENTRY_DELAY_MIN * 60,
                                 p["hold_h"], st.get("day_vol") or st["atr"])
            if not sim:
                continue
            aligned = (p["action"] == ("BUY" if st["ret_4h"] > 0 else "SELL"))
            ok, pl = pulse_gate(conn, p["asset"], ev["ts"], p["action"])
            need = pl.get("bull" if p["action"] == "BUY" else "bear") or 0
            rows.append({"regime": st["regime"], "bucket": ev["bucket"],
                         "ind": ev["indicator"], "aligned": aligned,
                         "signed": sim["pnl"], "outcome": sim["outcome"],
                         "size": p["size"], "hold": p["hold_h"],
                         "asset": p["asset"], "pulse_ok": ok,
                         "pulse_need": need, "pulse_veto": bool(pl.get("veto")),
                         "pulse": pl["label"]})

    if not rows:
        print("没有可结算的样本")
        return
    print(f"\n可结算 {len(rows)} 笔")
    if len(rows) < 30:
        print("  ⚠ 样本不足 30 笔 —— 下面的命中率/收益只能当调试信息, "
              "不得作为交易依据 (见 runtime/EVIDENCE_FROZEN.md)")
    print(f"{'分格':24s} {'笔数':>4s} {'命中率':>7s} {'平均':>9s} {'扣费后':>9s}")
    for name, sel in (
            ("全部", rows),
            ("顺势(同向)", [r for r in rows if r["aligned"]]),
            ("逆势", [r for r in rows if not r["aligned"]]),
            ("超预期", [r for r in rows if r["bucket"] == "超预期"]),
            ("低于预期", [r for r in rows if r["bucket"] == "低于预期"]),
            ("上涨趋势", [r for r in rows if r["regime"] == "上涨趋势"]),
            ("下跌趋势", [r for r in rows if r["regime"] == "下跌趋势"]),
            ("震荡", [r for r in rows if r["regime"] == "震荡"]),
            ("高波动", [r for r in rows if r["regime"] == "高波动"]),
    ):
        if not sel:
            continue
        hit = sum(1 for r in sel if r["signed"] > 0) / len(sel) * 100
        avg = sum(r["signed"] for r in sel) / len(sel)
        print(f"{name:24s} {len(sel):4d} {hit:6.1f}% {avg:+8.3f}% {avg-FEE_ROUND_TRIP:+8.3f}%")

    print("\n出场方式:", {k: sum(1 for r in rows if r["outcome"] == k)
                        for k in ("止盈", "止损", "到期")})

    # 脉搏闸门对比: 同样的事件, 加上"盘面情绪必须支持"这一层
    gated = [r for r in rows if r["pulse_ok"]]
    blocked = [r for r in rows if not r["pulse_ok"]]
    print(f"\n{'版本':16s} {'笔数':>4s} {'命中率':>7s} {'平均':>9s} {'扣费后':>9s}")
    for name, sel in (("不看情绪(全部)", rows), ("+脉搏闸门", gated),
                      ("被脉搏拦掉", blocked)):
        if not sel:
            print(f"{name:16s}    0       —         —         —")
            continue
        hit = sum(1 for r in sel if r["signed"] > 0) / len(sel) * 100
        avg = sum(r["signed"] for r in sel) / len(sel)
        print(f"{name:16s} {len(sel):4d} {hit:6.1f}% {avg:+8.3f}% {avg-FEE_ROUND_TRIP:+8.3f}%")

    # 门槛扫描: "多放几笔" 到底会不会把命中率搞垮
    print(f"\n{'闸门门槛':22s} {'笔数':>4s} {'命中率':>7s} {'扣费后':>9s}")
    for th in (0, 20, 25, 30, 35, 40, 45, 50, 55):
        sel = [r for r in rows if r["pulse_veto"] or r["pulse_need"] >= th]
        label = f"≥{th}" + (" (等于全放)" if th == 0 else "")
        if not sel:
            print(f"{label:22s}    0       —         —")
            continue
        hit = sum(1 for r in sel if r["signed"] > 0) / len(sel) * 100
        avg = sum(r["signed"] for r in sel) / len(sel)
        print(f"{label:22s} {len(sel):4d} {hit:6.1f}% {avg-FEE_ROUND_TRIP:+8.3f}%")

    # 分层方案: 40 分以上标准仓, 30~40 分半仓观察, 30 分以下不做
    print("\n分层方案 (≥40 标准仓 / 35~40 半仓 / <35 不做):")
    tiers = []
    for r in rows:
        if r["pulse_veto"]:
            tiers.append((r["signed"], 1.0))
        elif r["pulse_need"] >= 40:
            tiers.append((r["signed"], 1.0))
        elif r["pulse_need"] >= 35:
            tiers.append((r["signed"], 0.5))
    if tiers:
        n = len(tiers)
        hit = sum(1 for s, _ in tiers if s > 0) / n * 100
        avg = sum(s * w for s, w in tiers) / n
        wf = sum(w for _, w in tiers) / n                  # 平均仓位
        print(f"  笔数 {n}  命中率 {hit:.1f}%  平均 {avg:+.3f}%  "
              f"扣费后 {avg - FEE_ROUND_TRIP * wf:+.3f}%  (平均仓位 {wf:.0%})")

    # 最优组合格: 状态 × 落差
    print(f"\n{'状态×落差':30s} {'笔数':>4s} {'命中率':>7s} {'平均':>9s}")
    cells: dict[str, list[float]] = {}
    for r in rows:
        key = f"{r['regime']}×{r['bucket']}"
        cells.setdefault(key, []).append(r["signed"])
    for key, vals in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        if len(vals) < 3:
            continue
        hit = sum(1 for v in vals if v > 0) / len(vals) * 100
        print(f"{key:30s} {len(vals):4d} {hit:6.1f}% {sum(vals)/len(vals):+8.3f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if a.events:
            evs = detect(conn, a.days)
            print(f"近 {a.days} 天可交易事件 {len(evs)} 个:")
            for e in evs:
                print(f"  {datetime.datetime.fromtimestamp(e['ts']):%m-%d %H:%M} "
                      f"{e['indicator']:6s} 实际{e['actual']:>8} 预期{e['forecast']:>8} "
                      f"{e['bucket']:4s} | {e['headline'][:40]}")
        if a.replay:
            replay(conn, a.days)
        if a.backtest or not (a.events or a.replay):
            backtest(conn, a.days)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
