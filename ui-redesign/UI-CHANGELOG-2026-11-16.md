# 界面改版变更清单 · 2026-11-16

Trident 前端（`frontend/`，Next.js 14 App Router）。本轮全部改动只落在组件文件与 `lib/` 数据层，
**没有改后端业务逻辑、没有新增后端接口**（唯一例外是 `lib/api.ts` 的 fallback 名单）。

## 改动一览

| # | 文件 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | `components/quant/market-hours-bar.tsx` | 顶部市场条的 500 报错条 → 安静状态点 +「市场状态暂不可用 · 重试」，原始错误移入 `title`；窄屏隐藏长文案，只留 `USOIL / GOLD / BTC / US / KR` chip | 每页顶部常驻红字「市场状态加载失败: HTTP 500」，占位又吓人；窄屏把有效信息压没了 |
| 2 | `components/replay/replay-panel.tsx` | 12 个信号源 chip 从一条 flex-wrap 拆成「执行 / 信号源 / 参数 / 账户」四个带标题的组框；chip 加 `whitespace-nowrap` | 「飞书金十」被压成竖排两字；8 个数字输入挤在 10px 里无标签 |
| 3 | `components/replay/replay-panel.tsx` | 止盈数值补 `TP1× / TP2× / 比例1 / 比例2` 微标签；数值输入框 48px → 56px | `0.6 / 1 / 0.5 / 0.3` 是裸数字，`0.1725` 被截断 |
| 4 | `components/replay/replay-panel.tsx` | 「反思与改进」从下单区正下方下沉到页尾（与「反思→策略补丁」同族相邻） | 它占掉了首屏第二块，把持仓与信号顶到第三屏 |
| 5 | `components/replay/fed-rate-panel.tsx`<br>`components/replay/cross-asset-anomaly-panel.tsx`<br>`components/replay/reflection-improve-panel.tsx` | 整块红底错误框 → 一行灰点 +「数据源暂不可用」+ 重试按钮，原始 500 收进灰色小字/`title` | 三个 500 时整页像崩了，实际只是外部数据源没连上 |
| 6 | `components/quant/live-desk.tsx` | 首屏顺序改为：时段条 → 主工作区（EventBus + 行情）→ 反思战绩 → 错误仪表盘 → 漏检反思；`reflectOpen` 默认 `false`；去掉 `order-*` | 层级扁平、反思模块默认展开吃满一屏；`order-*` 让 DOM 顺序和视觉顺序不一致 |
| 7 | `components/quant/stat-cards.tsx` | KPI 网格 `lg:grid-cols-4` → `sm:grid-cols-2`；LLM 通道列表只列前 4 个，其余收成「+N 个通道 · 去联通页看 →」 | 右栏只有约 470px，4 张 KPI 每张 110px，文字挤成一团；10 行红字霸屏 |
| 8 | `components/quant/paper-equity-card.tsx`（新建） | 「纸面账户 + 权益曲线」卡：权益/涨跌、已结算笔数累加曲线、已实现·浮盈亏·可用·占用四项；结算不足 2 笔显示「样本不足以成线」；账户快照未就绪单列「计算中，15s 自动刷新」态 | 首页完全没有账户与持仓视图；接口有「先回持仓、后补 account」的快版，原样显示会变成假空值 |
| 9 | `components/quant/event-bus.tsx` | 空态重写：「暂无新事件（实时推送通道，上游没播报时本来为空，不是加载失败）」/「事件流暂不可用（后端 5xx 或重启，通常 1–2 分钟自愈）」 | 原来只有一行「等待数据」，像界面坏了 |
| 10 | `lib/api.ts` | `FALLBACK_PATHS` 增补 `/replay/positions`、`/replay/signals`、`/replay/settings`、`/replay/stats`、`/cross-asset/anomaly`、`/v188/markets/status` | 8000 的 `api_server` 整机掉线时，这些接口没有兜底 → 整页空数据；而 8100 桥接读同一份 SQLite |
| 11 | `lib/replay-data.ts` | `fetchReplayPositions()` 改为按 `[API_BASE, BRIDGE_BASE]` 顺序试，任一成功即返回 | 同上；持仓/账户是首页与模拟盘页的第一数据源 |
| 12 | `components/quant/market-hours-bar.tsx` | 市场状态请求改为主备顺序（8000 失败自动打 8100） | 8000 挂时顶部整条市场信息全丢 |

> 注：本仓库有自动提交脚本，每次改动会被并入一条带后端作业信息的提交，
> 所以不能用 commit 号逐个对应本清单 —— 文件与改动点才是准绳。查某个文件的历史用
> `git log --oneline -- <文件>`。

## 验证方式

- 生产构建 `npx next build` + `npx next start --port 3030`，每轮改动后都重新构建并重启（`3030` 是 `next start`，非 dev，文件改动不会热更新）
- 无头 Chrome 1680×1050 / 1280×900 / 655×654 三档截图，逐轮比对
- 兜底验证：在 `curl http://127.0.0.1:8000` 返回 `000` 时截图，首页仍有市场条 chip、事件流 4802 条、账户 9,683.15

截图证据（`/Users/claire/Desktop/金融1.0/.shots/`）：
`wide_home2.png`（KPI 2×2）、`wide_home5.png`（权益卡有数据）、`home_n.png`（655 窄屏）、
`replay_w2.png`（反思下沉 + 降噪）、`replay_fb.png`（8000 挂时账户仍有数据）、
`home_final.png` / `home_ok.png`（兜底 / 全绿）。

## 回滚

全部改动都是「明文可读的组件内替换」，没有跨文件耦合，单文件即可回退：

```bash
cd /Users/claire/Desktop/金融1.0/frontend

# ① 查该文件在本轮之前的版本
git log --oneline -5 -- components/quant/live-desk.tsx

# ② 回退单个文件到某次提交的父版本
git checkout <commit>^ -- components/quant/market-hours-bar.tsx

# ③ 权益卡是新增文件，删掉即可（同时删掉 live-desk.tsx 顶部的 import 与 <PaperEquityCard />）
rm components/quant/paper-equity-card.tsx
```

回退后必须重新构建并重启（3030 是 `next start`，不会热更新）：

```bash
npx next build && npx next start --port 3030
```

## 遗留 / 已知问题

1. **后端启动慢，不是死**：`api_server` 启动迁移要写锁，24 个常驻采集进程 + 高频周期作业同时打 1.4GB 的 `trident_event_bus.db`，日志刷 `database is locked`，实测从 `kickstart` 到端口监听约 4 分钟。仓库里 08:30 的提交把这点定性为「锁竞争源自 74 个高频周期作业」。前台只能兜底（见 #10–#12）；根治在后端：迁移改短事务 + 重试、`busy_timeout` 上调、采集器按用途分库。
2. **权益曲线只有 2 个采样点**：账户当前仅 2 笔已结算（`initial 10000 → 9683.15`，已实现 −316.85），曲线是两点直线。这是真实数据量，不是渲染问题——每结算一笔会多一个点。
3. **持仓表还没做**：首页右栏目前是「账户 + KPI + 信号」，没有「我实际持有什么」的列表。要做需要新的表格组件，且当前持仓为 0，得等有持仓时才能验证渲染。
