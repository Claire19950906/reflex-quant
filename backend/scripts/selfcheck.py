#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck.py — 一次跑完「能离线验的」和「等数据才能验的」，把结论摆成一张表。

为什么要有这个脚本
------------------
迭代节奏是"改代码 → 跑一遍 → 看对不对"，但真实库里随时可能没数据（库被重置、
行情源停摆、决策表清空），于是大量断言只能靠人肉判断"现在能不能验"。
本脚本把两类检查分开：

  L1 静态     ruff（若可用）+ 语法编译 + 核心模块 import 冒烟 —— 永远可跑，秒级
  L2 契约     单元测试子集（audit 闸门 / 落库 / 回测路径 / 丢写账本）—— 分钟级
  L3 数据探针  只读连库，检查"数据够不够、够的话结论对不对"—— 数据不足就 SKIP

L3 的设计要点（这就是"留一个口"）：
  · 每个探针自带 `needs`（需要什么数据才生效）与 `ready()`；
  · 数据不足 → 输出 SKIP + 明确写清「等什么、等多少、等到了看哪个字段」；
  · 数据够了 → 自动升级为 OK / WARN / FAIL，不需要改代码；
  · 任何探针抛异常都降级成 ERROR，绝不中断整轮自检。

用法
----
    python3 backend/scripts/selfcheck.py                # L1 + L3（默认，~10s）
    python3 backend/scripts/selfcheck.py --full         # 再加 L2 单测（~2-3min）
    python3 backend/scripts/selfcheck.py --json out.json
    python3 backend/scripts/selfcheck.py --probe exec_state

退出码：0 = 全部 OK/SKIP；1 = 有 WARN 或 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO_ROOT, "backend")
SRC = os.path.join(BACKEND, "src_python")
DEFAULT_DB = os.path.join(BACKEND, "trident_event_bus.db")
RUFF = os.getenv("TRIDENT_RUFF") or "/tmp/ruffpkg/bin/ruff"

OK, WARN, FAIL, SKIP, ERROR = "OK", "WARN", "FAIL", "SKIP", "ERROR"
BAD = (WARN, FAIL, ERROR)


# ─────────────────────────── 基础设施 ───────────────────────────
class Result:
    def __init__(self, layer: str, name: str, status: str,
                 detail: str = "", hint: str = "", cost_ms: int = 0):
        self.layer, self.name = layer, name
        self.status, self.detail, self.hint = status, detail, hint
        self.cost_ms = cost_ms

    def as_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "name": self.name, "status": self.status,
                "detail": self.detail, "hint": self.hint, "cost_ms": self.cost_ms}


def _ro(db: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """只读连库；库不存在返回 None（不创建空库！）。"""
    path = db or DEFAULT_DB
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        return conn
    except sqlite3.Error:
        return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    except sqlite3.Error:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[tuple]:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


# ─────────────────────────── L1 静态 ───────────────────────────
def l1_ruff() -> Result:
    t0 = time.time()
    if not os.path.exists(RUFF):
        return Result("L1", "ruff", SKIP, "ruff 未安装",
                      "python3 -m pip install --quiet --target /tmp/ruffpkg ruff")
    try:
        p = subprocess.run([RUFF, "check", SRC, os.path.join(BACKEND, "scripts"),
                            os.path.join(BACKEND, "tests")],
                           capture_output=True, text=True, timeout=180,
                           cwd=REPO_ROOT)
    except Exception as exc:
        return Result("L1", "ruff", ERROR, str(exc)[:200], "", int((time.time() - t0) * 1000))
    tail = (p.stdout or p.stderr or "").strip().splitlines()[-1:] or [""]
    status = OK if p.returncode == 0 else FAIL
    return Result("L1", "ruff", status, tail[0][:200],
                  "" if status == OK else "跑 /tmp/ruffpkg/bin/ruff check 看明细",
                  int((time.time() - t0) * 1000))


def l1_compile() -> Result:
    """全量语法编译：比 import 便宜，能抓住所有语法级问题。"""
    t0 = time.time()
    bad = []
    n = 0
    for root, _dirs, files in os.walk(SRC):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            n += 1
            ok = False
            for attempt in (0, 1):
                try:
                    with open(path, "rb") as fh:
                        compile(fh.read(), path, "exec")
                    ok = True
                    break
                except SyntaxError as exc:
                    # 编辑器/另一个会话正在写这个文件时，读到半截源码是常态。
                    # mtime 在 8 秒内 → 等等再读一次，避免把"写了一半"判成语法错。
                    fresh = time.time() - os.path.getmtime(path) < 8
                    if attempt == 0 and fresh:
                        time.sleep(1.2)
                        continue
                    bad.append(f"{os.path.relpath(path, REPO_ROOT)}:{exc.lineno}")
                    break
                except OSError:
                    break
    ms = int((time.time() - t0) * 1000)
    if bad:
        return Result("L1", "语法编译", FAIL, f"{len(bad)} 个文件语法错：{', '.join(bad[:5])}", "", ms)
    return Result("L1", "语法编译", OK, f"{n} 个文件全部可编译", "", ms)


CORE_MODULES = ("config", "db", "silent_ledger", "cron_health",
                "review.reflection_checkers", "analysis.contract")


def l1_import() -> Result:
    """核心模块 import 冒烟：抓循环导入 / 顶层副作用炸掉。"""
    t0 = time.time()
    code = (
        "import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "import importlib\n"
        f"mods = {CORE_MODULES!r}\n"
        "for m in mods:\n"
        "    importlib.import_module(m)\n"
        "print('IMPORT_OK', len(mods))\n"
    )
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=120, cwd=BACKEND)
    except Exception as exc:
        return Result("L1", "核心 import", ERROR, str(exc)[:200], "", int((time.time() - t0) * 1000))
    ms = int((time.time() - t0) * 1000)
    if "IMPORT_OK" in (p.stdout or ""):
        return Result("L1", "核心 import", OK, f"{len(CORE_MODULES)} 个核心模块可导入", "", ms)
    err = ((p.stderr or "").strip().splitlines() or ["?"])[-1]
    return Result("L1", "核心 import", FAIL, err[:200], "看是否有循环导入或顶层副作用", ms)


# ─────────────────────────── L2 契约测试 ───────────────────────────
CONTRACT_TESTS = (
    "audit/tests/test_gate.py",
    "backend/tests/test_silent_ledger.py",
    "backend/tests/test_backtest_pro.py",
    "backend/tests/test_v489_run_write_busy_cap.py",
)


def l2_pytest() -> Result:
    t0 = time.time()
    args = [sys.executable, "-m", "pytest", *CONTRACT_TESTS, "-q", "-p", "no:cacheprovider"]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT)
    except Exception as exc:
        return Result("L2", "契约测试", ERROR, str(exc)[:200], "", int((time.time() - t0) * 1000))
    ms = int((time.time() - t0) * 1000)
    last = ((p.stdout or "").strip().splitlines() or [""])[-1]
    status = OK if p.returncode == 0 else FAIL
    return Result("L2", "契约测试", status, last[:200],
                  "" if status == OK else "复现：python3 -m pytest audit/tests backend/tests -q", ms)


# ─────────────────────────── 运行态：暂停 / 端口 ───────────────────────────
PAUSE_FLAG = os.path.join(os.path.expanduser("~"), ".trident", "state", "DATA_PAUSED")


def _pause_state() -> Optional[str]:
    """数据采集是否被人工/程序暂停（pause_data.sh 会写这个 0 字节标记）。

    为什么必须先判：暂停时引擎被干掉、库不再长，决策/行情探针会集体掉成 FAIL，
    于是每次"停下来改代码"都被自己的自检骂一顿。暂停期一律降级为 SKIP。
    """
    if not os.path.exists(PAUSE_FLAG):
        return None
    try:
        mt = datetime.fromtimestamp(os.path.getmtime(PAUSE_FLAG))
        return mt.strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "?"


SERVICES = ((8100, "bridge", True), (3030, "frontend", True), (9001, "master", False))


def probe_services() -> Result:
    t0 = time.time()
    down, up = [], []

    def _tcp(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.8)
        try:
            return sock.connect_ex(("127.0.0.1", port)) == 0
        finally:
            sock.close()

    for port, name, required in SERVICES:
        ok = _tcp(port)
        # 重启窗口实测约 50 秒（bridge 要加载模型），一次连不上不代表服务死了：
        # 再给两轮试探（间隔 3 秒），避免"刚好在重启"被判 FAIL 冤枉人。
        for _ in range(2 if required and not ok else 0):
            time.sleep(3)
            ok = _tcp(port)
            if ok:
                break
        (up if ok else (down if required else [])).append(f"{name}:{port}")
        if not ok and not required:
            down.append(f"{name}:{port}（非必须）")
    ms = int((time.time() - t0) * 1000)
    if any("非必须" not in d for d in down):
        return Result("L3", "服务端口", FAIL, f"不可连：{', '.join(down)}",
                      "看 ~/.trident/log/{bridge,service-guard}.out.log；8100 重启期间 3030/api 会 500", ms)
    return Result("L3", "服务端口", OK, f"可连：{', '.join(up)}" +
                  (f"；{'，'.join(down)}" if down else ""), "", ms)


def _skipped_by_pause(name: str) -> Optional[Result]:
    stamp = _pause_state()
    if stamp is None:
        return None
    return Result("L3", name, SKIP, f"数据采集已暂停（{stamp} 起）",
                  "`bash ~/.trident/wrappers/resume_data.sh` 恢复后本项自动重测")


# ─────────────────────────── L3 数据探针 ───────────────────────────
def probe_db() -> Result:
    """库本身是否是可用的真库（不是 0 表空壳）。"""
    t0 = time.time()
    conn = _ro()
    if conn is None:
        return Result("L3", "库可用", FAIL, f"库不存在：{DEFAULT_DB}", "确认 TRIDENT_DB_PATH / 工作目录", 0)
    try:
        n = _q(conn, "SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        tables = n[0] if n else 0
        size = os.path.getsize(DEFAULT_DB) // 1048576
    finally:
        conn.close()
    if tables == 0:
        return Result("L3", "库可用", FAIL, f"0 张表（{size}MB）：连到空壳库了",
                      "真库应为 backend/trident_event_bus.db；空壳已改名 *.empty-shell.bak", 0)
    return Result("L3", "库可用", OK, f"{tables} 张表 / {size}MB", "", 0)


def probe_decisions() -> Result:
    """决策表有没有数据 —— 这是绝大多数"功能对不对"的前提。"""
    paused = _skipped_by_pause("决策数据")
    if paused:
        return paused
    conn = _ro()
    if conn is None:
        return Result("L3", "决策数据", SKIP, "库不可用", "等库恢复")
    try:
        if not _has_table(conn, "ai_decisions"):
            return Result("L3", "决策数据", SKIP, "无 ai_decisions 表", "等引擎写入首条决策")
        total = _q(conn, "SELECT COUNT(*) FROM ai_decisions")[0]
        directional = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                               "WHERE UPPER(suggested_action) IN ('BUY','SELL')")
        directional = directional[0] if directional else 0
        settled = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE outcome IS NOT NULL") \
            if "outcome" in _columns(conn, "ai_decisions") else None
        settled_n = settled[0] if settled else 0
    finally:
        conn.close()
    if total < 50:
        return Result("L3", "决策数据", SKIP,
                      f"表内 {total} 行（方向单 {directional}，已结算 {settled_n}）",
                      "等 ≥50 条决策、≥10 条已结算后才能判读命中率/落地率")
    return Result("L3", "决策数据", OK,
                  f"{total} 行 / 方向单 {directional} / 已结算 {settled_n}", "")


def probe_entry_price() -> Result:
    """方向单有没有建仓价 —— 89% 缺失是审计里的头号语义空洞。"""
    conn = _ro()
    if conn is None:
        return Result("L3", "方向单建仓价", SKIP, "库不可用", "等库恢复")
    try:
        cols = _columns(conn, "ai_decisions")
        if not cols:
            return Result("L3", "方向单建仓价", SKIP, "无 ai_decisions 表", "等引擎写入")
        has_price = "entry_price" in cols
        has_reason = "no_trade_reason" in cols
        d = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                     "WHERE UPPER(suggested_action) IN ('BUY','SELL')")[0]
        if d < 10:
            return Result("L3", "方向单建仓价", SKIP, f"方向单仅 {d} 条",
                          "等 ≥10 条方向单再判读（低于此样本量结论无意义）")
        empty_price = (_q(conn, "SELECT COUNT(*) FROM ai_decisions "
                                "WHERE UPPER(suggested_action) IN ('BUY','SELL') "
                                "AND (entry_price IS NULL OR entry_price = 0)")[0]
                       if has_price else d)
        empty_reason = (_q(conn, "SELECT COUNT(*) FROM ai_decisions "
                                 "WHERE UPPER(suggested_action) IN ('BUY','SELL') "
                                 "AND (no_trade_reason IS NULL OR no_trade_reason = '')")[0]
                        if has_reason else d)
    finally:
        conn.close()
    ratio = empty_price / d
    detail = f"方向单 {d} 条，缺建仓价 {empty_price}（{ratio*100:.0f}%），缺不交易原因 {empty_reason}"
    if ratio >= 0.5:
        return Result("L3", "方向单建仓价", FAIL, detail,
                      "合同层要求：BUY/SELL 要么有建仓价，要么有不交易原因；现在两头都空 = 语义空洞")
    if ratio > 0:
        return Result("L3", "方向单建仓价", WARN, detail, "看 ai_worker 取价分支的埋点是否补齐")
    return Result("L3", "方向单建仓价", OK, detail, "")


def probe_exec_state() -> Result:
    """「方向单必须有账」不变量 —— 字段还没加时留口，加了自动生效。"""
    conn = _ro()
    if conn is None:
        return Result("L3", "exec_state 不变量", SKIP, "库不可用", "等库恢复")
    try:
        cols = _columns(conn, "ai_decisions")
    finally:
        conn.close()
    if "exec_state" not in cols:
        return Result("L3", "exec_state 不变量", SKIP,
                      "ai_decisions 还没有 exec_state 列",
                      "等字段落地（迁移脚本就绪后）本项自动启用："
                      "断言 BUY/SELL ⇒ entry_price 非空 OR exec_state 非空")
    conn = _ro()
    try:
        viol = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                        "WHERE UPPER(suggested_action) IN ('BUY','SELL') "
                        "AND (entry_price IS NULL OR entry_price = 0) "
                        "AND (exec_state IS NULL OR exec_state = '')")[0]
    finally:
        conn.close()
    if viol:
        return Result("L3", "exec_state 不变量", FAIL, f"{viol} 条方向单既无建仓价也无执行状态",
                      "这是不变量被破坏：要么补价，要么落 exec_state=not_executed")
    return Result("L3", "exec_state 不变量", OK, "无违反：每条方向单要么有价、要么有执行状态", "")


_CRYPTO_SYMS = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON"}
_COMMODITY_SYMS = {"XAU", "XAG", "WTI", "BRENT", "GOLD", "COPPER", "NATGAS", "SILVER"}


def _asset_class(symbol: str) -> str:
    """品种分类：决定"多久没更新才算异常"。"""
    up = (symbol or "").upper().strip()
    if up in _CRYPTO_SYMS or up.endswith("USDT"):
        return "crypto"
    if up in _COMMODITY_SYMS:
        return "commodity"
    return "equity"


def probe_market_fresh() -> Result:
    """行情是否新鲜 —— 拿旧 K 线当现价是最贵的一类 bug。"""
    paused = _skipped_by_pause("行情新鲜度")
    if paused:
        return paused
    conn = _ro()
    if conn is None:
        return Result("L3", "行情新鲜度", SKIP, "库不可用", "等库恢复")
    try:
        if not _has_table(conn, "kline_bars"):
            return Result("L3", "行情新鲜度", SKIP, "无 kline_bars 表", "等采集写入")
        now_ms = int(time.time() * 1000)
        rows = conn.execute(
            "SELECT symbol, timeframe, MAX(ts) FROM kline_bars WHERE ts >= ? "
            "GROUP BY symbol, timeframe", (now_ms - 7 * 86400000,)).fetchall()
    finally:
        conn.close()
    # v489：阈值必须按资产类别分开，否则"美股收盘"会被判成"采集卡死"。
    # 实测 19:23 报 32 条过期，其中 30 条是 AMD/NVDA 这类股票在非交易时段的正常静默，
    # 只有 000660.KS（韩股日线，149 小时）是真缺档 —— 假警报会把真问题埋掉。
    limits = {
        # 7×24 交易
        "crypto": {"1m": 15, "3m": 45, "5m": 25, "15m": 45, "30m": 90,
                   "1h": 120, "4h": 480, "1d": 5760},
        # 商品：工作日近 24h，周末休市 → 放宽
        "commodity": {"1m": 30, "3m": 90, "5m": 60, "15m": 120, "30m": 240,
                      "1h": 300, "4h": 900, "1d": 7200},
        # 股票/ETF：只在交易时段长，隔夜+周末是常态 → 高频周期给 24h 容忍，
        # 日线给 4 天（跨周末），超过才算真缺档
        "equity": {"1m": 1440, "3m": 1440, "5m": 1440, "15m": 1440, "30m": 1440,
                   "1h": 1440, "4h": 2880, "1d": 5760},
    }
    stale = []
    for sym, tf, last in rows:
        cls = _asset_class(sym)
        lim = limits[cls].get(tf)
        if not lim or not last:
            continue
        lag = (now_ms - last) / 60000
        if lag > lim:
            stale.append(f"{sym}/{tf}({lag/60:.0f}h,{_asset_class(sym)})")
    if not rows:
        return Result("L3", "行情新鲜度", SKIP, "近 7 天无任何 K 线", "等行情采集恢复")
    if stale:
        return Result("L3", "行情新鲜度", WARN, f"{len(stale)} 条过期：{', '.join(stale[:4])}",
                      "先查对应采集班次是否卡死（数据库锁 / 符号被下架）")
    return Result("L3", "行情新鲜度", OK, f"{len(rows)} 条序列全部新鲜", "")


def probe_prices_live() -> Result:
    """真实取价链路 —— 需要外网，所以缺网就 SKIP（不当作失败）。"""
    t0 = time.time()
    paused = _skipped_by_pause("取价链路")
    if paused:
        return paused
    sys.path.insert(0, SRC)
    try:
        from engine import prices as _prices  # type: ignore
    except Exception as exc:
        return Result("L3", "取价链路", SKIP, f"无法导入 prices：{str(exc)[:80]}", "", 0)
    samples = ("XAU", "BTC")
    got, fails = [], []
    for sym in samples:
        try:
            t1 = time.time()
            px = _prices._get_current_price(sym)
            dt = time.time() - t1
            (got if px else fails).append(f"{sym}={px}({dt:.1f}s)")
        except Exception as exc:
            fails.append(f"{sym}:{type(exc).__name__}")
    if not got:
        return Result("L3", "取价链路", SKIP, f"全部取不到（{', '.join(fails)}）",
                      "多为网络/源不可用：等网络恢复再看；不要把 SKIP 当 FAIL 修", 0)
    slow = [g for g in got if float(g.split("(")[1].split("s")[0]) > 3]
    status = WARN if slow else OK
    return Result("L3", "取价链路", status, f"{', '.join(got)}"[:200],
                  "取价 >3s 会拖长决策事务，考虑缓存/异步" if slow else "",
                  int((time.time() - t0) * 1000))


def probe_silent_drops() -> Result:
    """丢写账本 —— 有记录说明有静默失败正在发生。"""
    p = os.path.join(BACKEND, "runtime", "silent_drops.jsonl")
    if not os.path.exists(p):
        return Result("L3", "丢写账本", OK, "今日无丢写记录", "")
    today = datetime.now().strftime("%Y-%m-%d")
    total, keys = 0, {}
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
                if str(row.get("ts", ""))[:10] != today:
                    continue
                k = str(row.get("key", "?"))
                keys[k] = keys.get(k, 0) + int(row.get("dropped") or 0)
                total += int(row.get("dropped") or 0)
    except OSError as exc:
        return Result("L3", "丢写账本", ERROR, str(exc)[:120], "")
    if total:
        top = sorted(keys.items(), key=lambda kv: -kv[1])[:3]
        return Result("L3", "丢写账本", WARN, f"今日 {total} 条：" +
                      "，".join(f"{k} {v}" for k, v in top),
                      f"看 {os.path.relpath(p, REPO_ROOT)} 的 reason 字段")
    return Result("L3", "丢写账本", OK, "今日无丢写记录", "")


def probe_change_zero() -> Result:
    """涨跌幅"零值"体检：0 到底是"没涨没跌"，还是"压根没取到"？

    实测（2026-11-19）：market_ticks 46 万行里 27.2 万行 change24h=0（59%）。
    按 source 拆开看就露馅了 —— sina 源 90.9% 是 0，其中 WTI 100%（15.5 万行）、
    XAU 81.3%。原因是 `_parse_sina_hf` 拿不到昨收时 change_pct=None，下游两处
    `float(x or 0.0)` 把它落成了 0。前端于是把"没有数据"显示成"今天一点没动"。
    这不是脏数据，是**语义错误**，也正好踩 0 号铁律：不造假，就别把未知写成 0。
    """
    paused = _skipped_by_pause("涨跌幅零值")
    if paused:
        return paused
    t0 = time.time()
    conn = _ro()
    if conn is None:
        return Result("L3", "涨跌幅零值", SKIP, "库不可用", "等库恢复")
    try:
        rows = conn.execute(
            """
            SELECT symbol, COUNT(*) AS n, SUM(change24h=0) AS z
              FROM market_ticks
             WHERE ts >= strftime('%s','now') - 86400
             GROUP BY symbol
            HAVING n >= 200 AND z * 100.0 / n >= 50
             ORDER BY z * 100.0 / n DESC
             LIMIT 6
            """
        ).fetchall()
    except Exception as exc:
        conn.close()
        return Result("L3", "涨跌幅零值", ERROR, f"{type(exc).__name__}: {str(exc)[:120]}", "", 0)
    conn.close()
    ms = int((time.time() - t0) * 1000)
    if not rows:
        return Result("L3", "涨跌幅零值", OK, "近 24h 无「多数为 0」的标的", "", ms)
    items = [f"{r[0]} {int(r[2] * 100 / r[1])}%({r[1]} 条)" for r in rows]
    return Result(
        "L3", "涨跌幅零值", WARN,
        "这些标的近 24h 过半是 change24h=0，多半是「取不到昨收」被写成了 0：" + "，".join(items),
        "要么改库允许 NULL（需改表结构），要么前端把这类 0 显示成「—」",
        ms,
    )


# 行情采集任务：注册丢了 = 价格慢慢变成"很久以前的价格"，页面不会报错但全是旧的。
# 2026-11-19 实测：equity-quote-cron / tencent-quotes-cron 都不在 launchctl list 里，
# 30 只股票/ETF 在 17:59 之后集体停更，而库、服务、自检其他项全绿。
QUOTE_JOBS = (
    "com.trident.equity-quote-cron",
    "com.trident.tencent-quotes-cron",
    "com.trident.tick-secondary",
)


def probe_quote_jobs() -> Result:
    """行情采集任务是否真的注册在 launchd 里（有 plist 文件 ≠ 已注册）。"""
    # 暂停期 pause-guard 会主动把采集任务 bootout（实测：手工 bootstrap 几秒后又被卸掉），
    # 这时报"未注册"是冤枉 —— 暂停本身就要它别跑。恢复后本项自动重测。
    paused = _skipped_by_pause("行情任务注册")
    if paused:
        return paused
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return Result("L3", "行情任务注册", WARN, f"查不了 launchctl：{exc}",
                      "在终端手动 `launchctl list | grep trident` 核对")
    registered = {ln.split("\t")[-1].strip() for ln in out.splitlines() if "trident" in ln}
    missing = [j for j in QUOTE_JOBS if j not in registered]
    if missing:
        return Result("L3", "行情任务注册", WARN,
                      f"{len(missing)} 个行情任务未注册：{', '.join(j.replace('com.trident.', '') for j in missing)}",
                      "`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist` 注册回来；"
                      "注册前这些标的价格会一直停在最后一次成功的时间")
    return Result("L3", "行情任务注册", OK, f"{len(QUOTE_JOBS)} 个行情任务都在册", "")


def probe_equity_fresh() -> Result:
    """股票/ETF 快照是否还在长 —— 加密/商品在写不代表股票在写，要分开看。"""
    paused = _skipped_by_pause("股票行情新鲜度")
    if paused:
        return paused
    conn = _ro()
    if conn is None:
        return Result("L3", "股票行情新鲜度", SKIP, "库不可用", "等库恢复")
    try:
        row = conn.execute("SELECT MAX(ts), COUNT(*) FROM market_ticks "
                           "WHERE source = 'tencent-eq'").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return Result("L3", "股票行情新鲜度", SKIP, "还没有股票行情", "等 equity-quote 任务跑起来")
    last = row[0] / 1000 if row[0] > 1e12 else row[0]
    age_min = (time.time() - last) / 60
    stamp = datetime.fromtimestamp(last).strftime("%H:%M:%S")
    if age_min > 15:
        return Result("L3", "股票行情新鲜度", WARN,
                      f"最后一笔股票价停在 {stamp}（{age_min:.0f} 分钟前），共 {row[1]} 条",
                      "eq 任务没跑或全线取价失败：看 ~/.trident/log/equity_quote.log 与行情任务注册")
    return Result("L3", "股票行情新鲜度", OK, f"最后 {stamp}（{age_min:.1f} 分钟前）", "")


PROBES: List[Tuple[str, Callable[[], Result], str]] = [
    ("services", probe_services, "bridge/frontend/master 端口可连"),
    ("db", probe_db, "库可用（不是空壳）"),
    ("decisions", probe_decisions, "决策表有数据"),
    ("entry_price", probe_entry_price, "方向单有建仓价/原因"),
    ("exec_state", probe_exec_state, "方向单必须有账（等字段落地）"),
    ("market", probe_market_fresh, "行情新鲜度"),
    ("prices", probe_prices_live, "取价链路可用且不慢"),
    ("drops", probe_silent_drops, "没有静默丢写"),
    ("change_zero", probe_change_zero, "涨跌幅零值＝未知被写成 0"),
    ("quote_jobs", probe_quote_jobs, "行情采集任务注册在 launchd"),
    ("equity_fresh", probe_equity_fresh, "股票/ETF 快照还在长"),
]


def run_probe(fn: Callable[[], Result]) -> Result:
    try:
        return fn()
    except Exception as exc:
        return Result("L3", getattr(fn, "__name__", "probe"), ERROR, f"{type(exc).__name__}: {exc}"[:200], "")


# ─────────────────────────── 渲染 ───────────────────────────
_COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m",
          SKIP: "\033[36m", ERROR: "\033[35m"}
_RESET = "\033[0m"


def render(results: List[Result], use_color: bool) -> str:
    def c(s: str) -> str:
        return f"{_COLOR.get(s, '')}{s}{_RESET}" if use_color else s

    lines = ["", f"自检 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines.append(f"{'层':<4}{'检查项':<20}{'结果':<7}{'说明'}")
    lines.append("-" * 96)
    for r in results:
        lines.append(f"{r.layer:<4}{r.name:<20}{c(r.status):<7}{r.detail}")
    lines.append("-" * 96)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines.append("汇总：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))

    hints = [r for r in results if r.hint]
    if hints:
        lines += ["", "下一步："]
        for r in hints:
            lines.append(f"  · [{r.name}] {r.hint}")
    skips = [r for r in results if r.status == SKIP]
    if skips:
        lines += ["", "等数据的（不是故障，别修）："]
        for r in skips:
            lines.append(f"  · [{r.name}] {r.detail} → {r.hint}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="加跑 L2 契约测试")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    ap.add_argument("--probe", default="", help="只跑指定探针（逗号分隔，如 db,decisions）")
    ap.add_argument("--no-color", action="store_true")
    args, _ = ap.parse_known_args(argv)

    results: List[Result] = []
    if not args.probe:
        results += [l1_ruff(), l1_compile(), l1_import()]
    if args.full:
        results.append(l2_pytest())

    wanted = {s.strip() for s in args.probe.split(",") if s.strip()}
    for key, fn, _desc in PROBES:
        if wanted and key not in wanted:
            continue
        results.append(run_probe(fn))

    use_color = (not args.no_color) and sys.stdout.isatty()
    print(render(results, use_color))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                       "results": [r.as_dict() for r in results]}, fh,
                      ensure_ascii=False, indent=2)
        print(f"JSON 写入：{args.json}")

    return 1 if any(r.status in BAD for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
