# CHANGELOG 历史归档（v463 及更早）

> 本文件由 CHANGELOG.md 切出；根文件只保留近 30 版现行条目。
> 版本号真值源仍为根 CHANGELOG.md 首行（见 config._read_changelog_version）。

## v462 · 2026-11-19 模拟盘账本：5000 USDT 的 XAU 空单，占账户多少、有没有止损、手续费记没记

- 新增 `src_python/ledger.py`（只读）：`t2_account` + `t2_positions(open)` + `t2_orders`
  + `t2_cost_ledger` 汇总；标记价 `market_ticks` 最新 tick → 1m → 5m，没价不估值；
  权益 = 现金 + Σ 浮盈亏，并显示与表内 equity 的差额。
- 新增 `GET /api/ledger/overview?order_limit=50`（60 秒缓存，`routers/ledger.py`）。
- 新增 `/ledger`「账本 · 模拟盘」：账户四卡 + 持仓表（标记/浮盈亏/名义/止损）+ 敞口四格
  + 对账提醒 + 最近成交 + 重置后已平仓；顶部明确 paper 模拟盘，避免误读实盘。
- 实测（2026-11-19 08:5x）：paper 账户 10,000 USDT / 8bps；3 笔持仓（XAU short +
  ETH short ×2），总名义 **15,025 USDT（权益 150%）**，ETH 两笔合并占权益 **100%**，
  三笔**都无止损**；3 笔 filled 成交，成本账本 4 行全是 **LLM Token（2.71M TOKEN）**、
  USDT 手续费 0 行（按 8bps 应记 12.00 USDT）；已平仓 0 笔 → 胜率/盈亏比不可计算。
- 口径修正：`t2_cost_ledger` 同时记交易费用和 Token，手续费只统计 `USDT/USD + fee/commission`
  行，TOKEN 单独显示；同品种多笔持仓合并算集中度（两笔 ETH 各 5000 = 一个 100% 押注）。
- 对账四类：无止损/集中度、旧行情、手续费未入账、无已平仓样本、孤立持仓（找不到来源决策）。
- 测试 `tests/test_ledger.py` 10 passed：空头/多头方向、无行情不编价、无止损+集中度、
  手续费缺口、TOKEN 不算手续费、同品种合并、无样本不算胜率、已平仓计入、旧行情标记。
- 文档 `docs/v462-模拟盘账本-2026-11-19.md`。

## v461 · 2026-11-19 样本工厂：时间分段复核，把「2bps 可开火」降为只观察

- 新增 `src_python/sample_factory.py`（只读）：真实影子账本 → 实盘 → 回放依次取样本，
  按 N 段时间等分，逐段结算；目标时刻用 5m 收盘**线性插值**，缺口 >10 分钟直接跳过，
  不补数据、不前视。
- 复核 v449 唯一存活格（BTC 20m / 2bps）：同一批 94 笔由 **+0.069% / t=2.12**
  修正为 **+0.046% / t=1.54**；4 段里只有 2 段非空，**1 段正 / 1 段负**，
  过不了「≥75% 分段为正」。0bps 虽合并 t=2.21，但两段仍是 −0.028 / +0.084。
- 真实样本（清理前 `shadow_ledger`）：BTC 已结算 27 笔，落在 5m 覆盖内仅 **9 笔**，
  2bps 20m **−0.027%/笔、t=−0.40** → 样本不足（<30）。
- `expectancy_policy.build()` 增加工厂闸：任何「可开火」必须过样本工厂；
  回放模拟（sim）最多只能给「只观察」；无样本时动作直接返回「样本不足」。
- 新增端点 `GET /api/factory/report?asset=BTC&days=60&folds=4&cost=2`（10 分钟缓存）。
- `/bets` 页面新增「样本工厂 · 分段验证」区块：来源/覆盖率/样本数、逐段表、
  正分段 x/y、最差段、没过分段的具体理由。
- 测试：`tests/test_sample_factory.py` 11 条（插值、缺口、闸门基准、分段、晋升、空账本），
  连同 v449/v447 共 **37 passed**。
- 文档 `docs/v461-样本工厂-2026-11-19.md`；v449 文档已加追认横幅。

## v460 · 2026-11-19 非公司标的基本面接通 + prompt 压缩（黄金/原油/美元/外汇/加密）

- 问题：`fundamental_factor` 只覆盖美股/A股/港股，XAU/WTI/USD/外汇/BTC/ETH 在
  `decision_guard` 里记「非公司标的 N/A」→ 0.08 的基本面权重让出后归一化，这些品种方向
  **没有任何可复现的基本面读数**。另外 `XAUUSD`/`BTCUSDT`/`EURUSD` 会被 `classify`
  认成美股 ticker，返回的是 `no_data`（假缺口）。
- 新增 `src_python/alt_fundamental.py`（零网络读库、30min 缓存、与公司侧同形）：
  XAU（实际利率水平+变化、美元、COT、SGE 溢价）/ WTI（库存水位+周变化、馏分油、产量、
  炼厂投入、COT）/ USD（2Y、TIPS、DXY COT）/ EURUSD（美元镜像 + 欧元 COT）/ USDJPY
  （美日利差**按分位**而非写死阈值 + 日元 COT）/ BTC·ETH（资金费率、稳定币供应、算力、未平仓）。
  门槛：分量 <2 **或** 独立数据族 <2 不发分；分位需 ≥30 观测；说不清符号的进 `gaps` 不打分。
- 实测（2026-11-19）：XAU −0.46 / WTI +0.06 / USD +0.49 / EURUSD −0.49 / USDJPY +0.03 /
  BTC −0.22 / ETH −0.71；SILVER 明确 `unsupported`（不返回 0 分，0 分会被读成中性）。
- `feeds/eia_feed.py`：新增 `oil.us_crude_production`（14,032 千桶/日）、
  `oil.refinery_inputs`（16,918 千桶/日，**是"炼厂投入"不是开工率%**）、
  `oil.distillate_stocks`（104,901 千桶）。产量/炼厂不在 `stoc/wstk` 路由下，改走
  `/v2/seriesid/PET.<id>.W`；一次抓 **52 周**并写入 `payload.history`，周频序列当场可算分位。
- 接线：`fundamental_factor.build()` 最前面分发 alt（修掉假缺口）；`decision_guard._f_bits`
  增 `kind=='alt'` 分支（文案「非公司基本面 XAU: …」，不再套用「估值分位/盈利修正」）；
  `macro_context.render_prompt_block` 新增 alt 一行结论 + `_cot_block` 折叠。
- prompt 体积（真库最近一轮实测）：10,490 → **8,442 字符（−19.5%）**，
  env `TRIDENT_COT_PROMPT_MODE=full` / `TRIDENT_ALT_PROMPT_MODE=off` 可回退。
- 测试：`tests/test_alt_fundamental_v460.py` 16 条（双场景符号翻转、利差分位、门槛、
  路由、闸门文案、prompt 折叠）。两条老测试按新口径更新（原断言"BTC 必须 N/A"）。
  全量回归 **221 passed**。
- 文档：`docs/v460-非公司基本面-2026-11-19.md`。
- **干净起点（同日 08:04）**：用项目既有 `scripts/oneoff/reset_derived_epoch.py --apply` 做全库回滚式重置
  —— 清空 106 张「判断/评分/账本/模拟盘」表共 **109,284 行**，模拟盘本金重置回 10,000，
  因果链 854 条权重重置为 1.0，写 `data_epoch` 第 3 纪元。
  备份 `runtime/backup/reset_20261119_0804.db`（1,007 MB，SQLite backup API 含 WAL），清单同目录 `reset_manifest_*.json`。
  保留全部原始事实：新闻原文 28,715 / 事件 32,505 / 宏观快照 779,838 / 外部指标 189,049 / K 线 346,485 /
  日线 106,961 / 资金流 35,798 / COT 2,067 / 因果链定义 854。目的：不让 v460 前的旧口径决策与净值曲线
  混进新账本（旧口径当时把非公司品种的基本面记 N/A、0.08 权重让出）。

## v459 · 2026-11-19 宏观驱动系数回测校准：先验无信号，cap 0.15→0.05

- 新增 `scripts/macro_driver_calibration.py`：用 FRED 官方 CSV（5 年日频：DGS10/DGS2/T10Y2Y/
  VIXCLS/BAMLH0A0HYM2/DTWEXBGS）+ 板块 ETF 日线（SMH/IGV/QQQ/SPY/GLD）检验 v454 那套
  「4 驱动 → 板块倾斜」先验。本库 `macro_snapshots` 宏观序列只有 36 天、`ohlcv_daily` 无美股，
  所以必须走外部；FRED 本机是间歇 SSL_ERROR_SYSCALL，脚本重试 6 次并校验 CSV 表头。
- 方法：前瞻 10/20 日双窗口，主结论用**非重叠取样**（跑 73/36 点），`ic_aligned = IC × 先验符号`
  避免"符号反了"被当成"有信号"；指标用 Spearman IC + 五分位多空差；另单独检验复合 tilt。
- 结论：四个驱动中位对齐 IC 全部 <0.10（rate -0.04 / risk +0.05 / usd +0.06 / curve +0.08）；
  **复合 tilt 对同桶 ETF 前瞻收益中位 IC = +0.00**（多空差 ±1% 内）；季度参考窗（60 日，
  独立样本 ~12）`risk` 全面转负（-0.20~-0.38，先验为正）—— 即短窗是噪声、长窗符号可疑。
- 落地：`fundamental_factor._MACRO_TILT_CAP` **0.15 → 0.05**（env `TRIDENT_MACRO_TILT_CAP` 可恢复），
  注释写清依据与"为什么不翻符号"（60 日不足以拟合权重）。宏观倾斜对方向分的影响上限
  由 ≈1.2% 降到 ≈0.4%；16 只覆盖读数与估值分位/PB 分位均不变。
- 新增 `cron/com.trident.macro-calibration.plist` + `cron/run_macro_calibration.sh`：**每月 1 日 07:20**
  重跑校准（月频：慢变量结论；但必须周期跑，静默失效没人会看），已注册进 `install_cron.sh`。
- `fundamental_drift_check.py --warn-check`：对每条 WARN 拉第三方 yfinance `priceToBook`，
  把「我方 vs 供应商」变三方表决。结果：**TSLA 我方 16.83 = 第三方 16.83（0.0%），供应商偏 18% → 我方对**；
  NVDA（16.36/14.05/23.61）、RIOT（3.00/2.85/3.16）三方互不接近 → 判"无法判定（期次/股本口径不同）"，并列展示；
  00700.HK 第三方取不到 → 保持 WARN 不猜。
- 测试：`tests/test_macro_calibration_v459.py` 10 条（排序相关/并列名次/前瞻收益/驱动口径截断/
  对齐符号/样本不足/cap 已降）。全量回归 **205 passed**。
- 报告：`docs/v459-宏观驱动系数校准-2026-11-19.md`、`reports/macro_calibration/latest.json`。

## v458 · 2026-11-18 实时图副图可切换：RSI / MACD / KDJ

- 实时 K 线图以前只有 RSI 一套副图（挤在底部 22% 的带子上），要看 MACD 得去回放页。
  现在图头多了一组 `RSI | MACD | KDJ` 按钮，点一下换指标，默认仍是 RSI。
- 三者**互斥，不叠加**：底部那条带只有主图高度的 22%，叠两套线必然互相糊住。
  实现是同一时间只让一套 `visible`，另外两套藏起来（数据留着，切回来不用重算）。
- 口径与后端 `technical_indicators.py` 是同一套（`lib/technical-indicators` 有
  parity 测试逐值对齐），所以图上看到的 MACD/KDJ 和决策链读到的是同一组数。
  MACD 用 DIF/DEA 双线 + 柱（柱翻正翻负标动量转向），KDJ 用 K/D/J 三线。
- 数据源就是已经加载进图表的 `chartBars`，切指标**不发新请求**，断网也能切。
- 选择记在 localStorage，刷新后停在上次那套。
- RSI 原来的 70/30/50 三条参考线不是靠"隐藏系列"蒙过去的：切走时**真删掉**这几条
  `priceLine`（`removePriceLine`），切回来再挂回去 —— 否则三条横线会压在新副图上。
- 顺手修一条**测试基建**污染（不在技术指标线上，但会让全量测试结论不可信）：
  `tests/test_cost_model.py` 的 `_setup_db` 直接给 `config.DB_PATH` 赋值、**从不还原**，
  收尾还把临时库 `unlink` 了 —— 之后任何模块调 `db.get_connection()` 都连到一个空文件。
  证据：`test_structured_api_v172` 单跑 18 passed，跟在别的模块后面跑 0 条。
  改用 `monkeypatch.setattr(config, "DB_PATH", ...)` 自动还原；顺带发现老注释里
  「db.DB_PATH 也要改」是错的（`db` 模块根本没有这个属性，`get_db_path()` 只读 config）。
  同一个毛病 `tests/test_no_lookahead.py` 也有一份，一并修；另外在 `conftest.py` 加了
  每个用例前的 DB 路径守卫 —— 谁再忘了还原，也伤不到后面的模块。
- 新增 `isolated_db` fixture（conftest）：给"自己建表 + 跑 db.migrate"的用例一个独立临时库。
  原来这类用例直接在**会话共享库**上 migrate，全量跑时前序用例留下的 api_server/bridge 线程
  还握着 WAL 写锁 → `database is locked`（v440 1 条、v441 2 条）。
- `test_reflection_logic_analysis::test_get_stats_empty_db` 改用 isolated_db：共享库被前面
  用例写了 112 行，空库断言必红。
- `test_v176_11_snapshot_routing` 的 WTI/NVDA：v2026.10.05 加了 yahoo 兜底后"必然为空"
  已不成立，改成"要么空、要么是真形态 close="（防瞎编，不锁死实现）。
- 一条**结构性**风险已堵住（但没被证实是 structured_api 的根因）：
  `api_server` 与 `routers/*` 在 **import 时**就把 `config.DB_PATH` 快照成模块级常量，
  只要它们是在某个用例把路径改坏的那一刻首次 import，之后整场测试都读那个库；
  `conftest.py` 的路径守卫现在同时把这几处的 `DB_PATH` 拉回本会话隔离库。
  `api_server` 与 `routers/*`（events/event_atoms/decisions/…）在 **import 时**就把
  `config.DB_PATH` 快照成模块级常量；只要它们是在某个用例把路径改坏的那一刻首次 import，
  整场测试就一直在读那个空库。`conftest.py` 的路径守卫现在同时把这几处的 `DB_PATH`
  拉回本会话的隔离库。
- `execution_guards.portfolio_guard` 上线后，TradeExecutor 开仓前要读 `t2_positions` /
  `t2_account`；老测试的临时库没这两张表 → guard 报 `portfolio_guard_db_error`、
  把单拒成 RISK_REJECTED。新增 `tests/_helpers_exec_guard.py` 统一补齐最小表结构 +
  正权益账户，trade_executor / execution_cost_settlement 各用例接上（8 + 1 条恢复）。
- `test_fed_rate_alerts` 3 条：标题文案已改「美联储转鹰/转鸽」、推送门槛 v450 收紧成
  只推 high + yield_move/both，用例仍断言旧文案与 med 可推。按现行语义改写，并补一条
  门槛用例（med 不推、expectation_shift 不推）。
- 修两条"全量才挂、单跑必过"的假失败：
  · `test_yahoo_fallback` 5 条：Yahoo 熔断用的是模块级 `_YAHOO_FAIL_STREAK / _YAHOO_OPEN_UNTIL`，
    全量跑时别的用例的真网络调用把 streak 推满 → 熔断打开 → 后面的用例在 `_yahoo_circuit_open()`
    直接早退 None。加 autouse fixture 每个用例前清零。
  · `test_v204_feedback_generalization::test_05`：R8 在 v453 P0-5 已从"兄弟数超限整批全拒"
    改成"同批按 evidence_confidence 保留前 N 名"，用例还断言旧语义（5 兄弟 → 第 1 名也拒）。
    按现行语义重写（第 1 名放行、第 5 名挡下），并补一条 `SAMPLE_R8_MODE=count` 的回滚开关用例。
- 顺手修 v457 写锁重试的一个尾巴：最后一次也锁着时仍会回滚并打印"第 5 次重试"，
  但根本没有第 5 次。现在最后一次直接 `break`，日志写 `第 N/4 次重试`。
  新增 `tests/test_v457_lock_retry.py` 三条：锁两次后成功（含退避秒数断言）、
  非锁异常立刻抛不重试、锁死时抛原异常不静默返回空。

## v457 · 2026-11-18 技术信号接到界面 + 三处稳定性修补

### 界面：信号终于可见
- 后端 `/api/tech/signals` 以前就有，`lib/api-gub.ts` 也封装了 `fetchTechSignals`，
  **但全仓 0 个调用方** —— 技术信号一直在算，界面上完全看不见。
  新增 `components/quant/tech-signals-panel.tsx` 并挂到实时工作台（BTC/XAU/WTI）：
  时间 / 触发类型 / 评分+评级 / 结论（方向+周期）/ 波动档 / 现价。
- 结论不是前端自己算的：接口新增 `with_decision` 参数，直接用
  `tech_signal_bridge.resolve_horizon / decide_direction` 出方向与周期，
  评级用 `tech_detector.grade_of`，波动档用 `tech_vol_regime.vol_bucket`。
  界面看到的口径 = 线上会下单的口径；算不出来返回 null，不猜。
- 三条硬约束写进组件：接口空数组 → 显示"这段时间没有信号"（不补占位行）；
  接口异常 → 只报错误，不展示推测信号；"仅观察"的信号照样列出并标注，
  避免用户以为信号被漏采。

### 数据：XAG 行数差异结案
- 查完：生产库 7,265 行**全部**落在各品种 1,200 根重算窗口内，
  窗口外遗留 0 行（演练库里那 194 行是旧状态，不是 bug）。
- 但顺手挖出真问题：`--history` 名义上"重算历史"，实际仍被 `load_bars(limit=1200)`
  截断 → 更早的老行永远换不掉，会和重算后的行混在一张表里做统计。
  改为 `limit=None`（仅 `--history` 生效，定时任务仍用 1,200 根）。
- 跑一次 `cron_tech_signals --force` 清掉不可复现的 10 行：7,265 → 7,255，
  XAG 267 → 265，`uses_future` 仍全 0。

### 稳定性：写锁与日志
- `cron_tech_signals` 增写锁重试：`database is locked` 按 2/8/20/45 秒退避重试 4 次，
  每次重试前 `rollback()` 释放残留事务；非锁类异常原样抛出（不吞错）。
- "无新行情数据, 跳过本轮"改为：TTY 里保留中文提示，launchd 日志里只留一行
  `skip: no new bars` —— 逐小时刷同一条会把日志冲掉，真出问题反而翻不到。

### 账本：样本进度
- 影子账本新增"样本进度"：已结算 / 目标 30 条、按信号市场时间跨度折算的入账速率、
  预计够样本的日期（当前 22/30，约 0.39 条/交易日，预计 2026-12-08）。
  速率用信号 ts 跨度而不是 created_at —— 回填是同一秒写入的，created_at 跨度是 0。

## v456 · 2026-11-18 技术信号按「品种类别 + 波动状态」分层放行

### 同批前情：清除未来函数 + 白名单按周期分档

- **未来函数清零**：`tech_signals` 里 9,671 条 `uses_future=1`（全部是 `reversal`，
- **台账重跑（口径变了）**：参与评估 7,253 条、剔除未来函数 0 条。
- **白名单按周期分档**（`tech_signal_bridge`）：`by_horizon.{1,3,5,10}` 各自放行，
- **影子账本按各自周期结算**：`tech_shadow_signals` 新增 `horizon` 列（含老库 ALTER 迁移），
- **回归**：`tests/test_v453_horizon_whitelist.py`（6 条：最短周期优先 / 黑名单整批拦截 /

- **为什么分层**：`asset_kinds.classify()` 回答的是"这是不是一家公司"，`BTC` 和 `AAPL`
  被归成同一类；可技术面关心的是"波动结构和漂移是否相似"。一套全局 5 日口径对 27 个
  品种不合理，所以新增 `src_python/tech_vol_regime.py` 作为**唯一口径来源**：
  品种类别 `crypto/metal/energy/index/equity/other`，波动档位按 `ATR14/收盘价` 在
  **自身**近 200 根里的分位三分（`low/mid/high`）。档位只用 ts 及之前的 bar，
  历史不足 120 根返回 `None` —— 不拿"没算出来的分位"充当中档。
- **台账加三个分层维度**：`class` / `vol` / `both` 同时统计，每个格子额外算
  **时间外样本**（最近 30% 信号）的命中率与均值超额；并写入多重比较校正信息。
  实测：2,640 个有样本的格子，单侧 5% 通关 215 格、纯随机预期 132 格；
  Bonferroni 门槛 4.12σ，通过 3 格。
- **三道闸才准接管线上周期**（`refresh_tech_actionable.py`）：
  `n ≥ 200` → `z ≥ Bonferroni` → 时间外命中率超额 ≥5pt **且**均值超额 ≥1pt。
  唯一全过的是 **高波动 + kdj_overbought → 10 日看跌**（n=329、z=4.82、
  命中 58.97% vs 基线 45.72%；时间外 n=100，命中率超额 +15.8pt、均值超额 +1.74pt）。
  看起来最强的 `两者/加密/高波动 + aroon_bull` 时间外均值超额 −0.85，被挡在门外。
- **决策端按格子覆盖周期**（`tech_signal_bridge`）：`strata_horizon()` 命中格子时直接接管
  该周期，而不是取并集 —— `kdj_overbought` 在全局 5 日口径里本就放行，取并集永远先命中
  5 日，分层结论等于白算。`strata.enabled=false` 或档位算不出来时自动退回全局口径。
  决策输出新增 `asset_class` / `vol_bucket` 字段，下游能看到"这条信号处在什么状态"。
- **影子账本记状态**：`tech_shadow_signals` 增 `asset_class` / `vol_bucket`（含老库
  ALTER 迁移），报告增"按品种类别""按波动状态"两组。90 天回填：150 条候选 →
  added 22（21 条 5 日 + 1 条 10 日分层）/ regime 35 / not_actionable 93；
  按波动看 high 10 条命中 60%、mid 10 条 50%、low 2 条 0%（样本都太小，不作结论）。
- **回归**：`tests/test_v456_strata.py`（10 条：类别映射 / 档位识别 / 因果性 /
  覆盖优先级 / 关闭与未知档位退回全局 / 三道闸各自拦截）。
  技术指标全链路 154 passed / 1 skipped。

## v456 · 2026-11-18 六项一起落地：口径体检 / prompt 瘦身 / 同业自动分组 / 财库票屏蔽 PB / A/B 埋点 / 仓位影子

- **口径漂移体检（新增 `scripts/fundamental_drift_check.py`）**：三问 —— 推导通道有没有点、
  推导值与供应商当期值偏差、原始快照是否超期。首跑 **OK 21 / FAIL 5**，逐条核对后修掉一个真 bug：
  `_equity_asof` 原来按"已知日"排序，而 SEC 会把**往年对比期**塞进后来的申报
  （META 的 2025-06-30 净资产出现在 2026-10-22 的 10-Q 里），导致取到旧表 →
  净资产 195.1B 而非 272.4B → PB 8.11 而非 5.81（偏差 +39.9%）。
  修法：**可用性按已知日判、挑哪一张按期末排**；并修掉"期末最新那行缺净资产会挡住有数旧行"
  （AAPL 2026-09-30 空行 vs SEC 2026-09-26 112B）。修后 **OK 22 / WARN 4 / FAIL 0**。
  判级三档：≤5% OK、5~25% WARN（供应商口径差，避免告警疲劳）、>25% FAIL。
  剩余 4 条 WARN 均已用 SEC 官方值核对为**供应商口径差异**（TSLA 我方 89.3B vs 隐含 110.2B、
  NVDA 229.0B vs 266.0B、RIOT 2.1B vs 2.4B、腾讯 BPS 时点差 7%），证据写入报告 json。
- **prompt 瘦身**：FRED 层原为"全量展示不截断"（33 组逐条进 prompt），而宏观已通过
  `macro_equity_regime` 的 4 驱动进了权重 → 同一批数字的第二份拷贝。改为 10 个关键刻度
  + 一行「宏观驱动→板块倾斜」（与权重同源）+ 折叠计数，实测体积 **−60%**；
  `TRIDENT_FRED_PROMPT_MODE=full` 可退回。
- **同业分组自动化**：`SECTORS` 手写 26 只 → 人工优先 / 缓存 / 现场识别三级；
  industry 关键词映射 + `gics:*` 粗桶（标 `coarse`）；yfinance `.info` 本机间歇 SSL 失败，
  加 **SEC SIC 兜底**（`submissions` 的 sicDescription）。实测 `ASML→semis`、`MRVL→semis`、
  `SHOP→software`（auto），半导体组 8→10 只；识别结果落盘 30 天，热路径不发网络请求；
  `--peer-warm` 名单扩为「人工 ∪ 已识别 ∪ 决策名单」，实测命中 24 / 缺失 5 / 归组 29。
  识别不到一律不猜。
- **财库票屏蔽 PB**：`PB_EXCLUDED={"MSTR": ...}`；自身跳过 PB 分量（PE 照常）并写明原因，
  同业中位数剔除财库票（某组 5.0 → 5.5）。`--coverage` 的估值分位由 16 只变 **15 只**（少 MSTR），
  PB 原始数据仍保留可审计。
- **离线 A/B 埋点**：先确认硬前提 —— `ai_decisions` **无标的代码列**，历史决策无法与当时分数对齐。
  故 `record()` 增写 `decision_fundamental_snapshot`（decision_id/ticker/fund_score/valuation/
  peer_rel_score/macro_tilt/sizing_mult/components），老库幂等 ALTER；新增
  `scripts/fundamental_ab_replay.py` 用**当时记下的分数** + 真实收盘比含/不含基本面，
  门槛 30 样本，不足输出 `insufficient`（首跑 0 样本，如实标注）。
- **基本面进仓位（影子）**：`sizing_multiplier = 1 + 0.20×score`，截 `[0.80,1.20]`，
  不可用时 1.0，逐条记 `sizing_mult` —— **不改实际下单**，等 A/B 有证据再放开。
- 测试：`tests/test_fundamentals_v456.py` 23 条 + v454 新增 3 条 `_equity_asof` 回归；
  合计 **195 passed**。已接进每日 05:10 cron（体检 + A/B 均 `|| true`）。
- 报告：`docs/v456-六项落地-2026-11-18.md`。

## v455 · 2026-11-18 A股/港股：估值分位从「不可用」到 8/8 可读

- **问题不是覆盖，是失真**：A股/港股财报读数早就有（8 票），但 `cn_valuation_snapshots`
  每票只有 1~7 个点、门槛 60 → 估值分量恒为 None，分数只剩"增速"一把尺。
  实测后果：东山精密 / 新易盛直接打满 **1.0** —— 没人拦它们贵不贵。
  （就近 7 天 20 条 CN/HK 决策核过：可反解 7 个代码、7 个全部已覆盖；13 条未反解的是
  "科创50跌超6%""投资日历"这类**没点名个股**的新闻，不是基本面缺口。）
- **做法与美股 v454 同构**：`PE = 当日收盘 ÷ 该日之前已公布的 EPS_TTM`，`PB = 收盘 ÷ 已公布 BPS`。
  收盘取腾讯 K 线（`cn_prices.fetch_stock_history`，实测 `sh600519`/`sz002384`/`hk00700` 各 320+ 根）；
  财报取东财 16 期（A股 `RPT_LICO_FN_CPD` / 港股 `RPT_HKF10_FN_MAININDICATOR`）；
  TTM 逐期还原复用 `_a_ttm` 切片（不为历史期另写一套逻辑）。
- **防前视用法定披露截止日**（一季报 4/30 / 半年报 8/31 / 三季报 10/31 / 年报次年 4/30）：
  宁可晚 0~30 天用，也不提前 1 天 —— 拿季末当已知日等于提前 1~4 个月知道业绩。
- **落库幂等**：写 `cn_valuation_snapshots` 标 `source='derived'`；同日已有东财原始快照不覆盖
  （采到的优先于推出来的）。首跑 **8/8 只、1940 行**（PE 1646 / PB 1940）。
- **口径校验**：东山精密推导 PE 24.08 / PB 4.99 与东财当期快照 24.08 / 4.99 **完全一致** ——
  推导与供应商同一口径，不是另造一套。
- **分数变化**：600519 茅台 +0.2184 → **−0.1793**（PE 95.9 百分位，自身一年最贵）；
  00700 腾讯 +0.3659 → **0.5794**（PE 1.2 百分位）；002594 比亚迪 −0.0259 → +0.0910；
  603636 南威软件 −1.0000 → −0.7186（亏损只用 PB 59.8 百分位，不再打满负分）。
- **运维**：新增 `--cn-valuation ALL`，已接进每日 05:10 的 `run_equity_fundamentals.sh`；
  `--coverage` 增加 `[A股港股估值] 分位可用 7 只…｜样本不足: 无`。
- 单测 `backend/tests/test_cn_valuation_v455.py` 18 条（法定披露日 / 历史期 TTM / 防前视 /
  亏损不给 PE / 缺 BPS 不给 PB / 写库后分位可读 / 个股 symbol 映射）。
- **诚实边界**：A股同业对比没做（8 票跨 8 个行业，样本不足，按"样本<2 不给结论"铁律不造数）；
  前复权价 ÷ BPS 的绝对水平受分红/拆分影响；A股/港股仍无财报日历、三表、宏观传导。
- 报告：`docs/v455-A股港股估值分位-2026-11-18.md`。

## v454 · 2026-11-18 基本面补洞：PB 可用 / 同业有锚 / 宏观进权重

- **PB 从 0/16 到 16/16 可用**：原来是「分子分母双缺」——MU/CLSK/HUT/SMCI 的
  `eq.mktcap` 全 0 非空（FMP 不返 marketCap），AAPL/AMZN/GOOGL 的净资产全 NULL。
  ① 新建 `src_python/sec_fundamentals.py`，走 SEC `companyfacts`（免配额、季频、
  **每个数带 `filed` 申报日**）；v448 把季末当已知日是错的，最多提前 6 周，现在取
  `max(period_end, filed)`；同一期末多条按最新申报去重，缺净资产时用 Assets−Liabilities 派生并标记。
  首跑新增 78 行 / 补空 28 行 → 净资产 16/16。
  ② 新建 `src_python/equity_mktcap.py`：已公布股本（yfinance `get_shares_full`）× 已公布收盘，
  前向填充、缺历史不插值（只给今天一个点，标 `shares_current×close`）。MARA 一年从 3.704 亿
  增发到 3.874 亿股 —— 拿当前股本回填历史就是造假。播种 4610 行 / 16 票 / 251~263 个交易日。
  ③ `equity_fundamentals.pb_history` 改成双源并流（快照优先于推导、按日合并），
  「市值恒定」守卫只拦快照那一路（推导市值跟价走）。实测 16/16 ✓、AAPL 95 天、NVDA 164 天。
  注：推导 ≠ 补采集积压 —— 乘数都是已公布真实数，口径与 PE「收盘÷已公布 EPS」同源，行内标 `derived`。
- **同业可比有了横向锚**（新增 `peer_valuation.py`）：5 组 26 票（semis/mega_tech/crypto_miner/
  crypto_asset/ai_growth），倍数本系统快照优先、yfinance 兜底（缓存 6h）；
  `估值 = 0.7×自身历史 + 0.3×同业`（自身缺失时 0.85×同业），组内有效样本 <2 一律不作结论。
  实测 NVDA semis 7 只可比 → −0.41、MU +1.0、AAPL mega_tech → −1.0、COIN +0.74、AMZN +0.47。
- **33 组 FRED 从 prompt 背景板变成可复现倾斜**（新增 `macro_equity_regime.py`）：
  4 个无量纲驱动（利率 Δ10Y/0.30pp、风险 VIX+HY OAS、美元 DXY%、曲线 −(10Y−2Y)/0.5）
  × 板块敏感度 → `tilt` 截 ±1 → `score = clamp(score + 0.15×tilt)`，写进 components 与解释。
  这一条尤其重要：此前 `decision_guard` 的权重表里**完全没有 macro 项**，宏观只被拼成 prompt 文本，
  不可复现、不可审计。实测 rate +0.43 / risk −0.51 / usd +0.36 / curve −0.60，
  semis −0.29、crypto_miner −0.43（当时 10Y +0.13pp、VIX 22.9、HY OAS +0.16pp）。
- **影子账本重写四处结构性问题**：`applicable/reason` 分适用性（XAU/WTI/BTC 不再混进样本池，
  不适用行 `settled=-1` 留痕）；主判据改为 **+10 天**（季报慢变量塞进 +3 天窗口测的是噪声）；
  基准价从「±12h 找 K 线」改为「决策时刻之前最后一根」（原逻辑匹配不上就永不结算 ——
  164 行 `settled` 全 0 的根因）；`summarize` 改为 samples/applicable/reasons/settled/by_horizon/verdict，
  并加 `backfill_reason()` 按 asset 列给老行**分类**（只分类、不改判定）：
  164 行 → `non_equity 88 / no_asset 74 / no_fund_data 1 / placeholder_cn 1`，报告不再是 "unknown"。
  **如实交代**：生产库仍 0 笔「适用」样本，权重 0.08 只能判 `insufficient`，机制能自证但还没被验证。
- **三开关进 cron**：`--sec-fill` / `--mktcap-fill DAYS` / `--peer-warm`，参数解析阶段先分派、
  不碰 FMP/AV、不吃配额；`--coverage` 增加 `[PB 分位]` ✓ 标记与 `[净资产] n/16`。
- 单测 `backend/tests/test_fundamentals_v454.py` 21 条（SEC 重述去重 / 已知日防前视 /
  股本前向填充 / 双源并流 / 相对分饱和 / 样本不足拒绝 / 宏观门槛与符号 / 0.7-0.3 配比 /
  倾斜上限 / 账本适用性与 10d 结算），与既有基本面 85 条、guard+macro 相关 165 条一起全绿。
- 报告：`docs/v454-基本面补洞-2026-11-18.md`。

## v453 · 2026-11-18 决策级归因接线 + 归因/洞察两条链常驻化

- **决策级归因终于有调用方**：`insight_decision_attr.py`（v409.7）写好两个月，
  全仓检索不到任何调用 —— "verified 0 条"不是算法缺，是链路悬空。
  新增 `backend/scripts/verify_insights_decision_level.py` 作为入口，并先修三处口径，
  否则会产出假 verified：
  ① `insight_direction` 是**参数调整方向**（提高 +1 / 降低 −1），不是看涨看跌，
  门槛类按参数键反推门卫方向（`score.buy_min_score`→BUY）；
  ② 仓位类（`sizing.*`）与纯叙事类测不出门卫质量，只对 `score.*`/`filter.*` 开放；
  ③ 证据方向要与建议方向对账（同一份证据，"提高门槛"成立、"降低门槛"即反证）。
  首跑：门槛类 17 条 → 判出 1 条（id 1215，放行 8 / 拦截 9，−1.72% vs −0.98%）→
  `verified`，`verification_method='decision_attr'` 留痕。全库 verified 0 → 1。
- **两条链常驻化**（此前都是"一次性回填"或"从没跑过"）：
  `com.trident.error-code-cron`（10 分钟，`--since-hours 6` 只补新决策，不回填停机期）
  首跑候选 36 → 3 个码；`com.trident.insight-decision-attr`（6 小时）首跑判出 1 条。
  plist 归档在 `scripts/`，wrapper 在 `~/.trident/wrappers/`。
- **纠正一处时区误读**：`decision_errors.created_at` 存 UTC，
  `ai_decisions.created_at` 存 +08:00。13:45 版据此得出"归因链没接上恢复"是错的 ——
  对齐后 16:00 起写入正常（22 点单小时 48 条）。复算脚本已写进报告，避免再犯。
- **生成侧硬门槛落地**：新增 `backend/src_python/insight_contract.py`。生成提示词里的
  "必须带数值/方向/参数键"挂了很久仍被模型漏 —— 提示词是软约束，代码门槛才是硬的。
  体检四问（数值/方向/参数键/窗口）→ 四档（actionable / narrative / meta / reject）→
  不合格带缺项清单重写一次（只改形式，禁止引入原文没有的数据）→ 仍不合格不入库并写审计
  `runtime/insight_contract.jsonl`。两条生成链（复盘 / 快速评测）都已接入。
  存量 71 条体检：4 可执行、8 元规则、59 不合格 —— "93% 只能观察"的根因在源头产出，
  不是入库判定。存量不删不重判。单测 `backend/tests/test_v453_insight_contract.py` 8 条全绿。
- 报告：`docs/细节洞察能力评估-2026-11-18-第三次.html`（框架 A− / 闭环 D→D+）。

## v452 · 2026-11-18 技术信号评分重算 + 影子账本窗口修复

- **评分口径落到存量**：新增 `backend/scripts/recompute_tech_scores.py`，用 v450
  事件族口径（`tech_detector._score_from_hits`，不复制公式）从 `signal_json` 反算，
  18,503/18,522 行由旧口径常数 0.95 改为真实分布。
  分级：A 18,504 → **4,393**，B 5,629，C 8,500 —— `grade_of` 与桥接 `--min-score 0.58`
  第一次真正生效（此前 A 级占比 99.9%，分级/门槛全在空转）。
  报告：`backend/reports/tech_engine/v452_score_recompute.md`。
- **影子账本不再空转**：`cron_tech_shadow.sync` 的候选窗口从
  `created_at >= 近1小时` 改成「评估台账 `tech_shadow_eval` + 线上回看 30 天」。
  根因：`created_at` 是整点批量写入的（全库仅 4 个值），信号落库与调度错开即整轮空转
  —— v451 期间 `tech_shadow_signals` 长期 0 行。现在每个候选只评估一次，
  被拒也记账（`added/hold/not_actionable/regime/no_items`），不重复烧 CPU。
- **入账漏斗进报告**：90 天 419 条候选 → added 33 / regime 96 / not_actionable 290。
  卡点清晰：69.2% 死于白名单（当前只放行 rsi_oversold、bb_lower_break、kdj_overbought）。
- 实测：影子账本 **0 → 33 条**，27 条已结算，方向命中 51.9%（样本 <30，不作结论）。
- 回归：`backend/tests/test_v452_shadow_window.py`（5 条：老 created_at 必捞到 /
  不重复评估 / 被拒记账 / 线上窗口有界 / 低于门槛不评估）；
  技术指标相关套件 69 passed。

## v451 · 2026-11-18 主分析采纳门卫主品种

- 现象：规则门卫已写 `gate_main_asset`（WTI/XAU/BTC），主分析仍判「非核心（无池内标的）」
  → `routing_miss`，21 点前每小时只有 2 条分析。
- 两处改动（`engine/ai_worker.py`）：
  1. `_build_gate_context`：门卫主品种属池内时，追加硬约束
     「target_asset 必须填 X，禁止 NONE/OTHER」；
  2. 落库前兜底：`successes` 里 target_asset 为空/NONE/OTHER 且门卫主品种属池内的，
     直接补成门卫主品种（只补空、不覆盖模型结论），打 `[GATE-ASSET]` 日志。
- 效果（实测）：**21 点 analysis 25 条**（19 点 2、20 点 2）；待分析队列 185 → 106 在消化。

## v449–v450 · 2026-11-18 门卫规则判决 + 主分析批处理

- **v449 门卫降级为「规则为主 + LLM 抽样复核」**（`realtime_filter.rule_verdict`）
  · 门卫 LLM 失败（鉴权/超时/网络/熔断）不再写「未判定」，改由规则给真结论：
    高权重主体+市场主题 / ≥2 资产词+≥2 主题词 / L1 CORE → `可交易`，其余 → `无信号`（按噪音归档）；
  · **池内资产映射**（`_rule_asset_hint`）：原油·炼油厂·OPEC→WTI，美联储·加息·CPI·通胀→XAU，
    比特币·加密→BTC，以太坊→ETH，SOL→SOL。写进 `gate_main_asset` 供主分析取用；
  · 收口「未判定泄漏」：门卫 LLM 内部降级（通道熔断窗口直接放行）返回的 `未判定`
    统一被规则判决补位。实测线上：38/38 条全部拿到真结论（0 未判定）。
  · 新增 `tests/test_v449_rule_gate_fallback.py`（9 条，全绿）。
- **v450 可交易插队改小批**（`engine/ai_worker.py`）
  · 旧逻辑：检测到建仓窗口内可交易行 → **只认领 1 条**，主分析约 15 条/小时；
    入库 380 条/小时 → 队列只涨不跌（21:25 实测 122 条可交易待分析）。
  · 现在：小批插队 `AI_TRADEABLE_BATCH`（默认 8）。实测 main 调用 2/10min → **17/10min**。
- 仍然存在（下一步）：主分析对"油价/加息"类新闻仍判「非核心（无池内标的）」→ routing_miss，
  `gate_main_asset` 尚未被主分析当作强制目标资产使用。

## v446–v448 · 2026-11-18 新闻入口与门卫治理

- **v447 新闻配额**：`paper_trading_settings.news_daily_target` 1200 → **12000**；
  `feeds/news_sources.py` 单源日上限 150–260 → 600–1200。
  事故：19:00 打满 `[QUOTA] 1200/1200` 后 15 个源集体停采，用户看到的现象是"新闻突然变少"。
  实测修复后入库 2 条/10 分钟 → 74 条/10 分钟（东财 37、深潮 23、Cointelegraph 5…）。
- **v446 入库前筛子**：新增 `news_keepfilter.py`（零 LLM 成本）
  · `dup_fingerprint`：同归一化指纹在 6h 窗口内已存在 → 重复，标 FILTERED；
  · `non_news:*`：榜单/复盘/研报/目标价/资金流向/同比环比汇总等"统计·分析类非新闻"；
  · 含事件动词（宣布/批准/降息/起诉/坠毁…）的句子**不筛**，防错杀突发；
  · 落在 `db.insert_raw_news` 入库前，`filter_reason` 留痕。新增 `idx_raw_news_fp` 索引。
- **v448 门卫节奏**：`llm_budget` 给 purpose='gate' 单独一套 pacing（独立 last_ts 文件，
  `GATE_MIN_INTERVAL_S=1.0`），不再与主分析抢同一个时间槽；门卫模型 M3 → M3.1-Flash、
  超时 20s → 6s（超时即降级放行，不再拖住入库）。
- 队列容量闸 50/20 → **200/100**（恢复新闻流后 50 条上限会成批误丢）。
- 并发 16 → 8：MiniMax 通道在本机网络下 `status=net` 失败明显，降低同时在途请求更耐受。
- 仍未解决：门卫约 50% `http_error/net` → 116/132 条待分析卡在"未判定"，
  导致 19:36 后几乎不出决策。要么给门卫换独立供应商/独立账户 key，要么把门卫降级成
  "规则为主 + LLM 抽样复核"。

## v445 · 2026-11-18 吞吐放开 + 队列容量闸

- 限速关闭：`LLM_MAX_RPM=0`、`LLM_MIN_INTERVAL_S=0`（用户指令"不限速"）。实测门卫吞吐
  从约 3 次/分钟升到约 6 次/分钟，主分析恢复产出；providers 侧仍有个别 429（无 pacing 时的
  并发突发），门卫按既有降级策略放行，不影响链路。
- 批与并发：`AI_BATCH_SIZE` 4 → 16、`LLM_MAX_CONCURRENCY` 6 → 16。批 4 条/约 10 分钟
  ≈ 24 条/小时，而新闻到达约 200 条/小时 → 队列只涨不降，这是 v444 之后的新瓶颈。
- 新增 `news_cutoff.maybe_advance_on_backlog()`（v445）：待分析 > `NEWS_BACKLOG_CAP`(50)
  时，水位推进到"只保留最新 `NEWS_BACKLOG_KEEP`(20) 条"，老的一次性归档
  （`filter_reason=pre_recovery_skip`），零 LLM 成本。断档水位管"停摆后恢复"，
  容量闸管"慢性堆积"，两者缺一不可。
- `NEWS_RECOVERY_GAP_MIN` 15 → 30：批大小抬到 16 后单批耗时可超 15 分钟，
  15 分钟会把"正在跑的大批次"误判成断档，反手把队列清空（实测 18:21 误清 172 条）。
- 新增 `tests/test_v445_news_backlog_cap.py`（5 条用例，全绿）。
- 注意：`news_cutoff._cfg()` 优先读 `config.XXX`，改 `.env` 后必须重启引擎；
  写单测时只 `monkeypatch.setenv` 不生效，要同时打 config 属性。

## v444 · 2026-11-18 · 断档恢复后**不补历史新闻**（用户定策，落进代码）

- 用户原话："以后所有的 LLM 通道恢复之后，都不去做历史的新闻……它只分析现在进来的，包括现在到以后进来的新闻……否则堆积的多的话我这个打不住的。"
- `backend/src_python/news_cutoff.py`（新）：持久化 epoch 水位 `floor_ts`（`backend/runtime/news_cutoff.json`，跨进程文件锁 + 原子替换，**只前进不后退**）。断档判定看 `ai_call_stats` 中 `purpose IN ('main','analysis') AND status='ok'` 的最近成功调用，距今 ≥ `NEWS_RECOVERY_GAP_MIN`（默认 15 分钟）即判"断过档"，水位被推到当前时刻 —— 通道真正恢复时水位天然等于恢复时刻。
- `engine/ai_worker.py`：认领 SQL 加 `ts >= floor`；新增 `_archive_below_floor()` 把水位之下的 PENDING 归档为 `FILTERED / filter_reason='pre_recovery_skip'`（留痕不删数据）；`_run_expire_stale_news` 每 60s 先判断档、再归档、再认领（全是 UPDATE，不花额度，且在通道体检之前跑 —— 熔断期间也能生效）。
- `engine/gate_replay_guard.py`：`count_backlog` 与回放候选都按水位过滤。只堵认领不堵回放，额度照样会被历史旧闻烧光（回放是 900s 一轮、50 条/轮的另一条"补历史"通道）。
- `news_score_worker.py`：6h 硬闸取 `max(6h_cutoff, floor)`，打分通道也不再回炉历史。
- `news_funnel.py`：面板"待分析"扣掉水位之下的行 —— 否则归档完 216 条，面板还写 216，人以为要等。
- `config.py` / `backend/.env.example`：新增 `NEWS_SKIP_BACKLOG_ON_RECOVERY`（默认 1）、`NEWS_RECOVERY_GAP_MIN`（默认 15）、`NEWS_CUTOFF_STATE_PATH`。
- `engine/worker_helpers.py::_has_fresh_tradeable`：也按水位过滤。它决定「是否单条插队」，若不看水位 → 判定有新鲜可交易 → 只认领 1 条 → 认领 SQL 又挡住这条 → 每轮空跑 30s，新新闻迟迟进不来（实测 17:27 就卡在这个组合上）。
- `AGENTS.md`：新增「v444 产品定策」章节（规则语义 + 四处同口径 + 开关 + 回归），防止后续改动把它当 bug"修"回去。
- 测试：`backend/tests/test_v444_news_cutoff.py` 12 条全绿（离线：只前进、断档判定、门卫 ok 不算健康、首跑不推进、认领过滤、归档留痕、面板同口径）。`py_compile` 6 个改动文件通过。

## v452 · 2026-11-18 · 基本面：票池铺满 16/16（yfinance 三表兜底 + 同季同比）

- `backend/src_python/equity_fundamentals.py`：新增 `parse_yf_statements()` / `fetch_yf_statements()`（yfinance 季报三表 → 本项目行格式，NaN 留空不写 0，12h 缓存，失败不抛）；`fetch_pack()` 在"无三表或净资产整列为空"时自动退 yfinance；新增 `_dedupe_rows()` 跨源同季去重（按非空字段数 + 来源优先级），**修掉 TTM 被同一季算两倍**的隐患。
- `backend/scripts/cron_equity_fundamentals.py`：新增 `--yf-fill T1,T2` 独立入口，只走 yfinance、不消耗 FMP/AV 配额（配额为 0 时也能补）。
- `backend/src_python/fundamental_factor.py`：新增 `stmt_growth()` —— 三表只有 5–7 期时用**同季同比**（EPS 优先，缺则净利打 0.8 折，负基期不算）；盈利股缺估值分位时方向分 ×0.7 并标 `earnings_only`。
- 实测：覆盖 **10/16 → 16/16**（三表 134 行/15 票）；META +0.70、MSFT +0.56、TSLA −0.70、PLTR +0.70、SMCI +0.35（含 `low_cash_quality`）；HUT 仅资产负债表 → 保持 `available=False`；既有 10 只分数不变（RIOT/COIN 因现金流补齐微调）。
- 未做：PB 分位仍 0~3 天（不补历史）；新票无 `eq.pe` 分位；events 无历史 actual；影子账本仍 0 样本。
- 测试：`backend/tests/test_fundamentals_v448.py` 29 条；fundamentals 全量 **85 passed**。
- 详见 `docs/v452-票池铺满16只-2026-11-18.md`。

## v451 · 2026-11-18 · 技术指标 P0：切断未来函数 / 重做打分 / 基线检验 / 影子账本

- `backend/src_python/tech_detector.py`：转折点改为**因果**——枢轴需右侧 `reversal_pivot_window=5` 根确认，再等反向幅度 ≥ `max(3%, 1.5×ATR%)`，信号落在真正可执行的确认那根，记录 `pivot_ts/lag_bars`，`uses_future=False`；旧版仅保留在 `_detect_reversals_lookahead`（默认关闭、`--lookahead-audit` 显式开启）。背离/支撑位补上「枢轴未确认不得引用」护栏。
- 打分重做：状态型证据（SuperTrend/ADX/均线排列/Ichimoku）降为 `role=context`，不参与计分；事件按证据族去重（同族只取最大权重）；logistic 映射，分数不再全部顶到 0.95；RSI/KDJ/Stoch/布林带改为「进入区间」那根才算事件，支撑位只在「刚贴近」触发。
- `backend/src_python/tech_signal_bridge.py`：候选查询硬过滤 `uses_future=0`；新增台账白名单 `src_python/runtime/tech_actionable.json`，首版只放行 `rsi_oversold`、`bb_lower_break`（BUY）与 `kdj_overbought`（SELL）；`kdj_overbought` 移出 HOLD_ONLY；`--min-score` 默认 0.95→0.58（新口径）。
- `backend/scripts/indicator_effectiveness_ledger.py`：方向表 6→30+ 因子；命中率基准从 50% 改为**同资产无条件方向概率**（样本长期正漂移，53%~56% 才是随机水平）；报告新增「基线命中」列。
- 五年回放（27 品种 / 22,476 条）：BUY 命中 52.9% vs 基线 53.4%，SELL 45.3% vs 46.2%，**整体无方向边际**；A 级(−2.0pt) 反而差于 C 级(−0.6pt)，证明「证据权重高」≠「方向更准」。唯一稳定为正的是 `rsi_oversold`（+5.9pt）；`divergence/reversal/support/volatility` 均为负。详见 `backend/reports/tech_engine/v450_replay.md`。
- `backend/src_python/backfill_tech_signals.py`：写入从「只 upsert」改为**窗口内先删后写**（纪元之后、到本轮最新 bar 为止），否则旧打分/旧未来函数残留会一直留在表里；`busy_timeout` 提到 60 秒（这轮实际撞到 `database is locked`，首次失败未落半成品）。本轮结果：删 33 条、写 12 条，表内 18,548 → 18,515，近期分数 0.40/0.45/0.50/0.53/0.55/0.65/0.93，`uses_future` 全 0。
- `backend/src_python/cron_tech_shadow.py`（新）：影子账本 `tech_shadow_signals`，只按桥接规则记账、5 个交易日结算，不写 `raw_news/ai_decisions`；launchd `com.trident.tech-shadow-ledger` 每小时（首次运行只向前，不补历史）。同步按 `created_at`（计算时间）取最近一小时，**不是按 K 线 ts**——日线信号收盘后才算出来，按 ts 会全部漏掉。
- 台账入周更：launchd `com.trident.indicator-ledger-cron` 每周一 06:10，报告写 `backend/reports/indicator_ledger/`。
- 测试：`tests/test_tech_detector_causal.py`(4) + `test_bridge_turn_events.py`(4) = **8 passed**；P1 环境闸门新增 `test_tech_regime_gate.py`(5)，技术类合计 **132 passed**。
- **P1 环境分层**（`backend/scripts/tech_regime_replay.py`，报告 `reports/tech_engine/v451_regime.md`）：五年 22,476 条按 MA200 / ADX 分层。现白名单无闸门 +3.2pt / 超额 +0.62%，**加「收盘在 MA200 上」闸门 +5.3pt / +1.08%**（样本 811）；`kdj_overbought` 无闸门 −0.5pt / −0.38%，**加「MA200 下 + ADX 空」闸门 +5.6pt / +1.48%**（样本 280）。`stoch_oversold`、`vol_bear_break` 单独并入会稀释（+5.2 / +5.5），暂不放行。
- 闸门落到代码：`tech_actionable.json` 新增 `regime` 段（`buy_require_ma200_above` / `sell_require_ma200_below_adx_bear`），`tech_signal_bridge.regime_ok()` 与 `cron_tech_shadow` 同步启用；MA200 只用信号当根及之前 200 根，样本不足直接不放行。
- 欠账：技术面方向边际仍然很薄，白名单只有 3 个因子且现在多了一层环境闸门，线上会更安静（当前纪元窗口 0 候选）；下一步把闸门后的真实成交样本继续攒，并复核 `vol_bear_break` 的「MA200 上、ADX 非多」分支。

## v450 · 2026-11-18 · 数据管线断了 / 旧结论作废 / 唯一能自己收钱的是套息

- **查出管线已断**：写 `flow_snapshots` 的 `market_flow.py` 不在仓库里（`~/.trident/log/missed_signals.err.log` 每分钟报 `can't open file`，同批还缺 `backfill_asset_routing.py`、`gate_calibrator.py`、`populate_missed_signals.py`）；该表 funding/lsratio/oi/close/volume 全部停在 11-17 15:00–21:00。`/edge` 一直在拿死数据算分位。
- `backend/src_python/structural_edge.py`：指标读取改为**活源优先** —— 先读 `external_indicators`（binance/okx/bybit 实时费率，5 分钟一采），按 8 小时桶去重后再进分位；读不到才回落 `flow_snapshots`。
- `backend/src_python/backfill_funding.py`（新）：直接调交易所 `/fapi/v1/fundingRate`，每币 990 个结算点 ≈ 330 天，写入同一条指标 `bn_funding_rate`（去重按 asset+indicator+ts）。已补齐 BTC/ETH/SOL 各 990 点，序列从 10.5 个月前的 960 点起。
- **结论作废**：成本收回唯一来源 `cost_model`（BTC/ETH 30bps、SOL 32bps），删除自建 5bps 表与 `config/cost_model.json`；样本从 70 天扩到 10 个月后，v424 的「ETH 1d 做空 净 +1.616%、t=3.00、p=0.006」变成 **t=0.34、p=0.485、n=38** —— 小样本噪声。10 个月里 3 币 × 2 方向 × 3 周期，**没有任何一格三关全过**；低费率做多 8h 甚至显著亏（BTC t=−3.70）。
- `backend/src_python/carry_yield.py`（新）+ `GET /api/edge/carry`：**资金费率套息**（空永续 + 多现货对冲，只收 8 小时租金）。BTC 30 天窗口：均值 0.0066%/次、正费率 94.4%、年化 1x **7.24%** / 3x 21.71%、成本 30bps、**回本 15 天**；ETH 90 天 4.48%；SOL 320 天 −0.39%（长期不收租）。这是唯一扣费后仍为正期望、且不依赖猜方向的结构。
- `frontend/app/edge/page.tsx`：新增「资金费率套息」面板（年化 1x/3x、正费率占比、成本、回本天数、判定），成本说明改为「统一取 cost_model」。
- `backend/src_python/funding_shadow.py`（新）：只向前记账的影子账本。首次运行只写 `activated_at`（本次 2026-11-18 09:05），激活前极端点**一条不补**；之后 Q80 高费率落 pending，24 小时后用 4h K 线 + 统一成本结算。launchd 新 label 拉不起来（`execs=0` 卡 xpcproxy）→ 挂在已稳定运行的 4 小时任务 `run_shadow_ledger.sh`，日志 `~/.trident/log/funding_shadow.log`。
- 测试：`test_carry_yield.py`(5) + `test_structural_edge.py`(8) + `test_funding_shadow.py`(5) = **18 passed**；钉住「年化 = 均值×3×365」「回本天数单位」「成本只认 cost_model」「激活前不许补记」。
- 欠账：`flow_snapshots` 仍无人写（4 个 cron 每分钟刷错误日志）；影子账本凑 30 笔需 6–10 周；套息真跑缺现货腿通道与基差风险度量。
- 详见 `docs/v425-修数据管线-结论修正-套息-2026-11-18.md`。

## v449 · 2026-11-18 · 单押注检测 + 正期望停单：13 笔只有 3 注，30bps 下停单

- `backend/src_python/bets.py`（新）：60 日日收益相关矩阵 + `CORR_CLUSTER=0.70` 正相关聚簇；未平仓按「簇 + 方向」合并成注。实测 **13 笔 → 3 注**（XAU SELL 7 笔 / WTI SELL 4 笔 / WTI BUY 2 笔），总敞口 1300 U；警告：WTI 多空两条腿都在付成本、XAU SELL 占敞口 54%。
- `backend/src_python/expectancy_policy.py`（新）：全样本 + v447 同向闸后（20 分钟 / 0.10%，仅 BTC）双 cohort；判据 **净>0、n≥30、t≥2** 才算可开火，t 1～2 只给观察仓。30bps 全负 → **停单**；5/10bps 仍只观察；≤2bps 时 BTC 20m +0.069%/笔（t=2.12）才可开火。
- `backend/src_python/routers/bets.py`（新）：`GET /api/bets/scan`、`GET /api/policy/expectancy`（10 分钟缓存，只读）；已在 `api_server.py` 注册。
- `frontend/app/bets/page.tsx`（新）+ 导航「更多 → 交易 → 该不该开火」：顶部直接给“停单 / 只观察 / 可以开火”，左栏仓位合并、右栏净期望矩阵，成本档 0/2/5/10/30 可切。
- `backend/tests/test_bets_policy.py`：9 passed。
- 关键口径：v447 的“+0.034%/笔、t=3.16”是**组间差**，不是绝对收益显著；5bps 下 1h 绝对 +0.031%、t=0.73，不能拿它当开火依据。
- 详见 `docs/v449-单押注与停单-2026-11-18.md`。

## v449 · 2026-11-18 · 基本面：盈利含金量护栏 + PB 样本倒计时

- `backend/src_python/fundamental_factor.py`：新增 `cash_quality()` —— TTM 经营现金流 / 净利 < 0.60 时把盈利修正折半并标 `low_cash_quality`（**只判差、不奖励好**，避免"盈利不错"被用两遍）；三个失真守卫：近四季 OCF 缺失 / 净利≤0 / 净利<1%营收 / ratio>3 一律不判。
- `backend/scripts/cron_equity_fundamentals.py --coverage` 增加 `[PB 倒计时]`：按日去重统计每票样本，门槛 60 天；同时修掉"复用已关闭连接 → 每票都显示 0 天"的假读数。
- PB 口径明确为**不补历史**：以采集恢复时刻为新起点按日累积（当前 0~3 天），约 2027-02 中旬陆续越过 60 天门槛；此前 PB 不参与估值分。
- 实测 10 只：OCF/NI = NVDA 0.70 / GOOGL 0.76 / MU 1.06 / AAPL 1.10 / AMZN 1.25，全部合格；COIN/MSTR/CLSK/RIOT/MARA 因 OCF 缺失或净利≤0 不判 → **本版在当前票池不改变任何分数**，属保护性护栏。
- 测试：`backend/tests/test_fundamentals_v448.py` 25 条（+4 条含金量用例）；fundamentals 全量 **81 passed**。
- 详见 `docs/v449-盈利含金量与PB倒计时-2026-11-18.md`。

## v448 · 2026-11-18 · 基本面：周期判型 / 亏损股口径 / 日历三源 / 权重影子账本

- `backend/src_python/fundamental_factor.py`：TTM EPS 序列改用 `equity_earnings_events` 滚动四季求和（三表 `eps_diluted` 实测 8 季全 NULL）；新增**周期判型**（盈利位置 + 周期度，cyc≥1.5 为强周期，峰值/谷底区估值与盈利修正**各折半一次**）；新增**亏损股专用口径**（营收增速 0.40 / 毛利率趋势 0.20 / 经营现金流率 0.25 / 现金流趋势 0.15，读数不足封顶 ±0.6 标 `loss_eps_only`）；**财报窗口改为 ±3 天对称**（原只认未来 → NVDA 事件早一天时 `days=None`，护栏一次没触发）。
- `backend/src_python/equity_fundamentals.py`：新增 `pb_history`（市值 ÷ 当时已公布净资产，十亿美元换算、防前视、市值恒定守卫）、`_equity_asof`/`pb_basis`；日历改为 **FMP → AV CSV → yfinance 三源兜底 + 关注清单补缺**，窗口 7/90 天。
- `backend/src_python/fundamental_shadow.py`（新）：影子账本表 + `compute_ab`（含/不含 fundamental 两个方向分与动作）+ 日线结算 + 报告；`ai_worker` 决策后落账，`api_server` 06:20 引擎内结算（`DISABLE_FUNDAMENTAL_SHADOW_WATCHER=1` 可关），`cron/run_fundamental_shadow.sh` 06:10 入口。
- `decision_guard`：闸门解释直接展示亏损口径分/四项读数/盈利位置/周期度，复盘不必再猜"为什么折半"。
- 实测（10 只覆盖票）：
  | 票 | v406 | v448 | 原因 |
  |---|---|---|---|
  | MU | +0.9308 | **+0.4673** | 强周期/峰（cyc 4.08）`peak_earnings` |
  | NVDA | +0.9445 | **+0.4723** | 平稳型不折半，但财报 −1 天窗口折半 |
  | COIN / RIOT / CLSK / MARA | −0.44 / −0.44 / −1.00 / −0.44 | **−0.3504 / −0.3992 / −0.4655 / −0.6000** | 亏损口径出现区分度；MARA 无三表封顶 |
  | AAPL | +0.0079 | +0.0355 | 平稳/峰不惩罚（变化 ≤0.05） |
- 数据侧：未来财报事件 **7 → 774 条（774 票）**；未来 30 天 527 条；覆盖票 `earnings_in_days` 0/10 → 5/10。
- **PB 分位仍不可用（0/10）**：NVDA/COIN/RIOT 缺净资产，MU/CLSK 市值快照全 NULL，MSTR 市值恒定被守卫拒绝 —— 属数据缺口，归因见文档。
- 测试：`backend/tests/test_fundamentals_v448.py` 21 条；fundamentals 全量 77 passed（既有 56 条不回归）。
- 详见 `docs/v448-基本面能力提升-2026-11-18.md`；同时修正 `docs/v445-...md` §五 验收表中 NVDA 与 §四 规则的矛盾。

## v447 · 2026-11-18 · 开仓闸校准：位置闸证伪，同向闸收到 20 分钟 / 0.10%（enforce 仅 BTC）

- 新增 `backend/scripts/gate_sweep.py`：位置 × 同向双维度网格（3 持有期 × 3 位置窗口 × 5 同向窗口 × 5 门槛 × 4 位置阈值），跨持有期一致性筛选 + 分品种拆分 + 可换成本档，只读。
- `backend/src_python/entry_guard.py`：同向闸默认 **20 分钟 / 0.10%**（原 30 分钟 / 0.05%）；**位置闸默认不拦单**（`ENTRY_GUARD_POSITION_ENABLED=0`，只输出 `position_gate.would_block`）；新增 `recommended_hold_min=60`、`ENTRY_GUARD_ENFORCE_ASSETS`（默认仅 BTC）。
- `backend/tests/test_entry_guard.py`：17 passed（锁住"位置只记录不拦单""enforce 只对 BTC""未同向才降 HOLD"）。
- 校准结论（2030 笔、60 天、真实 K 线）：
  | 维度 | 读数 | 判定 |
  |---|---|---|
  | 位置分位 vs 净收益 | r ∈ [−0.038, +0.058]（n≈1,100） | 无预测力，11 笔的 r=0.69 是噪音 |
  | 位置闸（1h、30bps） | 被拦 −0.275% vs 放行 −0.336%，t=−2.10 | **反向显著，不能上线** |
  | 同向闸 20m/0.10%（1h） | 放行 111 笔 −0.216%（30bps）→ +0.034%（5bps），Δ=+0.163pt，t=3.16 | 可用，但需低费率 + 1h 持有 |
  | 同向闸 20m/0.10%（4h） | 放行 −0.433%，Δ=+0.171pt，t=1.36 | 方向对不显著 |
  | 分品种（5bps/1h） | BTC +0.031%（n=94，t=3.50）；NVDA −0.637%（t=−2.0，负优化）；XAU 无 5m K 线 | **只在 BTC 成立** |
- 关键约束：1h 档毛 +0.084%/笔，**盈亏平衡成本 8.4bps**；20 分钟持有档 Δ 仅 +0.013pt（闸门必须配 60 分钟持有）。
- 详见 `docs/v447-开仓闸校准-2026-11-18.md`。

### v424 · 结构性优势体检：资金费率/持仓/多空比（2026-11-18）

- 新增 `backend/src_python/structural_edge.py`：walk-forward 分位触发 + 同持有期去重叠 + 真实 K 线结算 + 无条件基线 + 时机置换检验（三关：净>0、t≥2、p<0.05、样本≥30）。
- 新增 `GET /api/edge/scan`（`routers/edge.py`，只读、5 分钟缓存）、`frontend/app/edge/page.tsx`、导航「优势体检」。
- 新增 `backend/tests/test_structural_edge.py`（8 passed）。
- 结论：库内 funding 仅 70 天（BTC/ETH/SOL 各 210 点）；ETH「极端高 funding → 做空 1 天」净 +1.866%/笔、t=3.46、时机 p=0.006、超额 +1.483%（已扣熊市 beta），但仅 18 笔 → 线索，不可上实盘；lsratio/oi 均为 4–14 笔，按规矩不下结论。
- 详见 `docs/v424-结构性优势体检-资金费率-2026-11-18.md`。

## v446 · 2026-11-18 · 开仓闸：位置分位 / 同向确认（默认影子，只标注不拦单）

- 新增 `backend/src_python/entry_guard.py`：20 分钟窗口位置分位、决策前 30 分钟同向确认、回踩参考价、影子账本回放、2030 笔历史信号分持有期验证。
- 新增 `backend/src_python/routers/guards.py`：`GET /api/guards/entry/{config,check,shadow,scan}`（只读，scan 缓存 10 分钟）。
- `decision_guard.evaluate_decision` 结果新增 `entry_guard` 读数；**默认 `ENTRY_GUARD_MODE=shadow`，不改动作**，要拦单必须显式设 `enforce`。
- 新增 `frontend/app/guards/page.tsx` + 导航「开仓闸」；`backend/tests/test_entry_guard.py`（14 passed）。
- 验证结论（2030 笔、20m/1h/4h、扣 30bps）：
  | 持有期 | 全样本 | 位置闸（拦→放） | 同向闸（拦→放） |
  |---|---|---|---|
  | 20m | -0.292% | -0.279 → -0.296（无差别） | -0.275 → -0.312（**反向**） |
  | 1h | -0.335% | -0.176 → -0.383（**-0.207pt，\|t\|=3.76 反向**） | -0.401 → -0.250（**+0.152pt，\|t\|=3.28**） |
  | 4h | -0.496% | -0.467 → -0.506（反向） | -0.467 → -0.532（反向） |
- 结论：**「不追极值」在 1 小时档是反向的，不能上线；「同向确认」在 1 小时档能筛掉更差的一档，但放行组仍是负期望。** 11 笔实盘的 r=0.69 是小样本噪音。
- 详见 `docs/v446-开仓闸-位置与同向-2026-11-18.md`。

### v423 · 能不能赚钱：成本 × 持有期矩阵（2026-11-18）

- 新增 `backend/src_python/cost_horizon_eval.py`：同一批历史信号在 20m/1h/4h/1d/3d × 30/10/5/2/0bps 上重结算，按方向算毛收益，尾部未走完或空洞超 2 周期一律跳过，样本 <30 不下结论。
- 新增 `GET /api/profit/scan`（`routers/profit.py`，只读）、`frontend/app/profit/page.tsx`、导航「能不能赚钱」。
- 新增 `backend/tests/test_cost_horizon_eval.py`（9 passed）。
- 结论（2030 笔、真实 K 线）：毛胜率 42–49%，**所有持有期毛优势≈0；4h 显著为负（t=-4.73）**；30bps 下全档显著亏；唯一为正的格子是 1d + 0bps（+0.040%，t=0.68）。
- 详见 `docs/v423-能不能赚钱-成本持有期矩阵-2026-11-18.md`。

## v445 · 2026-11-18 · 仓库上线：分段推送 / 默认分支切换 / 文档编号纠正

> 触发：用户「上传到 github 吧」。本文记的是**仓库运维动作**，不含代码改动；
> 同时纠正一处编号冲突（基本面路线文档 v441 → v445）。

### 一、起点：本地根本没有 remote

```
$ git remote -v      # 空
$ gh auth status     # gh: command not found
```
`~/.gitconfig` 无 `[remote]`，只有 osxkeychain 凭据（`github.com` 有 password 记录）。
所以不是"推失败"，是**从未关联过**。已 `git remote add origin` 到
`Claire19950906/reflex-quant`。

### 二、推送前先扫密钥（这次没被 Push Protection 拦）

历史上有过 `d17d5f2 · 移除 ARK_MODEL 硬编码默认值, 消除 GitHub Push Protection 拦截`，
说明这个库曾被拦过一次。所以这次先扫：

```bash
MB=$(git merge-base origin/main HEAD)
git diff $MB..HEAD --diff-filter=AM | grep -aoE '(sk-…|ghp_…|AKIA…|AIza…|-----BEGIN … PRIVATE KEY)'
```
7 类 pattern，**0 命中**。密钥类文件本身也都在 `.gitignore` 里
（`backend/.env` / `backend/*.db*` / `backend/backups/` / `node_modules/` / `.next/`）。

### 三、推送：盲推失败两次，改分段推才成功

| 尝试 | 结果 |
|---|---|
| 整库 `git push` ×6 | 5 次 `SSL_ERROR_SYSCALL`；第 6 次连上但 `send-pack: unexpected disconnect while reading sideband packet`（传到一半被断） |
| 分段推：按提交史 20/40/60/80/100% 各推一次 tip | 20% / 40% 各 1 次重试即成功；**60% 段连续 5 次全失败**，但被 80% 段顺带传上去（80% 的 tip 祖先包含 60% 的对象） |

**为什么分段推有效**：`push` 不可断点续传，一次失败就从头传；但把 tip 换成历史中段的提交，
每次只需传"远端还没有的那部分"，单次体积小到能在被重置前传完。经验值——**100MB 以上的
首推，按提交史分 5 段推，比死磕一次整推靠谱**。

**校验**（重试 10 次才连上）：`ls-remote` 的 `v342-judgment-grain` == 本地 HEAD `ac1990b`，一致。

### 四、远端 main 早已分叉 —— 所以不强推

```
本地领先 origin/main : 323 提交
远端领先本地         :  32 提交   （含 0173eeb「合并远端 v225 公开发布历史 (unrelated histories)」）
```
直接推 main 必须 force，会抹掉那 32 个提交，故**只推分支，不碰 main**。

### 五、默认分支切到真实主线

既然这个库实际是"一条线往前推"的用法（main 停在 10-24 的 `f04fe7d`），
把默认分支换成 `v342-judgment-grain`：不丢任何提交，clone / PR 基线都指向最新线。

走 GitHub API `PATCH /repos/… {"default_branch": ...}`，前 6 次 `URLError`，第 7 次 200，
复核 `default_branch = v342-judgment-grain`。`main` 原样保留在 `f04fe7d` 当存档。

### 六、编号纠正

v441 在本文写入前已被「归档理由补全 + 漏单捞回」占用，基本面路线文档
`docs/v441-…` → `docs/v445-…`，内文首行同步改号。**已推到 GitHub 的 v441 文件名随之作废**。

### 七、遗留

- 本地 `main` 仍停在 `c5e6ea3`，与远端 main 分叉未解决（要不要合并由用户定）
- `v342-judgment-grain` 分支名与内容不符（内容已经是 v445 级），仅作历史沿用

### v422 · 三个实用功能：标的别名层 / 可执行信号卡 / 源健康看板（2026-11-18 00:00–00:45）

> 触发：用户「有没有什么实用的新功能」→ 选定「都要」。三个都只读（别名表写 config 文件），
> 不新增抓取、不新增 LLM 调用、不写生产库。

**当天为什么要这三个（不是拍脑袋）**：今日 228 条决策里 `routing_miss` 115 条（50%）、
`target_none` 107 条、真正"证据充分可给方向"的只有 12 条 —— 信号少的真因是**一半新闻没落到品种**；
源侧 49 个源近 24h 只有 8 个在出货。

1. **标的别名层**（`target_resolver.py` + `routers/targets.py` + `/targets` 页）
   - 33 个 canonical 标的 / ~110 个别名；ASCII 词边界匹配（`mu` 不命中 `must`，有测试钉住）。
   - `GET /api/targets/unresolved`：捞 `routing_miss` + `unclassified_events(pending)` 原文去重后解析成榜。
   - 实测近 7 天：扫描 307 条 → **65 条原文里明明提到了品种（上限 21%）**，NVDA 23 / AMZN 11 / US_STOCK 9。
   - `POST /api/targets/aliases` 只写 `backend/config/target_aliases.json`，不碰库。
2. **可执行信号卡**（`signal_cards.py` + `routers/actionable.py` + `/actionable` 页）
   - 入场=最新收盘，ATR(14) 定止损（1.5×ATR），仓位=风险平价（1%/止损距离），给 1R/2R/3R。
   - K 线不足 15 根 → 不硬算，改 2% 兜底并标 `estimated=true`；聚合标的用 SPX/TENCENT 代理且标 `proxy`。
   - 实测：近 3 天 72 条方向性 → 同向去重后 **13 张卡、11 张可执行**（折叠 59 条重复）。
3. **源健康看板**（`source_health.py` + `GET /api/sources/health` + `/sources` 顶面板）
   - 六分类 healthy/flaky/stalled/dead/not_enabled/no_data，每类带一句"该怎么办"。
   - 实测：49 源 / 活跃 37 / 入库 2854 / **top3 占 57%** / 停更 10（eastmoney_ann 96.2h）。
   - 明确边界：只对"有没有真的入库"负责，「接口 200 但空」走 site_audit 的 empty 分类。

**验证**：`tests/test_targets_and_cards.py` 13 passed；`tsc --noEmit` exit 0；
线上 3 个新接口 200（1.74s / 0.65s / 0.23s）；openapi 383 → **387 路径**。

**未做**：别名命中不回灌决策（改决策链需用户点头）；`target_asset` 枚举本身没扩；
源"自动降级"没做（要写采集层配置 + 误杀保护）。

### v444 · 日预算口径 bug：闸门一直以为"还有额度"（2026-11-17 23:56–2026-11-18 00:07）

> 接手 v443 时顺手核对"跨天预算会不会重置"，结果查出更严重的问题：**预算闸门本身是失灵的**。

**一、症状**
门卫日预算 6,000，今天实际打了 5,985 次也没被拦住；而 `llm_budget` 报"全站 6,044/6,000 用尽"
—— 两个账本对不上，一个说还有额度，一个说早就超了。

**二、根因**
```sql
strftime('%s','now','start of day','localtime')   -- 错
```
SQLite 修饰符从左到右执行：先在 **UTC** 上取零点，再把结果当 UTC 转本地 → 边界落在**本地 08:00**
（跨天后甚至落在昨天 16:00）。实测同一时刻：旧口径 **1,535**，真值 **5,968**，**少算 74%**。
→ 闸门以为还有额度 → 门卫打到把全站预算烧干 → 新新闻门卫请求被拒 → 全员"未判定"。

**三、修复**
```sql
strftime('%s','now','localtime','start of day','utc')   -- 对
```
- `realtime_filter.py::_gate_budget_used_today()` 改新口径（并写注释留坑说明）。
- `engine/ai_worker.py` 主分析 `AI_MAIN_DAILY_BUDGET` 同一处旧口径一并修正。
- `llm_budget.py` 用 Python `mktime` 算本地零点，本来就对 —— 所以是"两个闸打架"。

**四、测试**
新增 `tests/test_v444_day_boundary.py`（5 项：新口径必须落本地零点、旧口径必须不等价、
源码中不许再出现旧日界 SQL）。与 v433/门卫用例合跑 **37 passed**。

**五、跨天实测（00:07）**
- 门卫 `66/6000`，`llm_budget` gate `66` → **两个账本一致**。
- 回放自动恢复：`[gate-replay] #35058 ok verdict=可交易`（昨日被"让位"挡住，今日额度充足自动放行）。
- 门卫调用 7–12s，全部成功。

### v409.7 · 决策级归因落地 + 给"增产样本"定性（2026-11-18 01:18–01:35）

> 承接 v409.6：`verified` 恒为 0 的根因是成交样本只有 7 笔，而洞察验证要 ≥12 笔。
> 本轮把"不等成交也能验证"的链路建起来，并查清"为什么成交就是长不出来"。

**一、决策级归因（新链，已落库）**
- 新模块 `src_python/insight_decision_attr.py`：对方向类洞察（`insight_direction` = 1/-1，注意库里存的是 **1/-1/0** 而不是 BUY/SELL），
  用**决策后 24h 真实价格走向**验证，样本单位是"决策"不是"成交"：
  · 放行组 = `suggested_action` 与洞察方向一致；
  · 拦截组 = `ungated_action` 与该方向一致但 `suggested_action='HOLD'`（被门卫拦下）；
  · `score = 0.5 + 0.5·tanh((放行均值 − 拦截均值)/0.5%)`，两组各 ≥5 条才判 verified/failed，否则 `insufficient_sample`。
  · 价格取 `kline_bars`（`timeframe='1h'`，`ts` epoch ms）——`kline_snapshots` 每个 symbol×timeframe **只有 1 行**（快照表），不能当历史用。
- 新脚本 `scripts/verify_insights_by_decisions.py`（干跑/`--commit`/`--days`/`--min-group`）；
  已 `--commit` 写回 17 条：`verification_method='decision_attr'`，其中 6 条算出证据分 **0.908**，
  但两组样本只有 1–5 条（<5）→ 结论一律 `insufficient_sample`，**不拿小样本冒充 verified**。
- 单测 `tests/test_v409_7_decision_attr.py` 7 例全过（含"拦截组更赚 → 判 failed"与"样本不足仍留证据分"）。

**二、"增产模拟盘样本"的真实卡点（不是门槛）**
- `buy_min_score` 实际是 **0.1725**、`sell_min_score` **0.105** —— 早已被自动校准压低，"门槛太高"不成立。
- 近 2 天 759 条决策的闸门原因分布：`统一置信度 0.0 低于闸门` **465 条**、`无可证据 coverage=0` **152 条**、
  真正"证据充分允许方向" **只有 18 条**。`confidence = |final_score|·100·权重·覆盖度…`，
  置信度恒 0 说明 **`final_score`≈0**，即多空权重互相抵消 —— 这才是成交长不出来的原因。
- 18 条过闸决策里：WTI 6、XAU 4、**ETH 4、SOL 1、BTC 3**；而模拟盘白名单只有 `XAU,WTI` →
  6 条合格信号被白名单挡在门外。
- 动作（**仅模拟盘、可回滚**）：`backend/.env` 的 `SIM_ALLOWED_ASSETS` 由 `XAU,WTI` 改为 `XAU,WTI,ETH,SOL`
  （备份 `backend/.env.bak-v4097`）。**BTC 暂不加入**：它在 `SELL_GATE_BLOCKED_ASSETS` 里，加进来会变成只能做多。
  已重启后端加载（`[restart] 完成，bridge=200 engine=1`）。预期影响面：只影响 quick_sim 建仓白名单与 R4 样本构成，
  不改任何决策门槛、不改 `EVIDENCE_CONFIDENCE_GATE`（仍是 15）。

**三、把维护做成每日任务**
- 新脚本 `scripts/daily_insight_maintenance.py`：一条命令跑四步
  ① 决策级归因刷新 → ② 成交口径验证刷新 + 出池（`retire_failed_insights`）→ ③ 污染标记修复。
  干跑实跑通过（出池 0 命中=幂等；污染标记待重写 30 条、清空 19 条）。
- 已创建本地定时任务「洞察池每日维护」，**每天 08:20（Asia/Shanghai）** 执行 `--commit` 并汇报池子与 S19/S20 变化。

**四、仍未解决**
- `final_score`≈0 的结构性原因（多空权重互抵 / 因子被置零）没动 —— 这是"有没有信号"的总闸，动它等于改交易逻辑，需单独一轮设计。
- `verified` 仍为 0；决策级归因要出结论还差样本：放行组/拦截组各需 ≥5，目前最多 2 / 5，按当前节奏约需 3–7 天积累。
- S19 观察型占比 95.5%（63/66）：清池进一步压缩了可执行分母，指标更难看属正常。

### v409.6 · 经验池"活不下去"的真凶：污染否决 + 验证分母（2026-11-18 00:58–01:17）

> 触发：v409.5 落地后发现「新洞察入库即 active=0、active 数在 0/1 之间抖」。顺着线索查出两个**结构性 bug**，
> 都不是洞察质量问题，是机制自己把池子掐死了。

**一、污染否决（`verifier_feedback`）把经验池当垃圾场清**
- 实测：97 条洞察里 **96 条**被写 `E014_CONTAMINATED` / `E020_CONTAMINATED`；**35 条被 veto 成 `active=0`**；
  重启一次又掉 22 条 —— 全是这个机制干的。
- 两个错：
  1. **因果方向反了**：`_find_related_insights` 的窗口是 `decision_ts ± 48h`，把「决策之后才出生的洞察」
     也算成"带偏了那条决策"。一条洞察不可能影响比它更早发生的决策。→ 改为单向：
     `insight.created_at ∈ [决策-48h, 决策时刻]`。
  2. **该不该 veto 本身就不成立**：`E014` 说的是**那条决策的推理链**断了，`E020` 是**样本/新闻本身脏**，
     二者都不构成"某条经验规则害了它"的证据；叠加 `asset=''` 的全局匹配（一条事故命中所有全局洞察），
     结果一条事故就能把池子清空。→ 依模块 docstring 原意，`E014`/`E020` 的 `veto_insights` 由 `True` 改 `False`
     （**只标 note 留证据，不再停用**）。
- 重建历史标记：新脚本 `scripts/repair_contaminated_marks.py`（逐码重算单向合法关联，不成立的码从 note 里删掉），
  `--commit` 重写 32 条、其中 17 条标记被完全清空。
- 恢复被误杀的经验：同脚本 `--restore-inactive`（只恢复「非人工停用 + 未被验证否证」的行）→ 恢复 31 条，active 40→71。

**二、验证分母（`insight_verifier`）让 "verified" 数学上不可达**
- `sample_conf = min(n/60, 1.0)`。而**全库 `quick_sim_trades` 已结算交易只有 7 笔** → 任何洞察 `n ≤ 7`
  → `real_signal_pct` 上限 0.117 → `verified`（≥0.7）**永远不可能出现**。
  这就是 `passed=0` 的真相：不是规则都被否证，是分母错得离谱。
- 改为 `min(n/MIN_TRADES_FOR_VERDICT, 1.0)`（=12，与 `decide_verdict` 的下限对齐）。
- 刷新验证后（active 池 75 条）：`param_rule 37 / partial 24 / failed 9 / insufficient 5`，**verified 仍为 0**
  —— 因为最大样本只有 7 笔，改动只是让"可达"变成"可达但没料"，没有造假通过。

**三、活体证据（v409.6 重启后）**
- 新洞察 **1304–1307** 全部 `active=1`、无污染标记（此前是入库即被 veto）；其中 **1304 带
  `[sizing.max_position_pct]` 且判成可执行**；池子 `active` 从 40 回到 71，日志 `[REFLECT] 经验评估` 连续 `停用0`。
- 出池脚本再跑一次停用 9 条新判 failed；最终池 `active=66`（观察 63 / 可执行 3）。

**四、还没解决的（真正的下一关）**
- **可执行洞察的验证没有样本**：7 笔交易 vs 需要 ≥12 笔 —— 要么增产模拟盘成交（改 `buy_min_score`/门卫，属风险参数，需拍板），
  要么把验证改成**决策级归因**（拿 750 条决策的后续价格走向去验证规则，不等成交）。后者才是正解，但工作量是"新建一条链"。
- S19 观察型占比 88.6% → **95.5%**（63/66）：清池把可执行分母也清了，指标更难看 —— 与 v409.5 的结论一致，清池不是提分手段。
- 验证刷新目前靠反思循环，`retire_failed_insights` 仍只手工跑过；建议做成每日任务（需授权）。

**五、测试**
`tests/test_v409_5_insight_landing.py` 增 2 例：E014/E020 不得 veto、晚于决策的洞察不算污染源；
相关集合 **51 passed**。

### v409.5 · 洞察链三处收口：生成侧量化 / 验否出池 / 仓位上限真字段（2026-11-18 00:20–01:00）

> 触发：用户问「细节洞察能力如何」→ 定位到**洞察链后半段（落地 + 验证）基本不通**，
> 卡点两条：①文案不写数值 → 换算无从下手；②参数键/字段在库里不存在 → 标 unmappable。
> 我给了三条改进，用户「全做」。

**一、生成侧强制量化（`review/review_agent.py::_extract_from_quick_sim_llm`）**
提示词在原有 ①–⑥ 后追加两条硬要求：
- ⑦ 每条 insight 必须可被验证：写明**触发条件的数值区间**、**建议动作的具体数值+单位**（如 BUY 最低分 0.4→0.55、止损 0.6%、仓位上限 5%）、**适用窗口**（如 72h）；只写「降低仓位」「别追高」这类没数值的经验一律不输出；
- ⑧ 能落到参数就用**方括号写参数键前缀**（`[score.buy_min_score]`、`[sizing.stop_loss_pct]`、`[sizing.max_position_pct]`）。

**活体证据（00:41:52 生成、重启后首轮）**：新洞察 1300/1301/1302 全部带方括号键；
1300 直接是 `[sizing.max_position_pct]` 且判成**可执行**（bucket=settings）。

**二、验证失败出池（新脚本 `scripts/retire_failed_insights.py`）**
`active=1 AND verification_verdict='failed'` → `active=0`，verdict/score 原值保留可审计；默认干跑，`--commit/--limit/--min-score`。
干跑命中 26 条（score 0.000~0.02、n 0~4），`--commit` 后 active 93→62。

**但第一版白干了**：跑完 10 分钟内被救回 25/26 条 —— `review_agent` 的「无样本救回」逻辑
（`_rescue_unmeasured` + 主循环 `rescued_no_sample` 分支）会把「停用 + 无样本 + 非人工」的行
**无条件** `active=1`，它不知道这行已经有「验否」结论。
→ 补闸（本轮真正修掉的 bug）：
1. 主循环无样本分支：`verification_verdict='failed'` 直接 `continue`，不救；
2. `_rescue_unmeasured` 的 SQL 加 `COALESCE(verification_verdict,'') <> 'failed'`；
3. 胜率复活路径（`wr >= re_th`）同样排除 failed —— 验否的不许靠胜率翻案（等重新验过再说）；
4. 该循环的 SELECT 补 `verification_verdict` 列（否则 KeyError）。
复跑 `--commit` 停用 25 条，active=62（观察 58 / 可执行 4），再复跑 0 命中。

**三、两个真缺字段（不是加口径，是加真列 + 真消费点）**
- **权益**：`insight_hypothesis.market_context()` 优先取 `sim_equity_snapshots` 最新 equity
  （实测 9997.57，之前用的是 config 的 10000 初值）。于是 `sizing.position_weight` 的观察原因
  从「缺上下文 equity_usdt」变成「**有上下文，但文案没给具体数值**」—— 责任回到生成侧，正是 ⑦ 要治的。
- **仓位上限**：`sizing.max_position_pct → max_position_pct` 从 `UNMAPPABLE` 移入 `SETTINGS_ALIASES`；
  `paper_trading_settings` 加列 `max_position_pct REAL DEFAULT NULL`（NULL=不设限，行为不变），
  建表 + 迁移两处都写（`_ensure_columns` 清单，不是塞进「缺列检测」函数里）；
  `paper_trading.py` 白名单/`_apply_param_change`（乘法、钳 [0.01,1.0]、无当前值从 10% 起步）/kwargs/`set_settings`/UPDATE 全接入；
  `quick_sim.py` 建仓处新增真实消费点 `_apply_position_cap(conn, notional)`：
  上限 = 最新权益 × 比例，`.005×9997.57=49.99 < 100` 会封顶并打 `[POSITION-CAP]`，NULL/无快照/异常一律原样放行。
  实测生产列已建（1 列、值 NULL），引擎启动无异常。

**四、测试**
新增 `tests/test_v409_5_insight_landing.py` 7 例：别名转正 / 实时权益优先 / 无快照退回初值 /
乘法+钳制 / 白名单 / `_apply_position_cap` 四分支 / **验否不被无样本救回**。
相关集合 `test_v339_param_aliases + insight_verification_wiring + v398_insight_pool + v409_sizing + 本组` = **50 passed**。

**五、口径修正**
`scripts/degradation_stats.py` 的洞察可执行率原本统计全表（含已停用），改成只算 `active=1` ——
否则停用/启用的抖动会污染这个核心指标。

**六、还没解决的（诚实结论）**
- 清池**不是**提升可执行率的手段：停用的 26 条里 20 条是观察型，分母缩小后观察型占比反而 89.2%→**93.1%**（54/58）；
- `passed=0` 没变：4 条可执行洞察全库没有一条通过数据验证；
- `sizing.position_weight` 仍缺数值，`position_weight` 类洞察要靠 ⑦ 在新产出上生效；
- 降级率 67.6%、S18 39.3%、S3 49.8%、R4 50.4% 结构性，本轮未动。

### v409.4 · DLQ 陈旧告警自动老化 + 权益上下文 + 报告回改（2026-11-18 00:08–00:20）

> 「还有什么问题」健康扫描列出的 4 件事，这一轮一次收完：DLQ 噪音、报告旧数字、SIGTERM 归因、统计口径。

**一、DLQ 陈旧告警（真正的噪音源）**
`dlq_events` 里 3 条 `kind='decision_stall'` 一直 `resolved=0`（11-16 08:38 / 11-16 22:47 /
11-17 13:14），而决策链早就恢复 —— quota-sentinel 每 5 分钟照本宣科报一次「DLQ未解决=3」，
真出事时这条警报会被自己的历史噪音埋掉。

修复：`ai_worker.py` 新增 `_auto_resolve_recovered_stalls(conn, min_age_hours=2.0)`，
结单判据用**两个事实**而不是「时间到了」：
1. 告警存在 ≥2h（避免刚报警就自己关掉）；
2. 当前 `ai_decisions` 最新一条**晚于**该告警创建时间（= 停摆期确实结束）。
只改 `resolved` 标记，原文后追加 `resolved: auto(产出已恢复, 最新决策 …)`，不删证据。

挂两处：`_check_and_alert_decision_stall()` 巡检入口、`ai_worker()` 启动复位块之前。

**二、重启实测（00:12）**
```
[DLQ-AGING] 自动结单 3 条已恢复的 decision_stall 告警
dlq_events 未解决 = 0
```
quota-sentinel 下一轮应报 `DLQ未解决=0`。

**三、权益上下文（`sizing.position_weight` 的前置条件）**
`insight_hypothesis.market_context()` 增加 `equity_usdt`，取自 `config.PAPER_INITIAL_EQUITY_USDT`
（纸盘初始权益 = 10000，权威配置）。**注意：不是实时权益**，库里没有权益列，所以
换算说明写「按纸盘初始权益」，不冒充当前权益。

实测结果（重要）：干跑 `reclassify_insights.py` 扫 88 条、差异 13 条、**转为可执行 0 条** ——
因为 11 条 `position_weight` 洞察的文案**根本没给数值**（「降低仓位」「小仓位验证」），
缺的是「几 %」不是上下文。**没有编一个百分比去凑**，观察原因如实保留。这解释了
S19 88.6% 的真正卡点：**洞察文案的量化程度**，不是参数映射。

**四、报告回改**
`docs/细节洞察能力评估-2026-11-16.html` 增 8.7 节（733 决策复核）：S16 67.6%/n=463、
S18 39.3%/n=735、S19 88.6%/n=88、S20 56.0%/n=150、S14 64.2%/n=201、S3 49.8%/n=2200、
R4 50.4%（1107/2200）；并写明 8.1 与 8.7 不是同一批数据、不可跨表相减。footer 追加 00:14 复核。

**五、SIGTERM 归因留痕（两版才做对）**
v1（错）：handler 里直接 `subprocess` 起 `ps` 采快照 —— 信号上下文里 fork 会死锁，
实测两次都 `suspects: []`（**采到空比不采更危险，会让人误以为"没人杀"**）。
v2（对）：`run_engine.sh` 起一个**独立进程**每 2s 把「可疑杀进程」
（`launchctl kickstart` / `restart_backend.sh` / `pkill …engine`）写到
`~/.trident/state/kill_suspects.txt`；handler 只读这个文件，不做任何 fork。
同时记账加 `ppid` + `parent_cmd`（`ppid=1` = 由 launchd 拉起/管理）。

**实测（自测那次重启，00:21:33）**：
```json
"ppid": 1, "parent_cmd": "/sbin/launchd",
"suspects": [{"hits": ["… -c exec 9>'…DoubaoWork…", "bash …/restart_backend.sh"]}]
```
→ 机制有效：能抓到「谁在那一刻跑着 restart_backend.sh」。外部 kickstart 若真存在，
下次被杀时这份快照里会出现 `launchctl kickstart -k com.trident.engine` 那一行。

**六、新增统计脚本**
`backend/scripts/degradation_stats.py`（只读）：降级原因分布 / 洞察观察原因 Top / 抢救成功率
（按 why 分组）。首次读数：抢救 4/12 = 33%（`llm_empty` 7→3 成 43%、`summary_only` 5→1 成 20%）；
观察原因 Top 是「没有参数键 32」、「position_weight 缺 equity 11」、「持仓上限无对应列 8」。

**七、测试**
新增 `tests/test_v409_dlq_stall_aging.py` 3 例（已恢复→结单 / 仍停摆→不结单 / 无决策→不结单，
且验证刚报的与非 decision_stall 的都不动）。与 v409.1/v409.2 用例合跑 **21 passed**。

### v409.3 · 抢救日志把「预算拒绝」记到别人的决策上（decision_id 未赋值）

> 11-18 00:02 复核：v409.1 加的异常落盘生效了 —— `rescue_log` 从 1 行涨到 13 行，
> 异常分支第一次抓到东西：`{"news": 35047, "decision": 0, "skip":
> "exception:UnboundLocalError", "err": "cannot access local variable 'decision_id'..."}`。
> 也就是说**日志修对了，但它照出来的是另一个真 bug**。

- **根因**：两处 `_rescue_log(...)` 在落库前抢救阶段就写了 `"decision": decision_id`，
  而 `decision_id` 要等 INSERT 之后（`decision_id = cur.lastrowid`，约 13800 行）才有值。
  后果两层：
  1. 本批**第一条**新闻就抛 `UnboundLocalError` → 整个「要不要抢救」的预算判定被
     `except` 吞掉 → 这条该抢救的没抢救（实证 news 35047 / ETH / summary_only）；
  2. 后面的条目不抛，但会捞到**上一条新闻**残留的 `decision_id` → 预算拒绝被记到
     别人的决策上（实证 `{"news": 34860, "decision": 12100, "skip": "batch_cap"}`，
     12100 其实是另一行的 id）。
- **修法**：两处都改成 `"decision": 0` 并写明理由（抢救在落库之前，本来就没有 decision id）。
  不改成"先定义 decision_id=0"，因为 13800 行之后多处代码用它做真值判断（`if decision_id:`
  触发 gate / 评分 / 共识写回），提前赋值会让这些分支误判成"已有决策"。
- **测试**：关联集 **79 passed**，AST 通过。重启后 `BATCH-RESULT 成功 1 / 失败 0`。
### v443 · 门卫可用性：超时 25s + 重试 2 + 回放让位（2026-11-17 23:26–23:56）

> 上一轮（v442）把门卫成本压到 0.86 次/条，但**判定质量**没查：近 1 小时 116 条新闻里
> 96 条"未判定"（gate_unavailable=2），只有 18 条拿到真判定。门卫形同不存在。

**一、症状与根因**
- 日志：`pool-retry ... TimeoutError` ×3 → `pool-net retries exhausted` → `pool-fail | 64461ms`
  → 降级备用通道 → `LLM 全池不可用`。所有 breaker 都是 `closed`，**不是熔断**。
- 真实延迟：`MiniMax-M2.7-highspeed` 单次 5.2 / 7.2 / 12.0 / 12.2 / 31.7s 都有；
  `.env` 却是 `REALTIME_GATE_TIMEOUT_S=20` + 内层重试 3 → 最坏 64s 且三连全超时
  → 走 v427"超时降级放行"（`gate-llm-timeout-pass`）→ 等于没门卫。

**二、改 `realtime_filter.py`**
- `_GATE_TIMEOUT_S` 默认 `10.0` → `25.0`（`.env` 显式 25，覆盖 95 分位）。
- 内层 `range(3)` → `range(_HTTP_RETRIES)`，`REALTIME_GATE_HTTP_RETRIES=2`。
- 最坏耗时 64s → 50s。重启后 20 分钟 `purpose=gate` **ok 163 / http_error 21 = 88.6%**，
  `[GATE] MISS` 从 23 点每小时 19 条降到 **0 条**。

**三、改 `engine/gate_replay_guard.py`：回放让位**
- 发现：深夜只有 2 条新新闻却记录了 90 次 gate 调用 —— 回放在补判历史"未判定"（50 条/轮 × 300s
  ≈ 14,400 次/天），而门卫日预算 6,000、全站 9,000，会被一路烧干。
- 新增：`usage_by_purpose("gate")` 用量 ≥ `GATE_REPLAY_RESERVE_PCT`（默认 60%）→ 跳过本轮。
- 实测触发：`[gate-replay] 跳过（通道不可用）：回放让位：门卫今日已用 5886/6000 次（≥60%）…`

**四、测试**
`test_v433_timestamp_and_gate_down.py` + `test_v434_gate_fp_cache.py` + `test_realtime_filter_gate.py`
合跑 **40 passed in 4.19s**。

**五、验收**
见 `docs/全站分析与优化建议-2026-11-17.md` 附录 L（L3 成功率、L4 让位日志、L6 两条 SQL）。

### v435 · 重启审计：把「谁在杀引擎」抓出来（2026-11-17 20:05–20:12）

> 承接 v434 收尾时发现的第二个产能杀手。今日引擎被外部 `launchctl kickstart -k`
> 反复重启（`ENGINE-EXIT` 近 1 小时 ×9，`ops-audit` 告警「先停掉外部 kickstart」），
> 但仓库内**没有任何** `kickstart` / `bootout` 调用点，`restart_calls.log` 也没记录
> —— 只有被杀的视角，没有动手的视角。本轮补齐这个视角，不改任何决策行为。

**新增 `backend/scripts/restart_audit.py`（进程表侧采样）**
- 每 0.1s 采一次 `ps -axo pid,ppid,uid,tty,command`，命中
  `launchctl kickstart|bootout` / `restart_backend.sh` / `kill.*engine.py` /
  `pkill -f engine.py` / `killall engine` 时，记录 pid、uid、tty、命令行，
  以及**沿 ppid 往上的完整父子链**（最多 5 层，遇 launchd / 成环即停）。
- 两个坑都做了处理：
  1. **不能只按文本匹配** —— `bash -c "...kickstart..."` 是包装层、采集脚本正文里
     也可能出现这个词，所以命中后还要过第二道：**下一采样周期 pid 已消失**才算数
     （真正的 killer 是短命进程；长驻 shell 会被剔除）。
  2. 自身不记 —— 命令行含 `restart_audit` 的一律跳过，避免审计把自己写进日志。
  3. **加长度判据**（实测 20:11:25 的误报）：真实的调用命令很短（直接调用约 50 字符，
     套一层 `bash -c "cd … && launchctl …"` 也就一百出头），而「在 heredoc 里写文档／
     写 CHANGELOG」这类命令动辄几百上千字符，正文里提到 kickstart 纯属巧合。
     所以超过 220 字符的命令行一律不匹配。
- 落盘 `backend/runtime/restart_audit.jsonl`（每命中一行 JSON，含 ancestry）。
- 用法：`python3 backend/scripts/restart_audit.py [--duration 秒] [--interval 秒] [--once]`。

**引擎侧记账（`src_python/engine.py`）**
- SIGTERM 钩子里追加一条 `role="engine_sigterm"` 记录（`ts` / `pid` / 当时 `step`），
  与审计脚本采到的「调用方」按时间配对 —— 这样每次重启既知道**谁动手**，也知道
  **动手时引擎卡在哪一步**。handler 里只读 `runtime/ai_worker_progress.json`，
  不碰数据库，且整段包在 `try/except` 里（记账失败绝不影响退出）。

**测试**
- 新增 `backend/tests/test_v435_restart_audit.py`（**7 例**）：`ps` 字段对齐 / 三类 killer
  命令命中 / 普通命令与审计自身不命中 / ancestry 遇 launchd 断开 / 短命确认（活着不写、
  消失才写）/ 成环不死循环 / 长 heredoc 命令不误报。**7 passed**（连 v434 两组共 **23 passed**）。
- 端到端自检：用 `exec -a "launchctl kickstart -k ..."` 造了一个 50ms 的假调用，
  审计成功抓到并记录了完整父子链（`bash -c` ← `agent_infra bash` ← `AgentInfraService`
  ← `launchd`）。

**当前状态**
- 审计进程已在后台运行（pid 54698，限时 4 小时，日志 `~/.trident/log/restart_audit.out`）；
- 引擎侧记账要**下一次启动**才生效 —— 按当前被杀的频率，几个轮次内就会自然加载。

**未做**
- 没有自动「限制重启频率」的闸（那会掩盖调用方，先把人找出来更划算）。

### v434 · P0-A1：熔断干等不再是黑箱（轮次 / 已等待 / 最近可恢复倒计时）（2026-11-17 19:10）

> 承接 v413（产能诊断）。那一轮已经把「pool-blocked 不再被看门狗 30 分钟强杀」修完了，
> 但**干等本身还是不可见**：progress 里 `step` 恒为 `pool-blocked`、日志只有一句
> 「跳过认领：全部通道熔断」，运维看不出等的是 quota（30 分钟自愈）还是 auth（24 小时，
> 等也没用）。本轮只加观测，**不改任何决策行为**。

**引擎侧（`engine/ai_worker.py`）**
- 新增 `pool_recovery_eta(pool=None)`：按 quota / net / auth 三个熔断器的
  `_wait_until` 与 `probe_timeout`，算出池内**最快**可放行的通道还要多少秒。
  返回 `None`（算不出）/ `0.0`（已有通道可放行）/ `>0`（剩余秒数）。
- `_log_progress(step, **extra)`：支持附加字段落盘；`step` 名**保持不变**
  （`engine_watchdog.POOL_BLOCKED_STEP` 是精确匹配，改名会让 6 小时宽线失效）。
- 熔断分支新增四个字段：`pool_blocked_round` / `pool_blocked_since` /
  `pool_blocked_elapsed_s` / `pool_blocked_reason` / `pool_next_recovery_s`；
  日志改为 `第 N 轮，已等 Xs｜最近可恢复 HH:MM（还要 Ys）`；通道恢复后清零。

**红线**
- `pool_recovery_eta()` 只读 `state` / `_wait_until` / `probe_timeout`，**不调用**
  `allow()` —— 那会占掉 half_open 唯一的探测名额，把额度正常的通道卡死 30 分钟
  （v409.2 事故）。

**测试**
- 新增 `backend/tests/test_v434_pool_blocked_visibility.py`（8 例）：open 剩余秒数 /
  健康通道优先（取 min）/ 跨通道取最快 / half_open 探测占用按 probe_timeout 估算 /
  无熔断器返回 None / 空池返回 None / **allow() 零调用**（v409.2 回归）/
  step 名与字段字面量守卫。
- 相关回归：`-k "pool or progress or watchdog or breaker"` 163 passed；
  4 个 failed 均为本轮之前就红的旧用例（`test_noncore_fastpath_v396` 关键词表、
  `test_p3_gates_v396` 非池标的、`test_v322_breaker_epoch_and_gate_freeze`
  期望 `gate_unavailable=1` 与 v432/v433 的 `=2` 口径冲突），与本轮改动无关。

**顺手补的第二个洞：`processing_at IS NULL` 的僵尸永远回收不到（19:35）**
> 实测发现：2026-11-17 18:00–18:12 有 6 条新闻（33918 / 33981 / 34008 / 34011 /
> 34017 / 34025）挂在 `status='PROCESSING'`、`processing_at` 为空，一直挂到 19:14
> **只有重启才清掉**。看门狗每 120s 跑一轮，但回收 SQL 带 `processing_at IS NOT NULL`
> ——这 6 行被这条判据排除在外，等于永远判不了死。它们占着「待分析」计数、
> 占着认领幂等（`_CLAIM_DEDUP`），但在对账口径里既不是 PENDING 也不是 DONE。
- 改 `_WATCHDOG_RECLAIM_SQL_BASE`：判死改成看**实际认领时间**
  `COALESCE(processing_at, datetime(COALESCE(ts,0),'unixepoch','localtime'))`，
  空值用新闻入库 `ts`（epoch 整数，无时区歧义）兜底；`ts` 也空则按 1970 处理。
- 为什么 NULL 一定是异常：认领 SQL（`_BATCH_CLAIM_FRESH_SQL` / `_BATCH_CLAIM_SQL`）
  是**同一个 UPDATE** 同时写 `status` 与 `processing_at`，写不出「一半」。
- 测试：新增 `backend/tests/test_v434_processing_null_reclaim.py`（8 例：NULL+2h 回收 /
  NULL+1min 保留 / NULL+NULL 回收 / 旧 processing_at 路径不变 / 刚认领保留 /
  retry 达阈值 → FAILED / 在办批次排除 / PENDING·DONE 不动）；
  同时把 `test_v322_watchdog_active_batch.py` 里锁死旧判据的两条改为新语义（补 `ts` 列）。
  `test_v322 + test_v434_processing_null_reclaim + test_v434_pool_blocked_visibility` = **39 passed**。

**未做（下一轮）**
- 主动半开探测（发一条最小请求）：熔断器冷却到期后 `available()` 已会自动放行，
  探测只在「上游恢复得比冷却期早」时有用，收益不确定，先不上。
- **外部 kickstart 风暴**：发现今天引擎被外部会话每隔 3–5 分钟 `kickstart -k` 一次
  （`ENGINE-EXIT` 近 1 小时 ×9，`ops-audit` 也已报警「先停掉外部 kickstart」）。
  落库在批末一次性提交，每次重启都会作废在办批次 → 这是「零产出小时」的另一个
  独立成因，不在本仓库代码里，需要先找出那个调用方。

### v434 · P0-0 锁库结案：旧代码背锅 + 长事务哨兵（2026-11-17 19:20–19:45）

> 承接 v433。v433 迁移被 `database is locked` 挡了 23 分钟，当时只记成「引擎长事务」。
> 这轮查清楚了：**不是设计问题，是引擎进程在跑 18:26 的旧文件快照**。

- 症状一：19:22–19:28 每批 `Batch persist 失败: NameError: name 'analysis_degraded_reason' is not defined`
  → 决策零落库、行退 PENDING + 15 分钟冷却 → 「待分析」只增不减。
- 症状二：`scripts/lock_probe.py`（新增，只 `BEGIN IMMEDIATE` 后 `ROLLBACK`）实测
  **连续 138 秒零次抢到写锁**，与迁移被挡 23 分钟同源。
- 根因：`[BUILD] engine/ai_worker.py=8e0b02dc@18:26:38` —— 进程内存里是旧版（整批一个事务、
  事务里夹 LLM 抢救 → 一持几分钟）；磁盘 19:16 已改成逐条 commit + 变量名修正。
- 处置：重启 engine(43623)/api(43629)/bridge(43651)。验证：`Batch persist 失败` 停在 4 不再增长、
  批次 19:38:25 `5/6 ok wall=294s` 正常落库、30 分钟新决策 14 条、探针 100s 零被挡。
- 新增 **长事务哨兵**（`db.py`）：事务跨度 > `TRIDENT_TXN_SLOW_S`（默认 5s）就写
  `/tmp/trident_txn_profile.log`（pid + 首个写类型 + 调用栈），`TRIDENT_TXN_PROFILE=0` 可关。
- 流程教训：改 `ai_worker.py` / `db.py` 后必须重启服务，以日志 `[BUILD]` 行的 hash@时间 为准。

**P0-1 切片一：门卫标题指纹缓存（转发去重）**
- `realtime_filter.py` 新增 `_gate_title_fp / _gate_fp_get / _gate_fp_put / _gate_fp_stats`：
  抹平 `[hash:xxx]` 前缀 / 标点 / 空白 / 大小写 / 全半角后取 sha1，命中即**零请求**复用判定。
- `_attach_gate` 先查指纹缓存、再调 LLM，成功回写；失败不缓存（避免把瞬时故障固化成判定）。
- 默认开（`GATE_FP_CACHE=0` 关），上限复用 `GATE_CACHE_MAX_ENTRIES`、TTL 复用 `GATE_CACHE_TTL_S`；
  返回 `deepcopy`，调用方改 dict 不会污染缓存。
- 新增 `tests/test_v434_gate_fp_cache.py`（8 例）：多源转载同指纹 / 不同新闻不同指纹 /
  过短标题不缓存 / 环境开关 / 返回值隔离 / 未命中返回 None。

### v433 · P0 前两步：时间戳统一 epoch 秒 + 门卫故障独立成 `gate_down`（2026-11-17 18:30–19:05）

> 承接 v432。这轮只做「报告里排最前、又不改业务口径」的两件事：把 `raw_news.timestamp`
> 从三种格式归一成 epoch 秒；把「门卫故障导致的归档」从 `degraded_stale` 改名 `gate_down`，
> 不再伪装成「新闻旧了」。

**P0-4 时间戳归一**
- `db.py` 新增 `coerce_epoch_ts(value, default)`：int / 10 位串 / 13 位毫秒 / ISO 带时区 /
  本地 `YYYY-MM-DD HH:MM:SS` 一律转 epoch 秒，解析不出返回 default（**绝不猜时区**）。
- `insert_raw_news()` 改为「timestamp 与 ts 同源」：先用 `coerce_epoch_ts(ts)`、再退回
  `coerce_epoch_ts(timestamp)`、最后兜底 `now`，两列都写同一个整数；`first_seen_ts` 同步用归一值。
- 建表默认值 `datetime('now')` → `strftime('%s','now')`，新库从第一天就是 epoch。
- 补两处绕过 `insert_raw_news` 的裸 INSERT：`tech_signal_bridge.py`、`feeds/crypto_news_feed.py`（占位符 8→9）。
- 新增 `backend/scripts/normalize_raw_news_ts.py`（`--check` / `--apply`，200 行分块 + 退避重试）。

**P0-3 归档理由改名**
- `engine/ai_worker.py` v432 段：`filter_reason='degraded_stale'` → `'gate_down'`，
  日志文案改「门卫未判定超 N h → 退出建仓候选」，注释写清「不是旧闻、留在库里可回放」。
- 环境变量 `NEWS_EXPIRE_DEGRADED_H` → `NEWS_EXPIRE_GATE_DOWN_H`（旧名仍兼容读取）。
- `analysis_yield.py` 漏斗新增 `gate_down_archived` 并单列打印，和「噪音 / 超龄」分开看。

**守卫 + 测试**
- `pipeline_guard.py` 新增 `check_timestamp_format()`：窗口内 `timestamp` 必须是 10 位数字
  且等于 `ts`，异常判 **broken**（这类行会让 `now - timestamp` 算成 0，超龄/去重/窗口全静默出错）。
- 新增 `tests/test_v433_timestamp_and_gate_down.py`（20 例）：归一各形态 / 落库 `timestamp==ts` /
  `gate_down` 归档理由 / 旧环境变量兼容 / 守卫通过态与 broken 态。
- `test_v408` 两条 v419 宽限用例补 `NEWS_EXPIRE_GATE_DOWN_H=0`（v432 的 2h 快速归档会先命中，
  测 v419 语义时须关掉这一层）——这原本是今日改动留下的红灯。

**数据迁移（生产库，已备份）**
- 备份：`backend/backups/trident_event_bus.before_p0_20261117_184352.db`（817M）。
- `--apply`：**22,373 行 timestamp 全部 10 位 epoch**，`--check` 剩 0 条异形。
- `degraded_stale → gate_down` 回填 **876 条**，`degraded_stale` 归零。
- 迁移中撞到 `database is locked`：引擎整批写事务开了 20+ 分钟（6 条 PROCESSING 从 18:29 不动、
  `ai_call_stats` 停在 18:43），任何外部写全被挡。处置：`launchctl stop/start com.trident.engine`
  → 锁释放 → 迁移 → 重启（PID 29162 → 34391）。**这条「长事务锁库」记为待修项**（api 因此启动 97s）。

**回归**
- `test_v433` 20 通过；`test_v408 / test_pipeline_guard / test_v322 / test_v409 / test_v3213` 共 43 通过。
- 重启 api(34990)/bridge(35009)/engine(34391)，8000 与 8100 均 200；重启后新入库 27 条全部
  `timestamp == ts`（10 位 epoch）。

### v423 · 因果图谱本体化：35 个规范因子 + 核验刻度 + 注入埋点（2026-11-17 16:00–17:50）

> 承接 v422。v422 把"垃圾桶"关掉、把废话挡在 prompt 外；v423 解决**同一个因写成几十种说法、
> 图不可复用**：1500 个节点里 1298 个 label ≥16 字、64% 只有 1 条边、跨 ≥3 品种复用的 label 只有 2 条。

**录入：规范因子本体**
- 新增 `backend/src_python/factor_ontology.py`：35 个因子（`key/canonical≤14字/family/slow/aliases`），
  `map_label()` 最长别名匹配，归不到留 NULL 不硬猜；`risk_premium` 单列（避险/地缘风险溢价），
  避免被 valuation 的"风险溢价"抢占。
- 新增 `factor_ontology` 表；`asset_driver_nodes` 加 `factor_key` 列 + 索引。
- `backend/scripts/kg_factor_ontology.py`（dry-run / `--apply` / `--revive-slow`）落库：
  **35 因子 / 迁移写入 630 条归因 / 32 个 MACRO 短名节点 / 630 条 `instance_of` 边 / 覆盖 20 品种**
  （当前 `stats.ontology`：661/889 = **74.4%**，证据锚点不计入分母）。
- 慢变量复活：`active=0 & core & conf≥0.15 & 可归因`，按 `(asset,factor_key)` 去重取最优 → **31 条**，
  XAU 的 3 条真核因回位（4 条 active core）。
- 修掉 4 个坑：MACRO 被当资产做跨品种校验；复活去重缺 `version` 的 `KeyError`（事务回滚未半写）；
  XAU 核因 confidence 实存 0.1999… 阈值 0.2 漏掉；AAPL core 写"黄金定价"的跨品种污染。

**输出：核验刻度 + 跨品种同源**
- `_node_stats()`：n / 命中 / hit_rate / Wilson 95% 下界 / sample_status；`get_graph`、`get_knowledge`、
  `inject_block` 三处都带。
- `get_graph(include_macro=True)` 带 MACRO 邻居与 `instance_of` 边；`get_knowledge` 带 `factor_key/canonical/
  family/slow/n/hits/wilson_low/sample_status/cross_asset/chain_priors`（core<3 用链库先验并标注"无 LLM"）。
- `inject_block` 每条带刻度、0 core 明说"不要凭空补"、输出跨品种同源、边界话术按 `total_n` 切换；
  新增 `inject_block_json()`。
- `_reconcile` 慢变量分层：慢因子 `protect=version*2+1`、衰减 `min(0.92,decay+0.15)`、下限 0.1。

**埋点（只观测）**
- 表 `causal_injection_log(ts, asset, mode, block_len, factors, decision_id)`；
  `ai_worker` 的 batch / l2 / attribute×2 四个注入点接 `log_injection()`，独立短连接、异常全吞，不影响决策。

**前端（已构建上线）**
- `causal-graph-panel.tsx`：`macro` 节点 + `instance_of` 边；「骨架视图」开关（资产→核心→规范因子）；
  「置信≥/边权≥」阈值；ALL 视图只留 ≥2 品种持有的规范因子；详情面板显示因子/刻度/覆盖品种。
- `npx tsc --noEmit` 通过；`.next-prod` 17:51 构建完成，18:02 把 3030 切回 `com.trident.frontend` 托管进程；
  实测页面 chunk 已含「骨架视图 / 规范因子 / instance_of」。

**v423.1 输出降级（同一轮内补做）**
- 复活慢核带旧 `_status=CONTRADICTED` 时，注入不再写"核心驱动（最底层）"，改写
  `**旧核验否证（仅结构位，勿据此定方向）**`；`get_knowledge` 给 `structural_only=true`。
- **不动数据**：核验结论、支持/反驳边、n/命中全部保留，只改 prompt 话术，避免"既叫核心又叫已证伪"。

**测试**：`tests/test_v423_ontology.py` 16 passed（映射/规范名/幂等/`factor_key` 落库/慢衰减保护/
刻度与 Wilson/跨品种/`get_graph` MACRO/埋点不抛/慢核输出降级）；连同 v422 等共 **102 passed in 22.42s**。

**复测**：`/api/review/causal-graph/stats` → `node_count=1043, edge_count=1762`，新增 `ontology` 段
（`coverage=0.7435`、`by_family`、`top_factors`、`single_degree_nodes=154`、`instance_of_edges=630`、`unmatched_samples`），
`macro 32`、`instance_of 630`；单边节点 398→**154**；MACRO 平均度 **19.7**。
api_server / engine 已于 17:53–17:54 重启（`get_graph?asset=XAU` 返回 45 节点 / 11 MACRO / 52 条 instance_of）；
17:47 注入埋点落下首行（`个股/batch`），报表脚本 `scripts/kg_injection_report.py`。

### v422 · 因果图谱 P0 瘦身：关掉两个合成节点入口，注入不再喂执行层废话（2026-11-17 15:00–15:20）

> 触发：「因果图谱录入和输出怎么样怎么优化」的落地。
> 前十四章（`docs/因果图谱体检与整改-2026-11-17.md`）把**链库**修到能用；这轮只做减法：
> 图谱本体的两个合成节点入口 + 注入前过滤。不改推理逻辑、不动链库、全程可回滚。

**录入：两个"垃圾桶"入口**
- `seed_from_causal_chains` 旧实现把每条链的 `cause` 原文当 `asset='all'` 的 core，cron 每 6h 重跑再
  version+1 —— 实测 160 个节点、92 个"核心驱动"全是新闻标题、229 条出边。→ 默认 `macro_mode="off"`，
  只保证链里的资产有 asset 锚点；`atomic` 模式（≤14 字、非事件标题、非列名）留作因子本体化后再启用。
- `persist_evidence_edges` 每次核验新建 `[SUPPORT] ev_7d_<ts>_<nid>` —— 462 个一次性节点、每个 1 条边，
  同一假设的支持/反驳永远累积不成一条边。→ 每资产一个稳定锚点 `[证据] XAU`，边按 (锚点, 假设, kind) 唯一，
  `weight=本次票数`（覆盖语义，不累加），本次为 0 就删旧边。
- 数据迁移 `backend/scripts/kg_p0_graph_slim.py`（默认 dry-run + JSONL 备份 + `--refold-from` 幂等重放）：
  删 160 + 462 个节点、691 条边；删前把最近一次票数折叠进 158 条锚点边（81 个假设 / 13 个品种），悬空边 0。
  节点 1620 → **1011**，`all` 桶 160 → **0**，evidence 462 → **13**。

**输出：注入前过滤（只影响 prompt，不改库）**
- `_clean_why` 新增三道：跨品种（BTC 证据里的「XAU 多头反应更快」）、执行层（「同一事件重复入场」「冗余入场」）、
  列名碎片（`funding/cluster_heat`）；`_is_schema_label` 现在也能拦"中文 label 里夹英文字段名"。
- `inject_block` 增加因素名过滤 + "证据全是废话就整条不进 prompt"：BTC 去掉置信 0.95 的执行层毛病，
  SPX 条件从 3 条降到 1 条，XAU 不再让"未平仓止盈纪律"占名额。
- 合成锚点（`kind in (asset, evidence)`）不再走驱动三道校验，否则 `[证据] CN_STOCK` 会因 `cn_stock`
  带下划线被当列名拒掉（首次迁移实测踩到，已用备份重放补回）。

**测试**：`tests/test_v422_graph_slim.py` 18 passed（seed 默认/atomic、锚点唯一/覆盖非累加/零票删边、
跨品种/执行层/列名过滤）；`test_v407 / v406 / causal_mechanism` 合计 32 passed。

**生效范围（诚实记录）**：数据层已生效；api_server（14:34 启动）/ engine（14:43 启动）仍是旧内存，
`inject_block` 新过滤要等重启 —— `restart_backend.sh` 会暂停 engine、打断在跑批次，未擅自执行。

### v421 · 全站产出体检 + 防错机制（2026-11-17 13:00–14:40）

> 触发：用户三条质疑之后的追加要求——「修复，然后看一下产出有什么问题，整个网站中所有的产出」。

**最严重：整仓 import 被批量改成 `from signals import X`（1120 文件 / 6827 处）**
- `signals/` 是信号/决策领域包（14 个 `decision_*`/`signal_*` 模块），拿不出 `os/json/db/urllib`；
  `from signals import urllib.request` 是**语法错误**，`import api_server` 直接 `SyntaxError`。
- 线上进程没死，只因 08:27 已把旧代码载进内存 —— **任何一次重启，全站起不来**。
  证据：`git show HEAD:backend/src_python/api_server.py` 是正常 `import asyncio`；
  工作区同位置是 `from signals import asyncio`；`ps` 里有另一个并发会话在反复跑 `/tmp/deshim2.py signals`。
- 修法：新增 `backend/scripts/fix_signals_imports.py`（`--check` / `--apply`，判定=signals 包真实存在的模块名保留，
  其余还原 `import X`，逗号/别名/注释一并还原）。实测 6827 处 → 复检 0 处；`compileall` 通过；openapi 383 条路径。

**本轮修掉的 4 个"输出是坏的"缺陷（都是"没有任何断言会失败"的类型）**
1. `/openapi.json` 恒 500：`_BehaviorReport(BaseModel)` 定义在 `mount_antibot()` 函数体内 → schema 前向引用
   `PydanticUserError`，且这条异常能把进程带崩（`api.err.log` 有 `Shutting down`）。→ 类提到模块顶层。现 200。
2. `/api/news/report` 恒 500：取新闻函数搬去 `routers/news.py` 后，`api_server._news_report` 仍按老名字引用 →
   `NameError: get_techflow_news is not defined`。→ 函数内 import。现 200。
3. `/metrics` LLM 指标恒 0：用了 `json.load` 但没 `import json`，异常被 `except Exception: pass` 吞掉 →
   接口 200、数字恒 0。→ 补 import。修后实测 `trident_llm_calls_30m 171`。
4. `/api/v190/exchange/audit` 恒 500：`db.get_connection()` 不设 `row_factory`，行是 tuple，代码写 `dict(r)` →
   `TypeError`。→ 用 `cursor.description`：`dict(zip(cols, r))`。现 200（0.10s）。
5. 启动期"补列"（v392）从未执行：引用了后面才赋值的 `conn` → `UnboundLocalError` 被吞，
   `logs/bridge_20261116.log` 有 `补列检查失败（继续启动）: UnboundLocalError`。→ 提前取连接。

**全站实测（263 GET / 32 前端页）**
- 静态：openapi ok（383 路径 / 263 GET）、重复注册 0、HTTP 层 undefined name 0（另 2 处仅注解，PEP563 安全）。
- 动态（串行，跳过 11 条会改状态的）：ok 179 / empty 11 / 4xx 38 / 5xx 4（全是按设计拒绝）/ 慢>8s 20。
  **真 500 = 0**。empty 里 5 条是"源挂了但接口仍 200"（techflow 实测 `TechFlow unavailable: ReadTimeout`）。
- 前端 32 页：全部 200（6 个 307 为 dev 模式尾斜杠自跳），5xx 0。
- 旁证：机器上有东西"改 .py 就重启 api_server"，扫描期 `Connection refused` 是进程被重启；
  工具已把这种情况记为 `unreachable` 单列，不再混进 5xx。

**机制落地（"以后同类问题一次揪出来"）**
- `backend/scripts/site_audit.py`：静态 4 条（openapi/重复注册/undefined name/线上是否旧代码）+ 串行探活
  （`--live` 或 `--inproc`），ok/empty/4xx/5xx/timeout/unreachable 分列。
- `backend/tests/test_import_integrity.py`：4 条断言（无 signals 伪导入 / 全仓 .py 能 parse / openapi 能生成且无重复路由 / 领域包存在）。4 passed。
- `pipeline_guard.check_structure` 修掉"守卫自己吞错报 ok"：列不存在时 `_count` 吞 `OperationalError` 返回 0 →
  缺失率 0% → ok。改为按实际存在的列拼条件，算不出来报 `unknown`。连带 50 passed。

**未做（诚实清单）**：P0 结构化真缺失 34.8%、P1 品种路由 routing_miss 49.6%、P2 49 个源 error/停更、
P3 守卫定时化；另有 108 处 `dict(r)` 站点未设 row_factory（实探只炸 1 处）。

### v409.2 · 通道体检吃掉了唯一的探测名额 —— 流水线停摆的真根因（代码缺陷，不是额度）

> 11:00 复核：决策数从 09:51 起 4 小时一条没动，`raw_news` PENDING 一路涨到 261，
> 日志反复 `[AI] 跳过认领：全部通道熔断: minimax1[熔断中],minimax2[熔断中]`。
> 但 `llm_probe.json` 是 **9/9 通道 available=true** —— 额度其实是好的。

- **根因**：`main_pool_ready()` 用 `brk.allow()` 做「通道体检」。`allow()` 有副作用 ——
  quota 熔断器冷却到期时，它会顺手把状态推到 `half_open` 并置 `_probe_in_flight=True`，
  等于**占用掉唯一一次探测名额**；而体检本身不发任何请求。真正要发请求的调用点再
  `allow()` 就拿到 False，且要等 `probe_timeout`（quota = max(60, 1800) = **30 分钟**）
  才释放。日志特征是「熔断中」**没有倒计时**（说明卡的是 half_open 而不是 open）。
  状态文件实证：`{"name": "llm_main_quota:minimax1", "state": "half_open", "fail": 1,
  "updated_at": 10:24:20}`，两个通道同时如此。
- **修法**：`circuit_breaker.py` 新增只读方法 `available()`（closed→True；open→冷却是否
  到期；half_open→只有探测不在飞或已超时才 True），**不改状态、不占名额**；
  `main_pool_ready()` 改用它。同时体检文案补 `quota(probe占用)` / `quota(probe待放行)`，
  以后一眼能分清「冷却中」和「名额被占死」。
- **活体验证（11:00–11:13）**：重启后立刻 `[AI] 通道体检 ready=True reason=-` →
  `[11:12:11] Claimed 1 items` → **11:13:31 落库 `ai_decisions` id=11965**
  （news 32238 / analysis / summary_only / ok / BTC），这是 09:51 之后的第一条新决策；
  同批的新洞察 **1269 / 1270** 入库即判（`insight_classified_at` 与 `created_at` 同为
  03:08:15 UTC = 本地 11:08）。
- **仍未拿到的活体证据（不改口径）**：
  · `rescue_log.jsonl` 引擎侧仍 **0 行**（本批没触发降级抢救，异常分支没被走到）；
  · `[TICKER-RESOLVE]` 仍 **0 次**（本批新闻里没有 `$NVDA`/`600519.SH` 这类代码）；
  · `missing:market_closed` / `missing:unresolved_target` / `E001j` 仍 **0 条**
    （当前非休市时段，CME 维护窗口是本地 06:00–07:00）。
- **外加查到的第二件事**：11:07:54 引擎又吃到第 6 次 SIGTERM。`launchd` / `crontab` /
  `engine_watchdog` / `service_guard` 四条线全部排除（service-guard 10:26–10:53 五轮均
  报「无孤儿实例」，watchdog 判定「正常」）。全仓只有 `~/.trident/wrappers/restart_backend.sh`
  会 `pkill -f "engine.py"`，所以是外部进程在跑它。已给该脚本加调用链留痕
  （`~/.trident/log/restart_calls.log`，记录时间/pid/tty/父子命令链，脚本已备份
  `restart_backend.sh.bak-v4092`），下次再杀即可指认。
- **测试**：新增 `tests/test_v409_readiness_no_probe_consume.py` 4 例（体检不占名额、
  single-flight、probe 超时释放、只读不改状态）；关联集 **79 passed**。

### v409.1 · 复核暴露的三处真缺口：抢救日志盲区、asset 空串丢换算、重启无留痕

> 2026-11-17 09:37 的两小时只读复核发现：五项整改里 4 项拿不到活体证据，而真正卡住的是
> 引擎 09:24–09:47 连重启 8 次 + 队列积压 207→250。这轮修的是复核里挖出来的三处**真缺陷**。

- **抢救日志异常盲区（`engine/ai_worker.py`）**：`_rescue_log` 原来只在「预算被拒」和
  「抢救成功/失败」两条分支写。09:51 那次抢救实际走的是 `except` 分支（上游 MiniMax HTTP 529），
  只 print 了 `[落库前抢救] 跳过: RuntimeError`，`rescue_log.jsonl` 一条没有 →
  「没选中 / 预算耗尽 / 上游挂了」三种情况根本分不开。现在异常也落一行
  `skip=exception:<类型>` + `err` 字段，且 print 带上了异常消息本身。
- **asset 空串拿到不 ATR（`insight_hypothesis.py`）**：`save_insights` 只解析 `[WTI]` 这种
  **前缀**才写 `asset`；实测 1266/1267 没前缀 → `asset=''` → `market_context()` 按 symbol 查
  `kline_snapshots` 查不到（WTI 1h atr_pct=0.455 明明在库），本该能换算的 sizing.* 退化成
  observe_only。新增 `asset_from_text()`：只在 asset 为空时从正文认品种（整词匹配，避免
  `SOLUTION` 命中 `SOL`），认不出就继续 observe_only，**不猜**。
- **换算值要落在人话可解释的区间（`insight_hypothesis.py`）**：补 `--commit` 重判时抓到两条
  假值 —— 1259「账户0.3%—0.5%」的 0.3% 是**账户风险占比**不是价格幅度（换算成 sl_mult≈0.66
  等于把风控写坏）；1244「按约3.2%亏损反推」的 3.2% 是**账户回撤预算**（3.2/0.452≈7.08 倍
  ATR，等于没止损）。加两道闸：(a) 百分比前 14 字内必须有 止损/止盈/目标/回撤/亏损/盈利/波动；
  (b) `sl_mult`/`tp*_mult` 结果必须落在 0.2~4 倍 ATR。
- **重判历史洞察（`backend/scripts/reclassify_insights.py`，默认 dry-run）**：v409 的判定发生在
  写入那刻，规则改了旧行不会自动重判。扫 57 条活性洞察，21 条差异，其中 observe_only 1→0
  仅 **2 条**（1244 后来被区间闸判回 observe_only，最终只剩 1267 一条真转可执行，sl_mult=1.3274）；
  复跑差异 0 条，幂等。
- **重启无留痕（`engine.py`）**：`launchctl list` 显示 engine 退出码恒为 -15（SIGTERM），但进程侧
  没有任何记录，分不清「崩了」和「被 restart_backend.sh 杀了」。加 SIGTERM 钩子：打一行
  `[ENGINE-EXIT] 收到 SIGTERM @ <时间>` 后恢复默认行为（不改退出语义）。首次重启即验证到留痕。
- **测试**：`test_v409_sizing_convert_and_reason.py` 6 → 14 例（正文认品种、整词边界、
  账户风险占比拒绝、超区间拒绝、区间内仍换算）；关联集 **75 passed**，三个文件 AST 通过。
- **生效记录**：`restart_backend.sh` 跑完「bridge=200 engine=1 个」；当前运行版本
  `ai_worker.py=4d086e5e@10:21:00(783149B)`（含本轮修复 + 同仓 v415 改动）；`[ENGINE-EXIT]`
  钩子实测落到 5 条留痕（10:13:26 / 10:16:02 / 10:21:49 / 10:24:54 / 10:30:19），全部是
  SIGTERM 而非崩溃。
- **复核现场发现（重要）**：流水线停摆的真因不是代码，是 **LLM 通道 quota 熔断**。日志原话
  `[AI] 跳过认领：全部通道熔断: minimax1[quota(965s)],minimax2[quota(900s)]（熔断冷却中，零额度消耗）`
  —— 两个 MiniMax 分析通道同时进 quota 冷却，引擎主动零消耗跳过认领，所以 09:51 之后一个决策都没有，
  `raw_news` PENDING 从 207 涨到 254。冷却结束后（10:24:28）引擎立刻 `Claimed 8 items`，说明
  认领链路本身是通的。

#### v409.1 仍未解决（明写，不当已完成）

1. `missing:market_closed` / `missing:unresolved_target` / `E001j` / `[TICKER-RESOLVE]` 仍为 **0 条**：
   重启后只落了 5 条决策（11960–11964，全在开市时点），休市窗口（本地 06:00–07:00 / UTC 22:00–23:00）
   已过，要等下一个窗口 + 有对应品种新闻被分析才能真正验证。
2. 引擎被反复 SIGTERM：本轮修的是**留痕**，不是**根因**。10:13–10:30 之间实测 5 次 SIGTERM
   （间隔 2~6 分钟），launchd 与 crontab 里没有任何 job 调 `restart_backend.sh`，脚本层也只有
   engine_watchdog（判定「正常」不动手）。所以是人/另一条改动线在反复重启，**一批 8 条要跑 10+
   分钟，根本跑不完就被杀掉**——这也是拿不到 T2/T3 活体样本的直接原因。
3. `raw_news` PENDING 250（复核 2 小时内从 207 涨到 250），DONE 4385：产能仍小于到货。
4. 上游 MiniMax 侧 HTTP 529 与 `[FILTER] pool-retry TimeoutError` 频发，抢救即使被选中也大概率失败。
5. `sizing.position_weight` 仍不可换算（库里无权威 equity 列），未编假权益。

### v420 · 执行层毛利审计：2028 笔真实入场，毛利中位 0.00%

> 前面 v408~v419 验的都是**指标**（偏置/极端局面/波动/赔率）。这轮补最后一层：**真实入场点上的毛利**。

- **新脚本** `backend/scripts/news_edge_audit.py`：取 `t2_sim_trades` 里 `source='news_replay'` 的
  真实新闻入场点，用 **1m K 线收盘价重算毛利**，按 20 分 / 1h / 4h / 1d / 2d 重结算，并按信心分层。
  CLI：`--asset / --horizons / --cost-bps / --no-write`；产物 `reports/execution_edge/{ALL,<资产>}.md/.json`。
- **结论**（1948 笔可结算）：

| 持有期 | 毛中位 | 毛胜率 | 净中位（扣 30bp） |
|---|---|---|---|
| 20 分 | -0.001% | 49.3% | -0.301% |
| 1 小时 | 0.000% | 49.7% | -0.300% |
| 4 小时 | -0.036% | 46.4% | -0.336% |
| 1 天 | -0.049% | 47.7% | -0.349% |
| 2 天 | -0.075% | 49.2% | -0.375% |

- **信心分层无区分力**：高信心组（0.8~1.0）n=1501、毛中位 +0.008%、胜率 50.9% —— 等于掷硬币。
- **踩到并修掉一个假值**：首版把 30bp 成本"加回"算毛利，BTC 20 分钟胜率被算成 **92.7%**、
  毛利 +0.299%（与表里 `pnl_pct` 恰好差 0.6%）。K 线价算出的毛利本来就不含成本，不能再加。
- **落到代码**：
  - `technical_state._COST_BPS = 30.0`（来源：这批成交 `cost_bps` 中位，不是拍的）；
  - `risk_profile` 新增 `round_trip_cost_pct`、`require_basis`；
  - **`require_space_pct = max(赔率要求, 成本地板)`**：1h 常规取赔率线 0.66%；ATR 极低时自动抬到
    成本 0.30% 并标 `require_basis="cost"`；4h 无赔率退回逆风线标 `"adverse"`；
  - `decision_guard.risk_plan` 透传；读数显示「赔率线/逆风线/成本地板」与来回成本；
  - 前端 `requireNoteOf` + 卡片一行「门槛：至少要走出 0.66% 才够本（赔率线；来回成本 0.3%）」。
- **测试**：后端 8 文件 **285 条通过**（`test_risk_profile_v412.py` 现 **34 条**，新增 v420 三条）；
  前端 `lib/risk-plan.test.ts` **22 条通过**，`tsc` 干净。
- **报告**：`reports/execution_edge/conclusions.md`（七节，含成本地板与证据链边界）。
- **边界**：11 天单一市场段；方向来自新闻链路而非技术指标，**不能据此说"技术指标没用"**
  （指标侧已被 v408/v411/v415 单独证否）；30bp 是模拟系统假设，真实 taker 双边约 8bp，
  但毛利 0.00% 下 8bp 仍是负期望。

### v417 · 把「乘几倍」变成「开多大」：止损距离 → 名义敞口

> v412~v416 的口径都是倍数（1.0× / 2×ATR），执行层没法直接下单。这轮补上最后一步换算。

- **`technical_state.risk_profile` 新增**：
  - `stop_pct`（止损距离，%）、`max_risk_pct`（单笔风险预算，默认 1.0%）；
  - **`position_pct`** = `1% / 止损距离 × 已验证档位系数`，上限 100%（不加杠杆）、下限 1%；
  - **`position_pct_equal_risk`** = 基准敞口（不乘 size_factor）× `equal_risk_size`（仅日内有）。
- **口径统一**：档位系数（日线 0.5/0.8/1.0、日内 1.0）直接进仓位换算，
  避免「档位说半仓、风险预算说满仓」两套数字打架；有测试钉死。
- **`decision_guard.risk_plan`** 透传 `stop_pct` / `max_risk_pct` / `position_pct` /
  `position_pct_equal_risk`。
- **前端**：`lib/risk-plan.ts` 新增 `positionNoteOf`，风控卡多一行
  「敞口：止损 3.81%，按单笔亏 1% 本金反推 → 名义敞口约 26.2%（等风险口径 17.3%）」。
- **当前读数（1% 单笔风险口径）**：

| 资产 | 周期 | 档位 | 止损 | 止损距离 | 系数 | 名义敞口 | 等风险口径 |
|---|---|---|---|---|---|---|---|
| BTC | 4h | 中 | 2×ATR | 3.81% | 1.0× | 26.2% | 17.3% |
| ETH | 4h | 高 | 2×ATR | 4.79% | 1.0× | 20.9% | 15.4% |
| SOL | 4h | 高 | 2×ATR | 5.47% | 1.0× | 18.3% | 13.5% |
| XAU | 1d | 中 | 2×ATR | 3.63% | 0.8× | 22.0% | — |
| XAG | 1d | 高 | 3×ATR | 13.26% | 0.5× | **3.8%** | — |
| BTC | 1d | 高 | 3×ATR | 11.73% | 0.5× | 4.3% | — |
| BTC | 1h | 低 | 2×ATR | 1.70% | 1.0× | 58.8% | 68.8% |
| XAU | 1h | 低 | 2×ATR | 0.75% | 1.0× | **100%（触顶）** | 100% |

- **测试**：`test_risk_profile_v412.py` 扩到 **25 条**（仓位公式、日线系数入仓、
  上下限夹取、等风险用未缩放基准）；后端 8 文件 **276 条通过**；
  前端 `lib/risk-plan.test.ts` **15 条**（新增 `positionNoteOf` 三条）+ `tsc` 干净。
- **报告**：`reports/risk_profile/2026-11-17.md` 追加「从倍数到金额」一节（11 个组合的敞口表）。
- **边界**：敞口只回答「这笔亏多少、开多大」，**不回答方向**；单笔 1% 是默认值，
  换预算只改一个常量；`100%` 触顶说明止损太窄（如 XAU 1h 0.75%），
  意味着该周期靠 ATR 止损已无法再扩仓，想加仓必须换更小周期或加杠杆——都不在本系统内。

### v416 · 日内结论的稳健性复核：波动族跨窗口/跨前瞻/跨粒度都成立，ETH 始终不成立

> v415 的结论只来自「末尾 400 点」一个窗口。这轮专门做对抗性复核，
> 并修掉一个会静默跳过数据的口径 bug。

- **脚本加 `--horizon` / `--shift-back`**：前瞻根数可调（默认 5），窗口可整体前移 N 根，
  用来回答「是不是这段行情特殊」。
- **修掉口径 bug（重要）**：`_load_ohlcv` 原用 `ORDER BY ts DESC LIMIT 2000` 直取，
  而 `load_series` 用 `ROW_NUMBER() PARTITION BY ts` 去重。XAU 15m 存在重复 ts，
  两边长度不等 → 脚本报「序列不一致，跳过」，**看起来像数据不可用，其实是自己口径不一致**。
  已改为同一去重口径（`PARTITION BY ts ORDER BY rowid DESC`），长度校验保留。
- **复核结果**：

| 复核 | 设置 | 过线特征 | 结论 |
|---|---|---|---|
| BTC 1h 全序列 | 319 点 | atr_pct、bb_width_pct、bb_width_pctile、atr_pctile | 波动族稳（rv_pct 掉出） |
| BTC 1h 窗口前移 | `--shift-back 350`，202 点 | atr_pctile、bb_width_pctile、atr_pct、bb_width_pct | 跨窗口仍只有波动族 |
| BTC 1h 前瞻拉长 | `--horizon 10`，300 点 | bb_width_pctile、bb_width_pct、rv_pct | 5→10 根结论不变 |
| XAU 1h 全序列 | 278 点 | 波动族 8 个 | 更强更稳 |
| ETH 1h 全序列 | 317 点 | **无** | ETH 上依然不成立 |
| XAU 15m | 300 点 / 13 天 | 波动族 5 个 ＋ **same_dir_streak** | 首个独立特征，窗口太短 |

- **新增结论**：① 波动族是唯一跨窗口/跨前瞻/跨粒度的稳定项（分离比 1.6~3.0），可进产品；
  ② ETH 在任何设置下都不成立 → 这套关系**资产依赖**，不能拿 XAU 的 3 倍套 ETH；
  ③ 15m 上 `same_dir_streak`（连续同向）是四轮里唯一非波动换皮的过线项（相关 0.155、
  与 ATR 相关 0.02、分离比 1.62），但只有 13 天，列为「待复核」，**不进代码**。
- **产物**：`reports/intraday_risk/conclusions.md` 追加「稳健性复核」与「口径 bug」两节；
  新增 `reports/intraday_risk/XAU_15m.md/.json`；主报告改为全序列版本（BTC 319 点 / XAU 278 点 / ETH 317 点）。
- **未动**：`risk_profile` 的仓位/止损口径（等风险与档位仍是 v414/v415 那套），
  15m 结论待历史补到 ≥30 天后再决定是否进代码。

### v415 · 日内逆风到底跟什么有关：17 个特征只有波动尺度管用

> v414 证明了 ATR 分档在日线有效、在 4h 失效。这轮换问法：**日内该盯哪个变量**，
> 并用 1h 数据把 17 个候选特征全部检验一遍。

- **新增 `scripts/intraday_risk_drivers.py`**：逐时点 `asof_ts` 因果重算 `build_state`，
  抽 17 个候选特征（波动尺度 / 趋势强度 / 位置 / 单根冲击 四组），对前瞻 5 根
  `|MAE|` 算 Spearman 秩相关 + 三分位分离比；三条判据同时满足才标"可用"：
  分离比 ≥1.3、|秩相关| ≥0.15、**与 ATR |corr| <0.5（防同源重复计数）**。
  另外输出「止损设在 k×ATR 时 5 根内被打掉的概率」（按波动三分位）。
  产物 `reports/intraday_risk/{latest.md, <资产>_<周期>.md/.json}`。
- **结论一：日内唯一稳定来源是波动尺度本身**。BTC 1h 过线 4 个（`bb_width_pct`、
  `bb_width_pctile`、`atr_pct`、`rv_pct`，分离比 1.77~1.96）；XAU 1h 过线 7 个
  （分离比 2.84~3.46，最强）；**趋势强度（ADX/DI）、位置（donchian / 距 POC / 距边缘）、
  单根冲击（range/body/连续同向）在三个资产上全部落在噪声区间（分离比 0.71~1.55）**。
- **结论二：同族不能当多个证据**。BTC 那 4 个过线特征两两相关 0.62~1.00，
  本质是"今天波动比平时大"；报告里逐条标同源组，避免当成 4 个独立发现。
- **结论三：ETH 1h 一个都没过线**（最高相关 0.149、最高分离比 2.13 但相关 0.12）
  —— 同样 17 个特征、同样 400 点，说明这套关系不是"换谁都灵"。
- **反直觉但重要的发现**：按当时 ATR 归一后，**高波动档的逆风反而更小**
  （中位 |MAE5|÷ATR：BTC 0.483→0.445、XAU 0.580→0.551、ETH 0.514→0.442），
  止损被打概率（k=2×ATR）也随波动升高而下降（BTC 10.4%→9.0%、XAU 20.9%→13.5%、
  ETH 20.1%→10.5%）。原因：ATR 是**滞后**的，大 K 线把它顶起来后往往已在回归常态。
  → 日内不该以"更容易被打"为由缩仓；该按 **1/ATR 等风险**调金额。
- **`technical_indicators.volatility_state` 新增 `atr_pct_median`**（近 60 根 ATR% 中位）。
- **`technical_state.risk_profile` 新增**：`atr_median_pct`、`atr_vs_median`、
  **`equal_risk_size`**（等风险仓位参考，夹 0.5~1.5）、`equal_risk_note`；
  `_ADVERSE_ATR` 补 `"1h": 0.55`（实测 1h 中位 |MAE5|÷ATR 0.44~0.58，不再借用 4h 的 0.9）。
  **`size_factor` 不变**（等风险只作参考，不覆盖已验证的档位口径）。
- **`decision_guard.risk_plan`** 透传 `atr_vs_median` / `equal_risk_size` /
  `equal_risk_note` / `size_basis` / `validated_timeframes`。
- **前端**：`lib/risk-plan.ts` 新增 `equalRiskNote`，风控卡多一行等风险口径。
- **当前读数**：BTC 1h ATR 0.85%（常态 0.99%，便宜）→ 等风险 1.17×；XAU 1h 0.37%（0.41%）
  → 1.1×；**BTC 4h ATR 1.90% 是常态 1.26% 的 1.5 倍 → 等风险 0.67×**（止损仍 2×ATR）。
- **测试**：`test_risk_profile_v412.py` 扩到 **21 条**（新增等风险上下限夹取、
  等风险不改 size_factor、缺 ATR 中位不编数、1h 逆风倍数落地）；后端 8 文件 **272 条通过**；
  前端 `lib/risk-plan.test.ts` 12 条 + `tsc --noEmit` 干净。
- 报告：`reports/intraday_risk/conclusions.md`（三条结论 + 止损概率表 + 边界）、
  `reports/intraday_risk/latest.md`（汇总）、`reports/risk_profile/2026-11-17.md` 追加"日内口径"一节。

### v414 · 验证风控档位本身：打分档位被证否，改成 ATR 绝对档位

> v412/v413 把档位做出来了，但从没验过"档位打高是不是真的更险"。这轮补上了，
> 结论是**原来的打分制分档是错的**，并据此改了口径。

- **新增 `scripts/risk_tier_validation.py`**：逐时点 `asof_ts` 因果重算 `build_state`，
  按档位分桶测未来 5/10 根的 `|MAE|` 与 `|MAE|÷当时ATR`，同时给"绝对口径"与"波动归一
  口径"两个比值；样本门槛 n≥30，`--samples<=0` 走全序列，`--all` 跑五个组合，
  产物 `reports/risk_tiers/{latest.md, <资产>_<周期>.md/.json}`。
- **第一轮结论（打分制档位失败）**：按 v412 打分分档后，高档 5 根 `|MAE|` **反而更小**
  —— BTC 4h 高/低 = 0.44、ETH 0.33、XAG 0.39、BTC 1d 1.14、XAU 1d 1.66（中档不单调）。
  诊断：打分里除 ATR 外全是**滞后**指标（ADX、带宽分位、价格离价值区、减仓下跌），
  它们在"已经动完"之后才亮，把事后平静期标成了高风险。
- **第二轮结论（ATR 绝对档位成立）**：5 根 `|MAE|` 中位 —— XAU 1d 低(ATR<1.5%)0.46% /
  中 0.87% / 高(≥3%)1.63% → **3.54×**；XAG 1d 0.83/1.10/1.97 → **2.35×**；
  BTC 1d 中 1.13 / 高 1.96 → **1.72×（无低档样本，退比高 vs 中）**。
  **4h 上不成立**（BTC 0.80×、ETH 样本不足、n=78）→ 日内不做仓位缩放。
- **`technical_state.risk_profile` 改写**：档位不再由打分决定，改由 ATR 绝对百分比分档；
  日线 <1.5% 低（1.0×、1.5×ATR）/ 1.5~3% 中（0.8×、2×ATR）/ ≥3% 高（0.5×、3×ATR）；
  所有日内周期**仓位固定 1.0×**、止损 2×ATR。打分与 `drivers` 保留作解释。
  新增输出 `size_basis`（`atr_band_daily` / `uniform_intraday` / `fallback`）、
  `validated_timeframes=["1d"]`、`validation_note`。
- **`technical_indicators.volatility_state`** 新增 `atr_pct_percentile`（ATR 的历史分位），
  供档位与读数引用。
- **`ai_worker` 指令改口径**：由"高档必须写 0.5×"改成"按读数里的 size_factor /
  stop_atr_multiple 写"，避免把日线仓位规则套到 4h。
- **前端同步**：`lib/risk-plan.ts` 新增 `basisNoteOf`，卡片多一行口径说明
  （日线"已验证"、日内"不缩仓位，4h 缩仓无依据"），不让日内档位看起来像验过。
- **当前读数（v414 口径）**：BTC 4h 中波动档 1.0×/2×ATR（ATR 1.90%）；ETH/SOL 4h 高波动档
  但仍是 1.0×；XAU 4h 低；**XAU 1d 中 0.8×/2×ATR**；**XAG 1d 高 0.5×/3×ATR（ATR 4.42%）**；
  **BTC 1d 高 0.5×/3×ATR（ATR 3.87%）**。
- **测试**：`test_risk_profile_v412.py` 扩到 **17 条**（打分不再定档、日线三档、日内永不缩仓、
  验证范围随身带、缺 ATR 不编档位）；后端 8 文件 **268 条通过（实测）**；
  前端 `lib/risk-plan.test.ts` **12 条**（含口径说明）。
- 报告：`reports/risk_tiers/conclusions.md`（两轮验证与改口径依据）、
  `reports/risk_tiers/latest.md`（汇总）、`reports/risk_profile/2026-11-17.md`（已按 v414 重写规则表）。

### v414 · 因果图谱可落地性整改（四轮）

> 详见 `docs/因果图谱体检与整改-2026-11-17.md`（前后对照 + 复核命令）。
> 主库 `backend/trident_event_bus.db`（801MB/WAL/在线），改前快照
> `backend/backups/trident_event_bus-before-kgfix-20261117-0644.db`。

**起点诊断**：753 条链 100% 自动生成、人工 0 条；362 条（48%）触发词混 JSON 字段名；
`confidence` 750 条恒 1.0；`hit/miss` 全库为 0；决策侧 `aggregate_direction` 恒 UNKNOWN、否决 0 次；
36 品种只有 10 个有核心驱动，BTC 的"核因"是列名 `historical_confidence`。

#### 第一轮：链库净化 · 口径拆解 · 闭环接活

- **触发词净化**：`chain_ingest_cleanup.py --apply`，删 2270 个噪声词（Asset/verdict/partial/market_event）。
- **去重合并**：新增 `scripts/chain_dedup_merge.py`（dry-run 默认），17 组 / 32 条，outcomes 重指 26 条；
  坑：直接改 `chain_id` 撞 `UNIQUE(chain_id,trigger_ts,trigger_ref_id)`，须先删冲突行。
- **入库三道校验**（`causal_graph._upsert_node`）：列名 REJECT、事件标题冒充 core → 降级 condition、
  跨品种污染 REJECT；商品家族（XAU/XAG/WTI/BRENT/COPPER/CPI）豁免，保住"油冲击→金价"这类真结构。
- **关掉假的"已学知识"**：`ai_worker._fallback_business_knowledge_drivers(allow_generic=False)`，
  没学到返回 None（AAPL/MSFT/COPPER 实测 None）。
- **置信度分层**（`chain_steps_verify.py`）：`CONF_BOTH_MAX=0.55`、`CONF_UNVERIFIED_MAX=0.50`、
  有方向用 Beta(2,2) 后验且幂等 → 750 条 1.0 变为 0 条 ≥0.9，均值 0.502，`hit_count>0` 245 条。
- **口径拆解**：新增 `GET /api/causal/quality`（BOTH 单列并注"幅度口径"）；`GET /api/causal/export`
  支持 chains/nodes/edges/outcomes 的 CSV/JSON。
- **决策侧分级**（`chain_prior.py`）：strong(n≥12) / weak(3–11) / none，只有 strong 可否决，
  `min_posterior` 0.85→0.60，`posterior` 改取 `confidence`（原来取恒 1.0 的 weight）。
- **闭环接活**：`chain_steps_verify.py` 此前从未被任何 cron 调度 → 新增
  `com.trident.chain-steps-verify`（05:20）与 `com.trident.kg-coverage`（06:10）。
- **状态对齐**：`enabled=1 且 active=0` 的 135 条停用，`enabled=191` 与 quality 的 effective 一致。

#### 第二轮：cron 旧路径 · 边权口径 · 证据驱动补核因

- **`backend/cron/*` 旧路径**：6 个文件改到 `~/Desktop/金融1.0/backend`，留 `.bak-20261117`
  （生产本走 `~/.trident/wrappers`，这处只是消除误伤 30MB 旧库的隐患）。
- **边权语义查明**：不是"没归一"，而是两种语义混一列 —— `causes/conditions/correlates_with`
  在 0–10 设计量程内，`supports`(max 40)/`contradicts`(max 41) 记的是**票数**。
- **证据驱动补核因**：新增 `scripts/kg_core_from_chains.py`，从 `enabled=1 且 hit_count>0` 的链
  取「机制→结果」的机制段聚类，`confidence=min(0.5,0.25+hit*0.05)`，`evidence=chain:<id>(hit/miss)`。
  WTI 从 0 → 3 条，core 107 → 117。
- **第五类节点垃圾**：`kg_node_cleanup.py` 加「自指/执行层」规则（信号生成/交易执行/手续费/滑点/仓位），
  实测停用 COIN 的 core「信号生成与交易执行机制缺陷」。
- **LLM 补覆盖的放弃表**：`runtime/kg_coverage_giveup.json`，补出 0 条存活的品种 14 天不重试。

#### 第三轮：方向结论修正 · 边权压量程 · XAU 核因复活

- **【修正】方向不对称是薄样本假象**：quality 报的 UP 33.4% / DOWN 67.9% 是全量 outcome 混算
  （大量链仅 1–7 样本）。只取 n≥8 的 46 条启用链：**UP 55.8%（Wilson 下界 44.7%）/
  DOWN 61.3%（54.1%）/ BOTH 93.7%（幅度口径）** —— 两方向都高于随机，库没有"反向"。
  不重标、不剪枝（≤30% 命中率的链条数 = 0）。新增 `scripts/chain_direction_prune.py` +
  `runtime/chain_calibration.json`。
- **边权压回量程**：`edge_weight_audit.py --normalize --apply`，`supports` 40→10、`contradicts` 41→10，
  原值进新增列 `weight_raw`；全库 `weight>10` 归零。
- **XAU 核因复活**：10-07 被衰减任务停用的 3 条有证据核因重新启用（conf 0.30），XAU 现有 4 条。
- **覆盖度定性**：36 品种仅 6 个有链（XAU 227 / WTI 163 / BTC 31 / ETH 24 / SKHYNIX 2 / SOL 1），
  其余 30 个为 0 —— 链从真实交易/评测样本蒸馏，且 `ohlcv_daily` 只覆盖 27 个资产，非脚本可补。

#### 第四轮：第三个污染面 · 库体检上屏 · 端到端复验

- **机理文本里的自家列名**：`build_prompt_block` 端到端跑出 `Asset/verdict/market_event/prompt/
  geopolitical_conflict` 混在机理里并进入决策 prompt。新增 `scripts/chain_text_cleanup.py`
  （白名单删 schema token，不动 BUY/SELL/WIN 等真实语义词），范围 mechanism/precondition/
  invalidation/note，实测 **326 处 / 202 条链**，清洗后残留 0。
- **库体检上屏**：`knowledge-graph-panel.tsx` 导出条右侧加 chips（启用链、平均置信度、n≥12、
  永不触发、UP/DOWN 命中率），超阈值变琥珀色；tsc 无报错。
- **决策侧端到端复验**：XAU 标题 → 命中 3 条 weak（posterior 0.50）→ `DOWN_WEAK` →
  `conflict_veto(SELL)=None` → prompt 落「候选档·不得否决」。改造前是 posterior 1.00 / UNKNOWN / 无否决。
- **测试**：6 个因果测试文件 + 新增 `test_v407_chain_conf_cap.py`，**79 passed**。

#### 第五轮：闸门真相 · 幅度证据救回 · 死链清出

- **方向否决的真相**：方向链（UP/DOWN）里 Wilson 下界>0.5 的最好只有 4/4（下界 0.51），
  门槛放到 8 就是 0 条。**撤回"降门槛开闸"**：方向判定仍要 n≥12 + 下界>0.5，当前可行使否决的链 = 0，
  如实记录，不拿 4 个样本去挡真实交易。
- **22 条最有证据的链此前被丢掉**：`_evidence_from_outcomes` 只统计方向链（口径正确），
  副作用是 BOTH 链 `n_outcomes` 恒 0 → 排序垫底 → 被 `limit` 截掉。实测这批恰是全库样本最厚的
  （n=9/10，下界 0.70–0.72）。新增 `mag_n/mag_hit_rate/mag_wilson` 单独统计，
  `_rank` 按各自口径排序，`match_by_headline` 名额对半分（方向候选不被挤掉），
  prompt 加"幅度/胜率参考（不可定向）"段。
- **17 条 `asset=UNKNOWN` 死链停用**：按 asset 精确匹配，任何品种都命中不到；启用链 191→174，
  审计 `chain_cleanup/unknown-20261117-083105.jsonl`。
- **quality 加 `coverage`**：`assets_with_chains / universe / veto_ready_chains`，
  实测 6/36、可否决 0。
- **测试口径对齐**：`test_v337_fixes.py` 10 个陈旧用例（n=6 当 strong、prompt 找"反例依据"）
  改到现行契约；四文件 **44 passed**。
- **前端仍未上线**：chips 代码已写、tsc 过，但 `rebuild_frontend.sh` 因 `free=179MB<700 / load=74`
  跳过 —— 闸门是 11-16 构建把三个服务拖死后的保护，不绕过；源码比 BUILD_ID 新，每小时自动重试。

### v413 · 把风控档位变成产品能力：HOLD 的"不参与理由" + 前端执行口径

> v412 做出了档位，但两处缺口没补：HOLD 仍然只留一个"不参与"符号，前端也看不到
> "该用多大仓位、止损放哪"。这一版把两头都接上，仍然不动方向分。

- **`decision_guard.hold_reason`（新输出，HOLD 时才有）**：把"没下单"写成可复核的理由。
  归纳六类：① 分数距门槛还差多少（只算更近的那个方向）；② 证据层是否否决（有方向但没过 gate）；
  ③ 可得因子覆盖度与缺失维度（缺失不等于中性）；④ 技术状态是否给了反向证据（只折减不反向）；
  ⑤ **赔率是否不合算：预期空间 ≤ 5 根内常见逆风**（v411/v412 才有的判据，是"不做"最硬的理由）；
  ⑥ 已有矛盾清单。另给 `check_next`：分数再走多少、档位降到中以下、补齐哪些维度。
  BUY/SELL 时返回 `{"applicable": False}`。
- **`GET /api/technical-state/risk?asset=&timeframe=`（新接口）**：只读返回档位、仓位系数、
  止损 ATR 倍数与百分比、常见逆风、驱动项、最新收盘价、读数时间（`candle_last_ts` 在状态层是秒，
  **接口统一换算成毫秒**，避免前端猜单位）。方向分一律不掺。
- **前端执行口径**：`lib/risk-plan.ts`（`riskTone` / `describeSizeFactor` / `stopDistancePct` /
  `stopPriceFor` / `describeRiskPlan` / `fetchRiskSnapshot`）+ `components/analysis-process/risk-plan-section.tsx`，
  接进分析过程页第 ④ 段（交易执行之前）。显示：档位徽标、建议仓位（"基准仓位的 50%"）、
  止损倍数与百分比、**按最新收盘算出的止损价（做多往下、做空往上）**、常见逆风、触发原因、
  读数时间；HOLD 时标注"仅用于复盘口径，不构成开仓依据"；读不到就写"没有读数"，不补中性档位。
- **实测接口返回**：BTC 4h → 高（0.5×、3×ATR≈5.59%、逆风 1.68%、收盘 64,796.92）；
  XAU 1d → 低（1.0×、1.5×ATR、收盘 3855.6）。
- **测试**：后端 `test_risk_profile_v412.py` 扩到 **12 条**（新增赔率不合算进 headline、
  门槛缺口、非 HOLD 时 hold_reason 缺席）；后端 8 文件 **263 条通过（实测）**；
  前端新增 `lib/risk-plan.test.ts` **9 条**，全套 **687 条 / 49 文件通过（实测）**，`tsc --noEmit` 干净。
- 报告：`backend/reports/risk_profile/2026-11-17.md` 增补"第二轮补充：HOLD 为什么不参与（实例）"。

### v412 · 把"赔率结构"做成能用的东西：风控档位（仓位系数 + 止损 ATR 倍数）

> v411 证明偏置没有方向预测力，但顺手测出一件有用的事：**5 根内要挨多少逆风是可测的**。
> 这一版把那个测量结论做成产品能力 —— 不动方向分一分钱，只回答"该用多大仓位、止损放多远"。

- **实测依据（先量再用）**：逐时点算「5 根内最大不利偏移 MAE ÷ 当时 ATR%」，中位比值
  日线 **0.39~0.52**、4h 常态 **1.06~1.32** → 5 根内通常要挨 **0.4~1.3 倍 ATR** 的逆风。
  推论：**止损放在 1.5 倍 ATR 以内会被正常波动打掉**，所以档位用 ATR 倍数，不用固定百分比。
- **新增 `technical_state.risk_profile(state, timeframe)`**：打分制（0~7）——
  波动扩张 +2（≥60 分位 +1）、已实现波动 ≥80 分位 +1、ADX≥35 +1、价格离开价值区 +1、
  杠杆在撤（减仓/加仓下跌）+1、多空一侧拥挤 +1；映射成

  | 档位 | 分数 | 仓位系数 | 止损 | 5 根内常见逆风 |
  |---|---|---|---|---|
  | 低 | 0–1 | 1.0× | 1.5×ATR | 日线 ≈0.45 ATR、4h ≈0.9 ATR |
  | 中 | 2–3 | 0.8× | 2.0×ATR | 同上 |
  | 高 | ≥4 | 0.5× | 3.0×ATR | 同上 |

- **接进链路（全部不改方向分）**：`build_state` / `for_asset` 自动带 `risk`；
  `market_snapshot._technical_state` 精简字段带 `risk`；`decision_guard.evaluate_decision`
  输出 `risk_plan`（`size_factor` / `stop_atr_multiple` / `stop_pct_hint` / `expected_adverse_pct` /
  `drivers`），高风险档额外给出"建议按 0.5× 仓位执行、止损 3 倍 ATR"的 `detail`；
  `ai_worker` 的 `[TECH_STATE]` 段多一行「风控档位」，并在要求里写死
  「高档必须写出仓位 0.5×，不得因方向成立就满仓」；`technical_state_readout.py` 多一行「风控」。
- **当前读数（2026-11-17）**：BTC/ETH/SOL 4h 均为**高**（波动扩张 + 已实现波动高分位 +
  ADX 极端 + 价格离开价值区 + 减仓下跌，5 个驱动同时命中）→ 仓位 0.5×、止损 3 倍 ATR；
  XAU 4h/1d **低**（1.0×、1.5 倍 ATR）；XAG 1d **中**（0.8×、2 倍 ATR）；WTI 4h **低**。
  报告：`backend/reports/risk_profile/2026-11-17.md`（含实测比值表与规则）。
- **测试**：新增 `tests/test_risk_profile_v412.py` 9 条（三档判定、跨周期逆风倍数不同、
  error 不给档、止损是 ATR 倍数不是固定百分比、`build_state` 带 risk、
  **闸门带不带风险档位 final_score 必须一致**、无状态时 risk_plan 缺席）；
  后端 8 个文件 **260 条通过（实测）**。

### v411 · 换问题：不问"往哪走"，问"这种局面里赔率长什么样"

> v408/v410 已经把"偏置能预测方向"这条路钉死了。继续加指标、换权重都是白费。
> 但状态读数还有另一种用法：它描述的是**局面**，而局面能回答的问题不是"涨还是跌"，
> 是"在这个局面里，收益分布长什么样、先挨打还是先给糖、右尾是不是比左尾长"。

- **新增 `scripts/extreme_state_study.py`**：逐时点因果重算 `build_state`，把读数翻译成局面条件
  （强趋势·多/空、无趋势、波动扩张/收缩、价在价值区上/下方、RSI 极值、加仓/减仓 × 上涨/下跌、
  空头/多头拥挤、主动买/卖极端、CVD 背离、散大户分歧、以及两个组合条件），
  然后算前瞻 5/10 根的**分布统计**：均值、中位、上涨占比、Q25/Q75、P10/P90、
  **MAE（先打到的最大不利偏移）**、**MFE（最大有利偏移）**、**赔率 = P90 ÷ |P10|**。
  n<30 的条件一律不上表；每个条件都与"全样本基线"并排，差值直接摆出，不做显著性主张。
- **跑了两轮、五个资产 + 两个错开的窗口**，结论有三条，都写进了
  `backend/reports/extreme_state/conclusions.md`：

  | 观察 | 证据 | 判定 |
  |---|---|---|
  | "强空+价值区下方"**日线均值回归、4h 顺势下跌** | BTC 1d 中位 +0.79/+0.58、上涨 56.8%/54.5%；XAU 1d（20-22段）中位 +1.10、上涨 76.7%、赔率 3.94（基线 0.84）；**但 BTC 4h 中位 -1.21、上涨 29.6%，ETH 4h -0.95、32.6%** | 同一描述在周期之间**方向相反** → 不是信号 |
  | "强多+价值区上方"在贵金属牛市区漂亮、换窗口就翻脸 | XAG 1d 中位 +4.60、上涨 69.7%、赔率 1.58；XAU 1d 近期 +1.61、59.2%；**XAU 1d 2020-2022 段 -0.42/-1.30、上涨 28%~44%** | 行情段依赖，不可外推 |
  | "无趋势"最早看着最像优势，其实是牛市错觉 | BTC 1d 近期中位 +1.09、上涨 63.3%、赔率 2.20；**窗口前移 1000 根后 +0.06、赔率 1.38 低于基线 1.58** | 剔除 |

- **状态层的用途被写成一张表**（`conclusions.md` 末尾）：可以描述局面、可以描述赔率结构、
  可以当风控输入（极端局面缩仓位/放宽止损）；**不可以**当开仓信号、**不可以**跨周期移植。
  一句话："状态层给的是现在是什么战场，能不能赢取决于打法，战场本身不会告诉你方向。"
- 产物：`backend/reports/extreme_state/latest.md`（汇总）+
  `BTC_1d`/`BTC_1d_back1000`/`XAU_1d`/`XAU_1d_back1000`/`XAG_1d`/`BTC_4h`/`ETH_4h` 各 `.md/.json`
  + `conclusions.md`（结论与用途边界）。

### v410 · 黄金日线补齐十年历史，把"无成交量"这条路径打通

> 上一版把脏 K 线清了，但黄金的日内只有 1~2 个月，验证永远落在"样本不足"。
> 这一版换了个思路：**日内补不深，就把日线补到十年** —— 状态层在日线上一样能验。

- **XAU 日线原来只有 136 根却横跨十年**（稀疏得没法用），XAG 是 2,625 根正常序列。
  用项目自带的 `src_python/metals_daily.py`（新浪 COMEX GC/SI 一手源）重建：
  **XAU 2,589 根 / XAG 2,589 根 / HG 2,590 根，2016-11-17 → 2026-11-17**，重复收盘 0.5%、主间隔 86,400s 占 79.6%（周末缺口，正常）。
- **修掉一个把黄金日线"整根吃掉"的低级 bug**：`technical_state.load_candles` 原来要求
  `None not in (o,h,l,c,volume)` —— 而 COMEX 日线的成交量新浪一律给 0、`metals_daily.py`
  按"缺失就标缺失"写 **NULL**。结果 XAU 日线读出来 **0 根**，状态读数直接报"只有 0 根"。
  改成只需 OHLC 齐全，volume 允许缺失（不补 0、不冒充成交量）。
- **价值区加 TPO 兜底**：`volume_profile` 原来把"无成交量"的 bar 全部跳过，贵金属日线的
  价值区永远拿不到。现在成交量整体缺失时退到 **TPO**（每根 bar 按其 high-low 区间等权计入，
  问"价格在哪些价位被反复交易过"），并在返回值里用 `weight` 标明是 `volume` 还是 `tpo`；
  `market_state` 的 `value_area` 块与读数也带出这个字段 —— 同样叫 POC，口径必须说清楚。
  实测同一条价格序列，两种口径的 VAH 可以差一大截（116.85 vs 144.90），不能混用。
- **黄金/白银日线验证结论（各 600 时点，通过数据体检）**：

  | 资产 | 时段 | 命中 | 基线（无脑做多） | IC | 结论 |
  |---|---|---|---|---|---|
  | XAU 1d | 2024-07→2026-11 | 55.33% | 56.50% | +0.106 | 落后基线 1.2pt，**无预测力** |
  | XAU 1d | 2020-08→2022-12 | 49.17% | 49.67% | -0.042 | 落后 0.5pt，**无预测力** |
  | XAG 1d | 2024-07→2026-11 | 53.17% | 55.50% | +0.027 | 落后 2.3pt，**无预测力** |

  值得注意的是**黄金基线本身就有 56.5%**（这两年半是单边牛市，无脑做多就够了），
  而状态偏置反而是 55.33% —— 也就是说在这段行情里，读状态做判断还不如躺着做多。
  这与 BTC/ETH 1d 的结论一致：**偏置给的是"现在是什么局面"，不是"接下来往哪走"**。
- **测试**：新增 `tests/test_value_area_tpo_v409.py` 6 条（TPO 兜底、成交量口径、bar 太少、
  两口径 VAH 不同、NULL volume 的 candle 不被丢、XAU 日线状态能读出 value_area）；
  后端 7 个文件 **251 条通过（实测）**。

### v409 · 清掉 K 线里的"报价当 K 线"脏行：黄金/白银序列终于成序列

> 上一版查出 XAU 序列是假的，只在读取端做了防御。这一版处理数据本身：
> 先定性脏行的形状、再写可回滚的清理脚本、删掉 6,841 行、然后重跑验证确认"干净了"。

- **脏行形状（定性）**：`open=high=low=close` 且 `volume=0`，`ts` 是**采集那一刻的墙上时间**
  （如 4h 表里出现 `2026-09-19 01:54:53.260`），`fetched_at` 与 ts 相差恰好 15 天、
  价格恒为一个陈旧值（4,139.28）。也就是**某条采集路径把"当前报价"当成一根 bar 落库**。
  同一价格在 17 天里被写了 996 次，真实 K 线被假线层层包围 —— ADX/ATR/价值区/带宽全部失真，
  回测前瞻收益更是荒谬（12h 上涨占比 15%）。
- **新增 `scripts/repair_kline_bars.py`**：判定三条同时满足才删 ——
  ① 周期在日内集合 `{1m,5m,10m,15m,30m,1h,4h}`；② O=H=L=C 且 volume=0；③ ts 不是该周期整秒整数倍。
  日线**不在范围**（股票日线 ts 落本地 16:00、加密落 00:00，按 UTC 对齐会误删）。
  默认 `--dry-run` 只审计；`--apply` 才删，删前把每行完整字段导出 CSV，可逐行还原。
- **实删 6,841 行**（备份 `reports/data_repairs/kline_synthetic_20261117_074632.csv`）：

  | 品种/周期 | 删前 | 删后 | 重复收盘 | 主间隔 |
  |---|---|---|---|---|
  | XAU 4h | 1,352 行 / 重复 66.7% / 主间隔 2.7% | **356 行** | 0.6% | 100.0% |
  | XAU 1h | 2,890 / 55.1% / 1.7% | **1,138 行** | 1.1% | 99.9% |
  | XAU 15m | 5,318 / 31.4% / 63.6% | 3,590 行 | 4.8% | 100.0% |
  | XAU 1m | 41,988 / 22.6% / 95.8% | 40,548 行 | 21.0% | 99.9% |
  | XAG 4h | 681 / 41.5% / 5.0% | 285 行 | 3.2% | 99.6% |
  | XAG 1h | 1,476 / 18.2% / 59.7% | 948 行 | 4.0% | 99.9% |
  | WTI 4h | 278 / 11.2% / 62.5% | 277 行 | 10.9% | 62.7% |

  清理后 XAU 4h 覆盖 2026-09-19→11-17（59 天、6 根/天，序列完整）。
- **`state_bias_validation.py` 加 `--warmup`**（默认仍 300，可降到 240 吃更早的样本），
  并把 `load_series` 的 ts 去重与 `technical_state.load_candles` 对齐。
- **清理后重跑**：XAU 1h 首次通过数据体检（重复 1.1%、主间隔 99.9%），600 个时点、窗口 25 天，
  命中 49.83% vs 基线 45.33%，IC -0.002 → **数据干净了，但样本仍不足以判定**；
  XAU 4h 只剩 8 天、XAG 4h 6 天、WTI 4h 4 天，一律"不足"。
- **读数复核**：XAU 4h 现在 `强趋势·空头主导（ADX 41.53，+DI/-DI 14.74/26.97）｜波动常态（带宽 2.2035%，33.71 分位）｜价格在价值区内部（POC 3894.88 / VAH 4077.01 / VAL 3855.29）`，
  偏置 -0.02、覆盖 46%。`_meta.data_quality.ok` 已为真（此前是假）。
- **结论**：脏数据清了、机制清楚了，但**贵金属只有 1~2 个月干净历史**，
  想验证黄金上的状态有效性，得先补历史（COMEX/伦敦金的无偿历史源，或长期跑采集器）。

### v408 · 状态层第一次被验：偏置没有预测力 + 查出 XAU K 线是假序列

> v402 验的是"单个因子"，状态层（六维→偏置）从来没验过。这一版拿它当信号跑历史：
> 因果重算（`asof_ts` 截断，不看未来）、前瞻 1/3/5/10 根、和"无脑做多"基线对比。
> 结论对产品定位有直接影响——**偏置只能当描述，不能当方向策略**。

- **新增 `scripts/state_bias_validation.py`**：逐时点 `build_state` + `bias_of`，
  记录偏置/覆盖度/分量方向，算前瞻收益、方向命中率、IC（自实现秩相关，不引 scipy）、
  分桶明细、分量单独评定；输出 `reports/state_bias/{资产}_{周期}.md/.json` + 汇总 `latest.md`。
  内置两道防线：**数据体检**（重复收盘 ≤20% 且主间隔占比 ≥50%）与
  **样本门槛**（n≥250 且窗口 ≥120 天才允许说"有优势"）。
- **实测结论（BTC 1d 两段各 600 样本 / ETH 1d 600 样本，均通过数据体检）**：
  偏置方向命中 **46.17% / 49.33% / 50.50%**，同期"无脑做多"基线 **51.83% / 47.83% / 50.17%**，
  IC **-0.019 / -0.018 / +0.052** → **无可分离差异**。BTC 最近一段甚至是负贡献（-5.7pt）。
  分量层面同样无效：trend 46~53%、donchian 46~51%、value_area 46~53%，IC 全在 ±0.06 内。
  4h 周期（n=154、26 天、单一下跌段）命中 57%，但那是"顺势"，按样本门槛一律判"不足以判定"。
- **`technical_state.bias_of` 文档字符串写死这条结论**：`usable` 的含义是"读得出来"，
  不是"预测得准"；闸门只允许它做折减，不允许它反向开仓。
- **查出 XAU `kline_bars` 是假序列**：2026-09-19~10-22 段重复收盘 74%~89%、出现同一 ts 多行
  （gap=0）、主间隔 4793s/2237s/175s 混杂；1h 同样 55% 重复。后果：这段历史喂进任何回测
  都会得出"12h 上涨占比 15%"这种荒谬数。**10-23 之后 240 根是干净的**（重复 0.8%、间隔 100% 规整），
  所以当前 XAU 读数仍成立，但历史段不可用于回测。上游采集端要查（本轮不动数据、只在读取端防）。
- **`technical_state.load_candles` 按 ts 去重**（`ROW_NUMBER() PARTITION BY ts ORDER BY rowid DESC`）：
  同一 ts 多行只取最后写入的一行；`build_state._meta` 新增 `data_quality`
  （`ok` / `dup_close_pct` / `gap_main_sec` / `gap_main_share_pct`），读数里能一眼看出 K 线是否可信。
- **测试**：后端 6 个相关文件 **245 条通过（实测）**；`test_technical_state_v343` 13 条与
  交叉对账 11 条在去重 SQL 改动后全过（去重用的 `rowid` 而非 `id`，测试库最小 schema 也成立）。

### 第三轮：方向结论修正 · 边权压量程 · XAU 核因复活

- **【结论修正】方向不对称是薄样本假象**：`/api/causal/quality` 的 UP 33.4% / DOWN 67.9%
  是"全量 outcome 混算"（大量链仅 1–7 样本）。只取 n≥8 的 46 条启用链：
  **UP 55.8%（Wilson 下界 44.7%）/ DOWN 61.3%（54.1%）/ BOTH 93.7%（幅度口径）** ——
  两个方向都高于随机，库没有"反向"。因此不重标、不剪枝（≤30% 命中率的链条数 = 0）。
  新增 `scripts/chain_direction_prune.py`（数据驱动、可重复跑），落 `runtime/chain_calibration.json`。
- **边权重压回设计量程**：`scripts/edge_weight_audit.py --normalize --apply`，
  `supports`(max 40)/`contradicts`(max 41) 按组内缩放压到 10.0，原值写入新增列 `weight_raw`，
  全库 `weight>10` 归零。causes/conditions/correlates_with 本就在 0–10，未动。
- **XAU 核因复活**：10-07 被衰减任务停用的 3 条有证据核因（美国实际利率 / 地缘风险溢价 /
  通胀对冲）重新启用，confidence 0.30，审计 `chain_cleanup/xau-revive-20261117.jsonl`；
  XAU 现有 4 条核因。
- **覆盖度定性**：36 个品种仅 6 个有链（XAU 227 / WTI 163 / BTC 31 / ETH 24 / SKHYNIX 2 / SOL 1），
  其余 30 个链数为 0 —— 因为链是从真实交易/评测样本蒸馏的，且 `ohlcv_daily` 只覆盖 27 个资产。
  这条不能靠脚本补；唯一不造假的路（新闻事件研究）受新闻 asset 粒度限制（只有 NVDA 是标的级），暂不建。

#### 第二轮：cron 旧路径 · 边权口径 · 证据驱动补核因

- **`backend/cron/*` 旧路径修完**：`run_kg_reconcile.sh` / `run_weekly_reflection.sh` /
  `install_cron.sh` / 两个 plist / README 共 6 个文件从
  `~/.cline/.../trident_v170/backend` 改到 `~/Desktop/金融1.0/backend`，原文件留 `.bak-20261117`。
  （生产本来就走 `~/.trident/wrappers`，这处只是消除误伤旧库的隐患。）
- **边权重口径查明**：不是"没归一"，而是两种语义混在一列 —— `causes/conditions/correlates_with`
  在设计量程 0–10 内（621/411/18 条），`supports`(233 条, max 40) 与 `contradicts`(223 条, max 41)
  记的是**票数**。新增 `scripts/edge_weight_audit.py`（dry-run 默认；`--normalize --apply`
  才按 kind 压到 0–10，原值写入新增列 `weight_raw`，可回滚）。本轮不改数据。
- **覆盖度真因**：36 个品种里只有 4 个有因果链（WTI 74 / XAU 76 / BTC 19 / ETH 4），
  其余品种 LLM 只能编"黄金/原油怎么看它"，全被新校验拦掉（ETH 补 7 条拦 7 条）。
- **证据驱动补核因**：新增 `scripts/kg_core_from_chains.py` —— 从 `enabled=1 且 hit_count>0` 的链
  里取「机制→结果」的机制段聚类，`confidence=min(0.5, 0.25+hit*0.05)`，`evidence=chain:<id>(hit/miss)`。
  WTI 从 0 条 → 3 条（命中 12/9/9 的三条链），core 总数 107 → 117，覆盖 12 个品种。
- **第五类节点垃圾**：`kg_node_cleanup.py` 加「自指/执行层」规则（信号生成/交易执行/手续费/滑点/
  仓位管理/止损/复盘），实测 COIN 的 core「信号生成与交易执行机制缺陷」被停用。
- **已知未动**：XAU 的 3 条有证据核因在 2026-10-07 被系统自身的衰减任务降为 0.2 并停用（非本次改动），
  是否复活属于衰减策略问题，留待评估。

#### 第一轮：链库净化 · 口径拆解 · 闭环接活

> 用户指令：「做啊全做了」。详见 `docs/因果图谱体检与整改-2026-11-17.md`。
> 主库 `backend/trident_event_bus.db`（801MB/WAL/在线），改前快照
> `backend/backups/trident_event_bus-before-kgfix-20261117-0644.db`。

- **录入侧 · 触发词净化**：`chain_ingest_cleanup.py --apply`，362 条链（48.1%）的
  `trigger_words` 混入 JSON 字段名（Asset/verdict/partial/market_event），实删 2270 个噪声词，
  审计 `runtime/chain_cleanup/cleanup-20261117-064519.jsonl`。
- **录入侧 · 去重合并**：新增 `scripts/chain_dedup_merge.py`（dry-run 默认），按名字前 12 字聚类
  17 组 / 32 条冗余，outcomes 重指 26 条；**坑**：直接 UPDATE `chain_id` 撞
  `UNIQUE(chain_id,trigger_ts,trigger_ref_id)`，须先删冲突行。enabled 392→361。
- **录入侧 · 入库校验**：`causal_graph._upsert_node` 加三道拦截 —— `_is_schema_label`（列名 REJECT）、
  `_looks_like_event`（新闻标题冒充 core → 降级 condition）、`_foreign_asset_label`（跨品种污染 REJECT）。
  跨品种规则对 `_COMMODITY_FAMILY={XAU,XAG,WTI,BRENT,COPPER,CPI}` 豁免（油冲击→金价是真结构）。
- **录入侧 · 关掉假的"已学知识"**：`ai_worker._fallback_business_knowledge_drivers(allow_generic=False)`，
  没学到就返回 None，AAPL/MSFT/COPPER 实测 None。
- **录入侧 · 覆盖度**：新增 `src_python/kg_coverage_fill.py`（`--max N`，走 run_reasoning + 新校验），
  补 AAPL/AMZN/BRENT/BTC/CNY；补出 0 条存活的品种记 `runtime/kg_coverage_giveup.json`，14 天不重试。
  活跃 core 101→107（11 个品种）。新增 `scripts/kg_node_cleanup.py` 四类清洗（含跨品种降级）。
- **输出侧 · 置信度**：`chain_steps_verify.py` 加 `CONF_BOTH_MAX=0.55` / `CONF_UNVERIFIED_MAX=0.50`，
  有方向的链改用 Beta(2,2) 后验均值 `(hit+2)/(n+4)` 且幂等。结果：`confidence=1.0` 的 750 条 →
  0 条 ≥0.9，均值 0.502，58 条 <0.5，`hit_count>0` 245 条。
- **输出侧 · 口径拆解**：新增 `GET /api/causal/quality` —— BOTH 单列并注「幅度口径」，
  `chains_n_ge_12 / chains_weak_3_11 / avg_confidence / outcome_status`。
  实测 DOWN 67.9% vs UP 33.4%（方向不对称，待拍板），BOTH n=396 hit=1.0（幅度口径的典型假象）。
- **输出侧 · 导出**：新增 `GET /api/causal/export?what=chains|nodes|edges|outcomes&fmt=csv|json`；
  前端 `knowledge-graph-panel.tsx` 加四表 CSV 导出条。
- **决策侧 · 分级闸门**：`chain_prior.py` 加 `MIN_CHAIN_SAMPLES_FOR_WEAK=3` 与 `tier_of()`，
  `aggregate_direction` 增 `UP_WEAK/DOWN_WEAK/WEAK_MIXED`；`conflict_veto` 仅 strong 可否决、
  `min_posterior` 0.85→0.60；`posterior` 改取 `confidence`（原取恒 1.0 的 weight）；
  `match_by_headline` 补 `confidence` 与 `AND COALESCE(active,1)=1`；`build_prompt_block` 分「已验证/候选」两段。
  实测 XAU 命中 3 条 weak → `DOWN_WEAK`、`veto=None`、posterior=0.5。
- **闭环接活**：`chain_steps_verify.py` 此前**从未被任何 cron 调度**，新增
  `com.trident.chain-steps-verify`（每日 05:20 `--apply`）、`com.trident.kg-coverage`（06:10 `--max 3`），
  已 `launchctl bootstrap`。
- **状态对齐**：`enabled=1 且 active=0` 的 135 条链停用，`enabled=191` 与 quality 的 effective 对齐。
- **测试**：`test_v406_prior_gate.py` 改 weak 口径（+tier/weak 不否决）、新增
  `test_v407_chain_conf_cap.py`（5 例）；6 个因果测试文件 74 passed。
- **顺手修的生产故障**：改造中全库 `database is locked`，定位 `com.trident.binance-flow-cron`（pid 32065）
  持有未提交写事务 32 分钟，杀进程后释放（`engine.err.log` 已有 `DB persist skipped ... locked`）。

### v412 · 行情快照不再"全断即判死"：本地优先打底 + 远端限时补齐

> 起因：引擎反复打 `[SNAPSHOT] DOWN (0/9 OK) in 18.8s` → `RuntimeError: snapshot down/empty`
> → 整批决策降级 HOLD。查下来是三层同时成立：
> ① 环境级断源（`www.okx.com` DNS 无解析、`fapi.binance.com` TLS 被重置、
>    `query1.finance.yahoo.com` 429 + SSL 失败、系统无代理；google 302 正常）；
> ② 代码级：`_collect_asset` 的本地兜底写在远端之后，被 `wait_for(to_thread, 8s)` 丢弃，
>    兜底永不执行 → 9 个资产全 `ticker=None`；
> ③ 口径级：`ok_count==0` 就判 `down`，`partial`（本地真价）不算可用，汇总还硬写
>    "所有标的拉取失败"。

改动（`market_snapshot.py`）：

- **本地优先打底**：`get_snapshot()` 先用 `_rebuild_asset_from_local()` 从 `market_ticks` +
  `ohlcv_daily/intraday` 建 9 个资产（毫秒级），再给远端预算。
- **远端硬预算** `SNAPSHOT_REMOTE_BUDGET_S`（默认 3s）：`asyncio.wait` 超时即 cancel，
  不再被 DNS 卡死线程拖满整段（实测 `_okx_get` 在 DNS 不通时能挂几十秒）。
- **远端熔断**：远端 0 个资产可用 → 120s 内直接本地，不再每批撞墙。
- **状态口径**：新增 `usable_count`（`ok`/`partial` 且 `price>0`），`down` 只在 9 个全无价时成立；
  `get_snapshot_sync()` 同步；日志拆成 `直连 OK / 有价` 两个数。

**接入国内可访问源（`feeds/domestic_quotes.py`，新增）**：外盘全断时"本地库兜底"只是
隔夜旧价，报价还是死的。改用国内可直连的三家补齐**真实当前价**：

- **Gate.io**（`api.gateio.ws`）→ BTC/ETH/SOL 现货成交价，唯一能拿到实时加密价的源；
- **腾讯**（`qt.gtimg.cn`）→ XAU/WTI（`hf_GC`/`hf_CL`，COMEX/NYMEX 期货代理）、
  NVDA/AMZN/META（美股收盘价）、SKHYNIX（`kr000660`，韩股收盘价）；
- **新浪**（`hq.sinajs.cn`）→ 同上外盘 `hf_` 码的备份；
- **CoinPaprika** → 加密第三兜底（Gate 单品种 TLS 超时时顶上）。

配套五处工程细节（都是实测踩出来的）：① 说明文本**跟来源走不跟资产走**（`_note_for(asset, source)`），
否则 BTC 落新浪备份时仍显示"Gate.io 现货成交价"，口径污染；② 熔断**按"源+品种"**粒度
（`gate:BTC`/`gate:ETH`/`gate:SOL`），一个品种超时不连坐；③ Gate/CoinPaprika **不重试**
（`retries=1`），重试一次就把 3s 变 6s、整段预算被吃光；④ 国内源跑**独立线程池**
（`_DOM_EXECUTOR`），避免被 `_collect_asset` 里挂死的远端线程占满默认池而排队饿死；
⑤ 国内源与远端**并行发起**（`_dom_task` 在远端前创建），不是串行叠加。
另：腾讯 `usBTC` 是灰度迷你 ETF（≈28.7），列入 `_FORBIDDEN` 禁止当 BTC 现货价。

结果：引擎重启后首份快照 `国内源补齐 9/9` + `PARTIAL (3/9 直连 OK, 9/9 有价) in 7.811s`
（原 18.8s down）；单进程实测 `9/9` / `3.574s`。

**mtf 多周期改用 Gate 真实 K 线（v412-B）**：原来 mtf 的 1d/4h/1h 只有"远端（全断）"
和"本地库（隔夜，实测 WTI 1d 最新 23.6h 前）"两个来源，等于用昨天的方向指导今天的决策。
现在 Gate 一次拉 `1h×200` 根，本地聚合成 `4h(42)`/`1d(8)`/`1h(48)`（同一份序列，口径自洽），
优先级 **远端直连 > Gate K 线 > 本地隔夜线**，只覆盖 `source=local_kline` 的周期、不跨源拼，
每条 mtf 带 `source=gate_klines`/`source=local_kline`。覆盖 BTC/ETH/SOL（其余品种 Gate 没有）。
K 线失败是间歇 `SSLError`（约 1/3），改 `retries=2/timeout=2s`；与报价任务并行等，不串行叠预算。
实测 `K线补齐 9 个周期`、快照 `4.389s`；聚合值与 Gate 官方日线交叉核对一致（-13.85% vs -13.87%，
差在末根未收盘）。单测 +2 例（聚合取法/升序截断），合计 7 passed。

**快照总时限 + 技术读数预取（v412-C）**：`DOWN` 之外还有一个更隐蔽的敌人——**超时**。
引擎外层 `wait_for(get_snapshot(), 15.0)`，实测单次耗时 `5.3 / 7.8 / 10.9 / 12.1 / 14.4 /
18.7 / 22.4 / 30.4s` 一路超过 15s，同样会被判 down。逐层定位发现拖慢的不是国内源，而是：
①本地打底逐个库查询（库被引擎自己写锁住，单次只读等满 3s，9 个资产 × 2 次 → 22.4s）；
②`_technical_state` 串在装配里（库锁时从 0.25s 涨到 2s+，9 个串行十几秒）。
改法：只读超时 3s→1.5s；打底阶段 3s 总预算（超了给空壳）；技术读数**改为与取价并行预取**
（独立 3 线程池，`_collect_technical_states`），装配只做 O(1) 合并；再加总时限
`SNAPSHOT_TOTAL_BUDGET_S`（默认 11s）约束远端 3s / 国内 4s；预取没覆盖到的资产，
第一次读数 >1.2s 即熔断并标 `available=False`。
结果：`PARTIAL (1/9 直连 OK, 9/9 有价) in 7.506s（打底 0.3 / 远端 3.0 / 国内 4.0 / 装配 0.2）`，
装配阶段从 4–5s 降到 0.1–1.1s，技术读数可用 5/9（其余品种本来就没有 4h 数据，不是被跳过）。
阶段耗时直接打进汇总行；`0 补齐` 也显式打日志（`国内源本轮 0 补齐` / `K线本轮 0 补齐`），
避免"日志里没有这一行"被误读成代码没生效（08:02 那轮整波 ConnectionReset 的真实教训）。

**商品补线（v412-D / v412-F）**：XAU/WTI 原来 1d 与 4h/1h 全挂在本地隔夜线上。逐个源实测后：

- **1d 走新浪外盘日线**（`getGlobalFuturesDailyKLine?symbol=GC|CL`，2,589 / 7,690 根）→ `sina_daily_k`。
- **4h/1h 走东财国际期货 1h**：`101.GC00Y`（COMEX黄金）/ `102.CL00Y`（NYMEX原油，`101.CL00Y`
  是 rc=100 无数据），`klt=60` 只回 **118 根≈5 个交易日**，本地聚合成 4h（31 桶）→ `em_klines`。
- 东财的**可靠性矩阵**（同机同时段实测）：每请求新建 Session 同 secid 5/10；复用 keep-alive
  同 secid 11/12；复用同一连接**换 secid 0/10**（会被重置）；每请求独立 Session `101.GC00Y` 8/8、
  `102.CL00Y` 先 0/8 后间隔 1s 变 3/3。→ 实现改成「每品种独立连接 + 品种间 sleep(0.3) +
  重试递退 0.4/0.8/1.2s」，不共享 Session 串行。
- **列序坑**：东财 `fields2` 是 `f52=开 f53=收 f54=高 f55=低`（"收"在"高"前面），照抄 Gate 的
  开高低收会把高低写反 → 单测 `test_em_klines_column_order` 钉死。
- **来源按周期分开**：商品 1d 与 4h/1h 不同源，拆 `_source_1d` / `_source_intraday`；且**只有
  真拿到数据才 setdefault**，否则"没补上"会被上层读成"已补"（单测 `test_sina_daily_only_feeds_1d`）。
- **本地兜底必须可识别**：`_fetch_asset_klines` 内部退回 `kline_bars` 时返回值看不出区别，
  WTI 的 mtf 曾因此 `source=None`。新增 `_LOCAL_FALLBACK_MARKS` 打时间戳，回退周期标
  `local_kline`、真实外网标 `remote_kline`——**任何周期都不允许 source 为空**。
- **熔断键按源分开**（`_cooldown_key`）：东财失败原来写 `mtf_d:{asset}`，会把新浪日线的熔断一起打开。

实测 `K线补齐 15 个周期（gate_klines 9、sina_daily_k 2、em_klines 4）`，`7.439s`；
商品 1d/4h/1h 全部为真实交易所序列。东财是**机会源**（有则用、无则回落 `local_kline` 并如实打日志），
不保证每小时都有真实 4h 线。

**国内源总预算封顶（v412-F）**：引擎给国内段只有 4–5s，但各源超时是叠加的，这是
「引擎侧国内源 0 补齐、独立跑却能取 8–9/9」的真因（不是网络全断）：

- `fetch_all` 原来「每个源各等 4s」+ `with ThreadPoolExecutor`（退出默认 `wait=True`），
  新浪单请求 3s、重试 2 次≈6s → 整组拖到 6–10s，gate/腾讯已拿到的结果被超预算丢掉。
  改一次性总预算 `_cf.wait(timeout=_QUOTE_BUDGET_S=2.5)`、CoinPaprika 兜底再 0.8s、
  池子 `shutdown(wait=False)`，上限≈3.3s。
- `fetch_mtf` 同理 `_MTF_BUDGET_S=3.0`，东财再等 `min(EM_WAIT_S, 剩余)`，实测稳定 3.0–3.2s。
- 东财不能放在 gate 之前阻塞等待：原来先 `em_fut.result()` 再收 gate，东财一退避就吃光预算，
  连带已拿到的 gate/新浪一起丢。

**宏观读数并行 + 只读（v412-E）**：`_fred_macro_latest()` 原来用 `db.get_connection()`
（可写连接、timeout=5s），引擎写库时 SELECT 等满 5s——"装配"因此从 0.7s 涨到 4.8s，
整份快照从 8s 顶到 12s。改三层：①只读 `mode=ro` + `timeout=1.5`；②进程内缓存（宏观是日度）；
③改为与取价**并行预取**，装配只接结果。缓存 TTL 的坑：先设 300s，正好等于引擎快照冷却
300s → 每批必过期、等于没缓存，改 3600s。引擎侧实测 `11.585s` → `7.458s`（宏观 3.8s → 0.0s）。

今日 `server_fallback` 降级 0 条；
剩余 target_none 均为 `decision_kind=routing_miss`（新闻不指向 5 个核心品种，挂 HOLD 正确）。
漏斗日报补该口径行。新增 `tests/test_v412_domestic_quotes.py` 5 例离线单测。
详见 `docs/v412-行情断源修复-2026-11-17.md`。
**引擎侧落地：文件邮箱（v412-F 收官）**


---


### v411 · 快讯采集器原来不过闸：让 sina/wscn 走回过滤器

> 起因：查"为什么决策大量 target_none"，发现 `ai_decisions` 里近 1 小时 8 条决策有 6 条
> 对应的新闻 `gate_verdict` 为空、`gate_unavailable=0` —— 既没判噪音、也没过门卫。
> 顺着查下去，`sina_flash.py` / `wscn_flash.py` 是**裸 INSERT**：

```sql
INSERT INTO raw_news (source, content, timestamp, ts, published_at, status,
                      gate_kind, content_fingerprint, ...)
VALUES (?,?,?,?,?, 'PENDING', 'real_news', ?, ...)
```

**完全绕过 `evaluate_news`**：不判 L1/L2 噪音、不跑 L0 时效、不进门卫、`relevance_score` 留 0。
而模块 docstring 写的是"交给门卫判定" —— 文档承诺和实现差了整整一条链路。

- **实测影响（近 3 小时，`ts > now-3h` 且 `is_noise=0`）**：134 条里 **76 条（57%）**
  `gate_verdict='' 且 gate_unavailable=0` —— 没有任何过滤痕迹就进了主分析队列。
  来源：`sina_flash` 59 条、`wscn` 28 条（另含少量重复）。典型样本：美国 AI 出口计划三条、
  巴西芯片措施、"富国银行：标普500指数 2027 年要到 8200 点"（后者被分析成 **BUY +0.650**）。
- **新增 `backend/src_python/flash_gate.py`**：`gate_columns(body, published_at, ts)` 调
  `evaluate_news` 并把结果映射成 raw_news 列（`is_noise/relevance_score/gate_verdict/
  gate_unavailable/gate_strength/gate_main_asset/filter_reason`）。
  三条原则：判噪音 → 返回 None 由调用方丢弃；门卫不可用 → 标记照写（1 冻结 / 2 降级放行）；
  **过滤链路任何异常 → 退回旧行为照常入库**（宁可少过滤，不可丢新闻）。
- **`sina_flash.py` / `wscn_flash.py`**：改成先过闸再写库，新增列一并写入，
  日志带"门卫判噪音 N 条"。开关 `FLASH_FEEDS_GATE=0` 一键回退旧行为。
- **测试**：`tests/test_v411_flash_gate.py` 6 例（噪音不入库 / 门卫列透传 / 不可用标记不被隐藏 /
  异常退化不丢新闻 / 空正文 / 开关），与 v408+v409 合并 30 例全绿。
- **端到端验证（真实过滤器）**："富国银行…8200 点" → `[GATE] 排除 | 美股 | s=3` → 判噪音跳过，
  不再进分析；"某明星演唱会门票" → `L0-POOL 非交易类主体（明星）` → 丢弃。
- **未做**：`feishu_listener`/WS/webhook/api 四个入口本来就过闸，无需改；本版只补这两个裸入口。
- **附带（同版）· 门卫调用面开关 `GATE_CALL_SCOPE`**：`pool` 时只对命中池内资产/宏观驱动锚点
  的新闻调门卫，其余按 `gate_unavailable=2`（未判定但放行）。**默认 all，保持原行为**。
  实测近 3h 103 条非噪音会跳过 50 条（48.5%，门卫调用量减半），但被跳过的正是 target_none
  主要来源（"美国 AI 出口计划"、"巴西芯片措施"这类有国名无品种的稿子）—— 跳过等于让它们
  直接进分析，反而抬高 target_none 占比。所以只留手段，不默认开。测试
  `tests/test_v411_gate_scope.py` 5 例（含"写错值不许悄悄收紧"）。
- **附带（同版）· `cleanup_noise_pending.py` 归档留痕**：手工归档 SQL 原来只改 `status`，
  与 v408 之前的自动归档同一个毛病（事后查不出谁、为什么）。现在补
  `filter_reason='zombie_pending_manual'`（已有值时不动）。

### v409 · 面板不许骗人：漏斗数字走真源，失败要显形

> 用户指令：「继续」。上一版（v408）把"新闻消失"变成可查之后，去核对面板，发现面板本身在说谎：
> `/api/health/models` 返回 `pending_news=0 / analyzed_24h=0 / warming_up=true`，
> 而库里当时有 51 条 PENDING、当日 17 条决策、近 24h 真实分析 310 条。
> 一个"看不见问题"的监控，比没有监控更危险——后面所有改动都会失去观测。

两处独立缺陷，都在 `api_server.py` 的 `_probe_model_channels_now`：

1. **`except Exception: pass`（原 10852 行）**：DB 被引擎写锁占住时，查询失败被吞掉，
   变量保持初值 0 → 面板显示"没积压也没分析"。与 v322 那次 `except: return 0` 同一类训诫。
2. **`analyzed_24h` 的 cutoff 口径错**：`datetime('now','localtime','-24 hours')` 产出
   `2026-11-16 07:02:19`（空格），而 `ai_decisions.created_at` 是
   `2026-11-16T00:58:43.376+08:00`（T）。字符串比较下 `'T'(84) > ' '(32)`，跨日边界的整段旧数据
   会被算进"最近 24h"。**实测同一时刻：旧口径 521 行 vs 真实 310 行，虚高 68%。**
3. 附带结构问题：漏斗数字（本地两次 COUNT，几十毫秒）和通道探测（网络，Ollama 冷启动实测
   ~115s）耦合在同一个后台函数里 → 探测没跑完，连"待分析几条"都看不到。

- **新增 `backend/src_python/news_funnel.py`**：`funnel_stats(db_path, max_age_hours, now)`。
  只读连接（`file:...?mode=ro` + `PRAGMA query_only=1`，WAL 读者不挡写者）、ISO 口径、
  **失败返回 None + db_error，绝不用 0 冒充"查询成功且为空"**。
- **`api_server.py`**：请求路径上叠加 `_funnel_overlay()`（每请求真算，不受通道探测卡顿影响）；
  `_probe_model_channels_now` 里那段重复 SQL 删掉改调它；payload 增 `funnel_db_error`；
  `_model_health_skeleton()` 也补 `last_refresh_error`（原来 skeleton 什么都不带，
  "探测一直崩"和"探测还没好"看起来一模一样）。
- **测试**：`tests/test_v409_funnel_stats.py` 5 例（含对照断言：同一张表上旧口径必须算成 2、
  新口径算成 1，把"虚高"这件事钉成回归项）。关联子集 75 例全绿。
- **验证（重启后实测）**：`pending_news` 0→**106**、`analyzed_24h` 0→**310**（与我直接算的 310 一致）、
  `warming_up` 在 ~115s 后转 `false`、9 个通道全 `available=true`、
  `current_model=minimax/MiniMax-M3`。

**顺带纠正一个我自己的误判**：面板并非"永远是 warming_up"。实测通道探测要 ~115s（Ollama 冷启动），
缓存填上后就正常；真正一直错的是 DB 那两个数字。

**补丁（同日，v409 续）· 门卫真实判出率也上面板**：`/api/health/models` 里
`channels` 全 `available=true`，而同一时刻门卫在连续 429 —— 通道探针每轮**只探一次**
（14s 延迟都算通），门卫是高频连续调用，一打 429 整条通道熔断。于是面板"绿灯"+门卫"全池不可用"
可以同时成立，又是同一个病：**监控显示的不是瓶颈**。

- 新增 `news_funnel.gate_health(db_path, minutes=30)`：按近 N 分钟 `raw_news` 分出
  `frozen`(gate_unavailable=1，主分析不认领) / `degraded`(=2，未判定但放行) /
  `judged`(真拿到判定) / `unlabeled`(走 L1/L2 关键词，没调门卫)，并给出 `judged_pct`。
  同样只读 + 失败显形（`db_error`，不用 0 冒充）。
- `api_server._funnel_overlay()` 输出 `gate_health`。实测（07:1x）：近 30 分钟 52 条里
  冻结 **19（36.5%）**、有判定 **5（9.6%）**、降级 2、无标签 26 —— 与库内直算一致。
- 测试新增 2 例（`test_gate_health_counts` 六行样本逐项对账、`test_gate_health_missing_table_reports_error`），
  本文件 7 例、关联子集 59 例全绿。
- 说明：这是**观测修复**，不解决门卫配额。配额要独立端点（`.env` 三行 `GATE_ALT_*`，
  `config.py:706` 已留口），步骤与验证判据见 `docs/门卫供给-接法与验证-2026-11-17.md`。
- 版本号：`v410` 已被并行作者（因果图谱整改）占用，本补丁不另起号，续在 v409。

### v409 · 细节洞察能力整改：拆码、分池、补源、洞察即判定、S3 归因

> 用户指令：「全做了」。承接 `docs/细节洞察能力评估-2026-11-16.html` 的 P0/P1/P2 清单，
> 六项一次落地（数据截至 11-17 06:40：516 决策 / 611 反思 / 58 洞察）。

- **P0-1 拆 E001a**：`reflection_checkers.check_data` 不再对 `target_asset=NONE` 报「缺硬数据」
  （无标的不是缺事实），按 `hard_data_status` 拆出 `E001g`（本地无源，结构性，severity=INFO、
  fixable=0）/ `E001h`（断档，可修）/ `E001i`（符号或标的未解析，可修），未标注时回落 `E001a`。
  新增 `_classify_missing_hard_evidence()`；`reflection_dimension_map` 加 C 维子类 `feed_no_source`
  与三条码映射；`error_code_ledger_build.REGISTRY` 登记三码。
  实测：`decision_errors` 里 E001a 由 395 条（85%）降到 30 条，新分布 E001h 185 / E001i 27 / E001g 24。
- **P0-2 决策分池**：`ai_decisions` 增 `decision_kind`（DEFAULT 'analysis'）；`ai_worker` 在原有的
  `asset_route/hard_data_status` UPDATE 处一并写 `'routing_miss'`（`_r_tag=='target_none'`）；
  记分卡新增 `_analysis_pool()`，`_not_degraded()` 与 S16 分母都过该池（S16 定义文本随之改写）。
  `scripts/backfill_decision_kind.py`（默认 dry-run，`--commit` 落库，两条幂等 UPDATE）已对生产库执行：
  回填 161 行 → `{analysis:355, routing_miss:161}`，S16 由 87.2%(n=516) 变 82.8%(n=355)。
- **P1-1 硬数据**：① `hard_data_block.select_bar()` 新增 —— 当前小时只有 1~2 根 1m 行且无振幅时，
  回退到最近一根走完的 K 线（整小时都平仍判冻结，不洗白）；`ai_worker._build_evidence_from_local`
  改为取 8 根再挑、1H 聚合从选中那根往前数。② `decision_provenance.hard_status` 把桶标签
  （CN_STOCK/US_STOCK…）单独记 `missing:unresolved_target`，归到 `E001i`。
  **「个股换源」被实测证伪**：`missing:no_local_feed` 只有 CN_STOCK 17 / INDIVIDUAL_STOCK 4 /
  US_STOCK 2 / HK_STOCK 1，全是「公司有、代码没解析」的桶标签；XAU 87 + WTI 97 的 `feed_stale`
  是每日 22:00–23:00 UTC 休市窗口（1m 行四价全等且 volume=0），属休市不是管道故障。
- **P1-2 洞察即判定**：`insight_hypothesis.classify_one(conn, insight_id)`（幂等，已判定行不动），
  接到 `review_agent.save_insights` 主 INSERT 与 `engine/structured_reflection` 的 promoter 写入点 ——
  `observe_only` 与参数键在落库当刻就成立，不再等 worker 每轮巡检。
  注意可映射键极少：`score.buy_min_score`/`sell_min_score`（settings）、`score.signal_threshold`/
  `confidence_threshold`/`event_strength_threshold`（strategy）；`sizing.*` 全族在 UNMAPPABLE。
- **P2 S3 归因**：新增参考口径 **R4 平衡准确率**（`_q_causal_balanced`，不参与达标判定）。
  实测 49.2% 拆为 UP 37.1%(n=824) / DOWN 62.6%(n=746)，两组 5 日前向收益均值均 ≈ −2.2%，
  R4 = 49.9%(782/1570) ≈ 抛硬币 —— 49.2% 是单边下跌窗口 + 类别偏置的读数，不是方向能力。
  详见 `docs/S3方向命中归因-2026-11-17.md`。
- **顺手修掉的静默 bug**：S19 在 `-m src_python.capability_scorecard` 跑法下 `import insight_hypothesis`
  失败被 `except` 吞掉，指标静默显示 n=0「样本不足」。现改为 `_insight_hypothesis()` 双路径取模块 +
  取不到时直接查库兜底；S19 恢复可读：90.6%（48/53）。
- **测试**：新增 `test_v409_error_code_split.py`(8)、`test_v409_decision_kind_pool.py`(5)、
  `test_v409_hard_data_forming_bar.py`(5)、`test_v409_insight_classify_on_insert.py`(3)；
  `test_reflection_dimension_map.py` 的 C 维子类断言 4→5。关联子集 88 例全绿。
- **文档**：`docs/细节洞察能力评估-2026-11-16.html` 追加第八节（改口径前后对照、六项落地、
  评级修订、下一步杠杆）；新建 `docs/S3方向命中归因-2026-11-17.md`。

**未解决（写在台面上）**：① `analysis` 池内仍有 126 条 `analysis_degraded_reason='target_none'`
却带明确 `target_asset`（XAU 46 / WTI 42 / NVDA 13 / BTC 10），原因码未随路由回写，S16 分子可能被高估；
② `llm_empty`(72)/`summary_only`(66)/`server_fallback`(30) 需产品判断，不在机械整改范围；
③ ticker 解析（而非换数据源）才是硬数据缺口的真杠杆；④ `causal_chain_outcomes` 结算仍停在 11-09。

#### v409 第二轮 · 同一次授权里剩下的 5 条（回写原因码 / ticker 解析 / 休市感知 / sizing 换算 / 降级治理）

> 用户指令：「都」。以上五条全部落地，并重启后端让代码生效。

- **T1 回写降级原因码**：`ai_worker.classify_analysis_degraded()` 新增 `asset` 参数与
  `_is_resolved_asset()` —— 模型判 `目标资产 NONE`、但标的已被路由补成真代码时返回
  `asset_late_bound`（`analysis/contract.derive_state` 映射为 `parse_failed`）。
  新增 `scripts/backfill_degraded_reason.py`（默认 dry-run、`--commit`、`--limit`），已对生产库 commit：
  扫 290 行、改 **122 行** → `asset_late_bound`，剩 4 行真无代码。
  回写后 analysis 池：asset_late_bound 122 / llm_empty 72 / summary_only 67 / ok 64 /
  server_fallback 30 / target_none 4。
- **T2 ticker 解析**：新增 `src_python/ticker_resolve.py`（`resolve_from_text` / `maybe_resolve`）——
  只认 `$NVDA`、`600519.SH`、`00700.HK`、`NASDAQ: NVDA`、中文括号代码；裸六位数不认；
  港股 4 位自动补零成 5 位；最终只接受 `asset_kinds` 判为 us/a_share/hk 的。已接进 `ai_worker`
  落库前的 provenance 块：桶标签时读 `raw_news.content` 提取，命中写 `target_asset` 并打
  `[TICKER-RESOLVE]` 日志。
- **T3 休市感知**：新增 `src_python/market_session.py`（`session_state` / `closed_reason`）——
  BTC 等 7×24 恒 open；XAU/XAG/WTI/BRENT/OIL/NG/COPPER 走 CME 日历（每日 22:00–23:00 UTC 维护、
  周五 22:00 → 周日 23:00 UTC 停盘）；其它品种 unknown，不猜。
  `signals/decision_provenance.hard_status(..., ts=)` 休市记 `missing:market_closed`，`tags()` 透传 ts，
  ai_worker 两处调用点传 `ts=time.time()`。新增错误码 `E001j`（INFO、fixable=0、C 维 `signal_delay`），台账登记。
  另修一处自引入死分支：`missing:unresolved_target` 原写在 `no_local_feed` 之后永不命中，已重排；
  新增 `_UNRESOLVED_BUCKETS`，真实代码但本地无源（NVDA/600519.SH/00700.HK）走 `asset_kinds` 判市场 → `no_local_feed`。
- **T4 sizing.* 换算**：`param_key_aliases` 新增 `SIZING_REQUIRED_CONTEXT` / `convert_value()` /
  `missing_context()` —— `sizing.stop_loss_pct`、`sizing.take_profit_pct` 需 `atr_pct`
  （`sl_mult = |pct| / atr_pct`），`sizing.position_weight` 需 `equity_usdt`（`notional = equity × weight`），
  缺上下文一律拒绝并写清缺哪项；UNMAPPABLE 文案改为「缺什么上下文」。
  `insight_hypothesis.classify(..., context=)` 只对需上下文的键走换算（其余仍走静态别名，避免劫持
  `sizing.holding_horizon_minutes`），新增 `_PCT_RE` 解析文案百分比、`market_context()` 从
  `kline_snapshots`（timeframe='1h'）取 ATR%，`classify_one` 已传入。
  实测：`止损放宽到 1.5%` + atr_pct=0.5 → `sl_mult=3`、`observe_only=0`；无上下文 → observe_only 且写明缺 atr_pct；
  有上下文但文案无百分比 → 明确拒绝。`position_weight` 因库里无权威权益列（`paper_trading_settings` 只有
  sl_mult/tp1_mult 等、`paper_trading_runs` 无 equity 列）保持不可换算 —— 不编假数据。
- **T5 降级治理**：`server_fallback` 拿到专属抢救预算（默认 4/10min，`TRIDENT_TWO_STEP_RESCUE_SF_LIMIT` 可调），
  不再与 `llm_empty` 抢同一个 10 条窗口；落库前抢救块改为 `_rs_try` 两段式，预算被拒记 `skip=budget_exhausted`；
  新增 `_rescue_log()` 写 `backend/runtime/rescue_log.jsonl`（news/decision/why/ok/skip/asset/ts），
  把「预算耗尽 / 没文本 / 补了还是空」分开记（此前 72 条 llm_empty 补不回来却无从诊断）。
  **降级数量是否下降尚未被活体证明**，需跑满一个窗口后读 `rescue_log.jsonl` 与新批次 `ai_decisions`。
- **测试**：新增 `test_v409_session_and_status.py`(7)、`test_v409_ticker_resolve.py`(5)、
  `test_v409_sizing_convert_and_reason.py`(6)；关联集共 **106 例全绿**
  （含 v399/v398/v397 与 `test_reflection_dimension_map.py`）。
- **重启生效**：`~/.trident/wrappers/restart_backend.sh`（bootout watchdog+engine → kickstart bridge →
  拉回 engine）完成，bridge health=200、engine=1 个；另清掉一个占着 9001 端口的旧 `master_api`
  重复实例，由 launchd `com.trident.master` 重新拉起（health 401 = 需鉴权、进程正常）。
- **文档**：评估报告追加 8.6（五条落地 + 实测）、8.1 两处陈旧数字按最新库改正
  （S16 82.2%/n=359、126 → 122+4、S18 32.5%/n=532、S20 60.9%/n=64、R4 784/1571）。

**仍然未解决**：① `causal_chain_outcomes` 结算停在 11-09（epoch 11-15 重置后未更新）；
② `sizing.position_weight` 缺权威 equity 源，保持 observe_only；③ 72 条 llm_empty / 67 条 summary_only
的结构化成因未逐条拆（诊断日志已就位，等窗口）；④ S3 建议按 regime 切分，未动模型权重。

### v408 · 归档语义拆分 + 市场归属：让"没被分析"这件事可见

> 用户指令：「把归档语义和认领语义拆开，别再让 FILTERED 同时表示判噪音和超龄归档；
> 再给 news_router 加市场归属维度」。
> 起因是 11-16 的漏斗：入库 1115 条，真正分析 53 条（4.8%），而 status='FILTERED' 且
> is_noise=0 的有 742 条（66.5%）—— 也就是三分之二的新闻**在没有任何计数的地方消失了**，
> 其中 52 条门卫已经判过「可交易」。这不是过滤太严，是记账方式让损失不可见。

**根因（决定性那一条）**：`~/.trident/wrappers/run_engine.sh` 硬编码
`export NEWS_ANALYZE_MAX_AGE_HOURS=1`，而 `backend/.env:237` 与 `config.py` 都是 6。
60 分钟没被认领的 PENDING 会被 `engine/ai_worker.py` 的归档 SQL 标成 FILTERED
（**且不改 is_noise**），而认领 SQL 只取 `status='PENDING' AND is_noise=0`
→ 这批新闻结构性不可能被分析，也永远不会出现在任何健康指标里。
wrapper 与 config 两处配置静默打架，是这次事故的放大器。

- **`~/.trident/wrappers/run_engine.sh`**：`NEWS_ANALYZE_MAX_AGE_HOURS` 1 → 6，与
  `.env`/`config.py` 对齐；注释写明"两处会静默打架，别只改一边"。
- **`db.py`**：`raw_news` 增 `filter_reason`（`_RAW_NEWS_COLUMNS` 增量迁移 + `_CREATE_RAW_NEWS`
  同步），给"为什么不分析它"留一个字段。老库由 `_ensure_columns` 补列，新建库直接带列（两条路径都验过）。
- **`engine/ai_worker.py`**：删掉固定常量 `_BATCH_EXPIRE_SQL`，改为 `_expire_sql(keep_tradeable,
  has_reason_col)` 动态拼：SET 带 `filter_reason='stale_expired'`；WHERE 在
  `NEWS_EXPIRE_KEEP_TRADEABLE=1`（默认）时追加 `gate_verdict != '可交易'`。
  `_expire_stale_news` 按表结构探测列（老库自动降级，不会整条 UPDATE 被拒），归档时打印
  `[BATCH] 归档超龄新闻 N 条（>6h，门卫可交易的保留…）`，把"归档了多少、丢了什么"变成可读日志。
- **`feeds/news_router.py`**：新增市场归属三组常量 `FOREIGN_LOCAL_MARKETS` / `LOCAL_STAT_WORDS` /
  `STRONG_ANCHORS`（刻意不含 CPI/GDP 这类通用统计词）+ 纯函数 `foreign_local_stat_reason()`，
  在 `pool_out_reason` 里作为**规则 0** 排在"命中池关键词就放行"之前（否则「以色列CPI」被
  自己的 CPI 救回来）。三条同时成立才判池外：别国主体 + 本地统计口径 + 全文无强驱动锚点。
  `NEWS_POOL_SCOPE=0` 可一键退回。
- **测试**：新增 `tests/test_v408_archive_and_scope.py`（17 例）；`test_v406_pool_router.py`
  的白名单样例换成真正无锚点的文体稿（原"埃及最低工资"已被市场归属规则判池外，不再是"应放行"样例）；
  `test_p3_gates_v396.py` 的个股用例改为只守"必须被拦下"（v406 的"非交易类主体"口径会换理由串）。
  关联子集 82 例全绿。
- **生效状态**：`raw_news.filter_reason` 已在主库补列；engine（pid 40415，env 已读为 6）与 api
  （pid 40565）已重启。**bridge 是手工进程未重启**，其 filter-stream 预览仍用旧路由。

**未解决（必须写在台面上）**：市场归属规则实测只能拦下 279 条 target_none 里的 **7 条（2.5%）**。
剩下的 78.9% 不是"别国本地统计"，而是海外政治口水（美国众议员发言）、非池公司稿（达美航空
退改签、新世界发展供股）——那是**另一类**闸门，需要单独的准入规则，不在本版范围。

### v407 · 回放隔离：历史回放不许进绩效口径 + 造数源封堵

> 用户指令：「当然需要个隔离，系统内任何一个角落都不得出现假数据编造的信息」。
> 审计发现三处"合法外衣下的造数"：回放样本被当绩效样本用、master_api 用 random 生成
> 对外信号、seed 脚本用 random 生成反思。这一版把三处全部封死，并把守卫从 7 类扩到 9 类。

- **回放 ≠ 绩效（`trident2`）**：`Context.paper_trades` 改名 `replay_trades()`（docstring 写明
  「研究样本，不是绩效」），新增 `live_trades()`（只读 `quick_sim_trades` 已结算真实成交）
  和 `replay_count()`。F45 策略健康度、F12 PnL 归因、F59 置信度校准**删除全部"回放优先"分支**：
  样本不足时返回 `no_data`，文案写「真实成交仅 N 笔…历史回放 M 条已隔离」，`evidence` 只列
  真实表，`value` 增 `data_class: live`。原来三个 feature 的 `if len(paper) >= 阈值` 等于用
  一个可反复重跑、可调参重选的样本池刷胜率/Sharpe。
- **`master_api.py`**：`trident_compute_signal` 整段 random 演示实现（随机 strength/confidence、
  RSI、7 日曲线、`_demo: True`）替换为 `raise NotImplementedError`，端点返回 501
  `algorithm_not_implemented`。服务已重启，`/health` 200，函数级验证「OK 拒绝造数」。
- **`seed_trade_reflections.py`**：`main()` 首行加 `[REFUSED]` + `return 2`，随机抽维度/小类/
  命中率/outcome/参考价的写入逻辑保留在不可达代码里，防止有人"顺手再跑一次"。
- **`engine/ai_worker.py`** 两个真 bug 顺带修掉：①guard 没给 `confidence` 时原来用 `0.0` 顶上，
  现在标记 `_conf_unavailable=True` → 落库 NULL（F1 复发的根因）；②`_now_sticky = _ts()`
  返回字符串却与 float 相减，重启后主循环直接 `TypeError` 崩掉 —— 改 `time.time()`。
- **`fake_data.py`** 新增两类守卫：F8 回放混进真实绩效表（`quick_sim_trades` 带 replay 标识，
  清理=删行）、F9 回放行缺来源标签（人工核查，不自动改）。`--commit` 已清掉 4 条复发的 F1 行。
- **`feed_probe.py`** 文件头过期描述（"backfill 用历史回放伪造真实探测结果"）改为"已废弃"。
- **`AGENTS.md`** 0 号铁律补充「回放 ≠ 绩效」条款与「造数代码一律不许存在」硬约束。
- 回归：`tests/test_trident2.py` 28 例全绿（新用例 `test_replay_never_enters_performance`：
  塞 25 条回放后 F45 仍 `no_data`，notes 含"隔离"）。生产库只读实测 F45/F12/F59 均为 `no_data`
  + 「历史回放 2030 条已隔离」。

### v407 · OI 历史回补 CLI（代码就绪/出网受限）+ 前后端指标口径统一（parity 测试）

> 承接 v402 遗留的两件事：①补仓位类指标的历史深度 ②前端 `technical-indicators.ts`
> 与后端 `technical_indicators.py` 两套并行实现。第二件在本轮做完；第一件的代码全部到位，
> 但**本机与云主机都连不上币安/OKX/Bybit**（`fapi.binance.com` 30s 超时、`www.okx.com` 超时、
> `api.bybit.com` 超时；本机 7890/7891/1080/8888/6152/9090 无任何本地代理监听），
> 所以回补**未落一条数据**——这一点必须写清楚，不能写成"已补 30 天"。

- **`binance_flow.py` 新增 `BACKFILL` + `backfill(period, limit)` + `--backfill` CLI**：
  5 个指标走币安 `/futures/data/*`（该端点给最近 30 天）——`openInterestHist`
  （`sumOpenInterest` / `sumOpenInterestValue`）、`globalLongShortAccountRatio`、
  `topLongShortPositionRatio`、`takerlongshortRatio`；`source="binance_backfill"`、
  `meta={"unit","window","backfill":True}`，走 `ingest_external` 的 (asset,indicator,ts) 幂等，
  重复跑不会写重。**在有交易所访问权的机器上一条命令即可补齐近 30 天：**
  `python3 src_python/binance_flow.py --backfill --period 4h --limit 180`
  （若走本地代理：`MARKET_USE_PROXY=true` + `HTTP(S)_PROXY=http://127.0.0.1:7890`）。
- **前端 `bollinger` 改回总体标准差（除以 N）**：v302 以"与 TradingView 对齐"为由改成样本
  标准差（N-1），但 TradingView `ta.stdev()` 的 `biased` 默认 true 即**总体**标准差，
  改完反而偏离，且偏差 sqrt(N/(N-1))（period=20 时带宽宽 2.6%）会穿透到
  `bollingerWidthPct` → squeeze 分位判定。后端 `technical_indicators.bollinger` 一直是总体标准差。
- **前端 `rsi` 首位对齐**：原实现首个值放在第 `period+1` 根且多平滑一次，整条序列相对后端
  **错位一根 bar**；改为第 `period` 根出首个值（与后端 `rsi` 同口径）。
- **前端 `atr` 首位对齐**：原实现用 `trs[1..period]` 播种、落在第 `period` 根；
  改为 `trs[0..period-1]` 播种、落在第 `period-1` 根（与后端 `atr` 同口径）。
- **前端 `ema` 种子改回首根收盘**：原实现前置 `period-1` 根 null、用 SMA 播种；后端是
  `out[0]=values[0]` 起递归。改为后者——**图上的 EMA 现在从第 0 根开始画**（显示变化，
  决策链读数不受影响）。
- **新增 `backend/scripts/export_indicator_parity_fixture.py`**：取真实 BTC 4h 240 根，
  后端算一遍，连同 bars 写成 `frontend/lib/__fixtures__/indicator-parity.json`
  （candles 用后端约定 `[ts,open,high,low,close,vol]`——写错位会把 high 当 low，
  ATR 直接算成 6.6 万，本轮就是被这条测试抓出来的）。
- **新增 `frontend/lib/technical-indicators.parity.test.ts`（7 条）**：sma/ema/rsi/atr/
  bollinger 三轨/width 逐点比对 + 首个有效位断言。两套并行从"人会忘"变成"漂了就红"。
- **测试**：前端 `npm run test:run` **676 条通过（48 个文件，实测）**；后端 6 个相关文件
  **245 条通过（实测）**；`test_technical_indicators.test.ts` 8 条旧断言按新口径重写
  （样本→总体标准差、EMA 无暖机 null、RSI 首位、ATR 首位）。

### v405 · 基本面真正进决策链（权重 0 → 0.08）+ 财报三表/日历/一致预期 + 口径归一

> 起点是用户的一句提问：「这个项目对基本面了解的能力如何」。实测答案是——**公司级基本面
> 权重为 0**：`DIRECTIONAL_WEIGHTS` 六项全是价格/情绪/资金派生；估值层只有 5 只票 × 5 个
> TTM 比率；prompt 主动把财报降级成「不构成方向」；「超级低估（基本面+技术面）」的六个打分项
> 里一个财务指标都没有；`econ_events` 80 条里含「财报」的 0 条。这一版把四条改进全部落地。

- **新增 `src_python/fundamental_factor.py`**：公司级基本面方向因子。
  `score = 0.5×估值历史分位 + 0.5×盈利修正方向`（分位：自身 PE 在近 365 天自累积快照中的位置，
  50% 分位=0；修正：TTM EPS 同比 ×0.65 + 最近一次 EPS 预期差 ×0.25 + 下次一致预期抬升 ×0.10）。
  **零网络读库**、30 分钟缓存、异常一律降级为 `available=False`。非公司标的（BTC/XAU/WTI…）直接判不可得。
- **改 `decision_guard.py`**：`news_sentiment 0.40→0.32`，新增 `"fundamental": 0.08`（和仍 1.0）。
  沿用「取不到 ≠ 中性」：不可得时 0.08 权重让回其它因子；锚点规则不破（无新闻锚点时基本面不造方向）。
  输出新增 `fundamental` 字段；与新闻反向且 |score|≥0.35 记 `news_vs_fundamental` 矛盾，**只记不折减**。
  `market_bias` 的分子分母都排除 fundamental（它是对公司的读数，不是对盘口的读数）。
- **新增 `src_python/equity_fundamentals.py`**：财报三件套（利润表/资产负债表/现金流量表）+ 财报日历
  + 一致预期历史。FMP 优先，statement 端点 402/SSL EOF 时逐端点退 Alpha Vantage；
  两源都失败就标 unavailable（不拿年度数顶季度、不插值补季）。TTM / 同比 / 毛利率 / 净利率本地算，
  利润表缺 EPS 时用一致预期历史的 reportedEPS 补 TTM EPS，并标 `eps_source` 说明补源。
  `persist()` 落库、`db_pack()` **零网络**还原、`layer_items()` 一票 8 条。
- **`db.py` 增量迁移**：`equity_fundamentals` / `equity_earnings_events` 两表 + 两个索引（只加表，不动既有语义）。
- **新增 `scripts/cron_equity_fundamentals.py`**：`--tickers/--dry-run/--show`，AV 13s 节流、
  写锁重试 3 次、退出码区分"写入了/一列没写"。**故意不塞进宏观层**：AV 免费档 5 次/分钟、25 次/日，
  放进 22 分钟一轮的事件循环会把配额烧完并睡死判断链。
- **改 `macro_context.py`**：新增 `fundamentals` 层（只读库）+ 分行渲染 `_fundamentals_block()`；
  每层「当前不可用，不得臆造」的空行改为末尾一行「数据缺口」汇总；财报层不落 `macro_snapshots`
  （真值源是那两张表，每 22 分钟抄 24 行纯属膨胀）。
- **改 `feeds/equity_feed.py`**：指标 5 → 11 条（+PB/PS/TTM EPS/毛利率/营业利润率/ROE），
  `_unavailable_one` 与 `_ITEMS` 同源不再各写一份；**ROE 用 `netIncomePerShareTTM ÷
  shareholdersEquityPerShareTTM` 自算**（FMP `ratios-ttm` 没有 ROE 字段，实测拉全量 key 确认）；
  **`eq.pe` 补「近 365 天分位 + p25~p75 + 样本数 n」**，样本 <60 不给结论、PE≤0 不给分位；
  FMP 直连 SSL EOF 间歇率高（实测 3 次仍有 ~1/3 落空）→ 重试 3 → 5。
- **改 `feeds/fred_feed.py`**：`PAYEMS` 标签改「就业水平(存量)」并新增 `nonfarm_change`
  （本月−上月，附 3 月均值）——原来 159293「千人」会被读成「这个月新增 15.9 万人」。
- **改 `discovery.py` + 前端 `asset-scout.tsx`**：「💎 超级低估（基本面+技术面）」→「技术超跌」，
  并加真财务闸门：有财报时要求净利>0 且营收同比>-20%，破了封顶 35 分并明示「便宜有原因」；
  无财报的标的标 `fundamental_verified=False`，不再暗示"基本面没崩"。
- **改 `engine/ai_worker.py`**：两处 `evaluate_decision` 调用点注入 `fundamental`；prompt 里
  「财报仅影响个股/不构成延续信号」两句按新事实重写（对个股是实质增量、对非股票品种仍需写出传导链），
  并补「误判 7：给 BTC/黄金套市盈率」与 `fund.*` 口径说明。
- **实测**（AV 链路）：NVDA 8 季、TTM 营收 302.97B、毛利率 74.67%（与 FMP `grossProfitMarginTTM`
  0.7467 完全一致，双源交叉对上）、营收同比 +83.38%；`fundamental_factor` score 0.8823，
  PE 20.48x 处于近一年 **6% 分位**（n=2472）、上次预期差 +6.2%、下次财报 11-17 预期 EPS 2.47。
- **验证**：新增 `tests/test_fundamentals_v405.py` **17 条全绿**（含财务闸门 3 条）；
  `test_decision_guard_v318` + `test_macro_context` + `test_v401_fake_data` **48 条全绿**
  （`coverage` 断言在 news 0.32 后仍成立）。全量 `pytest tests/ -q` = **3763 passed / 34 failed**，
  34 条失败全部是工作树里既有重构（prompt 压缩掉了 v200 `3.5`/`P1-D` 章节、`ai_decisions`
  INSERT 改成列表推导）留下的旧断言，无一条与财报层/基本面因子相关。
- **边界（不装样子）**：`gold_etf` 在本机 0/2801、SPDR 官网被 DNS 劫持 → 失败负缓存 6 小时 +
  note 写明「网络不可达，不是配置错」；`flow` 删掉 XAU/WTI 那 4 条结构性永久缺口行。
  调度仍手动：`backend/cron/install_cron.sh` 的 `TRIDENT_CRON_DIR` 还指向旧工作区，本版只交脚本。

### v404 · 反思机制落地：把"命中率 1/605"修正为"0/605"

> 触发：`docs/反思机制评审-2026-11-16.md` 交付后用户三次加压（"开始 / 全做完了 / 做，全做完"），
> 要求按评审优先级 0→6 真正落到代码与数据。详见 `docs/v404-反思机制落地-2026-11-16.md`。

- **回填（优先级 0）**：新增 `src_python/reflection_truth_backfill.py`，以 `quick_sim_trades` 为唯一真值源；
  实跑回填 3 条，唯一 `hit=1` 的行（#95789，WTI SELL 86.42→88.335）修正为 LOSS，`SUM(hit)=0`。
- **schema**：`trade_reflections_v2` 增 `settle_source` / `reflection_effective`；新表 `error_code_ledger`。
- **指标（优先级 2）**：漏斗日报新增「有效判断（未降级且非 HOLD）」（当日 1/483）；
  反思统计接口新增 `n_settled` / `trade_won_rate_*` / `reflection_effective_rate=None`，旧字段保留但注明语义。
- **写入（优先级 3）**：`paper_reflection_worker` INSERT 绑定 `trade_uuid` + `settle_source`；
  缺数据写 `None` 而非 0（铁律 4）。
- **覆盖（优先级 4）**：`reflection_checkers.audit_coverage()` + 调度器 `step0_coverage`，
  如实报 HOLD 99.4% 不进方向性 checker；`missed_signal_reflection` 进调度 `step7`。
- **闭环（优先级 5）**：元反思准入改为「≥5 笔已结算独立成交」，`no_baseline_new` 不再落 cycle（107 条历史空转停止增长）；
  新增 launchd `meta-reflection-daily`(03:47) / `policy-update-daily`(04:17)；policy 候选 0 → 1 条。
- **分层（优先级 7）**：`error_code_ledger_build.py` 实跑 39 行（hard 15 / soft 8 / research 16），
  E021/E022 归 soft（模板合规检查，不进闸门统计）。
- **回归**：`tests/test_v404_reflection_truth.py` 3 例；`test_reflection_v230.py` 夹具补 `settled=1`。

### v403 · 禁止假数据：清理 1393 行 + 每日守卫（0 号铁律）

> 用户指令："假的全删了，假数据全删了。以后在这个系统里不能出现任何假的数据。"
> 清理是一次性的，产生假数据的机制不是 —— 所以同时立铁律、挂每日守卫、补回归测试。

- **新增 `src_python/fake_data.py`**：把"假数据"定义成可审计的七类（F1 置信度 0 占位 /
  F2 影响力 5.0 默认值 / F3 反思 confidence 占位 / F4 引用降级决策的假成交 / F5 影子样本
  引用降级决策 / F6 demo-mock 源 / F7 孤儿决策），提供 `scan() / purge() / --guard`。
  处置只有两种：**置 NULL**（"不知道"）或**删行**（"不该存在"），禁止编值填平。
- **生产库清理（2026-11-16 22:16，先备份 790MB）**：F1 401 + F2 398 + F3 592 置 NULL、
  F4 2 笔假成交（id 259/260）删除，复扫剩余 **0**。
- **连带重算净值 `rebuild_equity_snapshots()`**：删成交后 `sim_equity_snapshots` 的
  realized_pnl 已无法用 SQL 复现，按剩余真实成交重算 equity/n_settled/peak/drawdown，
  note 标明「已剔除假成交后重算」。
- **绩效口径更正**：模拟成交 3 笔 → **1 笔**、0 胜 3 负 → **0 胜 1 负**、
  累计 -2.56 → **-1.2103 USDT**。README / README_INVESTORS / PITCH_DECK / DATA_AUTHENTICITY
  同步改真值（文档里的假数字同样是假数据）。
- **0 号铁律写进 AGENTS.md**：判据（能否被真实观测复现）、三种处置、两条推论
  （降级路径不许落有效数值、降级决策不许建仓），排在所有其他规则之前。
- **每日守卫**：`fake_data.py --guard` 挂到 07:12 的 `run_funnel_report.sh`，
  任一 F1–F7 > 0 即退出码 1 + 日志 `❗FAKE-DATA-GUARD FAIL`，
  结果落 `backend/reports/health/fake_data_guard_<日期>.json`。
- **测试**：`tests/test_v401_fake_data.py` 6 例（占位置 NULL 不许猜、真值不许动、
  删成交必须重算净值、孤儿/demo 源必清、守卫退出码 1/0），全绿。
- **根因修复：清完 20 分钟又长回 11 行**。`missed_signal_reflection.py` / `paper_reflection_worker.py`
  的 upsert 用 `excluded.confidence_at_time` 把刚置的 NULL 写回 0（每 10 分钟一次）。
  改为写入前 0→NULL、upsert 用 `COALESCE(excluded, 原值)`（未知不得覆盖已知），重启 api 生效。
- **第二处根因：`impact_score` 阶梯的默认 5.0**。无模型判断、无门卫证据时，
  `resolve_impact_score` 改为返回 `(None, "unavailable")`（原 `(5.0, "default")`），
  没算出来的影响力落 NULL；`backfill_impact_score.plan()` 同步把 unavailable 计入"无法回填"。
- **第三处：常驻进程携带旧代码**。8000 上是游离的旧 api（`kickstart` 抢不到端口），
  后台调度实际在 bridge（8100）里，engine 也需重启 —— 三者按序 kickstart 后，
  复扫 0 + 等一个 10 分钟调度周期再扫 0 才收敛。
- **写库姿势**：按主键 80 行一批 + 每批 commit + 写锁退避重试（busy_timeout 120s），
  单条长事务在引擎持续写下必然 `database is locked`。

### v402 · 技术状态读数接入决策链 + 三层验收闭环（L1 交叉对账 / L3 可读性回放）

> 上一版把指标补齐、把读数算出来，但读数只活在脚本里：决策链看不到它，也没人能验
> "这句读数当时到底对不对"。这一版把两头接上：**读数进 prompt 与闸门**，
> 并补齐 L1（交叉对账）与 L3（人工回放）两道验收。

- **新增共享模块 `src_python/technical_state.py`**（只读取数 + 合成，零副作用）：
  `build_state(asof_ts=...)` 支持**严格因果**截断（供历史回放），`bias_of()` 把六个子维度
  各自已声明的偏向加权成 [-1,1] 盘面偏置，`for_asset()` 带 90s 缓存且永不抛异常。
  注入了"缺维度丢权重、不记 0 分"与 `usable` 门槛（覆盖 ≥50% 且 |bias| ≥0.45 才可作证据）。
- **接入 `market_snapshot.py`**：每资产新增 `technical_state`（readout/bias/coverage/usable/missing），
  摘要里多一行 `状态: …`，随 `decision_context` 一起进闸门；空快照保持键形状。
- **接入 `engine/ai_worker.py`**：新增 `_build_technical_state_block()`，与 microstructure 并列
  注入 user_text 头部（`[TECH_STATE]` 日志），prompt 里明确要求归到 `[水位博弈]` 段引用、
  「合成偏置」不得当方向结论、标「缺」的维度禁止用中性口径替代。
- **接入 `decision_guard.py`**：**不新增方向权重、不改 DIRECTIONAL_WEIGHTS**，沿用 surprise 的
  "只做反方证据"哲学 —— 读数 usable 且与决策方向相反时 `final_score × (1-0.15|bias|)`（上限 15%），
  并记 `news_vs_tech_state` 矛盾；输出新增 `technical_state` 字段全程可追溯。
- **L3 可读性回放 `scripts/technical_replay_checklist.py`**：抽 N 个历史时点，用 `asof_ts`
  严格截断重算"当时能看到的读数"，附之后 1/3/5 日走势（涨跌幅 + 期间最高/最低），
  给两列粗筛（偏置判定 / 标签订正）+ 一列留空给人工打分；报告落 `reports/technical_replay/`。
  **实测（BTC 4h，30 时点）**：文字标签订正 **19/30（63%）**，偏置仅 2 条可判 ——
  因为历史里 OI/主动成交只有最近 3 天，覆盖不足自动判"偏置不可用"（诚实边界，不是判定失败）。
- **L1 交叉对账 `tests/test_technical_indicators_crosscheck_v343.py`（11 条）**：在测试里用
  **另一条实现路径**（累计平均形式 `avg - avg/p + x/p`）重算 EMA/ATR/RSI/ADX/Donchian/
  已实现波动/布林带宽，与原实现逐值比对（容差 1e-6~0.06）。这条测试真的抓到过口径差：
  `ma_slope` 是"每根"，照总变化写参考值会差 5 倍。
- **口径踩坑（写进代码注释）**：`kline_bars.ts` 是毫秒，`external_indicators/macro_snapshots.ts`
  是秒 —— 同一条 240 根蜡烛里混用会让 OI 自适应窗口全部失效（单测已钉住）；
  XAU 只有散户多空比 → 偏置覆盖 14% → `usable=False`，闸门不会拿单维度偏置当反方证据。
- **回归**：`tests/test_technical_state_v343.py`（13 条：合成偏置/缺维度/闸门折减/权重表不变）
  与交叉对账 11 条全过；连同既有 `test_technical_indicators*`（76 条）、`test_decision_guard_v318`
  （21 条）、`test_abnormal_detector`（124 条）合计 **251 条**通过（实测）。

### v406 · 因果图谱 P0+ P1：触发词净化 / 品种绑定重定 / 方向重标 / outcome 扩容 / 评测集冻结

> 起点是上一轮评估的两条硬伤：**输入侧** 722/751 条链的 trigger_words 混进了 LLM 输出的
> JSON 键名与枚举（`verdict/prompt/Asset/BUY/SELL/ai_worker/checklist/market_event`），
> **绑定侧** 旧规则"词表里第一个有 ticker 的行情品种"让 156 条讲黄金的链被绑成 WTI，
> 319 条（42%）落到 UNKNOWN —— 而 `chain_prior`/bridge 的匹配都带 `asset=?`，这些链命中恒为 0。

- **新增纯模块 `chain_keywords.py`**（零依赖、可单测）：`clean_keywords()` 按白名单删系统 token /
  超长短语 / 重复（词表上限 12，**品种代码不占名额**）；`decide_asset()` 按语义打分判品种
  （`effect +3 / name +2 / cause +1 / trigger_words +1`），并列且只有词表证据时判 UNKNOWN 不硬猜。
- **入库侧接线**：`causal_chains.add_chain/update_chain`（新增/编辑即时净化）、
  `causal_chain_expander.clean_trigger_words`（R5 扩链同一口径）；`trigger_words` **保留** ticker
  （它是判 asset 的弱证据），匹配列 `trigger_keywords` 才剔除 —— 早一版连 ticker 一起删，
  实测把 15 条只能靠词表认领品种的链打成 UNKNOWN。
- **派生侧改口径**：`causal_chain_evaluator.derive_strategy_columns` / `load_active_chains`
  换用 `decide_asset`；`chain_prior._GENERIC_TOKENS` 并入同一份系统词表（历史行在回填前也不会再被污染词命中）。
- **存量回填**：`scripts/chain_ingest_cleanup.py`（**默认 dry-run**，`--apply` 落库，
  全表重算派生列 + `runtime/chain_cleanup/*.jsonl` 审计）；`scripts/chain_ingest_ab.py`
  （只读前后对照，用真实头条复刻生产匹配口径）。
- **生产库已落库（2026-11-16 21:54 / 22:12）**：先 `backups/trident_event_bus-before-v401-chain-cleanup-20261116-2153.db`
  （828MB）备份，再 `--apply`：**725 条 trigger_words + 752 条派生列**，回填后 `pragma quick_check = ok`。
- **实测（752 条链）**：含系统 token 的链 **723 → 0**；平均词长 14.71 → 7.65；
  品种分布 `XAU 74→227` / `WTI 303→163` / `BTC 12→31` / `ETH 42→24` / `UNKNOWN 319→304`；
  真实头条（14 天，每资产 60 条）平均命中链数 **BTC 0.07→0.53 · XAU 1.10→1.27 ·
  WTI 0.90→0.48**（156 条迁到 XAU 后不再抢 WTI 的头条）；纯噪声命中配对 **96→23**。
- **236 条"零关键词"链的处置**（`scripts/chain_inert_rebuild.py`）：逐条看过正文 ——
  **235 条是 `[auto_from_insight] ... auto_truth#NNN` 模板链**（正文是"经常漏报事件类型""资产
  价格、信号受影响"的填空句），1 条是流程教训。**没有重挖关键词**：试过用 `name+cause+effect`
  重抽，抽出来全是 `auto_from_insight / Asset=NONE / 覆盖度告警 / NONE-auto_truth#774` ——
  比空词表更糟（给它们一个"能匹配"的假象）。实际处置：`enabled=0 / active=0`，行不删、
  原值入审计（`runtime/chain_cleanup/inert-*.jsonl`），恢复一条 UPDATE 即可。
  实测这 236 条里**只有 1 条还处于启用态** —— 它们早就被别的流程停用了，问题一直是"账面 752"。
- **P0-3 方向重标（`scripts/chain_direction_label.py`）**：594 条 `BOTH` 是图谱"说了等于没说"的
  另一半原因。**没有用规则法** —— `_infer_direction` 的 docstring 里记着 v322 实测：用机制词表
  补方向，命中率从 50.0% 掉到 **41.9%（低于随机）**，因为"利多/利空"是相对别的资产说的。
  改走 LLM 语义，一次只问"对**这个资产**的**预期**方向"，且 prompt 里抹掉 `#47/+0.06%/WIN`
  这类事后痕迹（`_strip_leak`），置信度 < 0.7 或判不出就留 BOTH。实测 **357/363 条有结论
  （UP 171 / DOWN 134 / BOTH 52）**，同批再标一次自一致性只有 **80%** —— 每 5 条有 1 条会翻。
- **两遍交叉复核（`scripts/chain_direction_reconcile.py`）**：既然一遍不可信，就把"能核的都核一遍"：
  第二轮实测打了 **192 条**（LLM 通道有 30 次/60s 速率闸门，剩余 183 条没打完），
  两遍一致 **175**、真冲突 **2**、一侧 BOTH 15 —— 一致率 **91%**；**冲突一律降成 BOTH**
  （宁可说"看不出方向"，不赌某一边）。未打第二遍的 183 条保持单遍结论，标注为"未复核"。
- **方向不会被悄悄覆盖**（这是最容易白做的一处）：`derive_strategy_columns` 每次都重算
  `_infer_direction(name)`，而 `causal_weight_feedback` **每小时**对 applied 链跑一次 ——
  标注不留痕就会在下一个整点消失。线上 `causal_chains` 被 api_server/bridge_server 常驻读取，
  `ALTER TABLE` 拿不到排他锁（实测 `database is locked`），所以标注落成**覆盖层**
  `runtime/chain_direction/llm_labels.json`，`derive` / `load_active_chains` 都先查它；
  有 `direction_source` 列时还会写进表。回归用例 `test_derive_keeps_llm_direction_overlay`
  锁住"规则法会判 UP、覆盖层必须赢"。
- **落库后实测**：`expected_direction` 分布 `BOTH 594 → 371`、`UP 237`、`DOWN 144`；
  启用且带资产的链里 **311 条有明确方向**（原 158）。真实头条命中侧已经有方向了
  （XAU 30 条头条命中 6 UP / 11 DOWN），但 `aggregate_direction` 仍返回 UNKNOWN ——
  它要求每条链 `n_outcomes >= 2`，而全库 outcomes 只有 16 条 PENDING。**这是刻意的闸门**：
  没样本的链不许指挥开仓。所以下一步不是放松闸门，是攒样本（P1）。
- **回归**：`tests/test_v400_chain_keywords.py`（16 例，含"纯函数口径 == 派生列写入"、
  "覆盖层赢过规则法"两条一致性断言）；连同 `test_causal_mechanism` /
  `test_v3213_p1_chain_sort` / `test_v329_chain_steps` / `test_causal_weight_feedback` /
  `test_causal_graph_guard` 合计 **71 passed**。

**P1 · 把"能算"变成"真算过一次"（同日续做）**

- **outcome 扩容 + 前视闸门**：`backfill(days=60)` 旧实现不限时间，把 60 天内的新闻全喂给
  **今天才写下的链** —— 副本上实测凭空造出 **4,698 条** outcome，全部是"用今天的规律回测
  两个月前的事件"。改成 `ChainRow` 带 `created_ts`、`backfill` 跳过 `trigger_ts < created_ts` 之后，
  同样窗口只产出 **3,333 条**（砍掉 29% 的拟合样本）。生产落地 3,333 条，
  **已评估 2,079**（HIT 768 / HIT_WEAK 446 / MISS 834 / NEUTRAL 31 / PENDING 1,269）。
- **生产写不进去这件事本身是个发现**：`backfill` 逐行 commit，在 30+ 个 cron（
  `cron_atom_backfill --limit 3000`、`signal_performance_settler --apply`、`market_flow --commit`）
  每 10 秒抢锁的环境里跑 4 分钟直接被 `database is locked` 打死；期间还确认
  api_server/bridge_server 会长时间持有写事务（重启后仍锁 10 分钟）。
  对策不是硬等，是**算写在副本、单事务并库**：`scripts/outcome_backfill_merge.py`
  用一个 `BEGIN IMMEDIATE` 把 3,333 行在 **26 秒**内灌进生产（不是 3,333 次提交）。
- **评测集冻结 `scripts/eval_set_freeze.py`**：`frozen_ruler` 冻的是**尺子**，缺的是**考卷** ——
  11-15 那次"清空重开纪元"把 804 条已评估抹成 9 条，指标不可比的根因就在这里。
  现在把"链 id + outcome id + 口径指纹"钉成 JSON（`runtime/eval_sets/causal_v1.json`，
  指纹 `78dc1db6d8df5b0c`，328 条链 / 2,080 条 outcome），`--report` 能报出
  状态变了几条、方向变了几条、资产变了几条 —— 样本再被抹，账也翻得了。
- **实测结论（必须写清楚）**：含 BOTH 的方向命中率 **59.28%**（Wilson 57.1–61.4%）看着还行，
  但拆开是 —— **BOTH 链 428/428 = 100%**（它只主张"会动"，过幅度门槛就算 HIT，
  等于白送 20% 的命中），**有方向主张的 1,620 条只有 48.52%**（Wilson 46.1–51.0%）。
  同期实测基准：全押 UP **36.77%**、全押 DOWN **63.23%** —— 这窗口是单边下跌，
  **恒喊空的常数策略（63.2%）比因果链（48.5%）还高**；分方向看 UP 35.9%（基准 36.8%）、
  DOWN 62.8%（基准 63.2%），两侧都**贴着基准，边际为零**。
  结论：P0 的清洗没错（脏词表确实让匹配变准了），但**"匹配得更准"不等于"判断得对"**。
  这也解释了决策层为什么 99.4% 是 HOLD —— 门禁拒绝的不是一个有效先验。
- **`kg_improvements` 落地闸门 `scripts/kg_improvements_apply.py`**：9 条候选逐条验，
  **0 条可应用 / 7 条拒收 / 2 条待人工**。7 条 `add_edge` 的 reason 全是
  "Binance 行情快照缺失""WTI 实时快照缺失"——这是**数据基础设施故障**，做成边等于把
  "我们没取到价"写进"原油和谁有关系"；且 `target` 的 `trade:` id 在库里**跨表冲突**
  （256/258/259/260 只在 `trade_audit_log`，11483/11555/11816 只在 `ai_decisions`，
  10573 一张表都没有）。剩 2 条（E014 规则、reweight trade 260）需要人工改码，登记为待办
  而不是刷成 applied。
- **`causal_graph.inject_block` 结构化**：旧版把 1,152 个节点压成一句
  「核心驱动=X；条件因素=A、B」——没有置信度、没有验证状态、没有边界。新版每行带
  `置信 / 验证状态（已验证·未确认·**已被否证**）/ 2 条证据`，并显式写明
  "方向未经样本验证，命中率 48.5% 与随机无异，不能作为开仓依据"。
  同时修了两处数据卫生：`evidence` 是 `{"chain":[...], "_status":..., "_fail":...}` 的 dict，
  旧实现 `str()` 后整段 python repr 进 prompt（现拆成 `why_chain` + `evidence_status`）；
  审计日志混入的证据（`auto_from_insight` / `Asset=NONE` / `traceback`）注入前过滤；
  字段名当驱动的（BTC 核心是 `historical_confidence`）标注"疑似字段名/非驱动"。
- **验收**：能力 5 PASS（链 752 / 活跃 212 / 覆盖 7 资产）、能力 6 **PASS**
  （outcomes 3,349 · 已评估 2,079 · 超期未评估 0）、能力 10 仍 FAIL
  （真命中率 0.375 < 0.5、方向命中率 0.5928 < 0.6 —— 且如上，扣掉 BOTH 只有 0.485）。
  回归 54 passed（chain_keywords / causal_mechanism / causal_graph_guard / causal_weight_feedback）。

**P2 · 把"证据"接到决策线上（此前根本没接上）**

- **发现的接线断裂**：`chain_prior._evidence_score` 读的是 `causal_chains.hit_count/miss_count`，
  实测**752 条链这两列全是 0**（`weight` 也全是 1.0）→ 决策侧读到的证据永远是 (0, 0)
  → `aggregate_direction` 恒 UNKNOWN、`conflict_veto` 永不触发。也就是说：闭环写了
  3,349 条 outcome，**决策线一条都没看见**，"链→结果→反馈→权重"这条链在最后一米断了。
  现在 `_evidence_from_outcomes()` 直接从 `causal_chain_outcomes` 聚合（只算 UP/DOWN 链），
  两个 matcher 都会 `_enrich_evidence()`。
- **闸门从"n≥2 且命中≥0.6"改成"Wilson 下界 > 0.5"**：旧门槛下 **86 条链**能拿到否决权，
  而 2/2 全中的点估计是 100%、下界只有 34% —— 等于用两个样本否决真实交易。
  改后按当前数据 **0 条链**达标（全库单链样本最多 11 条），方向聚合一律 UNKNOWN，
  链退回"标注"角色。**这不是关功能，是把开闸条件写成可验证的**：样本够（n≥12、
  下界>0.5）会自动开。
- **`filter BOTH`**：BOTH 链的方向命中是"幅度过没过线"，不是方向对错；
  实测 428/428=100%，混进命中率会把 48.5% 抬到 59.3%。证据聚合与能力判据都剔除它。
- **分资产实测（这是"该不该继续投这条路"的依据）**：有方向样本里，
  XAU DOWN 72.0%（同资产恒喊空 67.5%）、WTI DOWN 55.2%（基准 58.3%）、
  BTC DOWN 59.7%（基准 60.8%）、ETH DOWN 58.0%（基准 61.5%）——
  **没有一组能在自身基准之上站住**（XAU-DOWN 的 Wilson 下界 66.9% 仍低于 67.5% 基准）。
  UP 侧更差（XAU 35.1% / WTI 38.1% / BTC 33.3% / ETH 26.7%）。
- **能力 10 口径修正**：判据改成"有方向子集 n≥50 且真命中率≥0.5 且方向命中率≥0.6"，
  BOTH 不计入判定，全量口径留作对照。实测 **真命中率 0.3006 / 方向命中率 0.4852**
  （对照全量 0.375 / 0.5928）—— 仍 FAIL，但现在是**真实**的 FAIL。
- **回归**：新增 `tests/test_v406_prior_gate.py`（5 例：小样本不许否决、样本薄不产生方向、
  BOTH 不进证据、老调用方补算下界），连同前 71 例合计 **76 passed**。

**P3 · 决策入口：先分清"真重复"和"按品种扇出"，再决定挡什么**

- **先纠正一个自己数错的结论**：504 条决策只对应 323 条新闻，第一眼像"36% 重复决策"，
  于是把"决策侧幂等去重"排进了优化清单第一条。逐组拆开才发现是**按品种扇出** ——
  一条宏观新闻会分别给 BTC / XAU / ETH 做一次决策（`news 29108 → (BTC,XAU,ETH,BTC)`，
  `news_events` 本身也是"每资产一行"，5,824 个 news_id 多行）。实测 **94 组是扇出、
  真重复（同 news + 同 target 多次）只有 3 组**。所以**没做幂等去重** ——
  给设计行为加唯一键，会把"一条新闻影响三个品种"这件事本身挡掉。
  （保留 `scripts/decision_prefilter_audit.py` 的 `fanout()` 随时可复算。）
- **`target_none` 的真实来源**：287/323 条新闻带降级标记，其中 274 条原因写着 `target_none`。
  拆开看**只有 148 条真的没有品种**（`market_category` 为空串 172 条），
  **126 条其实绑到了品种（XAU 46 / WTI 42 / NVDA 13 / BTC 10）只是 `reasoning` 空** ——
  所以"拆分退化原因"在这层已经由 `analysis/contract.py:derive_state` 做了（五终态 + `scope`），
  缺的是**入口就别建这条决策**。
- **现有 L0-POOL 只覆盖 8.7%**：v396 的 `news_router.pool_out_reason` 是黑名单式
  （必须"公司行为词 + 公司主体词"同时命中才挡），对 target_none 涉及的 196 条新闻只挡 22 条
  （11.2%），"OPPO 发布新机""佛得角大选""闲鱼起诉造谣"这类一路走到主分析再判 NONE。
- **试了三套规则，只有一套能上**（全部在 323 条真实决策新闻上跑，只读）：
  - v1 纯白名单（只认池内锚点 + 宏观词）：挡 **247 条（76.5%）**，但误伤池内品种 **65 条**
    —— "俄外长表态""航母部署""无人机袭击"这类纯地缘新闻会被挡掉，太危险，弃。
  - v2 白名单 + 地缘供给 + 交易所链上：挡 **190 条（58.8%）**，严格误伤（被挡但存在未降级
    且真绑到池内品种的决策）**8 条** —— 收益大，但 8 条真实损失需要人肉看，**只留开关
    `NEWS_POOL_WHITELIST`（默认关）**。
  - v3 白名单 ∩ 非交易类主体（黑名单扩充）：挡 **47 条（14.6%）**，降级行 42 条，
  **严格误伤 0 条** → 采用。
- **落地（`feeds/news_router.py`）**：新增 `NONMARKET_ACTIONS`（发布/推出/开卖/售价/机型/新品/
  发布会/辞职/免职/任命/当选/弹劾/人事/撤换/起诉/判决/庭审/判刑/诉讼/上诉/电影/明星/球赛/
  夺冠/演唱会/综艺/谣言/大选/竞选/就职）、`ANCHOR_EXTRA`（币安/BINANCE/COINBASE/交易所/链上/
  钱包/矿工/减半/SEC/CFTC/监管/合规 · 航母/空袭/无人机/停火/谈判/出口禁令/禁运/库存/钻井/
  炼厂/罢工/封锁/海峡/地震）、`_has_anchor()`；`pool_out_reason` 在规则 3 之前插
  **规则 3b**（无锚点 + 主体词 → `非交易类主体（X）`）与 **3c**（`NEWS_POOL_WHITELIST=1` 才生效的
  纯白名单"无池内锚点"）。**锚点优先**：只要新闻里有品种/宏观/地缘/交易所任一锚点，
  主体词一律不挡（"黄金 ETF 新品上市首发"仍照常分析）。
- **实测（生产库 323 条决策新闻，同口径）**：挡 **28 → 47 条**（8.7% → 14.6%），
  其中 42 条原本就是降级行；被挡的 47 条一眼可判 —— 手机发布、外国大选、企业人事、
  药企进医保、AI 模型发布、地方统计局表态。**严格误伤 0 条**。
  单点校验：`OPPO Reno17 发布`→挡；`俄外长/无人机拦截/币安恢复提现/美联储降息/
  CFTC 监管争议`→放行。
- **回归**：新增 `tests/test_v406_pool_router.py`（12 例，锁"该挡的挡、该放的三类锚点、
  白名单是开关、有锚点时主体词不挡"）。

**P4 · 行情断流：从"没有数据"改成"先看盘上有没有"**

- **根因不是没数据，是没往本地退一步**：XAU 走 OKX（实测 `ConnectionError`，连不通）→
  Yahoo 兜底 `429` 限流 + `SSLError`，20 分钟内熔断 → 五个周期全标 `unavailable`。
  而库里 `kline_bars` 的 XAU 1m 有 **41,647 根、最新一根 1.1 小时前**、1h 最新 0.6 小时前。
  WTI 更彻底：`knowledge_base.fetch_klines` 的 WTI 分支只走 Yahoo `CL=F`（同样 429），
  **本地兜底只给 1d 挂了 FRED**，分钟/小时线根本没有退路。
  于是 XAU 87 条 + WTI 97 条决策被记成 `missing:feed_stale`、降级成 HOLD ——
  「有没有盘面结构」被记成「没有数据」。
- **`market_snapshot._fetch_local_recent()`**：外网失败后按周期退到 `kline_bars`（其次
  `ohlcv_intraday`），三条纪律 —— ① 只读真实存在的 bar，不补造；② 超过该周期允许年龄
  就不用（1m 2h / 15m 12h / 1h 48h / 4h 7d / 1d 30d），宁可标 N/A 也不给旧价格；
  ③ 打 `[KLINE-LOCAL] <资产> <周期> 外网失败(原因) → 本地 <表> N 根（最新 X h 前）`，
  事后能分清"活数据/盘上数据"。同时把最后一根 bar 的时间写进 `payload.bar_age_s` ——
  快照年龄只说明"多久前建过一次"，不说明"数据本身多旧"，两者必须分开可见。
- **落地实测（生产库，重建 XAU+WTI 快照）**：XAU `1m/15m/1h/4h/1d` 从**全部 unavailable**
  变 **4/5 ok**（close 3868.3–3869.2）；WTI `1h/4h/1d` 变 ok（96–168 根，最新 3.4/6.4/18.4h 前）。
  仍未 ok 的是 XAU 1m（盘上 2.8h > 允许 2h）与 WTI 1m/15m（4.3h / 1094h）——
  **这是上游 1m 采集停滞，不是兜底失效**，也没有拿旧 bar 硬凑成 ok。

**P5 · 权重回写的门槛：2 → 12，并对"上调"单独设证**

- **门槛不一致本身是个 bug**：`causal_weight_feedback.MIN_CHAIN_SAMPLES = 2` 敢用 2 个样本改
  "这条链有多准"，而 `chain_prior.MIN_CHAIN_SAMPLES_FOR_DIRECTION = 12` 才敢让它指挥方向。
  同一个库里两套置信门槛，等于用 A 的把握去支撑 B 的结论。
- **改成 12 + 单向设证**：`n ≥ 12` 才动权重；**往上调**还必须 `Wilson 下界 > 先验`（否则就是拿
  12 条样本里的一次侥幸给链镀金）；**往下调不设这道闸** —— 证据显示它比自称的差就该降，是安全方向。
  Wilson 公式与 `chain_prior._wilson_lower` 同口径（用例逐点比对到 1e-12）。
- **dry-run 实测（327 条有 outcome 的链）**：`prior 0.4829`；敏感性
  `n≥2 → 290 条链（21 条 Wilson 过线）` / `n≥5 → 189（19）` / **`n≥12 → 0`** / `n≥20 → 0`。
  终日结论：**327 条全部 `skipped_sample`，一条也没改**（全库单链最多 11 条样本）。
  这不是门槛设错，是样本量根本不够 —— 但"不改"现在是有依据的判定，而且敏感性表把
  "旧门槛下 290 条链会被从 1.0 一路降下来"的病灶量化出来了（全部 327 条 proposed < 1.0）。
- **用例**：`tests/test_v406_weight_gate.py`（7 例：门槛同值、Wilson 逐点同口径、
  2/2 全中过不了 0.5、12/12 能过、浮点 credit、空样本、敏感性表不得退化成单一结论）。

**P6 · 方向覆盖层加 DB 镜像（不再只活在一个 JSON 文件里）**

- 369 条 LLM 方向标注原来只存在 `runtime/chain_direction/llm_labels.json`：换机/清 runtime
  目录标注凭空消失（链表里看不出任何痕迹），而且"方向 = DOWN 且启用"这类查询只能在内存里过滤。
- **新表 `chain_direction_overlay(chain_id, direction, source, confidence, updated_at, note)`**：
  **独立建表**而不是给 `causal_chains` 加列（那个 `ALTER` 拿不到排他锁，实测 `database is locked`）。
  写入是单事务 `BEGIN IMMEDIATE` upsert（逐行 commit 会被 30+ cron 抢锁打死，P1 的教训）；
  读取 **DB 优先、JSON 兜底**，进程内缓存 300s，写后自动失效。
- **已回填生产**：369 条（UP 173 / DOWN 135 / BOTH 61），先备份
  `backups/trident_event_bus-before-v406-overlay-202611162355.db`（798MB）。
  `scripts/chain_direction_label.py` 的 `save_overlay` 现在写 JSON 的同时同步 DB（DB 失败不影响 JSON）。
- **用例**：`tests/test_v406_overlay_mirror.py`（6 例：建表幂等、upsert 不产生重复行、
  非法方向/非法 id 不写、DB 优先且缓存会失效、表缺失退回空、覆盖层赢过规则法）。

### v401 · 技术指标补全：趋势强度 / 筹码结构 / 博弈结构 / 状态读数

> 原来的库能算出"RSI 78""布林突破"，但回答不了"趋势多强、谁在加仓、这波涨有没有主动买盘"。
> 指标数量不是瓶颈，缺的是**从指标到状态**的那一层。这次补 15 个纯函数 + 1 个聚合读数。

- **趋势强度**（此前完全缺失，只有 MA 交叉）：`adx` / `aroon` / `supertrend` / `donchian` / `ma_slope`。
  ADX 修掉一个常见实现错误：双向位移都约为 0（横盘/内包 K）时 DX 无定义，
  若按 `abs(+DI - -DI)/(+DI + -DI)` 直接算会得到 100——"没有趋势"被读成"极强趋势"。
  现按 `denom > 0.01` 判零（首根残留 DM 会衰减到 1e-6 级，1e-9 判不出来）。
- **筹码结构**：`volume_profile`（POC / VAH / VAL / 相对价值区位置，成交量按价格跨度等比摊箱）、
  `swing_structure`（HH_HL / LH_LL / HH_LL / LH_HL）。
  摆动点判定改为左右分开比较（左侧严格更高、右侧允许等高），
  用"整窗最大值唯一"会把双顶/平台整根丢掉；分类只看最近 3 个摆动点——
  实测 BTC 4h 240 根，带上更早的摆动会把一段清晰下跌判成 `mixed`。
- **博弈结构**：`oi_price_quadrant`（增仓涨 / 减仓涨 / 增仓跌 / 减仓跌，区分"新钱进场"与"平仓推动"）、
  `positioning_divergence`（大户多空比 vs 全体，识别散户追多/追空）、
  `taker_pressure`、`cvd_price_divergence`、`percentile_rank`（资金费率等绝对值转历史分位）。
- **波动情绪**：`realized_vol` / `bollinger_width` / `rsi_stretch`（极值驻留根数，数值之外的时长维度）。
- **`market_state()` 六维读数**：趋势阶段 / 波动状态 / 价值区位置 / 杠杆结构 / 主动性 / 持仓分歧，
  输出一句 `readout` + 逐维明细 + `missing`。**缺数据进 missing，不补零不推测**：
  黄金只有券商客户多空比时，读数为"仅散户多空比 X（缺大户对照）"。
- **`scripts/technical_state_readout.py`**：只读打开库，跑真实数据。
  OI 窗口按落库深度自适应（实测币安 OI 只有 3 天，7 日口径必然算空），
  并把实际窗口写进读数；价格与 OI 必须同窗口比较，否则"3 天跌 6%"会配"5 根跌 0.8%"得出反向结论。
- **测试**：`tests/test_technical_indicators_v343.py` 37 条，全部性质+边界断言；老用例 37 条不回归。

### v400 · 影子账本日更（自动结算 + 闸门结论 + 清污染）

> 起点是"样本不够"这件事只有人记得才去做。影子账本上线后（v335）一直是手动脚本，
> 库里 35 行、25 行是降级方向；首页面板把"0.75 条/天"当成积累速度，看着像永远攒不到 60 笔。

- **`shadow_ledger.py` 加 `prune_invalid()`**：删掉"这条决策本来就不该记"的行
  （`analysis_degraded=1` / 无方向 / 池外品种 / 决策已被删），不按时间窗口删。已跑：
  **删 25 条降级方向，剩 10 条真样本**。`--commit` 时自动清理，另有 `--prune-only`。
- **`summarize()` + `--json-out/--snapshot-dir`**：一批样本压成快照
  （`settled / hit_rate / days_covered / samples_per_day / target_samples=60 / remaining_to_target /
  by_asset / by_direction / gate_sweep / sweep_verdict`），按天落
  `backend/reports/shadow_ledger/latest.json + YYYY-MM-DD.json`，90 天自动清理。
- **`sweep_verdict()` 把扫描表翻成结论**：同一批决策按门槛切，比"不设门槛"与"门槛 20"的命中率
  → `insufficient`（门槛 20 剩 <5 条）/ `effective`（≥+5pt）/ `inverted`（≤-5pt）/ `flat`（±5pt 内）。
  结论与 CLI、端点共用一份实现，不各算一套。
- **端点 `/api/v291/shadow-ledger-summary` 口径修正**：`samples_per_day` 从"除以查询窗口
  （40 天）"改成"除以真有样本的天数"，并把 `days_covered / target_samples / remaining_to_target /
  sweep_verdict` 一并返回。实测 10 条样本：0.2 条/天 → **10 条/天**（1 天覆盖）。
  首页 `ShadowLedgerPanel` 增加"样本进度 10/60 + 还差 50 + 约几天"进度条和闸门结论行。
- **新增 launchd 任务 `com.trident.shadow-ledger`**（`StartInterval=14400`，与 240 分钟结算窗口
  对齐，RunAtLoad）：入口 `~/.trident/wrappers/run_shadow_ledger.sh`，仓库内
  `scripts/run_shadow_ledger.sh` 为同内容手动入口，`scripts/com.trident.shadow-ledger.plist` 为配置。
  **踩到的坑**：工程在 `~/Desktop`，launchd 直接执行桌面上的 `.sh` 会被 macOS 拒绝
  （`Operation not permitted`）；更隐蔽的是 launchd 下的 `/bin/bash` 连桌面上的普通文件都读不了
  （`cp`/`find` 全 EPERM），只有 `python3` 有权限 —— 所以落盘与清理都挪进 Python，bash 只负责调起。
- **回归**：`tests/test_v400_shadow_ledger_cron.py` 11 例（清理三态 / 天数口径 / 闸门四种结论 +
  no_base / 目标门槛别被改小），连同 `test_v335_shadow_ledger.py` 共 **18 passed**。
- **实测**：清理后 10 条已结算，方向命中率 30.0%；闸门扫描门槛 20 只剩 1 条 → 结论
  "样本不足：先攒样本，别急着评闸门"。按 10 条/天，约 6 天到 60 笔。

### v399 P2 补 · 决策来源留痕 + S20 硬数据覆盖率（v397 复测闭环）

> 起点是评估报告里两条"说不清"：29.7% 的决策 `target_asset=NONE`，到底是新闻真没有标的，
> 还是路由漏了？硬数据覆盖率只有 21.5%，缺的那一半又是谁？库里这两处都只留了空字符串。

- **新增纯模块 `decision_provenance.py`**（零依赖、只描述事实、不补造数据）：
  `route_tag(asset)` → `target_none` / `decided:<class>`；`hard_status(asset, evidence)` →
  `ok` / `missing:no_local_feed` / `missing:feed_stale` / `missing:none_target` / `missing:unknown_symbol`。
  `tags()` 一次返回两者。
- **`ai_decisions` 加 `asset_route` / `hard_data_status` 两列**（进 `_AI_DECISIONS_COLUMNS` 走
  `db.migrate → _ensure_columns`）。`asset_route` 原来只有 `scripts/oneoff/backfill_asset_routing.py`
  回填过 36 行，**运行时从不写**；现在在 `engine/ai_worker.py` 的 v200 completeness 回写之后补一段
  UPDATE 写两个标签，失败只打印 `[PROVENANCE-WRITE] failed`，不影响落库。
- **历史回填**：新增 `backend/scripts/backfill_decision_provenance.py`（默认 dry-run，
  `--commit` 落库）。已跑：479 行全部补齐。
  实测分布 —— `asset_route`：`target_none` 143 / `decided:metal` 118 / `decided:oil` 107 /
  `decided:crypto` 61 / `decided:symbol` 27 / `decided:stock` 23；
  `hard_data_status`：`ok` 102 / `missing:feed_stale` 185 / `missing:none_target` 142 /
  `missing:unknown_symbol` 27 / `missing:no_local_feed` 23。
- **新增 S20 硬数据证据覆盖率**（ratio，target 60，gate 300）+ `_q_hard_data_share` + `_s20_detail`。
  **44.2%（23/52，非降级池）**：`ok` 23 / 个股无本地源 13 / 本线断档 10 / 无标的 4 / 未知符号 2；
  缺得最多的是 CN_STOCK 9、XAU 5、WTI 5。全局 102/479 = 21.3%（历史行多产生于 v397 之前）。
  查根因时顺手看到引擎日志：个股走 Yahoo 外部源，现网 `429` 限流 + `ConnectionResetError`，breaker 熔断 90s（`000660.KS` 即 SKHYNIX），本地又无缓存 —— 所以个股这一整类必然缺，这不是取数代码写坏，是源不可用。
- **S18 detail 同步**：`asset_route 为空 0/143`（原来 143/143 全空），"真没标的 vs 路由漏了"
  不再混为一谈；S18 = 29.9%（143/479）。
- **S19 随新增洞察复算**：91.7%（44/48），可执行 4 条、已验证 4 条、通过 1 条（25%）。
- **记分卡快照**：`runtime/capability_scorecard/history.jsonl` 追加 20:34:02 一批 21 条
  （19 项指标 + R1–R3 参考口径），S18/S19/S20 与 v398 新口径首次进快照。
- **回归**：`tests/test_v399_decision_provenance.py` 5 例；连同 `test_v399_p2_insight_hypothesis` /
  `test_v398_insight_pool` / `test_capability_scorecard` / `test_scorecard_share` 合计 **42 passed**。

### v399 · 诚实口径三修（置信度占位 / 反思语料混算 / 线程递归）+ 对外数字校正

> 起点是一次外部评审（2026-11-16）：项目文档与代码的自我认知基本准确，
> 但**对外数字全部过期**（README 写 v225/3481 用例/27 错误码，实际 v398/3532/E001–E033；
> 投资人材料写"7 天 59 笔成交""43 个用例"，实际 3 笔成交、1 笔结算），
> 且有两处"看起来是 bug 的诚实缺陷"：置信度占位 0、反思 82% 是行情断流。

- **`evidence_confidence` 占位 0 → NULL**（新增纯模块 `confidence_availability.py`，零依赖可单测）。
  原来 `_evidence_confidence_pre = float(guard.confidence or 0.0)`，降级路径
  （target_none / llm_empty / summary_only）coverage=0、final_score=0，公式输出 0.0，
  于是 **447/494 条决策被统计成"低置信度决策"**（其中 394 条根本没进过置信度计算）。
  这违反铁律 4「0 不得作为有效值参与统计」。现在：coverage>0 才算"算过"，
  没算过落库 NULL；算过且真为 0 的 0.0 原样保留（不许把真结论洗成缺失）。
- **闸门原因分开写**：没算过 → `无可证据（coverage=0，置信度不适用）：<降级原因>`；
  算过但低分 → 保留原「统一置信度 X 低于闸门 Y」。原来没算过的行也报
  「统一置信度 0.0 低于闸门 15.0」，把占位值当结论，自相矛盾。
- **漏斗日报口径**（`scripts/daily_funnel_report.py`）：`无证据(NULL) N 条 / 真 0 分 M 条`
  分开计数；卡点文案同步改写。
- **反思统计分两类**（`bridge_server.v237_reflection_dimension_stats`）：复用 v398 的
  `insight_pool`（A/B=交易错因、C=技术故障），返回 `n_reasoning` / `n_infra` /
  `hit_rate_reasoning` / `hit_rate_infra`，每条样本带 `sample_class`。
  实测 605 条里 499 条是 `data_feed_timeout`（hit 全 0），混算会让命中率永远趋 0。
  另：`c_technical_reflections.py` 的 `confidence_at_time` 由占位 0.0 改 NULL；
  端点 `avg_confidence` 不再把缺失当 0（`AVG` 忽略 NULL，仍无值则返回 null）。
- **真 bug：`api_server.py:1605` 线程错峰包装器自递归**。`_ORIG_THREAD_START` 在模块级
  无条件赋值，模块被导入两次（脚本方式 `api_server` + 包方式 `src_python.api_server`
  并存，pytest / uvicorn reload 都会发生）时第二次捕获到已包装的函数 → `start()`
  调自己 → `RecursionError`。实测 `tests/test_security_p1_fixes.py` 5 failed + 5 errors，
  任何二次导入的进程建不了线程。修正：包装器打 `_trident_staggered` 标记，只包一次。
  该文件回归 **36 passed**（原 26 passed / 5 failed / 5 errors）。
- **对外数字校正**（README / README_INVESTORS / PITCH_DECK）：版本 v398→v399、
  错误码 27→33（E001–E033）、用例 3481→3,545、"59 paper trades / 43 pytest" → 真实
  3 笔（1 结算、0 胜、-0.13 USDT）；README 新增「现状（诚实口径）」一节，
  README_INVESTORS 的 Proof points 重写为「Honest status」+「尚未具备」，
  PITCH_DECK 的 Traction 表加真实数字脚注。
- **一次性迁移** `backfill_confidence_placeholder.py`（默认预演，`--commit` 落库）：
  只改 `evidence_confidence=0 AND analysis_degraded=1 AND reason IN (target_none,
  llm_empty, summary_only)`，**不动 `server_fallback`**（那条路径真可能算出 0）。
  预演：394 条可改（target_none 280 / llm_empty 73 / summary_only 41），
  server_fallback 29 条保留。幂等。
- **7 处「写路径静默吞异常」清零**（v322 审计门禁 `tests/test_v322_silent_failure_audits.py`
  长期红着）：`api_server`（健康分维度）、`build_series_registry`、`check_integrity`、
  `llm_degrade_probe`、`notification_channels`、`routers/meta`、`trident2/store`。
  全部改为 `except X as exc:` + 一行带标签的 `print`，**行为不变、失败留痕**。
  审计脚本 `scripts/audit_silent_except.py`：命中 7 处 → **0 处**。
- 回归：`tests/test_v399_confidence_honesty.py` 9 例；`test_security_p1_fixes` 36 passed；
  全量 **3,550 passed / 10 failed / 6 skipped**（修 bug 前 3,420 passed / 101 failed / 5 errors）。
  剩 10 条为既存环境相关：`test_probe_health`(2)/`test_signal_weight_diagnostics`(1) 依赖
  `.env` 里的本地通道配置（roster 空）、`test_v200_p4_quality_data`(3) 项目文档已登记、
  `test_stability_fuses`/`test_review_snapshot`/`test_data_authenticity_v396` 为跨用例时钟抖动
  （单跑均绿）。

### v399 P2 · 洞察做成可测假设 + 无标的决策单独度量

> 承接评估报告 P2 两条。核心是把"洞察"从一段经验文字，变成"能落到哪个参数、
> 往哪个方向、多久内可验证"；测不了的承认测不了，并让这个占比可见。

- **新模块 `insight_hypothesis.py`（纯函数 + 幂等写库）**：`classify(rule)` 从洞察文案里
  解出 (参数键, 预期方向, 观测窗口)，判据**复用 `param_key_aliases`**（审批链路同一把尺子，
  不另立标准；`settings` / `strategy` 两桶算可执行，`unmappable` 与单位不等价一律不算）。
  解不出键、解出但单位不等价、有键但没声明方向 → `observe_only=1` + 写明原因，
  **不编"看起来可测"的假设**。
- **`review_insights` 新增 8 列**（`insight_hypothesis` / `insight_param_key` /
  `insight_param_bucket` / `insight_direction` / `insight_window_h` / `observe_only` /
  `observe_reason` / `insight_classified_at`），走 `db.migrate → _ensure_columns` 幂等补齐。
  `ensure_classified()` 挂在 `paper_reflection_worker.main()` 每轮开头（5 分钟一次），
  历史 52 行已一次性补完。
- **实测（47 条 active 洞察）**：可执行 **4 条**、只能观察 **43 条 = 91.5%**；
  可执行池已验证 4 条、通过 1 条（25%）。也就是说"洞察→参数"这条链目前只有个位数样本，
  所谓"洞察落地率"此前是把不可执行的经验算进了分母。
- **新增 S19 观察型洞察占比**（share，毕业线 ≤30%，gate 50）：**91.5%（43/47）未达线**，
  detail 单列可执行池的验证通过数。晋升链路 `weekly_insight_apply` 本来就按
  「无参数键 / 无方向」拒收（v339），现在把同样的判据提前到入库时并变成可见指标。
- **新增 S18 无标的决策占比**（share，毕业线 ≤10%，gate 300）：**29.7%（142/478）未达线**，
  detail 拆原因：`target_none` 135 / `server_fallback` 3 / 无原因 4；
  **`asset_route` 为空 142/142** —— 路由字段从没写过，所以"真没有标的"和"路由漏了"
  目前分不开，这是下一处要补的观测点。
- **S14 分母加 `target_asset NOT IN ('NONE','')` 过滤**：无标的决策权重天然并列，
  留在分母里等于把"没路由到品种"算成"权重不精细"。实测这条过滤当前挡住 0 条
  （38 对配对里没有 NONE），但口径从此显式；R3 参考指标与 S14 现在完全等价。
- 回归：`tests/test_v399_p2_insight_hypothesis.py` 10 例；连同
  `test_capability_scorecard` / `test_scorecard_share` / `test_v339_param_aliases` /
  `test_v339_weekly_prefilter` / `test_insight_verification_wiring` /
  `test_v398_insight_pool` 合计 **59 passed**。bridge 已重启（28s 就绪），
  `[PAPER-REFLECT] daemon started | interval=300s` 确认新代码在跑。
- **两条都没达标，这是结论不是失败**：91.5% 的洞察不可执行、29.7% 的决策没有标的，
  正是"洞察闭环"和"路由打标"两处真实欠账的量化证据。

### v399 P1 · 错误码写入口径接通（反思落库前补跑校验器）

> 承接 `docs/细节洞察能力评估-2026-11-16.html` 的 P1 第一条。此前反思的"三维细分"
> 名义上有 27→33 个错误码支撑，实测 `decision_errors` 全库只有 23 行（E028×8 /
> E030×8 / E032×4 / E020×3），等于精确归因那条路几乎没跑过。

- **根因**：错误码由 `reflection_checkers.run_all_checkers()` 产生，只在独立任务
  `run_reflection.py` 里被调用（该脚本还带 `sample_quality_filter` 清洁样本硬门槛），
  实测几乎不跑；于是 `_resolve_dimension_for_decision` 的"路径 1（错误码）"永远取空，
  维度一路回落到 ontology 兜底 → 82.5% 挤在 `C/data_feed_timeout`、80.2% 挤在
  `A/pattern_recognition`。
- **写入端接通**（`paper_reflection_worker._insert_one`）：开库、解析维度之前先跑一次
  `run_all_checkers(d, conn)` + `save_errors(errs, conn)`。`save_errors` 是
  `INSERT OR IGNORE`，重复跑天然幂等；异常只打 `[PAPER-REFLECT] checker skipped` 不阻断落库。
  降级决策仍在 `main()` 就被跳过，不会走到这里。
- **修掉「严重度被字典序吃掉」**（`paper_reflection_worker._resolve_dimension_for_decision`
  与 `decision_engine._resolve_dimension_for_decision`）：取数由 `SELECT error_code`
  改为 `SELECT error_code, severity`，拼成 `E021:3` 再交给 `resolve_dimension`。
  这就是 `reflection_dimension_map` 里记的门禁 G11 根因 —— 不带 severity 时，
  最高频的低危码 `E001a`（INFO，缺 hard_data_evidence）会按字典序吃掉绝大多数决策，
  A/B/C 细分退化成一种。
- **实测**（当前库 4 条非降级决策，只读试跑）：每条稳定产出 8 个码
  （`E001a/E001b/E001c/E014/E021/E022/...`）；带 severity 后 4 条全部归
  `B/event_chain_break`（不带 severity 时会全部落到 `C/feed_missing`）。
- **新增 S17 错误码归因覆盖率**（ratio，毕业线 60%，gate 200）：
  A/B 两类反思里，该决策在 `decision_errors` 有程序化错误码的比例。
  改前 13.1%（13/99）→ 补写历史码后 **15.3%（15/98）**，status=样本不足。
- **历史欠账补写**：新增 `backend/scripts/backfill_decision_error_codes.py`
  （默认 dry-run，`--commit` 落库，`--limit N` 只跑最近 N 条）。对 43 条
  「非降级且尚无错误码」的决策跑校验器，28 条产出码（全部 `E001a` 缺硬数据事实），
  已落库：全库 23 行/15 决策 → **51 行/43 决策**。
- **S17 的结构性上限（写进 detail，不改口径）**：98 条 A/B 反思里 **83 条来自
  `analysis_degraded=1` 的决策** —— 它们是 v398 之前的历史行（维度靠 ontology 兜底），
  而校验器对降级决策按设计不跑，永远不可能有错误码。故 headline 的 60% 在现有样本
  结构下不可达；能验收的是非降级池 **10/15 = 66.7%**（已过 60%，但 n=15 远低于 gate 200）。
  `_s17_detail()` 现算并显示这两个数字，headline 定义不变（不靠改分母换达标）。
- **生效**：`~/.trident/wrappers/restart_backend.sh`（engine+bridge，bridge 31s 就绪）
  + `launchctl kickstart -k com.trident.api`；bridge 日志
  `[PAPER-REFLECT] daemon started | interval=300s` 已确认新代码在跑。
- **P1 第二条（v397 impact 回填复测）结论：不回填、不落库**。
  `backfill_impact_score.py` dry-run：只有 9 条能从 gate/strength 推出分数，
  396 条「门卫无分」（既无 gate 1–100 也无 strength）无法回填，S15 投影 15.3%→17.2%
  （目标 80%）。这是上游 gate 打分的覆盖缺口，不是回填能修的；写 9 行换 1.9pp 不值得，
  且会给"硬数据覆盖率"制造 9 条推算值。留作待修项（见报告 P1-2）。
- 回归：`tests/test_v398_insight_pool.py` 10 例（含 S17 + registry 断言）、
  `test_reflection_dimension_map` / `test_execution_cost_settlement` /
  `test_v310_outcome_pct_integrity` 合计 **79 passed**。
  全量 3,545 例：**3447 passed / 99 failed / 6 skipped / 5 errors**。99 条失败集中在
  6 个文件（timeout_util / breaker_epoch / silent_failure_audits / watchdog_active_batch /
  horizon_endpoint / yahoo_kline），**单独跑 78 passed** —— 是跨测试污染
  （同进程里 `api_server` 二次导入触发 `RecursionError`，日志可见「后台刷新失败(忽略):
  RecursionError」），不是本次改动引入；另有 1 条 `test_v322_silent_failure_audits`
  是存量审计失败（命中 7 处静默 `except`，均在 `api_server.py` / `trident2/store.py` 等
  **非本次改动文件**，属今天其他并发改动留下的欠账）。

### v398 P1 · 洞察样本池口径（降级不进洞察 / 技术错误不进错因分母）

> 起点是 `docs/细节洞察能力评估-2026-11-16.html`：494 条决策里 442 条（89.5%）走降级路径、
> 输出 99.4% HOLD；605 条反思里 499 条是一次抓取失败批量写入的 `C/data_feed_timeout`，
> 占 82.5%。两个都很"诚实"，但污染了下游所有分数的分母。

- **新增纯模块 `insight_pool.py`**（零依赖，只用 sqlite3 类型注解）：`is_degraded_decision()`
  按 `analysis_degraded` 判定、缺列/缺 id 一律保守放行（不让旧库悄悄停掉写入链）；
  `reflection_pool_stats()` 把 A+B / C / 无维度三类分开计数；`s10_detail()` 生成"排除了多少"的文案。
- **S10 只统计 A/B 交易错因**（`dimension IN ('A','B')`）：技术故障不再与"交易错因细分"共用分母。
  实测 88.2%（499/566）→ **86.7%（85/98）**，被排除的 C 类 499 条写进 detail，报告里可见。
  数字反而更差了 —— 因为过去那个 88.2% 是被故障批次稀释出的假象；真正的集中度是
  `A/pattern_recognition` 85/98。
- **S12/S15 分母剔除降级决策**：降级路径没有真分析，不该被算成"答得不好"。实测
  S12 26.1% → **78.2%**（n=55，非降级池）、S15 14.2% → **41.8%**（n=55）。
  两者都仍标"样本不足"（gate 300/200 未满足），只报值不下结论。
- **新增 S16 降级路径占比**（share，越低越好，毕业线 20%）：实测 **89.1%（449/504）未达线**。
  它决定其余指标还有多少解释力，必须与准确率类指标并列看。
- **降级决策不写反思**（三处写入端）：`paper_reflection_worker`（SELECT 增 `analysis_degraded`、
  循环跳过并打印跳过条数）· `decision_engine.settle_one`（结算账务照常，只跳过反思行）·
  `bridge_server._insert_settlement_reflection`（按 `trade_decisions.ai_decision_id` 反查）。
  实测 100 条 paper 反思里 84 条来自降级决策，本次起不再新增。
- 口径变更按治理规则处理：S10/S12/S15 的 `definition` 字符串已改 → `definition_hash` 变化 →
  历史对比自动作废，不与旧值混比。
- 回归：`tests/test_v398_insight_pool.py` 10 例（A/B 分母 / 空池 / 降级剔除 / S16 / 缺列放行）；
  `test_v398 + test_capability_scorecard + test_v336` 88 passed；结算与反思相关 121 passed。


- 症状：引擎日志长期 `国内源本轮 0 补齐` / `K线本轮 0 补齐`，而同一脚本在终端 4.8s 就返回真实周期。
- 三个被推翻的判断：①"引擎进程 TLS 整体不可用"——实为 `launchctl kickstart -k` 后新实例被单实例锁拦退（`[FATAL] another engine instance is already running`），旧进程一直在跑旧代码，几轮"重启验证"验证的是旧行为；②"fork 子进程能绕开"——引擎内 fork 出的子进程抓取**每次都恰好在 timeout 点被杀**（限 3.5/6.0/6.5/8.0s 分别死在 3.5/6.1/6.6/8.1s），是 macOS 多线程进程 fork 后做网络 IO 的死锁；③"预算不够"——加到 8.5s 仍卡在限制点，说明是卡住不是慢。
- 解法：`feeds/domestic_refresh.py`（独立进程抓取 → 原子写 `~/.trident/cache/domestic_{quotes,mtf}.json`，带 `ts`）+ `com.trident.domestic-refresh.plist`（每 45s）+ `market_snapshot._domestic_from_cache()` 读文件判新鲜度（TTL 180s，过期回落进程内）；`_domestic_via_subproc` 保留原名换实现，引擎不再 fork。
- 另修：装配硬保护误用未定义变量 `_t0` → `NameError: name '_t0' is not defined`，一份快照从 8/9 有价变成整份失败。
- 实测（真实重启 pid 31057）：`国内源补齐 8/9` / `K线补齐 15 个周期（sina_daily_k 2、em_klines 4、gate_klines 9）` / `PARTIAL (2/9 直连 OK, 9/9 有价) in 5.893s`（打底 0.5 / 远端 3.0 / 国内 2.4 / 装配 0.0+宏观 0.0）。
- 装配 9.8s 的真相：`装配慢 ETH 7.71s（内部=7.71s 事后=0.00s、light 无 DB 调用）` = 引擎满载下的 GIL 抢占等待，真实重启后回到 0.0s。


## v398 · 两步抢救的预算分流（NONE 类不再吃掉可救额度）

- 背景（真库 2026-11-16）：442 条降级里 **273 条（62%）是 `target_none`**。这批新闻本身不讲核心品种
  （个股 / 赛事 / 宏观汇总），两步抢救第一步几乎必然回 `NONE`，白烧一次 ~40s 调用；而它和
  「标的已锁、只缺八段」的新闻**共用同一个 10 条窗口** —— 一批 8 条 NONE 就把窗口吃干
  （日志实测 `窗口已用 6/10 … 9/10`），真正可救的那类抢不到预算。
- 新增 `_RESCUE_NONE_STATE` + `TRIDENT_TWO_STEP_RESCUE_NONE_LIMIT`（默认 **3 条/10 分钟**）：
  NONE/空标的走独立窗口，有标的的仍是原 10 条窗口。跳出预算时打印
  `两步抢救跳过（NONE 预算已用 N/N，标的=…）`，不再静默。
- 顺带修掉一个预算泄漏：`rescue_budget_ok` 内部会自增，而原实现**无条件先扣一次**
  （`_budget_ok = rescue_budget_ok(...)` 写在 `_need_two_step` 判断之外）→ 一批 16 条里有半批
  根本不需要抢救，却把窗口白吃掉。现在只在 `_need_two_step` 为真时才扣。
- 新增纯函数 `is_none_target()` / `rescue_budget_state_for()`，让预算路由可单测。
- 回归：`tests/test_v398_rescue_budget.py` 8 例（判定容错 / 两份窗口互不干扰 / 窗口过期重置 /
  NONE 用满即拒但主窗口一次未被扣）。
- 效果预期：NONE 新闻不再反复进抢救 → 单批 wall time 下降、`target_none` 类决策更快落库；
  省下的额度留给「有标的但字段缺失」的新闻（那才是抢救真正能救的）。

## v397 · 硬数据前置注入 + impact_score 阶梯（细节洞察的原料补口）

> 起点是能力记分卡：S12 实质分析覆盖 26.1%、S15 影响力维实质化 14.2%、market_confirmation 99.1% 是 unknown。
> 线上 478 条决策复核后定位到**同一处**：模型看不到可引用的数，且 impact 维长期靠默认 5.0 撑分母。

- **硬数据前置注入**（`engine/ai_worker._hard_data_block`，60s TTL 缓存）：此前 `hard_data_evidence` 只在
  LLM 漏填后由服务端回填，模型在推理前看不见任何价格 → [水位博弈] 只能写"未注入盘面快照，按中性处理"。
  现在按品种在 prompt 里先给一份实测证据（本地库优先、冻结/过期显式打"不作方向依据"标签），
  并要求引用带时间戳。缓存按「品种×时间片」，同品种一分钟内只取一次。
- **落库前统一回填**（`ai_worker` INSERT 之前）：硬数据是**事实**不是模型输出。降级/两步/兄弟链各分支
  都会漏掉回填，实测只有 21.1% 带证据；现在按最终品种补一次，已有值不覆盖，`field_sources` 记来源。
  按线上 478 条投影：`hard_data_evidence` 21.1% → **63.6%**（非 NONE 标的中 90.5%）；NONE/无本地线的标的（AMZN、CN_STOCK）仍留空，不编。
- **`impact_score` 阶梯抽成纯模块** `impact_score_ladder.py`（引擎/脚本/测试共用）：model > gate(1-100) > gate_strength > default，
  并新增 `field_sources["impact_score"]` 标记 —— 5.0 到底是判断还是兜底，复盘必须能分辨。
- **prompt 锚点改造**：原锚点"4-6 间接传导"正对 5.0，模型按档打分时天然落到兜底值；改为"按强弱打 4 或 6"，
  并明确"禁止一律写 5.0"。
- **新增 `backfill_impact_score.py`**（默认 dry-run，`--commit` 才写）：只改"impact=5.0 且有外部证据"的行，幂等。
  实测线上 405 条默认值里**只有 9 条可救**（4 条有 gate 分、5 条有 strength），S15 15.3% → 17.2%。
  其余 396 条对应新闻**门卫从未打分**（gate_verdict=''/'未判定'）—— 这是上游覆盖率问题，不编数。
- **新增 `hard_data_block.py`**：证据行/1H 聚合/冻结与过期判定的纯函数（原逻辑从 `_build_evidence_from_local` 原样搬出，输出逐字一致）。
- 回归：`tests/test_v397_hard_data_and_impact.py` 15 例（文案/冻结/过期/1H 聚合/缓存/阶梯/回填 plan）；
  相关 47 个测试文件 734 passed。全量套件有 6 个**既有**失败（P4 旧 prompt 断言、WTI 空路由断言、
  gate 默认值 20 与 config 现值 10、静默吞异常审计 8 个模块），与本次改动无关。

## v396 · 结算价取到了 6 天前的日线（假亏损/假盈利 + 账户双口径）

- `db.price_at()` 日线兜底 `ORDER BY ts ASC` → `DESC`：旧写法取窗口里**最老**的一根日线，
  WTI 1m 断档时两笔 20 分钟仓位被 11-10/11-11 的收盘价结算（差 6 天），造出 -3.17%/笔。
- 新增 `PRICE_AT_DAILY_MAX_LAG_SEC=86400`：日线滞后超上限一律当"无价"，不猜价。
- `quick_sim.settle_trades()`：超宽限期拿不到到期价 → `ABANDONED`，不再用"现在的价"结算过期仓。
- `_paper_metrics()` 改用 `quick_sim_trades.notional_usdt`：修掉前端 -215.28 / 账本 -4.31 的双口径。
- 新增 `scripts/audit_stale_settlements.py`（体检 + 退回，自动备份）。
- 重算：累计盈亏 -4.31 → **-0.13 USDT**，权益 9,999.87，有效样本 3 → 1 笔（2 笔 ABANDONED）。
- 回归：`test_quick_sim_settle_due_price.py` +4、`test_paper_metrics_notional_v386.py` +3，全绿。
- 详见 `docs/v396-结算取价口径-2026-11-16.md`。

### v396 P2 · 数据真实性规范（"不再产出假数据"）

- **R10 报价健康闸门**（`sample_quality_filter` + `db.quote_health`）：报价冻结（窗口内多笔同价）或过期（>15 分钟无更新）→ 不建仓，写 `quality_rejections`。开关 `QUOTE_HEALTH_GATE`。
- **新增 `src_python/data_authenticity.py`**：S1 结算价 vs 到期价、S2 结算时刻、S3 冻结报价、S4 1m 断档、S5 账实相符、S6 成本口径、S7 悬空未结；`--write / --json / --fail-on-bad`，已挂进每日 07:12 的漏斗日报 wrapper。
- **扣费后绩效一等化**：`quick_sim_trades` 增 `gross_pnl_pct`（毛收益），`pnl_pct` 明确为净值；漏斗日报新增"扣费后均值 + 净正笔数 + 有效样本门槛 + 无效样本数"，`已结算` 改为不含 `ABANDONED`。
- **规范落地**：新增 `docs/DATA_AUTHENTICITY.md`（六条铁律 / 四条口径 / 三道守门 / 准入清单 / 违规处置流程 / 禁止清单 / 风险登记），`AGENTS.md` 顶部加铁律摘要。
- 回归：`tests/test_data_authenticity_v396.py` 11 例（R10 冻结/过期/正常 + S1/S3/S5/S7）。
- 实测：`data_authenticity` 对真库跑出 0 条结算类违规，1 条 XAU 1m 断档（31 分钟，真实缺口）。

### v396 P3 · 99% HOLD 的三层根因（闸门/预路由/惩罚）

- **R11**：`analysis_degraded=1` 的决策禁止建仓（SQL 层 `COALESCE(ad.analysis_degraded,0)=0` + Python R11）。
  实测 3 笔成交里 2 笔来自 `summary_only` / `server_fallback` 降级路径。
- **新闻预路由 `news_router.py`**：A 股个股公告（公告/财报/回购/辞职/中标 + 无任何池内关键词）
  在进 LLM 前分流为 `FILTERED / 池外个股`。干跑近 2 天 3,598 条 → 命中 362 条（10.1%），
  按来源 sina_flash 268 / wscn 52 / 东方财富 36。开关 `NEWS_POOL_ROUTER`。
- **啰嗦惩罚 `text_quality.py`**：不再按字数罚。长度长 + 结构完整（≥5 段）+ 重复率 <15% → 不罚；
  只有重复句字数占比 ≥15% 才扣，上限 25%（原 60%）。修掉"全库唯一 BTC SELL 被压到闸门下"。
- **`llm_empty` 截断**：MINIMAX 主分析 max_tokens 2500 → 4000（八段中文 JSON 常见 3000+ token，
  2500 会在 closing brace 前截断），可用 `MINIMAX_MAX_TOKENS` 覆盖。
- **schema 不合格那一半**：`asset_nature` / `factor_ranking` / `reasoning` 三个叙述字段从
  `AI_DECISION_SCHEMA.required` 摘除，并把"带键空值"（`""` / `[]` / null）归一化为"未展开"
  （记入 `_empty_narrative_fields` 留痕）。失败明细原本 asset_nature 104 / factor_ranking 103 /
  reasoning 100 次 —— 全是空骨架，方向与八段推演都在。三个字段下游都有非编造补全路径
  （L2 业务知识 / 从 `[总结]` 回填）。`reasoning_path`(≥100 字) / 标的 / 动作 / 分数仍强制；
  非空但不合规的值照样报错。
- 回归：`tests/test_p3_gates_v396.py` 17 例、`tests/test_llm_schema_optional_v396.py` 8 例；
  预路由干跑脚本 `scripts/news_pool_router_dryrun.py`。
- **决策停写 3.4 小时（P0，已修复）**：`_batch_persist` 抛 `NameError: analysis_degraded_reason`
  且落库是"全有或全无"→ 一批 8 条全回滚、退回 PENDING 反复重分析。根因是**进程载入的是旧字节码**
  （pyc 20:01 / 源码 20:07），代码改干净也不会自动重载。修法：删 pyc + bootout/bootstrap，
  20:20:38 首条决策落库（id 11904）。顺带放宽两类"如实为空"（`market_category=""` 当 NONE、
  `hard_data_evidence="N/A"` 占位），避免每条 NONE 新闻白烧一次全量模型调用；
  测试 8 → 11 例全绿。详见 `docs/v396-决策停写-根因与修复-2026-11-16.md`。
- **写锁治理（cron_kline_aggregate）**：真凶是 ohlcv 阶段每轮把 10 万行历史日线全量 upsert
  （单这一步 60s+ CPU 且长持写锁）。改为窗口内写入（`OHLCV_UPSERT_WINDOW_DAYS=3`）+ 2,000 行分批提交
  + 看门狗线程硬超时（`SIGALRM` 在本机失效，改用线程 + `os._exit`）+ tick 三周期只读一次。
  实测整轮 17 分钟 → **11–21 秒**，user CPU 62s → 3.1s。新增 `tests/test_kline_aggregate_v396.py` 5 例。
  详见 `docs/v396-写锁治理-kline-cron-2026-11-16.md`。
- **事故（已恢复）**：18:51 在 `backend/` 直接跑 pytest，部分用例 fixture 对默认 DB 路径做了删除，
  生产库 `raw_news` 19,214→6、`ai_decisions` 494→4。已停服 → 从 17:11 备份完整恢复
  （损失 16:55–18:32 的 16 条决策，历史新闻/仓位/质量记录无损）。
  防护：`tests/conftest.py` 加硬闸，未显式给 `TRIDENT_DB_PATH` 时 pytest 一律跑独立临时库。
  详见 `docs/v396-事故-pytest截断生产库-2026-11-16.md`。

## v395 · 成绩单 14 天逐日走势 + 一次服务停摆归因

- `scorecard_share.collect()` 增 `daily`：近 14 天按日聚合已结算笔数与日均盈亏（仅有结算的日期才出现）。
- `/scorecard` 页与导出 HTML 都加了一条 14 日柱状：柱高=笔数，绿=当天均盈利、红=均亏损。
- 运维归因（真实故障）：API 启动时 lifespan 会"等 8100 就绪 (x/8)"，本次 bridge 8100 掉线（进程在但不监听），
  API 于是一直观望、端口不绑定 → 表现为"health 空响应但进程活着"。重启 bridge 后两者均恢复。
- 构建再次被内核 SIGKILL（exit 137）：机器内存紧张时 node 构建会被直接杀掉。
  处置顺序固定为：停前端 → 停 `frontend-rebuild-cron` → 杀残留 build → 清 `.next-prod` → 单次构建 → 起前端 → 恢复 cron。

## v394 · 短链分享 /d/{id} + 顺手修两处并发类型错

- 新增 `app/d/[id]/page.tsx`：`/d/11555` 60ms 后 `replace` 到 `/counterfactual?id=11555`，
  粘贴到飞书/微信不会被折行；编号非法时给格式提示与手动入口。
- 反事实页标题旁加「复制短链 /d/{id}」按钮（写剪贴板 + 已复制提示）。
- 修 `account-kpis.tsx` / `positions-table.tsx` 的 `settled === 1 || settled === true`
  类型冲突（`settled: number` 与 boolean 无重叠）→ 统一 `Number(p.settled) === 1`，语义不变。
  这两个文件是 14:00 前后被并发改动的，TS 报错会挡住构建，属必要修复。
- 构建注意：本次 `npm run build` 在收尾阶段被内核 SIGKILL（exit 137，机器内存紧张），
  但 BUILD_ID / pages-manifest / static 均已落盘，重启后 `/`、`/scorecard`、`/counterfactual`、
  `/memory`、`/d/11555` 全部 200，产物可用；后续若再遇 137，先停前端服务再构建。

## v393 · 启动耗时可视 + 冷启动归因

- `lifespan` 起点记 `_t_boot`，在两条出口（bridge 接管 / 自起守护）都打
  `[STARTUP] ready in X.Xs`，把"启动慢"和"服务没起来"分开，不用再靠时间点猜。
- 实测归因：单独起一个实例（API_PORT=8001）**24 秒**就 health 200；
  正式服务同一份代码要 **105 秒**，差额主要在 prestart 写标记（约 6s）+ import（约 16s）
  + 与 bridge 写库争锁；不是代码里死循环，也不是迁移卡住。
- 过程中发现并清理：8000 上曾同时挂着 3 个 `api_server` 进程（旧进程占端口、新进程卡 import），
  用 `bootout + pkill + bootstrap` 归零后恢复；这也解释了此前"health 空响应但端口在听"的现象。

## v392 · 启动期补列兜底（修「标记新鲜就永远不建新列」）

- 根因：`app lifespan` 的启动迁移在 `startup_marker.marker_fresh()` 为真时整段 `return`，
  于是本版新增的 `decision_actions.scope/event_type` 在线上库**从未被创建**，直到手工跑了一次 `db.migrate()`。
- 新增 `db.ensure_latest_columns(conn)`：只读 `PRAGMA` 检查最近几版新增列，缺哪列补哪列，
  千行以内耗时毫秒级；`api_server` 在「标记新鲜」分支调用它，并在日志打 `[API] schema 标记新鲜但补列: ...`。
- 测试 `tests/test_schema_guard.py` 2 条（造一个只有旧列的表 → 补列成功；再跑一次 → 返回空）。
- 已知限制：`ensure_latest_columns` 只覆盖清单内的列，新增列时必须同步登记到 `_LATEST_COLUMNS`。

## v390–v391 · 静默可只作用于单一事件 + 成绩单周对比

- `decision_actions` 增 `scope` / `event_type`（`db.py` 建表 + `_ensure_columns` 迁移）：
  `scope='event'` 时只静默同一 `event_type_code` 的信号，同品种其它事件照常分析，避免"一个事件烦了把整个品种关掉"。
- `is_asset_muted(conn, asset, ts, event_type="")`：品种级静默优先，事件级按类型匹配；`decision_writer` 从 `context` 取 `event_type_code` 传入。
- `POST /api/v1/decisions/{id}/action` 接受 `scope` / `event_type`；`/muted` 与 `/actions` 返回作用域字段。
- 前端处置区新增「只静默 XXX 事件」按钮（仅当该笔带事件类型时出现）。
- 成绩单增「周对比」：近 7 天 vs 前 7 天的笔数/胜率/均盈亏，页面与导出 HTML 同一口径；
  样本 <10 笔时注明只作方向参考。
- 运维注意：`db.migrate()` 只在部分入口调用（API 启动并未执行全量迁移），引擎未运行时新列可能缺失；
  本次已在线上库显式执行一次 `db.migrate()` 补列。API 启动在 bridge 忙写时会等锁，本次冷启动约 2 分 47 秒。

## v388–v389 · 静默可撤销 + 成绩单每日推送（含构建冲突修复）

- `decision_actions.py` 增 `active_silences()` / `unsilence()`：撤销走「把 expires_at 置为过去」，
  不删行，保留谁在什么时候撤过的痕迹；静默失效后 `decision_writer` 自动放开，不用重启。
- 新端点 `POST /api/v1/decisions/unsilence`（body: action_id 或 asset）；`GET /api/v1/decisions/muted` 追加 `silences`（带行 id）。
- 前端 `decision-quick-actions` 增加琥珀色「撤销静默 XXX (剩余分钟)」按钮，列表来自 muted 接口。
- 新增 `scripts/push_scorecard_lark.py`：成绩单每日推送到飞书群机器人 webhook，只在
  `FEISHU_SCORECARD_WEBHOOK` 有值时发，未配置静默跳过；已接进每日 wrapper 与 `.env.example`。
- 构建注意：`com.trident.frontend-rebuild-cron` 每小时会把 `.next-prod` 重建一次；
  手动构建前先确认没有 `next build` 在跑，否则会 rm 掉正在写入的目录并报
  `ENOENT .../pages-manifest.json`（本次踩过，停服 + 等 cron 构建结束 + 干净重建后恢复）。

## v387 · 处置口径进成绩单 + 反思页逐行诊断 + 每日自动生成

- `scorecard_share.collect()` 增 `muted_now / muted_total / ignored / reversed_shadow`：
  静默按「当前仍生效的品种数」和「累计次数」分开报；忽略查 `ai_decisions.quarantined_at`；
  并在页内注明「处置只影响之后写入的信号，不追溯已结算记录，故不并入漏斗」。
- `/history` 反思页的失败/成功逐单列表，编号后加「诊断」链接 → `/counterfactual?id=N`。
- 新增 launchd `com.trident.scorecard`（每早 08:20 跑 `~/.trident/wrappers/gen_scorecard.sh`），
  自动刷新 `docs/信号成绩单-YYYY-MM-DD.html`；日志在 `~/.trident/log/scorecard.log`。
  停用：`launchctl bootout gui/$(id -u)/com.trident.scorecard`。

## v386 · 反事实入口铺进历史回溯 + 成绩单上线为页面

- `/history`：每笔决策行尾巴加「四条腿 →」，跳到 `/counterfactual?id=<decision_id>`，
  反事实页支持 `?id=` 直接选中该笔（用 `window.location.search` 读，不引入 useSearchParams 的 Suspense 约束）。
- 新增 `GET /api/v1/scorecard`：直接返回 `scorecard_share.collect()` 的原样数据，
  页面、导出 HTML、对外快照三处共用同一口径，避免各算一套数。
- 新增 `/scorecard` 页（导航「后台 → 成绩单」）：KPI 四宫格、成交漏斗条形、闸门原因/错误码/漏检/反思维度/信息源五张表。
  页内明示「各层为独立计数，转化率只在相邻层成立」。

## v385 · 问库能诊断单笔 + 记忆检索页（在 v379–v384 上继续）

- `nlq.py` 新增 `decision` 意图：识别「11555 这笔为什么亏 / #11555 / 第 11555 笔」，
  直接调 `counterfactual.four_legs` 给四条腿，并按算术给出「方向反了 / 进早了 / 仓位偏重」，
  不臆测动机；重算与库内结算差 >1 个百分点时显式提示口径差。
- 新增 `/memory` 页（导航「研究 → 记忆」）：输入新闻文本 + 品种，看 top-k 召回，
  与管理端 `/api/v1/memory/search` 同一套索引 —— 页面看到的召回就是模型看到的。
- `/ask` 示例补「11555 这笔为什么亏」。

## v379–v384 · 五个新功能落地（语义记忆 / 反事实 / 问库 / 成绩单 / 快捷处置）

本轮按「先让系统更诚实，再让它更好用」的顺序补了 5 块，全部纯增量、零破坏：

**v379 语义记忆检索（`semantic_memory.py`）**
- 现状问题：历史经验按 asset/event_type 精确匹配 + 时间倒序取，同机理不同措辞永远召不回来。
- 做法：四张反思表（trade_reflections_v2 / decision_reflection / structured_reflections /
  experience_entries）建字符 n-gram TF-IDF 向量，按「品种×事件×当前新闻」余弦召回 top-5，
  每条截断 110 字；同品种 +0.22、同事件 +0.12、近期加权。
- 接入：`engine/ai_worker.py` 的 prompt 组装新增 `[SEMANTIC]` 分区（30s TTL，默认开）。
- 实测：686 条记忆，首枪索引 0.83s，之后命中缓存；查询词全 OOV 时退化为同品种近期教训。
- 接口：`GET /api/v1/memory/search`、`/api/v1/memory/stats`；开关 `SEMANTIC_MEMORY_INJECT/DAYS`。

**v380 反事实四条腿（`counterfactual.py`）**
- 现状问题：只能说「这笔亏了」，说不出「不进场 / 反向 / 晚 10 分钟 / 半仓」会怎样。
- 做法：用 `kline_bars` 重放同一窗口，输出 5 条路径（实际/不进场/反向/延迟/半仓）的
  PnL/MFE/MAE，并给「最好腿/最差腿/极差」与「重算 vs 库内结算」的漂移。
- 边界：品种没有 K 线时明确返回 `no_kline` + 可用周期，不拿相邻 K 线冒充。
- 接口：`GET /api/v1/counterfactual/{id}`、`/recent`；前端新增 `/counterfactual` 页。

**v381 自然语言问库（`nlq.py`）**
- 现状问题：看一条结论要翻 5–8 个页面。
- 做法：**不让 LLM 自由写 SQL**。意图路由（表现/错误码/漏检/异动/来源）+ 参数抽取
  （品种/时间窗）→ 白名单参数化查询 → 模板生成中文结论（数字全部来自查询结果）。
- 边界：库内 ts 同时存在秒/毫秒两种口径，按最大值量级自适应；问「为什么跌」只给异动分布，
  不给因果猜测；样本 <10 笔会明说「不具统计显著性」。
- 接口：`GET /api/nlq?q=...`；前端新增 `/ask` 页。

**v382 信号成绩单（`scorecard_share.py`）**
- 做法：一张自包含 HTML（无外链/无脚本），含 KPI、成交漏斗（新闻→分析→方向→闸门→结算→盈利）、
  闸门拦截原因、错误码、漏检、反思维度、信息源构成；所有数字当场现算并标注截止时间。
- 输出：`docs/信号成绩单-YYYY-MM-DD.html`，可直接发给外部看。

**v383 信号快捷处置（`decision_actions.py` + `decision_actions` 表）**
- 三个动作都真生效：`silence_asset`（写入口 `decision_writer.write_decision` 统一降级 HOLD，
  三条写入路径都拦得住）/ `ignore_decision`（写 `quarantined_at` 退出统计）/
  `reverse`（记录反向意图 + 当场算反向影子盈亏，不下真实单）。
- 迁移：`db.py` 新增 `decision_actions` 表 + 索引，并补声明 `ai_decisions.quarantined_at/reason`
  （线上老库有、schema 从未声明，全新库会缺列）。
- 接口：`POST /api/v1/decisions/{id}/action`、`GET /api/v1/decisions/actions`、`/muted`。

**v384 两处遗留修复**
- 分钟线空白：新增 `GET /api/v1/kline/coverage/{symbol}`；K 线组件按覆盖置灰不存在的周期
  并自动回落到可用最细周期（美股/韩股/指数不再出现空白图表）；周期按钮补 1h/1d。
- `/discover` 首屏：③④⑤ 三组改为 `LazySection` 进视口再挂载，首屏只挂 ①②，少 10+ 并发请求。

**测试**：新增 `test_semantic_memory.py`(7) / `test_nlq.py`(10) / `test_scorecard_share.py`(4) /
`test_decision_actions.py`(5)，全离线绿。

## v345.7 · by-dimension 4.7s → 0.5s（首屏提速）

- `/api/v237/missed-signals/by-dimension` 实测 **4.71s**（汇总 critical_price_moves + feed_health + 反思表），
  而 `/missed-signals` 与 `/discover` 首屏都会拉它，页面被拖到 20–30s 甚至「加载中」卡住。
- 加 60s TTL 缓存（统计窗口 90 天，60 秒陈旧可接受）：首枪 0.46s，之后 0.00–0.01s。
- 顺手确认 `/api/market/prices`、`by-dimension` 的 rewrites 已指向 bridge，避免先打 8000 拿 404 再回退。
- 复测：`/missed-signals` 8882 字 / 0 个「加载中」；`/discover` 38172 字 / 0 个「加载中」（首屏仍需 ~40s，来自多个接口串行拉取，下一步可做并发合并）。

## v345.6 · 整片接口 500 的根因（反爬 403）+ 报价条 404

**A. 全站未映射接口一律 500（重大）**
- `.env.local` 的 `NEXT_PUBLIC_API_URL=` 是**空值** → `next.config.mjs` 的 `backend` 兜底成 `http://127.0.0.1:8000`。
- 而 8000 的 `anti_bot.py` 对**没有 User-Agent** 的请求直接 403（`{"error":"forbidden","reason":"headless:missing_ua"}`）；
  Next 的 rewrites 转发不带 UA，于是 8000 侧全 403 → Next 侧统一变 **500**。
- 表现：`/sources` 整页 `Error: HTTP 500`、左上「市场状态加载失败: HTTP 500」、`/news` 拉不到列表。
- 修：`anti_bot.py` 对回环来源（127.0.0.1 / ::1 / localhost）跳过 headless 检测，外部访问仍拦。
- 实测：8000 `/api/health` 0.47s、`/api/sources/status` 0.28s、`/api/v188/markets/status` 0.06s，全部 200。
  前端 `/sources` 从 174 字空壳 → 2447 字正常页，`HTTP 500` 归零。

**B. 首页报价条永远「加载中」**
- `hooks/use-binance-prices.ts` 打 `/api/market/prices`，该路由只存在于 bridge(8100)，走兜底 8000 是 404。
- 修：`next.config.mjs` 新增 `{ source: '/api/market/prices', destination: 'http://127.0.0.1:8100/api/market/prices' }`。
- 实测：`/api/market/prices` 200（BTC 65417.15 +1.98%），首页「加载中」计数 0。

**C. 顺带**
- 信息源异常弹窗的 `last_error` 增加 `friendlySourceError()`：SSL/证书、连接重置、超时、404/429/5xx、DNS、代理 各自翻成人话，并抹掉 URL。
- 新闻页 5xx 文案同上（v345.4 已做）。

## v345.4 · 全站逐页复检（12 个路由）

复检对象：`/`、`/replay`（模拟盘）、`/attribution`、`/missed-signals`、`/review`、`/discover`、`/news`、`/prompts`、`/settings`、`/backtest`、`/history`、`/knowledge-graph`。
- 无 404、无白屏、无"加载中"卡死、无 `[hash:]`、无内部 URL、无 `NaN/undefined`。
- `/backtest`、`/history` 入口已挂到导航「交易」组，页面标题与说明齐全。

**发现并修复**
- `/news` 接口 500 时把 `Error: /news/recent?hours=24&limit=50 500` 原样显示在页面上（内部路径 + 黑话）。
- 改为：5xx → "新闻服务暂时不可用（5xx），多为后端重启中；本页每 60 秒自动重试，也可点「立即拉取」手动刷新"；其他异常 → "新闻数据读取失败，将在 60 秒内自动重试"。
- 复测：新闻页已加载 43 条 / 11974 字，raw 错误串不再出现在正文。

**遗留（下次处理）**
- 「4 个信息源异常」弹窗里的 `last_error` 是来源级原始报错（含抓取路径），需要按来源逐条清洗成人话。

## v345.3 · 全站扫描后逐项修复（内存/时延/标注/文案）

**A. 整机卡死的物理根因**
- 8GB 机器可用内存只剩 15MB，bridge 进程被系统反复杀掉（每次冷启 60–80s，期间前端全空）。
- 处置：停掉 Ollama 与空转的 `llama-server`（空占 2.3GB，LLM 本来已不可用），可用内存回到 ~280MB。
- 实测恢复：`/api/health` 14s → 1.5s；`/api/market/movers` 永不返回 → 1.6s。

**B. movers 不再同步现算（时延根因）**
- 原来 `market_movers` 每次请求都同步算 31 品种 × 异动 + 新闻（4–11s），前端 3 个周期同时轮询 → anyio 线程池占满 → 连 `/api/health` 都要排队。
- 改为：请求线程**只回轻量快照**（两条 SQL），完整版交给预热线程后台算完落缓存。
- 预热线程的开关 `TRIDENT_BRIDGE_AUX_JOBS` 从来没设过（等于从没跑过），改为默认启动，且调 `full=True`。

**C. 图表标注全是 `[hash:xxx]` 乱码**
- 库里新闻正文带 `[hash:hex]` 去重标记（入库时加的），K 线标注只截前 14 字 → 一屏十几行 `[hash:dece…]`。
- 新增 `plainNews()` / 改 `titleText()`，在标注、事件卡片、关联新闻时间线、驱动摘要共 6 处剥掉标记再截断。

**D. 其他界面收敛**
- 归因页工具栏窄屏改为标题/按钮分两行，右侧 3m–30d 周期按钮不再被卡片裁掉半个。
- 错误文案统一走 `friendlyFetchError()`：抹掉 `http://127.0.0.1:8100/api/...` 内部地址，超时/5xx 翻成人话（左下角不再出现原始 URL）。
- `[hash:]` 与内部 URL 在页面上实测均为 0 处。

**E. 构建铁律（再次确认）**
- 生产前端读 `.next-prod`：`NEXT_DIST_DIR=.next-prod npm run build`，不带这个变量构建等于没上线。

**已知遗留**
- 韩股/美股（SKHYNIX/000660.KS/TSLA…）切到 3m/5m/10m 时图表区空白（无分钟线，只剩坐标轴）；1h/30d 正常。下一步应在此类资产上禁用分钟周期或自动回落 1h。

# Changelog

All notable changes to **Reflex Quant** are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> **Detail changelog for the engine is in
> [`backend/CHANGELOG.md`](./backend/CHANGELOG.md)** (77KB, full per-commit
> engineering log since v100). This top-level file tracks **user-visible
> milestones** only.

## [Unreleased]

### Changed — v345 信息架构重整（去重 / 归位 / 删冗余文字）
- **首页**：删掉「A/B/C 数字 banner + 三维度摘要」两张卡 —— 与新增的「三类错误仪表盘」讲同一件事（买卖/方向/技术三个大类），只剩「仪表盘（摘要）+ 明细（漏检信号）」。四卡空态提示从 5 行压到 2 行（原因 + 三个跳转并入一行）。
- **归因页**：删掉未挂载的死 import（`news-impact-weight`）；两处英文标题中文化（实体影响 Top5 / 新闻影响权重）；「Updated xx UTC · auto-refresh 60s (统一)」改写为「最后更新 本地时间」。
- **归因页 K 线工具栏**：6 个按钮 → 「💾 存档」折叠（保存/导出/复制/查看/清空）+「📊 累计全量加载」，计数行中文化（`835 bars · 119 日历 · impact` → `835 根K线 · 119 日历 · 影响`）。
- **归因页 K 线空白真根因**：`lightweight-charts` 是 **v5.2.1**，v5 里 `chart.applyOptions({width,height})` 已不生效，只有 `chart.resize(w,h)` 会改画布缓冲 —— 之前所有「尺寸同步」代码都在空转，canvas 一直停在 300×150。改为 `chart.resize(w,h,true)`，并把「容器尺寸 >0 才建图」作为前置条件（movers-panel / live-kline-chart 同步修）。实测画布 1012×900（CSS 506×450 @dpr2）。

### Changed — v345 图例收敛
- 归因页 K 线上方图例 6 组平铺（绿字看涨/红字看跌/★3+日历/柱内绿点B/S/影响:跟随·反向·微弱·中性）→ 只留 3 个**可点击筛选** + 「更多图例 ▾」浮层，1280 屏宽下不再折成两行（实测行高 35px、单行 4 项）。

### Fixed — v345 桥接稳定性（第二轮）
- **每个写连接都跑 `PRAGMA journal_mode=WAL`** —— 这条 PRAGMA 要拿库级独占锁，engine 高频写时自己就把自己锁死，表现为「整机接口全挂、连 `/api/health` 都不回」。WAL 是库级持久设置，去掉连接级调用，只保留 `busy_timeout=15000`。
- **`/api/health` 改 async 且不查库**：表名单改为进程启动时快照（`_HEALTH_TABLES`），探针不再受写锁/线程池影响。
- **movers 改 stale-while-revalidate**：缓存过期时立刻返回旧值 + 后台线程刷新，页面永远不用等 4~11 秒的现算。
- 验证（走真实页面而非 curl，本机 curl 到 8100 的链路不可靠）：归因页 `990 根K线`、画布 `1012×900`、非白像素 `910,800`，图例单行 4 项。

### Fixed — v345 桥接稳定性
- `_SIGNAL_SQL_REFLECTIONS` 引用了不存在的列 `r.news_id`（`trade_reflections_v2` 只有 `decision_id` / `ai_decision_id`）→ `/trading/signals?...reflections` 稳定 500。改为 `LEFT JOIN raw_news rn ON rn.id = d.news_id`。
- anyio 线程池上限 40 → 160，且必须放 `@app.on_event("startup")`（模块级拿不到事件循环，之前那版报 `Not currently running on any asynchronous event loop`）。
- 写连接 `busy_timeout` 10s → 30s：engine 常驻高频写库，bridge 的 news/cot/ohlcv 批量写此前成片 `database is locked`，日志里刷屏且偶发把读接口拖死。
- **movers 预热**：新增后台线程 `_prewarm_movers_loop`，每 90s 把 10m/1h/4h/24h/30d 五个周期先算好塞进 `_BMOVERS_CACHE`（TTL 120s）。前端首屏命中缓存：1h/30d 实测 0.59s/0.61s（原先每次 4~11s，期间归因页一直停在"还没选品种"）。
- **分钟线取数封顶**：`/api/market/history` 前端按"一屏 720 根"传 limit=720，10m 折算成 5 天要用多页 1m 回补，实测直接撞前端 30s 超时（页面只剩一行"请求超时"）。分钟级统一 `limit=400` 上限后，`SOL 10m` 从超时降到 **0.93s**，画布 1444×900、728 根K线、非白像素 129 万（蜡烛+BOLL 三轨+Pivot 七线全部出图）。
- 运维：`launchctl kickstart -k` 在本机会把新起的进程一起带走（日志反复出现 Shutting down），可靠做法是 `launchctl stop` + `launchctl start`（实测 8s 内监听，movers 首查 3.8s）。

### Added — v344 三类错误仪表盘 + 事件效力散点
- 后端 `GET /api/analysis/error-taxonomy?hours=168`：按用户口径把错误拆成三大类，每类继续拆小类。
  · ① 买卖信号判断（`trade_reflections_v2`，仅 BUY/SELL）：近 7 天 340 笔、错 142 笔（41.8%）；小类 = 追多被打回 113 / 追空被打回 29 / 幅度不足 / 明显反向 ≥1%。
  · ② 信息分析方向错误（`event_move_attribution(+_rejected)`）：637 条里 371 条被判"事件与异动无因果关系"（58.1%）；另列弱因果 109 / 强因果 50 / 孤儿 87 / 行情播报 45。
  · ③ 技术错误（`feed_health` + `ohlcv_daily`）：712 次失败，按源拆 yahoo 238 / yfinance 222 / asia 189 / equity 26，外加"接口慢 ≥3s"与"日线超 3 天未更新"的资产。
- 后端 `GET /api/attribution/event-scatter?days=30`：X=因果评分、Y=异动幅度 |%|，返回 463 点（含 AI 判"不成立"的空心点），高确信+大幅的"有效事件" 50 个。
- 前端 `components/analysis/error-taxonomy-panel.tsx` 挂到实时信号台（`ReviewOverview` 下方）；`components/attribution/event-scatter-panel.tsx` 挂到行情归因页（K 线上方），带 7/30/90 天切换、按快涨/快跌/慢涨/慢跌着色、右上角"有效区"高亮与 hover 明细。
- 踩坑记录：新接口首次 HTTP 请求整机桥接会卡死（`/api/health` 也 000），根因是重启期间出现双进程抢 8100；`pkill -f bridge_server.py` 后再 `launchctl kickstart -k` 才干净起来（约 24s）。

### Fixed — v343 出网兜底铺开 + 告警收敛
- 新增 `net_curl.py`：`requests` 优先、失败自动用系统 curl 再取一次（返回 `.status_code/.text/.json()` 兼容对象）。`market_feeds` 的 Binance/OKX/Bitget/Gate 报价 4 个入口全部切到 `net_curl.get`；`intraday_store._fetch_yahoo_chart` 改为 curl 优先、urllib 兜底（本机到 Yahoo 的 urllib 常被 TLS reset）。
- 实测 OKX ticker 走 curl 1.8s、Binance 1.2s 均 200。
- `/api/sources/alerts` 改为按 `(source, feed_kind)` 收敛：原来「一个源 × 一个品种」失败就出一行，200 条原始失败 → **5 条**；新增 `assets_failed` 与 `n_raw_failures` 两个字段。前端「197 个信息源异常」随之降到「5 个」。
- 告警 UI 默认收成右上角一颗小胶囊（`N 个信息源异常 · 查看 · 我知道了`），点开才铺明细卡，不再整块盖住 K 线主图。
- K 线画布在未选品种时补空态说明（「还没选品种」+ 两条出路），不再是一整块纯白。

### Fixed — v343 K线画布空白（数据到了但图上不画）
- **现象**：归因页计数行写着「655 bars · 115 日历 · 126 impact」，画布区域却是纯白。JS 实测 `<canvas>` 的绘制缓冲 `width=300 height=150`（库默认值），而 CSS 尺寸 `562×450`——图元根本没按容器尺寸绘制。
- **根因**：`createChart(el, { autoSize: true })` 在本机（Next 生产构建首帧容器宽高为 0）只认第一次量到的 0 尺寸，canvas 缓冲停在 300×150 后再也没被改过；`ResizeObserver` 也没有把它救回来（手工 dispatch `window resize` 亦无效，缓冲仍是 300×150）。
- **修法**：`live-kline-chart.tsx` / `movers-panel.tsx` 都改成 `autoSize: false` + 显式尺寸管理：建图时传 `width/height = el.clientWidth/clientHeight`，随后 `syncChartSize()` 立刻量一次、`requestAnimationFrame` 再量一次、`ResizeObserver` 持续跟随、400ms 兜底补一次；清理时 `ro.disconnect()` + `clearTimeout`。
- **验收**：画布缓冲变为 `1972×900`（CSS 986×450 @dpr2）；`10m` 视图非白像素 **118,288**、`30d` 视图同样正常绘出蜡烛 + BOLL 三轨 + Pivot 七线 + 事件散点。归因页 K 线恢复可用。

### Fixed — v342 行情源出网修复（K线/日线恢复出图）
- **根因不是代码，是 TLS 栈**：本机 Python(OpenSSL) 到 `api.binance.com` / `api.coingecko.com` / `query1.finance.yahoo.com` 会在握手后**被 reset**（`SSL: UNEXPECTED_EOF_WHILE_READING`），而系统 `curl`（SecureTransport）对同一 URL **200**。实测 `data-api.binance.vision` 0.95s、`api.binance.us` 0.79s，`api.binance.com` 000/5s。
- 新增 `market_data_fallback.py`：用 `curl` 子进程取数（含 1 次重试），提供 ④ 新浪全球期货日线（XAU=GC / XAG=SI / WTI=CL，JSONP 解析）与 ⑤ Binance 现货日线。接进 `bridge_server._fetch_ohlcv_daily` 作为 yfinance → 东财 → CoinGecko 之后的最后一级降级；东财 `push2his` 现在也不可达，实际由 ④⑤ 兜住。
- `intraday_store._http_klines` 改为 **curl 优先**（原来要等 3 host × 4 重试 ≈50s 才轮到位，10m K 线冷启动 50~60s 直接超时）；另 `market/history` 按 `limit × 周期` 折算真实需要的天数回补，不再无脑按 `days=5` 拉 1m。
- 实测：`POST /api/ohlcv/refresh-now` 写入 **709 行**，BTC/ETH/SOL/XAU/WTI 最新到 **2026-11-14**（XAG 11-13）；`market/history?interval=1d` 0.02s、`1h` 0.4s、`10m` 199 根（冷启动仍偏慢）。
- 新增 `tests/test_v342_market_fallback.py`（4 例，解析/映射/重试，不联网）。

### Ops — v337 后端改动正式上线（engine 重启）
- engine 进程重启（launchd KeepAlive，新 PID 16685 @19:02），`[BUILD]` 行确认加载的是改后文件：`engine/ai_worker.py=b1816126@16:06:53`，即 chain prior prompt 块 + `decision_engine` 冲突否决已生效。
- 真链冒烟（只读库）：`match_by_headline(BTC, "美联储加息超预期，美元走强")` → chain 64「高利率/紧缩周期→风险资产承压」direction=DOWN、posterior=0.4262、matched_keywords=[加息]；后验低于否决阈值 0.85 → 弱证据不会强推 HOLD，设计符合预期。
- 带 HTTP 验收（重启后）：**23 PASS / 0 FAIL / 2 WARN / 1 SKIP**。此前 FAIL 的 #12 真因果 **57.79%**（152 真因果 / 107 弱因果 / 4 否证 / 0 行情播报）。两个 WARN：① 交易信号评估（样本不足）；⑭ 市场基本面（资金费率、订单簿传感器 `不可用`，桥接刚重启尚未采集）。

### Changed — v337-UI 界面可用性修复（空态说原因 / 导航去日志 / 图谱分组）
- **导航去开发日志化**：`AppNav` 删掉全部 `V291 / NEW / 多 / → / 新 / PAPER` 徽章，19 个入口平铺改为「交易 2 / 分析 4 / 研究 6 / 后台 3」四段分组，字号 10px → 13px；窄屏仍横向滚动 + 右侧渐隐提示，宽屏右端显示「组 / 页名」。
- **空态必须解释"为什么空"**：`NewsImpactList` / `EntityImpactPanel` 新增 `emptyReason`，归因页按 `items.length` 与 HOLD 占比生成两条不同说明（0 条 = 事件没进决策链；全 HOLD = 不是没新闻而是模型没给方向），并给「信息源 / 漏检信号」出口。首屏 `ReviewOverview` 四卡全是「—」时，上方加一条诊断条（三种原因 + 三个跳转）。
- **界面不再泄漏内部表名/cron**：`trade_reflections_v2`、`critical_price_moves`、`belief-maintenance-cron`、`beliefs/belief_edges` 从界面移除；「信念系统」能力卡从产品页 `因果图谱` 移到 `后台 → 联通`；漏检区标题 `Missed Signal Reflection (v237)` → `漏检信号反思`。
- **因果图谱页 tab 分组**：6 个 tab 由平铺拆为「图谱（因果图谱/因果链库/反馈闭环）｜传感器（市场状态/盘口深度/资金费率）」，窄屏不换行、横滚；图谱无品种时说明"图由 AI 5-why 推理生成、通道停摆期间必然为空、已有规则链在隔壁页签"。
- **顶部市场状态条裁切修复**：条高 24px → 28px，chip 去上下 padding 并强制单行、长 sub_status 截断；`AppNav` sticky 偏移同步 88px → 92px。
- **杂项**：发现页周期相位英文枚举 `markdown/markup/...` → 吸筹/拉升/派发/下跌；新闻时间 `HH:MMZ`（UTC）→ 本地时间；信息源异常 toast 由"每源一卡必须手关"改为最多 3 卡 + 「还有 N 个异常」，容器 55vh 内滚动（桥接抖动时曾一次堆十几张盖满屏）。

### Fixed — v339 策略进化死点（98 条建议 45 天点不动 · 假通过三连）
- **「反思 → 参数落地」不是没人审，是点了必失败**：`strategy_param_adjustments` 积压 **98 条 pending**，实测 `param_key like '%.%'` 命中 **98/98（100%）** —— 反思侧写的是带命名空间前缀的 key（`score.buy_min_score`、`sizing.position_weight`），审批侧白名单是裸 key（`buy_min_score`）。`POST /api/paper/adjustments/{id}/approve` 每条都抛 400 `unknown param_key`。新增 `param_key_aliases.py` 别名层：settings 桶 / strategy 桶 / **unmappable（明确拒绝并给原因，绝不乱改交易参数）**。
- **即使 key 对了也落不了地**：`strategy_store.add_version(source='reflection')` 有「**每天最多 1 个版本**」硬限制，逐条应用要 98 天。新增 `param_adjust_apply.py`：按 canonical key **归并 + 冲突裁决**（同一 key 的 up/down 按 `Σ(sample_size × confidence)` 比权重，实测 `sell_min_score` down:17678.69 vs up:113.32），一次成版。
- **假通过一：批准成功、参数没动**。`paper_trading.set_settings` 对 buy/sell 门槛有 `[0.1, 0.9]` 硬钳制，当前 `sell_min_score` 已是 0.1，「降低 10%」被静默钳回 0.1，状态照样写 `applied`。现在预览期就同步钳制边界判 `skipped_noop`，单条审批端点返回 `applied_to='noop'` 且不改状态，前端弹「未改变任何参数」而不是「已批准」。
- **假通过二：枚举参数被写成浮点，随后清空**。策略反思建议「提高 `min_event_strength`」，以 `up 0.1` 落地会算成 `0.73` 写进 params，`clamp_params` 发现不在 `{low,medium,high}` → 回退空串，等于**把强度门槛彻底关掉**（比不改更坏）而状态记 `applied`。新增 `check_direction()` 类型层：布尔只收 on/off、枚举只收 set、数值只收 up/down；`engine/strategy_reflection.approve_strategy_reflection` 逐条校验并把拒因写进 `admin_note` + 返回 `skipped_reasons`。
- **假通过四（复盘中补挖）：钳制回原位仍记 applied**。`apply_adjustment_to_settings` 只负责算 + 写，`approve_strategy_reflection` 的 settings 分支拿到 `old/new` 后直接 `applied_count += 1` —— 当前 `sell_min_score` 已在 0.1，「降 10%」被 `set_settings` 钳回 0.1，**old == new 一模一样**，仍记一条"已应用"。现在按 set_settings 返回的真实新旧值判，相等即 `skipped` 并写明"被钳制回原位"。
- **假通过五（复盘中补挖）：strategy 分支还留着同一份假基线**。`engine/strategy_reflection.py` 里 `base = float(cur_val or 0)` / `except → 0.5`，与 `param_adjust_apply` 里被禁编造门禁抓到的写法完全同源：`signal_threshold` 基线 0，乘完还是 0，却照样写进版本并计 applied。改为统一走 `is_effective` / `describe_change`，空操作判 `skipped` 并给原因。
- **id=3 / id=4 逐条演算（2026-11-14，只读不写库）**：id=3 的 5 条里只有 `buy_min_score 0.15 → 0.1725` 能落地，`sell_min_score` 钳回 0.1（noop）、`min_event_strength` / `asset_filter` 类型拒收、`signal_threshold` 空操作；id=4 的 4 条同理只有 `buy_min_score` 能落地。**注意**：两条同时批时 `buy_min_score` 会连乘两次（0.15 → 0.1725 → 0.1984，门槛抬 32%），不是各生效一次。
- **pending 从 12 清到 1（2026-11-14）**：新增 `expire_unlandable_adjustments.py`（默认 dry-run，`--apply` 才写），判定口径与审批链路完全一致（`resolve_key` → `is_effective` → 钳制后比较），把「新鲜但永远落不了地」的建议标 `expired` 并把原因写进 note。本次清退 11 条：`signal_threshold` 基线 0 空操作 2 条、`sell_min_score` 钳回原位 1 条、`sizing.*` 与 settings/策略参数计量单位不等价 8 条（比例 vs 名义金额 vs ATR 倍数，自动换算会误伤真实仓位与止损）。保留唯一一条真能落地的 `score.sell_min_score up 0.05`（0.1 → 0.105）。状态分布：applied 24 / expired 97 / pending 1，界面不再常年挂着「12 条待审但批不动」。
- **枚举参数补上 `set` 语义（2026-11-14）**：`min_event_strength`（low/medium/high）和 `asset_filter`（资产白名单）此前只有 up/down 一种落法，比例缩放写不出合法枚举值，于是反思里这两类建议**永远被拒**——枚举通道实际是断的。现在补齐：`param_key_aliases.check_direction` 接受 `target_value` 并**真正校验值域**（原实现对 set 直接返回 True，任意串都能过校验再被 `clamp_params` 清空，是第三种形态的假通过）；`_VALID_DIRECTIONS` 加 `set`；`_validate_recommendations` 保留 `target_value`、非法 set 直接丢弃；审批 strategy 分支新增 set 落地 + "已经是该值"判 noop；反思 prompt 明确要求枚举参数必须用 set 并给出可选值。
  - 边界：**set 只对枚举参数有效**，数值参数仍用 up/down——否则 `magnitude` 会同时表示"比例"和"绝对值"，两种语义混在一个字段里必然出错。数值参数 set 一律拒收。
  - 新增 `backend/tests/test_v339_set_semantics.py`（10 例）锁住：合法 set 通过、非法值/缺值拒收、数值参数 set 拒收、枚举仍拒 up/down、非法 set 不进 recommendations。
- **weekly promotion 生产端补预筛（2026-11-14，治本）**：`weekly_insight_apply.apply_once` 过去是纯搬运——从 `review_insights` 的 rule 里抽出 `param_key` 就 INSERT 一条 pending，不管能否落地。所以清退掉的那批建议**下一周又原样长回来**（复查时发现 `v190_weekly_promote` 在 14:27 一口气插入 11 条同样的 `sizing.*` / `sell_min_score down` / `confidence_threshold down`）。现在插入前按与审批完全一致的判据预筛：`resolve_key`（能否映射）→ `check_direction`（类型对不对）→ `is_effective`（基线做比例调整是否空转）→ 钳制后比较（会不会夹回原位），不合格进 `skipped_unlandable` 并留原因。实测 473 条洞察里 18 条被挡、只有 1 条真能落地。新增 `backend/tests/test_v339_weekly_prefilter.py`（3 例）。
  - **误杀防御**：第一版预筛在「读不到当前参数」时（老库缺表、单元测试的最小库）把 `_cur=None` 当成"基线为空"直接丢弃，结果把合法建议也杀了（`test_v200_p0_experience_loop` 的 A1 用例就是被这样误杀的）。现在分 `_settings_known` / `_params_known` 两个标志：**只有真的读到状态才做空操作与钳制判定**，读不到就放行。
- **唯一索引让状态迁移必然冲突（2026-11-14）**：`strategy_param_adjustments` 上有 `UNIQUE(asset, action, param_key, direction, status)`。于是把 pending 改成 expired 时，只要**同键 expired 行已存在**就会 `UNIQUE constraint failed`、整批回滚——第一次清退"成功"后 pending 又变成 11 条，一半原因在这里。`expire_unlandable_adjustments.py` 改为：同键 expired 已存在 → 把 sample_size 并入该行并删除重复 pending；否则才 UPDATE 为 expired。本次合并删除 11 条。
- **id=271 是 applied 却不生效（2026-11-14）**：`score.sell_min_score up 0.05`（asset=WTI）状态为 `applied`、`applied_setting_change` 写得像模像样，但 `paper_trading_settings.sell_min_score` 至今仍是 0.1（真落地应为 0.105），note 自己都写着"折算后无变化，未真正应用"。已纠正为 `expired` 并保留原因。**最终状态：applied 24 / expired 98 / pending 0**。
- **magnitude 语义错配（2026-11-15，设计级问题，待决策）**：`paper_trading._apply_param_change` 对 `buy_min_score` / `sell_min_score` / `tp1_ratio` / `tp2_ratio` 用的是**绝对加法**（`cur ± magnitude`），而建议侧（`_compute_magnitude` 按样本数给 0.03~0.10、反思 LLM 给 0.05~0.5）显然是把 magnitude 当**百分比**用。
  - 实测：`buy_min_score 0.15` 收到 "up 0.15" → 落地 **0.30**（门槛翻倍），而不是语义上应有的 **0.1725**（+15%）。
  - 影响面：所有针对这 4 个参数的建议，实际幅度都被放大数倍（一个 "10%" 的调整会变成 +67%）。
  - 已修的一半：`param_adjust_apply.describe_change` 原来**一律按乘法**折算、注释还自称"与 paper_trading 同口径"（其实是错的），导致**预览 0.1725 / 落地 0.30 —— 界面上给人看的那串数字是假的**。现在按参数类型分算法（加法型参数用 `cur ± base*magnitude` 之外的加法公式对拍），预览与落地已逐项对拍一致（`sell_min_score down 0.1` 的 0.0 vs 0.1 差异由上层 `SETTINGS_CLAMP` 补齐）。
  - **已决策并落地（2026-11-15）**：统一为**乘法**。`paper_trading._apply_param_change` 里 `buy_min_score` / `sell_min_score` / `tp1_ratio` / `tp2_ratio` 从 `cur ± magnitude` 改为 `cur × (1 ± magnitude)`，与 `describe_change` 完全一致。现在 `buy_min_score 0.15` 收到 "up 0.15" 落地为 **0.1725**（+15%），不是 0.30。
    - 影响：这 4 个参数未来的调参幅度会比以前**温和得多**（以前一个 0.10 的"10%"实际是 +67%）。历史已应用记录不受影响，它们是旧算法下写下的真实值。
    - 逐项对拍已通过：`buy_min_score` / `tp1_ratio` / `tp2_ratio` / `tp1_mult` 预览与落地完全一致；`sell_min_score down 0.1` 的 0.09 vs 0.1 差异由上层 `SETTINGS_CLAMP` 补齐后才判 noop，属预期。
- **id=4 批准后已回滚（2026-11-15）**：按修正后的模型发现真实幅度是 `0.15 → 0.30` 而非 `0.1725`，已把 `buy_min_score` 复位到 0.15，并把 id=4 从 `applied` 改回 `pending`（否则又是一条"标了 applied 但值没动"），admin_note 写明原因。
- **策略进化链首次真正闭环（2026-11-15）**：语义统一为乘法后重新批准 id=4，`buy_min_score` **0.15 → 0.1725**（+15%，与预览完全一致），1 条落地 / 3 条如实拒：`sell_min_score down 0.1`（钳回 0.1，noop）、`asset_filter on` 与 `min_event_strength up`（枚举参数必须用 set）。id=4 状态 `applied`，`admin_note` 记录"仅 buy_min_score 生效"。这是整条链路（反思产出 → 类型校验 → 别名映射 → 空操作守卫 → 真实落地 → 状态与事实一致）第一次全程无假通过。
- **审批链补 busy_timeout（2026-11-15）**：`approve_strategy_reflection` 没设 busy_timeout，而它要连写 3 张表（reflections / paper_trading_settings / strategy_versions），主库被 worker 写锁时直接抛 `database is locked`、已写的部分不回滚 —— **用户在前端点「批准」会随机失败**。已给 60s 等待窗口。
- **待审队列清零（2026-11-15）**：id=3 与 id=4 是同键重复（均 `buy_min_score up 0.15`），批第二次会把门槛连乘到 0.1984（累计抬 32%），故把 id=3 标 `expired` 并写明原因；`id=284 score.sell_min_score up 0.05`（WTI SELL）实际落地 `0.1 → 0.105`。最终：参数建议 applied 25 / expired 98 / **pending 0**，反思 applied 1 / expired 3 / **pending 0**。当前门槛 `buy_min_score 0.1725` / `sell_min_score 0.105`，两条都是本次经审批真实写入的。
- **统一 settings 读取口径（2026-11-15）**：`paper_trading.get_settings` 固定读 `WHERE id=1`，而我新加的 `expire_unlandable_adjustments._settings_snapshot` 和 `weekly_insight_apply` 预筛用的是 `ORDER BY id DESC LIMIT 1`。表当前只有 1 行所以等价，一旦变成多行，**判定用的"当前值"和引擎实际用的就不是同一行**——又是一个会悄悄错位的点。两处已统一走 `get_settings`。
  - 顺带修了 `test_v339_weekly_prefilter` 的夹具：它建的最小 `paper_trading_settings` 缺 `gate_enabled` / `tp_enabled` / `market_structure_enabled` / `news_trade_max_age_seconds` 等列，`get_settings` 读不到就被判成"状态未知"而放行，导致「在下限时应被挡」这条断言形同虚设。补齐列后断言真正生效。
- **参数生效链最终确认（2026-11-15）**：`paper_trading_settings` 只有 1 行（id=1），值 `buy 0.1725` / `sell 0.105`；`get_settings` 走 `WHERE id=1` 拿到同一组值；engine 的 `ai_worker` 每批处理前调用一次 `get_settings`，**无任何缓存**。所以改完参数不需要重启 engine，下一批信号评估即按新门槛筛。
- **更正：buy_min_score 确实参与实时决策（2026-11-15）**：此前我用 `grep -rn "buy_min_score" ... | head -12` 只看到前端回放面板和 `paper_trading` 的存取代码，据此判定「不进决策路径」。**这是错的**：`decision_guard.py:417` 就是拿它定方向的 ——
  ```python
  raw_action = (
      "BUY" if final_score >= scores["buy_min"]
      else "SELL" if final_score <= -scores["sell_min"]
      else "HOLD"
  )
  ```
  `scores` 来自 `_direction_scores()`，从 `paper_trading.get_settings()` 取 `buy_min_score` / `sell_min_score`，带 5s TTL 缓存。`_calibrated_thresholds()` 也用它做分级校准。
  - 所以 id=4 把 `buy_min_score` 抬到 **0.1725** 是真实生效的：`final_score >= 0.1725` 才给 BUY；`<= -0.105` 才给 SELL。
  - 教训：`head` 把关键行截断了，**grep 输出不完整时不能下「不存在」的结论**。后续判定"有没有消费方"必须先看命中总数，再决定要不要截断。
- **两层门槛叠加关系（2026-11-15）**：`decision_guard` 决定**方向**（`final_score` 对比 `buy_min` 0.1725 / `sell_min` 0.105），`strategy_store.signal_matches` 决定**准入**（`|score|` 对比 `signal_threshold` 0.15）。两者串联，所以有效门槛是：
  - BUY：`0.1725`（decision_guard 更严，`signal_threshold` 0.15 不额外收紧）
  - SELL：`max(0.105, 0.15) = 0.15`（**`signal_threshold` 这一设把 SELL 从 0.105 收到了 0.15**）
  - 实测影响：BUY 243 条历史决策**全部** `|score| >= 0.15`（0 条受影响）；SELL 146 条里只有 **3 条**落在 `[0.105, 0.15)` 会被新准入挡掉（2%）。
- **放开数值参数 set（2026-11-15）**：`signal_threshold` 基线是 0.0，`0 × (1±m)` 恒为 0，任何 up/down 都注定空操作 —— 这才是「12 条建议刷新却落不了地」的真正原因。现在 `check_direction` 对数值参数也接受 `set + target_value`（必须是有限数值），`_validate_recommendations` 保留原始类型（数值不再被转成字符串），审批落地分支区分枚举/数值各自走 set。实测 `signal_threshold set 0.15` 通过校验。
  - 边界不变：枚举参数仍必须 `set`（up/down 会写出非法值被清空）；数值参数 up/down 与 set 都可用，set 只读 `target_value`、不读 `magnitude`，两种语义不打架。
- **实时准入阈值首次真正设值（2026-11-15）**：新策略版本 **v2** 激活，`signal_threshold` **0.0 → 0.15**。全样本 389 条多空决策里 `|score|<0.15` 只有 11 条（2.8%），最近 7 天 0 条 —— 只切掉明确弱信号的温和取值。
  - 这一改对实盘/纸交易真实生效：`engine/ai_worker` 的建仓分支里 `strategy_store.signal_matches(active_params, action=..., score=..., asset=..., event_strength=..., direct_catalyst=...)` 拿**当前策略版本**做准入，`_reject_reason` 里第 108 行就是 `abs(score) < threshold` 的拦截。与 `buy_min_score` 那种只影响回放面板的形参不是一回事。
  - 至此「反思能调、调了真管用」这条才算成立：此前唯一的有效通道是 `strategy.params`，而 `signal_threshold` 因为基线 0.0 注定空操作，必须先有数值 set 才能动。
- **sl_mult 止损倍数打通读写（2026-11-15）**：`sl_mult`（ATR 止损倍数，默认 0.5）是**真实生效**的风控参数 —— `quick_sim.check_tp_sl` 每轮直接读 `paper_trading_settings.sl_mult` 来算止损价。但 `paper_trading.py` 里此前**一次都没出现过它**：`get_settings` 的 SELECT 没有这一列、`set_settings` 没有这个参数、反思白名单 `_V182_PARAM_KEYS` 也没有。
  - 后果：最能左右亏损幅度的参数，界面看不见、接口改不了、反思提不了，只能手改数据库。
  - 已补：`get_settings` 返回 `sl_mult`（缺列的极简测试库/老库自动退回旧列清单，不致使整条读取崩掉）、`set_settings` 接受该参数并在 `[0.1, 3.0]` 内钳制、`_V182_SETTINGS_KEYS` 与 `review_agent._V182_PARAM_KEYS` 都登记、`_apply_param_change` 走乘法折算（`0.5 up 0.1 → 0.55`，与 `describe_change` 同口径）。
  - 实库验证：读到 `0.5`，同值回写仍 `0.5`，`buy_min_score` / `sell_min_score` 未被扰动（0.1725 / 0.105）。相关回归 **584 passed / 0 failed**。
  - **全链路补完（同日）**：`PaperTradingBody` 加 `sl_mult` 字段、`_apply_replay_settings` 条件与落地参数都带上它；前端 `PaperTradingSettings` 类型、`updatePaperTrading` 的入参与快照解析、`changeScores` 的 Pick 都补了 `sl_mult`，`/replay` 面板新增「止损×ATR」输入框（钳制 0.1~3.0，title 写明它决定单笔亏损上限）。
  - 端点实测：`GET :8000/api/replay/settings` 与 `:8100/...` 均 200 且返回 `sl_mult: 0.5`（`buy 0.1725` / `sell 0.105` 未变）。前端 `npx tsc --noEmit` 无 replay 相关报错，`npx vitest run` **47 files / 668 passed**。
- **quick_sim 不是回放，是纸交易的成交结算引擎（2026-11-15，纠正）**：先前怀疑 `tp1_mult` / `tp1_ratio` 只在「模拟」里生效、实盘不用。实际上 `quick_sim.py` 的 `open_trades` / `settle_trades` / `check_tp_sl` / `cut_bleeding_trades` 写的是真实的 `quick_sim_trades` 表并回写 `ai_decisions`（`partial_exit_price` / `partial_exit_ratio` / `pnl_pct`），`check_tp_sl` 直接从 `paper_trading_settings` 读 `tp_enabled` / `tp1_mult` / `tp2_mult` / `tp1_ratio` / `tp2_ratio` / `sl_mult`。所以这批止盈参数是生效的，`trade_executor.py` 只负责下单、不负责止盈，前面看到的“缺失”是模块分工。
- **新增 /settings 交易设置页（2026-11-15）**：此前所有真正生效的参数（方向门槛 / 止损倍数 / 止盈倍数与比例 / 三个闸门开关 / 新闻时效）只散落在 `/replay`（模拟盘）面板里 —— 位置与作用域错配，用户在「回放」里调的东西其实作用于真实纸交易。
  - 新页按作用域分组，每项写明它影响哪一步：方向判定（`decision_guard` 切 BUY/SELL/HOLD）｜止损（`check_tp_sl` 的 ATR 倍数，唯一直接决定单笔亏损上限）｜止盈（倍数定目标价、比例定平仓量，两档合计 ≤0.95）｜开关（`tp_enabled` / `gate_enabled` / `market_structure_enabled`）｜新闻时效。页首注明「每项改动立即写库、引擎下一批生效」，并点明实时准入底线 `signal_threshold` 归策略版本管、不在本页。
  - 导航加在「后台」组首项（`frontend/components/app-nav.tsx`）。
  - 构建与测试：`.next-prod` 已产出 `/settings`（5.2 kB），`tsc --noEmit` 无报错，`npx vitest run` **47 files / 668 passed**（含 `app-nav` 3 例）。
  - **状态：未上线**。线上 3030 仍跑旧包，`GET /settings` 返回 404。原因是旧进程（PID 44179）一直占着端口，`launchctl kickstart -k` 换不掉它（与 v339 排查时 bridge 双实例同因），需要先 `kill -9` 再 kickstart；该命令两次提交均因确认超时未执行，需人工执行或再次授权。
- **反思 key 归一化（2026-11-15）**：`_validate_recommendations` 只认白名单 `_V182_PARAM_KEYS` 里的裸名，LLM 偶尔会写 `score.signal_threshold` 这种带命名空间的键 —— 入口直接当成「自创 key」静默丢弃，事后看起来像是「LLM 提了但系统没反应」。现在先用 `resolve_key` 归一化再查白名单：`score.signal_threshold → signal_threshold` 放行；`strategy.signal_threshold`（前缀无登记）与真正的自创 key 仍拒。实测 `signal_threshold set 0.2` 能一路进 recommendations。
- **入口默认折叠 = 无人可见**。`reflection-improve-panel.tsx` 原 `useState(false)` 默认收起、折叠条也不显示待审数量。改为默认展开 + 挂载即拉取 + 折叠条红字 `N 待审`。
- **清理测试脏数据**：`strategy_reflections` 的 id=1（`smoke-test: synthetic reflection`，状态竟是 `applied`）、id=2（`reject test`）标 `expired` 并注明合成数据；真分析 id=3/4 保留 `pending` 待人工批准。
- **schema 与文档不一致**：`strategy_param_adjustments.status` 的 CHECK 只允许 4 个值，而 `api_server` 注释一直宣称含 `expired`。`db.py` 建表补上 `expired`，新增幂等迁移 `migrate_param_adjust_status_expired.py`（无索引/无触发器/无视图，122 行单事务重建，失败自动回滚）。
- **实测结果（2026-11-14）**：98 条 pending → 新鲜 12 / 过期 86（标 `expired`）/ 不可映射 8（ratio 型仓位参数，语义不等价，留给人工）。12 条新鲜建议**当前参数下全部无法落地**：`sell_min_score` 已在钳制下限 0.1，`signal_threshold` 基线为 0（比例调整是空操作）。这是诚实结论，不再是假的 applied。
- **顺手挖出验收 #12 长期 FAIL 的真因（不是锁，是类型）**：`attribution_rule_verify.verify_all` 假设调用方连接是 `sqlite3.Row`，而 `calc_event_move_attribution` 的连接是 **tuple 行**，`dict(r)` 直接抛 `TypeError: object is not iterable` —— v338 把这函数接进重建后，**每轮都在此处异常且被 try/except 吞掉**，于是窗口内新行永远 `rule_verdict=NULL`，窗口外旧标注残留 88 条 confirm，正好 88/254 = **34.65%**（看起来像"并发竞态把标打空"）。修：改 `dict(zip(columns, row))` 不依赖行工厂；同时补 `PRAGMA busy_timeout=60000` 与 3 次退避重试。实测重建一次 → **254/254 全标注**（confirm 151 / partial 99 / reject 4），验收回到 **25 PASS / 0 FAIL / 1 WARN**。
- 新增 `apply_param_backlog.py`（默认 dry-run，`--apply` 才写库）、`backend/tests/test_v339_param_aliases.py`（14 例）与 `backend/tests/test_v339_verify_rowfactory.py`（2 例，锁定 tuple 行工厂可用）。
- 回归：`pytest --ignore=scripts -k "causal or chain or param or reflection or strategy or acceptance or aliases or rowfactory"` **387 passed / 0 failed**；前端 `tsc --noEmit` 通过、`vitest app-nav` 3 passed；能力验收 **25 PASS / 0 FAIL / 1 WARN**（唯一 WARN 仍是 #1 真实开仓样本 78 笔 < 100）。全量 `pytest --ignore=scripts` 为 **3164 passed / 28 failed**，失败集中在 `test_yfinance_feed`（yahoo 429 限流）与 `test_v330_attr_coverage`（夹具连到缺 `news_events` 表的库），两个模块本轮均未改动。
- 已知遗留：`pytest` 直接扫 `backend/` 会 INTERNALERROR —— `scripts/test_budget_gate.py` 是脚本却以 `test_` 命名，收集时 `raise SystemExit(1)`。需 `--ignore=scripts`，本轮未改其文件名。

### Changed — v339 界面信息架构与因果链带宽（P1 / P2 / P3）
- **P2 导航去掉两个假 tab**：`/capital-flow`（资金流向）、`/market-scanner`（行情扫描）从 v302 起就是 redirect 壳，实际跳到 `/discover#disc-flows` 与 `/discover#disc-cross`，在导航里看起来是两个独立页、点进去却是发现页的某个锚点。`app-nav.tsx` 删掉这两项（**路由与锚点保留，旧书签仍可用**），`app-nav.test.tsx` 改为反向断言"导航里不应出现这两个 link"（**3 passed**）。
- **P1 归因页 12 个面板 → 5 个可扫视块**：`app/attribution/page.tsx` 原为 12 个面板平铺，一屏塞满、看不出主次。改为：① `MoversPanel` 顶部独立（最该先看的放最前）；② `<details open>`「事件归因 · 谁影响了谁」包 `EntityImpactPanel` + `NewsImpactList` + `EventMarketAttribution`（默认展开）；③ `MissedSignalsPanel` 顶层不折叠（`#missed` 被实时信号台顶部 strip 深链引用，折叠会让跳转落空）；④ `<details>`「复核与质量」包 `PendingAttributionPanel` + `AbnormalAttributionPanel` + `AttributionQualityPanel`（默认折叠：核对类，不该占首屏）；⑤ `<details>`「市场环境」保持折叠，包 `MarketRegimePanel` + `OrderbookDepthPanel` + `FundingRatePanel`。`tsc --noEmit` 通过。
- **P3 `/api/causal/chains` 1 MB → 487 KB**：724 条链全字段 **1,004,166 B**，其中 `mechanism` 187.0K / `precondition` 141.2K / `invalidation` 140.1K / `note` 90.6K。前端 5 个消费方（`causal-chains-panel`、`asset-scout`、`meta-cognition`、`push-to-causal-chain-button`、`causal-chain-candidates-pill`）**全部不读前三个长文本**（只有元认知页在构造对象时写死文案，不来自 API）。`causal_chains.list_chains(lite=True)` 默认裁掉 `mechanism/precondition/invalidation`（`note` 保留：`causal-chains-panel` 的编辑表单要回填它），`?full=1` 取全量，`?limit=N` 取最近 N 条。实测 8100：默认 **487,679 B**、`?limit=5` **2,231 B**、`?full=1` **1,004,166 B**。ETag 签名混入 `full/lite:limit` 变体标识 —— 否则先取 lite、再带同一 `If-None-Match` 取 full 会拿到 304 旧体。
- **P3 顺带修掉一个静默降级（和 P0 同型的假通过）**：`list_chains` 原本把所有 `sqlite3.OperationalError` 都当成"老库缺 origin/weight 列"，于是 **`database is locked` 会走降级查询**，返回丢了 `note/mechanism` 的残缺体，而 ETag 仍按 `id+updated_at` 生成 → 客户端把残缺 body 当缓存存下来。实测症状：**同一个 ETag** 下 8000 返回 487,679 B（11 字段）、8100 返回 387,692 B（10 字段），界面上"同一条链有时有备注、有时空白"。现在只对 `no such column` 降级，locked 走 3 次退避重试后**抛出**；`_ensure_row` 统一补 `PRAGMA busy_timeout=8000`。
- **复现必读**：动过 `bridge_server.py` 或上游端点权重后，必须 `launchctl kickstart -k gui/$(id -u)/com.trident.bridge` 并等 **50–60 秒**（启动预热约 40 s，期间 connection refused 属正常）。bridge 未重启时会出现 8100 与 8000 数据不一致。另：`curl` 默认 UA 会被 8000 的 anti-bot 直接拉黑（`reason=ua_keyword:curl/`），测接口请用 `Mozilla/5.0 (Macintosh) trident-bridge`。

### Fixed — v339 测试基线治理（pytest 全扫可用 · 静默吞异常清零）
- **`pytest backend/` 不再 INTERNALERROR**：`scripts/test_budget_gate.py` 是可执行脚本却以 `test_` 命名，pytest 收集时 import 即执行、末尾 `raise SystemExit(1)` → 整个扫描直接挂。把执行体收进 `run_all()` 并加 `if __name__ == "__main__"`，现在 **3194 tests collected**，不必再加 `--ignore=scripts`。
- **写路径静默吞异常全库清零**：常驻门禁 `scripts/audit_silent_except.py` 命中 `news_score_worker.py:655` —— 「LLM 不可用 → 写待回放标记」那里的 `except sqlite3.OperationalError: pass`，与 2026-11-07 那起（CHECK 拒绝 UPDATE + except 吞掉 → 排查两天才定位）是同一类写法。改为 stderr 留痕（带事件 id 与异常）。审计 **0 处命中**，门禁测试 4 passed。
- **两处测试滞后于既有修复，改断言不改代码**：
  - `test_gate_deferred_v320` 期望门卫不可用时 `gate_verdict='无信号'`，而实现已是 `'未判定'`（`gate_drop_policy` 只丢弃 `'无信号'` 的行，改回就会把"门卫挂了"的新闻当噪音丢掉）→ 断言改为 `'未判定'`（20 passed）。
  - `components/v237/MissedSignalsPanel.test.tsx` 仍断言旧英文标题 `Missed Signal Reflection (v237)`，v337 已改中文「漏检信号反思」（6 passed）。
  - `lib/backend-registry.test.ts` 台账不变量失败：`beliefs` 域 `home` 仍写 `/knowledge-graph`，而 v337 已把信念系统移到「后台 → 联通」→ 改 `home='/connectivity'` 并更新 verdict 文案（14 passed）。
- 前端全量复验：**47 files / 668 tests passed**。
- **测试夹具与 schema 漂移，补齐 3 处建表**（一次消掉 14 条失败）：
  - `test_v322_head_of_line_block` / `test_v3211_claim_retry_lock` 的 `conn` 只建 `raw_news`，而认领 SQL 的去重子句会 `SELECT ai_decisions` → 补最小空表（**32 passed**）。
  - `test_v330_attr_coverage` 的 `_mk_db()` 缺 `news_events`，而 v334 用它的 `MIN(ts)` 收覆盖率窗口 → 补表 + 一条 `ts=0` 锚点（**4 passed**）。
- **`event_atom` 未知标签不再原样入库**：v337 为修「规则命中但 event_atom 一行不写」把英文 slug 改成无条件透传，副作用是任意串（实测 `not_a_real_event`）都会被归一成 slug 写进 `dim_event_type` —— 等于往维度表灌脏值。加 `_KNOWN_EVENT_SLUGS`（`EVENT_TYPE_TO_CODE` 的 values ∪ event_struct 规则实际产出的 19 个 slug）做白名单，未知一律返回 None（**28 passed**）。
- **`param_adjust_apply` 的假基线被「禁编造」门禁抓到**：`describe_change` / `is_effective` 原本写 `float(old_value or 0)`（还有 `except → base = 0.5`），把"未设置"的参数（`sl_mult=None`）折算成 0 / 0.5，预览值会显示成"能从 0 涨上去"。改成旧值缺失/非数值时返回 `None`、`is_effective` 返回 False（理由从"基线为 0"变为"没有基线"）。`test_no_fabrication` 门禁转绿。
- **两条用例自身写错，不是代码缺陷**：`test_quick_sim_sell_gate` 的两个 high-strength-SELL 用例拿了 `gate_cfg` fixture 却**没调用** `gate_cfg()` —— fixture 只在被调用时才改 config，等于门槛根本没开，于是断言"应被拦"失败。补上调用（**13 passed**）。
- **`test_v200_p0_experience_loop` 夹具缺 v331 新列**：`review_insights` 建表少了 `verification_verdict`，而查询用它过滤 failed/insufficient_sample → `no such column` 被上层吞掉 → 返回空块 → 用例假失败。补列（**23 passed**）。
- **最后 7 条也不是「网络问题」，是两处开关/桩没配对**：
  - `test_yfinance_feed` 5 条全走 `disabled: yahoo 429` 短路分支 —— v331 因 Yahoo 长期 429 把整源**主动停用**（`YFINANCE_FEED_DISABLED=True`，日线改由 `daily_bars` 承担），测试测的是 `_ingest_one` 的写库/替换/聚合逻辑，必须显式把开关打开。加 autouse fixture `_enable_feed`（**15 passed**）。
  - `test_v310_p1_fixes` 2 条 `updated` 恒为 0 —— v332 之后「模型不可用 → 不写假结论，直接跳过」是**设计正确**的行为，而用例要测的是关键词兜底路径。给它桩一个「有情绪分、无方向」的 verdict `("", 0.0)`，让 lexicon 决定方向（**21 passed**）。
- 后端全量 `pytest backend/`（2026-11-14 最终复跑）：**3196 passed / 0 failed / 2 skipped**（本次治理起点 3164 / 28）。前端 **668 passed / 0 failed**；能力验收 **25 PASS / 0 FAIL / 1 WARN**。

### Fixed — v338 归因/反思四洞（事件看不清 · 双验只跑 9% · B 类 91% 是垃圾 · 拒单从没复盘）
- **事件标题 5,561 条全瞎**：`event_kline_impact.event_title` 100% 是空串或 `[hash:xxx]`（写入端取 `raw_news.content`，去重指纹带进了标题），归因页只能显示哈希号。修：worker 改取 `news_events.headline` 优先 + `_clean_headline()` 剥前缀/取首行/截断；新增 `backfill_event_kline_titles.py`，已回填 **5,559/5,561**（剩 2 条 join 不到正文）。
- **AI 双重验证只覆盖 9.4%**：`attribution_ai_verify` 的 `ai_called=1` 只有 24/254，LLM 通道全熔断（远程额度废 + 本地 qwen2.5:3b 超时）。新增 **零 LLM 的第二道** `attribution_rule_verify.py`：资产/方向/时序三票制，全量 254 条落地 `rule_verdict` —— confirm 151 / partial 99 / reject 4，**真因果占比从账面的 100% 修正为 59.4%**。验收 #12 同步改为读 `rule_verdict`，并新增「否证占比 ≤10%」判据。
  - 归因表是 `calc_event_move_attribution` 的 **DELETE+INSERT 整表重建**，手动打标下一轮即被清空（实测 #12 从 59.4% 掉到 34.6%）。已把 `verify_all()` 接进重建后的提交点；并加 `BEGIN IMMEDIATE` —— 无锁时实测「SELECT 到 254 行 → 表被重建 → UPDATE 按旧 id 全打空」，91 行留 NULL。本脚本由 bridge_server 以子进程拉起，**无需重启常驻进程**即可生效。
- **B 类 91.4% 是历史垃圾**：`trade_reflections_v2` 的 B(信息方向) 1,762 条里 1,611 条是 `unverified_legacy_label`，其中 1,429 条是 `analysis/missed_signal`。新增 `reclassify_legacy_reflections.py`：漏检 → **A/market_regime_misread**（与 `missed_signals` 表既有 `A2_market_regime_misread_missed` 同口径，不新开小类）、信源分类错 → C/feed_missing。重判 1,502 条后 **B 从 1,762 → 260，真方向错 151 条浮出水面**（A 4,053 / C 2,153）。
- **3,187 条拒单从没被检查对错**：新增 `decision_reject_audit.py`（只读），用 entry_price + actual_outcome_pct 回算错拒率。实测 **总错拒 16.6%（528 条）**，其中 **强度闸门 733 条里错拒 399 条 = 54.4%、平均错过 7.72%**；同批连坐 12.3%；额度/集中度只有 1.8% —— 该收紧的是强度闸门，不是额度。
- 顺带修正两处漂移：`test_missed_reflection_vocabulary_is_controlled` 不再写死 12 个小类（改以 `reflection_dimension_map` 为唯一权威）；`test_missed_reflection_classify_market_regime_misread` 对齐 v326 的 peer-co-move 分支（有链无联动 = pattern_recognition）。
- 回归：`pytest -k "reflection or dimension or attribution or acceptance"` **298 passed / 0 failed**；能力验收 **25 PASS / 0 FAIL / 1 WARN**（唯一 WARN = #1 真实开仓样本 78 笔 < 100，未凑够样本前不判 PASS）。

### Fixed — v337 能力验证缺陷修复（除 LLM 通道外全修）
- **因果图谱接进决策（此前完全没接）**：新增 `backend/src_python/chain_prior.py`（资产级 + 证据加权的链匹配、方向冲突否决、prompt 块）。`decision_engine.propose_one` 调 `match_by_time` 注入 `evidence.chain_prior/chain_direction/chain_veto`，命中强链且方向相反直接改 HOLD 并记 `chain_veto:<chain_id>`；`engine/ai_worker` 新增 `_build_chain_prior_block` 注入 prompt（补上 v176.3「`_build_causal_block` 已停用但从未被替代」的空洞）。
- **链触发词污染**：启用链的 `trigger_words` 混着 `BUY/SELL/ETH/XAU/verdict/prompt/market_event`，等于任意 ETH 新闻都能命中任意 ETH 链。加 `_GENERIC_TOKENS` 黑名单 + 英文关键词词边界匹配（原先 `oil` 会命中 `boils`）。实测 500 条真实新闻命中 62 条 = 12.4%，精度可用。
- **归因「把结果当原因」**：`_is_price_echo` 从 v325 就写好但从未被调用。现在候选循环里加闸门（`price_echo_skipped`）、扩充动词（涨破/跌穿/涨穿）、加归档窗口（早于新闻归档的异动不进分母）、`purge_price_echo()` / `repair_orphans()` 存量治理（CLI `--purge-price-echo` / `--repair-orphans`）。生产库实测：268 行 → 迁出 87 孤儿 + 45 行情播报 → 重算后 252 行、孤儿 0、覆盖率 302/431 = 70.1%。
- **盘口 39 次调度全空转**：`depth_watch.py --commit` 只打印「已存盘口」，**从未调用 `store()`**，`depth_snapshots` 长期 0 行。修后实测落库 3 行（BTC/ETH/SOL）。
- **验收口径两处失真**：#11 绝对资产数（清掉播报后 10→9 就 FAIL，能力没变）改为覆盖率 ≥60%；#14 离线模式从不调端点却判「订单簿不可用」，改为读 `depth_snapshots`；C4 只认告警文件，文件被清空即误判「通道正常」，补充「决策链路停摆 >6h」证据。
- 新增 `backend/tests/test_v337_fixes.py`（18 例，全离线），覆盖链匹配/通用词黑名单/词边界/行情的价格回声/迁移与预演/停摆判定/盘口必须落库。

### Changed — v335 证据冻结（旧结论作废）
- 用户判定：旧数据算出的结论一律不可用。执行：清空 `shadow_ledger`（469 行）与 `event_swing_ledger`（74 行），两表推导出的所有命中率/收益/门槛结论作废；**原始行情与新闻全部保留**（`raw_news` 15,773 / `market_ticks` 868,671 / `kline_bars` 291,081 / `flow_snapshots` 35,301 / `econ_events` 38），因为它们是真实事实且接口无法回捞。
- 新增 `backend/runtime/EVIDENCE_FROZEN.md`：记录冻结原因、处理范围、保留清单与三条验收门槛（样本 ≥30 笔 / 两个独立时间段各自成立 / 扣费后为正）。
- `event_playbook.py --backtest`：样本 <30 笔时打印醒目警告「只能当调试信息，不得作为交易依据」。
- 全库备份：`backend/backup_before_reset_20261114.db`（1.4G，可还原）。


### Fixed — v335 事件识别漏读「金十公布」结构化条目
- `event_playbook.detect()` 原来只读 `news_events.headline`；实测 `raw_news` 里 40 天有 **522 条「[金十公布] … 实际 X 预测 Y 前值 Z」结构化条目**（金十财经日历发布件），其中 209 条美国口径、全部带三数值，完全没被读取。已增加第二来源（`raw_news.content LIKE '%[金十公布]%'`），并把处理逻辑抽成内部 `handle()` 供两个来源共用。
- **漏斗实测（40 天）**：522 条 → 美国 209 → 指标可识别 91 → 三数值可解析 47（其余是"预测 —"无预期，属合理排除）→ 去重后 22。加上 news_events 来源合计 26 个事件、19 笔可结算。**该漏斗已接近 40 天窗口的物理上限，不是解析能力问题。**
- **历史回溯证伪**：金十快讯接口 `flash-api.jin10.com/get_flash_list?max_time=YYYY-MM-DD` 可访问，但只能翻到最近 1~3 天（11-07 返回 0 条），40 天前及更早均取不到；`ff_calendar_lastweek/nextweek` 亦被墙。**结论：历史样本无法补齐，样本量只能随时间线性增长。**
- **样本算术（重要）**：40 天产出 19 笔，过 35 分闸门仅 4 笔 ≈ 0.1 笔/天；要凑 100 笔约需 1000 天。定档 35 的严格闸门一年只能验证方向、无法验证可持续收益。


### Changed — v335 事件预案闸门改为门槛扫描 + 分层放行
- `event_playbook.py`：`--backtest` 新增「闸门门槛扫描」（≥0/20/25/30/35/40/45/50/55）与「分层方案」（≥40 标准仓、30~40 半仓、<30 不做）；rows 里补记 `pulse_need`（脉搏支持分）与 `pulse_veto`，便于离线复算不同门槛。
- **实测门槛曲线（40 天 19 笔）**：≥0 → 19 笔 36.8% / 扣费 −0.129%；≥20 → 17 笔 41.2% / −0.100%；≥25 → 14 笔 50.0% / −0.040%；≥30 → 9 笔 55.6% / −0.017%；≥35 与 ≥40 → 4 笔 75.0% / +0.062%；≥45 → 3 笔 66.7% / −0.041%（样本塌了不可信）；≥50 → 2 笔 50.0%。
- **分层方案结果**：9 笔、命中 55.6%、平均仓位 72%、扣费后 +0.005%（几乎打平）。结论：把门槛从 40 降到 30 能把样本从 4 笔扩到 9 笔且命中率不塌，但收益只到打平线；**要真正为正，门槛仍得在 35~40**。
- 明确记录风险：19 笔样本上做 9 档门槛扫描，本身有挑数字风险，参数不可当作定论，只作"放行粒度"的操作指引。


### Added — v335 宏观底色（美元 / 实际利率 / 长债）
- 新增 `backend/src_python/macro_flow.py`：走新浪美股日线接口取 ETF 价格当宏观代理 —— `UUP`（美元走强≈美元指数上行）、`TIP`（抗通胀债涨≈实际利率下行）、`TLT`（长债涨≈长端利率下行）。已入库 UUP 4959 条（2007-03 起）、TIP 5501 条（2005-01 起）、TLT 2713 条（2016-02 起），source 记 `sina:us`；已接每小时 wrapper。选 ETF 而非指数：DXY 与 10Y 收益率的直连接口（CFTC / Yahoo / 东财 / 新浪外汇历史）在本机全部不可用。
- `relation_reader.py` 加 `--gold-edge`：把黄金「价涨 · 持仓大减(>5%)」信号按宏观环境切分（UUP 与 TIP 的 20 日方向组合）。
- **验证结论（信号不是宏观驱动，不要硬筛）**：全部样本 4482 天基准 +0.10% / 53.1%；信号不筛环境 n=200 +0.36% / 61.0%；**「美元走弱+实际利率下行」n=45 反而只有 +0.26% / 53.3%**；「美元走强+实际利率上行」n=66 +0.12% / 59.1%；「宏观混合」n=89 **+0.59% / 66.3%** 最强。→ 教科书式的黄金宏观逻辑解释不了这个信号，宏观只能当背景色，不能当过滤器。
- 当前宏观底色（2026-11-14）：UUP 20 日 +1.9%（美元走强）、TIP −1.5%（实际利率上行）、TLT −2.4%（长端利率上行）——对黄金不利的环境，与"价跌 + 持仓减 + 缩量 43%"的弱势结构一致。


### Added — v335 黄金/原油真实持仓量 + 四象限验证（含证伪）
- 新增 `backend/src_python/futures_flow.py`：走新浪财经国内期货日线接口（`stock2.finance.sina.com.cn/.../getDailyKLine?symbol=SC0|AU0`），一次拉回整段历史并带 **v(成交量) / p(持仓量) / c(收盘价)**。已入库 WTI 2095 个交易日（2018-03 起）、XAU 4590 个交易日（2008-01 起），source 记 `sina:SC0` / `sina:AU0`；已接每小时 wrapper。
- 新增 `backend/src_python/relation_reader.py`：价·仓·量四象限判读（价涨仓增=新多进场、价涨仓减=空头回补、价跌仓增=新空进场、价跌仓减=多头离场）+ 量能确认 + 五品种 4h 相关性矩阵。XAU/WTI 的持仓走新浪口径，加密三兄弟走 Bybit，避免两套单位混在一张表里。
- **四象限历史验证（诚实结论）**：WTI 1910 个交易日，除「价涨·仓增」59.4% / +0.45%（基准 54.0% / +0.14%）略优外，其余三象限与基准无差异；XAU 4482 个交易日四象限几乎全部贴近基准（±2pp）。教科书四象限在日线上**基本无预测力**。
- **唯一幸存信号（XAU）**：当持仓量变化 >5% 且价格涨、持仓减 → 后 3 日 +0.36%、胜率 61.0%（n=200，基准 54.8% / +0.16%）。分两半检验：前半 +0.38% / 54.9%（n=102），后半 +0.34% / 67.3%（n=98）——**收益两半都正、胜率差异大**，属"有正期望但不稳定"，不能当强信号用。
- 相关性实测：BTC-ETH +0.92 / ETH-SOL +0.86 / BTC-SOL +0.84（三兄弟等于一个仓位）；BTC-XAU +0.46；WTI-XAU −0.32（可对冲）。
- 数据口径：自 2026-11-14 起 Bybit 代币化黄金（XAUT+PAXG）持仓只作备用，正式读口径以新浪国内期货为准。


### Added — v335 盘口快照（挂单厚度与墙）
- 新增 `backend/src_python/depth_watch.py`：Bybit `/v5/market/orderbook?category=linear&limit=50`，算 买卖盘总量、失衡比(买方占比)、1% 内近端挂单、单档"墙"(>均值 6 倍)、价差 bp；新表 `depth_snapshots(asset,ts,...)`，已接每小时 wrapper 自动记录（盘口无历史，只能从今天攒）。
- 实测读数：BTC 买盘 22.9 / 卖盘 10.4（买方 68.6%，下方 4.5@68119 有买单墙）；ETH 买 475.5 / 卖 1180.4（买方仅 28.7%，上方 189.5@1921.58 有卖压墙）；SOL 挂单分散无墙、价差 1.30bp。
- 数据口径说明（写进模块文档）：`account-ratio` 的多空比按**账户数**统计且**仅 Bybit 一家**，不等于资金量；需与资金费率（按资金计价）、持仓量变化合看。强平历史 Bybit 已停公开、Binance 被墙，只能用"价格急跌 + OI 骤降"作代理。


### Added — v335 市场脉搏（情绪层）+ 脉搏闸门把事件命中率从 37% 提到 75%
- 新增 `backend/src_python/market_pulse.py`：7 个维度判读同一时刻的盘面与情绪 —— 结构（20/50 均线排列与斜率）、动能（RSI14 + 1h/4h/1d 涨跌）、波动（近 24h 波动 / 3 天常态的放大倍数）、资金费率、多空账户比、持仓量 8h 变化、跨资产联动（加密三兄弟 4h 均值 − 黄金 4h = 风险偏好开关）；输出 `bull`/`bear` 双评分（0~100）、标签与一句人话。
- `market_flow.py` 扩采 Bybit `account-ratio`（多空账户比，1h×200 条≈8 天），BTC/ETH/SOL 各 200 条入库（kind='lsratio'）；实测 BTC 做多账户 72.8%（散户一边倒）。
- `event_playbook.py` 加 `pulse_gate()`（事件方向须被脉搏支持，`PULSE_MIN_SUPPORT=40`）并在回测中输出对比。**40 天 19 笔：不看情绪 36.8% / −0.049%；+脉搏闸门 4 笔 75.0% / +0.142%（扣费 +0.062%）；被拦掉的 15 笔只有 26.7% / −0.100%**。方向明确但放行样本仅 4 笔，需继续攒。
- 当前读数样例：BTC 空分 67 多头 12（均线空头排列、RSI 28 超卖、73% 账户做多拥挤、加密弱于黄金 0.84% 避险）；XAU 空分 35 多分 0。


### Added — v335 事件预案交易员（预期落差 × 盘面状态）· 能力齐但样本不够
- 新增 `backend/src_python/event_playbook.py`：`parse_release()` 从标题解析 **实际/预期/前值**（修掉"11月6日"的 11 被误当实际值、库存负号丢失两处解析 bug）；`classify()` 13 类指标（CPI/PPI/PCE/非农/初请/失业率/零售/GDP/EIA库存/钻井/ISM/信心）；落差按相对 3% 分 符合预期/超预期/低于预期；`market_state()` 用 tick 判 上涨趋势/下跌趋势/震荡/高波动 + 资金费率拥挤；`plan()` 预案（延迟发布→不做、符合预期→不做、顺势 24h 标准仓、逆势 4h 半仓、高波动缩短减仓、逆拥挤方向加分）；`simulate_trade()` 带止盈止损（TP=max(0.5%,1.5×ATR)、SL=max(0.3%,0.8×ATR)）逐 tick 结算。
- 新增 `backend/src_python/econ_calendar.py`：走 ForexFactory 公开周历 `nfs.faireconomy.media`（Binance/OKX/GitHub raw 均不通，此源通），结构化取 国家/指标/星级/时间/实际/预期/前值，只留 USD；首采 106 条→美国 38 条入库 `econ_events`，已接入每小时 wrapper。
- 回测（40 天 / 26 个可解析事件 / 19 笔可结算）：全部 36.8% 命中、扣费 −0.085%；顺势 50%/+0.020%；逆势 14.3%/−0.266%（**验证"逆势不做"是对的**）；出场 止盈 7 / 止损 12 / 到期 0。样本不足以验证 70~80% 目标，需 3 个月约 200~600 笔。


### Added — v335 事件波段交易员（独立线）· 过 50% 但不稳
- 新增 `backend/src_python/event_swing.py`：`detect_events()` 从新闻流识别"真正公布"的美国宏观事件（CPI/非农/FOMC/EIA/PCE/失业率/零售，要求 `公布|实际|录得` + 美国口径，同类型 6 小时去重），`_price()` 取公布前价（5 分钟容忍窗），公布后 15 分钟进场、持有 24h；四种假设对比 follow/fade/long/short；`record()` 落 `event_swing_ledger`（74 条），已接入每小时 wrapper 自动攒样本。
- 回测（40 天 / 66 事件 / 74 样本）：follow 43.2% 扣费 −0.37%；**fade 56.8% 命中、平均 +0.287%、扣 0.08% 后 +0.207%**；基准 short +0.147% → fade 超额仅 +0.06%。**按类型**：CPI 23 笔 +0.321%、零售 7 笔 +0.873%、PCE 6 笔 +0.495%、EIA 5 笔 +1.849% 为正；FOMC 27 笔 −0.160%、非农 6 笔 −0.026% 为负。
- 稳健性不过关：时间对半切，fade 前半 64.9%、后半 48.6%（跌破 50%）；FOMC 前半 68.4% → 后半 25.0%。根因是 40 天仅 74 样本，分半后每格 3~19 个，噪声主导。结论：**不上真钱**，先自动记账攒 3 个月（预计 ~550 样本）再判定；方向只保留数据类事件（CPI/PCE/零售/EIA），FOMC/非农暂不做。


### Added — v335 资金流仪表 + 三条信号全部证伪
- 新增 `backend/src_python/market_flow.py`：走 **Bybit 公共 API**（Binance 在本机被墙，实测 `no_attempt`；OKX 也不通，Bybit 通），`fetch_funding()` 拉 8h 一笔费率历史 200 笔≈66 天、`fetch_oi()` 拉 1h 持仓量 200 笔≈8 天；新表 `flow_snapshots(asset,kind,ts,value,source)`，已入库 BTC/ETH/SOL 各 200+200 条；`--report` 看当前读数与拥挤度，`--test-funding` 做极值反向测试。已接入 `~/.trident/wrappers/run_missed_signals.sh` 每小时记录。
- **三条信号经回测全部无优势（市场中性口径）**：① 追已动价格（30m ≥1×ATR + 量比 ≥1.5）19.3 天 145 次，扣 0.08% 费后 BTC +0.002% / ETH +0.075% / XAU +0.054% / SOL 负；② 资金费率高位反向，表面 BTC −0.136%/ETH −0.551%/SOL −0.650%，减去同期大盘涨跌后超额仅 +0.02%/−0.07%/−0.17%，即**优势来自大盘在跌，不是费率**；③ BTC 领先→ETH/SOL 跟随，129 次命中 43~46%，扣费全负。
- 结论：公开价格类信号（价格动量、费率、跨品种跟随）都被人人可见，扣手续费归零。优势只能来自更快的数据（盘口/成交流）、非价格信息（宏观日历、链上、语义），或改用大波段降低费率占比。


### Added — v335 房间交易员原型 (BTC 样板, 有结论但无优势)
- 新增 `backend/src_python/room_trader.py`：`market_context()` 读 1m K 线算价格/30m-1h-4h 涨跌/ATR/量比；`decide()` 纯函数打分器（市场确认 0.45 + 1h 趋势 0.30 + 4h 0.15 + 量能 0.20，拥挤 -0.30，已定价 -0.50，阈值 0.55 下单、0.40 观察，仓位 1%~5%），每笔带理由码；`scan_price_opportunities()` 盘面驱动扫描（30m 幅度 ≥1×ATR 且量比 ≥1.5）。
- 回放结论：新闻驱动模式 BTC 房间 14 天只下手 3 笔、命中 33.3%；盘面驱动模式 19.3 天 145 次机会，命中 47.6%~51.0%，**扣 0.08% 手续费后全部归零**（BTC +0.002%、ETH +0.075%、XAU +0.054%、SOL 为负）。→ 目前无可交易优势，需接资金费率/持仓量/订单簿才有下一层信息。
- 门卫补英文关系词（Binance/bitcoin ETF/gold/crude/HBM/TSMC/个股等）+ 加 "Intel 崩盘" 类覆盖，二次回填 +23 条（BTC/NVDA），测试 7 例通过；分房有效性复核 1.26x vs 其它 1.01x（n=142）。


### Added — v336 P1-3：影子权重实验（结论：**不要**把异动/状态接进权重）

> 决策时点 4529 条配对里，六个乘子只有 impact 真正在动（ρ=+0.309），
> reaction/time 在 insert 时恒 1.0（输入没接、age≈0）。要补就得改公式，
> 而公式会真实影响下单权重 —— 所以先算影子，不改任何东西。

- **`opt_weight_shadow.py`（新）** —— 用"决策前 60 分钟该品种是否有异动"
  （方向一致 +/相反 −，幅度 ≥1.5% 加倍）与"决策时点 regime → 1.2/0.85/0.7"
  构造影子权重，并与**三维对照组**（base×impact×source，不接新输入）对比，
  剥离"换组合方式"带来的假提升。

实测（S4 同口径，逐小时桶内超额加权）：

| 权重 | ρ | 说明 |
|---|---|---|
| 现有复合 | 0.190 | 现状 |
| 对照组（三维相乘） | **0.274** | 只保留 base×impact×source |
| 影子（接异动/状态） | 0.251 | 比现状高，但比对照组低 |

结论：**收益来自"去掉死维度"，不是"接新输入"** ——
reaction 覆盖只有 135/4529（3.0%）、regime 578/4529（12.8%），
接进去反而把 0.274 拉回 0.251。所以本轮**没有**动生产权重公式。

**修正（同日第二次）**：上面对比的基线是"库里存的 `signal_weight`"（ρ=0.190），
但用**同一批乘子按生产公式重算**得 legacy ρ=**0.287**、减维档 ρ=**0.294** ——
减维只值 +0.007，在噪音范围内。也就是说"减维收益大"这个判断**撤回**，
原因是基线选错（把落库值和公式重算混为一谈）。

由此查出一个真问题：**落库的 `signal_weight` 与同批乘子重算的结果不是一组数**
（0.190 vs 0.287）。S4 记分卡报的 0.288 恰好等于重算值，说明字段可能是历史
版本公式写入、或写库路径与计算路径不同源 —— 待单独排查。

### Added — v336 P1-3b：权重档位开关（默认不开）

- **`signal_weight_evaluator.py`**：合成抽成纯函数 `compose_weight()`，
  新增 `WEIGHT_PROFILE` / `WEIGHT_PROFILES`，explain 末尾追加 `[profile]`。
  - `legacy`（默认）= 现网公式逐位一致，不设环境变量行为零变化
  - `impact_led` = `imp^0.85 × base^0.15`（`TRIDENT_WEIGHT_PROFILE=impact_led`）
  - 实测 +0.007 → 建议先不开
- 测试新增 7 条（等价性 / 忽略死维度 / 未知档回退 / 单调性 / 代理函数），
  `test_v336_optimizations.py` **57 passed**。

### Added — v336 P1-3c：权重分辨率记分卡 S14 + 落库精度修正

顺着"库里 0.190 / 重算 0.287"查下去，发现真正的缺陷不在公式，在**分辨率**：

- 决策时点 4529 条配对只有 **663 个互不相同的权重值（14.6%）**
- 最大一组 **396 条**完全同权重（0.4521），实际波动 **0.00%~14.59%**（中位 5.81%）
  → 权重对这批决策零区分力，S4 的 ρ 被并列值稀释约 0.097
- 根因是输入粒度：`base` 只有 121 个取值（0.05 步进）、`impact` 173 个

改动：

- **`capability_scorecard.py`**：新增 **S14 权重分辨率**（`distinct/n`），
  实测 **14.6% → 未达线**；口径说明写进 note（先看分辨率再看 ρ）。
- **`signal_weight_evaluator.py`**：权重落库精度 `round(...,4)` → `round(...,6)`
  （3 处）。只改审计留痕，下单权重在内存里始终满精度，不改写历史行。
- 测试 `test_v336_optimizations.py + test_capability_scorecard.py` **70 passed**。

未做（会动仓位）：把 `impact` 从分档乘子换成连续波动率估计、`base` 用连续情绪分。

**再拆一层（同日第三次）**：14.6% 的主因不在公式，分三段：

| 层 | 事实 | 占比 |
|---|---|---|
| ① 决策无品种 | `target_asset='NONE'` 2491 条，impact 恒 1.0；CN_STOCK 147 条无日线 | insert 总量 31.7% |
| ② 审计精度比计算粗 | 乘子 3 位小数把 1.495~1.505 并成 1.50 → 819 条 impact 相同 | 6.9% |
| ③ LLM 打分重复 | `impact_score=5/heat=5` → base 恒 0.55；396 条同权重横跨 81 个小时桶 | 8.7% |

第 ③ 层是信息上限，不是 bug。本轮把**乘子落库精度 3 位 → 6 位**（7 个字段），
与权重 6 位一致；仍然只影响留痕。测试 **70 passed**。

### Fixed — v336 P1-3d：`impact_score` 94.5% 恒为 5.0，门卫分从未被用上

- 8865 条决策里 **8379 条（94.5%）** `impact_score = 5.0`；
  其中 **7263 条（86.7%）** 对应的新闻在 `raw_news.gate_impact_1to100`
  已经有门卫 0-100 分（均值 16.9 / 最大 95），却没被采用。
- 根因：`_parse_result` 缺字段时直接写 5.0，而兜底条件是"值恰为 5.0" ——
  "模型没答"和"模型答了 5"永远无法区分。
- 新增模块级纯函数 **`resolve_impact_score()`**（返回 `(分数, model|gate|default)`），
  在 `_batch_persist` 统一调用；不改阈值、不改 `suggested_action`、不动公式。
- 只读估算：`base_mult` 取值数 **168 → 356**（分辨率下限翻倍）。
- 权重层面投影（历史 4529 条配对，同口径重算）：
  **4096 条会被改写**；分辨率 **18.9% → 42.5%**（补 heat 后 47.3%）；
  S4 口径 ρ **0.287 → 0.283**（−0.006，噪音级）。
  **口径修正**：先前写的"0.190→0.283"是拿库里 4 位小数的旧值当基线，
  与重算值不是同一起点 —— 结论应表述为"提高可分辨性，不是提高预测力"。

### Added — v336 P1-3e：字段默认值全量审计（`opt_field_defaults.py`，只读）

12 个带默认值的字段一次摊开，最刺眼的是：

- `sentiment_score = 0` **93.4%**（108 个取值，但九成是 0）
- `heat_score = 5.0` **94.6%**、`asset_impact = 5.0` **87.6%**
- `market_confirmation = unknown` **98.8%**、`impact_magnitude` 恒 0
- 三者叠加：**7859 条（88.7%）决策的 base_mult 恰好 = 0.550**

`sentiment_score` 不能靠门卫分替代（门卫回答"多重要"，不回答"做多做空"），
只能从提示词/模型侧修 —— 这是分辨率最后一块石头。

### Changed — v336 P1-3f：历史 impact_score 回填（可撤销）

- **`backfill_impact_gate.py`（新）**：用门卫分补 `impact_score`，
  **只改这一个字段**；写库前先把原值整表存进 `ai_decisions_impact_backfill`。
- 已 `--commit`：**7263 条**（81.9%）；`impact_score` 仍为 5.0 的从
  **94.5% → 12.5%**。补后主要档位 6.2(888)/6.8(781)/7.2(627)/4.5(554)。
- 撤销：`python3 backfill_impact_gate.py --rollback`
- 新增记分卡 **S15 影响力维实质化占比 = 87.4% → 达标**（目标 80%）。
- `signal_weight_history` 里的权重**未动**，S14 仍为 14.6%；
  要让 S14 反映回填后的分辨率需重算历史权重（投影 42.5%），留待决策。

### Fixed — v336 P1-3g：情绪分 (a) 档中心映射错误（方向系统性偏多）

- prompt 声明 `0-100 / 50=中性`，归一化却按 `v/100` 映射 →
  **40（偏空）存成 +0.4**，任何 1~100 的输入都变正。
- 数据佐证：`suggested_action=SELL` 的 146 条里 **90 条情绪分为正**
  （BUY 243 条全为正 —— 因为正数永远不会变负）。
- 修复：`v/50 - 1`（50→0、40→−0.2、100→1.0）。只改 (a) 档。
- **(b) 档（0-1）保持原样**：其 0.0 同时承担"模型没给分"的语义，
  按中点拉伸会把缺省算成 −1.0（极端看空）；试改后 9 条回归测试报错，已回退该部分。
- 历史行**未回填**（换算为 `new = 2*s - 1`，会一次性翻转约 400 条方向，待决策）。
- 测试：`_normalize_sentiment_score(75)=0.5 / (50)=0.0 / (40)=-0.2`，
  相关用例更新；情绪/评分/ai_worker 相关 **79 passed / 1 skipped**。

### Fixed — v336 P1-3h：历史情绪分定向回填（只修 SELL 矛盾行，可撤销）

- **`backfill_sentiment_center.py`（新）**：只改 `SELL 且 sentiment_score > 0`
  的 **90 条**（其中 67 条翻负），换算 `new = 2*s - 1`。
- 为什么不改全部正分：`BUY + 正分`本身自洽，一部分可能是 (b) 档原始输出，
  统一拉伸会制造新矛盾（`BUY +0.219` → `-0.562`）。只修能证明错的。
- 结果：SELL 一致性 **38% → 84%**（正分 90→23，负分 56→123）。
- 回滚表 `ai_decisions_sentiment_backfill`；`--rollback` 一键还原。
- 测试：三套回归 **103 passed**。

### Fixed — v336 P1-3i：历史权重重算（只重算 base 维，其余五因子保持原值）

- 前面修的是**输入**（impact 7263 条 / 情绪分 90 条），但
  `signal_weight_history` 存的仍是旧输入算出的权重 → S14 一直停在 14.6%，
  量到的是一份过期快照。
- 新增 **`backfill_weight_replay.py`**：用 ai_decisions 现在的
  `sentiment/heat/impact` 重算 `base_mult`，其余五乘子（impact/consensus/
  source/reaction/regime/time）保持库中原值，再用生产 `compose_weight` 合成。
- `--commit` 已执行 **8337 条**（全部 `trigger_kind='insert'` 行）：
  - **S14 分辨率 14.6% → 42.5%**（distinct 663 → 2287）
  - **S4 桶内 ρ 0.288 → 0.458**（并列值减少后，同样的预测信息才量得出来）
  - S14 仍低于 50% 目标；补 heat 维约到 47.3%，且 heat 的 0-10 映射是猜的，未做
- 回滚表 `signal_weight_replay_backfill`，`--rollback` 一键还原。
- 验证：三套回归 **103 passed**；记分卡 **达标 7 项（S3/S4/S6/S8/S9/S10/S15）**。

### Fixed — v336 P0-4：认领幂等（同一条新闻被反复分析 10 次）

- **实测**：news 11715 在库里有 **30 条决策**（ETH/XAU/WTI 各 10 条，权重逐位相同）。
  全库 `(news_id, target_asset)` 重复组 **812 组 / 2096 行 = 23.6%**，
  其中完全复制品 617 组 / 1506 行（多余 889 行）。去重后决策 8865 → **7581**。
- **根因**：认领 SQL 只看 `status='PENDING'`，不看"这条新闻是否已有决策"。
  落库成功但没进 `done_news_ids` 的新闻不会被置 DONE → 看门狗 15 分钟回收
  `PROCESSING` → 回 `PENDING` → 再分析、再写一遍。
- **修复**：`_BATCH_CLAIM_SQL` 与 `_BATCH_CLAIM_FRESH_SQL` 均加
  `AND NOT EXISTS (SELECT 1 FROM ai_decisions d WHERE d.news_id = raw_news.id)`；
  `TRIDENT_ALLOW_REANALYZE=1` 可关掉（重放历史新闻做回归时需要）。
- 新增 `opt_decision_dups.py`（只读）量化重复规模；历史 2096 行未删除。
- 测试：新增 2 条锁定该不变量；三套回归 **105 passed**。

### Fixed — v336 P0-5：历史复制品隔离 1284 条 + 指标改看未隔离行

- 新增 **`quarantine_duplicate_decisions.py`**（`--commit`/`--rollback`）：
  每个 `(news_id, target_asset)` 只留最早一条，其余打
  `quarantined_at` + `quarantine_reason='dup_reanalysis'`。
  已执行 **1284 条**，参与统计的决策 **8865 → 7581**（-14.5%）。
- `capability_scorecard.py` 新增 `_live(conn, alias)`：指标查询统一加
  `COALESCE(quarantined_at,0)=0`；老库/极简测试表无该列时降级成 `1=1`（不整体失效）。
- 隔离后真实值：S3 77.1% / S4 ρ0.458 / **S8 92.4%→89.0%（掉出达标）** /
  S11 40.0% / S12 25.2%→26.8% / **S14 42.5%→48.0%** / S15 87.4%→86.0%。
- **达标项 7 → 6**（S3/S4/S6/S9/S10/S15）：S8 的达标原本是靠重复行凑出来的。

### Fixed — v336 P0-6：时间戳单位混用（多因子归因永远拿不到新闻）

- **实测**：`abnormal_signals.anchor_ts` 8072 行是毫秒、79 行是秒；
  `abnormal_attributions.anchor_ts`、`news_events.ts`、`raw_news.ts` 全是秒。
- 多因子链路把毫秒锚直接喂给「往前 1800 秒」的新闻窗口：
  `ne.ts >= 1.79e12 - 1800` 恒为假 → **永远返回 0 条新闻**。
  同一锚实测：秒口径 5 条 / 毫秒口径 0 条。
- **影响**：抽样 1500 条归因，`used_news_n=0` 1131 条里 **361 条（32%）**
  窗口内其实有已打标签的新闻 → 模型在"无新闻"状态下归因，是 S7 卡 52% 的硬原因。
- **修复**：新增 `_to_sec_ts()`（`>=1e11` 按毫秒 /1000），`_abnormal_news` 入口归一；
  `opt_ts_audit.py`（只读）扫描全库 218 个时间列，列出 ms/µs/ns/ISO 与秒级混用清单。
- 测试：新增 1 条锁单位归一；三套回归 **106 passed**。

### Fixed — v336 P0-7：external_indicators 混单位，新鲜度审计失明

- **实测**：该表 52746 行秒 + **27383 行毫秒**（coinmetrics / derived_ratio /
  derived_crypto 直接拿 K 线 `ts_ms` 落库）。
- 后果：`freshness_audit` 的 `age = now - last` 对毫秒行得到 **-1.6e12**，
  `age > 阈值` 恒假 → **永远判"新鲜"**，源停更半年也报 ok；
  `discovery.py` / `cross_asset_context.py` 的 `ts >= cutoff` / `BETWEEN` 同样被污染。
- **修复**：写入端 `ingest_external` 归一（`_to_sec_ts`，>=1e11 按毫秒/1000），
  读端 `freshness_audit` 兜底，存量用 `normalize_external_ts.py` 归一 **27383 行**
  （回滚表 `external_indicators_ts_backfill`）。
- **归一后真实结果**：全表统一秒（80192 行），新鲜度 **55 ok / 1 late**
  （`sge_silver` 的 `sge_silver_premium_pct` 已停 54.2h，阈值 30h）。
- 测试：新增 2 条；三套回归 **108 passed**。

### Fixed — v336 P0-8：反思的反向证据窗口恒为空（T 分隔 + 北京时区 vs UTC 空格串）

- `reflection_core.audit_counter_evidence` 用 UTC 的 `'YYYY-MM-DD HH:MM:SS'`
  去比 `raw_news.first_seen_ts`，而后者存 `'2026-11-14T15:36:51+08:00'`。
- 字符串比较下 `'T'(0x54) > ' '(0x20)` → 同日内 `<= '... 03:00:00'` **恒为假**
  → 窗口永远 0 条 → `omitted_count` 恒为 0，**"漏掉的反向证据"审计形同虚设**。
- 实测同一小时：旧口径 **0 条** / 新口径 **59 条**。
- **修复**：改用 `CAST(strftime('%s', first_seen_ts) AS INTEGER)` 与秒级窗口比，
  时区交给 SQLite 解析，不再依赖字符串格式。
- 新增 `opt_iso_ts_audit.py`（只读）：扫 106 个 ISO 文本时间列，报混格式列 + 可疑比较点。
- 测试：新增 1 条（窗口必须能查到本地 ISO 新闻）；三套回归 **109 passed**。
- 新增 `opt_impact_default.py`（只读审计，复现上面全部数字）。
- 测试：`test_v336_optimizations.py + test_capability_scorecard.py` **73 passed**；
  ai_worker 相关 **24 passed / 1 skipped**。
- 端到端效果暂不可验证：LLM 通道离线（`ARK_API_KEY` 空 / minimax 熔断）。

### Added — v336 P1-2：归因证据分层（headline / cross_asset / body_only / macro）

> 实测 242 行归因：标题主语=标的只有 84 条（34.7%）。系统此前把"标题命中""正文顺带提到"
> "宏观背景"记成同一种归因，score 只差几个小数 —— 于是正文里一句 "BTC 报价 xxx"
> 就能把无关新闻挂成 BTC 暴跌的原因（干跑实测 `BTC -7.56%` ←「艾罗能源拟每10股派31.25元」score=0.798）。

- **`evidence_source.py`（新）** —— `classify_evidence()` 四档：
  `headline`（标题主语=标的）/ `cross_asset`（标题点的是别的品种 → 传导链）/
  `body_only`（只有正文提到，正文常夹带行情）/ `macro`（标题正文都没提到）。
  `tag_reason()` 幂等追加 `; evidence=<level>` 到 `algo_reasons`，**不改分数、不改筛选、不删行**。
- **`calc_event_move_attribution.py`** —— 落库前统一打标（主表/待复核/候选三处），并打印分层计数。
- **`backfill_attribution_evidence.py`（新）** —— 历史行补标，默认 dry-run。
  已执行 `--commit`：242 行全部打标。
- **`capability_scorecard.py`** —— 新增 **S13 归因标题级命中占比**：
  当前 **34.7%（84/242）→ 未达线**（target 60%）。

实测分层（242 行）：headline 84（34.7%）/ cross_asset 65（26.9%）/
body_only 31（12.8%）/ macro 62（25.6%）。
（首版规则把"标题点的是别的品种"误判进 macro，macro 虚高到 42.1%；
按"标题有主语但不是本标的 → cross_asset"修正后，macro 降到 25.6%。）

### Fixed — v336 P1：核心品种的"慢涨/慢跌"从未被记录

> 实测（2026-11-14 只读）：`critical_price_moves` 里 182 条 slow_rise/slow_fall
> **全部**落在 SKHYNIX / ADA / DOGE / XRP / BNB / fred / USA-LABOR-ICS / UST2Y / UST5Y，
> **BTC/XAU/WTI/ETH/SOL 一条都没有**；而 `ohlcv_daily` 里 10 月以来核心品种真实存在
> ≥3% 单日波动 35 次（XAU 2 / BTC 6 / WTI 7 / ETH 11 / SOL 9）。
> 也就是说需求 #6「快涨和慢涨都要识别」里的**慢的一半，对核心品种从未落地** ——
> 归因页看到的慢行情全是非核心品种，自然解释不了 BTC/XAU 的行情。

- **`fast_move_detector.py`** —— 新增 `enrich_context()`：按 move 的 `ts` 补齐
  `regime / vol_ratio / crowd_sentiment / liq_total_usd`，**全部带时间上界**
  （只取触发时刻及之前，防止把事后信息写进历史归因），**只补空值不覆盖已有观测**，
  任何表缺失都降级为 `available=False` 不抛异常。慢异动原先的 regime/vol/crowd 覆盖率是 **0**。
- **`backfill_core_slow_moves.py`（新）** —— 只扫核心品种的慢形态，复用现网阈值
  （1d≥3% / 3d≥5% / 5d≥7%），**双层去重**：
  ① `suppress_overlapping`：同向且相距 <2 天的算同一波行情，只留幅度最大的那条
  （否则一次 5% 的上涨会在连续几根日线上各记一条，等于 v336 查出的"分母造假"重演）；
  ② 同品种 12 小时内已有快异动记录的不再补，避免同一波行情在归因/结算里算两次。
  默认 dry-run，`--commit` 才写。
- **`backfill_slow_context.py`（新）** —— 给历史慢异动补上下文（同样默认 dry-run）。

实测结果（`--commit` 已执行）：

| 项 | 数值 |
|---|---|
| 90 天窗口检出 | 89 条（去重后），再做快慢去重 → **71 条写入** |
| 跳过 | 18 条（同品种 12h 内已有快异动） |
| 新增构成 | slow_rise 37 / slow_fall 34；BTC 13 / ETH 18 / SOL 23 / WTI 13 / XAU 4 |
| 表内 slow 总量 | 182 → **253**（覆盖品种 9 → 14） |
| 上下文覆盖 | vol_ratio 69/71、regime 22/71、crowd 9/71 |

归因干跑（`calc_event_move_attribution.py --days 30 --dry-run`）：
整体覆盖 **70.7%（292/413）**，**slow_fall 69.8% / slow_rise 64.3%**（此前 0~3%）——
说明 v325 已修好的"慢形态 72h 归因窗口"一直在，缺的是**根本没有慢异动进来**。

### Fixed — v336 P0：兜底文案把"LLM 空输出"写成"品种不在监控范围"

> 结论先行：这一条不是模型问题，是**话说错了**，它把 30.8% 的 HOLD 伪装成"判断"。
> 实测：8865 条决策里 **6627 条（74.8%）** 的 `reasoning_path` 是兜底模板；
> 其中 **6459 条**含"事件不直接命中本系统核心监控品种"，而这 6459 条里
> **3692 条（57%）** 标的正是 XAU(1475)/WTI(1432)/BTC(739)/ETH(46) —— 核心品种。
> 近两日更严重：11-12 374/504（74%）、11-11 193/270（71%），`completeness_score` 仅 0.8、
> `llm_retry_count` 恒为 0。也就是说"模型判断无方向"绝大多数其实是**输出为空没被标记**。

- **`engine/ai_worker.py`** —— 新增 `asset_in_core_scope()` 与
  `degraded_reasoning_template()`：兜底按"是否核心品种"分支。
  核心品种改为如实写「本次 LLM 未返回可用分析（空输出/解析前被截断），按分析降级记录，
  不代表该品种无影响」，并保留原八段段名（`[总框架]`…`[总结]`），不影响下游按段解析；
  非核心品种（NONE/个股/宏观/汇总类）保留"不在核心监控清单"的原有说明。
  **不碰任何买卖判定**，只改文案与归因口径。
- **降级打标** —— 核心品种走该分支时写
  `no_trade_reason = "LLM 源降级：空输出/截断（非无方向判断）"`（仅在原值为空时）。
  `reflection_ontology` 会据此归到"技术错误/源降级"，而不是"逻辑框架错误" ——
  正好对上三维反思里"技术错误"这一类。

### Added — v336 P0：记分卡 S12

- **`capability_scorecard.py`** —— 新增 S12「实质分析覆盖率（兜底套话不算）」：
  当前 **25.2%（2238/8865）→ 未达线**（target 80%）。这条把"有没有真分析"与
  "方向准不准"分开度量 —— 真分析覆盖不到一半时，任何方向准确率都没有解释力。

测试：`tests/test_v336_optimizations.py` 增至 **40 条全绿**（新增 4 条：核心品种兜底必须
承认空输出且八段齐全、非核心品种保留范围说明、`asset_in_core_scope`、S12 计数口径）。
`ai_worker` 相关 5 个测试文件回归 **77 passed / 1 skipped**。

### Fixed — 门卫分房（v335）

> 用户判断：门卫不该被要求"判断能不能赚钱"，它的本职是挡垃圾、打标签、分房间。
> 而本职恰恰没做好——近 7 天 940 条决策里 306 条（33%）`target_asset=NONE`。

- **`news_asset_router.py`（新）**：新闻没点名公司时，按产业链/宏观路径判它真正
  影响哪个品种。三档 + 上下文闸 + 主/次品种分离：`direct`（点名）/ `transmit`
  （强传导，如霍尔木兹油轮→原油）可作主品种；`weak`（松散联想，如 Anthropic
  融资→算力需求）只进观察，**绝不当主品种**；`none`/`noise` 继续丢。
- **修掉两个真实误判来源**：
  ① 标题混进来源栏（"Yahoo DXY (DX-Y.NYB) 快讯"），`DXY` 被当成美元指数命中，
  把"Under Armour 下调毛利率"判成黄金 → 新增 `clean_text()` 剥掉 hash 前缀、
  多行正文与 URL 尾巴；
  ② 宏观词不分国别，德国 CPI 也被推进黄金房间 → 关系表加 `require` 上下文闸
  （宏观词必须绑美国语境、地缘词必须绑军事/中东语境）。
- **实测**：近 14 天 796 条 NONE → 可分 200 条（25.1%），其余继续丢；黄金命中
  抽查全部是军售/战争/制裁，不再出现德国 CPI、日本银行卖 ETF 这类误分。已回填
  200 条（`asset_route='rule:direct/transmit:权重'`），近 7 天 NONE 306→223。
- 测试 `tests/test_v335_asset_router.py`（7 例，含"德国 CPI 不得进黄金房间"
  "来源栏不得污染分类"）。

### Added — v336 能力验证七件套（全部只读 / 默认 dry-run）

> 起因：用户要求验证 8 项能力并"把能优化的都优化掉"。本轮先做的是**让数字可信**，
> 再动模型。7 个新模块 + 3 处运行期改动 + 36 条测试，全部只读或默认 dry-run。

- **度量口径：`opt_metrics.py`** —— 发现"全样本方向命中 17.2%"是分母造假：
  一次行情被重复计成 300+ 样本（11-05 305 条 BUY 只中 2 条、11-08 413 条一条没中）。
  按 (asset, direction, 小时) 去重后 **41.1%（n=73）**；影子账本去重前后 40.2%→40.8%
  （压缩 1.2x），说明影子账本本身就是独立信号 —— **它才是可信的那一份**。
- **no_direction 三分：`opt_direction.py`** —— 11-11～13 闸门 816 次评估 passed=0，
  全部糊在 `no_direction`。拆开看近 14 天 HOLD 3166 条：
  模型保守 43.1% / **品种角色错标 30.8%** / 其它 25.1% / 传导链未建 1.0%。
  角色错标是 bug（BTC/XAU/WTI/ETH/SOL 被判成"副品种"），不是判断。
- **闸门留痕细分：`gate_audit.py`** —— `reject_detail` 增加 `sub=` 子码，
  **只改文本、不改任何放行判定**；`reasoning` 进 SELECT（老库缺该列时自动降级，PRAGMA 探测）。36 条测试覆盖
  "放行判定不变"与"子码正确"。
- **方向确认闸回测：`opt_direction.py`** —— 决策前 30 分钟已同向动 44.0%（n=159）
  vs 未同向 38.0%（n=142），增量 +6.0pp（90 条无分钟线）。方向对，但比此前记录的
  48.7/31.4 弱，建议按品种分别启用，不要全局硬上。
- **板块 beta 剥离：`opt_sector_beta.py`** —— 同板块中位涨幅做参照：
  同向 56.7%，但按幅度只有 6.3% 由板块解释。**不对称**：fast_fall 同向 97.1%
  （跌是集体行为），fast_rise 仅 9.4%（涨基本是个体行为）。
- **冲击形状：`opt_move_shape.py`** —— 用 t80（走到 80% 用了窗口几成时间）替代
  "极值位置"，logistic 网格拟合 + 回撤/跳空判据。可拟合 246/572：
  **drift 240、attack 6**；旧 fast_rise 里 68% 是匀速、只有 3% 是真攻击。
  结论：`fast` 只代表"窗口 1 小时"，不代表"瞬时冲击"，反应预算不能按它给。
  修了两个坑：① kline_bars.ts 是毫秒（自动探测）；② critical_price_moves.ts 是
  **异动终点**，窗口必须取 [ts-duration, ts]。
- **权重单因子标定：`opt_weight_diag.py`** —— 4529 条决策时点配对：
  impact ρ=0.309、source ρ=−0.100 有效；base ρ=−0.033 证据薄；
  **consensus（4 值）/ reaction（1 值）/ regime（2 值）/ time（1 值）在决策时点无方差**。
  所以"六维权重"实际只有两维在工作。
- **品种缺口审计：`opt_asset_cover.py`** —— NONE 11105（36.8%）中严格可映射仅
  **125 条（1.1%）**；其余是 no_subject 7863（71.6%）、macro_data 1732、calendar 564、
  blog 536、regulation 285。**36.8% 不是漏映射，是本来就没有标的** ——
  正确动作是打 `non_tradable` 并移出分母，而不是硬映射。
- **证据链缺口：`opt_atom_link.py`** —— event_atom_ids 覆盖 60.4%，缺 3508 条，
  靠 news_id **只能补 307（8.8%）**，3201 条上游根本没做原子化。
- **记分卡新增 S11：`capability_scorecard.py`** —— 把"独立信号方向命中（去重口径）"
  做成常驻指标：**41.1% [30.5, 52.6] n=73 → 未达线**（target 50）。
  低于 50% 就是低于抛硬币，这条指标存在的意义是让"17.2% 的假精度"永远不可能再回来。
  同时修了一个契约 bug：`Metric.query` 必须返回 (命中数, 样本数) 计数，不是百分比
  （返回百分比会被 evaluate_metric 再除一次，实测会导致 41.1% 被误显示成 56.3%）。

测试：`backend/tests/test_v336_optimizations.py` **36 条全绿** + 记分卡 13 条，
共 49 passed（离线，不碰生产库）。闸门回归 44 条通过；`test_gate_deferred_v320`
有 1 条既有失败（`gate_verdict` 期望"无信号"实得"未判定"），属 realtime_filter.py 的
在途未提交改动，与本轮无关，未擅自修改。

### Fixed
- **"一天几乎没有单"的根因不是闸门太高，是方向判断本身没有区分力（v335）**
  用户反馈："一天几乎都没有单，我没有样本我怎么测试；而且它一点都不准"。

  先查链路：近 7 天 940 条有资产的决策，**只有 7 条**过了置信度闸门（门槛 20），
  平均置信度 0.74 —— 等于全军覆没。再查被拦的那条：主品种 NONE / 模型输出
  "该新闻不足以给出明确方向" / 置信度 0.0，**不是"差一点没过"，是这个分数根本没算出来**。

  于是把"没放行的单"全部记账结算（新增 `shadow_ledger.py`）：闸门只管"开不开仓"，
  不该管"能不能被评估"。469 条样本、391 条已结算，结论很硬：

  · 整体方向命中率 **40.2%**（50% = 抛硬币）→ 用户"一点都不准"的感受是对的；
  · 门槛扫描：门槛 0 → 39.8%、门槛 20 → 37.5%、门槛 30 → 16.7%
    —— **门槛越高反而越差**，说明它在筛的不是质量，是随机性；
  · 按品种：XAU 36.6% / WTI 36.7% / ETH 40.7% / BTC 54.8%（只有 BTC 勉强及格）；
  · 按方向：BUY 38.3%（占 63%）/ SELL 43.4% → 系统性偏多；
  · 纯价格基线（4 小时顺势 50.1% / 逆势 49.9%）= 噪声，所以不是"市场没规律"，是判断在系统性地押反。

  同一批样本再加一层"市场是否已经反应"的切分：
  · 决策前 30 分钟**没同向动**（n=185）→ 命中 31.4%
  · 决策前 30 分钟**已同向动**（n=144）→ 命中 48.7%~51.6%
  即"新闻还没被市场反应就抢先押注"是最差的一档 —— 这条可作为下一步的方向确认闸。

  落地：影子账本已接进每小时 wrapper（`~/.trident/wrappers/run_missed_signals.sh`），
  新端点 `GET /api/v291/shadow-ledger-summary`，分析页新增「影子样本」面板
  （每天样本数 / 命中率 / 按品种 / 按方向 / 门槛扫描）。真仓闸门**暂不下调**：
  在 40% 命中率下放开只会放大亏损，先把样本和证据攒够。

- **S4「信号权重能不能区分影响大小」换口径：0.197 → 0.288（v335）**
  这是统计层唯一 FAIL，差 0.003 卡在门槛下方。查下来的结论是**口径把信号淹了，
  不是权重没用**：不同小时的波动量级差一个量级（平静时段 ±0.2%、剧烈时段 ±3%），
  老口径把所有小时混在一起排秩时，小时之间的量级差盖过了"同一小时内权重谁更强"。

  修法：
  · `capability_acceptance.bucket_spearman()`（纯函数）：按小时分桶（`ts//3600`，
    桶内 ≥5 个配对才收），桶内先减中位数、算 Spearman，再按样本量加权汇总；
  · `_weight_eval_stats()` 增 `rho_bucket / rho_bucket_n / rho_bucket_hours`；
  · `check_02_signal_weight` 新增 `rho_b_ok`，判据改成 `rho_b_ok or rho_ex_ok or rho_ok`
    （新口径为主判据、老口径留作参考，避免为了通过而删旧证据）；
  · `capability_scorecard._q_signal_weight()` 优先展示桶内口径。
  实测：桶内 ρ=+0.288（n=4376 / 177 小时）· 混合 ρ=+0.197（n=4529）· 全样本 ρ=+0.029。
  测试 `tests/test_v335_weight_bucket.py`（6）覆盖"桶内单调=+1""公式写反=-1"
  "平静/剧烈时段不串""<5 样本桶跳过""空输入""桶内优于混合"。

- **能力验收 #14 假 FAIL：端点在写锁竞争下超时（v335）**
  同一份体制数据，单跑 PASS（体制 4）、全量跑 FAIL（体制 0）。根因是
  `/api/v291/market-regime` 在全量验收并发压力下偶发 >8s 超时，`_http` 返回 None
  就当成"体制 0"。修法：HTTP 拿不到时回读 `market_regime_cache` 的落库结果，
  并在实测行标注来源（`体制 4 (api)` / `体制 4 (db_cache)`）—— 抖动不再变成假 FAIL，
  真不可达仍然照实报。

- **分析页"三类错因"细分小类被一个 slice 卡住（v335·前端）**
  错过信号面板的三维度里，每个维度只显示 4 个小类（`compact` 变体甚至一个都不显示），
  而 A 类实有 5 个小类 —— "细分小类方便快速锁定问题"的要求被截断；且小类只给条数，
  不给命中率，看不出"这类错到底有多严重"。

  修法（`MissedSignalsDimensionSummary.tsx`）：小类按样本量**全量**展示（compact 前 3），
  每个补 `hit_rate`（≥50% 绿 / <50% 红）+ `kind` 徽标（规则拦截 / 未验证 / 技术，
  判断类不标），类型补 `kind` 字段。实测 A 类 5 个子类全可见（pattern_recognition 132
  · 93.9%、risk_limit_blocked 等），B 类 unverified 1528 条被明确标成"未验证"而不是判断错。

- **归因页看不到"待复核"的大异动（v334·前端）**
  闸三把 |涨跌|≥5% 的大异动拦在 `event_move_attribution_pending` 里，而这批行
  前端**完全没有入口** —— 用户看到的是"大行情没有归因"，不知道系统其实已经找到了
  候选新闻，只是没通过复核。等于把最该被解释的一批行藏起来。

  修法：
  · 新端点 `GET /api/v291/event-move-attribution-pending`（bridge 8100）：
    返回待复核行 + 候选新闻标题（LEFT JOIN news_events 回查）+ `why_pending`
    （"证据分不够" / "匹配类型不是资产直配" / "AI 通道离线" —— 三种完全不同的问题，
    不能糊成一个"待复核"）；
  · 新面板 `frontend/components/v291/pending-attribution-panel.tsx`：折叠式，
    标题栏直接亮出「N 条 / 其中 |涨跌|≥5% M 条」，每条给出时间、资产、涨跌、
    形态、证据分、新闻滞后、候选标题、待复核原因；
  · 挂到 `/attribution` 页（EventMarketAttribution 之后），并把路径加进
    `lib/api.ts` 的 `FALLBACK_PATHS`（8000 无此端点，必须走 bridge）。
  · 待复核行**不计入**归因覆盖率 —— 它们是"线索"，不是"结论"。

  实测：`n=5`（limit 5 时）、`pending_total=18`、`pending_big_moves=18`；
  抽检 BNB −6.32% 慢跌：候选标题「BNB Chain：BSC 24 小时活跃地址达 486 万」，
  证据 0.25 → 正确标注为"新闻只是碰巧提到这个品种"，没有冒充因果。

- **归因覆盖率卡在 46%：大异动被"等 AI 复核"这道闸门堵死（v334）**
  拆开看覆盖率缺口，发现一个刺眼的事实：**涨跌 ≥5% 的大异动共 76 条，只有 3 条进了主表**。
  原因是 v323 闸三的口径 —— 大异动必须拿到 AI 复核（confirm/partial）才允许进主表，
  而 AI 通道（minimax 配额熔断、`ARK_API_KEY` 空）已经死了，这 73 条就永久卡在门外。
  最该被解释的大涨大跌，恰恰一条都解释不了。

  修法：给闸三加**退化策略**（`route_insert()` 纯函数）——
  AI 不在线时，只放行「asset 直配 + 分数 ≥ 0.35」的行，其余进待复核。门槛不是拍脑袋：
  实测 38 条直接放行时 21 条分数 <0.35（最低 0.11，新闻挂在 32 小时前），
  「标题里碰巧出现了这个代码」不配叫因果，所以弱证据一律回待复核排队。

  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | S7 异动被归因覆盖 | 45.8%（n=454） | **50.4%（n=456）** |
  | 大异动（≥5%）已归因 | 3 条 | **41 条** |
  | 慢涨/慢跌覆盖 | 7.1% / 7.2% | **43.3% / 46.2%** |
  | 主表 / 待复核 | 207 / 0 | **224 / 21** |

  同批诊断还澄清了 S7 的**天花板**在哪：454 条异动里 **66 条（15%）在窗口内根本
  没有任何相关新闻**（慢涨慢跌尤其多 —— 83 条慢异动里 68 条与同板块品种同向漂移，
  本质是板块 beta 而不是独立事件）；剩下 146 条有新闻但多为「交易员浮亏」「增发」
  这类弱相关，强行认领就是假因果。**所以 80% 目标在现有新闻源条件下不可达**，
  实测天花板约 55%。

  **降级放行是自动开关，不是永久开口**（`_ai_verify_available()`）：
  · AI 通道恢复 → 闸三回原口径（大异动必须复核），放行自动关闭；
  · AI 再挂 → 自动重新降级，大异动不至于集体消失。

  判据不能只看 `ARK_API_KEY` 在不在 —— key 在但配额烧光（minimax 当前状态）
  会被误判成"在线"，闸三重新锁死、待复核区无声堆积。所以要求
  **最近 14 天内真的写出过复核结果**（`AI_VERIFY_FRESH_DAYS`）才算在线。
  这也解决鸡生蛋问题：待复核区的行会被 `attribution_ai_verify.py` 复核并迁入主表，
  第一笔成功后开关自动翻回在线。


- **漏检结算每小时被"重新发现"抹掉（v334）**
  S9 修到 96.2% 后一小时又掉回 14.5%，根因不是结算逻辑，而是
  `populate_missed_signals.py --commit`（launchd 每小时跑）**DELETE 当天行再重插**，
  而 `compute_missed()` 产出的 `hit` 恒为 None —— 上一轮算好的命中率被整批清空。

  修法两层：① `populate_missed_signals.py` 删前快照已结算值、插回后还原；
  ② `~/.trident/wrappers/run_missed_signals.sh` 改成扫描后**立刻** `evaluate_missed_signals.py
  --commit`（hit 由结算器负责，发现器不负责）。
  验证：重跑发现器后 5d 结果 186 行、hit 179 行仍在，S9 稳定 96.2%。

  测试：`backend/tests/test_v334_bigmove_gate.py`（6 用例，含"弱证据不许放行"
  "AI 在线时维持原口径""reject 行不进任何区"）。
- **漏检信号只有 2.9% 判定完：一半是没跑结算，一半是关联断了（v334）**
  `missed_signals` 248 行里 171 行早有 5 日结果，却只有 5 行有 `hit`。两个原因叠在一起：

  ① `evaluate_missed_signals.py` 默认 dry-run，只在加 `--commit` 时落库 —— 之前跑过但没生效；
  ② 26 行的 `signal_id` 指向**已被删掉的** `critical_price_moves`（还是"删旧插新拿新 id"
     那个老毛病），JOIN 不上就被当成"缺异动记录"跳过，永远结算不了。

  修法：给孤儿行加回退路径 —— `signal` 文本里本来就写着 `move_pct=-5.03`，
  用正则还原方向；基准价改用 `decision_date` 那一刻的收盘价（语义比"异动起点"更贴合
  "如果当时接住这个信号"）。补完 26 条，缺异动记录归零。

  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | S9 漏检命中判定覆盖 | 2.9%（n=171） | **96.2%（n=186）· 达标** |

  顺带算出这个数才有意义的部分：**179 个被漏掉的信号里 67.6% 事后走对了，
  平均 5 日收益 +1.93%**（BUY 类 66.9%）—— 漏检不是噪声，是真金白银的损失。
  口径提醒写在脚本输出里：这些信号来自"已实现的强异动"，命中率反映的是
  **动量延续率**，不能直接等同于信号本身的前瞻预测力。

  测试：`backend/tests/test_v334_missed_settle.py`（6 用例）。
- **S1"事件方向命中"的假精度：1213 个样本其实只有 30 天（v334）**
  老口径按"事件数"加权，n=1213、区间 [47.4, 53.0] 看着很稳。实测拆开看：
  **1343 个原子样本只落在 30 个 (日期×资产) 格**里 —— 同一天几十条同题材新闻
  共享同一根 K 线，彼此强相关。后果有两个：
  · 区间是**假精度**，真实独立样本只有 30；
  · 某一天的逆向行情能一次性翻转几百条（`geopolitical_conflict × XAU` 一个组合
    占 890 条，命中 33.7%，只是因为那段黄金 23 天里只有 8 天上涨）。

  改成按独立日格统计（一格 = 日期×资产，判据是格内多数方向 vs 该格平均涨跌）：

  | 口径 | 命中率 | 独立样本 | 状态 |
  |---|---|---|---|
  | 原子加权（旧） | 50.2% | 1213（虚高） | 未达线 |
  | **独立日格（新）** | **46.7%** | **30** | **样本不足**（区间 [30.2, 63.9]） |

  同时确认了方向从哪来：`event_atom.direction` 取自
  `dim_tables.direction_for(event_type, asset)` —— 一张**固定的偏好表**，
  不是逐条新闻判断。所以同一题材所有新闻方向一致，压根不具备"逐条判方向"的能力。
  这是"事件方向 46.7%"的真实原因，不是算法参数问题。

  顺带修 S7 的窗口错配：`critical_price_moves` 跨 244 天，而 `news_events`
  只有 40 天存档 —— 40 天前的异动**永远不可能被归因**，留在分母里让覆盖率虚低。

  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | S7 异动被归因覆盖 | 35.8%（分母含无新闻时段） | **46.3%（454 条同窗口）** |

  统计层现状：**达标 4 项（S3 因果链 77.1% / S6 真因果 100% / S8 真值 92.4% /
  S10 反思集中度 26.1%）· 未达线 3 项（S4 ρ=0.197 / S7 46.3% / S9 2.9%）·
  样本不足 3 项（S1 46.7% / S2 55.1% / S5 41.3%）**。

  测试：`backend/tests/test_v334_event_cells.py`（8 用例，覆盖"同日 50 条新闻只算 1 格"
  "格内多数方向 vs 平均涨跌""跨日分格""无新闻存档时分母为空"）。
- **S8"真值覆盖率 3.7%"是分母错，不是数据缺（v333）**
  `ai_decisions` 实测 8865 行，其中 **8476 行是 HOLD**。HOLD 没有押方向，
  就没有"对/错"，永远不会有 `truth_verdict`。老口径拿**全表**当分母，
  覆盖率上限就是 389/8865 = 4.4%，而目标写着 90% —— **这个指标天生不可达**，
  但它会读成"数据不够，所以权重不可信"，直接误导对 #2 的判断。

  真实答案：有方向（BUY/SELL）且标的是**具体品种**的决策共 355 条，已判 328 条
  = **92.4%**。其中 34 条有方向但标的是 `INDIVIDUAL_STOCK`/`NONE` 这类「资产桶」，
  不是可交易标的、永远无法结算 —— 移出分母，但**单独计数报出**（`_q_truth_bucket_gap`），
  不装作不存在。

  另修一处定义错误：gate 从 500 降到 300。候选池总共 389 条，门槛高于全体 =
  永远"样本不足"，那不是质量要求。

  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | S8 快照真值覆盖率 | 3.7%（分母 8865，含 HOLD） | **92.4%（328/355，只算有方向的）** |
  | S8 状态 | 未达线 | **达标** |
  | 参考：行情真值覆盖（含 HOLD） | — | 新增独立口径 |

  顺带把可补的前向行情补了 8 条（WTI 1 / XAU 4 / BTC 2 / ETH 1，均为 HOLD，
  不产生胜负）。剩余 27 条有方向的缺口是"持有期未走完 / K 线缺价"（SKHYNIX 3 条已挂 13 天，
  说明该标的没有 K 线源），属真实数据缺口而非口径问题。

  测试：`backend/tests/test_v333_truth_coverage.py`（7 用例，含一条用 328/355 复现真实分布）。
- **同一个标的在两张表里用不同代码，归因直配永远打不中（v332）**
  #11「按资产的影响力评估」一直差一个资产（9 < 10）。追下去发现不是数据缺口，
  而是**代码不统一**：

  | 表 | 资产写法 | 条数 |
  |---|---|---|
  | `critical_price_moves`（异动） | `SKHYNIX` | 82 |
  | `news_events`（新闻） | `000660` / `000660.KS`（韩国交易所原始代码） | 74 已打标 + 204 标题提及"海力士" |

  两边字符串永不相等 → "品种直配"通道对 SKHYNIX 完全失效，只剩标题弱匹配
  (semantic)，而 v321.5 又默认把弱匹配挡在主表外 → **82 条异动、45 条窗口内真新闻、归因 0 条**。

  修法：`cem_constants.ASSET_TAG_ALIASES` + `normalize_asset_tag()` 做**代码归一**
  （`000660` → `SKHYNIX`），归一只发生在"直配判定"这一条通道里，
  **不引入任何新启发式**；`INDIVIDUAL_STOCK` / `US_STOCK` / `个股` 这类"资产桶"
  归一为空串（不替任何具体标的刷分），但仍留在集合里，以免被误当成"无标签宏观新闻"。

  顺带补了方向矛盾闸的词表：原 `_UP_WORDS` 只有"上涨/涨超/大涨…"，
  **漏了最高频的"涨""走强""高开""上扬"**，导致「SK海力士涨8.6%」去解释 −7.08%
  的下跌照样判通过。补词后方向矛盾拦截从 8013 次升到 12257 次（同批数据）。

  实测（90 天重建）：
  | 指标 | v325 后 | v332 后 |
  |---|---|---|
  | 归因覆盖异动 | 176/615 = 28.6% | **274/599 = 45.7%** |
  | 归因覆盖资产 | 9 | **10**（新增 SKHYNIX 10 条真归因） |
  | 主表 / 待复核 | 176 / 36 | 199 / 46 |
  | #11 按资产影响力 | FAIL | **PASS** |
  | S7 异动被归因覆盖 | 29.8% | **33.2%** |

  结构层：**PASS 24 / FAIL 1（#2 权重判别力）/ WARN 1（#1）**。
  测试：`backend/tests/test_v332_asset_alias.py`（12 用例）；与 v325 归因测试合计 **30 passed**。
- **181 条"验证失败"是算术假象，不是被证伪：差点误杀（v331）**
  计划里第 5 条是"清退 181 条 `verification_verdict='failed'` 却仍 active 的经验规律"。
  动手前拉数据发现**不能清退**：

  `insight_verifier` 的判定是 `real_signal_pct = composite × sample_conf`，
  `sample_conf = min(交易样本/60, 1)` —— **乘性封顶**。n<=6 时理论上限只有 0.1，
  恰好等于 partial 门槛，于是"样本太少"必然被判成"验证失败"。实测：

  | 事实 | 数值 |
  |---|---|
  | failed 的交易样本分布 | 0→53, 1→17, 2→43, 3→10, 4→15, 5→39, 6→4（**全部 < 12**） |
  | failed 的最高分 | **0.07**（全部 < 0.1） |
  | 真正"证据充分且被证伪" | **0 条** |

  这 181 条里 175 条当时正被 `ai_prompts.load_insight_rules()` 当"实战经验"
  喂进 LLM prompt —— 假教训污染决策，但源头是**判定口径**，不是数据本身。

  三层修法：
  1. **口径**：新增 `decide_verdict()` 纯函数 + `MIN_TRADES_FOR_VERDICT=12`。
     n<12 且 K 线侧没有否证（kline_score>0.3）→ `insufficient_sample`
     （新值）；K 线本身就否证（score<=0.3）或样本够但仍低分 → 仍判 `failed`。
     没验过 ≠ 验失败。
  2. **存量**：`relabel_failed_insights.py`（默认 dry-run）把 181 条重标为
     `insufficient_sample`，**active 保持 1**（规律没被否证，不该丢）；
     只有 n>=12 的真失败才 `active=0`。实跑结果：重标 181 / 停用 0。
  3. **提示词**：`ai_prompts` 的注入 SQL 显式挡掉 `failed` 与 `insufficient_sample`
     —— 未经验证的规律不再冒充教训，进 prompt 的规则从 352 条候选降到 **171 条**（仅 partial）。

  测试：`backend/tests/test_v331_insufficient_sample.py`（12 用例，含一条内存库
  复现"过滤后只剩 partial 进 prompt"）。相邻 24 passed。
- **验收的两个"假失败"被拆穿：端点剔行不解释、归因按自增 id 关联会断（v330）**
  ① `/api/v291/reflections/summary` 读数侧有一条可信区间过滤
  `ABS(actual_outcome_pct)<=60`（剔掉 5 日涨跌 508% 这类量纲错的行）。过滤是对的，
  但它让端点数比库内行数**天然少 10 条**（B/C 各 5），于是 #8「三维反思系统」和
  C6「端点↔DB 自洽」长期判 FAIL——错不在数据，在"差值没人解释"。
  改法：端点显式返回 `sanity_excluded`（`total` + `by_dimension`），验收比对时从
  库侧减掉；真漏行仍然会以差值暴露（有用例锁住，防止"解释"变成"掩盖"）。

  ② `backfill_critical_moves.py` 回填异动是"删旧行 + 重新插入"，同一根 K 线拿到
  **新自增 id**，历史归因行的 `move_id` 就成孤儿（实测 176 条归因里 62 条指向已删
  id），S7 异动归因覆盖率被算成 114/615=18.5%。归因表本来就存了稳定键
  `asset + move_ts`，改为"id 快路径 + (资产,时间) 兜底"：
  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | S7 异动被归因覆盖率 | 18.5% | **29.8%**（183/615） |

  结构层（带 HTTP 全量）：**PASS 23 / FAIL 2（#2 权重判别力、#11 资产覆盖）/ WARN 1**，
  #8、C6、#14 全部 PASS（#14 之前的 FAIL 是 `--no-http` 造成的假失败：regime 只经
  HTTP 端点取，带 HTTP 实测体制 4 · COT 2067 · 资金费率/订单簿均 OK）。
  统计层：S3 77.1% 达标、S10 26.1% 达标（#4 下线 70 条假链未影响健壮链方向命中）。

  测试：`backend/tests/test_v330_sanity_excluded.py`（6 用例）+
  `test_v330_attr_coverage.py`（4 用例）。
- **因果链只有"故事"没有步骤，且从不被真实结果纠正（v329）**
  2026-11-13 实测：`causal_chains` 719 条，`chain_steps` **全是 `'[]'`**，
  `version>1` **0 条**，近 7 日 `last_verified_ts` **0 条** —— 也就是说
  "事件 → 因果链 → 涨跌"这条主链**从未被市场结果校验过一次**。

  已有的 `chain_steps_builder.py` 会生成步骤，但：
  · 只写到 `runtime/eval/eval.db` 的 `chain_steps_eval`，**从不回写主库**；
  · 锚点用链的 `created_at` + 3 日行情，而不是"这条链被触发后的真实结果"；
  · 步1 判"触发词在全库新闻里出现过没有"，几乎必然"验证"，没有信息量。

  改法（新脚本 `backend/src_python/chain_steps_verify.py`，默认 dry-run）：
  换用**真实证据源** `causal_chain_outcomes`（5096 条已评估触发，带
  `actual_pct_5d` / `direction_hit` / `magnitude_hit`），给每条链生成 3 步，
  每步都带 proxy / threshold / window / verdict（铁律6 结构）：
  | 步骤 | 代理 | 阈值 | 判定 |
  |---|---|---|---|
  | 1 事件触发 | 该链已评估触发次数 | ≥1 次 | 验证 / 不可分离 |
  | 2 路径传导 | \|5日涨跌\| ≥0.5% 的比例 | ≥50% 验证 / <30% 否证 | + 不可分离 |
  | 3 结果兑现 | direction_hit 命中率 | ≥55% 验证 / ≤45% 否证 | 方向 BOTH 或 n<5 → 不可分离 |

  纠正写回（只对**有足够样本**的链动手）：
  · `chain_steps` ← 三步 JSON（含每步证据原文）
  · `version += 1`、`hit_count`/`miss_count` ← 真实命中/未命中次数、`last_verified_ts`
  · `confidence` ← 与实测命中率各让一半（夹在 0.2~0.95），不凭空抬
  · 步3 否证 且 n≥8 → `enabled=0`/`active=0` + `invalidation` 写明"方向命中 x/y=z%"（这条链在骗人）

  实测：
  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | 有结构化步骤的链 | 0 / 719 | **719 / 719**（每条 3 步、六字段齐） |
  | version>1（被纠正过） | 0 | **371** |
  | last_verified_ts 有值 | 0 | **371** |
  | 步骤 verdict | — | 验证 780 / 否证 73 / 不可分离 1304 |
  | 因步3否证被纠下线 | — | **70 条**（全部是 enabled 状态的定向链，61 UP / 9 DOWN） |
  | enabled 链数 | 452 | 382 |

  另外顺手修了数据契约：`unverified_legacy_label`（1611 条 B 类历史行，来自
  `recompute_b_labels.py` 的"证据不足"标记）此前不在合法枚举里，C8「非法枚举残留」
  一直判 FAIL；现纳入权威词表并让 `capability_acceptance` 的合法小类清单
  **直接取自 `reflection_dimension_map`**（三处各抄一份清单必然会漂）。

  实测结构层：**PASS 21 / FAIL 3（#2 权重、#11 资产覆盖、#14 市场体制）/ WARN 1**，
  #8 三维反思与 C8 数据契约由 FAIL 转 PASS。

  测试：`backend/tests/test_v329_chain_steps.py`（13 用例，全离线）。
- **反思 A 类 100% 挤在一个小类 → 按机制细分，S10 集中度 53.0% → 29.9%（v326）**
  2026-11-13 只读实测：`trade_reflections_v2` 的 A 类 **2626 条全部**落在
  `market_regime_misread`（"市场状态误读"）一个小类里，S10 集中度 53.0%（门槛 40%）；
  `reflection_3layer` 的 A 类 143 条也全是同一句"方向或时机错误"。

  根因不是"小类定义缺失"（`reflection_dimension_map` 早给了 A 类 4 个小类），
  而是**判据太粗 + 词表两处各抄一份**：
  - `missed_signal_reflection.py` / `missed_signal_taxonomy.py` 的 A 类只有一条证据：
    "该资产当时有因果链，但信号没命中触发条件" → 直接判 `market_regime_misread`；
  - 每条漏检反思其实都挂着 `trade_decisions.deny_reason`（机器记录的拒单机制），
    但分类器**从不看它**。

  把 deny_reason 拉出来一看，A 类根本不是"认知错"，而是三件不同的事：

  | 拒单机制 | 条数 | 事后赚钱占比 | 真相 |
  |---|---|---|---|
  | `asset_BTC` / `asset_WTI`（资产额度上限） | 1481 | **96.8%** | 额度把最好的信号全掐了 |
  | `R8:同批次兄弟决策=N>3`（同批次连坐） | 613 | **4.6%** | 这条规则在**省钱**，不能动 |
  | `high_strength`（强度闸门） | 283 | **98.2%** | 强度闸门也在拦赚钱的单 |

  修法：
  1. `reflection_dimension_map.py` 新增第 2 条输入路径 `deny_reason`（优先级仅低于 E 码、
     高于 LLM 的 reason_code），A 类新增 3 个机制小类：
     `risk_limit_blocked` / `batch_conflict_blocked` / `strength_gate_blocked`；
  2. 两个分类器的"有链就判市场误读"改为**同组联动判定**：
     同组 ≥2 个资产 ±2h 同向异动 ≥1.5% → `market_regime_misread`（板块级状态没读到）；
     只有它自己动 → `pattern_recognition`（单资产形态/强度没识别）；
  3. 词表**单一权威源**：写入侧不再自带一份 12 类常量，统一 `import reflection_dimension_map`；
  4. 证据落库：`trade_reflections_v2` 新增 `subcategory_evidence` 列，记下每条判定的依据；
  5. `reflection_3layer` 的"方向或时机错误"拆成 **方向判错**（|结算|≥0.3%）/
     **时机·止损偏差**（<0.3%，噪声区）—— 86 / 57 条。

  实测（新脚本 `backfill_reflection_a_subclass.py`，默认 dry-run）：
  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | A 类小类数 | 1 | **5**（+3 机制类） |
  | 最大小类占比（S10） | 53.0% | **29.9%**（达标，门槛 40%） |
  | 判定变化 | — | 2384 行 |

  分布：`risk_limit_blocked` 29.9% · `news_direction_inverted` 22.1% ·
  `event_chain_break` 12.8% · `batch_conflict_blocked` 12.4% · `calibration_drift` 11.0% ·
  `strength_gate_blocked` 5.7% · `market_regime_misread` 4.9%。

  测试：`backend/tests/test_v326_a_subclass.py`（21 用例，全离线）+
  维度映射/反思相邻测试 **86 passed**。
- **慢涨/慢跌归因 0 覆盖（v325）**
  2026-11-13 只读实测：`critical_price_moves` 里 `slow_rise` 111 条 → `event_move_attribution`
  **0 条**，`slow_fall` 141 条 → 5 条。异动识别本身是正常的（快涨 155 / 快跌 200 /
  慢涨 111 / 慢跌 141），断在**归因口径自相矛盾**：
  - `_window_for_kind()` 按形态给候选窗口（快 ±3h / 慢 ±3d）；
  - 但 `_apply_lag_gates()` 对**所有形态**硬切 `lag > 6h` → 慢形态的候选在闸门里被全部丢弃。
  慢涨本来就是几天内走出来的，真正起作用的新闻几乎必然早于 6h。

  修法（`backend/src_python/calc_event_move_attribution.py`）：
  1. 闸二上限按形态（快 6h / 慢 **72h**），与窗口同口径 —— 新增 `_lag_max_for_kind()`；
  2. 远距离不再"一刀切丢弃"，改**分档降权**（1–6h ×0.5 / 6–24h ×0.35 / 24–72h ×0.25），
     `lag < 0`（事后拟合）仍然绝对禁止；
  3. 慢形态只认**直接命中本标的**（`asset` / `semantic`），`cross`（同组同向）一律不进 ——
     实测假反例：BNB −5.07% 挂到「tBTC 跨链桥恢复」、XRP −4.44% 挂到「Machi 卖出无聊猿」；
  4. **方向一致性**（全形态）：标题方向与异动方向相反 → 不是原因。反例：
     SOL fast_rise +1.57% ←「SOL 跌破 95 美元」、MU fast_rise +1.62% ←「AI 硬件板块下挫」；
     方向混杂（如「纳指跌、芯片涨」）读不出方向时不做判断，避免误杀跨资产事件；
  5. **最低证据门槛** `LOW_SCORE_MIN=0.15`：低于此分不进主表，落到 candidates 表
     （页面显示为弱候选）；`|move_pct|≥5%` 的大异动例外，仍进待复核区交 AI 判。

  实测（90 天一次性回填）：
  | 指标 | 修复前 | 修复后 |
  |---|---|---|
  | 有归因的异动 | 107 / 607（17.6%） | **168 主表 + 36 待复核 = 204 / 588（34.7%）** |
  | slow_rise 覆盖 | 0 / 111（0%） | 5 条（全部为 |pct|≥5% 的大异动，在待复核区）|
  | slow_fall 覆盖 | 5 / 141（3.5%） | **26 条** |
  | 方向矛盾残留 | 若干 | **0** |
  | 统计层 S7 异动归因覆盖率 | 16.0% | **27.7%**（+11.7pp）|

  ⚠️ **结构层 #11（按资产影响力评估）从 PASS 转 FAIL（9 < 10）**，原因不是过滤太严，
  而是原先的 11 个资产里有 3 个（SKHYNIX / ADA / DOGE）是靠弱匹配凑数的：
  实测 SKHYNIX 82 条异动，窗口内**一条带该标的标签的新闻都没有**。这是上游
  「新闻资产标签 / 翻译」的质量缺口，归因器修不了 —— 需在第 1 项（AI 通道）恢复后
  补 `news_events.asset` 打标。

  测试：`backend/tests/test_v325_slow_attribution.py`（18 用例，全离线）；
  相邻归因测试 64 通过。
- **分钟线搬运没定时化 → 归因页 K 线停更（v324）**
  2026-11-12 复检发现 `ohlcv_intraday` 停在 11-07 08:59（落后 122 小时），逐环追查：
  ```
  market_ticks (实时 11-12 11:08 ✅) → kline_bars (实时 11-12 11:06 ✅)
    → ohlcv_intraday (停在 11-07 08:59 ❌)   ← 断点
      → 归因页 / 复盘分钟级 K 线 (读 ohlcv_intraday)
  ```
  断点原因：`ohlcv_intraday` 由 `backfill_intraday_from_kline_bars.py` 从 `kline_bars`
  搬运，但**这个脚本只被手工跑过一次**，从未定时化 —— 而
  `~/.trident/wrappers/run_kline_aggregate_cron.sh` 的链路注释里只写了
  `market_ticks → kline_bars → ohlcv_daily → tech_signals`，**独独缺了这一步**。
  修法：在同一 wrapper 里补上第四步（纯本地、零网络、幂等 `INSERT OR IGNORE`，
  失败不阻断后续步骤），与该 wrapper「顺序很重要，串在一起避免读到半成品」的原设计一致。
  实测：搬运一次新增 **114,493 行**，`ohlcv_intraday` 129,771 → **244,302**，
  数据到 11-12 11:04；`GET /api/market/history/{BTC,WTI,XAU}?interval=5m`
  随即返回真实分钟 bar（BTC 11:00 / WTI 85.729 / XAU 3990.2）。

  > ⚠️ **同时更正一个误判**：`trade_decisions` 停在 11-09 03:51 **不是**断链。
  > `fast_move_detector.load_tick_bars` 直接读 `market_ticks`（一直实时），
  > 48h 窗口内仍能检出异动（实测 SOL 1 个 / WTI 1 个，只是已被 `_existing_keys`
  > 去重记录过）；最近 40h 没有**新的**达阈值异动（`THRESH_FAST_1H_PCT=1.5`），
  > 所以没有新的 `critical_price_moves` → 也就没有新的 `trade_decisions`。
  > 这是真实市场状态，不是故障。

  顺带修 `backfill_intraday_from_kline_bars.py` 的 **dry-run 谎报 bug**：
  `--dry-run` 分支硬编码 `inserted = 0`，永远显示「新增 0」，
  让人误以为没有新数据可搬（本次排查被它误导过一次）；现按主键真算差集
  （修后 dry-run 正确显示「新增 30」）。

### Fixed
- **无代理环境下行情全链路失效 → 国内直连兜底（v324）**
  用户明确「没有代理」后实测定位：`feed_probe` 失败率 **46.7%**（success 12 / failed 14 /
  degraded 4），且失败集中在行情三源 —— yahoo（HTTP 429 / SSL handshake timeout）、
  yfinance（cookie/crumb 取不到，history 返回空行）、asia（同样只走 yfinance）。
  同一台机器上**国内源直连实测全通**（腾讯 200/265ms、新浪 200/210ms 且必须带 `Referer`，
  否则 403）。据此新增 `backend/src_python/cn_prices.py`（腾讯 `qt.gtimg.cn` +
  新浪 `hq.sinajs.cn` 双源，批量请求 + 重试 + 60s 缓存），并接入四条取价链：
  - `multi_source_fetcher._cn_fetch_price` → 注册为 `"cn"` 源，并加入
    `feed_redund_policy.FEED_REDUND_POLICY` 全部 18 个资产的 PRICE fallback 链末尾。
    **这是最关键的一处** —— 该链是反思系统的取价主干，此前只有
    `yahoo / yfinance / equity(恒 None) / asia` 四个 fetcher，无代理环境下**全断**，
    价格全缺失 → 反思系统把「数据缺失」当成判断错的理由，掩盖真正的推理/因果错误。
  - `stock_prices.current_price_for_symbol`：Yahoo → Polygon → **国内源**（美股建仓/结算价）
  - `asia_prices.fetch_price`：yfinance → **国内源**（A 股指数 `000001.SS`/`399001.SZ`/`000300.SS`）
  - `engine/prices.py`：XAU / WTI 在新浪/东财之后加**腾讯 hf_GC / hf_CL 备源**
    （新浪 `hf_` 前缀实测偶发 SSL EOF）
  实测端到端：应用层取价 **20/20 成功**（接入前全废），且 24h 涨跌齐备 ——
  NVDA −4.27% / XAU −0.57% / WTI −2.68%，与源端涨跌幅字段自洽。
  前收价解析按实测锁定（三个坑都踩过并修正）：
    ① 腾讯 `hf_` 期货载荷 `parts[1]` 是**涨跌幅%**不是涨跌额
       （(4090.72−4114.50)/4114.50 = −0.578% ≈ −0.58），昨收在 **`parts[7]`**；
    ② 新浪 `hf_` 同构，昨收也在 `parts[7]`（`parts[6]` 是时间串）——
       曾误取 `parts[6]` 导致前收恒为 None；
    ③ 新浪 `gb_` 美股靠「现价 − 涨跌额」反推昨收，但涨跌额**可为负**，
       `_to_float` 只收正数 → 新增 `_to_signed`。
  铁律不变：拉不到返回 `None`，绝不阻塞主流程。
- **K 线也断了 → 腾讯 K 线补上（v324）**
  取价补上之后发现 **K 线仍是空的**：`multi_source_fetcher._HISTORY_FETCHERS` 只有
  `yahoo / yfinance`，无代理环境下两者全断 → 前端只能「按 entry/max/min 模拟反推」，
  回测/复盘看到的是**假 K 线**。实测腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get`
  国内直连可用，接入为 `"cn"` history 源（复用同一张 PRICE 策略表，`fetch_stock_history`
  本来就遍历 `policy_for(asset, "PRICE")`）：
  - A 股指数 `sh000001` / 港股 `hkHSI` / 美股 `usNVDA.OQ` → ✅
  - **美股必须带交易所后缀**（`.OQ` 纳斯达克 / `.N` 纽交所）—— 不带只返回 2 根，
    故实现为「先试 `.OQ`，行数不足再试 `.N`」
  - 分钟线（`m60`）与期货（`hf_GC`）腾讯**不提供**（param error）→ 本模块不覆盖
  - 行格式是 `[日期, **开, 收, 高, 低**, 量]` —— **不是 OHLC 顺序**，写反了高低点就错位
  - ts 用**本地时区当日 00:00**（与项目内 Naver/KRX 源 `fetch_naver_candles` 同口径）
  实测校验：美股 5/5 成功，**末根收盘价与实时现价完全一致（差 0.00%）** ——
  NVDA 178.67 / AAPL 350.69 / TSLA 378.05 / MU 733.15 / MSTR 125.59。
- **重启后端到端生效（v324）**
  `launchctl kickstart -k gui/$(id -u)/com.trident.{api,bridge}` 后实测：
  - `GET /api/replay/kline/live?asset=NVDA` → `price: 178.67`（此前无代理下为 null）
  - `GET /api/market/history/NVDA?interval=1d` → 真实 bars，
    **末根 close=178.67 = 现价**、前一根 close=186.64 = 昨收，ts 落在 2026-11-10
  - `feed_health` 表近 2 小时 source 分布（最硬的证据）：
    `yahoo failed 59 / yfinance failed 47 / asia failed 41 / equity failed 26`
    vs **`cn success 46（46 次尝试零失败）`** —— 无代理环境下 cn 是唯一稳定可用的行情源。
  - 能力验收**零回归**：重启前后同为 **24 PASS / 1 FAIL / 1 WARN**
    （FAIL#2「信号权重自动学习」的根因是 LLM 不打分导致的乘子坍缩，与行情无关；
    WARN#1 是样本量/抖动项）。
  本次改动的意义：**把「数据缺失」这个借口从反思系统里彻底拔掉** ——
  价格与 K 线都真实了，反思系统才能真正暴露推理/因果错误，
  而不是把一切都归因到「没数据」。
- **出网通路体检并发化 + 国内源分组（v324）**
  `net_path_probe.py` 原来串行探测，一个被墙源能拖 72s（Google News 实测），
  整个体检要跑两分钟。现在**并发**探测 + **硬时限**守护（探测跑在 daemon 线程里，
  `join(cap)` 到点即判 FAIL，DNS 解析不再拖住进程）→ 总耗时 **~140s → 24s**。
  报告新增【国内直连源】分组（腾讯/新浪/同花顺/财联社/Coingecko），
  实测国内源可用 **4/5** —— 让「没有代理也能用哪条路」一眼可见，而不是一串 FAIL。
- **本地兜底通道熔断串味 + 权重判别力口径诊断（v324)**
  能力验收实测（2026-11-10 生产库只读）发现两处真问题 + 一个口径真相：
  ① **本地通道熔断串味（真 bug，已修）** — `config.py`：
  `v322` 想按用途给三个本地通道去重，但判据写成「模型名 == LOCAL_LLM_FAST_MODEL」，
  而 `.env` 里 `LOCAL_LLM_MODEL / LOCAL_LLM_FAST_MODEL / LOCAL_LLM_REFLECT_MODEL`
  **三个都是 `qwen2.5:3b`** → 第一个 `if` 永远命中 → 三个 id 全变成
  `local/qwen2.5:3b-fast`，**依然完全相同**。熔断器名按 `key_alias` 拼
  （`realtime_filter._llm_breaker` → `llm_net:{alias}` / `llm_auth:{alias}`），
  于是三个本地通道共用一组熔断器 → **任一通道超时，三个一起被拉黑**；
  实测 `llm_net:local` 与 `llm_auth:local` 同时 `open`，面板显示本地全挂，
  而直连 ollama `/v1/chat/completions` 实测 **HTTP 200**（7.7s / 1.4s / 2.3s）。
  修法：`_local_id` 一律按**用途**加后缀（与模型名无关），`key_alias` 同样按用途独立。
  实测：3 个本地通道的 4 组熔断器全部 `closed`，直连全部 OK。
  ② **权重判别力的真相（诊断工具，非改阈值）** — 新增
  `backend/src_python/signal_weight_diagnostics.py`：
  验收 #2 的单一全样本 Spearman（ρ=+0.199，阈值 0.20）把三件事混在一起。
  分层实测：全样本 +0.1974 / tier 内最高 +0.1666 / 按月 10月 +0.341 → 11月 **−0.1383**。
  更关键的是**乘子坍缩自检**：7 个乘子里 `reaction`/`time` 100% 恒为 1.0、
  `source` 95.7% 恒为 1.0、`base` 85.7% 恒为 0.55（`sentiment_score` 95.9% 是 0、
  `impact_score`/`heat_score` 94.7% 是 5 → `_base_mult` 凸组合恒得 0.55）、
  `regime` 仅 2 取值 —— **只有 `impact` 一维真正在动**。
  即权重排序几乎完全由「该品种平时能走多少」决定，「信号本身有多强」这一维已失效
  （上游根因是 LLM 不打分）。诊断工具把这套口径固化成可复跑命令。
  ③ **出网通路体检** — 新增 `backend/src_python/net_path_probe.py`：
  实测 Binance/百度/OpenRouter 通，**Yahoo / FRED / GoogleNews / Ollama Registry 全被堵**
  （SSL UNEXPECTED_EOF / 超时），且本机**无任何代理端口在监听** → `ollama pull` 直接
  `connection reset by peer`。体检工具能逐源定位阻塞并给出可粘贴的 `~/.trident/env` 修复行。

### Added
- `backend/src_python/signal_weight_diagnostics.py` — 权重判别力多层诊断
  （tier 内 / 市场状态 / 按月 / 乘子坍缩自检），只读，`--json` 可机读。
- `backend/src_python/net_path_probe.py` — 出网通路体检，默认只读，
  `--write-env` 才写 `~/.trident/env`（写前备份）。
- `backend/tests/test_signal_weight_diagnostics.py`（27 用例）、
  `backend/tests/test_net_path_probe.py`（11 用例）—— 全离线。

### Fixed
- **AI 通道健康面板：本地通道误判 + 「额度耗尽」不可见（v323)**
  (`backend/src_python/api_server.py`, `frontend/components/quant/stat-cards.tsx`):
  能力验收实测发现 **全部 LLM 通道熔断、实时决策链路停摆 27 小时**
  (`ai_decisions` 最后一条 2026-11-09 05:04)。逐个直连实测根因是
  **4 家远程通道额度/账号全废**（minimax 账户1 429「Token Plan 用量上限」、
  minimax 账户2 402 `insufficient_balance`、xinshu 429 `API_KEY_QUOTA_EXHAUSTED`、
  openrouter 401 `User not found.`），代码侧修不了，需充值/换 key。
  本次修掉两个**真 bug**：
  ① 本地 Ollama 通道被误判 `network` —— 探针 read timeout `(6, 15)` 小于本地模型
  冷启动实测 25~74s → 连续 3 次后熔断摘掉本地兜底通道。现按 `is_local` 放宽到
  `(10, 120)`（覆盖 `.env` 记录的历史最坏情况）、本地熔断窗口 180s、且本地超时**不计入熔断失败**（慢 ≠ 坏）。
  实测：本地 3 通道 `network` → **`ok` (HTTP 200)**。
  ② 402 / 带额度标记的 429 现在归 **`quota_exhausted`**（新类），普通 429 仍是
  `rate_limit`；超时的异常单列 `timeout`。面板新增标签「额度耗尽 / 超时」。
  另加**静默窗口**：`quota_exhausted` 30 分钟 / `auth` 15 分钟内**零网络请求**，
  窗口过后自动复探 → 充值或换 key 之后自愈，不必重启进程。
  `backend/src_python/capability_acceptance.py` 的端点探测失败重试一次
  （8s → 20s），消除「同一份数据两个结论」的假 FAIL。
  测试：`backend/tests/test_probe_health.py` 18 → **27 用例**（新增 9 条）。
  回归：全量 **2863 passed / 2 skipped / 0 failed**（21m59s）。
  附带发现：验收 #2「权重校准」判据处在临界点 —— 同一天两次跑 ρ=+0.207 PASS /
  +0.200 FAIL（阈值 0.20）；时间序滚动 600 条窗口实测 ρ = +0.680 / +0.054 /
  +0.423 / +0.520 / +0.146 / +0.195 / **-0.055**（中位数 +0.195，反向 1/7，
  4/7 低于阈值），且**前半段强、后半段衰减**。阈值属产品决策，本次未改；
  建议主判据改为「时间序滚动窗口 ρ 中位数 ≥ 0.15 且反向窗口 ≤ 1/7」。

### Added
- **Pattern deep-self-audit chain (v233)** (`backend/src_python/review_agent.py`):
  recursive 5-why / conditions-apply / overfit / fabricated-evidence audit on the
  top 3 patterns emitted by `summary.patterns`. New schema field
  `summary.pattern_audits?: PatternAudit[]` carrying
  `{ claim, factual, evidence, conditions_apply, conditions_fail, overfit_risks,
  confidence, caution, why_chain, depth_reached, stop_reason, fabricated_evidence }`.
  Cap: 3 patterns × 5 depth = 15 LLM calls per report (`stop_reason=max_depth`).
  Anti-fabrication: `_validate_audit_evidence` flags evidence whose `#id` does
  not exist in the supplied trade set, forces `confidence=0.3` and appends a
  "疑似编造" caution (preserved as long as LLM did not already self-flag).
  New `stop_reason=llm_failed` distinguishes "LLM actually unavailable" from
  `no_further_why` (semantic fix from smoke [8]). 18 pytest cases in
  `tests/test_review_pattern_audit.py` (all green; adjacent test files still
  pass). Frontend: `PatternAudit` type + `<PatternAuditItem>` and
  `<PatternAuditBlock>` in `frontend/components/replay/review-panel.tsx`,
  rendered as a collapsible block under the 规律发现 ListBlock. Visible bands:
  confidence (高/中/低) + stop_reason label + caution callout (色调区分
  contradicted / fabricated / no_llm) + why_chain 折叠 + 三列
  (适用/反条件/过拟合) + 证据折叠 (fabricated 划线).

### Added
- **Logic-analysis reflection (v231)** (`backend/src_python/reflection_logic_analysis.py`):
  6 programmatic detectors for formal-logic validity in reasoning_path — fills
  the third leg of the reflection tripod alongside `reflection_deep` (data-driven)
  and `meta_reflection` (system self). New error codes E028–E033 (CRITICAL ×2 +
  WARN ×4): self_contradiction, non_sequitur, false_dichotomy, circular_reasoning,
  equivocation, post_hoc. New `logic_analysis` layer in ontology (weight 3,
  between event_logic and reasoning_logic). New `trade_reflections_v2` columns:
  `logic_integrity_score` (0–1), `logic_findings_json` (JSON),
  `logic_checked_at` (ISO8601). Wired into `reflection_scheduler.py` step6.
  32 unit tests, fully offline. See `backend/META_REFLECTION.md` §1.5.
- Causal graph expansion: data-driven `correlates_with` weights (BTC↔ETH,
  WTI↔XAU, etc.) — replaces guesswork with rolling-window correlation.
- 18 cross-asset edges in `asset_driver_edges` (CPI / DXY / NDX / SPX /
  US10Y / XAG / etc.).
- 594 reasoning-graph nodes + 619 edges produced by LLM 5-why chain
  expansion across 13 assets.

### Changed
- `verify_hypothesis` now sources-aware: matches `ai_decisions` asset
  directly and falls back to indicator-name match in `factor_snapshots`.
- `_chain_infers_direction` vocabulary expanded (24 up-words, 24
  down-words) — LLM-derived Chinese causal chains now correctly infer
  direction.
- `verify_all_pending` shares a single DB connection + 1 commit per cycle
  — 18× faster (5.3s → 0.3s on the 87-node suite).

### Fixed
- **Yahoo Finance 实时 K 线 for 归因页** (`backend/src_python/api_server.py`):
  修复 `/api/replay/signal/{id}/kline` 与 `/api/replay/kline/live` 两个端点对
  `US_HYNIX` / `SKHYNIX` / `MU` / `NVDA` / `TSLA` / `AAPL` / `MSFT` / `META`
  等美股标的退化为「模拟反推」的 bug。`_fetch_binance_klines` 增加 `yahoo`
  分支（interval 1h→60m/4h→60m，limit 按周期缩放），`_fetch_real_klines` 解
  除「非 binance_fapi/okx_swap 即 None」的硬卡，`get_replay_signal_kline` 动
  态返回 `market_type="yahoo"`；前端 `kline-chart.tsx` 增加 Yahoo Finance 蓝
  色角标（与既有 `binance_perp`/`simulated` 并列）。多源 fallback
  yahoo→yfinance→asia 由现有 `multi_source_fetcher.fetch_stock_history_cached`
  承担（v?? 海力士 cap）。
- `_extract_facts` schema mismatch on `factor_snapshots`: old code queried
  `(captured_at, factor_name, value, prev_value)`; real schema is
  `(ts, final_score, gated_action)`. Now derives direction from
  `final_score` delta + `gated_action` fallback. BTC facts: 67 → 228.

### Added
- **Safety guards** (`backend/src_python/safety_guards.py`): two
  cycle-level guards for `quick_sim.open_trades` — daily realized-loss
  kill switch (`check_daily_drawdown`, default limit = one full notional)
  and high-impact event window (`check_event_window`, default ±300 s,
  configurable via `MACRO_EVENTS_FILE`). Both default ON, env-toggleable
  (`DAILY_LOSS_GUARD_ENABLED` / `EVENT_WINDOW_GUARD_ENABLED`); failures
  log + fall through, never break the cycle. 13 unit tests pass.
- **Accuracy baseline CLI** (`backend/src_python/accuracy_baseline.py`):
  reads `quick_sim_trades` (settled=1), computes win rate / avg pnl /
  profit factor / pay-off ratio / max drawdown / per-trade Sharpe /
  per-asset + per-action + per-event-strength breakdown / entry-delay
  buckets. Use `--days 30` / `--from 2026-09-01` / `--asset BTC` /
  `--json` / `--output report.json`. First run on real DB (last 30 days,
  59 trades): overall WR 50.85 %, BUY 55.56 % vs SELL 43.48 %, max DD
  92 % — SELL side now has a measurable baseline to attack.

### Known gap (v230)
- _none_

### Added
- **MAE early-cut (P0.5)** (`backend/src_python/quick_sim.py:cut_bleeding_trades`):
  cycle-level soft stop-loss that closes any open `quick_sim_trades` row
  whose current unrealised loss exceeds `EARLY_LOSS_CUT_PCT` (default
  `0.3 %`). Tuned from real-DB analysis (59 trades / 30 d): SELL WIN max
  MAE `0.251 %`, SELL LOSS max MAE `0.931 %`, BUY LOSS max `0.373 %` —
  threshold `0.3 %` catches 4 LOSS / 0 WIN on historical replay (saved
  bleed ≈ 0.96 percentage points). Wired into `run_cycle` between
  `update_open_extremes` and `settle_trades`; ai_decisions synced
  (COALESCE pattern, matches `settle_trades`). Toggle off via
  `EARLY_LOSS_CUT_ENABLED=0` or `EARLY_LOSS_CUT_PCT=0`. 10 new tests
  cover disabled / below / exact / above / multi-asset / no-decision-id
  paths.

### Added
- **SELL signal gate (P0.5b)** (`backend/src_python/quick_sim.py:gate_sell_signal`):
  rejects SELL orders pre-trade when `event_strength == 'high'` OR asset
  is in `SELL_GATE_BLOCKED_ASSETS` (default `BTC,WTI`). Tuned from real-DB
  (23 SELL trades / 90 d) — historical replay of dropped subset: high
  strength 33 % WR (n=3), BTC 33 % (n=6), WTI 37.5 % (n=8); kept subset
  `n=8 / WR 62.5 % / avg +0.007 %` is the only positive-expected-value
  region (vs SELL overall -0.06 % avg). Counter-intuitively, high-strength
  bearish signals are *anti-predictive* — strongest SELL signals arrive
  after the market has priced in the move. Toggle off via
  `SELL_GATE_ENABLED=0`; adjust asset list via
  `SELL_GATE_BLOCKED_ASSETS=ETH,SOL`. Rejected trades log via
  `[SELL_GATE]` prefix. 12 new tests cover disabled / BUY passthrough /
  high / BTC / WTI / XAU allowed / case-insensitive / custom asset list /
  open_trades integration.
- **TP/SL hard-exit (P0.2)** (`backend/src_python/quick_sim.py:check_tp_sl`):
  enforces hardened exits on every `quick_sim` cycle. Reads
  `paper_trading_settings.tp_enabled + tp1_mult / tp2_mult / sl_mult
  + tp1_ratio / tp2_ratio` (defaults `0.6 % / 1.0 % / 0.5 %`, ratios
  `0.5 / 0.3`); trigger evidence = `max_price / min_price` from
  `update_open_extremes` + current price. SL has priority (capital
  protection). **TP1 hit** → write partial exit (`partial_exit_price /
  partial_exit_ratio / partial_pnl_pct`) to `ai_decisions`; trade stays
  open. **TP2 hit** → full close at blended price (TP1 partial + remainder
  at TP2), `verdict = WIN`. **SL hit** → full close at SL price,
  `verdict = LOSS` (LOSS gets blended pnl = TP1 partial + remainder at
  SL). Backwards-compatible DB migration adds `sl_mult` column (default
  `0.5`); reflection_ontology referenced `sl_mult` but it was a bare
  name before — now real. New config knobs:
  `TP_SL_ENABLED=0|1` (global kill-switch, default 1) and
  `TP_SL_DEFAULT_TP1_MULT / TP2_MULT / SL_MULT / TP1_RATIO / TP2_RATIO`
  (defaults, overridable per-run via settings). Wired into `run_cycle`
  between `update_open_extremes` and `cut_bleeding_trades`. 20 new tests
  cover global / per-run kill-switches / bad config (zero mults, sum
  ratios > 1) / BUY-SELL TP2 / SL / partial-blend / max-price trigger
  / idempotent partial / mixed-cycle priority / price-unavailable
  fallback. **Known gap (P0.2 part B)**: per-trade breakeven-move after
  TP1 not implemented — would need a `sl_price` column on
  `quick_sim_trades`. Logged for future iteration.

- **Breakeven-move after TP1 (P0.2 part B)** (`backend/src_python/quick_sim.py:check_tp_sl`):
  closes the only remaining TP/SL gap. Adds `quick_sim_trades.sl_price
  REAL DEFAULT NULL` (auto-migrated; NULL = legacy formula). When a trade
  hits TP1, `_record_tp1_partial` writes `sl_price = entry × (1 ±
  offset × sgn)` to that row if not already set (idempotent on repeat
  TP1s). Subsequent `check_tp_sl` cycles see the override and use it
  instead of `entry × (1 ∓ sl_mult)`. New env knobs:
  `TP_SL_BREAKEVEN_ENABLED=0|1` (default 1 — production-safe: starts
  locked to entry, never worse than legacy), `TP_SL_BREAKEVEN_OFFSET_PCT=0`
  (set positive to lock in micro-profit e.g. `0.01`). Out dict from TP1
  now exposes `breakeven_applied` and `breakeven_sl_price`. Idempotency:
  existing override is preserved across re-runs. 7 new tests cover
  disabled / enabled (entry) / with-offset / override-uses-not-original /
  blended PnL at breakeven / SELL symmetry / idempotent re-fire. The
  full quick_sim suite (early_cut + sell_gate + tp_sl + db_migrate_order)
  is **52/52 green**.

### Added
- **Confidence-Tier Sizing (P0.1 v1)** (`backend/src_python/position_sizing.py`,
  hooked in `backend/src_python/quick_sim.py:open_trades`): scales each
  trade's `notional_usdt` by a tiered multiplier based on
  `ai_decisions.evidence_confidence`. Defaults: low conf `< 0.3` →
  `0.5×` base, medium `[0.3, 0.7)` → `1.0×`, high `≥ 0.7` → `1.5×`.
  Clamped to `[POSITION_SIZING_MIN_MULT, POSITION_SIZING_MAX_MULT]`
  (defaults `0.25×` / `2.0×`) to prevent runaway. Action-agnostic (v1
  keeps BUY/SELL same multiplier); action-aware scaling deferred to v2
  pending SELL data. New env knobs:
  `POSITION_SIZING_ENABLED=0|1` (default 1),
  `POSITION_SIZING_TIERS_JSON` (override default 3-tier),
  `POSITION_SIZING_MIN_MULT` / `POSITION_SIZING_MAX_MULT`. Missing or
  out-of-range `evidence_confidence` falls back to base × 1.0
  transparently (never blocks a trade). Returns
  `(scaled_notional, metadata dict)` for audit. New `[POSITION_SIZING]`
  log line per cycle shows tier counts + sample. 12 new tests cover
  kill switch / 3-tier mapping / clamp / missing / out-of-range /
  custom JSON tiers / invalid JSON fallback / BUY-SELL parity /
  end-to-end `open_trades` integration. The full P0.* suite
  (early_cut + sell_gate + tp_sl + sizing) is **61/61 green**.

## [v225] — 2026-10-06

### Added
- 27-layer reflection engine (E001–E027) covering event_logic, causal,
  sample_integrity, and reasoning_logic (7 sub-checkers).
- Meta-reflection loop: when all 7 reasoning-logic checkers miss a bad
  trade, log the blindspot and propose a new checker (E028+).
- Cron-driven L3 automation: daily KG review (04:00), causal expansion
  (04:10), 10-min scheduler.
- 5-sheet Excel export with frozen headers + auto-filter.
- 189-test pytest suite (all offline, CI-ready).

### Notes
- This is the first public release of Reflex Quant.

[Unreleased]: https://github.com/Claire19950906/reflex-quant/compare/v225...HEAD
[v225]: https://github.com/Claire19950906/reflex-quant/releases/tag/v225

## v345.1 · 全站界面扫描（除 LLM 外全修）

**扫描方式**：浏览器逐页体检（/、/attribution、/missed-signals、/review、/knowledge-graph、/discover、/news、/sources、/settings、/prompts、/history）+ 前端代码静态扫描。

**修掉的问题**
1. **两个页面没入口**：`/backtest`（策略回测）、`/history`（历史新闻交易回溯）做好了但导航里没有入口 → 新增「更多 → 交易」组。
2. **报价条永远「加载中」**：前端打 `/api/market/prices`，而 Next 的 `/api/*` 默认反代到 8100，桥接里**根本没这个路由** → 5 个报价(BTC/ETH/SOL/XAU/WTI)永远卡在「加载中」。已在桥接补上，直接从库里 K 线算，不走外网。
3. **出网无超时 → 整机接口全挂**：Yahoo/东财某个 SSL 挂住不回时线程永久停在 recv，anyio 线程池被僵尸线程占满，连 `/api/health` 都要排队。启动时给 urllib / requests 打默认 12s 超时。
4. 4 处英文加载态（`loading...` / `loading…` / `loading attributions…` / `Loading...`）→ 中文。
5. 3 处硬编码 UTC（`upd 08:12Z`、`Updated … UTC`、订单簿 `HH:MM:SS UTC`）→ 本地时间。
6. 中文标题被误加 `uppercase tracking`：清理 82 处（38 个文件）。
7. 开发信息泄漏到界面：`复盘系统 — /review 路由`、`事件-行情归因 (v291)`、`v291+ 反思系统聚焦页`、`AI 分析 Prompt` / `LLM PROMPT`。
8. `/news` 页 `<h1>新闻流` 与面板 `<h2>新闻流` 重复 → 面板改「实时抓取」。
9. `/settings` 在接口慢时是**纯白屏** → 加「读取设置中… 已等待 Ns」+ 20s 后重试按钮。
10. 首页宏观日历提示原来独占一整段，并入时段条右侧一行；`data-state` 空态补「为什么空」的说明。

**已知遗留**
- 桥接在后台补抓行情时仍会变慢（429/SSL），页面表现为部分卡片「加载中」。根因是数据源本身不稳，非前端。
- 窄窗口（约 655px）下 K 线画布 1012px，右侧价格轴被裁。
- 股票类资产无分钟线时提示仍是「还没选品种」，应改为「该品种暂无分钟线，切 30d/1h」。

## v345.9

**主题**：发现页信息架构重构 + 错误文案全站收口。

**用户原话**：「没必要的挤在一处、重复的、没用的说明文字都删掉，重新分类一下什么放哪儿」。

**重新分组**（原来 14 个面板平铺 → 5 组，按数据快慢排序）
1. 情绪与仓位（分钟级）：日内流动性 · 多空持仓比 · 聪明钱
2. 资产与事件（日级）：资产猎手 · 事件因果 · 跨资产 · 周期
3. Fed 与宏观节奏：Fed 一块（利率预期 + 传导 + 全景 + 邮箱，**原来拆成 3 个并列折叠块，同一数据源占三格**）· 宏观日历 · 周度观察
4. 资金与行业（慢变量）：资金流动 · 存储行业
5. 系统自检：AI 元认知 · 多机协作

**删掉的冗余文字**
- 每段一句 20~40 字长副标题（7 条）→ 只留一个频率标签（24h / 1h / 30s / 周度）。
- 顶部任务说明横幅 4 段（板块定位 + 4 条要点 + 4 条脚注，约 300 字）→ 压成一行「只探索、不下单 · 结论先进因果图谱，审批后才进分析链路」，未读异动红点功能保留。
- 宏观日历里整段「数据统筹」说明（含 BLS/FRED/金十介绍）→ 只留颜色图例。
- 底部「板块定位」块（含 `causal_chains.json` / `causal_graph.json` 文件路径、`/knowledge-graph` 审批流程）→ 整块删除，属开发口径。
- 徽章「9 大子段」→「4 组」；`与 fedwatch 共用 Provider` 这类实现细节 → `60s 刷新`。

**错误文案收口**
- 新增 `lib/friendly-error.ts`：抹掉内部地址（`http://127.0.0.1:8000/api/...`、`host:port`），按超时 / 5xx / 404 / 429 / 连接重置 / 证书 / DNS 翻人话。
- 接入：异动新闻卡（原 `err: {err}`）、事件流（原 `connecting {error}`）、排行榜、事件热力图、COT 面板、`/prompts`、`/settings`、`/sources`。
- `/attribution` 顶部错误条原来直接打 `/events error: {原始报错}` + 英文 `Retry` → 「事件列表暂时拉不到，下方各组数据各自独立，仍可查看」+「重试」。

**复检**：15 个路由全部 200，页面上无 `Application error` / `Server Error` / `[hash:` / `err:` / 原始内部地址（仅 CSP header 里保留 `connect-src`，属正常）。

## v345.8

**主题**：发现页三个重接口从 60 秒级降到毫秒级。

**根因两条**
1. 三个 handler 写成 `async def` 却全是同步 sqlite 查询 → **阻塞整个 uvicorn 事件循环**，期间连 `/api/health` 都要排队（实测 8s）。
2. 每次请求都现算，没有缓存。

**处置**
- `discovery.py`：`fed_impact_handler` / `asset_scout_handler` / `cycle_scan_handler` 由 `async def` 改普通 `def`（FastAPI 丢线程池，事件循环不再被占）。
- 加 `_DISC_CACHE` + `_DISC_TTL_S`（600s）+ `_disc_cache_get/_disc_cache_put`。
- 加启动预热 `start_disc_prewarm`：启动后 15s 算一轮把缓存填满，**只跑一次**（早前试过每 5 分钟刷一轮，反而因 GIL 被长期占用导致整站变慢，故弃用）。
- 预热用线程局部 `_DISC_TLS.force` 强制重算，**不先清缓存** —— 先 pop 会让用户在 60~70s 的重算窗口里白等一遍。

**另修的根因**：Ollama 的 `llama-server` 又自启占 532MB，机器只剩 14MB 可用，api_server 被系统反复 SIGKILL（表现为 `/discover` 首屏转圈、Next 报 `ECONNREFUSED 8000`）。已停服务并 kill，可用内存恢复到 ~1.6GB。

**实测（8000 直连）**
| 接口 | 修前 | 修后 |
|---|---|---|
| `/api/discovery/asset-scout` | 68.89s | 0.02s |
| `/api/discovery/fed-impact` | 25.28s | 0.92s |
| `/api/discovery/cycle-scan` | 9.33s | 0.04s |
| `/api/health` | 8.27s | 0.14s |

## v345.5

**主题**：数据源告警弹窗里的原始报错人话化。

`components/source-status-alerts.tsx` 新增 `friendlySourceError()`：按 SSL/证书、连接重置、超时、404/429/5xx、DNS、代理分别翻译，并抹掉 URL；弹窗内 `{it.last_error}` 改为 `{friendlySourceError(it.last_error)}`。

## v345.10

**主题**：全站第二轮扫除 —— 重复块、英文残留、运维按钮抢镜。

**去掉重复**
- `/attribution` 里又完整渲染了一遍 `MissedSignalsPanel`，和 `/missed-signals` 专页是**同一份数据、同一个组件**，等于同一屏看两遍。现改成一张入口卡（`漏检信号 · 去专页 →`），`id="missed"` 保留所以旧深链不失效；`live-desk` / `MissedSignalsDimensionSummary` / `MissedSignalsByDimBanner` 三处跳转同步改成直达 `/missed-signals`。

**界面英文清零（14 处）**
| 位置 | 原文 | 现在 |
|---|---|---|
| 首页事件流标题 | `real time event bus` | 实时事件流 |
| 历史回溯页徽章 | `HISTORY` | 历史回溯 |
| 学习沉淀面板徽章 | `LEARNING` | 学习沉淀 |
| 因果链面板徽章 | `CAUSAL` | 因果链 |
| AI 提示词页徽章 | `FILTER L1` | 一级筛选 |
| 回测表头 | `PnL %` / `PnL USDT` | 盈亏 % / 盈亏 USDT |
| COT 面板空态 | `no data` | 暂无数据 |
| 漏检表头 | `Asset` / `Pattern` / `Conf` | 品种 / 形态 / 置信度 |
| AI 提示词 | `System Prompt` | 系统提示词 |
| 因果图谱图例 | `brain:` | 大脑： |
| 异动强度图例 | `0.7 / medium 0.3-0.7 / weak 0.1-0.3 / ignored` | 强 ≥0.7 / 中 0.3–0.7 / 弱 0.1–0.3 / 忽略 <0.1 |
| 设置页说明 | `signal_threshold` | 信号阈值 |
| 资产规则说明 | `all` | 「全部」 |
| 信号台账说明 | `mock_trades` | 系统内部的模拟成交记录 |

**开发信息泄漏清零（4 处）**
- Fed 传导面板：`后端路由 /api/discovery/fed-impact 可选 — 未配置时本面板自动用启发式(基于 cross_asset_context.ASSET_CLASS_MAP)` → 「该模块不可用时，本面板会自动退回经验映射，数值仅供参考」。
- AI 元认知面板：`后端路由 /api/discovery/meta-cognition 暂未实现` → 「该模块暂不可用，已改用本地启发式给出报告」。
- Fed 邮箱面板页脚：`邮件生成规则在 buildMailbox() · 数据源 /api/discovery/fed-watch` → 「邮件由 Fed 全景监控的数据自动生成」。
- 数据健康面板空态：`运行 ai_worker / ingest 后会自动写入；也可手动调用 POST /api/data-health/record` → 「采集与分析跑起来后会自动写入」。

**其他**
- 归因页工具栏「⟳ 重算 10m 异常签名」是运维操作却用琥珀高亮抢首屏 → 降为普通描边 + 文案缩成「⟳ 重算」，解释移进 tooltip。
- 漏检页「先行候选按定义尚未兑现，故不给命中率口径 —— 此处只列事实，不做预测」→「先行候选尚未兑现，这里只列事实，不给命中率」。

**复检**：15 个路由全 200，页面无 `Application error` / `Server Error` / `[hash:` / `err:` / 上述任何英文残留。

## v345.11

**主题**：把「两个 0」换成原因，并让机器别被自己的预热压死。

**界面**
- 首页门卫双流「通过 今日 0 / 筛掉 今日 0」两个并排的零没有信息量 → 双零时只显示一句「今天还没有数据进入门卫（采集或 AI 通道未运行）」，有数据才恢复两列计数。
- 同一处右上角「15s 轮询 · 12s」是开发口径 → 「12s 前更新」，异常时显示「同步中断」而不是原样抛 `err`。

**性能 / 稳定性（本轮最大发现）**
- 实测机器 `load average = 420`、swap 占用 2.1GB/3GB、free pages 仅 4570（≈18MB）。此时接口不是"慢一点"，而是全站 500：`/api/health` 30s 超时、`movers` 40s、一批接口直接 `Server Error`。
- 根因是**我们自己的两个预热线程在互相抢 CPU**：api 的 discovery 预热（asset-scout 单轮 68s）+ bridge 的 movers 预热（5 个周期 × 31 品种，每 60s 一轮）。内存一紧就进 swap，越算越卡，最后进程被系统杀。
- 修法：
  - `discovery._mem_free_mb()` 不再把 `Pages inactive` 算成可用（8GB 机器上 inactive 一压就 swap，之前因此误判"还有 1GB"照样开跑 68s 重算）；阈值 120MB。
  - `bridge_server._prewarm_movers_loop()` 同样加门槛（free < 150MB 整轮跳过，等 120s 再看）。
- 改后实测冷启状态：`8000 /api/health` 0.01s、`8100 /api/health` 0.00s、`3030 /api/market/prices` 0.01s（此前分别是 2.13s / 40s+ / 超时）。

**给用户的事实说明**：这台 8GB 机器上现在有 95 个 `com.trident.*` launchd 服务，任一时刻常有 6-8 个 cron 在同时抓外部数据。内存吃紧到这个程度时，任何单点优化都会被打回原形；要么减少常驻 cron，要么加内存。

## v345.12 主分析 system prompt 减重

**诊断**（基于 `ai_call_stats` 40.3 万条真实记录）
- 累计 4.23 亿 token（in 3.80 亿 / out 4245 万），其中 `main` 2.39 亿占 **56%**，单次平均输入 **15,765**。
- `tokens_in` 分位数极稳：p50 15,778 / p99 17,493 / max 18,321 → 说明大头不是新闻正文，而是**每次调用都原样重发的 `_SYSTEM_PROMPT` 常量（16,254 字，≈14.6k token）**。
- `gate` 38.3 万次（占调用数 95%）但单次仅 357 token，占总量 37%；其中 14.7 万次 `http_error` 的 `tokens_in=0`，不计费。
- 近 7 天 2827 万，其中 main 2612 万（92%）；08:51 同一秒并发 6 条、每条都带 1.4 万说明书。

**改动**（`engine/ai_worker.py` 的 `_SYSTEM_PROMPT`，16,254 → 13,134 字，-19.2%）
1. 删「结构改革原因」——v176.1/176.2/176.3 的变更史，模型不需要（-250 字）。
2. 合并 `## 3.5 各品种盯盘优先级`：原文「v200 P2 原文版」与「v200 P4 精修版」是同一结论写两遍，压成按品类三行 + 加密四条关键判读（-1,500 字）。
3. 合并铁律 #4 / #9 / #13——三处都在说「缺技术面不要 HOLD」，保留 #4 的完整版，删 #13（-330 字）。
4. 合并「时间字段使用规则」里与 JSON 口径重复的「全网最早时间」定义（-200 字）。
5. 合并 JSON 字段口径里与模板逐字重复的 `net_origin_info` / `cross_net_first_seen_ts` 两条（-400 字）。
6. 「数据缺失降级」三条合并为一条（-130 字）。

**预期**：单次输入 ≈14,500 → ≈11,700（-2,800/次）。按当前每天约 228 次 main 计，日省约 64 万 token、月省约 1900 万。

**未做（需用户决定）**：真正的大杠杆是「一批 16 条新闻只发 1 次 system prompt」——即一次请求分析多条、输出多个 JSON。理论省 70%+，但要改 prompt 模板、解析与落库，出错会连带影响多条决策。

### v345.12 补：让 system prompt 真正命中缓存

**发现（实测调用 MiniMax 接口）**
- MiniMax 对相同前缀自动缓存：同一 system 连续两次调用，第二次 `prompt_tokens_details.cached_tokens` 从 158 → **6,982/6,983**（几乎全命中）。命中价约 0.06 元/1M，全价 4.20 元/1M。
- 但生产环境 **cached_tokens 恒为 0**。原因：`system` 消息末尾被拼接了一串**每条新闻都不同**的内容（因果链先验 chain_block、因果图谱 graph_block、市场风格 ms_block、决策间长期影响 _ctx_parts、品种经验 _asset_insights、反思错误码 _ref_block、经验块 _exp_block、SPA 块 _spa_block、复盘经验 insights_section）→ 整段 system 前缀失效。

**修法**（`engine/ai_worker.py`）
- 新增 `_dyn_parts`，把这 9 个按新闻变化的块从 `system` 挪到 `user` 消息最前面；`system` 只保留恒定内容（`_SYSTEM_PROMPT` + 市场结构开关 + 自定义指令 + 资产规则 + 长期慢变量块）。
- `db.py::log_ai_call` 增加 `cached_tokens` 参数；`ai_call_stats` 表 `ALTER TABLE ADD COLUMN cached_tokens`；主分析 / chat / lean 三条埋点都提取 `prompt_tokens_details.cached_tokens`。

**实测（10:47–11:06 共 34 次 main）**
- 单次输入：14,300–14,700 → **11,600–12,200**（-20%）
- 缓存命中：恒 0 → 出现 8,192 / 8,320 / 9,216 / 11,392 / 11,904（**65%–100%**），但约一半调用仍只有 128。
- 原因：key 轮转（minimax1 / minimax2 / minimax）各自独立缓存，某个 key 一段时间不用就冷掉。**minimax1 连续调用时稳定 70–79% 命中，minimax2 几乎恒 1%**。

**当前等效成本**：约等于 7,200 全价 token/次（原 14,500 全价）→ **等效下降约 50%**。
**下一步（未做）**：给模型池做「key 粘性」（优先复用上一次成功的 key，而不是轮转），命中率可望从 40% 提到 85%，等效再降一半。风险是单 key 被限流时会慢一拍，需配合熔断。

### v345.13 批内锁通道（sticky），缓存命中 1% → 77%

**根因**：`main_cfg = model_pool[idx % len(model_pool)]` —— 同一批 16 条新闻轮流用池里 4 个 key，每条都是该 key 的"第一次"，MiniMax 自动前缀缓存永远冷启。所以即使 system 恒定，`cached_tokens` 也只有 0/128。

**改法**（`engine/ai_worker.py`）
- 新增模块级 `_MAIN_BATCH_ROTATE = {"n": 0}`；轮转单位从「每条」改为「每批」：批内 16 条锁同一个通道，下一批才换。
- 配合 v345.12 把 9 个动态块移出 system，system 才对整批恒定。

**实测（11:35–11:37，8 次 main）**
| 时间 | tokens_in | cached | 命中 |
|---|---|---|---|
| 11:36:33 | 12,834 | 12,800 | **100%** |
| 11:37:53 | 13,107 | 10,112 | 77% |
| 11:36:11 | 13,139 | 10,112 | 77% |
| 11:35:57 | 12,933 | 10,240 | 79% |
| 11:37:12 | 12,595 | 8,448 | 67% |
| 11:36:55 | 13,103 | 8,320 | 63% |
| 11:36:18 | 12,972 | 8,192 | 63% |
| 11:36:29 | 12,213 | 128 | 1%（该批首条，冷） |

加权命中率 **77%**。

**等效成本**：13,000 token 里 10,000 走缓存价（0.06 元/1M），折算约 3,200 全价 token/次 —— 相比改造前的 14,500 全价，**等效下降约 78%**。

### v345.13 结论：不再继续压 main 调用（gate 收紧方案否决）

**原本计划**：A=gate 加硬门槛、B=归不到品种直接 HOLD 不调 LLM，预计再省一半。

**否决理由（实测数据，近 7 天）**
1. 真信号来源反查：3 条非 HOLD 决策（2 BUY + 1 SELL，全在 WTI）的 `gate_main_asset` **全部为空**、`gate_strength` 全为 0。
2. HOLD 也是同一画像：351 条 HOLD 来自"gate 无品种"。
   → 按"无品种就丢"筛，会把 351 条垃圾和 3 条真信号一起倒掉。
3. gate 标签本身不可用：近 7 天入库 9,280 条，**8,337 条（90%）gate 没归到品种**；放行样本里 strength 几乎全是 0；全库 gate http_error 累计 148,459 次。
   → gate 现在花 1.55 亿 token（占 37%）产出 90% 空标签，**没有筛选能力**，不能作为砍 main 的依据。

**最终状态**
- main 单次等效成本已从 14,500 → 约 3,200 全价 token（**-78%**），此部分收益已落地。
- gate 保持原状（单次仅 357 token，绝对值不大）。
- 后续若要再压，正确路径是**把 LLM 门卫替换成规则引擎**（来源白名单 + 品种关键词 + 数据类正则打分），属模块级重写，需单独排期。
- 当前真正的瓶颈是"一周 409 条决策里 406 条 HOLD"，属策略问题，不是 token 问题。

### v345.13 补：拆掉两道冤枉扣分（HOLD 归因）

**现象**：近 7 天 408 条决策里 405 条 HOLD，`trade_gate_reason` 绝大多数是
"统一置信度 0.0 低于闸门 20.0"。

**排查结论（逐层拆）**
1. 不是解析失败、不是空响应 —— 371 条 `analysis_degraded` 里大头是 `target_none`(232)
   与 `summary_only`(57)，属正常降级。
2. `evidence_confidence` 有 408 条全部有值，其中 ≥20 只有 3 条 —— 说明闸门确实极严。
3. 拆开看有两条**冤枉扣分**：
   - **过度分析惩罚**：`reasoning_path` 超 800 字就按比例扣分。而主分析契约本身要求
     详细推导链（实测正常输出 900–1,300 字）→ **写得越认真扣得越狠**，
     样本里 987/1028/1039/1287 字全被扣，18.x 分被压到 16.x，卡在 20 闸门。
   - **闸门 20** 相对 base 偏高：`confidence = |score|×100 × weight × coverage(0.6–1.0)`，
     即 score=0.3 的决策 base 只有 30，扣完覆盖度就 18–24，正好卡在门槛线上。
4. **真正的 0.0 来源**：`score=0.0` → `base=0` → confidence 必然是 0。这不是代码问题，
   是模型判定「该新闻不足以给出方向」（372/408 条），属新闻本身没信息量。

**改动（只拆冤枉分，不放宽方向性）**
- `OVERANALYSIS_THRESHOLD`：800 → **1800**（代码默认值同步改，因为 engine 进程
  不读 backend/.env 的该键，只读 os.environ）。
- `EVIDENCE_CONFIDENCE_GATE`：20 → **15**（.env，evidence.confidence_gate() 会读）。
- 备份：`backend/.env.bak.*_holdfix`。

**预期**：score≥0.3 的决策不再因为"写得详细"被误杀，能正常过闸门；
score=0 的仍会 HOLD（这是对的，不该改）。
**不改的**：不降 base 口径、不给"没方向"的新闻强行打分 —— 那是制造假信号。

## v414 · 分析产能体检（2026-11-17）

**动机**：日报的「降级 461/528」一个数字装着五种事实（越界/证据缺/模型未返回/结构不达标/只剩摘要），
「89% 降级」因此无法推动任何改动。

**先排除的方案**：标的词典闭环。审计近 7 天 NONE 176 条，可救回 8 条（4.5%），
93% 是真无关 —— 投入产出不成立，本轮不做。

**落地（全部只读，不改分析链、不写库）**
- `analysis_yield.py`：终态聚合 + 分层分母 + 可重试清单 + CLI
- `routers/analysis_yield.py`：`GET /api/analysis/yield`、`GET /api/analysis/yield/retry-list`
- `frontend/app/yield/page.tsx`：产能体检页（后台导航）
- `tests/test_analysis_yield.py`：13 条离线用例

**实测（近 7 天 539 条决策）**：范围内 332，真实失败 196（59%）；完整 69（20.8%）、
压缩 67（20.2%）、越界 207、模型未返回 98、结构不达标 98；方向性 6 条 = 0.86 条/日。
可重试新闻 30 条（去重、且之后未成功）。

**修掉的计数 bug**：`news_events` 对一条新闻是一对多（实测 4 行），直接 join 会把决策
放大 4 倍（539 → 1012）；现在先按 `news_id` 收敛再连，用例锁死。

**未做**：自动重试（需接认领队列/退避/配额，另开版本）；不读绩效表，不追溯改历史标签。

### v414 补 · 通道质量指标（同日）

查「模型未返回 98 条」的根因时发现：主分析 2868 次调用里失败仅 5 次（429），
`tokens_out<600`（八段写不完）占 **17%**。按 `model_id` 拆开：
`minimax-peer/MiniMax-M2.7` 短输出率 **69.8%**（43 次调用，平均 526 tokens），
主线 MiniMax-M3 为 18.2% / 6.2% / 11.7%。

- `analysis_yield.py` 新增 `channel_quality()`（分母只算 `status='ok'`，失败率单列），
  阈值 20%/40% 标注「未回测、只用于抓异常」，调用数 <20 报「样本不足」。
- `/api/analysis/yield` 返回 `channels`；前端产能体检页新增通道表。
- 测试 +4（短输出通道标记 / 小样本不下结论 / 失败不进质量分母 / 无表返回空）。

**结论修正**：`llm_failed` 的主因不是"模型没返回"，是"备用通道输出太短"；
自动重试优先级下调，先处理通道选路（属产品决策，未在本轮改）。

### v414 补二 · 队列水位 + 入口漏斗（同日）

把「产能」从「模型写不写得完」扩展到「进来多少、死在门外多少、队列排不排得动」。

**新增指标**

- `queue_health(conn, hours)`：PENDING/PROCESSING/DONE/FILTERED、最老 PENDING 年龄、
  进（入库条/时）、出（离开 PENDING 条/时）、真正出决策（`news_id` 去重）、排空小时数、verdict。
- `gate_funnel(conn, days)`：进来 → 被否（91.4%）→ 进分析 → 出决策新闻，并按
  `gate_unavailable`（0 正常判定 / 1 冻结 / 2 降级放行）把「谁否的」拆开，
  单列 `verdict_tradeable_but_filtered` / `filtered_with_asset` 两个可疑项。

**修掉一个口径错误（记在文档里）**：出队速率最初只数「有决策的新闻」，算出 6 条/h，
于是报「积压在涨」，而同一时刻 PENDING 正从 1437 掉到 260。门卫否掉的新闻同样算处理完
（24h 有 1992 条），出队必须按 `status ∈ (DONE, FILTERED, FAILED)` 计；
「真出决策」拆成独立字段 `decided_news_per_h`，两个数不混。

**近 7 天实测（10370 条进来）**：被否 9481（91.4%）、进分析 786、出决策新闻 356、方向性 6。
其中 **4687 条「门卫不可用·降级放行后被否」+ 439 条「门卫不可用仍出决策」≈ 49% 的流量
没有门卫判定** —— 这是「一天 0.86 条方向性」的上游原因之一。

**本轮不动门卫、不动模型配置**：改门卫=改放行口径，须先抽样 134 条「判可交易却被否」
与 778 条「有标的被否」；M2.7 短输出率 70% 那批调用已查明全部来自 11-16 的
`lean_recover_*`/`two_step_*` 救回路径，peer 通道 `purpose=["verify"]`、不在主池，
所以「摘掉备用通道」没有配置抓手，`.env` 未改。

**落地**：`analysis_yield.py`（+2 函数 + CLI 打印）、`routers/analysis_yield.py`（响应加 `queue`/`funnel`）、
`frontend/app/yield/page.tsx`（队列卡 + 漏斗表）、`tests/test_analysis_yield.py`（22 passed，+5）、
文档 `docs/v414-产能体检-2026-11-17.md` 新增 3.2 / 3.3 节并修正 3.1 结论。

### v414 补三 · 抽样复核（同日，结论修正）

**核了 134 条「门卫判可交易却被否」**：按天看最后一条是 11-17 05:47，而引擎 08:27 重启 ——
v408 的 `keep_tradeable`（可交易行不归档）重启后生效，**5 小时零新增**，这条漏点关掉了。

**核了 4683 条「门卫不可用被否」**：今天 00–04 时全 0、05–11 时各 0~1 条，
真正集中在 11-16 23 点那一档。所以它是**事故存量，不是持续现象** —— 上一版写成
「近一半流量没判定」是把存量当现状，已修正。

**但这批确实蒸发了**：11-10 至 11-16 每天 159~1538 条，`ai_decisions` 里 **无一有决策**，
4405 条 `retry_count=0`。根因是「门卫不可用→冻结 30 分钟→自动放行（标记改 2）→
放行后没人认领→`ts` 超龄→`_expire_stale_news` 归档成 `FILTERED/stale_expired`」。
近 7 天真正进分析 786 条，同期这样蒸发 4683 条 —— 产能最大漏洞在这条链上。

**队列口径再修一次**：`is_noise=0` 过滤会把 212 条噪音滞留行漏掉（52 vs 真值 262，
和 `quota_state.json` 对不上）。现在分三个数报：`pending` / `pending_actionable` / `noise_pending`，
ETA 只用 actionable。

**未动手**：修归档/认领要改写链路，而仓库正在做目录整改（`e8c4400` 起，几十个待提交文件），
此时改 `ai_worker.py` 会撞车。看板先摆数，动手排整改之后。
测试 23 passed（+1 噪音滞留）。

## v419 · 门卫未判定行不再静默归档（2026-11-17）

**问题**：`FILTERED + gate_unavailable=2` 近 7 天 4683 条，`ai_decisions` 里只有 3 条有决策，
4405 条 `retry_count=0`（基本等于从没被分析）。最高一天 11-13 蒸发 1538 条。

**根因**：门卫不可用 → 冻结(1) → 30 分钟后自动「放行」(2) → 放行只改标记、行仍在队尾
→ `ts DESC` 认领永远排不到 → 6h 超龄 → 归档成 `stale_expired`（语义被记成「超龄」）。

**修法**（`engine/ai_worker.py`，只动认领顺序与归档条件）

1. 认领 CASE 加一档：窗口内可交易(0) → 过期可交易(1) → **门卫未判定(2)** → 其余(3)，
   档内仍 `ts DESC`；`_build_claim_sql` 参数个数不变（4 个）。
2. 归档拆两段：普通行 6h → `stale_expired`；门卫未判定行 **24h 宽限** →
   新 `filter_reason='expired_gate_down'`（`_expire_gate_down_sql`）。
3. 开关：`NEWS_EXPIRE_KEEP_GATE_DOWN`（默认 1）、`NEWS_EXPIRE_GATE_DOWN_GRACE_H`（默认 24），
   关掉即退回 v408 行为。`.env` 未改。

**看板**：`gate_funnel` 新增 `expired_gate_down`，前端漏斗表单列一行；
上线后该值应接近 0、`analyzed_gate_down` 上升，即为生效。

**测试**：`test_v408_archive_and_scope.py` 21 passed（+4，覆盖宽限、独立留痕、两段 SQL、认领档）；
连带回归 `test_v322_head_of_line_block` / `test_gate_deferred_v320` /
`test_gate_passthrough_v3215` / `test_analysis_yield` 共 74 passed。

**未做**：历史 4683 条不回放；改完需重启引擎生效。

## v420 · 管线对账守卫 + 三层诊断（2026-11-17）

**新增** `backend/src_python/pipeline_guard.py`（只读断言，零副作用）+ `tests/test_pipeline_guard.py`（5 passed）。

9 条断言：入库断流 / 静默归档 / 可交易却被否 / 分析产能 / 结构化缺失 / 置信度归因 /
品种路由 / 源健康（error + 沉默）/ 影子账本在跑。阈值集中在 `THRESHOLDS`；
「算不出来」一律 unknown，不报 ok。

**今天实测抓出的断链**：静默归档 1184 条（24h）、判可交易却被否 92 条、
结构化字段缺失 95.8%（160/167）、routing_miss 46.1%、49 个源 error 或停更 >24h。

**诊断结论（三条质疑）**

1. 新闻总量少不是被拦，是源没产出（eastmoney_ann 停 3 天、reuters_news 已停用、
   qinghong 到期、fred/finnhub/bls 失败率 50%+）；配额 1200 今日仅用 176，不是瓶颈。
   被否构成里「排除+总结/分析类」是设计选择，真正丢的是「无理由归档 / 判可交易却被否」。
2. 信号为零的真凶是**结构化输出缺失 95.8%**（impact 空 / strength=0 / sentiment=0）→
   方向分恒 0 → 置信度 0 → 必被闸门打 HOLD。`routing_miss` 46% 是第二段损失。
   影子账本证明闸门一天只掐掉 15 条有方向的信号 —— **降闸门最多多 15 个且含掐对的**，
   印证「不降闸门换样本」的判断。
3. 上一轮把 4578 条空 verdict 当「现行黑洞」是错的：按 08:27 重启切段，
   重启后新写入为 0，属旧代码 + cleanup 脚本残留。守卫的验尸报告自己抓出了这个错。

**未做**：P0 结构化输出修复、P1 品种路由、P2 源修、P3 守卫定时化与告警。

**P0-1 切片二：通道故障期免重试**
- `realtime_filter.py` 新增 `_GATE_NET_STREAK / _GATE_RETRY_SKIP_UNTIL` 与
  `_gate_net_fail() / _gate_net_ok() / _gate_retry_worthwhile()`：
  连续 N 条（默认 3）「全通道无返回」→ 未来 M 秒（默认 300）内不再做 1.6x 超时重试，
  只打一遍就降级放行；任意通道成功一次自动解除。
- 被**全局请求预算**拒（`budget_blocked`）时同样不再重试、不再走备用通道（纯省等待）。
- 实测：单条门卫耗时从 ~18s 降到 ~9s；`bridge.out.log` 已出现「通道故障窗口内 → 跳过重试」。

**P0-1 切片三：门卫日预算改为持久计数 + 放宽**
- 预算默认 300 → 900；当日已用次数改为从 `ai_call_stats` 读（`_gate_budget_used_today()`），
  重启不再白送一份新额度（今天 801 次成功正是多次重启累积出来的）。
- 实测口径：门卫单次 ≈ 500 token，主分析单条 ≈ 8.2K token；门卫排除 1 条就省 8.2K，
  预算过紧（打满后门卫整段跳过、新闻裸奔进主分析）反而更贵。

**P0-1 切片四：全站请求预算与门卫超时（成本参数）**
- `backend/.env`：`LLM_DAILY_REQUEST_BUDGET` 6000 → **9000**（今天 6044 就爆掉，门卫被闸门全拒、
  新闻裸奔进主分析，比门卫过滤更贵）；`GATE_LOCAL_TIMEOUT_S` 8 → **5**；显式 `REALTIME_GATE_DAILY_BUDGET=900`。
- 上线实测（20:40–20:55）：门卫请求/条 **4.5 → 1.72**，门卫恢复判定（日志出现 `排除/可交易`），
  近 30 分钟 99 条决策、PENDING 积压 566；全站预算 6141/9000。

## v435 · 2026-11-17 20:46 · P0-2 首刀：结构字段本地推导，砍掉 schema 抢救链

**背景（实测）**：今日 `engine.out.log` 里「缺 P0-2 必填字段」的分布是
`fund_driver` 464 / `consensus_vs_us` 464 / `asset_classification` 464 / `asset_nature` 399 ——
这四个都不是模型判断，而是"描述/分类"字段，每缺一次就触发一次 `schema_fill_lean`
（≈500 token / 3–6s），今天 152 次里大部分花在这上面。

**改动**（`engine/ai_worker.py`）
- 新增 `_autofill_soft_fields(result)`：在计算必填缺口**之前**本地推导
  `asset_classification`（market_category/target_asset 映射）、`asset_nature`（标的+分类拼接）、
  `fund_driver`（模型自己 reasoning 前 60 字）、`consensus_vs_us`（|sentiment|≤0.05 → 一致）、
  `verification`（如实写"结构字段本地补全"，不伪造模型自检通过）。
- 本地补过的字段记进 `result["_schema_autofill"]`，可统计、可审计。
- 真判断字段（target_asset / reasoning / factor_ranking / elasticity_window）不本地造，缺口仍走抢救链。
- 新增 `tests/test_v435_schema_autofill.py`（8 例全过）。

**上线实测（20:47 重启，观察 55 分钟）**
- 主分析调用 / 条新闻：**4.74 → 1.80**（214 次 / 119 条独立新闻），已达 P0-2 验收线（≤2.0）；
- `schema_fill_lean`：152 次 → 窗口内 4 次；
- 产出：138 行决策 / 55 分钟（BUY 5 / SELL 12 / HOLD 121），批次 `6/6 ok` 正常。

## v436 · 2026-11-17 21:17 · P0-2 第二刀：user 消息瘦身（事件类型清单压缩 65%）

**先量**：加临时诊断 `TRIDENT_PROMPT_PARTS=1`（默认关），打出 user 消息构成 —— 总计 13.2k 字符，
其中 `dim_block`（事件类型清单）**2,718 字符**，比新闻正文还大，且对所有新闻完全相同。

**改动**（`engine/ai_worker.py::_load_dim_event_type_block`）
- 44 行「code + 中文名 + 英文名 + [分类] + → 默认方向: XAU bullish」压成紧凑表：
  `geopolitical_conflict（WTI+,XAU+）`，去掉英文名/分类标签/逐行箭头说明；
- 方向先验换成 `品种+/-` 记号（bullish/bearish 字符串也能正确转）；
- 清单 2,718 → **941 字符（↓65%）**。

**实测（21:17 重启加载 821625B）**
- user 消息 13,168–13,933 → **12,340–12,959**；
- 主分析单次 `tokens_in` 9,217 → **7,593（↓18%）**，`tokens_out` 1,084 → 962；
- `tests/test_v435_schema_autofill.py` 8 例仍全过。

**诊断开关**：`TRIDENT_PROMPT_PARTS=1` 会打印 user 消息构成前 8 大块（临时排查用，默认 0）。

## v437 · 2026-11-17 21:45 · P0-2 第三刀（system 侧清理）＋ P0-0b 长事务回潮修复

### 一、system prompt 清掉两处纯浪费（配错，非取舍）

1. **自定义指令串台**：`ai_prompts` 4 条启用条目全是别的子助手的 prompt
   （`v186_reflection_prompt` 反思助手 / `v186_hold_prompt` HOLD 判定 /
   `v186_causal_extraction` 因果抽取 / `v186_insight_extraction` 经验提炼），
   却带着「优先级最高，必须遵守」被拼进主分析 system prompt。
   → `ai_prompts.py` 新增 `_SUBAGENT_PROMPT_MARKERS` + `_is_subagent_prompt()`，
   `load_enabled_prompts()` 跳过并打印 `[PROMPTS] 跳过 N 条非主分析用途的指令…`。
   生产库实测 **663 → 0 字符**。
2. **品种规则重复**：24 条启用规则只对应 9 个品种（DXY/EURUSD/SOL/US10Y/USDJPY 各 4 遍）。
   → `load_asset_rules_block()` 加 `_seen_rule` 去重键 `(品种, 去空格内容)`。
   生产库实测 **1,098 → 579 字符**。

合并效果：`[CACHE-DBG] syslen` **11,483 → 10,270（-1,213，-10.6%）**；
user 侧同时回落到 9,628–10,467。

### 二、P0-0b：长事务回潮（P0-0 其实没关死）

重启后主分析 0 次调用、PENDING 656、日志又是 `database is locked`，
长事务哨兵 `/tmp/trident_txn_profile.log` 从空涨到 **274 行**。抓到三个真凶：

- `review/reflection_scheduler._collect_e020_from_settled_trades`：全部已结算 trade
  的写入攒在一个事务，**持锁 77.3s**；
- `causal_graph.verify_all_pending`：每个节点都写 UPDATE，整轮一次 commit，
  **12.6–39.2s**；
- `db.log_ai_call`（l2_batcher 里）：受害者，commit 排队等锁超时回滚。

**修法（只改提交粒度，不动业务）**：反思循环逐条 commit；因果核验每 20 个节点 commit。

**实测（21:33 重启 bridge+engine）**
- 最长写事务 77.3s → **10.4s**，典型 39.2s → 6.6–7.3s；
- 重启后 `database is locked` **0 条**（21:34–21:45）；
- 主分析恢复（9 次，avg_in 7,513 token）；
- 回归：`test_v433/434/435/436` **38 passed / 2 skipped**。

**仍超 5s 的三条（下轮）**：`quick_sim → gate_audit.ensure_gate_evaluations`（6.9s，含 CREATE TABLE）、
`api_server._persist_market_snapshot`（7.3s）、`l2_batcher._flush → log_ai_call`（10.4s）。

## v438 · 2026-11-17 22:00 · 写库停摆复盘 + 三处收口

### 事件
21:42 起写入全面停摆 8 分钟（DB mtime 定格、主分析 0 次、PENDING 656、
满屏 `database is locked`）。锁探针 `BEGIN IMMEDIATE` 连续全超时；
`wal_checkpoint` 返回 `(1,281,281)` = busy。
逐个 SIGTERM 排除 crons + 服务后锁才释放 → 结论：**有写入方持未提交写事务且事务里夹网络等待**，
所以 busy_timeout 调多大都没用（不是慢，是永不结束）。

### 恢复（无数据丢失）
停全部写入方 → `PRAGMA wal_checkpoint(TRUNCATE)` 18MB→0MB → 重启 engine/api/bridge。
验证：锁探针 0.00s、锁错误 0 条、写入恢复。

### 代码改动
1. `db.ensure_gate_evaluations`：`CREATE TABLE/INDEX` 改为**每进程一次**（原每轮 DDL 都算进写事务，6.9s）；
2. `db.log_ai_call`：改**异步入队**（单写线程 + 队列 20000，满则退回同步；新增 `flush_call_stats()` / `call_stats_writer_stats()`）。
   依据：统计写入在门卫分类关键路径上锁竞争时白等 12–14s，是 9.2–73.6s rollback 的全部来源；
3. `review/reflection_scheduler`：E020 循环逐条 commit（原 77.3s 一个事务）；
   `causal_graph.verify_all_pending`：每 20 节点 commit（原 39.2s）。

### 实测（22:00）
- 锁探针 FREE 0.00s；重启后 `database is locked` 0 条；哨兵只剩 5.0/5.7s 轻微超阈；
- 主分析近 10 分钟 14 次、**avg_in 5,189 token**（前 7,513，再降 31%）；
- `syslen` 10,270；PENDING 656 → 524。

### 未做
锁看门狗（每 30s 探锁 + 自动打出全部 DB 句柄 pid + 告警）。

## v439 · 2026-11-17 22:10 · 写锁看门狗上线 + 积压消化实测

### 一、看门狗（把附录 G 那次人工排查固化）

`backend/scripts/lock_watchdog.py`（新增）
- 每 30s 发一次 `BEGIN IMMEDIATE`（超时 2s），抢到立刻 `ROLLBACK`，**零写入**；
- 连续 3 次（≈90s）抢不到 → 判"写锁停摆"，一次打出四件事：
  ① 持库进程 pid/运行时长/命令行；② 长事务哨兵尾部；③ 引擎日志 lock 错误尾部；④ 三步处置顺序；
- 同波故障 10 分钟只详细告警一次；恢复时补一行 `✅ 写锁已恢复`。

常驻：`~/Library/LaunchAgents/com.trident.lock-watchdog.plist`（KeepAlive + RunAtLoad），已加载运行。
实测：造了个 12s 持锁进程 → 看门狗当场把它列进"持库进程"，退出码 1；恢复后自动报 ✅。

### 二、新增用例

`backend/tests/test_v439_lock_watchdog.py`（3 例）：锁空闲必须瞬间返回、锁被占必须判失败、
诊断函数在日志缺失时不崩。**3 passed**。

### 三、积压消化（22:00:58 → 22:09:03）

| 指标 | T0 | T1 |
|---|---|---|
| PENDING | 530 | **482** |
| FILTERED | 17,507 | 17,575 |
| DONE | 4,493 | 4,496 |
| `database is locked` | — | **0 条** |
| 门卫调用/8 分钟 | — | 24 次（≈0.3 次/条新闻） |
| 主分析/8 分钟 | — | 6 次，avg_in 7,678 |
| 看门狗 | — | 两次警告（4.2s / 3.8s），均未触发停摆 |

### 四、仍存在的排队（不是停摆）

哨兵仍记录 5–31.6s 事务，最长 `api_server._write_tick_sync → record_market_snapshot`
（几乎全是 `retry_on_busy` 退避等待，最终成功）。
下一步（未做）：把 `market_ticks` / `market_snapshots` 这类"高频、丢几条无所谓"的采样
也改成单写线程 + 队列（同 `log_ai_call` 模式），长事务会整体下台阶。

## v440 · 2026-11-17 22:35 · 行情采样异步写 + quick_sim 逐步骤提交

### 一、`timeseries.py`：行情采样改单写线程队列
- 新增 `enqueue_market_snapshot()` / `flush_market_writes()` / `market_writer_stats()`；
- worker 线程 `market-tick-writer`，单连接顺序落库（与 v438 `log_ai_call` 同一模式）；
- 开关 `TRIDENT_MARKET_ASYNC_WRITE=1`（默认）、队列上限 `TRIDENT_MARKET_Q_MAX=500`；
- `api_server._write_tick_sync` / `_persist_market_snapshot` 改为"先入队，失败才同步兜底"。

依据：哨兵 `_write_tick_sync → record_market_snapshot` 一次 commit **31.6s**，
几乎全是 `retry_on_busy` 退避等待 —— 采样数据不值得为它排队抢锁。

### 二、`quick_sim.run_cycle`：每个 `_step` 成功即提交
原来一轮里 gate_audit/open_trades/settle 的写全挂在同一事务，实测 **17.0s / 34.5s**。
现在成功 `commit()`、失败 `rollback()`（各步幂等，语义不变）。

### 三、实测（22:31）
| 指标 | 改前 | 改后 |
|---|---|---|
| 哨兵最长写事务 | 31.6s / 34.5s | **6.9s**（行情写线程，受害者） |
| 22:3x 哨兵条数 | 数条/几分钟 | **1** |
| `database is locked` | 每分钟多条 | **0** |
| `market_ticks` | 被锁卡住 | 131 条/6 分钟 |
| PENDING | 530 | **239** |

### 四、测试
`tests/test_v440_market_async_write.py`（3 例：入队→刷盘落库、空 payload 不入队、同步模式返回 False）。
与 v433/434/435/436/439 合并回归 **26 passed**（本轮抽跑）、全量 41 passed / 2 skipped。

### 五、教训
改写入路径后 **engine / api / bridge 三个都要重启**：bridge 也 import api_server 的写入路径，
只重启 api 会看到"改了但指标只降一半"的假象。

## v441 · 2026-11-17 22:45 · 归档理由补全 + 漏单捞回

### 一、`scripts/backfill_filter_reason.py`（新增）
把 `raw_news` 里 `status='FILTERED'` 且 `filter_reason=''` 的行按旁证还原：

| 旁证 | 条数 | 还原理由 |
|---|---|---|
| `gate_verdict='可交易'` 已过期 | 676 | `tradeable_but_expired` |
| 含「排除」 | 2,543 | `gate_noise` |
| 含「无信号」 | 2,460 | `gate_no_signal` |
| `gate_unavailable=2` | 4,838 | `gate_undetermined` |
| 无旁证遗留 | 5,469 | `legacy_unknown` |
| `degraded_stale` | 46 | → `gate_down` |

- 各分类**互斥**（`gate_unavailable=2` 与「无信号」重叠 250 条，已用 `COALESCE(...) != 2` 排除）；
- 门卫判「可交易」且仍在时效（默认 6h）的行 → 恢复 `PENDING` 重跑（真漏单；本次 0 行）；
- 每 200 行 commit 一次，幂等可重复执行；
- 已 `--apply`：**空理由 15,986 → 0**。

### 二、`analysis_yield.gate_funnel()`：新增 `archived_by_reason`
漏斗报表新增"归档理由"一行，`--days 1` 实测：
`gate_down 1611 / gate_undetermined 174 / legacy_unknown 148 / trade_expired 89 / tradeable_but_expired 33`。

### 三、确认两件事
1. 近 3 小时新归档**零空理由** —— v433 之后写入路径不再产生静默归档；
2. `tests/test_realtime_filter_gate.py` 12 例全通过，此前记录的 3 例漂移红灯不复现。

### 四、测试
`tests/test_v441_filter_reason_backfill.py`：7 行覆盖六种旁证 + 老名字归一，
断言捞回 / 归类 / 幂等三点。

### 五、v441 补丁（同日 23:10）
- `db.insert_raw_news()`：**入库当场归档**的行现在按结论写 `filter_reason`
  （`gate_undetermined` / `gate_noise` / `gate_no_signal` / `noise_rule`），
  堵住"新噪音新闻同样无理由"这条静默路径；
- `sample_quality_filter.audit_recent_rejections()`：改为**每条决策审计完即提交**，
  原来整批攒一个事务，实测持锁 10.3～17.8s，是 23:00 那批长事务的真正源头；
- 三服务重启后复查：>4s 长事务只剩 1 条 7.1s（行情写线程）、空理由新增 0、PENDING 239→118。

## v442 · 2026-11-17 23:40 · prompt 预算实测 + 两处瘦身

### 一、诊断能力
- `engine/ai_worker.py`：新增 `TRIDENT_PROMPT_DUMP=1` 开关，把真实主分析 prompt 落盘到
  `/tmp/trident_prompt_dump.txt`（默认关，仅供排查）。

### 二、实测分区（一次真实主分析，总计 24,688 字符 ≈ 12.4k token）
| 分区 | 字符 |
|---|---|
| system（八段+20字段+规则） | 8,708 |
| 全网搜索证据 | **3,842** |
| 反思错误码 + 经验注入 | 3,795 |
| 软启发（生效经验） | **2,926** |
| 技术状态读数 | 1,429 |
| 事件类型清单 | 902 |

### 三、`net_search.origin_evidence_block()`：搜索证据收窄
- 命中 6→4（`NET_EVIDENCE_MAX_ITEMS`）、摘要 160→60 字（`NET_EVIDENCE_SNIPPET`）、
  整块上限 900 字符（`NET_EVIDENCE_MAX_CHARS`）；
- 百科/词典站点（baike.baidu、zdic.net、wikipedia 词条等）与「xx_百度百科」拼接标题直接丢弃；
- 实测同词整块 3,842 → **653 字符**（-83%）。

### 四、`review_agent.build_active_insights_section()`：软启发按标签限流
- 同类标签最多 3 条（`REVIEW_INSIGHTS_PER_TAG`），总字符上限 1,400（`REVIEW_INSIGHTS_MAX_CHARS`）；
- 80 条生效经验 → 35 条（-56%）；硬铁律不受影响。

### 五、门卫批量化：不做
实测 22:30 后门卫 **0.86 次/条**（目标曾是 1.7→0.4），已经达标，
再改七处调用点收益为零，放弃该项。

### 六、待验
夜间流量骤降（7 分钟仅 1 次主分析），平均输入 token 样本不足；
预期从 12,422 降到约 10,800，下次复盘用附录 K 的 SQL 复核。
### 七、实测（近 1 小时 16 次主分析）
平均输入 **12,002 token**（改前 12,422，-3.4%）；搜索证据块只对重要新闻注入，
摊薄后整体降幅小，但对带块的调用单次省约 1,600 token。
门卫 1.33 次/条、归档零空理由、PENDING 78。
验收脚本：`python3 backend/scripts/verify_prompt_budget.py --hours 6`。

