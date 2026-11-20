# 金融1.0 引擎运维手册（v322 · 2026-11-04 事故修复后）

> 这次事故的完整根因链、修复清单和验证方法，都在这份文档里。
> 排障时按「先体检 → 再看积压 → 再看引擎」的顺序走，3 分钟能定位问题。

---

## 0.0 🔴 「待分析」不降 —— 十一层根因链（2026-11-04/05/07 三天）

「待分析」不降**不是一个 bug，是十一层独立缺陷叠在一起**。每修一层，下一层才显形。
**只修表层（看到「待分析不降」就去改认领 SQL）必然白费功夫。**

| 层 | 根因 | 症状 | 章节 |
|---|---|---|---|
| 1 | `ai_decisions` INSERT 的**列与参数错位**（`child_count` 收到聚合键）→ `int()` 抛 ValueError → 整批回滚 | `Batch persist 失败` 累计 175 次 | [10.11](#1011-待分析永不下降--队头阻塞死循环2026-11-05) |
| 2 | 队头阻塞：认领 `ORDER BY id ASC` + 落库失败立刻置 NULL → 同一批反复霸占 | 永远认领同一批 id，新新闻排不上 | [10.11](#1011-待分析永不下降--队头阻塞死循环2026-11-05) |
| 3 | 熔断状态**跨进程落盘**且**不区分凭据** → 昨天 401 写的 24h `open` 钉在磁盘 → 换了新 key 仍判熔断 → **跳过认领** | `通道体检 ready=False reason=全部通道熔断` | [10.12](#1012-待分析不降的第三层根因熔断状态钉在磁盘上2026-11-05-白天) |
| 4 | **Python 作用域陷阱**：函数内 `_ts = None` 遮蔽了模块级导入的 `_ts` **函数** → 同函数内 `_ts()` 抛 UnboundLocalError | `Batch persist 失败: UnboundLocalError` | [10.13](#1013-待分析不降的第四层根因python-作用域陷阱2026-11-05-下午) |
| 5 | **冷却短于单批耗时**（120s < 390s）→ 冷却形同虚设 → 同一批无限重跑 | `claimed ids=[11974…]` 反复出现 | [10.14](#1014-待分析不降的第五层根因冷却短于单批耗时2026-11-05-晚) |
| 6 | **评审（verify）吃光主分析预算** → 全局 token 闸门到线 → 主循环整体停摆 | `额度闸门：日 token 预算已用尽 (20077002/20000000)` | [10.15](#1015-待分析不降的第六层根因评审吃光主分析预算2026-11-05-晚) |
| 7 | 额度预警**只发微信一个通道**且 400 被吞 → 你被偷袭却毫不知情 | `[WECHAT] ERROR: HTTP 400`（满屏） | [10.16](#1016-为什么你毫不知情就被额度打满预警静默失效) |
| 8 | **门卫把「模型没回答」当成「不是噪音」** → 2340 条真实新闻被判"排除" → 永不分析却占着计数 | 前端满屏「待 AI 分析」，前端已分析 0 | [10.18](#1018-门卫把模型没回答当成不是噪音--2340-条真实新闻被丢弃) |
| 9 | **认领无时效门槛** → 引擎一条条分析 14 天前的老新闻 → 建仓闸全丢 → **额度照烧信号一条用不上** | `已 21315 分钟（> 45 分钟）仍入库统计不交易` | [10.19](#1019-认领时效门槛--别把额度烧在用不上的老新闻上) |
| 10 | **行情快照 97 秒**（每周期各 8 秒 + 多资产串行 await 的累加）→ 单批 154 秒 | `SNAPSHOT PARTIAL in 97.609s` | [10.21](#1021-行情快照-97-秒--每周期各-8-秒的累加陷阱) |
| 11 | **僵尸行占着「待分析」计数** —— 2340 条被门卫误杀的正常新闻 + 超龄积压，永远不会被分析、也永远不会 DONE | 前端「待分析」2500+，实际一条也处理不了 | [10.19](#1019-认领时效门槛--别把额度烧在用不上的老新闻上) |

### 通用排查手法（下次照这个顺序走）

```bash
cd /Users/claire/Desktop/金融1.0/backend

# ① 引擎到底在干什么 —— 这一行决定后面看什么
grep -a '通道体检\|Batch persist 失败\|Batch done\|BATCH\] claimed' \
  /Users/claire/.trident/log/engine.out.log | tail -12

# ② 认领正常但落库失败 → 看第 1/4 层（INSERT / 作用域）
grep -a 'Batch persist 失败' /Users/claire/.trident/log/engine.out.log | tail -3

# ③ 连 claimed 都没有 → 看第 3 层（熔断）+ 第 6 层（额度）
python3 scripts/reset_llm_breakers.py     # 熔断状态
python3 scripts/budget_status.py          # 额度

# ④ 批次很慢 → 看第 10 层（快照）
grep -a 'SNAPSHOT' /Users/claire/.trident/log/engine.out.log | tail -5

# ⑤ 队列构成（多少是僵尸噪音）
sqlite3 trident_event_bus.db "
select 'PENDING_real='||(select count(*) from raw_news where status='PENDING' and coalesce(is_noise,0)=0),
       'noise_zombie='||(select count(*) from raw_news where status='PENDING' and coalesce(is_noise,0)=1);
"
```

> **「LLM 全部成功（16/16 ok）但一条都没落库」= 落库路径炸了**，不是 LLM 问题。
> 「连 claimed 都没有」= 熔断/额度挡住了，是**省钱机制在正常工作**。
> 「批次很慢」= 行情源或网络，不是 LLM。

---

## 0.5 🔴 额度保险丝（2026-11-04 二次事故：额度被烧光）

### 0.5.1 先看账 —— 每天第一件事

```bash
cd /Users/claire/Desktop/金融1.0/backend
python3 scripts/budget_status.py --by-channel
```

输出（事故当天实测）：

```
  token             9.20M / 3.00M       (306.7%)  剩 0
  请求数                4.6K / 2.0K        (229.3%)  剩 0
  信号条数                244 / 300         ( 81.3%)  剩 56
--------------------------------------------------------------
  🔴 闸门状态：拦截中 —— 日请求数预算已用尽 (4587/2000 次)

  今日各通道消耗：
  通道                            次数       token
  minimax-gate                2570       2.24M      ← 门卫，占 40%
  MiniMax-M2.7-highspeed        91       1.75M
  minimax-gate2                1703       1.44M      ← 门卫
  ...
```

### 0.5.2 四道防线

| # | 机制 | 位置 | 作用 |
|---|---|---|---|
| 1 | **全局急停开关** | `LLM_GLOBAL_KILL_SWITCH=1` | 任何 LLM 调用都不发出（零请求零额度） |
| 2 | **日预算闸门** | `LLM_DAILY_*_BUDGET` | 认领前 + 请求前双重拦截，超额直接停 |
| 3 | **通道熔断** | `ai_worker._analysis_breaker` / `realtime_filter._llm_breaker` | 429/401 一次即熔断，401 锁 24h |
| 4 | **门卫本地优先** | `GATE_LOCAL_FIRST=1` | 本地模型能答就不打远程（本机实测不可用，默认 0，见下） |
| 5 | **账本 fail-closed** | `LLM_BUDGET_FAIL_CLOSED=1` | DB 读不到账本时按"已超预算"拦截 |

> **第 5 条是踩过坑才加的**：引擎重启后进程内计数归零，若恰好 DB 被写锁挡住，
> 闸门会看到 `requests=0` → 判定"没超预算" → **放行** → 重启后第一批请求全部漏网。
> 修复有三层：① `llm_budget.bootstrap()` 在进程启动时把当天真实用量灌进进程内基线；
> ② `_query()` 失败时重试一次；③ 账本仍不可读时按 fail-closed 拦截。
> 漏网的实测证据：16:11:20 有 2 次门卫调用在闸门修复前溜了过去。

> **第 4 条在本机默认关闭 —— 这是实测结论，不是偷懒**：
> 本机只有 **8GB 物理内存**，且长期被 GUI 应用（豆包 / VSCode / Chrome）占满，
> `load average` 常年 **700+**、`vm_stat` 空闲内存仅 **12MB**。
> Ollama 加载 qwen2.5:3b 的 1.9GB 权重后被 swap 到磁盘，
> 实测 `chat/completions` **90 秒不返回**（而 `/api/tags` 秒回 —— 服务活着，纯推理被内存拖死）。
> 开着 `GATE_LOCAL_FIRST=1` 只会让每条新闻**白等超时再回退远程**：
> 既不省钱，又把 ingest 拖慢 8 秒/条。
> 内存扩容到 16GB+ 且空闲内存 >3GB 后，可以改回 `1` 启用。
>
> **真正的门卫省钱方向不是"换成本地慢模型"，而是"少跑门卫"**：
> 今天门卫 4273 次调用 / 3.68M token（占全天 40%），而主分析只产出 244 条信号 ——
> 每条信号搭了 ~17 次门卫调用。减少门卫触发次数（去重/预筛/只在候选上跑）
> 才是数量级上的优化。

### 0.5.2b 门卫为什么是最大的漏钱口

| 通道 | 调用次数 | token | 占全天 |
|---|---|---|---|
| `minimax-gate` | 2572 | 2.24M | 24% |
| `minimax-gate2` | 1706 | 1.44M | 16% |
| **门卫小计** | **4278** | **3.68M** | **40%** |
| 主分析全部通道 | ~295 | ~5.4M | 59% |
| `local`（不计费） | 10 | 8.4K | ~0% |

**结论**：门卫每条新闻打一次远程，单条 ~870 token；一天几千条新闻 → 门卫自己就是主要开销。
预算闸门已把门卫纳入管控（超预算时门卫也不发请求），但**结构性优化需要另做**。

### 0.5.2c 🔴 全域封堵：保护曾经只加了一半（2026-11-04 二次事故的真正根因）

第一轮修复只给**主分析**（`ai_worker`）和**门卫**（`realtime_filter`）加了闸门与熔断。
但全局扫描发现 —— **还有 6 条裸 HTTP 路径完全绕过保护**：

| 路径 | 文件 | 原状 | 危害 |
|---|---|---|---|
| review 复盘 | `review_agent._llm` | 裸 `requests.post`，**429 时 `sleep(2)` 重试** | 每通道打 2 次，且**无埋点** → 账本看不到这些消耗 |
| judge 远程兜底 | `judge_worker._call_judge_llm` | 裸 `requests.post` | 判决失败即烧钱 |
| 新闻翻译 | `engine/webhook.py` | 裸 `urllib.request` | **每条新闻都跑**，最高频 |
| 多因子归因 | `engine/attribute_multi_factor.py` | 裸 `session.post` | 无管控 |
| 异动/驱动归因 | `engine/ai_worker.py` | 裸 `session.post` | 无管控 |
| 门卫回放探针 | `engine/gate_replay_guard.probe_channels` | 每通道发一次真实探针 | **每 5 分钟一轮**，全天几百次 |
| 通道体检 | `api_server._probe_channel` / `ai_worker._probe_openrouter` | 有熔断无闸门；后者 `max_tokens=600` | 面板点一下烧一次 |

> **最隐蔽的一条**：review 路径不仅没有闸门，**还没有埋点** ——
> 它烧掉的额度在 `ai_call_stats` 里根本看不到，统计会系统性低估。

**已全部补齐**，统一为「额度闸门 → 通道熔断 → 发请求」，且 429/401 **不再重试**。

#### 回归防线：审计脚本

```bash
cd backend
python3 scripts/audit_llm_gates.py     # 报告所有未受保护的 LLM 调用点
python3 scripts/verify_no_burn.py      # 端到端：额度锁死后真调每条路径，比对账本零增长
```

`audit_llm_gates.py` 用 AST 扫描所有含 `chat/completions` 的函数，
凡所在函数内没有 `llm_budget` / 熔断器标记就报红。
**以后任何人再加一条裸 LLM 路径，审计立刻报红**，而不是等额度烧穿才发现。
（已接入 `tests/test_gate_deferred_v320.py::test_audit_script_finds_no_unprotected_llm_call_sites`。）

`verify_no_burn.py` 更狠：它**真的去调**每条 LLM 入口，然后比对调用前后的账本快照 ——
请求数必须零增长才算通过。

#### ⚠️ 改完代码必须重启所有常驻服务

本次事故的最后一根坑：**改了代码但老进程还在跑**。

```
请求数从 4582 涨到 4584 —— 修复已生效却还在涨
→ 排查发现 launchd 里 7 个常驻服务跑的是修复前的旧代码
→ 全部 kickstart -k 重启后，请求数稳定在 4584 不再增长
```

常驻服务清单（改 `src_python/**` 后都要重启）：

```bash
U=$(id -u)
for s in engine api crowd-pulse master bridge signalweightcron; do
  launchctl kickstart -k gui/$U/com.trident.$s
done
```

cron 类任务（`*-cron`）下次触发时自动加载新代码，不用手动重启。

### 0.5.2d 门卫结果缓存（v322 已落地）

门卫是 **temperature=0 的确定性判断**，prompt 只由「标题 + 摘要」构成（无时间戳），
所以**同样的新闻必然得到同样的判定** → 缓存不改变语义，只是省掉重复付费。

直接命中的重复来源：多源转载、gate-replay 回放、批量补跑、引擎重启后重扫。

| 参数 | 默认 | 说明 |
|---|---|---|
| `GATE_CACHE_ENABLED` | `1` | 缓存开关 |
| `GATE_CACHE_MAX_ENTRIES` | `500` | 条数上限（本机 8GB RAM，刻意保守） |
| `GATE_CACHE_TTL_S` | `21600` | 存活 6 小时 |

日志里出现 `pool-cache-hit (零请求)` 就说明缓存命中了。
**注意**：缓存命中不写 `ai_call_stats` 埋点（本来就没花钱），所以额度统计仍然准确。

### 0.5.2e 为什么本地优先默认关闭

见上文「第 4 条」说明：本机 8GB 内存被 GUI 应用占满（load 900+、空闲 12MB），
Ollama 加载 1.9GB 权重后被 swap 到磁盘，实测 `chat/completions` **90 秒不返回**。
开着只会白等超时再回退远程 —— 不省钱还拖慢 ingest。

### 0.5.3 紧急止血（怀疑在烧钱时）

**最快** —— 一键急停：

```bash
cd /Users/claire/Desktop/金融1.0/backend
sed -i '' 's/^LLM_GLOBAL_KILL_SWITCH=0/LLM_GLOBAL_KILL_SWITCH=1/' .env
launchctl kickstart -k gui/$(id -u)/com.trident.engine
```

效果：引擎继续跑 ingest，但**一条 LLM 请求都不发**，额度消耗归零。
恢复：把 `1` 改回 `0` 再重启。

**更狠** —— 直接停引擎：

```bash
launchctl kill SIGTERM gui/$(id -u)/com.trident.engine
```

### 0.5.4 额度参数（.env）

| 参数 | 默认 | 含义 |
|---|---|---|
| `LLM_GLOBAL_KILL_SWITCH` | `0` | 1 = 全局急停，所有 LLM 调用停止 |
| `LLM_DAILY_TOKEN_BUDGET` | `3000000` | 每日 token 上限（0=不限） |
| `LLM_DAILY_REQUEST_BUDGET` | `2000` | 每日请求数上限（0=不限）← 最兜底 |
| `LLM_DAILY_NEWS_BUDGET` | `300` | 每日信号条数上限（0=不限） |
| `LLM_MAX_TOKENS_PER_NEWS` | `30000` | 单条新闻 token 上限（超了放弃该条） |
| `LLM_BUDGET_FAIL_CLOSED` | `1` | 账本读不到时按"已超预算"拦截（0=放行） |
| `GATE_LOCAL_FIRST` | `1` | 门卫本地优先（省远程调用） |
| `GATE_LOCAL_TIMEOUT_S` | `8` | 本地门卫等待上限（秒） |

> **重要**：主分析扣的是远程大模型的额度；门卫如果走远程同样扣钱。
> 事故当天门卫烧了 3.68M token / 4273 次调用，占全天 9.2M 的 **40%**，
> 而主分析只产出 244 条信号 —— 门卫才是最大的漏钱口。
> 所以 `GATE_LOCAL_FIRST=1` 必须开。

### 0.5.5 调预算的正确姿势

1. 先 `python3 scripts/budget_status.py` 看今天用了多少；
2. 按"信号条数 × 单条 12k token"估出**你愿意付的钱**；
3. 写进 `.env` 的 `LLM_DAILY_TOKEN_BUDGET`；
4. `launchctl kickstart -k gui/$(id -u)/com.trident.engine` 让配置生效。

**预算用尽时引擎不会挂**，只是停在「跳过认领」，等第二天 00:00 自动恢复。

### 0.5.6 回归自测

```bash
cd /Users/claire/Desktop/金融1.0/backend
python3 scripts/test_budget_gate.py
```

覆盖：急停开关、三种预算、剩余额度不足、单条估算、熔断参数。

---

## 0. 一句话现状

2026-11-04 上午引擎连续数小时**零信号产出**，前端「待分析」只涨不跌。
根因是**四件事叠加**，缺一都不会这么严重：

| # | 根因 | 表现 | 修复 |
|---|---|---|---|
| 1 | 4 个 LLM 通道全挂（2×429 配额打满 + 2×401 key 失效） | 分析全失败 | 本地 Ollama 兜底 + 熔断 |
| 2 | `LOCAL_LLM_ENABLED=0` 本地兜底被关 | 通道全挂 = 无路可走 | `.env` 改回 `1` |
| 3 | 主分析路径**完全没有熔断** | 通道全挂时仍疯狂重试 → 流量被打光 | 主分析 + 辅助通道全部接入熔断 |
| 4 | 引擎进程**卡死在 DNS 解析**（macOS mDNS） | 进程活着但零产出，launchd 无感 | DNS 超时护栏 + 挂死看门狗 |

外加一个放大器：`urllib3 Retry` 把 **429 放进了自动重试列表**，
每条新闻每模型自动重试 4 次，再叠加应用层重试 + 3 个 fallback 模型
→ 单条新闻最多打 30+ 次 HTTP 请求，配额瞬间打穿，429 反而变多。

---

## 1. 三个自检脚本（日常排障入口）

全部位于 `backend/scripts/`。

### 1.1 通道体检 —— 「一个信号都没有」时先跑这个

```bash
cd backend
python3 scripts/test_signal_pipeline.py --check
```

对 `main` / `gate` 两个池子的每个通道发一个 **max_tokens=8** 的极小请求，
报告哪个活着、哪个 429/401/超时。流量消耗可忽略（每通道 ~10 token）。

输出示例（事故当天）：

```
[purpose=main] 池内 2 个通道
  ❌ minimax/MiniMax-M3     status=429  已达到 Token Plan 用量上限 (2056)
  ❌ minimax2/MiniMax-M3    status=401  invalid api key (2049)
[purpose=gate] 池内 2 个通道
  ❌ minimax-gate/...       status=401  invalid api key (2049)
  ❌ minimax-gate2/...      status=429  已达到 Token Plan 用量上限 (2056)
结论：❌ 全部通道不可用 —— 这就是「一个信号都没有」的直接原因。
```

> 本地兜底通道（`local/qwen2.5:3b`）也会被体检到，它一直活着 ——
> 这是「有本地兜底」和「没本地兜底」的区别。

### 1.2 流水线状态 + 一键救援 —— 「待分析」只涨不跌时用

```bash
python3 scripts/rescue_pipeline.py --status     # 只看积压快照
python3 scripts/rescue_pipeline.py --apply      # 解冻门卫冻结 + 回收僵尸
python3 scripts/rescue_pipeline.py --apply --reset-failed   # 再把 FAILED 拉回 PENDING
```

三类积压：

| 积压 | 成因 | `--apply` 的处理 |
|---|---|---|
| 门卫冻结 `gate_unavailable=1` | 门卫通道挂了，新闻被标记「未判定」保留待回放 | 解冻（`gate_unavailable=0`） |
| `FAILED` | `retry_count` 打满 3 次后被永久标死 | `--reset-failed` 拉回 PENDING |
| 僵尸 `PROCESSING` | 引擎崩在分析中途 | 回滚 PENDING |

### 1.3 引擎看门狗 —— 「进程在但不动」时用

```bash
python3 scripts/engine_watchdog.py --status
```

判据：`runtime/ai_worker_progress.json` 的 mtime。
主循环每轮都会更新它；**超过 5 分钟没更新 = 主循环卡死**。

已注册为 launchd 服务 `com.trident.engine-watchdog`，**每 2 分钟自动跑一次**，
发现卡死会自动 `launchctl kickstart -k` 重启引擎。

```bash
launchctl list | grep -E 'trident.engine|trident.ollama|watchdog'
```

| 服务 | 作用 |
|---|---|
| `com.trident.engine` | 主引擎 |
| `com.trident.ollama` | 本地模型兜底（**必须常驻**） |
| `com.trident.engine-watchdog` | 挂死检测 + 自动重启 |

---

## 2. 怎么测试数据（完整链路验证）

### 2.1 注入一条测试新闻，等引擎产出信号

```bash
cd backend
python3 scripts/test_signal_pipeline.py --inject
python3 scripts/test_signal_pipeline.py --inject --text "你自己的新闻内容"
python3 scripts/test_signal_pipeline.py --cleanup      # 清掉注入的测试数据
```

流程：插入 `raw_news`（绕过门卫，`is_noise=0` / `gate_unavailable=0`）
→ 引擎认领 → 分析 → 落 `ai_decisions` → 脚本打印信号（标的/建议/模型/推理）。

**前置条件**：引擎在跑（`launchctl list | grep com.trident.engine`）。
本地兜底模式下单条约需 **3-5 分钟**（本地 3B 模型跑八段式 prompt）。

### 2.2 只验证门卫 / 分类器

```bash
python3 scripts/rescue_pipeline.py --status   # 顺带看门卫是否在冻结新闻
```

### 2.3 回归测试

```bash
cd backend
python3 -m pytest tests/ -q          # 全量
python3 -m pytest tests/ -q -k gate   # 只跑门卫相关
```

---

## 3. 关键配置（`backend/.env`）

### 3.1 本地兜底 —— **不要关**

```bash
LOCAL_LLM_ENABLED=1
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=qwen2.5:3b          # ⚠️ 必须与 `ollama list` 实际存在的模型一致
LOCAL_LLM_FAST_MODEL=qwen2.5:3b
LOCAL_LLM_REFLECT_MODEL=qwen2.5:3b
```

关掉它 = 远程全挂时**没有任何降级路径** = 必然零信号。
模型名写错 = 通道 404 = 等于没开。改模型名后**必须重启引擎**。

```bash
# 装/确认本地模型
/Applications/Ollama.app/Contents/Resources/ollama list
/Applications/Ollama.app/Contents/Resources/ollama pull qwen2.5:3b
```

### 3.2 各通道 key 的故障对照表

| 通道 | key 变量 | 429 | 401 |
|---|---|---|---|
| 主分析账户1 | `MINIMAX_MAIN_KEY` | 配额打满 → 充值 | key 失效 → 换 key |
| 主分析账户2 | `MINIMAX2_API_KEY` | 同上 | 同上 |
| 门卫账户2 | `MINIMAX_GATE_KEY` | 同上 | 同上 |
| 门卫账户1 | `MINIMAX_MAIN_KEY`（复用） | 同上 | 同上 |

> `.env` 里 v321 那段注释**早就预言过**这次事故：
> 「账户1 配额一打满，门卫 100% 不可用 → 新新闻全部落成 gate_unavailable=1 → 队列卡死」

### 3.3 熔断参数（`ai_worker.py`）

```python
_ANALYSIS_QUOTA_COOLDOWN_S = 600.0   # 配额/鉴权失败冷却 10 分钟
_ANALYSIS_NET_COOLDOWN_S = 60.0      # 网络抖动冷却 60 秒
_ANALYSIS_QUOTA_FAIL_THRESHOLD = 2   # 连续 2 次配额失败即熔断
_ANALYSIS_NET_FAIL_THRESHOLD = 3
_GATE_FREEZE_TTL_S = 1800            # 门卫冻结保质期 30 分钟
```

---

## 4. 排障决策树

```
前端「待分析」只涨不跌
  │
  ├─ 跑 test_signal_pipeline.py --check
  │    ├─ 全挂 → 充值/换 key；确认 LOCAL_LLM_ENABLED=1 且 ollama 在跑
  │    └─ 有活的 → 继续
  │
  ├─ 跑 rescue_pipeline.py --status
  │    ├─ 门卫冻结 > 0 → --apply（解冻）
  │    ├─ FAILED > 0   → --apply --reset-failed
  │    └─ 可认领 > 0   → 引擎应该认领，往下
  │
  ├─ 引擎没认领？
  │    ├─ cat runtime/ai_worker_progress.json   → step 停在哪儿
  │    ├─ step=pool-blocked → 通道全熔断（正常，等冷却）
  │    ├─ step 长时间不变 → 卡死 → engine_watchdog.py 会自动重启
  │    └─ launchctl list | grep trident.engine → 进程在不在
  │
  └─ 认领了但没信号
       ├─ tail runtime/llm_call_failures.jsonl | tail -20   → 最近失败原因
       ├─ 检查 gate_unavailable 是否又涨（门卫 401 会**降级放行**，不该再涨）
       └─ 检查模型池：日志里的 [POOL] 熔断跳过 N 个通道
```

### 卡在哪个 step 是什么意思

| step | 含义 |
|---|---|
| `loop` | 刚进主循环 |
| `macro` | Phase 0（行情/宏观/K线）刚过 —— **外部源全超时时会在这停很久** |
| `signals` | Phase 0.5（绩效/复盘/参考信号） |
| `batch` | 认领完成，准备分析 |
| `pool-blocked` | **通道全熔断，主动不认领**（零额度消耗，等冷却） |
| `analyze-done` | 一批分析完成 |

---

## 5. 本次修复清单（代码位置）

| 文件 | 改动 |
|---|---|
| `src_python/engine/net_guard.py` | **新增**：进程级 DNS 超时护栏 |
| `src_python/engine/main.py` | 启动时安装护栏（必须在任何协程之前） |
| `src_python/engine/ai_worker.py` | 429 移出 urllib3 重试；主分析+辅助通道熔断；认领前通道体检；熔断通道不进模型池；门卫冻结 TTL 自动放行；**`build_model_config` 补回 `key_alias`/`timeout`**；**主分析池按 purpose 过滤** |
| `src_python/config.py` | 本地通道 purpose 与 id 去重；`LOCAL_LLM_MAIN_ENABLED` 开关 |
| `src_python/realtime_filter.py` | 门卫 **401/402/403 → 降级放行**（不再冻结）；429/网络 → 冻结但有 TTL |
| `scripts/rescue_pipeline.py` | **新增**：一键救援 |
| `scripts/test_signal_pipeline.py` | **新增**：通道体检 + 注入测试数据 |
| `scripts/engine_watchdog.py` | **新增**：挂死检测 + 自动重启 |
| `.env` | `LOCAL_LLM_ENABLED=1` + 模型名对齐 + `LOCAL_LLM_MAIN_ENABLED=0` |
| `~/Library/LaunchAgents/com.trident.ollama.plist` | **新增**：Ollama 开机自启 |
| `~/Library/LaunchAgents/com.trident.engine-watchdog.plist` | **新增**：看门狗开机自启 |

### 5.1 排查过程中揪出的 3 个隐蔽 bug（都是"修了没效果"的真凶）

| # | Bug | 症状 | 修法 |
|---|---|---|---|
| A | `build_model_config()` **丢了 `key_alias` / `timeout`** | 熔断器用 label 当别名、体检用 key_alias 当别名 → **两个不同实例** → 熔断了体检看不到 → 永远不跳过认领；同时 timeout 永远取不到 → 本地通道被 60s 砍掉 | 返回值补齐这两个字段（并同步 `_resolve_model_cfg` / `_minimax_cfg`） |
| B | `_build_model_pool()` **不按 purpose 过滤** | 遍历整个 roster → gate/reflect/**local** 通道全被塞进主分析池 → 轮询分片把 1/3 新闻分配给跑不动的本地模型 → 必然 ReadTimeout | 只收 `purpose` 含 `main` 的条目 |
| C | `urllib3 Retry` 把 **429 放进 `status_forcelist`** | 每条 429 在连接层自动重试 4 次，叠加应用层重试 + 3 个 fallback → 单条新闻最多 30+ 次请求 | 429 移出，只保留 502/503/504 |

> 这三个 bug 有一个共同特征：**代码"看起来"有防护，实际防护对象和真实对象不是同一个**。
> 所以排查时不要相信"这里写了熔断"就认为熔断生效 —— 必须看**日志里真的打出熔断跳过**。

---

## 6. 修复后的实测数据（2026-11-04）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 引擎进程 | 卡死 44 分钟，进程活着但零产出 | DNS 护栏 + 看门狗，卡死 5 分钟内自动重启 |
| 门卫冻结（`gate_unavailable=1`） | 342 条，只增不减 | **23 条**（401 降级放行 + TTL 自动放行） |
| FAILED 积压 | 582 条（永久躺尸） | **0 条** |
| 可认领新闻 | **0 条** | **925 条** |
| `llm_call_failures.jsonl` 增速 | ~44 行/分钟 | **熔断后 0 行**（`[AI] 跳过认领：全部通道熔断`） |
| 主分析模型池 | 混入 gate/local 通道 | 只有 `minimax` + `minimax2` |
| 单元测试 | — | **2914 passed**（5 个失败为先前遗留，非本次引入） |


---

## 7. 还没做完的事（需要人来决策）

1. **MiniMax 配额**：账户1 429 / 账户2 401 —— 代码无法解决，必须充值或换 key。
   换好后引擎**无需重启**也会自动恢复（熔断 10 分钟冷却后自动探测恢复）；
   若要立刻恢复：`launchctl kickstart -k gui/$(id -u)/com.trident.engine`。
2. **本地模型质量**：`qwen2.5:3b` 只承担门卫。若要本地也跑主分析，
   装 `qwen2.5-coder:7b`（约 4.7GB）→ 把 `.env` 的 `LOCAL_LLM_MODEL` 改过去
   → `LOCAL_LLM_MAIN_ENABLED=1` → 重启引擎。
3. **翻译通道**：事故当天 Gemini / MiniMax / DeepSeek 三个翻译器全 401，
   每条英文新闻要试 3 次才降级，明显拖慢 ingest。尚未加熔断（建议照抄
   `_analysis_breaker` 的做法接入 `realtime_filter` 的同款熔断器）。
4. **外部数据源**：FRED / forexfactory / Yahoo / Binance 全线超时或 429，
   `[SNAPSHOT] DOWN` → 分析缺少盘面锚点。属于外网质量问题，需单独排查。
5. **先前遗留的 5 个测试失败**：`test_probe_health.py`（3 个，mock 签名少了
   `breaker_key`）与 `test_minimax_weight_override.py`（2 个，`.env` 里
   `MINIMAX2_WEIGHT=1` 与测试假设的默认 4 冲突）。与本次修复无关，但建议顺手修掉。

---

## 8. 二次事故复盘：一天烧掉 9.19M token 的完整因果链

### 8.1 时间线

| 时间 | 事件 |
|---|---|
| 白天 | 门卫 4278 次调用 / 3.68M token（占全天 40%），而主分析只产出 244 条信号 |
| 15:50 后 | 熔断器生效，`llm_call_failures.jsonl` **停止增长**（6200+ 条定格） |
| ~17:00 | MiniMax 账户1 429 / 账户2 401 → 全池不可用 |
| 17:41:24 | 最后一条 `ai_call_stats` 埋点（`minimax-gate2` http_error） |
| 之后 | 各类旁路继续消耗：**review / judge / 翻译 / 归因 / 探针** 全部无闸门 |
| 修复前 | 请求数 4582 → **4584**，且**埋点里看不到这些消耗**（review 路径无埋点） |
| 修复 + 全域封堵后 | 请求数**稳定在 4584**，最后埋点仍是 17:41:24，**零增长** |

### 8.2 根因不是"某个 bug"，而是"保护只加了一半"

第一轮修复给主分析和门卫加了闸门 + 熔断，但**没有做全局扫描**。
结果是系统里有 **11 个 LLM 调用点**，其中 **6 个是裸 HTTP**，完全绕过保护。
其中最隐蔽的是 `review_agent._llm`：不仅没闸门，**还没有埋点** ——
它烧掉的额度在 `ai_call_stats` 里根本看不到，统计会系统性低估。

> **教训**：加防护不能只修"出问题的那个路径"，
> 必须**枚举全部入口**并逐个验证。`scripts/audit_llm_gates.py` 就是为此存在。

### 8.3 三个隐蔽的坑

1. **改了代码但老进程还在跑**
   修复全部生效后请求数仍在 +2 —— 因为 launchd 里 7 个常驻服务跑的是旧代码。
   引擎的 `DB VERIFIED` 在日志里出现了 **101 次**（watchdog 反复重启）。
   → 改 `src_python/**` 后**必须** kickstart 全部常驻服务。

2. **埋点写库失败 → 账本少算 → 闸门放松**
   `ai_call_stats` 写入会因写锁失败（`database is locked`）而丢记录。
   账本偏小 → 闸门以为还有额度 → 放行更多请求。
   → 已加固：`llm_budget.usage()` 每次读到 DB 就把真实用量同步进 `_INPROC`
   （只取 `max`，方向保守），DB 短暂失明时用"最近一次已知真实用量"兜底。

3. **`local` 通道的排除是精确匹配**
   `key_alias != 'local'` 是精确相等，不是前缀匹配。
   实测本地 Ollama 的 `key_alias` 恰好就是 `'local'`（今日 10 条）→ 排除正确。
   但**将来若改 alias 名字（如 `local-ollama`），免费调用会被算进付费预算**。
   → 改 alias 时必须同步检查 `llm_budget.usage()` 的 SQL。

### 8.4 当前状态（2026-11-04 19:00+ 实测）

```
LLM 额度状态
  token      9.19M / 3.00M  (306.4%)  剩 0
  请求数      4.6K / 2.00M  (229.2%)  剩 0
  信号条数      244 / 300   ( 81.3%)  剩 56
  🔴 闸门状态：拦截中 —— 日请求数预算已用尽 (4584/2000 次)
```

| 验证项 | 结果 |
|---|---|
| `scripts/audit_llm_gates.py` | **11 个调用点，全部受保护，0 个漏网** |
| `scripts/verify_no_burn.py` | 逐条真调 review/judge/probe/归因 → **账本 4584 → 4584，零增长** |
| `tests/test_gate_deferred_v320.py` | **20 passed**（含 4 个新增全局闸门测试 + 审计回归测试） |
| 引擎日志（最后一次启动后） | 6 次闸门拦截，读数全为 **4584**，与 DB 完全一致 |
| `ai_call_stats` 最后埋点 | 17:41:24 —— 此后 85 分钟**零新增** |

### 8.5 遗留风险（未解决，需人工决策）

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| 1 | **账本可能少记**（埋点写库失败） | 闸门比真实用量宽松约 0.15%（实测 7/4584） | 给 `log_ai_call` 加独立持久化队列，或直接读 MiniMax 官方用量对账 |
| 2 | **机器 load 900+ / 空闲内存 12MB** | Ollama 推理 90 秒不返回；全量 pytest 跑不完；watchdog 重启引擎 101 次 | 关闭占内存的 GUI 应用或扩容到 16GB+ |
| 3 | **本地优先默认关闭** | 门卫仍打远程（3.68M token 的来源） | 内存恢复后开 `GATE_LOCAL_FIRST=1`，并做门卫去重/预筛（结构性优化） |
| 4 | **翻译通道无熔断** | 三个翻译器全 401 时每条新闻要试 3 次 | 照抄 `_analysis_breaker` 接入 `realtime_filter` 的同款熔断器 |
| 5 | **全量测试未跑完** | 只跑了门卫专项 20 个；基线为 2912 passed / 5 failed | 机器空闲后重跑全量 |

---

## 9. 稳定性专项（2026-11-04 20:00+）——「不影响使用且不崩溃」

本章记录为解决「系统反复崩溃 + 完全不可用」而做的一轮修复。

### 9.1 崩溃的三层根因（按修复顺序）

| 层 | 现象 | 根因 | 修复 |
|---|---|---|---|
| **L1 看门狗误杀** | 引擎 101 次重启，`DB VERIFIED` 刷屏 | progress 文件尚未生成时就被判"无进展"→ 反复 kickstart | `scripts/engine_watchdog.py`：加启动宽限期 1800s + 最小重启间隔 900s + 引擎运行时长探测 |
| **L2 端口争抢** | Ollama 起不来 / 后端连不上 11434 | `Ollama.app`（GUI）和 launchd 的 `com.trident.ollama` 同时抢端口 | 新建 `~/.trident/wrappers/run_ollama.sh`：先探测端口，占用则等待，否则 `exec ollama serve`；plist 的 `ProgramArguments` 指向该 wrapper |
| **L3 内存打满** | load 233、空闲内存 37MB、swap 6.5/7GB | 8GB 机器同时跑 VS Code(1.3G+) + Chrome(840M+) + 豆包(353M) + Ollama(1.9G) + 后端 | 降低 Ollama 常驻（`OLLAMA_KEEP_ALIVE=5m`）、单并发、单模型、8K 上下文；**不开启本地优先** |

### 9.2 看门狗判定逻辑（`decide()` 纯函数，已单测）

判定分两类，**冷却期只作用于软故障**：

| 情况 | 判定 | 是否重启 | 说明 |
|---|---|---|---|
| 进程不在（OOM/崩溃） | 硬故障 | **立刻重启，不受冷却期限制** | launchd 自身也可能拉不起来，看门狗是唯一兜底 |
| progress 缺失 + 运行时长 < 1800s | 启动中 | 否 | 引擎初始化要几十秒，之前误杀 → 无限重启 |
| progress 缺失 + 运行时长 > 1800s | 挂死 | 冷却期外重启 | |
| progress 超过 600s 未更新 | 挂死（软故障） | 冷却期内跳过，冷却期外重启 | 8GB 机器换页时 progress 会短暂不更新，盲目重启会打断正常一轮 |
| progress 正常 | 正常 | 否 | |

测试：`tests/test_engine_watchdog.py` —— **9 passed**。

### 9.3 Ollama 配置（`com.trident.ollama.plist`）

```xml
<key>OLLAMA_KEEP_ALIVE</key><string>5m</string>     <!-- 原 30m：常驻 30 分钟持续吃 2GB 把系统拖进 swap -->
<key>OLLAMA_NUM_PARALLEL</key><string>1</string>    <!-- 单并发 -->
<key>OLLAMA_MAX_LOADED_MODELS</key><string>1</string>
<key>OLLAMA_CONTEXT_LENGTH</key><string>8192</string>
<key>OLLAMA_FLASH_ATTENTION</key><string>1</string>
```

> **注意**：`Ollama.app` 打开时会自己占住 11434 端口，此时 wrapper 在等待 —— 这是设计内的，不是故障。
> `launchctl list | grep ollama` 显示 PID 为 `-` 通常就是这个状态。

### 9.4 一键体检 `scripts/doctor.py`

```bash
cd ~/Desktop/金融1.0/backend
python3 scripts/doctor.py            # 完整体检（每个通道真发 1 个 8-token 请求）
python3 scripts/doctor.py --fast     # 跳过探活，零消耗
```

体检 5 项：机器资源 / 服务状态 / 额度账本 / 通道探活 / 闸门审计。
退出码：`0` 正常、`1` 无任何可用 LLM 通道、`2` 部分降级。
**充值或换 key 之后先跑这个**，一眼看出哪条通道通了。

### 9.5 额度预算调整（2026-11-04）

| 项 | 原值 | 现值 | 理由 |
|---|---|---|---|
| 日 token 预算 | 3M | **20M** | 今日实际用到 9.19M，原值会半夜拦停 |
| 日请求预算 | 2000 | **6000** | 今日实际 4586 次（其中门卫 `realtime_filter` 占 4282 次） |
| 门卫缓存上限 | 500 | **5000** | 提高去重命中率 |
| 本地最大并发 | 2 | **1** | 8GB 机器 |

> ⚠️ **预算放宽的代价**：一旦远程 key 恢复可用，闸门会放行到 6000 次 / 20M token 为止。
> 冷却期内的实际用量见 9.6。

### 9.6 通道现状（2026-11-04 20:50 实测，已更新）

| 通道 | 结果 | 处置 |
|---|---|---|
| MiniMax 账户1 | ✅ **HTTP 200 可用** | 已换新 key（`sk-cp-UzjSivSJ…`，旧 key 429 额度用尽） |
| MiniMax 账户2 | ✅ **HTTP 200 可用** | 同上（当前与账户1 共用同一 key → 见 10.7 单点故障） |
| OpenRouter | HTTP 401 User not found | **需换 key**（`OPENROUTER_API_KEY`） |
| AIPING / Xinshu | 连接失败（SSL / DNS） | 服务侧问题 |
| 本地 Ollama | 8GB 机器加载 3B 超过 5 分钟，swap 打满 | **暂不可用**，需 16GB 内存 |

**key 轮换记录（2026-11-04 20:30）**

| key | 状态 | 说明 |
|---|---|---|
| `sk-cp-Y-ClXPn_…OZiM` | ❌ HTTP 429 | Token Plan 用量上限（与旧账户1同属一个 `-Y-` 账户） |
| `sk-cp-UzjSivSJ…2H5k` | ✅ HTTP 200 | **当前在用**（已写入全部 8 个 `MINIMAX*_KEY` 槽位） |
| `sk-cp-gUy0g9Rf…CNZw` | ❌ HTTP 401 | invalid api key（已注释留存，可回滚） |

> ⚠️ 当前 8 个槽位共用一个 key —— **能用，但失去双通道冗余**。
> 建议再开一个 MiniMax 账户，把 `MINIMAX2_API_KEY` 换成独立 key。
> 详见 **10.7 单点故障提醒**。

### 9.7 给用户的最小可行配置（8GB 机器）

```
必做：
  1. 关掉豆包 / Chrome / 多余 VS Code 窗口（它们占了 2.5GB+）
  2. python3 scripts/doctor.py --fast 确认内存回到 2GB 以上
  3. 充值 MiniMax 账户1 或替换 MINIMAX2_API_KEY
  4. python3 scripts/doctor.py 全量体检确认通道变绿

不要做：
  ✗ 在 8GB 机器上开 GATE_LOCAL_FIRST=1（本地模型加载会拖垮整机）
  ✗ 同时开 Ollama.app 和让后端直连 11434（端口争抢）
```

### 9.8 回滚

```bash
# 看门狗回到旧行为
git diff scripts/engine_watchdog.py && git checkout scripts/engine_watchdog.py

# Ollama 恢复 GUI 自管（launchd 不再介入）
launchctl bootout gui/$(id -u)/com.trident.ollama
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trident.ollama.plist
```
```bash
# 看门狗回到旧行为
git diff scripts/engine_watchdog.py && git checkout scripts/engine_watchdog.py

# Ollama 恢复 GUI 自管（launchd 不再介入）
launchctl bootout gui/$(id -u)/com.trident.ollama
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trident.ollama.plist
```

---

## 10. 「突然抽风」的四层根治（2026-11-04 20:40+）

用户原话：**「这问题能不能彻底解决，不影响我使用，有病不不会突然抽风」**。

### 10.1 先说结论：能治到什么程度

| 层面 | 治好了吗 | 怎么治的 |
|---|---|---|
| 熔断器形同虚设 | ✅ 治好 | half_open 改 single-flight，只放一个探测请求 |
| 通道一恢复就集体重试 | ✅ 治好 | 冷却加随机抖动 + 速率闸门限流 |
| 多进程各自重试放大额度 | ✅ 治好 | 熔断状态落盘共享，一个进程熔断大家一起冷却 |
| 额度用满突然全停 | ✅ 治好 | 80%/90% 水位提前推微信，不再"到了才停" |
| 8GB 内存不足导致整机卡顿 | ⚠️ 缓解 | 内存守卫自动卸载 Ollama 模型 + 告警；**根子还得靠你关掉 VS Code/豆包** |
| 上游额度池被打穿 | ⚠️ 靠配置 | 闸门只是止血，额度本身还是要充值 |

### 10.2 抽风的完整因果链（这次查清楚了）

之前一直笼统归因为"额度烧光"。实际上**抽风是 4 个独立机制叠加**，
每一个单独存在都足以让服务表现成「时好时坏」，而且**和额度总量无关**：

```
① 通道出问题（429/401/网络抖动）
        ↓
② 熔断器 half_open 本该"只放一个探测"，代码却是 return True
   → 熔断等于没开，通道坏了还在一直打
        ↓
③ 冷却结束那一刻，所有排队请求同时醒来
   → 几十个并发打向同一个 key → 立刻 429 → 再次熔断
   → 循环（这就是"突然抽风"最直接的形态）
        ↓
④ engine / api / review_agent / judge_worker 四个进程
   各自有各自的熔断器 → 同一个坏通道被打了 4 遍
   → 额度消耗放大 4 倍
        ↓
⑤ 即使没打满额度，服务也已经在「恢复 → 429 → 恢复」的循环里反复横跳
```

### 10.3 逐条修复

**① half_open single-flight**（`src_python/circuit_breaker.py`）

```python
# 修复前：注释写「只放行一次探测请求」，代码却是无限制放行
if self._state == "half_open":
    return True          # ← 熔断形同虚设

# 修复后：single-flight，只放第一个探测请求
if self._probe_in_flight:
    if time.time() - self._probe_at <= self.probe_timeout:
        return False     # 探测在飞 → 其余全拒
    self._probe_in_flight = False   # 探测丢了（进程卡死）→ 允许新探测
self._probe_in_flight = True
```

**② 冷却抖动（jitter）—— 放行时刻必须「固定」，不能每次调用重 roll**

```python
# 修复前（错）：在 allow() 里每次调用重新随机
wait = self.open_seconds * (1.0 + random.uniform(0.0, self.jitter_ratio))
if time.time() - self._opened_at >= wait:   # ← 每次重 roll，冷却可能被提前穿透
    ...

# 修复后：熔断「打开时」一次算好，之后固定不动
self._wait_until = self._opened_at + self.open_seconds * (
    1.0 + random.uniform(0.0, self.jitter_ratio))
# allow() 里直接用这个固定时刻
if time.time() >= self._wait_until:
    ...
```

冷却时间随机拉长 0~25%，把「同一时刻醒来的几十个请求」打散成一小批一小批。

> **为什么必须固定**：
> ① 每次重 roll → 连着几次 roll 到接近 0，冷却刚到基准时间就被穿透；
> ② 不同进程 roll 出不同时刻 → 「同时醒来」变成「参差醒来」，
>    既没真正打散，又让最早醒的那批抢先打过去。
> 现在 `wait_until` 写进共享状态文件，**所有进程用同一个放行时刻**醒来，
> 配合 single-flight 只放 1 个探测 —— 这才是真正的打散。
> 回归测试：`test_jitter_release_time_is_fixed`、`test_release_time_shared_across_processes`。

**③ 跨进程熔断共享**（新增）

熔断状态落盘到 `backend/runtime/breakers/*.json`（原子写），
一个进程发现通道坏了，其它进程 0.5 秒内一起冷却。
共享文件里同时带 `wait_until`，保证各进程的放行时刻一致。
实测：4 进程并发扣减速率名额，合计**精确等于上限**，无丢失无重复。
实测：子进程熔断 → 父进程（自己从未失败过）立刻 `allow() == False`。

---

### 10.4 v322 第二轮：门卫漏钱口（实测省掉 71% 的请求）

第二轮排查发现的**不是"抽风"，而是"漏钱"** —— 它才是额度永远不够的根因。

#### 现象

```
今日门卫调用 4,397 次 / 入库新闻 606 条  =  每条新闻 7.26 次门卫
今日主分析调用 607 次                              ← 主分析只用了 607 次
门卫占总请求 88%，284 次 http_error 全在门卫
```

#### 根因一：门卫判定被 thinking 截断 → 噪音全部放行

MiniMax 是 reasoning 模型，即使下发 `thinking:{"type":"disabled"}` 仍会**偶发**输出
`<think>...</think>`。而 L2 原来发的是 `max_tokens=4` —— think 一出现就把预算吃光，
输出被截断成 think 片段，**解析不出 0/1** → 门卫返回 `None` →
`evaluate_news` 全部放行 → **噪音全部涌进主分析**。

```python
# 修复前
"max_tokens": 4
m = re.search(r"\b([01])\b", raw)     # 直接在含 think 的文本上找 → 也会误判

# 修复后
_L2_MAX_TOKENS = 128                    # 给 think + 答案留够余量
cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
if "<think>" in cleaned:                # 未闭合 = 被截断，答案必然没输出
    cleaned = cleaned.split("<think>")[0]
hits = re.findall(r"\b([01])\b", cleaned)
return int(hits[-1]) if hits else None  # 取**最后一个**（答案在 think 之后）
```

**实测对照（同样 20 条真实新闻）：**

| | 判定成功 | 判定失败（被迫放行） | 放行率 |
|---|---|---|---|
| 修复前 | 7/20 | 13/20 | **65%** |
| 修复后 | 16/20 | 4/20 | **20%** |

→ 每 20 条新闻少放行 9 条 = **省下 45% 的主分析请求**。

#### 根因二：门卫把每条新闻并行发给全池，只用第一个结果

`_call_pool_sync` 原本把一条新闻**同时**发给池里所有远程通道，
但只用「第一个返回成功」的结果（**没有投票逻辑**）——
于是每条新闻的请求数 = 通道数 × (L2+L3)，其中绝大部分是纯浪费。

**修复：阶梯降级**（`GATE_REMOTE_STAIR_STEP`，默认 1）

```
第 1 级 = weight 最高的单个远程通道 → 成功即返回（1 次请求）
第 2 级 = 其余远程通道并行兜底 → 只在第 1 级全失败时触发
本地通道 = 最后一级（远程全挂时唯一还能出结果的通道，绝不丢弃）
```

延迟不变（原本也是「任一成功即返回」），主通道坏了自动降级，可靠性不降低。
置 `GATE_REMOTE_STAIR_STEP=0` 可一键回退旧行为。

#### 修复后实测（2 分钟窗口）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 门卫占总请求 | 88% | **17%** |
| 请求失败率 | ~6% | **0%** |
| 门卫判定失败率 | 65% | **20%** |

#### 顺带修：news 预算撞顶导致"看起来像抽风"

`LLM_DAILY_NEWS_BUDGET=300` 而今天入库 606 条 → 撞顶后闸门直接拦截 →
引擎停在分析环节（514 条 PENDING 堆着不动）→ 用户看到的现象是
**「服务不更新了 / 像是抽风」**，但根因是配置上限，不是故障。

已调整为 **500**（依据：请求预算 6000 才是真硬顶，500 条 × ~4 次 = 2000 次，
远低于 6000；token 20M 是第二道保险，80%/90% 会主动推微信告警）。
要更省就调小它 —— 它是「每天最多分析多少条新闻」的**业务上限**，不是故障开关。


**④ 速率闸门 Rate Fuse**（新增，`src_python/llm_budget.py`）

这是最关键的一道。总额度闸门管「今天花多少」，速率闸门管「这一秒同时打多少」。

```bash
LLM_MAX_RPM=120      # 最近 60 秒内最多 120 次 LLM 请求（0 = 不限制）
```

- 跨进程统一计数（flock 排他锁 + 原子替换写）
- 滑动窗口，不是固定整分钟窗口（避免整点对齐的突发）
- **在真正发 HTTP 请求前扣名额**，不是认领时扣（认领时扣会白扣）
- **已接入全部 11 个 LLM 调用点**（与 `audit_llm_gates.py` 的审计清单一一对应）：

  | 文件 | 用途 |
  |---|---|
  | `engine/ai_worker.py:6539` | 主分析 `_do_api_call`（**含重试循环，逐次扣**） |
  | `engine/ai_worker.py:392` | 通用 `chat` |
  | `engine/ai_worker.py:2682` | 5 分钟异常归因 |
  | `engine/ai_worker.py:2845` | 市场驱动归因（多通道循环，逐个扣） |
  | `engine/ai_worker.py:2952` | OpenRouter 探针 |
  | `engine/attribute_multi_factor.py:221` | 多因子归因 |
  | `engine/gate_replay_guard.py:162` | 门卫回放探针 |
  | `realtime_filter.py:945` | 实时门卫 |
  | `review_agent.py:1577` | 反思评审（多通道 fallback，逐个扣） |
  | `judge_worker.py:144` | 决策质量审核 |
  | `api_server.py:9455` | 面板通道探活 |

- 闸门自身故障时 **fail-open 放行** —— 保护机制不能反过来把系统打死

> **为什么重试循环里要逐次扣**：
> 主分析的 `_do_api_call` 内含 `retry_delays` 重试循环，一次逻辑调用可能发多次 HTTP。
> 只在循环外扣一次，等于限流完全失效 —— 这正是「同一条新闻连打 30 次把额度烧穿」的形态。

**实测**：当前真实用量 8 次/分钟，`LLM_MAX_RPM=120` 有 15 倍余量 ——
**完全不影响正常使用**，但能挡住「一瞬间并发几十个」的打法。

### 10.4 额度水位预警（治「用着用着突然全停」）

额度闸门是硬闸门，到点就一条不放，引擎立刻 `pool-blocked`，
前端大面积显示「待分析」—— 用户感知就是「服务挂了」，其实只是额度到线。

现在用量到 **80% / 90%** 时推一次微信（Server酱），让你有时间决定充值还是调额。
每个档位每天只推一次（跨进程 flock 去重）。

```bash
LLM_WARN_RATIO=0.8,0.9    # 默认值
```

### 10.5 内存守卫（新增 `scripts/memory_guard.py`）

8GB 机器 + swap 5.9GB 的状态下，后端进程全在 swap 里跑 ——
任何一次磁盘 IO 都变成秒级延迟，**这就是「有时候整个卡住」的物理来源**。

实测占用大户（RSS）：

| 应用 | 内存 |
|---|---|
| Visual Studio Code（多个 Renderer） | ~1.3 GB |
| 豆包 DoubaoWork | ~750 MB |
| Google Chrome | ~670 MB |
| 后端自身（engine/api/bridge） | ~380 MB |

守卫每 5 分钟跑一次（`com.trident.memory-guard`，launchd），内存低于阈值时：

1. **卸载 Ollama 已加载模型** —— 唯一能自动回收的一大块（~2GB），
   且本地模型当前不参与主流程（`GATE_LOCAL_FIRST=0`），卸载无副作用
2. 清理陈旧日志 / `__pycache__`
3. 推微信告警，**告诉你具体该关哪个应用**
   （「豆包占了 754MB，建议关掉」而不是笼统的「内存不足」）

> ⚠️ **守卫绝不杀你的 VS Code / Chrome / 豆包** —— 那是你的机器，不是我们的。
> 自动回收只能做到卸载 Ollama；真正的解法还是你关掉最大的那 1-2 个。

```bash
python3 scripts/memory_guard.py            # 检查一次，必要时清理
python3 scripts/memory_guard.py --status   # 只看，不动手
```

### 10.6 新增配置项一览（`.env`）

| 配置 | 默认 | 作用 |
|---|---|---|
| `LLM_MAX_RPM` | `0`（不限） | 每 60 秒最多 LLM 请求数。**生产建议 120** |
| `LLM_BREAKER_SHARED` | `1` | 熔断状态跨进程共享（落盘） |
| `LLM_WARN_RATIO` | `0.8,0.9` | 额度水位预警档位 |
| `MEMORY_GUARD_THRESHOLD_MB` | `900` | 内存守卫触发阈值 |

### 10.7 单点故障提醒（体检新增）

MiniMax 的 Token Plan 是**账户级**配额。多个通道共用一个 key 时，
它们抢的是同一份额度 —— **看似冗余，实则是单点故障**。

`scripts/doctor.py` 现在会检测并明确提示：

```
  [!!] 单点故障：3 个通道共用同一个 key
      sk-cp-UzjSivSJ…2H5k  →  minimax/MiniMax-M3, minimax/MiniMax-M3, minimax2/MiniMax-M3
      这些通道抢的是同一份额度 —— 换掉一个 key 等于全挂
```

**建议**：去 MiniMax 控制台再开一个账户，把 `MINIMAX2_API_KEY` 换成独立 key，
恢复真正的双通道冗余。

### 10.8 key 轮换工具（新增）

```bash
python3 scripts/rotate_minimax_keys.py <新key>
```

- 自动备份 `.env` → `.env.env.bak.<时间戳>`
- 把所有 `MINIMAX*_KEY` 槽位统一指向新 key
- 旧 key 以注释形式留在文件末尾，方便回滚
- 提示需要重启哪些服务

### 10.9 本轮测试

```bash
python3 -m pytest tests/test_stability_fuses.py tests/test_engine_watchdog.py -q
# 19 passed
```

覆盖：half_open single-flight、冷却抖动、探测超时兜底、跨进程熔断共享、
速率窗口上限、窗口滑出恢复、限流关闭时零影响、闸门故障 fail-open、
**4 进程并发扣减精确等于上限**、预算与速率状态协作。

### 10.10 回滚

```bash
# 速率闸门：改成 0 即完全关闭（不影响任何现有行为）
sed -i '' 's/^LLM_MAX_RPM=.*/LLM_MAX_RPM=0/' .env && launchctl kickstart -k gui/$(id -u)/com.trident.engine

# 跨进程熔断共享关闭
sed -i '' 's/^LLM_BREAKER_SHARED=.*/LLM_BREAKER_SHARED=0/' .env

# 内存守卫下线
launchctl bootout gui/$(id -u)/com.trident.memory-guard
rm ~/Library/LaunchAgents/com.trident.memory-guard.plist

# 熔断器完全回退到旧实现
git checkout src_python/circuit_breaker.py
```

---

## 10.11 「待分析」永不下降 —— 队头阻塞死循环（2026-11-05）

### 症状

前端「待分析」3320 条且 **12 小时零下降**；最后一条 DONE 停在 11-04 11:48。
但引擎**不是闲着** —— `ai_call_stats` 里 main 33 次 / verify 78 次 / failover 16 次
（40 分钟窗口）全部 `ok`。即：**LLM 在跑，分析结果却一条都落不了库**。

### 根因链（四环，逐环已验证）

| # | 环节 | 证据 |
|---|------|------|
| 1 | `ai_decisions` 的 INSERT **列与参数错位**：列第 9 位 `child_count`、第 10 位 `aggregation_key`，但 VALUES 把 `agg_key`（`'OIL\|WTI\|BUY'`）传给第 9 位、`0` 传给第 10 位 | `child_count` 列实测 **373 行**存的是聚合键字符串 |
| 2 | 落库时 `int(p_row["child_count"])` 抛 `ValueError: invalid literal for int() with base 10: 'OIL\|WTI\|BUY'` → **整批 16 条**事务 rollback，全部退回 PENDING | `engine.out.log` 里 `Batch persist 失败` 累计 **175 次** |
| 3 | 认领 SQL 是 `ORDER BY id ASC LIMIT 16` → 同一批（id 最小的 16 条）每轮被重新认领 → **队头阻塞**，新新闻永远排不上 | 日志里 `[BATCH] claimed 16 rows (ids=[11832, 11833, ...])` 连着出现多次，id 完全相同 |
| 4 | 看门狗用 `except: pass` 静默吞掉 DB 锁（ingest 长写事务几乎持续占锁）→ PROCESSING 僵尸永不回收 → `retry_count` 永远 0 → 队头永不让位 | 16 条僵尸 `retry_count=0`，而看门狗 SELECT 明明能命中 16 行 |

### 修复

| 位置 | 改动 |
|------|------|
| `engine/ai_worker.py` INSERT | `child_count` 传 `0`、`aggregation_key` 传 `agg_key`（**两列参数换回**） |
| `engine/ai_worker.py` `_safe_child_count()` | 新增 helper：脏 `child_count` 一律当 0，替换两处裸 `int()` |
| `engine/ai_worker.py` 看门狗 | `busy_timeout=30s` + `db.retry_on_busy` 重试；失败**打日志**不再静默 |
| `engine/ai_worker.py` 认领 SQL | PENDING 状态下 `processing_at` 当「冷却到什么时候」用；认领跳过冷却中的行 |
| 落库失败回写 | `processing_at` 写 `now+120s`（冷却），不再置 NULL 让它立刻重排队霸占队头 |
| 数据修复 | 373 行脏 `child_count` 归 0（`aggregation_key` 本身正确，无需回填）；备份在 `/tmp/v322_dirty_backup_*.json` |

### 回归测试

```bash
python3 -m pytest tests/test_v322_head_of_line_block.py \
                 tests/test_v3211_claim_retry_lock.py \
                 tests/test_stability_fuses.py -q
# 39 passed
```

锁死三条不变量：
- **A** INSERT 列/参数严格对齐，且 `child_count` 不再收 `agg_key`
- **B** 脏 `child_count` 不炸整批（`_safe_child_count`）
- **C** 冷却中的行不被认领（队头阻塞兜底）

### 排查这类问题的通用手法

```bash
# 1. 「待分析」不动但 LLM 在跑 → 先看落库有没有失败
grep -a 'Batch persist 失败' ~/.trident/log/engine.out.log | tail -5

# 2. 看认领是不是在反复取同一批（队头阻塞的指纹）
grep -a 'BATCH\] claimed' ~/.trident/log/engine.out.log | tail -10

# 3. 看状态分布与僵尸
sqlite3 trident_event_bus.db "select status,count(*) from raw_news group by status"

# 4. 看某列是不是被写成了非预期类型
sqlite3 trident_event_bus.db "select count(*) from ai_decisions where cast(child_count as text) like '%|%'"
```

---

## 10.12 「待分析」不降的**第三层**根因：熔断状态钉在磁盘上（2026-11-05 白天）

### 症状

10.11 修完队头阻塞后，「待分析」**仍然不降**。引擎日志：

```
[AI] 通道体检 ready=False reason=全部通道熔断: minimax1,minimax2
[AI] 跳过认领：全部通道熔断: minimax1,minimax2（熔断冷却中，零额度消耗）
```

而同时刻的 FILTER 日志里，熔断状态却是：

```
'llm_main_quota:minimax1': 'closed', 'llm_main_net:minimax1': 'closed',
'llm_main_quota:minimax2': 'closed', 'llm_main_net:minimax2': 'closed',
'llm_auth:minimax-gate': 'open',        ← 门卫通道 24h 冷却中
'llm_net:local': 'open'
```

主分析通道明明是 closed，门卫却是 open。

### 根因（三条，全部是 v322 引入跨进程熔断共享的副作用）

| # | 根因 | 后果 |
|---|---|---|
| 1 | **落盘状态不区分凭据**。昨天 key 失效时写的 `state="open"`（auth 冷却 **24 小时**）今天还在磁盘上 → 换上新 key 后通道体检仍判定熔断 → **主循环跳过认领** → 「待分析」只涨不跌 | 生产主因 |
| 2 | **`reset()` 只清内存注册表，不删磁盘文件** → 运维手工复位后，下一个进程把 open 状态读回来 → 「复位」形同虚设 | 运维手段失效 |
| 3 | **门卫熔断跳过时丢了失败类型**。`_call_one` 在熔断打开时返回 `err_code=None` → `_LAST_POOL_FAIL_CODES` 里没有 401 → `_attach_gate` 误判为「瞬时故障」→ 标记 `gate_unavailable=1` **冻结待回放** → 主分析的认领 SQL 排除这些行 → 队列整体卡死 | 门卫侧的卡死 |

第 3 条还有个更隐蔽的变体：判据原本是
`_LAST_POOL_FAIL_CODES.issubset(_AUTH_FAIL_CODES)`（**全部**失败码都是鉴权类）。
本地 Ollama 超时（网络失败）会往集合里塞非鉴权码 → `issubset` 为 False
→ 又走回冻结分支。**一个本地超时就能让整个门卫队列重新卡死。**

### 修复

| 位置 | 改动 |
|---|---|
| `circuit_breaker.py` | 落盘状态新增 `epoch` 字段（= 所有 LLM key 的 sha1 指纹）。读盘时指纹不符 → 认定是**旧凭据**的残留 → 直接忽略并删除该文件 |
| `circuit_breaker.py` | `reset(name)` 同时删除该 name 的落盘文件；`reset()` 清空整个状态目录；新增 `purge_all()` 运维入口 |
| `realtime_filter.py` | 熔断跳过时返回语义码（auth→401 / quota→429），并把 `circuit_open` 的错误码计入 `fail_codes` |
| `realtime_filter.py` | 鉴权判据从 `issubset` 改为 `&`（**存在**鉴权类失败即降级放行） |
| `engine/ai_worker.py` | 通道体检输出区分熔断类型与剩余秒数（`minimax1[quota(1743s)]`）—— 429 等 30 分钟 vs 401 换 key，处置完全相反 |

> **凭据指纹是这一章的关键设计**。它让「换 key」这个运维动作**自动**作废所有旧熔断状态，
> 不需要人工记得去清理 —— 而人工清理必然会忘。

### 运维命令

```bash
cd ~/Desktop/金融1.0/backend

# 看当前熔断状态（只读，零消耗）
python3 scripts/reset_llm_breakers.py

# 清空全部熔断状态（换 key 后 / 通道已恢复但仍显示熔断时）
python3 scripts/reset_llm_breakers.py --reset

# 只清指定通道
python3 scripts/reset_llm_breakers.py --reset llm_main_auth:minimax2
```

> v322 起换 key **不需要**再手动清熔断了（指纹自动作废）。
> 但**同一把 key** 从 429 恢复、或误触发熔断时仍需手动清。

### 测试隔离（顺带修的隐患）

熔断状态落盘 + 速率窗口落盘，都让**测试污染生产**：

- 测试读到生产残留的 `open` 状态 → 被测语义失真（`test_gate_deferred_v320` 6 个失败）
- 测试里的 `failure()` 会把**生产**通道真的熔断掉
- 测试里 `consume_rate_slot()` 扣的是**生产**的 RPM 名额 → 跑一次全量测试就能占满线上速率闸门

已在 `tests/conftest.py` 隔离：

```python
os.environ["TRIDENT_BREAKER_DIR"] = <临时目录>   # 熔断状态
os.environ["TRIDENT_RATE_DIR"]    = <临时目录>   # 速率窗口
# + autouse fixture：每个用例前后 circuit_breaker.reset()
```

### 回归测试

```bash
python3 -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py -q
```

锁死三条不变量：
- **A** 换 key 后旧凭据的熔断状态立即失效
- **B** `reset()` / `purge_all()` 真的清掉落盘状态
- **C** 门卫鉴权熔断 → 降级放行（不冻结）；网络故障 → 仍冻结待回放；熔断跳过必须携带 401

---

## 10.13 「待分析」不降的**第四层**根因：Python 作用域陷阱（2026-11-05 下午）

### 症状

熔断修好后，引擎日志变成：

```
[AI] 通道体检 ready=True reason=-          ← 熔断好了
  [BATCH] claimed 16 rows in 0.01s        ← 队列在认领了
[17:07:50] [AI] Batch persist 失败，跳过本批: UnboundLocalError:
              cannot access local variable '_ts' where it is not associated with a value
[17:07:50] [AI] Batch done | 16/16 ok | 0 failed | wall=390.2s
```

**LLM 全部成功（16/16 ok），但一条都落不了库** → 整批回滚 → 「待分析」永不下降。

### 根因

`engine/ai_worker.py` 顶层有一行：

```python
from .utils import _detect_vip, _now, _open_db, _ts      # _ts 是个**函数**
```

某个函数内部为了存一个时间戳，写了：

```python
_ts = None
...
_ts = float(_last_refresh)
```

Python 的作用域规则：**函数体内只要出现同名赋值，该名字在整个函数内
都被判定为局部变量**（哪怕赋值在异常分支里、哪怕先读后写）。

于是同一个函数里任何 `_ts()` 调用都会抛：

```
UnboundLocalError: cannot access local variable '_ts'
```

### 为什么它特别难发现

| 特征 | 说明 |
|---|---|
| 语法完全正确 | `py_compile` 通过，IDE 不报任何错 |
| 逻辑完全正确 | 读代码看不出问题 |
| 变量名合法 | `_ts` 是好名字，不是拼写错误 |
| **只在特定数据分支触发** | 只有 `last_updated` 恰好是字符串格式时才走到那行 |
| 症状离病因很远 | 抛错在批事务里，表象是「待分析不降」 |

### 修复

局部变量改名 `_ts` → `_last_refresh_ts`（ai_worker.py 的 baseline refresh 段）。

### 防线：AST 静态审计（新增 `scripts/audit_scope_shadow.py`）

这类 bug 靠人读代码发现不了 → 必须自动扫。

```bash
python3 scripts/audit_scope_shadow.py            # 全项目扫描
python3 scripts/audit_scope_shadow.py --selftest # 脚本自检
```

判据（三者同时成立才算 bug）：

1. 模块级有**可调用对象** X（函数/类/import 进来的名字）
2. 函数内有 **`X = ...` 赋值语句**
3. 函数内**调用了** X

少任何一个都安全。已排除的假遮蔽：函数内 `import`（合法局部导入）、
`global X` 声明、`except E as X`、推导式循环变量（Python 3 独立作用域）、
下标赋值里的读取部分（`d[k] = v` 的 `k` 是读不是写）。

> **脚本必须自证准确**：一个只会报噪音的审计脚本比没有脚本更糟 ——
> 大家会直接忽略它的输出。所以内置 `--selftest`：
> 用「2026-11-05 事故原样」做正样本（必须报），
> 用「局部 import」「下标赋值」做负样本（必须不报）。
> 自检已接入单测 `test_scope_shadow_audit_reports_zero` → 全项目零容忍。

---

## 10.14 「待分析」不降的**第五层**根因：冷却短于单批耗时（2026-11-05 晚）

### 症状

前四层都修好后，引擎日志变成：

```
[AI] 通道体检 ready=True reason=-          ← 熔断好了
  [BATCH] claimed 16 rows in 0.01s (ids=[11974, 11975, 11976, 11977, 11978])
[AI] 通道体检 ready=True reason=-
  [BATCH] claimed 16 rows in 0.01s (ids=[11974, 11975, 11976, 11977, 11978])
```

**同一批 id 反复被认领**，而 `PENDING` 还在涨（3700 → 3729）。

### 根因

队头阻塞的兜底机制是「落库失败 → 退回 PENDING + 写冷却时间 → 认领时跳过冷却中的行」。
但**冷却时长（120 秒）短于单批耗时（实测 wall=390.2 秒）**：

```
t=0s      批次开始（16 条并发分析 + 慢行情源）
t=120s    批次失败 → 退回 PENDING + 冷却到 t=240s
t=240s    冷却过期 → 同一批又被认领 → 再跑 390 秒
t=630s    又失败 → 又冷却 → 又被认领 …
```

**冷却形同虚设** → 队头被同一批永久霸占 → 新新闻永远排不上。

同样的问题在**看门狗回收**路径上也存在：原来 `processing_at = NULL`
（回收后立刻可被认领）→ 引擎重启后第一批又认领同一批 id。

### 修复

| 位置 | 改动 |
|---|---|
| `ai_worker.py` `_BATCH_RETRY_COOLDOWN_SEC` | 新增常量 = **900 秒**（15 分钟），实测单批最坏耗时 390 秒的 2 倍余量 |
| 落库失败回写 | `processing_at = now + 900s`（原来 120s） |
| 看门狗回收 SQL | `processing_at = now + 900s`（原来 `NULL`） |

> **教训**：任何「冷却/退避」机制，冷却时长必须 **> 单次操作的最坏耗时**。
> 否则它不是「延迟重试」，而是「立刻重试」。

### 回归测试

```bash
python3 -m pytest tests/test_v322_head_of_line_block.py -q
```

新增三条不变量：
- 冷却时长 > 实测单批最坏耗时（390.2 秒）
- 看门狗回收后写冷却时刻（不是 NULL），且冷却中的行认领不到
- 回收 SQL 占位符个数 == 参数个数（改 SQL 忘改参数会直接抛错）

> **实现细节**：冷却秒数用**真正的 SQL 参数占位符**传入：
> `datetime('now','localtime','+' || ? || ' seconds')`。
> 不能写成 `'+%d seconds'` —— 那是 Python 字符串里的字面量，
> 传到 sqlite 时 `%d` 不是合法绑定 → 直接抛
> `sqlite3.ProgrammingError: Incorrect number of bindings supplied`。
> 回归测试里已锁死 `sql.count("?") == len(params)`。

---

## 10.15 「待分析」不降的**第六层**根因：评审吃光主分析预算（2026-11-05 晚）

### 症状

前五层都修好后，队列终于开始流动（认领的 id 从 `[11974…]` 变成 `[11990…]`，
说明队头让位了），但**信号数不涨**：

```
[AI] 通道体检 ready=False reason=额度闸门：日 token 预算已用尽 (20077002/20000000)
[AI] 跳过认领：额度闸门：日 token 预算已用尽 (20077002/20000000)（熔断冷却中，零额度消耗）

  token            20.08M / 20.00M      (100.4%)  剩 0
  请求数                1.9K / 6.0K        ( 31.1%)  剩 4.1K
  信号条数                 43 / 500         (  8.6%)  剩 457
```

**请求数才用了 31%，token 却用满 100.4%** —— 说明单次调用极贵。

### 根因

`ai_call_stats` 最近 12 条**全是 `verify`（同行评审）**：

```
21:52:24|verify|minimax-peer2|ok|40698
21:52:21|verify|minimax-peer |ok|37991
...
```

同行评审每条高价值新闻要跑 **2 个模型**，而且**每次都带完整八段 prompt**
（10K+ token）。于是：

```
20.08M token ÷ 1.9K 请求 ≈ 每请求 10.5K token
20.08M token ÷   43 条信号 ≈ 每条信号 466K token   ← 荒谬
```

评审是「锦上添花」，但它把**主分析**的预算吃光了 → 全局闸门到线 →
主循环整体停摆 → 「待分析」冻结在 3700+。

### 修复：按用途分账（`LLM_PURPOSE_TOKEN_BUDGET`）

```bash
# .env
LLM_PURPOSE_TOKEN_BUDGET=verify=3000000,translate=1500000
```

| 用途 | 上限 | 语义 |
|---|---|---|
| `verify` | 3M（≈总额 15%） | 同行评审 —— 花完就停**自己**，主分析继续跑 |
| `translate` | 1.5M | 英文新闻翻译 —— 拿不到翻译就用原文入库 |
| 未列出 | 不限 | 走全局预算 |

实现：
- `llm_budget.usage_by_purpose(purpose)` —— 按用途统计今日 token/请求数
- `llm_budget.purpose_token_budget(purpose)` —— 解析配置
- `llm_budget.check_purpose_budget(purpose, est_tokens)` —— 该用途自己的额度检查
- `engine/ai_worker.py::_run_verify` —— 评审前先查自己的额度，超了就 `[PEER-SKIP]` 跳过
- `engine/webhook.py` —— 翻译被闸门拦截时**返回原文**而不是 `None`

> **`None` 那个 bug 很隐蔽**：翻译被额度闸门拦下时 `return None` →
> 调用方拿 `None` 继续做 `["..."]` 切片 → `TypeError: 'NoneType' object is
> not subscriptable` → **整条新闻被丢弃**（webhook 直接报错）。
> 现在返回原文（与函数末尾「所有模型失败 → 返回原文」同一语义）→ 零损失。

### 回归测试

```bash
python3 -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py -q
```

新增不变量：用途预算解析 / 默认不限制 / 超额拦截 / 额度内放行 /
评审路径必须接护栏（源码级）/ 翻译必须返回原文（源码级）。

---

## 10.17 评审（verify）成本失控 —— 全天 60% 的额度（2026-11-05 深夜）

### 实测数据

```sql
select purpose, count(*) as calls,
       round(sum(tokens_in+tokens_out)/1000000.0,2) as M_token
from ai_call_stats where ts >= strftime('%s','now','-1 day')
group by purpose order by M_token desc;
```

```
verify| 757 次 | 13.75M token   ← 评审
main  | 485 次 |  8.20M token   ← 主分析
gate  |1042 次 |  0.79M token
```

**评审 757 次调用烧了 13.75M token —— 占全天 60%，比主分析还多 56%。**
而今天只产出 43 条信号 → **每条信号 466K token**。

### 根因

评审（`_run_verify`）走的是 `_analyze_with` → `_call_llm_sync`，
即**完整主分析 prompt**：

```
八段推理链 system prompt
+ market_context（盘面快照）
+ performance_context（历史绩效）
+ review_insights（复盘经验）
+ SERVER VERDICT 块
+ kline / dim_event_type / calendar / stock / asia / origin / spa …
= 10K+ token 输入，max_tokens=4000
```

但评审**只需要「方向 + 分数」**（`_collect_reviews` 只读 `action` / `score` / `reasoning`）。
给一个只需要 3 个字段的功能，付 18K token 的代价 —— 纯浪费。

### 修复：精简模式（`lean=True`）

| | 主分析 | 评审（现在） |
|---|---|---|
| system prompt | 八段推理链（~4K 字） | 极简复核指令（~250 字） |
| 上下文注入 | 10+ 个块 | **0 个** |
| 新闻原文 | 全文 | 截断 2000 字 |
| max_tokens | 4000 | **300** |
| 单次成本 | ~18K token | **~1K token** |

实现：
- `_call_llm_sync(..., lean=False)` —— 新增参数
- `if lean:` 分支 —— 走独立的精简 HTTP 路径（自带熔断 / 额度闸门 / 速率闸门 / 埋点）
- `_analyze_with` 里 `_lean_call = (purpose == "verify")` —— 评审自动走精简
- `_PEER_REVIEW_SYSTEM_PROMPT` —— 极简指令，只要方向+分数，禁止编造

> **通用原则**：**功能复杂度要与 LLM 输入成比例**。
> 评审要 3 个字段却付了主分析的价钱 —— 这是"额度不够"的隐形黑洞，
> 比"重试太多次"隐蔽得多。

### 回归测试

```bash
python3 -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py -q
```

新增不变量（源码级）：`_call_llm_sync` 必须支持 lean / 必须存在精简分支 /
评审必须自动走精简 / 精简模式 max_tokens=300 / 原文截断 2000 /
评审 prompt 不含八段推理链关键词且 < 1500 字符。

---

## 10.18 门卫把「模型没回答」当成「不是噪音」—— 2340 条真实新闻被丢弃

### 症状（前端截图）

实时信号台满屏「待 AI 分析」「门卫待回放」，但：
- 数据库 `ai_decisions` 有 3514 条 DONE
- 前端「窗口已分析信号 0」
- `raw_news` 里 **2332 / 3796（61%）是 `is_noise=1` + `status='PENDING'`**

抽样看到被判噪音的「噪音」：

```
id=18416 | 波兰央行行长格拉平斯基重申反对波兰加入欧元区
id=18415 | 俄罗斯军事情报总局局长科斯秋科夫…
id=18412 | 国际金融协会（IIF）：10月份新兴市场股票流出317亿
id=18410 | 美联储巴尔金：加息可以影响需求…
```

**全是正常财经新闻。** 它们的 `gate_verdict` 全是 `排除`。

### 根因

`_harden_verdict` 的第一条硬规则：

```python
if not is_material or not assets or not core:
    return "排除"
```

而这三个值来自**模型输出**：

```python
is_material = bool(step1.get("is_material"))   # step1 缺失 → False
assets     = [a for a in (step2.get("assets") or []) if a in _ALLOWED_ASSET][:3]
core       = str(raw.get("core_impact") or "").strip()
```

**模型漏输出 `step1` / `step2` / `core_impact` 时，三个判断全为假 → 直接"排除"。**

问题在于：**「模型没回答」≠「模型回答了不是」**。
前者是**不可判定**，按 v320 已确立的原则（"不可判定 ≠ 是噪音"）必须保守放行。

更糟的是这些行 `status` 停在 `PENDING`（不是 `FILTERED`）→ 认领 SQL 要求
`is_noise=0` → **永远不会被分析、也不会被标 DONE**，却一直占着「待分析」计数。

### 修复

`_harden_verdict` 新增 `missing_fields` 参数：

```python
if not missing_fields and (not is_material or not assets or not core):
    return "排除"
if not missing_fields and verdict not in ("可交易", "排除"):
    return "排除"
# 规则 3/4（分析类观点稿 / 低影响力）不依赖缺失字段 → 仍然生效
```

`missing_fields` 由 `_l3_semantic_gate` 计算：
`step1` / `step2` 整个缺失，或 `core_impact` 键不存在 → True。

> **保持不放水**：模型**明确**回答「不重要 / 没影响标的 / 写不出核心影响」→ 照样排除。
> 观点稿（分析类）+ 低影响力 → 字段缺失时也照样排除（这两条不依赖缺失字段）。

### 回归测试

```bash
python3 -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py -q
```

四条不变量：字段缺失不排除 / 明确否定仍排除 / 观点稿与低影响力仍排除 /
默认参数维持旧行为。

---

## 10.19 认领时效门槛 —— 别把额度烧在用不上的老新闻上

### 症状

引擎日志满屏：

```
[AI] 信号过期，跳过建仓: news=#11982 已 21315 分钟（> 45 分钟 / 2700s），仍入库统计不交易
```

**21315 分钟 = 14.8 天前的老新闻。** 而认领 SQL 是 `ORDER BY id ASC`（最老优先）
→ 引擎一条条分析 14 天前的积压 → 建仓闸（45 分钟）把信号全丢 →
**额度照烧、信号一条也用不上**，队列还永远追不上（新新闻进来，老新闻排队）。

### 修复

新增 `NEWS_ANALYZE_MAX_AGE_HOURS`（默认 6 小时，0 = 不限制）：

```sql
AND (? <= 0 OR {ts_expr} >= ?)
```

`ts_expr` 走 `time_utils.sql_bj_ts_expr("timestamp")` —— `raw_news.timestamp`
是**北京墙钟**语义，必须用项目唯一入口归一化（v309 在 'T' vs ' ' 分隔上踩过坑）。

超过门槛的新闻不再送去分析 → 队列直接让位给新鲜新闻。

### 但光有门槛还不够 —— 还要归档

门槛加完后出现新问题：积压的 2500 条老新闻**永远不会被认领**，
却一直留在 `status='PENDING'` → 前端「待分析」显示 2500 条，实际一条也处理不了。

这些新闻在语义上已经死了：
- 建仓闸 `NEWS_TRADE_MAX_AGE_MINUTES=45` → 信号必然被丢弃（白烧额度）
- 认领门槛 6 小时 → 也不会送去分析

所以标 `FILTERED` 是**诚实**的结论（「已过期，不处理」），不是丢数据。

新增 `_expire_stale_news(conn)`：认领前把超龄的 PENDING 行批量标 FILTERED
（`gate_unavailable=1` 的冻结行不动 —— 它们还要等回放）。

### ⚠️ 踩坑记录：SQL 里的「开关参数」不能混用 AND / OR

归档 SQL 我先写成：

```sql
AND ? <= 0                    -- ❌ 反了
AND (ts IS NOT NULL AND ts < ?)
```

`max_age=6` 时 `6 <= 0` 为假 → **整条条件永不成立** → 归档 0 条，
而且**不报错**（SQL 合法，语义反了）→ 表现为「归档日志没出现、计数不降」。

认领 SQL 用的是 OR 形式（`? <= 0 OR (ts >= ?)`）→ 正确。

> **规则**：同一个「0 = 不限制」的开关，在不同 SQL 里必须**统一成一种形式**。
> 建议一律写成 `(? <= 0 OR <条件>)`（OR 形式），天然正确；
> 若必须用 AND 形式（因为后面还有别的条件），则写成 `(? > 0 AND <条件>)`。
> **两种形式混用就是这类静默 bug 的温床。**

### ⚠️ 时间口径：必须用 `ts`（epoch），不能用 `timestamp`

`raw_news.timestamp` 有**三种形态混存**：
`'T' 分隔北京` / `' ' 分隔 UTC` / `' ' 分隔北京`。

`time_utils.sql_bj_ts_expr()` 会把 `' '` 形态**当 UTC 再 +8 小时**
→ 对「空格分隔的北京墙钟」多算 8 小时 → 新鲜度判断整体偏移。

实测症状：6 小时前的新闻被算成「未来」→ **永远不匹配门槛** → 认领恒为 0 条
（`claimed 1 rows`）。

`raw_news.ts` 是 **epoch 整数**（实测 8464 行全是 integer）→ 直接和 epoch 比较，
零歧义、零时区坑。

```sql
AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))   -- 门槛
AND (? <= 0 AND (ts IS NOT NULL AND ts < ?))  -- 归档
```

> **教训**：做「按时间过滤」时，**先确认那一列的语义**。
> 同一张表里混着两种时间格式时，任何字符串比较都可能整体偏移 8 小时。

### 回归测试

`tests/test_v322_head_of_line_block.py` 五条：SQL 必须带门槛 / 老新闻不被认领 /
门槛关闭时保持旧行为 / 超龄行被标 FILTERED（新鲜的不动）/ 门卫冻结行不被归档。

---

## 10.20 单点故障修复：两把独立的 MiniMax key（2026-11-06）

事故期间所有 `MINIMAX*_KEY` 槽位都被指向**同一把 key** → 面板显示「有备用通道」，
实际共用一份配额 → **一把 key 网络抖 → 全线停摆**（假冗余比没冗余更危险）。

现在的分配：

| 槽位 | 账户 | 用途 |
|---|---|---|
| `MINIMAX_API_KEY` / `MINIMAX_MAIN_KEY` | **账户1** | 主分析热路径（吃额度最凶） |
| `MINIMAX2_API_KEY` / `MINIMAX_GATE_KEY` / `MINIMAX_ANOMALY_KEY` / `MINIMAX_REFLECT_KEY` | **账户2** | 门卫 / 归因 / 反思（独立配额） |

**两把 key 的额度互不影响** —— 账户1 打满时门卫走账户2 照常工作。

> 换 key 后旧熔断状态**自动作废**（凭据指纹机制，见 10.12）→ 换 key 立即生效。

### ⚠️ 实测：两把 key 里有一把已经 402

```
账户1 (UzjSivSJ…) → HTTP 402 insufficient_balance_error
                    「当前已达到 Token Plan 用量上限」
账户2 (G504kuXI…) → HTTP 200 ✅
```

**Token Plan 用量上限是账户级的**，主分析一天跑 20M token → 账户1 到顶。
所以当前分配是「**主分析走有余额的账户2**，账户1 留作备用（充值后再切回）」。

> **教训**：换 key 之前必须**逐把验证可用性**（`curl` 打一次 chat/completions）。
> 两把都写进 `.env` 但其中一把已 402 → 主链路直接死，且面板显示「有备用通道」。

```bash
# 逐把验证（换成你的 key）
for K in "$KEY1" "$KEY2"; do
  curl -s -o /dev/null -w "HTTP=%{http_code}\n" -m 30 \
    https://api.minimax.chat/v1/chat/completions \
    -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
    -d '{"model":"MiniMax-M3","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
done
```

---

## 10.21 行情快照 97 秒 —— 「每周期各 8 秒」的累加陷阱

### 症状

```
[SNAPSHOT] PARTIAL (3/4 OK) in 97.609s
[SNAPSHOT] PARTIAL (1/4 OK) in 51.142s
[AI] Batch done | 1/1 ok | 0 failed | wall=154.4s
```

单批 154 秒 —— 队列降得极慢。

### 根因（两处累加）

```python
# ① 多周期 K 线：4 个周期**串行** result(timeout=8)
for interval, fut in futures.items():
    candles = fut.result(timeout=8.0)     # 每个的计时从调用那刻起算 → 4×8=32s

# ② 多资产：**串行** await
for asset_id, task in tasks.items():
    raw_results[asset_id] = await task    # 每个 task 内部已并发启动，
                                          # 但 wait_for 的计时器也是各自起算
```

32 秒（K线）+ 10 秒（ticker/funding）= 42 秒/资产 → ×4 资产累加 ≈ **97 秒**。

**关键**：外层 `asyncio.wait_for(timeout=8)` 只是**不再等**，**线程仍在后台跑**
（Python 无法强杀线程）→ 线程池被慢请求占满 → 后续请求排队 → 越来越慢。

### 修复

| 位置 | 改动 |
|---|---|
| `_fetch_mtf_ohlcv` | 新增 `total_timeout`（默认 8 秒），用**统一 deadline**；预算用尽即放弃剩余周期（不再每周期各 8 秒） |
| `get_snapshot` | 改用 `asyncio.gather(*tasks, return_exceptions=True)` **并发等待**，总耗时 = max 而非 sum |

### 回归测试

`tests/test_v322_breaker_epoch_and_gate_freeze.py` 两条（源码级）：
不得再有「每周期固定 8 秒」写法 / 必须用 `asyncio.gather` 并发等待。

---

## 10.22 「全通道不可用」静默停摆 —— 补上主动告警（2026-11-07）

### 缺口

临时 key 一旦额度用尽/失效，主循环进入 `pool-blocked`：

```
[AI] 通道体检 ready=False reason=全部通道熔断
[AI] 跳过认领：全部通道熔断（熔断冷却中，零额度消耗）
```

引擎**不认领、不消耗额度、也完全不告警** —— 只有日志。
用户看到的现象是「待分析不涨、也不更新了」，但**收不到任何通知**，
只能靠猜。这就是反复投诉的"随机抽风"里最隐蔽的一种。

### 修复

`ai_worker.notify_pool_blocked(reason)` + `llm_budget.push_critical()`：

| 场景 | 行为 |
|---|---|
| 首次阻塞 | **立即**告警（微信 + 邮件 + PushPlus 并联派发） |
| 持续阻塞 | 每 **6 小时**再告警一次（≤4 次/天） |
| 原因变化 | **立即**告警（不等 6 小时） |
| 通道恢复 | 告警一次（提示可以换回 key 了） |
| 从没告警过阻塞时的"恢复" | 不告警（不浪费额度） |

告警状态落盘在 `runtime/pool_blocked_alert.json` → 跨进程共享 + 重启后不重复轰炸。

### ⚠️ Server酱每天只有 5 次发送额度

实测：`[AUTH] 超过当天的发送次数限制[5]`。所以：
- **节流是硬要求**，不能每次循环都发；
- 派发走**多通道并联**（`notification_channels.dispatch`），
  微信挂了还有邮件兜底 —— 实测自检派发：`email 送达 / wechat_sct 400`。

### 告警正文里写清楚"怎么办"

```
🚨 LLM 通道全挂 —— AI 分析已暂停
原因：全部通道熔断: minimax1

影响：引擎**不再认领**新闻（零额度消耗），但「待分析」会一直涨。

处理：
  1) 换一把可用 key：
     cd ~/Desktop/金融1.0/backend
     python3 scripts/rotate_minimax_keys.py <新key>
  2) 重启引擎加载新 key：
     launchctl kickstart -k gui/$(id -u)/com.trident.engine
  3) 或者给现有 key 充值（MiniMax Token Plan / 积分）
```

### 新增回归单测

`tests/test_v322_pool_blocked_alert.py`（8 个用例）锁死 6 条节流不变量：
首次必发 / 6 小时内不重发 / 超窗再发 / 原因变了立即发 /
没告警过就不报恢复 / 告警过就要报恢复 / 状态落盘 / 正文含操作步骤。



## 10.23 「待分析」不降的**第七层**根因：数据库 CHECK 约束（2026-11-07）

### 症状

前端「待分析」**2511 条**、库里 `PENDING` **4851 条**，两天只涨不降。引擎日志里
`通道体检 ready=True` 一路刷，但 `[BATCH] claimed` 只认领到 1 条新鲜新闻。

### 根因：不是逻辑，是**数据库约束**

```
$ sqlite3 trident_event_bus.db "UPDATE raw_news SET status='FILTERED' ... "
Error: stepping, CHECK constraint failed:
       status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')
```

`raw_news.status` 的 CHECK 只允许 4 个值。而我们把「超龄/僵尸行」的终态命名为
**`FILTERED`** —— 这个值**从来就不被允许**。于是：

```
ai_worker._expire_stale_news()  → UPDATE 被数据库拒绝 → IntegrityError
  → except Exception: return 0   ← 静默吞掉（这里没有任何日志）
  → 库里 0 条 FILTERED，日志 0 行
  → 「待分析」只涨不降
```

实测：4851 条 PENDING **100% 满足归档条件**（`gate_unavailable=0`、`ts < cutoff`），
却一条都没被归档。同一个错误在 `scripts/cleanup_noise_pending.py` 里也有一份 ——
所以那个清理脚本**从来没生效过**。

### 为什么单测没抓到（这才是关键教训）

`tests/test_v322_head_of_line_block.py` 的 `RAW_NEWS_DDL` 是**自己造的假表**，
**没有 CHECK 约束**。测试里写 `status='FILTERED'` 当然成功 → 测试全绿；
生产用带 CHECK 的真表 → 每条 UPDATE 都被拒 → 功能静默死亡。

> **测试用假 schema、生产用真 schema，是这类 bug 能溜过去的根本原因。**
> v322 的 10.13（作用域遮蔽）和 10.18（门卫误杀）也是同一类：
> 「代码逻辑看着对、单测也过，但生产环境有额外的约束没被复现」。

### 修复（4 处）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src_python/db.py` | `_CREATE_RAW_NEWS` 的 CHECK 增加 `'FILTERED'` |
| 2 | `src_python/db.py` | 新增 `_upgrade_raw_news_status_check()` —— 重建表放宽老库的 CHECK（幂等） |
| 3 | `src_python/engine/ai_worker.py` | `_expire_stale_news` 失败**必须打日志**（不再静默吞）；新增 `_run_expire_stale_news()` 独立连接执行；归档从认领事务里**移出**并提到**熔断检查之前** |
| 4 | `src_python/event_status.py` + `frontend/` | 新增规范状态 `FILTERED`（标签「已归档」），前端徽章不再显示成「待 AI 分析」 |

另外 `api_server` 的 `pending_news` 计数加了一道口径兜底：
只统计**时效门槛内**的 PENDING 行 —— 即使归档再出问题，面板数字也不会骗人。

### ⚠️ 重建表时踩过的两个雷（务必记住）

`ai_decisions` 有 `FOREIGN KEY (news_id) REFERENCES raw_news(id) ON DELETE CASCADE`，
而 `db.get_connection()` 里 **`PRAGMA foreign_keys=ON`**：

1. **DROP 旧表会触发隐式 `DELETE FROM` + 级联** → 把几千条决策记录删光。
   必须先 `PRAGMA foreign_keys=OFF`（该 PRAGMA 在事务内是空操作 → 先 `commit()`）。
2. SQLite ≥ 3.25 默认 `legacy_alter_table=OFF` → `ALTER TABLE ... RENAME TO`
   会**改写其它表里的外键引用**（指向临时表名）→ 必须临时
   `PRAGMA legacy_alter_table=ON`。

新表 DDL **从线上表自身的 SQL 文本派生**（只替换 CHECK 子句），
不用 `_CREATE_RAW_NEWS` —— 线上表还有一堆 `ALTER TABLE` 补出来的列
（`processing_at` / `net_origin_info` / `cross_net_*` / `retry_count` / `url` …），
用固定 DDL 重建会把它们全丢掉。

### 迁移与验证

```bash
cd ~/Desktop/金融1.0/backend
python3 scripts/migrate_raw_news_filtered.py --dry-run   # 先看
python3 scripts/migrate_raw_news_filtered.py             # 执行（自动备份 + 前后校验）
```

实测结果：`raw_news` 8466 行不变、`ai_decisions` 7928 条**一条没少**、
索引与外键引用完好、`FILTERED` 写入探针通过。

### 效果

```
PENDING   4851 → 1
FILTERED      0 → 4850
API /api/health/models  pending_news: 2511 → 1
```

引擎日志（自动归档，无需人工干预）：
```
[BATCH] 归档超龄新闻 1 条（超过 6 小时，已过期不可交易）→ 不再占用待分析计数
```

### 新增回归单测

`tests/test_v322_raw_news_filtered_status.py`（9 个用例，全部用**真实 DDL**）：

- `test_create_ddl_allows_filtered` —— DDL 真相源必须允许 `FILTERED`
- `test_expire_works_on_real_schema` —— 真实 DDL 上归档必须真的成功
- `test_expire_failure_is_not_silent` —— 失败必须留痕
- `test_migration_widens_check` / `keeps_rows_columns_index` /
  `does_not_cascade_delete_children` / `keeps_foreign_key_pointing_at_raw_news` /
  `is_idempotent` —— 迁移的 5 条安全不变量

**以后新增任何写 `raw_news.status` 的代码，必须同时更新 `event_status.VALID_STATUSES`
和 DB 的 CHECK 约束**，否则就是下一个「待分析不降」。


## 10.24 全库「静默失效」清扫 —— 把同类 bug 一次性根治（2026-11-07）

### 为什么还要扫

10.23 那个 bug 的杀伤力不在于"写错了一个状态值"，而在于
**它失败了却没人知道**：`except Exception: return 0` 把数据库的拒绝吞得干干净净。

只要这类写法还在，**下一个同类 bug 依然会藏两天**。
所以不能只修那一个点，要把这类写法**全库清零**。

### 两个审计脚本（可重复运行）

```bash
cd ~/Desktop/金融1.0/backend
python3 scripts/audit_status_constraints.py   # 代码写的值 vs 数据库 CHECK 约束
python3 scripts/audit_silent_except.py        # 写库/调用路径上的静默吞异常
```

| 脚本 | 判定方式 | 覆盖 |
|---|---|---|
| `audit_status_constraints.py` | 读线上库所有 `CHECK (col IN (...))` → 扫源码里写该列的 SQL 字面量 → 交叉比对 | 29 张表 / 16 个受约束写入点 |
| `audit_silent_except.py` | **AST 精确判定**：try 体里有 `INSERT/UPDATE/DELETE` 的 execute 或外部网络调用，且 handler 是 `pass`/`return None` 且**没有任何日志** | 全库 src_python |

> 两个脚本各自踩过坑，都记在脚本注释里：
> - status 审计第一版**逐行扫 SQL** → 本项目 SQL 多半是多行拼接 → 只抓到 11 处（漏 90%）；
>   改成全文 DOTALL 扫描后才是 16 处。
> - status 审计第二版给 UPDATE 正则加了 `(;|$)` 结尾约束 → 多行拼接的 SQL 没有分号 →
>   **整段匹配失败**，连 `FILTERED` 自己都漏了。
> - 静默审计第一版用"前 25 行有 execute"做上下文猜测 → 误报 111 处（大半是 `conn.close()`）；
>   改成"精确分析 try 体"后才是 14 处真问题。

### 清扫结果

```
静默吞异常：关键模块 14 处 → 全库 0 处（含 12 个非关键模块）
status/CHECK：16 个受约束写入点，16 个通过，0 违规
```

修掉的几处**真会坏数据**的（按危险度）：

| 位置 | 静默失败的后果 |
|---|---|
| `ai_worker` 启动时重置残留 PROCESSING | **僵尸永不复位** → 那批新闻永远不被认领 →「待分析」不降 |
| `ai_worker` 失败批次退回 PENDING | **整批卡 PROCESSING** → 15 分钟后才被看门狗回收 → 队头阻塞 |
| `ai_worker` 门卫冻结过期放行 | 冻结新闻**永远不放行** →「待分析」不降 |
| `ai_worker` 门卫排除行标 FAILED | 该行卡 PROCESSING → 15 分钟后才回收 |
| `ai_worker` LLM 超时退回 PENDING | 同上 |
| `api_server` 访问详情标记已读 | 前端一直显示 UNREAD / 待分析 |
| `api_server` 重置时清孤儿表 | 重置后统计仍混入上一轮残留 |
| `db.py` first_seen_ts 拉平 | 同簇新闻"全网最早出现时间"不一致 |
| `db.py` 信号权重历史写入 | 命中率统计悄悄失真 |
| `signal_weight_evaluator` source_stats 写/改 | **命中率永远停在旧值** → 权重基于过期统计决策 |
| `kb_facade` 知识库查询 | 面板"没有数据"，分不清是真没有还是查询炸了 |
| `causal_chain_expander` audit_log | 扩链审计看着成功，实际一条没记 |
| 其余（event_struct / cross_net / insight_hit_ids / 手机推送 / baseline / K线缓存） | 字段缺失、推送不到、指标不更新 |

**净效果：以后任何 CHECK 违规 / 写库失败都会在日志里留下一行带异常类型和消息的记录。**

### 做成常驻门禁

`tests/test_v322_silent_failure_audits.py`（4 个用例）：
- 全库静默吞异常 = 0（跑审计脚本，退出码非 0 就测试失败）
- 审计脚本**自检**：人造一个静默吞异常，审计脚本必须抓到（防审计脚本本身失效）
- `db._CREATE_RAW_NEWS` 必须允许 `FILTERED`
- `event_status` 必须认得 `FILTERED`（合法状态 + 终态 + 标签 + 文案）

### 顺手清理的一个坑

`src_python/` 下有 **360 个 `* N.py`**（Finder/iCloud 复制产生的旧副本，文件名带空格 →
Python 永远 import 不到）。审计脚本已排除它们（改它们没有意义），但它们仍会**污染 grep**
——本次排查中我就被 `src_python/db 2.py:216: PRAGMA foreign_keys=ON` 误导过一次。

已移到 `backend/_dead_files_backup/`（**内容 100% 保留**，随时可移回），
`compileall` 全量通过，已确认**没有任何 wrapper/plist/代码引用它们**。

> ⚠️ **iCloud/Finder 会在后台不断造新的副本**：移走一批（296 个），
> 十分钟后又冒出 `* 3.py`（29 个），连 mtime 都是当天。
> → **不要跟它对抗**（移不完）。`audit_silent_except.py` 的排除规则已改成
> **正则 ` \d+\.py$`（任意数字）**，而不是写死 `" 2.py"`。

> ⚠️ **但 `tests/` 下的 `* N.py` 不能移**！
> 我第一次把 `src_python` 和 `tests` 一起清了 → **测试数从 3047 掉到 2698（少 349 个）**
> → 原因：**pytest 是按文件名 glob 收集的**，`test_xxx 2.py` 照样会被收集成用例。
> 我的"死文件"判断（"文件名带空格 → import 不到"）**只对 `src_python` 成立，
> 对 `tests/` 不成立** —— 因为 pytest 不 import，它按路径 glob。
> 已恢复 `tests/` 下全部 21 个文件。
> **教训：判断"死文件"不能只看语言层面能不能 import，还要看**收集器**（pytest/flake8）
> **会不会按路径扫到它。**



## 10.25 14 条能力实测验证（2026-11-07）

### 一键复测

```bash
cd ~/Desktop/金融1.0/backend
python3 scripts/verify_capabilities.py            # 人读版
python3 scripts/verify_capabilities.py --json     # 机器可读
```

只读统计，**不花 LLM 额度、不改数据**。结论全部来自线上库的真实行数与真实分布，
不是"代码里有没有这个函数"。

### 结论：✅ 能跑 8 · 🟡 半成品 2 · ❌ 没做 2

> ⚠️ **这是本节编写当时的快照**（10.25 刚做完时的状态）。
> 后续 10.26（补串联）、10.27（方向判别力/基准校正）又改了不少，
> **最终状态见 10.27 末尾的"能力验证的最终状态"**：
> **✅ 能跑 10 · 🟡 半成品 3 · ❌ 没做 0**。
> 本节表格保留原样，便于对照"改了什么、为什么改"。

| # | 需求 | 结论 | 实测证据 |
|---|---|---|---|
| 1/2 | 评估交易信号 & 信号权重 | 🟡 | 近 174 条主行决策：**BUY/SELL 仅 2 条(1.1%)**、`target_asset=NONE` 43%、`sentiment_score=0` 84%。**机制在跑，方向给不出来** |
| 2b | 信号权重自动学习 | ✅ | `source_stats` 53 行 + `signal_weight_history` 47311 行，命中率→权重倍数联动（feishu_jin10/XAU 命中 92.3% → 倍数 1.423） |
| 3 | 市场共识 / 涨跌逻辑 / 心理预期 | ❌ | **全库无实现**。命中的 6 处 "consensus" 是「多模型共识」（模型间一致），不是市场共识；无持仓/情绪/恐慌指标 |
| 4 | K线归因识别有效信号 | ✅ | 6435 行、平均\|涨跌\|=0.35%、**最大 5.04%**、**≥1% 的 425 条(6.6%)**。但 up/down=1887/1959 对称度仅 2% → **"多大"能识别，"往哪"接近随机** |
| 5 | 数据串联互通 | ❌ | `event_atom` 3340 → **`event_atom_decisions` = 0 行**（事件原子根本没连到决策）；因果链结果 2885 条**只有 10.3% 带 decision_id** |
| 6a | 快涨/慢涨分类 | ✅ | `move_kind`：fast 58 / slow 163 |
| 6b | 资金流向 / 持仓 / 杠杆 | ✅ | COT 持仓 **2067 行**；`market_regime` 9 类 regime（large_spec_long/short、funding_extreme_long/short…）+ 价格-资金背离。**订单簿/挂单量未实现** |
| 7 | 错过信号界面 + 三类错因细分 | ✅ | 221 行、`dimension` 3 类(A/B/C)、**7 个子类**、前端 `app/missed-signals` 存在；与反思侧共用同一套词表 |
| 8 | 因果图谱 + 第一性原理 + 自我纠正 | ✅ | `causal_chains` 628 → `causal_chain_outcomes` 2885（**HIT 1009 / MISS 400 / 方向命中 1113**）；`causal_chain_first_principles.py` 实现「机制/前提/证伪」三问 |
| 12 | 评估信息的市场影响力 | ✅ | 门卫 `gate_impact_1to100` 有值 **3235/3536** |
| 13 | 判断利好方向 | 🟡 | `event_type_code` 100% 有值，但 `target_asset` 只有 45% 明确 —— **方向常退化成 HOLD**（见能力1） |
| 14 | 市场基本面（角色/杠杆/套利） | ✅ | 同 6b。**套利/杠杆离场评估未实现** |

### 错因细分已落地（能力7 的实际分布）

```
C2_calibration_drift（信号虚高）      126  ← 最大头
B1_news_direction_inverted（方向错）   32
C1_feed_missing（数据缺失）           26
A4_pattern_recognition（纯形态）      22
B2_event_chain_break（事件链断）       8
A2_market_regime_misread（结构读错）   6
C3_signal_delay（信号延迟）            1
```
最大的一类是 **C2 信号虚高** —— 与能力1/2 的实测（84% 的 score=0、99% 退化成 HOLD）
**互相印证**：这不是某一条链断了，而是"给不出方向"这个现象在反思侧被正确归类了。

### 关于"因果图谱越跑越准"这个说法

**闭环是真的在跑**（预测方向+幅度 → 5天后对账 → HIT/MISS → 反馈改权重），
但**"越跑越准"目前没有证据**：

- `causal_chain_outcomes` 2885 条里只有 **297 条(10.3%)** 连到了具体决策
  → 绝大多数因果链的"对账结果"**没有回流到产生它的那次决策**，
  反馈修正的作用面很小
- `event_atom_decisions` **0 行** → 事件原子（第一性原理拆解的产物）
  **完全没进决策链路**
- K线归因方向 up/down 几乎 1:1 → 现有反馈信号本身**判别力弱**，
  用它去修权重，修的可能是噪声

所以：**链路在跑 ≠ 在自进化**。要让它真自进化，先要补的是"串联"（能力5），
否则反馈信号传不回决策端。


## 10.26 补串联（能力5）—— 从"链路在跑"到"能自进化"（2026-11-07）

### 起点：能力验证发现的两个断点

能力实测（10.25）发现串联是**断的**：

| 环节 | 断点 | 根因 |
|---|---|---|
| 事件原子 → 决策 | `event_atom_decisions` **0 行** | 表存在且带索引，但**全代码库零写入** —— 纯孤儿表。`ai_worker` 写完 event_atom 后只把 atom id 列表塞进 `ai_decisions.event_atom_ids`（atom→news 方向），**从没写过 atom→decision 方向** |
| 因果链结果 → 决策 | 2885 条 outcome 只有 **297 条(10.3%)** 带 decision_id | `link_causal_chain_decisions.py` **实现完整但没有任何调度器调用** —— 只能手动跑 |

### 中途踩的坑：手动跑一次，灌进 14 万条噪声

第一次直接跑 `link_causal_chain_decisions.py --days 120`：

```
配对 143444，新建链接 141557 → 链接表 2078 → 143635 行
```

看着是"补上了"，实际是**灌了一堆垃圾**，两个原因：

1. **它把 REJECTED 的决策也当成"成交决策"**。
   docstring 说"真实成交决策"，但 SQL 是
   `WHERE decision_ts >= ? AND news_id IS NOT NULL` —— **没过滤动作**。
   而 `trade_decisions` 3241 条里 **REJECTED 占 3187 条(98.4%)**，
   真正成交只有 BUY 42 + SELL 7 + CLOSE 5 = **54 条**。
   → 结果：**141207 条(98.4%)是噪声链接**，还会多出几千条待对账 outcome
   → 未来 `causal_weight_feedback` 拿这些噪声改权重 = **拿噪声修权重**
2. **匹配无上限**。每条决策把所有候选链全建一遍链接 → 实测**每条决策挂 261 条链**
   （403 条决策都是 261 条）—— 这不是关联，是笛卡尔积。

### 修法

**① 收紧脚本**（`src_python/link_causal_chain_decisions.py`）
- SQL 加 `AND final_action IN ('BUY','SELL','CLOSE')` —— 只关联真实成交
- 收集全部命中后**按命中词数排序取前 N**（`--max-chains-per-decision`，默认 3）
- 末尾加**收敛步骤**：对历史遗留的超链接，按
  `length(trigger_keywords_matched)` 排序，每条决策只留前 N 条
  （窗口函数 `ROW_NUMBER() OVER (PARTITION BY decision_id ORDER BY ...)`）

**② 清理已灌进去的噪声**（`scripts/cleanup_causal_link_noise.py`，可回滚）
- 删链接表里指向 REJECTED 决策的行
- 删/解绑由噪声产生、且尚未对账（PENDING）的 outcome
- **保留所有已对账的历史**（HIT/MISS/NEUTRAL/HIT_WEAK/EXPIRED —— 它们是历史事实）
- 被删 id 备份到 `runtime/causal_link_noise_backup_<ts>.json`

**③ 实现事件原子→决策**（`scripts/link_event_atom_decisions.py`，新建）
- `JOIN event_atom a ON a.raw_news_id = d.news_id`（索引 `idx_event_atom_news` 现成）
- 只连**主行**决策（副品种行不重复计）
- `weight` = 原子的 `direction_confidence`（方向越确定，对决策贡献越大）
- 幂等：`INSERT OR IGNORE`

**④ 加调度**（`com.trident.causal-link-cron.plist`，每小时）
- 之前 `link_causal_chain_decisions.py` **从来没被任何调度器调用过**
  —— 与 v321 门卫回放是同一个模式（"实现完整但没人调"）
- wrapper `~/.trident/wrappers/run_causal_link.sh` 串跑两个链接器，失败不影响主流程

### 效果

```
清理前: 链接表 143635 行（98.4% 噪声）| event_atom_decisions 0 行
清理后: 链接表     70 行（每条决策 ≤3 条链，100% 指向真实成交）
        event_atom_decisions 4417 行（覆盖决策 68.8%）
        outcome 带 decision_id 2429 条（BUY 2395 + SELL 34，无一条 REJECTED）
        其中已对账 60/70(86%)，待对账 10 条
```

能力验证从 **✅8 🟡2 ❌2** 变成 **✅9 🟡2 ❌1**（能力5 从 ❌ 转 ✅），
因果链方向命中数从 1113 → **2170**（清噪声 + 补真实关联的净收益）。

### 剩下的短板（不是串联问题）

串联通了，但**链本身的方向判别力弱**：
- 关联链的 `predicted_direction`：**BOTH 60/70(86%)**、UP 10、DOWN 0
- 即"这条链预测涨还是跌"这件事上，链基本没给出方向
- 这正是能力1/2/13 弱（BUY/SELL 仅 1.7%）的根因 ——
  **串联是管道，链的判别力是内容**。下一步该修的是内容，不是管道。


## 10.27 方向判别力的真相 —— 以及为什么"命中率好看"是假的（2026-11-07）

这一节是 ① 链的方向 → ② K线归因方向 → ③ 市场共识（最小实现）
三条线的**共同病根**，也是本轮最重要的产出。

### 病根：方向判定没有剥离市场漂移（beta）

因果链的方向命中率（改动前）：

```
DOWN 链: 740 样本, 命中率 63.5%   ← 看着很棒
UP   链: 1186 样本, 命中率 37.9%  ← 看着很差
样本期上涨基准: 36.4%             ← 市场整体偏跌
```

**DOWN 的 63.5% ≈ 市场基准 63.6%** —— 它只是"押对了大盘方向"，**相对基准的判别力是 0**。
UP 的 37.9% 也是基准。**两组都没有判别力，指标本身被 beta 污染了。**

同理，K线归因的 `event_kline_impact.direction` 旧实现用**窗口内极值**猜方向：

```python
up_move = (max_high - pre) / pre ; dn_move = (pre - min_low) / pre ; 谁大判谁
```

这是用"**振幅**"猜方向，不是"事件后价格往哪走" → 先冲高再回落的窗口也判成 up
→ 实测 up/down = 1887/1959（几乎 1:1）。

**归因页由此给用户看的"这条新闻导致了上涨"，有 58.5% 是错的。**

### 修法：一切方向判定改用**超额**（相对同期市场组合）

新增两个市场基准函数（同一思路：等权中位数、抗极值、资产不足则不校正）：

| 函数 | 位置 | 用途 |
|---|---|---|
| `_market_benchmark_pct(conn, ts_t0, ts_t5)` | `causal_chain_evaluator.py` | 因果链：方向判定改用 `excess = pct_5d - bench` |
| `_market_bench_move(conn, symbol, t_start, t_end)` | `engine/event_kline_impact_worker.py` | K线归因：方向改用 `窗口末价 vs 事件前收盘 - 同期基准` |

K线归因的幅度 `impact_pct` **仍用极值** —— 幅度衡量"波动多大"，用极值是对的；
只有"方向"改用收盘价。

### 一次失败的尝试（已回滚，记录在此以免重犯）

曾尝试给 `_infer_direction` 增加**机制语义**判据（推升/买盘/流入 → UP；
压低/卖盘/流出 → DOWN），给 58 条链补上了方向。

**生产口径回测结果：方向命中率 43.9% → 低于随机。已回滚。**
原因：机制文本里的涨跌词是**相对别的资产**说的 ——
"地缘冲突（利空）→ 避险资金流入黄金（利多）"里"利空"指的是原油不是黄金，
纯文本词表会把方向**搞反**。→ 方向必须"对**这个资产**"判断，规则法做不到。

### 成果：方向判别力被**如实暴露**，并按族切分

加了**方向判别力守卫**（`causal_weight_feedback._apply_direction_guard`）：

```
全局方向判别力 = 0.4834（超额收益口径，随机 = 0.5）→ 触发守卫
被守卫（方向是反的）: WTI|UP 0.343 · XAU|DOWN 0.435 · BTC|DOWN · BTC|UP · ETH|UP
未守卫（真有判别力）: ETH|DOWN 0.767 · WTI|DOWN 0.660 · XAU|UP 0.579
```

**因果链不是完全没用 —— 是部分族有用、部分族有害。**
守卫精确切掉有害的那部分：判别力 ≤ 50% 的族，其"方向猜对"**不计入 credit**
（credit 拆成 方向部分 + 幅度部分，守卫触发时只留幅度部分）。

> 否则就是**拿噪声改权重** → 越跑越歪，"越算越准"永远不成立。

#### 守卫的滞回（防抖）

实测全局判别力 **48.3%**，离门槛 50% 只有 **1.66 个百分点** —— 样本多几十条就可能
翻过门槛 → 守卫反复开关 → 权重来回跳（"越跑越歪"的另一种形式）。

因此守卫加了**滞回**：

```
判别力 < 50%          → 守卫
判别力 > 55%          → 解除守卫
45% ~ 55%（滞回区）   → **保持上次状态**（不因噪声翻转）
```

状态落盘在 `runtime/direction_guard_state.json`（跨进程、跨轮次有效）。

### 复盘工具

```bash
cd ~/Desktop/金融1.0/backend
python3 scripts/backfill_chain_direction.py --dry-run   # 方向推断回测（看泄漏）
python3 scripts/refresh_chain_strategy_columns.py       # 刷新策略列 + 重算结论
python3 scripts/backfill_kline_impact_direction.py      # 回填 K线归因方向
```

`backfill_chain_direction.py` 自带**分组回测**（防泄漏的关键）：
```
★旧BOTH→新有方向: 542 条, 方向命中率 46.7%  (≈50%=无判别力, >70%=泄漏)
· 旧有方向→新有方向(未变): 1397 条, 命中率 50.0%
```

### 能力验证的最终状态

```
✅ 能跑 10   🟡 半成品 3   ❌ 没做 0
```

其中一条**自我纠错**：10.25 里我写"套利/杠杆离场评估：**未实现**"是**误判** ——
`market_regime` 的 `deleveraging`（套利离场）与 `leverage_buildup`（杠杆堆积）
本来就有，实测命中 13 个 regime：
`commercial_hedging · deleveraging · large_spec_long · large_spec_short ·
leverage_buildup · spec_extreme`
（另有 `risk_off_rotation` 集体撤资、`funding_extreme_*` 集中投资/撤离，本次未命中。）

### 仍然没做到的

| 需求 | 状态 | 原因 |
|---|---|---|
| **订单簿/挂单量** | ❌ 真没做 | 需要交易所 WebSocket 深度数据（币安/OKX depth stream），现有 `market_ticks` 只有分钟价。可以用 tick 密度/跳变做**流动性压力代理**，但那不是挂单量 |
| **真正的市场共识/心理预期** | 🟡 只有最小层 | 已实现"市场环境基准"（等权组合 + beta 剥离）；但持仓拥挤度之外的**情绪/恐慌/期权偏度**仍缺（需新数据源） |


## 10.16 为什么你「毫不知情」就被额度打满（预警静默失效）

### 症状

额度打满时没有任何通知，直到全局闸门拦停才发现「服务不更新了」。
而引擎日志里其实早就有线索：

```
[WECHAT] ERROR: HTTP Error 400: Bad Request
[WECHAT] ERROR: HTTP Error 400: Bad Request
```

**预警一直在失败，只是没人告诉你。**

### 根因（三层叠加）

| # | 根因 | 后果 |
|---|---|---|
| 1 | 额度预警**只发微信一个通道**（`only=["wechat_sct"]`） | 单通道故障 = 预警全静默 |
| 2 | Server酱 推送**不截断正文** | 免费版正文上限 32KB，超了直接 400 |
| 3 | `HTTPError` 被 `except Exception` 吞掉，**只打印异常摘要** | Server酱 把失败原因放在 body 里（"SendKey 无效"/"长度超限"），不打印响应体就永远查不出原因 |

### 修复

| 位置 | 改动 |
|---|---|
| `llm_budget._push()` | 改为**多通道并联派发**（微信 + 邮件 + PushPlus），并打印每个通道的成功/失败 |
| `engine/alerts.py::_send_serverchan_sync` | 正文截断 2900 字（与 `notification_channels` 一致）；单独捕获 `HTTPError` 并**打印响应体** |

> **设计原则**：关键告警**不能只有一个通道**。
> 单点故障的告警系统 = 没有告警系统。

### 回归测试

```bash
python3 -m pytest tests/test_v322_breaker_epoch_and_gate_freeze.py -q
```

新增不变量（源码级）：预警不得只发一个通道 / 必须多通道派发 /
通道失败必须可见。


