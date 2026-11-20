"""零延迟回放：把入场时间从"实际下单时刻"前移到"新闻发布时刻"。

为什么要做：
  线上 quick_sim 的 entry_ts 是 int(time.time())，即分析完成、真正下单的那一刻。
  新闻从发布到下单中位滞后 13.5 分钟（均值 26.8，最长 123），这段时间里价格已经走掉一段。
  于是"看对方向却亏钱"里有一块无法归因：是判断错了，还是进场太晚？

本脚本做三件事：
  1. 按同一批决策，把入场锚点换成新闻发布时刻，重算 20/60/240 分钟的盈亏；
  2. 算出"发布→下单"这段里已经走掉的幅度（延迟成本的名义值）；
  3. 把实际入场与回放入场并排，差多少 = 延迟的可回收上限（不是可赚的钱）。

口径纪律（重要）：
  - 回放价取"锚点时刻及之前最近一根 K 线收盘"，绝不使用锚点之后的bar；
  - 只读 DB，不写任何表；结果只打印，不落库；
  - 回放是"零延迟理想化"，不是可交易收益：分析本身要时间，且 pre_confirm 这类
    "已动确认"因子在回放里会消失，两者不可直接对比结论，只能对比差额。

用法：
  python3 scripts/backdated_entry_replay.py                 # 11 笔成交，20/60/240 分钟
  python3 scripts/backdated_entry_replay.py --shadow        # 38 笔影子账本，4 小时
  python3 scripts/backdated_entry_replay.py --offset 60     # 锚点后移 60 秒（留最小反应时间）
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path(__file__).resolve().parents[1] / "backend" / "trident_event_bus.db"

# 由细到粗；选"覆盖到锚点时刻"的最细周期
TIMEFRAMES = (("1m", 60), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400))
# 可接受的最大陈旧度（秒）：超过它说明这段行情是冻结/断源的，不能用来定价
MAX_STALE = {"1m": 900, "15m": 1800, "1h": 4500, "4h": 9000, "1d": 90000}


def epoch(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e12 else v


def fmt(ts) -> str:
    if ts is None:
        return "NA"
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")


class Pricer:
    """按符号取"目标时刻及之前最近一根收盘价"，带陈旧度检查。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cov: dict[Tuple[str, str], int] = {}
        for sym, tf, mx in conn.execute(
            "SELECT symbol, timeframe, MAX(ts) FROM kline_bars GROUP BY symbol, timeframe"
        ):
            self._cov[(sym, tf)] = int(mx or 0)

    def _tf_for(self, symbol: str, ts_sec: float) -> Optional[str]:
        target_ms = int(ts_sec * 1000)
        for tf, _ in TIMEFRAMES:
            if self._cov.get((symbol, tf), 0) >= target_ms:
                return tf
        return None

    def close_at(self, symbol: str, ts_sec: float) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """返回 (收盘价, 周期, 陈旧秒数)。陈旧超限的价格一律不返回——宁缺勿假。"""
        tf = self._tf_for(symbol, ts_sec)
        if not tf:
            return None, None, None
        row = self.conn.execute(
            "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1",
            (symbol, tf, int(ts_sec * 1000)),
        ).fetchone()
        if not row:
            return None, tf, None
        bar_ts = int(row[0]) / 1000.0
        stale = ts_sec - bar_ts
        if stale > MAX_STALE.get(tf, 900):
            return None, tf, stale
        return float(row[1]), tf, stale

    def move_pct(self, symbol: str, t0: float, t1: float) -> Optional[float]:
        p0, _, _ = self.close_at(symbol, t0)
        p1, _, _ = self.close_at(symbol, t1)
        if p0 and p1:
            return (p1 - p0) / p0 * 100.0
        return None


def news_ts(conn: sqlite3.Connection, news_id) -> Optional[float]:
    row = conn.execute("SELECT ts, first_seen_ts FROM raw_news WHERE id=?", (news_id,)).fetchone()
    if not row:
        return None
    return epoch(row[0]) or epoch(row[1])


def signed(action: str, move_pct: float) -> float:
    return move_pct if action.upper() == "BUY" else -move_pct


def replay_trade(conn, pricer, symbol, action, anchor, horizon_min, cost_bps):
    entry, tf, stale = pricer.close_at(symbol, anchor)
    exit_p, _, _ = pricer.close_at(symbol, anchor + horizon_min * 60)
    if entry is None or exit_p is None:
        return None
    gross = signed(action, (exit_p - entry) / entry * 100.0)
    net = gross - cost_bps / 100.0
    return {
        "entry": entry,
        "exit": exit_p,
        "gross": gross,
        "net": net,
        "tf": tf,
        "stale": stale,
    }


def load_trades(conn):
    rows = conn.execute(
        "SELECT id, decision_id, news_id, asset, action, entry_ts, entry_price, exit_price, "
        "pnl_pct, cost_bps, horizon_minutes FROM quick_sim_trades WHERE settled=1 ORDER BY entry_ts"
    ).fetchall()
    keys = ("id", "decision_id", "news_id", "asset", "action", "entry_ts", "entry_price",
            "exit_price", "pnl_pct", "cost_bps", "horizon")
    return [dict(zip(keys, r)) for r in rows]


def load_shadow(conn):
    rows = conn.execute(
        "SELECT s.decision_id, s.asset, s.direction, s.decided_ts, s.entry_price, s.exit_price, "
        "s.move_pct, s.hit, s.horizon_min, d.news_id, d.created_at "
        "FROM shadow_ledger s LEFT JOIN ai_decisions d ON d.id = s.decision_id "
        "WHERE s.settled_at IS NOT NULL AND s.hit IS NOT NULL ORDER BY s.decided_ts"
    ).fetchall()
    keys = ("decision_id", "asset", "action", "entry_ts", "entry_price", "exit_price",
            "move_pct", "hit", "horizon", "news_id", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", action="store_true", help="用影子账本(38笔/4小时)而不是成交(11笔/20分钟)")
    ap.add_argument("--offset", type=int, default=0, help="锚点后移秒数，给最小反应时间")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pricer = Pricer(conn)

    # 默认成本：影子账本不收成本，成交按实际 cost_bps
    print(f"DB: {args.db}")
    if args.shadow:
        items = load_shadow(conn)
        label = "影子账本（4 小时）"
        default_horizon = 240
    else:
        items = load_trades(conn)
        label = "快速模拟成交（20 分钟）"
        default_horizon = 20

    print(f"\n== {label} · n={len(items)} · 锚点后移 {args.offset}s ==\n")
    print(f"{'标的':5} {'方向':4} {'发布时刻':>11} {'实际入场':>11} {'滞后分':>6} "
          f"{'已走掉%':>8} {'实际%':>8} {'回放毛%':>8} {'回放净%':>8} {'周期':>4} {'陈旧':>6}")

    agg = {"actual": 0.0, "gross": 0.0, "net": 0.0, "pre": 0.0, "n": 0, "miss": 0}
    for it in items:
        nts = news_ts(conn, it["news_id"]) if it["news_id"] else None
        entry_ts = epoch(it["entry_ts"])
        if not nts or not entry_ts:
            agg["miss"] += 1
            print(f"{it['asset']:5} {it['action']:4} {'NA':>11} {fmt(entry_ts):>11} {'-':>6}")
            continue

        anchor = nts + args.offset
        horizon = int(it.get("horizon") or default_horizon)
        cost = float(it["cost_bps"]) if (not args.shadow and it.get("cost_bps")) else 0.0

        pre_move = pricer.move_pct(it["asset"], anchor, entry_ts)
        pre_signed = signed(it["action"], pre_move) if pre_move is not None else None

        rp = replay_trade(conn, pricer, it["asset"], it["action"], anchor, horizon, cost)
        actual = float(it["pnl_pct"]) if not args.shadow else signed(
            it["action"], float(it["move_pct"] or 0.0))
        if rp is None:
            agg["miss"] += 1
            print(f"{it['asset']:5} {it['action']:4} {fmt(nts):>11} {fmt(entry_ts):>11} "
                  f"{(entry_ts - nts) / 60:6.1f} {'NA':>8} {actual:+8.3f}")
            continue

        lag_min = (entry_ts - nts) / 60.0
        agg["n"] += 1
        agg["actual"] += actual
        agg["gross"] += rp["gross"]
        agg["net"] += rp["net"]
        agg["pre"] += pre_signed or 0.0
        # v489：这里原来在 f-string 里又套了同名引号的 f-string（f"{f'{x['y']}'}"），
        # Python 3.12 才允许复用引号，而 ruff.toml 的 target-version 是 py311 ——
        # 在 3.11 环境下这一行是**语法错误**，整个文件都 import 不了。
        # 3.14 解释器能跑，所以平时看不出来。拆成变量后两种版本都对。
        _pre_txt = f"{pre_signed:+.2f}" if pre_signed is not None else "NA"
        _stale_txt = f"{rp['stale'] / 60:.0f}m" if rp["stale"] else "-"
        print(f"{it['asset']:5} {it['action']:4} {fmt(nts):>11} {fmt(entry_ts):>11} {lag_min:6.1f} "
              f"{_pre_txt:>8} {actual:+8.3f} "
              f"{rp['gross']:+8.3f} {rp['net']:+8.3f} {str(rp['tf']):>4} "
              f"{_stale_txt:>6}")

    n = agg["n"]
    print("\n== 汇总 ==")
    if n:
        print(f" 样本            {n}（跳过 {agg['miss']}）")
        print(f" 实际累计        {agg['actual']:+.2f}%   均值 {agg['actual']/n:+.3f}%")
        print(f" 回放(毛)累计    {agg['gross']:+.2f}%   均值 {agg['gross']/n:+.3f}%")
        print(f" 回放(净)累计    {agg['net']:+.2f}%   均值 {agg['net']/n:+.3f}%")
        print(f" 发布→下单已走   {agg['pre']:+.2f}%（按持仓方向计）")
        print(f" 延迟可回收上限  {agg['gross'] - agg['actual']:+.2f}%")
    else:
        print(" 无可回放样本")

    print("\n口径提醒：回放是零延迟理想化。分析要 13.5 分钟是事实，"
          "pre_confirm 这类'已动确认'因子在回放里会消失，"
          "所以回放差值只能读作'延迟吃掉多少'，不能读作'改快就能赚多少'。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
