# API 地图

> 本文件由 `backend/scripts/gen_api_map.py` 生成，**不要手改**。
> 统计：68 个路由文件，422 个端点。

## 按域分布

| 域 | 路由文件 | 端点数 |
|---|---|---|
| `/review` | 3 | 30 |
| `/reflection` | 1 | 26 |
| `/v189` | 4 | 25 |
| `/news` | 1 | 22 |
| `/discovery` | 1 | 17 |
| `/beliefs` | 1 | 14 |
| `/ai` | 1 | 13 |
| `/security` | 2 | 12 |
| `/trident2` | 1 | 11 |
| `/v1` | 1 | 11 |
| `/divergence` | 1 | 10 |
| `/crowd` | 1 | 9 |
| `/paper` | 1 | 9 |
| `/v190` | 1 | 9 |
| `/abnormal-signals` | 1 | 8 |
| `/causal` | 1 | 8 |
| `/event_atoms` | 1 | 8 |
| `/replay` | 1 | 8 |
| `/strategies` | 1 | 8 |
| `/v186` | 1 | 8 |
| `/analysis` | 2 | 7 |
| `/market` | 1 | 7 |
| `/reasoning` | 1 | 7 |
| `/signal-weights` | 1 | 7 |
| `/admin` | 1 | 6 |
| `/history` | 1 | 6 |
| `/events` | 1 | 5 |
| `/health` | 1 | 5 |
| `/macro` | 1 | 5 |
| `/policy` | 1 | 5 |
| `/timeseries` | 1 | 5 |
| `/v187` | 1 | 5 |
| `/v201` | 1 | 5 |
| `/capabilities` | 1 | 4 |
| `/connectivity` | 1 | 4 |
| `/data-health` | 1 | 4 |
| `/decisions` | 1 | 4 |
| `/guards` | 1 | 4 |
| `/kb` | 1 | 4 |
| `/obs` | 1 | 4 |
| `/trading` | 1 | 4 |
| `/v188` | 1 | 4 |
| `/` | 1 | 3 |
| `/agent_chat` | 1 | 3 |
| `/analysis-process` | 1 | 3 |
| `/backtest` | 1 | 3 |
| `/backups` | 1 | 3 |
| `/dashboard` | 1 | 3 |
| `/export` | 1 | 3 |
| `/filter` | 1 | 3 |
| `/institutional` | 1 | 3 |
| `/smart-money` | 1 | 3 |
| `/sources` | 1 | 3 |
| `/targets` | 1 | 3 |
| `/bets` | 1 | 2 |
| `/edge` | 1 | 2 |
| `/attribution` | 1 | 1 |
| `/factory` | 1 | 1 |
| `/ledger` | 1 | 1 |
| `/profit` | 1 | 1 |
| `/signals` | 1 | 1 |

## 路由文件 → 端点数

| 文件 | 端点 | 代表路径 |
|---|---|---|
| `routers/reflection.py` | 26 | `GET /api/reflection/deep/enrich/{reflection_id}` |
| `routers/news.py` | 22 | `GET /api/news/exports` |
| `routers/discovery.py` | 17 | `GET /api/discovery/fed-impact` |
| `routers/beliefs.py` | 14 | `POST /api/beliefs` |
| `routers/ai.py` | 13 | `GET /api/ai/models` |
| `routers/causal_graph.py` | 13 | `GET /api/review/causal-graph` |
| `routers/review.py` | 13 | `GET /api/review/attributions` |
| `routers/trident2.py` | 11 | `GET /api/trident2/features` |
| `routers/views.py` | 11 | `GET /api/v1/scorecard` |
| `routers/divergence.py` | 10 | `GET /api/divergence/assets` |
| `routers/crowd.py` | 9 | `GET /api/crowd/ranking` |
| `routers/exchange.py` | 9 | `GET /api/v190/exchange/status` |
| `routers/market.py` | 9 | `GET /api/v188/markets/status` |
| `routers/ops.py` | 9 | `POST /api/v188/admin/daily-archive` |
| `routers/paper.py` | 9 | `GET /api/paper/buy-sell-kpi` |
| `routers/abnormal.py` | 8 | `GET /api/abnormal-signals` |
| `routers/causal.py` | 8 | `GET /api/causal/chains` |
| `routers/event_atoms.py` | 8 | `GET /api/event_atoms` |
| `routers/experience.py` | 8 | `GET /api/v186/experiences` |
| `routers/replay.py` | 8 | `GET /api/replay/signals` |
| `routers/strategies.py` | 8 | `GET /api/strategies` |
| `routers/market_quotes.py` | 7 | `GET /api/market/prices` |
| `routers/reasoning.py` | 7 | `GET /api/reasoning/review/list` |
| `routers/signal_weights.py` | 7 | `GET /api/signal-weights` |
| `routers/admin.py` | 6 | `POST /api/admin/id-reconcile/migrate` |
| `routers/antibot.py` | 6 | `POST /api/security/behavior` |
| `routers/history.py` | 6 | `GET /api/history/news-trades` |
| `routers/portal.py` | 6 | `GET /api/security/watermark` |
| `routers/events.py` | 5 | `GET /api/events/heatmap` |
| `routers/health.py` | 5 | `GET /api/health` |
| `routers/kg.py` | 5 | `GET /api/analysis/kg-improvements` |
| `routers/llm.py` | 5 | `GET /api/v187/llm-stats/by-purpose` |
| `routers/macro.py` | 5 | `GET /api/macro/context` |
| `routers/policy.py` | 5 | `GET /api/policy/patches` |
| `routers/timeseries.py` | 5 | `GET /api/timeseries/ticks/{symbol}` |
| `routers/trading_v201.py` | 5 | `GET /api/trading/signals` |
| `routers/capabilities.py` | 4 | `POST /api/capabilities/scenario` |
| `routers/coinglass.py` | 4 | `GET /api/v189/coinglass/lsr/latest` |
| `routers/connectivity.py` | 4 | `GET /api/connectivity/status` |
| `routers/data_health.py` | 4 | `GET /api/data-health/summary` |
| `routers/decisions.py` | 4 | `GET /api/decision/{decision_id}/horizons` |
| `routers/guards.py` | 4 | `GET /api/guards/entry/config` |
| `routers/indicators.py` | 4 | `GET /api/v188/indicators` |
| `routers/kb.py` | 4 | `GET /api/kb/asset/{asset}` |
| `routers/obs.py` | 4 | `GET /api/obs/sources` |
| `routers/review_run.py` | 4 | `POST /api/review/run` |
| `routers/trading.py` | 4 | `POST /api/trading/manager/start` |
| `routers/agent_chat.py` | 3 | `POST /api/agent_chat/upload` |
| `routers/analysis_process.py` | 3 | `GET /api/analysis-process/recent` |
| `routers/backtest.py` | 3 | `POST /api/backtest/run` |
| `routers/backups.py` | 3 | `GET /api/backups/{filename}` |
| `routers/calendar.py` | 3 | `GET /api/v189/calendar/events-for-asset` |
| `routers/charts_export.py` | 3 | `GET /api/klines/{symbol}` |
| `routers/filter.py` | 3 | `GET /api/filter/keywords` |
| `routers/institutional.py` | 3 | `GET /api/institutional/13f/managers` |
| `routers/meta.py` | 3 | `GET /api/health/security` |
| `routers/overview.py` | 3 | `GET /api/dashboard/overview` |
| `routers/smart_money.py` | 3 | `GET /api/smart-money/fills` |
| `routers/sources.py` | 3 | `GET /api/sources/status` |
| `routers/targets.py` | 3 | `GET /api/targets/unresolved` |
| `routers/analysis_yield.py` | 2 | `GET /api/analysis/yield` |
| `routers/bets.py` | 2 | `GET /api/bets/scan` |
| `routers/edge.py` | 2 | `GET /api/edge/carry` |
| `routers/actionable.py` | 1 | `GET /api/signals/actionable` |
| `routers/event_reverse.py` | 1 | `GET /api/attribution/reverse` |
| `routers/factory.py` | 1 | `GET /api/factory/report` |
| `routers/ledger.py` | 1 | `GET /api/ledger/overview` |
| `routers/profit.py` | 1 | `GET /api/profit/scan` |

## 怎么用这页

1. 先按域找，`/api/...` 是对前端开放的，`/ops/...` 与 `/admin/...` 是运维口径。
2. 找到文件后直接读那个 router，业务逻辑大多在它调用的 `src_python/*.py` 里。
3. 想确认端点总数有没有变：`python3 scripts/architecture_baseline.py`。
