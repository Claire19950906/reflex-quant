# 策略系统 Windows 服务器部署手册

> 适用：Windows Server 2022（或 Win10+），Python 3.10-3.12，Node.js 18+
> 部署包：`strategy_package.zip`（含全部源码 + 已配置好的 `backend/.env`）

---

## 一、前置环境安装（一次性）

### 1. Python 3.12

1. 下载 https://www.python.org/downloads/ → 选 **Windows installer (64-bit)**
2. 安装时 **勾选 "Add python.exe to PATH"**
3. 验证：
```bat
python --version   :: 应显示 Python 3.12.x
```

### 2. Node.js 20 LTS（前端需要）

1. 下载 https://nodejs.org/zh-cn → **LTS 版（20.x 或 22.x）**
2. 一路默认安装，装完**重开终端**
3. 验证：
```bat
node --version
npm --version
```

---

## 二、解压部署包

把 `strategy_package.zip` 拷到服务器，解压到目标目录（如 `C:\trident`）：

```bat
cd C:\
tar -xf strategy_package.zip
```

> 解压后目录结构应为：
> ```
> C:\trident\backend\src_python\...
> C:\trident\frontend\...
> C:\trident\dashboard\...
> ```

---

## 三、安装后端依赖

**注意：`requirements.txt` 含中文注释，Windows 下直接 pip 会 GBK 编码报错，必须加 `-X utf8`：**

```bat
cd C:\trident\backend
python -X utf8 -m pip install --upgrade pip
python -X utf8 -m pip install -r requirements.txt
python -X utf8 -m pip install python-multipart
```

> 国内网络慢可加镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`

验证后端可加载：

```bat
cd C:\trident\backend\src_python
python -X utf8 -c "import config, db, engine.main; print('OK')"
```

---

## 四、检查配置（`.env`）

部署包已带配置好的 `backend/.env`（含 FRED / MiniMax / OpenRouter / AIPING Key），但**按服务器网络环境调整**：

```bat
notepad C:\trident\backend\.env
```

| 项目 | 国内服务器 | 境外服务器 |
|---|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` | 保留（指向本地代理，如 127.0.0.1:22307） | **删除/注释这两行**（境外直连，走代理反而失败） |
| `OPENROUTER_API_KEY` | 保留（走代理） | 保留（直连） |
| `MINIMAX_API_KEY` | 保留（国内版直连可设 `MINIMAX_USE_PROXY=false`） | 保留（必要时走代理） |
| `FRED_API_KEY` | 保留 | 保留 |

> **关键机制**：OpenRouter 余额不足(402)时，AI 分析自动切换到 MiniMax 备用通道（无需干预，余额恢复自动切回）。

---

## 五、前端构建（可选，需要网页界面时）

```bat
cd C:\trident\frontend
npm install
npm run build
```

> `npm install` 慢可加：`npm install --registry=https://registry.npmmirror.com`

---

## 六、启动三个服务（三个终端）

### 终端 1 — 新闻引擎（采集 + AI 分析 + 模拟盘结算）

```bat
cd C:\trident\backend\src_python
python -u -X utf8 engine.py
```

### 终端 2 — 后端 API（8000 端口）

```bat
cd C:\trident\backend\src_python
python -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

> `--host 0.0.0.0` 允许局域网/其它机器访问；只用本机可改 `127.0.0.1`。

### 终端 3 — 前端（3000 端口，生产模式）

```bat
cd C:\trident\frontend
npm start
```

---

## 七、验证清单

1. **引擎日志**应看到：
   ```
   [MAIN] 代理已配置: ...  或  [MAIN] 无代理, 直连模式
   [ENGINE] DB VERIFIED
   [INGEST] Connected
   [TRACKER] Forward tracker started
   [AI] Model pool: ...     ← 有新闻进来时打印实际通道（OpenRouter 或 MiniMax 备用）
   ```

2. **API 健康检查**：
   ```bat
   curl http://127.0.0.1:8000/api/health
   ```

3. **前端**：浏览器打开 `http://127.0.0.1:3000`（局域网机器访问用 `http://<服务器IP>:3000`）

4. **AI 分析正常**：有新闻后，复盘页/实时信号台出现分析结果；`ai_decisions` 表持续增加。

---

## 八、常见问题

| 问题 | 解决 |
|---|---|
| `pip` 报 `UnicodeDecodeError: 'gbk'` | 用 `python -X utf8 -m pip install ...` |
| 引擎日志 `WinError 10061 连接被拒` | `.env` 的 `HTTP_PROXY` 指向的代理没开（国内），或境外服务器没删代理行 |
| AI 分析大量 `FAILED` | 查引擎日志：OpenRouter 402 → 自动切 MiniMax；若 MiniMax 也失败检查 Key/base_url |
| 新闻一直 `PENDING` 不分析 | 引擎没启动，或新闻被过滤（is_noise=1 不进 AI） |
| `npm` 不是内部命令 | Node.js 没装或终端没重开 |

### 重置积压的 FAILED 新闻（重新分析）

```bat
cd C:\trident\backend
python -X utf8 -c "import sqlite3; c=sqlite3.connect('trident_event_bus.db'); c.execute(\"UPDATE raw_news SET status='PENDING' WHERE status IN ('FAILED','PROCESSING')\"); c.commit(); print(c.total_changes)"
```

---

## 九、日常运维

- **重启**：三个终端各自 Ctrl+C 再重新启动
- **数据库**：`backend/trident_event_bus.db`（停机时备份：拷走 .db + .db-wal + .db-shm）
- **日志**：引擎/API 终端输出即日志；如需落盘可重定向：`python -u -X utf8 engine.py > runtime/engine.log 2>&1`
- **模拟盘重置**：复盘页"重置模拟盘"按钮（自动备份到 `backend/backups/`）
