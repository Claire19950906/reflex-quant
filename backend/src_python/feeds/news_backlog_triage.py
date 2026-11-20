#!/usr/bin/env python3
"""新闻积压分级：把待分析的 PENDING 事件排出优先级，模型恢复后先回放最值钱的。

为什么需要它：
  MiniMax 套餐额度到顶后，news_events 里如实积压了约 2,670 条 PENDING。
  额度一旦恢复，如果按 id / ts 顺序无差别回放，会把大量低价值条目
  （重复快讯、无品种归属的宏观噪声）先烧掉 token，真正能出结论的反倒排在后面。

排序依据（全部来自库里已有事实，不引入主观打分）：
  1. 来源历史产出率 —— 同一 source 过去已分析事件里"有明确品种归属"的比例；
     金十这类高产出源优先，实测产出率只有 3% 的源自动降到末位。
  2. 品种命中 —— 标题/正文里命中美元、黄金、加密、美股关键词的加分
     （对齐用户持仓口径：不碰 A 股，A 股权重为 0）。
  3. 时效 —— 超过 24h 的新闻对盘中决策价值衰减，按小时递减。

只读，不改库、不写结论。输出 JSON 便于回放脚本消费。
"""
from __future__ import annotations
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(SRC_DIR))
import db

# 只围绕用户真实持仓：美元 / 黄金 / 加密 / 美股。A 股刻意排除。
ASSET_WORDS = {
    "USD": ("美元", "美联储", "fomc", "cpi", "非农", "国债", "收益率", "降息", "加息", "dxy"),
    "GOLD": ("黄金", "金价", "白银", "comex", "上金所", "央行购金"),
    "CRYPTO": ("比特币", "btc", "以太坊", "eth", "加密", "稳定币", "binance", "coinbase", "etf 流入"),
    "US": ("纳指", "标普", "道指", "nvda", "英伟达", "特斯拉", "tsla", "美股", "财报", "半导体"),
}
DECAY_PER_HOUR = 1.0 / 120.0   # 每小时衰减，20 小时归零

# 用户明确不做 A 股 —— A 股专属源和 A 股专属词一律降权，不占回放额度。
A_SHARE_SOURCES = ("eastmoney", "cninfo", "sse", "szse", "xueqiu", "10jqka", "cn_etf")
A_SHARE_WORDS = ("涨停", "跌停", "龙虎榜", "北向", "沪指", "深成指", "创业板", "科创板",
                 "中证", "主力资金", "游资", "两融", "解禁", "股东大会", "股权激励")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db.get_db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def source_yield(conn: sqlite3.Connection) -> dict:
    """每个来源的历史产出率：已分析事件里"认领到具体品种"的占比。"""
    rows = conn.execute(
        """
        SELECT source,
               COUNT(*) AS total,
               SUM(CASE WHEN asset IS NOT NULL AND asset NOT IN ('', 'NONE') THEN 1 ELSE 0 END) AS owned
        FROM news_events
        WHERE status='DONE' AND score IS NOT NULL
        GROUP BY source
        HAVING total >= 5
        """
    ).fetchall()
    out = {}
    for r in rows:
        total, owned = r["total"], r["owned"] or 0
        out[r["source"]] = owned / total if total else 0.0
    return out


def _asset_hits(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for words in ASSET_WORDS.values() if any(w in low for w in words))


def rank(limit: int = 200, out_path: str | None = None) -> dict:
    conn = _conn()
    yld = source_yield(conn)
    default_yield = 0.15  # 没有历史样本的源给中性偏低权重，避免新源被埋
    rows = conn.execute(
        """
        SELECT id, news_id, ts, source, asset, headline, body, decision_space
        FROM news_events
        WHERE status='PENDING'
        ORDER BY ts DESC
        LIMIT 20000
        """
    ).fetchall()
    now = int(time.time())
    scored = []
    for r in rows:
        text = f"{r['headline'] or ''} {r['body'] or ''}"
        age_h = max(0.0, (now - int(r["ts"] or now)) / 3600.0)
        sy = yld.get(r["source"], default_yield)
        hits = _asset_hits(text)
        # 权重说明：来源产出率是主体（0~1），品种命中最多加 0.4，时效最多扣 1.0
        score = sy + min(hits, 4) * 0.1 + max(0.0, 0.5 - age_h * DECAY_PER_HOUR)
        if any(k in (r["source"] or "").lower() for k in A_SHARE_SOURCES):
            score -= 0.0  # 源本身不降（`eastmoney` 也发美股内容）
        a_hits = sum(1 for w in A_SHARE_WORDS if w in text)
        if a_hits:
            score -= min(a_hits, 3) * 0.3  # A 股专属措辞，回放优先级直接压低
        # 已经有明确品种归属的行，回放成本最低、收益最高
        if r["asset"] and r["asset"] not in ("NONE", ""):
            score += 0.5
        scored.append({
            "id": r["id"],
            "news_id": r["news_id"],
            "ts": r["ts"],
            "age_h": round(age_h, 1),
            "source": r["source"],
            "source_yield": round(sy, 3),
            "asset_hits": hits,
            "asset": r["asset"],
            "priority": round(score, 3),
            "headline": (r["headline"] or "")[:80],
        })
    scored.sort(key=lambda x: x["priority"], reverse=True)
    top = scored[:limit]
    result = {
        "generated_at": int(now),
        "pending_total": len(scored),
        "source_yield": {k: round(v, 3) for k, v in sorted(yld.items(), key=lambda kv: kv[1], reverse=True)},
        "top": top,
    }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=str(Path.home() / ".trident" / "news_replay_priority.json"))
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()
    res = rank(limit=args.limit, out_path=args.out)
    print(json.dumps({
        "pending_total": res["pending_total"],
        "source_yield": res["source_yield"],
        "out": args.out,
    }, ensure_ascii=False, indent=1))
    for item in res["top"][:args.show]:
        print(f"  [{item['priority']:>5}] {item['source'][:16]:<16} 产出{item['source_yield']:.2f} "
              f"{item['age_h']}h前 命中{item['asset_hits']} {item['headline'][:48]}")


if __name__ == "__main__":
    main()
