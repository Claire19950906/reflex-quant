#!/usr/bin/env python3
"""R5 路径 A：把验证结果写入 review_insights 表 + 自动停用 failed 事件型 insight。

逻辑：
  - 给 review_insights 增加 verification 列（score/verdict/method/sample_n/kline_n/verified_at）
  - 对 verdict='failed' 且 insight_type='event' 且 asset != '' 的 insight，active=0（停用）
  - manual_deactivated=1（标记是自动停用，避免和用户手动混淆）
  - 输出变更报告
"""
from __future__ import annotations
import sqlite3
import json
import time
from pathlib import Path
import db


def ensure_verification_columns():
    """确保 review_insights 表有 verification 相关列。

    铁律1：schema 迁移只在 db.py 定义。此处不再裸 ALTER TABLE，
    而是委托 db._ensure_columns 应用 db._REVIEW_INSIGHTS_COLUMNS（含 verification_* 列）。
    """
    conn = db.get_connection()
    try:
        before = {r[1] for r in conn.execute("PRAGMA table_info(review_insights)").fetchall()}
        db._ensure_columns(conn, "review_insights", db._REVIEW_INSIGHTS_COLUMNS)
        after = {r[1] for r in conn.execute("PRAGMA table_info(review_insights)").fetchall()}
        conn.commit()
        return sorted(after - before)
    finally:
        conn.close()


def load_verification_results(path="/tmp/insight_verification_report.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def backup_review_insights():
    """备份 review_insights 表到 review_insights_backup 表（幂等）。"""
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS review_insights_backup")
    cur.execute(
        "CREATE TABLE review_insights_backup AS SELECT * FROM review_insights"
    )
    n = cur.execute("SELECT COUNT(*) FROM review_insights_backup").fetchone()[0]
    conn.commit()
    return n


def write_verification_results(results, dry_run=False, deactivate_failed=False, conn=None):
    """把 verification_score/verdict 写入每条 insight 的新字段。

    deactivate_failed=False 时只写客观验证记录，不停用任何 insight。
    停用（active=0, manual_deactivated=1）仅在明确指定 --deactivate-failed 时执行，
    因为 XAU placeholder / 0 trades 会导致 failed 判定置信度不足。
    """
    own_conn = conn is None
    if conn is None:
        conn = db.get_connection()
    cur = conn.cursor()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n_written = 0
    n_event_failed = 0
    n_event_partial = 0
    n_to_deactivate = 0
    for r in results:
        insight_id = r["id"]
        verdict = r.get("verdict", "")
        real_signal = r.get("real_signal_pct")
        kline_n = sum(h.get("n", 0) for h in r.get("kline_verif", {}).get("horizons", []))
        sample_n = r.get("sample_n", 0)
        method = r.get("insight_type", "")

        is_event_with_asset = (
            method == "event" and r.get("asset", "") and verdict == "failed"
        )
        if is_event_with_asset:
            n_event_failed += 1
            n_to_deactivate += 1

        if verdict == "partial" and method == "event" and r.get("asset", ""):
            n_event_partial += 1

        if dry_run:
            continue

        # 1. 写入 verification_* 字段（客观记录，非破坏性）
        cur.execute("""
            UPDATE review_insights SET
              verification_score = ?,
              verification_verdict = ?,
              verification_method = ?,
              verification_sample_n = ?,
              verification_kline_n = ?,
              verified_at = ?
            WHERE id = ?
        """, (
            real_signal,
            verdict,
            method,
            sample_n,
            kline_n,
            now,
            insight_id,
        ))

        # 2. 仅在显式 opt-in 时停用 failed 事件型
        if deactivate_failed and is_event_with_asset:
            cur.execute("""
                UPDATE review_insights SET
                  active = 0,
                  manual_deactivated = 1
                WHERE id = ?
            """, (insight_id,))

        n_written += 1
    conn.commit()
    stats = {
        "n_written": n_written,
        "n_event_failed": n_event_failed,
        "n_event_partial": n_event_partial,
        "n_to_deactivate": n_to_deactivate,
        "now": now,
    }
    if own_conn:
        conn.close()
    return stats


def main():
    import argparse
    p = argparse.ArgumentParser(description="R5 路径 A：应用验证结果 + 可选停用 failed 事件型 insight")
    p.add_argument("--input", "-i", default="/tmp/insight_verification_report.json")
    p.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p.add_argument("--deactivate-failed", action="store_true",
                   help="停用 failed 事件型 insight（默认不停用，因为数据基础不足）")
    args = p.parse_args()

    print("=" * 70)
    print("R5 路径 A：应用验证结果")
    print("=" * 70)

    added = ensure_verification_columns()
    if added:
        print(f"[schema] 添加 {len(added)} 列: {added}")
    else:
        print("[schema] verification 列已存在，无需添加")

    results = load_verification_results(args.input)
    print(f"[load] {len(results)} 条 insights 加载完毕")

    if not args.dry_run:
        n_backup = backup_review_insights()
        print(f"[backup] review_insights 已备份到 review_insights_backup ({n_backup} 条)")

    stats = write_verification_results(
        results, dry_run=args.dry_run, deactivate_failed=args.deactivate_failed
    )
    print(f"[apply] {'dry-run ' if args.dry_run else ''}写入 {stats['n_written']} 条")
    print(f"        event+failed 数量: {stats['n_event_failed']}")
    print(f"        event+partial 数量: {stats['n_event_partial']}")
    print(f"        {'将停用' if args.deactivate_failed else '停用候选（未执行）'}: "
          f"{stats['n_to_deactivate']} 条")

    if not args.dry_run:
        print(f"\n[done] 数据库已更新 @ {stats['now']}")
    if not args.deactivate_failed:
        # 动态查询真实 trades / kline 状态
        try:
            import sqlite3 as _sqlite3
            import config as _config
            _db = _sqlite3.connect(_config.DB_PATH)
            _c = _db.cursor()
            _c.execute("SELECT COUNT(*), SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) FROM quick_sim_trades")
            _qt, _qs = _c.fetchone()
            _c.execute("SELECT COUNT(*) FROM kline_bars WHERE symbol='XAU' AND timeframe='1h' AND ABS(close-4139.28) < 0.01")
            _xph = _c.fetchone()[0]
            _c.execute("SELECT COUNT(*) FROM kline_bars WHERE symbol='XAU' AND timeframe='1h'")
            _xt = _c.fetchone()[0]
            _db.close()
            _xph_pct = round(100 * _xph / max(_xt, 1), 1)
            print(f"\n[warn] 未停用任何 insight —— 当前数据状态:")
            print(f"  1. quick_sim_trades 总 {_qt} 条（settled {_qs} 条），trades 流式实时写入")
            print(f"  2. kline_bars XAU 1h 占位 {_xph_pct}%（4139.28 = 国内现货兜底合成值，非真实 OKX swap）")
            print(f"  3. 18 条 verified 中需先审 XAU/地缘类是否含真信号 vs 巧合 WIN，方可批量停用 failed")
        except Exception as exc:
            print(f"\n[warn] 未停用任何 insight —— 数据状态查询失败: {exc}")
    print("\n[next] 建议下一步：")
    print("  1. 检查 review_insights 的 verification_* 字段")
    print("  2. 用 --deactivate-failed 单独停用可信度高的 failed/event 类")
    print("  3. 等 XAU OKX swap 成功率提升后重跑 verification")


if __name__ == "__main__":
    main()
