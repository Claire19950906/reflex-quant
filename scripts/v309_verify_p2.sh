#!/usr/bin/env bash
# v309_verify_p2.sh — P2: 时间口径一致性扫描 (全库 TEXT 时间列)
# ============================================================================
# 用途: 找出"cutoff 分隔符与被比较列的落库格式不一致"的端点。
#
# 背景 (v309 P2 发现的真 bug):
#   库里 TEXT 时间列有两种落库格式, 各表 DEFAULT 不同:
#     · 'T' 分隔 ('2026-10-25T08:00:00')  ← ai_decisions.created_at 等
#     · ' ' 分隔 ('2026-10-25 08:00:00')  ← review_insights.created_at 等
#   SQLite 字符串比较按字节序: ' '(0x20) < 'T'(0x54)。
#   → 用 'T' cutoff 去比空格存储的列, **cutoff 当天的行被整片漏掉**。
#
#   v302+ 那次修复把 cutoff 统一成 'T' 分隔, 对 ai_decisions 是对的,
#   但 review_insights 的存储格式没跟着改 → 修复对它是反效果。
#
# 检查方式: AST 扫描 api_server.py / bridge_server.py, 列出每个
#   "比较 TEXT 时间列" 的函数 + 它用的 cutoff helper, 人工核对配对。
#   脚本**不做自动判定** (不同表的格式只能逐个确认), 只负责不漏看。
#
# 用法:
#   bash scripts/v309_verify_p2.sh
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
echo " v309 P2 验证  ($(date '+%Y-%m-%d %H:%M:%S'))"
echo " repo: $ROOT"
echo "=============================================================="

# ── 1. 扫描: 列出所有 TEXT 时间列比较点 + cutoff helper ────────────────────
step "1. 扫描 TEXT 时间列比较点 (输出供人工核对)" \
  python3 "$SRC/scan_text_ts_columns.py"

# ── 2. review_insights 不得用 'T' 分隔 cutoff (本次修复的回归防线) ─────────
step "2. review_insights 用空格 cutoff (行为+AST 回归)" \
  bash -c 'python3 -m pytest backend/tests/test_api_server.py -q; rc=$?; echo "[pytest rc=$rc]"; exit $rc'

# ── 3. 静态编译 ───────────────────────────────────────────────────────────
step "3. py_compile 改动的文件" python3 -m py_compile \
  "$SRC/api_server.py" \
  "$SRC/time_utils.py" \
  "backend/tests/test_api_server.py"

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
