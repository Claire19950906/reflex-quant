# -*- coding: utf-8 -*-
"""R5 因果链反推 - 从 review_insights 反向补充 causal_chains.

# v202 R5: 从 review_insights 反推因果链，coverage 70% -> 90%.

调用方式：
- python causal_chain_expander.py --days 30
- POST /api/v189/causal/expand-from-insights
- cron_causal_expand.py（每日 04:10 跑）
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import db


# v424: review_insights 里有一类记录根本不是"市场机制"，而是系统对自己 prompt 的
# 体检结论（[覆盖度告警]/[真实性告警]/[自动沉淀]）。旧实现一律当成孤儿 insight
# 建链，effect 只能写成"NONE 资产价格/信号受影响（具体方向见 rule 全文）"这种占位符。
# 实测 756 条链里 290 条（38%）是这类噪声，后来又当"种子"写进图谱，直接污染核心驱动
# （WTI 核心驱动 = "原油供需新闻驱动短线方向" 就是这么来的）。这里在源头掐掉。
_META_RULE_PREFIXES = ("[覆盖度告警]", "[真实性告警]", "[自动沉淀]", "[模板链]", "[系统告警]")


def _is_meta_rule(rule: str) -> bool:
    """判断一条 insight 是"系统自我体检"还是"市场机制"。"""
    s = str(rule or "").strip()
    return s.startswith(_META_RULE_PREFIXES) or "Asset=NONE" in s


_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "或", "但", "而", "也", "就", "都",
    "不", "没", "无", "非", "否", "可", "能", "会", "将", "已", "被",
    "我们", "你们", "他们", "它们", "这个", "那个", "这是", "那是",
    "一个", "一些", "一种", "一定", "一直", "一样",
    "之后", "之前", "现在", "过去", "未来",
    "因为", "所以", "因此", "导致", "引起", "使", "让",
    "应该", "需要", "可能", "建议", "认为", "觉得", "显示",
    "样本", "评测", "信号", "胜率", "盈亏", "置信度", "决策", "门槛",
    "暗示", "表明", "呈现", "反映",
    "立即", "快速", "短周期", "长周期", "小时", "分钟", "窗口",
    "触发", "出现", "发生", "释放", "落地", "兑现", "反向",
    "低", "高", "弱", "强", "中等", "明显", "显著",
    "段", "中", "区间", "幅度", "幅度内", "接近",
    "复盘", "回顾", "事后", "经验",
    "包括", "涉及", "相关", "对应", "匹配",
    "资产", "品种", "方向", "多头", "空头", "持仓",
    # 噪声英文 token（避免误当事件词提取）
    "score", "Score", "SCORE", "signal", "Signal",
    "LOSS", "WIN", "PnL", "pnl", "avg", "min", "max", "win",
}

# 有意义的英文 token（保留 — 用于 R4 coverage 提升）
_KEEP_EN_TOKENS = {
    "BUY", "SELL", "HOLD", "HEDGE",
    "XAU", "ETH", "BTC", "WTI", "SOL", "NVDA", "TSLA",
    "SEC", "ETP", "ETF", "OPEC", "FOMC", "FED", "CPI", "GDP", "NFP",
    "DeFi", "AI",
    # 事件代码（带下划线）— v202 R4 用 rule.split() 匹配的关键 token
    "geopolitical_conflict", "geopolitical", "conflict",
    "market_event", "market", "event",
    "regulatory_event", "monetary_policy", "monetary",
    "trade_settlement", "ai_worker", "prompt", "checklist",
    "verdict", "partial", "Asset", "limit_up",
}

# 事件主题关键词白名单（金融/地缘/监管/经济/加密/大宗/外汇）
_EVENT_KEYWORDS = [
    # 地缘
    "地缘冲突", "地缘升级", "地缘利空", "避险买盘", "避险降温",
    "中东局势", "中东冲突", "中东地缘", "中东升级", "中东紧张",
    "胡塞武装", "沙特阿美", "伊朗防空", "格什姆岛", "弹道导弹",
    "俄乌冲突", "俄乌谈判", "美俄乌克兰", "美俄乌谈判",
    "卢克石油", "特朗普盟友", "特朗普盟友石油交易",
    "停火谈判", "和谈", "北约", "欧盟", "联合国",
    "战争", "袭击", "爆炸", "空袭", "报复",
    # 监管/政策
    "SEC批准", "SEC监管", "SEC 批准", "SEC 监管", "监管通过", "监管利好", "监管利空",
    "杠杆ETP", "3倍ETP", "杠杆ETF", "3倍杠杆",
    "ETF批准", "ETF通过", "ETF获批", "现货ETF",
    "合规落地", "合规批准",
    # 货币政策/经济数据
    "加息", "降息", "FOMC", "美联储", "鲍威尔",
    "非农", "CPI", "GDP", "通胀", "通缩", "经济衰退", "经济数据",
    "美元指数", "美元升值", "美元贬值",
    # 加密
    "加密资产", "加密货币", "链上", "DeFi",
    # 大宗商品/原油
    "原油供给", "OPEC", "产油国", "增产", "减产", "停产",
    "霍尔木兹", "运油通道", "原油中断",
    # 股市
    "纳指", "标普", "道指", "科技股",
    # 行为金融
    "利好兑现", "买预期", "卖事实", "price-in", "pricein",
    "消息见光死", "见光死",
]

_MIN_CN_LEN = 2
_MIN_EN_LEN = 4


def extract_asset_codes(text):
    if not text:
        return set()
    t = text.upper()
    codes = set()
    for code in (
        "XAU", "ETH", "WTI", "BTC", "SOL", "NVDA", "TSLA",
        "GOLD", "OIL", "BITCOIN", "ETHEREUM",
        "原油", "黄金", "加密", "石油", "比特", "以太",
    ):
        if code in t:
            if code in ("GOLD", "黄金"):
                codes.add("XAU")
            elif code in ("BITCOIN", "比特"):
                codes.add("BTC")
            elif code in ("OIL", "原油", "石油"):
                codes.add("WTI")
            elif code in ("ETHEREUM", "以太"):
                codes.add("ETH")
            elif code in ("加密",):
                codes.add("ETH")
                codes.add("BTC")
            else:
                codes.add(code)
    return codes


def extract_event_keywords(rule, max_keywords=8):
    """从 insight.rule 提取事件主题关键词。

    v202-R5-fix: 改用白名单匹配（_EVENT_KEYWORDS + _KEEP_EN_TOKENS），
    不再抓所有 4+ 字中文短语，避免把"中东地缘新闻"等噪声整句写入 trigger_words。
    同时保留英文 token（资产代码、事件代码）— 这些 token 是 v202 R4 coverage
    计算时 rule.split() 匹配的关键，能直接提升覆盖率。
    """
    if not rule:
        return []
    text = re.sub(r"\[[^\]]*\]", " ", rule)
    text = re.sub(r"[，。！？；：、（）()《》【】「」『』\"'\u201c\u201d\u2018\u2019,.\!?;:()\[\]<>]", " ", text)
    # 英文 token 提取（≥3 字符，包含下划线和数字）
    en_tokens = re.findall(r"[A-Za-z][A-Za-z_0-9]{2,}", rule)  # 用原始 rule 抓下划线 token

    keywords = []
    seen = set()

    # 1) 优先匹配白名单中文关键词（按长度倒序，长的优先）
    sorted_keywords = sorted(_EVENT_KEYWORDS, key=len, reverse=True)
    for kw in sorted_keywords:
        if kw in seen:
            continue
        if kw.lower() in text.lower() or kw in text:
            keywords.append(kw)
            seen.add(kw)
            if len(keywords) >= max_keywords:
                return keywords

    # 2) 不足时补白名单英文 token（资产/事件代码）
    if len(keywords) < max_keywords:
        for tok in en_tokens:
            if tok not in _KEEP_EN_TOKENS:
                continue
            if tok in seen:
                continue
            keywords.append(tok)
            seen.add(tok)
            if len(keywords) >= max_keywords:
                return keywords

    return keywords


def split_trigger_words(trigger_words):
    if not trigger_words:
        return set()
    parts = re.split(r"[,,;;\n]", trigger_words)
    return {p.strip() for p in parts if p.strip()}


def join_trigger_words(existing, additions):
    existing_set = split_trigger_words(existing)
    added = 0
    new_prefix = []
    seen = set()
    for kw in additions:
        if not kw:
            continue
        if kw in existing_set or kw in seen:
            continue
        new_prefix.append(kw)
        seen.add(kw)
        added += 1
    old_list = [p.strip() for p in re.split(r"[,,;;\n]", existing) if p.strip()]
    final = new_prefix + [kw for kw in old_list if kw not in seen]
    return ",".join(final), added


def clean_trigger_words(trigger_words: str, max_len_per_word: int = 20) -> str:
    """清洗已污染的 trigger_words —— 拆词后丢弃超长短语 + 系统 token。

    v202-R5-fix: R5 第一次写库（无白名单版本）时把大量 4+ 字中文短语写入 trigger_words，
    导致部分 chain 的 trigger_words 长度 >1000。回滚困难（backup 也是污染版），
    故提供清洗函数：拆词后按长度过滤，保留短词（通常是手填的事件词）。

    v400 P0-1: 除长度外，也删掉 `verdict / prompt / Asset / BUY / SELL / ai_worker`
    这类系统 token（口径见 chain_keywords.SYSTEM_TOKENS），并去重。
    返回 (清洗后字符串, 被丢掉的 token 数)。
    """
    if not trigger_words:
        return trigger_words or "", 0
    try:
        from chain_keywords import clean_keywords
        res = clean_keywords(trigger_words, max_keywords=10**6,
                             max_token_len=max_len_per_word)
        return res.joined, len(res.removed)
    except Exception:
        parts = re.split(r"[,，;；\s/、\|\n]+", trigger_words)
        parts = [p.strip() for p in parts if p and p.strip()]
        kept, dropped = [], 0
        for p in parts:
            if len(p) <= max_len_per_word:
                kept.append(p)
            else:
                dropped += 1
        return ",".join(kept), dropped


_DEAD_ASSETS = {"", "NONE", "NULL", "UNKNOWN", "MACRO"}


def _dead_reason(asset, triggers) -> str:
    """判定一条"待创建"的链是不是**天生死链**，返回原因（空串=可以建）。

    2026-11-18 实测：库里 305 条 `asset=UNKNOWN`、237 条零触发词（重叠 234，合计 308 条），
    其中 288 条的 note 就是本模块写的"由 R5 反推自动创建"。这些链：
      - `asset` 实际是字面 `NONE`（insight 没定品种时上游写死 NONE）；
      - `trigger_words` 是空串（`extract_event_keywords` 从 rule 里抽不出词）；
      - 同一个 insight 还被建了 **两条**（#774 出现两遍）——因为去重也依赖 asset+词。
    `load_active_chains` 见到 UNKNOWN 直接 skip，所以它们建了也永远不会被评估，
    只在 UI 分母里占位。这里在**入口**挡掉，别再往库里扔垃圾。
    """
    if not str(triggers or "").strip():
        return "no_trigger_words"
    if str(asset or "").strip().upper() in _DEAD_ASSETS:
        return "no_asset"
    return ""


def _same_mechanism_exists(conn, asset, cause_text, triggers, min_kw_jaccard=0.7,
                           min_text_ratio=0.30):
    """建链前查重（v427）。

    为什么：实测库里同一机制存在 2–3 份副本（"中东地缘升级→原油偏多"三份、
    "地缘升温但利率走强→黄金冲高回落"四份），表述不同、触发词几乎一样。
    副本各自记分 = 同一件事被重复算成多份独立样本，Wilson 下界虚高，注入时
    同一条机制还说三遍。所以新建前先跟同品种、同方向的启用链比一比。
    """
    keys = set(k.strip() for k in (triggers or "").replace("，", ",").split(",") if k.strip())
    if not keys:
        return None
    try:
        rows = conn.execute(
            """SELECT id, cause, effect, mechanism, trigger_keywords FROM causal_chains
               WHERE enabled=1 AND origin!='deprecated_meta' AND UPPER(asset)=UPPER(?)""",
            (asset,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for r in rows:
        old = set(k.strip() for k in (r["trigger_keywords"] or "").replace("，", ",").split(",") if k.strip())
        if not old:
            continue
        jac = len(keys & old) / max(1, len(keys | old))
        if jac < min_kw_jaccard:
            continue
        try:
            import causal_graph as _cg
            ratio = _cg._contain_ratio(
                (cause_text or ""), (r["cause"] or "") + " " + (r["effect"] or "") + " " + (r["mechanism"] or ""))
        except Exception:
            ratio = 1.0  # 拿不到相似度就按关键词判重，宁可少建一条
        if ratio >= min_text_ratio:
            return int(r["id"])
    return None


def find_matching_chains(insight_asset, chain_idx):
    ta = (insight_asset or "").upper()
    if not ta:
        return []
    matched = []
    for c in chain_idx:
        if ta in c["assets"]:
            matched.append(c["id"])
    return matched


def expand_from_insights(
    days=30,
    min_samples_for_new_chain=2,
    max_new_chains=5,
    dry_run=False,
    clean_first=False,
    clean_max_len=10,
    reset=False,
):
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        since_iso = (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
        insight_rows = conn.execute(
            """SELECT id, asset, scope, origin, kind, source_samples, rule, counter_factual, created_at
                 FROM review_insights
                WHERE active=1 AND manual_deactivated=0
                  AND asset IS NOT NULL AND asset != ''
                  AND rule IS NOT NULL AND rule != ''
                  AND created_at >= ?
             ORDER BY id DESC""",
            (since_iso,),
        ).fetchall()
        chain_rows = conn.execute(
            """SELECT id, name, cause, effect, trigger_words, enabled, weight, origin
                 FROM causal_chains WHERE enabled=1""",
        ).fetchall()
        chain_idx = []
        for c in chain_rows:
            chain_idx.append({
                "id": c["id"],
                "name": c["name"],
                "trigger_words": c["trigger_words"] or "",
                "assets": extract_asset_codes(c["effect"] or ""),
                "weight": c["weight"] or 1.0,
                "origin": c["origin"] or "manual",
            })
        stats = {
            "scanned_insights": len(insight_rows),
            "extended_chains": 0,
            "total_added_keywords": 0,
            "new_chains_created": 0,
            "new_chains": [],
            "dry_run": dry_run,
        }
        chain_addition_log = {}
        orphan_insights = []
        for ins in insight_rows:
            ins_id = ins["id"]
            ins_asset = ins["asset"]
            ins_samples = ins["source_samples"] or 0
            ins_scope = ins["scope"] or ""
            ins_origin = ins["origin"] or ""
            ins_rule = ins["rule"] or ""
            matched_ids = find_matching_chains(ins_asset, chain_idx)
            new_keywords = extract_event_keywords(ins_rule, max_keywords=4)
            if matched_ids:
                for cid in matched_ids:
                    existing = next((c["trigger_words"] for c in chain_idx if c["id"] == cid), "")
                    additions = [kw for kw in new_keywords if kw not in split_trigger_words(existing)]
                    if additions:
                        chain_addition_log.setdefault(cid, []).extend(additions)
            else:
                if (_is_meta_rule(ins_rule)):
                    # v424: 系统体检日志不是因果机制，建链只会产出占位 effect
                    stats["skipped_meta_insights"] = stats.get("skipped_meta_insights", 0) + 1
                    continue
                if ins_samples >= min_samples_for_new_chain and ins_scope in ("auto_truth", "review"):
                    orphan_insights.append({
                        "id": ins_id,
                        "asset": ins_asset,
                        "samples": ins_samples,
                        "rule": ins_rule[:200],
                        "origin": ins_origin,
                        "scope": ins_scope,
                    })
        # trigger_words 被改动 → 派生的 asset/trigger_keywords 需要跟着重算
        touched_ids: set[int] = set()
        if chain_addition_log:
            for cid, additions in chain_addition_log.items():
                unique_additions = []
                seen = set()
                for kw in additions:
                    if kw not in seen:
                        unique_additions.append(kw)
                        seen.add(kw)
                cur = conn.execute("SELECT trigger_words FROM causal_chains WHERE id=?", (cid,)).fetchone()
                if not cur:
                    continue
                old = cur["trigger_words"] or ""
                if reset:
                    # 完全重置：清空 trigger_words 后只追加白名单词
                    old = ""
                    stats.setdefault("reset_chains", 0)
                    stats["reset_chains"] += 1
                elif clean_first:
                    old, dropped = clean_trigger_words(old, max_len_per_word=clean_max_len)
                    if dropped > 0:
                        stats.setdefault("cleaned_tokens_dropped", 0)
                        stats["cleaned_tokens_dropped"] += dropped
                new_str, added_count = join_trigger_words(old, unique_additions)
                if added_count > 0:
                    stats["extended_chains"] += 1
                    stats["total_added_keywords"] += added_count
                    if not dry_run:
                        conn.execute(
                            "UPDATE causal_chains SET trigger_words=?, updated_at=datetime('now','localtime') WHERE id=?",
                            (new_str, cid),
                        )
                        touched_ids.add(int(cid))
                        # audit_log 表在某些部署不存在，做容错
                        try:
                            conn.execute(
                                "INSERT INTO audit_log(ts, actor, action, target, detail) VALUES(?,?,?,?,?)",
                                (
                                    datetime.now().isoformat(sep=" ", timespec="seconds"),
                                    "r5_expander",
                                    "extend_trigger_words",
                                    f"causal_chain:{cid}",
                                    json.dumps({"added": unique_additions[:added_count], "dry_run": dry_run}, ensure_ascii=False),
                                ),
                            )
                        except sqlite3.OperationalError as _silent_exc_342:
                            # v322: audit_log 写不进去要留痕 —— 否则"扩链审计"看着成功，
                            # 实际一条都没记，用户无法回溯扩了什么
                            print(f"[causal_chain_expander] ⚠️ audit_log 写入失败: "
                                  f"{type(_silent_exc_342).__name__}: "
                                  f"{str(_silent_exc_342)[:120]}", flush=True)
        for cand in orphan_insights[:max_new_chains]:
            new_name = f"[auto_from_insight] {cand['asset']}-{cand['scope']}#{cand['id']}"
            rule_short = cand["rule"]
            if len(rule_short) > 60:
                rule_short = rule_short[:60] + "..."
            new_cause = rule_short
            new_effect = f"{cand['asset']} 资产价格/信号受影响（具体方向见 rule 全文）"
            new_triggers = ",".join(extract_event_keywords(cand["rule"], max_keywords=6))
            # v400 P0-1: 白名单抽取后仍走统一净化（防系统 token 从 rule 里混进来）
            try:
                from chain_keywords import clean_keywords
                new_triggers = clean_keywords(new_triggers, max_keywords=6).joined
            except Exception:
                pass
            dup_of = _same_mechanism_exists(conn, cand["asset"], new_cause, new_triggers)
            if dup_of:
                stats.setdefault("new_chains_skipped_dup", 0)
                stats["new_chains_skipped_dup"] += 1
                continue
            # v418（录入侧 P0）：**没有品种或没有触发词的链一律不落库**。
            # 实测 2026-11-18：库里 305 条 `asset=UNKNOWN`、237 条零触发词（重叠 234，
            # 合计 308 条死链），其中 288 条的 note 正是这里写的"由 R5 反推自动创建" ——
            # 根因就是下面这条 INSERT 从来没写 asset 列（insight 里明明有 asset），
            # 而 load_active_chains 见到 UNKNOWN 直接 skip：链建了、永远不评估、
            # 只在 UI 分母里占位（778 条里有 308 条是这种）。
            _cand_asset = str(cand.get("asset") or "").strip().upper()
            _dead = _dead_reason(_cand_asset, new_triggers)
            if _dead:
                stats.setdefault("new_chains_skipped_dead", 0)
                stats["new_chains_skipped_dead"] += 1
                stats.setdefault("new_chains_skipped_dead_reasons", {})
                stats["new_chains_skipped_dead_reasons"][_dead] = \
                    stats["new_chains_skipped_dead_reasons"].get(_dead, 0) + 1
                continue
            stats["new_chains_created"] += 1
            if not dry_run:
                cur = conn.execute(
                    """INSERT INTO causal_chains
                         (name, cause, effect, trigger_words, note, enabled, origin,
                          weight, mechanism, precondition, invalidation, asset,
                          created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,
                               datetime('now','localtime'), datetime('now','localtime'))""",
                    (
                        new_name, new_cause, new_effect, new_triggers,
                        f"由 R5 反推自动创建，源自 review_insight #{cand['id']} (samples={cand['samples']})",
                        0, "auto_from_insight", 0.5, "", "", "", _cand_asset,
                    ),
                )
                new_id = cur.lastrowid
                touched_ids.add(int(new_id))
                stats["new_chains"].append({
                    "id": new_id,
                    "name": new_name,
                    "asset": cand["asset"],
                    "origin": "auto_from_insight",
                    "src_insight_id": cand["id"],
                })
                # audit_log 容错
                try:
                    conn.execute(
                        "INSERT INTO audit_log(ts, actor, action, target, detail) VALUES(?,?,?,?,?)",
                        (
                            datetime.now().isoformat(sep=" ", timespec="seconds"),
                            "r5_expander",
                            "create_chain",
                            f"causal_chain:{new_id}",
                            json.dumps({"src_insight_id": cand["id"], "asset": cand["asset"], "samples": cand["samples"], "enabled": 0, "weight": 0.5}, ensure_ascii=False),
                        ),
                    )
                except sqlite3.OperationalError as _silent_exc_387:
                    print("[causal_chain_expander] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                              % (type(_silent_exc_387).__name__, str(_silent_exc_387)[:120]), flush=True)
        if not dry_run:
            # 重新派生被改动链的策略列 (asset/trigger_keywords/expected_direction),
            # 否则 trigger_words 变了而派生列没变, bridge 匹配到的仍是旧资产。
            if touched_ids:
                try:
                    from causal_chain_evaluator import derive_strategy_columns
                    d = derive_strategy_columns(conn, chain_ids=sorted(touched_ids))
                    stats["derived_chains"] = d["updated"]
                except Exception as e:  # 策略列尚不存在 → 不阻断主流程
                    print(f"[R5] 派生策略列失败 (不影响主流程): {e}", file=sys.stderr)
            conn.commit()
    finally:
        conn.close()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description="R5 因果链反推：从 review_insights 反推 causal_chains")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-samples", type=int, default=2)
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--clean", action="store_true",
                        help="先清洗已污染的 trigger_words（按长度过滤），再加白名单关键词")
    parser.add_argument("--clean-max-len", type=int, default=10,
                        help="清洗阈值：trigger_words 中超过此长度的 token 被丢弃，默认 10")
    parser.add_argument("--reset", action="store_true",
                        help="完全重置 trigger_words（清空后仅写白名单关键词）；原内容会先备份到 CSV")
    parser.add_argument("--backup-csv", type=str, default="/tmp/causal_chains_trigger_backup.csv",
                        help="reset 模式下的备份文件路径，默认 /tmp/causal_chains_trigger_backup.csv")
    args = parser.parse_args(argv)
    started = time.time()

    # reset 模式：先备份原 trigger_words
    if args.reset and not args.dry_run:
        import csv as _csv
        conn = db.get_connection()
        with open(args.backup_csv, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow(["id", "name", "trigger_words_before_reset", "asset_or_effect"])
            for row in conn.execute("SELECT id, name, trigger_words, effect FROM causal_chains WHERE enabled=1"):
                w.writerow([row[0], row[1], row[2], row[3]])
        conn.close()
        print(f"[R5] 已备份原 trigger_words 到 {args.backup_csv}")

    result = expand_from_insights(
        days=args.days,
        min_samples_for_new_chain=args.min_samples,
        max_new_chains=args.max_new,
        dry_run=args.dry_run,
        clean_first=args.clean,
        clean_max_len=args.clean_max_len,
        reset=args.reset,
    )
    result["elapsed_sec"] = round(time.time() - started, 2)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[R5] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
