#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v231 逻辑分析反思 — 形式逻辑有效性的反思 (logic_analysis reflection)

设计动机 (用户 v230 反馈 5):
  反思系统只查事件链(event_logic)和推理质量(reasoning_logic), 不查
  *形式逻辑* — 同一 reasoning_path 内是否自相矛盾 / 结论能否从前提推出 /
  是否循环论证 / 假二择一 / 关键术语歧义 / 后此谬因。

设计原则 (沿用 reflection_deep.py 模式, 不改 schema 语义):
  - 不调用 LLM (避免 token 消耗, 启发式 / 关键词匹配)
  - 离线可跑 (测试不需要网络/LLM)
  - 6 个 detector, 全部独立, 单点异常 → 静默返回 []
  - 输出统一为 error_code + severity + source='logic_analysis' 字典,
    与 reflection_checkers.save_errors() 完全兼容 → 直接落 decision_errors
  - 同时落 trade_reflections_v2 新增列:
      logic_integrity_score REAL DEFAULT 1.0    # 0..1, 越高越严密
      logic_findings_json   TEXT DEFAULT '{}'   # {by_code: {...}, by_severity: {...}}
      logic_checked_at      TEXT DEFAULT ''     # ISO8601, 空表示未跑

主入口:
  analyze_logic(reasoning, decision=None) → findings[]
  enrich_reflection(reflection_id, write=True) → fills
  enrich_pending_logic(batch_size, max_per_run) → daemon
  get_logic_analysis_stats(window_days=30) → stats

CLI:
  python reflection_logic_analysis.py enrich [reflection_id]
  python reflection_logic_analysis.py stats [window_days]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db


# ═══════════════════════════════════════════════════════════════════════════
# E028-E033 错误码常量 (与 reflection_checkers.E0XX 系列平行)
# ═══════════════════════════════════════════════════════════════════════════

E028_SELF_CONTRADICTION     = "E028"  # 自相矛盾
E029_NON_SEQUITUR           = "E029"  # 推不出结论 (non_sequitur)
E030_FALSE_DICHOTOMY        = "E030"  # 假二择一
E031_CIRCULAR_REASONING     = "E031"  # 循环论证
E032_EQUIVOCATION           = "E032"  # 关键术语歧义
E033_POST_HOC               = "E033"  # 后此谬因 / post hoc

# 严重度
SEV_INFO     = 1
SEV_WARN     = 2
SEV_CRITICAL = 3

# 整合度分权重
_SEV_PENALTY = {SEV_INFO: 0.05, SEV_WARN: 0.15, SEV_CRITICAL: 0.30}

# ═══════════════════════════════════════════════════════════════════════════
# 关键词表
# ═══════════════════════════════════════════════════════════════════════════

_POSITIVE_KW = (
    "看涨", "上涨", "做多", "看多", "多头", "买入", "BUY", "LONG", "BULL", "BULLISH",
    "涨", "反弹", "突破", "向上", "走高", "拉升", "走强", "强势",
)
_NEGATIVE_KW = (
    "看跌", "下跌", "做空", "看空", "空头", "卖出", "SELL", "SHORT", "BEAR", "BEARISH",
    "跌", "下行", "向下", "走低", "回落", "走弱", "弱势",
)

# 中文对比标记 (出现这些词 → 不是自相矛盾, 而是显式对比)
_CONTRAST_KW = ("但", "却", "然而", "不过", "即使", "尽管", "虽然", "但仍", "但也")

# E030 假二择一关键词
_DICHOTOMY_KW = (
    "要么", "不是", "就是", "只会", "只能", "只有", "只剩", "仅", "非", "即",
)
# E030 第三选项关键词 (出现 → 不是真二择一)
_THIRD_OPTION_KW = ("或者", "另外", "也可", "还可以", "或许", "可能也", "除此")

# E033 后此谬因模式 (运行时构造, 见 detect_post_hoc)
# E032 歧义关键词 (同一词在文中含义不一)
_EQUIVOCATION_TERMS = ("支撑", "阻力", "强势", "弱势", "反弹", "修正", "企稳", "破位", "底部", "顶部")

# ═══════════════════════════════════════════════════════════════════════════
# 句法分割
# ═══════════════════════════════════════════════════════════════════════════

_SENT_SPLIT_RE = re.compile(r"[。！？；\n]+|(?:\[(?:总结|因素定位|驱动力|水位博弈|跨资产资金|反共识结论|逻辑自检)\])")


def _split_sentences(text: str) -> List[str]:
    """把 reasoning_path 切成句子列表 (中文标点 + 段落分隔符 + 反思段标记)."""
    if not text:
        return []
    parts = _SENT_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and len(p.strip()) >= 3]


def _has_any(text: str, kws) -> bool:
    if not text:
        return False
    return any(k in text for k in kws)


# ═══════════════════════════════════════════════════════════════════════════
# E028 自相矛盾 — 单句内正反方向词同现且无对比标记
# ═══════════════════════════════════════════════════════════════════════════

def detect_self_contradiction(reasoning: str) -> List[Dict[str, Any]]:
    """同一 reasoning 内, 同一议题出现相反方向词 (且无显式 but/却/然而) → 自相矛盾.

    启发式:
      1) 单句内同时含正向词 AND 反向词 (且无对比标记) → E028_inline
      2) 跨句子: 2+ 正向句 + 2+ 反向句 (且无对比标记) → E028_across
    """
    if not reasoning or len(reasoning) < 30:
        return []
    findings: List[Dict[str, Any]] = []
    sentences = _split_sentences(reasoning)
    seen_locations: set = set()

    # 1) 单句矛盾
    for sent in sentences:
        if len(sent) < 6:
            continue
        has_pos = _has_any(sent, _POSITIVE_KW)
        has_neg = _has_any(sent, _NEGATIVE_KW)
        if not (has_pos and has_neg):
            continue
        if _has_any(sent, _CONTRAST_KW):
            continue
        loc = reasoning.find(sent)
        if loc in seen_locations:
            continue
        seen_locations.add(loc)
        pos_hit = [w for w in _POSITIVE_KW if w in sent][:3]
        neg_hit = [w for w in _NEGATIVE_KW if w in sent][:3]
        findings.append({
            "error_code": E028_SELF_CONTRADICTION,
            "severity": SEV_CRITICAL,
            "source": "logic_analysis",
            "layer": "logic_analysis",
            "sub_layer": "formal_validity",
            "signal_name": "self_contradiction",
            "detail": (
                f"单句内同时出现正反方向词且无'但是/却/然而'等对比标记: "
                f"正向={pos_hit}, 反向={neg_hit}; 句子='{sent[:80]}...'"
            ),
            "evidence_refs": {
                "sentence": sent[:200],
                "pos_hits": pos_hit,
                "neg_hits": neg_hit,
                "loc": loc,
                "sub_kind": "self_contradiction_inline",
            },
        })
        if len(findings) >= 3:
            return findings

    # 2) 跨句子矛盾: >=2 纯正句 + >=2 纯反句 (无对比标记) → 整体自相矛盾
    pos_sents: List[str] = []
    neg_sents: List[str] = []
    for sent in sentences:
        if len(sent) < 6:
            continue
        # 含对比标记 → 转折, 不算立场
        if _has_any(sent, _CONTRAST_KW):
            continue
        has_pos = _has_any(sent, _POSITIVE_KW)
        has_neg = _has_any(sent, _NEGATIVE_KW)
        if has_pos and not has_neg:
            pos_sents.append(sent)
        elif has_neg and not has_pos:
            neg_sents.append(sent)

    if len(pos_sents) >= 2 and len(neg_sents) >= 2:
        findings.append({
            "error_code": E028_SELF_CONTRADICTION,
            "severity": SEV_CRITICAL,
            "source": "logic_analysis",
            "layer": "logic_analysis",
            "sub_layer": "formal_validity",
            "signal_name": "self_contradiction",
            "detail": (
                f"跨 {len(pos_sents)} 句正向 + {len(neg_sents)} 句反向, "
                f"无对比标记 → 整体自相矛盾: "
                f"正='{pos_sents[0][:50]}...'; 反='{neg_sents[0][:50]}...'"
            ),
            "evidence_refs": {
                "n_positive_sentences": len(pos_sents),
                "n_negative_sentences": len(neg_sents),
                "first_pos_sentence": pos_sents[0][:200],
                "first_neg_sentence": neg_sents[0][:200],
                "sub_kind": "self_contradiction_across",
            },
        })
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# E029 推不出结论 / non_sequitur — 结论段含方向词, 但前文不含
# ═══════════════════════════════════════════════════════════════════════════

def detect_non_sequitur(reasoning: str) -> List[Dict[str, Any]]:
    """[总结] 段引入方向词 (BUY/SELL/看涨/看跌) 但前文 body 不含 → 结论无据.

    启发式:
      - 取 [总结] 段 (最后一个 [xxx] 标记之后到末尾)
      - 若 [总结] 段含方向词 (BUY/SELL/看涨/看跌)
      - 且 body (排除 [总结] 段) 不含同方向词
      - → 视为非自相矛盾 (结论与前提脱节)
    """
    if not reasoning or len(reasoning) < 50:
        return []
    findings: List[Dict[str, Any]] = []

    # 找最后一段 marker
    summary_idx = -1
    summary_marker = ""
    for marker in ("[总结]", "[反共识结论]"):
        idx = reasoning.rfind(marker)
        if idx > summary_idx:
            summary_idx = idx
            summary_marker = marker

    if summary_idx < 0:
        # 没有 [总结] 段, 取最后 1/4 作"结论段"
        summary_idx = max(0, int(len(reasoning) * 0.75))
        summary_marker = "[末尾段]"

    summary_text = reasoning[summary_idx:]
    body_text = reasoning[:summary_idx]

    pos_in_sum = _has_any(summary_text, _POSITIVE_KW)
    neg_in_sum = _has_any(summary_text, _NEGATIVE_KW)

    if not (pos_in_sum or neg_in_sum):
        return findings

    pos_in_body = _has_any(body_text, _POSITIVE_KW)
    neg_in_body = _has_any(body_text, _NEGATIVE_KW)

    if pos_in_sum and not pos_in_body:
        findings.append({
            "error_code": E029_NON_SEQUITUR,
            "severity": SEV_WARN,
            "source": "logic_analysis",
            "layer": "logic_analysis",
            "sub_layer": "formal_validity",
            "signal_name": "non_sequitur",
            "detail": (
                f"结论段含看涨方向词 (BUY/看涨/上涨/...) 但前文 body 无任何看涨支撑 → "
                f"结论悬空, 建议补充驱动因素或降 confidence"
            ),
            "evidence_refs": {
                "summary_marker": summary_marker,
                "summary_pos_kw": [w for w in _POSITIVE_KW if w in summary_text][:5],
                "body_has_positive": False,
                "sub_kind": "non_sequitur_conclusion",
            },
        })
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# E030 假二择一 — "要么 X 要么 Y" 无第三选项
# ═══════════════════════════════════════════════════════════════════════════

def detect_false_dichotomy(reasoning: str) -> List[Dict[str, Any]]:
    """检测 '要么 A 要么 B / 不是 A 就是 B' 等二择一表述, 且附近无第三选项.

    启发式:
      - 正则匹配 "要么.*要么 / 不是.*就是 / 只会.*或 / 仅.*或"
      - 命中后检查同句/邻句是否含 "或者 / 另外 / 也可 / 或许" 等第三选项关键词
      - 无第三选项 → E030 (WARN)
    """
    if not reasoning or len(reasoning) < 20:
        return []
    findings: List[Dict[str, Any]] = []
    dichotomy_patterns = [
        re.compile(r"要么[^。！？\n]{2,40}?(?:要么|就是)[^。！？\n]{2,40}"),
        re.compile(r"不是[^。！？\n]{2,40}?就是[^。！？\n]{2,40}"),
        re.compile(r"只会[^。！？\n]{2,40}?(?:不会|不能)[^。！？\n]{2,40}"),
        re.compile(r"(?:仅|只有|只能)[^。！？\n]{2,40}?(?:或|或者)[^。！？\n]{2,40}"),
        re.compile(r"非[^。！？\n]{1,8}?即[^。！？\n]{2,40}"),
    ]
    seen_locations = set()
    for pat in dichotomy_patterns:
        for m in pat.finditer(reasoning):
            sentence = m.group(0)
            ctx_start = max(0, m.start() - 60)
            ctx_end = min(len(reasoning), m.end() + 60)
            context = reasoning[ctx_start:ctx_end]
            if _has_any(context, _THIRD_OPTION_KW):
                continue
            if m.start() in seen_locations:
                continue
            seen_locations.add(m.start())
            findings.append({
                "error_code": E030_FALSE_DICHOTOMY,
                "severity": SEV_WARN,
                "source": "logic_analysis",
                "layer": "logic_analysis",
                "sub_layer": "formal_validity",
                "signal_name": "false_dichotomy",
                "detail": (
                    f"二择一表述无第三选项: '{sentence[:80]}...'; "
                    f"建议显式列出第三选项或避免排他性表述"
                ),
                "evidence_refs": {
                    "matched_text": sentence[:200],
                    "loc": m.start(),
                    "sub_kind": "exclusive_disjunction",
                },
            })
            if len(findings) >= 3:
                return findings
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# E031 循环论证 — [总结] 与 [因素定位]/[驱动力] 高度重合
# ═══════════════════════════════════════════════════════════════════════════

def _tokenize_cn(text: str) -> List[str]:
    """粗粒度中文 2-gram token (去掉标点空格), 用于 Jaccard 比较."""
    if not text:
        return []
    text = re.sub(r"[\s\[\]【】()（）。，,.;:!?？、]", "", text)
    tokens = []
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        if re.match(r"[\u4e00-\u9fff]{2}", bigram):
            tokens.append(bigram)
    return tokens


def detect_circular_reasoning(reasoning: str) -> List[Dict[str, Any]]:
    """[总结] 段与 [因素定位]/[驱动力] 段 token 重合率 > 0.5 → 循环论证.

    启发式:
      - 取 [总结] 段 (最末 marker 之后)
      - 取 body 前段 [因素定位] + [驱动力] 段
      - 2-gram token Jaccard > 0.5 → 视为循环
    """
    if not reasoning or len(reasoning) < 100:
        return []
    findings: List[Dict[str, Any]] = []

    summary_idx = -1
    for marker in ("[总结]", "[反共识结论]"):
        idx = reasoning.rfind(marker)
        if idx > summary_idx:
            summary_idx = idx
    if summary_idx < 0:
        return findings

    body_parts: List[str] = []
    for marker in ("[因素定位]", "[驱动力]"):
        idx = reasoning.find(marker)
        if idx >= 0:
            seg_end = idx + 200
            body_parts.append(reasoning[idx:seg_end])

    if not body_parts:
        return findings

    summary_text = reasoning[summary_idx:]
    body_text = "".join(body_parts)

    summary_tokens = set(_tokenize_cn(summary_text))
    body_tokens = set(_tokenize_cn(body_text))
    if not summary_tokens or not body_tokens:
        return findings

    intersection = summary_tokens & body_tokens
    union = summary_tokens | body_tokens
    jaccard = len(intersection) / len(union) if union else 0.0

    if jaccard >= 0.5 and len(intersection) >= 6:
        findings.append({
            "error_code": E031_CIRCULAR_REASONING,
            "severity": SEV_CRITICAL,
            "source": "logic_analysis",
            "layer": "logic_analysis",
            "sub_layer": "formal_validity",
            "signal_name": "circular_reasoning",
            "detail": (
                f"[总结] 段与 [因素定位]/[驱动力] 段 2-gram token Jaccard={jaccard:.2f} "
                f"(≥0.5), 重合 {len(intersection)} 个 token → 循环论证嫌疑"
            ),
            "evidence_refs": {
                "jaccard": round(jaccard, 3),
                "shared_tokens_n": len(intersection),
                "summary_len": len(summary_tokens),
                "body_len": len(body_tokens),
                "sub_kind": "summary_premise_overlap",
            },
        })
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# E032 关键术语歧义 — 同一词在文中两种含义
# ═══════════════════════════════════════════════════════════════════════════

# 各时序的常见修饰词 (正面 / 负面 / 中性)
_POS_MODIFIER = ("强", "明确", "有效", "形成", "确立", "成功", "确认", "坚硬")
_NEG_MODIFIER = ("弱", "假", "破", "失效", "未形成", "未确认", "暂时", "短时")


def detect_equivocation(reasoning: str) -> List[Dict[str, Any]]:
    """同一术语 (支撑/阻力/强势/弱势/...) 在文中被赋予互斥修饰 → 歧义.

    启发式:
      - 切句, 找含 "支撑/阻力/强势/弱势/反弹/修正/企稳/破位/底部/顶部" 的句子
      - 同句检查 "强/确立" vs "弱/破" 修饰词
      - 若两修饰词均出现 (任一句子) → 视为歧义
    """
    if not reasoning or len(reasoning) < 30:
        return []
    findings: List[Dict[str, Any]] = []
    sentences = _split_sentences(reasoning)
    term_hits: Dict[str, List[Dict[str, Any]]] = {}
    for sent in sentences:
        if len(sent) < 4:
            continue
        for term in _EQUIVOCATION_TERMS:
            if term not in sent:
                continue
            pos_mod = _has_any(sent, _POS_MODIFIER)
            neg_mod = _has_any(sent, _NEG_MODIFIER)
            if pos_mod or neg_mod:
                term_hits.setdefault(term, []).append({
                    "sentence": sent[:100],
                    "pos_mod": pos_mod,
                    "neg_mod": neg_mod,
                })
    for term, hits in term_hits.items():
        has_pos = any(h["pos_mod"] for h in hits)
        has_neg = any(h["neg_mod"] for h in hits)
        if has_pos and has_neg:
            findings.append({
                "error_code": E032_EQUIVOCATION,
                "severity": SEV_WARN,
                "source": "logic_analysis",
                "layer": "logic_analysis",
                "sub_layer": "formal_validity",
                "signal_name": "equivocation",
                "detail": (
                    f"术语 '{term}' 在文中同时被正/负修饰 (如「强支撑」与「破位支撑」), "
                    f"含义游移 → 建议精确定义该术语指代何种"
                ),
                "evidence_refs": {
                    "term": term,
                    "n_sentences": len(hits),
                    "sample_sentences": [h["sentence"] for h in hits[:3]],
                    "sub_kind": "term_meaning_shift",
                },
            })
            if len(findings) >= 3:
                return findings
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# E033 后此谬因 — "A 后 B 故因 A"
# ═══════════════════════════════════════════════════════════════════════════

def detect_post_hoc(reasoning: str) -> List[Dict[str, Any]]:
    """检测 'A 后 B 所以 B 因 A' / '紧接着 X 所以 Y' 等 post hoc 谬误."""
    if not reasoning or len(reasoning) < 20:
        return []
    findings: List[Dict[str, Any]] = []
    real_patterns = [
        re.compile(r"(?:紧接着|紧跟着|随即|随后|之后)[^。！？\n]{2,30}?(?:所以|因此|故|致使)"),
        re.compile(r"因[^。]{2,15}?而[^。]{2,30}?(?:下跌|上涨|走弱|走强|回落|拉升)"),
        re.compile(r"(?:刚|刚刚|才)[^。]{2,15}?(?:就|便)[^。]{2,15}?(?:下跌|上涨|走弱|走强|回落|拉升)"),
    ]
    seen_locations = set()
    for pat in real_patterns:
        for m in pat.finditer(reasoning):
            if m.start() in seen_locations:
                continue
            seen_locations.add(m.start())
            findings.append({
                "error_code": E033_POST_HOC,
                "severity": SEV_WARN,
                "source": "logic_analysis",
                "layer": "logic_analysis",
                "sub_layer": "formal_validity",
                "signal_name": "post_hoc",
                "detail": (
                    f"后此谬因嫌疑 (时间先 ≠ 因果): '{m.group(0)[:80]}...'; "
                    f"建议补充因果机制 ('X 通过 ... 引起 Y') 而非仅时序并列"
                ),
                "evidence_refs": {
                    "matched_text": m.group(0)[:200],
                    "loc": m.start(),
                    "sub_kind": "temporal_not_causal",
                },
            })
            if len(findings) >= 3:
                return findings
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# 整合度评分 + findings 汇总
# ═══════════════════════════════════════════════════════════════════════════

def compute_logic_integrity_score(findings: List[Dict[str, Any]]) -> float:
    """根据 findings 计算 0..1 整合度分 (1.0 = 无错, 越低问题越严重).

    公式: score = max(0.0, 1.0 - sum(severity_penalty))
    上限 0.0 (避免负值).
    """
    if not findings:
        return 1.0
    penalty = sum(_SEV_PENALTY.get(int(f.get("severity", SEV_WARN)), 0.15) for f in findings)
    return round(max(0.0, 1.0 - penalty), 3)


def summarize_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 findings 列表汇总为 by_code / by_severity 字典 (落库 JSON)."""
    by_code: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    for f in findings:
        code = f.get("error_code", "unknown")
        sev = int(f.get("severity", SEV_WARN))
        sev_label = {SEV_INFO: "info", SEV_WARN: "warn", SEV_CRITICAL: "critical"}.get(sev, "warn")
        by_code[code] = by_code.get(code, 0) + 1
        by_severity[sev_label] = by_severity.get(sev_label, 0) + 1
    return {
        "n_findings": len(findings),
        "by_code": by_code,
        "by_severity": by_severity,
        "codes_list": sorted(set(f.get("error_code", "") for f in findings)),
    }


def analyze_logic(reasoning: str) -> List[Dict[str, Any]]:
    """主入口: 对一段 reasoning 跑 6 个 detector, 合并 findings.

    任一 detector 抛异常 → 静默跳过该 detector, 不影响整体。
    """
    findings: List[Dict[str, Any]] = []
    for fn in (
        detect_self_contradiction,
        detect_non_sequitur,
        detect_false_dichotomy,
        detect_circular_reasoning,
        detect_equivocation,
        detect_post_hoc,
    ):
        try:
            findings.extend(fn(reasoning))
        except Exception as exc:  # noqa: BLE001
            print(f"[logic_analysis] {fn.__name__} failed: {type(exc).__name__}: {exc}", flush=True)
    return findings

    return findings

# ═══════════════════════════════════════════════════════════════════════════
# DB 读写
# ═══════════════════════════════════════════════════════════════════════════

def _connect():
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _get_reflection_row(conn, reflection_id: int):
    try:
        return conn.execute(
            "SELECT * FROM trade_reflections_v2 WHERE id = ?",
            (reflection_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _get_reasoning_for_reflection(conn, reflection_row) -> str:
    """从 trade_reflections_v2 行 + ai_decisions 关联, 拼出 reasoning 文本.

    只取实际存在的列, schema 演进时增加新列不需要改这里.
    """
    decision_id = reflection_row["decision_id"]
    if not decision_id:
        return ""
    # 先查表实际有哪些列 (避免 schema 演进导致的 no-such-column)
    try:
        cols_info = conn.execute("PRAGMA table_info(ai_decisions)").fetchall()
        existing_cols = {row[1] for row in cols_info}
    except sqlite3.OperationalError:
        return ""
    # 候选列: reasoning 主体 + reasoning_path + hard_data_evidence + factor_ranking
    candidates = ["reasoning", "reasoning_path", "hard_data_evidence", "factor_ranking"]
    select_cols = [c for c in candidates if c in existing_cols]
    if not select_cols:
        return ""
    row = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM ai_decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    if not row:
        return ""
    parts: List[str] = []
    for col in select_cols:
        try:
            v = str(row[col] or "").strip()
        except (IndexError, KeyError):
            continue
        if v:
            parts.append(v)
    return "\n".join(parts)


def enrich_reflection(reflection_id: int, write: bool = True,
                      conn=None) -> Dict[str, Any]:
    """对单条 trade_reflections_v2 跑 6 个 detector, 落库 (findings + score + json)."""
    own = conn is None
    conn = conn or _connect()
    out: Dict[str, Any] = {"reflection_id": reflection_id, "ok": False}
    try:
        ref_row = _get_reflection_row(conn, reflection_id)
        if not ref_row:
            out["error"] = "reflection_not_found"
            return out
        reasoning = _get_reasoning_for_reflection(conn, ref_row)
        if not reasoning or len(reasoning.strip()) < 10:
            out["ok"] = True
            out["skipped"] = "reasoning_empty"
            out["integrity_score"] = 1.0
            out["summary"] = {"n_findings": 0, "by_code": {}, "by_severity": {}, "codes_list": []}
            if write:
                _write_enrichment(conn, reflection_id, out)
            out["written"] = bool(write)
            return out
        findings = analyze_logic(reasoning)
        decision_id = ref_row["decision_id"]
        for f in findings:
            f["decision_id"] = decision_id
            f.setdefault("origin", "logic_analysis")
        integrity_score = compute_logic_integrity_score(findings)
        summary = summarize_findings(findings)
        out.update({
            "ok": True,
            "findings": findings,
            "integrity_score": integrity_score,
            "summary": summary,
            "written": False,
        })
        if write:
            try:
                import reflection_checkers as rc
                written_errors = rc.save_errors(findings, conn=conn)
                out["decision_errors_written"] = written_errors
            except Exception as exc:  # noqa: BLE001
                out["decision_errors_error"] = f"{type(exc).__name__}: {exc}"
            _write_enrichment(conn, reflection_id, out)
            out["written"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _write_enrichment(conn, reflection_id: int, result: Dict[str, Any]) -> None:
    """把 integrity_score / findings_json / checked_at 写回 trade_reflections_v2."""
    integrity_score = float(result.get("integrity_score", 1.0))
    summary = result.get("summary") or {"n_findings": 0, "by_code": {}, "by_severity": {}, "codes_list": []}
    findings = result.get("findings") or []
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute(
            """UPDATE trade_reflections_v2
                  SET logic_integrity_score = ?,
                      logic_findings_json   = ?,
                      logic_checked_at      = ?
                WHERE id = ?""",
            (
                integrity_score,
                json.dumps({
                    "summary": summary,
                    "details": [
                        {
                            "error_code": f.get("error_code"),
                            "severity": f.get("severity"),
                            "signal_name": f.get("signal_name"),
                            "detail": (f.get("detail") or "")[:500],
                            "evidence_refs": f.get("evidence_refs", {}),
                        }
                        for f in findings
                    ],
                }, ensure_ascii=False),
                checked_at,
                reflection_id,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        print(f"[logic_analysis] _write_enrichment failed: {exc}", flush=True)


def enrich_pending_logic(batch_size: int = 50, max_per_run: int = 200,
                          conn=None) -> Dict[str, Any]:
    """扫 trade_reflections_v2 中 logic_checked_at='' 的反思, 跑 detector + 落库."""
    own = conn is None
    conn = conn or _connect()
    out: Dict[str, Any] = {
        "scanned": 0, "enriched": 0, "skipped": 0, "errors": 0,
        "score_histogram": {"1.0": 0, "0.75-1.0": 0, "0.5-0.75": 0,
                             "0.25-0.5": 0, "0.0-0.25": 0},
        "details": [],
    }
    try:
        try:
            rows = conn.execute(
                """SELECT id, decision_id FROM trade_reflections_v2
                    WHERE (logic_checked_at IS NULL OR logic_checked_at = '')
                          AND decision_id IS NOT NULL
                    ORDER BY id DESC LIMIT ?""",
                (min(batch_size, max_per_run),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            out["error"] = f"scan failed: {exc}"
            return out
        out["scanned"] = len(rows)
        for r in rows:
            rid = int(r["id"])
            try:
                result = enrich_reflection(rid, write=True, conn=conn)
                if not result.get("ok"):
                    out["errors"] += 1
                    out["details"].append({"id": rid, "error": result.get("error")})
                    continue
                if result.get("skipped") == "reasoning_empty":
                    out["skipped"] += 1
                else:
                    out["enriched"] += 1
                    score = float(result.get("integrity_score", 1.0))
                    if score >= 1.0:
                        out["score_histogram"]["1.0"] += 1
                    elif score >= 0.75:
                        out["score_histogram"]["0.75-1.0"] += 1
                    elif score >= 0.5:
                        out["score_histogram"]["0.5-0.75"] += 1
                    elif score >= 0.25:
                        out["score_histogram"]["0.25-0.5"] += 1
                    else:
                        out["score_histogram"]["0.0-0.25"] += 1
            except Exception as exc:  # noqa: BLE001
                out["errors"] += 1
                out["details"].append({"id": rid, "error": f"{type(exc).__name__}: {exc}"})
        return out
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# 统计接口
# ═══════════════════════════════════════════════════════════════════════════

def get_logic_analysis_stats(window_days: int = 30, conn=None) -> Dict[str, Any]:
    """返回近 N 天 logic_analysis 反思的统计."""
    own = conn is None
    conn = conn or _connect()
    out: Dict[str, Any] = {"window_days": window_days, "ok": False}
    try:
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          AVG(logic_integrity_score) AS avg_score,
                          MIN(logic_integrity_score) AS min_score,
                          MAX(logic_integrity_score) AS max_score
                     FROM trade_reflections_v2
                    WHERE logic_checked_at != ''
                      AND logic_checked_at >= datetime('now', ?)""",
                (f"-{int(window_days)} days",),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            out["error"] = f"stats failed: {exc}"
            return out
        total = int(row["total"] or 0)
        avg_score = float(row["avg_score"] or 0.0)
        min_score = float(row["min_score"] or 0.0)
        max_score = float(row["max_score"] or 0.0)
        out["total"] = total
        out["avg_score"] = round(avg_score, 3)
        out["min_score"] = min_score
        out["max_score"] = max_score

        buckets = {"perfect_1.0": 0, "high_0.75-1.0": 0, "mid_0.5-0.75": 0,
                   "low_0.25-0.5": 0, "broken_<0.25": 0}
        try:
            bucket_row = conn.execute(
                """SELECT
                      SUM(CASE WHEN logic_integrity_score >= 1.0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN logic_integrity_score >= 0.75 AND logic_integrity_score < 1.0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN logic_integrity_score >= 0.5  AND logic_integrity_score < 0.75 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN logic_integrity_score >= 0.25 AND logic_integrity_score < 0.5  THEN 1 ELSE 0 END),
                      SUM(CASE WHEN logic_integrity_score <  0.25 THEN 1 ELSE 0 END)
                     FROM trade_reflections_v2
                    WHERE logic_checked_at != ''
                      AND logic_checked_at >= datetime('now', ?)""",
                (f"-{int(window_days)} days",),
            ).fetchone()
            keys = list(buckets.keys())
            for i, k in enumerate(keys):
                buckets[k] = int(bucket_row[i] or 0)
        except sqlite3.OperationalError:
            pass
        out["by_score_bucket"] = buckets

        try:
            code_rows = conn.execute(
                """SELECT de.error_code, COUNT(*) AS n
                     FROM decision_errors de
                     JOIN trade_reflections_v2 trv ON trv.decision_id = de.decision_id
                    WHERE de.error_code IN ('E028','E029','E030','E031','E032','E033')
                      AND trv.logic_checked_at != ''
                      AND trv.logic_checked_at >= datetime('now', ?)
                    GROUP BY de.error_code
                    ORDER BY n DESC""",
                (f"-{int(window_days)} days",),
            ).fetchall()
        except sqlite3.OperationalError:
            code_rows = []
        out["by_code"] = {r["error_code"]: int(r["n"]) for r in code_rows}

        try:
            top_rows = conn.execute(
                """SELECT id, decision_id, logic_integrity_score,
                          logic_findings_json, pnl_pct, asset, action
                     FROM trade_reflections_v2
                    WHERE logic_checked_at != ''
                      AND logic_checked_at >= datetime('now', ?)
                      AND logic_integrity_score < 1.0
                    ORDER BY logic_integrity_score ASC LIMIT 20""",
                (f"-{int(window_days)} days",),
            ).fetchall()
            out["worst_20"] = []
            for r in top_rows:
                item = dict(r)
                try:
                    item["logic_findings_json"] = json.loads(item.get("logic_findings_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
                out["worst_20"].append(item)
        except sqlite3.OperationalError:
            out["worst_20"] = []

        out["ok"] = True
        return out
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "enrich":
        if len(sys.argv) >= 3:
            rid = int(sys.argv[2])
            print(json.dumps(enrich_reflection(rid, write=True),
                             ensure_ascii=False, indent=2))
        else:
            r = enrich_pending_logic(batch_size=200, max_per_run=200)
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "stats":
        wd = int(sys.argv[2]) if len(sys.argv) >= 3 else 30
        print(json.dumps(get_logic_analysis_stats(window_days=wd),
                         ensure_ascii=False, indent=2))
    else:
        print("Usage:")
        print("  python reflection_logic_analysis.py enrich [reflection_id]")
        print("  python reflection_logic_analysis.py enrich              # batch all pending")
        print("  python reflection_logic_analysis.py stats [window_days]")
