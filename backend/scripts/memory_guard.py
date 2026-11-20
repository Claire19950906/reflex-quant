#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v322: 内存守卫 —— 治「整机换页导致的随机卡顿」。

事故背景（2026-11-04，用户反馈「突然抽风」）：
    8GB 物理内存 + swap 已用 5.9GB + load 91 的状态下，
    后端进程全部在 swap 里跑 —— 任何一次磁盘 IO 都变成秒级延迟，
    表现出来就是「有时候正常，有时候整个卡住」，与代码逻辑无关。

    实测占用大户（RSS）：
        VS Code Code Helper (Renderer)  786MB + 417MB + 273MB + 120MB ≈ 1.6GB
        豆包 DoubaoWork                  488MB
        Chrome                           ~500MB
        后端自身（engine/api/bridge）    ~400MB
    VS Code 占最多 —— 但**不能替用户关掉他自己的编辑器**。

本守卫做三件「安全且有效」的事：
    1. 卸载 Ollama 已加载模型 —— 唯一能**自动**回收的一大块（~2GB），
       且本地模型当前不参与主流程（GATE_LOCAL_FIRST=0），卸载无副作用。
    2. 清理陈旧日志 / __pycache__ —— 释放磁盘，也减少 mmap 压力。
    3. 推送告警（Server酱）—— 告诉用户**具体该关哪个应用**，
       把「该关豆包」变成「豆包占了 488MB，建议关掉」。

绝不做的：杀用户的 VS Code / Chrome / 豆包 —— 那是用户的机器，不是我们的。

用法:
    python3 scripts/memory_guard.py            # 检查一次，必要时清理
    python3 scripts/memory_guard.py --status   # 只看，不动手
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
GUARD_LOG = RUNTIME / "memory_guard.log"
STATE_FILE = RUNTIME / "memory_guard_state.json"

# 可用内存低于这个值（MB）就动手清理
DEFAULT_THRESHOLD_MB = int(os.getenv("MEMORY_GUARD_THRESHOLD_MB", "900"))
# 告警节流：6 小时内只推一次
ALERT_COOLDOWN_S = 6 * 3600
OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(GUARD_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


# ── 内存读取 ───────────────────────────────────────────────────────────────
def _vm_stat() -> dict:
    out = {}
    try:
        raw = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=10).stdout
        for m in re.finditer(r"Pages\s+([a-z ]+):\s+([\d\.]+)", raw):
            key = m.group(1).strip().replace(" ", "_")
            out[key] = float(m.group(2).replace(".", "")) * 4096 / (1024 * 1024)
    except Exception:
        pass
    return out


def memory_state() -> dict:
    """返回 {"free_mb", "avail_mb", "swap_used_mb", "swap_total_mb", ...}"""
    vm = _vm_stat()
    free = vm.get("free", 0.0)
    spec = vm.get("speculative", 0.0)
    # 「可用」= free + speculative + 可回收的 inactive（macOS 会按需回收）
    inactive = vm.get("inactive", 0.0)
    avail = free + spec + inactive * 0.3
    swap_used = swap_total = 0.0
    try:
        raw = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"total\s*=\s*([\d\.]+)M", raw)
        if m:
            swap_total = float(m.group(1))
        m = re.search(r"used\s*=\s*([\d\.]+)M", raw)
        if m:
            swap_used = float(m.group(1))
    except Exception:
        pass
    try:
        load = os.getloadavg()[0]
    except Exception:
        load = 0.0
    return {
        "free_mb": round(free, 1),
        "avail_mb": round(avail, 1),
        "inactive_mb": round(inactive, 1),
        "swap_used_mb": swap_used,
        "swap_total_mb": swap_total,
        "load": round(load, 2),
    }


def top_consumers(limit: int = 6) -> list:
    """按 RSS 排名，按应用聚合。返回 [(应用名, MB), ...]。"""
    try:
        raw = subprocess.run(["ps", "-Ao", "rss=,comm="], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    agg = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            rss_kb = int(parts[0])
        except ValueError:
            continue
        comm = parts[1]
        m = re.search(r"/([^/]+\.app)/", comm)
        if m:
            app = m.group(1).replace(".app", "")
        else:
            app = Path(comm).name or comm
        agg[app] = agg.get(app, 0) + rss_kb
    rows = sorted(agg.items(), key=lambda kv: -kv[1])
    return [(name, round(kb / 1024.0, 1)) for name, kb in rows[:limit]]



# ── 清理动作 ───────────────────────────────────────────────────────────────
def unload_ollama_models() -> tuple:
    """卸载 Ollama 已加载的模型。返回 (卸载数量, 说明)。"""
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/ps", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return 0, "查询 Ollama 失败(%s)" % type(exc).__name__
    models = data.get("models") or []
    if not models:
        return 0, "没有已加载的模型"
    done = 0
    for m in models:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            done += 1
            log("   已卸载 Ollama 模型: %s" % name)
        except Exception as exc:
            log("   卸载 %s 失败: %s" % (name, exc))
    return done, "卸载 %d 个模型" % done


def cleanup_stale_files(max_age_days: int = 3) -> float:
    """清理陈旧日志（>max_age_days）与 __pycache__。返回释放的 MB（估算）。"""
    freed = 0.0
    cutoff = time.time() - max_age_days * 86400
    targets = []
    for pat in ("*.log", "*.log.*", "*.tmp"):
        targets.extend(RUNTIME.glob(pat))
    for p in targets:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                freed += p.stat().st_size / (1024 * 1024)
                p.unlink()
        except Exception:
            pass
    for p in list(ROOT.rglob("__pycache__")):
        try:
            if not p.is_dir() or time.time() - p.stat().st_mtime >= cutoff:
                continue
            for f in list(p.glob("*.pyc")):
                freed += f.stat().st_size / (1024 * 1024)
                f.unlink()
            try:
                p.rmdir()
            except Exception:
                pass
        except Exception:
            pass
    return round(freed, 2)


# ── 告警 ───────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(STATE_FILE))
    except Exception:
        pass


def maybe_alert(mem: dict, freed_mb: float, unloaded: int) -> None:
    """内存紧张时推一次告警（节流 6 小时）。"""
    state = _load_state()
    last = float(state.get("last_alert_ts") or 0)
    if time.time() - last < ALERT_COOLDOWN_S:
        return
    consumers = top_consumers(5)
    lines = ["Trident 金融后端 · 内存告警\n"]
    lines.append("可用内存仅 %.0f MB（阈值 %.0f MB）" %
                 (mem["avail_mb"], DEFAULT_THRESHOLD_MB))
    lines.append("swap 已用 %.0f / %.0f MB · load %.1f" %
                 (mem["swap_used_mb"], mem["swap_total_mb"], mem["load"]))
    if unloaded:
        lines.append("已自动卸载 %d 个 Ollama 模型" % unloaded)
    if freed_mb > 0:
        lines.append("已清理陈旧日志 %.1f MB" % freed_mb)
    lines.append("\n内存占用排行（建议从后往前关）：")
    for name, mb in consumers:
        lines.append("  · %s  %.0f MB" % (name, mb))
    lines.append("\n关掉最大的 1-2 个就能把 load 降下来。")
    try:
        sys.path.insert(0, str(ROOT / "src_python"))
        import notification_channels
        notification_channels.register_known_channels()
        notification_channels.dispatch("⚠️ 内存告警 · 后端可能卡顿",
                                       "\n".join(lines), only=["wechat_sct"])
        log("已推送内存告警")
    except Exception as exc:
        log("推送内存告警失败: %s" % exc)
    state["last_alert_ts"] = time.time()
    state["last_avail_mb"] = mem["avail_mb"]
    _save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只看状态，不清理")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_MB)
    args = ap.parse_args()

    mem = memory_state()
    consumers = top_consumers(6)
    print("=" * 62)
    print("  内存守卫")
    print("=" * 62)
    print("  可用内存   : %.0f MB (free %.0f / inactive %.0f)" %
          (mem["avail_mb"], mem["free_mb"], mem["inactive_mb"]))
    print("  swap       : %.0f / %.0f MB" %
          (mem["swap_used_mb"], mem["swap_total_mb"]))
    print("  load       : %.2f" % mem["load"])
    print("  阈值       : %d MB" % args.threshold)
    print("  占用排行   :")
    for name, mb in consumers:
        print("     · %-28s %.0f MB" % (name[:28], mb))

    if args.status:
        return 0

    if mem["avail_mb"] >= args.threshold:
        print("\n  ✅ 内存充足，无需处理")
        return 0

    print("\n  ⚠️  内存低于阈值，开始清理 …")
    unloaded, note = unload_ollama_models()
    print("     Ollama: %s" % note)
    freed = cleanup_stale_files()
    print("     清理陈旧日志: %.1f MB" % freed)

    after = memory_state()
    print("     清理后可用内存: %.0f MB (提升 %.0f MB)" %
          (after["avail_mb"], after["avail_mb"] - mem["avail_mb"]))
    maybe_alert(after, freed, unloaded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
