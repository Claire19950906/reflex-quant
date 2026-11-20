# Trident Agent MVP — Windows 部署指南

适用于 Windows Server / Windows 10-11。核心：**你只需要在三个终端里各粘贴一条命令**，
其余（建库、补配置、装依赖、清缓存、构建）全部自动；随时 Ctrl+C 终止。

---

## 0. 日常：三条命令（就这样用）

```bat
:: 终端 1 - 引擎
cd backend\src_python && python -u -X utf8 engine.py

:: 终端 2 - API（前端依赖它）
cd backend\src_python && python -X utf8 -m uvicorn api_server:app --host 127.0.0.1 --port 8000

:: 终端 3 - 前端（开发模式，改代码自动热更新）
cd frontend && npm run dev
```

> 这三条命令**不需要任何前置步骤**。引擎和 API 启动时会自己：建库/补列 →
> 首次自动导入提示库配置 → 继续启动。你直接 cd 过去跑就行。
> Ctrl+C 即可随时终止，重复启动也不会污染数据。

### 前端改用生产模式（省一半内存）

```bat
:: 终端 3 - 前端（生产模式）
cd C:\Users\Administrator\Desktop\strategy_package_v6\frontend
set NODE_OPTIONS=--max-old-space-size=3072
npm run build
npm start
```

生产模式只需在**改过前端代码后**重新 `npm run build`；日常重启直接 `npm start` 即可。
端口都是 **3030**（`next start --port 3030`）。

### 新电脑第一次跑

```bat
:: 1) 装 Python 依赖
cd backend
pip install -r requirements.txt

:: 2) 配密钥（只放 .env，不进代码）
copy ..\.env.example .env
notepad .env

:: 3) 前端依赖（只需一次，之后 npm run build 会用它）
cd ..\frontend
npm install
```

之后就是第 0 节那三条命令。

---

## 1. 可选：一键脚本

如果你不想记 `cd` 路径，等价的三条命令也封装成了脚本：

```bat
scripts\run_engine.bat      :: = 终端 1（额外做了端口预检与初始化）
scripts\run_api.bat         :: = 终端 2
scripts\run_frontend.bat    :: = 终端 3（清 .next 缓存 + build + start）
scripts\run_frontend.bat dev :: 只起开发服务器（等价 npm run dev）
```

两者行为一致，用你习惯的那种即可。

**端口预检**：脚本版会在启动前检查端口，若已有实例在跑会直接提示并退出，
不会抛出 Python 堆栈：

```
[ERROR] Port 9000 is already in use - the engine looks like it is
        ALREADY RUNNING in another window. Close that one first, or ...
```

（手敲命令如果重复启动，会看到 `OSError: [Errno 10048] ...` —— 那就是"已经有一个在跑了"。）

---

## 2. 启动时自动发生的事（不用你管）

| 自动动作 | 说明 | 会不会反复做 |
| --- | --- | --- |
| 建库 / 补列 | `db.migrate()` 幂等，只建表补列，不删数据 | 每次启动，约 300ms |
| 提示库配置入库 | 包里带 `backend\seed_config.db`（品种规则/经验/因果链/过滤词） | **只做一次**（见下） |
| 经验误杀回滚 | 恢复"已停用且无命中样本"的历史误杀 | **只做一次** |

### 2.1 一次性标记（保证"删了就是删了"）

两项"首次部署"动作各自用一个标记文件记录，与库同目录：

```
backend\trident_event_bus.db.seed_imported     ← 配置种子已导入
backend\trident_event_bus.db.insight_rescued   ← 经验自愈已做过
```

- 有标记 → 启动时**一个字节都不写**。你在界面上删掉的规则/经验**不会在重启后复活**
- 库被删掉重建 → 标记也没了 → 会重新导入一次（符合直觉）
- 执行失败**不写标记** → 下次启动会再试
- 需要重新灌一遍种子：`python -X utf8 deploy\restore_seed.py --force`

### 2.2 人工停用的经验不会被自动复活

经验面板里的「停用」会写入 `manual_deactivated=1`。自动启停逻辑（复盘评估 +
启动自愈）会**跳过人工停用的条目**：既不会救回，也不会因胜率回升而自动启用。

自动逻辑只作用于两类：`active=1` 且命中胜率 `<40%`（停用）、
非人工停用且胜率回升 `≥55%`（恢复）。

---

## 3. 可选：开机自启

Windows 没有 systemd，三种做法任选。

### 方案 A：任务计划程序（纯系统自带）

1. 「任务计划程序」→ 创建任务 → 勾选「不管用户是否登录都要运行」「使用最高权限运行」
2. 触发器：启动时
3. 操作 → 三个（或写一个 bat 串起来）：

| 程序或脚本 | 添加参数 | 起始于 |
| --- | --- | --- |
| `C:\...\scripts\run_engine.bat` | — | `C:\...\项目根` |
| `C:\...\scripts\run_api.bat` | — | `C:\...\项目根` |
| `C:\...\scripts\run_frontend.bat` | `nobuild` | `C:\...\项目根` |

（前端用 `nobuild`：开机不必每次重新构建。）

### 方案 B：NSSM 注册成 Windows 服务

```bat
nssm install TridentEngine "C:\...\venv\Scripts\python.exe" "C:\...\backend\src_python\engine.py"
nssm set TridentEngine AppDirectory "C:\...\backend\src_python"
nssm set TridentEngine AppStdout  "C:\...\backend\runtime\engine.out.log"
nssm set TridentEngine AppStderr  "C:\...\backend\runtime\engine.err.log"
nssm start TridentEngine
```

API 同理，参数换成 `-m uvicorn api_server:app --host 127.0.0.1 --port 8000`。

### 方案 C：启动文件夹

`Win+R` → `shell:startup` → 放入三个脚本的快捷方式。

---

## 4. 验证清单

```bat
:: 1) 三个端口在监听
netstat -ano | findstr ":9000 :8000 :3030"

:: 2) API 健康检查
curl http://127.0.0.1:8000/api/health

:: 3) 配置是否入库（应返回非零数字，如 6 / 110 / 28 / 362）
cd backend
python -X utf8 -c "import sqlite3,config;c=sqlite3.connect(config.DB_PATH);print({t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('ai_asset_rules','review_insights','causal_chains','filter_keywords')})"

:: 4) 前端
start http://localhost:3030
```

引擎日志里应能看到：

```
[ENGINE] DB VERIFIED: ...\backend\trident_event_bus.db
[SEED] 已从 seed_config.db 导入配置（一次性）：ai_asset_rules 6 条、...   ← 仅首次
[INSIGHT] 启动自愈：恢复 N 条「停用但无命中样本」的经验                  ← 仅首次
[MAIN] Webhook server listening on 127.0.0.1:9000
[INGEST] Connected
```

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `OSError: [Errno 10048]` | 端口已被占用 —— 已经有一个实例在跑，先关掉那个终端；或用脚本版（会友好提示） |
| `[ERROR] Python not found` | 装 Python 3.11+ 并加入 PATH，或在项目根建 `venv` |
| 提示库是空的 | `python -X utf8 deploy\restore_seed.py --force` |
| 删掉的规则/经验又回来了 | 检查两个标记文件是否被删；重跑一次启动即会重建标记 |
| 手动停用的经验自己启用了 | 不应发生（`manual_deactivated` 保护）。若发生请反馈，并检查经验表该行的 `manual_deactivated` |
| 页面内容不更新 | 重新 `npm run build` 再 `npm start`（开发模式用 `npm run dev`） |
| 改了代码没生效 | 重启对应服务。引擎启动会打印 `[BUILD]` 指纹，可与文件修改时间对照 |
| 想看 AI 为什么没下单 | `set TRIDENT_SIGNAL_TRACE=1` 后再启动引擎，然后 `python -X utf8 backend\scripts\analyze_signal_trace.py` |

---

## 6. 环境变量速查

| 变量 | 作用 |
| --- | --- |
| `TREE_NEWS_PORT` | 引擎 Webhook 端口，默认 9000 |
| `API_PORT` / `API_HOST` | API 端口/绑定，默认 8000 / 127.0.0.1 |
| `NODE_OPTIONS=--max-old-space-size=3072` | 前端构建内存上限（大项目建议设） |
| `TRIDENT_SIGNAL_TRACE=1` | 打开闸门/下单留痕（排查用） |
| `RESTORE_SEED=1` | 启动时强制重灌配置种子 |
| `SKIP_SEED_RESTORE=1` | 本次调用不导入种子（不影响标记） |

---

## 7. 工具脚本

| 文件 | 作用 |
| --- | --- |
| `scripts\run_engine.bat` / `run_api.bat` / `run_frontend.bat` | 三条命令的封装版（含端口预检） |
| `backend\scripts\check_version_freshness.py` | 核对"哪些进程跑的是旧代码" |
| `backend\scripts\analyze_signal_trace.py` | 留痕 vs 数据库逐行对照（排查闸门/下单） |
| `backend\scripts\verify_deploy_content.py` | 核对提示库三处内容 |
| `deploy\restore_seed.py` | 手动导入配置种子（`--dry-run` / `--force`） |

> 备注：`.bat` 文件**必须保持纯 ASCII**。cmd.exe 按系统 ANSI 代码页（简体中文为
> 936/GBK）解析 `.bat`，写入 UTF-8 中文会被拆成乱码并破坏命令；脚本首行的
> `chcp 65001` 只负责让后面 Python 的中文输出正常显示。手敲命令不受此影响。
