#!/bin/bash
# v329 验证：归档 + 快照预算 + 全链路
OUT=/tmp/v329_verify.txt
DB=/Users/claire/Desktop/金融1.0/backend/trident_event_bus.db
LOG=/Users/claire/.trident/log/engine.out.log
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
cd /Users/claire/Desktop/金融1.0/backend

{
echo "===== ① 编译 ====="
$PY -m py_compile src_python/engine/ai_worker.py src_python/market_snapshot.py \
  src_python/realtime_filter.py tests/test_v322_head_of_line_block.py \
  tests/test_v322_breaker_epoch_and_gate_freeze.py && echo COMPILE_OK

echo
echo "===== ② 本轮测试 ====="
$PY -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py \
  tests/test_v322_head_of_line_block.py tests/test_v322_watchdog_active_batch.py \
  tests/test_gate_deferred_v320.py tests/test_probe_health.py \
  tests/test_no_fabrication.py "tests/test_minimax_weight_override.py" \
  "tests/test_minimax_weight_override 2.py" -q 2>&1 | tail -8

echo
echo "===== ③ 重启 + 归档 + 快照耗时 ====="
launchctl kickstart -k gui/$(id -u)/com.trident.engine
sleep 120
grep -a '归档超龄新闻\|SNAPSHOT' "$LOG" | tail -10 | cut -c1-140

echo
echo "===== ④ 认领 / 落库 / 402 ====="
grep -a 'HTTP 402\|claimed\|Batch done\|Batch persist\|通道体检' "$LOG" | tail -12 | cut -c1-150

echo
echo "===== ⑤ 队列构成 + 产出 ====="
sqlite3 "$DB" "
select 'PENDING_real='||(select count(*) from raw_news where status='PENDING' and coalesce(is_noise,0)=0),
       'noise_zombie='||(select count(*) from raw_news where status='PENDING' and coalesce(is_noise,0)=1),
       'FILTERED='||(select count(*) from raw_news where status='FILTERED'),
       'DONE_30m='||(select count(*) from ai_decisions where created_at > datetime('now','-30 minutes'));
"

echo
echo "===== ⑥ 队列趋势 5 分钟 ====="
for i in 1 2 3 4 5; do
  sqlite3 "$DB" "
  select datetime('now','localtime'),
         'PENDING='||(select count(*) from raw_news where status='PENDING' and coalesce(is_noise,0)=0),
         'DONE_15m='||(select count(*) from ai_decisions where created_at > datetime('now','-15 minutes'));
  "
  sleep 60
done

echo
echo "===== ⑦ 额度 ====="
$PY scripts/budget_status.py 2>&1 | head -12
} > "$OUT" 2>&1

echo "DONE -> $OUT"
