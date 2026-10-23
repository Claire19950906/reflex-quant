#!/usr/bin/env python3
"""
Trident Agent MVP -- Binance USDT-M Futures REST client (stdlib-only)

设计：避开 ccxt（cryptography 在 Python 3.14 上无预编译 wheel），
用 stdlib `urllib` + `hmac` + `hashlib` + `json` 直接发 HMAC-SHA256
签名请求。覆盖 exchangeInfo / ticker / account / positionRisk /
new_order / cancel_order / openOrders 七个 endpoint。

testnet 切换：base_url 替换为 https://testnet.binancefuture.com
凭据由调用方传入；本模块不 import config（环测友好）。

注意：
  - MVP 客户端，无 connection pool / 重试；出错 raise 给上层 trade_executor 落 audit_log。
  - 所有 POST 带 recvWindow=5000 防止时钟漂移。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_BASE_FUTURES = "https://fapi.binance.com"
_BASE_FUTURES_TESTNET = "https://testnet.binancefuture.com"


class BinanceAPIError(RuntimeError):
    def __init__(self, code: int, msg: str, http_status: int = 0) -> None:
        super().__init__(f"[binance {code}] {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str,
                 testnet: bool = False, timeout: float = 5.0) -> None:
        if not api_key or not api_secret:
            raise ValueError("BinanceClient: api_key / api_secret cannot be empty")
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = _BASE_FUTURES_TESTNET if testnet else _BASE_FUTURES
        self.timeout = float(timeout)

    @staticmethod
    def _sign(query_string: str, secret: str) -> str:
        return hmac.new(
            secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, method: str, path: str,
                 params=None, signed: bool = True):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.base}{path}"
        if signed:
            sig = self._sign(qs, self.api_secret)
            url = f"{url}?{qs}&signature={sig}" if qs else f"{url}?signature={sig}"
        elif qs:
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                raise BinanceAPIError(
                    int(data.get("code", -1)),
                    str(data.get("msg", body)),
                    http_status=exc.code,
                ) from exc
            except (json.JSONDecodeError, ValueError):
                raise BinanceAPIError(exc.code, body, http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise BinanceAPIError(-1, f"network error: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def ping(self):
        return self._request("GET", "/fapi/v1/ping", signed=False)

    def server_time(self):
        return self._request("GET", "/fapi/v1/time", signed=False)

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

    def ticker_price(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v1/ticker/price", params=params, signed=False)

    def account(self):
        return self._request("GET", "/fapi/v1/account")

    def position_risk(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v1/positionRisk", params=params)

    def new_order(self, symbol: str, side: str, type_: str,
                  quantity=None, price=None,
                  time_in_force: str = "GTC",
                  reduce_only: bool = False,
                  new_client_order_id=None):
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": type_.upper(),
        }
        if quantity is not None:
            params["quantity"] = _trim_float(quantity)
        if price is not None:
            params["price"] = _trim_float(price)
        if type_.upper() in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        return self._request("POST", "/fapi/v1/order", params=params)

    def cancel_order(self, symbol: str, order_id=None, client_order_id=None):
        params = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        return self._request("DELETE", "/fapi/v1/order", params=params)

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v1/openOrders", params=params)


def _trim_float(v: float, digits: int = 8) -> float:
    s = f"{float(v):.{digits}f}".rstrip("0").rstrip(".")
    return float(s) if s else 0.0


__all__ = ["BinanceClient", "BinanceAPIError"]