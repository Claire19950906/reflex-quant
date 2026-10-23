"""
migrate_causal_chains_strategy_columns.py
=========================================
v300 修复: 为 causal_chains 表补齐"策略链路"schema 列, 修复 bridge_server 500。

背景
----
`causal_chains` 表名被两套 schema 共用, 造成生产库与代码期望不一致:

  A) 知识库版 (db.py 的 _CREATE_CAUSAL_CHAINS, 14 列) —— 生产库实际使用
     id / name / cause / effect / trigger_words / note / enabled /
     origin / weight / mechanism / precondition / invalidation / created_at / updated_at
     语义: "美元加息 → 黄金承压" 这类自然语言因果知识, 供新闻分析注入。

  B) 策略链路版 (migrate_causal_chains.py, 22 列) —— 旧工作区快照使用
     chain_id / asset / trigger_pattern / chain_steps / confidence /
     version / hit_count / miss_count / source / active /
     last_verified_ts / expected_direction / magnitude_lo / magnitude_hi /
     trigger_keywords
     语义: 可回测的资产传导链路 (BTC::fed_liquidity_tightening), 带 hit/miss 计数。

生产库只跑过 A 的建表, 从未跑过 B (所有生产备份均为 14 列)。
而 bridge_server.py 有 7 处按 B 查询 (含 `active=1`), 导致以下端点稳定 500:
    GET  /api/v291/stats
    GET  /api/v291/cross-asset-chains
    POST /news/impact-evaluate
    GET  /events/filter-stream
    POST /api/v291/causal-chain/score-batch

本脚本为生产库补齐 B 所需的 15 列 (全部可空/有默认值, 不破坏 A 的既有语义),
并把 A 的 enabled/origin 映射到 B 的 active/source, 使两套 schema 共存。

安全设计
--------
- 纯 ADD COLUMN, 不删列、不改列类型、不动既有 292 行数据。
- 幂等: 重复执行安全 (已存在的列跳过)。
- 默认 dry-run, 必须显式 --apply 才写库。
- 写库前自动备份 (SQLite 在线备份 API, 正确处理 WAL)。
- chain_id 由 id 派生 ('CC_'||id), 避免与 outcomes.chain_id 冲突。
- asset 置为 'UNKNOWN' —— 知识库条目没有资产维度, 不臆造。
- trigger_pattern / chain_steps 置空 —— 知识库条目没有多步链路, 不臆造。

用法
----
    python3 migrate_causal_chains_strategy_columns.py            # dry-run, 只报告
    python3 migrate_causal_chains_strategy_columns.py --apply     # 实际执行
    python3 migrate_causal_chains_strategy_columns.py --apply --db-path /path/to.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BACKEND_DIR / "trident_event_bus.db"

# (列名, 类型定义) —— 顺序即添加顺序
STRATEGY_COLUMNS: list[tuple[str, str]] = [
    ("chain_id",          "TEXT"),
    ("asset",             "TEXT"),
    ("trigger_pattern",   "TEXT"),
    ("chain_steps",       "TEXT DEFAULT '[]'"),
    ("confidence",        "REAL DEFAULT 0.5"),
    ("version",           "INTEGER DEFAULT 1"),
    ("hit_count",         "INTEGER DEFAULT 0"),
    ("miss_count",        "INTEGER DEFAULT 0"),
    ("source",            "TEXT DEFAULT 'rule_mined'"),
    ("active",            "INTEGER DEFAULT 1"),
    ("last_verified_ts",  "INTEGER"),
    ("expected_direction", "TEXT DEFAULT 'BOTH'"),
    ("magnitude_lo",      "REAL DEFAULT 0.0"),
    ("magnitude_hi",      "REAL DEFAULT 999.0"),
    ("trigger_keywords",  "TEXT DEFAULT ''"),
]

# 代码 (bridge_server.py) 实际查询的列 —— 用于最终自检
CODE_REQUIRED = [
    "chain_id", "asset", "trigger_pattern", "chain_steps", "confidence",
    "hit_count", "miss_count", "version", "expected_direction",
    "magnitude_lo", "magnitude_hi", "trigger_keywords", "last_verified_ts",
]


def _existing_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def analyze(db_path: Path) -> int:
    """只读体检, 返回缺失列数。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not _table_exists(conn, "causal_chains"):
            print(f"[ERROR] 表 causal_chains 不存在于 {db_path}")
            return 99

        cols = _existing_cols(conn, "causal_chains")
        missing = [(n, t) for n, t in STRATEGY_COLUMNS if n not in cols]
        n_rows = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]

        print(f"=== causal_chains 体检: {db_path}")
        print(f"  现有列数: {len(cols)}   行数: {n_rows}")
        print(f"  已有 enabled (知识库版): {'enabled' in cols}")
        print(f"  已有 active  (策略版):   {'active' in cols}")

        if not missing:
            print("  [OK] 策略版列已齐全, 无需迁移")
            return 0

        print(f"\n  待添加 {len(missing)} 列:")
        for n, t in missing:
            print(f"    + {n} {t}")
        print("\n  注: 本脚本不修改代码, 仅让库同时满足两套 schema 的列需求。")
        return len(missing)
    finally:
        conn.close()


def backup(db_path: Path) -> Path:
    """SQLite 在线备份 (一致快照, 正确处理 WAL)。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(f"{db_path.name}.bak_ccstrategy_{stamp}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst

    "chain_id", "asset", "trigger_pattern", "chain_steps", "confidence",
    "hit_count", "miss_count", "version", "expected_direction",
    "magnitude_lo", "magnitude_hi", "trigger_keywords", "last_verified_ts",


def apply_migration(db_path: Path, do_backup: bool = True) -> int:
    if not db_path.exists():
        print(f"[ERROR] 数据库不存在: {db_path}")
        return 2

    if do_backup:
        bk = backup(db_path)
        print(f"[OK] 已备份: {bk}  ({bk.stat().st_size / 1024 / 1024:.1f}MB)")

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cols = _existing_cols(conn, "causal_chains")
        before_rows = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
        before_outcomes = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes"
        ).fetchone()[0]

        added = 0
        for name, typ in STRATEGY_COLUMNS:
            if name in cols:
                print(f"  [SKIP] {name} 已存在")
                continue
            conn.execute(f'ALTER TABLE causal_chains ADD COLUMN "{name}" {typ}')
            added += 1
            print(f"  [OK] ADD COLUMN {name} {typ}")

        # 映射: enabled -> active, origin -> source
        # chain_id 由 id 派生, 避免与 outcomes.chain_id 冲突
        conn.execute(
            "UPDATE causal_chains SET "
            "  active = COALESCE(enabled, 1), "
            "  source = COALESCE(origin, 'manual'), "
            "  chain_id = 'CC_' || id, "
            "  asset = COALESCE(asset, 'UNKNOWN'), "
            "  trigger_pattern = COALESCE(trigger_pattern, 'unknown'), "
            "  chain_steps = COALESCE(chain_steps, '[]') "
            "WHERE chain_id IS NULL OR chain_id = ''"
        )
        conn.commit()

        print("\n=== 自检: bridge_server 实际查询 ===")
        ok = True
        checks = [
            ("/api/v291/stats",
             "SELECT COUNT(*) FROM causal_chains WHERE active=1"),
            ("/news/impact-evaluate",
             "SELECT chain_id, asset, trigger_pattern, chain_steps, confidence, "
             "hit_count, miss_count, version FROM causal_chains WHERE active=1"),
            ("/events/filter-stream",
             "SELECT chain_id, asset, expected_direction, magnitude_lo, magnitude_hi, "
             "trigger_keywords FROM causal_chains WHERE active=1 AND asset=?"),
            ("/causal-chain/score-batch",
             "SELECT chain_id, asset, hit_count, miss_count, last_verified_ts "
             "FROM causal_chains WHERE active=1 ORDER BY asset ASC"),
        ]
        for label, sql in checks:
            try:
                args = ("XAU",) if "?" in sql else ()
                rows = conn.execute(sql, args).fetchall()
                print(f"  [OK] {label}: {len(rows)} 行")
            except Exception as e:
                ok = False
                print(f"  [FAIL] {label}: {e}")

        after_rows = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
        after_outcomes = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes"
        ).fetchone()[0]
        n_enabled = conn.execute(
            "SELECT COUNT(*) FROM causal_chains WHERE enabled=1").fetchone()[0]
        n_active = conn.execute(
            "SELECT COUNT(*) FROM causal_chains WHERE active=1").fetchone()[0]
        n_null_cc = conn.execute(
            "SELECT COUNT(*) FROM causal_chains WHERE chain_id IS NULL "
            "OR chain_id=''").fetchone()[0]

        print("\n=== 完整性自检 ===")
        print(f"  新增列数: {added}")
        print(f"  causal_chains 行数: {before_rows} -> {after_rows} (应不变)")
        print(f"  causal_chain_outcomes 行数: {before_outcomes} -> {after_outcomes} (应不变)")
        print(f"  enabled=1 行数: {n_enabled}")
        print(f"  active=1 行数:  {n_active}  (应等于 enabled)")
        print(f"  chain_id 为空的行数: {n_null_cc} (应为 0)")

        if before_rows != after_rows or before_outcomes != after_outcomes:
            ok = False
            print("  [FAIL] 行数发生变化!")
        if n_null_cc != 0:
            ok = False
            print("  [FAIL] 存在 chain_id 为空的行")
        if n_enabled != n_active:
            ok = False
            print("  [FAIL] enabled 与 active 计数不一致")

        for label, sql in [
            ("知识库查询 (kb_facade)",
             "SELECT id, name, cause, effect, weight FROM causal_chains "
             "WHERE enabled = 1 ORDER BY weight DESC LIMIT 5"),
            ("causal_graph",
             "SELECT DISTINCT cause, effect FROM causal_chains WHERE enabled=1"),
        ]:
            try:
                rows = conn.execute(sql).fetchall()
                print(f"  [OK] {label}: {len(rows)} 行")
            except Exception as e:
                ok = False
                print(f"  [FAIL] {label}: {e}")

        print("\n结论:", "全部通过 [OK]" if ok else "存在失败 [FAIL]")
        return 0 if ok else 1
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 causal_chains 策略链路列")
    ap.add_argument("--apply", action="store_true", help="实际写库 (默认 dry-run)")
    ap.add_argument("--db-path", default=str(DEFAULT_DB), help="数据库路径")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份 (不推荐)")
    args = ap.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    print(f"数据库: {db_path}\n")

    if not args.apply:
        print("=== DRY-RUN (只读体检, 未修改任何数据) ===\n")
        n = analyze(db_path)
        print(f"\n需迁移列数: {n}")
        print("确认无误后执行:  python3 "
              f"{Path(__file__).name} --apply --db-path {db_path}")
        return 0

    return apply_migration(db_path, do_backup=not args.no_backup)


if __name__ == "__main__":
    sys.exit(main())

