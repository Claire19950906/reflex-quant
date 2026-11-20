# -*- coding: utf-8 -*-
"""功能层存储 —— 结果、告警、纸面账本、改动/干预/成本/快照。

所有表以 ``t2_`` 前缀独立，不改动既有 156 张表的语义。
建表用 ``CREATE TABLE IF NOT EXISTS``，可重复执行。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .types import DEFAULT_COOLDOWN_SEC, FeatureResult, SEVERITY_TO_LEVEL

SCHEMA: List[str] = [
    """CREATE TABLE IF NOT EXISTS t2_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feature_id TEXT NOT NULL,
        run_ts INTEGER NOT NULL,
        status TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        headline TEXT,
        value_json TEXT,
        evidence_json TEXT,
        anchor_ts INTEGER,
        sample_size INTEGER,
        action_json TEXT,
        notes_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_results_fid ON t2_results(feature_id, run_ts)",
    """CREATE TABLE IF NOT EXISTS t2_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feature_id TEXT NOT NULL,
        level TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        title TEXT,
        detail_json TEXT,
        created_at INTEGER NOT NULL,
        resolved_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_alerts_dedupe ON t2_alerts(feature_id, dedupe_key, created_at)",
    """CREATE TABLE IF NOT EXISTS t2_state (
        key TEXT PRIMARY KEY,
        value_json TEXT,
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_param_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        old_json TEXT,
        new_json TEXT,
        actor TEXT,
        note TEXT,
        perf_24h_json TEXT,
        perf_checked_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_account (
        id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL DEFAULT 'paper',
        cash REAL NOT NULL DEFAULT 0,
        equity REAL NOT NULL DEFAULT 0,
        realized_pnl REAL NOT NULL DEFAULT 0,
        fee_bps REAL NOT NULL DEFAULT 8,
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        side TEXT NOT NULL,
        qty REAL NOT NULL,
        entry_price REAL NOT NULL,
        entry_ts INTEGER NOT NULL,
        stop_price REAL,
        target_price REAL,
        time_stop_min INTEGER,
        trailing_bp REAL,
        thesis_json TEXT,
        strategy_id TEXT,
        subaccount TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        closed_ts INTEGER,
        exit_price REAL,
        realized_pnl REAL,
        fee_paid REAL NOT NULL DEFAULT 0,
        funding_paid REAL NOT NULL DEFAULT 0,
        gross_pnl REAL,
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_positions_status ON t2_positions(status, asset)",
    """CREATE TABLE IF NOT EXISTS t2_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        asset TEXT NOT NULL,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        qty REAL,
        notional_usdt REAL,
        price REAL,
        stop_price REAL,
        target_price REAL,
        oco_group TEXT,
        time_in_force TEXT,
        status TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'dry_run',
        decision_id INTEGER,
        parent_position_id INTEGER,
        filled_price REAL,
        filled_qty REAL,
        note TEXT,
        client_order_id TEXT,
        updated_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_orders_status ON t2_orders(status, asset)",
    """CREATE TABLE IF NOT EXISTS t2_interventions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        target TEXT,
        note TEXT,
        before_json TEXT,
        after_json TEXT,
        pnl_1h_pct REAL,
        pnl_1d_pct REAL,
        counterfactual_1h_pct REAL,
        counterfactual_1d_pct REAL,
        settled INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_cost_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        day TEXT NOT NULL,
        strategy_id TEXT,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USDT',
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_cost_day ON t2_cost_ledger(day, category)",
    """CREATE TABLE IF NOT EXISTS t2_decision_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL UNIQUE,
        ts INTEGER NOT NULL,
        market_json TEXT,
        news_json TEXT,
        prompt_hash TEXT,
        model TEXT,
        code_version TEXT,
        captured_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_eval_set (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_ts INTEGER NOT NULL,
        asset TEXT,
        headline TEXT,
        expected_json TEXT,
        prompt_id TEXT,
        output_json TEXT,
        run_id TEXT,
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_source_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        window_label TEXT NOT NULL,
        sample_n INTEGER NOT NULL,
        hit_n INTEGER NOT NULL,
        avg_pnl_pct REAL,
        avg_latency_min REAL,
        score REAL,
        updated_at INTEGER,
        UNIQUE(source, window_label)
    )""",
    """CREATE TABLE IF NOT EXISTS t2_subaccounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        capital REAL NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'USDT',
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS t2_anomaly_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        asset TEXT NOT NULL,
        kind TEXT NOT NULL,
        anchor_ts INTEGER,
        close REAL,
        baseline_json TEXT,
        evidence_json TEXT,
        pct_1h REAL,
        pct_4h REAL,
        pct_1d REAL,
        settled INTEGER NOT NULL DEFAULT 0,
        settled_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_anomaly_asset ON t2_anomaly_snapshots(asset, ts)",
    """CREATE TABLE IF NOT EXISTS t2_event_backfill (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id TEXT NOT NULL,
        asset TEXT NOT NULL,
        event_ts INTEGER NOT NULL,
        action TEXT,
        entry_price REAL,
        pct_1h REAL,
        pct_4h REAL,
        pct_24h REAL,
        settled INTEGER NOT NULL DEFAULT 0,
        filled_at INTEGER,
        UNIQUE(news_id, asset)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_event_backfill_ts ON t2_event_backfill(event_ts, settled)",
    """CREATE TABLE IF NOT EXISTS t2_sim_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        decision_id INTEGER,
        news_id TEXT,
        asset TEXT NOT NULL,
        action TEXT NOT NULL,
        confidence REAL,
        entry_ts INTEGER NOT NULL,
        entry_price REAL NOT NULL,
        exit_ts INTEGER,
        exit_price REAL,
        horizon_min INTEGER NOT NULL DEFAULT 20,
        pnl_pct REAL,
        cost_bps REAL NOT NULL DEFAULT 30,
        strategy_id TEXT,
        settled INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER,
        UNIQUE(source, decision_id, news_id, asset, entry_ts)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_t2_sim_trades_asset ON t2_sim_trades(asset, entry_ts)",
    "CREATE INDEX IF NOT EXISTS idx_t2_sim_trades_src ON t2_sim_trades(source, settled)",
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in SCHEMA:
        conn.execute(ddl)
    conn.commit()
    # v476：手续费/毛利两列，老库幂等 ALTER（建表语句已包含，重复执行无副作用）。
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t2_positions)")}
        if "fee_paid" not in cols:
            conn.execute("ALTER TABLE t2_positions ADD COLUMN fee_paid REAL NOT NULL DEFAULT 0")
        if "gross_pnl" not in cols:
            conn.execute("ALTER TABLE t2_positions ADD COLUMN gross_pnl REAL")
        if "funding_paid" not in cols:
            conn.execute("ALTER TABLE t2_positions ADD COLUMN funding_paid REAL NOT NULL DEFAULT 0")
        ocols = {r[1] for r in conn.execute("PRAGMA table_info(t2_orders)")}
        if "client_order_id" not in ocols and ocols:
            conn.execute("ALTER TABLE t2_orders ADD COLUMN client_order_id TEXT")
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_t2_orders_client "
                         "ON t2_orders(client_order_id) "
                         "WHERE client_order_id IS NOT NULL AND client_order_id<>''")
        except sqlite3.Error:
            pass
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[T2-POSITIONS] 手续费列迁移失败（不阻断）: {type(exc).__name__}: {str(exc)[:80]}",
              flush=True)
    try:
        # v464(P0)：账户初始权益必须是真实起始本金，不能是 0。
        # 之前写 0 会让组合闸的 equity<=0 分支直接拒绝所有新单，或者绕过杠杆上限
        # （持仓 25,000 名义 vs 本金 10,000），复盘战绩永远失真。
        try:
            start_equity = float(os.getenv("PAPER_INITIAL_EQUITY_USDT", "10000") or 10000)
        except ValueError:
            start_equity = 10000.0
        conn.execute(
            "INSERT OR IGNORE INTO t2_account (id, mode, cash, equity, realized_pnl, fee_bps, updated_at) "
            "VALUES (1, 'paper', ?, ?, 0, 8, ?)",
            (start_equity, start_equity, int(time.time())),
        )
        conn.commit()
    except sqlite3.Error as exc:
        # v399：允许失败，但不许不留痕（v322 审计门禁要求）
        print(f"[T2-ACCOUNT] t2_account 初始化写库失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)


def _dump(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    try:
        row = conn.execute("SELECT value_json FROM t2_state WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO t2_state (key, value_json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
        (key, _dump(value), int(time.time())),
    )
    conn.commit()


def account(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM t2_account WHERE id=1").fetchone()
    return dict(row) if row else {}


def update_account(conn: sqlite3.Connection, **fields: Any) -> Dict[str, Any]:
    if not fields:
        return account(conn)
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE t2_account SET {cols} WHERE id=1", tuple(fields.values()))
    conn.commit()
    return account(conn)


# ── 结果与告警 ──────────────────────────────────────────────────────────
def record_result(conn: sqlite3.Connection, result: FeatureResult) -> int:
    run_ts = int(result.run_ts or time.time())
    conn.execute(
        "INSERT INTO t2_results (feature_id, run_ts, status, severity, headline, value_json, "
        "evidence_json, anchor_ts, sample_size, action_json, notes_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            result.feature_id, run_ts, result.status, result.severity, result.headline,
            _dump(result.value), _dump(result.evidence), result.anchor_ts,
            result.sample_size, _dump(result.action), _dump(result.notes),
        ),
    )
    conn.commit()
    return run_ts


def recent_results(conn: sqlite3.Connection, feature_id: Optional[str] = None,
                   limit: int = 50) -> List[Dict[str, Any]]:
    if feature_id:
        rows = conn.execute(
            "SELECT * FROM t2_results WHERE feature_id=? ORDER BY id DESC LIMIT ?",
            (feature_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM t2_results ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for c in ("value_json", "evidence_json", "action_json", "notes_json"):
            if isinstance(d.get(c), str):
                try:
                    d[c] = json.loads(d[c])
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(d)
    return out


def latest_by_feature(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT feature_id, MAX(id) AS mid FROM t2_results GROUP BY feature_id"
    ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = conn.execute("SELECT * FROM t2_results WHERE id=?", (r[1],)).fetchone()
        if d:
            item = dict(d)
            for c in ("value_json", "evidence_json", "action_json", "notes_json"):
                if isinstance(item.get(c), str):
                    try:
                        item[c] = json.loads(item[c])
                    except (json.JSONDecodeError, TypeError):
                        pass
            out[r[0]] = item
    return out


def emit_alert(conn: sqlite3.Connection, result: FeatureResult,
               dedupe_key: Optional[str] = None, now: Optional[int] = None,
               cooldown_sec: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """按 F35 规则发告警：severity → P1/P2/P3，同级同类冷却窗口去重。"""
    level = SEVERITY_TO_LEVEL.get(result.severity)
    if level is None:
        return None
    now_ts = int(now or time.time())
    key = dedupe_key or f"{result.feature_id}:{result.headline[:40]}"
    window = cooldown_sec if cooldown_sec is not None else DEFAULT_COOLDOWN_SEC.get(level, 1800)
    last = conn.execute(
        "SELECT id, created_at FROM t2_alerts WHERE feature_id=? AND dedupe_key=? "
        "ORDER BY id DESC LIMIT 1", (result.feature_id, key),
    ).fetchone()
    if last and now_ts - int(last[1]) < window:
        return None
    conn.execute(
        "INSERT INTO t2_alerts (feature_id, level, dedupe_key, title, detail_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (result.feature_id, level, key, result.headline, _dump(result.to_dict()), now_ts),
    )
    conn.commit()
    return {"feature_id": result.feature_id, "level": level, "title": result.headline,
            "created_at": now_ts, "cooldown_sec": window}


def alerts(conn: sqlite3.Connection, level: Optional[str] = None,
           limit: int = 100) -> List[Dict[str, Any]]:
    if level:
        rows = conn.execute(
            "SELECT * FROM t2_alerts WHERE level=? ORDER BY id DESC LIMIT ?", (level, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM t2_alerts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ── 纸面账本 ────────────────────────────────────────────────────────────
def insert_order(conn: sqlite3.Connection, **kw: Any) -> int:
    allowed = {
        "ts", "asset", "side", "order_type", "qty", "notional_usdt", "price", "stop_price",
        "target_price", "oco_group", "time_in_force", "status", "mode", "decision_id",
        "parent_position_id", "filled_price", "filled_qty", "note",
    }
    data = {k: v for k, v in kw.items() if k in allowed}
    data.setdefault("ts", int(time.time()))
    data.setdefault("status", "pending")
    data.setdefault("mode", "dry_run")
    data["updated_at"] = int(time.time())
    cols = ", ".join(data.keys())
    marks = ",".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO t2_orders ({cols}) VALUES ({marks})", tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def update_order(conn: sqlite3.Connection, order_id: int, **fields: Any) -> None:
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE t2_orders SET {cols} WHERE id=?", (*fields.values(), int(order_id)))
    conn.commit()


def claim_order(conn: sqlite3.Connection, *, client_order_id: str, asset: str, side: str,
                order_type: str = "MARKET", notional_usdt: float = 0.0,
                price: float = 0.0, mode: str = "paper",
                decision_id: Optional[int] = None,
                note: str = "") -> Optional[int]:
    """幂等占位：同一个 client_order_id 只允许成功一次。

    返回新建的 order id；如果已存在（重复决策/重启重放）返回 None，调用方应跳过下单。
    表没有 client_order_id 列（老库尚未迁移）时退化为总是允许，返回 -1 表示"未跟踪"。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t2_orders)")}
    if "client_order_id" not in cols:
        return -1
    now = int(time.time())
    try:
        cur = conn.execute(
            "INSERT INTO t2_orders (ts, asset, side, order_type, qty, notional_usdt, price,"
            " status, mode, decision_id, client_order_id, note, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, asset, side.lower(), order_type, 0.0, float(notional_usdt), float(price),
             "PENDING", mode, decision_id, client_order_id, note, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def update_order_status(conn: sqlite3.Connection, order_id: int, status: str,
                        filled_price: Optional[float] = None,
                        filled_qty: Optional[float] = None,
                        note: Optional[str] = None) -> None:
    if order_id in (None, "", 0) or int(order_id) <= 0:
        return
    fields: Dict[str, Any] = {"status": status}
    if filled_price is not None:
        fields["filled_price"] = float(filled_price)
    if filled_qty is not None:
        fields["filled_qty"] = float(filled_qty)
        fields["qty"] = float(filled_qty)
    if note is not None:
        fields["note"] = note
    update_order(conn, int(order_id), **fields)


def cancel_stale_orders(conn: sqlite3.Connection, older_than_s: float = 120.0) -> int:
    """超时未成交的挂单置 CANCELLED（纸面瞬时成交，正常应恒为 0；实盘用得上）。"""
    cutoff = int(time.time() - float(older_than_s))
    rows = conn.execute(
        "SELECT id FROM t2_orders WHERE status IN ('PENDING','pending') AND ts<?", (cutoff,)
    ).fetchall()
    for r in rows:
        update_order(conn, int(r["id"]), status="CANCELLED", note="timeout")
    return len(rows)


def insert_position(conn: sqlite3.Connection, **kw: Any) -> int:
    allowed = {
        "asset", "side", "qty", "entry_price", "entry_ts", "stop_price", "target_price",
        "time_stop_min", "trailing_bp", "thesis_json", "strategy_id", "subaccount", "status", "note",
    }
    data = {k: v for k, v in kw.items() if k in allowed}
    data.setdefault("entry_ts", int(time.time()))
    if isinstance(data.get("thesis_json"), (dict, list)):
        data["thesis_json"] = _dump(data["thesis_json"])
    cols = ", ".join(data.keys())
    marks = ",".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO t2_positions ({cols}) VALUES ({marks})", tuple(data.values()))
    conn.commit()
    return int(cur.lastrowid)


def close_position(conn: sqlite3.Connection, position_id: int, exit_price: float,
                   closed_ts: Optional[int] = None, note: Optional[str] = None,
                   fee_bps: Optional[float] = None) -> Dict[str, Any]:
    pos = conn.execute("SELECT * FROM t2_positions WHERE id=?", (int(position_id),)).fetchone()
    if pos is None:
        raise ValueError(f"position {position_id} not found")
    p = dict(pos)
    if p["status"] != "open":
        return p
    # v464(P0)：平仓价合理性校验。11-19 事故：4 笔 BTC 持仓被以 exit_price=100
    # 平掉（入场 63,106），单笔 -4,992，账户直接 -19,968。任何与最新真实 tick
    # 偏离超过 50% 的平仓价都必须拒绝，宁可不平也不能污染账本。
    exit_price = float(exit_price)
    if exit_price <= 0:
        raise ValueError(f"exit_price_invalid:{exit_price}")
    if (os.getenv("PAPER_PRICE_SANITY_GUARD", "1") or "1").strip() not in ("0", "false", "off", "no"):
        latest = conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (p["asset"],),
        ).fetchone()
        if latest and latest[0] and float(latest[0]) > 0:
            dev = abs(exit_price / float(latest[0]) - 1.0)
            if dev > 0.5:
                raise ValueError(
                    f"exit_price_sanity:{p['asset']} exit={exit_price} "
                    f"tick={latest[0]} dev={dev:.1%}"
                )
    qty, entry = float(p["qty"]), float(p["entry_price"])
    sign = 1.0 if p["side"].lower() in ("long", "buy") else -1.0
    gross = (exit_price - entry) * qty * sign
    # v476：平仓必须计手续费。此前 realized_pnl 是纯毛利，账户比真实成交乐观
    # 1~3%（crypto 双边 16bps、股票更高）；开仓手续费在 account_ledger 入账，
    # 这里只记平仓这一边，并把总费用写回 fee_paid 供审计。
    bps = fee_bps
    if bps is None:
        try:
            bps = float(account(conn).get("fee_bps") or 0.0)
        except (TypeError, ValueError):
            bps = 0.0
    # 老库/最小测试库可能还没有 fee_paid / gross_pnl 两列：没有列就不计费（保持旧行为），
    # 生产库由 ensure_schema 幂等 ALTER 补齐后自动生效。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t2_positions)")}
    has_fee_cols = "fee_paid" in cols and "gross_pnl" in cols
    exit_fee = abs(exit_price * qty) * float(bps or 0.0) / 10_000.0 if has_fee_cols else 0.0
    fee_before = float(p.get("fee_paid") or 0.0) if has_fee_cols else 0.0
    fee_total = fee_before + exit_fee
    # v479：资金费率（paper 也要算，否则持仓越久权益越虚高）。
    # 只按持仓时长线性估算，不是交易所逐次结算的精确值；env=0 时不计。
    funding = 0.0
    has_funding_col = "funding_paid" in cols
    if has_funding_col:
        try:
            fund_bps = float(os.getenv("FUNDING_BPS_PER_8H", "0") or 0.0)
        except ValueError:
            fund_bps = 0.0
        # v491：资金费只存在于永续合约；美股/原油现货没有 funding，
        # 之前一律计费等于给股票凭空扣钱（持仓越久亏得越多）。
        try:
            from feeds import quote_sources as _qs
            _mkt = _qs.market_of(p["asset"])
        except Exception:
            _mkt = "unknown"
        if _mkt not in ("binance_fapi", "okx_swap"):
            fund_bps = 0.0
        if fund_bps > 0 and float(p.get("entry_price") or 0) > 0:
            entry_ts = int(p.get("entry_ts") or 0) or int(closed_ts or time.time())
            hours = max(0.0, (int(closed_ts or time.time()) - entry_ts) / 3600.0)
            notional = abs(float(p.get("entry_price")) * qty)
            funding = notional * fund_bps / 10_000.0 * (hours / 8.0)
    # realized_pnl 必须是“净盈亏”：开仓费 + 平仓费一起扣，否则会出现
    # 开仓时扣了钱、平仓后现金又把开仓费吐回去的漂移（v476 实测到过）。
    net = (gross - fee_total - funding) if has_fee_cols else gross
    if has_fee_cols:
        if has_funding_col:
            conn.execute(
                "UPDATE t2_positions SET status='closed', closed_ts=?, exit_price=?, "
                "realized_pnl=?, gross_pnl=?, fee_paid=?, funding_paid=?, note=? WHERE id=?",
                (int(closed_ts or time.time()), float(exit_price), net, gross, fee_total,
                 funding, note, int(position_id)),
            )
        else:
            conn.execute(
                "UPDATE t2_positions SET status='closed', closed_ts=?, exit_price=?, "
                "realized_pnl=?, gross_pnl=?, fee_paid=?, note=? WHERE id=?",
                (int(closed_ts or time.time()), float(exit_price), net, gross, fee_total,
                 note, int(position_id)),
            )
    else:
        conn.execute(
            "UPDATE t2_positions SET status='closed', closed_ts=?, exit_price=?, "
            "realized_pnl=?, note=? WHERE id=?",
            (int(closed_ts or time.time()), float(exit_price), net, note, int(position_id)),
        )
    acc = account(conn)
    conn.execute(
        "UPDATE t2_account SET realized_pnl=realized_pnl+?, equity=equity+?, cash=cash+?, updated_at=? WHERE id=1",
        (net, net, net, int(time.time())),
    )
    conn.commit()
    # v476：权益按剩余持仓市价重估，避免"平一笔就把权益挪一次"的漂移。
    try:
        import account_ledger as _al
        _al.revalue_equity(conn)
    except Exception:
        pass
    return {**p, "status": "closed", "exit_price": exit_price,
            "realized_pnl": net, "gross_pnl": gross, "fee_paid": fee_total,
            "funding_paid": funding}


def insert_intervention(conn: sqlite3.Connection, **kw: Any) -> int:
    data = dict(kw)
    data.setdefault("ts", int(time.time()))
    data.setdefault("created_at", int(time.time()))
    for k in ("before_json", "after_json"):
        if isinstance(data.get(k), (dict, list)):
            data[k] = _dump(data[k])
    cur = conn.execute(
        "INSERT INTO t2_interventions (ts, kind, target, note, before_json, after_json, "
        "pnl_1h_pct, pnl_1d_pct, counterfactual_1h_pct, counterfactual_1d_pct, settled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (data.get("ts"), data.get("kind"), data.get("target"), data.get("note"),
         data.get("before_json"), data.get("after_json"), data.get("pnl_1h_pct"),
         data.get("pnl_1d_pct"), data.get("counterfactual_1h_pct"),
         data.get("counterfactual_1d_pct"), data.get("settled", 0), data.get("created_at")),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_param_change(conn: sqlite3.Connection, **kw: Any) -> int:
    data = dict(kw)
    data.setdefault("ts", int(time.time()))
    for k in ("old_json", "new_json"):
        if isinstance(data.get(k), (dict, list)):
            data[k] = _dump(data[k])
    cur = conn.execute(
        "INSERT INTO t2_param_changes (ts, scope, key, old_json, new_json, actor, note) VALUES (?,?,?,?,?,?,?)",
        (data.get("ts"), data.get("scope"), data.get("key"), data.get("old_json"),
         data.get("new_json"), data.get("actor"), data.get("note")),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_cost(conn: sqlite3.Connection, **kw: Any) -> int:
    ts = int(kw.get("ts") or time.time())
    day = kw.get("day") or time.strftime("%Y-%m-%d", time.gmtime(ts))
    cur = conn.execute(
        "INSERT INTO t2_cost_ledger (ts, day, strategy_id, category, amount, currency, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, day, kw.get("strategy_id"), kw["category"], float(kw["amount"]),
         kw.get("currency", "USDT"), kw.get("note")),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_snapshot(conn: sqlite3.Connection, decision_id: int, **kw: Any) -> int:
    data = dict(kw)
    for k in ("market_json", "news_json"):
        if isinstance(data.get(k), (dict, list)):
            data[k] = _dump(data[k])
    try:
        cur = conn.execute(
            "INSERT INTO t2_decision_snapshots (decision_id, ts, market_json, news_json, "
            "prompt_hash, model, code_version, captured_at) VALUES (?,?,?,?,?,?,?,?)",
            (int(decision_id), int(data.get("ts") or time.time()), data.get("market_json"),
             data.get("news_json"), data.get("prompt_hash"), data.get("model"),
             data.get("code_version"), int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return 0
