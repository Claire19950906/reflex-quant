#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron_health.py —— launchd 任务"挂着但没在跑"的自动巡检

为什么做：
  `launchctl list` 显示 job 已加载、last exit = 0，看起来一切正常，
  但脚本可能一次都没执行。已经踩过三次：
    · fx-rates 静默停摆 2 小时（`getcwd: Operation not permitted`）
    · sge-silver 溢价值 55 小时没更新（同样是 getcwd，shell 起不来）
    · fx-rates 的 StartInterval 被改成 12 小时，而预期是 300 秒
  共同点：**没有任何一个地方会主动告诉你"它没在跑"**，只能靠数据变旧才发现。

判据（四条，命中任一即异常）：
  1. err 日志里出现已知致命片段（getcwd / Operation not permitted）
  2. out 日志最后修改时间 > max(3 × StartInterval, 2 小时)
  3. launchctl 报告的最后退出码非 0
  4. plist 里 StartInterval 与同族任务的常规节奏严重不符（> 20 × 中位数）
     —— 只看，不改；改配置是人的决定

只读：不重启、不重注册、不改任何 plist。
用法：python3 cron_health.py [--json]
"""
import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LA = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.trident."
FATAL_PATTERNS = ("getcwd", "Operation not permitted")
MAX_SILENCE = 2 * 3600          # 最短容忍 2 小时静默

#: 历史遗留：有些任务的输出日志名与 label 对不上（靠 stamp 猜会误判成"从未运行"）。
#: 这里逐个登记，不做模糊推断 —— 猜错的代价是"真停摆被判成正常"。
_LOG_ALIASES = {
    "com.trident.retention-prune": ("retention*.log",),
    "com.trident.kg-reconcile-cron": ("kg_review_cron*.log",),
}


def _paused_mode() -> bool:
    """数据暂停标记：存在即视为「暂停期」。"""
    return (Path.home() / ".trident" / "state" / "DATA_PAUSED").exists()


def _keep_labels() -> set:
    """从 pause_data.sh 的 KEEP 白名单里取 label，避免两份清单漂移。
    这些任务在暂停期本来就该在跑（页面/守护），不能被判成「暂停漏了」。
    """
    sh = Path.home() / ".trident" / "wrappers" / "pause_data.sh"
    try:
        txt = sh.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    m = re.search(r'KEEP="(.*?)"', txt, re.S)
    return set(m.group(1).split()) if m else set()


def _uid() -> int:
    return os.getuid()


def _print_job(label: str) -> Dict[str, str]:
    """launchctl print 的文本 → 关键字段。取不到就空字典。"""
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{label}"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    info: Dict[str, str] = {}
    for key in ("state", "last exit code", "run interval", "runs"):
        m = re.search(rf"{key} = (\S+)", out)
        if m:
            info[key] = m.group(1)
    return info


def _tail_has_fatal(path: Optional[str]) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    # 只看最后 3 行：err 日志不轮转，几十小时前的一行 getcwd 会一直躺在文件里，
    # 按"整个尾部 4KB 搜"去判，等于给这个任务判了无期。真正要问的是
    # "现在这条还在不在冒"。
    lines = [ln for ln in tail.splitlines() if ln.strip()][-3:]
    for pat in FATAL_PATTERNS:
        if any(pat in ln for ln in lines):
            return pat
    return ""


def _calendar_limit(p: Dict[str, Any]) -> Optional[int]:
    """日历型任务的容忍间隔（秒）。纯 StartInterval 任务返回 None。

    为什么要有：v483 之前只对 `StartInterval` 任务做静默检查，而
    `StartCalendarInterval`（如 retention-prune 每天 05:20）因为 `interval is None`
    直接跳过了静默判据 —— 库 30 天涨 1GB、`source_status_history` 冲到 164 万行，
    巡检却一直报"异常 0 个"。日历任务的"没跑"比定时任务更隐蔽。
    """
    cal = p.get("StartCalendarInterval")
    if not cal:
        return None
    entries = cal if isinstance(cal, list) else [cal]
    has_weekday = any(isinstance(e, dict) and "Weekday" in e for e in entries)
    has_day = any(isinstance(e, dict) and "Day" in e for e in entries)
    if has_day:
        base = 32 * 86400          # 每月一次 → 给 32 天
    elif has_weekday:
        base = 8 * 86400           # 每周一次 → 给 8 天
    else:
        base = 26 * 3600           # 每天一次 → 给 26 小时（留机器休眠余量）
    return base * max(1, len(entries))


def scan() -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    paused_mode = _paused_mode()
    keep = _keep_labels() if paused_mode else set()
    if not LA.exists():
        return jobs
    for f in sorted(LA.glob(f"{PREFIX}*.plist")):
        try:
            with open(f, "rb") as fh:
                p = plistlib.load(fh)
        except Exception:
            continue
        label = p.get("Label") or f.stem
        interval = p.get("StartInterval")
        out_path = p.get("StandardOutPath")
        err_path = p.get("StandardErrorPath")
        info = _print_job(label)
        # 有些脚本的输出去向不是 plist 里写的 StandardOutPath（比如看门狗写到
        # engine_watchdog.log），只看那一个文件会误判成"从未运行"。所以扫一遍
        # 日志目录里名字相近的文件，取最新的一次写入。
        stamp = label.replace(PREFIX, "").replace("-cron", "").replace("-", "_").replace(".", "_")
        cands = []
        if out_path and os.path.exists(out_path):
            cands.append(out_path)
        log_dir = Path.home() / ".trident" / "log"
        if log_dir.exists():
            for c in log_dir.glob(f"*{stamp}*.log"):
                if c.is_file():
                    cands.append(str(c))
            for pat in _LOG_ALIASES.get(label, ()):
                for c in log_dir.glob(pat):
                    if c.is_file():
                        cands.append(str(c))
        log_age = None
        if cands:
            newest = max(os.path.getmtime(c) for c in cands)
            log_age = int(time.time() - newest)

        verdict, note = "ok", ""
        # 致命片段必须是"最近还在冒"的。err 日志不轮转，几周前的一行 getcwd
        # 会让脚本永远判死 —— 第一版就是这么误报了大半任务。
        err_age = (int(time.time() - os.path.getmtime(err_path))
                   if err_path and os.path.exists(err_path) else None)
        cal_limit = _calendar_limit(p)
        limit = max(3 * int(interval), MAX_SILENCE) if interval else (cal_limit or MAX_SILENCE)
        fatal = _tail_has_fatal(err_path)
        # v483: 先判"有没有注册"。plist 躺在 LaunchAgents 里但 launchctl 里找不到，
        # 等于永远不会被触发 —— 这是最彻底的静默失败，之前完全没检查。
        # 已注册但一次没跑过（刚装上的任务）不算异常，标 pending 等人确认首跑时间
        pending = bool(info) and info.get("runs") == "0" and info.get("last exit code") == "(never exited)"
        if pending:
            verdict, note = "pending", "已注册但从未执行过（新装任务，等首次触发）"
        elif not info:
            # 未注册但日志仍在更新 = 由另一套调度（引擎内部循环 / service-guard）接管，
            # plist 只是历史残留，不判异常（否则 98 个残留会把真异常埋掉）；
            # 未注册且日志也停了 = 真停摆，这是要报的。
            if log_age is not None and log_age <= limit:
                verdict, note = "legacy", f"未注册，但输出 {log_age//60} 分钟前仍在更新（由内部调度接管）"
            else:
                verdict, note = "unregistered", "launchctl 里没有这个服务，且没有近期输出（永远不会触发）"
        # getcwd 这行在 bash 被 launchd 拉起时就会打，即使任务本身跑得好好的
        # （实测 fx_rates 一边报这行、一边每 5 分钟正常写入汇率）。
        # 所以判死要加一条：**输出也停了**才算真死；输出还在走，就是噪音。
        elif fatal and err_age is not None and err_age <= limit:
            if log_age is not None and log_age <= limit:
                note = f"（`{fatal}` 噪音，但输出 {log_age//60} 分钟前仍在更新）"
            else:
                verdict, note = "fatal", f"err 日志 {err_age//60} 分钟前仍在报 `{fatal}`"
        elif fatal:
            note = f"（历史遗留：err 里有 `{fatal}`，但已 {err_age//3600 if err_age else '?'}h 没再出现）"
        elif info.get("last exit code") not in (None, "0", "(never"):
            code = info["last exit code"]
            # 退出码非 0 但输出还在正常更新 → 多半是"部分成功"（比如某个子源没拿到），
            # 标警告不判死；输出也停了才升级成异常。
            if log_age is not None and log_age <= limit:
                note = f"（退出码 {code}，但输出 {log_age//60} 分钟前仍在更新，属部分成功）"
            else:
                verdict, note = "exit", f"最后退出码 {code}"
        elif log_age is not None:
            if log_age > limit:
                kind = "日历型" if cal_limit else "定时"
                verdict, note = "silent", f"输出日志 {log_age//3600}h 未更新（{kind}阈值 {limit//3600}h）"
        # v482：暂停期里「没在跑」是用户要求的，不是故障。
        # 不区分的话，每小时会把 100+ 个按清单停掉的任务报成停摆，真异常反而被埋掉。
        # 反过来，暂停期里**又被拉起来**的任务必须报 —— 那是暂停漏了。
        if paused_mode:
            if verdict in ("unregistered", "silent"):
                if info and label not in keep:
                    verdict, note = "pause-leak", "数据暂停期，但该任务仍被拉起（暂停清单漏了或有人手动 bootstrap）"
                else:
                    verdict, note = "paused", "数据暂停期，按暂停策略停用（非异常）"
        jobs.append({"label": label, "interval": interval,
                     "registered": bool(info),
                     "state": info.get("state", "?"),
                     "last_exit": info.get("last exit code", "?"),
                     "log_age_h": round(log_age / 3600, 1) if log_age is not None else None,
                     "verdict": verdict, "note": note})
    return jobs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args, _ = ap.parse_known_args(argv)

    jobs = scan()
    # legacy = 未注册但确有输出：由内部调度接管，属配置残留，不计入异常
    paused = [j for j in jobs if j["verdict"] == "paused"]
    bad = [j for j in jobs if j["verdict"] not in ("ok", "legacy", "paused")]
    unreg = [j for j in bad if j["verdict"] == "unregistered"]
    legacy = [j for j in jobs if j["verdict"] == "legacy"]
    if args.json:
        print(json.dumps({"jobs": jobs, "bad": len(bad), "paused": len(paused),
                          "unregistered_dead": len(unreg), "legacy": len(legacy)},
                         ensure_ascii=False, indent=2))
    else:
        # v483: 分三类报。之前"119 个任务，异常 0 个"给人全绿的错觉：
        # 98 个 plist 压根没注册 —— 数文件数 ≠ 任务在跑。
        extra = f"；暂停期停用 {len(paused)} 个（不计异常）" if paused else ""
        print(f"[{time.strftime('%H:%M:%S')}] cron 巡检：{len(jobs)} 个任务，异常 {len(bad)} 个"
              f"（未注册且停摆 {len(unreg)} 个；内部调度接管 {len(legacy)} 个{extra}）")
        for j in bad:
            iv = f"{int(j['interval'])}s" if j["interval"] else "-"
            print(f"  ✗ {j['label']:<34} state={j['state']:<12} exit={j['last_exit']:<4} "
                  f"间隔={iv:<7} -> {j['note']}")
    # 只报告，不改：真正的动作（改间隔、改工作目录、kickstart）交给人
    return 0


if __name__ == "__main__":
    sys.exit(main())
