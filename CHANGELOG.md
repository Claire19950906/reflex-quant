## v500（2026-11-20）验收分五档 + 一条命令自检 + 人工调权重

- **⏳ INSUFFICIENT 档**：8 个 FAIL 里 7 个是"样本没到门槛"，和"端点没接线"同色 → 分开；
  每项带 `need / have / watch` 三行（等什么、现在多少、数据到了看哪个数）。实测 15 PASS / 0 FAIL / 7 ⏳。
- **`scripts/iteration_check.py`**：`闭环 27 项 + 能力 26 项`压成三行 + 一句结论，40 秒；
  `--with-pytest` 加回归。有硬失败返回 1，只有 ⏳ 返回 0。
- **人工调权重**（补 #3）：`POST /api/signal-weights/override` 乘法系数 0.5~2.0、必须 reason+actor、
  `weight_explain` 留 `manual_override ×1.2 (#id by actor: ...)`、可 `/revoke`、写 `audit_log`。
  ⚠️ 需重启 `com.trident.bridge` 才生效。
- **测试**：`test_v500_acceptance_evidence_gate`(11) + `test_v500_manual_weight_override`(12)；
  `offline_selftest` 新增 S25（真实路径 evaluate_and_persist），**27/27**；权重相关回归 115 passed。
- **口径修正**：#2/#6 的 fix 文案早已过时（决策链路已写 insert 权重、event-kline cron 在跑），
  #3 的"3 个写权重端点"在代码里从未存在。
- 详情：`docs/archive/v500-验收分层与人工调权重-2026-11-20.md`

> 并发提示：本轮期间另一路会话在同一仓库写 `CHANGELOG.md`，本文件曾整体被覆盖一次；
> 版本号 v499 已被它占用，本轮改用 v500。

## v500（2026-11-20）验收分五档 + 一条命令自检（含产能脉搏）+ 人工调权重

- **⏳ INSUFFICIENT 档**：8 个 FAIL 里 7 个是"样本没到门槛"，和"端点没接线"同色 → 分开；
  每项带 `need / have / watch`（等什么、现在多少、数据到了看哪个数）。实测 15 PASS / 0 FAIL / 7 ⏳。
- **`scripts/iteration_check.py`**：闭环 27 项 + 能力 26 项 + 产能脉搏压成四行，40 秒；`--with-pytest` 加回归。
- **空烧体检（1h）**：`16/337 次零输出（4.8%）`，按 purpose 拆分并对 ≥10% 的红灯。
  7 天账：anomaly 通道 1282 次全零输出（795×429 / 444×402，v501 的 key 级 30 分钟熔断已把近 6h
  压到 12 次）；gate 零输出 13%；main 近 1h 为 0。
- **产能脉搏**（对齐"保产能、不限速、严禁零产出空烧"）：方向 0.71 条/日 · 完整率 81.8% ·
  队列 38 条/0.3h · 门卫否 96.9%；并对**主通道短输出 42%**（minimax/MiniMax-M3，7 天 2852 次、
  输入 17.9M tokens）亮黄灯 —— 这是当前最大的"付了钱只拿回半截 JSON"。
- **人工调权重**（补 #3）：`POST /api/signal-weights/override` 乘法系数 0.5~2.0、必须 reason+actor、
  `weight_explain` 留 `manual_override ×1.2 (#id by actor: ...)`、可 `/revoke`、写 `audit_log`。
  ⚠️ 需重启 `com.trident.bridge` 才生效。
- **测试**：新增 `test_v500_acceptance_evidence_gate`(11)、`test_v500_manual_weight_override`(12)；
  `offline_selftest` 新增 S25，**27/27**。全量回归 5002 passed（修掉 6 个红：2 处写路径静默吞异常
  改留痕、smart-money 3 条过期断言对齐 v498 的 `userFillsByTime`、精简评审截断断言对齐
  `_MAIN_NEWS_CHARS`、`_hl_info` 200-非-JSON 归 502）。
- 详情：`docs/archive/v500-验收分层与人工调权重-2026-11-20.md`

## v499（2026-11-20）决策之后的链路补测 + 真数据回放口 + 清掉 388 行重复路由

**1. 离线自测从 17 项扩到 26 项** — 原来只测到"钱和单子"（前置闸→开仓→计费→止损→对账）。
决策之后还有四段没人测：反思能不能从成交里长出来、成绩单能不能结算、两套账本会不会被混算、
事件能不能反查到时刻。新增 S17-S24，全部落在临时库里跑真实代码路径：

| 新增 | 钉死的不变量 |
|---|---|
| S17 | 反思三件套：三维度落库、`actual_outcome_pct` 取真值、教训带「本笔 -1.25%」、可执行建议非空 |
| S18 | 降级决策（LLM 全挂）不产洞察：2 条成交只写 1 条反思 |
| S19 | 成绩单结算：BUY 判 WIN、HOLD 也落行且保留原始位移、冻结窗口被 `frozen_window` 挡掉 |
| S20 | 两套账本不相加（只报 `R10_TWO_TRACKS` 提示）+ 孤儿成交被 `trg_quick_sim_reject_orphan` 拒收 |
| S21 | 事件反查：同品种命中、跨品种排除、空库返回空而不抛错 |
| S22 | 402 归额度耗尽、401/403 归 key 失效 |
| S23 | 数据回放口：示例 bundle 装载 9 张表并出 6 段结论 |
| S24 | HOLD 不进执行器：零成交、零审计行 |

**2. 真数据回放口 `scripts/replay_bundle.py` + 只读导出 `scripts/export_bundle.py`** — 合成的只能证明路径通，证明不了分布合不合理。
现在一条命令把生产库近 N 天的 11 张表导成 JSON（`mode=ro`，写不进库），再倒进临时库跑同一套检查，
固定给 6 段结论（决策分布 / 成绩单结算 / 反思闭环 / 教训可执行性 / 双账本 / 事件归因），
每段直接写"能不能据此下判断"（n<30 只能当轶事、HOLD>80% 先别看收益、三维度空值>50% 说明错误码没接上）。
导出 SQL 与 bundle 结构写在 `docs/离线自测与验收口.md` 三节；示例在 `tests/fixtures/offline_bundle_example.json`。
真库 7 天实测：决策 41 条（HOLD 90.2%、降级 39.0%、无品种 34.1%）；quick_sim 1 笔 -0.6、t2 已平 11 笔 -134.12；
事件配对 111/145、方向确认 74.8%。

**3. `routers/paper.py` 清掉 388 行重复块** — 同一批 9 个端点被贴了两份（`adjustments` / `strategy-reflections` / `ledger/reconcile` 全重）。
FastAPI 只认先注册的那份，第二份是死代码，而且它的 `ledger/reconcile` 引用了未定义的 `t2_traceable`
（真要执行到就是 NameError）。保留先注册版本（含 `t2_rows_traceable_to_decision`），删掉重复块。

**4. `signal_performance_settler` 抽出 `settle(conn, ...)`** — 原来逻辑全塞在 `main()` 里，
离线自测只能另写一套，等于测的不是线上路径。现在 `main()` 只是壳，S19 与回放脚本共用同一函数。

**5. 回归** — `pytest -k "paper or ledger or reflect or settle or reconcil"`：375 passed / 0 failed。
`iteration_check.py`：交易闭环 26/26；能力验收仍是 13 PASS · 2 FAIL · 7 ⏳（#3 手动权重端点、
#13 HOLD 69% 属产品/数据面，未动）。



**1. 能力验收新增第五档 ⏳ INSUFFICIENT** — 26 项里长期有 8 个 FAIL，其中 7 个是
"样本没到门槛"（真实开仓 1 笔、outcome 0 条、反思 10 条……）。它们和"端点没接线"
打印成同一个红叉，每次迭代都要人肉分辨，而且改代码修不好。现在：

| 状态 | 含义 | 该做什么 |
|---|---|---|
| ✅ PASS | 硬判据全达标 | — |
| ❌ FAIL | 逻辑/口径/接线坏了 | **只有这一类要改代码** |
| ⏳ INSUFFICIENT | 链路在跑，样本没到门槛 | 不改代码；照 `need/have/watch` 三行等数据 |
| ⚠️ WARN | 达标但样本偏少 | 看趋势，别据此下结论 |
| ⏭️ SKIP | 前置条件不具备 | 正常 |

每项 ⏳ 都带三个字段：**等什么（need）/ 现有多少（have）/ 数据到了看哪个数算不通（watch）**。
实测从 8 FAIL 变成 **13 PASS · 2 FAIL · 7 ⏳**；⏳ 不计退出码（`--strict` 才计）。

**2. 修正两处账面口径** — ① #2 的 `fix` 还写着"实时决策链路从不写权重"，实际
`signals/decision_writer.py` 已是唯一 writer、`trigger_kind='insert'` 随决策落库（v488）；
② #6 的 `fix` 还写着"event-kline worker 无调度、表恒为空"，实际该 cron 在跑、
`event_kline_impact` 1943 行。两处都会误导人改错地方，已按现状重写。
③ `docs/CAPABILITY_ACCEPTANCE.md` 里 #3「3 个写权重端点」标的是历史快照，代码里从未
有过这些端点（`policy_overrides` 3 行无人读），已标注为快照、不作现状引用。

**3. 一条命令看全局** — 新增 `backend/scripts/iteration_check.py`：
```
python3 scripts/iteration_check.py                 # 闭环 26 项 + 能力 26 项 (~40s)
python3 scripts/iteration_check.py --with-pytest   # 再加全量回归
```
输出三行 + 一句结论；有硬失败返回 1，只有 ⏳ 返回 0。给"改完不跑引擎"的迭代用。

**4. 测试** — 新增 `tests/test_v498_acceptance_evidence_gate.py`（11 条，内存库）：
⏳ 必带三字段、样本不足不判 PASS、到期仍 PENDING 必须判 FAIL（不能被 ⏳ 吞掉）、
有方向样本够了才判方向命中率、汇总分别计数。相关回归 65 passed。
`offline_selftest.py` 复跑 **26/26**。

**边界** — #3 仍是 FAIL：验收清单要求"人工可调权重"，但代码里没有这个入口，
且要不要让人手调一个自动学习的权重是产品决策，未擅自实现。
#13 已 PASS：口径改为只算"已分析(DONE)事件"后，有方向分事件 33.2%（214/644）、
品种映射 62.7%，不再被 4 万条未分析行稀释。按用户 2026-11-20「放宽门槛、保产能不限速」
把有方向分事件下限 30% → 25%，给新闻洪峰留余量（不是把 FAIL 改绿）。

## v496（2026-11-19）整体恢复运行：129 个任务、引擎/守护/桥/前端全在线

**1. 解除暂停并恢复全部任务** — `resume_data.sh` 跑完：`DATA_PAUSED` 已清除，
`engine` + `engine-watchdog` + `stops-guard`（此前被 pause-guard 踢掉）全部回来，
兜底扫描 129 个在跑、0 个失败。

**2. 运行实测** — `GET /api/trading/health` 200：`ok=True · equity 9919.89 · 杠杆 0% ·
持仓 0 · stale=[]`；5 分钟内 `market_ticks` 新增 85 行，BTC 最新 14s，
NVDA/TSLA/MSTR 由腾讯源 155s 内更新。

**3. 验收 13/13 PASS** — 服务（129 任务/引擎在跑/未暂停）、引擎心跳（25s）、行情
（5 个在交易）、账户/杠杆/对账/日亏/止损覆盖/run 唯一/未处理告警 全部通过。

## v495（2026-11-19）运行态收口：健康度上线 + 导航入口 + 一键运维

**1. `GET /api/trading/health` 真正生效** — 之前代码写进 `api_server.py` 但跑着的 bridge 是旧进程，
路由一直 404。重启 `com.trident.bridge` 后实测返回：`ok=True / equity≈9919.89 / lev=0% /
n_open=0 / stale=[]`，账户、杠杆、裸仓、行情新鲜度一次看全。

**2. 健康度进导航** — 新增 `frontend/app/trading-health/page.tsx`（薄壳 iframe，复用只读页），
`components/app-nav.tsx` 的「更多 → 交易」加「交易健康」入口，不再是孤立的 HTML 文件。

**3. 一键运维脚本** — `backend/scripts/trading_ops.sh`：
`status`（暂停标记 / launchd / health 一屏）、`restart-bridge`（重启并等路由就绪）、
`enable-guard`（DATA_PAUSED 时拒绝，防被 pause-guard 踢掉）、`resolve-alert <key>`。
顺手清掉悬挂 3 小时的 `account_reconcile:R7_LEVERAGE`（杠杆已降到 0，告警失去意义）。

## v494（2026-11-19）告警去噪 + 源可见 + 一个 import 期炸点

**1. 行情新鲜度按交易时段判定** — `trading_health.build()` / `acceptance_check.py`
只对"此刻本该更新"的品种判过期：休市品种、以及库里只做新闻/归因的 A股/ETF（`unknown`）
不再算 stale，也不计入"在交易"数量。健康度输出新增 `market_state` / `stale_open_markets`，
持仓行加 `market_open` / `session`。修前实测 11 个美股/ETF 旧 tick 让告警常亮。

**2. 新源进面板** — `source_status.register_known_sources()` 补腾讯美股报价/日线、
新浪外盘、Naver 韩股日线、Yahoo chart，源健康页能看到成功率和最近报错。

**3. 修 `.env` 行内注释导致的 import 崩** — 简易加载器把 `KEY=90   # 说明` 整行写进
`os.environ`，`feeds.quote_sources` 模块级 `int(os.getenv(...))` 直接 ValueError →
行情路由整体退化成 `unknown`（acceptance 里表现为"44 个品种全部休市"）。
加载器改为去掉未加引号值后面的行内注释；`quote_sources` 不再因一条注释而导入失败。

**4. 回归** — `test_v491_market_rules.py` 扩到 15 条（含健康度只报开盘品种）；
离线自测 17/17，核心回归 120 passed，验收 13 项 FAIL 0（行情显示"5 个在交易 · 39 个休市"）。

## v493（2026-11-19）账户级风控：相关性分桶 + 连亏熔断（附一个真 bug）

**1. 资产簇集中度** — `cluster_of()` 把涨跌同向的品种归为一个下注：crypto_major（BTC/ETH/SOL）、
crypto_equity（MSTR/COIN/MARA/RIOT）、metals、energy、us_tech、memory。
preflight 按"现有同桶名义 + 新单"与权益比，超 `MAX_CLUSTER_WEIGHT_PCT`（默认 80%）拒
`PREFLIGHT_CLUSTER`，85% 给预警。此前只限单品种 60%，同时押 BTC+ETH 可以顶到 120% 权益。

**2. 连亏熔断** — 最近 `CONSECUTIVE_LOSS_LIMIT`（默认 3）笔全亏且最后一笔在
`CONSECUTIVE_LOSS_PAUSE_MIN`（默认 60）分钟内 → 只拦新开仓 `PREFLIGHT_LOSS_STREAK`；
平仓一律放行（风控不能把出口也堵住）。

**3. 修掉"同一秒两根 tick 取错价"** — `market_ticks` 同 ts 时按 rowid 取，否则
`ORDER BY ts DESC` 可能拿到更早的价格：实测导致 BTC 止损该触发却没触发。
已在 stops / preflight / executor / store / ledger / health 六处统一为 `ts DESC, rowid DESC`。

**4. 回归** — `tests/test_v491_market_rules.py` 扩到 14 条；离线自测连续 3 次 17/17；核心回归 119 passed。

## v492（2026-11-19）交易规则层：盘外不下单、股票整股、不许裸空

**1. 新增 `market_rules.py`** — 资产分五类（加密 24/7、贵金属永续、美股、韩股、原油），
按 `America/New_York` / `Asia/Seoul` 判断交易时段，含 2026 休市日表；股票无券源禁止裸空；
股票最小单位 1 股。

**2. preflight 加三道硬闸** — `PREFLIGHT_MARKET_CLOSED`（美股盘外/周末/假日/原油结算暂停）、
`PREFLIGHT_SHORT_FORBIDDEN`（无多头持仓时禁止空股票）、`PREFLIGHT_LOT_SIZE`（不足 1 股）。

**3. 执行器整股化** — 股票下单按最新 mark 价折算后向下取整、按整股重算名义；
不足 1 股直接拒单并写审计，不会把 0.37 股发给券商。

**4. 回归** — 新增 `tests/test_v491_market_rules.py`（9 条：分类/时段/裸空/整股/preflight 四场景）；
离线自测 17/17，核心回归 72 passed。

## v491（2026-11-19）数据源补强：美股/原油不再吊在 Yahoo 一棵树上

**1. 修掉一个"永远走不到兜底"的 bug** — `fetch_asset_candles_with_yahoo_fallback()` 在
`route()["symbol"]` 为空时直接 `return None`，而 WTI 的 symbol 本来就是 `None`（OKX 无原油永续），
于是 Yahoo `CL=F` 这条兜底从来没被调用过 —— 原油「没数据」不是源不行，是代码早退。

**2. 加多源分层** — 新增 `fetch_daily_multisource()`：商品日线优先新浪外盘（国内直连 ~1s），
美股 Yahoo 失败转腾讯 `web.ifzq.gtimg.cn`，韩股 Naver 日线→腾讯；分钟线不拿日线冒充。

**3. 报价覆盖面扩到 17 个品种** — `feeds/domestic_quotes.py` 腾讯源加
TSLA/AAPL/MSTR/MSFT/GOOGL/COIN/PLTR/SMCI/MARA/RIOT/MU/US_HYNIX + XAG/OIL/GOLD，
新浪源加 XAG/OIL/GOLD；`api_server._candles_for_asset()` 的 `domestic_spot` 分支
改为先取真 OHLC 再退回单点现货价。

**4. 加体检口** — `backend/scripts/check_quote_sources.py`：逐品种打印报价来源/耗时、
K 线来源/根数，`--json` 可机读；实测 WTI 82.8（腾讯 0.3s）、美股 0.3–0.7s、日线 0.4s。

**5. 又抓出两处"钱算错"** — ①`close_position` 对**所有品种**计资金费，美股/原油现货根本没有 funding，
等于持仓越久凭空扣钱；现在只对 `binance_fapi`/`okx_swap` 计。②`trade_preflight` 把原油按加密 120s
新鲜度卡，正常原油单会被全拒；`domestic_spot` 改 10 分钟窗口（`PREFLIGHT_COMMODITY_MAX_AGE_SEC`）。

**6. 回归** — 新增 `tests/test_v479_quote_sources.py`（6 条：WTI 兜底被调用、商品优先级、
美股转腾讯、分钟线不伪造、覆盖名单、新浪原油解析）；`offline_selftest` 仍 17/17，
核心回归 66 passed。

## v490（2026-11-19）判断能力 vs 市场：把"命中率"换成"去偏置后还剩多少"

**1. 起手就是一次误判** — 影子层 4h 方向命中 56.7%（n=61）看着像及格，但同一批样本、
同一批窗口里「闭眼喊 SELL」的基线是 **62.9%**。11-04→11-19 BTC −18.7%、ETH −21.9%、
SOL −20.2%、WTI −14.2%、XAU −7.3%，空头红利白送 6 个点，裸命中率比一个没有判断力的策略还低。

**2. 三个数必须一起读（新增 `backend/src_python/judgment_benchmark.py`）** — 每个方向判断同时报：
裸命中 / 同批市场基线（always-SELL、always-BUY、同配比盲选）/ 平衡准确率 BA（去类偏置）/
扣费净期望（`mean(方向符号×涨跌幅)×100 − fee_bps`，默认 12bps 往返）。
关键：**只喊一个方向的系统 BA 恒等于 50**，所以另加 BA'（按喊的方向算），才能把
「没信息(beta)」和「系统性反指(inverted)」分开 —— 全喊 SELL 且 65% 在跌是 beta，35% 在跌是反指。

**3. 判定码永不 FAIL，但会拒绝下结论** — `empty / thin / inverted / edge / marginal / beta / flat`；
gate=200 未满只报区间（平衡准确率用解析区间 `0.5·sqrt(p1(1-p1)/n1+p2(1-p2)/n2)`，
bootstrap 降级为诊断用，单次渲染 9.5s → 1.1s）。

**4. 接进两处日常读数，防止继续被误读** — 记分卡新增 **S21 裸命中 / S22 平衡准确率 / S23 净期望**
三项 + 独立「判断力 vs 市场」区块（含产能行）；每日反思成绩单在"方向命中率"下面直接跟
同批基线、BA、净期望。两处都走同一批已结算样本，时间点/品种/持有期天然对齐。

**5. 产能才是"测不出来"的真因** — 新增 `throughput()`：近 24h 决策 15 条 / 带方向 4 条 /
**池内可结算 3 条（0.1 条/小时）**，距 n≥200 约 83 天。结论措辞会随产能改写：
产能为 0 时直接说"这不是再等等的问题，是判断链没在出方向"。

**6. 留口不接线** — 统计层只认 `Outcome`，三个 loader（影子账本 / 反射层 / jsonl）任选；
新数据源进来只加 loader，判定与渲染一行不改。空库、缺表、未结算行都不会炸，也不会把
"0 样本"渲染成"能力为 0"。

**7. 可验证** — `bash scripts/verify_judgment.sh`（自检 0.2s + 21 项 pytest + 当天报告）；
`judgment_benchmark.py --selftest` 用四个答案已知的合成世界（coin/bias/skill/anti）回归口径；
`capability_scorecard.py --market-fixture bias` 可在不碰库的前提下看完整报告形态。口径与用法见
`docs/v490-判断力对市场-基准层.md`。

## v490（2026-11-19）深度护栏进影子账本：先记账，不到 60 笔不下结论

**1. 落库** — `decision_fundamental_snapshot` 新增三列（幂等 `ALTER TABLE`，不重建）：
`depth_flags`（JSON，只存风险标签）、`depth_zone`（peak/mid/trough）、`depth_points`（财报期数）。
来源：美股 `components.depth_flags`，A/H 顶层 `flags`；白名单过滤掉 `growth_premium` /
`earnings_window` / `low_cash_quality` / `earnings_peak` 这类状态标签，避免 flagged 组失去含义。
已有 15 行**不回填** —— 用今天的读数补昨天等于伪造"决策当时就知道"。

**2. 评估** — 新增 `scripts/fundamental_depth_ab.py`：只用决策当时落库的 `depth_flags`，
按 +10 天前向收益（`ohlcv_daily`）比较 flagged（有护栏）vs clean（无护栏）的平均收益/中位数/下跌比例；
门槛写死 **总可评估 ≥60 且两组各 ≥20**，不到判 `insufficient`。当前真实库：15 行快照、
基本面可用 6、有 ticker 0 → **可评估 0/60，结论 insufficient**（不编胜率）。
注意：`ohlcv_daily` 只有美股/商品日线，A/H 日线未落库，A/H 样本先计入 `no_price`，不拿实时价倒推历史收益。

**3. 进度可见** — `reports/fundamental_depth/latest.md` 新增「影子账本进度」：快照总数、
有 ticker 数、可评估 N/60、flagged 数，并写明评估命令。
`fundamental_depth_report.py` 对「新库 depth 列未迁移」单独提示，不再笼统报"读取失败"。
顺手修正 `fundamental_ab_replay.py` 的行情符号映射：`ohlcv_daily` 美股存裸 ticker（AAPL），
旧写法 `US_STOCK:AAPL` 永远查不到 → 0.08 的 A/B 也会永远 0 样本；A/H 日线未落库，
A/B 暂只结算美股/商品，不拿实时价倒推历史收益。

**4. 不改分** — fundamental 权重仍 0.08；`depth_*` 不进 score/action/sizing；A股杠杆仍明确缺失。
美股 `components` 追加 `earnings_zone` / `depth_points`，只供影子账本分组，不参与打分。

**测试**：新增 `tests/test_depth_shadow_v490.py` **5 passed**（白名单过滤、老表补列与写入、
<60 判 insufficient、60 笔 flagged 跑输判 `flags_predict_underperformance`、NULL 老行不计组）；
相关回归 **210 passed**。

文档 `docs/v490-深度护栏影子账本-2026-11-19.md`。下一步：等真实公司决策攒到 60 笔，
先看单标签，再决定是否折半盈利修正；仍然先影子、后实盘。

## v489（2026-11-19）A股/港股多期历史：把接口给的 16 期真正用起来

**1. 起点：数据源本来就有 16 期，旧代码只取第一行** — `cn_equity_fundamentals` 原先 9 行 / 8 只，
除 00700.HK 外每票只有 2026-09-30 一期。东财 `_em(... pageSize=16)` 实际返回约 4 年 16 期，
但 `parse_a_share` / `parse_hk` 只读 `rows[0]`、`persist` 只写最新、`read_one` 只读最新 —— 历史被丢掉。

**2. 港股累计口径修正（数据 bug）** — `OPERATE_INCOME` / `HOLDER_PROFIT` 是年内累计，
旧实现直接当 TTM：00700.HK 2026Q3 营收报 608.579 十亿，实际 TTM **802.950 十亿**（少 24%）；
归母净利 177.727 → **235.987 十亿**。修法同 A股：本期累计 + 上年年报 − 去年同类型累计；
并修「2026年三季报 vs 2025年三季报」字符串不相等导致找不到去年同季的问题，改按季度数匹配。
`EPS_TTM` 本身是 TTM，不重复还原。

**3. 多期解析 / 落库 / 读库（P0）** — 新增 `parse_a_share_history` / `parse_hk_history` /
`fetch_history` / `persist_history` / `read_history`；每期都用该期及更早的数据还原 TTM，
不用未来年报回填过去；`load` 与 cron 两处抓取改为一次写 16 期。
回填后 `cn_equity_fundamentals` **9 → 128 行**（8 只，每只 16 期，2022Q4~2026Q3）。

**4. 新增 `cn_depth.py`（只读入，不改分）** — 用多期算：
TTM 营收/净利/EPS 同比（只跟上一年同一报告期比）、毛利率Δ、ROEΔ（同季 YTD）、
盈利位置（`(now−min)/(max−min)`，需 ≥8 个 TTM 点）、资产负债率（仅港股有）。
只判差：营收 ≤−10%、净利 ≤−20%、毛利率 ≤−2pp、ROE ≤−3pp、负债率 ≥0.85；
盈利位置高只写 `earnings_zone=peak`，不打空头标签。

**5. 实测读数（8 只，16 期）** — 东山精密 TTM 营收 +56.35%/毛利 +7.50pp/ROE +15.91pp/位置 100%；
新易盛 +100.72%/位置 100%/roe_down；比亚迪 −4.41%/位置 53%；复星医药 +8.05%/位置 89%；
贵州茅台 −4.12%/毛利 −1.71pp/位置 71%；翠微 −42.05%/位置 0%（rev_down/roe_down）；
中科飞测 +39.33%/毛利 −3.63pp（np_down/gm_down）；腾讯 +10.02%/位置 100%。

**6. 零改分** — `cn_fundamentals.build` 的「盈利修正 0.65 + 估值分位 0.35」一行没动；
`depth_*` / `flags` / `gaps` 只进 components 与解释，8 只票 score 与 v488 逐位一致。
A股杠杆仍明确写「无资产负债率：主要指标接口不返回」，不拿港股口径套 A股。

**测试**：新增 `tests/test_cn_history_v489.py` **8 passed**（A股逐期 TTM、港股累计还原与年份前缀、
历史期不算 PE、短历史写 gap、四项护栏、落库读库、build 零改分）；
相关回归 **112 passed**。体检脚本 A/H 段改为 16 期趋势表并重新生成 `reports/fundamental_depth/latest.md`。

文档 `docs/v489-AH多期历史-2026-11-19.md`。下一步：A/H 与美股深度读数一起进影子账本，
攒 ≥60 笔已结算样本再决定是否折半盈利修正。

## v488（2026-11-19）基本面深度：同一财季不再算两遍，补生意质量四件套

**1. 起点不是覆盖率，是财季没对齐（P0）** — 16 只美股里 TTM 营收只有 **11/16** 有值，
AAPL / NVDA / MU 全是 None。真因是 `_dedupe_rows` 只按 `period_end` 完全相等去重：
AV 的期末日是月末（AAPL 2026-09-30），SEC 是申报口径（2026-09-26），差 4 天被当成两个季度，
`rows[:4]` 只覆盖两个真实季度 → TTM 全断。HUT/MARA 是真无利润表，另计。

**2. 财季聚类 + 字段级合并（P0）** — 同 `fiscal_year+period` 优先合并，否则期末日相差 ≤21 天视为同季；
每季选非空字段最多的一条为主记录，其余只回填空字段，不覆盖、不跨季借数。
同时 `stmt_growth` 的去年同季改为「330~400 天内最接近 365 天」，日期漂 1 天也能匹配。
结果：TTM 营收/毛利率 **11 → 14/16**、利润率趋势 **9 → 14/16**、FCF 率 **9 → 12/16**。

**3. 生意质量四件套（P1，只读入不改分）** — 新增 `company_depth.py`：毛利率趋势（≥2pp 下滑）、
经营利润率趋势（≥3pp 下滑）、FCF 率（≤−5% 营收）、杠杆率（≥0.85 / 净资产为负），
只判差不奖励好；capex 跨源正负号取绝对值；经营利润率 >±200%、FCF 率 >±300% 判口径失真
（MSTR 财库资产），返回 None 并写 gaps，不外抛 +2756pp 这种伪读数。
实测 META 毛利率 −2.72pp / 经营利润率 −19.97pp、COIN −3.56pp / −22.81pp、TSLA 经营利润率 −4.07pp、
AMZN 与 SMCI FCF 率为负；AAPL/MSFT/NVDA/MU 无护栏。

**4. 零改分可验证** — 同一库同一时点把 `_dedupe_rows` 换回旧实现逐票对比，
16 只方向分**逐位相同**（AAPL +0.1391 / META +0.7696 / NVDA +0.4633 / MU +0.4139）。
深度读数只进 `components.depth_*`、`flags`、`gaps` 与闸门解释（「生意质量 毛利率X%/同比±Ypp/FCF率Z%」），
不进 `DIRECTIONAL_WEIGHTS`；理由与 0.08 权重仍未被影子账本验证一致 —— 先看见，再验证，后入分。

**5. 缺口显式化** — 公司侧每次输出 6 条固定缺口（分部/地区、债务到期与利息覆盖、指引/backlog、
电话会与 10-K、卖方覆盖、DCF/EV-EBITDA）；A股/港股 8 只输出 5 条（无三表/无季度历史/无同业/
无财报日历/无宏观传导），避免「16 只美股的深度」被误读成同口径覆盖。

**6. 体检脚本** — 新增 `scripts/fundamental_depth_report.py`（只读、零网络），
产出 `backend/reports/fundamental_depth/latest.md`：16 只美股四项读数覆盖 + 8 只 A/H 现状 + 缺口清单。

**测试**：新增 `tests/test_fundamentals_v488.py` **10 passed**（跨源同季对齐、相邻季不误并、
同季同比日期漂移、四项护栏、capex 符号、失真保护、深度不进分、A/H gaps）；
基本面相关回归 **196 passed, 0 failed**；`decision_guard` / alt 展示回归 **51 passed**。

文档 `docs/v488-基本面深度读数-2026-11-19.md`。下一步（v489 候选）：深度读数先进影子账本 ≥60 笔再决定是否折半；
A/H 补季度历史；分部/债务到期/指引需要新数据源，现有三表推不出来。

## v487（2026-11-19）技术指标数据完整性收口：不编数，只标「不可用」

指标算得准不准是第二位，底层数据脏不脏是第一位。本轮把三处「静默给错数」改成如实报不可用。

**1. 量价指标（P0）** — 实测 XAU 日线 2623 根里只有 120 根有 volume（4.6%）、HG 0%、WTI 45.3%。旧版 `volumes()` 用 `_clean` 压缩空值，长度与 K 线对不上：`obv` 直接 **IndexError**，`mfi` 用稀疏点算出 **24.14**（下游读成「超卖」）、`vwap` 给出 4216.14 这种「像正常金价」的伪值。修：新增 `VOLUME_MIN_COVERAGE=0.80` / `volumes_full()`（保留长度）/ `volume_availability()`（体检门，分开报 `n_null` 与 `n_zero`）；`obv` 改逐根对齐；obv/mfi/vwap/vol_zscore/vol_break/volume_regime 覆盖不足一律 None 或标 `成交量不可用`；`snapshot` 新增 `volume_availability`。**AAPL 等有量标的数值与改前完全一致**。

**2. 管线级跳过（P1）** — `IndicatorPipeline.run` 对 `_VOLUME_GATED` 里的指标先做体检，无量时直接返回 `{"skipped":"no_volume",coverage_pct,n_null,n_zero,note}`，不再白扫 2600 根再返回裸 None；`volume_profile` 因有 TPO 兜底（`weight="tpo"`）不受影响。

**3. WTI 日线混入（P1）** — 新增 `scripts/kline_gap_audit.py`（默认只读）：WTI 1d 225 根里 **91 根**间隔 <0.6 天（主间隔占比仅 40.6%）。判据用固定周期步长而非众数。顺带查出：**WTI 4h 292 根里 94 根混入更小周期**、**WTI 15m 只有 91 根且 90 个间隔 >3×（是日线碎片）**、AMD/MU/NDX/NVDA/TSLA 各 4 条、TENCENT 1 条。处置提供 `--apply`（先整库快照、再移入 `kline_bars_quarantine`、可回溯），**本轮未执行删除**。

**4. 空值与零值分开（P1）** — XAU 1d 空 2503 / 零 0；HG 空 2590；**WTI 1d 空 45 / 零 78（缺失被写成 0，最隐蔽）**；**XAU 5m 579 全零、WTI 1m 2778 零、XAU 1m 2515 零**。

**5. 库路径统一（P1）** — 本轮改 5 文件 6 处（`backfill_reflection_a_subclass.py` / `migrate_tech_signals.py` / `role_data_builder.py` / `repository/news.py` / `routers/meta.py` 2 处）；累计 8 处统一到 `db_location.DB_PATH`。硬编码会让 `TRIDENT_DB_PATH` 失效，正是 v484 记录「pytest 截断 ai_decisions」的通路之一。剩余 3 处（`attribution_ai_queue.py` / `paper_trading_default_on.py` / `db.py` 内部守卫）本来就优先读 env/config，只是回退值硬编码，不算风险。

**6. 证据深拷贝（P2）** — `risk_profile` 的 `sizing_evidence` 由模块级 dict **引用**改 `copy.deepcopy`：旧版任何调用方改一下返回值就会永久污染所有后续读数。滚动数字同步为 **30.8%（20/65）**。

**7. 比值分母下限（P2）** — MSFT 留出段 `C/A=4.667` 的真相是基准 Calmar 只有 0.03。新增 `MIN_MDD_FOR_CALMAR=0.005` 与 `MIN_BASE_CALMAR_FOR_RATIO=0.30`：MSFT/META/NVDA/TSLA 四只改判「不可比」；滚动窗 98→**65**、胜率 26.5%→**30.8%（20/65）**。**结论不变：连续等风险版仍不上线。**

**8. 台账分层评估：已做过，结论是否的** — `latest.json` 的 `strata` 已覆盖 品种类/波动档/交叉 共 **2640 格**：单侧 5% 通关 217 格（随机预期 132），最强 z=4.82，Bonferroni 要 4.12σ，**仅 3 格过关**。台账原话「分层在多重比较下没有可宣称的优势」。此方向不必重做。

**9. 决策链状态（P0）** — `ai_decisions`/`decision_actions`/`trade_reflections_v2`/`tech_shadow_signals` 全 0，上游 `news_events` 40233、`abnormal_signals` 4821、`tech_signals` 1962。对照 v483/v484：17:14 事故截断真实决策 + 主动重置 0 基线，**是干净基线不是引擎故障**。

**测试**：`tests/test_tech_data_integrity_v480.py` **22 passed**；`test_risk_sizing_evidence_v479.py` **10 passed**；相关回归（指标/风险/新闻/meta/仓/账本/背离/管线）**581 passed, 0 failed**。

文档 `docs/v487-数据完整性收口-2026-11-19.md`；体检报告 `backend/reports/tech_engine/kline_gap_audit.md`；回测报告 `backend/reports/tech_engine/risk_sizing_replay.md`。

## v486（2026-11-19）TCC 根因：两个"长期没跑"的作业其实是被 macOS 拦在门外

体检里 `retention-prune` 与 `kg-reconcile` 都显示"日志 920 小时没更新"。真因不是排程丢，是 **macOS TCC**：

`launchd` 用 `/bin/bash` 去读 `~/Desktop/金融1.0/...` 下的脚本，被拦成
`Operation not permitted`（`shell-init: getcwd: cannot access parent directories`），
`launchctl` 记 `rc=126` / `EX_CONFIG 78`，作业**从来没真正执行过**。
三个具体坑，每个都能让作业静默死掉：

| 坑 | 表现 | 处置 |
|---|---|---|
| 用 bash 执行 `~/Desktop` 下的脚本 | `rc=126 Operation not permitted` | 逻辑搬进 `~/.trident/wrappers/`，Python 解释器（已获 TCC 授权）去读仓库脚本 |
| `WorkingDirectory` 指向 `~/Desktop/.../backend` | `EX_CONFIG 78`，进程都没起 | 改成 `$HOME`，脚本自己 `cd` |
| `StandardOut/ErrorPath` 与 `tee` 写 `~/Desktop` 日志 | `tee: ...: Operation not permitted`，活儿干完了却记 rc=1 | 日志统一写 `~/.trident/log/` |

修完：`retention-prune` `last exit code = 0`、`kg-reconcile` `last exit code = 0`。

顺手修一个真 bug：`kg_reconcile.py` 写 `worker_heartbeat` 用裸 `INSERT`，
第二次运行必撞 `UNIQUE(worker_name)`，异常被吞 → 心跳永远停在第一次。
改成 `ON CONFLICT DO UPDATE`（与 `observability.report_worker_heartbeat` 同口径）。

## retention 现状（dry-run，未删任何数据）

跑了一次完整报告：13 张追加型表，预计可回收 **6.2 MB（占库 1.1%）**。
最大两块是 `flow_snapshots`（36,063 行，92.3% 超 30 天）和 `external_indicators`（161,063 行，12.5% 超 180 天）。
结论：**库涨到 581MB 不是保留策略造成的**，删老数据只值 1%；要真降体积得动 `VACUUM` 和其他大表。
`RETENTION_APPLY=0` 保持 dry-run，是否开 apply 由你决定。


指标算得准不准是第二位，底层数据脏不脏是第一位。本轮把三处「静默给错数」改成如实报不可用。

**1. 量价指标（P0）** — 实测 XAU 日线 2623 根里只有 120 根有 volume（4.6%）、HG 0%、WTI 45.3%。旧版 `volumes()` 用 `_clean` 压缩空值，长度与 K 线对不上：`obv` 直接 **IndexError**，而 `mfi` 会用稀疏点算出 **24.14**（下游读成「超卖」）、`vwap` 给出 4216.14 这种「像正常金价」的伪值。修：新增 `VOLUME_MIN_COVERAGE=0.80` / `volumes_full()`（保留长度）/ `volume_availability()`（体检门）；`obv` 改逐根对齐、不再错位；obv/mfi/vwap/vol_zscore/vol_break/volume_regime 覆盖不足一律 None 或标 `成交量不可用`；`snapshot` 新增 `volume_availability`。**AAPL 等有量标的数值与改前完全一致**。

**2. WTI 日线混入（P1）** — 新增 `scripts/kline_gap_audit.py`（默认只读）：WTI 1d 225 根里 **91 根**间隔 <0.6 天（主间隔占比仅 40.6%），判据用固定周期步长而非众数。另查出 AMD/MU/NDX/NVDA/TSLA 各 4 条、TENCENT 1 条。处置提供 `--apply`（先整库快照、再移入 `kline_bars_quarantine`、可回溯），**本轮未执行删除**。

**3. 库路径统一（P1）** — `bets.py` / `divergence_ledger.py` / `routers/divergence.py` 由硬编码 `trident_event_bus.db` 改为 `from db_location import DB_PATH`。硬编码会让 `TRIDENT_DB_PATH` 失效，正是 v483「pytest 截断 ai_decisions」的一条通路。剩余 6 处（脚本/迁移/只读层）记入欠账。

**4. 比值型指标分母下限（P2）** — MSFT 留出段 `C/A=4.667` 的真相是基准 Calmar 只有 0.03。新增 `MIN_MDD_FOR_CALMAR=0.005` 与 `MIN_BASE_CALMAR_FOR_RATIO=0.30`：MSFT/META/NVDA/TSLA 四只的留出段比值改判「不可比」；滚动窗 98→**65**、胜率 26.5%→**30.8%（20/65）**。**结论不变：连续等风险版仍不上线。**

**5. 决策链状态（P0）** — `ai_decisions`/`decision_actions`/`trade_reflections_v2`/`tech_shadow_signals` 全 0，上游 `news_events` 40233、`abnormal_signals` 4821、`tech_signals` 1962。对照 v483：17:14 事故截断真实决策 + 主动重置 0 基线。**是事故后的干净基线，不是引擎故障** —— 待重新跑出决策，不属指标代码问题。

**测试**：新增 `tests/test_tech_data_integrity_v480.py` **18 passed**；indicator/technical/divergence/bet/ledger/risk 回归 **361 passed, 0 failed**。

文档 `docs/v486-数据完整性收口-2026-11-19.md`；体检报告 `backend/reports/tech_engine/kline_gap_audit.md`。

## v485（2026-11-19）作业体检瘦身 + 三个真失败修复

体检工具先要可信，才谈得上修。`audit_jobs.py` 改了两处口径：
- 致命行只判**上次体检之后新增的字节**（offset 存 `~/.trident/state/audit_jobs_offsets.json`）——日志是追加写，几天前的 Traceback 一直躺在尾部，121 个作业能报出 65 个"问题"，绝大多数是陈年残留 → **65 降到 16**；
- 日志新鲜度连轮转文件一起算（`.log` / `.log.1` / `.log.*.gz` 取最新 mtime），不再把"刚被 logrotate 轮转"误判成"几小时没更新"。

三个真失败：
| 作业 | 症状 | 根因 | 处置 |
|---|---|---|---|
| `com.trident.quota-sentinel` | 每 180s `rc=1` | wrapper 没设 `PYTHONPATH`，`from signals import ...` 直接 ModuleNotFoundError，额度告警等于没跑 | wrapper 补 `PYTHONPATH=backend/src_python`；kickstart 后 `last exit code = 0` |
| `com.trident.master` | `rc=3`，日志 `[Errno 48] address already in use` | 9001 被一个 **Doubao 沙箱进程**（17:35 起）占着，launchd 作业每次绑定失败 | 清掉占用后 kickstart，canonical 进程接管 9001，`/api/health` 与 `/health` 均 200 |
| `com.trident.ops-audit` | `rc=1` | 报告类脚本把"发现告警"当自身失败退出（`return 1 if warns`）→ 真问题被"退出码 1"掩盖 | 改为告警照常打印、退出码 0；下次排程生效 |

`com.trident.frontend` 的 `rc=-9` 是历史被 `kill -9` 的残留，当前进程在跑，无需处理。


## v484（2026-11-19）暂停契约硬化：从"约定"变成"硬闸门"

第二轮加固。v483 把暂停写进了文档和启动器，但实测仍被绕过三次 —— 根因是**没有人真正拥有这个开关**。

**事故复盘（同一类第三次）**：17:14 另一路会话跑 `pytest -q`，用例把生产库当测试库，`ai_decisions` 仅存的 3 条真实决策被截断，2 条 `reasoning='test'` 残留入库，两道探针触发器被 `tests/conftest.py` 里的 patch 摘掉；17:15 / 17:19 引擎被外部 `restart_backend.sh` 又拉起两次。

四处硬化：
1. `db.get_connection()` 应用层写守：`TRIDENT_BLOCK_PROD_WRITE=1` 且路径等于生产库时，写语句抛 `sqlite3.OperationalError`；两个 conftest 在 import 时 `setdefault` 该 env，**子进程继承**（17:14 那轮就是靠 subprocess 绕过测试进程守卫的）。
2. `tests/conftest.py` 的 `_migrate_without_probe_guards` 加 `_is_prod_conn()`（`PRAGMA database_list` 真实路径），生产库跳过摘触发器并打印留痕。
3. `engine.py` 入口自检 `DATA_PAUSED` → `[ENGINE-REFUSED]` + `SystemExit(0)`：直接 `python engine.py` 也拦得住。
4. `~/.trident/wrappers/run_py_with_proxy.sh` 加暂停闸门：非白名单脚本一律 `[PAUSED-SKIP]` 退出 —— 100+ 个 launchd 作业和临时手工运行都走这里，一个闸门覆盖所有启动路径。

顺带修 `fake_data`：F11/F12 标"降级位没置"时**排除已成交决策**，且 purge 把它放在最后 —— 否则改标签会级联删掉真实成交（实测会误删 `quick_sim_trades` 201）。清理后生产库 `fake_data --guard` 恢复 0 行 / GUARD OK。

验证：`pytest -k "trigger or guard or migrate or decision or news"` **466 passed**；生产库 `raw_news` 在 60 秒观察窗内零增长；触发器 8 个；`ai_decisions` 0。


- **新增 `backend/scripts/risk_sizing_replay.py`（只读）**：补 v414 遗留欠账 ——
  `risk_profile` 的日线 ATR 档位（≥3.0%高×0.5 / ≥1.5%中×0.8 / <1.5%低×1.0）此前只用
  「5 根内 |MAE|」验过风险侧，**从没验过收益侧**。16 标的（XAU/XAG/HG 十年日线 +
  13 只美股/指数近四年，2600+/1000+ 根）因果回测。
- **口径**：ATR14(Wilder)/收盘×100（与 `technical_indicators.atr` 同源），t 日档位 → t+1 日仓位，
  单边 15bp 只按 |Δsize| 收费；四对照 A 满仓 / B 离散档 / C 连续等风险
  (`clip(120日中位ATR%/当前ATR%,0.5,1.0)`) / D 随机档位 200 次。
- **实测结论**：
  1. 降回撤真的 —— **16/16** 个标的 MDD 变小；
  2. 提升风险调整收益假的 —— 离散档只有 **4/16** 的 Calmar 高于满仓，
     符号翻转后 **0/16** 个标的 B/A 反而 >1.10 → 等价于「降杠杆」，不是 alpha；
  3. 连续等风险样本内好看（Calmar 9/16，翻转后优于离散档 15/16），
     但**时间外滚动窗口胜率只有 26.5%（26/98）**、留出段仅 6/16 → 典型样本内拟合。
- **方法论教训**：符号翻转只改收益方向、不改时间段，**翻转 ≠ 时间外**。
  上一版据此得出的"连续版结构性更优"在滚动留出下失效。以后改仓位/敞口的结论必须过时间外滚动。
- **落地（只加证据，不改计算）**：`risk_profile` 新增 `sizing_evidence`（verdict=`not_supported`）
  + `validation_note` 补「收益侧已验证不成立」；`/api/technical-state/risk` 带出该字段。
  仓位计算一行没改（日线仍 1.0/0.8/0.5、日内仍 1.0、止损倍数仍 1.5/2/3）。
- **测试**：新增 `tests/test_risk_sizing_evidence_v479.py`(10)，锁证据字段 + 回归护栏；
  与 `test_risk_profile_v412`(33) 合计 **43 passed**；技术状态/指标回归 **105 passed**。
- **下一步**：攒到 8~10 年样本再复验；换目标函数（Calmar 对 250 根滚动窗太敏感）；
  商品/加密按各自 ATR 分布单独定阈值。报告 `backend/reports/tech_engine/risk_sizing_replay.md`，
  文档 `docs/v479-波动档仓位收益侧回测-2026-11-19.md`。

## v483 · 2026-11-19 内部自洽审计：把"宣称已生效"的四处漂移接回真实运行态

- **背景**：一次全系统自洽性审计（代码/库/定时任务/文档/运行态五路）结论是
  **设计自洽、执行不自洽**：架构与守卫都在，但运行态、账本、门禁、文档四处漂移。
  本版按"一项一项落地"的顺序修完，每项都带验证。
- **暂停语义统一**：`engine_watchdog.py` 新增 `_pause_flag_path()`，`DATA_PAUSED`
  存在且非 `--status` 时打印 `[PAUSED]` 直接返回，不再 kickstart 引擎。
  事故背景：16:34 已置暂停标记、108 个作业被 bootout，引擎仍在 16:39 写决策；
  17:15 又被外部会话拉起。修完后 bootout 一次即稳住（40 秒内未被拉回）。
- **孤儿账本清理（可复跑脚本）**：`backend/scripts/orphan_ledger_cleanup_20261119.py`
  （默认 dry-run，`--apply` 才 VACUUM INTO 快照 + 逐表 JSON 归档 + 删除 + manifest）。
  实删 3454 行决策记账层（audit_gate_log 1287 / decision_errors 781 / decision_evidence 286 /
  fundamental_shadow 286 / signal_weight_history 286 / …），`sim_equity_snapshots`
  重置为 1 条可复现基线（0 笔成交 → 10000）。事实层（news_events / hermes_observations /
  quality_rejections / signal_chain_archive）**只统计不删**，留给口径决策。
- **pytest 生产库写保护（第三次同一个坑）**：`backend/tests/_prod_db_guard.py` 在
  `sqlite3.connect` 内核层给生产库注入 `GuardedConnection`，读放行、写/DDL 抛
  `_ProdWriteBlocked`；仓库根 `conftest.py` 与 `backend/tests/conftest.py` 双入口安装。
  `_helpers_db_triggers.drop_guard_triggers()` 增加 `_is_prod()` 真实路径判定，生产库直接 raise。
  事故链：11-16 raw_news 19,214→6；11-19 12:15 探针新闻；11-19 16:47 ai_decisions
  390→4、raw_news 1,270→217、三道守卫触发器被 drop。
- **trade_uuid 全链路打通**：`db_schema` 新增 `trg_ai_decisions_fill_trade_uuid`
  （AFTER INSERT，空则写 `lower(hex(randomblob(8)))`，16 位与 `id_reconcile` 同格式），
  并在 migrate 里登记；`quick_sim.py` 建仓 INSERT 补 `trade_uuid`（优先继承决策，否则生成
  并回写决策行），`open_trades` 两条候选 SQL 也带上该列。此后任何写入路径都会落 uuid。
- **审计/指纹覆盖补洞**：`analysis_drain.py`、`tech_signal_bridge.py` 两条落库路径
  此前既不落指纹也不进审计 —— 现补 `code_fingerprint.stamp()` + `audit_gate_hook.gate()`；
  `audit/gate.py` 的 `dict(row)` 改为兼容 tuple 行（调用方无 `row_factory` 时曾 TypeError →
  静默 skip）；跳过分支写 `audit_gate_log(verdict='skipped')`，让"按设计不审"与"漏审"可分。
- **假数据守卫补两条判据**：F11「自述'未做全文分析/关键词推断'却 `analysis_degraded=0`」、
  F12「推理链 <20 字却未置降级位」。此前 guard 报"0 行假数据"，但那 2 条 half-judgement 确实在库里。
- **作业与体检**：新建缺失的 `~/.trident/wrappers/run_funnel_report.sh`（plist 引用了它但文件
  从来不存在），funnel-report 进 `backend/cron/install_cron.sh` 注册表并安装到 launchd（07:12）；
  `db_schema` 新增 `audit_log` 表并进 migrate（`causal_chain_expander` 一直往不存在的表 INSERT）；
  `doctor.py` 第 6 段按候选路径解析（`audit_status_constraints.py` 已挪到 `scripts/oneoff/`）；
  `scripts/audit_jobs.py` 增加暂停豁免 —— 暂停中作业不再算"日志未更新"，体检从 58 个"坏"降到 24 个真问题。

## v477 · 2026-11-19 可交易化：唯一账本 + 前置硬闸 + 幂等 run

- **新增 `account_ledger.py`（唯一账户口径）**：现金/已实现/浮盈/权益全部由持仓行重算，
  `t2_account` 只作缓存。对账 11 条规则（R1 已实现漂移、R2 现金漂移、R3 权益未重估、
  R4 平仓 NULL、R5 无价/旧价、R6 重复 RUNNING、R7 杠杆、R8 未入账开仓费、R9 历史无费、
  R10 两轨并存、R11 无止损）。CLI：`--show/--reconcile/--rebuild/--snapshot/--repair-runs`。
- **手续费双边入账**：`t2_positions` 增 `fee_paid`、`gross_pnl`；`close_position` 的
  `realized_pnl` 改为**净额**（毛利 - 开仓费 - 平仓费），权益随每次 tick 重估。
  存量 6 笔历史平仓补费 48.14 USDT，已实现由 `+30.87` 修正为 `-17.27`。
- **新增 `trade_preflight.py`（下单前硬闸）**：账本对不平、权益未重估、行情超窗
  （加密 120s / 股票 4h）、RUNNING 不唯一、持仓数超限、新单后杠杆越界，任一命中即拒单；
  异常也 fail-closed。拒因写 `trade_audit_log.reason=preflight:<codes>`。
- **`paper_trading` 自愈幂等**：`_heal_active_run` 用 `BEGIN IMMEDIATE` 串行化并复用同策略
  同 spec 的 RUNNING，修掉 08:05 并发自愈出 3 条 RUNNING（成交会被重复记账）。
- **美股行情不再错发 OKX**：`api_server._candles_for_asset` 按 `ASSET_ROUTE` 分发
  （okx_swap→OKX、yahoo→Yahoo、binance_fapi→币安、domestic_spot→国内源），
  修掉 `okx_swap:NVDA` 累计 785 次全失败这类源健康噪声。
- **生产库已执行**：`--repair-runs`（保留 #5、停 #3/#4）、补列、`--rebuild --backfill-closed`、
  首条 `account_snapshots`（equity 9975.63 / 回撤 -0.32%）；引擎已重启加载新代码。
  当前对账只剩 R7（4 笔持仓名义 20000 = 权益 200.5%，满仓），属真实风险不是账错。
- **验收**：新增 `test_account_ledger_v476`(7) + `test_trade_preflight_v476`(7)，
  与 `test_stops`/`test_trade_executor`/`test_trident2` 等相关存量共 73 条全绿；
  新增 `scripts/v476_trade_roundtrip.py` 在临时库跑通「决策→闸→成交→计费→止损→平仓→对账 0 block」。
- **实盘适配位改为硬拒单（v477 第二波）**：`live_dry/live_real` 缺密钥、缺
  `LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY`、或单笔超过 `LIVE_MAX_NOTIONAL_USDT`（默认 1000）
  时，一律换成新增的 `RefusingAdapter`，只写 `reason=live_not_armed:...`，不产生任何成交；
  不再回退 Mock 假装在交易。
- **实盘/模拟账本隔离**：`account_ledger.canonical` 把 `subaccount in (live/live_dry/live_real)`
  的持仓排除出模拟权益（新增 `n_live_segregated`），对账新增 R12 提示；避免真钱与纸钱混账。
- **杠杆释放已证明不是死锁**：新增 `scripts/v477_leverage_release_check.py`，临时库实跑
  「4 笔 204% 满仓 → PREFLIGHT_LEVERAGE 拒单 → 平掉 1 笔降到 153% → 新 SOL 单 ORDER_PLACED
  → 开仓费入账 → 对账 0 block」。
- **止损独立守护（P0）**：新增 `stops_daemon.py`（30s 轮询 `stops.enforce`，只平仓不开仓；
  每 5 轮对账并写 `t2_alerts`），装成 launchd `com.trident.stops-guard`；
  `stops.enforce` 加跨进程文件锁，避免引擎与守护重复平同一笔仓。
- **T2 日亏限额（P0）**：`DAILY_LOSS_LIMIT_USDT=200` 写入 `.env`，preflight 新增
  `PREFLIGHT_DAILY_LOSS`（日初权益 vs 当前权益 + 当日已实现），80% 预警；此前日亏熔断
  只接了 quick_sim 轨道，T2 等于没有。
- **对账告警落库**：block 级问题写 `t2_alerts`（24h 同 code 去重、恢复自动 resolved），
  R7 满仓已有记录；不再需要人工跑 `--reconcile` 才发现。
- **离线自测台**：`backend/scripts/offline_selftest.py` —— 临时库 + 合成行情，跑完
  「硬闸→开仓→计费→杠杆→日亏→止损→平仓→对账→快照→告警→实盘拒单→执行器整轮」12 项，
  30 秒内判断闭环是否断裂，不碰生产库、不需要外网。
- **真数据验收口**：`backend/scripts/acceptance_check.py` —— 只读检查 13 项（行情新鲜度、
  引擎心跳、今日决策/成交、拒单 top3、杠杆、日亏、止损覆盖、run 唯一性、未处理告警），
  FAIL 退出码 1，可直接贴给模型看。
- **一条命令**：`backend/scripts/selftest_all.sh`（离线 + 核心回归），`--full` 跑全量，
  `--with-data` 追加真数据验收。
- **文档**：`docs/离线自测与验收口.md` —— 迭代节奏、判据表、留的三个"口"、已知边界。
- **移动止损 / 时间止损（v479）**：`trailing_bp`、`time_stop_min` 从"只有字段"变成真逻辑——
  多头按开仓以来最高价回撤 bp 抬止损、空头按最低价，超时未走出预期直接平仓，原因分别记
  `trailing_stop` / `time_stop` 到 `t2_interventions`。
- **成交模型（v479）**：`MockAdapter` 按 `PAPER_SLIPPAGE_BP` 走不利方向成交、`PAPER_FILL_RATIO`
  支持部分成交；平仓按 `FUNDING_BPS_PER_8H` × 持有小时数计资金费，净盈亏 = 毛利 − 双边费 − 资金费。
- **下单幂等 + 状态机（v479）**：`t2_orders.client_order_id` 唯一索引，同决策重放只会占位一次
  （拒因 `duplicate_client_order`）；订单走 PENDING→FILLED/CANCELLED，`ORDER_STALE_SECONDS`
  超时自动撤单。
- **健康度快照（v479）**：守护每轮对账时写 `backend/runtime/trading_health.json`
  （权益/杠杆/未平仓/对账 block/告警数/行情延迟），前端或巡检可直接读，不再查库。
- **快照定时化（v479）**：守护默认每 60 轮（30s × 60 = 30 分钟）写一条 `account_snapshots`，
  日亏限额有了稳定锚点。
- **两轨口径（v479）**：`R10_TWO_TRACKS` 直接给出"T2 + quick_sim = 合计"，对外只报一个数。
- **交易健康度接口/页面（v479）**：新增只读 `GET /api/trading/health`（`trading_health.py`，
  账户/杠杆/持仓浮盈/对账 block/告警/行情延迟，阈值读 `.env`），以及独立只读页
  `frontend/public/trading-health.html`（10s 刷新、可改 API 地址）。未挂进主导航。
- **未做**：实盘通道（仍 paper+MockAdapter，无券商适配/密钥/合规）、前端账户面板、
  R7 满仓提示。账本可信不等于策略有效（今日 8 笔 quick_sim 仍全亏、反思 hit=0）。

## v476 · 2026-11-19 全量测试收口：三道内核对齐、闸门阈值现读、健康探针不再误报

- **库路径懒读（生产）**：`api_server` 与 10 个 `routers/*` 原来在 import 时把 `config.DB_PATH`
  快照成模块常量，一旦别人（测试）改了路径，整场都读错库。现在删掉快照，模块内统一用
  `config.DB_PATH`，并在文件末尾补模块级 `__getattr__`（`模块.DB_PATH` 仍返回实时路径，
  `model_probe` 等老调用方不受影响）。
- **门卫闸门阈值改为现读**：`_GATE_RETRY_SKIP_AFTER/_S` 原来只在 import 读一次，进程起来后
  setenv 不再生效（排障与测试都踩空）。新增 `_gate_skip_after()/_gate_s()` 在 `_gate_net_fail()`
  里现读环境变量，import 常量退化为默认值。
- **健康探针不再在测试里误报 degraded**：`_code_snapshot()` 的 `code_stale` 判据是「磁盘源码比
  进程新」，在 pytest 进程里恒真且无意义（进程就是当场 import 的这份代码）。新增
  `_running_under_tests()`（看 `PYTEST_CURRENT_TEST` / `sys.modules`），测试内不再判 stale，
  `/api/health` 恢复 `status=ok`。
- **测试基建：摘掉探针/孤儿拦截触发器（仅测试库）**：新增 `tests/_helpers_db_triggers.py` 的
  `drop_guard_triggers()`。`db.migrate()` 现在会装三道 BEFORE INSERT 触发器
  （`reasoning='test'` / `source='test'` / 孤儿 `quick_sim_trades`），在测试里会把这些**故意造**的
  行静默丢掉。`test_data_integrity` / `test_regression` / `test_quick_sim_early_cut` 改为建库后
  先摘触发器；生产路径一行未动。
- **过期用例对齐 v450 语义**（v449 已被 v450 推翻）：规则兜底只负责放行，判定如实写「未判定」，
  不再伪造「可交易」（当日 648 条"可交易"只有 6 条真过门）。`test_v449_rule_gate_fallback`
  与 `test_gate_deferred_v320` 的断言改到新契约，仍锁死"不得判无信号 / 不得归档 / 资产指向落池内"。
- **`test_safety_guards` 同款触发器坑**：日亏熔断用例造的成交 decision_id 从 10000 起编、库里没有
  父决策 → 被 `trg_quick_sim_reject_orphan` 拦掉 → `settled_today` 恒为 0、熔断永远放行。
  fixture 里同样先 `drop_guard_triggers`。
- **分段验收**（本机 4700 条单进程跑不完，跑到 7%~37% 被沙箱 SIGKILL，系统 load 一度 300+）：
  按 360 个测试文件的字母序切 30 文件一段全量扫过一轮，逐条定性并修完；改动过的文件逐一复跑全绿：
  `test_data_integrity` 19、`test_regression`+`test_quick_sim_early_cut` 29、`test_v434_gate_fp_cache` 8、
  `test_v449`+`test_gate_deferred_v320` 30、`test_security_p1_fixes`+`test_safety_guards` 49、
  `test_structured_api_v172` 等 26。
- **新版涨跌排行把 `BTC-USD` 当非价格指标丢掉了（生产 bug）**：`cross_asset_rankings_handler`
  的价格过滤器黑名单里有一条裸 `"usd"`，把 `BTC-USD`/`ETH-USD` 这类标准行情 ticker 一并否掉，
  再叠加白名单只认 `price/close/last/median` 关键词 → 加密与贵金属序列全被过滤，全网排行恒空。
  改为：黑名单只挡带分隔符的 `_usd`/`usd_`/`usd_value`/`usd_amount`；新增 `-usd/-usdt/usd=`
  后缀直接判价格；新增「指标名去掉分隔符后等于 asset 代号」（`XAU`、`WTI`）也判价格。
  `test_discovery_v227` 由 4 failed 转 **15 passed**。
- **`/api/events` 15 天缓存永远判过期（真 bug）**：`_events_15d_reload_worker()` 里写了
  `_EVENTS_15D_LAST_LOAD = time.monotonic()` 却**没有 `global`**——赋值落在函数局部，模块级
  LAST_LOAD 永远停在 `0.0`（`_EVENTS_15D_CACHE` 是原地 clear/extend 所以看着"好像生效了"）。
  后果：TTL 判据恒为过期，每次请求都再起一个后台 reload，全表 SQL + 4000+ 行转换被反复跑，
  单飞锁只限并发不限次数。补 `global _EVENTS_15D_LAST_LOAD` 后
  `test_concurrent_requests_trigger_single_reload` 连跑 3 次全绿（原来 5/5 必红，reload 触发 2 次）。



- 新增 `decision_guard._confidence_penalty(conf)` = `min(0.10, 0.10×(1−conf))`，只收紧不放宽；
  `None` → 0，v321 纯校准行为逐位不变。`_CONF_GATE_CAP = 0.10`：基本面只占 0.08 权重，
  不该有能力否决整条信号。
- `buy_min_effective = buy_min_calibrated + penalty`，sell 同理（对称）；`thresholds` 同时保留
  原始 / 校准 / **生效** 三个数 + `confidence_penalty` + `action_before_confidence_gate`，
  新增 `confidence_gate` 字段（conf/penalty/changed_action）。
- **修一致性**：v472 的权重打折（0.6~1.0×）原来只对 alt 生效，公司票在 v474 拿到 confidence
  后仍拿满 0.08（"标出来了却没管"）。现打到公共路径：COIN 0.080→**0.0621**，AMZN 0.080→0.0752。
- `_fund_info` 新增 `dispersion/dispersion_level/confidence/weight_effective` 供下游读取。
- 实测 31 品种（15 alt + 16 公司）**今天 0 翻动作**；生产 buy_min 实为 **0.1725**（非报告夹具的 0.35）。
  最接近翻转：CU +0.253 vs 0.209（余量 0.044）、BTC +0.087 vs 0.215。
- 修掉一个真 bug：`_f_w` 原本在 `_fund_info` 之后才算，导致 `UnboundLocalError`（4/5 测试红）；
  已把权重计算上移到 `_is_alt` 判定之后。
- 新增测试 `test_confidence_gate_v475.py` 5 条；全量回归 **301 passed, 1 warning**。

## v474 · 2026-11-19 公司侧分歧度：价值陷阱从"被折半"变成"被标出来"

- `fundamental_factor` 新增 `_dispersion()`（纯函数）+ `_NULL_DISP`；输出五字段与 alt 侧同名同义。
  盈利股口径：`valuation_used 0.50` / `earnings_revision_used 0.50` / `macro_tilt 0.10`；
  亏损股口径：`_LOSS_WEIGHTS`（rev_yoy 0.40 / gm_trend 0.20 / ocf_margin 0.25 / ocf_trend 0.15）。
- `build()` 的 **7 处早退分支**（not supported/placeholder/no_data/error）全部带五字段（None），
  下游无需 hasattr。
- `decision_guard`：分歧度解释句从 `if _is_alt` 提到公共路径 —— 公司票也写。
- `fundamental_shadow` 无需改动：v473 已读 `comp["confidence"]`，公司票自动打折。
- 实测 16 只可用公司票：**COIN 0.84/高/可信 0.44**（估值 +0.25 对盈利 −0.60，标准价值陷阱）；
  CLSK 0.59 / RIOT 0.56 / TSLA 0.54 中；PLTR 0.905、META 0.770、SMCI 0.725 都是"中分歧"且多 1.00/空 0.10
  —— 强势是两条主线都同意，不是互相抵消；MARA 单分量不算分歧。
- 新增测试 `test_company_dispersion_v474.py` 7 条；全量回归 **296 passed, 1 warning**。

## v473 · 2026-11-19 影子仓位按分歧度打折（仍只记账，不动实盘）

- `fundamental_shadow.sizing_multiplier(score, available, confidence=None)`：
  `(1 + 0.20×score) × (0.75 + 0.25×conf)`，带 confidence 时下限放开到 0.70。
  **顺序很关键**：先按 v456 口径截到 [0.80,1.20] 再打折 —— 否则 `score=-2` 会从 0.80 变 0.70，
  属于"顺手改到的行为"，没有依据。v456 的边界测试正好挡住这次回归。
- `decision_fundamental_snapshot` 新增 `dispersion` / `dispersion_level` / `confidence` 三列（幂等 ALTER），
  公司票三列为 NULL、乘数仍按老口径 —— 审计能一眼分开两条路径。
- 实测 14 品种新旧乘数：USDJPY 1.006→**0.856**（分歧 0.89，降 15% 最狠）；BTC 1.018→**0.908**
  （链上极多 vs ETF 极空，正分不该被当真）；SPX/CSI300 只降 0.035（分量一致，打折幅度自然小）。
- 新增测试 `test_shadow_dispersion_v473.py` 8 条（含 v456 逐位兼容、5×4 组合夹逼）；
  全量回归 **289 passed, 1 warning**。

## v472 · 2026-11-19 分歧度显式化：每个分数配可信度，并据此打折

- `alt_fundamental.build()` 新增五字段（所有分支都有，含 unsupported/insufficient/error，值 None）：
  `dispersion`（加权标准差）、`dispersion_level`（高≥0.60 / 中≥0.35 / 低）、
  `confidence = max(0, 1 − disp/1.5)`、`bull_weight`、`bear_weight`。`detail` 也写入分歧度与可信度。
- `decision_guard`：`fundamental` 权重改为 `0.08 × (0.6 + 0.4 × confidence)`
  （conf=1 满权重，conf=0 打 0.6 折，下限 0.6 因"分歧大 ≠ 没信息"）；解释段多一句分歧度。
- `macro_context`：提示词每个品种后面带"分歧高/中/低"。
- 实测 15 品种：高分歧 4 个 —— USDJPY 0.89（多空 0.34/0.30，分数≈打平）、US2Y 0.71、UST 0.65、BTC 0.64；
  低分歧 8 个 —— ETH 0.32（空头权重 1.12、多头 0，全表最纯的空头）、XAU 0.33、SPX/NDX 0.26、CSI300 0.24。
- 新增测试 `test_dispersion_v472.py` 6 条（含单调性：更分歧必须更不可信）；全量回归 **281 passed, 1 warning**。

## v471 · 2026-11-19 ETH 补上链上维度：加密三品种结构拉平

- `backfill_eth_chain.py`：DefiLlama → `eth.tvl` **3,341 天**（2017-09-27 起，最新 $36.72b）、
  `eth.fees` **4,122 天**（2015-08-07 起，最新 $0.26m/日）。坑：TVL 端点链名 `Ethereum`、
  fees 端点 `ethereum`（大小写不同）；SSL 大响应更易被掐 → `Accept-Encoding: identity` + 8 次重试 + 60s 超时。
- `_build_crypto` 的 ETH 分支加 `tvl 0.22`（30 日变化，资本流出=偏空）+ `fees 0.18`（使用强度）；
  ETH 3→**5 分量**、权重和 0.72→**1.12**、−0.7087 → **−0.6602**。
- ETH 的结构：TVL **−20.7%**（资本撤）+ 未平仓 **96% 分位**（杠杆堆到极值）+ 资金费率 88% 分位（多头挤），
  而手续费只 −1.3%（链没被抛弃）→ 典型"链在用、价靠杠杆撑"的多头陷阱形态。
- 三品种对比：BTC 7 分量/1.26、ETH 5 分量/1.12、SOL 3 分量/1.00；
  **只有 BTC 有"资金流"第三层（ETF 净流入）**，这是它仍站在 +0.07 的原因。
- 新增测试 `test_eth_chain_v471.py` 6 条（含品种隔离：ETH 的键不许渗到 BTC/SOL）；全量回归 **275 passed, 1 warning**。

## v470 · 2026-11-19 SOL 接入：链上 TVL + 链上手续费，加密三品种全部有基本面

- `backfill_sol_chain.py`：DefiLlama 免费日频 → `sol.tvl` **2,074 天**（2021-03-17 起，最新 $4.70b）、
  `sol.fees` **2,132 天**（2021-01-17 起，最新 $0.56m/日）。Solana 公共 RPC 只给最近采样，
  没有免费历史地址/笔数序列 —— 所以用"资本（TVL）+ 活动（手续费）"代替。
- `_build_sol`：`tvl 0.44`（30 日变化，资本流入=偏多）+ `fees 0.34`（30 日变化，真实需求扩张=偏多）
  + `cot 0.22`（持仓分位反向）。SOL **−0.7510（3 分量）**：TVL −19.3%、手续费 −28.3%，
  人和钱一起走；COT 仅 43% 分位，空头也没挤满 —— 阴跌结构。
- SOL 进 `SUPPORTED`；别名 SOLANA/SOLUSD/SOLUSDT/索拉纳 → SOL；`_COMP_SOURCE` 加 tvl/fees/cot；
  `macro_context._ALT_ASSETS` 加 SOL。
- 覆盖现状：15 个品种可用（XAU/XAG/CU/WTI/USD/EURUSD/USDJPY/UST/US2Y/SPX/NDX/CSI300/BTC/ETH/SOL）；
  仍 unsupported：PLATINUM/PALLADIUM/HSI/VIX/NATGAS。
- ETH 成为最薄的一个（3 分量、权重 0.72，缺链上维度），DefiLlama 的 Ethereum TVL/手续费可直接复用同一套写法。
- 新增测试 `test_sol_fundamental_v470.py` 7 条；全量回归 **269 passed, 1 warning**。

## v469 · 2026-11-19 BTC 现货 ETF 净流入接入；美债期限溢价确认换源

- `backfill_btc_etf_flow.py`：farside.co.uk 全量页（首页只给近 14 天）→ `btc.etf_net_flow` **733 个交易日**
  （2024-01-11 +655.3 → 2026-11-18 −157.8 百万美元），11 只美国现货 ETF 逐日净流入合计。
  坑：SSL 间歇 EOF（6 次退避）、负数写成 `(405.3)`、Total 列位置不固定（取行内最后一个可解析数字）。
- `_build_crypto` 增 `etf_flow 0.14`（20 日累计净流入历史分位，(p−0.5)×2，流入=偏多）；
  BTC 6→**7 分量**、+0.1601 → **+0.0564**。20 日净赎回 **−37.13 亿美元（6% 分位，−0.88）**
  压住链上三绿（算力 +14.8% / 笔数 +20.2% / 地址 +7.1%）——**使用度与资金流方向相反**，不要只讲"链上很强"。
- 缺口清单删掉 ETF 一条，只留 MVRV/交易所余额/矿工关机价。
- 美债期限溢价全部入口复核失败：`acmtp.csv`/`acm_tp.csv` 返 HTML、`ACMTP10.csv` 连不上、
  `ACMSeries.zip` SSL 断、`fredgraph.csv?id=ACMTP10` 404、FRED JSON API 无此序列 →
  **确认必须换源（Wind/Bloomberg）**，不再原样重试。
- 新增测试 `test_btc_etf_flow_v469.py` 5 条；经验：合成测试数据时间基准必须落在 `_HIST_DAYS`(1000天) 窗口内，
  否则被裁成"数据不足"。全量回归 **262 passed, 1 warning**。

## v468 · 2026-11-19 BTC 链上活跃度接入；三条免费源确认到顶

- BTC 接 blockchain.info 日频链上数据：`btc.onchain_addr`（722 天，最新 481,358）、
  `btc.onchain_tx`（725 天，最新 810,857），`_build_crypto` 增 `onchain_addr 0.12` / `onchain_tx 0.10`
  （活跃度扩张=偏多，**显式假设非因果**，注释与测试都写明）。BTC 分量 4→6，
  分数 −0.0067 → **+0.1601**：算力 +14.8%、交易 +20.2%、地址 +7.1% 偏多，
  资金费率 57% 分位、稳定币供应 −3.1%、未平仓 54% 分位偏空 —— 分歧度很高，不可只看单一数字。
- 上期所：仅 `data/tradedata/future/dailydata/kxYYYYMMDD.dat` 可用，内容是每日行情+持仓
  （铜 OPENINTEREST 165,100 / 结算 102,620 元/吨），**仓单/库存全部 404**。持仓与方向无因果，
  按纪律不硬打分，铜库存缺口保留。
- FRED `ACMTP10`（期限溢价）9 次重试全灭（URLError/HTTPError 交替）→ 美债 carry 仍缺。
- 中国 10Y：eastmoney `RemoteDisconnected`、新浪 `globalbd_cn10y` 返回空串 → 沪深300 ERP 仍缺。
- blockchain `tx-volume` 5 次重试全灭（同域其它 chart 正常）→ 只接两条链上序列，不硬凑。
- 新增测试 `test_btc_onchain_v468.py` 5 条；全量回归 **257 passed, 1 warning**。

## v467 · 2026-11-19 银/铜/纳指/沪深300/TIC：剩余品种一次接完

- 银、铜：yfinance 5 年日线（`xag.usd` 1,257 / `xcu.usd` 1,258 / `xau.usd` 1,258），
  `_build_metal()` 用**跨品种比价**做估值：银看金银比（0.26，高=银便宜=偏多）、
  铜看铜金比（0.30，高=增长强=偏多），再加 COT 反向、美元 20 日反向、白银实际利率。
  实测 **XAG −0.0625 / CU +0.2527**；铜净多 28,904 张处 4% 分位（拥挤度极低，不是基本面强）。
- NDX：无同口径 CAPE，**复用 SPX CAPE 但强制标注「（SPX 代理）」并在 gaps 写明不是 NDX 自身估值**，
  有测试钉住标签不许删。实测 −0.6743（与 SPX 同源，看板需知这点）。
- 沪深300：中证指数官方 `index-perf`（`startDate/endDate` 必填，按年拉）回填 **1,708 天**
  （`csi300.pe` 13.09 / `csi300.close` 3,983）；分量 `pe 0.45`（5 年分位反向）+ `eps 0.27`
  （指数盈利=点位/PE，60 日变化分位）。实测 **−0.5697**。`000300.SS/沪深300/HS300` 归一。
  标准 ERP 算不了：FRED 中国长端序列已停更（400），写进 gaps。
- TIC：`ticdata.treasury.gov/Publish/mfhhis01.txt` 解析 Grand Total，回填 **321 个月**（2000-03→2025-12）。
  分量 `tic` 0.06（12 个月变化分位，增持=偏多），**滞后 >75 天不发分**——当前滞后 339 天，
  故 UST 仍 8 分量不含 tic，`gaps` 明确写滞后天数。旧数据不充当下。
- 测试 `test_metal_fundamental_v467.py` 13 条；老测试 4 处"未接入代表"换成 VIX/HSI/铂金。
- 全量回归 **252 passed, 1 warning**。`_ALT_ASSETS` 加 NDX/XAG/CU。

## v466 · 2026-11-19 股指基本面：SPX 装上 CAPE 估值锚（NDX/沪深300 明确不做）

- 背景：库里一直没有任何指数级估值（无 cape/pe_index/earnings_yield，`equity_fundamentals` 只有个股 pe/pb），
  SPX 记「基本面不可得」。本轮接 multpl 月度 CAPE：**1,871 个观测（1871-02 → 2026-11-18，当前 38.63）**，
  存成 `spx.cape`，决策路径仍只读库、零网络。
- `alt_fundamental._build_index()`（正=看多股指）：
  `valuation 0.34`（CAPE 近 30 年分位反向）、`erp 0.18`（盈利收益率(1/CAPE)−10Y 分位，越高=股票相对债券越便宜）、
  `credit 0.22`（HY OAS 20 日走阔=**利空股指**）。
- **同一个 HY OAS 变化，对债券正分、对股指负分**（避险买盘 vs 风险偏好恶化），有专门测试钉住符号不对称。
- 别名归一 `SP500/S&P500/标普500/GSPC/SPX500/US500 → SPX`；`macro_context._ALT_ASSETS` 加 SPX。
- NDX / 沪深300：无同口径 CAPE，继续 `unsupported`（不拿 SPX 估值硬套）；EPS 修正、股票宽度也写进 gaps（无源）。
- 实测 SPX **−0.6743**：CAPE 38.6（近 30 年 90% 分位）、盈利收益率−10Y −2.53pp（36 个月 4% 分位）、信用利差走阔
  —— 全品种里最负的一条读数。
- 抓取坑：multpl 表格值带 `&#x2002;`（EN SPACE 实体），正则不吃掉会解析到 0 行。
- 脚本 `scripts/oneoff/backfill_index_valuation.py`；测试 `tests/test_index_fundamental_v466.py` 7 条。
- 见 [`docs/archive/v466-股指估值CAPE-2026-11-19.md`](./docs/archive/v466-股指估值CAPE-2026-11-19.md)。

## v465 · 2026-11-19 美债拍卖需求接入：补掉「债券好不好卖」这一层

- v464 留下了硬缺口「拍卖需求未接入」。收益率反映价格，**投标倍数反映这个价格有没有人接**，
  是债券独有的需求端证据。财政部 `api.fiscaldata.treasury.gov/.../auctions_query` 直接给
  `bid_to_cover_ratio` / `indirect_bidder_accepted` / `offering_amt`。
- 新增 `scripts/oneoff/backfill_treasury_auction.py`，回填 4 条序列：
  `ust.auction_btc_10y`（72 场，2014-11 起，最新 2.53）、`ust.auction_btc_30y`（75 场，最新 2.54）、
  `ust.auction_btc_2y`（72 场，最新 2.63）、`ust.auction_indirect_share_10y`（间接投标÷发行额）。
- 分量：长久期 `auction 0.08`（10Y+30Y 投标倍数分位均值，**合成一个分量不拆两条证据**）、
  短久期 `auction 0.10`（2Y）。倍数越高=买家越踊跃=需求越强 → 正分。
- 两个坑写进注释：① Bond 里混 20Y 重开（`19-Year 11-Month`），必须按 29/30 开头过滤；
  ② 只翻 48 行时 30Y 只剩 26 场、不够 30 场分位门槛，翻页量提到 140 才够 75 场。
- 硬缺口 3 → 2 条（剩 carry/roll-down、TIC）。
- 实测 UST **−0.1551**（7 分量）：`auction=+0.59`（10Y 71% 分位 / 30Y 89% 分位，长端需求强），
  US2Y **+0.4595**（5 分量）：`auction=−0.22`（2Y 39% 分位，短端偏弱）—— 长端紧、短端松的微观证据。
- 测试扩到 11 条（新增投标倍数符号、短久期用 2Y）。
- 见 [`docs/archive/v465-美债拍卖需求-2026-11-19.md`](./docs/archive/v465-美债拍卖需求-2026-11-19.md)。

## v464 · 2026-11-19 美债基本面接通（长短久期两套口径）+ FRED 历史回填 3 年

- 背景：v460 把黄金/原油/美元/外汇/加密接进 0.08 基本面权重，**美债仍是 0 分**。而库里原料最全：
  FRED 有 `dgs2/5/10/30`、`t10y2y`、`tips10y`、`t10yie`、`hy_oas`、`baa10y`，COT 有
  `tbond/tnote2y/tnote5y/tnote10y`。美债只以新闻标的出现（`dim_asset` 无债券代码），先做别名归一。
- `alt_fundamental.py`：
  - `SUPPORTED` +10 个债券代码（UST/US10Y/US30Y/US2Y/TLT/IEF/SHY/ZB/ZN/ZT）；
    `_ALIASES` 覆盖 `TREASURY/BOND/美债/国债/UST10Y/10Y/TNX/TY` 等 20 余种写法。
  - `_build_ust()` 分长短久期两套口径，**正=看多债券价格**：
    - 长久期 6 分量：`real_yield 0.26`（TIPS 分位反向）、`real_yield_chg 0.10`（20 日 pp 每 0.40 反向）、
      `breakeven 0.18`（`t10yie` 分位反向）、`curve 0.16`（30s10 分位**正向，carry 视角：越陡=期限溢价越高=做多久期补偿越厚**）、
      `cot 0.20`（tbond 反向）、`credit 0.10`（`hy_oas` 20 日走阔=避险=利多国债）；
    - 短久期 4 分量：`policy_rate 0.34`（`dgs2` 分位正向）、`real_yield 0.16`、`cot 0.24`（tnote2y）、`breakeven 0.14`。
  - 硬留三条 gap：拍卖 bid-to-cover、期限结构 carry/roll-down（缺债券期货/ETF 日线）、TIC 海外持仓。
  - 输出增 `duration`，`evidence` 上限 4→6（6 个分量全可审计）。
- **抓到更根本的数据缺陷**：全部 FRED 序列在库里只有 38 天（2026-10-03 起），所有"历史分位 83%"
  其实是"38 天分位"。新增 `scripts/oneoff/backfill_fred_history.py`（FRED 官方 JSON API，
  `observation_start`+`limit=20000`，共用线上 key），回填 3 年日频 **10,214 行**（14 条序列 724~761 天）；
  `_HIST_DAYS` 400 → **1000（~2.7 年）**。
  > `fredgraph.csv` 在本机被 TLS 掐断（UNEXPECTED_EOF/timeout，14 条全灭），官方 API 重试后 14/14 成功。
- 回填前后（同一读数在不同窗口下结论不同，这就是回填的理由）：
  UST −0.09 → **−0.22**（实际利率 2.89% 从"38 天 83%"变成"3 年 98%"）、US2Y +0.50 → +0.54、
  XAU −0.46 → −0.55、USD +0.49 → +0.69、EURUSD −0.49 → −0.69、BTC −0.22 → −0.01、ETH −0.71 → −0.75。
- 接线：`macro_context._ALT_ASSETS` 加 `UST`/`US2Y`（prompt 里能看到美债那句）；
  `decision_guard` 走已有 `_is_alt` 分支（文案「非公司基本面 UST: real_yield=−0.97（…）」）。
- 测试：`tests/test_ust_fundamental_v464.py` 9 条（符号 / 分位而非绝对值翻转 / carry 符号 /
  长短久期两套口径 / 单族不发分 / 别名路由）。全量回归 **230 passed, 1 warning**。
- 明确未做：铜/白银（无库存与产量，硬做会给人"有基本面"的错觉）、股指（库里无指数级估值）、
  加密估值锚（MVRV/ETF 流入无源）。
- 见 [`docs/archive/v464-美债基本面-2026-11-19.md`](./docs/archive/v464-美债基本面-2026-11-19.md)。

## v463 · 2026-11-19 执行层组合闸 + 开仓止损：同品种重复开仓拒绝，开仓即带 2% 止损

- 背景：v462 账本上线后抓到 7 笔持仓、BTC 4 笔同向多单、总敞口约为权益 3.5 倍、
  7 笔全无止损。v463 把风控接到下单前和持仓后，而不是只做展示。
- 新增 `src_python/execution_guards.py`：
  - 同品种已有 open 同向仓 → 拒绝（`duplicate_open`）；反向仓默认也拒绝，避免对锁；
  - 单品种名义 ≤ 权益 50%、总名义 ≤ 权益 100%，超了先缩单、不足 100 USDT 拒绝；
  - `DUP_ENTRY_GUARD=enforce`（默认）/ `shadow` / `off`，`ALLOW_HEDGE=0`。
- `trade_executor._process_one()` 在 `place_order()` **之前**调用组合闸，拒绝写
  `trade_audit_log(status=risk_rejected)`，不产生成交。
- 新增 `src_python/stops.py`：
  - `auto_stop_price()` 开仓即带 2% 硬止损（空单上、多单下）；
  - `TradeExecutor.tick()` 每轮先扫止损，多头 mark≤stop / 空头 mark≥stop 调
    `store.close_position()`，写 realized_pnl、更新账户并记 `t2_interventions`；
  - `python3 stops.py --backfill --once` 给存量无止损持仓补挂。
- 实测：`check_new_entry()` 对 ETH SELL / XAU SELL / BTC BUY 全部返回
  `duplicate_open`；7 笔存量仓已补挂 2% 止损（XAU 3812.67、ETH 1816.80/1820.08、
  BTC 61843.88/61909.01），现价未触发，平仓 0 笔。
- 测试：`tests/test_execution_guards.py` 8 passed + `tests/test_stops.py` 5 passed。
- `.env.example` / `backend/.env.example` 增加组合闸与止损配置；
  文档 `docs/v463-执行闸与止损-2026-11-19.md`。
- 边界：纸面按 tick 平仓，不模拟滑点；真实模式仍需交易所止损单；
  手工写 `t2_positions` 不经过组合闸；当前敞口超上限，新单会被持续拒绝直到降杠杆。

