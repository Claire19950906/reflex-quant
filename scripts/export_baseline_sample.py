"""导出闸门基线所需的可公开样本库。

只保留结构化决策字段与决策时间窗附近的K线，丢掉 reasoning / 新闻正文 / 事件原文
这类自由文本，使样本库足够小、可直接提交进仓库，并且任何人 clone 后都能复现同一张
对照表（scripts/gate_baseline.py 的 --db 直接指这个文件即可）。

用法:
    python3 scripts/export_baseline_sample.py \
        --src backend/trident_event_bus.db \
        --out data/gate_baseline_sample.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = (
    "id", "target_asset", "ungated_action", "suggested_action",
    "entry_time", "created_at", "ts", "gate_chain",
)
GATE_REASON_MAX = 60
TFS = ("5m", "15m", "1h", "4h", "1m")


def parse_iso(raw: str | None, fallback: int | None) -> int | None:
    if raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return fallback


def trim_chain(raw: str) -> str:
    """只留闸门名、结论、短理由；reason 截断避免把长分析带进公开样本。"""
    try:
        chain = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return "[]"
    if not isinstance(chain, list):
        return "[]"
    out = []
    for step in chain:
        if not isinstance(step, dict):
            continue
        out.append(
            {
                "gate": str(step.get("gate", ""))[:40],
                "verdict": str(step.get("verdict", ""))[:20],
                "reason": str(step.get("reason", ""))[:GATE_REASON_MAX],
            }
        )
    return json.dumps(out, ensure_ascii=False)


def build(src: Path, out: Path) -> None:
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    rows = src_conn.execute(
        "select " + ", ".join(COLUMNS) +
        " from ai_decisions where ungated_action in ('BUY','SELL')"
    ).fetchall()

    dst = sqlite3.connect(out)
    dst.execute(
        "create table ai_decisions (id integer, target_asset text, ungated_action text, "
        "suggested_action text, entry_time text, created_at text, ts integer, gate_chain text)"
    )
    dst.execute(
        "create table kline_bars (symbol text, timeframe text, ts integer, "
        "open real, high real, low real, close real, volume real)"
    )

    prepared = []
    for row in rows:
        prepared.append(list(row[:-1]) + [trim_chain(row[-1])])
    dst.executemany("insert into ai_decisions values (?,?,?,?,?,?,?,?)", prepared)

    # 按标的取时间窗：决策最早前 1 天 到 最晚后 2 天，覆盖 24h horizon 的回看
    spans: dict[str, list[int]] = {}
    for row in prepared:
        asset = (row[1] or "NONE").upper()
        ets = parse_iso(row[4], row[6] if len(row) > 6 else None)
        if ets is None:
            continue
        span = spans.setdefault(asset, [ets, ets])
        span[0] = min(span[0], ets)
        span[1] = max(span[1], ets)

    total_bars = 0
    for asset, (lo, hi) in spans.items():
        lo_ms = (lo - 86400) * 1000
        hi_ms = (hi + 2 * 86400) * 1000
        for tf in TFS:
            bars = src_conn.execute(
                "select symbol, timeframe, ts, open, high, low, close, volume "
                "from kline_bars where symbol=? and timeframe=? and ts between ? and ? "
                "order by ts",
                (asset, tf, lo_ms, hi_ms),
            ).fetchall()
            if not bars:
                continue
            dst.executemany(
                "insert into kline_bars values (?,?,?,?,?,?,?,?)", bars
            )
            total_bars += len(bars)
            print(f"  {asset} {tf}: {len(bars)} bars")

    dst.execute("create index idx_kline on kline_bars(symbol, timeframe, ts)")
    dst.commit()
    dst.close()
    src_conn.close()
    print(f"决策 {len(prepared)} 条，K线 {total_bars} 根 → {out} ({out.stat().st_size / 1e6:.2f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="导出可公开的基线样本库")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default="data/gate_baseline_sample.db")
    args = ap.parse_args()
    build(Path(args.src), Path(args.out))
    print("导出时间:", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()
