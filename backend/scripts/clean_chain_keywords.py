#!/usr/bin/env python3
"""因果链触发词「主题内聚」清洗 —— 只改派生列 trigger_keywords

背景（2026-11-19 审计发现）
--------------------------
192 条 enabled 链里，138 条把 BUY/SELL 当触发词、160 条把品种名当触发词、
80 条把"美联储/加息/通胀"这类宏观泛词当触发词。结果：任何一条带"美联储"
的新闻都会点燃一批地缘/供应/清算链（实测 66 条决策里"美联储"命中 29 次，
是最高频触发词），匹配退化成"品种+动作"，不是"新闻主题"。

做法
----
1. 先用 chain_keywords.clean_keywords 去掉系统 token / 品种代码（沿用入库口径）；
2. 再按**链自身主题**做词类门控：
     · 宏观词（美联储/加息/降息/FOMC…）只允许留在货币政策主题链
     · 地缘词（伊朗/霍尔木兹/战争/袭击…）只允许留在地缘主题链
     · 供应词（原油供给/减产/库存…）只允许留在供应、地缘主题链
     · 加密资金流词（ETF/流出/多头清算…）只允许留在加密主题链
   主题由链的 name/cause/effect 打分判定（name 权重最高）；判不出主题（other）
   的链不做门控 —— 判不准就不删，宁少不滥。
3. 只写 `trigger_keywords`（匹配/评估用的派生列），**不动 `trigger_words`
   （知识列保留原始记录）**；改前把原值导出 CSV，可回滚。

用法：
  python3 scripts/clean_chain_keywords.py                # dry-run
  python3 scripts/clean_chain_keywords.py --apply         # 落库
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from chain_keywords import clean_keywords

BACKEND = os.path.dirname(_HERE)
DB = os.path.join(BACKEND, "trident_event_bus.db")

# ── 词类词典（小写比对）────────────────────────────────────────────────
MACRO_TOKENS = {
    "美联储", "加息", "降息", "通胀", "利率", "cpi", "非农", "fomc", "fed",
    "经济数据", "政策利率", "货币政策", "美债", "收益率", "降准", "点阵图",
}
# 通胀在"供应"链里也算说得通（供应→通胀），单列允许名单
MACRO_ALLOWED_IN_SUPPLY = {"通胀"}
GEO_TOKENS = {
    "伊朗", "霍尔木兹", "霍尔木兹海峡", "战争", "袭击", "中东冲突", "地缘冲突",
    "停火谈判", "胡塞", "美伊再开战", "军事冲突", "以色列", "俄乌", "制裁",
    "红海", "避险", "地缘", "中东", "爆炸", "海峡", "地缘升级",
    "中东局势升级", "伊朗封锁", "袭击伊朗", "沙特", "俄乌升级", "海峡受袭",
    "israel",
}
# 地缘词允许出现在「供应」链：霍尔木兹/袭击 → 供应中断，本来就是供应链的成因
GEO_ALLOWED_IN_SUPPLY = True
SUPPLY_TOKENS = {
    "原油供给", "供应中断", "减产", "库存", "opec", "产能", "航运", "港口",
    "供应风险", "供给",
}
CRYPTO_TOKENS = {
    "etf", "流出", "资金外流", "多头清算", "liquidation", "cascade", "利空",
    "巨鲸", "交易所", "合约", "爆仓", "清算级差",
}

THEME_LEXICON = {
    "monetary": {"利率", "货币", "央行", "美联储", "加息", "降息", "通胀", "cpi",
                 "fomc", "fed", "政策", "国债", "收益率", "非农", "降准", "点阵图"},
    "geo": {"地缘", "冲突", "战争", "袭击", "伊朗", "霍尔木兹", "中东", "胡塞",
            "以色列", "俄乌", "军事", "红海", "制裁", "避险", "停火"},
    "supply": {"供应", "供给", "原油", "库存", "减产", "opec", "产能", "航运",
               "港口", "运力"},
    "crypto": {"加密", "etf", "清算", "杠杆", "资金流", "流出", "流入", "比特币",
               "btc", "eth", "以太坊", "稳定币", "交易所", "爆仓"},
}
THEME_CN = {"monetary": "货币政策", "geo": "地缘", "supply": "供应",
            "crypto": "加密资金流", "other": "未判定"}


def detect_theme(name: str, cause: str, effect: str) -> tuple[str, dict]:
    """按 name/cause/effect 打分判定主题；name 权重 ×3。"""
    scores = {k: 0.0 for k in THEME_LEXICON}
    fields = ((name or "", 3.0), (cause or "", 1.0), (effect or "", 1.0))
    for text, w in fields:
        low = text.lower()
        for theme, lex in THEME_LEXICON.items():
            for kw in lex:
                if kw in low:
                    scores[theme] += w
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] <= 0:
        return "other", scores
    return best[0], scores


def gate(kept: list[str], theme: str) -> tuple[list[str], list[str]]:
    """按主题门控，返回 (保留, 因跨主题被删)。"""
    stay, drop = [], []
    for t in kept:
        low = t.lower()
        if low in MACRO_TOKENS:
            if theme == "monetary" or (theme == "supply" and low in MACRO_ALLOWED_IN_SUPPLY):
                stay.append(t)
            else:
                drop.append(t)
        elif low in GEO_TOKENS:
            if theme == "geo" or (GEO_ALLOWED_IN_SUPPLY and theme == "supply"):
                stay.append(t)
            else:
                drop.append(t)
        elif low in SUPPLY_TOKENS:
            if theme in ("supply", "geo"):
                stay.append(t)
            else:
                drop.append(t)
        elif low in CRYPTO_TOKENS:
            if theme == "crypto":
                stay.append(t)
            else:
                drop.append(t)
        else:
            stay.append(t)
    return stay, drop


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="runtime/chain_keywords_clean.csv")
    ap.add_argument("--db", default=None, help="指定库文件（默认 trident_event_bus.db）")
    args = ap.parse_args()
    db_path = args.db or DB
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BACKEND, args.out)

    conn = sqlite3.connect(f"file:{db_path}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, chain_id, name, asset, cause, effect, trigger_words,"
            " trigger_keywords FROM causal_chains WHERE enabled=1"
            " AND COALESCE(active,1)=1")]
        changes, emptied, unchanged = [], [], 0
        for r in rows:
            theme, _ = detect_theme(r["name"], r["cause"], r["effect"])
            cr = clean_keywords(r["trigger_words"], max_keywords=12, drop_tickers=True)
            kept, cross = gate(list(cr.kept), theme)
            old = (r["trigger_keywords"] or "").strip()
            new = ",".join(kept)
            if old == new:
                unchanged += 1
                continue
            rec = {
                "id": r["id"], "chain_id": r["chain_id"],
                "name": (r["name"] or "")[:40], "asset": r["asset"],
                "theme": THEME_CN[theme],
                "old_keywords": old, "new_keywords": new,
                "system_removed": ",".join(cr.removed),
                "cross_theme_removed": ",".join(cross),
                "kept_n": len(kept), "removed_n": len(cr.removed) + len(cross),
            }
            changes.append(rec)
            if not kept:
                emptied.append(rec)

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(changes[0].keys()) if changes else ["id"])
            w.writeheader()
            w.writerows(changes)

        print(f"[{'APPLY' if args.apply else 'dry-run'}] enabled 链 {len(rows)} 条："
              f"需改 {len(changes)}，无变化 {unchanged}，清洗后空词表 {len(emptied)}")
        print(f"明细 {out_path}")
        print("\n逐条对比（最多 30 条）：")
        for c in changes[:30]:
            print(f"  #{c['chain_id']} {c['name']} [{c['theme']}] {c['asset']}")
            print(f"     原: {c['old_keywords']}")
            print(f"     新: {c['new_keywords'] or '<空>'}  "
                  f"(删系统词: {c['system_removed'] or '-'} | 删跨主题: {c['cross_theme_removed'] or '-'})")
        if emptied:
            print(f"\n⚠️ 清洗后词表为空的链 {len(emptied)} 条（这些链将不再命中任何新闻，"
                  f"等于失效，需要人工复核或补充真事件词）：")
            for e in emptied:
                print(f"   #{e['chain_id']} {e['name']} [{e['theme']}] 原: {e['old_keywords']}")

        if args.apply:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for c in changes:
                conn.execute("UPDATE causal_chains SET trigger_keywords=?, updated_at=?"
                             " WHERE id=?", (c["new_keywords"], now, c["id"]))
            conn.commit()
            print(f"\n已写入 trigger_keywords（{len(changes)} 条）；trigger_words 知识列未改动。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
