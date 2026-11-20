#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测台（不碰生产库、不依赖外网、不需要真实数据）。

用途：改完代码想验证"交易闭环还通不通"，跑这一条命令即可：
    cd backend && python3 scripts/offline_selftest.py

做法：在临时目录建一个空库，写入明确标注的合成行情（synthetic fixture），
然后按真实代码路径跑：前置硬闸 → 开仓 → 计费 → 重估 → 杠杆闸 → 日亏闸
→ 止损平仓 → 账本对账 → 快照 → 告警 → 实盘硬拒单 → 执行器整轮 tick。

退出码 0 表示全通；非 0 表示有项失败，失败项会打印细节。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
for p in (SRC, BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

import account_ledger as al
import stops
import stops_daemon as sd
import trade_preflight as pf
from trident2 import store as t2store

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def _env(**kw):
    """临时改环境变量，返回恢复函数。"""
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def new_db(tag: str) -> tuple[sqlite3.Connection, str]:
    d = tempfile.mkdtemp(prefix=f"trident_offline_{tag}_")
    path = os.path.join(d, "offline.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    t2store.ensure_schema(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL);
    CREATE TABLE IF NOT EXISTS ai_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER, sentiment_score REAL,
        suggested_action TEXT, reasoning TEXT, created_at TEXT, status TEXT DEFAULT 'UNREAD',
        market_category TEXT DEFAULT 'OTHER', target_asset TEXT DEFAULT 'NONE',
        parent_id INTEGER, child_count INTEGER DEFAULT 0, aggregation_key TEXT DEFAULT '',
        cluster_size INTEGER DEFAULT 1, reasoning_path TEXT DEFAULT '', vip_tag TEXT DEFAULT '',
        doubao_action TEXT DEFAULT 'HOLD', doubao_reasoning TEXT DEFAULT '',
        extra_models_consensus TEXT DEFAULT '', entry_price REAL, exit_price REAL,
        max_price REAL, min_price REAL, is_correct TEXT DEFAULT '',
        settled INTEGER DEFAULT 0, entry_time TEXT DEFAULT '',
        paper_trading_run_id INTEGER DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS account_snapshots (
        ts INTEGER PRIMARY KEY, cash REAL DEFAULT 0, realized_pnl REAL DEFAULT 0,
        unrealized REAL DEFAULT 0, equity REAL DEFAULT 0, gross_exposure REAL DEFAULT 0,
        leverage_pct REAL DEFAULT 0, n_open INTEGER DEFAULT 0, peak_equity REAL DEFAULT 0,
        drawdown_pct REAL DEFAULT 0, note TEXT DEFAULT ''
    );
    """)
    conn.execute("UPDATE t2_account SET cash=10000, equity=10000, realized_pnl=0, fee_bps=8")
    conn.commit()
    return conn, path


def tick(conn, sym: str, price: float, age: int = 0):
    conn.execute("INSERT INTO market_ticks VALUES (?,?,?)", (sym, int(time.time()) - age, price))
    conn.commit()


def open_pos(conn, asset, side, qty, entry, stop=None, strategy="offline_fixture"):
    pid = t2store.insert_position(
        conn, asset=asset, side=side, qty=qty, entry_price=entry,
        stop_price=stop, strategy_id=strategy, subaccount="paper", status="open",
        note="离线自测合成仓（非真实数据）",
    )
    fee = al.book_entry_fee(conn, pid)
    return pid, fee


# ── 1. 前置硬闸 ────────────────────────────────────────────────────────
def s1_preflight(conn):
    restore = _env(MAX_GROSS_LEVERAGE_PCT=200, MAX_OPEN_POSITIONS=5,
                   DAILY_LOSS_LIMIT_USDT=0, PAPER_TRADING_STALE_SECONDS=86400)
    try:
        tick(conn, "BTC", 63800.0)
        out = pf.preflight(conn, "BTC", "BUY", 5000)
        check("S1 空账本放行", out["ok"] is True,
              f"denies={[d['code'] for d in out['denies']]}")

        out2 = pf.preflight(conn, "ZZZ_NOT_A_SYMBOL", "BUY", 5000)
        check("S1 无行情拒单", out2["ok"] is False,
              f"denies={[d['code'] for d in out2['denies']]}")
    finally:
        restore()


# ── 2. 开仓 + 计费 + 权益 ─────────────────────────────────────────────
def s2_fee(conn):
    pid, fee = open_pos(conn, "BTC", "long", 0.0784, 63800.0, stop=60610.0)
    c = al.canonical(conn)
    ok = abs(fee - 4.0) < 0.05 and abs(c["cash"] - 9996.0) < 0.05
    check("S2 开仓计费入账", ok,
          f"持仓#{pid} 费={fee:.2f} cash={c['cash']:.2f} equity={c['equity']:.2f}")
    return pid


# ── 3. 杠杆闸 ─────────────────────────────────────────────────────────
def s3_leverage(conn):
    for a, px in (("XAU", 3730.0), ("WTI", 82.3), ("ETH", 1807.0)):
        tick(conn, a, px)
    open_pos(conn, "XAU", "long", 5000 / 3730.0, 3730.0, stop=3660.0)
    open_pos(conn, "WTI", "long", 5000 / 82.3, 82.3, stop=80.6)
    open_pos(conn, "ETH", "short", 5000 / 1807.0, 1807.0, stop=1832.0)
    c = al.canonical(conn)
    restore = _env(MAX_GROSS_LEVERAGE_PCT=200, MAX_OPEN_POSITIONS=10, DAILY_LOSS_LIMIT_USDT=0)
    try:
        out = pf.preflight(conn, "BTC", "BUY", 500)
    finally:
        restore()
    codes = [d["code"] for d in out["denies"]]
    check("S3 满仓拒新单", "PREFLIGHT_LEVERAGE" in codes,
          f"杠杆={c['leverage_pct']:.0f}% codes={codes}")


# ── 4. 日亏闸 ─────────────────────────────────────────────────────────
def s4_daily_loss(conn):
    # BTC 从 63800 跌到 60000：单笔浮亏约 -298
    tick(conn, "BTC", 60000.0, age=0)
    al.revalue_equity(conn)
    c = al.canonical(conn)
    restore = _env(DAILY_LOSS_LIMIT_USDT=100, MAX_GROSS_LEVERAGE_PCT=400,
                   MAX_OPEN_POSITIONS=10)
    try:
        out = pf.preflight(conn, "XAU", "BUY", 300)
    finally:
        restore()
    codes = [d["code"] for d in out["denies"]]
    check("S4 日亏触发拒单", "PREFLIGHT_DAILY_LOSS" in codes,
          f"equity={c['equity']:.2f} codes={codes}")


# ── 5. 止损平仓 + 净盈亏 ──────────────────────────────────────────────
def s5_stop(conn, pid):
    before = al.canonical(conn)
    closed = stops.enforce(conn)
    row = conn.execute("SELECT * FROM t2_positions WHERE id=?", (pid,)).fetchone()
    after = al.canonical(conn)
    ok = (row["status"] == "closed" and len(closed) == 1
          and row["realized_pnl"] < 0
          and before["realized"] == pytest_zero(before["realized"]))
    check("S5 止损触发平仓", ok,
          f"平 {len(closed)} 笔 · 净盈亏={row['realized_pnl']:.2f} "
          f"费={row['fee_paid']:.2f} realized {before['realized']:.2f}→{after['realized']:.2f}")


def pytest_zero(v: float) -> float:
    return 0.0 if abs(float(v)) < 1e-9 else float(v)


# ── 6. 账本对账 ───────────────────────────────────────────────────────
def s6_reconcile(conn):
    restore = _env(MAX_GROSS_LEVERAGE_PCT=200, MAX_ASSET_WEIGHT_PCT=60)
    try:
        issues = al.reconcile(conn)
    finally:
        restore()
    blocks = [i for i in issues if i.get("severity") == "block"]
    check("S6 对账无 block", not blocks,
          f"warn={[i['code'] for i in issues if i.get('severity') != 'block']} "
          f"block={[i['code'] for i in blocks]}")


# ── 7. 快照 ───────────────────────────────────────────────────────────
def s7_snapshot(conn):
    snap = al.snapshot(conn, apply=True)
    row = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
    check("S7 权益快照落库", row >= 1,
          f"equity={snap.get('equity', 0):.2f} 行数={row}")


# ── 8. 告警去重/恢复 ──────────────────────────────────────────────────
def s8_alerts(conn):
    conn.execute("DELETE FROM t2_alerts")
    a1 = sd.sync_alerts(conn, [{"code": "R1_CASH_MISMATCH", "severity": "block", "detail": "测试"}])
    a2 = sd.sync_alerts(conn, [{"code": "R1_CASH_MISMATCH", "severity": "block", "detail": "测试"}])
    a3 = sd.sync_alerts(conn, [])
    ok = a1["inserted"] == 1 and a2["inserted"] == 0 and a3["resolved"] == 1
    check("S8 告警去重与恢复", ok,
          f"插入={a1['inserted']}/{a2['inserted']} 恢复={a3['resolved']}")


# ── 9. 实盘硬拒单 ─────────────────────────────────────────────────────
def s9_live_refusal():
    from trade_executor import build_default_executor, RefusingAdapter, OrderRequest
    conn, path = new_db("live")
    try:
        ex = build_default_executor(path, env={
            "TRADE_EXECUTOR_MODE": "live_real",
            "BINANCE_NOTIONAL_USDT": "500",
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_TRADING_ACK": "",
            "LIVE_MAX_NOTIONAL_USDT": "1000",
        })
        res = ex.adapter.place_order(OrderRequest(symbol="BTC-USDT", side="BUY", quantity=0.001))
        check("S9 实盘未武装硬拒单",
              isinstance(ex.adapter, RefusingAdapter) and res.ok is False and res.skipped,
              f"adapter={ex.adapter.name} note={getattr(res, 'note', '')}")
    finally:
        conn.close()


# ── 10. 执行器整轮 tick（决策→成交→审计）──────────────────────────────
def s10_executor_tick():
    from trade_executor import build_default_executor
    conn, path = new_db("exec")
    try:
        tick(conn, "BTC", 63800.0)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())
        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
            " created_at, market_category, target_asset, settled, entry_price, aggregation_key)"
            " VALUES (9001, 0.5, 'BUY', '离线自测：合成决策', ?, 'CRYPTO', 'BTC', 0, 63800.0, 'offline')",
            (now_iso,),
        )
        conn.commit()
        ex = build_default_executor(path, env={
            "TRADE_EXECUTOR_MODE": "paper",
            "BINANCE_NOTIONAL_USDT": "5000",
            "MAX_GROSS_LEVERAGE_PCT": "200",
            "MAX_OPEN_POSITIONS": "10",
            "DAILY_LOSS_LIMIT_USDT": "0",
            "WHITELIST_ASSETS": "*",
            "PAPER_INITIAL_EQUITY_USDT": "10000",
        })
        out = ex.tick()
        audit = conn.execute(
            "SELECT status, reason FROM trade_audit_log ORDER BY id DESC LIMIT 1").fetchone()
        pos = conn.execute("SELECT COUNT(*) FROM t2_positions WHERE status='open'").fetchone()[0]
        check("S10 执行器整轮成交",
              pos == 1 and audit is not None and audit["status"] == "ORDER_PLACED",
              f"processed={len(out.get('processed', []))} 持仓={pos} "
              f"audit={audit['status'] if audit else 'none'}")
    finally:
        conn.close()
    _ = conn



# ── 12. 移动止损 / 时间止损 ────────────────────────────────────────────
def s12_trailing_time():
    conn, _ = new_db("trail")
    try:
        tick(conn, "BTC", 100.0, age=400)
        open_pos(conn, "BTC", "long", 1.0, 100.0, stop=None, strategy="offline_trailing")
        conn.execute("UPDATE t2_positions SET entry_ts=?, trailing_bp=200 WHERE id=1",
                     (int(time.time()) - 300,))
        conn.commit()
        tick(conn, "BTC", 110.0, age=60)
        tick(conn, "BTC", 107.0)
        closed = stops.enforce(conn)
        ok1 = len(closed) == 1 and closed[0]["trigger"] == "trailing_stop"
        # 时间止损：另起一笔，价格不动但持仓超过 1 分钟
        tick(conn, "XAU", 3730.0)
        pid2 = t2store.insert_position(conn, asset="XAU", side="long", qty=1.0,
                                       entry_price=3730.0, entry_ts=int(time.time()) - 130,
                                       time_stop_min=1, strategy_id="offline_time",
                                       subaccount="paper", status="open")
        closed2 = stops.enforce(conn)
        ok2 = any(c["position_id"] == pid2 and c["trigger"] == "time_stop" for c in closed2)
        check("S12 移动/时间止损", ok1 and ok2,
              f"trailing={closed[0]['trigger'] if closed else 'none'} "
              f"time={[c['trigger'] for c in closed2]}")
    finally:
        conn.close()


# ── 13. 资金费率 ──────────────────────────────────────────────────────
def s13_funding():
    conn, _ = new_db("fund")
    restore = _env(FUNDING_BPS_PER_8H=10)
    try:
        now = int(time.time())
        # 永续（BTC）要计资金费；现货（WTI）不能凭空扣
        tick(conn, "BTC", 64000)
        pid = t2store.insert_position(conn, asset="BTC", side="long", qty=0.05,
                                      entry_price=64000, entry_ts=now - 8 * 3600,
                                      strategy_id="offline_funding", subaccount="paper",
                                      status="open")
        al.book_entry_fee(conn, pid)
        res = t2store.close_position(conn, pid, exit_price=64000, closed_ts=now)
        expect = 64000 * 0.05 * 10 / 10000.0
        tick(conn, "WTI", 82.3)
        pid2 = t2store.insert_position(conn, asset="WTI", side="long", qty=60.0,
                                       entry_price=82.3, entry_ts=now - 8 * 3600,
                                       strategy_id="offline_funding", subaccount="paper",
                                       status="open")
        al.book_entry_fee(conn, pid2)
        res2 = t2store.close_position(conn, pid2, exit_price=82.3, closed_ts=now)
        ok = (abs(res.get("funding_paid", 0) - expect) < expect * 0.05
              and res["realized_pnl"] < 0 and res2.get("funding_paid", 0) == 0)
        check("S13 资金费入账", ok,
              f"BTC funding={res.get('funding_paid', 0):.3f} 预期≈{expect:.3f} "
              f"WTI funding={res2.get('funding_paid', 0):.3f}（应为 0）")
    finally:
        restore()
        conn.close()


# ── 14. 下单幂等 + 超时撤单 ────────────────────────────────────────────
def s14_idempotency():
    conn, _ = new_db("idem")
    try:
        first = t2store.claim_order(conn, client_order_id="dec7:BTC:BUY", asset="BTC",
                                    side="BUY", notional_usdt=5000, decision_id=7)
        second = t2store.claim_order(conn, client_order_id="dec7:BTC:BUY", asset="BTC",
                                     side="BUY", notional_usdt=5000, decision_id=7)
        conn.execute("UPDATE t2_orders SET ts=? WHERE id=?", (int(time.time()) - 600, first))
        conn.commit()
        n = t2store.cancel_stale_orders(conn, older_than_s=120)
        check("S14 下单幂等与撤单", first > 0 and second is None and n == 1,
              f"首次={first} 重复={second} 撤单={n}")
    finally:
        conn.close()


# ── 15. 交易健康度 JSON ───────────────────────────────────────────────
def s15_health():
    conn, path = new_db("health")
    try:
        tick(conn, "BTC", 63800.0)
        open_pos(conn, "BTC", "long", 0.05, 63800.0, stop=60610.0)
        h = sd.write_health(conn, path)
        ok = h.get("equity", 0) > 0 and isinstance(h.get("blocks"), list)
        check("S15 健康度 JSON", ok,
              f"equity={h.get('equity')} 杠杆={h.get('leverage_pct')} blocks={h.get('blocks')}")
    finally:
        conn.close()



# ── 16. 交易健康度 API / 面板 ─────────────────────────────────────────
def s16_health_api():
    import trading_health as th
    conn, path = new_db("api")
    try:
        tick(conn, "BTC", 63800.0)
        open_pos(conn, "BTC", "long", 0.05, 63800.0, stop=60610.0)
    finally:
        conn.close()
    h = th.build(path)
    api_route = "api/trading/health"
    route_file = os.path.join(SRC, "api_server.py")
    has_route = os.path.exists(route_file) and api_route in open(route_file, encoding="utf-8").read()
    page = os.path.join(BACKEND, "..", "frontend", "public", "trading-health.html")
    ok = h.get("ok") is True and len(h.get("positions", [])) == 1 and has_route and os.path.exists(page)
    check("S16 健康度接口/面板", ok,
          f"ok={h.get('ok')} 持仓={len(h.get('positions', []))} "
          f"路由={has_route} 页面={os.path.exists(page)}")

def s11_docs_seam():
    """验收口是否就位：检查脚本 + 文档 + 回放口（导出/回放两支）+ 示例 bundle。"""
    acc = os.path.join(BACKEND, "scripts", "acceptance_check.py")
    doc = os.path.join(BACKEND, "..", "docs", "离线自测与验收口.md")
    replay = os.path.join(BACKEND, "scripts", "replay_bundle.py")
    export = os.path.join(BACKEND, "scripts", "export_bundle.py")
    sample = os.path.join(BACKEND, "tests", "fixtures", "offline_bundle_example.json")
    ok = all(map(os.path.exists, (acc, doc, replay, export, sample)))
    check("S11 验收口就位", ok,
          f"acceptance_check={os.path.exists(acc)} doc={os.path.exists(doc)} "
          f"replay={os.path.exists(replay)} export={os.path.exists(export)} "
          f"示例bundle={os.path.exists(sample)}")


# ══════════════════════════════════════════════════════════════════════
# 决策之后的链路（S17-S24）
#
# S1-S16 只证明"钱和单子"这一层是对的。分析层之后还有四段没人测过：
# 反思能不能从成交里长出来、成绩单能不能结算、两套账本会不会被混算、
# 事件能不能反查到时刻。这四段恰好是"要真数据才看得出合不合理"的部分，
# 所以这里先用合成数据把**代码路径**和**口径**钉死；真数据留给
# scripts/replay_bundle.py（同一套检查、换数据源）。
# ══════════════════════════════════════════════════════════════════════

_EXTRA_DDL = (
    "CREATE TABLE IF NOT EXISTS ohlcv_intraday (asset TEXT, interval TEXT, ts INTEGER, "
    "open REAL, high REAL, low REAL, close REAL, volume REAL, trades INTEGER, "
    "closed INTEGER, source TEXT);"
    "CREATE TABLE IF NOT EXISTS critical_price_moves (id INTEGER PRIMARY KEY, asset TEXT, "
    "ts INTEGER, start_price REAL, peak_price REAL, end_price REAL, move_pct REAL, "
    "move_kind TEXT, duration_min INTEGER, algo_score REAL, context_json TEXT, created_at TEXT);"
)


def full_db(tag: str) -> str:
    """建一个和生产结构一致的临时库（db.migrate + 采集侧的两张表 + t2）。"""
    import db as _db
    d = tempfile.mkdtemp(prefix=f"trident_chain_{tag}_")
    path = os.path.join(d, "chain.db")
    conn = sqlite3.connect(path)
    _db.migrate(conn)
    conn.executescript(_EXTRA_DDL)
    t2store.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


class db_switch:
    """把 config.DB_PATH 临时指到测试库（模块内的 db.get_connection 都看它）。"""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        import config
        self._old = config.DB_PATH
        self._old_env = os.environ.get("TRIDENT_DB_PATH")
        config.DB_PATH = self.path
        os.environ["TRIDENT_DB_PATH"] = self.path
        return self.path

    def __exit__(self, *exc):
        import config
        config.DB_PATH = self._old
        if self._old_env is None:
            os.environ.pop("TRIDENT_DB_PATH", None)
        else:
            os.environ["TRIDENT_DB_PATH"] = self._old_env
        return False


def _settled_pair(conn, decision_id: int, asset: str, action: str, pnl_pct: float,
                  pnl_usdt: float, ts: int, *, degraded: int = 0, verdict: str = "LOSS",
                  news_id: int = 9001):
    """一条决策 + 一笔已结算成交（两条表的口径差异在这里一次对齐）。"""
    conn.execute(
        "INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action, reasoning,"
        " created_at, status, market_category, target_asset, ts, settled, is_correct,"
        " analysis_degraded, paper_trading_run_id, completeness_score, code_fingerprint,"
        " entry_price, exit_price, mfe_pct, mae_pct)"
        " VALUES (?,?,0.6,?, '离线自测：合成决策（非真实数据）', ?, 'UNREAD','CRYPTO',?,?,1,?,?,1,0.9,"
        " 'offline:v498',60000,59000,1.5,-3.0)",
        (decision_id, news_id, action,
         time.strftime("%Y-%m-%d %H:%M:%S"), asset, ts, verdict, degraded),
    )
    conn.execute(
        "INSERT INTO quick_sim_trades (decision_id, news_id, asset, action, score, gate_passed,"
        " gate_reason, strategy_id, notional_usdt, entry_price, entry_ts, horizon_minutes,"
        " exit_price, exit_ts, pnl_pct, pnl_usdt, verdict, settled, created_at, mfe_pct, mae_pct,"
        " cost_bps, trade_uuid)"
        " VALUES (?,?,?,?,0.6,1,'ok','offline',1000,60000,?,240,59000,?,?,?,?,1,?,1.5,-3.0,8,?)",
        (decision_id, news_id, asset, action, ts, ts + 240 * 60, pnl_pct, pnl_usdt,
         verdict, time.strftime("%Y-%m-%d %H:%M:%S"), f"offline-uuid-{decision_id}"),
    )
    conn.commit()


def s17_reflection_chain():
    """反思能不能从成交里长出来，而且带本笔数字 + 可执行动作（v497 的验收点）。"""
    import paper_reflection_worker as prw
    path = full_db("reflect")
    try:
        with db_switch(path):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            ts = int(time.time()) - 5 * 3600
            _settled_pair(conn, 71, "BTC", "BUY", -1.25, -12.5, ts)
            row = conn.execute(prw._DEDUPE_SELECT).fetchone()
            conn.close()
            if row is None:
                check("S17 反思三件套", False, "合成成交没有进 _DEDUPE_SELECT（口径断在这里）")
                return
            rid = prw._insert_one(dict(row))
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rf = conn.execute("SELECT * FROM trade_reflections_v2 WHERE decision_id=71").fetchone()
            dr = conn.execute("SELECT * FROM decision_reflection ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        ok = (bool(rid) and rf is not None and dr is not None
              and float(rf["actual_outcome_pct"]) == -1.25
              and "本笔" in (dr["bias_text"] or "")
              and "%" in (dr["bias_text"] or "")
              and bool((dr["actionable_suggestion"] or "").strip())
              and (rf["dimension"] or "") != "")
        check("S17 反思三件套", ok,
              f"rid={rid} 维度={rf['dimension'] if rf else None} "
              f"结果={rf['actual_outcome_pct'] if rf else None} "
              f"教训={((dr['bias_text'] or '')[:38] + '…') if dr else '无'}")
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def s18_degraded_skip():
    """LLM 全挂的降级决策不能产出洞察（否则"没看见"会被当成教训写进下次 prompt）。"""
    import paper_reflection_worker as prw
    path = full_db("degraded")
    try:
        with db_switch(path):
            conn = sqlite3.connect(path)
            ts = int(time.time()) - 5 * 3600
            _settled_pair(conn, 81, "BTC", "BUY", -0.9, -9.0, ts)
            _settled_pair(conn, 82, "ETH", "SELL", -1.1, -11.0, ts, degraded=1)
            conn.close()
            inserted = prw.main()
            conn = sqlite3.connect(path)
            got = [r[0] for r in conn.execute(
                "SELECT decision_id FROM trade_reflections_v2 ORDER BY decision_id")]
            conn.close()
        ok = inserted == 1 and got == [81]
        check("S18 降级决策不产洞察", ok,
              f"本轮写入={inserted} 有反思的决策={got}（82 是降级，应缺席）")
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def s19_settler():
    """成绩单结算：WIN/LOSS/HOLD 都落行，冻结窗口不许冒充"没波动"。"""
    sys.path.insert(0, os.path.join(SRC, "signals"))
    import signal_performance_settler as sps
    path = full_db("settle")
    try:
        conn = sqlite3.connect(path)
        now = int(time.time())
        dts = now - 6 * 3600
        # 上升序列 → BUY 应判 WIN；同一段 HOLD 记原始位移（不是 0）
        for i in range(0, 26):
            px = 60000 + i * 500
            conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','10m',?,?,?,?,?,100,10,1,'offline')",
                         (dts + i * 600, px, px + 80, px - 80, px))
        # 冻结序列（high==low 且价格不动）→ 应被 frozen_window 挡掉
        for i in range(0, 26):
            conn.execute("INSERT INTO ohlcv_intraday VALUES ('ETH','10m',?,?,?,?,?,100,10,1,'offline')",
                         (dts + i * 600, 1800, 1800, 1800, 1800))
        for did, asset, action in ((91, "BTC", "BUY"), (92, "BTC", "HOLD"), (93, "ETH", "BUY")):
            conn.execute(
                "INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action, reasoning,"
                " created_at, status, target_asset, ts, settled)"
                " VALUES (?,9001,0.5,?,'离线自测：合成决策（非真实数据）',?,'UNREAD',?,?,0)",
                (did, action, time.strftime("%Y-%m-%d %H:%M:%S"), asset, dts),
            )
        conn.commit()
        out = sps.settle(conn, apply=True, now=now)
        rows = {r[0]: r for r in conn.execute(
            "SELECT decision_id, action, is_correct, forward_pnl, mfe_pct, mae_pct"
            " FROM signal_performance")}
        conn.close()
        buy = rows.get(91)
        hold = rows.get(92)
        ok = (buy is not None and buy[2] == "WIN"
              and hold is not None and hold[2] == "HOLD" and abs(hold[3]) > 0
              and 93 not in rows
              and out["stats"].get("frozen_window", 0) >= 1)
        check("S19 成绩单结算", ok,
              f"BUY={buy[2] if buy else None} pnl={buy[3] if buy else None}% "
              f"HOLD={hold[2] if hold else None} pnl={hold[3] if hold else None}% "
              f"冻结跳过={out['stats'].get('frozen_window', 0)}")
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def s20_two_ledgers():
    """两套账本必须并列且不能相加：quick_sim 只作提示，不进 T2 权益。"""
    import account_ledger as al
    path = full_db("ledger")
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE t2_account SET cash=10000, equity=10000, realized_pnl=0, fee_bps=8")
        ts = int(time.time()) - 3600
        # 先插决策：库里 trg_quick_sim_reject_orphan 会把"无对应决策"的成交直接丢掉，
        # 这本身也是要保住的不变量（下面顺带验一条孤儿成交被拒）。
        conn.execute("INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action,"
                     " reasoning, created_at, status, target_asset, ts, settled)"
                     " VALUES (91,9001,0.5,'BUY','离线自测：合成决策（非真实数据）',?, 'UNREAD','BTC',?,1)",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), ts))
        conn.execute("INSERT INTO quick_sim_trades (decision_id, asset, action, entry_price,"
                     " entry_ts, horizon_minutes, pnl_usdt, settled, created_at)"
                     " VALUES (91,'BTC','BUY',60000,?,240,-12.5,1,'offline')", (ts,))
        conn.execute("INSERT INTO sim_equity_snapshots VALUES (?,10000,-12.5,1,0,10000,0,'offline')",
                     (ts,))
        conn.commit()
        # 孤儿成交（决策 999 不存在）必须被触发器挡掉
        conn.execute("INSERT INTO quick_sim_trades (decision_id, asset, action, entry_price,"
                     " entry_ts, horizon_minutes, pnl_usdt, settled, created_at)"
                     " VALUES (999,'BTC','BUY',60000,?,240,-5.0,1,'offline')", (ts,))
        conn.commit()
        n_trades = conn.execute("SELECT COUNT(*) FROM quick_sim_trades").fetchone()[0]
        issues = al.reconcile(conn)
        equity = al.canonical(conn)["equity"]
        conn.close()
        codes = [i["code"] for i in issues]
        ok = ("R10_TWO_TRACKS" in codes and abs(equity - 10000.0) < 0.01 and n_trades == 1)
        check("S20 双账本不相加", ok,
              f"提示={[c for c in codes if c.startswith('R10')]} T2权益={equity:.2f}（应仍是 10000）"
              f" 孤儿成交被拒={n_trades == 1}")
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def s21_event_reverse():
    """时刻 → 事件反查：同品种能命中、跨品种要排除、空库不崩。"""
    import event_reverse_lookup as erl
    path = full_db("reverse")
    try:
        conn = sqlite3.connect(path)
        t0 = int(time.time()) - 3 * 3600
        conn.execute("INSERT INTO critical_price_moves (asset, ts, start_price, peak_price,"
                     " end_price, move_pct, move_kind, duration_min, algo_score, created_at)"
                     " VALUES ('BTC',?,64000,67200,63500,-2.1,'flash',30,0.8,'offline')", (t0,))
        conn.execute("INSERT INTO news_events (id, news_id, ts, source, headline, assets, score,"
                     " is_noise, status) VALUES (101,9001,?,'wire','比特币ETF遭大额赎回，BTC 短线跳水',"
                     " 'BTC',0.72,0,'ok')", (t0 - 300,))
        conn.execute("INSERT INTO news_events (id, news_id, ts, source, headline, assets, score,"
                     " is_noise, status) VALUES (102,9002,?,'wire','以太坊升级完成','ETH',0.5,0,'ok')",
                     (t0 - 300,))
        conn.commit()
        res = erl.lookup(conn, "BTC", t0, lookback_sec=7200, lookahead_sec=3600)
        conn.close()
        ids = [h["event_id"] for h in res["events"]]
        ok = res["event_count"] == 1 and ids == [101] and res["moves"][0]["move_pct"] == -2.1
        check("S21 事件反查", ok, f"命中={ids}（102 是 ETH，应被排除） 异动={len(res['moves'])}")
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    # 空库 / 缺表必须返回空结果而不是抛错
    import tempfile as _tf
    d = _tf.mkdtemp()
    try:
        conn = sqlite3.connect(os.path.join(d, "empty.db"))
        res = erl.lookup(conn, "BTC", int(time.time()))
        conn.close()
        check("S21 反查空库不崩", res["event_count"] == 0 and res["moves"] == [],
              f"空库返回 events={res['event_count']} moves={len(res['moves'])}")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def s22_quota_classify():
    """402 是额度耗尽（quota 熔断），401/403 才是 key 失效（auth 熔断）。"""
    import model_probe
    try:
        import realtime_filter as _rt  # noqa: F401  确保模块可导入（分类逻辑在调用链里）
        importable = True
    except Exception:
        importable = False
    q = model_probe._classify_probe_failure(402)
    a1 = model_probe._classify_probe_failure(401)
    a2 = model_probe._classify_probe_failure(403)
    ok = importable and q == "quota_exhausted" and a1 == "auth" and a2 == "auth"
    check("S22 402/401 分类", ok, f"402={q} 401={a1} 403={a2}")


def s23_replay_bundle():
    """数据回放口：示例 bundle 能装载、能出结论（真数据就换这个文件）。"""
    sys.path.insert(0, os.path.join(BACKEND, "scripts"))
    import replay_bundle as rb
    sample = os.path.join(BACKEND, "tests", "fixtures", "offline_bundle_example.json")
    report = rb.run(sample)
    load = report["load"]
    sections = report["analysis"]["sections"]
    names = [s["name"] for s in sections]
    ok = (load["ai_decisions"]["loaded"] == 3
          and load["quick_sim_trades"]["loaded"] == 2
          and load["t2_positions"]["loaded"] == 2
          and "反思闭环" in names and "双账本对账" in names
          and all(s["verdict"] for s in sections))
    check("S23 数据回放口", ok,
          "装载=" + ", ".join(f"{k}:{v.get('loaded', 0)}" for k, v in load.items())
          + f" 结论段={len(sections)}")


def s24_hold_no_order():
    """HOLD / 无方向决策不进执行器 —— 决策层空转不能变成成交。"""
    from trade_executor import build_default_executor
    conn, path = new_db("hold")
    try:
        tick(conn, "BTC", 63800.0)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())
        conn.execute(
            "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
            " created_at, market_category, target_asset, settled, entry_price, aggregation_key)"
            " VALUES (9101, 0.2, 'HOLD', '离线自测：合成 HOLD', ?, 'CRYPTO', 'BTC', 0, 63800.0, 'offline')",
            (now_iso,),
        )
        conn.commit()
        ex = build_default_executor(path, env={
            "TRADE_EXECUTOR_MODE": "paper",
            "BINANCE_NOTIONAL_USDT": "5000",
            "MAX_GROSS_LEVERAGE_PCT": "200",
            "MAX_OPEN_POSITIONS": "10",
            "DAILY_LOSS_LIMIT_USDT": "0",
            "WHITELIST_ASSETS": "*",
            "PAPER_INITIAL_EQUITY_USDT": "10000",
        })
        out = ex.tick()
        pos = conn.execute("SELECT COUNT(*) FROM t2_positions WHERE status='open'").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM trade_audit_log").fetchone()[0]
        ok = pos == 0 and audits == 0 and not out.get("processed")
        check("S24 HOLD 不成交", ok,
              f"processed={len(out.get('processed') or [])} 持仓={pos} 审计行={audits}")
    finally:
        conn.close()


def s25_manual_override():
    """人工调权重：乘法生效 → explain 留痕 → 撤销后回到原值。

    这是 v499 的核心闭环：接口只是壳，真正要证明的是"人工系数确实进了决策落库
    的那一列"。所以这里走真实路径 evaluate_and_persist，查 ai_decisions 里的
    signal_weight / weight_explain，而不是只看 helper 返回值。
    """
    from signals import signal_weight_evaluator as swe
    path = full_db("override")
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        ts = int(time.time())
        conn.execute(
            "INSERT INTO ai_decisions (id, news_id, sentiment_score, suggested_action,"
            " reasoning, created_at, status, target_asset, ts, settled)"
            " VALUES (201,9001,0.72,'BUY','离线自测：合成决策（非真实数据）',?,'UNREAD','BTC',?,0)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), ts),
        )
        conn.commit()
        base = swe.evaluate_and_persist(conn, 201, trigger_kind="insert", now_ts=ts)
        w0 = float(base["signal_weight"])
        swe.ensure_override_table(conn)
        oid = conn.execute(
            "INSERT INTO signal_weight_overrides (asset, action, multiplier, reason, actor,"
            " created_at, active) VALUES ('BTC','',1.5,'离线自测：回撤期压低信号','offline',?,1)",
            (ts,),
        ).lastrowid
        conn.commit()
        boosted = swe.evaluate_and_persist(conn, 201, trigger_kind="manual", now_ts=ts)
        row = conn.execute("SELECT signal_weight, weight_explain FROM ai_decisions "
                           "WHERE id=201").fetchone()
        conn.execute("UPDATE signal_weight_overrides SET active=0, revoked_at=? WHERE id=?",
                     (ts, oid))
        conn.commit()
        back = swe.evaluate_and_persist(conn, 201, trigger_kind="manual", now_ts=ts)
        ok = (abs(float(boosted["signal_weight"]) - min(1.0, w0 * 1.5)) < 1e-6
              and "manual_override ×1.50" in (boosted["explain"] or "")
              and abs(float(row["signal_weight"]) - float(boosted["signal_weight"])) < 1e-6
              and "manual_override" in (row["weight_explain"] or "")
              and abs(float(back["signal_weight"]) - w0) < 1e-6)
        check("S25 人工调权重", ok,
              f"原值={w0:.4f} 覆盖后={float(boosted['signal_weight']):.4f}"
              f" 撤销后={float(back['signal_weight']):.4f}")
        conn.close()
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def main() -> int:
    conn, _path = new_db("main")
    try:
        s1_preflight(conn)
        pid = s2_fee(conn)
        s3_leverage(conn)
        s4_daily_loss(conn)
        s5_stop(conn, pid)
        s6_reconcile(conn)
        s7_snapshot(conn)
        s8_alerts(conn)
    finally:
        conn.close()
    s9_live_refusal()
    s10_executor_tick()
    s12_trailing_time()
    s13_funding()
    s14_idempotency()
    s15_health()
    s16_health_api()
    s11_docs_seam()
    s17_reflection_chain()
    s18_degraded_skip()
    s19_settler()
    s20_two_ledgers()
    s21_event_reverse()
    s22_quota_classify()
    s23_replay_bundle()
    s24_hold_no_order()
    s25_manual_override()

    width = max(len(r["name"]) for r in RESULTS) + 2
    print("\n离线自测结果")
    print("-" * (width + 12))
    for r in RESULTS:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']:<{width}} {r['detail']}")
    bad = [r for r in RESULTS if not r["ok"]]
    print("-" * (width + 12))
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
    if bad:
        print("\n失败详情：")
        for r in bad:
            print(json.dumps(r, ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
