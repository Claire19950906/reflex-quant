# GitHub 发布清单

先读这份，再决定推哪个分支。

---

## 一、这一份是什么

`GitHub 版/` 是从 `金融1.0/` 复制出来的**可公开版本**，和本机运行版互不影响。

**已剔除**（这些不会出现在 GitHub 版里）：

| 剔除项 | 原因 |
|---|---|
| `backend/.env` 及 4 个 `.env.bak-*` | 真实密钥：大模型 Key、交易所 Key、推送 token、代理 |
| `.git/` | 历史提交里可能残留旧版本密钥，重新 `git init` 更干净 |
| `backend/runtime`、`backend/backups`、`backend/backup` | 运行时数据、事故备份（含真实成交与账户） |
| `backend/reports`、`backend/news_exports`、`backend/uploads` | 真实报表与新闻导出 |
| 全部 `*.db` / `*.db-wal` / `*.db-shm` | 生产库（决策、新闻、账户全在里面） |
| `frontend/node_modules`、`.next*`、缓存目录 | 构建产物 |
| `audit_out`、`*.log`、`__pycache__` | 生成物 |

**保留**：源码、脚本、文档、测试、`docs/`、`.shots/`（截图）、`.env.example`（值已为空）。

**体积**：60 MB / 2358 个文件（原 8.7 GB）。

---

## 二、密钥体检结果

已扫描 `*.py / *.ts / *.tsx / *.json / *.yml`：

- 无 `sk-`、`ghp_`、`AKIA`、`PRIVATE KEY` 等硬编码密钥；
- 无 `.pem / .key / .p12 / id_rsa`；
- `.env.example` 与 `frontend/.env.local` 的密钥字段全部为空值；
- `frontend/.env.local` 只有一行 `NEXT_PUBLIC_API_URL=`（空）。

**仍需你自己确认两件事**：`docs/` 里有没有粘贴过真实截图或账单；`.shots/` 里的截图有没有露出账户号。

---

## 三、仓库 About 与 Topics

About（一行，粘到仓库设置里）：

> 提高决策正确率、提高赚钱几率的交易复盘系统：拦错单、记错因、每天 7 点一条早报。

Topics：

`trading-journal` `portfolio-tracker` `quant-trading` `market-news` `risk-management` `self-hosted` `fastapi` `nextjs` `sqlite` `python`

---

## 四、推送

**当前状态（2026-11-20 已执行）**

- 已推到 `stardust888888/reflex-quant` 的默认分支 **`main`**（干净历史）。
- 仓库首页仍显示旧的 `main`。要让访问者看到新 README，二选一：
  1. 仓库 **Settings → Branches → Default branch** 切成 `main`（可逆，推荐）；
  2. 确认后把 `main` 覆盖成这个快照（`git push -f origin main:main`）——**不可逆，会丢远端 426 个提交的历史**。

```bash
cd "~/Desktop/GitHub 版"
git push -u origin main:main   # 已执行（原 github-clean 已合并为 main）
```

推之前再跑一次：`grep -rE "sk-[A-Za-z0-9_-]{16,}|ghp_|AKIA" --include="*.py" --include="*.ts" . | head`

