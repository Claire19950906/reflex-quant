#!/usr/bin/env bash
# verify_judgment.sh —— 迭代后"跑一下就好测"的唯一入口
#
# 做三件事，顺序固定（前一步失败就不往下跑，避免拿坏数据得结论）：
#   1) 口径自检（4 个答案已知的合成世界 + 无数据/缺表等边界），秒级
#   2) 契约测试（真实统计口径、基线、净期望、接线一致性）
#   3) 出当天报告：判断力 vs 市场（真实数据；没数据也会明确说"什么时候能测"）
#
# 用法：
#   bash scripts/verify_judgment.sh              # 写报告到 docs/reports/
#   bash scripts/verify_judgment.sh --no-write   # 只看不写（迭代中频繁自查用）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
NO_WRITE="${1:-}"

echo "== 1/3 口径自检 =="
"$PY" backend/src_python/judgment_benchmark.py --selftest

echo
echo "== 2/3 契约测试 =="
"$PY" -m pytest backend/tests/test_judgment_benchmark.py -q

echo
echo "== 3/3 判断力 vs 市场（真实库）=="
if [[ "$NO_WRITE" == "--no-write" ]]; then
  "$PY" scripts/judgment_vs_market.py --no-write
else
  "$PY" scripts/judgment_vs_market.py
fi
