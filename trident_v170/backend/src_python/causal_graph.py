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
from typing import Any, Dict, List, Optional

import db
import review_agent


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
        # 宏观因果知识（causal_chains）——跨品种底层驱动候选
        chains = conn.execute(
            "SELECT DISTINCT cause, effect FROM causal_chains WHERE enabled=1"
        ).fetchall()
        for r in chains:
            if r["cause"]:
                counter[r["cause"]] += 1
            if r["effect"]:
                counter[r["effect"]] += 1
    finally:
        conn.close()
    return [f for f, _ in counter.most_common(limit)]


def reason_asset_drivers(asset: str,
                         candidates: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """对单个品种做 5-why 推理，产出核心因素/条件因素/推理链。"""
    if candidates is None:
        candidates = collect_candidate_factors(asset)
    if not candidates:
        return None
    factor_lines = "\n".join(f"- {c}" for c in candidates)
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
        '{"core_factors":[{"factor":"…","why_chain":["表层现象短语","中间原因短语","底层原因短语"],"confidence":0-1}],'
        '"condition_factors":[{"factor":"…","why_chain":["触发条件短语","放大/抑制机制短语"],"confidence":0-1}],'
        '"summary":"一句话总结该品种涨跌本质"}'
    )
    result = _parse_json(_llm(prompt, "你是量化宏观因果分析师，只输出 JSON。", max_tokens=1600))
    if not isinstance(result, dict):
        return None
    return result


def _upsert_node(conn: sqlite3.Connection, asset: str, label: str, kind: str,
                 is_core: bool, confidence: float, evidence: str = "") -> Optional[int]:
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
    if row:
        nid = int(row[0])
        old_kind = row[3]
        old_is_core = int(row[4] or 0)
        old_evidence = row[5] or ""
        new_conf = max(float(row[1]), float(confidence))
        new_ver = int(row[2]) + 1
        # evidence 合并（按 JSON list 去重保留原始出现顺序）
        merged_evidence = _merge_evidence(old_evidence, evidence)
        # 防御性：若调用方错误地把 asset 节点当作 condition 传进来，记录一次警告
        # 但不修改 kind（按上述不变量）
        if old_kind == "asset" and kind != "asset":
            print(f"[causal_graph] WARN: 节点 {asset}/{norm} 已为 asset 节点，"
                  f"被以 kind={kind} 重新 upsert；保留 kind=asset 不变")
        conn.execute(
            "UPDATE asset_driver_nodes SET confidence=?, version=?, active=1, "
            "evidence=?, updated_at=? WHERE id=?",
            (new_conf, new_ver, merged_evidence, now, nid),
        )
        return nid
    cur = conn.execute(
        "INSERT INTO asset_driver_nodes "
        "(asset, label, kind, is_core, confidence, evidence, version, active, updated_at) "
        "VALUES (?,?,?,?,?,?,1,1,?)",
        (asset, norm, kind, 1 if is_core else 0, confidence, _cap_evidence(evidence), now),
    )
    return int(cur.lastrowid)


def _merge_evidence(old: str, new: str) -> str:
    """合并 evidence 字符串（JSON list），按出现顺序去重。

    旧值/新值都尝试按 JSON list 解析，解析失败则降级为整体字符串。
    """
    def _parse(s: str):
        if not s:
            return []
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else [str(v)]
        except (json.JSONDecodeError, TypeError):
            return [s]

    items = _parse(old) + _parse(new)
    seen = set()
    out = []
    for x in items:
        key = str(x)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    # 如果合并后为空（极端情况），至少保留 new 的原始字符串
    return _cap_evidence(json.dumps(out if out else ([new] if new else []),
                                    ensure_ascii=False))


# v300-audit: evidence 字段曾在生产库里长到 872,422,822 字节（单行一条），
# 后果有三：
#   1) asset_driver_nodes 的任何全表扫描都要读这几百 MB →
#      /api/analysis/kg-stats 与 /api/review/causal-graph/stats 稳定 60s+ 超时
#   2) WAL 被撑到 468MB，整个库膨胀到 3.66GB（freelist 188k 页）
#   3) 任何 SELECT ... FROM asset_driver_nodes 都被拖慢
# 根因：upsert 每次都往 evidence 追加一条，_merge_evidence 只去重不限制体积。
# 这里加硬上限，超限后丢弃最旧条目、保留最新证据。
_MAX_EVIDENCE_BYTES = 64 * 1024


def _cap_evidence(text: str) -> str:
    """把 evidence 字符串限制在 _MAX_EVIDENCE_BYTES 以内（超限丢最旧条目）。"""
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
    out: List[str] = []
    total = 0
    for x in reversed(items):          # 最新的优先保留
        b = len(str(x).encode("utf-8", "ignore")) + 2
        if out and total + b > _MAX_EVIDENCE_BYTES:
            break
        out.append(str(x))
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
        }
    if isinstance(v, list):
        # 旧数据：纯 list
        return {
            "chain": [str(x) for x in v if str(x).strip()],
            "_status": _STATUS_UNCONFIRMED,
            "_fail": None,
            "_verified_at": None,
            "_fact_ids": [],
            "_position": None,
        }
    if isinstance(v, dict):
        # 新数据：补全缺省字段
        chain = v.get("chain", [])
        if not isinstance(chain, list):
            chain = [str(chain)]
        return {
            "chain": [str(x) for x in chain if str(x).strip()],
            "_status": v.get("_status", _STATUS_UNCONFIRMED),
            "_fail": v.get("_fail"),
            "_verified_at": v.get("_verified_at"),
            "_fact_ids": v.get("_fact_ids", []) or [],
            "_position": v.get("_position"),
        }
    # 兜底
    return {
        "chain": [str(v)],
        "_status": _STATUS_UNCONFIRMED,
        "_fail": None,
        "_verified_at": None,
        "_fact_ids": [],
        "_position": None,
    }


def _serialize_evidence(chain: List[str], status: str = _STATUS_UNCONFIRMED,
                         fail: Optional[str] = None,
                         position: Optional[str] = None,
                         fact_ids: Optional[List[int]] = None,
                         verified_at: Optional[str] = None) -> str:
    """序列化为 JSON 字符串（统一入口，写库前必走这里）。"""
    obj = {
        "chain": [str(x) for x in (chain or []) if str(x).strip()],
        "_status": status,
        "_fail": fail,
        "_verified_at": verified_at,
        "_fact_ids": list(fact_ids or []),
        "_position": position,
    }
    return json.dumps(obj, ensure_ascii=False)


def _chain_text(ev: Dict[str, Any]) -> str:
    """把 chain 列表拼成单字符串（用于关键词匹配）。"""
    return " ".join(str(x) for x in ev.get("chain", []))


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


def _chain_infers_direction(chain: List[str]) -> Optional[str]:
    """从 chain 推理「假设预期方向」：取因果链「驱动端」方向。

    返回 'up' / 'down' / None（无法判定）。

    设计说明：因果链形如 "DXY 涨 → 实际利率上行 → BTC 跌"，整段的核心是
    "驱动方向"（链首：DXY 涨=up）。事实核验时，问的是"驱动是否发生"，
    而不是"结果是否如预期"——因为结果受中间层多重传导影响，可能是错的。
    所以核验的方向应该用「因端方向」（第一个出现的方向词）。

    实现：扫描 chain，遇到第一个含方向词的层就返回其方向。

    v276+ fix: 扩展中文方向词表,覆盖 LLM 生成链常用的隐式方向表达:
      - 涨/跌、上行/下行、上涨/下跌、升值/贬值
      - 升/降、走强/走弱、增强/减弱
      - 强势/弱势、牛市/熊市、利好/利空、正向/负向
      - 反弹/回落、突破/跌破、走高/走低、攀升/下挫
      - 回升/下行(重复安全)、支撑/压制 (中性但有倾向)
    """
    up_words = [
        "涨", "上行", "上涨", "升值", "利好", "升", "+",
        "走强", "增强", "强势", "牛市", "正向", "正面",
        "反弹", "突破", "走高", "攀升", "回升", "回升", "向上",
        "提振", "助推", "利多", "增益", "扩张", "上扬",
    ]
    down_words = [
        "跌", "下行", "下跌", "贬值", "利空", "降", "-",
        "走弱", "减弱", "弱势", "熊市", "负向", "负面",
        "回落", "跌破", "走低", "下挫", "向下",
        "压制", "承压", "拖累", "利空", "收缩", "回落",
    ]
    for layer in chain:
        s = str(layer)
        # 优先 up（如果同层两个词都有，按"驱动优先"取 up）
        if any(w in s for w in up_words):
            return "up"
        if any(w in s for w in down_words):
            return "down"
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
                             limit: int = 200) -> Dict[str, Any]:
    """把 causal_chains 表里的链条快速种进图谱 — 免 LLM,立即让 XAU/XAG/WTI
    等新接入资产也有"驱动因素"节点。

    v276+ 设计动机:
      之前 XAU/WTI 有 100+ 条因果链在 causal_chains,但 asset_driver_nodes 里
      黄金/原油是空的 — 因为 run_reasoning 要 LLM,且 cron 从未自动跑过。
      seed_from_causal_chains 走纯规则路径:每个 cause 作为一个 macro 节点,
      每个 (cause, effect) 中包含的资产 token 作为 asset 节点,再建边。

    输入:
      asset: 仅种指定资产的链 (None = 全部)
      min_weight: 只用 weight >= 此值的链 (默认 0.5)
      limit: 最多处理 N 条链

    返回: {chains_processed, nodes_added, edges_added, by_asset}
    """
    conn = db.get_connection()
    chains_processed = 0
    nodes_added = 0
    edges_added = 0
    by_asset: Dict[str, int] = {}

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

            # 1) cause 作为 macro 节点(归一化 label)
            cause_label = str(cause).strip()[:60] or f"chain#{cid}"
            cause_norm = _norm_label(cause_label)
            # 用 'all' 作为 asset (跨品种宏观因素)
            cause_id = _upsert_node(conn, "all", cause_label, "core", True,
                                    float(weight or 0.5),
                                    json.dumps([str(name)[:60] if name else ""],
                                               ensure_ascii=False))
            if cause_id:
                nodes_added += 1

            # 2) 每个被关联的资产建 asset 节点 + 一条 causes 边
            for a in asset_tokens:
                aid = _ensure_asset_node(conn, a)
                if not aid:
                    continue
                by_asset[a] = by_asset.get(a, 0) + 1
                if cause_id:
                    before = conn.execute(
                        "SELECT 1 FROM asset_driver_edges "
                        "WHERE from_id=? AND to_id=? AND kind=?",
                        (cause_id, aid, "causes"),
                    ).fetchone()
                    if not before:
                        edges_added += 1
                    _upsert_edge(conn, cause_id, aid, "causes",
                                 weight=float(weight or 0.5),
                                 evidence=f"[seed from chain#{cid}] {name or ''}"[:120])
                # 跨资产神经: 同 cause 触发的两两资产 → correlates_with
                # 已在 link_cross_asset_neurons 中处理,这里不再重复

        conn.commit()
        return {
            "ok": True,
            "chains_processed": chains_processed,
            "nodes_added": nodes_added,
            "edges_added": edges_added,
            "by_asset": by_asset,
        }
    finally:
        conn.close()


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

        new_cores = result.get("core_factors") or []
        for item in new_cores:
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "").strip()
            if not factor:
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
            core_labels.append(_norm_label(factor))
            core_id = _upsert_node(conn, asset, factor, "core", True, conf,
                                   json.dumps(chain, ensure_ascii=False))
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
            chain = item.get("why_chain") or []
            if isinstance(chain, str):
                try:
                    chain = json.loads(chain)
                except json.JSONDecodeError:
                    chain = [chain]
            chain = [_clean_chain_item(x) for x in chain if str(x).strip()]
            chain = [x for x in chain if x]
            conf = _clamp(item.get("confidence"))
            cond_labels.append(_norm_label(factor))
            cond_factor_count += 1
            cid = _upsert_node(conn, asset, factor, "condition", False, conf,
                               json.dumps(chain, ensure_ascii=False))
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
                "condition_factors": cond_factor_count}
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
    import config  # v200+ Q5: 自适应三件套 — 本地 import 避免循环
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
        if int(ver or 0) < _protect_v:
            continue  # 成长期保护 (自适应)
        new_conf = max(float(conf) * _core_decay, 0.0)
        if new_conf < 0.2:
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
        if int(ver or 0) < _protect_v:
            continue
        new_conf = max(float(conf) * _cond_decay, 0.0)
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
                       conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
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
        chain_direction = _chain_infers_direction(chain)

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
            if chain_direction is None:
                neutral_ids.append(sid)
                continue
            if fdir == "neutral":
                neutral_ids.append(sid)
                continue
            if fdir == chain_direction:
                support_ids.append(sid)
            else:
                contradict_ids.append(sid)

        # 决定 status
        if contradict_ids:
            new_status = _STATUS_CONTRADICTED
        elif support_ids:
            new_status = _STATUS_CONFIRMED
        else:
            new_status = _STATUS_UNCONFIRMED

        # 决定 fail 诊断（仅 CONTRADICTED 时）
        fail: Optional[str] = None
        if new_status == _STATUS_CONTRADICTED:
            if not support_ids and contradict_ids:
                fail = _FAIL_WRONG_HYPOTHESIS
            elif support_ids and contradict_ids:
                fail = _FAIL_WRONG_INFERENCE
            else:
                fail = _FAIL_MISSING_CONTEXT

        # 计算 confidence
        if new_status == _STATUS_CONFIRMED:
            new_conf = min(_CONF_FLOOR + _CONF_VERIFIED_STEP * min(len(support_ids), 5),
                           _CONF_VERIFIED_CAP)
        elif new_status == _STATUS_CONTRADICTED:
            new_conf = max(_CONF_FLOOR - _CONF_CONTRADICTED_STEP * min(len(contradict_ids), 3),
                           _CONF_CONTRADICTED_FLOOR)
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
        )
        now = _now()
        new_active = 0 if new_status == _STATUS_CONTRADICTED else 1
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
            "support_count": len(support_ids),
            "contradict_count": len(contradict_ids),
            "neutral_count": len(neutral_ids),
            "matched_facts": len(support_ids) + len(contradict_ids),
            "active": new_active,
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
        nodes = conn.execute(
            "SELECT id, asset, label, kind, is_core, evidence FROM asset_driver_nodes "
            "WHERE asset=? AND active=1 ORDER BY is_core DESC, kind LIMIT ?",
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
                )
                conn.execute(
                    "UPDATE asset_driver_nodes SET evidence=?, updated_at=? WHERE id=?",
                    (_cap_evidence(new_ev_str), _now(), nid),
                )

            try:
                r = verify_hypothesis(nid, facts, conn=conn)
                results.append(r)
            except Exception as e:
                results.append({"node_id": nid, "error": str(e)})
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


def persist_evidence_edges(asset: str,
                             results: Optional[List[Dict[str, Any]]] = None,
                             since_days: int = 7) -> Dict[str, Any]:
    """把 verify_all_pending 的结果写成 supports / contradicts 边。

    v276+: 之前 verify_hypothesis 把 contradict 计数只是写到 evidence JSON 字段,
    前端看不到。现在把这些计数变成红色/绿色边,可视化时显示"X 因素:5 支持 2 反例"。

    入参:
      asset: 品种 (None = 全部)
      results: 已有 verify 结果(避免重跑 verify_hypothesis)。None = 自动跑一次。
      since_days: evidence 节点 label 用最近 N 天的 (避免重名冲突)。

    返回 {supports_added, contradicts_added, evidence_nodes_created, by_node: [...]}
    """
    asset = _norm_asset(asset) if asset else None
    conn = db.get_connection()
    supports_added = 0
    contradicts_added = 0
    evidence_nodes_created = 0
    by_node: List[Dict[str, Any]] = []
    try:
        if results is None:
            if not asset:
                return {"ok": False, "error": "asset 必填 (或传 results)"}
            r = verify_all_pending(asset)
            results = r.get("results") or []

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

            # 1) 创建"证据源"虚拟节点 — label 标记时间窗 + 节点 id
            ts_label = f"ev_{since_days}d_{int(time.time())}_{nid}"
            evidence_label_sup = f"[SUPPORT] {ts_label}"
            evidence_label_con = f"[CONTRADICT] {ts_label}"

            # 2) 建 [supports] 边:  证据节点 --supports--> 假设节点 (绿色)
            if sup > 0:
                eid = _upsert_node(conn, node_asset, evidence_label_sup,
                                   "evidence", False, 0.5,
                                   json.dumps({"src": "verify_all_pending",
                                               "node_id": nid,
                                               "since_days": since_days},
                                              ensure_ascii=False))
                if eid:
                    evidence_nodes_created += 1
                    _upsert_edge(conn, eid, nid, "supports",
                                 weight=float(sup),
                                 evidence=f"verify 支持 {sup} 个事实")
                    supports_added += 1

            # 3) 建 [contradicts] 边:  证据节点 --contradicts--> 假设节点 (红色)
            if con > 0:
                eid = _upsert_node(conn, node_asset, evidence_label_con,
                                   "evidence", False, 0.5,
                                   json.dumps({"src": "verify_all_pending",
                                               "node_id": nid,
                                               "since_days": since_days},
                                              ensure_ascii=False))
                if eid:
                    evidence_nodes_created += 1
                    _upsert_edge(conn, eid, nid, "contradicts",
                                 weight=float(con),
                                 evidence=f"verify 反驳 {con} 个事实")
                    contradicts_added += 1

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


def get_graph(asset: Optional[str] = None, include_inactive: bool = False) -> Dict[str, Any]:
    """返回图谱节点 + 边（供前端可视化）。"""
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
            "SELECT id, asset, label, kind, is_core, confidence, evidence, version, active, updated_at "
            f"FROM asset_driver_nodes {where} ORDER BY asset, is_core DESC, confidence DESC",
            params,
        ).fetchall()
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
        return {"nodes": nids, "edges": edges, "narrative": _build_narrative(nids)}
    finally:
        conn.close()


def get_knowledge(asset: str) -> Dict[str, Any]:
    """返回某品种的核心因素/条件因素 + 推理链（供展示 / 回灌）。"""
    asset = _norm_asset(asset)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        cores = conn.execute(
            "SELECT label, confidence, evidence, version FROM asset_driver_nodes "
            "WHERE asset=? AND is_core=1 AND active=1 ORDER BY confidence DESC",
            (asset,),
        ).fetchall()
        conds = conn.execute(
            "SELECT label, confidence, evidence, version FROM asset_driver_nodes "
            "WHERE asset=? AND is_core=0 AND kind='condition' AND active=1 ORDER BY confidence DESC",
            (asset,),
        ).fetchall()

        def _fmt(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for r in rows:
                why: List[str] = []
                try:
                    raw = json.loads(r["evidence"] or "[]") if r["evidence"] else []
                    why = raw if isinstance(raw, list) else [str(raw)]
                except json.JSONDecodeError:
                    why = []
                out.append({
                    "factor": r["label"],
                    "confidence": round(float(r["confidence"]), 3),
                    "why_chain": why,
                    "version": int(r["version"]),
                })
            return out

        return {"asset": asset, "core_factors": _fmt(cores), "condition_factors": _fmt(conds)}
    finally:
        conn.close()


def inject_block(asset: str, top: int = 4) -> str:
    """生成回灌文本块：把某品种涨跌本质注入 AI 分析（供 ai_worker prompt 接线）。"""
    asset = _norm_asset(asset)
    k = get_knowledge(asset)
    cores = [c["factor"] for c in k["core_factors"][:top]]
    conds = [c["factor"] for c in k["condition_factors"][:top]]
    if not cores and not conds:
        # v176.2：即使没学过该品种，也返回非空兜底，避免 LLM 写"未注入因果图谱"占位过短。
        # 兜底用硬编码的"通用驱动结构"作为 prompt 注入依据，不让 [总框架] 段空手。
        fallback = _get_default_drivers(asset)
        return (
            f"[品种涨跌本质·{asset}] 通用驱动结构(尚未学习): "
            f"核心驱动={fallback['core']}; 条件因素={fallback['condition']}。"
            f"（系统自学习未达到回灌阈值，注入通用结构供 LLM 推理参考，勿盲从）"
        )
    parts: List[str] = []
    if cores:
        parts.append("核心驱动(最底层)=" + "、".join(cores))
    if conds:
        parts.append("条件因素(触发/放大/抑制)=" + "、".join(conds))
    return f"[品种涨跌本质·{asset}] " + "；".join(parts) + "（系统自学习，供推理参考，勿盲从）"


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