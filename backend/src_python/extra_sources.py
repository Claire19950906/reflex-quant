#!/usr/bin/env python3
"""extra_sources.py — 三个「信息源」增补：市场隐含的恐慌/尾部定价、稳定币供给、事件概率。

为什么是这三个（而不是再买一份行情）:
  · 现有数据全是"已经发生"的价格与新闻，缺「市场自己在提前定价什么」。
    CBOE 的 VIX / SKEW 是市场花钱买出来的尾部风险定价，比任何内部指标都早。
  · 稳定币总量是加密市场最直接的流动性水位（进场钱的总池子）。
  · 预测市场（Polymarket）给事件的前瞻概率，是唯一"概率型"信息源。

数据口径（不许造假）:
  · 一律取官方/公开原始值，不做平滑、不补缺、不插值。
  · 取不到就写 status='unavailable' 并带错误原因，绝不写 0 或旧值。

来源与可达性（本机实测，2026-11-16）:
  · CBOE CSV：cdn.cboe.com，全历史（SKEW 1990 起），偶发 SSL 断连，重试 2-3 次可通；
  · DefiLlama：stablecoins.llama.fi，免费无 key，直连通；
  · Polymarket：gamma-api.polymarket.com，免费无 key，偶发握手失败，重试可通；
  · 三者都不需要付费 key。

用法:
  python3 src_python/extra_sources.py --fetch --commit   # 采一轮并落库
  python3 src_python/extra_sources.py --report           # 看库里各指标最新值
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BACKEND_DIR, "trident_event_bus.db")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
CBOE_BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/"
LLAMA_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=false"
PM_EVENT_URL = ("https://gamma-api.polymarket.com/events?closed=false"
                "&limit=12&tag_slug={tag}")

# 关注的事件关键词 → 归到系统能吃的主题（命中即留，不命中丢弃，不做语义猜测）
PM_TOPICS = {
    "fed": ("fed", "fomc", "rate cut", "rate hike", "interest rate"),
    "btc": ("bitcoin", "btc"),
    "eth": ("ethereum", "eth "),
    "gold": ("gold",),
    "oil": ("oil", "opec", "crude"),
    "recession": ("recession",),
    "cpi": ("cpi", "inflation"),
}


def _get(url: str, tries: int = 4, timeout: int = 15) -> Optional[bytes]:
    """带退避重试的 GET。本机网络对部分站点会间歇性 SSL 断连，重试是唯一稳定办法。"""
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (i + 1))
    print(f"[EXTRA-SRC] {url[:70]} 重试 {tries} 次仍失败: {last}", flush=True)
    return None


def _row(metric_key: str, value: Optional[float], unit: str, *, category: str, source: str,
         label: str = "", payload: Optional[Dict[str, Any]] = None,
         status: Optional[str] = None) -> Dict[str, Any]:
    p = dict(payload or {})
    if label:
        p["label"] = label
    return {
        "category": category,
        "source": source,
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": status or ("ok" if value is not None else "unavailable"),
        "payload": p,
    }


def _parse_cboe(body: bytes, value_col: int) -> Optional[Dict[str, Any]]:
    """CBOE 日度 CSV → 最新值 + 全历史分位 + 近期涨幅。"""
    text = body.decode("utf-8", "ignore")
    rows: List[tuple] = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) <= value_col:
            continue
        try:
            ts = int(time.mktime(time.strptime(rec[0].strip(), "%m/%d/%Y")))
            val = float(rec[value_col])
        except (ValueError, IndexError):
            continue
        rows.append((ts, val))
    if not rows:
        return None
    rows.sort()
    vals = [v for _, v in rows]
    latest_ts, latest = rows[-1]
    pct_rank = sum(1 for v in vals if v <= latest) / len(vals)
    n = len(vals)
    out: Dict[str, Any] = {
        "value": latest, "date": time.strftime("%Y-%m-%d", time.localtime(latest_ts)),
        "history_n": n, "pct_rank_all": round(pct_rank, 4),
    }
    for win, key in ((20, "chg_20d_pct"), (60, "chg_60d_pct")):
        if n > win and vals[-1 - win]:
            out[key] = round((latest / vals[-1 - win] - 1) * 100, 3)
        p_win = vals[-win:]
        if p_win:
            out[f"pct_rank_{win}d"] = round(sum(1 for v in p_win if v <= latest) / len(p_win), 4)
    out["series"] = [{"ts": int(ts), "value": float(v)} for ts, v in rows]  # 供一次性回填
    return out


def fetch_skew() -> Optional[Dict[str, Any]]:
    """CBOE SKEW（尾部风险定价）—— 系统里 `us_skew_index` 只有零星几行，这里补全历史。

    VIX 已由既有 `us_vix_cboe` 每天写入，这里不再重复写，避免两个口径打架。
    返回 {value, payload}；失败返回 None。落库走 external_indicators，指标名沿用 us_skew_index。
    """
    body = _get(CBOE_BASE + "SKEW_History.csv")
    parsed = _parse_cboe(body, 1) if body else None
    if not parsed:
        _report("cboe", False, "SKEW 取数失败")
        return None
    _report("cboe", True, f"skew={parsed['value']} pct_rank={parsed.get('pct_rank_all')}")
    return parsed


def fetch_stablecoins() -> List[Dict[str, Any]]:
    """稳定币总供给（USDT/USDC 等）—— 加密市场的流动性水位。"""
    body = _get(LLAMA_URL)
    if not body:
        _report("defillama", False, "stablecoins 取数失败")
        return [_row("stable_supply_usd", None, "USD", category="flow",
                     source="defillama", label="稳定币总供给", status="unavailable")]
    try:
        assets = (json.loads(body.decode("utf-8", "ignore")) or {}).get("peggedAssets") or []
    except Exception:
        _report("defillama", False, "JSON 解析失败")
        return [_row("stable_supply_usd", None, "USD", category="flow",
                     source="defillama", label="稳定币总供给", status="unavailable")]
    total = 0.0
    top: Dict[str, float] = {}
    for a in assets:
        circ = ((a.get("circulating") or {}).get("peggedUSD"))
        try:
            v = float(circ)
        except (TypeError, ValueError):
            continue
        total += v
        sym = str(a.get("symbol") or "")
        if sym:
            top[sym] = top.get(sym, 0.0) + v
    if not total:
        _report("defillama", False, "无有效 circulating 值")
        return [_row("stable_supply_usd", None, "USD", category="flow",
                     source="defillama", label="稳定币总供给", status="unavailable")]
    biggest = sorted(top.items(), key=lambda kv: -kv[1])[:4]
    _report("defillama", True, f"total={total/1e9:.1f}B")
    return [_row("stable_supply_usd", round(total / 1e9, 3), "十亿美元", category="flow",
                 source="defillama", label="稳定币总供给",
                 payload={"top": {k: round(v / 1e9, 2) for k, v in biggest},
                          "n_assets": len(assets)})]


def _pm_yes_price(market: Dict[str, Any]) -> Optional[float]:
    raw = market.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        return float(prices[0])
    except Exception:
        pass
    for key in ("lastTradePrice", "bestBid"):
        try:
            return float(market.get(key))
        except (TypeError, ValueError):
            continue
    return None


def fetch_predictions() -> List[Dict[str, Any]]:
    """预测市场里与交易主题相关的前瞻概率（Fed / 宏观 / 加密）。

    用 tag_slug 分主题拉 events（markets 端点按 volume 排序拿到的是一堆体育盘口，
    实测无效），再从每个 event 的 markets 里取「Yes」价作为隐含概率。
    """
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for tag in ("fed", "economy", "crypto"):
        body = _get(PM_EVENT_URL.format(tag=tag))
        if not body:
            continue
        try:
            events = json.loads(body.decode("utf-8", "ignore")) or []
        except Exception:
            continue
        for ev in events:
            ev_title = str(ev.get("title") or "")
            ev_n = 0
            for m in (ev.get("markets") or []):
                if ev_n >= 3:      # 同一事件最多留 3 个档位（否则「降几次息」会把槽位占满）
                    break
                q = str(m.get("question") or ev.get("title") or "")
                prob = _pm_yes_price(m)
                if prob is None or not q:
                    continue
                slug = str(m.get("slug") or m.get("id") or "")[:60]
                if slug in seen:
                    continue
                seen.add(slug)
                try:
                    vol = float(m.get("volumeNum") or m.get("volume") or 0)
                except (TypeError, ValueError):
                    vol = 0.0
                rows.append(_row(f"prob:{slug}", round(prob, 4), "probability",
                                 category="prediction", source="polymarket",
                                 label=q[:120],
                                 payload={"question": q[:160], "tag": tag,
                                          "event": ev_title[:120],
                                          "volume_usd": round(vol, 0),
                                          "url": f"https://polymarket.com/market/{slug}"}))
                ev_n += 1
    if rows:
        rows.sort(key=lambda r: -(r["payload"].get("volume_usd") or 0))
        rows = rows[:15]
        _report("polymarket", True, f"markets={len(rows)}")
    else:
        _report("polymarket", False, "无可用事件概率")
        rows = [_row("prob_watch", None, "probability", category="prediction",
                     source="polymarket", label="事件概率", status="unavailable")]
    return rows


def _ensure_registered() -> None:
    """把三家源登记到面板（幂等）；不依赖 api_server lifespan 的 register_known_sources。"""
    for sid, label, desc in (
        ("cboe", "CBOE 波动率/尾部风险", "VIX 与 SKEW 日度全历史（1990 起，官方 CSV；恐慌与尾部风险定价，免 key）"),
        ("defillama", "DefiLlama 稳定币供给", "稳定币总供给（加密流动性水位，免 key）"),
        ("polymarket", "Polymarket 事件概率", "Fed/宏观/加密事件的前瞻隐含概率（免 key）"),
    ):
        from feeds import source_status # 同函数内惰性导入：模块级导入会与本模块形成循环
        try:
            source_status.register(sid, category="宏观", label=label, description=desc)
        except Exception:
            pass


_ensure_registered()


def _report(source_id: str, ok: bool, detail: str) -> None:
    """把结果记进 source_status，面板上能看到这个源活着还是死了。"""
    try:
        from feeds import source_status
        source_status.report(source_id, ok, detail=detail[:160],
                             error="" if ok else detail[:160])
    except Exception:
        pass


def write_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = int(time.time())
    try:
        recent = {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                "SELECT metric_key, value, max(ts) FROM macro_snapshots "
                "WHERE category='prediction' AND ts >= ? GROUP BY metric_key, value",
                (now - 3600,),
            )
        }
    except sqlite3.Error:
        recent = {}
    rows = [r for r in rows
            if recent.get((r["metric_key"], r.get("value"))) is None]
    if not rows:
        return 0
    payload = [
        (now, r["category"], r["source"], r["metric_key"], r.get("value"),
         r.get("unit") or "", r.get("status") or "unavailable",
         json.dumps(r.get("payload") or {}, ensure_ascii=False))
        for r in rows
    ]
    cur = conn.executemany(
        "INSERT INTO macro_snapshots(ts, category, source, metric_key, value, unit, status, payload) "
        "VALUES(?,?,?,?,?,?,?,?)", payload)
    conn.commit()
    return int(cur.rowcount or 0)


def refresh(conn: Optional[sqlite3.Connection] = None, commit: bool = True,
            backfill: bool = False) -> Dict[str, Any]:
    """采一轮：CBOE SKEW（→external_indicators）+ 事件概率（→macro_snapshots）。

    稳定币供给已由 free_alt_sources.collect_stablecoins 覆盖（stable_supply_total 2000+ 行），
    这里不再重复采集，避免同一指标两个口径。
    """
    skew = fetch_skew()
    rows = fetch_predictions()
    if backfill and skew and skew.get("series"):
        try:
            import external_data
            external_data.ingest_external("US", "us_skew_index", skew["series"], source="cboe",
                                          meta={"unit": "index", "history_backfill": True})
            print(f"[EXTRA-SRC] SKEW 历史回填 {len(skew['series'])} 行", flush=True)
        except Exception as exc:
            print(f"[EXTRA-SRC] 回填失败: {type(exc).__name__}: {exc}", flush=True)
    ok = sum(1 for r in rows if r.get("status") == "ok") + (1 if skew else 0)
    result = {"skew": skew, "rows": rows, "ok": ok, "total": len(rows) + 1, "written": 0}
    if commit:
        written = 0
        owned = conn is None
        if owned:
            conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            if skew:
                try:
                    import external_data
                    external_data.ingest_external(
                        "US", "us_skew_index",
                        [{"ts": int(time.time()), "value": float(skew["value"])}],
                        source="cboe", meta={k: v for k, v in skew.items() if k != "value"})
                    written += 1
                except Exception as exc:
                    print(f"[EXTRA-SRC] skew 落库失败: {type(exc).__name__}: {exc}", flush=True)
            if rows:
                written += write_rows(conn, rows)
        finally:
            if owned:
                conn.close()
        result["written"] = written
    return result


def _report_db(conn: sqlite3.Connection) -> None:
    for cat in ("breadth", "flow", "prediction"):
        print(f"\n== {cat}")
        rows = conn.execute(
            "SELECT metric_key, value, unit, status, ts FROM macro_snapshots "
            "WHERE category=? ORDER BY ts DESC LIMIT 8", (cat,)).fetchall()
        for r in rows:
            print(f"  {r[0][:46]:46} {str(r[1])[:12]:12} {r[2][:8]:8} {r[3]:11} "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(int(r[4])))}")
        if not rows:
            print("  (空)")


def main() -> int:
    ap = argparse.ArgumentParser(description="CBOE / 稳定币 / 预测市场 三源采集")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="一次性回填 SKEW 全历史")
    ap.add_argument("--commit", action="store_true", help="落库；不带则只打印")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report or not args.fetch:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        _report_db(conn)
        conn.close()
        if not args.fetch:
            return 0

    res = refresh(commit=args.commit, backfill=args.backfill)
    if res.get("skew"):
        print(f"ok          us_skew_index  {res['skew']['value']}  index  "
              f"(全历史分位 {res['skew'].get('pct_rank_all')})")
    else:
        print("unavailable us_skew_index  None")
    for r in res["rows"]:
        print(f"{r['status']:11} {r['metric_key'][:40]:40} {str(r.get('value'))[:14]:14} "
              f"{r.get('unit') or ''}")
    print(f"\n合计 {res['total']} 条，ok {res['ok']} 条，落库 {res['written']} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
