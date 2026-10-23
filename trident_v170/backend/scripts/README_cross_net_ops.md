# cross_net 全网溯源 · 日常运维工具

v170 在 `raw_news` 表加了 `cross_net_first_seen_ts` + `cross_net_source` 两个结构化
字段，让前端能用 4 档徽章（紫边精确 / AMBIGUOUS 灰 / 同源灰 / 不渲染）显示"事件
原始发生时间 + 来源"，取代不可信的 `first_seen_ts`（实测 94.7% 等于入库时间）。

但 **LLM 输出行为不可控**：MiniMax-M3 经常走关键词推断或长推演模式不输出完整
JSON，导致 cross_net 字段被跳；这就是为什么需要以下三个工具做日常巡检。

---

## 1. `watch_cross_net.py` — 本地事件看板

盯 `raw_news` 新进卡片的 cross_net 标记，每 5 秒刷新一次；末尾展示 24h 累计
填充率（filled / AMBIGUOUS / empty 三档）。

```bash
# TUI 模式（交互终端）
python3 scripts/watch_cross_net.py

# 单次快照（接 cron / 自动化）
python3 scripts/watch_cross_net.py --once

# JSONL 输出（前端 SSE / 数据管道抓取）
python3 scripts/watch_cross_net.py --json

# 调时间窗 / 间隔
python3 scripts/watch_cross_net.py --interval 10 --limit 20
```

输出示例（节选）：
```
=== Trident 事件看板 · cross_net 全网筛查 · since_id=0 · 09:04:18 ===
   id  status  入库Δt    cross_net_first_seen_ts    source
 5903  DONE       5m
 5902  DONE       7m
 5901  DONE      13m
=== 24h DONE 累计 === total=184  filled=  0(0%)  AMBIGUOUS=  0(0%)  empty=184(100%)
```

---

## 2. `sync_prompts.py` — 代码 vs DB 覆盖对齐检查

v173 把 `system_prompt` 等放进 `ai_main_prompts` 表让用户能在 `/api/ai/prompts`
页面编辑。但代码每次升级加新字段（v170 的 cross_net_* 就是新增的），如果用户没
手动去页面同步，LLM 仍然按用户旧覆盖跑 → 静默失配。

这个脚本做的事：
- 读 `src_python/engine/ai_worker.py` 的 `_SYSTEM_PROMPT` 常量（最新代码版）
- 读 DB `ai_main_prompts` 表里 `kind='system_prompt'` 的覆盖（可空）
- 比对 hash + 关键字段存在性，输出缺失列表

```bash
# 检查对齐状态（默认只读）
python3 scripts/sync_prompts.py

# 首次同步：DB 无覆盖时写入代码常量；有覆盖则跳过以保护用户编辑
python3 scripts/sync_prompts.py --init

# 强制同步：覆盖用户编辑（谨慎！）
python3 scripts/sync_prompts.py --force
```

输出示例：
```
代码常量: sha256=7a7799a2ddfd  长度=9831  字
DB 覆盖:  sha256=a0005956ad90  长度=9119  字

⚠️  hash 不一致 — 代码里有但 DB 没有的关键字段：
   （DB 覆盖包含所有关键字段；hash 差异来自其他文本/顺序）

💡 建议：
   1) 若需补齐关键字段 → 去 /api/ai/prompts 页面手动加缺失字段说明
   2) 若用户编辑已损坏 → python3 scripts/sync_prompts.py --force
   3) 若从未编辑过 → python3 scripts/sync_prompts.py --init
```

---

## 3. `daily_audit.py` — 每日抽检清单

从 `raw_news` 抽近 N 小时内 `status=DONE` 且 `relevance_score` 最高的 Top K 条，
列出 content + cross_net_* + net_origin_info，给每条加判断标签（✅ / 🔶 / ❌ /
⚠️），供人工核对 AI 是否漏填或瞎填。

```bash
# 默认近 48h top 5
python3 scripts/daily_audit.py

# 改时间窗和条数
python3 scripts/daily_audit.py --top 8 --hours 72

# 只看 id > since 的（避免重复抽）
python3 scripts/daily_audit.py --since-id 5900

# 同步写 markdown 到 news_exports/（便于 git 历史追踪 / 邮件）
python3 scripts/daily_audit.py --write-md
```

输出示例（节选）：
```
━━━ #5775  ❌ 完全未溯源  relevance=0.95  入库=2026-09-19T17:34:16+08:00 ━━━
  内容  : 【美联储紧急降息】金十数据9月19日讯，美联储今晚紧急宣布降息50个基点…
  cn_ts : '〈空〉'
  cn_src: '〈空〉'
  noi   : '〈空〉'
```

---

## 单测

```bash
python3 -m pytest tests/test_cross_net_ops.py tests/test_extract_cross_net.py tests/test_parse_result_e2e.py -v
# 42 passed in ~1s
```

---

## v170 全网溯源补丁（已应用 + 已验证）

### 补丁 A：`fallback3` 关键词推断 → 也带 cross_net 占位
**位置**：`src_python/engine/ai_worker.py` line ~3027  
**作用**：fallback3 现在返回 `cross_net_first_seen_ts=""`、`cross_net_source=""`、
`net_origin_info=""` 占位（仍合规 = "拒绝回答"），避免 `_sanitize_llm_result` 默认空串
污染 DB 而前端无法区分"AI 拒答"和"LLM 没填"。

### 补丁 B：Fallback 2.5 — MiniMax-M3 长中文推演弱信号抽取
**位置**：`src_python/engine/ai_worker.py` `_extract_cross_net_from_text()` line ~349  
**作用**：当 LLM 输出 2000 字中文推演但**没有 JSON 包裹**（MiniMax-M3 常见行为），
从纯文本里正则抽取：
- **时间**：4 种中文时间模式（YYYY年M月D日 H:M / YYYY-MM-DD H:M / 仅日期 / M月D日 H:M）
  + 跨年保护（M-D 比今天晚 30+ 天 → 回退一年）+ 范围保护（1990 ≤ 年 ≤ 当前+1）
- **来源**：10 个高信度中文模式（"X 官网新闻稿" / "据 X 报道" / "X 通讯社" 等）+ 6 个英文媒体
  + 必须以机构后缀结尾（社/网/台/局/部/院/所/行/公司/委员会/联储/央行/通讯社/Press/Reuters 等）
  + 防吞动词（negative lookahead 排除"紧急/立即/即将"）
- **net_origin_info**：拼装 `来源:X | 推断时间:Y`（≤200 字符）

### 已验证（v170 上线后）
- `_extract_cross_net_from_text` 单测 21 个 ✓
- `_parse_result` 端到端 5 个真实场景 ✓（M3 长推演 / 合法 JSON / 关键词推断 / 仅来源 / 无信号）
- 全套 299 个测试通过

---

## 日常三步落地建议

1. **盯新卡片** — 终端跑 `watch_cross_net.py`，看 filled 占比是否随时间爬升
   （LLM 在新 prompt 下学到填字段 + Fallback 2.5 兜底 → 填充率应每天 +5%）。
   如果仍是 0%，去 `/api/ai/prompts` 页面看 prompt 里 cross_net 字段说明是否还在
   （参考 `sync_prompts.py` 输出）。
2. **prompt 对齐** — 每次升级 v 都在 backend 跑一次 `sync_prompts.py`，看是否
   有"代码里有但 DB 没有的关键字段"，有则去 `/api/ai/prompts` 页面手工补一段。
3. **每日抽检** — 每天早上跑一次 `daily_audit.py --write-md --since-id 上次最大id`，
   打开生成的 markdown 文件，重点看 🔶 AMBIGUOUS（人工补判）和 ❌ 完全未溯源
   （确认是真没法溯源 vs AI 偷懒）。