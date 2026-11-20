# macOS 常驻服务（launchd）

这台机器上跑的是「本机生产」形态：三个常驻进程由 launchd 拉起，wrapper 脚本在
`~/.trident/wrappers/`，日志在 `~/.trident/log/`。

| 标签 | 端口 | 入口 | 日志 |
|---|---|---|---|
| `com.trident.api` | 8000 | `wrappers/run_api.sh` | `log/api.out.log` / `api.err.log` |
| `com.trident.bridge` | 8100 | `wrappers/run_bridge.sh` | `log/bridge.out.log` / `bridge.err.log` |
| `com.trident.engine` | 9000(webhook) | `wrappers/run_engine.sh` | `log/engine.*.log` |

本目录里的三个 `.plist` 是从 `~/Library/LaunchAgents/` **镜像过来的版本快照**，
目的是让部署描述进版本库（此前 api / bridge 两份只在 LaunchAgents 里存在，仓库没有）。
改动要两边同步：先改本目录，再 `cp` 回 `~/Library/LaunchAgents/` 并 `launchctl kickstart`。

## 重启

```bash
# 单个服务（-k 会先杀掉旧的，别手工 nohup，会和 launchd 抢端口）
launchctl kickstart -k gui/$(id -u)/com.trident.api
launchctl kickstart -k gui/$(id -u)/com.trident.bridge

# 两个一起重启的脚本（内部串行 + 等初始化，避免同时 lifespan 初始化 DB 互锁）
~/.trident/wrappers/restart_backend.sh
```

bridge 冷启动约 90 秒（要先 warm 行情快照），`/api/health` 探活请留够时间。

## wrapper 为什么不进仓库

`~/.trident/wrappers/*.sh` 会读 `~/.trident/env` 里的代理与凭据并 export，
直接入库等于把密钥写进 git 历史。要备份就备份到密码管理里，不要放进仓库。
