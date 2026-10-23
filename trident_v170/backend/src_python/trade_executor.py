#!/usr/bin/env python3
"""Trident Agent MVP -- Live Trade Executor"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("trade_executor")

STATUS_RISK_PASSED = "RISK_PASSED"
STATUS_ORDER_PLACED = "ORDER_PLACED"
STATUS_RISK_REJECTED = "RISK_REJECTED"
STATUS_ADAPTER_SKIPPED = "ADAPTER_SKIPPED"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"

DEFAULT_SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XAU": "PAXGUSDT",
    "GOLD": "PAXGUSDT",
    "BNB": "BNBUSDT",
}

UNSUPPORTED_ASSETS = {"", "N/A", "NONE", "NULL", "INDIVIDUAL_STOCK", "STOCK", "FX"}
@dataclass
class OrderRequest:
    symbol: str
    side: str
    type_: str = "MARKET"
    quantity: Optional[float] = None
    notional_usdt: Optional[float] = None
    decision_id: int = 0
    client_order_id: str = field(default_factory=lambda: f"trident-{uuid.uuid4().hex[:12]}")


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    filled_qty: float = 0.0
    filled_price: float = 0.0
    fee: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    skipped: bool = False
    note: str = ""


class OrderAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult: ...

    def get_price(self, symbol: str) -> Optional[float]:
        return None


class MockAdapter(OrderAdapter):
    name = "mock"

    def __init__(self, price_lookup=None) -> None:
        self._price_lookup = price_lookup

    def place_order(self, req: OrderRequest) -> OrderResult:
        price = self.get_price(req.symbol) or 0.0
        if req.quantity and req.quantity > 0:
            qty = float(req.quantity)
        elif price and req.notional_usdt:
            qty = float(req.notional_usdt) / float(price)
        else:
            qty = 0.0
        return OrderResult(
            ok=True, order_id=f"mock-{uuid.uuid4().hex[:10]}",
            filled_qty=qty, filled_price=price,
            fee=0.0, raw={"mock": True, "symbol": req.symbol},
            skipped=True, note="paper_mode_no_real_order",
        )

    def get_price(self, symbol: str) -> Optional[float]:
        if not self._price_lookup:
            return None
        try:
            v = self._price_lookup(symbol)
            return float(v) if v is not None else None
        except Exception:
            return None


class BinanceAdapter(OrderAdapter):
    name = "binance"

    def __init__(self, client, *, dry_run: bool = False) -> None:
        self.client = client
        self.dry_run = bool(dry_run)

    def get_price(self, symbol: str) -> Optional[float]:
        try:
            data = self.client.ticker_price(symbol)
            if isinstance(data, dict):
                p = data.get("price")
                return float(p) if p is not None else None
            if isinstance(data, list) and data and isinstance(data[0], dict):
                p = data[0].get("price")
                return float(p) if p is not None else None
        except Exception as exc:
            logger.warning("BinanceAdapter.get_price(%s) failed: %s", symbol, exc)
        return None

    def place_order(self, req: OrderRequest) -> OrderResult:
        if self.dry_run:
            price = self.get_price(req.symbol) or 0.0
            if req.quantity and req.quantity > 0:
                qty = float(req.quantity)
            elif price and req.notional_usdt:
                qty = float(req.notional_usdt) / float(price)
            else:
                qty = 0.0
            return OrderResult(
                ok=True, order_id="DRY-RUN",
                filled_qty=qty, filled_price=price,
                fee=0.0, raw={"dry_run": True},
                skipped=True, note="live_dry_no_real_order",
            )
        try:
            quantity = None
            if req.quantity and req.quantity > 0:
                quantity = float(req.quantity)
            elif req.notional_usdt:
                price = self.get_price(req.symbol)
                if not price:
                    return OrderResult(False, error="no_price_for_notional_calc")
                quantity = round(float(req.notional_usdt) / float(price), 6)
            if not quantity:
                return OrderResult(False, error="missing_quantity")
            resp = self.client.new_order(
                symbol=req.symbol,
                side=req.side,
                type_=req.type_,
                quantity=quantity,
                time_in_force="GTC",
                new_client_order_id=req.client_order_id,
            )
            if not isinstance(resp, dict):
                return OrderResult(False, error=f"unexpected_response: {resp!r}")
            return OrderResult(
                ok=True,
                order_id=str(resp.get("orderId", "")),
                filled_qty=float(resp.get("executedQty", 0.0) or 0.0),
                filled_price=float(resp.get("avgPrice", 0.0) or 0.0),
                fee=0.0,
                raw=resp,
            )
        except Exception as exc:
            logger.warning("BinanceAdapter.place_order failed: %s", exc)
            return OrderResult(False, error=str(exc), raw={"error": str(exc)})


# ──────────────────────────────────────────────────────────────────────
# Trade Executor — 主类
# ──────────────────────────────────────────────────────────────────────
@dataclass
class ExecutorConfig:
    mode: str = "off"                  # off / paper / live_dry / live_real
    poll_interval: float = 5.0
    notional_usdt: float = 100.0
    symbol_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SYMBOL_MAP))
    unsupported: set = field(default_factory=lambda: set(UNSUPPORTED_ASSETS))
    testnet: bool = False
    max_decisions_per_tick: int = 5
    run_heartbeat_seconds: float = 30.0

    @classmethod
    def from_env(cls, env=None) -> "ExecutorConfig":
        env = env or os.environ
        from risk_manager import parse_whitelist
        return cls(
            mode=(env.get("TRADE_EXECUTOR_MODE", "off") or "off").strip().lower(),
            poll_interval=float(env.get("TRADE_EXECUTOR_POLL_INTERVAL", "5") or 5.0),
            notional_usdt=float(env.get("BINANCE_NOTIONAL_USDT", "100") or 100.0),
            testnet=(env.get("BINANCE_USE_TESTNET", "false").strip().lower() in ("1", "true", "yes")),
        )


class TradeExecutor:
    """周期扫描 ai_decisions → risk → adapter → 回写 + audit。"""

    def __init__(self, cfg: ExecutorConfig, adapter: OrderAdapter,
                 risk_manager, db_path: str) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.risk_manager = risk_manager
        self.db_path = db_path
        self._last_heartbeat = 0.0
        self._stats = {
            "ticks": 0, "decisions_seen": 0, "risk_passed": 0,
            "risk_rejected": 0, "orders_placed": 0,
            "orders_skipped": 0, "errors": 0,
        }

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_audit_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trade_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                asset TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                notional_usdt REAL DEFAULT 0.0,
                quantity REAL DEFAULT 0.0,
                filled_price REAL DEFAULT 0.0,
                order_id TEXT DEFAULT '',
                adapter TEXT DEFAULT '',
                mode TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                raw_json TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )"""
        )
        conn.commit()

    def _fetch_pending(self, conn) -> List[sqlite3.Row]:
        sql = """
            SELECT id, news_id, sentiment_score, suggested_action, target_asset,
                   created_at, market_category, reasoning, aggregation_key
            FROM ai_decisions
            WHERE entry_price IS NULL
              AND settled = 0
              AND UPPER(suggested_action) IN ('BUY', 'SELL')
              AND target_asset IS NOT NULL
              AND target_asset != ''
              AND target_asset != 'NONE'
              AND id NOT IN (SELECT DISTINCT decision_id FROM trade_audit_log)
            ORDER BY id ASC
            LIMIT ?
        """
        return list(conn.execute(sql, (self.cfg.max_decisions_per_tick,)))

    def _insert_audit(self, conn, *, decision_id, asset, symbol, side, status,
                      notional_usdt, quantity, filled_price, order_id,
                      adapter, mode, reason, raw) -> None:
        conn.execute(
            """INSERT INTO trade_audit_log
               (decision_id, asset, symbol, side, status, notional_usdt,
                quantity, filled_price, order_id, adapter, mode, reason, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, asset, symbol, side, status, float(notional_usdt),
             float(quantity), float(filled_price), str(order_id),
             adapter, mode, reason,
             json.dumps(raw, ensure_ascii=False) if raw is not None else None),
        )
        conn.commit()

    def _mark_ai_decision_entry(self, conn, decision_id: int,
                                 price: float, qty: float,
                                 order_id: str, ts: str) -> None:
        conn.execute(
            """UPDATE ai_decisions
               SET entry_price = ?, entry_time = ?
               WHERE id = ? AND entry_price IS NULL""",
            (float(price), ts, int(decision_id)),
        )
        conn.commit()

    def _resolve_symbol(self, asset: str) -> Optional[str]:
        a = (asset or "").upper().strip()
        if not a or a in self.cfg.unsupported:
            return None
        if a in self.cfg.symbol_map:
            return self.cfg.symbol_map[a]
        if a + "USDT" in self.cfg.symbol_map.values():
            return a + "USDT"
        for k, v in self.cfg.symbol_map.items():
            if a == k or a.startswith(k + "_"):
                return v
        return None

    def _archive_signal_chain(self, conn, decision_id: int) -> None:
        """v191: 把这条 decision 的全链路快照写入 signal_chain_archive(best-effort)。

        任何异常都吞掉 — 归档是辅助能力,绝不能阻塞交易主路径。
        表不存在/列不一致时也只 warn,不影响后续 audit 落库。
        """
        try:
            from daily_archive import archive_signal_chain
        except Exception as exc:
            logger.warning("archive_signal_chain import failed: %s", exc)
            return
        try:
            archive_signal_chain(conn, decision_id)
        except Exception as exc:
            logger.warning("archive_signal_chain(%s) failed: %s", decision_id, exc)

    def tick(self) -> Dict[str, Any]:
        self._stats["ticks"] += 1
        result: Dict[str, Any] = {"processed": [], "errors": []}
        try:
            conn = self._conn()
        except Exception as exc:
            logger.error("trade_executor db open failed: %s", exc)
            self._stats["errors"] += 1
            result["errors"].append(str(exc))
            return result
        try:
            self._ensure_audit_table(conn)
            pending = self._fetch_pending(conn)
        except Exception as exc:
            logger.error("trade_executor fetch failed: %s", exc)
            self._stats["errors"] += 1
            conn.close()
            result["errors"].append(str(exc))
            return result
        for row in pending:
            item = self._process_one(conn, row)
            result["processed"].append(item)
            self._stats["decisions_seen"] += 1
        conn.close()
        now = time.time()
        if now - self._last_heartbeat >= self.cfg.run_heartbeat_seconds:
            self._last_heartbeat = now
            try:
                logger.info("trade_executor heartbeat %s",
                            json.dumps(self.snapshot(), ensure_ascii=False))
            except Exception:
                pass
        return result

    def _process_one(self, conn, row: sqlite3.Row) -> Dict[str, Any]:
        decision_id = int(row["id"])
        asset = (row["target_asset"] or "").strip().upper()
        action = (row["suggested_action"] or "").strip().upper()

        symbol = self._resolve_symbol(asset)
        if not symbol:
            reason = f"asset_unsupported:{asset}"
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol="", side=action, status=STATUS_RISK_REJECTED,
                                notional_usdt=0.0, quantity=0.0, filled_price=0.0,
                                order_id="", adapter=self.adapter.name,
                                mode=self.cfg.mode, reason=reason, raw=None)
            self._archive_signal_chain(conn, decision_id)
            self._stats["risk_rejected"] += 1
            return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED, "reason": reason}

        ctx = {
            "decision_id": decision_id,
            "market_category": row["market_category"],
            "sentiment_score": row["sentiment_score"],
            "aggregation_key": row["aggregation_key"],
        }
        decision = self.risk_manager.evaluate(asset, action, self.cfg.notional_usdt, ctx)
        if not decision.allow:
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_RISK_REJECTED,
                                notional_usdt=self.cfg.notional_usdt,
                                quantity=0.0, filled_price=0.0, order_id="",
                                adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=decision.reason, raw=decision.to_dict())
            self._archive_signal_chain(conn, decision_id)
            self._stats["risk_rejected"] += 1
            return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED,
                    "reason": decision.reason}

        size_usdt = float(decision.max_allowed_size)
        self._stats["risk_passed"] += 1

        req = OrderRequest(
            symbol=symbol, side=action, type_="MARKET",
            notional_usdt=size_usdt, decision_id=decision_id,
        )
        order_result = self.adapter.place_order(req)

        if order_result.skipped:
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_ADAPTER_SKIPPED,
                                notional_usdt=size_usdt,
                                quantity=order_result.filled_qty,
                                filled_price=order_result.filled_price,
                                order_id=order_result.order_id,
                                adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=order_result.note or "skipped",
                                raw={"raw": order_result.raw})
            try:
                now_iso = time.strftime("%Y-%m-%d %H:%M:%S")
                self._mark_ai_decision_entry(conn, decision_id,
                                              order_result.filled_price or 0.0,
                                              order_result.filled_qty or 0.0,
                                              order_result.order_id, now_iso)
            except Exception as exc:
                logger.warning("mark entry on skip failed: %s", exc)
            self._archive_signal_chain(conn, decision_id)
            self._stats["orders_skipped"] += 1
            return {"decision_id": decision_id, "status": STATUS_ADAPTER_SKIPPED,
                    "order_id": order_result.order_id, "note": order_result.note}

        if not order_result.ok:
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_ADAPTER_ERROR,
                                notional_usdt=size_usdt, quantity=0.0,
                                filled_price=0.0, order_id="",
                                adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=order_result.error,
                                raw={"raw": order_result.raw})
            self._archive_signal_chain(conn, decision_id)
            self._stats["errors"] += 1
            return {"decision_id": decision_id, "status": STATUS_ADAPTER_ERROR,
                    "error": order_result.error}

        try:
            now_iso = time.strftime("%Y-%m-%d %H:%M:%S")
            self._mark_ai_decision_entry(conn, decision_id,
                                          order_result.filled_price or 0.0,
                                          order_result.filled_qty or 0.0,
                                          order_result.order_id, now_iso)
            self.risk_manager.on_position_opened(asset)
        except Exception as exc:
            logger.warning("post-fill write failed: %s", exc)

        self._insert_audit(conn, decision_id=decision_id, asset=asset,
                            symbol=symbol, side=action, status=STATUS_ORDER_PLACED,
                            notional_usdt=size_usdt,
                            quantity=order_result.filled_qty,
                            filled_price=order_result.filled_price,
                            order_id=order_result.order_id,
                            adapter=self.adapter.name, mode=self.cfg.mode,
                            reason="ok", raw={"raw": order_result.raw})
        self._archive_signal_chain(conn, decision_id)
        self._stats["orders_placed"] += 1
        return {"decision_id": decision_id, "status": STATUS_ORDER_PLACED,
                "order_id": order_result.order_id,
                "qty": order_result.filled_qty,
                "price": order_result.filled_price}

    async def run(self) -> None:
        """engine worker 主协程。"""
        logger.info("trade_executor start mode=%s poll=%.1fs",
                    self.cfg.mode, self.cfg.poll_interval)
        if self.cfg.mode == "off":
            logger.warning("trade_executor disabled (mode=off)")
            return
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:
                    logger.exception("trade_executor tick error: %s", exc)
                    self._stats["errors"] += 1
                await asyncio.sleep(max(0.5, self.cfg.poll_interval))
        except asyncio.CancelledError:
            logger.info("trade_executor cancelled")
            raise

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.cfg.mode,
            "adapter": self.adapter.name,
            "poll_interval": self.cfg.poll_interval,
            "notional_usdt": self.cfg.notional_usdt,
            "stats": dict(self._stats),
            "risk": self.risk_manager.snapshot(),
        }


def build_default_executor(db_path: str, env=None) -> TradeExecutor:
    """env / config → 选 adapter → 组装 TradeExecutor。"""
    env = env or os.environ
    from risk_manager import RiskManager, parse_whitelist
    cfg = ExecutorConfig.from_env(env=env)
    rm = RiskManager(
        whitelist=tuple(parse_whitelist(env.get("WHITELIST_ASSETS", "*"))),
        max_position_usdt=float(env.get("MAX_POSITION_USDT", "5000") or 5000.0),
    )

    if cfg.mode == "off":
        adapter = MockAdapter()
    elif cfg.mode == "paper":
        adapter = MockAdapter()
    elif cfg.mode in ("live_dry", "live_real"):
        from binance_client import BinanceClient
        key = env.get("BINANCE_API_KEY", "").strip()
        secret = env.get("BINANCE_API_SECRET", "").strip()
        if not key or not secret:
            logger.warning("BINANCE_API_KEY/SECRET 未配，回退 MockAdapter（仍会写 audit_log）")
            adapter = MockAdapter()
        else:
            client = BinanceClient(
                key, secret,
                testnet=cfg.testnet,
                timeout=float(env.get("BINANCE_REQUEST_TIMEOUT", "5") or 5.0),
            )
            adapter = BinanceAdapter(client, dry_run=(cfg.mode == "live_dry"))
    else:
        logger.warning("未知 mode=%s，回退 off", cfg.mode)
        adapter = MockAdapter()
        cfg.mode = "off"

    return TradeExecutor(cfg, adapter, rm, db_path)


__all__ = [
    "ExecutorConfig", "TradeExecutor",
    "OrderAdapter", "OrderRequest", "OrderResult",
    "MockAdapter", "BinanceAdapter",
    "DEFAULT_SYMBOL_MAP", "UNSUPPORTED_ASSETS",
    "STATUS_RISK_PASSED", "STATUS_ORDER_PLACED",
    "STATUS_RISK_REJECTED", "STATUS_ADAPTER_SKIPPED", "STATUS_ADAPTER_ERROR",
    "build_default_executor",
]
