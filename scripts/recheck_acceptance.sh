#!/bin/bash
# recheck_acceptance.sh — 2026-11-22 之后跑一次：清空那批 SKIP 该转成什么了
#
# 背景：2026-11-15 清空历史数据后，验收里 15 个"0 样本"的项被判成 SKIP
# （TRIDENT_EPOCH_GRACE_H=168，7 天宽限）。宽限一过，仍然没样本的项会变回 FAIL,
# 那时候的 FAIL 才是真的"链路没跑起来"，不是清空的副作用。
#
# 基线（2026-11-15）：11 PASS / 0 FAIL / 15 SKIP，退出码 0
#
# 用法：bash scripts/recheck_acceptance.sh
set -o pipefail

PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend" || exit 1

echo "=== 0/5 launchd 作业体检（只看 24h 内新日志 + 退出码） ==="
"$PY" scripts/audit_jobs.py 2>&1 | head -25
echo "(非 0 退出码/日志停更才算出问题；database is locked 属并发告警，见体检报告)"

echo "=== 0.5/5 表级存活盘点（0 行 / 停更） ==="
"$PY" scripts/audit_tables.py 2>&1 | head -12

echo "=== 1/4 验收（--no-http） ==="
"$PY" src_python/capability_acceptance.py --no-http 2>&1 | tail -30
echo "exit=$?"

echo
echo "=== 2/4 清空是否被回填污染（只数 epoch 之前残留，应全为 0） ==="
"$PY" - <<'PYEOF'
import sqlite3, datetime
db = sqlite3.connect("file:trident_event_bus.db?mode=ro", uri=True)
started = db.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()[0]
if not started:
    print("无 data_epoch —— 无法判定")
    raise SystemExit(0)
ts0 = int(datetime.datetime.strptime(str(started), "%Y-%m-%d %H:%M:%S").timestamp()) - 86400
# 留 1 天余量：日频信号的 ts 锚在当天 00:00，会早于纪元几小时，属正常
# (表, 候选时间列) —— 列名各表不同，逐个试
probes = [
    ("critical_price_moves", ["move_ts", "ts"]),
    ("ai_decisions", ["ts", "created_ts"]),
    ("trade_decisions", ["ts", "created_ts"]),
    ("signal_weight_history", ["ts", "created_ts"]),
    ("event_move_attribution", ["move_ts"]),
    ("event_move_attribution_pending", ["move_ts"]),
    # tech_signals 是"用干净行情重算出来的派生缓存"，ts 是 K 线时间，
    # 天生落在纪元之前（--history 回填后约 1.3 万行）。它不承载决策/归因，
    # 不构成"旧数据没清掉"，改看"计算时间" created_ts。
    ("tech_signals", ["created_ts"]),
    ("missed_signals", ["ts", "created_ts"]),
    ("trade_reflections_v2", ["ts", "created_ts"]),
    ("abnormal_signals", ["ts", "created_ts"]),
    ("event_swing_ledger", ["ts", "created_ts"]),
    ("shadow_ledger", ["created_ts", "ts"]),
]
total_bad = 0
for tbl, cols in probes:
    try:
        info = {r[1] for r in db.execute(f"PRAGMA table_info({tbl})")}
    except Exception:
        continue
    col = next((c for c in cols if c in info), None)
    if not col:
        continue
    try:
        n_all = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        n_old = db.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col} < ?", (ts0,)).fetchone()[0]
    except Exception as e:
        print(f"  {tbl}: 读失败 {e}")
        continue
    total_bad += n_old
    flag = "OK" if n_old == 0 else "污染"
    print(f"  [{flag}] {tbl}: 共 {n_all:,} 行, epoch 之前 {n_old:,} 行")
print(f"\nepoch 之前残留合计: {total_bad:,}（0 = 清空仍然有效）")
PYEOF

echo
echo "=== 2.5/4 跨表一致性（悬空 move_id / 队列 gone / 重复行 / 别名残留） ==="
"$PY" src_python/check_integrity.py --strict
echo "exit=$?"

echo
echo "=== 2.6/4 阈值政策是否在生效（矩阵缺失/过期/开关关掉都会静默回落） ==="
"$PY" "$ROOT/scripts/audit_threshold_policy.py"
echo "exit=$?"

echo
echo "=== 2.7/4 AI 通道体检（全断时 ai_decisions=0 是正确行为，不是 bug） ==="
"$PY" "$ROOT/scripts/audit_ai_channels.py"
echo "exit=$? （0=至少一条通道可用；2=全断，需补 key 或加载本地模型）"

echo
echo "=== 3/4 纪元 ==="
"$PY" - <<'PY'
import sqlite3, time
db = sqlite3.connect("file:trident_event_bus.db?mode=ro", uri=True)
try:
    started = db.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()[0]
    print("data_epoch:", started)
    ts = int(time.mktime(time.strptime(str(started), "%Y-%m-%d %H:%M:%S")))
    print("已运行: %.1f 天（宽限 7 天）" % ((time.time() - ts) / 86400))
except Exception as e:
    print("读取纪元失败:", e)
PY

cat <<'TIP'

以上对比基线：11 PASS / 0 FAIL / 15 SKIP。
· SKIP → FAIL：那一环 7 天内真没跑起来，去看 ~/.trident/log/<作业名>.err.log
· 仍是 SKIP：仍无样本（清空后行情/新闻还没攒够）
· SKIP → PASS：链路已恢复
TIP
