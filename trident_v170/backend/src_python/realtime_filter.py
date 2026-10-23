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

import json
import os
import re
import sqlite3
import ssl
import sys
import time
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
    "AMA", "Ask Me Anything", "直播预告", "直播", "webinar",
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
    "复盘", "总结", "回顾", "相关性矩阵", "相关性分析",
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

    # JUNK check first (conservative: "BTC 空投活动" is still noise)
    for kw in _ACTIVE_KEYWORDS["junk"]:
        if kw in clean:
            return {
                "is_noise": 1,
                "relevance_score": 0.0,
                "status": "FILTERED",
                "layer": "L1",
                "verdict": "JUNK",
                "matched_keyword": kw,
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


# ── Pool fallback (L2 / L3 共享) ────────────────────────────────────────
# 2026-09-17：OpenRouter key 失效后，L2 / L3 也接进主分析模型池；
# 池子全失败才回退 OpenRouter(若用户新配置了 key)。
# 直接 import config，避免和 ai_worker.py 漂移；model pool 是 AI_MODEL_ROSTER 展开的实例。
try:
    import config as _cfg  # noqa: E402
    _MINIMAX_KEY = getattr(_cfg, "MINIMAX_API_KEY", "")
    _MINIMAX2_KEY = getattr(_cfg, "MINIMAX2_API_KEY", "")
    _MINIMAX_BASE = getattr(_cfg, "MINIMAX_BASE_URL", "")
    _MINIMAX_MODEL = getattr(_cfg, "MINIMAX_MODEL", "MiniMax-Text-01")
    _XINSHU_KEY = getattr(_cfg, "AIPING_API_KEY", "")
    _XINSHU_BASE = getattr(_cfg, "AIPING_BASE_URL", "")
    _XINSHU_MODEL = os.getenv("REALTIME_FILTER_XINSHU_MODEL", "gpt-5.5")
except Exception:
    _cfg = None
    _MINIMAX_KEY = _MINIMAX2_KEY = _XINSHU_KEY = ""
    _MINIMAX_BASE = _XINSHU_BASE = ""
    _MINIMAX_MODEL = "MiniMax-Text-01"
    _XINSHU_MODEL = "gpt-5.5"


def _post_openai_compat(
    api_base: str, api_key: str, model: str,
    payload: Dict[str, Any], timeout: float,
) -> Optional[str]:
    """发 OpenAI 兼容 chat/completions 请求，返回 message.content 字符串；失败 None。
    用于 L2 / L3 走 MiniMax / MiniMax#2 / Xinshu。
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
    # 间歇 SSL 握手失败/连接瞬断：秒级重试 3 次，全失败才返回 None（上层 L2/L3 自行降级）
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        if attempt:
            time.sleep(1.0 * attempt)
        try:
            resp = opener.open(req, timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            print(f"[FILTER] {_ts()} pool-http {model} {e.code} | {e}", flush=True)
            return None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            print(f"[FILTER] {_ts()} pool-retry {model} #{attempt + 1} | {type(e).__name__}", flush=True)
            continue
    else:
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
    """
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
        t0 = time.perf_counter()
        out = _post_openai_compat(m["api_base"], m["api_key"], payload["model"],
                                  payload, float(m.get("timeout") or timeout))
        dt = (time.perf_counter() - t0) * 1000
        return (m.get("label") or m["id"], payload["model"], dt, out, m.get("key_alias", "?"), m.get("is_local", False))

    # 并行调用所有 provider；任一成功立刻退出（不等其他 in-flight 线程——避免 Xinshu 阻塞拖垮整个 ingest）
    ex = ThreadPoolExecutor(max_workers=len(pool_models))
    # fast_exit=True 让 finally 不再 wait=True，避免 Xinshu 30s timeout 拖垮 ingest
    fast_exit = {"value": False}
    try:
        futures = [ex.submit(_call_one, m) for m in pool_models]
        for fut in as_completed(futures):
            try:
                tag, mdl, dt, out, key_alias, is_local = fut.result()
            except Exception:
                continue
            # v187：成功/失败都写 token 埋点(零开销,失败时 tokens=0)
            try:
                import db as _db_filter
                _db_filter.log_ai_call(
                    purpose=purpose, key_alias=key_alias, model_id=str(tag or "?")[:80],
                    tokens_in=int((max_tokens + len(system) + len(user)) // 4) if out else 0,  # 粗估
                    tokens_out=len(out) // 4 if out else 0,
                    latency_ms=int(dt), status="ok" if out else "http_error",
                    call_site=call_site,
                )
            except Exception:
                pass
            if out:
                print(f"[{label}] {_ts()} pool-ok {tag}/{mdl} | {dt:.0f}ms (parallel)", flush=True)
                fast_exit["value"] = True
                ex.shutdown(wait=False, cancel_futures=False)
                return out
            print(f"[{label}] {_ts()} pool-fail {tag}/{mdl} | {dt:.0f}ms (parallel)", flush=True)
    finally:
        # 仅在"全失败"或异常时 wait=True；fast_exit=True（任一 provider 成功）则不等
        if not fast_exit["value"]:
            ex.shutdown(wait=True)
        else:
            ex.shutdown(wait=False)
    return None

# ── Ultra-minimal prompt (~40 tokens → fast + cheap) ──
_L2_SYSTEM = (
    "You are a financial news filter. Output ONLY a single digit: "
    "1 if the headline is actionable for a macro/crypto trader, "
    "0 if it is noise.\n"
    "Do NOT output any other text, punctuation, or explanation."
)


def _l2_classify_haiku(title: str, content: str = "") -> Optional[int]:
    """Call L2 binary classifier. Returns 1 / 0, or None when unavailable.

    None 表示无密钥、超时或解析失败，由 evaluate_news 放行给 AI。
    """
    if not _OPENROUTER_KEY:
        return None

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
        "max_tokens": 4,          # "1" or "0" is 1 token; 4 is safe margin
        "temperature": 0.0,       # Deterministic
    }
    # NOTE: NOT using response_format={"type":"json_object"} — Anthropic models
    # behave more reliably with plain-text prompts when only a single digit is needed.

    # 2026-09-17：先试池子（MiniMax/MiniMax#2/Xinshu），失败再回退 OpenRouter。
    # 池子在国内直连 1-5s，但 L2 默认 timeout=3s 容易超时——池子用 2.5s 让步。
    raw = _call_pool_sync(
        _L2_SYSTEM, text,
        max_tokens=4, temperature=0.0,
        timeout=min(2.5, _FILTER_TIMEOUT_S), label="FILTER-L2",
    )
    if raw:
        raw = raw.strip()
        m = re.search(r"\b([01])\b", raw)
        if m:
            return int(m.group(1))
        # 池子回了非 0/1：当作噪声，让 evaluate_news 走兜底
        return None

    # ── 兜底：OpenRouter（若用户日后提供有效 key）──
    if not _OPENROUTER_KEY:
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
        resp = opener.open(req, timeout=_FILTER_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"[FILTER] {_ts()} L2 HTTP {e.code} | {err_body}", flush=True)
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
_GATE_TIMEOUT_S = float(os.getenv("REALTIME_GATE_TIMEOUT_S", "10.0"))
# v176.9.1 (2026-09-22)：MiniMax-M3 是 reasoning 模型，thinking 块 2–5k tokens，
# 单纯 400 max_tokens 会被截断在 <think>... 里 → JSON 解析失败 → 误判 gate miss。
# 提到 1500：装得下完整 thinking + JSON 终结部分（实测 MiniMax-M3 ~3.5s 完成）。
_GATE_MAX_TOKENS = int(os.getenv("REALTIME_GATE_MAX_TOKENS", "1500"))

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
) -> str:
    """程序侧硬裁决（不靠模型自觉）—— 纯函数，便于单测。

    v181 重构（二分类：可交易 / 排除，砍掉「只能看」）：
      1. 没重量 / 没影响标的 / 写不出核心影响 → 排除
      2. verdict 非法（不再接受"只能看"） → 排除
      3. 分析类（带观点/预测/评论）= 观点噪音 → 直接排除
         总结类 + 已定价（纯事后盘点） → 排除
      4. LLM 自评 impact_1to100 < NEWS_GATE_IMPACT_MIN → 排除（兜底淘汰）
      5. 总结类 + 即时 + 力度足 → 保持可交易
    """
    if not is_material or not assets or not core:
        return "排除"
    if verdict not in ("可交易", "排除"):
        return "排除"
    # 分析类（观点稿） → 一律排除
    if kind == "分析类":
        return "排除"
    # 总结类 + 已定价 → 排除
    if kind == "总结类" and timing == "已定价":
        return "排除"
    # 力度门槛：strength 是 0-10, impact 是 1-100，低于各自阈值都排除
    impact_threshold = getattr(_cfg, "NEWS_GATE_IMPACT_MIN", 20)
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
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as e:
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
    if raw_str is None and _OPENROUTER_KEY:
        raw_str = _post_openrouter(payload, _GATE_TIMEOUT_S)
    if raw_str is None:
        return None

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


def _attach_gate(result: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
    """把门卫标签挂到 filter 结果上；门卫判"排除" → 置 is_noise=1（引擎不认领分析）。

    v176.9 (2026-09-22)：门卫 LLM 调用失败时（gate is None）改为置 is_noise=1，
    让 ingest 阶段直接丢弃该条新闻（不入 raw_news.PENDING），不再让 ai_worker 主分析
    跑出"AI 观望"的假信号卡片。前端会显示"门卫无信号/暂不可用"而不是伪造的"六段推演"。
    旧行为：降级 verdict="只能看" → 主分析仍跑 → 看起来像 AI 真分析 → 实际是被瞎门卫强制 HOLD。
    """
    gate = _l3_semantic_gate(title, content)
    result["gate"] = gate
    if gate is None:
        # 门卫 LLM 不可用（网络/API/熔断）—— 停止信号输出，避免伪造"AI 观望"假结论
        result["gate_miss"] = True
        result["gate_verdict"] = "无信号"
        result["gate"] = {"verdict": "无信号", "reason": "gate-llm-unavailable"}
        result["is_noise"] = 1
        result["relevance_score"] = 0.0
        result["status"] = "FILTERED"
        print(
            f"[GATE] {_ts()} MISS  | gate LLM 不可用 → 置 is_noise=1 (FILTERED) | '{title[:60]}'",
            flush=True,
        )
        return result

    verdict = gate["verdict"]
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


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

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
        score = _l2_classify_haiku(title, content)
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
        result = _attach_gate(result, title, content)
    else:
        result["gate"] = None

    # ── v176 news_kind L2 分类（依据 title+content 关键词，给 raw_news.kind 写值） ──
    # 分类粒度：gold/oil/crypto/stock/macro/policy/geopolitical/company/sector/opinion
    # 目的：dashboard 按 kind 过滤 + 引擎按 kind 选 model roster / 模板。
    try:
        result["kind"] = _classify_news_kind(title, content)
    except Exception:
        result["kind"] = "other"

    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
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
