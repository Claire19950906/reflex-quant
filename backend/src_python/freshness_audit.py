#!/usr/bin/env python3
"""数据新鲜度审计：让"某个源悄悄断掉"这件事无法隐藏。

为什么需要：
  · 这套系统最怕的不是拿不到数据，而是**以为有、其实是几小时前的旧值**。
    之前雅虎就是这样：序列停在 31 小时前，但它仍然在库里"看起来正常"。
  · 审计逻辑：对每条序列取最近 20 个采样点，算**中位采样间隔**当它的正常节奏；
    若"距上次更新"超过 3 倍节奏（且至少超过 2 小时），判为 stale，标记出来。
    —— 节奏由数据自身推出来，不给每条序列硬编码频率，避免尺子造错。

输出：新建表 `indicator_freshness`（不改动既有 source_status，避免污染前端源列表），
      另在日志里打印一览，方便人工快速看出哪条断了。
"""
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

CST = timezone(timedelta(hours=8))
LOOKBACK_DAYS = 7     # 用最近 7 天的采样点判断节奏
MIN_STALE_SECONDS = 2 * 3600

# 官方发布节奏的已知下限（秒）。只有"发布频率本身就低"的源才写在这里，
# 目的不是放宽，而是别拿日频去要求周报。
SOURCE_MIN_CADENCE = {
    "fed_h41": 7 * 86400,        # 美联储 H.4.1 周报，每周四发布
}
SLACK = 3.0           # 超过 3 倍正常节奏即判 stale


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indicator_freshness (
            computed_at   INTEGER NOT NULL,
            source        TEXT    NOT NULL,
            asset         TEXT    NOT NULL,
            indicator     TEXT    NOT NULL,
            last_ts       INTEGER,
            age_seconds   INTEGER,
            median_interval_seconds INTEGER,
            status        TEXT,           -- ok / stale / dead
            note          TEXT,
            PRIMARY KEY (computed_at, source, asset, indicator)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_freshness_status "
                 "ON indicator_freshness(computed_at, status)")


def _cn_market_closed(now: int) -> bool:
    """A 股是否处于休市时段（北京时间，工作日 09:15–15:15 之外）。

    v333: 周末/收盘后 A50、CN-SZC 这些序列本来就不会有新数据，
    按"3 倍节奏"判死会天天误报。用户几乎不做 A 股，这些只是参考背景，
    没必要让它们天天占着异常列表 —— 但**超过 4 天没更新仍报 dead**，
    那才是真的接不上，不是休市。
    """
    try:
        cst = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=8)))
    except Exception:
        return False
    if cst.weekday() >= 5:          # 周六、周日
        return True
    minutes = cst.hour * 60 + cst.minute
    return not (9 * 60 + 15 <= minutes <= 15 * 60 + 15)


def series_list(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    cutoff = int(time.time()) - LOOKBACK_DAYS * 86400
    rows = conn.execute(
        """SELECT source, asset, indicator FROM external_indicators
           WHERE ts >= ? GROUP BY source, asset, indicator""",
        (cutoff,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def audit(conn: sqlite3.Connection, now: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cutoff = now - LOOKBACK_DAYS * 86400
    for source, asset, indicator in series_list(conn):
        ts_rows = conn.execute(
            """SELECT ts FROM external_indicators
               WHERE source=? AND asset=? AND indicator=? AND ts>=?
               ORDER BY ts DESC LIMIT 25""",
            (source, asset, indicator, cutoff)).fetchall()
        stamps = [int(r[0]) for r in ts_rows]
        if not stamps:
            continue
        last = stamps[0]
        age = now - last
        # 节奏按"窗口内每天平均采样点数"折算，不用中位间隔：
        # 有的序列曾被密集写过一阵（日历类一天刷几十次），之后改成日频，
        # 中位间隔就会被那阵密集拉成 403 秒 —— 等于把日频当成 7 分钟节奏去判死，
        # 天天误报。点数/天 这个口径对"密集一阵"和"一直稀疏"两种形态都稳。
        days = len({int(s // 86400) for s in stamps}) or 1
        per_day = len(stamps) / days
        interval = int(86400 / per_day) if per_day else 0
        # 少数官方发布本身就是低频：H-41 是**周报**（每周四发一次），
        # 用"日频"去要求它，每周都会误报一次"停 90 小时"。
        # 这里按**官方发布节奏**给下限，不是把阈值调宽来掩盖断流：
        # 真断流时一周内必然露头。
        floor = SOURCE_MIN_CADENCE.get(source)
        if interval:
            stale_after = max(int(interval * SLACK), MIN_STALE_SECONDS, floor or 0)
        if floor and not interval:
            stale_after = max(floor * SLACK, MIN_STALE_SECONDS)
        else:
            # 只有一个观测点的序列（如日频、偶发）没有节奏可推，
            # 不能按小时判死，否则会把"周五公布、周一才更新"的正常滞后误判为断流。
            stale_after = 7 * 86400
        status = "ok"
        note = ""
        # A 股背景序列：休市时段不算断流（超过 4 天才算）
        is_cn = asset.startswith("CN") or asset == "A50"
        if is_cn and _cn_market_closed(now) and age <= 4 * 86400:
            status, note = "offhours", "A 股休市，非断流"
            out.append({"source": source, "asset": asset, "indicator": indicator,
                        "last_ts": last, "age": age, "interval": interval,
                        "status": status, "note": note})
            continue
        if age > stale_after * 2:
            status, note = "dead", "远超正常节奏"
        elif age > stale_after:
            status = "stale"
            note = f"正常节奏 {interval}s，已停 {age}s"
        out.append({"source": source, "asset": asset, "indicator": indicator,
                    "last_ts": last, "age": age, "interval": interval,
                    "status": status, "note": note})
    return out


# 关键指标的"应该有"清单：库里根本没有记录也算异常。
# 只靠遍历已有数据是不够的——一条序列如果从未成功落库，审计会当它不存在，
# 界面上也就永远看不到"美债收益率缺失"这件事。下面这份清单专门覆盖这种空洞。
EXPECTED = (
    ("fred_csv", "US_MACRO", "us_dgs10_calc", 96, "10 年期美债收益率（计算值）"),
    # FRED 日频数据有发布滞后（周末与节假日不发），阈值放到 96h 才叫断
    ("fred_csv", "US_MACRO", "us_dgs2", 96, "2 年期美债收益率"),
    ("fred_csv", "US_MACRO", "us_t10y2y", 96, "10Y-2Y 期限利差"),
    ("cboe", "US_MACRO", "us_vix_cboe", 2, "VIX"),
    ("frankfurter", "FX", "dxy_ecb_computed", 30, "美元指数（计算值）"),
    ("xvenue", "BTC", "x_spot_dispersion_pct", 1, "BTC 跨所价差"),
    ("blockchair", "BTC", "btc_mempool_tx", 3, "BTC 内存池"),
    ("sge", "SGE_GOLD", "sge_gold_premium_pct", 1, "中国金溢价"),
    # —— v333: 财政部一手曲线（每日一次，周末不发，阈值 96h）——
    ("cboe_skew", "US_EQ", "us_skew_index", 30, "CBOE 尾部风险偏度 SKEW"),
    ("fed_fomc_calendar", "US_MACRO", "fomc_days_to_next", 192, "距下一次 FOMC 决议日"),
    ("fed_fomc_calendar", "US_MACRO", "fomc_meetings_90d", 192, "未来 90 天 FOMC 场次"),
    ("sge_silver", "XAG", "sge_ag9999_cny_g", 30, "上金所白银价"),
    ("binance_options", "BTC", "crypto_atm_iv", 48, "BTC 平值隐含波动率"),
    ("binance_options", "XAU", "xau_atm_iv", 72, "黄金平值隐含波动率"),
    ("binance_options", "XAU", "xau_25d_skew", 72, "黄金 25Δ 偏度"),
    ("binance_options", "XAG", "xag_atm_iv", 72, "白银平值隐含波动率"),
    ("binance_options", "XAG", "xag_25d_skew", 72, "白银 25Δ 偏度"),
    ("binance_options", "BTC", "crypto_25d_skew", 48, "BTC 25Δ 偏度"),
    ("binance_options", "ETH", "crypto_atm_iv", 48, "ETH 平值隐含波动率"),
    ("binance_options", "ETH", "crypto_25d_skew", 48, "ETH 25Δ 偏度"),
    ("sge_silver", "XAG", "sge_silver_premium_pct", 30, "国内外白银溢价"),
    ("derived_crypto", "ETH", "cb_premium_pct", 48, "Coinbase 溢价（ETH）"),
    ("derived_crypto", "BTC", "stable_supply_30d_pct", 72, "稳定币供应30日变化"),
    ("derived_crypto", "BTC", "stable_supply_total", 72, "USDT+USDC 总供应"),
    ("sec_form4", "AAPL", "insider_buy_shares_30d", 96, "AAPL 内部人买入（30日）"),
    ("sec_form4", "AAPL", "insider_sell_shares_30d", 96, "AAPL 内部人卖出（30日）"),
    ("sec_form4", "MU", "insider_buy_shares_30d", 96, "MU 内部人买入（30日）"),
    ("sec_form4", "MU", "insider_sell_shares_30d", 96, "MU 内部人卖出（30日）"),
    ("sec_form4", "META", "insider_buy_shares_30d", 96, "META 内部人买入（30日）"),
    ("sec_form4", "META", "insider_sell_shares_30d", 96, "META 内部人卖出（30日）"),
    ("sec_form4", "GOOGL", "insider_buy_shares_30d", 96, "GOOGL 内部人买入（30日）"),
    ("sec_form4", "GOOGL", "insider_sell_shares_30d", 96, "GOOGL 内部人卖出（30日）"),
    ("coinmetrics", "BTC", "cm_active_addr", 72, "BTC 活跃地址（CoinMetrics）"),
    ("coinmetrics", "BTC", "cm_hashrate", 72, "BTC 算力（CoinMetrics）"),
    ("coinmetrics", "BTC", "cm_supply", 72, "BTC 流通量（CoinMetrics）"),
    ("coinmetrics", "ETH", "cm_active_addr", 72, "ETH 活跃地址（CoinMetrics）"),
    ("coinmetrics", "ETH", "cm_supply", 72, "ETH 流通量（CoinMetrics）"),
    ("nasdaq_calendar", "US_EQ", "us_earnings_count_7d", 48, "美股财报家数（7日）"),
    ("nasdaq_calendar", "US_EQ", "us_earnings_watch_7d", 48, "关注标的财报数（7日）"),
    ("derived_ratio", "*", "xau_xag_ratio", 96, "金银比（派生）"),
    ("derived_ratio", "*", "xau_hg_ratio", 96, "金铜比（派生）"),
    ("derived_ratio", "*", "xau_wti_ratio", 96, "金油比（派生）"),
    ("derived_ratio", "*", "xau_spx_ratio", 96, "金股比（派生）"),
    ("fed_h41", "*", "fed_total_assets", 240, "美联储总资产（H.4.1 周报）"),
    ("fed_h41", "*", "fed_securities_held", 240, "美联储证券持仓（H.4.1）"),
    ("fed_h41", "*", "fed_treasury_holdings", 240, "美联储国债持仓（H.4.1）"),
    ("treasury_gov", "US_MACRO", "us_dgs10", 96, "10Y 美债（财政部官方）"),
    ("treasury_gov", "US_MACRO", "us_dgs2", 96, "2Y 美债（财政部官方）"),
    ("treasury_gov", "US_MACRO", "us_t_30y", 96, "30Y 美债（财政部官方）"),
    ("treasury_gov", "US_MACRO", "us_t10y2y_gov", 96, "10Y-2Y 利差（官方两腿相减）"),
    ("treasury_gov", "US_MACRO", "us_t10y3m_gov", 96, "10Y-3M 利差（官方两腿相减）"),
    # —— v333: 联动 ETF（新浪美股日线，asset='*'，周末最多隔 3 天）——
    ("sina_etf", "*", "GLD", 96, "黄金 ETF"),
    ("sina_etf", "*", "TLT", 96, "20Y+ 美债 ETF"),
    ("sina_etf", "*", "UUP", 96, "美元指数 ETF"),
    ("sina_etf", "*", "SLV", 96, "白银 ETF"),
    ("sina_etf", "*", "GDX", 96, "金矿股 ETF"),
    ("sina_etf", "*", "USO", 96, "原油 ETF"),
    ("sina_etf", "*", "IBIT", 96, "比特币 ETF"),
)


def _to_sec_ts(v: int) -> int:
    """v336: 表里混着秒和毫秒（毫秒行 age 会变负 → 永远判"新鲜"）。读端兜一层。"""
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n // 1000 if n >= 10 ** 11 else n


def check_expected(conn: sqlite3.Connection, now: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for source, asset, indicator, max_age_h, label in EXPECTED:
        note_src = ""
        row = conn.execute(
            """SELECT MAX(ts) FROM external_indicators
               WHERE source=? AND asset=? AND indicator=?""",
            (source, asset, indicator)).fetchone()
        last = _to_sec_ts(int(row[0])) if row and row[0] else None
        # v333: 主源换了通道不该被误判成"从未落库"。
        #   例：Frankfurter 挂掉后美元指数改由腾讯外汇写入（source='tencent_fx'），
        #   若这里死认 source 就会报 missing，看着像数据没了，实际一直都在。
        if last is None:
            row2 = conn.execute(
                """SELECT MAX(ts), MAX(source) FROM external_indicators
                   WHERE asset=? AND indicator=?""",
                (asset, indicator)).fetchone()
            if row2 and row2[0]:
                last = _to_sec_ts(int(row2[0]))
                if source != row2[1]:
                    note_src = f"（当前来源 {row2[1]}，预期 {source}）"
                else:
                    note_src = ""
        age = (now - last) if last else None
        if age is None:
            status, note = "missing", f"从未落库（应每 {max_age_h}h 一次）"
        elif age > max_age_h * 3600:
            status, note = "late", f"已停 {age/3600:.1f}h（阈值 {max_age_h}h）{note_src}"
        else:
            status, note = "ok", ""
        out.append({"source": source, "asset": asset, "indicator": indicator,
                    "label": label, "last_ts": last, "age": age, "interval": max_age_h * 3600,
                    "status": status, "note": note})
    return out


def _retry_write(fn, tries: int = 6, pause: float = 1.5):
    """库被引擎高频占用时，写操作会撞 database is locked；重试几次比直接失败好。"""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == tries - 1:
                raise
            time.sleep(pause)


def main() -> int:
    now = int(time.time())
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = audit(conn, now)

    covered = {(r["source"], r["asset"], r["indicator"]) for r in rows}
    for e in check_expected(conn, now):
        key = (e["source"], e["asset"], e["indicator"])
        if key in covered:
            for r in rows:
                if (r["source"], r["asset"], r["indicator"]) == key and e["status"] != "ok":
                    r.update(status=e["status"], note=e["note"])
        else:
            rows.append(e)
    conn.close()

    # offhours = A 股休市，属正常，不计入异常
    stale = [r for r in rows if r["status"] not in ("ok", "offhours")]

    def _persist() -> None:
        wconn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
        wconn.execute("PRAGMA busy_timeout=30000")
        try:
            ensure_table(wconn)
            wconn.execute("DELETE FROM indicator_freshness WHERE computed_at < ?",
                          (now - 7 * 86400,))
            for r in rows:
                wconn.execute(
                    """INSERT OR REPLACE INTO indicator_freshness
                       (computed_at, source, asset, indicator, last_ts, age_seconds,
                        median_interval_seconds, status, note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, r["source"], r["asset"], r["indicator"], r["last_ts"],
                     r["age"] if r["age"] is not None else -1,
                     r["interval"], r["status"], r["note"]))
        finally:
            wconn.close()

    try:
        _retry_write(_persist)
    except sqlite3.OperationalError as exc:
        print(f"[WARN] 审计结果写库失败（库被占用）: {exc}", flush=True)

    print(f"[{datetime.fromtimestamp(now, CST).strftime('%H:%M:%S')}] "
          f"新鲜度审计 共 {len(rows)} 条序列，异常 {len(stale)} 条", flush=True)
    for r in sorted(stale, key=lambda x: -(x.get("age") or 0)):
        age_txt = f"停 {r['age']/3600:.1f}h" if r.get("age") else "无记录"
        label = r.get("label") or r["indicator"]
        print(f"  [{r['status']:7}] {r['source']}/{r['asset']}/{label} "
              f"{age_txt}（节奏 {r['interval']}s）{r['note']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
