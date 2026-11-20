# -*- coding: utf-8 -*-
"""cron_kline_aggregate.py — 用实时 market_ticks 补齐 kline_bars 的 1h/4h/1d 缺口。

为什么需要这个模块
------------------
数据链路是::

    market_ticks(秒级实时) → kline_bars(1h/4h/1d) → ohlcv_daily → tech_signals

其中 ``kline_bars`` 的主动写入方 ``api_server._kline_backfill_watcher`` 每 30min
拉一次, 但它对**所有**品种都调用 ``_fetch_binance_klines``。币安没有黄金/白银/
原油, 所以 XAU / XAG / WTI 必然拉取失败(静默 fail)。实测::

    WTI  1h 最新 = 2026-10-30 20:00   (落后 3 天)
    WTI  1d 最新 = 2026-10-27 00:00   (落后 5 天)
    XAU  1h 最新 = 2026-10-30 20:00   (落后 3 天)

而 crypto 走币安源一直正常(1h 更新到当前小时)。kline_bars 一断, 下游
``ohlcv_daily`` 停更 → ``tech_signals`` 停更 → 归因页的 K 线形态证据 / 错过信号
的形态维度 / signal_impact 全部在拿几天前的旧形态当"当前形态"。

本模块的做法
------------
不碰任何外部 API —— **``market_ticks`` 是活的**(WTI/XAU/BTC/ETH/SOL 每秒都在写,
覆盖最近 30 天), 直接从它聚合出缺失的 1h/4h/1d:

* **只补缺口, 不覆盖活源** —— 若 ``kline_bars`` 里该 (symbol, timeframe) 的最新
  bar 起点已经 >= 当前应有时点, 说明外部源还活着, 直接跳过。crypto 因此完全
  不受影响(绝不拿 tick 聚合覆盖 OKX 的真实 K 线)。
* **口径与 crypto 一致** —— 同样写入未收盘的当前 bar(币安源的 1h/1d 也含)。
* **不伪造成交量** —— market_ticks 没有成交量字段, 这里用该 bar 内的 **tick
  计数** 作为活跃度代理, source 标 ``kline_bars_agg`` 语义在下游可见。

用法::

    python3 cron_kline_aggregate.py --dry-run   # 只报告不写库
    python3 cron_kline_aggregate.py             # 补齐并回填 ohlcv_daily
"""
from __future__ import annotations
try:  # 重批量写：进入批处理车道（同一时刻只放一个），拿不到本轮让道
    from batch_lane import acquire_or_exit as _lane_enter
except Exception:  # 车道模块不可用 → 照旧跑，不引入新故障点
    _lane_enter = None


import os
import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"

# timeframe -> 秒数。刻意只做这三个: 它们是 tech_detector / ohlcv_daily / 归因
# K 线证据真正消费的周期。
TIMEFRAME_SEC = {"1h": 3600, "4h": 14400, "1d": 86400}

# 与 api_server._KLINE_BACKFILL_ASSETS 保持一致的市场范围。
SYMBOLS = ("BTC", "ETH", "SOL", "XAU", "XAG", "WTI")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def expected_start(tf_sec: int, now_ts: int) -> int:
    """当前 bar 的起始秒 (floor 到周期边界)。"""
    return (now_ts // tf_sec) * tf_sec


def _exec_retry(conn, sql, rows, tries=6):
    """写库遇到 database is locked 就退避重试。

    这个 cron 每小时跑一次，但引擎/API/bridge 都在同时写同一个库，
    单次 busy_timeout 等不到锁就直接 SQLITE_BUSY（日志里连续几天 rc=1）。
    写的是 INSERT OR IGNORE / UPSERT，幂等，重试安全。
    """
    for attempt in range(tries):
        try:
            return conn.executemany(sql, rows)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))

def last_bar_start_ms(conn: sqlite3.Connection, symbol: str, tf: str) -> int | None:
    """kline_bars 里该 (symbol, timeframe) 最新 bar 的起始时间(毫秒)。"""
    row = conn.execute(
        "SELECT MAX(ts) FROM kline_bars WHERE symbol=? AND timeframe=?",
        (symbol, tf),
    ).fetchone()
    return row[0] if row and row[0] is not None else None



def fetch_ticks(conn: sqlite3.Connection, symbol: str, since_ts: int) -> list[tuple[int, float]]:
    """一次性把该品种的 tick 拉出来 —— 供 1h/4h/1d 三个周期复用。

    v396: 原来每个 timeframe 各查一次，5 天窗口 × 6 品种 × 3 周期 = 18 次扫描、
    同一批 tick 被反复搬进 Python。这里改成只读一次。
    """
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks "
        "WHERE symbol=? AND ts>=? AND price>0 ORDER BY ts ASC",
        (symbol, since_ts),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def aggregate_rows(
    ticks: list[tuple[int, float]], tf_sec: int
) -> dict[int, tuple[float, float, float, float, int]]:
    """把已读出来的 tick 聚成 bar（纯内存，不打库）。"""
    buckets: dict[int, list] = {}
    for ts, price in ticks:
        b = (int(ts) // tf_sec) * tf_sec
        slot = buckets.get(b)
        if slot is None:
            buckets[b] = [price, price, price, price, 1]
        else:
            if price > slot[1]:
                slot[1] = price
            if price < slot[2]:
                slot[2] = price
            slot[3] = price
            slot[4] += 1
    return buckets  # type: ignore[return-value]


def aggregate_ticks(
    conn: sqlite3.Connection, symbol: str, tf_sec: int, since_ts: int
) -> dict[int, tuple[float, float, float, float, int]]:
    """把 market_ticks 聚合成 bar。

    返回 ``{bar_start_sec: (open, high, low, close, tick_count)}``。

    注意:
    * market_ticks.ts 是**秒**, kline_bars.ts 是**毫秒** —— 这里统一用秒做桶键,
      落库时再换算成毫秒。
    * 成交量 = tick 计数(代理值), 不是交易所成交量。
    """
    return aggregate_rows(fetch_ticks(conn, symbol, since_ts), tf_sec)


def plan_symbol(
    conn: sqlite3.Connection, symbol: str, now_ts: int
) -> list[tuple[str, int, int, str]]:
    """决定该 symbol 需要补哪些 (timeframe, bar_start_sec, action, reason)。

    action:
      * ``0 skip``   —— 外部源还活着, 不碰
      * ``2 fill``   —— 补缺口: 历史 bar 只 insert, 当前 bar upsert
    """
    plan: list[tuple[str, int, int, str]] = []
    for tf, tf_sec in TIMEFRAME_SEC.items():
        exp = expected_start(tf_sec, now_ts)
        last_ms = last_bar_start_ms(conn, symbol, tf)
        if last_ms is not None and (last_ms // 1000) >= exp:
            plan.append((tf, exp, 0, "source_alive"))
            continue
        if last_ms is not None:
            since = last_ms // 1000      # 从库里最后那根之后开始补
        else:
            row = conn.execute(
                "SELECT MIN(ts) FROM market_ticks WHERE symbol=?", (symbol,)
            ).fetchone()
            if not row or row[0] is None:
                plan.append((tf, exp, 0, "no_tick_data"))
                continue
            since = int(row[0])
        plan.append((tf, exp, 2, "fill_gap"))
    return plan



def _setup_hard_timeout(seconds: int) -> None:
    """整轮硬超时：到点直接退出进程，绝不恋战。

    v396：实测这个 cron 曾以 85% CPU 连跑 17 分钟，期间 bridge 启动被 DB 写锁卡住
    6 分钟起不来。软上限（循环里检查）挡不住"单个批次算很久"，所以要真刹车。

    为什么用线程 + os._exit 而不是 SIGALRM：
    本机实测 `signal.alarm()` 不生效（进程的 SIGALRM 处于被屏蔽状态，Python 里
    直接 signal.signal + alarm(1) 也照样睡满 4 秒不中断）。看门狗线程在
    CPython 里能正常跑，`os._exit()` 一定会终结进程并释放 SQLite 锁。

    已提交的批次保留（每个 (symbol,tf) 批次都单独 commit），未提交的自然丢弃，
    整轮幂等，下一轮会补上。
    """
    if seconds <= 0:
        return
    import threading

    def _watch() -> None:
        time.sleep(seconds)
        print(f"[HARD-TIMEOUT] 整轮超过 {seconds}s 硬上限 → 立即退出"
              f"（已提交批次保留）", flush=True)
        _log_perf(f"HARD-TIMEOUT after {seconds}s")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    t = threading.Thread(target=_watch, name="kline-hard-timeout", daemon=True)
    t.start()


def _log_perf(line: str) -> None:
    """把每轮耗时写进独立小日志，供次日核查（stdout 会被 launchd 的 out.log 吞没）。"""
    try:
        log_dir = Path.home() / ".trident" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "kline_aggregate.log", "a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="用 market_ticks 补齐 kline_bars 1h/4h/1d")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--max-runtime", type=float, default=None,
                    help="整轮时长上限（秒），覆盖 KLINE_MAX_RUNTIME_S")
    ap.add_argument("--hard-timeout", type=float, default=None,
                    help="整轮硬超时（秒），覆盖 KLINE_HARD_TIMEOUT_S")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    now_ts = int(time.time())
    conn = _connect()
    inserted = 0
    updated = 0
    # 2026-11-16 让路节流：实测本任务曾以 48% CPU 连跑 8 分钟，期间 30+ 个写入方
    # （含 api 启动）全部排队 → 8000 端口整段不可用。根因不是"它不 commit"，
    # 而是 commit 之间的间隙太密，交互式写者抢不到窗口。
    # 所以：每批提交后 sleep 一小段，并给整轮一个时长上限（到点收工，下轮再来）。
    _sleep_s = float(os.getenv("KLINE_BATCH_SLEEP_S", "0.2"))
    _max_runtime_s = (args.max_runtime if args.max_runtime is not None
                      else float(os.getenv("KLINE_MAX_RUNTIME_S", "600")))
    _hard_s = (args.hard_timeout if args.hard_timeout is not None
               else float(os.getenv("KLINE_HARD_TIMEOUT_S", "900")))
    _run_t0 = time.time()
    _cut_short = False
    _setup_hard_timeout(int(_hard_s))
    try:
        for symbol in SYMBOLS:
            if time.time() - _run_t0 > _max_runtime_s:
                print(f"[CAP] 本轮已跑 {time.time() - _run_t0:.0f}s，超过上限 "
                      f"{_max_runtime_s:.0f}s → 收工，剩余留给下一轮", flush=True)
                _cut_short = True
                break
            plan = plan_symbol(conn, symbol, now_ts)
            alive = [tf for tf, _, act, _ in plan if act == 0]
            gaps = [tf for tf, _, act, _ in plan if act == 2]
            if not gaps:
                print(f"  {symbol:<6} ok (live: {','.join(alive) or '-'})")
                continue
            # v396：先把该品种的 since 算到最早（跨周期取最小），tick 只读一次
            since_by_tf: dict[str, int] = {}
            for tf, exp, action, _reason in plan:
                if action == 0:
                    continue
                last_ms = last_bar_start_ms(conn, symbol, tf)
                since = (last_ms // 1000) if last_ms is not None else 0
                # v370: 本机只有 2G 内存, 一次回溯几个月 market_ticks 会占满 CPU
                #   (实测单进程 50%+ 跑 20 分钟以上), 把 api 启动饿死在 startup。
                #   只补最近 5 天即可修正日内/日线; 更早的历史缺口不在这里追。
                since_by_tf[tf] = max(since, now_ts - 5 * 86400)
            _ticks_cache: tuple[int, list[tuple[int, float]]] | None = None
            for tf, exp, action, _reason in plan:
                if action == 0:
                    continue
                tf_sec = TIMEFRAME_SEC[tf]
                since = since_by_tf[tf]
                # 跨周期复用：1d 的 since 通常更早（覆盖 1h/4h 的需求），
                # 但 1h 的 since 可能更晚；取"更早"的那个，一次读够，绝不多读。
                _req_since = min(since_by_tf.values())
                if _ticks_cache is None or _ticks_cache[0] != _req_since:
                    _ticks_cache = (_req_since, fetch_ticks(conn, symbol, _req_since))
                buckets = aggregate_rows(
                    [t for t in _ticks_cache[1] if t[0] >= since], tf_sec
                )
                if not buckets:
                    print(f"  {symbol:<6} {tf:<3} no_ticks")
                    continue
                rows = []
                for b_start, (o, h, l, c, v) in sorted(buckets.items()):
                    if b_start > exp:
                        continue          # 未来桶, 丢弃
                    # v335: 日线不许出现周末。
                    #   加密/贵金属是 24 小时连续报价，按"自然日"直接聚合会把周六周日
                    #   也切成 1d 桶；XAU 就是这么被写坏的 —— 161 个交易日塞了 1,519 行，
                    #   其中 1,344 行是同一个收盘价反复复制，任何均线/波动率都失真。
                    if tf == "1d":
                        if symbol in ("XAU", "XAG"):
                            continue      # 贵金属日线由 metals_daily.py（COMEX 一手源）负责
                        if time.localtime(b_start).tm_wday >= 5:
                            continue      # 周六/周日不生成日线
                    rows.append((symbol, tf, b_start * 1000, o, h, l, c, float(v)))
                if not rows:
                    print(f"  {symbol:<6} {tf:<3} nothing_new")
                    continue
                if args.dry_run:
                    print(f"  {symbol:<6} {tf:<3} would_write={len(rows)} "
                          f"(latest={rows[-1][2] // 1000})")
                    continue
                # 历史 bar 用 INSERT OR IGNORE(不覆盖已有真实 K 线);
                # 当前未收盘 bar 用 UPSERT(价格每分钟都在变, 必须能刷新)。
                cur = _exec_retry(conn,
                    "INSERT OR IGNORE INTO kline_bars "
                    "(symbol,timeframe,ts,open,high,low,close,volume,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                    rows,
                )
                inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                cur = _exec_retry(conn,
                    "INSERT INTO kline_bars "
                    "(symbol,timeframe,ts,open,high,low,close,volume,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime')) "
                    "ON CONFLICT(symbol,timeframe,ts) DO UPDATE SET "
                    "open=excluded.open,high=excluded.high,low=excluded.low,"
                    "close=excluded.close,volume=excluded.volume",
                    [r for r in rows if r[2] // 1000 == exp],
                )
                updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                # v369 关键修复: 原来整轮只在最后 commit 一次 —— 第一个 INSERT 就开启
                #  写事务并一直持到结束, 实测能攥着 SQLite 写锁 13 小时, 导致 api 启动
                #  永远卡在 "Waiting for application startup"、全站 5xx。
                #  改成每个 (symbol,tf) 批次立即提交, 锁按毫秒级释放。
                conn.commit()
                if _sleep_s > 0:
                    time.sleep(_sleep_s)   # 让路：把写锁窗口让给 api/采集
                print(f"  {symbol:<6} {tf:<3} written={len(rows)} "
                      f"latest={rows[-1][2] // 1000}")
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    _kline_dur = time.time() - _run_t0
    print(f"[OK] kline_bars 补齐: inserted={inserted} updated={updated}"
          f"{'  (dry-run 未落库)' if args.dry_run else ''}")
    print(f"[PERF] kline 阶段 {_kline_dur:.1f}s (软上限 {_max_runtime_s:.0f}s"
          f"{', 被截断' if _cut_short else ''})")

    if args.dry_run:
        _log_perf(f"dry-run kline={_kline_dur:.1f}s inserted=0 updated=0")
        return 0

    # 阶段间也让一次：kline 阶段跑久了，先把写锁让出来再算 ohlcv
    if _sleep_s > 0:
        time.sleep(max(_sleep_s, 0.5))

    # 补完 kline_bars → 顺手把 ohlcv_daily 也刷新(下游 tech_signals 靠它)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backfill_ohlcv_local import build_rows

    conn = _connect()
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS ohlcv_daily ("
            "asset TEXT NOT NULL, ts INTEGER NOT NULL, open REAL NOT NULL,"
            " high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,"
            " volume INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'yfinance',"
            " UNIQUE(asset, ts));"
            "CREATE INDEX IF NOT EXISTS idx_ohlcv_asset_ts "
            "ON ohlcv_daily(asset, ts DESC);"
        )
        rows, stats = build_rows(conn)
        # v341: 落库前统一品种名 —— 取数端给的是行情符号（BTC-USD / GC=F），
        # 直接入库就会和规范代码（BTC / XAU）各存一份，同一品种被拆成两份样本。
        from asset_alias import canonical as _canon

        rows = [(_canon(r[0]),) + tuple(r[1:]) for r in rows]
        dedup: dict[tuple[str, str], tuple] = {}
        for r in rows:
            # 同资产同一天只留 ts 最大的一根，否则又会出现"一天两根"把次日收益算错
            day = dt.datetime.fromtimestamp(r[1], dt.UTC).strftime("%Y-%m-%d")
            k = (r[0], day)
            if k not in dedup or r[1] > dedup[k][1]:
                dedup[k] = r
        _t_ohlcv0 = time.time()
        if dedup:
            # v396：原来每轮把 10 万行日线全量 upsert 一遍 —— 实测这一步单独就吃
            #   60s+ CPU、并把写锁攥住好几分钟（17 分钟那次 CPU 峰值的真凶在 ohlcv，
            #   不在 kline 聚合）。日线历史是不会变的，只有最近几天会刷新。
            #   所以只写"窗口内"和"库里还没有"的行；窗口可用
            #   OHLCV_UPSERT_WINDOW_DAYS 调整（默认 3 天）。
            _win_days = int(os.getenv("OHLCV_UPSERT_WINDOW_DAYS", "3"))
            _max_existing = conn.execute(
                "SELECT MAX(ts) FROM ohlcv_daily"
            ).fetchone()
            _have_max = int(_max_existing[0]) if _max_existing and _max_existing[0] else 0
            if _have_max <= 0:
                _to_write = list(dedup.values())      # 空表首跑：全量铺底
            else:
                _floor = _have_max - _win_days * 86400
                _to_write = [r for r in dedup.values() if r[1] >= _floor]
            _skipped = len(dedup) - len(_to_write)
            _chunk = 2000
            for _i in range(0, len(_to_write), _chunk):
                conn.executemany(
                    "INSERT INTO ohlcv_daily "
                    "(asset,ts,open,high,low,close,volume,source) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(asset,ts) DO UPDATE SET "
                    "open=excluded.open,high=excluded.high,low=excluded.low,"
                    "close=excluded.close,volume=excluded.volume,source=excluded.source",
                    _to_write[_i:_i + _chunk],
                )
                conn.commit()
                if _i + _chunk < len(_to_write) and _sleep_s > 0:
                    time.sleep(_sleep_s)
            print(f"[OHLCV] 写入 {len(_to_write)} 行（窗口 {_win_days}d 内），"
                  f"跳过 {_skipped} 行历史不变行")
        print(f"[OK] ohlcv_daily 刷新: 候选 {len(dedup)} 行 "
              f"(源 {stats['src_rows']} 行, 丢弃非法 {stats['dropped']})")
        _ohlcv_dur = time.time() - _t_ohlcv0
        _total_dur = time.time() - _run_t0
        print(f"[PERF] ohlcv 阶段 {_ohlcv_dur:.1f}s, 整轮 {_total_dur:.1f}s")
        _log_perf(f"kline={_kline_dur:.1f}s ohlcv={_ohlcv_dur:.1f}s "
                  f"total={_total_dur:.1f}s inserted={inserted} updated={updated} "
                  f"ohlcv_rows={len(dedup)} cut_short={int(_cut_short)}")
        for sym, n in sorted(stats["assets"].items(), key=lambda x: -x[1]):
            mx = conn.execute(
                "SELECT MAX(ts) FROM ohlcv_daily WHERE asset=?", (sym,)
            ).fetchone()[0]
            day = dt.datetime.fromtimestamp(mx, dt.UTC).strftime("%Y-%m-%d")
            print(f"  {sym:<8}{n:>7}   最新交易日 {day}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if _lane_enter:
        _lane_enter(__file__.split("/")[-1].replace(".py", ""))
    raise SystemExit(main())
