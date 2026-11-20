# Trident v300 — 体验模式 & 反逆向安全模型

> 本文件是 `SECURITY.md` 的「对外发布场景」专项补充。  
> 读者：Trident 维护者、DevOps、给受邀体验者的对接人。

---

## 1. 三种部署模式

| 模式 | 触发方式 | 目标用户 | 是否可读源码 | API Key | License | 输出 | 限流 | 审计 |
|---|---|---|---|---|---|---|---|---|
| **full** | `TRIDENT_MODE=full`（默认） | 开发者本人 | ✅ 是 | 不要 | 不要 | 全精度 | ❌ | ❌ |
| **demo** | `TRIDENT_MODE=demo` | 公开/朋友 | ❌ 体验镜像 | 不强制 | 不要 | 2 位小数 + 历史 1 天 | ✅ 60s/30 次 + 每日 5k | ✅ |
| **licensed** | `TRIDENT_MODE=licensed` | 受邀请用户 | ❌ 体验镜像 | ✅ 必填 | ✅ 必填 | 4 位小数 + 历史按 tier | ✅ 60s/120 次 + 每日 50k | ✅ |

**一句话规则**：源码只在 `full` 模式下挂载；`demo`/`licensed` 永远只发体验镜像。

---

## 2. 威胁模型

| 攻击面 | 威胁 | 当前防御（v300） |
|---|---|---|
| 客户端反编译 JS | 拿到「信号」公式 | 公式在后端，前端只展示脱敏数 |
| 抓包分析 API | 重放 + 推断算法结构 | 限流 + 数值截位 + 水印 + 历史截断 |
| 爬虫遍历所有股票 | 拼凑训练数据 | 60s 滑窗 + 每日配额 + 404 防枚举 |
| 盗版镜像 | 复制 `TRIDENT_LICENSE_SECRET` 复用 | HMAC 验签 + 设备指纹（可选）+ 启动自检 |
| 截屏泄漏 | 别人看到精确数字 | 演示精度只到 2-4 位小数 |
| 内部审计 | 不知道谁在什么时间拉了什么 | SQLite 审计表 `trident_audit.db` |

**不防的**（明确告知体验者）：

- 真的有 0day 攻破 Linux 内核 → 攻破者能看到所有内存，包括脱敏前的数
- 抓 HTTPS 包 → 用 `LICENSE_SECRET` 重签的合法请求我们认
- 体验者截屏后 OCR → 输出精度是主动压低的，对外就承认这点

> **核心防御思路**：算法在云端，IP 不可下放。客户端就算完全开源，也只能拿到「经过裁剪的」信号。

---

## 3. 5 层防御（代码视角）

```
┌───────────────────────────────────────────────────────┐
│ L5  审计层  demo_audit.py   → SQLite 按 (IP, ts) 记录 │
├───────────────────────────────────────────────────────┤
│ L4  输出层  demo_mode.sanitize_number / trim_history  │
│              / make_watermark / @demo_guarded         │
├───────────────────────────────────────────────────────┤
│ L3  流量层  demo_mode.check_rate_limit  60s 滑窗 +    │
│              每日 token 配额                            │
├───────────────────────────────────────────────────────┤
│ L2  鉴权层  license_auth.require_license              │
│              + require_api_key  (HMAC + 设备指纹)     │
├───────────────────────────────────────────────────────┤
│ L1  架构层  算法 + 模型在 server/ 私有仓库；           │
│              客户端只拉「分位档」,不传 raw feature     │
└───────────────────────────────────────────────────────┘
```

每一层独立 fail-closed：

- L1 失败 → 没源文件，本质无 IP
- L2 失败 → `401`/`403`，不进入 L3-L5
- L3 失败 → `429`，不计费
- L4 失败 → 数字精度降为 0，业务仍跑
- L5 失败 → 记录并告警，但请求正常返回

---

## 4. License 文件格式

签发（admin 跑）：

```bash
python3 scripts/gen_license.py issue \
    --tier pro \
    --issued-to "alice@friend.com" \
    --days 90 \
    --fingerprint $(python3 -c "import license_auth; print(license_auth.get_machine_fingerprint())") \
    --features realtime,historical_7d \
    --secret "$TRIDENT_LICENSE_SECRET" \
    --out /etc/trident/license.lic
```

文件内容（实际是单行 JSON）：

```json
{
  "license_key": "tr-XXXX-XXXX-XXXX",
  "tier": "pro",
  "issued_to": "alice@friend.com",
  "issued_at": "2026-10-09T12:00:00Z",
  "expires_at": "2027-01-07T12:00:00Z",
  "fp": "abcdef1234567890abcdef1234567890",
  "features": ["realtime", "historical_7d"],
  "sig": "a3f1b2c4d5e6..."   // HMAC-SHA256(payload without sig, secret)
}
```

校验流程（`license_auth.load_license`）：

1. 没设 `TRIDENT_LICENSE_SECRET` → `None`（fail-closed）
2. 文件不存在 → `None`（**仅** full 模式允许此状态）
3. 缺 `sig` 字段 → `None`
4. HMAC 重算不匹配 → `None`（**篡改即拒**）
5. 全部通过 → 返回 `License` dataclass；上层业务自己 `is_expired()` / `has_feature()`

**key 矩阵**（`features` 字段）：

| 值 | 含义 |
|---|---|
| `["*"]` | 全部功能 |
| `["realtime"]` | 仅实时信号 |
| `["realtime","historical_7d"]` | 实时 + 7 天历史 |
| `["historical_30d"]` | 30 天历史（不能与 realtime 同开，除非显式列出） |

---

## 5. API Key 流程

签发：

```bash
python3 scripts/gen_api_key.py create \
    --name "alice-laptop" \
    --tier pro \
    --note "朋友 alice" \
    --store /etc/trident/api_keys.json
# 打印：trk_xxxxxxxxxxxxxxxxxxxxxxxx
```

撤销：

```bash
python3 scripts/gen_api_key.py revoke --name alice-laptop --store /etc/trident/api_keys.json
```

请求侧：

```bash
curl -H "X-Trident-API-Key: trk_xxx" https://demo.trident.example/api/v4/signal
```

服务端校验：`verify_api_key(token)` 读 `/etc/trident/api_keys.json`，过滤 `revoked=true`。

---

## 6. 响应头（demo / licensed 模式才发）

| Header | 示例 | 用途 |
|---|---|---|
| `X-Trident-Mode` | `demo` / `licensed` | 调试时一眼看出模式 |
| `X-Trident-Watermark` | `tk_a1b2c3d4e5f6...` | 每次响应唯一；泄漏截图能定位 |
| `X-Trident-Demo-RPM` | `30` | demo 限流阈值（让前端显示） |
| `X-Trident-Demo-Days` | `1` | demo 历史窗口 |

`full` 模式：以上 header **不发送**（让 full 用户看不出差异；防止部署降级到 demo 时漏看）。

---

## 7. 关键 env 变量清单

见 [`SECURITY_DEMO_MODE_DEPLOY.md`](./SECURITY_DEMO_MODE_DEPLOY.md) 第 2 节和 `backend/.env.example`。
