# Security Hardening Changelog

按时间倒序记录 **后端 API** (`backend/src_python/api_server.py` + `config.py`) 的安全修复，
含 P0 / P1 / P2 等级。所有改动均配测试 (`tests/test_security_p0_fixes.py` + `p1_fixes.py`)。

| 等级 | 含义 | 修复延迟目标 |
|---|---|---|
| **P0** | 鉴权绕过 / 远程代码执行 / 凭据泄露 | < 24h |
| **P1** | Stored XSS / SSRF / 注入 / 资源耗尽 | < 7d |
| **P2** | 限流 / 监控盲点 / 配置漂移 | < 30d |

---

## v300+ 安全加固（最新）

### P2 — per-IP rate limit

**问题**：`/api/agent_chat` + `/api/agent_chat/upload` 此前**完全无限流**，单一客户端
（脚本小子 / 失控的 cron / 恶意浏览器扩展）可以在分钟内打满 LLM 配额 → 账单爆炸。

**修复**（`api_server.py` + `config.py`）：

- 新增 `_IPRateLimiter` 类 — 滑动窗口（60s）in-memory 实现，`threading.Lock` 线程安全。
- 新增 `_get_client_ip(request)` — 仅在直接连接方为 loopback 时采纳 `X-Forwarded-For`，
  **避免客户端伪造 XFF 绕过限流**。
- 新增两个 FastAPI 依赖 `_rate_limit_dep_agent_chat` / `_rate_limit_dep_upload`，
  通过 `Depends(...)` 注入到端点。
- 鉴权用户 (`X-Trident-API-Key` 有效) 自动享有 `RATE_LIMIT_AUTH_MULTIPLIER` (默认 10×) 配额，
  防止 admin / CI 被误伤。
- 触发限流：返回 `429 Too Many Requests` + `Retry-After` header + 结构化 JSON 错误体。

**新增配置**（`config.py`）：

```python
RATE_LIMIT_ENABLED             = os.getenv("RATE_LIMIT_ENABLED", "1") in ("1", "true", "yes", "on")
RATE_LIMIT_AGENT_CHAT_PER_MIN  = int(os.getenv("RATE_LIMIT_AGENT_CHAT_PER_MIN", "60"))
RATE_LIMIT_UPLOAD_PER_MIN      = int(os.getenv("RATE_LIMIT_UPLOAD_PER_MIN", "10"))
RATE_LIMIT_AUTH_MULTIPLIER     = float(os.getenv("RATE_LIMIT_AUTH_MULTIPLIER", "10.0"))
```

**测试**（8 个）：滑动窗口 / 多 IP 隔离 / 窗口过期重置 / XFF 仅 loopback 信任 /
鉴权用户高配额 / 触发 429 + Retry-After / RATE_LIMIT_ENABLED=0 关停。

**已知限制**：in-memory 单进程。多 uvicorn worker / 多实例需升级到 Redis。
目前是单 worker 部署所以 OK；多 worker 需把 `_log` 移到 `redis-py`。

---

### P2 — audit log 全模式覆盖

**问题**：原 `trident_demo_middleware` 在 `IS_FULL=True` 时**完全透传** — 也就是说
full / 生产模式下**没有任何审计记录**。攻击发生无法溯源（不知道哪个 IP / key 触发的）。

**修复**（`api_server.py::trident_demo_middleware`）：

- 移除 `if demo_mode.IS_FULL: return await call_next(request)` 短路。
- 审计全模式开启；高噪音端点前缀白名单跳过：`/health`, `/api/health`,
  `/api/visitor/`, `/api/agent_behavior/`, `/api/anti_bot/`, `/api/heartbeat`,
  `/favicon.ico`, `/robots.txt`。
- 审计失败 (DB 写错 / 磁盘满) → 落到 `trident.audit` logger warning，**不抛错**，
  业务请求继续。

**测试**（5 个）：full 模式也审计 / 跳过 health 端点 / 敏感端点记录 /
含 status + latency_ms / DB 写入失败业务仍正常。

**位置**：审计写入 `backend/trident_audit.db` (SQLite)，可用
`/api/admin/audit_log?limit=...` 查询或直接 `sqlite3 backend/trident_audit.db`。

---

### P3 — CORS 启动期自检

**问题**：`CORS_ALLOW_ORIGINS` 默认值是 `localhost:*`（开发端口）。full 模式
生产部署若忘记覆盖，前端 dashboard 直接被锁在外面；运维还得 SSH 上服务器改 .env。
另一个隐藏风险：`*` 与 `allow_credentials=True` 不兼容（浏览器拒绝 + CORS 误配置）。

**修复**（`api_server.py::lifespan` 启动期）：

- `*` 出现在 `CORS_ALLOW_ORIGINS` → 立即抛 `RuntimeError` 拒绝启动。
- `IS_FULL=True` 且 origins 仍是 localhost 默认 → 打印 ⚠️ 警告（不阻塞启动，
  兼容运维已经故意只跑内网 dashboard 的场景）。

**测试**（3 个）：`*` 硬拒绝 / localhost-in-full warning / prod 域名 silent。

---

### P2 — master_api token 日志保护 + 默认 token 警告

**问题 A**：`master_api.py:236` 启动日志打印 `MASTER_API_TOKEN[:8]`（明文前 8 字节）。
仓库公开 → 攻击者拿到日志后搜索空间从 `2^N` 降到 `2^(N-64)`，是不必要的降维。

**问题 B**：默认 token `"trident-master-token-change-me-32bytes-min"` 是公开常量。
如果用户忘了设 `TRIDENT_MASTER_TOKEN` env，**任何人都能用这个字符串通过鉴权** → 远程调用算法本体。

**修复**（`master_api.py`）：

- 启动日志改用 `sha256(token)[:8]` 指纹 + token 长度；token 明文任何前缀都不再打印。
- 新增 `_USING_DEFAULT_TOKEN` 标志（import 时检测）：若检测到默认 token，
  打印 🚨 CRITICAL 警告，提示用户必须 `export TRIDENT_MASTER_TOKEN=$(openssl rand -hex 32)`。
  fail-**loud** 不阻塞启动（dev 模式合法），但希望用户立即看到。

**测试**（4 个）：日志不含 token 明文 / 默认 token 标志 / 自定义 token 标志 /
critical 警告内容。

---

### P2 — audit log 自动 purge（防 SQLite 无界增长）

**问题**：`trident_demo_middleware` 在 Step 3 已开启全模式审计，但 `trident_audit.db`
从此**无界增长** — 单条记录 ~300 bytes，高频端点（chat 每分钟 60 + heartbeat 每秒 1）
跑一个月就 10MB+ → 一年后是 GB 级，备份/查询/磁盘都吃不消。

**修复**（`demo_audit.py` + `api_server.py::lifespan`）：

- 新增 `purge_old_records(retention_days: int) -> int` — 删 `ts < cutoff` 的旧数据；
  `retention_days <= 0` 时跳过（opt-out 开关）。
- `lifespan` 启动期拉起 `audit-purge-daemon` 守护线程，默认每 **6 小时**跑一次，
  保留 **30 天**（两个 env 可调：`TRIDENT_AUDIT_RETENTION_DAYS` /
  `TRIDENT_AUDIT_PURGE_INTERVAL_HOURS`）。
- `purge_old_records` 内部 `try/except`，DB 锁 / 磁盘满 → 仅 logger warning，
  不让审计清理拖死业务（与 `record()` 一致的容错哲学）。

**新增配置**：

```python
TRIDENT_AUDIT_RETENTION_DAYS        = int(os.getenv("TRIDENT_AUDIT_RETENTION_DAYS", "30"))
TRIDENT_AUDIT_PURGE_INTERVAL_HOURS  = float(os.getenv("TRIDENT_AUDIT_PURGE_INTERVAL_HOURS", "6"))
```

**测试**（3 个）：过期记录被删 / 新记录保留 / `retention_days=0` 跳过 /
DB 失败不抛错。

---

### P1 — sanitize 边缘疏漏 + 注入模式黑名单

**问题**：`_sanitize_user_message_for_llm` 此前只剥控制字符，**不识别自然语言注入**。
攻击者通过 prompt 拼接"忽略之前的指令" / "reveal your prompt" 即可让 LLM 偏离
data-copilot 任务输出 prompt 内容。

**修复**（`api_server.py`）：

- 新增 `_INJECTION_PATTERNS` blocklist（13 条；8 英文 + 5 中文；典型如
  `ignore previous instructions` / `disregard all` / `reveal your prompt` /
  `忽略之前的指令` / `你现在是` / `泄露你的系统提示` / `执行 SQL`）。
- 命中段包裹为 `>>>suspicious:<原句><<<`，**结构化标记**让 LLM 识别 data vs command。
  选择包裹而非删词：因为删词会被 unicode 同形字/拼音绕过；包裹只是给 LLM
  视觉提示，**纵深防御的额外一层**，主防御仍是 `_SCHEMA_TEXT` 的 untrusted 规则 + delimiter。
- None / bytes / 非字符串输入 → 统一返回 `(empty)` / `repr()`，避免 NoneType 异常。
- emoji / 数学符号天然不会被切半（Python 3 `str[:n]` 是 code-point 切片），无需额外防御。

**测试**（6 个）：非字符串输入 / 英文注入模式 / 中文注入模式 / 干净文本不变 /
strip 后空串返回 placeholder / emoji 完整保留 / 混合干净+注入。

---

### P0 — 之前已完成的修复

> 详见 `tests/test_security_p0_fixes.py` (19 测试)。概要：

- `/api/agent_chat/upload` 增加鉴权（防 Stored XSS via `/uploads` 静态服务）
- `/api/agent_chat` + `/api/agent_chat/upload` 全模式强制 `require_api_key`，
  full 模式默认强制（除非 `TRIDENT_ALLOW_ANON_FULL=1` 显式覆盖）
- 上传流式校验大小（chunk 32KB），不再 `await file.read()` 全量入内存
- `ALLOWED_UPLOAD_TYPES` 收紧：移除 `text/html` / `text/csv`（防 Stored XSS +
  CSV formula injection）
- `DEMO_DEFAULT_DB` 不再被 demo 模式读写（防越权读他人数据库）
- 导出 Excel 公式注入过滤（prefix `'` / 移除 `=` / `+` / `-` / `@` 起始字符）
- SQL 端点增加调用方白名单
- CORS methods/headers 显式白名单（替代 `*`）

---

## 测试覆盖

```bash
cd backend && python3 -m pytest tests/test_security_p0_fixes.py tests/test_security_p1_fixes.py --no-header
# 48 passed in 5.68s
```

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| `test_security_p0_fixes.py` | 19 | 鉴权 / 上传 / 导出 / CORS / demo DB 隔离 |
| `test_security_p1_fixes.py` | 29 | sanitize 边缘 / 注入模式 / rate limit / audit log / CORS 启动期 |
| `test_security_p2_fixes.py` | 7 | master_api token 日志 / 默认 token 检测 / audit log rotation |
| **合计** | **55** | |

---

## 环境变量一览（安全相关）

```bash
# 鉴权
TRIDENT_MODE=full|demo|licensed
TRIDENT_LICENSE_SECRET=<hmac-secret>          # 必设，否则 license 加载失败
TRIDENT_ALLOW_ANON_FULL=0|1                    # full 模式是否允许匿名 (默认 0)

# 上传
UPLOAD_DIR=/abs/path/to/uploads
MAX_UPLOAD_SIZE_BYTES=5242880                  # 5 MB

# Rate limit (P2)
RATE_LIMIT_ENABLED=1
RATE_LIMIT_AGENT_CHAT_PER_MIN=60
RATE_LIMIT_UPLOAD_PER_MIN=10
RATE_LIMIT_AUTH_MULTIPLIER=10.0

# CORS
CORS_ALLOW_ORIGINS=https://your-domain.com,https://admin.your-domain.com

# Audit
TRIDENT_AUDIT_DB=/abs/path/to/trident_audit.db
TRIDENT_AUDIT_RETENTION_DAYS=30            # P2: 默认保留天数；0 = disable cleanup
TRIDENT_AUDIT_PURGE_INTERVAL_HOURS=6       # P2: 守护线程触发间隔

# Master API (localhost-only)
TRIDENT_MASTER_TOKEN=$(openssl rand -hex 32)   # 必须显式设置；默认 token 是公开常量
TRIDENT_MASTER_PORT=9001
```

---

## 报告漏洞

见根目录 [`SECURITY.md`](../SECURITY.md)。邮件：`z2132743607@163.com`，
标题请加 `[backend-api]` 前缀。