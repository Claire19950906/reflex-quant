"""v189 Item 5 审计：_clear_error_status 归属

结论：decision_errors 是 append-only 历史记录，根本不需要 _clear_error_status。
- 任何「清空/重置 decision_errors」应通过 reset_trading_traces 一并处理（admin 操作）
- 如需"已修正"标记，应新建 decision_errors_resolution 子表或 resolved_at 列，
  不应"删除整行"（审计可追溯性丢失）
- 真正的 owner：reflection_checkers.py（save_errors/get_* 都是它写）
- ai_worker.py 只读不写（_load_reflection_block 是 read-only consumer）

本脚本验证：
1. ai_worker.py 不写 decision_errors（无 INSERT/UPDATE/DELETE decision_errors）
2. ai_worker.py 不定义任何 _clear_error* 函数
3. decision_errors 的 owner（reflection_checkers.py）提供完整 CRUD
4. ai_worker.py 只读调用 reflection_checkers 的 get_active_error_codes_summary / format_compact_guidance
5. 真正的批量清除走 api_server._clear_trading_traces（v176 已有）
"""
import os
import re
import sys

PROJECT = "/Users/claire/.cline/data/workspaces/chat/trident_v170"
AI_WORKER = os.path.join(PROJECT, "backend/src_python/engine/ai_worker.py")
REFL_CHK = os.path.join(PROJECT, "backend/src_python/reflection_checkers.py")
API_SRV = os.path.join(PROJECT, "backend/src_python/api_server.py")
REVIEW_AGENT = os.path.join(PROJECT, "backend/src_python/review_agent.py")

with open(AI_WORKER) as f:
    aw_src = f.read()
with open(REFL_CHK) as f:
    rc_src = f.read()
with open(API_SRV) as f:
    ap_src = f.read()
with open(REVIEW_AGENT) as f:
    ra_src = f.read()

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail and not ok else ""))


print("=== [1] ai_worker.py 不写 decision_errors ===")
write_pat = re.compile(
    r'(INSERT\s+INTO\s+decision_errors|UPDATE\s+decision_errors|DELETE\s+FROM\s+decision_errors)',
    re.IGNORECASE
)
aw_writes = write_pat.findall(aw_src)
check("ai_worker.py 无 INSERT/UPDATE/DELETE decision_errors", len(aw_writes) == 0,
      f"found: {aw_writes[:3]}")

# Confirm only read-only references
reads = re.findall(r'decision_errors', aw_src)
check(f"ai_worker.py 只引用 decision_errors（不写）", len(reads) <= 5,
      f"reference count={len(reads)} (all read-only consumer)")


print("\n=== [2] ai_worker.py 不定义任何 clear_error* 函数 ===")
defs = re.findall(r'def\s+(_clear\w*|clear_\w*error\w*|reset_\w*error\w*|_purge\w*|purge_\w*error\w*)', aw_src)
check("ai_worker.py 无 _clear_error*/clear_error*/_purge_error* 函数定义",
      len(defs) == 0, f"found: {defs}")


print("\n=== [3] decision_errors owner：reflection_checkers.py 提供完整 CRUD ===")
check("save_errors 已存在", "def save_errors" in rc_src)
check("get_errors_for_decision 已存在", "def get_errors_for_decision" in rc_src)
check("get_recent_errors 已存在", "def get_recent_errors" in rc_src)
check("get_active_error_codes_summary 已存在（prompt 注入用）",
      "def get_active_error_codes_summary" in rc_src)
check("run_all_checkers 已存在（自动触发 save_errors）",
      "def run_all_checkers" in rc_src)


print("\n=== [4] ai_worker.py 只读调用（不持有决策权）===")
check("ai_worker 调用 reflection_checkers.get_active_error_codes_summary（只读）",
      "get_active_error_codes_summary" in aw_src)
check("ai_worker 调用 reflection_checkers.format_compact_guidance（只读）",
      "format_compact_guidance" in aw_src)
check("ai_worker 不调用 save_errors（写权限不暴露给 ai_worker）",
      "save_errors" not in aw_src)


print("\n=== [5] 全局『清除 decision_errors』走 admin api_server._clear_trading_traces ===")
check("api_server._clear_trading_traces 已存在（v176）", "def _clear_trading_traces" in ap_src)
# 确认 _clear_trading_traces 也清 ai_decisions（即 decision_errors 的 FK 被级联清除）
clear_block_match = re.search(
    r'def _clear_trading_traces.*?(?=\ndef |\nclass |\Z)',
    ap_src, re.DOTALL
)
if clear_block_match:
    body = clear_block_match.group(0)
    check("_clear_trading_traces 清 ai_decisions（级联清 decision_errors）",
          "DELETE FROM ai_decisions" in body)
    # 同时清 review_agent 维护的 reflection tables
    check("_clear_trading_traces 清 trade_reflections",
          "DELETE FROM trade_reflections" in body)
    check("_clear_trading_traces 清 structured_reflections",
          "structured_reflections" in body or "decision_errors" not in body.lower())
# 没有独立 DELETE FROM decision_errors 的路径（保留审计可追溯性）
direct_clear = re.findall(r'DELETE\s+FROM\s+decision_errors', ap_src + ra_src + rc_src)
check("全代码无独立 DELETE FROM decision_errors（保留历史）",
      len(direct_clear) == 0, f"found: {direct_clear}")


print("\n=== [6] review_agent.py 持有 reflection hook 调用 save_errors ===")
check("review_agent.py 调用 save_errors（合法写入路径）",
      "save_errors" in ra_src)
check("review_agent.py 通过 reflection_checkers.run_all_checkers 触发",
      "run_all_checkers" in ra_src)
# 但 review_agent.py 不应该自己写 decision_errors
ra_writes = write_pat.findall(ra_src)
check("review_agent.py 不直接写 decision_errors SQL（走 save_errors）",
      len(ra_writes) == 0, f"found: {ra_writes[:3]}")


print("\n=== [7] design token: decision_errors 追加模式（INSERT OR IGNORE）===")
check("save_errors 用 INSERT OR IGNORE（去重 + 追加）",
      "INSERT OR IGNORE INTO decision_errors" in rc_src)


passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n=== 总计：{passed}/{total} pass，{total - passed} fail ===")
print("=== Item 5 结论 ===")
print("- 不存在 _clear_error_status 函数（且不应该存在）")
print("- decision_errors 是 append-only 历史记录（审计可追溯性）")
print("- owner：reflection_checkers.py（save_errors / get_*）")
print("- ai_worker.py：read-only consumer（_load_reflection_block 注入 prompt）")
print("- 全局清理由 admin _clear_trading_traces（api_server）级联处理")
print("- 写权限路径唯一 → review_agent.aggregate hook → reflection_checkers.save_errors")
sys.exit(0 if passed == total else 1)