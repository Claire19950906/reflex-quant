#!/usr/bin/env python3
"""connect-only 重试的 requests 适配器 —— 缓解 LetsVPN/TUN 分流对新域名的瞬时握手失败。

设计要点（吸取 OKX 熔断 + quote_sources 的教训）：
  - 只重试「连接建立」失败（DNS/TCP/TLS 握手被重置/超时）：urllib3 Retry(connect=N, read=0)。
  - 绝不重试「读取」失败：read 挂起时若重试，会把超时放大成“卡住”。
  - 绝不重试 HTTP 状态码：status=0 + raise_on_status=False，4xx/5xx 由 raise_for_status 抛出。

用法:
    from http_retry import new_retry_session, retry_adapter

    s = new_retry_session(trust_env=True)          # 走 env 代理（币安 fapi 类）
    s = new_retry_session(trust_env=False)         # 强制直连（binance.vision 类）

    # 或给现有 session 只挂重试虚线：
    # s.mount("https://", retry_adapter())
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DEFAULT_ATTEMPTS = 2          # 连接建立总尝试次数（1 次重试）
_DEFAULT_BACKOFF = 0.5        # 首次退避秒（urllib3 按 backoff_factor 递增）


def retry_adapter(
    attempts: int = _DEFAULT_ATTEMPTS,
    backoff: float = _DEFAULT_BACKOFF,
    pool_connections: int = 12,
    pool_maxsize: int = 24,
) -> HTTPAdapter:
    """只对连接建立失败重试的 adapter；read/status 一律不重试。"""
    retry = Retry(
        connect=attempts,
        read=0,
        status=0,
        redirect=0,
        backoff_factor=backoff,
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    return HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=retry,
    )


def new_retry_session(
    trust_env: bool = True,
    pool_connections: int = 12,
    pool_maxsize: int = 24,
) -> Any:
    """带 connect-only 重试 + 大连接池的共享 Session（requests.Session 线程安全，可全局复用）。"""
    session = requests.Session()
    session.trust_env = trust_env
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    adapter = retry_adapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session