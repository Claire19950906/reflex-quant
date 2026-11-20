# macOS 进程守护（LaunchAgent）

解决网页频繁 `ERR_CONNECTION_REFUSED` 的根因：**三个服务过去靠 `nohup` 手动拉起，没有守护，进程一退出就拒绝连接。**

本目录把 engine / api / 前端 交给 macOS 的 `launchd` 守护，进程**退出后会自动拉起**，不再需要一直开着终端窗口。

## 一键命令

```bash
# 启动（清理旧进程 → 生成 plist → 注册 launchd → 健康检查）
scripts/macos/start_all.sh

# 停止
scripts/macos/stop_all.sh

# 重启
scripts/macos/restart_all.sh

# 查看状态（launchd + 端口 + HTTP 探活 + 错误日志）
scripts/macos/status.sh
```

## 原理

| 服务 | 命令 | 端口 | Label |
|---|---|---|---|
| engine | `python3 engine.py` | webhook `9000`（`TREE_NEWS_PORT`） | `com.trident.engine` |
| api | `python3 api_server.py` | `8000` | `com.trident.api` |
| 前端 | `node next dev` | `3030` | `com.trident.frontend` |

- 三份 plist 运行时按**当前项目绝对路径**生成到 `~/Library/LaunchAgents/`。
- 均设 `KeepAlive=true` + `ThrottleInterval=10`：任何原因退出，launchd 会在 10 秒后自动拉起。
- 均设 `TRIDENT_NO_AUTO_SETUP=1`：自动重启**不会**重新执行种子导入/经验救援，你在 UI 里删掉的东西不会复活。
- 日志统一落 `scripts/macos/log/*.out.log` / `*.err.log`。

## 注意事项

1. **守护是「登录用户级」的**（LaunchAgent），你登录账号后生效；关机重开会自动随登录拉起。
2. 前端默认用**开发模式** `next dev`（与之前手动运行一致，支持热更新）。若要上生产模式，把
   `_lib.sh` 里 `write_plists()` 的 frontend `ProgramArguments` 改成：
   ```
   <string>start</string>   （替代 dev / --hostname / --port 那几行）
   ```
   并先执行一次 `cd frontend && npm run build`。
3. 改端口：用环境变量覆盖即可，例如
   `API_PORT=8010 scripts/macos/start_all.sh`（前端 `FRONTEND_PORT`、engine `TREE_NEWS_PORT` 同理）。
4. 停止后若想**彻底删除**生成的 plist：`rm ~/Library/LaunchAgents/com.trident.*.plist`。
5. 这是 macOS 专属；Windows 仍沿用原来的 `scripts/*.bat`。