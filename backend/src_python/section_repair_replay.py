#!/usr/bin/env python3
"""批量回放 v423 八段补齐：对历史"缺段"方向单跑一次真实精简调用，统计补齐成功率。

用途：新决策还没来时，先用历史样本给"补齐到底能不能成"一个数字，
避免只靠单测（stub）和一条 11909 的个例。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_REPO = _SRC.parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _sections(text: str) -> Dict[str, Any]:
    from review.reflection_checkers import _ANALYSIS_STEPS, _extract_reasoning_steps
    found = _extract_reasoning_steps(text or "")
    missing = [s for s in _ANALYSIS_STEPS if s not in found]
    return {"found": found, "missing": missing, "missing_n": len(missing)}


def pick_samples(conn, days: int, limit: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.id, d.news_id, d.target_asset, d.suggested_action, "
        "       COALESCE(d.reasoning_path, '') AS rp, d.ts "
        "  FROM ai_decisions d "
        " WHERE d.suggested_action IN ('BUY','SELL') "
        "   AND (d.ts IS NULL OR d.ts = 0 OR d.ts >= strftime('%s','now') - ?) "
        " ORDER BY d.id DESC",
        (int(days) * 86400,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = {"id": r[0], "news_id": r[1], "asset": r[2] or "", "action": r[3], "rp": r[4] or "",
             "ts": r[5]}
        st = _sections(d["rp"])
        if st["missing_n"] >= 2:
            d["before_missing"] = st["missing_n"]
            d["before_found"] = len(st["found"])
            out.append(d)
            if len(out) >= limit:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--out", default="")
    ap.add_argument("--db", default="", help="按指定库采样（默认生产库）")
    args = ap.parse_args()

    import engine.ai_worker as w
    import db

    if args.db:
        import sqlite3
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    else:
        conn = db.get_connection()
    cfg = w._pick_main_model_cfg()
    if not cfg:
        print("[REPLAY] 模型池无可用配置，退出")
        return 2
    samples = pick_samples(conn, args.days, args.limit)
    print(f"[REPLAY] 候选 {len(samples)} 条（近 {args.days} 天、缺≥2 段）")
    # 先读完新闻正文并**关连接**，再逐条调模型：跨多次 LLM 调用一直攥着写连接
    # 会撞上 lock-watchdog（08:04 就被它清理过一次），回放这种后台任务不该占锁。
    for s in samples:
        try:
            row = conn.execute("SELECT content FROM raw_news WHERE id=?", (s["news_id"],)).fetchone()
            s["news"] = str(row[0]) if row and row[0] else ""
        except Exception:
            s["news"] = ""
    conn.close()
    results: List[Dict[str, Any]] = []
    for s in samples:
        news = s.get("news", "")
        res = {"reasoning_path": s["rp"]}
        t0 = time.time()
        ok = w._maybe_repair_sections(
            news, s["asset"], s["action"], res, cfg,
            str(datetime.fromtimestamp(s["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            if s["ts"] else "", tag=f"replay#{s['id']}",
        )
        elapsed = round(time.time() - t0, 1)
        after = _sections(str(res.get("reasoning_path") or ""))
        results.append({**{k: s[k] for k in ("id", "news_id", "asset", "action", "before_missing", "before_found")},
                        "after_missing": after["missing_n"], "after_found": len(after["found"]),
                        "ok": bool(ok), "news_len": len(news), "elapsed_s": elapsed})
        print(f"  #{s['id']} {s['asset']} {s['action']}: 缺{s['before_missing']}→缺{after['missing_n']} "
              f"({elapsed}s)")

    n = len(results)
    success = sum(1 for r in results if r["after_missing"] == 0)
    improved = sum(1 for r in results if r["after_missing"] < r["before_missing"])
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": args.days, "n": n,
        "success_full8": success,
        "success_rate": round(success / n, 3) if n else None,
        "improved": improved,
        "improved_rate": round(improved / n, 3) if n else None,
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in results) / n, 1) if n else None,
        "results": results,
    }
    out_dir = Path(_REPO) / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    base = Path(args.out) if args.out else (out_dir / f"section-repair-replay-{stamp}")
    if not base.is_absolute():
        base = Path(_REPO) / base
    Path(str(base) + ".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# 八段补齐回放（{stamp}）", "",
        f"- 样本：近 {args.days} 天方向单中缺≥2 段的 {n} 条",
        f"- 补成完整八段：{success}/{n} = {summary['success_rate']}",
        f"- 有改善（缺段变少）：{improved}/{n} = {summary['improved_rate']}",
        f"- 平均耗时：{summary['avg_elapsed_s']}s/条（精简通道 mode=b，约 2K token）", "",
        "| id | 品种 | 方向 | 补前缺段 | 补后缺段 | 耗时(s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['id']} | {r['asset']} | {r['action']} | {r['before_missing']} | "
                     f"{r['after_missing']} | {r['elapsed_s']} |")
    Path(str(base) + ".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[REPLAY] 写出 {base}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
