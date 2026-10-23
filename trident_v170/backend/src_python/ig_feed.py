# -*- coding: utf-8 -*-
"""IG Markets 客户情绪指数（散户多空比）—— 宏观/舆情数据源。

认证三步：请求头带 X-IG-API-KEY + Version:2，POST /gateway/deal/session(identifier+password)
换取 CST + X-SECURITY-TOKEN，之后带这两个头调 /gateway/deal/sentiment/{epic}。

仅配 IG_API_KEY 无法登录；IG_IDENTIFIER / IG_PASSWORD 必填。失败逐个 unavailable（不造数）。
返回 row 结构与 macro_context._metric 一致（category/source/metric_key/value/unit/status/payload）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

import config
import source_status

_EPICS: Dict[str, str] = {
    "XAU": "CS.D.CFDGOLD.CFDGC.IP",
    "WTI": "CC.D.NYMEXCL.CFD.IP",
    "BTC": "CS.D.BTCUSD.CFD.IP",
}

_session: Optional[Dict[str, str]] = None
_last_login_ts: float = 0.0
_SESSION_TTL = 1800.0

_cfg_cache: Optional[Dict[str, str]] = None


def _cfg() -> Dict[str, str]:
    global _cfg_cache
    if _cfg_cache is None:
        env = str(getattr(config, "IG_ENV", "demo") or "demo").strip().lower()
        base = str(getattr(config, "IG_BASE_URL", "") or "").strip()
        if not base:
            base = "https://api.ig.com" if env == "live" else "https://demo-api.ig.com"
        _cfg_cache = {
            "api_key": str(getattr(config, "IG_API_KEY", "") or "").strip(),
            "identifier": str(getattr(config, "IG_IDENTIFIER", "") or "").strip(),
            "password": str(getattr(config, "IG_PASSWORD", "") or "").strip(),
            "env": env,
            "base_url": base.rstrip("/"),
        }
    return _cfg_cache


def _login() -> Optional[Dict[str, str]]:
    global _session, _last_login_ts
    now = time.time()
    if _session and now - _last_login_ts < _SESSION_TTL:
        return _session
    cfg = _cfg()
    if not cfg["api_key"]:
        source_status.report("ig", False, error="未配置 IG_API_KEY")
        return None
    if not cfg["identifier"] or not cfg["password"]:
        source_status.report("ig", False, error="未配置 IG_IDENTIFIER / IG_PASSWORD（登录凭据）")
        return None
    url = f"{cfg['base_url']}/gateway/deal/session"
    headers = {
        "X-IG-API-KEY": cfg["api_key"],
        "Version": "2",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "TridentAgentMVP/1.0",
    }
    payload = {"identifier": cfg["identifier"], "password": cfg["password"]}
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=12)
            if resp.status_code != 200:
                source_status.report("ig", False, error=f"session HTTP {resp.status_code}")
                return None
            cst = resp.headers.get("CST")
            token = resp.headers.get("X-SECURITY-TOKEN")
            if not cst or not token:
                source_status.report("ig", False, error="session 缺 CST/X-SECURITY-TOKEN")
                return None
            _session = {"CST": cst, "X-SECURITY-TOKEN": token}
            _last_login_ts = now
            return _session
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    source_status.report("ig", False, error=f"登录失败 {type(last_exc).__name__}" if last_exc else "登录失败")
    return None


def _sentiment_long_pct(epic: str) -> Optional[float]:
    session = _session
    if not session:
        return None
    cfg = _cfg()
    try:
        resp = requests.get(
            f"{cfg['base_url']}/gateway/deal/sentiment/{epic}",
            headers={
                "X-IG-API-KEY": cfg["api_key"],
                "Version": "2",
                "Accept": "application/json",
                "CST": session["CST"],
                "X-SECURITY-TOKEN": session["X-SECURITY-TOKEN"],
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        pct = (resp.json() or {}).get("longPositionPercentage")
        return round(float(pct), 2) if pct is not None else None
    except Exception:
        return None


def _row(asset: str, value: Optional[float], note: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"label": asset}
    if value is not None:
        payload["short_pct"] = round(100.0 - value, 2)
    if note:
        payload["note"] = note
    return {
        "category": "structure",
        "source": "ig",
        "metric_key": f"{asset}.ig_long_pct",
        "value": value,
        "unit": "%",
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def fetch_ig_sentiment() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if _login() is None:
        for asset in _EPICS:
            rows.append(_row(asset, None, "IG 未登录（缺 identifier/password 或登录失败）"))
        return rows
    ok_any = False
    first_err = ""
    for asset, epic in _EPICS.items():
        try:
            long_pct = _sentiment_long_pct(epic)
            if long_pct is None:
                rows.append(_row(asset, None, f"EPIC {epic} 无情绪数据（账户未开通该市场？）"))
                first_err = first_err or f"{asset} 无数据"
                continue
            ok_any = True
            rows.append(_row(asset, long_pct, ""))
        except Exception as exc:
            rows.append(_row(asset, None, f"{type(exc).__name__}"))
            first_err = first_err or f"{asset} {type(exc).__name__}"
    if ok_any:
        source_status.report("ig", True, detail=f"long_pct {len(_EPICS)} 标")
    else:
        source_status.report("ig", False, error=first_err or "无情绪数据")
    return rows