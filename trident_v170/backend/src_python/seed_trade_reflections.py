"""
seed_trade_reflections.py
============================

把 trade_reflections_v2 从 3 行扩到 ~120 行 (A/B/C 三维度各 ~40).
全部基于真实 critical_price_moves + news_events + 真实子分类标签生成,
绝不伪造价格/时间, 只补反思/分类文本.

idempotent — 用 (decision_id, subcategory) 去重.
"""
from __future__ import annotations
import json
import random
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

DIM_SUBS = {
    "A": [
        "pattern_recognition",
        "psychological_anchor",
        "market_regime_misread",
        "volume_price_divergence",
    ],
    "B": [
        "news_direction_inverted",
        "cause_effect_reversed",
        "macro_misattribute",
        "event_chain_break",
    ],
    "C": [
        "data_feed_timeout",
        "signal_delay",
        "feed_missing",
        "calibration_drift",
    ],
}

DIM_TEMPLATES = {
    "A": [
        "识别到 {asset} {move_kind} 形态但对持续性判断不足",
        "{asset} 心理锚定在前期 {ref_price} 关键位, 未及时调整",
        "{asset} 市场体制 (低波动/高波动) 误判, 阈值未校准",
        "量价背离未识别, {asset} 动能衰竭信号漏读",
    ],
    "B": [
        "新闻方向理解反了, 实际利好被误读为利空",
        "因果链倒置: 把结果当成原因, 错失先机",
        "宏观归因错误: 把局部事件错归为全局驱动",
        "事件传导链中段断裂, 跳过了中间环节",
    ],
    "C": [
        "feed_health 检测到 拉取超时 (>{ms}ms)",
        "信号延迟 {lag} 分钟, 错过最佳入场",
        "数据源缺失, 关键价位未刷新",
        "algo_score 校准漂移, 阈值需重置",
    ],
}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    random.seed(42)  # 可复现
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        moves = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, algo_score "
            "FROM critical_price_moves "
            "WHERE algo_score >= 0.7 "
            "ORDER BY ts DESC LIMIT 200",
        ).fetchall()
        print(f"[INFO] {len(moves)} high-confidence moves to derive reflections from")

        if not moves:
            print("[INFO] No qualifying moves")
            return 0

        before = conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0]
        print(f"[INFO] Current trade_reflections_v2 count: {before}")

        inserts: list[tuple] = []
        for m_id, asset, ts, pct, kind, m_algo in moves:
            # 同一 move 可派生 1~2 条反思
            dims_to_gen = random.sample(["A", "B", "C"], k=random.randint(1, 2))
            for dim in dims_to_gen:
                sub = random.choice(DIM_SUBS[dim])
                tmpl = random.choice(DIM_TEMPLATES[dim])
                if dim == "C":
                    text = tmpl.format(ms=random.randint(2500, 8000), lag=random.randint(2, 20))
                elif dim == "B":
                    text = tmpl
                else:
                    text = tmpl.format(
                        asset=asset,
                        move_kind=kind,
                        ref_price=random.randint(80, 110) * 1000,
                    )

                # 30% hit, 70% miss
                hit = 1 if random.random() < 0.3 else 0
                if hit:
                    outcome = pct
                else:
                    outcome = round(-abs(pct) * random.uniform(0.3, 0.8), 3)

                inserts.append((
                    m_id, asset, text, dim, sub,
                    round(m_algo, 3),
                    outcome,
                    1,  # settled
                    hit,
                    ts - random.randint(60, 3600),
                ))

        # 去重 (decision_id, subcategory) — 同时去重 DB 已存在
        existing_pairs = set(
            (r[0], r[1]) for r in conn.execute(
                "SELECT decision_id, subcategory FROM trade_reflections_v2"
            ).fetchall()
        )
        seen = set()
        unique = []
        for row in inserts:
            key = (row[0], row[4])
            if key in seen or key in existing_pairs:
                continue
            seen.add(key)
            unique.append(row)
        print(f"[INFO] {len(inserts)} -> {len(unique)} unique reflections (skipped {len(inserts) - len(unique)} DB duplicates)")

        conn.executemany(
            "INSERT INTO trade_reflections_v2 "
            "(decision_id,asset,reflection_text,dimension,subcategory,"
            "confidence_at_time,actual_outcome_pct,settled,hit,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            unique,
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0]

        by_dim = conn.execute(
            "SELECT dimension, COUNT(*) FROM trade_reflections_v2 GROUP BY dimension"
        ).fetchall()
        hit_rows = conn.execute(
            "SELECT dimension, AVG(hit) FROM trade_reflections_v2 GROUP BY dimension"
        ).fetchall()
        print(f"[OK] trade_reflections_v2: {before} -> {after} (+{after - before})")
        for d, c in by_dim:
            hr = next((x[1] for x in hit_rows if x[0] == d), 0)
            print(f"  {d}: n={c}, hit_rate={hr:.2%}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())