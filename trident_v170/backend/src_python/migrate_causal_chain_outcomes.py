"""
v302+ P0: 因果图谱反馈闭环 (causal_chain_outcomes 表)
- 目的: 把每条 causal_chain 在 5 个交易日后的实际表现登记, 用于校准 confidence 与排除干扰
- 数据流: evaluator 每日扫 causal_chain_outcomes.status='PENDING' AND eval_deadline_ts<=now
         → 查 ohlcv_daily T+5 真实涨跌 → 写 actual_* 字段 + status='HIT'/'MISS'/'NEUTRAL'
         → 同时 UPDATE causal_chains.hit_count/miss_count
- 配合: causal_chains_score_batch (v292) 已经把 hit/miss 写到表里, 但只有机会评价
       没有按"单次事件"归档. 本表补齐"逐次事件"明细, 便于复盘.
"""
import sqlite3
import os
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS causal_chain_outcomes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id                TEXT    NOT NULL,
    trigger_ts              INTEGER NOT NULL,
    trigger_source          TEXT    NOT NULL DEFAULT 'news_events',
    trigger_ref_id          INTEGER,
    trigger_headline        TEXT,
    trigger_keywords_matched TEXT,
    predicted_direction     TEXT    NOT NULL DEFAULT 'BOTH',
    predicted_mag_lo        REAL    NOT NULL DEFAULT 0.0,
    predicted_mag_hi        REAL    NOT NULL DEFAULT 999.0,
    eval_deadline_ts       INTEGER NOT NULL,
    actual_close_at_trigger REAL,
    actual_close_at_5d      REAL,
    actual_pct_5d           REAL,
    status                  TEXT    NOT NULL DEFAULT 'PENDING',
    direction_hit           INTEGER,
    magnitude_hit           INTEGER,
    notes                   TEXT,
    evaluated_at            INTEGER,
    created_at              INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(chain_id, trigger_ts, trigger_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_chain
    ON causal_chain_outcomes(chain_id, trigger_ts DESC);

CREATE INDEX IF NOT EXISTS idx_outcomes_status_deadline
    ON causal_chain_outcomes(status, eval_deadline_ts);

CREATE INDEX IF NOT EXISTS idx_outcomes_status_chain
    ON causal_chain_outcomes(status, chain_id);
"""


def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ DB not found: {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    # 列出现有表
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%outcomes%' OR name LIKE '%causal%' ORDER BY name"
    ).fetchall()
    print("✅ causal_chain_outcomes 表已就绪")
    print("   相关表:", [r[0] for r in rows])
    # 检查 schema
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='causal_chain_outcomes'"
    ).fetchone()
    if schema:
        print("   schema:", schema[0][:200], "...")
    conn.close()


if __name__ == "__main__":
    main()
