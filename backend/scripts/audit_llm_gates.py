#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_llm_gates — 审计所有 LLM 调用点是否受额度闸门保护。

背景（2026-11-04 事故）
-----------------------
一天烧掉 9.19M token / 4582 次请求。根因不是一个 bug，而是**保护只加了一半**：

    ai_worker 主分析路径   → 有闸门、有熔断器   ✅
    realtime_filter 门卫   → 有闸门、有熔断器   ✅
    review_agent._llm      → 裸 requests.post，429 还会重试  ❌
    judge_worker 远程兜底   → 裸 requests.post                        ❌
    engine/webhook 翻译    → 裸 urllib.request（每条新闻都跑！）      ❌
    attribute_multi_factor → 裸 session.post                         ❌
    gate_replay_guard 探针 → 每通道一次，5 分钟一轮                   ❌
    api_server 通道体检     → 有熔断器、无闸门                        ❌

修完之后，这个脚本就是**回归防线**：任何人再加一条裸 LLM 路径，
审计立刻报红，而不是等额度烧穿才发现。

用法：
    python3 scripts/audit_llm_gates.py            # 人读格式
    python3 scripts/audit_llm_gates.py --json     # 机器读
退出码：0 = 全部受保护；1 = 有裸调用点
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SRC = Path(__file__).resolve().parent.parent / "src_python"

# 判定"受保护"的关键标记（出现在调用点所在函数内即算受保护）
GATE_MARKERS = (
    "llm_budget",          # 额度闸门
    "budget_blocked",
    "_analysis_breaker",   # 熔断器
    "circuit_breaker",
    "cb_get",
)
# 已知豁免：保护逻辑不在本函数内（在被它调用的公共实现里），或
# 该调用点本身就是「受保护的公共实现入口」——由调用它的上层负责闸门。
KNOWN_PROTECTED = {
    ("realtime_filter.py", "_post_openai_compat"),
    ("realtime_filter.py", "_post_openrouter"),
    ("engine/ai_worker.py", "_do_api_call"),
}


def _norm(path: Path) -> str:
    rel = path.relative_to(SRC).as_posix()
    return rel


def _enclosing_funcs(tree: ast.AST) -> Dict[ast.AST, str]:
    """node -> 所在函数名（不在任何函数里则为空串）。"""
    out: Dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                out[sub] = node.name
    return out


def _func_source(fn: ast.AST) -> str:
    try:
        return ast.get_source_segment(_SRC_TEXT, fn) or ""
    except Exception:
        return ""


_SRC_TEXT = ""


def scan_file(path: Path) -> List[Dict[str, object]]:
    global _SRC_TEXT
    try:
        _SRC_TEXT = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        tree = ast.parse(_SRC_TEXT)
    except SyntaxError:
        return []

    if "chat/completions" not in _SRC_TEXT:
        return []

    fn_owner = _enclosing_funcs(tree)
    fn_nodes: Dict[str, ast.AST] = {}
    for cand in ast.walk(tree):
        if isinstance(cand, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_nodes.setdefault(cand.name, cand)

    # ── 步骤 1：定位「含 LLM 端点字符串」的函数 = LLM 函数 ──────────────
    # 不能按文件级判定：api_server.py 有 1.8 万行、上百个 .post()，
    # 其中绝大多数是 REST 调用而非 LLM —— 按文件级会产出几百条误报。
    llm_funcs: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "chat/completions" in node.value:
            owner = fn_owner.get(node)
            if owner:
                llm_funcs.add(owner)
    if not llm_funcs:
        return []

    # ── 步骤 2：只审这些函数里的 HTTP 调用 ────────────────────────────
    hits: List[Dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = ""
        if isinstance(func, ast.Attribute):
            fname = func.attr
        elif isinstance(func, ast.Name):
            fname = func.id
        if fname not in ("post", "urlopen", "Post", "request", "post_json"):
            continue

        owner = fn_owner.get(node, "")
        if owner not in llm_funcs:
            continue

        fn_node = fn_nodes.get(owner)
        src = _func_source(fn_node) if fn_node is not None else ""
        protected = any(mk in src for mk in GATE_MARKERS)

        rel = _norm(path)
        if (rel, owner) in KNOWN_PROTECTED:
            protected = True

        hits.append({
            "file": rel,
            "line": getattr(node, "lineno", 0),
            "func": owner or "<module>",
            "protected": protected,
            "markers": sorted({mk for mk in GATE_MARKERS if mk in src}),
        })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    all_hits: List[Dict[str, object]] = []
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        all_hits.extend(scan_file(p))

    unprotected = [h for h in all_hits if not h["protected"]]

    if args.json:
        print(json.dumps({
            "total_call_sites": len(all_hits),
            "unprotected": len(unprotected),
            "sites": all_hits,
        }, ensure_ascii=False, indent=2))
        return 1 if unprotected else 0

    print("=" * 66)
    print("  LLM 调用点额度闸门审计")
    print("=" * 66)
    if not all_hits:
        print("  未发现 LLM 调用点（扫描范围可能不对）")
        return 1
    for h in all_hits:
        flag = "✅" if h["protected"] else "❌ 未受保护"
        print(f"  {flag}  {h['file']}:{h['line']}  ({h['func']})")
    print("-" * 66)
    print(f"  调用点总数 {len(all_hits)} · 受保护 {len(all_hits) - len(unprotected)}"
          f" · 未保护 {len(unprotected)}")
    if unprotected:
        print()
        print("  ⚠ 以下调用点没有任何额度闸门/熔断保护：")
        for h in unprotected:
            print(f"    · {h['file']}:{h['line']} ({h['func']})")
        print()
        print("  修法：在发请求前加 llm_budget.check(...) —— 参考")
        print("        src_python/engine/attribute_multi_factor.py 的 [ATTR-MF] 段。")
        return 1
    print("  ✅ 全部 LLM 调用点均受额度闸门保护")
    return 0


if __name__ == "__main__":
    sys.exit(main())
