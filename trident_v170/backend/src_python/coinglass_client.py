#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coinglass + LSR 中转 API 客户端（v189-7）。

手册: Coinglass_LSR_API_用户使用手册_v4
服务: https://vip2.coinglass.site (默认 .env COINGLASS_BASE_URL 可覆盖)
认证: 请求头 X-API-Key (手册要求"不要把 Key 放在网址里")

实现要点（按手册逐条落实）:
- 全部 GET, 头固定 X-API-Key + Accept-Encoding: gzip, 不发 CG-API-KEY
- 启用 gzip 自动解压 (urllib 不默认解压, 必须手工 gzip.decompress)
- 限流响应头: X-Rate-Limit-Limit-User / Used-User / Remaining-User
              Retry-After (429 时严格遵守); X-API-Key-Expires-At
- 错误码分类（手册第 6 节）:
    HTTP 200 + code=40300 → 套餐不含该接口
    HTTP 400              → 参数错, 不重试
    HTTP 401              → key 无效/过期, 标 source_status.error 不重试
    HTTP 404              → 路径/币种/权限错
    HTTP 429              → 等 Retry-After, 不无限重试
    HTTP 502/503/504      → 退避 retry ≤3 次
- 所有日志打印 key 时使用 config.coinglass_key_masked() 脱敏

Usage:
    from coinglass_client import CoinglassClient, CoinglassAPIError
    cli = CoinglassClient()
    info = cli.account()
    data = cli.lsr_signal("BTC", mode="trader")
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

import config
import source_status

SOURCE_ID = "coinglass_lsr"

_CONNECT_TIMEOUT_S = 8.0
_READ_TIMEOUT_S = 12.0
_TRANSIENT_RETRIES = 3
_CODE_FORBIDDEN_PLAN = 40300


class CoinglassAPIError(Exception):
    """Coinglass/LSR API 错误（按手册第 6 节分类）."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: Optional[int] = None,
        code: Any = None,
        retry_after: Optional[float] = None,
        body_summary: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.body_summary = body_summary

    def __str__(self) -> str:
        bits = [f"{type(self).__name__}({self.kind})"]
        if self.status is not None:
            bits.append(f"http={self.status}")
        if self.code is not None:
            bits.append(f"code={self.code}")
        if self.retry_after is not None:
            bits.append(f"retry_after={self.retry_after}s")
        bits.append(self.body_summary[:80])
        return " ".join(bits)


class CoinglassClient:
    """Coinglass + LSR 中转 API 同步 HTTP 客户端."""

    def __init__(self) -> None:
        self._api_key: str = config.COINGLASS_API_KEY
        self._base_url: str = config.COINGLASS_BASE_URL

    def _request(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        etag: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], bytes, Optional[str]]:
        """单次 HTTP GET. 返回 (status, headers_lower, raw_body, new_etag)."""
        url = self._base_url + path
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                url = url + ("&" if "?" in url else "?") + qs
        req = urllib.request.Request(
            url,
            headers={
                "X-API-Key": self._api_key,
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
                "User-Agent": "TridentAgentMVP/1.0",
                **({"If-None-Match": etag} if etag else {}),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_READ_TIMEOUT_S) as resp:
                status = resp.status
                raw_headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read()
                if raw_headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                new_etag = raw_headers.get("etag")
                return status, raw_headers, raw, new_etag
        except urllib.error.HTTPError as exc:
            raw_headers = {k.lower(): v for k, v in exc.headers.items()}
            raw = exc.read()
            if raw_headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            new_etag = raw_headers.get("etag")
            kind, retry_after = _classify_http_error(exc.code, raw_headers)
            summary = _safe_body_summary(raw)
            raise CoinglassAPIError(
                f"{kind} (path={path}, http={exc.code})",
                kind=kind,
                status=exc.code,
                retry_after=retry_after,
                body_summary=summary,
            ) from exc
        except urllib.error.URLError as exc:
            raise CoinglassAPIError(
                f"network error: {exc.reason}",
                kind="network",
            ) from exc

    def get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        etag: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, str], Optional[str]]:
        """GET 接口并解析 JSON. 返回 (body_dict, headers, new_etag)."""
        status, headers, raw, new_etag = self._request(path, params=params, etag=etag)
        if status == 304:
            return {"_not_modified": True}, headers, new_etag
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CoinglassAPIError(
                f"malformed JSON (path={path})",
                kind="malformed",
                status=status,
                body_summary=_safe_body_summary(raw),
            ) from exc
        if isinstance(body, dict):
            code = body.get("code")
            if code is not None and code != 0 and code != "0" and code == _CODE_FORBIDDEN_PLAN:
                raise CoinglassAPIError(
                    f"plan doesn't include this endpoint (path={path})",
                    kind="forbidden_plan",
                    status=200,
                    code=code,
                    body_summary=json.dumps(body, ensure_ascii=False)[:120],
                )
            if body.get("success") is False:
                msg = str(body.get("msg") or body.get("message") or "")
                raise CoinglassAPIError(
                    f"LSR success=false (path={path}): {msg}",
                    kind="malformed",
                    status=200,
                    body_summary=json.dumps(body, ensure_ascii=False)[:120],
                )
        return body, headers, new_etag

    def account(self) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """GET /api/gateway/account — 套餐/剩余/到期 (不消耗 LSR/Coinglass 额度)."""
        body, headers, _ = self.get("/api/gateway/account")
        return body, headers

    def coins_markets(self, per_page: int = 10) -> Dict[str, Any]:
        """GET /api/futures/coins-markets?per_page=N — Coinglass 标准版行情."""
        body, _headers, _ = self.get(
            "/api/futures/coins-markets",
            params={"per_page": max(1, min(100, int(per_page)))},
        )
        return body


def _classify_http_error(status: int, headers: Dict[str, str]) -> Tuple[str, Optional[float]]:
    """按手册第 6 节分类 HTTP 错误码."""
    if status == 400:
        return "bad_request", None
    if status == 401:
        return "unauthorized", None
    if status == 404:
        return "not_found", None
    if status == 429:
        ra_raw = headers.get("retry-after")
        try:
            return "rate_limited", float(ra_raw) if ra_raw else None
        except (TypeError, ValueError):
            return "rate_limited", None
    if status in (502, 503, 504):
        return "transient", None
    return "http_error", None


def _safe_body_summary(raw: bytes) -> str:
    try:
        return raw[:200].decode("utf-8", errors="replace")
    except Exception:
        return "<binary>"


def extract_rate_limit(headers: Dict[str, str]) -> Dict[str, Optional[int]]:
    """从响应头读限流信息 (手册: X-Rate-Limit-* / Retry-After)."""
    def _to_int(name: str) -> Optional[int]:
        v = headers.get(name)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    ra_raw = headers.get("retry-after")
    try:
        retry_after = float(ra_raw) if ra_raw else None
    except (TypeError, ValueError):
        retry_after = None
    return {
        "limit": _to_int("x-rate-limit-limit-user"),
        "used": _to_int("x-rate-limit-used-user"),
        "remaining": _to_int("x-rate-limit-remaining-user"),
        "retry_after": retry_after,
        "expires_at": headers.get("x-api-key-expires-at"),
    }


def call_with_backoff(
    fn,
    *,
    max_transient_retries: int = _TRANSIENT_RETRIES,
):
    """包装 fn() 调用, 处理 502/503/504 退避 + 429 等待.

    fn 应该是接受任意形参并返回 (body, headers, etag) 的 callable.
    业务码错误直接抛, 不重试.
    """
    delay = 0.5
    last_exc: Optional[CoinglassAPIError] = None
    for attempt in range(max_transient_retries + 1):
        try:
            return fn()
        except CoinglassAPIError as exc:
            last_exc = exc
            if exc.kind == "transient" and attempt < max_transient_retries:
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
                continue
            if exc.kind == "rate_limited" and exc.retry_after and attempt < max_transient_retries:
                time.sleep(min(exc.retry_after, 30.0))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("call_with_backoff: exhausted without exception")


def startup_self_check() -> Optional[str]:
    """在 lifespan 启动时调用. 返回 None=OK; string=skip 原因."""
    if not config.COINGLASS_ENABLED:
        return "COINGLASS_ENABLED=0"
    if not config.COINGLASS_API_KEY:
        return "COINGLASS_API_KEY not set"
    try:
        cli = CoinglassClient()
        body, headers = cli.account()
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            api_key_info = data.get("api_key") or {}
            rate = data.get("rate_limits") or {}
            expires_at = api_key_info.get("expires_at", "?")
            remaining_days = api_key_info.get("remaining_days", "?")
            lsr_plan = (rate.get("lsr") or {}).get("plan", "?")
            cg_plan = (rate.get("coinglass") or {}).get("plan", "?")
            print(
                f"[COINGLASS] key={config.coinglass_key_masked()} "
                f"expires={expires_at} (剩 {remaining_days} 天) | "
                f"coinglass={cg_plan} lsr={lsr_plan}",
                flush=True,
            )
            if isinstance(remaining_days, int) and remaining_days <= 3:
                print(
                    f"[COINGLASS] ⚠️  key 即将过期 ({remaining_days} 天), "
                    f"尽快联系服务方续期",
                    flush=True,
                )
        source_status.report(SOURCE_ID, True, latency_ms=None)
        return None
    except CoinglassAPIError as exc:
        msg = f"{exc.kind} (status={exc.status}, code={exc.code})"
        source_status.report(SOURCE_ID, False, error=msg[:120])
        return msg
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:80]}"
        source_status.report(SOURCE_ID, False, error=msg[:120])
        return msg