#!/usr/bin/env python3
"""口径漂移体检（v456）—— 防止"推导值悄悄跟供应商分家"。

为什么需要它：
  v454/v455 给 PB 与 A股估值补了「推导通道」（两个已公布真数相除）。首跑时东山精密
  推导 PE 24.08 / PB 4.99 与东财当期值**完全一致**，说明口径同源；但这是**一次**校验，
  不是持续保证。真实翻车形态很具体：供应商改字段名、改复权方式、改 TTM 口径 →
  推导序列要么归零、要么整体偏移 10%，而打分照旧跑，没人知道。港股首轮就是这么
  静默归零的（`REPORT_DATE` 写成 `REPORTDATE`，0 个点、0 报错）。

体检三问（任一 FAIL 就退出码 1）：
  1. 推导通道**有没有点**？点数为 0 = 通道死了（不是"刚好没数据"）。
  2. 推导出的**最新 PE/PB** 与供应商当期值偏差是否 ≤5%？
  3. 美化前的原始快照是否过期（>7 天没更新 = 抓取链断了）。

用法：
  python3 backend/scripts/fundamental_drift_check.py            # 打印 + 写报告
  python3 backend/scripts/fundamental_drift_check.py --days 30  # 只看近 30 天决策过的票
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import config
import db

_DRIFT_TOL = float(os.environ.get("TRIDENT_DRIFT_TOL", "0.05") or 0.05)
_FAIL_TOL = float(os.environ.get("TRIDENT_DRIFT_FAIL_TOL", "0.25") or 0.25)
_STALE_DAYS = int(os.environ.get("TRIDENT_DRIFT_STALE_DAYS", "7") or 7)
REPORT_DIR = BACKEND / "reports" / "fundamental_drift"


def _grade(drift: float) -> str:
    """三档判定。

    为什么不把 5% 以上一律判 FAIL：v456 首跑抓到 5 处偏差，逐个核对后结论分两类 ——
    · 真 bug（META 39.9%）：`_equity_asof` 按"已知日"排序，把**旧报表**当成最新一期
      （SEC 会把往年对比期塞进后来的申报），已被修成按期末排序，修后偏差 0.2%；
    · **供应商口径差异**（TSLA 22.0% / NVDA 16.4%）：我们的分母已用 SEC 官方值核对
      （TSLA 89.3B @2026-10-22 申报、NVDA 229.0B @2026-08-26 申报，与库内完全一致），
      是对方快照的净资产口径/期次与股价不同步。这类不该每天报警（会训练出"忽略告警"）。
    所以 5~25% 记 WARN 并留证据，>25% 才 FAIL —— 那才是"明显有一边错了"的量级。
    """
    if drift <= _DRIFT_TOL:
        return "OK"
    return "FAIL" if drift > _FAIL_TOL else "WARN"


def _pct(a: float, b: float) -> float:
    if b in (0, None):
        return float("inf")
    return abs(float(a) - float(b)) / abs(float(b))


def check_cn(conn: sqlite3.Connection) -> list:
    """A股/港股：推导序列 vs 东财当期快照。"""
    out = []
    pairs = conn.execute(
        "SELECT DISTINCT ticker, market FROM cn_equity_fundamentals").fetchall()
    for ticker, market in pairs:
        derived = conn.execute(
            """SELECT pe, pb, ts FROM cn_valuation_snapshots
               WHERE ticker=? AND market=? AND source='derived'
               ORDER BY ts DESC LIMIT 1""", (ticker, market)).fetchone()
        vendor = conn.execute(
            """SELECT pe, pb, ts FROM cn_valuation_snapshots
               WHERE ticker=? AND market=? AND source!='derived'
               ORDER BY ts DESC LIMIT 1""", (ticker, market)).fetchone()
        row = {"market": "CN", "ticker": ticker, "side": market}
        if not derived:
            row.update(status="FAIL", reason="推导通道 0 点（通道死了，不是没数据）",
                       derived_pe=None, vendor_pe=(vendor[0] if vendor else None))
            out.append(row)
            continue
        d_pe, d_pb, d_ts = derived[0], derived[1], derived[2]
        v_pe, v_pb = (vendor[0], vendor[1]) if vendor else (None, None)
        row.update(derived_ts=int(d_ts), vendor_pe=v_pe, derived_pe=d_pe)
        checks = []
        if d_pe and v_pe:
            checks.append(("pe", _pct(d_pe, v_pe)))
        if d_pb and v_pb:
            checks.append(("pb", _pct(d_pb, v_pb)))
        if not checks:
            row.update(status="WARN", reason="两边没有可比的 PE/PB（亏损或缺快照）")
            out.append(row)
            continue
        worst_k, worst = max(checks, key=lambda kv: kv[1])
        row.update(worst_field=worst_k, worst_drift=round(worst, 4),
                   status=_grade(worst),
                   reason=(f"{worst_k} 偏差 {worst * 100:.1f}%"
                           + ("（>5% 提示差异，>25% 才报警）" if worst > _DRIFT_TOL else "")))
        out.append(row)
    return out


def check_us(conn: sqlite3.Connection) -> list:
    """美股：PB 推导通道（股本×收盘 ÷ 净资产）vs 快照 eq.pb。"""
    out = []
    try:
        import equity_fundamentals as ef
    except Exception as exc:
        return [{"market": "US", "ticker": "*", "status": "WARN",
                 "reason": f"import 失败: {type(exc).__name__}"}]
    tickers = sorted(getattr(config, "EQUITY_TICKERS", ()) or ())
    for tk in tickers:
        row = {"market": "US", "ticker": tk}
        try:
            hist = ef.pb_history(tk, days=365, conn=conn)
        except Exception as exc:
            row.update(status="WARN", reason=f"pb_history 出错: {type(exc).__name__}")
            out.append(row)
            continue
        if not hist:
            row.update(status="WARN", reason="PB 无历史（可能缺股本/收盘/净资产）")
            out.append(row)
            continue
        derived_pb = hist[-1][1]
        snap = conn.execute(
            """SELECT value, ts FROM macro_snapshots
               WHERE metric_key='eq.pb' AND value IS NOT NULL AND payload LIKE ?
               ORDER BY ts DESC LIMIT 1""", (f'%"label": "{tk}"%',)).fetchone()
        row.update(derived_pb=round(float(derived_pb), 2),
                   vendor_pb=(round(float(snap[0]), 2) if snap else None))
        # 诊断：把偏差拆成"净资产差多少" —— 供人工判断是期次还是口径
        try:
            caps = ef._derived_caps(conn, tk, 365)
            mktcap = list(caps.values())[-1] if caps else None
            rows_b = ef._balance_rows(conn, tk)
            equity, basis = ef._equity_asof(rows_b, time.strftime("%Y-%m-%d"))
            period = max((str(r[1])[:10] for r in rows_b), default=None) if rows_b else None
            row.update(our_equity_b=(round(equity / 1e9, 1) if equity else None),
                       equity_basis=basis, equity_period=period)
            if mktcap and snap and snap[0]:
                row["implied_vendor_equity_b"] = round(mktcap / float(snap[0]) / 1e9, 1)
        except Exception:
            pass
        if snap and snap[0]:
            drift = _pct(derived_pb, snap[0])
            row.update(worst_field="pb", worst_drift=round(drift, 4), status=_grade(drift),
                       reason=(f"pb 偏差 {drift * 100:.1f}%：我方分母 {row.get('our_equity_b')}B"
                               f"（{row.get('equity_basis')}，期末 {row.get('equity_period')}）"
                               f" vs 供应商隐含 {row.get('implied_vendor_equity_b')}B"
                               if drift > _DRIFT_TOL else ""))
        else:
            row.update(status="OK", reason="仅有推导通道（快照未采到 eq.pb，无可比）")
        out.append(row)
    return out


def check_freshness(conn: sqlite3.Connection) -> list:
    """原始快照是否还在更新（抓取链断掉也会让分位慢慢失真）。"""
    out = []
    now = int(time.time())
    for key, label in (("eq.pe", "美股 PE 快照"), ("eq.pb", "美股 PB 快照")):
        try:
            snap = conn.execute(
                "SELECT MAX(ts) FROM macro_snapshots WHERE metric_key=? AND value IS NOT NULL",
                (key,)).fetchone()
            last = int(snap[0] or 0)
        except Exception:
            last = 0
        stale = (now - last) // 86400 if last else None
        out.append({"market": "FEED", "ticker": key, "label": label,
                    "last_ts": last, "stale_days": stale,
                    "status": ("FAIL" if (stale is None or stale > _STALE_DAYS) else "OK"),
                    "reason": "" if stale is not None and stale <= _STALE_DAYS
                    else f"{label} {stale if stale is not None else '永'}天未更新"})
    return out


def _yf_pb(ticker: str) -> dict:
    """第三方（yfinance）PB，作为"我方 vs 供应商"之争的裁判。

    本机 yfinance 的 crumb/SSL 间歇失败（见 peer_valuation 的同类处理），
    所以重试 3 次；仍拿不到就返回 {}，让结论写"第三方不可得"，不猜。
    """
    for attempt in range(3):
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info or {}
            pb = info.get("priceToBook")
            bv = info.get("bookValue")
            mcap = info.get("marketCap")
            if pb or (bv and mcap):
                return {"pb": float(pb) if pb else None,
                        "book_value_per_share": float(bv) if bv else None,
                        "market_cap_b": round(float(mcap) / 1e9, 1) if mcap else None}
        except Exception:
            time.sleep(1.0 + attempt)
    return {}


def _yf_symbol(market: str, ticker: str) -> str:
    if market == "HK":
        return ticker.replace(".HK", "") + ".HK"
    if market == "CN":
        return ticker.replace(".SZ", ".SZ").replace(".SH", ".SS")
    return ticker


def cross_check_warns(rows: list) -> list:
    """对每条 WARN 拉第三方 PB，判"到底谁错"。

    三方：ours（我方推导）/ vendor（当期快照）/ third（yfinance）。
    判读规则（容差同主流程 5%）：
      · third ≈ ours  → 我方对，供应商快照偏（可继续用，但要标口径）
      · third ≈ vendor → 我方偏（真问题，要修）
      · 三者互不接近 → 无法判定，并列展示（不强行下结论）
    """
    out = []
    for row in rows:
        if row.get("status") != "WARN":
            continue
        ticker = row.get("ticker")
        market = row.get("market", "US")
        if not ticker or ticker == "*":
            continue
        ours = row.get("derived_pb")
        vendor = row.get("vendor_pb")
        info = _yf_pb(_yf_symbol(market, ticker))
        third = info.get("pb")
        item = {"ticker": ticker, "market": market, "ours": ours,
                "vendor": vendor, "third_party_pb": round(third, 2) if third else None,
                "third_party_source": "yfinance priceToBook"}
        if third and ours and vendor:
            d_ours = _pct(ours, third)
            d_vendor = _pct(vendor, third)
            if d_ours <= _DRIFT_TOL and d_vendor > _DRIFT_TOL:
                item["verdict"] = "ours_ok_vendor_off"
                item["detail"] = f"我方 vs 第三方差 {d_ours * 100:.1f}%，供应商 vs 第三方差 {d_vendor * 100:.1f}%"
            elif d_vendor <= _DRIFT_TOL and d_ours > _DRIFT_TOL:
                item["verdict"] = "vendor_ok_ours_off"
                item["detail"] = f"供应商 vs 第三方差 {d_vendor * 100:.1f}%，我方差 {d_ours * 100:.1f}% → 我们的推导有问题"
            elif d_ours <= _DRIFT_TOL and d_vendor <= _DRIFT_TOL:
                item["verdict"] = "both_close"
                item["detail"] = "三方都在容差内（原 WARN 可能来自时点差异）"
            else:
                item["verdict"] = "disagree"
                item["detail"] = f"三方互不接近（我方差 {d_ours * 100:.1f}%，供应商差 {d_vendor * 100:.1f}%）"
        elif third:
            item["verdict"] = "partial"
            item["detail"] = (f"只有第三方 {item['third_party_pb']} 可比"
                              f"（我方 {ours} / 供应商 {vendor}）")
        else:
            item["verdict"] = "third_party_unavailable"
            item["detail"] = "yfinance 取不到 PB（本机 crumb/SSL 间歇失败）—— 保持 WARN，不猜"
        out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="基本面口径漂移体检（只读）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--no-write", action="store_true", help="不写报告文件")
    ap.add_argument("--warn-check", action="store_true",
                    help="对 WARN 逐条拉第三方 PB 交叉验证（会走网络，约 10~20 秒）")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        cn = check_cn(conn)
        us = check_us(conn)
        feed = check_freshness(conn)
    finally:
        conn.close()

    rows = cn + us + feed
    fails = [r for r in rows if r.get("status") == "FAIL"]
    warns = [r for r in rows if r.get("status") == "WARN"]
    # 交叉验证：把"我方 vs 供应商谁对"变成"三方表决"，只有 WARN 才值得花网络请求
    cross = cross_check_warns(warns) if args.warn_check else []
    payload = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tolerance": _DRIFT_TOL,
        "total": len(rows),
        "ok": len([r for r in rows if r.get("status") == "OK"]),
        "warn": len(warns),
        "fail": len(fails),
        "verdict": "fail" if fails else ("warn" if warns else "ok"),
        "warn_cross_check": cross,
        "rows": rows,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"[口径体检] {payload['checked_at']}  容差 {_DRIFT_TOL * 100:.0f}%")
        for r in rows:
            mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}[r.get("status", "WARN")]
            extra = ""
            if r.get("worst_drift") is not None:
                extra = f" 偏差 {r['worst_drift'] * 100:.1f}%"
            print(f"  {mark} {r['market']}/{r['ticker']}{extra} {r.get('reason') or ''}")
        print(f"[结论] OK {payload['ok']} / WARN {payload['warn']} / FAIL {payload['fail']}"
              f" → {payload['verdict']}")
        if cross:
            print("\n[第三方交叉验证] （我方 / 供应商 / yfinance）")
            for item in cross:
                print(f"  {item['market']}/{item['ticker']:10s} 我方 {item['ours']}"
                      f" | 供应商 {item['vendor']} | 第三方 {item['third_party_pb']}"
                      f"  → {item['verdict']}")
                print(f"      {item['detail']}")

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
