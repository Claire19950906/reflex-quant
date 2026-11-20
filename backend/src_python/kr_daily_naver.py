#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kr_daily_naver.py — 用 Naver Finance 补韩股日线（当前目标：000660.KS / SK海力士）

为什么另找源
--------------
000660.KS 在库里只有 33 根（2026-09-27 起），技术指标/慢异动/回测对它基本空白。
两条既有通道都不通：
  · yfinance `yf.download("000660.KS", period="max")` → "possibly delisted; no price data"
  · Yahoo chart API 能返回，但本机 IP 被限流（HTTP 429 持续 9 小时未恢复）

可用通道：Naver Finance（韩国本土行情站，不需要 key、不需要代理）
    https://api.finance.naver.com/siseJson.naver?symbol=000660&requestType=1
        &startTime=20100101&endTime=YYYYMMDD&timeframe=day
返回的是"伪 JSON"（单引号 + 韩文表头 + 行尾逗号），不能直接 json.loads，
用正则逐行抠数字。列顺序（韩文表头确认过）：
    [日期, 시가(开), 고가(高), 저가(低), 종가(收), 거래량(量), 외국인소진율]
注意这里是 **开/高/低/收**，与腾讯的"开/收/高/低"不同，写错会让 high<low 全被判非法。

落库路径与既有日线一致：
    kline_bars(symbol,'1d',ts=韩国收盘 15:30 KST 即 06:30 UTC 毫秒) → ohlcv_daily

用法：
    python3 kr_daily_naver.py                    # dry-run，默认 000660.KS
    python3 kr_daily_naver.py --apply
    python3 kr_daily_naver.py --apply --symbols 000660.KS --start 20100101
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db

URL = ("https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1"
       "&startTime={start}&endTime={end}&timeframe=day")
DEFAULT_SYMBOLS = {"000660.KS": "000660"}
CLOSE_HOUR_UTC = 6          # 韩国 15:30 KST = 06:30 UTC
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "Chrome/120 Safari/537.36")
ROW_RE = re.compile(
    r'\["?\s*(\d{8})\s*"?\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)')


def _fetch_curl(url: str, timeout: int = 25) -> str:
    """用系统 curl 取数。

    为什么要绕一层：这台机器上 Python 的 urllib 对 naver 做 TLS 握手会被中断
    （SSL: UNEXPECTED_EOF_WHILE_READING），而同样 UA 的 curl 一次就通。
    与其调 SSL 上下文，不如直接借用 curl 的 OpenSSL 配置。
    """
    import subprocess

    proc = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-A", UA, "-H", "Referer: https://finance.naver.com/", url],
        capture_output=True, text=True, timeout=timeout + 10)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"curl rc={proc.returncode} 输出 {len(proc.stdout)} 字节 {proc.stderr[:80]}")
    return proc.stdout


def fetch(code: str, start: str, end: str, timeout: int = 25) -> list[tuple]:
    url = URL.format(code=code, start=start, end=end)
    try:
        raw = _fetch_curl(url, timeout)
    except Exception as curl_err:
        req = Request(url, headers={"User-Agent": UA, "Referer": "https://finance.naver.com/"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            raise RuntimeError(f"curl: {curl_err} | urllib: {type(exc).__name__} {exc}") from exc
    rows = ROW_RE.findall(raw)
    if not rows:
        raise ValueError(f"解析到 0 行（返回 {len(raw)} 字节，前 60: {raw[:60]!r}）")
    return rows


def to_rows(asset: str, raw: list[tuple]) -> list[tuple]:
    out: list[tuple] = []
    seen: set[str] = set()
    for day_s, o, h, l, c, v in raw:
        try:
            day = dt.datetime.strptime(day_s, "%Y%m%d").replace(
                hour=CLOSE_HOUR_UTC, tzinfo=dt.timezone.utc)
            oo, hh, ll, cc = float(o), float(h), float(l), float(c)
            vv = int(float(v))
        except ValueError:
            continue
        if min(oo, hh, ll, cc) <= 0 or hh < ll:
            continue
        if hh > ll * 3:            # 量级断层的脏行（单位/拆股错位）
            continue
        key = day_s
        if key in seen:
            continue
        seen.add(key)
        out.append((asset, "1d", int(day.timestamp() * 1000), oo, hh, ll, cc, vv, int(time.time())))
    out.sort(key=lambda r: r[2])
    return out


def write_kline(conn: sqlite3.Connection, rows: list[tuple], tries: int = 4) -> int:
    for i in range(tries):
        try:
            cur = conn.executemany(
                "INSERT INTO kline_bars "
                "(symbol,timeframe,ts,open,high,low,close,volume,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,timeframe,ts) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,"
                "close=excluded.close,volume=excluded.volume,fetched_at=excluded.fetched_at",
                rows)
            conn.commit()
            return cur.rowcount
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == tries - 1:
                raise
            wait = 5 * (i + 1)
            print(f"  [LOCK] 写库被占，{wait}s 后重试 ({i + 1}/{tries - 1})")
            time.sleep(wait)
    return 0


def refresh_ohlcv(conn: sqlite3.Connection, symbols: set[str],
                  keep_existing: bool = True) -> dict[str, int]:
    """kline_bars → ohlcv_daily。

    keep_existing=True（默认）：**只补空缺日期**，已有行一律不动。
    起因：000660.KS 的旧 33 根来自 yfinance，与新源 Naver 在重叠的 26 天上
    平均相差 5%、最大 12.9%。两个都自称"收盘价"却对不上，在没有查明谁对之前
    不应该用新值覆盖旧值 —— 补历史是净收益，改现值是有风险的。
    """
    from backfill_ohlcv_local import build_rows

    rows, _stats = build_rows(conn)
    picked = [r for r in rows if r[0] in symbols]
    dedup: dict[tuple[str, str], tuple] = {}
    for r in picked:
        day = dt.datetime.fromtimestamp(r[1], dt.UTC).strftime("%Y-%m-%d")
        k = (r[0], day)
        if k not in dedup or r[1] > dedup[k][1]:
            dedup[k] = r

    if keep_existing:
        existing = {}
        for asset in symbols:
            for (ts_val,) in conn.execute("SELECT ts FROM ohlcv_daily WHERE asset=?", (asset,)).fetchall():
                existing[(asset, dt.datetime.fromtimestamp(ts_val, dt.UTC).strftime("%Y-%m-%d"))] = ts_val
        added = {k: v for k, v in dedup.items() if k not in existing}
    else:
        # 清掉同日非规范行（老源一天多根的情况）
        canon = {r[1] for r in dedup.values()}
        for asset in symbols:
            for (ts_val,) in conn.execute("SELECT ts FROM ohlcv_daily WHERE asset=?", (asset,)).fetchall():
                if ts_val in canon:
                    continue
                day = dt.datetime.fromtimestamp(ts_val, dt.UTC).strftime("%Y-%m-%d")
                if any(dt.datetime.fromtimestamp(c, dt.UTC).strftime("%Y-%m-%d") == day for c in canon):
                    conn.execute("DELETE FROM ohlcv_daily WHERE asset=? AND ts=?", (asset, ts_val))
        conn.commit()
        added = dedup

    conn.executemany(
        "INSERT INTO ohlcv_daily (asset,ts,open,high,low,close,volume,source) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(asset,ts) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,"
        "close=excluded.close,volume=excluded.volume,source=excluded.source",
        list(added.values()))
    conn.commit()
    return {s: sum(1 for r in added.values() if r[0] == s) for s in symbols}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--start", default="20100101")
    ap.add_argument("--overwrite-existing", action="store_true",
                    help="覆盖已有行（默认只补空缺，保留旧来源的现值）")
    args = ap.parse_args()
    pairs = [(s.strip().upper(), DEFAULT_SYMBOLS.get(s.strip().upper(), s.strip().split(".")[0]))
             for s in args.symbols.split(",") if s.strip()]
    end = dt.date.today().strftime("%Y%m%d")

    print(f"[INFO] 目标: {args.symbols}  {args.start}→{end}  "
          f"{'(写入)' if args.apply else '(dry-run)'}")
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        for asset, code in pairs:
            try:
                raw = fetch(code, args.start, end)
            except Exception as exc:
                print(f"  [FAIL] {asset}/{code}: {type(exc).__name__} {exc}")
                continue
            rows = to_rows(asset, raw)
            if not rows:
                print(f"  [FAIL] {asset}: 0 行")
                continue
            span = (f"{dt.datetime.fromtimestamp(rows[0][2] / 1000, dt.UTC):%Y-%m-%d} → "
                    f"{dt.datetime.fromtimestamp(rows[-1][2] / 1000, dt.UTC):%Y-%m-%d}")
            print(f"  [FETCH] {asset}: {len(rows)} 根 ({span})  末根 close={rows[-1][6]:,.0f}")
            if args.apply:
                print(f"  [WRITE] {asset}: kline_bars 影响 {write_kline(conn, rows)} 行")
        if args.apply:
            counts = refresh_ohlcv(conn, {a for a, _ in pairs},
                                   keep_existing=not args.overwrite_existing)
            print("[OK] ohlcv_daily: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        else:
            print("[DRY] 未写库")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
