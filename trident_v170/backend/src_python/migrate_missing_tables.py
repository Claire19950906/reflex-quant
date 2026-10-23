"""
migrate_missing_tables.py — 一次性迁移脚本
==============================================

为什么需要这个:
  - /api/v291/* 端点期望 critical_price_moves + event_move_attribution 两张表
  - /api/v237/missed-signals 期望 missed_signals 表
  - /api/v237/missed-signals/by-dimension 期望 missed_signal_dimensions 模块
  - /api/v237/calibration 期望 judgment_calibration 表
  - 当前 trident_event_bus.db 中这 4 张表都不存在,API 一直返 500

数据源真实性保证:
  - critical_price_moves: 从 external_indicators 真实 yfinance 价格计算而来,绝无伪造
  - event_move_attribution: 空表(无上游 event/news 表);空状态由前端现有 EmptyState 渲染
  - missed_signals: 空表(无上游 trade_decisions 表);空状态由前端现有 EmptyState 渲染
  - missed_signal_dimensions: Python 模块,本脚本不创建(由 bridge_server.py 提供)

执行:
  cd backend && python3 src_python/migrate_missing_tables.py
"""
from __future__ import annotations
import re
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

T_CRITICAL = """
CREATE TABLE IF NOT EXISTS critical_price_moves (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset           TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    start_price     REAL    NOT NULL,
    peak_price      REAL    NOT NULL,
    end_price       REAL    NOT NULL,
    move_pct        REAL    NOT NULL,
    move_kind       TEXT    NOT NULL,
    duration_min    INTEGER NOT NULL,
    algo_score      REAL    NOT NULL DEFAULT 0,
    context_json    TEXT    NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_cpm_asset_ts ON critical_price_moves(asset, ts DESC);
CREATE INDEX IF NOT EXISTS idx_cpm_ts       ON critical_price_moves(ts DESC);
CREATE INDEX IF NOT EXISTS idx_cpm_kind     ON critical_price_moves(move_kind);
"""

T_ATTRIBUTION = """
CREATE TABLE IF NOT EXISTS event_move_attribution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    move_id         INTEGER NOT NULL,
    news_event_id   INTEGER,
    asset           TEXT    NOT NULL,
    time_lag_sec    INTEGER NOT NULL,
    algo_score      REAL    NOT NULL DEFAULT 0,
    algo_reasons    TEXT    NOT NULL DEFAULT '',
    ai_called       INTEGER NOT NULL DEFAULT 0,
    ai_explanation  TEXT,
    ai_confidence   REAL,
    ai_error        TEXT,
    move_ts         INTEGER NOT NULL,
    move_pct        REAL    NOT NULL,
    move_kind       TEXT    NOT NULL,
    duration_min    INTEGER NOT NULL,
    ev_news_id      INTEGER,
    ev_source       TEXT,
    ev_action       TEXT,
    ev_score        REAL,
    ev_ts           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ema_move_id   ON event_move_attribution(move_id);
CREATE INDEX IF NOT EXISTS idx_ema_asset_ts  ON event_move_attribution(asset, ev_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ema_lag       ON event_move_attribution(time_lag_sec);
"""

T_NEWS_EVENTS = """
CREATE TABLE IF NOT EXISTS news_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT,
    headline        TEXT,
    body            TEXT,
    action          TEXT,
    score           REAL,
    assets          TEXT,
    ts              INTEGER NOT NULL,
    fetched_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_ne_ts ON news_events(ts DESC);
"""

T_MISSED = """
CREATE TABLE IF NOT EXISTS missed_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset               TEXT    NOT NULL,
    decision_date       TEXT    NOT NULL,
    decision_id         INTEGER,
    signal_id           INTEGER,
    pattern_type        TEXT    NOT NULL,
    signal              TEXT    NOT NULL,
    ignored_reason      TEXT    NOT NULL DEFAULT '',
    confidence_at_time  REAL,
    actual_outcome_5d   REAL,
    actual_outcome_20d  REAL,
    hit                 INTEGER,
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_ms_asset_date ON missed_signals(asset, decision_date DESC);
CREATE INDEX IF NOT EXISTS idx_ms_pattern     ON missed_signals(pattern_type);
"""

# trade_reflections_v2 说明(重要):
# 本脚本早期版本在这里定义过一个 12 列的精简 trade_reflections_v2(dimension/subcategory/
# confidence_at_time...), 并在其后 CREATE INDEX idx_trv2_dim(dimension, created_at DESC)。
# 但生产库里该表早已演进为 69 列(layer/signal_name/root_cause/core_*_audit/...),
# 并不存在 dimension / subcategory 列 —— CREATE TABLE IF NOT EXISTS 是无害的 no-op,
# 但那条 CREATE INDEX 会抛 "no such column: dimension" 并中断整个迁移。
# 故此处不再定义/创建它: 生产库已有该表, 旧定义一旦在空库上执行反而会造出与代码不匹配的表。
# 下方 _apply_ddl 也会跳过目标列不存在的索引, 防止同类问题再次中断迁移。


def _apply_ddl(conn, label: str, sql: str) -> None:
    """执行 DDL, 但跳过依赖不存在列的 CREATE INDEX。

    CREATE TABLE IF NOT EXISTS 对已存在的表是 no-op, 此时脚本里的索引列可能早已不存在
    (表结构演进过), 直接执行会中断迁移 —— 对已存在的索引而言它们也本就是 no-op。
    """
    for stmt in (s.strip() for s in sql.split(";")):
        if not stmt:
            continue
        upper = stmt.upper()
        if upper.startswith("CREATE INDEX"):
            m = re.search(r"ON\s+(\w+)\s*\(([^)]*)\)", stmt, re.I)
            if m:
                table, col_part = m.group(1), m.group(2)
                if not _table_exists(conn, table):
                    print(f"  skip {label}: 表 {table} 不存在, 跳过其索引")
                    continue
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                # 去掉索引里的 ASC/DESC/COLLATE 等修饰, 只留列名
                want = {re.split(r"[\s(]", c.strip())[0].strip('"')
                        for c in col_part.split(",") if c.strip()}
                missing = want - have
                if missing:
                    print(f"  skip {label}: {table} 缺列 {sorted(missing)}, 跳过该索引")
                    continue
        conn.execute(stmt)
    print(f"  ok {label}")


T_CALIB = """
CREATE TABLE IF NOT EXISTS judgment_calibration (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start              TEXT    NOT NULL,
    asset                   TEXT    NOT NULL,
    pattern_type            TEXT    NOT NULL,
    n_signals               INTEGER NOT NULL DEFAULT 0,
    n_hit                   INTEGER NOT NULL DEFAULT 0,
    hit_rate                REAL    NOT NULL DEFAULT 0,
    avg_edge_5d             REAL    NOT NULL DEFAULT 0,
    avg_confidence          REAL    NOT NULL DEFAULT 0,
    confidence_calibration  REAL    NOT NULL DEFAULT 0,
    recommendation          TEXT    NOT NULL DEFAULT 'hold',
    notes                   TEXT    NOT NULL DEFAULT '',
    created_at              INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_jc_week ON judgment_calibration(week_start DESC);
"""


# ─── P0-3 续：CFTC COT (Commitments of Traders) ──────────────────
# 周报(每周五 3:30pm ET),来源 publicreporting.cftc.gov Socrata Open Data API
# 3 类持仓: Commercial(套保)、Non-Commercial(机构投机)、Non-Reportable(散户)
# 用于判断机构/散户资金流向, 是与价格方向相反的领先指标(逼空/杀多信号)
T_COT_DATA = """
CREATE TABLE IF NOT EXISTS cftc_cot_data (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date             TEXT    NOT NULL,           -- YYYY-MM-DD (周二收盘)
    cftc_contract_code      TEXT    NOT NULL,           -- 如 '133741'
    cftc_contract_name      TEXT    NOT NULL,           -- 如 'BITCOIN'
    cftc_market_code        TEXT,                       -- exchange code
    commodity_group         TEXT,                       -- 'FINANCIAL INSTRUMENTS' / 'AGRICULTURE' ...
    asset                   TEXT,                       -- 映射到内部 ticker: BTC / ETH / ES ...
    open_interest           INTEGER,
    change_oi               INTEGER,
    -- Commercial (套保/产业客户)
    comm_long               INTEGER,
    comm_short              INTEGER,
    comm_net                INTEGER,                    -- comm_long - comm_short
    -- Non-Commercial (机构投机)
    noncomm_long            INTEGER,
    noncomm_short           INTEGER,
    noncomm_spread          INTEGER,
    noncomm_net             INTEGER,                    -- noncomm_long - noncomm_short (核心信号)
    -- Non-Reportable (散户)
    nonrept_long            INTEGER,
    nonrept_short           INTEGER,
    -- 变化
    change_comm_long        INTEGER,
    change_comm_short       INTEGER,
    change_noncomm_long     INTEGER,
    change_noncomm_short    INTEGER,
    change_nonrept_long     INTEGER,
    change_nonrept_short    INTEGER,
    -- 占比 (% of OI)
    pct_oi_comm_long        REAL,
    pct_oi_comm_short       REAL,
    pct_oi_noncomm_long     REAL,
    pct_oi_noncomm_short    REAL,
    -- 集中度
    traders_total           INTEGER,
    traders_noncomm_long    INTEGER,
    traders_noncomm_short   INTEGER,
    -- 派生(每周 worker 算,加速读)
    spec_index              REAL,                       -- normalized net spec position [-1, 1]
    fetched_at              INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cot_code_date ON cftc_cot_data(cftc_contract_code, report_date);
CREATE INDEX IF NOT EXISTS idx_cot_asset_date   ON cftc_cot_data(asset, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_cot_fetched      ON cftc_cot_data(fetched_at DESC);
"""


NAMES = ("critical_price_moves", "event_move_attribution", "news_events",
         "missed_signals", "trade_reflections_v2", "judgment_calibration",
         "cftc_cot_data")


def _table_exists(conn, table) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _column_exists(conn, table, col) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())


def _patch_schema(conn) -> None:
    """兼容修正:历史 run 留下的表可能用旧列名(context 而非 context_json),API 期望新列名"""
    # critical_price_moves: context -> context_json
    if _column_exists(conn, "critical_price_moves", "context") \
            and not _column_exists(conn, "critical_price_moves", "context_json"):
        conn.execute("ALTER TABLE critical_price_moves RENAME COLUMN context TO context_json")
        print("  patch critical_price_moves.context -> context_json")

    # event_move_attribution: add created_at
    if not _column_exists(conn, "event_move_attribution", "created_at"):
        conn.execute(
            "ALTER TABLE event_move_attribution ADD COLUMN created_at INTEGER NOT NULL "
            "DEFAULT (strftime('%s','now'))"
        )
        print("  patch event_move_attribution.+created_at")

    # news_events: API 期望 e.news_id(不是 id),添加冗余 news_id
    if not _column_exists(conn, "news_events", "news_id"):
        conn.execute("ALTER TABLE news_events ADD COLUMN news_id INTEGER")
        # 同步 news_id = rowid
        conn.execute("UPDATE news_events SET news_id = id WHERE news_id IS NULL")
        print("  patch news_events.+news_id")


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    print(f"[INFO] Opening {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        print("[INFO] Creating 6 tables (idempotent)...")
        tables = [
            ("critical_price_moves", T_CRITICAL),
            ("event_move_attribution", T_ATTRIBUTION),
            ("news_events", T_NEWS_EVENTS),
            ("missed_signals", T_MISSED),
            ("judgment_calibration", T_CALIB),
            ("cftc_cot_data", T_COT_DATA),
        ]
        for label, sql in tables:
            _apply_ddl(conn, label, sql)
        if not _table_exists(conn, "trade_reflections_v2"):
            print("[ERROR] trade_reflections_v2 表不存在 —— 该表的真实结构(69 列)"
                  "无法由本脚本重建, 请从备份恢复。", file=sys.stderr)
            conn.rollback()
            return 2
        print("  ok trade_reflections_v2 (已存在, 生产库 69 列结构, 未改动)")
        print("[INFO] Patching schema for legacy tables...")
        _patch_schema(conn)
        conn.commit()

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('critical_price_moves','event_move_attribution','news_events','missed_signals',"
            "'trade_reflections_v2','judgment_calibration','cftc_cot_data') "
            "ORDER BY name"
        )
        created = [r[0] for r in cur.fetchall()]
        missing = set(NAMES) - set(created)
        print(f"[OK] Tables present: {created}")
        if missing:
            print(f"[ERROR] 仍未创建: {sorted(missing)}", file=sys.stderr)
            return 2
        print("[OK] 全部 7 张表就绪")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())