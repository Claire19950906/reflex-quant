# -*- coding: utf-8 -*-
"""反思系统 - 6 个程序化校验器（v189 新增 1 个 → 6 个）。

每个 checker 输入：ai_decisions 行 (dict) + 上下文, 输出错误码列表。
  1. DataCheck        E001/E002  hard_data_evidence 真实性
  2. LogicCheck       E003       action vs factor_ranking 方向
  3. RuleCheck        E005       vs review_insights.active=1
  4. CausalCheck      E004       asset_driver_nodes 图谱匹配
  5. MarketCheck      E007       决策后 forward_pnl 走势
  6. EventLogicCheck  E014/E015/E016  v189 事件逻辑链层 — 分析页推理链/概念/跨资产方向
                       （反思系统主要工作；不删旧的 5 个 checker）
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import db


E001_DATA_MISSING = "E001"
# v225: E001 拆细为 6 个子码，避免一个"数据缺失"打天下（原来 78.6% 都归这）
E001a_MISSING_HARD_EVIDENCE = "E001a"   # 缺品种事实 (hard_data_evidence 0 条)
E001b_MISSING_ANCHOR_TS     = "E001b"   # 缺时间锚点 (data_anchor_ts 空/坏)
E001c_MISSING_FACTOR_DIR    = "E001c"   # 缺因子方向 (factor_ranking 空但有 action)
E001d_MISSING_EVENT_LINK    = "E001d"   # 缺事件链关联 (news_id/event_atom_ids 空)
E001e_MISSING_CROSS_ASSET   = "E001e"   # 缺跨资产验证 (cross_asset_likely 没在 evidence 里)
E001f_MISSING_BACKTEST_DATA = "E001f"   # 缺回测样本 (样本不足)
E002_DATA_STALE = "E002"
E003_LOGIC_CONTRADICT = "E003"
E004_CAUSAL_BROKEN = "E004"
E005_RULE_VIOLATION = "E005"
E006_SAMPLE_INSUFFICIENT = "E006"
E007_EVENT_TOO_OLD = "E007"
E008_ASSET_MISMATCH = "E008"
E009_CONFIDENCE_INFLATED = "E009"
E010_REVERSE_NO_ARG = "E010"
E011_FACT_WRONG = "E011"
E012_CHAIN_TOO_LONG = "E012"
E013_NARRATIVE_BIAS = "E013"
# v189 事件逻辑链层（最高权重；反思系统主要工作）
E014_REASONING_CHAIN_BROKEN = "E014"   # 因果图谱推理链错位
E015_CONCEPT_MISALIGNED = "E015"       # 概念错位/反共识缺位
E016_CROSS_ASSET_DIRECTION_WRONG = "E016"  # 跨资产传导方向错
E017_CROSS_ASSET_UNVERIFIED = "E017"     # v189 Item 4: cross_asset ↔ evidence 互证缺位
# v200+ Q5: 事件逻辑层加深 — 历史同类反例(同类 type_code 历史实际走势 vs 本次 LLM 方向单)
E018_HISTORICAL_COUNTERFACTUAL = "E018"  # 同类新闻历史真实走势与本次 LLM 方向相反
# v231+ logic_analysis 层 (形式逻辑有效性, 由 reflection_logic_analysis.py 写入)
# 注: 这些 code 不在 run_all_checkers() 触发 (那是 reflection_logic_analysis.py 单独跑)
#      但它们会通过 save_errors() 落 decision_errors, 所以这里注册 guidance + priority
E028_LOGIC_SELF_CONTRADICTION = "E028"  # 自相矛盾 — 单句/跨句正反方向词同现且无对比标记
E029_LOGIC_NON_SEQUITUR       = "E029"  # 推不出结论 — [总结] 引入方向词但 body 无支撑
E030_LOGIC_FALSE_DICHOTOMY    = "E030"  # 假二择一 — 要么A要么B 无第三选项
E031_LOGIC_CIRCULAR_REASONING = "E031"  # 循环论证 — 结论 = 前提 (Jaccard≥0.5)
E032_LOGIC_EQUIVOCATION       = "E032"  # 关键术语歧义 — 同术语被正负修饰
E033_LOGIC_POST_HOC           = "E033"  # 后此谬因 — 时间先 ≠ 因果

SEVERITY_INFO = 1
SEVERITY_WARN = 2
SEVERITY_CRITICAL = 3

# v189 Item 8: 层权重（事件逻辑链层 = 最高，反共识缺位 / 跨资产错向都属此类）
# weight = severity * 10 + layer_weight（让跨层同 severity 也有明确次序）
# v225+: 新增 reasoning_logic 层（E021-E027 LLM 推理质量）— 权重 90，介于 event_logic 与 causal 之间
# v231+: 新增 logic_analysis 层（E028-E033 形式逻辑有效性）— 权重 85, 介于 reasoning_logic 与 causal 之间
_LAYER_WEIGHT = {
    "event_logic":      100,  # E014/E015/E016 — v189 最高权重层（反思系统主要工作）
    "reasoning_logic":   90,  # E021-E027 — v225+ LLM 推理质量（次高权重）
    "logic_analysis":    85,  # E028-E033 — v231+ 形式逻辑有效性（推理结构本身）
    "causal":            60,  # E004 — 因果链错位
    "rule":              50,  # E005 — 硬铁律未引
    "logic":             40,  # E003 — 因子方向矛盾
    "data":              30,  # E001/E002 — 数据时效/缺铁律
    "market":            20,  # E006-E008 — 市场异常
}
_DEFAULT_LAYER_WEIGHT = 10


def _compute_weight(severity: int, source: str) -> int:
    """v189 Item 8: 权重 = severity * 10 + 层权重。"""
    return int(severity) * 10 + _LAYER_WEIGHT.get(source, _DEFAULT_LAYER_WEIGHT)


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json_loads(raw: Any, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _parse_factor_ranking(raw: str) -> List[Dict[str, str]]:
    data = _safe_json_loads(raw, default=[])
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _direction_to_sign(direction: str) -> int:
    s = str(direction or "").strip()
    sl = s.lower()
    # Chinese: 多 / 空（含 v189 扩展：上涨/跌、看多/空、看涨/跌、强势/弱势、+方向）
    if any(k in s for k in ("看多", "看涨", "上涨", "强势", "反弹", "上行", "攀升")) and "空" not in s:
        return +1
    if any(k in s for k in ("看空", "看跌", "下跌", "跌势", "弱势", "下行", "回调")) and "多" not in s:
        return -1
    if "多" in s and "空" not in s:
        return +1
    if "空" in s:
        return -1
    # English
    if any(t in sl for t in ("bull", "long", "up", "positive", "good", "+")):
        return +1
    if any(t in sl for t in ("bear", "short", "down", "negative", "bad", "-")):
        return -1
    return 0


def _action_to_sign(action: str) -> int:
    s = str(action or "").strip().upper()
    if s in ("BUY", "买涨", "做多", "LONG"):
        return +1
    if s in ("SELL", "买跌", "做空", "SHORT"):
        return -1
    return 0


def _count_evidence(hard_data: str) -> int:
    if not hard_data or hard_data.strip() in ("", "N/A", "AMBIGUOUS"):
        return 0
    parts = [p for p in re.split(r"[|;|\n]", hard_data) if p.strip() and "=" in p]
    return len(parts)


def _ts_age_minutes(anchor_ts: str, now: Optional[datetime] = None) -> Optional[float]:
    if not anchor_ts or anchor_ts.strip() in ("", "N/A", "AMBIGUOUS"):
        return None
    now = now or datetime.now()
    s = anchor_ts.replace("+00:00", "+0000")[:25]
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return (now - dt).total_seconds() / 60.0
        except ValueError:
            continue
    return None


# Checker 1: DataCheck
# v225: E001 拆细为 6 个子码（hard_evidence/anchor/factor_dir/event_link/cross_asset/backtest），
# 每个子码独立产出，避免一个"数据缺失"打天下（之前 78.6% 都归 E001，单一归类隐藏了真因）。
def check_data(decision):
    out = []
    decision_id = decision.get("id")
    hard_data = (decision.get("hard_data_evidence") or "").strip()
    anchor_ts = (decision.get("data_anchor_ts") or "").strip()
    factor_raw = decision.get("factor_ranking") or ""
    news_id = decision.get("news_id")
    event_atom_ids = decision.get("event_atom_ids")
    cross_asset_likely = decision.get("cross_asset_likely") or ""
    evidence_count = _count_evidence(hard_data)

    # E001a: 缺品种事实
    if evidence_count == 0:
        out.append({
            "error_code": E001a_MISSING_HARD_EVIDENCE,
            "severity": SEVERITY_WARN,
            "detail": "hard_data_evidence 缺少可解析数据条目（0 条带品种+时间+数值的事实）",
            "evidence_refs": {"field": "hard_data_evidence", "count": 0,
                              "sub_kind": "missing_hard_evidence"},
        })

    # E001b: 缺时间锚点（空 / 不可解析）
    if not anchor_ts:
        out.append({
            "error_code": E001b_MISSING_ANCHOR_TS,
            "severity": SEVERITY_INFO,
            "detail": "data_anchor_ts 为空（无最新时间锚点）",
            "evidence_refs": {"field": "data_anchor_ts", "value": "",
                              "sub_kind": "missing_anchor_ts"},
        })
    elif anchor_ts not in ("N/A", "AMBIGUOUS"):
        age_min = _ts_age_minutes(anchor_ts)
        if age_min is None:
            out.append({
                "error_code": E001b_MISSING_ANCHOR_TS,
                "severity": SEVERITY_INFO,
                "detail": f"data_anchor_ts 格式无法解析: {anchor_ts[:40]}",
                "evidence_refs": {"field": "data_anchor_ts", "value": anchor_ts,
                                  "sub_kind": "anchor_ts_unparseable"},
            })
        elif age_min > 30:
            # 仍报 E002（数据过期）作为时间维度
            severity = SEVERITY_CRITICAL if age_min > 120 else SEVERITY_WARN
            out.append({
                "error_code": E002_DATA_STALE,
                "severity": severity,
                "detail": f"data_anchor_ts 距今 {age_min:.0f} 分钟 (> 30 min 阈值)",
                "evidence_refs": {"field": "data_anchor_ts", "value": anchor_ts,
                                  "age_minutes": round(age_min, 1)},
            })

    # E001c: 缺因子方向（factor_ranking 空 + 有非 HOLD action）
    factors = _parse_factor_ranking(factor_raw)
    action = (decision.get("suggested_action") or "").strip().upper()
    if not factors and action not in ("HOLD", "未出手", "NONE", ""):
        out.append({
            "error_code": E001c_MISSING_FACTOR_DIR,
            "severity": SEVERITY_WARN,
            "detail": f"suggested_action={action} 但 factor_ranking 为空（无方向证据）",
            "evidence_refs": {"field": "factor_ranking", "action": action,
                              "sub_kind": "missing_factor_dir"},
        })

    # E001d: 缺事件链关联
    if not news_id and not event_atom_ids:
        out.append({
            "error_code": E001d_MISSING_EVENT_LINK,
            "severity": SEVERITY_INFO,
            "detail": "news_id / event_atom_ids 均为空（无事件链关联）",
            "evidence_refs": {"field": "event_link", "news_id": news_id,
                              "event_atom_ids": event_atom_ids,
                              "sub_kind": "missing_event_link"},
        })

    # E001e: 缺跨资产验证（cross_asset_likely 提到某品种/方向但 hard_data 没互证）
    if cross_asset_likely and cross_asset_likely not in ("N/A", "AMBIGUOUS", ""):
        ca_keywords = re.findall(r"\b(BTC|ETH|SOL|DXY|USD|VIX|SPX|NQ|GLD|TLT|UST10|GOLD|OIL|COPPER)\b",
                                 cross_asset_likely.upper())
        if ca_keywords:
            mentioned_in_evidence = 0
            for kw in set(ca_keywords):
                if kw in hard_data.upper():
                    mentioned_in_evidence += 1
            if mentioned_in_evidence < len(set(ca_keywords)):
                out.append({
                    "error_code": E001e_MISSING_CROSS_ASSET,
                    "severity": SEVERITY_WARN,
                    "detail": f"cross_asset_likely 提到 {list(set(ca_keywords))}，但 hard_data_evidence 仅 {mentioned_in_evidence}/{len(set(ca_keywords))} 有数值互证",
                    "evidence_refs": {"field": "cross_asset_likely", "value": cross_asset_likely,
                                      "mentioned_symbols": list(set(ca_keywords)),
                                      "verified_in_evidence": mentioned_in_evidence,
                                      "sub_kind": "missing_cross_asset_verify"},
                })

    # E001f: 缺回测样本（这一类由调用方触发，本 checker 仅产出 placeholder）
    sample_size = decision.get("backtest_sample_size")
    if sample_size is not None and 0 <= int(sample_size) < 30:
        out.append({
            "error_code": E001f_MISSING_BACKTEST_DATA,
            "severity": SEVERITY_WARN,
            "detail": f"回测样本仅 {int(sample_size)} 条 (< 30 阈值)",
            "evidence_refs": {"field": "backtest_sample_size", "value": int(sample_size),
                              "sub_kind": "missing_backtest_sample"},
        })

    score = float(decision.get("sentiment_score") or 0)
    if abs(score) > 0.7 and evidence_count < 3:
        out.append({
            "error_code": E009_CONFIDENCE_INFLATED,
            "severity": SEVERITY_WARN,
            "detail": f"sentiment_score={score:+.2f} 但 evidence 仅 {evidence_count} 条",
            "evidence_refs": {"field": "sentiment_score", "value": score,
                              "evidence_count": evidence_count},
        })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "data"
    return out


# Checker 2: LogicCheck
def check_logic(decision):
    out = []
    decision_id = decision.get("id")
    action = (decision.get("suggested_action") or "").strip().upper()
    factor_raw = decision.get("factor_ranking") or ""
    factors = _parse_factor_ranking(factor_raw)

    if not factors:
        # v225: factor_ranking 为空时，让 check_data 用 E001c（缺因子方向）报，
        # 这里不再重复报 E003（逻辑矛盾）。让错误码单一来源。
        return out

    signs = [_direction_to_sign(f.get("direction")) for f in factors]
    signs = [s for s in signs if s != 0]
    if not signs:
        return out

    pos = sum(1 for s in signs if s > 0)
    neg = sum(1 for s in signs if s < 0)
    majority = +1 if pos > neg else (-1 if neg > pos else 0)

    action_sign = _action_to_sign(action)
    if action in ("BUY", "SELL") and majority != 0 and action_sign != majority:
        out.append({
            "error_code": E003_LOGIC_CONTRADICT,
            "severity": SEVERITY_CRITICAL,
            "detail": (f"action={action} ({'+' if action_sign>0 else '-'}) "
                       f"但多数方向{'+' if majority>0 else '-'} ({pos}多 vs {neg}空)"),
            "evidence_refs": {"action": action, "factors": factors,
                              "pos": pos, "neg": neg},
        })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "logic"
    return out


# Checker 3: RuleCheck
def check_rules(decision, conn=None):
    out = []
    decision_id = decision.get("id")
    action = (decision.get("suggested_action") or "").strip().upper()
    reasoning = (decision.get("reasoning") or "")

    own = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            "SELECT id, rule FROM review_insights WHERE active = 1 AND kind = 0"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        if own:
            conn.close()

    if not rows:
        return out

    if action in ("BUY", "SELL"):
        hit_count = 0
        for r in rows:
            keywords = re.findall(r"[\u4e00-\u9fa5]{2,6}", r["rule"])
            if any(kw in reasoning for kw in keywords[:5]):
                hit_count += 1
        if hit_count == 0:
            out.append({
                "error_code": E005_RULE_VIOLATION,
                "severity": SEVERITY_WARN,
                "detail": f"决策 {action} 但 reasoning 未引用任何硬铁律 (kind=0 共{len(rows)}条)",
                "evidence_refs": {"action": action, "hard_rule_count": len(rows),
                                  "referenced": 0},
            })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "rule"
    return out


# Checker 4: CausalCheck
def check_causal(decision, conn=None):
    out = []
    decision_id = decision.get("id")
    asset = (decision.get("target_asset") or "").strip()
    factor_raw = decision.get("factor_ranking") or ""
    factors = _parse_factor_ranking(factor_raw)
    if not factors or not asset:
        return out

    own = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            "SELECT label FROM asset_driver_nodes WHERE asset = ? AND active = 1",
            (asset,),
        ).fetchall()
        node_labels = {r["label"] for r in rows}
    except sqlite3.OperationalError:
        node_labels = set()
    finally:
        if own:
            conn.close()

    if not node_labels:
        return out

    matched = 0
    unmatched_factors = []
    for f in factors:
        label = str(f.get("factor") or "").strip()
        if not label:
            continue
        hit = any(nl in label or label in nl for nl in node_labels)
        if hit:
            matched += 1
        else:
            unmatched_factors.append(label)

    if len(unmatched_factors) >= 2 and matched == 0:
        out.append({
            "error_code": E004_CAUSAL_BROKEN,
            "severity": SEVERITY_WARN,
            "detail": (f"target_asset={asset}, factor_ranking 全部 "
                       f"{len(unmatched_factors)} 条在图谱中无对应节点"),
            "evidence_refs": {"asset": asset, "unmatched": unmatched_factors[:5]},
        })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "causal"
    return out


# Checker 5: MarketCheck
def check_market(decision):
    out = []
    decision_id = decision.get("id")
    action = (decision.get("suggested_action") or "").strip().upper()
    if action not in ("BUY", "SELL"):
        return out

    forward_pnl = decision.get("forward_pnl")
    if forward_pnl is None:
        return out

    action_sign = _action_to_sign(action)
    is_wrong = (action_sign > 0 and forward_pnl < 0) or (action_sign < 0 and forward_pnl > 0)
    if is_wrong:
        severity = SEVERITY_CRITICAL if abs(forward_pnl) > 0.02 else SEVERITY_WARN
        out.append({
            "error_code": E007_EVENT_TOO_OLD,
            "severity": severity,
            "detail": (f"action={action} ({'+' if action_sign>0 else '-'}) "
                       f"但 forward_pnl={forward_pnl:+.4f} 反向"),
            "evidence_refs": {"action": action, "forward_pnl": forward_pnl},
        })

    mfe = decision.get("mfe_pct")
    mae = decision.get("mae_pct")
    if mfe is not None and mae is not None:
        if abs(mfe) < 0.003 and abs(mae) < 0.003:
            out.append({
                "error_code": E010_REVERSE_NO_ARG,
                "severity": SEVERITY_INFO,
                "detail": f"MFE={mfe:.4f}, MAE={mae:.4f} 均接近 0, 决策无市场反应",
                "evidence_refs": {"mfe_pct": mfe, "mae_pct": mae},
            })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "market"
    return out


# ─────────────────────────────────────────────────────────────────────
# v189 Checker 6: EventLogicCheck（事件逻辑链层 — 最高权重）
# 主战场：分析页（reasoning_path / factor_ranking / cross_asset_likely）
# 检查 3 件事（反思系统主要工作）：
#   E014 因果图谱推理链错位  reasoning_path 八段推理链是否完整
#   E015 概念错位/反共识缺位  factor_ranking 概念 ↔ asset_driver_nodes + 反向思考
#   E016 跨资产传导方向错    cross_asset_likely ↔ target_asset
# 重要：本 checker 产出 source='event_logic'，统计/注入都优先
# ─────────────────────────────────────────────────────────────────────

_ANALYSIS_STEPS = (
    "总框架", "因素定位", "驱动力", "水位博弈",
    "跨资产资金", "反共识结论", "逻辑自检", "总结",
)

_COUNTER_CONSENSUS_KEYWORDS = (
    # ── 中文：原 13 个 ──
    "反共识", "反向", "然而", "但是", "不过", "尽管", "即便", "反之",
    "另一种", "相反", "对立", "差异化", "相反地", "反向看", "是否成立",
    # ── 中文：v189 扩展（v189 Item 7：反向思考 / 风险提示 / 已定价）──
    "从反面", "反方观点", "反方", "反对", "反对者",
    "不利于", "不支持", "风险点", "风险提示", "警惕",
    "担忧", "顾虑", "误区", "误判", "不应", "不能", "否定",
    # 单字但（最常见连接词，但/但是均入）
    "但",
    "若不成立", "假设…错", "反向场景", "悲观情形", "乐观情形",
    "牛转熊", "熊转牛", "警惕", "盲点", "陷阱",
    "price-in", "priced in", "已反映", "已消化", "充分定价",
    "估值偏高", "估值偏低", "高估", "低估", "透支",
    # ── 英文（v189 Item 7：跨境英文新闻/英文模型输出需要）──
    "however", "but", "yet", "although", "though",
    "even though", "on the other hand", "counter", "contrary",
    "opposing", "opposite", "downside risk", "upside risk",
    "caveat", "bearish case", "bullish case", "alt view",
    "alternative view", "what if", "if wrong", "if this fails",
    "risk to view", "key risk", "main risk",
)


def _extract_reasoning_steps(reasoning: str) -> List[str]:
    if not reasoning:
        return []
    return [s for s in _ANALYSIS_STEPS if s in reasoning]


def _extract_factor_concepts(factor_raw) -> List[str]:
    if not factor_raw:
        return []
    try:
        data = json.loads(factor_raw) if isinstance(factor_raw, str) else factor_raw
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict):
            v = str(it.get("factor") or "").strip()
            if v:
                out.append(v)
    return out


def _extract_cross_asset_likely(decision) -> List[Dict[str, Any]]:
    """v189 Item 4: 同时支持两种 schema ——
    1. list[dict] {what/asset + direction/dir}
    2. list[str] "ETH上涨" / "BTC 跌" 等短串（自动拆 asset+方向）
    3. mixed 混合格式
    """
    raw = decision.get("cross_asset_likely") or decision.get("cross_asset") or "[]"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError):
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for x in raw:
        if isinstance(x, dict):
            if x.get("what") or x.get("asset") or x.get("direction") or x.get("dir"):
                out.append(x)
        elif isinstance(x, str):
            s = x.strip()
            if not s:
                continue
            # 拆 "ETH上涨" → {"what": "ETH", "direction": "上涨"}
            # 拆 "BTC 跌" → {"what": "BTC", "direction": "跌"}
            m = re.match(r"^([A-Za-z0-9]+)\s*(.*)$", s)
            if m:
                asset, rest = m.group(1), m.group(2).strip()
                if rest:
                    out.append({"what": asset.upper(), "direction": rest})
                else:
                    out.append({"what": asset.upper(), "direction": ""})
    return out


def _target_kind(target_asset: str) -> str:
    s = str(target_asset or "").strip().upper()
    if s in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LTC", "DOT", "LINK", "AVAX", "MATIC"):
        return "crypto"
    if s in ("XAU", "XAG", "GOLD", "SILVER", "WTI", "BRENT", "USOIL", "UKOIL", "HG", "COPPER"):
        return "commodity"
    if s in ("DXY", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCNH", "USDCNY"):
        return "fx"
    if s in ("US10Y", "US02Y", "US30Y", "BUND", "GILT", "JGB", "CGB", "UST", "TLT"):
        return "bond"
    if s in ("SPX", "NDX", "DJI", "SPY", "QQQ"):
        return "equity_us"
    if s in ("000001", "000300", "600519", "MAOTAI"):
        return "equity_cn"
    return "other"


# ═══════════════════════════════════════════════════════════════════════════
# 反思系统 v225+ · reasoning_logic 层 · 7 个 LLM 推理质量 checker
# E021-E027，source='reasoning_logic'，全部平铺、无 severity 视觉分层
# 与 event_logic 区别: event_logic 查 8 段/概念/反共识/跨资产方向(整体结构)
#                    reasoning_logic 查 假设/反事实/校准/历史/跨资产一致/因果/置信-证据
# ═══════════════════════════════════════════════════════════════════════════

E021_ASSUMPTION_PREMISE_FAILED = "E021"
E022_COUNTERFACTUAL_NOT_CONSIDERED = "E022"
E023_CONFIDENCE_NOT_CALIBRATED = "E023"
E024_HISTORICAL_PATTERN_MISREAD = "E024"
E025_CROSS_ASSET_MISREAD = "E025"
E026_CAUSAL_MECHANISM_MISSING = "E026"
E027_CONFIDENCE_WITHOUT_EVIDENCE = "E027"

_ASSUMPTION_KW = ("假设", "前提", "假定", "预设", "若 ", "如果", "基于此", "建立在",
                  "assuming", "premise", "given that", "provided", "presuppose")
_COUNTERFACTUAL_KW = ("反之", "否则", "如果失败", "如果错误", "反方向", "对立",
                      "but if", "otherwise", "conversely", "if not", "what if",
                      "反向", "相反方向", "反例", "反共识", "反向思考", "对冲情景")
_CAUSAL_KW_BEFORE = ("因为", "由于", "源于", "源自", "基于", "在.*作用下",
                     "due to", "because", "driven by", "owing to")
_CAUSAL_KW_AFTER = ("所以", "因此", "导致", "故", "使得", "引发", "促成",
                    "therefore", "thus", "hence", "leading to", "results in")
_HISTORICAL_KW = ("上次", "历史", "历史上", "此前", "复盘", "回看", "过去", "当时",
                  "案例", "同类", "去年", "上个月", "三月", "回顾",
                  "historically", "last time", "previously", "prior", "recall",
                  "similar case", "回顾")


def _contains_any(text, keywords) -> bool:
    if not text:
        return False
    return any(k in text for k in keywords)


def check_event_logic(decision, conn=None):
    """v189 事件逻辑链层 — 反思系统主要工作。

    产出 source='event_logic' 的错误（在 format_compact_guidance / 注入 / 排序时优先）。
    不删旧 5 个 checker；本 checker 仅 ADD。
    """
    out: List[Dict[str, Any]] = []
    decision_id = decision.get("id")
    target_asset = (decision.get("target_asset") or "").strip()
    action = (decision.get("suggested_action") or "").strip().upper()
    reasoning = decision.get("reasoning") or decision.get("reasoning_path") or ""
    factor_raw = decision.get("factor_ranking") or "[]"
    cross = _extract_cross_asset_likely(decision)

    # E014 推理链错位 —— 八段缺失 ≥ 4 → critical；2~3 → warn
    steps = _extract_reasoning_steps(reasoning)
    missing = [s for s in _ANALYSIS_STEPS if s not in steps]
    if action in ("BUY", "SELL"):
        miss_n = len(missing)
        if miss_n >= 4:
            out.append({
                "error_code": E014_REASONING_CHAIN_BROKEN,
                "severity": SEVERITY_CRITICAL,
                "detail": (f"reasoning_path 仅含 {len(steps)}/8 阶段，"
                           f"缺 {miss_n} 段（{', '.join(missing)}）→ 因果链断裂"),
                "evidence_refs": {
                    "found_steps": steps,
                    "missing_steps": missing,
                    "missing_count": miss_n,
                    "field": "reasoning_path",
                },
            })
        elif miss_n >= 2:
            out.append({
                "error_code": E014_REASONING_CHAIN_BROKEN,
                "severity": SEVERITY_WARN,
                "detail": (f"reasoning_path 缺 {miss_n} 段"
                           f"({', '.join(missing)}) → 推理链不完整"),
                "evidence_refs": {
                    "found_steps": steps,
                    "missing_steps": missing,
                    "missing_count": miss_n,
                },
            })

    # E015 概念错位/反共识缺位
    concepts = _extract_factor_concepts(factor_raw)
    if action in ("BUY", "SELL") and concepts:
        own = conn is None
        conn2 = conn or _connect()
        rows = []
        try:
            if target_asset:
                rows = conn2.execute(
                    "SELECT label, is_core FROM asset_driver_nodes"
                    " WHERE asset IN (?, 'all') AND active = 1",
                    (target_asset,),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            if own and conn2 is not conn:
                conn2.close()
        node_labels = [(r["label"], int(r["is_core"] or 0)) for r in rows]
        if node_labels:
            matched_core = 0
            unmatched = []
            for c in concepts:
                hit_core = any(
                    (nl and (nl in c or c in nl))
                    for nl, core in node_labels if core
                )
                if hit_core:
                    matched_core += 1
                hit_any = any(
                    (nl and (nl in c or c in nl))
                    for nl, _ in node_labels
                )
                if not hit_any:
                    unmatched.append(c)
            if matched_core == 0 and len(concepts) >= 2:
                out.append({
                    "error_code": E015_CONCEPT_MISALIGNED,
                    "severity": SEVERITY_WARN,
                    "detail": (f"factor_ranking {len(concepts)} 个概念均未命中"
                               f"{target_asset} 的核心驱动节点"),
                    "evidence_refs": {
                        "concepts": concepts,
                        "core_nodes": [nl for nl, core in node_labels if core][:5],
                        "unmatched": unmatched[:5],
                    },
                })
        # 反共识缺位
        if action in ("BUY", "SELL"):
            # v189 Item 7: 大小写不敏感（英文关键词需要）
            reasoning_lower = reasoning.lower()
            has_counter = any(
                kw.lower() in reasoning_lower
                for kw in _COUNTER_CONSENSUS_KEYWORDS
            )
            if not has_counter and len(reasoning) > 60:
                out.append({
                    "error_code": E015_CONCEPT_MISALIGNED,
                    "severity": SEVERITY_WARN,
                    "detail": (f"reasoning {len(reasoning)} 字无任何反向思考词 → 单边叙事风险"),
                    "evidence_refs": {
                        "reasoning_len": len(reasoning),
                        "counter_keywords_searched": list(_COUNTER_CONSENSUS_KEYWORDS)[:5],
                    },
                })

    # E016 跨资产传导方向错
    if action in ("BUY", "SELL") and cross:
        action_sign = _action_to_sign(action)
        kind = _target_kind(target_asset)
        conflicts: List[Dict[str, Any]] = []
        for c in cross[:20]:
            c_asset = str(c.get("what") or c.get("asset") or "").upper()
            c_dir = str(c.get("direction") or c.get("dir") or "").strip()
            c_sign = _direction_to_sign(c_dir)
            if not c_asset or c_sign == 0:
                continue
            same_kind = False
            if kind == "crypto" and c_asset in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA"):
                same_kind = True
            if kind == "commodity" and c_asset in ("XAU", "XAG", "WTI", "BRENT", "HG"):
                same_kind = True
            if not same_kind:
                continue
            if action_sign != 0 and c_sign != 0 and action_sign != c_sign:
                conflicts.append({
                    "what": c_asset, "direction": c_dir,
                    "action_sign": action_sign, "cross_sign": c_sign,
                })
        if conflicts:
            out.append({
                "error_code": E016_CROSS_ASSET_DIRECTION_WRONG,
                "severity": SEVERITY_WARN,
                "detail": (f"action={action} ({target_asset}) 但同向品种"
                           f"{len(conflicts)} 项反向"
                           f"（{', '.join(c['what'] for c in conflicts[:3])}）"),
                "evidence_refs": {
                    "target_asset": target_asset,
                    "conflicts": conflicts[:5],
                },
            })

    # E017 cross_asset ↔ evidence 互证（v189 Item 4）
    #   每条 cross_asset_likely entry 的 direction 应有 evidence / news / reasoning 互证
    #   互证不足 → 该 entry 是"无据传导"，可能误导决策 → warn
    if action in ("BUY", "SELL") and cross:
        hard_data_lower = (decision.get("hard_data_evidence") or "").lower()
        news_text = (decision.get("news_text") or decision.get("news_summary") or "").lower()
        reasoning_lower_full = reasoning.lower()
        unverified: List[Dict[str, Any]] = []
        for c in cross[:20]:
            c_asset = str(c.get("what") or c.get("asset") or "").upper()
            c_dir = str(c.get("direction") or c.get("dir") or "").strip()
            if not c_asset or not c_dir:
                continue
            # 互证 3 源：hard_data_evidence / news_text / reasoning
            token = c_asset.lower()
            in_evidence = bool(token) and (
                token in hard_data_lower
                or c_asset in (decision.get("hard_data_evidence") or "")
            )
            in_news = bool(token) and token in news_text
            in_reasoning = bool(token) and token in reasoning_lower_full
            # 方向词也要互证（避免"ETH出现"但方向反着说）
            dir_sign = _direction_to_sign(c_dir)
            dir_word_match = False
            if dir_sign > 0:
                for w in ("涨", "up", "long", "bull", "+", "↑"):
                    if w in c_dir.lower() or w in reasoning_lower_full:
                        dir_word_match = True
                        break
            elif dir_sign < 0:
                for w in ("跌", "down", "short", "bear", "-", "↓"):
                    if w in c_dir.lower() or w in reasoning_lower_full:
                        dir_word_match = True
                        break
            if not (in_evidence or in_news or (in_reasoning and dir_word_match)):
                unverified.append({
                    "what": c_asset,
                    "direction": c_dir,
                    "checked_sources": {
                        "in_evidence": in_evidence,
                        "in_news": in_news,
                        "in_reasoning": in_reasoning,
                        "dir_word_match": dir_word_match,
                    },
                })
        if unverified:
            out.append({
                "error_code": E017_CROSS_ASSET_UNVERIFIED,
                "severity": SEVERITY_WARN,
                "detail": (f"cross_asset_likely {len(unverified)}/{len(cross[:20])} 项无"
                           f"evidence/news/reasoning 互证"
                           f"（{', '.join(c['what'] for c in unverified[:3])}）"),
                "evidence_refs": {
                    "unverified": unverified[:5],
                    "total_cross": len(cross[:20]),
                    "hint": "每条 cross_asset 需在 hard_data_evidence / news_text / reasoning 中至少一项提及同 asset+方向",
                },
            })

    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "event_logic"
    return out


# Aggregation entry
def run_all_checkers(decision, conn=None):
    """v200 P0+: 入口处加 completeness 过滤 + 新增 E019 (LLM incomplete).

    过滤原则: completeness_score < 0.7 的脏决策 → 不入反射检查器,
    仅记录 E019 标记后跳过. 因为:
      - 缺 reasoning/asset_reasoning/invalidation_condition 的决策, 
        check_logic / check_rules / check_event_logic / check_historical_counterfactual
        都会跑在脏数据上 → 误报 + 污染
      - 反思系统拿到的 N 个错误里大多是"基于假字符串的逻辑矛盾",
        写进 decision_errors / 24h daemon / review_insights → 闭环污染
      - LLM 的目的是用「真观察」帮我们识别问题, 不是给 fake reasoning 触发假校验
    """
    # v200 P0+: 完整性过滤(< 0.7 跳过全部 checker, 只记录 E019)
    _comp = float(decision.get("completeness_score") or 1.0)
    if _comp < 0.7:
        return [_make_E019_low_completeness(decision, _comp)]
    results = []
    results.extend(check_data(decision))
    results.extend(check_logic(decision))
    results.extend(check_rules(decision, conn=conn))
    results.extend(check_causal(decision, conn=conn))
    results.extend(check_market(decision))
    # v189 事件逻辑链层（最高权重）— 反思系统主要工作
    results.extend(check_event_logic(decision, conn=conn))
    # v200+ Q5: 事件逻辑层加深 — 同类新闻历史真实反例
    results.extend(check_historical_counterfactual(decision, conn=conn))
    # v202+: 样本质量检查 — trade 是事故样本时拒绝计入 alpha (E020)
    results.extend(check_sample_integrity(decision, conn=conn))
    # v225+: reasoning_logic 层 — 7 个 LLM 推理质量 checker (E021-E027)
    results.extend(check_reasoning_logic(decision, conn=conn))
    return results


E019_LLM_INCOMPLETE = "E019"  # v200 P0+: 完整性过滤, 反思系统跳过脏决策
E020_SAMPLE_INVALID = "E020"  # v202+: trade 不满足样本质量 5 条硬规则, 反思系统拒绝计入 alpha


def _make_E019_low_completeness(decision, completeness_score: float):
    """v200 P0+: 完整性太低, 反思系统跳过 + 标记 E019.

    错误字段结构与 check_xxx() 系列保持一致, 让决策错误落库正常.
    severity=INFO(不升级), source=data(低权重), 因为不是真错,只是数据质量问题.
    """
    return {
        "error_code": E019_LLM_INCOMPLETE,
        "code": E019_LLM_INCOMPLETE,  # 兼容别名
        "severity": SEVERITY_INFO,
        "source": "data",
        "layer": "data",
        "sub_layer": "completeness",
        "signal_name": "llm_incomplete",
        "detail": f"completeness_score={completeness_score:.2f}<0.7, 反思系统跳过避免脏数据污染",
        "evidence": json.dumps({
            "completeness_score": completeness_score,
            "missing_fields": decision.get("missing_fields") or "[]",
            "field_sources": decision.get("field_sources") or "{}",
        }, ensure_ascii=False),
        "decision_id": decision.get("id") or decision.get("decision_id"),
        "origin": "checker",
        "weight": _compute_weight(SEVERITY_INFO, "data"),  # 低权重, 不阻塞其他反思
    }


def check_reasoning_logic(decision, conn=None):
    """v225+ reasoning_logic 层 — 反思系统主要工作(7 个 LLM 推理质量 checker)。

    与 check_event_logic 区别:
      event_logic    → 8 段推理链 / 概念对齐 / 反共识 / 跨资产方向(整体结构)
      reasoning_logic → 假设 / 反事实 / 校准 / 历史 / 跨资产一致 / 因果 / 置信度-证据

    7 个 E0XX 都 source='reasoning_logic'，severity 平铺(无级别视觉分层)。
    """
    out: List[Dict[str, Any]] = []
    action = (decision.get("suggested_action") or "").strip().upper()
    reasoning = decision.get("reasoning") or decision.get("reasoning_path") or ""
    hard_data = (decision.get("hard_data_evidence") or "").strip()

    # 只对 BUY/SELL 决策跑(HOLD 推理质量差不影响持仓)
    if action not in ("BUY", "SELL"):
        return out

    cross = _extract_cross_asset_likely(decision)
    is_correct = decision.get("is_correct")
    forward_pnl = decision.get("forward_pnl")
    evidence_conf = float(decision.get("evidence_confidence") or 0)
    target_asset = (decision.get("target_asset") or "").strip().upper()

    # E021 假设/前提未明示
    if not _contains_any(reasoning, _ASSUMPTION_KW):
        out.append({
            "error_code": E021_ASSUMPTION_PREMISE_FAILED,
            "severity": SEVERITY_WARN,
            "source": "reasoning_logic",
            "detail": ("reasoning_path 未明示任何前提/假设（找不到『假设/前提/若/如果』"
                       "等关键词）→ 高风险决策无显式前提"),
            "evidence_refs": {
                "field": "reasoning", "length": len(reasoning),
                "assumption_kw_found": False, "sub_kind": "assumption_unstated",
            },
        })

    # E022 反事实未考虑
    if not _contains_any(reasoning, _COUNTERFACTUAL_KW):
        out.append({
            "error_code": E022_COUNTERFACTUAL_NOT_CONSIDERED,
            "severity": SEVERITY_WARN,
            "source": "reasoning_logic",
            "detail": ("reasoning_path 无反向/反事实/反之 关键词 → 单边叙事，"
                       "未考虑『如果错了会怎样』"),
            "evidence_refs": {
                "field": "reasoning", "counterfactual_kw_found": False,
                "sub_kind": "counterfactual_missing",
            },
        })

    # E023 置信度未校准 — 高置信但亏损
    if (is_correct == 0 and evidence_conf >= 70
            and forward_pnl is not None and float(forward_pnl) < -0.5):
        out.append({
            "error_code": E023_CONFIDENCE_NOT_CALIBRATED,
            "severity": SEVERITY_CRITICAL,
            "source": "reasoning_logic",
            "detail": (f"evidence_confidence={evidence_conf:.0f} 但 is_correct=0 且 "
                       f"forward_pnl={forward_pnl:+.2f}% → 置信度严重虚高"),
            "evidence_refs": {
                "evidence_confidence": evidence_conf, "is_correct": is_correct,
                "forward_pnl": float(forward_pnl), "sub_kind": "confidence_overstated",
            },
        })

    # E024 历史反例/同类案例未引
    if not _contains_any(reasoning, _HISTORICAL_KW):
        out.append({
            "error_code": E024_HISTORICAL_PATTERN_MISREAD,
            "severity": SEVERITY_INFO,
            "source": "reasoning_logic",
            "detail": ("reasoning_path 未引用任何历史同类案例/过去同类场景 → "
                       "未参考先例"),
            "evidence_refs": {
                "field": "reasoning", "historical_kw_found": False,
                "sub_kind": "historical_uncited",
            },
        })

    # E025 跨资产提及但方向不一致
    # v225+: 跨资产方向判断需要 asset-pair awareness：
    #   - 直相关（同涨同跌）：equity ↔ equity, crypto ↔ crypto, commodity ↔ commodity
    #   - 反相关（一涨一跌）：XAU ↔ DXY, XAU ↔ US10Y, BTC ↔ DXY
    # 字典只列"目标品种"+"跨资产品种"→反相关对；其余按直相关处理
    _INVERSE_PAIRS = {
        ("XAU", "DXY"): True, ("XAU", "US10Y"): True, ("XAU", "USD"): True,
        ("XAG", "DXY"): True, ("XAG", "US10Y"): True,
        ("GOLD", "USD"): True,
        ("BTC", "DXY"): True, ("ETH", "DXY"): True,
    }
    target_kind_v = _target_kind(target_asset)
    if cross:
        bad = []
        for x in cross:
            d = str(x.get("direction") or "")
            w = str(x.get("what") or x.get("asset") or "").upper()
            if not d or not w:
                continue
            d_low = d.lower()
            up_kw = ("涨", "多", "long", "up", "buy", "rise")
            down_kw = ("跌", "空", "short", "down", "sell", "fall")
            # 是否反相关对？
            is_inverse = (target_kind_v in ("commodity", "crypto")
                          and (target_asset, w) in _INVERSE_PAIRS)
            if is_inverse:
                # 反相关：cross 涨 = 目标应跌；cross 跌 = 目标应涨
                if action == "BUY" and any(k in d_low for k in up_kw):
                    bad.append({"asset": w, "direction": d, "expected": "下跌(反相关)"})
                elif action == "SELL" and any(k in d_low for k in down_kw):
                    bad.append({"asset": w, "direction": d, "expected": "上涨(反相关)"})
            else:
                # 直相关：cross 涨 = 目标应涨
                if action == "BUY" and any(k in d_low for k in down_kw):
                    bad.append({"asset": w, "direction": d, "expected": "上涨"})
                elif action == "SELL" and any(k in d_low for k in up_kw):
                    bad.append({"asset": w, "direction": d, "expected": "下跌"})
        if bad:
            out.append({
                "error_code": E025_CROSS_ASSET_MISREAD,
                "severity": SEVERITY_CRITICAL,
                "source": "reasoning_logic",
                "detail": (f"action={action} 但 cross_asset_likely 中 "
                           f"{len(bad)} 条同向品种方向相反 → "
                           f"{', '.join(b['asset']+b['direction'] for b in bad[:3])}"),
                "evidence_refs": {
                    "action": action, "bad_cross_assets": bad[:5],
                    "sub_kind": "cross_asset_inconsistent",
                },
            })

    # E026 因果机制不明确 — 缺完整 因→果 关键词
    has_cause = _contains_any(reasoning, _CAUSAL_KW_BEFORE)
    has_effect = _contains_any(reasoning, _CAUSAL_KW_AFTER)
    if not (has_cause and has_effect):
        out.append({
            "error_code": E026_CAUSAL_MECHANISM_MISSING,
            "severity": SEVERITY_WARN,
            "source": "reasoning_logic",
            "detail": (f"reasoning 因果机制关键词不全：因侧={has_cause}, "
                       f"果侧={has_effect} → 缺完整『因为…所以…』结构"),
            "evidence_refs": {
                "has_cause_kw": has_cause, "has_effect_kw": has_effect,
                "sub_kind": "causal_mechanism_vague",
            },
        })

    # E027 置信度高但 evidence_count 不足
    ev_count = _count_evidence(hard_data)
    if evidence_conf >= 65 and ev_count < 2:
        out.append({
            "error_code": E027_CONFIDENCE_WITHOUT_EVIDENCE,
            "severity": SEVERITY_WARN,
            "source": "reasoning_logic",
            "detail": (f"evidence_confidence={evidence_conf:.0f} 但 hard_data_evidence "
                       f"仅 {ev_count} 条可解析事实 → 置信度与证据量不匹配"),
            "evidence_refs": {
                "evidence_confidence": evidence_conf, "evidence_count": ev_count,
                "sub_kind": "confidence_evidence_mismatch",
            },
        })

    return out


def check_sample_integrity(decision, conn=None):
    """v202+: 样本质量检查 — trade 不满足 5 条硬规则 → E020.

    背景: V4-V8 ablation 跑出"XAU BUY 方向 alpha +46%"等结论,
    但 V8 验证发现全部 24 笔 trade 都是事故样本:
      - trade_gate_reason 非空 (gate 失败)
      - evidence_action=HOLD (AI 建议观望)
      - 8/24 笔 pnl=0 (K 线 fallback 占位)

    反思系统之前没专门检查这个盲区。本 checker 接入 sample_quality_filter:
      R1. trade_gate_reason 为空
      R2. evidence_action != 'HOLD'
      R3. pnl != 0 占位
      R4. asset 在 allowlist
      R7. evidence_confidence >= 阈值

    不满足任一 → E020 (severity=INFO 但 weight 高, 反思系统拒绝计入 alpha 评估)。

    注: 本 checker 不修改 quick_sim_trades / ai_decisions 任何字段,
    也不动 DB schema; 只是把审计结果以错误码形式输出。
    """
    out: List[Dict[str, Any]] = []
    decision_id = decision.get("id")
    if not decision_id:
        return out

    own = conn is None
    conn2 = conn or _connect()
    try:
        # 找对应 trade (通过 decision_id)
        row = conn2.execute(
            "SELECT t.id, t.asset, t.action, t.pnl_pct, "
            "d.trade_gate_reason, d.evidence_action, d.evidence_confidence "
            "FROM quick_sim_trades t "
            "LEFT JOIN ai_decisions d ON t.decision_id=d.id "
            "WHERE t.decision_id = ? AND t.settled=1 LIMIT 1",
            (decision_id,)
        ).fetchone()
        if row is None:
            # 该 decision 没有对应 settled trade, 不算样本
            return out

        try:
            import sample_quality_filter as sqf
            audit = sqf._check_one(
                row, row,
                sqf.DEFAULT_SIM_ALLOWED_ASSETS,
                sqf.DEFAULT_MIN_CONFIDENCE,
            )
        except ImportError:
            # 兜底: sample_quality_filter 不可用, 手动跑 R1/R2
            gate = row['trade_gate_reason'] or ''
            ev_act = row['evidence_action'] or ''
            reasons = []
            if gate.strip(): reasons.append('R1:trade_gate_reason非空')
            if ev_act.strip().upper() == 'HOLD': reasons.append('R2:evidence_action=HOLD')
            audit = {
                'is_valid_for_alpha': len(reasons) == 0,
                'reasons': reasons,
                'flags': {'R1_gate_passed': not gate.strip(),
                          'R2_evidence_action': ev_act.strip().upper() != 'HOLD'},
            }

        if not audit['is_valid_for_alpha']:
            out.append({
                "error_code": E020_SAMPLE_INVALID,
                "code": E020_SAMPLE_INVALID,
                "severity": SEVERITY_INFO,
                "source": "data",
                "layer": "data",
                "sub_layer": "sample_integrity",
                "signal_name": "trade_not_valid_for_alpha",
                "detail": "trade 不满足样本质量 5 条硬规则: {}".format('; '.join(audit['reasons'])),
                "evidence": json.dumps({
                    "trade_id": row['id'],
                    "asset": row['asset'],
                    "action": row['action'],
                    "pnl_pct": row['pnl_pct'],
                    "flags": audit['flags'],
                    "reasons": audit['reasons'],
                    "audit_module": "sample_quality_filter.v202",
                }, ensure_ascii=False),
                "decision_id": decision_id,
                "origin": "checker",
                # severity=INFO 但 source=data, weight 与 E019 同档, 不阻塞
                "weight": _compute_weight(SEVERITY_INFO, "data"),
            })
        return out
    finally:
        if own: conn2.close()


def check_historical_counterfactual(decision, conn=None):
    """v200+ Q5: 反思系统事件逻辑层加深。

    之前 check_event_logic 只检查结构性(步骤是否齐全、跨资产方向是否互证),
    没有检查「实质逻辑性」—— 同 type_code 同 asset 的历史同类新闻,实际 forward_pnl 与
    本次 LLM 给的方向单背离？

    触发条件:
      - LLM 给 BUY/SELL(主方向单)
      - 同 (type_code, asset) 历史样本 ≥ 3 且 win_rate ≤ 33% 且 avg_pnl_4h ≤ -0.005
      - 这时 LLM 的"本次同向"是逆势 → critical 错误

    失败/无样本 → 返回 [] (静默)。

    来源:signal_performance JOIN ai_decisions JOIN event_atom → 30d 窗口聚合。
    注:历史样本本身来自 ai_decisions,所以 ground-truth 不含本次。
    """
    out: List[Dict[str, Any]] = []
    decision_id = decision.get("id")
    action = (decision.get("suggested_action") or "").strip().upper()
    target_asset = (decision.get("target_asset") or "").strip().upper()
    event_type_code = (decision.get("event_type_code") or "").strip().lower()
    if action not in ("BUY", "SELL"):
        return out
    if not target_asset or target_asset in ("NONE", "N/A", ""):
        return out
    # event_type_code 缺失 → 没法查历史同类, 跳过
    if not event_type_code:
        return out

    own = conn is None
    conn2 = conn or _connect()
    try:
        # 历史 30d 同 (type_code, asset) 真实 forward_pnl 聚合
        # 注: signal_performance.is_correct='HOLD' 已被过滤 (避免未结样本污染)
        # v189: avg_pnl_4h 单位是「百分比」, 即 0.01 = 1%
        rows = conn2.execute(
            """
            SELECT COUNT(*) AS sample_n,
                   SUM(CASE WHEN sp.is_correct = 'WIN' THEN 1 ELSE 0 END) AS win_n,
                   SUM(CASE WHEN sp.is_correct = 'LOSS' THEN 1 ELSE 0 END) AS loss_n,
                   AVG(sp.forward_pnl) AS avg_pnl
            FROM signal_performance sp
            JOIN ai_decisions ad ON ad.id = sp.decision_id
            WHERE ad.event_type_code = ?
              AND UPPER(COALESCE(ad.asset, '')) = ?
              AND sp.is_correct IN ('WIN', 'LOSS')
              AND sp.forward_pnl IS NOT NULL
              AND sp.ts >= CAST(strftime('%s','now') AS INTEGER) - 30*86400
            """,
            (event_type_code, target_asset),
        ).fetchone()
    except sqlite3.OperationalError:
        return out
    finally:
        if own and conn2 is not conn:
            conn2.close()

    if not rows:
        return out
    sample_n = int(rows["sample_n"] or 0)
    win_n = int(rows["win_n"] or 0)
    loss_n = int(rows["loss_n"] or 0)
    avg_pnl = float(rows["avg_pnl"] or 0.0)
    if sample_n < 3:
        return out
    win_rate = win_n / sample_n if sample_n > 0 else 0.0
    # 反例触发: 历史胜率 ≤ 33% 且平均亏损 ≥ -0.5%
    # 例: 30d 内 8 次同类新闻, 2 赢 6 输, 平均 -1.2% → 触发 E018
    if win_rate <= 0.33 and avg_pnl <= -0.005:
        out.append({
            "error_code": E018_HISTORICAL_COUNTERFACTUAL,
            "severity": SEVERITY_CRITICAL,
            "detail": (
                f"同类新闻({event_type_code}, {target_asset})过去 30d 有 "
                f"{sample_n} 条,胜率仅 {win_rate:.0%} ({win_n}胜/{loss_n}负),"
                f"平均 4h 收益 {avg_pnl:+.2%};本次 LLM 给 {action} 实质同向,"
                f"但历史样本反向 → 该类事件过往表现对本次方向无支撑"
            ),
            "evidence_refs": {
                "event_type_code": event_type_code,
                "target_asset": target_asset,
                "sample_n": sample_n,
                "win_n": win_n,
                "loss_n": loss_n,
                "win_rate": round(win_rate, 3),
                "avg_pnl_4h": round(avg_pnl, 4),
                "current_action": action,
                "lookback_days": 30,
                "hint": "同类新闻历史反向, 建议 HOLD 或显式引用反例作为风险提示",
            },
        })
    for err in out:
        err["decision_id"] = decision_id
        err["origin"] = "checker"
        err["source"] = "event_logic"  # 与 E014-E017 同层, 最高权重
    return out


def save_errors(errors, conn=None):
    if not errors:
        return 0
    own = conn is None
    conn = conn or _connect()
    inserted = 0
    try:
        for e in errors:
            try:
                severity = int(e.get("severity", SEVERITY_WARN))
                source = e.get("source", "") or ""
                # v189 Item 8: 写入 weight 列（severity*10 + 层权重）
                weight = _compute_weight(severity, source)
                conn.execute(
                    """INSERT OR IGNORE INTO decision_errors
                       (decision_id, error_code, origin, severity, detail,
                        evidence_refs, source, weight, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        e.get("decision_id"),
                        e.get("error_code"),
                        e.get("origin", "checker"),
                        severity,
                        str(e.get("detail", "")),
                        json.dumps(e.get("evidence_refs", {}), ensure_ascii=False),
                        source,
                        weight,
                    ),
                )
                inserted += 1
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                continue
        conn.commit()
    finally:
        if own:
            conn.close()
    return inserted


def get_errors_for_decision(decision_id, conn=None):
    """查询某决策的所有错误。

    v189 Item 8: 跨层排序用 weight 列（severity*10 + layer_weight），
    取代 v189 初版的硬编码 CASE WHEN source='event_logic'。
    """
    own = conn is None
    conn = conn or _connect()
    try:
        # v189 Item 8: 排序由 weight 列驱动（索引 idx_dec_errors_weight）
        rows = conn.execute(
            """SELECT id, error_code, origin, severity, detail, evidence_refs,
                      source, weight, created_at FROM decision_errors
               WHERE decision_id = ?
               ORDER BY weight DESC, error_code""",
            (decision_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        if own:
            conn.close()


def get_recent_errors(error_codes=None, limit=10, conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        # v225: evidence_refs / source 加入返回，供 code_subkinds 精细化聚合
        if error_codes:
            placeholders = ",".join("?" for _ in error_codes)
            rows = conn.execute(
                f"""SELECT decision_id, error_code, origin, severity, detail,
                          evidence_refs, source, created_at
                    FROM decision_errors
                    WHERE error_code IN ({placeholders})
                    ORDER BY created_at DESC LIMIT ?""",
                (*error_codes, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT decision_id, error_code, origin, severity, detail,
                          evidence_refs, source, created_at
                   FROM decision_errors ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # evidence_refs 是 TEXT (JSON 字符串) → 解析成 dict 给前端
            if isinstance(d.get("evidence_refs"), str):
                try:
                    import json as _json
                    d["evidence_refs"] = _json.loads(d["evidence_refs"])
                except Exception:
                    pass
            out.append(d)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        if own:
            conn.close()


# v302+ P2 PR-17: 失败模式聚类 — 按 error_code 聚合,带趋势/品种/示例
# 单 SQL JOIN ai_decisions 拿 asset,Python 端 group_by + 趋势分桶
def cluster_failures(days=30, min_count=2, limit_per_group=3, conn=None):
    """Return failure patterns grouped by error_code with trend + assets + examples.

    Output: {
      "days": 30, "min_count": 2, "total_errors": 1234, "total_decisions": 567,
      "patterns": [
        {"code": "E014", "count": 87, "pct_of_total": 0.071, ...},
        ...
      ]
    }
    """
    if conn is None:
        return {"days": days, "min_count": min_count,
                "total_errors": 0, "total_decisions": 0, "patterns": []}
    days = min(max(int(days), 1), 90)
    min_count = max(int(min_count), 1)
    limit_per_group = min(max(int(limit_per_group), 1), 10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT de.decision_id, de.error_code, de.severity, de.detail,
                  de.created_at,
                  COALESCE(ad.target_asset, '') AS target_asset
           FROM decision_errors de
           LEFT JOIN ai_decisions ad ON ad.id = de.decision_id
           WHERE de.created_at >= datetime('now', ?)
           ORDER BY de.created_at DESC""",
        ("-{} days".format(days),),
    ).fetchall()
    if not rows:
        return {"days": days, "min_count": min_count,
                "total_errors": 0, "total_decisions": 0, "patterns": []}
    groups: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        code = r["error_code"] or "?"
        groups.setdefault(code, []).append(r)
    total_errors = len(rows)
    unique_decisions = {r["decision_id"] for r in rows}
    total_decisions = len(unique_decisions)
    patterns = []
    for code, items in groups.items():
        if len(items) < min_count:
            continue
        sev_avg = sum(int(i["severity"] or 2) for i in items) / len(items)
        last_7d = sum(1 for i in items if i["created_at"] >= _date_offset(-7))
        prior_7d = sum(1 for i in items
                       if _date_offset(-14) <= (i["created_at"] or "") < _date_offset(-7))
        trend, trend_delta = _compute_trend(last_7d, prior_7d)
        asset_counts: Dict[str, int] = {}
        for i in items:
            a = (i["target_asset"] or "").upper() or "?"
            asset_counts[a] = asset_counts.get(a, 0) + 1
        top_assets = sorted(asset_counts.items(), key=lambda x: -x[1])[:3]
        examples = []
        for i in items[:limit_per_group]:
            examples.append({
                "decision_id": int(i["decision_id"] or 0),
                "detail": (i["detail"] or "")[:160],
                "created_at": i["created_at"] or "",
            })
        patterns.append({
            "code": code, "count": len(items),
            "pct_of_total": round(len(items) / total_errors, 4),
            "severity_avg": round(sev_avg, 2),
            "trend": trend, "trend_delta": trend_delta,
            "last_7d": last_7d, "prior_7d": prior_7d,
            "top_assets": [{"asset": a, "count": c} for a, c in top_assets],
            "examples": examples,
        })
    patterns.sort(key=lambda p: (-p["count"], p["code"]))
    return {"days": days, "min_count": min_count,
            "total_errors": total_errors, "total_decisions": total_decisions,
            "patterns": patterns}


def _date_offset(days_offset: int) -> str:
    """'YYYY-MM-DD HH:MM:SS' for (now + days_offset days)。Python-side,避免再发 SQL。"""
    from datetime import datetime, timedelta
    return (datetime.utcnow() + timedelta(days=days_offset)).strftime("%Y-%m-%d %H:%M:%S")


def _compute_trend(last_7d: int, prior_7d: int):
    """(last_7d - prior_7d) / max(prior_7d, 1)。>+20% up,<-20% down,中间 flat。
    prior=0&last=0 → flat;prior=0&last>0 → up(新模式,显著)。"""
    if prior_7d == 0 and last_7d == 0:
        return "flat", 0.0
    if prior_7d == 0:
        return "up", 1.0
    delta = (last_7d - prior_7d) / prior_7d
    if delta > 0.20:
        return "up", round(delta, 3)
    if delta < -0.20:
        return "down", round(delta, 3)
    return "flat", round(delta, 3)


def format_errors_for_prompt(errors):
    if not errors:
        return ""
    lines = ["\u2550\u2550\u2550 \u524d\u8f66\u4e4b\u9274\uff08\u6700\u8fd1\u53cd\u601d\u9519\u8bef\u7801\uff09\u2550\u2550\u2550"]
    lines.append("\u4ee5\u4e0b\u4e3a\u6700\u8fd1\u51b3\u7b56\u4e2d\u89e6\u53d1\u7684\u53cd\u601d\u9519\u8bef\u7801\uff0cAI \u51b3\u7b56\u65f6\u5e94\u4e3b\u52a8\u89c4\u907f\uff1a")
    for i, e in enumerate(errors[:8], 1):
        sev = {1: "INFO", 2: "WARN", 3: "CRIT"}.get(int(e.get("severity", 2)), "WARN")
        lines.append(f"{i}. [{e['error_code']}/{sev}] {str(e.get('detail', ''))[:120]}")
    return "\n".join(lines)


# 各错误码的"一句话指导"，用于系统提示中紧凑注入（节省 tokens）
_ERROR_CODE_GUIDANCE = {
    "E001": "⚠️ 数据缺失（粗）：hard_data_evidence 至少给出 1-2 条带品种+时间+数值的事实，否则降置信度或转 HOLD",
    # v225: E001 细分子码指导
    "E001a": "⚠️ 缺品种事实：hard_data_evidence 0 条 → 必须补充带品种+时间+数值的事实",
    "E001b": "⚠️ 缺时间锚点：data_anchor_ts 空/无法解析 → 补最新时间或标 N/A",
    "E001c": "⚠️ 缺因子方向：factor_ranking 为空却有非 HOLD action → 补 direction 数据",
    "E001d": "⚠️ 缺事件链关联：news_id/event_atom_ids 均为空 → 关联到具体新闻 atom",
    "E001e": "⚠️ 缺跨资产验证：cross_asset_likely 提到某品种但 hard_data 没数值互证 → 必须加跨资产数值证据",
    "E001f": "⚠️ 缺回测样本：样本 < 30 → 标注置信度受样本不足限制",
    "E002": "⚠️ 数据过期：data_anchor_ts 距今 > 30 分钟应主动更新或标 N/A",
    "E003": "⚠️ 逻辑矛盾：factor_ranking 多数方向必须与 suggested_action 一致，否则属 fabrication_hard",
    "E004": "⚠️ 因果断裂：缺关键因果链节点 → 提示注入因果图谱（asset_driver_nodes）参考",
    "E005": "⚠️ 规则冲突：reasoning 必须引用至少 1 条当前 active=1 的硬铁律，否则转 HOLD",
    "E007": "⚠️ 市场反向：BUY 但 forward_pnl<0 或 SELL 但 forward_pnl>0 → 重新检查事件强度",
    "E009": "⚠️ 置信度虚高：sentiment_score 绝对值 > 0.6 但 evidence 条数 < 2 → 降低 confidence",
    "E010": "⚠️ 抢跑风险：BUY 但 signal 仍在反方向 → 检查 gate_verdict",
    # v189 事件逻辑链层（最高权重）— 反思系统主要工作
    "E014": "🔴 推理链断裂：reasoning_path 八段必须齐全（总框架/因素定位/驱动力/水位博弈/跨资产资金/反共识结论/逻辑自检/总结），缺段属最高优先级修正",
    "E015": "🔴 概念/反共识缺位：factor_ranking 概念必须命中 asset_driver_nodes 核心驱动；reasoning 必须含反向思考关键词（然而/但是/反之…），否则属单边叙事",
    "E016": "🔴 跨资产方向错：cross_asset_likely 中同类品种方向与 suggested_action 必须一致（如 BTC 目标做多但 BTC cross=跌）→ 立即重检传导方向",
    "E017": "🟡 跨资产无据传导：cross_asset_likely 条目在 hard_data_evidence / news_text / reasoning 中无 asset+方向互证 → 标注无据传导，仅供参考",
    # v225+ reasoning_logic 层 — 7 个 LLM 推理质量 checker
    "E021": "⚠️ 假设/前提未明示：reasoning 缺『假设/前提/若/如果』关键词 → 高风险决策无显式前提，失败时无法定位",
    "E022": "⚠️ 反事实未考虑：reasoning 缺『反之/否则/what if/如果失败』关键词 → 单边叙事",
    "E023": "🔴 置信度未校准：evidence_confidence ≥ 70 但实际亏损 forward_pnl<-0.5% → 立即降低置信度档位",
    "E024": "ℹ️ 历史反例未引：reasoning 没引用任何历史同类案例/过去同类场景 → 易重复同类错误",
    "E025": "🔴 跨资产方向不一致：action 与 cross_asset_likely 中同向品种方向相反（如 BTC 目标做多但 BTC cross=跌）",
    "E026": "⚠️ 因果机制不明确：reasoning 因侧/果侧关键词不齐全 → 缺完整『因为…所以…』结构",
    "E027": "⚠️ 置信度-证据量不匹配：evidence_confidence ≥ 65 但 hard_data_evidence < 2 条事实 → 过度自信",
    # v231+ logic_analysis 层 — 形式逻辑 6 类问题
    "E028": "🔴 自相矛盾：reasoning 单句/跨句正反方向词同现且无对比标记（但/却/然而）→ 整段推理不可信",
    "E029": "⚠️ 推不出结论：[总结] 段引入 BUY/SELL/看涨/看跌 但前文 body 无任何同向支撑 → 结论悬空",
    "E030": "⚠️ 假二择一：'要么A要么B / 不是A就是B' 排他性表述无第三选项 → 显式列出 HOLD/减仓/对冲",
    "E031": "🔴 循环论证：[总结] 段与 [因素定位]/[驱动力] 段 token Jaccard≥0.5 → 结论=前提, 需新增信息",
    "E032": "⚠️ 关键术语歧义：'支撑/阻力/强势'等术语在文中被正负修饰（如「强支撑」+「破位支撑」）→ 精确定义",
    "E033": "⚠️ 后此谬因：'紧接着 X 所以 Y' / '因A而B' 类时序≠因果 → 必须显式写因果机制",
}

# v189 事件逻辑链层排序优先级（数字越小越靠前）
# v225: E001 子码加入优先级字典（向后兼容 E001）
# v225+: E021-E027 加入 reasoning_logic 层（排在 event_logic 之后）
_ERROR_CODE_PRIORITY: Dict[str, int] = {
    "E014": 1, "E015": 2, "E016": 3, "E017": 4,  # 事件逻辑链层（最高权重）
    "E003": 4, "E004": 5, "E007": 6,  # 原 v181 高优先级（逻辑/因果/市场）
    "E023": 7, "E025": 8,  # reasoning_logic critical (置信度虚高 / 跨资产错向)
    "E021": 9, "E022": 10, "E026": 11, "E027": 12,  # reasoning_logic warn
    "E024": 13,  # reasoning_logic info
    # v231+ logic_analysis 层 — 形式逻辑 critical (E028/E031) 排在 reasoning_logic critical 之后
    "E028": 14, "E031": 15,
    "E029": 16, "E030": 17, "E032": 18, "E033": 19,
    "E005": 11, "E001": 12, "E002": 13, "E009": 14, "E010": 15,
    "E001a": 12, "E001b": 13, "E001c": 12, "E001d": 14, "E001e": 13, "E001f": 14,
}


def format_compact_guidance(error_codes):
    """按错误码去重输出"一句话指导"，适合注入到系统提示（限制 token 数）。

    v189: 事件逻辑链层（E014/E015/E016）按最高权重排最前，原有错误码保留不动。
    v225+: reasoning_logic 层（E021-E027）作为第二段插入。
    """
    if not error_codes:
        return ""
    seen = []
    for code in error_codes:
        if code and code not in seen:
            seen.append(code)
    # v189 排序：event_logic 来源的排最前
    event_logic_codes = [c for c in seen if c in ("E014", "E015", "E016", "E017")]
    # v225+: reasoning_logic 来源第二
    reasoning_logic_codes = [c for c in seen if c in (
        "E021", "E022", "E023", "E024", "E025", "E026", "E027")]
    # v231+: logic_analysis 来源第三
    logic_analysis_codes = [c for c in seen if c in (
        "E028", "E029", "E030", "E031", "E032", "E033")]
    other_codes = [c for c in seen
                   if c not in ("E014", "E015", "E016", "E017")
                   and c not in ("E021", "E022", "E023", "E024", "E025", "E026", "E027")
                   and c not in ("E028", "E029", "E030", "E031", "E032", "E033")]
    ordered = event_logic_codes + reasoning_logic_codes + logic_analysis_codes + other_codes
    lines = ["═══ 反思错误码提示（AI 决策时应主动规避）═══"]
    if event_logic_codes:
        lines.append("━━ 事件逻辑链层（v189 最高权重 — 反思系统主要工作）━━")
    if reasoning_logic_codes:
        lines.append("━━ 推理逻辑层（v225+ — LLM 推理质量）━━")
    if logic_analysis_codes:
        lines.append("━━ 逻辑分析层（v231+ — 形式逻辑有效性）━━")
    for code in ordered:
        guidance = _ERROR_CODE_GUIDANCE.get(code, f"⚠️ 错误码 {code}")
        tag = "🔴" if code in ("E014", "E015", "E016", "E023", "E025", "E028", "E031") else "⚠️"
        lines.append(f"  {tag} [{code}] {guidance}")
    return "\n".join(lines)


def get_active_error_codes_summary(limit=10, conn=None):
    """获取最近 N 个错误码的去重列表（用于系统提示注入）。"""
    errors = get_recent_errors(limit=limit * 2, conn=conn)
    codes = []
    for e in errors:
        c = e.get("error_code")
        if c and c not in codes:
            codes.append(c)
        if len(codes) >= limit:
            break
    return codes