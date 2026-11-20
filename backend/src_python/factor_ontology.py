#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_ontology.py — v423 规范因子本体（把"长句驱动"折到可复用的因子上）

问题（实测）：1500 个活跃节点里 1298 个 label ≥16 字、964 个（64%）只有 1 条边；
同一个「实际利率/U10Y」在不同品种、不同轮次写成不同长句，全库能跨 ≥3 个品种
复用的 label 只有 2 条 —— 图越跑越大、不越跑越聪明。

设计：
  · 一张 `factor_ontology` 表（key / canonical / family / slow / aliases）
  · 一个**纯规则**映射 `map_label()`：label 里命中别名表最长的那条，归到规范因子
  · 归不到的保持 NULL（不硬猜；覆盖度单独上报，方便后面补别名）
  · `slow=1` 的因子（利率/通胀/增长/美元流动性/储备…）在 `_reconcile` 里获得更长
    的成长期保护与更慢的衰减 —— 低频但真实的宏观驱动不该因为"本轮没再出现"被杀。

别名只放**机制词**，不放品种词（"黄金""BTC"不进别名表），避免把资产当因子。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# key, canonical(<=14字，给图谱当节点名), family, slow, aliases
FACTORS: List[Dict[str, Any]] = [
    # ── 利率 / 政策 / 流动性（慢变量）────────────────────────────────────
    {"key": "real_rate", "canonical": "实际利率", "family": "rates", "slow": 1,
     "aliases": ["实际利率", "实际收益率", "通胀调整后", "tips收益率", "真实利率"]},
    {"key": "nominal_yield", "canonical": "名义利率与美债收益率", "family": "rates", "slow": 1,
     "aliases": ["美债收益率", "10年期美债", "30年期美债", "10y", "30y", "名义利率",
                 "国债收益率", "收益率曲线", "美债拍卖", "长端利率"]},
    {"key": "fed_policy", "canonical": "美联储政策与降息路径", "family": "rates", "slow": 1,
     "aliases": ["美联储", "降息", "加息", "fomc", "鲍威尔", "点阵图", "政策利率",
                 "货币政策", "缩表", "qe", "qt"]},
    {"key": "fiscal", "canonical": "财政与国债供给", "family": "macro", "slow": 1,
     "aliases": ["财政", "赤字", "债务上限", "发债", "国债供给", "预算", "财政刺激"]},
    {"key": "dollar", "canonical": "美元与美元流动性", "family": "dollar", "slow": 1,
     "aliases": ["美元指数", "美元走强", "美元走弱", "美元流动性", "dxy", "美元"]},
    {"key": "liquidity", "canonical": "全球流动性与风险偏好", "family": "liquidity", "slow": 1,
     "aliases": ["流动性", "风险偏好", "vix", "金融条件", "信贷条件", "回购", "准备金"]},
    {"key": "credit", "canonical": "信用利差与融资条件", "family": "liquidity", "slow": 1,
     "aliases": ["信用利差", "信用风险", "融资条件", "高收益债", "违约", "信贷紧缩"]},
    {"key": "banking", "canonical": "银行与金融稳定", "family": "liquidity", "slow": 1,
     "aliases": ["银行", "存款", "挤兑", "流动性危机", "svb", "地区银行"]},
    # ── 通胀 / 增长 / 就业（慢变量）──────────────────────────────────────
    {"key": "inflation", "canonical": "通胀预期", "family": "inflation", "slow": 1,
     "aliases": ["通胀", "cpi", "ppi", "物价", "通缩", "通胀预期"]},
    {"key": "growth", "canonical": "增长与衰退预期", "family": "growth", "slow": 1,
     "aliases": ["增长", "衰退", "gdp", "pmi", "制造业", "经济数据", "软着陆",
                 "硬着陆", "经济周期"]},
    {"key": "jobs", "canonical": "就业与工资", "family": "growth", "slow": 1,
     "aliases": ["就业", "非农", "失业率", "工资", "薪资", "职位空缺"]},
    {"key": "demand_cycle", "canonical": "工业需求与库存周期", "family": "growth", "slow": 1,
     "aliases": ["工业需求", "基建", "地产", "去库存", "补库存", "库存周期", "下游需求"]},
    # ── 商品供给 / 储备（慢变量）────────────────────────────────────────
    {"key": "energy_supply", "canonical": "能源供给与库存", "family": "commodity", "slow": 1,
     "aliases": ["原油供给", "能源供给", "opec", "减产", "增产", "钻井", "炼厂",
                 "原油库存", "供给冲击", "霍尔木兹"]},
    {"key": "metals_supply", "canonical": "金属供给与库存", "family": "commodity", "slow": 1,
     "aliases": ["铜矿", "矿山", "冶炼", "智利", "秘鲁", "金属库存", "铜库存"]},
    {"key": "reserve", "canonical": "央行储备与购金", "family": "reserve", "slow": 1,
     "aliases": ["央行", "储备", "购金", "黄金储备", "增持", "去美元化"]},
    # ── 权益（慢变量为主）────────────────────────────────────────────────
    {"key": "earnings", "canonical": "盈利预期", "family": "equity", "slow": 1,
     "aliases": ["盈利", "财报", "eps", "指引", "营收", "毛利", "业绩"]},
    {"key": "valuation", "canonical": "估值与风险溢价", "family": "equity", "slow": 1,
     "aliases": ["估值", "市盈率", "风险溢价", "折现率", "股权风险", "估值扩张"]},
    {"key": "sector_flow", "canonical": "板块轮动与资金流", "family": "equity", "slow": 0,
     "aliases": ["板块", "轮动", "行业资金", "etf申购", "被动资金"]},
    {"key": "fx_carry", "canonical": "汇率利差与套息", "family": "fx", "slow": 1,
     "aliases": ["利差", "套息", "汇率", "干预", "日元", "欧元", "人民币"]},
    # ── 风险 / 政策（快变量）──────────────────────────────────────────────
    {"key": "geopolitics", "canonical": "地缘政治风险", "family": "risk", "slow": 0,
     "aliases": ["地缘", "战争", "冲突", "袭击", "制裁", "中东", "俄乌", "胡塞",
                 "停火", "红海", "导弹", "无人机"]},
    {"key": "risk_premium", "canonical": "避险与风险溢价", "family": "risk", "slow": 1,
     "aliases": ["避险需求", "避险买盘", "避险溢价", "地缘风险溢价", "风险溢价重定价",
                 "尾部风险", "避险资产"]},
    {"key": "political", "canonical": "政治与选举不确定性", "family": "risk", "slow": 0,
     "aliases": ["选举", "大选", "政治", "政策不确定性", "特朗普", "拜登", "政府关门"]},
    {"key": "trade_policy", "canonical": "贸易与关税", "family": "macro", "slow": 0,
     "aliases": ["关税", "贸易", "出口管制", "禁运", "供应链", "贸易谈判"]},
    # ── 加密（快变量）────────────────────────────────────────────────────
    {"key": "regulation", "canonical": "监管与合规", "family": "crypto_reg", "slow": 0,
     "aliases": ["监管", "sec", "合规", "法案", "牌照", "执法", "批准", "etp",
                 "加密立法", "genius"]},
    {"key": "etf_flow", "canonical": "ETF与机构资金流", "family": "flows", "slow": 0,
     "aliases": ["etf", "机构资金", "资金流", "净流入", "净流出", "grayscale",
                 "贝莱德", "申购赎回"]},
    {"key": "crypto_supply", "canonical": "加密供给与减半", "family": "crypto_supply", "slow": 1,
     "aliases": ["减半", "矿工", "发行量", "销毁", "代币解锁", "质押", "新增供给"]},
    {"key": "crypto_structure", "canonical": "加密市场结构与杠杆", "family": "crypto_struct", "slow": 0,
     "aliases": ["资金费率", "杠杆", "清算", "未平仓", "爆仓", "基差", "做市",
                 "期货持仓", "合约持仓"]},
    {"key": "onchain", "canonical": "链上活动与费用", "family": "crypto_onchain", "slow": 0,
     "aliases": ["链上", "gas费", "活跃地址", "转账量", "tvl", "网络费用"]},
    {"key": "stablecoin", "canonical": "稳定币与法币通道", "family": "crypto_struct", "slow": 0,
     "aliases": ["稳定币", "usdt", "usdc", "法币通道", "出入金", "溢价"]},
    {"key": "positioning", "canonical": "持仓拥挤与情绪", "family": "flows", "slow": 0,
     "aliases": ["持仓拥挤", "多空比", "cftc", "贪婪", "恐惧", "资金拥挤", "投机净多头"]},
    # ── 技术（快变量）────────────────────────────────────────────────────
    {"key": "tech_structure", "canonical": "技术结构与趋势", "family": "tech", "slow": 0,
     "aliases": ["技术结构", "趋势", "均线", "支撑", "阻力", "突破", "形态", "动量",
                 "rsi", "macd", "价格结构", "区间震荡"]},
    {"key": "volatility", "canonical": "波动率结构", "family": "tech", "slow": 0,
     "aliases": ["隐含波动率", "已实现波动率", "波动率", "iv", "skew", "期权"]},
    {"key": "volume", "canonical": "量价与流动性深度", "family": "flows", "slow": 0,
     "aliases": ["量能", "成交量", "价格量能", "流动性深度", "盘口", "成交额", "换手"]},
    {"key": "model_risk", "canonical": "模型与历史过拟合", "family": "tech", "slow": 0,
     "aliases": ["过度拟合", "历史规律", "样本外", "规律失效", "透支基本面", "历史相似窗口"]},
    {"key": "fundamentals", "canonical": "基本面强度", "family": "growth", "slow": 1,
     "aliases": ["基本面", "盈利质量", "现金流", "毛利率", "自由现金流"]},
]

_BY_KEY: Dict[str, Dict[str, Any]] = {f["key"]: f for f in FACTORS}

_SLOW_FAMILIES = {"rates", "inflation", "growth", "liquidity", "dollar",
                  "macro", "commodity", "reserve", "equity", "fx"}


def all_factors() -> List[Dict[str, Any]]:
    return FACTORS


def info(key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    return _BY_KEY.get(str(key))


def is_slow(key: Optional[str]) -> bool:
    f = info(key)
    if not f:
        return False
    return bool(f.get("slow"))


def canonical(key: Optional[str]) -> str:
    f = info(key)
    return str(f["canonical"]) if f else ""


def family(key: Optional[str]) -> str:
    f = info(key)
    return str(f["family"]) if f else ""


# 预编译别名 → (alias_len, key, alias)。最长匹配优先，避免"美元"抢占"美元流动性"。
_ALIAS_INDEX: List[Tuple[str, str, int]] = sorted(
    ((a.lower(), f["key"], len(a)) for f in FACTORS for a in f["aliases"]),
    key=lambda x: -x[2],
)


def map_label(label: str) -> Optional[str]:
    """把一条 label 归到规范因子；归不到返回 None（不硬猜）。"""
    s = str(label or "").strip().lower()
    if len(s) < 2:
        return None
    for alias, key, _n in _ALIAS_INDEX:
        if alias in s:
            return key
    return None


def matches(label: str) -> List[str]:
    """命中多因子时按别名长度排序返回（用于审计"一条 label 被多因子抢"的情况）。"""
    s = str(label or "").strip().lower()
    out: List[Tuple[int, str]] = []
    for alias, key, n in _ALIAS_INDEX:
        if alias in s:
            out.append((n, key))
    return [k for _n, k in sorted(out, reverse=True)]


DDL_FACTOR_ONTOLOGY = """
CREATE TABLE IF NOT EXISTS factor_ontology (
    key         TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL,
    family      TEXT,
    slow        INTEGER NOT NULL DEFAULT 0,
    aliases     TEXT,
    created_at  TEXT,
    updated_at  TEXT
)
"""

DDL_INJECTION_LOG = """
CREATE TABLE IF NOT EXISTS causal_injection_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    asset       TEXT NOT NULL,
    mode        TEXT,
    block_len   INTEGER,
    factors     TEXT,
    decision_id INTEGER
)
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(DDL_FACTOR_ONTOLOGY)
    conn.execute(DDL_INJECTION_LOG)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_driver_nodes)")}
    if cols and "factor_key" not in cols:
        conn.execute("ALTER TABLE asset_driver_nodes ADD COLUMN factor_key TEXT")
    if cols:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_adn_factor_key "
                     "ON asset_driver_nodes(factor_key)")


def seed_ontology(conn: sqlite3.Connection, now: Optional[str] = None) -> int:
    """把因子表 upsert 进库（幂等）。返回写入行数。"""
    import json
    import time
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for f in FACTORS:
        conn.execute(
            "INSERT INTO factor_ontology(key, canonical, family, slow, aliases, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET canonical=excluded.canonical, family=excluded.family, "
            "  slow=excluded.slow, aliases=excluded.aliases, updated_at=excluded.updated_at",
            (f["key"], f["canonical"], f["family"], int(f["slow"]),
             json.dumps(f["aliases"], ensure_ascii=False), now, now),
        )
        n += 1
    return n


_CANONICALS = {f["canonical"]: f["key"] for f in FACTORS}


def extract_factors(block: str) -> List[str]:
    """从 inject_block 文本里抽用到的因子 key（按 "归因:xxx" 与规范名出现）。

    只做统计口径，不反向影响注入内容；抽不到就返回空列表。
    """
    if not block:
        return []
    out: List[str] = []
    for canon, key in _CANONICALS.items():
        if canon and canon in block and key not in out:
            out.append(key)
    return out


def log_injection(asset: str, block: str, mode: str = "text",
                  decision_id: Optional[int] = None) -> None:
    """v423 注入埋点：记下"这次决策实际注入了哪些因子"。

    best-effort：独立短连接、异常全吞，绝不因为埋点失败影响决策。
    只做观测（决策侧不读这张表），因此不会反向改变 AI 行为。
    """
    try:
        import time
        import db as _db
        conn = sqlite3.connect(_db.get_db_path(), timeout=5)
        try:
            conn.execute(DDL_INJECTION_LOG)
            conn.execute(
                "INSERT INTO causal_injection_log(ts, asset, mode, block_len, factors, decision_id) "
                "VALUES (?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), str(asset or "").upper(), mode,
                 len(block or ""), ",".join(extract_factors(block)), decision_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # best-effort 埋点：失败不影响决策，但留一行 debug 痕迹，避免静默死亡无从排查。
        try:
            print(f"[factor_ontology] log_injection skipped: {asset}", flush=True)
        except Exception:
            pass
        return


def ontology_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT key, canonical, family, slow, aliases FROM factor_ontology"
    ).fetchall()]


_CN_RE = re.compile(r"[\u4e00-\u9fa5]")


def canonical_label_safe(key: str, max_len: int = 14) -> bool:
    """规范因子名必须短、含中文、不像事件标题 —— 它要能当图谱节点名。"""
    c = canonical(key)
    return bool(c) and len(c) <= max_len and bool(_CN_RE.search(c))
