#!/usr/bin/env python3
"""ohlcv_refresh_cn.py — 用国内可达源刷新 ohlcv_daily（替代已废弃的 yahoo/yfinance）。

背景（2026-11-15 实测）：
  · yahoo v8 / yfinance 在本机网络恒不可达（SSL handshake 超时 / 被限流），
    bridge 里那条 `_fetch_ohlcv_daily` 主源等于空转，日线因此停更 3 天。
  · 腾讯 `web.ifzq.gtimg.cn/appstock/app/usfqkline` 可取美股/美股指数日线（qfqday / day）。
  · 新浪全球期货 `market_data_fallback.fetch_sina_futures_daily` 可取 WTI/XAU/XAG。
  · Binance `fetch_binance_daily` 可取 BTC/ETH/SOL 等加密日线。

原则：只写真实取到的 OHLCV；取不到就不写（前端按缺失显示），绝不合成。
写库幂等：INSERT OR IGNORE（UNIQUE(asset,ts)），ts 为秒。

用法：
  python3 src_python/ohlcv_refresh_cn.py            # 全部可映射品种
  python3 src_python/ohlcv_refresh_cn.py NVDA,WTI   # 指定品种
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BACKEND_DIR, "trident_event_bus.db")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.8103.51 Safari/537.36")
_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
              "?param={sym},day,,,{n},qfq")

# 资产 → 腾讯代码（美股 .OQ/.N 后缀；指数用 usDJI / us.XXX）
TENCENT_SYMBOL: Dict[str, str] = {
    "NVDA": "usNVDA.OQ",
    "AAPL": "usAAPL.OQ",
    "MSFT": "usMSFT.OQ",
    "TSLA": "usTSLA.OQ",
    "MU": "usMU.OQ",
    "AMD": "usAMD.OQ",
    "GOOGL": "usGOOGL.OQ",
    "AMZN": "usAMZN.OQ",
    "META": "usMETA.OQ",
    "MSTR": "usMSTR.OQ",
    "DJI": "usDJI",
    "IXIC": "us.IXIC",
    "NDX": "usNDX",
    "SPX": "us.INX",
}
SINA_FUT = ("WTI", "XAU", "XAG")
CRYPTO = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA")

# 不做 A 股 / 韩股（用户口径）
SKIP_PREFIX = (".KS", ".KQ", ".SS", ".SZ")
SKIP_ASSETS = ("SKHYNIX", "US_HYNIX", "CN-SZC", "TENCENT", "A50")


def _fetch_tencent(sym: str, n: int = 400) -> List[Tuple[int, float, float, float, float, int]]:
    url = _KLINE_URL.format(sym=sym, n=n)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    import json as _json
    d = _json.loads(raw)
    node = (d.get("data") or {}).get(sym) or {}
    bars = node.get("qfqday") or node.get("day") or []
    out = []
    for b in bars:
        if len(b) < 6:
            continue
        try:
            ts = int(datetime.strptime(str(b[0]), "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp())
            o, c, h, l, v = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
        except Exception:
            continue
        if o <= 0 or c <= 0:
            continue
        out.append((ts, o, h, l, c, int(v)))
    return out


def _rows_for(asset: str) -> Tuple[List[tuple], str]:
    """返回 (写库行, 数据源名)。取不到返回空列表 —— 不合成。"""
    a = asset.upper()
    if a in TENCENT_SYMBOL:
        try:
            bars = _fetch_tencent(TENCENT_SYMBOL[a])
        except Exception as e:
            print(f"  {a}: tencent fail {type(e).__name__}: {str(e)[:60]}")
            bars = []
        if bars:
            return ([(a, ts, o, h, l, c, v, "tencent_refresh")
                     for ts, o, h, l, c, v in bars], "tencent")
    if a in SINA_FUT:
        try:
            from market import market_data_fallback as mdf
            bars = mdf.fetch_sina_futures_daily(a) or []
        except Exception as e:
            print(f"  {a}: sina futures fail {type(e).__name__}: {str(e)[:60]}")
            bars = []
        if bars:
            return ([(a, ts, o, h, l, c, v, "sina_futures_refresh")
                     for ts, o, h, l, c, v in bars], "sina_futures")
    if a in CRYPTO:
        try:
            from market import market_data_fallback as mdf
            bars = mdf.fetch_binance_daily(a) or []
        except Exception as e:
            print(f"  {a}: binance fail {type(e).__name__}: {str(e)[:60]}")
            bars = []
        if bars:
            return ([(a, ts, o, h, l, c, v, "binance_refresh")
                     for ts, o, h, l, c, v in bars], "binance")
    return ([], "")


def _max_ts(conn: sqlite3.Connection, asset: str, tries: int = 4) -> int:
    """读 MAX(ts)，遇到锁就重试。

    实测：本脚本在 commit 之后紧接着读 MAX(ts) 时会拿到 `database is locked`，
    整轮直接抛栈退出（launchd 退出码 2122），于是加密品种的日线就停在昨天。
    这不是"写不进去"，是 WAL 下读遇到 checkpoint 也会 BUSY —— 读同样要重试。
    """
    last = None
    for i in range(tries):
        try:
            return int(conn.execute(
                "SELECT MAX(ts) FROM ohlcv_daily WHERE asset=?", (asset,)).fetchone()[0] or 0)
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(3 * (i + 1))
    raise last


def _assets(argv_assets: Optional[List[str]]) -> List[str]:
    if argv_assets:
        return [a.strip().upper() for a in argv_assets if a.strip()]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        have = []
        for i in range(4):
            try:
                have = [r[0] for r in conn.execute("SELECT DISTINCT asset FROM ohlcv_daily")]
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or i == 3:
                    raise
                time.sleep(3 * (i + 1))
    finally:
        conn.close()
    keep = []
    for a in have:
        if any(a.endswith(p) for p in SKIP_PREFIX) or a in SKIP_ASSETS:
            continue
        if a in TENCENT_SYMBOL or a in SINA_FUT or a in CRYPTO:
            keep.append(a)
    return sorted(keep)


def main(argv: List[str]) -> int:
    assets = _assets([x for x in (argv[1].split(",") if len(argv) > 1 else [])])
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    total_new = 0
    print(f"[ohlcv_refresh_cn] 品种 {len(assets)}: {', '.join(assets)}")
    try:
        for a in assets:
            try:
              rows, src = _rows_for(a)
            except Exception as _fetch_exc:
                print(f"  {a}: 取行情失败（{_fetch_exc}），本轮跳过")
                continue
            if not rows:
                print(f"  {a}: 所有源都没拿到 -> 跳过（不写合成行）")
                continue
            try:
                # v366: 只落最近 14 天的 bar —— 原来每 30 分钟把 400 天历史整段重写，
                #   叠加"一日一行"触发器会反复 delete+insert，既拖慢又长期占着写锁。
                import time as _t2
                _cutoff = int(_t2.time()) - 14 * 86400
                rows = [r for r in rows if int(r[1]) >= _cutoff]
                before = _max_ts(conn, a)
                # v366: 写锁被别的常驻进程占住时别整轮崩掉 —— 退避重试 4 次
                cur = None
                for _attempt in range(4):
                    try:
                        cur = conn.executemany(
                            "INSERT OR IGNORE INTO ohlcv_daily "
                            "(asset,ts,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
                            rows)
                        conn.commit()
                        break
                    except sqlite3.OperationalError as _e:
                        conn.rollback()
                        if "locked" not in str(_e) or _attempt == 3:
                            print(f"  {a}: 写库失败（{_e}），本轮跳过")
                            cur = None
                            break
                        time.sleep(6 * (_attempt + 1))
                if cur is None:
                    continue
                new = cur.rowcount or 0
                total_new += new
                after = _max_ts(conn, a)
                tail = datetime.fromtimestamp(after, tz=timezone.utc).strftime("%Y-%m-%d")
                print(f"  {a:8} src={src:12} +{new:4}行  最新={tail}  ({before}->{after})")
                if src == "binance":
                    # Binance 对突发请求限流: 连发会返回空数组, 逐个之间留点间隔
                    time.sleep(1.5)
            except Exception as _write_exc:
                # 单品种锁死/异常不拖垮整轮；其余品种继续刷新
                print(f"  {a}: 写库异常（{_write_exc}），本轮跳过")
                continue
        print(f"[ohlcv_refresh_cn] 合计新增 {total_new} 行")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
