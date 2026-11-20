"""audit CLI

    python -m audit.cli selftest
    python -m audit.cli run    --input decisions.jsonl --out out/ [--db trident.db]
    python -m audit.cli replay --db trident.db --limit 482 --out out/
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

from .core.context import CheckContext
from .core.pipeline import audit_many
from .core.report import summarize
from .providers import VERSION, SqliteBundle


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{i} 不是合法 JSON：{exc}") from exc
    return rows


def _load_from_db(db: str, limit: int, since: str | None, ids: str | None) -> List[Dict[str, Any]]:
    path = Path(db)
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        where: list[str] = []
        params: list[Any] = []
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
            where.append("id IN (" + ",".join("?" for _ in id_list) + ")")
            params.extend(id_list)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        sql = "SELECT * FROM ai_decisions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def _export_providers(db: str, outdir: Path) -> Dict[str, int]:
    """把库里的 4 组数据导出为 provider 文件（对外交付的最小数据集）。"""
    bundle = SqliteBundle(db)
    try:
        rules, drivers, history, kline = bundle.rules(), bundle.drivers(), bundle.history(), bundle.kline()
    finally:
        bundle.close()

    outdir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "rules.json": {"version": VERSION, "rules": rules.rules},
        "drivers.json": {"version": VERSION, "nodes": drivers.nodes},
        "history.json": {
            "version": VERSION,
            "samples": history.samples,
            "trades": history.trades,
            "decisions": history.decisions,
        },
        "kline.json": {"version": VERSION, "bars": kline.bars},
    }
    counts: Dict[str, int] = {}
    for name, payload in payloads.items():
        (outdir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        if name == "history.json":
            counts["samples"] = len(history.samples)
            counts["trades"] = len(history.trades)
            counts["decisions"] = len(history.decisions)
        elif name == "rules.json":
            counts["rules"] = len(rules.rules)
        elif name == "drivers.json":
            counts["nodes"] = len(drivers.nodes)
        else:
            counts["bars"] = len(kline.bars)
    return counts


def _write_outputs(reports: List[Dict[str, Any]], outdir: Path) -> Dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    rdir = outdir / "reports"
    rdir.mkdir(exist_ok=True)
    for r in reports:
        (rdir / f"{r['trace_id']}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    summary = summarize(reports)
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "summary.md").write_text(_render_md(summary), encoding="utf-8")
    return summary


def _render_md(s: Dict[str, Any]) -> str:
    lat = s["latency_ms"]
    lines = [
        "# 审计回放 baseline",
        "",
        f"- 决策数：**{s['decisions']}**",
        f"- 产出 finding：**{s['findings']}**（{s['findings_per_decision']}/条决策）",
        f"- 缺 remediation/evidence 的 finding：**{s['incomplete_findings']}**",
        f"- 有效审计率：**{s['audited_ratio']:.1%}**（insufficient = 证据不足被短路）",
        f"- 判定依据：决策当时可判 {s.get('basis', {}).get('decision', 0)} 条 / 需事后行情 {s.get('basis', {}).get('outcome', 0)} 条",
        f"- 延迟 P50 / P95 / max：{lat['p50']} / {lat['p95']} / {lat['max']} ms",
        "",
        "## verdict 分布",
        "",
        "| verdict | 条数 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in s["verdicts"].items()]
    lines += ["", "## 错误码分布", "", "| code | 命中 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in s["error_codes"].items()] or ["| （无） | 0 |"]
    lines += ["", "## 各层状态", "", "| 层 | checked | not_applicable | error |", "|---|---|---|---|"]
    lines += [
        f"| {k} | {v.get('checked', 0)} | {v.get('not_applicable', 0)} | {v.get('error', 0)} |"
        for k, v in s["layers"].items()
        if any(v.get(x, 0) for x in ("checked", "not_applicable", "error"))
    ]
    return "\n".join(lines) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit", description="决策审计层 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="用 3 条构造样本验证契约与报告格式")

    p_run = sub.add_parser("run", help="审 decisions.jsonl")
    p_run.add_argument("--input", required=True)
    p_run.add_argument("--out", default="audit_out")
    p_run.add_argument("--db", help="可选：据真实库打开 provider（否则纯离线）")
    p_run.add_argument("--providers", help="可选：用 provider 目录（file bundle）替代数据库")
    p_run.add_argument("--mode", choices=["full", "pretrade"], default="full",
                       help="pretrade=只用决策当时可判的码（进单前门禁）")

    p_rep = sub.add_parser("replay", help="从 ai_decisions 回放真实决策")
    p_rep.add_argument("--db", required=True)
    p_rep.add_argument("--limit", type=int, default=200)
    p_rep.add_argument("--since", default=None)
    p_rep.add_argument("--ids", default=None)
    p_rep.add_argument("--out", default="audit_out")
    p_rep.add_argument("--mode", choices=["full", "pretrade"], default="full")

    p_gate = sub.add_parser("gate", help="同步门禁：补审已落库决策并留痕（W3）")
    p_gate.add_argument("--db", required=True)
    p_gate.add_argument("--limit", type=int, default=0, help="0 = 全部决策")
    p_gate.add_argument("--commit", action="store_true",
                        help="写入 decision_errors / audit_gate_log（默认只算不写）")
    p_gate.add_argument("--enforce-sim", action="store_true",
                        help="只统计：如果开拦截会拦掉哪些方向单")
    p_gate.add_argument("--mode", choices=["pretrade", "full"], default="pretrade")
    p_gate.add_argument("--out", default="audit_out/gate")

    p_exp = sub.add_parser("export-providers", help="把库导出为 providers/ 目录（4 个 JSON）")
    p_exp.add_argument("--db", required=True)
    p_exp.add_argument("--out", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "export-providers":
        counts = _export_providers(args.db, Path(args.out))
        print(json.dumps({"out": str(args.out), "counts": counts}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "selftest":
        from .examples.selftest import run_selftest
        return run_selftest()

    if args.cmd == "run":
        decisions = _load_jsonl(Path(args.input))
        if args.providers:
            ctx = CheckContext.from_file(args.providers)
        elif args.db:
            ctx = CheckContext.from_sqlite(args.db)
        else:
            ctx = CheckContext.null()
        try:
            reports = audit_many(decisions, ctx, strict=False, mode=args.mode)
        finally:
            ctx.close()
        s = _write_outputs(reports, Path(args.out))
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "gate":
        from .gate import decide, coverage, enforce_summary, gate_row

        decisions = _load_from_db(args.db, args.limit or 10**9, None, None)

        if args.enforce_sim:
            ctx = CheckContext.from_sqlite(args.db)
            try:
                out = enforce_summary(decisions, ctx, mode=args.mode)
            finally:
                ctx.close()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        if not args.commit:
            print("只算不写（加 --commit 才落库）", file=sys.stderr)

        conn = sqlite3.connect(args.db, timeout=20.0)
        conn.row_factory = sqlite3.Row
        stats = {"gated": 0, "reject": 0, "persisted": 0, "errors": 0}
        try:
            for d in decisions:
                try:
                    res = gate_row(conn, int(d["id"]), mode=args.mode,
                                   persist_findings=args.commit, outdir=args.out,
                                   gate_action="shadow")
                    conn.commit()
                    if res:
                        stats["gated"] += 1
                        stats["reject"] += 1 if res["verdict"] == "reject" else 0
                        stats["persisted"] += int(res.get("persisted") or 0)
                except Exception as exc:
                    stats["errors"] += 1
                    print(f"  gate {d.get('id')} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            cov = coverage(conn)
        finally:
            conn.close()
        print(json.dumps({"stats": stats, "coverage": cov}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "replay":
        decisions = _load_from_db(args.db, args.limit, args.since, args.ids)
        ctx = CheckContext.from_sqlite(args.db)
        try:
            reports = audit_many(decisions, ctx, strict=False, mode=args.mode)
        finally:
            ctx.close()
        s = _write_outputs(reports, Path(args.out))
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
