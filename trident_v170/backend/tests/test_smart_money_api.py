# -*- coding: utf-8 -*-
"""smart-money 端点 pytest(v285+).

覆盖:
  · /api/smart-money/fills  合法地址 → 返回规范化 fills
  · /api/smart-money/fills  非法地址 → 400
  · /api/smart-money/state  合法地址(空持仓)→ 200 + 空 positions
  · /api/smart-money/state  有持仓 → side/leverage 正确标注
  · _hl_info 缓存命中(同 key 5s 内只打 1 次远端)
  · _hl_info 超时 → 504
  · _hl_info 5xx → 502
"""
from __future__ import annotations

import sys
import time
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src_python"))


@pytest.fixture(scope="module")
def _server():
    """延迟 import api_server — 避免顶层 import 时相对导入失败."""
    from fastapi.testclient import TestClient
    import api_server
    return api_server, TestClient(api_server.app)


VALID_ADDR = "0xc8b527864ef2ad6dc49de7e99943a3a76ad48891"
VALID_ADDR_UC = VALID_ADDR.upper()


class _ok_resp:
    def __init__(self, body: Any):
        self._body = body
        self.status_code = 200
        self.text = str(body)
        self.headers = {"content-type": "application/json"}
    def json(self):
        return self._body


# ── /api/smart-money/fills ───────────────────────────────────────────

def test_fills_happy_path(_server):
    api_server, client = _server
    canned = [
        {"coin": "BTC", "px": "65000", "sz": "0.5", "side": "B",
         "time": 1791477123677, "startPosition": "0.0",
         "dir": "Open Long", "closedPnl": "0",
         "hash": "0xabc", "oid": 1, "crossed": False,
         "fee": "0.05", "tid": 111, "feeToken": "USDC", "twapId": None},
        {"coin": "ETH", "px": "3500", "sz": "2.0", "side": "A",
         "time": 1791477200000, "startPosition": "2.0",
         "dir": "Close Long", "closedPnl": "120.5",
         "hash": "0xdef", "oid": 2, "crossed": True,
         "fee": "0.07", "tid": 222, "feeToken": "USDC", "twapId": None},
    ]
    with patch.object(api_server, "_hl_info", return_value=canned) as m:
        r = client.get(f"/api/smart-money/fills?address={VALID_ADDR}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["address"] == VALID_ADDR  # 归一为小写
    assert body["count"] == 2
    f0, f1 = body["fills"]
    assert f0["coin"] == "BTC" and f0["side"] == "B" and f0["dir"] == "Open Long"
    assert f0["closed_pnl"] == "0"      # 字段名已重命名
    assert f0["tid"] == 111
    assert f1["dir"] == "Close Long" and f1["crossed"] is True
    m.assert_called_once_with({"type": "userFills", "user": VALID_ADDR}, f"fills:{VALID_ADDR}")


@pytest.mark.parametrize("bad", [
    "not-an-address",
    "0x123",
    "0xZZZ527864ef2ad6dc49de7e99943a3a76ad48891",
    "",
    "c8b527864ef2ad6dc49de7e99943a3a76ad48891",
])
def test_fills_bad_address_400(bad, _server):
    _, client = _server
    r = client.get(f"/api/smart-money/fills?address={bad}")
    assert r.status_code == 400
    assert "0x" in r.json()["detail"]


def test_fills_uppercase_normalized(_server):
    api_server, client = _server
    with patch.object(api_server, "_hl_info", return_value=[]) as m:
        r = client.get(f"/api/smart-money/fills?address={VALID_ADDR_UC}")
    assert r.status_code == 200
    assert r.json()["address"] == VALID_ADDR
    args, _ = m.call_args
    assert args[0]["user"] == VALID_ADDR
    assert args[1] == f"fills:{VALID_ADDR}"


def test_fills_hyperliquid_returns_non_list_502(_server):
    api_server, client = _server
    with patch.object(api_server, "_hl_info", return_value={"error": "rate limited"}):
        r = client.get(f"/api/smart-money/fills?address={VALID_ADDR}")
    assert r.status_code == 502
    assert "unexpected shape" in r.json()["detail"]


def test_fills_malformed_fill_skipped_not_500(_server):
    """坏 fill 缺关键字段 → 因 .get() 返回 None,endpoint 不应崩(200 而非 500)."""
    api_server, client = _server
    canned = [
        {"coin": "BTC", "px": "1", "sz": "1", "side": "B", "time": 1,
         "startPosition": "0", "dir": "Open Long", "closedPnl": "0",
         "hash": "0xa", "oid": 1, "crossed": False, "fee": "0", "tid": 1, "feeToken": "USDC"},
        {"coin": "ETH"},  # 缺字段 → .get() 全部 None,不会抛
    ]
    with patch.object(api_server, "_hl_info", return_value=canned):
        r = client.get(f"/api/smart-money/fills?address={VALID_ADDR}")
    assert r.status_code == 200
    body = r.json()
    # endpoint 容忍缺字段(不崩),fill 全部归一化(None 字段透传)
    assert body["count"] == 2
    assert body["fills"][0]["coin"] == "BTC" and body["fills"][0]["tid"] == 1
    assert body["fills"][1]["coin"] == "ETH" and body["fills"][1]["tid"] is None


# ── /api/smart-money/state ───────────────────────────────────────────

def test_state_empty_positions_ok(_server):
    """空持仓 = API 正常返回,前端会显示「当前 0 持仓」."""
    api_server, client = _server
    canned = {
        "marginSummary": {"accountValue": "0", "totalNtlPos": "0",
                          "totalRawUsd": "0", "totalMarginUsed": "0"},
        "withdrawable": "0",
        "assetPositions": [],
        "time": 1791551805806,
    }
    with patch.object(api_server, "_hl_info", return_value=canned):
        r = client.get(f"/api/smart-money/state?address={VALID_ADDR}")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert body["account_value"] == "0"


def test_state_long_and_short_side_labeled(_server):
    """正 szi → long,负 szi → short;leverage/value/type 透传."""
    api_server, client = _server
    canned = {
        "marginSummary": {"accountValue": "1000", "totalNtlPos": "5000",
                          "totalRawUsd": "1000", "totalMarginUsed": "100"},
        "withdrawable": "900",
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.5", "entryPx": "60000",
                          "positionValue": "32500", "unrealizedPnl": "2500",
                          "liquidationPx": "55000"},
             "leverage": {"value": 5, "type": "cross"}},
            {"position": {"coin": "ETH", "szi": "-3.0", "entryPx": "3500",
                          "positionValue": "10800", "unrealizedPnl": "-300",
                          "liquidationPx": "4000"},
             "leverage": {"value": 3, "type": "isolated"}},
        ],
    }
    with patch.object(api_server, "_hl_info", return_value=canned):
        r = client.get(f"/api/smart-money/state?address={VALID_ADDR}")
    assert r.status_code == 200
    body = r.json()
    assert body["account_value"] == "1000"
    assert len(body["positions"]) == 2
    p0, p1 = body["positions"]
    assert p0["coin"] == "BTC" and p0["side"] == "long" and p0["leverage_value"] == 5
    assert p0["leverage_type"] == "cross"
    assert p1["coin"] == "ETH" and p1["side"] == "short" and p1["leverage_type"] == "isolated"


def test_state_non_dict_returns_502(_server):
    api_server, client = _server
    with patch.object(api_server, "_hl_info", return_value=[1, 2, 3]):
        r = client.get(f"/api/smart-money/state?address={VALID_ADDR}")
    assert r.status_code == 502


# ── _hl_info 缓存 + 错误传播 ─────────────────────────────────────────

def test_hl_info_cache_hit_skips_remote(_server):
    api_server, _ = _server
    api_server._HL_CACHE.clear()
    with patch.object(api_server.requests, "post", return_value=_ok_resp([1, 2])) as m:
        a = api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k1")
        b = api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k1")
    assert a == b == [1, 2]
    m.assert_called_once()


def test_hl_info_cache_ttl_expires(_server):
    api_server, _ = _server
    api_server._HL_CACHE.clear()
    with patch.object(api_server.requests, "post", return_value=_ok_resp([1])) as m:
        api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k1")
        # 把过期时间拨到过去
        exp, val = api_server._HL_CACHE["k1"]
        api_server._HL_CACHE["k1"] = (time.time() - 1, val)
        api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k1")
    assert m.call_count == 2


def test_hl_info_timeout_raises_504(_server, monkeypatch):
    api_server, _ = _server
    api_server._HL_CACHE.clear()
    import requests as _rq
    monkeypatch.setattr(api_server.requests, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(_rq.Timeout("simulated")))
    with pytest.raises(Exception) as exc:
        api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k_unique_t")
    assert getattr(exc.value, "status_code", None) == 504


def test_hl_info_5xx_raises_502(_server, monkeypatch):
    api_server, _ = _server
    api_server._HL_CACHE.clear()
    class _R:
        status_code = 503
        text = "service unavailable"
    monkeypatch.setattr(api_server.requests, "post", lambda *a, **kw: _R())
    with pytest.raises(Exception) as exc:
        api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k_unique_5")
    assert getattr(exc.value, "status_code", None) == 502


def test_hl_info_non_json_raises_502(_server, monkeypatch):
    api_server, _ = _server
    api_server._HL_CACHE.clear()
    class _R:
        status_code = 200
        text = "<html>not json</html>"
        def json(self):
            raise ValueError("not json")
    monkeypatch.setattr(api_server.requests, "post", lambda *a, **kw: _R())
    with pytest.raises(Exception) as exc:
        api_server._hl_info({"type": "x", "user": VALID_ADDR}, "k_unique_nj")
    assert getattr(exc.value, "status_code", None) == 502
