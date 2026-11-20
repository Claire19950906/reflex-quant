# Security Policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| v225+   | ✅ active dev      |
| v170–v224 | ⚠️ critical fixes only |
| < v170  | ❌ no longer supported |

## Reporting a vulnerability

**Please do NOT open a public GitHub issue for security bugs.**

Email **z2132743607@163.com** with:

1. A clear description of the vulnerability and its impact
2. Reproduction steps (PoC preferred)
3. Affected commit / version
4. Your name / handle for the credits list (optional)

You should hear back within **48 hours**. We will:

- Confirm receipt within 48h
- Provide an initial assessment within 5 business days
- Coordinate a fix + disclosure timeline (typically 30–90 days)
- Credit you in the release notes unless you prefer to stay anonymous

## Threat model (what we care about)

Reflex Quant is a **local-first** system. Your `.env` file holds the API keys
that matter most. The biggest realistic threats are:

1. **API key leak via committed `.env`** — guarded by `.gitignore`
   (`*secret*`, `*credential*`, `*private*`). Run
   `git log --all --diff-filter=A -- backend/.env` to verify yours.
2. **SQLite WAL file leakage** — `.gitignore` covers this. Backup rotation
   scripts (`scripts/backup/`) keep copies under `backups/` which is also
   gitignored.
3. **LLM prompt injection via malicious news feeds** — the L1 keyword filter
   + L2 LLM classifier are designed to drop injection attempts, but new
   vectors may appear. If you find one, please report.
4. **Live-trading mode bypass** — `TRADING_MANAGER_ENABLED` defaults to
   *off*. The fail-closed gate is intentional; do not change it without
   reading the comment in `backend/src_python/config.py`.

## Out of scope

- Vulnerabilities in upstream dependencies (report to OpenRouter, FastAPI,
  Next.js, etc. directly)
- Theoretical attacks without a PoC
- Best-practice recommendations that are not exploitable

---

## v300 — 体验模式 & 反逆向（IP 保护）

Trident 的核心算法（`engine/`、`src_python/strategy/`）是私有 IP。
v300 起提供三种部署模式，由 `TRIDENT_MODE` 切换：

| 模式 | 谁用 | 看到啥 | 是否可下载源码 |
|---|---|---|---|
| `full` | 开发者本人 | 全精度 + 全部功能 | ✅ 本地 |
| `demo` | 公开/朋友 | 2 位小数 + 1 天历史 | ❌ 体验镜像 |
| `licensed` | 受邀请用户 | 4 位小数 + 7 天历史 | ❌ 体验镜像 |

**关键不变量**：

- License 文件是 HMAC-SHA256 签名的 JSON，**漏设 `TRIDENT_LICENSE_SECRET` → 拒加载**（fail-closed）
- demo / licensed 模式所有响应都带 `X-Trident-Watermark` 头（可定位截图泄漏）
- demo 模式 60s 内超过 `TRIDENT_DEMO_RPM`（默认 30）→ 429
- 所有调用写到 `TRIDENT_AUDIT_DB`（SQLite），按 IP + 路径聚合

详细威胁模型、5 层防御、License 文件格式、应急响应见：

- [`docs/SECURITY_DEMO_MODE.md`](./docs/SECURITY_DEMO_MODE.md) — 设计 & 威胁模型
- [`docs/SECURITY_DEMO_MODE_DEPLOY.md`](./docs/SECURITY_DEMO_MODE_DEPLOY.md) — 三种发布场景的部署手册

**报告 demo / licensed 模式相关问题**：同样发到 `z2132743607@163.com`，
标题请加 `[v300-demo]` 前缀。

---

## 后端 API 安全修复历史

最近一次完整加固（2026-Q1）覆盖了 P0（鉴权绕过）+ P1（注入 / Stored XSS）+
P2（限流 / 审计 / CORS 自检），共 **48 个测试**。

| 等级 | 修复内容 |
|---|---|
| P0 | 鉴权强制 / 上传鉴权 / 流式大小校验 / 导出公式注入 / SQL 白名单 / CORS methods 白名单 |
| P1 | sanitize 边缘疏漏 + 13 条注入模式黑名单（English + 中文） |
| P2 | per-IP rate limit (60/min chat + 10/upload) / 审计日志全模式开启 + 自动 purge / CORS 启动期 `*` 硬拒 + localhost warning / master_api token 日志保护 + 默认 token 警告 |

完整 changelog、环境变量速查、测试清单见
[`backend/docs/SECURITY_CHANGELOG.md`](./backend/docs/SECURITY_CHANGELOG.md)。
