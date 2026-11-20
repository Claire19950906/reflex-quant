"""db.py 取价 / 归一 / 衰减 层的用例（结算口径的护栏）。

`db.py` 是全系统唯一取价口径。它错一次不会报错，只会让库里的收益数字静默变错：
2026-10-29 那次把均值从 +1.45% 拉到 +117%，起因就是「入场价与结算价不同源」+
「日线兜底取了窗口里最老的一根」。

这里钉死的是四条口径，不是函数能不能跑：
1. 取价优先级：1m 分钟线 > 日线 > tick / kline_bars 兜底，且**不猜价**（取不到返回 None）
2. `price_at` 只看 `interval='1m'`：不过滤会取到 4h 收盘价，跟到期时刻差 4 小时
3. 日线兜底取**最近一根**（DESC）+ 滞后上限，不是窗口里最老的那根
4. 时间戳归一：ISO / epoch 字符串 / 本地时间三种混存，SQLite 把它们当 0 比较 → 静默算错

用内存库跑，不碰演示库，也不碰生产库。
"""

import datetime
import sqlite3

import pytest

import db


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ohlcv_intraday (asset TEXT, interval TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL)")
    conn.execute("CREATE TABLE kline_bars (symbol TEXT, ts INTEGER, close REAL)")
    return conn


# ── 指纹 ──────────────────────────────────────────────────────────────────

def test_fingerprint_ignores_case_whitespace_and_punctuation():
    a = db.content_fingerprint("美联储 宣布：降息25个基点！")
    b = db.content_fingerprint("美联储宣布降息25个基点")
    assert a == b and len(a) == 32


def test_fingerprint_keeps_numbers_so_25_and_50_do_not_merge():
    # 去标点但不能去量级数字，否则「加息25」与「加息50」会被并成同一簇
    assert db.content_fingerprint("加息25个基点") != db.content_fingerprint("加息50个基点")


def test_fingerprint_strips_hash_prefix_and_empty_input():
    assert db.content_fingerprint("[hash:9f3a1b] 只有一条") == db.content_fingerprint("只有一条")
    assert db.content_fingerprint("") == ""
    assert db.content_fingerprint(None) == ""


def test_fingerprint_only_uses_first_200_chars():
    base = "美联储" * 100
    assert db.content_fingerprint(base + "结尾甲") == db.content_fingerprint(base + "结尾乙")


# ── 时间戳归一 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (1700000000, 1700000000),
    ("1700000000", 1700000000),
    (1700000000000, 1700000000),          # 13 位毫秒 → 秒
    ("1700000000000", 1700000000),
])
def test_coerce_epoch_ts_numeric(raw, expect):
    assert db.coerce_epoch_ts(raw) == expect


def test_coerce_epoch_ts_iso_keeps_its_own_timezone():
    expect = int(datetime.datetime(
        2026, 11, 17, 14, 6, 40,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp())
    assert db.coerce_epoch_ts("2026-11-17T14:06:40+08:00") == expect


def test_coerce_epoch_ts_iso_utc_and_z_suffix():
    expect = int(datetime.datetime(
        2026, 11, 17, 6, 6, 40, tzinfo=datetime.timezone.utc).timestamp())
    assert db.coerce_epoch_ts("2026-11-17T06:06:40Z") == expect
    assert db.coerce_epoch_ts("2026-11-17T06:06:40+00:00") == expect


def test_coerce_epoch_ts_naive_means_local_time():
    # `YYYY-MM-DD HH:MM:SS` 这类历史数据都是本机写入 → 按本地时区解析
    naive = datetime.datetime(2026, 11, 17, 14, 6, 40)
    assert db.coerce_epoch_ts("2026-11-17 14:06:40") == int(naive.astimezone().timestamp())
    assert db.coerce_epoch_ts("2026/11/17 14:06:40") == int(naive.astimezone().timestamp())


@pytest.mark.parametrize("raw", [None, "", "   ", "不是时间", True, [], {}])
def test_coerce_epoch_ts_never_guesses(raw):
    assert db.coerce_epoch_ts(raw, default=-1) == -1
    assert db.coerce_epoch_ts(raw) is None


# ── 衰减与年龄 ────────────────────────────────────────────────────────────

def test_compute_decay_basics():
    assert db.compute_decay(0.0, 0.5, 3600) == 0.0
    assert db.compute_decay(-1.0, 0.5, 3600) == 0.0
    assert db.compute_decay(1.0, 0.0, 0) == pytest.approx(1.0)
    # λ 取小的量级：λ=0.5/s 时 3600 秒已指数下溢到 0，两档都变 0 就测不出"越久越弱"
    assert db.compute_decay(1.0, 1e-5, 86400) < db.compute_decay(1.0, 1e-5, 3600) < 1.0


def test_compute_decay_clamps_negative_age_and_negative_lambda():
    # 时钟漂移导致 age 为负时不能放大强度；λ 为负按无衰减
    assert db.compute_decay(1.0, 0.5, -100) == pytest.approx(1.0)
    assert db.compute_decay(1.0, -0.5, 86400) == pytest.approx(1.0)


def test_safe_age_seconds_guards():
    now = 1_700_000_000
    assert db.safe_age_seconds(None, now=now) == 30 * 86400
    assert db.safe_age_seconds(0, now=now) == 30 * 86400
    assert db.safe_age_seconds("垃圾", now=now) == 30 * 86400
    assert db.safe_age_seconds(now + 60, now=now) == 0            # wall-clock 回退
    assert db.safe_age_seconds(now - 120, now=now) == 120
    assert db.safe_age_seconds(now - 400 * 86400, now=now) == 30 * 86400
    assert db.safe_age_seconds(now - 3600, now=now, max_age=60) == 60


# ── 结算合理性 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,expect", [
    (0.0, False), (12.5, False), (90.0, False),
    (90.1, True), (-95.0, True), (None, False), ("abc", False),
])
def test_is_implausible_outcome(pct, expect):
    assert db.is_implausible_outcome(pct) is expect


# ── action / assets 归一 ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("buy", "BUY"), (" Sell ", "SELL"), ("HOLD", "HOLD"),
    ("bullish", "BUY"),          # COT 极空 → 看涨反转
    ("bearish", "SELL"),         # COT 极多 → 看跌反转
    ("E020", "HOLD"), ("", "HOLD"), (None, "HOLD"),
])
def test_normalize_news_action(raw, expect):
    assert db.normalize_news_action(raw) == expect


def test_normalize_news_assets_keeps_only_tickers():
    # 门卫标签里混着中文板块类别（美股/原油/黄金），这一列只放品种代码
    assert db.normalize_news_assets('["美股", "tsla", "原油", "NVDA"]') == "TSLA,NVDA"


def test_normalize_news_assets_comma_input_dedupe_and_cap():
    assert db.normalize_news_assets("btc, BTC ,eth") == "BTC,ETH"
    many = ",".join([f"T{i}" for i in range(20)])
    assert len(db.normalize_news_assets(many).split(",")) == 10


@pytest.mark.parametrize("raw", [None, "", "[]", "[", "中文板块", "  "])
def test_normalize_news_assets_empty_inputs(raw):
    assert db.normalize_news_assets(raw) == ""


# ── 表结构探测 ────────────────────────────────────────────────────────────

def test_table_columns_reads_pragma():
    conn = _mem()
    assert db.table_columns(conn, "market_ticks") == {"symbol", "ts", "price"}
    assert db.table_columns(conn, "no_such_table") == set()


# ── 入场价：往回找最近一根 ────────────────────────────────────────────────

def test_resolve_entry_price_prefers_intraday():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',1000,101.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC',500,90.0)")
    conn.execute("INSERT INTO market_ticks VALUES ('BTC',900,80.0)")
    assert db.resolve_entry_price(conn, "btc", 1100) == pytest.approx(101.0)


def test_resolve_entry_price_falls_back_to_daily_then_ticks():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_daily VALUES ('WTI',1000,90.0)")
    conn.execute("INSERT INTO market_ticks VALUES ('WTI',1000,80.0)")
    assert db.resolve_entry_price(conn, "WTI", 1100) == pytest.approx(90.0)

    conn2 = _mem()
    conn2.execute("INSERT INTO market_ticks VALUES ('XAU',1000,80.0)")
    assert db.resolve_entry_price(conn2, "XAU", 1100) == pytest.approx(80.0)


def test_resolve_entry_price_uses_kline_bars_with_ms_timestamps():
    # 白银/原油 1m 线只在 kline_bars（毫秒 ts），v432 之前这品种 entry_price 恒 NULL
    conn = _mem()
    conn.execute("INSERT INTO kline_bars VALUES ('XAG',1_700_000_000_000,24.5)")
    assert db.resolve_entry_price(conn, "XAG", 1_700_000_060) == pytest.approx(24.5)


def test_resolve_entry_price_respects_lookback_and_guards():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC',1,50.0)")
    assert db.resolve_entry_price(conn, "BTC", 1 + 40 * 86400) is None   # 超出 30 天回看
    assert db.resolve_entry_price(conn, "BTC", 0) is None
    assert db.resolve_entry_price(conn, "", 1000) is None
    assert db.resolve_entry_price(conn, "NONE", 1000) is None
    assert db.resolve_entry_price(conn, "BTC", "abc") is None


def test_resolve_entry_price_ignores_non_positive_close():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('ETH','1m',1000,0.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('ETH',900,120.0)")
    assert db.resolve_entry_price(conn, "ETH", 1100) == pytest.approx(120.0)


# ── 到期价：前后各找一根，且只认 1m ──────────────────────────────────────

def test_price_at_prefers_1m_bar_before_expiry():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',995,100.0)")
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',1005,103.0)")
    assert db.price_at(conn, "BTC", 1000) == pytest.approx(100.0)   # as-of，不用未来数据


def test_price_at_ignores_non_1m_intervals():
    # 主键是 (asset, interval, ts)，不过滤 interval 会取到 4h 收盘价 —— 离到期差 4 小时
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','4h',999,999.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC',900,90.0)")
    assert db.price_at(conn, "BTC", 1000, daily_max_lag_sec=86400) == pytest.approx(90.0)


def test_price_at_forward_bar_only_when_nothing_before():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('ETH','1m',1002,105.0)")
    assert db.price_at(conn, "ETH", 1000, tolerance_sec=300) == pytest.approx(105.0)


def test_price_at_daily_fallback_takes_nearest_not_oldest():
    # v386 事故：旧实现 ORDER BY ts ASC 取到窗口最老的日线，凭空造出 -3.17%/笔
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_daily VALUES ('WTI',990,111.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('WTI',900,222.0)")
    conn.execute("INSERT INTO ohlcv_daily VALUES ('WTI',500,333.0)")
    assert db.price_at(conn, "WTI", 1000) == pytest.approx(111.0)


def test_price_at_daily_fallback_respects_lag_cap():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_daily VALUES ('WTI',1000 - 2 * 86400, 86.43)")
    assert db.price_at(conn, "WTI", 1000) is None


@pytest.mark.parametrize("asset,ts", [("", 1000), ("NONE", 1000), (None, 1000), ("BTC", None), ("BTC", "abc")])
def test_price_at_guard_conditions(asset, ts):
    assert db.price_at(_mem(), asset, ts) is None


# ── 远期价与收益 ──────────────────────────────────────────────────────────

def test_forward_price_offsets_by_settle_days():
    conn = _mem()
    target = 1000 + 4 * 86400
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',?,110.0)", (target + 60,))
    assert db.forward_price(conn, "BTC", 1000, settle_days=5) == pytest.approx(110.0)


def test_forward_price_skips_bars_before_target():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',1010,50.0)")   # 窗口外
    conn.execute("INSERT INTO ohlcv_daily VALUES ('BTC',1000 + 4 * 86400,120.0)")
    assert db.forward_price(conn, "BTC", 1000, settle_days=5) == pytest.approx(120.0)


def test_forward_price_min_ts_pushes_target_for_t_plus_one():
    # T+1 股票不能用当天价平仓，min_ts 把目标时刻顶上去
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('AAPL','1m',?,100.0)", (1001,))
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('AAPL','1m',?,130.0)", (1000 + 2 * 86400,))
    assert db.forward_price(conn, "AAPL", 1000, settle_days=1) == pytest.approx(100.0)
    assert db.forward_price(conn, "AAPL", 1000, settle_days=1,
                            min_ts=1000 + 2 * 86400) == pytest.approx(130.0)


def test_forward_outcome_pct_and_none_cases():
    conn = _mem()
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('BTC','1m',?,110.0)",
                 (1000 + 4 * 86400 + 30,))
    assert db.forward_outcome_pct(conn, "BTC", 1000, 100.0) == pytest.approx(10.0)
    assert db.forward_outcome_pct(conn, "BTC", 1000, None) is None
    assert db.forward_outcome_pct(conn, "BTC", 1000, 0) is None
    assert db.forward_outcome_pct(conn, "ETH", 1000, 100.0) is None


# ── 报价健康度：冻结价在源头拦住 ─────────────────────────────────────────

def test_quote_health_empty_when_no_ticks():
    assert db.quote_health(_mem(), "WTI", now_ts=1000) == {}
    assert db.quote_health(_mem(), "", now_ts=1000) == {}


def test_quote_health_flags_frozen_price():
    # 事故：WTI 报 88.985 冻了 22.5 小时，系统照常建仓结算
    conn = _mem()
    for i in range(30):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI',?,88.985)", (1000 - i * 30,))
    h = db.quote_health(conn, "WTI", now_ts=1000, window_sec=3600)
    assert h["frozen"] is True
    assert h["distinct_prices"] == 1 and h["n_ticks"] == 30
    assert h["age_sec"] == 0


def test_quote_health_moving_price_is_not_frozen():
    conn = _mem()
    for i in range(30):
        conn.execute("INSERT INTO market_ticks VALUES ('BTC',?,?)", (1000 - i * 30, 100 + i))
    h = db.quote_health(conn, "BTC", now_ts=1000, window_sec=3600, min_ticks=20)
    assert h["frozen"] is False and h["distinct_prices"] == 30


def test_quote_health_few_ticks_never_frozen_but_reports_age():
    conn = _mem()
    for i in range(3):
        conn.execute("INSERT INTO market_ticks VALUES ('BTC',?,100.0)", (900 - i,))
    h = db.quote_health(conn, "BTC", now_ts=1000, window_sec=3600, min_ticks=20)
    assert h["frozen"] is False
    assert h["age_sec"] == 100
