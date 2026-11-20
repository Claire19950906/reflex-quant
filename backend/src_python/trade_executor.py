#!/usr/bin/env python3
"""Trident Agent MVP -- Live Trade Executor"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
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

    def __init__(self, price_lookup=None, simulate_fill: bool = False) -> None:
        self._price_lookup = price_lookup
        # paper 模式：真的记一笔模拟成交（否则永远 ADAPTER_SKIPPED → 攒不出持仓和样本）
        self._simulate_fill = bool(simulate_fill)

    def place_order(self, req: OrderRequest) -> OrderResult:
        price = self.get_price(req.symbol) or 0.0
        if req.quantity and req.quantity > 0:
            qty = float(req.quantity)
        elif price and req.notional_usdt:
            qty = float(req.notional_usdt) / float(price)
        else:
            qty = 0.0
        if self._simulate_fill and price > 0 and qty > 0:
            try:
                slip_bp = float(os.getenv("PAPER_SLIPPAGE_BP", "0") or 0.0)
            except ValueError:
                slip_bp = 0.0
            try:
                fill_ratio = float(os.getenv("PAPER_FILL_RATIO", "1") or 1.0)
            except ValueError:
                fill_ratio = 1.0
            fill_ratio = max(0.0, min(1.0, fill_ratio))
            side = str(req.side).upper()
            fill_px = price * (1 + slip_bp / 10_000.0) if side in ("BUY", "LONG") \
                else price * (1 - slip_bp / 10_000.0)
            filled_qty = qty * fill_ratio
            return OrderResult(
                ok=True, order_id=f"mock-{uuid.uuid4().hex[:10]}",
                filled_qty=filled_qty, filled_price=fill_px,
                fee=0.0,
                raw={"mock": True, "symbol": req.symbol, "slippage_bp": slip_bp,
                     "fill_ratio": fill_ratio, "ref_price": price},
                skipped=False, note="paper_simulated_fill",
            )
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


class RefusingAdapter(OrderAdapter):
    """实盘未武装时的硬拒单适配器。

    为什么不是"回退 MockAdapter"：live 模式回退 Mock 会让人以为在下单，
    实际只是把 skipped 写进审计；更危险的是有人拿它当实盘"已接通"的证据。
    这里明确拒单，原因原样落到 trade_audit_log.reason，绝不产生成交。
    """

    name = "refusing"

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)

    def place_order(self, req: OrderRequest) -> OrderResult:
        return OrderResult(ok=False, skipped=True, note=self.reason,
                           raw={"refusing": True, "symbol": req.symbol})


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
    # v432：只执行最近 N 秒内产生的决策，避免重启后把几天前的旧决策一次性重放。
    max_decision_age_seconds: int = 7200
    run_heartbeat_seconds: float = 30.0

    @classmethod
    def from_env(cls, env=None) -> "ExecutorConfig":
        # v310 修: 原来是 `env = env or os.environ` —— **显式传入的空 dict 是 falsy**,
        # 会被当成"没传" → 回落到 os.environ → 读到 backend/.env 里的
        # BINANCE_NOTIONAL_USDT=5000 → from_env(env={}) 拿到的不是默认值 100。
        # 语义应是"只有 None 才表示用进程环境"。
        env = os.environ if env is None else env
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
        # v499：执行器自建连接时只传了 timeout=10s，没有 busy_timeout/WAL 配置。
        # 而 _process_one 在**同一个连接**里既写 trade_audit_log 又调
        # archive_signal_chain（写 signal_chain_archive，跨 5+ 张表 upsert）。
        # 归档期间写锁被本连接持有 → 其他进程（engine / 新闻落库）撞锁排队，
        # 昨晚高写入期 `database is locked` 552 次的主因之一。
        # 两处修正：① busy_timeout 抬到 30s（与 db.get_connection 的 20s 对齐并放宽，
        # 抓锁失败不再靠 10s 硬超时）；② 沿用 db.py 的 WAL/NORMAL 组合，减少 fsync。
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
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
        # v432（2026-11-17）：原条件 `entry_price IS NULL` 让执行器**永远查不到决策**。
        # 实测：ai_decisions 里的 BUY/SELL 行都自带 entry_price（模型给了入场价），
        # 于是 decisions_seen 恒为 0、今日零成交；库里积压 6 条未执行的 BUY/SELL。
        # 改为「未结算 + 审计去重 + 只执行最近 N 秒」，时间过滤在 Python 里做，
        # 因为 created_at 实际存的是 ISO8601 文本（2026-11-17T11:16:43.729+08:00），
        # SQL 里的 CAST/strftime 对带时区的文本不可靠。
        sql = """
            SELECT id, news_id, sentiment_score, suggested_action, target_asset,
                   created_at, market_category, reasoning, aggregation_key
            FROM ai_decisions
            WHERE settled = 0
              AND UPPER(suggested_action) IN ('BUY', 'SELL')
              AND target_asset IS NOT NULL
              AND target_asset != ''
              AND target_asset != 'NONE'
              AND id NOT IN (SELECT DISTINCT decision_id FROM trade_audit_log)
            ORDER BY id DESC
            LIMIT 50
        """
        rows = list(conn.execute(sql))
        cutoff = time.time() - float(self.cfg.max_decision_age_seconds)
        fresh = []
        for r in rows:
            created = r["created_at"]
            ts = 0.0
            if isinstance(created, (int, float)):
                ts = float(created)
            elif isinstance(created, str):
                try:
                    dt = datetime.fromisoformat(created)
                    if dt.tzinfo is None:
                        # 2026-11-18 修: ai_decisions.created_at 的表默认值是
                        # datetime('now') —— **UTC 且不带时区**。老代码直接 .timestamp()
                        # 按本地(CST)解读, 刚写入的行会被算成 8 小时前, 撞上 7200s
                        # 新鲜度窗口 → 静默丢弃、零成交(实测 tick 返回 processed=[] 且
                        # 无异常, 最难查的那种失败)。带 +08:00 的行不受影响。
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                except ValueError:
                    try:
                        ts = float(created)
                    except ValueError:
                        ts = 0.0
            if ts >= cutoff:
                fresh.append(r)
            if len(fresh) >= int(self.cfg.max_decisions_per_tick):
                break
        return fresh

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

    def _mirror_to_t2(self, conn, *, decision_id: int, asset: str, side: str,
                      qty: float, price: float, notional: float,
                      order_id: str, adapter: str) -> None:
        """把成交镜像进 T2 账本（t2_orders / t2_positions）。

        背景（2026-11-18）：执行器成交只写 trade_audit_log，而前端/风控看的是
        trident2 的 t2_orders / t2_positions —— 于是"明明下了单"，界面上一直是 11-16
        的旧仓，等于看不见成交。这里把成交补写进 T2 账本，口径统一。
        """
        if str(os.getenv("TRIDENT_EXECUTOR_MIRROR_T2", "1")).strip() in {"0", "false", "off", "no"}:
            return
        try:
            from trident2 import store as t2store
        except Exception:
            from ..trident2 import store as t2store  # type: ignore
        now = int(time.time())
        order_row_id = t2store.insert_order(
            conn, ts=now, asset=asset, side=side.lower(),
            order_type="MARKET", qty=float(qty), notional_usdt=float(notional),
            price=float(price), status="filled", mode=self.cfg.mode,
            decision_id=int(decision_id), filled_price=float(price),
            filled_qty=float(qty), note=f"trade_executor/{adapter}",
        )
        pos_side = "long" if side.upper() == "BUY" else "short"
        try:
            import execution_guards as _eg
            stop_price = _eg.auto_stop_price(float(price), pos_side)
        except Exception as exc:
            logger.warning("auto stop calc failed: %s", exc)
            stop_price = None
        pos_id = t2store.insert_position(
            conn, asset=asset, side=pos_side, qty=float(qty),
            entry_price=float(price), entry_ts=now,
            stop_price=stop_price,
            thesis_json={"reason": "AI 决策成交", "decision_id": int(decision_id),
                         "order_id": str(order_id), "t2_order_id": order_row_id,
                         "auto_stop": bool(stop_price)},
            strategy_id="ai_news", subaccount="paper" if self.cfg.mode == "paper" else self.cfg.mode,
            status="open", note=f"executor decision#{decision_id}",
        )
        # v476：开仓手续费即时入账（否则权益一直高估且平仓时才一次扣两次费）。
        try:
            import account_ledger as _al
            _al.book_entry_fee(conn, int(pos_id))
        except Exception as exc:
            logger.warning("book entry fee failed: %s", exc)

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

    def _cost_meta(self, asset: str, entry_ts: int) -> Dict[str, Any]:
        """v319+: 这笔委托的**成本口径** + **交收时点口径** (写进 audit raw_json)。

        为什么放在 audit 里而不是只靠结算时扣费:
          · 审计/复盘时要能回答"这笔单子当时认为值多少钱、能不能当天平";
          · paper 模式下成交价是**报价** (不含滑点), 真实成交成本必须显式记账,
            否则 paper 净值比实盘高估 1-3% (crypto 30bps/次往返)。
        """
        try:
            from cost_model import settlement_policy, earliest_exit_ts, is_exit_allowed
            policy = settlement_policy(asset)
            earliest = earliest_exit_ts(asset, entry_ts)
            same_day_ok, _why = is_exit_allowed(asset, entry_ts, entry_ts)
            return {
                "cost": policy,
                "settlement": {
                    "t_plus": policy["t_plus"],
                    "lag_days": policy["lag_days"],
                    "entry_ts": int(entry_ts),
                    "earliest_exit_ts": int(earliest),
                    "same_day_exit_allowed": bool(same_day_ok),
                },
            }
        except Exception as exc:  # 成本口径算不出来不该拦住下单
            logger.warning("cost meta failed for %s: %s", asset, exc)
            return {}

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
            # v476：先把权益按最新行情重估，后面的组合闸/前置闸才看得到真实杠杆。
            try:
                import account_ledger as _al
                _al.revalue_equity(conn)
            except Exception as exc:
                logger.warning("account revalue failed: %s", exc)
            # v479：卡住的挂单超时撤销（纸面恒 0；实盘/断连时才起作用）
            try:
                from trident2 import store as _t2s
                n = _t2s.cancel_stale_orders(conn, float(os.getenv("ORDER_STALE_SECONDS", "120")))
                if n:
                    logger.warning("cancel_stale_orders: %d 笔超时撤单", n)
            except Exception as exc:
                logger.warning("cancel_stale_orders failed: %s", exc)
            # v463：先执行止损，再处理新决策；同一轮里不让“已破位”的仓位继续叠新仓
            try:
                import stops as _stops
                closed = _stops.enforce(conn)
                if closed:
                    result["stops_closed"] = closed
                    logger.warning("auto stop closed %d position(s): %s", len(closed), closed)
            except Exception as exc:
                logger.warning("stop enforce failed: %s", exc)
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
        # v464(P0)：探针/mock 决策不得到真实账本。11-19 事故：decision_id
        # -990101/-990102 的假单被成交并生成 4 笔 BTC 仓，最终以 100 平仓，
        # 把账户打成 -9,968。负 id / 0 一律拒单。
        if decision_id <= 0:
            reason = f"invalid_decision_id:{decision_id}"
            self._insert_audit(conn, decision_id=decision_id,
                               asset=(row["target_asset"] or "").strip().upper(),
                               symbol="", side=(row["suggested_action"] or "").strip().upper(),
                               status=STATUS_RISK_REJECTED, notional_usdt=0.0,
                               quantity=0.0, filled_price=0.0, order_id="",
                               adapter=self.adapter.name, mode=self.cfg.mode,
                               reason=reason, raw=None)
            self._stats["risk_rejected"] += 1
            return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED, "reason": reason}
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

        # v463：组合闸——同品种重复开仓 / 单品种上限 / 总杠杆上限，必须在 place_order 之前
        try:
            import execution_guards as _eg
            guard = _eg.check_new_entry(conn, asset, action, size_usdt)
        except Exception as exc:
            guard = {"allow": False, "size_usdt": 0.0,
                     "reason": f"portfolio_guard_error:{exc}", "warnings": []}
        if not guard["allow"]:
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_RISK_REJECTED,
                                notional_usdt=size_usdt, quantity=0.0, filled_price=0.0,
                                order_id="", adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=guard["reason"],
                                raw=guard)
            self._archive_signal_chain(conn, decision_id)
            self._stats["risk_rejected"] += 1
            return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED,
                    "reason": guard["reason"]}
        if float(guard["size_usdt"]) < size_usdt:
            logger.warning("portfolio guard resize %s %s: %.0f -> %.0f (%s)",
                           asset, action, size_usdt, guard["size_usdt"],
                           "; ".join(guard.get("warnings") or []))
            size_usdt = float(guard["size_usdt"])

        # v476：交易前置闸 —— 账本对平 / 行情新鲜 / 持仓数 / 总敞口 / run 唯一。
        # 失败即拒单（fail-closed）：账本对不上时，任何成交都只会放大不可信的数字。
        try:
            import trade_preflight as _pf
            pf = _pf.preflight(conn, asset, action, size_usdt)
        except Exception as exc:
            pf = {"ok": False, "denies": [{"code": "PREFLIGHT_ERROR", "detail": str(exc)}],
                  "warnings": []}
        if not pf["ok"]:
            reason = "preflight:" + ",".join(str(d["code"]) for d in pf["denies"])
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_RISK_REJECTED,
                                notional_usdt=size_usdt, quantity=0.0, filled_price=0.0,
                                order_id="", adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=reason, raw=pf)
            self._archive_signal_chain(conn, decision_id)
            self._stats["risk_rejected"] += 1
            return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED, "reason": reason}
        if pf.get("warnings"):
            logger.warning("preflight warnings %s %s: %s", asset, action,
                           "; ".join(str(w) for w in pf["warnings"]))

        # v479：幂等占位。同一决策只允许下一次单，重启重放/重复扫描都拦在这里。
        order_row = None
        _t2 = None
        try:
            from trident2 import store as _t2
            order_row = _t2.claim_order(
                conn, client_order_id=f"dec{decision_id}:{asset}:{action}",
                asset=asset, side=action, order_type="MARKET",
                notional_usdt=size_usdt, mode=self.cfg.mode, decision_id=int(decision_id),
            )
        except Exception as exc:
            logger.warning("claim_order failed（按未跟踪继续）: %s", exc)
            order_row = -1
        if order_row is None:
            reason = "duplicate_client_order"
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_ADAPTER_SKIPPED,
                                notional_usdt=size_usdt, quantity=0.0, filled_price=0.0,
                                order_id="", adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=reason, raw=None)
            self._stats["orders_skipped"] += 1
            return {"decision_id": decision_id, "status": STATUS_ADAPTER_SKIPPED,
                    "reason": reason}

        # v491：股票只能整股。按最新 mark 价折算 → 向下取整 → 按整股重算名义；
        # 不足 1 股直接拒，不把 0.3 股发给券商（现实里挂不进去）。
        _qty = 0.0
        try:
            import market_rules as _mr
            step = _mr.lot_size(asset)
            if step > 0:
                mark_row = conn.execute(
                    "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                    (asset,)).fetchone()
                mark_px = float(mark_row["price"]) if mark_row and mark_row["price"] else 0.0
                if mark_px > 0:
                    _qty = _mr.round_qty(asset, size_usdt / mark_px)
                    if _qty <= 0:
                        reason = "preflight:PREFLIGHT_LOT_SIZE"
                        self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                           symbol=symbol, side=action,
                                           status=STATUS_RISK_REJECTED,
                                           notional_usdt=size_usdt, quantity=0.0,
                                           filled_price=0.0, order_id="",
                                           adapter=self.adapter.name, mode=self.cfg.mode,
                                           reason=reason,
                                           raw={"mark": mark_px, "lot": step})
                        self._stats["risk_rejected"] += 1
                        return {"decision_id": decision_id, "status": STATUS_RISK_REJECTED,
                                "reason": reason}
                    size_usdt = _qty * mark_px
        except Exception:
            _qty = 0.0

        req = OrderRequest(
            symbol=symbol, side=action, type_="MARKET",
            notional_usdt=size_usdt, decision_id=decision_id,
            quantity=_qty or None,
        )
        order_result = self.adapter.place_order(req)
        if _t2 is not None and order_row and order_row > 0:
            try:
                if order_result.skipped:
                    _t2.update_order_status(conn, order_row, "CANCELLED",
                                            note=order_result.note or "skipped")
                elif not order_result.ok:
                    _t2.update_order_status(conn, order_row, "CANCELLED",
                                            note=order_result.error or "adapter_error")
                else:
                    _t2.update_order_status(conn, order_row, "FILLED",
                                            filled_price=order_result.filled_price,
                                            filled_qty=order_result.filled_qty)
            except Exception as exc:
                logger.warning("update_order_status failed: %s", exc)

        if order_result.skipped:
            self._insert_audit(conn, decision_id=decision_id, asset=asset,
                                symbol=symbol, side=action, status=STATUS_ADAPTER_SKIPPED,
                                notional_usdt=size_usdt,
                                quantity=order_result.filled_qty,
                                filled_price=order_result.filled_price,
                                order_id=order_result.order_id,
                                adapter=self.adapter.name, mode=self.cfg.mode,
                                reason=order_result.note or "skipped",
                                raw={"raw": order_result.raw,
                                     **self._cost_meta(asset, int(time.time()))})
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
            self._mirror_to_t2(conn, decision_id=decision_id, asset=asset, side=action,
                               qty=order_result.filled_qty or 0.0,
                               price=order_result.filled_price or 0.0,
                               notional=size_usdt,
                               order_id=order_result.order_id,
                               adapter=self.adapter.name)
        except Exception as exc:
            logger.warning("post-fill write failed: %s", exc)

        self._insert_audit(conn, decision_id=decision_id, asset=asset,
                            symbol=symbol, side=action, status=STATUS_ORDER_PLACED,
                            notional_usdt=size_usdt,
                            quantity=order_result.filled_qty,
                            filled_price=order_result.filled_price,
                            order_id=order_result.order_id,
                            adapter=self.adapter.name, mode=self.cfg.mode,
                            reason="ok", raw={"raw": order_result.raw,
                                              **self._cost_meta(asset, int(time.time()))})
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
                    # v498（体检修复）: tick() 内部全是同步 SQLite 读写，直接在事件循环里调用，
                    # 一旦 SQLite 被其它 worker 锁住（busy_timeout 30s），整个 FastAPI 进程
                    # 会连续 30s 不响应任何请求 —— 页面全站"加载中/超时"的真因。
                    # 挪到线程池后，锁等待只阻塞这一轮 tick，不再堵死 HTTP。
                    await asyncio.to_thread(self.tick)
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


def _make_db_price_lookup(db_path: str, symbol_map: Dict[str, str]):
    """用库里 market_ticks 的最新价给 mock 适配器供价。

    事故（v323）：MockAdapter 没接价格源 → filled_price 恒 0 → 审计行
    `ADAPTER_SKIPPED` 的成交价是 0.0 → 写回 ai_decisions.entry_price=0
    → 这条"交易"永远无法结算，等于开了模拟盘也攒不出样本。
    """
    def _lookup(symbol: str) -> Optional[float]:
        # 优先用映射后的 symbol（BTCUSDT），再退回原始（BTC）
        candidates = [symbol]
        for asset, mapped in symbol_map.items():
            if mapped == symbol:
                candidates.append(asset)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                for sym in candidates:
                    row = conn.execute(
                        "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                        (sym,),
                    ).fetchone()
                    if row and row[0]:
                        return float(row[0])
            finally:
                conn.close()
        except Exception:
            return None
        return None
    return _lookup


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
        # paper 模式只有"库里有价"这一个前提，所以把 market_ticks 里实际有价的品种
        # 补进映射（原先 WTI 不在 DEFAULT_SYMBOL_MAP → RISK_REJECTED asset_unsupported）
        try:
            _conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                for (_sym,) in _conn.execute("SELECT DISTINCT symbol FROM market_ticks"):
                    if _sym and _sym not in cfg.symbol_map.values() and _sym not in cfg.unsupported:
                        cfg.symbol_map.setdefault(str(_sym).upper(), str(_sym).upper())
            finally:
                _conn.close()
        except Exception as exc:
            logger.warning("paper 模式补品种映射失败: %s", exc)
        # 用库里最新价供价，否则 mock 成交价恒 0 → entry_price=0 → 永远结算不了
        adapter = MockAdapter(
            price_lookup=_make_db_price_lookup(db_path, cfg.symbol_map),
            simulate_fill=True,
        )
    elif cfg.mode in ("live_dry", "live_real"):
        # v477：实盘必须"三重武装"才给下单能力——密钥、显式确认、单笔上限。
        # 任何一项缺失都换成 RefusingAdapter（硬拒单、写原因），绝不回退 Mock 假装在交易。
        from binance_client import BinanceClient
        key = env.get("BINANCE_API_KEY", "").strip()
        secret = env.get("BINANCE_API_SECRET", "").strip()
        ack = env.get("LIVE_TRADING_ACK", "").strip()
        cap = float(env.get("LIVE_MAX_NOTIONAL_USDT", "1000") or 1000.0)
        reasons: List[str] = []
        if not key or not secret:
            reasons.append("missing_api_key")
        if cfg.mode == "live_real" and ack != "I_UNDERSTAND_REAL_MONEY":
            reasons.append("missing_LIVE_TRADING_ACK")
        if float(cfg.notional_usdt) > cap:
            reasons.append(f"notional_over_cap:{cfg.notional_usdt:g}>{cap:g}")
        if reasons:
            logger.error("实盘未武装，全部拒单：%s", ",".join(reasons))
            adapter = RefusingAdapter("live_not_armed:" + ",".join(reasons))
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
