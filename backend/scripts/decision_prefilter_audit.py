# -*- coding: utf-8 -*-
"""decision_prefilter_audit.py — 决策链前置过滤的只读体检（v406 / P3）

要回答两个问题
--------------
1. **"重复决策"到底是不是重复？** 504 条决策只对应 323 条新闻，看着像 36% 冗余。
   实测是**按品种扇出**：一条宏观新闻会分别给 BTC / XAU / ETH 各做一次决策
   （news 29108 → (BTC,XAU,ETH,BTC)），这是设计，不是浪费。
   真正意义上的重复只有"同一 news + 同一 target 出现多次且 action 不同"那一小撮。
   本脚本把它分开数，避免拿错的数去改错的代码。

2. **`target_none` 那 274 条能不能在进 LLM 之前就挡掉？** 仓库已有
   `news_router.pool_out_reason`（v396 L0-POOL，黑名单式：命中"公司行为 + 公司主体"才挡），
   实测只覆盖 11.2%。这里试一个**白名单式**的候选规则：
   "没有任何池内锚点（BTC/XAU/WTI/ETH/SOL 及别名）也没有宏观驱动词（美联储/CPI/非农/
   OPEC/关税/地缘/央行…）→ 不进分析"。黑名单挡不住的"产品发布/人事表态/机构观点"
   会被这条挡掉，但也可能误伤，所以先出混淆矩阵再决定。

用法
----
    python3 scripts/decision_prefilter_audit.py            # 汇总
    python3 scripts/decision_prefilter_audit.py --samples 12   # 附误伤样本
    python3 scripts/decision_prefilter_audit.py --json
纯只读，不写库。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "src_python") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src_python"))

POOL_ASSETS = {"BTC", "XAU", "WTI", "ETH", "SOL", "XAG", "XRP", "DOGE", "ADA", "BNB"}
# 池内锚点：品种名/别名/代码
ANCHORS = ["btc", "bitcoin", "比特币", "xau", "黄金", "金价", "gold",
           "wti", "原油", "油价", "crude", "brent", "布伦特", "opec",
           "eth", "ethereum", "以太坊", "sol", "solana",
           # 交易所/链上事件同样能打到 BTC/ETH（"币安恢复提现"实测被当作误伤）
           "币安", "binance", "coinbase", "交易所", "稳定币", "usdt", "usdc",
           "链上", "钱包", "矿工", "减半", "etf 资金"]
# 宏观驱动词：没有具体标的也可能影响全池
MACRO = ["美联储", "加息", "降息", "利率决议", "fomc", "cpi", "通胀", "非农", "就业数据",
         "关税", "制裁", "地缘", "战争", "袭击", "冲突", "央行", "缩表", "qe", "流动性",
         "国债收益率", "美元指数", "衰退", "gdp", "pmi", "财政部", "鲍威尔"]


def _db_path() -> str:
    try:
        import db
        return db.get_db_path()
    except Exception:
        return str(BACKEND / "trident_event_bus.db")


def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


# 地缘/供给类锚点：标题里没有"原油/黄金"，但事件本身能打到 WTI/XAU
# （实测"俄外长表态""无人机袭击""航母部署"这类都会被 LLM 正确绑到 WTI/XAU，
#  白名单里不给出这一组，误伤率 26%）
GEOPOL = ["中东", "伊朗", "以色列", "乌克兰", "俄罗斯", "俄", "胡塞", "霍尔木兹", "红海",
          "航母", "袭击", "无人机", "空袭", "停火", "谈判", "制裁", "出口禁令", "禁运",
          "减产", "增产", "库存", "钻井", "炼厂", "罢工", "封锁", "海峡"]


# 非交易类主体：产品发布 / 人事 / 司法 / 文体。出现这些词且没有任何池内/宏观/地缘锚点，
# 基本可以断定"这条新闻与五个品种无关" —— 这是**黑名单式**扩充，比纯白名单安全得多。
NONMARKET = ["发布", "推出", "开卖", "售价", "机型", "新品", "上市首发", "发布会",
             "辞职", "免职", "任命", "当选", "弹劾", "人事",
             "起诉", "判决", "庭审", "判刑", "诉讼",
             "电影", "明星", "球赛", "夺冠", "演唱会", "综艺"]


def whitelist_block(title: str, body: str, with_geopol: bool = True) -> str:
    """白名单式候选规则：非空 = 建议不进分析。"""
    text = (str(title or "") + " " + str(body or "")[:400]).lower()
    if any(a in text for a in ANCHORS):
        return ""
    if any(m in text for m in MACRO):
        return ""
    if with_geopol and any(g in text for g in GEOPOL):
        return ""
    return "无池内锚点、无宏观、无地缘供给"


def fanout(conn) -> dict:
    rows = conn.execute(
        """SELECT news_id, COUNT(*) n, COUNT(DISTINCT COALESCE(target_asset,'')) t,
                  COUNT(DISTINCT suggested_action) a
           FROM ai_decisions GROUP BY news_id""").fetchall()
    groups = [r for r in rows if r["n"] > 1]
    return {
        "decisions": conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0],
        "distinct_news": len(rows),
        "groups_multi": len(groups),
        "fanout_by_asset": sum(1 for r in groups if r["t"] > 1),
        # 真正的重复：同一新闻、同一品种、出现多次
        "true_dup_groups": conn.execute(
            """SELECT COUNT(*) FROM (SELECT news_id, COALESCE(target_asset,'')
               FROM ai_decisions GROUP BY 1,2 HAVING COUNT(*)>1)""").fetchone()[0],
        "conflict_action_groups": sum(1 for r in groups if r["a"] > 1),
    }


def prefilter_matrix(conn, samples: int = 0) -> dict:
    from feeds import news_router as nr
    rows = conn.execute(
        """SELECT d.news_id, COALESCE(rn.content, ne.headline, '') body,
                  ne.headline title, COALESCE(d.target_asset,'') asset,
                  COALESCE(d.analysis_degraded_reason,'') reason
           FROM ai_decisions d
           LEFT JOIN news_events ne ON ne.news_id = d.news_id
           LEFT JOIN raw_news rn ON rn.id = d.news_id
           GROUP BY d.news_id""").fetchall()
    stat = {"news": 0, "degraded": 0, "router_blocks": 0, "wish_blocks": 0,
            "wish_block_on_degraded": 0, "wish_block_with_pool_asset": 0,
            "router_block_with_pool_asset": 0}
    false_positive = []
    for r in rows:
        stat["news"] += 1
        t, b = str(r["title"] or ""), str(r["body"] or "")
        is_deg = bool(r["reason"])
        stat["degraded"] += 1 if is_deg else 0
        try:
            rb = nr.pool_out_reason(t[:80], b)
        except Exception:
            rb = ""
        wb = whitelist_block(t, b)
        wb1 = whitelist_block(t, b, with_geopol=False)
        wb3 = ""
        if wb and any(m in (t + " " + b[:400]).lower() for m in NONMARKET):
            wb3 = "白名单+非交易类主体（发布/人事/司法/文体）"
        if rb:
            stat["router_blocks"] += 1
            if r["asset"] in POOL_ASSETS:
                stat["router_block_with_pool_asset"] += 1
        if wb1:
            stat["v1_blocks"] = stat.get("v1_blocks", 0) + 1
            if r["asset"] in POOL_ASSETS:
                stat["v1_block_with_pool_asset"] = stat.get("v1_block_with_pool_asset", 0) + 1
        if wb3:
            stat["v3_blocks"] = stat.get("v3_blocks", 0) + 1
            if is_deg:
                stat["v3_block_on_degraded"] = stat.get("v3_block_on_degraded", 0) + 1
            if r["asset"] in POOL_ASSETS and not is_deg:
                stat["v3_block_with_pool_asset_ok"] = \
                    stat.get("v3_block_with_pool_asset_ok", 0) + 1
        if wb:
            stat["wish_blocks"] += 1
            if is_deg:
                stat["wish_block_on_degraded"] += 1
            if r["asset"] in POOL_ASSETS:
                # 误伤：LLM 真的绑到了池内品种，说明这条新闻是有信息的
                stat["wish_block_with_pool_asset"] += 1
                # 更严格的误伤口径：只有"没降级"的行才算真丢信息 —— 降级行本来就没产出
                if not is_deg:
                    stat["wish_block_with_pool_asset_ok"] = \
                        stat.get("wish_block_with_pool_asset_ok", 0) + 1
                if len(false_positive) < 20:
                    false_positive.append({"news_id": r["news_id"], "asset": r["asset"],
                                           "title": t[:60]})
    stat["false_positive"] = false_positive[:samples] if samples else false_positive[:3]
    return stat


def main(argv=None):
    ap = argparse.ArgumentParser(description="决策链前置过滤体检（v406）")
    ap.add_argument("--db", default="")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    conn = _ro(a.db or _db_path())
    out = {"fanout": fanout(conn), "prefilter": prefilter_matrix(conn, a.samples)}
    conn.close()
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    f, p = out["fanout"], out["prefilter"]
    print("① 重复决策的真实结构")
    print("   决策 %d 条 / 新闻 %d 条；一对一以外的组 %d 个"
          % (f["decisions"], f["distinct_news"], f["groups_multi"]))
    print("   按品种扇出（设计如此）%d 组 · 真重复（同 news+同 target）%d 组 · 同新闻不同 action %d 组"
          % (f["fanout_by_asset"], f["true_dup_groups"], f["conflict_action_groups"]))
    print("② 前置过滤覆盖率（新闻 %d 条，其中带降级标记 %d 条）"
          % (p["news"], p["degraded"]))
    print("   现有 news_router（黑名单）挡 %d 条（%.1f%%），其中误伤池内品种 %d 条"
          % (p["router_blocks"], 100.0 * p["router_blocks"] / max(1, p["news"]),
             p["router_block_with_pool_asset"]))
    print("   v1（只看池内锚点+宏观）挡 %d 条（%.1f%%），误伤池内品种 %d 条"
          % (p.get("v1_blocks", 0), 100.0 * p.get("v1_blocks", 0) / max(1, p["news"]),
             p.get("v1_block_with_pool_asset", 0)))
    print("   v2（+ 地缘/供给锚点）挡 %d 条（%.1f%%）：其中降级行 %d 条，误伤池内品种 %d 条"
          "（严格口径：未降级且池内 %d 条）"
          % (p["wish_blocks"], 100.0 * p["wish_blocks"] / max(1, p["news"]),
             p["wish_block_on_degraded"], p["wish_block_with_pool_asset"],
             p.get("wish_block_with_pool_asset_ok", 0)))
    print("   v3（白名单 ∩ 非交易类主体）挡 %d 条：其中降级行 %d 条，"
          "严格误伤（未降级且池内）%d 条"
          % (p.get("v3_blocks", 0), p.get("v3_block_on_degraded", 0),
             p.get("v3_block_with_pool_asset_ok", 0)))
    for s in p["false_positive"]:
        print("     误伤样本 news=%s asset=%s %s" % (s["news_id"], s["asset"], s["title"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
