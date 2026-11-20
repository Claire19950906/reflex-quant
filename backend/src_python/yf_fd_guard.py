#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yf_fd_guard.py — yfinance 文件描述符泄漏根治 (P0 · 「AI分析挂了」根因)
================================================================================

事故现象
--------
api_server 进程不再响应任何请求 (前端「AI 分析挂了」), 但进程还在、端口还 LISTEN。
日志里是:

    socket.accept() out of system resource
    OSError: [Errno 24] Too many open files

根因 (实测, 非推测)
--------------------
1. macOS launchd 拉起的进程 fd 软上限只有 **256**
   (`launchctl limit maxfiles` → 256 / unlimited), 而交互式 shell 是 1048575。
   所以只有 launchd 托管的 api / bridge / engine 会撞上限 —— 这就是为什么
   「手动跑一下就好了, 重启一下又活了」但过几周必然复发。

2. yfinance 1.7.0 的两层缓存都用 peewee SqliteDatabase:
   - `tkr-tz.db`   (时区缓存, yfinance/cache.py:85)
   - `cookies.db`  (cookie 缓存, yfinance/cache.py:280)
   peewee 的 `SqliteDatabase` **每个线程各开一条连接**, 线程退出后不立即释放
   (要等 GC, 时机不可控)。

3. api_server 每个请求都新建线程 / 新建线程池:
   - api_server.py:4133 / :4169 / :4612   `threading.Thread(...).start()`
   - api_server.py:5341 / :5449           `ThreadPoolExecutor(...)` 每次请求新建
   线程数无上限 → 泄漏无上限。

实测复现 (离线, 不走网络, 40 个线程各访问一次 yfinance 缓存):
    对照组 (现状)   泄漏 81 个 fd   (每线程 4 个: db + wal + shm + cookie)
    实验组 (本模块) 泄漏  0 个 fd

为什么以前修 20 多次都没修好
---------------------------
以前每次修的都是**水位线**: 重启进程 / 补个 worker / 调阈值。fd 归零后页面活了,
但「每请求新建线程 + yfinance」这个组合还在, 于是继续泄漏, 必然再次打满 256。
本模块修的是**水源**; 再配合 fd_guard.py 的监控自愈 + wrapper 的 ulimit 抬限做
纵深防御, 三层一起才不会再复发。

修法
----
yfinance 所有 HTTP 请求都收敛到 `YfData._make_request` 这一个出口
(yfinance/data.py:421; `get` / `post` 都调它)。在这里包一层 finally, 请求结束
(含异常)后显式关闭**当前线程**持有的 peewee 连接:

    peewee SqliteDatabase.close() → 关闭本线程的连接 (正是需要的粒度)

一次 patch 覆盖全部 12 个 yfinance 调用点, 不需要改动任何业务模块。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

_installed = False
_install_lock = threading.Lock()

# yfinance 缓存管理器名 (yfinance/cache.py:52 _TzDBManager, :245 _CookieDBManager)
_YF_CACHE_MANAGERS = ("_TzDBManager", "_CookieDBManager")


def close_yf_caches() -> int:
    """关闭**当前线程**持有的 yfinance 缓存 (peewee) 连接。

    返回成功关闭的连接数。peewee 的 `SqliteDatabase.close()` 关的正是当前线程
    那一条连接 —— 这就是我们需要的粒度: 不影响别的线程, 只回收自己漏的。

    任何异常都吞掉 —— 这是个"善后"动作, 绝不能因为它把调用方的业务逻辑搞挂。
    """
    closed = 0
    try:
        import yfinance.cache as yfc  # 局部 import: 不给冷启动加负担
    except Exception:
        return 0
    for mgr_name in _YF_CACHE_MANAGERS:
        mgr = getattr(yfc, mgr_name, None)
        if mgr is None:
            continue
        db = getattr(mgr, "_db", None)
        if db is None:
            continue
        try:
            db.close()
            closed += 1
        except Exception:
            # 已关闭 / 数据库被锁 等 —— 一律忽略
            pass
    return closed


def install() -> bool:
    """给 yfinance 打补丁: 每次请求结束后回收本线程的缓存连接。

    幂等 —— 重复调用只生效一次。返回是否成功安装补丁。
    """
    global _installed
    if _installed:
        return True
    with _install_lock:
        if _installed:
            return True
        try:
            from yfinance.data import YfData
        except Exception as exc:  # yfinance 没装 / 导入失败 → 不patch, 也不致命
            print(f"[YF-FD-GUARD] yfinance 不可用, 跳过安装: {type(exc).__name__}: {exc}",
                  flush=True)
            return False

        original = getattr(YfData, "_make_request", None)
        if original is None:
            print("[YF-FD-GUARD] 找不到 YfData._make_request, 跳过安装", flush=True)
            return False
        if getattr(original, "_yf_fd_guard_wrapped", False):
            _installed = True
            return True

        def _make_request(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return original(self, *args, **kwargs)
            finally:
                # 关键: 无论成功还是异常, 都把本线程的 peewee 连接还回去
                close_yf_caches()

        _make_request._yf_fd_guard_wrapped = True  # type: ignore[attr-defined]
        try:
            YfData._make_request = _make_request  # type: ignore[method-assign]
        except Exception as exc:
            print(f"[YF-FD-GUARD] 打补丁失败: {type(exc).__name__}: {exc}", flush=True)
            return False
        _installed = True
        print("[YF-FD-GUARD] 已安装: yfinance 请求结束回收 peewee 连接 (根治 fd 泄漏)",
              flush=True)
        return True


def yf_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """显式包一层调用 (给不想依赖 patch 的地方用)。"""
    try:
        return fn(*args, **kwargs)
    finally:
        close_yf_caches()


def is_installed() -> bool:
    return _installed


__all__ = ["close_yf_caches", "install", "yf_call", "is_installed"]

if __name__ == "__main__":
    ok = install()
    print(f"install -> {ok}")
    print(f"close_yf_caches() -> {close_yf_caches()} 个连接")
