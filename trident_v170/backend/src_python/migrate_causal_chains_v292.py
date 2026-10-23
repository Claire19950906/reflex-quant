"""
migrate_causal_chains_v292.py
=============================

v292 增量: causal_chains 表 + 4 字段, 配合"实战反馈自动更新 hit/miss"闭环.

新字段:
  - expected_direction  : 'UP' / 'DOWN' / 'BOTH' (触发后期望的方向)
  - magnitude_lo / hi    : 期望幅度区间 (%) 用于过滤掉噪声
  - trigger_keywords     : 关键词 (逗号分隔), 在 subcategory / reason 中匹配

回填 5 条种子 (与 migrate_causal_chains.py 协同):
  - BTC::fed_liquidity_tightening  → DOWN, -2 ~ -8,  关键词 fed/liquidity/hawkish/dotplot
  - BTC::regulatory_crackdown      → DOWN, -2 ~ -10, 关键词 sec/regulation/ban/crackdown
  - SKHYNIX::ai_capex_upcycle      → UP,   +3 ~ +12, 关键词 hbm/ai_capex/hyperscaler/dram
  - WTI::opec_supply_cut           → UP,   +2 ~ +10, 关键词 opec/quota/cut/supply
  - XAU::real_yields_inverse       → UP,   +1 ~ +6,  关键词 real_yield/tips/10y/yield

幂等: 已存在的字段、已回填的种子重复执行都安全.
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 4 个新字段定义 (每行: name, type, default)
NEW_FIELDS = [
    ("expected_direction", "TEXT", "'BOTH'"),
    ("magnitude_lo",       "REAL", "0.0"),
    ("magnitude_hi",       "REAL", "999.0"),
    ("trigger_keywords",   "TEXT", "''"),
]

# 5 条种子的新字段值
SEED_OVERRIDES = {
    "BTC::fed_liquidity_tightening": {
        "expected_direction": "DOWN", "magnitude_lo": 2.0, "magnitude_hi": 8.0,
        # 前置: btc/bitcoin 别名 (确保 asset-pinned 命中); 触发: fed/fomc 等政策语义
        "trigger_keywords": "btc,bitcoin,fed,fomc,dotplot,hawkish,rate_hike,liquidity,加息,降息,通胀,流动性",
    },
    "BTC::regulatory_crackdown": {
        "expected_direction": "DOWN", "magnitude_lo": 2.0, "magnitude_hi": 10.0,
        "trigger_keywords": "btc,bitcoin,sec,regulation,ban,crackdown,enforcement,lawsuit,compliance,监管,诉讼,合规,执法",
    },
    "SKHYNIX::ai_capex_upcycle": {
        "expected_direction": "UP", "magnitude_lo": 3.0, "magnitude_hi": 12.0,
        "trigger_keywords": "skhynix,hynix,000660,海力士,sk_hynix,hbm,ai_capex,hyperscaler,dram,memory,datacenter,blackwell,h100,半导体,存储,内存",
    },
    "WTI::opec_supply_cut": {
        "expected_direction": "UP", "magnitude_lo": 2.0, "magnitude_hi": 10.0,
        "trigger_keywords": "wti,oil,crude,原油,opec,quota,supply,production,barrel,saudi,减产,增产,欧佩克,油价",
    },
    "XAU::real_yields_inverse": {
        "expected_direction": "UP", "magnitude_lo": 1.0, "magnitude_hi": 6.0,
        "trigger_keywords": "xau,gold,黄金,gc1,real_yield,tips,10y,yield,dovish,inflation,geopolitical,降息,避险,国债",
    },
}

# v292+ altcoin 链路 (让 519 条 altcoin reflections 有匹配目标)
EXTRA_CHAINS = [
    {
        "chain_id": "ADA::whale_accumulation",
        "asset": "ADA",
        "trigger_pattern": "whale_accumulation",
        "expected_direction": "UP",
        "magnitude_lo": 5.0, "magnitude_hi": 18.0,
        "trigger_keywords": "ada,cardano,艾达,whale,accumulation,onchain,链上,巨鲸,大额,holder,delegation,stake",
        "confidence": 0.55,
        "source": "rule_mined",
    },
    {
        "chain_id": "SOL::narrative_revival",
        "asset": "SOL",
        "trigger_pattern": "narrative_revival",
        "expected_direction": "UP",
        "magnitude_lo": 5.0, "magnitude_hi": 15.0,
        "trigger_keywords": "sol,solana,narrative,memecoin,defi,nft,onchain,链上,生态,validator,staking",
        "confidence": 0.5,
        "source": "rule_mined",
    },
    {
        "chain_id": "MU::hbm_demand_pull",
        "asset": "MU",
        "trigger_pattern": "hbm_demand_pull",
        "expected_direction": "UP",
        "magnitude_lo": 3.0, "magnitude_hi": 12.0,
        "trigger_keywords": "mu,micron,美光,hbm,dram,memory,datacenter,ai,半导体,存储,高带宽,hyperscaler",
        "confidence": 0.62,
        "source": "rule_mined",
    },
]


def has_column(conn, table: str, col: str) -> bool:
    """检查列是否已存在 (幂等性核心)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def add_column_safe(conn, table: str, col: str, ctype: str, default_sql: str) -> bool:
    if has_column(conn, table, col):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype} DEFAULT {default_sql}")
    return True


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        added = 0
        for col, ctype, default in NEW_FIELDS:
            if add_column_safe(conn, "causal_chains", col, ctype, default):
                added += 1
                print(f"[OK] ADD COLUMN causal_chains.{col} {ctype} DEFAULT {default}")
        print(f"[OK] Columns: added={added} (剩余 {(len(NEW_FIELDS) - added)} 已存在)")

        # 回填种子 (v292+ 用更宽的关键词, 强制覆盖以启用更宽松的语义匹配)
        updated = 0
        for chain_id, v in SEED_OVERRIDES.items():
            cur = conn.execute(
                "SELECT expected_direction, magnitude_lo, magnitude_hi, trigger_keywords "
                "FROM causal_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if not cur:
                print(f"[WARN] seed {chain_id} 不存在, 跳过 (请先跑 migrate_causal_chains.py)")
                continue
            # v292+ 强制覆盖: 旧关键词 (fed,liquidity,...) 太窄, 改成 (fed,fomc,降息,通胀,...) 等宽语义
            conn.execute(
                "UPDATE causal_chains SET expected_direction=?, magnitude_lo=?, magnitude_hi=?, "
                "trigger_keywords=?, updated_at=strftime('%s','now') WHERE chain_id=?",
                (v["expected_direction"], v["magnitude_lo"], v["magnitude_hi"],
                 v["trigger_keywords"], chain_id),
            )
            updated += 1
            print(f"[OK] widened {chain_id}: {v['expected_direction']} "
                  f"{v['magnitude_lo']}~{v['magnitude_hi']}% "
                  f"kws={v['trigger_keywords'][:50]}…")

        # v292+ altcoin 链路 (用 INSERT OR IGNORE 幂等)
        inserted = 0
        for c in EXTRA_CHAINS:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO causal_chains "
                    "(chain_id,asset,trigger_pattern,chain_steps,confidence,version,source,active,"
                    " expected_direction,magnitude_lo,magnitude_hi,trigger_keywords) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["chain_id"], c["asset"], c["trigger_pattern"], "[]",
                     c["confidence"], 1, c["source"], 1,
                     c["expected_direction"], c["magnitude_lo"], c["magnitude_hi"],
                     c["trigger_keywords"]),
                )
                # 检查是否真插入
                cur = conn.execute(
                    "SELECT 1 FROM causal_chains WHERE chain_id=?", (c["chain_id"],)
                ).fetchone()
                existing = conn.execute(
                    "SELECT hit_count FROM causal_chains WHERE chain_id=?", (c["chain_id"],)
                ).fetchone()
                if existing and existing[0] == 0:
                    # 新插入, hit/miss 必然 0
                    pass
                inserted += 1
            except Exception as e:
                print(f"[WARN] {c['chain_id']} insert 失败: {e}")
        print(f"[OK] altcoin 链路: {len(EXTRA_CHAINS)} 条已写入 (insert or ignore)")

        conn.commit()

        # 最终报告
        print()
        print("=== causal_chains 当前状态 (v292+ schema) ===")
        for r in conn.execute(
            "SELECT chain_id, asset, expected_direction, magnitude_lo, magnitude_hi, "
            "hit_count, miss_count, substr(trigger_keywords,1,30) FROM causal_chains ORDER BY asset"
        ):
            print(f"  {r[0]:35s} {r[1]:8s} dir={r[2]:4s} mag={r[3]:.1f}~{r[4]:.1f}  "
                  f"hit={r[5]} miss={r[6]}  kws='{r[7]}'")
        print(f"\n[OK] updated={updated} (剩余已正确), all idempotent.")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
