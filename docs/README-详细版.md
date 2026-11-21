<div align="center">

# Reflex Quant 🪞
### *Every signal audited. Every mistake coded. Every blind spot hunted.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/stardust888888/reflex-quant?style=social)](https://github.com/stardust888888/reflex-quant)
[![Version](https://img.shields.io/badge/version-v492-blueviolet)](./CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-green)](./backend/requirements.txt)
[![Tests](https://img.shields.io/badge/tests-3500%2B%20collected-success)](./backend/tests/)

> 版本号与用例数以 `CHANGELOG.md` 和 `python -m pytest --collect-only` 的实跑结果为准，
> 本文件的徽章只是快照，不随每次发版自动更新。

> **2026-11-19 / v477 交易可用性**：账户口径已统一到 `account_ledger.py`（现金/已实现/浮盈/权益
> 全部由持仓重算，手续费双边入账），下单前必须过 `trade_preflight.py` 硬闸（账不平、行情过期、
> run 重复、杠杆越界直接拒单），止损触发会真实平仓。当前仍是 **paper + MockAdapter**——
> 能交易、能对账，但**没有接真钱**。现状与验收口径见
> [`docs/archive/交易可用性-2026-11-19.md`](./archive/交易可用性-2026-11-19.md)。

**大多数 AI 量化在比谁的信号更"准"。Reflex Quant 比的是——谁先抓到自己的错。**

**This is the Chinese developer README.** For the English investor pitch, see [`README_INVESTORS.md`](../README_INVESTORS.md). For the deck, see [`PITCH_DECK.md`](../PITCH_DECK.md).

</div>

---

大多数 LLM 量化系统都是「开火-遗忘」式——出信号、看胜率、自评。
但**用来评价自己的指标，恰好就是它没做好的那一项**，盲区永远抓不到。

Reflex Quant 反过来——**先把信号审一遍，再决定信不信**。但这句话得说准：

- **决策时的闸**是 `decision_guard`（事前否决）；`reflection_checkers` 是**事后审计**，
  且方向性检查器（因果/事件逻辑/推理质量）只审 **BUY/SELL** —— HOLD 走的是
  「漏单反思」（`missed_signal_reflection`，用事后行情判"该动没动"）。
  所以"每个信号都审一遍"只在方向性决策上成立，HOLD 是另一条通道。
- **33 类错因 / 43 个错误码 / 7 层反思（E001–E033，E001 拆 a–j）**：事件因果、因果方向、样本完整性、假设/反事实/校准/历史/跨品种/机理/置信度。
  每条都有明确的可执行修复建议。**但不是每个码都同等有效**：`error_code_ledger`
  按 hard（15）/ soft（8）/ research（16）分层 —— soft 只是文本模板合规检查
  （E021/E022 那类"有没有写反向预案"），research 需统计验证才可采信，只有 hard 进闸门统计。
- **元反思循环（meta-reflection loop）**：7 层全漏同一笔坏交易时，记下这个盲点。
  准入是「同信号组 ≥5 笔**已结算独立成交**」；样本不足时明确报"样本不足"，
  不再写 `no_baseline_new` 凑数（历史 107 条 cycle 全是这一类记账，0 次判断）。
  每日 03:47 定时跑，与 policy 更新（04:17）分开。

> Reflex Quant 不会随时间变得更"自信"，而会随时间变得更"诚实"。
> 这是 LLM 驱动的 quant 系统里少有的差异化方向，也是 E028+ 向社区开放协作的入口。

### 改完代码怎么自测（不用起引擎）

```bash
cd backend
python3 scripts/offline_selftest.py      # 离线闭环 12 项，30 秒，不碰生产库
bash scripts/selftest_all.sh             # 离线 + 核心回归
python3 scripts/acceptance_check.py      # 真数据到位后：只读验收 13 项
```

详见 `docs/离线自测与验收口.md`。


## 现状（诚实口径，2026-11-19 17:30 库内实测）

README 里的能力描述是"设计上有什么"，不是"已经验证了什么"。两者差距必须写出来，
否则第一个问题就能问穿。

> **2026-11-19 16:47 事故与重置（必读）**：某 pytest 用例把 `TRIDENT_DB_PATH` 指回生产库，
> 并 `drop` 掉了三道守卫触发器，`ai_decisions` 390 → 4、`raw_news` 1,270 → 217、
> `quick_sim_trades` 8 → 1（且是孤儿探针单）。处置：重建触发器、删测试残留、
> 清 3,454 行孤儿记账（明细归档在 `backend/backups/orphan_ledger_20261119_170157/`）、
> 净值快照重置为 1 条可复现基线。随后加了 sqlite 层的生产库写保护，同一个坑不会再踩第四次。
> **下面这行数字是重置后的真实状态，不是"系统本该如此"。**

| 指标 | 实测（2026-11-19 17:30） | 说明 |
|---|---|---|
| 模拟成交（累计） | **0 笔** | 旧 8 笔（全亏，均值 −0.33%/笔）已随事故清零；逐笔明细见 `backend/reports/funnel/` |
| 已结算 | **0 笔** | 无可结算样本，胜率/均值一律不构成结论 |
| 净值快照 | **1 条基线**（equity 10000、realized 0） | note 标明 `account_baseline_orphan_cleanup_20261119`，可被一条 SQL 复现 |
| 决策产出 | **3 条在库**（17:42 恢复后新产出，引擎 PID 正常写库） | 17:37 已恢复采集；暂停闸门保留（引擎自检 + 包装器 + sitecustomize 写守），标记在即自动冻结 |
| 反思样本 | `decision_reflection` 4 条 / `structured_reflections` 6 条 | 与旧口径 605 条不可比（旧样本已随决策清零） |
| 反思有效性 | **未验证（NULL）** | `hit` 记的是交易胜负（WIN→1），不是反思有效性；对照实验需 ≥60 笔 |

**结论**：反思层"能跑、能留证据、能自证失败"，但**还没有可验证的交易边际**。
下一步的目标是攒够 ≥60 笔已结算样本，而不是再加第 34 个错误码。

### 公司级基本面（v448 → v454，2026-11-18）

自查命令（只读、不联网）：`cd backend && python3 scripts/cron_equity_fundamentals.py --coverage`
影子账本：`cd backend && bash cron/run_fundamental_shadow.sh`（写 `backend/reports/fundamental_shadow/latest.json`）

宏观基本面（FRED 33 组）覆盖是可以的，**公司级基本面一直是薄的**；v405/v406 补宽度，v448 修正确性与可验证性：

| 项 | 实测 | 说明 |
|---|---|---|
| 美股覆盖 | **16 / 16 只**（三表 134 行 / 15 票） | FMP 402 + AV 配额打满 → 接 yfinance 季报三表（`--yf-fill`，不耗配额）；跨源同季去重防 TTM 双算 |
| 同季同比（v452） | META **+0.70** / MSFT +0.56 / TSLA **−0.70** / PLTR +0.70 / SMCI +0.35 | 三表 5–7 期凑不出 TTM 同比 → 同季对比（EPS 优先，缺则净利 ×0.8）；缺估值分位时方向分 ×0.7 标 `earnings_only` |
| TTM EPS 序列 | **10 只都有 8~11 个点** | 三表 `eps_diluted` 实测 8 季全 NULL → 改用财报事件滚动四季求和（带真实公布日，可防前视） |
| 周期判型（修 PE 陷阱） | MU 强周期/峰（cyc 4.08）**0.9308 → 0.4673**；NVDA 平稳/峰不折半 | 强周期峰/谷：估值与盈利修正**各折半一次**，标 `peak_earnings` / `trough_earnings` |
| 亏损股口径 | COIN **−0.3504** / RIOT −0.3992 / CLSK **−0.4655** / MARA **−0.6000** | 营收增速 + 毛利率趋势 + 现金流率 + 现金流趋势；读数不足封顶 ±0.6 标 `loss_eps_only`，不再挤在 −0.44 / −1.0 |
| 财报窗口 | 10 只里 5 只有 `earnings_in_days`；NVDA 事件 −1 天 → **0.9445 折到 0.4723** | 窗口 ±3 天**对称**（原只认未来会漏掉"昨天公布"）；日历 **FMP → AV CSV → yfinance** 三源 + 关注清单补缺 |
| 财报日历 | 未来事件 **7 → 774 条（774 票）**，未来 30 天 527 条 | 90 天窗口 |
| 盈利含金量 | NVDA 0.70 / GOOGL 0.76 / MU 1.06 / AAPL 1.10 / AMZN 1.25（全部 ≥0.60） | TTM 经营现金流÷净利 <0.60 → 盈利修正折半标 `low_cash_quality`；只判差不奖励好；OCF 缺失/净利≤0/净利<1%营收/ratio>3 不判 |
| PB 分位 | **16 / 16 ✓**（AAPL 95 天、NVDA 164 天、其余 250~263 天） | v454：分子 = 快照 ∪「已公布股本×收盘」推导，分母 = SEC 公司事实净资产（带 `filed` 申报日防前视）；60 天门槛已过 |
| 净资产 | **16 / 16**（v448 时仅 7 只，AAPL/AMZN/GOOGL 全 NULL） | SEC companyfacts 免配额通道（AV 免费档字段不全、FMP statement 402）；同期末按最新申报去重，缺失用 Assets−Liabilities 派生并标记 |
| 估值分位 | **16 只全部可得**（v448 时 5 只） | PE 5 只有真实分位（各 ~336 样本）；PB 补齐后取代 PE 缺位的 11 只；再与同业混算 |
| 同业可比（v454） | 5 组 26 票；NVDA `semis 7 只 → −0.41`、MU `+1.0`、AAPL `mega_tech → −1.0`、COIN `+0.74` | 自身历史分位 = 时间锚，同业倍数 = 横截面锚 → `0.7×自身 + 0.3×同业`；组内样本 <2 一律不作结论 |
| 宏观传导（v454） | 33 组 FRED → 4 驱动（rate +0.43 / risk −0.51 / usd +0.36 / curve −0.60）→ 板块 tilt（semis −0.29、crypto_miner −0.43） | 原本只是 prompt 背景板（`decision_guard` 里没有任何 macro 权重）→ 现在是可复现的 `±0.15` 倾斜，写进 components 与解释 |
| 权重可验证 | 影子账本已挂钩引擎，每天 06:20 结算（06:10 另有 cron 入口） | v454 重写：`applicable/reason` 分适用性、主判据 **+10 天**（季报慢变量不用 3 天）、基准价取「决策前最后一根」；<30 笔一律判 `insufficient` |

**诚实边界**：财报只有"规模/增长/质量/估值"四类 TTM 读数，**没有**分部与地区拆分、现金流质量、
债务到期结构、管理层指引、分析师覆盖、DCF/EV-EBITDA；**权重 0.08 仍是先验** —— 影子账本
生产样本仍为 0 笔「适用」的（164 行全是 XAU/WTI/BTC 或占位），机制能自证但还没被验证；
同业分组是人工写死的 26 票，宏观传导系数未回测校准；MSTR 这类财库票净资产含 BTC，PB 0.58 不代表便宜。
口径、PB 补洞与验收清单见 [`docs/archive/v454-基本面补洞-2026-11-18.md`](./archive/v454-基本面补洞-2026-11-18.md)，
宽度扩面见 [`docs/archive/v406-基本面扩面-2026-11-17.md`](./archive/v406-基本面扩面-2026-11-17.md)。

**A股 / 港股（v406 + v455）**：东财财报读数（营收/净利同比、年化 ROE）按需抓取落 `cn_equity_fundamentals`；
估值分位在 v455 之前**恒为不可用**（每票 1~7 个快照点、门槛 60），导致分数只剩增速一把尺 ——
东山精密/新易盛直接打满 1.0。v455 用「当日收盘 ÷ 该日之前已公布的 EPS_TTM / BPS」推导一年序列
（法定披露截止日当已知日，防前视），8/8 只、1940 行，PE 分位实测 002384 0.8% / 600519 95.9% / 00700 1.2%，
茅台分数 +0.22 → **−0.18**。推导 PE 24.08 / PB 4.99 与东财当期快照完全一致。
**边界**：无同业对比（8 票跨 8 个行业，样本不足不造数）、无财报日历、无三表、无宏观传导；
见 [`docs/archive/v455-A股港股估值分位-2026-11-18.md`](./archive/v455-A股港股估值分位-2026-11-18.md)。

**口径体检与工程化（v456）**：`scripts/fundamental_drift_check.py` 每日核对「推导值 vs 供应商当期值」
（≤5% OK / 5~25% WARN / >25% FAIL），首跑就抓到净资产取到旧报表的真 bug（META PB 偏差 +39.9% → 0.2%），
当前 **OK 22 / WARN 4 / FAIL 0**；剩余 WARN 均经 SEC 官方值核对为供应商口径差异。
同业分组自动识别（yfinance industry + SEC SIC 兜底，`ASML/MRVL→semis`、`SHOP→software`）；
财库票（MSTR）PB 不参与自身与同业判断（估值分位因此由 16 只变 15 只，数据仍保留可审计）；
FRED 层进 prompt 前压缩 60%（与权重同源，`TRIDENT_FRED_PROMPT_MODE=full` 可回退）；
`decision_fundamental_snapshot` 埋点 + `scripts/fundamental_ab_replay.py` 为“0.08 是否有效”攒证据
（当前样本 0，如实输出 insufficient）；仓位乘数只做影子（`sizing_mult`，不改实际下单）。
见 [`docs/archive/v456-六项落地-2026-11-18.md`](./archive/v456-六项落地-2026-11-18.md)。

**宏观驱动系数校准（v459）**：`scripts/macro_driver_calibration.py` 用 FRED 5 年日频 + 板块 ETF 检验
v454 那套「4 驱动 → 板块倾斜」先验。结论：四个驱动的中位对齐 IC 全部 <0.10，**复合 tilt 对同桶 ETF
前瞻 10/20 日收益的中位 IC = +0.00**；季度窗口（60 日，独立样本仅 ~12）`risk` 符号与先验相反。
因此 `_MACRO_TILT_CAP` **0.15 → 0.05**（`TRIDENT_MACRO_TILT_CAP` 可恢复），宏观倾斜只作小幅修正，
不再按满额调分；新增每月 1 日 07:20 的 `macro-calibration` 定时任务。另 `fundamental_drift_check.py
--warn-check` 把 WARN 变成三方表决：**TSLA 我方与 yfinance 完全一致（16.83 vs 16.83），供应商偏 18%**，
NVDA/RIOT 属期次口径不同，腾讯第三方不可得。见 [`docs/archive/v459-宏观驱动系数校准-2026-11-19.md`](./archive/v459-宏观驱动系数校准-2026-11-19.md)。

**非公司基本面（v460）**：此前只有美股/A股/港股有基本面这一票，黄金/原油/美元/外汇/加密在
`decision_guard` 里记「非公司标的 N/A」→ 0.08 权重让出后归一化，方向**没有可复现的基本面依据**。
`src_python/alt_fundamental.py` 给 7 个品种各接一条可审计分量（黄金=实际利率水平+变化/美元/COT/SGE 溢价；
原油=库存水位+周变化/馏分油/产量/炼厂投入/COT；美元=2Y/TIPS/DXY COT；EURUSD=美元镜像；USDJPY=美日
利差**按分位**+日元 COT；BTC/ETH=资金费率/稳定币供应/算力/未平仓），实测 2026-11-19：
XAU −0.46 / WTI +0.06 / USD +0.49 / EURUSD −0.49 / USDJPY +0.03 / BTC −0.22 / ETH −0.71，
白银等未接入品种明确 `unsupported`（不返回 0 分）。门槛：分量 <2 或独立数据族 <2 不发分、分位需 ≥30 观测、
说不清符号的进 `gaps` 不打分。同时 EIA 补了产量/炼厂/馏分油三条序列（走 `/v2/seriesid/PET.<id>.W`，
一次抓 52 周供分位），prompt 用「一行结论」替掉原始点位、COT 168 项折叠成计数，体积 10,490 → 8,442（−19.5%）。
见 [`docs/archive/v460-非公司基本面-2026-11-19.md`](./archive/v460-非公司基本面-2026-11-19.md)。

**美债基本面（v464）**：接上 v460 之后，美债从 0 分变成可打分，且**长短久期两套口径** ——
10Y/30Y/TLT 那套看实际利率、通胀预期、曲线 carry、期货持仓与信用利差（6 分量）；2Y/SHY 那套看政策利率
（4 分量，短债不吃久期风险）。方向约定「正=看多债券价格」，曲线符号写死为 carry 视角并在 `detail` 里说明理由。
同时发现并修掉一个更根本的问题：库里 FRED 序列原本只有 38 天，"历史分位 83%"实为 38 天分位 ——
用官方 API 回填 3 年 10,214 行、分位窗口抬到 ~2.7 年，实际利率 2.89% 的分位因此从 83% 修正为 98%，
UST 分数由 −0.09 变为 −0.22。见 [`docs/archive/v464-美债基本面-2026-11-19.md`](./archive/v464-美债基本面-2026-11-19.md)。

**拍卖需求与股指估值（v465/v466）**：美债补上「投标倍数」这一层（10Y/30Y 合成一个分量，
长端 89% 分位、短端 39% 分位——曲线长端紧短端松的微观证据）；SPX 接 multpl 月度 CAPE
（1,871 个观测到 1871 年，当前 38.63 = 近 30 年 90% 分位），配「盈利收益率−10Y」与信用利差，
实测 −0.67 为全品种最负。同一个 HY OAS 变化对债券是正分、对股指是负分，符号差异有专门测试钉住。
见
[`docs/archive/v465-美债拍卖需求-2026-11-19.md`](./archive/v465-美债拍卖需求-2026-11-19.md)、
[`docs/archive/v466-股指估值CAPE-2026-11-19.md`](./archive/v466-股指估值CAPE-2026-11-19.md)。
**银/铜、纳指代理、沪深300、TIC（v467）**：剩下的品种一次接完。银、铜补上 5 年日线后，
用**跨品种比价**做估值 —— 金银比（高=白银相对黄金便宜=偏多白银）、铜金比（高=增长预期强=偏多铜），
再叠 COT 反向、美元 20 日反向、白银实际利率，实测 XAG −0.06 / CU +0.25（铜投机净多只剩 4% 分位，
是"拥挤度极低"而不是"基本面强"）。纳指没有同口径 CAPE，复用 SPX CAPE 但强制在 evidence 写「SPX 代理」、
在 gaps 写明"不是 NDX 自己成分股的估值"，测试钉住标签不许删。沪深300 接中证指数官方 PE
（1,708 天，当前 13.09）+ 由「点位/PE」反推的指数盈利（60 日变化），实测 −0.57；
标准 ERP 算不了（FRED 中国长端序列已停更，返回 400），写进缺口。TIC 外资持仓接通 321 个月，
但**滞后 >75 天不发分**：当前数据滞后 339 天，所以只出现在 gaps 里，不参与加权 ——
旧持仓当"当下买盘"用比不接更危险。见
[`docs/archive/v467-银铜纳指沪深300-TIC-2026-11-19.md`](./archive/v467-银铜纳指沪深300-TIC-2026-11-19.md)。
**BTC 链上活跃度（v468）**：加密的"贵不贵"（MVRV/已实现市值/交易所余额）仍需付费源，
但"还有没有人在用"免费可得 —— blockchain.info 日频接入 722 天活跃地址、725 天交易笔数，
BTC 增加 `onchain_addr 0.12` / `onchain_tx 0.10` 两分量（活跃度扩张=偏多，属显式假设不属因果）。
BTC 由此从 4 分量变 6 分量、−0.0067 → +0.1601：链上扩张、场内杠杆退、稳定币缩，三种信号打架，
分歧度高，看板不可只看单一分数。同一轮把三条免费源的失败证据固化下来：上期所只有行情+持仓（库存全 404）、
FRED 期限溢价 9 次重试全灭、中国 10Y 两个入口一个断连一个返回空 —— 说不清符号的一律不做。
见 [`docs/archive/v468-BTC链上活动与免费源到顶-2026-11-19.md`](./archive/v468-BTC链上活动与免费源到顶-2026-11-19.md)。
**BTC 现货 ETF 净流入（v469）**：加密补上"真金白银的增量资金"—— farside 全量页回填 733 个交易日
（2024-01-11 起），11 只美国现货 ETF 逐日净流入合计，BTC 增 `etf_flow 0.14`（20 日累计净流入分位，流入=偏多），
6 分量变 7 分量。实测 20 日净赎回 **37.13 亿美元（历史 6% 分位）**，把链上三绿压回中性（+0.16 → +0.06）：
**使用度在涨、钱在撤**，这是当下 BTC 最该知道的一句。美债期限溢价五个入口全数复核失败，
结论改为"必须换源（Wind/Bloomberg）"，不再原样重试。
见 [`docs/archive/v469-BTC-ETF净流入与期限溢价换源-2026-11-19.md`](./archive/v469-BTC-ETF净流入与期限溢价换源-2026-11-19.md)。
**SOL 接入（v470）**：加密第三品种补齐 —— DefiLlama 免费给链上 TVL（2,074 天）与日手续费（2,132 天），
SOL 增 `tvl 0.44` / `fees 0.34` / `cot 0.22` 三分量（资本流入、真实需求扩张=偏多，持仓拥挤=反向）。
实测 **−0.7510**：TVL −19.3%、手续费 −28.3%，"人和钱一起走"而空头未挤满，典型阴跌结构。
至此 15 个品种有非公司基本面（XAU/XAG/CU/WTI/USD/EURUSD/USDJPY/UST/US2Y/SPX/NDX/CSI300/BTC/ETH/SOL）；
ETH 成为最薄的一个（无链上维度），是下一个明确优化点。
见 [`docs/archive/v470-SOL链上TVL与手续费接入-2026-11-19.md`](./archive/v470-SOL链上TVL与手续费接入-2026-11-19.md)。
**ETH 链上维度（v471）**：以太坊补上 DefiLlama 的链上 TVL（3,341 天）与日手续费（4,122 天），
ETH 增 `tvl 0.22` / `fees 0.18`，3 分量 → **5 分量、权重和 1.12**、−0.71 → −0.66。
结构值得看：TVL −20.7%（资本在撤）、未平仓 96% 分位（杠杆堆到极值）、资金费率 88% 分位（多头拥挤），
但手续费只 −1.3%（链还在用）—— **链在用、价靠杠杆撑** 的多头陷阱形态。
至此 BTC/ETH/SOL 三个加密都有"链上 + 场内"两层；只有 BTC 多一层 ETF 资金流，这也是它仍站在正区间的唯一原因。
见 [`docs/archive/v471-ETH链上维度补齐-2026-11-19.md`](./archive/v471-ETH链上维度补齐-2026-11-19.md)。
**分歧度与可信度（v472）**：加权平均不是共识 —— "全体轻微偏多"和"一半极多一半极空"能算出同一个分数。
现在每个品种直接输出 `dispersion`（加权标准差）与 `confidence`（可信度）、多空权重占比，
`decision_guard` 的 0.08 权重按 `0.6 + 0.4×confidence` 打折（下限 0.6，分歧大不等于没信息）。
实测 15 品种里 4 个高分歧：USDJPY 0.89（多空几乎对半，分数≈打平）、US2Y 0.71、UST 0.65、BTC 0.64（链上极多 vs ETF 极空）；
低分歧的高分最可信：SPX 0.26、ETH 0.32（空头权重 1.12、多头 0，全表最纯的空头）、XAU 0.33。
见 [`docs/archive/v472-分歧度显式化-2026-11-19.md`](./archive/v472-分歧度显式化-2026-11-19.md)。
**影子仓位按分歧度打折（v473）**：分歧度从"只影响方向分"扩到"也影响仓位乘数" ——
`sizing_multiplier` = `(1+0.20×score) × (0.75+0.25×confidence)`，带可信度时下限放开到 0.70，
公司侧（无 confidence）逐位保持 v456 口径不变；快照表新增 `dispersion/dispersion_level/confidence` 三列可审计。
实测 USDJPY 1.006→0.856（分歧 0.89）；BTC 1.018→0.908（链上极多 vs ETF 极空，正分不该当真）；
SPX/CSI300 只降 0.035（分量一致，本就该少打折）。**仍然只记账，不改实盘。**
见 [`docs/archive/v473-影子仓位按分歧度打折-2026-11-19.md`](./archive/v473-影子仓位按分歧度打折-2026-11-19.md)。
**公司侧分歧度（v474）**：公司票也会自相矛盾 —— 最典型是"估值说便宜、盈利在砍"。
现在 `fundamental_factor` 也输出 `dispersion/dispersion_level/confidence/bull_weight/bear_weight`，
口径与 alt 侧逐字一致（盈利股 估值 0.50 / 盈利修正 0.50 / 宏观 0.10；亏损股用生意四项权重），
`decision_guard` 的解释句、影子仓位乘数全部共用一套读取。
实测 16 只票：**COIN 分歧 0.84、可信 0.44**（估值 +0.25 对盈利 −0.60，标准价值陷阱）；
PLTR/META/SMCI 都是"中分歧 + 多 1.00 / 空 0.10"，强势来自两条主线都同意而非互相抵消。
见 [`docs/archive/v474-公司侧分歧度-2026-11-19.md`](./archive/v474-公司侧分歧度-2026-11-19.md)。
**可信度门槛闸（v475）**：可信度不只决定"分数算多少"（权重打折）和"仓位多大"（乘数打折），
现在还决定"够不够格开仓"：`buy_min_effective = 校准门槛 + min(0.10, 0.10×(1−conf))`，
只收紧不放宽、封顶 0.10（基本面仅 0.08 权重，不该一票否决）。同轮把 v472 的权重打折
从 alt 专有改成两侧通用（COIN 0.080→0.0621）。`thresholds` 同时保留原始/校准/生效三个数
加 `action_before_confidence_gate`，能证明"这次 HOLD 是不是被可信度拦下的"。
见 [`docs/archive/v475-可信度门槛闸-2026-11-19.md`](./archive/v475-可信度门槛闸-2026-11-19.md)。










**信号端背景**（不算差异化，一句话）：双源新闻采集（FinancialJuice WebSocket + TreeNews webhook）→ L1 关键词 + L2 LLM 双层去噪 → Kimi K3 推演 → 2h 真实行情前向验证 → SSE 推送 Next.js 前端 + 飞书群机器人。完整链路见下方架构图。

## 架构

```
 FinancialJuice WSS ──┐
                      ├─→ realtime_filter (L1 关键词 / L2 LLM) ─→ raw_news (SQLite, WAL)
 TreeNews webhook ────┘        (engine/ingest.py, engine/webhook.py)        │
 127.0.0.1:9000                                                             ▼
                                              engine/ai_worker.py (Kimi K3 via OpenRouter,
                                              市场快照 + 历史绩效注入, 父子信号聚合)
                                                                             │
                                                                             ▼
                                                                      ai_decisions 表
                                                                       │           │
                                                       engine/forward.py            │
                                                       2h 前向验证 (MFE/MAE/PnL,    │
                                                       WIN/LOSS 结算)               │
                                                                                   ▼
                                            api_server.py (FastAPI: REST + SSE, 127.0.0.1:8000)
                                                             │                │
                                                             ▼                ▼
                                            frontend/ (Next.js 14, :3000)   飞书告警
                                                                            (engine/alerts.py)

 dashboard/ (Streamlit :8501) — 离线复盘看板，直接读 SQLite，不在实时链路内
```

## 目录结构

> 下列数字来自 `backend/scripts/architecture_baseline.py` 与 `pytest --collect-only` 实跑，
> 结构改造进行中时请以跑出来的结果为准，不要手改本文数字。

```
reflex-quant/
├── backend/
│   ├── src_python/                  # 运行时根（sys.path 根，**不是** Python 包：无 __init__.py）
│   │   ├── api_server.py            # FastAPI + SSE 主后端 · 20.6k 行 / 357 端点（待按域拆分）
│   │   ├── bridge_server.py         # :8100 桥接服务 · 10.3k 行 / 87 端点（含 api_server 的 29 条重复）
│   │   ├── engine.py                # 薄入口：python engine.py
│   │   ├── engine/                  # ✅ 引擎包：唯一完成领域内聚的样板（编排/采集/分析/结算/告警）
│   │   ├── routers/                 # ⚠ 按域路由，目前只有 meta.py（拆分进行中）
│   │   ├── repository/              # ⚠ 数据访问，目前只有 news.py
│   │   ├── migrations/              # 数据迁移
│   │   ├── market_sentiment/        # 情绪子包（fng / trump_posts）
│   │   ├── config.py · db.py        # 唯一配置入口 / 唯一 schema 权威 + SQL 安全门（被引用最多，勿动）
│   │   └── *.py                     # 其余 420 个模块平铺在此，按 feeds/signals/decision/reflection 待分包
│   ├── scripts/                     # 自检 / 审计 / 运维脚本；oneoff/ 为一次性脚本
│   │   └── architecture_baseline.py # 端点基线护栏（CI 第一步，只读，产出 reports/baseline/）
│   ├── tests/                       # pytest 套件：243 个文件 / 3500+ 用例，全离线
│   ├── reports/                     # 端点基线 + 健康 / 漏斗报告（生成物，CI artifact）
│   ├── runtime/ · logs/ · backups/  # 运行状态与快照（.gitignore，不入库）
│   ├── cron/ · launchd/             # macOS 定时任务定义
│   ├── init_db.py                   # 全新部署的 DB 初始化（幂等，不删数据）
│   ├── requirements.txt             # 运行依赖（宽松区间）
│   └── requirements-dev.txt         # 测试依赖
├── frontend/                        # Next.js 14 实时信号台（app/ 按页面分域，components/ 按域分目录）
├── dashboard/                       # Streamlit 离线复盘看板（见 dashboard/README.md）
├── deploy/                          # systemd units + nginx 示例 + 部署文档
├── docs/                            # 设计文档 61 篇（archive/ 为已下架方向；27 篇带日期/版本前缀）
├── scripts/                         # 跨端启动 / 部署 / 签发脚本 + macOS launchd 模板
├── ui-redesign/                     # UI 改版过程稿（mock html / 结构图 / 落地记录）
└── .env.example                     # 环境变量模板（复制为 backend/.env）
```

**结构现状**：顶层按职责分层是健康的，但 `src_python/` 是一层扁平目录（无包边界，靠 `sys.path` 注入），
模块按「领域」而不是按「版本号」分组的工作尚未完成。改造路径与风险清单见
[`docs/后端领域分包方案-2026-11-16.md`](./archive/后端领域分包方案-2026-11-16.md)；
拆分 `api_server` 期间的端点总数护栏见 `backend/scripts/architecture_baseline.py`。

## 快速开始（Windows）

前置：Python 3.11–3.14（开发机实测 3.14.7）、Node.js 18+。

```cmd
copy .env.example backend\.env
REM 编辑 backend\.env，至少填入 OPENROUTER_API_KEY

pip install -r backend\requirements.txt
cd frontend && npm ci && cd ..

scripts\dev_start.bat
```

`dev_start.bat` 会开三个窗口：engine、api_server（127.0.0.1:8000）、frontend（localhost:3000）。

手动方式（三个终端）：

```cmd
cd backend\src_python && python engine.py
cd backend\src_python && uvicorn api_server:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

可选第四条链路（TreeNews 快讯源）：

```cmd
cd backend\src_python && python treenews_bridge.py
```

## 环境变量

复制 `.env.example` 为 `backend/.env` 后填写。完整注释见模板文件。

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENROUTER_API_KEY` | 是 | Kimi K3 主模型 + 实时过滤 L2 的调用通道 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 否 | Agent Chat 数据副驾（前端问答） |
| `AGENT_LLM_BASE_URL` / `AGENT_LLM_API_KEY` / `AGENT_LLM_MODEL` | 否 | Agent Chat 专用配置，默认可复用 DeepSeek |
| `XAI_API_KEY` | 否 | Grok 备用 |
| `DOUBAO_API_KEY` / `DOUBAO_MODEL` / `DOUBAO_BASE_URL` | 否 | 火山引擎（当前停用，DOUBAO_MODEL 须为 Endpoint ID） |
| `FEISHU_WEBHOOK_URL` | 建议 | 飞书群机器人告警地址；为空则跳过推送 |
| `CORS_ALLOW_ORIGINS` | 否 | API 跨域白名单，逗号分隔，默认 `http://localhost:3000,http://127.0.0.1:3000` |
| `HTTP_PROXY` | 视网络 | 国内机器访问 OpenRouter/Binance 需要；海外直连不要设 |
| `TREE_NEWS_PORT` | 否 | engine webhook 监听端口，默认 9000 |
| `TRIDENT_DB_PATH` | 否 | 覆盖 SQLite 路径，默认 `backend/trident_event_bus.db` |
| `MT5_*` / `BINANCE_*` | 否 | MetaTrader 5 本地行情 / 币安账户（均非必需） |

### v240+ 反思增强：K-Bayes / BOCPD / Tournament

| 字段 | 默认 | 说明 |
|---|---|---|
| `REFLECTION_BAYES_ENABLED` | `0` | 启用 Beta-Binomial 后验概率追踪每条反思器真实胜率；启用后 evidence_refs 增 `_bayes_p_curr_lt_base` |
| `REFLECTION_BAYES_ALPHA_PRIOR` | `2` | Beta 先验 α（胜例先验样本） |
| `REFLECTION_BAYES_BETA_PRIOR` | `2` | Beta 先验 β（负例先验样本） |
| `REFLECTION_BAYES_ABS_DELTA` | `0.05` | 触发"真实胜率显著偏离基线"的最小后验-先验差 |
| `REFLECTION_BAYES_MIN_SAMPLES` | `10` | 样本数低于此值不判定（避免小样本误判） |
| `REFLECTION_BOCPD_ENABLED` | `0` | 启用 Bayesian Online Change-Point Detection 检测信号突变点；触发时写入 evidence_refs `_bocpd_fresh_run_prob` |
| `REFLECTION_BOCPD_HAZARD` | `200` | BOCPD 先验"突变概率倒数"（值越大越保守） |
| `REFLECTION_BOCPD_THRESHOLD` | `0.5` | 突变后验概率阈值；≥ 此值记为 fresh-run 信号 |
| `TOURNAMENT_ENABLED` | `0` | 启用 k=3 LLM verifier 投票替代单次 LLM；开启后 evidence_refs 增 `_tournament_quorum` / `_tournament_verifier_ids` |
| `TOURNAMENT_QUORUM` | `2` | quorum 数（k=3 中至少 2 个同意才 confirm） |
| `TOURNAMENT_TIMEOUT` | `60` | 单次 tournament wall-clock 上限（秒） |
| `TOURNAMENT_PARALLEL` | `1` | 并发跑 verifier；设 `0` 走 serial（调试用） |
| `TOURNAMENT_CB_THRESHOLD` | `0.7` | Circuit breaker：滑动窗口内"全 verifier 失败"率 ≥ 此值触发 OPEN，短路回单次 LLM |
| `TOURNAMENT_CB_WINDOW` | `10` | 滑动窗口大小（最近 N 次调用） |
| `TOURNAMENT_CB_COOLDOWN` | `20` | OPEN 后冷却 N 次调用再 half-open 重试 |

**全部默认 `0`（opt-in）** — 开启任意一项即可立即获得更细粒度的反射审计，零破坏性。

观测端点：
```bash
curl http://127.0.0.1:8000/api/health/tournament
# → tournament metrics + circuit breaker state + 当前 config
#   { "metrics": {total_runs, confirmed_count, avg_elapsed_ms, quorum_distribution, ...},
#     "circuit_breaker": {is_open, recent_calls, ...}, "config": {...} }
```

## 测试

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 3500+ 用例，全部离线（当前 3596 条，`--collect-only` 实测）
```

## 体验模式（v300 新增）— 给朋友/公开展示用

Trident 的核心算法是私有 IP。v300 起提供三种部署模式，由 `TRIDENT_MODE` 切换：

| 模式 | 场景 | 体验者看到 |
|---|---|---|
| `full`（默认） | 自己玩 | 全精度 + 全部功能 + 源码 |
| `demo` | 公开展示 / 朋友初探 | 2 位小数 + 1 天历史 + 限流 + 水印 |
| `licensed` | 给受邀请用户 | 4 位小数 + 7 天历史 + 凭 License + API Key |

体验者拿到的是**只读镜像**（Docker / Modal），永远拿不到 `engine/` 源码。
每次响应带 `X-Trident-Watermark` 头，截图泄漏可定位。

快速体验：

```bash
# 1. 在你本机签发许可证（仅 licensed 模式需要）
python3 scripts/gen_license.py issue --tier pro --days 30 \
    --issued-to "bob@friend.com" \
    --secret "$TRIDENT_LICENSE_SECRET" \
    --out ./license.lic

# 2. 创一个 API Key
python3 scripts/gen_api_key.py create --name bob-laptop --tier pro \
    --store ./api_keys.json
# → trk_xxxxxxxxxxxxxxxx

# 3. 启动体验模式
export TRIDENT_MODE=licensed
export TRIDENT_LICENSE_SECRET=...        # openssl rand -hex 32
export TRIDENT_LICENSE_FILE=$(pwd)/license.lic
export TRIDENT_API_KEY_STORE=$(pwd)/api_keys.json
python3 -m api_server
```

完整设计、5 层防御、应急响应见 [`docs/SECURITY_DEMO_MODE.md`](./SECURITY_DEMO_MODE.md)
和 [`docs/SECURITY_DEMO_MODE_DEPLOY.md`](./SECURITY_DEMO_MODE_DEPLOY.md)。

## 反思系统 XLSX 导出

`/review` 页面右下角 `↓ 导出 XLSX` 按钮调后端 `GET /api/review/export.xlsx`，
返回 5 个 sheet 的 .xlsx（Overview / Trades / Reflections / Insights / Experiences），
每个 sheet 都冻结首行 + 开启自动筛选，方便 Excel 中筛选分析。

```bash
curl -OJ http://127.0.0.1:8000/api/review/export.xlsx
# → trident_review_YYYYMMDD_HHMMSS.xlsx  (~70KB, cold 12s / warm 0.5s)
```

CLI 自检（不依赖前端）：

```bash
cd backend/src_python && python review_excel_export.py
# → ./trident_review_YYYYMMDD_HHMM.xlsx
```

导出逻辑复用 `_safe_query` + `pnl_pct>0/<0` 派生 win/loss，不引入新 schema 假设。

## v309 P0 — 三维度归因闭环（A/B/C）

把「反思到底错在哪」变成可统计、可回填、可注入的结构化数据：

- **权威映射表** `backend/src_python/reflection_dimension_map.py`：36 个 E 码 + 26 个 reason_code + v188 ontology 三层标签 → `A 买卖信号判断 / B 信息分析方向 / C 技术错误` + 12 个受控小类。取数优先级 E 码 > reason_code > ontology，**多错误码时按字典定义顺序确定性排序**（`decision_errors` 查询无 `ORDER BY`，不排序会导致同一决策两次运行落到不同维度）。
- **Schema**：`trade_reflections_v2` 新增 7 列（`dimension` / `subcategory` / `hit` / `actual_outcome_pct` / `confidence_at_time` / `reflection_text` / `settled`），新库建表即含，老库由 `db.migrate()` 幂等 `ALTER` 补齐。
- **写入方** `paper_reflection_worker.py`：每条反思按同一套口径落维度；拿不到维度就写空串，**绝不用默认桶凑数**。
- **历史回填** `backend/src_python/backfill_reflection_dimensions.py`：`decision_errors` 错误码 → `attribution_reviews.reason_code` → v188 三层标签 兜底。结算信息取自 `quick_sim_trades.verdict`（`trade_reflections_v2` 本身**没有** `is_correct` 列）。幂等：只更新维度为空的行。
- **API**：`/api/v237/reflection-dimension-map`（口径审计）、`/api/v237/reflection-dimension-stats`（按维度/小类统计）、`/api/v291/reflections/summary`、`/api/v237/missed-signals/by-dimension`。
- **未来函数纪律**：
  - P0-2 技术信号确认只用 `tech_signals.uses_future = 0`，且时间上界是 `move_ts` 本身（**不含** `move_ts + 6h`——采信异动之后才出现的信号是典型未来函数）。
  - P0-3 归因块只取 `move_ts < now` **且** `move_ts >= now - days*86400`（`days` 参数必须真正生效）。
  - `created_at` 是 TEXT，一律用字符串比较；时区按项目约定（`config.parse_time_to_bj`）视为**北京时间**。

验证：

```bash
bash scripts/v309_verify_p0.sh              # 静态检查 + 离线自检 (不碰 DB)
bash scripts/v309_verify_p0.sh --with-db    # 额外跑真实 schema 检查 + 回填 dry-run
python3 backend/src_python/check_v309_schema.py           # 只读: 真实列齐没齐
python3 backend/src_python/selftest_v309_endpoints.py     # 端点/归因**真调用**自检
```

> 验证脚本必须看**退出码**，不要只看输出里的 "PASS"。
> 早期版本用 `python3 -m pytest ... | tail -5`，管道的退出码取自 `tail`，
> pytest 报 `1 failed` 也会被判成 PASS —— 现在所有步骤都显式传退出码。

老库补 v309 七列的命令（`db.migrate(conn)` 需要一个连接，不能写成 `db.migrate()`）：

```bash
cd backend/src_python && python3 -c "import db; c = db.get_connection(); db.migrate(c); c.close()"
```

> 端点参数一律写成 `days: Annotated[int, Query(ge=1, le=3650)] = 90`，
> **不要**写成 `days: int = Query(90, ge=1, le=3650)`。后者的默认值是 fastapi 的
> `Query` 对象，HTTP 请求由 FastAPI 注入真实值没问题，但**直接 Python 调用**
> （端点互相调用 / 真调用自检）拿到的是 `Query` 实例 → `Query * 86400` 抛 `TypeError`。
> `selftest_v309_endpoints.py` 现在用 AST 静态拦这一类写法，并且把 `TypeError`
> 从"可接受"改为**必须失败**，不会再出现假 PASS。

## 部署

Linux 生产部署（systemd + nginx）见 [deploy/README.md](./archive/README.md)。

## 子项目

- `frontend/` — Next.js 14 实时信号台。开发 `npm run dev`（:3000），生产 `npm run build && npm start`。环境变量见 `frontend/.env.example`。
- `dashboard/` — Streamlit 离线复盘看板（:8501），直接读 SQLite/Excel 做历史信号分析，不在实时链路内。详见 `dashboard/README.md`。
- `docs/` — 架构设计文档；`docs/archive/` 是已移除的 Hermes 研究 Agent 层设计存档。

---

## 📬 联系与贡献

- **邮箱（项目）：** research@example.com
- **Issues / 提案：** [github.com/stardust888888/reflex-quant/issues](https://github.com/stardust888888/reflex-quant/issues)
- **贡献指南：** [`CONTRIBUTING.md`](../CONTRIBUTING.md)
- **行为准则：** [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)
- **安全问题：** [`SECURITY.md`](../SECURITY.md)

**想成为 33+ 错误码（E001–E033）/ 7 层反思器的协作者？** 元反思循环（E028+）是开放的——如果你抓到
一次所有反思器都漏掉的坏交易，欢迎提一个 issue 描述场景并提议新的 checker。

## 📄 License

MIT — 见 [`LICENSE`](../LICENSE)。

## 🌟 Star History

如果你认可"LLM 驱动的 quant 系统需要反思层"这个方向，请点 ⭐ — 这是我们唯一的
分发渠道，也是吸引潜在合伙人/投资人最直接的信号。

<sub>v399 — 2026-11-16 · 数字口径见下方「现状」一节</sub>
