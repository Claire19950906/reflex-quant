#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据到位后的验收检查（只读，不改库）。

    cd backend && python3 scripts/acceptance_check.py
    cd backend && python3 scripts/acceptance_check.py --json

回答一个问题：现在这套东西「能不能用来交易」，还是差哪一段。
检查项：数据新鲜度 / 引擎心跳 / 今日决策与成交 / 拒单原因 / 账户口径 /
杠杆 / 日亏 / 止损覆盖率 / paper run 唯一性 / 未处理告警。

退出码：0=无 FAIL（可能有 WARN），1=存在 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import account_ledger as al

R = []


def add(name, status, detail):
    R.append({"name": name, "status": status, "detail": str(detail)})


def q(conn, sql, params=()):
    try:
        return list(conn.execute(sql, params))
    except Exception as exc:
        return [{"__err__": f"{type(exc).__name__}: {exc}"}]


def env_num(key, default):
    v = os.environ.get(key)
    if v is None:
        envf = os.path.join(BACKEND, ".env")
        if os.path.exists(envf):
            for line in open(envf, encoding="utf-8"):
                line = line.strip()
                if line.startswith(key + "="):
                    v = _clean_env_value(line.split("=", 1)[1])
                    break
    try:
        return float(v) if v not in (None, "") else float(default)
    except ValueError:
        return float(default)



def _clean_env_value(v: str) -> str:
    """去掉未加引号值后面的行内注释（`KEY=90   # 说明` → `90`）。

    原来直接把整行当值写进 os.environ，导致 `int(os.getenv(...))` 在 import 期就炸，
    `feeds.quote_sources` 整个模块导入失败，行情路由全部退化成 unknown。
    """
    v = (v or "").strip()
    if v[:1] in ('"', "'"):
        q = v[0]
        end = v.rfind(q)
        return v[1:end] if end > 0 else v.strip(q)
    for sep in (" #", "\t#"):
        idx = v.find(sep)
        if idx >= 0:
            v = v[:idx]
    return v.strip()

def load_env_defaults() -> None:
    """把 backend/.env 的键设为默认值（不覆盖已显式设置的环境变量）。

    对账/前置闸都从 os.environ 读阈值，不加载就会回落到代码默认的 100% 杠杆上限。
    """
    envf = os.path.join(BACKEND, ".env")
    if not os.path.exists(envf):
        return
    for line in open(envf, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), _clean_env_value(v)
        if k and k not in os.environ:
            os.environ[k] = v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BACKEND, "trident_event_bus.db"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-tick-age", type=float, default=600)
    a = ap.parse_args(argv)
    now = int(time.time())
    load_env_defaults()

    if not os.path.exists(a.db):
        print(f"库不存在：{a.db}", file=sys.stderr)
        return 2
    size_mb = os.path.getsize(a.db) / 1024 / 1024
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    add("库", "PASS", f"{size_mb:.0f} MB · {a.db}")

    # 服务与暂停状态
    try:
        jobs = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
        n_jobs = sum(1 for line in jobs.splitlines() if "com.trident." in line)
        engine = any("com.trident.engine" in l and not l.startswith("-") for l in jobs.splitlines())
    except Exception:
        n_jobs, engine = -1, False
    paused = os.path.exists(os.path.expanduser("~/.trident/state/DATA_PAUSED"))
    add("服务", "PASS" if engine else "WARN",
        f"trident 任务={n_jobs} · engine={'在跑' if engine else '未跑'} · "
        f"DATA_PAUSED={'是' if paused else '否'}")

    # 引擎心跳
    prog = os.path.join(BACKEND, "runtime", "ai_worker_progress.json")
    if os.path.exists(prog):
        age = now - int(os.path.getmtime(prog))
        add("引擎心跳", "PASS" if age <= 600 else "WARN", f"progress {age}s 未更新")
    else:
        add("引擎心跳", "WARN" if paused else "FAIL", "没有 progress 文件")

    # 数据新鲜度
    rows = q(conn, "SELECT symbol, MAX(ts) AS ts, COUNT(*) AS n FROM market_ticks "
                   "GROUP BY symbol ORDER BY ts DESC")
    if not rows or "__err__" in rows[0]:
        add("行情", "FAIL", "market_ticks 读不到")
    else:
        # v494：只对"此刻本该更新"的品种判过期；美股收盘后 old tick 是正常的
        try:
            import market_rules as _mr
        except Exception:
            _mr = None
        def _tradable(asset):
            if not _mr:
                return True
            return _mr.classify(asset) != "unknown" and _mr.session_open(asset)[0]
        open_rows = [r for r in rows if _tradable(r["symbol"])]
        if _mr is not None and not open_rows:
            newest = int(rows[0]["ts"] or 0)
            add("行情", "PASS",
                f"{len(rows)} 个品种全部休市（非交易时段）· 最新 {now - newest}s 前")
        else:
            newest = max(int(r["ts"] or 0) for r in open_rows)
            age = now - newest
            st = "PASS" if age <= a.max_tick_age else ("WARN" if paused else "FAIL")
            closed_n = len(rows) - len(open_rows)
            top = ", ".join(f"{r['symbol']}({now - int(r['ts'] or now)}s)" for r in open_rows[:5])
            add("行情", st, f"{len(open_rows)} 个在交易 · 最新 {age}s 前 · {top}"
                            + (f" · {closed_n} 个休市" if closed_n else ""))

    today = time.strftime("%Y-%m-%d", time.localtime())
    d = q(conn, "SELECT COUNT(*) AS n FROM ai_decisions WHERE substr(created_at,1,10)=?", (today,))
    n_dec = d[0]["n"] if d and "__err__" not in d[0] else -1
    o = q(conn, "SELECT COUNT(*) AS n FROM trade_audit_log WHERE substr(created_at,1,10)=? "
               "AND status='ORDER_PLACED'", (today,))
    n_ord = o[0]["n"] if o and "__err__" not in o[0] else -1
    add("今日决策/成交", "PASS" if n_dec > 0 else "WARN", f"决策 {n_dec} 条 · 成交 {n_ord} 笔")

    rj = q(conn, "SELECT reason, COUNT(*) AS n FROM trade_audit_log "
                 "WHERE status='RISK_REJECTED' AND substr(created_at,1,10)=? "
                 "GROUP BY reason ORDER BY n DESC LIMIT 3", (today,))
    if rj and "__err__" not in rj[0]:
        add("今日拒单", "PASS", " · ".join(f"{r['reason']}({r['n']})" for r in rj) or "无")
    else:
        add("今日拒单", "PASS", "无")

    # 账户
    try:
        c = al.canonical(conn)
        lev = float(c["leverage_pct"])
        lim = env_num("MAX_GROSS_LEVERAGE_PCT", 200)
        add("账户", "PASS",
            f"cash {c['cash']:.2f} · equity {c['equity']:.2f} · realized {c['realized']:.2f} · "
            f"{c['n_open']} 笔 · 名义 {c['gross_exposure']:.0f}")
        add("杠杆", "PASS" if lev <= lim else "FAIL", f"{lev:.0f}% / 上限 {lim:.0f}%")
    except Exception as exc:
        add("账户", "FAIL", f"canonical 失败：{exc}")

    # 对账
    try:
        issues = al.reconcile(conn)
        blocks = [i["code"] for i in issues if i.get("severity") == "block"]
        warns = [i["code"] for i in issues if i.get("severity") != "block"]
        add("对账", "FAIL" if blocks else "PASS",
            f"block={blocks or '无'} · warn={warns or '无'}")
    except Exception as exc:
        add("对账", "FAIL", f"reconcile 失败：{exc}")

    # 日亏
    try:
        limit = env_num("DAILY_LOSS_LIMIT_USDT", 0)
        rows = q(conn, "SELECT equity FROM account_snapshots WHERE ts<? ORDER BY ts DESC LIMIT 1",
                 (int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))),))
        if rows and "__err__" not in rows[0]:
            day_open = float(rows[0]["equity"])
        else:
            r2 = q(conn, "SELECT equity FROM account_snapshots ORDER BY ts ASC LIMIT 1")
            day_open = float(r2[0]["equity"]) if r2 and "__err__" not in r2[0] else None
        if limit <= 0:
            add("日亏", "WARN", "未设限额（DAILY_LOSS_LIMIT_USDT=0）")
        elif day_open is None:
            add("日亏", "WARN", "没有快照，无法算日初权益")
        else:
            cur = q(conn, "SELECT equity FROM t2_account WHERE id=1")
            eq = float(cur[0]["equity"]) if cur and "__err__" not in cur[0] else day_open
            loss = day_open - eq
            add("日亏", "FAIL" if loss >= limit else ("WARN" if loss >= limit * 0.8 else "PASS"),
                f"{loss:.2f} / 限额 {limit:.0f}（日初 {day_open:.2f} → 当前 {eq:.2f}）")
    except Exception as exc:
        add("日亏", "FAIL", f"{exc}")

    # 止损覆盖
    ns = q(conn, "SELECT COUNT(*) AS n FROM t2_positions WHERE status='open' "
                 "AND (stop_price IS NULL OR stop_price<=0)")
    if ns and "__err__" not in ns[0]:
        add("止损覆盖", "FAIL" if ns[0]["n"] else "PASS", f"无止损持仓 {ns[0]['n']} 笔")
    else:
        add("止损覆盖", "WARN", "查不到 t2_positions")

    # run 唯一性
    nr = q(conn, "SELECT COUNT(*) AS n FROM paper_trading_runs WHERE status='RUNNING'")
    if nr and "__err__" not in nr[0]:
        add("run 唯一性", "FAIL" if nr[0]["n"] > 1 else "PASS", f"RUNNING={nr[0]['n']}")

    # 未处理告警
    na = q(conn, "SELECT COUNT(*) AS n FROM t2_alerts WHERE resolved_at IS NULL")
    if na and "__err__" not in na[0]:
        add("未处理告警", "WARN" if na[0]["n"] else "PASS", f"{na[0]['n']} 条")

    fails = [r for r in R if r["status"] == "FAIL"]
    if a.json:
        print(json.dumps({"checks": R, "fails": len(fails)}, ensure_ascii=False, indent=2))
    else:
        w = max(len(r["name"]) for r in R) + 2
        print("\n验收检查（只读）")
        print("-" * (w + 46))
        for r in R:
            print(f"{r['status']:<5} {r['name']:<{w}} {r['detail']}")
        print("-" * (w + 46))
        print(f"FAIL {len(fails)} 项 / 共 {len(R)} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
