#!/bin/bash
# v291: 事件-行情归因 worker — 每 5 分钟扫一次临界点 + 关联事件
# 不依赖 api daemon, 直接调模块函数. 日志 → ~/.trident/log/event_market_attributor.*.log
#
# 2026-10-24 修复:
#   原调用 event_market_attributor.py --once --lookback-hours 24,
#   但该脚本在仓库中不存在 (只有 calc_event_move_attribution.py),
#   导致每 5 分钟必然失败, event_move_attribution 长期为 0 行。
#   改为调用实际存在的 calc_event_move_attribution.py (与 bridge 的
#   /api/v291/event-move-attribution/rebuild 端点同一契约: --days N)。
#   --days 3 = 滚动窗口取最近 3 天。
#   实测 critical_price_moves 的检测延迟 (created_at - ts) 达 37~65 小时,
#   若用 --days 1 会漏掉刚检测到的 move (实测扫到 0 个 move)。
#   取 3 天可覆盖最坏 65h 延迟 + 1 天余量。
#   脚本内按 cutoff 先 DELETE 再重算, 重复执行是幂等的。
set -o pipefail

BACKEND_SRC="/Users/claire/Desktop/金融1.0/backend/src_python"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="/Users/claire/.trident/log"

mkdir -p "$LOG_DIR"
cd "$BACKEND_SRC" || exit 1

exec "$PY" calc_event_move_attribution.py --days 3
