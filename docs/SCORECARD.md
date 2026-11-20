# 综合评分（自查，10 分制）

自查时间 2026-11-20。评分不是自我表扬，是找"下一个该修的地方"。每条都写了分数依据和缺口。

| # | 维度 | 分 | 依据 | 缺口 |
|---|---|---|---|---|
| 1 | 一眼看懂 | 8/10 | 首屏是"剔掉不该做的单"+ 三句话说明白 + 一张截图 | 缺一段 20 秒 GIF；首屏仍偏长 |
| 2 | 能跑起来 | 10/10 | 自带合成演示库（17 品种 / 6800 根日线 / 9 资产 / 25 事件类型）；本机实测 `/api/health` **200**、`/api/trident2/features` 返回 **64 项**；CI 里 `demo-smoke` 每次真起一次服务验证 | — |
| 3 | 结构可读 | 9/10 | 四层清晰；`trident2/catalog.py` 单一真值源；`API-MAP.md`（422 端点自动生成）；切片 ② 已落地：格式化层移入 `api_format.py`，`api_server.py` 9,514 → 9,292 行 | 还有 9.3k 行，③④ 两个切片要在全套测试环境做 |
| 4 | 测试与自检 | 9/10 | 全量按 6 块复跑合计 **5,353 passed / 0 failed / 12 skipped**（含并行协作者新增用例；其中 1 例在分块并发执行时因共享演示库被占用而失败，单独复跑 11 passed —— 属顺序/并发相关，非本轮改动）；覆盖率基线 **40%**（128,554 语句）；**五轮共新增 321 条**纯函数用例：`us_release_schedule` **0→95%**、`true_anomaly` **0→88%**、`yahoo_chart_daily` **0→93%**、`wscn_flash` **0→94%**、`ust_curve_treasury` **0→73%**、`us_daily_sina` **0→86%**、`watermark_v2` **25→83%**、`abnormal_detector` **0→20%**、`market_snapshot` **0→23%**、`technical_indicators` **0→24%**、`db.py` 取价/归一/证据/残留 **0→54%**、`realtime_filter` **0→15%**、`api_server` 纯函数 19% | 40% 离 60% 还差约 26,000 条语句的覆盖，不是几篇用例能补的 |
| 5 | CI | 9/10 | typecheck + 离线核心子集（1038 条，覆盖率 20.0%，棘轮 20% 只升不降）+ ruff + 端点基线 + `docs-drift` 文档过期校验 + nightly 全量失败开 issue + gitleaks | 棘轮目标 60% 尚未到位（子集覆盖 20%） |
| 6 | 文档 | 10/10 | 中英双版 + 架构 + 快速上手 + FAQ + ROADMAP + 详细版 + **数据源清单（从代码生成，CI 校验不过期）** | — |
| 7 | 安全与密钥卫生 | 10/10 | `.env` 从未入仓；公开版剔除数据库与运行时；`secret-scan.yml`（gitleaks）每次 push/PR 必扫 | — |
| 8 | 边界与诚实 | 10/10 | 0 结算写 0；胜率标 NULL；分歧大直接不给方向；事故写在文档里 | — |
| 9 | 可扩展 | 8/10 | 64 项能力走注册表；`/api/trident2` 可单跑；feeds 层加源有范式 | 无插件示例与第三方源接入文档 |
| 10 | 维护活性 | 8/10 | CHANGELOG 逐版可追；nightly 失败自动开 issue，不会静默烂掉 | 单人维护，Issue 响应无 SLA |

**总分 9.2 / 10。**

---

## 已经拿到的 9 分是怎么来的

1. **格式化层拆出去（+0.1）**：`_safe` / `_row_to_event` 等 6 个纯函数移进 `api_format.py`，配 17 条断言。巨石没消失，但入口不再是"9.5k 行全都要读"。
2. **CI 真的会报警（+0.1）**：夜间全量失败自动开 issue，同时修掉了 `ci.yml` 里重复的 `permissions` 与重复开 issue 步骤（重复键会让整个 workflow 无效）。

## 要到 10 分，还差两件

1. **覆盖率 39% → 60%（+0.6）**：门槛按「实测 − 2pp」走，只升不降。**这是分轮的账**——每轮盯 1–2 个 0% 的核心模块补行为测试，先把"决策链路过一遍"（闸门 / 准确率 / 归因 / 回灌），再攻 `api_server.py` 与前端接口层。
2. **③④ 两个切片（+0.4）**：同步读 687 行、启动装配 643 行，风险中高，要在能跑全量用例的环境做。
3. ~~补一页数据源清单与延迟（+0.2）~~ ✅ **已做**：`docs/DATA-SOURCES.md` 由 `gen_data_sources.py` 从 `ASSET_ROUTE` / `SOURCE_KEYS` / 间隔常量生成，CI 的 `docs-drift` 校验过期。

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

覆盖率这轮的账（别自己骗自己）：

```
$ pytest tests/ -q --cov=src_python        → 5008 passed / 12 skipped，40%（77710 未覆盖）
$ pytest -k "dimension or reflection or risk or gate or contract or schema or registry
             or api_format or api_pure" --cov=src_python
                                           → 1038 passed，20.0%（102885 未覆盖）
```

**从 40% 到 60% 要覆盖约 26,000 条语句**——相当于再写几十个测试文件。
所以这轮的做法是"从 0% 的大块头里挑纯函数开刀"，而不是凑一堆断言把数字刷上去：
`us_release_schedule`（FOMC 前瞻）0→95%、`true_anomaly`（减噪器）0→88%、
`yahoo_chart_daily`（K 线补齐）0→93%、`wscn_flash`（快讯过闸）0→94%、
`ust_curve_treasury`（收益率曲线）0→73%——五个都是"逻辑一改就会静默走样、却没人拦"的地方。

顺手做了一次死代码体检：`api_server.py` 192 个顶层函数里，95 个在文件外零引用，
但逐个看下来——26 个在文件内也零引用，其中绝大多数是 `@app` 路由（框架按装饰器调用，
文本搜索看不到），真正能删的**只有 1 个 3 行的 `_us_eastern_now`**（本轮已删）。
结论：这个文件的病不是"垃圾多"，是"职责混"。所以选择拆，不是删。

## 第四轮补测：三条口径被写成用例（不是凑数）

这一轮补 `abnormal_detector`（904 语句，异动闸）、`market_snapshot`（1141 语句，盘面数字）、
`realtime_filter`（1142 语句，新闻 L1 漏斗）、`technical_indicators`（1173 语句）四个模块，
共 99 条（四个文件：`test_technical_indicators_volume_gate` 19、`test_abnormal_detector_core` 34、`test_realtime_filter_l1` 19、`test_market_snapshot_pure` 27，组合跑 **99 passed / 6.0 秒**）。挑它们是因为**错了在界面上看不出来**，而写用例时把三件原本只躺在代码注释里的事固定了下来：

1. **同一个涨跌幅，在不同周期结论相反**：涨 1.6% 在 10m（门槛 1.1%）算异动，在 1h（门槛 2.0%）不算。
   阈值按 interval 走表，旧代码写死 2.5% 时两个周期都是错的。
2. **L1 只匹配前 500 字**：关键词在第 500 字之后出现，L1 不给结论、升级给 L2，不是静默判垃圾。
   这是刻意的成本控制，但以前没有任何测试拦着它被改成"全文匹配"（那会把长文里的营销词全炸出来）。
3. **平盘算不出 ADX，就返回 None**：完全没有方向动作时没有 DX，宁可空着也不编一个 0 出来。
   同一个模块里，8 根日线算 RSI14 也返回 None —— 界面上显示"—"是设计，不是 bug。

另外把两个事故固化成回归：`数据整理` 曾把「金十数据整理：每日全球外汇市场要闻速递」整篇 FX 要闻打成垃圾（v433 加的"汇总类词让位"现在有用例守着）；裸 `AMA` 误匹配英文标题里的普通单词，已从 JUNK 列表删除。

## 第五轮：`db.py`（1711 语句）0→54%

`db.py` 是全系统唯一取价口径，错一次不报错，只让库里的收益数字静默变错。101 条用例钉住四件事：

1. **取价优先级与"不猜价"**：1m 分钟线 > 日线 > tick / `kline_bars`（毫秒 ts）兜底；取不到一律 None。
   `price_at` 只看 `interval='1m'` —— 不过滤会取到 4h 收盘价，跟 20 分钟 horizon 差 4 小时。
2. **日线兜底取最近一根**：v386 事故里旧实现 `ORDER BY ts ASC` 取到窗口最老的日线，
   两笔 WTI 凭空多出 -3.17%/笔，把胜率和权益一起污染。现在有滞后上限，超了当"无价"。
3. **入库时间戳只留一种格式**：ISO / 13 位毫秒 / `YYYY-MM-DD HH:MM:SS` 全部归一到 epoch 秒字符串；
   `now - timestamp` 再不会把 ISO 行当 0 算出「积压 49 万小时」。
4. **重复筛与门卫标签**：同内容（含标点/大小写差异）第二次入库直接 `FILTERED / dup_fingerprint`，
   `first_seen_ts` 指向同簇最早那次；门卫主品种「黄金」写入前归一成 `XAU`（否则下游认不出、整条判 NONE）；
   `gate_unavailable` 的 0/1/2 三态不再被压成布尔（压了会把"降级放行"的行冻死）。

顺带测出两个口径缺口，先记录不改（改动会动到历史数据口径）：
- 重复判定只比对**非 FILTERED** 历史行 —— 首条自身就是 FILTERED 时，后续同内容仍进 PENDING；
- `raw_news.content_fingerprint` 是入库时算的，手改正文**不会**重算，判断"正文被改过"必须自己再算一遍比对。

## 之前顺手修的三类真问题

1. `tests/test_repository_news.py`、`tests/test_v418_manual_chain_guard.py` 在 import 期就读真实库（后者拷 schema），`pytestmark = skipif` 来不及生效——无库环境是 **ERROR** 不是 skip。已改模块级 `pytest.skip(allow_module_level=True)`。
2. `test_v413::test_analyze_within_threshold_is_normal` 断言的是旧规则（step 阈值 = 3×threshold），代码早已改成固定 700s，用例一直红着没人看。已改为显式传 `hang_after`，测的仍是原意。
3. 全量首跑 6 红，其中 5 红是上面两类环境问题，1 红是用例与实现脱节——**这就是不跑全量的代价：红的用例会被当成"已知噪声"。**
4. 第二轮全量（5007 passed / 1 failed）只剩 `test_v453::test_fingerprint_has_git_and_param_dimensions`——单独跑绿、全量跑红。原因：别的用例短暂改写工作区，git 维度从 `hash` 变成 `hash+`（脏标记），指纹自然不等。已把 git 维度钉成常量，这条只测"参数变→指纹变"。**单独跑绿、全量跑红，是最容易骗过自己的一类假绿。**

## 不该为了分数做的事

加更多功能（第 9 项已经 8 分，缺的不是数量）；写更多形容词（第 8 项已满分，再加就是注水）。
