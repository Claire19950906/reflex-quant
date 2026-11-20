#!/usr/bin/env python3
"""别名桥：把新数据源的值映射回旧的雅虎符号名。

为什么需要：
  删掉雅虎之后，`cross_asset_context.py` 的白名单还是 7 个雅虎代码
  （BTC-USD / ^GSPC / ^VIX / DX-Y.NYB / TLT / GLD / ^TNX）。
  实测库里这 7 个：BTC-USD 停在 37 天前，其余 6 个**一行都没有**。
  也就是说跨资产联动判断这个模块，现在读的是空气。

  新源其实已经把这些东西采回来了，只是**名字不一样**（us_vix_cboe、dxy_ecb_computed、
  SPX/tencent_price 等），老消费者按名字查自然查不到。

  这里做一层显式映射，不改老代码：把新指标的最新值以**旧符号名**再写一条，
  source 标 `alias`，meta 写明 alias_of 指向真实来源，不冒充原始数据。
  TLT / GLD 没有等价替代（TLT 是 20Y+ 国债 ETF、GLD 是黄金 ETF，都不是现成的价格序列），
  故意不映射 —— 缺就是缺，不拿别的品种顶替。

每 5 分钟跑一次，和原采集同频。
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import db_retry

# (旧符号, 新来源 asset, 新指标名, 值域说明)
ALIASES: Tuple[Tuple[str, str, str, str], ...] = (
    ("BTC-USD", "BTC", "x_spot_median", "四所现货中位价，USD"),
    ("^VIX", "US_MACRO", "us_vix_cboe", "CBOE 官方延迟 VIX"),
    ("DX-Y.NYB", "FX", "dxy_ecb_computed", "按 ICE 六币权重计算，非官方报价"),
    ("^TNX", "US_MACRO", "us_dgs10_calc", "10Y 收益率（2Y+利差推导）"),
    ("^GSPC", "SPX", "tencent_price", "标普 500 腾讯实时价"),
    # v343: discovery 按"裸名"读 UST2Y / UST10Y 算收益率相关性，而这两个名字只有
    #   fred 直连在写、已停更 41 天 → 该相关性一直是死的。fred_csv 的 us_dgs2/us_dgs10
    #   是新鲜的同一口径，补成别名即可复活（不动 discovery 的读代码）。
    #   注意：us_dgs5 / us_dgs30 上游不存在（fred_csv 只出 2Y/10Y），UST5Y/UST30Y 仍然缺。
    ("UST2Y", "US_MACRO", "us_dgs2", "2年期美债收益率（FRED CSV 口径）"),
    ("UST10Y", "US_MACRO", "us_dgs10", "10年期美债收益率（FRED CSV 口径）"),
    # 5Y/30Y 没有 FRED CSV 口径，但财政部日终曲线里有 us_t_5y / us_t_30y（官方一手）
    ("UST5Y", "US_MACRO", "us_t_5y", "5年期美债收益率（财政部日终曲线）"),
    ("UST30Y", "US_MACRO", "us_t_30y", "30年期美债收益率（财政部日终曲线）"),
)

# GLD / TLT 没有现成指标可映射，但它们是美股 ETF —— 新浪日线能直接给。
# 这两个是跨资产判断里的"避险腿"（黄金 ETF / 20Y+ 国债 ETF），缺了它们就没法判 risk-on/off。
# v333: 补齐与真实持仓直接相关的联动标的（全部来自新浪美股日线，免 key、全历史）：
#   UUP —— 美元指数 ETF，DXY 的场内映射，可与 `dxy_ecb_computed` 交叉验证；
#   SLV —— 白银 ETF，黄金的联动腿；
#   GDX —— 金矿股 ETF，历史上常领跑金价；
#   USO —— 原油 ETF，通胀链的一环；
#   IBIT —— 比特币现货 ETF，把加密货币和美股资金面连起来（2024 年上市，根数少属正常）。
ETF_SYMBOLS = {
    "GLD": "GLD", "TLT": "TLT",
    "UUP": "UUP", "SLV": "SLV", "GDX": "GDX", "USO": "USO", "IBIT": "IBIT",
}

# 上市日期下限：数据源会把标的上市前的时间段也塞进来（实测 IBIT 返回到 2022-09，
# 而它 2024-01-11 才上市），这些行是错的，必须在写入前就切掉 —— 不许存可疑数据。
LISTING_DATE = {"IBIT": "2024-01-11"}
_LISTING_TS = {
    k: int(datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    for k, v in LISTING_DATE.items()
}
# v333: 从 60 根放到 3000 根。原来只取 60 天，是为了"先把跨资产腿接上"；
#   但 GLD/TLT 是避险腿的核心，60 天连一个季度都不到，任何回看窗口都不够用。
#   新浪日线本身给全历史，且写入按 (asset, indicator, ts) 去重，放大天数只是补历史、不会重复。
ETF_DAYS = 3000

# 关键：cross_asset_context 是按 `asset IN ('*', '', ?)` 过滤的，
# 所以这些行的 asset 必须写 '*'，写成符号名（如 '^VIX'）它根本查不到。
CROSS_ASSET_OWNER = "*"


def latest(conn: sqlite3.Connection, asset: str, indicator: str) -> Optional[Tuple[float, int]]:
    row = conn.execute(
        """SELECT value, ts FROM external_indicators
           WHERE asset=? AND indicator=? AND value IS NOT NULL
           ORDER BY ts DESC LIMIT 1""", (asset, indicator)).fetchone()
    if not row:
        return None
    return float(row[0]), int(row[1])


def _sina_daily(symbol: str, days: int) -> List[Tuple[int, float]]:
    """新浪美股日线（ETF 也能取），返回 [(ts, close)]。"""
    import requests
    requests.Session.trust_env = False
    url = "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
    for i in range(3):
        try:
            r = requests.get(url, params={"symbol": symbol, "___qn": 3},
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://finance.sina.com.cn/"},
                             timeout=20)
            rows = r.json()
            if not isinstance(rows, list):
                return []
            out = []
            for row in rows[-days:]:
                # v334: 新浪的 d 就是真实交易日，不加偏移（此前的"早一天"是
                #   按 UTC 反解本地零点造成的显示错觉）。只丢弃周末与无成交量的占位行。
                dt = datetime.strptime(str(row["d"])[:10], "%Y-%m-%d")
                if dt.weekday() >= 5 or not row.get("v"):
                    continue
                out.append((int(time.mktime(dt.timetuple())), float(row["c"])))
            return out
        except Exception:
            time.sleep(3 * (i + 1))
    return []


def write_etf_history(conn) -> int:
    """GLD / TLT 日线收盘写入 external_indicators（asset='*'，按天去重）。"""
    written = 0
    for name, symbol in ETF_SYMBOLS.items():
        floor_ts = _LISTING_TS.get(name, 0)
        for ts, close in _sina_daily(symbol, ETF_DAYS):
            if floor_ts and ts < floor_ts:
                continue  # 上市前的价格一定是错的，不写
            dup = conn.execute(
                """SELECT 1 FROM external_indicators
                   WHERE asset=? AND indicator=? AND ts=? LIMIT 1""",
                (CROSS_ASSET_OWNER, name, ts)).fetchone()
            if dup:
                continue
            conn.execute(
                """INSERT INTO external_indicators
                   (asset, indicator, value, ts, source, regime_tags, meta)
                   VALUES (?,?,?,?,?,?,?)""",
                (CROSS_ASSET_OWNER, name, close, ts, "sina_etf",
                 '["alias_bridge"]',
                 '{"etf": "%s", "note": "新浪美股日线收盘，用于跨资产避险腿"}' % symbol))
            written += 1
        time.sleep(2)
    return written


def main() -> int:
    now = int(time.time())
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    written, missing = 0, []
    etf_written = 0
    try:
        for symbol, asset, indicator, note in ALIASES:
            got = latest(conn, asset, indicator)
            if not got:
                missing.append(f"{symbol}<-{indicator}")
                continue
            value, src_ts = got
            # 用源数据自己的时间戳写，保证下游按 ts 判新鲜度时看到的是真实时间
            dup = conn.execute(
                """SELECT 1 FROM external_indicators
                   WHERE asset=? AND indicator=? AND ts=? LIMIT 1""",
                (CROSS_ASSET_OWNER, symbol, src_ts)).fetchone()
            if dup:
                continue
            # 直插裸 INSERT 会撞上引擎的长写事务（实测 database is locked 直接中断整轮）。
            # 重试仍失败时不能整轮崩：这是 5 分钟一次的作业，漏一条下一轮补上即可。
            try:
                wrote = db_retry.insert_with_retry(
                conn,
                "INSERT INTO external_indicators "
                "(asset, indicator, value, ts, source, regime_tags, meta) "
                "VALUES (?,?,?,?,?,?,?)",
                    (CROSS_ASSET_OWNER, symbol, value, src_ts, "alias",
                     '["alias_bridge"]',
                     '{"alias_of": "%s/%s", "note": "%s"}' % (asset, indicator, note)))
            except Exception as exc:
                print(f"[alias_bridge] {symbol} 暂未写入（下一轮补）: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                written += 0
                continue
            written += 1
        etf_written = write_etf_history(conn)
    finally:
        conn.close()

    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] 别名桥 别名 {written} 条｜ETF 日线 {etf_written} 条", flush=True)
    if missing:
        print("  无上游数据（保持缺失，不用别的品种顶替）: " + ", ".join(missing), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
