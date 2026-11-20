#!/usr/bin/env python3
"""system_health_report.py — 每日系统体检（只读，不重启任何进程）

补的是"可观测输出"这一层缺口：现在 heartbeat / DLQ 都写进库了，但没人每天读。
本脚本把它们 + 库增长 + 决策活跃度 + 端点基线 + 端口存活，合成一份可交付的日报。

判定规则（写死在代码里，避免每次口径漂移）：
  * 决策活跃度：ai_decisions 最近 6h 为 0 → FAIL；< 5 条 → WARN
  * LLM 通道：近 1h 调用里 error 占比 ≥ 80% → FAIL；≥ 30% → WARN
  * 心跳：超过 90s 未 tick 的 worker → STALE；last_status=error → FAIL
  * DLQ：未 resolved 且 24h 内新增 → WARN；累计 > 5 → FAIL
  * 库增长：按最近 7 天均值推算 30 天增量，> 当前体积 30% → WARN
  * 端口：8100 / 3030 任一不可连 → FAIL；8000 api_server 已退役（无守护），只登记不判 FAIL

用法：
  python3 backend/scripts/system_health_report.py
  python3 backend/scripts/system_health_report.py --out backend/reports/health
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(REPO_ROOT, "backend", "trident_event_bus.db")
BASELINE = os.path.join(REPO_ROOT, "backend", "reports", "baseline")
PORTS = [  # (port, service, required) —— required=False 表示已退役：只登记，不判 FAIL
    (8000, "api_server", False),  # v483: 无 launchd 守护（ensure_core 只管 bridge/engine/frontend），已退役
    (8100, "bridge", True),
    (3030, "frontend", True),     # 3000 已废弃，前端固定在 3030
]

RETENTION_TARGETS = ("source_status_history", "market_ticks", "macro_snapshots", "ai_call_stats")


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# v489：体检脚本原来把"查询失败"静默成 None → 上层再算成 0/正常，
# 于是库缺表、列改名、锁超时全都会被伪装成"0 条决策"这种好看的数字。
# 现在失败必须留痕：_SAFE_ERRORS 汇总后写进报告，别让体检报告撒谎。
_SAFE_ERRORS: List[str] = []


def _safe(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        brief = " ".join(sql.split())[:70]
        _SAFE_ERRORS.append(f"{type(exc).__name__}: {exc} ← {brief}")
        return None


def _now() -> datetime:
    return datetime.now()


def check_ports() -> List[Dict[str, Any]]:
    out = []
    for port, name, required in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            ok = s.connect_ex(("127.0.0.1", port)) == 0
        finally:
            s.close()
        out.append({"port": port, "service": name, "reachable": ok, "required": required})
    return out


def check_decisions(conn: sqlite3.Connection) -> Dict[str, Any]:
    total = _safe(conn, "SELECT COUNT(*) FROM ai_decisions")
    total = total[0] if total else 0
    last = _safe(conn, "SELECT MAX(created_at) FROM ai_decisions")
    last = last[0] if last else None
    # v453: created_at 是**混格式**——老行是带时区的 ISO（...+08:00），
    # 新行是 SQLite datetime('now') 的 UTC 裸时间。用本地时间字符串去比，
    # 会把刚写的决策判成"8 小时前"，于是「决策活跃度」长期假 FAIL。
    # 改用 julianday() 让 SQLite 自己解析两种格式，再和 UTC 现在比。
    c6 = _safe(conn, "SELECT COUNT(*) FROM ai_decisions "
                     "WHERE julianday(created_at) >= julianday('now','-6 hours')")
    c24 = _safe(conn, "SELECT COUNT(*) FROM ai_decisions "
                      "WHERE julianday(created_at) >= julianday('now','-1 day')")
    c6 = c6[0] if c6 else 0
    c24 = c24[0] if c24 else 0
    if c6 == 0:
        level = "FAIL"
    elif c6 < 5:
        level = "WARN"
    else:
        level = "OK"
    return {"total_rows": total, "last_decision": last, "last_6h": c6, "last_24h": c24, "level": level}


def check_llm(conn: sqlite3.Connection) -> Dict[str, Any]:
    since = int((_now() - timedelta(hours=1)).timestamp())
    rows = _safe(conn, """
        SELECT status, COUNT(*) FROM ai_call_stats WHERE ts >= ? GROUP BY status
    """, (since,))
    # GROUP BY 返回多行，上面的 _safe 只取第一行 → 单独查询
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM ai_call_stats WHERE ts >= ? GROUP BY status", (since,)
        ).fetchall()
    except sqlite3.Error:
        rows = []
    total = sum(n for _, n in rows)
    errors = sum(n for s, n in rows if s and s != "ok")
    ratio = (errors / total) if total else 0.0
    recent = []
    try:
        recent = conn.execute("""
            SELECT purpose, model_id, status, error_code, latency_ms
            FROM ai_call_stats ORDER BY id DESC LIMIT 6
        """).fetchall()
    except sqlite3.Error:
        pass
    level = "OK"
    if total and ratio >= 0.8:
        level = "FAIL"
    elif total and ratio >= 0.3:
        level = "WARN"
    elif total == 0:
        level = "WARN"   # 一小时没有任何调用也是异常（要么没新闻，要么链路断）
    return {"calls_1h": total, "error_ratio": round(ratio, 3), "by_status": dict(rows),
            "recent": recent, "level": level}


def check_heartbeat(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = []
    try:
        rows = conn.execute(
            "SELECT worker_name, last_tick_at, last_status, last_error FROM worker_heartbeat"
        ).fetchall()
    except sqlite3.Error:
        pass
    items = []
    for name, tick, status, err in rows:
        stale = True
        age_sec = None
        if tick:
            try:
                age_sec = (_now() - datetime.strptime(str(tick)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
                stale = age_sec > 90
            except ValueError:
                pass
        items.append({"worker": name, "last_tick": tick, "age_sec": age_sec,
                      "stale": stale, "status": status, "error": (err or "")[:80]})
    level = "OK"
    if any(i["status"] == "error" for i in items):
        level = "FAIL"
    elif any(i["stale"] for i in items):
        level = "WARN"
    if not items:
        level = "WARN"
    return {"workers": items, "level": level}


def check_dlq(conn: sqlite3.Connection, days: int = 7) -> Dict[str, Any]:
    since = (_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM dlq_events WHERE COALESCE(resolved,0)=0"
        ).fetchone()[0]
        recent = conn.execute("""
            SELECT id, kind, source, created_at, substr(COALESCE(payload,''),1,120)
            FROM dlq_events WHERE created_at >= ? AND COALESCE(resolved,0)=0
            ORDER BY id DESC LIMIT 10
        """, (since,)).fetchall()
    except sqlite3.Error:
        return {"unresolved": None, "recent": [], "level": "WARN"}
    level = "OK"
    if unresolved > 5:
        level = "FAIL"
    elif recent:
        level = "WARN"
    return {"unresolved": unresolved, "recent_unresolved": len(recent),
            "items": recent, "level": level}


def check_db(conn: sqlite3.Connection, path: str) -> Dict[str, Any]:
    size_mb = os.path.getsize(path) / 1048576 if os.path.exists(path) else 0
    tables = []
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        pass
    big = []
    daily = {}
    for (t,) in tables:
        if t.startswith("sqlite_"):
            continue
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            continue
        if n < 10000:
            continue
        big.append({"table": t, "rows": n})
        # 近 7 天增量（ts 为 int 或 text 的表才可算）
        try:
            typeof = conn.execute(f'SELECT typeof(ts) FROM "{t}" LIMIT 1').fetchone()
            if not typeof:
                continue
            if typeof[0] == "integer":
                cutoff = int((_now() - timedelta(days=7)).timestamp())
            else:
                cutoff = (_now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            c7 = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE ts >= ?', (cutoff,)).fetchone()[0]
            daily[t] = round(c7 / 7, 1)
        except sqlite3.Error:
            continue
    big.sort(key=lambda r: -r["rows"])
    # 增量推算不用"每行多少字节"的拍脑袋常数：直接按「近 7 天新增行 / 现有行」×「现有体积」
    # 求比例。这样口径与库实际体积自洽（本机 sqlite 未编译 dbstat，无法逐表精确）。
    total_rows = sum(r["rows"] for r in big) or 1
    daily_rows = sum(v for v in daily.values() if v)
    projected_30d_mb = round(size_mb * (daily_rows * 30) / total_rows, 1)
    level = "OK"
    if size_mb > 0 and projected_30d_mb > size_mb * 0.3:
        level = "WARN"
    return {"size_mb": round(size_mb, 1), "top_tables": big[:10], "rows_per_day_7d": daily,
            "projected_30d_growth_mb": projected_30d_mb, "level": level}


def check_klines(conn: sqlite3.Connection) -> Dict[str, Any]:
    """v453: 行情 K 线新鲜度。

    为什么单开一项：11-18 实测 WTI/XAG 的 1m 停在 32 小时前、BTC 5m 停在 23 小时前，
    而系统照常出信号 —— 也就是说「用旧 K 线算出来的价格」被当成了现价。
    这一项只查「最近 7 天还在写」的序列（早已停用的 3m/10m 历史序列不算故障），
    再按时框给容忍度：1m 15 分钟 / 5m 25 分钟 / 15m 45 分钟 / 1h 2 小时 /
    4h 8 小时 / 1d 4 天（含周末与假日）。
    """
    limits = {"1m": 15, "3m": 45, "5m": 25, "10m": 50, "15m": 45, "30m": 90,
              "1h": 120, "4h": 480, "1d": 5760}
    now_ms = int(_now().timestamp() * 1000)
    cutoff_ms = now_ms - 7 * 86400 * 1000
    rows = []
    try:
        rows = conn.execute(
            "SELECT symbol, timeframe, MAX(ts) AS last_ts, COUNT(*) FROM kline_bars "
            "WHERE ts >= ? GROUP BY symbol, timeframe", (cutoff_ms,)
        ).fetchall()
    except sqlite3.Error:
        return {"level": "SKIP", "reason": "kline_bars 不可读", "stale": [], "checked": 0}
    stale = []
    for symbol, tf, last_ts, n in rows:
        lim = limits.get(tf)
        if not lim or not last_ts:
            continue
        lag_min = (now_ms - last_ts) / 60000.0
        if lag_min > lim:
            stale.append({
                "symbol": symbol, "timeframe": tf, "lag_hours": round(lag_min / 60, 1),
                "limit_min": lim, "rows": n,
                "last_bar": datetime.fromtimestamp(last_ts / 1000).strftime("%Y-%m-%d %H:%M"),
            })
    stale.sort(key=lambda r: -r["lag_hours"])
    level = "WARN" if stale else "OK"
    return {"level": level, "checked": len(rows), "stale": stale[:12], "stale_count": len(stale)}


def check_silent_drops() -> Dict[str, Any]:
    """v483: 读丢写账本 backend/runtime/silent_drops.jsonl（由 silent_ledger 写）。

    为什么单独一项：全仓 619 处「except 后直接 pass」，静态扫描不出的真实损失
    全在这里。账本是节流的（每 key 每分钟一行，行内带窗口累计 dropped），
    所以这里算的是各 key 的**累计 total** 与窗口 dropped 之和，不是行数。
    """
    p = os.path.join(REPO_ROOT, "backend", "runtime", "silent_drops.jsonl")
    if not os.path.exists(p):
        return {"level": "OK", "keys": {}, "total": 0, "path": p, "days": 0}
    today = _now().strftime("%Y-%m-%d")
    total = 0
    by_key: Dict[str, int] = {}
    last_reason: Dict[str, str] = {}
    days = set()
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = str(row.get("ts") or "")
                days.add(ts[:10])
                if ts[:10] != today:
                    continue
                k = str(row.get("key") or "?")
                by_key[k] = by_key.get(k, 0) + int(row.get("dropped") or 0)
                total += int(row.get("dropped") or 0)
                last_reason[k] = str(row.get("reason") or "")[:120]
    except OSError:
        return {"level": "SKIP", "reason": "账本不可读", "keys": {}, "total": 0, "path": p, "days": 0}
    level = "FAIL" if total >= 1000 else ("WARN" if total > 0 else "OK")
    return {"level": level, "keys": by_key, "reasons": last_reason,
            "total": total, "path": p, "days": len(days)}


def check_baseline() -> Dict[str, Any]:
    p = os.path.join(BASELINE, "summary.json")
    if not os.path.exists(p):
        return {"available": False}
    with open(p, encoding="utf-8") as f:
        return {"available": True, **json.load(f)}


def check_discards() -> Dict[str, Any]:
    """丢弃审计（2026-11-16 新增）。

    为什么必须单独统计：超过时效的 PENDING 会被 UPDATE 成 status='FILTERED'，
    与"正常被门卫过滤"的新闻**在库里长得一模一样**，事后无法区分。
    所以只能从引擎日志的 `[BATCH] 归档超龄新闻 N 条` 里累计。
    没有这个数，就不知道"每天到底扔了多少信号"。

    口径说明：扫当前 engine.out.log + 当日轮转归档；跨日会被低估，属于已知偏差。
    """
    import re
    logs = [os.path.expanduser("~/.trident/log/engine.out.log")]
    arch = os.path.expanduser("~/.trident/log/archive")
    today = _now().strftime("%Y-%m-%d")
    try:
        for name in os.listdir(arch):
            if name.startswith("engine.out") and today in name:
                logs.append(os.path.join(arch, name))
    except Exception:
        pass

    total, hits = 0, 0
    pat = re.compile(r"归档超龄新闻\s*([0-9]+)\s*条")
    for path in logs:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if m:
                        total += int(m.group(1))
                        hits += 1
        except Exception:
            continue
    level = "OK"
    if total >= 300:
        level = "WARN"
    return {"discarded": total, "batches": hits, "level": level, "files": len(logs)}


def check_eval() -> Dict[str, Any]:
    """冻结评测集比对（2026-11-16 接入）。

    为什么放进体检而不是 CI：它需要真实的 1.4GB 库，CI 环境没有。
    每天跑一次 60 条，就能回答"昨天到今天，输出有没有悄悄变"。
    跑不动或库不可读时返回 level=SKIP，不让体检因此变红。
    """
    script = os.path.join(REPO_ROOT, "backend", "scripts", "oneoff", "eval_frozen.py")
    if not os.path.exists(os.path.join(REPO_ROOT, "backend", "tests", "eval", "frozen_set.json")):
        return {"level": "SKIP", "reason": "无基准（先跑 eval_frozen.py --freeze）"}
    try:
        proc = subprocess.run([sys.executable, script], capture_output=True,
                              text=True, timeout=180)
    except Exception as exc:
        return {"level": "SKIP", "reason": f"{type(exc).__name__}: {exc}"}
    tail = (proc.stdout or "").strip().splitlines()
    result_line = next((x for x in reversed(tail) if x.startswith("[RESULT]")), "")
    def _count(label: str) -> int:
        if label not in result_line:
            return 0
        import re
        m = re.search(label + r"\s*(\d+)", result_line)
        return int(m.group(1)) if m else -1

    drift = _count("漂移")
    missing = _count("消失")
    return {
        "level": "WARN" if proc.returncode != 0 else "OK",
        "exit_code": proc.returncode,
        "drift": drift,
        "missing": missing,
        "result": result_line or "（无输出）",
        "detail": "\n".join([x for x in tail if x.startswith("[DRIFT]") or x.startswith("[MISSING]")])[:2000],
    }


def render(report: Dict[str, Any]) -> str:
    now = report["generated_at"]
    lines = [
        f"# 系统体检 · {now}", "",
        f"**总体：{report['verdict']}**", "",
        "| 检查项 | 结果 | 关键数字 |", "|---|---|---|",
    ]
    if _SAFE_ERRORS:
        # 有查询失败时，上面的数字可能偏小甚至为 0 —— 必须在最前面说明，否则等于撒谎。
        lines += [
            "",
            f"> ⚠️ **{len(_SAFE_ERRORS)} 条查询失败**（下表相关数字可能偏小）：",
            *[f"> - `{e}`" for e in _SAFE_ERRORS[:5]],
        ]
        if len(_SAFE_ERRORS) > 5:
            lines.append(f"> - …… 其余 {len(_SAFE_ERRORS) - 5} 条省略")
    d = report["decisions"]
    lines.append(f"| 决策活跃度 | {d['level']} | 近 6h {d['last_6h']} 条 / 近 24h {d['last_24h']} 条 / 表内共 {d['total_rows']} 行 |")
    l = report["llm"]
    lines.append(f"| LLM 通道 | {l['level']} | 近 1h {l['calls_1h']} 次调用，错误率 {l['error_ratio']*100:.0f}% |")
    h = report["heartbeat"]
    errs = [w["worker"] for w in h["workers"] if w["status"] == "error"]
    lines.append(f"| Worker 心跳 | {h['level']} | 报错 {len(errs)} 个：{', '.join(errs) or '无'} |")
    q = report["dlq"]
    lines.append(f"| DLQ | {q['level']} | 未解决 {q['unresolved']} 条 |")
    db = report["db"]
    lines.append(f"| 库体积 | {db['level']} | {db['size_mb']} MB，按近 7 天速率约 +{db['projected_30d_growth_mb']} MB/30 天 |")
    ports = report["ports"]
    # v483: 已退役端口（required=False）只登记、不判 FAIL —— 8000 的 api_server
    # 没有任何 launchd 守护（ensure_core_jobs.sh 只管 bridge/engine/frontend），
    # 天天拿它判 FAIL 只会训练出"FAIL 不用看"，把真 FAIL 一起淹掉。
    down = [f"{p['service']}:{p['port']}" for p in ports
            if p.get("required", True) and not p["reachable"]]
    retired = [f"{p['service']}:{p['port']}" for p in ports
               if not p.get("required", True) and not p["reachable"]]
    note = f"不可连：{', '.join(down)}" if down else "全部可连"
    if retired:
        note += f"（已退役不判 FAIL：{', '.join(retired)}）"
    lines.append(f"| 端口存活 | {'FAIL' if down else 'OK'} | {note} |")
    ev = report.get("eval") or {}
    lines.append(f"| 冻结评测集 | {ev.get('level', 'SKIP')} | {ev.get('result', ev.get('reason', '-'))} |")
    dc = report.get("discards") or {}
    lines.append(f"| 丢弃（超龄归档） | {dc.get('level', 'OK')} | 今日累计 {dc.get('discarded', 0)} 条（{dc.get('batches', 0)} 批） |")
    sd = report.get("silent") or {}
    if sd.get("total"):
        top = sorted((sd.get("keys") or {}).items(), key=lambda kv: -kv[1])[:3]
        lines.append(f"| 丢写账本 | {sd.get('level', 'OK')} | 今日 {sd['total']} 条："
                     f"{'，'.join(f'{k} {v}' for k, v in top)} |")
    else:
        lines.append(f"| 丢写账本 | {sd.get('level', 'OK')} | 今日无丢写记录 |")
    k = report.get("klines") or {}
    if k.get("stale_count"):
        first = k["stale"][0]
        lines.append(f"| 行情新鲜度 | {k.get('level', 'OK')} | {k['stale_count']} 条序列过期，最老 {first['symbol']} {first['timeframe']} 落后 {first['lag_hours']}h |")
    else:
        lines.append(f"| 行情新鲜度 | {k.get('level', 'SKIP')} | {k.get('checked', 0)} 条活跃序列全部新鲜 |")

    if q.get("items"):
        lines += ["", "## 未处理的 DLQ 事件", "", "| id | kind | 时间 | 摘要 |", "|---|---|---|---|"]
        for i in q["items"][:6]:
            lines.append(f"| {i[0]} | {i[1]} | {i[3]} | `{(i[4] or '').replace('|','/')}` |")

    if l.get("recent"):
        lines += ["", "## 最近 6 次 LLM 调用", "", "| purpose | 模型 | 状态 | 错误码 | 延迟(ms) |", "|---|---|---|---|---|"]
        for r in l["recent"]:
            lines.append(f"| {r[0]} | `{r[1]}` | {r[2]} | {r[3] or '-'} | {r[4]} |")

    if db.get("top_tables"):
        lines += ["", "## 大表（>1 万行）", "", "| 表 | 行数 | 近 7 天/天 |", "|---|---|---|"]
        for r in db["top_tables"]:
            lines.append(f"| {r['table']} | {r['rows']:,} | {db['rows_per_day_7d'].get(r['table'], '-')} |")

    if ev.get("detail"):
        lines += ["", "## 评测集漂移明细", "", "```", ev["detail"], "```"]

    b = report.get("baseline", {})
    if b.get("available"):
        lines += ["", "## 端点基线（冻结值）", "",
                  f"- 端点总数 {b['total']}（api_server {b['by_server'].get('api_server', 0)} / "
                  f"bridge {b['by_server'].get('bridge_server', 0)}）",
                  f"- 两个 server 字面重复 {b['duplicate_count']} 条",
                  f"- 前端零引用（近似）{b['frontend_zero_ref_count']} 条"]

    lines += ["", "## 建议动作", ""]
    lines += [f"- {a}" for a in report["actions"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="每日系统体检（只读）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "backend", "reports", "health"))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在：{args.db}")

    conn = _ro(args.db)
    try:
        decisions = check_decisions(conn)
        llm = check_llm(conn)
        heartbeat = check_heartbeat(conn)
        dlq = check_dlq(conn)
        db = check_db(conn, args.db)
        klines = check_klines(conn)
    finally:
        conn.close()

    ports = check_ports()
    baseline = check_baseline()
    eval_res = check_eval()
    discards = check_discards()
    silent = check_silent_drops()

    ports_level = "FAIL" if any(
        p.get("required", True) and not p["reachable"] for p in ports) else "OK"
    levels = [decisions["level"], llm["level"], heartbeat["level"], dlq["level"], db["level"],
              eval_res.get("level", "OK"), klines.get("level", "OK"), ports_level,
              silent.get("level", "OK")]
    verdict = "FAIL" if "FAIL" in levels else ("WARN" if "WARN" in levels else "OK")

    actions: List[str] = []
    if ports_level == "FAIL":
        bad = [f"{p['service']}:{p['port']}" for p in ports
               if p.get("required", True) and not p["reachable"]]
        actions.append(f"核心端口不可连：{', '.join(bad)} —— 先看 "
                       f"~/.trident/log/ensure_core_jobs.log 里有没有 bootstrap/kickstart 失败")
    if decisions["level"] == "FAIL":
        actions.append("决策链路停摆：先看 ai_call_stats 最近 error_code（429=额度/限流，network_error=本地模型没起）")
    if llm["level"] == "FAIL":
        actions.append("LLM 通道大面积失败：确认 MiniMax 额度 / 本地 Ollama 是否在跑；必要时降级到单通道")
    if heartbeat["level"] == "FAIL":
        actions.append(f"Worker 报错：{' / '.join(w['worker'] + ' → ' + w['error'] for w in heartbeat['workers'] if w['status']=='error')}")
    if dlq["level"] in ("FAIL", "WARN"):
        actions.append(f"DLQ 有 {dlq['unresolved']} 条未解决事件，需人工 disposition（加 resolved 标记或修根因）")
    if db["level"] == "WARN":
        actions.append("库增长偏快：跑 retention_prune.py 看保留窗口，并挂成每日任务")
    if klines.get("stale_count"):
        names = ", ".join(f"{r['symbol']}/{r['timeframe']}({r['lag_hours']}h)" for r in klines["stale"][:4])
        actions.append(f"K 线过期：{names} —— 先查对应采集班次是否卡死（日线看 daily_bars.err.log 的 database is locked）")
    if (discards.get("discarded") or 0) >= 100:
        actions.append(f"今日已丢弃 {discards['discarded']} 条超龄新闻（超过分析时效门槛）："
                       f"要么提产能，要么下调时效门槛的等待策略，别默认损失")
    if silent.get("level") in ("WARN", "FAIL"):
        top = sorted((silent.get("keys") or {}).items(), key=lambda kv: -kv[1])[:3]
        detail = "，".join(f"{k} {v} 条" for k, v in top)
        actions.append(f"今日丢写 {silent.get('total')} 条（{detail}）："
                       f"看 backend/runtime/silent_drops.jsonl 的 reason，多半是写锁竞争/表结构变动")
    if eval_res.get("level") == "WARN":
        if (eval_res.get("drift") or 0) > 0:
            actions.append(f"冻结评测集出现输出漂移（{eval_res.get('result')}）：先确认是有意改动，"
                           f"再决定更新基准 eval_frozen.py --freeze")
        elif (eval_res.get("missing") or 0) > 0:
            actions.append(f"冻结评测基准样本已不在库（{eval_res.get('result')}，数据被清/重置，非输出回归）："
                           f"待带决策的新闻重新积累到 60 条后跑 eval_frozen.py --freeze 重建基准")
    if not actions:
        actions.append("各项正常，无需动作")

    report = {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "decisions": decisions, "llm": llm, "heartbeat": heartbeat,
        "dlq": dlq, "db": db, "ports": ports, "baseline": baseline, "eval": eval_res,
        "discards": discards, "klines": klines, "silent": silent,
        "actions": actions,
    }

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    stamp = _now().strftime("%Y-%m-%d")
    md_path = os.path.join(out_dir, f"health-{stamp}.md")
    json_path = os.path.join(out_dir, f"health-{stamp}.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render(report))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(render(report))
    print(f"写入：{md_path}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
