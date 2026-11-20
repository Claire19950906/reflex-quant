#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用熔断器（Circuit Breaker）—— 连续失败自动熔断，避免反复打必失败请求。

背景（金十 token 被烧的教训）：原 5s x 2 页轮询对同一必失败请求反复打，
免费配额快速耗尽。根因不是轮询快，而是「没有失败计数 → 没有熔断 → 没有冷却」。

三态流转：
    closed ──连续失败 N 次──> open（拒绝请求，冷却 open_seconds 秒）
    open   ──冷却结束──────> half_open（放行一次探测）
    half_open ──探测成功──> closed（清零）
    half_open ──探测失败──> open（立即重新熔断，重新计时）

v322 根治「突然抽风」的三处硬伤（2026-11-04）：

1. **half_open 曾经无限制放行**
   老代码注释写「只放行一次探测请求」，实现却是 ``return True`` ——
   任何线程在 open→half_open 之后都能继续放行，熔断等于失效。
   现在 half_open 用 ``_probe_in_flight`` 做 single-flight：
   冷却结束后**只有第一个**请求能过，其余全部拒绝。

2. **恢复瞬间的补偿性重试风暴**
   熔断冷却结束的那一刻，所有排队请求同时醒来 → 几十个并发同时打过去
   → 立刻 429 → 再次熔断 → 循环抽风。
   现在 open→half_open 的放行带**随机抖动**（jitter），
   把同一时刻醒来的请求打散成一小批一小批。

3. **熔断状态是进程内的 → 多进程各自重试放大**
   engine / api / review_agent 是三个独立进程，各有自己的熔断器。
   一个进程熔断了，另外两个还在继续打 → 同一份额度被放大 N 倍消耗。
   现在熔断状态**落盘共享**（原子写），一个进程发现通道坏了，
   其它进程立刻一起冷却 —— 这才叫「治抽风」而不是「治某一处抽风」。

纯 stdlib，无副作用，任何数据源 fetch 前调用 ``allow``，成功/失败回报即可。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Dict, Optional

# ── 跨进程共享状态的落盘目录 ────────────────────────────────────────────────
# 放在 backend/runtime/breakers/ 下（与其它运行时状态同目录，便于一起清理）。
_STATE_DIR = Path(os.getenv("TRIDENT_BREAKER_DIR") or
                  (Path(__file__).resolve().parent.parent / "runtime" / "breakers"))
_SHARE_ENABLED = os.getenv("LLM_BREAKER_SHARED", "1").strip().lower() not in (
    "0", "false", "False")


def _file_for(name: str) -> Path:
    """熔断器名 → 状态文件（名字做 hash，避免路径非法字符）。"""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:16]
    return _STATE_DIR / ("%s.json" % digest)


# ── 凭据指纹（credential epoch）──────────────────────────────────────────
# 事故（2026-11-05）：key 轮换后通道已经恢复，但**昨天**的 401 写下的
# state="open"（auth 冷却 24h）还钉在磁盘上 → 换 key 后仍被判熔断 →
# 主循环跳过认领 → 前端「待分析」只涨不跌（通道体检 ready=False）。
#
# 修法：落盘状态里带上「当时的凭据指纹」。读盘时指纹与当前不符 →
# 认定该状态属于**旧凭据**，直接忽略（等价于这个通道从未熔断过）。
# 指纹覆盖所有 LLM key：任一 key 变化 → 指纹变化 → 全部旧状态作废。
# 指纹从 os.environ 现算（config.load_dotenv 已把 .env 灌进 os.environ），
# 不缓存 —— 进程内换 key 后立刻生效。
_CRED_ENV_KEYS = (
    "MINIMAX_API_KEY", "MINIMAX2_API_KEY", "MINIMAX_MAIN_KEY", "MINIMAX_GATE_KEY",
    "MINIMAX_ANOMALY_KEY", "MINIMAX_REFLECT_KEY", "MINIMAX_PEER_KEY",
    "MINIMAX_PEER2_KEY", "OPENROUTER_API_KEY", "ARK_API_KEY", "AIPING_API_KEY",
    "XINSHU_API_KEY",
)


def credential_epoch() -> str:
    """当前 LLM 凭据指纹（任一 key 变化即变化）。

    返回空串表示环境里没有任何 LLM key（非 LLM 数据源，如 Yahoo/OKX，
    不需要凭据隔离 → 不参与指纹判定）。
    """
    parts = []
    for k in _CRED_ENV_KEYS:
        try:
            v = (os.getenv(k) or "").strip()
        except Exception:
            v = ""
        if v:
            parts.append("%s=%s" % (k, v))
    if not parts:
        return ""
    return hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]



def _write_state(path: Path, payload: Dict[str, object]) -> None:
    """原子写（先写临时文件再 os.replace）—— 防止半截文件把别的进程读崩。"""
    tmp = None
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass


def _read_state(path: Path) -> Optional[Dict[str, object]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class CircuitBreaker:
    """按 key（数据源/资产）独立计数的熔断器（内存态 + 跨进程共享落盘）。"""

    def __init__(self, name: str, fail_threshold: int = 3,
                 open_seconds: float = 60.0,
                 jitter_ratio: float = 0.25,
                 probe_timeout: Optional[float] = None) -> None:
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold))
        self.open_seconds = max(0.001, float(open_seconds))
        # 抖动比例：冷却时间额外随机拉长 0~jitter_ratio 倍
        self.jitter_ratio = max(0.0, float(jitter_ratio))
        # 探测丢失兜底：半开探测若迟迟不返回（进程卡死/超时），
        # 不能让熔断永久卡在 half_open —— 超时后允许新的探测。
        # v423（2026-11-17）：原来取 max(60, open_seconds)，而 LLM 额度熔断
        # 的 open_seconds = 1800 → probe_timeout 也是 1800，意味着一只丢失的
        # 探测会把通道锁死 30 分钟。实测事故：额度 16:20 已恢复（手工探针
        # 2.8s 通），引擎却一直打「跳过认领：…[quota(probe占用)]」，就是因为
        # 探测在飞且要等到底。现在封顶 180s：真实 LLM 调用超时都在 30~120s，
        # 180s 足够区分"慢请求"和"丢了的探测"，最坏也只白等 3 分钟。
        self.probe_timeout = (float(probe_timeout) if probe_timeout
                              else min(180.0, max(60.0, self.open_seconds)))
        self._fail = 0
        self._opened_at = 0.0          # 用 time.time() 而非 monotonic：跨进程可比
        self._wait_until = 0.0         # 抖动后的**固定**放行时刻（见 allow()）
        self._state = "closed"
        self._probe_in_flight = False
        self._probe_at = 0.0
        self._lock = threading.Lock()
        self._shared_path = _file_for(name) if _SHARE_ENABLED else None
        self._last_sync = 0.0

    # ── 跨进程同步 ─────────────────────────────────────────────────────────
    def _sync_from_shared(self) -> None:
        """从共享文件拉取状态（每 0.5s 最多一次，避免频繁 IO）。"""
        if self._shared_path is None:
            return
        now = time.time()
        if now - self._last_sync < 0.5:
            return
        self._last_sync = now
        data = _read_state(self._shared_path)
        if not data:
            return
        try:
            other_state = str(data.get("state") or "closed")
            other_opened = float(data.get("opened_at") or 0.0)
            other_fail = int(data.get("fail") or 0)
            other_wait = float(data.get("wait_until") or 0.0)
        except (TypeError, ValueError):
            return
        # ── v322: 凭据指纹校验 ──────────────────────────────────────────
        # 落盘状态属于「写它时的那套凭据」。key 换了 → 指纹不符 → 旧状态作废
        # （否则换 key 后仍被 24h 的 auth 熔断钉死 → 主循环跳过认领 →
        #  「待分析」只涨不跌）。旧版本文件没有 epoch 字段 → 视为过期作废。
        _cur_epoch = credential_epoch()
        if _cur_epoch:
            _file_epoch = str(data.get("epoch") or "")
            if _file_epoch != _cur_epoch:
                # 旧凭据留下的状态 → 忽略，并清掉文件避免反复读。
                try:
                    if self._shared_path.exists():
                        self._shared_path.unlink()
                except Exception:
                    pass
                return
        # 旧版状态文件没有 wait_until → 退回「基准冷却」推导，保证向后兼容
        if other_state == "open" and other_wait <= 0.0:
            other_wait = other_opened + self.open_seconds
        # 共享状态更「悲观」时采纳它（任一进程发现故障，其它进程一起冷却）。
        # 只有共享状态已恢复 closed 才把本进程拉回 closed。
        if other_state == "closed":
            if self._state != "closed":
                self._state = "closed"
                self._fail = 0
                self._probe_in_flight = False
                self._opened_at = 0.0
                self._wait_until = 0.0
            return
        if other_state == "open":
            if self._state != "open" or other_opened > self._opened_at:
                self._state = "open"
                self._opened_at = other_opened
                self._fail = max(self._fail, other_fail)
                self._probe_in_flight = False
                # 采纳对方的放行时刻 → 所有进程在**同一刻**醒来，
                # 配合 single-flight 只放 1 个探测，才是真正的「打散」。
                self._wait_until = max(self._wait_until, other_wait)
            return
        if other_state == "half_open" and self._state == "closed":
            self._state = "half_open"
            self._probe_in_flight = False

    def _publish(self) -> None:
        """把当前状态发布到共享文件。"""
        if self._shared_path is None:
            return
        _write_state(self._shared_path, {
            "name": self.name,
            "state": self._state,
            "fail": self._fail,
            "opened_at": self._opened_at,
            "wait_until": self._wait_until,
            # v322: 带上凭据指纹 —— key 轮换后旧状态自动作废
            "epoch": credential_epoch(),
            "updated_at": time.time(),
        })

    # ── 三态流转 ───────────────────────────────────────────────────────────
    def allow(self) -> bool:
        """请求前调用：True 可发起，False 已熔断（应跳过本轮并降级）。"""
        with self._lock:
            self._sync_from_shared()
            if self._state == "closed":
                return True
            if self._state == "open":
                # 放行时刻是**熔断打开时固定算好的**（含抖动），
                # 绝不能每次调用重 roll —— 重 roll 会让冷却被提前穿透：
                # 连着几次 roll 到接近 0 就会在基准时间刚到时放行。
                wait_until = self._wait_until or (
                    self._opened_at + self.open_seconds)
                if time.time() >= wait_until:
                    self._state = "half_open"
                    self._probe_in_flight = True
                    self._probe_at = time.time()
                    self._publish()
                    return True
                return False
            # half_open：single-flight —— 只放行第一个探测请求。
            # 探测超时未回报 → 允许新探测，避免熔断永久卡死。
            if self._probe_in_flight:
                if time.time() - self._probe_at <= self.probe_timeout:
                    return False
                self._probe_in_flight = False   # 上一个探测丢了，重新选一个
            self._probe_in_flight = True
            self._probe_at = time.time()
            return True

    def success(self) -> None:
        """请求成功回执：清零并闭合。"""
        with self._lock:
            self._fail = 0
            self._state = "closed"
            self._opened_at = 0.0
            self._wait_until = 0.0
            self._probe_in_flight = False
            self._publish()

    def failure(self) -> None:
        """请求失败回执：累计；达阈值熔断 / half-open 探测失败立即重新熔断。"""
        with self._lock:
            self._fail += 1
            if self._state == "half_open":
                # 探测失败：立刻回到 open 并重新计时（失败计数保持 >= 阈值）
                self._fail = max(self._fail, self.fail_threshold)
                self._state = "open"
                self._opened_at = time.time()
                self._wait_until = self._opened_at + self.open_seconds * (
                    1.0 + random.uniform(0.0, self.jitter_ratio))
                self._probe_in_flight = False
                self._publish()
                return
            if self._fail >= self.fail_threshold:
                if self._state != "open":
                    self._opened_at = time.time()
                    # 抖动在**打开时一次算完**并固定下来，
                    # 所有进程读同一个 wait_until → 同时刻醒来 → single-flight 生效
                    self._wait_until = self._opened_at + self.open_seconds * (
                        1.0 + random.uniform(0.0, self.jitter_ratio))
                self._state = "open"
                self._probe_in_flight = False
                self._publish()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def available(self) -> bool:
        """**只读**判定：这个通道现在是否"还有机会"发请求（不消耗探测名额）。

        为什么需要它（2026-11-17 实测事故）：
          `main_pool_ready()` 原来用 `allow()` 做通道体检。可是 `allow()` 有副作用 ——
          open 冷却到期时它会**顺手把状态推到 half_open 并把 _probe_in_flight 置 True**，
          等于把唯一的那次探测名额用掉了。而体检本身不发任何请求，真正要发请求的
          调用点再 `allow()` 时拿到 False（同进程 30 分钟 probe_timeout 内），于是
          两个 MiniMax 通道被永久判成「熔断中」（注意：没有倒计时，说明不是 open
          而是卡在 half_open），引擎每轮打印「跳过认领：全部通道熔断…」、
          零决策、PENDING 一路涨 —— 而账户额度其实是好的。
        语义：
          · closed         → True
          · open           → 冷却是否已到（到了 True，但不改状态，探测留给真正调用点）
          · half_open      → 只有"没有探测在飞"（或探测已超时）才算可用
        """
        with self._lock:
            self._sync_from_shared()
            if self._state == "closed":
                return True
            if self._state == "open":
                wait_until = self._wait_until or (
                    self._opened_at + self.open_seconds)
                return time.time() >= wait_until
            # half_open：探测在飞就别再判可用，否则两个调用点会同时放行。
            # v432（2026-11-17）：探测超时后**必须把名额真正释放并落盘**。
            # 原来这里只返回 True、不清 _probe_in_flight —— 于是下一个调用点
            # allow() 又把它重置成"在飞"，跨进程看永远是 half_open+probe占用，
            # 实测把主分析锁死 4 小时（额度其实是好的）。
            if self._probe_in_flight:
                if time.time() - self._probe_at > self.probe_timeout:
                    self._probe_in_flight = False
                    self._probe_at = 0.0
                    self._publish()
                return not self._probe_in_flight
            return True

    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "fail_count": self._fail,
                "fail_threshold": self.fail_threshold,
                "open_seconds": self.open_seconds,
                "jitter_ratio": self.jitter_ratio,
                "shared": self._shared_path is not None,
                "probe_in_flight": self._probe_in_flight,
                "opened_at": round(self._opened_at, 3),
            }
# ---------------------------------------------------------------------------
# 全局按 key 注册表（进程内共享，避免每个 fetch 各自维护一个 breaker）
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get(name: str, fail_threshold: int = 3,
        open_seconds: float = 60.0) -> CircuitBreaker:
    """按 key 取（不存在则创建）熔断器。"""
    key = str(name or "").strip() or "_default"
    with _REGISTRY_LOCK:
        brk = _REGISTRY.get(key)
        if brk is None:
            brk = CircuitBreaker(key, fail_threshold=fail_threshold,
                                 open_seconds=open_seconds)
            _REGISTRY[key] = brk
        return brk


def report_all() -> Dict[str, Dict[str, object]]:
    """导出全部熔断器状态（供 /api/sources/status 之类面板展示）。"""
    with _REGISTRY_LOCK:
        return {k: b.status() for k, b in _REGISTRY.items()}


def reset(name: Optional[str] = None) -> None:
    """手动复位：name=None 复位全部。

    v322 修复：**必须同时删除落盘的共享状态文件**。
    原来只清进程内注册表 → 磁盘上的 state="open" 仍在 →
    下一次 get() 新建熔断器时 _sync_from_shared() 又把它读回来 →
    "复位"形同虚设。这有三个真实后果：
      1. 单测被前一天的 open 状态污染（test_gate_deferred_v320 6 个失败）；
      2. 生产里"换 key 后通道已恢复但仍被判定熔断" → 主循环跳过认领
         → 前端「待分析」只涨不跌（2026-11-05 实测）；
      3. 运维手工 reset 后问题依旧。

    现在 reset 同时删文件：name=None 清空整个状态目录，否则只删该 name 的文件。
    """
    with _REGISTRY_LOCK:
        if name is None:
            _REGISTRY.clear()
            _purge_state_dir()
        else:
            _REGISTRY.pop(str(name), None)
            _remove_state_file(str(name))


def _remove_state_file(name: str) -> None:
    """删除某个熔断器对应的落盘状态文件（不存在则忽略）。"""
    try:
        p = _file_for(name)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _purge_state_dir() -> None:
    """清空整个落盘状态目录（reset() 全量复位用）。"""
    try:
        if not _STATE_DIR.exists():
            return
        for f in _STATE_DIR.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def purge_all() -> None:
    """运维用：清空全部熔断状态（内存 + 磁盘）。

    换 key / 通道恢复后仍显示"熔断"时执行：
        python3 -c "import sys;sys.path.insert(0,'src_python');import circuit_breaker;circuit_breaker.purge_all()"
    """
    reset(None)


if __name__ == "__main__":
    import tempfile

    # 确定性用例：关掉抖动 + 共享目录指向临时目录（避免污染 runtime/）
    tmpdir = tempfile.mkdtemp(prefix="cbtest_")
    _STATE_DIR = Path(tmpdir)

    b = CircuitBreaker("jin10", fail_threshold=2, open_seconds=0.2,
                       jitter_ratio=0.0)
    print("初始:", b.status())
    assert b.allow() is True
    b.failure()
    b.failure()  # 达到阈值 → open
    print("连续失败后:", b.status())
    assert b.state == "open"
    assert b.allow() is False  # 熔断中拒绝
    time.sleep(0.25)  # 冷却结束
    assert b.allow() is True  # half-open 放行一次探测
    b.success()
    print("探测成功复位:", b.status())
    assert b.state == "closed"

    # half-open 探测失败 → 立即重新熔断
    b.failure(); b.failure()
    time.sleep(0.25)
    assert b.allow() is True
    b.failure()
    print("探测失败重新熔断:", b.status())
    assert b.state == "open"

    # ── v322: half_open single-flight ────────────────────────────────────
    c = CircuitBreaker("singleflight", fail_threshold=1, open_seconds=0.1,
                       jitter_ratio=0.0)
    c.failure()                       # → open
    time.sleep(0.15)
    first = c.allow()                  # 第一个：放行探测
    second = c.allow()                 # 第二个：必须被拒（旧实现会误放行）
    print("single-flight: first=%s second=%s" % (first, second))
    assert first is True
    assert second is False, "half_open 必须 single-flight，只放行一次探测"

    # ── v322: 探测超时兜底，不会永久卡死 ─────────────────────────────────
    d = CircuitBreaker("probetimeout", fail_threshold=1, open_seconds=0.1,
                       jitter_ratio=0.0, probe_timeout=0.2)
    d.failure()
    time.sleep(0.15)
    assert d.allow() is True          # 第一个探测
    assert d.allow() is False         # 探测在飞 → 拒
    time.sleep(0.25)                  # 探测超时
    assert d.allow() is True          # 超时后允许新探测（不会永久卡死）
    print("探测超时兜底: OK")

    # ── v322: 跨进程共享（一个进程熔断，其它进程一起冷却）──────────────
    p1 = CircuitBreaker("shared_x", fail_threshold=1, open_seconds=30.0,
                        jitter_ratio=0.0)
    p2 = CircuitBreaker("shared_x", fail_threshold=1, open_seconds=30.0,
                        jitter_ratio=0.0)
    p1.failure()                      # p1 熔断 → 落盘
    assert p2.allow() is False, "其它进程必须同步到熔断状态"
    print("跨进程共享: OK（p1 熔断 → p2 同步拒绝）")

    # 全局注册表
    get("yahoo_finance").failure()
    print("注册表:", report_all())
    print("熔断器自测通过 ✔")