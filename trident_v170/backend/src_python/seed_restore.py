# -*- coding: utf-8 -*-
"""配置种子库自动入库（幂等）—— 免去部署时"忘记跑 restore_seed.py"。

## 为什么需要

提示库里的品种专项规则 / 复盘经验 / 因果链 / 过滤词都只存在 SQLite，而部署包
按安全约定不含生产 `*.db`（有交易数据）。于是包里另带一份只含配置表的
`backend/seed_config.db`，部署后要用 `deploy/restore_seed.py` 导入。
实测这一步极易遗漏——忘了跑，新环境的提示库就是空的，而且症状很隐蔽
（界面能开、分析能跑，只是没有规则与经验在起作用）。

## 触发规则（安全第一）

**只在"这个库第一次见到种子"时导入一次**，之后永不重复：

  · 判定用一次性标记文件 `<库名>.seed_imported`（与库同目录，跟库一起走）。
    库被删掉重建 → 标记也没了 → 会重新导入，符合直觉。
  · 没有标记 → 导入并把标记写上；有标记 → 直接返回，一个字节都不写。
  · 为什么需要标记：服务会被反复重启（`init_db.py` 每次都在启动脚本的 [PREP]
    里跑）。若每次都"补齐缺失行"，你在界面上**删掉的规则/经验会在下次重启时
    复活** —— 实测踩过。有了标记，删除就是删除。
  · 强制重导：`force=True` / `RESTORE_SEED=1` / CLI `--force`，会重写标记。

环境变量：
  · ``RESTORE_SEED=1``  → 强制全量 upsert 导入（覆盖同主键行）
  · ``SKIP_SEED_RESTORE=1`` → 完全跳过本次调用（不影响标记）
  · ``TRIDENT_NO_AUTO_SETUP=1`` → 服务进程用：跳过自动动作（此开关 + 经验自愈）

调用点：`backend/init_db.py`（部署第一步）、`engine.py` / `api_server.py`
启动（兜底；启动脚本对服务进程置 ``TRIDENT_NO_AUTO_SETUP=1``）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, List, Optional

# 随包携带的配置类表（与 deploy/restore_seed.py 保持一致）
SEED_TABLES = (
    "ai_asset_rules",
    "review_insights",
    "causal_chains",
    "filter_keywords",
    "ai_prompts",
    "ai_main_prompts",
)


def _seed_path() -> str:
    """种子库路径：backend/seed_config.db（相对本文件上两级）。"""
    here = os.path.dirname(os.path.abspath(__file__))          # src_python
    backend = os.path.dirname(here)                            # backend
    return os.path.join(backend, "seed_config.db")


def marker_path(db_path: Optional[str] = None) -> str:
    """一次性标记文件的路径：与库同目录，`<库名>.seed_imported`。"""
    return str(_db_file(db_path)) + ".seed_imported"


def insight_marker_path(db_path: Optional[str] = None) -> str:
    """经验自愈的一次性标记：`<库名>.insight_rescued`。

    与种子标记分开：种子导入成功、经验自愈完成是两件事，各自只做一次。
    """
    return str(_db_file(db_path)) + ".insight_rescued"


def _db_file(db_path: Optional[str] = None) -> str:
    if not db_path:
        try:
            import config as _cfg
            db_path = _cfg.DB_PATH
        except Exception:
            db_path = ""
    return str(db_path)


def _import_done(db_path: Optional[str] = None) -> bool:
    try:
        return os.path.exists(marker_path(db_path))
    except Exception:
        return False


def _mark_imported(db_path: Optional[str] = None, detail: str = "") -> None:
    try:
        with open(marker_path(db_path), "w", encoding="utf-8") as f:
            f.write(f"seed_config.db imported at {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    f"{(' | ' + detail) if detail else ''}\n")
    except Exception:
        pass


def reset_marker(db_path: Optional[str] = None) -> None:
    """删掉一次性标记，让下次启动重新导入（CLI --force 用）。"""
    try:
        os.remove(marker_path(db_path))
    except OSError:
        pass


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    # 注意：不要依赖调用方已设 row_factory —— 用 rowcount/长度无关的写法
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone())
    except sqlite3.Error:
        return False


def _count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def _missing_rows(seed: sqlite3.Connection, conn: sqlite3.Connection,
                  table: str) -> List[sqlite3.Row]:
    """返回"目标库里还不存在"的种子行。

    逐行比较【全部列的值】（不用 rowid——`ai_main_prompts` 这类表 rowid 不稳定）。
    这样既能补齐缺失行，又不会覆盖目标机上已有（可能被用户改过）的行。
    """
    try:
        seed_rows = list(seed.execute(f"SELECT * FROM {table}"))
    except sqlite3.Error:
        return []
    if not seed_rows:
        return []
    cols = list(seed_rows[0].keys())
    try:
        existing = {
            tuple(r[c] for c in cols)
            for r in conn.execute(f"SELECT * FROM {table}")
        }
    except sqlite3.Error:
        return []
    return [r for r in seed_rows if tuple(r[c] for c in cols) not in existing]


def auto_import_from_seed(conn: sqlite3.Connection,
                          force: Optional[bool] = None,
                          quiet: bool = False) -> Dict[str, int]:
    """按需把种子库导入当前库。返回 {表名: 导入行数}（空 dict = 无需导入）。

    默认**每个库只导入一次**（靠 `<库名>.seed_imported` 标记）；force=True 时
    忽略标记并全量 INSERT OR REPLACE（覆盖同主键行）。
    任何异常都被吞掉并返回已导入的部分——种子导入失败绝不该阻止引擎启动。

    为什么用"一次性标记"而不是"逐行补齐缺失项"：服务会被反复重启，每次启动都补齐
    的话，**你在界面上删掉的规则/经验会在下次重启时复活**（实测踩过）。标记写好之后
    启动路径一个字节都不写；要重新灌种子请显式 `--force` / `RESTORE_SEED=1`。

    自己先跑一次 `db.migrate()`：不依赖"调用方已经建过表"这种顺序假设
    （实测踩过：seed 导入若跑在 migrate 之前，目标表还不存在 → 静默跳过、
    一个字段都不导入，且看起来像是"种子库是空的"）。
    """
    imported: Dict[str, int] = {}
    try:
        if os.environ.get("TRIDENT_NO_AUTO_SETUP") or os.environ.get("SKIP_SEED_RESTORE"):
            return imported
        if force is None:
            force = bool(os.environ.get("RESTORE_SEED"))

        seed_file = _seed_path()
        if not os.path.exists(seed_file):
            return imported   # 不是部署包（比如源码仓库里删了种子库）→ 正常跳过

        try:
            import config as _cfg
            _db_path = _cfg.DB_PATH
        except Exception:
            _db_path = None

        # 一次性闸门：这个库已经导入过 → 直接返回（删除的东西不会被"复活"）
        if not force and _import_done(_db_path):
            return imported

        try:
            import db as _db
            _db.migrate(conn)   # 幂等：确保配置表存在，再谈补齐
        except Exception:
            pass

        # 目标连接也必须按列名访问 —— 实测踩过：迁移内部会设置 row_factory，
        # 但快路径下可能不设；此时 fetchone() 返回 tuple，按列名取会抛
        # TypeError 让整个导入静默失败（日志只有一行"自动导入跳过"）。
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass

        seed = sqlite3.connect(seed_file)
        seed.row_factory = sqlite3.Row
        try:
            for table in SEED_TABLES:
                if not _table_exists(seed, table) or not _table_exists(conn, table):
                    continue   # 种子没有该表 / 目标表还没建（migrate 未跑）→ 不擅自建表
                if (_seed_n := _count(seed, table) or 0) <= 0:
                    continue

                if force:
                    rows = list(seed.execute(f"SELECT * FROM {table}"))
                else:
                    # 逐行补齐：目标已有全量 → 空列表（零写入、零副作用）
                    rows = _missing_rows(seed, conn, table)
                if not rows:
                    continue

                cols = list(rows[0].keys())
                col_names = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join("?" * len(cols))
                conn.executemany(
                    f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
                imported[table] = len(rows)
            conn.commit()
        finally:
            seed.close()

        # 导入成功 → 写一次性标记（此后启动路径不再写任何东西，
        # 所以在界面上删掉的规则/经验不会被重启"复活"）
        if imported or force:
            _mark_imported(_db_path, "、".join(f"{t} {n}" for t, n in imported.items()))
    except Exception as exc:  # noqa: BLE001 —— 种子导入失败绝不阻断启动
        if not quiet:
            print(f"[SEED] 自动导入跳过（{type(exc).__name__}: {exc}）", flush=True)
        return imported

    if imported and not quiet:
        detail = "、".join(f"{t} {n} 条" for t, n in imported.items())
        print(f"[SEED] 已从 seed_config.db 导入配置（一次性）：{detail}", flush=True)
        print("[SEED] 已写标记 %s —— 以后重启不再改动提示库，删除即保持删除；"
              "要重新灌种子请用 --force / RESTORE_SEED=1" % marker_path(_db_path),
              flush=True)
    return imported
