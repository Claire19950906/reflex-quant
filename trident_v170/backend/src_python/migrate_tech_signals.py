"""
migrate_tech_signals.py — v303: 建 tech_signals 表 (异动识别新存储)
=====================================================================
设计约束(用户明确要求):
  * **有数据才显示, 没有就不显示** —— 每行必须能追溯到真实 OHLCV
  * **不用假数据填充** —— 不写入任何推测/占位信号
  * **不用过时数据误导** —— 写入时记录数据水位, 供读取端判断新鲜度

表设计
------
tech_signals: 取代 critical_price_moves 的"纯涨跌幅统计"角色。
  asset          标的
  ts             信号所在 bar 的时间戳(= 该 bar 的真实 ts, 不是窗口起点)
  close          该 bar 收盘价(真实)
  trigger_type   divergence/volume/support/atr/reversal/liquidation/combo/pct
  score          0~0.95
  uses_future    0=纯因果, 1=转折点(用户许可使用未来函数)
  pct_1d/pct_3d  真实涨跌幅
  vol_ratio      真实量比
  support_level  支撑位(可空)
  signal_json    完整命中信号明细
  merged_types   被合并的其它类型
  bars_available 该信号计算时可用 bar 数(数据充足度)
  data_max_ts    写入时该资产最新 bar ts(新鲜度水位, 供读取端判陈旧)
  created_at
UNIQUE(asset, ts) 保证同 ts 单行, 与 _merge_same_ts 语义一致。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"

DDL = """
CREATE TABLE IF NOT EXISTS tech_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset          TEXT    NOT NULL,
    ts             INTEGER NOT NULL,
    close          REAL,
    trigger_type   TEXT    NOT NULL,
    score          REAL    NOT NULL,
    uses_future    INTEGER NOT NULL DEFAULT 0,
    pct_1d         REAL,
    pct_3d         REAL,
    vol_ratio      REAL,
    support_level  REAL,
    signal_json    TEXT,
    merged_types   TEXT,
    bars_available INTEGER,
    data_max_ts    INTEGER,
    created_at     INTEGER NOT NULL,
    UNIQUE(asset, ts)
);
CREATE INDEX IF NOT EXISTS idx_tech_signals_asset_ts
    ON tech_signals(asset, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tech_signals_type
    ON tech_signals(trigger_type, ts DESC);
"""


def migrate(conn: sqlite3.Connection) -> dict:
    conn.executescript(DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tech_signals)")}
    conn.commit()
    return {"created": True, "columns": sorted(cols)}


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB_PATH)
    try:
        info = migrate(conn)
        n = conn.execute("SELECT COUNT(*) FROM tech_signals").fetchone()[0]
        print(f"[OK] tech_signals 就绪, 现有 {n} 行")
        print(f"[OK] columns: {','.join(info['columns'])}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())