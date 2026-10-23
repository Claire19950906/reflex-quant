#!/usr/bin/env python3
"""清理历史 E014 误报（reasoning_path 8 段检查器误判）。

背景：
  2026-10-08 检查 reflection_checkers._extract_reasoning_steps 时发现：
  数据库中 132/133 个 E014 决策的 reasoning_path 实际包含 ≥4 个段名
  （[总框架]/[因素定位]/[驱动力]/[水位博弈]/[跨资产资金]/[反共识结论]/[逻辑自检]/[总结]），
  但旧版本 _extract_reasoning_steps 只识别了 1 个段。
  原因：历史上某个版本 _extract_reasoning_steps 用了错误的过滤逻辑，
  当前版本已修复（src_python/reflection_checkers.py:506 简单 in 检查）。

  误报数据：
    Total E014 错误：288 (133 unique decisions)
    False positive：132 decisions (99.2%) ← 误报
    True positive：0 decisions ← 当前真正缺 8 段的
    Empty reasoning_path：1 decision ← 真缺

  误报对反思系统的危害：
    - 污染 review_insights（错误地把"LLM 不会输出 8 段"列为高频盲区）
    - 污染 reflection_alert_log（误触发 E014 阈值告警）
    - 误导 weekly_reflection 报告（看起来 LLM 输出质量差，实际是检查器 bug）

用法：
    python3 clean_stale_e014.py --dry-run    # 只统计误报，不删除
    python3 clean_stale_e014.py              # 实际删除误报 E014 记录
    python3 clean_stale_e014.py --yes        # 跳过确认 prompt（cron 用）
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB = str(Path(__file__).resolve().parents[1] / "trident_event_bus.db")
E014 = "E014"
E014_MIN_STEPS_FOR_PASS = 4   # E014 判定"缺段"：实际 ≥4 段就是误报


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _classify_decisions(conn) -> dict:
    """分类所有 E014 决策：false positive / true positive / empty."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))
    from reflection_checkers import _extract_reasoning_steps

    rows = conn.execute("""
        SELECT DISTINCT e.decision_id
        FROM decision_errors e
        WHERE e.error_code = ?
    """, (E014,)).fetchall()

    fp_ids = []   # false positive: 实际有 ≥4 段
    tp_ids = []   # true positive: 真的缺 ≥4 段
    empty_ids = []  # reasoning_path 为空

    for r in rows:
        did = r["decision_id"]
        d = conn.execute(
            "SELECT reasoning_path FROM ai_decisions WHERE id=?",
            (did,),
        ).fetchone()
        if not d:
            continue
        rp = d["reasoning_path"] or ""
        if len(rp.strip()) < 5:
            empty_ids.append(did)
            continue
        steps_found = _extract_reasoning_steps(rp)
        if len(steps_found) >= E014_MIN_STEPS_FOR_PASS:
            fp_ids.append(did)
        else:
            tp_ids.append(did)

    return {
        "total": len(rows),
        "false_positive": fp_ids,
        "true_positive": tp_ids,
        "empty": empty_ids,
    }


def _count_errors_to_delete(conn, decision_ids: list) -> int:
    """统计这些决策关联的 E014 错误记录数。"""
    if not decision_ids:
        return 0
    placeholders = ",".join("?" * len(decision_ids))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM decision_errors "
        f"WHERE error_code = ? AND decision_id IN ({placeholders})",
        [E014] + decision_ids,
    ).fetchone()
    return row["n"]


def _delete_errors(conn, decision_ids: list) -> int:
    if not decision_ids:
        return 0
    placeholders = ",".join("?" * len(decision_ids))
    cur = conn.execute(
        f"DELETE FROM decision_errors "
        f"WHERE error_code = ? AND decision_id IN ({placeholders})",
        [E014] + decision_ids,
    )
    conn.commit()
    return cur.rowcount


def _resolve_alerts(conn, error_count: int) -> int:
    """如果当前 E014 1h 内 < threshold，自动 resolve 旧的 E014 告警。"""
    row = conn.execute("""
        SELECT COUNT(DISTINCT decision_id) AS n
        FROM decision_errors
        WHERE error_code = ?
          AND created_at >= datetime('now', '-1 hour')
    """, (E014,)).fetchone()
    current_1h = row["n"]
    if current_1h <= 5:  # reflection_alert.DEFAULT_THRESHOLD
        cur = conn.execute("""
            UPDATE reflection_alert_log
            SET resolved = 1,
                resolved_at = datetime('now'),
                resolved_note = ?
            WHERE message LIKE '%E014%' AND resolved = 0
        """, (f"clean_stale_e014: 1h 内 {current_1h} 笔 ≤ 5 阈值，"
              f"自动 resolve（清理了 {error_count} 笔历史误报）",))
        conn.commit()
        return cur.rowcount
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="清理历史 E014 误报（reasoning_path 8 段检查器误判）",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计误报，不删除")
    parser.add_argument("--yes", action="store_true",
                        help="跳过确认 prompt")
    args = parser.parse_args()

    conn = _connect()
    cls = _classify_decisions(conn)
    del_count = _count_errors_to_delete(conn, cls["false_positive"])

    print("=" * 70)
    print("E014 误报清理报告")
    print("=" * 70)
    print(f"总 E014 决策数 (unique)         : {cls['total']}")
    print(f"  误报 (实际有 >=4 段)            : {len(cls['false_positive'])} "
          f"-> 删除 {del_count} 条 decision_errors 记录")
    print(f"  真报 (实际缺 >=4 段)            : {len(cls['true_positive'])} "
          f"-> 保留")
    print(f"  reasoning_path 为空            : {len(cls['empty'])} "
          f"-> 保留（服务端兜底标记）")
    print()

    if cls["false_positive"]:
        print("--- 误报样本（decision_id, 实际8段数）---")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))
        from reflection_checkers import _extract_reasoning_steps
        for did in cls["false_positive"][:3]:
            d = conn.execute(
                "SELECT reasoning_path FROM ai_decisions WHERE id=?",
                (did,),
            ).fetchone()
            steps = _extract_reasoning_steps(d["reasoning_path"] or "")
            print(f"  decision_id={did}: 实际含 {len(steps)}/8 段 -> "
                  f"{', '.join(steps)}")
        print()

    if args.dry_run:
        print("[DRY-RUN] 未实际删除任何记录")
        return 0

    if not args.yes:
        print(f"将删除 {del_count} 条 E014 误报记录。")
        ans = input("确认执行？(yes/no): ").strip().lower()
        if ans not in ("yes", "y"):
            print("已取消")
            return 1

    deleted = _delete_errors(conn, cls["false_positive"])
    print(f"+ 已删除 {deleted} 条 E014 误报记录")

    resolved_alerts = _resolve_alerts(conn, deleted)
    if resolved_alerts:
        print(f"+ 已自动 resolve {resolved_alerts} 条 E014 历史告警")

    report = {
        "deleted_e014_errors": deleted,
        "false_positive_decisions": len(cls["false_positive"]),
        "true_positive_decisions": len(cls["true_positive"]),
        "empty_reasoning_path_decisions": len(cls["empty"]),
        "resolved_alerts": resolved_alerts,
    }
    print()
    print("--- audit JSON ---")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
