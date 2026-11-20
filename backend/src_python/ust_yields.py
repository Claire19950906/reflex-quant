#!/usr/bin/env python3
"""美债收益率：FRED 单序列慢速抓取（一次只发一个请求）。

为什么单独写一个：
  · fred_csv.py 一次要过 6 条序列，FRED 对"短时间连发"会直接 ReadTimeout/断连，
    所以那批序列一直没落库（10Y、2Y、期限利差全空白）。
  · 这里改成**每轮只取一条**：发一个请求、落库、退出，靠 state 文件轮转。
    单发实测可用（DGS10 返回 2026-11-10, 4.98），连发必挂。
  · cron 每 20 分钟一轮，6 条序列约 2 小时各更新一次 —— 债券收益率本来就是日频，
    这个频率足够，且不会触发限流。

序列：DGS10（10Y）、DGS2（2Y）、DFF（联邦基金利率）、T10Y2Y（10Y-2Y 利差）。
"""
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import external_data

requests.Session.trust_env = False   # 必须：否则会命中本机 DOUBAO_*_FORWARD_PROXY 被掐断

SERIES: Tuple[Tuple[str, str, str], ...] = (
    ("DGS10", "us_dgs10", "%"),
    ("DGS2", "us_dgs2", "%"),
    ("T10Y2Y", "us_t10y2y", "%"),
    ("DFF", "us_dff", "%"),
)
STATE_PATH = os.path.expanduser("~/.trident/ust_rotation.json")
# cosd 限定起始日期：DGS10 的全历史 CSV 很长（1962 起），
# 大文件在境外链路上常在半路被断开，表现为 ConnectionError；
# 只取近两年，既小又够用（收益率是日频，两年足够算分位与趋势）。
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2024-11-01"

# 财政部 CSV 是确认直连可用的（另一支 ust_curve_treasury 一直在写 us_t_*），
# FRED 一断（本机不走代理就连不上），这里就顶上，别让收益率空着。
TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/daily-treasury-rates.csv/{year}/all"
                "?type=daily_treasury_yield_curve"
                "&field_tdr_date_value={year}&page&_format=csv")
# FRED 序列 → 财政部 CSV 里的列名
TREASURY_COL = {"DGS10": "10 Yr", "DGS2": "2 Yr", "DGS5": "5 Yr",
                "T10Y2Y": None, "DFF": None}   # None = 财政部没有这一列


def _next_index() -> int:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("idx", 0)) % len(SERIES)
    except (OSError, ValueError, KeyError):
        return 0


def _save_index(idx: int) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"idx": idx % len(SERIES), "updated": int(time.time())}, fh)


def fetch_from_treasury(sid: str) -> Optional[Tuple[str, float]]:
    """从财政部日频收益率 CSV 取最新一条。DFF / T10Y2Y 财政部没有对应列。"""
    _hit = _SERIES_CACHE.get(sid)
    if _hit and time.time() - _hit["ts"] < _SERIES_TTL:
        return _hit["val"]
    col = TREASURY_COL.get(sid)
    if sid == "T10Y2Y":
        got10 = fetch_from_treasury("DGS10")
        got2 = fetch_from_treasury("DGS2")
        if got10 and got2 and got10[0] == got2[0]:
            return got10[0], round(got10[1] - got2[1], 3)
        return None
    if not col:
        return None
    # 先看库里有没有财政部那一路已经采好的（treasury_yield_feed 写 us_dgs10/us_dgs2，
    # source=treasury_gov），有就直接用，省得再解析一遍 CSV 还解析错。
    try:
        conn = sqlite3.connect(db.get_db_path(), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            row = conn.execute(
                """SELECT ts, value FROM external_indicators
                   WHERE indicator=? AND source='treasury_gov'
                   ORDER BY ts DESC LIMIT 1""", (f"us_{sid.lower()}",)).fetchone()
        finally:
            conn.close()
        if row:
            t = row[0]
            sec = t / 1000 if t > 1e11 else t       # 有的是毫秒
            # 库内值太旧就别用：收益率是日频，超过 10 天等于拿旧结论冒充当下
            if time.time() - sec <= 10 * 86400:
                return time.strftime("%Y-%m-%d", time.localtime(sec)), float(row[1])
    except Exception:
        pass
    year = time.localtime().tm_year
    for y in (year, year - 1):                 # 年初第一周可能还没今年的文件
        text = (_CSV_TEXT_CACHE.get(str(y)) or {}).get("text")
        if not text:
            for attempt in range(2):           # 境外链路偶发 reset，重试一次
                try:
                    r = requests.get(TREASURY_CSV.format(year=y), timeout=12,
                                     headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and r.text.strip():
                        text = r.text
                        _CSV_TEXT_CACHE[str(y)] = {"ts": time.time(), "text": text}
                        break
                except Exception:
                    continue
        if not text:
            continue
        rows = [ln for ln in text.strip().splitlines() if ln.strip()]
        if not rows:
            continue
        # 财政部 "all" 的 CSV 是多张表首尾相连（日度曲线 / 实际收益率 / 短期票据 / 长期…），
        # 每张表都有自己的表头。只有「Date 打头」的行才是新表开始，必须按块解析，
        # 否则会读到别的表的最后一行（实测 DGS5 因此拿到 2026-01-02 这种旧值）。
        blocks, cur = [], []
        for ln in rows:
            head_cells = [h.strip().strip('"') for h in ln.split(",")]
            if head_cells and head_cells[0] == "Date":
                if cur:
                    blocks.append(cur)
                cur = [ln]
            elif cur:
                cur.append(ln)
        if cur:
            blocks.append(cur)

        best = None  # (date_struct, iso, value)
        for blk in blocks:
            head = [h.strip().strip('"') for h in blk[0].split(",")]
            if "Date" not in head or col not in head:
                continue
            di, ci = head.index("Date"), head.index(col)
            for ln in blk[1:]:
                parts = [p.strip().strip('"') for p in ln.split(",")]
                if len(parts) <= max(di, ci) or not parts[ci]:
                    continue
                try:
                    # 财政部 CSV 的日期是 MM/DD/YYYY，统一转成 ISO，别让上层按两种格式猜
                    d = time.strptime(parts[di], "%m/%d/%Y")
                    v = float(parts[ci])
                except ValueError:
                    continue
                if best is None or d > best[0]:
                    best = (d, time.strftime("%Y-%m-%d", d), v)
        if best:
            # 新鲜度闸门：今年文件拉不到时会回落到去年文件，那里的"最新一行"是
            # 2025-12-31 这种一年前的值。超过 30 天一律不给，宁可标缺失。
            if time.time() - time.mktime(best[0]) > 30 * 86400:
                print(f"[WARN] {sid} 财政部 CSV 最新行是 {best[1]}，已过期，按缺失处理", flush=True)
                return None
            val = (best[1], best[2])
            _SERIES_CACHE[sid] = {"ts": time.time(), "val": val}
            return val
    return None


LAST_SOURCE = {"src": ""}

# v350: 财政部 CSV 一次解析 40~50 秒（文件大 + 境外链路慢），而它一天只变一次。
# 加两层缓存：CSV 原文按年缓存 30 分钟，单序列结果缓存 10 分钟。
_CSV_TEXT_CACHE: Dict[str, Any] = {}
_CSV_TTL = 1800.0
_SERIES_CACHE: Dict[str, Any] = {}
_SERIES_TTL = 600.0


def fetch_series(sid: str) -> Optional[Tuple[str, float]]:
    """返回 (最新日期, 数值)。FRED 只试一次（重试会加重限流），失败改打财政部 CSV。"""
    try:
        r = requests.get(URL.format(sid=sid), timeout=30)
    except Exception as exc:
        print(f"[WARN] {sid} FRED 请求失败: {type(exc).__name__}，改试财政部 CSV", flush=True)
        r = None
    if r is not None and r.status_code == 200:
        lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
        for ln in reversed(lines):
            parts = ln.split(",")
            if len(parts) < 2:
                continue
            date_s, val_s = parts[0], parts[1]
            if val_s in ("", "."):
                continue            # FRED 用 "." 表示休市/无观测
            try:
                LAST_SOURCE["src"] = "fred_csv"
                return date_s, float(val_s)
            except ValueError:
                continue
    elif r is not None:
        print(f"[WARN] {sid} FRED HTTP {r.status_code}，改试财政部 CSV", flush=True)

    # FRED 不可用（本机不走代理就连不上）→ 财政部 CSV 顶上
    got = fetch_from_treasury(sid)
    if got:
        LAST_SOURCE["src"] = "us_treasury"
        return got
    print(f"[WARN] {sid} 两个源都没拿到", flush=True)
    return None


def _derive_dgs10() -> None:
    """DGS10 单文件过大，链路上反复被切断；用 2Y + 期限利差补一个计算值。

    10Y = 2Y + (10Y-2Y)。两个输入都是 FRED 官方原值，所以这是**公式推导**而不是估算。
    指标名单独叫 us_dgs10_calc，meta 写明推导方式，不冒充 FRED 官方序列；
    官方 us_dgs10 一旦抓通，两者会并存，看板上应优先取官方值。
    """
    try:
        # 直接读库（带 busy_timeout），不走 list_indicators：后者建连接不加超时，
        # 引擎高频写入时容易撞 "database is locked"。
        conn = sqlite3.connect(db.get_db_path(), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            vals = {}
            for ind in ("us_dgs2", "us_t10y2y"):
                row = conn.execute(
                    """SELECT value FROM external_indicators
                       WHERE asset='US_MACRO' AND indicator=?
                       ORDER BY ts DESC LIMIT 1""", (ind,)).fetchone()
                if not row:
                    return
                vals[ind] = float(row[0])
        finally:
            conn.close()
        calc = round(vals["us_dgs2"] + vals["us_t10y2y"], 3)
        external_data.ingest_external(
            asset="US_MACRO", indicator="us_dgs10_calc",
            records=[{"ts": int(time.time()), "value": calc}],
            source="fred_csv",
            meta={"unit": "%", "derived_from": "us_dgs2 + us_t10y2y",
                  "note": "计算值，非 FRED 官方 DGS10 原始序列"},
        )
    except Exception as exc:
        print(f"[WARN] 推导 10Y 失败: {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    _derive_dgs10()      # 每轮都先补一次计算值，不依赖当轮抓取是否成功
    idx = _next_index()
    sid, indicator, unit = SERIES[idx]
    got = fetch_series(sid)

    if not got:
        _save_index(idx + 1)      # 失败也轮转，下一轮换一条
        return 1

    date_s, value = got
    ts = int(time.mktime(time.strptime(date_s, "%Y-%m-%d")))
    external_data.ingest_external(
        asset="US_MACRO", indicator=indicator,
        records=[{"ts": ts, "value": value}],
        # 来源不能写死 fred_csv：FRED 断了会静默走财政部，混标会让后续归因撒谎
        source=LAST_SOURCE["src"] or "fred_csv",
        meta={"unit": unit, "fred_id": sid, "obs_date": date_s,
              "note": "单发轮转采集；FRED 优先，不可用时回落财政部日频 CSV"},
    )
    _save_index(idx + 1)
    _derive_dgs10()
    print(f"[{time.strftime('%H:%M:%S')}] {sid} → {indicator} = {value} "
          f"（{date_s}）下一轮 {SERIES[(idx + 1) % len(SERIES)][0]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
