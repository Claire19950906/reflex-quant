#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launchd 作业体检（v341）。

为什么需要：这个项目有 87 个 com.trident.* 作业，`launchctl list` 的退出码
只反映**最后一次**运行，日志里也可能只是"每天照常打印失败"。实测就抓到两个
长期 exit 1 的（tech-reflections 被 DB 锁死、bls-macro 打在 v2 上被 403），
都属于"看着在跑、其实一直在失败"。

判据：
  · 退出码非 0（-15 = 被 SIGTERM，长驻服务重启不算失败）
  · 日志 mtime 超过 2×StartInterval（周期作业该写日志却没写）
  · 日志里最近 200 行出现致命关键字（Traceback / FATAL / database is locked）

v483 暂停态豁免：DATA_PAUSED 存在时，paused_jobs.txt 里登记过的作业**本来就不该跑**，
"日志没更新"不是故障而是预期。此前不动这个判据，结果是每次暂停后体检都报几十个
"坏作业"，把真正的问题（比如 database is locked）淹掉。现在这类作业单独列 PAUSED，
不计入"有问题"总数，也不看日志新鲜度（但保留五类真实错误：锁冲突/Traceback）。

用法：python3 scripts/audit_jobs.py [--verbose]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import re
import subprocess
import sys

AGENTS = os.path.expanduser("~/Library/LaunchAgents")
FATAL_RE = re.compile(r"(Traceback \(most recent call last\)|\[FATAL\]|"
                      r"database is locked|OperationalError|SyntaxError)")


def _launchctl_status() -> dict[str, int]:
    out = {}
    try:
        txt = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("com.trident."):
            try:
                out[parts[2]] = int(parts[1])
            except ValueError:
                pass
    return out


def _interval(p: dict) -> int | None:
    if "StartInterval" in p:
        return int(p["StartInterval"])
    if "StartCalendarInterval" in p:
        return 86400
    return None                      # 长驻服务（KeepAlive）


FRESH_H = 24.0     # 只信 24 小时内还在写的日志：追加型日志里的旧 Traceback 不算现状

PAUSE_FLAG = os.path.expanduser("~/.trident/state/DATA_PAUSED")
PAUSED_LIST = os.path.expanduser("~/.trident/state/paused_jobs.txt")


def _paused_labels() -> set[str]:
    """暂停中且登记过的作业。DATA_PAUSED 不存在时返回空集（行为与 v482 一致）。"""
    if not os.path.exists(PAUSE_FLAG):
        return set()
    try:
        with open(PAUSED_LIST, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except Exception:
        return set()


_OFFSETS_PATH = os.path.expanduser("~/.trident/state/audit_jobs_offsets.json")


def _load_offsets() -> dict:
    try:
        with open(_OFFSETS_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_offsets(offsets: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_OFFSETS_PATH), exist_ok=True)
        with open(_OFFSETS_PATH, "w", encoding="utf-8") as f:
            json.dump(offsets, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _newest_mtime_age(path: str) -> float | None:
    """日志"新鲜度"要连轮转文件一起算。

    v485：logrotate 把 `.log` 改名成 `.log.1` / `.log.*.gz` 之后，`.log` 的 mtime
    还是旧的（甚至被日志轮转重置），单看 `.log` 会把"其实一直在正常写、只是刚被轮转"
    的作业误判成"几小时没更新"。这里取原文件 + 常见轮转后缀里最新的 mtime。
    """
    cands = [path, path + ".1", path + ".gz", path + ".1.gz", path + ".2.gz"]
    best = None
    for c in cands:
        try:
            if not os.path.exists(c):
                continue
            m = os.path.getmtime(c)
            if best is None or m > best:
                best = m
        except Exception:
            continue
    if best is None:
        return None
    return (dt.datetime.now().timestamp() - best) / 3600.0


def _tail_fatal(path: str, n: int = 20, since: int | None = None) -> str | None:
    """检查日志里的致命行。

    v484：日志是追加写，几小时前甚至几天前的 Traceback 会永久留在尾部，
    导致每次体检都报"日志有致命行"（实测 121 个作业报 65 个问题，多数是这种陈年残留）。
    改成只看**自上次体检之后新增**的字节区间（state 里存 offset）：
    首次没有基线时退回"最近 20 行"，之后只判新增内容。
    """
    try:
        a = (dt.datetime.now().timestamp() - os.path.getmtime(path)) / 3600.0
        if a > FRESH_H:
            return None
    except Exception:
        return None
    try:
        size = os.path.getsize(path)
        start = since if (since is not None and 0 <= since <= size) else None
        with open(path, "rb") as f:
            if start is None:
                f.seek(max(0, size - 4096))
                chunk = f.read()
                lines = chunk.decode("utf-8", "replace").splitlines()[-n:]
            else:
                f.seek(start)
                chunk = f.read()
                lines = chunk.decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    # 只看**最近 20 行**：日志是追加写的，几周前的旧 Traceback 不能代表现状
    for line in reversed(lines):
        if FATAL_RE.search(line):
            return line.strip()[:160]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    status = _launchctl_status()
    paused = _paused_labels()
    rows = []
    offsets = _load_offsets()
    offsets_targets: list[str] = []
    for path in sorted(glob.glob(os.path.join(AGENTS, "com.trident.*.plist"))):
        try:
            with open(path, "rb") as f:
                p = plistlib.load(f)
        except Exception:
            continue
        label = p.get("Label", os.path.basename(path))
        iv = _interval(p)
        rc = status.get(label)
        out_path = p.get("StandardOutPath")
        err_path = p.get("StandardErrorPath")
        age_h = None
        for lp in (err_path, out_path):
            a = _newest_mtime_age(lp) if lp else None
            if a is not None:
                age_h = a if age_h is None else min(age_h, a)
        fatal = (_tail_fatal(err_path, since=offsets.get(err_path))
                 if err_path and os.path.exists(err_path) else None)
        if not fatal and out_path and os.path.exists(out_path):
            fatal = _tail_fatal(out_path, since=offsets.get(out_path))

        issues = []
        if rc is not None and rc not in (0, -15):
            issues.append(f"退出码 {rc}")
        # 暂停中的作业不判"日志不新鲜"—— 它本来就不该起跑
        if iv and age_h is not None and age_h > 2 * iv / 3600.0 \
                and label not in paused:
            issues.append(f"日志 {age_h:.1f}h 未更新（周期 {iv / 3600:.0f}h）")
        if fatal:
            issues.append(f"日志有致命行: {fatal}")
        offsets_targets.extend([x for x in (err_path, out_path) if x])
        rows.append((label, iv, rc, age_h, issues))

    # 记下本次读到的位置，下一轮只判新增内容
    for lp in offsets_targets:
        try:
            if lp and os.path.exists(lp):
                offsets[lp] = os.path.getsize(lp)
        except Exception:
            pass
    _save_offsets(offsets)

    bad = [r for r in rows if r[4]]
    paused_ids = [r[0] for r in rows if r[0] in paused]
    print(f"=== com.trident.* 作业 {len(rows)} 个，有问题 {len(bad)} 个"
          + (f"，暂停中 {len(paused_ids)} 个（不判日志新鲜度）" if paused_ids else "") + " ===")
    for label, iv, rc, age_h, issues in bad:
        print(f"  [{label}] rc={rc} 日志 {age_h:.1f}h 前" if age_h is not None
              else f"  [{label}] rc={rc}")
        for i in issues:
            print(f"      · {i}")
    if args.verbose:
        print("\n--- 全部作业 ---")
        for label, iv, rc, age_h, issues in rows:
            flag = "OK  " if not issues else "BAD "
            print(f"  [{flag}] {label:42s} rc={rc} 间隔={iv} 日志={age_h and round(age_h, 1)}h")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
