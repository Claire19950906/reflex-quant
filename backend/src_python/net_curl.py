"""统一出网取数：requests 优先，失败自动退回系统 curl。

背景（2026-11-14 实测）：本机 Python 的 OpenSSL 栈到部分境外站点会在握手后被 reset
（`SSL: UNEXPECTED_EOF_WHILE_READING`），同一 URL 用系统 curl（走 SecureTransport）
却能 200。于是「同样一个接口，脚本里抓不到、shell 里抓得到」。

用法：

    import net_curl
    r = net_curl.get(url, timeout=8)      # 返回对象支持 .status_code/.text/.json()
    data = r.json()

行为：
  - 先用 requests 走正常 TLS；任何异常（含 SSLError / ConnectionError / 超时）或
    HTTP 4xx/5xx 时，改用 curl 再取一次；
  - curl 成功 → 返回轻量响应对象（status_code=200，text 为原始文本）；
  - 两边都失败 → 抛出最后一次异常，调用方按原逻辑降级。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

from market import market_data_fallback as _mdf

DEFAULT_UA = _mdf.HTTP_UA if hasattr(_mdf, "HTTP_UA") else "Mozilla/5.0"


class CurlResponse:
    """curl 结果的轻量替身，只实现调用方常用的几个成员。"""

    __slots__ = ("status_code", "text", "url")

    def __init__(self, text: str, url: str, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8", "ignore")

    def json(self, **kw: Any) -> Any:
        return json.loads(self.text, **kw)

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<CurlResponse {self.status_code} {self.url[:60]}>"


def get(url: str, timeout: float | int = 10, headers: Optional[dict] = None,
        allow_curl: bool = True, **kwargs: Any):
    """requests 优先、curl 兜底的 GET。

    `headers` 会一并带给 curl（只取 UA / Referer / Accept 三类，其余忽略）。
    """
    _timeout = max(1, int(timeout))
    last_err: Exception | None = None
    try:
        resp = requests.get(url, timeout=_timeout, headers=headers or {}, **kwargs)
        if resp.status_code < 400:
            return resp
        last_err = RuntimeError(f"HTTP {resp.status_code}")
    except Exception as exc:
        last_err = exc

    if not allow_curl:
        raise last_err if last_err else RuntimeError("请求失败")

    referer = None
    if headers:
        for k, v in headers.items():
            if str(k).lower() == "referer":
                referer = v
                break
    try:
        text = _mdf._curl_text(url, timeout=_timeout, referer=referer)
        return CurlResponse(text, url)
    except Exception as exc:
        raise (last_err or exc) from exc


def get_json(url: str, timeout: float | int = 10, **kwargs: Any) -> Any:
    return get(url, timeout=timeout, **kwargs).json()
