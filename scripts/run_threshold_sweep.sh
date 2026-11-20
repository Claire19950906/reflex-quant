#!/bin/bash
# run_threshold_sweep.sh — 每周重算品种阈值矩阵，并打印政策体检结论
#
# 为什么需要它：阈值政策读的是 runtime/backtest/threshold_map_*.json 这个文件。
# 文件不会自己更新 —— 行情多了一周、品种多了一个，矩阵还是老的，
# 系统会一直按旧阈值跑而没人知道。这条作业把"重算 + 自检"绑在一起。
set -o pipefail
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend/src_python" || exit 1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 重算阈值矩阵 ==="
"$PY" run_backtest.py --all --sweep --threshes 2,3,5,8 --apply 2>&1 | tail -30
echo "--- 政策体检 ---"
"$PY" "$ROOT/scripts/audit_threshold_policy.py"
rc=$?
echo "=== 完成 rc=$rc ==="
exit $rc
