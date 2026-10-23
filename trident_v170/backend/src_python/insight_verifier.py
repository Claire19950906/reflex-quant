#!/usr/bin/env python3
"""R5 路径 A：insight 事后因果验证器。

v282+: HONESTY 边界 — 绝不允许捏造数据/结论。详见 _honesty.NEVER_FABRICATE_INVARIANT
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import db
# v282+: 引用诚实宪法真理源 — 任何 _PLACEHOLDER_ 已被 _is_flat_fake_bar 取代
# v283: 改用绝对导入 — tests 把 src_python 加到 sys.path 后用 top-level import,
#       相对导入会 ImportError;绝对导入双兼容
import _honesty  # noqa: F401
NEVER_FABRICATE_INVARIANT = _honesty.NEVER_FABRICATE_INVARIANT

_PARAM_PREFIXES = (
    "sizing.", "score.", "filter.",
    "[sizing.", "[score.", "[filter.",
)

_EVENT_KEYWORDS = (
    "\u4e2d\u4e1c", "\u5730\u7f18", "\u80e1\u585e", "\u88ad\u51fb", "\u5236\u88c1", "\u52a0\u606f", "\u964d\u606f",
    "CPI", "\u975e\u519c", "FOMC", "GDP", "ETF", "SEC", "ETP",
    "WTI", "XAU", "\u9ec4\u91d1", "\u539f\u6cb9", "BTC", "ETH", "SOL",
    "FED", "\u592e\u884c", "OPEC", "\u6218\u4e89", "\u51b2\u7a81", "\u505c\u706b",
    "crypto", "\u52a0\u5bc6", "lever", "\u6760\u6746", "AI", "\u7ecf\u6d4e",
    "geopolitical", "verdict", "market_event",
)

_TIME_KEYWORDS = (
    "\u65f6\u6bb5", "\u65e9\u76d8", "\u665a\u76d8", "\u591c\u76d8", "\u5f00\u76d8", "\u6536\u76d8",
    "\u51cc\u6668", "\u4e0a\u5348", "\u4e0b\u5348", "03-", "06:", "0-", "24h", "UTC",
    "\u9694\u591c", "\u5de5\u4f5c\u65e5", "\u5468\u672b",
)


def parse_insight_type(rule):
    r = rule.lower()
    has_param = any(r.startswith(p.lower()) for p in _PARAM_PREFIXES)
    has_event = any(kw in rule for kw in _EVENT_KEYWORDS)
    has_time = any(kw in rule for kw in _TIME_KEYWORDS)
    if has_param and not (has_event or has_time):
        return "param"
    if has_event and not has_param:
        return "event"
    if has_time and not (has_event or has_param):
        return "time"
    if has_param and (has_event or has_time):
        return "mixed"
    return "unknown"


# v252.2: 不再硬编码"已知假样本"价格 — 金价随时可能涨到 4139 附近,
# 按 close==4139.28 过滤会误伤真实黄金 K 线。
# 改为检测"扁平假 K 线"特征: open=high=low=close (一根 bar 振幅=0)。
# 这种 bar 出现在历史 quote_sources._xau_xag_spot_fallback 的合成 bug 里,
# 已在 v176+ 停用,但 db 里可能仍有遗留。
_NOISE_AMP_PCT = 0.05


def _is_flat_fake_bar(d: Dict[str, Any]) -> bool:
    """检测扁平假 K 线: open==high==low==close (合成占位 bar)."""
    try:
        o, h, lo, c = float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"])
        # 容许 0.01 浮点误差
        return abs(o - h) < 0.01 and abs(h - lo) < 0.01 and abs(lo - c) < 0.01
    except (KeyError, TypeError, ValueError):
        return False


def _fetch_kline_window(conn, asset, start_ts, end_ts, timeframe="1h"):
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM kline_bars "
        "WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ? "
        "AND (high - low) > 0 ORDER BY ts ASC",
        (asset, timeframe, start_ts, end_ts),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if _is_flat_fake_bar(d):
            # 扁平假 K 线(open=high=low=close)跳过
            continue
        out.append(d)
    return out


def compute_kline_direction(klines, asset):
    if not klines:
        return {"direction": "no_data", "pct": 0.0, "amp_pct": 0.0, "n": 0}
    first_open = klines[0]["open"]
    last_close = klines[-1]["close"]
    max_high = max(k["high"] for k in klines)
    min_low = min(k["low"] for k in klines)
    pct = (last_close - first_open) / first_open * 100
    amp_pct = (max_high - min_low) / first_open * 100
    direction = "flat"
    if amp_pct < _NOISE_AMP_PCT:
        direction = "noise"
    elif pct > 0.05:
        direction = "up"
    elif pct < -0.05:
        direction = "down"
    return {"direction": direction, "pct": round(pct, 4),
            "amp_pct": round(amp_pct, 4), "n": len(klines)}


def find_related_trades(conn, asset, ts_lo_ms, ts_hi_ms):
    # quick_sim_trades.entry_ts 是秒级（kline_bars.ts 是毫秒级，注意区分）
    # 复用前要把入口从 ms 转为 sec，否则 1000x 单位不匹配导致永远查不到
    ts_lo_s = int(ts_lo_ms // 1000)
    ts_hi_s = int(ts_hi_ms // 1000)
    rows = conn.execute(
        "SELECT id, decision_id, news_id, asset, action, score, "
        "entry_price, exit_price, max_price, min_price, "
        "entry_ts, exit_ts, pnl_pct, pnl_usdt, verdict "
        "FROM quick_sim_trades WHERE asset = ? AND entry_ts >= ? AND entry_ts <= ? "
        "AND settled = 1 ORDER BY entry_ts ASC",
        (asset, ts_lo_s, ts_hi_s),
    ).fetchall()
    return [dict(r) for r in rows]


def _parse_insight_ts(created_at):
    try:
        return int(time.mktime(time.strptime(created_at, "%Y-%m-%d %H:%M:%S")) * 1000)
    except Exception:
        return int(time.time() * 1000) - 7 * 24 * 3600 * 1000


def verify_insight_with_trades(insight, conn):
    asset = insight.get("asset", "").upper()
    if not asset:
        return {"method": "trades", "applicable": False, "reason": "no_asset"}
    ts0 = _parse_insight_ts(insight["created_at"])
    windows = [(24, "24h"), (48, "48h"), (72, "72h")]
    out = []
    seen_ids = set()
    unique_trades: List[dict] = []
    for hours, label in windows:
        ts1 = ts0 + hours * 3600 * 1000
        trades = find_related_trades(conn, asset, ts0, ts1)
        if not trades:
            out.append({"window": label, "n": 0, "win_rate": None, "avg_pnl": None})
            continue
        # 收集独立交易（跨窗口去重）
        for t in trades:
            tid = t.get("id")
            if tid is not None and tid not in seen_ids:
                seen_ids.add(tid)
                unique_trades.append(t)
        n = len(trades)
        wins = sum(1 for t in trades if t.get("verdict") == "WIN")
        win_rate = wins / n if n else 0
        avg_pnl = sum(t.get("pnl_pct") or 0 for t in trades) / n
        out.append({"window": label, "n": n, "win_rate": round(win_rate, 3),
                    "avg_pnl": round(avg_pnl, 4)})
    # 用独立交易数计算 n_total（避免 24h+48h+72h 窗口重叠虚增样本）
    n_unique = len(unique_trades)
    if n_unique:
        wins_u = sum(1 for t in unique_trades if t.get("verdict") == "WIN")
        win_rate_u = wins_u / n_unique
        avg_pnl_u = sum(t.get("pnl_pct") or 0 for t in unique_trades) / n_unique
    else:
        win_rate_u = None
        avg_pnl_u = None
    return {"method": "trades", "applicable": True, "asset": asset,
            "windows": out, "n_total": n_unique,
            "win_rate_unique": round(win_rate_u, 3) if win_rate_u is not None else None,
            "avg_pnl_unique": round(avg_pnl_u, 4) if avg_pnl_u is not None else None}


def verify_insight_with_kline(insight, conn, horizons_hours=(24, 48, 72)):
    asset = insight.get("asset", "").upper()
    if not asset:
        return {"method": "kline", "applicable": False, "reason": "no_asset"}
    ts0 = _parse_insight_ts(insight["created_at"])
    out = []
    for h in horizons_hours:
        ts1 = ts0 + h * 3600 * 1000
        klines = _fetch_kline_window(conn, asset, ts0, ts1, "1h")
        dir_info = compute_kline_direction(klines, asset)
        out.append({"horizon_h": h, **dir_info})
    return {"method": "kline", "applicable": True, "asset": asset, "horizons": out}


def _detect_insight_direction(rule):
    """从 rule 文本推断 insight 隐含方向。

    - buy   : 主张做多（BUY/买/多/做多 出现次数占优）
    - sell  : 主张做空（SELL/卖/空/做空 出现次数占优）
    - neutral: 无明确方向（风控/覆盖度/区间规则等）
    """
    r = rule.upper()
    buy_hits = sum(1 for kw in ("BUY", "做多", "买", "LONG", "多") if kw in r)
    sell_hits = sum(1 for kw in ("SELL", "做空", "卖", "SHORT", "空") if kw in r)
    if buy_hits > sell_hits:
        return "buy"
    if sell_hits > buy_hits:
        return "sell"
    return "neutral"


def _score_from_kline(kline_verif, rule=""):
    """按 insight 方向对 K 线走势打分。

    - buy   insight → 市场涨(up)即得分
    - sell  insight → 市场跌(down)即得分
    - neutral       → 市场震荡(noise/flat)即得分（保守规则成立）
    """
    if not kline_verif.get("applicable"):
        return 0.5
    horizons = kline_verif.get("horizons", [])
    if not horizons:
        return 0.5
    valid = [h for h in horizons if h.get("n", 0) > 0]
    if not valid:
        return 0.5
    direction = _detect_insight_direction(rule)
    favorable = {"buy": ("up",), "sell": ("down",), "neutral": ("noise", "flat")}[direction]
    matches = sum(1 for h in valid if h.get("direction") in favorable)
    return matches / len(valid)


def _score_from_trades(trade_verif):
    if not trade_verif.get("applicable"):
        return None
    windows = trade_verif.get("windows", [])
    rates = [w["win_rate"] for w in windows if w.get("win_rate") is not None]
    if not rates:
        return None
    return 1.0 - (sum(rates) / len(rates))


def compute_real_signal_pct(insight, kline_verif, trade_verif):
    insight_type = parse_insight_type(insight.get("rule", ""))
    if insight_type == "param":
        return {"verdict": "param_rule", "real_signal_pct": None,
                "reason": "参数规则（sizing/score/filter），无法做 K 线因果验证"}
    if insight_type == "time":
        return {"verdict": "time_rule", "real_signal_pct": None,
                "reason": "时段规则，需按小时分布特殊验证"}
    kline_score = _score_from_kline(kline_verif, insight.get("rule", ""))
    trade_score = _score_from_trades(trade_verif)
    sample_n = trade_verif.get("n_total", 0)
    # v170.5+: 收紧 sample_conf 饱和门槛（之前 30 → 现在 60 唯一交易），
    # 防止 13 笔真实交易 + 三窗口重叠虚增成 33 笔 → composite 直接通过 verified
    sample_conf = min(sample_n / 60.0, 1.0) if sample_n else 0.0
    if trade_verif.get("applicable") and trade_verif.get("n_total", 0) > 0:
        composite = 0.6 * kline_score + 0.4 * trade_score
    else:
        composite = kline_score
        sample_conf = 0.3
    real_signal_pct = round(composite * sample_conf, 3)
    if real_signal_pct >= 0.7:
        verdict = "verified"
    elif real_signal_pct >= 0.1:
        verdict = "partial"
    elif sample_n == 0 and kline_verif.get("horizons") and all(h.get("n", 0) == 0 for h in kline_verif["horizons"]):
        verdict = "unknown_no_data"
    else:
        verdict = "failed"
    return {"verdict": verdict, "real_signal_pct": real_signal_pct,
            "sample_n": sample_n, "sample_conf": round(sample_conf, 3),
            "kline_score": round(kline_score, 3),
            "trade_score": round(trade_score, 3) if trade_score is not None else None}


def verify_all_insights(active_only=True, conn=None):
    own_conn = conn is None
    if conn is None:
        conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    where = "WHERE active = 1" if active_only else ""
    rows = conn.execute(
        f"SELECT id, rule, asset, scope, active, source_samples, origin, "
        f"kind, counter_factual, created_at, manual_deactivated "
        f"FROM review_insights {where} ORDER BY id ASC"
    ).fetchall()
    insights = [dict(r) for r in rows]
    results = []
    for ins in insights:
        kline_verif = verify_insight_with_kline(ins, conn)
        trade_verif = verify_insight_with_trades(ins, conn)
        score = compute_real_signal_pct(ins, kline_verif, trade_verif)
        results.append({
            "id": ins["id"], "asset": ins.get("asset", ""),
            "origin": ins.get("origin", ""), "source_samples": ins.get("source_samples", 0),
            "insight_type": parse_insight_type(ins["rule"]),
            "rule_short": ins["rule"][:80], "created_at": ins["created_at"],
            **score, "kline_verif": kline_verif, "trade_verif": trade_verif,
        })
    if own_conn:
        conn.close()
    return results


def rank_insights_by_confidence(results):
    def keyf(x):
        v = x.get("real_signal_pct")
        return (1 if v is None else 0, -v if v is not None else 0)
    return sorted(results, key=keyf)


def _format_report(results):
    lines = ["=" * 100, "R5 路径 A：insight 深度因果验证报告", "=" * 100]
    type_counts = {}
    for r in results:
        t = r.get("insight_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    lines.append("\n[1] insight 类型分布：")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        lines.append(f"    {t:12s}: {n}")
    verdict_counts = {}
    for r in results:
        v = r.get("verdict", "unknown")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    lines.append("\n[2] 验证 verdict 分布：")
    for v, n in sorted(verdict_counts.items(), key=lambda x: -x[1]):
        lines.append(f"    {v:20s}: {n}")
    ranked = rank_insights_by_confidence(results)
    top_verified = [r for r in ranked if r.get("verdict") == "verified"][:10]
    lines.append(f"\n[3] Top {len(top_verified)} verified insights：")
    for r in top_verified:
        lines.append(f"    #{r['id']:3d} {r.get('real_signal_pct', 0):.3f} "
                     f"{r.get('asset',''):4s} {r.get('rule_short','')[:60]}")
    top_failed = [r for r in ranked if r.get("verdict") == "failed"][:10]
    lines.append(f"\n[4] Top {len(top_failed)} failed insights：")
    for r in top_failed:
        lines.append(f"    #{r['id']:3d} {r.get('real_signal_pct', 0):.3f} "
                     f"{r.get('asset',''):4s} {r.get('rule_short','')[:60]}")
    no_data = [r for r in results if r.get("verdict") == "unknown_no_data"]
    lines.append(f"\n[5] 数据不足 insights: {len(no_data)} 条（无法做 K 线因果验证）")
    lines.append("    这些 insight 需要先扩展 paper trading 样本量才能验证")
    lines.append("\n[6] 数据基础警告：")
    # 动态查询真实 trades / kline 占位比例（避免硬编码过期数据）
    try:
        import sqlite3 as _sqlite3
        import config as _config
        _db = _sqlite3.connect(_config.DB_PATH)
        _c = _db.cursor()
        _c.execute("SELECT COUNT(*), SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) FROM quick_sim_trades")
        _total, _settled = _c.fetchone()
        _c.execute("SELECT COUNT(*) FROM kline_bars WHERE symbol='XAU' AND timeframe='1h' AND ABS(close-4139.28) < 0.01")
        _xau_ph = _c.fetchone()[0]
        _c.execute("SELECT COUNT(*) FROM kline_bars WHERE symbol='XAU' AND timeframe='1h'")
        _xau_total = _c.fetchone()[0]
        _xau_ph_pct = round(100 * _xau_ph / max(_xau_total, 1), 1)
        _db.close()
        lines.append(f"    - quick_sim_trades 总 {_total} 条（已 settled {_settled} 条），流式实时写入中")
        lines.append(f"    - kline_bars XAU 1h 占位 {_xau_ph_pct}%（4139.28 是国内现货兜底合成值），真实 OKX swap 价格 ≈ 4146-4148")
    except Exception:
        lines.append("    - quick_sim_trades / kline_bars 状态查询失败（不影响主流程）")
    lines.append("    - 部分 insights 仍为 param_rule（sizing/score/filter），无法做 K 线因果验证")
    lines.append("    - 路径 A 当前阶段最大瓶颈：样本量 + kline 长期稳定（OKX swap 偶尔失败）")
    return "\n".join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description="R5 路径 A：insight 深度因果验证")
    p.add_argument("--output", "-o", default="/tmp/insight_verification_report.txt")
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-inactive", action="store_true")
    args = p.parse_args()
    results = verify_all_insights(active_only=not args.include_inactive)
    report = _format_report(results)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[报告已写入] {args.output}")
    if args.json:
        json_path = args.output.replace(".txt", ".json")
        Path(json_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[JSON 已写入] {json_path}")


if __name__ == "__main__":
    main()
