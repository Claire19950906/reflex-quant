# Schema 迁移规范（P2-1，2026-11-19）

## 现状

- 既有 `db.migrate(conn)`：`CREATE TABLE IF NOT EXISTS` + `_ensure_columns`，幂等，每次启动跑。
- 历史版本标记：SQLite `PRAGMA user_version`（基线 23，见 `db.P2_USER_VERSION`）。
- 新增轻量注册表：`src_python/schema_migrations.py`，补"按版本号顺序执行、失败不抬版本、可重跑"的增量迁移骨架。

## 什么时候用哪一种

| 场景 | 做法 |
|---|---|
| 新表 / 补默认列、不涉及数据迁移 | 继续在 `db.migrate()` 里加 `CREATE TABLE IF NOT EXISTS` / `_ensure_columns`（幂等） |
| 结构性变更（删列、改类型、回填、拆表） | 在 `schema_migrations.MIGRATIONS` 登记一条版本化迁移 |
| 一次性数据清洗 / 假数据剔除 | 放 `scripts/oneoff/`，不进迁移注册表 |

## 新增一条版本化迁移

1. 打开 `src_python/schema_migrations.py`；
2. 在 `MIGRATIONS` 追加（版本号严格递增、不复用）：

```python
Migration(
    version=24,
    description="给 xxx 表补 yyy 列",
    up=lambda conn: _ensure_column(conn, "xxx", "yyy", "TEXT DEFAULT ''"),
),
```

3. 要求：
   - **幂等**：可重复执行不出错；先查表/列是否存在再 ALTER/CREATE；
   - 只改 schema，不写业务数据；
   - 函数内不发网络请求、不调 LLM；
4. 跑测试：`python3 -m pytest tests/test_schema_migrations.py -q`；
5. 在迁移函数旁写清"为什么这么改"，避免后人重复加列。

## 执行与校验

- dry-run（只看待执行清单，不改库）：

```python
import schema_migrations as sm
sm.run_pending(conn, real_run=False)
```

- 真实执行：`sm.run_pending(conn)`；每条成功后独立 commit 并抬升 `user_version`，失败回滚、不抬版本，下次启动重跑。

## 禁止事项

- 不要手工改生产库 `user_version` 跳过迁移；
- 迁移函数不要 `DROP TABLE` 不带备份导出；
- 不要在迁移里读 `config` 之外的外部状态；
- 版本号只能递增，合并冲突时取较大值并补齐缺口。
