#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# cline_fix_and_replay.sh — 门卫(gate)单点故障修复 + 全量回放
# 2026-11-03 · 由 Cline 生成
#
# 背景（已定位的根因，非猜测）：
#   .env 里 MINIMAX_GATE_KEY 与 MINIMAX_MAIN_KEY 都是"账户1"，而 purpose='gate'
#   池里**只有** minimax-gate 这一条通道（LOCAL_LLM_ENABLED=0 → 本地兜底也不存在）。
#   账户1 配额被打满 → 门卫 100% 不可用 → 每条新新闻落成 gate_unavailable=1
#   → ai_worker 按 v320 规则排除它们 → 队列整体卡死（722+ 条 PENDING）。
#   主分析是高频大 prompt 热路径，配额被它吃光后门卫连一次都发不出去。
#
# 本脚本做四件事：
#   1) 探测两个 MiniMax key 的真实可用性（决定门卫该用哪个 key）
#   2) 备份 DB
#   3) 全量回放（不限条数、不按时间过滤）—— 代码已改好 status 回写语义
#   4) 回放后自检（残留待回放数 / 新增可认领数）
#
# 用法：
#   bash cline_fix_and_replay.sh            # 探测 + 备份 + 回放 + 自检 + 重启
#   bash cline_fix_and_replay.sh --probe-only   # 只探测，不回放
#   bash cline_fix_and_replay.sh --no-restart   # 回放但不碰后端进程
#
# ⚠️ v321 修正：探测失败时**必须中止回放**（原版无脑继续，
#    门卫全挂时会把 700+ 条全部再刷一遍 429，白烧配额）。
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../../src_python" && pwd)"
DB="$(cd "$HERE/../.." && pwd)/trident_event_bus.db"
cd "$SRC" || { echo "[FATAL] 目录不存在: $SRC"; exit 1; }

PROBE_ONLY=0
DO_RESTART=1
for a in "$@"; do
  case "$a" in
    --probe-only) PROBE_ONLY=1 ;;
    --no-restart) DO_RESTART=0 ;;
  esac
done

echo "════════════════════════════════════════════════════════"
echo " 步骤 1/5  探测两个 MiniMax key 可用性"
echo "════════════════════════════════════════════════════════"
# 探测脚本退出码：0=门卫至少一条通道可用 / 1=全挂 / 2=脚本自身出错
python3 /tmp/cline_probe_keys.py
PROBE_RC=$?
echo

if [[ $PROBE_ONLY -eq 1 ]]; then
  echo "[--probe-only] 探测结束，未执行回放。"
  exit $PROBE_RC
fi

# v321: 门卫全挂时**中止回放** —— 否则 700+ 条全部再刷一遍 429，白烧配额且噪音日志。
if [[ $PROBE_RC -ne 0 ]]; then
  echo "════════════════════════════════════════════════════════"
  echo " [中止] 门卫通道全部不可用（探测失败）—— 不执行回放"
  echo "════════════════════════════════════════════════════════"
  echo "  原因：回放每条都要调门卫 LLM。门卫全挂时回放 = 把 700+ 条全部"
  echo "  再刷一遍 429，纯浪费配额。"
  echo "  请等配额恢复（或给门卫配一个可用 key）后重跑本脚本。"
  exit 1
fi
echo "  门卫通道可用，开始回放。"

echo "════════════════════════════════════════════════════════"
echo " 步骤 2/5  备份 DB"
echo "════════════════════════════════════════════════════════"
STAMP=$(date +%Y%m%d-%H%M%S)
cp "$DB" "${DB}.bak-gate-${STAMP}" && echo "  已备份 → ${DB}.bak-gate-${STAMP}"
echo

echo "════════════════════════════════════════════════════════"
echo " 步骤 3/5  全量回放（不限条数 / 不按时间过滤）"
echo "════════════════════════════════════════════════════════"
echo "  注意：每条一次 LLM 调用，串行执行，耗时较长。"
echo "  中断可 Ctrl-C；已回放的条目已提交，不会重来。"
echo
# replay_deferred_gate.py 是独立进程 → 它 import realtime_filter 时会重新读 .env，
# 所以**不需要重启后端**就能用上新的门卫 key（账户2）。
# 若账户2 此刻仍 429 → 门卫不可用 → 该条保持 gate_unavailable=1（不丢数据），
# 脚本会如实报出失败数，可稍后重跑。
python3 "$HERE/replay_deferred_gate.py" --commit 2>&1 | tee /tmp/cline_replay.log
echo

echo "════════════════════════════════════════════════════════"
echo " 步骤 4/5  回放后自检"
echo "════════════════════════════════════════════════════════"
sqlite3 "$DB" <<'SQL'
.mode column
.headers on
SELECT '--- 残留待回放（gate_unavailable=1）---' AS '';
SELECT COUNT(*) AS still_deferred FROM raw_news WHERE COALESCE(gate_unavailable,0)=1;
SELECT '--- 最近 3000 条的状态分布（回放后）---' AS '';
SELECT status, is_noise, COUNT(*) AS n FROM raw_news
 WHERE id > (SELECT MAX(id)-3000 FROM raw_news)
 GROUP BY status, is_noise ORDER BY n DESC;
SELECT '--- 门卫判例分布（回放后）---' AS '';
SELECT gate_verdict, COUNT(*) AS n FROM raw_news
 WHERE COALESCE(gate_unavailable,0)=0 AND gate_verdict IS NOT NULL AND gate_verdict<>''
 GROUP BY gate_verdict ORDER BY n DESC;
SQL
echo
echo "  解读："
echo "   · still_deferred 归 0 → 全部补判完成，门卫已恢复"
echo "   · PENDING / is_noise=0 → 待 ai_worker 分析（会持续减少）"
echo "   · PENDING / is_noise=1 → 门卫判排除（正常留存，不会被分析）"
echo "   · still_deferred > 0 → 门卫此刻不可用（配额/网络），属预期；数据未丢，"
echo "     下次配额恢复后重跑本脚本即可。"
echo
echo "════════════════════════════════════════════════════════"
echo " 步骤 5/5  重启后端（让新门卫 key 对【新来的新闻】生效）"
echo "════════════════════════════════════════════════════════"
echo "  回放用的是独立进程，不重启也能用新 key；"
echo "  但 8000/8100 常驻进程里 config.py 是 import 时读的 .env → 需重启。"
echo
if [[ $DO_RESTART -eq 1 ]]; then


# v321: 从 launchctl 实际输出里取真实 label 重启。
# 硬编码 'com.trident' 会报 "Could not find service ... in domain for user gui: 501"
# —— 因为真实 label 名不是 com.trident。launchctl list 第一列就是 label。
restart_backend_v2() {
  LABELS=$(launchctl list 2>/dev/null | awk '{print $1}' | grep -i trident)
  if [ -z "$LABELS" ]; then
    echo "  -> 未检测到 launchd 服务（launchctl list 里没有含 trident 的 label）。"
    echo "    请手动重启后端（按你平时用的方式），例如："
    echo "      cd $SRC && python3 api_server.py"
    return 1
  fi
  for L in $LABELS; do
    echo "  -> 尝试 kickstart: $L"
    if launchctl kickstart -k "gui/$(id -u)/$L" 2>&1; then
      echo "  [OK] 已重启 $L"
      RESTARTED=1
    else
      echo "     kickstart 失败（若是系统域服务，需 sudo launchctl kickstart -k system/$L）"
    fi
  done
  if [ -n "$RESTARTED" ]; then
    return 0
  fi
  echo "  -> 所有 kickstart 均失败，请手动重启后端。"
  return 1
}
RESTARTED=""


_disabled_restart_backend() {
  # 已废弃（v321）：硬编码 label 名不对，kickstart 必然失败。
  # 现行实现是上面的 restart_backend_v2 —— 从 launchctl 实际输出取真实 label。
  # 保留本段仅作历史对照，不再被调用。
  return 1
}
  # (旧的硬编码 label 实现已整段移除)
  # (旧的硬编码 label 实现已整段移除)
  # (旧的硬编码 label 实现已整段移除)
  # (旧的硬编码 label 实现已整段移除)

restart_backend_v2
echo "  重启后建议再确认一次（等 10~20 秒让进程起来）："
echo "    curl -s http://127.0.0.1:8000/api/health/models | python3 -m json.tool | head -60"
echo "  重点看 gate 通道 available、以及 pending_news 是否开始下降。"
echo "  通道列表里应同时出现 '(gate专用)' 与 '(gate备用)' —— 两条跨账户通道。"
echo
echo "  如果两条都在但 pending_news 不降，看引擎日志里 ai_worker 的认领计数。"
fi
echo
echo "  完成。若 still_deferred 仍 > 0，直接重跑本脚本即可（不丢数据）。"


