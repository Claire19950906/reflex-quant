"""opt_field_defaults.py —— 哪些字段在"静默兜底"，以及它们对权重的影响（只读）

背景：`impact_score` 94.5% 恒为 5.0 这条查完后，怀疑同批字段也在同一位置兜底。
本脚本把 ai_decisions 里所有"有默认值"的字段一次性摊开，回答三个问题：

  1. 每个字段的默认值占比多少？（>80% 基本等于该字段没工作）
  2. 有几个不同取值？（<5 个 = 没有区分力）
  3. 它是否进入 base_mult / 权重？（进入且默认占比高的，就是分辨率的真凶）

用法: python3 opt_field_defaults.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# (字段, 默认值, 是否进入 base_mult, 说明)
FIELDS: List[Tuple[str, Any, bool, str]] = [
    ("sentiment_score", 0.0, True, "方向强度 |s|，0 = 无方向"),
    ("impact_score", 5.0, True, "影响力 0-10（门卫分可替代）"),
    ("heat_score", 5.0, True, "关注度 0-10"),
    ("impact_magnitude", 0, False, "旧口径字段，已恒 0"),
    ("asset_impact", 5.0, False, "分品种影响力（权重未用）"),
    ("event_strength", "medium", False, "事件强度档"),
    ("prediction_type", "continuation", False, "延续/反转"),
    ("market_confirmation", "unknown", False, "市场确认"),
    ("expected_horizon", "1-3d", True, "持有期（影响 time 维与 impact 估计）"),
    ("timeframe_match", "intraday", False, "周期匹配"),
    ("event_phase", "mid", False, "事件阶段"),
    ("llm_retry_count", 0, False, "LLM 重试次数"),
]


def audit(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    n = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    out = []
    for field, default, in_base, note in FIELDS:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) c, COUNT(DISTINCT {field}) d FROM ai_decisions"
            ).fetchone()
            dc = conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions WHERE {field} = ?", (default,)
            ).fetchone()[0]
            top = conn.execute(
                f"SELECT {field} v, COUNT(*) c FROM ai_decisions "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 1").fetchone()
        except sqlite3.Error as e:
            out.append({"field": field, "note": f"读不了: {e}"})
            continue
        out.append({
            "field": field, "default": default, "n": n,
            "distinct": int(row["d"] or 0),
            "default_share": (dc / n) if n else 0.0,
            "top_value": top["v"] if top else None,
            "top_share": (top["c"] / n) if (top and n) else 0.0,
            "in_base": in_base, "note": note,
        })
    return out


def main() -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = audit(conn)
    print(f"{'字段':<20}{'取值数':>6}{'默认值占比':>12}{'最大同值占比':>14}  进入base  说明")
    print("-" * 100)
    for r in rows:
        if "note" in r and "field" in r and "n" not in r:
            print(f"{r['field']:<20}  {r['note']}")
            continue
        print(f"{r['field']:<20}{r['distinct']:>6}{r['default_share']:>11.1%}"
              f"{r['top_share']:>13.1%}  {'是' if r['in_base'] else '否':<7}  {r['note']}")

    # base_mult 的实际分布：看它被哪几档字段锁住
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))
        from signals.signal_weight_evaluator import _base_mult
        vals = conn.execute(
            "SELECT sentiment_score s, impact_magnitude m, heat_score h, impact_score i, "
            "       COUNT(*) c FROM ai_decisions "
            "GROUP BY s, m, h, i ORDER BY c DESC LIMIT 6").fetchall()
        print("\nbase_mult 最粗的几档（按决策数排序）:")
        for v in vals:
            b = _base_mult(v["s"], v["m"], v["h"], impact_score=v["i"])
            print(f"  s={v['s']} heat={v['h']} impact={v['i']} → base={b:.3f}  ({v['c']} 条)")
    except Exception as e:
        print(f"  base 分布跳过: {e}")

    print("\n判读:")
    print("  · 默认占比 >80% 且进入 base 的字段, 等于把 base 钉死在常数上")
    print("  · 取值数 <5 的字段没有区分力, 在合成里只贡献稀释")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
