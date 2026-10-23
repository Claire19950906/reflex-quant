#!/usr/bin/env python3
"""每日 cross_net 抽检清单 — 人工核对 AI 是否漏填或瞎填。

从 raw_news 抽近 N 小时内 status=DONE 且 relevance_score 最高的 Top K 条，
列出 content / cross_net_first_seen_ts / cross_net_source / net_origin_info，
供人眼快速对照判断：
  - cross_net_first_seen_ts 精确时间合理 → ✅
  - 'AMBIGUOUS' 但来源含糊 → 标 🔶 人工补判
  - 空字符串但事件明显可溯源 → 标 ❌ 让 AI 重跑或人工补
  - 精确时间荒谬（晚于入库/早于新闻本身）→ 标 ⚠️ 直接修 DB

用法：
    python3 scripts/daily_audit.py                     # 默认近 48h top 5
    python3 scripts/daily_audit.py --top 8 --hours 72  # 改时间窗和条数
    python3 scripts/daily_audit.py --write-md          # 同步写 markdown 到 news_exports/
    python3 scripts/daily_audit.py --since-id 5900     # 只看 id > since 的（避免重复抽）

零写库，只读；可放心挂 cron。
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

DB_PATH = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db"),
)
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "news_exports")
DEFAULT_HOURS = 48
DEFAULT_TOP = 5


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _candidate_news(conn: sqlite3.Connection, hours: int, top: int, since_id: int) -> list:
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
    return conn.execute(
        """
        SELECT id, content, timestamp, relevance_score,
               cross_net_first_seen_ts, cross_net_source, net_origin_info
        FROM raw_news
        WHERE status = 'DONE'
          AND relevance_score IS NOT NULL
          AND timestamp >= ?
          AND id > ?
        ORDER BY relevance_score DESC, id DESC
        LIMIT ?
        """,
        (cutoff, since_id, top),
    ).fetchall()


def _audit_label(r) -> str:
    """根据 cross_net_* 字段给单条加一个易读的判断标签。"""
    ts = (r["cross_net_first_seen_ts"] or "").strip()
    src = (r["cross_net_source"] or "").strip()
    noi = (r["net_origin_info"] or "").strip()
    if ts == "AMBIGUOUS":
        if "候选" in src or "/" in src or "或" in src:
            return "🔶 AMBIGUOUS"
        return "❓ AMBIGUOUS 但来源无并列"
    if ts == "" or ts is None:
        if not noi or noi in ("", "无法确认", "N/A"):
            return "❌ 完全未溯源"
        return "🔶 只有 net_origin_info 无结构化字段"
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        if dt.year < 1990:
            return "⚠️ 时间过早"
        if dt.year > datetime.datetime.now().year + 1:
            return "⚠️ 时间过远"
    except ValueError:
        return "⚠️ 时间格式异常"
    return "✅ 精确"


def _render_console(rows: list, hours: int) -> str:
    out = []
    out.append(f"=== Trident cross_net 每日抽检清单 · {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    out.append(f"窗口：近 {hours}h · 抽 {len(rows)} 条 · 按 relevance_score 倒序")
    out.append("")
    for r in rows:
        label = _audit_label(r)
        out.append(f"━━━ #{r['id']}  {label}  relevance={r['relevance_score']:.2f}  入库={r['timestamp']} ━━━")
        content = (r["content"] or "").strip()[:280].replace("\n", " ")
        out.append(f"  内容  : {content}{'…' if len(r['content'] or '') > 280 else ''}")
        out.append(f"  cn_ts : {(r['cross_net_first_seen_ts'] or '〈空〉')!r}")
        out.append(f"  cn_src: {(r['cross_net_source'] or '〈空〉')!r}")
        out.append(f"  noi   : {(r['net_origin_info'] or '〈空〉')!r}")
        out.append("")
    return "\n".join(out)


def _render_md(rows: list, hours: int) -> str:
    out = [
        f"# Trident cross_net 每日抽检清单",
        f"",
        f"- 生成时间：`{datetime.datetime.now():%Y-%m-%d %H:%M:%S}`",
        f"- 窗口：近 {hours}h",
        f"- 抽检条数：{len(rows)}（按 relevance_score 倒序）",
        f"",
        f"## 标记含义",
        f"",
        f"- ✅ 精确：结构化时间格式正确、来源合理",
        f"- 🔶 AMBIGUOUS：来源含并列表达，符合规范但需人工核对",
        f"- ❓ AMBIGUOUS 但来源无并列：提示模板里要求的两条并列写法未出现",
        f"- ❌ 完全未溯源：cross_net_* 与 net_origin_info 全空",
        f"- ⚠️ 时间异常：年份早于 1990 / 远未来 / 格式异常",
        f"",
    ]
    for r in rows:
        label = _audit_label(r)
        out.append(f"## #{r['id']} {label}")
        out.append("")
        out.append(f"- relevance_score: `{r['relevance_score']}`")
        out.append(f"- 入库时间: `{r['timestamp']}`")
        out.append(f"- 内容:")
        out.append(f"  > {(r['content'] or '').strip().replace(chr(10), ' ')}")
        out.append(f"- cross_net_first_seen_ts: `{(r['cross_net_first_seen_ts'] or '〈空〉')}`")
        out.append(f"- cross_net_source: `{(r['cross_net_source'] or '〈空〉')}`")
        out.append(f"- net_origin_info: `{(r['net_origin_info'] or '〈空〉')}`")
        out.append("")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "audit")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS, help=f"时间窗口（小时，默认 {DEFAULT_HOURS}）")
    p.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"抽取条数（默认 {DEFAULT_TOP}）")
    p.add_argument("--since-id", type=int, default=0, help="只看 id > since 的（避免重复抽）")
    p.add_argument("--write-md", action="store_true", help="同步写 markdown 到 news_exports/")
    p.add_argument("--db", default=DB_PATH, help="DB 路径")
    args = p.parse_args()

    conn = _connect(args.db)
    try:
        rows = _candidate_news(conn, args.hours, args.top, args.since_id)
        if not rows:
            print(f"（窗口 {args.hours}h 内无 status=DONE 且 relevance_score 非空的 raw_news）")
            return 0

        print(_render_console(rows, args.hours))

        if args.write_md:
            os.makedirs(EXPORT_DIR, exist_ok=True)
            fname = f"cross_net_audit_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.md"
            fpath = os.path.join(EXPORT_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(_render_md(rows, args.hours))
            print(f"📝 已写入 {fpath}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())