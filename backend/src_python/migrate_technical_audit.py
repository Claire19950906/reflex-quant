"""把"技术审计"行从 trade_reflections_v2 迁到独立表 technical_audit（v412-G）。

## 为什么要迁

`c_technical_reflections.py` 把两类**流程指标**写进了交易反思表：

  · 决策延迟：`新闻 X 入库，决策 Y 才出，延迟 53 分钟；延迟期间 WTI 实际 +0.00%`
  · 行情断流：`行情断流 Nh（… → …）：…`

它们没有 decision_id、没有 paper_run_id、pnl 为空、entry 为 0，本质是"链路体检记录"，
不是"这笔交易判断错在哪"。但混在交易反思表里会连锁污染：

  | 现象 | 真因 |
  |---|---|
  | C 类占 81% | 这些行全标 dimension='C' |
  | hit 644 条全 0 | 没有成交，hit 只能是 0 |
  | 3layer 判出 663 条"技术错误" | pnl 为 NULL → 恒判技术错误 |
  | 富化 525 条 pending 卡死 | 富化只处理有 decision_id 的行 |
  | 单条反思 95% 不到 80 字 | 短文本就是这类模板句 |

## 判据（只迁这一类，不误伤真反思）

    COALESCE(decision_id,'') = ''      -- 没有对应决策
AND COALESCE(paper_run_id,'') = ''     -- 不是 paper 反思行
AND dimension = 'C'                    -- 技术维度

真交易反思（paper 反思行 + 结算反思行）都有 decision_id，不会被选中。

## 安全

1. 先 `CREATE TABLE trade_reflections_v2_bak_<日期> AS SELECT *` 整表备份；
2. 迁移与删除在**同一个事务**里，失败整体回滚；
3. 幂等：重复跑时源表已无匹配行，迁 0 条。

用法：
    python3 migrate_technical_audit.py --dry-run
    python3 migrate_technical_audit.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "trident_event_bus.db"

PICK = """COALESCE(decision_id,'') = ''
      AND COALESCE(paper_run_id,'') = ''
      AND COALESCE(dimension,'') = 'C'"""

DDL = """
CREATE TABLE IF NOT EXISTS technical_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT, action TEXT, dimension TEXT, subcategory TEXT,
    audit_text TEXT, created_at TEXT, hit INTEGER, actual_outcome_pct REAL,
    ai_decision_id TEXT, layer TEXT, sub_layer TEXT, signal_name TEXT,
    ontology_signal TEXT,
    migrated_from_id INTEGER, migrated_at TEXT
)
"""

COLS = ("asset, action, dimension, subcategory, reflection_text, created_at, hit, "
        "actual_outcome_pct, ai_decision_id, layer, sub_layer, signal_name, ontology_signal")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB), timeout=90)
    try:
        conn.execute("PRAGMA busy_timeout=90000")
        n = conn.execute(
            f"SELECT COUNT(*) FROM trade_reflections_v2 WHERE {PICK}").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0]
        print(f"待迁 {n} 条 / 表内共 {total} 条 → 迁完应剩 {total - n} 条真交易反思")

        if args.dry_run:
            print("样本（前 2 条）：")
            for row in conn.execute(
                    f"SELECT id, asset, subcategory, substr(reflection_text,1,60) "
                    f"FROM trade_reflections_v2 WHERE {PICK} LIMIT 2"):
                print("  ", row)
            return 0
        if not n:
            print("没有需要迁移的行（幂等）")
            return 0

        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"trade_reflections_v2_bak_{tag}"
        for attempt in range(3):
            try:
                with conn:                       # 单事务：备份+建表+迁移+删除
                    conn.execute(f"CREATE TABLE {bak} AS SELECT * FROM trade_reflections_v2")
                    conn.execute(DDL)
                    conn.execute(
                        f"""INSERT INTO technical_audit
                            (asset, action, dimension, subcategory, audit_text,
                             created_at, hit, actual_outcome_pct, ai_decision_id,
                             layer, sub_layer, signal_name, ontology_signal,
                             migrated_from_id, migrated_at)
                            SELECT {COLS}, id, ?
                              FROM trade_reflections_v2 WHERE {PICK}""",
                        (datetime.now().isoformat(timespec="seconds"),))
                    conn.execute(f"DELETE FROM trade_reflections_v2 WHERE {PICK}")
                print(f"已迁移 {n} 条 → technical_audit")
                print(f"备份表：{bak}")
                break
            except sqlite3.OperationalError as e:
                if attempt == 2:
                    raise
                print(f"[MIGRATE] 锁冲突重试 {attempt + 1}/3: {e}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))

        left = conn.execute("SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0]
        dist = conn.execute(
            "SELECT COALESCE(dimension,'NULL'), COUNT(*) FROM trade_reflections_v2 "
            "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        print(f"迁移后 trade_reflections_v2 = {left} 条，维度分布: {dict(dist)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
