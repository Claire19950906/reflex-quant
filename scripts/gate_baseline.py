"""闸门基线评估：被拦下的方向性决策，事后是否真的不该做。

思路（反事实回放）：
    1. ai_decisions 里 ungated_action 是"模型原本想做的方向"，suggested_action 是
       "闸门之后实际做的"；两者不同即视为被拦下。
    2. 用 kline_bars 里已有的历史K线，回填 entry 时刻价格与 +1h/+4h/+24h 价格。
    3. 按 ungated 方向计算"假如照模型原意执行"的收益，再看闸门拦对了没有。
    4. 对照组：全部放行、同比例随机拦。随机拦用 bootstrap 给分布，避免拿一个
       点估计去和闸门比。

只读数据库，不写入任何业务库；输出 markdown + csv 到 --out 目录。

用法:
    python3 scripts/gate_baseline.py \
        --db backend/trident_event_bus.db \
        --db backend/runtime/backup/pre_prune_20261119_154634.db \
        --out audit
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIRECTIONAL = ("BUY", "SELL")
HORIZONS = (1, 4, 24)  # 小时
# (最大滞后秒数, 平滑带宽) —— 1h/4h 要求 15 分钟内对齐，24h 给到 1 小时
HORIZON_TOLERANCE = {1: 15 * 60, 4: 15 * 60, 24: 60 * 60}
FLAT_BAND = 0.001  # |收益| < 0.1% 记为中性，不判对错
TF_ORDER = ("5m", "1m", "15m", "1h", "4h")
BOOTSTRAP_ROUNDS = 2000

# target_asset → kline_bars.symbol；US_STOCK/NONE 这类无具体标的的单独统计
ASSET_ALIAS = {
    "GOLD": "XAU",
    "SILVER": "XAG",
    "OIL": "WTI",
    "CRUDE": "WTI",
}


@dataclass
class Decision:
    db: str
    did: int
    asset: str
    ungated: str
    suggested: str
    entry_ts: int
    gate_chain: str


@dataclass
class Result:
    decision: Decision
    symbol: str
    blocked: bool
    entry_price: float | None = None
    moves: dict[int, float] = field(default_factory=dict)  # horizon → ret
    reached: dict[int, bool] = field(default_factory=dict)


def parse_ts(raw: str | None, fallback: int | None) -> int | None:
    """ISO 时间串转 epoch 秒；缺失时回落到库里已有的 ts。"""
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp())
        except ValueError:
            pass
    return fallback


def load_decisions(db_paths: list[Path]) -> list[Decision]:
    """跨库读取方向性决策并去重（同一决策在备份库里会重复出现）。"""
    seen: set[tuple] = set()
    out: list[Decision] = []
    for path in db_paths:
        if not path.exists():
            print(f"[skip] 找不到 {path}")
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"[skip] 打不开 {path}: {exc}")
            continue
        with conn:
            rows = conn.execute(
                "select id, target_asset, ungated_action, suggested_action, "
                "entry_time, created_at, ts, coalesce(gate_chain,'') "
                "from ai_decisions where ungated_action in ('BUY','SELL')"
            ).fetchall()
        conn.close()
        for did, asset, ungated, suggested, entry_time, created_at, ts, chain in rows:
            key = (created_at, asset, ungated, suggested)
            if key in seen:
                continue
            seen.add(key)
            ets = parse_ts(entry_time, ts) or parse_ts(created_at, ts)
            if ets is None:
                continue
            out.append(
                Decision(
                    db=str(path),
                    did=did,
                    asset=(asset or "NONE").upper(),
                    ungated=ungated,
                    suggested=suggested,
                    entry_ts=ets,
                    gate_chain=chain,
                )
            )
    return out


def price_at(conn: sqlite3.Connection, symbol: str, ts_ms: int, max_lag_s: int) -> float | None:
    """取 ts 之前最近一根收盘价；滞后超过容差视为缺失。"""
    for tf in TF_ORDER:
        row = conn.execute(
            "select ts, close from kline_bars where symbol=? and timeframe=? "
            "and ts<=? order by ts desc limit 1",
            (symbol, tf, ts_ms),
        ).fetchone()
        if row and row[1]:
            lag = (ts_ms - row[0]) / 1000
            if lag <= max_lag_s:
                return float(row[1])
    return None


def evaluate(decisions: list[Decision], db_path: Path) -> tuple[list[Result], list[Decision]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    results: list[Result] = []
    skipped: list[Decision] = []
    for dec in decisions:
        symbol = ASSET_ALIAS.get(dec.asset, dec.asset)
        blocked = dec.suggested == "HOLD"
        res = Result(decision=dec, symbol=symbol, blocked=blocked)
        entry = price_at(conn, symbol, dec.entry_ts * 1000, 30 * 60)
        if entry is None:
            skipped.append(dec)
            continue
        res.entry_price = entry
        for h in HORIZONS:
            target_ms = (dec.entry_ts + h * 3600) * 1000
            px = price_at(conn, symbol, target_ms, HORIZON_TOLERANCE[h])
            if px is None:
                res.reached[h] = False
                continue
            raw = (px - entry) / entry
            res.moves[h] = raw if dec.ungated == "BUY" else -raw
            res.reached[h] = True
        results.append(res)
    conn.close()
    return results, skipped


def win_rate(values: list[float]) -> float:
    if not values:
        return float("nan")
    return sum(1 for v in values if v > FLAT_BAND) / len(values)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def bootstrap_random_block(results: list[Result], horizon: int, rounds: int = BOOTSTRAP_ROUNDS) -> dict:
    """随机拦同比例时，"拦对率"能到什么水平 —— 闸门的对照基线。"""
    pool = [r.moves[horizon] for r in results if r.reached.get(horizon)]
    blocked_n = sum(1 for r in results if r.blocked and r.reached.get(horizon))
    if not pool or blocked_n == 0:
        return {"mean": float("nan"), "p95": float("nan"), "n": 0}
    rng = random.Random(20261121)
    rates = []
    for _ in range(rounds):
        sample = rng.sample(pool, blocked_n)
        rates.append(sum(1 for v in sample if v < -FLAT_BAND) / blocked_n)
    rates.sort()
    return {
        "mean": mean(rates),
        "p95": rates[int(0.95 * (len(rates) - 1))],
        "n": blocked_n,
    }


def fmt_pct(x: float, digits: int = 2) -> str:
    return "—" if x != x else f"{x * 100:+.{digits}f}%"


def build_report(results: list[Result], skipped: list[Decision], dbs: list[Path]) -> str:
    lines: list[str] = []
    lines.append("# 闸门基线评估（反事实回放）")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append("数据来源：" + "、".join(f"`{p}`" for p in dbs))
    lines.append("")
    lines.append(
        f"样本：方向性决策 {len(results) + len(skipped)} 条，"
        f"其中 {len(results)} 条能在K线里对齐价格，{len(skipped)} 条因标的不具体"
        f"或时间越界被跳过。"
    )
    blocked = [r for r in results if r.blocked]
    passed = [r for r in results if not r.blocked]
    lines.append(f"被闸门拦下 {len(blocked)} 条，放行 {len(passed)} 条。")
    lines.append("")

    lines.append("## 分周期结果")
    lines.append("")
    lines.append("| 周期 | 组 | n | 平均收益（按模型原方向） | 方向正确率 |")
    lines.append("|---|---|---|---|---|")
    for h in HORIZONS:
        for name, group in (("闸门拦下", blocked), ("闸门放行", passed), ("全部照模型执行", results)):
            vals = [r.moves[h] for r in group if r.reached.get(h)]
            if not vals:
                lines.append(f"| {h}h | {name} | 0 | — | — |")
                continue
            lines.append(f"| {h}h | {name} | {len(vals)} | {fmt_pct(mean(vals))} | {win_rate(vals) * 100:.1f}% |")
    lines.append("")

    lines.append("## 拦对率 vs 随机拦")
    lines.append("")
    lines.append("「拦对」= 被拦下的单若照原方向执行会亏钱（收益 < -0.1%）。随机拦对照按同样拦截比例重采样 2000 次。")
    lines.append("")
    lines.append("| 周期 | 闸门拦对率 | 随机拦平均 | 随机拦 95 分位 | n |")
    lines.append("|---|---|---|---|---|")
    for h in HORIZONS:
        vals = [r.moves[h] for r in blocked if r.reached.get(h)]
        gate_rate = (sum(1 for v in vals if v < -FLAT_BAND) / len(vals)) if vals else float("nan")
        bs = bootstrap_random_block(results, h)
        gate_txt = "—" if gate_rate != gate_rate else f"{gate_rate * 100:.1f}%"
        lines.append(
            f"| {h}h | {gate_txt} | {bs['mean'] * 100:.1f}% | {bs['p95'] * 100:.1f}% | {bs['n']} |"
        )
    lines.append("")

    lines.append("## 说明与局限")
    lines.append("")
    lines.append(f"- 收益按缺口计算，未计手续费与滑点；|收益| < {FLAT_BAND * 100:.1f}% 视为中性，不计入正确率。")
    lines.append("- 标的为组合级别的决策（如 US_STOCK）没有对应K线符号，已跳过，未计入任何比例。")
    lines.append("- 样本时间跨度短、单市场，结论属于初步证据，不构成策略有效性的证明。")
    return "\n".join(lines) + "\n"


def write_csv(results: list[Result], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["decision_id", "asset", "ungated_action", "suggested_action",
             "blocked", "entry_ts", "entry_price"]
            + [f"ret_{h}h" for h in HORIZONS]
            + ["gate_count", "gate_blocked_by"]
        )
        for r in results:
            gates = _gate_stats(r.decision.gate_chain)
            writer.writerow(
                [r.decision.did, r.decision.asset, r.decision.ungated,
                 r.decision.suggested, int(r.blocked), r.decision.entry_ts,
                 f"{r.entry_price:.6f}" if r.entry_price else ""]
                + [f"{r.moves[h]:.6f}" if h in r.moves else "" for h in HORIZONS]
                + [gates["count"], gates["blocked_by"]]
            )


def _gate_stats(chain_raw: str) -> dict:
    """从 gate_chain 里取出闸门数量与第一个没放行的闸门名，便于后续归因。"""
    if not chain_raw:
        return {"count": 0, "blocked_by": ""}
    try:
        chain = json.loads(chain_raw)
    except json.JSONDecodeError:
        return {"count": 0, "blocked_by": ""}
    if not isinstance(chain, list):
        return {"count": 0, "blocked_by": ""}
    blocked_by = ""
    for step in chain:
        if not isinstance(step, dict):
            continue
        verdict = str(step.get("verdict", ""))
        if verdict in ("fail", "block", "reject", "veto") and not blocked_by:
            blocked_by = str(step.get("gate", ""))
    return {"count": len(chain), "blocked_by": blocked_by}


def main() -> None:
    ap = argparse.ArgumentParser(description="闸门基线评估")
    ap.add_argument("--db", action="append", required=True, help="可重复，多库合并")
    ap.add_argument("--price-db", help="取K线的库，默认同第一个 --db")
    ap.add_argument("--out", default="audit", help="输出目录")
    args = ap.parse_args()

    dbs = [Path(p) for p in args.db]
    price_db = Path(args.price_db) if args.price_db else dbs[0]
    decisions = load_decisions(dbs)
    results, skipped = evaluate(decisions, price_db)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_path = out_dir / f"gate_baseline_{stamp}.md"
    csv_path = out_dir / f"gate_baseline_{stamp}.csv"
    md_path.write_text(build_report(results, skipped, dbs), encoding="utf-8")
    write_csv(results, csv_path)

    print(f"决策 {len(results) + len(skipped)} 条，可对齐 {len(results)} 条，跳过 {len(skipped)} 条")
    for name, group in (
        ("拦下", [r for r in results if r.blocked]),
        ("放行", [r for r in results if not r.blocked]),
    ):
        for h in HORIZONS:
            vals = [r.moves[h] for r in group if r.reached.get(h)]
            if vals:
                print(f"{name} {h}h n={len(vals)} 均值={fmt_pct(mean(vals))} 正确率={win_rate(vals) * 100:.1f}%")
    print(f"报告: {md_path}")
    print(f"明细: {csv_path}")


if __name__ == "__main__":
    main()
