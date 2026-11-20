"""事件波段交易员 —— 独立一条线: 只做"有确定公布时间"的宏观事件, 持仓到第二天。

为什么单开一条线 (v335 结论):
  短线(30 分钟~8 小时)所有基于公开价格的信号, 扣 0.08% 手续费后全部归零。
  根因是: 那个尺度上价格能预测的幅度只有 0.03%~0.1%, 手续费比利润还大。
  换到大波段: 宏观事件(CPI/非农/FOMC/EIA)公布后, 1~2 天内的波动通常 1%~3%,
  手续费只占 4%, 方向判断只要略好于抛硬币就能赚钱。

与短线交易员的区别:
  · 短线: 追已经动的价格 → 已验证无优势
  · 事件波段: 只在"日历上写好的时刻"进场, 持有 24~48 小时

回测的四个方向假设 (都要用真实历史比):
  follow : 公布后 15 分钟价格往哪走, 就跟着做 (动量延续)
  fade   : 反着做 (公布后的第一反应过度)
  long   : 一律做多 (基准: 看这段时间市场本身是不是涨的)
  short  : 一律做空

用法:
  python3 event_swing.py --events --days 40          # 列出识别到的事件
  python3 event_swing.py --backtest --days 40        # 回测四种假设
"""
from __future__ import annotations

import argparse
import datetime
import epoch_gate
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# ── 事件定义: 只有"确定时刻公布"的宏观数据, 不含纪要/讲话/预测文章 ──────────
EVENT_TYPES: dict[str, dict] = {
    "CPI":  {"pat": r"CPI", "assets": ("XAU", "BTC")},
    "非农": {"pat": r"非农", "assets": ("XAU", "BTC")},
    "FOMC": {"pat": r"利率决议|FOMC决议|美联储宣布|加息\s?\d|降息\s?\d|维持利率", "assets": ("XAU", "BTC")},
    "EIA":  {"pat": r"EIA", "assets": ("WTI",)},
    "PCE":  {"pat": r"PCE", "assets": ("XAU", "BTC")},
    "失业率": {"pat": r"失业率", "assets": ("XAU", "BTC")},
    "零售": {"pat": r"零售销售", "assets": ("XAU", "BTC")},
}
# 必须像"实际公布", 不是预告/评论
RELEASE_RE = re.compile(r"公布|实际值?|录得|发布|升至|降至|高于预期|低于预期")
# 必须是美国口径 (德国 CPI / 日本 CPI 不碰)
US_RE = re.compile(r"美国|美\b|美金|美元|US|U\.S\.|Fed|美联储")
DEDUP_SEC = 6 * 3600          # 同一事件多源转载, 6 小时内只算一次
ENTRY_DELAY_MIN = 15          # 等第一波冲击过去再进
DEFAULT_HOLD_H = 24
FEE_ROUND_TRIP = 0.08         # 来回手续费 %
DEFAULT_STEP = 15             # 同一事件 15 分钟内不重复进场


def _clean(h: str) -> str:
    """剥掉 [hash:xxx] 前缀与 [来自xx] 尾巴, 这些会干扰关键词判断。"""
    h = re.sub(r"\[hash:[^\]]*\]", "", h or "")
    h = re.sub(r"\[来自[^\]]*\]", "", h)
    return h.strip()


def detect_events(conn: sqlite3.Connection, days: int) -> list[dict]:
    """从新闻流里捞出"真正公布"的美国宏观事件, 按类型 6 小时去重。"""
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    # v340 纪元闸门: 该作业按"近 N 天"重扫新闻，会跨过 data_epoch，
    # 把清空前的波段事件重新写进 event_swing_ledger（实测 77 行）。
    # 统一走 epoch_gate —— 这里曾三次写成 datetime.strptime 的不同变体，
    # 全部静默失败（AttributeError 被吞），闸门等于没开。
    since = epoch_gate.clamp_since(since, epoch_gate.epoch_ts(conn)) or since
    rows = conn.execute("SELECT ts, headline FROM news_events WHERE ts>=? ORDER BY ts",
                        (since,)).fetchall()
    out: list[dict] = []
    seen: dict[tuple[str, int], int] = {}
    for ts, h in rows:
        text = _clean(h)
        if not text or not US_RE.search(text) or not RELEASE_RE.search(text):
            continue
        for name, spec in EVENT_TYPES.items():
            if re.search(spec["pat"], text, re.I):
                bucket = ts // DEDUP_SEC
                key = (name, bucket)
                if key in seen:
                    break
                seen[key] = ts
                out.append({"type": name, "ts": ts, "headline": text[:80],
                            "assets": spec["assets"]})
                break
    return out


def _price(conn: sqlite3.Connection, asset: str, ts: int,
           tol: int = 300) -> float | None:
    """取 ts 前最近一笔价 (容忍 5 分钟空洞), 与影子账本同口径。"""
    r = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    if not r or (ts - r[0]) > tol:
        return None
    return float(r[1])


def backtest(conn: sqlite3.Connection, days: int, hold_h: int = DEFAULT_HOLD_H) -> None:
    events = detect_events(conn, days)
    print(f"\n=== 事件波段回测 · 近 {days} 天 · 持有 {hold_h} 小时 ===")
    print(f"识别到事件 {len(events)} 个")
    if not events:
        print("没有可用事件")
        return
    from collections import Counter
    print("按类型:", dict(Counter(e["type"] for e in events)))

    variants = {"follow": [], "fade": [], "long": [], "short": []}
    detail = []
    for ev in events:
        for asset in ev["assets"]:
            p_pre = _price(conn, asset, ev["ts"])
            p_entry = _price(conn, asset, ev["ts"] + ENTRY_DELAY_MIN * 60)
            p_exit = _price(conn, asset, ev["ts"] + hold_h * 3600)
            if not (p_pre and p_entry and p_exit):
                continue
            react = (p_entry - p_pre) / p_pre * 100      # 公布后第一反应
            move = (p_exit - p_entry) / p_entry * 100    # 进场后到结算的涨跌
            if react == 0:
                continue
            d = 1.0 if react > 0 else -1.0
            variants["follow"].append(move * d)
            variants["fade"].append(move * -d)
            variants["long"].append(move)
            variants["short"].append(-move)
            detail.append({"type": ev["type"], "asset": asset, "ts": ev["ts"],
                           "react": react, "move": move, "p_entry": p_entry})

    print(f"\n可用样本 {len(detail)} 个 (事件×品种)")
    print(f"{'假设':8s} {'笔数':>5s} {'命中率':>8s} {'平均收益':>10s} {'扣费后':>9s}")
    for name, rows in variants.items():
        if not rows:
            continue
        hit = sum(1 for r in rows if r > 0) / len(rows) * 100
        avg = sum(rows) / len(rows)
        print(f"{name:8s} {len(rows):5d} {hit:7.1f}% {avg:+9.3f}% {avg-FEE_ROUND_TRIP:+8.3f}%")

    # 按类型看, 哪类事件真的有肉
    print(f"\n{'事件类型':8s} {'笔数':>5s} {'follow命中':>10s} {'follow均值':>11s}")
    by_type: dict[str, list[float]] = {}
    react_signed: dict[str, list[float]] = {}
    for x in detail:
        d = 1.0 if x["react"] > 0 else -1.0
        by_type.setdefault(x["type"], []).append(x["move"] * d)
    for t, rows in by_type.items():
        hit = sum(1 for r in rows if r > 0) / len(rows) * 100
        print(f"{t:8s} {len(rows):5d} {hit:9.1f}% {sum(rows)/len(rows):+10.3f}%")


def ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS event_swing_ledger (
        event_type TEXT, asset TEXT, ts INTEGER,
        react_pct REAL, entry_price REAL, exit_ts INTEGER, exit_price REAL,
        move_pct REAL, fade_pnl REAL, long_pnl REAL,
        hold_h INTEGER, settled_at INTEGER,
        PRIMARY KEY (event_type, asset, ts, hold_h))""")
    conn.commit()


def record(conn: sqlite3.Connection, days: int, hold_h: int = DEFAULT_HOLD_H) -> int:
    """把所有已能结算的事件记账, 攒样本供日后重测 (只记已到期的)。"""
    ensure_ledger(conn)
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for ev in detect_events(conn, days):
        for asset in ev["assets"]:
            p_pre = _price(conn, asset, ev["ts"])
            p_entry = _price(conn, asset, ev["ts"] + ENTRY_DELAY_MIN * 60)
            exit_ts = ev["ts"] + hold_h * 3600
            if not (p_pre and p_entry) or exit_ts > now:
                continue
            p_exit = _price(conn, asset, exit_ts)
            if not p_exit:
                continue
            react = (p_entry - p_pre) / p_pre * 100
            if react == 0:
                continue
            move = (p_exit - p_entry) / p_entry * 100
            fade = move * (-1.0 if react > 0 else 1.0)
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_swing_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev["type"], asset, ev["ts"], react, p_entry, exit_ts, p_exit,
                 move, fade, move, hold_h, now))
            n += cur.rowcount
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--hold", type=int, default=DEFAULT_HOLD_H)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    ro = not a.commit
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) if ro else sqlite3.connect(DB_PATH)
    try:
        if a.events:
            evs = detect_events(conn, a.days)
            print(f"近 {a.days} 天识别到 {len(evs)} 个宏观事件:")
            for e in evs:
                print(f"  {datetime.datetime.fromtimestamp(e['ts']):%m-%d %H:%M} "
                      f"{e['type']:5s} {e['headline'][:56]}")
        if a.commit:
            print(f"记账 {record(conn, a.days, a.hold)} 条到 event_swing_ledger")
        if a.backtest or not (a.events or a.commit):
            backtest(conn, a.days, a.hold)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
