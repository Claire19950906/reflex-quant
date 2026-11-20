"""
populate_missed_signals.py — 2026-10-19 v1
============================================
填充 missed_signals 表 (审计发现 0 行 → 必须用真实 critical_price_moves × trade_decisions 推导)

漏检定义 (与 bridge_server.v237_missed_signals 端点一致):
  - 强信号出现: critical_price_moves.algo_score >= 0.7
  - 同期 (±24h) 内无 trade_decision 记录覆盖 → 决策系统未识别

pattern_type:
  - duration_min >= 4320  → A3_continuation_missed (优先)
  - move_pct > 0          → A1_strong_buy_missed
  - move_pct < 0          → A2_strong_sell_missed

去重: 同一 (asset, decision_date) 组合只保留最强信号 (algo_score 最高)

用法:
  python3 src_python/populate_missed_signals.py --dry-run
  python3 src_python/populate_missed_signals.py --commit
"""

import argparse
import sqlite3
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
DAYS = 90


def get_db(readonly=False):
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def compute_missed(conn):
    """与 bridge_server.v237_missed_signals 一致的漏检识别逻辑."""
    cutoff = int(time.time()) - DAYS * 86400
    has_dec = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_decisions'"
    ).fetchone() is not None
    rows = conn.execute(
        "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min, created_at "
        "FROM critical_price_moves "
        "WHERE algo_score >= 0.7 AND ts >= ? "
        "ORDER BY ts DESC",
        (cutoff,),
    ).fetchall()

    # v2026-10-27 (G11) 修复: 原来用 ABS(created_at - ?) 匹配同期决策, 但
    # trade_decisions 的时间列是 decision_ts (REAL epoch) —— created_at 是文本,
    # 相减恒为巨大值 → **每条强信号都被误判成漏检**。这里改成按 decision_ts
    # 匹配 (±24h), 与 bridge 端同一口径。
    dec_by_asset: dict = {}
    if has_dec:
        try:
            for dr in conn.execute(
                "SELECT asset, decision_ts FROM trade_decisions "
                "WHERE decision_ts IS NOT NULL"
            ).fetchall():
                dec_by_asset.setdefault((dr["asset"] or "").upper(), []).append(
                    float(dr["decision_ts"]))
        except sqlite3.OperationalError:
            dec_by_asset = {}

    items = []
    for r in rows:
        if has_dec:
            dts = dec_by_asset.get((r["asset"] or "").upper()) or []
            hit_td = any(abs(d - float(r["ts"])) <= 86400 for d in dts)
            if hit_td:
                continue
            ignored = (
                f"强信号出现时, 同期 (±24h) 无 trade_decision 记录覆盖 — "
                f"决策系统未识别 (asset={r['asset']})"
            )
        else:
            ignored = "DB 中无 trade_decisions 表 — 上游决策源未接入, 所有强信号视为漏检"

        if (r["duration_min"] or 0) >= 4320:
            ptype = "A3_continuation_missed"
        elif r["move_pct"] > 0:
            ptype = "A1_strong_buy_missed"
        else:
            ptype = "A2_strong_sell_missed"

        items.append({
            "asset": r["asset"],
            "decision_date": time.strftime("%Y-%m-%d", time.gmtime(r["ts"])),
            "decision_id": None,
            "signal_id": r["id"],
            "pattern_type": ptype,
            "signal": (
                f"algo_score={r['algo_score']:.2f} "
                f"move_pct={r['move_pct']:.2f} move_kind={r['move_kind']}"
            ),
            "ignored_reason": ignored,
            "confidence_at_time": r["algo_score"],
            "actual_outcome_5d": None,
            "actual_outcome_20d": None,
            "hit": None,
            "created_at": r["created_at"],
            "ts": r["ts"],
        })

    # 去重: (asset, decision_date) 保留 algo_score 最高
    seen: dict = {}
    for it in items:
        k = (it["asset"], it["decision_date"])
        if k not in seen or it["confidence_at_time"] > seen[k]["confidence_at_time"]:
            seen[k] = it
    return list(seen.values())


def dry_run(items):
    print("=" * 70)
    print("DRY RUN — populate_missed_signals.py (no writes)")
    print("=" * 70)
    print(f"\n将写入: {len(items)} 行 (按 asset×date 去重)")
    by_ptype: dict = {}
    by_asset: dict = {}
    for it in items:
        by_ptype[it["pattern_type"]] = by_ptype.get(it["pattern_type"], 0) + 1
        by_asset[it["asset"]] = by_asset.get(it["asset"], 0) + 1
    print("\npattern_type 分布:")
    for k, n in sorted(by_ptype.items()):
        print(f"  {k:<28} {n}")
    print("\nasset 分布 (top 10):")
    for a, n in sorted(by_asset.items(), key=lambda x: -x[1])[:10]:
        print(f"  {a:<8} {n}")
    print("\n前 5 条 sample:")
    for it in items[:5]:
        print(f"  {it['asset']:<6} {it['decision_date']} {it['pattern_type']:<26} "
              f"algo={it['confidence_at_time']:.2f}")


def commit(items):
    if not items:
        print("无数据可写 — 退出.")
        return
    conn = get_db(readonly=False)
    try:
        # v2026-10-27 (G11): 写入时同时给出**错因小类** (与反思侧同一套词表)
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))
            import missed_signal_taxonomy as _mst
            _mst.ensure_columns(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 错因分类不可用: {exc}")
            _mst = None
        cur_dec_dates = list({it["decision_date"] for it in items})
        placeholders = ",".join("?" * len(cur_dec_dates))
        # v334: 结算结果不许被"重新发现"冲掉。
        # 这个作业每小时 DELETE 当天的行再 INSERT, 而 compute_missed() 产出的
        # hit / actual_outcome 恒为 None —— 上一轮算好的命中率会被整批抹平
        # (实测: 结算完 179 行, 一小时后又只剩 25 行, S9 永远停在个位数)。
        # 删之前先按 (asset, decision_date, pattern_type) 快照, 插回后还原。
        _settled: dict = {}
        for _r in conn.execute(
            f"SELECT asset, decision_date, pattern_type, actual_outcome_5d, "
            f"actual_outcome_20d, hit FROM missed_signals "
            f"WHERE decision_date IN ({placeholders}) AND "
            f"(hit IS NOT NULL OR actual_outcome_5d IS NOT NULL)",
            cur_dec_dates,
        ).fetchall():
            # 键用 (asset, decision_date): 实测该组合在表内唯一, 而 pattern_type
            # 会在重跑分类时变 (A4↔C2 之类), 带上它会还原失败 (161 快照只复原 25)。
            _settled[(_r[0], _r[1])] = (_r[3], _r[4], _r[5])
        if _settled:
            print(f"[commit] 快照已结算结果 {len(_settled)} 行 (插回后还原)")
        deleted = conn.execute(
            f"DELETE FROM missed_signals WHERE decision_date IN ({placeholders})",
            cur_dec_dates,
        ).rowcount
        print(f"[commit] 删除旧 batch: {deleted} 行")
        by_ptype: dict = {}
        for it in items:
            dim = sub = reason = ""
            ptype = it["pattern_type"]
            out5 = it.get("actual_outcome_5d")
            out20 = it.get("actual_outcome_20d")
            if _mst is not None:
                try:
                    # 真实结果补齐 (漏掉的信号后来到底涨没涨)
                    if out5 is None and it.get("ts"):
                        out5 = _mst.compute_outcome(conn, it["asset"], int(it["ts"]), 5)
                    if out20 is None and it.get("ts"):
                        out20 = _mst.compute_outcome(conn, it["asset"], int(it["ts"]), 20)
                    res = _mst.classify_missed(
                        conn, dict(it, actual_outcome_5d=out5))
                    dim, sub = res["dimension"], res["subcategory"]
                    reason = res["reason"]
                    ptype = res["pattern_type"]
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] 分类失败 asset={it['asset']}: {exc}")
            by_ptype[ptype] = by_ptype.get(ptype, 0) + 1
            _prev = _settled.get((it["asset"], it["decision_date"]))
            if _prev:
                out5, out20, hit = _prev
            else:
                hit = it["hit"]
            conn.execute("""
                INSERT INTO missed_signals
                  (asset, decision_date, decision_id, signal_id, pattern_type, signal,
                   ignored_reason, confidence_at_time, actual_outcome_5d,
                   actual_outcome_20d, hit, created_at, dimension, subcategory, miss_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                it["asset"], it["decision_date"], it["decision_id"], it["signal_id"],
                ptype, it["signal"], it["ignored_reason"],
                it["confidence_at_time"], out5,
                out20, hit, it["created_at"],
                dim, sub, reason[:300],
            ))
        conn.commit()
        print(f"[commit] 写入 {len(items)} 行真实漏检信号")
        print("[commit] pattern_type 分布: %s" % by_ptype)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    conn = get_db(readonly=True)
    try:
        items = compute_missed(conn)
    finally:
        conn.close()

    if args.commit:
        commit(items)
    else:
        dry_run(items)


if __name__ == "__main__":
    main()