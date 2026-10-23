# Trident Weekly Reflection Cron (macOS launchd)

`weekly_reflection.py` 是 Trident 系统的"周报"——每周一次汇总交易数据、生成 AI prompt。
**P2 #11** 让 launchd 在**周一早上 9:00**自动跑，不必人工记得。

## 文件

```
cron/
├── run_weekly_reflection.sh                    # 包装脚本（日志、错误处理）
├── com.trident.weekly_reflection.plist         # launchd 配置
├── install_cron.sh                              # 安装/卸载/状态/测试
└── README.md                                    # 本文件
```

## 安装

```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170/backend
bash cron/install_cron.sh install
```

完成后：
- launchd 在每个**周一 09:00**自动跑 `weekly_reflection.py --days 7`
- 报告输出到 `reports/weekly/weekly_report_<date>.md`
- AI prompt 输出到 `reports/weekly/weekly_ai_prompt_<date>.md`
- 日志写到 `logs/launchd.{out,err}.log` + `logs/weekly_reflection_<date>.log`

## 验证

```bash
# 看状态
bash cron/install_cron.sh status

# 立刻跑一次（不等定时）
bash cron/install_cron.sh run

# 直接测试脚本（不走 launchd）
bash cron/install_cron.sh test
```

## 卸载

```bash
bash cron/install_cron.sh uninstall
```

## 故障排查

| 现象 | 排查 |
|---|---|
| 周一 9 点没跑 | `launchctl list \| grep com.trident.weekly_reflection` 看是否在 list 里 |
| 跑但失败 | `tail -50 logs/launchd.err.log` |
| 跑但没产物 | `tail -20 logs/weekly_reflection_$(date +%Y-%m-%d).log` |
| 想换时间 | 改 plist 里的 `Hour` / `Minute` / `Weekday`，然后 `bash install_cron.sh install`（自动 unload+load）|
| Python 路径变了 | 改 `run_weekly_reflection.sh` 里的 `PYTHON=` |
| 想跑其他命令 | 复制 plist + shell 改 Label/ProgramArguments，然后 install |

## 触发时间速查

macOS launchd `Weekday` 字段（man launchd.plist 原文："0 and 7 are Sunday"）：
- 0 = 周日
- 7 = 周日（别名）
- **1 = 周一** ← 本配置
- 2 = 周二
- 3 = 周三
- 4 = 周四
- 5 = 周五
- 6 = 周六

> **注意**：crontab 用 0-6（0=周日）但 launchd 多了一个 7=周日，**保持一致用 0/7=周日**。
> launchd 的 1=周一 与 crontab 一致。

也可以用 `StartCalendarInterval` 数组定义多个触发点（每天、每周几、每月几号都支持）。
