# -*- coding: utf-8 -*-
"""导出「配置类数据」为可移植种子库，供部署包携带。

背景：提示库里的可编辑内容（品种专项规则 / 复盘经验 / 因果链 / 过滤词 / 自定义 prompt）
全部存在 SQLite 里，而部署包按安全约定排除 *.db（生产库含真实交易数据与账户信息）。
结果：部署到 VPS 后提示库是全空的，测试期调好的规则带不过去。

本脚本只导出【配置类】表（不含交易/新闻数据），生成 backend/seed_config.db：
  ai_asset_rules  品种专项分析规则（含示例与 all 兜底）
  review_insights 复盘经验（含生效与停用，便于部署后自行启用）
  causal_chains   因果链库
  filter_keywords 过滤关键词（JUNK/CORE）
  ai_prompts      用户自定义分析指令
  ai_main_prompts 主 prompt 覆盖（页面上编辑过的提示词）

配合 deploy/restore_seed.py 在目标机导入（幂等：按主键 upsert，不删原有数据）。

用法：cd backend && python -X utf8 ../scripts/export_seed_config.py
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "src_python"))

import config

# 需要随包携带的配置类表（顺序固定，便于比对）
SEED_TABLES = (
    "ai_asset_rules",
    "review_insights",
    "causal_chains",
    "filter_keywords",
    "ai_prompts",
    "ai_main_prompts",
)

OUT = os.path.join(ROOT, "backend", "seed_config.db")


def main() -> int:
    src_path = config.DB_PATH
    if not os.path.exists(src_path):
        print("源库不存在: %s" % src_path)
        return 1

    if os.path.exists(OUT):
        os.remove(OUT)
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(OUT)
    total = 0
    print("从 %s 导出配置类数据 → %s" % (src_path, OUT))
    for table in SEED_TABLES:
        try:
            rows = list(src.execute("SELECT * FROM %s" % table))
        except sqlite3.OperationalError:
            print("  %-16s 源库无此表，跳过" % table)
            continue
        if not rows:
            print("  %-16s 0 条（跳过建表）" % table)
            continue
        cols = rows[0].keys()
        # 用源表 DDL 建表，保证列定义与索引一致
        ddl = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if ddl and ddl[0]:
            dst.execute(ddl[0])
        else:
            col_defs = ", ".join('"%s"' % c for c in cols)
            dst.execute("CREATE TABLE IF NOT EXISTS %s (%s)" % (table, col_defs))
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join('"%s"' % c for c in cols)
        dst.executemany(
            "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (table, col_names, placeholders),
            [tuple(r) for r in rows],
        )
        enabled = ""
        if "enabled" in cols:
            enabled = " 启用 %d" % sum(1 for r in rows if r["enabled"])
        if "active" in cols:
            enabled = " 生效 %d" % sum(1 for r in rows if r["active"])
        print("  %-16s %4d 条%s" % (table, len(rows), enabled))
        total += len(rows)
    dst.commit()
    dst.close()
    src.close()
    size_kb = os.path.getsize(OUT) / 1024
    print()
    print("完成：共 %d 条 → %s（%.0f KB，不含交易/新闻数据）" % (total, OUT, size_kb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
