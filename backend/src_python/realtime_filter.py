#!/usr/bin/env python3
"""
Trident Agent MVP — Real-Time News Filter (Ingest-Time)
=========================================================

Replaces the batch `news_ingestion_pipeline.py` for ingest-time filtering.
Call `evaluate_news()` at the moment a news item arrives (WebSocket, Webhook,
polling loop), BEFORE writing to `raw_news`.  The returned dict tells you what
`status`, `is_noise`, and `relevance_score` to insert.

This eliminates the "race condition" where `engine.py` picks up junk news
(`status='PENDING'`) before the batch pipeline has a chance to filter it.

Architecture:
    News Source → evaluate_news() → INSERT INTO raw_news → engine.py
                    │    │                                  (only sees
               L1 rules  L2 Haiku                          status='PENDING'
              (microsec) (≤3 sec)                           clean news)

Two-layer funnel (same semantic as the batch pipeline):
  L1 — Substring keyword matching (zero cost, ~10µs)
        JUNK_WORDS hit → is_noise=1, status='FILTERED'
        CORE_WORDS hit → is_noise=0, status='PENDING', relevance_score=0.90

  L2 — Claude Haiku via OpenRouter (≤3s timeout)
        Only called when L1 is UNCERTAIN.
        Prompt: "Output 1 or 0 — is this actionable for a macro/crypto trader?"
        Timeout/error → conservative: is_noise=1, status='FILTERED'
        Uses `anthropic/claude-haiku-latest` — always the newest version.

Integration example (see also module bottom):
    from realtime_filter import evaluate_news

    result = evaluate_news(news_title, news_content)

    conn.execute(
        "INSERT INTO raw_news (source, content, timestamp, status, "
        "is_noise, relevance_score) VALUES (?, ?, datetime('now','localtime'), "
        "?, ?, ?)",
        (source, content, result["status"], result["is_noise"],
         result["relevance_score"]),
    )

Usage (CLI test):
    python realtime_filter.py "美联储暗示9月降息"
    python realtime_filter.py "BTC空投活动来袭，注册即送100USDT"
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# v181: 影响力阈值常量（避免在 _harden_verdict 内重复 getattr）
import config as _cfg  # 局部模块级常量（运行时已就绪）



# ── Timezone ──────────────────────────────────────────────────────────────
TZ_SHANGHAI = timezone(timedelta(hours=8))


def _ts() -> str:
    return datetime.now(TZ_SHANGHAI).strftime("%H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════
# L1 — Rule-Based Keyword Filter (zero-cost, microsecond latency)
# ═══════════════════════════════════════════════════════════════════════════

# ── JUNK keywords → immediate discard ──
# Hit any of these → is_noise=1, status='FILTERED'.
# Sorted by domain for maintainability; flattened for matching.

JUNK_WORDS: List[str] = [
    # ── 营销 / 空投 / 抽奖 ──
    "空投", "airdrop", "抽奖", "赠金", "红包", "领取", "白名单",
    "giveaway", "lottery", "奖励发放", "福利", "返佣", "邀请返利",
    "注册即送", "开户即送", "新用户", "注册奖励", "邀请码", "推荐码",
    "affiliate", "referral", "bonus", "promo",

    # ── 教程 / 入门 / 科普 ──
    "新手教程", "入门指南", "怎么买", "什么是", "科普", "小课堂",
    "初学者", "小白", "零基础", "tutorial", "beginner", "how to",
    "指南", "攻略", "技巧", "秘籍",

    # ── AMA / 直播 / 预告 ──
    # v433：删掉裸 "AMA"（3 字母缩写会误匹配英文标题里的普通单词），只保留完整词组。
    "Ask Me Anything", "直播预告", "直播", "webinar",
    "Twitter Space", "社区活动", "meetup", "线上活动",
    "活动预告", "活动时间", "敬请期待",

    # ── NFT / 元宇宙 ──
    "NFT", "mint", "铸造", "盲盒", "元宇宙", "metaverse",
    "GameFi", "链游", "P2E", "土地",

    # ── 测试网 / 主网上线 ──
    "测试网", "testnet", "devnet", "previewnet",
    "主网上线", "mainnet launch", "升级公告", "版本更新",
    "patch", "hotfix", "sprint",

    # ── 治理 / 社区投票 ──
    "社区投票", "治理提案", "governance", "提案通过",
    "DAO", "Snapshot", "投票开始", "社区治理",

    # ── 周报 / 月报 / 技术分析 ──
    "周报", "weekly report", "月报", "monthly report",
    "日报", "daily report", "晨报", "晚报", "早报",
    "复盘", "相关性矩阵", "相关性分析",
    "技术分析", "K线图", "行情分析", "走势分析", "盘面分析",

    # ── 社交媒体噪音 ──
    "涨粉", "粉丝", "follow", "点赞", "转发抽奖",
    "评论有奖", "互动有奖", "打卡",

    # ── 交易所上币公告 ──
    "新币上线", "代币上线", "token listing", "上架公告",
    "下架公告", "delisting", "暂停交易",

    # ── 假消息 / 辟谣 ──
    "辟谣", "假消息", "fake news", "谣言", "rumor",
    "不实信息", "虚假信息",

    # ── 观点 / 汇总类 (屏蔽) ──
    # 注意：单字"什么"会误杀一切疑问句标题；"分析师"出现在大量正常宏观新闻里会误杀。
    # 改为精确短语"什么是/是什么"（科普），观点/汇总类交给 L2 LLM 兜底判定。
    "什么是", "是什么", "数据整理",
    # ── 观点/解读/汇总 (2026-09 收紧) ──
    # 这些词是 L1 "立即当 JUNK" 的高危类（分析师、机构观点、专家解读等），
    # 命中后立即丢弃，不交给 L2 —— 节省 L2 token + 防止"分析员观点"标题引发假信号。
    # 注意："机构观点/分析师预测"类标题信息密度低且常常是事后归纳，对建仓无价值。
    "机构观点", "分析师预测", "分析师预计", "分析师观点", "专家解读", "专家观点",
    "操盘必读", "今日观点", "今日解读", "深度解读", "独家解读",
    "行情解读", "盘前必读", "收盘播报", "盘中观点", "复盘观点",
    "数据前瞻", "数据公布", "财经早餐", "财经晚餐",
]

# ── CORE keywords → bypass L2, immediate high-relevance ──

CORE_WORDS: List[str] = [
    # ── 央行 / 货币政策 ──
    "美联储", "FOMC", "Powell", "鲍威尔", "降息", "加息",
    "interest rate", "利率决议", "联邦基金利率", "点阵图",
    "央行", "ECB", "欧洲央行", "BOJ", "日本央行", "BOE", "英国央行",
    "PBOC", "人民银行", "LPR", "MLF", "逆回购", "降准", "存款准备金",
    "量化宽松", "QE", "量化紧缩", "QT", "缩表", "扩表",
    "taper", "缩减购债", "收益率曲线控制", "YCC",
    "负利率", "零利率", "NIRP", "ZIRP",

    # ── 通胀 / 就业 / 经济数据 ──
    "CPI", "PPI", "PCE", "通胀", "核心通胀", "inflation",
    "房价", "房地产", "住房", "房屋销售", "抵押贷款",
    "非农", "NFP", "失业率", "初请失业金", "就业数据",
    "GDP", "PMI", "ISM", "制造业", "服务业", "零售销售",
    "消费者信心", "密歇根", "耐用品订单", "贸易逆差",

    # ── 经济数据惯用语 / 评级确认（防止被 L2 误判噪声）──
    "月率", "年率", "季率", "实际值", "预测值", "前值",
    "贸易差额", "贸易帐", "经常帐", "消费者支出", "进口物价",
    "出口物价", "失业人数", "景气指数", "工业信心", "评级",
    "标普确认", "穆迪确认", "惠誉", "评级机构",

    # ── 监管 / 政策 ──
    "SEC", "CFTC", "监管", "合规", "执法", "起诉", "罚款",
    "法案", "立法",
    "stablecoin", "稳定币法案", "MiCA", "FIT21",
    "禁止", "ban", "限制", "restriction",
    "牌照", "许可", "license", "注册",

    # ── 地缘冲突 / 制裁 ──
    "地缘", "战争", "冲突", "制裁", "sanction",
    "中东", "伊朗", "以色列", "俄乌", "乌克兰", "台海", "朝鲜",
    "OPEC", "欧佩克", "原油", "石油", "能源危机",
    "核", "nuclear", "导弹", "军事",

    # ── ETF / 机构资金 ──
    "ETF", "spot ETF", "期货ETF", "比特币ETF", "以太坊ETF",
    "流入", "inflow", "流出", "outflow", "净流入", "净流出",
    "BlackRock", "贝莱德", "Fidelity", "富达", "Grayscale", "灰度",
    "GBTC", "IBIT", "FBTC",
    "资产管理", "AUM", "持仓", "增持", "减持",

    # ── 清算 / 爆仓 / 挤兑 ──
    "清算", "liquidation", "爆仓", "强制平仓", "margin call",
    "挤兑", "bank run", "赎回", "暂停赎回",
    "违约", "default", "破产", "bankruptcy", "暴雷",

    # ── 交易所安全 / 黑客 ──
    "黑客", "hack", "被盗", "exploit", "漏洞", "攻击",
    "安全事件", "私钥", "热钱包", "冷钱包",
    "链上异动", "大额转账", "whale", "巨鲸",

    # ── 总统 / 政策人物 ──
    "特朗普", "拜登", "Trump", "Biden",
    "总统", "大选", "中选", "就职",
    "财长", "财政部长", "Yellen", "耶伦",

    # ── 市场结构 / 流动性 ──
    "流动性", "liquidity", "做市商", "market maker",
    "深度", "滑点", "价差", "波动率", "VIX",
    "轧空", "逼空", "short squeeze", "多头挤压",

    # ── 汇率 / 美债 ──
    "美元指数", "DXY", "美元", "美债", "国债收益率",
    "yield", "收益率倒挂", "期限利差",
    "汇率", "forex", "日元", "欧元", "英镑",
    "去美元化", "de-dollarization", "BRICS",

    # ── 主流加密品种 / 交易所 ──
    "比特币", "以太坊", "BTC", "ETH", "SOL", "Bitcoin", "Ethereum", "Solana",
    "资金费率", "现货", "期货", "Coinbase", "Binance", "OKX",

    # ── 减半 / 供应事件 ──
    "减半", "halving", "区块奖励", "供应冲击",
    "减仓", "仓位", "持仓量", "OI", "open interest",

    # ── 稳定币 ──
    "USDT", "USDC", "DAI", "脱锚", "depeg",
    "稳定币", "Tether", "Circle",

    # ── 紧急/突发信号词 ──
    "紧急", "urgent", "突发", "breaking",
    "闪崩", "crash", "暴跌", "暴涨", "熔断",

    # ── 人物 / 宏观 / 商品 / 避险 (2026-09 扩容) ──
    "指数", "实际利率", "利率", "tips", "马斯克", "埃里森",
    "马克扎克伯格", "v神", "军队", "购金", "交火",
    "通缩", "避险", "油价", "美指",
    # ── 贸易 / 财政 / 能源供应 (2026-09 补充：缺失的确定性高相关词) ──
    "关税", "贸易战", "债务上限", "政府停摆", "战略储备",
    "央行购金", "OPEC+",
    # ── 金十快讯 / 现货异动（来源与异动事件，优先判定为高相关）──
    "金十快讯", "现货异动",

    # ── 2026-09 补充：EIA / 库存 / OPEC+ 决策 / 美联储讲话 ──
    # 之前 CORE 漏掉"鲍威尔讲话/美联储官员/非农数据公布/EIA原油库存"等关键宏观事件。
    # 这些是"事件型硬驱动"——不该被 L2 误判成低相关；先在 L1 直通。
    "鲍威尔讲话", "美联储官员", "美联储主席", "FOMC会议", "利率决议",
    "纪要公布", "会议纪要", "新闻发布会",
    "非农就业", "非农报告", "小非农", "ADP就业", "JOLTS", "失业金人数",
    "EIA原油", "EIA库存", "API库存", "原油库存", "汽油库存",
    "OPEC会议", "OPEC+", "减产协议", "增产协议", "限产",
    "关税政策", "关税上调", "加征关税", "豁免关税",
    "鲍威尔", "耶伦", "贝森特", "哈塞特",
    "财政部长", "国务卿", "白宫发言人",
]

# Pre-compute lowercase lists (done once at import — O(1) per call)
_JUNK: List[str] = [w.lower() for w in JUNK_WORDS]
_CORE: List[str] = [w.lower() for w in CORE_WORDS]

# ── 可管理提示词库 ──────────────────────────────────────────────────────────
# L1 关键词来源：默认内置常量；load_keywords_from_db() 后切换为
# filter_keywords 表（内置种子 + 人工增删）。增删后调用 refresh_keywords()。
# evaluate_news 每次调用前按 TTL 自动检查，保证 API/引擎多进程动态同步。
_ACTIVE_KEYWORDS: Dict[str, List[str]] = {"junk": _JUNK, "core": _CORE}
_KEYWORDS_REFRESH_TTL = 15.0  # 秒：超过则下次 evaluate_news 时自动从 DB 刷新
_KEYWORDS_LAST_REFRESH = 0.0


def _ensure_fresh() -> None:
    """TTL 内不重复加载；过期自动从 DB 刷新（任何进程的过滤都动态同步）。"""
    global _KEYWORDS_LAST_REFRESH
    now = time.time()
    if now - _KEYWORDS_LAST_REFRESH < _KEYWORDS_REFRESH_TTL:
        return
    _KEYWORDS_LAST_REFRESH = now
    try:
        load_keywords_from_db()
    except Exception:
        pass


def _keyword_conn():
    import db as _db
    conn = _db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def sync_seed_keywords(conn=None) -> int:
    """把内置 JUNK/CORE 常量增量同步进 filter_keywords（builtin=1），幂等。

    始终按当前内置词做「缺失则插入」（不再因已有内置词而整批跳过），
    这样后续新增的内置词能自动进旧库；人工增删/停用不受覆盖（只补缺失）。
    """
    own = conn is None
    conn = conn or _keyword_conn()
    try:
        existing = set()
        for r in conn.execute("SELECT kind, keyword FROM filter_keywords").fetchall():
            existing.add((r[0], r[1]))
        rows = [
            (kind, w)
            for kind, words in (("junk", JUNK_WORDS), ("core", CORE_WORDS))
            for w in words
            if (kind, w) not in existing
        ]
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO filter_keywords (kind, keyword, builtin, active, note) "
                "VALUES (?, ?, 1, 1, '内置')",
                rows,
            )
            conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def load_keywords_from_db(conn=None) -> bool:
    """从 filter_keywords 加载 active=1 关键词到 _ACTIVE_KEYWORDS（含内置种子同步）。

    失败或表不存在时保留内置常量，不影响 L1 过滤。
    """
    try:
        own = conn is None
        conn = conn or _keyword_conn()
        try:
            sync_seed_keywords(conn)
            rows = conn.execute(
                "SELECT kind, keyword FROM filter_keywords WHERE active = 1"
            ).fetchall()
            junk = [str(r["keyword"]).lower() for r in rows if r["kind"] == "junk"]
            core = [str(r["keyword"]).lower() for r in rows if r["kind"] == "core"]
            if junk:
                _ACTIVE_KEYWORDS["junk"] = junk
            if core:
                _ACTIVE_KEYWORDS["core"] = core
            return True
        finally:
            if own:
                conn.close()
    except Exception:
        return False


def refresh_keywords() -> bool:
    """关键词库变更后刷新内存（添加/删除/停用后调用）。"""
    global _KEYWORDS_LAST_REFRESH
    ok = load_keywords_from_db()
    _KEYWORDS_LAST_REFRESH = time.time()
    return ok


def list_keywords(conn=None) -> Dict[str, Any]:
    """全部提示词（含停用与内置标记），供管理页展示。"""
    own = conn is None
    conn = conn or _keyword_conn()
    try:
        sync_seed_keywords(conn)
        rows = conn.execute(
            "SELECT id, kind, keyword, builtin, active, note, created_at "
            "FROM filter_keywords ORDER BY id"
        ).fetchall()
        items = [dict(r) for r in rows]
        active_count = sum(1 for it in items if it["active"])
        return {
            "junk": [it for it in items if it["kind"] == "junk"],
            "core": [it for it in items if it["kind"] == "core"],
            "total": len(items),
            "active_count": active_count,
        }
    finally:
        if own:
            conn.close()


def add_keyword(kind: str, keyword: str, note: str = "") -> Dict[str, Any]:
    """新增自定义提示词（builtin=0, active=1），并刷新内存。"""
    kind = "junk" if kind == "junk" else "core"
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("关键词不能为空")
    conn = _keyword_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO filter_keywords (kind, keyword, builtin, active, note) "
                "VALUES (?, ?, 0, 1, ?)",
                (kind, keyword, (note or "").strip()[:80]),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 已存在（可能是停用的内置词）：重新启用
            conn.execute(
                "UPDATE filter_keywords SET active=1, note=? "
                "WHERE kind=? AND keyword=?",
                ((note or "").strip()[:80], kind, keyword),
            )
            conn.commit()
    finally:
        conn.close()
    refresh_keywords()
    return {"ok": True}


def deactivate_keyword(keyword_id: int) -> Dict[str, Any]:
    """停用（软删除）提示词，刷新内存。"""
    conn = _keyword_conn()
    try:
        cur = conn.execute(
            "UPDATE filter_keywords SET active=0 WHERE id=? AND active=1",
            (int(keyword_id),),
        )
        conn.commit()
        hit = cur.rowcount > 0
    finally:
        conn.close()
    refresh_keywords()
    if not hit:
        return {"ok": False, "error": "提示词不存在或已停用"}
    return {"ok": True}

# Hash-stripping regex (engine dedup prefix like "[hash:1a2b3c4d] ")
_HASH_RE: re.Pattern = re.compile(r"\[hash:[a-fA-F0-9]+\]\s*")


def _l1_classify(text: str) -> Optional[Dict[str, Any]]:
    """Run L1 keyword matching on a news item.

    Returns:
        None         — UNCERTAIN (needs L2)
        dict         — definitive verdict ready to return
    """
    # Strip hash prefix, lowercase, trim
    clean = _HASH_RE.sub("", text).lower()
    # Content can be long; cap at 500 chars for matching
    clean = clean[:500]

    # v433（2026-11-17）：**资讯汇总类垃圾词**在池内关键词命中时让位，其余 JUNK 照拦。
    # 背景（实测误杀）：
    #   · kw='数据整理' 打掉「金十数据整理：每日全球外汇市场要闻速递」——
    #     整篇 FX 要闻汇总（美元/日元/欧元都在池内）被当垃圾丢掉；
    #   · kw='AMA'（3 字母缩写）误匹配英文标题里的普通单词，已从 JUNK_WORDS 删除。
    # 注意只让"汇总类"词让位：`空投/抽奖/邀请码` 这类营销词即使含 "比特币" 也照拦
    # （否则「比特币空投活动开始」会被放进来，等于把噪音灌进主分析）。
    _POOL_OVERRIDE_JUNK = {"数据整理", "晚报", "周报", "晨报", "汇总", "速递", "日报", "盘前", "盘后"}
    _junk_kw = None
    for kw in _ACTIVE_KEYWORDS["junk"]:
        if kw in clean:
            _junk_kw = kw
            break
    if _junk_kw in _POOL_OVERRIDE_JUNK:
        # 除了池内品种词，也认"市场类别词"——「每日全球大宗商品市场要闻速递」
        # 里没有 XAU/WTI 这种具体代码，但整篇就是池内资产的价格驱动。
        _MARKET_CATEGORY = ("大宗商品", "贵金属", "外汇", "美债", "美股", "原油",
                            "黄金", "白银", "加密货币", "数字资产", "收益率")
        try:
            from feeds.news_router import POOL_KEYWORDS as _PK
            _pool_hit = any(str(k).lower() in clean for k in _PK)
        except Exception:
            _pool_hit = False
        if not _pool_hit:
            _pool_hit = any(c in clean for c in _MARKET_CATEGORY)
        if _pool_hit:
            return {
                "is_noise": 0,
                "relevance_score": 0.70,
                "status": "PENDING",
                "layer": "L1",
                "verdict": "CORE",
                "matched_keyword": "pool-keyword",
            }

    # JUNK check first (conservative: "BTC 空投活动" is still noise)
    if _junk_kw:
        return {
            "is_noise": 1,
            "relevance_score": 0.0,
            "status": "FILTERED",
            "layer": "L1",
            "verdict": "JUNK",
            "matched_keyword": _junk_kw,
        }

    # CORE check
    for kw in _ACTIVE_KEYWORDS["core"]:
        if kw in clean:
            return {
                "is_noise": 0,
                "relevance_score": 0.90,
                "status": "PENDING",
                "layer": "L1",
                "verdict": "CORE",
                "matched_keyword": kw,
            }

    return None  # UNCERTAIN → escalate to L2


# ═══════════════════════════════════════════════════════════════════════════
# L2 — Claude 3.5 Haiku Binary Classifier (≤3s timeout)
# ═══════════════════════════════════════════════════════════════════════════

# ── Config (env-var overridable) ──
_OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Default: Claude Haiku latest (auto-tracks newest version via OpenRouter alias).
# Override with REALTIME_FILTER_MODEL.
_FILTER_MODEL = os.getenv(
    "REALTIME_FILTER_MODEL",
    "deepseek/deepseek-chat",
)
# Timeout in seconds (L2 must return fast or we bail)
_FILTER_TIMEOUT_S = float(os.getenv("REALTIME_FILTER_TIMEOUT_S", "3.0"))

# Proxy (same pattern as engine.py)
_HTTP_PROXY = os.getenv("HTTP_PROXY", "").strip()


# ── OpenRouter 兜底：开关 + 熔断（v321）────────────────────────────────
# 背景（2026-11-04 生产实测）：
#   1) .env 里的 OPENROUTER_API_KEY 已失效 —— 任何请求返回
#      HTTP 401 {"error":{"message":"User not found.","code":401}}。
#   2) config.py 早已把 OPENROUTER_ENABLED 默认设为 0（主分析池据此停用），
#      但**本模块的兜底路径没遵守这个开关**，只看 key 非空就发请求。
#   3) urllib 的 timeout 是**每次 socket 操作**的上限、不是总耗时上限：
#      实测一次 401 要 27.9s（DNS + TLS + 请求 + 读响应逐段各吃一次 timeout）。
#   4) 门卫/L2 的兜底是**同步阻塞**跑在入库主链路上的：
#      「MiniMax 池全挂 + OpenRouter key 死」⇒ 每条新闻白等 27 秒
#      ⇒ 整条 ingest 管道被拖成爬行 —— 2026-11-04 02:36~03:17 事故的放大器。
#
# 对策（三层）：
#   A. 兜底统一走 _openrouter_ready()：尊重 config.OPENROUTER_ENABLED 开关
#      （key 失效时默认关闭，不再白等）。
#   B. 熔断：运行中 key 被吊销（401/403）→ 冷却期内直接跳过，零延迟；
#      429 配额 → 短冷却（配额可能很快恢复）。
#   C. 兜底单次超时收紧：兜底只是救场，不能拖慢主链路。
_OR_BREAK_UNTIL = 0.0          # time.monotonic() 截止时刻；0 = 未熔断
_OR_BREAK_REASON = ""
_OR_BREAK_AUTH_COOLDOWN_S = float(os.getenv("FALLBACK_AUTH_COOLDOWN_S", "1800"))
_OR_BREAK_QUOTA_COOLDOWN_S = float(os.getenv("FALLBACK_QUOTA_COOLDOWN_S", "300"))
# 兜底单次 socket 超时（秒）——比门卫主超时短，救场不该拖垮主链路
_OR_FALLBACK_TIMEOUT_S = float(os.getenv("REALTIME_FALLBACK_TIMEOUT_S", "8.0"))


def _or_breaker_reason() -> str:
    """返回熔断原因；未熔断返回空串。"""
    if _OR_BREAK_UNTIL and time.monotonic() < _OR_BREAK_UNTIL:
        return _OR_BREAK_REASON or "unknown"
    return ""


def _or_trip(reason: str, cooldown_s: float) -> None:
    """打开熔断：冷却期内所有 OpenRouter 兜底调用直接跳过。"""
    global _OR_BREAK_UNTIL, _OR_BREAK_REASON
    _OR_BREAK_REASON = reason
    _OR_BREAK_UNTIL = time.monotonic() + max(1.0, float(cooldown_s))
    print(f"[FALLBACK] {_ts()} OpenRouter 熔断 {cooldown_s:.0f}s | {reason}", flush=True)


def _openrouter_ready() -> bool:
    """OpenRouter 兜底当前是否可用。

    三重门：开关(config.OPENROUTER_ENABLED) + key 非空 + 未熔断。
    """
    if not _OPENROUTER_KEY:
        return False
    try:
        if not bool(getattr(_cfg, "OPENROUTER_ENABLED", False)):
            return False
    except Exception:
        pass
    return not _or_breaker_reason()


# ── Pool fallback (L2 / L3 共享) ────────────────────────────────────────
# 2026-09-17：OpenRouter key 失效后，L2 / L3 也接进主分析模型池；
# 池子全失败才回退 OpenRouter(若用户新配置了 key)。
# 直接 import config，避免和 ai_worker.py 漂移；model pool 是 AI_MODEL_ROSTER 展开的实例。
try:
    import config as _cfg
    _MINIMAX_KEY = getattr(_cfg, "MINIMAX_API_KEY", "")
    _MINIMAX2_KEY = getattr(_cfg, "MINIMAX2_API_KEY", "")
    _MINIMAX_BASE = getattr(_cfg, "MINIMAX_BASE_URL", "")
    _MINIMAX_MODEL = getattr(_cfg, "MINIMAX_MODEL", "MiniMax-M3")
    _XINSHU_KEY = getattr(_cfg, "AIPING_API_KEY", "")
    _XINSHU_BASE = getattr(_cfg, "AIPING_BASE_URL", "")
    _XINSHU_MODEL = os.getenv("REALTIME_FILTER_XINSHU_MODEL", "gpt-5.5")
except Exception:
    _cfg = None
    _MINIMAX_KEY = _MINIMAX2_KEY = _XINSHU_KEY = ""
    _MINIMAX_BASE = _XINSHU_BASE = ""
    _MINIMAX_MODEL = "MiniMax-M3"
    _XINSHU_MODEL = "gpt-5.5"


def _post_openai_compat(
    api_base: str, api_key: str, model: str,
    payload: Dict[str, Any], timeout: float,
    err_sink: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """发 OpenAI 兼容 chat/completions 请求，返回 message.content 字符串；失败 None。
    用于 L2 / L3 走 MiniMax / MiniMax#2 / Xinshu。

    v320: 新增 err_sink —— 调用失败时把错误分类写进去，供上层熔断区分
    "配额耗尽(429/401/402/403，长冷却)" 与 "网络抖动(短冷却)"。
    """
    if not api_base or not api_key:
        return None
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    ssl_ctx = ssl.create_default_context()
    try:
        ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass
    # 国内直连：空 ProxyHandler 强制直连，避免被 env HTTP_PROXY 劫持
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # 间歇 SSL 握手失败/连接瞬断：秒级重试，全失败才返回 None（上层 L2/L3 自行降级）
    # v443：3 次 → 2 次。配合 25s 超时，最坏 50s；原来 3×20s ≈ 64s 且大概率全失败。
    # v480: 保持 2 次（这是 L2/L3 共用函数，主分析一次成功更值钱）；
# 真正拖时间的是超时值，已把 gate 超时 25s→10s（最坏 2×10s+1s 退避 ≈ 21s，原来 50s）。
    _HTTP_RETRIES = max(1, int(os.getenv("REALTIME_GATE_HTTP_RETRIES", "2") or 2))
    last_exc: Optional[Exception] = None
    for attempt in range(_HTTP_RETRIES):
        if attempt:
            time.sleep(1.0 * attempt)
        try:
            resp = opener.open(req, timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            if err_sink is not None:
                err_sink["http_code"] = int(e.code)
                err_sink["model"] = model
                # 错误分类（与 model_probe 口径一致）：
                #   401/403 = key 失效/无权限 -> auth 熔断（15 分钟不探测）
                #   402     = 余额/套餐用量耗尽 -> quota 熔断（30 分钟，需充值/升级套餐）
                #   429     = 配额打满/瞬时限速 -> quota / ratelimit 熔断
                if int(e.code) == 402:
                    err_sink["kind"] = "quota"
                    # v501：402 是账户级事实，换通道/重试都不会变好 → 全局短路 30 分钟
                    try:
                        from common import llm_budget as _lbq
                        _lbq.note_quota_dead(30, "realtime_filter 402", scope="gate")
                    except Exception:
                        pass
                elif int(e.code) in (401, 403):
                    err_sink["kind"] = "auth"
                elif int(e.code) == 429:
                    # 2026-11-15: 429 再分一层 —— 瞬时限速 vs 额度耗尽
                    _rl_low = ""
                    try:
                        _rl_low = (e.read() or b"").decode("utf-8", "ignore")[:400].lower()
                    except Exception:
                        _rl_low = ""
                    if any(k in _rl_low for k in ("rate_limit", "速率限制", "tpm", "rpm",
                                                  "too many requests", "concurrency")):
                        err_sink["kind"] = "ratelimit"
                    else:
                        err_sink["kind"] = "quota"
                else:
                    err_sink["kind"] = "http"
            print(f"[FILTER] {_ts()} pool-http {model} {e.code} | {e}", flush=True)
            return None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            if err_sink is not None:
                err_sink["http_code"] = None
                err_sink["model"] = model
                err_sink["kind"] = "network"
                err_sink["error"] = f"{type(e).__name__}: {e}"
            print(f"[FILTER] {_ts()} pool-retry {model} #{attempt + 1} | {type(e).__name__}", flush=True)
            continue
    else:
        if err_sink is not None:
            err_sink["http_code"] = None
            err_sink["model"] = model
            err_sink["kind"] = "network"
            err_sink["error"] = f"retries exhausted: {last_exc}"
        print(f"[FILTER] {_ts()} pool-net {model} retries exhausted | {last_exc}", flush=True)
        return None
    try:
        body = json.loads(resp.read().decode("utf-8"))
        return (
            body.get("choices", [{}])[0].get("message", {}).get("content", "")
            or ""
        )
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        return None


# ══════════════════════════════════════════════════════════════════
# v320: LLM 通道熔断 + 全池故障节流告警
# ──────────────────────────────────────────────────────────────────
# 事故背景（2026-11-01）：MiniMax 两个账户 Token Plan 全部耗尽，gate 池
# 全程 429（"已达到 Token Plan 用量上限"），持续约 3h15m、2651 次失败。
# 期间每条新闻都要对已死通道重试 3 次 → ingest 被拖慢、日志被刷爆，
# 且新闻全被门卫 fail-closed 判成噪音丢弃（数据不可恢复）。

# 配额类错误长冷却：额度耗尽后重试无意义，等 30 分钟再试
_QUOTA_COOLDOWN_S = 1800.0
# 2026-11-15: 瞬时限速短冷却。
# 事故背景：MiniMax 返回 429 + rate_limit_error（"已达到 Token Plan 速率限制 2062"），
# 额度并未耗尽，几十秒即恢复；此前与"额度打满"共用 30 分钟冷却，
# 一次瞬时限速被放大成半小时静默（实测连续 46 小时零决策）。
_RATELIMIT_COOLDOWN_S = float(os.getenv("GATE_RATELIMIT_COOLDOWN_S", "45"))
# 网络类错误短冷却：TLS 抖动/超时，60s 后半开探测
_NET_COOLDOWN_S = 60.0
# 鉴权类(401/402/403)：key 失效不会自己变好 → 24 小时熔断，**不做自动探测**。
# 事故背景（2026-11-04）：门卫池 minimax-gate 持续 401 数小时，
# 每 10 分钟探测一次 = 一天 144 次白打（每次都可能计费）。
# 2026-11-16 修正：24h 太长。实测「一次 401 后 key 其实已恢复，门卫却继续黑箱
# 一整天」——熔断器本身支持 half_open 单飞探测，一次探测只花 1 个 token。
# 现在默认 15 分钟放行一次探测（可用 GATE_AUTH_COOLDOWN_S 覆盖）：
# 真失效时最多每 15 分钟白打一次（≈96 次/天、几 token），
# 而换来的是 key 恢复后 ≤15 分钟自动复活，不再需要人工 reset。
_AUTH_COOLDOWN_S = float(os.getenv("GATE_AUTH_COOLDOWN_S", "900"))


# ══════════════════════════════════════════════════════════════════
# v322: 门卫结果缓存（省钱核心 #2）
# ──────────────────────────────────────────────────────────────────
# 为什么要缓存（实测数据驱动）：
#   2026-11-04 全天门卫 4278 次调用 / 3.68M token，占全天 9.2M 的 40%，
#   而主分析只产出 244 条信号 —— 每产出 1 条信号要付 ~17 次门卫调用。
#   重复来源：同一新闻多源转载、gate-replay 回放、批量补跑、引擎重启后重扫。
#
# 为什么安全：
#   · 门卫 temperature=0，prompt 只由「标题 + 摘要」构成（无时间戳/无随机数）
#     → 同样的新闻必然得到同样的判定。
#   · 只缓存**成功**的结果；失败不入缓存（避免把"临时故障"固化成判定）。
#
# 内存约束：本机只有 8GB RAM（且常年被 GUI 应用占满），
#   所以缓存必须克制 —— 进程内 LRU + TTL + 硬上限，默认 500 条 / 6 小时。
_GATE_CACHE: Dict[str, Tuple[float, str]] = {}
_GATE_CACHE_LOCK = threading.Lock()


def _gate_cache_enabled() -> bool:
    try:
        return bool(getattr(_cfg, "GATE_CACHE_ENABLED", True))
    except Exception:
        return True


def _gate_cache_max() -> int:
    try:
        return max(0, int(getattr(_cfg, "GATE_CACHE_MAX_ENTRIES", 500)))
    except Exception:
        return 500


def _gate_cache_ttl() -> float:
    try:
        return max(0.0, float(getattr(_cfg, "GATE_CACHE_TTL_S", 21600.0)))
    except Exception:
        return 21600.0


def _gate_cache_key(purpose: str, system: str, user: str,
                    max_tokens: int) -> Optional[str]:
    """生成缓存 key；缓存关闭或超长输入时返回 None（不缓存）。"""
    if not _gate_cache_enabled() or _gate_cache_max() <= 0:
        return None
    try:
        raw = f"{purpose}|{max_tokens}|{system}|{user}".encode("utf-8")
    except Exception:
        return None
    return hashlib.sha1(raw).hexdigest()


def _gate_cache_get(key: str) -> Optional[str]:
    ttl = _gate_cache_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _GATE_CACHE_LOCK:
        hit = _GATE_CACHE.get(key)
        if hit is None:
            return None
        ts, val = hit
        if now - ts > ttl:
            _GATE_CACHE.pop(key, None)
            return None
        return val


def _gate_cache_put(key: str, value: str) -> None:
    ttl = _gate_cache_ttl()
    if ttl <= 0 or not value:
        return
    now = time.time()
    max_n = _gate_cache_max()
    with _GATE_CACHE_LOCK:
        # 顺手清掉过期项，避免长期运行后无限增长
        if len(_GATE_CACHE) > max_n:
            expired = [k for k, (t, _v) in _GATE_CACHE.items()
                       if now - t > ttl]
            for k in expired:
                _GATE_CACHE.pop(k, None)
        # 仍然超上限 → 丢最旧的
        while len(_GATE_CACHE) >= max_n and _GATE_CACHE:
            oldest = min(_GATE_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _GATE_CACHE.pop(oldest, None)
        _GATE_CACHE[key] = (now, value)


def _gate_cache_stats() -> Dict[str, Any]:
    with _GATE_CACHE_LOCK:
        return {"entries": len(_GATE_CACHE), "max": _gate_cache_max(),
                "ttl_s": _gate_cache_ttl()}


# ══════════════════════════════════════════════════════════════════
# v434 P0-1：门卫**标题指纹**缓存（转发去重）
# ──────────────────────────────────────────────────────────────────
# 为什么需要（2026-11-17 实测）：同一条消息常常被 3 个源同时抓到
# （jin10 / feishu_jin10 / 东方财富），标题只差一个 `[hash:xxxxxxxx]` 前缀、
# 一个标点、或前后空格。原来的 `_gate_cache_key` 把 system+user 原文整体哈希，
# 这些"只差一个字符"的转载全部 miss → 同一条新闻付 2–3 次门卫。
# 指纹把「hash 前缀 / 标点 / 空格 / 大小写 / 全半角」抹平后再哈希：
# 只要语义同一句，就复用第一次的判定（temperature=0 → 判定只由文本决定）。
#
# 与 `_GATE_CACHE` 的分工：
#   · `_GATE_CACHE` 缓存**原始响应文本**（键=完整 prompt 哈希），跨 L2/L3 通用；
#   · `_GATE_FP_CACHE` 缓存**解析后的 gate dict**（键=标题指纹），只管门卫，
#     命中后连解析都省了。两层互不冲突，任一层命中都零请求。
_GATE_FP_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_GATE_FP_LOCK = threading.Lock()
_GATE_FP_STATS = {"hit": 0, "miss": 0, "put": 0}


def _gate_fp_enabled() -> bool:
    return str(os.getenv("GATE_FP_CACHE", "1")).strip() != "0"


def _gate_title_fp(title: str) -> Optional[str]:
    """标题指纹：抹平 hash 前缀/标点/空白/大小写后取 sha1。

    截 160 字：门卫 prompt 里的标题本身就截 300，过长的尾部差异
    （不同源的尾注）不该让指纹 miss。
    """
    if not _gate_fp_enabled():
        return None
    try:
        t = re.sub(r"\[hash:[a-zA-Z0-9]+\]", "", str(title or ""))
        t = unicodedata.normalize("NFKC", t).lower()
        t = re.sub(r"[\s\u3000]+", "", t)
        t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
        if len(t) < 8:          # 太短（"非农"）不缓存，容易误命中不同新闻
            return None
        return hashlib.sha1(t[:160].encode("utf-8")).hexdigest()
    except Exception:
        return None


def _gate_fp_get(fp: Optional[str]) -> Optional[Dict[str, Any]]:
    if not fp:
        return None
    ttl = _gate_cache_ttl()
    now = time.time()
    with _GATE_FP_LOCK:
        hit = _GATE_FP_CACHE.get(fp)
        if hit is None:
            _GATE_FP_STATS["miss"] += 1
            return None
        ts, val = hit
        if now - ts > ttl:
            _GATE_FP_CACHE.pop(fp, None)
            _GATE_FP_STATS["miss"] += 1
            return None
        _GATE_FP_STATS["hit"] += 1
        return copy.deepcopy(val)      # 调用方会改 dict，必须给副本


def _gate_fp_put(fp: Optional[str], gate: Optional[Dict[str, Any]]) -> None:
    if not fp or not gate:
        return
    max_n = _gate_cache_max()
    if max_n <= 0 or _gate_cache_ttl() <= 0:
        return
    now = time.time()
    with _GATE_FP_LOCK:
        if len(_GATE_FP_CACHE) >= max_n:
            oldest = min(_GATE_FP_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _GATE_FP_CACHE.pop(oldest, None)
        _GATE_FP_CACHE[fp] = (now, copy.deepcopy(gate))
        _GATE_FP_STATS["put"] += 1


def _gate_fp_stats() -> Dict[str, Any]:
    with _GATE_FP_LOCK:
        n = len(_GATE_FP_CACHE)
        h, m = _GATE_FP_STATS["hit"], _GATE_FP_STATS["miss"]
        return {"entries": n, "hit": h, "miss": m,
                "hit_rate": (h / (h + m)) if (h + m) else None}


# ── v479：门卫失败短期记忆 ─────────────────────────────────────────────
# 实测（2026-11-19）：门卫通道单通道，MiniMax-M3.1-Flash 两天被打了 7,256 次，
# 成功 3,832 次（成功率 53%）。失败后还要走「放宽超时重试 + OpenRouter 兜底」，
# 一条新闻最坏 4-5 次请求。而同一批新闻反复回放（待分析积压、gate-replay）时，
# **同一个标题**会被反复判、反复失败 —— 失败结果没被记住，等于每次重新烧一遍。
#
# 这里只记「这条标题近期调用失败过」这一个事实，TTL 默认 180 秒：
#   · 命中 → 不再发 LLM 请求，直接走规则判决 + 降级放行（gate_unavailable=2）
#   · 语义安全：不把失败当"噪音"，不伪造"可交易"，只是省掉重复的无用请求
_GATE_FAIL_MEMO: Dict[str, float] = {}
_GATE_FAIL_MEMO_TTL = float(os.getenv("TRIDENT_GATE_FAIL_MEMO_TTL", "180"))


def _gate_fail_recent(fp: Optional[str]) -> bool:
    if not fp:
        return False
    now = time.time()
    ts = _GATE_FAIL_MEMO.get(fp)
    if ts is None:
        return False
    if now - ts > _GATE_FAIL_MEMO_TTL:
        _GATE_FAIL_MEMO.pop(fp, None)
        return False
    return True


def _gate_fail_mark(fp: Optional[str]) -> None:
    if not fp:
        return
    if len(_GATE_FAIL_MEMO) > 4096:      # 简单防膨胀
        _GATE_FAIL_MEMO.clear()
    _GATE_FAIL_MEMO[fp] = time.time()


def _llm_breaker(alias: str, kind: str):
    """按 (通道别名, 错误类型) 取熔断器。

    kind="quota"  → 402/429（余额/额度打满）→ 30 分钟长冷却
    kind="auth"   → 401/403（key 失效）→ 15 分钟长冷却（不自动探测）
    kind="net"    → 超时/SSL/连接错误（网络抖动）→ 60 秒短冷却

    v322: 配额/鉴权类的 fail_threshold 从 2 降到 **1** ——
    一次失败就熔断。原来"失败 2 次才熔断"意味着每个已死通道至少被打 2 轮
    （每轮内部还各有 3 次 SSL 重试），是额度被快速打穿的主因之一。
    """
    try:
        from circuit_breaker import get as _cb_get
    except Exception:
        return None
    try:
        if kind == "quota":
            return _cb_get(f"llm_quota:{alias}", fail_threshold=1,
                           open_seconds=_QUOTA_COOLDOWN_S)
        if kind == "ratelimit":
            # 瞬时限速：阈值 2（避免单次抖动就熔断），45s 后即可重试
            return _cb_get(f"llm_ratelimit:{alias}", fail_threshold=2,
                           open_seconds=_RATELIMIT_COOLDOWN_S)
        if kind == "auth":
            return _cb_get(f"llm_auth:{alias}", fail_threshold=1,
                           open_seconds=_AUTH_COOLDOWN_S)
        return _cb_get(f"llm_net:{alias}", fail_threshold=3,
                       open_seconds=_NET_COOLDOWN_S)
    except Exception:
        return None


# 全池故障计数（进程内）：用于节流告警，避免每条新闻刷一行
_POOL_OUTAGE_COUNT = 0
_POOL_OUTAGE_ALERT_EVERY = 50

# v322: 最近一次「门卫池全失败」的 HTTP 状态码集合。
#
# 用途：区分两种完全不同的门卫失效，决定新闻是「冻结待回放」还是「降级放行」：
#   · 401/402/403（鉴权失效 / 欠费 / 封禁）—— **重试无意义**，key 不换就永远调不通。
#     冻结等回放 = 等一个永远不会成功的结果 = 数据永久积压（前端「待分析」只涨不跌）。
#     实测事故（2026-11-04）：minimax-gate 通道 HTTP 401 invalid api key (2049)
#     持续数小时 → 324 条新闻被永久冻结，主分析一条都认领不到。
#     → 这类必须**直接降级放行**，让主分析在缺门卫标签下处理。
#   · 429（限流）/ 网络故障 —— 是**瞬时**的，回放可能成功 → 保持"冻结待回放"语义。
_LAST_POOL_FAIL_CODES: set = set()

# 鉴权类状态码：出现这些说明「重试也不会好」
_AUTH_FAIL_CODES = frozenset({401, 402, 403})


def _note_pool_outage(reason: str) -> None:
    """全池不可用时的节流告警：每 N 次打一行汇总（而非每条新闻刷屏）。"""
    global _POOL_OUTAGE_COUNT
    _POOL_OUTAGE_COUNT += 1
    if _POOL_OUTAGE_COUNT % _POOL_OUTAGE_ALERT_EVERY != 1:
        return
    try:
        from circuit_breaker import report_all as _cb_report
        states = {k: v.get("state") for k, v in _cb_report().items()
                  if k.startswith("llm_")}
    except Exception:
        states = {}
    print(
        f"[FILTER] {_ts()} ⚠️  LLM 全池不可用（累计 {_POOL_OUTAGE_COUNT} 次）| {reason} "
        f"| 熔断态={states} —— 新闻将标记 gate_unavailable=1 保留待回放，不再当噪音丢弃",
        flush=True,
    )


def _call_pool_sync(
    system: str, user: str, *,
    max_tokens: int = 64, temperature: float = 0.0,
    timeout: float = 3.0, label: str = "POOL",
    purpose: str = "gate",            # v187: 默认走 gate 通道
    call_site: str = "realtime_filter",  # v187: token 埋点用
) -> Optional[str]:
    """按 purpose-routed 模型池顺序并行调用,第一个成功就用其结果。
    返回 message.content 字符串；全失败返回 None。

    v187：改为走 config.get_pool_for_purpose(purpose)（默认 gate 通道，含
    minimax-gate + 本地 qwen2.5:3b 兜底）。保留旧 v176.9 并行调用逻辑。

    v322：**先查门卫结果缓存** —— 命中就直接返回，一个请求都不发。
    """
    # ── v322: 门卫结果缓存（省钱核心 #2）────────────────────────────
    # 动机：同一条新闻（标题+摘要相同）会被反复过门卫 —— 重复新闻、
    #       多源转载、gate-replay 回放、批量补跑，每一轮都重新付费。
    #       实测（2026-11-04）：门卫 4278 次调用 / 3.68M token（占全天 40%），
    #       而主分析只产出 244 条信号。
    # 安全性：门卫 temperature=0、prompt 只含标题+摘要（无时间戳）
    #        → 同输入必然同输出，缓存不改变判定结果。
    _ck = _gate_cache_key(purpose, system, user, max_tokens)
    if _ck is not None:
        _hit = _gate_cache_get(_ck)
        if _hit is not None:
            print(f"[{label}] {_ts()} pool-cache-hit (零请求)", flush=True)
            return _hit

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # v187：按 purpose 取池（gate = minimax-gate + 本地 fast + minimax1/2 共用）
    try:
        import config as _cfg_v187
        pool_models = _cfg_v187.get_pool_for_purpose(purpose)
    except Exception:
        pool_models = []
    # 兼容旧代码：若 pool_models 为空，回退到原 3-channel 列表
    if not pool_models:
        pool_models = []
        for tag, base, key, mdl in [
            ("MiniMax", _MINIMAX_BASE, _MINIMAX_KEY, _MINIMAX_MODEL),
            ("MiniMax#2", _MINIMAX_BASE, _MINIMAX2_KEY, _MINIMAX_MODEL),
            ("Xinshu", _XINSHU_BASE, _XINSHU_KEY, _XINSHU_MODEL),
        ]:
            if base and key and mdl:
                pool_models.append({
                    "id": f"{tag}/{mdl}",
                    "api_base": base,
                    "api_key": key,
                    "request_model": mdl,
                    "label": f"{tag}/{mdl}",
                    "extra_body": {},
                    "key_alias": tag.lower().replace("#", ""),
                    "is_local": False,
                    "timeout": timeout,
                    "use_proxy": False,
                    "purpose": [purpose],
                })

    if not pool_models:
        # v322: 无池可用 —— 清空上一次的失败码（避免用陈旧状态误判鉴权失败）
        _LAST_POOL_FAIL_CODES.clear()
        return None

    base_payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    def _call_one(m: dict):
        # v176.9.1：每个 provider 必须把 model 字段显式填入 payload,
        # 否则 MiniMax-M3 等 API 收到空 model → 400 Bad Request.
        payload = dict(base_payload)
        payload["model"] = m.get("request_model") or m.get("id")
        # 把 extra_body 也合并进去（包含 max_tokens/thinking 禁用）
        for k, v in (m.get("extra_body") or {}).items():
            payload.setdefault(k, v)
        alias = m.get("key_alias", "?")
        # v320: 通道级熔断 —— 配额耗尽(429/401/402/403)长冷却(10min)，
        # 网络抖动(超时/SSL)短冷却(60s)。避免"每条新闻都对着已耗尽额度的通道
        # 重试 3 次"：既刷爆日志，又把 ingest 拖慢（实测全池 429 时 ingest 每条
        # 新闻要等 3 次重试 + 多通道串行）。熔断跳过不写 ai_call_stats 埋点
        # （那不是一次真实调用，计入会污染通道健康度统计）。
        brk_q = _llm_breaker(alias, "quota")
        brk_a = _llm_breaker(alias, "auth")
        brk_n = _llm_breaker(alias, "net")
        brk_r = _llm_breaker(alias, "ratelimit")
        if brk_q is None or brk_n is None:
            allowed = True
        else:
            allowed = (brk_q.allow() and brk_n.allow()
                       and (brk_a is None or brk_a.allow())
                       and (brk_r is None or brk_r.allow()))
        if not allowed:
            # ── v322 关键修复：熔断跳过也必须带上「失败类型」────────────────
            # 事故（2026-11-05 实测）：门卫通道 llm_auth:minimax-gate 处于 open
            # （24h 冷却）→ 这里直接跳过、不发请求 → err_code=None →
            # _LAST_POOL_FAIL_CODES 里**没有 401** → _attach_gate 误判为
            # 「瞬时故障」→ 标记 gate_unavailable=1 冻结待回放 →
            # 主分析的认领 SQL 排除这些行 → 队列永久卡死，「待分析」只涨不跌。
            #
            # 正确语义：**auth 熔断 = key 已失效，重试无用** → 必须降级放行，
            # 不能冻结等回放（等一个永远不会成功的结果）。
            # quota/net 熔断是瞬时的 → 仍应冻结待回放。
            _skip_kind = "network"
            try:
                if brk_a is not None and brk_a.state == "open":
                    _skip_kind = "auth"
                elif brk_q is not None and brk_q.state == "open":
                    _skip_kind = "quota"
                elif brk_r is not None and brk_r.state == "open":
                    # 限速也是瞬时的 → 与 quota 同样语义：冻结待回放（429）
                    _skip_kind = "quota"
            except Exception:
                pass
            _skip_code = {"auth": 401, "quota": 429}.get(_skip_kind)
            return (m.get("label") or m["id"], payload["model"], 0.0, None,
                    alias, m.get("is_local", False), "circuit_open", _skip_code)

        # ── v322: 额度闸门（门卫同样在烧钱）────────────────────────────
        # 实测（2026-11-04）：门卫今天烧了 3.68M token / 4273 次调用，
        # 占全天 9.2M 的 40% —— 而主分析只产出 244 条信号。
        # 门卫以前**完全不受**主分析的额度闸门约束，属于漏钱口。
        # 命中闸门 → 直接跳过该通道（不请求、不计费）。
        try:
            import llm_budget
            _ok, _reason = llm_budget.check(0, purpose=purpose)
            if not _ok:
                return (m.get("label") or m["id"], payload["model"], 0.0, None,
                        alias, m.get("is_local", False), "budget_blocked", None)
        except Exception:
            pass  # 预算模块异常 → 放行

        # ── v322: 速率闸门（真正发请求前扣名额）──────────────────────────
        # 治「瞬时打满」：熔断冷却一结束，所有排队的门卫请求同时醒来，
        # 几十个并发打向同一个 key → 立刻 429 → 再次熔断 → 循环抽风。
        # 这里扣的是**真实请求**的名额（认领阶段不扣，避免白扣）。
        try:
            import llm_budget as _lb_rpm
            _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="gate")
            if not _ok_rpm:
                return (m.get("label") or m["id"], payload["model"], 0.0, None,
                        alias, m.get("is_local", False), "rate_limited", None)
        except Exception:
            pass  # 闸门自身异常 → 放行

        err_sink: Dict[str, Any] = {}
        t0 = time.perf_counter()
        # v322: 本地通道用更短的超时上限 —— 本地 3B 答"要不要深挖"很快，
        # 不该让 ingest 等它 90 秒。超时即视为失败 → 回退远程。
        _eff_timeout = float(m.get("timeout") or timeout)
        if m.get("is_local"):
            try:
                import config as _cfg_lt
                _lt_cap = float(getattr(_cfg_lt, "GATE_LOCAL_TIMEOUT_S", 8.0))
                _eff_timeout = min(_eff_timeout, _lt_cap)
            except Exception:
                _eff_timeout = min(_eff_timeout, 8.0)
        out = _post_openai_compat(m["api_base"], m["api_key"], payload["model"],
                                  payload, _eff_timeout,
                                  err_sink=err_sink)
        dt = (time.perf_counter() - t0) * 1000
        if out:
            if brk_q is not None:
                brk_q.success()
            if brk_n is not None:
                brk_n.success()
            if brk_a is not None:
                brk_a.success()
            if brk_r is not None:
                brk_r.success()
        elif brk_q is not None and brk_n is not None:
            _kind = err_sink.get("kind") or "network"
            if _kind == "auth":
                # v322: 鉴权失效 → 24h 熔断（key 不换就永远不好）
                if brk_a is not None:
                    brk_a.failure()
            elif _kind == "quota":
                brk_q.failure()
            elif _kind == "ratelimit":
                # 2026-11-15: 瞬时限速 → 45s 短冷却，不污染额度熔断
                if brk_r is not None:
                    brk_r.failure()
            elif brk_a is not None and _kind == "http":
                # 上游 403 之类也算鉴权类
                brk_a.failure()
            else:
                brk_n.failure()
        return (m.get("label") or m["id"], payload["model"], dt, out,
                alias, m.get("is_local", False),
                "" if out else (err_sink.get("kind") or "network"),
                err_sink.get("http_code"))

    # ── v322: 本地优先（省钱核心）──────────────────────────────────────
    # 原实现把所有 provider **并行**提交 → 每次门卫都同时打远程通道，
    # 即使本地模型能立刻答出来（实测 15:17 本地门卫成功过）。
    # 实测（2026-11-04）：门卫 4273 次调用 / 3.68M token，占全天 9.2M 的 40%，
    # 而主分析只产出 244 条信号 —— 门卫是最大的漏钱口。
    #
    # 新逻辑（GATE_LOCAL_FIRST=1 时）：
    #   ① 先只提交**本地**通道（免费）；本地成功 → 直接返回，远程一个都不发；
    #   ② 本地全失败/超时 → 才提交远程通道兜底。
    # 这样正常情况下门卫的远程消耗降到 0。
    try:
        import config as _cfg_lf
        _local_first = bool(getattr(_cfg_lf, "GATE_LOCAL_FIRST", True))
        _local_timeout = float(getattr(_cfg_lf, "GATE_LOCAL_TIMEOUT_S", 8.0))
    except Exception:
        _local_first, _local_timeout = True, 8.0

    local_models = [m for m in pool_models if m.get("is_local")]
    remote_models = [m for m in pool_models if not m.get("is_local")]

    # ── v322: 远程通道也做「阶梯降级」────────────────────────────────────
    # 旧行为：本地不可用时**一次并行打满全部远程通道**，只用第一个返回成功的
    # 结果（没有投票逻辑）—— 每条新闻的请求数 = 通道数 × (L2+L3)。
    # 实测 2026-11-04：门卫 4397 次调用 / 606 条新闻 = 每条 7.26 次，
    # 而主分析总共才 607 次。88% 的请求配额烧在门卫上，其中绝大部分是浪费。
    #
    # 新行为（阶梯）：
    #   第 1 级 = weight 最高的**单个**远程通道 → 成功即返回（1 次请求）
    #   第 2 级 = 其余远程通道并行兜底 → 只在第 1 级全失败时才触发
    # 延迟不变（原本也是「任一成功即返回」，主通道成功同样是立刻返回）；
    # 主通道坏掉时自动降级到原并行行为，可靠性不降低。
    try:
        import config as _cfg_ss
        _stair = bool(getattr(_cfg_ss, "GATE_REMOTE_STAIR_STEP", True))
    except Exception:
        _stair = True

    _remote_groups: List[List[dict]] = []
    if remote_models:
        if _stair and len(remote_models) > 1:
            _remote_groups = [remote_models[:1], remote_models[1:]]
        else:
            _remote_groups = [remote_models]

    # 本地通道的处理：
    #   GATE_LOCAL_FIRST=1 → 本地排最前（免费优先），远程阶梯兜底
    #   GATE_LOCAL_FIRST=0 → 本地排**最后**当救命稻草（远程全挂时兜底）
    # 关键：本地**绝不能被丢弃** —— 8GB 机器上它确实慢/常超时，
    # 但远程全挂时它是唯一还能出结果的通道。
    _local_group_idx = -1
    if not _stair:
        # 关闭阶梯 → 完全还原旧行为（本地 + 全部远程一次并行）
        groups = [pool_models]
        _local_group_idx = 0
    elif _local_first and local_models:
        groups = [local_models] + _remote_groups
        _local_group_idx = 0
    else:
        groups = list(_remote_groups)
        if local_models:
            groups.append(local_models)
            _local_group_idx = len(groups) - 1
    if not groups:
        groups = [pool_models]

    # 并行调用所有 provider；任一成功立刻退出（不等其他 in-flight 线程——避免 Xinshu 阻塞拖垮整个 ingest）
    ex = ThreadPoolExecutor(max_workers=max(1, len(pool_models)))
    # fast_exit=True 让 finally 不再 wait=True，避免 Xinshu 30s timeout 拖垮 ingest
    fast_exit = {"value": False}
    # v322: 收集本轮真实失败的 HTTP 状态码（供门卫判定"冻结"vs"降级放行"）
    fail_codes: set = set()
    try:
        for gi, group in enumerate(groups):
            futures = [ex.submit(_call_one, m) for m in group]
            for fut in as_completed(futures):
                try:
                    tag, mdl, dt, out, key_alias, is_local, err_kind, err_code = fut.result()
                except Exception:
                    continue
                if err_kind in ("circuit_open", "budget_blocked"):
                    # 熔断跳过 / 额度闸门拦截：不是一次真实调用 → 不写埋点、不刷日志。
                    # 但**必须把鉴权/配额类熔断的语义码传出去**（见 _call_one 里的说明）：
                    # auth 熔断 = key 已失效 → 重试无用 → 上层必须降级放行而不是冻结待回放。
                    if err_code is not None:
                        try:
                            fail_codes.add(int(err_code))
                        except (TypeError, ValueError):
                            pass
                    continue
                if not out and err_code is not None:
                    try:
                        fail_codes.add(int(err_code))
                    except (TypeError, ValueError):
                        pass
                # v187：成功/失败都写 token 埋点(零开销,失败时 tokens=0)
                try:
                    import db as _db_filter
                    _db_filter.log_ai_call(
                        purpose=purpose, key_alias=key_alias, model_id=str(tag or "?")[:80],
                        tokens_in=int((max_tokens + len(system) + len(user)) // 4) if out else 0,  # 粗估
                        tokens_out=len(out) // 4 if out else 0,
                        latency_ms=int(dt),
                        # v453: 失败必须带 error_code —— 此前只写 status="http_error"，
                        # error_code 留空，24h 有 1,069+622 条失败无法归因
                        # （分不清 超时/限流/鉴权/网络），通道治理只能靠猜。
                        status="ok" if out else (
                            "timeout" if str(err_kind or "").lower() == "timeout" else "http_error"
                        ),
                        error_code="" if out else (
                            str(err_code) if err_code is not None else str(err_kind or "unknown")
                        ),
                        call_site=call_site,
                    )
                except Exception:
                    pass
                if out:
                    print(f"[{label}] {_ts()} pool-ok {tag}/{mdl} | {dt:.0f}ms (parallel)", flush=True)
                    fast_exit["value"] = True
                    ex.shutdown(wait=False, cancel_futures=False)
                    # v322: 成功后写缓存 —— 下次同样的新闻直接命中，零请求
                    if _ck is not None:
                        _gate_cache_put(_ck, out)
                    # v322: 本轮有成功 → 不存在"全池失败"，清空失败码
                    _LAST_POOL_FAIL_CODES.clear()
                    return out
                print(f"[{label}] {_ts()} pool-fail {tag}/{mdl} | {dt:.0f}ms ({err_kind}) (parallel)", flush=True)
            # 本组全部失败 → 若还有下一组则回退
            if _local_group_idx >= 0 and gi == _local_group_idx and gi + 1 < len(groups):
                print(
                    f"[{label}] {_ts()} 本地门卫未命中 → 回退远程通道兜底"
                    f"（本地超时上限 {_local_timeout:.0f}s）",
                    flush=True,
                )
            elif _stair and gi == 0 and len(groups) > 1 and gi != _local_group_idx:
                # v322: 阶梯降级 —— 主远程通道这一级没成，降级到下一级
                nxt = groups[gi + 1]
                print(
                    f"[{label}] {_ts()} 门卫主通道未命中 → 降级到备用通道"
                    f"（{len(nxt)} 个）",
                    flush=True,
                )
    finally:
        # 仅在"全失败"或异常时 wait=True；fast_exit=True（任一 provider 成功）则不等
        if not fast_exit["value"]:
            ex.shutdown(wait=True)
        else:
            ex.shutdown(wait=False)
    # v320: 全池失败 → 节流告警（让运维一眼看到"通道挂了"，而不是刷屏）
    _note_pool_outage(f"purpose={purpose} call_site={call_site}")
    # v322: 记录本轮真实失败的 HTTP 状态码 → 供 _attach_gate 区分
    #      「鉴权失效(401/402/403，重试无用 → 降级放行)」与
    #      「限流/网络(瞬时 → 冻结待回放)」
    _LAST_POOL_FAIL_CODES.clear()
    _LAST_POOL_FAIL_CODES.update(fail_codes)
    return None

# ── Ultra-minimal prompt (~40 tokens → fast + cheap) ──
# ── v322: L2 的 max_tokens 不能是 4 ──────────────────────────────────────
# 实测（2026-11-04）：MiniMax 是 reasoning 模型，即使下发 thinking:disabled
# 仍会**偶发**输出 <think>...</think>。max_tokens=4 时 think 一出现就把
# 预算吃光 → 输出被截断成 think 片段 → 解析不出 0/1 → 门卫返回 None
# → evaluate_news 全部放行 → **噪音涌进主分析**，配额黑洞。
# 实测：max_tokens=64 时 think(约 15 token) + 答案(1 token) 都能装下，
# 实测判定失败率 65% → 20%；提到 128 可再覆盖长 think。
# 多花的 64 token 相对主分析单条约 15K token 可以忽略。
_L2_MAX_TOKENS = 128

_L2_SYSTEM = (
    "You are a financial news filter. Output ONLY a single digit: "
    "1 if the headline is actionable for a macro/crypto trader, "
    "0 if it is noise.\n"
    "Do NOT output any other text, punctuation, or explanation."
)


_L2_BATCH_MAX_TOKENS = 320   # 8 条编号标题 + 可能的 think 余量


def _l2_batch_call(system: str, user: str) -> Optional[str]:
    """一次判多条：直接返回原始文本，解析与兜底交给 l2_batcher。"""
    return _call_pool_sync(
        system, user,
        max_tokens=_L2_BATCH_MAX_TOKENS, temperature=0.0,
        timeout=min(4.0, _FILTER_TIMEOUT_S), label="FILTER-L2-BATCH",
    )


def _l2_classify_haiku(title: str, content: str = "") -> Optional[int]:
    """Call L2 binary classifier. Returns 1 / 0, or None when unavailable.

    None 表示无密钥、超时或解析失败，由 evaluate_news 放行给 AI。

    v321：原来的顶部早退 `if not _OPENROUTER_KEY: return None` 是在
    **池子调用之前**返回的 —— 只要 OpenRouter 不可用，L2 就整层失效，
    池子（MiniMax）根本没机会跑，所有噪音直接放行给主分析（烧配额）。
    改成：池子是主路径always先跑；OpenRouter 只在兜底位置、且
    `_openrouter_ready()` 为真（开关打开 + key 非空 + 未熔断）时才试。
    """
    # Build minimal user message (title is the most signal-dense)
    text = title.strip()
    if content.strip():
        # Add first 150 chars of content for context
        text = f"标题: {text[:200]}\n摘要: {content[:150]}"

    payload = {
        "model": _FILTER_MODEL,
        "messages": [
            {"role": "system", "content": _L2_SYSTEM},
            {"role": "user", "content": text},
        ],
        "max_tokens": _L2_MAX_TOKENS,   # 见 _L2_MAX_TOKENS 定义
        "temperature": 0.0,       # Deterministic
    }
    # NOTE: NOT using response_format={"type":"json_object"} — Anthropic models
    # behave more reliably with plain-text prompts when only a single digit is needed.

    # 2026-09-17：先试池子（MiniMax/MiniMax#2/Xinshu），失败再回退 OpenRouter。
    # 池子在国内直连 1-5s，但 L2 默认 timeout=3s 容易超时——池子用 2.5s 让步。
    raw = _call_pool_sync(
        _L2_SYSTEM, text,
        max_tokens=_L2_MAX_TOKENS, temperature=0.0,
        timeout=min(2.5, _FILTER_TIMEOUT_S), label="FILTER-L2",
    )
    if raw:
        raw = raw.strip()
        # ── v322: 先剥离 thinking 再取判定 ────────────────────────────────
        # MiniMax 是 reasoning 模型，偶尔（即使下了 thinking:disabled 指令）
        # 仍会把思考过程裹进 <think>...</think>。
        # 旧代码直接 `re.search(r"\b([01])\b", raw)` 有两个致命问题：
        #   ① think 里出现数字（"0 or 1"、"第 1 步"）会被当成判定 → 误判；
        #   ② max_tokens 太小，think 被截断 → 答案根本没输出 → 解析失败
        #      → return None → 门卫放行 → **噪音全部涌进主分析**（配额黑洞）。
        # 现在：先剥掉 think 块（兼容未闭合的截断），再取**最后一个**独立 0/1。
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
        if "<think>" in cleaned:
            # 未闭合 = 被 max_tokens 截断，答案必然在 think 之后且没输出
            cleaned = cleaned.split("<think>")[0]
        hits = re.findall(r"\b([01])\b", cleaned)
        if hits:
            return int(hits[-1])
        # 池子回了非 0/1：当作噪声，让 evaluate_news 走兜底
        return None

    # ── 兜底：OpenRouter（若用户日后提供有效 key 且 OPENROUTER_ENABLED=1）──
    if not _openrouter_ready():
        return None

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{_OPENROUTER_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {_OPENROUTER_KEY}",
            "Content-Type": "application/json",
            # OpenRouter headers for reduced latency
            "X-Title": "Trident-RealtimeFilter",
        },
    )

    # SSL context
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = True
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass

    proxy_handler = None
    if _HTTP_PROXY:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": _HTTP_PROXY, "https": _HTTP_PROXY}
        )

    try:
        opener = (
            urllib.request.build_opener(proxy_handler)
            if proxy_handler
            else urllib.request.build_opener()
        )
        resp = opener.open(req, timeout=min(_FILTER_TIMEOUT_S, _OR_FALLBACK_TIMEOUT_S))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        # v321：与门卫兜底共用熔断（401 永久失败 / 429 配额）
        code = int(getattr(e, "code", 0) or 0)
        if code in (401, 403):
            _or_trip("HTTP %d 认证失败（key 失效/被吊销）" % code,
                     _OR_BREAK_AUTH_COOLDOWN_S)
        elif code == 429:
            _or_trip("HTTP 429 配额超限", _OR_BREAK_QUOTA_COOLDOWN_S)
        print(f"[FILTER] {_ts()} L2 HTTP {code} | {err_body}", flush=True)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"[FILTER] {_ts()} L2 network/timeout | {e}", flush=True)
        return None

    try:
        body = json.loads(resp.read().decode("utf-8"))
        raw = (body.get("choices", [{}])[0]
               .get("message", {})
               .get("content", "") or "")
        raw = raw.strip()
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        return None

    # Extract the first "0" or "1" from the response
    m = re.search(r"\b([01])\b", raw)
    if m:
        return int(m.group(1))
    return None


# ═══════════════════════════════════════════════════════════════════════════
# L3 — 门卫四步：挖核心影响 + 打标签（语义理解，非关键词匹配）
# ═══════════════════════════════════════════════════════════════════════════

_GATE_MODEL = os.getenv("REALTIME_GATE_MODEL", "deepseek/deepseek-chat")

# ── v431（2026-11-17）：门卫每日调用预算 —— 防止门卫把整个配额烧穿 ──
# 实测（当日 14:13）：全天 2836 次 LLM 调用里 **2473 次是门卫**（96%），
# 主分析只 167 次且成功率 97%。门卫一条新闻会顺着通道池挨个试
# （minimax-gate → minimax1 → minimax2），一条新闻吃掉 3 次调用，
# 860 条入库量下必然把 MiniMax 配额打空，主分析跟着全池熔断、当天零信号。
# 现在给门卫加日预算：超出后不再发起调用，直接返回 None →
# 上层走 v427 的"降级放行"，新闻照常进主分析，配额留给真正干活的 main。
# 预算按只读常量算，可用 REALTIME_GATE_DAILY_BUDGET 调整；设 0 表示不限制。
#
# v434 P0-1 修正（2026-11-17 20:30 实测）：预算 300 是**过度收紧**，而且计数在
# 进程内存里 —— 每次重启就清零重给 300，同一天能被重置好几次（今天 801 次成功
# 就是这么累积出来的），行为完全不可预期。
# 实测成本结构：门卫单次 ≈ 500 token（今日 801 次 / 40.1 万 token），
# 主分析单条 ≈ 8.2K token（今日 127 次 / 104.6 万 token） = 门卫的 16 倍。
# 预算打满后门卫整段跳过、新闻裸奔进主分析 → 反而更贵；门卫只要能过滤掉一部分
# 噪音就划算（排除 1 条 = 省 8.2K，成本 500）。所以：① 默认放到 900；
# ② 当日已用次数改为**从 ai_call_stats 读**，重启不再重置。
_GATE_DAILY_BUDGET = max(0, int(os.getenv("REALTIME_GATE_DAILY_BUDGET", "900") or 900))
_GATE_BUDGET_STATE = {"date": "", "used": 0}
_GATE_BUDGET_LOCK = threading.Lock()


def _gate_budget_used_today() -> int:
    """当日已发起多少次门卫调用（跨重启一致）。读不到就当 0，不阻塞门卫。

    v444：修掉一个**把预算闸门废掉**的口径 bug。
    旧写法 `strftime('%s','now','start of day','localtime')` 里的 'start of day' 是先在
    UTC 上取零点、再转本地时间 → 实际边界是**本地 08:00**（甚至跨天后落在昨天 16:00），
    导致"当日已用"严重低估：实测 1,535 vs 真实 5,968（少算 4,400 次）。
    预算闸门按 1,535 判断"还有额度" → 门卫一路打到把全站预算烧干。
    正确写法：先转本地时间，再取本地零点，最后转回 UTC epoch。
    """
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_call_stats WHERE purpose='gate' "
                "AND ts>=strftime('%s','now','localtime','start of day','utc')"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def _db_path() -> str:
    try:
        return _cfg.DB_PATH
    except Exception:
        return os.getenv("TRIDENT_DB_PATH") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "trident_event_bus.db",
        )


def _data_paused() -> bool:
    """v500: 暂停采集时**一律不发 LLM 请求**。

    事故背景（2026-11-20）：07:00 打上 DATA_PAUSED 之后，页面上"分析停了"，
    实际 5.5 小时里仍打了 **1,970 次** LLM（其中 1,866 次是门卫）、烧掉
    约 95 万 token —— 因为暂停白名单保留了 bridge，而实时过滤是 bridge
    内联跑的：新闻照进 → 每条照过门卫 → 照扣钱。
    暂停的语义必须是"零成本"，所以在这里硬挡。读标记做 5s 缓存。
    """
    global _PAUSED_CACHE
    now = time.time()
    if now - _PAUSED_CACHE[0] < 5.0:
        return _PAUSED_CACHE[1]
    try:
        p = os.path.join(os.path.expanduser("~"), ".trident", "state", "DATA_PAUSED")
        v = os.path.exists(p)
    except Exception:
        v = False
    _PAUSED_CACHE = (now, v)
    return v


_PAUSED_CACHE = (0.0, False)


def _gate_budget_allow() -> bool:
    """今日门卫预算是否还有余额；有则计数 +1。"""
    if _data_paused():
        return False
    # v501：门卫只用自己那条通道的欠费标记（gate 通道独立计费，别的 key 死了不影响它）
    try:
        from common import llm_budget as _lbq
        if _lbq.quota_dead("gate"):
            return False
    except Exception:
        pass
    if _GATE_DAILY_BUDGET <= 0:
        return True
    today = time.strftime("%Y-%m-%d")
    with _GATE_BUDGET_LOCK:
        if _GATE_BUDGET_STATE["date"] != today:
            _GATE_BUDGET_STATE["date"] = today
            # 重启后先读当天真实消耗，避免"重启 = 白送一份新预算"
            _GATE_BUDGET_STATE["used"] = _gate_budget_used_today()
        if _GATE_BUDGET_STATE["used"] >= _GATE_DAILY_BUDGET:
            return False
        _GATE_BUDGET_STATE["used"] += 1
        return True
# v443：10s → 25s。实测 gate 专用通道（MiniMax-M2.7-highspeed）单次耗时
# 5.2s / 7.2s / 12.0s / 12.2s / 31.7s 都有，原文 10s 超时 + 3 次重试 = 三连超时，
# 结果 23:26 那批新闻全部降级放行（无门卫判定），一小时 116 条里 96 条未判定。
# 25s 覆盖到 95 分位，配合 2 次重试，最坏 50s（比原来 64s 还短）。
# v480: 25s → 10s。实测 gate 成功中位 5.9s、P90 13.7s；25s×2 次重试最坏 50s，
# 高峰每分钟 50-68 单时把队列拖死（失败中位 12.9s，几乎都是 read timeout）。
# 10s×1 次最坏 20s，把"等一个大概率不来的结果"的时间省下来交给下一轮。
_GATE_TIMEOUT_S = float(os.getenv("REALTIME_GATE_TIMEOUT_S", "10.0"))

# ── v434 P0-1：通道故障期**别硬重试** ──────────────────────────────────
# 实测（2026-11-17 20:00–20:20）：门卫主通道 + 备用通道全部 net 失败时，
# 每条新闻仍然「先打一遍 → 放宽超时再打一遍」= 约 18 秒白等，最后照样
# 降级放行（is_noise=0 照常进主分析）。同一时段 44 条新闻 = 13 分钟纯等待，
# 全部发生在 ingest 的关键路径上（采集器串行调用门卫）。
# 现在：连续 N 条都是"所有通道都没返回"→ 判定通道处于系统性故障，
# 接下来 M 秒内**不再重试**（只打一遍就降级），故障恢复后第一次成功自动解除。
_GATE_NET_STREAK = 0
_GATE_RETRY_SKIP_UNTIL = 0.0
_GATE_RETRY_SKIP_AFTER = max(1, int(os.getenv("GATE_RETRY_SKIP_AFTER", "3") or 3))
_GATE_RETRY_SKIP_S = float(os.getenv("GATE_RETRY_SKIP_S", "300") or 300)


def _gate_skip_after() -> int:
    """阈值现读环境变量（import 时的常量只作默认值）。

    测试/排障时常在进程起来后才 setenv；若只在 import 读一次，改了就
    不生效，只能在 import 前设好，很容易踩空。
    """
    try:
        return max(1, int(os.getenv("GATE_RETRY_SKIP_AFTER", "") or _GATE_RETRY_SKIP_AFTER))
    except (TypeError, ValueError):
        return _GATE_RETRY_SKIP_AFTER


def _gate_skip_s() -> float:
    """免重试窗口时长，同样现读环境变量。"""
    try:
        return float(os.getenv("GATE_RETRY_SKIP_S", "") or _GATE_RETRY_SKIP_S)
    except (TypeError, ValueError):
        return _GATE_RETRY_SKIP_S


def _gate_net_fail() -> None:
    """记一次「全通道没返回」；连续到阈值就进入免重试窗口。"""
    global _GATE_NET_STREAK, _GATE_RETRY_SKIP_UNTIL
    _GATE_NET_STREAK += 1
    after, skip_s = _gate_skip_after(), _gate_skip_s()
    if _GATE_NET_STREAK >= after and time.time() >= _GATE_RETRY_SKIP_UNTIL:
        _GATE_RETRY_SKIP_UNTIL = time.time() + skip_s
        print(f"[GATE-L3] 连续 {_GATE_NET_STREAK} 条全通道无返回 → 未来 "
              f"{int(skip_s)}s 内不再重试（降级放行，不阻塞采集）", flush=True)


def _gate_net_ok() -> None:
    global _GATE_NET_STREAK, _GATE_RETRY_SKIP_UNTIL
    if _GATE_NET_STREAK or _GATE_RETRY_SKIP_UNTIL:
        print(f"[GATE-L3] 通道恢复（此前连续 {_GATE_NET_STREAK} 条无返回）", flush=True)
    _GATE_NET_STREAK = 0
    _GATE_RETRY_SKIP_UNTIL = 0.0


def _gate_retry_worthwhile() -> bool:
    return time.time() >= _GATE_RETRY_SKIP_UNTIL
# v176.9.1 (2026-09-22)：MiniMax-M3 是 reasoning 模型，thinking 块 2–5k tokens，
# 单纯 400 max_tokens 会被截断在 <think>... 里 → JSON 解析失败 → 误判 gate miss。
# 提到 1500：装得下完整 thinking + JSON 终结部分（实测 MiniMax-M3 ~3.5s 完成）。
# v398h：1500 → 800。实测门卫平均只输出 283 tokens（今日 0.79M/2793 次），
# 1500 只是 tail 上限，但 thinking 被禁用后并不需要这么大；降到 800 既够装
# 四步 JSON，又不至于一次被抽走 1500 的配额（门卫与主分析共用同一把 key，
# 单次占得越多，主分析越容易被挤到 429）。
_GATE_MAX_TOKENS = int(os.getenv("REALTIME_GATE_MAX_TOKENS", "800"))

# 市场底下的六根筋：兜底 / 信心 / 资金成本 / 流动性 / 供需 / 预期
_GATE_SYSTEM = """你是金融新闻的"看懂员"。目标：判断这新闻值不值得看、看了买谁。

底线主线（贯穿全程）：这条新闻动了市场的哪根筋（兜底/信心/资金成本/流动性/供需/预期），会不会有人因此拿真钱去买或卖。

按下面四步走，前两步任意一步"淘汰"，整条新闻直接判"排除"：

第一步·有没有重量：
这新闻是有实质影响、有分量的信息，还是无关/垃圾/已经炒烂的旧闻？
谁说的很重要：财长/央行行长/总统/OPEC官方 = 有重量（表态=信号）；
分析师/自媒体观点 = 没重量（观点=噪音）；无署名平台汇总 = 没重量。

第二步·影响谁：
它的内容会实际影响到哪些标的，按影响从大到小排出前 3 个（最少 1 个）。
标的只有六类：黄金、原油、美股、加密主流、山寨、个股。
一个都影响不到 -> 排除。

第三步·性质（三个维度都要答）：
a) 类型：分析类（带观点/预测/评论）还是 总结类（对已发生行情的盘点汇总）；
b) 时间：即时影响（当下就动）、趋势影响（未来慢慢兑现）、已定价（行情已走完，只剩事后统计）；
c) 力度：0-10（0无、5背景、7边界、9硬催化）。

第四步·打标签：综合四步，输出 verdict：可交易 / 排除（只有两个取值）。
并给一个 1-100 的自评打分 impact_1to100（你认为这条新闻对市场的真实冲击有多大；
60+ 才算"硬催化"，20 以下一律打 0，模型自己先过滤）。最终判定以 _harden_verdict
程序侧硬规则为准，你的 verdict 字段只作参考。

只输出 JSON，不要解释文字。"""

_GATE_USER_TEMPLATE = (
    "新闻标题：{title}\n新闻内容：{content}\n\n"
    "按系统四步，输出 JSON：\n"
    "{{\n"
    '  "step1": {{"is_material": true或false, "why": "一句话说明有/没有重量"}},\n'
    '  "step2": {{"assets": ["黄金","原油",...], "main": "最主要的一个"}},\n'
    '  "step3": {{"kind": "分析类|总结类", "timing": "即时|趋势|已定价", "strength": 0到10的整数}},\n'
    '  "core_impact": "一句话：动了谁的哪根筋、谁会拿真钱买什么",\n'
    '  "impact_1to100": 60,   # 1-100 的自评打分\n'
    '  "verdict": "可交易|排除"\n'
    "}}\n\n"
    '规则：step1.is_material=false -> verdict="排除"；step2.assets 为空 -> verdict="排除"；'
    'core_impact 写不出 -> verdict="排除"；拿不准 -> "排除"。'
    'impact_1to100 你给多少都行（不参与硬裁决），程序侧只用 NEWS_GATE_IMPACT_MIN 阈值。'
)

_ALLOWED_ASSET = {"黄金", "白银", "原油", "美股", "加密主流", "山寨", "个股"}
_ALLOWED_KIND = {"分析类", "总结类"}
_ALLOWED_TIMING = {"即时", "趋势", "已定价"}


def _harden_verdict(
    verdict: str,
    is_material: bool,
    assets: List[str],
    core: str,
    kind: str,
    timing: str,
    strength: int,
    impact_1to100: int = 0,
    missing_fields: bool = False,
) -> str:
    """程序侧硬裁决（不靠模型自觉）—— 纯函数，便于单测。

    v181 重构（二分类：可交易 / 排除，砍掉「只能看」）：
      1. 没重量 / 没影响标的 / 写不出核心影响 → 排除
      2. verdict 非法（不再接受"只能看"） → 排除
      3. 分析类（带观点/预测/评论）= 观点噪音 → 直接排除
         总结类 + 已定价（纯事后盘点） → 排除
      4. LLM 自评 impact_1to100 < NEWS_GATE_IMPACT_MIN → 排除（兜底淘汰）
      5. 总结类 + 即时 + 力度足 → 保持可交易

    v322 修正（**误杀真实新闻**）：
      事故（2026-11-05 实测）2340 条正常新闻（波兰央行/俄央行/IIF 数据/
      美联储官员讲话）被判"排除" —— 因为**模型输出缺字段**时
      is_material/assets/core 三者全为假 → 规则 1 直接"排除"。
      「模型没回答」≠「模型回答了不是」：前者是**不可判定**，
      按 v320 原则（不可判定 ≠ 是噪音）必须保守放行。
      现在 missing_fields=True 时跳过规则 1/2 —— 但**规则 3/4 仍生效**
      （分析类观点稿 + 低影响力照样排除，那两条不依赖缺失字段）。
    """
    if not missing_fields and (not is_material or not assets or not core):
        return "排除"
    if not missing_fields and verdict not in ("可交易", "排除"):
        return "排除"
    impact_threshold = getattr(_cfg, "NEWS_GATE_IMPACT_MIN", 20)

    # ── v423 修正：高分优先放行（优先于类型规则）────────────────────
    # 事故（2026-11-17 实测）：kind/timing 两条类型规则把"模型自己判为有重量、
    #   有明确影响标的、写得出核心影响、且 impact 分已达标"的新闻照样杀掉。
    #   当日 140 条排除中 38 条 is_material=1（strength 3-7），其中 25 条 impact>=20，
    #   包含"美联储哈玛克：通胀预期已合理锚定"（strength 6）、
    #   "Nabep 接手委内瑞拉原油升级设施"（strength 4）等真新闻。
    # 语义：类型（分析类/总结类）与时间（已定价）只是**修辞形态**，
    #   不该覆盖"有重量 + 有标的 + 有核心影响 + 影响分达标"这个实质结论。
    #   实质达标的 → 放行。
    if is_material and assets and core and impact_1to100 >= impact_threshold:
        return "可交易"

    # 分析类（观点稿） → 一律排除
    if kind == "分析类":
        return "排除"
    # 总结类 + 已定价 → 排除
    if kind == "总结类" and timing == "已定价":
        return "排除"
    # 力度门槛：strength 是 0-10, impact 是 1-100，低于各自阈值都排除
    if impact_1to100 and impact_1to100 < impact_threshold:
        return "排除"
    return verdict


def _post_openrouter(payload: Dict[str, Any], timeout: float) -> Optional[str]:
    """发 OpenRouter 请求，返回 message.content 字符串；失败返回 None。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{_OPENROUTER_URL}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {_OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "X-Title": "Trident-RealtimeGate",
        },
    )
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = True
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass
    handler = (
        urllib.request.ProxyHandler({"http": _HTTP_PROXY, "https": _HTTP_PROXY})
        if _HTTP_PROXY else None
    )
    opener = urllib.request.build_opener(handler) if handler else urllib.request.build_opener()
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # v321：HTTPError 是 OSError 的子类，历史上被归到 "network" 分支，
        # 导致 401 认证失败被误报成网络故障（排查时严重误导）。这里分开处理：
        #   · 401/403 → key 被吊销/不存在，**永久**失败 → 熔断长冷却
        #   · 429     → 配额超限，**暂时**失败 → 熔断短冷却
        #   · 其它 HTTP → 单次失败，不熔断
        code = int(getattr(e, "code", 0) or 0)
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:160]
        except Exception:
            err_body = ""
        if code in (401, 403):
            _or_trip("HTTP %d 认证失败（key 失效/被吊销）" % code,
                     _OR_BREAK_AUTH_COOLDOWN_S)
        elif code == 429:
            _or_trip("HTTP 429 配额超限", _OR_BREAK_QUOTA_COOLDOWN_S)
        print(f"[GATE] {_ts()} L3 HTTP {code} | {err_body}", flush=True)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"[GATE] {_ts()} L3 network | {e}", flush=True)
        return None
    try:
        body = json.loads(resp.read().decode("utf-8"))
        return body.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        return None


def _l3_semantic_gate(title: str, content: str = "") -> Optional[Dict[str, Any]]:
    """门卫四步：挖核心影响 + 打标签。返回 gate dict；调用失败返回 None（由上层降级）。

    返回 None 只代表"没跑到模型"，不代表"没把握"——上层按降级处理，不误杀。
    """
    # v431：日预算用尽 → 不再发起调用（上层会走降级放行，新闻不丢、配额不被烧穿）
    if not _gate_budget_allow():
        return None
    user_msg = _GATE_USER_TEMPLATE.format(
        title=(title or "").strip()[:300],
        content=(content or "").strip()[:500],
    )
    payload = {
        "model": _GATE_MODEL,
        "messages": [
            {"role": "system", "content": _GATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": _GATE_MAX_TOKENS,
    }

    # 2026-09-17：先池子（MiniMax/MiniMax#2/Xinshu），失败再回退 OpenRouter。
    raw_str = _call_pool_sync(
        _GATE_SYSTEM, user_msg,
        max_tokens=_GATE_MAX_TOKENS, temperature=0.0,
        timeout=_GATE_TIMEOUT_S, label="GATE-L3",
    )
    # v398h：门卫失败会让新闻卡在"未判定"里永远不进分析（实测 244 条 pending
    # 中 219 条是空/未判定，只有 7 条可交易）—— 卡住的是整条流水线，不只是
    # 那一次调用。这里对失败再做一次更宽容的重试（超时放宽 60%），仍然失败
    # 才交给上层降级。多花一次门卫调用（~500 token），换回一条能做决策的新闻。
    if raw_str is None:
        # v434：系统性故障期不再重试（见 _gate_net_fail 的实测说明）——
        # 一遍都拿不到时，第二遍只是把采集器多拖 8–18 秒，判定结果照样是降级放行。
        # 另外：若被**全局请求预算**拒掉（budget_blocked），重试和备用通道同样发不出去，
        # 直接放弃（实测 2026-11-17 预算烧干后每条新闻仍在走完整阶梯 + 重试 = 纯等待）。
        if "budget_blocked" in _LAST_POOL_FAIL_CODES:
            try:
                print("  [GATE-L3] 全局请求预算已耗尽 → 不再重试/不再走备用", flush=True)
            except Exception:
                pass
        elif _gate_retry_worthwhile():
            try:
                print(f"  [GATE-L3] 首次失败 → 放宽超时重试一次", flush=True)
            except Exception:
                pass
            raw_str = _call_pool_sync(
                _GATE_SYSTEM, user_msg,
                max_tokens=_GATE_MAX_TOKENS, temperature=0.0,
                timeout=_GATE_TIMEOUT_S * 1.6, label="GATE-L3-RETRY",
            )
        else:
            try:
                print("  [GATE-L3] 通道故障窗口内 → 跳过重试，直接降级放行", flush=True)
            except Exception:
                pass
    # ── 兜底：OpenRouter（仅在开关打开且未熔断时才试）──
    if raw_str is None and _openrouter_ready():
        raw_str = _post_openrouter(payload, min(_GATE_TIMEOUT_S, _OR_FALLBACK_TIMEOUT_S))
    if raw_str is None:
        _gate_net_fail()      # v434：累计"全通道无返回"，连续到阈值进免重试窗口
        return None
    _gate_net_ok()            # 拿到任意通道的结果 → 解除故障窗口

    try:
        raw = json.loads(raw_str)
    except json.JSONDecodeError:
        # v176.9.1 (2026-09-22)：MiniMax-M3 是 reasoning 模型，响应总是包裹在
        # `<think>...思考内容...</think>` 块里。需先剥离 thinking 再找 JSON。
        cleaned = re.sub(r"<think>.*?</think>", "", raw_str, flags=re.S)
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return None
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    step1 = raw.get("step1") or {}
    step2 = raw.get("step2") or {}
    step3 = raw.get("step3") or {}

    # ── v322: 区分「模型明确否」与「模型没说」────────────────────────────
    # 事故（2026-11-05 实测）：2340 条正常新闻（波兰央行/俄央行/IIF 数据/
    # 美联储官员讲话…）被门卫判成"排除" → is_noise=1 → 永远不会被分析，
    # 却在「待分析」计数里挂着。
    # 根因：模型输出**缺字段**时，下面三个判断全为假 → 硬裁决直接"排除"：
    #   · step1 整个缺失      → is_material=False
    #   · step2.assets 缺失   → assets=[]
    #   · core_impact 缺失    → core=""
    # 但「模型没回答」≠「模型回答了不是」—— 前者是**不可判定**，
    # 按 v320 的既定原则（"不可判定 ≠ 是噪音"）应**保守放行**，
    # 让主分析去判断，而不是在门卫这里把真实新闻丢掉。
    _step1_present = bool(raw.get("step1"))
    _step2_present = bool(raw.get("step2"))
    _core_present = "core_impact" in raw

    is_material = bool(step1.get("is_material"))
    core = str(raw.get("core_impact") or "").strip()
    assets = [a for a in (step2.get("assets") or []) if a in _ALLOWED_ASSET][:3]
    main = step2.get("main") if step2.get("main") in _ALLOWED_ASSET else (assets[0] if assets else None)
    kind = step3.get("kind") if step3.get("kind") in _ALLOWED_KIND else "总结类"
    timing = step3.get("timing") if step3.get("timing") in _ALLOWED_TIMING else "已定价"
    try:
        strength = max(0, min(10, int(step3.get("strength", 5) or 5)))
    except (TypeError, ValueError):
        strength = 5
    try:
        impact_1to100 = max(0, min(100, int(raw.get("impact_1to100", 0) or 0)))
    except (TypeError, ValueError):
        impact_1to100 = 0

    verdict = str(raw.get("verdict") or "").strip()

    # ── 程序侧硬裁决（不靠模型自觉）──
    verdict = _harden_verdict(
        verdict=verdict,
        is_material=is_material,
        assets=assets,
        core=core,
        kind=kind,
        timing=timing,
        strength=strength,
        impact_1to100=impact_1to100,
        # v322: 把「字段是否缺失」告诉硬裁决 → 缺失 ≠ 明确否
        missing_fields=(
            (not _step1_present) or (not _step2_present) or (not _core_present)
        ),
    )

    return {
        "core_impact": core,
        "is_material": is_material,
        "assets": assets,
        "main_asset": main,
        "kind": kind,
        "timing": timing,
        "strength": strength,
        "impact_1to100": impact_1to100,
        "verdict": verdict,
    }


def _check_news_age(published_at: str, ingested_ts: Optional[int] = None) -> Dict[str, Any]:
    """v181 L0 时效检查。返回 {stale, age_hours, threshold_h, source}。

    策略：
      - 有 published_at → 用 published_at 与 now 对比，阈值 NEWS_MAX_AGE_HOURS（默认 12h）。
      - 缺 published_at → 用 ingested_ts（采集入库 unix epoch 秒）作 fallback，阈值收紧为
        NEWS_MAX_AGE_FALLBACK_HOURS（默认 2h）—— 因为没有原始时间意味着采集器
        也没法信任其新鲜度，宁可错过不可错判。
      - 任何解析失败 / 时区异常 → 走 fallback 路径（最严）。
      - 两者都缺 → 按"已新鲜"放行（绝不让队列积压把所有新闻干掉）。
    """
    import config as _cfg

    now_utc = datetime.now(timezone.utc)
    primary_th = int(getattr(_cfg, "NEWS_MAX_AGE_HOURS", 12))
    fallback_th = int(getattr(_cfg, "NEWS_MAX_AGE_FALLBACK_HOURS", 2))

    parsed_dt: Optional[datetime] = None
    source = "none"
    pub = (published_at or "").strip()
    if pub:
        try:
            # 兼容 "2026-10-03 12:34:56" 和 ISO-8601
            cleaned = pub.replace("T", " ").replace("Z", "+00:00")
            if "+" in cleaned[10:] or cleaned.endswith(("Z",)):
                parsed_dt = datetime.fromisoformat(cleaned)
            else:
                # 无时区：当作 UTC（数据库里存的 timestamp 也是 UTC）
                parsed_dt = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
            source = "published_at"
        except (ValueError, TypeError):
            parsed_dt = None

    if parsed_dt is None and ingested_ts:
        try:
            parsed_dt = datetime.fromtimestamp(int(ingested_ts), tz=timezone.utc)
            source = "ingested_ts"
        except (ValueError, TypeError, OSError):
            parsed_dt = None

    if parsed_dt is None:
        # 两路都没拿到 → 放行（避免把没时间戳的合法新闻全部干掉）
        return {
            "stale": False,
            "age_hours": None,
            "threshold_h": None,
            "source": "none",
        }

    delta = now_utc - parsed_dt
    age_hours = max(0.0, delta.total_seconds() / 3600.0)
    threshold_h = primary_th if source == "published_at" else fallback_th
    return {
        "stale": age_hours > threshold_h,
        "age_hours": round(age_hours, 2),
        "threshold_h": threshold_h,
        "source": source,
    }


# ── v342: 门卫规则兜底 ─────────────────────────────────────────────────────
# 背景：MINIMAX_GATE_KEY / MINIMAX_MAIN_KEY / MINIMAX_API_KEY 实测是**同一个** key，
# 所以"gate 专用 + gate 备用"两条通道其实是共用一份配额（假冗余）。
# 配额一打满 → gate 全池不可用 → 新闻只能冻结等回放（实测断流数小时）。
# 这里给出**保守**兜底：只放行"高权重发言 + 关键宏观主题"这一类，
# 其余仍冻结等回放（不改变"不可判定≠噪音"的既有原则）。
_HIGH_AUTHORITY = (
    "美联储", "FOMC", "鲍威尔", "央行", "行长", "财长", "财政部", "白宫", "总统",
    "OPEC", "欧佩克", "能源部", "商务部", "SEC", "证监会", "国务院", "发改委",
    "非农", "CPI", "PPI", "PCE", "GDP", "PMI", "失业率", "通胀", "利率决议",
)
_MARKET_TOPIC = (
    "降息", "加息", "利率", "通胀", "CPI", "非农", "失业", "GDP", "PMI", "关税",
    "制裁", "减产", "增产", "库存", "ETF", "流动性", "缩表", "扩表", "回购",
    "抛售", "冻结", "降准", "汇率", "违约", "评级", "战争", "停火", "袭击",
    "避险", "风险偏好",
)
_ASSET_WORDS = (
    "黄金", "金价", "XAU", "白银", "原油", "油价", "WTI", "布伦特", "OIL",
    "比特币", "BTC", "以太", "ETH", "SOL", "加密", "币价", "美股", "纳指", "标普",
)


def rule_gate_fallback(title: str, content: str = "") -> Dict[str, Any]:
    """门卫 LLM 不可用时的规则判定（纯函数）。

    只做一件事：判断这条新闻是否属于"高权重发言 + 关键宏观主题"，
    命中 → 建议放行（gate_unavailable=2）；否则 → 继续冻结等回放。

    刻意保守：宁可漏放，不放大批噪音，否则主分析会被垃圾新闻挤爆配额。
    """
    text = f"{title or ''} {content or ''}"
    hits_auth = [w for w in _HIGH_AUTHORITY if w in text]
    hits_topic = [w for w in _MARKET_TOPIC if w in text]
    hits_asset = [w for w in _ASSET_WORDS if w in text]
    # 高权重主体 + ≥1 个市场主题 = 放行；仅主题没有主体（分析师观点类）不放行
    if hits_auth and (hits_topic or hits_asset):
        return {"pass": True, "reason": "authority+topic",
                "hits": {"authority": hits_auth[:3], "topic": hits_topic[:3], "asset": hits_asset[:3]}}
    if len(hits_asset) >= 2 and len(hits_topic) >= 2:
        return {"pass": True, "reason": "asset+topic>=2",
                "hits": {"authority": [], "topic": hits_topic[:3], "asset": hits_asset[:3]}}
    return {"pass": False, "reason": "not-enough-signal",
            "hits": {"authority": hits_auth[:3], "topic": hits_topic[:3], "asset": hits_asset[:3]}}


_RULE_ASSET_MAP = (
    ("WTI", ("原油", "炼油厂", "OPEC", "油价", "汽油", "柴油", "页岩油", "能源部", "输油")),
    ("XAU", ("黄金", "美联储", "降息", "加息", "CPI", "通胀", "非农", "欧洲央行",
             "国债", "美元指数", "鲍威尔", "沃勒", "关税", "地缘")),
    ("BTC", ("比特币", "BTC", "加密", "现货ETF", "交易所", "稳定币", "代币")),
    ("ETH", ("以太坊", "ETH", "DeFi", "质押")),
    ("SOL", ("SOL", "Solana")),
)


def _rule_asset_hint(text: str):
    """v449：规则门卫也要给"池内主资产"，否则主分析把宏观/地缘新闻判成非核心→routing_miss。

    事故：20:39 时 20 条 `可交易` 待分析里全是"美联储沃勒/欧洲央行人事/乌克兰袭击炼油厂"，
    主分析按池内清单（BTC/XAU/WTI/ETH/SOL）找不到标的 → 全部 routing_miss，零决策。
    """
    hits, assets = None, []
    for asset, words in _RULE_ASSET_MAP:
        if any(w in text for w in words):
            assets.append(asset)
            if hits is None:
                hits = asset
    return hits, assets


def rule_verdict(title: str, content: str = "") -> Dict[str, Any]:
    """v449（2026-11-18）：门卫 LLM 不可用时的**规则判决**（不再一律"未判定"）。

    用户定策：门卫降级为"规则为主 + LLM 抽样复核"。理由很硬：实测门卫
    `http_error/net` 占 50%，116/132 条待分析卡在"未判定"→ 之后按 gate_down
    归档 → 一天几乎不出决策。

    规则只用确定性判据（零成本）：
      · 高权重主体 + 市场主题/资产词  → 可交易
      · ≥2 资产词 + ≥2 主题词          → 可交易
      · 其余                            → 无信号（按噪音归档，留痕可回捞）

    LLM 门卫仍然优先：能调通就用 LLM 判定，这条只在失败/配额耗尽时兜底。
    """
    text = f"{title or ''} {content or ''}"
    _rg = rule_gate_fallback(title, content)
    if _rg.get("pass"):
        _asset, _assets = _rule_asset_hint(text)
        # v450（2026-11-19）：规则命中只代表"值得进主分析"，**不等于门卫判了可交易**。
        # v449 曾直接返回"可交易"，实测在门卫抖动期（http_error 43%）把 91% 的新闻
        # 都标成可交易（当日 648 条可交易里仅 6 条真过门），队列被假信号灌满、
        # 主分析方向判断被稀释。这里改为如实标"未判定"（仍放行、仍进分析），
        # 由主分析自己判断方向，不再伪造可交易标签。
        return {"verdict": "未判定", "reason": "rule:" + str(_rg.get("reason")),
                "hits": _rg.get("hits"), "rule": True,
                "main_asset": _asset, "assets": _assets}
    # L1 CORE 关键词命中（池内资产 + 强事件）也算可用信号
    try:
        _l1 = _l1_classify(f"{title or ''} {content or ''}")
    except Exception:
        _l1 = None
    if _l1 and _l1.get("verdict") == "CORE":
        _asset, _assets = _rule_asset_hint(text)
        return {"verdict": "未判定", "reason": "rule:l1-core", "hits": None,
                "rule": True, "main_asset": _asset, "assets": _assets}
    return {"verdict": "无信号", "reason": "rule:no-signal", "hits": None, "rule": True}


def _attach_gate(result: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
    """把门卫标签挂到 filter 结果上；门卫判"排除" → 置 is_noise=1（引擎不认领分析）。

    v176.9 (2026-09-22)：门卫 LLM 调用失败时（gate is None）改为置 is_noise=1，
    让 ingest 阶段直接丢弃该条新闻（不入 raw_news.PENDING），不再让 ai_worker 主分析
    跑出"AI 观望"的假信号卡片。前端会显示"门卫无信号/暂不可用"而不是伪造的"六段推演"。
    旧行为：降级 verdict="只能看" → 主分析仍跑 → 看起来像 AI 真分析 → 实际是被瞎门卫强制 HOLD。

    v320 (2026-11-01)：修正 v176.9 的一个致命副作用 —— 当门卫**通道系统性故障**
    （配额耗尽 429 / 鉴权失效 401，连续数小时全池不可用）时，fail-closed 会把
    成百上千条"不可判定"的新闻永久销毁，且 LLM 恢复后也回放不了。
    实测事故：MiniMax 两个账户 Token Plan 耗尽 → 全池 429 持续 3h15m / 2651 次失败
    → 该时段新闻 100% 被判噪音丢弃 → 零交易信号、且数据不可恢复。
    新语义：**"不可判定" ≠ "是噪音"**。
      · is_noise 保持 0（不当噪音），relevance_score 保留 L1/L2 打分
      · status 保持 PENDING（留在库里等回放）
      · 标记 gate_unavailable=1 → ai_worker 认领时排除（仍不产出伪造信号）
      · LLM 恢复后由 replay_deferred_gate.py 重跑门卫补判并放行
    注：单次抖动同样走此路径（可回放比丢弃更安全），只是量级极小。
    """
    # v434 P0-1：先查标题指纹缓存 —— 同一条消息被多源转载/回放时零请求复用判定。
    # 不命中才真调 LLM；调用成功再写回指纹缓存（失败不缓存，避免把瞬时故障固化）。
    _fp = _gate_title_fp(title)
    gate = _gate_fp_get(_fp)
    if gate is not None:
        print(f"[GATE] {_ts()} fp-cache-hit (零请求) | verdict={gate.get('verdict')} "
              f"| '{str(title)[:50]}'", flush=True)
    elif _gate_fail_recent(_fp):
        # v479：这条标题刚失败过（180s 内）→ 不再烧一次 LLM，直接走规则判决。
        gate = None
        print(f"[GATE] {_ts()} fail-memo-hit (零请求) → 规则判决 | '{str(title)[:50]}'",
              flush=True)
    else:
        gate = _l3_semantic_gate(title, content)
        if gate is None:
            _gate_fail_mark(_fp)
        _gate_fp_put(_fp, gate)
    result["gate"] = gate
    if gate is None:
        # ── v322: 区分「鉴权失效」与「限流/网络」—— 决定冻结还是降级放行 ──
        #
        # 鉴权失效(401/402/403)：key 坏了，**重试一万次也调不通**。
        #   冻结等回放 = 等一个永远不会成功的结果 = 这条新闻永远躺在「待分析」里。
        #   实测事故（2026-11-04）：minimax-gate 通道 HTTP 401 invalid api key (2049)
        #   持续数小时 → 324 条新闻被永久冻结，可认领量掉到 1 条 → 零信号产出。
        #   → 直接降级放行：is_noise 保持 0、gate_unavailable=0，
        #     主分析照常处理（决策层对缺门卫标签有既有兜底路径）。
        #
        # 限流(429)/网络故障：是**瞬时**的，回放可能成功 → 保持"冻结待回放"语义。
        #   即便如此，冻结也**不是永久状态** —— ai_worker 主循环有
        #   「冻结超 30 分钟自动放行」兜底（F3b），不会无限期积压。
        #
        # v322 修复：判据从「全部失败码都是鉴权类」改成「**存在**鉴权类失败」。
        # 原来用 issubset：只要有一个通道是网络失败（本地 Ollama 超时很常见），
        # 整个集合就不是鉴权子集 → 走冻结分支 → 队列卡死。
        # 语义上应该是「只要有一个远程通道鉴权失效，就说明门卫整体不可用」→
        # 降级放行。多个失败码并存时，鉴权失效的优先级更高（它不可恢复）。
        auth_failed = bool(_LAST_POOL_FAIL_CODES & _AUTH_FAIL_CODES)
        if auth_failed:
            # ── FIX-8b（2026-11-09）：降级放行必须"真的放行" ──
            # 修复前写 gate_unavailable=0，于是主分析端看到
            # `gate_strength=0 + verdict='无信号'`，把这条新闻当"没信号"丢弃并标 FAILED，
            # **设计说放行、实现是打死**（实测近 24h 449 条里 344 条就这么没了）。
            # 现在写 2 = "门卫鉴权失效/额度耗尽，未给判定，但明确放行"，
            # ai_worker 见到 2 会照常写 ai_decisions（决策上没有门卫标签，下游可识别）。
            result["gate_unavailable"] = 2
            result["gate_miss"] = True
            # v449：不再一律"未判定"（那会让 90% 新闻卡在门卫上）。改走规则判决。
            _rv = rule_verdict(title, content)
            result["gate_verdict"] = _rv["verdict"]
            result["gate_rule_only"] = 1
            result["gate"] = dict(_rv, reason="gate-auth-failed/" + str(_rv.get("reason")))
            if _rv["verdict"] == "无信号":
                result["is_noise"] = 1
                result["status"] = "FILTERED"
            print(
                f"[GATE] {_ts()} MISS  | 门卫鉴权失效(status={sorted(_LAST_POOL_FAIL_CODES)})"
                f" → 降级放行(不冻结,否则数据永久积压) | '{title[:60]}'",
                flush=True,
            )
            return result

        # ── v342: 先给规则兜底一个机会（配额打满时不再无条件冻结） ──
        if str(os.getenv("TRIDENT_GATE_RULE_FALLBACK", "1")).strip() != "0":
            _rg = rule_gate_fallback(title, content)
            if _rg.get("pass"):
                result["gate_unavailable"] = 2
                result["gate_miss"] = True
                result["gate_rule_approved"] = 1
                # v450（2026-11-19 修 v449 带来的失真）：
                # v449 曾让"规则兜底放行"直接写成"可交易"，本意是别让主分析拿不到信号，
                # 实测后果是**所有兜底行都被升级成可交易**：2026-11-19 13 点回灌高峰，
                # 门卫 30 分钟内 199 次调用有 85 次失败 → 593 条全部兜底 → 当日 648 条
                # "可交易"里只有 6 条是真过门的，队列被假信号灌满、方向判断被稀释。
                # 正确语义：兜底放行 ≠ 门卫判了可交易。仍写 2（放行、不被冻结），
                # 但判定要如实标"未判定"，让主分析自己判断，不伪造可交易标签。
                result["gate_verdict"] = "未判定"
                result["gate_rule_only"] = 1
                result["gate"] = {"verdict": "未判定", "reason": "gate-rule-fallback",
                                  "rule_hits": _rg.get("hits")}
                print(
                    f"[GATE] {_ts()} MISS  | gate LLM 不可用 → 规则兜底放行"
                    f"({_rg.get('reason')}) | '{title[:60]}'",
                    flush=True,
                )
                return result

        # ── v427（2026-11-17）：门卫 LLM 超时/限流 → **降级放行**，不再冻结 30 分钟。
        # 事故实测：13:00 入库高峰把 gate 通道（MiniMax-M2.7-highspeed）打满，
        # 13 点那批 202 条全部 gate_unavailable=1 冻结，当日 DONE 卡在 18 条、零新信号。
        # 冻结的本意是"可回放比丢弃安全"，但 30 分钟延迟已超过 45 分钟建仓窗口
        # （news_trade_max_age_seconds=2700），等于"救回了新闻、错过了交易"。
        # 现在：超时/限流/熔断一律走 gate_unavailable=2（未判定但放行），
        # 由主分析自行判断；要退回旧行为可设 TRIDENT_GATE_FREEZE_ON_TIMEOUT=1。
        if str(os.getenv("TRIDENT_GATE_FREEZE_ON_TIMEOUT", "0")).strip() != "1":
            result["gate_unavailable"] = 2
            result["gate_miss"] = True
            _rv = rule_verdict(title, content)
            result["gate_verdict"] = _rv["verdict"]
            result["gate_rule_only"] = 1
            result["gate"] = dict(_rv, reason="gate-llm-timeout-pass/" + str(_rv.get("reason")))
            if _rv["verdict"] == "无信号":
                result["is_noise"] = 1
                result["status"] = "FILTERED"
            print(
                f"[GATE] {_ts()} MISS  | 门卫 LLM 不可用(status={sorted(_LAST_POOL_FAIL_CODES) or 'net'})"
                f" → 降级放行(不冻结，避免错过建仓窗口) | '{title[:60]}'",
                flush=True,
            )
            return result

        # 门卫 LLM 不可用（限流/网络/熔断）—— 标记"未判定"，保留待回放
        # v452 修：这里原来写 `gate_unavailable = 2`，与本分支的语义（FREEZE_ON_TIMEOUT=1
        # 显式要求冻结待回放）矛盾 —— 开关形同虚设，且把"未判定"行当成了"降级放行"。
        result["gate_unavailable"] = 1
        result["gate_miss"] = True
        result["gate_verdict"] = "未判定"
        result["gate"] = {"verdict": "未判定", "reason": "gate-llm-unavailable"}
        # 不改 is_noise / relevance_score / status —— 保留原判定，交给回放流程
        print(
            f"[GATE] {_ts()} MISS  | gate LLM 不可用(status={sorted(_LAST_POOL_FAIL_CODES) or 'net'})"
            f" → 标记 gate_unavailable=1 保留待回放 "
            f"(不当噪音,超30分钟自动放行) | '{title[:60]}'",
            flush=True,
        )
        return result

    result["gate_unavailable"] = 0
    verdict = gate["verdict"]
    # ── v449 收口：门卫 LLM 内部降级（通道熔断窗口 "直接降级放行"）会返回
    # verdict='未判定'，从这里漏出去 → 27/30 条又变回未判定，白改一遍。
    # 凡是 LLM 没给真结论（未判定/空/不可用），一律用规则判决补位。
    if str(verdict or "").strip() not in ("可交易", "排除", "无信号"):
        _rv = rule_verdict(title, content)
        verdict = _rv["verdict"]
        result["gate_unavailable"] = 2
        result["gate_miss"] = True
        result["gate_rule_only"] = 1
        result["gate"] = dict(_rv, reason="gate-llm-undetermined/" + str(_rv.get("reason")))
        gate = result["gate"]
        if verdict == "无信号":
            result["is_noise"] = 1
            result["status"] = "FILTERED"
    result["gate_verdict"] = verdict
    if verdict == "排除":
        result["is_noise"] = 1
        result["relevance_score"] = 0.0
        result["status"] = "FILTERED"

    print(
        f"[GATE] {_ts()} {verdict:4s} | {(gate.get('main_asset') or '-'):6s} | "
        f"s={gate.get('strength')} | {gate.get('timing')} | '{title[:50]}'",
        flush=True,
    )
    return result


_POOL_TARGETS = frozenset({"BTC", "ETH", "SOL", "XAU", "XAG", "WTI"})

# 门卫写「类」而不是品种时的池内代理（只在正文也找不到具体代码时用）。
# 加密主流：池里 BTC/ETH/SOL 都在，但一条没点名币种的加密新闻，用 BTC 做市场 beta
# 比留空（下游 v451 没得填 → NONE）更有信息量；山寨不映射（不在池内，照旧排除）。
_CLASS_TO_POOL = {
    "加密主流": "BTC", "加密货币": "BTC", "数字货币": "BTC", "虚拟货币": "BTC",
    "CRYPTO": "BTC",
    "贵金属": "XAU",
}


def _finalize_gate_assets(result: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
    """v453 P0-3：门卫主品种归一 + 股票桶降级。

    实测（2026-11-18）门卫判「可交易」的 921 条里，`gate_main_asset` 写的是
    `个股`243 / `原油`58 / `黄金`51 / `美股`35 / `加密主流`17 —— 只有 190 条是
    XAU/BTC/WTI/ETH/SOL 这类代码。下游 v451「按 main_asset 填池」只认代码，
    于是 297 条个股/桶标签照常走完整主分析，最后产出 NONE + 模板观望。

    这里做两件事（不改门卫判定本身，只收回口径）：
      1) 用 `analysis.route.route_news` 把 `黄金/GOLD/OIL/原油/比特币` 归一成池内代码，
         写回 gate["main_asset"]，入库后下游才认得出；
      2) 路由判 `out_of_scope`（个股/桶标签且正文无核心别名、无宏观传导）
         的「可交易」直接降级成「排除」—— 这类票本地无行情源、池里也不交易，
         走完门卫+主分析只会得到一条假 HOLD。
    保守原则沿用 route 模块：判不准就保留「可交易」进分析，绝不误杀核心新闻。
    """
    gate = result.get("gate")
    if not isinstance(gate, dict):
        return result
    verdict = str(result.get("gate_verdict") or gate.get("verdict") or "").strip()
    if verdict != "可交易":
        return result
    try:
        from analysis.route import route_news as _route
    except Exception as _imp_exc:
        print(f"[GATE] {_ts()} v453 route import 失败(保持原判定): "
              f"{type(_imp_exc).__name__}", flush=True)
        return result
    try:
        rr = _route(
            gate_main_asset=str(gate.get("main_asset") or ""),
            gate_assets=gate.get("assets") or "",
            news_text=f"{title} {content}",
        )
    except Exception as _rt_exc:
        print(f"[GATE] {_ts()} v453 route 失败(保持原判定): "
              f"{type(_rt_exc).__name__}: {str(_rt_exc)[:80]}", flush=True)
        return result

    if rr.scope == "out_of_scope":
        gate["verdict"] = "排除"
        gate["main_asset"] = ""
        gate["reason"] = f"v453-out-of-scope/{rr.reason}"
        result["gate"] = gate
        result["gate_verdict"] = "排除"
        result["is_noise"] = 1
        result["relevance_score"] = 0.0
        result["status"] = "FILTERED"
        print(f"[GATE] {_ts()} DOWN  | v453 越界降级({rr.reason}) | '{str(title)[:60]}'",
              flush=True)
        return result

    if rr.target and rr.target in _POOL_TARGETS and \
            str(gate.get("main_asset") or "").strip().upper() != rr.target:
        old = gate.get("main_asset")
        gate["main_asset"] = rr.target
        result["gate"] = gate
        print(f"[GATE] {_ts()} NORM  | {old or '-'} → {rr.target} ({rr.reason}) "
              f"| '{str(title)[:50]}'", flush=True)
        return result

    # 路由没给出具体代码（如门卫只写「加密主流」）→ 用类代理兜一层。
    _raw_ma = str(gate.get("main_asset") or "").strip().upper()
    _proxy = _CLASS_TO_POOL.get(_raw_ma)
    if _proxy:
        gate["main_asset"] = _proxy
        result["gate"] = gate
        print(f"[GATE] {_ts()} NORM  | {_raw_ma} → {_proxy} (class-proxy) "
              f"| '{str(title)[:50]}'", flush=True)
    return result


def _gate_scope_should_skip(text: str) -> bool:
    """GATE_CALL_SCOPE=pool 时：没有池内资产/宏观驱动锚点 → 跳过门卫（省配额）。

    默认（all / 未设置）恒返回 False，即保持原有"每条非噪音都进门卫"的行为。
    词表来源与池外路由同一个（`feeds.news_router.POOL_KEYWORDS + ANCHOR_EXTRA`），
    避免两套词表各自漂移；词表不可用时选择"不收紧"（宁可多调，不可少判）。

    每次调用都读 env（不缓存）→ 改 `.env` 重启即生效，也方便单测。
    """
    if str(os.getenv("GATE_CALL_SCOPE", "all")).strip().lower() != "pool":
        return False
    try:
        from feeds.news_router import _has_anchor
        return not _has_anchor(text or "")
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

# ── 门卫 L2 微批：把 N 条判定压成一次调用（2026-11-16）─────────────
# 开关 L2_BATCH_ENABLED=0 可一键退回一条一请求；注入失败时 classify() 自动退化为单条。
try:
    import l2_batcher as _l2_batcher
    _l2_batcher.configure(single=_l2_classify_haiku, batch=_l2_batch_call)
except Exception as _exc:  # 绝不因为优化把门卫本身搞挂
    _l2_batcher = None
    print(f"[FILTER] l2_batcher 不可用，回退单条调用: {_exc}", flush=True)


def evaluate_news(title: str, content: str = "", published_at: str = "", ingested_ts: Optional[int] = None) -> Dict[str, Any]:
    """Filter a single news item at ingest time.

    Call this BEFORE inserting into `raw_news`.  The returned dict gives you
    the correct `status`, `is_noise`, `relevance_score`, and v176+ `kind`
    (L2 二级分类：黄金/原油/加密/股票/宏观/政策/地缘/公司/行业/观点) to write.

    Args:
        title:        News headline / title.
        content:      Optional body text (first ~150 chars used for L2 context).
        published_at: v181 新增 — 新闻原始发布时间（ISO-8601，采集器解析）。
                      空串 → 走 NEWS_MAX_AGE_FALLBACK_HOURS 收紧窗口（fallback 源 = ingested_ts）。
        ingested_ts:  v181 新增 — 采集入库 unix epoch 秒（用于 published_at 缺失时的 fallback）。

    Returns:
        {
            "is_noise":         0 or 1,
            "relevance_score":  float 0.0–1.0,
            "status":           "PENDING" or "FILTERED",
            "layer":            "L1" or "L2",
            "verdict":          "CORE" | "JUNK" | "L2_SIGNAL" | "L2_NOISE" | "L2_TIMEOUT" | "STALE",
            "matched_keyword":  str | None (L1 only),
            "elapsed_ms":       float,
        }
    """
    t0 = time.perf_counter()

    # ── v396 P3 L0-POOL: 交易池外的个股公告，进 LLM 之前就分流 ──
    # 实测 273/494 条决策（55%）是 target_none 降级，源头是 A 股个股公告
    # （总经理辞职/财报/回购/客座率），而交易池只有 BTC/XAU/WTI/ETH/SOL。
    # 每条都要过门卫 + 主分析两次 LLM，最后判 NONE、记一条假 HOLD。
    try:
        if str(os.getenv("NEWS_POOL_ROUTER", "1")).strip() != "0":
            # v432（2026-11-17）：模块已迁到 feeds/ 包（顶层垫片在 P3 收尾时删除），
            # 旧路径 `import news_router` 每次都 ModuleNotFoundError → 池外过滤整段失效，
            # 所有非交易池新闻（个股/财报/赛事）都照常过门卫，白烧额度。
            from feeds import news_router as _nr
            _pool = _nr.pool_out_reason(title, content)
            if _pool:
                elapsed = (time.perf_counter() - t0) * 1000
                print(
                    f"[FILTER] {_ts()} L0-POOL | {_pool} | '{title[:60]}'",
                    flush=True,
                )
                return {
                    "is_noise": 1,
                    "relevance_score": 0.0,
                    "status": "FILTERED",
                    "layer": "L0-POOL",
                    "verdict": "池外个股",
                    "matched_keyword": None,
                    "elapsed_ms": round(elapsed, 2),
                    "gate": {"verdict": "池外个股", "reason": _pool},
                }
    except Exception as _pool_exc:  # 路由失败不阻断入库
        print(f"[FILTER] {_ts()} POOL-ROUTER 失败(放行): "
              f"{type(_pool_exc).__name__}: {str(_pool_exc)[:80]}", flush=True)


    # ── v181 L0: 时效检查（在 L1 之前 — 过期新闻一律排除，省 LLM 流量）──
    age_verdict = _check_news_age(published_at, ingested_ts)
    if age_verdict.get("stale"):
        elapsed = (time.perf_counter() - t0) * 1000
        print(
            f"[FILTER] {_ts()} L0 STALE | age={age_verdict.get('age_hours', '?')}h "
            f"threshold={age_verdict.get('threshold_h')}h src={age_verdict.get('source')} | '{title[:60]}'",
            flush=True,
        )
        return {
            "is_noise": 1,
            "relevance_score": 0.0,
            "status": "FILTERED",
            "layer": "L0",
            "verdict": "STALE",
            "matched_keyword": None,
            "elapsed_ms": round(elapsed, 2),
            "gate": None,
            "gate_unavailable": 0,
            "age_hours": age_verdict.get("age_hours"),
            "threshold_hours": age_verdict.get("threshold_h"),
            "age_source": age_verdict.get("source"),
        }

    # ── L1: keyword matching (microseconds) ──
    _ensure_fresh()  # 动态同步提示词库（TTL 内不重复加载）
    l1_result = _l1_classify(title + " " + content[:300])

    # L1 命中 JUNK → 立即丢弃，不进后续
    if l1_result is not None and l1_result.get("is_noise") == 1:
        elapsed = (time.perf_counter() - t0) * 1000
        l1_result["elapsed_ms"] = round(elapsed, 2)
        l1_result["gate"] = None
        l1_result["gate_unavailable"] = 0
        print(
            f"[FILTER] {_ts()} L1 {l1_result['verdict']:5s} | "
            f"kw='{l1_result['matched_keyword']}' | "
            f"{elapsed:.1f}ms | "
            f"'{title[:60]}'",
            flush=True,
        )
        return l1_result

    # L1 命中 CORE → 跳过 L2，直接进门卫(L3)
    if l1_result is not None:
        print(
            f"[FILTER] {_ts()} L1 {l1_result['verdict']:5s} | "
            f"kw='{l1_result['matched_keyword']}' | '{title[:60]}'",
            flush=True,
        )
        result: Dict[str, Any] = l1_result
    else:
        # ── L2: Claude 3.5 Haiku (≤3s) ──
        l1_elapsed = (time.perf_counter() - t0) * 1000
        print(
            f"[FILTER] {_ts()} L1 UNCERTAIN ({l1_elapsed:.1f}ms) → "
            f"L2 ({_FILTER_MODEL}) | '{title[:60]}'",
            flush=True,
        )

        t_l2 = time.perf_counter()
        score = (_l2_batcher.classify(title, content)
                 if _l2_batcher is not None else _l2_classify_haiku(title, content))
        l2_elapsed = (time.perf_counter() - t_l2) * 1000
        total_elapsed = (time.perf_counter() - t0) * 1000

        if score == 1:
            result = {
                "is_noise": 0,
                "relevance_score": 0.50,
                "status": "PENDING",
                "layer": "L2",
                "verdict": "L2_SIGNAL",
                "matched_keyword": None,
                "elapsed_ms": round(total_elapsed, 2),
            }
        elif score == 0:
            result = {
                "is_noise": 1,
                "relevance_score": 0.0,
                "status": "FILTERED",
                "layer": "L2",
                "verdict": "L2_NOISE",
                "matched_keyword": None,
                "elapsed_ms": round(total_elapsed, 2),
            }
        else:
            # 无密钥 / 超时 / 解析失败：放行给 AI，避免把可分析新闻当垃圾丢掉
            result = {
                "is_noise": 0,
                "relevance_score": 0.35,
                "status": "PENDING",
                "layer": "L2",
                "verdict": "L2_PASSTHROUGH",
                "matched_keyword": None,
                "elapsed_ms": round(total_elapsed, 2),
            }
        print(
            f"[FILTER] {_ts()} L2 {result['verdict']:9s} | "
            f"Haiku {l2_elapsed:.0f}ms | "
            f"total {total_elapsed:.0f}ms | "
            f"'{title[:60]}'",
            flush=True,
        )

    # ── L3 门卫（四步挖核心影响）：只对非噪音新闻执行 ──
    if result.get("is_noise") == 0:
        # v411b：门卫调用面收紧（默认关，GATE_CALL_SCOPE=pool 才生效）。
        # 背景：门卫与主分析共用配额，配额打满时门卫整体 429（近 30 分钟 37% 的新闻被冻结）。
        # 收紧后只对"至少命中一个池内资产/宏观驱动词"的新闻调门卫，其余直接按
        # gate_unavailable=2（未判定但放行）走主分析 —— 语义上仍是"门卫没给判定"，
        # 不伪装成"门卫判过"。代价：只有宏观词、没有品种名的稿子拿不到门卫过滤。
        if _gate_scope_should_skip(title + " " + content[:300]):
            result["gate"] = {"verdict": "未判定", "reason": "gate-scope-skip"}
            result["gate_verdict"] = "未判定"
            result["gate_unavailable"] = 2
            result["gate_miss"] = True
            print(
                f"[GATE] {_ts()} SKIP  | 无池内品种锚点，按 GATE_CALL_SCOPE=pool 省配额放行 "
                f"| '{title[:60]}'",
                flush=True,
            )
        else:
            result = _attach_gate(result, title, content)
    else:
        result["gate"] = None
        result["gate_unavailable"] = 0

    # ── v176 news_kind L2 分类（依据 title+content 关键词，给 raw_news.kind 写值） ──
    # 分类粒度：gold/oil/crypto/stock/macro/policy/geopolitical/company/sector/opinion
    # 目的：dashboard 按 kind 过滤 + 引擎按 kind 选 model roster / 模板。
    try:
        result["kind"] = _classify_news_kind(title, content)
    except Exception:
        result["kind"] = "other"

    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    result.setdefault("gate_unavailable", 0)
    # v453 P0-3：门卫主品种归一 + 股票桶降级（只对 verdict='可交易' 生效）
    result = _finalize_gate_assets(result, title, content)
    return result


def _classify_news_kind(title: str, content: str) -> str:
    """v176 news_kind L2 二级分类：基于正则+主体关键词的 11 类分类器。

    设计：纯正则，不调 LLM；性能 ~50µs。优先级从高到低：
      geopolitical > hynix > gold > oil > crypto > policy > macro > stock > company > sector > opinion > other

    v213 (海力士坑修复):
      · 新增 hynix 子分类（SKHYNIX/海力士/000660 单独识别）。
      · 放在 stock 之前 — 避免 SKHYNIX 新闻被 NVDA/TSLA/美光等「股票」大类吞掉，
        dashboard 可按 kind=hynix 单独过滤、统计、推流。
    """
    text = (title or "") + " " + (content or "")[:300]
    t = text.lower()

    # 地缘：战争 / 制裁 / 冲突 / 军事
    if re.search(r"(战争|冲突|制裁|军事|导弹|空袭|核武|海峡|霍尔木兹|美军|乌克|以色|中东|台海|朝鲜|伊朗|俄乌|战争升级|停火)", t):
        return "geopolitical"

    # v213: 海力士主体（必须在 stock 之前，否则被「财报/股价」通用分支吞掉）。
    # 关键词：海力士、SK 海力士、SK Hynix、SK Hynix、000660 (KOSPI ticker)、KRX:000660。
    # 注意：单独 HBM/DRAM 不算 — 三星/美光/南亚都会触发，这里只锁「海力士」主体。
    if re.search(r"(海力士|sk[\s\u00A0]?海力士|sk[\s\u00A0]?hynix|000660\.?ks|kospi[\s\u00A0]?000660)", t):
        return "hynix"

    # 黄金：XAU / 金价 / 黄金 ETF
    if re.search(r"(金价|黄金|xau|gold|央行购金|黄金etf|央行储备|避险|现货金)", t):
        return "gold"

    # 原油：WTI / 布伦特 / OPEC / 原油库存
    if re.search(r"(原油|wti|布伦特|opec|欧佩克|油价|原油库存|能源|炼油)", t):
        return "oil"

    # 加密：BTC / ETH / SOL / 主流币
    if re.search(r"(比特币|btc|eth|以太|sol|以太坊|crypto|加密|区块链|defi|链上|交易所|稳定币|usdc|usdt|币安|coinbase|币圈|web3|nft)", t):
        return "crypto"

    # 政策：央行利率 / 美联储 / 决议
    if re.search(r"(美联储|fomc|央行|利率决议|降息|加息|利率|rba|rboe|ecb|日银|人民银行|加息|缩表|qe|量化宽松|鲍威尔|耶伦|议息会议|票委)", t):
        return "policy"

    # 宏观：CPI / GDP / NFP / 失业率 / 通胀
    if re.search(r"(cpi|ppi|gdp|非农|nfp|失业率|通胀|通胀率|零售销售|消费者信心|ism|pce|进口价格|出口|外贸|财政赤字|通胀预期)", t):
        return "macro"

    # 股票：财报 / 股价 / 业绩 / 美股 / A股 / 个股
    if re.search(r"(财报|营收|净利润|每股收益|股价|市值|股票|个股|纳斯达克|s&p|道指|标普|tsla|aapl|nvda|amd|英伟达|苹果|特斯拉|a股|上证|深证|创业板|科创板|沪深300|韩股|s海力士|sk hynix|hbm|dram|nand)", t):
        return "stock"

    # 公司：收购 / 融资 / IPO / 财报
    if re.search(r"(收购|并购|ipo|融资|上市|退市|重组|破产|清算|违约|债务|评级|增发|分红|股息|股权|股东)", t):
        return "company"

    # 行业：行业趋势 / 赛道 / 板块
    if re.search(r"(行业|赛道|板块|产业链|供应链|需求|产能|销量|装机量|出货量|订单|库存|技术|研发|新品|发布会|行业会议|峰)", t):
        return "sector"

    # 观点：分析师 / 评论 / 预测
    if re.search(r"(分析师|投行|预测|预期|预计|观点|评论|认为|暗示|据悉|消息称|分析称)", t):
        return "opinion"

    return "other"


# ═══════════════════════════════════════════════════════════════════════════
# CLI — quick test
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python realtime_filter.py <news headline>")
        print()
        print("Examples:")
        print('  python realtime_filter.py "美联储暗示9月降息"')
        print('  python realtime_filter.py "BTC空投活动来袭"')
        print('  python realtime_filter.py "ETH质押收益率上升"')
        sys.exit(1)

    headline = " ".join(sys.argv[1:])
    result = evaluate_news(headline)
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))
