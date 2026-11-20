# 拆 `api_server.py`：实测数据与切片顺序

现状（2026-11-20 实测）：

- **9,292 行**，**185 个顶层函数**（切片 ② 已做，见下）；
- 直接挂在 `@app` 上的路由只有 **4 个**——422 个端点已经在 `routers/` 的 68 个文件里；
- 所以它不是"路由巨石"，而是**启动装配 + 同步查询 helper + 少量重端点**混在一个文件里。

这也解释了新人为什么卡住：他按"入口文件"去读，读到的是 lifespan 和一堆 `_xxx_sync`，而不是业务。

---

## 四个切片（按风险从低到高）

| 切片 | 内容 | 行数 | 风险 | 验证方式 |
|---|---|---|---|---|
| ① 导出 | `_do_export_excel` | 164 | 低 | 调一次 `/api/export/excel` 比对文件大小与表头 |
| ② 格式化 | `_row_to_event` + `_safe` + `_strip_hold_override` | 225 | 低 | ✅ **已完成**，见下 |
| ③ 同步读 | `_movers_sync` 213 / `_history_sync` 142 / `_ingest_external_news_sync` 122 / `_abnormal_signature_sync` 107 / `_writeback_swing_to_decisions` 103 | 687 | 中 | 对应端点响应 + `offline_selftest` |
| ④ 启动装配 | `lifespan` 201 / `_boot_subsystems` 211 / `_boot_reflection_workers` 231 | 643 | 高 | 起服务看 boot 日志顺序与子进程是否全起 |

合计可移出 **约 1,660 行**，`api_server.py` 降到 7.8k 行左右。想再降，就得动业务逻辑本身，那是另一件事。

## 三条纪律（避免拆完 API 变了样）

1. **只移不改**：函数体一个字都不动，只换 import 路径。要改逻辑就单独一个提交。
2. **一次一个切片**：移完立刻跑 `offline_selftest.py` + 该切片对应的端点，绿了再动下一块。
3. **端点基线不许漂**：`scripts/architecture_baseline.py` 的端点数与路径是护栏，拆完必须一字节不差。

## 切片 ② 已完成（2026-11-20）

格式化这一层（`_safe` / `_normalize_analysis_status` / `_parse_peer_reviews` /
`_strip_hold_override` / `gate_reason_fallback` / `_row_to_event` + `_HOLD_OVERRIDE_RE`）
整体移到 `backend/src_python/api_format.py`，`api_server.py` 只留一行 import。

- **只移不改**：函数体一字未动，连注释都原样带过去；
- 行数：`api_server.py` 9,514 → **9,292**（−222），新模块 251 行；
- 新增 `backend/tests/test_api_format.py`：**17 条用例，0.85 秒跑完**，覆盖
  "同一行数据在 8000 与 8100 两个端口显示不同状态"这类真出过事故的分支；
- 回归：`pytest -k "schema or contract or registry or gate or event"`
  → **862 passed, 2 skipped, 0 failed**（5 分半）。

为什么先拆这一块：它是纯函数、不碰数据库、不碰网络，风险最低，却能顺手把
"事件展示口径"这块最难测的逻辑变成可断言的东西。

## 剩下三块为什么还没动

③④ 会移动启动装配与同步查询，公开仓库里跑不起来的代价比"文件长"大得多。
要做就在有全套用例的环境（本机 `金融1.0/`，3,500 条）里做，做完同步回来。

## 拆完能拿到什么

- 新人的第一步从"读 9.5k 行"变成"读 `boot/` 与 `api/reads/` 两个目录"；
- 覆盖率棘轮（当前 20%）会随 import 面自然抬升，这是唯一的顺带收益；
- 结构化评分里「结构可读」从 8 往上走，但**不会因为"拆了"就自动满分**——真正的分数在测试与文档。

配套：端点全景见 [`API-MAP.md`](./API-MAP.md)（由 `backend/scripts/gen_api_map.py` 生成，会随源码更新）。
