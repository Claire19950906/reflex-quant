#!/usr/bin/env python3
"""news_score_worker.py — 后台 AI 评分 worker

给 news_events 写回 **新闻方向** (action / score) + **品种归属** (asset) + **处理状态** (status)。

v310 P1 (本轮修复):
  1. 补 `import re` —— `_primary_asset()` 用了 `re.split` 但模块没 import re,
     处理第一行就 NameError → 整批回滚 → 5,770 条 PENDING 事件永远处理不掉,
     验收 #13 (有方向分的新闻占比)、C4 (事件积压)、C8 (非法枚举) 同时断掉。
  2. score 语义改成 **方向幅度** [0, 1]: HOLD=0.0, BUY/SELL=幅度。
     旧版是带符号的 (-1..1), SELL 为负 → `score > 0` 永远数不到 SELL 行,
     「有方向分的新闻占比」被系统性低估 (验收口径写的是 score>0)。
  3. action 收敛到三态 {BUY, SELL, HOLD} —— DECISION_VALUES 里的 'IGNORE' 不是
     合法枚举 (验收 C8 只认这三个)。
  4. 文本恢复链: 自身 headline/body → raw_news.content → signal_chain_archive.news_title
     → hermes_observations.content。原先只看 headline/body, 而 2,211 条 PENDING 事件
     的 headline/body 全空 → 永远算不出方向。
  5. 品种映射链: 已有 asset → signal_chain_archive.target_asset → raw_news.gate_main_asset
     → 正文白名单 → 'NONE'。原先只看 assets 列 + 正文, 采集端没标的品种全落 'NONE'。
  6. 方向来源优先用 **LLM 已经落库的情绪分** (signal_chain_archive / ai_decisions
     的 sentiment_score), 没有方向性结论时才回落到关键词表。

关键词表: **不新增任何词条**。复用仓库内已有的三张情绪表 (news_score_worker /
reflection_core / reflection_logic_analysis), 并套用仓库既有的「有意义关键词」口径
(causal_chain_evaluator.meaningful_keyword_hit: ASCII 词 len>=4, 含非 ASCII 词 len>=2),
避免 "涨/跌/多/空/up" 这类碎片词把命中率刷上去。

用法:
  python3 news_score_worker.py --once            # 一次性清场 (默认 200 条)
  python3 news_score_worker.py --once --limit 500
  python3 news_score_worker.py --once --limit 50000 --rescore-done   # 全量重打分
  python3 news_score_worker.py --loop            # 持续守护 (默认 5 分钟)
  python3 news_score_worker.py --loop --period 180 --limit 50
"""
from typing import Any, Dict
import argparse
import json
import re
import sqlite3
import sys
import os
import time
from pathlib import Path

DB_PATH = Path(os.getenv("TRIDENT_DB_PATH") or (
    Path(__file__).resolve().parent.parent / "trident_event_bus.db"
))

BULLISH_KEYWORDS = (
    "突破", "创新高", "新高", "上涨", "飙升", "暴涨", "大涨", "利好", "看多",
    "增长", "加仓", "买入", "增持", "底部", "筑底", "反弹", "回暖", "复苏",
    "rally", "surge", "breakout", "bullish", "long",
)
BEARISH_KEYWORDS = (
    "暴跌", "腰斩", "崩盘", "跌停", "创新低", "新低", "下跌", "大跌", "利空", "看空",
    "撤资", "出逃", "清仓", "减持", "减仓", "闪崩", "恐慌", "衰退",
    "crash", "plunge", "drop", "fall", "bearish", "short", "liquidation",
)
DECISION_VALUES = ("HOLD", "BUY", "SELL")
# 合法三态 —— 验收 C8 的 VALID_NEWS_ACTIONS 只认这三个 ('IGNORE' 是历史脏值)。

# 方向判定阈值: 沿用既有口径, 不改数值。
DIRECTION_THRESHOLD = 0.3

# ── 品种映射 (v310 P0) ──────────────────────────────────────────────────────
# C4 "事件状态积压" 的真根因: news_events 里 5339 条 status='PENDING' 的事件,
# 其中 **asset 一条都没写** → 被判成"无主孤儿"(50.26%), 而实际上它们只是
# 采集完了但没人给它们打上品种归属。
# 优先级: ① 已有 assets 列(采集端已识别) → ② 正文/标题按资产代码白名单推断
#         ③ 都没有 → 'NONE' (宏观新闻, 本就不归属单一品种)
_ASSET_ALIASES = {
    "XAU": ("XAU", "GOLD", "黄金"),
    "BTC": ("BTC", "BITCOIN", "比特"),
    "ETH": ("ETH", "ETHEREUM", "以太"),
    "WTI": ("WTI", "OIL", "原油", "石油"),
    "SOL": ("SOL", "SOLANA"),
    "NVDA": ("NVDA", "NVIDIA", "英伟达"),
    "TSLA": ("TSLA", "TESLA", "特斯拉"),
}


def _meaningful_keyword(kw: str) -> bool:
    """仓库既有的「有意义关键词」口径 (与 causal_chain_evaluator.meaningful_keyword_hit 一致)。

    纯 ASCII 词需 len>=4 (排除 buy/up 这类高频碎片), 含非 ASCII 的词需 len>=2
    (排除 "涨"/"跌"/"多"/"空" 这类单字 —— 它们会命中 "多少/许多/空间" 之类的噪声)。
    """
    if not kw:
        return False
    if kw.isascii():
        return len(kw) >= 4
    return len(kw) >= 2


def _load_lexicons() -> list:
    """仓库内已有的情绪关键词表 (不新增词条, 也不删词条)。

    三张表都是产品既有资产:
      * news_score_worker        —— 新闻打分自身用的表
      * reflection_core          —— 反思链路情绪极性表
      * reflection_logic_analysis —— 逻辑分析正负向词表
    惰性 import + 兜底: 任何一张表缺失只影响该表的贡献, 不会让 worker 挂掉。
    """
    tables = [("news_score_worker", BULLISH_KEYWORDS, BEARISH_KEYWORDS)]
    try:
        from review.reflection_core import _BULL_KEYWORDS as _rc_bull, _BEAR_KEYWORDS as _rc_bear
        tables.append(("reflection_core", tuple(_rc_bull), tuple(_rc_bear)))
    except Exception:  # pragma: no cover - 依赖缺失时退化到本地表
        pass
    try:
        from review.reflection_logic_analysis import _POSITIVE_KW as _rla_bull, _NEGATIVE_KW as _rla_bear
        tables.append(("reflection_logic_analysis", tuple(_rla_bull), tuple(_rla_bear)))
    except Exception:  # pragma: no cover
        pass
    return [(name, tuple(bull), tuple(bear)) for name, bull, bear in tables]


_ASSET_CODE_BY_ALIAS = {}
for _code, _aliases in _ASSET_ALIASES.items():
    for _a in _aliases:
        _ASSET_CODE_BY_ALIAS.setdefault(_a.upper(), _code)


def _normalize_asset(value: str) -> str:
    """把中文/别名品种名归一成 ticker (原油->WTI, 黄金->XAU ...)。

    LLM 的 target_asset 与采集端的 gate_main_asset 给的是「原油」「黄金」这种中文名,
    而 news_events.asset 全库统一用 ticker (下游 event_move_attribution / 事件K线
    都是拿 ticker 去匹配的)。不归一的话这些事件在归因里永远匹配不上异动。
    """
    v = (value or "").strip().upper()
    if not v or v in ("NONE", "AGGREGATE", "ALL"):
        return v
    return _ASSET_CODE_BY_ALIAS.get(v, v)


def _extract_codes(text: str) -> set[str]:
    """从文本里按资产代码白名单推断品种 (与 causal_chain_expander 同口径)。"""
    if not text:
        return set()
    t = text.upper()
    out: set[str] = set()
    for code, aliases in _ASSET_ALIASES.items():
        for a in aliases:
            if a.upper() in t:
                out.add(code)
                break
    return out


def _extract_codes_strict(title: str) -> set[str]:
    """从**标题区**按白名单精确提取品种代码 (v321.2 P0 修正误判)。

    为什么不能直接用 `_extract_codes`
    --------------------------------
    `_extract_codes` 是**全文**子串扫描, 用于"这条新闻跟哪些品种有关"的宽松召回。
    但拿它做"asset 改写"的依据会大量误判 (实测 --fix-asset 首轮 809 条里):

        · "埃隆·马斯克正在缩减 Cybertruck 产能…" → 误判 NVDA
          (正文尾部有 "[美股 NVDA]" 提示行, 与标题内容无关)
        · "比特币在 Coinbase 折价…[美股 COIN]" → 误判 BTC
          (标题主体讲 BTC, 但提示行标的 COIN; 且标题里"比特币"命中 比特)

    所以改写依据必须是**标题本身**, 且:
      1. 只取标题第一行 (新闻正文/来源提示行在后面, 不参与判定);
      2. ticker 型别名 (纯 ASCII 大写代码) 必须**独立成词**才算命中 ——
         "Model S" 里不会命中 "NVDA", 但 "Model" 也不会误命中;
      3. 中文别名 (黄金/原油/比特币) 直接子串匹配, 无歧义;
      4. "比特" 这种**前缀**别名必须搭配完整词 ("比特币") 才算命中 ——
         单独的"比特"也可能是"比特流"。

    返回集合为空 = 标题里没有明确的品种指称 → 不改写 (保持原值)。
    """
    if not title:
        return set()
    # 只取标题主体: 第一行 + [hash:...] 前缀去
    first = str(title).split("\n", 1)[0]
    first = re.sub(r"^\[hash:[0-9a-fA-F]+\]\s*", "", first).strip()
    if not first:
        return set()
    t = first.upper()
    out: set[str] = set()
    for code, aliases in _ASSET_ALIASES.items():
        for a in aliases:
            au = a.upper()
            if not au:
                continue
            if au.isascii():
                # ticker 型: 必须独立成词 (前后不是字母数字)
                if re.search(r"(?<![A-Z0-9])" + re.escape(au) + r"(?![A-Z0-9])", t):
                    out.add(code)
                    break
            else:
                # 中文别名: "比特" 这种前缀要求更严 —— 必须有完整词
                if au == "比特":
                    if "比特币" in first or "比特幣" in first:
                        out.add(code)
                        break
                    continue
                if au in first:
                    out.add(code)
                    break
    return out


def _strip_ticker_prefix(title: str) -> str:
    """剥掉 Yahoo 行情源风格的 ticker 前缀 (如 "道琼斯：xxx")。

    这些前缀是**行情源标签**, 不是新闻标题的一部分 —— 真正的标题在冒号后面。
    不剥掉的话, "道琼斯：特朗普宣布…比特币守在8.3万" 会被当成"道琼斯主体",
    从而跳过本该做的资产修正。

    只处理"开头一段短标签 + 冒号"的形式 (标签 <= 8 字符且不含空格), 避免
    把正常的正文冒号当成前缀。
    """
    t = str(title or "").strip()
    if not t:
        return ""
    head = t.split("\n", 1)[0]
    for sep in ("：", ":"):
        if sep in head:
            label, rest = head.split(sep, 1)
            label = label.strip()
            if 0 < len(label) <= 8 and " " not in label and rest.strip():
                return rest.strip()
    return head


def _subject_text(title: str) -> str:
    """事件主体文本 = 剥掉 hash 前缀与 ticker 前缀后的标题首行。

    `[hash:xxx] ` 是采集端加的内容指纹, 不是标题的一部分 —— 不剥掉的话它会
    占掉"主体位置"的前 8 个字, 导致主体判定永远失败。
    """
    t = str(title or "")
    first = t.split("\n", 1)[0]
    first = re.sub(r"^\[hash:[0-9a-fA-F]+\]\s*", "", first).strip()
    return _strip_ticker_prefix(first) or first


# 标题**开头**出现这些词时, 事件主体是宽基指数, 顺带提到的黄金/原油只是
# 计价单位, 不是事件主体 → 不做资产改写。
_INDEX_SUBJECT_RE = re.compile(r"^(标普|纳斯达克|纳指|道琼斯|S&P|大盘|指数|股指)")

# 资产别名必须出现在标题这么靠前的位置, 才认为是"事件主体"。
# 阈值外的只是顺带提及 —— 例如 "瑞士投资集团…接管比特币矿企" 的主体是股票,
# 不是比特币。
_SUBJECT_POS_LIMIT = 8


def _alias_in_subject(code: str, subject: str) -> bool:
    """code 的任一别名是否出现在**主体位置** (subject 前几个字)。"""
    if not subject:
        return False
    head = subject[:_SUBJECT_POS_LIMIT]
    for a in _ASSET_ALIASES.get(code, ()):
        au = str(a).upper()
        if not au:
            continue
        if au.isascii():
            if au in head.upper():
                return True
        elif au == "比特":
            if "比特币" in head:
                return True
        elif au in head:
            return True
    return False


def _primary_asset(assets_raw: str, text: str) -> str:
    """该事件的归属品种 (news_events.asset 列)。

    ① assets 列已有值 → 取第一个非 NONE 的;
    ② 否则按标题/正文白名单推断;
    ③ 都没有 → 'NONE' (与既有 DONE 行里 'NONE' 的写法一致)。
    """
    for part in re.split(r"[,\s;/|]+", assets_raw or ""):
        p = part.strip().upper()
        if p and p != "NONE":
            return p
    codes = _extract_codes(text)
    return sorted(codes)[0] if codes else "NONE"


def _score_text(text):
    """返回 (text_score [-1,1], bull_hits, bear_hits)"""
    bull = sum(1 for k in BULLISH_KEYWORDS if k in text)
    bear = sum(1 for k in BEARISH_KEYWORDS if k in text)
    if bull + bear == 0:
        return 0.0, bull, bear
    return (bull - bear) / (bull + bear), bull, bear


def _decide_action(s):
    """v303 Q3: 三态显式映射 — |score|<0.3 显式归 HOLD, 杜绝空字符串污染."""
    if s >= 0.3:
        return "BUY"
    if s <= -0.3:
        return "SELL"
    return "HOLD"  # 包含 score=0 (无关键词命中) 和 |score|<0.3 (弱信号) 两种情况


def _lexicon_polarity(text_lower: str, lexicons) -> tuple:
    """关键词表打分。

    返回 (signed_polarity, bull_hits, bear_hits, any_directional):
      * signed_polarity  —— 三张表命中数合并后的方向与强度 [-1, 1]
      * any_directional  —— 是否**任一张**表给出 |polarity| >= 0.3 的方向结论
    """
    if not text_lower:
        return 0.0, 0, 0, False
    total_bull = total_bear = 0
    any_dir = False
    for _name, bull_kws, bear_kws in lexicons:
        b = sum(1 for k in bull_kws if k.lower() in text_lower)
        s = sum(1 for k in bear_kws if k.lower() in text_lower)
        total_bull += b
        total_bear += s
        if b + s and abs((b - s) / (b + s)) >= DIRECTION_THRESHOLD:
            any_dir = True
    hits = total_bull + total_bear
    polarity = (total_bull - total_bear) / hits if hits else 0.0
    return polarity, total_bull, total_bear, any_dir


def _score_event(text: str, llm_verdict, lexicons) -> tuple:
    """给一条新闻算方向。

    llm_verdict: LLM 已经落库的结论 (action, sentiment) 或 None。
      action ∈ {BUY, SELL, HOLD, ''}; sentiment ∈ [-1, 1]。
    返回 (action, score) —— score 是 **方向幅度** [0, 1]:
      BUY  -> score = 幅度 (>0)
      SELL -> score = 幅度 (>0)
      HOLD -> score = 0.0
    """
    # 1) LLM 已经给过方向结论时优先用它 (它比关键词表准)
    if llm_verdict:
        llm_action, sentiment = llm_verdict
        llm_action = (llm_action or "").strip().upper()
        if llm_action in ("BUY", "SELL"):
            # 幅度: 情绪分的绝对值; 情绪分缺失/为 0 时用方向阈值兜底
            #      (方向结论本身至少值一个"最小方向置信度")。
            mag = abs(float(sentiment or 0.0))
            mag = max(mag, DIRECTION_THRESHOLD)
            return llm_action, round(min(1.0, mag), 3)
        if sentiment is not None and abs(float(sentiment)) >= DIRECTION_THRESHOLD:
            action = "BUY" if float(sentiment) > 0 else "SELL"
            return action, round(min(1.0, abs(float(sentiment))), 3)
    # 2) 回落到关键词表
    polarity, _b, _s, any_dir = _lexicon_polarity((text or "").lower(), lexicons)
    if not any_dir:
        return "HOLD", 0.0
    action = "BUY" if polarity > 0 else "SELL"
    return action, round(min(1.0, abs(polarity)), 3)


def _load_context(conn) -> dict:
    """一次性把打分需要的外部上下文读进内存 (避免逐行子查询)。"""
    ctx = {"sca": {}, "ai_sent": {}, "raw": {}, "hermes": {}}
    try:
        for r in conn.execute(
            """SELECT news_id, news_title, sentiment_score, target_asset, suggested_action
               FROM signal_chain_archive WHERE news_id IS NOT NULL ORDER BY id ASC"""
        ):
            nid = r[0]
            if nid not in ctx["sca"]:
                ctx["sca"][nid] = (r[1], r[2], r[3], r[4])
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            """SELECT news_id, sentiment_score, suggested_action
               FROM ai_decisions WHERE news_id IS NOT NULL ORDER BY id ASC"""
        ):
            nid = r[0]
            if nid not in ctx["ai_sent"]:
                ctx["ai_sent"][nid] = (r[1], r[2])
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            "SELECT id, content, gate_main_asset FROM raw_news ORDER BY id ASC"
        ):
            ctx["raw"][r[0]] = (r[1], r[2])
    except sqlite3.OperationalError:
        pass
    try:
        # hermes 观测分四类: desk / decision / news / market。
        #   'decision' 是系统自己写下的决策结论 (例: "XAU SELL score=-0.075"),
        #   拿它当新闻正文会让打分器"读自己的作业" —— 直接排除。
        #   'news' 是新闻本身, 优先用; 其余按 desk/market 兜底。
        rank = {"news": 0, "desk": 1, "market": 2}
        best = {}
        for r in conn.execute(
            """SELECT news_id, observation_type, content FROM hermes_observations
               WHERE news_id IS NOT NULL AND content IS NOT NULL AND TRIM(content) <> ''
               ORDER BY id ASC"""
        ):
            nid, otype, content = r[0], (r[1] or "").strip().lower(), r[2]
            if otype == "decision":
                continue
            cur = best.get(nid)
            if cur is None or rank.get(otype, 3) < cur[0]:
                best[nid] = (rank.get(otype, 3), content)
        for nid, (_r, content) in best.items():
            ctx["hermes"][nid] = content
    except sqlite3.OperationalError:
        pass
    return ctx


def _event_text(row, ctx) -> str:
    """文本恢复链: 自身 -> raw_news -> signal_chain_archive -> hermes。"""
    parts = [row["headline"] or "", row["body"] or ""]
    nid = row["news_id"]
    raw = ctx["raw"].get(nid)
    if raw and raw[0]:
        parts.append(raw[0])
    sca = ctx["sca"].get(nid)
    if sca and sca[0]:
        parts.append(sca[0])
    hermes = ctx["hermes"].get(nid)
    if hermes:
        parts.append(hermes)
    return "\n".join(p for p in parts if p)


def _resolve_asset(row, text, ctx) -> str:
    """品种归属: 已有 asset -> LLM target_asset -> gate_main_asset -> assets 列/正文 -> 'NONE'。

    v321.2 P0 — **正文交叉校验** (修 400 条资产错配)
    --------------------------------------------------
    旧逻辑第 ① 步"已有 asset 直接返回"是**无条件**的, 于是采集端 gate 标错
    的品种永远得不到纠正:

        news_events 里标题含 "BTC/比特币" 的 456 条, 有 99 行 asset='WTI'、
        23 行 'XAU'、36 行 'ETH' —— 新闻讲的是比特币, 品种却记成原油。
        根因: gate_main_asset 是**板块级中文标签** ("加密主流"/"原油"),
        采集端按关键词分类, "比特币 ETF 净流出 + 油价承压" 这种**双关键词标题**
        会被判成"原油"。错配再经 event_move_attribution / event_kline_impact
        / missed_signal_taxonomy 三处继承 → 归因把 BTC 的消息算到 WTI 头上。

    修法: 已有 asset **不再无条件信任** —— 先用**标题区**白名单
    (`_extract_codes_strict`) 抽取标题里真实指称的品种, 与已有 asset 交叉校验:

      · 标题明确命中另一个品种 → 以标题为准 (覆盖错的 gate 标签);
      · 标题命中多个品种       → 保守保留已有 asset (不猜);
      · 标题没命中任何品种     → 保留已有 asset (原行为不变)。

    注意 `_extract_codes_strict` 走的是 `_ASSET_ALIASES` 白名单 (BTC/ETH/WTI/
    XAU/SOL/NVDA/TSLA), 不会把 "美股/个股" 这类板块标签当成品种 —— 这正好避开
    旧链路把中文板块塞进 asset 列的老问题。
    """
    cur = (row["asset"] or "").strip().upper()
    cur_valid = bool(cur) and cur not in ("NONE", "AGGREGATE", "ALL")

    # v321.2 P0: 标题区白名单交叉校验 (只在有标题时做)
    # 用 **strict** 版 (只看标题第一行 + ticker 独立成词) —— 宽松的全文扫描
    # 会把正文尾部的 "[美股 NVDA]" 提示行当成品种指称, 造成误判。
    if cur_valid and text:
        body_codes = _extract_codes_strict(text)
        if len(body_codes) == 1:
            code = next(iter(body_codes))
            subject = _subject_text(text)
            # 主体判定 (与 _fix_asset_mismatch 同口径):
            # · 宽基指数是主体 → 不动 (提到的黄金/原油只是计价单位)
            # · 资产别名不在主体位置 → 只是顺带提及 → 不动
            if not _INDEX_SUBJECT_RE.match(subject) and _alias_in_subject(code, subject):
                if cur != code:
                    return code
                return cur
            return cur

    if cur_valid:
        return _normalize_asset(cur)
    nid = row["news_id"]
    sca = ctx["sca"].get(nid)
    if sca and sca[2]:
        cand = _normalize_asset(sca[2])
        if cand and cand not in ("NONE", "AGGREGATE", "ALL"):
            return cand
    raw = ctx["raw"].get(nid)
    if raw and raw[1]:
        cand = _normalize_asset(raw[1])
        if cand and cand not in ("NONE", "AGGREGATE", "ALL"):
            return cand
    assets_col = (row["assets"] or "").strip()
    if assets_col:
        got = _primary_asset(assets_col, "")
        if got and got != "NONE":
            return _normalize_asset(got)
    return _primary_asset("", text)


def _llm_verdict(row, ctx):
    """取 LLM 已落库的结论 (action, sentiment) —— signal_chain_archive 优先, 其次 ai_decisions。

    返回 None 表示这条新闻 LLM 根本没给过结论 (走关键词表)。
    """
    nid = row["news_id"]
    sca = ctx["sca"].get(nid)
    if sca:
        action = (sca[3] or "").strip().upper()
        sentiment = sca[1]
        if action in ("BUY", "SELL", "HOLD") or (sentiment is not None and float(sentiment or 0.0) != 0.0):
            try:
                sent = float(sentiment or 0.0)
            except (TypeError, ValueError):
                sent = 0.0
            return action, sent
    ad = ctx["ai_sent"].get(nid)
    if ad:
        action = (ad[1] or "").strip().upper()
        try:
            sent = float(ad[0] or 0.0)
        except (TypeError, ValueError):
            sent = 0.0
        if action in ("BUY", "SELL", "HOLD") or sent != 0.0:
            return action, sent
    return None


def _fix_asset_mismatch(conn, limit=500) -> Dict[str, Any]:
    """v321.2 P0 — **只修品种归属**, 不动方向分/状态。

    为什么单独一个模式
    ------------------
    需求: 修正已落库的资产错配 (400 条: 标题讲 BTC, asset 记成 WTI)。

    为什么不复用 run_once(rescore_done=True)
    -----------------------------------------
    `rescore_done` 会**连 action/score 一起重算**, 而 news_events 里已经 DONE
    的行带的是**交易决策**的 action/score (验收 #13 统计的是"新闻方向",
    两者口径不同)。用 rescore_done 修资产 = 顺带把决策结论洗掉, 副作用超出
    "修资产"的范围。

    这里只做一件事: 对 asset 非 NONE 的行, 用正文白名单交叉校验, 命中唯一
    另一个品种时改写 asset; 其它字段一律不动。

    幂等: 改完再跑一次不会二次改动 (正文命中品种 == asset 时跳过)。
    """
    rows = conn.execute(
        "SELECT id, news_id, headline, body, asset FROM news_events "
        "WHERE asset IS NOT NULL AND asset <> '' AND asset NOT IN ('NONE','AGGREGATE','ALL') "
        "ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    if not rows:
        return {"ok": True, "scanned": 0, "fixed": 0, "samples": []}
    ctx = _load_context(conn)
    fixed = 0
    samples = []
    for r in rows:
        # v321.2 P0: 依据必须是**标题区** —— 宽松全文扫描会把正文尾部的
        # "[美股 NVDA]" 提示行当成品种指称 (实测造成 "马斯克/Model S" → NVDA
        # 这类误判)。_extract_codes_strict 内部只取标题第一行。
        title = r["headline"] or ""
        if not title:
            continue
        body_codes = _extract_codes_strict(title)
        if len(body_codes) != 1:
            continue                      # 0 个=没指称, 多个=不猜
        code = next(iter(body_codes))
        cur = (r["asset"] or "").strip().upper()
        if cur == code:
            continue                      # 一致
        # 主体判定: 资产必须出现在**主体位置** (标题前几个字), 否则只是顺带提及
        # (例: "瑞士投资集团…接管比特币矿企" 主体是股票, 不是比特币)。
        subject = _subject_text(title)
        if _INDEX_SUBJECT_RE.match(subject):
            continue                      # 宽基指数是主体 → 不改
        if not _alias_in_subject(code, subject):
            continue
        new_asset = code
        conn.execute(
            "UPDATE news_events SET asset=? WHERE id=? AND asset=?",
            (new_asset, r["id"], r["asset"]),
        )
        fixed += 1
        if len(samples) < 5:
            samples.append({
                "id": r["id"], "old_asset": r["asset"], "new_asset": new_asset,
                "headline": title.split("\n", 1)[0][:90],
            })
    conn.commit()
    return {"ok": True, "scanned": len(rows), "fixed": fixed, "samples": samples}


def run_once(limit=200, rescore_done=False):
    if not DB_PATH.exists():
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    # v310 P0: 接入 launchd 调度后与 api/bridge/engine 三进程并发写同一库,
    # 必须显式设 WAL + busy_timeout, 否则高频写会抛 "database is locked"
    # 导致本轮打分整批回滚 (与 db.get_connection 的口径对齐)。
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    # v302+ P2: 加 action IS NULL 分支 — 旧版只看 score=0, 但 score 有值但 action=NULL 的
    # 新闻 (例如外部 API 只写 score 不写 action) 会被漏掉,30 天内积压 56 条.
    # v303 Q3 P0: 加 action='' (空字符串) 分支 — SQLite 里 '' 不等于 NULL, 旧 query 漏抓 58 条
    # techflow/eastmoney 源 (无 bull/bear 关键词命中 → score=0 → 派生 action 也 = '').
    # v310 P0-1: **去掉 `score=0` 这个条件**。原因: score=0 且 action='HOLD' 是
    #   「无关键词命中 → 显式归 HOLD」的**合法终态** (v303 Q3 的设计), 它会永久匹配
    #   旧条件 → 每轮 ORDER BY ts DESC LIMIT 200 都被这批最新行占满 → 真正需要修的
    #   action='' / action='bullish' 脏行 (ts 较旧) 永远排不进前 200 → 永远清不掉
    #   (实测: 接入调度后 498 条脏行一条没少, worker 却在"空转"刷 HOLD 行)。
    #   正确口径: 只有 action 缺失/越界, 或 score 从未计算过, 才需要重算。
    # v310 P1: 取数口径。
    #   · action 缺失/空串/越界 (含历史脏值 'IGNORE') → 必算
    #   · status != 'DONE' (PENDING 积压) → 必算
    #   · score IS NULL → 必算
    #   · rescore_done=True → 连已经 DONE 的行一起重算 (决策回写行原本带的是
    #     **交易决策**的 action/score, 验收 #13 统计的是「新闻方向」, 两者口径不同,
    #     所以提供显式开关而不是默认覆盖, 避免日常调度把决策结论洗掉。
    if rescore_done:
        q = (
            "SELECT id, news_id, headline, body, action, score, ts, source, assets, status, asset "
            "FROM news_events ORDER BY ts DESC LIMIT ?"
        )
    else:
        q = (
            "SELECT id, news_id, headline, body, action, score, ts, source, assets, status, asset "
            "FROM news_events "
            "WHERE action IS NULL "
            "   OR action = '' "
            "   OR action NOT IN ('HOLD','BUY','SELL') "
            "   OR score IS NULL "
            # v420（2026-11-17）：原来这里是 `status != 'DONE'`，把
            #   status='FILTERED' 的**已归档旧新闻**也扫进来，取 verdict=None 时
            #   还把它们打回 status='PENDING'（decision_space=LLM_UNAVAILABLE）。
            #   实测：清空积压后 9 分钟，本 cron 就把 11-15 的 200 条旧新闻翻回队列 →
            #   引擎重启即回灌，正是"旧新闻灌模型"这个不稳定源。
            #   现在只认真正的待分析态（PENDING / 状态为空），FILTERED 与 DONE 一样不再动。
            "   OR status IS NULL OR status = 'PENDING' "
            "ORDER BY ts DESC LIMIT ?"
        )
    pending = list(conn.execute(q, (limit,)))
    # v420：超龄硬闸 —— 无论状态如何，超过 NEWS_ANALYZE_MAX_AGE_HOURS 的新闻
    #   一律不重算、不回炉（旧闻再判一遍只是白烧额度，判出来的结论也没有交易价值）。
    #   这些行如果此刻是 PENDING，就地归档成 FILTERED，让"待分析"计数如实下降。
    if not rescore_done:
        _max_age_h = 6
        try:
            import config as _cfg  # 延迟导入：本模块也能被单独跑（无 config 时用默认 6h）
            _max_age_h = int(getattr(_cfg, "NEWS_ANALYZE_MAX_AGE_HOURS", 6) or 6)
        except Exception:
            pass
        if _max_age_h > 0:
            _cutoff = time.time() - _max_age_h * 3600
            # v444（2026-11-18）：断档水位优先 —— 水位之下的新闻同样不重算，
            # 否则"不补历史"只堵主分析，这条打分通道还在拿历史旧闻烧额度。
            try:
                import news_cutoff as _nc  # noqa: WPS433
                _floor_ts = int(_nc.get_floor())
            except Exception:
                _floor_ts = 0
            if _floor_ts > 0:
                _cutoff = max(_cutoff, float(_floor_ts))
            _fresh, _stale_ids = [], []
            for _r in pending:
                _ts = _r["ts"]
                if isinstance(_ts, str):
                    try:
                        _ts = float(_ts)
                    except Exception:
                        _ts = 0.0
                if (_ts or 0) >= _cutoff:
                    _fresh.append(_r)
                elif str(_r["status"] or "") == "PENDING":
                    _stale_ids.append(_r["id"])
            if _stale_ids:
                conn.execute(
                    "UPDATE news_events SET status='FILTERED' "
                    "WHERE status='PENDING' AND id IN (%s)"
                    % ",".join("?" * len(_stale_ids)),
                    _stale_ids,
                )
                conn.commit()
                print(f"[news_score] 归档超龄旧闻 {len(_stale_ids)} 条（>{_max_age_h}h，不回炉）",
                      file=sys.stderr)
            pending = _fresh
    if not pending:
        conn.close()
        return {"ok": True, "scanned": 0, "updated": 0, "action_dist": {}}
    lexicons = _load_lexicons()
    ctx = _load_context(conn)
    results = []
    skipped_no_llm = 0
    for r in pending:
        text = _event_text(r, ctx)
        verdict = _llm_verdict(r, ctx)
        # v332: 模型不可用时**不许写出"已分析"的假结论**。
        #   实测：MiniMax 套餐额度到顶后，worker 仍把每条写成 score=0 / action=HOLD /
        #   asset=NONE 并标 DONE，24 小时内堆积 1337 条"看起来分析过、其实是空壳"的事件。
        #   这违反"拿不到就标断"的原则 —— 宁可行留在 PENDING 明说没分析，
        #   也不能给它一个假的 0 分结论。这里直接跳过，并打上待回放标记。
        if verdict is None:
            # v342: 标记写入加锁重试。实测 24h 内这条 UPDATE 有数十次
            # "database is locked" —— 标记没写进去，这条事件下次还会被当成
            # 待处理重扫（或者永远留在 PENDING 没人知道为什么）。锁只是短暂占用，
            # 退避重试几次基本都能趟过去。
            import db_retry as _dr

            marked = False
            for _attempt in range(_dr.DEFAULT_TRIES):
                try:
                    conn.execute(
                        "UPDATE news_events SET status='PENDING', decision_space='LLM_UNAVAILABLE' "
                        "WHERE id=?", (r["id"],),
                    )
                    # v340: 这条 UPDATE 也要立刻提交 —— 否则它开启的事务会跨过后面
                    # 所有 LLM 调用（每条几十秒），把写锁按住十几分钟。
                    conn.commit()
                    marked = True
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    if _attempt == _dr.DEFAULT_TRIES - 1:
                        # v339: 原来这里是裸 pass。而"标记写不进去"恰恰是 2026-11-07 那起
                        # 生产事故的起点（CHECK 约束拒绝 UPDATE，异常被吞 → 排查两天）。
                        # 至少留一行痕迹，让 doctor/验收/日志能看见。
                        print(f"[news_score] 待回放标记写入失败 id={r['id']}: {exc}",
                              file=sys.stderr)
                        break
                    import time as _t
                    _t.sleep(_dr._backoff(_attempt, _dr.DEFAULT_BASE_SLEEP, _dr.DEFAULT_MAX_SLEEP))
            skipped_no_llm += 1
            continue
        action, score = _score_event(text, verdict, lexicons)
        # v310 P0: 一并补齐**品种归属**与**处理状态**。
        #   · asset: 已有 → LLM target_asset → gate_main_asset → 正文白名单;
        #            都没有 → 'NONE' (宏观新闻本就不归属单一品种, 不是"没人认领")。
        #   · status: 打分即完成 → 'DONE'。此前 5,770 条事件永远停在 'PENDING',
        #            被验收 C4 判成 51% 的"无主孤儿" ——
        #            实际它们早就采集完+打完分, 只是没人给它们写收尾状态。
        asset = _resolve_asset(r, text, ctx)
        # v314: action 只允许 BUY/SELL/HOLD (写入边界枚举归一) —— _score_event
        # 本来就只产出这三种, 这里兜底防止未来改动漏出非法值 (验收 #13/C8)。
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        # v311: 写库撞锁重试 —— 之前这一句直接抛 "database is locked" 让整个 worker 以
        # 退出码 1 挂掉, 于是 news_events 的 score/action 永远补不上
        # (表现为"新闻没有分析结论")。WAL 下写-写冲突最多等 busy_timeout,
        # 仍撞锁就退避重试, 绝不让单行失败拖垮整轮 200 条的打分。
        for attempt in range(8):
            try:
                conn.execute(
                    "UPDATE news_events SET score=?, action=?, asset=?, status='DONE' WHERE id=?",
                    (score, action, asset, r["id"]),
                )
                # v340 P0: 逐条提交。
                # 旧实现只在整轮 200 条跑完后 commit 一次 —— 这一轮里有 200 次
                # LLM 调用（每条几十秒），写事务于是持续十几分钟，实测把
                # calc_event_move_attribution / news_score / fast_move_detector
                # 全部拖成 "database is locked"（2026-11-15 12:00~12:14 归因重建
                # 连续两轮因此放弃落库）。WAL 下逐条提交的代价可以忽略。
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < 7:
                    # 1.5s 起步、翻倍、封顶 60s，总预算约 4 分钟 —— 引擎批事务
                    # 实测可持续 2~5 分钟，原来总共只等 22s，必然整轮挂掉。
                    # 每次重试前先 rollback：这条连接自己持有的读/写状态不清掉，
                    # 再等也没用（锁是它自己的快照挡着）。
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    _d = min(60.0, 1.5 * (2 ** attempt))
                    time.sleep(_d)
                    continue
                raise
        results.append({
            "id": r["id"], "score": score, "action": action,
            "old_action": r["action"], "old_score": r["score"],
            "asset": asset, "old_status": r["status"],
            "ts_iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"])),
        })
    conn.commit()
    dist = {}
    for r in results:
        dist[r["action"]] = dist.get(r["action"], 0) + 1
    conn.close()
    return {
        "ok": True, "scanned": len(pending), "updated": len(results),
        "action_dist": dist, "sample": results[:3],
    }


def run_loop(period_s=300, limit=100):
    print(f"[score-worker] started, period={period_s}s limit={limit}", file=sys.stderr)
    while True:
        try:
            r = run_once(limit=limit)
            ts = time.strftime("%H:%M:%S")
            if r["updated"] > 0:
                print(
                    f"[{ts}] scanned={r['scanned']} updated={r['updated']} "
                    f"dist={r['action_dist']}",
                    file=sys.stderr,
                )
            else:
                print(f"[{ts}] no pending (scanned={r['scanned']})", file=sys.stderr)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] error: {e}", file=sys.stderr)
        time.sleep(period_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--period", type=int, default=300)
    ap.add_argument("--rescore-done", action="store_true",
                    help="连 status='DONE' 的行一起重算 (决策回写行原本带的是交易决策口径)")
    ap.add_argument("--fix-asset", action="store_true",
                    help="只修品种归属错配 (正文白名单交叉校验), 不动 action/score/status")
    args = ap.parse_args()
    if args.fix_asset:
        if not DB_PATH.exists():
            print(json.dumps({"ok": False, "error": f"DB not found: {DB_PATH}"}, ensure_ascii=False))
            sys.exit(1)
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            r = _fix_asset_mismatch(conn, limit=args.limit)
        finally:
            conn.close()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r.get("ok"):
            sys.exit(1)
    elif args.loop:
        run_loop(period_s=args.period, limit=args.limit)
    else:
        r = run_once(limit=args.limit, rescore_done=args.rescore_done)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
