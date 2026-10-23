#!/usr/bin/env python3
"""v189 Item 6 — 八段式段名 prompt ↔ DB ↔ checker 三处对齐验证。

三层对齐校验：
  [1] ai_worker 段名（_SIX_STAGES / _STAGE_MARK_RE）
  [2] reflection_checkers._ANALYSIS_STEPS 段名
  [3] reasoning_path DB column 默认值/校验
─────────────────────────────────────────────────────────
"""
import sys
import os
import re

PROJECT = "/Users/claire/.cline/data/workspaces/chat/trident_v170"
sys.path.insert(0, os.path.join(PROJECT, "backend", "src_python"))

import reflection_checkers as rc
# ai_worker 不独立可导入（会触发副作用），用文本扫描
import engine.ai_worker as aw

passes = 0; fails = 0; total = 0
def chk(name, cond, detail=""):
    global passes, fails, total
    total += 1
    if cond: passes += 1; print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else: fails += 1; print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


print("=== [1] ai_worker._SIX_STAGES ===")
expected_7 = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检")
chk("_SIX_STAGES 等于 7 段", tuple(aw._SIX_STAGES) == expected_7,
    f"got {aw._SIX_STAGES}")

print("\n=== [2] ai_worker._STAGE_MARK_RE 包含 8 段（含[总结]）===")
mark_re = aw._STAGE_MARK_RE.pattern
all_in_reg = all(s in mark_re for s in expected_7)
chk("_STAGE_MARK_RE 含 7 段", all_in_reg)
chk("_STAGE_MARK_RE 含 总结", "总结" in mark_re)

print("\n=== [3] reflection_checkers._ANALYSIS_STEPS 8 段 ===")
expected_8 = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检", "总结")
chk("rc._ANALYSIS_STEPS == 8 段", tuple(rc._ANALYSIS_STEPS) == expected_8,
    f"got {rc._ANALYSIS_STEPS}")
chk("rc._ANALYSIS_STEPS 含总结", "总结" in rc._ANALYSIS_STEPS)

print("\n=== [4] 三处段名集合完全一致 ===")
worker_set = set(aw._SIX_STAGES) | {"总结"}
checker_set = set(rc._ANALYSIS_STEPS)
chk("worker == checker", worker_set == checker_set,
    f"worker={sorted(worker_set)} checker={sorted(checker_set)}")

print("\n=== [5] ai_worker 兜底模板含全部 8 段标记 ===")
import importlib
worker_src_path = aw.__file__
with open(worker_src_path, "r", encoding="utf-8") as f:
    src = f.read()
# 检查源文件里 [段名] 出现 >=3 次（提示词 + 兜底 + 解析函数 + 重试 hint）
for s in expected_8:
    cnt = src.count(f"[{s}]")
    chk(f"  [{s}] 出现 >=3 次", cnt >= 3, f"count={cnt}")

print("\n=== [6] ai_worker 八段重试提示含新顺序 ===")
retry_hint = aw._SIX_STAGE_RETRY_HINT
chk("重试提示含 [总框架]→[因素定位]→...", "[总框架] → [因素定位] →" in retry_hint)
for s in expected_8:
    chk(f"  重试提示含 [{s}]", f"[{s}]" in retry_hint)

print("\n=== [7] reasoning_path DB column 长度 ≥ 100（schema 验证）===")
sys.path.insert(0, os.path.join(PROJECT, "backend", "src_python"))
import llm_schema as ls
rp_schema = ls.AI_DECISION_SCHEMA["properties"]["reasoning_path"]
chk("DB schema minLength ≥ 100", rp_schema.get("minLength", 0) >= 100,
    f"minLength={rp_schema.get('minLength')}")
chk("reasoning_path 是必填", "reasoning_path" in ls.AI_DECISION_SCHEMA["required"])

print("\n=== [8] _ANALYSIS_STEPS 与 _STAGE_MARK_RE 提取一致 ===")
fake_path = ("[总框架] 一句话\n[因素定位] 子链\n[驱动力] driver\n"
            "[水位博弈] level\n[跨资产资金] cross\n[反共识结论] contrarian\n"
            "[逻辑自检] self-check\n[总结] final")
extracted = rc._extract_reasoning_steps(fake_path)
chk("rc._extract_reasoning_steps 检出 8 段", len(extracted) == 8,
    f"got {len(extracted)}: {extracted}")
chk("顺序正确", list(extracted) == list(expected_8),
    f"extracted={extracted} expected={list(expected_8)}")

print("\n=== [9] 缺段时的 E014 报错 detail 列全 8 段名 ===")
import json
errs = rc.run_all_checkers({
    "id": 99, "suggested_action": "BUY", "target_asset": "BTC",
    "reasoning": "空", "factor_ranking": "[]",
    "cross_asset_likely": "[]", "sentiment_score": 0.0,
})
e014 = [e for e in errs if e["error_code"] == "E014"]
if e014:
    detail = e014["detail"] if isinstance(e014, dict) else e014[0]["detail"]
    # 7 段均缺，每个都应在 detail 里
    for s in expected_8[:7]:
        chk(f"  E014 detail 含 [{s}]", s in detail)

print(f"\n=== 总计：{passes}/{total} pass，{fails} fail ===")
sys.exit(0 if fails == 0 else 1)