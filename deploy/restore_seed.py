# -*- coding: utf-8 -*-
"""把随包携带的配置种子库导入当前实例（幂等）。

用途：部署包按安全约定不含 *.db（生产库有交易数据），但提示库里调好的
品种专项规则 / 复盘经验 / 因果链 / 过滤词需要带到新环境。本脚本读取
backend/seed_config.db 写入当前库，**不删除原有数据**。

自动化的说明：正常部署**不需要手动跑本脚本** —— `init_db.py`、引擎与 API
启动时都会调用 `seed_restore.auto_import_from_seed()`，且只在"配置表为空"
（即首次部署）时导入，已有数据的表不会被覆盖。本脚本用于：
  · 强制重新导入（覆盖本机改动）：python deploy/restore_seed.py --force
  · 先看清会导入什么：python deploy/restore_seed.py --dry-run

用法：
    cd backend && python -X utf8 ../deploy/restore_seed.py            # 按需导入
    cd backend && python -X utf8 ../deploy/restore_seed.py --dry-run  # 只看统计
    cd backend && python -X utf8 ../deploy/restore_seed.py --force    # 强制覆盖

    注意：手动模式下仍是"逐行补齐缺失项"，不会覆盖目标机已有（可能被改过的）行。
    dry-run 显示的是种子库总量，不代表需要补这么多。
  · --force 会把同主键的行覆盖为种子内容（想保留目标机改动就别加）。
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_PY = os.path.join(ROOT, "backend", "src_python")
sys.path.insert(0, SRC_PY)

import config
import db
import seed_restore

SEED = seed_restore._seed_path()
TABLES = seed_restore.SEED_TABLES


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    if not os.path.exists(SEED):
        print("找不到种子库: %s" % SEED)
        return 1

    print("种子库: %s" % SEED)
    marker = seed_restore.marker_path()
    if dry:
        print("（--dry-run：只统计，不写入）")
    elif force:
        print("（--force：忽略一次性标记，全量覆盖同主键行）")
    else:
        print("（按需模式：仅当该库还没导入过种子时才导入）")
    print("   一次性标记: %s %s" % (marker, "（已存在）" if os.path.exists(marker) else "（不存在）"))

    # 确保目标表存在（引擎/API 启动时也会 migrate，这里兜底）
    conn = sqlite3.connect(config.DB_PATH)
    try:
        db.migrate(conn)
    except Exception as exc:
        print("（migrate 跳过：%s）" % type(exc).__name__)
    conn.row_factory = sqlite3.Row

    if dry:
        seed = sqlite3.connect(SEED)
        seed.row_factory = sqlite3.Row
        total = 0
        for table in TABLES:
            try:
                rows = list(seed.execute("SELECT * FROM %s" % table))
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue
            print("  %-16s %4d 条" % (table, len(rows)))
            total += len(rows)
        seed.close()
        conn.close()
        print()
        print("种子里共 %d 条（--dry-run 未写入）" % total)
        if os.path.exists(marker):
            print("注意：本库已导入过种子（标记存在），不加 --force 时不会写入任何内容。")
        return 0

    imported = seed_restore.auto_import_from_seed(conn, force=force, quiet=True)
    if imported:
        for table, n in imported.items():
            print("  %-16s %4d 条" % (table, n))
    conn.close()
    print()
    if imported:
        print("已导入 %d 条配置数据（重启引擎/API 后生效）" % sum(imported.values()))
    elif os.path.exists(marker):
        print("无需导入：本库已导入过种子（标记存在）—— 删除的行不会自动复活。")
        print("想重新灌一遍种子请加 --force。")
    else:
        print("没有可导入的内容（种子库为空或表不匹配）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
