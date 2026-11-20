# 引擎重启与队列排空 Runbook

> 适用范围：Reflex Quant（Trident）引擎在 macOS launchd 下的重启、验证与排障。
> 2026-11-05 补充 v322「待分析永不下降」事故的根因链、修复与回归测试位置。

## 0. 服务与日志

| 项 | 值 |
|---|---|
| launchd 服务 | `com.trident.engine` |
| 启动脚本 | `/Users/claire/.trident/wrappers/run_engine.sh` |
| 工作目录 | `/Users/claire/Desktop/金融1.0/backend/src_python` |
| 解释器 | `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` |
| 标准输出 | `/Users/claire/.trident/log/engine.out.log` |
| 标准错误 | `/Users/claire/.trident/log/engine.err.log` |
| 数据库 | SQLite / WAL，路径见 `backend/src_python/config.py` 的 `DB_PATH` |

```bash
# 重启（launchd KeepAlive=true，会自动拉起）
launchctl kickstart -k gui/$(id -u)/com.trident.engine

# 跟随日志
tail -f /Users/claire/.trident/log/engine.out.log

# 崩溃重启循环自检（正常应当只有个位数 "engine starting"）
grep -c "engine starting" /Users/claire/.trident/log/engine.out.log
```

## 1. 队列健康度判据

```sql
-- 队列分布：PENDING 必须单调下降，DONE 必须单调上升
SELECT status, COUNT(*) FROM raw_news GROUP BY status;

-- 最后一条 DONE 的时间 —— 判断是否还在推进
SELECT MAX(created_at) FROM raw_news WHERE status = 'DONE';

-- 看门狗僵尸候选（processing_at 非空 = 已被认领且未完成）
SELECT COUNT(*) FROM raw_news
 WHERE status = 'PROCESSING' AND processing_at IS NOT NULL;

-- 队头阻塞：最老的一批 PENDING（retry_count 高 = 反复失败）
SELECT id, retry_count, processing_at, substr(content, 1, 40)
  FROM raw_news WHERE status = 'PENDING' ORDER BY id ASC LIMIT 20;

-- 重试计数分布：确认认领没有把 retry_count 清零
SELECT retry_count, COUNT(*) FROM raw_news
 WHERE status = 'PENDING' GROUP BY retry_count;
```

## 2. v322 事故：「待分析」永不下降的根因链

2026-11-05 生产实测：**PENDING 只增不减，DONE 停在 3514**。四环相扣，缺一不可：

1. **行情源整体不通**
   `query1.finance.yahoo.com` 与 `www.okx.com` 均连接超时（5s），只有 MiniMax 通。
   而 `_yahoo_get_json()` 把**连接类失败**（timeout / DNS / ConnectionError）与 HTTP 429
   混在同一个重试循环里：`4 × 2 = 8` 次尝试 × 10s timeout + 退避 ≈ **89 秒/次**；
   OKX 侧 ≈ 31 秒/次 → 每条新闻落库多耗 1-2 分钟 → 单批 16 条跑 **42 分钟**。

2. **看门狗把在办批次当僵尸回收**
   看门狗规则是「认领后 15 分钟未完成即僵尸 → `retry_count+1` → 改回 `PENDING`」。
   42 分钟 > 15 分钟 → **批还在跑，行已被改回 `PENDING`**。
   现场证据：`raw_news.processing_at` 全表 6913 行**全是 NULL** —— 只有看门狗的
   `processing_at = NULL` 会这样。

3. **批事务白干**
   批事务末尾 `UPDATE raw_news SET status='DONE' ... WHERE id=? AND status='PROCESSING'`
   此时匹配 **0 行** → 这一批一条都进不了 DONE。

4. **无限重认领**
   下一轮仍按 `id ASC` 认领同一批（队头）→ 反复重跑 → 「待分析」永不下降。

> ⚠️ 已被证伪的假设：**不是 baseline 聚合慢**。`signal_performance` / `backtest_event_outcomes`
> 两张表都是空的，那次 90 天 5 表 JOIN 实测只花 **0.001 秒**。baseline 的 1 小时节流保留为
> 防御性措施，但不能再当作耗时归因。

### 2.1 生产现场二次取证（`engine.out.log` / `engine.err.log`）

日志给出的**更靠前**的两个杀手 —— 它们让「等引擎修好」这件事本身就不成立：

**(A) `child_count` 脏值让整批落库作废**（比看门狗更早发生）

```
[14:14:55] [AI] Batch persist 失败，跳过本批: ValueError: invalid literal for int() with base 10: 'GOLD|XAU|BUY'
[14:14:55] [AI] Batch done | 16/16 ok | 0 failed | wall=150.6s
```

`ai_decisions.child_count` 历史上被写坏过（存的是 `'GOLD|XAU|BUY'` 这类**聚合键**而不是数字），
`int()` 抛 ValueError → **整个批事务回滚** → 16 条全部进不了 DONE。
注意批次本身只跑了 **150.6 秒**（很快）—— 说明"慢"从来不是主因，"落库被打回"才是。
源码已用 `_safe_child_count()` 把脏值当 0 处理（v322），**但引擎未重启 → 修复未生效**。

**(B) 文件描述符耗尽（Errno 24）**

```
OSError: [Errno 24] Too many open files
socket.accept() out of system resource        # 9000 端口 webhook 服务
[REFLECT] worker error: OperationalError: unable to open database file
[STRUCT-REFLECT] worker error: OperationalError: unable to open database file
RuntimeWarning: The executor did not finishing joining its threads within 300 seconds.
```

FD 打满后：**新开 socket 失败**（webhook 收不到消息）、**新开 SQLite 失败**
（`unable to open database file`）、**线程池收不回来**（executor 300s 未 join）。
同时还有 `database is locked`（写锁争用）。
行情源是最高频的外部调用点，v322 已给 Yahoo/OKX 的 response 加显式 `close()`
（原来只靠 GC 回收底层连接，攒够就爆 FD）。

**(C) 结论：跑着的进程是陈旧的**

日志里仍在打 `[INSIGHTS-HIT-WRITE] failed: NameError: name '_asset_insight_hit_ids' is not defined`，
而该 NameError **在源码里早已修好**（`ai_worker.py` 10664-10674 注释写明）。
→ 当前进程加载的是**修复前的代码**。所有修复（`_safe_child_count` / 看门狗跳过在办批次 /
行情源快速失败 / baseline 节流）**都必须重启后才生效**。

## 3. v322 修复内容

### 3.1 行情源：连接类失败快速放弃

`backend/src_python/quote_sources.py`

- 新增 `_is_conn_error(exc)`：识别 timeout / DNS / ConnectionError / SSL 等**连接层**失败，
  与「已拿到响应的业务错误」（HTTP 4xx/5xx、JSON 解析错误）严格区分 —— 后者不该被误伤成快速失败。
- Yahoo：`_YAHOO_CONN_FAIL_BUDGET` 次连接失败后立即 `break`，交熔断 + 调用方降级
  （原来跑满 8 次 ≈ 89 秒）。
- OKX：第一次连接失败即 `break`（原来 ≈ 31 秒）。

### 3.2 看门狗：跳过「在办批次」

`backend/src_python/engine/ai_worker.py`

- `_ACTIVE_BATCH_IDS` 登记表（news_id → 心跳时间戳）+ `_ACTIVE_BATCH_LOCK`。
- 认领后 `_active_batch_register(batch_ids)` 登记；批处理落库后 `_active_batch_release(...)` 注销。
- 看门狗回收 SQL 由模块级 `_build_reclaim_sql(active_ids)` 构造，自动追加
  `AND id NOT IN (…)` 排除在办批次。
- **租约 + 心跳**（自愈，防止「注销漏执行」导致行永久卡在 PROCESSING）：
  - 登记时起 daemon 心跳线程，每 30 秒刷新租约；
  - 心跳发现 ① 被新批次取代 ② 批次主线程已死 ③ 超过 1 小时硬上限 → 主动注销本批，
    把残留的 PROCESSING 交回看门狗回收；
  - 租约 30 分钟无进展 → 同样不再受保护。

> 为什么不写 `processing_at` 心跳：批事务全程持 SQLite 写锁，其它连接写不进去，
> 心跳永远提交不了（还会放大锁冲突）。同进程内存表没有这个问题。

### 3.3 其它 v322 修复（同一批落地，均需重启生效）

| 修复 | 位置 | 作用 |
|---|---|---|
| `_safe_child_count()` | `ai_worker.py` | `child_count` 脏值（`'GOLD\|XAU\|BUY'`）当 0 处理，不再让**整批**落库回滚 |
| response 显式 `close()` | `quote_sources.py` | 释放 HTTP 连接，压制 `Errno 24 Too many open files` |
| `_is_conn_error()` | `quote_sources.py` | 连接层失败 vs 业务错误严格区分，避免误伤正常重试 |
| baseline 1 小时节流 | `ai_worker.py` | 防御性；实测不是耗时主因 |

## 4. 回归测试

```bash
cd /Users/claire/Desktop/金融1.0/backend
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest \
  tests/test_v322_watchdog_active_batch.py -q
```

覆盖三条不变量：
- A. 回收 SQL 必须排除在办批次（真僵尸仍被回收 —— 含真实 SQLite 行为验证）；
- B. 认领后登记、落库后注销、租约过期/被取代后自动失去保护；
- C. 行情源连接类失败快速放弃，且业务错误不被误判。

## 5. 重启后验证清单

```bash
launchctl kickstart -k gui/$(id -u)/com.trident.engine
tail -f /Users/claire/.trident/log/engine.out.log
```

逐条确认：

| # | 检查项 | 期望 |
|---|---|---|
| 1 | 日志出现 `[AI] Claimed N items` | 正常认领 |
| 2 | 单批耗时 `wall=...s` | 行情源修好后应回到**分钟级**，远小于 15 分钟看门狗阈值 |
| 3 | 日志出现 `[WATCHDOG] 回收卡死 PROCESSING` | **不应**回收正在处理的批次 |
| 4 | `DONE` 计数 | 单调上升 |
| 5 | `PENDING` 计数 | 单调下降 |
| 6 | `retry_count` | 不再无故递增（`retry_count + 1 >= 3` 才转 `FAILED`） |
| 7 | `engine.err.log` | 无 `database is locked`、无 `batch persist failed` |
| 8 | `engine.out.log` | 无崩溃重启循环（`engine starting` 计数不增长） |

## 6. FD 耗尽（Errno 24）诊断与治理

```bash
# 1) 进程 FD 上限与当前占用（引擎进程）
ENGINE_PID=$(pgrep -f "src_python/engine.py" | head -1)
ulimit -n                      # 软上限（macOS 默认 256）
ls /proc/$ENGINE_PID/fd 2>/dev/null | wc -l    # Linux 有效；macOS 用下面这条：
lsof -p "$ENGINE_PID" | wc -l
lsof -p "$ENGINE_PID" | awk '{print $5}' | sort | uniq -c | sort -rn | head   # 按类型看谁在占

# 2) 抬上限（launchd 下要写进 plist，不能靠 shell ulimit）
#    /Users/claire/Library/LaunchAgents/com.trident.engine.plist 增加：
#      <key>SoftLimitNumberOfFiles</key><integer>10240</integer>
#      <key>HardLimitNumberOfFiles</key><integer>10240</integer>
#    然后 launchctl unload/load 重新加载 plist

# 3) 确认真凶：按 FD 类型排序（大量 ESTABLISHED/SOCKET → 行情源或推送连接泄漏）
```

治理要点：
- v322 已给 Yahoo/OKX 的 response 加显式 `close()`（原先依赖 GC 回收底层连接）。
- `requests.Session` 已设 `Connection: close`，避免 keep-alive 死连接堆积。
- 仍需排查：飞书/微信/邮件的 fire-and-forget 推送任务（`asyncio.create_task(send_feishu_alert(...))`）
  在网络异常时是否堆积 —— 现场日志里 `[WECHAT] ERROR: HTTP Error 400` 连续刷屏。

## 7. 已知外部阻塞（不属于本次代码修复范围）

- **Yahoo / OKX 直连超时**：快速失败只能止损，不能恢复报价；报价缺失时决策落
  `no_trade_reason='行情源不可用，未能取到价格'`。
- **通道 401**：部分 key 失效（账号 2 返回 401），需换 key。
- **配额逼近上限**：熔断打开时引擎**不认领**（零额度消耗），冷却后自动恢复。
- **MiniMax key 暴露在配置中**：需轮换。
