"""backfill_core_slow_moves.py —— 让核心品种的慢涨/慢跌也进 critical_price_moves（v336 P1）

发现（2026-11-14 只读实测）：
  · critical_price_moves 里 182 条 slow_rise/slow_fall **全部**落在
    SKHYNIX / ADA / DOGE / XRP / BNB / fred / USA-LABOR-ICS / UST2Y / UST5Y，
    **核心品种 BTC/XAU/WTI/ETH/SOL 一条都没有**；
  · 但 ohlcv_daily 里 10 月以来核心品种真实存在 ≥3% 的单日波动：
    XAU 2 次、BTC 6 次、WTI 7 次、ETH 11 次、SOL 9 次（共 35 次）；
  · fast_* 全部是 duration_min=60 的 1 小时窗口，也没有一条日级/多日级。

  也就是说"需求 #6：快涨和慢涨都要识别"里的**慢的一半，对核心品种从未落地**。
  归因页看到的慢行情全是非核心品种，自然解释不了 BTC/XAU 的行情。

本脚本：
  1) 用与现网一致的阈值（1d≥3% / 3d≥5% / 5d≥7%，见 backfill_critical_moves_late）；
  2) 只扫核心品种，按 (asset, ts, move_kind) 去重，绝不重复插入；
  3) 每条用 fast_move_detector.enrich_context 补齐 regime/vol/crowd/liq
     （全部带 ts 上界，只取触发时刻及之前，杜绝未来函数）；
  4) 默认 dry-run，`--commit` 才写库。

用法：
  python3 backfill_core_slow_moves.py                    # dry-run
  python3 backfill_core_slow_moves.py --days 400         # 扩大回看
  python3 backfill_core_slow_moves.py --commit           # 写库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

CORE_ASSETS = ("BTC", "XAU", "WTI", "ETH", "SOL")


def suppress_overlapping(moves, min_gap_sec: int = 2 * 86400):
    """同一波行情只留一条：同向、起止相距不足 min_gap 的合并，保留幅度更大的。

    为什么必须做：detect_moves 从每根日线都尝试 1d/3d/5d 三个窗口，
    一段 5% 的上涨会在连续几根 bar 上各命中一次 —— 不去重就会把
    **一次行情记成十几条独立异动**，和 v336 查出的"分母造假"是同一种错误。
    """
    kept = []
    for m in sorted(moves, key=lambda x: (x["ts"], -abs(x["move_pct"]))):
        for k in kept:
            if (k["move_kind"] == m["move_kind"]
                    and abs(int(m["ts"]) - int(k["ts"])) < min_gap_sec):
                if abs(m["move_pct"]) > abs(k["move_pct"]):
                    k.update(m)  # 换成幅度更大的那条
                break
        else:
            kept.append(dict(m))
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--days", type=int, default=90, help="回看天数（默认 90）")
    ap.add_argument("--assets", default=",".join(CORE_ASSETS))
    args = ap.parse_args()

    try:
        import fast_move_detector as fmd
        from backfill_critical_moves_late import detect_moves, fetch_prices
    except Exception as exc:
        print(f"[FATAL] 导入失败: {type(exc).__name__}: {exc}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro" if not args.commit else str(DB_PATH),
                           uri=not args.commit, timeout=30)
    conn.row_factory = sqlite3.Row

    since = int(time.time()) - args.days * 86400
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]

    existing = {(r["asset"], int(r["ts"]), r["move_kind"])
                for r in conn.execute(
                    "SELECT asset, ts, move_kind FROM critical_price_moves "
                    "WHERE move_kind LIKE 'slow%'")}
    # 同一波行情可能已经有 1 小时快异动记录：相隔 12 小时内的不重复补，
    # 否则一次行情在归因/结算里被算两次（v336 "分母造假"的翻版）。
    fast_ts = {}
    for r in conn.execute("SELECT asset, ts FROM critical_price_moves "
                          "WHERE move_kind LIKE 'fast%'"):
        fast_ts.setdefault(r["asset"], []).append(int(r["ts"]))

    new_rows, per_asset = [], {}
    skipped_fast = 0
    for asset in assets:
        prices = fetch_prices(conn, asset, since)
        if len(prices) < 6:
            per_asset[asset] = {"prices": len(prices), "new": 0, "note": "日线不足"}
            continue
        moves = detect_moves(prices)
        raw_n = len(moves)
        moves = suppress_overlapping(moves)
        kept = 0
        for m in moves:
            key = (asset, int(m["ts"]), m["move_kind"])
            if key in existing:
                continue
            if any(abs(int(m["ts"]) - t) <= 12 * 3600 for t in fast_ts.get(asset, [])):
                skipped_fast += 1
                continue
            existing.add(key)
            try:
                base = json.loads(m["context_json"] or "{}")
            except (TypeError, ValueError):
                base = {}
            ctx = fmd.enrich_context(conn, asset, int(m["ts"]), base)
            ctx["source"] = "v336_core_slow"
            m["asset"] = asset
            m["context_json"] = json.dumps(ctx, ensure_ascii=False)
            new_rows.append(m)
            kept += 1
        per_asset[asset] = {"prices": len(prices), "new": kept,
                            "moves": len(moves), "raw": raw_n}

    print(f"核心品种 {assets} · 回看 {args.days} 天 · 日线起点 "
          f"{time.strftime('%Y-%m-%d', time.localtime(since))}")
    for asset, info in per_asset.items():
        print(f"  {asset:<6} 日线 {info['prices']:>4} 根 · 检出 {info.get('moves', 0):>3} · "
              f"新增 {info['new']:>3} {info.get('note', '')}")

    kinds = {}
    ctx_cov = {"regime": 0, "vol_ratio": 0, "crowd_sentiment": 0}
    for m in new_rows:
        kinds[m["move_kind"]] = kinds.get(m["move_kind"], 0) + 1
    for m in new_rows:
        ctx = json.loads(m["context_json"])
        for k in ctx_cov:
            if ctx.get(k) is not None:
                ctx_cov[k] += 1

    print(f"\n新增 {len(new_rows)} 条  {kinds}")
    if skipped_fast:
        print(f"已跳过 {skipped_fast} 条：同品种 12 小时内已有快异动记录（避免同一波行情算两次）")
    if new_rows:
        print("上下文覆盖:", {k: f"{v}/{len(new_rows)}" for k, v in ctx_cov.items()})

    if not args.commit:
        print("\n（dry-run：没有写库。确认后加 --commit）")
        return 0

    conn.executemany(
        "INSERT INTO critical_price_moves "
        "(asset,ts,start_price,peak_price,end_price,move_pct,move_kind,"
        " duration_min,algo_score,context_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(m["asset"], m["ts"], m["start_price"], m["peak_price"], m["end_price"],
          m["move_pct"], m["move_kind"], m["duration_min"], m["algo_score"],
          m["context_json"]) for m in new_rows],
    )
    conn.commit()
    print(f"已写入 {len(new_rows)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
