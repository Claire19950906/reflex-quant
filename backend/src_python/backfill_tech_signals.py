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
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tech_detector as td

DB_PATH = BACKEND / "trident_event_bus.db"

# 少于这么多 bar 不足以计算 ma_support(20)+rsi(14)+atr(14) → 跳过
MIN_BARS = 60


def load_assets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT asset, COUNT(*) n, MAX(ts) mx FROM ohlcv_daily "
        "GROUP BY asset ORDER BY asset"
    ).fetchall()
    return [{"asset": r[0], "n": r[1], "data_max_ts": r[2]} for r in rows]


def load_bars(conn: sqlite3.Connection, asset: str,
              limit: int | None = 1200) -> list[dict]:
    """装载 K 线。limit=None 表示**全部历史**（只在 `--history` 显式回填时用）。

    为什么要有 None：定时任务用 1,200 根是为了省算力，但"--history 重算历史"
    如果仍被 1,200 根截断，window 之外的老行就永远换不掉 —— 那是旧检测口径产出的
    信号，和重算后的行混在一张表里做统计，等于把两个版本的数据当成同一套结论。
    """
    if limit is None:
        rows = conn.execute(
            "SELECT asset,ts,open,high,low,close,volume FROM ohlcv_daily "
            "WHERE asset=? ORDER BY ts DESC", (asset,)
        ).fetchall()
    else:
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
def backfill(conn: sqlite3.Connection, dry_run: bool = False,
             include_history: bool = False,
             ignore_epoch: bool = False) -> dict:
    from migrate_tech_signals import migrate
    if not dry_run:
        migrate(conn)

    liq_all = load_liq(conn)
    now = int(time.time())
    report: list[dict] = []
    upserts: list[tuple] = []
    replace_ranges: dict[str, tuple[int, int]] = {}
    # v340 纪元闸门: 该作业会把**全部历史 bar** 重算一遍并 upsert。
    # 清空后实测 11,975 行 tech_signals 全部是 epoch 之前重新长回来的
    # （ts 是 K 线时间而不是计算时间，所以"重算历史"看起来像"旧数据没清掉"）。
    # 只写 epoch 之后的信号；历史重算若确实需要，另开显式回填。
    epoch_ts = 0
    try:
        row = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
        if row and row[0]:
            epoch_ts = int(datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        epoch_ts = 0

    for meta in load_assets(conn):
        a, n_db = meta["asset"], meta["n"]
        if n_db < MIN_BARS:
            report.append({"asset": a, "status": "skipped_insufficient",
                           "bars": n_db, "need": MIN_BARS, "written": 0})
            print(f"  SKIP  {a:<10} bars={n_db} < {MIN_BARS} → 数据不足, 不写入")
            continue

        bars = load_bars(conn, a, None if include_history else 1200)
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
        # 该品种本轮能算出的信号时间下界：纪元闸门或整段历史
        # v477: ignore_epoch=True 时按「滚动的最近 1,200 根」重算, 不套纪元闸门。
        # 原因: tech_signals 的 ts 是 **K 线时间**(日线固定 08:00), 纪元是"重置时刻"(08:04),
        # 于是每根日线都落在纪元之前 → 闸门把全部信号滤掉 → 表恒为 0 行,
        # 下游形态证据 / 命中率统计全部无料可用。派生缓存本就该从干净行情重算。
        win_lo = ((max(epoch_ts, bars[0]["ts"]) if (epoch_ts and not include_history and not ignore_epoch)
                   else bars[0]["ts"]))
        # 即使本轮 0 信号也要登记：旧版残留（废弃的未来函数反转、旧 0.95 打分）
        # 必须随窗口一起清掉，否则桥接按 min_score 会捞到脏行。
        replace_ranges[a] = (int(win_lo), int(data_max_ts))
        stale_days = (now - data_max_ts) // 86400

        by_type: dict[str, int] = {}
        for s in sigs:
            # v341: 默认只写纪元之后的信号（避免每轮把历史重算一遍刷回旧行）。
            # 但 tech_signals 是**从干净行情重算出来的派生缓存**，ts 是 K 线时间，
            # 天生就是历史的 —— 闸门全开的结果是表里 0 行，下游形态证据/命中率
            # 统计全部无料可用。需要历史时显式传 --history（仍受每品种 1,200 根上限约束）。
            if epoch_ts and not include_history and not ignore_epoch and int(s["ts"]) < epoch_ts:
                continue
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
        # v451：先删掉窗口内的旧信号再整体重写。
        # 旧版只 upsert，算不出来的存量行会留在表里（例如已废弃的未来函数反转、
        # 或旧打分 0.95 的行），桥接/影子账本按 min_score 一捞就会捞到脏数据。
        # tech_signals 是从干净行情重算的派生缓存，窗口内全量替换是安全且必要的。
        for a, (lo, hi) in replace_ranges.items():
            conn.execute("DELETE FROM tech_signals WHERE asset=? AND ts>=? AND ts<=?",
                         (a, lo, hi))
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
    ap.add_argument("--history", action="store_true",
                    help="连历史信号一起写（默认只写纪元之后）")
    ap.add_argument("--no-future", action="store_true",
                    help="兼容旧参数：未来函数本来就已默认关闭，此参数现在无副作用")
    ap.add_argument("--lookahead-audit", action="store_true",
                    help="⚠ 审计专用：额外产出 uses_future=1 的旧版转折点，不得用于决策/统计")
    a = ap.parse_args()
    td.ALLOW_FUTURE = bool(a.lookahead_audit)
    td.CONFIG["enable_reversal_future"] = bool(a.lookahead_audit)
    if a.lookahead_audit:
        print("[MODE] ⚠ 已启用未来函数审计信号（uses_future=1，仅供对照）")
    else:
        print("[MODE] 纯因果：转折点用已确认枢轴 + 反向幅度触发，不看未来")

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # v451：窗口全量替换会在一个事务里做 27 个 DELETE + 插入，必须给足等锁时间，
    # 否则高峰期会被别的 cron 的写事务顶掉（默认 5 秒不够）。
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        rep = backfill(conn, dry_run=a.dry_run, include_history=a.history)
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