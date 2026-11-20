# 综合评分（自查，10 分制）

自查时间 2026-11-20。评分不是自我表扬，是找"下一个该修的地方"。每条都写了分数依据和缺口。

| # | 维度 | 分 | 依据 | 缺口 |
|---|---|---|---|---|
| 1 | 一眼看懂 | 8/10 | 首屏是"剔掉不该做的单"+ 三句话说明白 + 一张截图 | 缺一段 20 秒 GIF；首屏仍偏长 |
| 2 | 能跑起来 | 10/10 | 自带合成演示库（17 品种 / 6800 根日线 / 9 资产 / 25 事件类型）；本机实测 `/api/health` **200**、`/api/trident2/features` 返回 **64 项**；CI 里 `demo-smoke` 每次真起一次服务验证 | — |
| 3 | 结构可读 | 9/10 | 四层清晰；`trident2/catalog.py` 单一真值源；`API-MAP.md`（422 端点自动生成）；切片 ② 已落地：格式化层移入 `api_format.py`，`api_server.py` 9,514 → 9,292 行 | 还有 9.3k 行，③④ 两个切片要在全套测试环境做 |
| 4 | 测试与自检 | 9/10 | 全量 **4989 passed / 0 failed / 11 skipped**；全量覆盖率实测 **39%**（128,556 语句）；新增 `test_api_format.py` 17 条断言（0.85 秒），把"事件展示口径"从"没人敢改"变成"改错就红" | 39% 离 60% 还远，缺的主要是 `api_server.py` 与前端接口层 |
| 5 | CI | 9/10 | typecheck + 离线核心子集（覆盖率棘轮 20%，只升不降）+ ruff 错误级 + 端点基线 artifact + nightly 全量 | 棘轮目标 60% 尚未到位 |
| 6 | 文档 | 9/10 | README 中英双版 + 架构 + 快速上手 + FAQ + ROADMAP + 详细版 | 缺一页"数据源清单与延迟" |
| 7 | 安全与密钥卫生 | 10/10 | `.env` 从未入仓；公开版剔除数据库与运行时；`secret-scan.yml`（gitleaks）每次 push/PR 必扫 | — |
| 8 | 边界与诚实 | 10/10 | 0 结算写 0；胜率标 NULL；分歧大直接不给方向；事故写在文档里 | — |
| 9 | 可扩展 | 8/10 | 64 项能力走注册表；`/api/trident2` 可单跑；feeds 层加源有范式 | 无插件示例与第三方源接入文档 |
| 10 | 维护活性 | 8/10 | CHANGELOG 逐版可追；nightly 失败自动开 issue，不会静默烂掉 | 单人维护，Issue 响应无 SLA |

**总分 9.0 / 10。**

---

## 已经拿到的 9 分是怎么来的

1. **格式化层拆出去（+0.1）**：`_safe` / `_row_to_event` 等 6 个纯函数移进 `api_format.py`，配 17 条断言。巨石没消失，但入口不再是"9.5k 行全都要读"。
2. **CI 真的会报警（+0.1）**：夜间全量失败自动开 issue，同时修掉了 `ci.yml` 里重复的 `permissions` 与重复开 issue 步骤（重复键会让整个 workflow 无效）。

## 要到 10 分，还差三件

1. **覆盖率 39% → 60%（+0.6）**：门槛按「实测 − 2pp」走，只升不降。补测优先 `api_server.py` 与前端接口层。
2. **③④ 两个切片（+0.4）**：同步读 687 行、启动装配 643 行，风险中高，要在能跑全量用例的环境做。
3. **补一页数据源清单与延迟（+0.2）**：15 个品种各接哪几个源、滞后多少，现在散在 docs 里。

## 本轮验证（不是自评，是实测）

```
$ TRIDENT_DB_PATH=backend/demo.db python3 src_python/api_server.py
$ curl -w '%{http_code}' /api/health          → 200
$ curl /api/trident2/features | jq .summary   → total 64 / P0 11 / P1 36 / P2 17
```

这条链路已固化成 CI 的 `demo-smoke` job：起服务 → 等 health 200 → 断言 64 项。红一次就说明"陌生人跑不起来"。

```
$ pytest tests/ -q          → 4989 passed, 11 skipped（21 分 33 秒）
$ pytest tests/ --cov=src_python → 39%（128,556 语句，78,333 未覆盖）
```

全量跑完是判断"这份代码到底能不能自洽"的唯一办法：子集 20% 的覆盖率骗得过 CI，骗不过一次全量。

## 本轮实测（2026-11-20）

```
$ pytest tests/ -q -k "schema or contract or registry or gate or event"
  → 862 passed, 2 skipped, 0 failed（5 分 27 秒）
$ pytest tests/test_api_format.py -q
  → 17 passed（0.85 秒）
```

顺手做了一次死代码体检：`api_server.py` 192 个顶层函数里，95 个在文件外零引用，
但逐个看下来——26 个在文件内也零引用，其中绝大多数是 `@app` 路由（框架按装饰器调用，
文本搜索看不到），真正能删的**只有 1 个 3 行的 `_us_eastern_now`**（本轮已删）。
结论：这个文件的病不是"垃圾多"，是"职责混"。所以选择拆，不是删。

## 之前顺手修的三类真问题

1. `tests/test_repository_news.py`、`tests/test_v418_manual_chain_guard.py` 在 import 期就读真实库（后者拷 schema），`pytestmark = skipif` 来不及生效——无库环境是 **ERROR** 不是 skip。已改模块级 `pytest.skip(allow_module_level=True)`。
2. `test_v413::test_analyze_within_threshold_is_normal` 断言的是旧规则（step 阈值 = 3×threshold），代码早已改成固定 700s，用例一直红着没人看。已改为显式传 `hang_after`，测的仍是原意。
3. 全量首跑 6 红，其中 5 红是上面两类环境问题，1 红是用例与实现脱节——**这就是不跑全量的代价：红的用例会被当成"已知噪声"。**

## 不该为了分数做的事

加更多功能（第 9 项已经 8 分，缺的不是数量）；写更多形容词（第 8 项已满分，再加就是注水）。
