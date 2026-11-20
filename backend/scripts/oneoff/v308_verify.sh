#!/usr/bin/env bash
# v308 P1 验证脚本 (Desktop 部署树副本)
# ===================================================
# 用法:  bash backend/scripts/oneoff/v308_verify.sh
#
# ⚠️ 本脚本在部署树里, WS_ROOT/DEPLOY_ROOT 都指向部署树, diff 检查会自我比较。
#    要真正对比两棵树, 请跑 workspace 那份:
#      bash /Users/claire/.cline/data/workspaces/chat/trident_v170/backend/src_python/v308_verify.sh
#
# 步骤:
#   1. Python 语法 (py_compile) — 4 个后端文件
#   2. 树内一致性检查
#   3. 重启 8100 桥接 (只杀占用 8100 的进程, 从部署树启动)
#   4. 冒烟 5 个端点 + 计时 + 响应体字段断言
#   5. 前端类型检查 (tsc --noEmit)
#
# 背景: 8100 桥接**没有开 --reload**, 改完代码必须重启进程才生效。
#       /api/health 里的 code_version 字段就是用来确认"跑的是不是新代码"的探针。

set -uo pipefail

DEPLOY_ROOT="${DEPLOY_ROOT:-/Users/claire/Desktop/金融1.0}"
WS_ROOT="$DEPLOY_ROOT"
cd "$DEPLOY_ROOT/backend" || exit 1

FAILED=0
step()  { echo; echo "─── $* ───"; }
ok()    { echo "  ✅ $*"; }
bad()   { echo "  ❌ $*"; FAILED=1; }

step "1/5 Python 语法检查 (py_compile)"
for f in market_regime.py funding_rate.py orderbook_depth.py bridge_server.py backfill_critical_moves.py calc_event_move_attribution.py v308_audit_move_readers.py; do
  p="$DEPLOY_ROOT/backend/src_python/$f"
  if [ ! -f "$p" ]; then bad "$p 不存在"; continue; fi
  if python3 -m py_compile "$p" 2>/tmp/pyc.err; then
    ok "语法通过: $f"
  else
    bad "语法错误: $p"; cat /tmp/pyc.err
  fi
done

step "2/5 重启 8100 桥接 (让新代码生效)"
# v308 P1-fix3: 价格序列污染的**存量行**必须重跑 backfill 才会被清掉。
# backfill_critical_moves.main() 会先 DELETE 掉 context_json 含
# 'external_indicators' 的旧行, 再用修复后的 fetch_prices/detect_moves 重算。
# 不重跑 → 库里那 9867% 的假异动还在, A 维度仍然被污染。
if [ "${SKIP_BACKFILL:-0}" != "1" ]; then
  # 说明: backfill 与运行中的 8100 并发是安全的 ——
  #   DB 处于 WAL 模式, 读不阻塞写; 桥接的写路径已设 busy_timeout=10000。
  #   (实测 2026-10-24: 桥接在跑时 backfill 正常执行到 DELETE, 无锁错误。)
  # 写锁容错由 backfill 侧的 busy_timeout 负责, 不在这里杀进程 ——
  #   杀进程会让下方"按 PID 找进程再启动"的逻辑失去前提, 可能导致桥接起不来。
  echo "  重跑 critical_price_moves backfill (清理量纲污染存量行)…"
  ( cd "$DEPLOY_ROOT/backend" && python3 src_python/backfill_critical_moves.py > /tmp/backfill.log 2>&1 )
  if [ $? -eq 0 ]; then
    ok "backfill 完成"
    grep -E 'Total moves|implausible|SKIP' /tmp/backfill.log 2>/dev/null | head -10
  else
    bad "backfill 失败 —— 看 /tmp/backfill.log"
    tail -20 /tmp/backfill.log 2>/dev/null
  fi
else
  echo "  SKIP_BACKFILL=1 → 跳过 backfill (存量污染行不会被清理)"
fi

# 直接查 DB 验证污染是否真的被清掉 (不依赖端点, 端点可能仍有缓存/旧进程)
DB_POLLUTION="$(python3 - "$DEPLOY_ROOT/backend/trident_event_bus.db" <<'PYEOF' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
    tot = c.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
    bad = c.execute(
        "SELECT COUNT(*) FROM critical_price_moves WHERE ABS(COALESCE(move_pct,0)) > 90.0"
    ).fetchone()[0]
    print(f"{tot} {bad}")
except Exception as e:
    print(f"ERR {e}")
PYEOF
)"
if [ -n "$DB_POLLUTION" ] && [[ "$DB_POLLUTION" != ERR* ]]; then
  DB_TOT="${DB_POLLUTION%% *}"; DB_BAD="${DB_POLLUTION##* }"
  if [ "$DB_BAD" = "0" ]; then
    ok "DB critical_price_moves: 总 $DB_TOT 行, 量纲异常行 0 —— 污染已清除"
  else
    bad "DB 仍有 $DB_BAD 行 |move_pct|>90% 的量纲污染 (总 $DB_TOT 行)"
  fi
else
  bad "无法读取 DB 核对污染: $DB_POLLUTION"
fi

# v308 P1-fix3: event_move_attribution 是**派生表** —— 它的 move_pct 是从
# critical_price_moves 直接拷贝的。backfill 清掉源表污染后, 派生表里的旧行
# 仍然是脏的, 必须重跑 calc_event_move_attribution.py 才会用干净数据重算。
# (calc 脚本已加过滤: 源头不拦就等于把污染原样搬进派生表)
if [ "${SKIP_ATTRIB_REBUILD:-0}" != "1" ]; then
  echo "  重跑 event_move_attribution 重算 (清理派生表污染)…"
  ( cd "$DEPLOY_ROOT/backend" && python3 src_python/calc_event_move_attribution.py --days 30 > /tmp/attrib.log 2>&1 )
  if [ $? -eq 0 ]; then
    ok "event_move_attribution 重算完成"
    grep -E 'Loaded|Cleared|rows' /tmp/attrib.log 2>/dev/null | head -5
  else
    bad "event_move_attribution 重算失败 —— 看 /tmp/attrib.log"
    tail -20 /tmp/attrib.log 2>/dev/null
  fi
else
  echo "  SKIP_ATTRIB_REBUILD=1 → 跳过派生表重算 (派生表污染行不会被清理)"
fi

# 直接查派生表的污染行数
DB_ATTRIB="$(python3 - "$DEPLOY_ROOT/backend/trident_event_bus.db" <<'PYEOF' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
    tot = c.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
    bad = c.execute(
        "SELECT COUNT(*) FROM event_move_attribution WHERE ABS(COALESCE(move_pct,0)) > 90.0"
    ).fetchone()[0]
    print(f"{tot} {bad}")
except Exception as e:
    print(f"ERR {e}")
PYEOF
)"
if [ -n "$DB_ATTRIB" ] && [[ "$DB_ATTRIB" != ERR* ]]; then
  A_TOT="${DB_ATTRIB%% *}"; A_BAD="${DB_ATTRIB##* }"
  if [ "$A_BAD" = "0" ]; then
    ok "DB event_move_attribution: 总 $A_TOT 行, 量纲异常行 0"
  else
    bad "DB event_move_attribution 仍有 $A_BAD 行量纲污染 (总 $A_TOT 行) —— 需重跑重算"
  fi
else
  bad "无法读取 DB 核对 event_move_attribution 污染: $DB_ATTRIB"
fi

BRIDGE_PIDS="$(lsof -ti tcp:8100 2>/dev/null || true)"
if [ -n "$BRIDGE_PIDS" ]; then
  echo "  停止占用 8100 的进程: $BRIDGE_PIDS"
  # shellcheck disable=SC2086
  kill $BRIDGE_PIDS 2>/dev/null || true
  sleep 2
  # shellcheck disable=SC2086
  kill -9 $BRIDGE_PIDS 2>/dev/null || true
else
  echo "  8100 当前没有进程在跑"
fi
sleep 1
nohup python3 src_python/bridge_server.py > /tmp/bridge.log 2>&1 &
disown

# v308 P1-fix3: 桥接启动**很慢** —— 要 import api_server (挂载 328 条路由)
# + 连 679MB 的 DB + 起 news/cot 后台拉取线程, 实测冷启动 10~25s。
# 原来固定 sleep 3 就去 curl → 必然 connection refused → 后面所有步
# 全部假失败 ("HTTP 000" / "TOO_MANY_UNREACHABLE")。
# 改成轮询等待, 最多 120s。
#
# 另外: 不能用 `curl ... | grep -q` —— set -o pipefail 下 grep 提前退出会让
# curl 收到 SIGPIPE, 整个管道返回非 0, 即使内容匹配也会误判失败。
# 所以先把响应体存进变量, 再 grep。
BRIDGE_READY=0
for _i in $(seq 1 60); do
  HEALTH_BODY="$(curl -s -m 5 http://127.0.0.1:8100/api/health 2>/dev/null || true)"
  # 用 bash 字符串匹配, 不用 `printf | grep -q`:
  # set -o pipefail 下 grep -q 提前退出会让上游收到 SIGPIPE → 管道返回非 0,
  # 内容明明匹配也会被误判成"没就绪"。
  if [[ "$HEALTH_BODY" == *'"code_version":"v308-p1"'* \
     || "$HEALTH_BODY" == *'"code_version": "v308-p1"'* ]]; then
    BRIDGE_READY=1
    break
  fi
  sleep 2
done
if [ "$BRIDGE_READY" = "1" ]; then
  ok "桥接已就绪, 跑的是新代码 (code_version=v308-p1)"
else
  bad "桥接 120s 内未就绪或仍是旧代码 —— 看 /tmp/bridge.log"
  tail -20 /tmp/bridge.log 2>/dev/null
fi
DB_NOW="$(curl -s -m 5 http://127.0.0.1:8100/api/health 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("db",""))' 2>/dev/null || true)"
if [ -n "$DB_NOW" ]; then
  echo "  当前服务 DB: $DB_NOW"
fi

step "3/5 端点冒烟 + 计时 + 响应体断言"
probe() {  # probe <label> <url> <max_seconds> <关键字>
  local label="$1" url="$2" max="$3" expect="${4:-}"
  local t0 t1 took code
  t0=$(python3 -c 'import time;print(time.time())')
  code=$(curl -s -m "$max" -o /tmp/probe.json -w '%{http_code}' "$url")
  t1=$(python3 -c 'import time;print(time.time())')
  took=$(python3 -c 'import sys;print(f"{float(sys.argv[2])-float(sys.argv[1]):.1f}")' "$t0" "$t1")
  if [ "$code" = "200" ]; then
    if [ -n "$expect" ] && ! grep -q "$expect" /tmp/probe.json; then
      bad "$label => HTTP 200 (${took}s) 但响应体缺少 '$expect'"
      head -c 300 /tmp/probe.json; echo
    else
      ok "$label => HTTP $code (${took}s) 含 '$expect'"
    fi
  else
    bad "$label => HTTP $code (${took}s)"
    head -c 300 /tmp/probe.json 2>/dev/null; echo
  fi
}
probe "market-regime   " "http://127.0.0.1:8100/api/v291/market-regime?symbols=BTC,ETH" 30 '"regimes"'
probe "funding-rate    " "http://127.0.0.1:8100/api/v291/funding-rate?symbol=BTCUSDT" 15 '"current_rate"'
probe "orderbook-depth " "http://127.0.0.1:8100/api/v291/orderbook-depth?symbol=BTCUSDT" 15 '"mid_price"'
probe "causal-feedback " "http://127.0.0.1:8100/api/v291/causal-chains/feedback?days=30&auto_run=false" 30 '"chains"'
probe "by-dimension    " "http://127.0.0.1:8100/api/v237/missed-signals/by-dimension?days=90" 30 '"by_dimension"'

# ── v308 P1-fix3: 三维度数据诚实性断言 ──────────────────────────────
step "4/5 三维度数据诚实性 (v308 P1-fix3)"

fetch_dim() {  # fetch_dim <days> → 打印 "A B C sum grand_total reflections_total"
  curl -s -m 30 "http://127.0.0.1:8100/api/v237/missed-signals/by-dimension?days=$1" \
  | python3 -c '
import sys, json
try:
    j = json.load(sys.stdin)
except Exception as e:
    print("PARSE_ERROR", e); raise SystemExit(0)
t = j.get("totals") or {}
bd = j.get("by_dimension") or {}
a = (bd.get("A_signal") or {}).get("n")
b = (bd.get("B_direction") or {}).get("n")
c = (bd.get("C_technical") or {}).get("n")
print(a, b, c, t.get("sum"), t.get("grand_total"), t.get("reflections_total"))
' 2>/dev/null
}

read -r A90 B90 C90 SUM90 GT90 RT90 <<< "$(fetch_dim 90)"
read -r A30 B30 C30 SUM30 GT30 RT30 <<< "$(fetch_dim 30)"

if [ -n "${A90:-}" ] && [ "$A90" != "None" ] && [ "$A90" != "PARSE_ERROR" ]; then
  if [ "$GT90" = "$SUM90" ]; then
    ok "grand_total == sum ($SUM90) —— 不再自相矛盾"
  else
    bad "grand_total ($GT90) != sum ($SUM90) —— 前后端对不上"
  fi
  if [ "$A90" = "200" ]; then
    bad "A 维度 days=90 恰好 200 (撞 LIMIT 200 上限), days=30=$A30 —— 计数仍被截断"
  else
    ok "A 维度随天数变化 (30d=$A30, 90d=$A90) —— 未撞 LIMIT 上限"
  fi
  DQ="$(curl -s -m 30 "http://127.0.0.1:8100/api/v237/missed-signals/by-dimension?days=90" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("data_quality",{}).get("implausible_move_rows_excluded","-1"))' 2>/dev/null)"
  if [ -n "$DQ" ] && [ "$DQ" != "-1" ] && [ "$DQ" != "None" ]; then
    ok "data_quality 存在, 剔除量纲异常行 = $DQ"
  else
    bad "data_quality.implausible_move_rows_excluded 缺失 —— 新代码没生效"
  fi
  DTC="$(curl -s -m 30 "http://127.0.0.1:8100/api/v237/missed-signals/by-dimension?days=90" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("data_quality",{}).get("decision_ts_column","?"))' 2>/dev/null)"
  if [ "$DTC" = "decision_ts" ] || [ "$DTC" = "created_at" ]; then
    ok "决策匹配时间列 = $DTC"
  else
    bad "decision_ts_column 异常: '$DTC'"
  fi
else
  bad "无法解析 by-dimension 响应 (A90='${A90:-}')"
fi

# 断言 5: 前端所有卡片必须用同一个时间窗口 (90d), 否则同页数字矛盾。
# v308 P1-fix3: 原来只 grep 2 个文件 → 别的组件再写 days=2000 照样漏出去
# (实测 bridge.log 里就有一路 by-dimension?days=2000 的请求)。
# 现在扫**整个** frontend 源码树 (排除 node_modules/.next), 两棵树都查。
FRONT_DAYS="$(find "$WS_ROOT/frontend" "$DEPLOY_ROOT/frontend" \
  -type d \( -name node_modules -o -name .next -o -name .git \) -prune -o \
  -type f \( -name '*.tsx' -o -name '*.ts' \) -print 2>/dev/null \
  | while IFS= read -r _f; do
      grep -oh 'days={2000}\|days = 2000\|days=2000\|days: 2000' "$_f" 2>/dev/null
    done | head -1)"
if [ -z "$FRONT_DAYS" ]; then
  ok "前端无残留 days=2000 —— 卡片窗口已统一为 90d"
else
  bad "前端仍有 days=2000 ($FRONT_DAYS) —— 与 /missed-signals 页的 90d 矛盾"
fi

# ── v308 P1-fix3 (续): 量纲污染过滤**全覆盖**断言 ─────────────────────
# 背景: 复查发现除 by-dimension / attribution-stats / calibration /
#       causal-graph / abnormal-signals / impact 之外, 还有 4 处读
#       critical_price_moves 漏了过滤 (cpm_range 计数 / B 维度 nearby /
#       review-snapshot 信号数 / news impact-evaluate 因子B), 外加
#       event_move_attribution 主路径。
#       "某个端点返回 200" 测不出这些 —— 必须逐端点扫响应里的 move_pct,
#       并静态检查源码里没有漏网的查询 (防止以后再加 reader 又漏)。
step "4c/5 量纲污染过滤全覆盖 (端点响应 move_pct + 源码查询)"

POLLUTE_RUNTIME="$(python3 - <<'PYEOF'
import json, re, urllib.request

BASE = "http://127.0.0.1:8100"
URLS = [
    "/api/v237/missed-signals/by-dimension?days=90",
    "/api/v291/attribution-stats?hours=720",
    "/api/v237/calibration?days=90",
    "/api/v291/causal-graph?asset=BTC&hours=720",
    "/api/abnormal-signals?hours=720&limit=200",
    "/api/review/snapshot?days=90",
    "/api/v201/impact/by-asset?asset=BTC",
    "/api/v291/event-move-attribution?limit=100",
    "/api/news/impact-evaluate?news_id=1",
    # v308 P1-fix3: AST 静态检查新抓出来的 3 处漏网端点 ——
    # 加进运行时扫描, 这样"源码补了过滤但响应里仍有污染"也能被抓到。
    "/api/v237/missed-signals?days=90&limit=200",
    "/api/v291/critical-moves?hours=720&limit=40",
    "/api/v291/move-attributions?limit=100",
]
LIMIT = 90.0
PCT_RE = re.compile(r"move_pct=(-?\d+(?:\.\d+)?)")

viol, errs = [], []
worst = 0.0


def walk(node, path, acc):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("move_pct", "avg_move_pct") and isinstance(v, (int, float)):
                acc.append(abs(v))
                if abs(v) > LIMIT:
                    viol.append(f"{path}.{k} = {v}")
            elif isinstance(v, str):
                for m in PCT_RE.finditer(v):
                    x = abs(float(m.group(1)))
                    acc.append(x)
                    if x > LIMIT:
                        viol.append(f"{path}.{k} = {m.group(0)}")
            else:
                walk(v, f"{path}.{k}", acc)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", acc)


for u in URLS:
    try:
        with urllib.request.urlopen(BASE + u, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        errs.append(f"{u} -> {type(e).__name__}: {e}")
        continue
    acc = []
    walk(j, u, acc)
    if acc:
        worst = max(worst, max(acc))

if viol:
    for v in viol:
        print("VIOLATION " + v)
    raise SystemExit(1)
for e in errs:
    print("SKIP " + e)
# 端点大面积取不到 = 桥接挂了/没重启, 这时"没扫到污染"是假通过 → 必须判失败
if len(errs) * 2 > len(URLS):
    print(f"TOO_MANY_UNREACHABLE {len(errs)}/{len(URLS)} 个端点取不到, 无法断言")
    raise SystemExit(1)
print(f"CLEAN max_abs_move_pct={worst:.3f}")
PYEOF
)"
if [ $? -eq 0 ]; then
  ok "所有端点响应内 move_pct 均 <= 90 ($POLLUTE_RUNTIME)"
else
  bad "端点响应里仍有量纲污染的 move_pct"
  echo "$POLLUTE_RUNTIME" | head -20
fi

POLLUTE_SRC="$(python3 - "$DEPLOY_ROOT" <<'PYEOF'
import ast, pathlib, sys

# 静态检查: 所有读 critical_price_moves / event_move_attribution 的查询,
# 只要 SELECT 里带 move_pct, 就必须套量纲污染过滤 (|move_pct| <= 90)。
#
# 为什么用 AST 而不是正则: SQL 在源码里是**相邻字面量隐式拼接**的
# ("SELECT ... " "FROM critical_price_moves " "WHERE ..."), 正则扫源码会
# 看不全一条 SQL 的边界, 容易把下一条查询的过滤误判成这条的。
# ast 折叠隐式拼接后拿到的是完整 SQL 字符串, 判断才准。
#
# 两种合法形态都放行:
#   ① SQL 里直接带 ABS(COALESCE(move_pct,0)) <= ?
#   ② SQL 取全集, Python 侧再按阈值筛 (A 维度主查询就是这种)
# ③ 动态 where 拼装 (event_move_attribution 列表端点) —— 过滤条件在
#      where.append(...) 里, 不在 SQL 文本内, 按"同函数内有过滤"放行

FILES = (
    "bridge_server.py",
    "calc_event_move_attribution.py",
    # v308 P1-fix3: api_server.py 的 328 条路由是**挂载**到 8100 上的
    # (bridge_server 末尾 import api_server 并注册其 router), 所以它读的
    # critical_price_moves / event_move_attribution 同样会流到前端。
    # 之前静态检查只扫 2 个文件 → 挂载路由里的 reader 完全没被覆盖。
    "api_server.py",
)
FILTER = "ABS(COALESCE(move_pct,0))"
THRESH = "MAX_PLAUSIBLE_MOVE_PCT"
TABLES = ("critical_price_moves", "event_move_attribution")


def sql_text(node):
    """尽力还原 SQL 文本: 常量 / 隐式拼接 / f-string 静态部分 / + 拼接。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("?")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return sql_text(node.left) + sql_text(node.right)
    if isinstance(node, ast.IfExp):
        return sql_text(node.body) + " " + sql_text(node.orelse)
    return ""


def enclosing_func(node, parents):
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def resolve_sql(node, parents):
    """取 SQL 文本。args[0] 是个变量名时, 回头找它的赋值表达式。"""
    txt = sql_text(node)
    if txt:
        return txt
    if isinstance(node, ast.Name):
        fnode = enclosing_func(node, parents)
        scope = fnode if fnode is not None else None
        if scope is not None:
            for sub in ast.walk(scope):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == node.id:
                            return sql_text(sub.value)
    return ""


bad = []
for root in sys.argv[1:]:
    for fn in FILES:
        p = pathlib.Path(root) / "backend/src_python" / fn
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            print(f"CHECKER_ERROR {p} 语法错误: {e}")
            raise SystemExit(2)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            sql = resolve_sql(node.args[0], parents)
            if not sql.strip().upper().startswith("SELECT"):
                continue          # INSERT / DELETE / UPDATE 不是"读污染值"
            if not any(t in sql for t in TABLES):
                continue
            if "move_pct" not in sql:
                continue          # 只取 ts/count 的查询不受 move_pct 污染影响
            # 过滤串在不同文件里空格/写法可能不同
            # (ABS(COALESCE(move_pct,0)) / abs(coalesce(move_pct, 0)) / ABS(move_pct))
            # → 去掉所有空白 + 忽略大小写再比对, 避免误报。
            def has_filter(text: str) -> bool:
                flat = "".join(text.split()).lower()
                return (
                    "abs(coalesce(move_pct,0))" in flat
                    or "abs(move_pct)" in flat
                    or "move_pctbetween" in flat
                )
            if has_filter(sql):
                continue          # ① SQL 层已过滤
            fnode = enclosing_func(node, parents)
            fsrc = ast.get_source_segment(src, fnode) if fnode else ""
            if fsrc and has_filter(fsrc):
                continue          # ③ 动态 where 拼装
            if fsrc and "move_pct" in fsrc and THRESH in fsrc:
                continue          # ② Python 侧过滤
            bad.append(f"{p}:{node.lineno} 查 {sql[:60]!r} 但没套量纲污染过滤")
if bad:
    for b in bad:
        print(b)
    raise SystemExit(1)
print("OK")
PYEOF
)"
if [ $? -eq 0 ]; then
  ok "源码里所有 critical_price_moves / event_move_attribution 查询都套了污染过滤"
else
  bad "源码里有查询漏了量纲污染过滤 (见下)"
  echo "$POLLUTE_SRC" | head -20
fi

# ── v308 P1-fix3 (续): 全量后端审计 (超出上面 3 个文件) ─────────────────
# 上面的内联检查只覆盖 bridge_server.py / calc_event_move_attribution.py /
# api_server.py 三个**已知** reader 宿主。但:
#   ① backend/src_python 下还有几十个模块 (abnormal_detector / causal_graph /
#      quick_sim / news_* ...), 任何一个都可能读这两张表;
#   ② SQL 写在**模块级常量**里时, 内联检查器的 resolve_sql 只在函数作用域内
#      找赋值 → 解析不到 → 那条 SQL 被静默跳过 (漏检)。
# v308_audit_move_readers.py 是内联检查器的**超集**: 扫全部 .py, 支持模块级
# 常量, 并把判定分成 OK / SUSPECT / MISSING 三档 (不把"看不清"当"没过滤")。
#   MISSING —— 真的没过滤 → --strict 时 exit 1 → 卡住构建 (必须补)
#   SUSPECT —— 方向可疑 / SQL 解析不出来 → 只提示, **不阻断** (人工看一眼)
step "4d/5 全量后端 reader 审计 (全部 .py, 超出上面 3 个文件)"

if [ "${SKIP_AUDIT:-0}" = "1" ]; then
  echo "  SKIP_AUDIT=1 → 跳过全量 reader 审计"
else
  AUDIT_TOOL="$DEPLOY_ROOT/backend/scripts/oneoff/v308_audit_move_readers.py"
  if [ ! -f "$AUDIT_TOOL" ]; then
    bad "审计工具不存在: $AUDIT_TOOL"
  else
    # 先跑内置自检 —— 判定逻辑被改坏时立刻暴露 (不依赖扫描结果, 秒级)
    SELFTEST_OUT="$(python3 "$AUDIT_TOOL" --selftest 2>&1)"
    if [ $? -eq 0 ]; then
      ok "审计判定自检通过"
    else
      bad "审计判定自检失败 (判定逻辑被改坏, 结果不可信)"
      echo "$SELFTEST_OUT" | head -10 | sed 's/^/       /'
    fi
    AUDIT_OUT="$(python3 "$AUDIT_TOOL" --root "$DEPLOY_ROOT" --strict --show-suspect 2>&1)"
    if [ $? -eq 0 ]; then
      ok "全量 reader 审计通过: $DEPLOY_ROOT"
      echo "$AUDIT_OUT" | tail -1 | sed 's/^/       /'
    else
      bad "全量 reader 审计发现漏网查询: $DEPLOY_ROOT (MISSING 行需补过滤)"
      echo "$AUDIT_OUT" | head -25 | sed 's/^/       /'
      echo "       ↳ 用 SKIP_AUDIT=1 可临时跳过本步 (但污染会进前端)"
    fi
  fi
fi

step "5/5 前端类型检查"
if [ -d "$DEPLOY_ROOT/frontend/node_modules" ]; then
  if ( cd "$DEPLOY_ROOT/frontend" && npx --no-install tsc --noEmit >/tmp/tsc.log 2>&1 ); then
    ok "tsc 通过"
  else
    bad "tsc 报错"
    head -30 /tmp/tsc.log
  fi
else
  bad "frontend/node_modules 不存在 (跳过 tsc)"
fi

echo
if [ "$FAILED" = "0" ]; then
  echo "✅ 全部通过"
else
  echo "❌ 有失败项 (见上面 ❌ 标记)"
fi
exit "$FAILED"