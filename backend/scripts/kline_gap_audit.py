#!/usr/bin/env python3
"""kline_gap_audit.py — K 线间隔体检 / 混入行隔离（默认只读，绝不直接删）。

背景（v480 实测）：
  WTI 的 1d 表只有 225 根，但相邻 K 线的「主间隔」只占 40.6%（主间隔 0.2 天）——
  说明小时级的数据被写进了日线表。任何用 WTI 日线做的分析都会失真。
  美股 1d 的主间隔占比 77~78%（周末/假日造成的正常跳空），属正常。

用法：
  python3 scripts/kline_gap_audit.py                       # 全表体检，只读，出报告
  python3 scripts/kline_gap_audit.py --symbol WTI --tf 1d  # 单标的细查
  python3 scripts/kline_gap_audit.py --symbol WTI --tf 1d --apply
                                                          # 把异常间隔行移到隔离表（可回溯）

「隔离」而不是「删除」：把可疑行**复制**到 kline_bars_quarantine 并从原表移除，
源数据留在 quarantine 表里，随时可以查回来。--apply 前会先 VACUUM INTO 做整库快照。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from typing import Any, Dict, List, Optional

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(BACKEND, "trident_event_bus.db")
REPORT_DIR = os.path.join(BACKEND, "reports", "tech_engine")

# 正常间隔的判定：某标的某周期的「众数间隔」视为该周期的标准步长（秒）
_DAY = 86400


def connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def connect_rw(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path)


def load_ts(con: sqlite3.Connection, symbol: str, tf: str) -> List[int]:
    rows = con.execute(
        "SELECT ts FROM kline_bars WHERE symbol=? AND timeframe=? ORDER BY ts",
        (symbol, tf)).fetchall()
    return [int(r[0]) // 1000 for r in rows]      # 统一成秒


def gap_stats(ts: List[int], std_sec: Optional[int] = None) -> Dict[str, Any]:
    """返回主间隔、主间隔占比、间隔直方图。"""
    if len(ts) < 3:
        return {"n": len(ts), "main_gap": None, "main_share": None, "hist": {}}
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not gaps:
        return {"n": len(ts), "main_gap": None, "main_share": None, "hist": {}}
    # 众数：按 5% 容差归并（日线周末跳空会造成 3 天大间隔，不应当众数，但也不该判异常）
    buckets: Dict[int, int] = {}
    for g in gaps:
        key = int(round(g / (g * 0.05 or 1)) * (g * 0.05 or 1)) if g else g
        buckets[key] = buckets.get(key, 0) + 1
    main_gap = max(buckets, key=lambda k: buckets[k])
    main_share = buckets[main_gap] / len(gaps)
    # 直方图按「标准步长的倍数」归类 —— 固定用「天」分档对 4h/5m 毫无意义
    base = std_sec or _DAY
    hist: Dict[str, int] = {}
    for g in gaps:
        if g < base * 0.5:
            hist["<0.5×"] = hist.get("<0.5×", 0) + 1
        elif g < base * 1.5:
            hist["≈1×"] = hist.get("≈1×", 0) + 1
        elif g < base * 4:
            hist["2~3×"] = hist.get("2~3×", 0) + 1
        else:
            hist[">3×"] = hist.get(">3×", 0) + 1
    return {"n": len(ts), "main_gap": main_gap, "main_share": round(main_share, 3),
            "hist": hist, "gaps": gaps, "ts": ts}


# 各周期的标准步长（秒）。判异常时用这个而不是「众数」——
# 因为混入严重时众数本身就会变成被混入的小周期（WTI 1d 的主间隔就是 0.2 天）。
_STD_SEC = {"1d": 86400, "1h": 3600, "4h": 14400, "15m": 900, "5m": 300, "1m": 60}


def find_suspects(st: Dict[str, Any], tf: str) -> List[int]:
    """异常间隔：小于标准步长的 60%。

    对 1d 即 <0.6 天 —— "一天出现两根"只可能是更小周期的数据混进来；
    对 4h 即 <2.4h，依此类推。周末 3 天跳空不会被误判（它比标准步长大）。
    """
    std = _STD_SEC.get(tf) or st.get("main_gap")
    if not std:
        return []
    gaps, ts = st["gaps"], st["ts"]
    out: List[int] = []
    for i, g in enumerate(gaps):
        if g < std * 0.6:
            out.append(ts[i + 1])      # 记后一根的时间戳（重复/混入的那根）
    return out


def audit_all(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT symbol, timeframe, COUNT(*) FROM kline_bars GROUP BY symbol, timeframe"
    ).fetchall()
    out = []
    for sym, tf, n in rows:
        ts = load_ts(con, sym, tf)
        st = gap_stats(ts, _STD_SEC.get(tf))
        # v486：成交量缺失要分「空」和「零」——零值比 NULL 更隐蔽（看着像有量）
        vnull, vzero = con.execute(
            "SELECT SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END),"
            "       SUM(CASE WHEN volume = 0 THEN 1 ELSE 0 END)"
            " FROM kline_bars WHERE symbol=? AND timeframe=?", (sym, tf)).fetchone()
        out.append({
            "symbol": sym, "tf": tf, "n": n,
            "vol_null": int(vnull or 0), "vol_zero": int(vzero or 0),
            "main_gap_days": round(st["main_gap"] / _DAY, 3) if st.get("main_gap") else None,
            "main_share": st.get("main_share"),
            "hist": st.get("hist"),
            "suspects": len(find_suspects(st, tf)),
        })
    return out


def render(rows: List[Dict[str, Any]], applied: Optional[Dict[str, Any]] = None) -> str:
    L = ["# K 线间隔体检", "",
         f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}　库：`trident_event_bus.db`（只读）", "",
         "> 「主间隔占比」= 众数间隔出现次数 / 总间隔数。日线正常值：美股 ~78%（周末跳空）、"
         "加密 ~100%。**明显偏低说明有更小周期的行混进了这张表**。", "",
         "| 标的 | 周期 | 根数 | 主间隔(天) | 主间隔占比 | 量空/量零 | 间隔分布(倍数) | 可疑行 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["main_share"] or 1)):
        hist = "、".join(f"{k}:{v}" for k, v in (r["hist"] or {}).items())
        L.append(f"| {r['symbol']} | {r['tf']} | {r['n']} | {r['main_gap_days']} | "
                 f"{r['main_share']} | {r['vol_null']}/{r['vol_zero']} | {hist} | {r['suspects']} |")
    L.append("")
    weak = [r for r in rows if r["tf"] == "1d" and (r["main_share"] or 0) < 0.6]
    if weak:
        L.append("## 需要处理")
        L.append("")
        for r in weak:
            L.append(f"- **{r['symbol']} 1d**：主间隔 {r['main_gap_days']} 天，占比 {r['main_share']}，"
                     f"可疑行 {r['suspects']} 条 → 小时级记录混入日线表，"
                     "该标的日线指标/回测结论不可信。")
        L.append("")
        L.append("处理方式（不删数据）：")
        L.append("```")
        L.append("# 1) 先看清单（默认就是只读）")
        L.append("python3 scripts/kline_gap_audit.py --symbol WTI --tf 1d")
        L.append("# 2) 确认后移到隔离表（可回溯，执行前自动整库快照）")
        L.append("python3 scripts/kline_gap_audit.py --symbol WTI --tf 1d --apply")
        L.append("```")
        L.append("")
    if applied:
        L.append("## 本次已隔离")
        L.append("")
        L.append(f"- 标的/周期：{applied['symbol']} {applied['tf']}")
        L.append(f"- 隔离行数：{applied['moved']}")
        L.append(f"- 备份：`{applied['backup']}`")
        L.append(f"- 隔离表：`kline_bars_quarantine`（ts 列表见 JSON）")
        L.append("")
    return "\n".join(L)


_SNAPSHOTS: Dict[str, str] = {}


def apply_quarantine(con_rw_path: str, symbol: str, tf: str,
                     suspects: List[int]) -> Dict[str, Any]:
    """把可疑行移入隔离表。先整库快照，再 copy+delete，全程可回溯。

    v488：快照**每次进程只做一次**。
    原来每个 (symbol, tf) 组都 `VACUUM INTO` 一次 581MB 的库，
    `--apply` 跑全库会生成二十几个快照（十几 GB），磁盘直接被撑爆。
    现在同一进程内复用第一份快照。
    """
    backup = _SNAPSHOTS.get(con_rw_path)
    if not backup:
        backup = os.path.join(
            REPORT_DIR, f"db_snapshot_before_quarantine_{int(time.time())}.db")
        os.makedirs(REPORT_DIR, exist_ok=True)
        src = sqlite3.connect(con_rw_path)
        src.execute("VACUUM INTO ?", (backup,))
        src.close()
        _SNAPSHOTS[con_rw_path] = backup

    con = connect_rw(con_rw_path)
    con.execute("""CREATE TABLE IF NOT EXISTS kline_bars_quarantine (
        symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL,
        close REAL, volume REAL, reason TEXT, quarantined_at INTEGER)""")
    moved = 0
    for ts_sec in suspects:
        rows = con.execute(
            "SELECT symbol,timeframe,ts,open,high,low,close,volume FROM kline_bars "
            "WHERE symbol=? AND timeframe=? AND ts=?",
            (symbol, tf, ts_sec * 1000)).fetchall()
        for r in rows:
            con.execute(
                "INSERT INTO kline_bars_quarantine VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*r, "gap_mixed_subperiod", int(time.time())))
            con.execute(
                "DELETE FROM kline_bars WHERE symbol=? AND timeframe=? AND ts=?",
                (symbol, tf, ts_sec * 1000))
            moved += 1
    con.commit()
    con.close()
    return {"symbol": symbol, "tf": tf, "moved": moved, "backup": backup,
            "ts_seconds": suspects}


def main() -> int:
    ap = argparse.ArgumentParser(description="K 线间隔体检 / 混入行隔离")
    ap.add_argument("--symbol", help="只查某标的")
    ap.add_argument("--tf", help="只查某周期，如 1d/4h/1m")
    ap.add_argument("--apply", action="store_true", help="真的把可疑行移到隔离表")
    ap.add_argument("--json-out", default=os.path.join(REPORT_DIR, "kline_gap_audit.json"))
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[ERR] 库不存在: {DB_PATH}", file=sys.stderr)
        return 1

    os.makedirs(REPORT_DIR, exist_ok=True)
    con = connect_ro(DB_PATH)
    if args.symbol and args.tf:
        ts = load_ts(con, args.symbol, args.tf)
        st = gap_stats(ts, _STD_SEC.get(args.tf))
        sus = find_suspects(st, args.tf)
        con.close()
        print(f"{args.symbol} {args.tf}: {st['n']} 根，主间隔 "
              f"{round(st['main_gap'] / _DAY, 3) if st.get('main_gap') else None} 天，"
              f"占比 {st['main_share']}，可疑 {len(sus)} 行")
        if sus:
            print("可疑 ts（秒）：", sus[:20], "..." if len(sus) > 20 else "")
        if args.apply:
            if not sus:
                print("无可隔离行。")
                return 0
            res = apply_quarantine(DB_PATH, args.symbol, args.tf, sus)
            print(f"[APPLIED] 隔离 {res['moved']} 行，快照 {res['backup']}")
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump({"applied": res}, f, ensure_ascii=False, indent=2)
        return 0

    rows = audit_all(con)
    con.close()
    md = render(rows)
    out_md = os.path.join(REPORT_DIR, "kline_gap_audit.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"报告：{out_md}\nJSON：{args.json_out}")
    for r in rows:
        if r["tf"] == "1d":
            print(f"  {r['symbol']:<9} 1d {r['n']:>5} 根  主间隔占比 {r['main_share']}  "
                  f"可疑 {r['suspects']}")

    # v488：全库 --apply。旧版只有指定 --symbol/--tf 时才隔离，
    # `--apply` 跑全库等于什么都没做（v487 记录"未执行删除"就是这个原因）。
    if args.apply:
        total, groups = 0, 0
        con_ro = connect_ro(DB_PATH)
        for r in rows:
            if not r.get("suspects"):
                continue
            ts = load_ts(con_ro, r["symbol"], r["tf"])
            sus = find_suspects(gap_stats(ts, _STD_SEC.get(r["tf"])), r["tf"])
            if not sus:
                continue
            res = apply_quarantine(DB_PATH, r["symbol"], r["tf"], sus)
            total += res["moved"]
            groups += 1
            print(f"[APPLIED] {r['symbol']} {r['tf']}: 隔离 {res['moved']} 行")
        con_ro.close()
        print(f"[APPLIED] 合计：{groups} 组 / {total} 行已移入 kline_bars_quarantine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
