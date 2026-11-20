#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_guard — 进程级 DNS 解析超时护栏（v322）。

背景（2026-11-04 生产实测）
---------------------------
引擎进程（PID 5874）跑着跑着**整个卡死**，表现是：
  · `runtime/ai_worker_progress.json` 停在 `{"step":"macro"}`，44 分钟不更新
  · `engine.out.log` 不再增长（PERF/REVIEW/SIGNAL 三连刷屏后戛然而止）
  · `launchctl list` 里 `com.trident.engine` **PID 还在** → launchd 的 KeepAlive
    只在进程**退出**时拉起，进程"活着但卡住"它完全无感 → 永久停摆
  · `sample <pid> 3` 抓栈，主线程 + asyncio 工作线程 100% 停在：

        socket_getaddrinfo → getaddrinfo → si_addrinfo → search_addrinfo
                          → mdns_addrinfo → _mdns_search_ex → kevent

    即 **macOS 的 mDNS 解析路径里无限阻塞**。

为什么"有 timeout 也救不了"
--------------------------
主循环里到处是 `await asyncio.wait_for(coro, timeout=15)`。但 wait_for 的超时
回调**依赖事件循环本身能继续跑**。一旦某个协程在 **C 层同步** 调用
`socket.getaddrinfo()`（requests / urllib3 建连前的域名解析），整个事件循环
线程就被卡住 —— 定时器根本没机会触发，wait_for 退化成"永远等"。
这正是"明明写了超时，还是永久挂死"的根本原因。

修法
----
在**进程级**给 `socket.getaddrinfo` 套一层超时护栏：
  · 把真正的解析丢到一次性线程里跑；
  · 主调用方最多等 `timeout_s` 秒；
  · 超时 → 抛 `socket.gaierror`（语义上就是"解析失败"），
    让上层的 requests/urllib3 走它**本来就写好的**错误分支。

这样任何同步 DNS 调用最多阻塞 N 秒，事件循环不可能被 DNS 永久焊死。

为什么放在 engine/main.py 最开头
-------------------------------
必须**在任何协程启动之前**装好，否则引擎启动期的解析仍可能卡死。
装好后第一次调用会打印一行 `[net-guard] DNS 超时护栏已启用 (5.0s)`，
便于运维确认"这版代码真的跑起来了"。
"""
from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

# 默认超时（秒）。给到 5s 是权衡：
#   · 正常公网 DNS 解析 < 200ms，5s 足够覆盖慢 DNS；
#   · macOS mDNS 卡死是"无限期"的，5s 足以把一次挂死压成一次可恢复的失败。
DEFAULT_DNS_TIMEOUT_S = 5.0

_installed = False
_original_getaddrinfo = None


class _DnsTimeout:
    """getaddrinfo 的超时包装（单例 executor）。"""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = float(timeout_s)
        # 独立线程池：与业务线程池隔离，DNS 卡死不会占满业务 worker。
        # max_workers=8 足以覆盖并发解析；线程是"一次性"的，卡死的解析线程
        # 会一直占着，但 getaddrinfo 很少并发爆这个量级，且主调用方已经超时返回。
        self._pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="dns-guard"
        )
        self._orig = socket.getaddrinfo

    def __call__(self, host, port, *args, **kwargs):
        fut = self._pool.submit(self._orig, host, port, *args, **kwargs)
        try:
            return fut.result(timeout=self.timeout_s)
        except TimeoutError:
            # 关键：不能把 future 留在这里等 —— 直接抛错，让调用方立刻继续。
            # 被卡住的那个解析线程会自己挂着，不影响主流程。
            raise socket.gaierror(
                socket.EAI_AGAIN,
                "DNS 解析超时（net_guard 护栏 %.1fs）：%s" % (self.timeout_s, host),
            ) from None
        except socket.gaierror:
            raise
        except Exception as exc:  # pragma: no cover
            raise socket.gaierror(
                socket.EAI_FAIL, "DNS 解析失败（net_guard）：%s" % (exc,)
            ) from None

    def uninstall(self) -> None:
        socket.getaddrinfo = self._orig
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def install_dns_timeout_guard(timeout_s: float = DEFAULT_DNS_TIMEOUT_S) -> bool:
    """给 socket.getaddrinfo 装超时护栏。返回是否成功安装。

    幂等：重复调用只生效一次（避免二次包装成"超时套超时"）。
    """
    global _installed, _original_getaddrinfo

    if _installed:
        return True
    try:
        _original_getaddrinfo = socket.getaddrinfo
        guard = _DnsTimeout(timeout_s)
        socket.getaddrinfo = guard  # type: ignore[assignment]
        _installed = True
        print(
            "[net-guard] DNS 超时护栏已启用 (%.1fs) —— 解析卡死不再永久焊死事件循环"
            % timeout_s,
            flush=True,
        )
        return True
    except Exception as exc:  # pragma: no cover
        print(
            "[net-guard] 安装失败（不阻断启动）: %s: %s"
            % (type(exc).__name__, str(exc)[:120]),
            flush=True,
        )
        return False


def is_installed() -> bool:
    return _installed


if __name__ == "__main__":  # 手动自测
    import time

    ok = install_dns_timeout_guard(3.0)
    print("installed:", ok)
    t0 = time.perf_counter()
    try:
        print(socket.getaddrinfo("api.minimax.chat", 443))
    except Exception as exc:
        print("resolve failed: %s" % exc)
    print("elapsed: %.2fs" % (time.perf_counter() - t0))
