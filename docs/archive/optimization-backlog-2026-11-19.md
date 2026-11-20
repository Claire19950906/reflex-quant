# 优化待办（2026-11-19 夜）

## 本轮已完成（已落盘、已验证）

| 事项 | 产物 | 关键数字 |
|---|---|---|
| 决策级影子打分（1h/4h/24h，不等开仓） | `backend/src_python/shadow_score.py`、`scripts/shadow_score.py`、`tests/test_v488_shadow_score.py` | 5 passed；`overnight_report.py` 已接入 |
| 日报降级原因 + AI 失败归因 | `scripts/daily_funnel_report.py` | 今日降级：`target_none` 8（88.9%）、`llm_empty` 1；失败：`network` 2,026（60.7%）、空码 901（27.0%）、`think_only` 195 |
| 双台账一致性校验 | `scripts/overnight_report.py`「2b」段 | 成交无打点 0、打点指向不存在成交 5、指纹不一致 0 |
| 预筛收益体检（只记录不拦截） | `scripts/prefilter_impact.py` → `backend/reports/ops/prefilter_impact.md` | 样本不足（NONE 8 / 有效 3），暂不灰度 |
| K 线脏数据隔离 | `backend/scripts/kline_gap_audit.py`（新增全库 `--apply` + 单次快照） | 隔离 **196 行**（WTI 1d 91 / WTI 4h 94 / 其余 11），快照 449MB 可回溯 |
| 新闻积压分级排程 | `~/.trident/wrappers/run_news_triage.sh` + `com.trident.news-triage.plist`（30 分钟） | 已建未加载：暂停期作业数上限 16，留到 `resume_data.sh` 的 sweep 自动拉起 |
| 引擎暂停不再空转 | `com.trident.engine.plist`（`KeepAlive={SuccessfulExit:false}`，备份 `.bak-20261119`） | 实测 `runs=1`，不再每 30s 刷 `ENGINE-REFUSED` |

**关于 FRED（原计划修"17 组旧路径"，实测结论是：不用修）**
FRED CSV 通道本身可用：`requests` 直连被掐，但 `fred_csv._curl_latest` 兜底能拿到
（实测 DGS10 = 5.1）。审计里的 13 组属于"旧 source 行 + 另有新鲜源"，只要读端
不按 source 过滤就不会读到旧值；真断粮仍只有 `derived_crypto.stable_supply_30d_pct` 一组。

本文件只记录**这一轮没动、但已有证据的项**。

排序原则：先解决"明天能不能拿到结论"，再解决"系统稳不稳"，最后才是结构债。


---

## P0-1 失败归因（代码层）— 已被并发编辑阻塞

**证据**：今日失败 3,310 次，其中 `http_error/network` 2,026（60.7%）、
空码 901（27.0%）、`empty_response/think_only` 195（5.8%）、429 仅 22。

**要做什么**：调用层捕获异常时记录 `异常类型 + HTTP 状态码 + 重试次数`，
不要统一写成 `network`。空码按 `db.log_ai_call` 的兜底写成 `unknown`（已做），
但 `unknown` 本身不能定位。

**为什么没做**：`engine/ai_worker.py` 正在被另一个会话改写（17:41 落盘），
现在改会互相覆盖。**等它停手后再动**，改动面估计 1 个函数。

**验收**：次日 `http_error/(空)` 占比 < 5%，且 429/5xx/timeout 能各自成行。

---

## P0-2 影子打分落库为常驻作业（当前按需计算）

**已完成**：`backend/src_python/shadow_score.py` + `scripts/shadow_score.py`，
按 1h/4h/24h 对 BUY/SELL 决策打分，不依赖开仓；`overnight_report.py` 已接入。

**还没做**：没有 launchd 定时器，只在跑报告时即时计算（274k 根 K 线，耗时秒级）。
好处是不占常驻资源、不会和写锁抢；坏处是历史不累积。

**如果要常驻**：加 `com.trident.shadow-score`（StartInterval 600，跑
`scripts/shadow_score.py --commit`），并在 `db.migrate` 里建 `shadow_scores` 表。
风险低，但要等数据恢复后观察一轮，避免和 `signal_weight_cron` 抢锁。

---

## P1-1 作业合并（有清单，未执行）

**证据**：`backend/reports/ops/job_lock_advice.md` —— 锁错误累计 3,372 次的
`event_kline_impact`（900s）、463 次 `event_market_attributor`（600s）、
314 次 `atom_backfill`（900s）；30s/45s/60s 三个采集作业本身锁错误不高，
但它们是"短事务高频"，和长事务作业叠加时才爆。

**执行顺序**：
1. `event_kline_impact`：确认是否仍有长事务（它已按事件 commit，需看是否有
   外层未提交事务）。改完再看锁错误增量。
2. 把 `ensure-core`(30s) / `domestic-refresh`(45s) / `equity-quote-cron`(60s)
   合成一个 batcher，一次事务写三份，减少写者切换。
3. 其余批处理统一用 `execute_with_retry`，不要各自调 `busy_timeout`。

**验收**：`overnight_report.py` 的"锁冲突（相对基线）"次日 < 50 次。

---

## P1-2 暂停机制（已改，需观察）

**已做**：`com.trident.engine.plist` 的 `KeepAlive` 由 `true` 改为
`{SuccessfulExit: false}`（备份 `com.trident.engine.plist.bak-20261119`）。
引擎因 `DATA_PAUSED` 退出码 0 → launchd 不再每 30s 拉起一次。
实测重载后 45 秒内 `runs` 停在 1，日志不再刷 `ENGINE-REFUSED`。

**待观察**：真崩溃（非 0 退出）仍会重启；但如果引擎因 bug 以 0 退出就再也不会
自愈 —— 需要 `engine-watchdog`（120s）兜底，下次确认它是否覆盖这种场景。

---

## P2-1 前端 39 处独立轮询

**证据**：`frontend/` 39 个 `page.tsx` 各自 `setInterval`。

**改法**：抽一个 `usePolling(key, ms)`，同一 key 共享一个定时器 + 页面隐藏时暂停；
数据量大的页面改 SSE。**改动面 39 个文件，今晚不动**（没有前端回归测试，
改错会白屏）。

**验收**：同一页面 `fetch` 次数下降 ≥50%，且首屏时间不变差。

---

## P2-2 bridge / routers 22 条重复路径

**证据**：`bridge_server.py` 81 端点，与 `routers/` 有 22 条同路径。

**改法**：先加一个启动断言：同一 `(method, path)` 只允许注册一次，第二次启动直接
报错并打印冲突清单 —— 先把"隐性覆盖"变成"显性失败"，再逐个删。

**风险**：删错会让前端 404。建议先只加断言（不改行为），跑一周无冲突再删。

---

## P2-3 库体积 581MB / 205 表

**证据**：retention dry-run 只值 6.2MB（1.1%）；大头在 `external_indicators`
（161k 行）、`flow_snapshots`（36k 行）之外的历史事实表。

**结论**：删老数据解决不了体积问题。真要降，得做"冷数据归档到独立库 + VACUUM"，
属于结构性改造，且必须先确认没有代码读归档表。

---

## P2-4 gate_calibration 全部 applied=0

**证据**：6 组（XAU BUY n=109、ETH SELL 37、WTI BUY 37、XAU SELL 36、BTC SELL 31、
BTC BUY 30），全部 `applied=0`。

**判断**：样本 30~109，单组 lift 从 -7.5% 到 +10.5%，**都在噪声范围内**。
现在打开自适应只会过拟合。等影子打分累计 ≥ 数百条后再评估。

---

## P2-5 反思链全 0

**证据**：`trade_reflections`/`strategy_reflections`/`decision_outcomes_horizon` 全 0。

**原因**：这些表依赖"持有期走完"或"有开仓结算"，`1w+` 持有期 + 几笔成交 →
自然为空。**不是代码坏了**。

**可做**：让反思链读 `shadow_scores`（短窗口已出结果），把"反思"从"等一周"
变成"当晚就有素材"。这需要改反思的取数口径，改动面中等，**留到数据恢复后再做**。
