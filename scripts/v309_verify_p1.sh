#!/usr/bin/env bash
# v309_verify_p1.sh — v309 P1: 时间口径统一 + 参数边界 验证
# ============================================================================
# 用途: 一次性跑完 P1 的全部离线检查项 (不碰 DB, 不写库)。
#
# 为什么单独一个脚本:
#   v309 P0 的验证脚本第 8 项是 `grep 'strftime("%Y-%m-%d %H:%M:%S")'` ——
#   那是**假检查**: 只证明源码里出现过这个字符串, 不证明过滤逻辑对不对。
#   实际 bug 是: created_at 列里同时有两种落库格式 (T 分隔 / 空格分隔),
#   同一日期内字典序必然错乱; 而且 trading_signals 端点拿 epoch 整数比 TEXT 列,
#   SQLite 类型亲和性让它**永远返回空**。
#   P1 把这类检查全部换成**行为测试** (内存 SQLite 复现真实数据形状)。
#
# 用法:
#   bash scripts/v309_verify_p1.sh
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
echo " v309 P1 验证  ($(date '+%Y-%m-%d %H:%M:%S'))"
echo " repo: $ROOT"
echo "=============================================================="

# ── 1. 语法编译 ────────────────────────────────────────────────────────────
step "1. py_compile 全部改动文件" python3 -m py_compile \
  "$SRC/time_utils.py" \
  "$SRC/bridge_server.py" \
  "$SRC/paper_reflection_worker.py" \
  "$SRC/config.py" \
  "$SRC/db.py" \
  "$SRC/selftest_v309_endpoints.py" \
  "backend/tests/test_time_utils.py"

# ── 2. pytest 时间口径单测 ─────────────────────────────────────────────────
step "2. pytest test_time_utils.py" bash -c \
  'python3 -m pytest backend/tests/test_time_utils.py -q; rc=$?; echo "[pytest rc=$rc]"; exit $rc'

# ── 3. 端点自检 (含 P1 新增的两项行为测试) ─────────────────────────────────
step "3. selftest_v309_endpoints.py (端点真调用 + P1 行为测试)" \
  python3 "$SRC/selftest_v309_endpoints.py"

# ── 4. 写入方必须用统一口径落库 ────────────────────────────────────────────
# 关键: 不能只 grep 'time.strftime' —— 那样旧写法也会通过。
# 要求 paper_reflection_worker 与 bridge_server 都走 time_utils。
step "4. 写入方统一走 time_utils (不再自己拼 strftime)" bash -c '
  grep -q "time_utils.bj_now_str()" '"$SRC"'/paper_reflection_worker.py &&
  grep -q "import time_utils" '"$SRC"'/paper_reflection_worker.py &&
  grep -q "time_utils.bj_now_str()" '"$SRC"'/bridge_server.py &&
  grep -q "import time_utils" '"$SRC"'/bridge_server.py'

# ── 5. 读端点必须用归一化表达式, 不能裸比 created_at ─────────────────────
step "5. 读端点用归一化表达式 (禁止裸 created_at >= ?)" bash -c '
  python3 - <<'"'"'PYEOF'"'"'
import re, sys
src = open("backend/src_python/bridge_server.py", encoding="utf-8").read()
# 找所有直接拿 created_at 与 ? 比较的地方
bad = re.findall(r"created_at\s*(>=|<=|<|>)\s*\?", src)
if bad:
    print(f"  这些地方仍直接拿 TEXT 列与参数比较: {bad}", file=sys.stderr)
    sys.exit(1)
if "sql_bj_ts_expr" not in src:
    print("  bridge_server 完全没用归一化表达式", file=sys.stderr)
    sys.exit(1)
print("  OK: 所有 created_at 窗口过滤都走归一化表达式")
PYEOF'

# ── 6. hours 参数不得再乘 86400 (当小时用) ─────────────────────────────────
step "6. hours 参数不再误乘 86400" bash -c '
  if grep -nE "hours \* 86400" '"$SRC"'/bridge_server.py; then
    echo "  hours 仍在乘 86400 (那是天数, 不是小时)" >&2
    exit 1
  fi
  echo "  OK"'

# ── 7. 所有端点的 limit/days/hours 都有上下界 ─────────────────────────────
step "7. 端点 limit/days/hours 参数都有上下界" python3 - <<'PYEOF'
import ast, sys
src = open("backend/src_python/bridge_server.py", encoding="utf-8").read()
tree = ast.parse(src)
bad = []
n = 0
for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    is_endpoint = any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and d.func.attr in ("get", "post", "put", "delete", "patch")
        for d in node.decorator_list)
    if not is_endpoint:
        continue
    n += 1
    for a in node.args.args:
        if a.arg not in ("limit", "days", "hours"):
            continue
        # 该参数必须声明成 Annotated[..., Query(...)] 才算有上下界
        has_bound = False
        if a.annotation is not None:
            for sub in ast.walk(a.annotation):
                if isinstance(sub, ast.Call):
                    fname = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                    if fname == "Query":
                        has_bound = True
        if not has_bound:
            bad.append(f"{node.name}({a.arg})")
assert n > 0, "没找到端点 (AST 规则失效?)"
if bad:
    print(f"  这些端点参数缺上下界: {bad}", file=sys.stderr)
    sys.exit(1)
print(f"  OK: 已检查全部 {n} 个端点")
PYEOF

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
