"""
migrate_causal_chains.py
==========================

建 causal_chains 表 (当前不存在) + 初始化种子规则.

schema 设计:
  - chain_id    : 链路 ID (asset + pattern 唯一)
  - asset       : 目标资产
  - trigger_pattern: 触发模式 (e.g. "fed_hike_expectation", "flash_crash")
  - chain_steps : JSON 数组, 每步一个因果节点
  - confidence  : 当前置信度 0~1 (随 hit/miss 衰减或上调)
  - version     : 版本号 (每次因果链升级 +1)
  - hit_count / miss_count: 经实时信号台验证过的次数
  - source      : 来源 ("auto_5why" / "manual_review" / "rule_mined")
  - active      : 是否启用 (1/0)
  - last_verified_ts: 最近一次验证时间

idempotent — 重复运行安全.
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS causal_chains (
    chain_id          TEXT    PRIMARY KEY,
    asset             TEXT    NOT NULL,
    trigger_pattern   TEXT    NOT NULL,
    chain_steps       TEXT    NOT NULL DEFAULT '[]',
    confidence        REAL    NOT NULL DEFAULT 0.5,
    version           INTEGER NOT NULL DEFAULT 1,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    miss_count        INTEGER NOT NULL DEFAULT 0,
    source            TEXT    NOT NULL DEFAULT 'rule_mined',
    active            INTEGER NOT NULL DEFAULT 1,
    last_verified_ts  INTEGER,
    created_at        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at        INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_cc_asset       ON causal_chains(asset);
CREATE INDEX IF NOT EXISTS idx_cc_pattern     ON causal_chains(trigger_pattern);
CREATE INDEX IF NOT EXISTS idx_cc_confidence  ON causal_chains(confidence DESC);
"""

SEED_CHAINS = [
    {
        "chain_id": "BTC::fed_liquidity_tightening",
        "asset": "BTC",
        "trigger_pattern": "fed_liquidity_tightening",
        "chain_steps": [
            {"step": 1, "node": "Fed hawkish dot-plot / 利率预期上行", "evidence_type": "macro", "lag_min": 0},
            {"step": 2, "node": "DXY 走强 + 10Y 美债收益率突破", "evidence_type": "fx", "lag_min": 30},
            {"step": 3, "node": "风险资产贴现率上行", "evidence_type": "asset_class", "lag_min": 120},
            {"step": 4, "node": "BTC/BNB/SOL 高 beta 资产抛售", "evidence_type": "crypto", "lag_min": 360},
        ],
        "confidence": 0.7,
        "source": "rule_mined",
    },
    {
        "chain_id": "BTC::regulatory_crackdown",
        "asset": "BTC",
        "trigger_pattern": "regulatory_crackdown",
        "chain_steps": [
            {"step": 1, "node": "SEC / 央行监管负面消息", "evidence_type": "news", "lag_min": 0},
            {"step": 2, "node": "市场情绪转负面 (Fear&Greed 下行)", "evidence_type": "sentiment", "lag_min": 60},
            {"step": 3, "node": "主动卖盘 CVD 翻负", "evidence_type": "flow", "lag_min": 180},
            {"step": 4, "node": "BTC 跌破关键支撑 / 慢跌持续", "evidence_type": "price", "lag_min": 720},
        ],
        "confidence": 0.65,
        "source": "rule_mined",
    },
    {
        "chain_id": "SKHYNIX::ai_capex_upcycle",
        "asset": "SKHYNIX",
        "trigger_pattern": "ai_capex_upcycle",
        "chain_steps": [
            {"step": 1, "node": "Hyperscaler CapEx 指引上调", "evidence_type": "news", "lag_min": 0},
            {"step": 2, "node": "HBM/DRAM 需求预期抬升", "evidence_type": "industry", "lag_min": 60},
            {"step": 3, "node": "DRAM 现货价开始上行 (慢变量月级别)", "evidence_type": "macro_commodity", "lag_min": 43200},
            {"step": 4, "node": "海力士/三星股价上涨", "evidence_type": "equity", "lag_min": 86400},
        ],
        "confidence": 0.75,
        "source": "rule_mined",
    },
    {
        "chain_id": "XAU::real_yields_inverse",
        "asset": "XAU",
        "trigger_pattern": "real_yields_inverse",
        "chain_steps": [
            {"step": 1, "node": "10Y TIPS 实际利率下行", "evidence_type": "macro", "lag_min": 0},
            {"step": 2, "node": "黄金作为零息资产吸引力上升", "evidence_type": "asset_class", "lag_min": 60},
            {"step": 3, "node": "SPDR GLD 持仓流入", "evidence_type": "flow", "lag_min": 240},
            {"step": 4, "node": "XAU 上涨", "evidence_type": "price", "lag_min": 480},
        ],
        "confidence": 0.8,
        "source": "rule_mined",
    },
    {
        "chain_id": "WTI::opec_supply_cut",
        "asset": "WTI",
        "trigger_pattern": "opec_supply_cut",
        "chain_steps": [
            {"step": 1, "node": "OPEC+ 减产声明", "evidence_type": "news", "lag_min": 0},
            {"step": 2, "node": "供应收紧预期", "evidence_type": "macro_commodity", "lag_min": 30},
            {"step": 3, "node": "近月期货升水 (backwardation 减弱)", "evidence_type": "futures_curve", "lag_min": 120},
            {"step": 4, "node": "WTI 上涨", "evidence_type": "price", "lag_min": 360},
        ],
        "confidence": 0.72,
        "source": "rule_mined",
    },
]


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        print(f"[INFO] Connecting to {DB_PATH}")
        conn.executescript(SCHEMA)
        print(f"[OK] Schema applied (causal_chains table ready)")

        inserted = 0
        updated = 0
        for chain in SEED_CHAINS:
            existing = conn.execute(
                "SELECT chain_id FROM causal_chains WHERE chain_id=?",
                (chain["chain_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE causal_chains SET confidence=?, updated_at=strftime('%s','now') "
                    "WHERE chain_id=? AND confidence < ?",
                    (chain["confidence"], chain["chain_id"], chain["confidence"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO causal_chains "
                    "(chain_id,asset,trigger_pattern,chain_steps,confidence,version,source,active) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (chain["chain_id"], chain["asset"], chain["trigger_pattern"],
                     json.dumps(chain["chain_steps"], ensure_ascii=False),
                     chain["confidence"], 1, chain["source"], 1),
                )
                inserted += 1
        conn.commit()

        cnt = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
        print(f"[OK] causal_chains: inserted={inserted}, updated={updated}, total={cnt}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())