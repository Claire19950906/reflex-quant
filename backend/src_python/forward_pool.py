"""前瞻样本池：从"现在"开始，只收池子建立之后发布的新闻。

与 anchor_replay.py 的区别：
  anchor_replay 是**回溯**——把过去的新闻挪回发布时刻重跑。它再怎么隔离，
  prompt 规则和模型版本仍然是今天的（见报告第 7.3 节）。
  本脚本是**前瞻**——新闻一出现就记下来，用那一刻的价进场，等时间到了再结算。
  没有历史、没有重建、没有一丝未来信息，只有一条纪律：**池子建立之前的新闻，一条都不收。**

池子起点写死在池子建立那一刻（pool_meta.start_ts），之后改不了——
否则"从现在开始"就变成了"从我觉得合适的时候开始"。

用法：
  python3 forward_pool.py --init                # 建池，起点=现在（只建一次）
  python3 forward_pool.py --ingest               # 收新决策进池
  python3 forward_pool.py --settle               # 到期结算
  python3 forward_pool.py --report               # 看池子
  python3 forward_pool.py --loop                 # 常驻：每 60s 收一次、结一次
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROD_DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
POOL_DB = Path(os.getenv("FORWARD_POOL_DB") or (Path(__file__).resolve().parents[1] / "forward_pool.db"))

ASSETS = ("BTC", "XAU", "WTI", "ETH", "SOL")
TIMEFRAMES = (("1m", 60), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400))
MAX_STALE = {"1m": 900, "15m": 1800, "1h": 4500, "4h": 9000, "1d": 90000}
COST_BPS = 18.0
HORIZON_MAIN = 240     # 主口径 4h
HORIZON_SHORT = 20     # 对照口径 20m（对齐线上实际持有期）

DDL = """
CREATE TABLE IF NOT EXISTS pool_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS forward_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  news_id INTEGER, decision_id INTEGER, news_ts INTEGER, decision_ts INTEGER,
  asset TEXT, action TEXT, score REAL, conf REAL, pre_confirm INTEGER,
  entry_anchor REAL, entry_decision REAL, delay_move_pct REAL,
  exit_4h REAL, gross_4h REAL, net_4h REAL, hit_4h INTEGER,
  exit_20m REAL, gross_20m REAL, net_20m REAL, hit_20m INTEGER,
  status TEXT, note TEXT, created_at TEXT, settled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fs_decision ON forward_samples(decision_id);
CREATE TABLE IF NOT EXISTS pool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ev TEXT, from_ts INTEGER, to_ts INTEGER,
  skipped_decisions INTEGER, note TEXT, at TEXT
);
"""


def conn_prod() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def conn_pool() -> sqlite3.Connection:
    c = sqlite3.connect(POOL_DB)
    c.executescript(DDL)
    return c


def meta_get(c: sqlite3.Connection, k: str) -> Optional[str]:
    r = c.execute("SELECT v FROM pool_meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(c: sqlite3.Connection, k: str, v: str) -> None:
    c.execute("INSERT INTO pool_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()


# ───────────── 定价：只用锚点及之前的 bar ─────────────
class Pricer:
    def __init__(self, prod: sqlite3.Connection):
        self.c = prod
        self.cov = {(s, tf): int(mx or 0) for s, tf, mx in
                    prod.execute("SELECT symbol, timeframe, MAX(ts) FROM kline_bars GROUP BY symbol, timeframe")}

    def close_at(self, sym: str, ts: float) -> Tuple[Optional[float], Optional[str]]:
        for tf, _ in TIMEFRAMES:
            if self.cov.get((sym, tf), 0) < int(ts * 1000):
                continue
            r = self.c.execute(
                "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1", (sym, tf, int(ts * 1000))).fetchone()
            if not r:
                continue
            if ts - int(r[0]) / 1000.0 <= MAX_STALE.get(tf, 900):
                return float(r[1]), tf
        return None, None


def _epoch(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 1000.0 if f > 1e12 else f


def _parse_ts(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s[:26].split("+")[0].split("Z")[0], fmt).timestamp()
        except ValueError:
            continue
    return _epoch(v)


# ───────────── 建池 ─────────────
def do_init(at: Optional[str]) -> None:
    c = conn_pool()
    if meta_get(c, "start_ts"):
        print(f"池子已存在，起点 {datetime.datetime.fromtimestamp(float(meta_get(c,'start_ts'))):%Y-%m-%d %H:%M:%S}（不覆盖）")
        return
    start = _parse_ts(at) if at else time.time()
    meta_set(c, "start_ts", str(int(start)))
    meta_set(c, "horizon_main", str(HORIZON_MAIN))
    meta_set(c, "horizon_short", str(HORIZON_SHORT))
    meta_set(c, "cost_bps", str(COST_BPS))
    print(f"池子已建立，起点 {datetime.datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S}")
    print("规则：只收发布时间 >= 起点的新闻；线上若停摆，池子就是空的——不补、不回填。")


# ───────────── 中断保护：只认"现在"，不补堆积 ─────────────
def roll_forward(pc: sqlite3.Connection, now: float, reason: str) -> None:
    """把池子起点前进到 now，并记一笔事件。

    铁律（用户口径，2026-11-18 定）：中断后恢复，以恢复时刻为新起点，
    停机期间堆在队列里的历史新闻一律不处理 —— 宁可样本少，不可样本脏。
    """
    old = int(float(meta_get(pc, "start_ts") or now))
    if now <= old:
        return
    prod = conn_prod()
    skipped = prod.execute(
        """SELECT COUNT(*) FROM ai_decisions d JOIN raw_news n ON n.id=d.news_id
           WHERE n.ts >= ? AND n.ts < ? AND d.analysis_degraded=0
             AND d.target_asset IN ('BTC','XAU','WTI','ETH','SOL')""",
        (old, int(now))).fetchone()[0]
    meta_set(pc, "start_ts", str(int(now)))
    pc.execute("INSERT INTO pool_events(ev,from_ts,to_ts,skipped_decisions,note,at) VALUES(?,?,?,?,?,?)",
               ("roll_forward", old, int(now), int(skipped), reason,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    pc.commit()
    print(f"[中断保护] 起点 {datetime.datetime.fromtimestamp(old):%m-%d %H:%M:%S} → "
          f"{datetime.datetime.fromtimestamp(now):%m-%d %H:%M:%S}；"
          f"停机期堆积 {skipped} 条决策一律不入池（{reason}）")


def check_gap(pc: sqlite3.Connection, gap_threshold: int) -> None:
    """判中断。没有上次扫描记录 → 视同中断（宁严不宽），起点直接前进到现在。"""
    last = meta_get(pc, "last_scan_ts")
    now = time.time()
    if not last:
        roll_forward(pc, now, "无上次扫描记录（视同中断）")
    else:
        gap = now - float(last)
        if gap > gap_threshold:
            roll_forward(pc, now, f"中断 {gap/60:.0f} 分钟（阈值 {gap_threshold/60:.0f} 分钟）")
    meta_set(pc, "last_scan_ts", str(int(now)))


# ───────────── 收样本 ─────────────
def do_ingest(verbose: bool = True, gap_threshold: int = 600) -> int:
    pc = conn_pool()
    check_gap(pc, gap_threshold)
    start = meta_get(pc, "start_ts")
    if not start:
        print("池子还没建，先跑 --init")
        return 0
    start_ts = int(start)
    prod = conn_prod()
    px = Pricer(prod)

    rows = prod.execute(
        """
        SELECT d.id, d.news_id, n.ts AS news_ts, d.created_at, d.target_asset,
               d.suggested_action, d.sentiment_score, d.evidence_confidence,
               d.analysis_degraded, d.decision_context
        FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE n.ts >= ? AND d.analysis_degraded = 0
          AND d.target_asset IN ('BTC','XAU','WTI','ETH','SOL')
        ORDER BY d.id ASC
        """, (start_ts,)).fetchall()
    # 池表在另一个库，"是否已入池"只能在本地判（下面逐条查）

    added = 0
    for r in rows:
        did = r["id"]
        if pc.execute("SELECT 1 FROM forward_samples WHERE decision_id=?", (did,)).fetchone():
            continue
        nts = _epoch(r["news_ts"])
        dts = _parse_ts(r["created_at"])
        asset = str(r["target_asset"]).upper()
        action = str(r["suggested_action"] or "HOLD").upper()
        if not nts:
            continue

        entry, tf = px.close_at(asset, nts)
        dentry, _ = px.close_at(asset, dts) if dts else (None, None)
        delay = None
        if entry and dentry and entry:
            delay = (dentry - entry) / entry * 100.0
        pre = None
        try:
            ctx = r["decision_context"]
            if ctx:
                pre = 1 if '"pre_confirm": true' in str(ctx) or "'pre_confirm': True" in str(ctx) else 0
        except Exception:
            pre = None

        status = "pending" if (action in ("BUY", "SELL") and entry) else ("hold" if action == "HOLD" else "no_price")
        pc.execute(
            """INSERT INTO forward_samples
            (news_id, decision_id, news_ts, decision_ts, asset, action, score, conf, pre_confirm,
             entry_anchor, entry_decision, delay_move_pct, status, note, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["news_id"], did, int(nts), int(dts) if dts else None, asset, action,
             r["sentiment_score"], r["evidence_confidence"], pre,
             entry, dentry, delay, status,
             f"anchor_tf={tf}" if tf else "无锚点价",
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        added += 1
        if verbose:
            print(f"  + #{did} {asset} {action} 新闻 {datetime.datetime.fromtimestamp(nts):%m-%d %H:%M} "
                  f"锚点价 {entry if entry else 'NA'}({tf}) 延迟位移 {f'{delay:+.2f}%' if delay is not None else 'NA'}")
    pc.commit()
    if verbose:
        print(f"新增 {added} 条（池子起点之后、未入池的决策全部扫过）")
    return added


# ───────────── 结算 ─────────────
def _pnl(action: str, entry: Optional[float], exit_p: Optional[float], cost_bps: float):
    if not entry or not exit_p or action not in ("BUY", "SELL"):
        return None, None, None
    move = (exit_p - entry) / entry * 100.0
    gross = move if action == "BUY" else -move
    net = gross - cost_bps / 100.0
    return gross, net, 1 if net > 0 else 0


def do_settle(verbose: bool = True) -> int:
    pc = conn_pool()
    start = meta_get(pc, "start_ts")
    if not start:
        print("池子还没建，先跑 --init")
        return 0
    horizon_main = int(meta_get(pc, "horizon_main") or HORIZON_MAIN)
    horizon_short = int(meta_get(pc, "horizon_short") or HORIZON_SHORT)
    cost = float(meta_get(pc, "cost_bps") or COST_BPS)
    prod = conn_prod()
    px = Pricer(prod)
    now = time.time()

    pending = pc.execute(
        "SELECT id, news_ts, asset, action, entry_anchor FROM forward_samples "
        "WHERE status='pending' AND news_ts + ? <= ?", (horizon_main * 60, int(now))).fetchall()
    done = 0
    for sid, nts, asset, action, entry in pending:
        e4, _ = px.close_at(asset, nts + horizon_main * 60)
        e20, _ = px.close_at(asset, nts + horizon_short * 60)
        g4, n4, h4 = _pnl(action, entry, e4, cost)
        g20, n20, h20 = _pnl(action, entry, e20, cost)
        if n4 is None:
            continue
        pc.execute(
            """UPDATE forward_samples SET exit_4h=?, gross_4h=?, net_4h=?, hit_4h=?,
               exit_20m=?, gross_20m=?, net_20m=?, hit_20m=?, status='settled', settled_at=?
               WHERE id=?""",
            (e4, g4, n4, h4, e20, g20, n20, h20,
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid))
        done += 1
        if verbose:
            print(f"  = #{sid} {asset} {action} 4h净 {n4:+.3f}% | 20m净 {f'{n20:+.3f}%' if n20 is not None else 'NA'}")
    pc.commit()
    if verbose:
        print(f"结算 {done} 条")
    return done


# ───────────── 报告 ─────────────
def do_report() -> None:
    c = conn_pool()
    start = meta_get(c, "start_ts")
    if not start:
        print("池子还没建")
        return
    st = datetime.datetime.fromtimestamp(float(start))
    print(f"\n池子起点：{st:%Y-%m-%d %H:%M:%S}（此前的新闻一律不计）")
    counts = dict(c.execute("SELECT status, COUNT(*) FROM forward_samples GROUP BY status").fetchall())
    print(f"状态分布：{counts or '空'}")

    for label, net_col, hit_col in (("4h 口径", "net_4h", "hit_4h"), ("20m 对照", "net_20m", "hit_20m")):
        rows = c.execute(f"SELECT action, {net_col}, {hit_col} FROM forward_samples WHERE {net_col} IS NOT NULL").fetchall()
        print(f"\n[{label}]")
        if not rows:
            print("  暂无结算样本（线上停摆或还没到期）")
            continue
        n = len(rows)
        print(f"  合计 n={n} 净利 {sum(r[1] for r in rows):+.2f}% 命中 {100*sum(r[2] for r in rows)/n:.1f}%")
        for a in ("BUY", "SELL"):
            sub = [r for r in rows if r[0] == a]
            if sub:
                print(f"    {a}: n={len(sub)} 净利 {sum(r[1] for r in sub):+.2f}% 命中 {100*sum(r[2] for r in sub)/len(sub):.1f}%")
    ev = c.execute("SELECT COUNT(*), MAX(at) FROM pool_events WHERE ev='roll_forward'").fetchone()
    if ev and ev[0]:
        print(f"\n中断重置 {ev[0]} 次，最近一次 {ev[1]}（停机期数据未补）")
    d = c.execute("SELECT AVG(ABS(delay_move_pct)), COUNT(*) FROM forward_samples WHERE delay_move_pct IS NOT NULL").fetchone()
    if d and d[1]:
        print(f"\n延迟位移（锚点→下单）：均值 {d[0]:.2f}%，n={d[1]}")


def do_loop(interval: int = 60, gap_threshold: int = 600) -> None:
    print(f"常驻采集中，每 {interval}s 扫一次；Ctrl+C 退出")
    while True:
        try:
            do_settle(verbose=False)
            do_ingest(verbose=True, gap_threshold=gap_threshold)
            do_report()
        except KeyboardInterrupt:
            print("停止")
            return
        except Exception as e:
            print(f"[LOOP] {type(e).__name__}: {e}")
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="建池，起点=现在（只建一次）")
    ap.add_argument("--start-at", default="", help="建池时指定起点（默认现在）")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--gap-threshold", type=int, default=600, help="中断多久算一次新起点（秒）")
    ap.add_argument("--no-gap-check", action="store_true", help="跳中断判定（仅调试用）")
    args = ap.parse_args()

    if args.init:
        do_init(args.start_at or None)
    if args.ingest:
        do_ingest(gap_threshold=args.gap_threshold)
    if args.settle:
        do_settle()
    if args.loop:
        do_loop(args.interval, args.gap_threshold)
    if args.report or not any([args.init, args.ingest, args.settle, args.loop]):
        do_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
