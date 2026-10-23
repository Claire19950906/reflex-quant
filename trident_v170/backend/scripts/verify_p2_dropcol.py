#!/usr/bin/env python3
"""P2 验证脚本：检查 ai_decisions 结算列清理是否就绪。

Phase:
  1. P2.1 残留检查: src_python/ 全部代码里不应再有 ad.* 结算字段读取
  2. DB 镜像检查: quick_sim_trades 里 settled=1 行的结算字段必须完整
  3. 模拟迁移检查: 在临时 DB 副本上跑 drop_settlement_columns_from_ai_decisions(dry_run=False)
  4. 模块端到端: review_agent / experience_reflector / engine.reflection / api_server SQL 模式
"""
import os
import sys
import shutil
import sqlite3
import tempfile
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_PYTHON = ROOT / "src_python"
sys.path.insert(0, str(SRC_PYTHON))
sys.path.insert(0, str(ROOT))

import db
import config

DB = Path(config.DB_PATH)
if not DB.exists():
    print(f"[FAIL] DB 不存在: {DB}", flush=True)
    sys.exit(1)


BAD = re.compile(
    r"\bad\.(is_correct|forward_pnl|mfe_pct|mae_pct|settled|entry_price|"
    r"exit_price|entry_time|max_price|min_price|max_price_time|"
    r"min_price_time|mfe_time_mins)\b"
)


def section(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


def check(label: str, ok: bool, detail: str = "") -> tuple:
    """返回 (label, ok, detail)，方便 results 列表统一追加。"""
    mark = "[PASS]" if ok else "[FAIL]"
    msg = f"  {mark} {label}"
    if detail:
        msg += f" ({detail})"
    print(msg, flush=True)
    return (label, ok, detail)


def scan_residues() -> list:
    out: list = []
    for py in SRC_PYTHON.rglob("*.py"):
        if py.name == "verify_p2_dropcol.py":
            continue
        if "DELETED_P0" in py.name:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in BAD.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            out.append(f"{py.relative_to(ROOT)}:{line_no} -> {m.group(0)}")
    return out


def phase1_residues(results: list) -> None:
    section("P2.1 残留扫描")
    offs = scan_residues()
    results.append(check("零残留", len(offs) == 0, f"n={len(offs)}"))
    for o in offs[:10]:
        print(f"         {o}", flush=True)


def phase2_mirror(results: list, conn) -> None:
    section("quick_sim_trades 镜像完整性")
    n = int(conn.execute("SELECT COUNT(*) FROM quick_sim_trades WHERE settled=1").fetchone()[0])
    results.append(check("settled=1 行数", n > 0, f"n={n}"))
    for f in ("verdict", "pnl_pct", "entry_price", "exit_price", "mfe_pct", "mae_pct"):
        n_null = int(conn.execute(
            f"SELECT COUNT(*) FROM quick_sim_trades WHERE settled=1 AND {f} IS NULL"
        ).fetchone()[0])
        results.append(check(f"settled=1 中 {f} 非空率", n_null == 0, f"null={n_null}/{n}"))
    rows = conn.execute(
        """SELECT qs.decision_id,
                  qs.verdict AS qs_verdict, ad.is_correct AS ad_verdict,
                  qs.pnl_pct AS qs_pnl, ad.forward_pnl AS ad_pnl,
                  qs.entry_price AS qs_entry, ad.entry_price AS ad_entry
             FROM quick_sim_trades qs
             JOIN ai_decisions ad ON ad.id = qs.decision_id
            WHERE qs.settled=1"""
    ).fetchall()
    inconsistent: list = []
    for r in rows:
        if r["qs_verdict"] != r["ad_verdict"]:
            inconsistent.append(f"verdict diff id={r['decision_id']}")
        if r["qs_pnl"] != r["ad_pnl"]:
            inconsistent.append(f"pnl diff id={r['decision_id']}")
        if r["qs_entry"] != r["ad_entry"]:
            inconsistent.append(f"entry diff id={r['decision_id']}")
    results.append(check("镜像一致性", len(inconsistent) == 0, f"diff={len(inconsistent)}"))
    for inc in inconsistent[:5]:
        print(f"         {inc}", flush=True)


def phase3_drop_simulate(results: list) -> None:
    section("模拟迁移: 临时 DB 副本上跑 DROP COLUMN")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        shutil.copy2(DB, tmp_path)
        conn2 = sqlite3.connect(str(tmp_path))
        conn2.row_factory = sqlite3.Row
        before = {row[1] for row in conn2.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        planned = db.drop_settlement_columns_from_ai_decisions(conn2, dry_run=True)
        results.append(check("dry_run 返回非空", len(planned) > 0, f"n={len(planned)}"))
        dropped = db.drop_settlement_columns_from_ai_decisions(conn2, dry_run=False)
        after = {row[1] for row in conn2.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        results.append(check("真跑返回非空", len(dropped) > 0, f"n={len(dropped)}"))
        results.append(check("列数减少", len(after) < len(before), f"before={len(before)} after={len(after)}"))
        for col in dropped:
            assert col not in after, f"{col} 仍在表中"
        results.append(check("所有 drop 列不在 PRAGMA", True, f"n={len(dropped)}"))
        must_keep = ["id", "news_id", "suggested_action", "reasoning", "target_asset",
                     "market_category", "created_at", "sentiment_score"]
        missing_keep = [c for c in must_keep if c not in after]
        results.append(check("决策上下文字段保留", len(missing_keep) == 0,
            f"missing={missing_keep}" if missing_keep else f"all {len(must_keep)} kept"))
        n_join = int(conn2.execute(
            """SELECT COUNT(*) FROM ai_decisions ad
               LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
               WHERE qs.settled=1"""
        ).fetchone()[0])
        results.append(check("DROP 后 JOIN qs 仍可跑", n_join > 0, f"n={n_join}"))
        conn2.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def phase4_modules(results: list, conn) -> None:
    section("模块端到端 SQL 模式")
    import review_agent
    for scope in ("today", "week", "all"):
        try:
            rows = review_agent._fetch_trades(conn, scope, limit=10)
            results.append(check(f"review_agent._fetch_trades({scope})", True, f"n={len(rows)}"))
        except Exception as e:
            results.append(check(f"review_agent._fetch_trades({scope})", False,
                type(e).__name__ + ": " + str(e)))
    try:
        agg = review_agent._agg_range(conn)
        keys = list(agg.keys())[:3] if agg else []
        results.append(check("review_agent._agg_range", True, f"keys={keys}"))
    except Exception as e:
        results.append(check("review_agent._agg_range", False,
            type(e).__name__ + ": " + str(e)))
    try:
        stats = review_agent._model_stats(conn)
        results.append(check("review_agent._model_stats", True, f"n={len(stats)}"))
    except Exception as e:
        results.append(check("review_agent._model_stats", False,
            type(e).__name__ + ": " + str(e)))
    try:
        sample = conn.execute("""
            SELECT COUNT(*) AS n,
                   AVG(CASE WHEN qs.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
              FROM experience_applications ea
              JOIN ai_decisions ad ON ad.id = ea.decision_id
              JOIN quick_sim_trades qs ON qs.decision_id = ad.id
             WHERE qs.settled = 1
               AND ea.applied_at >= datetime('now', '-168 hours')
        """).fetchone()
        results.append(check("experience_reflector SQL pattern", True, f"n={sample['n']}"))
    except Exception as e:
        results.append(check("experience_reflector SQL", False,
            type(e).__name__ + ": " + str(e)))
    try:
        sid = conn.execute("SELECT id FROM ai_decisions ORDER BY id DESC LIMIT 1").fetchone()
        if sid:
            ctx = conn.execute(
                """SELECT ad.reasoning, qs.entry_price, qs.exit_price,
                          qs.mfe_pct, qs.mae_pct, qs.pnl_pct AS forward_pnl,
                          qs.verdict AS qs_verdict,
                          datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time
                     FROM ai_decisions ad
                     LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                    WHERE ad.id = ?""",
                (int(sid["id"]),),
            ).fetchone()
            keys_n = len(list(dict(ctx).keys())) if ctx else 0
            results.append(check("engine.reflection ctx SQL pattern", True, f"keys={keys_n}"))
        else:
            results.append(check("engine.reflection (no decisions)", False, "no ai_decisions"))
    except Exception as e:
        results.append(check("engine.reflection", False, type(e).__name__ + ": " + str(e)))


def main() -> int:
    results: list = []
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    phase1_residues(results)
    phase2_mirror(results, conn)
    phase3_drop_simulate(results)
    phase4_modules(results, conn)
    section("P2 验证汇总")
    n_pass = sum(1 for r in results if r[1])
    n_total = len(results)
    print(f"\n通过: {n_pass}/{n_total}", flush=True)
    for r in results:
        if not r[1]:
            print(f"  [FAIL] {r[0]}: {r[2]}", flush=True)
    conn.close()
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())