#!/usr/bin/env python3
"""品种涨跌驱动因果图谱（问题 3）：像神经元一样生长、链接、逻辑推理。

节点 = 因素/资产/宏观变量/结论（神经元）；
边   = 因果/条件/证实/证伪（突触，带权重）。

流程：
1. 从历史 factor_ranking / factor_snapshots / causal_chains 收集候选因素（LLM 素材）。
2. LLM 用「连问为什么」(5-why) 追到最底层 → 产出核心因素 + 条件因素 + 推理链。
3. 并入图谱（同义节点合并 + 建因果边）。
4. 新旧结论互相推理：命中的上调置信度、加版本；未再出现的衰减、低于阈值失效。
5. 供 API 查询展示 + 注入 AI 分析（回灌）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import db
import factor_ontology
import mechanism_family   # v417: 机制族口径唯一来源（评估器也用同一份）
from review import review_agent


def _now() -> str:
    return review_agent._now()


# 衰减保护：version 低于此值的节点本轮 reconcile 不衰减（"成长期"）。
# 设计理由：用户连点几次「重新推理」时，第一轮新生成的节点 version=1，第二轮会被
# _upsert_node 上调为 version=2（因为 _norm_label 命中），但若同义不完全匹配，仍
# 可能在第一轮就被打 version=1 的新节点 -> 第二轮直接被 reconcile 衰减到 active=0，
# 图谱瞬间失忆。这里设 3 表示至少 3 次 reason 仍未被同义匹配的节点才进入衰减队列。
#
# v200+ Q5: 自适应保护期 — 改为函数 adaptive_protect_version(sample_n) (config.py),
# 数据稀疏 → 长保护 (防冷启动误杀); 数据充足 → 短保护 (n // 2, cap=11).
# 旧常量保留作 fallback (单元测试 / 外部 import / 跨进程一致性).
_RECONCILE_PROTECT_VERSION = 3
_RECONCILE_PROTECT_VERSION_FALLBACK = _RECONCILE_PROTECT_VERSION


# === 假设核验闭环元数据（塞进 evidence 字段 JSON 的 _ 前缀键）====================
#
# 设计目标：把"假设是否被事实核验"这件事从"机械 confidence 衰减"升级为"逻辑状态机"。
# 但铁律 1 规定不破 schema，所以元数据只能复用现有 evidence TEXT 字段（JSON 字符串）。
#
# evidence JSON 结构（双角色）：
#   {
#     "chain": [<why_chain 短语列表>],          # 业务数据 — 5-why 推理层
#     "_status": "<status>",                     # 元数据 — 状态（CONFIRMED/UNCONFIRMED/CONTRADICTED）
#     "_fail":   "<failure_reason>|null",        # 元数据 — 失败诊断（仅 CONTRADICTED 时）
#     "_verified_at": "<ISO 时间>",              # 元数据 — 最后核验时间
#     "_fact_ids": [<int>, ...],                 # 元数据 — 引用的事实 id 列表
#     "_position": "<cause|middle|effect>",      # 元数据 — 节点逻辑位置
#   }
#
# 规则：
#   - _ 前缀的键是逻辑层元数据，前端 parseEvidence 时过滤掉；
#   - chain 键是业务数据（why_chain 列表）；
#   - 旧数据兼容：纯 list 字符串自动包装为 dict(_status=UNCONFIRMED)；
#   - 写入统一走 _serialize_evidence，读取统一走 _parse_evidence。
# ==============================================================================

# 核验状态枚举（字符串值，与 _status 字段一一对应）
_STATUS_CONFIRMED = "CONFIRMED"           # 至少 1 个支持事实，无反驳 → 假设成立
_STATUS_UNCONFIRMED = "UNCONFIRMED"       # 无支持无反驳或信息不足 → 不知道
_STATUS_CONTRADICTED = "CONTRADICTED"     # 至少 1 个反驳事实 → 假设被否定

# 失败诊断枚举（_fail 字段，仅 CONTRADICTED 时有值）
_FAIL_WRONG_HYPOTHESIS = "WRONG_HYPOTHESIS"   # 假设本身错（关键词命中但方向反）
_FAIL_WRONG_INFERENCE = "WRONG_INFERENCE"     # 推理链中间层错（有支持也有反驳）
_FAIL_MISSING_CONTEXT = "MISSING_CONTEXT"     # 缺少关键信息（关键词都没命中）

# 位置枚举（_position 字段）
_POSITION_CAUSE = "cause"        # 因端：只有出边无入边（驱动源）
_POSITION_MIDDLE = "middle"      # 中间层：都有出入边
_POSITION_EFFECT = "effect"      # 果端：只有入边无出边

# 置信度档位
_CONF_FLOOR = 0.5                # 中性地板（"不知道"）
_CONF_VERIFIED_STEP = 0.1       # 每个支持事实 +0.1，最多 5 个
_CONF_VERIFIED_CAP = 1.0         # 最高 1.0
_CONF_CONTRADICTED_STEP = 0.1   # 每个反驳事实 -0.1，最多 3 个
_CONF_CONTRADICTED_FLOOR = 0.0  # 最低 0.0

# v424 核验计分制（取代"一票否决"）：
#   旧规则 contradict>=1 即 CONTRADICTED，一个噪声事实就能干掉一条核心，
#   实测 56 个核心驱动里 31 个被判否证、0 个被证实 —— 这不是市场给的答案。
#   新规则看「支持率」，样本不足就不下结论。
_MIN_VERIFY_SAMPLES = 5      # 少于 5 个相关事实 → 不下结论（UNCONFIRMED）
_CONFIRM_RATIO = 0.60        # 支持率 ≥ 0.60 且支持数 ≥ 5 → CONFIRMED
_REFUTE_RATIO = 0.40         # 支持率 ≤ 0.40 → CONTRADICTED
_HARD_REFUTE_N = 12          # 样本 ≥ 12 且支持率 ≤ 0.4，才允许把节点下线（active=0）

# 简单关键词同义映射（核验时判断 fact.indicator 与 chain 是否对齐）
_INDICATOR_ALIASES = {
    "DXY": ["美元", "dxy", "美元指数"],
    "FedRate": ["利率", "美联储", "fed", "实际利率"],
    "BTC_Price": ["btc", "比特币", "比特币价格"],
    "ETH_Price": ["eth", "以太坊", "以太坊价格"],
    "Oil_Price": ["wti", "原油", "油价", "石油"],
    "Gold_Price": ["xau", "黄金", "金价"],
    "Volume": ["成交量", "交易量", "volume"],
    "VIX": ["vix", "恐慌指数", "波动率"],
    "Geopolitics": ["地缘", "战争", "冲突", "制裁"],
    "Macro": ["宏观", "经济数据", "cpi", "gdp", "非农"],
    # v314 P0: 外部行情源(Yahoo ticker)归一后的规范名。
    # 旧实现 external_indicators 的 indicator 直接写 ticker("DX-Y.NYB" / "^GSPC" /
    # "TLT" / "^VIX" / "BTC-USD")，belief thesis 里写的是中文语义词("美元" /
    # "实际利率" / "风险资产" / "BTC" / "波动率") → 文本永远匹配不上 ticker →
    # 这些链恒 0 证据。现在把 ticker 归一到规范名，再配同义词表。
    "SPX": ["spx", "标普", "标普500", "美股", "风险资产", "^gspc"],
    "Rates": ["rates", "利率", "国债", "实际利率", "收益率", "期限溢价",
              "国债收益率", "tlt", "^tnx"],
}


def _llm(prompt: str, system: str, max_tokens: int = 1600) -> Optional[str]:
    return review_agent._llm(prompt, system, max_tokens=max_tokens)


def _parse_json(text: Optional[str]) -> Any:
    return review_agent._parse_json(text)


def _clamp(v: Any, default: float = 0.5) -> float:
    """置信度/权重安全收敛到 0-1，非法值回退 default（默认 0.5，偏中性）。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _norm_expect(v: Any) -> Optional[str]:
    """把 LLM 给的 expect（涨/跌/不定）归一成核验用的 up/down/None。

    v424: 核验需要「该机制成立时本品种应有方向」。让 LLM 直接标注比从中文链文本
    猜方向可靠得多（旧实现靠猜，反向机制全判反）。"不定"→ None = 不参与核验。
    """
    s = str(v or "").strip().lower()
    if s in ("up", "涨", "上行", "上涨", "看涨", "利多", "利好", "+", "1"):
        return "up"
    if s in ("down", "跌", "下行", "下跌", "看跌", "利空", "-", "-1"):
        return "down"
    return None


def _norm_label(s: str) -> str:
    """归一化节点名：去空白、全角转半角、小写，用于同义合并。

    全角→半角映射覆盖：CJK 标点、英文标点、引号、货币、空白类。
    """
    if not s:
        return ""
    s = "".join(str(s).split())
    # 全角→半角（长→短必须一一对应）
    full = "，。！？（）【】《》「」『』“”‘’：；￥·～—…、"
    half = ",.!?()[]<>\"\"''\"'':;$*~--.,"
    trans = str.maketrans(full, half)
    s = s.translate(trans)
    return s.lower()


_CHAIN_PREFIXES = (
    # 中文前缀（全角 + 半角冒号 + 中英文分号/逗号）
    "最底层：", "最底层:", "最底层；", "最底层;", "最底层，", "最底层,",
    "触发因素：", "触发因素:", "触发因素；", "触发因素;", "触发因素，", "触发因素,",
    "现象：", "现象:", "现象；", "现象;", "现象，", "现象,",
    "因为：", "因为:", "因为；", "因为;", "因为，", "因为,",
    "中间层：", "中间层:", "中间层；", "中间层;", "中间层，", "中间层,",
    "结果：", "结果:", "结果；", "结果;", "结果，", "结果,",
    # 裸词（无冒号）
    "最底层", "触发因素", "现象", "因为", "中间层", "结果",
    # 英文前缀（防 LLM 偶尔输出英文标签）
    "root cause:", "root cause:", "root cause，",
    "trigger:", "trigger,", "trigger ",
    "phenomenon:", "phenomenon,",
    "because:", "because,",
    "middle layer:", "middle layer,",
    "result:", "result,",
    "root cause", "trigger", "phenomenon", "because", "middle layer", "result",
)


def _clean_chain_item(s: str) -> str:
    """剥掉 why_chain 每层可能带的『现象/因为/最底层/触发因素』前缀标签。"""
    s = str(s).strip()
    # 大小写不敏感（LLM 偶尔输出 "Root cause" 而非 "root cause"）
    s_lower = s.lower()
    for p in _CHAIN_PREFIXES:
        if s_lower.startswith(p):
            # 把可能残留的全角空白/半角空白/逗号/句号统一清掉
            s = s[len(p):].strip(" ：:；;，,。. \t")
            break
    return s


def _is_placeholder(name: str) -> bool:
    """过滤服务端兜底 / 空占位的因素名，避免污染 5-why 候选因子。"""
    s = (name or "").strip()
    if not s:
        return True
    if s.upper() in ("N/A", "NA", "NONE", "NULL", "UNDEFINED"):
        return True
    if "服务端兜底" in s or "兜底因素" in s:
        return True
    return False


def _parse_factors(raw: Any) -> List[str]:
    """从 factor_ranking / factors 字段提取因素名列表（兼容数组/字典/字符串）。

    支持的 schema：
      - list[str]
      - list[dict]：取 factor / name / key / label 字段
      - dict[str, list[str]]：把每个 value 数组的元素作为因素名
      - dict[str, int|float]：key 本身作为因素名
      - 以上任意的 JSON 字符串
    """
    if not raw:
        return []
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return []
    names: List[str] = []
    if isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                f = it.get("factor") or it.get("name") or it.get("key") or it.get("label")
                if f:
                    names.append(str(f))
            elif isinstance(it, str):
                names.append(it)
            elif isinstance(it, (int, float)):
                names.append(str(it))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        names.append(x)
                    elif isinstance(x, dict):
                        f = x.get("factor") or x.get("name") or x.get("key") or x.get("label")
                        if f:
                            names.append(str(f))
                    elif isinstance(x, (int, float)):
                        names.append(str(x))
            elif isinstance(v, (int, float)):
                names.append(str(k))
            elif isinstance(v, str):
                # 兼容 {"Fed rate": "0.8"} 这种 value 是数字字符串的情况
                names.append(str(k))
    return [n for n in names if not _is_placeholder(n)]


def _norm_asset(asset: str) -> str:
    """品种名归一化：strip + 大写。

    防止 'BTC' / 'btc' / 'Btc' 在 DB 里分裂成多个节点；所有写入
    ``asset_driver_nodes.asset`` / ``asset_driver_edges``（间接）的入口都应走
    这里。历史脏数据去重见 :func:`merge_duplicate_assets`。
    """
    return str(asset or "").strip().upper()


# 已知的合法资产白名单(因果链/异常归因里的 token 必须命中这里才视为资产,
# 否则 "BUY/SELL/LOSS/WIN" 这种操作码/胜负标会被误当作品种)
_VALID_ASSET_WHITELIST: set[str] = {
    # 加密
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "TRX", "DOT", "AVAX",
    "MATIC", "LINK", "LTC", "BCH", "ATOM", "NEAR", "APT", "OP", "ARB",
    # 商品
    "XAU", "XAG", "WTI", "BRENT", "NATGAS", "HG",  # HG=铜
    # 指数
    "SPX", "NDX", "DJI", "RUT", "VIX", "HSI", "N225",
    # 美债
    "US10Y", "US02Y", "US30Y", "DGS10", "DGS2",
    # 外汇
    "DXY", "USDCNY", "EURUSD", "USDJPY", "USDTWD",
    # 股票板块
    "US_STOCK", "CN_STOCK", "HK_STOCK", "JP_STOCK",
    # 主题/板块
    "MEME", "DEFI", "L1", "L2",
}


def known_assets() -> List[str]:
    """历史上出现过的主品种（排除脏值 NONE），并归一化为大写。

    v276+: 扩展到 5 个数据源 — 不只 ai_decisions，还包括 abnormal_attributions /
    cycle_reversal_signals / causal_chains / asset_driver_nodes 自身。目的：
    让 XAU/XAG/WTI/US_STOCK/CN_STOCK 这些「有数据但无交易决策」的品类也能
    进入因果图谱 — 之前因为只在 ai_decisions 查，BTC/ETH/SOL/XAU/WTI 之外
    的品类永远是图谱孤儿。

    token 提取走白名单过滤 — 防止把 BUY/SELL/LOSS/WIN/ETP 等误当作品种。
    """
    conn = db.get_connection()
    try:
        out_set: set[str] = set()
        # 1. ai_decisions (历史有 AI 决策的)
        for (v,) in conn.execute(
            "SELECT DISTINCT target_asset FROM ai_decisions "
            "WHERE target_asset IS NOT NULL AND target_asset != '' AND target_asset != 'NONE'"
        ).fetchall():
            k = _norm_asset(v)
            if k and k != "NONE" and k in _VALID_ASSET_WHITELIST:
                out_set.add(k)
        # 2. abnormal_attributions (跨资产异常归因 — XAU 2326, ETH 1551, SOL 1546, BTC 1531, WTI 946)
        try:
            for (v,) in conn.execute(
                "SELECT DISTINCT asset FROM abnormal_attributions "
                "WHERE asset IS NOT NULL AND asset != '' AND asset != 'NONE'"
            ).fetchall():
                k = _norm_asset(v)
                if k and k != "NONE" and k in _VALID_ASSET_WHITELIST:
                    out_set.add(k)
        except sqlite3.OperationalError:
            pass
        # 3. cycle_reversal_signals (周期反转信号 — BTC/ETH/SOL/WTI/XAU/XAG)
        try:
            for (v,) in conn.execute(
                "SELECT DISTINCT asset FROM cycle_reversal_signals "
                "WHERE asset IS NOT NULL AND asset != ''"
            ).fetchall():
                k = _norm_asset(v)
                if k and k != "NONE" and k in _VALID_ASSET_WHITELIST:
                    out_set.add(k)
        except sqlite3.OperationalError:
            pass
        # 4. causal_chains (因果链 — cause/effect 里有资产名的提取出来,走白名单)
        try:
            for r in conn.execute(
                "SELECT DISTINCT cause, effect FROM causal_chains WHERE enabled=1"
            ).fetchall():
                for fld in (r[0], r[1]):
                    if not fld:
                        continue
                    for token in re.findall(r"\b[A-Z]{2,5}\b", str(fld)):
                        token_u = _norm_asset(token)
                        if token_u in _VALID_ASSET_WHITELIST:
                            out_set.add(token_u)
        except sqlite3.OperationalError:
            pass
        # 5. asset_driver_nodes 里已有的（兜底 — 防止新增数据源漏掉）
        try:
            for (v,) in conn.execute(
                "SELECT DISTINCT asset FROM asset_driver_nodes "
                "WHERE kind='asset' AND asset IS NOT NULL AND asset != ''"
            ).fetchall():
                k = _norm_asset(v)
                if k and k != "NONE" and k in _VALID_ASSET_WHITELIST:
                    out_set.add(k)
        except sqlite3.OperationalError:
            pass
        # 6. 把硬编码传导对里的也加上(保证神经元图至少有这些节点)
        for from_a, to_a, _, _ in _HARDCODED_CROSS_LINKS:
            out_set.add(_norm_asset(from_a))
            out_set.add(_norm_asset(to_a))
        return sorted(out_set)
    finally:
        conn.close()


_MAX_CANDIDATE_LEN = 40   # 超过 40 字的多半是一句论点/事件描述，不是"因子"
_MAX_FACTOR_NAME_LEN = 24  # 因子名上限（核心驱动是短语，不是论述）

# v424: 交易复盘语汇 —— "WTI原油做多胜率显著高于XAU做多"是复盘结论，不是涨跌成因。
# 这类句子以前会被当成"候选因子"喂给 LLM，LLM 只能把它抽象成"新闻驱动短线方向"。
_REVIEW_WORDS = ("胜率", "入场", "出场", "持仓", "盈利", "亏损", "盈亏", "做多", "做空",
                 "抄底", "均价", "止损", "仓位", "手续费", "滑点", "复盘", "窗口",
                 "PAIR", "对冲比例", "收益", "回撤")

# 常见品种代码（用于识别"别的品种串味"）
_ASSET_CODE_RE = re.compile(r"\b(?:XAU|XAG|XAUT|WTI|BRENT|BTC|ETH|SOL|SPX|NDX|DJI|"
                            r"CN_STOCK|CN_BOND|CNY|JPY|EUR|DXY|COPPER|COIN)\b", re.I)


def _asset_vocab(asset: str) -> List[str]:
    """本品种"已用过的规范因子族"→ 规范名，作为受控词表（v424）。

    候选过闸门后难免只剩两三条，直接喂给 LLM 会饿死它。补上该品种图谱里
    真实用过的规范名，既保证 LLM 有词可用，又不会把 21 个品种拉回同一套套话。
    """
    keys: List[str] = []
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT factor_key FROM asset_driver_nodes "
            "WHERE asset=? AND COALESCE(factor_key,'')!=''", (asset,)
        ).fetchall()
        keys = [str(r[0]) for r in rows if r[0]]
    except sqlite3.Error:
        keys = []
    out = [factor_ontology.canonical(k) for k in keys]
    return [c for c in out if c]


def _gate_candidates(items: List[str], asset: str, limit: int) -> Tuple[List[str], Dict[str, int]]:
    """候选因子入口闸门（v424）。

    为什么要有闸门
    --------------
    旧 `collect_candidate_factors` 把三个来源原样丢给 LLM：AI 决策的 factor_ranking、
    因子快照、以及**全库所有因果链的 cause/effect**。第三个来源是全局的 —— 结果
    XAU / WTI / BTC 拿到的候选清单几乎一模一样（实测三个品种 top-40 完全相同），
    所以 21 个品种学出来的核心驱动都是「流动性/实际利率」那几句套话。

    闸门做四件事：
      1) 挡掉交易复盘语（胜率/入场/盈亏/做多…）——那是执行层的账，不是市场成因；
      2) 挡掉跨品种串味（讲 WTI 的句子不该出现在 XAU 的候选里）；
      3) 挡掉列名碎片、系统自身毛病、内部 ID、超长论述；
      4) 只留能归到 `factor_ontology` 规范族的项，同族只留最短一条。

    返回 (保留项, 统计)。
    """
    stats = {"in": len(items), "no_ontology": 0, "junk": 0, "too_long": 0,
             "review": 0, "cross_asset": 0, "dup_family": 0, "kept": 0}
    clean: List[str] = []
    asset_u = str(asset or "").upper()
    for s in items:
        s = str(s or "").strip()
        if not s:
            continue
        if len(s) > _MAX_CANDIDATE_LEN:
            stats["too_long"] += 1
            continue
        if _has_schema_fragment(s) or _INTERNAL_REF_RE.search(s):
            stats["junk"] += 1
            continue
        if any(w in s for w in _SELF_META_WORDS) or any(w in s for w in _REVIEW_WORDS):
            stats["review"] += 1
            continue
        if any(j.lower() in s.lower() for j in _JUNK_EVIDENCE):
            stats["junk"] += 1
            continue
        # 跨品种串味：句子里出现别的品种代码，且不含本品种
        codes = {c.upper().replace("_", "") for c in _ASSET_CODE_RE.findall(s)}
        me = asset_u.replace("_", "")
        if codes and me not in codes:
            stats["cross_asset"] += 1
            continue
        clean.append(s)

    kept: List[str] = []
    seen_family: Dict[str, str] = {}
    for s in clean:
        key = factor_ontology.map_label(s)
        if not key:
            stats["no_ontology"] += 1
            continue
        fam = factor_ontology.family(key)
        old = seen_family.get(fam)
        if old is not None:
            # 同族只留最短的一条（更接近"因子名"而非论述）
            stats["dup_family"] += 1
            if len(s) < len(old):
                kept[kept.index(old)] = s
                seen_family[fam] = s
            continue
        seen_family[fam] = s
        kept.append(s)

    # 受控词表兜底：补本品种已用过的规范名，保证 LLM 有词可用（不会回到全库套话）
    for c in _asset_vocab(asset):
        if len(kept) >= limit:
            break
        if c not in kept and len(c) <= _MAX_CANDIDATE_LEN:
            kept.append(c)
    out = kept[:limit]
    stats["kept"] = len(out)
    return out, stats


def collect_candidate_factors(asset: str, limit: int = 30) -> List[str]:
    """收集该品种历史出现的影响涨跌因素（按出现次数降序）。"""
    asset = _norm_asset(asset)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    counter: Counter = Counter()
    try:
        rows = conn.execute(
            "SELECT factor_ranking FROM ai_decisions "
            "WHERE target_asset=? AND factor_ranking IS NOT NULL AND factor_ranking != '' "
            "ORDER BY id DESC LIMIT 500",
            (asset,),
        ).fetchall()
        for r in rows:
            for f in _parse_factors(r["factor_ranking"]):
                counter[f] += 1
        snaps = conn.execute(
            "SELECT factors FROM factor_snapshots "
            "WHERE asset=? AND factors IS NOT NULL AND factors != '' "
            "ORDER BY id DESC LIMIT 500",
            (asset,),
        ).fetchall()
        for r in snaps:
            for f in _parse_factors(r["factors"]):
                counter[f] += 1
        # 宏观因果知识（causal_chains）—— 只取**本品种**的链。
        # v424: 旧实现把全库所有链的 cause/effect 都塞进来，导致 XAU/WTI/BTC 拿到的
        # 候选清单完全相同（实测 top-40 一模一样），21 个品种学出同一套"流动性/实际利率"。
        chains = conn.execute(
            "SELECT DISTINCT cause, effect FROM causal_chains WHERE enabled=1 "
            "AND COALESCE(origin,'')!='deprecated_meta' "
            "AND (asset=? OR effect LIKE ?)",
            (asset, "%" + asset + "%"),
        ).fetchall()
        for r in chains:
            if r["cause"]:
                counter[r["cause"]] += 1
            if r["effect"]:
                counter[r["effect"]] += 1
    finally:
        conn.close()
    # v424: 过入口闸门（本体归一 / 挡脏数据 / 同族去重）
    gated, _stats = _gate_candidates([f for f, _ in counter.most_common(limit * 2)],
                                     asset, limit)
    return gated


def reason_asset_drivers(asset: str,
                         candidates: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """对单个品种做 5-why 推理，产出核心因素/条件因素/推理链。"""
    if candidates is None:
        candidates = collect_candidate_factors(asset)
    if not candidates:
        return None

    def _cand_line(c: str) -> str:
        key = factor_ontology.map_label(c)
        if key:
            return "- %s（规范因子：%s）" % (c, factor_ontology.canonical(key))
        return "- %s" % c

    factor_lines = "\n".join(_cand_line(c) for c in candidates)
    prompt = (
        f"你是量化宏观因果分析师。针对品种 {asset}，下面是它历史上出现的影响涨跌因素：\n"
        f"{factor_lines}\n\n"
        "用「连问为什么」的方法追溯它涨跌的最底层驱动：每个核心因素从表层现象开始，"
        "不断问『为什么』，一直追到无法再往下拆的根本原因（如美元实际利率、央行流动性、"
        "全球风险偏好、供给刚性等），形成一条 3-5 层的因果链（why_chain，从现象到最底层）。\n"
        "why_chain 的每一层只写该层本身的关键事实短语，不要加『现象/因为/最底层/触发因素』等前缀标签。\n\n"
        "区分两类：\n"
        "1) core_factors（核心因素）= 最底层、无法再拆的根本驱动；\n"
        "2) condition_factors（条件因素）= 本身不是根本驱动，但会触发/放大/抑制核心因素"
        "兑现的中间条件（如地缘事件、库存、季节性、突发事件、某个技术位）。\n\n"
        "只输出 JSON（不要其他文字）：\n"
        '{"core_factors":[{"factor":"…","why_chain":["表层现象短语","中间原因短语","底层原因短语"],'
        '"confidence":0-1,"expect":"涨|跌|不定"}],'
        '"condition_factors":[{"factor":"…","why_chain":["触发条件短语","放大/抑制机制短语"],'
        '"confidence":0-1,"expect":"涨|跌|不定"}],'
        '"summary":"一句话总结该品种涨跌本质"}\n\n'
        f"每个因素必须带 expect：当这个因素**走强/恶化**时，{asset} 通常是涨还是跌。"
        "只填「涨」「跌」「不定」三者之一；如果它只是中间变量、方向取决于别的条件，填「不定」。"
        "expect 描述的是这个因素对**本品种**的方向影响，不是因素自身的涨跌。\n\n"
        "命名硬约束（不遵守会被入库校验拦掉）：\n"
        "· factor 必须是 ≤14 字的**短语**，用下面标了「规范因子」的名字优先；\n"
        "· 一条只讲一个因子，禁止用「与/和/、」把两个驱动拼成一条（那是论点不是因子）；\n"
        "· 不要写「新闻驱动方向」「二轮入场溢价」这类同义反复或系统自己的入场动作。"
    )
    result = _parse_json(_llm(prompt, "你是量化宏观因果分析师，只输出 JSON。", max_tokens=1600))
    if not isinstance(result, dict):
        return None
    return result


_SCHEMA_TOKENS_CACHE = None
# 事件标题特征词：出现即说明这是"某次事件"而不是"结构性驱动"
_EVENT_VERBS = ("批准", "发布", "宣布", "下调", "上调", "谈判", "袭击", "会谈",
                "决议", "讲话", "报告", "签署", "制裁", "停火", "投票")


def _schema_tokens(conn: sqlite3.Connection) -> set:
    """本库自身列名集合（用于拦住"把列名当驱动"的入库）。

    v407 实测: BTC 的唯一 core 驱动是 `historical_confidence`，ETH 的是
    `cluster_heat` —— 都是表的列名。旧实现没有任何拦截，LLM 一复读字段名就入库，
    注入 prompt 时等于告诉模型"这个列名是金价的底层原因"。
    """
    global _SCHEMA_TOKENS_CACHE
    if _SCHEMA_TOKENS_CACHE is None:
        names = set()
        for t in ("asset_driver_nodes", "causal_chains", "causal_chain_outcomes",
                  "trade_decisions", "factor_ranking", "decision_errors"):
            try:
                names |= {str(r[1]).lower() for r in conn.execute("PRAGMA table_info(%s)" % t)}
            except Exception:
                pass
        _SCHEMA_TOKENS_CACHE = names
    return _SCHEMA_TOKENS_CACHE


_METRIC_WORDS = {"funding", "yield", "volatility", "liquidity", "sentiment",
                 "momentum", "correlation", "beta", "drawdown", "turnover",
                 "confidence", "evidence", "score", "weight"}

# v422: 系统自身的执行层毛病被写成"市场驱动" —— 「同一事件重复入场」「信号生成缺陷」
# 描述的是我们这套系统的行为，不是品种涨跌的原因。命中即从注入材料里剔掉。
_SELF_META_WORDS = ("信号生成", "交易执行", "手续费", "滑点", "仓位管理", "止损",
                    "复盘", "结算失败", "过度交易", "重复入场", "冗余入场",
                    "未平仓止盈")

# v422: 中文 label / 证据里夹英文列名（实测 "(funding/cluster_heat/volatility)"）。
# 这类片段绕过"纯英文 label 才是列名"的旧规则，必须在碎片级拦。
_SCHEMA_FRAGMENT_WORDS = {"prompt", "verdict", "partial", "market_event", "trigger_ref",
                          "geopolitical_conflict", "historical_confidence", "cluster_heat",
                          "news_sentiment", "final_score", "gated_action", "pnl_pct",
                          "exit_ts", "asset", "ai_worker", "checklist"}


def _has_schema_fragment(text: str, min_len: int = 4) -> bool:
    """text 里是否嵌着字段名/指标口径词（英文、>=min_len、带下划线或命中白名单）。

    `btc/eth`、`ETF`、`VIX` 这类真实语义词长度 <4 或不在白名单，不会被误伤。
    """
    for tok in re.findall(r"[a-z][a-z0-9_]+", str(text or "").lower()):
        if len(tok) < min_len:
            continue
        if "_" in tok or tok in _METRIC_WORDS or tok in _SCHEMA_FRAGMENT_WORDS:
            return True
    return False

# v407: 跨品种污染 —— AAPL 的"核心驱动"里出现"黄金短期方向"这种事，实测真实发生。
# 只拦"另一品种的专有别名"，不拦通用宏观词（美元/实际利率对多个品种都成立）。
# 一个别名可合法属于多个品种（石油 → WTI/BRENT），所以按 codes 集合判断。
_FOREIGN_ALIASES = {
    "黄金": {"XAU"},
    "金价": {"XAU"},
    "原油": {"WTI", "BRENT"},
    "油价": {"WTI", "BRENT"},
    "石油": {"WTI", "BRENT"},
    "布伦特": {"BRENT"},
    "比特币": {"BTC"},
    "以太": {"ETH"},
    "加密": {"BTC", "ETH"},
    "加密资产": {"BTC", "ETH"},
    "白银": {"XAG"},
}


# 大宗商品彼此之间的传导是真实结构（油冲击→金价、金铜共振），所以这四个品种
# 不做跨品种拦截；股票/外汇/债券的"核心驱动"里出现黄金原油，才是噪声。
_COMMODITY_FAMILY = {"XAU", "XAG", "WTI", "BRENT", "COPPER", "CPI"}


def _foreign_asset_label(asset: str, label: str) -> bool:
    """label 里提到了**别的**品种的专有别名 → 对这个品种就是噪声驱动。"""
    a = str(asset or "").upper()
    if a in ("", "ALL", "MACRO") or a in _COMMODITY_FAMILY:
        return False
    s = str(label or "")
    for alias, codes in _FOREIGN_ALIASES.items():
        if alias in s and a not in codes:
            return True
    return False


# v422: 证据文本里的跨品种污染（实测 BTC 的条件因素写着「XAU 多头反应更快」）。
# 只拦"通篇在讲别的品种"的句子；同时提到本品种的（「BTC 与 XAU 共振」）保留。
_CROSS_ASSET_CODES = {"XAU", "XAG", "WTI", "BRENT", "BTC", "ETH", "SOL",
                      "COPPER", "NVDA", "AAPL", "MSFT", "TSLA", "COIN"}


def _mentions_foreign_asset(asset: str, text: str) -> bool:
    """text 是否只讲别的品种（不含本品种代码）。大宗商品族豁免（油→金是真传导）。"""
    a = _norm_asset(asset)
    if not a or a in ("ALL",) or a in _COMMODITY_FAMILY:
        return False
    s = str(text or "").upper()
    for code in _CROSS_ASSET_CODES:
        if code == a:
            continue
        # 不能用 \b：中文紧跟在代码后面时（"XAU多头…"）\b 不成立
        pat = r"(?<![A-Z0-9])%s(?![A-Z0-9])" % code
        if re.search(pat, s) and not re.search(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % a, s):
            return True
    return False


def _is_schema_label(conn: sqlite3.Connection, label: str) -> bool:
    s = str(label or "").strip().lower()
    if not s:
        return False
    if re.search(r"[\u4e00-\u9fa5]", s):
        # v422: 中文 label 里夹英文字段名（"(funding/cluster_heat/volatility)"）
        return _has_schema_fragment(s)
    if s in _schema_tokens(conn):
        return True
    # 纯小写英文下划线串 / 指标词 —— 实测这些就是列名或口径词，不是驱动
    if re.fullmatch(r"[a-z][a-z0-9_]{3,}", s) and ("_" in s or s in _METRIC_WORDS):
        return True
    return _has_schema_fragment(s)


def _looks_like_event(label: str) -> bool:
    s = str(label or "")
    if len(s) < 16:
        return False
    return any(v in s for v in _EVENT_VERBS)


def _upsert_node(conn: sqlite3.Connection, asset: str, label: str, kind: str,
                 is_core: bool, confidence: float, evidence: str = "",
                 expect: Optional[str] = None) -> Optional[int]:
    """插入或复用节点（同义合并）；已存在时上调置信度、版本 +1、重置为生效。

    关键不变量：节点的 ``kind`` / ``is_core`` 在首次 INSERT 后即视为「语义身份」，
    后续 UPDATE 不再覆盖。这样可避免：
      1) 一个长期生效的 core 节点被某次缺省的 condition 推理覆盖为 condition；
      2) 边的 kind（causes/conditions）与节点 kind（core/condition）出现不一致；
      3) 边 UNIQUE (from_id,to_id,kind) 仍能正确去重，因为 from/to 节点的 id 不变。

    evidence 处理：若传入的 evidence 非空且与原 evidence 不同（按 JSON list 去重后），
    则合并成新 JSON 列表写入；否则保留原值。这保留了「本节点历史上被哪些证据链支撑」
    的可追溯性，而不是无脑覆盖。
    """
    norm = label if kind == "asset" else _norm_label(label)
    if not norm:
        return None
    # v407 入库校验（两道）：
    #   1) 列名不是驱动 —— 直接拒绝入库，避免"historical_confidence 是 BTC 核心驱动"；
    #   2) 事件标题不是核心驱动 —— 降级为 condition（是触发条件，不是结构性原因）。
    # v422: asset / evidence 是图谱的合成锚点（资产端点、证据源），不是"驱动"，
    # 不参与下面三道驱动校验（否则 [证据] CN_STOCK 会因为 cn_stock 带下划线被拒）。
    if kind not in ("asset", "evidence") and _is_schema_label(conn, norm):
        print(f"[causal_graph] REJECT 列名当驱动: {asset}/{norm}", flush=True)
        return None
    if kind not in ("asset", "evidence", "macro") and _foreign_asset_label(asset, norm):
        print(f"[causal_graph] REJECT 跨品种污染: {asset}/{norm[:40]}", flush=True)
        return None
    if kind not in ("asset", "evidence") and is_core and _looks_like_event(norm):
        print(f"[causal_graph] DEMOTE 事件标题→条件因素: {asset}/{norm[:40]}", flush=True)
        is_core = 0
        kind = "condition"
    # 查找现有节点：先精确匹配归一化 label，再尝试大小写不敏感匹配，
    # 防止 asset 节点 label 未归一化（"BTC" vs "btc"）而错过同义合并。
    row = conn.execute(
        "SELECT id, confidence, version, kind, is_core, evidence FROM asset_driver_nodes "
        "WHERE asset=? AND label=?",
        (asset, norm),
    ).fetchone()
    if not row:
        # 大小写不敏感回退（SQLite LIKE 在 ASCII 默认大小写敏感，但 COLLATE NOCASE 可工作）
        row = conn.execute(
            "SELECT id, confidence, version, kind, is_core, evidence FROM asset_driver_nodes "
            "WHERE asset=? AND label COLLATE NOCASE = ?",
            (asset, norm),
        ).fetchone()
    now = _now()
    # v423: 归到规范因子（利率/通胀/地缘…）。归不到保持 NULL —— 不硬猜。
    fkey = factor_ontology.map_label(norm)
    # 老库/测试库可能还没有 factor_key 列：就地补，不依赖迁移脚本先跑
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_driver_nodes)")}
    if _cols and "factor_key" not in _cols:
        conn.execute("ALTER TABLE asset_driver_nodes ADD COLUMN factor_key TEXT")
    if row:
        nid = int(row[0])
        old_kind = row[3]
        old_is_core = int(row[4] or 0)
        old_evidence = row[5] or ""
        old_conf = float(row[1])
        new_conf = max(old_conf, float(confidence))
        # evidence 合并（按 JSON list 去重保留原始出现顺序）+ 保留核验元数据
        merged_evidence = _merge_evidence(old_evidence, evidence, expect=expect)
        # v424: 版本只在「内容真的变了」时 +1。
        # 旧实现无条件 +1 —— 每 6h 一次的 cron 都会把资产锚点节点 version++，
        # 实测 XAU=7246 / BTC=4612，这个数字看起来像"被验证了 7000 次"，
        # 实际只是被摸了 7000 次，把「版本」这个信号彻底毁掉。
        content_changed = (
            merged_evidence != (old_evidence or "")
            or new_conf > old_conf + 1e-9
            or kind != old_kind
        )
        new_ver = int(row[2]) + 1 if content_changed else int(row[2])
        # 防御性：若调用方错误地把 asset 节点当作 condition 传进来，记录一次警告
        # 但不修改 kind（按上述不变量）
        if old_kind == "asset" and kind != "asset":
            print(f"[causal_graph] WARN: 节点 {asset}/{norm} 已为 asset 节点，"
                  f"被以 kind={kind} 重新 upsert；保留 kind=asset 不变")
        conn.execute(
            "UPDATE asset_driver_nodes SET confidence=?, version=?, active=1, "
            "evidence=?, factor_key=COALESCE(NULLIF(factor_key,''), ?), updated_at=? "
            "WHERE id=?",
            (new_conf, new_ver, merged_evidence, fkey, now, nid),
        )
        return nid
    # v427：拒绝创建"没有机制文本的 chain 节点"。
    # 实测 951 个在册节点里 733 个 kind=chain 的节点 chain 为空 —— 它们没法核验
    # 方向、没法挂链库，只是把"资产名+一句断言"堆进图谱，还占 prompt 名额。
    # 只拦 chain 类型：core / condition 是结构位或推理产物，允许先建后补机制文本。
    if kind == "chain" and not (_parse_evidence(evidence or "").get("chain") or []):
        print(f"[causal_graph] REJECT 无机制文本的空壳节点: {asset}/{norm[:40]}", flush=True)
        return None
    cur = conn.execute(
        "INSERT INTO asset_driver_nodes "
        "(asset, label, kind, is_core, confidence, evidence, version, active, updated_at, factor_key) "
        "VALUES (?,?,?,?,?,?,1,1,?,?)",
        (asset, norm, kind, 1 if is_core else 0, confidence,
         _cap_evidence(_serialize_evidence(_parse_evidence(evidence or "").get("chain") or [],
                                           expect=expect)),
         now, fkey),
    )
    return int(cur.lastrowid)


def _merge_evidence(old: str, new: str, expect: Optional[str] = None) -> str:
    """合并 evidence：chain 取并集去重，**核验元数据沿用旧值**。

    v301-fix: 合并前先把嵌套的序列化 evidence 拆平（_flatten_chain_items）。
    不拆平的话，每次 upsert 都会把旧 evidence 重新 JSON 序列化一次，
    而列表元素本身是 JSON 字符串 → 每轮多一层转义 → 体积逐轮翻倍
    （生产库实测：13 个 asset 节点各 536,519,911 字节 = 65,493 × 2^13）。

    v424: 旧实现只返回纯 list，等于每次 upsert 都把 ``_status`` / ``_fail`` /
    ``_fact_ids`` 一起抹掉 —— 重新推理一轮，被事实否证过的核心悄悄变回
    「未确认」，核验结论全丢（实测 954 个活跃节点里 861 个是 UNCONFIRMED，
    其中相当一部分是这么来的）。现在 chain 与元数据一起合并。
    """
    old_ev = _parse_evidence(old)
    new_ev = _parse_evidence(new)
    items = _flatten_chain_items(list(old_ev.get("chain") or []) + list(new_ev.get("chain") or []))
    seen = set()
    out: List[str] = []
    for x in items:
        key = str(x)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    # 合并后为空（极端情况）→ 保留 new 原文，避免证据整体丢失
    if not out and new:
        out = [str(new)]
    return _cap_evidence(_serialize_evidence(
        out,
        status=old_ev.get("_status") or _STATUS_UNCONFIRMED,
        fail=old_ev.get("_fail"),
        position=old_ev.get("_position"),
        fact_ids=old_ev.get("_fact_ids") or [],
        verified_at=old_ev.get("_verified_at"),
        expect=expect or old_ev.get("_expect") or new_ev.get("_expect"),
    ))


# v300-audit: evidence 字段曾在生产库里长到 872,422,822 字节（单行一条），
# 后果有三：
#   1) asset_driver_nodes 的任何全表扫描都要读这几百 MB →
#      /api/analysis/kg-stats 与 /api/review/causal-graph/stats 稳定 60s+ 超时
#   2) WAL 被撑到 468MB，整个库膨胀到 3.66GB（freelist 188k 页）
#   3) 任何 SELECT ... FROM asset_driver_nodes 都被拖慢
# 根因：upsert 每次都往 evidence 追加一条，_merge_evidence 只去重不限制体积。
# 这里加硬上限，超限后丢弃最旧条目、保留最新证据。
_MAX_EVIDENCE_BYTES = 64 * 1024

# v301-fix: 上面这版 cap 有个致命漏洞 —— 循环条件是
#     if out and total + b > _MAX_EVIDENCE_BYTES: break
# 当「最新的一条」本身就超限时，out 还是空列表，`out` 短路 → 超限条目被直接放行，
# cap 形同虚设。实际事故：13 个 asset 节点各 536,519,911 字节
# （= 65,493 × 2^13，正好是 64KB 起步、每轮翻倍 13 次）。
# 翻倍机制：_merge_evidence 每次 upsert 都把旧 evidence 重新 JSON 序列化，
# 而列表元素本身是 JSON 字符串 → 每轮多一层引号/反斜杠转义 → 体积大约 ×2。
# 本次修复三件事：
#   1) 单条超限也硬截断（cap 真正生效）；
#   2) 合并/解析前把嵌套的序列化 evidence 拆平（chain 里只留纯文本）；
#   3) 体积核算按「转义后」的真实字节数算，不再低估。
_MAX_EVIDENCE_ITEM_BYTES = 8 * 1024      # 单条 chain item 上限（拆平后每条应是短句）
_MAX_EVIDENCE_NEST_DEPTH = 32            # 嵌套拆平层数上限（防御性，正常 1~2 层）


def _truncate_bytes(s: str, limit: int) -> str:
    """按 UTF-8 字节截断字符串（不切碎多字节字符）。"""
    raw = s.encode("utf-8", "ignore")
    if len(raw) <= limit:
        return s
    return raw[:limit].decode("utf-8", "ignore") + "...[TRUNCATED]"


def _flatten_chain_items(items: List[Any], _depth: int = 0) -> List[str]:
    """把 chain item 里嵌套的序列化 evidence（'{"chain": [...]}' 字符串）拆平。

    嵌套是体积翻倍的关键：外层列表每被重新 JSON 序列化一次，内层的引号/反斜杠
    就多一层转义，体积大约翻倍。拆平后 chain 里只剩纯文本，套娃不再增长。
    """
    out: List[str] = []
    for x in items or []:
        s = str(x)
        if _depth < _MAX_EVIDENCE_NEST_DEPTH and len(s) > 2 and s.lstrip()[:1] == "{":
            try:
                v = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                v = None
            if isinstance(v, dict) and isinstance(v.get("chain"), list):
                out.extend(_flatten_chain_items([y for y in v["chain"]], _depth + 1))
                continue
        out.append(s)
    return out


def _cap_evidence(text: str) -> str:
    """把 evidence 字符串限制在 _MAX_EVIDENCE_BYTES 以内。

    超限时丢最旧条目、保留最新证据；**单条本身就超限时也硬截断**，
    否则 cap 会被单条巨型 evidence 绕过（见上方 v301-fix 注释）。
    """
    if not text:
        return text or ""
    if len(text.encode("utf-8", "ignore")) <= _MAX_EVIDENCE_BYTES:
        return text
    try:
        items = json.loads(text)
        if not isinstance(items, list):
            items = [text]
    except (json.JSONDecodeError, TypeError):
        items = [text]
    items = [str(x) for x in items]
    out: List[str] = []
    total = 0
    for x in reversed(items):          # 最新的优先保留
        b = len(json.dumps(x, ensure_ascii=False).encode("utf-8", "ignore")) + 2
        if out and total + b > _MAX_EVIDENCE_BYTES:
            break
        if not out and b > _MAX_EVIDENCE_BYTES:
            # 单条就超限 → 硬截断，并按转义后体积回缩，确保最终落回上限内
            limit = _MAX_EVIDENCE_BYTES // 2
            while limit > 256:
                x = _truncate_bytes(x, limit)
                b = len(json.dumps(x, ensure_ascii=False).encode("utf-8", "ignore")) + 2
                if b <= _MAX_EVIDENCE_BYTES:
                    break
                limit //= 2
        out.append(x)
        total += b
    out.reverse()
    return json.dumps(out, ensure_ascii=False)


def _parse_evidence(raw: str) -> Dict[str, Any]:
    """解析 evidence 字段为统一 dict 结构（容错旧数据）。

    输入兼容：
      - 空字符串 / None → 返回空 dict（chain=[]）
      - 纯 list JSON（旧数据）→ 自动包装为 {_status: UNCONFIRMED, chain: [...]}
      - 完整 dict JSON（新数据）→ 直接返回
      - 任意字符串（不是 JSON）→ 当作单元素 chain

    返回结构总是包含：
      {
        "chain": [<str>, ...],
        "_status": str,         # 缺省 UNCONFIRMED
        "_fail": str|None,      # 缺省 None
        "_verified_at": str|None,
        "_fact_ids": [int, ...],
        "_position": str|None,
      }
    """
    if not raw:
        return {
            "chain": [],
            "_status": _STATUS_UNCONFIRMED,
            "_fail": None,
            "_verified_at": None,
            "_fact_ids": [],
            "_position": None,
            "_expect": None,
        }
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 整个串不是 JSON → 当作单元素 chain
        return {
            "chain": [str(raw)],
            "_status": _STATUS_UNCONFIRMED,
            "_fail": None,
            "_verified_at": None,
            "_fact_ids": [],
            "_position": None,
            "_expect": None,
        }
    if isinstance(v, list):
        # 旧数据：纯 list
        return {
            "chain": _flatten_chain_items([str(x) for x in v if str(x).strip()]),
            "_status": _STATUS_UNCONFIRMED,
            "_fail": None,
            "_verified_at": None,
            "_fact_ids": [],
            "_position": None,
            "_expect": None,
        }
    if isinstance(v, dict):
        # 新数据：补全缺省字段
        chain = v.get("chain", [])
        if not isinstance(chain, list):
            chain = [str(chain)]
        return {
            "chain": _flatten_chain_items([str(x) for x in chain if str(x).strip()]),
            "_status": v.get("_status", _STATUS_UNCONFIRMED),
            "_fail": v.get("_fail"),
            "_verified_at": v.get("_verified_at"),
            "_fact_ids": v.get("_fact_ids", []) or [],
            "_position": v.get("_position"),
            "_expect": v.get("_expect"),
            # v425：推断来的预期方向要能随规则修正而刷新；llm 标注的不动
            "_expect_src": v.get("_expect_src") or "inferred",
            # v425：条件样本刻度必须一起读出来，否则输出层拿不到（曾漏透传导致
            # 已验证机制显示成 0/0）
            "_cond_n": v.get("_cond_n", 0) or 0,
            "_cond_hits": v.get("_cond_hits", 0) or 0,
            "_cond_ratio": v.get("_cond_ratio", 0.0) or 0.0,
            "_cond_chains": v.get("_cond_chains", []) or [],
            # v426：方向基准与净增益，输出层要用（漏透传会让"净增益"显示成 null）
            "_cond_base_rate": v.get("_cond_base_rate"),
            "_cond_edge": v.get("_cond_edge"),
            # v432：样本外（时间后一半）战绩必须一起读出来，否则降级逻辑拿不到
            "_cond_oos_n": v.get("_cond_oos_n", 0) or 0,
            "_cond_oos_hits": v.get("_cond_oos_hits", 0) or 0,
            "_cond_oos_ratio": v.get("_cond_oos_ratio"),
            "_cond_is_ratio": v.get("_cond_is_ratio"),
            # v430：定性分层与文本出处必须透传；漏了就会让 annotation 节点
            # 混回注入块（#583 刚被修复完就差这一行）
            "_tier": v.get("_tier"),
            "_text_src": v.get("_text_src"),
            # v427：链库来源（补链时写入 chain_lib:<id>，核验后会重写 evidence，
            # 不透传就会把"这条机制出自哪条链"抹掉，无法回溯）
            "_source": v.get("_source"),
        }
    # 兜底
    return {
        "chain": [str(v)],
        "_status": _STATUS_UNCONFIRMED,
        "_fail": None,
        "_verified_at": None,
        "_fact_ids": [],
        "_position": None,
        "_expect": None,
    }


def _serialize_evidence(chain: List[str], status: str = _STATUS_UNCONFIRMED,
                         fail: Optional[str] = None,
                         position: Optional[str] = None,
                         fact_ids: Optional[List[int]] = None,
                         verified_at: Optional[str] = None,
                         expect: Optional[str] = None,
                         expect_src: Optional[str] = None,
                         cond: Optional[Dict[str, Any]] = None,
                         tier: Optional[str] = None,
                         source: Optional[str] = None) -> str:
    """序列化为 JSON 字符串（统一入口，写库前必走这里）。

    ``expect``（v424）= 该机制成立时**本品种应有的方向**（"up"/"down"/None）。
    这是核验的"预期端"：旧实现拿「驱动端方向」去跟「资产涨跌」比，反向机制
    （实际利率↑→金价↓）会被系统性判反，这里把预期端显式存下来。
    """
    # v430 护栏：若把 evidence 字典误当 chain 传进来，会静默写出一串键名
    # （2026-11-18 事故：6 个节点的机制文本被这样覆盖）。这里显式拦下。
    if isinstance(chain, dict):
        raise TypeError("_serialize_evidence(chain=...) 不接受 dict，"
                        "请展开为 chain=list / status=... 等具名参数")
    obj = {
        "chain": [str(x) for x in (chain or []) if str(x).strip()],
        "_status": status,
        "_fail": fail,
        "_verified_at": verified_at,
        "_fact_ids": list(fact_ids or []),
        "_position": position,
    }
    if source:
        obj["_source"] = source
    if expect:
        obj["_expect"] = expect
        # v425：记来源。llm=模型明确标注（不覆盖）；inferred=本代码推断（
        # 推断规则修好后要能刷新生效，否则旧的错判会永久锁死在库里）
        obj["_expect_src"] = expect_src or "inferred"
    # v425：条件样本（链库 hit/miss），有才写，避免给无匹配的节点造假样本
    # v433：分层标记必须原样带回。核验会重写 evidence，漏了这一步
    # annotation 节点就会在每次核验后"洗白"，重新混进注入块。
    if tier:
        obj["_tier"] = str(tier)
    if cond and int(cond.get("n") or 0) > 0:
        obj["_cond_n"] = int(cond.get("n") or 0)
        obj["_cond_hits"] = int(cond.get("hits") or 0)
        obj["_cond_ratio"] = float(cond.get("ratio") or 0.0)
        obj["_cond_chains"] = list(cond.get("chain_ids") or [])
        # v426：方向基准（同期"随便猜这个方向"能蒙对的概率）与净增益。
        # 实测 XAU 近 30 天 5 日窗口 64% 下跌 —— 一个 21/33=64% 的看跌机制
        # 恰好等于"什么都不想、直接猜跌"，不加基准就只能把漂移当本事。
        if cond.get("base_rate") is not None:
            obj["_cond_base_rate"] = float(cond["base_rate"])
        if cond.get("edge") is not None:
            obj["_cond_edge"] = float(cond["edge"])
        # v432：样本外（时间后一半）战绩 —— 用同一批样本既训练又验收的毛病
        if int(cond.get("oos_n") or 0) > 0:
            obj["_cond_oos_n"] = int(cond.get("oos_n") or 0)
            obj["_cond_oos_hits"] = int(cond.get("oos_hits") or 0)
            obj["_cond_oos_ratio"] = float(cond.get("oos_ratio") or 0.0)
            obj["_cond_is_ratio"] = float(cond.get("is_ratio") or 0.0)
    return json.dumps(obj, ensure_ascii=False)


def _chain_text(ev: Dict[str, Any]) -> str:
    """把 chain 列表拼成单字符串（用于关键词匹配）。"""
    return " ".join(str(x) for x in ev.get("chain", []))


# ───────── v425：条件样本核验（P2）─────────
# 背景：v424 的计分制仍然用"共享交易事实池"，而那个池子里的样本与机制是否触发
# 无关（每笔交易都算一条），所以任何机制的命中率都天然趋近 50%，永远出不了
# CONFIRMED。v425 增加第二条证据源：链库自身的条件样本 —— `causal_chains`
# 的 hit_count/miss_count 只在**链被触发**（新闻命中触发词）时累计，这才是
# "机制在它成立的场合里到底有没有效"的样本。
#
# 节点与链之间没有 ID 直连（节点 evidence 存的是链句子文本），所以用"二元组
# 包含度"做严格匹配：阈值 0.55，且只认同品种/宏观链；名字完全一致的直接满分。
# 实测该阈值在 XAU/WTI 上给出的匹配都可人工核对（地缘→地缘链、沙特OSP→OSP链），
# 更低的阈值（0.05~0.2）会大量误配（流动性节点配到 SEC ETP 链），所以不采用。
_CHAIN_MATCH_MIN = 0.55      # 节点文本与链名的二元组包含度下限
_COND_TOP_K = 3              # 单个节点最多聚合几条链，避免"挂一堆弱链"虚增样本
_CHAIN_INDEX_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CHAIN_INDEX_TTL = 300.0     # 链库索引缓存 5 分钟（链库变更不频繁）


def _bigrams(text: str) -> set:
    """中文二元组集合（去掉标点/空白），用于轻量文本相似度。"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _contain_ratio(small: str, big: str) -> float:
    """small 的二元组有多大比例出现在 big 里（0~1）。"""
    s = _bigrams(small)
    if not s:
        return 0.0
    return len(s & _bigrams(big)) / len(s)


def _build_chain_index(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """载入链库（只保留可匹配的链），样本量取自 `causal_chain_outcomes` 的方向结算。

    v425 关键口径修正：链表上的 hit_count/miss_count **不能直接当方向命中率**——
    实测 chain#1/#69/#159 的 expected_direction 是 BOTH，结算全部 predicted_direction
    也是 BOTH，direction_hit=1 只表示"幅度达标"，不含方向信息。若把它算成"方向命中
    10/10"，就是又把历史上那个"BOTH 当方向"的错重犯一遍。

    所以这里逐条统计结果表：只计 status 已结算、且 predicted_direction ∈ {UP,DOWN}
    的样本，命中 = direction_hit=1。返回 [{id, name, asset, hit, miss, n, grams...}]。
    """
    cached = _CHAIN_INDEX_CACHE.get("data")
    if cached is not None and (time.time() - float(_CHAIN_INDEX_CACHE.get("ts") or 0)) < _CHAIN_INDEX_TTL:
        return cached
    rows = conn.execute(
        "SELECT id, name, cause, effect, asset, origin FROM causal_chains"
    ).fetchall()
    agg: Dict[int, Dict[str, int]] = {}
    try:
        # v426：按**方向分桶**统计。之前把一条链的 UP 样本和 DOWN 样本混在一起，
        # 于是"看跌机制"可以拿"看涨链"的命中给自己背书（实测 XAU #565 就是这么
        # 拿到 21/33 的）。方向不同的样本根本不能互换。
        for r in conn.execute(
            "SELECT CAST(chain_id AS INTEGER) AS cid, UPPER(predicted_direction) AS d, "
            "COUNT(*) AS n, SUM(CASE WHEN direction_hit=1 THEN 1 ELSE 0 END) AS h "
            "FROM causal_chain_outcomes "
            "WHERE status IN ('HIT','HIT_WEAK','MISS') AND UPPER(predicted_direction) IN ('UP','DOWN') "
            "GROUP BY cid, d"
        ).fetchall():
            cur = agg.setdefault(int(r[0]), {"up_n": 0, "up_hits": 0, "down_n": 0, "down_hits": 0})
            if str(r[1]).upper() == "UP":
                cur["up_n"] = int(r[2] or 0)
                cur["up_hits"] = int(r[3] or 0)
            else:
                cur["down_n"] = int(r[2] or 0)
                cur["down_hits"] = int(r[3] or 0)
    except sqlite3.OperationalError:
        agg = {}  # 表不存在/结构不同 → 全部无样本，退回交易池
    index: List[Dict[str, Any]] = []
    for r in rows:
        # 已被标记为元信息脏链的（v424 清洗）不参与，避免用它给节点背书
        if str(r["origin"] or "") == "deprecated_meta":
            continue
        name = str(r["name"] or "").strip()
        if not name:
            continue
        a = agg.get(int(r["id"]), {"up_n": 0, "up_hits": 0, "down_n": 0, "down_hits": 0})
        index.append({
            "id": int(r["id"]),
            "name": name,
            "asset": str(r["asset"] or "").upper(),
            "up_n": int(a["up_n"] or 0),
            "up_hits": int(a["up_hits"] or 0),
            "down_n": int(a["down_n"] or 0),
            "down_hits": int(a["down_hits"] or 0),
            "grams_name": _bigrams(name),
            "grams_full": _bigrams(name + str(r["cause"] or "") + str(r["effect"] or "")),
        })
    _CHAIN_INDEX_CACHE["data"] = index
    _CHAIN_INDEX_CACHE["ts"] = time.time()
    return index


_BASE_RATE_LOOKBACK_DAYS = 90
_BASE_RATE_HORIZON = 5
_EDGE_MARGIN = 0.08
_EDGE_CONFIRM = 0.10
_EDGE_REFUTE = 0.10
_BASE_RATE_CACHE: Dict[Tuple[str, int, int], Optional[float]] = {}


def _direction_base_rate(conn: sqlite3.Connection, asset: str,
                         days: int = _BASE_RATE_LOOKBACK_DAYS,
                         horizon: int = _BASE_RATE_HORIZON) -> Optional[float]:
    """同期"闭着眼猜这个方向"能蒙对的概率（v426）。

    为什么要它：链级方向准确率 40%（UP）/ 60%（DOWN）看着像机制有效，实测
    XAU 近 30 天 5 日窗口本身就是 64% 下跌 —— 看跌机制"验证通过"其实只是
    骑在行情漂移上。核验必须扣掉这个基准，否则越跌越"准"。

    取近 days 天的日线，算每根 K 线往后 horizon 根的总收益，统计上涨占比。
    数据缺失时返回 None（调用方退回旧口径，不阻断核验）。
    """
    key = (asset, days, horizon)
    if key in _BASE_RATE_CACHE:
        return _BASE_RATE_CACHE[key]
    try:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? ORDER BY ts DESC LIMIT ?",
            (asset, max(days * 2, 30)),
        ).fetchall()
    except sqlite3.Error:
        _BASE_RATE_CACHE[key] = None
        return None
    if not rows or len(rows) <= horizon:
        _BASE_RATE_CACHE[key] = None
        return None
    series = [(int(r[0]), float(r[1])) for r in reversed(rows)]
    lo_ts = series[-1][0] - days * 86400
    n = ups = 0
    for i in range(len(series) - horizon):
        if series[i][0] < lo_ts:
            continue
        n += 1
        ups += 1 if series[i + horizon][1] > series[i][1] else 0
    p_up = round(ups / n, 3) if n else None
    _BASE_RATE_CACHE[key] = p_up
    return p_up


def _family_cond_samples(conn: sqlite3.Connection, asset: str, factor_key: Optional[str],
                         exclude_id: int) -> Dict[str, Any]:
    """核心驱动的"同族印证"：把同品种、同族（factor_ontology.family）的机制节点
    条件样本累加起来。

    为什么需要：56 个核心的名字是**族级概念**（"美国实际利率与美元信用预期"），
    链库里没有同名机制链，文本匹配永远挂不上，核心只能显示"无样本"。但图上
    同族机制（例如 rates 族的"美联储鹰派→金价压制"）是有的、而且已验证过。
    核心不该自己去猜方向，应该继承同族机制的战绩。

    保守原则：只用于**加分**（够阈值才标"同族印证"），绝不用它判否证——
    同族是语义归类，不是同一机制，拿它否掉一个核心太激进。
    """
    fam = factor_ontology.family(factor_key) if factor_key else ""
    empty = {"n": 0, "hits": 0, "ratio": 0.0, "mechanisms": []}
    if not fam:
        return empty
    rows = conn.execute(
        "SELECT id, label, factor_key, evidence FROM asset_driver_nodes "
        "WHERE asset=? AND active=1 AND id!=? AND factor_key IS NOT NULL "
        "AND kind NOT IN ('asset','evidence')",
        (asset, exclude_id),
    ).fetchall()
    n = hits = 0
    mechs: List[Dict[str, Any]] = []
    for r in rows:
        if factor_ontology.family(r["factor_key"]) != fam:
            continue
        ev = _parse_evidence(r["evidence"] or "")
        cn = int(ev.get("_cond_n") or 0)
        if cn < _MIN_VERIFY_SAMPLES:
            continue
        n += cn
        hits += int(ev.get("_cond_hits") or 0)
        mechs.append({"node_id": int(r["id"]), "factor": r["label"],
                      "cond_n": cn, "cond_hits": int(ev.get("_cond_hits") or 0)})
    if not n:
        return empty
    return {"n": n, "hits": hits, "ratio": round(hits / n, 3),
            "mechanisms": mechs[:_COND_TOP_K]}


def _cond_oos_split(conn: sqlite3.Connection, chain_ids: Sequence[int],
                    expect: Optional[str]) -> Dict[str, Any]:
    """把条件样本按时间对半切，给出前/后两半各自战绩（v432 walk-forward）。

    为什么必须做：唯一被标 CONFIRMED 的机制实测前一半 15/21=0.714、
    后一半 9/21=0.429 —— 样本内漂亮、样本外跌破硬币，是典型过拟合。
    只报总体命中率等于用同一批样本既训练又验收。
    """
    out = {"oos_n": 0, "oos_hits": 0, "oos_ratio": 0.0,
           "is_n": 0, "is_hits": 0, "is_ratio": 0.0}
    ids = [int(i) for i in (chain_ids or []) if i]
    if not ids or expect not in ("up", "down"):
        return out
    dir_ = "UP" if expect == "up" else "DOWN"
    marks = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""SELECT direction_hit FROM causal_chain_outcomes
                WHERE chain_id IN ({marks}) AND predicted_direction=?
                  AND status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL
                ORDER BY trigger_ts""", (*ids, dir_)).fetchall()
    except sqlite3.Error:
        return out
    n = len(rows)
    if n < 6:
        return out
    mid = n // 2
    ins = [int(r[0]) for r in rows[:mid]]
    oos = [int(r[0]) for r in rows[mid:]]
    if ins:
        out["is_n"], out["is_hits"] = len(ins), sum(ins)
        out["is_ratio"] = round(sum(ins) / len(ins), 3)
    if oos:
        out["oos_n"], out["oos_hits"] = len(oos), sum(oos)
        out["oos_ratio"] = round(sum(oos) / len(oos), 3)
    return out


def _match_chain_samples(chain_index: Optional[List[Dict[str, Any]]],
                         asset: str, text: str,
                         expect: Optional[str] = None) -> Dict[str, Any]:
    """给节点找"它由哪些链支撑"，累计这些链的条件样本。

    v426：多了一个 `expect` —— 只累计**方向一致**的样本。之前把一条链的
    UP 样本和 DOWN 样本混算，于是"鹰派→金价跌"这种看跌机制可以拿看涨链的
    命中给自己背书（XAU #565 就是这么刷出 21/33 的，实际它自己那条链的
    看跌样本是 13/20）。方向不一致的样本没有任何可比性。

    返回 {n, hits, ratio, chain_ids, matched:[{id,name,n,hit,miss,score}]}；
    找不到链时 n=0（调用方据此回退到交易事实池）。
    """
    empty = {"n": 0, "hits": 0, "ratio": 0.0, "chain_ids": [], "matched": []}
    if not chain_index or not text:
        return empty
    asset_u = (asset or "").upper()
    grams_text = _bigrams(text)
    if not grams_text:
        return empty
    cands: List[Dict[str, Any]] = []
    for c in chain_index:
        if c["asset"] and c["asset"] not in (asset_u, "MACRO"):
            continue
        if not c["grams_name"]:
            continue
        # 双向包含取大：链名整个出现在节点文本里，或节点文本被链名覆盖
        score = max(len(c["grams_name"] & grams_text) / len(c["grams_name"]),
                    len(c["grams_full"] & grams_text) / max(1, len(grams_text)))
        if score < _CHAIN_MATCH_MIN:
            continue
        # v426：方向必须对得上。expect 推不出来时不挑方向，但记 mixed 供上层说明
        d = (expect or "").lower()
        if d == "up":
            cn, ch = int(c.get("up_n") or 0), int(c.get("up_hits") or 0)
        elif d == "down":
            cn, ch = int(c.get("down_n") or 0), int(c.get("down_hits") or 0)
        else:
            cn = int(c.get("up_n") or 0) + int(c.get("down_n") or 0)
            ch = int(c.get("up_hits") or 0) + int(c.get("down_hits") or 0)
        if cn <= 0:
            continue
        cands.append({**c, "score": round(score, 3), "n": cn, "hit": ch})
    if not cands:
        return empty
    # 先按匹配度、再按样本量排序；只取前 K 条聚合
    cands.sort(key=lambda c: (c["score"], c["n"]), reverse=True)
    picked = cands[:_COND_TOP_K]
    hits = sum(int(c["hit"]) for c in picked)
    n = sum(int(c["n"]) for c in picked)
    return {
        "n": n,
        "hits": hits,
        "ratio": round(hits / n, 3) if n else 0.0,
        "chain_ids": [int(c["id"]) for c in picked],
        "matched": [{"id": int(c["id"]), "name": c["name"][:24],
                     "n": int(c["n"]), "hit": int(c["hit"]),
                     "miss": int(c["n"]) - int(c["hit"]), "score": c["score"]} for c in picked],
    }


def _classify_position(node_kind: str, is_core: bool,
                       outgoing_count: int, incoming_count: int) -> str:
    """节点逻辑位置分类（三档）。

    规则（基于边数）：
      - cause: 只出不入（驱动源）
      - effect: 只入不出（果端 / 资产节点）
      - middle: 都有

    注意：asset 节点强制归 effect（它是图谱的"果端 anchor"）。
    """
    if node_kind == "asset":
        return _POSITION_EFFECT
    if is_core:
        # core 节点天然偏 cause（驱动源）
        return _POSITION_CAUSE if outgoing_count > 0 else _POSITION_MIDDLE
    if outgoing_count > 0 and incoming_count > 0:
        return _POSITION_MIDDLE
    if outgoing_count > 0:
        return _POSITION_CAUSE
    if incoming_count > 0:
        return _POSITION_EFFECT
    # 无边节点（孤立）
    return _POSITION_MIDDLE


def _extract_indicator_keywords(indicator: str) -> List[str]:
    """从 fact.indicator 取所有同义关键词（含自身）。"""
    indicator = (indicator or "").strip()
    if not indicator:
        return []
    kws = {indicator.lower()}
    for canonical, aliases in _INDICATOR_ALIASES.items():
        if canonical.lower() == indicator.lower() or indicator.lower() in [a.lower() for a in aliases]:
            for a in aliases:
                kws.add(a.lower())
            kws.add(canonical.lower())
    return list(kws)


def _chain_contains_indicator(chain_text: str, indicator: str) -> bool:
    """判断 chain 文本是否提到 fact.indicator 的任一同义词。"""
    kws = _extract_indicator_keywords(indicator)
    text = chain_text.lower()
    return any(kw in text for kw in kws if kw)


_DIR_UP_WORDS = (
    "涨", "上行", "上涨", "升值", "利好", "升", "+",
    "走强", "增强", "强势", "牛市", "正向", "正面",
    "反弹", "突破", "走高", "攀升", "回升", "向上",
    "提振", "助推", "利多", "增益", "扩张", "上扬",
)
_DIR_DOWN_WORDS = (
    "跌", "下行", "下跌", "贬值", "利空", "降", "-",
    "走弱", "减弱", "弱势", "熊市", "负向", "负面",
    "回落", "跌破", "走低", "下挫", "向下",
    "压制", "承压", "拖累", "收缩",
)

# v424: 「资产方向线索」——出现这些词，同一层里的方向词才算"本品种的方向"。
_ASSET_DIRECTION_CUES = (
    "价格", "金价", "油价", "银价", "币价", "股价", "指数", "资产",
    "多头", "空头", "受益", "承压", "避险资金", "风险偏好",
)


def _direction_of(text: str) -> Optional[str]:
    """单层文本的方向词判定（up/down/None）。同层双向词都有时返回 None。"""
    s = str(text or "")
    up = any(w in s for w in _DIR_UP_WORDS)
    down = any(w in s for w in _DIR_DOWN_WORDS)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def _direction_of_last(text: str) -> Optional[str]:
    """同层同时出现涨/跌词时，取**最后出现**的那个（结局在句末）。

    v425：中文因果链是"因→…→果"的线性表达，结局落在句末。实测
    "以美元计价的无息资产WTI折现率上升→持有成本压制价格" 这类句子，
    因端有"上升"、果端有"压制"，旧逻辑判成 None → 上层兜底取到"上升"，
    把一个"鹰派→油价承压"的反向机制记成 expect=up，核验时必然判反。
    """
    s = str(text or "")
    up_pos = max((s.rfind(w) for w in _DIR_UP_WORDS), default=-1)
    down_pos = max((s.rfind(w) for w in _DIR_DOWN_WORDS), default=-1)
    if up_pos < 0 and down_pos < 0:
        return None
    return "up" if up_pos > down_pos else "down"


def _chain_infers_direction(chain: List[str]) -> Optional[str]:
    """从 chain 推理「驱动端方向」（保留旧语义，供 belief_store 等旧调用方使用）。

    ⚠️ v424 起**核验不再使用本函数**：它返回的是"因端方向"，直接跟"资产涨跌"
    比较时，反向机制（实际利率↑→金价↓）会被系统性判反。核验改用
    :func:`_infer_expect_direction`（资产预期方向）。
    """
    for layer in chain:
        d = _direction_of(layer)
        if d:
            return d
    return None


def _infer_expect_direction(chain: List[str], label: str = "",
                            asset: str = "") -> Optional[str]:
    """推断「该机制成立时本品种应有的方向」（核验的预期端，v424）。

    优先级：
      1) 有「资产方向线索」的层（价格/金价/多头/承压…）→ 取它的方向，离资产
         最近的一层优先（chain[0] 在边构造里最靠近资产）；
      2) 全链方向唯一（所有层同向）→ 用它（保守兜底，语义不含糊）；
      3) 层间方向冲突 → None，**不臆断**。核验时记 UNCONFIRMED，而不是判错。
    """
    layers = [str(x) for x in (chain or []) if str(x).strip()]
    if label:
        layers.append(str(label))
    if not layers:
        return None

    asset_u = str(asset or "").upper()
    cues = list(_ASSET_DIRECTION_CUES)
    if asset_u:
        cues.append(asset_u)

    # 1) 资产线索层优先（chain[0] 最靠近资产）
    for layer in layers:
        if any(c and c in layer for c in cues):
            d = _direction_of(layer) or _direction_of_last(layer)
            if d:
                return d

    # 2) v426：**没有资产线索就不猜**。
    # 旧实现第二档"全链同向就用它"——实测这是反向机制判反的复发：
    # 节点「美联储官员一致警告通胀过高,利率仍有上行空间」链为空，只剩标签，
    # 标签里只有因端"上行"，于是推出 expect=up，而这条机制对 XAU 实际是跌；
    # 再拿它去和方向相反的链样本对齐，就能凑出"21/33 已验证"。
    # 宁可记"无预期方向"（核验时只判未确认），也不要用错方向去凑结论。
    return None


def _upsert_edge(conn: sqlite3.Connection, from_id: Optional[int], to_id: Optional[int],
                 kind: str, weight: float = 1.0, evidence: str = "") -> None:
    """插入或强化一条边（证据越多权重越大，上限 10）。

    使用 ``INSERT ... ON CONFLICT`` 原子化处理 (from_id,to_id,kind) 唯一约束，
    避免「先 SELECT 再 UPDATE」在并发情况下丢失其它 worker 的累加结果。
    """
    if not from_id or not to_id or from_id == to_id:
        return
    now = _now()
    # weight 累加上限 10；新插入时直接用 weight
    conn.execute(
        "INSERT INTO asset_driver_edges (from_id,to_id,kind,weight,evidence,updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(from_id,to_id,kind) DO UPDATE SET "
        "  weight    = MIN(weight + excluded.weight, 10.0), "
        "  evidence  = excluded.evidence, "
        "  updated_at = excluded.updated_at",
        (from_id, to_id, kind, weight, evidence, now),
    )


# 跨资产神经（cross-asset synapse）======================================
# v276+: 让图谱像人脑一样,资产之间有「突触」相互传导。
#
# 来源:
#   1) 硬编码常识对 (DXY↔XAU, BTC↔ETH 等 16 对)
#   2) causal_chains 表里 cause 同现的两两资产 (双向 supports)
#   3) abnormal_attributions 同 run_id 反向异动 (反例 contradicts)
#
# 形态:
#   - from_asset(节点) --[correlates_with 0.6]--> to_asset(节点)
#   - 边 weight = base_weight + 共现次数 * 0.1
#
# 反例 (defense vs confirmation bias):
#   - XAU 和 BTC 历史上 70% 同向, 30% 反向 → 既有 supports 边, 又有 contradicts 边
#   - 前端展示「红绿并存」→ 提示用户「不是单边信念,有反例」
# ============================================================================

# 16 对硬编码宏观传导(常识兜底 — 数据源都没有也能保证神经网基本结构)
_HARDCODED_CROSS_LINKS: List[tuple] = [
    # (from_asset, to_asset, mechanism_label, base_weight)
    ("DXY", "XAU", "美元走强压制黄金", 0.6),
    ("DXY", "WTI", "美元走强压制原油(计价)", 0.4),
    ("DXY", "BTC", "美元走强压制风险资产(弱)", 0.2),
    ("US10Y", "XAU", "实际利率↑直接压制黄金", 0.7),
    ("US10Y", "BTC", "无风险收益↑压制风险资产", 0.5),
    ("US10Y", "WTI", "利率走高压制工业/经济预期", 0.3),
    ("XAU", "XAG", "贵金属板块同向", 0.6),
    ("XAU", "BTC", "避险/风险偏好同步(弱)", 0.2),
    ("WTI", "CPI", "能源价格传导通胀", 0.5),
    ("WTI", "XAU", "通胀预期↑利好黄金", 0.4),
    ("BTC", "ETH", "龙头币带动板块", 0.7),
    ("ETH", "SOL", "山寨β龙头", 0.5),
    ("BTC", "SOL", "整体风险偏好同步", 0.4),
    ("SPX", "BTC", "风险资产联动", 0.3),
    ("SPX", "WTI", "经济预期↔工业品需求", 0.3),
    ("NDX", "BTC", "科技/数字资产联动", 0.3),
    ("US_STOCK", "CN_STOCK", "全球风险偏好同步(弱)", 0.2),
    ("CN_STOCK", "WTI", "中国需求是大宗关键变量", 0.3),
]


def _ensure_asset_node(conn: sqlite3.Connection, symbol: str) -> Optional[int]:
    """确保资产节点存在,返回 id。"""
    sym = _norm_asset(symbol)
    if not sym or sym == "NONE":
        return None
    return _upsert_node(conn, sym, sym, "asset", False, 1.0, "")


def link_cross_asset_neurons(asset: Optional[str] = None) -> Dict[str, Any]:
    """建跨资产神经 — 让图谱里多个 asset 节点之间有 [correlates_with] 边。

    v276+: 之前图谱是 3 个孤岛 (BTC/ETH/SOL 各自一摊),现在把 XAU/XAG/WTI/DXY/
    US10Y/SPX/NDX/CN_STOCK 都连起来,前端可视化才能画出「像人脑神经网络的图」。
    """
    conn = db.get_connection()
    added = 0
    updated = 0
    evidence_samples: List[str] = []

    try:
        # 1) 硬编码传导对(常识兜底)
        for from_a, to_a, mechanism, base_w in _HARDCODED_CROSS_LINKS:
            if asset and asset not in (from_a, to_a):
                continue
            fid = _ensure_asset_node(conn, from_a)
            tid = _ensure_asset_node(conn, to_a)
            if fid and tid:
                before = conn.execute(
                    "SELECT weight FROM asset_driver_edges "
                    "WHERE from_id=? AND to_id=? AND kind=?",
                    (fid, tid, "correlates_with"),
                ).fetchone()
                if before:
                    updated += 1
                else:
                    added += 1
                _upsert_edge(conn, fid, tid, "correlates_with",
                             weight=base_w,
                             evidence=f"[hardcoded] {mechanism}")
                if len(evidence_samples) < 6:
                    evidence_samples.append(
                        f"{from_a}→{to_a}: {mechanism} (w={base_w})")
        conn.commit()
        return {
            "ok": True,
            "links_added": added,
            "links_updated": updated,
            "evidence_samples": evidence_samples,
        }
    finally:
        conn.close()


def seed_from_causal_chains(asset: Optional[str] = None,
                             min_weight: float = 0.5,
                             limit: int = 200,
                             macro_mode: str = "off",
                             macro_max_len: int = 14) -> Dict[str, Any]:
    """把 causal_chains 表里的链条种进图谱 — 免 LLM。

    v422（P0）改造：**默认不再写 macro 节点**。
    旧实现把每条链的 `cause` 原文当 `asset='all'` 的 core 节点（长句 + 事件标题），
    且 cron 每 6h 重跑一次就 version+1、confidence 取 max 顶到 1.0。实测攒出
    160 个 `all` 节点（92 个"核心驱动"全是新闻标题）、229 条出边，把 ALL 视图变成标题墙。

    现在的两种模式：
      · macro_mode="off"（默认）—— 只保证链里提到的资产有 asset 锚点，不写任何 `all` 节点；
      · macro_mode="atomic" —— 仅接受 ≤ macro_max_len 字、非事件标题、非列名的机制名，
        作为 `all` 下的 core。给后续「因子本体表」留口子，当前不启用。

    返回里 `nodes_added` 只统计真正写进图谱的 macro 节点；`assets_ensured` 是资产锚点数量。
    """
    conn = db.get_connection()
    chains_processed = 0
    assets_ensured = 0
    nodes_added = 0
    edges_added = 0
    skipped_macro = 0
    skipped_samples: List[str] = []
    by_asset: Dict[str, int] = {}

    atomic = str(macro_mode or "off").lower() == "atomic"

    try:
        # 取所有 enabled + weight >= min_weight 的链
        params: list = []
        sql = ("SELECT id, name, cause, effect, weight FROM causal_chains "
               "WHERE enabled=1")
        if min_weight:
            sql += " AND weight >= ?"
            params.append(min_weight)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        chains = conn.execute(sql, tuple(params)).fetchall()

        for cid, name, cause, effect, weight in chains:
            chains_processed += 1
            if not cause or not effect:
                continue

            # 从 effect 里提取资产 token (走白名单)
            asset_tokens: List[str] = []
            for tok in re.findall(r"\b[A-Z]{2,5}\b", str(effect)):
                tok_u = _norm_asset(tok)
                if tok_u in _VALID_ASSET_WHITELIST:
                    asset_tokens.append(tok_u)
            # 去重保持顺序
            seen: set[str] = set()
            asset_tokens = [a for a in asset_tokens if a not in seen and not seen.add(a)]

            # 如果指定了 asset 过滤,跳过不在范围内的链
            if asset and asset not in asset_tokens:
                continue

            if not asset_tokens:
                continue

            # 1) 资产锚点：链里出现过的资产必须在图谱里有 kind='asset' 节点
            for a in asset_tokens:
                if _ensure_asset_node(conn, a):
                    assets_ensured += 1
                by_asset[a] = by_asset.get(a, 0) + 1

            # 2) macro 节点：默认完全不写（旧实现在这里污染了整个 all 桶）
            if not atomic:
                continue
            label = str(cause).strip()
            if not label:
                continue
            if len(label) > macro_max_len or _looks_like_event(label) or _is_schema_label(conn, label):
                skipped_macro += 1
                if len(skipped_samples) < 5:
                    skipped_samples.append(label[:48])
                continue
            cause_id = _upsert_node(conn, "all", label, "core", True,
                                    float(weight or 0.5),
                                    json.dumps([str(name)[:60] if name else ""],
                                               ensure_ascii=False))
            if cause_id:
                nodes_added += 1
                for a in asset_tokens:
                    aid = _ensure_asset_node(conn, a)
                    if not aid:
                        continue
                    if not conn.execute(
                        "SELECT 1 FROM asset_driver_edges "
                        "WHERE from_id=? AND to_id=? AND kind=?",
                        (cause_id, aid, "causes"),
                    ).fetchone():
                        edges_added += 1
                    _upsert_edge(conn, cause_id, aid, "causes",
                                 weight=float(weight or 0.5),
                                 evidence=f"[seed from chain#{cid}] {name or ''}"[:120])

        conn.commit()
        return {
            "ok": True,
            "macro_mode": "atomic" if atomic else "off",
            "chains_processed": chains_processed,
            "assets_ensured": assets_ensured,
            "nodes_added": nodes_added,
            "edges_added": edges_added,
            "skipped_macro": skipped_macro,
            "skipped_samples": skipped_samples,
            "by_asset": by_asset,
        }
    finally:
        conn.close()


def _reject_factor_name(name: str) -> Optional[str]:
    """因子名入库校验（v424）。返回拒绝原因，None = 通过。

    LLM 常把"一句话论点"当因子名交上来（"美国实际利率(通胀预期与名义利率之差)
    决定美债无风险收益率的贴现定价"），这种名字既没法核验也没法归并，必须拦在门口。
    """
    s = str(name or "").strip()
    if not s:
        return "空名"
    if len(s) > _MAX_FACTOR_NAME_LEN:
        return "名过长(>%d字)" % _MAX_FACTOR_NAME_LEN
    if _has_schema_fragment(s):
        return "列名碎片"
    if any(w in s for w in _SELF_META_WORDS):
        return "系统自身动作"
    # 同义反复：用"新闻/方向/入场/胜率"解释涨跌，等于没解释
    for w in ("新闻驱动", "方向跟随", "方向取决于", "二轮入场", "首次入场",
              "胜率提升", "胜率显著"):
        if w in s:
            return "同义反复"
    return None


def upsert_knowledge(asset: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """把推理结果并入图谱：同义合并 + 建因果边 + 新旧互推（reconcile）。"""
    asset = _norm_asset(asset)
    if not asset:
        return {"asset": asset, "core_factors": 0, "condition_factors": 0,
                "error": "asset 为空"}
    conn = db.get_connection()
    try:
        asset_id = _upsert_node(conn, asset, asset, "asset", False, 1.0, "")
        core_labels: List[str] = []
        cond_labels: List[str] = []
        cond_factor_count = 0
        rejected: List[Dict[str, str]] = []

        new_cores = result.get("core_factors") or []
        for item in new_cores:
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "").strip()
            if not factor:
                continue
            why_bad = _reject_factor_name(factor)
            if why_bad:
                rejected.append({"factor": factor[:40], "reason": why_bad})
                continue
            chain = item.get("why_chain") or []
            if isinstance(chain, str):
                try:
                    chain = json.loads(chain)
                except json.JSONDecodeError:
                    chain = [chain]
            chain = [_clean_chain_item(x) for x in chain if str(x).strip()]
            chain = [x for x in chain if x]
            conf = _clamp(item.get("confidence"))
            # v424: LLM 标注的方向（该因素走强时本品种涨/跌），核验的预期端
            expect = _norm_expect(item.get("expect"))
            core_labels.append(_norm_label(factor))
            core_id = _upsert_node(conn, asset, factor, "core", True, conf,
                                   json.dumps(chain, ensure_ascii=False),
                                   expect=expect)
            # 链上非因素层 → chain 节点；因果方向：最底层因 → … → 现象层 → 资产
            middle: List[int] = []
            for layer in chain:
                if _norm_label(layer) == _norm_label(factor):
                    continue
                cond_labels.append(_norm_label(layer))
                lid = _upsert_node(conn, asset, layer, "chain", False,
                                   max(conf - 0.1, 0.2), "")
                if lid:
                    middle.append(lid)
            if middle:
                if core_id:
                    _upsert_edge(conn, core_id, middle[-1], "causes")
                for i in range(len(middle) - 1, 0, -1):
                    _upsert_edge(conn, middle[i], middle[i - 1], "causes")
                _upsert_edge(conn, middle[0], asset_id, "causes")
            else:
                _upsert_edge(conn, core_id, asset_id, "causes")

        new_conds = result.get("condition_factors") or []
        for item in new_conds:
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "").strip()
            if not factor:
                continue
            why_bad = _reject_factor_name(factor)
            if why_bad:
                rejected.append({"factor": factor[:40], "reason": why_bad})
                continue
            chain = item.get("why_chain") or []
            if isinstance(chain, str):
                try:
                    chain = json.loads(chain)
                except json.JSONDecodeError:
                    chain = [chain]
            chain = [_clean_chain_item(x) for x in chain if str(x).strip()]
            chain = [x for x in chain if x]
            conf = _clamp(item.get("confidence"))
            expect = _norm_expect(item.get("expect"))
            cond_labels.append(_norm_label(factor))
            cond_factor_count += 1
            cid = _upsert_node(conn, asset, factor, "condition", False, conf,
                               json.dumps(chain, ensure_ascii=False),
                               expect=expect)
            if cid:
                _upsert_edge(conn, cid, asset_id, "conditions", weight=max(conf, 0.3))
            for layer in chain:
                nl = _norm_label(layer)
                if nl == _norm_label(factor):
                    continue
                cond_labels.append(nl)
                lid = _upsert_node(conn, asset, layer, "chain", False,
                                   max(conf - 0.05, 0.15), "")
                if lid and cid:
                    _upsert_edge(conn, lid, cid, "conditions", weight=max(conf, 0.3))

        _reconcile(conn, asset, core_labels, cond_labels)
        conn.commit()
        return {"asset": asset, "core_factors": len(core_labels),
                "condition_factors": cond_factor_count,
                "rejected_factors": rejected}
    finally:
        conn.close()


def _reconcile(conn: sqlite3.Connection, asset: str,
               core_labels: List[str], cond_labels: List[str]) -> None:
    """新旧结论互相推理：本次未再出现的旧结论衰减，低于阈值标记失效。

    衰减保护：version < _RECONCILE_PROTECT_VERSION 的节点（即近期才被加入、尚在
    "成长期"的节点）本轮不衰减。这样可避免：
      1) 用户连点几次「重新推理」就把刚生成的节点降到 active=0；
      2) 衰减过快导致图谱失忆（API 层 get_knowledge 看不到任何结论）。

    衰减规则（保护期之外）：
      - core 节点：confidence *= 0.7；< 0.2 时 active=0
      - condition / chain：confidence *= 0.85；< 0.1 时 active=0

    v200+ Q5 自适应:
      - 保护期 → config.adaptive_protect_version(sample_n) 取代固定 3
      - 衰减倍数 → config.adaptive_decay_factor(sample_n) 取代固定 0.7 / 0.85
      - 样本稀疏 (sample_n < 10) → 长保护 + 慢衰减（防止冷启动误杀）
      - 样本充足 (sample_n >= 10) → 短保护 + 快衰减（数据多了就严格）
    """
    import config # v200+ Q5: 自适应三件套 — 本地 import 避免循环
    now = _now()
    # 估算该品种样本数: 用 active core+cond 节点数 + active chain 节点数 / 3
    # (1 个 sample 大约对应 3 个 chain 节点, 这是经验值).
    try:
        _sample_n = conn.execute(
            "SELECT COUNT(*) FROM asset_driver_nodes "
            "WHERE asset=? AND active=1", (asset,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        _sample_n = 0
    _protect_v = config.adaptive_protect_version(_sample_n)
    _core_decay, _cond_decay = config.adaptive_decay_factor(_sample_n)

    old_cores = conn.execute(
        "SELECT id, label, confidence, version FROM asset_driver_nodes "
        "WHERE asset=? AND is_core=1 AND active=1",
        (asset,),
    ).fetchall()
    for nid, label, conf, ver in old_cores:
        if _norm_label(label) in core_labels:
            continue  # 已由 _upsert_node 上调置信度/版本
        # v423: 慢变量（实际利率/通胀/增长/美元流动性/央行储备…）低频但真实，
        # 不该因为"本轮 5-why 没再提"就被衰减掉（实测 XAU 3 条真核因被降到 0.2 后失效）。
        _slow = factor_ontology.is_slow(factor_ontology.map_label(label))
        _protect_eff = _protect_v * 2 + 1 if _slow else _protect_v
        _decay_eff = min(0.92, _core_decay + 0.15) if _slow else _core_decay
        _floor = 0.1 if _slow else 0.2
        if int(ver or 0) < _protect_eff:
            continue  # 成长期保护 (自适应 + 慢变量加倍)
        new_conf = max(float(conf) * _decay_eff, 0.0)
        if new_conf < _floor:
            conn.execute("UPDATE asset_driver_nodes SET confidence=?, active=0, updated_at=? WHERE id=?",
                         (new_conf, now, nid))
        else:
            conn.execute("UPDATE asset_driver_nodes SET confidence=?, updated_at=? WHERE id=?",
                         (new_conf, now, nid))
    old_conds = conn.execute(
        "SELECT id, label, confidence, version FROM asset_driver_nodes "
        "WHERE asset=? AND is_core=0 AND kind IN ('condition','chain') AND active=1",
        (asset,),
    ).fetchall()
    for nid, label, conf, ver in old_conds:
        if _norm_label(label) in cond_labels:
            continue
        _slow = factor_ontology.is_slow(factor_ontology.map_label(label))
        _protect_eff = _protect_v * 2 + 1 if _slow else _protect_v
        _decay_eff = min(0.95, _cond_decay + 0.1) if _slow else _cond_decay
        if int(ver or 0) < _protect_eff:
            continue
        new_conf = max(float(conf) * _decay_eff, 0.0)
        if new_conf < 0.1:
            conn.execute("UPDATE asset_driver_nodes SET confidence=?, active=0, updated_at=? WHERE id=?",
                         (new_conf, now, nid))
        else:
            conn.execute("UPDATE asset_driver_nodes SET confidence=?, updated_at=? WHERE id=?",
                         (new_conf, now, nid))


def run_reasoning(assets: Optional[List[str]] = None,
                  max_workers: int = 4) -> Dict[str, Any]:
    """主入口：对指定品种（默认全部历史品种）跑一轮 5-why 推理 + 并入图谱。

    LLM 调用是瓶颈（每品种 ~2-10s），N 个品种串行太慢。这里拆成两步：
      1) 并发调 LLM（线程池，max_workers 控制并发）；
      2) 拿到结果后**串行**写 SQLite（sqlite3 Connection 非线程安全）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if assets is None:
        assets = known_assets()
    else:
        assets = [_norm_asset(a) for a in assets if _norm_asset(a)]

    # 第一步：并发收集 LLM 结果
    results: Dict[str, Optional[Dict[str, Any]]] = {}

    def _one(asset: str) -> tuple:
        return asset, reason_asset_drivers(asset)

    workers = max(1, min(max_workers, len(assets) or 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_one, a) for a in assets if a]
        for fut in as_completed(futures):
            asset, result = fut.result()
            results[asset] = result

    # 第二步：串行写 DB
    reasoned: Dict[str, Any] = {}
    for a in assets:
        if not a:
            continue
        result = results.get(a)
        if result is None:
            reasoned[a] = {"ok": False, "error": "无候选因素或 LLM 不可用"}
            continue
        try:
            upserted = upsert_knowledge(a, result)
            # 写入新假设后立即用近期事实核验（事件驱动闭环）
            try:
                verification = verify_all_pending(a)
                upserted["verification"] = verification
            except Exception as ve:
                upserted["verification_error"] = str(ve)
            upserted["ok"] = True
            reasoned[a] = upserted
        except Exception as e:
            reasoned[a] = {"ok": False, "error": f"upsert 失败: {e}"}
    return {"reasoned": list(reasoned.keys()), "detail": reasoned}


# ==============================================================================
# === 假设核验闭环（verify_hypothesis / verify_all_pending / _extract_facts）==
# ==============================================================================
#
# 设计：把"假设是否成立"从机械 confidence 衰减升级为事件驱动的逻辑核验。
# 入口：
#   - verify_hypothesis(node_id, facts): 核验单个节点
#   - verify_all_pending(asset, since_ts): 批量核验某品种所有 active 节点
# 事实来源：
#   - ai_decisions（交易决策表的资产/动作/收益，反向验证假设）
#   - factor_snapshots（指标快照，可与 chain 中的指标对应对齐）
#   - market_data（手动或外部接入）
# ==============================================================================


def _extract_facts(asset: str, since_ts: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 ai_decisions + factor_snapshots 抽取事实（统一格式）。

    返回 [{"asset", "timestamp", "indicator", "direction", "magnitude", "source"}, ...]

    ai_decisions 的解读：
      - is_correct=1 且 pnl>0 → 支持该品种的"看对"假设
      - is_correct=0 或 pnl<0 → 反驳（前提是该决策原本基于图中假设）
    factor_snapshots 的解读：
      - 数值变化方向直接映射 direction（涨/跌）
    """
    asset = _norm_asset(asset)
    if not asset:
        return []
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    facts: List[Dict[str, Any]] = []
    try:
        # 规范化 since_ts：DB 列存的是 Unix int 字符串 ("1791191685")；
        # 调用方常传 ISO 字符串 ("2026-10-05T...")，直接词典序比较永远 false。
        # 这里把 ISO 转成 int 再传下去；让所有调用方都能用 ISO。
        since_norm: Optional[str] = None
        if since_ts:
            try:
                if isinstance(since_ts, str) and ("T" in since_ts or "-" in since_ts):
                    since_norm = str(int(datetime.fromisoformat(since_ts).timestamp()))
                else:
                    since_norm = str(int(float(since_ts)))
            except Exception:
                since_norm = since_ts  # fallback 原文

        # 来源 1: ai_decisions + quick_sim_trades（用 suggested_action + pnl_pct）
        # P1: 结算字段改读 quick_sim_trades（结算唯一权威）；决策上下文仍来自 ai_decisions。
        try:
            params: List[Any] = [asset]
            sql = ("SELECT ad.id, qs.exit_ts AS created_at, ad.suggested_action, qs.pnl_pct AS forward_pnl "
                   "FROM ai_decisions ad "
                   "JOIN quick_sim_trades qs ON qs.decision_id = ad.id "
                   "WHERE ad.target_asset=? AND qs.settled = 1")
            if since_norm:
                sql += " AND qs.exit_ts >= ?"
                params.append(since_norm)
            sql += " ORDER BY qs.exit_ts DESC LIMIT 200"
            for r in conn.execute(sql, params).fetchall():
                pnl = float(r["forward_pnl"] or 0.0)
                facts.append({
                    "asset": asset,
                    "timestamp": str(r["created_at"] or ""),
                    "indicator": f"TradeOutcome:{r['suggested_action'] or 'HOLD'}",
                    "direction": "up" if pnl > 0 else ("down" if pnl < 0 else "neutral"),
                    "magnitude": abs(pnl),
                    "source": "ai_decisions",
                    "source_id": int(r["id"]),
                })
        except Exception:
            pass  # 表可能没建或 schema 差异，静默跳过

        # 来源 2: factor_snapshots
        # v276+ fix: schema 是 (id, news_id, asset, ts, final_score, confidence,
        # raw_action, gated_action, p_value, ...),不是 (captured_at, factor_name, value, prev_value)。
        # 旧代码用错列名导致整段 try 静默失败,9 个非 BTC 资产一直 0 facts。
        # 修正: 用 final_score delta vs 同资产上一条 gated_action 推导 direction;
        # indicator 用 gated_action (BUY/SELL/HOLD) + final_score 数值。
        try:
            params2: List[Any] = [asset]
            sql2 = ("SELECT id, ts, final_score, gated_action "
                    "FROM factor_snapshots WHERE asset=?")
            if since_norm:
                sql2 += " AND ts >= ?"
                params2.append(since_norm)
            sql2 += " ORDER BY ts DESC LIMIT 200"
            rows = conn.execute(sql2, params2).fetchall()
            # 同一 asset 取上一条 final_score 作为 prev
            for i, r in enumerate(rows):
                ts = int(r["ts"] or 0)
                score = float(r["final_score"] or 0.0)
                action = str(r["gated_action"] or "HOLD")
                # prev_score = 同 asset 在更早的快照 score
                prev_score = float(rows[i + 1]["final_score"]) if (i + 1) < len(rows) else score
                delta = score - prev_score
                # 优先用 delta 判定方向,gate_action 兜底
                if abs(delta) > 1e-6:
                    direction = "up" if delta > 0 else "down"
                else:
                    direction = {"BUY": "up", "SELL": "down"}.get(action.upper(), "neutral")
                indicator = f"factor:{action}:{round(score, 3)}"
                facts.append({
                    "asset": asset,
                    "timestamp": str(ts),
                    "indicator": indicator,
                    "direction": direction,
                    "magnitude": abs(delta) if abs(delta) > 1e-6 else 1.0,
                    "source": "factor_snapshots",
                    "source_id": int(r["id"]),
                })
        except Exception:
            pass

        # 来源 3: external_indicators (v225 Stage 5)
        # 让 worldview 的 regime_tags 可被 Yahoo Finance 等外部指标覆盖；
        # 用 LAG() 在 SQL 里算 prev_value（外部表无 prev 列）。
        try:
            params3: List[Any] = []
            sql3 = (
                "SELECT id, ts, indicator, value, regime_tags, asset, "
                "LAG(value) OVER (PARTITION BY indicator ORDER BY ts) AS prev_value "
                "FROM external_indicators WHERE 1=1"
            )
            if asset:
                sql3 += " AND (asset=? OR asset='' OR asset='*')"
                params3.append(asset)
            if since_norm:
                sql3 += " AND ts >= ?"
                params3.append(since_norm)
            sql3 += " ORDER BY ts DESC LIMIT 500"
            try:
                _check = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='external_indicators'"
                ).fetchone()
            except Exception:
                _check = None
            if _check is not None:
                for r in conn.execute(sql3, params3).fetchall():
                    try:
                        v = float(r["value"]) if r["value"] is not None else 0.0
                        pv_raw = r["prev_value"]
                        pv = float(pv_raw) if pv_raw is not None else v
                    except (TypeError, ValueError):
                        continue
                    delta = v - pv
                    try:
                        rt_list = json.loads(r["regime_tags"] or "[]")
                    except Exception:
                        rt_list = []
                    facts.append({
                        "asset": asset,
                        "timestamp": str(r["ts"] or ""),
                        "indicator": str(r["indicator"] or ""),
                        "direction": "up" if delta > 0 else ("down" if delta < 0 else "neutral"),
                        "magnitude": abs(delta),
                        "source": "external_indicators",
                        "source_id": int(r["id"]),
                        "regime_tags": rt_list,
                    })
        except Exception:
            pass
    finally:
        conn.close()
    return facts


def verify_hypothesis(node_id: int, facts: List[Dict[str, Any]],
                       conn: Optional[sqlite3.Connection] = None,
                       chain_index: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """核验单个假设节点（核心函数）。

    流程：
      1. 加载节点
      2. 解析 evidence → 取 chain
      3. 推断 chain 隐含方向（up/down/None）
      4. 对每个 fact：
         - chain 是否提到 fact.indicator（同义词匹配）
         - 是 → 比较 fact.direction vs chain_inferred_direction
         - 否 → neutral
      5. 累计 support / contradict
      6. 决定 status + fail + confidence
      7. 写回节点

    返回 {node_id, old_status, new_status, old_conf, new_conf, fail,
          support_count, contradict_count, neutral_count, matched_facts}

    v276+ fix: 接受可选 conn,批量调用(verify_all_pending)共享一个连接,
    避免 100 节点 × 100ms connection setup = 10s 开销。
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, asset, label, kind, is_core, confidence, evidence "
            "FROM asset_driver_nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"node_id": node_id, "error": "节点不存在"}

        ev = _parse_evidence(row["evidence"] or "")
        old_status = ev.get("_status") or _STATUS_UNCONFIRMED
        old_conf = float(row["confidence"] or 0.5)
        chain = ev.get("chain") or []
        chain_text = _chain_text(ev)
        # v424: 预期端 = 该机制成立时**本品种**应有的方向（不是驱动端方向）。
        # 显式元数据（LLM 标注 / 回填）优先，否则从链文本保守推断；推不出就是 None，
        # 此时不判支持也不判反驳，只记"无预期方向"。
        # v425: 区分来源 —— 只有 LLM 明确标注的才锁死；库里那些当年由代码推断出来的
        # （带 inferred 标记或压根没标记）每次重新推断，让推断规则的修正能真正生效
        # （实测 WTI「美联储利率路径」被锁成 up，把一条反向机制判反了）。
        inferred_expect = _infer_expect_direction(
            chain, label=str(row["label"] or ""), asset=str(row["asset"] or ""))
        if str(ev.get("_expect_src") or "inferred") == "llm" and ev.get("_expect"):
            expect = ev.get("_expect")
            expect_src = "llm"
        else:
            expect = inferred_expect or ev.get("_expect")
            expect_src = "inferred" if expect else None

        # 统计支持/反驳/中性
        # v276+ fix: TradeOutcome facts 的 indicator 是 "TradeOutcome:BUY", 但
        # chain 用中文描述(「BTC 上行」)永远不匹配。增加 source-aware 兜底:
        #   - ai_decisions (TradeOutcome): 节点资产与 fact.asset 一致即视为匹配
        #   - factor_snapshots / external_indicators: 先做 indicator 匹配,失败时
        #     若 factor_name 提到节点资产名(如「btc_price」+ node.asset=BTC)也视为匹配
        node_asset_lower = (row["asset"] or "").lower()
        support_ids: List[int] = []
        contradict_ids: List[int] = []
        neutral_ids: List[int] = []
        for fact in facts:
            indicator = str(fact.get("indicator") or "")
            sid = fact.get("source_id")
            fact_src = fact.get("source") or ""
            fdir = fact.get("direction")

            # 判定是否相关(能进入 direction 比较)
            relevant = False
            if fact_src == "ai_decisions":
                # TradeOutcome 兜底:节点资产 == fact.asset
                relevant = (str(fact.get("asset") or "").upper() == str(row["asset"] or "").upper())
            elif _chain_contains_indicator(chain_text, indicator):
                relevant = True
            elif node_asset_lower and node_asset_lower in indicator.lower():
                # factor_name 含节点资产 (例:btc_price + asset=BTC)
                relevant = True
            if not relevant:
                neutral_ids.append(sid)
                continue
            if expect is None:
                neutral_ids.append(sid)
                continue
            if fdir == "neutral":
                neutral_ids.append(sid)
                continue
            if fdir == expect:
                support_ids.append(sid)
            else:
                contradict_ids.append(sid)

        # 决定 status —— v424 计分制：看支持率，样本不足不下结论
        # v425：如果这个节点能挂到链库上有样本的链，**条件样本优先**——
        # 交易事实池里的样本与"机制是否触发"无关（每笔交易都算），命中率天然趋近
        # 50%，永远判不出结论；链库 hit/miss 只在链被触发时累计，才是有信息的样本。
        n_sup = len(support_ids)
        n_con = len(contradict_ids)
        total = n_sup + n_con
        # v426：预期方向未知 → 不聚合条件样本（UP/DOWN 混算等于让反向机制
        # 借正向机制的命中给自己的"已验证"背书，这正是 #565 假验证的来源）
        if expect in ("up", "down"):
            cond: Dict[str, Any] = _match_chain_samples(
                chain_index, str(row["asset"] or ""),
                _chain_text(ev) + " " + str(row["label"] or ""),
                expect=expect,
            )
        else:
            cond = {"n": 0, "hits": 0, "ratio": 0.0, "chain_ids": [], "matched": []}
        cond_n = int(cond.get("n") or 0)
        # v432：条件样本够看时，顺手做时间对半切 —— 后一半就是"样本外"
        if cond_n >= _MIN_VERIFY_SAMPLES and expect in ("up", "down"):
            cond.update(_cond_oos_split(conn, cond.get("chain_ids") or [], expect))
        verdict_source = "trade_pool"
        # v426：预期方向都推不出来的节点，条件样本无法归属方向（UP/DOWN 混算
        # 等于把反向机制的战绩借给正向机制），此时不用条件样本判结论。
        if cond_n >= _MIN_VERIFY_SAMPLES and expect in ("up", "down"):
            verdict_source = "chain_conditional"
            cond_ratio = float(cond.get("hits") or 0) / cond_n
            # v426：扣掉同期方向漂移。例：XAU 近 30 天 64% 的 5 日窗口是下跌，
            # 一个"猜跌"的机制即使 64% 命中也只是等于瞎猜 —— 不扣基准，
            # 越到单边行情，看跌机制就越是"自动验证通过"。
            base_rate = None
            if expect in ("up", "down"):
                p_up = _direction_base_rate(conn, str(row["asset"] or ""))
                if p_up is not None:
                    base_rate = p_up if expect == "up" else round(1.0 - p_up, 3)
            edge = None
            if base_rate is not None:
                edge = round(cond_ratio - base_rate, 3)
                cond["base_rate"] = base_rate
                cond["edge"] = edge
            # v426：判定改成"净增益"，不再用绝对命中率。
            # 单边行情里 60% 的绝对命中可能等于零本事（XAU 5 日窗口 69% 下跌），
            # 而反向行情里 55% 的命中反而是明显本事（XAU 地缘条款 18/33 猜涨、
            # 同期只有 31% 的窗口上涨，净增益 +0.23）。
            if edge is not None:
                if edge >= _EDGE_CONFIRM:
                    new_status = _STATUS_CONFIRMED
                elif edge <= -_EDGE_REFUTE:
                    new_status = _STATUS_CONTRADICTED
                else:
                    new_status = _STATUS_UNCONFIRMED
            elif cond_ratio >= _CONFIRM_RATIO:
                new_status = _STATUS_CONFIRMED   # 缺基准数据时退回旧口径
            elif cond_ratio <= _REFUTE_RATIO:
                new_status = _STATUS_CONTRADICTED
            else:
                new_status = _STATUS_UNCONFIRMED
            total = cond_n
            n_sup = int(cond.get("hits") or 0)
            n_con = cond_n - n_sup
        elif total < _MIN_VERIFY_SAMPLES:
            new_status = _STATUS_UNCONFIRMED
        else:
            ratio = n_sup / total
            if ratio >= _CONFIRM_RATIO and n_sup >= _MIN_VERIFY_SAMPLES:
                new_status = _STATUS_CONFIRMED
            elif ratio <= _REFUTE_RATIO:
                new_status = _STATUS_CONTRADICTED
            else:
                # 中间地带（0.4 < 支持率 < 0.6）= 证据互相打架，明确记"未确认"
                new_status = _STATUS_UNCONFIRMED

        # 决定 fail 诊断（仅 CONTRADICTED 时）
        fail: Optional[str] = None
        if new_status == _STATUS_CONTRADICTED:
            if not n_sup and n_con:
                fail = _FAIL_WRONG_HYPOTHESIS
            elif n_sup and n_con:
                fail = _FAIL_WRONG_INFERENCE
            else:
                fail = _FAIL_MISSING_CONTEXT

        # 计算 confidence —— 用 Wilson 下界，小样本不吹牛
        if new_status == _STATUS_CONFIRMED:
            new_conf = min(1.0, _CONF_FLOOR + _CONF_FLOOR * _wilson_lower(n_sup, total))
        elif new_status == _STATUS_CONTRADICTED:
            new_conf = max(0.0, _CONF_FLOOR * (1.0 - _wilson_lower(n_con, total)))
        else:
            new_conf = _CONF_FLOOR

        # 写回：合并元数据到现有 evidence JSON
        merged_fact_ids = sorted(set((ev.get("_fact_ids") or []) +
                                     [x for x in (support_ids + contradict_ids) if isinstance(x, int)]))
        merged_fact_ids = merged_fact_ids[-50:]

        new_evidence = _serialize_evidence(
            chain=chain,
            status=new_status,
            fail=fail,
            position=ev.get("_position"),
            fact_ids=merged_fact_ids,
            verified_at=_now(),
            expect=expect,
            expect_src=expect_src,
            cond=cond,
            source=ev.get("_source"),
            tier=ev.get("_tier"),
        )
        now = _now()
        # v424: 不再"一被否证就下线"。只有样本够多（≥12）且支持率确实很低才下线，
        # 否则保留在图里当"反例"，让用户看到"这条被事实打脸过"，而不是静默消失。
        # v425: 只有交易池判出来的否证才允许下线；条件样本是链级聚合（几条链撑起
        # 一个样本量），拿它把一个节点从图里删掉太激进，只标记不下线。
        new_active = 1
        if (verdict_source == "trade_pool" and new_status == _STATUS_CONTRADICTED
                and total >= _HARD_REFUTE_N and n_sup / total <= _REFUTE_RATIO):
            new_active = 0
        conn.execute(
            "UPDATE asset_driver_nodes SET confidence=?, evidence=?, active=?, updated_at=? "
            "WHERE id=?",
            (new_conf, _cap_evidence(new_evidence), new_active, now, node_id),
        )
        # v276+ fix: 共享 conn 时不要在这里 commit,让调用方(verify_all_pending)
        # 一次性 commit,避免每节点 fsync = N×100ms → 1×100ms
        if own_conn:
            conn.commit()

        return {
            "node_id": node_id,
            "asset": row["asset"],
            "label": row["label"],
            "kind": row["kind"],
            "old_status": old_status,
            "new_status": new_status,
            "old_conf": round(old_conf, 3),
            "new_conf": round(new_conf, 3),
            "fail": fail,
            "expect": expect,
            "support_count": n_sup,
            "contradict_count": n_con,
            "neutral_count": len(neutral_ids),
            "matched_facts": total,
            "support_ratio": round(n_sup / total, 3) if total else None,
            "active": new_active,
            # v425：这条结论是"链条件样本"判的还是"交易事实池"判的，必须能追溯
            "verdict_source": verdict_source,
            "cond_n": cond_n,
            "cond_hits": int(cond.get("hits") or 0),
            "cond_ratio": cond.get("ratio"),
            "cond_chains": cond.get("matched", []),
        }
    finally:
        if own_conn:
            conn.close()


def verify_all_pending(asset: str, since_ts: Optional[str] = None,
                       facts: Optional[List[Dict[str, Any]]] = None,
                       limit: int = 100) -> Dict[str, Any]:
    """批量核验某品种所有 active=1 节点。

    流程：
      1. 抽 facts（一次性，避免重复查询）
      2. 加载所有 active 节点
      3. 对每个节点分类位置（cause/middle/effect），先写一次 _position 元数据
      4. 调 verify_hypothesis 逐个核验

    返回 {asset, fact_count, verified_count, results: [<dict>, ...]}
    """
    asset = _norm_asset(asset)
    if not asset:
        return {"asset": asset, "fact_count": 0, "verified_count": 0, "results": [],
                "error": "asset 为空"}

    if facts is None:
        facts = _extract_facts(asset, since_ts)

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        # v424: asset / evidence 是图谱锚点，不是"假设"——核验它们会把自己的
        # confidence 从 1.0 打回 0.5，而且永远"未确认"，纯属自伤。跳过。
        nodes = conn.execute(
            "SELECT id, asset, label, kind, is_core, evidence FROM asset_driver_nodes "
            "WHERE asset=? AND active=1 AND kind NOT IN ('asset','evidence') "
            "ORDER BY is_core DESC, kind LIMIT ?",
            (asset, limit),
        ).fetchall()

        # 预统计出入度（用于 _classify_position）
        node_ids = [int(n["id"]) for n in nodes]
        in_degree: Dict[int, int] = {nid: 0 for nid in node_ids}
        out_degree: Dict[int, int] = {nid: 0 for nid in node_ids}
        if node_ids:
            ph = ",".join("?" * len(node_ids))
            for r in conn.execute(
                f"SELECT from_id, to_id FROM asset_driver_edges "
                f"WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                node_ids + node_ids,
            ).fetchall():
                fid, tid = int(r[0]), int(r[1])
                if fid in out_degree:
                    out_degree[fid] += 1
                if fid in in_degree:
                    in_degree[fid] += 1
                if tid in out_degree:
                    out_degree[tid] += 1
                if tid in in_degree:
                    in_degree[tid] += 1

        results: List[Dict[str, Any]] = []
        # v425：链库索引一次性构建（含二元组预计算），供每个节点做条件样本匹配
        try:
            chain_index = _build_chain_index(conn)
        except Exception:
            chain_index = []
        for n in nodes:
            nid = int(n["id"])
            ev = _parse_evidence(n["evidence"] or "")
            position = _classify_position(
                node_kind=str(n["kind"] or ""),
                is_core=bool(int(n["is_core"] or 0)),
                outgoing_count=out_degree.get(nid, 0),
                incoming_count=in_degree.get(nid, 0),
            )

            # 先写 _position（如果变了），再核验
            if ev.get("_position") != position:
                new_ev_str = _serialize_evidence(
                    chain=ev.get("chain") or [],
                    status=ev.get("_status") or _STATUS_UNCONFIRMED,
                    fail=ev.get("_fail"),
                    position=position,
                    fact_ids=ev.get("_fact_ids") or [],
                    verified_at=ev.get("_verified_at"),
                    # v425 修复：这里原来不传 expect，一改位置就把 _expect 抹掉，
                    # 下一轮核验只能重新猜方向。顺带把条件样本刻度也带上。
                    expect=ev.get("_expect"),
                    expect_src=ev.get("_expect_src"),
                    source=ev.get("_source"),
                    cond={"n": ev.get("_cond_n") or 0,
                          "hits": ev.get("_cond_hits") or 0,
                          "ratio": ev.get("_cond_ratio") or 0.0,
                          "chain_ids": ev.get("_cond_chains") or []},
                )
                conn.execute(
                    "UPDATE asset_driver_nodes SET evidence=?, updated_at=? WHERE id=?",
                    (_cap_evidence(new_ev_str), _now(), nid),
                )

            try:
                r = verify_hypothesis(nid, facts, conn=conn, chain_index=chain_index)
                results.append(r)
            except Exception as e:
                results.append({"node_id": nid, "error": str(e)})
            # v437 P0-0b：每 20 个节点提交一次。
            # 事故（2026-11-17 21:31 长事务哨兵）：cron_persist_evidence →
            # verify_all_pending 一次事务持写锁 39.2s，引擎/桥接全部撞
            # "database is locked"，主分析积压到 656 条 PENDING。
            # 循环里每个节点都在写 UPDATE，全攒到最后 commit = 把整轮核验
            # 变成一个大写事务。改成分段提交，单事务回到亚秒级。
            if len(results) % 20 == 0:
                try:
                    conn.commit()
                except Exception:
                    pass
        conn.commit()
        return {
            "asset": asset,
            "fact_count": len(facts),
            "verified_count": len(results),
            "results": results,
        }
    finally:
        conn.close()


# ============================================================================
# v276+: 反例持久化 — 把 verify 的 supports/contradicts 统计落成"证据边"
# ============================================================================
#
# 防御 confirmation bias 设计:
#   - 之前 verify_all_pending 只把节点 active=0(CONTRADICTED 时), 证伪数据丢失
#   - 现在每次 verify 后, 创建 2 类证据节点:
#     - "evidence_supports" 节点 (绿色, asset 自动归属): 每个核验支持的 fact
#     - "evidence_contradicts" 节点 (红色): 每个核验反驳的 fact
#   - 这样图谱里能看到 "XAU 上行" 节点有 5 条绿线 (5 个支持事实) +
#     2 条红线 (2 个反例) → 用户一眼能看出"不是单边信念"
#
# 性能: 一次 verify 完批量写入,不每次 verify_hypothesis 都写 (那太频繁)
# ============================================================================


_EVIDENCE_ANCHOR_PREFIX = "[证据]"


def _evidence_anchor_node(conn: sqlite3.Connection, asset: str) -> Optional[int]:
    """每个资产一个**稳定**的证据源节点（不再每次核验造新节点）。

    v422：旧实现 label 里带时间戳 `ev_7d_<now>_<nid>`，于是每次跑 persist 都新增
    节点，实测留下 462 个一次性节点、每个只有 1 条边，图被点淹没而信息没增加。
    """
    asset = _norm_asset(asset)
    if not asset:
        return None
    return _upsert_node(conn, asset, f"{_EVIDENCE_ANCHOR_PREFIX} {asset}",
                        "evidence", False, 0.5, "")


def persist_evidence_edges(asset: str,
                             results: Optional[List[Dict[str, Any]]] = None,
                             since_days: int = 7) -> Dict[str, Any]:
    """把 verify_all_pending 的结果写成 supports / contradicts 边。

    v422（P0）改造：
      · 不再为每次核验新建 `[SUPPORT] ev_7d_<ts>_<nid>` 节点；
      · 每个资产只有一个稳定证据源节点 `[证据] XAU`；
      · 边按 (锚点, 假设, kind) 唯一，weight = 本次核验的支持/反驳事实数
        （绝对值覆盖，不是 `_upsert_edge` 的累加，避免同一批事实被重复计票）；
      · 本次该假设没有支持（或没有反驳）→ 删掉对应旧边，不让去年的票留在图上。

    `since_days` 仅为兼容既有调用方签名保留，不再参与节点命名。
    """
    asset = _norm_asset(asset) if asset else None
    conn = db.get_connection()
    supports_added = 0
    contradicts_added = 0
    evidence_nodes_created = 0
    by_node: List[Dict[str, Any]] = []
    anchors: Dict[str, Optional[int]] = {}
    try:
        if results is None:
            if not asset:
                return {"ok": False, "error": "asset 必填 (或传 results)"}
            r = verify_all_pending(asset)
            results = r.get("results") or []

        now = _now()
        for r in results:
            if not isinstance(r, dict):
                continue
            nid = r.get("node_id")
            if not nid:
                continue
            sup = int(r.get("support_count") or 0)
            con = int(r.get("contradict_count") or 0)
            if sup == 0 and con == 0:
                continue

            # 节点自己的归属资产
            node_asset_row = conn.execute(
                "SELECT asset FROM asset_driver_nodes WHERE id=?", (nid,)
            ).fetchone()
            node_asset = (node_asset_row[0] if node_asset_row else "") or asset or "all"
            node_asset = _norm_asset(node_asset) or "all"

            if node_asset not in anchors:
                anchors[node_asset] = _evidence_anchor_node(conn, node_asset)
                if anchors[node_asset]:
                    evidence_nodes_created += 1
            out_id = anchors[node_asset]
            if not out_id:
                continue

            for kind, cnt, label in (("supports", sup, "支持"),
                                      ("contradicts", con, "反驳")):
                if cnt > 0:
                    # 绝对值覆盖：本次核验的票数就是这条边的权重
                    conn.execute(
                        "INSERT INTO asset_driver_edges "
                        "(from_id, to_id, kind, weight, evidence, updated_at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(from_id,to_id,kind) DO UPDATE SET "
                        "  weight=excluded.weight, evidence=excluded.evidence, "
                        "  updated_at=excluded.updated_at",
                        (out_id, int(nid), kind, float(min(cnt, 10)),
                         f"verify {label} {cnt} 个事实", now),
                    )
                    if kind == "supports":
                        supports_added += 1
                    else:
                        contradicts_added += 1
                else:
                    conn.execute(
                        "DELETE FROM asset_driver_edges "
                        "WHERE from_id=? AND to_id=? AND kind=?",
                        (out_id, int(nid), kind),
                    )

            by_node.append({
                "node_id": nid,
                "supports": sup,
                "contradicts": con,
                "ratio": round(sup / max(sup + con, 1), 2),
            })

        conn.commit()
        return {
            "ok": True,
            "asset": asset,
            "supports_added": supports_added,
            "contradicts_added": contradicts_added,
            "evidence_nodes_created": evidence_nodes_created,
            "anchors": {k: v for k, v in anchors.items()},
            "by_node": by_node[:20],  # 截断防爆
        }
    finally:
        conn.close()


def update_cross_asset_weights(since_days: int = 7,
                              min_evidence: int = 50) -> Dict[str, Any]:
    """基于 abnormal_attributions 时间序列动态更新跨资产神经权重。

    v276+:
      之前 link_cross_asset_neurons() 用硬编码 base_weight (0.2~0.7),
      实际「BTC↔ETH 真相关多强?」是数据驱动问题。
      现在用 abnormal_attributions.cross_json(LLM 标的每条异动给的 top-3 同步资产)
      在 since_days 窗口内统计每对 (A,B) 共现次数 → 计算 weight,
      直接 UPDATE asset_driver_edges.weight。

    公式:
      raw_count = COUNT(*) WHERE abnormal.asset=A AND B IN cross_json
      weight = round(raw_count / since_days_signals_of_A * 10, 2)
      说明: 归一化到 [0, 10] (与 _upsert_edge 上限一致)
            100% 同向 → 10.0; 50% 同向 → 5.0

    只更新已有的 correlates_with 边(不新建)。
    没异常归因数据的对保持原 weight。

    入参:
      since_days: 统计窗口 (默认 7)
      min_evidence: 阈值,raw_count < min_evidence 跳过(防冷启动噪声)

    返回 {updated, by_pair: [{from,to,count,weight}]}
    """
    conn = db.get_connection()
    try:
        cutoff = int(time.time()) - since_days * 86400
        conn.row_factory = sqlite3.Row

        # 1) 每个 (from_asset, to_asset) 在窗口内的共现计数
        rows = conn.execute(
            "SELECT asset, cross_json "
            "FROM abnormal_attributions "
            "WHERE anchor_ts >= ? AND cross_json != '[]' AND cross_json IS NOT NULL",
            (cutoff,),
        ).fetchall()

        pair_count: Dict[tuple, int] = {}
        asset_total: Dict[str, int] = {}
        for r in rows:
            a = r["asset"]
            try:
                cross = json.loads(r["cross_json"]) if r["cross_json"] else []
            except (json.JSONDecodeError, TypeError):
                continue
            asset_total[a] = asset_total.get(a, 0) + 1
            for c in cross:
                # 仅统计白名单里的资产 (避免「风险资产整体」之类噪声 token)
                c_clean = _norm_asset(c) if c else ""
                if c_clean and c_clean in _VALID_ASSET_WHITELIST:
                    pair_count[(a, c_clean)] = pair_count.get((a, c_clean), 0) + 1

        # 2) 取出所有现有 correlates_with 边 (from_asset_id, to_asset_id)
        edges = conn.execute(
            "SELECT e.id, e.from_id, e.to_id, e.weight, "
            "       n1.asset AS from_asset, n2.asset AS to_asset "
            "FROM asset_driver_edges e "
            "JOIN asset_driver_nodes n1 ON e.from_id=n1.id "
            "JOIN asset_driver_nodes n2 ON e.to_id=n2.id "
            "WHERE e.kind='correlates_with' AND n1.kind='asset' AND n2.kind='asset'"
        ).fetchall()

        # 3) 对每条边计算新 weight 并 UPDATE
        updated = 0
        by_pair: List[Dict[str, Any]] = []
        now = _now()
        for e in edges:
            fa, ta = e["from_asset"], e["to_asset"]
            total = asset_total.get(fa, 0)
            cnt = pair_count.get((fa, ta), 0)
            if cnt < min_evidence or total == 0:
                # 数据不足 — 保留原 weight,记录供调试
                by_pair.append({"from": fa, "to": ta, "count": cnt,
                                "old_weight": round(e["weight"], 2),
                                "new_weight": round(e["weight"], 2),
                                "skipped": "min_evidence"})
                continue
            # 归一化到 [0, 10] — 共现率 * 10
            new_w = round(min(cnt / total * 10.0, 10.0), 2)
            conn.execute(
                "UPDATE asset_driver_edges SET weight=?, updated_at=? WHERE id=?",
                (new_w, now, e["id"]),
            )
            by_pair.append({
                "from": fa, "to": ta,
                "count": cnt,
                "from_total": total,
                "old_weight": round(e["weight"], 2),
                "new_weight": new_w,
                "delta": round(new_w - e["weight"], 2),
            })
            updated += 1
        conn.commit()
        # 按变化幅度排序输出
        by_pair.sort(key=lambda x: abs(x.get("delta", 0)), reverse=True)
        return {
            "ok": True,
            "since_days": since_days,
            "min_evidence": min_evidence,
            "updated": updated,
            "total_pair_signals": sum(pair_count.values()),
            "by_pair": by_pair,
        }
    finally:
        conn.close()


def _build_narrative(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把 active 节点的核验状态浓缩成「当前叙事坐实度」汇总（按 asset 分组）。

    叙事逻辑 = 每个驱动因素都要经过事实核验：已证实(坐实)/已证伪(否定)/待证实(存疑)。
    汇总给前端一句话叙事，替代「一堆节点」的认知负担。
    """
    agg: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        if n.get("kind") == "asset":
            continue
        asset = n.get("asset") or ""
        ev = _parse_evidence(n.get("evidence") or "")
        status = ev.get("_status") or _STATUS_UNCONFIRMED
        a = agg.setdefault(asset, {
            "total": 0, "confirmed": 0, "unconfirmed": 0, "contradicted": 0,
            "confirmed_factors": [], "contradicted_factors": [], "summary": "",
        })
        a["total"] += 1
        label = n.get("label") or ""
        if status == _STATUS_CONFIRMED:
            a["confirmed"] += 1
            a["confirmed_factors"].append(label)
        elif status == _STATUS_CONTRADICTED:
            a["contradicted"] += 1
            a["contradicted_factors"].append(label)
        else:
            a["unconfirmed"] += 1
    for a in agg.values():
        parts = []
        if a["confirmed"]:
            parts.append(f"{a['confirmed']} 个已证实")
        if a["contradicted"]:
            parts.append(f"{a['contradicted']} 个已证伪")
        if a["unconfirmed"]:
            parts.append(f"{a['unconfirmed']} 个待证实")
        a["summary"] = "，".join(parts) + f"（共 {a['total']} 个驱动）" if parts else "暂无驱动结论"
        a["confirmed_factors"] = a["confirmed_factors"][:3]
        a["contradicted_factors"] = a["contradicted_factors"][:3]
    return agg


def _wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """95% Wilson 下界 —— 小样本不能拿命中率当结论（3/3 不是 100%）。"""
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, hits / n))
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def _node_stats(conn: sqlite3.Connection, ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """节点的"刻度"：支持/反驳事实数 → n、命中率、Wilson 下界。

    v423: 这些数字原来只在链级（causal_chain_outcomes）有，节点输出只有 confidence
    和"未确认"；现在 support/contradicts 边是稳定的（每资产一个锚点），可以直接汇总。
    """
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT to_id, kind, weight FROM asset_driver_edges "
        f"WHERE to_id IN ({ph}) AND kind IN ('supports','contradicts')",
        ids,
    ).fetchall()
    agg: Dict[int, Dict[str, float]] = {}
    for tid, kind, w in rows:
        d = agg.setdefault(int(tid), {"sup": 0.0, "con": 0.0})
        d["sup" if kind == "supports" else "con"] += float(w or 0)
    out: Dict[int, Dict[str, Any]] = {}
    for nid, d in agg.items():
        n = int(round(d["sup"] + d["con"]))
        hits = int(round(d["sup"]))
        if n == 0:
            status = "无样本"
        else:
            wl = _wilson_lower(hits, n)
            if wl >= 0.55:
                status = "偏正"
            elif hits / n <= 0.45:
                status = "偏负"
            else:
                status = "未定"
            out[nid] = {"n": n, "hits": hits,
                        "hit_rate": round(hits / n, 3),
                        "wilson_low": round(wl, 3), "status": status}
    for nid in ids:
        out.setdefault(int(nid), {"n": 0, "hits": 0, "hit_rate": 0.0,
                                  "wilson_low": 0.0, "status": "无样本"})
    return out


def get_graph(asset: Optional[str] = None, include_inactive: bool = False,
              include_macro: bool = True) -> Dict[str, Any]:
    """返回图谱节点 + 边（供前端可视化）。

    v423: 每个节点带上 `factor_key` / `family` 与核验刻度（n/命中率/Wilson 下界）；
    指定 asset 时顺带把与本品种共享规范因子的 `MACRO` 节点 + `instance_of` 边带上，
    让"跨品种同源"在图上是连通的，而不是只靠 18 条硬编码 correlates。
    """
    if asset:
        asset = _norm_asset(asset)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if asset:
            where, params = "WHERE asset=?", [asset]
        elif include_inactive:
            where, params = "", []
        else:
            where, params = "WHERE active=1", []
        nodes = conn.execute(
            "SELECT id, asset, label, kind, is_core, confidence, evidence, version, active, "
            "updated_at, factor_key "
            f"FROM asset_driver_nodes {where} ORDER BY asset, is_core DESC, confidence DESC",
            params,
        ).fetchall()
        narr_nodes = [dict(n) for n in nodes]
        nids = [dict(n) for n in nodes]
        ids = [n["id"] for n in nids]
        edges: List[Dict[str, Any]] = []
        if ids:
            ph = ",".join("?" * len(ids))
            edges = [dict(e) for e in conn.execute(
                f"SELECT id, from_id, to_id, kind, weight, evidence, updated_at "
                f"FROM asset_driver_edges WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                ids + ids,
            ).fetchall()]

        # v423: 跨品种同源 —— 带上与本品种共享规范因子的 MACRO 节点
        if asset and include_macro and ids:
            ph = ",".join("?" * len(ids))
            inst = [dict(e) for e in conn.execute(
                f"SELECT id, from_id, to_id, kind, weight, evidence, updated_at "
                f"FROM asset_driver_edges WHERE kind='instance_of' AND from_id IN ({ph})",
                ids,
            ).fetchall()]
            extra_ids = [e["to_id"] for e in inst if e["to_id"] not in ids]
            if extra_ids:
                ph2 = ",".join("?" * len(extra_ids))
                extra_nodes = [dict(n) for n in conn.execute(
                    "SELECT id, asset, label, kind, is_core, confidence, evidence, version, active, "
                    "updated_at, factor_key FROM asset_driver_nodes WHERE id IN (%s)" % ph2,
                    extra_ids,
                ).fetchall()]
                nids += extra_nodes
                edges += inst

        stats = _node_stats(conn, [n["id"] for n in nids])
        for n in nids:
            n["family"] = factor_ontology.family(n.get("factor_key"))
            n.update(stats.get(int(n["id"]), {}))
        return {"nodes": nids, "edges": edges, "narrative": _build_narrative(narr_nodes)}
    finally:
        conn.close()


def _factor_priority(f: Dict[str, Any]) -> int:
    """因素可分发的优先级：0 已验证 → 1 有样本未定 → 2 样本不足 → 3 已否证。

    v424: 输出排序不再只看 confidence（旧实现里 0.5 的地板值让一堆"无结论"
    和"已否证"排在真正验证过的因素前面），改成先按证据质量分档。
    有样本的（哪怕结论是"未定"）排在"完全没样本"的后面会误导用户 ——
    证据多寡本身就是信息，所以 n≥5 的一律优先于 n=0。
    """
    if f.get("verified"):
        return 0
    if not f.get("usable"):
        return 3
    if int(f.get("n") or 0) >= _MIN_VERIFY_SAMPLES:
        return 1
    return 2


def get_knowledge(asset: str) -> Dict[str, Any]:
    """返回某品种的核心因素/条件因素 + 推理链（供展示 / 回灌）。

    v423: 每条因素补三样原来没有的东西 ——
      1) `factor_key` / `canonical` / `family`：它到底是哪个规范因子；
      2) `n / hits / hit_rate / wilson_low / sample_status`：核验刻度，不再只有"未确认"；
      3) 品种级 `cross_asset`：同一规范因子还被哪些品种在用（多跳/跨品种推理的入口）。
    核心 < 3 条时额外给 `chain_priors`（链库反推，明确标"无 LLM、未节点化"），不伪装成核心。
    """
    asset = _norm_asset(asset)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        cores = conn.execute(
            "SELECT id, label, confidence, evidence, version, factor_key FROM asset_driver_nodes "
            "WHERE asset=? AND is_core=1 AND active=1 ORDER BY confidence DESC",
            (asset,),
        ).fetchall()
        conds = conn.execute(
            "SELECT id, label, confidence, evidence, version, factor_key FROM asset_driver_nodes "
            "WHERE asset=? AND is_core=0 AND kind='condition' AND active=1 ORDER BY confidence DESC",
            (asset,),
        ).fetchall()
        stats = _node_stats(conn, [int(r["id"]) for r in list(cores) + list(conds)])

        def _fmt(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for r in rows:
                why: List[str] = []
                status, fail = None, None
                try:
                    raw = json.loads(r["evidence"] or "[]") if r["evidence"] else []
                except json.JSONDecodeError:
                    raw = []
                # v405：evidence 实际是 {"chain":[...], "_status": "...", "_fail": "..."}
                if isinstance(raw, dict):
                    for k in ("chain", "why", "evidence"):
                        v = raw.get(k)
                        if isinstance(v, (list, tuple)):
                            why += [str(x) for x in v]
                        elif v:
                            why.append(str(v))
                    status = raw.get("_status")
                    fail = raw.get("_fail")
                elif isinstance(raw, list):
                    why = [str(x) if not isinstance(x, dict) else str(x.get("chain") or x)
                           for x in raw]
                elif raw:
                    why = [str(raw)]
                fkey = r["factor_key"] if "factor_key" in r.keys() else None
                st = stats.get(int(r["id"]), {})
                # v424: 输出分层 —— 之前 get_knowledge 只按 confidence 排序，
                # 已否证的核心照样排在最前面、照样进 prompt（只加了一句"勿据此定方向"），
                # 等于把已知错误喂给 LLM。这里显式给 usable / verified 两个口径。
                verdict = (status or "UNCONFIRMED").upper()
                # v425：优先用"链条件样本"作为刻度（它只在链被触发时累计），
                # 没有条件样本再退回边统计（EdgeStats 口径）。
                cond_n = int(raw.get("_cond_n") or 0) if isinstance(raw, dict) else 0
                cond_hits = int(raw.get("_cond_hits") or 0) if isinstance(raw, dict) else 0
                cond_ratio = float(raw.get("_cond_ratio") or 0.0) if isinstance(raw, dict) else 0.0
                cond_base = raw.get("_cond_base_rate") if isinstance(raw, dict) else None
                cond_edge = raw.get("_cond_edge") if isinstance(raw, dict) else None
                # v432：样本外战绩（时间后一半）。样本内漂亮、样本外跌破基准的，
                # 不能算"已验证" —— 那只是把同一段行情背下来了。
                oos_n = int(raw.get("_cond_oos_n") or 0) if isinstance(raw, dict) else 0
                oos_hits = int(raw.get("_cond_oos_hits") or 0) if isinstance(raw, dict) else 0
                oos_ratio = raw.get("_cond_oos_ratio") if isinstance(raw, dict) else None
                is_ratio = raw.get("_cond_is_ratio") if isinstance(raw, dict) else None
                n_i = cond_n if cond_n >= _MIN_VERIFY_SAMPLES else int(st.get("n", 0))
                hits_i = cond_hits if cond_n >= _MIN_VERIFY_SAMPLES else int(st.get("hits", 0))
                if n_i:
                    hit_rate_i = round(hits_i / n_i, 3)
                    wl = round(_wilson_lower(hits_i, n_i), 3)
                else:
                    hit_rate_i = 0.0
                    wl = float(st.get("wilson_low", 0.0))
                refuted = verdict in ("CONTRADICTED", "REFUTED", "FAILED")
                verified = (not refuted) and verdict == "CONFIRMED"
                # v425：核心的"同族印证" —— 核心名是族级概念，链库里没有同名链，
                # 但它同族的机制节点是有条件样本的（如 rates 族的美联储鹰派链）。
                # 只有核心/条件节点做这一步，且只加分不否证（见 _family_cond_samples）。
                fam_cond = {"n": 0, "hits": 0, "ratio": 0.0, "mechanisms": [], "edge": None}
                if not refuted and cond_n < _MIN_VERIFY_SAMPLES and fkey:
                    try:
                        fam_cond = _family_cond_samples(conn, asset, fkey, int(r["id"]))
                    except sqlite3.Error:
                        fam_cond = {"n": 0, "hits": 0, "ratio": 0.0, "mechanisms": [],
                                    "edge": None}
                # v426：同族印证也要扣漂移基准，否则会跟机制层自相矛盾
                # （机制被判"跑输基准"，核心却凭它被标"已验证"）
                if fam_cond["n"] >= _MIN_VERIFY_SAMPLES:
                    expect_i = (raw.get("_expect") if isinstance(raw, dict) else None)
                    p_up = _direction_base_rate(conn, asset)
                    if expect_i in ("up", "down") and p_up is not None:
                        base_i = p_up if expect_i == "up" else round(1.0 - p_up, 3)
                        fam_cond["edge"] = round(float(fam_cond["ratio"]) - base_i, 3)
                        fam_cond["base"] = base_i
                fam_edge = fam_cond.get("edge")
                if (not refuted and not verified
                        and fam_cond["n"] >= _MIN_VERIFY_SAMPLES
                        and fam_edge is not None and fam_edge >= _EDGE_CONFIRM):
                    verdict = "SUPPORTED_BY_MECHANISMS"
                    verified = True
                # v432：已验证但样本外不稳 → 降级为"样本内观察"，不给 verified
                if verified and oos_n >= _MIN_VERIFY_SAMPLES and oos_ratio is not None:
                    _base = (1.0 - (cond_base or 0.0)) if expect_i == "down" else (cond_base or 0.5)
                    _base = cond_base if cond_base is not None else 0.5
                    if float(oos_ratio) - float(_base) < _EDGE_CONFIRM:
                        verdict = "CONFIRMED_UNSTABLE"
                        verified = False
                        usable_reason = ("样本内 %.2f / 样本外 %.2f（后一半未跑赢基准，"
                                         "不标已验证）" % (float(is_ratio or 0), float(oos_ratio)))
                if refuted:
                    usable_reason = "已否证"
                elif verified and verdict == "SUPPORTED_BY_MECHANISMS":
                    usable_reason = (f"同族机制印证 {fam_cond['hits']}/{fam_cond['n']}"
                                     + (f"，净增益 {fam_cond['edge']:+.2f}"
                                        if fam_cond.get("edge") is not None else ""))
                elif verified:
                    usable_reason = "已验证"
                elif fam_cond["n"] >= _MIN_VERIFY_SAMPLES:
                    usable_reason = (f"同族机制仅 {fam_cond['hits']}/{fam_cond['n']}"
                                     "，未达阈值")
                elif cond_n >= _MIN_VERIFY_SAMPLES:
                    # v426：把"扣掉漂移后还剩多少"写在理由里 —— 只报原始命中率
                    # 会让人误以为 64% 是真本事，而同期瞎猜就是 64%
                    if cond_edge is not None:
                        usable_reason = (f"条件样本 {cond_hits}/{cond_n}"
                                         f"（基准 {cond_base:.2f}，净增益 {cond_edge:+.2f}）")
                    else:
                        usable_reason = f"条件样本 {cond_hits}/{cond_n} 未达阈值"
                elif n_i == 0:
                    usable_reason = "无样本（结构位候选）"
                elif n_i < _MIN_VERIFY_SAMPLES:
                    usable_reason = "样本不足"
                else:
                    usable_reason = "命中率不足"
                out.append({
                    "node_id": int(r["id"]),
                    "factor": r["label"],
                    "confidence": round(float(r["confidence"]), 3),
                    "why_chain": why,
                    "version": int(r["version"]),
                    "evidence_status": status,
                    "evidence_fail": fail,
                    "factor_key": fkey,
                    "canonical": factor_ontology.canonical(fkey),
                    "family": factor_ontology.family(fkey),
                    "slow": factor_ontology.is_slow(fkey),
                    "structural_only": bool(
                        refuted and factor_ontology.is_slow(fkey)),
                    "n": n_i,
                    "hits": hits_i,
                    "hit_rate": hit_rate_i,
                    "wilson_low": wl,
                    "sample_status": ("条件样本" if cond_n >= _MIN_VERIFY_SAMPLES
                                      else st.get("status", "无样本")),
                    # v424 输出分层
                    "verdict": verdict,
                    "verified": bool(verified),
                    "usable": not refuted,
                    "usable_reason": usable_reason,
                    # v425：条件样本明细（哪几条链在支撑、各自 hit/miss）
                    "cond_n": cond_n,
                    "cond_hits": cond_hits,
                    "cond_ratio": cond_ratio,
                    "cond_base_rate": cond_base,
                    "cond_edge": cond_edge,
                    "cond_oos_n": oos_n,
                    "cond_oos_hits": oos_hits,
                    "cond_oos_ratio": oos_ratio,
                    "cond_is_ratio": is_ratio,
                    "cond_chains": (raw.get("_cond_chains") or []) if isinstance(raw, dict) else [],
                    # v425：同族机制印证（核心专用，只加分）
                    "family_cond_n": fam_cond["n"],
                    "family_cond_hits": fam_cond["hits"],
                    "family_cond_ratio": fam_cond["ratio"],
                    "family_mechanisms": fam_cond["mechanisms"],
                    # v430：出处（这条机制来自链库哪一条链）+ 核验时间
                    "source": (raw.get("_source") if isinstance(raw, dict) else None),
                    # v430：证据档位 —— annotation=纯推理注释（链库无对应条目，
                    # 不进 prompt）；chain_backed=有链可验
                    "tier": (raw.get("_tier") if isinstance(raw, dict) else None),
                    "verified_at": (raw.get("_verified_at") if isinstance(raw, dict) else None),
                })
            return out

        core_out = _fmt(cores)
        cond_out = _fmt(conds)
        # v424: 按证据质量分档排序（已验证 → 结构位 → 样本不足 → 已否证）
        core_out.sort(key=lambda f: (_factor_priority(f), -(f.get("wilson_low") or 0),
                                     -(f.get("confidence") or 0)))
        cond_out.sort(key=lambda f: (_factor_priority(f), -(f.get("wilson_low") or 0),
                                     -(f.get("confidence") or 0)))
        refuted = [f for f in core_out + cond_out if not f.get("usable")][:8]

        # 跨品种同源：同一 factor_key 还被哪些品种持有（排除 MACRO 自己）
        keys = sorted({f["factor_key"] for f in core_out + cond_out if f["factor_key"]})
        cross_asset: List[Dict[str, Any]] = []
        if keys:
            ph = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT factor_key, asset, COUNT(*) c FROM asset_driver_nodes "
                f"WHERE active=1 AND asset!='MACRO' AND factor_key IN ({ph}) "
                f"GROUP BY factor_key, asset",
                keys,
            ).fetchall()
            by_key: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                d = by_key.setdefault(r["factor_key"], {"key": r["factor_key"],
                                                        "canonical": factor_ontology.canonical(r["factor_key"]),
                                                        "family": factor_ontology.family(r["factor_key"]),
                                                        "assets": [], "instances": 0})
                d["instances"] += int(r["c"])
                if r["asset"] != asset:
                    d["assets"].append(r["asset"])
            cross_asset = [d for d in by_key.values() if d["assets"]]
            cross_asset.sort(key=lambda d: (len(d["assets"]), d["instances"]), reverse=True)
            for d in cross_asset:
                d["assets"] = d["assets"][:6]

        # 核心不足 3 条 → 链库反推（明确不是节点、没有 LLM 参与）
        chain_priors: List[Dict[str, Any]] = []
        if len(core_out) < 3:
            # v417: 旧实现 ORDER BY weight DESC —— weight 实测恒为 1.0，
            # 等于按 id 随机取 5 条，而这 5 条里常有 4 条是同一句"地缘冲突→黄金避险"
            # 的近义重复（93 条启用链共用"袭击"触发词）。改成"证据优先 + 近义合并"：
            # 先按样本量/置信度取候选，再把名字高度重合的合并成一条（记 dup_count），
            # 让 prompt 里的 5 个名额真的说 5 件不同的事。
            try:
                rows2 = conn.execute(
                    "SELECT name, cause, effect, hit_count, miss_count, weight,"
                    " confidence, expected_direction FROM causal_chains "
                    "WHERE asset=? AND COALESCE(active,1)=1 AND enabled=1 AND trigger_words!='' "
                    "ORDER BY (COALESCE(hit_count,0)+COALESCE(miss_count,0)) DESC,"
                    " confidence DESC LIMIT 20",
                    (asset,),
                ).fetchall()
                kept: List[Dict[str, Any]] = []
                for r in rows2:
                    label = str(r["name"] or "") or (str(r["cause"] or "") + "→" + str(r["effect"] or ""))
                    dup_of = None
                    for k in kept:
                        if _contain_ratio(label, k["name"]) >= 0.72 or \
                                _contain_ratio(k["name"], label) >= 0.72:
                            dup_of = k
                            break
                    if dup_of is not None:
                        dup_of["dup_count"] = int(dup_of.get("dup_count") or 1) + 1
                        continue
                    n = int(r["hit_count"] or 0) + int(r["miss_count"] or 0)
                    kept.append({
                        "name": label[:60],
                        "cause": r["cause"],
                        "effect": r["effect"],
                        "weight": round(float(r["weight"] or 0), 3),
                        "n": n,
                        "hits": int(r["hit_count"] or 0),
                        "hit_rate": round(int(r["hit_count"] or 0) / n, 3) if n else 0.0,
                        "wilson_low": round(_wilson_lower(int(r["hit_count"] or 0), n), 3),
                        "direction": str(r["expected_direction"] or "BOTH").upper(),
                        "confidence": round(float(r["confidence"] or 0), 3),
                        "dup_count": 1,
                        "source": "chain_prior",
                    })
                    if len(kept) >= 5:
                        break
                chain_priors = kept
            except sqlite3.OperationalError:
                chain_priors = []

        # v425：核心之外的已验证机制（多为 kind='chain' 节点）。核心只有 4~5 条，
        # 若把"通过条件样本核验的机制"埋在其它 kind 里，用户只看到"核心 0 条已验证"
        # 会以为整条链路没跑通，所以单独列出来，并标明它不属于核心驱动。
        verified_mech: List[Dict[str, Any]] = []
        unstable_mech: List[Dict[str, Any]] = []
        try:
            for r in conn.execute(
                "SELECT id, label, kind, evidence FROM asset_driver_nodes "
                "WHERE asset=? AND active=1 AND kind NOT IN ('asset','evidence')",
                (asset,),
            ).fetchall():
                ev = _parse_evidence(r["evidence"] or "")
                if str(ev.get("_status") or "").upper() != _STATUS_CONFIRMED:
                    continue
                # v432：样本外不达标的不进"已验证"名单，单独列"样本内观察"
                _oos_n = int(ev.get("_cond_oos_n") or 0)
                _oos_ratio = ev.get("_cond_oos_ratio")
                _base = ev.get("_cond_base_rate")
                _fragile = False
                if _oos_n >= _MIN_VERIFY_SAMPLES and _oos_ratio is not None:
                    _b = float(_base) if _base is not None else 0.5
                    if float(_oos_ratio) - _b < _EDGE_CONFIRM:
                        _fragile = True
                _item = {
                    "node_id": int(r["id"]),
                    "factor": r["label"],
                    "kind": r["kind"],
                    "cond_n": int(ev.get("_cond_n") or 0),
                    "cond_hits": int(ev.get("_cond_hits") or 0),
                    "cond_ratio": float(ev.get("_cond_ratio") or 0.0),
                    "cond_chains": ev.get("_cond_chains") or [],
                    "base_rate": ev.get("_cond_base_rate"),
                    "edge": ev.get("_cond_edge"),
                    "source": ev.get("_source"),          # v430 出处（链库 id）
                    "verified_at": ev.get("_verified_at"),
                    "cond_oos_n": _oos_n,
                    "cond_oos_hits": int(ev.get("_cond_oos_hits") or 0),
                    "cond_oos_ratio": _oos_ratio,
                    "cond_is_ratio": ev.get("_cond_is_ratio"),
                    "cond_oos_decay": (round(float(ev.get("_cond_is_ratio")) -
                                             float(_oos_ratio), 3)
                                       if _oos_n and _oos_ratio is not None and
                                       ev.get("_cond_is_ratio") is not None else None),
                    "oos_ok": (not _fragile),
                    "is_core": False,
                }
                (unstable_mech if _fragile else verified_mech).append(_item)
            # v426：按净增益排序 —— 绝对命中率在单边行情里会误导排序
            verified_mech.sort(key=lambda x: (-(x["edge"] or 0), -(x["cond_n"] or 0)))
        except sqlite3.Error:
            verified_mech = []

        return {"asset": asset, "core_factors": core_out,
                "condition_factors": cond_out,
                "refuted_factors": refuted,
                "verified_core_count": sum(1 for f in core_out if f.get("verified")),
                "verified_mechanisms": verified_mech[:5],
                "verified_mechanism_count": len(verified_mech),
                "unstable_mechanisms": unstable_mech[:5],
                "unstable_mechanism_count": len(unstable_mech),
                "cross_asset": cross_asset[:5],
                "chain_priors": chain_priors}
    finally:
        conn.close()


# v405：evidence 里混了大量系统审计日志（"auto_from_insight"、"Asset=NONE"、
# "覆盖度告警"…）。这些字符串被原样塞进 prompt，等于告诉 LLM "这条驱动是某次脚本
# 跑过的记录"。注入前先过滤，只留有信息量的证据片段。
_JUNK_EVIDENCE = ("auto_from_insight", "auto_truth", "Asset=NONE", "覆盖度告警",
                  "traceback", "Traceback", "pytest", "AssertionError", "sqlite3.")

# v424: 内部引用不该出现在给 LLM 的证据里。实测 WTI 的核心驱动证据原文是
# "chain:69(hit=9/miss=2)" —— 数据库主键 + 计数，模型从中什么都学不到，
# 还占着一个"核心驱动"的名额。这里连同 chain#/causal_chain: 一起剥掉。
_INTERNAL_REF_RE = re.compile(r"(causal_chain\s*[:#]?\s*\d+|chain\s*[#:]?\s*\d+)", re.I)
_STATS_REF_RE = re.compile(r"\(\s*(hit|miss)\s*=\s*\d+[^)]*\)", re.I)


def _clean_why(items, limit: int = 2, max_len: int = 42,
               asset: Optional[str] = None) -> List[str]:
    """evidence 可能是 list，也可能是 {chain:[...], ...} —— 两种都要能取。

    v422: 这里同时是**注入前的过滤器**。旧实现只滤 junk，导致三类污染原样进 prompt：
      1) 别的品种的结论（BTC 的证据里写「XAU 多头反应更快」）；
      2) 系统自身的执行层毛病（「同一事件重复入场」）；
      3) 夹在中文里的列名碎片（`funding/cluster_heat/volatility`）。
    过滤只影响回灌，不改库 —— 原始证据仍然可在 get_knowledge / 图页查看。
    """
    flat: List[str] = []
    if isinstance(items, dict):
        for key in ("chain", "why", "evidence"):
            v = items.get(key)
            if isinstance(v, (list, tuple)):
                flat += [str(x) for x in v]
            elif v:
                flat.append(str(v))
        if not flat:
            flat = [str(k) + "=" + str(v) for k, v in items.items()
                    if isinstance(v, (str, int, float))]
    elif isinstance(items, (list, tuple)):
        flat = [str(x) for x in items]
    elif items:
        flat = [str(items)]
    out: List[str] = []
    for s in flat:
        s = s.strip()
        if not s or s.startswith("{"):
            continue
        # v424: 先剥内部引用（chain:69 / causal_chain:123 / (hit=9/miss=2)）
        s = _STATS_REF_RE.sub("", _INTERNAL_REF_RE.sub("", s)).strip(" ，,;；:：")
        if not s:
            continue
        if any(j.lower() in s.lower() for j in _JUNK_EVIDENCE):
            continue
        if _has_schema_fragment(s):
            continue
        if any(w in s for w in _SELF_META_WORDS):
            continue
        if asset and (_foreign_asset_label(asset, s) or _mentions_foreign_asset(asset, s)):
            continue
        if len(s) > 3 and s not in out:
            out.append(s[:max_len] + ("…" if len(s) > max_len else ""))
        if len(out) >= limit:
            break
    return out


def _is_junk_factor_name(name: str) -> bool:
    """因素名本身是否是噪声（列名碎片 / 系统执行层毛病）。

    v422: `_clean_why` 只洗证据文案，但「同一事件重复入场」这类执行层毛病
    是作为**因素名**进 prompt 的 —— 必须在入口再拦一次。
    """
    n = str(name or "")
    if _has_schema_fragment(n):
        return True
    return any(w in n for w in _SELF_META_WORDS)


def _keep_for_prompt(asset: str, factor: Dict[str, Any]) -> bool:
    """该因素是否还值得进 prompt：名字干净，且至少有一条证据能通过过滤。

    v422: 名字干净但证据全是执行层废话的（SPX「事件驱动交易的微观结构与规则」），
    也不该占 prompt 位置 —— 模型只会学到"系统在说自己"。
    """
    if _is_junk_factor_name(factor.get("factor")):
        return False
    why = factor.get("why_chain") or []
    if why and not _clean_why(why, limit=1, asset=asset):
        return False
    return True


def _stats_bit(f: Dict[str, Any]) -> str:
    n = int(f.get("n") or 0)
    if n <= 0:
        return "无样本"
    return "n=%d 命中%d/%d（下界%.2f）" % (n, int(f.get("hits") or 0), n,
                                        float(f.get("wilson_low") or 0))


def _split_for_inject(k: Dict[str, Any], asset: str,
                      top: int = 4) -> tuple:
    """把 get_knowledge 的结果分成「可注入」与「反例留档」两组（v424）。

    可注入 = 名字/证据干净 且 未被否证；反例 = 已被事实否证（单独一行提示，
    不再占核心名额）。之前已否证的核心照样进 prompt，等于把已知错误喂给模型。
    """
    cores_all = [c for c in (k.get("core_factors") or []) if _keep_for_prompt(asset, c)]
    # v430：`tier=annotation` 的条件因素是推理写入、链库无对应条目（实测 99/99），
    # 它们占着 prompt 位置却没有任何样本可核 —— 只留在知识接口里展示，不进 prompt。
    conds_all = [c for c in (k.get("condition_factors") or [])
                 if _keep_for_prompt(asset, c) and c.get("tier") != "annotation"]
    cores = [c for c in cores_all if c.get("usable")][:top]
    conds = [c for c in conds_all if c.get("usable")][:top]
    refuted = [c for c in cores_all + conds_all if not c.get("usable")][:3]
    return cores, conds, refuted


def inject_block_json(asset: str, top: int = 4) -> Dict[str, Any]:
    """结构化回灌（给前端卡片 / 下游解析用；文本版见 inject_block）。

    字段口径与 inject_block 完全一致，避免"文本说一套、JSON 说另一套"。
    """
    asset = _norm_asset(asset)
    k = get_knowledge(asset)
    cores, conds, refuted = _split_for_inject(k, asset, top)

    def _f(c: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "factor": c["factor"],
            "factor_key": c.get("factor_key"),
            "canonical": c.get("canonical"),
            "family": c.get("family"),
            "confidence": c.get("confidence"),
            "n": c.get("n", 0),
            "hits": c.get("hits", 0),
            "hit_rate": c.get("hit_rate", 0.0),
            "wilson_low": c.get("wilson_low", 0.0),
            "sample_status": c.get("sample_status", "无样本"),
            "evidence_status": c.get("evidence_status"),
            "verdict": c.get("verdict"),
            "verified": c.get("verified", False),
            "usable_reason": c.get("usable_reason"),
            "why": _clean_why(c.get("why_chain"), asset=asset),
        }

    return {
        "asset": asset,
        "version": max([int(c.get("version") or 0) for c in cores + conds] or [0]),
        "core_factors": [_f(c) for c in cores],
        "condition_factors": [_f(c) for c in conds],
        "refuted_factors": [_f(c) for c in refuted],
        "cross_asset": k.get("cross_asset", []),
        "chain_priors": k.get("chain_priors", [])[:3],
        "total_samples": sum(int(c.get("n") or 0) for c in cores + conds),
    }


def _chain_library_hit_rate(conn: sqlite3.Connection) -> Tuple[Optional[float], int]:
    """链库实测命中率（只用启用中、样本≥12 的真实链，剔除模板/元信息链）。

    v424: 注入块里原来写死"同期链级实测命中率 48.5%"——那是一次性测量的快照，
    链库一变这行就在说假话。改成实时算，数字对不上就明写"样本不足"。
    返回 (命中率 or None, 样本数)。
    """
    try:
        row = conn.execute(
            "SELECT SUM(hit_count) h, SUM(hit_count+miss_count) t FROM causal_chains "
            "WHERE enabled=1 AND (hit_count+miss_count)>=12 "
            "AND COALESCE(origin,'')!='deprecated_meta' "
            "AND effect NOT LIKE '%NONE%' AND cause NOT LIKE '%[覆盖度告警]%'"
        ).fetchone()
        h, t = int(row[0] or 0), int(row[1] or 0)
        return ((h / t) if t else None, t)
    except sqlite3.Error:
        return (None, 0)


def _chain_library_edge(conn: sqlite3.Connection) -> Dict[str, Any]:
    """链库整体"超额"：实测命中率 vs 同期同方向基准率（v417）。

    为什么必须报这个：只报"命中率 50.2%"会让 LLM 以为一半一半是"还没有结论"，
    而真正的结论是**它跑不赢基准**——同期 XAU 5 日窗口本身就是 71% 下跌，
    DOWN 链 62.7% 反而比闭眼猜跌更差。把基准一起摆出来，模型才不会把
    "骑行情漂移"当成"机制有效"。

    返回 {hit_rate, n, base_rate, lift, chains_strong, verdict}；
    基准率取各品种样本按链数加权（缺行情的品种不计入，不猜 50%）。
    """
    try:
        import chain_prior as _cp  # 阈值只留一份，避免两处口径漂移
    except Exception:
        return {"hit_rate": None, "n": 0, "base_rate": None, "lift": None,
                "chains_strong": 0, "verdict": "口径不可用"}
    rows = []
    try:
        rows = conn.execute(
            "SELECT asset, UPPER(COALESCE(expected_direction,'')) d, hit_count, miss_count"
            " FROM causal_chains WHERE enabled=1 AND COALESCE(active,1)=1"
            " AND COALESCE(hit_count,0)+COALESCE(miss_count,0)>=?"
            " AND UPPER(COALESCE(expected_direction,'')) IN ('UP','DOWN')",
            (_cp.MIN_CHAIN_SAMPLES_FOR_STRONG,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return {"hit_rate": None, "n": 0, "base_rate": None, "lift": None,
                "chains_strong": 0, "verdict": "无达样本链"}
    hits = total = 0
    weighted_base = 0.0
    strong = 0
    for asset, d, h, m in rows:
        h, m = int(h or 0), int(m or 0)
        n = h + m
        if n <= 0:
            continue
        p_up = _cp._base_rate_up(conn, asset)
        if p_up is None:
            continue
        p0 = p_up if d == "UP" else 1.0 - p_up
        hits += h
        total += n
        weighted_base += p0 * n
        if ((h / n) - p0) >= _cp.MIN_LIFT_FOR_STRONG and \
                _cp._binom_p_greater(h, n, p0) <= _cp.MAX_P_VALUE_FOR_STRONG:
            strong += 1
    if not total:
        return {"hit_rate": None, "n": 0, "base_rate": None, "lift": None,
                "chains_strong": 0, "verdict": "基准率不可得"}
    rate = hits / total
    base = weighted_base / total
    lift = rate - base
    if strong:
        verdict = "有 %d 条链跑赢基准，可作方向依据" % strong
    elif lift > 0:
        verdict = "整体超额 +%.0fpp 但不显著，不得作方向依据" % (lift * 100)
    else:
        verdict = "整体跑不赢基准（%.0fpp），不得作方向依据" % (lift * 100)
    return {"hit_rate": round(rate, 4), "n": total, "base_rate": round(base, 4),
            "lift": round(lift, 4), "chains_strong": strong, "verdict": verdict}


_MECH_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = mechanism_family.FAMILIES


def _mechanism_family(name: str) -> str:
    """链名归到机制族（地缘/利率/供需/通胀/监管/风险偏好/其它）。

    口径唯一定义在 `mechanism_family.py`（评估器入库时也要用同一份，
    否则"分析时去重"和"入库时收敛"会各说各话）。这里只做转发。
    """
    return mechanism_family.family_of(name)


def _proven_chains(conn: sqlite3.Connection, asset: str,
                   limit: int = 4) -> List[Dict[str, Any]]:
    """本品种**跑赢基准**的链 —— 唯一可以直接当方向依据的一批（v417）。

    命中率必须比"同品种同方向闭眼猜"高 ≥10pp 且单侧二项 p≤0.05。
    0 条就返回空列表，调用方要明写"0 条达标"，不许用绝对命中率凑数。
    """
    try:
        import chain_prior as _cp
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT id, name, expected_direction, hit_count, miss_count FROM causal_chains"
            " WHERE enabled=1 AND COALESCE(active,1)=1 AND asset=?"
            " AND UPPER(COALESCE(expected_direction,'')) IN ('UP','DOWN')"
            " AND COALESCE(hit_count,0)+COALESCE(miss_count,0)>=?",
            (str(asset), _cp.MIN_CHAIN_SAMPLES_FOR_STRONG),
        ).fetchall()
    except sqlite3.Error:
        return []
    p_up = _cp._base_rate_up(conn, asset)
    if p_up is None:
        return []
    out: List[Dict[str, Any]] = []
    for cid, name, d, h, m in rows:
        h_i, n = int(h or 0), int(h or 0) + int(m or 0)
        if n <= 0:
            continue
        d = str(d).upper()
        p0 = p_up if d == "UP" else 1.0 - p_up
        hit_rate = h_i / n
        lift = hit_rate - p0
        pval = _cp._binom_p_greater(h_i, n, p0)
        if lift < _cp.MIN_LIFT_FOR_STRONG or pval > _cp.MAX_P_VALUE_FOR_STRONG:
            continue
        out.append({"chain_id": int(cid), "name": str(name or "")[:48],
                    "direction": d, "n": n, "hit_rate": round(hit_rate, 4),
                    "base_rate": round(p0, 4), "lift": round(lift, 4),
                    "p_value": round(pval, 4), "dup_count": 1})
    out.sort(key=lambda x: (-x["lift"], -x["n"]))
    # v417: 同族同向的达标链必须合并展示。
    # 实测 XAU 有 4 条"地缘冲突→黄金避险"全部跑赢基准，名字不同但讲的是同一件事：
    # 分开列会让 LLM 以为有 4 倍把握。合并后保留证据最强的一条，
    # 其余只报数量与合计样本 —— **不把 n 相加、不重算 p**（同源事件不独立，
    # 相加就是拿同一件事算 4 遍）。
    cluster: List[Dict[str, Any]] = []
    for c in out:
        fam = _mechanism_family(c["name"])
        same = None
        for k in cluster:
            if k["family"] == fam and k["direction"] == c["direction"]:
                same = k
                break
        if same is not None:
            same["dup_count"] = int(same["dup_count"]) + 1
            same["merged_n"] = int(same.get("merged_n") or same["n"]) + c["n"]
            continue
        c["family"] = fam
        c["merged_n"] = c["n"]
        cluster.append(c)
    return cluster[:limit]


def inject_block(asset: str, top: int = 4) -> str:
    """生成回灌文本块：把某品种涨跌本质注入 AI 分析（供 ai_worker prompt 接线）。

    v405 结构化；v422 加过滤；v423 加刻度（n/命中/Wilson 下界）、跨品种同源与
    链库先验。方向不再用"48.5% 与随机无异"一句话打发所有因素 —— 有样本的给数字，
    没样本的明写"无样本"。
    """
    asset = _norm_asset(asset)
    k = get_knowledge(asset)
    # v422: 进水口过滤 —— 列名碎片/执行层毛病不作为「驱动」进 prompt
    # v424: 已否证的因素不再占核心名额，单独成"反例"一行
    cores, conds, refuted = _split_for_inject(k, asset, top)
    priors = k.get("chain_priors") or []
    if not cores and not conds and not refuted:
        # v176.2：即使没学过该品种，也返回非空兜底，避免 LLM 写"未注入因果图谱"占位过短。
        fallback = _get_default_drivers(asset)
        tail = ""
        if priors:
            tail = "\n链库先验（无 LLM、未节点化，仅供参照）：\n" + "\n".join(
                " %d. %s（权重%.2f；样本 n=%d）" % (i + 1, p["name"], p["weight"], p["n"])
                for i, p in enumerate(priors[:3]))
        return (
            f"[品种涨跌本质·{asset}] 通用驱动结构(尚未学习): "
            f"核心驱动={fallback['core']}; 条件因素={fallback['condition']}。"
            f"（系统自学习未达到回灌阈值，注入通用结构供 LLM 推理参考，勿盲从）"
            f"\n覆盖状态：本品种知识库 0 个因子、链库 0 条可用链、0 次核验 —— "
            f"上面这段通用结构是**行业常识而非本系统学到的证据**，不得作为方向依据，"
            f"也不得据此给出「因为X所以涨/跌」的归因；只能用来解释波动的大类来源。" + tail
        )
    ver = max([int(c.get("version") or 0) for c in cores + conds] or [0])

    def _line(i, f, tag="", role=""):
        why = _clean_why(f.get("why_chain"), asset=asset)
        conf = f.get("confidence")
        name = f["factor"]
        bits = []
        if conf is not None:
            bits.append("置信 %.2f" % float(conf))
        # v425：有同族机制样本时，交易池的 n 会跟同族数字打架（一个 14、一个 33），
        # 后者才是这个核心真正的刻度 —— 只报一个，避免 prompt 里自相矛盾
        if int(f.get("family_cond_n") or 0) >= _MIN_VERIFY_SAMPLES:
            bits.append("同族机制 %d/%d 命中" % (int(f.get("family_cond_hits") or 0),
                                            int(f.get("family_cond_n") or 0)))
        else:
            bits.append(_stats_bit(f))
        st = (f.get("verdict") or f.get("evidence_status") or "").upper()
        if st in ("CONTRADICTED", "REFUTED", "FAILED"):
            # v423.1: 慢变量核心可能是"旧证伪"——它被核验杀过一次、又因结构位复活。
            # 不涂掉证伪结论，但也不能一边叫"核心驱动"一边让模型当方向依据。
            if role == "core" and f.get("slow"):
                bits.append("**旧核验否证（仅结构位，勿据此定方向）**")
            else:
                bits.append("**已被否证%s**" % ("/" + str(f.get("evidence_fail")) if f.get("evidence_fail") else ""))
        elif st == "CONFIRMED":
            bits.append("已验证")
        elif st == "SUPPORTED_BY_MECHANISMS":
            bits.append("✅同族机制印证（非自身样本）")
        elif st:
            bits.append("未确认")
        if why:
            bits.append("证据: " + " / ".join(why))
        else:
            bits.append("无独立证据")
        if f.get("canonical") and f.get("canonical") != name:
            bits.append("归因:" + str(f["canonical"]))
        return " %d. %s（%s）%s" % (i, name, "；".join(bits), tag)

    parts = ["[品种涨跌本质·%s] 自学习图谱 v%s，注入 %d 核心 + %d 条件"
             % (asset, ver, len(cores), len(conds))]
    if cores:
        parts.append("核心驱动（最底层，按证据质量降序）：")
        parts += [_line(i + 1, c, role="core") for i, c in enumerate(cores)]
    else:
        # v424: 有否证记录时不再退回"通用硬编码结构" —— 那会把"我们其实学过、
        # 但都被事实打脸了"说成"尚未学习"，掩盖问题。
        parts.append("核心驱动：0 条可用%s"
                     % ("（%d 条候选已被事实否证，见下方反例）" % len(refuted)
                        if refuted else "（该品种尚未从 5-why 学到结构性原因，不要凭空补）"))
    if conds:
        parts.append("条件因素（触发/放大/抑制）：")
        parts += [_line(i + 1, c) for i, c in enumerate(conds)]
    if refuted:
        # v424: 反例单独成段 —— 保留"这条被打脸过"的信息，但不让它冒充核心驱动
        parts.append("已否证（仅留档，勿据此定方向）：")
        for c in refuted:
            n_i = int(c.get("n") or 0)
            if c.get("structural_only"):
                # 慢变量核心被旧核验否证过 → 只可能是"结构位"，不能让模型当方向依据
                parts.append(" · %s（旧核验否证，仅结构位，勿据此定方向）" % c["factor"])
            elif n_i == 0:
                parts.append(" · %s（旧结论已否证，当前无新样本）" % c["factor"])
            else:
                parts.append(" · %s（已否证，支持率 %.0f%%，n=%d）"
                             % (c["factor"],
                                100 * float(c.get("hit_rate") or 0),
                                n_i))
    cross = k.get("cross_asset") or []
    if cross:
        parts.append("跨品种同源（同一规范因子还被这些品种持有）：")
        parts += [" · %s（%s）：%s" % (c["canonical"], c["key"],
                                      "、".join(c["assets"])) for c in cross[:3]]
    if priors:
        parts.append("链库其余候选（未达验证门槛，仅作解释假设）：")
        for p in priors[:3]:
            n_p = int(p.get("n") or 0)
            dup = int(p.get("dup_count") or 1)
            parts.append(
                " · %s（n=%d，命中%s）%s"
                % (p["name"], n_p,
                   ("%.0f%%" % (100 * float(p.get("hit_rate") or 0))) if n_p else "无样本",
                   ("，已合并 %d 条近义链" % (dup - 1)) if dup > 1 else "")
            )
    # v417: 链级证据必须"跑赢基准"才算数 —— 绝对命中率在趋势行情里会骗人
    total_n = sum(int(c.get("n") or 0) for c in cores + conds)
    conn = None
    try:
        conn = db.get_connection()
        edge = _chain_library_edge(conn)
        proven = _proven_chains(conn, asset)
    except Exception:
        edge, proven = {"hit_rate": None, "n": 0, "base_rate": None, "lift": None,
                        "chains_strong": 0, "verdict": "口径不可用"}, []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if proven:
        parts.append("链级已验证证据（跑赢同品种同向基准≥10pp 且 p≤0.05，可作方向依据；"
                     "同族链已合并）：")
        parts += [
            " · [%s|%s] %s（命中 %d/%d=%.0f%%，基准 %.0f%%，超额 %+.0fpp，p=%.3f）%s"
            % (asset, p["direction"], p["name"], int(round(p["hit_rate"] * p["n"])),
               p["n"], p["hit_rate"] * 100, p["base_rate"] * 100,
               p["lift"] * 100, p["p_value"],
               ("（同族另有 %d 条达标链已合并，合计 n=%d；把握不叠加）"
                % (p["dup_count"] - 1, p["merged_n"]))
               if int(p.get("dup_count") or 1) > 1 else "")
            for p in proven
        ]
    else:
        parts.append("链级已验证证据：本品种 0 条链跑赢基准（绝对命中率不算数，勿据之以定向）")
    if edge.get("hit_rate") is not None:
        head = ("链级整体 命中率 %.1f%%（n=%d） vs 同向基准 %.1f%% = 超额 %+.0fpp"
                % (edge["hit_rate"] * 100, edge["n"], edge["base_rate"] * 100,
                   edge["lift"] * 100))
        if proven:
            edge_bit = "%s；本品种可用 %d 条（见上）" % (head, len(proven))
        else:
            edge_bit = ("%s；但**本品种 0 条链跑赢基准** —— 方向不得引用链库，"
                        "只能按其余证据判断" % head)
    else:
        edge_bit = "链级样本不足或基准率不可得，尚无实测超额"
    if total_n > 0:
        parts.append("使用边界：n 来自事实核验（支持/反驳事实计数），不是交易样本；"
                     "方向只能依据上面「已验证证据」那几条（%s）；证据多来自系统内部记录，"
                     "只能作为推理时的候选解释。" % edge_bit)
    else:
        parts.append("使用边界：这些因素尚无核验样本（方向未经样本验证；%s）；"
                     "证据多来自系统内部记录，不能作为开仓依据。" % edge_bit)
    return "\n".join(parts)


def _get_default_drivers(asset: str) -> Dict[str, str]:
    """v176.2 兜底：每个品种的"通用驱动结构"硬编码（causal_graph 没学到时用）。"""
    defaults = {
        "XAU":   {"core": "实际利率与美元指数",   "condition": "央行购金、地缘风险溢价、通胀预期"},
        "WTI":   {"core": "全球供需平衡",           "condition": "OPEC+ 减产、地缘冲突、美国战略储备释放"},
        "BTC":   {"core": "全球流动性与美元周期",   "condition": "ETF 资金流、监管政策、链上活跃度"},
        "ETH":   {"core": "链上应用与L2生态",       "condition": "Gas 费用、ETF 预期、解锁抛压"},
        "SOL":   {"core": "高性能链生态扩张",       "condition": "TVL 增长、Memecoin 热度、Solana 网络稳定性"},
        "US_STOCK": {"core": "美联储利率路径与盈利预期", "condition": "非农就业、CPI、PMI、龙头财报"},
        "CN_STOCK": {"core": "国内宏观政策与流动性", "condition": "PMI、社融、地产销售、政策刺激"},
    }
    return defaults.get(asset or "", {"core": "宏观流动性", "condition": "供需结构、消息面、资金流向"})


def merge_duplicate_assets(dry_run: bool = True) -> Dict[str, Any]:
    """一次性迁移：把 ``asset_driver_nodes`` 中同义（大写后相同）的节点合并为大写。

    合并规则：
      - 取大写后相同的若干节点中 ``version`` 最大者作为「主节点」；
      - 把其它副本的边（from_id/to_id 指向副本）改写到主节点 id；
      - 删除副本节点。
    表 ``ai_decisions.target_asset`` 不动 — 那是 ai_decisions 自身职责，不属于
    causal_graph 模块边界。

    ``dry_run=True``（默认）只返回将要做的事情，不写入。生产 DB 必须先用
    dry_run 预览，再 dry_run=False 真正执行。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, asset, label, kind, is_core, version FROM asset_driver_nodes"
        ).fetchall()
        groups: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            k = _norm_asset(r["asset"])
            groups.setdefault(k, []).append(r)

        plan: List[Dict[str, Any]] = []
        for norm_asset, items in groups.items():
            assets_here = {r["asset"] for r in items}
            if len(assets_here) <= 1:
                continue
            primary = max(items, key=lambda r: (int(r["version"] or 0), int(r["id"])))
            dup_ids = [int(r["id"]) for r in items if r["id"] != primary["id"]]
            plan.append({
                "asset_canonical": norm_asset,
                "primary_id": int(primary["id"]),
                "duplicate_ids": dup_ids,
                "duplicate_assets_seen": sorted(assets_here),
            })

        if dry_run:
            return {"dry_run": True, "merge_groups": len(plan), "plan": plan}

        # 真正执行
        edges_moved = 0
        nodes_removed = 0
        for entry in plan:
            primary_id = entry["primary_id"]
            # kind=asset 是图谱「果」端 anchor，所有因果边都指向它，
            # 必须按 (asset_canonical, label) 唯一保留一个，不能被合并掉。
            # 因此排除 entry 里所有 asset 节点不参与合并。
            for dup_id in entry["duplicate_ids"]:
                dup_kind = conn.execute(
                    "SELECT kind FROM asset_driver_nodes WHERE id=?", (dup_id,)
                ).fetchone()
                if dup_kind and dup_kind[0] == "asset":
                    # 把副本 asset 节点的 asset 字段直接改写为大写（不动 id）
                    conn.execute(
                        "UPDATE asset_driver_nodes SET asset=? WHERE id=?",
                        (entry["asset_canonical"], dup_id),
                    )
                    continue
                cur = conn.execute(
                    "UPDATE asset_driver_edges SET from_id=? WHERE from_id=?",
                    (primary_id, dup_id),
                )
                edges_moved += cur.rowcount
                cur = conn.execute(
                    "UPDATE asset_driver_edges SET to_id=? WHERE to_id=?",
                    (primary_id, dup_id),
                )
                edges_moved += cur.rowcount
                # 移除因合并产生的自环 / 重复边（UNIQUE 约束会导致下一句 INSERT/UPDATE 冲突）
                conn.execute(
                    "DELETE FROM asset_driver_edges WHERE from_id=to_id"
                )
                # 同 (from_id,to_id,kind) 多条重复边只保留 weight 最大一条
                conn.execute(
                    "DELETE FROM asset_driver_edges WHERE id NOT IN ("
                    "  SELECT id FROM asset_driver_edges WHERE rowid IN ("
                    "    SELECT MIN(rowid) FROM asset_driver_edges "
                    "    GROUP BY from_id, to_id, kind)"
                    ")"
                )
                conn.execute("DELETE FROM asset_driver_nodes WHERE id=?", (dup_id,))
                nodes_removed += 1
            # 主节点 asset 字段统一为大写
            conn.execute(
                "UPDATE asset_driver_nodes SET asset=? WHERE id=?",
                (entry["asset_canonical"], primary_id),
            )
        conn.commit()
        return {"dry_run": False, "merge_groups": len(plan),
                "edges_moved": edges_moved, "nodes_removed": nodes_removed}
    finally:
        conn.close()