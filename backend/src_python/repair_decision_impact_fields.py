"""repair_decision_impact_fields.py — 历史决策影响力字段幂等回填 (v497)

背景
----
ai_worker 的服务端兜底此前读的是不存在的 `score` 键（分析结果里只有
`sentiment_score`），与 v323 修过的 verification 判定是同一类残留：

  · low_impact      ：score 恒取 0 → 低影响门槛永远不触发，27 条全 0；
  · attention_score ：score 恒 0 → 只剩保底 25 + asset_impact×4，恒 28；
  · impact_magnitude：score 恒 0 → 反推幅度塌成 0，全表 0。

本轮已在 ai_worker 三处兜底把取数键改为 `sentiment_score or score`，
magnitude 再用 `impact_score/10` 兜底。本脚本对存量决策按**同一口径**
做幂等回填，只补这三个派生字段，不改行情 / 方向 / 成交记录。

口径（与 engine/ai_worker.py 兜底段一致）
-----------------------------------------
  · sentiment = sentiment_score（无则 0）
  · asset_impact = asset_impact（无则 0）
  · event_strength 乘子：low 0.5 / medium 1.0 / high 1.5，缺省 1.0
  · low_impact      = 1 if asset_impact<=3 or |sentiment|<0.10 else 0
  · attention_score = clamp(25 + |sentiment|*60 + asset_impact*4, 25, 100)
  · impact_magnitude:
        主式 |sentiment|*100*max(asset_impact,1)/10*strength_mult；
        主式为 0 时用 impact_score*10*strength_mult 兜底；clamp [0,100]。

用法:
    python3 repair_decision_impact_fields.py            # dry-run
    python3 repair_decision_impact_fields.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import db  # noqa: E402

STRENGTH_MULT = {"low": 0.5, "medium": 1.0, "high": 1.5}


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def derive(row: sqlite3.Row) -> dict:
    sentiment = abs(_f(row["sentiment_score"]))
    ai = _f(row["asset_impact"])
    imp = _f(row["impact_score"])
    es = str(row["event_strength"] or "medium").strip().lower()
    mult = STRENGTH_MULT.get(es, 1.0)

    low_impact = 1 if (ai <= 3 or sentiment < 0.10) else 0

    base = sentiment * 60 + ai * 4
    if str(row["data_anchor_ts"] or "").strip() not in ("", "N/A", "AMBIGUOUS"):
        base += 5
    if str(row["event_type_code"] or "").strip():
        base += 8
    attention = max(25.0, min(100.0, round(base, 1)))

    mag = sentiment * 100 * max(ai, 1) / 10 * mult
    if mag <= 0 and imp > 0:
        mag = imp * 10 * mult
    magnitude = max(0.0, min(100.0, round(mag, 1)))

    return {
        "low_impact": low_impact,
        "attention_score": attention,
        "impact_magnitude": magnitude,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认 dry-run）")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, sentiment_score, asset_impact, impact_score, event_strength, "
        "data_anchor_ts, event_type_code, low_impact, attention_score, impact_magnitude "
        "FROM ai_decisions"
    ).fetchall()

    todo = 0
    for row in rows:
        d = derive(row)
        cur = {
            "low_impact": row["low_impact"],
            "attention_score": _f(row["attention_score"]),
            "impact_magnitude": _f(row["impact_magnitude"]),
        }
        if (int(cur["low_impact"]) == d["low_impact"]
                and abs(cur["attention_score"] - d["attention_score"]) < 0.05
                and abs(cur["impact_magnitude"] - d["impact_magnitude"]) < 0.05):
            continue
        todo += 1
        print(f"  #{row['id']}: low {cur['low_impact']}→{d['low_impact']}  "
              f"attention {cur['attention_score']}→{d['attention_score']}  "
              f"magnitude {cur['impact_magnitude']}→{d['impact_magnitude']}")
        if args.apply:
            conn.execute(
                "UPDATE ai_decisions SET low_impact=?, attention_score=?, "
                "impact_magnitude=? WHERE id=?",
                (d["low_impact"], d["attention_score"], d["impact_magnitude"], row["id"]),
            )

    print(f"\n待回填 {todo} / {len(rows)} 条；" + ("已写库。" if args.apply else "dry-run，未写库（加 --apply 落库）。"))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
