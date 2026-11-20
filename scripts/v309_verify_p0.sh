#!/usr/bin/env bash
# v309_verify_p0.sh — P0 (三维度映射 / 技术信号入打分 / 归因回流) 验证
# ============================================================================
# 用途: 一次跑完 P0 三个改动的全部可离线检查项。
# 设计: 每一步独立, 某步失败不阻断后续 (全部结果最后汇总), 便于定位。
#
# 用法:
#   bash scripts/v309_verify_p0.sh            # 静态检查 + 自检 (不碰 DB)
#   bash scripts/v309_verify_p0.sh --with-db  # 额外跑 DB 回填 (dry-run)
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
SRC="backend/src_python"
PASS=0
FAIL=0
FAILED_STEPS=()

step() {
  local name="$1"; shift
  echo ""
  echo "── $name ──"
  if "$@"; then
    echo "  ✓ PASS: $name"
    PASS=$((PASS+1))
  else
    echo "  ✗ FAIL: $name"
    FAIL=$((FAIL+1))
    FAILED_STEPS+=("$name")
  fi
}

echo "=============================================================="
echo " v309 P0 验证  ($(date '+%Y-%m-%d %H:%M:%S'))"
echo " repo: $ROOT"
echo "=============================================================="

# ── 1. 语法编译 (9 个改动文件) ────────────────────────────────────────────
step "1. py_compile 全部改动文件" python3 -m py_compile \
  "$SRC/reflection_dimension_map.py" \
  "$SRC/backfill_reflection_dimensions.py" \
  "$SRC/selftest_reflection_dimension_map.py" \
  "$SRC/selftest_v309_endpoints.py" \
  "$SRC/check_v309_schema.py" \
  "$SRC/db.py" \
  "$SRC/time_utils.py" \
  "$SRC/paper_reflection_worker.py" \
  "$SRC/bridge_server.py" \
  "$SRC/calc_event_move_attribution.py" \
  "$SRC/engine/ai_worker.py" \
  "backend/tests/test_reflection_dimension_map.py" \
  "backend/tests/test_time_utils.py"

# ── 2. 映射表口径自检 (纯离线, 不碰 DB) ───────────────────────────────────
step "2. 映射表口径自检" python3 "$SRC/selftest_reflection_dimension_map.py"

# ── 3. pytest (若环境装了) ────────────────────────────────────────────────
# 关键: 不能写成 `python3 -m pytest ... | tail -5` —— 管道的退出码取自 tail,
# pytest 失败也会返回 0 (早期版本就因此把 "1 failed" 报成 PASS)。
step "3. pytest test_reflection_dimension_map.py" bash -c \
  'python3 -m pytest backend/tests/test_reflection_dimension_map.py -q; rc=$?; echo "[pytest rc=$rc]"; exit $rc'

# ── 3b. pytest 时间口径单测 (v309 P1) ───────────────────────────────────────
step "3b. pytest test_time_utils.py (时间口径唯一入口)" bash -c \
  'python3 -m pytest backend/tests/test_time_utils.py -q; rc=$?; echo "[pytest rc=$rc]"; exit $rc'

# ── 4. schema 一致性: db.py 必须同时定义列与 ensure_columns ───────────────
step "4. db.py v309 列定义 + ensure_columns 挂载" bash -c \
  'grep -q "_TRADE_REFLECTIONS_V2_V309_DIMENSION_COLUMNS" '"$SRC"'/db.py &&
   grep -q "_ensure_columns(conn, \"trade_reflections_v2\", _TRADE_REFLECTIONS_V2_V309_DIMENSION_COLUMNS)" '"$SRC"'/db.py'

# ── 5. 写入方: paper_reflection_worker 必须写 7 个新列 ────────────────────
step "5. paper_reflection_worker 写入 7 个 v309 列" bash -c \
  'grep -q "dimension, subcategory, hit, actual_outcome_pct" '"$SRC"'/paper_reflection_worker.py &&
   grep -q "confidence_at_time, reflection_text, settled)" '"$SRC"'/paper_reflection_worker.py &&
   grep -q "_resolve_dimension_for_decision" '"$SRC"'/paper_reflection_worker.py'

# ── 6. 未来函数纪律: 技术信号只用 uses_future=0 ───────────────────────────
step "6. P0-2 技术信号排除 uses_future=1" bash -c \
  'grep -q "uses_future = 0" '"$SRC"'/calc_event_move_attribution.py'

# ── 7. 未来函数纪律: 归因块只取历史 (move_ts < now) ───────────────────────
step "7. P0-3 归因块只取历史行" bash -c \
  'grep -q "move_ts < ?" '"$SRC"'/engine/ai_worker.py'

# ── 8. 窗口过滤修复: created_at 是 TEXT, 必须用统一时间口径 ──────────────
# v309 P0 原来的检查项是:
#   grep -qE "strftime\(\"%Y-%m-%d %H:%M:%S\"\)" bridge_server.py
# 这是**假检查**: 只证明文件里出现过这个字符串, 不证明过滤逻辑对不对。
# 实际 bug: created_at 列里同时有两种落库格式 (T 分隔=本机本地时间 /
# 空格分隔=UTC), 只换成空格格式字符串, 同一日期内字典序仍然错乱。
# 现在改成**行为测试**: 内存 SQLite 复现两种格式, 断言过滤结果正确。
step "8. created_at 窗口过滤口径正确 (行为测试, 非 grep)" bash -c '
  python3 - <<'"'"'PYEOF'"'"'
import os, sys
SRC = "backend/src_python"
sys.path.insert(0, SRC)
import selftest_v309_endpoints as st
st._time_window_filter_is_correct()
st._time_utils_roundtrip()
print("  OK: 两种历史落库格式的窗口过滤结果一致且正确")
PYEOF'

# ── 8b. 回填脚本不得引用不存在的列 (is_correct / layer 等) ─────────────────
# 关键: 不能用 `grep` 全文匹配! 早期版本用
#   ! grep -qE "\bis_correct\b|\blayer, sub_layer, signal_name" backfill_reflection_dimensions.py
# 结果 **永远 FAIL** —— 因为回填脚本的 docstring 里就写着
#   "trade_reflections_v2 **没有** is_correct / verdict 列"
# grep 命中的是**说明文字**而不是 SQL, 假阳性把验证脚本变成永远红的噪音。
#
# 正确做法: 用 ast 只取**可执行代码里的字符串常量**(排除 docstring),
# 再检查这些 SQL 片段是否引用了不存在的列。
step "8b. 回填脚本不使用不存在的列名 (AST, 排除 docstring)" python3 - <<'PYEOF'
import ast
import sys

SRC = "backend/src_python/backfill_reflection_dimensions.py"
with open(SRC, encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=SRC)

# 收集所有 docstring 节点 (模块/类/函数的第一个字符串表达式) —— 这些不是 SQL
docstring_nodes = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstring_nodes.add(id(body[0].value))

FORBIDDEN = ("is_correct", "layer, sub_layer, signal_name")
bad = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        continue
    if id(node) in docstring_nodes:
        continue  # docstring 是说明文字, 不是 SQL
    for token in FORBIDDEN:
        if token in node.value:
            bad.append(f"line {node.lineno}: 引用了 '{token}'")

if bad:
    print("  回填脚本的 SQL 引用了不存在的列:", file=sys.stderr)
    for b in bad:
        print(f"    {b}", file=sys.stderr)
    sys.exit(1)
print("  OK: 无效列名只出现在说明文字里, SQL 干净")
PYEOF

# ── 8c. 端点函数真调用 (静态 grep 抓不到 NameError / 未定义别名) ────────────
# 之前 `/api/v237/reflection-dimension-map` 里 import 别名是 _rdm 却调用 rdm.xxx,
# 静态检查全过、实际调用直接 NameError —— 所以这里必须**真 import 并调用**。
step "8c. bridge_server 端点函数可真实调用 (无 NameError)" \
  python3 "$SRC/selftest_v309_endpoints.py"

# ── 9. 可选: DB 回填 dry-run ─────────────────────────────────────────────
if [ "${1:-}" = "--with-db" ]; then
  # 9a. 先看真实 schema —— 之前"no such column: is_correct"这种错误被吞掉后
  #     脚本还报 PASS, 现在必须先确认列到底在不在, 失败要能看到原因。
  step "9a. DB schema 检查 (trade_reflections_v2 v309 列)" \
    python3 "$SRC/check_v309_schema.py"
  step "9b. 历史回填 dry-run" python3 "$SRC/backfill_reflection_dimensions.py" --dry-run
else
  echo ""
  echo "── 9. DB schema 检查 + 历史回填 dry-run ──"
  echo "  (跳过 — 传 --with-db 启用)"
fi

echo ""
echo "=============================================================="
echo " 结果: PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo " 失败项: ${FAILED_STEPS[*]}"
  echo "=============================================================="
  exit 1
fi
echo " 全部通过"
echo "=============================================================="
exit 0
