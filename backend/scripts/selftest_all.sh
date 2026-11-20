#!/usr/bin/env bash
# 一条命令自检：离线闭环 → 回归测试 →（可选）真数据验收
#   bash scripts/selftest_all.sh          只跑离线 + 核心回归
#   bash scripts/selftest_all.sh --full   跑全量 pytest
#   bash scripts/selftest_all.sh --with-data  再跑一次 acceptance_check（只读生产库）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
MODE_FULL=0; WITH_DATA=0
for a in "$@"; do
  case "$a" in
    --full) MODE_FULL=1 ;;
    --with-data) WITH_DATA=1 ;;
  esac
done

echo "== 1/3 离线闭环 =="
python3 scripts/offline_selftest.py || exit 1

echo
echo "== 2/3 回归测试 =="
if [ "$MODE_FULL" = "1" ]; then
  python3 -m pytest tests/ -q || exit 1
else
  # 快速门禁集：pytest.ini 注册的 smoke marker（离线、纯单元、秒级）
  python3 -m pytest -m smoke -q || exit 1
fi

if [ "$WITH_DATA" = "1" ]; then
  echo
  echo "== 3/3 真数据验收（只读）=="
  python3 scripts/acceptance_check.py || true
else
  echo
  echo "（跳过真数据验收；需要时加 --with-data，或直接 python3 scripts/acceptance_check.py）"
fi
