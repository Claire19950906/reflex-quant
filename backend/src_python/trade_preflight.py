#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易前置闸（v476）—— 下单前必须全部通过，否则拒单。

为什么必须有这一层：
    2026-11-19 的实测里，下单前没有任何"系统是否可交易"的检查：
      · `t2_account` 权益是缓存值，4 笔 5,000 USDT 持仓的浮亏根本没算进去；
      · 同一时刻有 3 条 RUNNING 的 paper run（自愈时并发创建），成交会被重复记账；
      · 有 1 笔平仓行 `realized_pnl` 为 NULL，账上直接丢了一笔；
      · 美股行情被错发到 OKX（NVDA 累计 785 次全失败），价格可能是别的源兜底。
    这些都不是"策略好不好的问题"，是"账本能不能信"的问题。

闸门（deny = 直接拒单，无法用参数绕过）：
    PREFLIGHT_LEDGER      账本对不平（R1/R2/R4 任一）
    PREFLIGHT_EQUITY      权益未按市价重估 / 权益 <= 0
    PREFLIGHT_MARK        该品种没有真实行情，或行情超过窗口（加密 120s / 股票 4h）
    PREFLIGHT_RUNS        RUNNING run 不唯一
    PREFLIGHT_MAX_OPEN    持仓数已达上限（默认 5）
    PREFLIGHT_LEVERAGE    这笔下去总名义会超过权益上限（默认 100%）
    PREFLIGHT_NOTIONAL    名义 <= 0 或 小于下限

命令：
    python3 trade_preflight.py --asset XAU --side BUY --notional 1000
    python3 trade_preflight.py --asset XAU --side BUY --notional 1000 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import account_ledger

CRYPTO_MAX_AGE_S = 120.0
STOCK_MAX_AGE_S = 4 * 3600.0
MIN_NOTIONAL = 100.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


_CLUSTERS: Dict[str, set] = {
    "crypto_major": {"BTC", "ETH", "SOL"},
    "crypto_equity": {"MSTR", "COIN", "MARA", "RIOT"},
    "metals": {"XAU", "XAG", "GOLD", "SILVER"},
    "energy": {"WTI", "OIL", "BRENT"},
    "us_tech": {"NVDA", "MSFT", "GOOGL", "AAPL", "AMZN", "META", "TSLA", "PLTR", "SMCI"},
    "memory": {"SKHYNIX", "US_HYNIX", "MU", "SAMSUNG"},
}


def cluster_of(asset: str) -> str:
    """相关性分桶：同一桶视为"同一个下注"（BTC+ETH、NVDA+MSFT、WTI+OIL…）。"""
    up = (asset or "").upper()
    for name, members in _CLUSTERS.items():
        if up in members:
            return name
    return up


def _mark_for(conn, asset: str) -> float:
    try:
        r = conn.execute("SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                         (asset,)).fetchone()
        if r and r[0]:
            return float(r[0])
    except Exception:
        pass
    return 0.0


def _mark_max_age(asset: str) -> float:
    """股票盘后允许旧价，加密/贵金属/原油必须近实时。"""
    try:
        from feeds import quote_sources
        r = quote_sources.route(asset)
        market = (r or {}).get("market", "unknown")
    except Exception:
        market = "unknown"
    if market == "yahoo":
        return _env_float("PREFLIGHT_STOCK_MAX_AGE_SEC", STOCK_MAX_AGE_S)
    if market == "domestic_spot":
        # v491：原油现货（新浪/腾讯 hf_CL）不是每 2 分钟一跳，原先按加密 120s 卡
        # 会把正常的原油单全部拒掉；给 10 分钟，仍然拒绝"没在更新"的价格。
        return _env_float("PREFLIGHT_COMMODITY_MAX_AGE_SEC", 600.0)
    return _env_float("PREFLIGHT_CRYPTO_MAX_AGE_SEC", CRYPTO_MAX_AGE_S)


def preflight(conn, asset: str, action: str,
              notional_usdt: float, now: Optional[int] = None) -> Dict[str, Any]:
    now = int(now or time.time())
    asset = str(asset or "").upper().strip()
    action = str(action or "").upper().strip()
    notional = float(notional_usdt or 0.0)
    denies: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    def deny(code: str, detail: str) -> None:
        # 同一原因可能被 R7 和"新单后杠杆"各命中一次，去重后只留一条
        if any(d["code"] == code for d in denies):
            return
        denies.append({"code": code, "detail": detail})

    # 1) 名义合法性
    if notional <= 0:
        deny("PREFLIGHT_NOTIONAL", f"名义 {notional:.2f} <= 0")
    elif notional < MIN_NOTIONAL:
        deny("PREFLIGHT_NOTIONAL", f"名义 {notional:.2f} 小于下限 {MIN_NOTIONAL:.0f} USDT")

    # 2) 账本对平 + 敞口/权益
    canonical = account_ledger.canonical(conn, now)
    issues = account_ledger.reconcile(conn, now)
    for item in issues:
        code = item["code"]
        if item["severity"] != "block":
            warnings.append({"code": code, "detail": item["detail"]})
            continue
        if code in ("R1_REALIZED_DRIFT", "R2_CASH_DRIFT", "R4_PNL_NULL"):
            deny("PREFLIGHT_LEDGER", f"{code}: {item['detail']}")
        elif code == "R3_EQUITY_STALE":
            deny("PREFLIGHT_EQUITY", f"{code}: {item['detail']}")
        elif code == "R6_DUP_RUNS":
            deny("PREFLIGHT_RUNS", f"{code}: {item['detail']}")
        elif code == "R7_LEVERAGE":
            deny("PREFLIGHT_LEVERAGE", f"{code}: {item['detail']}")
        elif code == "R5_MARK_MISSING":
            deny("PREFLIGHT_MARK", f"{code}: {item['detail']}")
        else:
            deny(f"PREFLIGHT_{code}", item["detail"])

    # 3) 权益必须为正
    if canonical["equity"] <= 0:
        deny("PREFLIGHT_EQUITY", f"权益 {canonical['equity']:.2f} <= 0")

    # 4) 本品种行情新鲜度
    row = None
    has_tick_table = True
    try:
        row = conn.execute(
            "SELECT price, ts FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (asset,),
        ).fetchone()
    except Exception:
        # 没有行情表的库（最小测试库/裸库）不是生产库：不假装有价，跳过这一项但留痕
        has_tick_table = False
        warnings.append({"code": "NO_TICK_TABLE",
                         "detail": "库内无 market_ticks 表，行情新鲜度未校验"})
    max_age = _mark_max_age(asset)
    if has_tick_table and (not row or not row["price"]):
        deny("PREFLIGHT_MARK", f"{asset} 没有真实 tick，禁止下单")
    elif has_tick_table:
        age = now - int(row["ts"])
        if age > max_age:
            deny("PREFLIGHT_MARK",
                 f"{asset} 最新价 {age}s 前（上限 {max_age:.0f}s），价格不可用于成交")
        elif age > max_age * 0.5:
            warnings.append({"code": "MARK_AGING",
                             "detail": f"{asset} 最新价 {age}s 前，接近上限 {max_age:.0f}s"})

    # 4.4) v492：先摸清这个品种现有方向，用来区分"开仓"与"平仓"
    has_long = has_short = False
    try:
        has_long = conn.execute(
            "SELECT 1 FROM t2_positions WHERE asset=? AND status='open' "
            "AND lower(side) IN ('long','buy') LIMIT 1", (asset,)).fetchone() is not None
        has_short = conn.execute(
            "SELECT 1 FROM t2_positions WHERE asset=? AND status='open' "
            "AND lower(side) IN ('short','sell') LIMIT 1", (asset,)).fetchone() is not None
    except Exception:
        has_long = has_short = True      # 查不到持仓表就不额外拦，避免误杀平仓
    is_entry = (action == "BUY" and not has_short) or (action in ("SELL", "SHORT") and not has_long)

    # 4.5) v491 交易规则：时段 / 裸空 / 整股
    try:
        import market_rules as _mr
    except Exception:
        _mr = None
    if _mr is not None:
        open_now, why = _mr.session_open(asset, now)
        if not open_now:
            deny("PREFLIGHT_MARKET_CLOSED", f"{why}，禁止下单")
        is_sell = action in ("SELL", "SHORT")
        if is_sell:
            allowed, why2 = _mr.allows_short(asset)
            if not allowed and not has_long:
                deny("PREFLIGHT_SHORT_FORBIDDEN", why2)
        if _mr.lot_size(asset) > 0 and row and row["price"]:
            qty_est = notional / float(row["price"])
            if qty_est < _mr.lot_size(asset):
                deny("PREFLIGHT_LOT_SIZE",
                     f"{asset} 按当前价只能买 {qty_est:.3f} 股，不足 1 股（最小单位 "
                     f"{_mr.lot_size(asset):.0f}）")

    # 5) 持仓数上限（同品种重复开仓由 execution_guards 负责）
    max_open = _env_int("MAX_OPEN_POSITIONS", 5)
    if canonical["n_open"] >= max_open:
        deny("PREFLIGHT_MAX_OPEN",
             f"持仓 {canonical['n_open']} 笔，已达上限 {max_open}")

    # 5.5) v492 资产簇集中度：相关性高的品种算同一个下注
    equity = canonical["equity"]
    cluster = cluster_of(asset)
    cluster_cap = _env_float("MAX_CLUSTER_WEIGHT_PCT", 80.0)
    if equity > 0 and cluster_cap > 0:
        cluster_now = 0.0
        try:
            for p in conn.execute(
                    "SELECT asset, qty, entry_price FROM t2_positions WHERE status='open'"):
                if cluster_of(p["asset"]) != cluster:
                    continue
                px = _mark_for(conn, p["asset"]) or float(p["entry_price"] or 0.0)
                cluster_now += abs(float(p["qty"] or 0.0) * px)
        except Exception:
            cluster_now = 0.0
        projected_cluster = (cluster_now + notional) / equity * 100.0
        if projected_cluster > cluster_cap + 0.1:
            deny("PREFLIGHT_CLUSTER",
                 f"{cluster} 桶新单后 {projected_cluster:.0f}% 权益（上限 {cluster_cap:.0f}%），"
                 f"这些品种涨跌同向，等于同一个下注")
        elif projected_cluster > cluster_cap * 0.85:
            warnings.append({"code": "CLUSTER_NEAR_CAP",
                             "detail": f"{cluster} 桶已占 {projected_cluster:.0f}%（上限 {cluster_cap:.0f}%）"})

    # 5.6) v492 连亏熔断：只拦"新开仓"，平仓一律放行
    streak_limit = _env_int("CONSECUTIVE_LOSS_LIMIT", 3)
    pause_min = _env_int("CONSECUTIVE_LOSS_PAUSE_MIN", 60)
    if is_entry and streak_limit > 0:
        try:
            recent = conn.execute(
                "SELECT realized_pnl, closed_ts FROM t2_positions "
                "WHERE status='closed' AND realized_pnl IS NOT NULL "
                "ORDER BY closed_ts DESC LIMIT ?", (streak_limit,)).fetchall()
        except Exception:
            recent = []
        if len(recent) == streak_limit and all(float(r["realized_pnl"]) < 0 for r in recent):
            last_ts = int(recent[0]["closed_ts"] or 0)
            if pause_min <= 0 or (now - last_ts) < pause_min * 60:
                deny("PREFLIGHT_LOSS_STREAK",
                     f"最近 {streak_limit} 笔全亏，冷却 {pause_min} 分钟后再开新仓"
                     f"（上一笔 {int((now - last_ts) / 60)} 分钟前）")
            else:
                warnings.append({"code": "LOSS_STREAK_PAST",
                                 "detail": f"最近 {streak_limit} 笔全亏，但已过冷却期"})

    # 6) 这笔下去后的总杠杆
    cap_pct = canonical["leverage_cap_pct"]
    if canonical["equity"] > 0:
        projected = canonical["gross_exposure"] + notional
        projected_pct = projected / canonical["equity"] * 100.0
        if projected_pct > cap_pct + 0.1:
            deny("PREFLIGHT_LEVERAGE",
                 f"新单后总名义 {projected:.0f} = 权益 {projected_pct:.0f}%，超过上限 {cap_pct:.0f}%")
        elif projected_pct > cap_pct * 0.9:
            warnings.append({"code": "LEVERAGE_NEAR_CAP",
                             "detail": f"新单后杠杆 {projected_pct:.0f}%（上限 {cap_pct:.0f}%）"})

    # 7) 当日亏损限额（T2 口径，不是 quick_sim 口径）
    limit = _env_float("DAILY_LOSS_LIMIT_USDT", 0.0)
    if limit > 0:
        lt = time.localtime(now)
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        day_open_equity = None
        if account_ledger._table_exists(conn, "account_snapshots"):
            row_e = conn.execute(
                "SELECT equity FROM account_snapshots WHERE ts<? ORDER BY ts DESC, rowid DESC LIMIT 1",
                (day_start,)).fetchone()
            if not (row_e and row_e[0]):
                # 今天还没有跨日锚点：用今天最早的一条快照近似"日初"
                row_e = conn.execute(
                    "SELECT equity FROM account_snapshots WHERE ts>=? ORDER BY ts ASC LIMIT 1",
                    (day_start,)).fetchone()
            if row_e and row_e[0]:
                day_open_equity = float(row_e[0])
        if day_open_equity is None:
            day_open_equity = float(canonical["initial_capital"])
        day_loss = day_open_equity - float(canonical["equity"])
        realized_today = 0.0
        if account_ledger._table_exists(conn, "t2_positions"):
            row_r = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM t2_positions "
                "WHERE status='closed' AND COALESCE(closed_ts,0)>=?",
                (day_start,)).fetchone()
            realized_today = float(row_r[0] or 0.0)
        if day_loss >= limit or realized_today <= -limit:
            deny("PREFLIGHT_DAILY_LOSS",
                 f"当日亏损 {max(day_loss, -realized_today):.2f} ≥ 限额 {limit:.2f}"
                 f"（日初权益 {day_open_equity:.2f} → 当前 {canonical['equity']:.2f}，"
                 f"今日已实现 {realized_today:+.2f}）")
        elif day_loss >= limit * 0.8:
            warnings.append({"code": "DAILY_LOSS_NEAR",
                             "detail": f"当日亏损 {day_loss:.2f}，接近限额 {limit:.2f}"})

    # 8) 同品种已有持仓（提示，不越权替 execution_guards 决策）
    same = [p for p in canonical["positions"] if p["asset"] == asset]
    if same:
        warnings.append({"code": "SAME_ASSET_OPEN",
                         "detail": f"{asset} 已有 {len(same)} 笔持仓 {[p['side'] for p in same]}"})

    return {
        "ok": not denies,
        "asset": asset,
        "action": action,
        "notional_usdt": round(notional, 2),
        "checked_at": now,
        "denies": denies,
        "warnings": warnings,
        "account": {
            "equity": canonical["equity"], "cash": canonical["cash"],
            "realized": canonical["realized"], "unrealized": canonical["unrealized"],
            "gross_exposure": canonical["gross_exposure"],
            "leverage_pct": canonical["leverage_pct"], "n_open": canonical["n_open"],
        },
        "mark": None if not row else {"price": float(row["price"]), "age_s": now - int(row["ts"])},
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="交易前置闸")
    ap.add_argument("--asset", required=True)
    ap.add_argument("--side", default="BUY")
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    conn = account_ledger.connect(a.db, readonly=True)
    try:
        out = preflight(conn, a.asset, a.side, a.notional)
    finally:
        conn.close()
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    acc = out["account"]
    print(f"账户 权益 {acc['equity']:.2f} · 现金 {acc['cash']:.2f} · 浮盈亏 {acc['unrealized']:+.2f} "
          f"· 杠杆 {acc['leverage_pct']:.0f}% · 持仓 {acc['n_open']}")
    print(f"下单 {out['action']} {out['asset']} {out['notional_usdt']:.0f} USDT → "
          f"{'允许' if out['ok'] else '拒绝'}")
    for d in out["denies"]:
        print(f"  ✗ {d['code']}: {d['detail']}")
    for w in out["warnings"]:
        print(f"  ! {w['code']}: {w['detail']}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
