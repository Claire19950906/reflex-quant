# 界面改版变更清单 · 2026-11-17

Trident 前端（`frontend/`，Next.js 14 App Router）。本轮改动只落在组件文件与共享 hook，
**没有改后端业务逻辑、没有新增后端接口**。落地细节见 `P2-P3-体验收尾-落地记录.md`。

## 改动一览

| # | 文件 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | `components/app-nav.tsx` | 「更多 → 研究」补入 `/causal-chains`（因果链）、`/asset-graph`（资产图谱）两个入口 | 两页代码在、导航里进不去，只能敲 URL |
| 2 | `components/quant/event-bus.tsx` | 默认从「展开全部」改为收起限高态，首屏只露前 20 条；点「全部」才展开全量；`VISIBLE_INITIAL` 500 → 20 | 事件流全量正文铺满首屏，KPI / 持仓要往下找 |
| 3 | `components/quant/live-desk.tsx` | 新增 `CollapsibleSection` 折叠容器；「复盘战绩」「错误归因」两个诊断面板默认折叠 | 诊断内容占满首屏，第一眼看不到账户结论 |
| 4 | `components/replay/replay-panel.tsx` | 参数输入框补常驻单位：条 / 秒 / ×MFE / 占比 / ×ATR；「重置模拟盘」改红色描边实心 + 文案标注「清空」 | 裸数字不知口径；重置是破坏性操作，与日常开关同色易误点 |
| 5 | 多个页面 / 组件 | 残留 8/9/10px 硬编码字号统一升到 11px，复核 0 处 | P0 降噪遗留的小字号，部分场景低于可读下限 |
| 6 | `components/quant/use-replay-positions.ts`（新建） | 共享取数 hook：快照未就绪 15s 追一次、就绪后 60s 慢刷新，带 in-flight 防叠加 | 三个卡片各自打同一个 `/replay/positions`，首页 3 份并行轮询 |
| 7 | `components/quant/account-kpis.tsx`<br>`components/quant/positions-table.tsx`<br>`components/quant/paper-equity-card.tsx` | 去掉各自的 `useEffect + setInterval`，改读顶层传入的 `snapshot / err / loading` | 配合 #6，取数只在 hook 一处发生 |
| 8 | `components/quant/live-desk.tsx` | 顶层挂 `useReplayPositions()`，把快照下发三张卡片 | 同上 |
| 9 | `components/quant/paper-equity-card.tsx` | 权益曲线/渐变/端点由写死绿红 hex 改用语义色 `var(--long)/var(--short)` | 暗色下曲线颜色不随主题提亮，与文本多空色不一致 |
| 10 | `components/replay/policy-patches-panel.tsx`<br>`failure-clustering-panel.tsx`<br>`reflection-errors/draft-summary-banner.tsx` | zinc 结构色、彩色 chip/文本/边框补 `dark:` 兜底 | 暗色下浅底浅字、边框融于背景 |
| 11 | `app/globals.css`<br>`components/quant/stat-cards.tsx`<br>`components/quant/causal-graph-panel.tsx` | 新增 `--duration-fast/base/slow`、`--ease-out` 令牌；两处例外时长与 `.pulse-dot` 缓动改用 token | 时长/缓动散落在组件内联值，后续统一调档要逐处翻 |
| 12 | `components/quant/live-desk.tsx` | 「复盘战绩」「错误归因」折叠态按标题存 `localStorage`，刷新后保持；读写容错回退默认折叠 | 每次刷新诊断面板都回到折叠，要重开 |

## 验证方式

- `npx tsc --noEmit`：通过，无报错。
- `npx vitest run`：**49 文件 / 706 用例全绿**。
- `NEXT_DIST_DIR=.next-prod npx next build --no-lint`：通过，31 个路由产出。
- 3030 用 `.next-prod` 新产物重启；无头 Chrome 1680×1050 截图核对。
- 三个卡片 `fetchReplayPositions` 引用均为 0，取数唯一来源为 hook。

截图证据（`.shots/`）：`p3-prod-home.png`、`p3-prod-replay.png`、`p3-poll-home.png`、`p3-dev-home.png`、`p3-dev-replay.png`、`p3-dark-replay.png`。

## 遗留 / 已知问题

1. **密集数据态未验证**：当前空仓、事件流为空，满持仓 / 上千条事件下的排版要等后端数据饱满后，再复核 1680 / 1280 / 655 三档。
2. **深色主题未逐卡复核**：字号 / 配色、图表色板、150ms 动效仍沿 P0 约定，未在暗色下过一遍。
3. **折叠态不持久化**：`CollapsibleSection` 用本地 `useState`，刷新后回到默认折叠；需要记忆展开态再上 localStorage。
4. **慢轮询节奏**：合并后就绪回落 60s（旧账户卡为 120s）；后端压力大可再调回 120s。
