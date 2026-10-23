"""
backfill_tech_signals.py — v303: 全品类异动信号回填
==================================================
对 ohlcv_daily 中**每一个**有足够历史的资产跑 tech_detector,
把结果写入 tech_signals。

用户约束落实:
  * 全品类覆盖 —— 不做资产白名单, 遍历所有 asset
  * 不伪造 —— 数据不足的资产直接跳过并报告, 不写任何占位行
  * 不误导 —— 每行记录 data_max_ts / bars_available,
             读取端据此判断"这条信号基于多少数据算出来的"
  * 幂等 —— UNIQUE(asset,ts) + upsert, 重复运行安全

用法:
    python3 backfill_tech_signals.py            # 回填全部
    python3 backfill_tech_signals.py --dry-run  # 只报告不写库
    python3 backfill_tech_signals.py --no-future  # 禁用未来函数(纯因果)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tech_detector as td  # noqa: E402

DB_PATH = BACKEND / "trident_event_bus.db"

# 少于这么多 bar 不足以计算 ma_support(20)+rsi(14)+atr(14) → 跳过
MIN_BARS = 60


def load_assets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT asset, COUNT(*) n, MAX(ts) mx FROM ohlcv_daily "
        "GROUP BY asset ORDER BY asset"
    ).fetchall()
    return [{"asset": r[0], "n": r[1], "data_max_ts": r[2]} for r in rows]


def load_bars(conn: sqlite3.Connection, asset: str, limit: int = 1200) -> list[dict]:
    rows = conn.execute(
        "SELECT asset,ts,open,high,low,close,volume FROM ohlcv_daily "
        "WHERE asset=? ORDER BY ts DESC LIMIT ?", (asset, limit)
    ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        # 防御: 非法 OHLC 直接丢弃, 绝不参与指标计算
        if (d["close"] or 0) <= 0 or (d["high"] or 0) < (d["low"] or 0):
            continue
        d["high"] = float(d["high"]); d["low"] = float(d["low"])
        d["close"] = float(d["close"])
        d["open"] = float(d["open"] or d["close"])
        d["volume"] = float(d["volume"] or 0.0)
        out.append(d)
    return out


def load_liq(conn: sqlite3.Connection) -> dict:
    """读取强平热度(若 liquidation_heat 表存在且有数据)。"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='liquidation_heat'").fetchone()
    if not exists:
        return {}
    rows = conn.execute(
        "SELECT ts, total_usd, long_liq_usd, short_liq_usd, symbol "
        "FROM liquidation_heat").fetchall()
    return {int(r[0]): {"total_usd": r[1], "long_liq_usd": r[2],
                        "short_liq_usd": r[3], "symbol": r[4]} for r in rows}


def bytype_str(d: dict) -> str:
    if not d:
        return "{无}"
    return "{" + ",".join(f"{k}:{v}" for k, v in sorted(d.items())) + "}"


def _symbol_matches(sym: str, asset: str) -> bool:
    """强平 symbol(BTCUSDT) 与 asset(BTC) 是否对应。"""
    return (sym or "").upper().startswith(asset.upper())
def backfill(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    from migrate_tech_signals import migrate
    if not dry_run:
        migrate(conn)

    liq_all = load_liq(conn)
    now = int(time.time())
    report: list[dict] = []
    upserts: list[tuple] = []

    for meta in load_assets(conn):
        a, n_db = meta["asset"], meta["n"]
        if n_db < MIN_BARS:
            report.append({"asset": a, "status": "skipped_insufficient",
                           "bars": n_db, "need": MIN_BARS, "written": 0})
            print(f"  SKIP  {a:<10} bars={n_db} < {MIN_BARS} → 数据不足, 不写入")
            continue

        bars = load_bars(conn, a)
        if len(bars) < MIN_BARS:
            report.append({"asset": a, "status": "skipped_insufficient",
                           "bars": len(bars), "need": MIN_BARS, "written": 0})
            print(f"  SKIP  {a:<10} usable_bars={len(bars)} → 不写入")
            continue

        # 强平数据只对真正有强平数据的资产生效(币安标的), 绝不跨品类套用
        liq = {k: v for k, v in liq_all.items()
               if v.get("symbol") and _symbol_matches(v["symbol"], a)}

        sigs = td.detect_signals(bars, liq_by_ts=liq or None)
        data_max_ts = bars[-1]["ts"]
        stale_days = (now - data_max_ts) // 86400

        by_type: dict[str, int] = {}
        for s in sigs:
            by_type[s["trigger_type"]] = by_type.get(s["trigger_type"], 0) + 1
            sup = next((h["v"].get("level") for h in s["signals"]
                        if h["k"] == "support_test"
                        and isinstance(h["v"], dict)), None)
            upserts.append((
                a, s["ts"], s["close"], s["trigger_type"], s["score"],
                1 if s["uses_future"] else 0,
                s.get("pct_1d"), s.get("pct_3d"), s.get("vol_ratio"),
                sup,
                json.dumps(s["signals"], ensure_ascii=False),
                json.dumps(s.get("merged_types", []), ensure_ascii=False),
                len(bars), data_max_ts, now,
            ))

        report.append({"asset": a, "status": "ok", "bars": len(bars),
                       "data_max_ts": data_max_ts, "stale_days": stale_days,
                       "signals": len(sigs), "by_type": by_type,
                       "has_liq": bool(liq)})
        stale_mark = f" [数据滞后{stale_days}天]" if stale_days > 2 else ""
        print(f"  OK    {a:<10} bars={len(bars):<5} signals={len(sigs):<4} "
              f"{bytype_str(by_type)}liq={'Y' if liq else '-'}{stale_mark}")

    if not dry_run and upserts:
        conn.executemany(
            "INSERT INTO tech_signals "
            "(asset,ts,close,trigger_type,score,uses_future,pct_1d,pct_3d,"
            " vol_ratio,support_level,signal_json,merged_types,bars_available,"
            " data_max_ts,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(asset,ts) DO UPDATE SET "
            "close=excluded.close, trigger_type=excluded.trigger_type, "
            "score=excluded.score, uses_future=excluded.uses_future, "
            "pct_1d=excluded.pct_1d, pct_3d=excluded.pct_3d, "
            "vol_ratio=excluded.vol_ratio, "
            "support_level=excluded.support_level, "
            "signal_json=excluded.signal_json, "
            "merged_types=excluded.merged_types, "
            "bars_available=excluded.bars_available, "
            "data_max_ts=excluded.data_max_ts",
            upserts,
        )
        conn.commit()

    try:
        total = conn.execute("SELECT COUNT(*) FROM tech_signals").fetchone()[0]
    except sqlite3.OperationalError:
        total = None          # dry-run 时表尚未创建
    return {"assets": report, "written": len(upserts),
            "table_total": total, "dry_run": dry_run}
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-future", action="store_true",
                    help="禁用转折点(未来函数), 只保留纯因果信号")
    a = ap.parse_args()
    if a.no_future:
        td.ALLOW_FUTURE = False
        print("[MODE] 未来函数已禁用 (纯因果)")
    else:
        print("[MODE] 转折点启用 (uses_future=1, 用户许可)")

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rep = backfill(conn, dry_run=a.dry_run)
    finally:
        conn.close()
    ok = [r for r in rep["assets"] if r["status"] == "ok"]
    skip = [r for r in rep["assets"] if r["status"] != "ok"]
    print("\n" + "=" * 62)
    print(f"资产覆盖: 成功 {len(ok)} / 跳过 {len(skip)} / 共 {len(rep['assets'])}")
    print(f"写入信号: {rep['written']}  表内总计: {rep['table_total']}"
          f"{'  (dry-run 未落库)' if rep['dry_run'] else ''}")
    for r in skip:
        print(f"  跳过 {r['asset']}: {r['status']} bars={r.get('bars')}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())