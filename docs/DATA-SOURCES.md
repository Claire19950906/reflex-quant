# 数据源清单与采集节奏

> 本页由 `backend/scripts/gen_data_sources.py` 从代码生成，**不要手改**。
> CI 里 `--check` 会校验它是否过期（源码改了而本页没重生成，就会红）。

## 一、行情：每个品种走哪个源

路由表的唯一事实源是 `backend/src_python/feeds/quote_sources.py` 的 `ASSET_ROUTE`。
之所以要一张表而不是一个默认源：**不是所有源的合约都存在**——币安没有美股合约，
Yahoo 没有黄金永续，国内现货源没有 BTC。硬走一条路，结果就是“拿不到价还以为是策略错”。

| 品种 | 数据源 | 合约 / 代码 |
|---|---|---|
| `BTC` | `binance_fapi` | `BTCUSDT` |
| `ETH` | `binance_fapi` | `ETHUSDT` |
| `SOL` | `binance_fapi` | `SOLUSDT` |
| `OIL` | `domestic_spot` | `—` |
| `WTI` | `domestic_spot` | `—` |
| `GOLD` | `okx_swap` | `XAU-USDT-SWAP` |
| `XAG` | `okx_swap` | `XAG-USDT-SWAP` |
| `XAU` | `okx_swap` | `XAU-USDT-SWAP` |
| `AAPL` | `yahoo` | `AAPL` |
| `AMZN` | `yahoo` | `AMZN` |
| `COIN` | `yahoo` | `COIN` |
| `GOOGL` | `yahoo` | `GOOGL` |
| `MARA` | `yahoo` | `MARA` |
| `META` | `yahoo` | `META` |
| `MSFT` | `yahoo` | `MSFT` |
| `MSTR` | `yahoo` | `MSTR` |
| `MU` | `yahoo` | `MU` |
| `NVDA` | `yahoo` | `NVDA` |
| `PLTR` | `yahoo` | `PLTR` |
| `RIOT` | `yahoo` | `RIOT` |
| `SKHYNIX` | `yahoo` | `000660.KS` |
| `SMCI` | `yahoo` | `SMCI` |
| `TSLA` | `yahoo` | `TSLA` |
| `US_HYNIX` | `yahoo` | `MU` |

按源归类（“哪几个源各自覆盖什么”一眼就能看出来）：

- **`binance_fapi`** — 3 个品种：`BTC`, `ETH`, `SOL`
- **`domestic_spot`** — 2 个品种：`OIL`, `WTI`
- **`okx_swap`** — 3 个品种：`GOLD`, `XAG`, `XAU`
- **`yahoo`** — 16 个品种：`AAPL`, `AMZN`, `COIN`, `GOOGL`, `MARA`, `META`, `MSFT`, `MSTR`, `MU`, `NVDA`, `PLTR`, `RIOT`, `SKHYNIX`, `SMCI`, `TSLA`, `US_HYNIX`

四级兜底在 `feeds/quote_fallback.py`：主源失败 → 备源 → 缓存价 → 显式报“无价”，
**不编价**。取不到价时下游闸门会拒绝出信号，而不是用上一个旧价顶着。

## 二、新闻：16 个源，靠指纹去重

注册表在 `backend/src_python/feeds/news_sources.py` 的 `SOURCE_KEYS`：

```
financialjuice, tree_news, techflow, eastmoney, blockbeats, jin10, fred, finnhub, feishu_jin10, reuters, bls, yahoo_finance, marketwatch, fxstreet, cointelegraph, oilprice
```

共 **16** 个源。同一条消息常被五六家同时转发，所以入库时算 `content_fingerprint`，
同一指纹只留最早的一条，其余并入 `cross_net_first_seen_ts`（“全网溯源”）——
前端看到的不是“五条新闻”，而是“一条新闻、五个来源、最早出现在 X 时刻”。

## 三、宏观与慢变量：多久问一次

| 任务 | 常量 | 默认节奏 | 能改环境变量吗 |
|---|---|---|---|
| 市场情绪（FNG + Truth Social） | `MARKET_SENTIMENT_POLL_SECONDS` | 1 小时 | 是：`MARKET_SENTIMENT_POLL_SECONDS` |
| 金十日历 | `JIN10_CALENDAR_POLL_SECONDS` | 30 秒 | 是：`JIN10_CALENDAR_POLL_SECONDS` |
| 美股行情（yfinance 兜底） | `YFINANCE_INTERVAL_S` | 5 分钟 | 是：`YFINANCE_INTERVAL_S` |
| 漏检补偿 | `_MISSED_SIG_INTERVAL_S` | 5 分钟 | 否（改常量后重启） |
| 异常签名扫描 | `_ABNORMAL_SIG_INTERVAL_S` | 15 秒 | 否（改常量后重启） |
| K 线回补 | `_KLINE_BACKFILL_INTERVAL_S` | 30 分钟 | 否（改常量后重启） |
| 链上归档 | `_CHAIN_ARCHIVE_INTERVAL_S` | 10 分钟 | 否（改常量后重启） |
| FOMC 升级 | `FOMC_UPGRADE_INTERVAL_S` | 1 小时 | 是：`FOMC_UPGRADE_INTERVAL_S` |
| 联邦资金推送 | `FED_PUSH_INTERVAL_S` | 5 分钟 | 是：`FED_PUSH_INTERVAL_S` |
| 回放快照刷新 | `_REPLAY_REFRESH_INTERVAL_S` | 30 秒 | 否（改常量后重启） |

**注意这是“多久问一次”，不是“数据有多新”。** 高频源（新闻）问得密，
慢变量（情绪、日历）问得稀——问太密只会被封 IP，不会让数据变准。

## 四、滞后：怎么自己量，而不是信一个平均数

本页**不印“平均滞后 N 分钟”这种数**。原因很实在：各源的时间戳口径不一致
（`timestamp` 是采集时刻的 epoch 秒，`published_at` 是源站给的本地时间文本，
个别源甚至是“抓取时才发现发布日期”）。混在一起算平均，会得到一个看起来精确、
实际差 8 小时的假数字。

要量就按下面这条 SQL 自己算，**先按源分组、再挑干净区间**（想更稳就把 AVG 换成中位数算法）：

```sql
-- 单源滞后的平均值（分钟）；m 落在 -5..1440 之外的行先剔掉，那是口径不一致的脏数据
WITH lag AS (
  SELECT source,
         (julianday(datetime(timestamp,'unixepoch','+8 hours')) - julianday(published_at)) * 1440 AS m
  FROM raw_news WHERE published_at <> ''
), clean AS (SELECT source, m FROM lag WHERE m BETWEEN -5 AND 1440)
SELECT source, COUNT(*) n,
       ROUND(AVG(m),1) avg_min
FROM clean GROUP BY source ORDER BY n DESC;
```

口径对不上的源（平均值为空或离谱），说明它给的不是事件时间而是页面时间——
这种源只适合做“佐证”，不该进时效性判断。

## 五、明确的缺口

- **没有付费终端**（Bloomberg / Refinitiv / Wind）：只有公开源与 RSS，
  所以“机构级快讯”这一档天然慢几十秒到几分钟，不假装能赢。
- **没有逐笔成交（tick）历史**：只有 1 分钟及以上 K 线，
  做不了微观结构这类基于订单流的分析。
- **情绪类源反爬严重**：Truth Social 靠低频轮询，断就断，不重试到封号。

相关：[架构](./ARCHITECTURE.md) · [快速上手](./QUICKSTART.md) · [FAQ](./FAQ.md)
