#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine_watchdog — 引擎挂死检测 & 自动拉起。

为什么需要它（2026-11-04 生产实测）
-----------------------------------
launchd 的 `KeepAlive` **只在进程退出时**才拉起。而 2026-11-04 的事故里，
引擎进程**活着但卡死**（主线程停在 macOS mDNS 的 socket_getaddrinfo 里）：

    launchctl list | grep com.trident.engine
    5874    -15    com.trident.engine     ← PID 5874 还在，状态看似正常

于是：
  · launchd 认为一切正常 → 永远不重启；
  · 进程内的 asyncio 超时也救不了（事件循环被同步 DNS 调用焊死）；
  · 结果：引擎从 12:37 卡到手动介入为止，**零信号产出**，前端一直显示「待分析」。

检测原理
--------
引擎主循环每轮都会写 `runtime/ai_worker_progress.json`（step + 时间戳）。
只要**这个文件超过 N 分钟没更新**，就说明主循环不再推进 → 判定挂死。
这个信号比"进程是否活着"可靠得多，且零成本（读一个文件）。

动作
----
  · 挂死 → `launchctl kickstart -k gui/<uid>/com.trident.engine`
    （强制重启；launchd 的 KeepAlive 会保证它被拉回来）
  · 进程压根没在 → 同样 kickstart 拉起

用法：
    python3 scripts/engine_watchdog.py            # 检查并在需要时重启
    python3 scripts/engine_watchdog.py --status   # 只报告，不动手
    python3 scripts/engine_watchdog.py --threshold 600   # 10 分钟无更新即判定挂死
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config

LABEL = "com.trident.engine"
DEFAULT_THRESHOLD_S = 600.0   # 10 分钟无进展即判定挂死
                               # （8GB 机器上引擎一轮要处理几百条新闻，5 分钟容易误判）
GRACE_AFTER_BOOT_S = 1800.0   # 引擎刚启动的宽限期：progress 文件尚未生成时不算挂死
MIN_RESTART_INTERVAL_S = 900.0  # 两次重启之间的最小间隔 —— 防无限重启循环

# v413（2026-11-17）：`pool-blocked` 不能用 30 分钟这条线判死。
# 主循环进入 pool-blocked = **提供方全池熔断**（quota/auth/net），此时重启进程
# 对上游故障**毫无作用**，只会丢掉在办批次、重新认领、再进 pool-blocked。
# 实测 2026-11-17：23:35 起停在 pool-blocked 11,400s → 02:18 被强杀；
# 04:00-06:00 又因同样原因零产出；engine_watchdog.log 累计强杀 859 次。
# 所以给它一条**独立、更宽**的线（6 小时），超时才当作真挂。
POOL_BLOCKED_HANG_AFTER_S = 21600.0
POOL_BLOCKED_STEP = "pool-blocked"

# v435（2026-11-19）：非 pool-blocked 步长的挂死线，独立于 threshold。
# 事故：06:21 一批分析协程被同步调用阻塞、批次 420s 预算因事件循环焊死没能触发，
# 主循环卡在 analyze 63 分钟；旧逻辑 step 挂死线 = threshold*3 = 5400s(90min)，
# 心跳线程又持续刷新文件 mtime，于是看门狗全程判「正常」，零产出直到人工介入。
# 单批硬预算 TRIDENT_BATCH_TIMEOUT_S=420s(7min)，加上下文预热余量，正常一批不会
# 连续停在 analyze/batch 超过 ~12min；取 1500s(25min) 留足 3 倍余量，既不误杀
# 长批次，又能在真挂死时半小时内自动拉起。
STEP_HANG_AFTER_S = 700.0

_STATE_FILE = Path(config.BASE_DIR) / "runtime" / "engine_watchdog_state.json"


def _progress_path() -> Path:
    return Path(config.BASE_DIR) / "runtime" / "ai_worker_progress.json"


def _pause_flag_path() -> Path:
    """数据暂停标记（pause_data.sh 写、resume_data.sh 删）。"""
    return Path.home() / ".trident" / "state" / "DATA_PAUSED"


def engine_pid() -> int | None:
    """从 launchctl 拿引擎 PID。"""
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == LABEL:
            pid = parts[0].strip()
            if pid and pid != "-":
                try:
                    return int(pid)
                except ValueError:
                    return None
    # v342: 引擎经常不是 launchd 直管的进程，而是 master_api.py 拉起的子进程 ——
    # 这时 launchctl 里显示 "-"，看门狗会误判"进程不在"并反复尝试重启。
    # 加一条 pgrep 兜底，按真实进程判断存活。
    try:
        # v414（2026-11-17）：原来的 "src_python/engine.py" 匹配不到实际命令行
        # `.../Python engine.py`（cwd 就在 src_python，命令行不带目录）→ 看门狗
        # 永远判"进程不在" → kickstart -k 给健康引擎发 SIGTERM → 反复重启。
        # 改成按"以 engine.py 结尾"匹配：`Python engine.py` 与
        # `python src_python/engine.py` 两种布局都能命中，且不会误中 engine_watchdog.py。
        out2 = subprocess.run(
            ["pgrep", "-f", r"engine\.py$"], capture_output=True, text=True,
            timeout=10,
        ).stdout.split()
        if out2:
            return int(out2[-1])
    except Exception:
        pass
    return None


def engine_uptime_s() -> float | None:
    """引擎进程已运行多少秒（拿不到返回 None）。"""
    pid = engine_pid()
    if not pid:
        return None
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        # etime 形如 01:23:45 或 12:34
        if not out:
            return None
        if "-" in out:  # 形如 12-03:04:05（超过一天）
            days, rest = out.split("-", 1)
            h, m, s = rest.split(":")
            return int(days) * 86400 + int(h) * 3600 + int(m) * 60 + int(s)
        parts = out.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None
    return None


def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(st: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def last_restart_ago_s() -> float | None:
    """距上次看门狗重启过了多少秒。"""
    ts = _read_state().get("last_restart_ts")
    if not ts:
        return None
    return max(0.0, time.time() - float(ts))


def engine_alive() -> bool:
    """引擎进程是否在跑。

    v421（2026-11-17）：原来只读 `launchctl list` 的 PID，看到 "-" 就直接判死。
    但 launchd 拉起失败后的兜底是「直接 exec run_engine.sh」，那种进程的 PPID=1、
    launchd 里没有 PID → 判死 → 再 kickstart → 又多一个进程（实测 14:10 两个
    engine 同时在跑，互相抢队列、双倍烧额度）。
    现在：launchd 有 PID 才算活；否则退回 pgrep 按真实进程判断。
    """
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == LABEL:
                pid = parts[0].strip()
                if pid and pid != "-":
                    return True
                break
    except Exception:
        pass
    # 兜底：launchd 无 PID（-）时，按真实进程判断
    return bool(engine_pid())


def engine_pids() -> list:
    """所有 engine.py 进程 PID（按启动先后排序，最老的在前）。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"engine\.py$"], capture_output=True, text=True,
            timeout=10, ).stdout.split()
        pids = [int(p) for p in out if p.strip().isdigit()]
    except Exception:
        return []
    def _etime(pid):
        try:
            s = subprocess.run(["ps", "-p", str(pid), "-o", "etime="],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            return s or "99"
        except Exception:
            return "99"
    return sorted(pids, key=_etime)


def progress_age_s() -> float | None:
    """progress 文件距今多少秒没更新；文件不存在返回 None。"""
    p = _progress_path()
    if not p.exists():
        return None
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return None


def progress_step() -> Optional[str]:
    """progress 里当前的 step 名（如 batch/analyze/pool-blocked）；拿不到返回 None。

    v413：只有 step 名能区分「主循环卡住」和「上游全挂只能干等」——
    后者重启无用，前者必须重启。
    """
    p = _progress_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        step = str(data.get("step") or "").strip()
        return step or None
    except Exception:
        return None


def step_age_s() -> float | None:
    """progress 里 step 已停留多少秒（step_ts）；拿不到返回 None。

    v398c 背景：progress 的 ts 现在由**独立心跳线程**每 45s 刷新，
    「文件新鲜」只能证明进程活着，证明不了主循环在推进。
    step_ts 只在真正换步时更新，因此它才是「是否卡死」的真信号：
      · 长批次（一条新闻 10-20 分钟 × 8 条）step 会长时间停在 analyze/batch，
        这是正常的 —— 用更宽的阈值（默认 3×threshold）区分；
      · 主循环真卡住时 step 完全不换，最终仍会被判挂并重启。
    """
    p = _progress_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        step_ts = float(data.get("step_ts") or 0.0)
        if step_ts <= 0:
            return None
        return max(0.0, time.time() - step_ts)
    except Exception:
        return None


def _dump_stacks_before_kill(reason: str) -> None:
    """v488：重启前先取证。挂死的栈是唯一能定位「到底卡在哪个同步调用」的证据，
    杀完就永久丢失。engine 侧 sitecustomize 已注册 faulthandler(SIGUSR1) →
    全线程栈直接落到 engine.err.log，这里只负责发信号并留一条分隔标记。
    注意：若目标进程没注册 SIGUSR1，发过去等于 SIGKILL（默认动作），
    所以只在 engine 侧确认注册过（cwd/PYTHONPATH 已显式给）后才用；
    保险起见先检查 stderr 里是否有 [STACK-DUMP] registered 标记。"""
    pid = engine_pid()
    if not pid:
        return
    try:
        marked = False
        try:
            log = Path("/Users/claire/.trident/log/engine.err.log")
            if log.exists():
                with log.open("rb") as f:
                    f.seek(max(0, log.stat().st_size - 400_000))
                    marked = b"[STACK-DUMP] SIGUSR1 registered pid=%d" % pid in f.read()
        except Exception:
            marked = False
        if not marked:
            print(f"⚠️ 跳过取栈：engine({pid}) 未见 SIGUSR1 注册标记（发了会直接杀进程）")
            return
        print(f"📸 重启前取栈：kill -USR1 {pid}（原因：{reason}）")
        os.kill(pid, signal.SIGUSR1)
        time.sleep(2.0)
    except Exception as exc:
        print(f"⚠️ 取栈失败: {type(exc).__name__}: {exc}")


def restart_engine() -> bool:
    """强制重启引擎（launchd 会按 KeepAlive 重新拉起）。"""
    uid = os.getuid()
    for target in (f"gui/{uid}/{LABEL}", f"user/{uid}/{LABEL}", LABEL):
        try:
            r = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                print(f"✅ 已重启引擎（launchctl kickstart -k {target}）")
                st = _read_state()
                st["last_restart_ts"] = time.time()
                st["restart_count"] = int(st.get("restart_count", 0)) + 1
                _write_state(st)
                return True
        except Exception:
            continue
    print("❌ 重启失败：launchctl kickstart 未成功")
    # v342: 兜底 —— com.trident.engine 可能根本不存在于 launchd 域（由 master 托管），
    # 那样 kickstart 永远失败，看门狗只能干看着引擎躺着。直接按 launchd 的同一条
    # 命令把进程拉起来，并用 pgrep 确认真的起来了。
    try:
        wrapper = "/Users/claire/.trident/wrappers/run_engine.sh"
        if os.path.exists(wrapper):
            log_path = "/Users/claire/.trident/log/engine.out.log"
            fh = open(log_path, "ab")
            subprocess.Popen(
                ["/bin/bash", wrapper], stdout=fh, stderr=fh,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            time.sleep(3)
            if engine_pid():
                print("✅ 已重启引擎（兜底：直接拉起 run_engine.sh）")
                st = _read_state()
                st["last_restart_ts"] = time.time()
                st["restart_count"] = int(st.get("restart_count", 0)) + 1
                _write_state(st)
                return True
            print("❌ 兜底拉起后仍无引擎进程")
    except Exception as exc:
        print(f"❌ 兜底拉起异常: {type(exc).__name__}: {exc}")
    return False


def decide(alive: bool, age: Optional[float], uptime: Optional[float],
           threshold: float, since_last_restart: Optional[float] = None,
           step_age: Optional[float] = None, hang_after: Optional[float] = None,
           step: Optional[str] = None,
           pool_hang_after: Optional[float] = None
           ) -> Tuple[bool, bool, str, bool]:
    """挂死判定 —— 纯逻辑，不碰系统，方便单测。

    返回 (hung, hard_failure, reason, should_restart)

    hung          : 是否判定挂死
    hard_failure  : 是否「进程已经不在」这种硬故障（硬故障不冷却，立刻拉起）
    reason        : 判定理由（给人看）
    should_restart: 最终是否真的要执行重启
    """
    # ── 1. 进程不在 = 硬故障 ──────────────────────────────────
    # 进程死亡是硬故障（OOM / 崩溃），必须尽快拉起 —— 不受冷却期限制。
    # 冷却期只用于「进程还活着但主循环卡死」这种软故障：
    # 那种情况可能是误判（8GB 机器换页时 progress 会短暂不更新），
    # 盲目重启会打断正常的一轮处理。
    if not alive:
        # v434：PID 探测可能瞬时漏判（launchd 丢失跟踪 / pgrep 瞬时空返 /
        # launchctl list 超时）。若 progress 心跳仍新鲜（刚在写），说明进程
        # 实际存活——死进程不会继续更新心跳。此时判为探测不确定，不重启，
        # 避免把跑到一半的批次 SIGTERM 掉、白烧已发的输入 token。
        if age is not None and age < min(120.0, threshold * 0.5):
            reason = (f"PID 探测未见进程，但心跳 {age:.0f}s 前刚更新"
                      " → 视为探测误判，本轮不重启")
            return False, False, reason, False
        reason = "进程不在"
        # 硬故障永远要重启 —— launchd 自身也可能拉不起来，
        # 看门狗是唯一的兜底（实测曾出现引擎被 OOM kill 后 15 分钟无人拉起）。
        return True, True, reason, True

    # ── 2. progress 文件不存在 ────────────────────────────────
    # 引擎启动时要初始化（连 DB / 拉配置 / 建内存），progress 要几十秒后才出现；
    # 之前这段时间 cron 一查就杀 → 无限重启循环（实测 101 次重启）。
    # 现在改为：文件不存在时，只有引擎已运行超过宽限期才判死。
    if age is None:
        if uptime is None:
            return False, False, "progress 文件不存在但拿不到运行时长 → 暂不判定", False
        if uptime > max(threshold, GRACE_AFTER_BOOT_S):
            reason = f"引擎已运行 {uptime:.0f}s 仍无 progress → 判定挂死"
            return _apply_cooldown(True, False, reason, since_last_restart)
        return False, False, f"引擎启动中（{uptime:.0f}s），progress 未生成 → 宽限期内不判定", False

    # ── 3. progress 过期 ─────────────────────────────────────
    if age > threshold:
        reason = f"主循环已 {age:.0f}s 无进展"
        return _apply_cooldown(True, False, reason, since_last_restart)

    # ── 4. v398c：文件新鲜但 step 长期不换 = 真卡死 ───────────────
    # 心跳线程让 mtime 永远是新的，"文件过期"这条判死通道实际已失效；
    # 这里补一条只认真推进的判据：step 超过 hang_after（默认 3×threshold，
    # 即 30 分钟）一动不动 → 判挂死。长批次停在 analyze/batch 属于正常推进
    # （批次内部会反复回到 batch/analyze），不会连续 30 分钟不换步。
    #
    # v413：`pool-blocked` 不属于「卡死」，属于「上游全挂只能干等」——
    # 重启进程不解决上游故障，只会丢掉在办批次，所以给它一条独立的宽线。
    if step_age is not None:
        if step and step == POOL_BLOCKED_STEP:
            _pool_after = float(
                pool_hang_after if pool_hang_after is not None
                else POOL_BLOCKED_HANG_AFTER_S)
            if step_age > _pool_after:
                reason = (f"pool-blocked 已 {step_age:.0f}s（阈值 {_pool_after:.0f}s，"
                          f"上游全池熔断）→ 判定真挂死")
                return _apply_cooldown(True, False, reason, since_last_restart)
            return False, False, (
                f"上游全池熔断，引擎在等恢复（已 {step_age:.0f}s，"
                f"阈值 {_pool_after:.0f}s）→ 重启无用，不判定挂死"), False

        _hang_after = float(hang_after if hang_after is not None else STEP_HANG_AFTER_S)
        if step_age > _hang_after:
            reason = f"step 停在 {step_age:.0f}s 未换步（阈值 {_hang_after:.0f}s）"
            return _apply_cooldown(True, False, reason, since_last_restart)

    return False, False, "正常", False


def _apply_cooldown(hung: bool, hard_failure: bool, reason: str,
                    since_last_restart: Optional[float]) -> Tuple[bool, bool, str, bool]:
    """软故障套上「最小重启间隔」防无限重启循环。"""
    if hard_failure:
        return hung, hard_failure, reason, True
    if since_last_restart is not None and since_last_restart < MIN_RESTART_INTERVAL_S:
        return hung, hard_failure, reason + f"（距上次重启仅 {since_last_restart:.0f}s，冷却期内跳过）", False
    return hung, hard_failure, reason, True


def main() -> int:
    ap = argparse.ArgumentParser(description="引擎挂死检测 & 自动拉起")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_S,
                    help="progress 多少秒没更新即判定挂死（默认 %d）" % DEFAULT_THRESHOLD_S)
    ap.add_argument("--status", action="store_true", help="只报告，不动手")
    args = ap.parse_args()

    # v482：数据暂停期间不做任何"拉起/重启"动作。
    # 背景（2026-11-19 16:35 实测）：pause_data.sh 把 engine 从 launchd 摘掉后，
    # 本看门狗仍按阈值把它 kickstart 回来 —— 结果"标记说暂停、引擎却在跑、
    # 还在拿暂停前的旧新闻出决策"。暂停语义以 state/DATA_PAUSED 为准。
    if _pause_flag_path().exists() and not args.status:
        print(f"  [PAUSED] 检测到 {_pause_flag_path()}，跳过引擎拉起/重启")
        return 0

    # v421：重复引擎去重 —— 兜底 exec 与 launchd 各拉起一次时会同时存在两个引擎，
    #   它们抢同一批 raw_news、双倍烧额度、写锁互相压。这里保留一个（优先留 launchd
    #   管的那只，否则留最老的），其余 SIGTERM。
    _pids = engine_pids()
    if len(_pids) > 1:
        _keep = None
        try:
            _out = subprocess.run(["launchctl", "list"], capture_output=True,
                                  text=True, timeout=15).stdout
            for _line in _out.splitlines():
                _p = _line.split()
                if len(_p) >= 3 and _p[2] == LABEL and _p[0].strip() not in ("", "-"):
                    _keep = int(_p[0])
                    break
        except Exception:
            _keep = None
        if _keep not in _pids:
            _keep = _pids[0]
        for _pid in _pids:
            if _pid == _keep:
                continue
            try:
                os.kill(_pid, signal.SIGTERM)
                print(f"  [去重] 发现重复引擎 pid={_pid}，保留 pid={_keep}，已发 SIGTERM")
            except Exception as _e:
                print(f"  [去重] 终止 pid={_pid} 失败: {_e}")

    alive = engine_alive()
    age = progress_age_s()
    pf = _progress_path()
    uptime = engine_uptime_s()
    since = last_restart_ago_s()
    current_step = progress_step()

    print("══ 引擎看门狗 " + "═" * 50)
    print(f"  进程存活      : {'是' if alive else '否 ❌'}")
    if uptime is not None:
        print(f"  引擎已运行    : {uptime:.0f}s")
    if age is None:
        print(f"  progress 文件 : 不存在（{pf}）")
    else:
        print(f"  progress 年龄 : {age:.0f}s  (阈值 {args.threshold:.0f}s)")
        print(f"  当前 step     : {current_step or '-'}")
        try:
            print(f"  progress 内容 : {pf.read_text(encoding='utf-8').strip()}")
        except Exception:
            pass

    hung, hard_failure, reason, should_restart = decide(
        alive, age, uptime, args.threshold, since,
        step_age=step_age_s(), step=progress_step())

    print(f"  判定          : {'⚠️ 挂死' if hung else '正常'}（{reason}）")
    if hard_failure:
        print("  类型          : 硬故障（进程已死）→ 不走冷却期，立即拉起")

    if args.status:
        return 0

    if should_restart:
        print(f"  → {reason}，执行重启")
        if not hard_failure:
            # 软故障（活着但卡死）才有栈可取；硬故障进程已经没了
            _dump_stacks_before_kill(reason)
        return 0 if restart_engine() else 1

    print("  → 引擎健康，无需动作")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
