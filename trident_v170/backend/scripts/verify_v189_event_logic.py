#!/usr/bin/env python3
"""v189 反思系统·事件逻辑链层 — 验证脚本（不动代码，只校验）。"""
import sys
import os
import json

PROJECT = "/Users/claire/.cline/data/workspaces/chat/trident_v170"
sys.path.insert(0, os.path.join(PROJECT, "backend", "src_python"))

import review_agent as ra
import reflection_ontology as o
import reflection_checkers as rc

passes = 0; fails = 0; total = 0
def chk(name, cond, detail=""):
    global passes, fails, total
    total += 1
    if cond: passes += 1; print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else: fails += 1; print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

print("=== [1] _FAILURE_FRAMEWORK ===")
ff = ra._FAILURE_FRAMEWORK
chk("24 in ff", "24" in ff and "因果图谱推理链错位" in ff)
chk("25 in ff", "25" in ff and "概念错位" in ff)
chk("26 in ff", "26" in ff and "跨资产传导方向错" in ff)

print("=== [2] _REASON_NAMES ===")
chk("24 name", ra._REASON_NAMES.get(24) == "因果图谱推理链错位")
chk("25 name", ra._REASON_NAMES.get(25) == "概念错位/反共识缺位")
chk("26 name", ra._REASON_NAMES.get(26) == "跨资产传导方向错")

print("=== [3] _REASON_OBSERVABILITY (auto) ===")
chk("24 observability=auto", ra._REASON_OBSERVABILITY.get(24) == "auto")
chk("25 observability=auto", ra._REASON_OBSERVABILITY.get(25) == "auto")
chk("26 observability=auto", ra._REASON_OBSERVABILITY.get(26) == "auto")

print("=== [4] _REASON_TO_LAYER → event_logic ===")
chk("24→event_logic", ra._REASON_TO_LAYER.get(24) == "event_logic")
chk("25→event_logic", ra._REASON_TO_LAYER.get(25) == "event_logic")
chk("26→event_logic", ra._REASON_TO_LAYER.get(26) == "event_logic")

print("=== [5] event_logic weight=1 (最高) ===")
chk("weight=1", ra.layer_weight("event_logic") == 1)

print("=== [6] list_layers_ordered() 把 event_logic 排第 1 ===")
chk("event_logic first", ra.list_layers_ordered()[0][0] == "event_logic")

print("=== [7] reason_layer(24/25/26) ===")
chk("24", ra.reason_layer(24) == "event_logic")
chk("25", ra.reason_layer(25) == "event_logic")
chk("26", ra.reason_layer(26) == "event_logic")

print("=== [8] event_logic < 老 layer 权重 ===")
chk("< data", ra.layer_weight("event_logic") < ra.layer_weight("data"))
chk("< gate", ra.layer_weight("event_logic") < ra.layer_weight("gate"))
chk("< judgment", ra.layer_weight("event_logic") < ra.layer_weight("judgment"))

print("=== [9] _SUBLAYER_TWO_LEVEL 24/25/26 ≥ 3 个 l1 ===")
chk("24 l1>=3", len(ra._SUBLAYER_TWO_LEVEL.get(24, [])) >= 3)
chk("25 l1>=3", len(ra._SUBLAYER_TWO_LEVEL.get(25, [])) >= 3)
chk("26 l1>=3", len(ra._SUBLAYER_TWO_LEVEL.get(26, [])) >= 3)

print("=== [10] 老 23 类映射不变 ===")
ORIG_MAP = {1:"data", 18:"data", 19:"data", 21:"data",
            2:"timing", 8:"timing", 9:"timing", 16:"timing",
            7:"signal", 12:"signal", 22:"signal",
            3:"judgment", 6:"judgment", 11:"judgment", 13:"judgment", 14:"judgment",
            15:"execution", 17:"execution", 20:"execution",
            4:"regime", 5:"regime", 10:"regime", 23:"regime"}
all_kept = all(ra._REASON_TO_LAYER.get(c) == ORIG_MAP.get(c) for c in range(1, 24))
chk("老 23 映射全保留", all_kept)

print("=== [11] o.LAYERS[0]=event_logic ===")
chk("first layer", o.LAYERS[0] == "event_logic")

print("=== [12] SUB_LAYERS['event_logic'] ≥ 5 ===")
chk("subs >= 5", len(o.SUB_LAYERS.get("event_logic", [])) >= 5)

print("=== [13] LAYER_WEIGHTS['event_logic']=1 ===")
chk("weight=1", o.LAYER_WEIGHTS.get("event_logic") == 1)

print("=== [14] E014/E015/E016 常量 ===")
chk("E014", rc.E014_REASONING_CHAIN_BROKEN == "E014")
chk("E015", rc.E015_CONCEPT_MISALIGNED == "E015")
chk("E016", rc.E016_CROSS_ASSET_DIRECTION_WRONG == "E016")

print("=== [15] check_event_logic 可调用 ===")
chk("callable", callable(rc.check_event_logic))

print("=== [16] run_all_checkers 集成 event_logic ===")
errs = rc.run_all_checkers({"id":999, "suggested_action":"BUY", "target_asset":"XAU",
                             "reasoning":"", "factor_ranking":"[]"})
has_el = any(e.get("source") == "event_logic" for e in errs)
chk("含 event_logic 来源", has_el)

print("=== [17] _ANALYSIS_STEPS 8 段 ===")
chk("count=8", len(rc._ANALYSIS_STEPS) == 8)
chk("含总框架", "总框架" in rc._ANALYSIS_STEPS)
chk("含反共识结论", "反共识结论" in rc._ANALYSIS_STEPS)

print("=== [18] _ERROR_CODE_GUIDANCE 含 E014/E015/E016 ===")
chk("E014", "E014" in rc._ERROR_CODE_GUIDANCE)
chk("E015", "E015" in rc._ERROR_CODE_GUIDANCE)
chk("E016", "E016" in rc._ERROR_CODE_GUIDANCE)

print("=== [19] format_compact_guidance 排序 ===")
out = rc.format_compact_guidance(["E003","E014","E015","E016","E001"])
pos_e014 = out.find("E014"); pos_e003 = out.find("E003")
chk("E014 在 E003 前", pos_e014 != -1 and pos_e003 != -1 and pos_e014 < pos_e003)

print("=== [20] 旧 23 类 name 不丢 ===")
chk("code 1", ra._REASON_NAMES.get(1) == "数据缺失")
chk("code 23", ra._REASON_NAMES.get(23) == "经验误用/过拟合")

print("=== [21] BUY BTC + BTC cross=下跌 → E016 ===")
errs = rc.run_all_checkers({"id":1, "suggested_action":"BUY", "target_asset":"BTC",
                             "sentiment_score":0.4, "reasoning":"BTC会涨。",
                             "factor_ranking":json.dumps([{"factor":"宏观"},{"factor":"情绪"}]),
                             "cross_asset_likely":json.dumps([{"what":"BTC","direction":"下跌"}])})
e016 = [e for e in errs if e["error_code"]=="E016"]
chk("E016 触发", len(e016) >= 1, f"found {len(e016)}")

print("=== [22] BUY XAU 缺 5 段 → E014 critical ===")
errs = rc.run_all_checkers({"id":2, "suggested_action":"BUY", "target_asset":"XAU",
                             "sentiment_score":0.4, "reasoning":"黄金会涨。",
                             "factor_ranking":json.dumps([{"factor":"宏观"}]),
                             "cross_asset_likely":"[]"})
e014 = [e for e in errs if e["error_code"]=="E014"]
chk("E014 触发", len(e014) >= 1)
if e014:
    chk("severity=critical", e014[0]["severity"] == rc.SEVERITY_CRITICAL)
    chk("source=event_logic", e014[0]["source"] == "event_logic")

print(f"\n=== 总计：{passes}/{total} pass，{fails} fail ===")
sys.exit(0 if fails == 0 else 1)